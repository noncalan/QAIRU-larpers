"""Шаг 2: нарезка документа на требования (заглушка каркаса).

Контракт: ``segment(doc, llm, cfg) -> Segmentation``. Настоящую нарезку («модель указывает блоки,
код цитирует», Sonnet 5.5 medium) делает #19. Заглушка вызывает только запасной путь из плана:
каждый содержательный блок становится требованием, цитата это дословный текст блока.
``fallback_segmentation`` останется запасным путём, когда модель упала.
"""

from __future__ import annotations

from validator.llm.base import LLMClient
from validator.models import (
    Block,
    BlockKind,
    BlockLabel,
    DecidedBy,
    ExcludedBlock,
    ParsedDocument,
    Requirement,
    RunConfig,
    Segmentation,
    SegmentationMode,
)
from validator.text import stable_key

MIN_WORDS = 3
"""Короче этого блок считаем пояснением, а не требованием."""

SUMMARY_WORDS = 15
"""Краткая формулировка в таблице нарезки: до 15 слов (исследование 18, п. 3.3)."""

_CONTENT_KINDS = frozenset({BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.TABLE_ROW})


def _label(block: Block) -> tuple[BlockLabel, str]:
    if block.kind is BlockKind.HEADING:
        return BlockLabel.HEADING, "заголовок раздела"
    if block.kind is BlockKind.CODE:
        return BlockLabel.EXAMPLE, "блок кода или пример"
    if block.kind not in _CONTENT_KINDS:
        return BlockLabel.METADATA, f"{block.kind.label}"
    if len((block.serialized or block.text).split()) < MIN_WORDS:
        return BlockLabel.CONTEXT, f"меньше {MIN_WORDS} слов"
    return BlockLabel.REQUIREMENT, ""


def _summary(block: Block) -> str:
    source = block.serialized or (block.sentences[0].text if block.sentences else block.text)
    words = source.split()
    tail = "…" if len(words) > SUMMARY_WORDS else ""
    return " ".join(words[:SUMMARY_WORDS]) + tail


def _locator(block: Block) -> str:
    """Где требование в документе, если у него нет своего номера (исследование 18, п. 3.4)."""
    section = block.section[-1] if block.section else "документ"
    if block.kind is BlockKind.TABLE_ROW and block.anchor.row is not None:
        return f"{section}, строка таблицы {block.anchor.row}"
    if block.ordinal is not None:
        return f"{section}, п. {block.ordinal}"
    return section


def requirement_id(number: int) -> str:
    """``R-NN`` по порядку документа: две цифры, после 99 три."""
    return f"R-{number:02d}"


def fallback_segmentation(doc: ParsedDocument) -> Segmentation:
    """Запасная нарезка без модели: каждый содержательный блок становится требованием."""
    requirements: list[Requirement] = []
    excluded: list[ExcludedBlock] = []
    for block in doc.blocks:
        label, reason = _label(block)
        if label is not BlockLabel.REQUIREMENT:
            excluded.append(ExcludedBlock(block_id=block.id, label=label, reason=reason))
            continue
        requirements.append(
            Requirement(
                id=requirement_id(len(requirements) + 1),
                source_id=block.source_id,
                source_locator=_locator(block),
                section_path=list(block.section),
                quote=block.text,
                summary=_summary(block),
                anchor=block.anchor,
                decided_by=DecidedBy.CODE,
                stable_key=stable_key(block.text, block.source_id or ""),
            )
        )
    return Segmentation(requirements=requirements, excluded=excluded, mode=SegmentationMode.FALLBACK)


def segment(doc: ParsedDocument, llm: LLMClient, cfg: RunConfig) -> Segmentation:
    """Заглушка: модель не вызывается, нарезка запасным путём (#19 заменит)."""
    del llm, cfg  # понадобятся настоящей нарезке
    return fallback_segmentation(doc)
