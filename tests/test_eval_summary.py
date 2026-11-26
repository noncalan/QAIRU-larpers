"""Таблица «один промпт против агента» (#51): форматирование, разбор результатов, записанные прогоны."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from conftest import ROOT
from evals.chart import Bar, Group, render_svg
from evals.freetext import quote_segments
from evals.judge import EvalError
from evals.summary import (
    ConfigResult,
    app,
    chart_groups,
    load_config,
    num,
    pct,
    read_precision,
    spread,
    summary_table,
    unstable_gold,
)

RESULTS = ROOT / "evals" / "results"
BASELINE = RESULTS / "2026-11-26-baseline.json"
AGENT = RESULTS / "2026-11-26-checkpoint-b.json"
DOCUMENT = ROOT / "data" / "input" / "requirements.md"


def fake_config(key: str, gold_runs: list[dict[str, str]], **config: Any) -> ConfigResult:
    runs = [
        {"source": "agent", "findings": 10 + i, "gold": g, "calls": {"reused": 0, "wall_s": 100.0 + i}}
        for i, g in enumerate(gold_runs)
    ]
    base = {
        "quotes_final_verified": 1.0,
        "quotes_exact": 1.0,
        "noise_share": 0.0,
        "gold_jaccard": 0.9,
        "key_jaccard": 0.5,
        "critical_pass_all": 1.0,
        "critical_recall": 1.0,
        "critical_recall_strict": 1.0,
        "recall": 0.9,
        "recall_strict": 0.8,
        "cost_usd": 1.0,
    }
    return ConfigResult(
        key=key,
        title=f"Конфигурация ({key})",
        path=Path("x.json"),
        data={"config": base | config, "runs": runs, "settings": {}, "name": key},
        place_jaccard=0.75,
        critical_keys=(3, 4),
    )


def test_percent_and_number_use_russian_decimal_comma() -> None:
    assert pct(0.9333) == "93,3 %"
    assert num(0.5061, 3) == "0,506"
    assert pct(None) == "—"


def test_spread_shows_min_max_and_deviation() -> None:
    assert spread([46, 49, 50]) == "46–50, σ 1,7"


def test_unstable_gold_lists_defects_found_in_some_runs_only() -> None:
    cfg = fake_config(
        "B",
        [{"1": "found", "2": "missed", "3": "partial"}, {"1": "found", "2": "found", "3": "missed"}],
    )

    assert unstable_gold(cfg) == [2, 3]


def test_summary_table_has_one_cell_per_configuration_and_pending_precision() -> None:
    cfgs = [fake_config("A", [{"1": "found"}]), fake_config("B", [{"1": "found"}])]

    rows = summary_table(cfgs)

    assert all(r.count("|") == rows[0].count("|") for r in rows)
    assert any("Precision" in r and r.count("ждёт раунда #49") == 2 for r in rows)


def test_quote_segments_split_glued_quotes_and_drop_short_ones() -> None:
    assert quote_segments("первая часть цитаты … вторая часть цитаты … ок") == [
        "первая часть цитаты ",
        " вторая часть цитаты ",
    ]


def test_chart_has_no_overlapping_rows_and_marks_missing_value() -> None:
    svg = render_svg("Заголовок", "Подпись", [Group("Метрика", [Bar("A", 0.5), Bar("B", None)])])

    assert "50,0" in svg
    assert "нет" in svg
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


def test_load_config_rejects_file_that_is_not_an_eval_result(tmp_path: Path) -> None:
    broken = tmp_path / "x.json"
    broken.write_text(json.dumps({"hello": 1}), encoding="utf-8")

    with pytest.raises(EvalError, match=r"не результат evals\.eval"):
        load_config("A", "A", broken, DOCUMENT)


def test_recorded_runs_give_the_published_headline_numbers() -> None:
    a = load_config("A", "Один промпт (A)", BASELINE, DOCUMENT)
    b = load_config("B", "Агент, 1 проход (B)", AGENT, DOCUMENT)

    assert len(a.runs) == 3 and len(b.runs) == 3
    assert a.config["quotes_final_verified"] < 0.9 and b.config["quotes_final_verified"] == 1.0
    assert a.place_jaccard is not None and b.place_jaccard is not None
    assert a.critical_keys is None and b.critical_keys is not None
    stable, total = b.critical_keys
    assert 0 < stable <= total
    assert len(chart_groups([a, b])[0].bars) == 2


def test_cli_writes_stability_summary_and_chart(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "--a", str(BASELINE),
            "--b", str(AGENT),
            "--name", "t",
            "--results-dir", str(tmp_path / "r"),
            "--img-dir", str(tmp_path / "img"),
        ],
    )  # fmt: skip

    assert result.exit_code == 0, result.output
    assert (tmp_path / "r" / "summary.md").read_text(encoding="utf-8").startswith("# Один промпт")
    assert (tmp_path / "img" / "t.svg").is_file()
    assert any(p.name.endswith("-t.md") for p in (tmp_path / "r").iterdir())


def test_load_config_reports_missing_agent_report_without_traceback(tmp_path: Path) -> None:
    data = json.loads(AGENT.read_text(encoding="utf-8"))
    for run in data["runs"]:
        run["label"] = str(tmp_path / "no-such-run")
    broken = tmp_path / "agent.json"
    broken.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(EvalError):
        load_config("B", "B", broken, DOCUMENT)


def test_read_precision_takes_strict_and_soft_share_from_both_labelers() -> None:
    path = RESULTS / "2026-11-26-precision-pilot-e2e-md.json"

    cells = read_precision(path)

    assert set(cells) == {"agent", "baseline"}
    assert cells["baseline"].startswith("51,2 % / 97,7 %")


def test_read_precision_rejects_file_without_runs(tmp_path: Path) -> None:
    broken = tmp_path / "p.json"
    broken.write_text("{}", encoding="utf-8")

    with pytest.raises(EvalError, match=r"evals\.precision"):
        read_precision(broken)
