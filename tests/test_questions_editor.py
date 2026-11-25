"""Шаг 8, редактор вопросов: промпт, разбор ответа «рассуждение, затем JSON», живой ответ, конвейер."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from conftest import FIXTURES_LLM, REQUIREMENTS
from questions_helpers import library_findings, make_cfg, make_doc, verification
from validator.config import load_config
from validator.errors import LLM_BAD_RESPONSE, LLM_QUESTIONS_FAILED, LLMError
from validator.evidence import evidence
from validator.ingest import parse
from validator.lint import lint
from validator.llm import FakeLLM
from validator.merge import merge
from validator.models import Backend, DocKind, Effort, ExitCode, LLMResponse, LLMStep, Report
from validator.pipeline import run as run_pipeline
from validator.questions import build_questions
from validator.questions.candidates import collect_candidates
from validator.questions.editor import (
    SYSTEM_PROMPT,
    EditorReplySchema,
    build_request,
    extract_json,
    normalize_candidate_id,
    parse_reply,
)
from validator.search import search
from validator.segment import segment
from validator.verify import verify

RECORDED = FIXTURES_LLM / "questions" / "1.json"


# ---------------------------------------------------------------- промпт


def test_prompt_example_matches_reply_schema() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")

    example = extract_json(text)

    assert example is not None
    reply = EditorReplySchema.model_validate(example)
    assert reply.questions and all(2 <= len(c.options) <= 4 for c in reply.questions)


def test_prompt_does_not_ask_model_for_priority_or_id() -> None:
    example = extract_json(SYSTEM_PROMPT.read_text(encoding="utf-8"))
    assert example is not None

    keys = {k for card in example["questions"] for k in card}

    assert not keys & {"id", "priority", "blocks"}


def test_request_has_document_candidates_and_step_model(tmp_path: Path) -> None:
    doc = make_doc(tmp_path)
    cands = collect_candidates(verification(library_findings(doc)))
    cfg = make_cfg(doc, tmp_path)

    req = build_request(cands, doc, {}, cfg)

    assert req.step is LLMStep.QUESTIONS and req.call_index == 1
    assert (req.model, req.effort) == ("claude-opus-5-5", Effort.HIGH)
    assert req.json_schema is None  # рассуждение текстом, затем JSON: строгая схема режет рассуждение
    assert "[B002]" in req.prompt and "### C-01" in req.prompt and "### C-03" in req.prompt
    assert "{document}" not in req.prompt and "{candidates}" not in req.prompt
    assert "Предварительный приоритет: P0" in req.prompt
    assert "F-04" not in req.prompt  # однозначная правка не кандидат


def test_document_text_with_placeholder_is_not_substituted(tmp_path: Path) -> None:
    doc = make_doc(tmp_path, "# Шаблон\n\nПоле {candidates} заполняет оператор вручную.\n")
    cands = collect_candidates(verification(library_findings(make_doc(tmp_path))))

    req = build_request(cands, doc, {}, make_cfg(doc, tmp_path))

    assert "Поле {candidates} заполняет" in req.prompt


# ---------------------------------------------------------------- разбор ответа


def test_extract_json_takes_last_block_with_questions() -> None:
    text = (
        '## Рассуждение\n\nПример: ```json\n{"x": 1}\n```\n\n'
        '```json\n{"questions": [], "dropped": []}\n```\n\n'
        '## Ответ\n\n```json\n{"questions": [{"question": "Итог?"}]}\n```\n'
    )

    data = extract_json(text)

    assert data == {"questions": [{"question": "Итог?"}]}


def test_extract_json_without_fence() -> None:
    text = 'Рассуждение {скобки в тексте}.\n\n{"questions": [{"question": "Итог?"}], "dropped": []}\nКонец.'

    assert extract_json(text) == {"questions": [{"question": "Итог?"}], "dropped": []}


def test_reply_without_json_is_llm_error() -> None:
    resp = LLMResponse(call_id="questions.1", text="Только рассуждение, без итога.", backend=Backend.FAKE)

    with pytest.raises(LLMError) as err:
        parse_reply(resp)

    assert err.value.code == LLM_BAD_RESPONSE and err.value.location == "questions.1"


def test_one_bad_card_does_not_spoil_the_reply() -> None:
    body = {"questions": [{"question": "Годный?"}, {"candidates": ["C-01"]}, "мусор"], "dropped": [{"x": 1}]}
    text = f"```json\n{json.dumps(body, ensure_ascii=False)}\n```"

    reply = parse_reply(LLMResponse(call_id="questions.1", text=text, backend=Backend.FAKE))

    assert [c.question for c in reply.cards] == ["Годный?"]
    assert reply.invalid == 2 and reply.dropped == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("C-01", "C-01"), ("c1", "C-01"), ("C 12", "C-12"), ("C-007", "C-07"), ("F-01", None), ("", None)],
)
def test_candidate_ids_are_normalized(raw: str, expected: str | None) -> None:
    assert normalize_candidate_id(raw) == expected


# ---------------------------------------------------------------- записанный живой ответ на requirements.md


def test_recorded_editor_reply_gives_checked_cards() -> None:
    """Живой ответ Opus 5.5 high на кандидатах из фикстур поиска (experiments/20-questions-editor)."""
    doc = parse(REQUIREMENTS[DocKind.MD], DocKind.MD)
    cfg = load_config(REQUIREMENTS[DocKind.MD], Path("out/report.md"), backend=Backend.FAKE)
    fake = FakeLLM(FIXTURES_LLM)
    seg = segment(doc, fake, cfg)
    merged = evidence(merge(search(doc, fake, cfg), lint(doc, seg), doc), doc, fake, cfg)
    ver = verify(merged, doc, seg)

    outcome = build_questions(ver, doc, seg, fake, cfg)

    assert outcome.edited
    assert outcome.questions
    assert [q.id for q in outcome.questions] == [f"Q-{n:02d}" for n in range(1, len(outcome.questions) + 1)]
    ranks = [q.priority.value for q in outcome.questions]
    assert ranks == sorted(ranks)
    known = {f.id for f in ver.findings}
    for q in outcome.questions:
        assert q.quotes and all(x.located and x.located in doc.text for x in q.quotes)
        assert 2 <= len(q.options) <= 4 and all(o.change for o in q.options)
        assert q.related_findings and set(q.related_findings) <= known
        assert len(re.findall(r"\w+", q.question)) <= 40  # промпт просит до 35 слов


def test_recorded_reply_is_raw_claude_output() -> None:
    payload = json.loads(RECORDED.read_text(encoding="utf-8"))

    assert payload["subtype"] == "success" and not payload["is_error"]
    assert "## Рассуждение" in payload["result"] and "```json" in payload["result"]
    assert "claude-opus-5-5" in payload["modelUsage"]


# ---------------------------------------------------------------- конвейер


def test_pipeline_survives_editor_failure(tmp_path: Path) -> None:
    fixtures = tmp_path / "fixtures"
    shutil.copytree(FIXTURES_LLM / "search", fixtures / "search")
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "out" / "report.md", backend=Backend.FAKE)

    outcome = run_pipeline(cfg.model_copy(update={"fake_fixtures": fixtures}))

    assert outcome.exit_code is ExitCode.OK
    report = Report.model_validate_json(cfg.report_json_path.read_text(encoding="utf-8"))
    assert report.questions
    assert LLM_QUESTIONS_FAILED in [d.code for d in report.diagnostics]
    text = cfg.report_md_path.read_text(encoding="utf-8")
    assert "### Q-01" in text and LLM_QUESTIONS_FAILED in text
    assert cfg.questions_path.is_file()


def test_report_marks_options_for_discussion(tmp_path: Path) -> None:
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "out" / "report.md", backend=Backend.FAKE)

    run_pipeline(cfg)

    report = Report.model_validate_json(cfg.report_json_path.read_text(encoding="utf-8"))
    ungrounded = [o for q in report.questions for o in q.options if o.grounded_in is None]
    text = cfg.report_md_path.read_text(encoding="utf-8")
    assert text.count("_(вариант для обсуждения)_") == len(ungrounded)
