"""Код возврата из диагностики (исследование 04, раздел 4)."""

from __future__ import annotations

import pytest

from validator.errors import InputError, exit_code
from validator.models import DiagLevel, Diagnostic, ExitCode


def diag(code: str, level: DiagLevel = DiagLevel.ERROR) -> Diagnostic:
    return Diagnostic(code=code, level=level, message="m", action="a")


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ([], ExitCode.OK),
        ([diag("IN-011", DiagLevel.INFO)], ExitCode.OK),
        ([diag("LLM-030", DiagLevel.WARNING)], ExitCode.OK),
        ([diag("IN-051", DiagLevel.WARNING)], ExitCode.PARTIAL_INPUT),
        ([diag("IN-001")], ExitCode.INPUT),
        ([diag("LLM-031")], ExitCode.MODEL),
        ([diag("CFG-001")], ExitCode.MODEL),
        ([diag("OUT-001")], ExitCode.USAGE),
        ([diag("INT-001")], ExitCode.INTERNAL),
        ([diag("LLM-031"), diag("IN-001")], ExitCode.INPUT),
        ([diag("IN-001"), diag("INT-001")], ExitCode.INTERNAL),
        ([diag("IN-051", DiagLevel.WARNING), diag("LLM-031")], ExitCode.MODEL),
    ],
)
def test_exit_code_from_diagnostics(codes: list[Diagnostic], expected: ExitCode) -> None:
    assert exit_code(codes) is expected


def test_error_becomes_diagnostic_with_action() -> None:
    error = InputError("Файл не найден", code="IN-001", hint="Проверьте путь", location="x.pdf")

    d = error.to_diagnostic(action="Проверка не выполнена")

    assert (d.code, d.level, d.hint, d.location) == ("IN-001", DiagLevel.ERROR, "Проверьте путь", "x.pdf")
