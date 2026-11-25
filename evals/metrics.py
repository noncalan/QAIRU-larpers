"""Метрики eval по вердикту судьи: на прогон и на конфигурацию (несколько прогонов одной настройки).

Определения как в исследовании 21 (``experiments/21-model-effort/score.py``):

- recall: доля 45 дефектов эталона со статусом found или partial;
- строгий recall: только found;
- критичные: то же по 9 дефектам с важностью «Критично»;
- шум: находки, которые судья счёл noise, от всех находок прогона;
- объединение: дефекты, найденные хотя бы в одном прогоне конфигурации (что дадут K проходов).

Сверх исследования 21 (docs/plan.md, research 08): pass^k критичных (найден во всех прогонах),
средний попарный Жаккар множеств найденных дефектов эталона и множеств находок агента
(``stable_key`` требования + свойство).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence, Set
from itertools import combinations
from statistics import fmean
from typing import TypeVar

from pydantic import BaseModel, ConfigDict, Field

from evals.findings import JudgeItem
from evals.gold import Gold, GoldSeverity
from evals.judge import ExtraVerdict, GoldStatus, JudgeCall, JudgeVerdict
from evals.runs import CallStats, QuoteStats, RunData, Source

QUOTE_PREVIEW_CHARS = 80
T = TypeVar("T")


class UnmatchedFinding(BaseModel):
    """Находка, которую судья не сопоставил с эталоном: по отпечатку пишется ручная поправка."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: int
    fingerprint: str
    verdict: ExtraVerdict | None
    quote: str


class RunScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    source: Source
    findings: int
    gold: dict[int, GoldStatus]
    recall: float
    recall_strict: float
    critical_recall: float
    critical_recall_strict: float
    recall_by_severity: dict[GoldSeverity, float]
    extra_real: int
    extra_noise: int
    unmatched_unjudged: int = Field(description="Несопоставленные находки без вердикта real/noise")
    noise_share: float | None
    quotes: QuoteStats
    quotes_final: QuoteStats | None
    calls: CallStats
    judge: JudgeCall
    keys: list[str] | None = None
    unmatched: list[UnmatchedFinding] = Field(default_factory=list)
    overrides: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    agent_version: str | None = None
    layout: str | None = None
    document: str | None = None

    def ids_with(self, *statuses: GoldStatus) -> list[int]:
        return [g for g, s in self.gold.items() if s in statuses]


def _share(hits: int, total: int) -> float:
    return hits / total if total else 0.0


def _recall(gold: dict[int, GoldStatus], ids: Sequence[int], *, strict: bool) -> float:
    ok = {GoldStatus.FOUND} if strict else {GoldStatus.FOUND, GoldStatus.PARTIAL}
    return _share(sum(gold.get(i) in ok for i in ids), len(ids))


def count_findings(verdict: JudgeVerdict, items: Sequence[JudgeItem]) -> int:
    """Число находок: длина списка; если список нумеровал судья (research21), наибольший номер."""
    if items:
        return len(items)
    ids = [i for m in verdict.gold.values() for i in m.finding_ids] + list(verdict.extra)
    return max(ids, default=0)


def score_run(
    run: RunData,
    verdict: JudgeVerdict,
    gold: Gold,
    judge: JudgeCall,
    *,
    findings: int,
    applied: Sequence[str] = (),
) -> RunScore:
    statuses = {g: (verdict.gold[g].status if g in verdict.gold else GoldStatus.MISSED) for g in gold.ids}
    matched = verdict.matched_findings
    by_id = {i.id: i for i in run.items}
    unmatched = [
        UnmatchedFinding(
            id=i,
            fingerprint=by_id[i].fingerprint(),
            verdict=verdict.extra.get(i),
            quote=by_id[i].quote[:QUOTE_PREVIEW_CHARS],
        )
        for i in sorted(by_id)
        if i not in matched
    ]
    extras = list(verdict.extra.values())
    noise = extras.count(ExtraVerdict.NOISE)
    return RunScore(
        label=run.label,
        source=run.source,
        findings=findings,
        gold=statuses,
        recall=_recall(statuses, gold.ids, strict=False),
        recall_strict=_recall(statuses, gold.ids, strict=True),
        critical_recall=_recall(statuses, gold.critical_ids, strict=False),
        critical_recall_strict=_recall(statuses, gold.critical_ids, strict=True),
        recall_by_severity={s: _recall(statuses, gold.ids_with(s), strict=False) for s in GoldSeverity},
        extra_real=extras.count(ExtraVerdict.REAL),
        extra_noise=noise,
        unmatched_unjudged=sum(u.verdict is None for u in unmatched),
        noise_share=noise / findings if findings else None,
        quotes=run.quotes,
        quotes_final=run.quotes_final,
        calls=run.calls,
        judge=judge,
        keys=run.keys,
        unmatched=unmatched,
        overrides=list(applied),
        warnings=list(verdict.warnings),
        notes=list(run.notes),
        agent_version=run.agent_version,
        layout=run.layout,
        document=run.document,
    )


def jaccard(a: Set[T], b: Set[T]) -> float:
    """|A ∩ B| / |A ∪ B|; два пустых множества считаем одинаковыми (1.0)."""
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def mean_pairwise_jaccard(sets: Sequence[Set[T]]) -> float | None:
    """Средний Жаккар по всем парам; ``None``, если прогонов меньше двух."""
    pairs = list(combinations(sets, 2))
    return fmean(jaccard(a, b) for a, b in pairs) if pairs else None


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return fmean(present) if present else None


class ConfigScore(BaseModel):
    """Сводка по прогонам одной конфигурации."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: int
    recall: float | None
    recall_strict: float | None
    critical_recall: float | None
    critical_recall_strict: float | None
    findings: float | None
    noise_share: float | None
    quotes_exact: float | None
    quotes_verified: float | None
    quotes_final_verified: float | None
    wall_s: float | None
    cost_usd: float | None
    union_recall: float
    union_recall_strict: float
    critical_pass_all: float = Field(description="pass^k: доля критичных, найденных во всех прогонах")
    critical_pass_any: float = Field(description="pass@k: доля критичных, найденных хотя бы раз")
    gold_jaccard: float | None = Field(description="Жаккар множеств найденных дефектов эталона")
    key_jaccard: float | None = Field(description="Жаккар множеств находок агента (stable_key и свойство)")
    judge_cost_usd: float | None = None


def _hits(run: RunScore, *, strict: bool) -> set[int]:
    statuses = (GoldStatus.FOUND,) if strict else (GoldStatus.FOUND, GoldStatus.PARTIAL)
    return set(run.ids_with(*statuses))


def aggregate(runs: Sequence[RunScore], gold: Gold) -> ConfigScore:
    lenient = [_hits(r, strict=False) for r in runs]
    strict = [_hits(r, strict=True) for r in runs]
    critical = set(gold.critical_ids)
    in_all = set.intersection(*lenient) if lenient else set()
    in_any = set.union(*lenient) if lenient else set()
    key_sets = [set(r.keys) for r in runs if r.keys is not None]
    judge_costs = [r.judge.cost_usd for r in runs if r.judge.cost_usd is not None and r.judge.source == "cli"]
    return ConfigScore(
        runs=len(runs),
        recall=_mean(r.recall for r in runs),
        recall_strict=_mean(r.recall_strict for r in runs),
        critical_recall=_mean(r.critical_recall for r in runs),
        critical_recall_strict=_mean(r.critical_recall_strict for r in runs),
        findings=_mean(float(r.findings) for r in runs),
        noise_share=_mean(r.noise_share for r in runs),
        quotes_exact=_mean(r.quotes.share_exact for r in runs),
        quotes_verified=_mean(r.quotes.share_verified for r in runs),
        quotes_final_verified=_mean(r.quotes_final.share_verified for r in runs if r.quotes_final),
        wall_s=_mean(r.calls.wall_s for r in runs),
        cost_usd=_mean(r.calls.cost_usd for r in runs),
        union_recall=_share(len(in_any), len(gold.ids)),
        union_recall_strict=_share(len(set.union(*strict)) if strict else 0, len(gold.ids)),
        critical_pass_all=_share(len(in_all & critical), len(critical)),
        critical_pass_any=_share(len(in_any & critical), len(critical)),
        gold_jaccard=mean_pairwise_jaccard(lenient),
        key_jaccard=mean_pairwise_jaccard(key_sets) if len(key_sets) == len(runs) else None,
        judge_cost_usd=round(sum(judge_costs), 6) if judge_costs else None,
    )
