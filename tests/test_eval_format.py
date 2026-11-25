"""Список находок для судьи одинаков для агента и базовой линии; нарезка и цитаты свободного текста."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import ROOT
from evals.findings import agent_item, free_items, render_items
from evals.freetext import check_quote, extract_quotes, split_findings
from evals.judge import EvalError
from evals.runs import Source, load_run
from validator.models import (
    DefectType,
    Finding,
    ParsedDocument,
    Property,
    Quote,
    QuoteMatch,
    Scope,
    Severity,
    Verdict,
)

BASELINE_RUN = ROOT / "tests" / "fixtures" / "eval" / "baseline-run"
DOCUMENT = ROOT / "data" / "input" / "requirements.md"
KEYS = ["id", "quote", "property", "explanation"]


def make_finding(quotes: list[str], number: int = 1) -> Finding:
    return Finding(
        id=f"F-{number:02d}",
        merged_id=f"M-{number:03d}",
        property=Property.SET_CONSISTENT,
        defect_type=DefectType.CONTRADICTION,
        verdict=Verdict.VIOLATION,
        scope=Scope.SET,
        severity=Severity.CRITICAL,
        quotes=[Quote(text=q, match=QuoteMatch.EXACT, score=100.0) for q in quotes],
        explanation="Два требования нельзя выполнить одновременно.",
    )


def _parsed(block: str) -> list[dict[str, object]]:
    data = json.loads(block)
    assert isinstance(data, list)
    return data


def test_agent_and_baseline_lists_have_the_same_shape() -> None:
    agent_block = render_items([agent_item(n, make_finding(["первое", "второе"], n)) for n in (1, 2)])
    baseline = load_run(BASELINE_RUN, DOCUMENT)

    baseline_block = render_items(baseline.items)

    for block in (agent_block, baseline_block):
        rows = _parsed(block)
        assert [list(r) for r in rows] == [KEYS] * len(rows)
        assert [r["id"] for r in rows] == list(range(1, len(rows) + 1))
        assert all(isinstance(r[k], str) for r in rows for k in KEYS[1:])


def test_list_does_not_reveal_whose_it_is() -> None:
    agent_block = render_items([agent_item(1, make_finding(["цитата"]))])
    baseline_block = render_items(load_run(BASELINE_RUN, DOCUMENT).items)

    for block in (agent_block, baseline_block):
        assert not re.search(
            r"\bF-\d{2}\b|\bM-\d{3}\b|\bB\d{3}\b|baseline|агент|source", block, re.IGNORECASE
        )


def test_agent_item_joins_quotes_and_names_property_and_type() -> None:
    item = agent_item(3, make_finding(["первая цитата", "вторая цитата"]))

    assert item.id == 3
    assert item.quote == "первая цитата … вторая цитата"
    assert item.property == "Согласованность, противоречие"
    assert item.explanation == "Два требования нельзя выполнить одновременно."


def test_fingerprint_ignores_number_and_markup() -> None:
    a = agent_item(1, make_finding(["Цитата"]))
    b = agent_item(7, make_finding(["**цитата**"]))

    assert a.fingerprint() == b.fingerprint()
    assert len(a.fingerprint()) == 12


def test_recorded_baseline_answer_splits_into_table_rows() -> None:
    run = load_run(BASELINE_RUN, DOCUMENT)

    assert run.source is Source.BASELINE
    assert run.layout == "table"
    assert len(run.items) == 45  # модель сама пишет «Найдено 45 дефектов»
    assert all(i.property for i in run.items)
    assert sum(bool(i.quote) for i in run.items) == 45
    assert run.calls.calls == 1
    assert run.calls.cost_usd is not None


def test_recorded_baseline_quotes_are_checked_like_research_11() -> None:
    run = load_run(BASELINE_RUN, DOCUMENT)

    q = run.quotes

    assert q.total == 82
    assert (q.exact, q.fuzzy, q.not_found) == (58, 8, 16)
    assert run.quotes_final == q


TABLE_ANSWER = """# Ревью

## Шкала

| Уровень | Значение |
|---|---|
| Высокий | «это не находка, хотя здесь цитата» |

## Дефекты

| № | Цитата | Нарушенное свойство | Почему | Критичность |
|---|---|---|---|---|
| 1 | «А \\| Б» | Однозначность | потому что | Высокий |
| 2 | «В» | Полнота | так | Низкий |

## Итог

Найдено 2 дефекта: «А».
"""

HEADINGS_ANSWER = """# Ревью

## Введение

Документ про «что-то важное в документе».

### 1. Противоречие

**Цитата:** «первое требование»
**Свойство:** Согласованность
Почему: нельзя выполнить оба.

### 2. Неясный срок

> срок не указан нигде в тексте
**Нарушено свойство:** Проверяемость

## Итог

1. Исправить «первое требование».
"""

LIST_ANSWER = """Дефекты:

1. **Ф-1:** «первое требование» — однозначность.
   Свойство: Однозначность
2. «второе требование» — полнота.

Предлагаемые исправления:

- «не находка»
"""


def test_table_layout_takes_rows_of_tables_with_a_quote_column() -> None:
    split = split_findings(TABLE_ANSWER)

    assert split.layout == "table"
    assert [f.quote for f in split.findings] == ["«А | Б»", "«В»"]
    assert [f.property for f in split.findings] == ["Однозначность", "Полнота"]
    assert split.findings[0].explanation == "потому что"


def test_headings_layout_skips_intro_and_summary_sections() -> None:
    split = split_findings(HEADINGS_ANSWER)

    assert split.layout == "headings"
    assert len(split.findings) == 2
    assert split.findings[0].quote == "первое требование"
    assert split.findings[0].property == "Согласованность"
    assert split.findings[1].quote == "срок не указан нигде в тексте"
    assert split.findings[1].property == "Проверяемость"


def test_list_layout_takes_numbered_items() -> None:
    split = split_findings(LIST_ANSWER)

    assert split.layout == "list"
    assert [f.quote for f in split.findings] == ["первое требование", "второе требование"]
    assert split.findings[0].property == "Однозначность"


def test_unrecognized_layout_falls_back_to_paragraphs_with_quotes() -> None:
    split = split_findings("Вступление без цитат.\n\nТут «первая цитата».\n\nИ «вторая цитата» тоже.")

    assert split.layout == "paragraph"
    assert len(split.findings) == 2
    assert split.notes


def test_free_items_use_whole_text_when_no_explanation() -> None:
    split = split_findings(LIST_ANSWER)

    rows = _parsed(render_items(free_items(split.findings)))

    assert [r["id"] for r in rows] == [1, 2]
    assert "второе требование" in str(rows[1]["explanation"])


def test_extract_quotes_follows_research_11_rules() -> None:
    text = (
        "Ф-1: «Порог задаётся целым числом» и `amount` и `длинный идентификатор`.\n"
        "> Ф-2: Строка-цитата без ёлочек\n"
        'И "двойные кавычки длиной больше пятнадцати" тоже.\n'
        "Таблица: «A \\| B»\n"
        "## Предлагаемые формулировки\n"
        "«Это наша формулировка, а не цитата»\n"
    )

    quotes = extract_quotes(text)

    assert quotes == [
        "Порог задаётся целым числом",
        "A | B",
        "Строка-цитата без ёлочек",
        "длинный идентификатор",
        "двойные кавычки длиной больше пятнадцати",
    ]


@pytest.fixture
def doc_text() -> str:
    return "Первое требование про сумму.\n\nВторое требование про срок отправки."


def test_check_quote_splits_glued_fragments(doc_text: str) -> None:
    assert check_quote(doc_text, "Первое требование про сумму") is QuoteMatch.EXACT
    assert check_quote(doc_text, "Первое требование … про срок отправки") is QuoteMatch.EXACT
    assert check_quote(doc_text, "Первое требование / придуманный кусок текста") is QuoteMatch.NOT_FOUND
    assert check_quote(doc_text, "а … б") is None


def test_check_quote_uses_agent_locate_quote(requirements_md: ParsedDocument) -> None:
    assert (
        check_quote(requirements_md, "Месяц определяется по дате совершения покупки в Алматы")
        is QuoteMatch.EXACT
    )


def test_load_run_rejects_unknown_folder(tmp_path: Path) -> None:
    with pytest.raises(EvalError):
        load_run(tmp_path, DOCUMENT)
    with pytest.raises(EvalError):
        load_run(tmp_path / "missing", DOCUMENT)
