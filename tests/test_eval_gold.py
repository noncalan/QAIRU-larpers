"""Эталон в YAML совпадает с ``docs/gold/defects.md``: число, номера и ячейки таблиц дословно."""

from __future__ import annotations

from evals.gold import (
    GOLD_TOTAL,
    GoldSeverity,
    load_gold,
    parse_gold_md,
    read_gold_md,
)


def test_gold_yaml_matches_markdown_cell_by_cell() -> None:
    from_md = parse_gold_md(read_gold_md())

    from_yaml = load_gold().defects

    assert len(from_yaml) == len(from_md) == GOLD_TOTAL
    assert [d.id for d in from_yaml] == [d.id for d in from_md]
    mismatched = [y.id for y, m in zip(from_yaml, from_md, strict=True) if y != m]
    assert not mismatched, f"gold.yaml расходится с defects.md в дефектах {mismatched}"


def test_gold_ids_are_consecutive_from_one() -> None:
    assert load_gold().ids == list(range(1, GOLD_TOTAL + 1))


def test_gold_severity_counts_match_research_21() -> None:
    gold = load_gold()

    counts = {s: len(gold.ids_with(s)) for s in GoldSeverity}

    assert counts == {GoldSeverity.CRITICAL: 9, GoldSeverity.MEDIUM: 21, GoldSeverity.LOW: 15}
    assert gold.critical_ids == [1, 2, 3, 4, 6, 8, 14, 25, 34]


def test_disputed_and_addendum_flags() -> None:
    gold = load_gold()

    assert [d.id for d in gold.defects if d.disputed] == [40]
    assert [d.id for d in gold.defects if d.addendum] == list(range(34, 46))
