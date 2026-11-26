"""Проверка входа судей, расчёта совпадений и повторного прогона из кеша без вызова модели."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from conftest import ROOT
from evals.eval import ASTANA
from evals.gold import sha256_text
from evals.judge import EvalError, display_path, verdict_key
from evals.kickoff import (
    KICKOFF_PROMPT,
    KICKOFF_SCHEMA,
    KICKOFF_SYSTEM,
    Category,
    KickoffResult,
    Match,
    MatchLabel,
    Mode,
    agreement,
    main,
    make_slice,
    match_questions,
    parse_matches,
    parse_questions,
    reverse_coverage,
    write_result,
)
from evals.precision_labeler import LABEL_TIMEOUT_S, Labeler
from validator.models import Backend, Effort, LLMRequest, LLMResponse, LLMStep, Report

QUESTIONS = ROOT / "evals/holdout/kickoff_questions.md"
RUN = ROOT / "tests/fixtures/e2e/md"
DOCUMENT = ROOT / "data/input/requirements.md"
ROW = "| K-01 | 10:00 | требование | Первый вопрос? | 1 | Как сказали? | Уточнение |"


def test_real_questions_have_expected_ids_and_categories() -> None:
    questions = parse_questions(QUESTIONS.read_text(encoding="utf-8"))

    assert [q.id for q in questions] == [f"K-{n:02d}" for n in range(1, 14)]
    assert Counter(q.category for q in questions) == {Category.REQUIREMENT: 10, Category.IMPLEMENTATION: 3}
    assert questions[0].time == "08:46"
    assert questions[0].note == ""


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (f"{ROW}\n{ROW}", "дважды"),
        (ROW.replace("требование", "другое"), "неизвестная категория"),
        (ROW.replace("K-01", "K-001"), "нет вопросов"),
        (ROW.replace("| Как сказали?", ""), "столбцов"),
        (ROW.replace("Первый вопрос?", ""), "пустой вопрос"),
    ],
)
def test_invalid_question_file_fails_before_judging(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "questions.md"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(EvalError, match=message):
        match_questions(path, RUN, DOCUMENT, mode=Mode.CACHE, cache_dir=tmp_path)


def test_run_without_final_questions_is_rejected(tmp_path: Path) -> None:
    report = json.loads((RUN / "report.json").read_text(encoding="utf-8"))
    report["questions"] = []
    (tmp_path / "report.json").write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(EvalError, match="нет вопросов"):
        match_questions(QUESTIONS, tmp_path, DOCUMENT, mode=Mode.CACHE, cache_dir=tmp_path)


@pytest.mark.parametrize("response", [None, [], {}, {"matches": {}}])
def test_response_without_matches_list_is_rejected(response: object) -> None:
    with pytest.raises(EvalError, match="нет списка matches"):
        parse_matches(response, [], [], [], "судья")


def test_parse_matches_warns_and_keeps_last_valid_label() -> None:
    response = {
        "matches": [
            None,
            {"id": "K-99"},
            {"id": "K-03", "label": "maybe"},
            {"id": "K-01", "label": "match", "b_ids": ["Q-99", "Q-01"], "reason": "раньше"},
            {"id": "K-01", "label": "none", "b_ids": ["Q-01"], "reason": "  тема\n другая  "},
            {"id": "K-02", "label": "partial", "b_ids": ["Q-77"], "reason": "я" * 500},
            {"id": "K-04", "label": "match", "b_ids": ["Q-01", "Q-99"]},
        ]
    }
    warnings: list[str] = []

    marks = parse_matches(response, ["K-01", "K-02", "K-03", "K-04"], ["Q-01"], warnings, "Opus")

    assert marks == {
        "K-01": Match(label=MatchLabel.NONE, b_ids=[], reason="тема другая"),
        "K-02": Match(label=MatchLabel.PARTIAL, b_ids=[], reason="я" * 400),
        "K-04": Match(label=MatchLabel.MATCH, b_ids=["Q-01"], reason=""),
    }
    for fragment in [
        "не объект",
        "K-99",
        "maybe",
        "Q-99",
        "дважды",
        "отброшены",
        "без ссылок",
        "без метки K-03, в метрики не идут",
    ]:
        assert any(fragment in warning for warning in warnings)


def test_metrics_use_each_judges_denominator_and_only_shared_pairs_for_agreement() -> None:
    match, partial, none = MatchLabel
    ids = list("1234567")

    def marks(keys: str, values: list[MatchLabel], ref: str) -> dict[str, Match]:
        return {
            k: Match(label=v, b_ids=[] if v is none else ["Q-1", ref], reason="")
            for k, v in zip(keys, values, strict=True)
        }

    labels = {
        Labeler.OPUS: marks("123456", [match, match, partial, none, partial, none], "Q-2"),
        Labeler.FABLE: marks("123457", [match, partial, none, none, partial, partial], "Q-3"),
    }

    metrics = make_slice(ids, labels)
    agreed = agreement(ids, labels)
    reverse = reverse_coverage(["Q-1", "Q-2", "Q-3", "Q-4"], labels)

    opus, fable = metrics.per_judge.values()
    assert (opus.n_labeled, opus.strict, opus.soft) == (6, 2 / 6, 4 / 6)
    assert (fable.n_labeled, fable.strict, fable.soft) == (6, 1 / 6, 4 / 6)
    both = metrics.consensus
    assert both.counts == {match: 1, partial: 2, none: 2}
    assert (both.n_labeled, both.strict, both.soft) == (5, 1 / 5, 3 / 5)
    assert (agreed.n_both, agreed.share) == (5, 3 / 5)
    assert agreed.kappa3 == pytest.approx(7 / 17)
    assert agreed.kappa2 == pytest.approx(6 / 11)
    assert {lab: cov.ids for lab, cov in reverse.per_judge.items()} == {
        Labeler.OPUS: ["Q-1", "Q-2"],
        Labeler.FABLE: ["Q-1", "Q-3"],
    }
    assert all(cov.count == 2 and cov.share == 1 / 2 for cov in reverse.per_judge.values())
    assert reverse.both and (reverse.both.ids, reverse.both.count, reverse.both.share) == (["Q-1"], 1, 1 / 4)


def test_empty_and_unpaired_slices_have_undefined_rates() -> None:
    labels: dict[Labeler, dict[str, Match]] = {Labeler.OPUS: {}}

    metrics = make_slice(["K-01"], labels)
    agreed = agreement(["K-01"], labels)

    for score in (metrics.per_judge[Labeler.OPUS], metrics.consensus):
        assert score.n_labeled == 0 and score.strict is None and score.soft is None
    assert agreed.n_both == 0 and agreed.share is None and agreed.kappa3 is None and agreed.kappa2 is None
    assert reverse_coverage(["Q-1"], labels).both is None


class StubLabeler:
    """Запоминает запрос и возвращает ответ, когда обе модели получили свои запросы."""

    def __init__(self, barrier: Barrier) -> None:
        self.backend = Backend.CLI
        self.barrier = barrier
        self.requests: list[LLMRequest] = []

    def call(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        self.barrier.wait(timeout=10)
        _, kickoff, agent = _blocks(req)
        label = "match" if req.model == Labeler.OPUS.model else "partial"
        matches = [
            {"id": q["id"], "label": label, "b_ids": [agent[0]["id"]], "reason": "тема | аспект"}
            for q in kickoff[:-1]
        ]
        return LLMResponse(
            call_id=req.call_id,
            backend=self.backend,
            structured={"matches": matches},
            cost_usd=0.01,
            model=req.model,
        )


def _blocks(req: LLMRequest) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    prefix, lists = req.prompt.split("\n\n=== СПИСОК A ===\n")
    kickoff, agent = lists.split("\n\n=== СПИСОК B ===\n")
    return prefix, json.loads(kickoff), json.loads(agent)


@pytest.fixture
def judged(tmp_path: Path) -> tuple[KickoffResult, dict[Labeler, StubLabeler]]:
    barrier = Barrier(len(Labeler))
    stubs = {lab: StubLabeler(barrier) for lab in Labeler}
    result = match_questions(
        QUESTIONS,
        RUN,
        DOCUMENT,
        cache_dir=tmp_path,
        client_factory=stubs.__getitem__,
        now=datetime(2026, 11, 26, tzinfo=UTC),
    )
    return result, stubs


def test_judges_receive_identical_blinded_inputs(
    judged: tuple[KickoffResult, dict[Labeler, StubLabeler]],
) -> None:
    result, stubs = judged
    report = Report.model_validate_json((RUN / "report.json").read_text(encoding="utf-8"))
    document = DOCUMENT.read_text(encoding="utf-8")
    questions_text = QUESTIONS.read_text(encoding="utf-8")
    questions = parse_questions(questions_text)
    opus, fable = (stub.requests[0] for stub in stubs.values())

    assert all(len(stub.requests) == 1 for stub in stubs.values())
    assert opus.prompt == fable.prompt
    assert opus.system == fable.system == KICKOFF_SYSTEM
    assert (opus.model, fable.model) == (Labeler.OPUS.model, Labeler.FABLE.model)
    assert opus.effort == fable.effort == Effort.HIGH
    for req in (opus, fable):
        assert (req.step, req.call_index, req.timeout_s, req.json_schema) == (
            LLMStep.JUDGE,
            1,
            LABEL_TIMEOUT_S,
            KICKOFF_SCHEMA,
        )
    prefix, kickoff, agent = _blocks(opus)
    assert prefix == KICKOFF_PROMPT + "\n\n=== ТЗ ===\n" + document
    assert kickoff == [{"id": q.id, "question": q.question} for q in questions]
    assert all(private not in opus.prompt for q in questions for private in (q.transcript, q.note) if private)
    assert agent == [
        {
            "id": q.id,
            "question": q.question,
            "why": q.why,
            "where": [s.text for s in q.quotes],
            "options": [o.text for o in q.options],
        }
        for q in report.questions
    ]
    assert result.settings.n_kickoff == 13
    assert result.settings.n_agent == len(report.questions)
    assert result.settings.document_sha256 == sha256_text(document)
    assert result.settings.questions_sha256 == sha256_text(questions_text)
    assert result.settings.run == display_path(RUN)
    assert result.settings.agent_version == report.passport.agent_version
    assert result.created_at == datetime(2026, 11, 26, tzinfo=UTC).astimezone(ASTANA)
    assert result.kickoff_questions == questions
    assert {c: s.consensus.n_labeled for c, s in result.by_category.items()} == {
        Category.REQUIREMENT: 9,
        Category.IMPLEMENTATION: 3,
    }


def test_cached_answers_reproduce_labels_without_a_client(
    judged: tuple[KickoffResult, dict[Labeler, StubLabeler]],
    tmp_path: Path,
) -> None:
    first, stubs = judged

    replay = match_questions(QUESTIONS, RUN, DOCUMENT, mode=Mode.CACHE, cache_dir=tmp_path)

    assert first.labels == replay.labels and first.agreement == replay.agreement
    assert [c.source for c in first.calls] == ["cli", "cli"]
    assert [c.source for c in replay.calls] == ["cache", "cache"]
    for call in replay.calls:
        [req] = stubs[call.labeler].requests
        assert call.key == verdict_key(req)
        record = json.loads((tmp_path / f"{call.key}.json").read_text(encoding="utf-8"))
        assert record["meta"] == {
            "labeler": call.labeler.value,
            "kickoff": 13,
            "agent": first.settings.n_agent,
        }


def test_cache_miss_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(EvalError, match="в кеше"):
        match_questions(QUESTIONS, RUN, DOCUMENT, mode=Mode.CACHE, cache_dir=tmp_path)

    code = main(
        [
            "--mode",
            "cache",
            "--questions",
            str(QUESTIONS),
            "--document",
            str(DOCUMENT),
            "--run",
            str(RUN),
            "--cache-dir",
            str(tmp_path),
        ]
    )

    assert code == 1 and "Ошибка kickoff:" in capsys.readouterr().err


def test_cli_replays_selected_judge_and_prints_summary_and_output_paths(
    judged: tuple[KickoffResult, dict[Labeler, StubLabeler]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first, _ = judged
    stem = tmp_path / "single"

    code = main(
        [
            "--questions",
            str(QUESTIONS),
            "--run",
            str(RUN),
            "--document",
            str(DOCUMENT),
            "--mode",
            "cache",
            "--cache-dir",
            str(tmp_path),
            "--labelers",
            "fable",
            "--out",
            str(stem),
        ]
    )

    assert code == 0
    summary, paths = capsys.readouterr().out.splitlines()
    assert "Fable 5.2 0,0 % / 100,0 %" in summary and "согласие —" in summary
    assert all(display_path(stem.with_suffix(ext)) in paths for ext in (".json", ".md"))
    result = KickoffResult.model_validate_json(stem.with_suffix(".json").read_text(encoding="utf-8"))
    assert result.labels == {Labeler.FABLE: first.labels[Labeler.FABLE]}
    assert result.notes and result.reverse_coverage.both is None


def test_report_overwrites_fixed_paths_and_lists_questions_and_references(
    judged: tuple[KickoffResult, dict[Labeler, StubLabeler]],
    tmp_path: Path,
) -> None:
    result, _ = judged
    stem = tmp_path / "results" / "kickoff.v1"

    paths = write_result(result, stem)
    assert write_result(result, stem) == paths
    json_path, md_path = paths
    markdown = md_path.read_text(encoding="utf-8")

    assert json_path.name == "kickoff.v1.json" and md_path.name == "kickoff.v1.md"
    assert json_path.read_text(encoding="utf-8") == result.model_dump_json(indent=1) + "\n"
    assert "| Судья | Размечено | Совпало | Частично | Нет |" in markdown
    assert all(f"| {q.id} |" in markdown for q in result.kickoff_questions)
    assert markdown.count("| Q-01 |") == 3
    assert result.agent_questions[0].question in markdown
    assert r"тема \| аспект" in markdown
    assert "сопоставление моделями, не людьми" in markdown
    assert "## Предупреждения" in markdown and "100,0 %" in markdown
