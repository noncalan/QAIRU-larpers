"""Шапка таблицы DOCX (#139): ``tblLook firstRow`` не шапка, первая строка без явной шапки остаётся строкой.

Синтетические документы: python-docx (как Word по умолчанию) и фабрика ``docx_factory``. Модель не нужна.
"""

from __future__ import annotations

from pathlib import Path

import docx
import pytest

from docx_factory import WORD_LOOK, cell, make_docx, row, table
from validator.ingest import parse, render_for_model
from validator.ingest.blocks import row_label
from validator.ingest.docx_header import is_column_name, looks_like_header
from validator.models import Block, BlockKind, DocKind, ParsedDocument
from validator.segment import fallback_segmentation

FT = (
    ("ФТ-1", "Система должна отправлять уведомление в течение 5 секунд после операции."),
    ("ФТ-2", "Система должна хранить историю уведомлений 90 дней."),
    ("ФТ-3", "Пользователь может отключить уведомления в настройках."),
)
SHORT_ROWS = (("Администратор", "Полный доступ"), ("Оператор", "Только чтение"))


def read(tmp_path: Path, body: str) -> ParsedDocument:
    return parse(make_docx(tmp_path / "doc.docx", body), DocKind.DOCX)


def table_rows(doc: ParsedDocument) -> list[Block]:
    return [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]


def cells(*values: str, bold: bool = False, style: str | None = None) -> list[str]:
    return [cell(v, bold=bold, style=style) for v in values]


# ---------------------------------------------------------------- таблица требований без шапки


def test_word_table_without_header_keeps_first_requirement(tmp_path: Path) -> None:
    document = docx.Document()
    document.add_heading("Требования к сервису уведомлений", 1)
    document.add_paragraph("Ниже перечислены функциональные требования.")
    grid = document.add_table(rows=3, cols=2)
    grid.style = "Table Grid"
    for line, values in zip(grid.rows, FT, strict=True):
        line.cells[0].text, line.cells[1].text = values
    path = tmp_path / "noheader.docx"
    document.save(str(path))
    assert 'w:firstRow="1"' in grid._tbl.xml  # условие ошибки: флаг оформления Word по умолчанию

    doc = parse(path, DocKind.DOCX)

    rows = table_rows(doc)
    assert [row_label(b) for b in rows] == ["ФТ-1", "ФТ-2", "ФТ-3"]
    assert [b.anchor.row for b in rows] == [1, 2, 3]
    assert rows[0].serialized == f"колонка 1: ФТ-1; колонка 2: {FT[0][1]}"
    assert all(FT[0][1] not in b.serialized for b in rows[1:])  # первое требование не стало названием колонки
    found = [r.source_id for r in fallback_segmentation(doc).requirements]
    assert {"ФТ-1", "ФТ-2", "ФТ-3"} <= set(found)


def test_first_row_flag_of_tbllook_alone_is_not_a_header(tmp_path: Path) -> None:
    doc = read(tmp_path, table(*(row(*cells(*values)) for values in FT), look=WORD_LOOK))

    assert [b.text for b in table_rows(doc)] == [" | ".join(values) for values in FT]
    assert [c.column for c in table_rows(doc)[1].cells] == ["колонка 1", "колонка 2"]


def test_requirement_table_text_is_fully_covered_by_blocks(tmp_path: Path) -> None:
    doc = read(tmp_path, table(*(row(*cells(*values)) for values in FT), look=WORD_LOOK))

    covered = "".join(b.text for b in doc.blocks)
    assert all(value in covered for values in FT for value in values)


# ---------------------------------------------------------------- шапка по разметке


def test_marked_header_names_columns_and_is_not_a_block(tmp_path: Path) -> None:
    body = table(row(*cells("ID", "Требование"), header=True), *(row(*cells(*v)) for v in FT), look=WORD_LOOK)

    rows = table_rows(read(tmp_path, body))

    assert [b.anchor.row for b in rows] == [1, 2, 3]
    assert rows[0].serialized == f"ID: ФТ-1; Требование: {FT[0][1]}"


def test_header_mark_switched_off_is_not_a_header(tmp_path: Path) -> None:
    body = table(row(*cells(*FT[0]), header="0"), *(row(*cells(*v)) for v in FT[1:]), look=WORD_LOOK)

    rows = table_rows(read(tmp_path, body))

    assert [row_label(b) for b in rows] == ["ФТ-1", "ФТ-2", "ФТ-3"]


# ---------------------------------------------------------------- шапка по виду первой строки


def test_guessed_header_names_columns_and_stays_a_block(tmp_path: Path) -> None:
    body = table(row(*cells("ID", "Требование")), *(row(*cells(*v)) for v in FT), look=WORD_LOOK)

    doc = read(tmp_path, body)

    rows = table_rows(doc)
    assert [b.anchor.row for b in rows] == [1, 2, 3, 4]  # номера строк как в Word: шапка тоже строка
    assert rows[0].text == "ID | Требование"
    assert rows[0].serialized == "колонка 1: ID; колонка 2: Требование"
    assert rows[1].serialized == f"ID: ФТ-1; Требование: {FT[0][1]}"
    assert [(c.column, c.text) for c in rows[1].cells] == [("ID", "ФТ-1"), ("Требование", FT[0][1])]
    assert "строка таблицы 2" in render_for_model(doc)


def test_guessed_header_over_numbers_and_sentences(tmp_path: Path) -> None:
    body = table(
        row(*cells("№", "Требование", "Приоритет")),
        row(*cells("1", FT[0][1], "Высокий")),
        row(*cells("2", FT[1][1], "Средний")),
        look=WORD_LOOK,
    )

    rows = table_rows(read(tmp_path, body))

    assert rows[2].serialized == f"№: 2; Требование: {FT[1][1]}; Приоритет: Средний"


@pytest.mark.parametrize(
    ("first", "guessed"),
    [
        pytest.param(cells("Роль", "Права", bold=True), True, id="bold-runs"),
        pytest.param(cells("Роль", "Права", style="TableHeading"), True, id="bold-paragraph-style"),
        pytest.param(cells("Роль", "Права"), False, id="plain"),
    ],
)
def test_bold_first_row_is_a_header_over_short_rows(tmp_path: Path, first: list[str], guessed: bool) -> None:
    body = table(row(*first), *(row(*cells(*v)) for v in SHORT_ROWS), look=WORD_LOOK)

    rows = table_rows(read(tmp_path, body))

    assert rows[0].text == "Роль | Права"  # угаданная шапка или нет, её текст в блоке
    assert rows[1].serialized == (
        "Роль: Администратор; Права: Полный доступ"
        if guessed
        else "колонка 1: Администратор; колонка 2: Полный доступ"
    )


@pytest.mark.parametrize(("look", "guessed"), [(WORD_LOOK, True), ('w:firstRow="0"', False)])
def test_table_style_bold_first_row_counts_only_with_first_row_flag(
    tmp_path: Path, look: str, guessed: bool
) -> None:
    body = table(
        row(*cells("Роль", "Права")), *(row(*cells(*v)) for v in SHORT_ROWS), look=look, style="HeaderTable"
    )

    rows = table_rows(read(tmp_path, body))

    assert (rows[1].cells[0].column == "Роль") is guessed


def test_first_row_flag_switched_off_forbids_a_guess(tmp_path: Path) -> None:
    body = table(row(*cells("ID", "Требование")), *(row(*cells(*v)) for v in FT), look='w:firstRow="0"')

    rows = table_rows(read(tmp_path, body))

    assert rows[1].serialized == f"колонка 1: ФТ-1; колонка 2: {FT[0][1]}"


def test_key_value_list_without_header_gets_no_wrong_column_names(tmp_path: Path) -> None:
    body = table(
        row(*cells("Документ", "Техническое задание")),
        row(*cells("Версия", "1.2")),
        row(*cells("Дата", "26.11.2026")),
        look=WORD_LOOK,
    )

    rows = table_rows(read(tmp_path, body))

    assert [b.serialized for b in rows] == [
        "колонка 1: Документ; колонка 2: Техническое задание",
        "колонка 1: Версия; колонка 2: 1.2",
        "колонка 1: Дата; колонка 2: 26.11.2026",
    ]


def test_key_value_header_word_names_columns(tmp_path: Path) -> None:
    body = table(
        row(*cells("Поле", "Значение")),
        row(*cells("Наименование", "Сервис уведомлений")),
        row(*cells("Владелец", "Розничный бизнес")),
        look=WORD_LOOK,
    )

    rows = table_rows(read(tmp_path, body))

    assert [b.serialized for b in rows[1:]] == [
        "Наименование: Сервис уведомлений",
        "Владелец: Розничный бизнес",
    ]


def test_header_over_empty_form_names_columns(tmp_path: Path) -> None:
    body = table(row(*cells("Подразделение", "ФИО", "Подпись")), row(*cells("", "", "")), look=WORD_LOOK)

    rows = table_rows(read(tmp_path, body))

    assert rows[1].serialized == "Подразделение: (пусто); ФИО: (пусто); Подпись: (пусто)"


# ---------------------------------------------------------------- правила догадки


@pytest.mark.parametrize(
    "text",
    [
        "№",
        "ID",
        "Требование",
        "Ожидаемый результат",
        "Приоритет (1–3)",
        "Должность",
        "Обязанности",
        "**Поле**",
    ],
)
def test_column_name_shapes(text: str) -> None:
    assert is_column_name(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ФТ-1",
        "REQ-12 Вход по коду",
        "1",
        "26.11.2026",
        "Система должна хранить историю.",
        "Пользователь может отключить уведомления",
        "Сумма shall be positive",
        "Одна две три четыре пять шесть семь",
    ],
)
def test_not_column_name_shapes(text: str) -> None:
    assert not is_column_name(text)


def test_value_repeated_below_means_first_row_is_data() -> None:
    rows = [["Вход по коду", "Высокий"], [FT[0][1], "Высокий"], [FT[1][1], "Средний"]]

    assert not looks_like_header(rows, [None, None, None])


def test_single_cell_over_wide_table_is_a_caption_not_a_header() -> None:
    rows = [["Таблица требований"], ["ФТ-1", FT[0][1]], ["ФТ-2", FT[1][1]]]

    assert not looks_like_header(rows, [True, False, False])


def test_one_row_table_has_no_header() -> None:
    assert not looks_like_header([["ID", "Требование"]], [True])
