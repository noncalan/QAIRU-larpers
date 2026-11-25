"""Пронумерованный список находок для судьи: один формат для агента и базовой линии.

Формат ровно тот, что в исследовании 21 для находок в JSON: массив объектов ``id``, ``quote``,
``property``, ``explanation`` (``json.dumps(..., ensure_ascii=False, indent=1)``). Кто автор
списка, судья не видит: в элементах нет ни источника, ни ID блоков, ни номеров ``F-NN``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from evals.freetext import QUOTE_JOIN, FreeFinding
from validator.models import Finding
from validator.text import normalize

FINGERPRINT_CHARS = 12


class JudgeItem(BaseModel):
    """Находка в списке судьи."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(ge=1)
    quote: str
    property: str
    explanation: str

    def fingerprint(self) -> str:
        """Отпечаток содержания без номера: по нему пишутся ручные поправки (overrides.yaml)."""
        material = "\x1f".join(normalize(part)[0] for part in (self.quote, self.property, self.explanation))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def agent_item(number: int, finding: Finding) -> JudgeItem:
    """Находка агента после верификатора: цитаты как их дала модель, свойство и тип дефекта."""
    return JudgeItem(
        id=number,
        quote=QUOTE_JOIN.join(q.text for q in finding.quotes),
        property=f"{finding.property.label}, {finding.defect_type.label}",
        explanation=finding.explanation,
    )


def free_item(number: int, finding: FreeFinding) -> JudgeItem:
    return JudgeItem(
        id=number,
        quote=finding.quote,
        property=finding.property,
        explanation=finding.explanation or finding.text,
    )


def agent_items(findings: Sequence[Finding]) -> list[JudgeItem]:
    return [agent_item(n, f) for n, f in enumerate(findings, start=1)]


def free_items(findings: Sequence[FreeFinding]) -> list[JudgeItem]:
    return [free_item(n, f) for n, f in enumerate(findings, start=1)]


def render_items(items: Sequence[JudgeItem]) -> str:
    """Блок «(3) НАХОДКИ» для судьи: как ``findings_block`` в ``run_matrix.judge`` исследования 21."""
    numbered = [
        {"id": i.id, "quote": i.quote, "property": i.property, "explanation": i.explanation} for i in items
    ]
    return json.dumps(numbered, ensure_ascii=False, indent=1)
