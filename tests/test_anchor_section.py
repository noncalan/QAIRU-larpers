"""Раздел в якоре цитаты (#119): путь заголовков блока, где цитата стоит, а не раздел требования.

На ревью RC1 цитаты из блоков-примеров (JSON под подзаголовком «Статусы › 400. …») получали в отчёте
раздел требования находки: текст и страница верные, раздел чужой. Теперь раздел хранит якорь, его
показывают отчёт и ``questions.yaml``, а самопроверка сверяет его с блоком документа.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REQUIREMENTS, ROOT
from questions_helpers import finding, make_doc
from validator.models import Anchor, DocKind, ExitCode, ParsedDocument, Quote, QuoteMatch, Report
from validator.questions.export import _place
from validator.report.render_md import _section_resolver
from validator.report.selfcheck import _recheck_quote
from validator.text import locate_quote

EXAMPLE_DOC = """# Сервис заявок

## Ответы

Сервис отвечает кодом и телом в JSON.

### 400. Ошибка запроса

```json
{"message":"Плохой запрос"}
```

### 200. Успех

Тело содержит поле result.
"""

EXAMPLE_QUOTE = '{"message":"Плохой запрос"}'
REQUIREMENT_QUOTE = "Сервис отвечает кодом и телом в JSON."


@pytest.fixture
def doc(tmp_path: Path) -> ParsedDocument:
    return make_doc(tmp_path, EXAMPLE_DOC)


def test_quote_from_example_block_gets_its_own_heading_path(doc: ParsedDocument) -> None:
    quote = locate_quote(doc, EXAMPLE_QUOTE)

    assert quote.verified and quote.anchor is not None
    assert quote.anchor.section[-2:] == ["Ответы", "400. Ошибка запроса"]


def test_quote_from_paragraph_gets_paragraph_section(doc: ParsedDocument) -> None:
    quote = locate_quote(doc, REQUIREMENT_QUOTE)

    assert quote.anchor is not None
    assert quote.anchor.section[-1] == "Ответы"


def test_every_block_anchor_carries_block_section(doc: ParsedDocument) -> None:
    assert doc.blocks
    assert all(b.anchor.section == b.section for b in doc.blocks)


def test_report_section_comes_from_quote_anchor_not_requirement() -> None:
    resolve = _section_resolver([])
    anchor = Anchor(char_start=10, char_end=20, section=["Статусы", "400. Ошибка"])
    quote = Quote(text="x", match=QuoteMatch.EXACT, score=100.0, located="x", anchor=anchor)

    assert resolve(quote, ["Часть B", "Ответ"]) == ["Статусы", "400. Ошибка"]


def test_report_section_falls_back_without_anchor_section() -> None:
    """Отчёт старой версии без раздела в якоре: прежнее поведение, запасной раздел находки."""
    resolve = _section_resolver([])
    quote = Quote(
        text="x", match=QuoteMatch.EXACT, score=100.0, located="x", anchor=Anchor(char_start=0, char_end=1)
    )

    assert resolve(quote, ["Часть B", "Ответ"]) == ["Часть B", "Ответ"]


def test_questions_yaml_place_names_section(doc: ParsedDocument) -> None:
    quote = locate_quote(doc, EXAMPLE_QUOTE)

    assert _place(quote).startswith("раздел «Ответы › 400. Ошибка запроса»")


def test_selfcheck_passes_when_anchor_section_matches_block(doc: ParsedDocument) -> None:
    f = finding(doc, "F-01", [EXAMPLE_QUOTE])
    sections = {b.id: list(b.section) for b in doc.blocks}

    checked, failures = _recheck_quote(f, doc.text, sections)

    assert checked == 1 and failures == []


def test_selfcheck_flags_foreign_section_in_anchor(doc: ParsedDocument) -> None:
    f = finding(doc, "F-01", [EXAMPLE_QUOTE])
    q = f.quotes[0]
    assert q.anchor is not None
    wrong = q.model_copy(update={"anchor": q.anchor.model_copy(update={"section": ["Ответы"]})})
    f = f.model_copy(update={"quotes": [wrong]})
    sections = {b.id: list(b.section) for b in doc.blocks}

    _, failures = _recheck_quote(f, doc.text, sections)

    assert len(failures) == 1 and "раздел в якоре" in failures[0]


# ---------------------------------------------------------------- запись RC1 (#54)

RC1_RUN = ROOT / "reports" / "v0.9-rc1" / "run"
RC1_EXAMPLES = {
    '{"message":"Некорректный запрос"}': ("Статусы", "400. Unsuccessful — Bad request"),
    '{"result":"OK","amount":"50000","enabled":"1"}': ("Статусы", "200. Successful — Created"),
    '{"result":"ERROR","message":"Проверьте сумму"}': ("Статусы", "200. Successful — Created"),
}
"""Цитаты из блоков-примеров, которые на ревью RC1 получили раздел требования (F-36, F-53, F-08)."""


@pytest.fixture(scope="module")
def rc1_report(tmp_path_factory: pytest.TempPathFactory) -> Report:
    if not (RC1_RUN / "calls.jsonl").is_file():
        pytest.skip("нет записи RC1: reports/v0.9-rc1/run/")
    out = tmp_path_factory.mktemp("rc1") / "report.md"
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["PYTHONIOENCODING"] = "utf-8"
    # запись RC1 старше текущего кода (#133 поменял вход segment.1): ответы только по номеру вызова (#132)
    args = [str(REQUIREMENTS[DocKind.PDF]), str(out), "--backend", "replay", "--replay", str(RC1_RUN)]
    args.append("--replay-by-position")
    result = subprocess.run(
        [sys.executable, "-m", "validator", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
        timeout=300,
        check=False,
    )
    assert result.returncode == ExitCode.OK, result.stderr
    return Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))


def test_rc1_example_quotes_get_example_section(rc1_report: Report) -> None:
    assert rc1_report.verification is not None
    quotes = [q for f in rc1_report.verification.findings for q in f.quotes if q.anchor is not None]
    seen: dict[str, list[tuple[str, ...]]] = {}
    for q in quotes:
        text = (
            json.dumps(json.loads(q.located), ensure_ascii=False, separators=(",", ":"))
            if _is_json(q)
            else ""
        )
        if text in RC1_EXAMPLES:
            assert q.anchor is not None
            seen.setdefault(text, []).append(tuple(q.anchor.section[-2:]))

    assert set(seen) == set(RC1_EXAMPLES)
    for text, sections in seen.items():
        assert set(sections) == {RC1_EXAMPLES[text]}, text


def test_rc1_selfcheck_rechecks_sections(rc1_report: Report) -> None:
    assert rc1_report.selfcheck is not None
    item = next(i for i in rc1_report.selfcheck.items if i.id == "quotes")

    assert item.passed
    assert "с разделом" in item.detail


def _is_json(q: Quote) -> bool:
    if not q.located or not q.located.startswith("{"):
        return False
    try:
        json.loads(q.located)
    except ValueError:
        return False
    return True
