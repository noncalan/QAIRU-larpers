"""Шаг 6, попытка тест-кейса (#40): какого значения не хватает, чтобы написать тест.

Для находок про проверяемость и однозначность и для всех, что мешают написать тест, модель (D-010:
Sonnet 5.5 ``high``) пытается написать тест-кейс: предусловие, шаги, ожидаемый результат. Где значения
в документе нет, она ставит пропуск ``[?N]`` и объясняет, чего не хватает. Это доказательство находки:
лид QA видит, где тестировщик застрянет, и что спросить у автора.

Модель предлагает, код проверяет (``evidence_testdraft_check.py``): значение «из документа» остаётся
только с цитатой, которую нашёл ``locate_quote``, иначе оно заменяется пропуском. Если все значения
подтверждены и пропусков нет, тест написать можно, и находка перестаёт «мешать тестированию»: важность
пересчитает верификатор по таблице. Связь пропусков с вопросами автору делает ``evidence_testdraft_links.py``
после шага вопросов.

Формат ответа: рассуждение текстом, затем один блок ```json (без ``--json-schema``: в строгом JSON модель
думает меньше, исследование 21). Находки идут пачками по ``BATCH_SIZE``; сбой пачки не роняет отчёт:
её находки остаются без черновика, в диагностике ``LLM-044``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from pydantic import ValidationError

from validator.config import call_timeout
from validator.errors import (
    LLM_BAD_RESPONSE,
    LLM_TEST_DRAFT_CHECKED,
    LLM_TEST_DRAFT_FAILED,
    LLMError,
)
from validator.evidence_testdraft_check import CheckLog, DraftEntry, build_draft
from validator.llm.base import LLMClient
from validator.models import (
    CriticAction,
    DefectType,
    DiagLevel,
    Diagnostic,
    LLMRequest,
    LLMResponse,
    LLMStep,
    MergedFinding,
    ParsedDocument,
    Property,
    RunConfig,
)
from validator.parts import focus_view
from validator.prompting import PROMPTS_DIR, fill, load_prompt
from validator.questions.editor import extract_json

logger = logging.getLogger("validator")

STEP = "test_draft"
SYSTEM_PROMPT = PROMPTS_DIR / "test_draft_system.md"
USER_PROMPT = PROMPTS_DIR / "test_draft_user.md"
REPLY_KEY = "drafts"
BATCH_SIZE = 13
"""Находок в одном вызове. Живой прогон на ``requirements.md`` 26.11.2026 (Астана), Sonnet 5.5 ``high``:
50 находок с признаком «мешает тесту» из 75, три пачки по 20 шли 162–261 с и стоили $0,27–0,44 каждая
(по тарифу API), около 10 секунд и 1,5 тысячи выходных токенов на черновик. Пачка поменьше идёт короче."""

MAX_PARALLEL_BATCHES = 4
"""Пачки идут параллельно, как проходы поиска (``search.MAX_PARALLEL_PASSES``)."""

CONTESTED_NOTE = (
    "Код: черновик выбрал одно прочтение, а в документе их два (противоречие или принятый свидетель), "
    "поэтому находка по-прежнему мешает тестированию."
)

TARGET_PROPERTIES = frozenset({Property.VERIFIABLE, Property.UNAMBIGUOUS})
"""Свойства, по которым попытку теста делаем всегда; остальные находки только если мешают тесту."""


@dataclass(frozen=True)
class TestDraftOutcome:
    """Итог шага: находки с черновиками и диагностика."""

    __test__ = False  # не путать pytest: это не тестовый класс

    findings: list[MergedFinding]
    diagnostics: list[Diagnostic] = field(default_factory=list)
    log: CheckLog = field(default_factory=CheckLog)


def needs_draft(m: MergedFinding) -> bool:
    """Нужна ли попытка теста: проверяемость, однозначность или «мешает тесту»; снятым критиком не нужна."""
    if m.critic is not None and m.critic.action is CriticAction.DROP:
        return False
    return m.property in TARGET_PROPERTIES or m.blocks_testing


def is_contested(m: MergedFinding) -> bool:
    """Противоречие или находка с принятым свидетелем: у документа два прочтения, черновик берёт одно."""
    return m.defect_type is DefectType.CONTRADICTION or (m.witness is not None and m.witness.checked)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_finding(m: MergedFinding) -> str:
    """Находка для промпта: что нашли, где, что проверяющие считают недостающим. Важность модели не нужна."""
    lines = [
        f"### {m.mid}",
        f"Свойство: {m.property.label} ({m.property.info.english}); тип дефекта: {m.defect_type.label}",
    ]
    if m.title:
        lines.append(f"Суть: {_one_line(m.title)}")
    lines.append(f"Почему это проблема: {_one_line(m.explanation)}")
    if m.block_ids:
        lines.append(f"Блоки: {', '.join(m.block_ids)}")
    if m.quotes:
        lines.append("Цитаты проверяющих:")
        lines += [f"- «{_one_line(q)}»" for q in m.quotes]
    if m.scenario:
        lines.append(f"Сценарий проверяющих: {_one_line(m.scenario)}")
    if m.test_gap:
        lines.append(f"Чего не хватает для теста, по мнению проверяющих: {_one_line(m.test_gap)}")
    return "\n".join(lines)


def build_request(
    batch: Sequence[MergedFinding], doc: ParsedDocument, cfg: RunConfig, call_index: int
) -> LLMRequest:
    model = cfg.model_for(LLMStep.TEST_DRAFT)
    # Длинный документ целиком не отдаём: только разделы с местами находок пачки и раздел терминов (#38).
    prompt = fill(
        load_prompt(USER_PROMPT.name),
        document=focus_view(doc, (b for m in batch for b in m.block_ids)).rstrip("\n"),
        findings="\n\n".join(render_finding(m) for m in batch),
    )
    system = load_prompt(SYSTEM_PROMPT.name).strip()
    return LLMRequest(
        step=LLMStep.TEST_DRAFT,
        call_index=call_index,
        model=model.model,
        effort=model.effort,
        system=system,
        prompt=prompt,
        json_schema=None,
        timeout_s=call_timeout(cfg, system, prompt),
    )


def parse_reply(resp: LLMResponse) -> tuple[list[DraftEntry], int]:
    """Черновики из ответа и число записей не по схеме. Нет JSON со списком ``drafts`` → ``LLMError``."""
    data = extract_json(resp.text, REPLY_KEY)
    if data is None:
        raise LLMError(
            "В ответе модели с попыткой теста нет JSON со списком drafts",
            code=LLM_BAD_RESPONSE,
            hint="Повторите прогон; если повторяется, сообщите команде",
            location=resp.call_id,
            retryable=True,
        )
    entries: list[DraftEntry] = []
    invalid = 0
    for raw in data[REPLY_KEY]:
        try:
            entries.append(DraftEntry.model_validate(raw))
        except ValidationError:
            invalid += 1
    return entries, invalid


def _batches(items: Sequence[MergedFinding]) -> list[list[MergedFinding]]:
    return [list(items[i : i + BATCH_SIZE]) for i in range(0, len(items), BATCH_SIZE)]


def _mid(raw: str) -> str:
    return raw.strip().upper().replace(" ", "")


def _apply(
    batch: Sequence[MergedFinding],
    entries: Sequence[DraftEntry],
    doc: ParsedDocument,
    log: CheckLog,
) -> dict[str, MergedFinding]:
    """Находки пачки с черновиками после проверок кода, по ``mid``; без записи модели находки нет."""
    by_mid = {m.mid.upper(): m for m in batch}
    out: dict[str, MergedFinding] = {}
    for entry in entries:
        m = by_mid.get(_mid(entry.finding))
        if m is None:
            log.no_finding += 1
            continue
        if m.mid in out:
            continue  # второй черновик той же находки: берём первый
        draft = build_draft(entry, m, doc, log)
        if draft is None:
            continue
        # Тест написать можно: код подтвердил все значения, находка больше не мешает тестированию.
        # Исключение: противоречие и находка с принятым свидетелем. Там два разных ожидания уже доказаны
        # кодом по цитатам, и черновик, выбравший одно из них, «мешает тесту» не снимает.
        update: dict[str, object] = {"test_draft": draft}
        if draft.complete and is_contested(m):
            update["test_draft"] = draft.model_copy(
                update={"contested": True, "note": f"{draft.note} {CONTESTED_NOTE}".strip()}
            )
        elif draft.complete and m.blocks_testing:
            update["blocks_testing"] = False
        out[m.mid] = m.model_copy(update=update)
    return out


def _failed(count: int, reason: str, hint: str) -> Diagnostic:
    return Diagnostic(
        code=LLM_TEST_DRAFT_FAILED,
        level=DiagLevel.WARNING,
        message=f"Попытка теста не сработала: {reason}",
        action=f"Находки без черновика теста: {count}",
        hint=hint or "Повторите прогон; если повторяется, сообщите команде",
        step=STEP,
    )


def _checked(log: CheckLog, drafted: int, total: int) -> list[Diagnostic]:
    details = log.details()
    if not details:
        return []
    return [
        Diagnostic(
            code=LLM_TEST_DRAFT_CHECKED,
            level=DiagLevel.INFO,
            message=f"Код проверил черновики теста: готово {drafted} из {total}",
            action="; ".join(details),
            step=STEP,
        )
    ]


@dataclass
class _BatchResult:
    drafted: dict[str, MergedFinding] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    log: CheckLog = field(default_factory=CheckLog)


def _one_batch(
    index: int, batch: Sequence[MergedFinding], doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> _BatchResult:
    """Одна пачка: вызов, разбор, проверки кода. Сбой пачки остаётся диагностикой и не выходит наружу."""
    result = _BatchResult()
    try:
        entries, invalid = parse_reply(llm.call(build_request(batch, doc, cfg, index)))
        result.log.unusable += invalid
        result.drafted = _apply(batch, entries, doc, result.log)
    except LLMError as exc:
        logger.warning("Попытка теста, пачка %d: %s (%s)", index, exc.message, exc.code)
        result.diagnostics.append(_failed(len(batch), exc.message, exc.hint))
    except (
        Exception
    ) as exc:  # граница пачки: шаг необязательный, другие пачки и их оплаченные ответы терять нельзя
        logger.exception("Попытка теста, пачка %d: внутренняя ошибка", index)
        reason = f"внутренняя ошибка {type(exc).__name__}"
        result.diagnostics.append(_failed(len(batch), reason, "Подробности в report.log; сообщите команде"))
        result = _BatchResult(diagnostics=result.diagnostics)
    return result


def draft_tests(
    found: Sequence[MergedFinding], doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> TestDraftOutcome:
    """Находки с черновиками теста. Нечего делать или сбой модели: находки возвращаются как были."""
    targets = [m for m in found if needs_draft(m)]
    if not targets:
        return TestDraftOutcome(findings=list(found))
    batches = _batches(targets)
    with ThreadPoolExecutor(
        max_workers=min(MAX_PARALLEL_BATCHES, len(batches)), thread_name_prefix="test-draft"
    ) as pool:
        results = list(pool.map(lambda ib: _one_batch(ib[0], ib[1], doc, llm, cfg), enumerate(batches, 1)))
    log = CheckLog()
    diagnostics: list[Diagnostic] = []
    drafted: dict[str, MergedFinding] = {}
    for r in results:
        log.add(r.log)
        diagnostics += r.diagnostics
        drafted.update(r.drafted)
    out = [drafted.get(m.mid, m) for m in found]
    logger.info("Попытка теста: находок %d, черновиков %d", len(targets), len(drafted))
    return TestDraftOutcome(
        findings=out, diagnostics=diagnostics + _checked(log, len(drafted), len(targets)), log=log
    )


__all__ = [
    "BATCH_SIZE",
    "CONTESTED_NOTE",
    "MAX_PARALLEL_BATCHES",
    "TestDraftOutcome",
    "draft_tests",
    "is_contested",
    "needs_draft",
]
