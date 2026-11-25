"""Общее ядро: нормализация текста с картой смещений и сверка цитаты с документом.

Порт ``experiments/03-proto/quote_check.py`` с правками:

- пробел перед знаком препинания и после открывающей скобки убираем (исследование 18, п. 2.6:
  в нашем PDF это 18 → 65 точных совпадений);
- перенос слова по слогам в конце строки («перено-⏎сами») склеиваем;
- карта смещений верна и тогда, когда NFKC или смена регистра меняют длину символа;
- поиск можно ограничить блоками и предложениями (``within``).

Цитату сверяем так: точное вхождение после нормализации, затем нечёткое (rapidfuzz ≥ ``FUZZY_THRESHOLD``),
иначе «не найдено». Найденный фрагмент берём из исходного текста, а не из ответа модели.
"""

from __future__ import annotations

import hashlib
import unicodedata
from bisect import bisect_left
from collections.abc import Sequence
from functools import lru_cache

from rapidfuzz import fuzz

from validator.models import Anchor, Extraction, ParsedDocument, Quote, QuoteMatch

FUZZY_THRESHOLD = 90.0
"""Минимальный балл нечёткой сверки, как в прототипе и исследовании 18 (rapidfuzz partial_ratio ≥ 90)."""

MIN_FUZZY_CHARS = 12
"""Короче этого (после нормализации) только точное совпадение: у коротких строк нечёткий поиск шумит."""

_TRANSLATE = {
    "ё": "е",
    "«": '"',
    "»": '"',
    "“": '"',
    "”": '"',
    "„": '"',
    "‟": '"',
    # Одинарные кавычки тоже: модель может написать 'Сохранить' вместо «Сохранить». Апостроф меняется
    # одинаково в документе и в цитате, поэтому совпадению не мешает.
    "'": '"',
    "‘": '"',
    "’": '"',
    "‚": '"',
    "‛": '"',
    "—": "-",
    "–": "-",
    "‒": "-",
    "‑": "-",
    "‐": "-",
    "−": "-",
    "→": "->",
    "←": "<-",
}
_DROP_AS_SPACE = frozenset("*_`|#>")  # разметка Markdown и разделители таблиц
_DROP_ENTIRELY = frozenset("\u00ad\u200b\u200c\u200d\u2060\ufeff")  # мягкий перенос, нулевая ширина
# Пробел перед закрывающим знаком и после открывающего убираем; у дефиса и тире с обеих сторон
# («Бизнес -процесс» в PDF и «Бизнес-процесс» в цитате).
_CLOSE = frozenset(".,;:!?)]}%-")
_OPEN = frozenset("([{-")


def _expand(text: str) -> tuple[list[str], list[int]]:
    """Посимвольная замена: NFKC, нижний регистр, ё → е, кавычки и тире к одному виду.

    Пробельные символы становятся " " или "\\n" (перевод строки нужен склейке переносов).
    """
    chars: list[str] = []
    index: list[int] = []
    for i, raw in enumerate(text):
        if raw in _DROP_ENTIRELY:
            continue
        if raw == "\n":
            chars.append("\n")
            index.append(i)
            continue
        for piece in unicodedata.normalize("NFKC", raw).lower():
            for ch in _TRANSLATE.get(piece, piece):
                out = " " if ch in _DROP_AS_SPACE or ch.isspace() else ch
                chars.append(out)
                index.append(i)
    return chars, index


def _join_hyphenation(chars: list[str], index: list[int]) -> tuple[list[str], list[int]]:
    """«перено-⏎сами» → «переносами»: дефис в конце строки между буквами, дальше строчная буква."""
    out_c: list[str] = []
    out_i: list[int] = []
    k, n = 0, len(chars)
    while k < n:
        ch = chars[k]
        if ch == "-" and out_c and out_c[-1].isalpha():
            j = k + 1
            saw_newline = False
            while j < n and chars[j] in " \n":
                saw_newline = saw_newline or chars[j] == "\n"
                j += 1
            if saw_newline and j < n and chars[j].isalpha() and chars[j].islower():
                k = j
                continue
        out_c.append(ch)
        out_i.append(index[k])
        k += 1
    return out_c, out_i


def _squeeze_spaces(chars: list[str], index: list[int]) -> tuple[list[str], list[int]]:
    """Схлопывает пробелы и убирает лишние: в начале и конце, внутри числа, у знаков препинания."""
    flat_c: list[str] = []
    flat_i: list[int] = []
    for raw, i in zip(chars, index, strict=True):
        ch = " " if raw == "\n" else raw
        if ch == " " and (not flat_c or flat_c[-1] == " "):
            continue
        flat_c.append(ch)
        flat_i.append(i)
    out_c: list[str] = []
    out_i: list[int] = []
    n = len(flat_c)
    for k, ch in enumerate(flat_c):
        if ch == " ":
            prev = out_c[-1] if out_c else ""
            nxt = flat_c[k + 1] if k + 1 < n else ""
            if not prev or not nxt:
                continue
            if (prev.isdigit() and nxt.isdigit()) or nxt in _CLOSE or prev in _OPEN:
                continue
        out_c.append(ch)
        out_i.append(flat_i[k])
    return out_c, out_i


def normalize(text: str) -> tuple[str, list[int]]:
    """Нормализованный текст и карта: индекс символа нормализованной строки → индекс в исходной.

    Одинаково применяется к документу и к цитате, поэтому точное совпадение после нормализации
    означает, что цитата есть в документе с точностью до регистра, ё/е, кавычек, тире, пробелов,
    разметки Markdown и переносов строк.
    """
    chars, index = _expand(text)
    chars, index = _join_hyphenation(chars, index)
    chars, index = _squeeze_spaces(chars, index)
    return "".join(chars), index


@lru_cache(maxsize=16)
def _normalized_document(text: str) -> tuple[str, tuple[int, ...]]:
    norm, index = normalize(text)
    return norm, tuple(index)


def line_of(text: str, offset: int) -> int:
    """Номер строки (с 1) для смещения в тексте."""
    return text.count("\n", 0, offset) + 1


def stable_key(text: str, *parts: str) -> str:
    """Короткий стабильный хеш нормализованного текста: сравнение прогонов и версий документа."""
    material = "\x1f".join([normalize(text)[0], *parts])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def span_anchor(doc: ParsedDocument, start: int, end: int) -> Anchor:
    """Якорь для фрагмента ``doc.text[start:end]``: блоки, предложения, строки, страница, ячейка."""
    blocks = [b for b in doc.blocks if b.anchor.char_start < end and start < b.anchor.char_end]
    sentences = [s.id for b in blocks for s in b.sentences if s.char_start < end and start < s.char_end]
    first = blocks[0] if blocks else None
    cell = None
    if first is not None and len(blocks) == 1:
        cell = next(
            (c for c in first.cells if c.char_start <= start and end <= c.char_end),
            None,
        )
    return Anchor(
        char_start=start,
        char_end=end,
        block_ids=[b.id for b in blocks],
        sentence_ids=sentences,
        line_start=doc.line_of(start),
        line_end=doc.line_of(max(start, end - 1)),
        page=first.anchor.page if first else None,
        paragraph=first.anchor.paragraph if first else None,
        table=first.anchor.table if first else None,
        row=first.anchor.row if first else None,
        cell=cell.index if cell else None,
        column=cell.column if cell else None,
        extraction=first.anchor.extraction if first else Extraction.TEXT,
    )


def _spans_within(
    doc: ParsedDocument, within: Sequence[str], norm: str, index: Sequence[int]
) -> list[tuple[int, int]]:
    """Диапазоны нормализованного текста для указанных блоков и предложений, слитые и по порядку.

    Соседние диапазоны, между которыми только пробел (перевод строки между блоками), сливаем:
    цитата через границу двух названных блоков должна находиться внутри ``within``.
    """
    raw: list[tuple[int, int]] = []
    for ref in within:
        sentence = doc.sentence(ref) if "." in ref else None
        if sentence is not None:
            raw.append((sentence.char_start, sentence.char_end))
            continue
        block = doc.block(ref)
        if block is not None:
            raw.append((block.anchor.char_start, block.anchor.char_end))
    spans: list[tuple[int, int]] = []
    for start, end in sorted(raw):
        ns, ne = bisect_left(index, start), bisect_left(index, end)
        if spans and not norm[spans[-1][1] : ns].strip():
            spans[-1] = (spans[-1][0], max(spans[-1][1], ne))
        else:
            spans.append((ns, ne))
    return spans


def _anchor(doc: ParsedDocument | str, start: int, end: int) -> Anchor:
    if isinstance(doc, ParsedDocument):
        return span_anchor(doc, start, end)
    return Anchor(
        char_start=start,
        char_end=end,
        line_start=line_of(doc, start),
        line_end=line_of(doc, max(start, end - 1)),
    )


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_" or unicodedata.category(ch).startswith("M")


def snap_to_words(text: str, start: int, end: int) -> tuple[int, int]:
    """Границы фрагмента ``text[start:end]`` по целым словам (исследование 03, ``snap_to_words``).

    Нечёткое выравнивание может откусить начало или конец слова («ервис расходов», «одновремен»)
    и захватить пробел по краю. Убираем пробелы по краям и дотягиваем края до границ слов; внутрь
    фрагмента не режем. Если от фрагмента ничего не осталось, возвращаем исходные границы.
    """
    s, e = start, end
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    if s == e:
        return start, end
    while s > 0 and _is_word_char(text[s - 1]) and _is_word_char(text[s]):
        s -= 1
    while e < len(text) and _is_word_char(text[e - 1]) and _is_word_char(text[e]):
        e += 1
    return s, e


def locate_quote(
    doc: ParsedDocument | str,
    quote: str,
    within: Sequence[str] = (),
    *,
    threshold: float = FUZZY_THRESHOLD,
) -> Quote:
    """Ищет цитату в документе: точно, затем нечётко, иначе «не найдено».

    ``within``: ID блоков (B012) и предложений (B012.2), где искать. Пусто значит весь документ.
    Неизвестные ID пропускаются; если не осталось ни одного, цитата «не найдена» (расширить поиск
    на весь документ решает вызывающий, например верификатор). ``within`` требует ``ParsedDocument``.
    """
    text = doc.text if isinstance(doc, ParsedDocument) else doc
    if within and not isinstance(doc, ParsedDocument):
        raise TypeError("within работает только с ParsedDocument: нужны блоки")
    norm_doc, index = _normalized_document(text)
    norm_quote = normalize(quote)[0]
    not_found = Quote(text=quote, match=QuoteMatch.NOT_FOUND, score=0.0)
    if not norm_quote or not norm_doc:
        return not_found
    spans = (
        _spans_within(doc, within, norm_doc, index) if isinstance(doc, ParsedDocument) and within else None
    )
    if spans is None:
        spans = [(0, len(norm_doc))]

    for ns, ne in spans:
        pos = norm_doc.find(norm_quote, ns, ne)
        if pos >= 0:
            start, end = index[pos], index[pos + len(norm_quote) - 1] + 1
            return Quote(
                text=quote,
                match=QuoteMatch.EXACT,
                score=100.0,
                located=text[start:end],
                anchor=_anchor(doc, start, end),
            )

    if len(norm_quote) < MIN_FUZZY_CHARS:
        return not_found
    best: tuple[float, int, int] | None = None
    best_score = 0.0
    for ns, ne in spans:
        al = fuzz.partial_ratio_alignment(norm_quote, norm_doc[ns:ne])
        if al is None:
            continue
        best_score = max(best_score, al.score)
        if al.score >= threshold and (best is None or al.score > best[0]) and al.dest_end > al.dest_start:
            best = (al.score, ns + al.dest_start, ns + al.dest_end)
    if best is None:
        return not_found.model_copy(update={"score": round(best_score, 1)})
    score, nstart, nend = best
    # Балл остаётся от выравнивания, а показываем и якорим целые слова исходного текста.
    start, end = snap_to_words(text, index[nstart], index[nend - 1] + 1)
    return Quote(
        text=quote,
        match=QuoteMatch.FUZZY,
        score=round(score, 1),
        located=text[start:end],
        anchor=_anchor(doc, start, end),
    )


__all__ = [
    "FUZZY_THRESHOLD",
    "MIN_FUZZY_CHARS",
    "line_of",
    "locate_quote",
    "normalize",
    "snap_to_words",
    "span_anchor",
    "stable_key",
]
