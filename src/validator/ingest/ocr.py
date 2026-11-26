"""Распознавание (OCR) страниц PDF без текстового слоя (#38): PyMuPDF и языковые данные Tesseract.

OCR необязателен (у жюри Tesseract может не быть). PyMuPDF распознаёт встроенным в MuPDF движком
Tesseract, ему нужна только папка ``tessdata`` с языками ``rus`` и ``eng``: её находит
``pymupdf.get_tessdata`` (переменная ``TESSDATA_PREFIX`` или установленный ``tesseract``). Нет папки
или языка: скан даёт отчёт-диагностику с инструкцией (``SCAN_HELP``), а не падение.

Язык только ``rus+eng``: по умолчанию PyMuPDF берёт ``eng``, и русский скан превращается в латинскую
кашу (исследование 04, TL;DR п. 4: recall слов 0,90 при 300 dpi, около 3 с на страницу на CPU).
Распознанный текст идёт в тот же конвейер, что и обычный: символы с координатами (``Glyph``), из них
строки и абзацы. Якоря таких страниц помечены ``Extraction.OCR``, отчёт пишет «текст распознан (OCR)».
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from validator.ingest.pdf_backends import MUPDF_TEXT_FLAGS, Glyph, mupdf_glyphs

logger = logging.getLogger("validator")

OCR_ENV = "VALIDATOR_OCR"
"""``off`` выключает распознавание (как ``--no-ocr``); по умолчанию OCR идёт, если есть Tesseract."""
OCR_OFF_VALUES = frozenset({"off", "0", "false", "no"})
OCR_LANGUAGE = "rus+eng"
OCR_LANGUAGES = ("rus", "eng")
OCR_DPI = 300
"""Разрешение рендера страницы для OCR, как в замере исследования 04 (recall слов 0,90 с ``rus+eng``)."""
MAX_OCR_PAGES = 150
"""Больше стольких страниц не распознаём (около 10 минут): остальные перечисляются как непроверенные."""

SCAN_HELP = (
    "Лучше всего пришлите исходный документ: DOCX или PDF, сохранённый из Word или Google Docs "
    "(«Файл → Сохранить как → PDF»), в нём есть текст для цитат. Или включите распознавание: установите "
    "Tesseract с русским языком (Ubuntu: sudo apt install tesseract-ocr tesseract-ocr-rus; macOS: "
    "brew install tesseract tesseract-lang; Windows: установщик UB Mannheim с галочкой Russian) и "
    "запустите проверку снова, агент распознает страницы сам. Текстовый PDF из скана можно сделать и "
    "заранее: ocrmypdf -l rus+eng скан.pdf текст.pdf"
)
"""Что делать со сканом, если распознавания нет: текст для поля «что сделать» диагностики."""


@dataclass(frozen=True)
class OcrStatus:
    """Можно ли распознавать: папка языковых данных или причина, почему нельзя."""

    tessdata: str | None
    reason: str = ""

    @property
    def available(self) -> bool:
        return self.tessdata is not None

    def describe(self) -> str:
        return f"OCR Tesseract ({OCR_LANGUAGE}, {OCR_DPI} dpi)"


def ocr_enabled(flag: bool | None = None) -> bool:
    """Флаг ``--ocr/--no-ocr``, иначе переменная ``VALIDATOR_OCR``, иначе включено."""
    if flag is not None:
        return flag
    return os.environ.get(OCR_ENV, "").strip().lower() not in OCR_OFF_VALUES


def ocr_status(enabled: bool | None = None) -> OcrStatus:
    """Есть ли в системе Tesseract с русским и английским. Не бросает исключений.

    ``enabled``: решение флага; None значит «по переменной ``VALIDATOR_OCR``».
    """
    if not ocr_enabled(enabled):
        return OcrStatus(None, "распознавание выключено (--no-ocr или VALIDATOR_OCR=off)")
    try:
        tessdata = pymupdf.get_tessdata()
    except (RuntimeError, OSError):
        return OcrStatus(None, "Tesseract не установлен")
    missing = [lang for lang in OCR_LANGUAGES if not (Path(tessdata) / f"{lang}.traineddata").is_file()]
    if missing:
        return OcrStatus(None, f"у Tesseract нет языковых данных: {', '.join(missing)}")
    return OcrStatus(str(tessdata))


class OcrReader:
    """Распознаёт страницы PDF по номеру. Файл открывает PyMuPDF один раз и только если понадобилось.

    Нужен независимо от движка чтения (``mupdf`` или ``pdfium``): распознаёт MuPDF.
    """

    def __init__(self, path: Path, enabled: bool | None = None, status: OcrStatus | None = None) -> None:
        self.path = path
        self.enabled = enabled
        self.pages_done = 0
        self._status = status
        self._doc: pymupdf.Document | None = None

    @property
    def status(self) -> OcrStatus:
        """Проверка Tesseract только при первой странице, которую нужно распознать: обычный PDF её не ждёт."""
        if self._status is None:
            self._status = ocr_status(self.enabled)
        return self._status

    @property
    def consulted(self) -> bool:
        return self._status is not None

    @property
    def exhausted(self) -> bool:
        return self.pages_done >= MAX_OCR_PAGES

    def glyphs(self, number: int) -> list[Glyph]:
        """Символы распознанной страницы ``number`` (с 1); пустой список, если не вышло."""
        if self.status.tessdata is None or self.exhausted:
            return []
        self.pages_done += 1
        try:
            if self._doc is None:
                self._doc = pymupdf.open(self.path, filetype="pdf")
            page = self._doc[number - 1]
            textpage = page.get_textpage_ocr(
                flags=MUPDF_TEXT_FLAGS,
                language=OCR_LANGUAGE,
                dpi=OCR_DPI,
                full=True,
                tessdata=self.status.tessdata,
            )
            return mupdf_glyphs(page, textpage)
        except (RuntimeError, ValueError, OSError, IndexError) as exc:
            logger.warning("Стр. %d: распознавание не удалось (%s)", number, exc)
            return []

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None


__all__ = [
    "MAX_OCR_PAGES",
    "OCR_DPI",
    "OCR_ENV",
    "OCR_LANGUAGE",
    "SCAN_HELP",
    "OcrReader",
    "OcrStatus",
    "ocr_enabled",
    "ocr_status",
]
