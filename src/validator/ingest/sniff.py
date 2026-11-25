"""Тип файла по содержимому, а не по расширению (шаг 0, исследование 04, раздел 3).

Детерминированные сигнатуры без libmagic: так одинаково на Linux, macOS и Windows жюри. Сюда приходят
первые байты файла; глубокие проверки контейнеров (ZIP, OLE, PDF) делает ``ingest/containers.py``.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

SNIFF_BYTES = 8192
"""Сколько байт с начала файла смотрим. Хватает на сигнатуры и на оценку «текст или двоичный»."""
PDF_HEADER_WINDOW = 1024
"""Заголовок ``%PDF-`` может стоять не в самом начале: перед ним бывает мусор (так читают и просмотрщики)."""
MAX_CONTROL_SHARE = 0.05
"""Доля управляющих символов, выше которой «текст» считаем двоичным файлом. В настоящем тексте их нет,
в случайных байтах около 10 %."""
UTF8_MAX_CHAR_BYTES = 4
MIN_UTF16_PRINTABLE = 0.9
"""Текст в UTF-16 без BOM: доля печатных символов после декодирования должна быть не ниже этой."""

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_IMAGE_MAGIC = {
    b"\x89PNG\r\n\x1a\n": "PNG",
    b"\xff\xd8\xff": "JPEG",
    b"GIF87a": "GIF",
    b"GIF89a": "GIF",
    b"II*\x00": "TIFF",
    b"MM\x00*": "TIFF",
    b"BM": "BMP",
}
_PDF_HEADER = re.compile(rb"%PDF-\d\.\d")
_HTML = re.compile(rb"\s*(?:<!doctype\s+html|<html)", re.IGNORECASE)
_TEXT_CONTROLS = frozenset("\t\n\r\f\v\x1b")
_SPACE = 0x20
_DEL = 0x7F


class Sniffed(StrEnum):
    """Что лежит в файле по первым байтам."""

    PDF = "pdf"
    ZIP = "zip"
    OLE = "ole"
    RTF = "rtf"
    HTML = "html"
    IMAGE = "image"
    TEXT = "text"
    BINARY = "binary"


def image_format(head: bytes) -> str | None:
    """Название формата картинки по сигнатуре или None. BMP проверяем строже: «BM» бывает началом текста."""
    for magic, name in _IMAGE_MAGIC.items():
        if head.startswith(magic) and (name != "BMP" or b"\x00" in head[:16]):
            return name
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WebP"
    return None


def _control_share(text: str) -> float:
    if not text:
        return 0.0
    bad = sum(
        1
        for ch in text
        if ch not in _TEXT_CONTROLS and (ch == "�" or unicodedata.category(ch) in ("Cc", "Cs", "Co"))
    )
    return bad / len(text)


def _looks_utf16(head: bytes) -> bool:
    """UTF-16 без BOM: нули через байт. У кириллицы старший байт 0x04: судим по итогу декодирования."""
    even = head[: len(head) - len(head) % 2]
    for codec in ("utf-16-le", "utf-16-be"):
        text = even.decode(codec, errors="replace")
        printable = sum(1 for ch in text if ch.isprintable() or ch in _TEXT_CONTROLS)
        if (
            text
            and printable / len(text) >= MIN_UTF16_PRINTABLE
            and _control_share(text) <= MAX_CONTROL_SHARE
        ):
            return True
    return False


def _utf8_prefix(head: bytes) -> str | None:
    """Текст, если ``head`` это UTF-8; окно может обрезать последний многобайтовый символ."""
    try:
        return head.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data" and exc.start >= len(head) - UTF8_MAX_CHAR_BYTES:
            return head[: exc.start].decode("utf-8", errors="replace")
        return None


def _is_text(head: bytes) -> bool:
    if head.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")):
        return True
    if b"\x00" in head:
        return _looks_utf16(head)
    text = _utf8_prefix(head)
    if text is not None:
        return _control_share(text) <= MAX_CONTROL_SHARE
    # Однобайтовые кодировки (cp1251, koi8-r, cp866): буквы лежат в 0x80–0xFF, управляющие ниже 0x20.
    bad = sum(1 for byte in head if byte == _DEL or (byte < _SPACE and chr(byte) not in _TEXT_CONTROLS))
    return bad / len(head) <= MAX_CONTROL_SHARE


def sniff(head: bytes) -> Sniffed:
    """Тип по сигнатуре. Пустой ``head`` сюда не приходит: пустой файл отсекает ``guard``."""
    if _PDF_HEADER.search(head[:PDF_HEADER_WINDOW]):
        return Sniffed.PDF
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return Sniffed.ZIP
    if head.startswith(_OLE_MAGIC):
        return Sniffed.OLE
    if head.startswith(b"{\\rtf"):
        return Sniffed.RTF
    if image_format(head) is not None:
        return Sniffed.IMAGE
    if _HTML.match(head[:512]):
        return Sniffed.HTML
    return Sniffed.TEXT if _is_text(head) else Sniffed.BINARY
