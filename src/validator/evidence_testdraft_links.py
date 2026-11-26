"""Шаг 6 и 8: связь пропусков ``[?N]`` черновика теста с вопросами автору (#40, исследование 17, п. 3.4).

Пропуск это неизвестное, которое автор документа должен назвать, то есть почти всегда вопрос. Находка с
пропусками уже попадает в кандидаты шага вопросов (``questions/candidates.py``, источник «тест»), поэтому
отдельных вопросов шаг не создаёт, а после шага вопросов только отмечает в каждом пропуске, какой
``Q-NN`` его закрывает:

- у находки один вопрос: пропуск связан с ним;
- вопросов несколько: берём тот, чей текст ближе к пояснению пропуска (rapidfuzz);
- вопроса нет (редактор отклонил кандидата или шаг вопросов не выполнен): ``question_id`` пусто, отчёт
  говорит об этом прямо;
- вопрос снят критиком, потому что ответ есть в документе: пропуск связан со снятым вопросом и помечен.

Находки не меняются, кроме ``test_draft.gap_items``.
"""

from __future__ import annotations

from collections.abc import Sequence

from rapidfuzz import fuzz

from validator.models import DraftGap, Finding, Question, Verification, WithdrawnQuestion


def _text(q: Question) -> str:
    return " ".join([q.topic, q.question, q.why])


def _closest(gap: DraftGap, questions: Sequence[Question]) -> Question:
    """Вопрос, ближайший по смыслу к пояснению пропуска; при равенстве первый (меньший номер)."""
    return max(questions, key=lambda q: fuzz.token_set_ratio(gap.missing, _text(q)))


def _link(f: Finding, active: Sequence[Question], withdrawn: Sequence[Question]) -> Finding:
    draft = f.test_draft
    if draft is None or not draft.gap_items:
        return f
    mine = [q for q in active if f.id in q.related_findings]
    dropped = [q for q in withdrawn if f.id in q.related_findings]
    items: list[DraftGap] = []
    for gap in draft.gap_items:
        if mine:
            items.append(gap.model_copy(update={"question_id": _closest(gap, mine).id}))
        elif dropped:
            items.append(
                gap.model_copy(update={"question_id": _closest(gap, dropped).id, "question_withdrawn": True})
            )
        else:
            items.append(gap)
    return f.model_copy(update={"test_draft": draft.model_copy(update={"gap_items": items})})


def link_gaps(
    ver: Verification, questions: Sequence[Question], withdrawn: Sequence[WithdrawnQuestion] = ()
) -> Verification:
    """Верификация с номерами вопросов в пропусках черновиков теста."""
    if not any(f.test_draft is not None and f.test_draft.gap_items for f in ver.findings):
        return ver
    dropped = [w.question for w in withdrawn]
    return ver.model_copy(update={"findings": [_link(f, questions, dropped) for f in ver.findings]})


__all__ = ["link_gaps"]
