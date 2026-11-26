"""Значения полей находки из ответа прохода поиска (шаг 4): цитаты, ID блоков, свойство, тип, вердикт.

Разбор строки поля в значение контракта (``models.py``). Общий для разбора ответа кодом
(``search_parse.py``) и переноса в JSON моделью (``search_transfer.py``): метки модели переноса
проходят те же функции, что строки ответа прохода.
"""

from __future__ import annotations

import re

from validator.models import DefectType, Property, Scope, Severity, Verdict

BLOCK_ID = re.compile(r"(?<![\w.])[BВ](\d{3,}(?:\.\d+)?)(?:\s*[–—-]\s*[BВ]?(\d{3,}(?:\.\d+)?))?(?![\w])")
"""ID блока или предложения: B012, B012.3, диапазон B012–B015. Кириллическая «В» вместо B тоже годится."""
MAX_BLOCK_RANGE = 30
"""Диапазон «B075–B083» раскрывается в блоки, если в нём не больше 30 блоков; иначе берутся концы."""


def norm(text: str) -> str:
    """Нижний регистр, «ё» как «е», без звёздочек разметки и лишних пробелов."""
    return " ".join(text.lower().replace("ё", "е").replace("*", " ").split())


_OPENING_QUOTES = ("“", "„", '"')
_PAIRED_QUOTE = re.compile(r"[“„\"]([^“”„\"]+)[”“\"]")
_QUOTE_JOINERS = frozenset({"", "и", "или", "и/или", "плюс", "and", "or"})
"""Что стоит между двумя цитатами одного поля: «A» и «B»; всё остальное («— см. также «B»») пояснение."""
_JOINER_EDGES = " \t,;:.—–-/+"


def _guillemet_quotes(value: str) -> list[str]:
    """Цитаты в «ёлочках» верхнего уровня; после пояснения вроде «— см. также» новые цитаты не берутся."""
    found: list[str] = []
    depth, start, last_end = 0, 0, 0
    for i, ch in enumerate(value):
        if ch == "«":
            depth += 1
            if depth == 1:
                if found and norm(value[last_end:i]).strip(_JOINER_EDGES) not in _QUOTE_JOINERS:
                    break
                start = i + 1
        elif ch == "»" and depth:
            depth -= 1
            if depth == 0:
                found.append(value[start:i].strip())
                last_end = i + 1
    return found


def quotes_in(value: str) -> list[str]:
    """Цитаты поля: «A» и «B» дают две, вложенные «…» остаются внутри.

    Если значение начинается с “ „ или прямой кавычки, цитата это то, что внутри этих кавычек, а «ёлочки» в
    ней вложенные: иначе внутренний фрагмент подменил бы цитату. Без кавычек вокруг берётся значение целиком
    без кавычек по краям: в «Система обрабатывает "срочные" заявки» кавычки внутри цитаты, а не вокруг неё.
    """
    found: list[str] = []
    if value.lstrip().startswith(_OPENING_QUOTES):
        found = [m.group(1).strip() for m in _PAIRED_QUOTE.finditer(value)]
    found = [q for q in found or _guillemet_quotes(value) if q]
    if found:
        return found
    bare = value.strip().rstrip(";").strip().strip("\"'“”„`«» ")
    return [bare] if bare else []


def _expand(start: str, end: str | None) -> list[str]:
    """«B075–B083» → B075, B076, …, B083. Предложения и слишком длинные диапазоны не раскрываются."""
    ids = [start] + ([end] if end else [])
    if end is None or "." in start or "." in end:
        return ids
    first, last = int(start), int(end)
    if not 0 < last - first <= MAX_BLOCK_RANGE:
        return ids
    return [f"{n:0{len(start)}d}" for n in range(first, last + 1)]


def block_ids_in(value: str) -> list[str]:
    """ID блоков в порядке появления, без повторов; кириллическая «В» заменяется на латинскую B."""
    found = (n for m in BLOCK_ID.finditer(value) for n in _expand(m.group(1), m.group(2)))
    return list(dict.fromkeys(f"B{n}" for n in found))


def _first_match(
    text: str, table: tuple[tuple[str, Property, Property | None], ...]
) -> tuple[Property, Property | None] | None:
    best: tuple[int, Property, Property | None] | None = None
    for pattern, single, of_set in table:
        m = re.search(pattern, text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), single, of_set)
    return (best[1], best[2]) if best else None


_P = Property
# (образец, свойство требования, свойство набора). Если свойства набора нет, свойство одно на оба уровня.
_ENGLISH: tuple[tuple[str, Property, Property | None], ...] = (
    (r"necessar", _P.NECESSARY, None),
    (r"appropriat", _P.APPROPRIATE, None),
    (r"ambigu", _P.UNAMBIGUOUS, None),
    (r"singular", _P.SINGULAR, None),
    (r"verifiab", _P.VERIFIABLE, None),
    (r"conform", _P.CONFORMING, None),
    (r"\bcomplete", _P.COMPLETE, _P.SET_COMPLETE),
    (r"feasib", _P.FEASIBLE, _P.SET_FEASIBLE),
    (r"\bcorrect", _P.CORRECT, None),
    (r"consisten", _P.SET_CONSISTENT, None),
    (r"comprehensib", _P.SET_COMPREHENSIBLE, None),
    (r"validat", _P.SET_VALIDATABLE, None),
    (r"modifiab", _P.SET_MODIFIABLE, None),
    (r"traceab", _P.TRACEABLE, _P.SET_TRACEABLE),
    (r"\brank", _P.RANKED, _P.SET_RANKED),
)
_RUSSIAN: tuple[tuple[str, Property, Property | None], ...] = (
    (r"необходим", _P.NECESSARY, None),
    (r"уместн", _P.APPROPRIATE, None),
    (r"однозначн", _P.UNAMBIGUOUS, None),
    (r"единичн|атомарн", _P.SINGULAR, None),
    (r"проверяем", _P.VERIFIABLE, None),
    (r"шаблон", _P.CONFORMING, None),
    (r"полнот", _P.COMPLETE, _P.SET_COMPLETE),
    (r"выполним", _P.FEASIBLE, _P.SET_FEASIBLE),
    (r"корректн", _P.CORRECT, None),
    (r"согласованн|непротиворечив", _P.SET_CONSISTENT, None),
    (r"понятн", _P.SET_COMPREHENSIBLE, None),
    (r"валидир", _P.SET_VALIDATABLE, None),
    (r"модифицир", _P.SET_MODIFIABLE, None),
    (r"прослежив", _P.TRACEABLE, _P.SET_TRACEABLE),
    (r"ранжир|приоритет", _P.RANKED, _P.SET_RANKED),
)
_IEEE_CLAUSE: dict[str, tuple[Property, Property | None]] = {
    "1": (_P.CORRECT, None),
    "2": (_P.UNAMBIGUOUS, None),
    "3": (_P.COMPLETE, _P.SET_COMPLETE),
    "4": (_P.SET_CONSISTENT, None),
    "5": (_P.RANKED, _P.SET_RANKED),
    "6": (_P.VERIFIABLE, None),
    "7": (_P.SET_MODIFIABLE, None),
    "8": (_P.TRACEABLE, _P.SET_TRACEABLE),
}


def _property_level(value: str, section: Scope | None) -> Scope:
    """Уровень свойства, которое есть и у требования, и у набора: Complete, Feasible, Traceable, Ranked."""
    low = norm(value)
    if "набор" in low:
        return Scope.SET
    individual, of_set = "5.2.5" in low, "5.2.6" in low
    if of_set and not individual:
        return Scope.SET
    if individual and not of_set:
        return Scope.REQUIREMENT
    return section or Scope.REQUIREMENT


def parse_property(value: str, section: Scope | None = None) -> Property | None:
    """Свойство из строки «Свойство: Verifiable (проверяемость), ISO/IEC/IEEE 29148:2018, п. 5.2.5».

    Порядок: английское название (первое по тексту), русское, пункт IEEE 830-1998 «4.3.x».
    Уровень (требование или набор) для общих названий берётся из пункта 5.2.5 или 5.2.6, слова
    «набора» или подраздела ответа.
    """
    low = norm(value)
    found = _first_match(low, _ENGLISH) or _first_match(low, _RUSSIAN)
    if found is None:
        clause = re.search(r"4\.3\.(\d)", low)
        found = _IEEE_CLAUSE.get(clause.group(1)) if clause else None
    if found is None:
        return None
    single, of_set = found
    if of_set is not None and _property_level(value, section) is Scope.SET:
        return of_set
    return single


_DEFECT_STEMS: tuple[tuple[str, DefectType], ...] = (
    ("противореч", DefectType.CONTRADICTION),
    ("двусмысл", DefectType.AMBIGUOUS),
    ("неоднознач", DefectType.AMBIGUOUS),
    ("незаполн", DefectType.PLACEHOLDER),
    ("неполн", DefectType.INCOMPLETE),
    ("непровер", DefectType.UNVERIFIABLE),
    ("невыполн", DefectType.INFEASIBLE),
    ("некоррект", DefectType.INCORRECT),
    ("разнобой", DefectType.TERMINOLOGY),
    ("дублир", DefectType.REDUNDANCY),
    ("составн", DefectType.NOT_ATOMIC),
    ("неуместн", DefectType.DESIGN_DETAIL),
    ("прослежив", DefectType.TRACEABILITY),
    ("оформлен", DefectType.TRACEABILITY),
    ("посторон", DefectType.INJECTED_INSTRUCTION),
)


def parse_defect_type(value: str) -> DefectType | None:
    """Тип дефекта из закрытого списка промпта: точная метка, метка внутри строки, затем основа слова."""
    low = norm(value).strip(" .,;:«»\"'`")
    exact = DefectType.from_label(low)
    if exact is not None:
        return exact
    for kind in sorted(DefectType, key=lambda k: -len(k.label)):
        if norm(kind.label) in low:
            return kind
    hits = [(low.find(stem), kind) for stem, kind in _DEFECT_STEMS if stem in low]
    return min(hits, key=lambda h: h[0])[1] if hits else None


def parse_verdict(value: str, heading: str) -> Verdict:
    """Вердикт; если строки нет или она не разобрана, «неясно» при заголовке «Неясно…», иначе «нарушение»."""
    low = norm(value)
    if low:
        word = Verdict.from_label(low.split()[0].strip(" .,;:"))
        if word is not None:
            return word
        if "неясн" in low:
            return Verdict.UNCLEAR
        if "наруш" in low:
            return Verdict.VIOLATION
    return Verdict.UNCLEAR if norm(heading).startswith("неясн") else Verdict.VIOLATION


def parse_severity(value: str) -> Severity | None:
    low = norm(value)
    if not low:
        return None
    word = Severity.from_label(low.split()[0].strip(" .,;:"))
    if word is not None:
        return word
    for stem, severity in (
        ("критич", Severity.CRITICAL),
        ("средн", Severity.MAJOR),
        ("низк", Severity.MINOR),
    ):
        if stem in low:
            return severity
    return None


_BLOCKS_TEST = re.compile(r"^(?:да|мешает)\b[\s,:;.—–-]*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_test(value: str) -> tuple[bool, str | None]:
    """«Тест: мешает, <чего не хватает>» → (True, чего не хватает); «не мешает» → (False, None)."""
    text = value.strip().strip("*").strip()
    low = norm(text)
    if not low or low.startswith("не мешает"):
        return False, None
    m = _BLOCKS_TEST.match(text)
    if m:
        return True, m.group(1).strip() or None
    if "не мешает" in low or "мешает" not in low:
        return False, None
    return True, text


def unwrap(value: str) -> str:
    """Снимает внешние ёлочки, если значение целиком одна цитата: «Текст» → Текст."""
    text = value.strip()
    inner = quotes_in(text)
    if len(inner) == 1 and text.startswith("«") and text.endswith("»") and text[1:-1].strip() == inner[0]:
        return inner[0]
    return text


def clean(value: str) -> str | None:
    text = value.strip().strip("*").strip()
    return text or None


__all__ = [
    "BLOCK_ID",
    "block_ids_in",
    "clean",
    "norm",
    "parse_defect_type",
    "parse_property",
    "parse_severity",
    "parse_test",
    "parse_verdict",
    "quotes_in",
    "unwrap",
]
