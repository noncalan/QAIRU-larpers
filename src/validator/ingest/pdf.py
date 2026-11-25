"""Разбор PDF в строки для ``build_blocks`` (шаг 1): текст в порядке чтения, якоря страниц, признаки качества.

Конвейер: движок (``pdf_backends``: ``mupdf`` по умолчанию или ``pdfium`` без копилефта, переключатель
``RunConfig.pdf_backend``) отдаёт символы с координатами, ``pdf_layout`` собирает из них строки, убирает
колонтитулы, находит заголовки и абзацы и склеивает переносы. Здесь результат превращается в
``SourceLine`` с номером страницы и строки на странице.

Признаки для диагностики (``TextSignals``): страницы без текста (скан), доли букв, кириллицы и мусорных
символов (испорченный ``/ToUnicode``), флаг ремонта. Диагностики и коды по ним делает «Защищённый вход» (#27);
здесь только честный подсчёт. Таблицы читает ``pdf_tables`` по ячейкам (#33): строки таблицы идут в
``SourceLine`` с номером таблицы, строки и ячейками, а их текст не попадает в поток абзацев. OCR и сканы: #34.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from validator.errors import IN_FURNITURE_REMOVED, IN_NO_TEXT, IN_PAGES_WITHOUT_TEXT
from validator.ingest.blocks import ParsedLines
from validator.ingest.pdf_backends import (
    BACKEND_NAMES,
    DEFAULT_BACKEND,
    Glyph,
    PdfBackend,
    PdfSource,
    RawPage,
    get_backend,
)
from validator.ingest.pdf_layout import (
    LayoutStats,
    Line,
    assign_headings,
    build_lines,
    compute_stats,
    find_furniture,
    group_lines,
)
from validator.ingest.pdf_table_lines import table_part_lines
from validator.ingest.pdf_tables import LogicalTable, PageTable, find_tables, link_tables
from validator.models import DiagLevel, Diagnostic, SourceLine, TextSignals

MIN_PAGE_GLYPHS = 5
"""Меньше стольких символов на странице: текстового слоя нет (скан с одиноким номером страницы тоже).
Короткая, но настоящая страница («Система должна работать.») остаётся текстовой."""

_GARBAGE_CATEGORIES = frozenset({"Cc", "Co", "Cn", "Cs"})
_CYRILLIC = range(0x0400, 0x0530)
FURNITURE_EXAMPLES = 3
logger = logging.getLogger("validator")

__all__ = ["BACKEND_NAMES", "DEFAULT_BACKEND", "parse_pdf"]


@dataclass
class _Counts:
    total: int = 0
    letters: int = 0
    cyrillic: int = 0
    garbage: int = 0

    def add(self, glyphs: Iterable[Glyph]) -> None:
        for glyph in glyphs:
            for ch in glyph.char:
                self.total += 1
                if ch.isalpha():
                    self.letters += 1
                    self.cyrillic += ord(ch) in _CYRILLIC
                elif ch == "�" or unicodedata.category(ch) in _GARBAGE_CATEGORIES:
                    self.garbage += 1

    def signals(self, pages: int, empty: int, repaired: bool) -> TextSignals:
        return TextSignals(
            pages_total=pages,
            pages_without_text=empty,
            letters_share=round(self.letters / self.total, 4) if self.total else None,
            cyrillic_share=round(self.cyrillic / self.letters, 4) if self.letters else None,
            garbage_share=round(self.garbage / self.total, 4) if self.total else None,
            repaired=repaired,
        )


@dataclass
class _Pages:
    """Прочитанные страницы: визуальные строки (все и только текст вне таблиц), таблицы, высоты страниц."""

    lines: list[list[Line]] = field(default_factory=list)
    prose: list[list[Line]] = field(default_factory=list)
    tables: list[PageTable] = field(default_factory=list)
    heights: dict[int, float] = field(default_factory=dict)
    counts: _Counts = field(default_factory=_Counts)
    empty: int = 0


def _find_tables(raw: RawPage, gap_em: float) -> list[PageTable]:
    """Таблицы страницы; сбой разбора одной страницы не роняет прогон: текст остаётся строками."""
    try:
        return find_tables(raw, gap_em)
    except (ValueError, IndexError, KeyError, ArithmeticError) as exc:
        logger.warning("Стр. %d: таблицы не разобраны (%s), текст страницы идёт строками", raw.number, exc)
        return []


def _renumber(prose: list[Line], full: list[Line]) -> list[Line]:
    """Номер строки на странице у текста вне таблиц берём от ближайшей визуальной строки всей страницы."""
    if not full:
        return prose
    return [
        replace(line, page_line=min(full, key=lambda other: abs(other.center - line.center)).page_line)
        for line in prose
    ]


def _read_pages(source: PdfSource, engine: PdfBackend) -> _Pages:
    read = _Pages()
    for raw in source.pages():
        read.counts.add(raw.glyphs)
        read.empty += len(raw.glyphs) < MIN_PAGE_GLYPHS
        read.heights[raw.number] = raw.height
        full = build_lines(raw, engine.space_gap_em)
        found = _find_tables(raw, engine.space_gap_em)
        prose = full
        if found:
            rest = [g for g in raw.glyphs if not any(t.contains(g) for t in found)]
            prose = _renumber(build_lines(replace(raw, glyphs=rest), engine.space_gap_em), full)
        read.lines.append(full)
        read.prose.append(prose)
        read.tables.extend(found)
    return read


def _append_paragraphs(out: list[SourceLine], lines: list[Line], stats: LayoutStats) -> None:
    for paragraph in group_lines(lines, stats):
        if out:
            out.append(SourceLine(text="", page=paragraph.lines[0].page))
        for k, line in enumerate(paragraph.lines):
            out.append(
                SourceLine(
                    text=line.text,
                    page=line.page,
                    page_line=line.page_line,
                    wrapped=k > 0,
                    heading_level=line.heading_level,
                )
            )


def _source_lines(
    lines: list[Line], stats: LayoutStats, tables: list[LogicalTable], pages: list[list[Line]]
) -> list[SourceLine]:
    """Абзацы и таблицы в порядке чтения: каждая часть таблицы встаёт на своём месте страницы.

    Таблица разрывает абзац, даже если строки над ней и под ней стоят близко друг к другу. Часть таблицы на
    следующей странице идёт после текста, который стоит над ней, а не вместе с первой частью.
    """
    out: list[SourceLine] = []
    run: list[Line] = []
    pending = sorted(
        ((part.page, part.top, table, k) for table in tables for k, part in enumerate(table.parts)),
        key=lambda item: item[:2],
    )

    def flush() -> None:
        _append_paragraphs(out, run, stats)
        run.clear()

    def emit(page: int, table: LogicalTable, part: int) -> None:
        flush()
        if out and out[-1].text and out[-1].table != table.number:
            out.append(SourceLine(text="", page=page))
        out.extend(table_part_lines(table, part, pages))

    for line in assign_headings(lines, stats):
        while pending and pending[0][:2] <= (line.page, line.top):
            page, _, table, part = pending.pop(0)
            emit(page, table, part)
        run.append(line)
    flush()
    for page, _, table, part in pending:
        emit(page, table, part)
    return out


def _diagnostics(pages: int, empty: int, furniture: list[Line]) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if pages and empty:
        diagnostics.append(
            Diagnostic(
                code=IN_NO_TEXT if empty == pages else IN_PAGES_WITHOUT_TEXT,
                level=DiagLevel.ERROR if empty == pages else DiagLevel.WARNING,
                message=f"Нет текстового слоя на {empty} из {pages} стр.",
                action="Проверка идёт только по страницам с текстом",
                hint="Пришлите PDF с текстовым слоем (не скан) или исходник в DOCX",
            )
        )
    if furniture:
        examples = "; ".join(f"«{line.text}»" for line in furniture[:FURNITURE_EXAMPLES])
        diagnostics.append(
            Diagnostic(
                code=IN_FURNITURE_REMOVED,
                level=DiagLevel.INFO,
                message=(
                    f"Колонтитулы и номера страниц ({len(furniture)} строк на "
                    f"{len({line.page for line in furniture})} стр.) не считаются требованиями: {examples}"
                ),
                action="Эти строки убраны из текста до нарезки на требования",
            )
        )
    return diagnostics


def parse_pdf(path: Path, backend: str | None = None) -> ParsedLines:
    """Текст PDF строками в порядке чтения. Битый или запароленный файл даёт ``InputError``."""
    engine = get_backend(backend)
    source = engine.open(path)
    try:
        read = _read_pages(source, engine)
        page_count, repaired = source.page_count, source.repaired
    finally:
        source.close()
    tables = link_tables(read.tables, read.heights)
    marked = find_furniture(read.prose)
    body = [line for lines in read.prose for line in lines if (line.page, line.page_line) not in marked]
    furniture = [line for lines in read.prose for line in lines if (line.page, line.page_line) in marked]
    extraction = f"{engine.describe()}, символы с координатами в порядке чтения"
    if tables:
        extraction += f", таблицы по ячейкам ({len(tables)})"
    return ParsedLines(
        lines=_source_lines(body, compute_stats(body), tables, read.lines),
        extraction=extraction,
        diagnostics=_diagnostics(page_count, read.empty, furniture),
        pages=page_count,
        signals=read.counts.signals(page_count, read.empty, repaired),
    )
