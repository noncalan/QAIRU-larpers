"""Шаг 5: слияние проходов и линтера, «найдено в N из K» (заглушка каркаса).

Контракт: ``merge(passes, lint_hits, doc) -> list[MergedFinding]``. Настоящую кластеризацию (пересечение
блоков и цитат, совместимость свойств, независимость от порядка проходов) делает #29. Заглушка
сливает только точные повторы: то же свойство и та же первая цитата после нормализации.
"""

from __future__ import annotations

from collections.abc import Sequence

from validator.models import CandidateFinding, MergedFinding, ParsedDocument, PassResult, SourceKind
from validator.text import stable_key


def _key(cand: CandidateFinding) -> tuple[str, str]:
    first = cand.quotes[0] if cand.quotes else cand.explanation
    return cand.property.value, stable_key(first)


def merge(
    passes: Sequence[PassResult], lint_hits: Sequence[CandidateFinding], doc: ParsedDocument
) -> list[MergedFinding]:
    del doc  # понадобится настоящему слиянию (сверка цитат по смещениям)
    of = sum(1 for p in passes if p.ok)
    clusters: dict[tuple[str, str], list[CandidateFinding]] = {}
    for cand in [c for p in passes if p.ok for c in p.candidates] + list(lint_hits):
        clusters.setdefault(_key(cand), []).append(cand)

    merged: list[MergedFinding] = []
    for members in clusters.values():
        head = members[0]
        passes_found = {m.source.pass_index for m in members if m.source.kind is SourceKind.PASS}
        merged.append(
            MergedFinding(
                mid=f"M-{len(merged) + 1:03d}",
                property=head.property,
                defect_type=head.defect_type,
                verdict=head.verdict,
                scope=head.scope,
                block_ids=sorted({b for m in members for b in m.block_ids}),
                quotes=list(dict.fromkeys(q for m in members for q in m.quotes)),
                explanation=max((m.explanation for m in members), key=len),
                suggestion=next((m.suggestion for m in members if m.suggestion), None),
                title=next((m.title for m in members if m.title), None),
                scenario=next((m.scenario for m in members if m.scenario), None),
                test_gap=next((m.test_gap for m in members if m.test_gap), None),
                blocks_testing=any(m.blocks_testing for m in members),
                severity_hint=head.severity_hint,
                sources=[m.source for m in members],
                members=[m.cid for m in members],
                found_in=len(passes_found),
                of=of,
            )
        )
    return merged
