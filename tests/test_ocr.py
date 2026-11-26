"""Сканы без текста (#38): без Tesseract понятная инструкция и код 4, с ним распознавание и код 3.

Тесты по умолчанию идут с выключенным OCR (``tests/conftest.py``). Тесты распознавания включают его
флагом и пропускаются, если на машине нет Tesseract с русским и английским языками.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from broken_factory import BROKEN_DIR
from validator.config import load_config
from validator.ingest import guard, parse
from validator.ingest.ocr import OCR_ENV, SCAN_HELP, ocr_enabled, ocr_status
from validator.models import Backend, DiagLevel, DocKind, ExitCode, Extraction, Report
from validator.pipeline import run
from validator.report.fmt import where

TESSERACT = ocr_status(enabled=True)
needs_tesseract = pytest.mark.skipif(not TESSERACT.available, reason=f"нет OCR: {TESSERACT.reason}")
SCAN_WORDS = ("требований", "документ")
"""Слова, которые есть на первых страницах ``requirements.pdf``: распознанный текст должен их содержать."""


def run_on(path: Path, tmp_path: Path, *, ocr: bool) -> tuple[ExitCode, Report, str]:
    cfg = load_config(path, tmp_path / "out" / "report.md", backend=Backend.FAKE, ocr=ocr)
    outcome = run(cfg)
    report = Report.model_validate_json(cfg.report_json_path.read_text(encoding="utf-8"))
    return outcome.exit_code, report, cfg.report_md_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- без распознавания


def test_scan_without_ocr_gives_code_4_and_instruction(tmp_path: Path) -> None:
    code, report, md = run_on(BROKEN_DIR / "scan.pdf", tmp_path, ocr=False)

    assert code is ExitCode.INPUT
    diag = next(d for d in report.input_diagnostics if d.code == "IN-055")
    assert diag.level is DiagLevel.ERROR and "скан" in diag.message
    assert diag.hint == SCAN_HELP
    assert "### Что сделать" in md
    assert "tesseract-ocr-rus" in md and "ocrmypdf -l rus+eng" in md
    assert "Traceback" not in md


def test_mixed_scan_without_ocr_checks_text_pages_with_code_3(tmp_path: Path) -> None:
    code, report, _ = run_on(BROKEN_DIR / "mixed_scan.pdf", tmp_path, ocr=False)

    assert code is ExitCode.PARTIAL_INPUT
    diag = next(d for d in report.input_diagnostics if d.code == "IN-051")
    assert "1 из 2" in diag.message and SCAN_HELP in diag.hint
    assert report.segmentation is not None and report.segmentation.requirements


def test_ocr_status_explains_missing_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing() -> str:
        raise RuntimeError("No tessdata specified and Tesseract is not installed")

    monkeypatch.setattr(pymupdf, "get_tessdata", missing)

    status = ocr_status(enabled=True)

    assert not status.available and "не установлен" in status.reason


def test_ocr_status_explains_missing_russian(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "eng.traineddata").write_bytes(b"x")
    monkeypatch.setattr(pymupdf, "get_tessdata", lambda: str(tmp_path))

    status = ocr_status(enabled=True)

    assert not status.available and "rus" in status.reason


@pytest.mark.parametrize(
    ("flag", "env", "expected"), [(None, "off", False), (None, "", True), (True, "off", True)]
)
def test_ocr_switch_follows_flag_then_environment(
    flag: bool | None, env: str, expected: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OCR_ENV, env)

    assert ocr_enabled(flag) is expected


def test_disabled_ocr_never_looks_for_tesseract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pymupdf, "get_tessdata", lambda: pytest.fail("OCR выключен, Tesseract не ищем"))

    status = ocr_status(enabled=False)

    assert not status.available and "выключено" in status.reason


# ---------------------------------------------------------------- с распознаванием


@needs_tesseract
def test_scan_with_ocr_is_read_and_marked(tmp_path: Path) -> None:
    doc = parse(BROKEN_DIR / "scan.pdf", DocKind.PDF, ocr=True)

    assert not [d for d in doc.diagnostics if d.level is DiagLevel.ERROR]
    assert "IN-050" in {d.code for d in doc.diagnostics}
    assert doc.blocks and all(b.anchor.extraction is Extraction.OCR for b in doc.blocks)
    text = doc.text.lower()
    assert all(word in text for word in SCAN_WORDS)
    assert "OCR" in where(doc.blocks[1].anchor)


@needs_tesseract
def test_scan_with_ocr_runs_the_check_with_code_3(tmp_path: Path) -> None:
    code, report, md = run_on(BROKEN_DIR / "scan.pdf", tmp_path, ocr=True)

    assert code is ExitCode.PARTIAL_INPUT
    assert "IN-050" in {d.code for d in report.input_diagnostics}
    assert report.segmentation is not None and report.segmentation.requirements
    assert "распознан" in md


@needs_tesseract
def test_mixed_scan_with_ocr_keeps_text_page_as_text(tmp_path: Path) -> None:
    doc = parse(BROKEN_DIR / "mixed_scan.pdf", DocKind.PDF, ocr=True)

    pages = {b.anchor.page: b.anchor.extraction for b in doc.blocks}
    assert pages.get(1) is Extraction.TEXT and pages.get(2) is Extraction.OCR
    ocr = next(d for d in doc.diagnostics if d.code == "IN-050")
    assert "стр. 2" in ocr.message and "1 из 2" in ocr.message


@needs_tesseract
def test_picture_named_pdf_is_recognized_not_rejected(tmp_path: Path) -> None:
    result = guard(BROKEN_DIR / "image.pdf", workdir=tmp_path, ocr=True)

    assert not result.fatal
    assert "IN-053" in {d.code for d in result.diagnostics}
    assert result.read_path is not None and Path(result.read_path).is_file()
