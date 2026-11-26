"""Попытка теста не теряет значения из ячеек таблиц (#120).

Модель видит строку таблицы как «Колонка: значение; …» (``Block.serialized``) и так её и цитирует. В
документе такого текста нет, и раньше код считал значение «названным без цитаты» и ставил пропуск, хотя
оно дословно стоит в ячейке (ревью RC1: «Итог 50 000 ₸ и один push», «19 900 ₸»).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import REQUIREMENTS, ROOT
from questions_helpers import make_doc
from validator.evidence_testdraft_check import CheckLog, DraftEntry, DraftValueIn, build_draft, check_value
from validator.ingest import parse
from validator.models import DefectType, DocKind, MergedFinding, ParsedDocument, Property
from validator.questions.editor import extract_json
from validator.questions.quotes import verify_quote

TABLE_DOC = """# Сервис заказов

## Критерии приемки

| Исходные условия | Действие | Ожидаемый результат |
| --- | --- | --- |
| Баланс 9 000 ₽ | Оплатить заказ на 1 000 ₽ | Итог 10 000 ₽ и одно письмо |
| Баланс 5 000 ₽ | Отменить заказ | Баланс 5 000 ₽ без письма |

## Параметры запроса

| Parameter | Format | Size |
| --- | --- | --- |
| `orderId` | string | 1–64 |
"""

CELL_QUOTE = "Ожидаемый результат: Итог 10 000 ₽ и одно письмо"


@pytest.fixture
def doc(tmp_path: Path) -> ParsedDocument:
    return make_doc(tmp_path, TABLE_DOC)


def _finding() -> MergedFinding:
    return MergedFinding(
        mid="M-001",
        property=Property.COMPLETE,
        defect_type=DefectType.INCOMPLETE,
        quotes=["Отменить заказ"],
        explanation="Пояснение находки.",
    )


def test_quote_of_one_labelled_cell_is_found_in_its_cell(doc: ParsedDocument) -> None:
    quote = verify_quote(doc, CELL_QUOTE, ())

    assert quote is not None and quote.anchor is not None
    assert quote.located == "Итог 10 000 ₽ и одно письмо"
    assert (quote.anchor.cell, quote.anchor.column) == (3, "Ожидаемый результат")


def test_quote_of_short_cells_is_found_in_its_row(doc: ParsedDocument) -> None:
    """«string» и «1–64» короче порога нечёткого поиска: по одной ячейке их не найти."""
    quote = verify_quote(doc, "Parameter: orderId; Format: string; Size: 1–64", ())

    assert quote is not None and quote.located is not None
    assert "orderId" in quote.located and "1–64" in quote.located


def test_quote_not_in_any_row_stays_unverified(doc: ParsedDocument) -> None:
    assert verify_quote(doc, "Ожидаемый результат: Итог 20 000 ₽ и два письма", ()) is None


def test_quote_in_two_rows_needs_a_hint(tmp_path: Path) -> None:
    """Одна и та же ячейка в двух строках: без подсказки блока место неизвестно, цитату не принимаем."""
    doubled = TABLE_DOC.replace(
        "| Баланс 5 000 ₽ | Отменить заказ | Баланс 5 000 ₽ без письма |",
        "| Баланс 9 000 ₽ | Повторить оплату | Итог 10 000 ₽ и одно письмо |",
    )
    doc = make_doc(tmp_path, doubled)
    row = next(b for b in doc.blocks if "Повторить оплату" in b.text)

    assert verify_quote(doc, CELL_QUOTE, ()) is None
    hinted = verify_quote(doc, CELL_QUOTE, [row.id])
    assert hinted is not None and hinted.anchor is not None and hinted.anchor.block_ids == [row.id]


def test_value_from_table_cell_is_confirmed_with_document_text(doc: ParsedDocument) -> None:
    value = check_value(DraftValueIn(value="одно письмо", quote=CELL_QUOTE), _finding(), doc)

    assert value.verified
    assert value.quote == "Итог 10 000 ₽ и одно письмо"
    assert value.anchor is not None and value.anchor.cell == 3


def test_draft_keeps_table_values_instead_of_gaps(doc: ParsedDocument) -> None:
    entry = DraftEntry(
        finding="M-001",
        precondition="Баланс 9 000 ₽.",
        steps=["Оплатить заказ на 1 000 ₽."],
        expected="Итог 10 000 ₽ и одно письмо.",
        values=[
            DraftValueIn(value="9 000 ₽", quote="Исходные условия: Баланс 9 000 ₽"),
            DraftValueIn(value="Итог 10 000 ₽ и одно письмо", quote=CELL_QUOTE),
        ],
        can_write=True,
    )
    log = CheckLog()

    draft = build_draft(entry, _finding(), doc, log)

    assert draft is not None and draft.complete
    assert draft.expected == "Итог 10 000 ₽ и одно письмо."
    assert (log.values_verified, log.values_replaced, log.numbers_replaced) == (2, 0, 0)


# ---------------------------------------------------------------- запись RC1 (#54)

RC1_RESPONSES = ROOT / "reports" / "v0.9-rc1" / "run" / "responses"
RC1_TABLE_VALUES = {"Итог 50 000 ₸ и один push", "19 900 ₸", "один push"}
"""Значения из таблицы критериев приёмки, которые на ревью RC1 стали пропусками (F-19, F-20, F-26)."""


def _recorded_values() -> list[DraftValueIn]:
    values: list[DraftValueIn] = []
    for path in sorted(RC1_RESPONSES.glob("test_draft.*.json")):
        text = json.loads(path.read_text(encoding="utf-8"))["payload"]["result"]
        for raw in extract_json(text, "drafts")["drafts"]:
            values += DraftEntry.model_validate(raw).values
    return values


def test_rc1_recorded_values_are_all_confirmed() -> None:
    """Все значения черновиков записи RC1 с цитатой подтверждаются по документу, в том числе из ячеек."""
    if not RC1_RESPONSES.is_dir():
        pytest.skip("нет записи RC1: reports/v0.9-rc1/run/")
    doc = parse(REQUIREMENTS[DocKind.PDF], DocKind.PDF)
    values = [v for v in _recorded_values() if v.value.strip() and v.quote.strip()]

    unconfirmed = [v.value for v in values if not check_value(v, _finding(), doc).verified]

    assert {v.value for v in values} >= RC1_TABLE_VALUES
    assert unconfirmed == []
