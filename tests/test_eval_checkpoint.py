"""Контрольная точка #37: правило D-011 кодом на синтетических сводках и таблица из результатов eval."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from conftest import ROOT
from evals.checkpoint import decide, main
from evals.metrics import ConfigScore
from validator.models import SearchInput

BASELINE_RESULT = ROOT / "evals" / "results" / "2026-11-26-baseline.json"


def score(**over: Any) -> ConfigScore:
    base: dict[str, Any] = {
        "runs": 3,
        "recall": 0.90,
        "recall_strict": 0.80,
        "critical_recall": 8 / 9,
        "critical_recall_strict": 7 / 9,
        "findings": 60.0,
        "noise_share": 0.03,
        "quotes_exact": 0.95,
        "quotes_verified": 0.97,
        "quotes_final_verified": 1.0,
        "wall_s": 400.0,
        "cost_usd": 1.5,
        "union_recall": 0.95,
        "union_recall_strict": 0.9,
        "critical_pass_all": 7 / 9,
        "critical_pass_any": 1.0,
        "gold_jaccard": 0.85,
        "key_jaccard": 0.60,
    }
    return ConfigScore(**(base | over))


A = score(key_jaccard=None, quotes_verified=0.85)
B = score()


def test_three_passes_when_quotes_hold_and_critical_recall_grows_by_ten_points() -> None:
    c = score(critical_recall=1.0, recall=0.91, key_jaccard=0.62)

    d = decide(A, B, B, c)

    assert d.passes == 3 and d.chosen == "C"
    first = d.items[0]
    assert [ch.met for ch in first.checks] == [True, False, True, False]
    assert "+11,1 п. п." in first.checks[2].text


def test_one_pass_when_c_quotes_are_worse_even_with_gains() -> None:
    c = score(critical_recall=1.0, recall=0.99, key_jaccard=0.9, quotes_verified=0.96)

    d = decide(A, B, B, c)

    assert d.passes == 1 and d.chosen == "B"
    assert not d.items[0].checks[0].met
    assert "цитат у C ниже" in d.items[0].conclusion


def test_one_pass_when_every_gain_is_below_threshold() -> None:
    c = score(recall=0.90 + 6 / 135, critical_recall=8 / 9 + 0.05, key_jaccard=0.69)

    d = decide(A, B, B, c)

    assert d.passes == 1
    assert [ch.met for ch in d.items[0].checks] == [True, False, False, False]


def test_threshold_is_inclusive_despite_float_rounding() -> None:
    c = score(key_jaccard=0.7, recall=0.90)  # 0.7 − 0.6 = 0.0999…, порог «0,10 и больше»

    d = decide(A, score(key_jaccard=0.6), B, c)

    assert d.items[0].checks[1].met and d.passes == 3


def test_raw_input_only_when_blocks_lose_more_than_five_points() -> None:
    assert decide(A, B, score(recall=0.95), B).search_input is SearchInput.BLOCKS  # ровно 5 п. п.
    d = decide(A, B, score(recall=0.90 + 7 / 135 * 1.0), B)

    assert d.search_input is SearchInput.RAW and d.chosen == "B′"


def test_reopen_search_prompt_when_chosen_config_loses_to_baseline() -> None:
    a = score(recall=0.97, key_jaccard=None)

    d = decide(a, B, B, B)

    assert d.reopen_search_prompt and d.chosen == "B"
    assert "B − A = −7,0 п. п." in d.items[2].checks[0].text


def test_missing_metric_is_not_a_gain() -> None:
    c = score(key_jaccard=None, critical_recall=None, recall=None)

    d = decide(A, B, B, c)

    assert d.passes == 1
    assert all("нет данных" in ch.text for ch in d.items[0].checks[1:])


def test_raw_with_three_passes_notes_that_c_raw_was_not_measured() -> None:
    d = decide(A, B, score(recall=0.99), score(critical_recall=1.0))

    assert (d.passes, d.search_input, d.chosen) == (3, SearchInput.RAW, "C")
    assert d.notes and "не мерили" in d.notes[0]


def test_cli_builds_table_from_eval_results(tmp_path: Path, capsys: Any) -> None:
    src = str(BASELINE_RESULT)

    code = main(["--a", src, "--b", src, "--b-raw", src, "--c", src, "--out-dir", str(tmp_path)])

    assert code == 0
    [md] = tmp_path.glob("*-checkpoint.md")
    text = md.read_text(encoding="utf-8")
    assert "| Метрика | A | B | B′ | C |" in text
    assert "| Recall (found + partial) | 93,3 % | 93,3 % | 93,3 % | 93,3 % |" in text
    assert "Проходов по умолчанию (standard и submit): **1**" in text
    assert "D-011: проходов 1, вход поиска blocks" in capsys.readouterr().out
    assert list(tmp_path.glob("*-checkpoint.json"))
