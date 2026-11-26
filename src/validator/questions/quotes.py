"""Сверка цитат карточки вопроса с документом (CLAUDE.md, правило 4: каждая цитата есть в документе).

Цитату ищем ``locate_quote``: сначала в блоках кандидатов, затем во всём документе. Модель видит строки
таблиц в форме «Колонка: значение; …» (``render_for_model``) и иногда так их и цитирует, а в документе
такого текста нет. Тогда сверяем по ячейкам: каждое значение ищем отдельно, и если нашлись ячейки одной
строки, в карточку идёт дословный фрагмент этой строки документа от первой найденной ячейки до последней.

Короткие значения ячеек («string», «1–64») и одну ячейку с названием колонки («Ожидаемый результат: …»)
по отдельности не найти. Поэтому последняя попытка: цитата дословно входит в строку таблицы в том виде,
в каком её видела модель (``Block.serialized``), и в документ идёт текст ячеек, которые она накрывает (#120).
"""

from __future__ import annotations

from collections.abc import Sequence

from validator.models import Block, BlockKind, ParsedDocument, Quote
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


def _loose(text: str) -> str:
    """Текст для сравнения с сериализацией строки: нормализация, без разметки Markdown и лишних пробелов."""
    return " ".join(normalize(text.replace("`", "").replace("**", ""))[0].split())


def _cell_spans(row: Block, serialized: str) -> list[tuple[int, int, int]]:
    """Где в ``_loose(row.serialized)`` стоит текст каждой непустой ячейки: (номер ячейки, начало, конец)."""
    out: list[tuple[int, int, int]] = []
    cursor = 0
    for k, cell in enumerate(row.cells):
        text = _loose(cell.text)
        if not text:
            continue
        labelled = _loose(f"{cell.column}: {cell.text}")
        pos = serialized.find(labelled, cursor)
        start = pos + len(labelled) - len(text) if pos >= 0 else serialized.find(text, cursor)
        if start < 0:
            continue
        out.append((k, start, start + len(text)))
        cursor = start + len(text)
    return out


def _serialized_row(doc: ParsedDocument, raw: str, within: Sequence[str]) -> Quote | None:
    """Цитата из строки таблицы в виде «Колонка: значение; …»: фрагмент документа по накрытым ячейкам.

    Строку берём среди блоков ``within``, иначе единственную строку документа, где цитата есть.
    """
    needle = _loose(raw)
    if len(needle) < MIN_FUZZY_CHARS:
        return None
    rows = [
        b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW and b.cells and needle in _loose(b.serialized)
    ]
    named = {ref.split(".", 1)[0] for ref in within}
    picked = [b for b in rows if b.id in named] or (rows if len(rows) == 1 else [])
    if not picked:
        return None
    row = picked[0]
    serialized = _loose(row.serialized)
    start = serialized.find(needle)
    end = start + len(needle)
    covered = [row.cells[k] for k, s, e in _cell_spans(row, serialized) if s < end and start < e]
    if not covered:
        return None
    fragment = doc.text[covered[0].char_start : covered[-1].char_end]
    found = locate_quote(doc, fragment, within=[row.id])
    return found if found.verified else None


def verify_quote(doc: ParsedDocument, raw: str, within: Sequence[str]) -> Quote | None:
    """Сверенная цитата (или фрагмент строки таблицы) либо None, если в документе её нет."""
    if not raw.strip():
        return None
    found = locate_in(doc, raw, within)
    if found.verified:
        return found
    return _row_fragment(doc, raw, within) or _serialized_row(doc, raw, within)
