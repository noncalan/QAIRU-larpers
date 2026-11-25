"""Движки чтения PDF за одним интерфейсом: ``mupdf`` (PyMuPDF, AGPL) и ``pdfium`` (pypdfium2, без копилефта).

Причина в исследовании 04, «AGPL: проблема ли для нас»: для открытого репозитория хакатона AGPL не мешает,
а банку-заказчику нужен вариант без копилефта. Поэтому движок отдаёт только **символы с координатами**
(``Glyph``), а сборку строк, абзацев, заголовков и колонтитулов делает общий код ``pdf_layout``.
Так оба бэкенда дают один и тот же канонический текст с точностью до различий самих парсеров.

Символы, а не готовый текст, нужны из-за PDF, где знаки препинания и пробелы лежат отдельными
фрагментами другого шрифта: ``get_text("text", sort=True)`` ставит пробел перед знаком («в <город> .»).
Пробел между символами решает геометрия: зазор больше доли кегля ``space_gap_em``.
"""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import pymupdf
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw

from validator.errors import IN_PDF_BROKEN, IN_PDF_ENCRYPTED, InputError

BACKEND_MUPDF = "mupdf"
BACKEND_PDFIUM = "pdfium"
DEFAULT_BACKEND = BACKEND_MUPDF
BACKEND_NAMES = (BACKEND_MUPDF, BACKEND_PDFIUM)

_PDFIUM_PASSWORD_ERROR = 4  # FPDF_ERR_PASSWORD
_MUPDF_ITALIC = 2
_MUPDF_BOLD = 16
_PDFIUM_FONT_NAME_BYTES = 128
_PDFIUM_BOLD_WEIGHT = 700
_BOLD_NAMES = ("bold", "black", "heavy", "semibold", "demi")
_ITALIC_NAMES = ("italic", "oblique")


@dataclass(frozen=True, slots=True)
class Glyph:
    """Один видимый символ (или лигатура) страницы. Координаты в пунктах, ось Y вниз, 0 вверху."""

    char: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool = False
    italic: bool = False


@dataclass(frozen=True, slots=True)
class RawPage:
    number: int
    width: float
    height: float
    glyphs: list[Glyph]


class PdfSource(Protocol):
    """Открытый PDF: страницы по одной, чтобы документ в сотни страниц не жил в памяти целиком."""

    page_count: int
    repaired: bool

    def pages(self) -> Iterator[RawPage]: ...

    def close(self) -> None: ...


class PdfBackend(Protocol):
    name: str
    space_gap_em: float
    """Зазор между символами в долях кегля, начиная с которого считаем пробел.

    У PyMuPDF рамка символа включает ширину глифа, зазор внутри слова около 0, пробел 0,2 кегля и больше.
    У pdfium рамка по контуру глифа: внутри слова до 0,12, пробел от 0,25 (замер на ``requirements.pdf``).
    """

    def describe(self) -> str: ...

    def open(self, path: Path) -> PdfSource: ...


def _style(font: str, flags: int, bold_flag: int, italic_flag: int) -> tuple[bool, bool]:
    low = font.lower()
    bold = bool(flags & bold_flag) or any(name in low for name in _BOLD_NAMES)
    italic = bool(flags & italic_flag) or any(name in low for name in _ITALIC_NAMES)
    return bold, italic


# ---------------------------------------------------------------- PyMuPDF


class _MuPdfSource:
    def __init__(self, doc: pymupdf.Document) -> None:
        self._doc = doc
        self.page_count: int = doc.page_count
        self.repaired: bool = bool(doc.is_repaired)

    def pages(self) -> Iterator[RawPage]:
        # Без PRESERVE_LIGATURES лигатура «ﬁ» приходит как «fi»: цитата модели с ней не совпала бы.
        flags = pymupdf.TEXT_MEDIABOX_CLIP
        for number, page in enumerate(self._doc, start=1):
            glyphs: list[Glyph] = []
            for block in page.get_text("rawdict", flags=flags)["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        bold, italic = _style(span["font"], span["flags"], _MUPDF_BOLD, _MUPDF_ITALIC)
                        for ch in span["chars"]:
                            if ch["c"].strip():
                                x0, top, x1, bottom = ch["bbox"]
                                glyphs.append(Glyph(ch["c"], x0, x1, top, bottom, span["size"], bold, italic))
            rect = page.rect
            yield RawPage(number, rect.width, rect.height, glyphs)

    def close(self) -> None:
        self._doc.close()


class MuPdfBackend:
    name = BACKEND_MUPDF
    space_gap_em = 0.10

    def describe(self) -> str:
        return f"PyMuPDF {pymupdf.VersionBind}"

    def open(self, path: Path) -> PdfSource:
        try:
            doc = pymupdf.open(path, filetype="pdf")
        except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
            raise _broken(exc) from exc
        if doc.needs_pass:
            doc.close()
            raise _encrypted()
        return _MuPdfSource(doc)


# ---------------------------------------------------------------- pdfium


class _PdfiumSource:
    def __init__(self, doc: pdfium.PdfDocument) -> None:
        self._doc = doc
        self.page_count: int = len(doc)
        self.repaired = False  # pdfium о ремонте не сообщает

    def pages(self) -> Iterator[RawPage]:
        for number in range(self.page_count):
            page = self._doc[number]
            try:
                width, height = page.get_size()
                yield RawPage(number + 1, width, height, _pdfium_glyphs(page, height))
            finally:
                page.close()

    def close(self) -> None:
        self._doc.close()


def _pdfium_glyphs(page: pdfium.PdfPage, height: float) -> list[Glyph]:
    textpage = page.get_textpage()
    glyphs: list[Glyph] = []
    font = ctypes.create_string_buffer(_PDFIUM_FONT_NAME_BYTES)
    flags = ctypes.c_int()
    try:
        raw_page = textpage.raw
        for i in range(textpage.count_chars()):
            code = pdfium_raw.FPDFText_GetUnicode(raw_page, i)
            # Дефис на конце строки pdfium отдаёт служебным кодом (\x02) с признаком «hyphen».
            char = "-" if pdfium_raw.FPDFText_IsHyphen(raw_page, i) else chr(code)
            # Пробелы pdfium дописывает сам (generated): пробел решаем по зазору, а не по ним.
            if char.isspace() or pdfium_raw.FPDFText_IsGenerated(raw_page, i):
                continue
            left, bottom, right, top = textpage.get_charbox(i, loose=True)
            pdfium_raw.FPDFText_GetFontInfo(raw_page, i, font, _PDFIUM_FONT_NAME_BYTES, flags)
            bold, italic = _style(
                font.value.decode("latin-1"), flags.value, 0x40000, 0x40
            )  # ForceBold, Italic (PDF 32000, 9.8.2)
            bold = bold or pdfium_raw.FPDFText_GetFontWeight(raw_page, i) >= _PDFIUM_BOLD_WEIGHT
            size = pdfium_raw.FPDFText_GetFontSize(raw_page, i)
            glyphs.append(Glyph(char, left, right, height - top, height - bottom, size, bold, italic))
    finally:
        textpage.close()
    return glyphs


class PdfiumBackend:
    name = BACKEND_PDFIUM
    space_gap_em = 0.10

    def describe(self) -> str:
        return f"pypdfium2 {pdfium.version.PYPDFIUM_INFO}"

    def open(self, path: Path) -> PdfSource:
        try:
            doc = pdfium.PdfDocument(path)
        except pdfium.PdfiumError as exc:
            if getattr(exc, "err_code", None) == _PDFIUM_PASSWORD_ERROR:
                raise _encrypted() from exc
            raise _broken(exc) from exc
        except (RuntimeError, ValueError, OSError) as exc:
            raise _broken(exc) from exc
        return _PdfiumSource(doc)


# ---------------------------------------------------------------- общее


def _broken(exc: Exception) -> InputError:
    return InputError(
        f"PDF не открывается: {type(exc).__name__}",
        code=IN_PDF_BROKEN,
        hint="Пересохраните PDF или пришлите исходник в DOCX или Markdown",
    )


def _encrypted() -> InputError:
    return InputError(
        "PDF защищён паролем на открытие",
        code=IN_PDF_ENCRYPTED,
        hint="Снимите пароль и запустите проверку заново",
    )


_BACKENDS: dict[str, PdfBackend] = {BACKEND_MUPDF: MuPdfBackend(), BACKEND_PDFIUM: PdfiumBackend()}


def get_backend(name: str | None = None) -> PdfBackend:
    """Движок по имени из ``RunConfig.pdf_backend``; неизвестное имя это ошибка настройки."""
    key = (name or DEFAULT_BACKEND).lower()
    try:
        return _BACKENDS[key]
    except KeyError:
        raise ValueError(f"Неизвестный движок PDF «{name}», доступны: {', '.join(BACKEND_NAMES)}") from None
