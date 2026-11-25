"""ID блоков и предложений → фрагменты канонического текста документа.

Всё, что попадает в цитату требования, вырезается отсюда из ``ParsedDocument.text``: модель называет
только ID и, для части предложения, слова на её границах. Поэтому цитата дословная по построению.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from validator.ingest.blocks import SOURCE_ID
from validator.models import Block, BlockKind, ParsedDocument
from validator.text import normalize

_REF = re.compile(r"^\[?\s*[BВ]\s*0*(\d+)(?:\s*\.\s*0*(\d+))?\s*\]?$", re.IGNORECASE)


def normalize_ref(raw: str) -> str | None:
    """``B23``, ``[b023.2]``, ``В023`` (кириллица) → ``B023``, ``B023.2``; не ID → None."""
    m = _REF.match(raw.strip())
    if not m:
        return None
    ref = f"B{int(m.group(1)):03d}"
    return f"{ref}.{int(m.group(2))}" if m.group(2) else ref


def content_span(block: Block) -> tuple[int, int]:
    """Содержательная часть блока: без маркера списка и без внешних ``|`` строки таблицы."""
    if block.kind is BlockKind.TABLE_ROW and block.cells:
        return block.cells[0].char_start, block.cells[-1].char_end
    if block.sentences:
        return block.sentences[0].char_start, block.sentences[-1].char_end
    return block.anchor.char_start, block.anchor.char_end


@dataclass(frozen=True)
class Piece:
    """Фрагмент документа, на который указывает один ID."""

    ref: str
    block: Block
    start: int
    end: int


def resolve(doc: ParsedDocument, raw: str) -> tuple[Piece | None, bool]:
    """Фрагмент по ID и признак «ID исправлен»: несуществующее предложение известного блока → блок."""
    ref = normalize_ref(raw)
    block = doc.block(ref) if ref else None
    if ref is None or block is None:
        return None, False
    if "." in ref:
        sentence = doc.sentence(ref)
        if sentence is not None:
            return Piece(ref, block, sentence.char_start, sentence.char_end), False
        start, end = content_span(block)
        return Piece(block.id, block, start, end), True
    start, end = content_span(block)
    return Piece(ref, block, start, end), False


def _adjacent(text: str, left: Piece, right: Piece) -> bool:
    """Между фрагментами нет текста (только пробелы и разметка): их можно цитировать одним куском.

    Маркер следующего пункта списка («2.», «б)») чужим текстом не считается: условие в одном пункте и
    действие в следующем модель может объединить в одно требование.
    """
    if right.start <= left.end:
        return True
    gap_end = right.start
    if right.block.id != left.block.id and right.start == content_span(right.block)[0]:
        gap_end = right.block.anchor.char_start
    return not any(ch.isalnum() for ch in text[left.end : gap_end])


def runs(text: str, pieces: Iterable[Piece]) -> list[list[Piece]]:
    """Фрагменты по порядку документа, сгруппированные в непрерывные куски."""
    out: list[list[Piece]] = []
    for piece in sorted(pieces, key=lambda p: (p.start, p.end)):
        if out and _adjacent(text, out[-1][-1], piece):
            out[-1].append(piece)
        else:
            out.append([piece])
    return out


def narrow(text: str, start: int, end: int, first: str, last: str) -> tuple[int, int] | None:
    """Часть ``text[start:end]`` от слов ``first`` до слов ``last`` (сверка после нормализации).

    Модель называет границы части словами из предложения, а цитату по ним вырезает код.
    Пустая граница значит «с начала» или «до конца». Границы не нашлись → None.
    """
    norm, index = normalize(text[start:end])
    a, b = 0, len(norm)
    head = normalize(first)[0].strip() if first.strip() else ""
    tail = normalize(last)[0].strip() if last.strip() else ""
    if head:
        a = norm.find(head)
        if a < 0:
            return None
    if tail:
        pos = norm.find(tail, a)
        if pos < 0:
            return None
        b = pos + len(tail)
    if b <= a or not norm[a:b].strip():
        return None
    return start + index[a], start + index[b - 1] + 1


def trim(doc: ParsedDocument, block: Block, start: int, end: int) -> tuple[int, int]:
    """Без пробелов по краям и без метки требования в начале («**REQ-1.** Текст» → «Текст»)."""
    text = doc.text
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if block.source_id:
        m = SOURCE_ID.match(text[start:end])
        if m and m.group("id").replace("‑", "-") == block.source_id and start + m.end() < end:
            start += m.end()
    return start, end
