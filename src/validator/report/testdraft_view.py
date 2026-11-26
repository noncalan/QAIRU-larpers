"""Попытка тест-кейса в отчёте (#40): готовые строки блока «Попытка теста» для Markdown и PDF.

Блок и его подсчёты готовит код, шаблон и PDF только раскладывают строки: так оба вида отчёта
показывают одно и то же, а подписи проверяются тестами. Здесь нет ни модели, ни решений: черновик,
цитаты и связи с вопросами уже проверены шагами 6 и 8.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from validator.models import DraftGap, Finding, TestDraft
from validator.report.fmt import plain, where

LABEL = "Попытка теста"
NO_QUESTION = "отдельного вопроса автору нет"


@dataclass(frozen=True)
class DraftView:
    """Блок «Попытка теста» одной находки: статус, части теста, пропуски и значения из документа."""

    status: str
    parts: tuple[tuple[str, str], ...]
    gaps: tuple[str, ...]
    values: tuple[str, ...]
    note: str


@dataclass(frozen=True)
class DraftStats:
    """Сводка по попыткам теста для раздела «Сводка»."""

    drafts: int
    with_gaps: int
    complete: int
    gaps: int
    linked: int
    values: int
    replaced: int


def _gap_line(g: DraftGap) -> str:
    text = f"[?{g.number}] ({g.part}): {plain(g.missing)}" if g.part else f"[?{g.number}]: {plain(g.missing)}"
    if g.question_id and g.question_withdrawn:
        return f"{text} → {g.question_id} снят: ответ есть в документе"
    if g.question_id:
        return f"{text} → {g.question_id}"
    return f"{text} → {NO_QUESTION}"


def _status(draft: TestDraft) -> str:
    replaced = sum(1 for g in draft.gap_items if g.replaced)
    if draft.contested:
        return "тест написан по одному из двух прочтений документа: находка по-прежнему мешает тестированию"
    if draft.complete:
        return "тест написать можно: все значения результата взяты из документа, у каждого есть цитата"
    if draft.gap_items:
        text = f"тест написать нельзя без решения автора: пропусков {len(draft.gap_items)}"
        if replaced:
            text += f", из них {replaced} поставил код (значение модели было без цитаты из документа)"
        return text
    return "вывод о возможности теста не принят: нет значений с цитатами из документа"


def _steps(steps: Sequence[str]) -> str:
    return " ".join(f"{i}. {plain(s).rstrip('.')}." for i, s in enumerate(steps, 1))


def draft_view(f: Finding) -> DraftView | None:
    """Блок попытки теста находки или None, если попытки не было."""
    d = f.test_draft
    if d is None:
        return None
    parts = [
        ("Предусловие", plain(d.precondition)),
        ("Шаги", _steps(d.steps) if d.steps else plain(d.action)),
        ("Ожидаемый результат", plain(d.expected)),
    ]
    values = tuple(
        f"«{plain(v.value)}»: «{plain(v.quote, 200)}» ({where(v.anchor)})" for v in d.values if v.verified
    )
    return DraftView(
        status=_status(d),
        parts=tuple((label, text) for label, text in parts if text),
        gaps=tuple(_gap_line(g) for g in d.gap_items),
        values=values,
        note=plain(d.note),
    )


def draft_stats(findings: Sequence[Finding]) -> DraftStats | None:
    """Подсчёт по находкам с попыткой теста; None, если попыток не было."""
    drafts = [f.test_draft for f in findings if f.test_draft is not None]
    if not drafts:
        return None
    gaps = [g for d in drafts for g in d.gap_items]
    values = [v for d in drafts for v in d.values]
    return DraftStats(
        drafts=len(drafts),
        with_gaps=sum(1 for d in drafts if d.gap_items),
        complete=sum(1 for d in drafts if d.complete and not d.contested),
        gaps=len(gaps),
        linked=sum(1 for g in gaps if g.question_id),
        values=sum(1 for v in values if v.verified),
        replaced=sum(1 for g in gaps if g.replaced),
    )


def stats_line(s: DraftStats) -> str:
    """Строка сводки: сколько черновиков, пропусков и как они связаны с вопросами."""
    return (
        f"{LABEL}: черновиков {s.drafts}; с пропусками {s.with_gaps} (пропусков {s.gaps}, "
        f"связано с вопросами автору {s.linked}); тест написать можно {s.complete}; "
        f"значений из документа с цитатой {s.values}, заменено кодом на пропуск (нет цитаты) {s.replaced}."
    )


__all__ = ["LABEL", "DraftStats", "DraftView", "draft_stats", "draft_view", "stats_line"]
