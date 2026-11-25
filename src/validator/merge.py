"""Шаг 5: слияние проходов поиска и линтера, «найдено в N из K».

Контракт: ``merge(passes, lint_hits, doc) -> list[MergedFinding]``. Модель не вызывается.

Несколько проходов находят один дефект разными словами. Два кандидата считаем одной находкой, если
сразу выполнены три условия:

1. **Место:** у них есть общий блок (из ``block_ids`` или из якорей найденных цитат). Если у кого-то
   блоков нет или цитата вне указанных блоков встречается в документе несколько раз, решают цитаты.
2. **Цитаты** лежат друг на друге по смещениям в документе после ``locate_quote`` (цитату, которой нет
   в документе, сравниваем по нормализованному тексту через rapidfuzz; кандидатов без цитат по сути
   и объяснению). Доля не меньше ``MIN_QUOTE_OVERLAP``. При том же свойстве или типе дефекта хватает
   вложенности (фраза внутри полной формулировки); при свойствах из одной группы нужно взаимное
   перекрытие, потому что на одной фразе бывают два разных дефекта. Линтеру хватает вложенности.
3. **Свойство:** то же свойство стандарта, тот же тип дефекта или свойство из одной группы
   (``PROPERTY_GROUPS``: проходы путают однозначность и проверяемость одной расплывчатой фразы).

Кластеры собираем жадно по рёбрам «похожих» пар, от самой похожей к наименее похожей. Цепочки через
третьего запрещены для двух находок одного прохода и для двух находок с несколькими цитатами
(противоречия «A и B» и «A и C»): такие пары попадают в кластер, только если похожи сами. Порядок
рёбер и номера ``M-NNN`` задаёт содержимое (место в документе, свойство, цитаты), а не порядок
проходов, поэтому результат не зависит от того, в каком порядке пришли проходы.

Свойство, тип и вердикт решает большинство проходов (линтер голосует, только если кластер целиком
его). Тексты (суть, объяснение, предложение) берём у одного прохода, совпавшего с итогом голосования,
с самой длинной сверенной цитатой. «Мешает тесту» ставим, если так считает хоть один источник.

Ничего не отбрасываем: находки «1 из 3» идут дальше, решают критик (шаг 6) и верификатор (шаг 7).
Разногласия проходов по свойству, типу, вердикту и «мешает тесту» сохраняются для критика.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from rapidfuzz import fuzz

from validator.models import (
    CandidateFinding,
    DefectType,
    FindingSource,
    MergedFinding,
    ParsedDocument,
    PassResult,
    Property,
    Severity,
    SourceKind,
)
from validator.text import locate_quote, normalize

MIN_QUOTE_OVERLAP = 0.6
"""Доля цитат кандидата, которая должна лечь на цитаты другого (по символам документа).

Выше 0,5 намеренно: противоречия «A и B» и «A и C» делят половину цитат, но это разные находки.
"""

TEXT_SIMILARITY = 85.0
"""Минимальный балл rapidfuzz (0–100) для цитат, которых нет в документе, и для находок без цитат."""

_FAR = 1 << 30
"""Позиция кандидата без найденной цитаты: такие идут после всех привязанных к месту."""

PROPERTY_GROUPS: tuple[frozenset[Property], ...] = (
    # Расплывчатая формулировка: проходы путают двусмысленность и непроверяемость одной фразы.
    frozenset(
        {
            Property.UNAMBIGUOUS,
            Property.VERIFIABLE,
            Property.SET_COMPREHENSIBLE,
            Property.SET_VALIDATABLE,
        }
    ),
    # Чего-то не хватает: в требовании или в наборе.
    frozenset({Property.COMPLETE, Property.SET_COMPLETE}),
    # Противоречие и неверное утверждение.
    frozenset({Property.SET_CONSISTENT, Property.CORRECT}),
    # Невыполнимо: одно требование или набор.
    frozenset({Property.FEASIBLE, Property.SET_FEASIBLE}),
    # Лишнее и дублирующее.
    frozenset({Property.NECESSARY, Property.SET_MODIFIABLE}),
    # Одно и то же свойство на уровне требования и набора.
    frozenset({Property.TRACEABLE, Property.SET_TRACEABLE}),
    frozenset({Property.RANKED, Property.SET_RANKED}),
    # Отдельные дефекты, которые на одной фразе не равны соседям: составное, не по шаблону, не к месту.
    frozenset({Property.SINGULAR}),
    frozenset({Property.CONFORMING}),
    frozenset({Property.APPROPRIATE}),
)
"""Свойства, которые на одной цитате означают один дефект. Каждое свойство ровно в одной группе."""

_GROUP_OF: dict[Property, int] = {p: i for i, group in enumerate(PROPERTY_GROUPS) for p in group}

Span = tuple[int, int]
T = TypeVar("T")


# ---------------------------------------------------------------- подготовка кандидатов


@dataclass(frozen=True)
class _Prepared:
    """Кандидат и то, что про него посчитано один раз: места цитат, блоки, ключ сортировки."""

    cand: CandidateFinding
    spans: tuple[Span | None, ...]  # место каждой цитаты в doc.text или None, если не найдена
    norm_quotes: tuple[str, ...]
    blocks: frozenset[str]
    position: int
    key: tuple[object, ...]

    @property
    def pass_index(self) -> int | None:
        src = self.cand.source
        return src.pass_index if src.kind is SourceKind.PASS else None

    @property
    def longest_located(self) -> int:
        return max((s[1] - s[0] for s in self.spans if s), default=0)


class _Locator:
    """Сверка цитат с документом с кешем: сначала в указанных блоках, затем во всём документе.

    Если в указанных блоках цитаты нет, а во всём документе она встречается несколько раз, место
    неизвестно: первое вхождение может оказаться чужим требованием и склеить разные находки.
    """

    def __init__(self, doc: ParsedDocument) -> None:
        self.doc = doc
        self._norm_doc: str | None = None
        self._cache: dict[tuple[str, tuple[str, ...]], tuple[Span | None, tuple[str, ...]]] = {}

    def __call__(self, quote: str, block_ids: Sequence[str]) -> tuple[Span | None, tuple[str, ...]]:
        key = (quote, tuple(block_ids))
        if key not in self._cache:
            self._cache[key] = self._locate(quote, block_ids)
        return self._cache[key]

    def _ambiguous(self, quote: str) -> bool:
        if self._norm_doc is None:
            self._norm_doc = normalize(self.doc.text)[0]
        norm = normalize(quote)[0]
        return bool(norm) and self._norm_doc.count(norm) > 1

    def _locate(self, quote: str, block_ids: Sequence[str]) -> tuple[Span | None, tuple[str, ...]]:
        found = locate_quote(self.doc, quote, within=block_ids) if block_ids else None
        if found is None or not found.verified:
            found = locate_quote(self.doc, quote)
            if self._ambiguous(quote):
                return None, ()
        if not found.verified or found.anchor is None:
            return None, ()
        a = found.anchor
        return (a.char_start, a.char_end), tuple(a.block_ids)


def _prepare(cand: CandidateFinding, locate: _Locator) -> _Prepared:
    located = [locate(q, cand.block_ids) for q in cand.quotes]
    spans = tuple(span for span, _ in located)
    norm_quotes = tuple(normalize(q)[0] for q in cand.quotes)
    blocks = frozenset(cand.block_ids) | {b for _, ids in located for b in ids}
    position = min((s[0] for s in spans if s), default=_FAR)
    src = cand.source
    key = (
        position,
        cand.property.value,
        cand.defect_type.value,
        norm_quotes,
        normalize(cand.title or "")[0],
        normalize(cand.explanation)[0],
        src.kind.value,
        src.pass_index or 0,
        src.rule_id or "",
        cand.cid,
    )
    return _Prepared(cand, spans, norm_quotes, blocks, position, key)


# ---------------------------------------------------------------- похожесть двух кандидатов


def _overlap(span: Span, others: Iterable[Span]) -> int:
    """Сколько символов ``span`` покрыто объединением ``others``."""
    start, end = span
    covered, cursor = 0, start
    for o_start, o_end in sorted(others):
        lo, hi = max(o_start, cursor), min(o_end, end)
        if hi > lo:
            covered += hi - lo
            cursor = hi
    return covered


def _quote_coverage(a: _Prepared, i: int, b: _Prepared) -> float:
    """Насколько i-я цитата ``a`` лежит на цитатах ``b``: 0–1."""
    span = a.spans[i]
    b_spans = [s for s in b.spans if s]
    if span is not None and b_spans:
        by_place = _overlap(span, b_spans) / max(1, span[1] - span[0])
        if by_place > 0 or all(b.spans):
            return by_place
    # Цитату не нашли (или у другого не нашли часть цитат): сравниваем нормализованный текст.
    by_text = max(
        (
            fuzz.partial_ratio(a.norm_quotes[i], q)
            for j, q in enumerate(b.norm_quotes)
            if not (span and b.spans[j])
        ),
        default=0.0,
    )
    return by_text / 100 if by_text >= TEXT_SIMILARITY else 0.0


def _directed(a: _Prepared, b: _Prepared) -> float:
    if not a.norm_quotes:
        return 0.0
    return sum(_quote_coverage(a, i, b) for i in range(len(a.norm_quotes))) / len(a.norm_quotes)


def _gist(p: _Prepared) -> str:
    return normalize(f"{p.cand.title or ''} {p.cand.explanation}")[0]


def _quote_score(a: _Prepared, b: _Prepared, *, containment: bool) -> float:
    """Похожесть цитат 0–1. Кандидаты без цитат сравниваются по сути и объяснению.

    ``containment``: достаточно, чтобы цитаты одного легли на цитаты другого (фраза внутри полной
    формулировки требования). Иначе нужно взаимное перекрытие: цитаты обоих лежат друг на друге.
    """
    if not a.norm_quotes and not b.norm_quotes:
        ratio = fuzz.token_set_ratio(_gist(a), _gist(b))
        return ratio / 100 if ratio >= TEXT_SIMILARITY else 0.0
    if not a.norm_quotes or not b.norm_quotes:
        return 0.0
    ab, ba = _directed(a, b), _directed(b, a)
    return max(ab, ba) if containment else min(ab, ba)


def _same_kind(x: CandidateFinding, y: CandidateFinding) -> bool:
    return x.property is y.property or x.defect_type is y.defect_type


def compatible(x: CandidateFinding, y: CandidateFinding) -> bool:
    """Свойства совместимы: то же свойство, тот же тип дефекта или одна группа ``PROPERTY_GROUPS``."""
    return _same_kind(x, y) or _GROUP_OF.get(x.property, -1) == _GROUP_OF.get(y.property, -2)


def _same_place(a: _Prepared, b: _Prepared) -> bool:
    """Есть общий блок. Если у кого-то место неизвестно (нет блоков), решают цитаты."""
    if a.blocks and b.blocks:
        return bool(a.blocks & b.blocks)
    return True


def _similarity(a: _Prepared, b: _Prepared) -> float:
    """0, если кандидаты разные; иначе похожесть цитат (не меньше ``MIN_QUOTE_OVERLAP``)."""
    if not compatible(a.cand, b.cand) or not _same_place(a, b):
        return 0.0
    # Фраза внутри полной формулировки считается той же находкой, только если совпало свойство или
    # тип; для свойств из одной группы нужно взаимное перекрытие: на одной фразе бывают два дефекта.
    # Линтер цитирует короткое слово и N не меняет, поэтому ему хватает вложенности.
    lint = a.pass_index is None or b.pass_index is None
    score = _quote_score(a, b, containment=lint or _same_kind(a.cand, b.cand))
    return score if score >= MIN_QUOTE_OVERLAP else 0.0


# ---------------------------------------------------------------- кластеризация


def _needs_direct_edge(a: _Prepared, b: _Prepared) -> bool:
    """Пары, которые нельзя склеить через третьего: находки одного прохода и две находки с несколькими
    цитатами (противоречия «A и B» и «A и C» не сливаются через находку только про A)."""
    same_pass = a.pass_index is not None and a.pass_index == b.pass_index
    return same_pass or (len(a.norm_quotes) > 1 and len(b.norm_quotes) > 1)


def _cluster(items: Sequence[_Prepared]) -> list[list[_Prepared]]:
    """Жадная кластеризация по рёбрам от самых похожих, без опасных цепочек (``_needs_direct_edge``)."""
    edges: dict[tuple[int, int], float] = {}
    for i, a in enumerate(items):
        for j in range(i + 1, len(items)):
            score = _similarity(a, items[j])
            if score > 0:
                edges[(i, j)] = score

    root = list(range(len(items)))
    members: dict[int, list[int]] = {i: [i] for i in range(len(items))}

    def find(i: int) -> int:
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i

    def mergeable(x: list[int], y: list[int]) -> bool:
        return all(
            (min(i, j), max(i, j)) in edges or not _needs_direct_edge(items[i], items[j])
            for i in x
            for j in y
        )

    for i, j in sorted(edges, key=lambda e: (-edges[e], items[e[0]].key, items[e[1]].key)):
        ri, rj = find(i), find(j)
        if ri == rj or not mergeable(members[ri], members[rj]):
            continue
        keep, drop = min(ri, rj), max(ri, rj)
        root[drop] = keep
        members[keep] = sorted(members[keep] + members.pop(drop))
    return [[items[i] for i in group] for group in members.values()]


# ---------------------------------------------------------------- сборка MergedFinding


def _vote(values: Sequence[T], prefer: T, order: Callable[[T], str] = str) -> T:
    """Самое частое значение; при равенстве ``prefer``, если он среди лидеров, иначе первое по ``order``."""
    counts = Counter(values)
    top = max(counts.values(), default=0)
    leaders = [v for v, c in counts.items() if c == top]
    if not leaders or prefer in leaders:
        return prefer
    return min(leaders, key=order)


def _fill(prefer: str | None, *pools: Iterable[str | None]) -> str | None:
    """``prefer``, если он есть; иначе самое длинное непустое значение первого пула, где оно нашлось."""
    if prefer:
        return prefer
    for pool in pools:
        present = [v for v in pool if v]
        if present:
            return max(present, key=lambda v: (len(v), v))
    return None


def _source_key(src: FindingSource) -> tuple[int, int, str]:
    return (
        0 if src.kind is SourceKind.PASS else 1,
        src.pass_index or 0,
        f"{src.kind.value}:{src.rule_id or ''}",
    )


def _quotes(cluster: Sequence[_Prepared]) -> list[str]:
    """Все разные цитаты кластера по месту в документе; вложенные в другую цитату и повторы убираем."""
    seen: dict[str, tuple[str, Span | None]] = {}
    for p in cluster:
        for quote, norm, span in zip(p.cand.quotes, p.norm_quotes, p.spans, strict=True):
            if norm and norm not in seen:
                seen[norm] = (quote, span)

    def length(item: tuple[str, tuple[str, Span | None]]) -> int:
        span = item[1][1]
        return span[1] - span[0] if span else 0

    # Длинные сначала: короткая цитата внутри уже взятой длинной не нужна.
    ranked = sorted(seen.items(), key=lambda item: (-length(item), item[0]))
    kept: list[tuple[str, str, Span | None]] = []
    for norm, (quote, span) in ranked:
        if span is not None and any(s and s[0] <= span[0] and span[1] <= s[1] for _, _, s in kept):
            continue
        if span is None and any(fuzz.partial_ratio(norm, k) >= TEXT_SIMILARITY for k, _, _ in kept):
            continue
        kept.append((norm, quote, span))
    kept.sort(key=lambda t: (t[2][0] if t[2] else _FAR, t[0]))
    return [quote for _, quote, _ in kept]


def _disagreements(cluster: Sequence[_Prepared]) -> list[str]:
    """Разногласия источников: свойство, тип дефекта, вердикт, мешает ли тесту."""
    aspects: list[tuple[str, Callable[[CandidateFinding], str]]] = [
        ("свойство", lambda c: f"{c.property.value} ({c.property.label})"),
        ("тип дефекта", lambda c: c.defect_type.label),
        ("вердикт", lambda c: c.verdict.label),
        ("мешает тесту", lambda c: "да" if c.blocks_testing else "нет"),
    ]
    out: list[str] = []
    for name, value in aspects:
        groups: dict[str, list[str]] = {}
        for p in sorted(cluster, key=lambda p: _source_key(p.cand.source)):
            groups.setdefault(value(p.cand), []).append(p.cand.source.label)
        if len(groups) > 1:
            parts = [f"{v}: {', '.join(dict.fromkeys(labels))}" for v, labels in sorted(groups.items())]
            out.append(f"{name}: " + "; ".join(parts))
    return out


def _representative(voters: Sequence[_Prepared], prop: Property, defect: DefectType) -> _Prepared:
    """Голос, чьи тексты идут в находку: совпадает с итогом голосования по свойству и типу, затем
    самая длинная сверенная цитата и самое полное объяснение. Так суть, объяснение и предложение
    не спорят со свойством находки."""
    return min(
        voters,
        key=lambda p: (
            p.cand.property is not prop,
            p.cand.defect_type is not defect,
            -p.longest_located,
            -len(p.cand.explanation),
            p.key,
        ),
    )


def _severity_hint(cluster: Sequence[_Prepared], voters: Sequence[_Prepared]) -> Severity | None:
    """Большинство подсказок проходов (при равенстве важнее), без них подсказка линтера."""
    hints = [p.cand.severity_hint for p in voters if p.cand.severity_hint]
    hints = hints or [p.cand.severity_hint for p in cluster if p.cand.severity_hint]
    if not hints:
        return None
    return _vote(hints, min(hints, key=lambda s: s.rank), lambda s: str(s.rank))


def _build(cluster: Sequence[_Prepared], mid: str, of: int, block_order: dict[str, int]) -> MergedFinding:
    # Голосуют проходы; линтер голосует, только если кластер целиком из линтера.
    voters = [p for p in cluster if p.pass_index is not None] or list(cluster)
    first = min(voters, key=lambda p: (-p.longest_located, p.key)).cand
    prop = _vote([p.cand.property for p in voters], first.property)
    defect = _vote([p.cand.defect_type for p in voters], first.defect_type)
    head = _representative(voters, prop, defect).cand
    texts = [p.cand for p in voters], [p.cand for p in cluster]
    return MergedFinding(
        mid=mid,
        property=prop,
        defect_type=defect,
        verdict=_vote([p.cand.verdict for p in voters], head.verdict),
        scope=prop.scope,
        block_ids=sorted({b for p in cluster for b in p.blocks}, key=lambda b: (block_order.get(b, _FAR), b)),
        quotes=_quotes(cluster),
        explanation=head.explanation,
        suggestion=_fill(head.suggestion, *([c.suggestion for c in pool] for pool in texts)),
        title=_fill(head.title, *([c.title for c in pool] for pool in texts)),
        scenario=_fill(head.scenario, *([c.scenario for c in pool] for pool in texts)),
        test_gap=_fill(head.test_gap, *([c.test_gap for c in pool] for pool in texts)),
        # Мешает тесту хотя бы по одному источнику: безопасная сторона, разногласие видно критику.
        blocks_testing=any(p.cand.blocks_testing for p in cluster),
        severity_hint=_severity_hint(cluster, voters),
        sources=sorted((p.cand.source for p in cluster), key=_source_key),
        members=[p.cand.cid for p in sorted(cluster, key=lambda p: (_source_key(p.cand.source), p.key))],
        found_in=len({p.pass_index for p in cluster if p.pass_index is not None}),
        of=of,
        disagreements=_disagreements(cluster),
    )


def merge(
    passes: Sequence[PassResult], lint_hits: Sequence[CandidateFinding], doc: ParsedDocument
) -> list[MergedFinding]:
    """Сливает кандидатов успешных проходов и линтера в находки с «найдено в N из K».

    K: число успешных проходов. N: в скольких разных проходах есть находка кластера; линтер N не
    увеличивает, но остаётся в ``sources``. Упавшие проходы не учитываются ни в N, ни в K.
    """
    locate = _Locator(doc)
    of = len({p.index for p in passes if p.ok})
    candidates = [c for p in passes if p.ok for c in p.candidates] + list(lint_hits)
    items = sorted((_prepare(c, locate) for c in candidates), key=lambda p: p.key)
    clusters = _cluster(items)
    clusters.sort(key=lambda cl: min(p.key for p in cl))
    block_order = {b.id: i for i, b in enumerate(doc.blocks)}
    return [_build(cl, f"M-{n:03d}", of, block_order) for n, cl in enumerate(clusters, start=1)]


__all__ = ["MIN_QUOTE_OVERLAP", "PROPERTY_GROUPS", "TEXT_SIMILARITY", "compatible", "merge"]
