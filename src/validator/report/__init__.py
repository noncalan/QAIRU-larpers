"""Шаг 9: отчёт. ``build_report(ctx)`` собирает ``Report``, ``render_md`` и ``render_pdf`` рисуют его."""

from __future__ import annotations

from validator.report.build import build_report
from validator.report.render_md import render_md
from validator.report.render_pdf import PdfRenderError, render_pdf

__all__ = ["PdfRenderError", "build_report", "render_md", "render_pdf"]
