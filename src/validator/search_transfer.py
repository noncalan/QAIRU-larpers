"""Шаг 4б: перенос в JSON моделью тех находок прохода, которые код не разобрал (issue #18).

Модель шага ``search_transfer`` (Haiku 5.5 ``medium``, D-010) получает документ с ID блоков и текст
находок, возвращает записи по ``TRANSFER_SCHEMA``; код проверяет каждую запись (``TransferItem``) и
превращает её в тот же черновик ``Draft``, что даёт разбор кодом. Схема живёт здесь, а не в
``models.py``: схемы ответов модели принадлежат модулю, который делает вызов (docs/plan.md).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from validator.errors import LLM_BAD_RESPONSE, LLMError
from validator.models import DefectType, LLMRequest, LLMStep, Property, RunConfig, Scope, Severity, Verdict
from validator.prompting import fill, load_prompt
from validator.search_parse import Draft, RawFinding, block_ids_in, parse_defect_type, parse_severity, unwrap

TRANSFER_SYSTEM = "search_transfer.md"
TRANSFER_USER = "search_transfer_user.md"
TRANSFER_ATTEMPTS = 2
"""Перенос в JSON: попытка и один повтор, затем отказ."""
TRANSFER_TIMEOUT_S = 600
"""Таймаут переноса: Haiku переносит проход из 47 находок за ~80 с (исследование 21), берём с запасом."""
SEVERITY_LABELS = ("критично", "средне", "низко")
"""Подсказки важности в промпте поиска (#14); ``Severity.from_label`` переводит их в ``Severity``."""
_UNMATCHED = 1_000_000
"""Позиция записи, которую не удалось сопоставить с номером «Н-k»: после разобранных кодом."""


def transfer_request(rendered: str, findings: str, cfg: RunConfig, index: int, attempt: int) -> LLMRequest:
    """Запрос переноса в JSON. Номер вызова: проход k это k, повтор это k + число проходов."""
    model = cfg.model_for(LLMStep.SEARCH_TRANSFER)
    return LLMRequest(
        step=LLMStep.SEARCH_TRANSFER,
        call_index=index + attempt * cfg.passes,
        model=model.model,
        effort=model.effort,
        system=load_prompt(TRANSFER_SYSTEM),
        prompt=fill(load_prompt(TRANSFER_USER), document=rendered, findings=findings),
        json_schema=TRANSFER_SCHEMA,
        timeout_s=min(cfg.call_timeout_s, TRANSFER_TIMEOUT_S),
    )


_TEXT: dict[str, Any] = {"type": "string"}
TRANSFER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "number": {"type": "integer"},
                    "title": _TEXT,
                    "block_ids": {"type": "array", "items": _TEXT},
                    "quotes": {"type": "array", "items": _TEXT},
                    "property": {"type": "string", "enum": [p.value for p in Property]},
                    "defect_type": {"type": "string", "enum": [d.label for d in DefectType]},
                    "verdict": {"type": "string", "enum": [v.label for v in Verdict]},
                    "scenario": _TEXT,
                    "explanation": _TEXT,
                    "blocks_testing": {"type": "boolean"},
                    "test_gap": _TEXT,
                    "severity": {"type": "string", "enum": list(SEVERITY_LABELS)},
                    "suggestion": _TEXT,
                },
                "required": ["quotes", "property", "defect_type", "verdict", "explanation", "blocks_testing"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}
"""Ответ модели переноса. Метки типа, вердикта и важности те же, что в ответе прохода (промпт #14)."""


class TransferItem(BaseModel):
    """Одна запись ответа переноса. Проверка схемы кодом: ответ модели не доверенный."""

    model_config = ConfigDict(extra="ignore")

    number: int | None = None
    title: str = ""
    block_ids: list[str] = Field(default_factory=list)
    quotes: list[str]
    property: Property
    defect_type: DefectType
    verdict: Verdict = Verdict.VIOLATION
    scenario: str = ""
    explanation: str = ""
    blocks_testing: bool = False
    test_gap: str = ""
    severity: Severity | None = None
    suggestion: str = ""

    @field_validator("quotes")
    @classmethod
    def _quotes(cls, value: list[str]) -> list[str]:
        quotes = [q.strip() for q in value if q.strip()]
        if not quotes:
            raise ValueError("нет цитаты")
        return quotes

    @field_validator("defect_type", mode="before")
    @classmethod
    def _defect_type(cls, value: object) -> object:
        return (parse_defect_type(value) or value) if isinstance(value, str) else value

    @field_validator("verdict", mode="before")
    @classmethod
    def _verdict(cls, value: object) -> object:
        return (Verdict.from_label(value) or value) if isinstance(value, str) else value

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return parse_severity(value) if value.strip() else None

    def to_draft(self, section: Scope | None) -> Draft:
        title = self.title.strip() or None
        return Draft(
            title=title,
            block_ids=tuple(b for value in self.block_ids for b in block_ids_in(value)),
            quotes=tuple(self.quotes),
            property=self.property,
            defect_type=self.defect_type,
            verdict=self.verdict,
            scope=section or self.property.scope,
            explanation=self.explanation.strip() or title or "",
            scenario=self.scenario.strip() or None,
            blocks_testing=self.blocks_testing,
            test_gap=self.test_gap.strip() or None,
            severity_hint=self.severity,
            suggestion=unwrap(self.suggestion) or None,
        )


def transferred(
    structured: object, chunks: Sequence[RawFinding], call_id: str
) -> tuple[list[tuple[float, Draft]], int]:
    """Черновики из ответа переноса с позицией в ответе прохода и число записей не по схеме.

    Позиция берётся по номеру «Н-k» фрагмента, который переносили; запись без такого номера идёт
    после разобранных кодом в порядке ответа модели. Ответ без списка ``findings`` или где ни одна
    запись не прошла схему, это ``LLMError``: перенос повторяют.
    """
    items = structured.get("findings") if isinstance(structured, dict) else None
    if not isinstance(items, list):
        raise LLMError(
            "В ответе переноса в JSON нет списка findings",
            code=LLM_BAD_RESPONSE,
            location=call_id,
            retryable=True,
        )
    by_number = {c.number: c for c in chunks if c.number is not None}
    drafts: list[tuple[float, Draft]] = []
    invalid = 0
    for j, raw in enumerate(items):
        try:
            item = TransferItem.model_validate(raw)
        except ValidationError:
            invalid += 1
            continue
        chunk = by_number.get(item.number) if item.number is not None else None
        position = chunk.position + j / len(items) if chunk else _UNMATCHED + j
        drafts.append((position, item.to_draft(chunk.section if chunk else None)))
    if items and not drafts:
        raise LLMError(
            f"Перенос в JSON: ни одна из {len(items)} записей не прошла схему",
            code=LLM_BAD_RESPONSE,
            location=call_id,
            retryable=True,
        )
    return drafts, invalid


__all__ = [
    "SEVERITY_LABELS",
    "TRANSFER_ATTEMPTS",
    "TRANSFER_SCHEMA",
    "TransferItem",
    "transfer_request",
    "transferred",
]
