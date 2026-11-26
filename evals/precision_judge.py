"""Для сравнения с precision (#49): шум по судье eval (#24) для тех же прогонов, только из кеша.

Судья видит эталон, разметчики нет. «Настоящая» у судьи: находка сопоставлена с эталоном (found или
partial) или вне эталона с вердиктом real. Вердикт берётся тем же запросом, что строит ``evals.eval``
по умолчанию (документ ``data/input/requirements.md``, формат ``uniform``), и с ручными поправками
``evals/overrides.yaml``, как в eval. Нет вердикта в кеше или он не читается: заметка, а не ошибка.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evals.findings import render_items
from evals.gold import GOLD_DOCUMENT, load_gold, read_gold_md
from evals.judge import (
    DEFAULT_CACHE_DIR,
    EvalError,
    ExtraVerdict,
    Judge,
    JudgeVerdict,
    VerdictCache,
    judge_prompt,
    parse_verdict,
    read_utf8,
)
from evals.overrides import DEFAULT_OVERRIDES, apply_overrides, load_overrides
from evals.precision_items import LabelItem, Origin
from evals.precision_labeler import ItemLabel, Label, Labeler
from evals.runs import RunData


class JudgeCompare(BaseModel):
    """Шум по судье #24 для прогона и совпадение «настоящий или нет» с каждым разметчиком."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run: str
    items: int
    real: int = Field(description="Сопоставлены с эталоном или real вне эталона")
    noise: int
    agree: dict[Labeler, float | None] = Field(description="Совпадение «настоящий или нет» с судьёй")
    cache_path: str | None = None

    @property
    def real_share(self) -> float | None:
        return self.real / self.items if self.items else None

    @property
    def noise_share(self) -> float | None:
        return self.noise / self.items if self.items else None


def _judged(verdict: JudgeVerdict, run_items: Sequence[LabelItem]) -> dict[str, bool]:
    """uid → судья счёл находку настоящей. Находки без вердикта судьи не попадают."""
    out: dict[str, bool] = {}
    for number, item in enumerate(run_items, start=1):
        extra = verdict.extra.get(number)
        if number in verdict.matched_findings or extra is ExtraVerdict.REAL:
            out[item.uid] = True
        elif extra is ExtraVerdict.NOISE:
            out[item.uid] = False
    return out


def judge_compare(
    runs: Sequence[RunData],
    items: Sequence[LabelItem],
    labels: Mapping[Labeler, Mapping[str, ItemLabel]],
    notes: list[str],
    cache_dir: Path = DEFAULT_CACHE_DIR,
    overrides_path: Path | None = DEFAULT_OVERRIDES,
) -> list[JudgeCompare]:
    gold = load_gold()
    prompt_parts = (read_utf8(GOLD_DOCUMENT, "Документ эталона"), read_gold_md())
    overrides = load_overrides(overrides_path)
    judge = Judge(None, VerdictCache(cache_dir))
    out = []
    for run in runs:
        prompt = judge_prompt(*prompt_parts, render_items(run.items))
        try:
            structured, call = judge.ask(prompt, 1, {"run": run.label})
            verdict = parse_verdict(structured, gold.ids, len(run.items))
        except EvalError as exc:
            notes.append(f"{run.label}: сравнения с судьёй нет: {exc}")
            continue
        verdict, _ = apply_overrides(verdict, run.label, run.items, overrides)
        run_items = [i for i in items if i.run == run.label and i.origin is not Origin.CONTROL]
        judged = _judged(verdict, run_items)
        agree: dict[Labeler, float | None] = {}
        for lab, marks in labels.items():
            pairs = [(marks[u].label is Label.REAL, real) for u, real in judged.items() if u in marks]
            agree[lab] = sum(x == y for x, y in pairs) / len(pairs) if pairs else None
        out.append(
            JudgeCompare(
                run=run.label,
                items=len(run_items),
                real=sum(judged.values()),
                noise=sum(not v for v in judged.values()),
                agree=agree,
                cache_path=call.cache_path,
            )
        )
    return out
