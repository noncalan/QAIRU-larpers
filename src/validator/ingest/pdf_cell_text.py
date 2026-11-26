"""Текст ячейки таблицы PDF из её строк: где склеить слово, разорванное у края узкой колонки (#121).

В узкой колонке программа вёрстки рвёт длинное слово без знака переноса: «Наимено» и «вание», «order_i»
и «d», «Статус обработ» и «ки». Если склеить строки через пробел, цитата из такой ячейки не находится
поиском по PDF и выглядит как опечатка в отчёте. Если склеивать всегда, сольются обычные слова
(«согласования» и «сторон»). Поэтому склеиваем, только когда это видно по самому документу:

1. Перенос со знаком («перено-» и «сами»): как в абзацах (``join_hyphenation``).
2. Дефис в конце строки и цифра в начале следующей («ref=abc-» и «42»): код или номер, дефис остаётся.
3. Строка ячейки из одного обрывка, которого нет среди слов документа, а склеенное слово в документе есть
   («Наимено» + «вание» = «наименование» из абзаца).
4. Строка ячейки из одного обрывка доходит до правого края колонки, обрывка нет среди слов документа,
   а следующая строка начинается с короткого хвоста в 1–3 строчные буквы, который не служебное слово
   («обработ» + «ки», «согласовани» + «я»).

Слова документа (``vocabulary``) берём из абзацев и из строк таблиц, кроме самих подозрительных обрывков
и хвостов: иначе обрывок «нашёлся» бы в документе сам по себе. Правила общие, без слов конкретного файла.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from itertools import pairwise

from validator.ingest.pdf_layout import join_hyphenation

SHORT_TAIL = 3
"""Хвост разорванного слова на новой строке не длиннее стольких букв (правило 4)."""

MIN_HEAD = 4
"""Обрывок в конце строки не короче стольких знаков: короткие строки это обычно целые слова."""

SERVICE_WORDS = frozenset(
    {"и", "в", "с", "к", "у", "о", "а", "на", "по", "не", "от", "до", "за", "из", "но", "да", "ни", "же"}
    | {"ли", "бы", "во", "со", "ко", "об", "то", "для", "при", "без", "или", "под", "над", "про"}
)
"""Короткие служебные слова: строка, которая с них начинается, это перенос по пробелу, а не хвост слова."""

_WORD = re.compile(r"\w+")
_STEM = re.compile(r"\w+$")


@dataclass(frozen=True)
class CellLine:
    """Строка внутри ячейки: текст, одно ли это слово и доходит ли строка до правого края колонки."""

    text: str
    single: bool = False
    at_edge: bool = False


def words(text: str) -> list[str]:
    return [w.casefold() for w in _WORD.findall(text)]


def _starts_lower(word: str) -> bool:
    return bool(word) and (word[0].islower() or word[0].isdigit() or word[0] == "_")


def _glue(text: str, prev: CellLine, head: str, vocab: frozenset[str] | None) -> str | None:
    """Склеенный текст без пробела перед ``head`` или None, если это перенос по пробелу."""
    last = text.rsplit(" ", 1)[-1]
    if len(last) > 1 and last.endswith("-") and last[-2].isalnum() and head[:1].isdigit():
        return text + head  # правило 2: код или номер, дефис остаётся
    if vocab is None or not prev.single:
        return None
    tail_match = _WORD.match(head)
    tail = tail_match.group(0) if tail_match else ""
    stem_match = _STEM.search(last)  # «продукты/систе» → «систе»: обрывок это хвост последнего слова
    stem = stem_match.group(0).casefold() if stem_match else ""
    if not _starts_lower(tail) or not stem or stem in vocab:
        return None
    if stem + tail.casefold() in vocab:
        return text + head  # правило 3
    short = len(tail) <= SHORT_TAIL and tail.isalpha() and tail.casefold() not in SERVICE_WORDS
    if prev.at_edge and len(stem) >= MIN_HEAD and short:
        return text + head  # правило 4
    return None


def join_lines(lines: Sequence[CellLine], vocab: frozenset[str] | None = None) -> str:
    """Строки ячейки в один текст. ``vocab=None``: только переносы со знаком и дефис перед цифрой."""
    if not lines:
        return ""
    text = lines[0].text
    for prev, nxt in pairwise(lines):
        head, _, rest = nxt.text.partition(" ")
        glued = join_hyphenation(text, head) if head else None
        if glued is None and head:
            glued = _glue(text, prev, head, vocab)
        if glued is None:
            glued, rest = text, nxt.text
        text = f"{glued} {rest}".strip() if rest else glued
    return text


def join_tail(a: str, b: str, vocab: frozenset[str] | None = None) -> str:
    """Хвост ячейки с новой страницы к её началу: те же правила, без геометрии (строки на разных листах)."""
    if not a or not b:
        return a or b
    return join_lines([CellLine(a, single=" " not in a.strip()), CellLine(b)], vocab)


def suspects(lines: Sequence[CellLine]) -> set[int]:
    """Номера строк ячейки, слова которых не идут в словарь: возможный обрывок (строка из одного слова,
    за которой ячейка продолжается) и следующая за ним строка с возможным хвостом."""
    out: set[int] = set()
    for k, line in enumerate(lines[:-1]):
        if line.single:
            out.update((k, k + 1))
    return out


def vocabulary(prose: Iterable[str], cells: Iterable[Sequence[CellLine]]) -> frozenset[str]:
    """Слова документа: все слова абзацев и строк таблиц, кроме возможных обрывков и хвостов (suspects)."""
    found = {w for text in prose for w in words(text)}
    for lines in cells:
        skip = suspects(lines)
        found.update(w for k, line in enumerate(lines) if k not in skip for w in words(line.text))
    return frozenset(found)


__all__ = ["CellLine", "join_lines", "join_tail", "vocabulary", "words"]
