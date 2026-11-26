"""Шаг 8б: редактор вопросов, один вызов модели (исследование 17, п. 4.2; D-010: Opus 5.5 ``high``).

Модель получает весь документ с ID блоков и кандидатов, собранных кодом, и переписывает их в карточки
по п. 3.1 исследования 17: объединяет дубликаты, отбрасывает то, на что отвечает сам документ, пишет
адресата, вопрос со сценарием и варианты с последствиями. Приоритет и ``Q-NN`` модели не отдаём:
их считает код (``scoring.py``, ``cards.py``).

Формат ответа: сначала рассуждение текстом, затем один блок ```json. Строгий JSON по схеме
(``--json-schema``) не используем: в нём модель думает в 3–5 раз меньше (исследование 21). Схема
ответа это pydantic-модели ниже; разбор мягкий: карточка не по схеме выпадает одна, а не весь ответ.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from validator.config import call_timeout
from validator.errors import LLM_BAD_RESPONSE, LLMError
from validator.ingest.blocks import defuse_delimiters
from validator.llm.base import LLMClient
from validator.models import (
    Finding,
    LLMRequest,
    LLMResponse,
    LLMStep,
    ParsedDocument,
    RunConfig,
)
from validator.parts import focus_view
from validator.questions.candidates import Candidate, reasons
from validator.questions.scoring import draft_gaps, priority, value

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
SYSTEM_PROMPT = PROMPTS_DIR / "questions_system.md"
USER_PROMPT = PROMPTS_DIR / "questions_user.md"

CALL_INDEX = 1
"""Редактор вызывается один раз за прогон; второй вызов шага, критик-самоответ, живёт в ``critic.py``."""

_PLACEHOLDER = re.compile(r"\{(document|candidates)\}")
_FENCE = re.compile(r"```[ \t]*(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", re.DOTALL | re.IGNORECASE)
_CANDIDATE_ID = re.compile(r"C\s*-?\s*0*(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------- схема ответа


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class EditorOption(_Lenient):
    text: str = Field(description="Правило поведения одной фразой")
    change: str = Field(default="", description="Что поменять в документе или реализации")
    grounded_in: str | None = Field(default=None, description="Дословная цитата-основание или null")


class EditorCard(_Lenient):
    candidates: list[str] = Field(default_factory=list, description="ID кандидатов: C-01")
    addressee: list[str] = Field(default_factory=list, description="business, analyst, architect, security")
    topic: str = ""
    quotes: list[str] = Field(default_factory=list, description="1–3 дословные цитаты")
    question: str
    why: str = ""
    options: list[EditorOption] = Field(default_factory=list)
    if_unanswered: str = ""

    @field_validator("candidates", "addressee", "quotes", mode="before")
    @classmethod
    def _one_or_many(cls, raw: object) -> object:
        """Модель иногда пишет одно значение строкой, а не списком."""
        return [raw] if isinstance(raw, str) else raw


class EditorDrop(_Lenient):
    candidate: str
    reason: str = ""


class EditorReplySchema(_Lenient):
    """Схема ответа редактора целиком: образец в ``questions_system.md`` обязан ей соответствовать."""

    questions: list[EditorCard]
    dropped: list[EditorDrop] = Field(default_factory=list)


@dataclass(frozen=True)
class EditorReply:
    """Разобранный ответ: карточки по схеме, отброшенные редактором кандидаты, сколько записей выпало."""

    cards: list[EditorCard] = field(default_factory=list)
    dropped: list[EditorDrop] = field(default_factory=list)
    invalid: int = 0
    call_id: str = ""


# ---------------------------------------------------------------- промпт


def normalize_candidate_id(raw: str) -> str | None:
    """``C-01``, ``c1``, ``C 1`` → ``C-01``; не ID кандидата → None."""
    m = _CANDIDATE_ID.fullmatch(raw.strip())
    return f"C-{int(m.group(1)):02d}" if m else None


def _finding_line(f: Finding) -> list[str]:
    lines = [
        f"- {f.id} · {f.severity.label} · {f.verdict.label} · {f.defect_type.label} · "
        f"{f.property.label} ({f.property.info.english}): {' '.join(f.explanation.split())}"
    ]
    if f.suggestion:
        lines.append(f"  Предложенная формулировка: {' '.join(f.suggestion.split())}")
    if f.witness is not None and f.witness.checked:
        lines.append(f"  Пример-свидетель: {' '.join(f.witness.scenario.split())}")
        lines += [f"  - по «{r.quote}» ожидается: {r.expectation}" for r in f.witness.readings]
    gaps = draft_gaps(f)
    if gaps:
        lines.append(f"  Чего не хватает для тест-кейса: {'; '.join(gaps)}")
    return lines


def render_candidate(c: Candidate) -> str:
    """Кандидат для промпта: предварительный приоритет и ценность от кода, цитаты, находки."""
    p = priority(c.findings)
    reqs = ", ".join(c.requirement_ids) or "уровень набора, без отдельного требования"
    lines = [
        f"### {c.cid}",
        f"Предварительный приоритет: {p.value} ({p.label}), ценность ответа: {value(c.findings)}",
        f"Почему кандидат: {'; '.join(reasons(c))}",
        f"Требования: {reqs}",
        "Цитаты:",
    ]
    for q in c.quotes:
        block = q.anchor.block_ids[0] if q.anchor and q.anchor.block_ids else "?"
        lines.append(f"- [{block}] «{defuse_delimiters(' '.join((q.located or q.text).split()))}»")
    if c.placeholders:
        lines.append(f"Что неизвестно: {'; '.join(c.placeholders)}")
    lines.append("Находки:")
    for f in c.findings:
        lines += _finding_line(f)
    return "\n".join(lines)


def _fill(template: str, values: Mapping[str, str]) -> str:
    """Подстановка за один проход: текст документа с «{candidates}» внутри не подменится."""
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def build_request(
    candidates: Sequence[Candidate],
    doc: ParsedDocument,
    cfg: RunConfig,
) -> LLMRequest:
    model = cfg.model_for(LLMStep.QUESTIONS)
    rendered = "\n\n".join(render_candidate(c) for c in candidates)
    places = (
        b for c in candidates for f in c.findings for q in f.quotes if q.anchor for b in q.anchor.block_ids
    )
    prompt = _fill(
        USER_PROMPT.read_text(encoding="utf-8"),
        {"document": focus_view(doc, places).rstrip("\n"), "candidates": rendered},
    )
    system = SYSTEM_PROMPT.read_text(encoding="utf-8").strip()
    return LLMRequest(
        step=LLMStep.QUESTIONS,
        call_index=CALL_INDEX,
        model=model.model,
        effort=model.effort,
        system=system,
        prompt=prompt,
        json_schema=None,
        timeout_s=call_timeout(cfg, system, prompt),
    )


# ---------------------------------------------------------------- разбор ответа


def _has_list(data: object, key: str) -> bool:
    return isinstance(data, dict) and isinstance(data.get(key), list)


def extract_json(text: str, key: str = "questions") -> dict[str, Any] | None:
    """Итоговый JSON из ответа «рассуждение, затем ```json»: последний блок со списком ``key``.

    Если ограды нет, ищем последний разбираемый JSON-объект верхнего уровня с ``key`` в тексте.
    Тем же разбором пользуется критик-самоответ (``critic.py``, список ``reviews``).
    """
    for block in reversed(_FENCE.findall(text)):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if _has_list(data, key):
            return data  # type: ignore[no-any-return]
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    start = text.find("{")
    while start != -1:
        try:
            data, end = decoder.raw_decode(text, start)
        except ValueError:
            start = text.find("{", start + 1)
            continue
        if _has_list(data, key):
            found = data
        start = text.find("{", end)
    return found


def parse_reply(resp: LLMResponse) -> EditorReply:
    """Карточки из ответа редактора. Нет JSON со списком ``questions`` → ``LLMError``."""
    data = extract_json(resp.text)
    if data is None:
        raise LLMError(
            "В ответе редактора вопросов нет JSON со списком questions",
            code=LLM_BAD_RESPONSE,
            hint="Повторите прогон; если повторяется, сообщите команде",
            location=resp.call_id,
            retryable=True,
        )
    cards: list[EditorCard] = []
    invalid = 0
    for raw in data["questions"]:
        try:
            cards.append(EditorCard.model_validate(raw))
        except ValidationError:
            invalid += 1
    dropped: list[EditorDrop] = []
    raw_dropped = data.get("dropped")
    for raw in raw_dropped if isinstance(raw_dropped, list) else []:
        try:
            dropped.append(EditorDrop.model_validate(raw))
        except ValidationError:
            continue
    return EditorReply(cards=cards, dropped=dropped, invalid=invalid, call_id=resp.call_id)


def edit(
    candidates: Sequence[Candidate],
    doc: ParsedDocument,
    llm: LLMClient,
    cfg: RunConfig,
) -> EditorReply:
    """Один вызов редактора. Сбой модели или ответ без JSON → ``LLMError``: тогда запасной путь."""
    req = build_request(candidates, doc, cfg)
    return parse_reply(llm.call(req))
