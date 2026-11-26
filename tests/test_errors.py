"""Код возврата из диагностики (исследование 04, раздел 4)."""

from __future__ import annotations

import re

import pytest

from validator import errors
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


def test_diagnostic_codes_are_unique() -> None:
    """Один код диагностики значит одно: слияние веток не должно дать два смысла на один номер."""
    codes = {
        name: value
        for name, value in vars(errors).items()
        if name.isupper() and isinstance(value, str) and re.fullmatch(r"[A-Z]+-\d{3}", value)
    }
    by_value: dict[str, list[str]] = {}
    for name, value in codes.items():
        by_value.setdefault(value, []).append(name)
    # IN_NO_REQUIREMENTS намеренный псевдоним IN_HEADINGS_ONLY (один «пустой шаблон»).
    by_value["IN-081"] = [n for n in by_value.get("IN-081", []) if n != "IN_NO_REQUIREMENTS"]
    clashes = {value: names for value, names in by_value.items() if len(names) > 1}
    assert not clashes, f"Коды диагностики повторяются: {clashes}"
