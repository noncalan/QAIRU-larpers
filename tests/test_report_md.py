"""Отчёт в Markdown: снапшоты на выдуманных фикстурах, статус каждого требования, согласованность чисел.

Фикстуры ``tests/fixtures/report/*.report.json`` собирает ``tests/report_factory.py``. Если отчёт
изменился намеренно, посмотрите разницу глазами и обновите эталоны:
``UPDATE_SNAPSHOTS=1 uv run pytest tests/test_report_md.py``.
"""

from __future__ import annotations

import json
import os
import re

import pytest

import report_factory as factory
from conftest import ROOT
from validator.config import load_config
from validator.models import (
    Backend,
    Conclusion,
    DiagLevel,
    Diagnostic,
    Finding,
    ParsedDocument,
    Property,
    Quote,
    QuoteMatch,
    Report,
    ReportStatus,
    RunContext,
    SearchInput,
    Severity,
    TrafficLight,
    Verdict,
)
from validator.models import DefectType as D
from validator.report import build_report, render_md
from validator.report.fmt import cell, where
from validator.report.render_md import NO_VIOLATIONS, requirement_rows

FIXTURES = factory.FIXTURES
CASES = tuple(factory.CASES)
SECTION_ORDER = (
    "## Итог на первой странице",
    "## 1. Паспорт прогона",
    "## 2. Диагностика входа",
    "## 3. Как агент нарезал требования",
    "## 4. Проверка каждого требования",
    "## 5. Проверка всего набора требований",
    "## 6. Вопросы автору ТЗ",
    "## 7. Сводка",
    "## 8. Итог",
    "## 9. Метрики прогона",
    "## 10. Диагностика прогона",
)


def load(name: str) -> Report:
    return Report.model_validate_json((FIXTURES / f"{name}.report.json").read_text(encoding="utf-8"))


def section(md: str, heading: str) -> str:
    """Текст раздела от его заголовка до следующего ``## ``."""
    start = md.index(heading)
    nxt = md.find("\n## ", start + 1)
    return md[start : nxt if nxt != -1 else len(md)]


# ---------------------------------------------------------------- снапшоты


@pytest.mark.parametrize("name", CASES)
def test_report_matches_snapshot(name: str) -> None:
    expected_path = FIXTURES / f"{name}.expected.md"

    actual = render_md(load(name))

    if os.environ.get("UPDATE_SNAPSHOTS"):
        expected_path.write_text(actual, encoding="utf-8", newline="\n")
    assert actual == expected_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", CASES)
def test_json_fixture_is_up_to_date_with_factory(name: str) -> None:
    """Фикстура JSON это ровно то, что собирает фабрика: правка модели без пересборки видна сразу."""
    built = factory.CASES[name]()

    assert json.loads((FIXTURES / f"{name}.report.json").read_text(encoding="utf-8")) == json.loads(
        built.model_dump_json()
    )


def test_report_json_keys_follow_model_field_order() -> None:
    """Стабильный порядок ключей: ``report.json`` сериализует поля в порядке объявления модели."""
    data = json.loads(factory.full_report().model_dump_json())

    assert list(data) == list(Report.model_fields)


# ---------------------------------------------------------------- разделы и их порядок


def test_sections_follow_task_order() -> None:
    md = render_md(load("full"))

    positions = [md.index(heading) for heading in SECTION_ORDER]

    assert positions == sorted(positions)


def test_passport_shows_raw_search_input_only_when_it_is_not_default() -> None:
    report = load("full")
    raw = report.model_copy(
        update={"passport": report.passport.model_copy(update={"search_input": SearchInput.RAW})}
    )

    assert "без ID блоков" not in render_md(report)
    assert "проходов поиска: 3, поиск по тексту без ID блоков |" in render_md(raw)


def test_first_page_comes_before_passport_and_answers_the_main_question() -> None:
    md = render_md(load("full"))
    first_page = md[: md.index("## 1. Паспорт прогона")]

    assert "🔴" in first_page
    assert "Что исправить первым" in first_page
    assert "Какие вопросы задать автору первыми" in first_page
    assert "нарушений: **4**" in first_page


def test_unread_input_has_only_passport_diagnostics_and_conclusion() -> None:
    md = render_md(load("unread"))

    assert "## 1. Паспорт прогона" in md
    assert "IN-011" in md
    assert "Проверка не выполнена" in md
    assert "## 3. Как агент нарезал требования" not in md
    assert "Traceback" not in md


def test_empty_sections_say_so_instead_of_disappearing() -> None:
    md = render_md(load("clean"))

    assert "Нарушений в отдельных требованиях не найдено." in md
    assert "Нарушений уровня набора" in md
    assert "Вопросов нет: нарушений и неясностей не найдено." in md
    assert "Нарушений, которые нужно исправить до начала работ, не найдено." in md
    assert "Замечаний к прогону нет." in md


def test_questions_step_failure_is_not_reported_as_no_questions() -> None:
    report = load("clean").model_copy(
        update={
            "diagnostics": [
                Diagnostic(
                    code="LLM-031",
                    level=DiagLevel.ERROR,
                    message="Шаг вопросов не отработал",
                    step="questions",
                )
            ]
        }
    )

    md = render_md(report)

    assert "Вопросы не сформированы" in md
    assert "Вопросов нет" not in md


# ---------------------------------------------------------------- статус каждого требования


@pytest.mark.parametrize("name", ["full", "clean"])
def test_every_requirement_has_a_status_row(name: str) -> None:
    report = load(name)
    assert report.segmentation is not None
    md = render_md(report)
    block = section(md, "## 4. Проверка каждого требования")

    for req in report.segmentation.requirements:
        assert re.search(rf"^\| {req.id} \| .+ \| .+ \|$", block, flags=re.MULTILINE), req.id


def test_requirement_status_is_no_violations_or_finding_links() -> None:
    rows = {row.requirement.id: row.status for row in requirement_rows(load("full"))}

    assert rows["R-01"] == NO_VIOLATIONS
    assert rows["R-02"] == "F-01 (критично, нарушение)"
    assert rows["R-05"] == "F-03 (важно, неясно)"


def test_every_linked_finding_has_a_card() -> None:
    report = load("full")
    md = render_md(report)

    linked = {fid for row in requirement_rows(report) for fid in row.finding_ids}

    assert linked
    assert all(f"### {fid} · " in md for fid in linked)


# ---------------------------------------------------------------- счётчики и таблицы


@pytest.mark.parametrize("name", ["full", "clean"])
def test_summary_counters_match_tables_and_cards(name: str) -> None:
    report = load(name)
    assert report.verification is not None and report.segmentation is not None
    s = report.verification.summary
    md = render_md(report)
    segmentation_rows = re.findall(
        r"^\| R-\d+(?:\.\d+)? \|", section(md, "## 3. Как агент нарезал требования"), re.M
    )
    status_rows = re.findall(
        r"^\| R-\d+(?:\.\d+)? \|", section(md, "## 4. Проверка каждого требования"), re.M
    )
    cards = re.findall(r"^### F-\d+ · ", md, re.M)
    summary_block = section(md, "## 7. Сводка")

    assert len(segmentation_rows) == len(status_rows) == s.requirements_total
    assert len(cards) == s.findings_total
    assert f"Требований: **{s.requirements_total}**" in summary_block
    assert f"Находок: **{s.findings_total}** (нарушений {s.violations}, «неясно» {s.unclear}" in summary_block
    assert f"Критичных: **{s.critical}**" in summary_block
    assert f"Вопросов автору: {len(report.questions)}" in summary_block


def test_set_level_findings_are_only_in_set_section() -> None:
    md = render_md(load("full"))
    requirement_part = section(md, "## 4. Проверка каждого требования")
    set_part = section(md, "## 5. Проверка всего набора требований")

    assert "### F-01 · " in set_part and "### F-01 · " not in requirement_part
    assert "### F-02 · " in requirement_part and "### F-02 · " not in set_part


def test_pipe_in_quote_does_not_break_the_table() -> None:
    md = render_md(load("full"))
    row = next(line for line in md.splitlines() if line.startswith("| R-04 |"))

    assert "2 с \\| при 100" in row
    assert len(re.findall(r"(?<!\\)\|", row)) == 7


def test_questions_are_linked_from_findings() -> None:
    md = render_md(load("full"))
    card = md[md.index("### F-02 · ") : md.index("### F-03 · ")]

    assert "**Вопрос автору:** Q-02" in card


def test_lint_only_finding_does_not_say_found_in_zero_passes() -> None:
    """Живой прогон #36: слияние ставит находке только линтера «0 из 3», отчёт пишет это словами."""
    report = load("full")
    assert report.verification is not None
    findings = [
        f.model_copy(update={"found_in": 0, "of": 3}) if f.id == "F-04" else f
        for f in report.verification.findings
    ]
    report = report.model_copy(
        update={"verification": report.verification.model_copy(update={"findings": findings})}
    )

    md = render_md(report)
    card = md[md.index("### F-04 · ") : md.index("### F-05 · ")]

    assert "в 0 из 3 проходов" not in card
    assert (
        "**Найдено:** правилом «Несколько обязательств в одном предложении»; проходы модели этого не "
        "отметили (0 из 3)"
    ) in card


# ---------------------------------------------------------------- места в документе


def test_where_for_markdown_pdf_docx_and_ocr() -> None:
    md_anchor = factory._anchor(0, 5, 41)
    pdf_anchor = factory._anchor(0, 5, 41, page=4)
    docx_paragraph = factory._anchor(0, 5, 41, paragraph=17)
    docx_table = factory._anchor(0, 5, 41, table=2, row=3, column="Срок")
    ocr = factory._anchor(0, 5, 41, page=2, extraction=factory.Extraction.OCR)

    assert where(md_anchor, ["3 Права", "3.2 Роли"]) == "раздел «3 Права › 3.2 Роли», строка 41"
    assert where(pdf_anchor) == "стр. 4"
    assert where(docx_paragraph) == "абзац 17"
    assert where(docx_table) == "таблица 2, строка 3, колонка «Срок»"
    assert where(ocr) == "стр. 2, текст распознан (OCR)"
    assert where(None) == "место не определено"


def test_line_range_is_written_as_range() -> None:
    anchor = factory.Anchor(char_start=0, char_end=9, line_start=3, line_end=4)

    assert where(anchor) == "строки 3–4"


def test_ocr_quote_place_has_section_and_note() -> None:
    md = render_md(load("full"))

    assert "(раздел «Ограничения › Права», строка 40, текст распознан (OCR); сверено: точно)" in md


def test_cell_escapes_pipes_and_collapses_newlines() -> None:
    assert cell("a | b\n  c") == "a \\| b c"
    assert cell("x" * 10, limit=5) == "xxxx…"


# ---------------------------------------------------------------- сборка: первая страница и итог


def _quote(verified: bool) -> Quote:
    if verified:
        return factory._quote("текст", 0, 1)
    return Quote(text="нет такого", match=QuoteMatch.NOT_FOUND, score=0)


def _with_findings(found: list[Finding]) -> Report:
    base = factory.full_report()
    ver = factory._verification(found, factory.REQS, TrafficLight.YELLOW, "есть нарушения")
    return base.model_copy(update={"verification": ver, "questions": []})


def _f(n: int, severity: Severity, *, verdict: Verdict = Verdict.VIOLATION, verified: bool = True) -> Finding:
    return factory._finding(
        n,
        property=Property.VERIFIABLE,
        defect_type=D.UNVERIFIABLE,
        severity=severity,
        verdict=verdict,
        quotes=[_quote(verified)],
        requirement_ids=["R-01"],
        explanation="Объяснение.",
    )


def test_first_page_takes_only_verified_violations_sorted_by_severity() -> None:
    found = [
        _f(1, Severity.MINOR),
        _f(2, Severity.CRITICAL, verified=False),
        _f(3, Severity.CRITICAL, verdict=Verdict.UNCLEAR),
        _f(4, Severity.MAJOR),
        _f(5, Severity.CRITICAL),
    ]
    ctx = _context_from(_with_findings(found))

    report = build_report(ctx)

    assert report.conclusion.fix_first == ["F-05", "F-04", "F-01"]


def test_first_page_is_limited() -> None:
    found = [_f(n, Severity.MAJOR) for n in range(1, 9)]

    report = build_report(_context_from(_with_findings(found)))

    assert report.conclusion.fix_first == ["F-01", "F-02", "F-03", "F-04", "F-05"]


def test_blocking_questions_go_first_p0_before_p1_and_p2_never() -> None:
    qs = factory.questions()
    shuffled = [qs[1], qs[2], qs[0]]
    ctx = _context_from(factory.full_report().model_copy(update={"questions": shuffled}))

    report = build_report(ctx)

    assert report.conclusion.ask_first == ["Q-01", "Q-02"]


def test_report_does_not_recompute_severity_or_light() -> None:
    """Светофор и важность приходят от верификатора: отчёт их не пересчитывает."""
    report = load("full")
    assert report.verification is not None
    forced = report.verification.model_copy(
        update={"traffic_light": TrafficLight.GREEN, "traffic_reason": "тест"}
    )

    built = build_report(_context_from(report.model_copy(update={"verification": forced})))

    assert built.conclusion.traffic_light is TrafficLight.GREEN
    assert built.verification is not None
    assert [f.severity for f in built.verification.findings] == [
        f.severity for f in report.verification.findings
    ]


def test_failed_step_never_gives_green_light() -> None:
    clean = factory.clean_report()
    diag = Diagnostic(code="LLM-031", level=DiagLevel.ERROR, message="Шаг не отработал", step="search")
    ctx = _context_from(clean).model_copy(update={"diagnostics": [diag]})

    report = build_report(ctx)

    assert report.status is ReportStatus.PARTIAL
    assert report.conclusion.traffic_light is TrafficLight.YELLOW
    assert "не полностью" in report.conclusion.headline


def test_input_read_with_reservations_never_gives_green_light() -> None:
    """#126: код 3 (вход с оговорками) не даёт зелёного итога, заголовок называет оговорку."""
    clean = factory.clean_report()
    diag = Diagnostic(
        code="IN-050",
        level=DiagLevel.WARNING,
        message="Документ распознан OCR: цитаты сверены с распознанным текстом",
        step="ingest",
    )
    ctx = _context_from(clean).model_copy(update={"diagnostics": [diag]})

    report = build_report(ctx)

    assert report.conclusion.traffic_light is TrafficLight.YELLOW
    assert report.conclusion.headline.startswith(
        "Проверка выполнена с оговорками (документ распознан OCR: цитаты сверены с распознанным текстом). "
        "По прочитанному: "
    )


def test_unread_input_gives_not_performed_conclusion() -> None:
    unread = load("unread")

    assert unread.status is ReportStatus.NOT_PERFORMED
    assert isinstance(unread.conclusion, Conclusion)
    assert unread.conclusion.performed is False


def _context_from(report: Report) -> RunContext:
    """Минимальный ``RunContext`` из готового отчёта: проверяем сборку первой страницы и статуса."""
    cfg = load_config(
        ROOT / "data" / "input" / "requirements.md", ROOT / "out" / "unused.md", backend=Backend.FAKE
    )
    return RunContext(
        cfg=cfg,
        started_at=report.passport.started_at,
        finished_at=report.passport.finished_at or report.passport.started_at,
        exit_code=report.passport.exit_code,
        segmentation=report.segmentation,
        verification=report.verification,
        questions=list(report.questions),
        diagnostics=list(report.diagnostics),
        passport=report.passport,
        doc=_doc_stub(report),
    )


def _doc_stub(report: Report) -> ParsedDocument:
    info = report.document
    assert info is not None
    return ParsedDocument(
        path=info.path,
        name=info.name,
        kind=info.kind,
        sha256=info.sha256 or "0" * 64,
        size_bytes=info.size_bytes or 0,
        text="x",
        extraction=info.extraction,
    )


def test_markdown_marks_in_quotes_are_escaped_so_the_quote_reads_as_in_the_document() -> None:
    assert cell("поле *id* и <b>сумма</b> и `код`") == "поле \\*id\\* и \\<b>сумма\\</b> и \\`код\\`"
    assert cell("_метка_ и limit_kzt") == "\\_метка\\_ и limit_kzt"
