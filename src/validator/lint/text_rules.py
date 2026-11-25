"""Правила внутри одного предложения или ячейки: L-01…L-06 и L-10.

Каждая проверка получает единицу (``Unit``) и возвращает срабатывание или ``None``. На единицу правило
даёт не больше одной находки: если в предложении два размытых слова, это одна находка с обоими словами.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from validator.lint.hits import Hit
from validator.lint.lexicon import Match, find_terms
from validator.lint.rules import RULE_BY_ID, Rule
from validator.lint.units import Unit, has_number, quote_span

UnitCheck = Callable[[Unit, str], Hit | None]
"""Проверка единицы: ``(единица, весь текст документа) -> срабатывание``."""

MIN_OBLIGATIONS = 2
"""L-10: столько глаголов обязательства в одном предложении делают его кандидатом на составное требование."""

_OBLIGATION = re.compile(r"(?<!\w)(?:долж(?:ен|на|но|ны)|обязан(?:а|о|ы)?|shall|must)(?!\w)")
_CONSTRAINT = (
    r"(?:не\s+(?:более|менее|позднее|ранее|превышает|превышать|выше|ниже|чаще|реже)"
    r"|в\s+течение|в\s+пределах|до|свыше|менее|более|within|no\s+more\s+than|no\s+less\s+than"
    r"|at\s+most|at\s+least|not\s+(?:exceed|more\s+than|less\s+than)|up\s+to|less\s+than|more\s+than)"
)
_NUMBER = r"\d+(?:[\s\u00a0\u202f]\d{3})*(?:[.,]\d+)?"
_LIMIT = re.compile(rf"(?<!\w)({_CONSTRAINT})\s+({_NUMBER})", re.IGNORECASE)
_TAIL_STOP = frozenset('.,;:!?)]}»"|')
_TAIL_WORD = re.compile(r"[^\W\d_]+")
_FUNCTION_WORDS = frozenset(
    [
        "и",
        "или",
        "но",
        "при",
        "для",
        "если",
        "когда",
        "после",
        "перед",
        "на",
        "по",
        "за",
        "от",
        "из",
        "как",
        "что",
        "чтобы",
        "то",
        "and",
        "or",
        "per",
        "for",
        "when",
        "if",
        "after",
        "before",
        "in",
        "on",
        "of",
        "at",
        "but",
    ]
)
"""После этих слов единицы нет: «не более 5 и менее 2», «до 3 при отказе». Существительное это единица."""


def _terms(unit: Unit, section: str, *, masked: bool = True) -> list[Match]:
    return find_terms(unit.masked if masked else unit.folded, section)


def _quoted_words(matches: list[Match]) -> str:
    return ", ".join(dict.fromkeys(f"«{m.text}»" for m in matches))


def _span(doc_text: str, unit: Unit, matches: list[Match]) -> tuple[int, int]:
    """Цитата: от первого до последнего найденного слова, если вместе они укладываются в окно."""
    first, last = matches[0], matches[-1]
    a, b = unit.start + first.start, unit.start + last.end
    lo, hi = quote_span(doc_text, unit, a, b)
    if lo <= a and b <= hi:
        return lo, hi
    return quote_span(doc_text, unit, a, unit.start + first.end)


def _hit(rule: Rule, span: tuple[int, int], title: str, explanation: str, suggestion: str) -> Hit:
    return Hit(rule=rule, spans=(span,), title=title, explanation=explanation, suggestion=suggestion)


def check_vague(unit: Unit, doc_text: str) -> Hit | None:
    """L-01: слово из словаря размытых слов; если в предложении есть число, мера, вероятно, уже задана."""
    if has_number(unit.text):
        return None
    matches = _terms(unit, "vague_terms")
    if not matches:
        return None
    reasons = "; ".join(dict.fromkeys(m.term.why for m in matches))
    words = _quoted_words(matches)
    return _hit(
        RULE_BY_ID["L-01"],
        _span(doc_text, unit, matches),
        f"Размытое слово {words}",
        f"Слово {words} не задаёт меры: {reasons}. Разработчик и тестировщик поймут его по-разному и не "
        "смогут написать однозначную проверку.",
        f"Заменить {words} измеримым критерием: [уточнить: значение и единица измерения или сценарий].",
    )


def check_loophole(unit: Unit, doc_text: str) -> Hit | None:
    """L-02: оборот, который оставляет условие или обязательность на усмотрение исполнителя."""
    matches = _terms(unit, "loopholes")
    if not matches:
        return None
    reasons = "; ".join(dict.fromkeys(m.term.why for m in matches))
    words = _quoted_words(matches)
    return _hit(
        RULE_BY_ID["L-02"],
        _span(doc_text, unit, matches),
        f"Лазейка {words}",
        f"Оборот {words} оставляет условие без критерия: {reasons}. Тест не знает, при каких входных "
        "данных результат обязателен.",
        "Назвать условие явно: [уточнить: когда именно, по какому признаку и кто решает].",
    )


def check_open_list(unit: Unit, doc_text: str) -> Hit | None:
    """L-03: перечень обрывается словами «и т. д.», «и так далее» или «не ограничиваясь»."""
    matches = _terms(unit, "open_lists")
    if not matches:
        return None
    words = _quoted_words(matches)
    return _hit(
        RULE_BY_ID["L-03"],
        _span(doc_text, unit, matches),
        f"Открытый перечень {words}",
        f"Перечень заканчивается оборотом {words}: {matches[0].term.why}. Разработчик не знает, что "
        "реализовать сверх названного, а тестировщик не знает, что считать выходом за перечень.",
        "Привести закрытый перечень или сослаться на справочник: [уточнить: полный список значений].",
    )


def check_and_or(unit: Unit, doc_text: str) -> Hit | None:
    """L-04: «и/или»."""
    matches = _terms(unit, "and_or")
    if not matches:
        return None
    words = _quoted_words(matches)
    return _hit(
        RULE_BY_ID["L-04"],
        _span(doc_text, unit, matches),
        f"Неоднозначное {words}",
        f"Оборот {words} допускает три чтения (только первое, только второе, оба сразу), и для каждого "
        "нужен свой тест. Из текста не видно, какое имеется в виду.",
        "Выбрать одно чтение: «или» (одно из двух), «и» (оба) или перечислить допустимые сочетания.",
    )


def check_marker(unit: Unit, doc_text: str) -> Hit | None:
    """L-05: маркер недописанного (ищем и внутри кавычек)."""
    matches = _terms(unit, "markers", masked=False)
    if not matches:
        return None
    words = _quoted_words(matches)
    return _hit(
        RULE_BY_ID["L-05"],
        _span(doc_text, unit, matches),
        f"Незаполненное место {words}",
        f"В требовании остался маркер {words}: {matches[0].term.why}. Пока значение не задано, "
        "требование нельзя ни реализовать, ни проверить.",
        "Заполнить значение или явно записать открытый вопрос с ответственным и сроком.",
    )


def _unit_follows(tail: str) -> bool:
    """После числа идёт единица или название того, что считаем (не конец фразы и не служебное слово)."""
    tail = tail.lstrip()
    if not tail:
        return False
    if tail[0] in _TAIL_STOP:
        return False
    word = _TAIL_WORD.match(tail)
    if word is None:
        return True  # %, ₸, $, °: символ единицы
    return word.group(0).lower() not in _FUNCTION_WORDS


def check_unitless(unit: Unit, doc_text: str) -> Hit | None:
    """L-06: «не более 5», «в течение 30» без единицы измерения."""
    found: list[re.Match[str]] = []
    for m in _LIMIT.finditer(unit.masked):
        tail = unit.masked[m.end() :]
        if re.match(
            r"\s*[:./\-–‑]\d", tail
        ):  # время 15:00, дата 15.03, диапазон 10-20: единица может быть дальше
            continue
        if not _unit_follows(tail):
            found.append(m)
    if not found:
        return None
    first, last = found[0], found[-1]
    a, b = unit.start + first.start(), unit.start + last.end()
    lo, hi = quote_span(doc_text, unit, a, b)
    phrases = ", ".join(dict.fromkeys(f"«{' '.join(m.group(0).split())}»" for m in found))
    return _hit(
        RULE_BY_ID["L-06"],
        (lo, hi),
        f"Число без единицы {phrases}",
        f"После предела {phrases} не названа единица измерения или то, что считаем. Тестировщик не "
        "знает, секунды это, минуты или штуки, и не сможет проверить границу.",
        "Дописать единицу: [уточнить: единица измерения, например секунды, штуки, проценты].",
    )


def check_many_obligations(unit: Unit, doc_text: str) -> Hit | None:
    """L-10: в предложении два и больше глагола обязательства."""
    matches = list(_OBLIGATION.finditer(unit.masked))
    if len(matches) < MIN_OBLIGATIONS:
        return None
    a, b = unit.start + matches[0].start(), unit.start + matches[-1].end()
    span = quote_span(doc_text, unit, a, b)
    return _hit(
        RULE_BY_ID["L-10"],
        span,
        f"Несколько обязательств в одном предложении ({len(matches)})",
        f"В одном предложении {len(matches)} раза стоит глагол обязательства: это кандидат на составное "
        "требование. Если сработает только часть, неясно, выполнено требование или нет, а один тест "
        "проверяет сразу несколько вещей.",
        "Разрезать на отдельные требования, по одному обязательству в каждом, с собственным ID.",
    )


UNIT_CHECKS: dict[str, UnitCheck] = {
    "L-01": check_vague,
    "L-02": check_loophole,
    "L-03": check_open_list,
    "L-04": check_and_or,
    "L-05": check_marker,
    "L-06": check_unitless,
    "L-10": check_many_obligations,
}
