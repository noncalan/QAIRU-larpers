"""Регрессии по ревью каркаса: каждый тест закрывает найденный дефект."""

from __future__ import annotations

import zipfile
from collections.abc import Callable
from pathlib import Path

import docx
import pytest

from conftest import REQUIREMENTS
from validator import pipeline
from validator.config import load_config
from validator.errors import LLMError
from validator.ingest import build_blocks
from validator.llm import AnthropicAPI, make_client, parse_cli_payload
from validator.models import (
    Backend,
    CacheMode,
    DefectType,
    DiagLevel,
    DiscardReason,
    DocKind,
    Effort,
    ExitCode,
    LLMRequest,
    LLMStep,
    MergedFinding,
    ParsedDocument,
    Property,
    QuoteMatch,
    Report,
    Scope,
    SourceLine,
)
from validator.segment import fallback_segmentation
from validator.text import locate_quote
from validator.verify import verify


def _run(src: Path, tmp_path: Path, **options: object) -> tuple[pipeline.RunOutcome, Path]:
    cfg = load_config(src, tmp_path / "out" / "report.md", backend=Backend.FAKE, **options)  # type: ignore[arg-type]
    return pipeline.run(cfg), cfg.report_md_path


# ---------------------------------------------------------------- битый DOCX и сбой чужого парсера


def _docx_with_cut_xml(path: Path) -> Path:
    good = path.with_name("good.docx")
    document = docx.Document()
    document.add_paragraph("Система должна сохранять настройку пользователя.")
    document.save(str(good))
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(path, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "word/document.xml":
                data = data[: len(data) // 2]
            dst.writestr(item, data)
    return path


def test_docx_with_truncated_xml_is_input_error(tmp_path: Path) -> None:
    outcome, report_md = _run(_docx_with_cut_xml(tmp_path / "cut.docx"), tmp_path)

    assert outcome.exit_code is ExitCode.INPUT
    assert "IN-032" in report_md.read_text(encoding="utf-8")


def test_any_parser_crash_is_input_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(path: Path, kind: DocKind) -> ParsedDocument:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(pipeline, "parse", explode)

    outcome, report_md = _run(REQUIREMENTS[DocKind.MD], tmp_path)

    assert outcome.exit_code is ExitCode.INPUT
    assert "IN-099" in report_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------- отчёт не затирает вход


@pytest.mark.parametrize("output_name", ["req.md", "req.pdf", "req.json"])
def test_report_never_overwrites_input(tmp_path: Path, output_name: str) -> None:
    src = tmp_path / "req.md"
    original = "# Требования\n\nСистема должна сохранять настройку пользователя.\n"
    src.write_text(original, encoding="utf-8")
    cfg = load_config(src, tmp_path / output_name, backend=Backend.FAKE)

    outcome = pipeline.run(cfg)

    assert outcome.exit_code is ExitCode.USAGE
    assert src.read_text(encoding="utf-8") == original
    assert outcome.diagnostics[0].code == "OUT-003"


# ---------------------------------------------------------------- метки требований и предложения


@pytest.mark.parametrize(
    "text",
    ["В 2025 году система должна работать.", "До 10 попыток ввода подряд.", "За 5 минут до конца сессии."],
)
def test_short_word_and_number_is_not_a_requirement_label(
    make_md: Callable[[str], ParsedDocument], text: str
) -> None:
    assert make_md(text + "\n").blocks[0].source_id is None


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("**REQ-7.** Система должна сохранять настройку.", "REQ-7"),
        ("FR12: Система должна сохранять настройку.", "FR12"),
        ("ТР-2.1) Система должна сохранять настройку.", "ТР-2.1"),
        ("* NFR_3. Ответ не дольше секунды.", "NFR_3"),
    ],
)
def test_requirement_labels_are_found(
    make_md: Callable[[str], ParsedDocument], text: str, label: str
) -> None:
    assert make_md(text + "\n").blocks[0].source_id == label


def test_pdf_sentence_is_not_cut_at_line_wrap() -> None:
    lines = [
        SourceLine(text="Система должна отправлять уведомление клиенту в течение", page=1),
        SourceLine(text="5 секунд после операции. Второе предложение.", page=1),
    ]

    block = build_blocks(lines, DocKind.PDF)[0]

    assert [s.text.replace("\n", " ") for s in block.sentences] == [
        "Система должна отправлять уведомление клиенту в течение 5 секунд после операции.",
        "Второе предложение.",
    ]
    assert [s.line for s in block.sentences] == [1, 2]


def test_quote_across_two_named_blocks_is_found_within(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md("Система должна сохранять заявку клиента.\n\nЗаявка хранится один год.\n")
    ids = [b.id for b in doc.blocks]

    quote = locate_quote(doc, "заявку клиента. Заявка хранится", within=ids)

    assert quote.match is QuoteMatch.EXACT


# ---------------------------------------------------------------- run() не падает


def test_passport_failure_still_writes_emergency_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_passport(*args: object, **kwargs: object) -> None:
        raise PermissionError("prompts unreadable")

    monkeypatch.setattr(pipeline, "build_passport", broken_passport)

    outcome, report_md = _run(REQUIREMENTS[DocKind.MD], tmp_path)

    assert outcome.exit_code is ExitCode.INTERNAL
    assert "аварийном режиме" in report_md.read_text(encoding="utf-8")


def test_serialization_failure_is_internal_error_not_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_yaml(*args: object, **kwargs: object) -> None:
        raise ValueError("cannot serialize")

    monkeypatch.setattr(pipeline, "write_questions_yaml", broken_yaml)

    outcome, _ = _run(REQUIREMENTS[DocKind.MD], tmp_path)

    assert outcome.exit_code is ExitCode.INTERNAL


# ---------------------------------------------------------------- выбор модели и параметры


def test_auto_backend_with_key_uses_the_api_even_without_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-key-not-a-key")
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "r.md").model_copy(
        update={"backend": Backend.AUTO, "has_api_key": True}
    )

    client, diagnostics = make_client(cfg)

    assert isinstance(client, AnthropicAPI)
    assert not any(d.level is DiagLevel.ERROR for d in diagnostics)


def test_unimplemented_options_are_reported(tmp_path: Path) -> None:
    outcome, _ = _run(REQUIREMENTS[DocKind.MD], tmp_path, ask=True)

    assert outcome.exit_code is ExitCode.OK
    assert any(d.code == "CFG-010" and "--ask" in d.message for d in outcome.diagnostics)


def test_budget_and_cache_options_are_not_reported_as_unimplemented(tmp_path: Path) -> None:
    outcome, _ = _run(REQUIREMENTS[DocKind.MD], tmp_path, max_budget_usd=1.0, cache=CacheMode.OFF)

    assert outcome.exit_code is ExitCode.OK
    assert not any(d.code == "CFG-010" and d.level is not DiagLevel.INFO for d in outcome.diagnostics)
    # С фикстурами модель не вызывается: отчёт честно говорит, что параметры ни на что не повлияли.
    assert any("не действуют с бэкендом fake" in d.message for d in outcome.diagnostics)


# ---------------------------------------------------------------- каждая находка с цитатой


def _merged(mid: str, quotes: list[str], scope: Scope = Scope.REQUIREMENT) -> MergedFinding:
    return MergedFinding(
        mid=mid,
        property=Property.SET_COMPLETE if scope is Scope.SET else Property.UNAMBIGUOUS,
        defect_type=DefectType.INCOMPLETE if scope is Scope.SET else DefectType.AMBIGUOUS,
        scope=scope,
        quotes=quotes,
        explanation="Пояснение.",
    )


def test_finding_without_quote_is_discarded_at_any_scope(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md("Система должна сохранять заявку клиента.\n")

    ver = verify([_merged("M-001", [], Scope.SET)], doc, fallback_segmentation(doc))

    assert not ver.findings
    assert [d.reason for d in ver.discarded] == [DiscardReason.QUOTE_NOT_FOUND]


def test_unmatched_quotes_are_dropped_but_counted(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md("Система должна сохранять заявку клиента.\n")
    merged = [
        _merged("M-001", ["сохранять заявку клиента", "Этой фразы в документе нет совсем."]),
        _merged("M-002", ["И этой фразы тоже нет в документе."]),
    ]

    ver = verify(merged, doc, fallback_segmentation(doc))

    assert [len(f.quotes) for f in ver.findings] == [1]
    assert ver.findings[0].quotes[0].verified
    assert (ver.summary.quotes_total, ver.summary.quotes_not_found) == (3, 2)


def test_long_number_in_error_text_is_not_a_limit() -> None:
    req = LLMRequest(step=LLMStep.SEARCH, call_index=1, model="m", effort=Effort.LOW, system="s", prompt="p")
    payload = {"is_error": True, "result": "Prompt is too long: 14290 tokens > 8000 maximum"}

    with pytest.raises(LLMError) as err:
        parse_cli_payload(payload, req, Backend.CLI)

    assert err.value.code == "LLM-010"
    assert not err.value.retryable


def test_report_json_is_written_for_regression_runs(tmp_path: Path) -> None:
    outcome, report_md = _run(REQUIREMENTS[DocKind.MD], tmp_path)

    report = Report.model_validate_json(report_md.with_suffix(".json").read_text(encoding="utf-8"))
    assert outcome.exit_code is ExitCode.OK
    assert report.verification is not None
    assert report.verification.summary.quotes_not_found >= 1  # выдуманная цитата из фикстуры посчитана
