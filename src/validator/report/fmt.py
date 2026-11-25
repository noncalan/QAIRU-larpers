"""Фильтры шаблонов отчёта: места в документе по-человечески, ячейки таблиц, проценты, даты."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from validator.models import Anchor, Quote

_SPACES = re.compile(r"\s+")


def cell(text: object, limit: int | None = None) -> str:
    """Текст для ячейки Markdown-таблицы: одна строка, ``|`` экранирован, по желанию обрезан."""
    value = _SPACES.sub(" ", str(text or "")).strip().replace("|", "\\|")
    if limit is not None and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return value


def short(text: object, limit: int = 160) -> str:
    return cell(text, limit)


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
    return ", ".join(parts) or "место не определено"


def quote_text(quote: Quote, limit: int = 300) -> str:
    """Цитата для отчёта: дословный фрагмент документа, если найден, иначе как дала модель."""
    return cell(quote.located or quote.text, limit)


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
}
