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
from dataclasses import dataclass, field
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
MAX_RULE_THICKNESS = 2.5
"""Линия или прямоугольник тоньше стольких пунктов это линейка (черта под заголовком таблицы)."""
MIN_RULE_WIDTH = 100.0
"""Линейка уже стольких пунктов не годится в границу таблицы: это подчёркивание слова или рамка символа."""
_LINE_TO_SIZE = 1.2
_FALLBACK_SIZE = 10.0
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
class Rule:
    """Горизонтальная линейка страницы (в пунктах, ось Y вниз): граница таблицы, черта под её заголовком."""

    x0: float
    x1: float
    y: float


def make_rule(x0: float, x1: float, top: float, bottom: float) -> Rule | None:
    """Рамка рисованного объекта → линейка, если она тонкая и достаточно длинная; иначе None."""
    if bottom - top > MAX_RULE_THICKNESS or x1 - x0 < MIN_RULE_WIDTH:
        return None
    return Rule(x0, x1, (top + bottom) / 2)


@dataclass(frozen=True, slots=True)
class RawPage:
    number: int
    width: float
    height: float
    glyphs: list[Glyph]
    rules: list[Rule] = field(default_factory=list)
    images: bool = False
    """На странице есть картинка: страница без текста с картинкой похожа на скан (#38), без неё на пустую."""


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

    Рамки символов берём «с шириной глифа» (у pdfium ``loose``): внутри слова зазор около 0, обычный пробел
    0,2 кегля и больше (замер на ``requirements.pdf``). У рамки «по контуру глифа» зазор внутри слова доходит
    до 0,3 в узких буквах моноширинного шрифта, поэтому её не используем.
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
        for number, page in enumerate(self._doc, start=1):
            turn = page.rotation_matrix if page.rotation else None
            rect = page.rect
            glyphs = mupdf_glyphs(page)
            yield RawPage(
                number, rect.width, rect.height, glyphs, _mupdf_rules(page, turn), _mupdf_images(page)
            )

    def close(self) -> None:
        self._doc.close()


MUPDF_TEXT_FLAGS = (
    pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_LIGATURES & ~pymupdf.TEXT_PRESERVE_IMAGES
)
"""Стандартные флаги rawdict, но без лигатур (ﬁ → «fi»: цитата модели с лигатурой не совпала бы) и без
картинок (нужен только текст)."""


def mupdf_glyphs(page: pymupdf.Page, textpage: pymupdf.TextPage | None = None) -> list[Glyph]:
    """Символы страницы с координатами на экране. ``textpage``: готовый слой текста (OCR, #38)."""
    glyphs: list[Glyph] = []
    # Рамки символов лежат в неповёрнутой странице; для /Rotate переводим их в вид на экране.
    turn = page.rotation_matrix if page.rotation else None
    try:
        blocks = page.get_text("rawdict", flags=MUPDF_TEXT_FLAGS, textpage=textpage)["blocks"]
    except (RuntimeError, ValueError):
        return []  # страница не читается: она считается страницей без текста
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                bold, italic = _style(span["font"], span["flags"], _MUPDF_BOLD, _MUPDF_ITALIC)
                for ch in span["chars"]:
                    if not ch["c"].strip():
                        continue
                    box = pymupdf.Rect(ch["bbox"]) * turn if turn else pymupdf.Rect(ch["bbox"])
                    size = span["size"] or (box.y1 - box.y0) / _LINE_TO_SIZE or _FALLBACK_SIZE
                    glyphs.append(Glyph(ch["c"], box.x0, box.x1, box.y0, box.y1, size, bold, italic))
    return glyphs


def _mupdf_images(page: pymupdf.Page) -> bool:
    """Есть ли на странице картинки: ``get_image_info`` видит и встроенные (BI/ID/EI), и XObject."""
    try:
        return bool(page.get_images(full=False)) or bool(page.get_image_info())
    except (RuntimeError, ValueError):
        return False


def _mupdf_rules(page: pymupdf.Page, turn: pymupdf.Matrix | None) -> list[Rule]:
    """Тонкие длинные линии и прямоугольники страницы: у таблиц без рамки это черта под заголовком."""
    try:
        drawings = page.get_drawings()
    except (RuntimeError, ValueError):
        return []  # рисунки не читаются: таблицы без линий остаются обычным текстом
    rules: list[Rule] = []
    for drawing in drawings:
        box = pymupdf.Rect(drawing["rect"])
        box = box * turn if turn else box
        rule = make_rule(box.x0, box.x1, box.y0, box.y1)
        if rule is not None:
            rules.append(rule)
    return rules


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
                width, height = page.get_size()  # уже с учётом поворота страницы
                try:
                    glyphs = _pdfium_glyphs(page)
                except (pdfium.PdfiumError, RuntimeError, ValueError):
                    glyphs = []  # страница не читается: она считается страницей без текста
                yield RawPage(number + 1, width, height, glyphs, _pdfium_rules(page), _pdfium_images(page))
            finally:
                page.close()

    def close(self) -> None:
        self._doc.close()


def _rotate_box(
    box: tuple[float, float, float, float], width: float, height: float, rotation: int
) -> tuple[float, float, float, float]:
    """Рамка (left, bottom, right, top) без поворота, Y вверх → (x0, x1, top, bottom) на экране."""
    left, bottom, right, top = box
    corners = {
        0: ((left, height - top), (right, height - bottom)),
        90: ((bottom, left), (top, right)),
        180: ((width - right, bottom), (width - left, top)),
        270: ((height - top, width - right), (height - bottom, width - left)),
    }[rotation]
    (x0, y0), (x1, y1) = corners
    return x0, x1, y0, y1


def _combine_surrogates(codes: list[int]) -> Iterator[tuple[int, str]]:
    """Коды pdfium в символы: на некоторых платформах это половины суррогатной пары UTF-16."""
    i = 0
    while i < len(codes):
        code = codes[i]
        if 0xD800 <= code < 0xDC00 and i + 1 < len(codes) and 0xDC00 <= codes[i + 1] < 0xE000:
            yield i, chr(0x10000 + ((code - 0xD800) << 10) + (codes[i + 1] - 0xDC00))
            i += 2
            continue
        yield i, "\ufffd" if 0xD800 <= code < 0xE000 else chr(code)
        i += 1


def _pdfium_glyphs(page: pdfium.PdfPage) -> list[Glyph]:
    left0, bottom0, right0, top0 = page.get_mediabox()
    width, height = right0 - left0, top0 - bottom0
    rotation = page.get_rotation() % 360
    textpage = page.get_textpage()
    glyphs: list[Glyph] = []
    font = ctypes.create_string_buffer(_PDFIUM_FONT_NAME_BYTES)
    flags = ctypes.c_int()
    try:
        raw_page = textpage.raw
        count = textpage.count_chars()
        codes = [pdfium_raw.FPDFText_GetUnicode(raw_page, i) for i in range(count)]
        for i, symbol in _combine_surrogates(codes):
            # Дефис на конце строки pdfium отдаёт служебным кодом (\x02) с признаком «hyphen».
            char = "-" if pdfium_raw.FPDFText_IsHyphen(raw_page, i) else symbol
            # Пробелы pdfium дописывает сам (generated): пробел решаем по зазору, а не по ним.
            if char.isspace() or pdfium_raw.FPDFText_IsGenerated(raw_page, i):
                continue
            box_l, box_b, box_r, box_t = textpage.get_charbox(i, loose=True)
            x0, x1, top, bottom = _rotate_box(
                (box_l - left0, box_b - bottom0, box_r - left0, box_t - bottom0), width, height, rotation
            )
            pdfium_raw.FPDFText_GetFontInfo(raw_page, i, font, _PDFIUM_FONT_NAME_BYTES, flags)
            bold, italic = _style(
                font.value.decode("latin-1"), flags.value, 0x40000, 0x40
            )  # ForceBold, Italic
            bold = bold or pdfium_raw.FPDFText_GetFontWeight(raw_page, i) >= _PDFIUM_BOLD_WEIGHT
            size = (
                pdfium_raw.FPDFText_GetFontSize(raw_page, i)
                or abs(bottom - top) / _LINE_TO_SIZE
                or _FALLBACK_SIZE
            )
            glyphs.append(Glyph(char, x0, x1, top, bottom, size, bold, italic))
    finally:
        textpage.close()
    return glyphs


def _pdfium_rules(page: pdfium.PdfPage) -> list[Rule]:
    """Тонкие длинные пути страницы (границы объектов), как ``_mupdf_rules``."""
    left0, bottom0, right0, top0 = page.get_mediabox()
    width, height = right0 - left0, top0 - bottom0
    rotation = page.get_rotation() % 360
    rules: list[Rule] = []
    try:
        for obj in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_PATH]):
            box_l, box_b, box_r, box_t = obj.get_bounds()
            x0, x1, top, bottom = _rotate_box(
                (box_l - left0, box_b - bottom0, box_r - left0, box_t - bottom0), width, height, rotation
            )
            rule = make_rule(x0, x1, top, bottom)
            if rule is not None:
                rules.append(rule)
    except (pdfium.PdfiumError, RuntimeError, ValueError):
        return []  # объекты не читаются: таблицы без линий остаются обычным текстом
    return rules


def _pdfium_images(page: pdfium.PdfPage) -> bool:
    try:
        return next(page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]), None) is not None
    except (pdfium.PdfiumError, RuntimeError, ValueError):
        return False


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
