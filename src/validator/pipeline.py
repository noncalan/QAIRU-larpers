"""Оркестрация шагов 0–9 с политикой отказа на каждом шаге (docs/plan.md, «Конвейер и модели»).

``run(cfg) -> RunOutcome`` не бросает исключений: сбой шага превращается в диагностику и запасной
путь, отчёт пишется всегда, когда доступна папка отчёта. Код возврата выводится из диагностики
(``errors.exit_code``). Трассировки идут только в ``report.log``.

| Шаг | Если упал |
|---|---|
| 0 вход | отчёт-диагностика, код 4 |
| 1 разбор | отчёт-диагностика, код 4 (часть текста: предупреждение, код 3) |
| 2 нарезка | запасная нарезка без модели, предупреждение |
| 3 линтер | шаг пропущен, предупреждение |
| части | план не построился: документ целиком; длинный документ: ИНФО LLM-070 с оценкой цены до вызовов |
| 4 поиск | упал проход: «K−1 из K»; упали все: отчёт по линтеру, код 5 |
| 4в сверка между частями | находки частей остаются, предупреждение LLM-072 |
| 5 слияние, 7 верификатор | внутренняя ошибка, код 1, отчёт из того, что есть |
| 6 доказательства | упал свидетель или критик: находки без него, предупреждение |
| 8 вопросы | редактор упал: вопросы кодом без редактуры; шаг упал: раздел без вопросов; предупреждение |
| 8е уточнения | файл ответов не читается: ответы не учтены; сверка упала: ответы без сверки |
| ID блоков в текстах | остаются как есть, предупреждение |
| 9 отчёт | Markdown пишется всегда, в крайнем случае аварийный; PDF не собрался: INT-002, Markdown остаётся |
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from validator.checklists import Selection, select_checklists
from validator.clarify import AnswersError, AnswersFile, clarify, load_answers
from validator.errors import (
    CFG_NOT_IMPLEMENTED,
    IN_NO_TEXT,
    IN_PARSE_FAILED,
    INT_STEP_DEGRADED,
    INT_STEP_FAILED,
    LLM_ALL_PASSES_FAILED,
    LLM_BUDGET_ESTIMATE,
    LLM_LONG_DOCUMENT,
    LLM_REPLAYED,
    OUT_OVERWRITES_INPUT,
    OUT_WRITE_FAILED,
    InputError,
    ValidatorError,
    exit_code,
)
from validator.estimate import describe_estimate, estimate_run
from validator.evidence import gather
from validator.ingest import guard, parse
from validator.ingest.quality import review
from validator.lint import lint
from validator.llm import RecordingClient, align_config, instrument, make_client, write_calls_jsonl
from validator.llm.base import LLMClient
from validator.llm.calllog import LIVE_BACKENDS
from validator.llm.replay import ReplayLLM
from validator.merge import merge
from validator.models import (
    Backend,
    CandidateFinding,
    ChecklistUse,
    Clarification,
    DiagLevel,
    Diagnostic,
    DocKind,
    ExitCode,
    GuardResult,
    MergedFinding,
    ParsedDocument,
    PassResult,
    Question,
    Report,
    RunConfig,
    RunContext,
    RunOutcome,
    Segmentation,
    SelfCheck,
    Verification,
    WithdrawnQuestion,
)
from validator.parts import PartPlan, describe, plan_parts, whole_plan
from validator.passport import build_passport
from validator.questions import build_questions, write_questions_yaml
from validator.refs import humanize
from validator.report import PdfRenderError, build_report, render_md, render_pdf
from validator.report.selfcheck import check_report, selfcheck_diagnostics
from validator.search import first_pass_request, search
from validator.segment import fallback_segmentation, segment
from validator.set_check import set_check
from validator.verify import light_with_questions, verify

logger = logging.getLogger("validator")
NOTICE_LOGGER = "validator.notice"
notice = logging.getLogger(NOTICE_LOGGER)
"""Сообщения человеку до конца прогона (длинный документ, оценка цены): CLI печатает их в stderr."""
T = TypeVar("T")
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


@dataclass
class _State:
    """Изменяемое состояние одного прогона; наружу уходит неизменяемый ``RunContext``."""

    cfg: RunConfig
    started: datetime
    diagnostics: list[Diagnostic] = field(default_factory=list)
    guard: GuardResult | None = None
    doc: ParsedDocument | None = None
    segmentation: Segmentation | None = None
    lint_hits: list[CandidateFinding] = field(default_factory=list)
    passes: list[PassResult] = field(default_factory=list)
    plan: PartPlan | None = None
    set_check: list[CandidateFinding] = field(default_factory=list)
    merged: list[MergedFinding] = field(default_factory=list)
    verification: Verification | None = None
    questions: list[Question] = field(default_factory=list)
    questions_withdrawn: list[WithdrawnQuestion] = field(default_factory=list)
    checklists: list[ChecklistUse] = field(default_factory=list)
    clarification: Clarification | None = None
    client: RecordingClient | None = None
    backend_used: Backend | None = None
    pdf_written: bool = False


def _step(
    state: _State,
    name: str,
    fn: Callable[[], T],
    fallback: Callable[[], T],
    *,
    action: str,
    level: DiagLevel = DiagLevel.WARNING,
) -> T:
    """Шаг с политикой отказа: исключение превращается в диагностику, дальше идёт запасной путь."""
    clock = time.perf_counter()
    try:
        result = fn()
    except ValidatorError as exc:
        logger.warning("Шаг %s: %s (%s)", name, exc.message, exc.code)
        state.diagnostics.append(exc.to_diagnostic(action=action, level=level, step=name))
        return fallback()
    except Exception as exc:  # граница шага: любая ошибка кода становится диагностикой, а не падением
        logger.exception("Шаг %s упал", name)
        code = INT_STEP_FAILED if level is DiagLevel.ERROR else INT_STEP_DEGRADED
        state.diagnostics.append(
            Diagnostic(
                code=code,
                level=level,
                message=f"Внутренняя ошибка на шаге {name}: {type(exc).__name__}",
                action=action,
                hint=f"Подробности в {state.cfg.log_path.name}; сообщите команде",
                step=name,
            )
        )
        return fallback()
    logger.info("Шаг %s: %.2f с", name, time.perf_counter() - clock)
    return result


def _read_input(state: _State) -> ParsedDocument | None:
    cfg = state.cfg
    state.guard = g = guard(cfg.input_path, workdir=cfg.run_dir, ocr=cfg.ocr)
    state.diagnostics.extend(g.diagnostics)
    if g.fatal:
        return None
    source = Path(g.read_path) if g.read_path else cfg.input_path
    try:
        doc = parse(source, g.kind, pdf_backend=cfg.pdf_backend, ocr=cfg.ocr)
    except InputError as exc:
        state.diagnostics.append(
            exc.to_diagnostic(action="Проверка не выполнялась: документ не прочитан", step="parse")
        )
        return None
    except Exception as exc:  # битый файл может уронить чужой парсер чем угодно: это ошибка входа
        logger.exception("Разбор входа упал")
        state.diagnostics.append(
            Diagnostic(
                code=IN_PARSE_FAILED,
                level=DiagLevel.ERROR,
                message=f"Документ повреждён или не разбирается: {type(exc).__name__}",
                action="Проверка не выполнялась: документ не прочитан",
                hint=(
                    "Пересохраните документ (Word: «Сохранить как», PDF: «Печать в PDF») "
                    "или пришлите другой формат"
                ),
                location=cfg.input_path.name,
                step="parse",
            )
        )
        return None
    if source != cfg.input_path:  # прочитана восстановленная копия: в паспорт идёт исходный файл
        doc = doc.model_copy(
            update={"path": g.path, "name": g.name, "sha256": g.sha256, "size_bytes": g.size_bytes}
        )
    doc = review(doc)
    state.diagnostics.extend(doc.diagnostics)
    if any(d.level is DiagLevel.ERROR for d in doc.diagnostics):
        return None
    if not doc.text.strip() or not doc.blocks:
        state.diagnostics.append(
            Diagnostic(
                code=IN_NO_TEXT,
                level=DiagLevel.ERROR,
                message="В документе нет текста",
                action="Проверка не выполнялась",
                hint="Пришлите документ с текстом (не скан и не пустой шаблон)",
                location=doc.name,
                step="parse",
            )
        )
        return None
    return doc


def _check_requests(state: _State) -> None:
    cfg = state.cfg
    if cfg.replay_dir is not None and cfg.backend not in (Backend.REPLAY, Backend.AUTO):
        state.diagnostics.append(
            Diagnostic(
                code=CFG_NOT_IMPLEMENTED,
                level=DiagLevel.WARNING,
                message=(
                    f"Параметр --replay работает только с --backend replay или auto, а не {cfg.backend.value}"
                ),
                action="Записанные ответы не использованы",
                step="config",
            )
        )


def _announce(state: _State, plan: PartPlan) -> None:
    """Длинный документ и оценка цены до первого вызова модели: в диагностику и сразу в stderr (#38)."""
    cfg = state.cfg
    estimate = estimate_run(plan, cfg)
    expected = describe_estimate(estimate)
    if plan.split:
        message = (
            f"Документ длинный (около {plan.tokens // 1000} тыс. токенов): проверен по частям, "
            f"частей: {len(plan.parts)}"
        )
        state.diagnostics.append(
            Diagnostic(
                code=LLM_LONG_DOCUMENT,
                level=DiagLevel.INFO,
                message=message,
                action=(
                    "Нарезка и поиск дефектов шли по каждой части, места в отчёте даны по всему документу; "
                    f"противоречия и повторы между частями сверены отдельным вызовом. Оценка: {expected}"
                ),
                step="parts",
            )
        )
        notice.info(
            "Документ длинный (около %d тыс. токенов), проверка идёт по частям, частей: %d. Оценка: %s.",
            plan.tokens // 1000,
            len(plan.parts),
            expected,
        )
    budget, usd = cfg.max_budget_usd, estimate.usd
    if budget is None or usd is None or usd <= budget or state.backend_used not in LIVE_BACKENDS:
        return
    diag = Diagnostic(
        code=LLM_BUDGET_ESTIMATE,
        level=DiagLevel.WARNING,
        message=f"Оценка цены прогона ${usd:.2f} выше лимита --max-budget-usd ${budget:.2f}",
        action="Прогон идёт; при исчерпании лимита оставшиеся вызовы не делаются, отчёт будет неполным",
        hint="Увеличьте --max-budget-usd или возьмите профиль quick (один проход поиска)",
        step="parts",
    )
    state.diagnostics.append(diag)
    notice.warning("%s. %s.", diag.message, diag.hint)


def _cross_parts(state: _State, seg: Segmentation, plan: PartPlan, llm: LLMClient) -> list[CandidateFinding]:
    outcome = set_check(seg, plan, llm, state.cfg)
    state.diagnostics.extend(outcome.diagnostics)
    return outcome.candidates


def _load_answers(state: _State) -> AnswersFile | None:
    """Файл ответов автора (--answers). CLI проверяет его до прогона (код 2), здесь сбой не роняет прогон."""
    path = state.cfg.answers_path
    if path is None:
        return None
    try:
        return load_answers(path)
    except AnswersError as exc:
        state.diagnostics.append(
            exc.to_diagnostic(
                action="Ответы автора не учтены, проверка выполнена по документу",
                level=DiagLevel.WARNING,
                step="clarify",
            )
        )
        return None


def _clarify(
    state: _State, answers: AnswersFile, doc: ParsedDocument, llm: RecordingClient, asked: bool
) -> None:
    """Шаг 8е: ответы автора → закрытые, уточнённые и новые находки, открытые вопросы, новый светофор.

    ``asked``: шаг вопросов отработал; если нет, светофор и после ответов не становится зелёным.
    """
    ver, seg = state.verification, state.segmentation
    if ver is None or seg is None:
        return
    questions, cfg = (state.questions if asked else None), state.cfg
    outcome = _step(
        state,
        "clarify",
        lambda: clarify(ver, questions, answers, doc, seg, llm, cfg),
        lambda: None,
        action="Ответы автора не учтены, проверка выполнена по документу",
    )
    if outcome is None:
        return
    state.verification = outcome.verification
    state.questions = outcome.questions
    state.clarification = outcome.clarification
    state.diagnostics.extend(outcome.diagnostics)


def _execute(state: _State) -> None:
    cfg = state.cfg
    _check_requests(state)
    answers = _load_answers(state)
    doc = _read_input(state)
    if doc is None:
        return
    state.doc = doc
    plan = _step(
        state,
        "parts",
        lambda: plan_parts(doc),
        lambda: whole_plan(doc),
        action="Документ проверяется целиком",
    )
    state.plan = plan
    client, client_diags = make_client(cfg)
    state.diagnostics.extend(client_diags)
    cfg, aligned_diags = align_config(cfg, client, parts=len(plan.parts))
    state.cfg = cfg
    state.diagnostics.extend(aligned_diags)
    llm = instrument(cfg, client)
    state.client, state.backend_used = llm, client.backend
    _announce(state, plan)
    seg = _step(
        state,
        "segment",
        lambda: segment(doc, llm, cfg, plan),
        lambda: fallback_segmentation(doc),
        action="Нарезка сделана запасным путём без модели",
    )
    state.segmentation = seg
    state.diagnostics.extend(seg.diagnostics)
    state.lint_hits = _step(state, "lint", lambda: lint(doc, seg), list, action="Линтер пропущен")
    cfg = _search_step(state, doc, plan, client, llm)
    state.set_check = _step(
        state,
        "set_check",
        lambda: _cross_parts(state, seg, plan, llm),
        list,
        action="Противоречия и повторы между частями не сверены",
    )
    passes, rules = state.passes, [*state.lint_hits, *state.set_check]
    state.merged = _step(
        state,
        "merge",
        lambda: merge(passes, rules, doc),
        list,
        action="Находки не слиты",
        level=DiagLevel.ERROR,
    )
    merged = state.merged
    proven = _step(
        state,
        "evidence",
        lambda: gather(merged, doc, llm, cfg),
        lambda: None,
        action="Находки идут без примера-свидетеля и без решения критика",
    )
    if proven is not None:
        state.merged = proven.findings
        state.diagnostics.extend(proven.diagnostics)
    found = state.merged
    state.verification = _step(
        state,
        "verify",
        lambda: verify(found, doc, seg),
        lambda: None,
        action="Находки не сверены и не вошли в отчёт",
        level=DiagLevel.ERROR,
    )
    ver = state.verification
    if ver is not None:
        state.diagnostics.extend(ver.diagnostics)
        asked = _step(
            state,
            "questions",
            lambda: build_questions(ver, doc, seg, llm, cfg),
            lambda: None,
            action="Раздел вопросов собран без них",
        )
        if asked is not None:
            state.questions = list(asked.questions)
            state.questions_withdrawn = list(asked.withdrawn)
            state.diagnostics.extend(asked.diagnostics)
        state.verification = light_with_questions(ver, state.questions if asked is not None else None)
        if answers is not None:
            _clarify(state, answers, doc, llm, asked is not None)
    before = (state.verification, state.questions, state.questions_withdrawn)
    state.verification, state.questions, state.questions_withdrawn = _step(
        state,
        "refs",
        lambda: humanize(*before, doc, seg),
        lambda: before,
        action="ID блоков в текстах моделей оставлены как есть",
    )


def _search_step(
    state: _State, doc: ParsedDocument, plan: PartPlan, client: LLMClient, llm: LLMClient
) -> RunConfig:
    """Шаг 4 с выбором доменных чек-листов перед ним; возвращает конфигурацию после выбора (#35)."""
    chosen = _step(
        state,
        "checklists",
        lambda: _choose_checklists(state, doc, plan, client),
        lambda: None,
        action="Поиск идёт без доменных чек-листов",
    )
    cfg = state.cfg
    state.checklists = list(chosen.uses) if chosen else []
    active = chosen.active if chosen else ()
    state.passes = _step(
        state, "search", lambda: search(doc, llm, cfg, plan, active), list, action="Поиск пропущен"
    )
    for p in state.passes:
        state.diagnostics.extend(p.diagnostics)
    if not any(p.ok for p in state.passes):
        state.diagnostics.append(
            Diagnostic(
                code=LLM_ALL_PASSES_FAILED,
                level=DiagLevel.ERROR,
                message="Ни один проход поиска дефектов не удался",
                action="Отчёт собран по правилам линтера без модели",
                hint="Проверьте доступ к модели (см. диагностику выше) и запустите снова",
                step="search",
            )
        )
    return cfg


def _choose_checklists(state: _State, doc: ParsedDocument, plan: PartPlan, client: LLMClient) -> Selection:
    """Доменные чек-листы для поиска (#35). Replay записи без них (до #35 или ``--no-checklists``)
    выключает их сам: иначе проход искался бы по другому ключу и брался бы из записи по номеру вызова."""
    cfg = state.cfg
    chosen = select_checklists(doc, enabled=cfg.checklists)
    if not (isinstance(client, ReplayLLM) and chosen.active):
        return chosen
    with_lists = first_pass_request(doc, plan, cfg, chosen.active).key
    without = first_pass_request(doc, plan, cfg).key
    if client.has_key(with_lists) or not client.has_key(without):
        return chosen
    state.cfg = cfg.model_copy(update={"checklists": False})
    state.diagnostics.append(
        Diagnostic(
            code=LLM_REPLAYED,
            level=DiagLevel.INFO,
            message="Проход поиска в записи сделан без доменных чек-листов",
            action="Replay идёт без чек-листов, как в записи",
            step="checklists",
        )
    )
    return select_checklists(doc, enabled=False)


# ---------------------------------------------------------------- выходы


def _context(
    state: _State, finished: datetime, code: ExitCode, extra: Sequence[Diagnostic] = ()
) -> RunContext:
    cfg, doc, g = state.cfg, state.doc, state.guard
    calls = state.client.records if state.client else []
    passport = build_passport(
        cfg,
        document_name=doc.name if doc else cfg.input_path.name,
        document_kind=doc.kind if doc else (g.kind if g else DocKind.UNKNOWN),
        document_sha256=doc.sha256 if doc else (g.sha256 if g else None),
        document_size_bytes=doc.size_bytes if doc else (g.size_bytes if g else None),
        document_pages=doc.pages if doc else None,
        started_at=state.started,
        finished_at=finished,
        backend_used=state.backend_used,
        calls=calls,
        exit_code=code,
        checklists=[u.id for u in state.checklists if u.used],
    )
    return RunContext(
        cfg=cfg,
        started_at=state.started,
        finished_at=finished,
        exit_code=code,
        guard=g,
        doc=doc,
        segmentation=state.segmentation,
        lint_hits=state.lint_hits,
        passes=state.passes,
        set_check=state.set_check,
        parts=describe(state.plan, doc) if state.plan is not None and doc is not None else [],
        merged=state.merged,
        verification=state.verification,
        questions=state.questions,
        questions_withdrawn=state.questions_withdrawn,
        clarification=state.clarification,
        diagnostics=[*state.diagnostics, *extra],
        calls=list(calls),
        passport=passport,
        checklists=state.checklists,
    )


def _emergency_md(state: _State, code: ExitCode) -> str:
    """Отчёт без шаблона и без паспорта, если сборка отчёта сама упала: что известно и диагностика."""
    cfg, g = state.cfg, state.guard
    lines = [
        f"# Отчёт о проверке требований: {cfg.input_path.name}",
        "",
        "Отчёт собран в аварийном режиме: основной шаблон отчёта не сработал.",
        "",
        "## Паспорт прогона",
        "",
        f"- Документ: {cfg.input_path.name}, SHA-256: {(g.sha256 if g else None) or '—'}",
        f"- Дата прогона: {state.started.isoformat()}",
        f"- Код завершения: {code.value}: {code.meaning}",
        "",
        "## Диагностика",
        "",
    ]
    lines += [
        f"- {d.code} {d.level.label}: {d.message}. {d.action} {d.hint}".rstrip() for d in state.diagnostics
    ]
    return "\n".join(lines) + "\n"


def _selfcheck(state: _State, report: Report) -> tuple[SelfCheck | None, list[Diagnostic]]:
    """Самопроверка отчёта (#43). Её собственный сбой не должен стоить отчёта: предупреждение INT-002."""
    try:
        check = check_report(report, render_md(report), state.doc.text if state.doc else None)
        return check, selfcheck_diagnostics(check)
    except Exception:  # самопроверка вспомогательна: отчёт важнее, причина идёт в лог
        logger.exception("Самопроверка отчёта упала")
        return None, [
            Diagnostic(
                code=INT_STEP_DEGRADED,
                level=DiagLevel.WARNING,
                message="Самопроверка отчёта не выполнена: внутренняя ошибка",
                action="Отчёт записан без самопроверки",
                hint=f"Подробности в {state.cfg.log_path.name}",
                step="report",
            )
        ]


def _render(state: _State) -> tuple[ExitCode, Report | None, str]:
    """Код возврата, отчёт и Markdown. Не падает: при любом сбое сборки пишет аварийный Markdown."""
    finished = datetime.now(UTC)
    try:
        ctx = _context(state, finished, exit_code(state.diagnostics))
        report = build_report(ctx)
        # Самопроверка идёт по отчёту, каким его прочтут, и ничего в нём не правит. Её диагностика входит
        # в отчёт и в код возврата (INT-003 даёт 1), поэтому отчёт при провале собирается заново.
        check, extra = _selfcheck(state, report)
        if extra:
            ctx = _context(state, finished, exit_code([*state.diagnostics, *extra]), extra)
            report = build_report(ctx)
            check = (
                _selfcheck(state, report)[0] or check
            )  # итоговый отчёт проверяем ещё раз: диагностика вошла в него
        if check is not None:
            report = report.model_copy(update={"selfcheck": check})
        return ctx.exit_code, report, render_md(report)
    except Exception as exc:  # отчёт пишется всегда: при сбое шаблона аварийный Markdown
        logger.exception("Сборка отчёта упала")
        state.diagnostics.append(
            Diagnostic(
                code=INT_STEP_FAILED,
                level=DiagLevel.ERROR,
                message=f"Внутренняя ошибка при сборке отчёта: {type(exc).__name__}",
                action="Записан аварийный отчёт",
                hint=f"Подробности в {state.cfg.log_path.name}",
                step="report",
            )
        )
        code = exit_code(state.diagnostics)
        return code, None, _emergency_md(state, code)


def _remove_stale_pdf(cfg: RunConfig) -> None:
    """Прежний PDF с тем же именем рядом со свежим Markdown выдавал бы себя за отчёт этого прогона."""
    with contextlib.suppress(OSError):
        cfg.report_pdf_path.unlink(missing_ok=True)


def _write_pdf(state: _State, report: Report) -> bool:
    """PDF рядом с Markdown. Любой сбой (Typst, модель вида, запись) не роняет прогон: диагностика INT-002.

    Возвращает True, если файл записан; иначе Markdown остаётся единственным отчётом.
    """
    cfg = state.cfg
    try:
        cfg.report_pdf_path.write_bytes(render_pdf(report))
    except Exception as exc:  # PDF необязателен для отчёта: причина в лог, прогон идёт дальше
        logger.exception("PDF-версия отчёта не собрана")
        _remove_stale_pdf(cfg)
        reason = exc.message if isinstance(exc, PdfRenderError) else type(exc).__name__
        state.diagnostics.append(
            Diagnostic(
                code=INT_STEP_DEGRADED,
                level=DiagLevel.WARNING,
                message=f"PDF-версия отчёта не собрана: {reason}",
                action=f"Отчёт записан в Markdown: {cfg.report_md_path.name}",
                hint=f"Подробности в {cfg.log_path.name}; откройте отчёт в Markdown",
                step="report",
            )
        )
        return False
    state.pdf_written = True
    return True


def _write(state: _State, report: Report | None, markdown: str) -> dict[str, Path]:
    cfg = state.cfg
    outputs = {"report_md": cfg.report_md_path}
    if state.pdf_written:
        outputs["report_pdf"] = cfg.report_pdf_path
    cfg.report_md_path.write_text(markdown, encoding="utf-8")
    if report is not None:
        cfg.report_json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        outputs["report_json"] = cfg.report_json_path
    doc = state.doc
    write_questions_yaml(
        cfg.questions_path,
        state.questions,
        doc.name if doc else cfg.input_path.name,
        doc.sha256 if doc else None,
    )
    outputs["questions"] = cfg.questions_path
    calls_path = cfg.run_dir / "calls.jsonl"
    write_calls_jsonl(calls_path, state.client.records if state.client else [])
    outputs["calls"] = calls_path
    outputs["log"] = cfg.log_path
    return outputs


def _message(code: ExitCode, report: Report | None, path: Path | None, markdown: Path | None = None) -> str:
    head = f"Код {code.value}: {code.meaning}."
    if path is not None:
        beside = f" (Markdown рядом: {markdown.name})" if markdown is not None else ""
        head = f"Отчёт: {path}{beside}. {head}"
    if report is None:
        return head
    c = report.conclusion
    if report.verification is None:
        return f"{head} {c.traffic_light.icon} {c.headline}"
    s = report.verification.summary
    return (
        f"{head} Требований: {s.requirements_total}, нарушений: {s.violations}, "
        f"критичных: {s.critical}, вопросов автору: {len(report.questions)}. "
        f"{c.traffic_light.icon} {c.headline}"
    )


def _same_file(a: Path, b: Path) -> bool:
    if a.exists() and b.exists():
        with contextlib.suppress(OSError):
            return a.samefile(b)
    return a.resolve() == b.resolve()


def _overwrites_input(cfg: RunConfig) -> Path | None:
    """Файл выхода, который совпал бы со входом (``validator x.md x.md``, ``x.md x.pdf``), или None."""
    return next((p for p in cfg.output_files if _same_file(p, cfg.input_path)), None)


def _attach_log(path: Path) -> logging.Handler:
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def run(cfg: RunConfig) -> RunOutcome:
    """Один прогон агента. Исключений наружу не выпускает."""
    state = _State(cfg=cfg, started=datetime.now(UTC))
    clash = _overwrites_input(cfg)
    if clash is not None:
        diag = Diagnostic(
            code=OUT_OVERWRITES_INPUT,
            level=DiagLevel.ERROR,
            message=f"Выход {clash} совпадает с проверяемым документом и затёр бы его",
            action="Проверка не выполнялась, ничего не записано",
            hint="Укажите отчёт с другим именем, например out/report.md",
        )
        return RunOutcome(exit_code=ExitCode.USAGE, message=f"Код 2: {diag.message}", diagnostics=[diag])
    try:
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        cfg.run_dir.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler | None = _attach_log(cfg.log_path)
    except OSError as exc:
        diag = Diagnostic(
            code=OUT_WRITE_FAILED,
            level=DiagLevel.ERROR,
            message=f"Папка отчёта недоступна: {cfg.out_dir} ({exc.strerror})",
            action="Проверка не выполнялась",
            hint="Укажите путь отчёта в папке, куда есть право записи",
        )
        return RunOutcome(exit_code=ExitCode.USAGE, message=f"Код 2: {diag.message}", diagnostics=[diag])
    try:
        logger.info("Прогон: %s → %s, бэкенд %s", cfg.input_path, cfg.output_path, cfg.backend.value)
        try:
            _execute(state)
        except Exception as exc:  # верхняя граница: никаких трассировок пользователю
            logger.exception("Непойманная ошибка конвейера")
            state.diagnostics.append(
                Diagnostic(
                    code=INT_STEP_FAILED,
                    level=DiagLevel.ERROR,
                    message=f"Внутренняя ошибка агента: {type(exc).__name__}",
                    action="Отчёт содержит то, что успели сделать",
                    hint=f"Подробности в {cfg.log_path.name}; сообщите команде",
                )
            )
        if state.client is not None:
            state.diagnostics.extend(state.client.diagnostics)
        code, report, markdown = _render(state)
        if cfg.wants_pdf and report is None:
            _remove_stale_pdf(cfg)  # отчёт аварийный (INT-001 уже в нём): PDF строить не из чего
        elif cfg.wants_pdf and report is not None and not _write_pdf(state, report):
            code, report, markdown = _render(state)  # в отчёте появилась диагностика о PDF
        try:
            outputs = _write(state, report, markdown)
        except OSError as exc:
            logger.exception("Запись отчёта не удалась")
            return RunOutcome(
                exit_code=ExitCode.USAGE,
                message=f"Код 2: не удалось записать отчёт ({exc.strerror})",
                diagnostics=list(state.diagnostics),
            )
        except Exception as exc:  # сериализация отчёта: трассировку только в лог
            logger.exception("Запись отчёта не удалась")
            return RunOutcome(
                exit_code=ExitCode.INTERNAL,
                message=(
                    f"Код 1: внутренняя ошибка при записи отчёта ({type(exc).__name__}), см. {cfg.log_path}"
                ),
                diagnostics=list(state.diagnostics),
            )
        logger.info("Готово: код %s", code.value)
        shown = cfg.report_pdf_path if state.pdf_written else cfg.report_md_path
        return RunOutcome(
            exit_code=code,
            message=_message(code, report, shown, cfg.report_md_path if state.pdf_written else None),
            report_path=shown,
            outputs=outputs,
            report=report,
            diagnostics=list(state.diagnostics),
        )
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
