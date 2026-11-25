"""Сверка цитат карточки вопроса с документом (CLAUDE.md, правило 4: каждая цитата есть в документе).

Цитату ищем ``locate_quote``: сначала в блоках кандидатов, затем во всём документе. Модель видит строки
таблиц в форме «Колонка: значение; …» (``render_for_model``) и иногда так их и цитирует, а в документе
такого текста нет. Тогда сверяем по ячейкам: каждое значение ищем отдельно, и если нашлись ячейки одной
строки, в карточку идёт дословный фрагмент этой строки документа от первой найденной ячейки до последней.
"""

from __future__ import annotations

from collections.abc import Sequence

from validator.models import ParsedDocument, Quote
from validator.text import MIN_FUZZY_CHARS, locate_quote, normalize

ROW_SEPARATOR = ";"
CELL_LABEL_SEPARATOR = ":"


def locate_in(doc: ParsedDocument, raw: str, within: Sequence[str]) -> Quote:
    """Сначала в указанных блоках, затем во всём документе (как верификатор)."""
    if within:
        found = locate_quote(doc, raw, within=within)
        if found.verified:
            return found
    return locate_quote(doc, raw)


def span(q: Quote) -> tuple[int, int] | None:
    return (q.anchor.char_start, q.anchor.char_end) if q.anchor else None


def _cell_values(raw: str) -> list[str]:
    """«Колонка: значение; Колонка 2: значение 2» → варианты каждой ячейки: как есть и без названия."""
    out: list[str] = []
    for part in (p.strip() for p in raw.split(ROW_SEPARATOR)):
        if not part:
            continue
        out.append(part)
        label, sep, value = part.partition(CELL_LABEL_SEPARATOR)
        if sep and label.strip() and value.strip():
            out.append(value.strip())
    return out


def _row_fragment(doc: ParsedDocument, raw: str, within: Sequence[str]) -> Quote | None:
    if ROW_SEPARATOR not in raw:
        return None
    cells: list[Quote] = []
    for value in _cell_values(raw):
        if len(normalize(value)[0]) < MIN_FUZZY_CHARS:
            continue
        found = locate_in(doc, value, within)
        if found.verified and found.anchor and found.anchor.block_ids:
            cells.append(found)
    if not cells:
        return None
    block = cells[0].anchor.block_ids[0] if cells[0].anchor else ""
    same_row = [q.anchor for q in cells if q.anchor and q.anchor.block_ids[:1] == [block]]
    start = min(a.char_start for a in same_row)
    end = max(a.char_end for a in same_row)
    fragment = locate_quote(doc, doc.text[start:end], within=[block])
    return fragment if fragment.verified else None


def verify_quote(doc: ParsedDocument, raw: str, within: Sequence[str]) -> Quote | None:
    """Сверенная цитата (или фрагмент строки таблицы) либо None, если в документе её нет."""
    if not raw.strip():
        return None
    found = locate_in(doc, raw, within)
    if found.verified:
        return found
    return _row_fragment(doc, raw, within)
