"""Строка заголовка таблицы DOCX: по разметке или по виду первой строки (#139).

Word и python-docx ставят почти у любой таблицы ``w:tblLook firstRow="1"``. Это флаг оформления
(«выделить первую строку стилем»), а не признак шапки: в таблице требований без шапки («ФТ-1 | Система
должна…») такая строка это первое требование. Поэтому шапкой считаем:

- строку с ``w:tblHeader`` («повторять как заголовок» на каждой странице): автор сам назвал её шапкой;
- иначе первую строку, которая выглядит как названия колонок и отличается от строк ниже
  (``looks_like_header``). ``tblLook firstRow="0"`` догадку запрещает: автор снял галочку «строка заголовка».

Шапка по разметке блоком не становится (как в MD и PDF). Угаданная шапка даёт названия колонок и
сама остаётся блоком: если догадка неверна, ни одно слово строки не выпадет из нарезки и проверки.
Правила общие, без слов конкретного документа: решает форма ячеек, а не их содержание.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import Enum

from validator.ingest.blocks import KEY_VALUE_HEADERS, ROW_LABEL, SOURCE_ID


class HeaderKind(Enum):
    NONE = "none"
    MARKED = "marked"
    """Первая строка помечена ``w:tblHeader``: шапка, блоком не становится."""
    GUESSED = "guessed"
    """Первая строка похожа на названия колонок: шапка и при этом блок."""


MAX_HEADER_WORDS = 6
MAX_HEADER_CHARS = 60
"""Название колонки короткое: «Ожидаемый результат», «Критерий приёмки», «№ п/п»."""

_SENTENCE_END = (".", "!", "?", ";")
_MODAL = re.compile(
    r"\b(?:долж(?:ен|на|но|ны)|обязан[аоы]?|мож(?:ет|ут)|необходимо|требуется|следует|"
    r"shall|must|should|may|will|can)\b",
    re.IGNORECASE,
)
"""Глагол долженствования или возможности: так пишут требование, а не название колонки. Только формы
глагола: «Должность» и «Обязанности» это обычные названия колонок."""


def _clean(cell: str) -> str:
    return " ".join(cell.replace("**", "").replace("__", "").split())


def is_column_name(cell: str) -> bool:
    """Ячейка похожа на название колонки: коротко, без точки в конце, без числа-значения,
    метки требования («ФТ-1») и глагола долженствования («должна», «shall»)."""
    text = _clean(cell)
    if not text or len(text) > MAX_HEADER_CHARS or len(text.split()) > MAX_HEADER_WORDS:
        return False
    if text.endswith(_SENTENCE_END):
        return False
    if any(ch.isdigit() for ch in text) and not any(ch.isalpha() for ch in text):
        return False  # число, дата, номер пункта
    if ROW_LABEL.match(text) or SOURCE_ID.match(f"{text} "):
        return False
    return not _MODAL.search(text)


def _filled(cells: Sequence[str]) -> list[str]:
    return [c for c in cells if _clean(c)]


def _repeats_below(first: Sequence[str], below: Sequence[Sequence[str]]) -> bool:
    """Текст ячейки первой строки повторяется ниже в той же колонке: это значение («Высокий»), а не шапка."""
    for k, head in enumerate(first):
        key = _clean(head).casefold()
        if key and any(k < len(r) and _clean(r[k]).casefold() == key for r in below):
            return True
    return False


def _bold_apart(bold: Sequence[bool | None]) -> bool:
    """Первая строка вся жирная, а ниже есть строка не жирным: автор выделил шапку."""
    return bold[0] is True and any(b is False for b in bold[1:])


def _key_value_head(first: Sequence[str]) -> bool:
    """«Поле | Значение», «Параметр | …»: общие слова шапки таблицы ключей."""
    return bool(first) and _clean(first[0]).lower() in KEY_VALUE_HEADERS


def _contrast(first: Sequence[str], below: Sequence[Sequence[str]]) -> bool:
    """Под названием колонки ниже только значения другого вида: номера, метки, предложения.

    Если первая колонка ниже это сплошь короткие ключи (паспорт документа, глоссарий), первая строка
    может быть такой же парой «ключ | значение»: тогда догадки по контрасту нет.
    """
    keys = [r[0] for r in below if r and _clean(r[0])]
    if keys and all(is_column_name(c) for c in keys):
        return False
    for k, head in enumerate(first):
        if not _clean(head):
            continue
        column = [r[k] for r in below if k < len(r) and _clean(r[k])]
        if column and not any(is_column_name(c) for c in column):
            return True
    return False


def looks_like_header(rows: Sequence[Sequence[str]], bold: Sequence[bool | None]) -> bool:
    """Первая строка таблицы похожа на шапку: все её ячейки похожи на названия колонок и есть признак шапки.

    ``rows``: тексты ячеек по строкам; ``bold``: строка вся жирная (True), не вся (False), без текста (None).
    Признаки шапки: выделена жирным в отличие от строк ниже; общее слово таблицы ключей («Поле»); под ней
    пустая форма; или в какой-то колонке под названием только значения другого вида.
    """
    if len(rows) < 2:
        return False
    first, below = rows[0], rows[1:]
    filled = _filled(first)
    if not filled or not all(is_column_name(c) for c in filled):
        return False
    if len(first) == 1 and any(len(r) > 1 for r in below):
        return False  # одна ячейка на всю ширину: подпись над таблицей, а не названия колонок
    if _repeats_below(first, below):
        return False
    if not any(_filled(r) for r in below):
        return True  # под шапкой пустая форма (подписи, согласование)
    return _bold_apart(bold) or _key_value_head(first) or _contrast(first, below)
