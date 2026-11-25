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
| 4 поиск | упал проход: «K−1 из K»; упали все: отчёт по линтеру, код 5 |
| 5 слияние, 7 верификатор | внутренняя ошибка, код 1, отчёт из того, что есть |
| 6 доказательства | находки без свидетеля, предупреждение |
| 8 вопросы | раздел без вопросов, предупреждение |
| 9 отчёт | Markdown пишется всегда, в крайнем случае аварийный |
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from validator.errors import (
    CFG_NOT_IMPLEMENTED,
    IN_NO_TEXT,
    INT_STEP_DEGRADED,
    INT_STEP_FAILED,
    LLM_ALL_PASSES_FAILED,
    OUT_FORMAT_FALLBACK,
    OUT_WRITE_FAILED,
    InputError,
    ValidatorError,
    exit_code,
)
from validator.evidence import evidence
from validator.ingest import guard, parse
from validator.lint import lint
from validator.llm import RecordingClient, make_client, write_calls_jsonl
from validator.merge import merge
from validator.models import (
    Backend,
    CandidateFinding,
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
    Verification,
)
from validator.passport import build_passport
from validator.questions import ask, write_questions_yaml
from validator.report import build_report, render_md
from validator.search import search
from validator.segment import fallback_segmentation, segment
from validator.verify import verify

logger = logging.getLogger("validator")
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
    merged: list[MergedFinding] = field(default_factory=list)
    verification: Verification | None = None
    questions: list[Question] = field(default_factory=list)
    client: RecordingClient | None = None
    backend_used: Backend | None = None


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
    state.guard = guard(cfg.input_path)
    state.diagnostics.extend(state.guard.diagnostics)
    if state.guard.fatal:
        return None
    try:
        doc = parse(cfg.input_path, state.guard.kind)
    except InputError as exc:
        state.diagnostics.append(
            exc.to_diagnostic(action="Проверка не выполнялась: документ не прочитан", step="parse")
        )
        return None
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
    if cfg.answers_path is not None or cfg.ask:
        state.diagnostics.append(
            Diagnostic(
                code=CFG_NOT_IMPLEMENTED,
                level=DiagLevel.WARNING,
                message="Цикл уточнения (--answers, --ask) в этой версии агента ещё не работает",
                action="Ответы автора не учтены, проверка выполнена по документу",
                step="clarify",
            )
        )
    if cfg.output_path.suffix.lower() == ".pdf":
        state.diagnostics.append(
            Diagnostic(
                code=OUT_FORMAT_FALLBACK,
                level=DiagLevel.WARNING,
                message="PDF-версия отчёта в этой версии агента ещё не делается",
                action=f"Отчёт записан в Markdown: {cfg.report_md_path.name}",
                step="report",
            )
        )


def _execute(state: _State) -> None:
    cfg = state.cfg
    _check_requests(state)
    doc = _read_input(state)
    if doc is None:
        return
    state.doc = doc
    client, client_diags = make_client(cfg)
    state.diagnostics.extend(client_diags)
    llm = RecordingClient(client)
    state.client, state.backend_used = llm, client.backend

    seg = _step(
        state,
        "segment",
        lambda: segment(doc, llm, cfg),
        lambda: fallback_segmentation(doc),
        action="Нарезка сделана запасным путём без модели",
    )
    state.segmentation = seg
    state.diagnostics.extend(seg.diagnostics)
    state.lint_hits = _step(state, "lint", lambda: lint(doc, seg), list, action="Линтер пропущен")
    state.passes = _step(state, "search", lambda: search(doc, llm, cfg), list, action="Поиск пропущен")
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
    passes, lint_hits = state.passes, state.lint_hits
    state.merged = _step(
        state,
        "merge",
        lambda: merge(passes, lint_hits, doc),
        list,
        action="Находки не слиты",
        level=DiagLevel.ERROR,
    )
    merged = state.merged
    state.merged = _step(
        state,
        "evidence",
        lambda: evidence(merged, doc, llm, cfg),
        lambda: merged,
        action="Находки идут без примера-свидетеля",
    )
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
        state.questions = _step(
            state,
            "questions",
            lambda: ask(ver, doc, seg, llm, cfg),
            list,
            action="Раздел вопросов собран без них",
        )


# ---------------------------------------------------------------- выходы


def _context(state: _State, finished: datetime, code: ExitCode) -> RunContext:
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
        merged=state.merged,
        verification=state.verification,
        questions=state.questions,
        diagnostics=list(state.diagnostics),
        calls=list(calls),
        passport=passport,
    )


def _emergency_md(ctx: RunContext) -> str:
    """Отчёт без шаблона, если сборка отчёта сама упала: паспорт и диагностика."""
    p = ctx.passport
    lines = [
        f"# Отчёт о проверке требований: {p.document_name}",
        "",
        "Отчёт собран в аварийном режиме: основной шаблон отчёта не сработал.",
        "",
        "## Паспорт прогона",
        "",
        f"- Документ: {p.document_name}, SHA-256: {p.document_sha256 or '—'}",
        f"- Дата прогона: {p.started_at.isoformat()}",
        f"- Версия агента: {p.agent_version}",
        f"- Код завершения: {ctx.exit_code.value}: {ctx.exit_code.meaning}",
        "",
        "## Диагностика",
        "",
    ]
    lines += [
        f"- {d.code} {d.level.label}: {d.message}. {d.action} {d.hint}".rstrip() for d in ctx.diagnostics
    ]
    return "\n".join(lines) + "\n"


def _render(state: _State) -> tuple[RunContext, Report | None, str]:
    finished = datetime.now(UTC)
    ctx = _context(state, finished, exit_code(state.diagnostics))
    try:
        report = build_report(ctx)
        return ctx, report, render_md(report)
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
        ctx = _context(state, finished, exit_code(state.diagnostics))
        return ctx, None, _emergency_md(ctx)


def _write(state: _State, report: Report | None, markdown: str) -> dict[str, Path]:
    cfg = state.cfg
    outputs = {"report_md": cfg.report_md_path}
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


def _message(code: ExitCode, report: Report | None, path: Path | None) -> str:
    head = f"Код {code.value}: {code.meaning}."
    if path is not None:
        head = f"Отчёт: {path}. {head}"
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


def _attach_log(path: Path) -> logging.Handler:
    handler = logging.FileHandler(path, mode="w", encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    return handler


def run(cfg: RunConfig) -> RunOutcome:
    """Один прогон агента. Исключений наружу не выпускает."""
    state = _State(cfg=cfg, started=datetime.now(UTC))
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
        ctx, report, markdown = _render(state)
        try:
            outputs = _write(state, report, markdown)
        except OSError as exc:
            logger.exception("Запись отчёта не удалась")
            return RunOutcome(
                exit_code=ExitCode.USAGE,
                message=f"Код 2: не удалось записать отчёт ({exc.strerror})",
                diagnostics=list(state.diagnostics),
            )
        logger.info("Готово: код %s", ctx.exit_code.value)
        return RunOutcome(
            exit_code=ctx.exit_code,
            message=_message(ctx.exit_code, report, cfg.report_md_path),
            report_path=cfg.report_md_path,
            outputs=outputs,
            report=report,
            diagnostics=list(ctx.diagnostics),
        )
    finally:
        if handler is not None:
            logger.removeHandler(handler)
            handler.close()
