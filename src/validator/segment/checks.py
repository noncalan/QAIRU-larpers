"""Ответ модели → нарезка с проверками кодом (исследование 18, пп. 3.1 и 4.2).

- несуществующие ID отбрасываются, требование без единого существующего ID не входит в нарезку
  (диагностика ``LLM-021``);
- полнота: каждый блок и каждое предложение либо в требованиях, либо исключены с причиной. Фрагмент без
  решения модели код берёт сам (от 3 слов это требование, recall важнее точности) и пишет ``LLM-022``,
  а фрагменты с признаками обязательства перечисляет как «возможно пропущено»;
- пересекающиеся требования дают предупреждение ``LLM-023``;
- ни одного требования: ``IN-081`` (пустой шаблон или не ТЗ).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from validator.errors import (
    IN_NO_REQUIREMENTS,
    LLM_SEGMENT_BAD_REFS,
    LLM_SEGMENT_OVERLAP,
    LLM_SEGMENT_UNCOVERED,
)
from validator.ingest.blocks import OBLIGATION
from validator.models import (
    Block,
    BlockKind,
    BlockLabel,
    DecidedBy,
    DiagLevel,
    Diagnostic,
    ExcludedBlock,
    ParsedDocument,
    Requirement,
    Segmentation,
    SegmentationMode,
)
from validator.segment.answer import EXCLUDE_LABELS, AnswerError, ModelAnswer, ModelExclusion, ModelItem
from validator.segment.build import (
    MIN_WORDS,
    Draft,
    block_draft,
    build_requirements,
    fallback_label,
    word_count,
)
from validator.segment.spans import Piece, content_span, narrow, resolve, runs, trim

MAX_REASON_CHARS = 200
MAX_LISTED_IDS = 12
"""Сколько ID перечислять в тексте диагностики, остальное «и ещё N»."""


@dataclass
class Notes:
    """Что заметил код при сборке нарезки: из этого получаются диагностики."""

    unknown_refs: list[str] = field(default_factory=list)
    corrected_refs: list[str] = field(default_factory=list)
    parts_not_found: list[str] = field(default_factory=list)
    dropped_items: int = 0
    invalid: int = 0
    duplicates: int = 0
    conflicts: list[str] = field(default_factory=list)
    uncovered: list[str] = field(default_factory=list)
    uncovered_signals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- требования модели


def _pieces(doc: ParsedDocument, item: ModelItem, notes: Notes) -> list[Piece]:
    out: list[Piece] = []
    for raw in item.refs:
        piece, corrected = resolve(doc, raw)
        if piece is None:
            notes.unknown_refs.append(raw.strip() or "(пусто)")
            continue
        if corrected:
            notes.corrected_refs.append(raw.strip())
        out.append(piece)
    return out


def _drafts_for(doc: ParsedDocument, item: ModelItem, notes: Notes) -> list[Draft]:
    """Черновики одного требования модели: по одному на непрерывный кусок текста.

    Несмежные ссылки (B012.1 и B012.3) не склеиваются в одну цитату с чужим текстом посередине:
    каждый кусок становится своим требованием. Границы части предложения применяются, только если
    кусок один.
    """
    pieces = _pieces(doc, item, notes)
    if not pieces:
        notes.dropped_items += 1
        return []
    chunks = runs(doc.text, pieces)
    wants_part = bool(item.part_start.strip() or item.part_end.strip())
    out: list[Draft] = []
    for chunk in chunks:
        start, end, part = chunk[0].start, max(p.end for p in chunk), False
        if wants_part and len(chunks) == 1:
            bounds = narrow(doc.text, start, end, item.part_start, item.part_end)
            if bounds is None:
                notes.parts_not_found.append(chunk[0].ref)
            else:
                part = bounds != (start, end)
                start, end = bounds
        block = chunk[0].block
        start, end = trim(doc, block, start, end)
        if end > start:
            out.append(Draft(start, end, block, DecidedBy.MODEL, item=item, part=part))
    return out


def _model_drafts(doc: ParsedDocument, items: Sequence[ModelItem], notes: Notes) -> list[Draft]:
    """Черновики всех требований модели без повторов одного и того же фрагмента."""
    seen: set[tuple[int, int]] = set()
    out: list[Draft] = []
    for draft in (d for item in items for d in _drafts_for(doc, item, notes)):
        if (draft.start, draft.end) in seen:
            notes.duplicates += 1
            continue
        seen.add((draft.start, draft.end))
        out.append(draft)
    return out


def _exclusions(
    doc: ParsedDocument, exclusions: Sequence[ModelExclusion], notes: Notes
) -> dict[str, ExcludedBlock]:
    """Исключения модели по ID блока или предложения; неизвестная метка считается пояснением."""
    out: dict[str, ExcludedBlock] = {}
    for ex in exclusions:
        piece, _ = resolve(doc, ex.block)
        if piece is None:
            notes.unknown_refs.append(ex.block.strip() or "(пусто)")
            continue
        try:
            label = BlockLabel(ex.label.strip().lower())
        except ValueError:
            label = BlockLabel.CONTEXT
        if label not in EXCLUDE_LABELS:
            label = BlockLabel.CONTEXT
        reason = " ".join(ex.reason.split())[:MAX_REASON_CHARS]
        out.setdefault(piece.ref, ExcludedBlock(block_id=piece.ref, label=label, reason=reason))
    return out


# ---------------------------------------------------------------- полнота


@dataclass(frozen=True)
class _Unit:
    """Единица полноты: предложение блока или блок целиком (строка таблицы, код, заголовок)."""

    id: str
    start: int
    end: int
    text: str


def _units(block: Block) -> list[_Unit]:
    if block.kind is not BlockKind.TABLE_ROW and block.sentences:
        return [_Unit(s.id, s.char_start, s.char_end, s.text) for s in block.sentences]
    start, end = content_span(block)
    return [_Unit(block.id, start, end, block.serialized or block.text)]


def _is_covered(unit: _Unit, drafts: Sequence[Draft]) -> bool:
    return any(d.start < unit.end and unit.start < d.end for d in drafts)


def _undecided_block(doc: ParsedDocument, block: Block, notes: Notes) -> Draft | ExcludedBlock:
    """Блок, по которому модель не дала решения: решает код, как в запасной нарезке."""
    label, reason = fallback_label(block)
    if block.kind is not BlockKind.HEADING:  # заголовки модель не перечисляет: это не пропуск
        notes.uncovered.append(block.id)
        if "obligation" in block.hints or block.source_id:
            notes.uncovered_signals.append(block.id)
    if label is BlockLabel.REQUIREMENT:
        return block_draft(doc, block)
    return ExcludedBlock(block_id=block.id, label=label, reason=reason)


def _partly_decided(
    doc: ParsedDocument,
    block: Block,
    units: Sequence[_Unit],
    drafts: Sequence[Draft],
    excluded: dict[str, ExcludedBlock],
    notes: Notes,
) -> list[Draft | ExcludedBlock]:
    """Предложения блока без требования: исключение модели (своё или блока), иначе решает код."""
    own = excluded.get(block.id)
    out: list[Draft | ExcludedBlock] = []
    for unit in units:
        if _is_covered(unit, drafts):
            continue
        decision = excluded.get(unit.id) or own
        if decision is not None:
            out.append(decision.model_copy(update={"block_id": unit.id}))
        elif word_count(unit.text) >= MIN_WORDS:
            notes.uncovered.append(unit.id)
            if OBLIGATION.search(unit.text):
                notes.uncovered_signals.append(unit.id)
            start, end = trim(doc, block, unit.start, unit.end)
            out.append(Draft(start, end, block, DecidedBy.CODE))
    return out


def cover(
    doc: ParsedDocument, drafts: list[Draft], excluded: dict[str, ExcludedBlock], notes: Notes
) -> tuple[list[Draft], list[ExcludedBlock]]:
    """Каждый блок либо в требованиях, либо исключён. Пробелы закрывает код: пропуск дороже лишнего."""
    added: list[Draft] = []
    kept: list[ExcludedBlock] = []
    for block in doc.blocks:
        units = _units(block)
        covered = any(_is_covered(u, drafts) for u in units)
        own = excluded.get(block.id)
        if not covered and own is not None:
            kept.append(own)
            continue
        if not covered and not any(u.id in excluded for u in units):
            decided = [_undecided_block(doc, block, notes)]
        else:
            if covered and own is not None:
                notes.conflicts.append(block.id)
            decided = _partly_decided(doc, block, units, drafts, excluded, notes)
        added.extend(d for d in decided if isinstance(d, Draft))
        kept.extend(d for d in decided if isinstance(d, ExcludedBlock))
    return drafts + added, kept


# ---------------------------------------------------------------- диагностика


def _listed(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:MAX_LISTED_IDS])
    rest = len(ids) - MAX_LISTED_IDS
    return f"{shown} и ещё {rest}" if rest > 0 else shown


def overlaps(reqs: Sequence[Requirement]) -> list[str]:
    """Пары требований, чьи фрагменты пересекаются (требования отсортированы по началу)."""
    out: list[str] = []
    prev: Requirement | None = None
    for req in reqs:
        if prev is not None and req.anchor.char_start < prev.anchor.char_end:
            out.append(f"{prev.id} и {req.id}")
        if prev is None or req.anchor.char_end > prev.anchor.char_end:
            prev = req
    return out


def _bad_refs(notes: Notes) -> Diagnostic | None:
    problems: list[str] = []
    if notes.unknown_refs:
        problems.append(f"несуществующие ID {_listed(notes.unknown_refs)}")
    if notes.dropped_items:
        problems.append(f"требований без единого существующего ID: {notes.dropped_items}")
    if notes.invalid:
        problems.append(f"записей не по схеме: {notes.invalid}")
    if not problems:
        return None
    return Diagnostic(
        code=LLM_SEGMENT_BAD_REFS,
        level=DiagLevel.WARNING,
        message="Ответ нарезки с ошибками: " + "; ".join(problems),
        action="Ошибочные ссылки и записи отброшены, остальная нарезка принята",
        hint="Сверьте таблицу нарезки с документом; если повторяется, сообщите команде",
        step="segment",
    )


def _corrections(notes: Notes) -> Diagnostic | None:
    fixes: list[str] = []
    if notes.corrected_refs:
        fixes.append(f"ID предложений заменены на ID блока: {_listed(notes.corrected_refs)}")
    if notes.parts_not_found:
        parts = _listed(notes.parts_not_found)
        fixes.append(f"границы части не найдены, взято предложение целиком: {parts}")
    if notes.duplicates:
        fixes.append(f"повторно размеченных фрагментов убрано: {notes.duplicates}")
    if notes.conflicts:
        fixes.append(
            f"блок и исключён, и взят в требования (оставлено требование): {_listed(notes.conflicts)}"
        )
    if not fixes:
        return None
    return Diagnostic(
        code=LLM_SEGMENT_BAD_REFS,
        level=DiagLevel.INFO,
        message="Нарезка поправлена кодом: " + "; ".join(fixes),
        action="Цитаты взяты из документа по исправленным ссылкам",
        step="segment",
    )


def _uncovered(notes: Notes) -> Diagnostic | None:
    if not notes.uncovered:
        return None
    signals = notes.uncovered_signals
    tail = f"; с признаками требования, возможно пропущены моделью: {_listed(signals)}" if signals else ""
    count, listed = len(notes.uncovered), _listed(notes.uncovered)
    return Diagnostic(
        code=LLM_SEGMENT_UNCOVERED,
        level=DiagLevel.WARNING,
        message=f"Модель не дала решения по фрагментам ({count}): {listed}{tail}",
        action=f"Фрагменты от {MIN_WORDS} слов взяты в нарезку кодом как требования, остальные исключены",
        hint="Проверьте эти места в таблице нарезки; в report.json у них decided_by = code",
        step="segment",
    )


def _overlap(reqs: Sequence[Requirement]) -> Diagnostic | None:
    pairs = overlaps(reqs)
    if not pairs:
        return None
    return Diagnostic(
        code=LLM_SEGMENT_OVERLAP,
        level=DiagLevel.WARNING,
        message=f"Требования пересекаются по тексту: {_listed(pairs)}",
        action="Оба требования оставлены, находки привяжутся к каждому",
        hint="Сверьте границы этих требований в таблице нарезки",
        step="segment",
    )


def no_requirements(doc: ParsedDocument) -> list[Diagnostic]:
    """``IN-081`` для нарезки без требований; если разбор уже выдал его («только заголовки»), не повторяем."""
    if any(d.code == IN_NO_REQUIREMENTS for d in doc.diagnostics):
        return []
    return [
        Diagnostic(
            code=IN_NO_REQUIREMENTS,
            level=DiagLevel.WARNING,
            message="В документе не найдено ни одного требования: похоже на пустой шаблон или не на ТЗ",
            action="Проверка выполнена, но требований для неё нет",
            hint="Проверьте, тот ли файл передан: в документе требований должны быть обязательства к системе",
            step="segment",
        )
    ]


def diagnostics(doc: ParsedDocument, notes: Notes, reqs: Sequence[Requirement]) -> list[Diagnostic]:
    found = [_bad_refs(notes), _corrections(notes), _uncovered(notes), _overlap(reqs)]
    out = [d for d in found if d is not None]
    return out if reqs else out + no_requirements(doc)


# ---------------------------------------------------------------- сборка


def from_answer(doc: ParsedDocument, answer: ModelAnswer) -> Segmentation:
    """Нарезка по ответу модели: цитаты из документа, проверки кодом, номера по порядку документа."""
    notes = Notes(invalid=answer.invalid)
    drafts = _model_drafts(doc, answer.items, notes)
    if answer.items and not drafts:
        raise AnswerError("ни одна ссылка ответа не найдена в документе")
    excluded = _exclusions(doc, answer.exclusions, notes)
    drafts, kept = cover(doc, drafts, excluded, notes)
    requirements = build_requirements(doc, drafts)
    order = {b.id: k for k, b in enumerate(doc.blocks)}

    def position(e: ExcludedBlock) -> tuple[int, int]:
        block_id, _, sentence = e.block_id.partition(".")
        return order.get(block_id, len(order)), int(sentence or 0)

    kept.sort(key=position)
    return Segmentation(
        requirements=requirements,
        excluded=kept,
        mode=SegmentationMode.MODEL,
        diagnostics=diagnostics(doc, notes, requirements),
    )
