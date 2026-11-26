"""Генератор битых и пограничных входов: фикстуры ``tests/fixtures/broken/`` и большие файлы для тестов.

Перегенерировать фикстуры в репозитории (маленькие файлы, до ~150 КБ каждый):

    uv run python tests/broken_factory.py            # в tests/fixtures/broken/
    uv run python tests/broken_factory.py --big DIR  # плюс zip-бомба (≈250 КБ на диске, 300 МБ внутри)

Источник: ``data/input/requirements.*`` и маленькие синтетические документы. Рецепты по исследованию 04,
раздел 5 (``experiments/04-parsing-lab/make_fixtures.py``). Ожидаемые код возврата и диагностика
каждого файла лежат в ``CASES``: по ним идут тесты и таблица в ``tests/fixtures/broken/README.md``.
"""

from __future__ import annotations

import io
import random
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import msoffcrypto
import pikepdf
import pymupdf

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "data" / "input"
BROKEN_DIR = ROOT / "tests" / "fixtures" / "broken"

TRUNCATE_SOFT = 0.6
"""Обрезка PDF, после которой страницы ещё можно спасти (объекты страниц записаны раньше обрыва)."""
TRUNCATE_HARD = 0.05
"""Обрезка PDF, после которой от документа остался только заголовок и шрифты."""
BOMB_UNPACKED = 300 * 1024 * 1024
SCAN_DPI = 60

SMALL_REQUIREMENTS = (
    "The system shall store the user settings for 30 days.",
    "The system shall export the report in PDF within 5 seconds.",
)
RU_REQUIREMENTS = (
    "# Требования к сервису уведомлений\n\n"
    "## Функциональные требования\n\n"
    "* **FR-1.** Система должна отправлять уведомление не позднее 5 секунд после события.\n"
    "* **FR-2.** Пользователь должен иметь возможность отключить уведомления в настройках.\n"
)
NOT_REQUIREMENTS = (
    "# Осенний дневник\n\n"
    + (
        "Было раннее утро, когда Анна вышла из дома и направилась к реке. Туман лежал над водой, "
        "и где-то вдалеке кричали птицы. Она шла медленно, вспоминая лето и старый сад у бабушки. "
    )
    * 40
)


@dataclass(frozen=True)
class Case:
    """Битый вход: как сделать, какой код возврата и какая диагностика ожидаются."""

    make: Callable[[], bytes]
    exit_code: int
    code: str
    what: str
    big: bool = False


def _pdf(pages: list[str]) -> bytes:
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        return bytes(doc.tobytes())


def _source_pdf() -> bytes:
    return (INPUT / "requirements.pdf").read_bytes()


def _source_docx() -> bytes:
    return (INPUT / "requirements.docx").read_bytes()


def _first_pages(data: bytes, count: int) -> pikepdf.Pdf:
    pdf = pikepdf.open(io.BytesIO(data))
    del pdf.pages[count:]
    return pdf


def _save(pdf: pikepdf.Pdf, **kwargs: object) -> bytes:
    out = io.BytesIO()
    pdf.save(out, **kwargs)
    return out.getvalue()


def _truncated(share: float) -> Callable[[], bytes]:
    return lambda: _source_pdf()[: int(len(_source_pdf()) * share)]


def _encrypted_pdf() -> bytes:
    with pikepdf.open(io.BytesIO(_pdf(list(SMALL_REQUIREMENTS)))) as pdf:
        return _save(pdf, encryption=pikepdf.Encryption(user="secret", owner="owner", R=6))


def _owner_only_pdf() -> bytes:
    with pikepdf.open(io.BytesIO(_pdf(list(SMALL_REQUIREMENTS)))) as pdf:
        allow = pikepdf.Permissions(extract=False, accessibility=False)
        return _save(pdf, encryption=pikepdf.Encryption(user="", owner="owner", R=6, allow=allow))


def _scan_page(doc: pymupdf.Document, page: pymupdf.Page) -> None:
    pix = page.get_pixmap(dpi=SCAN_DPI, colorspace=pymupdf.csGRAY)
    target = doc.new_page(width=page.rect.width, height=page.rect.height)
    target.insert_image(target.rect, stream=pix.tobytes("png"))


def _scan(text_pages: int) -> Callable[[], bytes]:
    """Первые две страницы требований: ``text_pages`` текстом, остальные картинкой без текстового слоя."""

    def make() -> bytes:
        with pymupdf.open(stream=_source_pdf(), filetype="pdf") as src, pymupdf.open() as out:
            for number in range(2):
                if number < text_pages:
                    out.insert_pdf(src, from_page=number, to_page=number)
                else:
                    _scan_page(out, src[number])
            return bytes(out.tobytes(garbage=4, deflate=True))

    return make


def _garbled_pdf() -> bytes:
    """Шрифты без ``/ToUnicode``: текстовый слой есть, но извлекаются управляющие символы."""
    with _first_pages(_source_pdf(), 2) as pdf:
        for page in pdf.pages:
            for _, font in (page.Resources.get("/Font") or {}).items():
                if "/ToUnicode" in font:
                    del font["/ToUnicode"]
        return _save(pdf)


def _bad_xref_pdf() -> bytes:
    data = bytearray(_pdf(list(SMALL_REQUIREMENTS)))
    at = data.rfind(b"startxref") + len(b"startxref\n")
    end = data.find(b"\n", at)
    data[at:end] = b"9" * (end - at)  # смещение таблицы ссылок указывает в никуда
    return bytes(data)


def _encrypted_docx() -> bytes:
    out = io.BytesIO()
    msoffcrypto.OfficeFile(io.BytesIO(_source_docx())).encrypt("secret", out)
    return out.getvalue()


def _spreadsheet_zip() -> bytes:
    types = (
        '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types"><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-'
        'officedocument.spreadsheetml.sheet.main+xml"/></Types>'
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return out.getvalue()


def _png() -> bytes:
    with pymupdf.open(stream=_pdf(list(SMALL_REQUIREMENTS)), filetype="pdf") as doc:
        return bytes(doc[0].get_pixmap(dpi=30).tobytes("png"))


def _binary() -> bytes:
    return random.Random(27).randbytes(4096)  # noqa: S311 - фикстура, не криптография


def zip_bomb() -> bytes:
    """Настоящий DOCX, в котором ``document.xml`` разворачивается в 300 МБ пробелов (≈300 КБ на диске)."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(_source_docx())) as src,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as dst,
    ):
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename != "word/document.xml":
                dst.writestr(item, data)
                continue
            cut = data.find(b"<w:body>") + len(b"<w:body>")
            with dst.open("word/document.xml", "w", force_zip64=True) as stream:
                stream.write(data[:cut])
                chunk = b" " * (1 << 20)
                for _ in range(BOMB_UNPACKED // len(chunk)):
                    stream.write(chunk)
                stream.write(data[cut:])
    return out.getvalue()


CASES: dict[str, Case] = {
    "empty.pdf": Case(lambda: b"", 4, "IN-002", "файл нулевой длины"),
    "header_only.pdf": Case(lambda: b"%PDF-1.7\n", 4, "IN-040", "от PDF остался только заголовок"),
    "truncated.pdf": Case(_truncated(TRUNCATE_SOFT), 3, "IN-043", "PDF обрезан на 60 %, страницы спасены"),
    "truncated_hard.pdf": Case(_truncated(TRUNCATE_HARD), 4, "IN-042", "PDF обрезан на 5 %, страниц нет"),
    "bad_xref.pdf": Case(_bad_xref_pdf, 0, "IN-041", "битая ссылка на таблицу xref, PDF восстановлен"),
    "password.pdf": Case(_encrypted_pdf, 4, "IN-020", "PDF с паролем на открытие"),
    "owner_only.pdf": Case(_owner_only_pdf, 0, "IN-022", "PDF с паролем владельца (запрет копирования)"),
    "scan.pdf": Case(_scan(0), 4, "IN-055", "скан: страницы-картинки без текстового слоя, OCR выключен"),
    "mixed_scan.pdf": Case(_scan(1), 3, "IN-051", "стр. 1 текстом, стр. 2 сканом"),
    "garbled_text.pdf": Case(_garbled_pdf, 4, "IN-052", "мусорный текстовый слой (шрифты без ToUnicode)"),
    "image.pdf": Case(_png, 4, "IN-010", "картинка PNG с расширением .pdf"),
    "password.docx": Case(_encrypted_docx, 4, "IN-021", "DOCX с паролем (контейнер OLE)"),
    "truncated.docx": Case(lambda: _source_docx()[: len(_source_docx()) // 2], 4, "IN-032", "DOCX обрезан"),
    "spreadsheet.docx": Case(_spreadsheet_zip, 4, "IN-031", "таблица Excel с расширением .docx"),
    "pdf_named.docx": Case(lambda: _pdf(list(SMALL_REQUIREMENTS)), 0, "IN-011", "PDF с расширением .docx"),
    "text_named.pdf": Case(RU_REQUIREMENTS.encode, 4, "IN-010", "обычный текст с расширением .pdf"),
    "binary.md": Case(_binary, 4, "IN-010", "двоичный мусор как .md"),
    "utf16.md": Case(lambda: RU_REQUIREMENTS.encode("utf-16"), 0, "IN-061", "Markdown в UTF-16"),
    "headings_only.md": Case(
        lambda: b"# \xd0\xa2\xd0\x97\n\n## 1. Goals\n\n## 2. Requirements\n", 3, "IN-081", "пустой шаблон"
    ),
    "not_requirements.md": Case(
        NOT_REQUIREMENTS.encode, 3, "IN-080", "художественный текст вместо требований"
    ),
    "zip_bomb.docx": Case(zip_bomb, 4, "IN-030", "zip-бомба: 300 МБ внутри DOCX", big=True),
}


def write(target: Path, *, big: bool = False) -> list[Path]:
    """Пишет фикстуры в ``target``; большие только с ``big``."""
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name, case in CASES.items():
        if case.big and not big:
            continue
        path = target / name
        path.write_bytes(case.make())
        written.append(path)
    return written


if __name__ == "__main__":
    args = sys.argv[1:]
    with_big = "--big" in args
    folders = [a for a in args if a != "--big"]
    for path in write(Path(folders[0]) if folders else BROKEN_DIR, big=with_big):
        print(f"{path.stat().st_size:>8} {path}")
