"""Ответ модели на шаге нарезки: схема записей и разбор без доверия к содержимому.

Модель возвращает только ссылки на ID блоков и предложений и метаданные требования (тип, метка из
документа, краткая формулировка, сущности); цитаты в ответе нет. Отдельные записи не по схеме
отбрасываются и считаются, а ответ, из которого нельзя взять ни одного требования, это ``AnswerError``:
шаг повторит вызов или пойдёт запасным путём.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from validator.models import BlockLabel, RequirementType

ENTITY_KEYS = {
    "amount": "amounts",
    "period": "periods",
    "event": "events",
    "actor": "actors",
    "system": "systems",
    "endpoint": "endpoints",
    "status": "statuses",
    "data_field": "data_fields",
}
"""Вид сущности в ответе модели → ключ ``Requirement.entities`` (исследование 18, п. 3.3)."""

API_SUBTYPES = ("method", "url", "auth", "param", "body", "response", "status", "logic", "rule")
"""Подтип требования к API (исследование 18, п. 3.3)."""

EXCLUDE_LABELS = (
    BlockLabel.CONTEXT,
    BlockLabel.METADATA,
    BlockLabel.TEMPLATE,
    BlockLabel.EXAMPLE,
    BlockLabel.PLACEHOLDER,
    BlockLabel.HEADING,
)
"""Метки «не требование», которые модель может поставить блоку."""


def _none_to_empty(value: object) -> object:
    return "" if value is None else value


def _none_to_list(value: object) -> object:
    return [] if value is None else value


Text = Annotated[str, BeforeValidator(_none_to_empty)]
"""Строка ответа модели: ``null`` считаем пустой строкой."""


class ModelEntity(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Text = ""
    value: Text = ""


class ModelItem(BaseModel):
    """Требование в ответе модели: только ссылки на ID и метаданные, без цитаты."""

    model_config = ConfigDict(extra="ignore")

    refs: list[str]
    part_start: Text = ""
    part_end: Text = ""
    type: Text = RequirementType.OTHER.value
    subtype: Text = ""
    source_id: Text = ""
    summary: Text = ""
    entities: Annotated[list[ModelEntity], BeforeValidator(_none_to_list)] = Field(default_factory=list)
    links: Annotated[list[str], BeforeValidator(_none_to_list)] = Field(default_factory=list)
    depends_on: Text = ""


class ModelExclusion(BaseModel):
    """Блок или предложение, которые модель считает «не требованием», с меткой и причиной."""

    model_config = ConfigDict(extra="ignore")

    block: str
    label: Text = BlockLabel.CONTEXT.value
    reason: Text = ""


@dataclass(frozen=True)
class ModelAnswer:
    """Разобранный ответ модели: годные записи и сколько записей не прошли схему."""

    items: list[ModelItem]
    exclusions: list[ModelExclusion]
    invalid: int = 0


class AnswerError(ValueError):
    """Ответ модели нельзя использовать целиком: нужен повтор или запасной путь."""


def parse_answer(structured: object) -> ModelAnswer:
    """Разбор ответа по схеме. Отдельные кривые записи отбрасываются, кривой ответ целиком — ошибка."""
    if not isinstance(structured, dict):
        raise AnswerError("ответ не JSON-объект")
    raw_items = structured.get("requirements")
    raw_excluded = structured.get("excluded", [])
    if not isinstance(raw_items, list) or not isinstance(raw_excluded, list):
        raise AnswerError("в ответе нет списков requirements и excluded")
    items: list[ModelItem] = []
    exclusions: list[ModelExclusion] = []
    invalid = 0
    for raw in raw_items:
        try:
            items.append(ModelItem.model_validate(raw))
        except ValidationError:
            invalid += 1
    for raw in raw_excluded:
        try:
            exclusions.append(ModelExclusion.model_validate(raw))
        except ValidationError:
            invalid += 1
    if raw_items and not items:
        raise AnswerError("ни одна запись requirements не соответствует схеме")
    return ModelAnswer(items=items, exclusions=exclusions, invalid=invalid)
