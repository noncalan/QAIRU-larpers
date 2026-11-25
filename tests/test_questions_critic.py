"""Шаг 8г, критик-самоответ (#41): снятие вопросов только по сверенной цитате, отказ модели, метрики."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import FIXTURES_LLM, REQUIREMENTS
from questions_helpers import (
    CATALOG,
    EXTEND,
    FINE,
    QUEUE,
    TERM,
    card,
    cli_payload,
    critic_text,
    editor_llm,
    editor_text,
    library_findings,
    make_cfg,
    make_doc,
    review,
    segmentation,
    verification,
)
from validator.config import load_config
from validator.errors import LLM_BAD_RESPONSE, LLM_QUESTIONS_CRITIC_FAILED, LLM_QUESTIONS_WITHDRAWN, LLMError
from validator.models import (
    Addressee,
    Backend,
    DiagLevel,
    DocKind,
    LLMResponse,
    LLMStep,
    ParsedDocument,
    Question,
    QuestionOption,
    QuestionPriority,
    QuestionReview,
    Report,
    WithdrawnQuestion,
)
from validator.pipeline import run as run_pipeline
from validator.questions import QuestionsOutcome, build_questions, question_metrics
from validator.questions.critic import (
    FORMULATION_ERRORS,
    SYSTEM_PROMPT,
    CriticReplySchema,
    CriticReview,
    apply_reviews,
    build_request,
    normalize_question_id,
    parse_reply,
)
from validator.questions.metrics import is_closed
from validator.text import locate_quote

RECORDED = FIXTURES_LLM / "questions_critic" / "1.json"
NOT_IN_DOC = "Продление разрешено не более трёх раз подряд."


@pytest.fixture
def doc(tmp_path: Path) -> ParsedDocument:
    return make_doc(tmp_path)


def three_cards() -> str:
    return editor_text(
        [
            card(["C-01"], "Книгу вернули на пятнадцатый день: штраф есть?", quotes=[TERM]),
            card(["C-02"], "Продление заняло десять секунд: это быстро?", quotes=[EXTEND]),
            card(["C-03"], "Штраф за выходной день тоже начисляется?", quotes=[FINE]),
        ]
    )


def run(doc: ParsedDocument, tmp_path: Path, critic: str) -> QuestionsOutcome:
    llm = editor_llm(three_cards(), critic=critic)
    return build_questions(
        verification(library_findings(doc)), doc, segmentation(doc), llm, make_cfg(doc, tmp_path)
    )


def question(doc: ParsedDocument, qid: str, text: str, quote: str) -> Question:
    return Question(
        id=qid,
        priority=QuestionPriority.P1,
        addressee=[Addressee.BUSINESS],
        quotes=[locate_quote(doc, quote)],
        question=text,
        options=[
            QuestionOption(label="A", text="Первое", change="Поправить первое место"),
            QuestionOption(label="B", text="Второе", change="Поправить второе место"),
        ],
        related_findings=["F-01"],
    )


# ---------------------------------------------------------------- решение кода


def test_question_answered_by_document_is_withdrawn_and_rest_renumbered(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    critic = critic_text(
        [
            review("Q-01", answered=True, answer="Только без очереди", answer_quote=QUEUE),
            review("Q-02"),
            review("Q-03"),
        ]
    )

    outcome = run(doc, tmp_path, critic)

    assert [q.id for q in outcome.questions] == ["Q-01", "Q-02"]
    assert [q.question for q in outcome.questions] == [
        "Книгу вернули на пятнадцатый день: штраф есть?",
        "Штраф за выходной день тоже начисляется?",
    ]
    assert len(outcome.withdrawn) == 1
    gone = outcome.withdrawn[0]
    assert gone.question.question == "Продление заняло десять секунд: это быстро?"
    assert gone.answer.verified and gone.answer.located == QUEUE
    assert gone.reason == "Только без очереди"
    assert outcome.reviewed
    note = next(d for d in outcome.diagnostics if d.code == LLM_QUESTIONS_WITHDRAWN)
    assert note.level is DiagLevel.INFO and "снято вопросов: 1" in note.message


def test_answer_quote_not_in_document_keeps_question(doc: ParsedDocument, tmp_path: Path) -> None:
    critic = critic_text(
        [review("Q-01", answered=True, answer_quote=NOT_IN_DOC), review("Q-02"), review("Q-03")]
    )

    outcome = run(doc, tmp_path, critic)

    assert len(outcome.questions) == 3 and outcome.withdrawn == []
    kept = outcome.questions[0].review
    assert kept is not None and kept.answered and kept.answer_quote is None
    note = next(d for d in outcome.diagnostics if d.code == LLM_QUESTIONS_WITHDRAWN)
    assert "без сверенной цитаты, вопрос оставлен: 1" in note.action


def test_answered_without_quote_keeps_question(doc: ParsedDocument) -> None:
    q = question(doc, "Q-01", "Продление заняло десять секунд: это быстро?", EXTEND)

    outcome = apply_reviews([q], [CriticReview(id="Q-01", answered=True, answer="Да")], doc)

    assert outcome.questions[0].question == q.question and outcome.withdrawn == []


def test_answer_from_the_unclear_place_itself_keeps_question(doc: ParsedDocument) -> None:
    """Критик «отвечает» тем самым местом, о котором вопрос: это не ответ, а самоподтверждение."""
    q = question(doc, "Q-01", "Продление заняло десять секунд: это быстро?", EXTEND)

    outcome = apply_reviews([q], [CriticReview(id="Q-01", answered=True, answer_quote=EXTEND)], doc)

    assert [x.id for x in outcome.questions] == ["Q-01"] and outcome.withdrawn == []
    assert outcome.questions[0].review is not None and outcome.questions[0].review.answer_quote is not None
    assert "неясное место" in outcome.diagnostics[0].action


def test_common_sense_and_obvious_fix_do_not_remove_question(doc: ParsedDocument) -> None:
    q = question(doc, "Q-01", "Каталог обновляется каждый день или раз в неделю?", CATALOG)
    flags = CriticReview(id="q1", common_sense=True, obvious_fix=True, scenario=False, errors=[1, 8])

    outcome = apply_reviews([q], [flags], doc)

    assert len(outcome.questions) == 1 and outcome.withdrawn == []
    got = outcome.questions[0].review
    assert got == QuestionReview(common_sense=True, obvious_fix=True, scenario=False, errors=[1, 8])
    assert outcome.diagnostics == []


def test_question_without_review_stays_and_is_counted(doc: ParsedDocument) -> None:
    qs = [
        question(doc, "Q-01", "Книгу вернули на пятнадцатый день: штраф есть?", TERM),
        question(doc, "Q-02", "Штраф за выходной день тоже начисляется?", FINE),
    ]

    outcome = apply_reviews(qs, [CriticReview(id="Q-01"), CriticReview(id="Q-77")], doc)

    assert [q.review is not None for q in outcome.questions] == [True, False]
    assert "без отзыва критика: 1" in outcome.diagnostics[0].action


# ---------------------------------------------------------------- отказ модели


def test_critic_failure_keeps_questions_with_warning(doc: ParsedDocument, tmp_path: Path) -> None:
    llm = editor_llm(three_cards())
    llm.responses[(LLMStep.QUESTIONS_CRITIC, 1)] = cli_payload("Сбой", is_error=True)

    outcome = build_questions(
        verification(library_findings(doc)), doc, segmentation(doc), llm, make_cfg(doc, tmp_path)
    )

    assert len(outcome.questions) == 3 and outcome.withdrawn == [] and not outcome.reviewed
    assert all(q.review is None for q in outcome.questions)
    warning = next(d for d in outcome.diagnostics if d.code == LLM_QUESTIONS_CRITIC_FAILED)
    assert warning.level is DiagLevel.WARNING


@pytest.mark.parametrize("text", ["Ответа нет", '```json\n{"questions": []}\n```'])
def test_reply_without_reviews_is_failure_not_withdrawal(
    doc: ParsedDocument, tmp_path: Path, text: str
) -> None:
    outcome = run(doc, tmp_path, text)

    assert len(outcome.questions) == 3 and outcome.withdrawn == []
    assert LLM_QUESTIONS_CRITIC_FAILED in [d.code for d in outcome.diagnostics]


def test_no_questions_means_no_critic_call(doc: ParsedDocument, tmp_path: Path) -> None:
    llm = editor_llm(editor_text([]))
    clean = [f for f in library_findings(doc) if f.id in ("F-04", "F-05")]

    outcome = build_questions(verification(clean), doc, segmentation(doc), llm, make_cfg(doc, tmp_path))

    assert outcome.questions == [] and llm.requests == []


def test_parse_reply_raises_without_json() -> None:
    resp = LLMResponse(call_id="questions_critic.1", text="нет блока", backend=Backend.FAKE)

    with pytest.raises(LLMError) as err:
        parse_reply(resp)

    assert err.value.code == LLM_BAD_RESPONSE


def test_bad_review_does_not_spoil_others_and_errors_are_filtered() -> None:
    text = critic_text([{"answered": True}, review("Q-02", errors=[2, "3", 99, 2, "x"])])
    resp = LLMResponse(call_id="questions_critic.1", text=text, backend=Backend.FAKE)

    reviews = parse_reply(resp)

    assert [r.id for r in reviews] == ["Q-02"]
    assert reviews[0].errors == [2, 3]


@pytest.mark.parametrize(
    ("raw", "expected"), [("Q-01", "Q-01"), ("q1", "Q-01"), ("Q 12", "Q-12"), ("C-01", None)]
)
def test_question_ids_are_normalized(raw: str, expected: str | None) -> None:
    assert normalize_question_id(raw) == expected


# ---------------------------------------------------------------- промпт


def test_prompt_example_matches_reply_schema() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)[-1]

    example = CriticReplySchema.model_validate(json.loads(block))

    assert example.reviews and all(e <= max(FORMULATION_ERRORS) for r in example.reviews for e in r.errors)
    assert len(re.findall(r"^\d+\. ", text.split("## Ошибки формулировки")[1], re.M)) == len(
        FORMULATION_ERRORS
    )


def test_request_has_document_questions_and_step_model(doc: ParsedDocument, tmp_path: Path) -> None:
    cfg = make_cfg(doc, tmp_path)
    q = question(doc, "Q-01", "Книгу вернули на пятнадцатый день: штраф есть?", TERM)

    req = build_request([q], doc, cfg)

    model = cfg.model_for(LLMStep.QUESTIONS_CRITIC)
    assert req.step is LLMStep.QUESTIONS_CRITIC and req.call_index == 1
    assert (req.model, req.effort) == (model.model, model.effort)
    assert req.json_schema is None
    assert "### Q-01" in req.prompt and TERM in req.prompt and "[B0" in req.prompt
    assert "{document}" not in req.prompt and "{questions}" not in req.prompt
    assert "Приоритет" not in req.prompt and "F-01" not in req.prompt


# ---------------------------------------------------------------- записанный ответ и конвейер


def test_recorded_critic_reply_reviews_every_pipeline_question(tmp_path: Path) -> None:
    """Живой ответ Opus 5.5 high на вопросах фейкового конвейера (experiments/41-questions-critic)."""
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "out" / "report.md", backend=Backend.FAKE)
    payload = json.loads(RECORDED.read_text(encoding="utf-8"))

    run_pipeline(cfg)

    assert payload["subtype"] == "success" and "claude-opus-5-5" in payload["modelUsage"]
    report = Report.model_validate_json(cfg.report_json_path.read_text(encoding="utf-8"))
    every = [*report.questions, *(w.question for w in report.questions_withdrawn)]
    assert every and all(q.review is not None for q in every)
    assert LLM_QUESTIONS_CRITIC_FAILED not in [d.code for d in report.diagnostics]
    metrics = report.metrics.questions
    assert metrics is not None and metrics.reviewed == len(every)
    assert metrics.before_critic == len(every) and metrics.total == len(report.questions)
    text = cfg.report_md_path.read_text(encoding="utf-8")
    assert "| Вопросы: со сценарием |" in text and "до самопроверки" in text


# ---------------------------------------------------------------- метрики


def test_metrics_on_fixture(doc: ParsedDocument) -> None:
    good = question(doc, "Q-01", "Книгу вернули на пятнадцатый день: штраф есть?", TERM).model_copy(
        update={"review": QuestionReview(scenario=True)}
    )
    vague = question(doc, "Q-02", "Как уточнить штраф?", FINE).model_copy(
        update={
            "addressee": [Addressee.ANALYST],
            "options": [QuestionOption(label="A", text="Уточнить", change="")],
            "review": QuestionReview(scenario=False, errors=[1], obvious_fix=True),
        }
    )
    twin = question(doc, "Q-03", "Книгу вернули на пятнадцатый день: штраф есть ли?", TERM)
    answered = question(doc, "Q-04", "Продление возможно при очереди?", EXTEND).model_copy(
        update={"review": QuestionReview(answered=True)}
    )
    gone = WithdrawnQuestion(question=answered, reason="Нет", answer=locate_quote(doc, QUEUE))

    m = question_metrics([good, vague, twin], [gone])

    assert (m.total, m.before_critic, m.withdrawn, m.reviewed) == (3, 4, 1, 3)
    assert m.withdrawn_share == 0.25
    assert m.with_scenario == 0.5  # у третьего вопроса нет отзыва: в знаменатель не идёт
    assert m.closed == round(2 / 3, 4)
    assert m.without_errors == 0.5
    assert (m.obvious_fix, m.duplicate_pairs) == (1, 1)
    assert m.by_priority == {"P1": 3}
    assert m.by_addressee == {"analyst": 1, "business": 2}


def test_metrics_without_critic_have_no_review_shares(doc: ParsedDocument) -> None:
    m = question_metrics([question(doc, "Q-01", "Книгу вернули на пятнадцатый день: штраф есть?", TERM)])

    assert (m.with_scenario, m.without_errors, m.withdrawn_share) == (None, None, None)
    assert m.closed == 1.0


def test_repeated_options_are_not_closed(doc: ParsedDocument) -> None:
    q = question(doc, "Q-01", "Штраф за выходной день тоже начисляется?", FINE)
    same = [QuestionOption(label=x, text="Да", change="Поправить") for x in "AB"]

    assert is_closed(q) and not is_closed(q.model_copy(update={"options": same}))
