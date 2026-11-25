"""Судья и подсчёт совпадают с исследованием 21 (``experiments/21-model-effort``).

Иначе цифры eval несравнимы с прошлыми замерами и базовой линией (issue #24, «Чего не делать»):
промпт, схема и запрос судьи те же, recall и шум на том же вердикте те же, что у ``score.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from conftest import ROOT
from evals import judge as our
from evals.findings import JudgeItem, render_items
from evals.gold import GOLD_TOTAL, load_gold, read_gold_md
from evals.judge import JudgeCall, parse_verdict
from evals.metrics import score_run
from evals.runs import CallStats, QuoteStats, RunData, Source

EXPERIMENT = ROOT / "experiments" / "21-model-effort"
DOCUMENT = ROOT / "data" / "input" / "requirements.md"

pytestmark = pytest.mark.skipif(not (EXPERIMENT / "run_matrix.py").is_file(), reason="нет эксперимента 21")


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def run_matrix() -> ModuleType:
    return _load("exp21_run_matrix", EXPERIMENT / "run_matrix.py")


@pytest.fixture(scope="module")
def score() -> ModuleType:
    return _load("exp21_score", EXPERIMENT / "score.py")


def _capture(run_matrix: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_call(name: str, model: str, effort: str, system: str, text: str, schema: Any) -> dict[str, Any]:
        calls.append(
            {"name": name, "model": model, "effort": effort, "system": system, "text": text, "schema": schema}
        )
        return {"returncode": 0, "wall_s": 0.0}

    monkeypatch.setattr(run_matrix, "call", fake_call)
    return calls


def test_judge_prompt_schema_and_effort_are_verbatim(run_matrix: ModuleType) -> None:
    assert our.JUDGE_SYSTEM == run_matrix.JUDGE_SYSTEM
    assert our.JUDGE_PROMPT == run_matrix.JUDGE_PROMPT
    assert our.JUDGE_SCHEMA == run_matrix.JUDGE_SCHEMA
    assert our.JUDGE_EFFORT.value == "high"
    assert our.JUDGE_MODEL == "claude-opus-5-5"  # в исследовании 21 алиас «opus» = Opus 5.5


FINDINGS = [
    {
        "quote": "Месяц определяется по дате",
        "property": "Согласованность",
        "explanation": "а",
        "severity": "критично",
    },
    {"quote": "заполняются тестировщиками", "property": "Полнота", "explanation": "б", "severity": "низко"},
]


def test_structured_findings_go_to_judge_exactly_as_in_research_21(
    run_matrix: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture(run_matrix, monkeypatch)
    run_matrix.judge("opus-medium-1", {"parsed": {"structured_output": {"findings": FINDINGS}}})
    items = [
        JudgeItem(id=n, quote=f["quote"], property=f["property"], explanation=f["explanation"])
        for n, f in enumerate(FINDINGS, start=1)
    ]

    prompt = our.judge_prompt(DOCUMENT.read_text(encoding="utf-8"), read_gold_md(), render_items(items))

    assert prompt == calls[0]["text"]
    assert our.judge_request(prompt).system == calls[0]["system"]
    assert our.judge_request(prompt).json_schema == calls[0]["schema"]


def test_free_text_research21_block_is_exactly_as_in_research_21(
    run_matrix: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _capture(run_matrix, monkeypatch)
    answer = "## Дефекты\n\n1. «Месяц определяется по дате» — противоречие."
    run_matrix.judge("free-opus-medium-1", {"parsed": {"result": answer}})

    prompt = our.judge_prompt(
        DOCUMENT.read_text(encoding="utf-8"), read_gold_md(), our.FREE_TEXT_PREAMBLE + answer
    )

    assert prompt == calls[0]["text"]


def _judge_output() -> dict[str, Any]:
    """Непротиворечивый вердикт: находка 1 в эталоне, находка 2 вне его.

    На противоречивом вердикте (одна находка и в эталоне, и вне его) мы, в отличие от ``score.py``,
    не считаем её во «вне эталона» и пишем предупреждение; в записанных вердиктах такого нет.
    """
    statuses = ["found", "partial", "missed"]
    gold = [
        {"id": g, "status": statuses[g % 3], "finding_ids": [] if g % 3 == 2 else [1]}
        for g in range(1, GOLD_TOTAL + 1)
    ]
    return {"gold": gold, "extra": [{"finding_id": 2, "verdict": "noise"}]}


def test_metrics_equal_score_py_on_the_same_verdict(
    score: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    judged = _judge_output()
    usage = {"output_tokens": 1, "output_tokens_details": {"thinking_tokens": 0}}
    search = {"wall_s": 1.0, "parsed": {"structured_output": {"findings": FINDINGS}, "usage": usage}}
    search["parsed"]["total_cost_usd"] = 0.1  # type: ignore[index]
    (tmp_path / "search-x.json").write_text(json.dumps(search, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "judge-x.json").write_text(
        json.dumps({"parsed": {"structured_output": judged}}), encoding="utf-8"
    )
    monkeypatch.setattr(score, "OUT", tmp_path)
    theirs = score.run_metrics("x", score.gold_severity())
    items = [
        JudgeItem(id=n, quote=f["quote"], property="p", explanation="e") for n, f in enumerate(FINDINGS, 1)
    ]
    run = RunData(label="x", source=Source.AGENT, items=items, quotes=QuoteStats(), calls=CallStats())

    ours = score_run(
        run,
        parse_verdict(judged, load_gold().ids, 2),
        load_gold(),
        JudgeCall(key="k", source="fake"),
        findings=2,
    )

    assert ours.recall == pytest.approx(theirs["recall"])
    assert ours.recall_strict == pytest.approx(theirs["recall_strict"])
    assert ours.critical_recall == pytest.approx(theirs["crit_recall"])
    assert ours.critical_recall_strict == pytest.approx(theirs["crit_recall_strict"])
    assert (ours.extra_real, ours.extra_noise) == (theirs["extra_real"], theirs["extra_noise"])
    assert ours.findings == theirs["findings"]
    assert sorted(ours.ids_with(our.GoldStatus.FOUND)) == theirs["found"]
    assert sorted(ours.ids_with(our.GoldStatus.PARTIAL)) == theirs["partial"]


def test_critical_ids_equal_score_py(score: ModuleType) -> None:
    severity = score.gold_severity()

    assert [i for i, v in severity.items() if v.startswith("крит")] == load_gold().critical_ids
