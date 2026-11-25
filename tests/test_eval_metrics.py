"""Метрики eval и разбор вердикта судьи на игрушечных данных (без сети и без эталона)."""

from __future__ import annotations

import pytest

from evals.findings import JudgeItem
from evals.gold import Gold, GoldDefect, GoldSeverity
from evals.judge import EvalError, ExtraVerdict, GoldStatus, JudgeCall, JudgeVerdict, parse_verdict
from evals.metrics import aggregate, count_findings, jaccard, mean_pairwise_jaccard, score_run
from evals.runs import CallStats, QuoteStats, RunData, Source

CALL = JudgeCall(key="k", source="fake")


def toy_gold() -> Gold:
    severities = [GoldSeverity.CRITICAL, GoldSeverity.CRITICAL, GoldSeverity.MEDIUM, GoldSeverity.LOW]
    return Gold(
        version="toy",
        source="toy",
        defects=[
            GoldDefect(id=n, where=f"место {n}", defect=f"дефект {n}", criterion="Полнота", severity=s)
            for n, s in enumerate(severities, start=1)
        ],
    )


def items(n: int) -> list[JudgeItem]:
    return [
        JudgeItem(id=i, quote=f"цитата {i}", property="Полнота", explanation=f"почему {i}")
        for i in range(1, n + 1)
    ]


def toy_run(n_items: int = 5, keys: list[str] | None = None, label: str = "run-a") -> RunData:
    return RunData(
        label=label,
        source=Source.AGENT,
        items=items(n_items),
        quotes=QuoteStats(total=4, exact=2, fuzzy=1, not_found=1),
        calls=CallStats(),
        keys=keys,
    )


def verdict(
    gold: dict[int, tuple[str, list[int]]], extra: dict[int, str] | None = None, n: int = 5
) -> JudgeVerdict:
    raw = {
        "gold": [{"id": g, "status": s, "finding_ids": ids} for g, (s, ids) in gold.items()],
        "extra": [{"finding_id": f, "verdict": v} for f, v in (extra or {}).items()],
    }
    return parse_verdict(raw, toy_gold().ids, n)


def test_recall_counts_found_and_partial_strict_counts_found() -> None:
    v = verdict({1: ("found", [1]), 2: ("partial", [2]), 3: ("missed", []), 4: ("found", [3])})

    score = score_run(toy_run(), v, toy_gold(), CALL, findings=5)

    assert score.recall == 0.75
    assert score.recall_strict == 0.5
    assert score.critical_recall == 1.0
    assert score.critical_recall_strict == 0.5
    assert score.recall_by_severity == {
        GoldSeverity.CRITICAL: 1.0,
        GoldSeverity.MEDIUM: 0.0,
        GoldSeverity.LOW: 1.0,
    }


def test_noise_share_counts_noise_from_all_findings() -> None:
    v = verdict(
        {1: ("found", [1]), 2: ("missed", []), 3: ("missed", []), 4: ("missed", [])},
        extra={2: "noise", 3: "real", 4: "real"},
    )

    score = score_run(toy_run(), v, toy_gold(), CALL, findings=5)

    assert (score.extra_real, score.extra_noise) == (2, 1)
    assert score.noise_share == pytest.approx(0.2)
    assert [u.id for u in score.unmatched] == [2, 3, 4, 5]
    assert score.unmatched_unjudged == 1  # находка 5 без вердикта real/noise


def test_quote_stats_shares() -> None:
    q = QuoteStats(total=4, exact=2, fuzzy=1, not_found=1)

    assert q.share_exact == 0.5
    assert q.share_verified == 0.75
    assert QuoteStats().share_exact is None


def test_jaccard_of_sets() -> None:
    assert jaccard({1, 2, 3}, {2, 3, 4}) == 0.5
    assert jaccard(set(), set()) == 1.0
    assert mean_pairwise_jaccard([{1}]) is None
    assert mean_pairwise_jaccard([{1, 2}, {1, 2}, {1}]) == pytest.approx((1 + 0.5 + 0.5) / 3)


def test_aggregate_union_pass_k_and_jaccard_over_runs() -> None:
    gold = toy_gold()
    a = score_run(
        toy_run(keys=["r1|IND-UNAMB", "r2|SET-CONS"], label="a"),
        verdict({1: ("found", [1]), 2: ("partial", [2]), 3: ("missed", []), 4: ("missed", [])}),
        gold,
        CALL,
        findings=5,
    )
    b = score_run(
        toy_run(keys=["r1|IND-UNAMB", "r3|IND-COMPL"], label="b"),
        verdict({1: ("found", [1]), 2: ("missed", []), 3: ("found", [2]), 4: ("missed", [])}),
        gold,
        CALL,
        findings=5,
    )

    config = aggregate([a, b], gold)

    assert config.recall == pytest.approx(0.5)
    assert config.union_recall == 0.75  # {1, 2, 3}
    assert config.union_recall_strict == 0.5  # {1, 3}
    assert config.critical_pass_all == 0.5  # только дефект 1 найден в обоих прогонах
    assert config.critical_pass_any == 1.0
    assert config.gold_jaccard == pytest.approx(1 / 3)  # {1, 2} и {1, 3}
    assert config.key_jaccard == pytest.approx(1 / 3)
    assert config.quotes_exact == 0.5


def test_key_jaccard_is_none_without_agent_keys() -> None:
    gold = toy_gold()
    v = verdict({1: ("found", [1]), 2: ("missed", []), 3: ("missed", []), 4: ("missed", [])})
    runs = [score_run(toy_run(keys=None, label=x), v, gold, CALL, findings=5) for x in "ab"]

    assert aggregate(runs, gold).key_jaccard is None
    assert aggregate(runs, gold).gold_jaccard == 1.0


def test_unrated_gold_defect_counts_as_missed_with_warning() -> None:
    v = parse_verdict({"gold": [{"id": 1, "status": "found", "finding_ids": [1]}], "extra": []}, [1, 2], 3)

    assert v.gold[2].status is GoldStatus.MISSED
    assert any("2" in w for w in v.warnings)


def test_out_of_range_finding_ids_are_dropped() -> None:
    raw = {
        "gold": [{"id": 1, "status": "found", "finding_ids": [1, 7, 0]}],
        "extra": [{"finding_id": 9, "verdict": "noise"}, {"finding_id": 2, "verdict": "real"}],
    }

    v = parse_verdict(raw, [1], 3)

    assert v.gold[1].finding_ids == [1]
    assert v.extra == {2: ExtraVerdict.REAL}
    assert len(v.warnings) == 2


def test_unknown_gold_id_and_status_are_skipped_duplicate_last_wins() -> None:
    raw = {
        "gold": [
            {"id": 99, "status": "found", "finding_ids": []},
            {"id": 1, "status": "maybe", "finding_ids": []},
            {"id": 2, "status": "missed", "finding_ids": []},
            {"id": 2, "status": "partial", "finding_ids": [1]},
        ],
        "extra": [],
    }

    v = parse_verdict(raw, [1, 2], 2)

    assert v.gold[1].status is GoldStatus.MISSED
    assert v.gold[2].status is GoldStatus.PARTIAL
    assert len(v.warnings) == 4


def test_judge_answer_without_gold_list_is_an_error() -> None:
    with pytest.raises(EvalError):
        parse_verdict({"extra": []}, [1], 1)
    with pytest.raises(EvalError):
        parse_verdict(None, [1], 1)


def test_research21_mode_keeps_any_positive_finding_ids() -> None:
    raw = {
        "gold": [{"id": 1, "status": "found", "finding_ids": [40]}],
        "extra": [{"finding_id": 41, "verdict": "noise"}],
    }

    v = parse_verdict(raw, [1], None)

    assert v.gold[1].finding_ids == [40]
    assert count_findings(v, None) == 41
    assert count_findings(v, items(3)) == 3


def test_run_without_findings_drops_judge_ids_and_counts_zero() -> None:
    raw = {
        "gold": [{"id": 1, "status": "found", "finding_ids": [5]}],
        "extra": [{"finding_id": 3, "verdict": "noise"}],
    }

    v = parse_verdict(raw, [1], 0)
    score = score_run(toy_run(0), v, toy_gold(), CALL, findings=count_findings(v, []))

    assert count_findings(v, []) == 0
    assert v.gold[1].finding_ids == [] and v.extra == {}
    assert score.findings == 0 and score.noise_share is None
    assert score.unmatched == []


def test_extra_verdict_for_matched_finding_is_not_counted_and_warned() -> None:
    v = verdict({1: ("found", [1]), 2: ("missed", []), 3: ("missed", []), 4: ("missed", [])}, {1: "noise"})

    score = score_run(toy_run(), v, toy_gold(), CALL, findings=5)

    assert (score.extra_real, score.extra_noise) == (0, 0)
    assert score.noise_share == 0.0
    assert any("[1]" in w for w in score.warnings)


def test_judge_numbered_findings_are_listed_without_text() -> None:
    raw = {
        "gold": [{"id": 1, "status": "found", "finding_ids": [2]}],
        "extra": [{"finding_id": 4, "verdict": "noise"}],
    }
    v = parse_verdict(raw, toy_gold().ids, None)

    score = score_run(toy_run(2), v, toy_gold(), CALL, findings=count_findings(v, None), judge_numbered=True)

    assert score.findings == 4
    assert [(u.id, u.fingerprint, u.quote) for u in score.unmatched] == [
        (1, None, None),
        (3, None, None),
        (4, None, None),
    ]
    assert score.unmatched[2].verdict is ExtraVerdict.NOISE
    assert score.noise_share == 0.25
