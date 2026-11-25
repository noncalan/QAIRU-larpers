"""Глубокие проверки контейнеров до разбора (шаг 0): ZIP (DOCX), OLE (пароль Office, старый .doc) и PDF.

Рецепты и коды по исследованию 04, раздел 3 (строки 4, 9–12, 16–21 матрицы отказов). Каждая проверка
отвечает ``Probe``: формат для разборщика или ``None`` (читать нельзя) и диагностику по-русски. Исключения
библиотек ловим здесь и превращаем в диагностику: битый файл не должен ронять агента.

Почему zip-бомбу хватает поймать по заголовкам: ``zipfile`` при распаковке не выдаёт больше байт, чем
записано в центральном каталоге (``file_size``), а при расхождении бросает ``BadZipFile`` по CRC.
Значит, подделанный маленький размер не раздует память разборщика, а честный большой мы отклоняем
до разбора, не распаковывая ни байта.
"""

from __future__ import annotations

import io
import logging
import re
import struct
import tempfile
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import olefile
import pikepdf
import pymupdf

from validator.errors import (
    IN_DOCX_BROKEN,
    IN_LEGACY_DOC,
    IN_OFFICE_ENCRYPTED,
    IN_PDF_BROKEN,
    IN_PDF_ENCRYPTED,
    IN_PDF_NO_PAGES,
    IN_PDF_REPAIRED,
    IN_PDF_RESTRICTED,
    IN_PDF_SALVAGED,
    IN_UNSUPPORTED,
    IN_ZIP_BOMB,
    IN_ZIP_NOT_DOCX,
)
from validator.models import DiagLevel, Diagnostic, DocKind

logger = logging.getLogger("validator.guard")

MAX_UNPACKED_BYTES = 200 * 1024 * 1024
"""Сумма распакованных размеров DOCX, выше которой архив отклоняем (на практике ТЗ занимает единицы МБ)."""
MAX_COMPRESSION_RATIO = 100
"""Коэффициент сжатия, выше которого архив похож на бомбу. У обычного DOCX он 3–20."""
MIN_RATIO_CHECK_BYTES = 20 * 1024 * 1024
"""Коэффициент проверяем только у архивов больше этого: маленький XML с повторами честно жмётся сильно."""
MAX_ZIP_ENTRIES = 10_000
"""Столько частей в DOCX не бывает; больше это атака на разборщик числом файлов."""
CONTENT_TYPES_LIMIT = 1024 * 1024
"""Сколько читать из ``[Content_Types].xml``: на деле там единицы КБ."""
SALVAGE_SCAN_BYTES = 400
"""Сколько байт после «N 0 obj» смотреть, чтобы узнать объект страницы."""
SALVAGED_PREFIX = "salvaged-"

_DOCX_MAIN = "wordprocessingml.document.main+xml"
_ZIP_KINDS = (
    ("wordprocessingml.template.main", "шаблон Word (.dotx)"),
    ("ms-word.document.macroEnabled", "документ Word с макросами (.docm)"),
    ("ms-word.template.macroEnabled", "шаблон Word с макросами (.dotm)"),
    ("spreadsheetml", "таблица Excel (.xlsx)"),
    ("presentationml", "презентация PowerPoint (.pptx)"),
)
_ODF_KINDS = {
    "application/vnd.oasis.opendocument.text": "документ OpenDocument (.odt)",
    "application/vnd.oasis.opendocument.spreadsheet": "таблица OpenDocument (.ods)",
    "application/vnd.oasis.opendocument.presentation": "презентация OpenDocument (.odp)",
}
_OLE_KINDS = {
    "Workbook": "таблица Excel старого формата (.xls)",
    "Book": "таблица Excel старого формата (.xls)",
    "PowerPoint Document": "презентация PowerPoint старого формата (.ppt)",
}
_ZIP_ENCRYPTED_FLAG = 0x1
_ZIP_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    struct.error,
    EOFError,
    OSError,
    NotImplementedError,
    RuntimeError,
    ValueError,
)
_SAVE_AS_DOCX = "Откройте файл в Word или LibreOffice и сохраните как DOCX или PDF"
_NOT_READ = "Проверка не выполнялась: файл не прочитан"

_OBJ = re.compile(rb"(\d+) 0 obj")
_PAGE_OBJ = re.compile(rb"\d+ 0 obj\s*<<[^>]*?/Type\s*/Page[^s]", re.DOTALL)


@dataclass(frozen=True)
class Probe:
    """Итог проверки контейнера: ``kind`` для разборщика или ``None``, если читать нельзя."""

    kind: DocKind | None
    diagnostics: list[Diagnostic] = field(default_factory=list)
    read_path: Path | None = None


def input_diag(
    code: str, level: DiagLevel, message: str, action: str, hint: str = "", location: str | None = None
) -> Diagnostic:
    """Диагностика шага 0: что увидели, что сделал агент, что сделать человеку."""
    return Diagnostic(
        code=code, level=level, message=message, action=action, hint=hint, location=location, step="guard"
    )


def reject(code: str, message: str, hint: str, location: str | None = None) -> Probe:
    """Файл читать нельзя: одна ошибка, проверка не выполняется (код возврата 4)."""
    return Probe(
        kind=None, diagnostics=[input_diag(code, DiagLevel.ERROR, message, _NOT_READ, hint, location)]
    )


def _mib(size: int) -> str:
    return f"{size / 2**20:.0f} МБ"


# ---------------------------------------------------------------- ZIP (DOCX)


def _zip_kind(archive: zipfile.ZipFile, names: set[str]) -> str | None:
    """None для настоящего DOCX, иначе человеческое название того, что лежит в архиве."""
    if "[Content_Types].xml" in names:
        with archive.open("[Content_Types].xml") as stream:
            types = stream.read(CONTENT_TYPES_LIMIT).decode("utf-8", errors="replace")
        if _DOCX_MAIN in types:
            return None
        return next(
            (label for marker, label in _ZIP_KINDS if marker in types), "архив Office неизвестного вида"
        )
    if "mimetype" in names:
        with archive.open("mimetype") as stream:
            mimetype = stream.read(200).decode("ascii", errors="replace").strip()
        return _ODF_KINDS.get(mimetype, "документ OpenDocument")
    return "обычный ZIP-архив"


def probe_zip(path: Path) -> Probe:
    """DOCX: целость архива, zip-бомба по заголовкам, пароль, что внутри действительно документ Word."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ZIP_ENTRIES:
                return reject(
                    IN_ZIP_BOMB,
                    f"В архиве {len(infos)} частей, в документе Word их бывает десятки: файл отклонён",
                    _SAVE_AS_DOCX,
                )
            unpacked = sum(i.file_size for i in infos)
            packed = sum(i.compress_size for i in infos) or 1
            ratio = unpacked / packed
            if unpacked > MAX_UNPACKED_BYTES or (
                unpacked > MIN_RATIO_CHECK_BYTES and ratio > MAX_COMPRESSION_RATIO
            ):
                return reject(
                    IN_ZIP_BOMB,
                    f"Архив разворачивается в {_mib(unpacked)} (сжатие ×{ratio:.0f}): похоже на zip-бомбу, "
                    "файл отклонён без распаковки",
                    _SAVE_AS_DOCX,
                )
            if any(i.flag_bits & _ZIP_ENCRYPTED_FLAG for i in infos):
                return reject(
                    IN_OFFICE_ENCRYPTED, "Архив защищён паролем", "Снимите пароль и запустите проверку снова"
                )
            kind = _zip_kind(archive, {i.filename for i in infos})
    except _ZIP_ERRORS as exc:
        logger.info("ZIP не читается: %r", exc)
        return reject(
            IN_DOCX_BROKEN,
            "Файл похож на DOCX, но ZIP-структура повреждена (файл обрезан или испорчен)",
            "Пересохраните документ в Word или пришлите PDF",
        )
    if kind is not None:
        return reject(IN_ZIP_NOT_DOCX, f"Это ZIP-архив, но не документ Word: {kind}", _SAVE_AS_DOCX)
    return Probe(kind=DocKind.DOCX)


# ---------------------------------------------------------------- OLE (.doc, DOCX с паролем)


def probe_ole(path: Path) -> Probe:
    """Контейнер OLE: DOCX с паролем (``EncryptedPackage``), старый .doc или старые Excel и PowerPoint."""
    try:
        with olefile.OleFileIO(str(path)) as ole:
            streams = {"/".join(entry) for entry in ole.listdir()}
    except (OSError, ValueError, IndexError, struct.error) as exc:  # OleFileError наследует OSError
        logger.info("OLE не читается: %r", exc)
        return reject(IN_UNSUPPORTED, "Файл Office старого формата повреждён и не читается", _SAVE_AS_DOCX)
    if "EncryptedPackage" in streams:
        return reject(
            IN_OFFICE_ENCRYPTED,
            "Документ Office защищён паролем на открытие",
            "Снимите пароль (Word: Файл → Сведения → Защита документа) и запустите проверку снова",
        )
    if "WordDocument" in streams:
        return reject(
            IN_LEGACY_DOC,
            "Старый формат Word (.doc) не поддерживается",
            "Сохраните документ как DOCX или PDF (Word: Файл → Сохранить как)",
        )
    label = next((label for stream, label in _OLE_KINDS.items() if stream in streams), None)
    return reject(
        IN_UNSUPPORTED, f"Это не документ Word: {label or 'файл OLE неизвестного вида'}", _SAVE_AS_DOCX
    )


# ---------------------------------------------------------------- PDF


def salvage_pdf(data: bytes, target: Path) -> int:
    """Спасение обрезанного PDF: найти объекты страниц, дописать дерево страниц и trailer, пересохранить.

    Работает, когда страницы и шрифты записаны раньше точки обрыва (так пишут LibreOffice и многие
    генераторы). Возвращает число страниц в ``target`` или 0, если спасти нечего.
    """
    objects = {int(m.group(1)): m.start() for m in _OBJ.finditer(data)}
    pages = sorted(
        n for n, offset in objects.items() if _PAGE_OBJ.match(data, offset, offset + SALVAGE_SCAN_BYTES)
    )
    if not pages:
        return 0
    top = max(objects) + 1
    kids = " ".join(f"{n} 0 R" for n in pages)
    tail = (
        f"\nendobj\n{top} 0 obj << /Type /Pages /Kids [{kids}] /Count {len(pages)} >> endobj\n"
        f"{top + 1} 0 obj << /Type /Catalog /Pages {top} 0 R >> endobj\n"
        f"trailer << /Root {top + 1} 0 R /Size {top + 2} >>\n%%EOF\n"
    ).encode()
    with pikepdf.open(io.BytesIO(data + tail)) as pdf:  # qpdf сам перестроит таблицу ссылок
        for page in pdf.pages:
            page.obj.Parent = pdf.Root.Pages
        pdf.save(target)
    with pymupdf.open(target, filetype="pdf") as fixed:
        return int(fixed.page_count)


def _salvage_or_reject(path: Path, workdir: Path | None, code: str, message: str) -> Probe:
    hint = "Пришлите полный файл или пересохраните PDF из исходника"
    if workdir is None:
        return reject(code, message, hint)
    # Своё уникальное имя: восстановленная копия никогда не затрёт входной файл, даже если он лежит в workdir.
    target: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=workdir, prefix=SALVAGED_PREFIX, suffix=".pdf", delete=False
        ) as tmp:
            target = Path(tmp.name)
        pages = salvage_pdf(path.read_bytes(), target)
    except (
        pikepdf.PdfError,
        pymupdf.FileDataError,
        RuntimeError,
        ValueError,
        OSError,
        KeyError,
        TypeError,
        AttributeError,
    ) as exc:
        logger.info("Спасение PDF не удалось: %r", exc)
        pages = 0
    if not pages or target is None:
        if target is not None:
            target.unlink(missing_ok=True)
        return reject(code, f"{message}; восстановить страницы не удалось", hint)
    diag = input_diag(
        IN_PDF_SALVAGED,
        DiagLevel.WARNING,
        f"{message}. Восстановлено {pages} стр.; сколько страниц было в оригинале, неизвестно",
        "Проверка идёт по восстановленным страницам. Порядок страниц взят по внутренней нумерации файла "
        "и может отличаться от оригинала",
        "Пришлите полный файл и сверьте число страниц с оригиналом",
    )
    return Probe(kind=DocKind.PDF, diagnostics=[diag], read_path=target)


def probe_pdf(path: Path, workdir: Path | None = None) -> Probe:
    """PDF: пароль, ограничения владельца, ремонт, страницы. Обрезанный файл пробуем спасти в ``workdir``."""
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except (pymupdf.FileDataError, RuntimeError, ValueError) as exc:
        logger.info("PDF не открывается: %r", exc)
        return _salvage_or_reject(
            path, workdir, IN_PDF_BROKEN, "PDF не открывается: файл обрезан или повреждён"
        )
    with doc:
        if doc.needs_pass:
            return reject(
                IN_PDF_ENCRYPTED,
                "PDF защищён паролем на открытие",
                "Снимите пароль (сохраните копию без защиты) и запустите проверку снова",
            )
        pages, repaired, restricted = (
            doc.page_count,
            doc.is_repaired,
            not doc.permissions & pymupdf.PDF_PERM_COPY,
        )
    if pages == 0:
        return _salvage_or_reject(path, workdir, IN_PDF_NO_PAGES, "В PDF не найдено ни одной страницы")
    diagnostics: list[Diagnostic] = []
    if restricted:
        diagnostics.append(
            input_diag(
                IN_PDF_RESTRICTED,
                DiagLevel.INFO,
                "PDF с ограничениями владельца: копирование текста запрещено",
                "Текст извлечён только для проверки, документ не изменялся",
            )
        )
    if repaired:
        diagnostics.append(
            input_diag(
                IN_PDF_REPAIRED,
                DiagLevel.INFO,
                f"Таблица ссылок PDF повреждена, файл восстановлен автоматически ({pages} стр.)",
                "Проверка идёт по восстановленному файлу. Если страниц без текста нет, "
                "текст прочитан целиком",
                "Пересохраните PDF из исходника, если число страниц не совпадает с оригиналом",
            )
        )
    return Probe(kind=DocKind.PDF, diagnostics=diagnostics)
