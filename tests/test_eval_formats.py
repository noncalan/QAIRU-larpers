"""Таблица согласия форматов (``evals.formats``): одна находка в разных форматах считается одной."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evals.formats import FormatRun, clusters, items_of, live_seconds, load_runs, main, render
from report_factory import full_report
from validator.models import (
    Backend,
    CallRecord,
    CallStatus,
    DefectType,
    DocKind,
    Effort,
    LLMStep,
    ParsedDocument,
    Property,
    Report,
)


def _with(report: Report, kind: DocKind, keep: Callable[[str], bool] = lambda _: True) -> Report:
    assert report.verification is not None
    findings = [f for f in report.verification.findings if keep(f.id)]
    verification = report.verification.model_copy(update={"findings": findings})
    passport = report.passport.model_copy(update={"document_kind": kind})
    return report.model_copy(update={"verification": verification, "passport": passport})


@pytest.fixture
def reference(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    """Опорный текст, где лежат все цитаты выдуманного отчёта."""
    report = full_report()
    assert report.verification is not None
    quotes = [q.located or q.text for f in report.verification.findings for q in f.quotes]
    return make_md("# Требования\n\n" + "\n\n".join(f"{q}." for q in quotes) + "\n")


def _runs(*reports: Report) -> list[FormatRun]:
    return [
        FormatRun(label=r.passport.document_kind.value.upper(), path=Path("x"), report=r) for r in reports
    ]


def test_same_findings_in_two_formats_form_one_group_each(reference: ParsedDocument) -> None:
    base = full_report()
    runs = _runs(_with(base, DocKind.MD), _with(base, DocKind.PDF))

    items = items_of(runs, reference)
    groups = clusters(items)

    assert base.verification is not None
    assert len(groups) == len(base.verification.findings)
    assert all({items[i].run for i in g} == {0, 1} for g in groups)


def test_finding_missing_in_other_format_is_listed_as_single(reference: ParsedDocument) -> None:
    base = full_report()
    runs = _runs(_with(base, DocKind.MD), _with(base, DocKind.DOCX, keep=lambda fid: fid != "F-03"))

    text = render(runs, reference)

    singles = text.split("## Находки только в одном формате")[1]
    assert "| MD | F-03 |" in singles
    assert "F-01" not in singles
    assert "| MD + DOCX | 4 |" in text
    assert "| MD | 1 |" in text


def test_same_place_but_unrelated_property_is_not_merged(reference: ParsedDocument) -> None:
    base = full_report()
    other = _with(base, DocKind.PDF)
    assert other.verification is not None
    changed = [
        f.model_copy(update={"property": Property.SET_COMPLETE, "defect_type": DefectType.INCOMPLETE})
        if f.id == "F-04"
        else f
        for f in other.verification.findings
    ]
    other = other.model_copy(
        update={"verification": other.verification.model_copy(update={"findings": changed})}
    )
    runs = _runs(_with(base, DocKind.MD), other)

    items = items_of(runs, reference)
    groups = clusters(items)

    f04 = [g for g in groups if any(items[i].finding.id == "F-04" for i in g)]
    assert len(f04) == 2


def test_cli_reads_report_folders(tmp_path: Path, reference: ParsedDocument) -> None:
    base = full_report()
    for kind in (DocKind.MD, DocKind.PDF):
        folder = tmp_path / kind.value
        folder.mkdir()
        (folder / "report.json").write_text(_with(base, kind).model_dump_json(), encoding="utf-8")
    ref = tmp_path / "reference.md"
    ref.write_text(reference.text, encoding="utf-8")
    out = tmp_path / "formats.md"

    code = main([str(tmp_path / "md"), str(tmp_path / "pdf"), "--reference", str(ref), "--out", str(out)])

    assert code == 0
    assert "| MD + PDF | 5 |" in out.read_text(encoding="utf-8")


def test_cli_missing_report_is_an_error_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main([str(tmp_path / "nope")])

    assert code == 2
    assert "Ошибка" in capsys.readouterr().err


def test_live_seconds_span_live_calls_and_skip_replay(tmp_path: Path) -> None:
    start = datetime(2026, 11, 26, 3, 0, tzinfo=UTC)

    def record(
        call_id: str, backend: Backend, offset_s: int, duration_ms: int, status: CallStatus = CallStatus.OK
    ) -> str:
        return CallRecord(
            call_id=call_id,
            step=LLMStep(call_id.split(".", maxsplit=1)[0]),
            call_index=1,
            key="k",
            model_requested="m",
            effort=Effort.MEDIUM,
            backend=backend,
            started_at=start + timedelta(seconds=offset_s),
            duration_ms=duration_ms,
            status=status,
        ).model_dump_json()

    run = tmp_path / "run"
    run.mkdir()
    lines = [
        record("search.1", Backend.CLI, 0, 30_000),
        record("search.2", Backend.CLI, 10, 50_000),
        record("segment.1", Backend.CLI, 3600, 2, CallStatus.CACHED),
        # Шаг перезаписан отдельно через час: промежуток между шагами не считается.
        record("questions.1", Backend.CLI, 3600, 20_000),
    ]
    (run / "calls.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    replay = tmp_path / "replay"
    (replay / "run").mkdir(parents=True)
    (replay / "run" / "calls.jsonl").write_text(
        record("search.1", Backend.REPLAY, 0, 5) + "\n", encoding="utf-8"
    )

    assert live_seconds(tmp_path) == pytest.approx(80.0)
    assert live_seconds(replay) is None
    assert live_seconds(tmp_path / "nope") is None


def test_set_level_gap_with_same_property_groups_across_places(reference: ParsedDocument) -> None:
    base = full_report()
    other = _with(base, DocKind.PDF)
    assert other.verification is not None and base.verification is not None
    elsewhere = base.verification.findings[0].quotes[:1]
    moved = [
        f.model_copy(update={"quotes": elsewhere}) if f.id == "F-05" else f
        for f in other.verification.findings
    ]
    other = other.model_copy(
        update={"verification": other.verification.model_copy(update={"findings": moved})}
    )

    items = items_of(_runs(_with(base, DocKind.MD), other), reference)
    groups = clusters(items)

    f05 = [g for g in groups if any(items[i].finding.id == "F-05" for i in g)]
    assert len(f05) == 1 and {items[i].run for i in f05[0]} == {0, 1}


def test_no_chaining_two_findings_of_one_run_through_a_third(reference: ParsedDocument) -> None:
    """Два разных дефекта MD на одной фразе и одна находка PDF, похожая на оба: группа берёт один."""
    base = full_report()
    assert base.verification is not None
    f02 = base.verification.findings[1]
    twin = f02.model_copy(update={"id": "F-09", "defect_type": DefectType.UNVERIFIABLE})
    md = base.model_copy(
        update={"verification": base.verification.model_copy(update={"findings": [f02, twin]})}
    )
    pdf = _with(md, DocKind.PDF, keep=lambda fid: fid == "F-02")

    items = items_of(_runs(_with(md, DocKind.MD), pdf), reference)
    groups = clusters(items)

    assert len(groups) == 2
    assert all(len({items[i].run for i in g}) == len(g) for g in groups)


def test_two_runs_of_one_format_get_different_labels(tmp_path: Path) -> None:
    base = full_report()
    for name in ("md1", "md2"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "report.json").write_text(
            _with(base, DocKind.MD).model_dump_json(), encoding="utf-8"
        )

    runs = load_runs([tmp_path / "md1", tmp_path / "md2"])

    assert len({r.label for r in runs}) == 2
