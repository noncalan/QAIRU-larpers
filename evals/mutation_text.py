"""Разбор Markdown на строки и предложения, правки с учётом смещений: основа мутаций (#50).

Ничего не знает о проверяемом документе: только форма текста (заголовок, пункт списка, строка таблицы).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

# --- Свойства стандарта (коды как в validator.models.Property) ---------------------------------

UNAMB = "IND-UNAMB"
VERIF = "IND-VERIF"
COMPL = "IND-COMPL"
CORR = "IND-CORR"
SING = "IND-SING"
APPR = "IND-APPR"
TRACE = "IND-TRACE"
SET_CONS = "SET-CONS"
SET_COMPL = "SET-COMPL"


@dataclass(frozen=True)
class OperatorInfo:
    code: str
    name: str
    expected: tuple[str, ...]
    """Свойства, при которых находка на месте мутации считается пойманной строго."""


OPERATORS: dict[str, OperatorInfo] = {
    "M1": OperatorInfo("M1", "Размыть число", (UNAMB, VERIF)),
    "M2": OperatorInfo("M2", "Убрать единицу или границу", (UNAMB, VERIF, COMPL)),
    "M3": OperatorInfo("M3", "Второе значение параметра", (SET_CONS, CORR)),
    "M4": OperatorInfo("M4", "Висячая ссылка", (COMPL, CORR, TRACE, SET_COMPL)),
    "M5": OperatorInfo("M5", "Удалить ветку ошибки", (COMPL, SET_COMPL)),
    "M6": OperatorInfo("M6", "Склеить требования", (SING,)),
    "M7": OperatorInfo("M7", "Решение вместо требования", (APPR,)),
    "M8": OperatorInfo("M8", "Непроверяемая формулировка", (VERIF, UNAMB)),
    "M9": OperatorInfo("M9", "Безличное «обеспечивается»", (UNAMB,)),
    "M10": OperatorInfo("M10", "Заглушка", (COMPL, SET_COMPL)),
}

NEUTRAL: dict[str, str] = {
    "N1": "Поменять местами соседние разделы",
    "N2": "Перенумеровать нумерованные списки",
    "N3": "Таблицу из двух столбцов превратить в список",
}

# --- Разбор документа на строки -----------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_LIST = re.compile(r"^(\s*)(?:[*+-]|\d+[.)])\s+")
_LIST_LABEL = re.compile(r"^(\s*)(?:[*+-]|\d+[.)])\s+(?:\*\*[^*\n]{1,40}\*\*\s*)?")
_ORDERED = re.compile(r"^(\s*)(\d+)([.)])(\s+)")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


@dataclass(frozen=True)
class Line:
    start: int
    end: int  # без перевода строки
    text: str
    kind: str  # heading, list, table, table_head, table_sep, para, code, blank, hr
    section: str  # путь заголовков до уровня 3: ключ «раздела» для непересекающихся мутаций
    heading_level: int = 0


SECTION_DEPTH = 3


def parse(text: str, depth: int = SECTION_DEPTH) -> list[Line]:
    """Строки документа. ``depth``: до какого уровня заголовков считать раздел (ключ ``section``)."""
    lines: list[Line] = []
    path: dict[int, str] = {}
    in_code = False
    pos = 0
    prev_kind = "blank"
    for raw in text.split("\n"):
        start, end = pos, pos + len(raw)
        pos = end + 1
        level = 0
        if _FENCE.match(raw):
            in_code = not in_code
            kind = "code"
        elif in_code:
            kind = "code"
        elif not raw.strip():
            kind = "blank"
        elif m := _HEADING.match(raw):
            level = len(m.group(1))
            path = {k: v for k, v in path.items() if k < level}
            path[level] = m.group(2)
            kind = "heading"
        elif _HR.match(raw):
            kind = "hr"
        elif raw.lstrip().startswith("|"):
            if _TABLE_SEP.match(raw):
                kind = "table_sep"
            elif prev_kind not in {"table", "table_head", "table_sep"}:
                kind = "table_head"
            else:
                kind = "table"
        elif _LIST.match(raw):
            kind = "list"
        else:
            kind = "para"
        section = " / ".join(path[k] for k in sorted(path) if k <= depth)
        lines.append(Line(start, end, raw, kind, section, level))
        prev_kind = kind
    return lines


PROSE = {"list", "para"}

# --- Предложения ------------------------------------------------------------------------------

_SENT_END = re.compile(r"(?<=[.!?…])\s+(?=[«\"(\[*]*[A-ZА-ЯЁ0-9])")


def sentences(text: str, line: Line) -> list[tuple[int, int]]:
    """Предложения строки прозы: абсолютные смещения, без маркера списка и метки «**…**»."""
    body_start = line.start
    if line.kind == "list" and (m := _LIST_LABEL.match(line.text)):
        body_start += m.end()
    body = text[body_start : line.end]
    spans: list[tuple[int, int]] = []
    cursor = 0
    for m in _SENT_END.finditer(body):
        spans.append((cursor, m.start()))
        cursor = m.end()
    spans.append((cursor, len(body)))
    out = []
    for a, b in spans:
        chunk = body[a:b]
        lead = len(chunk) - len(chunk.lstrip())
        trail = len(chunk.rstrip())
        if trail > lead:
            out.append((body_start + a + lead, body_start + a + trail))
    return out


def sentence_at(text: str, line: Line, offset: int) -> tuple[int, int]:
    for a, b in sentences(text, line):
        if a <= offset <= b:
            return a, b
    return line.start, line.end


# --- Правка и учёт смещений --------------------------------------------------------------------

Span = tuple[int, int]


@dataclass
class Edit:
    """Замена ``text[start:end]`` на ``new``. Смещения в тексте до правки."""

    operator: str
    start: int
    end: int
    new: str
    section: str
    sections_used: tuple[str, ...]
    context: list[Span] = field(default_factory=list)
    """Места рядом с правкой (до правки), которые тоже засчитываются как место мутации."""
    note: str = ""
    match_new: bool = True
    """Засчитывать ли сам новый фрагмент (для удалений его нет)."""
    tier: int = 0
    """Предпочтение места: из свободных берём кандидатов с наименьшим ``tier``."""


@dataclass
class Mutation:
    id: str
    operator: str
    name: str
    expected_properties: list[str]
    section: str
    original: str
    replacement: str
    start: int
    end: int
    line: int
    match_spans: list[list[int]]
    note: str


def _shift(span: Span, s: int, e: int, delta: int) -> Span:
    a, b = span
    if b <= s and not (a == b == s):
        return span
    if a >= e:
        return a + delta, b + delta
    if a <= s and b >= e:
        return a, b + delta
    raise ValueError(f"пересечение мест мутаций: {span} и правка {s}:{e}")


@dataclass
class _Applied:
    edit: Edit
    original: str
    new_span: Span
    context: list[Span]


def apply_edits(text: str, edits: Sequence[Edit]) -> tuple[str, list[_Applied]]:
    """Последовательно применяет правки (каждая в координатах текста после предыдущих)."""
    applied: list[_Applied] = []
    for edit in edits:
        s, e = edit.start, edit.end
        delta = len(edit.new) - (e - s)
        original = text[s:e]
        text = text[:s] + edit.new + text[e:]
        for item in applied:
            item.new_span = _shift(item.new_span, s, e, delta)
            item.context = [_shift(c, s, e, delta) for c in item.context]
        context = [_shift(c, s, e, delta) for c in edit.context]
        applied.append(_Applied(edit, original, (s, s + len(edit.new)), context))
    return text, applied


def _line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def to_mutations(text: str, applied: Sequence[_Applied], prefix: str) -> list[Mutation]:
    out = []
    for i, item in enumerate(applied, start=1):
        info = OPERATORS[item.edit.operator]
        spans: list[Span] = []
        if item.edit.match_new and item.new_span[1] > item.new_span[0]:
            spans.append(item.new_span)
        spans.extend(c for c in item.context if c[1] > c[0])
        out.append(
            Mutation(
                id=f"{prefix}-{i:02d}",
                operator=info.code,
                name=info.name,
                expected_properties=list(info.expected),
                section=item.edit.section,
                original=item.original,
                replacement=item.edit.new,
                start=item.new_span[0],
                end=item.new_span[1],
                line=_line_no(text, item.new_span[0]),
                match_spans=[list(s) for s in spans],
                note=item.edit.note,
            )
        )
    return out
