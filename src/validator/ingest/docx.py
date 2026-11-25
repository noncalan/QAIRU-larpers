"""Разбор DOCX в строки для ``build_blocks`` (минимальная версия каркаса).

Абзацы и таблицы python-docx в порядке документа; заголовки по стилю «Heading N». Абзац получает
номер (якорь «абзац N»), строка таблицы номер таблицы и ячейки. Списки, сноски, колонтитулы и
объединённые ячейки делает #25.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

import docx
from docx.opc.exceptions import PackageNotFoundError
from docx.table import Table
from docx.text.paragraph import Paragraph
from lxml import etree

from validator.errors import IN_DOCX_BROKEN, InputError
from validator.ingest.blocks import ParsedLines
from validator.models import Extraction, SourceLine

_HEADING_STYLE = re.compile(r"^(?:Heading|Заголовок)\s*(\d)$", re.IGNORECASE)
_CELL_SEPARATOR = " | "


def _heading_level(paragraph: Paragraph) -> int | None:
    name = paragraph.style.name if paragraph.style is not None else ""
    if name == "Title":
        return 1
    m = _HEADING_STYLE.match(name or "")
    return int(m.group(1)) if m else None


def _paragraph_lines(paragraph: Paragraph, number: int) -> list[SourceLine]:
    level = _heading_level(paragraph)
    return [
        SourceLine(text=part.strip(), paragraph=number, heading_level=level)
        for part in paragraph.text.split("\n")
        if part.strip()
    ]


def _table_lines(table: Table, number: int) -> list[SourceLine]:
    rows = list(table.rows)
    out: list[SourceLine] = []
    for index, row in enumerate(rows):
        cells = [" ".join(cell.text.split()) for cell in row.cells]
        out.append(
            SourceLine(
                text=_CELL_SEPARATOR.join(cells),
                table=number,
                row=index,
                cells=cells,
                header=index == 0 and len(rows) > 1,
                extraction=Extraction.TABLE,
            )
        )
    return out


def _read_lines(path: Path) -> list[SourceLine]:
    document = docx.Document(str(path))
    lines: list[SourceLine] = []
    paragraphs = tables = 0
    for item in document.iter_inner_content():
        if isinstance(item, Paragraph):
            paragraphs += 1
            group = _paragraph_lines(item, paragraphs)
        else:
            tables += 1
            group = _table_lines(item, tables)
        if not group:
            continue
        if lines:
            lines.append(SourceLine(text=""))
        lines.extend(group)
    return lines


def parse_docx(path: Path) -> ParsedLines:
    """Абзацы и таблицы по порядку. Битый архив или XML внутри дают ``InputError`` IN-032."""
    try:
        lines = _read_lines(path)
    except (PackageNotFoundError, zipfile.BadZipFile, KeyError, ValueError, etree.XMLSyntaxError) as exc:
        raise InputError(
            f"DOCX не открывается: {type(exc).__name__}",
            code=IN_DOCX_BROKEN,
            hint="Пересохраните документ в Word или пришлите PDF",
        ) from exc
    return ParsedLines(lines=lines, extraction="python-docx: абзацы и таблицы по порядку")
