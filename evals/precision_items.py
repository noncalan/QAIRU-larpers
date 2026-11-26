"""Находки для разметки precision (#49): общий пул агента и базовой линии, контрольные ложные, пачки.

Разметчик видит у находки только номер в пачке, цитату, место, свойство и объяснение. Источник
(агент или базовая линия), ``F-NN``, важность, проходы и признак контрольной находки остаются здесь,
в результатах eval, и в запрос не попадают (``LabelItem.shown``).

Контрольные ложные находки собирает код из настоящих, детерминированно по ``seed``:

- ``misplaced_quote``: свойство и объяснение настоящей находки, а цитата из другого места документа,
  с которым объяснение почти не делит слов;
- ``swapped_claim``: цитата и место одной находки, свойство и объяснение другой, из другого места;
- ``nitpick``: придирка к формулировке случайного фрагмента, которая сама говорит, что на разработку
  и тест не влияет.

Доля пойманных контрольных находок показывает, можно ли верить разметчику.
"""

from __future__ import annotations

import random
import re
from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from evals.findings import JudgeItem
from evals.freetext import QUOTE_JOIN
from evals.runs import RunData, Source
from validator.models import Block, BlockKind, Finding, ParsedDocument, QuoteMatch, SourceKind
from validator.report.fmt import where
from validator.text import locate_quote

CONTROL_SHARE = 0.125
"""Доля контрольных находок в пачке: issue #49 просит 10–15 %."""
DEFAULT_BATCH = 40
"""Настоящих находок в пачке: документ 8–15 тыс. символов плюс 40 находок помещаются в один вызов,
а на прогон агента и прогон базовой линии хватает 3 вызовов на разметчика (бюджет issue: 2–4)."""
DEFAULT_SEED = 49
MIN_CONTROL_CHARS = 40
MAX_CONTROL_CHARS = 220
MAX_OVERLAP = 0.2
"""Наибольшая доля общих слов между цитатой контрольной находки и объяснением донора."""
NITPICK_WORDS = 6
MIN_SWAP_QUOTE = 25
"""Цитата донора для «чужого объяснения»: короче похожа на пустую ячейку или имя поля."""
_CONTENT_WORD = re.compile(r"[0-9a-zа-я]{4,}")
_GUILLEMETS = re.compile(r"«([^«»\n]+)»")
_PIECE_SPLIT = re.compile(r"…|\.\.\.|→|\s/\s|\s\|\s")
MIN_PIECE_CHARS = 12
_SENTENCE_END = re.compile(r"(?<=[.!?;])\s+")
CONTROL_BLOCK_KINDS = frozenset({BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.TABLE_ROW})
NITPICK_PROPERTY = "Соответствие шаблону, прослеживаемость и оформление"
NITPICK_TEMPLATES = (
    "Фразу «{frag}» можно сформулировать короче и с другим порядком слов. Смысл требования и "
    "ожидаемый результат проверки от этого не меняются, но текст читался бы ровнее.",
    "Стилистика: в «{frag}» лучше выдержать единый стиль с соседними пунктами. На реализацию "
    "и тест-кейсы это не влияет.",
    "Предлагаю переписать «{frag}» более деловым языком. Содержание остаётся тем же, проверить "
    "требование можно и сейчас.",
)


class Origin(StrEnum):
    AGENT = "agent"
    BASELINE = "baseline"
    CONTROL = "control"

    @property
    def label(self) -> str:
        return {"agent": "агент", "baseline": "базовая линия", "control": "контрольная"}[self.value]


class ControlKind(StrEnum):
    MISPLACED_QUOTE = "misplaced_quote"
    SWAPPED_CLAIM = "swapped_claim"
    NITPICK = "nitpick"

    @property
    def label(self) -> str:
        return {
            "misplaced_quote": "цитата из другого места",
            "swapped_claim": "чужое свойство и объяснение",
            "nitpick": "придирка без влияния на тест",
        }[self.value]


class LabelItem(BaseModel):
    """Находка в пуле разметки. Разметчику уходит только ``shown``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    uid: str = Field(description="agent:<прогон>#F-07, baseline:<прогон>#12, control:<пачка>.<k>")
    origin: Origin
    run: str | None = None
    ref: str = Field(description="F-NN у агента, номер в списке судьи у базовой линии")
    quote: str
    place: str
    property: str
    explanation: str
    severity: str | None = None
    finder: str | None = Field(default=None, description="Кто нашёл: проходы, линтер, чек-лист")
    passes: str | None = Field(default=None, description="«в K из N проходов» у находок проходов")
    blocks: list[str] = Field(
        default_factory=list, description="ID блоков цитаты: чтобы контрольные брать не оттуда"
    )
    control: ControlKind | None = None
    donors: list[str] = Field(default_factory=list, description="uid настоящих находок, из которых собрана")

    def shown(self, number: int) -> dict[str, object]:
        """Что видит разметчик: ни источника, ни F-NN, ни признака контрольной находки."""
        return {
            "id": number,
            "quote": self.quote,
            "place": self.place,
            "property": self.property,
            "explanation": self.explanation,
        }


class Batch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=1)
    uids: list[str] = Field(description="uid по порядку: номер находки в пачке это позиция с 1")


def content_words(text: str) -> set[str]:
    return set(_CONTENT_WORD.findall(text.lower().replace("ё", "е")))


def overlap(a: str, b: str) -> float:
    """Доля общих слов от меньшего множества: 0 разные тексты, 1 один содержит другой."""
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


def _block_section(doc: ParsedDocument, block_ids: Sequence[str]) -> list[str]:
    by_id = {b.id: b for b in doc.blocks}
    return next((by_id[b].section for b in block_ids if b in by_id), [])


def _quote_candidates(quote: str) -> list[str]:
    """Что искать в документе: сегменты склейки « … » и текст в «ёлочках» без многоточия по краям."""
    parts = [*quote.split(QUOTE_JOIN), *_GUILLEMETS.findall(quote)]
    pieces = [seg for p in parts for seg in _PIECE_SPLIT.split(p) if len(seg.strip()) >= MIN_PIECE_CHARS]
    return [c for p in [*parts, *pieces] if (c := p.strip().strip("…").strip())]


def _place_of(doc: ParsedDocument, quote: str) -> tuple[str, list[str]]:
    """Место цитаты в документе разметки по сверке кодом: первый сегмент, который нашёлся."""
    for candidate in _quote_candidates(quote):
        found = locate_quote(doc, candidate)
        if found.verified and found.anchor is not None:
            anchor = found.anchor
            return where(anchor, _block_section(doc, anchor.block_ids)), list(anchor.block_ids)
    return "место не определено", []


def _finder(finding: Finding) -> str:
    kinds = {s.kind for s in finding.sources}
    names = [
        n
        for k, n in (
            (SourceKind.PASS, "проходы"),
            (SourceKind.LINT, "линтер"),
            (SourceKind.CHECKLIST, "чек-лист"),
        )
        if k in kinds
    ]
    return " + ".join(names) or "не указан"


def agent_items(run: RunData, findings: Sequence[Finding], doc: ParsedDocument) -> list[LabelItem]:
    """Находки агента: текст как у судьи (``JudgeItem``), место и разрезы из ``report.json``."""
    out = []
    for item, finding in zip(run.items, findings, strict=True):
        first = finding.quotes[0] if finding.quotes else None
        if first is not None and first.anchor is not None:
            section = finding.section_path or _block_section(doc, first.anchor.block_ids)
            place, blocks = where(first.anchor, section), list(first.anchor.block_ids)
        else:
            place, blocks = _place_of(doc, item.quote)
        passes = (
            f"{finding.found_in} из {finding.of}"
            if any(s.kind is SourceKind.PASS for s in finding.sources)
            else None
        )
        out.append(
            _from_judge_item(item, Origin.AGENT, run.label, finding.id, place, blocks).model_copy(
                update={"severity": finding.severity.label, "finder": _finder(finding), "passes": passes}
            )
        )
    return out


def baseline_items(run: RunData, doc: ParsedDocument) -> list[LabelItem]:
    out = []
    for item in run.items:
        place, blocks = _place_of(doc, item.quote)
        out.append(_from_judge_item(item, Origin.BASELINE, run.label, str(item.id), place, blocks))
    return out


def _from_judge_item(
    item: JudgeItem, origin: Origin, run: str, ref: str, place: str, blocks: list[str]
) -> LabelItem:
    return LabelItem(
        uid=f"{origin.value}:{run}#{ref}",
        origin=origin,
        run=run,
        ref=ref,
        quote=item.quote,
        place=place,
        property=item.property,
        explanation=item.explanation,
        blocks=blocks,
    )


def items_for_run(run: RunData, findings: Sequence[Finding] | None, doc: ParsedDocument) -> list[LabelItem]:
    if run.source is Source.AGENT:
        return agent_items(run, findings or [], doc)
    return baseline_items(run, doc)


# ---------------------------------------------------------------- контрольные находки


def _clean_fragment(doc: ParsedDocument, text: str) -> str | None:
    """Фрагмент без разметки Markdown по краям, если он дословно есть в документе и подходит по длине."""
    cleaned = text.replace("**", "").strip().strip("`*_|").strip()
    if not MIN_CONTROL_CHARS <= len(cleaned) <= MAX_CONTROL_CHARS:
        return None
    return cleaned if locate_quote(doc, cleaned).match is QuoteMatch.EXACT else None


def _fragment(doc: ParsedDocument, block: Block) -> str | None:
    """Дословный фрагмент блока для цитаты: ячейка таблицы, предложение или начало блока."""
    if block.cells:
        candidates = [c.text for c in block.cells]
    else:
        candidates = [s.text for s in block.sentences] or _SENTENCE_END.split(block.text)
    for candidate in candidates:
        fragment = _clean_fragment(doc, candidate)
        if fragment:
            return fragment
    if block.cells or len(block.text) <= MAX_CONTROL_CHARS:
        return None
    cut = block.text[:MAX_CONTROL_CHARS]
    return _clean_fragment(doc, cut[: cut.rfind(" ")])


def _control(
    kind: ControlKind, uid: str, quote: str, place: str, prop: str, expl: str, donors: list[str]
) -> LabelItem:
    return LabelItem(
        uid=uid,
        origin=Origin.CONTROL,
        ref=uid.split(":", 1)[1],
        quote=quote,
        place=place,
        property=prop,
        explanation=expl,
        control=kind,
        donors=donors,
    )


class ControlMaker:
    """Собирает контрольные находки из пула и документа. Блок и донор берутся не больше одного раза."""

    def __init__(self, pool: Sequence[LabelItem], doc: ParsedDocument) -> None:
        self.pool = list(pool)
        self.fragments = [
            (b, f) for b in doc.blocks if b.kind in CONTROL_BLOCK_KINDS and (f := _fragment(doc, b))
        ]
        self.used_blocks: set[str] = set()
        self.used_donors: set[str] = set()

    def _free(self) -> list[tuple[Block, str]]:
        return [(b, f) for b, f in self.fragments if b.id not in self.used_blocks]

    def _donors(self, rng: random.Random) -> list[LabelItem]:
        free = [i for i in self.pool if i.uid not in self.used_donors]
        return rng.sample(free, len(free))

    def _take(self, block: Block) -> str:
        self.used_blocks.add(block.id)
        return where(block.anchor, block.section)

    def misplaced(self, rng: random.Random, uid: str) -> LabelItem | None:
        for donor in self._donors(rng):
            claim = f"{donor.quote} {donor.explanation}"
            away = [
                (b, f)
                for b, f in self._free()
                if b.id not in donor.blocks and overlap(f, claim) <= MAX_OVERLAP
            ]
            if away:
                block, fragment = rng.choice(away)
                self.used_donors.add(donor.uid)
                place = self._take(block)
                kind = ControlKind.MISPLACED_QUOTE
                return _control(kind, uid, fragment, place, donor.property, donor.explanation, [donor.uid])
        return None

    def swapped(self, rng: random.Random, uid: str) -> LabelItem | None:
        donors = self._donors(rng)
        for a in (d for d in donors if len(d.quote) >= MIN_SWAP_QUOTE and content_words(d.quote)):
            for b in donors:
                far = b is not a and not set(a.blocks) & set(b.blocks) and a.place != b.place
                if (
                    far
                    and a.property != b.property
                    and overlap(a.quote, f"{b.quote} {b.explanation}") <= MAX_OVERLAP
                ):
                    self.used_donors.update({a.uid, b.uid})
                    kind = ControlKind.SWAPPED_CLAIM
                    return _control(kind, uid, a.quote, a.place, b.property, b.explanation, [a.uid, b.uid])
        return None

    def nitpick(self, rng: random.Random, uid: str) -> LabelItem | None:
        free = self._free() or self.fragments
        if not free:
            return None
        block, fragment = rng.choice(free)
        frag = " ".join(fragment.split()[:NITPICK_WORDS])
        text = rng.choice(NITPICK_TEMPLATES).format(frag=frag)
        return _control(ControlKind.NITPICK, uid, fragment, self._take(block), NITPICK_PROPERTY, text, [])

    def make(self, rng: random.Random, batch: int, n: int) -> list[LabelItem]:
        """``n`` контрольных находок пачки, виды по кругу со случайного."""
        kinds = list(ControlKind)
        start = rng.randrange(len(kinds))
        out: list[LabelItem] = []
        for k in range(n):
            kind = kinds[(start + k) % len(kinds)]
            uid = f"control:{batch}.{k + 1}"
            if kind is ControlKind.MISPLACED_QUOTE:
                item = self.misplaced(rng, uid)
            elif kind is ControlKind.SWAPPED_CLAIM:
                item = self.swapped(rng, uid)
            else:
                item = self.nitpick(rng, uid)
            item = item or self.nitpick(rng, uid)
            if item is not None:
                out.append(item)
        return out


def control_count(real: int, share: float = CONTROL_SHARE) -> int:
    """Сколько контрольных подмешать к ``real`` настоящим, чтобы их доля в пачке была около ``share``."""
    if real <= 0 or share <= 0:
        return 0
    return max(1, round(share * real / (1 - share)))


def make_batches(
    pool: Sequence[LabelItem],
    doc: ParsedDocument,
    *,
    seed: int = DEFAULT_SEED,
    batch_size: int = DEFAULT_BATCH,
    control_share: float = CONTROL_SHARE,
) -> tuple[list[Batch], list[LabelItem]]:
    """Пачки: пул перемешан по ``seed`` и порезан поровну, в каждую подмешаны контрольные находки.

    Агент и базовая линия идут вперемешку, поэтому по пачке не понять, чей это список. Возвращает
    пачки и контрольные находки (настоящие уже в ``pool``).
    """
    if not pool:
        return [], []
    order = list(pool)
    random.Random(f"{seed}:pool").shuffle(order)  # noqa: S311 - выборка для eval, не криптография
    n_batches = -(-len(order) // max(1, batch_size))
    size = -(-len(order) // n_batches)
    batches: list[Batch] = []
    controls: list[LabelItem] = []
    maker = ControlMaker(pool, doc)
    for index in range(1, n_batches + 1):
        real = order[(index - 1) * size : index * size]
        rng = random.Random(f"{seed}:batch:{index}")  # noqa: S311 - то же
        extra = maker.make(rng, index, control_count(len(real), control_share))
        members = real + extra
        rng.shuffle(members)
        batches.append(Batch(index=index, uids=[m.uid for m in members]))
        controls.extend(extra)
    return batches, controls
