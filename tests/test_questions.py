"""Шаг 8, вопросы автору: кандидаты кодом, проверки карточек, приоритет, отказ модели, questions.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import validator.questions as questions_step
from questions_helpers import (
    EXTEND,
    FINE,
    FIVE_DAYS,
    QUEUE,
    TERM,
    card,
    editor_llm,
    editor_text,
    finding,
    library_findings,
    make_cfg,
    make_doc,
    segmentation,
    verification,
)
from validator.errors import LLM_QUESTIONS_CHECKED, LLM_QUESTIONS_FAILED, LLM_QUESTIONS_SHORTLIST
from validator.llm import FakeLLM
from validator.models import (
    Addressee,
    DefectType,
    DiagLevel,
    ParsedDocument,
    Property,
    Question,
    QuestionOrigin,
    QuestionPriority,
    Severity,
    Verdict,
    WorkStage,
)
from validator.questions import QuestionsOutcome, ask, build_questions, write_questions_yaml
from validator.questions.candidates import collect_candidates, shortlist
from validator.questions.cards import MAX_QUESTIONS, CardDraft, CheckLog, finalize
from validator.questions.export import SCHEMA_VERSION

INVENTED = "Продление занимает ровно три секунды и выполняется на сервере."
ANSWERED = "Ответ есть в документе."


def dropped(*cids: str) -> list[dict[str, str]]:
    return [{"candidate": cid, "reason": ANSWERED} for cid in cids]


@pytest.fixture
def doc(tmp_path: Path) -> ParsedDocument:
    return make_doc(tmp_path)


def run(doc: ParsedDocument, tmp_path: Path, llm: FakeLLM) -> QuestionsOutcome:
    findings = library_findings(doc)
    return build_questions(verification(findings), doc, segmentation(doc), llm, make_cfg(doc, tmp_path))


def assert_card_is_grounded(q: Question, doc: ParsedDocument) -> None:
    assert q.quotes and all(x.verified and x.located and x.located in doc.text for x in q.quotes)
    assert 2 <= len(q.options) <= 4
    assert all(o.change for o in q.options)
    assert q.related_findings


# ---------------------------------------------------------------- кандидаты кодом


def test_candidates_come_only_from_findings_that_need_an_answer(doc: ParsedDocument) -> None:
    cands = collect_candidates(verification(library_findings(doc)))

    covered = {fid for c in cands for fid in c.finding_ids}

    assert covered == {"F-01", "F-02", "F-03", "F-06"}  # F-04 однозначная правка, F-05 инструкция
    assert [c.cid for c in cands] == [f"C-{n:02d}" for n in range(1, len(cands) + 1)]


def test_duplicate_findings_merge_into_one_candidate(doc: ParsedDocument) -> None:
    cands = collect_candidates(verification(library_findings(doc)))

    merged = [c for c in cands if "F-02" in c.finding_ids]

    assert len(merged) == 1 and merged[0].finding_ids == ("F-02", "F-03")
    assert merged[0].origin is QuestionOrigin.UNCLEAR_VERDICT
    assert merged[0].placeholders == ("сколько секунд",)


def test_different_topics_in_one_place_stay_separate(doc: ParsedDocument) -> None:
    findings = [
        finding(doc, "F-01", [EXTEND], verdict=Verdict.UNCLEAR, requirements=["R-02"]),
        finding(
            doc,
            "F-02",
            [EXTEND],
            defect=DefectType.UNVERIFIABLE,
            prop=Property.VERIFIABLE,
            blocks_testing=True,
            requirements=["R-02"],
        ),
    ]

    cands = collect_candidates(verification(findings))

    assert [c.finding_ids for c in cands] == [("F-01",), ("F-02",)]


def test_same_requirement_and_same_gap_merge(doc: ParsedDocument) -> None:
    findings = [
        finding(doc, "F-01", [TERM], suggestion="Срок [уточнить: в календарных днях]", requirements=["R-03"]),
        finding(
            doc,
            "F-02",
            [FIVE_DAYS],
            defect=DefectType.INCOMPLETE,
            prop=Property.COMPLETE,
            suggestion="Указать [уточнить: в календарных днях]",
            requirements=["R-03"],
        ),
    ]

    cands = collect_candidates(verification(findings))

    assert [c.finding_ids for c in cands] == [("F-01", "F-02")]


# ---------------------------------------------------------------- карточки редактора и проверки кода


def test_editor_cards_get_priority_and_ids_from_code(doc: ParsedDocument, tmp_path: Path) -> None:
    reply = editor_text(
        [
            card(["C-03"], "Штраф за выходной день тоже начисляется?", quotes=[FINE]),
            {
                **card(["C-02"], "Книгу вернули на пятнадцатый день: штраф есть?", quotes=[TERM]),
                "priority": "P2",
            },
            card(["C-01"], "Сколько секунд допустимо на продление?", quotes=[EXTEND]),
        ]
    )

    outcome = run(doc, tmp_path, editor_llm(reply))

    assert outcome.edited and outcome.candidates == 3
    assert [q.id for q in outcome.questions] == ["Q-01", "Q-02", "Q-03"]
    assert [q.priority for q in outcome.questions] == [
        QuestionPriority.P0,  # связанная находка критичная, «P2» редактора проигнорирован
        QuestionPriority.P1,  # неясно и мешает тесту, важно
        QuestionPriority.P1,  # мешает тесту, незначительно: ниже по ценности
    ]
    assert outcome.questions[0].related_findings == ["F-01"]
    assert outcome.questions[0].blocks == [WorkStage.DEVELOPMENT, WorkStage.TESTING]
    assert outcome.questions[1].related_findings == ["F-02", "F-03"]
    assert outcome.questions[0].standard and "5.2.6" in outcome.questions[0].standard[0]
    for q in outcome.questions:
        assert_card_is_grounded(q, doc)
    assert outcome.diagnostics == []


def test_unverified_quote_is_removed(doc: ParsedDocument, tmp_path: Path) -> None:
    reply = editor_text(
        [card(["C-01"], "Сколько секунд допустимо на продление?", quotes=[EXTEND, INVENTED])],
        dropped("C-02", "C-03"),
    )

    outcome = run(doc, tmp_path, editor_llm(reply))

    (q,) = outcome.questions
    assert [x.located for x in q.quotes] == [EXTEND]
    (diag,) = outcome.diagnostics
    assert diag.code == LLM_QUESTIONS_CHECKED and diag.level is DiagLevel.INFO
    assert "несверенных цитат убрано: 1" in diag.message


def test_card_without_verified_quotes_takes_quotes_of_its_findings(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    reply = editor_text(
        [card(["C-01"], "Сколько секунд допустимо на продление?", quotes=[INVENTED])], dropped("C-02", "C-03")
    )

    (q,) = run(doc, tmp_path, editor_llm(reply)).questions

    assert [x.located for x in q.quotes] == [EXTEND]


def test_table_row_quoted_in_column_form_is_matched_by_cells(doc: ParsedDocument, tmp_path: Path) -> None:
    row = (
        "Исходные условия: Книга выдана первого марта; Действие: Вернуть двадцатого марта; "
        "Ожидаемый результат: Штраф за пять дней"
    )
    reply = editor_text(
        [card(["C-02"], "Книгу вернули на пятнадцатый день: штраф есть?", quotes=[TERM, row])]
    )

    (q,) = run(doc, tmp_path, editor_llm(reply)).questions

    assert len(q.quotes) == 2
    assert q.quotes[1].located is not None and q.quotes[1].located.endswith(FIVE_DAYS)
    assert q.quotes[1].located in doc.text


def test_card_without_known_candidates_is_dropped_with_diagnostic(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    reply = editor_text(
        [
            card(["C-01"], "Сколько секунд допустимо на продление?", quotes=[EXTEND]),
            card(["C-99"], "Можно ли продлить книгу дважды подряд?", quotes=[EXTEND]),
            card([], "Нужна ли очередь на продление?", quotes=[EXTEND]),
        ],
        dropped("C-02", "C-03"),
    )

    outcome = run(doc, tmp_path, editor_llm(reply))

    assert len(outcome.questions) == 1
    (diag,) = outcome.diagnostics
    assert "карточек без связанных находок: 2" in diag.message
    assert "ссылок на несуществующих кандидатов: 1" in diag.message


def test_options_are_checked_by_code(doc: ParsedDocument, tmp_path: Path) -> None:
    five = [{"text": f"Правило {n}", "change": f"Правка {n}", "grounded_in": None} for n in "абвгд"]
    lonely = [
        {"text": "Единственное правило", "change": "Правка", "grounded_in": None},
        {"text": "Без последствий", "change": "", "grounded_in": None},
        {"text": "единственное   правило", "change": "Повтор", "grounded_in": None},
    ]
    reply = editor_text(
        [
            card(["C-01"], "Сколько секунд допустимо на продление?", quotes=[EXTEND], options=five),
            card(["C-03"], "Штраф за выходной день тоже начисляется?", quotes=[FINE], options=lonely),
        ],
        dropped("C-02"),
    )

    outcome = run(doc, tmp_path, editor_llm(reply))

    (q,) = outcome.questions
    assert [o.label for o in q.options] == ["A", "B", "C", "D"]
    message = outcome.diagnostics[0].message
    assert "карточек меньше чем с двумя вариантами: 1" in message
    assert "вариантов без последствия или повторных убрано: 2" in message
    assert "карточек, где вариантов было больше 4: 1" in message


def test_option_grounding_is_verified(doc: ParsedDocument, tmp_path: Path) -> None:
    options = [
        {"text": "Срок две недели", "change": "Поправить критерий", "grounded_in": TERM},
        {"text": "Срок пятнадцать дней", "change": "Поправить срок", "grounded_in": INVENTED},
    ]
    reply = editor_text([card(["C-02"], "Книгу вернули на пятнадцатый день: штраф есть?", options=options)])

    (q,) = run(doc, tmp_path, editor_llm(reply)).questions

    assert q.options[0].grounded_in == TERM
    assert q.options[1].grounded_in is None  # вариант для обсуждения


def test_duplicates_merge_and_each_finding_has_one_question(doc: ParsedDocument, tmp_path: Path) -> None:
    reply = editor_text(
        [
            card(["C-01"], "Сколько секунд допустимо на продление книги?", quotes=[EXTEND]),
            card(["C-03"], "Сколько секунд допустимо на продление книги читателем?", quotes=[FINE]),
            card(["C-02", "C-03"], "Книгу вернули на пятнадцатый день: штраф есть?", quotes=[TERM]),
            card(["C-02"], "Срок выдачи считается в рабочих или календарных днях?", quotes=[TERM]),
        ]
    )

    outcome = run(doc, tmp_path, editor_llm(reply))

    related = [q.related_findings for q in outcome.questions]
    assert related == [["F-01", "F-06"], ["F-02", "F-03"]]
    flat = [fid for ids in related for fid in ids]
    assert len(flat) == len(set(flat))
    assert "повторов слито с другими вопросами: 2" in outcome.diagnostics[0].message


def test_addressees_are_mapped_and_limited(doc: ParsedDocument, tmp_path: Path) -> None:
    reply = editor_text(
        [
            card(
                ["C-01"],
                "Сколько секунд допустимо на продление?",
                quotes=[EXTEND],
                addressee=["Аналитик", "security", "architect", "космонавт"],
            ),
            card(
                ["C-03"], "Штраф за выходной день тоже начисляется?", quotes=[FINE], addressee=["космонавт"]
            ),
        ],
        dropped("C-02"),
    )

    questions = run(doc, tmp_path, editor_llm(reply)).questions

    assert questions[0].addressee == [Addressee.ANALYST, Addressee.SECURITY]
    assert questions[1].addressee == [Addressee.ANALYST]


def test_more_than_limit_is_cut(doc: ParsedDocument) -> None:
    findings = {
        f"F-{n:02d}": finding(doc, f"F-{n:02d}", [EXTEND], severity=Severity.MINOR) for n in range(1, 21)
    }
    words = (
        "яблоко груша слива вишня малина клубника ежевика смородина крыжовник арбуз "
        "дыня персик абрикос лимон апельсин мандарин банан ананас киви манго"
    )
    drafts = [
        CardDraft(
            finding_ids=(fid,),
            origin=QuestionOrigin.FINDING,
            addressee=(Addressee.ANALYST,),
            topic=word,
            quotes=tuple(findings[fid].quotes),
            question=f"{word}?",
            why="",
            options=(),
            if_unanswered="",
        )
        for fid, word in zip(findings, words.split(), strict=True)
    ]
    log = CheckLog()

    questions = finalize(drafts, findings, {}, log)

    assert len(questions) == MAX_QUESTIONS
    assert log.over_limit == 20 - MAX_QUESTIONS


# ---------------------------------------------------------------- отказ модели и запасной путь


def test_model_failure_gives_code_questions_and_warning(doc: ParsedDocument, tmp_path: Path) -> None:
    outcome = run(doc, tmp_path, editor_llm("You've hit your usage limit", is_error=True))

    assert not outcome.edited
    assert [d.code for d in outcome.diagnostics] == [LLM_QUESTIONS_FAILED]
    assert outcome.diagnostics[0].level is DiagLevel.WARNING
    assert [q.related_findings for q in outcome.questions] == [["F-01"], ["F-02", "F-03"], ["F-06"]]
    assert outcome.questions[0].priority is QuestionPriority.P0
    for q in outcome.questions:
        assert_card_is_grounded(q, doc)


def test_fallback_does_not_merge_template_questions(doc: ParsedDocument, tmp_path: Path) -> None:
    findings = [
        finding(doc, "F-01", [EXTEND], suggestion="Продление за [уточнить: время ответа сервиса]"),
        finding(doc, "F-02", [FINE], suggestion="Штраф за [уточнить: время ответа сервиса]"),
    ]
    llm = editor_llm("Сбой", is_error=True)

    outcome = build_questions(verification(findings), doc, segmentation(doc), llm, make_cfg(doc, tmp_path))

    assert [q.related_findings for q in outcome.questions] == [["F-01"], ["F-02"]]


def test_fallback_contradiction_uses_sides_of_the_contradiction(doc: ParsedDocument, tmp_path: Path) -> None:
    findings = [
        finding(
            doc, "F-01", [QUEUE], suggestion="Срок [уточнить: в календарных днях]", requirements=["R-03"]
        ),
        finding(
            doc,
            "F-02",
            [TERM, FIVE_DAYS],
            defect=DefectType.CONTRADICTION,
            prop=Property.SET_CONSISTENT,
            suggestion="Срок [уточнить: в календарных днях]",
            requirements=["R-03"],
        ),
    ]
    llm = editor_llm("Сбой", is_error=True)

    (q,) = build_questions(
        verification(findings), doc, segmentation(doc), llm, make_cfg(doc, tmp_path)
    ).questions

    assert q.related_findings == ["F-01", "F-02"]
    assert [o.grounded_in for o in q.options] == [TERM, FIVE_DAYS]
    assert [x.located for x in q.quotes[:2]] == [TERM, FIVE_DAYS]


def test_unexpected_error_on_reply_falls_back(
    doc: ParsedDocument, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> None:
        raise KeyError("сломалось")

    monkeypatch.setattr("validator.questions.draft_from_card", broken)
    reply = editor_text([card(["C-01"], "Сколько секунд допустимо на продление?", quotes=[EXTEND])])

    outcome = run(doc, tmp_path, editor_llm(reply))

    assert outcome.diagnostics[0].code == LLM_QUESTIONS_FAILED
    assert "KeyError" in outcome.diagnostics[0].message
    assert len(outcome.questions) == 3


def test_contradiction_fallback_offers_both_places(doc: ParsedDocument, tmp_path: Path) -> None:
    outcome = run(doc, tmp_path, editor_llm("Сбой", is_error=True))

    first = outcome.questions[0]
    assert [o.grounded_in for o in first.options] == [TERM, FIVE_DAYS]
    assert first.addressee == [Addressee.BUSINESS, Addressee.ANALYST]


def test_missing_fixture_falls_back(doc: ParsedDocument, tmp_path: Path) -> None:
    outcome = run(doc, tmp_path, FakeLLM(root=tmp_path / "empty"))

    assert outcome.questions and outcome.diagnostics[0].code == LLM_QUESTIONS_FAILED


@pytest.mark.parametrize(
    "text",
    [
        "## Рассуждение\n\nВопросов не будет, JSON забыл.",
        '```json\n{"questions": [{"candidates": ["C-02"]}]}\n```',  # карточка без вопроса
        editor_text([card(["C-77"], "Неизвестный кандидат?")]),
        editor_text([]),  # пусто и без объяснений
    ],
    ids=["no-json", "invalid-card", "unknown-candidate", "empty"],
)
def test_unusable_reply_falls_back(doc: ParsedDocument, tmp_path: Path, text: str) -> None:
    outcome = run(doc, tmp_path, editor_llm(text))

    assert not outcome.edited
    assert outcome.diagnostics[0].code == LLM_QUESTIONS_FAILED
    assert len(outcome.questions) == 3


def test_editor_may_drop_every_candidate(doc: ParsedDocument, tmp_path: Path) -> None:
    outcome = run(doc, tmp_path, editor_llm(editor_text([], dropped("C-01", "C-02", "C-03"))))

    assert outcome.edited and outcome.questions == []
    (diag,) = outcome.diagnostics
    assert diag.code == LLM_QUESTIONS_CHECKED
    assert "однозначна): 3, пропустил без причины: 0" in diag.message


def test_silently_skipped_blocking_candidate_is_not_lost(doc: ParsedDocument, tmp_path: Path) -> None:
    """Редактор не взял и не отклонил C-02 (P0) и C-03 (P1): P0 собирается кодом, P1 только в диагностике."""
    reply = editor_text([card(["C-01"], "Сколько секунд допустимо на продление?", quotes=[EXTEND])])

    outcome = run(doc, tmp_path, editor_llm(reply))

    assert [q.related_findings for q in outcome.questions] == [["F-01"], ["F-02", "F-03"]]
    assert outcome.questions[0].priority is QuestionPriority.P0
    assert outcome.questions[0].options[0].grounded_in == TERM  # шаблон запасного пути: два места
    message = outcome.diagnostics[0].message
    assert "кандидатов редактор не взял и не отклонил: 2" in message
    assert "из них блокируют разработку и собраны кодом без редактуры: 1" in message


def test_partly_dropped_reply_without_cards_falls_back(doc: ParsedDocument, tmp_path: Path) -> None:
    outcome = run(doc, tmp_path, editor_llm(editor_text([], dropped("C-01", "C-77"))))

    assert outcome.edited
    assert [q.related_findings for q in outcome.questions] == [["F-01"]]  # P0 не потерян


def test_no_candidates_means_no_model_call(doc: ParsedDocument, tmp_path: Path) -> None:
    llm = editor_llm(editor_text([]))
    clean = [f for f in library_findings(doc) if f.id in ("F-04", "F-05")]

    questions = ask(verification(clean), doc, segmentation(doc), llm, make_cfg(doc, tmp_path))

    assert questions == [] and llm.requests == []


def test_shortlist_takes_highest_priority_and_keeps_document_order(doc: ParsedDocument) -> None:
    """Кандидаты C-01 (P1, ценность 6), C-02 (P0, 8), C-03 (P1, 1): сначала приоритет, потом ценность."""
    cands = collect_candidates(verification(library_findings(doc)))
    req_types = {r.id: r.type for r in segmentation(doc).requirements}

    assert shortlist(cands, req_types) == cands
    assert [c.cid for c in shortlist(cands, req_types, limit=1)] == ["C-02"]
    assert [c.cid for c in shortlist(cands, req_types, limit=2)] == ["C-01", "C-02"]


def test_editor_sees_only_the_shortlist_on_a_long_document(
    doc: ParsedDocument, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Кандидатов больше, чем разбирает редактор (#38): лишние не идут в вызов и не считаются пропущенными."""
    monkeypatch.setattr(questions_step, "shortlist", lambda cands, types: shortlist(cands, types, limit=2))
    reply = editor_text(
        [card(["C-02"], "Сколько секунд допустимо на продление?", quotes=[EXTEND])], dropped("C-01")
    )
    llm = editor_llm(reply)

    outcome = run(doc, tmp_path, llm)

    prompt = llm.requests[0].prompt
    assert "C-01" in prompt and "C-02" in prompt and "C-03" not in prompt
    assert outcome.candidates == 3
    (note,) = [d for d in outcome.diagnostics if d.code == LLM_QUESTIONS_SHORTLIST]
    assert note.level is DiagLevel.INFO and "Кандидатов в вопросы 3" in note.message
    assert not any("не взял и не отклонил" in d.message for d in outcome.diagnostics)


# ---------------------------------------------------------------- questions.yaml


def test_questions_yaml_is_valid_and_ready_for_answers(doc: ParsedDocument, tmp_path: Path) -> None:
    questions = run(doc, tmp_path, editor_llm("Сбой", is_error=True)).questions
    path = tmp_path / "questions.yaml"

    write_questions_yaml(path, questions, doc.name, doc.sha256)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert path.read_text(encoding="utf-8").startswith("# Вопросы агента QAIRU")
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["document"] == doc.name and data["document_sha256"] == doc.sha256
    assert data["answered_by"] is None
    assert [q["id"] for q in data["questions"]] == [q.id for q in questions]
    for item, q in zip(data["questions"], questions, strict=True):
        assert item["answer"] is None and item["comment"] is None
        assert item["priority"] == q.priority.value
        assert [o["label"] for o in item["options"]] == [o.label for o in q.options]
        assert all(o["text"] and o["change"] and o["for_discussion"] is False for o in item["options"])
        assert all(w["quote"] in doc.text and w["place"] for w in item["where"])
        assert item["related_findings"] == q.related_findings


def test_questions_yaml_without_questions_is_valid(tmp_path: Path) -> None:
    path = tmp_path / "questions.yaml"

    write_questions_yaml(path, [], "doc.md", None)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["questions"] == []
