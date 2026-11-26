"""Сквозной прогон одной командой на бэкенде ``fake``: все разделы отчёта, файлы рядом, коды возврата."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from conftest import FIXTURES_LLM, FORMATS, REQUIREMENTS, ROOT
from validator.ingest import parse
from validator.models import (
    CallRecord,
    DiscardReason,
    DocKind,
    ExitCode,
    QuoteMatch,
    Report,
    ReportStatus,
    SourceKind,
)

REPORT_SECTIONS = (
    "## Итог на первой странице",
    "## 1. Паспорт прогона",
    "## 2. Диагностика входа",
    "## 3. Как агент нарезал требования",
    "## 4. Проверка каждого требования",
    "## 5. Проверка всего набора требований",
    "## 6. Вопросы автору ТЗ",
    "## 7. Сводка",
    "## 8. Итог",
    "## 9. Метрики прогона",
    "## 10. Диагностика прогона",
)
INVENTED_QUOTE = "Push отправляется мгновенно в момент покупки"
"""Цитата, которой нет в документе: фикстура прохода 1 подсовывает её нарочно, код обязан отбросить."""


def run_cli(*args: str | Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "VALIDATOR_FAKE_FIXTURES": str(FIXTURES_LLM), "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "validator", *map(str, args)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
        timeout=120,
        check=False,
    )


@pytest.fixture(scope="module", params=FORMATS, ids=lambda k: k.value)
def fake_run(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[DocKind, Path, int]:
    kind: DocKind = request.param
    out = tmp_path_factory.mktemp(kind.value) / "report.md"
    # Три прохода явно: по умолчанию один (D-011), а smoke проверяет и слияние проходов.
    result = run_cli(REQUIREMENTS[kind], out, "--backend", "fake", "--passes", "3")
    return kind, out, result.returncode


def test_run_exits_with_zero(fake_run: tuple[DocKind, Path, int]) -> None:
    _, _, code = fake_run

    assert code == ExitCode.OK


def test_report_has_all_sections(fake_run: tuple[DocKind, Path, int]) -> None:
    _, out, _ = fake_run
    report = out.read_text(encoding="utf-8")

    missing = [s for s in REPORT_SECTIONS if s not in report]
    assert not missing


def test_json_report_matches_contract(fake_run: tuple[DocKind, Path, int]) -> None:
    kind, out, _ = fake_run

    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))

    assert report.status is ReportStatus.COMPLETE
    assert report.passport.document_kind is kind
    assert report.passport.exit_code is ExitCode.OK
    assert report.segmentation is not None and report.segmentation.requirements
    assert report.verification is not None and report.verification.findings


def test_findings_quote_the_document(fake_run: tuple[DocKind, Path, int]) -> None:
    kind, out, _ = fake_run
    doc = parse(REQUIREMENTS[kind], kind)
    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert report.verification is not None

    for finding in report.verification.findings:
        verified = [q for q in finding.quotes if q.match is not QuoteMatch.NOT_FOUND]
        assert verified, finding.id
        for quote in verified:
            assert quote.located is not None and quote.located in doc.text
            assert quote.anchor is not None


def test_fixture_block_ids_match_markdown_blocks(fake_run: tuple[DocKind, Path, int]) -> None:
    """Фикстуры ссылаются на ID блоков Markdown: если нумерация блоков поменялась, обнови фикстуры."""
    kind, out, _ = fake_run
    if kind is not DocKind.MD:
        pytest.skip("ID блоков в фикстурах размечены по Markdown")
    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert report.verification is not None

    assert not [f.id for f in report.verification.findings if f.anchor_corrected]


def test_title_scenario_and_test_gap_reach_report(fake_run: tuple[DocKind, Path, int]) -> None:
    """Поля из ответа поиска проходят слияние и верификатор; суть находки видна в «Что исправить первым»."""
    _, out, _ = fake_run
    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert report.verification is not None
    findings = report.verification.findings
    by_id = {f.id: f for f in findings}
    from_passes = [f for f in findings if any(s.kind is SourceKind.PASS for s in f.sources)]

    # Ответ прохода в формате промпта #14 (#18): суть есть у каждой находки поиска (у линтера тоже),
    # сценарий и пробел теста там, где модель их написала.
    assert from_passes and all(f.title for f in findings)
    assert any(f.scenario and f.test_gap for f in from_passes)
    first_page = out.read_text(encoding="utf-8").split("## 1. Паспорт прогона")[0]
    first = by_id[report.conclusion.fix_first[0]]
    assert f"**{first.id}**" in first_page
    assert first.title is not None and first.title in first_page


def test_invented_quote_is_discarded(fake_run: tuple[DocKind, Path, int]) -> None:
    _, out, _ = fake_run
    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert report.verification is not None

    assert [d.reason for d in report.verification.discarded] == [DiscardReason.QUOTE_NOT_FOUND]
    assert all(INVENTED_QUOTE not in q.text for f in report.verification.findings for q in f.quotes)


def test_questions_yaml_is_ready_for_answers(fake_run: tuple[DocKind, Path, int]) -> None:
    _, out, _ = fake_run

    data = yaml.safe_load((out.parent / "questions.yaml").read_text(encoding="utf-8"))

    assert data["questions"]
    assert all(q["answer"] is None and q["options"] for q in data["questions"])


def test_calls_log_has_every_model_call(fake_run: tuple[DocKind, Path, int]) -> None:
    _, out, _ = fake_run

    lines = (out.parent / "run" / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    records = [CallRecord.model_validate(json.loads(line)) for line in lines]

    ids = [r.call_id for r in records]
    # Проходы поиска идут параллельно (#18): между собой они в порядке завершения вызовов.
    # Шаг 6 (#39): свидетель между поиском и вопросами; критик находок по умолчанию выключен.
    assert ids[0] == "segment.1" and ids[-3:] == ["evidence.1", "questions.1", "questions_critic.1"]
    assert sorted(ids[1:-3]) == ["search.1", "search.2", "search.3"]


def test_missing_input_gives_diagnostic_report(tmp_path: Path) -> None:
    out = tmp_path / "x.md"

    result = run_cli("nope.pdf", out)

    assert result.returncode == ExitCode.INPUT
    report = out.read_text(encoding="utf-8")
    assert "Проверка не выполнена" in report
    assert "IN-001" in report
    assert "Traceback" not in result.stderr


def test_no_arguments_is_usage_error() -> None:
    result = run_cli()

    assert result.returncode == ExitCode.USAGE
    assert "ВХОД" in result.stderr
    assert "Traceback" not in result.stderr


def test_unknown_option_is_usage_error_in_russian() -> None:
    result = run_cli("--bogus", "a.md", "b.md")

    assert result.returncode == ExitCode.USAGE
    assert "Ошибка в аргументах" in result.stderr


def test_report_must_be_md_or_pdf(tmp_path: Path) -> None:
    result = run_cli(REQUIREMENTS[DocKind.MD], tmp_path / "report.txt", "--backend", "fake")

    assert result.returncode == ExitCode.USAGE


def test_raw_search_input_reaches_passport(tmp_path: Path) -> None:
    out = tmp_path / "report.md"

    result = run_cli(REQUIREMENTS[DocKind.MD], out, "--backend", "fake", "--search-input", "raw")

    assert result.returncode == ExitCode.OK, result.stderr
    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert report.passport.search_input == "raw"
    assert report.verification is not None and report.verification.findings


def test_unknown_search_input_is_usage_error() -> None:
    result = run_cli(REQUIREMENTS[DocKind.MD], "out/report.md", "--search-input", "html", "--backend", "fake")

    assert result.returncode == ExitCode.USAGE


def test_version_flag() -> None:
    result = run_cli("--version")

    assert result.returncode == ExitCode.OK
    assert result.stdout.startswith("QAIRU validator ")
