"""Шаг 9б: отчёт в Markdown из ``Report`` на Jinja2.

Шаблон ``templates/report.md.j2`` только раскладывает данные по разделам ТЗ п. 6. Всё, что требует
выбора или подсчёта по данным (статус каждого требования, место находки, связи находка ↔ вопрос),
готовит этот модуль, чтобы шаблон оставался плоским и его можно было проверить тестами.
Важность, светофор и счётчики здесь не считаются: они пришли от верификатора.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from validator.models import (
    Addressee,
    DefectType,
    DiagLevel,
    Finding,
    QuestionPriority,
    Quote,
    Report,
    ReportStatus,
    Requirement,
    Scope,
)
from validator.report.coverage import (
    LEGEND,
    NOT_CHECKED_NOTE,
    SHORT_LABELS,
    STATE_LABELS,
    cell_text,
    coverage_totals,
)
from validator.report.fmt import FILTERS
from validator.report.run_metrics import step_label

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
NO_VIOLATIONS = "нарушений нет"
"""Статус требования без находок: слова из ТЗ п. 6, по ним тесты ищут каждое R-NN."""

_BLANK_RUNS = re.compile(r"\n{3,}")
_CRITICAL_CLASSES = frozenset(d for d in DefectType if d.critical_class)


@dataclass(frozen=True)
class RequirementRow:
    """Строка раздела «Проверка каждого требования»: у каждого R-NN есть статус."""

    requirement: Requirement
    finding_ids: tuple[str, ...]
    status: str


def _environment() -> Environment:
    # Шаблон выдаёт Markdown, а не HTML: экранирование HTML здесь исказило бы цитаты.
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=False,  # noqa: S701
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters.update(FILTERS)
    return env


def requirement_rows(report: Report) -> list[RequirementRow]:
    """Статус каждого требования: «нарушений нет» или список F-NN с важностью и вердиктом."""
    ver, seg = report.verification, report.segmentation
    if ver is None or seg is None:
        return []
    by_id = {f.id: f for f in ver.findings}
    rows: list[RequirementRow] = []
    for req in seg.requirements:
        ids = tuple(ver.requirement_status.get(req.id, ()))
        known = [by_id[i] for i in ids if i in by_id]
        parts = [f"{f.id} ({f.severity.label}, {f.verdict.label})" for f in known]
        parts += [i for i in ids if i not in by_id]
        rows.append(RequirementRow(req, ids, ", ".join(parts) if parts else NO_VIOLATIONS))
    return rows


def _finding_sections(
    findings: Sequence[Finding], requirements: Sequence[Requirement]
) -> dict[str, list[str]]:
    """Раздел документа для каждой находки: у набора свой, у требования берём раздел первого требования."""
    by_req = {r.id: r.section_path for r in requirements}
    sections: dict[str, list[str]] = {}
    for f in findings:
        path = list(f.section_path)
        if not path and f.scope is Scope.REQUIREMENT:
            path = next((by_req[i] for i in f.requirement_ids if by_req.get(i)), [])
        sections[f.id] = path
    return sections


def _section_resolver(requirements: Sequence[Requirement]) -> Callable[[Quote, Sequence[str]], list[str]]:
    """Раздел документа для цитаты: раздел самого узкого требования, в которое она попадает."""
    spans = [(r.anchor.char_start, r.anchor.char_end, r.section_path) for r in requirements]

    def resolve(quote: Quote, fallback: Sequence[str] = ()) -> list[str]:
        if quote.anchor is None:
            return list(fallback)
        start = quote.anchor.char_start
        inside = [(end - begin, path) for begin, end, path in spans if begin <= start < end and path]
        return list(min(inside, key=lambda item: item[0])[1]) if inside else list(fallback)

    return resolve


def _questions_by_finding(report: Report) -> dict[str, list[str]]:
    links: dict[str, list[str]] = defaultdict(list)
    for q in report.questions:
        for fid in q.related_findings:
            links[fid].append(q.id)
    return dict(links)


def _addressee_label(value: str) -> str:
    """Адресат из метрик вопросов (``by_addressee`` хранит значения перечисления) по-русски."""
    try:
        return Addressee(value).label
    except ValueError:
        return value


def _input_status(report: Report) -> str:
    if report.status is ReportStatus.NOT_PERFORMED:
        return "НЕ ПРОЧИТАН: проверка невозможна"
    if any(d.level is not DiagLevel.INFO for d in report.input_diagnostics):
        return "ЧАСТИЧНО: проверка выполнена, есть предупреждения"
    return "ПРОЧИТАН ПОЛНОСТЬЮ"


def report_context(report: Report) -> dict[str, Any]:
    """Всё, что нужно шаблонам отчёта: готовые списки, связи и подписи. Общий источник для MD и PDF."""
    ver = report.verification
    findings: list[Finding] = list(ver.findings) if ver else []
    requirements: list[Requirement] = list(report.segmentation.requirements) if report.segmentation else []
    blocking = {
        priority: [q.id for q in report.questions if q.priority is priority]
        for priority in (QuestionPriority.P0, QuestionPriority.P1)
    }
    return {
        "report": report,
        "findings_by_id": {f.id: f for f in findings},
        "requirements_by_id": {r.id: r for r in requirements},
        "requirement_rows": requirement_rows(report),
        "req_findings": [f for f in findings if f.scope is Scope.REQUIREMENT],
        "set_findings": [f for f in findings if f.scope is Scope.SET],
        "critical_classes": [f for f in findings if f.defect_type in _CRITICAL_CLASSES],
        "questions_by_id": {q.id: q for q in report.questions},
        "finding_sections": _finding_sections(findings, requirements),
        "section_of": _section_resolver(requirements),
        "questions_by_finding": _questions_by_finding(report),
        "blocks_development": blocking[QuestionPriority.P0],
        "blocks_testing": blocking[QuestionPriority.P1],
        "input_status": _input_status(report),
        "no_violations": NO_VIOLATIONS,
        "addressee_label": _addressee_label,
        "step_label": step_label,
        "coverage_text": cell_text,
        "coverage_short": SHORT_LABELS,
        "coverage_legend": LEGEND,
        "coverage_not_checked_note": NOT_CHECKED_NOTE,
        "coverage_state_label": STATE_LABELS,
        "coverage_totals": {k.value: n for k, n in coverage_totals(report.coverage).items()}
        if report.coverage
        else {},
    }


def render_md(report: Report) -> str:
    text = _environment().get_template("report.md.j2").render(**report_context(report))
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return _BLANK_RUNS.sub("\n\n", text) + "\n"
