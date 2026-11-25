"""Шаг 5: слияние проходов и линтера, «найдено в N из K» (#29)."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import REQUIREMENTS
from validator import pipeline
from validator.config import load_config
from validator.merge import PROPERTY_GROUPS, merge
from validator.models import (
    Backend,
    CandidateFinding,
    DefectType,
    DocKind,
    FindingSource,
    ParsedDocument,
    PassResult,
    Property,
    Scope,
    Severity,
    SourceKind,
    Verdict,
)

DOC = """# Бронирование

Система должна быстро подтверждать бронь. Пользователь получает письмо с номером брони.

Бронь хранится 30 дней. Бронь хранится 60 дней.

Отмена брони доступна администратору.
"""
# Блоки документа: B002 первый абзац, B003 второй, B004 третий.

SENTENCE = "Система должна быстро подтверждать бронь."


@pytest.fixture
def doc(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(DOC)


def cand(
    source: int | str,
    n: int,
    prop: Property,
    quotes: list[str],
    blocks: list[str],
    *,
    defect: DefectType | None = None,
    verdict: Verdict = Verdict.VIOLATION,
    explanation: str = "Пояснение.",
    blocks_testing: bool = False,
    severity: Severity | None = None,
) -> CandidateFinding:
    """Кандидат прохода (``source`` число) или линтера (``source`` ID правила)."""
    if isinstance(source, int):
        src = FindingSource(kind=SourceKind.PASS, pass_index=source)
        cid = f"p{source}.{n:03d}"
    else:
        src = FindingSource(kind=SourceKind.LINT, rule_id=source)
        cid = f"lint.{source}.{n:03d}"
    default_defect = {
        Property.VERIFIABLE: DefectType.UNVERIFIABLE,
        Property.UNAMBIGUOUS: DefectType.AMBIGUOUS,
        Property.SET_CONSISTENT: DefectType.CONTRADICTION,
        Property.COMPLETE: DefectType.INCOMPLETE,
        Property.SINGULAR: DefectType.NOT_ATOMIC,
    }
    return CandidateFinding(
        cid=cid,
        source=src,
        property=prop,
        defect_type=defect or default_defect[prop],
        verdict=verdict,
        scope=prop.scope,
        block_ids=blocks,
        quotes=quotes,
        explanation=explanation,
        blocks_testing=blocks_testing,
        severity_hint=severity,
    )


def passes_of(*groups: list[CandidateFinding], failed: tuple[int, ...] = ()) -> list[PassResult]:
    """Проходы 1..K; номера из ``failed`` упали (их кандидаты слияние не видит)."""
    return [
        PassResult(index=k, ok=k not in failed, candidates=group, error="сбой" if k in failed else None)
        for k, group in enumerate(groups, start=1)
    ]


def three_wordings() -> list[PassResult]:
    """Одна находка тремя формулировками: разные цитаты одной фразы, свойства из одной группы."""
    return passes_of(
        [cand(1, 1, Property.VERIFIABLE, [SENTENCE], ["B002"], explanation="Нет числа: что значит быстро.")],
        [
            cand(
                2,
                1,
                Property.UNAMBIGUOUS,
                ["быстро подтверждать бронь"],
                ["B002"],
                verdict=Verdict.UNCLEAR,
                explanation="Слово «быстро» каждый читает по-своему, тест не написать без срока в секундах.",
            )
        ],
        [cand(3, 1, Property.VERIFIABLE, ["должна быстро подтверждать"], [], blocks_testing=True)],
    )


# ---------------------------------------------------------------- одна находка разными словами


def test_one_finding_in_three_wordings_is_one_cluster_found_in_3_of_3(doc: ParsedDocument) -> None:
    merged = merge(three_wordings(), [], doc)

    assert len(merged) == 1
    m = merged[0]
    assert (m.found_in, m.of) == (3, 3)
    assert m.members == ["p1.001", "p2.001", "p3.001"]
    assert [s.pass_index for s in m.sources] == [1, 2, 3]
    assert m.property is Property.VERIFIABLE  # большинство: 2 из 3
    assert m.scope is Scope.REQUIREMENT
    assert m.block_ids == ["B002"]


def test_texts_come_from_voice_matching_the_vote(doc: ParsedDocument) -> None:
    m = merge(three_wordings(), [], doc)[0]

    assert m.quotes == [SENTENCE]  # короткие цитаты внутри полной фразы не повторяются
    # Объяснение от прохода со свойством-победителем и самой длинной цитатой, а не от проигравшего.
    assert m.explanation == "Нет числа: что значит быстро."


def test_disagreements_between_passes_are_kept_for_critic(doc: ParsedDocument) -> None:
    m = merge(three_wordings(), [], doc)[0]

    text = "\n".join(m.disagreements)
    assert "свойство" in text
    assert "IND-UNAMB" in text
    assert "проход 2" in text
    assert "вердикт" in text
    assert "мешает тесту" in text
    assert m.verdict is Verdict.VIOLATION  # большинство
    assert m.blocks_testing is True  # хотя бы один источник: безопасная сторона


# ---------------------------------------------------------------- разные находки не сливаются


def test_two_different_findings_in_one_block_stay_apart(doc: ParsedDocument) -> None:
    passes = passes_of(
        [
            cand(1, 1, Property.VERIFIABLE, ["быстро подтверждать бронь"], ["B002"]),
            cand(1, 2, Property.COMPLETE, ["письмо с номером брони"], ["B002"]),
        ],
        [
            cand(2, 1, Property.VERIFIABLE, ["быстро подтверждать"], ["B002"]),
            cand(2, 2, Property.UNAMBIGUOUS, ["Пользователь получает письмо"], ["B002"]),
        ],
    )

    merged = merge(passes, [], doc)

    assert sorted(m.members for m in merged) == [["p1.001", "p2.001"], ["p1.002"], ["p2.002"]]


def test_same_quote_with_unrelated_property_stays_apart(doc: ParsedDocument) -> None:
    passes = passes_of(
        [cand(1, 1, Property.VERIFIABLE, [SENTENCE], ["B002"])],
        [cand(2, 1, Property.SINGULAR, [SENTENCE], ["B002"])],
    )

    merged = merge(passes, [], doc)

    assert len(merged) == 2
    assert {(m.found_in, m.of) for m in merged} == {(1, 2)}


def test_fragment_with_other_property_of_same_group_stays_apart(doc: ParsedDocument) -> None:
    passes = passes_of(
        [cand(1, 1, Property.VERIFIABLE, [SENTENCE], ["B002"])],
        [cand(2, 1, Property.UNAMBIGUOUS, ["подтверждать бронь"], ["B002"])],
    )

    merged = merge(passes, [], doc)

    assert sorted(m.members for m in merged) == [["p1.001"], ["p2.001"]]


def test_contradictions_sharing_one_side_are_different_findings(doc: ParsedDocument) -> None:
    a, b, c = "Бронь хранится 30 дней.", "Бронь хранится 60 дней.", "Отмена брони доступна администратору."
    passes = passes_of(
        [cand(1, 1, Property.SET_CONSISTENT, [a, b], ["B003"])],
        [cand(2, 1, Property.SET_CONSISTENT, [a, c], ["B003", "B004"])],
        [cand(3, 1, Property.SET_CONSISTENT, [b, a], ["B003"])],
    )

    merged = merge(passes, [], doc)

    assert sorted(m.members for m in merged) == [["p1.001", "p3.001"], ["p2.001"]]


def test_contradictions_do_not_chain_through_one_sided_finding(doc: ParsedDocument) -> None:
    a, b, c = "Бронь хранится 30 дней.", "Бронь хранится 60 дней.", "Отмена брони доступна администратору."
    passes = passes_of(
        [cand(1, 1, Property.SET_CONSISTENT, [a], ["B003"])],
        [cand(2, 1, Property.SET_CONSISTENT, [a, c], ["B003", "B004"])],
        [cand(3, 1, Property.SET_CONSISTENT, [a, b], ["B003"])],
    )

    merged = merge(passes, [], doc)

    assert len(merged) == 2
    assert not any({"p2.001", "p3.001"} <= set(m.members) for m in merged)


def test_quote_outside_stated_block_with_several_occurrences_has_no_place(doc: ParsedDocument) -> None:
    # «брони» есть и в B002, и в B004; модель указала B003, где его нет: место неизвестно.
    passes = passes_of(
        [cand(1, 1, Property.UNAMBIGUOUS, ["брони"], ["B003"])],
        [cand(2, 1, Property.UNAMBIGUOUS, ["письмо с номером брони"], ["B002"])],
    )

    merged = merge(passes, [], doc)

    assert len(merged) == 2


def test_no_chaining_of_two_findings_of_one_pass_through_a_third(doc: ParsedDocument) -> None:
    passes = passes_of(
        [
            cand(1, 1, Property.VERIFIABLE, ["Система должна быстро"], ["B002"]),
            cand(1, 2, Property.VERIFIABLE, ["подтверждать бронь"], ["B002"]),
        ],
        [cand(2, 1, Property.VERIFIABLE, [SENTENCE], ["B002"])],
    )

    merged = merge(passes, [], doc)

    assert len(merged) == 2
    assert sorted(len(m.members) for m in merged) == [1, 2]
    assert all(m.found_in <= 2 for m in merged)


def test_quote_not_in_document_is_compared_by_text(doc: ParsedDocument) -> None:
    passes = passes_of(
        [cand(1, 1, Property.SET_CONSISTENT, ["Бронь хранится тридцать дней, а потом шестьдесят"], ["B003"])],
        [cand(2, 1, Property.SET_CONSISTENT, ["бронь хранится тридцать дней а потом шестьдесят"], ["B003"])],
    )

    merged = merge(passes, [], doc)

    assert len(merged) == 1
    assert merged[0].found_in == 2
    assert len(merged[0].quotes) == 1


# ---------------------------------------------------------------- порядок проходов


def test_pass_order_does_not_change_result(doc: ParsedDocument) -> None:
    passes = passes_of(
        [
            cand(1, 1, Property.VERIFIABLE, [SENTENCE], ["B002"]),
            cand(
                1,
                2,
                Property.SET_CONSISTENT,
                ["Бронь хранится 30 дней.", "Бронь хранится 60 дней."],
                ["B003"],
            ),
        ],
        [
            cand(2, 1, Property.SET_CONSISTENT, ["Бронь хранится 60 дней."], ["B003"]),
            cand(2, 2, Property.UNAMBIGUOUS, ["быстро подтверждать бронь"], ["B002"]),
            cand(2, 3, Property.COMPLETE, ["доступна администратору"], ["B004"]),
        ],
        [
            cand(3, 1, Property.COMPLETE, ["Отмена брони доступна администратору."], ["B004"]),
            cand(3, 2, Property.VERIFIABLE, ["должна быстро"], ["B002"]),
        ],
    )
    lint = [cand("L-01", 1, Property.UNAMBIGUOUS, ["быстро"], ["B002"])]
    expected = [m.model_dump() for m in merge(passes, lint, doc)]

    for order in itertools.permutations(passes):
        shuffled = [p.model_copy(update={"candidates": list(reversed(p.candidates))}) for p in order]
        assert [m.model_dump() for m in merge(shuffled, lint, doc)] == expected


def test_merged_ids_follow_document_order(doc: ParsedDocument) -> None:
    passes = passes_of(
        [
            cand(1, 1, Property.COMPLETE, ["Отмена брони доступна администратору."], ["B004"]),
            cand(1, 2, Property.VERIFIABLE, [SENTENCE], ["B002"]),
        ]
    )

    merged = merge(passes, [], doc)

    assert [(m.mid, m.block_ids) for m in merged] == [("M-001", ["B002"]), ("M-002", ["B004"])]


# ---------------------------------------------------------------- линтер


def test_lint_hit_attaches_to_pass_finding_without_raising_n(doc: ParsedDocument) -> None:
    lint = [cand("L-07", 1, Property.UNAMBIGUOUS, ["быстро"], ["B002"])]

    merged = merge(three_wordings(), lint, doc)

    assert len(merged) == 1
    m = merged[0]
    assert (m.found_in, m.of) == (3, 3)
    assert [s.kind for s in m.sources] == [SourceKind.PASS] * 3 + [SourceKind.LINT]
    assert m.sources[-1].rule_id == "L-07"
    assert m.property is Property.VERIFIABLE  # голосуют проходы, линтер только источник


def test_hits_of_different_lint_rules_on_one_phrase_stay_apart(doc: ParsedDocument) -> None:
    lint = [
        cand("L-02", 1, Property.UNAMBIGUOUS, ["быстро подтверждать"], ["B002"]),
        cand("L-04", 1, Property.UNAMBIGUOUS, ["быстро подтверждать бронь"], ["B002"]),
    ]

    merged = merge([], lint, doc)

    assert len(merged) == 2
    assert sorted(m.sources[0].rule_id or "" for m in merged) == ["L-02", "L-04"]


def test_pass_finding_joins_one_lint_hit_but_does_not_chain_two_rules(doc: ParsedDocument) -> None:
    passes = passes_of([cand(1, 1, Property.UNAMBIGUOUS, [SENTENCE], ["B002"])])
    lint = [
        cand("L-02", 1, Property.UNAMBIGUOUS, ["быстро подтверждать"], ["B002"]),
        cand("L-04", 1, Property.UNAMBIGUOUS, ["быстро подтверждать бронь"], ["B002"]),
    ]

    merged = merge(passes, lint, doc)

    assert len(merged) == 2
    assert all(len({s.rule_id for s in m.sources if s.kind is SourceKind.LINT}) <= 1 for m in merged)
    assert sum(1 for m in merged if m.found_in == 1) == 1  # находка прохода достаётся одному из двух


@pytest.mark.parametrize("first", [True, False], ids=["L-02 первым", "L-04 первым"])
def test_two_passes_and_two_lint_rules_keep_n_equal_to_2(doc: ParsedDocument, first: bool) -> None:
    """Находки двух проходов сливаются в одну, срабатывания линтера не делят её надвое и не занижают N."""
    passes = passes_of(
        [cand(1, 1, Property.UNAMBIGUOUS, ["быстро подтверждать"], ["B002"])],
        [cand(2, 1, Property.UNAMBIGUOUS, ["подтверждать бронь"], ["B002"])],
    )
    lint = [
        cand("L-02", 1, Property.UNAMBIGUOUS, ["быстро подтверждать"], ["B002"]),
        cand("L-04", 1, Property.UNAMBIGUOUS, ["подтверждать бронь"], ["B002"]),
    ]

    merged = merge(passes, lint if first else lint[::-1], doc)

    assert sorted((m.found_in, len(m.sources)) for m in merged) == [(0, 1), (2, 3)]


def test_two_hits_of_the_same_lint_rule_are_still_merged_when_they_overlap(doc: ParsedDocument) -> None:
    lint = [
        cand("L-02", 1, Property.UNAMBIGUOUS, ["быстро подтверждать"], ["B002"]),
        cand("L-02", 2, Property.UNAMBIGUOUS, ["быстро подтверждать бронь"], ["B002"]),
    ]

    assert len(merge([], lint, doc)) == 1


def test_lint_hint_and_test_flag_survive_when_passes_have_none(doc: ParsedDocument) -> None:
    passes = passes_of([cand(1, 1, Property.VERIFIABLE, [SENTENCE], ["B002"])])
    lint = [
        cand(
            "L-01",
            1,
            Property.UNAMBIGUOUS,
            ["быстро"],
            ["B002"],
            severity=Severity.CRITICAL,
            blocks_testing=True,
        )
    ]

    m = merge(passes, lint, doc)[0]

    assert m.members == ["p1.001", "lint.L-01.001"]
    assert m.severity_hint is Severity.CRITICAL
    assert m.blocks_testing is True


def test_lint_only_finding_is_found_in_zero_passes(doc: ParsedDocument) -> None:
    lint = [cand("L-03", 1, Property.COMPLETE, ["доступна администратору"], ["B004"])]

    merged = merge(three_wordings(), lint, doc)

    lint_only = [m for m in merged if m.members == ["lint.L-03.001"]]
    assert len(lint_only) == 1
    assert (lint_only[0].found_in, lint_only[0].of) == (0, 3)


# ---------------------------------------------------------------- упавшие проходы


def test_failed_pass_is_not_counted_k_is_2(doc: ParsedDocument) -> None:
    passes = passes_of(
        [cand(1, 1, Property.VERIFIABLE, [SENTENCE], ["B002"])],
        [cand(2, 1, Property.VERIFIABLE, [SENTENCE], ["B002"])],
        [cand(3, 1, Property.VERIFIABLE, ["должна быстро"], ["B002"])],
        failed=(2,),
    )

    merged = merge(passes, [], doc)

    assert [(m.found_in, m.of, m.members) for m in merged] == [(2, 2, ["p1.001", "p3.001"])]


def test_all_passes_failed_gives_lint_only(doc: ParsedDocument) -> None:
    lint = [cand("L-01", 1, Property.UNAMBIGUOUS, ["быстро"], ["B002"])]

    merged = merge(passes_of([], [], failed=(1, 2)), lint, doc)

    assert [(m.found_in, m.of) for m in merged] == [(0, 0)]


def test_nothing_to_merge_gives_empty_list(doc: ParsedDocument) -> None:
    assert merge([], [], doc) == []


# ---------------------------------------------------------------- голосование и подсказки


def test_severity_hint_is_majority_and_tie_goes_to_more_severe(doc: ParsedDocument) -> None:
    passes = passes_of(
        [cand(1, 1, Property.VERIFIABLE, [SENTENCE], ["B002"], severity=Severity.MINOR)],
        [cand(2, 1, Property.VERIFIABLE, [SENTENCE], ["B002"], severity=Severity.CRITICAL)],
    )

    m = merge(passes, [], doc)[0]

    assert m.severity_hint is Severity.CRITICAL


def test_finding_without_quotes_merges_by_gist(doc: ParsedDocument) -> None:
    gist = "В документе нет раздела о хранении персональных данных клиента."
    passes = passes_of(
        [cand(1, 1, Property.SET_COMPLETE, [], [], defect=DefectType.INCOMPLETE, explanation=gist)],
        [
            cand(
                2,
                1,
                Property.SET_COMPLETE,
                [],
                [],
                defect=DefectType.INCOMPLETE,
                explanation=gist + " Это риск.",
            )
        ],
    )

    merged = merge(passes, [], doc)

    assert [(m.found_in, m.scope) for m in merged] == [(2, Scope.SET)]


def test_every_property_is_in_exactly_one_group() -> None:
    counts = {p: sum(p in g for g in PROPERTY_GROUPS) for p in Property}

    assert set(counts.values()) == {1}


# ---------------------------------------------------------------- сквозной прогон на фикстурах


def test_pipeline_report_shows_found_in_n_of_3(tmp_path: Path) -> None:
    cfg = load_config(
        REQUIREMENTS[DocKind.MD], tmp_path / "out" / "report.md", backend=Backend.FAKE, passes=3
    )

    outcome = pipeline.run(cfg)

    report = json.loads(cfg.report_json_path.read_text(encoding="utf-8"))
    findings = report["verification"]["findings"]
    assert outcome.report is not None
    assert findings
    assert {f["of"] for f in findings} == {3}
    assert max(f["found_in"] for f in findings) == 3
    md = cfg.report_md_path.read_text(encoding="utf-8")
    # Находка только линтера (0 из 3) пишется словами: «проходы модели этого не отметили (0 из 3)».
    assert md.count("из 3 проходов") + md.count("не отметили (0 из 3)") >= len(findings)
