"""Единицы проверки: предложения и ячейки внутри требований, цитаты и якоря для находок.

Правило срабатывает только внутри требований из нарезки: заголовки, код, сноски и колонтитулы пропускаем.
Требование режем на единицы (предложение абзаца или пункта, ячейка строки таблицы), чтобы цитата и
якорь были точными, а проверка «число рядом» смотрела на то же предложение.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass
from functools import cached_property

from validator.lint.lexicon import fold
from validator.models import Block, BlockKind, ParsedDocument, Segmentation

MAX_QUOTE = 200
"""Длиннее этого цитату сужаем до окна вокруг найденного: длинная цитата хуже читается и чаще не сходится."""

_SCANNED_KINDS = frozenset({BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.TABLE_ROW})
_QUOTED = re.compile(r"«[^»\n]{0,160}»|“[^”\n]{0,160}”|\"[^\"\n]{0,160}\"|`[^`\n]+`")
_LIST_MARKER = re.compile(r"(?:[-*+•]|\d+[.)])\s+")
_EDGE = " \t\r\n*_|#>`"
_NUMBER = re.compile(r"(?<![\w\-‑.])\d")
_LEADING_NUMBER = re.compile(r"^[\s*_#>|-]*\d+(?:\.\d+)*[.)]?\s+")
_SECTION_REF = re.compile(
    r"(?:п\.|пп\.|пункт\w*|раздел\w*|глав\w*|section|clause|§)\s*\d+(?:\.\d+)*", re.IGNORECASE
)


def mask_quoted(text: str) -> str:
    """Заменяет пробелами всё, что в кавычках «…», "…" или в обратных кавычках: смещения не меняются."""
    return _QUOTED.sub(lambda m: " " * len(m.group(0)), text)


def has_number(text: str) -> bool:
    """Есть ли в тексте число; не считаем цифры в метках («АБ-1», «FR12»), номер пункта в начале
    («3.2.1 Система…») и номера в ссылках («п. 3.2», «раздел 4»)."""
    text = _SECTION_REF.sub(" ", _LEADING_NUMBER.sub("", text, count=1))
    return _NUMBER.search(text) is not None


@dataclass(frozen=True)
class Unit:
    """Предложение или ячейка внутри требования: ``text`` это ``doc.text[start:end]``."""

    start: int
    end: int
    text: str
    block_id: str
    context_end: int = -1
    """Конец требования, в котором лежит единица (``-1``: не задан). Предложение PDF бывает разрезано на
    два блока переносом страницы: продолжение того же требования лежит в ``doc.text[end:context_end]``."""

    @cached_property
    def folded(self) -> str:
        return fold(self.text)

    @cached_property
    def masked(self) -> str:
        """Свёрнутый текст без слов в кавычках и коде."""
        return fold(mask_quoted(self.text))


def build_units(doc: ParsedDocument, seg: Segmentation) -> list[Unit]:
    """Единицы всех требований нарезки по порядку документа, без повторов."""
    ranges = sorted({(r.anchor.char_start, r.anchor.char_end) for r in seg.requirements})
    blocks = sorted(doc.blocks, key=lambda b: b.anchor.char_start)
    ends = [b.anchor.char_end for b in blocks]  # блоки идут подряд и не пересекаются: концы не убывают
    units: dict[tuple[int, int], Unit] = {}
    for lo, hi in ranges:
        for block in blocks[bisect_right(ends, lo) :]:
            if block.anchor.char_start >= hi:
                break
            if block.kind not in _SCANNED_KINDS:
                continue
            for start, end in _pieces(block):
                a, b = max(start, lo), min(end, hi)
                if b > a and (a, b) not in units:
                    units[(a, b)] = Unit(a, b, doc.text[a:b], block.id, context_end=hi)
    return [units[key] for key in sorted(units)]


def _pieces(block: Block) -> list[tuple[int, int]]:
    if block.cells:
        return [(c.char_start, c.char_end) for c in block.cells]
    if block.sentences:
        return [(s.char_start, s.char_end) for s in block.sentences]
    return [(block.anchor.char_start, block.anchor.char_end)]


def _trim(text: str, lo: int, hi: int) -> tuple[int, int]:
    while lo < hi and text[lo] in _EDGE:
        lo += 1
    marker = _LIST_MARKER.match(text, lo, hi)
    if marker:
        lo = marker.end()
        while lo < hi and text[lo] in _EDGE:
            lo += 1
    while hi > lo and text[hi - 1] in _EDGE:
        hi -= 1
    return lo, hi


def quote_span(text: str, unit: Unit, start: int, end: int) -> tuple[int, int]:
    """Фрагмент документа для цитаты: вся единица, а если она длинная, окно вокруг ``text[start:end]``.

    Цитату берёт код из документа, а не из памяти правила, поэтому она всегда есть в тексте дословно.
    """
    lo, hi = _trim(text, unit.start, unit.end)
    if hi - lo <= MAX_QUOTE:
        return lo, hi
    half = max(0, (MAX_QUOTE - (end - start)) // 2)
    a, b = max(lo, start - half), min(hi, end + half)
    while a < start and a > lo and not text[a - 1].isspace():
        a += 1
    while b > end and b < hi and not text[b].isspace():
        b -= 1
    return _trim(text, a, b)
