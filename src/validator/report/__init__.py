"""Шаг 9: отчёт. ``build_report(ctx) -> Report``, ``render_md(report) -> str``; PDF через Typst делает #34."""

from __future__ import annotations

from validator.report.build import build_report
from validator.report.render_md import render_md

__all__ = ["build_report", "render_md"]
