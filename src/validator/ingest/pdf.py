"""Разбор PDF в строки для ``build_blocks`` (шаг 1): текст в порядке чтения, якоря страниц, признаки качества.

Конвейер: движок (``pdf_backends``: ``mupdf`` по умолчанию или ``pdfium`` без копилефта, переключатель
``RunConfig.pdf_backend``) отдаёт символы с координатами, ``pdf_layout`` собирает из них строки, убирает
колонтитулы, находит заголовки и абзацы и склеивает переносы. Здесь результат превращается в
``SourceLine`` с номером страницы и строки на странице.

Признаки для диагностики (``TextSignals``): страницы без текста (скан), доли букв, кириллицы и мусорных
символов (испорченный ``/ToUnicode``), флаг ремонта. Диагностики и коды по ним делает «Защищённый вход» (#27);
здесь только честный подсчёт. Таблицы по ячейкам делает #33, OCR и сканы #34.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from validator.errors import IN_FURNITURE_REMOVED, IN_NO_TEXT, IN_PAGES_WITHOUT_TEXT
from validator.ingest.blocks import ParsedLines
from validator.ingest.pdf_backends import (
    BACKEND_NAMES,
    DEFAULT_BACKEND,
    Glyph,
    PdfBackend,
    PdfSource,
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
from validator.models import DiagLevel, Diagnostic, SourceLine, TextSignals

MIN_PAGE_GLYPHS = 20
"""Меньше стольких символов на странице: текстового слоя нет (скан с одиноким номером страницы тоже)."""

_GARBAGE_CATEGORIES = frozenset({"Cc", "Co", "Cn", "Cs"})
_CYRILLIC = range(0x0400, 0x0530)
FURNITURE_EXAMPLES = 3

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


def _read_pages(source: PdfSource, engine: PdfBackend) -> tuple[list[list[Line]], _Counts, int]:
    pages: list[list[Line]] = []
    counts = _Counts()
    empty = 0
    for raw in source.pages():
        counts.add(raw.glyphs)
        empty += len(raw.glyphs) < MIN_PAGE_GLYPHS
        pages.append(build_lines(raw, engine.space_gap_em))
    return pages, counts, empty


def _source_lines(lines: list[Line], stats: LayoutStats) -> list[SourceLine]:
    out: list[SourceLine] = []
    for paragraph in group_lines(assign_headings(lines, stats), stats):
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
        pages, counts, empty = _read_pages(source, engine)
        page_count, repaired = source.page_count, source.repaired
    finally:
        source.close()
    marked = find_furniture(pages)
    body = [line for lines in pages for line in lines if (line.page, line.page_line) not in marked]
    furniture = [line for lines in pages for line in lines if (line.page, line.page_line) in marked]
    return ParsedLines(
        lines=_source_lines(body, compute_stats(body)),
        extraction=f"{engine.describe()}, символы с координатами в порядке чтения",
        diagnostics=_diagnostics(page_count, empty, furniture),
        pages=page_count,
        signals=counts.signals(page_count, empty, repaired),
    )
