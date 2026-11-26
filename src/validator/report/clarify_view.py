"""Раздел «Уточнения» (issue #42): готовые строки для Markdown и PDF из ``Report.clarification``.

Шаблон Markdown и ``render_pdf`` только раскладывают эти строки, поэтому оба формата показывают одно и
то же. Здесь ничего не решается: что закрыто, уточнено и ново, решил шаг уточнений (``clarify``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from validator.models import (
    AnswerKind,
    AuthorAnswer,
    Clarification,
    ClarifiedFinding,
    ClarifyEffect,
    ClarifySnapshot,
    Report,
    TrafficLight,
)
from validator.report.fmt import plain, where

CLARIFY_TITLE = "Уточнения: что изменили ответы автора"
CLOSED_NOTE = (
    "Находка закрыта: автор выбрал правило, и разработка может опираться на него. Документ при этом "
    "не изменился, поэтому предложенную правку текста нужно внести в документ."
)
SHA_SHOWN = 12


@dataclass(frozen=True)
class ClarifyView:
    """Всё, что показывает раздел «Уточнения»; строки уже без разметки."""

    intro: str
    compare: list[tuple[str, str, str]]
    answers: list[tuple[str, str, str]]
    closed: list[tuple[str, str, str, str, str]]
    refined: list[tuple[str, str, str, str]]
    new: list[tuple[str, str, str, str]]
    open_questions: str
    warnings: list[str] = field(default_factory=list)
    summary_line: str = ""
    reviewed: bool = False


_COLOR = {TrafficLight.RED: "красный", TrafficLight.YELLOW: "жёлтый", TrafficLight.GREEN: "зелёный"}


def _light(s: ClarifySnapshot) -> str:
    """Цвет словом, без значка: шрифты PDF не рисуют цветные кружки."""
    return f"{_COLOR[s.traffic_light]}: {s.traffic_light.label}"


def _compare(before: ClarifySnapshot | None, after: ClarifySnapshot | None) -> list[tuple[str, str, str]]:
    if before is None or after is None:
        return []
    return [
        ("Светофор", _light(before), _light(after)),
        ("Находок в отчёте", str(before.findings), str(after.findings)),
        ("Нарушений", str(before.violations), str(after.violations)),
        ("Критичных", str(before.critical), str(after.critical)),
        ("Открытых вопросов автору", str(before.open_questions), str(after.open_questions)),
        ("Из них блокируют разработку или тестирование", str(before.open_blocking), str(after.open_blocking)),
    ]


def _answer_text(a: AuthorAnswer) -> str:
    if a.kind is AnswerKind.OPTION:
        text = f"вариант {a.option}: {plain(a.text, 200)}"
    elif a.kind is AnswerKind.TEXT:
        text = f"свой ответ: «{plain(a.text, 300)}»"
    elif a.kind is AnswerKind.SKIPPED:
        text = "отложен"
    else:
        text = "нет ответа"
    if a.comment:
        text += f" ({plain(a.comment, 200)})"
    return text


def _effects_by_question(effects: Sequence[ClarifiedFinding]) -> dict[str, dict[ClarifyEffect, list[str]]]:
    out: dict[str, dict[ClarifyEffect, list[str]]] = {}
    for e in effects:
        for qid in e.question_ids:
            out.setdefault(qid, {}).setdefault(e.effect, []).append(e.finding_id)
    return out


def _answer_effect(a: AuthorAnswer, by_q: dict[str, dict[ClarifyEffect, list[str]]], reviewed: bool) -> str:
    if not a.kind.answered:
        return "вопрос остаётся открытым" if a.asked_again else "вопрос в этом прогоне не задан"
    if not reviewed:
        return "сверка с находками не выполнена"
    parts = [f"{effect.label}: {', '.join(ids)}" for effect, ids in by_q.get(a.question_id, {}).items()]
    return "; ".join(parts) or "находок не изменил"


def _closed_row(e: ClarifiedFinding) -> tuple[str, str, str, str, str]:
    f = e.finding
    head = f"{e.finding_id} · {f.severity.label} · {f.property.label}" if f else e.finding_id
    first = next((q for q in f.quotes if q.verified), None) if f else None
    place = f"«{plain(first.located or first.text, 160)}» ({where(first.anchor)})" if first else "—"
    return head, ", ".join(e.question_ids), plain(e.reason, 300) or "—", plain(e.fix, 300) or "—", place


def _refined_row(e: ClarifiedFinding) -> tuple[str, str, str, str]:
    reason = plain(e.reason, 300) or "—"
    return e.finding_id, ", ".join(e.question_ids), reason, plain(e.remaining, 300) or "—"


def _new_row(e: ClarifiedFinding, report: Report) -> tuple[str, str, str, str]:
    ver = report.verification
    f = next((x for x in ver.findings if x.id == e.finding_id), None) if ver else None
    head = f"{e.finding_id} · {f.severity.label} · {f.property.label}" if f else e.finding_id
    return head, ", ".join(e.question_ids), plain(e.reason, 300), plain(e.fix, 300) or "—"


def _intro(c: Clarification, answered: int) -> str:
    by = f", ответил: {plain(c.answered_by, 120)}" if c.answered_by else ""
    skipped = sum(a.kind is AnswerKind.SKIPPED for a in c.answers)
    empty = sum(a.kind is AnswerKind.NONE for a in c.answers)
    return (
        f"Файл ответов {c.answers_file} (SHA-256 {c.answers_sha256[:SHA_SHOWN]}…){by}. "
        f"Ответов: {answered} из {len(c.answers)} вопросов (отложено {skipped}, без ответа {empty}). "
        "Ответы автора агент считает данными: документ не меняется, цитаты сверяются только с ним, "
        "а модель лишь предлагает, какие находки ответ закрыл; связь находки с вопросом и цитаты новых "
        "находок проверяет код."
    )


def clarify_view(report: Report) -> ClarifyView | None:
    c = report.clarification
    if c is None:
        return None
    answered = len(c.answered)
    by_q = _effects_by_question(c.effects)
    closed = c.by_effect(ClarifyEffect.CLOSED)
    refined = c.by_effect(ClarifyEffect.REFINED)
    new = c.by_effect(ClarifyEffect.NEW)
    open_ids = [q.id for q in report.questions]
    warnings = []
    if c.document_matches is False:
        warnings.append(
            "Ответы даны к другой версии документа: вопросы сопоставлены по тексту, проверьте ответы заново."
        )
    if not c.reviewed and answered:
        warnings.append(
            "Сверка ответов с находками не выполнена (см. диагностику прогона): "
            "находки остались как без ответов."
        )
    summary = (
        f"Ответов автора: {answered}; закрыто находок: {len(closed)}, уточнено: {len(refined)}, "
        f"новых из-за ответов: {len(new)}; открытых вопросов: {len(open_ids)}."
    )
    return ClarifyView(
        intro=_intro(c, answered),
        compare=_compare(c.before, c.after),
        answers=[(a.question_id, _answer_text(a), _answer_effect(a, by_q, c.reviewed)) for a in c.answers],
        closed=[_closed_row(e) for e in closed],
        refined=[_refined_row(e) for e in refined],
        new=[_new_row(e, report) for e in new],
        open_questions=", ".join(open_ids) if open_ids else "все вопросы получили ответ",
        warnings=warnings,
        summary_line=summary,
        reviewed=c.reviewed,
    )


__all__ = ["CLARIFY_TITLE", "CLOSED_NOTE", "ClarifyView", "clarify_view"]
