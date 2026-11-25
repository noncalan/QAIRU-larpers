"""Шаг 7: верификатор без модели. Принцип «модель предлагает, код проверяет».

Контракт: ``verify(found, doc, seg) -> Verification``. Модель здесь не вызывается. Что делает код:

1. **Цитаты.** Каждую цитату находки сверяет ``text.locate_quote``: сначала в блоках, которые указала
   модель, затем во всём документе. Статусы: точно, нечётко (rapidfuzz не ниже ``FUZZY_THRESHOLD``,
   балл сохраняется), не найдено. В отчёт идёт фрагмент из документа, а не текст модели. Ненайденная
   цитата в находку не попадает, но считается в сводке и отмечается в находке.
2. **Отброшено.** Находка без единой сверенной цитаты уходит в «отброшено» при любом уровне
   (CLAUDE.md, правило 4). Находку, которую снял критик (шаг 6), тоже отбрасываем, с его причиной.
3. **Якорь.** Если цитата нашлась вне блоков, которые указала модель, место берём из документа,
   а находка получает пометку ``anchor_corrected``.
4. **Привязка к R-NN** по пересечению якорей цитат и требований. Если цитата лежит в атоме R-07.2,
   в находку пишется атом, а не родитель R-07; в статусе R-07 находка всё равно видна. Находка,
   цитаты которой не лежат ни в одном требовании (пропуск на уровне набора цитирует заголовок
   раздела или фразу, из которой следует ожидание), привязывается к разделу этой цитаты.
5. **Важность** считает ``validator.severity``: таблица «тип дефекта × мешает ли написать тест-кейс»,
   ограничения кода и решение критика. Подсказка модели сохраняется, но не решает; расхождение
   больше чем на уровень помечается (``Finding.severity_disputed``).
6. **Порядок и номера.** Сортировка по важности и месту в документе, затем ``F-01``, ``F-02``…
7. **Сводка, светофор, статус требований.** Правило светофора в ``TRAFFIC_RULE`` и ``traffic_light``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from typing import Final, TypeVar

from validator.models import (
    Anchor,
    CriticAction,
    DefectType,
    DiscardedFinding,
    DiscardReason,
    Finding,
    MergedFinding,
    ParsedDocument,
    Property,
    Quote,
    QuoteMatch,
    Scope,
    Segmentation,
    Severity,
    Summary,
    TrafficLight,
    Verdict,
    Verification,
)
from validator.severity import severity_for
from validator.text import locate_quote

# ---------------------------------------------------------------- светофор

TRAFFIC_RULE: Final = (
    "красный, если есть критичное нарушение, которое мешает написать тест-кейс; жёлтый, если критичных "
    "нет, но есть важные нарушения или находки «неясно»; зелёный, если остались только незначительные "
    "нарушения или их нет. Цвет считает код по находкам со сверенными цитатами, модель его не выбирает."
)
"""Правило светофора (``traffic_light``) словами: отчёт показывает его рядом с итогом."""

REASON_IDS_SHOWN: Final = 5
"""Сколько ``F-NN`` перечислить в причине светофора, остальные «и ещё N»."""


# ---------------------------------------------------------------- цитаты и якоря


def _blocks_of(ids: Iterable[str]) -> set[str]:
    """``B012.2`` → ``B012``: исправлен ли якорь, решаем по блокам, а не по предложениям."""
    return {i.split(".", 1)[0] for i in ids}


def locate(doc: ParsedDocument, quote: str, block_ids: Sequence[str]) -> tuple[Quote, bool]:
    """Сверка цитаты: сначала в блоках модели, затем во всём документе.

    Второе значение ``True``, если цитата нашлась только вне блоков, которые указала модель
    (якорь исправил код). Если модель блоков не указала, исправлять нечего.
    """
    if block_ids:
        found = locate_quote(doc, quote, within=block_ids)
        if found.verified:
            return found, False
    found = locate_quote(doc, quote)
    if not (block_ids and found.verified and found.anchor):
        return found, False
    return found, not (_blocks_of(found.anchor.block_ids) & _blocks_of(block_ids))


def _overlaps(a: Anchor, b: Anchor) -> bool:
    return a.char_start < b.char_end and b.char_start < a.char_end


def requirement_ids(quotes: Sequence[Quote], seg: Segmentation) -> list[str]:
    """Требования, с которыми пересекаются якоря цитат, по порядку нарезки.

    Если пересеклись и атом, и его родитель, остаётся только атом: он точнее.
    """
    hit = [
        r
        for r in seg.requirements
        if any(q.anchor is not None and _overlaps(q.anchor, r.anchor) for q in quotes)
    ]
    parents = {r.parent_id for r in hit if r.parent_id}
    return [r.id for r in hit if r.id not in parents]


def _section_path(doc: ParsedDocument, quotes: Sequence[Quote]) -> list[str]:
    for q in quotes:
        if q.anchor and q.anchor.block_ids:
            block = doc.block(q.anchor.block_ids[0])
            if block is not None:
                return list(block.section)
    return []


# ---------------------------------------------------------------- одна находка


def _discard(m: MergedFinding, quotes: Sequence[Quote], doc: ParsedDocument) -> DiscardedFinding:
    """Находка без единой сверенной цитаты: причина и подробность для метрик прогона."""
    if not m.quotes:
        detail = "у находки нет цитаты"
    else:
        best = max((q.score for q in quotes), default=0.0)
        detail = f"не найдена ни одна цитата из {len(m.quotes)} (лучший балл сверки {best:.0f} из 100)"
    unknown = sorted({b for b in m.block_ids if doc.block(b) is None})
    if unknown:
        detail += f"; блоков {', '.join(unknown)} в документе нет"
    # Все блоки модели выдуманы: это отдельный вид ошибки модели, его видно в метриках прогона.
    all_unknown = bool(m.block_ids) and len(unknown) == len(set(m.block_ids))
    reason = DiscardReason.UNKNOWN_BLOCKS if all_unknown else DiscardReason.QUOTE_NOT_FOUND
    return DiscardedFinding(finding=m, reason=reason, detail=detail)


def _quote_notes(
    m: MergedFinding,
    verified: Sequence[Quote],
    linked: Sequence[str],
    section: Sequence[str],
    seg: Segmentation,
) -> list[str]:
    notes: list[str] = []
    missing = len(m.quotes) - len(verified)
    if missing:
        notes.append(
            f"Цитат модели не найдено в документе: {missing} из {len(m.quotes)}. В отчёт они не вошли, "
            "находка держится на сверенных цитатах."
        )
    # Если нарезка пуста (шаг 2 упал), об этом говорит его диагностика, а не каждая находка.
    if m.scope is Scope.REQUIREMENT and not linked and seg.requirements:
        where = f"разделу «{' › '.join(section[-2:])}»" if section else "документу в целом"
        notes.append(f"Цитата не входит ни в одно требование из таблицы нарезки: находка отнесена к {where}.")
    return notes


def check(
    m: MergedFinding, doc: ParsedDocument, seg: Segmentation
) -> tuple[Finding | DiscardedFinding, list[Quote]]:
    """Одна находка после сверки: ``Finding`` (номер ``F-00`` до сортировки) или отказ с причиной.

    Второе значение: все сверенные цитаты находки, включая ненайденные, для сводки.
    """
    located = [locate(doc, q, m.block_ids) for q in m.quotes]
    quotes = [q for q, _ in located]
    verified = [q for q in quotes if q.verified]
    if not verified:
        return _discard(m, quotes, doc), quotes
    if m.critic is not None and m.critic.action is CriticAction.DROP:
        return DiscardedFinding(
            finding=m, reason=DiscardReason.CRITIC_REJECTED, detail=m.critic.reason
        ), quotes
    linked = requirement_ids(verified, seg)
    section = _section_path(doc, verified)
    severity = severity_for(m, verified)
    finding = Finding(
        id="F-00",  # номер ставит verify после сортировки
        merged_id=m.mid,
        property=m.property,
        defect_type=m.defect_type,
        verdict=m.verdict,
        scope=m.scope,
        severity=severity.severity,
        severity_hint=m.severity_hint,
        severity_basis=severity.basis,
        severity_disputed=severity.disputed,
        blocks_testing=m.blocks_testing,
        quotes=verified,
        requirement_ids=linked,
        section_path=section,
        explanation=m.explanation,
        suggestion=m.suggestion,
        title=m.title,
        scenario=m.scenario,
        test_gap=m.test_gap,
        found_in=m.found_in,
        of=m.of,
        sources=list(m.sources),
        witness=m.witness,
        test_draft=m.test_draft,
        critic=m.critic,
        anchor_corrected=any(fixed for _, fixed in located),
        notes=_quote_notes(m, verified, linked, section, seg) + severity.notes,
    )
    return finding, quotes


# ---------------------------------------------------------------- порядок, статус, сводка


def _position(f: Finding) -> int:
    return min((q.anchor.char_start for q in f.quotes if q.anchor), default=1 << 30)


def number(findings: Iterable[Finding]) -> list[Finding]:
    """Сортировка (важность, место в документе) и номера ``F-01``, ``F-02``…"""
    ordered = sorted(findings, key=lambda f: (f.severity.rank, _position(f), f.merged_id))
    return [f.model_copy(update={"id": f"F-{n:02d}"}) for n, f in enumerate(ordered, start=1)]


def requirement_status(findings: Sequence[Finding], seg: Segmentation) -> dict[str, list[str]]:
    """``R-NN`` → его ``F-NN`` по порядку; пустой список значит «нарушений нет».

    Находка атома R-07.2 видна и в статусе родителя R-07: у родителя есть нарушение.
    """
    parent = {r.id: r.parent_id for r in seg.requirements}

    def with_ancestors(rid: str) -> set[str]:
        seen: set[str] = set()
        current: str | None = rid
        while current and current not in seen:
            seen.add(current)
            current = parent.get(current)
        return seen

    status: dict[str, list[str]] = {r.id: [] for r in seg.requirements}
    for f in findings:
        for rid in set().union(*(with_ancestors(r) for r in f.requirement_ids)):
            if rid in status:
                status[rid].append(f.id)
    return status


_E = TypeVar("_E", bound=Enum)


def _count(values: Iterable[_E], order: Iterable[_E]) -> dict[_E, int]:
    """Счётчик в порядке перечисления, без нулей: стабильный порядок в отчёте и ``report.json``."""
    counts = Counter(values)
    return {key: counts[key] for key in order if counts[key]}


def summarize(
    findings: Sequence[Finding],
    discarded: Sequence[DiscardedFinding],
    seg: Segmentation,
    quotes: Sequence[Quote] | None = None,
    status: Mapping[str, Sequence[str]] | None = None,
) -> Summary:
    """Сводка (ТЗ п. 6). Все числа считаются из тех же списков, что видит отчёт.

    Требования считаются по строкам статуса (``requirement_status``): если нарезка дала и единицу R-07,
    и её атомы, считаются все строки, как в таблице отчёта.

    ``quotes``: все сверенные цитаты, включая цитаты отброшенных находок (доля сверенных); по умолчанию
    цитаты находок. ``status``: статус требований; по умолчанию считается из находок.
    """
    quotes = list(quotes) if quotes is not None else [q for f in findings for q in f.quotes]
    status = status if status is not None else requirement_status(findings, seg)
    critical = [f.id for f in findings if f.severity is Severity.CRITICAL and f.verdict is Verdict.VIOLATION]
    return Summary(
        requirements_total=len(status),
        requirements_with_findings=sum(1 for ids in status.values() if ids),
        findings_total=len(findings),
        violations=sum(f.verdict is Verdict.VIOLATION for f in findings),
        unclear=sum(f.verdict is Verdict.UNCLEAR for f in findings),
        critical=len(critical),
        critical_ids=critical,
        by_severity=_count((f.severity for f in findings), Severity),
        by_property=_count((f.property for f in findings), Property),
        by_defect_type=_count((f.defect_type for f in findings), DefectType),
        set_level=sum(f.scope is Scope.SET for f in findings),
        discarded_total=len(discarded),
        discarded_by_reason=_count((d.reason for d in discarded), DiscardReason),
        quotes_total=len(quotes),
        quotes_exact=sum(q.match is QuoteMatch.EXACT for q in quotes),
        quotes_fuzzy=sum(q.match is QuoteMatch.FUZZY for q in quotes),
        quotes_not_found=sum(not q.verified for q in quotes),
        anchors_corrected=sum(f.anchor_corrected for f in findings),
    )


# ---------------------------------------------------------------- светофор


def _plural(n: int, one: str, few: str, many: str) -> str:
    """«1 нарушение», «3 нарушения», «5 нарушений»."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _ids(findings: Sequence[Finding]) -> str:
    shown = ", ".join(f.id for f in findings[:REASON_IDS_SHOWN])
    rest = len(findings) - REASON_IDS_SHOWN
    return f"{shown} и ещё {rest}" if rest > 0 else shown


def _violations(n: int, adjective: tuple[str, str, str]) -> str:
    word = _plural(n, "нарушение", "нарушения", "нарушений")
    return f"{n} {_plural(n, *adjective)} {word}"


def _join(parts: Sequence[str]) -> str:
    """«a», «a и b», «a, b и c»."""
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} и {parts[-1]}"


def traffic_light(findings: Sequence[Finding]) -> tuple[TrafficLight, str]:
    """Светофор «можно ли начинать разработку и тестирование» и причина одной фразой (``TRAFFIC_RULE``).

    - Красный: есть нарушение с важностью «критично», которое мешает написать тест-кейс.
    - Жёлтый: таких нет, но есть нарушения «важно» (или «критично», не мешающие тесту) или «неясно».
    - Зелёный: только «незначительно» или находок нет.
    """
    violations = [f for f in findings if f.verdict is Verdict.VIOLATION]
    critical = [f for f in violations if f.severity is Severity.CRITICAL]
    blocking = [f for f in critical if f.blocks_testing]
    if blocking:
        n = len(blocking)
        head = _violations(n, ("критичное", "критичных", "критичных"))
        return (
            TrafficLight.RED,
            f"{head} {_plural(n, 'мешает', 'мешают', 'мешают')} написать тест-кейсы ({_ids(blocking)})",
        )
    major = [f for f in violations if f.severity is Severity.MAJOR]
    unclear = [f for f in findings if f.verdict is Verdict.UNCLEAR]
    parts = []
    if critical:
        parts.append(
            _violations(len(critical), ("критичное", "критичных", "критичных"))
            + f" без влияния на тест-кейсы ({_ids(critical)})"
        )
    if major:
        parts.append(_violations(len(major), ("важное", "важных", "важных")) + f" ({_ids(major)})")
    if unclear:
        n = len(unclear)
        parts.append(f"{n} {_plural(n, 'находка', 'находки', 'находок')} «неясно» ({_ids(unclear)})")
    if parts:
        head = "нарушений, которые мешают написать тест-кейсы, нет" if critical else "критичных нарушений нет"
        return TrafficLight.YELLOW, f"{head}, но есть {_join(parts)}"
    if violations:
        minor = _violations(len(violations), ("незначительное", "незначительных", "незначительных"))
        return TrafficLight.GREEN, f"критичных и важных нарушений нет, есть {minor}"
    return TrafficLight.GREEN, "нарушений не найдено"


# ---------------------------------------------------------------- шаг 7 целиком


def verify(found: Sequence[MergedFinding], doc: ParsedDocument, seg: Segmentation) -> Verification:
    """Шаг 7: находки ``F-NN`` со сверенными цитатами, отброшенные с причиной, сводка и светофор."""
    results = [check(m, doc, seg) for m in found]
    kept = [c for c, _ in results if isinstance(c, Finding)]
    discarded = [c for c, _ in results if isinstance(c, DiscardedFinding)]
    findings = number(kept)
    status = requirement_status(findings, seg)
    summary = summarize(findings, discarded, seg, [q for _, quotes in results for q in quotes], status)
    light, reason = traffic_light(findings)
    return Verification(
        findings=findings,
        discarded=discarded,
        summary=summary,
        traffic_light=light,
        traffic_reason=reason,
        traffic_rule=TRAFFIC_RULE,
        requirement_status=status,
    )
