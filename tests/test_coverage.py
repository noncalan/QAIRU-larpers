"""Матрица покрытия критериев и метрики прогона (#43).

Матрица строится из находок и обязана с ними совпадать; метрики считаются по записям вызовов и по находкам.
Выдуманные отчёты из ``report_factory`` и сборка ``build_report`` на настоящем прогоне ``fake``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import report_factory as factory
from conftest import REQUIREMENTS, ROOT
from validator import pipeline
from validator.config import load_config
from validator.models import (
    Backend,
    CallRecord,
    CallStatus,
    CellState,
    CoverageMatrix,
    CriticAction,
    CriticDecision,
    DiscardReason,
    DocKind,
    DocumentPart,
    Effort,
    ExitCode,
    LLMStep,
    PassResult,
    Property,
    Report,
    Scope,
    Severity,
    TokenUsage,
    Verdict,
)
from validator.report import render_md
from validator.report.coverage import (
    REQUIREMENT_PROPERTIES,
    SET_PROPERTIES,
    build_coverage,
    cell_text,
    coverage_totals,
    grid_finding_ids,
    row_cells,
    row_finding_ids,
)
from validator.report.run_metrics import (
    critic_counts,
    discarded_by_reason,
    found_in_distribution,
    step_metrics,
)

OK_PASS = [PassResult(index=1, ok=True)]


def full() -> Report:
    return factory.full_report()


def matrix_of(report: Report, passes: list[PassResult] = OK_PASS) -> CoverageMatrix:
    assert report.verification is not None and report.segmentation is not None
    return build_coverage(report.verification, report.segmentation.requirements, passes)


# ---------------------------------------------------------------- матрица


def test_matrix_has_a_row_per_requirement_and_a_column_per_requirement_property() -> None:
    report = full()
    assert report.segmentation is not None

    matrix = matrix_of(report)

    assert [r.requirement_id for r in matrix.rows] == [r.id for r in report.segmentation.requirements]
    assert matrix.requirement_properties == list(REQUIREMENT_PROPERTIES)
    assert all(set(row.cells) == set(REQUIREMENT_PROPERTIES) for row in matrix.rows)
    assert [r.property for r in matrix.set_rows] == list(SET_PROPERTIES)
    assert all(p.scope is Scope.REQUIREMENT for p in REQUIREMENT_PROPERTIES)
    assert all(p.scope is Scope.SET for p in SET_PROPERTIES)


def test_every_finding_is_visible_in_the_matrix_and_nothing_extra() -> None:
    report = full()
    assert report.verification is not None

    matrix = matrix_of(report)

    assert grid_finding_ids(matrix) == {f.id for f in report.verification.findings}


def test_matrix_cells_agree_with_finding_property_and_verdict() -> None:
    report = full()
    assert report.verification is not None
    by_id = {f.id: f for f in report.verification.findings}

    matrix = matrix_of(report)

    for row in matrix.rows:
        for prop, cell in row.cells.items():
            for fid in cell.finding_ids:
                assert by_id[fid].property is prop
                assert fid in report.verification.requirement_status[row.requirement_id]
            expected = {
                (True, True): CellState.VIOLATION,
                (True, False): CellState.UNCLEAR,
                (False, False): CellState.CLEAN,
            }
            has_violation = any(by_id[i].verdict is Verdict.VIOLATION for i in cell.finding_ids)
            assert cell.state is expected[(bool(cell.finding_ids), has_violation)]


def test_violation_beats_unclear_and_unclear_gets_a_question_mark() -> None:
    report = full()
    matrix = matrix_of(report)

    texts = {cell_text(c) for row in matrix.rows for c in row.cells.values()}

    assert "F-02" in texts  # нарушение: номер находки
    assert "F-03?" in texts  # «неясно»: номер и знак вопроса
    assert "√" in texts


def test_set_level_findings_land_in_set_rows_with_standard_references() -> None:
    report = full()

    matrix = matrix_of(report)

    consistent = next(r for r in matrix.set_rows if r.property is Property.SET_CONSISTENT)
    assert consistent.state is CellState.VIOLATION and consistent.finding_ids == ["F-01"]
    assert any("5.2.6" in ref for ref in consistent.refs) and any("830" in ref for ref in consistent.refs)
    assert next(r for r in matrix.set_rows if r.property is Property.SET_MODIFIABLE).state is CellState.CLEAN


def test_without_a_successful_pass_clean_cells_say_not_checked() -> None:
    report = full()

    matrix = matrix_of(report, [PassResult(index=1, ok=False)])

    assert not matrix.checked
    states = {c.state for row in matrix.rows for c in row_cells(row)}
    assert CellState.CLEAN not in states and CellState.NOT_CHECKED in states
    assert CellState.VIOLATION in states  # находки остаются находками
    assert all(r.state is not CellState.CLEAN for r in matrix.set_rows)
    assert not any(row.searched for row in matrix.rows)
    assert cell_text(matrix.rows[0].cells[Property.NECESSARY]) == "–"
    assert cell_text(matrix.rows[0].set_cell) == "–"


def test_set_level_finding_is_shown_in_the_row_of_every_requirement_it_touches() -> None:
    """#118: R-14 с критичным противоречием F-01 не может стоять «√» по всем колонкам."""
    report = full()
    assert report.verification is not None
    status = {k: list(v) for k, v in report.verification.requirement_status.items()}
    status["R-04"] = [*status["R-04"], "F-01"]  # противоречие R-02 и R-04
    ver = report.verification.model_copy(update={"requirement_status": status})

    matrix = build_coverage(ver, report.segmentation.requirements, OK_PASS)  # type: ignore[union-attr]

    rows = {r.requirement_id: r for r in matrix.rows}
    for rid in ("R-02", "R-04"):
        assert rows[rid].set_cell.finding_ids == ["F-01"]
        assert rows[rid].set_cell.state is CellState.VIOLATION
        assert all(c.state is CellState.CLEAN for c in rows[rid].cells.values())
    assert rows["R-01"].set_cell.state is CellState.CLEAN
    consistent = next(r for r in matrix.set_rows if r.property is Property.SET_CONSISTENT)
    assert consistent.finding_ids == ["F-01"]  # и в строке набора тоже


def test_every_row_shows_exactly_the_findings_of_its_requirement() -> None:
    report = full()
    assert report.verification is not None

    matrix = matrix_of(report)

    for row in matrix.rows:
        assert row_finding_ids(row) == set(report.verification.requirement_status[row.requirement_id])


def test_set_property_on_a_requirement_finding_goes_to_set_column_and_set_row() -> None:
    report = full()
    assert report.verification is not None
    first = report.verification.findings[1]
    assert first.scope is Scope.REQUIREMENT
    odd = first.model_copy(
        update={"property": Property.SET_CONSISTENT}
    )  # свойство набора у находки требования
    findings = [report.verification.findings[0], odd, *report.verification.findings[2:]]
    ver = report.verification.model_copy(update={"findings": findings})

    matrix = build_coverage(ver, report.segmentation.requirements, OK_PASS)  # type: ignore[union-attr]

    row = next(r for r in matrix.rows if odd.id in ver.requirement_status[r.requirement_id])
    assert odd.id in row.set_cell.finding_ids
    assert odd.id in next(r for r in matrix.set_rows if r.property is Property.SET_CONSISTENT).finding_ids
    assert not matrix.extra_finding_ids
    assert grid_finding_ids(matrix) == {f.id for f in findings}


def test_set_finding_with_a_requirement_property_lands_in_the_matching_set_row() -> None:
    """Находка без требования, которую верификатор отнёс к набору, видна в матрице набора (#118)."""
    report = full()
    assert report.verification is not None
    target = report.verification.findings[3]
    moved = target.model_copy(
        update={"scope": Scope.SET, "property": Property.TRACEABLE, "requirement_ids": []}
    )
    findings = [*report.verification.findings[:3], moved, *report.verification.findings[4:]]
    status = {k: [i for i in v if i != target.id] for k, v in report.verification.requirement_status.items()}
    ver = report.verification.model_copy(update={"findings": findings, "requirement_status": status})

    matrix = build_coverage(ver, report.segmentation.requirements, OK_PASS)  # type: ignore[union-attr]

    traceable = next(r for r in matrix.set_rows if r.property is Property.SET_TRACEABLE)
    assert traceable.finding_ids == [target.id] and traceable.state is CellState.VIOLATION
    assert not matrix.extra_finding_ids and not matrix.unlinked_finding_ids


def test_every_requirement_property_has_a_set_row_to_go_to() -> None:
    assert {p.set_level for p in Property} == set(SET_PROPERTIES)
    assert all(p.set_level is p for p in SET_PROPERTIES)


# ---------------------------------------------------------------- длинный документ по частям


def two_parts(report: Report) -> list[DocumentPart]:
    assert report.segmentation is not None
    ids = [r.id for r in report.segmentation.requirements]
    return [
        DocumentPart(index=1, first_block="B001", last_block="B020", requirement_ids=ids[:3]),
        DocumentPart(index=2, first_block="B021", last_block="B044", requirement_ids=ids[3:]),
    ]


def test_requirements_of_a_part_where_every_pass_failed_are_not_checked() -> None:
    """Сбой модели на части 2: её требования «не проверялось», а не «проверено, нарушений нет» (#38)."""
    report = full()
    assert report.verification is not None and report.segmentation is not None
    passes = [PassResult(index=1, ok=True, parts_failed=[2]), PassResult(index=2, ok=True, parts_failed=[2])]

    matrix = build_coverage(report.verification, report.segmentation.requirements, passes, two_parts(report))

    assert matrix.checked and matrix.unchecked_parts == [2]
    first, second = matrix.rows[:3], matrix.rows[3:]
    assert all(row.searched for row in first) and not any(row.searched for row in second)
    assert CellState.CLEAN in {c.state for row in first for c in row_cells(row)}
    states = {c.state for row in second for c in row_cells(row)}
    assert CellState.CLEAN not in states and CellState.NOT_CHECKED in states
    assert CellState.VIOLATION in states  # находки части 2 (линтер, сверка частей) остаются
    clean_set = [r for r in matrix.set_rows if not r.finding_ids]
    assert clean_set and all(r.state is CellState.PARTIAL for r in clean_set)


def test_part_checked_by_another_pass_counts_as_checked() -> None:
    report = full()
    assert report.verification is not None and report.segmentation is not None
    passes = [PassResult(index=1, ok=True, parts_failed=[2]), PassResult(index=2, ok=True)]

    matrix = build_coverage(report.verification, report.segmentation.requirements, passes, two_parts(report))

    assert not matrix.unchecked_parts and all(row.searched for row in matrix.rows)
    assert all(r.state is not CellState.PARTIAL for r in matrix.set_rows)


def test_requirement_outside_every_part_needs_a_pass_that_covered_all_parts() -> None:
    report = full()
    assert report.verification is not None and report.segmentation is not None
    parts = two_parts(report)
    parts[1] = parts[1].model_copy(update={"requirement_ids": parts[1].requirement_ids[1:]})  # R-04 без части

    matrix = build_coverage(
        report.verification,
        report.segmentation.requirements,
        [PassResult(index=1, ok=True, parts_failed=[1])],
        parts,
    )

    rows = {r.requirement_id: r for r in matrix.rows}
    assert not rows["R-04"].searched and rows["R-05"].searched and not rows["R-01"].searched


def test_pass_covers_a_part_only_if_it_did_not_fail_there() -> None:
    whole = PassResult(index=1, ok=True)
    split = PassResult(index=1, ok=True, parts_failed=[2])

    assert whole.covers(None) and whole.covers(3)
    assert split.covers(1) and not split.covers(2) and not split.covers(None)
    assert not PassResult(index=1, ok=False).covers(1)


def test_finding_without_a_requirement_is_listed_as_unlinked_not_as_other_level() -> None:
    report = full()
    assert report.verification is not None and report.segmentation is not None
    target = report.verification.findings[1]
    assert target.scope is Scope.REQUIREMENT
    findings = [report.verification.findings[0], target.model_copy(update={"requirement_ids": []})]
    findings += report.verification.findings[2:]
    status = {k: [i for i in v if i != target.id] for k, v in report.verification.requirement_status.items()}
    ver = report.verification.model_copy(update={"findings": findings, "requirement_status": status})

    matrix = build_coverage(ver, report.segmentation.requirements, OK_PASS)

    assert matrix.unlinked_finding_ids == [target.id] and target.id not in matrix.extra_finding_ids
    assert grid_finding_ids(matrix) == {f.id for f in findings}


def test_atom_finding_is_visible_on_its_parent_like_in_the_requirement_table() -> None:
    report = full()
    assert report.verification is not None
    status = {k: list(v) for k, v in report.verification.requirement_status.items()}
    status["R-01"] = [*status["R-01"], "F-02"]  # родитель видит находку атома, как в таблице раздела 4
    ver = report.verification.model_copy(update={"requirement_status": status})

    matrix = build_coverage(ver, report.segmentation.requirements, OK_PASS)  # type: ignore[union-attr]

    assert "F-02" in matrix.rows[0].cells[Property.VERIFIABLE].finding_ids


def test_totals_count_every_cell_once() -> None:
    matrix = matrix_of(full())

    totals = coverage_totals(matrix)

    assert sum(totals.values()) == len(matrix.rows) * (len(REQUIREMENT_PROPERTIES) + 1)  # и колонка «Набор»
    assert totals[CellState.VIOLATION] == 3 and totals[CellState.UNCLEAR] == 1  # F-02, F-04 и F-01 у R-02


def test_matrix_is_complete_in_json_and_compact_in_markdown() -> None:
    report = full()
    assert report.coverage is not None

    dumped = Report.model_validate_json(report.model_dump_json())

    assert dumped.coverage == report.coverage
    assert len(dumped.coverage.rows[0].cells) == len(REQUIREMENT_PROPERTIES)  # type: ignore[union-attr]
    matrix_part = render_md(report).split("## 11.")[1]
    row = next(line for line in matrix_part.splitlines() if line.startswith("| R-03 |"))
    assert row.count("|") == len(REQUIREMENT_PROPERTIES) + 3 and "F-02" in row  # свойства и «Набор»


# ---------------------------------------------------------------- метрики прогона


def call(
    step: LLMStep, status: CallStatus, *, ms: int = 1000, cost: float | None = 0.1, tokens: int = 10
) -> CallRecord:
    return CallRecord(
        call_id=f"{step.value}.1",
        step=step,
        call_index=1,
        key="k",
        model_requested="claude-opus-5-5",
        effort=Effort.MEDIUM,
        backend=Backend.CLI,
        started_at=datetime(2026, 11, 26, tzinfo=UTC),
        duration_ms=ms,
        usage=TokenUsage(input_tokens=tokens, output_tokens=tokens // 2),
        cost_usd=cost,
        status=status,
    )


def test_step_metrics_group_calls_by_step_in_pipeline_order() -> None:
    calls = [
        call(LLMStep.QUESTIONS, CallStatus.OK, ms=2000),
        call(LLMStep.SEARCH, CallStatus.OK, ms=3000, cost=0.5),
        call(LLMStep.SEARCH, CallStatus.CACHED, ms=0, cost=None),
        call(LLMStep.SEARCH, CallStatus.ERROR, ms=500, cost=0.2),
        call(LLMStep.SEGMENT, CallStatus.REPLAYED, ms=100, cost=0.01),
    ]

    steps = step_metrics(calls)

    assert [s.step for s in steps] == ["segment", "search", "questions"]
    search = steps[1]
    assert (search.calls, search.failed, search.cached) == (3, 1, 1)
    assert search.duration_s == 3.5 and search.cost_usd == 0.7
    assert search.tokens.input_tokens == 30
    assert steps[0].replayed == 1


def test_retried_attempt_is_not_a_separate_call_but_its_time_and_money_count() -> None:
    calls = [
        call(LLMStep.SEARCH, CallStatus.RETRIED, ms=1000, cost=0.1),
        call(LLMStep.SEARCH, CallStatus.OK, ms=2000),
    ]

    [search] = step_metrics(calls)

    assert search.calls == 1 and search.duration_s == 3.0 and search.cost_usd == 0.2


def test_step_without_any_price_has_no_cost_instead_of_zero() -> None:
    [row] = step_metrics([call(LLMStep.SEGMENT, CallStatus.OK, cost=None)])

    assert row.cost_usd is None


def test_found_in_distribution_goes_from_the_most_stable_findings() -> None:
    report = full()
    assert report.verification is not None

    buckets = found_in_distribution(report.verification.findings)

    assert (buckets[0].found_in, buckets[0].of) == (3, 3)
    assert sum(b.count for b in buckets) == len(report.verification.findings)


def test_discarded_reasons_are_counted_in_enum_order_without_zeros() -> None:
    report = full()

    assert list(discarded_by_reason(report.verification).items()) == [
        (DiscardReason.QUOTE_NOT_FOUND, 1),
        (DiscardReason.CRITIC_REJECTED, 1),
    ]
    assert discarded_by_reason(None) == {}


def test_critic_counts_are_zero_until_a_critic_decides() -> None:
    ver = full().verification
    assert ver is not None
    assert critic_counts(ver) == (3, 1, 1)  # фикстура #39: критик оставил, понизил и снял по находке
    findings = [f.model_copy(update={"critic": None}) for f in ver.findings]
    kept = [d for d in ver.discarded if d.reason is not DiscardReason.CRITIC_REJECTED]
    clean = ver.model_copy(update={"findings": findings, "discarded": kept})
    assert critic_counts(clean) == (0, 0, 0)

    decided = findings[0].model_copy(
        update={"critic": CriticDecision(action=CriticAction.DOWNGRADE, reason="мелочь")}
    )
    ver = clean.model_copy(update={"findings": [decided, *findings[1:]]})

    assert critic_counts(ver) == (1, 1, 0)
    assert critic_counts(None) == (0, 0, 0)


# ---------------------------------------------------------------- настоящий прогон


@pytest.mark.parametrize("kind", [DocKind.MD, DocKind.DOCX], ids=lambda k: k.value)
def test_real_run_fills_matrix_and_metrics_consistently(tmp_path: Path, kind: DocKind) -> None:
    outcome = pipeline.run(load_config(REQUIREMENTS[kind], tmp_path / "report.md", backend=Backend.FAKE))

    report = outcome.report
    assert report is not None and report.verification is not None and report.coverage is not None
    m = report.metrics
    assert report.coverage.checked and m.passes_ok == m.passes_total > 0
    assert grid_finding_ids(report.coverage) == {f.id for f in report.verification.findings}
    assert m.merged_total == m.findings_total + m.discarded_total
    assert m.candidates_total == m.candidates_passes + m.candidates_lint
    assert sum(s.calls for s in m.steps) == m.calls_total
    assert sum(b.count for b in m.found_in) == m.findings_total
    assert sum(m.discarded_by_reason.values()) == m.discarded_total


RC1_RUN = ROOT / "reports" / "v0.9-rc1" / "run"


@pytest.mark.skipif(not RC1_RUN.is_dir(), reason="нет записи прогона RC1")
def test_rc1_replay_shows_set_level_findings_in_rows_of_their_requirements(tmp_path: Path) -> None:
    """Критерий #118 на записи RC1 (``reports/v0.9-rc1/run``, без модели): у R-14, R-41 и R-12.1 в матрице
    видны их критичные находки уровня набора, «Прослеживаемость набора» не «нарушений нет», у каждого
    требования матрица и раздел 4 называют одни и те же находки, самопроверка пройдена."""
    cfg = load_config(
        REQUIREMENTS[DocKind.PDF], tmp_path / "report.md", backend=Backend.REPLAY, replay_dir=RC1_RUN
    )

    outcome = pipeline.run(cfg)

    report = outcome.report
    assert outcome.exit_code is ExitCode.OK
    assert report is not None and report.verification is not None and report.coverage is not None
    ver, rows = report.verification, {r.requirement_id: r for r in report.coverage.rows}
    by_id = {f.id: f for f in ver.findings}
    for rid in ("R-14", "R-41", "R-12.1"):
        critical = [i for i in ver.requirement_status[rid] if by_id[i].severity is Severity.CRITICAL]
        assert critical and all(by_id[i].scope is Scope.SET for i in critical)
        assert set(critical) <= set(rows[rid].set_cell.finding_ids)
        assert rows[rid].set_cell.state is CellState.VIOLATION
    assert all(row_finding_ids(row) == set(ver.requirement_status[rid]) for rid, row in rows.items())
    traceable = next(r for r in report.coverage.set_rows if r.property is Property.SET_TRACEABLE)
    assert traceable.state is CellState.VIOLATION
    assert not report.coverage.extra_finding_ids and not report.coverage.unlinked_finding_ids
    assert report.selfcheck is not None and all(item.passed for item in report.selfcheck.items)
