"""Ручные поправки к вердиктам судьи (``evals/overrides.yaml``): вердикт человека важнее судьи.

Два вида поправок:

- ``gold``: статус эталонного дефекта в конкретном прогоне (``run`` это метка прогона из результатов
  eval или шаблон ``fnmatch``, например ``evals/results/baseline/run-*``);
- ``findings``: «настоящий дефект вне эталона» или «шум» для находки по её отпечатку (столбец
  «Отпечаток» в результатах); без ``run`` поправка действует во всех прогонах.
"""

from __future__ import annotations

from collections.abc import Sequence
from fnmatch import fnmatch
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.findings import JudgeItem
from evals.judge import EvalError, ExtraVerdict, GoldMatch, GoldStatus, JudgeVerdict, read_utf8

DEFAULT_OVERRIDES = Path(__file__).resolve().parent / "overrides.yaml"


class GoldOverride(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run: str = Field(description="Метка прогона или шаблон fnmatch")
    gold: int = Field(ge=1, description="Номер дефекта эталона")
    status: GoldStatus
    finding_ids: list[int] = Field(default_factory=list, description="Номера находок в списке судьи")
    by: str = ""
    reason: str


class FindingOverride(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    finding: str = Field(min_length=6, description="Отпечаток находки (начало допускается)")
    verdict: ExtraVerdict
    run: str | None = Field(default=None, description="Метка прогона или шаблон; пусто значит везде")
    by: str = ""
    reason: str


class Overrides(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    gold: list[GoldOverride] = Field(default_factory=list)
    findings: list[FindingOverride] = Field(default_factory=list)


def load_overrides(path: Path | None = DEFAULT_OVERRIDES) -> Overrides:
    if path is None or not path.is_file():
        return Overrides()
    try:
        data = yaml.safe_load(read_utf8(path, "Файл поправок")) or {}
        return Overrides.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise EvalError(f"Файл поправок {path} не читается: {exc}") from exc


def apply_overrides(
    verdict: JudgeVerdict, run: str, items: Sequence[JudgeItem] | None, overrides: Overrides
) -> tuple[JudgeVerdict, list[str]]:
    """Вердикт с поправками и список применённых поправок (для результатов eval).

    ``items`` это список, который видел судья. ``None``: находки нумеровал сам судья (research21,
    свободный текст), тогда номера находок в поправках не проверяются по списку, а поправки находок
    по отпечатку не применяются (текстов находок нет).
    """
    gold = dict(verdict.gold)
    extra = dict(verdict.extra)
    applied: list[str] = []
    warnings = list(verdict.warnings)
    for g in overrides.gold:
        if not fnmatch(run, g.run):
            continue
        if g.gold not in gold:
            warnings.append(f"поправка: дефекта {g.gold} нет в эталоне, пропущена")
            continue
        bad = [i for i in g.finding_ids if i < 1 or (items is not None and i > len(items))]
        if bad:
            warnings.append(f"поправка дефекта {g.gold}: номера находок вне списка {bad}, убраны")
        before = gold[g.gold].status
        ids = [] if g.status is GoldStatus.MISSED else sorted(set(g.finding_ids) - set(bad))
        gold[g.gold] = GoldMatch(status=g.status, finding_ids=ids)
        applied.append(
            f"дефект {g.gold}: {before.value} → {g.status.value} ({g.by or 'человек'}: {g.reason})"
        )
    for f in overrides.findings:
        if f.run is not None and not fnmatch(run, f.run):
            continue
        if items is None:
            warnings.append(f"поправка находки {f.finding}: находки нумеровал судья, не применена")
            continue
        for item in items:
            if item.fingerprint().startswith(f.finding):
                before_extra = extra.get(item.id)
                extra[item.id] = f.verdict
                was = before_extra.value if before_extra else "без вердикта"
                applied.append(f"находка {item.id} ({item.fingerprint()}): {was} → {f.verdict.value}")
    updated = JudgeVerdict(gold=gold, extra=dict(sorted(extra.items())), warnings=warnings)
    return updated, applied
