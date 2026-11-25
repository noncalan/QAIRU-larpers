"""Битые, пустые, защищённые и «безтекстовые» входы: понятная диагностика в отчёте и код возврата, не падение.

Это отдельно проверит жюри (CLAUDE.md, «Что оценивает жюри»).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pikepdf
import pymupdf
import pytest

from conftest import REQUIREMENTS
from validator.config import load_config
from validator.models import Backend, DocKind, ExitCode, Report, ReportStatus, RunOutcome
from validator.pipeline import run

PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def run_on(path: Path, tmp_path: Path) -> tuple[RunOutcome, Report, str]:
    cfg = load_config(path, tmp_path / "out" / "report.md", backend=Backend.FAKE)
    outcome = run(cfg)
    report = Report.model_validate_json(cfg.report_json_path.read_text(encoding="utf-8"))
    return outcome, report, cfg.report_md_path.read_text(encoding="utf-8")


def _pdf(path: Path, pages: list[str]) -> Path:
    with pymupdf.open() as doc:
        for text in pages:
            page = doc.new_page()
            if text:
                page.insert_text((72, 72), text)
        doc.save(path)
    return path


def _encrypted_pdf(path: Path) -> Path:
    plain = _pdf(path.with_name("plain.pdf"), ["Secret requirements text"])
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(user="u", owner="o"))
    return path


FATAL_CASES: dict[str, tuple[Callable[[Path], Path], str]] = {
    "empty_file": (lambda d: _write(d / "empty.pdf", b""), "IN-002"),
    "broken_pdf": (lambda d: _write(d / "broken.pdf", b"%PDF-1.7\n\x00garbage without objects"), "IN-040"),
    "encrypted_pdf": (lambda d: _encrypted_pdf(d / "locked.pdf"), "IN-020"),
    "pdf_without_text": (lambda d: _pdf(d / "scan.pdf", ["", ""]), "IN-071"),
    "broken_docx": (lambda d: _write(d / "broken.docx", b"PK\x03\x04 not a real zip archive"), "IN-032"),
    "image_named_md": (lambda d: _write(d / "picture.md", PNG_HEAD + bytes(64)), "IN-010"),
    "directory": (lambda d: _mkdir(d / "folder.pdf"), "IN-005"),
    "empty_markdown": (lambda d: _write(d / "blank.md", b"   \n\n  \n"), "IN-071"),
}


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def _mkdir(path: Path) -> Path:
    path.mkdir()
    return path


@pytest.mark.parametrize("case", FATAL_CASES, ids=str)
def test_unreadable_input_gives_diagnostic_report(case: str, tmp_path: Path) -> None:
    make, code = FATAL_CASES[case]
    src = tmp_path / "src"
    src.mkdir()

    outcome, report, md = run_on(make(src), tmp_path)

    assert outcome.exit_code is ExitCode.INPUT
    assert report.status is ReportStatus.NOT_PERFORMED
    assert code in {d.code for d in report.input_diagnostics}
    assert all(d.action and d.message for d in report.input_diagnostics)
    assert "Проверка не выполнена" in md
    assert code in md
    assert "Traceback" not in md


def test_pdf_with_some_empty_pages_is_partial(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "half.pdf", ["The system shall store the user setting.", ""])

    outcome, report, _ = run_on(path, tmp_path)

    assert outcome.exit_code is ExitCode.PARTIAL_INPUT
    assert report.status is ReportStatus.PARTIAL
    assert "IN-051" in {d.code for d in report.input_diagnostics}


def test_markdown_in_legacy_encoding_is_read(tmp_path: Path) -> None:
    path = tmp_path / "legacy.md"
    path.write_bytes("# Требования\n\nСистема должна сохранять настройку пользователя.\n".encode("cp1251"))

    outcome, report, _ = run_on(path, tmp_path)

    assert outcome.exit_code is ExitCode.OK
    assert report.document is not None
    assert "IN-061" in {d.code for d in report.input_diagnostics}


def test_wrong_extension_is_detected_by_content(tmp_path: Path) -> None:
    path = tmp_path / "requirements.md"
    path.write_bytes(REQUIREMENTS[DocKind.PDF].read_bytes())

    outcome, report, _ = run_on(path, tmp_path)

    assert outcome.exit_code is ExitCode.OK
    assert report.passport.document_kind is DocKind.PDF
    assert "IN-011" in {d.code for d in report.input_diagnostics}
