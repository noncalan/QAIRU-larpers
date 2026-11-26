"""Разбор PDF в строки для ``build_blocks`` (шаг 1): текст в порядке чтения, якоря страниц, признаки качества.

Конвейер: движок (``pdf_backends``: ``mupdf`` по умолчанию или ``pdfium`` без копилефта, переключатель
``RunConfig.pdf_backend``) отдаёт символы с координатами, ``pdf_layout`` собирает из них строки, убирает
колонтитулы, находит заголовки и абзацы и склеивает переносы. Здесь результат превращается в
``SourceLine`` с номером страницы и строки на странице.

Признаки для диагностики (``TextSignals``): страницы без текста (скан), доли букв, кириллицы и мусорных
символов (испорченный ``/ToUnicode``), флаг ремонта. Диагностики и коды по ним делает «Защищённый вход» (#27);
здесь только честный подсчёт. Таблицы читает ``pdf_tables`` по ячейкам (#33): строки таблицы идут в
``SourceLine`` с номером таблицы, строки и ячейками, а их текст не попадает в поток абзацев.

Сканы (#38): страница без текстового слоя с картинкой и страница с мусорным слоем распознаются OCR
(``ocr.py``), если в системе есть Tesseract с русским языком; строки таких страниц помечены
``Extraction.OCR``, в диагностике ``IN-050``. Без распознавания скан (текста нет больше чем на
``SCAN_SHARE`` страниц) даёт ``IN-055`` с инструкцией и код 4, смешанный документ ``IN-051`` и код 3.
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from validator.errors import (
    IN_FURNITURE_REMOVED,
    IN_NO_TEXT,
    IN_OCR_APPLIED,
    IN_PAGES_WITHOUT_TEXT,
    IN_SCANNED,
)
from validator.ingest.blocks import ParsedLines
from validator.ingest.ocr import MAX_OCR_PAGES, SCAN_HELP, OcrReader
from validator.ingest.pdf_backends import (
    BACKEND_NAMES,
    DEFAULT_BACKEND,
    Glyph,
    PdfBackend,
    PdfSource,
    RawPage,
    get_backend,
)
from validator.ingest.pdf_cell_text import vocabulary
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
from validator.ingest.pdf_tables import LogicalTable, PageTable, cell_lines, find_tables, link_tables, rejoin
from validator.ingest.quality import page_ranges
from validator.models import DiagLevel, Diagnostic, Extraction, SourceLine, TextSignals

MIN_PAGE_GLYPHS = 5
"""Меньше стольких символов на странице: текстового слоя нет (скан с одиноким номером страницы тоже).
Короткая, но настоящая страница («Система должна работать.») остаётся текстовой."""

SCAN_SHARE = 0.5
"""Без текста больше этой доли страниц (с картинками) и распознавания нет: документ это скан, проверять
нечего, отчёт-диагностика и код 4. Не больше этой доли: смешанный документ, проверяем страницы с текстом."""
GARBLED_PAGE_SHARE = 0.3
"""Доля служебных символов страницы, с которой её текстовый слой мусор и её стоит распознать OCR (как
``quality.GARBLED_FATAL_SHARE`` для всего документа)."""

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
    empty: list[int] = field(default_factory=list)
    """Страницы без текста (после распознавания, если оно было)."""
    scanned: list[int] = field(default_factory=list)
    """Из них страницы с картинкой: похоже на скан."""
    ocr: list[int] = field(default_factory=list)
    """Страницы, текст которых распознан OCR."""


def _garbled(glyphs: list[Glyph]) -> bool:
    """Текстовый слой страницы мусор: шрифты без таблицы Unicode дают управляющие символы."""
    counts = _Counts()
    counts.add(glyphs)
    return counts.total >= MIN_PAGE_GLYPHS and counts.garbage >= GARBLED_PAGE_SHARE * counts.total


def _recognized(raw: RawPage, ocr: OcrReader | None, read: _Pages) -> RawPage:
    """Страница после OCR, если её стоит и можно распознать: без текста с картинкой или с мусорным слоем."""
    empty = len(raw.glyphs) < MIN_PAGE_GLYPHS
    if ocr is None or not ((empty and raw.images) or _garbled(raw.glyphs)):
        return raw
    glyphs = ocr.glyphs(raw.number)
    if len(glyphs) < MIN_PAGE_GLYPHS:
        return raw
    read.ocr.append(raw.number)
    return replace(raw, glyphs=glyphs, rules=[])


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


def _flatten_ocr_styles(read: _Pages) -> None:
    """Кегль распознанных строк это высота рамки строки, а не шрифт: разброс в полтора раза внутри абзаца,
    жирного нет. Заголовок по кеглю на таких страницах был бы случайным, поэтому кегль распознанных строк
    приравниваем к основному кеглю документа: заголовками остаются короткие обособленные нумерованные строки.
    """
    if not read.ocr:
        return
    pages = frozenset(read.ocr)
    text_lines = [line for lines in read.prose for line in lines if line.page not in pages]
    ocr_lines = [line for lines in read.prose for line in lines if line.page in pages]
    body = compute_stats(text_lines or ocr_lines).body_size

    def flat(lines: list[Line]) -> list[Line]:
        return [replace(ln, size=body, bold=False, italic=False) if ln.page in pages else ln for ln in lines]

    read.prose = [flat(lines) for lines in read.prose]
    read.lines = [flat(lines) for lines in read.lines]


def _read_pages(source: PdfSource, engine: PdfBackend, ocr: OcrReader | None) -> _Pages:
    read = _Pages()
    for page in source.pages():
        raw = _recognized(page, ocr, read)
        read.counts.add(raw.glyphs)
        if len(raw.glyphs) < MIN_PAGE_GLYPHS:
            read.empty.append(raw.number)
            if raw.images:
                read.scanned.append(raw.number)
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


def _append_paragraphs(
    out: list[SourceLine], lines: list[Line], stats: LayoutStats, ocr_pages: frozenset[int]
) -> None:
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
                    extraction=Extraction.OCR if line.page in ocr_pages else Extraction.TEXT,
                )
            )


def _source_lines(
    lines: list[Line],
    stats: LayoutStats,
    tables: list[LogicalTable],
    pages: list[list[Line]],
    ocr_pages: frozenset[int] = frozenset(),
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
        _append_paragraphs(out, run, stats, ocr_pages)
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


@dataclass(frozen=True)
class _OcrNote:
    """Что знает диагностика про распознавание: было ли оно доступно, почему нет, чем распознано."""

    available: bool = False
    reason: str = ""
    engine: str = ""
    exhausted: bool = False

    @classmethod
    def of(cls, ocr: OcrReader) -> _OcrNote:
        status = ocr.status
        return cls(status.available, status.reason, status.describe(), ocr.exhausted)


def _ocr_diagnostic(pages: int, ocr_pages: list[int], note: _OcrNote) -> Diagnostic:
    where = f"стр. {page_ranges(ocr_pages)}"
    return Diagnostic(
        code=IN_OCR_APPLIED,
        level=DiagLevel.WARNING,
        message=(
            f"На {where} ({len(ocr_pages)} из {pages}) нет текстового слоя или он испорчен: текст распознан "
            f"{note.engine}. Распознавание ошибается примерно в каждом десятом слове"
        ),
        action="Проверка идёт по распознанному тексту, места с этих страниц помечены «текст распознан (OCR)»",
        hint=(
            "Сверьте цитаты с этих страниц с оригиналом; для точной проверки пришлите PDF с текстовым "
            "слоем (сохранённый из Word или Google Docs) или DOCX"
        ),
        location=where,
    )


def _scan_diagnostic(pages: int, read: _Pages, note: _OcrNote) -> Diagnostic:
    """``IN-055``: скан без распознавания, проверять нечего. В «что сделать» инструкция ``SCAN_HELP``."""
    where = f"стр. {page_ranges(read.empty)}"
    rest = f", текст есть только на {pages - len(read.empty)} из {pages}" if len(read.empty) < pages else ""
    return Diagnostic(
        code=IN_SCANNED,
        level=DiagLevel.ERROR,
        message=(
            f"Документ похож на скан: страницы это картинки без текстового слоя ({where}{rest}), "
            f"а распознать их нельзя: {note.reason}"
        ),
        action="Проверка не выполнялась: без текста нечего цитировать, отчёт по картинкам был бы выдумкой",
        hint=SCAN_HELP,
        location=where,
    )


def _empty_diagnostic(pages: int, read: _Pages, note: _OcrNote) -> Diagnostic:
    """Страницы без текста после распознавания: все (``IN-071``) или часть (``IN-051``, код 3)."""
    empty = len(read.empty)
    tried = note.available and bool(read.scanned)
    if empty == pages:
        detail = ", распознавание не нашло на них текста" if tried else ""
        return Diagnostic(
            code=IN_NO_TEXT,
            level=DiagLevel.ERROR,
            message=f"Нет текстового слоя ни на одной из {pages} стр.{detail}",
            action="Проверка не выполнялась",
            hint="Пришлите PDF с текстовым слоем (не скан и не пустые страницы) или исходник в DOCX",
        )
    hint = "Пришлите PDF с текстовым слоем (не скан) или исходник в DOCX"
    if read.scanned and not note.available:
        hint = f"На страницах без текста картинки, похоже на скан. {SCAN_HELP}"
    elif read.scanned and note.exhausted:
        hint = (
            f"За один прогон распознаём не больше {MAX_OCR_PAGES} стр.: разделите документ на части. {hint}"
        )
    return Diagnostic(
        code=IN_PAGES_WITHOUT_TEXT,
        level=DiagLevel.WARNING,
        message=f"Нет текстового слоя на {empty} из {pages} стр.",
        action="Проверка идёт только по страницам с текстом",
        hint=hint,
    )


def _diagnostics(pages: int, read: _Pages, furniture: list[Line], note: _OcrNote) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    if pages and read.ocr:
        diagnostics.append(_ocr_diagnostic(pages, read.ocr, note))
    if pages and read.empty:
        scan = bool(read.scanned) and not note.available and len(read.empty) > SCAN_SHARE * pages
        diagnostics.append(
            _scan_diagnostic(pages, read, note) if scan else _empty_diagnostic(pages, read, note)
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


def parse_pdf(path: Path, backend: str | None = None, *, ocr: bool | None = None) -> ParsedLines:
    """Текст PDF строками в порядке чтения. Битый или запароленный файл даёт ``InputError``.

    ``ocr``: распознавать страницы без текста, если в системе есть Tesseract с русским (#38); None значит
    «по переменной ``VALIDATOR_OCR``».
    """
    engine = get_backend(backend)
    source = engine.open(path)
    reader = OcrReader(path, enabled=ocr)
    try:
        read = _read_pages(source, engine, reader)
        page_count, repaired = source.page_count, source.repaired
        _flatten_ocr_styles(read)
    finally:
        source.close()
        reader.close()
    vocab = vocabulary((line.text for lines in read.prose for line in lines), cell_lines(read.tables))
    tables = link_tables(rejoin(read.tables, vocab), read.heights, vocab)
    marked = find_furniture(read.prose)
    body = [line for lines in read.prose for line in lines if (line.page, line.page_line) not in marked]
    furniture = [line for lines in read.prose for line in lines if (line.page, line.page_line) in marked]
    note = _OcrNote.of(reader) if reader.consulted else _OcrNote()
    extraction = f"{engine.describe()}, символы с координатами в порядке чтения"
    if tables:
        extraction += f", таблицы по ячейкам ({len(tables)})"
    if read.ocr:
        extraction += f", {note.engine}: стр. {page_ranges(read.ocr)}"
    return ParsedLines(
        lines=_source_lines(body, compute_stats(body), tables, read.lines, frozenset(read.ocr)),
        extraction=extraction,
        diagnostics=_diagnostics(page_count, read, furniture, note),
        pages=page_count,
        signals=read.counts.signals(page_count, len(read.empty), repaired),
    )
