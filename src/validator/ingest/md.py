"""Разбор Markdown и простого текста в строки для ``build_blocks`` (минимальная версия каркаса).

Полный разбор (Setext-заголовки, кодировки с оценкой уверенности, цитаты) делает #25.
"""

from __future__ import annotations

from pathlib import Path

from charset_normalizer import from_bytes

from validator.errors import IN_ENCODING, InputError
from validator.ingest.blocks import ParsedLines
from validator.models import DiagLevel, Diagnostic, SourceLine


def decode_text(data: bytes) -> tuple[str, list[Diagnostic]]:
    """UTF-8 (с BOM и без), иначе кодировку угадывает charset-normalizer."""
    try:
        return data.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        pass
    best = from_bytes(data).best()
    if best is None:
        raise InputError(
            "Не удалось определить кодировку текстового файла",
            code=IN_ENCODING,
            hint="Сохраните документ в UTF-8 или пришлите PDF или DOCX",
        )
    diag = Diagnostic(
        code=IN_ENCODING,
        level=DiagLevel.INFO,
        message=f"Файл не в UTF-8, кодировка определена как {best.encoding}",
        action="Текст перекодирован в UTF-8",
        hint="Если в цитатах отчёта видны странные символы, сохраните документ в UTF-8",
    )
    return str(best), [diag]


def parse_md(path: Path) -> ParsedLines:
    text, diagnostics = decode_text(path.read_bytes())
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [SourceLine(text=line) for line in text.split("\n")]
    return ParsedLines(lines=lines, extraction="Markdown, построчно", diagnostics=diagnostics)
