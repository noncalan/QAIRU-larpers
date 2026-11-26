"""Чтение ``answers.yaml`` (#42): ошибки схемы понятны и по-русски, ответы разбираются как данные."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from report_factory import full_report
from validator.clarify.answers import AnswersError, RawAnswer, load_answers, resolve_answer
from validator.clarify.ask import ask_questions, pick_questions
from validator.models import AnswerKind, QuestionOption

OPTIONS = {"A": ("Первое правило", "Поправить первое место"), "B": ("Второе правило", "")}


def _write(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "answers.yaml"
    text = payload if isinstance(payload, str) else yaml.safe_dump(payload, allow_unicode=True)
    path.write_text(text, encoding="utf-8")
    return path


def _entry(qid: str = "Q-01", answer: object = "A", **extra: object) -> dict[str, object]:
    return {
        "id": qid,
        "question": "Какое правило действует?",
        "options": [{"label": "A", "text": "Первое правило", "change": "Поправить первое место"}],
        "answer": answer,
        **extra,
    }


def _raw(answer: str, options: dict[str, tuple[str, str]] | None = None) -> RawAnswer:
    return RawAnswer("Q-01", "Вопрос?", answer, "", [], [], dict(options or {}))


def test_valid_file_keeps_header_hash_and_entries(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        {"schema_version": 1, "document_sha256": "ABC", "answered_by": "Автор", "questions": [_entry()]},
    )

    loaded = load_answers(path)

    assert loaded.document_sha256 == "abc"
    assert loaded.answered_by == "Автор"
    assert len(loaded.sha256) == 64
    assert [a.question_id for a in loaded.answers] == ["Q-01"]
    assert loaded.answers[0].options == {"A": ("Первое правило", "Поправить первое место")}


@pytest.mark.parametrize(
    ("payload", "fragment"),
    [
        ("questions: [unclosed", "это не YAML"),
        ("- просто список", "в начале файла ожидаются поля"),
        ({"schema_version": 2, "questions": []}, "версия схемы 2 не поддерживается"),
        ({"schema_version": 1}, "нет списка questions"),
        ({"questions": ["Q-01: A"]}, "вопрос №1 в списке questions записан не как набор полей"),
        ({"questions": [{"answer": "A"}]}, "у вопроса №1 нет id вида Q-01"),
        ({"questions": [_entry(), _entry()]}, "вопрос Q-01 записан дважды"),
        ({"questions": [_entry(answer=["A", "B"])]}, "ответ Q-01: ожидается текст"),
        ({"questions": [_entry(answer="x" * 2001)]}, "длиннее 2000 символов"),
    ],
)
def test_invalid_file_gives_clear_russian_error(tmp_path: Path, payload: object, fragment: str) -> None:
    path = _write(tmp_path, payload)

    with pytest.raises(AnswersError) as caught:
        load_answers(path)

    assert fragment in caught.value.message
    assert caught.value.message.startswith("Файл ответов answers.yaml")
    assert caught.value.hint


def test_missing_file_is_error(tmp_path: Path) -> None:
    with pytest.raises(AnswersError, match="файл не найден"):
        load_answers(tmp_path / "nope.yaml")


def test_not_utf8_file_is_error(tmp_path: Path) -> None:
    path = tmp_path / "answers.yaml"
    path.write_bytes("answer: Ответ".encode("cp1251"))

    with pytest.raises(AnswersError, match="UTF-8"):
        load_answers(path)


@pytest.mark.parametrize(
    ("answer", "kind", "option"),
    [
        ("A", AnswerKind.OPTION, "A"),
        ("b.", AnswerKind.OPTION, "B"),
        ("А", AnswerKind.OPTION, "A"),  # кириллическая «А»
        ("пропустить", AnswerKind.SKIPPED, None),
        ("", AnswerKind.NONE, None),
        ("Считаем по дате проведения", AnswerKind.TEXT, None),
    ],
)
def test_answer_kinds(answer: str, kind: AnswerKind, option: str | None) -> None:
    resolved, warning = resolve_answer(_raw(answer, OPTIONS))

    assert resolved.kind is kind
    assert resolved.option == option
    assert warning is None


def test_option_answer_takes_rule_and_change_from_option() -> None:
    resolved, _ = resolve_answer(_raw("A", OPTIONS))

    assert resolved.text == "Первое правило"
    assert resolved.change == "Поправить первое место"


def test_unknown_letter_is_warning_not_free_text() -> None:
    resolved, warning = resolve_answer(_raw("D", OPTIONS))

    assert resolved.kind is AnswerKind.NONE
    assert warning is not None and "варианта «D» нет" in warning


def test_letter_uses_options_of_this_run_when_file_has_none() -> None:
    resolved, warning = resolve_answer(_raw("B"), OPTIONS)

    assert resolved.kind is AnswerKind.OPTION
    assert resolved.text == "Второе правило"
    assert warning is None


# ---------------------------------------------------------------- --ask


def _scripted(lines: list[str]):  # type: ignore[no-untyped-def]
    feed = iter(lines)
    return lambda _prompt: next(feed, None)


def test_ask_maps_number_to_letter_keeps_text_and_skips_empty() -> None:
    questions = pick_questions(full_report())[:3]
    shown: list[str] = []

    answers = ask_questions(questions, _scripted(["1", "", "Свой ответ"]), shown.append)

    assert answers == {questions[0].id: questions[0].options[0].label, questions[2].id: "Свой ответ"}
    assert any(questions[0].question in s for s in shown)


def test_ask_stops_at_end_of_input() -> None:
    questions = pick_questions(full_report())
    shown: list[str] = []

    answers = ask_questions(questions, _scripted(["2"]), shown.append)

    assert list(answers) == [questions[0].id]
    assert "Ввод закончился" in shown[-1]


def test_pick_questions_takes_blocking_first_and_at_most_five() -> None:
    report = full_report()

    picked = pick_questions(report)

    assert 1 <= len(picked) <= 5
    assert [q.id for q in picked][: len(report.conclusion.ask_first)] == report.conclusion.ask_first[:5]


def test_option_number_outside_range_is_free_text() -> None:
    q = pick_questions(full_report())[0]
    q = q.model_copy(update={"options": [QuestionOption(label="A", text="Правило")]})

    answers = ask_questions([q], _scripted(["7"]), lambda _s: None)

    assert answers == {q.id: "7"}
