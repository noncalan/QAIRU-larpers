"""Сравнение двух версий документа по отчётам агента: ``validator-diff`` (#59). Без вызовов модели."""

from validator.diff.load import ReportReadError, load_document_text, load_report
from validator.diff.match import compare
from validator.diff.model import DiffResult, FindingClass, ReqStatus
from validator.diff.render import render_json, render_markdown, totals

__all__ = [
    "DiffResult",
    "FindingClass",
    "ReportReadError",
    "ReqStatus",
    "compare",
    "load_document_text",
    "load_report",
    "render_json",
    "render_markdown",
    "totals",
]
