"""Эталон дефектов: ``evals/gold.yaml`` (машиночитаемый) и разбор ``docs/gold/defects.md``.

Судья получает ``defects.md`` дословно, как в исследовании 21: так цифры сравнимы с прошлыми
замерами. Метрики (номера, важность, критичные) берутся из ``gold.yaml``. Тест
``tests/test_eval_gold.py`` держит оба файла одинаковыми по числу, номерам и тексту ячеек.
"""

from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from functools import cached_property
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
GOLD_MD = ROOT / "docs" / "gold" / "defects.md"
GOLD_YAML = ROOT / "evals" / "gold.yaml"
GOLD_DOCUMENT = ROOT / "data" / "input" / "requirements.md"

GOLD_TOTAL = 45
"""Дефектов в эталоне; судья исследования 21 знает это число из своего промпта («их 45»)."""

_ROW = re.compile(r"^\|\s*(\d+)\s*\|(.*)\|\s*$", re.MULTILINE)
_DISPUTED = re.compile(r"\(спорно\)", re.IGNORECASE)
MAIN_LIST_LAST = 33
"""Номера 1–33 это основной список, 34–45 дополнения после независимой перепроверки."""


class GoldSeverity(StrEnum):
    CRITICAL = "critical"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def label(self) -> str:
        return {"critical": "Критично", "medium": "Средне", "low": "Низко"}[self.value]

    @classmethod
    def from_label(cls, label: str) -> GoldSeverity:
        key = label.strip("* ").lower()
        for item in cls:
            if item.label.lower() == key:
                return item
        raise ValueError(f"Неизвестная важность в эталоне: {label!r}")


class GoldDefect(BaseModel):
    """Строка эталона: ячейки таблицы ``defects.md`` дословно."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int = Field(ge=1)
    where: str = Field(description="Столбец «Где»")
    defect: str = Field(description="Столбец «Дефект»")
    criterion: str = Field(description="Столбец «Критерий»: свойство стандарта")
    severity: GoldSeverity
    disputed: bool = Field(default=False, description="Пометка «спорно»: агенту не обязательно находить")
    addendum: bool = Field(default=False, description="Из дополнений после независимой перепроверки")


class Gold(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    source: str
    defects: list[GoldDefect]

    @model_validator(mode="after")
    def _unique_ids(self) -> Gold:
        ids = [d.id for d in self.defects]
        if len(ids) != len(set(ids)):
            raise ValueError("Номера дефектов эталона повторяются")
        return self

    @cached_property
    def by_id(self) -> dict[int, GoldDefect]:
        return {d.id: d for d in self.defects}

    @property
    def ids(self) -> list[int]:
        return [d.id for d in self.defects]

    @property
    def critical_ids(self) -> list[int]:
        return [d.id for d in self.defects if d.severity is GoldSeverity.CRITICAL]

    def ids_with(self, severity: GoldSeverity) -> list[int]:
        return [d.id for d in self.defects if d.severity is severity]


def parse_gold_md(text: str) -> list[GoldDefect]:
    """Строки обеих таблиц ``defects.md``: «# | Где | Дефект | Критерий | Важность»."""
    out: list[GoldDefect] = []
    for m in _ROW.finditer(text):
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) != 4:
            raise ValueError(f"Строка {m.group(1)} эталона: ожидалось 4 ячейки, найдено {len(cells)}")
        where, defect, criterion, severity = cells
        number = int(m.group(1))
        out.append(
            GoldDefect(
                id=number,
                where=where,
                defect=defect,
                criterion=criterion,
                severity=GoldSeverity.from_label(severity),
                disputed=bool(_DISPUTED.search(defect)),
                addendum=number > MAIN_LIST_LAST,
            )
        )
    return out


def load_gold(path: Path = GOLD_YAML) -> Gold:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Gold.model_validate(data)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_gold_md(path: Path = GOLD_MD) -> str:
    """Текст эталона для судьи: файл целиком, как его читает исследование 21."""
    return path.read_text(encoding="utf-8")
