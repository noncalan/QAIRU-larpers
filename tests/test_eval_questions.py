"""Eval вопросов автору (#41): метрики до и после критика по report.json, сводка, CLI без сети."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import report_factory as factory
from conftest import REQUIREMENTS
from evals.eval import JudgeMode, evaluate
from evals.questions import aggregate_questions, load_scores, main, render_questions, score_questions
from validator.config import load_config
from validator.models import Backend, DocKind, Report
from validator.pipeline import run as run_pipeline


def write_report(folder: Path, report: Report) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "report.json").write_text(report.model_dump_json(), encoding="utf-8")
    return folder


def test_before_counts_withdrawn_and_after_does_not() -> None:
    score = score_questions(factory.full_report(), "full")

    assert score is not None
    assert (score.before.total, score.after.total) == (4, 3)
    assert (score.after.before_critic, score.after.withdrawn, score.after.withdrawn_share) == (4, 1, 0.25)
    assert score.before.with_scenario == round(2 / 3, 4)  # снятый вопрос со сценарием считается «до»
    assert score.after.with_scenario == 0.5
    assert score.withdrawn and "«Бронь хранится 30 дней»" in score.withdrawn[0]


def test_report_without_check_has_no_question_score() -> None:
    assert score_questions(factory.unread_report(), "unread") is None


def test_aggregate_means_and_place_stability() -> None:
    full = score_questions(factory.full_report(), "a")
    clean = score_questions(factory.clean_report(), "b")
    assert full is not None and clean is not None
    left = full.model_copy(update={"places": ["B001", "B002", "B003"]})
    right = clean.model_copy(update={"places": ["B002", "B003", "B004"]})

    same = aggregate_questions([left, left])
    mixed = aggregate_questions([left, right])

    assert same is not None and mixed is not None
    assert same.place_jaccard == 1.0 and same.withdrawn_share == 0.25
    assert mixed.place_jaccard == 0.5
    assert (mixed.total_before, mixed.total_after) == (2.0, 1.5)
    single = aggregate_questions([left])
    assert single is not None and single.place_jaccard is None
    assert aggregate_questions([]) is None


def test_render_has_before_after_table_and_withdrawn_list() -> None:
    score = score_questions(factory.full_report(), "full")
    assert score is not None

    text = "\n".join(render_questions(aggregate_questions([score]), [score]))

    assert "| Метрика | До критика | После критика |" in text
    assert "| Снято самоответом (доля) | — | 25,0 % |" in text
    assert "Снятые вопросы:" in text and "`full`" in text


def test_cli_writes_results_without_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_1 = write_report(tmp_path / "run-1", factory.full_report())
    run_2 = write_report(tmp_path / "run-2", factory.clean_report())

    code = main([str(run_1), str(run_2), "--name", "toy", "--out-dir", str(tmp_path / "results")])

    assert code == 0
    [json_path] = (tmp_path / "results").glob("*-questions-toy.json")
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["config"]["runs"] == 2 and len(data["runs"]) == 2
    assert json_path.with_suffix(".md").is_file()
    assert "До критика" in capsys.readouterr().out


def test_cli_errors_are_messages(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([str(tmp_path / "нет"), "--name", "x"]) == 1
    assert main([str(tmp_path), "--name", "плохое имя"]) == 1
    assert "Ошибка eval" in capsys.readouterr().err


def test_main_eval_includes_question_metrics(tmp_path: Path) -> None:
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "run" / "report.md", backend=Backend.FAKE)
    run_pipeline(cfg)

    result = evaluate([tmp_path / "run"], name="q", judge_mode=JudgeMode.AUTO)

    [score] = load_scores([tmp_path / "run"])
    assert result.runs[0].questions == score
    assert result.config.questions is not None and result.config.questions.runs == 1
    assert score.after.reviewed == score.before.total
