"""Разбор Markdown и простого текста в строки для ``build_blocks`` (шаг 1).

Файл читается как байты: UTF-8 (с BOM и без) сразу, остальное (cp1251, koi8-r, cp866, UTF-16)
определяет charset-normalizer. Если кодировка угадана ненадёжно, в отчёт уходит предупреждение
``IN-062``: цитаты из такого текста могут быть искажены, и это должен знать автор документа.
Разметку (заголовки ATX и Setext, списки, таблицы, цитаты, код) распознаёт ``ingest/blocks.py``,
номер строки у блока это номер строки исходного файла.
"""

from __future__ import annotations

from pathlib import Path

from charset_normalizer import from_bytes
from charset_normalizer.models import CharsetMatch

from validator.errors import IN_ENCODING, IN_ENCODING_UNCERTAIN, InputError
from validator.ingest.blocks import ParsedLines, text_diagnostics
from validator.models import DiagLevel, Diagnostic, DocKind, SourceLine

MAX_CHAOS = 0.1
"""Доля «странных» последовательностей символов, выше которой кодировка считается угаданной ненадёжно."""
MIN_COHERENCE = 0.3
"""Насколько буквы похожи на реальный язык; ниже это почти всегда случайная кодировка (на коротком тексте)."""
MAX_REPLACEMENTS = 0.01
"""Доля символов замены U+FFFD в тексте, выше которой перекодирование считаем неудачным."""
SAFE_ENCODINGS = frozenset({"ascii", "utf_8"})


def _is_confident(match: CharsetMatch, text: str) -> bool:
    """Кодировка с BOM и чистый UTF-8 надёжны; иначе смотрим на «хаос», связность и символы замены."""
    if match.encoding in SAFE_ENCODINGS or match.bom:
        return True
    replaced = text.count("�") / max(1, len(text))
    return match.chaos <= MAX_CHAOS and match.coherence >= MIN_COHERENCE and replaced <= MAX_REPLACEMENTS


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
    text = str(best).lstrip("﻿")
    diagnostics = [
        Diagnostic(
            code=IN_ENCODING,
            level=DiagLevel.INFO,
            message=f"Файл не в UTF-8, кодировка определена как {best.encoding}",
            action="Текст перекодирован в UTF-8",
            hint="Если в цитатах отчёта видны странные символы, сохраните документ в UTF-8",
        )
    ]
    if not _is_confident(best, text):
        diagnostics.append(
            Diagnostic(
                code=IN_ENCODING_UNCERTAIN,
                level=DiagLevel.WARNING,
                message=(
                    f"Кодировка файла определена с низкой уверенностью ({best.encoding}): "
                    "в тексте и цитатах возможны искажённые символы"
                ),
                action="Текст перекодирован как есть, проверка выполнена по нему",
                hint="Сохраните документ в UTF-8 и запустите проверку снова",
            )
        )
    return text, diagnostics


def split_lines(text: str) -> list[SourceLine]:
    """Строки как в редакторе: перевод строки любого вида (CRLF, CR, LF) это одна граница."""
    return [SourceLine(text=line) for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def parse_md(path: Path) -> ParsedLines:
    """Markdown или простой текст. Пустой документ даёт пустой результат и ``IN-071``."""
    text, diagnostics = decode_text(path.read_bytes())
    lines = split_lines(text)
    problems = text_diagnostics(lines, DocKind.MD, path.name)
    if any(d.level is DiagLevel.ERROR for d in problems):
        lines = []
    return ParsedLines(
        lines=lines,
        extraction="Markdown, построчно",
        diagnostics=[*diagnostics, *problems],
    )
