"""Фильтры шаблонов отчёта: места в документе по-человечески, ячейки таблиц, проценты, даты."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from validator.ingest.blocks import is_empty_cell_quote
from validator.models import Anchor, Extraction, Finding, FindingSource, Quote, ReportStatus, SourceKind

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


ChecklistItems = Mapping[str, tuple[str, str]]
"""Пункт доменного чек-листа: ID (MON-05) → (название чек-листа, вопрос пункта)."""


def _source_text(s: FindingSource, items: ChecklistItems, rules: Mapping[str, str]) -> str | None:
    if s.kind is SourceKind.LINT:
        name = rules.get(s.rule_id or "")
        return f"правилом «{name}»" if name else f"правилом {s.rule_id}"
    if s.kind is SourceKind.SET_CHECK:
        return "сверкой требований между частями документа"
    if s.kind is SourceKind.ANSWER:
        return f"сверкой с ответом автора {s.rule_id}"
    item = s.checklist_item if s.kind is SourceKind.PASS else s.rule_id
    if not item:
        return None
    title, question = items.get(item, ("", ""))
    return f"по чек-листу «{title}»: {plain(question)}" if title else f"по пункту чек-листа {item}"


def found_text(
    f: Finding, items: ChecklistItems | None = None, rules: Mapping[str, str] | None = None
) -> str:
    """Как найдена находка, человеческими словами; пусто, если сказать нечего (один проход без подсказок).

    Чек-лист называем по-русски с текстом пункта, правило линтера по названию (#125). «В N из K проходов»
    пишем, только если проходов больше одного: при одном проходе это ничего не говорит.
    """
    items = items or {}
    rules = rules or {}
    how = list(dict.fromkeys(t for s in f.sources if (t := _source_text(s, items, rules))))
    model = any(s.kind is SourceKind.PASS for s in f.sources)
    if f.of > 1 and model:
        how.insert(0, f"в {f.found_in} из {f.of} проходов поиска")
    elif f.of > 1 and not f.found_in and not all(s.kind is SourceKind.SET_CHECK for s in f.sources):
        how.append(f"проходы модели этого не отметили (0 из {f.of})")
    if f.sources and all(s.kind is SourceKind.SET_CHECK for s in f.sources):
        how.append("проходы поиска видят по одной части и такие пары не сравнивают")
    return "; ".join(how)
