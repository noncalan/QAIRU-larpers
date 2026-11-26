"""Названия колонок таблиц DOCX без ``w:tblHeader`` (#139, доработка #148).

Неверные названия колонок хуже, чем никаких: модель строит на них ложные находки. Двенадцать синтетических
таблиц, как их делает Word (python-docx, ``tblLook firstRow="1"``): где шапка угадывается, где нет. В любом
случае каждая строка таблицы остаётся блоком, текст не теряется. Модель не нужна.
"""

from __future__ import annotations

from pathlib import Path

import docx
import pytest
from docx.oxml.ns import qn

from validator.ingest import parse
from validator.models import BlockKind, DocKind, ParsedDocument

R1 = "Система должна отправлять push в течение 5 секунд."
R2 = "Система должна хранить историю 90 дней."
FT1 = "Система должна отправлять уведомление в течение 5 секунд после операции."
FT2 = "Система должна хранить историю уведомлений 90 дней."
FT3 = "Пользователь может отключить уведомления в настройках."
API = "Программный интерфейс для обмена данными между системами банка."
PUSH = "Короткое уведомление, которое приходит на экран телефона клиента."
BLOCK = "Система должна блокировать вход после 5 ошибок."
ROLES = (("Роль", "Права"), ("Администратор", "Полный доступ"), ("Оператор", "Только чтение"))
ROLES_NAMED = [
    "колонка 1: Роль; колонка 2: Права",
    "Роль: Администратор; Права: Полный доступ",
    "Роль: Оператор; Права: Только чтение",
]
ROLES_PLAIN = [
    "колонка 1: Роль; колонка 2: Права",
    "колонка 1: Администратор; колонка 2: Полный доступ",
    "колонка 1: Оператор; колонка 2: Только чтение",
]

# имя: (стиль таблицы, первая строка жирным, строки, ожидаемый вид строк для модели)
CASES: dict[str, tuple[str, bool, tuple[tuple[str, ...], ...], list[str]]] = {
    "numbered-header": (
        "Table Grid",
        False,
        (("№", "Требование", "Приоритет"), ("1", R1, "Высокий"), ("2", R2, "Средний")),
        [
            "колонка 1: №; колонка 2: Требование; колонка 3: Приоритет",
            f"№: 1; Требование: {R1}; Приоритет: Высокий",
            f"№: 2; Требование: {R2}; Приоритет: Средний",
        ],
    ),
    "passport-keys": (
        "Table Grid",
        False,
        (("Документ", "Техническое задание"), ("Версия", "1.2"), ("Дата", "26.11.2026")),
        [
            "колонка 1: Документ; колонка 2: Техническое задание",
            "колонка 1: Версия; колонка 2: 1.2",
            "колонка 1: Дата; колонка 2: 26.11.2026",
        ],
    ),
    "glossary-keys": (
        "Table Grid",
        False,
        (("Термин", "Определение"), ("API", API), ("Push", PUSH)),
        [
            "колонка 1: Термин; колонка 2: Определение",
            f"колонка 1: API; колонка 2: {API}",
            f"колонка 1: Push; колонка 2: {PUSH}",
        ],
    ),
    "bold-header": ("Table Grid", True, ROLES, ROLES_NAMED),
    "style-header": ("Light List Accent 1", False, ROLES, ROLES_NAMED),
    "field-value-keys": (
        "Table Grid",
        False,
        (("Поле", "Значение"), ("Наименование", "Сервис уведомлений"), ("Владелец", "Розничный бизнес")),
        [
            "колонка 1: Поле; колонка 2: Значение",
            "колонка 1: Наименование; колонка 2: Сервис уведомлений",
            "колонка 1: Владелец; колонка 2: Розничный бизнес",
        ],
    ),
    "value-repeated-below": (
        "Table Grid",
        False,
        (("Вход по SMS-коду", "Высокий"), (BLOCK, "Высокий"), ("Выход из всех сессий", "Средний")),
        [
            "колонка 1: Вход по SMS-коду; колонка 2: Высокий",
            f"колонка 1: {BLOCK}; колонка 2: Высокий",
            "колонка 1: Выход из всех сессий; колонка 2: Средний",
        ],
    ),
    "requirements-without-header": (
        "Table Grid",
        False,
        (("ФТ-1", FT1), ("ФТ-2", FT2), ("ФТ-3", FT3)),
        [
            f"колонка 1: ФТ-1; колонка 2: {FT1}",
            f"колонка 1: ФТ-2; колонка 2: {FT2}",
            f"колонка 1: ФТ-3; колонка 2: {FT3}",
        ],
    ),
    "short-requirement-first": (
        "Table Grid",
        False,
        (("Пользователь может отключить уведомления", "Да"), ("Система должна хранить историю", "Да")),
        [
            "колонка 1: Пользователь может отключить уведомления; колонка 2: Да",
            "колонка 1: Система должна хранить историю; колонка 2: Да",
        ],
    ),
    "id-header-one-row": (
        "Table Grid",
        False,
        (("ID", "Требование"), ("ФТ-1", "Система должна шифровать данные.")),
        ["колонка 1: ID; колонка 2: Требование", "ID: ФТ-1; Требование: Система должна шифровать данные."],
    ),
    "api-params-keys": (
        "Table Grid",
        False,
        (
            ("Параметр", "Тип", "Обязательный", "Описание"),
            ("cardId", "string", "Да", "Идентификатор карты"),
            ("amount", "number", "Да", "Сумма операции"),
        ),
        [
            "колонка 1: Параметр; колонка 2: Тип; колонка 3: Обязательный; колонка 4: Описание",
            "колонка 1: cardId; колонка 2: string; колонка 3: Да; колонка 4: Идентификатор карты",
            "колонка 1: amount; колонка 2: number; колонка 3: Да; колонка 4: Сумма операции",
        ],
    ),
    "signatures-form": (
        "Table Grid",
        False,
        (("Подразделение", "ФИО", "Подпись"), ("", "", ""), ("", "", "")),
        [
            "колонка 1: Подразделение; колонка 2: ФИО; колонка 3: Подпись",
            "колонка 1: (пусто); колонка 2: (пусто); колонка 3: (пусто)",
            "колонка 1: (пусто); колонка 2: (пусто); колонка 3: (пусто)",
        ],
    ),
}


def write_table(
    path: Path, rows: tuple[tuple[str, ...], ...], *, style: str = "Table Grid", bold: bool = False
) -> Path:
    document = docx.Document()
    document.add_paragraph("Текст перед таблицей.")
    word_table = document.add_table(rows=len(rows), cols=len(rows[0]))
    word_table.style = style
    for index, (word_row, values) in enumerate(zip(word_table.rows, rows, strict=True)):
        for word_cell, value in zip(word_row.cells, values, strict=True):
            word_cell.text = value
            if bold and index == 0:
                for run in word_cell.paragraphs[0].runs:
                    run.bold = True
    document.save(str(path))
    return path


def table_rows(doc: ParsedDocument) -> list[str]:
    return [b.serialized for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]


@pytest.mark.parametrize("name", list(CASES))
def test_column_names_only_when_first_row_is_clearly_a_header(tmp_path: Path, name: str) -> None:
    style, bold, rows, expected = CASES[name]
    path = write_table(tmp_path / f"{name}.docx", rows, style=style, bold=bold)

    doc = parse(path, DocKind.DOCX)

    assert table_rows(doc) == expected
    texts = [b.text for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert texts == [" | ".join(values).strip() for values in rows]  # каждая строка, включая первую, это блок


def test_style_bold_first_row_needs_first_row_formatting_switched_on(tmp_path: Path) -> None:
    path = tmp_path / "style-off.docx"
    write_table(path, ROLES, style="Light List Accent 1")
    document = docx.Document(str(path))
    look = document.tables[0]._tbl.tblPr.find(qn("w:tblLook"))
    assert look is not None
    look.attrib.clear()
    look.set(qn("w:firstRow"), "0")  # «Строка заголовка» в конструкторе таблиц снята
    document.save(str(path))

    assert table_rows(parse(path, DocKind.DOCX)) == ROLES_PLAIN


def test_bold_first_row_does_not_name_columns_when_its_value_repeats_below(tmp_path: Path) -> None:
    rows = (("Вход по SMS-коду", "Высокий"), (BLOCK, "Высокий"))
    path = write_table(tmp_path / "bold-repeat.docx", rows, bold=True)

    assert table_rows(parse(path, DocKind.DOCX))[1] == f"колонка 1: {BLOCK}; колонка 2: Высокий"


def test_whole_bold_table_gives_no_header_by_boldness(tmp_path: Path) -> None:
    path = tmp_path / "all-bold.docx"
    write_table(path, ROLES)
    document = docx.Document(str(path))
    for word_row in document.tables[0].rows:
        for word_cell in word_row.cells:
            for run in word_cell.paragraphs[0].runs:
                run.bold = True
    document.save(str(path))

    assert table_rows(parse(path, DocKind.DOCX)) == ROLES_PLAIN
