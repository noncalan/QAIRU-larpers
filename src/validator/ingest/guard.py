"""Шаг 0: защищённый вход (заглушка каркаса с базовыми проверками).

Решает до разбора: можно ли читать файл, какой у него формат по содержимому и что делать человеку.
Каркас проверяет существование, размер, сигнатуру PDF и ZIP и двоичный мусор в тексте. Пароли,
zip-бомбы, обрезанные файлы и полную матрицу отказов исследования 04 делает #27.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from validator.errors import (
    IN_EMPTY,
    IN_EXTENSION_MISMATCH,
    IN_NOT_A_FILE,
    IN_NOT_FOUND,
    IN_TOO_LARGE,
    IN_UNSUPPORTED,
)
from validator.models import DiagLevel, Diagnostic, DocKind, GuardResult

MAX_INPUT_BYTES = 100 * 1024 * 1024
"""Больше этого не читаем: защита от случайно поданного архива или видео."""

SNIFF_BYTES = 4096
EXTENSION_KIND = {
    ".md": DocKind.MD,
    ".markdown": DocKind.MD,
    ".txt": DocKind.MD,
    ".pdf": DocKind.PDF,
    ".docx": DocKind.DOCX,
}
_SUPPORTED_HINT = "Подайте документ требований в PDF, DOCX или Markdown (.md)"


def _fatal(path: Path, code: str, message: str, hint: str, size: int | None = None) -> GuardResult:
    diag = Diagnostic(
        code=code,
        level=DiagLevel.ERROR,
        message=message,
        action="Проверка не выполнялась: файл не прочитан",
        hint=hint,
        location=path.name,
        step="guard",
    )
    return GuardResult(
        path=str(path), name=path.name, kind=DocKind.UNKNOWN, size_bytes=size, fatal=True, diagnostics=[diag]
    )


def sniff(head: bytes) -> DocKind | None:
    """Формат по сигнатуре: PDF, ZIP (кандидат в DOCX) или текст без нулевых байтов."""
    if head.startswith(b"%PDF-"):
        return DocKind.PDF
    if head.startswith(b"PK\x03\x04"):
        return DocKind.DOCX
    if b"\x00" not in head:
        return DocKind.MD
    return None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guard(path: Path) -> GuardResult:
    """Проверки до разбора. Не бросает исключений: всё, что не так, уходит в диагностику."""
    if not path.exists():
        return _fatal(path, IN_NOT_FOUND, f"Файл не найден: {path}", "Проверьте путь к документу")
    if not path.is_file():
        return _fatal(path, IN_NOT_A_FILE, f"Это не файл: {path}", "Укажите путь к файлу документа")
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            head = stream.read(SNIFF_BYTES)
    except OSError as exc:
        return _fatal(path, IN_NOT_A_FILE, f"Файл не читается: {exc.strerror}", "Проверьте права на файл")
    if size == 0:
        return _fatal(path, IN_EMPTY, "Файл пустой (0 байт)", "Передайте документ заново", size)
    if size > MAX_INPUT_BYTES:
        return _fatal(
            path, IN_TOO_LARGE, f"Файл слишком большой: {size} байт", "Разделите документ на части", size
        )

    by_extension = EXTENSION_KIND.get(path.suffix.lower())
    by_content = sniff(head)
    if by_content is None or (by_extension in (DocKind.PDF, DocKind.DOCX) and by_content is DocKind.MD):
        return _fatal(path, IN_UNSUPPORTED, "Содержимое не распознано как документ", _SUPPORTED_HINT, size)
    if by_extension is None and by_content is DocKind.MD:
        return _fatal(
            path,
            IN_UNSUPPORTED,
            f"Формат {path.suffix or 'без расширения'} не поддерживается",
            _SUPPORTED_HINT,
            size,
        )

    diagnostics: list[Diagnostic] = []
    if by_extension is not None and by_extension is not by_content:
        diagnostics.append(
            Diagnostic(
                code=IN_EXTENSION_MISMATCH,
                level=DiagLevel.INFO,
                message=f"Расширение {path.suffix} не совпадает с содержимым ({by_content.value})",
                action=f"Файл прочитан как {by_content.value}",
                hint="Исправьте расширение файла",
                location=path.name,
                step="guard",
            )
        )
    return GuardResult(
        path=str(path),
        name=path.name,
        kind=by_content,
        size_bytes=size,
        sha256=file_sha256(path),
        diagnostics=diagnostics,
    )
