"""Первая страница, сводка и паспорт для лида QA (#123, #125): все критичные видны, без жаргона."""

from __future__ import annotations

import pymupdf

import report_factory as factory
from test_report_md import _context_from, _quote, load
from test_report_pdf import pdf_text
from validator.models import (
    Block,
    BlockKind,
    DraftValue,
    Finding,
    Property,
    Question,
    QuestionPriority,
    Report,
    Severity,
    StepMetrics,
    TestDraft,
    TokenUsage,
    Verdict,
)
from validator.models import DefectType as D
from validator.report import build_report, render_md, render_pdf
from validator.report.build import document_title
from validator.report.testdraft_view import draft_view


def _critical(
    n: int, *, reqs: list[str] | None = None, blocks: bool = True, defect: D = D.CONTRADICTION
) -> Finding:
    return factory._finding(
        n,
        property=Property.SET_CONSISTENT,
        defect_type=defect,
        severity=Severity.CRITICAL,
        blocks_testing=blocks,
        quotes=[_quote(True)],
        requirement_ids=reqs or ["R-01"],
        explanation=f"Суть критичного нарушения {n}.",
    )


def _report(found: list[Finding], questions: list[Question] | None = None) -> Report:
    base = factory.full_report()
    ver = factory._verification(found, factory.REQS)
    return base.model_copy(update={"verification": ver, "questions": questions or []})


def _first_page(md: str) -> str:
    return md[: md.index("## 1. Паспорт прогона")]


def test_first_page_lists_every_critical_finding_even_above_the_limit() -> None:
    found = [_critical(n) for n in range(1, 9)]

    report = build_report(_context_from(_report(found)))
    first = _first_page(render_md(report))

    assert len(report.conclusion.fix_first) == 8
    for f in found:
        assert f"**{f.id}** · критично · противоречие" in first
    assert "и ещё" not in first.split("### Что исправить первым")[1]


def test_blockers_first_then_wider_impact() -> None:
    narrow = _critical(1)
    no_test_impact = _critical(2, reqs=["R-01", "R-02", "R-03"], blocks=False)
    wide = _critical(3, reqs=["R-01", "R-02"])

    report = build_report(_context_from(_report([narrow, no_test_impact, wide])))

    assert report.conclusion.fix_first[:3] == ["F-03", "F-01", "F-02"]


def test_questions_for_the_first_fixes_are_asked_first() -> None:
    found = [_critical(1), _critical(2, reqs=["R-01", "R-02"])]
    questions = [
        factory.questions()[0].model_copy(update={"id": "Q-01", "related_findings": ["F-01"]}),
        factory.questions()[0].model_copy(update={"id": "Q-02", "related_findings": ["F-02"]}),
    ]
    for q in questions:
        assert q.priority is QuestionPriority.P0

    report = build_report(_context_from(_report(found, questions)))

    assert report.conclusion.fix_first[:2] == ["F-02", "F-01"]
    assert report.conclusion.ask_first == ["Q-02", "Q-01"]


def test_summary_has_one_critical_list_with_essence_and_a_separate_tz_class_line() -> None:
    unclear = factory._finding(
        9,
        property=Property.COMPLETE,
        defect_type=D.INCOMPLETE,
        severity=Severity.MAJOR,
        verdict=Verdict.UNCLEAR,
        quotes=[_quote(True)],
        requirement_ids=["R-01"],
        explanation="Неясно.",
    )
    report = build_report(_context_from(_report([_critical(1, defect=D.AMBIGUOUS), unclear])))

    md = render_md(report)
    summary = md[md.index("## 7. Сводка") : md.index("## 8. Итог")]

    assert "- Критичных: **1**:\n  - F-01 · двусмысленность · Суть критичного нарушения 1." in summary
    assert "любой важности: 0." in summary  # «неясно» F-09 не нарушение, F-01 не класс из ТЗ
    assert "Критичные классы по ТЗ" not in md


def test_conclusion_names_the_order_without_repeating_cards() -> None:
    md = render_md(build_report(_context_from(_report([_critical(1), _critical(2)]))))
    conclusion = md[md.index("## 8. Итог") : md.index("## 9. Метрики прогона")]

    assert "**Что исправить первым, по порядку:** F-01, F-02." in conclusion
    assert "Цитата" not in conclusion


def test_passport_speaks_plainly() -> None:
    report = load("full")
    usage = TokenUsage(
        input_tokens=16, output_tokens=500, cache_creation_input_tokens=900, cache_read_input_tokens=84
    )
    passport = report.passport.model_copy(
        update={
            "agent_version": "v1.0",
            "agent_commit": "abc1234",
            "tokens": usage,
            "models": {**report.passport.models, "critic": "claude-opus-5-5 · high"},
        }
    )
    steps = [StepMetrics(step="search", calls=3, tokens=usage)]
    report = report.model_copy(
        update={
            "passport": passport,
            "metrics": report.metrics.model_copy(update={"steps": steps}),
            "document": report.document.model_copy(update={"title": "Бронирование переговорных"})
            if report.document
            else None,
        }
    )

    md = render_md(report)
    passport_md = md[md.index("## 1. Паспорт прогона") : md.index("## 2. Диагностика входа")]

    assert "| Версия агента | v1.0, коммит abc1234 |" in passport_md
    assert "| Модели по шагам | Поиск дефектов (проходы): claude-opus-5-5 · medium |" in passport_md
    assert "Критик" not in passport_md  # шаг не вызывался в этом прогоне
    assert "на входе 1 000 (из них через кеш 984" in passport_md
    assert "| Документ | «Бронирование переговорных», файл " in passport_md


def test_document_title_is_the_first_heading() -> None:
    doc = factory.full_report()
    stub = _context_from(doc).doc
    assert stub is not None
    heading = Block(
        id="B001",
        kind=BlockKind.HEADING,
        text="# Бронирование  переговорных",
        anchor=factory.Anchor(char_start=0, char_end=5),
    )
    para = Block(
        id="B002", kind=BlockKind.PARAGRAPH, text="Текст.", anchor=factory.Anchor(char_start=6, char_end=12)
    )

    assert document_title(stub.model_copy(update={"blocks": [para, heading]})) == "Бронирование переговорных"
    assert document_title(stub.model_copy(update={"blocks": [para]})) is None


def test_test_draft_value_equal_to_its_quote_is_shown_once() -> None:
    value = DraftValue(
        value="Итог 50 ₸",
        quote="Итог 50 ₸",
        verified=True,
        anchor=factory.Anchor(char_start=0, char_end=5, page=3),
    )
    finding = _critical(1).model_copy(
        update={
            "test_draft": TestDraft(precondition="П.", steps=["Шаг"], expected="Итог 50 ₸", values=[value])
        }
    )

    view = draft_view(finding)

    assert view is not None
    assert view.values == ("«Итог 50 ₸» (стр. 3)",)


def test_witness_reading_refers_to_the_card_quote_by_number() -> None:
    md = render_md(load("full"))

    assert "  - по цитате 1 → отмена отклонена" in md
    assert "  - по цитате 2 → бронь отменена" in md


def test_pdf_finding_and_question_numbers_link_to_their_cards() -> None:
    data = render_pdf(load("full"))

    with pymupdf.open(stream=data, filetype="pdf") as doc:
        links = [link for page in doc for link in page.get_links() if link.get("kind") == pymupdf.LINK_GOTO]

    assert len(links) >= 5
    assert "F-01" in pdf_text(data)
