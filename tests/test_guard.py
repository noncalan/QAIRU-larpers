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
        (b"\x00\x01\x02\x03\x04\x05\x06\x07junk%PDF-1.4\n...", Sniffed.PDF),
        (b"Text that mentions %PDF-1.4 header.", Sniffed.TEXT),
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
        "pdf-mentioned",
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


def test_html_fragment_without_doctype_is_unsupported_html(tmp_path: Path) -> None:
    (tmp_path / "frag.html").write_bytes("<h1>Требования</h1><p>FR-1. Система должна работать.</p>".encode())

    result = guard(tmp_path / "frag.html")

    assert result.fatal
    assert [d.code for d in result.diagnostics] == ["IN-010"]


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("req.json", '{"requirements": [{"id": "FR-1", "text": "Система должна работать."}]}'),
        ("req.xml", '<?xml version="1.0" encoding="UTF-8"?><reqs><r id="FR-1">Система работает.</r></reqs>'),
        ("req.csv", "id;text\nFR-1;Система должна работать быстро\nFR-2;Система должна быть удобной\n"),
    ],
)
def test_data_file_is_read_as_text_with_warning(tmp_path: Path, name: str, text: str) -> None:
    (tmp_path / name).write_text(text, encoding="utf-8")

    result = guard(tmp_path / name)

    assert not result.fatal
    assert ("IN-012", DiagLevel.WARNING) in [(d.code, d.level) for d in result.diagnostics]


@pytest.mark.parametrize(
    "text",
    [
        "[x] Система должна работать.\n",
        "[Ссылка]: https://example.org\n\n# Требования\n\nСистема должна работать.\n",
        "Система должна хранить данные, сроки, права.\nСистема должна быть быстрой, удобной, надёжной.\n"
        "Система должна быть доступной, понятной, простой.\n",
    ],
)
def test_ordinary_text_gets_no_data_warning(tmp_path: Path, text: str) -> None:
    (tmp_path / "doc.md").write_text(text, encoding="utf-8")

    assert "IN-012" not in {d.code for d in guard(tmp_path / "doc.md").diagnostics}


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


# ---------------------------------------------------------------- регрессии по ревью


def _corrupt_member(source: Path, target: Path, member: str) -> Path:
    """Копия DOCX, в которой испорчен сжатый поток одной части (заголовки архива целы)."""
    data = bytearray(source.read_bytes())
    with zipfile.ZipFile(source) as archive:
        info = archive.getinfo(member)
    start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
    for k in range(start + 2, start + min(info.compress_size, 40)):
        data[k] ^= 0x5A
    target.write_bytes(bytes(data))
    return target


def test_corrupt_deflate_stream_in_docx_is_input_error_not_crash(tmp_path: Path) -> None:
    path = _corrupt_member(REQUIREMENTS[DocKind.DOCX], tmp_path / "bad.docx", "[Content_Types].xml")

    result = guard(path)

    assert result.fatal
    assert result.diagnostics[0].code == "IN-032"


def test_markdown_mentioning_pdf_header_is_text(tmp_path: Path) -> None:
    path = tmp_path / "spec.md"
    path.write_text(
        "# Формат\n\nСистема должна принимать файлы, начинающиеся с %PDF-1.7.\n", encoding="utf-8"
    )

    result = guard(path)

    assert not result.fatal
    assert result.kind is DocKind.MD


def test_utf16_without_bom_is_decoded(tmp_path: Path) -> None:
    path = tmp_path / "spec.md"
    path.write_bytes(RU_REQUIREMENTS.encode("utf-16-le"))

    code, report, _ = run_on(path, tmp_path)

    assert code is ExitCode.OK
    assert report.document is not None
    assert "IN-052" not in {d.code for d in report.input_diagnostics}


def test_descriptive_english_spec_is_not_flagged_as_non_requirements(
    make_md: Callable[[str], ParsedDocument],
) -> None:
    text = (
        "# Software requirements\n\n" + "The system will authenticate users. The user can export data. " * 40
    )

    assert "IN-080" not in {d.code for d in review(make_md(text)).diagnostics}


def test_numbered_list_spec_is_not_flagged_as_non_requirements(
    make_md: Callable[[str], ParsedDocument],
) -> None:
    items = "\n".join(f"{n}. Сервис отправляет отчёт номер {n} клиенту по почте." for n in range(1, 40))

    assert "IN-080" not in {d.code for d in review(make_md("# Отчёты\n\n" + items)).diagnostics}


def test_number_tables_are_not_garbled_text(make_md: Callable[[str], ParsedDocument]) -> None:
    rows = "\n".join(f"| {n} | {n * 100} | 0..999 | {n}.{n} |" for n in range(1, 60))

    doc = make_md("# Лимиты\n\n| a | b | c | d |\n|---|---|---|---|\n" + rows)

    assert "IN-052" not in {d.code for d in review(doc).diagnostics}


def test_salvage_never_overwrites_input_inside_workdir(tmp_path: Path) -> None:
    path = tmp_path / "salvaged.pdf"
    original = (BROKEN_DIR / "truncated.pdf").read_bytes()
    path.write_bytes(original)

    result = guard(path, workdir=tmp_path)

    assert path.read_bytes() == original
    assert result.read_path is not None
    assert Path(result.read_path) != path
