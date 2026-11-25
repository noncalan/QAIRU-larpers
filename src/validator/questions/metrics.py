"""Метрики вопросов автору (исследование 17, п. 4.3): считает код, 0 вызовов модели.

Одна функция для отчёта (раздел «Метрики прогона», ``RunMetrics.questions``) и для eval
(eval пересчитывает их по ``report.json`` той же функцией, до и после критика).

- **закрытость**: 2–4 различающихся варианта, у каждого непустое «что поменять»;
- **сценарий** и **ошибки формулировки**: по отзыву критика (``Question.review``); вопросы без отзыва
  в знаменатель не идут, а если отзывов нет совсем, доля ``None``;
- **дубли с находками**: критик считает правку однозначной, значит хватило бы находки с правкой;
- **повторы**: пары вопросов с почти одинаковой формулировкой или с теми же местами документа
  (та же граница сходства, что у слияния карточек в ``cards.py``);
- **снято самоответом**: доля снятых критиком от всех вопросов до критика.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from itertools import combinations

from rapidfuzz import fuzz

from validator.models import Question, QuestionMetrics, WithdrawnQuestion
from validator.questions.cards import DUPLICATE_SIMILARITY, MAX_OPTIONS, MIN_OPTIONS
from validator.questions.quotes import span
from validator.text import normalize

ROUND = 4


def _share(hits: int, total: int) -> float | None:
    return round(hits / total, ROUND) if total else None


def is_closed(q: Question) -> bool:
    """2–4 различающихся варианта, и у каждого есть последствие («что поменять»)."""
    texts = {normalize(o.text)[0].lower() for o in q.options}
    return (
        MIN_OPTIONS <= len(q.options) <= MAX_OPTIONS
        and len(texts) == len(q.options)
        and all(o.change.strip() for o in q.options)
    )


def _places(q: Question) -> frozenset[tuple[int, int]]:
    return frozenset(s for s in (span(x) for x in q.quotes) if s is not None)


def is_duplicate(a: Question, b: Question) -> bool:
    text_a, text_b = normalize(a.question)[0].lower(), normalize(b.question)[0].lower()
    if fuzz.token_sort_ratio(text_a, text_b) >= DUPLICATE_SIMILARITY:
        return True
    places = _places(a)
    return bool(places) and places == _places(b)


def question_metrics(
    questions: Sequence[Question], withdrawn: Sequence[WithdrawnQuestion] = ()
) -> QuestionMetrics:
    """Метрики вопросов в отчёте; ``withdrawn`` даёт счётчики «до критика» и «снято самоответом»."""
    before = len(questions) + len(withdrawn)
    reviews = [q.review for q in questions if q.review is not None]
    scenario = [r.scenario for r in reviews if r.scenario is not None]
    reviewed_before = len(reviews) + sum(w.question.review is not None for w in withdrawn)
    return QuestionMetrics(
        total=len(questions),
        before_critic=before,
        withdrawn=len(withdrawn),
        withdrawn_share=_share(len(withdrawn), before) if reviewed_before else None,
        reviewed=reviewed_before,
        with_scenario=_share(sum(scenario), len(scenario)),
        closed=_share(sum(is_closed(q) for q in questions), len(questions)),
        without_errors=_share(sum(not r.errors for r in reviews), len(reviews)),
        obvious_fix=sum(r.obvious_fix for r in reviews),
        common_sense=sum(r.common_sense for r in reviews),
        duplicate_pairs=sum(is_duplicate(a, b) for a, b in combinations(questions, 2)),
        by_priority=dict(sorted(Counter(q.priority.value for q in questions).items())),
        by_addressee=dict(sorted(Counter(a.value for q in questions for a in q.addressee).items())),
    )
