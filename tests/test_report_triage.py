"""Порядок находок для проверяющего: незначительные отдельным блоком, находки одного места подряд.

Перестановка только в отчёте: номера ``F-NN``, ``report.json`` и счётчики не меняются, самопроверка
проходит, Markdown и PDF показывают одно и то же.
"""

from __future__ import annotations

import re

import pymupdf

import report_factory as factory
from validator.models import Finding, Report, Scope, Severity, Verdict
from validator.report import render_md, render_pdf
from validator.report.selfcheck import check_report
from validator.report.triage import MINOR_BLOCK_TITLE, reading_order, review_line, split_minor

FIXTURES = factory.FIXTURES
REQ_SECTION = "## 4. Проверка каждого требования"
SET_SECTION = "## 5. Проверка всего набора требований"
CARD = re.compile(r"^### (F-\d+)", re.MULTILINE)


def load(name: str) -> Report:
    return Report.model_validate_json((FIXTURES / f"{name}.report.json").read_text(encoding="utf-8"))


def section(md: str, heading: str) -> str:
    start = md.index(heading)
    nxt = md.find("\n## ", start + 1)
    return md[start : nxt if nxt != -1 else len(md)]


def finding(fid: str, severity: Severity, reqs: list[str], base: Finding) -> Finding:
    return base.model_copy(update={"id": fid, "severity": severity, "requirement_ids": reqs})


def findings_of(report: Report) -> list[Finding]:
    assert report.verification is not None
    return list(report.verification.findings)


# ---------------------------------------------------------------- порядок


def test_findings_of_one_requirement_stand_together_within_one_severity() -> None:
    base = findings_of(load("full"))[0]
    items = [
        finding("F-01", Severity.MAJOR, ["R-01"], base),
        finding("F-02", Severity.MAJOR, ["R-02"], base),
        finding("F-03", Severity.MAJOR, ["R-01"], base),
        finding("F-04", Severity.MINOR, ["R-01"], base),
    ]

    ordered = reading_order(items, {})

    assert [f.id for f in ordered] == ["F-01", "F-03", "F-02", "F-04"]


def test_severity_order_wins_over_grouping_by_place() -> None:
    base = findings_of(load("full"))[0]
    items = [
        finding("F-01", Severity.CRITICAL, ["R-02"], base),
        finding("F-02", Severity.MAJOR, ["R-01"], base),
        finding("F-03", Severity.MINOR, ["R-02"], base),
        finding("F-04", Severity.MAJOR, ["R-02"], base),
    ]

    ordered = reading_order(items, {})

    assert [f.severity for f in ordered] == sorted((f.severity for f in items), key=lambda s: s.rank)
    assert [f.id for f in ordered] == ["F-01", "F-02", "F-04", "F-03"]


def test_atom_findings_group_with_their_parent_requirement() -> None:
    base = findings_of(load("full"))[0]
    items = [
        finding("F-01", Severity.MAJOR, ["R-07.1"], base),
        finding("F-02", Severity.MAJOR, ["R-08"], base),
        finding("F-03", Severity.MAJOR, ["R-07"], base),
    ]

    ordered = reading_order(items, {"R-07.1": "R-07", "R-07": None, "R-08": None})

    assert [f.id for f in ordered] == ["F-01", "F-03", "F-02"]


def test_split_keeps_every_finding_exactly_once() -> None:
    items = findings_of(load("full"))

    main, minor = split_minor(items)

    assert sorted(f.id for f in main + minor) == sorted(f.id for f in items)
    assert all(f.severity is Severity.MINOR and f.verdict is Verdict.VIOLATION for f in minor)


def test_unclear_and_fix_first_minor_findings_stay_in_the_main_list() -> None:
    base = findings_of(load("full"))[0]
    plain = finding("F-01", Severity.MINOR, ["R-01"], base).model_copy(update={"verdict": Verdict.VIOLATION})
    unclear = finding("F-02", Severity.MINOR, ["R-02"], base).model_copy(update={"verdict": Verdict.UNCLEAR})
    first = finding("F-03", Severity.MINOR, ["R-03"], base).model_copy(update={"verdict": Verdict.VIOLATION})

    main, optional = split_minor([plain, unclear, first], keep={"F-03"})

    assert [f.id for f in optional] == ["F-01"]
    assert [f.id for f in main] == ["F-02", "F-03"]


def test_review_line_takes_critical_count_from_violations_like_the_counters() -> None:
    s = load("full").verification.summary  # type: ignore[union-attr]
    unclear_only = s.model_copy(update={"critical": 0})

    line = review_line(unclear_only, optional=0)

    assert line is not None
    assert "Начните с критичных" not in line
    assert "по желанию" not in line


# ---------------------------------------------------------------- Markdown, PDF, самопроверка


def with_optional_minor(report: Report) -> Report:
    """В фикстуре ``full`` незначительная F-04 стоит в «Что исправить первым»; убираем её оттуда."""
    minor = {f.id for f in findings_of(report) if f.severity is Severity.MINOR}
    fix_first = [fid for fid in report.conclusion.fix_first if fid not in minor]
    return report.model_copy(
        update={"conclusion": report.conclusion.model_copy(update={"fix_first": fix_first})}
    )


def optional_ids(report: Report, scope: Scope) -> set[str]:
    return {
        f.id
        for f in findings_of(report)
        if f.scope is scope and f.severity is Severity.MINOR and f.verdict is Verdict.VIOLATION
    }


def test_fix_first_minor_finding_is_not_called_optional() -> None:
    report = load("full")

    md = render_md(report)

    assert report.conclusion.fix_first
    for fid in report.conclusion.fix_first:
        _, _, tail = section(md, REQ_SECTION).partition(f"### {MINOR_BLOCK_TITLE}")
        assert fid not in CARD.findall(tail)


def test_minor_findings_come_after_the_rest_in_their_own_block() -> None:
    report = with_optional_minor(load("full"))

    md = section(render_md(report), REQ_SECTION)

    head, _, tail = md.partition(f"### {MINOR_BLOCK_TITLE}")
    minor = optional_ids(report, Scope.REQUIREMENT)
    assert minor and tail
    assert set(CARD.findall(tail)) == minor
    assert not set(CARD.findall(head)) & minor


def test_every_finding_still_has_one_card_and_selfcheck_passes() -> None:
    report = with_optional_minor(load("full"))
    md = render_md(report)

    cards = CARD.findall(section(md, REQ_SECTION)) + CARD.findall(section(md, SET_SECTION))
    sc = check_report(report, md)

    assert MINOR_BLOCK_TITLE in md
    assert sorted(cards) == sorted(f.id for f in findings_of(report))
    assert all(item.passed for item in sc.items), [i for i in sc.items if not i.passed]


def test_no_minor_block_when_there_are_no_minor_findings() -> None:
    report = with_optional_minor(load("full"))
    assert report.verification is not None
    kept = [
        f.model_copy(update={"severity": Severity.MAJOR}) if f.severity is Severity.MINOR else f
        for f in report.verification.findings
    ]
    report = report.model_copy(
        update={"verification": report.verification.model_copy(update={"findings": kept})}
    )

    assert f"### {MINOR_BLOCK_TITLE}" not in render_md(report)


def test_pdf_shows_minor_block_and_review_line_like_markdown() -> None:
    report = with_optional_minor(load("full"))
    assert report.verification is not None
    md = render_md(report)

    with pymupdf.open(stream=render_pdf(report), filetype="pdf") as doc:
        text = re.sub(r"\s+", " ", " ".join(page.get_text() for page in doc))

    line = next(x for x in md.splitlines() if x.startswith("Объём проверки"))
    assert f"### {MINOR_BLOCK_TITLE}" in md
    assert MINOR_BLOCK_TITLE in text
    assert line[:40] in text


def test_review_line_counts_levels_and_states_no_minutes() -> None:
    report = load("full")
    assert report.verification is not None
    s = report.verification.summary

    line = review_line(s, optional=1)

    assert line is not None
    assert f"Объём проверки: {s.findings_total} наход" in line
    for sev, n in s.by_severity.items():
        assert f"{sev.label} {n}" in line
    assert f"критичных нарушений ({s.critical})" in line
    assert "Незначительные нарушения (1)" in line
    assert "мин" not in line
