"""Защищённый вход (шаг 0) и проверки после разбора: каждый битый файл даёт свой код и отчёт-диагностику.

Фикстуры ``tests/fixtures/broken/`` делает ``tests/broken_factory.py``; там же ожидаемые коды. Жюри отдельно
проверит битые файлы и документы без текста (CLAUDE.md): агент не падает, а объясняет, что не так.
"""

from __future__ import annotations

import hashlib
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from broken_factory import BROKEN_DIR, CASES, RU_REQUIREMENTS, write, zip_bomb
from conftest import FORMATS, REQUIREMENTS
from test_smoke import run_cli
from validator.config import load_config
from validator.ingest import containers as containers_module
from validator.ingest import guard, parse
from validator.ingest.containers import probe_ole, salvage_pdf
from validator.ingest.quality import review
from validator.ingest.sniff import Sniffed, sniff
from validator.models import Backend, DiagLevel, DocKind, ExitCode, ParsedDocument, Report
from validator.pipeline import run

COMMITTED = [name for name, case in CASES.items() if not case.big]
ZIP_BOMB_SECONDS = 1.0


def run_on(path: Path, tmp_path: Path) -> tuple[ExitCode, Report, str]:
    cfg = load_config(path, tmp_path / "out" / "report.md", backend=Backend.FAKE)
    outcome = run(cfg)
    report = Report.model_validate_json(cfg.report_json_path.read_text(encoding="utf-8"))
    return outcome.exit_code, report, cfg.report_md_path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def bomb(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("big") / "zip_bomb.docx"
    path.write_bytes(zip_bomb())
    return path


# ---------------------------------------------------------------- фикстуры целиком через конвейер


def test_committed_fixtures_match_factory() -> None:
    present = {p.name for p in BROKEN_DIR.iterdir() if p.name != "README.md"}

    assert present == set(COMMITTED)


@pytest.mark.parametrize("name", COMMITTED)
def test_broken_input_gives_expected_exit_code_and_diagnostic(name: str, tmp_path: Path) -> None:
    case = CASES[name]

    code, report, md = run_on(BROKEN_DIR / name, tmp_path)

    assert code.value == case.exit_code
    assert case.code in {d.code for d in report.input_diagnostics}
    assert all(d.message and d.action for d in report.input_diagnostics)
    assert "## 2. Диагностика входа" in md
    assert case.code in md
    assert "Traceback" not in md


@pytest.mark.parametrize("name", ["password.pdf", "truncated.pdf", "garbled_text.pdf"])
def test_cli_on_broken_file_has_no_traceback(name: str, tmp_path: Path) -> None:
    result = run_cli(BROKEN_DIR / name, tmp_path / "report.md", "--backend", "fake")

    assert result.returncode == CASES[name].exit_code
    assert "Traceback" not in result.stdout + result.stderr
    assert (tmp_path / "report.md").exists()


def test_regenerated_fixtures_pass_guard_like_committed(tmp_path: Path) -> None:
    """Генератор не разошёлся с закоммиченными файлами: шаг 0 видит в свежих то же самое."""
    fresh = write(tmp_path / "fresh")

    for path in fresh:
        new, old = guard(path, workdir=tmp_path), guard(BROKEN_DIR / path.name, workdir=tmp_path)
        assert (new.fatal, [d.code for d in new.diagnostics]) == (
            old.fatal,
            [d.code for d in old.diagnostics],
        )


# ---------------------------------------------------------------- zip-бомба и контейнеры


def test_zip_bomb_rejected_fast_without_unpacking(bomb: Path) -> None:
    started = time.perf_counter()
    result = guard(bomb)
    elapsed = time.perf_counter() - started

    assert result.fatal
    assert [d.code for d in result.diagnostics] == ["IN-030"]
    assert elapsed < ZIP_BOMB_SECONDS


def test_zip_bomb_full_run_writes_diagnostic_report(bomb: Path, tmp_path: Path) -> None:
    code, report, _ = run_on(bomb, tmp_path)

    assert code is ExitCode.INPUT
    assert "IN-030" in {d.code for d in report.input_diagnostics}


def test_legacy_doc_is_named_not_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeOle:
        def __init__(self, _: str) -> None: ...
        def __enter__(self) -> FakeOle:
            return self

        def __exit__(self, *_: object) -> None: ...
        def listdir(self) -> list[list[str]]:
            return [["WordDocument"], ["1Table"]]

    monkeypatch.setattr(containers_module.olefile, "OleFileIO", FakeOle)

    probe = probe_ole(tmp_path / "old.doc")

    assert probe.kind is None
    assert [d.code for d in probe.diagnostics] == ["IN-033"]


def test_corrupt_ole_is_diagnosed(tmp_path: Path) -> None:
    path = tmp_path / "broken.doc"
    path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(100))

    result = guard(path)

    assert result.fatal
    assert result.diagnostics[0].code == "IN-010"


def test_odt_named_docx_is_not_docx(tmp_path: Path) -> None:
    path = tmp_path / "spec.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", "<office:document-content/>")

    result = guard(path)

    assert result.fatal
    assert "OpenDocument" in result.diagnostics[0].message


# ---------------------------------------------------------------- PDF: спасение обрезанного файла


def test_truncated_pdf_without_workdir_is_fatal() -> None:
    result = guard(BROKEN_DIR / "truncated.pdf")

    assert result.fatal
    assert result.diagnostics[0].code == "IN-040"


def test_salvaged_pdf_keeps_original_identity_in_passport(tmp_path: Path) -> None:
    source = BROKEN_DIR / "truncated.pdf"

    _, report, _ = run_on(source, tmp_path)

    assert report.document is not None
    assert report.document.name == "truncated.pdf"
    assert report.document.size_bytes == source.stat().st_size
    assert report.passport.document_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert report.document.pages == 8


def test_salvage_gives_up_without_page_objects(tmp_path: Path) -> None:
    assert salvage_pdf(b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n", tmp_path / "x.pdf") == 0


def test_fatal_input_still_has_hash_in_passport(tmp_path: Path) -> None:
    _, report, _ = run_on(BROKEN_DIR / "password.pdf", tmp_path)

    assert report.passport.document_sha256


# ---------------------------------------------------------------- тип по содержимому


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (RU_REQUIREMENTS.encode("utf-8"), Sniffed.TEXT),
        (RU_REQUIREMENTS.encode("cp1251"), Sniffed.TEXT),
        (RU_REQUIREMENTS.encode("koi8-r"), Sniffed.TEXT),
        (RU_REQUIREMENTS.encode("utf-16-le"), Sniffed.TEXT),
        (RU_REQUIREMENTS.encode("utf-8")[:-1], Sniffed.TEXT),  # окно обрезало многобайтовый символ
        (b"junk\n%PDF-1.4\n...", Sniffed.PDF),
        (b"{\\rtf1\\ansi text}", Sniffed.RTF),
        (b"<!DOCTYPE html><html><body>x</body></html>", Sniffed.HTML),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", Sniffed.IMAGE),
        (bytes(range(256)) * 4, Sniffed.BINARY),
    ],
    ids=[
        "utf8",
        "cp1251",
        "koi8r",
        "utf16le-nobom",
        "cut-utf8",
        "pdf-after-junk",
        "rtf",
        "html",
        "jpeg",
        "bin",
    ],
)
def test_sniff_by_content(data: bytes, expected: Sniffed) -> None:
    assert sniff(data) is expected


def test_text_with_unknown_extension_is_read_with_note(tmp_path: Path) -> None:
    path = tmp_path / "requirements.rst"
    path.write_text(RU_REQUIREMENTS, encoding="utf-8")

    result = guard(path)

    assert not result.fatal
    assert result.kind is DocKind.MD
    assert [(d.code, d.level) for d in result.diagnostics] == [("IN-011", DiagLevel.INFO)]


def test_html_page_is_unsupported_but_markdown_with_html_is_read(tmp_path: Path) -> None:
    page = b"<html><body><p>The system shall work.</p></body></html>"
    (tmp_path / "spec.html").write_bytes(page)
    (tmp_path / "spec.md").write_bytes(page)

    assert guard(tmp_path / "spec.html").fatal
    assert not guard(tmp_path / "spec.md").fatal


# ---------------------------------------------------------------- после разбора


@pytest.mark.parametrize("kind", FORMATS, ids=lambda k: k.value)
def test_real_requirements_get_no_input_warnings(
    kind: DocKind, requirements_docs: dict[DocKind, ParsedDocument]
) -> None:
    reviewed = review(requirements_docs[kind])

    assert {d.code for d in reviewed.diagnostics} <= {d.code for d in requirements_docs[kind].diagnostics}
    assert guard(REQUIREMENTS[kind]).diagnostics == []


def test_pages_without_text_are_named(tmp_path: Path) -> None:
    doc = parse(BROKEN_DIR / "mixed_scan.pdf", DocKind.PDF)

    reviewed = review(doc)

    pages = [d for d in reviewed.diagnostics if d.code == "IN-051"]
    assert len(pages) == 1
    assert pages[0].location == "стр. 2"
    assert "стр. 2" in pages[0].action


def test_short_text_is_not_judged_by_genre(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md("# Заметка\n\nВстреча перенесена на пятницу.")

    assert "IN-080" not in {d.code for d in review(doc).diagnostics}
