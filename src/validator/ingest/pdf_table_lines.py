"""Таблицы PDF как строки для ``build_blocks`` (шаг 1, #33).

``pdf_tables`` находит таблицы и ячейки, здесь они становятся ``SourceLine``: заголовок, затем строки с
номером таблицы, строки, ячейками и якорем страницы. Строка таблицы это одна строка канонического текста,
ячейки через ``CELL_SEPARATOR`` (как в DOCX), поэтому ``build_blocks`` и сверка цитат работают без изменений.
"""

from __future__ import annotations

from validator.ingest.pdf_layout import Line
from validator.ingest.pdf_tables import CELL_SEPARATOR, LogicalTable, TableRow
from validator.models import Extraction, SourceLine


def _page_line(pages: list[list[Line]], page: int, top: float) -> int | None:
    """Номер первой визуальной строки страницы, которая лежит не выше ``top``: «строка N на странице»."""
    lines = next((lines for lines in pages if lines and lines[0].page == page), [])
    return next((line.page_line for line in lines if line.bottom >= top - 1), None)


def _line(table: LogicalTable, row: TableRow, index: int, pages: list[list[Line]]) -> SourceLine:
    return SourceLine(
        text=CELL_SEPARATOR.join(row.cells),
        page=row.page,
        page_line=_page_line(pages, row.page, row.top),
        table=table.number,
        row=index,
        cells=list(row.cells),
        header=index == 0,
        extraction=Extraction.TABLE,
    )


def table_part_lines(table: LogicalTable, part: int, pages: list[list[Line]]) -> list[SourceLine]:
    """Строки одной страничной части таблицы: у первой части впереди заголовок.

    Строка относится к части своей страницы (``TableRow.page``); хвост ячейки, перенесённый на следующую
    страницу, уже присоединён к своей строке и отдельной строки не даёт. Таблица с заголовком без единой
    строки это незаполненная форма (блок подписей): она получает одну пустую строку, потому что пустое поле
    тоже факт документа.
    """
    page = table.parts[part].page
    out: list[SourceLine] = []
    if part == 0:
        first = table.parts[0]
        head = TableRow(list(table.header), first.page, first.top, first.top)
        out.append(_line(table, head, 0, pages))
        if not table.rows:
            empty = TableRow([""] * len(table.header), first.page, first.bottom, first.bottom)
            out.append(_line(table, empty, 1, pages))
    out.extend(_line(table, row, k, pages) for k, row in enumerate(table.rows, start=1) if row.page == page)
    return out
