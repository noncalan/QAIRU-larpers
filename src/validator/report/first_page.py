"""Первая страница, сводка и итог (#123): один список критичных нарушений, общий для Markdown и PDF.

Лид QA открывает отчёт ради одного ответа: можно ли начинать разработку и тестирование, и что решить
первым. Поэтому на первой странице все критичные нарушения (каждое одной строкой с цитатой и местом),
а в сводке тот же список с классом дефекта по ТЗ п. 6. Порядок «что исправить первым» считает
``build._fix_first``; здесь только готовые строки, без подсчётов важности.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from validator.models import DefectType, Finding, Quote, Severity, Verdict
from validator.report.fmt import plain, where

REQUIREMENTS_SHOWN = 4
"""Сколько R-NN перечислить в строке находки на первой странице, остальные «и ещё N»."""

TITLE_LIMIT = 220
QUOTE_LIMIT = 160

CRITICAL_CLASSES = frozenset(d for d in DefectType if d.critical_class)
"""Классы дефектов, которые ТЗ п. 6 называет критичными: непроверяемые, противоречивые, неполные."""

TZ_CLASSES_TITLE = (
    "Все нарушения классов из ТЗ п. 6 (непроверяемые, противоречивые, неполные), любой важности"
)

Resolver = Callable[[Quote, Sequence[str]], list[str]]


@dataclass(frozen=True)
class FixItem:
    """Находка первой страницы или сводки: суть, первая сверенная цитата, место и связи."""

    id: str
    severity: str
    tone: str
    kind: str
    title: str
    quote: Quote | None
    place: str
    requirements: str
    questions: str


def _ids(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:REQUIREMENTS_SHOWN])
    rest = len(ids) - REQUIREMENTS_SHOWN
    return f"{shown} и ещё {rest}" if rest > 0 else shown


def fix_item(
    f: Finding,
    resolve: Resolver,
    sections: Mapping[str, Sequence[str]],
    questions: Mapping[str, Sequence[str]],
) -> FixItem:
    first = next((q for q in f.quotes if q.verified), None)
    place = where(first.anchor, resolve(first, sections.get(f.id, []))) if first else ""
    return FixItem(
        id=f.id,
        severity=f.severity.label,
        tone=f.severity.value,
        kind=f.defect_type.label,
        title=plain(f.title or f.explanation, TITLE_LIMIT),
        quote=first,
        place=place,
        requirements=_ids(f.requirement_ids),
        questions=", ".join(questions.get(f.id, [])),
    )


def fix_items(
    order: Sequence[str],
    findings: Mapping[str, Finding],
    resolve: Resolver,
    sections: Mapping[str, Sequence[str]],
    questions: Mapping[str, Sequence[str]],
) -> list[FixItem]:
    """Строки «Что исправить первым» в порядке ``conclusion.fix_first``."""
    return [fix_item(findings[fid], resolve, sections, questions) for fid in order if fid in findings]


def critical_items(
    findings: Sequence[Finding],
    order: Sequence[str],
    resolve: Resolver,
    sections: Mapping[str, Sequence[str]],
    questions: Mapping[str, Sequence[str]],
) -> list[FixItem]:
    """Критичные нарушения для сводки: те же, что в счётчике «критичных», в порядке первой страницы."""
    critical = [f for f in findings if f.severity is Severity.CRITICAL and f.verdict is Verdict.VIOLATION]
    rank = {fid: n for n, fid in enumerate(order)}
    critical.sort(key=lambda f: rank.get(f.id, len(rank)))
    return [fix_item(f, resolve, sections, questions) for f in critical]


def tz_class_findings(findings: Sequence[Finding]) -> list[Finding]:
    """Нарушения классов из ТЗ п. 6 любой важности; «неясно» не нарушение и сюда не входит."""
    return [f for f in findings if f.defect_type in CRITICAL_CLASSES and f.verdict is Verdict.VIOLATION]


def fix_intro(items: Sequence[FixItem]) -> str:
    """Пояснение над списком: почему здесь именно эти находки и в каком порядке."""
    critical = sum(1 for i in items if i.tone == Severity.CRITICAL.value)
    if not items:
        return ""
    if critical == len(items):
        return (
            f"Все критичные нарушения ({critical}): каждое мешает начать работу. Сначала те, что мешают "
            "написать тест-кейс и затрагивают больше требований."
        )
    if critical:
        return (
            f"Все критичные нарушения ({critical}), затем самые важные из остальных. Внутри уровня важности "
            "сначала то, что мешает написать тест-кейс и затрагивает больше требований."
        )
    return (
        "Критичных нарушений нет. Самые важные из остальных: сначала то, что мешает написать тест-кейс "
        "и затрагивает больше требований."
    )


__all__ = [
    "CRITICAL_CLASSES",
    "TZ_CLASSES_TITLE",
    "FixItem",
    "critical_items",
    "fix_intro",
    "fix_item",
    "fix_items",
    "tz_class_findings",
]
