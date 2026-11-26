"""Вызов модели шага «Уточнения»: сверка находок с ответами автора (issue #42).

Модель (Opus 5.5 ``high``, как у вопросов автору, D-010) видит документ с ID блоков, находки прогона и
ответы автора, рассуждает текстом и отвечает одним блоком ```json: ``effects`` (какие находки ответ закрыл
или уточнил) и ``new_findings`` (дефекты, которые появились из-за ответа, с цитатой из документа).
Решает код (``apply.py``): модель только предлагает.

Ответы автора и документ подаются между метками как данные: промпт прямо запрещает выполнять текст,
обращённый к модели, а код не берёт из ответа модели ничего, кроме записей по схеме ниже.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import Field, ValidationError, field_validator

from validator.errors import LLM_BAD_RESPONSE, LLMError
from validator.ingest.blocks import render_for_model
from validator.models import (
    AuthorAnswer,
    DefectType,
    Finding,
    LLMRequest,
    LLMResponse,
    LLMStep,
    ParsedDocument,
    Property,
    RunConfig,
)
from validator.prompting import fill, load_prompt
from validator.questions.editor import _Lenient, extract_json

SYSTEM_PROMPT = "clarify_system.md"
USER_PROMPT = "clarify_user.md"
CALL_INDEX = 1
"""Сверка вызывается один раз за прогон, пакетно на все ответы и находки."""
REPLY_KEY = "effects"
EFFECTS = ("closed", "refined")

_FINDING_ID = re.compile(r"F\s*-?\s*0*(\d+)", re.IGNORECASE)


def normalize_finding_id(raw: object) -> str | None:
    """``F-03``, ``f3``, ``F 3`` → ``F-03``; не ID находки → None."""
    if not isinstance(raw, str):
        return None
    m = _FINDING_ID.fullmatch(raw.strip())
    return f"F-{int(m.group(1)):02d}" if m else None


def _as_list(raw: object) -> object:
    """Модель иногда пишет одно значение строкой, а не списком."""
    return [raw] if isinstance(raw, str) else raw


# ---------------------------------------------------------------- схема ответа


class EffectRecord(_Lenient):
    finding: str
    answers: list[str] = Field(default_factory=list)
    effect: str
    reason: str = ""
    fix: str = ""
    remaining: str = ""

    @field_validator("answers", mode="before")
    @classmethod
    def _one_or_many(cls, raw: object) -> object:
        return _as_list(raw)

    @field_validator("reason", "fix", "remaining", mode="before")
    @classmethod
    def _null_is_empty(cls, raw: object) -> object:
        return "" if raw is None else raw

    @field_validator("effect", mode="before")
    @classmethod
    def _known_effect(cls, raw: object) -> object:
        value = str(raw).strip().lower() if raw is not None else ""
        if value not in EFFECTS:
            raise ValueError(f"неизвестный effect: {raw!r}")
        return value


class NewFindingRecord(_Lenient):
    answers: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    property: Property
    defect_type: DefectType
    verdict: str = "violation"
    blocks_testing: bool = False
    title: str = ""
    explanation: str
    suggestion: str | None = None

    @field_validator("answers", "quotes", mode="before")
    @classmethod
    def _one_or_many(cls, raw: object) -> object:
        return _as_list(raw)

    @field_validator("blocks_testing", mode="before")
    @classmethod
    def _null_is_false(cls, raw: object) -> object:
        return False if raw is None else raw


class ClarifyReplySchema(_Lenient):
    """Схема ответа целиком: образец в ``clarify_system.md`` обязан ей соответствовать."""

    effects: list[EffectRecord]
    new_findings: list[NewFindingRecord] = Field(default_factory=list)


class ClarifyReply(_Lenient):
    """Разобранный ответ: записи по схеме и сколько записей выпало как не по схеме."""

    effects: list[EffectRecord] = Field(default_factory=list)
    new_findings: list[NewFindingRecord] = Field(default_factory=list)
    invalid: int = 0
    call_id: str = ""


# ---------------------------------------------------------------- промпт


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _properties() -> str:
    return "\n".join(f"- `{p.value}` — {p.label} ({p.info.english})" for p in Property)


def _defect_types() -> str:
    return "\n".join(f"- `{d.value}` — {d.label}" for d in DefectType)


def render_finding(f: Finding) -> str:
    """Находка для сверки: что нарушено, где и почему; свидетель и черновик теста не нужны."""
    lines = [
        f"### {f.id} · {f.severity.label} · {f.verdict.label} · {f.defect_type.label} · "
        f"{f.property.label} ({f.property.info.english})",
        f"Требования: {', '.join(f.requirement_ids) or 'уровень набора'}",
        "Цитаты:",
    ]
    for q in f.quotes:
        block = q.anchor.block_ids[0] if q.anchor and q.anchor.block_ids else "?"
        lines.append(f"- [{block}] «{_one_line(q.located or q.text)}»")
    if f.title:
        lines.append(f"Суть: {_one_line(f.title)}")
    lines.append(f"Почему дефект: {_one_line(f.explanation)}")
    if f.suggestion:
        lines.append(f"Предложенная формулировка: {_one_line(f.suggestion)}")
    return "\n".join(lines)


def render_answer(a: AuthorAnswer) -> str:
    """Ответ автора для сверки: вопрос, места документа, решение автора. Всё это данные."""
    asked = "вопрос задан и в этом прогоне" if a.asked_again else "вопрос из прошлого прогона"
    lines = [f"### {a.question_id} ({asked})"]
    if a.question:
        lines.append(f"Вопрос: {_one_line(a.question)}")
    if a.where:
        lines.append("Места документа, о которых вопрос:")
        lines += [f"- «{_one_line(w)}»" for w in a.where]
    if a.related_findings:
        lines.append(f"Находки, о которых был вопрос: {', '.join(a.related_findings)}")
    if a.option:
        answer = f"вариант {a.option}: {_one_line(a.text)}"
        if a.change:
            answer += f" (что поменять: {_one_line(a.change)})"
    else:
        answer = f"свой ответ: {_one_line(a.text)}"
    lines.append(f"Ответ автора: {answer}")
    if a.comment:
        lines.append(f"Пояснение автора: {_one_line(a.comment)}")
    return "\n".join(lines)


def build_request(
    findings: Sequence[Finding], answers: Sequence[AuthorAnswer], doc: ParsedDocument, cfg: RunConfig
) -> LLMRequest:
    model = cfg.model_for(LLMStep.CLARIFY)
    system = fill(load_prompt(SYSTEM_PROMPT), properties=_properties(), defect_types=_defect_types())
    prompt = fill(
        load_prompt(USER_PROMPT),
        document=render_for_model(doc).rstrip("\n"),
        findings="\n\n".join(render_finding(f) for f in findings) or "Находок нет.",
        answers="\n\n".join(render_answer(a) for a in answers),
    )
    return LLMRequest(
        step=LLMStep.CLARIFY,
        call_index=CALL_INDEX,
        model=model.model,
        effort=model.effort,
        system=system.strip(),
        prompt=prompt,
        json_schema=None,
        timeout_s=cfg.call_timeout_s,
    )


# ---------------------------------------------------------------- разбор ответа


def parse_reply(resp: LLMResponse) -> ClarifyReply:
    """Записи из ответа модели. Нет JSON со списком ``effects``: ``LLMError``; кривая запись выпадает одна."""
    data = extract_json(resp.text, REPLY_KEY)
    if data is None:
        raise LLMError(
            "В ответе сверки с ответами автора нет JSON со списком effects",
            code=LLM_BAD_RESPONSE,
            hint="Повторите прогон; если повторяется, сообщите команде",
            location=resp.call_id,
            retryable=True,
        )
    effects: list[EffectRecord] = []
    new: list[NewFindingRecord] = []
    invalid = 0
    for raw in data[REPLY_KEY]:
        try:
            effects.append(EffectRecord.model_validate(raw))
        except ValidationError:
            invalid += 1
    raw_new = data.get("new_findings")
    for raw in raw_new if isinstance(raw_new, list) else []:
        try:
            new.append(NewFindingRecord.model_validate(raw))
        except ValidationError:
            invalid += 1
    return ClarifyReply(effects=effects, new_findings=new, invalid=invalid, call_id=resp.call_id)


__all__ = [
    "ClarifyReply",
    "ClarifyReplySchema",
    "EffectRecord",
    "NewFindingRecord",
    "build_request",
    "normalize_finding_id",
    "parse_reply",
    "render_answer",
    "render_finding",
]
