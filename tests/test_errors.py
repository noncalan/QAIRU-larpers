"""Код возврата из диагностики (исследование 04, раздел 4)."""

from __future__ import annotations

import inspect
import re
from collections import Counter

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


_LITERAL_CODE = re.compile(r'^([A-Z_]+) = "([A-Z]{2,3}-\d{3})"', re.MULTILINE)


def test_diagnostic_codes_are_unique() -> None:
    """Два смысла под одним кодом ломают отчёт и eval: коды каталога не повторяются (#38, #96).

    Синоним одного смысла задаётся ссылкой на имя (``IN_NO_REQUIREMENTS = IN_HEADINGS_ONLY``), не строкой.
    """
    codes = _LITERAL_CODE.findall(inspect.getsource(errors))

    repeated = {code: k for code, k in Counter(value for _, value in codes).items() if k > 1}

    assert len(codes) > 50, "каталог кодов не найден"
    assert repeated == {}
