"""Шаги 0–1: защищённый вход и разбор документа в блоки с якорями."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from validator.errors import IN_UNSUPPORTED, InputError
from validator.ingest.blocks import ParsedLines, build_blocks, make_document, render_for_model
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


def parse(path: Path, kind: DocKind) -> ParsedDocument:
    """Шаг 1: документ в блоки с якорями. Ошибки чтения приходят как ``InputError``."""
    reader = _READERS.get(kind)
    if reader is None:
        raise InputError(f"Формат {kind.value} не поддерживается", code=IN_UNSUPPORTED)
    return make_document(path, kind, reader(path))


__all__ = ["build_blocks", "guard", "parse", "render_for_model"]
