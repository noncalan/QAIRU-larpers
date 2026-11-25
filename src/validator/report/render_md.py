"""Шаг 9б: отчёт в Markdown из ``Report`` на Jinja2 (минимальная версия каркаса, полноту делает #23)."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from validator.models import DefectType, Finding, Report, Requirement, Scope
from validator.report.fmt import FILTERS

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


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


def render_md(report: Report) -> str:
    ver = report.verification
    findings: list[Finding] = list(ver.findings) if ver else []
    requirements: list[Requirement] = list(report.segmentation.requirements) if report.segmentation else []
    context = {
        "report": report,
        "findings_by_id": {f.id: f for f in findings},
        "requirements_by_id": {r.id: r for r in requirements},
        "req_findings": [f for f in findings if f.scope is Scope.REQUIREMENT],
        "set_findings": [f for f in findings if f.scope is Scope.SET],
        "critical_classes": [f for f in findings if f.defect_type in _CRITICAL_CLASSES],
        "questions_by_id": {q.id: q for q in report.questions},
    }
    text = _environment().get_template("report.md.j2").render(**context)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip() + "\n"


_CRITICAL_CLASSES = frozenset(d for d in DefectType if d.critical_class)
