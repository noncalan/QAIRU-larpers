"""Шаг 6, попытка теста (#40): значение без цитаты становится [?], пропуск связан с вопросом, отказ модели
не роняет отчёт, блок есть в Markdown и PDF."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import validator.evidence_testdraft as step
from conftest import REQUIREMENTS
from questions_helpers import (
    EXTEND,
    FINE,
    FIVE_DAYS,
    TERM,
    cli_payload,
    finding,
    make_cfg,
    make_doc,
)
from test_report_pdf import load, pdf_text
from validator.config import load_config
from validator.errors import LLM_NO_FIXTURE, LLM_TEST_DRAFT_CHECKED, LLM_TEST_DRAFT_FAILED, LLMError
from validator.evidence_testdraft import (
    BATCH_SIZE,
    CONTESTED_NOTE,
    SYSTEM_PROMPT,
    build_request,
    draft_tests,
    needs_draft,
    parse_reply,
)
from validator.evidence_testdraft_check import DraftReplySchema
from validator.evidence_testdraft_links import link_gaps
from validator.llm import FakeLLM
from validator.llm.base import parse_cli_payload
from validator.models import (
    Backend,
    CriticAction,
    CriticDecision,
    DefectType,
    DiagLevel,
    DocKind,
    DraftGap,
    Effort,
    LLMRequest,
    LLMStep,
    MergedFinding,
    ParsedDocument,
    Property,
    Question,
    QuestionPriority,
    Report,
    TestDraft,
    Verification,
    WithdrawnQuestion,
    Witness,
)
from validator.pipeline import run as run_pipeline
from validator.refs import RefNamer, humanize_finding
from validator.report import render_md, render_pdf
from validator.report.testdraft_view import draft_stats, draft_view, stats_line
from validator.text import locate_quote


@pytest.fixture
def doc(tmp_path: Path) -> ParsedDocument:
    return make_doc(tmp_path)


def merged(
    mid: str = "M-001",
    *quotes: str,
    prop: Property = Property.VERIFIABLE,
    blocks_testing: bool = False,
    test_gap: str | None = None,
    critic: CriticDecision | None = None,
    defect_type: DefectType = DefectType.UNVERIFIABLE,
    witness: Witness | None = None,
) -> MergedFinding:
    return MergedFinding(
        mid=mid,
        property=prop,
        defect_type=defect_type,
        witness=witness,
        quotes=list(quotes or [FINE]),
        explanation="Пояснение находки.",
        blocks_testing=blocks_testing,
        test_gap=test_gap,
        critic=critic,
    )


def draft(finding: str = "M-001", **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "finding": finding,
        "can_write": False,
        "precondition": "Книга выдана читателю.",
        "steps": ["Вернуть книгу на пятый день просрочки."],
        "expected": "Штраф составляет [?1] за день.",
        "values": [],
        "gaps": [{"number": 1, "missing": "размер штрафа за день", "part": "expected"}],
        "note": "",
    }
    return {**base, **fields}


def reply(*drafts: dict[str, Any]) -> str:
    body = json.dumps({"drafts": list(drafts)}, ensure_ascii=False, indent=2)
    return f"## Рассуждение\n\n- Находки разобраны.\n\n## Ответ\n\n```json\n{body}\n```\n"


def llm_with(*drafts: dict[str, Any]) -> FakeLLM:
    return FakeLLM(responses={(LLMStep.TEST_DRAFT, 1): cli_payload(reply(*drafts))})


def run(
    doc: ParsedDocument, found: Sequence[MergedFinding], llm: FakeLLM, tmp_path: Path
) -> tuple[list[MergedFinding], list[Any]]:
    out = draft_tests(found, doc, llm, make_cfg(doc, tmp_path))
    return out.findings, out.diagnostics


def only_draft(findings: Sequence[MergedFinding]) -> TestDraft:
    assert findings[0].test_draft is not None
    return findings[0].test_draft


# ---------------------------------------------------------------- значения и цитаты


def test_value_with_verified_quote_is_kept_with_its_place(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(
        expected="Срок выдачи составляет четырнадцать дней.",
        can_write=True,
        gaps=[],
        values=[{"value": "четырнадцать дней", "quote": TERM}],
    )

    findings, _ = run(doc, [merged("M-001", TERM)], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert t.complete and not t.gap_items
    assert [v.value for v in t.values] == ["четырнадцать дней"]
    assert t.values[0].verified and t.values[0].anchor is not None


def test_value_without_quote_in_document_becomes_gap(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(
        expected="Штраф за день составляет пять процентов.",
        gaps=[],
        values=[{"value": "пять процентов", "quote": "Штраф составляет пять процентов от суммы."}],
    )

    findings, diagnostics = run(doc, [merged()], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert "пять процентов" not in t.expected
    assert t.expected == "Штраф за день составляет [?1]."
    assert not t.values and not t.complete
    gap = t.gap_items[0]
    assert gap.number == 1 and gap.replaced == "пять процентов" and "без цитаты" in gap.missing
    assert t.gaps == [gap.missing]
    info = next(d for d in diagnostics if d.code == LLM_TEST_DRAFT_CHECKED)
    assert info.level is DiagLevel.INFO and "пропусков вместо значений без цитаты: 1" in info.action


def test_value_replaced_in_two_places_counts_two_gaps_in_diagnostic_and_summary(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    """#118: диагностика писала «заменено: 3», сводка «4» (одно значение в двух местах). Считаем места."""
    d = draft(
        precondition="Штраф составляет пять процентов.",
        expected="Начислено пять процентов.",
        gaps=[],
        values=[{"value": "пять процентов", "quote": "Штраф составляет пять процентов от суммы."}],
    )

    findings, diagnostics = run(doc, [merged()], llm_with(d), tmp_path)

    t = only_draft(findings)
    replaced = [g for g in t.gap_items if g.replaced]
    assert len(replaced) == 2
    info = next(d for d in diagnostics if d.code == LLM_TEST_DRAFT_CHECKED)
    assert "пропусков вместо значений без цитаты: 2" in info.action


def test_quote_found_but_value_not_in_it_becomes_gap(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(
        expected="Срок выдачи составляет двадцать дней.",
        gaps=[],
        values=[{"value": "двадцать дней", "quote": TERM}],
    )

    findings, _ = run(doc, [merged("M-001", TERM)], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert t.expected == "Срок выдачи составляет [?1]." and t.gap_items[0].replaced == "двадцать дней"


def test_value_missing_from_draft_text_is_not_shown_as_source(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(values=[{"value": "четырнадцать дней", "quote": TERM}])

    findings, _ = run(doc, [merged()], llm_with(d), tmp_path)

    assert not only_draft(findings).values


def test_number_in_expected_result_without_source_becomes_gap(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(expected="Штраф 500 ₸ за день.", gaps=[])

    findings, _ = run(doc, [merged()], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert t.expected == "Штраф [?1] ₸ за день."
    assert "не подтверждено" in t.gap_items[0].missing


def test_number_from_test_data_may_be_used_in_expected_result(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(
        precondition="Остаток на карте 50000 ₸.",
        steps=["Списать 1 000 ₸."],
        expected="Остаток до списания был 50000 ₸, списано 1 000 ₸.",
        gaps=[],
    )

    findings, _ = run(doc, [merged(test_gap="неизвестно")], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert t.expected == "Остаток до списания был 50000 ₸, списано 1 000 ₸. [?1]" and t.gaps == ["неизвестно"]


def test_thousands_separator_is_one_number(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(
        precondition="Порог 50000 ₸.",
        expected="Порог остаётся 50 000 ₸.",
        gaps=[],
    )

    findings, _ = run(doc, [merged(test_gap="x")], llm_with(d), tmp_path)

    assert "50 000" in only_draft(findings).expected


# ---------------------------------------------------------------- пропуски и вывод «можно написать»


def test_gaps_are_renumbered_in_text_order_with_explanations(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(
        precondition="Срок [?3].",
        expected="Штраф [?7] и размер [?3].",
        gaps=[
            {"number": 3, "missing": "срок продления", "part": "precondition"},
            {"number": 7, "missing": "размер штрафа", "part": "expected"},
        ],
    )

    findings, _ = run(doc, [merged()], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert t.precondition == "Срок [?1]." and t.expected == "Штраф [?2] и размер [?1]."
    assert [(g.number, g.missing, g.part) for g in t.gap_items] == [
        (1, "срок продления", "предусловие"),
        (2, "размер штрафа", "ожидаемый результат"),
    ]


def test_marker_without_explanation_still_gets_a_gap(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(expected="Штраф [?].", gaps=[])

    findings, _ = run(doc, [merged()], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert t.expected == "Штраф [?1]." and t.gap_items[0].missing


def test_complete_draft_clears_blocks_testing(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(
        expected="Срок выдачи составляет четырнадцать дней.",
        can_write=True,
        gaps=[],
        values=[{"value": "четырнадцать дней", "quote": TERM}],
    )

    findings, _ = run(doc, [merged("M-001", TERM, blocks_testing=True)], llm_with(d), tmp_path)

    assert not findings[0].blocks_testing and only_draft(findings).complete


def _writable() -> dict[str, Any]:
    return draft(
        expected="Срок выдачи составляет четырнадцать дней.",
        can_write=True,
        gaps=[],
        values=[{"value": "четырнадцать дней", "quote": TERM}],
    )


@pytest.mark.parametrize(
    ("defect_type", "witness"),
    [
        (DefectType.CONTRADICTION, None),
        (DefectType.AMBIGUOUS, Witness(scenario="Сценарий.", checked=True)),
    ],
    ids=["contradiction", "accepted-witness"],
)
def test_contested_finding_keeps_blocks_testing_after_complete_draft(
    doc: ParsedDocument, tmp_path: Path, defect_type: DefectType, witness: Witness | None
) -> None:
    m = merged("M-001", TERM, blocks_testing=True, defect_type=defect_type, witness=witness)

    findings, _ = run(doc, [m], llm_with(_writable()), tmp_path)

    t = only_draft(findings)
    assert findings[0].blocks_testing and t.contested and CONTESTED_NOTE in t.note
    assert "по-прежнему мешает" in draft_view(findings[0]).status  # type: ignore[union-attr]
    assert draft_stats([findings[0]]).complete == 0  # type: ignore[union-attr]


def test_unchecked_witness_does_not_keep_blocks_testing(doc: ParsedDocument, tmp_path: Path) -> None:
    m = merged(
        "M-001",
        TERM,
        blocks_testing=True,
        defect_type=DefectType.AMBIGUOUS,
        witness=Witness(scenario="Сценарий.", checked=False),
    )

    findings, _ = run(doc, [m], llm_with(_writable()), tmp_path)

    assert not findings[0].blocks_testing and not only_draft(findings).contested


def test_can_write_without_confirmed_values_is_not_accepted(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(expected="Штраф начислен.", can_write=True, gaps=[])

    findings, diagnostics = run(doc, [merged(blocks_testing=True)], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert not t.complete and findings[0].blocks_testing
    assert "не принят" in t.note
    assert any("не принято" in d.action for d in diagnostics if d.code == LLM_TEST_DRAFT_CHECKED)


def test_draft_without_gap_and_claim_takes_gap_from_search(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(expected="Штраф начислен.", gaps=[])

    findings, _ = run(doc, [merged(test_gap="неизвестен размер штрафа")], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert [g.missing for g in t.gap_items] == ["неизвестен размер штрафа"] and not t.complete


def test_draft_without_gap_claim_or_search_gap_is_dropped(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(expected="Штраф начислен.", gaps=[])

    findings, _ = run(doc, [merged()], llm_with(d), tmp_path)

    assert findings[0].test_draft is None


def test_unknown_finding_and_second_draft_are_ignored(doc: ParsedDocument, tmp_path: Path) -> None:
    other = draft("M-099")
    second = draft(expected="Другой [?1].")

    findings, diagnostics = run(doc, [merged()], llm_with(draft(), other, second), tmp_path)

    assert only_draft(findings).expected == "Штраф составляет [?1] за день."
    assert any("несуществующие находки" in d.action for d in diagnostics)


# ---------------------------------------------------------------- отказы модели


def test_refusal_keeps_findings_and_reports_it(doc: ParsedDocument, tmp_path: Path) -> None:
    llm = FakeLLM(
        responses={(LLMStep.TEST_DRAFT, 1): cli_payload("Не могу помочь с этим запросом.", is_error=True)}
    )
    found = [merged(), merged("M-002", prop=Property.UNAMBIGUOUS)]

    findings, diagnostics = run(doc, found, llm, tmp_path)

    assert findings == found
    failed = [d for d in diagnostics if d.code == LLM_TEST_DRAFT_FAILED]
    assert len(failed) == 1 and failed[0].level is DiagLevel.WARNING and "2" in failed[0].action


def test_reply_without_json_keeps_findings(doc: ParsedDocument, tmp_path: Path) -> None:
    llm = FakeLLM(responses={(LLMStep.TEST_DRAFT, 1): cli_payload("Тест написать можно.")})

    findings, diagnostics = run(doc, [merged()], llm, tmp_path)

    assert findings[0].test_draft is None
    assert [d.code for d in diagnostics] == [LLM_TEST_DRAFT_FAILED]


def test_missing_fixture_is_a_diagnostic_not_a_crash(doc: ParsedDocument, tmp_path: Path) -> None:
    cfg = make_cfg(doc, tmp_path)

    out = draft_tests([merged()], doc, FakeLLM(tmp_path / "nothing"), cfg)

    assert out.findings[0].test_draft is None
    assert [d.code for d in out.diagnostics] == [LLM_TEST_DRAFT_FAILED]
    assert LLM_NO_FIXTURE not in {d.code for d in out.diagnostics}  # причина в тексте, не в коде


def test_parse_reply_raises_llm_error_without_json() -> None:
    req = build_stub_request()
    resp = parse_cli_payload(cli_payload("нет json"), req, Backend.FAKE)

    with pytest.raises(LLMError):
        parse_reply(resp)


def build_stub_request() -> Any:
    return LLMRequest(
        step=LLMStep.TEST_DRAFT, call_index=1, model="m", effort=Effort.HIGH, system="s", prompt="p"
    )


def test_malformed_entries_are_skipped_one_by_one(doc: ParsedDocument, tmp_path: Path) -> None:
    bad: dict[str, Any] = {"note": "нет поля finding"}

    findings, _ = run(doc, [merged()], llm_with(bad, draft()), tmp_path)

    assert only_draft(findings).gap_items


# ---------------------------------------------------------------- выбор находок и пачки


def test_selection_by_property_blocks_testing_and_critic() -> None:
    assert needs_draft(merged(prop=Property.VERIFIABLE))
    assert needs_draft(merged(prop=Property.UNAMBIGUOUS))
    assert needs_draft(merged(prop=Property.COMPLETE, blocks_testing=True))
    assert not needs_draft(merged(prop=Property.COMPLETE))
    dropped = CriticDecision(action=CriticAction.DROP, reason="нет")
    assert not needs_draft(merged(prop=Property.VERIFIABLE, critic=dropped))


def test_no_targets_means_no_model_call(doc: ParsedDocument, tmp_path: Path) -> None:
    llm = FakeLLM(tmp_path / "nothing")
    found = [merged(prop=Property.COMPLETE)]

    findings, diagnostics = run(doc, found, llm, tmp_path)

    assert findings == found and not diagnostics and not llm.requests


def test_many_findings_go_in_batches_and_one_failed_batch_keeps_the_rest(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    found = [merged(f"M-{n:03d}") for n in range(1, BATCH_SIZE * 2 + 2)]
    llm = FakeLLM(
        responses={
            (LLMStep.TEST_DRAFT, 1): cli_payload(reply(draft("M-001"))),
            (LLMStep.TEST_DRAFT, 3): cli_payload(reply(draft(f"M-{BATCH_SIZE * 2 + 1:03d}"))),
        }
    )

    findings, diagnostics = run(doc, found, llm, tmp_path)

    assert sorted(r.call_index for r in llm.requests) == [1, 2, 3]
    assert [d.code for d in diagnostics].count(LLM_TEST_DRAFT_FAILED) == 1
    assert {f.mid for f in findings if f.test_draft} == {"M-001", f"M-{BATCH_SIZE * 2 + 1:03d}"}


# ---------------------------------------------------------------- запрос и промпт


def test_request_has_document_findings_and_step_model(doc: ParsedDocument, tmp_path: Path) -> None:
    cfg = make_cfg(doc, tmp_path)
    f = merged("M-004", FINE, test_gap="размер штрафа за день")

    req = build_request([f], doc, cfg, 1)

    model = cfg.model_for(LLMStep.TEST_DRAFT)
    assert req.step is LLMStep.TEST_DRAFT and req.json_schema is None
    assert (req.model, req.effort) == (model.model, model.effort)
    assert "### M-004" in req.prompt and FINE in req.prompt and "размер штрафа за день" in req.prompt
    assert "{document}" not in req.prompt and "{findings}" not in req.prompt
    assert EXTEND in req.prompt  # весь документ


def test_prompt_example_matches_reply_schema() -> None:
    text = SYSTEM_PROMPT.read_text(encoding="utf-8")
    block = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)[-1]

    example = DraftReplySchema.model_validate(json.loads(block))

    assert example.drafts and any(d.can_write for d in example.drafts) and any(d.gaps for d in example.drafts)


# ---------------------------------------------------------------- связь с вопросами


def _question(qid: str, text: str, findings: Sequence[str]) -> Question:
    return Question(id=qid, priority=QuestionPriority.P1, question=text, related_findings=list(findings))


def _verified(doc: ParsedDocument, tmp_path: Path, *gaps: str) -> Verification:
    f = finding(doc, "F-01", [FINE], prop=Property.VERIFIABLE, defect=DefectType.UNVERIFIABLE)
    items = [DraftGap(number=i, missing=g) for i, g in enumerate(gaps, 1)]
    td = TestDraft(expected="x", gaps=list(gaps), gap_items=items)
    return Verification(findings=[f.model_copy(update={"test_draft": td})])


def test_gap_is_linked_to_the_question_of_its_finding(doc: ParsedDocument, tmp_path: Path) -> None:
    ver = _verified(doc, tmp_path, "размер штрафа")

    linked = link_gaps(ver, [_question("Q-02", "Каков размер штрафа за день?", ["F-01"])])

    gap = linked.findings[0].test_draft.gap_items[0]  # type: ignore[union-attr]
    assert gap.question_id == "Q-02" and not gap.question_withdrawn


def test_gap_takes_the_closest_of_several_questions(doc: ParsedDocument, tmp_path: Path) -> None:
    ver = _verified(doc, tmp_path, "размер штрафа за день", "срок продления книги")
    questions = [
        _question("Q-01", "На сколько дней можно продлить книгу и какой срок продления?", ["F-01"]),
        _question("Q-02", "Каков размер штрафа за каждый день просрочки?", ["F-01"]),
    ]

    gaps = link_gaps(ver, questions).findings[0].test_draft.gap_items  # type: ignore[union-attr]

    assert [g.question_id for g in gaps] == ["Q-02", "Q-01"]


def test_gap_without_question_stays_unlinked(doc: ParsedDocument, tmp_path: Path) -> None:
    ver = _verified(doc, tmp_path, "размер штрафа")

    linked = link_gaps(ver, [_question("Q-02", "О другом", ["F-09"])])

    assert linked.findings[0].test_draft.gap_items[0].question_id is None  # type: ignore[union-attr]


def test_gap_of_withdrawn_question_is_marked(doc: ParsedDocument, tmp_path: Path) -> None:
    ver = _verified(doc, tmp_path, "размер штрафа")
    gone = _question("Q-03", "Каков размер штрафа?", ["F-01"])
    answer = locate_quote(doc, FIVE_DAYS)

    linked = link_gaps(ver, [], [WithdrawnQuestion(question=gone, reason="есть в документе", answer=answer)])

    gap = linked.findings[0].test_draft.gap_items[0]  # type: ignore[union-attr]
    assert gap.question_id == "Q-03" and gap.question_withdrawn


# ---------------------------------------------------------------- конвейер и отчёт


def _run_md(tmp_path: Path) -> Report:
    cfg = make_cfg_for(REQUIREMENTS[DocKind.MD], tmp_path)
    run_pipeline(cfg)
    return Report.model_validate_json((tmp_path / "out" / "report.json").read_text(encoding="utf-8"))


def make_cfg_for(path: Path, tmp_path: Path) -> Any:
    return load_config(path, tmp_path / "out" / "report.md", backend=Backend.FAKE)


def test_pipeline_report_has_drafts_with_gaps_linked_to_questions(tmp_path: Path) -> None:
    report = _run_md(tmp_path)

    assert report.verification is not None
    drafted = [f for f in report.verification.findings if f.test_draft and f.test_draft.gap_items]
    assert drafted
    ids = {q.id for q in report.questions}
    gaps = [g for f in drafted for g in f.test_draft.gap_items]  # type: ignore[union-attr]
    assert gaps and all(g.question_id in ids for g in gaps)
    for f in drafted:
        t = f.test_draft
        assert t is not None
        text = " ".join([t.precondition, *t.steps, t.expected])
        assert all(f"[?{g.number}]" in text for g in t.gap_items)


def test_markdown_report_shows_attempt_with_gaps_and_question(tmp_path: Path) -> None:
    report = _run_md(tmp_path)

    markdown = render_md(report)

    assert "**Попытка теста:**" in markdown and "Пропуск [?1]" in markdown
    assert re.search(r"Пропуск \[\?1\] \([^)]+\): .+ → Q-\d\d", markdown)
    assert "- Попытка теста: черновиков" in markdown


def _with_draft() -> Report:
    report = load("full")
    assert report.verification is not None
    f = report.verification.findings[0]
    td = TestDraft(
        precondition="Бронь оформлена за три дня до заезда.",
        action="Отменить бронь",
        expected="Гостю возвращается [?1] стоимости.",
        gaps=["доля возврата при отмене за три дня"],
        steps=["Отменить бронь в личном кабинете."],
        gap_items=[
            DraftGap(number=1, missing="доля возврата при отмене за три дня", part="ожидаемый результат")
        ],
    )
    findings = [f.model_copy(update={"test_draft": td}), *report.verification.findings[1:]]
    return report.model_copy(
        update={"verification": report.verification.model_copy(update={"findings": findings})}
    )


def test_pdf_shows_the_same_attempt_block_as_markdown() -> None:
    report = _with_draft()

    markdown, text = render_md(report), pdf_text(render_pdf(report))

    for needle in (
        "Попытка теста",
        "тест написать нельзя без решения автора: пропусков 1",
        "Бронь оформлена за три дня до заезда.",
        "Гостю возвращается [?1] стоимости.",
        "доля возврата при отмене за три дня",
        "отдельного вопроса автору нет",
        "черновиков 1; с пропусками 1",
    ):
        assert needle in markdown, needle
        assert needle in text, needle


def test_report_without_attempts_has_no_block() -> None:
    report = load("full")

    assert "Попытка теста" not in render_md(report)
    assert draft_stats(report.verification.findings if report.verification else []) is None


def test_view_for_complete_and_rejected_drafts() -> None:
    report = _with_draft()
    assert report.verification is not None
    f = report.verification.findings[0]
    complete = TestDraft(expected="x", complete=True)
    rejected = TestDraft(expected="x")

    assert "написать можно" in draft_view(f.model_copy(update={"test_draft": complete})).status  # type: ignore[union-attr]
    assert "не принят" in draft_view(f.model_copy(update={"test_draft": rejected})).status  # type: ignore[union-attr]
    assert draft_view(f.model_copy(update={"test_draft": None})) is None
    stats = draft_stats([f.model_copy(update={"test_draft": complete})])
    assert stats is not None and "тест написать можно 1" in stats_line(stats)


def test_block_ids_in_draft_text_become_places(doc: ParsedDocument) -> None:
    f = finding(doc, "F-01", [FINE], prop=Property.VERIFIABLE, defect=DefectType.UNVERIFIABLE)
    block = doc.blocks[0].id
    td = TestDraft(
        expected=f"Результат по {block} [?1].",
        steps=[f"Открыть {block}."],
        gaps=[f"что сказано в {block}"],
        gap_items=[DraftGap(number=1, missing=f"что сказано в {block}")],
    )

    fixed = humanize_finding(f.model_copy(update={"test_draft": td}), RefNamer(doc, None)).test_draft

    assert fixed is not None and block not in fixed.expected + fixed.steps[0] + fixed.gaps[0]
    assert fixed.gap_items[0].missing == fixed.gaps[0] and "[?1]" in fixed.expected


def test_every_gap_has_its_marker_in_the_text(doc: ParsedDocument, tmp_path: Path) -> None:
    unplaced = draft(
        expected="Штраф начислен.",
        gaps=[{"number": 1, "missing": "размер штрафа", "part": "expected"}],
    )

    findings, _ = run(doc, [merged()], llm_with(unplaced), tmp_path)

    t = only_draft(findings)
    assert t.expected == "Штраф начислен. [?1]" and t.gap_items[0].missing == "размер штрафа"


def test_replaced_value_does_not_clash_with_numbers_in_the_same_text(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    d = draft(
        precondition="Лимит 700 ₸.",
        expected="Лимит четырнадцать дней, комиссия 0 ₸, остаток 1 ₸.",
        gaps=[],
        values=[{"value": "четырнадцать дней", "quote": "Срок выдачи составляет пять лет."}],
    )

    findings, _ = run(doc, [merged()], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert "\x00" not in t.expected and not any(0xE000 <= ord(c) <= 0xF8FF for c in t.expected)
    assert t.expected == "Лимит [?1], комиссия [?2] ₸, остаток [?3] ₸."
    assert [g.replaced for g in t.gap_items] == ["четырнадцать дней", "0", "1"]


def test_step_reference_is_not_a_value_but_a_big_number_after_the_word_step_is(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    d = draft(
        steps=["Первое действие.", "Второе действие."],
        expected="Шаг 2: итог выполнен. Шаг 7: ничего. Шаг 3 000 рублей.",
        gaps=[],
    )

    findings, _ = run(doc, [merged(test_gap="x")], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert t.expected.startswith("Шаг 2: итог выполнен. Шаг [?1]: ничего. Шаг [?2] рублей.")


def test_gap_without_marker_goes_to_the_part_the_model_named(doc: ParsedDocument, tmp_path: Path) -> None:
    d = draft(
        precondition="Бронь оформлена.",
        expected="Бронь отменена.",
        gaps=[{"number": 1, "missing": "какой тип брони", "part": "precondition"}],
    )

    findings, _ = run(doc, [merged()], llm_with(d), tmp_path)

    t = only_draft(findings)
    assert t.precondition == "Бронь оформлена. [?1]" and t.expected == "Бронь отменена."
    assert t.gap_items[0].part == "предусловие"


def test_unexpected_error_in_one_batch_keeps_the_other_batches(
    doc: ParsedDocument, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = step.build_draft

    def flaky(entry: Any, finding: MergedFinding, document: ParsedDocument, log: Any) -> Any:
        if finding.mid == f"M-{BATCH_SIZE + 1:03d}":
            raise KeyError("сбой в проверке")
        return real(entry, finding, document, log)

    monkeypatch.setattr(step, "build_draft", flaky)
    found = [merged(f"M-{n:03d}") for n in range(1, BATCH_SIZE + 2)]
    llm = FakeLLM(
        responses={
            (LLMStep.TEST_DRAFT, 1): cli_payload(reply(draft("M-001"))),
            (LLMStep.TEST_DRAFT, 2): cli_payload(reply(draft(f"M-{BATCH_SIZE + 1:03d}"))),
        }
    )

    findings, diagnostics = run(doc, found, llm, tmp_path)

    assert [f.mid for f in findings if f.test_draft] == ["M-001"]
    failed = [d for d in diagnostics if d.code == LLM_TEST_DRAFT_FAILED]
    assert len(failed) == 1 and "внутренняя ошибка" in failed[0].message
