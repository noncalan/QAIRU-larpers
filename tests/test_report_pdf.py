"""PDF-версия отчёта (#34): собирается из того же ``Report``, что и Markdown, текст извлекается.

Проверяем содержимое по тексту PDF (PyMuPDF): заголовки всех разделов, все R-NN, F-NN и Q-NN, кириллица
и знаки разметки Typst в цитатах, шрифты только из репозитория, одинаковые байты при повторной сборке и
запасной путь конвейера (Typst упал: Markdown остаётся, диагностика INT-002, код возврата 0).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pymupdf
import pytest

import report_factory as factory
from conftest import REQUIREMENTS
from validator import pipeline
from validator.config import load_config
from validator.models import Backend, DocKind, ExitCode, QuestionMetrics, Report, WithdrawnQuestion
from validator.report import PdfRenderError, render_md, render_pdf
from validator.report.render_pdf import FONTS_DIR, SECTION_TITLES, build_view

FIXTURES = factory.FIXTURES
CASES = tuple(factory.CASES)
MARKUP_SAMPLE = "Цена #1 в $ и 100% *звёзд* _под_ @имя <тег> [скобки] \\ `код` ; REST type | GET"
"""Знаки, которые Typst и Markdown понимают как разметку: в PDF они обязаны остаться текстом."""


def load(name: str) -> Report:
    return Report.model_validate_json((FIXTURES / f"{name}.report.json").read_text(encoding="utf-8"))


def pdf_text(data: bytes) -> str:
    """Текст всех страниц одной строкой с пробелами вместо переносов (колонтитулы входят в текст)."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages = [page.get_text() for page in doc]
    return re.sub(r"\s+", " ", " ".join(pages))


def md_headings(markdown: str) -> list[str]:
    return [line.removeprefix("## ") for line in markdown.splitlines() if line.startswith("## ")]


# ---------------------------------------------------------------- содержимое


@pytest.mark.parametrize("name", CASES)
def test_pdf_contains_every_section_heading_of_the_markdown_report(name: str) -> None:
    report = load(name)

    text = pdf_text(render_pdf(report))

    for heading in md_headings(render_md(report)):
        assert heading in text, heading


def test_pdf_shows_withdrawn_questions_and_question_metrics_like_markdown() -> None:
    report = load("full")
    assert report.verification is not None
    answer = next(q for f in report.verification.findings for q in f.quotes if q.verified)
    withdrawn = WithdrawnQuestion(
        question=report.questions[0].model_copy(update={"id": "Q-90"}),
        reason="срок указан в разделе о бронировании",
        answer=answer,
    )
    metrics = report.metrics.model_copy(
        update={
            "questions": QuestionMetrics(
                total=3,
                before_critic=4,
                withdrawn=1,
                reviewed=4,
                with_scenario=0.75,
                closed=1.0,
                without_errors=0.5,
                by_addressee={"analyst": 2, "business": 1},
            )
        }
    )
    both = report.model_copy(update={"questions_withdrawn": [withdrawn], "metrics": metrics})

    markdown, text = render_md(both), pdf_text(render_pdf(both))

    for needle in (
        "Снятые вопросы: ответ есть в документе",
        "Q-90",
        "срок указан в разделе о бронировании",
        "до самопроверки 4, снято 1",
        "Вопросы: со сценарием",
        "аналитик 2, бизнес 1",
    ):
        assert needle in markdown, needle
        assert needle in text, needle


def test_pdf_section_titles_match_markdown_headings() -> None:
    markdown_titles = md_headings(render_md(load("full")))

    assert [t for t in markdown_titles if t[0].isdigit()] == list(SECTION_TITLES.values())


@pytest.mark.parametrize("name", CASES)
def test_pdf_has_every_requirement_finding_and_question_id(name: str) -> None:
    report = load(name)
    text = pdf_text(render_pdf(report))

    ids = [r.id for r in report.segmentation.requirements] if report.segmentation else []
    ids += [f.id for f in report.verification.findings] if report.verification else []
    ids += [q.id for q in report.questions]

    missing = [i for i in ids if not re.search(rf"(?<![\w-]){re.escape(i)}(?![\w.]|\.\d)", text)]
    assert not missing
    if name == "full":
        assert any(i.startswith("R-") for i in ids) and any(i.startswith("Q-") for i in ids)


def test_pdf_keeps_cyrillic_quotes_and_places_from_the_report() -> None:
    report = load("full")
    assert report.verification is not None
    text = pdf_text(render_pdf(report))

    for finding in report.verification.findings:
        for quote in finding.quotes:
            if quote.verified and quote.located:
                assert re.sub(r"\s+", " ", quote.located)[:40] in text, finding.id
    assert report.conclusion.headline in text


def test_pdf_shows_traffic_light_headline_on_first_page() -> None:
    report = load("full")

    with pymupdf.open(stream=render_pdf(report), filetype="pdf") as doc:
        first = re.sub(r"\s+", " ", doc[0].get_text())

    assert report.conclusion.headline in first
    assert "Что исправить первым" in first and "Какие вопросы задать автору первыми" in first
    assert report.passport.agent_version in first


def test_pdf_header_and_footer_carry_document_date_and_agent_version() -> None:
    report = load("full")
    p = report.passport

    with pymupdf.open(stream=render_pdf(report), filetype="pdf") as doc:
        assert doc.page_count > 1
        for page in doc:
            text = re.sub(r"\s+", " ", page.get_text())
            assert p.document_name in text and p.agent_version in text
            assert f"из {doc.page_count}" in text


def test_markup_characters_in_document_text_stay_plain_text() -> None:
    report = load("full")
    assert report.segmentation is not None and report.verification is not None
    requirement = report.segmentation.requirements[0].model_copy(update={"quote": MARKUP_SAMPLE})
    finding = report.verification.findings[0].model_copy(update={"explanation": MARKUP_SAMPLE})
    broken = report.model_copy(
        update={
            "segmentation": report.segmentation.model_copy(
                update={"requirements": [requirement, *report.segmentation.requirements[1:]]}
            ),
            "verification": report.verification.model_copy(
                update={"findings": [finding, *report.verification.findings[1:]]}
            ),
        }
    )

    text = pdf_text(render_pdf(broken))

    assert text.count(MARKUP_SAMPLE) >= 2  # цитата в таблице нарезки и объяснение в карточке


# ---------------------------------------------------------------- шрифты и повторяемость


def test_pdf_embeds_only_bundled_fonts() -> None:
    with pymupdf.open(stream=render_pdf(load("full")), filetype="pdf") as doc:
        names = {font[3] for page in doc for font in page.get_fonts()}

    assert names
    assert all("Liberation" in name for name in names), names


def test_font_files_and_license_are_in_the_package() -> None:
    files = {p.name for p in FONTS_DIR.iterdir()}

    assert {"LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf", "LiberationMono-Regular.ttf"} <= files
    assert "OFL.txt" in files
    assert "SIL OPEN FONT LICENSE" in (FONTS_DIR / "OFL.txt").read_text(encoding="utf-8")


def test_same_report_gives_identical_pdf_bytes() -> None:
    report = load("full")

    assert render_pdf(report) == render_pdf(report)


def test_view_model_is_plain_json_with_all_sections_in_order() -> None:
    view = build_view(load("full"))

    assert [s["title"] for s in view["sections"]] == list(SECTION_TITLES.values())
    assert view["first"]["fix"] and view["first"]["ask"]


def test_unread_document_report_gets_a_pdf_without_requirement_sections() -> None:
    report = load("unread")

    text = pdf_text(render_pdf(report))

    assert SECTION_TITLES["input"] in text
    assert SECTION_TITLES["segmentation"] not in text
    assert "Диагностика прогона" in text


# ---------------------------------------------------------------- конвейер


def _run(tmp_path: Path, name: str = "report.pdf", kind: DocKind = DocKind.MD) -> pipeline.RunOutcome:
    return pipeline.run(load_config(REQUIREMENTS[kind], tmp_path / name, backend=Backend.FAKE))


@pytest.mark.parametrize("kind", [DocKind.MD, DocKind.PDF, DocKind.DOCX], ids=lambda k: k.value)
def test_pdf_output_writes_pdf_and_markdown_side_by_side(tmp_path: Path, kind: DocKind) -> None:
    outcome = _run(tmp_path, kind=kind)

    assert outcome.exit_code is ExitCode.OK
    assert outcome.report_path == tmp_path / "report.pdf"
    assert (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF")
    assert (tmp_path / "report.md").is_file() and (tmp_path / "report.json").is_file()
    assert outcome.outputs["report_pdf"] == tmp_path / "report.pdf"
    assert outcome.report is not None and outcome.report.segmentation is not None
    text = pdf_text((tmp_path / "report.pdf").read_bytes())
    assert all(r.id in text for r in outcome.report.segmentation.requirements)
    assert not [d for d in outcome.diagnostics if d.code.startswith("INT")]


def test_markdown_output_does_not_build_pdf(tmp_path: Path) -> None:
    outcome = _run(tmp_path, "report.md")

    assert outcome.exit_code is ExitCode.OK
    assert not (tmp_path / "report.pdf").exists()
    assert "report_pdf" not in outcome.outputs


def test_typst_failure_keeps_markdown_and_returns_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = tmp_path / "report.pdf"
    stale.write_bytes(b"%PDF-1.7 old run")

    def boom(report: Report) -> bytes:
        raise PdfRenderError("Typst не собрал PDF: RuntimeError")

    monkeypatch.setattr(pipeline, "render_pdf", boom)

    outcome = _run(tmp_path)

    assert outcome.exit_code is ExitCode.OK
    assert not stale.exists()  # прежний PDF не выдаёт себя за отчёт этого прогона
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "INT-002" in markdown and "PDF-версия отчёта не собрана" in markdown
    assert [d for d in outcome.diagnostics if d.code == "INT-002"]
    assert outcome.report_path == tmp_path / "report.md"
    assert "report_pdf" not in outcome.outputs


def test_unexpected_typst_error_is_also_degradation_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(report: Report) -> bytes:
        raise ValueError("timestamp datetime must not include microseconds")

    monkeypatch.setattr(pipeline, "render_pdf", boom)

    outcome = _run(tmp_path)

    assert outcome.exit_code is ExitCode.OK
    assert (tmp_path / "report.md").is_file()
    assert "ValueError" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_any_exception_while_building_pdf_view_keeps_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(report: Report) -> dict[str, object]:
        raise KeyError("F-99")

    monkeypatch.setattr(
        sys.modules["validator.report.render_pdf"], "build_view", boom
    )  # имя пакета занято функцией

    outcome = _run(tmp_path)

    assert outcome.exit_code is ExitCode.OK
    assert not (tmp_path / "report.pdf").exists()
    assert "INT-002" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_emergency_report_does_not_duplicate_diagnostics_or_leave_stale_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "report.pdf").write_bytes(b"%PDF-1.7 old run")

    def boom(ctx: object) -> Report:
        raise RuntimeError("build failed")

    monkeypatch.setattr(pipeline, "build_report", boom)

    outcome = _run(tmp_path)

    assert outcome.exit_code is ExitCode.INTERNAL
    assert not (tmp_path / "report.pdf").exists()
    codes = [d.code for d in outcome.diagnostics]
    assert codes.count("INT-001") == 1 and "INT-002" not in codes


def test_pdf_output_never_overwrites_a_pdf_input(tmp_path: Path) -> None:
    src = tmp_path / "req.pdf"
    src.write_bytes(REQUIREMENTS[DocKind.PDF].read_bytes())
    before = src.read_bytes()

    outcome = pipeline.run(load_config(src, src, backend=Backend.FAKE))

    assert outcome.exit_code is ExitCode.USAGE
    assert src.read_bytes() == before
    assert outcome.diagnostics[0].code == "OUT-003"
