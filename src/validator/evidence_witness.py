"""Шаг 6а: пример-свидетель (docs/ideas/final-idea.md, шаг 6; исследование 03: «нет свидетеля —
нет противоречия»).

Модель (D-010: Opus 5.5 ``high``) получает документ с ID блоков и пачку находок: противоречия и находки, где
поиск сам написал сценарий (двусмысленности, невыполнимые сочетания). Для каждой она строит пример-свидетель:
конкретный сценарий и как его читает каждое место документа (или каждое толкование одного места): дословная
цитата и ожидаемый результат. Или честно отвечает, что такого сценария нет. Решает код, а не модель:

- каждая цитата есть в документе (``locate_quote``, строка таблицы по ячейкам); короткая цитата не в счёт;
- ожидания различаются после нормализации (регистр, «ё», знаки, порядок слов);
- у противоречия цитаты из разных мест документа (``severity.distinct_places`` ≥ 2);
- хотя бы одна цитата из мест самой находки: свидетель про эту находку, а не про соседнюю.

Прошло: ``Witness(checked=True)``. Не прошло: ``Witness(checked=False, note=почему)``, в прочтениях остаются
только сверенные цитаты (выдуманная цитата в отчёт не попадает). Модель не нашла сценария: свидетель с пустым
сценарием и её причиной в ``note``. Отказ модели, ответ без JSON: находки идут без свидетеля, ``LLM-040``.

Формат ответа: рассуждение текстом, затем один блок ```json со списком ``witnesses``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import Field, ValidationError, field_validator

from validator.errors import LLM_BAD_RESPONSE, LLM_EVIDENCE_FAILED, LLM_WITNESS_CHECKED, LLMError
from validator.evidence_common import (
    Lenient,
    Located,
    from_model,
    locate_model_quote,
    normalize_mid,
    one_line,
    render_finding,
    top_blocks,
)
from validator.ingest.blocks import render_for_model
from validator.llm.base import LLMClient
from validator.models import (
    DefectType,
    DiagLevel,
    Diagnostic,
    LLMRequest,
    LLMResponse,
    LLMStep,
    MergedFinding,
    ParsedDocument,
    Quote,
    RunConfig,
    Witness,
    WitnessReading,
)
from validator.prompting import PROMPTS_DIR, fill, load_prompt
from validator.questions.editor import extract_json
from validator.severity import distinct_places

logger = logging.getLogger("validator")

SYSTEM_PROMPT = PROMPTS_DIR / "witness_system.md"
USER_PROMPT = PROMPTS_DIR / "witness_user.md"
CALL_INDEX: Final = 1
"""Свидетель вызывается один раз за прогон, пакетно на все выбранные находки."""
STEP: Final = "evidence"
REPLY_KEY: Final = "witnesses"
MAX_FINDINGS: Final = 30
"""Сколько находок в одном вызове: больше модель разбирает поверхностно (противоречий за прогон до ~10)."""
MIN_READINGS: Final = 2
MIN_SCENARIO_CHARS: Final = 20
"""Сценарий короче — это не конкретная ситуация со значениями, а слово «например»."""

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


# ---------------------------------------------------------------- схема ответа


class ReplyReading(Lenient):
    quote: str = ""
    reading: str = ""
    expectation: str = ""

    @field_validator("quote", "reading", "expectation", mode="before")
    @classmethod
    def _null_is_empty(cls, raw: object) -> object:
        return "" if raw is None else raw


class ReplyWitness(Lenient):
    """Свидетель одной находки. ``scenario: null`` значит «сценария, где места расходятся, нет»."""

    id: str
    scenario: str | None = None
    readings: list[ReplyReading] = Field(default_factory=list)
    reason: str = ""

    @field_validator("reason", mode="before")
    @classmethod
    def _null_reason(cls, raw: object) -> object:
        return "" if raw is None else raw

    @field_validator("readings", mode="before")
    @classmethod
    def _readings_list(cls, raw: object) -> object:
        return raw if isinstance(raw, list) else []


class WitnessReplySchema(Lenient):
    """Схема ответа целиком: образец в ``witness_system.md`` обязан ей соответствовать."""

    witnesses: list[ReplyWitness]


# ---------------------------------------------------------------- выбор находок и промпт


def wanted(m: MergedFinding, quotes: Sequence[Quote]) -> bool:
    """Свидетель нужен противоречию и находке модели, у которой поиск сам написал сценарий."""
    if not quotes or not from_model(m):
        return False
    return m.defect_type is DefectType.CONTRADICTION or bool(m.scenario and m.scenario.strip())


def select(found: Sequence[MergedFinding], located: Located) -> list[MergedFinding]:
    """Находки для свидетеля: противоречия первыми, затем по порядку слияния; не больше ``MAX_FINDINGS``."""
    picked = [m for m in found if wanted(m, located.get(m.mid, ()))]
    picked.sort(key=lambda m: m.defect_type is not DefectType.CONTRADICTION)
    return picked[:MAX_FINDINGS]


def build_request(
    batch: Sequence[MergedFinding], located: Located, doc: ParsedDocument, cfg: RunConfig
) -> LLMRequest:
    model = cfg.model_for(LLMStep.EVIDENCE)
    findings = "\n\n".join(render_finding(m, located.get(m.mid, ())) for m in batch)
    prompt = fill(
        load_prompt(USER_PROMPT.name), document=render_for_model(doc).rstrip("\n"), findings=findings
    )
    return LLMRequest(
        step=LLMStep.EVIDENCE,
        call_index=CALL_INDEX,
        model=model.model,
        effort=model.effort,
        system=load_prompt(SYSTEM_PROMPT.name).strip(),
        prompt=prompt,
        json_schema=None,
        timeout_s=cfg.call_timeout_s,
    )


def parse_reply(resp: LLMResponse) -> list[ReplyWitness]:
    """Свидетели из ответа. Нет JSON со списком ``witnesses`` → ``LLMError``."""
    data = extract_json(resp.text, REPLY_KEY)
    if data is None:
        raise LLMError(
            "В ответе шага примеров-свидетелей нет JSON со списком witnesses",
            code=LLM_BAD_RESPONSE,
            hint="Повторите прогон; если повторяется, сообщите команде",
            location=resp.call_id,
        )
    out: list[ReplyWitness] = []
    for raw in data[REPLY_KEY]:
        try:
            out.append(ReplyWitness.model_validate(raw))
        except ValidationError:
            continue
    return out


# ---------------------------------------------------------------- проверка кодом


def expectation_key(text: str) -> str:
    """Ожидание для сравнения: без регистра, «ё» и знаков. Порядок слов важен: «не» относится к соседнему
    слову, и «штраф начислен, письмо не отправлено» не то же, что «штраф не начислен, письмо отправлено»."""
    return " ".join(_PUNCT.sub(" ", text.lower().replace("ё", "е")).split())


@dataclass
class WitnessLog:
    """Что код сделал со свидетелями: из этого собирается строка диагностики."""

    checked: int = 0
    declined: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    missing: int = 0

    def reject(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


REJECT_QUOTE = "цитата не найдена в документе"
REJECT_SAME = "ожидания совпадают или не указаны"
REJECT_ONE_PLACE = "у противоречия меньше двух разных мест документа"
REJECT_OFF_TOPIC = "ни одна цитата не из мест самой находки"
REJECT_FEW = "меньше двух прочтений"
REJECT_SCENARIO = "нет конкретного сценария"


def _rejection(
    m: MergedFinding, w: ReplyWitness, readings: Sequence[WitnessReading], own: Sequence[Quote]
) -> str:
    """Первая причина, по которой свидетель не проходит; пустая строка, если проходит."""
    if len(one_line(w.scenario)) < MIN_SCENARIO_CHARS:
        return REJECT_SCENARIO
    if len(w.readings) < MIN_READINGS:
        return REJECT_FEW
    if len(readings) < len(w.readings):
        return REJECT_QUOTE
    keys = {expectation_key(r.expectation) for r in readings}
    if "" in keys or len(keys) < len(readings):
        return REJECT_SAME
    quotes = [r.located for r in readings if r.located is not None]
    if m.defect_type is DefectType.CONTRADICTION and distinct_places(quotes) < MIN_READINGS:
        return REJECT_ONE_PLACE
    if own and not set(top_blocks(quotes)) & set(top_blocks(own)):
        return REJECT_OFF_TOPIC
    return ""


def judge(
    m: MergedFinding, w: ReplyWitness, own: Sequence[Quote], doc: ParsedDocument, log: WitnessLog
) -> Witness:
    """Свидетель после проверки кодом. Несверенные цитаты в прочтения не попадают."""
    if w.scenario is None or not w.scenario.strip():
        log.declined += 1
        reason = one_line(w.reason).rstrip(".") or "модель не нашла сценария, в котором места расходятся"
        return Witness(scenario="", checked=False, note=f"Свидетель не построен: {reason}.")
    within = top_blocks(own)
    readings: list[WitnessReading] = []
    for r in w.readings:
        found = locate_model_quote(doc, r.quote, within)
        if found is None:
            continue
        readings.append(
            WitnessReading(
                quote=found.located or found.text,
                expectation=one_line(r.expectation),
                reading=one_line(r.reading),
                located=found,
            )
        )
    why = _rejection(m, w, readings, own)
    scenario = one_line(w.scenario)
    if why:
        log.reject(why)
        return Witness(
            scenario=scenario, readings=readings, checked=False, note=f"Код отклонил свидетеля: {why}."
        )
    log.checked += 1
    return Witness(scenario=scenario, readings=readings, checked=True)


# ---------------------------------------------------------------- шаг


@dataclass(frozen=True)
class WitnessOutcome:
    findings: list[MergedFinding] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    requested: int = 0


def _note(log: WitnessLog, requested: int) -> list[Diagnostic]:
    rejected = sum(log.reasons.values())
    parts = [f"{why}: {n}" for why, n in log.reasons.items()]
    if log.missing:
        parts.append(f"без ответа модели: {log.missing}")
    return [
        Diagnostic(
            code=LLM_WITNESS_CHECKED,
            level=DiagLevel.INFO,
            message=(
                f"Примеры-свидетели: запрошено {requested}, проверено кодом {log.checked}, "
                f"отклонено {rejected}, модель не нашла сценария {log.declined}"
            ),
            action="; ".join(["Отклонённые свидетели в находках помечены, находки остались", *parts]),
            step=STEP,
        )
    ]


def apply(
    found: Sequence[MergedFinding],
    replies: Sequence[ReplyWitness],
    located: Located,
    doc: ParsedDocument,
    batch: Sequence[MergedFinding],
) -> WitnessOutcome:
    """Свидетели в находках пачки; остальные находки как были."""
    by_id: dict[str, ReplyWitness] = {}
    for r in replies:
        mid = normalize_mid(r.id)
        if mid is not None:
            by_id.setdefault(mid, r)
    log = WitnessLog()
    asked = {m.mid for m in batch}
    out: list[MergedFinding] = []
    for m in found:
        reply = by_id.get(m.mid) if m.mid in asked else None
        if m.mid in asked and reply is None:
            log.missing += 1
        if reply is None:
            out.append(m)
            continue
        witness = judge(m, reply, located.get(m.mid, ()), doc, log)
        out.append(m.model_copy(update={"witness": witness}))
    logger.info(
        "Свидетели: запрошено %d, проверено %d, отклонено %d, не построено %d, без ответа %d",
        len(batch),
        log.checked,
        sum(log.reasons.values()),
        log.declined,
        log.missing,
    )
    return WitnessOutcome(findings=out, diagnostics=_note(log, len(batch)), requested=len(batch))


def failed(found: Sequence[MergedFinding], reason: str, hint: str, requested: int) -> WitnessOutcome:
    logger.warning("Свидетель не сработал (%s): находки без свидетеля", reason)
    return WitnessOutcome(
        findings=list(found),
        requested=requested,
        diagnostics=[
            Diagnostic(
                code=LLM_EVIDENCE_FAILED,
                level=DiagLevel.WARNING,
                message=f"Примеры-свидетели не построены: {reason}",
                action="Находки вошли в отчёт без примера-свидетеля",
                hint=hint or "Повторите прогон; если повторяется, сообщите команде",
                step=STEP,
            )
        ],
    )


def witness(
    found: Sequence[MergedFinding], located: Located, doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> WitnessOutcome:
    """Один пакетный вызов на находки, которым нужен свидетель. Таких нет: модель не вызывается."""
    batch = select(found, located)
    if not batch:
        return WitnessOutcome(findings=list(found))
    try:
        replies = parse_reply(llm.call(build_request(batch, located, doc, cfg)))
    except LLMError as exc:
        return failed(found, exc.message, exc.hint, len(batch))
    known = {normalize_mid(r.id) for r in replies} & {m.mid for m in batch}
    if not known:
        return failed(found, "в ответе нет ни одного свидетеля для находок прогона", "", len(batch))
    return apply(found, replies, located, doc, batch)


__all__ = [
    "MAX_FINDINGS",
    "WitnessOutcome",
    "WitnessReplySchema",
    "apply",
    "build_request",
    "expectation_key",
    "judge",
    "parse_reply",
    "select",
    "wanted",
    "witness",
]
