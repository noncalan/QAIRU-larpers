"""Шаг 9а: сборка отчёта в одну модель ``Report`` (минимальная версия каркаса, полноту делает #23).

``report.json`` это сериализация ``Report``; Markdown и PDF рендерятся из неё. Здесь ничего не
считается заново: важность, счётчики и светофор пришли от верификатора, здесь только итог прогона
с учётом диагностики (упавший шаг не даёт «зелёного» итога).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from validator.evidence_metrics import evidence_metrics
from validator.models import (
    BlockKind,
    CallStatus,
    ChecklistUse,
    Conclusion,
    CoverageMatrix,
    DiagLevel,
    Diagnostic,
    DocumentInfo,
    Finding,
    ParsedDocument,
    Question,
    QuestionPriority,
    Report,
    ReportStatus,
    RunContext,
    RunMetrics,
    Severity,
    TrafficLight,
    Verdict,
    Verification,
)
from validator.questions.metrics import question_metrics
from validator.report.coverage import build_coverage
from validator.report.run_metrics import (
    critic_counts,
    discarded_by_reason,
    found_in_distribution,
    step_metrics,
)

FIRST_PAGE_ITEMS = 5
"""Сколько исправлений и вопросов на первой странице (решение 1 сессии идеи: 5 и 5). Критичные нарушения
показываются все, даже если их больше (#123): каждое из них одно решает, можно ли начинать работу."""


def _dedupe(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    return list(dict.fromkeys(diagnostics))


def _split(diagnostics: Sequence[Diagnostic]) -> tuple[list[Diagnostic], list[Diagnostic]]:
    unique = _dedupe(diagnostics)
    inputs = [d for d in unique if d.code.startswith("IN-")]
    others = [d for d in unique if not d.code.startswith("IN-")]
    return inputs, others


def impact(f: Finding) -> tuple[int, int]:
    """Насколько широко бьёт находка: затронуто требований и пропусков в попытке тест-кейса."""
    gaps = len(f.test_draft.gap_items) if f.test_draft is not None else 0
    return len(f.requirement_ids), gaps


def _fix_first(ver: Verification) -> list[str]:
    """Первая страница: нарушения со сверенной цитатой, все критичные и до ``FIRST_PAGE_ITEMS`` всего.

    Порядок: важность; внутри неё сначала то, что мешает написать тест-кейс, затем то, что затрагивает
    больше требований и оставляет больше пропусков в попытке теста; при равенстве порядок верификатора.
    """
    candidates = [
        f for f in ver.findings if f.verdict is Verdict.VIOLATION and any(q.verified for q in f.quotes)
    ]
    order = {f.id: n for n, f in enumerate(candidates)}

    def key(f: Finding) -> tuple[int, bool, int, int, int]:
        reqs, gaps = impact(f)
        return f.severity.rank, not f.blocks_testing, -reqs, -gaps, order[f.id]

    candidates.sort(key=key)
    critical = sum(f.severity is Severity.CRITICAL for f in candidates)
    return [f.id for f in candidates[: max(critical, FIRST_PAGE_ITEMS)]]


def _ask_first(questions: Sequence[Question], fix_first: Sequence[str] = ()) -> list[str]:
    """Вопросы, блокирующие работу: P0, затем P1. Внутри приоритета сначала вопросы к находкам из «Что
    исправить первым» в их порядке (блокеры наверх, #123), затем остальные в порядке списка вопросов."""
    rank = {fid: n for n, fid in enumerate(fix_first)}
    blocking = [q for q in questions if q.priority in (QuestionPriority.P0, QuestionPriority.P1)]
    order = {q.id: n for n, q in enumerate(blocking)}

    def key(q: Question) -> tuple[str, int, int]:
        linked = min((rank[f] for f in q.related_findings if f in rank), default=len(rank))
        return q.priority.value, linked, order[q.id]

    blocking.sort(key=key)
    return [q.id for q in blocking[:FIRST_PAGE_ITEMS]]


def _conclusion(ctx: RunContext, errors: Sequence[Diagnostic]) -> Conclusion:
    if ctx.doc is None:
        reason = errors[0].message if errors else "вход не прочитан"
        return Conclusion(
            traffic_light=TrafficLight.RED,
            headline=f"Проверка не выполнена: {reason}",
            performed=False,
        )
    ver = ctx.verification
    if ver is None:
        return Conclusion(
            traffic_light=TrafficLight.YELLOW,
            headline=(
                "Проверка выполнена не полностью: верификатор не отработал, смотрите диагностику прогона"
            ),
            ask_first=_ask_first(ctx.questions),
        )
    light, reason = ver.traffic_light, ver.traffic_reason
    headline = f"{light.label[:1].upper()}{light.label[1:]}: {reason}"
    if errors:
        light = TrafficLight.RED if light is TrafficLight.RED else TrafficLight.YELLOW
        headline = f"Проверка выполнена не полностью ({errors[0].message}). По проверенной части: {reason}"
    fix_first = _fix_first(ver)
    return Conclusion(
        traffic_light=light,
        headline=headline,
        fix_first=fix_first,
        ask_first=_ask_first(ctx.questions, fix_first),
    )


def checklist_items(f: Finding) -> list[str]:
    """Пункты доменных чек-листов, которыми проходы отметили находку (#35), по порядку и без повторов."""
    return list(dict.fromkeys(s.checklist_item for s in f.sources if s.checklist_item))


def _checklists(ctx: RunContext) -> list[ChecklistUse]:
    """Выбор чек-листов прогона и F-NN находок по каждому: чек-лист это префикс ID пункта (MON-02 → MON)."""
    findings = ctx.verification.findings if ctx.verification else []
    by_list: dict[str, list[str]] = {}
    for f in findings:
        for list_id in dict.fromkeys(item.split("-", 1)[0] for item in checklist_items(f)):
            by_list.setdefault(list_id, []).append(f.id)
    return [u.model_copy(update={"findings": by_list.get(u.id, [])}) for u in ctx.checklists]


def _metrics(ctx: RunContext) -> RunMetrics:
    ver = ctx.verification
    summary = ver.summary if ver else None
    quotes_total = summary.quotes_total if summary else 0
    verified = (summary.quotes_exact + summary.quotes_fuzzy) if summary else 0
    reviewed, downgraded, dropped = critic_counts(ver)
    lint = len(ctx.lint_hits)
    return RunMetrics(
        duration_s=ctx.passport.duration_s,
        calls_total=ctx.passport.calls_total,
        calls_failed=ctx.passport.calls_failed,
        calls_cached=sum(c.status is CallStatus.CACHED for c in ctx.calls),
        tokens=ctx.passport.tokens,
        cost_usd=ctx.passport.cost_usd,
        candidates_total=sum(len(p.candidates) for p in ctx.passes) + lint + len(ctx.set_check),
        merged_total=len(ctx.merged),
        findings_total=len(ver.findings) if ver else 0,
        discarded_total=len(ver.discarded) if ver else 0,
        quotes_total=quotes_total,
        quotes_verified=verified,
        verified_quote_share=round(verified / quotes_total, 4) if quotes_total else None,
        questions=question_metrics(ctx.questions, ctx.questions_withdrawn) if ver else None,
        evidence=evidence_metrics(ctx.merged, critic_enabled=ctx.cfg.critic),
        steps=step_metrics(ctx.calls),
        passes_total=len(ctx.passes),
        passes_ok=sum(p.ok for p in ctx.passes),
        candidates_passes=sum(len(p.candidates) for p in ctx.passes),
        candidates_lint=lint,
        candidates_checklist=sum(bool(c.source.checklist_item) for p in ctx.passes for c in p.candidates),
        findings_checklist=sum(bool(checklist_items(f)) for f in ver.findings) if ver else 0,
        discarded_by_reason=discarded_by_reason(ver),
        critic_reviewed=reviewed,
        critic_downgraded=downgraded,
        critic_dropped=dropped,
        found_in=found_in_distribution(ver.findings) if ver else [],
    )


TITLE_MAX_CHARS = 200
"""Название документа в паспорте длиннее этого обрезается: заголовок не должен растягивать таблицу."""


def document_title(doc: ParsedDocument) -> str | None:
    """Название документа: первый заголовок текста (у файла без заголовков названия нет)."""
    heading = next((b for b in doc.blocks if b.kind is BlockKind.HEADING and b.text.strip()), None)
    if heading is None:
        return None
    title = " ".join(heading.text.lstrip("#").split())
    return title[: TITLE_MAX_CHARS - 1].rstrip() + "…" if len(title) > TITLE_MAX_CHARS else title or None


def _document(ctx: RunContext) -> DocumentInfo | None:
    if ctx.doc is not None:
        doc = ctx.doc
        return DocumentInfo(
            name=doc.name,
            path=doc.path,
            kind=doc.kind,
            title=document_title(doc),
            sha256=doc.sha256,
            size_bytes=doc.size_bytes,
            pages=doc.pages,
            chars=len(doc.text),
            blocks=len(doc.blocks),
            extraction=doc.extraction,
            parts=list(ctx.parts),
        )
    if ctx.guard is not None:
        g = ctx.guard
        return DocumentInfo(name=g.name, path=g.path, kind=g.kind, sha256=g.sha256, size_bytes=g.size_bytes)
    return None


def _coverage(ctx: RunContext) -> CoverageMatrix | None:
    """Матрица покрытия: только когда есть и нарезка, и находки (иначе не по чему её строить)."""
    if ctx.verification is None or ctx.segmentation is None:
        return None
    return build_coverage(ctx.verification, ctx.segmentation.requirements, ctx.passes)


def build_report(ctx: RunContext) -> Report:
    inputs, others = _split(ctx.diagnostics)
    errors = [d for d in others if d.level is DiagLevel.ERROR]
    if ctx.doc is None:
        status = ReportStatus.NOT_PERFORMED
        errors = [d for d in inputs + others if d.level is DiagLevel.ERROR]
    elif errors or any(d.level is not DiagLevel.INFO for d in inputs):
        status = ReportStatus.PARTIAL
    else:
        status = ReportStatus.COMPLETE
    return Report(
        status=status,
        passport=ctx.passport,
        document=_document(ctx),
        input_diagnostics=inputs,
        diagnostics=others,
        segmentation=ctx.segmentation,
        verification=ctx.verification,
        questions=list(ctx.questions),
        questions_withdrawn=list(ctx.questions_withdrawn),
        clarification=ctx.clarification,
        conclusion=_conclusion(ctx, errors),
        metrics=_metrics(ctx),
        coverage=_coverage(ctx),
        checklists=_checklists(ctx),
    )
