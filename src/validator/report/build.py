"""Шаг 9а: сборка отчёта в одну модель ``Report`` (минимальная версия каркаса, полноту делает #23).

``report.json`` это сериализация ``Report``; Markdown и PDF рендерятся из неё. Здесь ничего не
считается заново: важность, счётчики и светофор пришли от верификатора, здесь только итог прогона
с учётом диагностики (упавший шаг не даёт «зелёного» итога).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from validator.models import (
    CallStatus,
    Conclusion,
    DiagLevel,
    Diagnostic,
    DocumentInfo,
    Question,
    QuestionPriority,
    Report,
    ReportStatus,
    RunContext,
    RunMetrics,
    TrafficLight,
    Verdict,
    Verification,
)
from validator.questions.metrics import question_metrics

FIRST_PAGE_ITEMS = 5
"""Сколько исправлений и вопросов на первой странице (решение 1 сессии идеи: 5 и 5)."""


def _dedupe(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    return list(dict.fromkeys(diagnostics))


def _split(diagnostics: Sequence[Diagnostic]) -> tuple[list[Diagnostic], list[Diagnostic]]:
    unique = _dedupe(diagnostics)
    inputs = [d for d in unique if d.code.startswith("IN-")]
    others = [d for d in unique if not d.code.startswith("IN-")]
    return inputs, others


def _fix_first(ver: Verification) -> list[str]:
    """Первая страница: нарушения со сверенной цитатой по важности, затем в порядке верификатора."""
    candidates = [
        f for f in ver.findings if f.verdict is Verdict.VIOLATION and any(q.verified for q in f.quotes)
    ]
    candidates.sort(key=lambda f: f.severity.rank)  # сортировка устойчивая: порядок верификатора сохраняется
    return [f.id for f in candidates[:FIRST_PAGE_ITEMS]]


def _ask_first(questions: Sequence[Question]) -> list[str]:
    """Вопросы, блокирующие работу (P0, затем P1), по порядку в списке вопросов."""
    blocking = [q for q in questions if q.priority in (QuestionPriority.P0, QuestionPriority.P1)]
    blocking.sort(key=lambda q: q.priority.value)  # P0 раньше P1, порядок внутри приоритета сохраняется
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
    return Conclusion(
        traffic_light=light,
        headline=headline,
        fix_first=_fix_first(ver),
        ask_first=_ask_first(ctx.questions),
    )


def _metrics(ctx: RunContext) -> RunMetrics:
    ver = ctx.verification
    summary = ver.summary if ver else None
    quotes_total = summary.quotes_total if summary else 0
    verified = (summary.quotes_exact + summary.quotes_fuzzy) if summary else 0
    return RunMetrics(
        duration_s=ctx.passport.duration_s,
        calls_total=ctx.passport.calls_total,
        calls_failed=ctx.passport.calls_failed,
        calls_cached=sum(c.status is CallStatus.CACHED for c in ctx.calls),
        tokens=ctx.passport.tokens,
        cost_usd=ctx.passport.cost_usd,
        candidates_total=sum(len(p.candidates) for p in ctx.passes) + len(ctx.lint_hits),
        merged_total=len(ctx.merged),
        findings_total=len(ver.findings) if ver else 0,
        discarded_total=len(ver.discarded) if ver else 0,
        quotes_total=quotes_total,
        quotes_verified=verified,
        verified_quote_share=round(verified / quotes_total, 4) if quotes_total else None,
        questions=question_metrics(ctx.questions, ctx.questions_withdrawn) if ver else None,
    )


def _document(ctx: RunContext) -> DocumentInfo | None:
    if ctx.doc is not None:
        doc = ctx.doc
        return DocumentInfo(
            name=doc.name,
            path=doc.path,
            kind=doc.kind,
            sha256=doc.sha256,
            size_bytes=doc.size_bytes,
            pages=doc.pages,
            chars=len(doc.text),
            blocks=len(doc.blocks),
            extraction=doc.extraction,
        )
    if ctx.guard is not None:
        g = ctx.guard
        return DocumentInfo(name=g.name, path=g.path, kind=g.kind, sha256=g.sha256, size_bytes=g.size_bytes)
    return None


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
    )
