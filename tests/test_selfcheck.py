"""Самопроверка отчёта по ТЗ п. 6 (#43): целый отчёт проходит, испорченный ловится, правок задним числом нет.

Испорчен каждый раз один признак: удалён раздел, счётчик сводки не равен числу строк, находка без якоря,
требование выпало из таблицы, матрица разошлась с находками. На выдуманных отчётах из ``report_factory``
и на сквозном прогоне ``fake``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import report_factory as factory
from conftest import REQUIREMENTS
from validator import pipeline
from validator.config import load_config
from validator.errors import INT_SELFCHECK_FAILED, INT_SELFCHECK_WARNING, INT_STEP_DEGRADED
from validator.models import (
    Backend,
    CellState,
    CoverageCell,
    DiagLevel,
    DocKind,
    DocumentInfo,
    DocumentPart,
    ExitCode,
    PassResult,
    Property,
    QuoteMatch,
    Report,
    SelfCheck,
    SelfCheckItem,
)
from validator.report import render_md
from validator.report.coverage import build_coverage
from validator.report.selfcheck import TZ_SECTIONS, check_report, selfcheck_diagnostics


def full() -> Report:
    return factory.full_report().model_copy(update={"selfcheck": None})


def check(report: Report, markdown: str | None = None, doc_text: str | None = None) -> SelfCheck:
    return check_report(report, markdown or render_md(report), doc_text)


def failed_ids(sc: SelfCheck) -> set[str]:
    return {item.id for item in sc.items if not item.passed}


def without_section(markdown: str, title: str) -> str:
    """Markdown без одного раздела ``##`` (вместе с содержимым)."""
    lines, skipping, kept = markdown.splitlines(), False, []
    for line in lines:
        if line.startswith("## "):
            skipping = title in line
        if not skipping:
            kept.append(line)
    return "\n".join(kept) + "\n"


# ---------------------------------------------------------------- целый отчёт


@pytest.mark.parametrize("name", ["full", "clean"])
def test_intact_report_passes_every_check(name: str) -> None:
    report = factory.CASES[name]()

    sc = check(report)

    assert sc.passed and sc.integrity_ok
    assert failed_ids(sc) == set()
    assert (sc.sections_found, sc.sections_total) == (6, 6)
    assert sc.findings_anchored == sc.findings_total == len(report.verification.findings)  # type: ignore[union-attr]
    assert selfcheck_diagnostics(sc) == []


def test_summary_line_matches_the_example_from_the_task() -> None:
    sc = check(full())

    assert sc.summary.startswith("ТЗ п. 6: 6 из 6 разделов")
    assert "цитат в находках сверено 100 %" in sc.summary
    assert "счётчики сводки сходятся с таблицами" in sc.summary


def test_every_task_section_has_its_own_heading_in_the_report() -> None:
    markdown = render_md(full())

    for title in TZ_SECTIONS:
        assert any(line.startswith("## ") and line.endswith(title) for line in markdown.splitlines()), title


def test_check_only_reports_and_never_edits_the_report() -> None:
    report = full()
    before = report.model_dump_json()

    check(report)

    assert report.model_dump_json() == before


def test_unread_document_gets_not_applicable_check_without_failures() -> None:
    report = factory.unread_report()

    sc = check(report)

    assert not sc.applicable and sc.items == []
    assert "не применима" in sc.summary
    assert selfcheck_diagnostics(sc) == []


def test_report_with_failed_verifier_is_checked_without_findings() -> None:
    intact = full()
    report = intact.model_copy(
        update={
            "verification": None,
            "coverage": None,
            "conclusion": intact.conclusion.model_copy(update={"fix_first": []}),
        }
    )

    sc = check(report)

    assert sc.passed
    assert "проверка требований не выполнена" in sc.summary


def test_check_renders_markdown_itself_when_none_given() -> None:
    report = full()

    assert check_report(report).summary == check(report).summary


# ---------------------------------------------------------------- испорченный отчёт


def test_missing_section_is_caught_as_a_gap_not_as_broken_integrity() -> None:
    report = full()
    markdown = without_section(render_md(report), "Сводка")

    sc = check(report, markdown)

    assert failed_ids(sc) == {"sections"}
    assert sc.sections_found == 5
    sections = next(i for i in sc.items if i.id == "sections")
    assert not sections.integrity and "Сводка" in sections.failures[0]
    assert [d.code for d in selfcheck_diagnostics(sc) if "Разделы ТЗ" in d.message] == [INT_SELFCHECK_WARNING]


def test_removed_set_section_also_breaks_counters() -> None:
    report = full()
    markdown = without_section(render_md(report), "Проверка всего набора требований")

    sc = check(report, markdown)

    assert {"sections", "counters"} <= failed_ids(sc)
    assert INT_SELFCHECK_FAILED in {d.code for d in selfcheck_diagnostics(sc)}


def test_gap_in_sections_alone_does_not_raise_integrity_error() -> None:
    report = full()
    markdown = without_section(render_md(report), "Сводка")

    sc = check(report, markdown)

    diagnostics = selfcheck_diagnostics(sc)
    assert sc.sections_found == 5 and sc.integrity_ok
    assert [(d.code, d.level) for d in diagnostics] == [(INT_SELFCHECK_WARNING, DiagLevel.WARNING)]


def test_wrong_summary_counter_is_caught_as_integrity_failure() -> None:
    report = full()
    assert report.verification is not None
    summary = report.verification.summary.model_copy(
        update={"violations": report.verification.summary.violations + 1}
    )
    broken = report.model_copy(
        update={"verification": report.verification.model_copy(update={"summary": summary})}
    )

    sc = check(broken)

    assert "counters" in failed_ids(sc)
    counters = next(i for i in sc.items if i.id == "counters")
    assert counters.integrity and any("нарушений в сводке" in f for f in counters.failures)
    assert not sc.integrity_ok
    assert sc.summary.startswith("НАРУШЕНИЯ")
    assert INT_SELFCHECK_FAILED in {d.code for d in selfcheck_diagnostics(sc)}


def test_metrics_that_disagree_with_summary_are_caught() -> None:
    report = full()
    broken = report.model_copy(update={"metrics": report.metrics.model_copy(update={"merged_total": 99})})

    sc = check(broken)

    assert failed_ids(sc) == {"metrics"}
    assert sc.integrity_ok
    assert [d.code for d in selfcheck_diagnostics(sc)] == [INT_SELFCHECK_WARNING]


def test_finding_without_anchor_is_caught_as_integrity_failure() -> None:
    report = full()
    assert report.verification is not None
    first = report.verification.findings[0]
    stripped = first.model_copy(
        update={"quotes": [q.model_copy(update={"anchor": None}) for q in first.quotes]}
    )
    findings = [stripped, *report.verification.findings[1:]]
    broken = report.model_copy(
        update={"verification": report.verification.model_copy(update={"findings": findings})}
    )

    sc = check(broken)

    assert "anchors" in failed_ids(sc)
    anchors = next(i for i in sc.items if i.id == "anchors")
    assert anchors.integrity and anchors.failures[0].startswith(first.id)
    assert sc.findings_anchored == sc.findings_total - 1
    assert INT_SELFCHECK_FAILED in {d.code for d in selfcheck_diagnostics(sc)}


def test_unverified_quote_in_a_finding_is_caught() -> None:
    report = full()
    assert report.verification is not None
    first = report.verification.findings[0]
    quote = first.quotes[0].model_copy(
        update={"match": QuoteMatch.NOT_FOUND, "located": None, "anchor": None}
    )
    findings = [first.model_copy(update={"quotes": [quote]}), *report.verification.findings[1:]]
    broken = report.model_copy(
        update={"verification": report.verification.model_copy(update={"findings": findings})}
    )

    sc = check(broken, render_md(report))

    assert {"anchors", "quotes"} <= failed_ids(sc)


def test_quote_that_is_not_where_the_anchor_points_is_caught_against_document_text() -> None:
    report = full()
    assert report.verification is not None
    quote = report.verification.findings[0].quotes[0]
    assert quote.anchor is not None and quote.located
    text = "x" * quote.anchor.char_end + "y" * 50

    sc = check(report, doc_text=text)

    assert "quotes" in failed_ids(sc)


def test_quote_recheck_passes_when_document_has_the_text_at_the_anchor() -> None:
    report = full()
    assert report.verification is not None
    text = ["?"] * 5000
    for f in report.verification.findings:
        for q in f.quotes:
            if q.anchor and q.located:
                text[q.anchor.char_start : q.anchor.char_end] = list(q.located)  # длина фрагмента та же
    doc_text = "".join(text)

    sc = check(report, doc_text=doc_text)

    assert sc.quotes_rechecked > 0
    assert "quotes" not in failed_ids(sc)


def test_requirement_row_missing_from_the_table_is_caught() -> None:
    report = full()
    markdown = render_md(report)
    broken = "\n".join(line for line in markdown.splitlines() if not line.startswith("| R-03 |")) + "\n"

    sc = check(report, broken)

    assert "requirements" in failed_ids(sc)
    assert any("R-03" in f for item in sc.items for f in item.failures)


def test_requirement_without_status_is_caught() -> None:
    report = full()
    assert report.verification is not None
    status = {k: v for k, v in report.verification.requirement_status.items() if k != "R-02"}
    broken = report.model_copy(
        update={"verification": report.verification.model_copy(update={"requirement_status": status})}
    )

    assert "requirements" in failed_ids(check(broken))


def test_finding_without_card_is_caught() -> None:
    report = full()
    markdown = render_md(report).replace("### F-02 ·", "#### F-02 ·")

    sc = check(report, markdown)

    assert "cards" in failed_ids(sc)


def test_matrix_that_lost_a_finding_is_caught() -> None:
    report = full()
    assert report.coverage is not None
    row = report.coverage.rows[2]
    cells = {
        p: c.model_copy(update={"state": CellState.CLEAN, "finding_ids": []}) for p, c in row.cells.items()
    }
    rows = [*report.coverage.rows[:2], row.model_copy(update={"cells": cells}), *report.coverage.rows[3:]]
    broken = report.model_copy(update={"coverage": report.coverage.model_copy(update={"rows": rows})})

    sc = check(broken)

    assert "matrix" in failed_ids(sc)
    assert any("F-02" in f for f in next(i for i in sc.items if i.id == "matrix").failures)


def test_matrix_cell_with_a_finding_of_another_property_is_caught() -> None:
    report = full()
    assert report.coverage is not None
    row = report.coverage.rows[2]
    cells = dict(row.cells)
    cells[Property.NECESSARY] = row.cells[Property.VERIFIABLE]  # F-02 «Проверяемость» под «Необходимостью»
    rows = [*report.coverage.rows[:2], row.model_copy(update={"cells": cells}), *report.coverage.rows[3:]]
    broken = report.model_copy(update={"coverage": report.coverage.model_copy(update={"rows": rows})})

    assert "matrix" in failed_ids(check(broken))


def test_matrix_row_for_set_property_pointing_at_requirement_finding_is_caught() -> None:
    report = full()
    assert report.coverage is not None
    set_rows = list(report.coverage.set_rows)
    set_rows[0] = set_rows[0].model_copy(update={"finding_ids": ["F-02"], "state": CellState.VIOLATION})
    broken = report.model_copy(update={"coverage": report.coverage.model_copy(update={"set_rows": set_rows})})

    assert "matrix" in failed_ids(check(broken))


def matrix_failures(sc: SelfCheck) -> list[str]:
    return next(i for i in sc.items if i.id == "matrix").failures


def with_row(report: Report, index: int, **changes: object) -> Report:
    assert report.coverage is not None
    rows = list(report.coverage.rows)
    rows[index] = rows[index].model_copy(update=changes)
    return report.model_copy(update={"coverage": report.coverage.model_copy(update={"rows": rows})})


def test_set_level_finding_hidden_behind_clean_row_is_caught() -> None:
    """#118: раздел 4 пишет у R-02 противоречие F-01, а матрица «√» по всем колонкам. Самопроверка
    не может сказать «пройдена»."""
    report = full()
    assert report.coverage is not None
    clean = CoverageCell(state=CellState.CLEAN)
    broken = with_row(report, 1, set_cell=clean)  # R-02: как матрица RC1 до правки

    sc = check(broken)

    assert "matrix" in failed_ids(sc)
    failures = matrix_failures(sc)
    assert any(f.startswith("R-02: F-01 есть у требования в разделе 4") for f in failures)
    assert any(f.startswith("R-02: в разделе 4 F-01, в матрице находок нет") for f in failures)
    assert any(d.code == INT_SELFCHECK_FAILED for d in selfcheck_diagnostics(sc))


def test_markdown_where_section_4_and_matrix_disagree_is_caught() -> None:
    report = full()
    markdown = render_md(report)
    row = next(line for line in markdown.splitlines() if line.startswith("| R-02 |") and "F-01 (" in line)
    tampered = markdown.replace(row, row.replace(row.split("|")[-2], " нарушений нет "))

    failures = matrix_failures(check(report, tampered))

    assert any(f.startswith("R-02: в разделе 4 находок нет, в матрице F-01") for f in failures)


def two_parts(report: Report, *, second_ok: int) -> Report:
    assert report.document is not None and report.segmentation is not None
    ids = [r.id for r in report.segmentation.requirements]
    parts = [
        DocumentPart(index=1, first_block="B001", last_block="B020", passes_ok=1, requirement_ids=ids[:3]),
        DocumentPart(
            index=2, first_block="B021", last_block="B044", passes_ok=second_ok, requirement_ids=ids[3:]
        ),
    ]
    document: DocumentInfo = report.document.model_copy(update={"parts": parts})
    return report.model_copy(update={"document": document})


def test_clean_cells_in_a_part_that_no_pass_searched_are_caught() -> None:
    """Сбой модели на части 2 из 2: матрица по-старому пишет «√» у её требований. Это нарушение."""
    report = two_parts(full(), second_ok=0)  # матрица фабрики строилась так, будто искали везде

    failures = matrix_failures(check(report))

    assert any(f.startswith("R-04: «нарушений нет», а часть 2 поиском не проверена") for f in failures)
    assert any("непроверенные части в матрице: [], а в паспорте: [2]" in f for f in failures)


def test_matrix_built_with_parts_passes_the_check() -> None:
    report = two_parts(full(), second_ok=0)
    assert report.verification is not None and report.segmentation is not None and report.document
    passes = [PassResult(index=1, ok=True, parts_failed=[2])]
    matrix = build_coverage(
        report.verification, report.segmentation.requirements, passes, report.document.parts
    )
    fixed = report.model_copy(update={"coverage": matrix})
    markdown = render_md(fixed)

    sc = check(fixed, markdown)

    assert "matrix" not in failed_ids(sc), matrix_failures(sc)
    assert "| R-04 | НФТ-1 | не проверено моделью |" in markdown
    assert "| R-05 | НФТ-2 | F-03 (важно, неясно); остальное не проверено моделью |" in markdown
    assert "Не проверено моделью: 3" in markdown and "Поиском не проверены: часть 2 из 2" in markdown


def test_clean_cells_when_no_pass_worked_are_caught() -> None:
    report = full()
    metrics = report.metrics.model_copy(update={"passes_ok": 0})

    failures = matrix_failures(check(report.model_copy(update={"metrics": metrics})))

    assert any(f.startswith("R-01: «нарушений нет», а ни один проход поиска не отработал") for f in failures)


def test_many_failures_are_cut_to_ten_lines_with_the_total() -> None:
    report = full()
    markdown = (
        "\n".join(line for line in render_md(report).splitlines() if not line.startswith("| R-")) + "\n"
    )

    sc = check(report, markdown)

    item = next(i for i in sc.items if i.id == "requirements")
    assert not item.passed and len(item.failures) <= 11


# ---------------------------------------------------------------- диагностика и код возврата


def item(item_id: str, *, passed: bool, integrity: bool) -> SelfCheckItem:
    return SelfCheckItem(
        id=item_id, title=f"проверка {item_id}", passed=passed, integrity=integrity, failures=["F-01: пример"]
    )


def test_integrity_failure_makes_error_int_003_and_gap_makes_warning_int_004() -> None:
    sc = SelfCheck(
        items=[
            item("counters", passed=False, integrity=True),
            item("sections", passed=False, integrity=False),
        ]
    )

    by_code = {d.code: d for d in selfcheck_diagnostics(sc)}

    assert by_code[INT_SELFCHECK_FAILED].level is DiagLevel.ERROR
    assert by_code[INT_SELFCHECK_WARNING].level is DiagLevel.WARNING


# ---------------------------------------------------------------- в конвейере


def _run(tmp_path: Path, name: str = "report.md", kind: DocKind = DocKind.MD) -> pipeline.RunOutcome:
    return pipeline.run(load_config(REQUIREMENTS[kind], tmp_path / name, backend=Backend.FAKE))


@pytest.mark.parametrize("kind", [DocKind.MD, DocKind.PDF, DocKind.DOCX], ids=lambda k: k.value)
def test_real_run_passes_selfcheck_on_every_format_and_shows_it_in_passport(
    tmp_path: Path, kind: DocKind
) -> None:
    outcome = _run(tmp_path, kind=kind)

    assert outcome.exit_code is ExitCode.OK
    report = outcome.report
    assert report is not None and report.selfcheck is not None
    assert report.selfcheck.passed, [i for i in report.selfcheck.items if not i.passed]
    assert report.selfcheck.quotes_rechecked > 0  # цитаты сверены заново по тексту документа
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    passport = markdown.split("## 2.")[0]
    assert f"| Самопроверка отчёта | {report.selfcheck.summary}" in passport
    assert "## 12. Самопроверка отчёта" in markdown and "## 11. Матрица покрытия критериев" in markdown
    assert not [d for d in outcome.diagnostics if d.code.startswith("INT")]


def test_broken_finding_in_pipeline_gives_code_1_and_report_is_still_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_build = pipeline.build_report

    def build_with_anchorless_finding(ctx: object) -> Report:
        report = real_build(ctx)  # type: ignore[arg-type]
        assert report.verification is not None
        first = report.verification.findings[0]
        stripped = first.model_copy(
            update={"quotes": [q.model_copy(update={"anchor": None}) for q in first.quotes]}
        )
        findings = [stripped, *report.verification.findings[1:]]
        ver = report.verification.model_copy(update={"findings": findings})
        return report.model_copy(update={"verification": ver})

    monkeypatch.setattr(pipeline, "build_report", build_with_anchorless_finding)

    outcome = _run(tmp_path)

    assert outcome.exit_code is ExitCode.INTERNAL
    assert (tmp_path / "report.md").is_file() and (tmp_path / "report.json").is_file()
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert INT_SELFCHECK_FAILED in markdown and "НЕ ПРОЙДЕНА" in markdown
    assert outcome.report is not None and outcome.report.selfcheck is not None
    assert outcome.report.passport.exit_code is ExitCode.INTERNAL
    assert not outcome.report.selfcheck.integrity_ok
    assert outcome.report.verification is not None  # отчёт не поправлен задним числом
    assert any(f.quotes[0].anchor is None for f in outcome.report.verification.findings)


def test_selfcheck_crash_does_not_cost_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> SelfCheck:
        raise RuntimeError("selfcheck bug")

    monkeypatch.setattr(pipeline, "check_report", boom)

    outcome = _run(tmp_path)

    assert outcome.exit_code is ExitCode.OK
    assert outcome.report is not None and outcome.report.selfcheck is None
    assert INT_STEP_DEGRADED in (tmp_path / "report.md").read_text(encoding="utf-8")
