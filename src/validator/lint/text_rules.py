"""Правила внутри одного предложения или ячейки: L-01…L-06 и L-10.

Каждая проверка получает единицу (``Unit``) и возвращает срабатывание или ``None``. На единицу правило
даёт не больше одной находки: если в предложении два размытых слова, это одна находка с обоими словами.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from validator.lint.hits import Hit
from validator.lint.lexicon import Match, find_terms, fold
from validator.lint.rules import RULE_BY_ID, Rule
from validator.lint.units import Unit, has_number, mask_quoted, quote_span

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

_COUNTED = re.compile(r"(?<!\w)(?:(в\s+том\s+числе)|количеств\w*|кол-?во|число|number\s+of|count\s+of)(?!\w)")
"""L-06: «количество заявок — не более 40», «в количестве не менее 12»: что считаем, названо до предела,
число означает штуки. «В том числе» (группа 1) это «включая», а не счёт."""
_BY_NUMBER = re.compile(rf"\s*(?:на|x|х|×|by)\s*{_NUMBER}", re.IGNORECASE)
"""L-06: размер «1920 на 1080 точек», «20 × 30 см»: единица стоит после последнего числа."""
_NO_LETTERS = re.compile(r"[\W\d_]*")
_USER_ACTOR = re.compile(
    r"\s+(?:of\s+(?:the\s+|an?\s+)?)?(?:пользовател|оператор|администратор|user|operator|administrator)",
    re.IGNORECASE,
)
"""L-02: «по усмотрению пользователя»: решающий назван, и это пользователь системы, а не исполнитель."""


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


def _is_loophole(unit: Unit, match: Match) -> bool:
    """Оборот оставляет решение исполнителю, а не объявляет пожелание и не отдаёт выбор пользователю.

    Слово предпочтения в начале фразы («Рекомендуется ежедневно …») делает всю фразу пожеланием:
    по ISO/IEC/IEEE 29148, п. 5.2.4 это явно необязательное положение, а не требование с лазейкой.
    Оборот «чьё решение» с пользователем следом («по усмотрению пользователя») называет решающего.
    """
    if match.term.role == "preference":
        return _NO_LETTERS.fullmatch(unit.masked, 0, match.start) is None
    if match.term.role == "discretion":
        return _USER_ACTOR.match(unit.masked, match.end) is None
    return True


def check_loophole(unit: Unit, doc_text: str) -> Hit | None:
    """L-02: оборот, который оставляет условие или обязательность на усмотрение исполнителя."""
    matches = [m for m in _terms(unit, "loopholes") if _is_loophole(unit, m)]
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


def _continuation(unit: Unit, doc_text: str) -> str:
    """Продолжение требования после единицы, если оно не начинается новой фразой (перенос страницы PDF)."""
    if unit.context_end <= unit.end:
        return ""
    rest = doc_text[unit.end : unit.context_end].lstrip()
    if not rest or (rest[:1].isupper() and rest[1:2].islower()):
        return ""  # «Система …»: новая фраза, а не единица («МБ», «точек»)
    return fold(mask_quoted(rest))


def _counted_before(text: str, lo: int, hi: int) -> bool:
    """Между прошлым пределом и этим назван счёт («количество …»): число означает штуки названного."""
    return any(m.group(1) is None for m in _COUNTED.finditer(text, lo, hi))


def _unitless_after(unit: Unit, doc_text: str, end: int) -> bool:
    """После числа предела, кончающегося на ``end``, нет единицы."""
    tail = unit.masked[end:]
    while by := _BY_NUMBER.match(tail):  # размер «N на M точек»: смотрим после последнего числа
        tail = tail[by.end() :]
    if re.match(
        r"\s*[:./\-–‑]\d", tail
    ):  # время 15:00, дата 15.03, диапазон 10-20: единица может быть дальше
        return False
    if not tail.strip():  # единица фразы кончилась на числе: продолжение может быть на следующей странице
        tail = _continuation(unit, doc_text)
        if tail and _TAIL_WORD.match(tail) is None:
            return True  # продолжение без слова («— пункт»): единицу не нашли
    return not _unit_follows(tail)


def check_unitless(unit: Unit, doc_text: str) -> Hit | None:
    """L-06: «не более 5», «в течение 30» без единицы измерения."""
    found: list[re.Match[str]] = []
    previous_end = 0
    for m in _LIMIT.finditer(unit.masked):
        counted = _counted_before(unit.masked, previous_end, m.start())
        previous_end = m.end()
        if not counted and _unitless_after(unit, doc_text, m.end()):
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
