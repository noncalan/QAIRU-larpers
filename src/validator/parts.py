"""Длинный документ по частям (#38): план частей, вид части для модели, оглавление.

Документ для модели (``render_for_model``) длиннее ``config.LONG_DOC_TOKENS`` режется на части по разделам
верхнего уровня (исследование 18, п. 3.5: по структуре, а не по символам; блоки атомарны, перекрытия нет):

1. Блоки группируются в разделы верхнего уровня (заголовок-название документа не считается разделом).
   Раздел длиннее ``LONG_DOC_TOKENS`` делится по подразделам, а без них окнами из целых блоков.
2. Разделы подряд набираются в часть до ``PART_TARGET_TOKENS`` (запас до ``LONG_DOC_TOKENS``).
3. Раздел терминов и определений (по заголовку) идёт в каждую часть для справки, если он не длиннее
   ``GLOSSARY_MAX_TOKENS``; проверяет и нарезает его только своя часть.
4. Перед блоками части модель видит служебную пометку ``prompts/part_note.md``: номер части, её разделы
   и оглавление всего документа, чтобы не считать пропуском то, что лежит в другой части.

Вид части это тот же ``ParsedDocument`` с тем же каноническим текстом, но только с блоками части: ID блоков
и смещения якорей глобальные, а ссылка модели на блок чужой части отбрасывается как несуществующая.
Требования нумерует код по всему документу сразу (``segment``), поэтому ``R-NN`` тоже сквозные.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from validator.config import (
    FULL_VIEW_MAX_TOKENS,
    GLOSSARY_MAX_TOKENS,
    LONG_DOC_TOKENS,
    PART_TARGET_TOKENS,
    estimate_tokens,
)
from validator.ingest.blocks import document_title, render_block, render_for_model
from validator.models import Block, BlockKind, DocumentPart, ParsedDocument
from validator.prompting import fill, load_prompt

PART_NOTE = "part_note.md"
FOCUS_NOTE = "focus_note.md"
FOCUS_WINDOW_BLOCKS = 6
"""Если разделов с нужными местами слишком много, показываем столько блоков до и после каждого места."""
GLOSSARY_TITLE = re.compile(
    r"(?i)^[#\s]*(?:раздел\s+)?(?:[\dIVX]+(?:\.\d+)*[.)]?\s+)?(?:(?:список|перечень)\s+)?"
    r"(?:термины|терминов|определения|определений|глоссарий|сокращения|сокращений|аббревиатуры|"
    r"обозначения|обозначений|terms|definitions|glossary|abbreviations|acronyms)\b"
)
"""Заголовок раздела терминов: общий признак для любого документа, без слов конкретного ТЗ."""
OUTLINE_MAX_CHARS = 4_000
"""Оглавление в пометке части не длиннее этого (около 2 тыс. токенов): иначе только верхний уровень."""
OUTLINE_LEVELS = 2
"""Сколько уровней разделов показывать в оглавлении."""
PREAMBLE = "начало документа"
SMALL_SHARE = 4
"""Кусок меньше ``target // SMALL_SHARE`` токенов не идёт отдельной частью, если есть куда присоединить."""


@dataclass(frozen=True)
class Part:
    """Часть документа: свои блоки подряд по порядку документа и разделы верхнего уровня."""

    index: int
    blocks: tuple[Block, ...]
    sections: tuple[str, ...]
    tokens: int

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(b.id for b in self.blocks)


@dataclass(frozen=True)
class PartPlan:
    """Как документ идёт к модели: целиком (одна часть) или по частям."""

    parts: tuple[Part, ...]
    tokens: int
    glossary: tuple[Block, ...] = ()
    outline: str = ""
    _owner: dict[str, int] = field(default_factory=dict, repr=False, compare=False)

    @property
    def split(self) -> bool:
        return len(self.parts) > 1

    def part_of(self, block_id: str) -> int | None:
        """Номер части, которой принадлежит блок (или блок предложения ``B012.3``)."""
        return self._owner.get(block_id.split(".", 1)[0])

    def context_for(self, part: Part) -> tuple[Block, ...]:
        """Блоки раздела терминов, которых нет в самой части: их часть видит для справки."""
        own = part.ids
        return tuple(b for b in self.glossary if b.id not in own)


# ---------------------------------------------------------------- план


def _block_tokens(doc: ParsedDocument) -> dict[str, int]:
    title = document_title(doc)
    return {b.id: estimate_tokens(sum(len(line) + 1 for line in render_block(b, title))) for b in doc.blocks}


def _depth(doc: ParsedDocument) -> int:
    """Уровень разделов верхнего уровня в ``Block.section``: 1, если у документа есть общее название."""
    return 1 if document_title(doc) is not None else 0


def _runs(blocks: Sequence[Block], key: Callable[[Block], str | None]) -> list[list[Block]]:
    """Подряд идущие блоки с одним ключом раздела."""
    out: list[list[Block]] = []
    last: object = object()
    for block in blocks:
        k = key(block)
        if not out or k != last:
            out.append([])
            last = k
        out[-1].append(block)
    return out


def _section_key(level: int) -> Callable[[Block], str | None]:
    def key(block: Block) -> str | None:
        return block.section[level] if len(block.section) > level else None

    return key


def _windows(blocks: Sequence[Block], tokens: dict[str, int], target: int) -> list[list[Block]]:
    """Окна из целых блоков не больше ``target`` (блок больше окна идёт один)."""
    out: list[list[Block]] = [[]]
    size = 0
    for block in blocks:
        if out[-1] and size + tokens[block.id] > target:
            out.append([])
            size = 0
        out[-1].append(block)
        size += tokens[block.id]
    return out


def _units(
    blocks: Sequence[Block], level: int, tokens: dict[str, int], limit: int, target: int
) -> list[list[Block]]:
    """Разделы уровня ``level``; раздел больше ``limit`` делится на подразделы, без них на окна блоков."""
    out: list[list[Block]] = []
    for run in _runs(blocks, _section_key(level)):
        if sum(tokens[b.id] for b in run) <= limit:
            out.append(run)
            continue
        deeper = _runs(run, _section_key(level + 1))
        if len(deeper) > 1:
            out.extend(_units(run, level + 1, tokens, limit, target))
        else:
            out.extend(_windows(run, tokens, target))
    return out


def _pack(units: Sequence[list[Block]], tokens: dict[str, int], target: int, limit: int) -> list[list[Block]]:
    """Разделы подряд в части не больше ``target``; раздел крупнее идёт отдельной частью.

    Маленький кусок (вступление, короткое приложение, меньше ``target // SMALL_SHARE``) присоединяется
    к соседнему разделу, если вместе они не длиннее ``limit``: отдельный вызов на него был бы пустой тратой.
    """
    out: list[list[Block]] = []
    size = 0
    for unit in units:
        unit_size = sum(tokens[b.id] for b in unit)
        small = min(size, unit_size) < target // SMALL_SHARE and size + unit_size <= limit
        if out and (size + unit_size <= target or small):
            out[-1].extend(unit)
            size += unit_size
            continue
        out.append(list(unit))
        size = unit_size
    return out


def _glossary(doc: ParsedDocument, tokens: dict[str, int], limit: int) -> tuple[Block, ...]:
    """Раздел терминов и определений: первый по порядку, который не длиннее ``limit``.

    Заголовок должен начинаться со слова раздела терминов («Термины и определения», «2. Глоссарий»,
    «Список сокращений»): «Требования к терминалу» или «Определение статуса» разделом терминов не считаются.
    """
    for heading in doc.blocks:
        if heading.kind is not BlockKind.HEADING or not GLOSSARY_TITLE.search(heading.text):
            continue
        level = len(heading.section) - 1
        title = heading.section[level] if heading.section else None
        blocks = tuple(
            b
            for b in doc.blocks
            if len(b.section) > level
            and b.section[level] == title
            and b.section[:level] == heading.section[:level]
        )
        if sum(tokens[b.id] for b in blocks) <= limit:
            return blocks
    return ()


def _outline(doc: ParsedDocument, depth: int) -> str:
    """Оглавление без ID блоков: модель не может сослаться на блок чужой части."""
    headings = [b for b in doc.blocks if b.kind is BlockKind.HEADING and len(b.section) > depth]
    for levels in (OUTLINE_LEVELS, 1):
        lines = [
            f"{'  ' * (len(b.section) - depth - 1)}- {' '.join(b.section[-1].split())}"
            for b in headings
            if len(b.section) - depth <= levels
        ]
        text = "\n".join(lines)
        if len(text) <= OUTLINE_MAX_CHARS:
            return text
    return text[:OUTLINE_MAX_CHARS].rsplit("\n", 1)[0] + "\n- …"


def whole_plan(doc: ParsedDocument) -> PartPlan:
    """Документ целиком одной частью: запасной путь, если план частей не построился."""
    tokens = estimate_tokens(len(doc.text))
    whole = Part(1, tuple(doc.blocks), (), tokens)
    return PartPlan(parts=(whole,), tokens=tokens, _owner={b.id: 1 for b in doc.blocks})


def plan_parts(
    doc: ParsedDocument,
    *,
    limit: int = LONG_DOC_TOKENS,
    target: int = PART_TARGET_TOKENS,
    glossary_limit: int = GLOSSARY_MAX_TOKENS,
) -> PartPlan:
    """План частей. Документ не длиннее ``limit`` токенов идёт целиком одной частью."""
    tokens = _block_tokens(doc)
    total = sum(tokens.values())
    if total <= limit or len(doc.blocks) < 2:
        whole = Part(1, tuple(doc.blocks), (), total)
        return PartPlan(parts=(whole,), tokens=total, _owner={b.id: 1 for b in doc.blocks})
    depth = _depth(doc)
    groups = _pack(_units(doc.blocks, depth, tokens, limit, target), tokens, target, limit)
    key = _section_key(depth)
    parts = tuple(
        Part(
            index=k,
            blocks=tuple(group),
            sections=tuple(dict.fromkeys(key(b) or PREAMBLE for b in group)),
            tokens=sum(tokens[b.id] for b in group),
        )
        for k, group in enumerate(groups, start=1)
    )
    owner = {b.id: p.index for p in parts for b in p.blocks}
    return PartPlan(
        parts=parts,
        tokens=total,
        glossary=_glossary(doc, tokens, glossary_limit),
        outline=_outline(doc, depth),
        _owner=owner,
    )


# ---------------------------------------------------------------- вид части для модели


def view_document(doc: ParsedDocument, blocks: Sequence[Block]) -> ParsedDocument:
    """Документ только с этими блоками: тот же текст и якоря, ссылки на чужие блоки не найдутся.

    ``model_construct``, а не ``model_copy``: копия унесла бы закешированный индекс блоков целого документа.
    """
    fields = {name: getattr(doc, name) for name in type(doc).model_fields}
    return type(doc).model_construct(**{**fields, "blocks": list(blocks)})


def _range(blocks: Sequence[Block]) -> str:
    return blocks[0].id if len(blocks) == 1 else f"{blocks[0].id}–{blocks[-1].id}"


def _sections(part: Part) -> str:
    return "; ".join(f"«{' '.join(s.split())}»" for s in part.sections)


def part_note(plan: PartPlan, part: Part, context: Sequence[Block] = ()) -> str:
    """Служебная пометка перед блоками части: номер, разделы, оглавление, блоки для справки."""
    reference = ""
    if context:
        reference = (
            f" Блоки {_range(context)} из раздела терминов показаны для справки: "
            "их проверяет та часть, где они стоят, находки по ним самим здесь не нужны."
        )
    return fill(
        load_prompt(PART_NOTE),
        index=str(part.index),
        total=str(len(plan.parts)),
        sections=_sections(part),
        reference=reference,
        outline=plan.outline,
    ).rstrip("\n")


def render_part(doc: ParsedDocument, plan: PartPlan, part: Part, *, with_context: bool) -> str:
    """Текст части для модели: пометка, затем блоки (с разделом терминов для справки, если ``with_context``).

    Документ целиком (одна часть) рендерится как раньше, без пометки: промпт и кеш не меняются.
    """
    if not plan.split:
        return render_for_model(doc)
    context = plan.context_for(part) if with_context else ()
    blocks = sorted([*context, *part.blocks], key=lambda b: _order(doc)[b.id])
    return part_note(plan, part, context) + "\n\n" + render_for_model(doc, blocks)


def part_view(doc: ParsedDocument, plan: PartPlan, part: Part, *, with_context: bool) -> ParsedDocument:
    """Документ для проверки ссылок модели в части: свои блоки и, по желанию, раздел терминов."""
    if not plan.split:
        return doc
    context = plan.context_for(part) if with_context else ()
    order = _order(doc)
    return view_document(doc, sorted([*context, *part.blocks], key=lambda b: order[b.id]))


def _order(doc: ParsedDocument) -> dict[str, int]:
    return {b.id: k for k, b in enumerate(doc.blocks)}


def focus_view(doc: ParsedDocument, block_ids: Iterable[str], *, limit: int = FULL_VIEW_MAX_TOKENS) -> str:
    """Документ для вопросов автору и критика (#38): целиком, если не длиннее ``limit``, иначе только
    разделы верхнего уровня с блоками ``block_ids`` и раздел терминов, перед ними пометка с оглавлением.

    Разделов с нужными местами слишком много: по ``FOCUS_WINDOW_BLOCKS`` блоков вокруг мест в их порядке,
    пока вход не длиннее ``limit``.
    """
    full = render_for_model(doc)
    if estimate_tokens(len(full)) <= limit:
        return full
    tokens = _block_tokens(doc)
    depth = _depth(doc)
    key = _section_key(depth)
    wanted = list(dict.fromkeys(b.split(".", 1)[0] for b in block_ids))
    glossary = {b.id for b in _glossary(doc, tokens, GLOSSARY_MAX_TOKENS)}
    sections = {key(b) for b in doc.blocks if b.id in wanted}
    chosen = [b for b in doc.blocks if b.id in glossary or key(b) in sections]
    if sum(tokens[b.id] for b in chosen) > limit:
        chosen = _windows_around(doc, wanted, glossary, tokens, limit)
    note = fill(load_prompt(FOCUS_NOTE), outline=_outline(doc, depth)).rstrip("\n")
    return note + "\n\n" + render_for_model(doc, chosen)


def _windows_around(
    doc: ParsedDocument, wanted: Sequence[str], glossary: set[str], tokens: dict[str, int], limit: int
) -> list[Block]:
    """Раздел терминов и по ``FOCUS_WINDOW_BLOCKS`` блоков вокруг мест ``wanted`` в их порядке (сначала
    важные), пока вход не длиннее ``limit``; окно первого места берётся всегда."""
    order = {b.id: k for k, b in enumerate(doc.blocks)}
    taken = set(glossary)
    size = sum(tokens[b] for b in taken)
    for k in (order[b] for b in wanted if b in order):
        lo, hi = max(0, k - FOCUS_WINDOW_BLOCKS), min(len(doc.blocks), k + FOCUS_WINDOW_BLOCKS + 1)
        window = {doc.blocks[j].id for j in range(lo, hi)} - taken
        extra = sum(tokens[b] for b in window)
        if size + extra > limit and taken - glossary:
            break
        taken |= window
        size += extra
    return [b for b in doc.blocks if b.id in taken]


def describe(plan: PartPlan, doc: ParsedDocument) -> list[DocumentPart]:
    """Части для отчёта (``DocumentInfo.parts``); пусто, если документ проверялся целиком."""
    if not plan.split:
        return []
    out: list[DocumentPart] = []
    for part in plan.parts:
        pages = sorted({b.anchor.page for b in part.blocks if b.anchor.page is not None})
        shown = "" if not pages else str(pages[0]) if pages[0] == pages[-1] else f"{pages[0]}–{pages[-1]}"
        out.append(
            DocumentPart(
                index=part.index,
                sections=[" ".join(s.split()) for s in part.sections],
                first_block=part.blocks[0].id,
                last_block=part.blocks[-1].id,
                blocks=len(part.blocks),
                pages=shown,
                tokens=part.tokens,
                context_blocks=len(plan.context_for(part)),
            )
        )
    return out


__all__ = [
    "Part",
    "PartPlan",
    "describe",
    "focus_view",
    "part_note",
    "part_view",
    "plan_parts",
    "render_part",
    "view_document",
    "whole_plan",
]
