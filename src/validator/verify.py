"""Шаг 7: верификатор без модели (заглушка каркаса).

Контракт: ``verify(found, doc, seg) -> Verification``. Принцип «модель предлагает, код проверяет»:
цитаты сверяет код, находка без единой сверенной цитаты уходит в «отброшено», важность, ``F-NN``,
сводку и светофор считает код. Таблицу важности «тип дефекта × мешает тесту», лимиты и полные
правила светофора делает #22. Заглушка: сверка цитат, привязка к ``R-NN`` по пересечению якорей,
важность из подсказки модели, простой светофор.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from validator.models import (
    DiscardedFinding,
    DiscardReason,
    Finding,
    MergedFinding,
    ParsedDocument,
    Quote,
    QuoteMatch,
    Scope,
    Segmentation,
    Severity,
    Summary,
    TrafficLight,
    Verdict,
    Verification,
)
from validator.text import locate_quote

DEFAULT_SEVERITY = Severity.MAJOR
"""Важность, если модель не дала подсказку (таблицу важности делает #22)."""


def _locate(doc: ParsedDocument, quote: str, block_ids: Sequence[str]) -> tuple[Quote, bool]:
    """Сначала в указанных блоках, затем во всём документе. Второе значение: якорь исправил код."""
    if block_ids:
        found = locate_quote(doc, quote, within=block_ids)
        if found.verified:
            return found, False
    found = locate_quote(doc, quote)
    return found, bool(block_ids) and found.verified


def _requirement_ids(quotes: Sequence[Quote], seg: Segmentation) -> list[str]:
    out: list[str] = []
    for req in seg.requirements:
        for q in quotes:
            a = q.anchor
            if a and a.char_start < req.anchor.char_end and req.anchor.char_start < a.char_end:
                out.append(req.id)
                break
    return out


def _check(
    m: MergedFinding, doc: ParsedDocument, seg: Segmentation
) -> tuple[Finding | DiscardedFinding, list[Quote]]:
    """Находка с найденными цитатами или отказ, плюс все сверенные цитаты (для доли сверенных).

    Каждая находка с цитатой (CLAUDE.md, правило 4): без единой найденной цитаты находку отбрасываем
    при любом уровне, а ненайденные цитаты в находку не попадают, их считает только сводка.
    """
    located = [_locate(doc, q, m.block_ids) for q in m.quotes]
    quotes = [q for q, _ in located]
    verified = [q for q in quotes if q.verified]
    if not verified:
        reason = "ни одна цитата не найдена" if m.quotes else "у находки нет цитаты"
        return DiscardedFinding(finding=m, reason=DiscardReason.QUOTE_NOT_FOUND, detail=reason), quotes
    first = verified[0].anchor if verified else None
    block = doc.block(first.block_ids[0]) if first and first.block_ids else None
    return Finding(
        id="F-00",  # номер ставится после сортировки
        merged_id=m.mid,
        property=m.property,
        defect_type=m.defect_type,
        verdict=m.verdict,
        scope=m.scope,
        severity=m.severity_hint or DEFAULT_SEVERITY,
        severity_hint=m.severity_hint,
        blocks_testing=m.blocks_testing,
        quotes=verified,
        requirement_ids=_requirement_ids(verified, seg),
        section_path=list(block.section) if block else [],
        explanation=m.explanation,
        suggestion=m.suggestion,
        title=m.title,
        scenario=m.scenario,
        test_gap=m.test_gap,
        found_in=m.found_in,
        of=m.of,
        sources=list(m.sources),
        witness=m.witness,
        test_draft=m.test_draft,
        critic=m.critic,
        anchor_corrected=any(fixed for _, fixed in located),
    ), quotes


def _position(f: Finding) -> int:
    anchors = [q.anchor.char_start for q in f.quotes if q.anchor]
    return min(anchors) if anchors else 1 << 30


def summarize(
    findings: Sequence[Finding],
    discarded: Sequence[DiscardedFinding],
    seg: Segmentation,
    quotes: Sequence[Quote] | None = None,
) -> Summary:
    """Сводка по находкам.

    ``quotes``: все цитаты кандидатов, включая отброшенные (для доли сверенных); по умолчанию цитаты находок.
    """
    quotes = list(quotes) if quotes is not None else [q for f in findings for q in f.quotes]
    critical = [f.id for f in findings if f.severity is Severity.CRITICAL and f.verdict is Verdict.VIOLATION]
    with_findings = {r for f in findings for r in f.requirement_ids}
    return Summary(
        requirements_total=len(seg.requirements),
        requirements_with_findings=len(with_findings),
        findings_total=len(findings),
        violations=sum(f.verdict is Verdict.VIOLATION for f in findings),
        unclear=sum(f.verdict is Verdict.UNCLEAR for f in findings),
        critical=len(critical),
        critical_ids=critical,
        by_severity=dict(Counter(f.severity for f in findings)),
        by_property=dict(Counter(f.property for f in findings)),
        by_defect_type=dict(Counter(f.defect_type for f in findings)),
        set_level=sum(f.scope is Scope.SET for f in findings),
        discarded_total=len(discarded),
        discarded_by_reason=dict(Counter(d.reason for d in discarded)),
        quotes_total=len(quotes),
        quotes_exact=sum(q.match is QuoteMatch.EXACT for q in quotes),
        quotes_fuzzy=sum(q.match is QuoteMatch.FUZZY for q in quotes),
        quotes_not_found=sum(not q.verified for q in quotes),
    )


def traffic_light(summary: Summary) -> tuple[TrafficLight, str]:
    """Заглушка правила светофора: красный при критичных нарушениях, жёлтый при любых находках."""
    if summary.critical:
        return TrafficLight.RED, f"критичных нарушений: {summary.critical}"
    if summary.findings_total:
        return TrafficLight.YELLOW, f"есть нарушения ({summary.violations}) или неясности ({summary.unclear})"
    return TrafficLight.GREEN, "нарушений не найдено"


def verify(found: Sequence[MergedFinding], doc: ParsedDocument, seg: Segmentation) -> Verification:
    results = [_check(m, doc, seg) for m in found]
    checked = [c for c, _ in results]
    all_quotes = [q for _, quotes in results for q in quotes]
    kept = [c for c in checked if isinstance(c, Finding)]
    discarded = [c for c in checked if isinstance(c, DiscardedFinding)]
    kept.sort(key=lambda f: (f.severity.rank, _position(f)))
    findings = [f.model_copy(update={"id": f"F-{n:02d}"}) for n, f in enumerate(kept, start=1)]
    summary = summarize(findings, discarded, seg, all_quotes)
    light, reason = traffic_light(summary)
    status = {r.id: [f.id for f in findings if r.id in f.requirement_ids] for r in seg.requirements}
    return Verification(
        findings=findings,
        discarded=discarded,
        summary=summary,
        traffic_light=light,
        traffic_reason=reason,
        requirement_status=status,
    )
