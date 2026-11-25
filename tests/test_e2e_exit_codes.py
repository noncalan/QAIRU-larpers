"""Коды возврата одной командой на живом пути (issue #36): без фикстур ``fake`` и без замены модулей.

0 на трёх форматах проверяет живой прогон и его запись (``test_e2e_replay.py``); 2 без аргументов и 4 на
несуществующем файле проверяет ``test_smoke.py``. Здесь то, что видит человек без настроенной модели
и с битым файлом: код 4 или 5 и отчёт с диагностикой вместо трассировки.

CLI запускается из пустой временной папки: ``.env`` и кеш репозитория не подхватываются, а ``PATH``
указывает в пустую папку, поэтому команды ``claude`` нет.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REQUIREMENTS
from test_smoke import REPORT_SECTIONS
from validator.errors import LLM_ALL_PASSES_FAILED, LLM_UNAVAILABLE
from validator.models import DocKind, ExitCode, Report, ReportStatus


def run_without_model(tmp_path: Path, *args: str | Path) -> subprocess.CompletedProcess[str]:
    """CLI без ключа API и без ``claude`` в ``PATH``, из пустой папки (без ``.env`` репозитория)."""
    empty_bin = tmp_path / "bin"
    empty_bin.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in {"ANTHROPIC_API_KEY", "VALIDATOR_FAKE_FIXTURES"}}
    env.update(PATH=str(empty_bin), PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "validator", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
        timeout=180,
        check=False,
    )


@pytest.mark.parametrize("backend", ["auto", "cli"])
def test_no_model_gives_code_5_and_full_report(tmp_path: Path, backend: str) -> None:
    out = tmp_path / "out" / "report.md"

    result = run_without_model(tmp_path, REQUIREMENTS[DocKind.MD], out, "--backend", backend)

    assert result.returncode == ExitCode.MODEL
    assert "Traceback" not in result.stderr
    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))
    codes = {d.code for d in report.diagnostics}
    assert {LLM_UNAVAILABLE, LLM_ALL_PASSES_FAILED} <= codes
    assert report.status is not ReportStatus.NOT_PERFORMED
    assert report.passport.exit_code is ExitCode.MODEL
    report_md = out.read_text(encoding="utf-8")
    assert not [s for s in REPORT_SECTIONS if s not in report_md]
    assert LLM_UNAVAILABLE in report_md


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("broken.pdf", b"%PDF-1.7\n\x00garbage without objects"),
        ("broken.docx", b"PK\x03\x04 not a real zip archive"),
        ("empty.md", b""),
    ],
)
def test_broken_file_gives_code_4_and_diagnostic_report(tmp_path: Path, name: str, data: bytes) -> None:
    src = tmp_path / name
    src.write_bytes(data)
    out = tmp_path / "out" / "report.md"

    result = run_without_model(tmp_path, src, out)

    assert result.returncode == ExitCode.INPUT
    assert "Traceback" not in result.stderr
    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert report.status is ReportStatus.NOT_PERFORMED
    assert report.input_diagnostics
    assert "Проверка не выполнена" in out.read_text(encoding="utf-8")
