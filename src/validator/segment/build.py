"""Черновики требований → ``Requirement``: номера R-NN, цитата и якорь кодом, локатор, ``stable_key``.

Номер ``R-NN`` идёт по порядку документа; подряд идущие требования одного блока это атомы одной
единицы, они получают ``R-NN.1``, ``R-NN.2`` (исследование 18, п. 3.4). Номера не зависят от порядка
и формулировок ответа модели, поэтому стабильны между прогонами. Модуль общий для нарезки моделью
и для запасной нарезки без модели.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from validator.ingest.blocks import KEY_VALUE_HEADERS
from validator.models import (
    Block,
    BlockKind,
    BlockLabel,
    DecidedBy,
    ParsedDocument,
    Requirement,
    RequirementType,
)
from validator.segment.answer import API_SUBTYPES, ENTITY_KEYS, ModelItem
from validator.segment.spans import content_span, resolve, trim
from validator.text import span_anchor, stable_key

MIN_WORDS = 3
"""Короче этого фрагмент без решения модели считаем пояснением, а не требованием."""

SUMMARY_WORDS = 15
"""Краткая формулировка в таблице нарезки: до 15 слов (исследование 18, п. 3.3)."""

MAX_ENTITIES = 12
MAX_ENTITY_CHARS = 80
MAX_SOURCE_ID_CHARS = 30

TABLE_KEY_CHARS = 40
"""Первая ячейка строки таблицы не длиннее этого называет строку в локаторе."""

KEY_HEADERS = KEY_VALUE_HEADERS | {"name", "имя", "название", "код", "code", "id", "метод", "method"}
"""Колонки, значение которых называет строку таблицы: «Параметры: amount» вместо «строка таблицы 2»."""

_CONTENT_KINDS = frozenset(
    {BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.TABLE_ROW, BlockKind.QUOTE, BlockKind.FOOTNOTE}
)
"""Блоки, в которых может быть требование: цитата и сноска тоже (условие к требованию бывает в сноске)."""
_MARKUP = re.compile(r"[*`_]+")
_EMPHASIS = re.compile(r"\*\*|__|`")
_LABEL_LIKE = re.compile(r"(?=.*[A-Za-zА-Яа-яЁё])(?=.*\d)")


@dataclass
class Draft:
    """Требование до нумерации: фрагмент документа и метаданные от модели или от кода."""

    start: int
    end: int
    block: Block
    decided_by: DecidedBy
    item: ModelItem | None = None
    part: bool = False


def word_count(text: str) -> int:
    return len(_MARKUP.sub(" ", text).split())


def fallback_label(block: Block) -> tuple[BlockLabel, str]:
    """Решение кода по блоку без модели: содержательный блок от 3 слов это требование."""
    if block.kind is BlockKind.HEADING:
        return BlockLabel.HEADING, "заголовок раздела"
    if block.kind is BlockKind.CODE:
        return BlockLabel.EXAMPLE, "блок кода или пример"
    if block.kind not in _CONTENT_KINDS:
        return BlockLabel.METADATA, block.kind.label
    if word_count(block.serialized or block.text) < MIN_WORDS:
        return BlockLabel.CONTEXT, f"меньше {MIN_WORDS} слов"
    return BlockLabel.REQUIREMENT, ""


def block_draft(doc: ParsedDocument, block: Block) -> Draft:
    """Требование из целого блока, решение кода."""
    start, end = trim(doc, block, *content_span(block))
    return Draft(start=start, end=end, block=block, decided_by=DecidedBy.CODE)


def requirement_id(number: int) -> str:
    """``R-NN`` по порядку документа: две цифры, после 99 три."""
    return f"R-{number:02d}"


def _summary(draft: Draft, quote: str) -> str:
    """Формулировка модели, а без неё начало цитаты (у строки таблицы «Колонка: значение»), до 15 слов."""
    if draft.item is not None and draft.item.summary.strip():
        source = draft.item.summary
    elif draft.block.kind is BlockKind.TABLE_ROW and draft.block.serialized:
        source = draft.block.serialized
    else:
        source = _EMPHASIS.sub("", quote)
    return clip_words(source, SUMMARY_WORDS)


def clip_words(text: str, limit: int) -> str:
    """Не больше ``limit`` слов с буквами: числа вроде «1 000 000» слов не добавляют и не рвутся."""
    tokens = text.split()
    words = 0
    for k, token in enumerate(tokens):
        if any(ch.isalpha() for ch in token):
            words += 1
            if words > limit:
                return " ".join(tokens[:k]).rstrip(",;:—-") + "…"
    return " ".join(tokens)


def _sentence_position(block: Block, start: int, end: int) -> str:
    """«предл. 2/4» для атома из части предложений блока; для всего блока пусто."""
    total = len(block.sentences)
    hits = [k for k, s in enumerate(block.sentences, start=1) if s.char_start < end and start < s.char_end]
    if not hits or len(hits) == total:
        return ""
    first, last = hits[0], hits[-1]
    span = str(first) if first == last else f"{first}–{last}"
    return f"предл. {span}/{total}"


def locator(block: Block, start: int, end: int, *, part: bool = False) -> str:
    """Где требование в документе: «REQ-2, предл. 2/4», «Раздел, п. 4», «Параметры: amount»."""
    section = block.section[-1] if block.section else "документ"
    if block.kind is BlockKind.TABLE_ROW:
        first = block.cells[0] if block.cells else None
        key = _MARKUP.sub("", first.text).strip() if first else ""
        if first and first.column.lower() in KEY_HEADERS and key and len(key) <= TABLE_KEY_CHARS:
            return f"{section}: {key}"
        return f"{section}, строка таблицы {block.anchor.row}" if block.anchor.row else section
    parts = [block.source_id or section]
    if block.ordinal is not None and not block.source_id:
        parts.append(f"п. {block.ordinal}")
    position = _sentence_position(block, start, end)
    if position:
        parts.append(position)
    if part:
        parts.append("часть")
    return ", ".join(parts)


def _entities(item: ModelItem) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for entity in item.entities[:MAX_ENTITIES]:
        key = ENTITY_KEYS.get(entity.kind.strip().lower())
        value = " ".join(entity.value.split())[:MAX_ENTITY_CHARS]
        if key and value and value not in out.get(key, []):
            out.setdefault(key, []).append(value)
    return out


def _type(item: ModelItem | None) -> tuple[RequirementType, str | None]:
    if item is None:
        return RequirementType.OTHER, None
    try:
        kind = RequirementType(item.type.strip().lower())
    except ValueError:
        kind = RequirementType.OTHER
    subtype = item.subtype.strip().lower()
    return kind, subtype if kind is RequirementType.API and subtype in API_SUBTYPES else None


def _source_id(doc: ParsedDocument, draft: Draft) -> str | None:
    """Метка из документа: найденная кодом, иначе названная моделью, если она дословно есть в блоке.

    Метка от модели принимается, только если похожа на идентификатор (буквы и цифры, до 30 знаков)
    и стоит в тексте блока отдельным словом: номер пункта или слово из текста меткой не станут.
    """
    if draft.block.source_id:
        return draft.block.source_id
    claimed = draft.item.source_id.strip() if draft.item else ""
    if not claimed or len(claimed) > MAX_SOURCE_ID_CHARS or not _LABEL_LIKE.search(claimed):
        return None
    block_text = doc.text[draft.block.anchor.char_start : draft.block.anchor.char_end]
    found = re.search(rf"(?<![\w-]){re.escape(claimed)}(?!\w)", block_text)
    return claimed if found else None


def _groups(drafts: Sequence[Draft]) -> list[list[Draft]]:
    """Подряд идущие требования одного блока это атомы одной единицы."""
    out: list[list[Draft]] = []
    for draft in drafts:
        if out and out[-1][-1].block.id == draft.block.id:
            out[-1].append(draft)
        else:
            out.append([draft])
    return out


def _numbered(drafts: Sequence[Draft]) -> list[tuple[str, str | None, Draft]]:
    """``(R-NN или R-NN.k, родитель, черновик)`` по порядку документа."""
    ordered = sorted(drafts, key=lambda d: (d.start, d.end))
    out: list[tuple[str, str | None, Draft]] = []
    for n, group in enumerate(_groups(ordered), start=1):
        base = requirement_id(n)
        if len(group) == 1:
            out.append((base, None, group[0]))
        else:
            out.extend((f"{base}.{k}", base, d) for k, d in enumerate(group, start=1))
    return out


def _targets(
    doc: ParsedDocument, refs: Iterable[str], spans: Sequence[tuple[int, int, str]], own: str
) -> list[str]:
    """Связанные места: номер требования, если фрагмент в нарезке, иначе ID блока."""
    out: list[str] = []
    for raw in refs:
        piece, _ = resolve(doc, raw) if raw.strip() else (None, False)
        if piece is None:
            continue
        hits = [rid for start, end, rid in spans if start < piece.end and piece.start < end]
        out.extend(t for t in hits or [piece.ref] if t != own and t not in out)
    return out


def build_requirements(doc: ParsedDocument, drafts: Sequence[Draft]) -> list[Requirement]:
    """Нумерация по порядку документа, цитата и якорь кодом, ``stable_key`` без повторов."""
    numbered = _numbered(drafts)
    spans = [(d.start, d.end, rid) for rid, _, d in numbered]
    seen: Counter[str] = Counter()
    out: list[Requirement] = []
    for rid, parent, draft in numbered:
        quote = doc.text[draft.start : draft.end]
        source_id = _source_id(doc, draft)
        key = stable_key(quote, source_id or "")
        seen[key] += 1
        if seen[key] > 1:  # одинаковый текст в двух местах документа: ключи не должны совпасть
            key = stable_key(quote, source_id or "", str(seen[key]))
        kind, subtype = _type(draft.item)
        item = draft.item
        depends = _targets(doc, [item.depends_on], spans, rid) if item else []
        out.append(
            Requirement(
                id=rid,
                parent_id=parent,
                source_id=source_id,
                source_locator=locator(draft.block, draft.start, draft.end, part=draft.part),
                section_path=list(draft.block.section),
                type=kind,
                subtype=subtype,
                quote=quote,
                summary=_summary(draft, quote),
                anchor=span_anchor(doc, draft.start, draft.end),
                entities=_entities(item) if item else {},
                links=_targets(doc, item.links, spans, rid) if item else [],
                depends_on=depends[0] if depends else None,
                decided_by=draft.decided_by,
                stable_key=key,
            )
        )
    return out
