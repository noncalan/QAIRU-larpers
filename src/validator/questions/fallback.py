"""Шаг 8, запасной путь: карточки из кандидатов кода без редактуры моделью.

Политика отказа (docs/plan.md, шаг 8): если редактор не ответил или его ответ не прошёл проверки,
раздел вопросов всё равно есть: по одной карточке на кандидата, с диагностикой в отчёте. Формулировки
шаблонные, но каждая карточка опирается на сверенные цитаты и связанные находки. Варианты ответа
здесь не догадки модели о бизнес-правилах: это прочтения двух мест документа или «принять предложенную
формулировку / оставить текст» со ссылкой на находку.
"""

from __future__ import annotations

from validator.models import Addressee, Finding, QuestionOption, Quote
from validator.questions.candidates import CLARIFY, Candidate
from validator.questions.cards import MAX_QUOTES, CardDraft
from validator.questions.scoring import SEVERITY_WEIGHT, draft_gaps, is_contradiction
from validator.report.fmt import short

QUOTE_CHARS = 120
"""Сколько символов цитаты показывать внутри шаблонного вопроса и варианта."""

IF_UNANSWERED = (
    "Команда выберет трактовку сама; если автор имел в виду другое, расхождение выяснится только на приёмке."
)


def _main(c: Candidate) -> Finding:
    """Главная находка кандидата: самая важная, затем первая по номеру."""
    return max(c.findings, key=lambda f: SEVERITY_WEIGHT[f.severity])


def _quote(q: Quote) -> str:
    return f"«{short(q.located or q.text, QUOTE_CHARS)}»"


def _two_places(f: Finding) -> tuple[Quote, Quote] | None:
    """Две стороны противоречия из самой находки: первая цитата и первая цитата из другого блока."""
    placed = [(q, q.anchor.block_ids) for q in f.quotes if q.verified and q.anchor and q.anchor.block_ids]
    if not placed:
        return None
    first, blocks = placed[0]
    other = next((q for q, b in placed[1:] if b != blocks), None)
    return (first, other) if other is not None else None


def _contradiction(c: Candidate) -> tuple[str, list[QuestionOption], tuple[Quote, ...]] | None:
    """Два расходящихся места: вопрос «какое верно» и по варианту на каждое место."""
    sides = next((p for f in c.findings if is_contradiction(f) and (p := _two_places(f))), None)
    if sides is None:
        return None
    first, second = sides
    question = f"Какое из двух мест документа задаёт верное правило: {_quote(first)} или {_quote(second)}?"
    options = [
        QuestionOption(
            label="A",
            text=f"Верно первое место: {_quote(first)}",
            change="Исправить второе место, чтобы оно совпадало с первым",
            grounded_in=first.located,
        ),
        QuestionOption(
            label="B",
            text=f"Верно второе место: {_quote(second)}",
            change="Исправить первое место, чтобы оно совпадало со вторым",
            grounded_in=second.located,
        ),
    ]
    rest = tuple(q for q in c.quotes if q not in (first, second))
    return question, options, (first, second, *rest)[:MAX_QUOTES]


def _question(c: Candidate) -> str:
    gaps = [g for f in c.findings for g in draft_gaps(f)]
    if c.placeholders:
        gap = "пропуски" if len(c.placeholders) > 1 else "пропуск"
        return f"Как заполнить {gap} в требовании: {'; '.join(c.placeholders)}?"
    if gaps:
        return f"Каких значений не хватает, чтобы написать тест: {'; '.join(gaps)}?"
    if c.quotes:
        return f"Как правильно понимать это место документа: {_quote(c.quotes[0])}?"
    return "Как правильно понимать это место документа?"


def _rewrite(c: Candidate, main: Finding) -> tuple[str, list[QuestionOption]]:
    """Неясное или неполное место: принять предложенную формулировку или зафиксировать текущую."""
    with_gaps = next((f for f in c.findings if f.suggestion and CLARIFY.search(f.suggestion)), None)
    source = with_gaps or next((f for f in c.findings if f.suggestion), None)
    if source is not None and source.suggestion:
        change = (
            "Заменить текст в документе и заполнить места [уточнить: …]"
            if with_gaps is not None
            else "Заменить текст в документе на предложенный"
        )
        first = QuestionOption(
            label="A",
            text=f"Принять предложенную формулировку: {' '.join(source.suggestion.split())}",
            change=change,
            grounded_in=source.id,
        )
    else:
        first = QuestionOption(
            label="A",
            text="Уточнить формулировку в документе",
            change="Переписать место так, чтобы у теста был один ожидаемый результат",
            grounded_in=main.id,
        )
    keep = QuestionOption(
        label="B",
        text="Оставить текущую формулировку",
        change="Дописать в документ, как её понимать, чтобы у теста был один ожидаемый результат",
        grounded_in=main.id,
    )
    return _question(c), [first, keep]


def draft_from_candidate(c: Candidate) -> CardDraft:
    """Шаблонная карточка по кандидату: цитаты и находки от кода, без догадок модели."""
    main = _main(c)
    contradiction = _contradiction(c)
    if contradiction is not None:
        question, options, quotes = contradiction
        addressee: tuple[Addressee, ...] = (Addressee.BUSINESS, Addressee.ANALYST)
    else:
        question, options = _rewrite(c, main)
        quotes, addressee = c.quotes[:MAX_QUOTES], (Addressee.ANALYST,)
    return CardDraft(
        finding_ids=c.finding_ids,
        origin=c.origin,
        addressee=addressee,
        topic=main.defect_type.label,
        quotes=quotes,
        question=question,
        why=" ".join(main.explanation.split()),
        options=tuple(options),
        if_unanswered=IF_UNANSWERED,
        candidate_ids=(c.cid,),
    )
