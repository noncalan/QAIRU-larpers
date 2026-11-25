"""Шаг 0: защищённый вход. Решает до разбора, можно ли читать файл, и что делать человеку, если нельзя.

Порядок проверок (исследование 04, раздел 3): файл есть и читается → не пустой и не огромный → тип по
содержимому (``sniff``), а не по расширению → глубокая проверка контейнера (``containers``: zip-бомба,
пароль, старый .doc, PDF с паролем, ремонтом, без страниц, обрезанный со спасением). Расхождение
расширения и содержимого не ошибка: файл читается по содержимому, в отчёт идёт ``IN-011``.

``guard`` не бросает исключений: всё, что не так, уходит в ``Diagnostic`` с кодом ``IN-xxx``.
Проверки после разбора (мусорный текстовый слой, страницы без текста, «не похоже на требования»)
делает ``ingest/quality.py``.
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
from validator.ingest.containers import Probe, input_diag, probe_ole, probe_pdf, probe_zip, reject
from validator.ingest.sniff import SNIFF_BYTES, Sniffed, image_format, sniff
from validator.models import DiagLevel, Diagnostic, DocKind, GuardResult

MAX_INPUT_BYTES = 100 * 1024 * 1024
"""Больше этого не читаем: защита от случайно поданного архива или видео."""

EXTENSION_KIND = {
    ".md": DocKind.MD,
    ".markdown": DocKind.MD,
    ".txt": DocKind.MD,
    ".pdf": DocKind.PDF,
    ".docx": DocKind.DOCX,
}
_SUPPORTED_HINT = "Подайте документ требований в PDF, DOCX или Markdown (.md)"
_KIND_LABEL = {DocKind.MD: "текст (Markdown)", DocKind.PDF: "PDF", DocKind.DOCX: "DOCX"}

__all__ = ["EXTENSION_KIND", "MAX_INPUT_BYTES", "file_sha256", "guard"]


def _located(path: Path, diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    return [d.model_copy(update={"location": d.location or path.name}) for d in diagnostics]


def _fatal(path: Path, probe: Probe, size: int | None = None, digest: str | None = None) -> GuardResult:
    """Читать нельзя. Хеш и размер всё равно идут в паспорт, если файл удалось прочитать."""
    return GuardResult(
        path=str(path),
        name=path.name,
        kind=DocKind.UNKNOWN,
        size_bytes=size,
        sha256=digest,
        fatal=True,
        diagnostics=_located(path, probe.diagnostics),
    )


def _unreadable(path: Path, exc: OSError) -> GuardResult:
    return _fatal(path, reject(IN_NOT_A_FILE, f"Файл не читается: {exc.strerror}", "Проверьте права на файл"))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(sniffed: Sniffed, path: Path, head: bytes, workdir: Path | None) -> Probe:
    """Глубокая проверка по типу содержимого: формат для разборщика или отказ с диагностикой."""
    by_extension = EXTENSION_KIND.get(path.suffix.lower())
    match sniffed:
        case Sniffed.PDF:
            return probe_pdf(path, workdir)
        case Sniffed.ZIP:
            return probe_zip(path)
        case Sniffed.OLE:
            return probe_ole(path)
        case Sniffed.TEXT:
            return Probe(kind=DocKind.MD)
        case Sniffed.HTML if by_extension is DocKind.MD:
            return Probe(kind=DocKind.MD)  # Markdown может начинаться с HTML-разметки
        case Sniffed.HTML:
            return reject(
                IN_UNSUPPORTED,
                "Это веб-страница (HTML), такой формат не поддерживается",
                "Сохраните страницу как PDF («Печать → Сохранить как PDF») или пришлите исходник в DOCX",
            )
        case Sniffed.RTF:
            return reject(
                IN_UNSUPPORTED, "Формат RTF не поддерживается", "Сохраните документ как DOCX или PDF"
            )
        case Sniffed.IMAGE:
            return reject(
                IN_UNSUPPORTED,
                f"Это изображение ({image_format(head)}), а не документ с текстом",
                "Пришлите PDF с текстовым слоем, DOCX или Markdown: распознавания картинок (OCR) "
                "в этой версии агента нет",
            )
    return reject(
        IN_UNSUPPORTED, "Файл двоичный: это не текст и не известный формат документа", _SUPPORTED_HINT
    )


def _extension_note(path: Path, kind: DocKind) -> list[Diagnostic]:
    """``IN-011`` ИНФО, если расширение не совпало с содержимым: файл всё равно читается по содержимому."""
    by_extension = EXTENSION_KIND.get(path.suffix.lower())
    if by_extension is kind:
        return []
    shown = path.suffix or "(без расширения)"
    message = (
        f"Расширение {shown} не совпадает с содержимым: внутри {_KIND_LABEL[kind]}"
        if by_extension is not None
        else f"Расширение {shown} не из списка поддерживаемых, внутри {_KIND_LABEL[kind]}"
    )
    return [
        input_diag(
            IN_EXTENSION_MISMATCH,
            DiagLevel.INFO,
            message,
            f"Файл прочитан как {_KIND_LABEL[kind]}",
            "Исправьте расширение файла",
            path.name,
        )
    ]


def guard(path: Path, *, workdir: Path | None = None) -> GuardResult:
    """Проверки до разбора. Не бросает исключений: всё, что не так, уходит в диагностику.

    ``workdir``: папка прогона, куда можно положить восстановленную копию обрезанного PDF. Без неё
    обрезанный PDF сразу даёт ошибку ``IN-040``.
    """
    if not path.exists():
        return _fatal(path, reject(IN_NOT_FOUND, f"Файл не найден: {path}", "Проверьте путь к документу"))
    if not path.is_file():
        return _fatal(path, reject(IN_NOT_A_FILE, f"Это не файл: {path}", "Укажите путь к файлу документа"))
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            head = stream.read(SNIFF_BYTES)
    except OSError as exc:
        return _unreadable(path, exc)
    if size == 0:
        return _fatal(path, reject(IN_EMPTY, "Файл пустой (0 байт)", "Передайте документ заново"), size)
    if size > MAX_INPUT_BYTES:
        message = f"Файл слишком большой: {size / 2**20:.0f} МБ, предел {MAX_INPUT_BYTES / 2**20:.0f} МБ"
        return _fatal(path, reject(IN_TOO_LARGE, message, "Разделите документ на части"), size)

    try:
        digest = file_sha256(path)
    except OSError as exc:
        return _unreadable(path, exc)
    probe = _probe(sniff(head), path, head, workdir)
    if probe.kind is None:
        return _fatal(path, probe, size, digest)
    diagnostics = _located(path, [*_extension_note(path, probe.kind), *probe.diagnostics])
    return GuardResult(
        path=str(path),
        name=path.name,
        kind=probe.kind,
        size_bytes=size,
        sha256=digest,
        diagnostics=diagnostics,
        read_path=str(probe.read_path) if probe.read_path else None,
    )
