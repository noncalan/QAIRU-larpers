"""Разбор PDF в строки для ``build_blocks`` (минимальная версия каркаса).

PyMuPDF ``get_text("text", sort=True)`` по страницам: порядок чтения (без ``sort`` цифры и знаки
выпадают из строки, исследование 18, п. 2.6), у каждой строки номер страницы. Заголовки по шрифту,
колонтитулы, второй бэкенд (pdfium) и признаки скана делает #26, таблицы по ячейкам #33.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

from validator.errors import (
    IN_NO_TEXT,
    IN_PAGES_WITHOUT_TEXT,
    IN_PDF_BROKEN,
    IN_PDF_ENCRYPTED,
    InputError,
)
from validator.ingest.blocks import ParsedLines
from validator.models import DiagLevel, Diagnostic, SourceLine, TextSignals


def _page_lines(page: pymupdf.Page, number: int) -> list[list[SourceLine]]:
    """Строки страницы в порядке чтения; пустая строка в выводе PyMuPDF разделяет абзацы."""
    groups: list[list[SourceLine]] = []
    current: list[SourceLine] = []
    for raw in page.get_text("text", sort=True).split("\n"):
        if raw.strip():
            current.append(SourceLine(text=raw.strip(), page=number))
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def parse_pdf(path: Path) -> ParsedLines:
    try:
        doc = pymupdf.open(path)
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        raise InputError(
            f"PDF не открывается: {type(exc).__name__}",
            code=IN_PDF_BROKEN,
            hint="Пересохраните PDF или пришлите исходник в DOCX или Markdown",
        ) from exc
    with doc:
        if doc.needs_pass:
            raise InputError(
                "PDF защищён паролем на открытие",
                code=IN_PDF_ENCRYPTED,
                hint="Снимите пароль и запустите проверку заново",
            )
        lines: list[SourceLine] = []
        empty_pages = 0
        for number, page in enumerate(doc, start=1):
            groups = _page_lines(page, number)
            if not groups:
                empty_pages += 1
            for group in groups:
                if lines:
                    lines.append(SourceLine(text="", page=number))
                lines.extend(group)
        pages = doc.page_count
    diagnostics: list[Diagnostic] = []
    if pages and empty_pages:
        diagnostics.append(
            Diagnostic(
                code=IN_NO_TEXT if empty_pages == pages else IN_PAGES_WITHOUT_TEXT,
                level=DiagLevel.ERROR if empty_pages == pages else DiagLevel.WARNING,
                message=f"Нет текстового слоя на {empty_pages} из {pages} стр.",
                action="Проверка идёт только по страницам с текстом",
                hint="Пришлите PDF с текстовым слоем (не скан) или исходник в DOCX",
            )
        )
    return ParsedLines(
        lines=lines,
        extraction=f'PyMuPDF {pymupdf.VersionBind}, get_text("text", sort=True)',
        diagnostics=diagnostics,
        pages=pages,
        signals=TextSignals(pages_total=pages, pages_without_text=empty_pages),
    )
