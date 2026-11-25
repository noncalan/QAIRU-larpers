"""Eval целиком без сети: smoke на прогоне агента с бэкендом fake, кеш вердиктов, ошибки по-русски."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import FIXTURES_LLM, ROOT
from evals.eval import EvalResult, JudgeFormat, JudgeMode, evaluate, main
from evals.findings import render_items
from evals.gold import read_gold_md
from evals.judge import EvalError, Judge, VerdictCache, judge_prompt, judge_request
from evals.runs import load_run
from validator.errors import LLM_UNAVAILABLE, LLMError
from validator.models import Backend, LLMRequest, LLMResponse

DOCUMENT = ROOT / "data" / "input" / "requirements.md"
BASELINE_RUN = ROOT / "tests" / "fixtures" / "eval" / "baseline-run"
TOY_VERDICT = {
    "gold": [{"id": 1, "status": "found", "finding_ids": [1]}],
    "extra": [{"finding_id": 2, "verdict": "noise"}],
}


def _python(*args: str | Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "VALIDATOR_FAKE_FIXTURES": str(FIXTURES_LLM), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
        timeout=180,
        check=False,
    )


def test_eval_smoke_on_fake_agent_run_needs_no_network(tmp_path: Path) -> None:
    run_dir = tmp_path / "out"
    agent = _python("-m", "validator", DOCUMENT, run_dir / "report.md", "--backend", "fake")
    assert agent.returncode == 0, agent.stderr

    result = _python("-m", "evals.eval", run_dir, "--name", "smoke", "--out-dir", tmp_path / "results")

    assert result.returncode == 0, result.stderr
    [json_path] = (tmp_path / "results").glob("*-smoke.json")
    report = EvalResult.model_validate_json(json_path.read_text(encoding="utf-8"))
    [run] = report.runs
    assert run.judge.source == "fake"
    assert run.findings > 0
    assert 0 < report.config.recall <= 1  # type: ignore[operator]
    assert run.quotes_final is not None and run.quotes_final.share_verified == 1.0
    assert run.keys
    md = json_path.with_suffix(".md").read_text(encoding="utf-8")
    assert "## Сводка" in md and "## Матрица: дефект эталона × прогон" in md
    assert "recall" in result.stdout


class StubCLI:
    """Клиент как подписка: отвечает заданным вердиктом и считает вызовы."""

    backend = Backend.CLI

    def __init__(self) -> None:
        self.calls = 0

    def call(self, req: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(call_id=req.call_id, structured=TOY_VERDICT, backend=Backend.CLI, cost_usd=0.5)


def test_live_verdict_is_cached_and_reused(tmp_path: Path) -> None:
    client = StubCLI()
    judge = Judge(lambda: client, VerdictCache(tmp_path))

    first, call1 = judge.ask("запрос", 1, {"run": "x"})
    second, call2 = judge.ask("запрос", 1, {"run": "x"})

    assert first == second == TOY_VERDICT
    assert client.calls == 1
    assert (call1.source, call2.source) == ("cli", "cache")
    assert call2.cost_usd == 0.5
    record = json.loads((tmp_path / f"{call1.key}.json").read_text(encoding="utf-8"))
    assert record["model"] == "claude-opus-5-5"
    assert "ЭТАЛОН" not in json.dumps(record, ensure_ascii=False)  # в кеше нет ни эталона, ни запроса


def test_cache_mode_reads_saved_verdict_for_the_exact_input(tmp_path: Path) -> None:
    run = load_run(BASELINE_RUN, DOCUMENT)
    prompt = judge_prompt(DOCUMENT.read_text(encoding="utf-8"), read_gold_md(), render_items(run.items))
    req = judge_request(prompt, 1)
    VerdictCache(tmp_path).put(
        req, LLMResponse(call_id=req.call_id, structured=TOY_VERDICT, backend=Backend.CLI), {"run": run.label}
    )

    result = evaluate([BASELINE_RUN], name="cached", judge_mode=JudgeMode.CACHE, cache_dir=tmp_path)

    [score] = result.runs
    assert score.judge.source == "cache"
    assert score.recall == pytest.approx(1 / 45)
    assert score.extra_noise == 1


def test_cache_mode_without_verdict_fails_with_clear_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([str(BASELINE_RUN), "--name", "x", "--judge", "cache", "--cache-dir", str(tmp_path)])

    assert code == 1
    assert "Нет вердикта судьи в кеше" in capsys.readouterr().err


def test_corrupted_cache_file_is_an_error(tmp_path: Path) -> None:
    req = judge_request("запрос", 1)
    (tmp_path / f"{req.key}.json").write_text("{не json", encoding="utf-8")

    with pytest.raises(EvalError, match="Кеш судьи повреждён"):
        VerdictCache(tmp_path).get(req)


def test_missing_claude_becomes_eval_error(tmp_path: Path) -> None:
    def no_claude() -> StubCLI:
        raise LLMError("Команда claude не найдена", code=LLM_UNAVAILABLE, hint="Установите Claude Code")

    judge = Judge(no_claude, VerdictCache(tmp_path))

    with pytest.raises(EvalError, match="Судья недоступен"):
        judge.ask("запрос", 1, {})


def test_bad_name_and_missing_run_are_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(EvalError):
        evaluate([BASELINE_RUN], name="плохое имя")

    code = main([str(tmp_path / "nope"), "--name", "x", "--out-dir", str(tmp_path)])

    assert code == 1
    assert "Прогон не найден" in capsys.readouterr().err


RECORDED = ROOT / "tests" / "fixtures" / "eval"


@pytest.mark.parametrize(
    ("fixtures", "fmt", "recall", "strict"),
    [
        ("llm", JudgeFormat.UNIFORM, 43 / 45, 40 / 45),
        ("llm-research21", JudgeFormat.RESEARCH21, 42 / 45, 39 / 45),
    ],
    ids=["uniform", "research21"],
)
def test_recorded_live_judge_gives_recorded_numbers(
    fixtures: str, fmt: JudgeFormat, recall: float, strict: float
) -> None:
    result = evaluate(
        [BASELINE_RUN],
        name="recorded",
        judge_mode=JudgeMode.FAKE,
        judge_format=fmt,
        fixtures=RECORDED / fixtures,
    )

    [run] = result.runs
    assert run.recall == pytest.approx(recall)
    assert run.recall_strict == pytest.approx(strict)
    assert run.critical_recall == 1.0
    assert run.findings == 45
    assert (run.extra_real, run.extra_noise) == (3, 0)
    assert not run.warnings
