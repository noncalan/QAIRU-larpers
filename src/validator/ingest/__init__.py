"""Шаги 0–1: защищённый вход и разбор документа в блоки с якорями."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from validator.errors import IN_UNSUPPORTED, InputError
from validator.ingest.blocks import (
    ParsedLines,
    build_blocks,
    defuse_delimiters,
    make_document,
    render_for_model,
)
from validator.ingest.docx import parse_docx
from validator.ingest.guard import guard
from validator.ingest.md import parse_md
from validator.ingest.pdf import parse_pdf
from validator.models import DocKind, ParsedDocument

_READERS: dict[DocKind, Callable[[Path], ParsedLines]] = {
    DocKind.MD: parse_md,
    DocKind.PDF: parse_pdf,
    DocKind.DOCX: parse_docx,
}


def parse(
    path: Path, kind: DocKind, *, pdf_backend: str | None = None, ocr: bool | None = None
) -> ParsedDocument:
    """Шаг 1: документ в блоки с якорями. Ошибки чтения приходят как ``InputError``.

    ``pdf_backend``: ``mupdf`` (по умолчанию) или ``pdfium``; для других форматов не используется.
    ``ocr``: распознавать страницы PDF без текста, если в системе есть Tesseract с русским (#38); None
    значит «по переменной ``VALIDATOR_OCR``».
    """
    reader = _READERS.get(kind)
    if reader is None:
        raise InputError(f"Формат {kind.value} не поддерживается", code=IN_UNSUPPORTED)
    if kind is DocKind.PDF:
        return make_document(path, kind, parse_pdf(path, pdf_backend, ocr=ocr))
    return make_document(path, kind, reader(path))


__all__ = ["build_blocks", "defuse_delimiters", "guard", "parse", "render_for_model"]
