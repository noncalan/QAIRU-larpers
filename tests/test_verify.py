"""Верификатор (шаг 7): сверка цитат, якоря, привязка к R-NN, важность, сводка и светофор.

Документ в тестах из другого домена (библиотека), чтобы не подгонять правила под проверяемый документ.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import REQUIREMENTS
from validator import pipeline
from validator.config import load_config
from validator.models import (
    Anchor,
    Backend,
    CriticAction,
    CriticDecision,
    DefectType,
    DiscardReason,
    DocKind,
    ExitCode,
    Finding,
    MergedFinding,
    ParsedDocument,
    Property,
    QuoteMatch,
    Requirement,
    Scope,
    Segmentation,
    Severity,
    TrafficLight,
    Verdict,
)
from validator.segment import fallback_segmentation
from validator.severity import SEVERITY_TABLE, severity_disputed, table_severity
from validator.verify import TRAFFIC_RULE, traffic_light, verify

LIBRARY = """# Библиотека

## Выдача книг

Читатель может держать на руках не более пяти книг одновременно.

Библиотекарь оформляет выдачу в течение рабочего дня после заявки.

## Возврат книг

Возврат книги отмечается в карточке читателя в момент сдачи. Просрочка считается со следующего дня.

Максимум выдачи: три книги на читателя.
"""
# Блоки: B001 и B002 заголовки, B003 и B004 абзацы «Выдачи», B005 заголовок, B006 (два предложения) и B007.
LIMIT_FIVE = "Читатель может держать на руках не более пяти книг одновременно."
LIMIT_THREE = "Максимум выдачи: три книги на читателя."
SAME_DAY = "Библиотекарь оформляет выдачу в течение рабочего дня после заявки."
OVERDUE = "Просрочка считается со следующего дня."
INVENTED = "Библиотека выдаёт книги круглосуточно и без выходных."

MEANING = {
    DefectType.CONTRADICTION,
    DefectType.AMBIGUOUS,
    DefectType.INCOMPLETE,
    DefectType.UNVERIFIABLE,
    DefectType.INFEASIBLE,
    DefectType.INCORRECT,
}


@pytest.fixture
def doc(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(LIBRARY)


@pytest.fixture
def seg(doc: ParsedDocument) -> Segmentation:
    return fallback_segmentation(doc)


def merged(
    *quotes: str,
    mid: str = "M-001",
    block_ids: tuple[str, ...] = (),
    defect: DefectType = DefectType.AMBIGUOUS,
    prop: Property = Property.UNAMBIGUOUS,
    scope: Scope = Scope.REQUIREMENT,
    verdict: Verdict = Verdict.VIOLATION,
    blocks_testing: bool = False,
    hint: Severity | None = None,
    critic: CriticDecision | None = None,
) -> MergedFinding:
    return MergedFinding(
        mid=mid,
        property=prop,
        defect_type=defect,
        verdict=verdict,
        scope=scope,
        block_ids=list(block_ids),
        quotes=list(quotes),
        explanation="Пояснение находки.",
        blocks_testing=blocks_testing,
        severity_hint=hint,
        critic=critic,
    )


def only(findings: list[Finding]) -> Finding:
    assert len(findings) == 1
    return findings[0]


# ---------------------------------------------------------------- сверка цитат


def test_exact_quote_is_verified_from_document_text(doc: ParsedDocument, seg: Segmentation) -> None:
    ver = verify([merged(LIMIT_FIVE, block_ids=("B003",))], doc, seg)

    quote = only(ver.findings).quotes[0]
    assert (quote.match, quote.score) == (QuoteMatch.EXACT, 100.0)
    assert quote.located == LIMIT_FIVE
    assert quote.anchor is not None and quote.anchor.block_ids == ["B003"]


def test_fuzzy_quote_keeps_score_and_takes_text_from_document(doc: ParsedDocument, seg: Segmentation) -> None:
    paraphrase = "Читатель может держать на руках не более 5 книг одновременно"

    ver = verify([merged(paraphrase, block_ids=("B003",))], doc, seg)

    quote = only(ver.findings).quotes[0]
    assert quote.match is QuoteMatch.FUZZY
    assert 90 <= quote.score < 100
    assert quote.located is not None and quote.located in doc.text
    assert (ver.summary.quotes_exact, ver.summary.quotes_fuzzy) == (0, 1)


def test_invented_quote_discards_finding_and_is_counted(doc: ParsedDocument, seg: Segmentation) -> None:
    ver = verify([merged(INVENTED, block_ids=("B003",))], doc, seg)

    assert not ver.findings
    discarded = ver.discarded[0]
    assert discarded.reason is DiscardReason.QUOTE_NOT_FOUND
    assert "балл сверки" in discarded.detail
    assert ver.summary.discarded_by_reason == {DiscardReason.QUOTE_NOT_FOUND: 1}
    assert (ver.summary.quotes_total, ver.summary.quotes_not_found) == (1, 1)


def test_unknown_blocks_are_named_in_discard_reason(doc: ParsedDocument, seg: Segmentation) -> None:
    ver = verify([merged(INVENTED, block_ids=("B999",))], doc, seg)

    assert "B999" in ver.discarded[0].detail


def test_unmatched_quote_is_dropped_with_note(doc: ParsedDocument, seg: Segmentation) -> None:
    ver = verify([merged(LIMIT_FIVE, INVENTED, block_ids=("B003",))], doc, seg)

    finding = only(ver.findings)
    assert [q.located for q in finding.quotes] == [LIMIT_FIVE]
    assert any("1 из 2" in note for note in finding.notes)


# ---------------------------------------------------------------- якорь


def test_quote_outside_model_blocks_gets_anchor_from_document(doc: ParsedDocument, seg: Segmentation) -> None:
    ver = verify([merged(LIMIT_FIVE, block_ids=("B006",))], doc, seg)

    finding = only(ver.findings)
    assert finding.anchor_corrected
    assert finding.quotes[0].anchor is not None and finding.quotes[0].anchor.block_ids == ["B003"]
    assert finding.requirement_ids == ["R-01"]
    assert ver.summary.anchors_corrected == 1


def test_other_sentence_of_named_block_is_not_a_correction(doc: ParsedDocument, seg: Segmentation) -> None:
    ver = verify([merged(OVERDUE, block_ids=("B006.1",))], doc, seg)

    assert not only(ver.findings).anchor_corrected


def test_quote_without_model_blocks_is_not_a_correction(doc: ParsedDocument, seg: Segmentation) -> None:
    ver = verify([merged(LIMIT_FIVE)], doc, seg)

    assert not only(ver.findings).anchor_corrected


# ---------------------------------------------------------------- привязка к R-NN и уровень набора


def test_finding_is_linked_to_two_requirements(doc: ParsedDocument, seg: Segmentation) -> None:
    contradiction = merged(
        LIMIT_FIVE,
        LIMIT_THREE,
        block_ids=("B003", "B007"),
        defect=DefectType.CONTRADICTION,
        prop=Property.SET_CONSISTENT,
        scope=Scope.SET,
        blocks_testing=True,
    )

    ver = verify([contradiction], doc, seg)

    finding = only(ver.findings)
    assert finding.requirement_ids == ["R-01", "R-04"]
    assert ver.requirement_status["R-01"] == ver.requirement_status["R-04"] == [finding.id]
    assert ver.requirement_status["R-02"] == []


def test_atom_is_linked_and_parent_status_shows_finding(doc: ParsedDocument) -> None:
    block = doc.block("B006")
    assert block is not None and len(block.sentences) == 2
    first, second = block.sentences

    def req(rid: str, start: int, end: int, parent: str | None = None) -> Requirement:
        anchor = Anchor(char_start=start, char_end=end, block_ids=["B006"])
        return Requirement(id=rid, parent_id=parent, quote=doc.text[start:end], anchor=anchor, stable_key=rid)

    seg = Segmentation(
        requirements=[
            req("R-01", block.anchor.char_start, block.anchor.char_end),
            req("R-01.1", first.char_start, first.char_end, "R-01"),
            req("R-01.2", second.char_start, second.char_end, "R-01"),
        ]
    )

    ver = verify([merged(OVERDUE, block_ids=("B006.2",))], doc, seg)

    finding = only(ver.findings)
    assert finding.requirement_ids == ["R-01.2"]
    assert ver.requirement_status == {"R-01": ["F-01"], "R-01.1": [], "R-01.2": ["F-01"]}


def test_set_level_gap_quoting_a_heading_is_bound_to_section(doc: ParsedDocument, seg: Segmentation) -> None:
    gap = merged(
        "Возврат книг",
        block_ids=("B005",),
        defect=DefectType.INCOMPLETE,
        prop=Property.SET_COMPLETE,
        scope=Scope.SET,
    )

    ver = verify([gap], doc, seg)

    finding = only(ver.findings)
    assert finding.scope is Scope.SET
    assert finding.requirement_ids == []
    assert finding.section_path == ["Библиотека", "Возврат книг"]
    assert ver.summary.set_level == 1
    assert not any(ver.requirement_status.values())


def test_requirement_finding_outside_requirements_gets_note(doc: ParsedDocument, seg: Segmentation) -> None:
    ver = verify([merged("Выдача книг", block_ids=("B002",))], doc, seg)

    finding = only(ver.findings)
    assert finding.requirement_ids == []
    assert any("не входит ни в одно требование" in note for note in finding.notes)


# ---------------------------------------------------------------- важность


def test_severity_table_covers_every_defect_type() -> None:
    assert set(SEVERITY_TABLE) == set(DefectType)


@pytest.mark.parametrize("defect", list(DefectType), ids=lambda d: d.value)
@pytest.mark.parametrize("blocks_testing", [False, True], ids=["test_ok", "test_blocked"])
def test_severity_table(defect: DefectType, blocks_testing: bool) -> None:
    if defect is DefectType.INJECTED_INSTRUCTION:
        expected = Severity.MAJOR
    elif defect in MEANING:
        expected = Severity.CRITICAL if blocks_testing else Severity.MAJOR
    else:
        expected = Severity.MAJOR if blocks_testing else Severity.MINOR

    assert table_severity(defect, blocks_testing) is expected


def test_critical_needs_a_meaning_defect_that_blocks_testing() -> None:
    critical = {
        (d, b) for d in DefectType for b in (False, True) if table_severity(d, b) is Severity.CRITICAL
    }

    assert critical == {(d, True) for d in MEANING}
    assert {d for d in DefectType if d.critical_class} <= MEANING


def test_model_hint_is_kept_but_does_not_decide(doc: ParsedDocument, seg: Segmentation) -> None:
    finding = merged(
        LIMIT_FIVE, defect=DefectType.TRACEABILITY, prop=Property.TRACEABLE, hint=Severity.CRITICAL
    )

    result = only(verify([finding], doc, seg).findings)

    assert (result.severity, result.severity_hint) == (Severity.MINOR, Severity.CRITICAL)
    assert result.severity_disputed
    assert any("расходятся больше чем на уровень" in note for note in result.notes)
    assert "прослеживаемость" in result.severity_basis


def test_one_level_difference_is_not_disputed() -> None:
    assert not severity_disputed(Severity.MAJOR, Severity.MINOR)
    assert not severity_disputed(None, Severity.CRITICAL)
    assert severity_disputed(Severity.MINOR, Severity.CRITICAL)


def test_unclear_verdict_is_at_most_major(doc: ParsedDocument, seg: Segmentation) -> None:
    unclear = merged(LIMIT_FIVE, verdict=Verdict.UNCLEAR, blocks_testing=True)

    ver = verify([unclear], doc, seg)

    assert only(ver.findings).severity is Severity.MAJOR
    assert ver.summary.critical == 0


def test_contradiction_needs_two_verified_places_to_be_critical(
    doc: ParsedDocument, seg: Segmentation
) -> None:
    def contradiction(*quotes: str) -> MergedFinding:
        return merged(
            *quotes, defect=DefectType.CONTRADICTION, prop=Property.SET_CONSISTENT, blocks_testing=True
        )

    proven = only(verify([contradiction(LIMIT_FIVE, LIMIT_THREE)], doc, seg).findings)
    one_place = only(verify([contradiction(LIMIT_FIVE, INVENTED)], doc, seg).findings)
    same_place = only(verify([contradiction(LIMIT_FIVE, "не более пяти книг")], doc, seg).findings)

    assert proven.severity is Severity.CRITICAL
    assert one_place.severity is same_place.severity is Severity.MAJOR
    assert any("одно место противоречия" in note for note in one_place.notes)


def test_critic_drop_moves_finding_to_discarded(doc: ParsedDocument, seg: Segmentation) -> None:
    critic = CriticDecision(action=CriticAction.DROP, reason="Документ задаёт это значение ниже.")

    ver = verify([merged(LIMIT_FIVE, critic=critic)], doc, seg)

    assert not ver.findings
    assert ver.discarded[0].reason is DiscardReason.CRITIC_REJECTED
    assert ver.discarded[0].detail == critic.reason


def test_invented_quote_wins_over_critic_decision(doc: ParsedDocument, seg: Segmentation) -> None:
    critic = CriticDecision(action=CriticAction.DROP, reason="Снять.")

    ver = verify([merged(INVENTED, critic=critic)], doc, seg)

    assert ver.discarded[0].reason is DiscardReason.QUOTE_NOT_FOUND


@pytest.mark.parametrize(
    ("critic_severity", "expected"),
    [(None, Severity.MAJOR), (Severity.MINOR, Severity.MINOR), (Severity.CRITICAL, Severity.CRITICAL)],
)
def test_critic_downgrade_only_lowers(
    doc: ParsedDocument, seg: Segmentation, critic_severity: Severity | None, expected: Severity
) -> None:
    critic = CriticDecision(
        action=CriticAction.DOWNGRADE, reason="Тест пишется по соседнему пункту.", severity=critic_severity
    )

    result = only(verify([merged(LIMIT_FIVE, blocks_testing=True, critic=critic)], doc, seg).findings)

    assert result.severity is expected
    assert any("Критик понизил" in note for note in result.notes) is (expected is not Severity.CRITICAL)


def test_findings_are_sorted_by_severity_then_place(doc: ParsedDocument, seg: Segmentation) -> None:
    found = [
        merged(LIMIT_FIVE, mid="M-001", defect=DefectType.TRACEABILITY),  # незначительно, B003
        merged(OVERDUE, mid="M-002", blocks_testing=True),  # критично, B006
        merged(SAME_DAY, mid="M-003"),  # важно, B004
        merged(LIMIT_THREE, mid="M-004", blocks_testing=True),  # критично, B007
    ]

    ver = verify(found, doc, seg)

    assert [(f.id, f.merged_id) for f in ver.findings] == [
        ("F-01", "M-002"),
        ("F-02", "M-004"),
        ("F-03", "M-003"),
        ("F-04", "M-001"),
    ]


# ---------------------------------------------------------------- светофор и сводка


def _light(doc: ParsedDocument, seg: Segmentation, *found: MergedFinding) -> tuple[TrafficLight, str]:
    ver = verify(list(found), doc, seg)
    assert ver.traffic_rule == TRAFFIC_RULE
    return ver.traffic_light, ver.traffic_reason


def test_light_is_red_when_critical_violation_blocks_testing(doc: ParsedDocument, seg: Segmentation) -> None:
    light, reason = _light(doc, seg, merged(LIMIT_FIVE, blocks_testing=True), merged(SAME_DAY))

    assert light is TrafficLight.RED
    assert reason == "1 критичное нарушение мешает написать тест-кейсы (F-01)"


def test_light_is_yellow_for_major_violations(doc: ParsedDocument, seg: Segmentation) -> None:
    light, reason = _light(doc, seg, merged(LIMIT_FIVE), merged(SAME_DAY))

    assert light is TrafficLight.YELLOW
    assert "2 важных нарушения (F-01, F-02)" in reason


def test_light_is_yellow_for_unclear_only(doc: ParsedDocument, seg: Segmentation) -> None:
    unclear = merged(LIMIT_FIVE, verdict=Verdict.UNCLEAR, defect=DefectType.TERMINOLOGY)

    light, reason = _light(doc, seg, unclear)

    assert light is TrafficLight.YELLOW
    assert "1 находка «неясно» (F-01)" in reason


def test_light_is_green_for_minor_violations_or_none(doc: ParsedDocument, seg: Segmentation) -> None:
    minor = merged(LIMIT_FIVE, defect=DefectType.REDUNDANCY, prop=Property.SET_MODIFIABLE)

    assert _light(doc, seg, minor)[0] is TrafficLight.GREEN
    assert _light(doc, seg) == (TrafficLight.GREEN, "нарушений не найдено")


def test_critical_without_blocking_testing_is_not_red() -> None:
    finding = Finding(
        id="F-01",
        merged_id="M-001",
        property=Property.CORRECT,
        defect_type=DefectType.INCORRECT,
        verdict=Verdict.VIOLATION,
        scope=Scope.REQUIREMENT,
        severity=Severity.CRITICAL,
        blocks_testing=False,
        explanation="Пояснение.",
    )

    assert traffic_light([finding])[0] is TrafficLight.YELLOW


def test_reason_lists_at_most_five_ids(doc: ParsedDocument, seg: Segmentation) -> None:
    quotes = [
        LIMIT_FIVE,
        SAME_DAY,
        OVERDUE,
        LIMIT_THREE,
        "Возврат книги отмечается",
        "Выдача книг",
        "Библиотека",
    ]
    found = [merged(q, mid=f"M-{n:03d}", blocks_testing=True) for n, q in enumerate(quotes, start=1)]

    light, reason = _light(doc, seg, *found)

    assert light is TrafficLight.RED
    assert reason.startswith("7 критичных нарушений мешают")
    assert reason.endswith("F-05 и ещё 2)")


def test_summary_counters_match_tables(doc: ParsedDocument, seg: Segmentation) -> None:
    found = [
        merged(
            LIMIT_FIVE,
            LIMIT_THREE,
            mid="M-001",
            defect=DefectType.CONTRADICTION,
            prop=Property.SET_CONSISTENT,
            scope=Scope.SET,
            blocks_testing=True,
        ),
        merged(SAME_DAY, mid="M-002", verdict=Verdict.UNCLEAR, blocks_testing=True),
        merged(OVERDUE, INVENTED, mid="M-003", block_ids=("B003",), defect=DefectType.TRACEABILITY),
        merged("Возврат книг", mid="M-004", block_ids=("B005",), scope=Scope.SET, prop=Property.SET_COMPLETE),
        merged(INVENTED, mid="M-005"),
        merged(SAME_DAY, mid="M-006", critic=CriticDecision(action=CriticAction.DROP, reason="Снять.")),
    ]

    ver = verify(found, doc, seg)
    s, findings = ver.summary, ver.findings

    assert s.findings_total == len(findings) == 4
    assert s.violations + s.unclear == s.findings_total
    assert sum(s.by_severity.values()) == sum(s.by_property.values()) == sum(s.by_defect_type.values()) == 4
    assert s.critical_ids == [
        f.id for f in findings if f.severity is Severity.CRITICAL and f.verdict is Verdict.VIOLATION
    ]
    assert s.critical == len(s.critical_ids) == s.by_severity.get(Severity.CRITICAL, 0) == 1
    assert s.requirements_total == len(ver.requirement_status) == len(seg.requirements)
    assert s.requirements_with_findings == sum(1 for ids in ver.requirement_status.values() if ids) == 4
    assert s.set_level == sum(f.scope is Scope.SET for f in findings) == 2
    assert s.discarded_total == len(ver.discarded) == sum(s.discarded_by_reason.values()) == 2
    assert s.discarded_by_reason == {DiscardReason.QUOTE_NOT_FOUND: 1, DiscardReason.CRITIC_REJECTED: 1}
    assert s.quotes_total == s.quotes_exact + s.quotes_fuzzy + s.quotes_not_found == 8
    assert s.quotes_not_found == 2
    assert s.anchors_corrected == sum(f.anchor_corrected for f in findings) == 1
    assert list(s.by_severity) == sorted(s.by_severity, key=lambda sev: sev.rank)


def test_report_shows_ids_severity_light_and_rule(tmp_path: Path) -> None:
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "out" / "report.md", backend=Backend.FAKE)

    outcome = pipeline.run(cfg)

    report = cfg.report_md_path.read_text(encoding="utf-8")
    assert outcome.exit_code is ExitCode.OK
    assert "#### F-01 · критично" in report
    assert "- **Требования:** R-" in report
    assert f"Правило светофора: {TRAFFIC_RULE}" in report
    assert "🔴" in report
