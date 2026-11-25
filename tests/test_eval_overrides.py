"""Ручные поправки важнее судьи: меняют статус дефекта эталона и вердикт находки вне эталона."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.judge import EvalError, ExtraVerdict, GoldStatus, JudgeCall, parse_verdict
from evals.metrics import score_run
from evals.overrides import (
    DEFAULT_OVERRIDES,
    FindingOverride,
    GoldOverride,
    Overrides,
    apply_overrides,
    load_overrides,
)
from test_eval_metrics import items, toy_gold, toy_run

CALL = JudgeCall(key="k", source="fake")


def judged() -> tuple[object, list[int]]:
    raw = {
        "gold": [
            {"id": 1, "status": "found", "finding_ids": [1]},
            {"id": 2, "status": "missed", "finding_ids": []},
            {"id": 3, "status": "partial", "finding_ids": [2]},
            {"id": 4, "status": "missed", "finding_ids": []},
        ],
        "extra": [{"finding_id": 3, "verdict": "real"}, {"finding_id": 4, "verdict": "noise"}],
    }
    return raw, toy_gold().ids


def test_gold_override_changes_recall() -> None:
    raw, ids = judged()
    verdict = parse_verdict(raw, ids, 5)
    overrides = Overrides(
        gold=[
            GoldOverride(
                run="run-a", gold=2, status=GoldStatus.FOUND, finding_ids=[3], reason="судья не увидел"
            )
        ]
    )

    before = score_run(toy_run(), verdict, toy_gold(), CALL, findings=5)
    fixed, applied = apply_overrides(verdict, "run-a", items(5), overrides)
    after = score_run(toy_run(), fixed, toy_gold(), CALL, findings=5, applied=applied)

    assert before.recall == 0.5
    assert after.recall == 0.75
    assert after.critical_recall == 1.0
    assert after.gold[2] is GoldStatus.FOUND
    assert len(after.overrides) == 1
    assert 3 not in [u.id for u in after.unmatched]


def test_override_for_other_run_is_ignored_pattern_matches() -> None:
    raw, ids = judged()
    verdict = parse_verdict(raw, ids, 5)
    other = Overrides(gold=[GoldOverride(run="run-b", gold=2, status=GoldStatus.FOUND, reason="r")])
    pattern = Overrides(
        gold=[GoldOverride(run="results/baseline/run-*", gold=2, status=GoldStatus.PARTIAL, reason="r")]
    )

    unchanged, applied_other = apply_overrides(verdict, "run-a", items(5), other)
    matched, applied_pattern = apply_overrides(verdict, "results/baseline/run-2", items(5), pattern)

    assert unchanged.gold == verdict.gold
    assert not applied_other
    assert matched.gold[2].status is GoldStatus.PARTIAL
    assert applied_pattern


def test_missed_override_clears_finding_ids() -> None:
    raw, ids = judged()
    verdict = parse_verdict(raw, ids, 5)
    overrides = Overrides(
        gold=[GoldOverride(run="*", gold=1, status=GoldStatus.MISSED, finding_ids=[1], reason="r")]
    )

    fixed, _ = apply_overrides(verdict, "run-a", items(5), overrides)

    assert fixed.gold[1].finding_ids == []
    assert 1 not in fixed.matched_findings


def test_finding_override_by_fingerprint_turns_real_into_noise() -> None:
    raw, ids = judged()
    verdict = parse_verdict(raw, ids, 5)
    target = items(5)[2]  # находка 3, судья счёл её настоящей
    overrides = Overrides(
        findings=[
            FindingOverride(finding=target.fingerprint()[:8], verdict=ExtraVerdict.NOISE, reason="придирка")
        ]
    )

    fixed, applied = apply_overrides(verdict, "any-run", items(5), overrides)
    score = score_run(toy_run(), fixed, toy_gold(), CALL, findings=5, applied=applied)

    assert fixed.extra[3] is ExtraVerdict.NOISE
    assert (score.extra_real, score.extra_noise) == (0, 2)
    assert score.noise_share == pytest.approx(0.4)


def test_out_of_range_override_finding_ids_are_dropped_with_warning() -> None:
    raw, ids = judged()
    verdict = parse_verdict(raw, ids, 5)
    overrides = Overrides(
        gold=[GoldOverride(run="*", gold=2, status=GoldStatus.FOUND, finding_ids=[2, 99], reason="r")]
    )

    fixed, _ = apply_overrides(verdict, "run-a", items(5), overrides)

    assert fixed.gold[2].finding_ids == [2]
    assert any("99" in w for w in fixed.warnings)


def test_load_overrides_from_yaml(tmp_path: Path) -> None:
    path = tmp_path / "overrides.yaml"
    path.write_text(
        "gold:\n  - run: run-a\n    gold: 2\n    status: partial\n    reason: «вскользь»\n"
        "findings:\n  - finding: abcdef12\n    verdict: noise\n    reason: шум\n",
        encoding="utf-8",
    )

    overrides = load_overrides(path)

    assert overrides.gold[0].status is GoldStatus.PARTIAL
    assert overrides.findings[0].verdict is ExtraVerdict.NOISE


def test_invalid_overrides_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "overrides.yaml"
    path.write_text("gold:\n  - run: x\n    gold: 2\n    status: maybe\n    reason: r\n", encoding="utf-8")

    with pytest.raises(EvalError):
        load_overrides(path)


def test_repository_overrides_file_is_valid_and_missing_file_means_none(tmp_path: Path) -> None:
    assert isinstance(load_overrides(DEFAULT_OVERRIDES), Overrides)
    assert load_overrides(tmp_path / "nope.yaml") == Overrides()
    assert load_overrides(None) == Overrides()
