"""Правила по всему набору: L-07 (ссылки), L-08 (повтор ID), L-09 (пропуск в нумерации).

Им нужен весь документ, а не одно предложение: какие метки требований и какие разделы там есть.
Метки берём из ``Block.source_id`` (их ставит разбор документа), заголовки из блоков-заголовков.
Срабатывают правила только на единицах и блоках внутри требований нарезки.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import cached_property
from itertools import pairwise

from validator.lint.hits import Hit
from validator.lint.lexicon import fold
from validator.lint.rules import RULE_BY_ID
from validator.lint.units import Unit, quote_span
from validator.models import Block, BlockKind, ParsedDocument

MIN_FAMILY_FOR_GAPS = 3
"""L-09: ряд короче этого не проверяем, по двум меткам ряд не виден."""

MAX_MISSING_IN_GAP = 3
"""L-09: пропущено больше номеров подряд: нумерация шагами (10, 20, 30) или с резервом, а не потерянные."""

MIN_FAMILY_FOR_REFS = 2
"""L-07: у префикса метки не меньше стольких разных номеров, чтобы считать его схемой нумерации."""

MIN_NUMBERED_HEADINGS = 2
MIN_HEADINGS_FOR_NAMES = 3
MAX_QUOTES_PER_HIT = 4

_CELL_ID = re.compile(r"^(?P<id>(?:[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё]{0,5}[-\u2011.\u2010_]|[A-ZА-ЯЁ]{1,6})\d+)$")
_ID_PARTS = re.compile(r"^(?P<prefix>[^\d.]+)(?P<digits>\d+)$")
_HEADING_NUMBER = re.compile(r"^\s*#*\s*(?P<num>\d+(?:\.\d+)*)[.)]?\s+\S")
_HEADING_TITLE = re.compile(r"^\s*#*\s*(?:\d+(?:\.\d+)*[.)]?\s+)?(?P<title>.*?)\s*#*\s*$")
_SECTION_NUMBER_REF = re.compile(
    r"(?<!\w)(?:раздел(?:а|е|у|ом|ы|ов)?|глав(?:а|е|у|ой)|section|chapter)\s+(?P<num>\d+(?:\.\d+)*)(?![\d\w])"
)
_SECTION_NAME_REF = re.compile(
    r"(?<!\w)(?:раздел(?:а|е|у|ом|ы|ов)?|глав(?:а|е|у|ой)|section|chapter)\s+[«\"“](?P<name>[^»\"”\n]{3,80})[»\"”]"
)
_OTHER_DOCUMENT = re.compile(
    r"документ|приложени|специфик|стандарт|регламент|договор|закон|гост|ieee|iso|rfc|document|appendix|spec"
)
_THIS_DOCUMENT = re.compile(r"(?:этого|настоящего|данного|текущего)\s+документа|this\s+document")
_MARKUP = re.compile(r"[*_`|#>]")


def _row_id(block: Block) -> str | None:
    """Метка в первой ячейке строки таблицы («ID | Требование»): разбор документа её не отмечает."""
    if block.kind is not BlockKind.TABLE_ROW or not block.cells:
        return None
    match = _CELL_ID.match(block.cells[0].text.replace("**", "").replace("__", "").strip())
    return match["id"].replace("\u2011", "-") if match else None


@dataclass(frozen=True)
class DocContext:
    """Что знает правило о документе: метки требований, заголовки, единицы внутри требований."""

    doc: ParsedDocument
    units: list[Unit]

    @cached_property
    def scanned_blocks(self) -> frozenset[str]:
        return frozenset(u.block_id for u in self.units)

    @cached_property
    def defined_ids(self) -> list[tuple[Block, str]]:
        """Все метки требований документа по порядку (не только внутри нарезки): чтобы знать, что есть."""
        found: list[tuple[Block, str]] = []
        for block in self.doc.blocks:
            ident = block.source_id or _row_id(block)
            if ident and block.kind is not BlockKind.HEADING:
                found.append((block, ident))
        return found

    @cached_property
    def families(self) -> dict[str, dict[int, tuple[Block, str]]]:
        """Префикс метки («ABC-») → номер → (блок, метка). Только простые метки «префикс + число»."""
        out: dict[str, dict[int, tuple[Block, str]]] = defaultdict(dict)
        for block, ident in self.defined_ids:
            parts = _ID_PARTS.match(ident)
            if parts:
                out[fold(parts["prefix"])].setdefault(int(parts["digits"]), (block, ident))
        return dict(out)

    @cached_property
    def heading_numbers(self) -> frozenset[str]:
        found = (_HEADING_NUMBER.match(b.text) for b in self.doc.blocks if b.kind is BlockKind.HEADING)
        return frozenset(m["num"] for m in found if m)

    @cached_property
    def heading_titles(self) -> list[str]:
        titles: list[str] = []
        for b in self.doc.blocks:
            if b.kind is BlockKind.HEADING:
                m = _HEADING_TITLE.match(_MARKUP.sub(" ", b.text))
                if m and m["title"]:
                    titles.append(" ".join(fold(m["title"]).split()))
        return titles

    def id_quote(self, block: Block, ident: str) -> tuple[int, int]:
        """Цитата с меткой: начало блока, где стоит метка, суженное до окна."""
        pos = self.doc.text.find(ident, block.anchor.char_start, block.anchor.char_end)
        a = pos if pos >= 0 else block.anchor.char_start
        unit = Unit(block.anchor.char_start, block.anchor.char_end, "", block.id)
        return quote_span(self.doc.text, unit, a, a + len(ident))


def check_duplicate_ids(ctx: DocContext) -> list[Hit]:
    """L-08: одна метка у двух разных блоков внутри требований."""
    rule = RULE_BY_ID["L-08"]
    seen: dict[str, list[tuple[Block, str]]] = defaultdict(list)
    for block, ident in ctx.defined_ids:
        if block.id in ctx.scanned_blocks:
            seen[fold(ident)].append((block, ident))
    hits = []
    for entries in seen.values():
        if len(entries) < 2:
            continue
        ident = entries[0][1]
        spans = tuple(ctx.id_quote(b, i) for b, i in entries[:MAX_QUOTES_PER_HIT])
        hits.append(
            Hit(
                rule=rule,
                spans=spans,
                title=f"Повтор метки «{ident}»",
                explanation=(
                    f"Метка «{ident}» стоит у {len(entries)} разных требований. Ссылка на неё неоднозначна: "
                    "непонятно, какое требование имеется в виду, и тест нельзя привязать к одному из них."
                ),
                suggestion="Дать каждому требованию уникальную метку и перепроверить ссылки на старую.",
            )
        )
    return hits


def _format_id(prefix_source: str, number: int, width: int) -> str:
    parts = _ID_PARTS.match(prefix_source)
    prefix = parts["prefix"] if parts else prefix_source
    return f"{prefix}{number:0{width}d}"


def check_numbering_gaps(ctx: DocContext) -> list[Hit]:
    """L-09: в ряду меток с одним префиксом не хватает номера."""
    rule = RULE_BY_ID["L-09"]
    hits: list[Hit] = []
    for members in ctx.families.values():
        numbers = sorted(members)
        if len(numbers) < MIN_FAMILY_FOR_GAPS or numbers[0] > 1:
            continue
        for before, after in pairwise(numbers):
            missing = list(range(before + 1, after))
            if not missing or len(missing) > MAX_MISSING_IN_GAP:
                continue
            (b_block, b_id), (a_block, a_id) = members[before], members[after]
            if b_block.id not in ctx.scanned_blocks and a_block.id not in ctx.scanned_blocks:
                continue
            width = len(re.sub(r"\D", "", b_id)) if re.search(r"\D0\d", b_id) else 0
            names = ", ".join(f"«{_format_id(b_id, n, width)}»" for n in missing)
            hits.append(
                Hit(
                    rule=rule,
                    spans=(ctx.id_quote(b_block, b_id), ctx.id_quote(a_block, a_id)),
                    title=f"Пропущен номер {names}",
                    explanation=(
                        f"Между метками «{b_id}» и «{a_id}» нет {names}. Либо требование потеряно или "
                        "удалено без следа, либо нумерация сбита, и ссылки из тестов и других документов "
                        "могут вести не туда."
                    ),
                    suggestion="Проверить, было ли требование: [уточнить: удалено, перенесено, потеряно].",
                )
            )
    return hits


def _reference_regex(ctx: DocContext) -> re.Pattern[str] | None:
    prefixes = [p for p, members in ctx.families.items() if len(members) >= MIN_FAMILY_FOR_REFS]
    if not prefixes:
        return None
    body = "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?P<prefix>{body})(?P<digits>\d+)(?![\d\w]|\.\d)")


def _heading_known(ctx: DocContext, name: str) -> bool:
    wanted = " ".join(fold(name).split()).strip(" .,:;")
    return any(
        title and (wanted in title or title in wanted) for title in ctx.heading_titles if len(title) >= 4
    )


def _broken_refs(ctx: DocContext, unit: Unit, own_id: str | None) -> list[tuple[int, int, str]]:
    """Ссылки внутри единицы, которым нет адресата: (начало, конец, текст ссылки)."""
    found: list[tuple[int, int, str]] = []
    pattern = _reference_regex(ctx)
    text = unit.folded
    if pattern:
        for m in pattern.finditer(text):
            is_own_label = own_id is not None and m.start() <= 8 and m.group(0) == fold(own_id)
            if not is_own_label and int(m["digits"]) not in ctx.families[m["prefix"]]:
                found.append((m.start(), m.end(), unit.text[m.start() : m.end()]))
    # Ссылка на раздел чужого документа («IEEE 830, раздел 4.3», «приложение к договору») не наша.
    foreign = _OTHER_DOCUMENT.search(_THIS_DOCUMENT.sub(" ", text)) is not None
    numbered = ctx.heading_numbers
    if len(numbered) >= MIN_NUMBERED_HEADINGS and not foreign:
        depths = {n.count(".") for n in numbered}
        for m in _SECTION_NUMBER_REF.finditer(text):
            if m["num"] not in numbered and m["num"].count(".") in depths:
                found.append((m.start(), m.end(), unit.text[m.start() : m.end()]))
    if len(ctx.heading_titles) >= MIN_HEADINGS_FOR_NAMES and not foreign:
        for m in _SECTION_NAME_REF.finditer(text):
            if not _heading_known(ctx, m["name"]):
                found.append((m.start(), m.end(), unit.text[m.start() : m.end()]))
    return sorted(found)


def check_broken_references(ctx: DocContext) -> list[Hit]:
    """L-07: ссылка на метку требования или раздел, которых в документе нет."""
    rule = RULE_BY_ID["L-07"]
    hits: list[Hit] = []
    for unit in ctx.units:
        block = ctx.doc.block(unit.block_id)
        own_id = block.source_id if block else None
        refs = _broken_refs(ctx, unit, own_id)
        if not refs:
            continue
        a, b = unit.start + refs[0][0], unit.start + refs[-1][1]
        lo, hi = quote_span(ctx.doc.text, unit, a, b)
        if not (lo <= a and b <= hi):
            lo, hi = quote_span(ctx.doc.text, unit, a, unit.start + refs[0][1])
        names = ", ".join(dict.fromkeys(f"«{' '.join(text.split())}»" for _, _, text in refs))
        hits.append(
            Hit(
                rule=rule,
                spans=((lo, hi),),
                title=f"Ссылка в никуда {names}",
                explanation=(
                    f"Ссылка {names} ведёт на метку или раздел, которых в документе нет. Читатель не найдёт, "
                    "на что опирается требование, а проверить его по недостающему тексту нельзя."
                ),
                suggestion="Исправить адресата ссылки или добавить раздел: [уточнить: на что ссылка].",
            )
        )
    return hits


DOC_CHECKS = {
    "L-07": check_broken_references,
    "L-08": check_duplicate_ids,
    "L-09": check_numbering_gaps,
}
