"""Фильтры шаблонов отчёта: места в документе по-человечески, ячейки таблиц, проценты, даты."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from validator.ingest.blocks import is_empty_cell_quote
from validator.models import Anchor, Extraction, Quote, ReportStatus

_SPACES = re.compile(r"\s+")
_MD_SPECIAL = re.compile(r"[*`<]|(?<!\w)_|_(?!\w)")
"""Знаки, которые Markdown превращает в разметку: цитата должна выглядеть, как в документе."""


def plain(text: object, limit: int | None = None) -> str:
    """Одна строка без переносов, по желанию обрезанная: общий вид текста для MD и PDF, без разметки."""
    value = _SPACES.sub(" ", str(text or "")).strip()
    if limit is not None and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def cell(text: object, limit: int | None = None) -> str:
    """Текст для ячейки Markdown-таблицы: одна строка, ``|`` и разметка экранированы, по желанию обрезан."""
    value = plain(text, limit).replace("|", "\\|")
    return _MD_SPECIAL.sub(lambda m: "\\" + m.group(0), value)


def short(text: object, limit: int = 160) -> str:
    return cell(text, limit)


_EXTRACTION_NOTES = {
    Extraction.OCR: "текст распознан (OCR)",
    Extraction.REPAIRED: "текст восстановлен после ремонта файла",
}
"""Пометки у мест, текст которых получен не прямым чтением (исследование 04, раздел 4)."""

_STATUS_LABELS = {
    ReportStatus.COMPLETE: "проверка выполнена полностью",
    ReportStatus.PARTIAL: "проверка выполнена частично, смотрите диагностику",
    ReportStatus.NOT_PERFORMED: "проверка не выполнена",
}


def status_label(status: ReportStatus) -> str:
    return _STATUS_LABELS[status]


def where(anchor: Anchor | None, section: Sequence[str] | None = None) -> str:
    """«раздел «A › B», строка 41», «стр. 4», «абзац 17», «таблица 2, строка 3»."""
    parts: list[str] = []
    if section:
        parts.append(f"раздел «{' › '.join(section[-2:])}»")
    if anchor is None:
        return ", ".join(parts) or "место не определено"
    if anchor.page is not None:
        parts.append(f"стр. {anchor.page}")
    if anchor.table is not None and anchor.row is not None:
        table = f"таблица {anchor.table}, строка {anchor.row}"
        if anchor.cell is not None:
            table += f", ячейка {anchor.cell}"
        if anchor.column:
            table += f", колонка «{anchor.column}»"
        parts.append(table)
    elif anchor.paragraph is not None:
        parts.append(f"абзац {anchor.paragraph}")
    elif anchor.page is None and anchor.line_start is not None:
        same = anchor.line_end in (None, anchor.line_start)
        parts.append(
            f"строка {anchor.line_start}" if same else f"строки {anchor.line_start}–{anchor.line_end}"
        )
    place = ", ".join(parts) or "место не определено"
    note = _EXTRACTION_NOTES.get(anchor.extraction)
    return f"{place}, {note}" if note else place


def quote_text(quote: Quote, limit: int = 300) -> str:
    """Цитата для отчёта: дословный фрагмент документа, если найден, иначе как дала модель.

    Цитату пустых ячеек («Подпись: (пусто)») показываем, как её видела модель: в документе на месте
    пустой ячейки только разделители таблицы, а место строки даёт якорь.
    """
    if is_empty_cell_quote(quote):
        return cell(quote.text, limit)
    return cell(quote.located or quote.text, limit)


def quote_plain(quote: Quote, limit: int = 300) -> str:
    """То же, что ``quote_text``, но без экранирования Markdown (для PDF)."""
    return plain(quote.located or quote.text, limit)


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f} %"


def money(value: float | None) -> str:
    return "—" if value is None else f"${value:.2f}"


def when(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M:%S %Z").strip()


FILTERS = {
    "cell": cell,
    "short": short,
    "where": where,
    "quote_text": quote_text,
    "pct": pct,
    "money": money,
    "when": when,
    "status_label": status_label,
}
