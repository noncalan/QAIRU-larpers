"""Таблицы PDF по ячейкам (#33): строки, колонки, якорь ячейки, склейка через страницы.

Проверяем на двух движках (``mupdf`` и ``pdfium``): общий код должен давать один и тот же результат. Реальный
документ ``requirements.pdf`` сверяем с DOCX-версией того же документа. Маленькие PDF собираем на лету
латиницей: кириллического шрифта в CI может не быть.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path

import pymupdf
import pytest

from conftest import REQUIREMENTS
from validator.ingest import parse
from validator.ingest.blocks import render_for_model
from validator.ingest.pdf_backends import BACKEND_NAMES
from validator.models import BlockKind, DocKind, Extraction, ParsedDocument
from validator.text import locate_quote

PDF = REQUIREMENTS[DocKind.PDF]
DOCX = REQUIREMENTS[DocKind.DOCX]
_NOISE = re.compile(r"[\W_]+")
# (x, y, текст) одной строки текста; кегль 11, шрифт helv
Cell = tuple[float, float, str]
COLUMNS = (72.0, 200.0, 330.0)
SIZE = 11
STEP = 13.0  # шаг строк внутри ячейки
PAD = 5.0  # отступ между строками таблицы


@pytest.fixture(scope="module", params=BACKEND_NAMES)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture(scope="module")
def pdf_doc(backend: str) -> ParsedDocument:
    return parse(PDF, DocKind.PDF, pdf_backend=backend)


def table_rows(doc: ParsedDocument) -> dict[int, list[list[str]]]:
    """Тексты ячеек по таблицам и строкам: ``{номер таблицы: [[ячейка, …], …]}``."""
    out: dict[int, list[list[str]]] = {}
    for block in doc.blocks:
        if block.kind is BlockKind.TABLE_ROW and block.anchor.table is not None:
            out.setdefault(block.anchor.table, []).append([c.text for c in block.cells])
    return out


def squash(text: str) -> str:
    return _NOISE.sub("", text.casefold())


# ---------------------------------------------------------------- requirements.pdf против DOCX


def test_requirements_pdf_has_the_same_tables_and_rows_as_docx(pdf_doc: ParsedDocument) -> None:
    docx = table_rows(parse(DOCX, DocKind.DOCX))

    pdf = table_rows(pdf_doc)

    assert {t: len(rows) for t, rows in pdf.items()} == {t: len(rows) for t, rows in docx.items()}


def test_requirements_pdf_cells_match_docx_cells(pdf_doc: ParsedDocument) -> None:
    """Каждая ячейка PDF совпадает с ячейкой DOCX с точностью до пробелов и знаков (в узких колонках PDF
    слово разорвано по строкам, как в самой вёрстке)."""
    docx = table_rows(parse(DOCX, DocKind.DOCX))

    pdf = table_rows(pdf_doc)

    for number, rows in docx.items():
        for index, row in enumerate(rows):
            assert [squash(c) for c in pdf[number][index]] == [squash(c) for c in row], (number, index + 1)


BROKEN_IN_LAYOUT = {
    "Идентификатор карты": "Иденти фикатор",
    "limit_kzt": "limit_kz t",
    "Признак включения": "включен ия",
    "Исход сохранения": "сохранени я",
    "Description": "Descriptio n",
    "cardId=demo-42": "demo- 42",
}
"""Слова, которые вёрстка PDF рвёт у края узкой колонки без знака переноса (#121): целое слово и обрывки."""


def test_words_broken_at_narrow_column_edge_are_glued(pdf_doc: ParsedDocument) -> None:
    for word, broken in BROKEN_IN_LAYOUT.items():
        assert word in pdf_doc.text, word
        assert broken not in pdf_doc.text, broken


def test_pdf_cells_match_docx_cells_word_for_word(pdf_doc: ParsedDocument) -> None:
    """Ячейки PDF и DOCX совпадают по словам. Осталось одно известное расхождение: хвост «о» после обрывка
    («интеграционног о») похож на служебное слово «о», и склейка без словаря его не трогает."""
    docx = table_rows(parse(DOCX, DocKind.DOCX))
    pdf = table_rows(pdf_doc)

    def words(text: str) -> str:
        return " ".join(re.sub(r"\s+([.,:;])", r"\1", text).split())

    differ = [
        (number, index, a)
        for number, rows in docx.items()
        for index, row in enumerate(rows)
        for a, b in zip(pdf[number][index], row, strict=True)
        if words(a) != words(b)
    ]

    assert len(differ) <= 1, differ
    assert all("интеграционног о" in text for _, _, text in differ)


def test_table_text_is_not_duplicated_in_the_paragraph_flow(pdf_doc: ParsedDocument) -> None:
    assert pdf_doc.text.count("Февральский счётчик") == 1
    assert pdf_doc.text.count("Сохранение порога") == 1


def test_table_rows_carry_page_table_row_and_cell_anchors(pdf_doc: ParsedDocument) -> None:
    quote = locate_quote(pdf_doc, "Февральский счётчик вырос на 4 000 ₸")

    anchor = quote.anchor
    assert anchor is not None
    assert (anchor.page, anchor.table, anchor.row, anchor.cell) == (3, 2, 2, 3)
    assert anchor.column == "Ожидаемый результат"
    assert anchor.extraction is Extraction.TABLE


def test_row_spanning_a_page_break_keeps_its_tail(pdf_doc: ParsedDocument) -> None:
    """Хвост ячейки с прошлой страницы («функционала») возвращается в свою строку, а не становится строкой."""
    row = table_rows(pdf_doc)[3][4]

    assert row[3].endswith("варианты скрытия функционала")
    assert len(table_rows(pdf_doc)[3]) == 14


def test_table_continued_on_next_page_is_one_table(pdf_doc: ParsedDocument) -> None:
    pages = {b.anchor.page for b in pdf_doc.blocks if b.anchor.table == 3}

    assert pages == {3, 4, 5}
    assert len({b.anchor.table for b in pdf_doc.blocks if b.kind is BlockKind.TABLE_ROW}) == 7


def test_header_only_table_is_an_empty_form_row(pdf_doc: ParsedDocument) -> None:
    block = next(b for b in pdf_doc.blocks if b.anchor.table == 4)

    assert block.serialized == "Подразделение: (пусто); ФИО ответственного: (пусто); Подпись: (пусто)"


def test_model_sees_header_names_with_the_cell_values(pdf_doc: ParsedDocument) -> None:
    view = render_for_model(pdf_doc)

    assert "Ожидаемый результат: Февральский счётчик вырос на 4 000 ₸" in view
    assert "Исходные условия: Покупка 31 января в 23:58 на 4 000 ₸; Действие:" in view


def test_headings_after_tables_stay_headings(pdf_doc: ParsedDocument) -> None:
    titles = [b.text for b in pdf_doc.blocks if b.kind is BlockKind.HEADING]

    assert "Ревью требований" in titles and "Блок согласующих" in titles and "Логика работы метода" in titles


def test_two_backends_give_the_same_tables() -> None:
    mupdf = parse(PDF, DocKind.PDF, pdf_backend="mupdf")
    pdfium = parse(PDF, DocKind.PDF, pdf_backend="pdfium")

    assert table_rows(mupdf) == table_rows(pdfium)
    assert mupdf.text == pdfium.text


# ---------------------------------------------------------------- маленькие PDF на лету


def line_items(rows: list[list[str]], top: float) -> tuple[list[Cell], float]:
    """Строки таблицы (в ячейке строки через ``\\n``) в элементы страницы; возврат: y после таблицы."""
    items: list[Cell] = []
    y = top
    for row in rows:
        height = max(len(cell.split("\n")) for cell in row)
        for x, cell in zip(COLUMNS, row, strict=False):
            for k, text in enumerate(cell.split("\n")):
                if text:
                    items.append((x, y + k * STEP, text))
        y += (height - 1) * STEP + STEP + PAD
    return items, y - STEP - PAD


@pytest.fixture
def make_pdf(tmp_path: Path) -> Callable[..., Path]:
    """PDF из страниц: ``texts`` (x, y, текст) и ``rules`` (y линейки на странице, от 66 до 520)."""

    def _make(pages: list[tuple[list[Cell], list[float]]]) -> Path:
        path = tmp_path / "table.pdf"
        with pymupdf.open() as doc:
            for texts, rules in pages:
                page = doc.new_page()
                for x, y, text in texts:
                    page.insert_text((x, y), text, fontsize=SIZE, fontname="helv")
                for y in rules:
                    page.draw_line((66, y), (520, y), width=0.75)
            doc.save(path)
        return path

    return _make


def parsed(path: Path, backend: str) -> ParsedDocument:
    return parse(path, DocKind.PDF, pdf_backend=backend)


HEADER = ["Condition", "Action", "Result"]


def single_table_page(rows: list[list[str]], *, rule: bool = True) -> tuple[list[Cell], list[float]]:
    head, _ = line_items([HEADER], 100)
    body, _ = line_items(rows, 100 + STEP + PAD + 4)
    return head + body, [106] if rule else []


def test_borderless_table_is_read_by_cells(make_pdf: Callable[..., Path], backend: str) -> None:
    rows = [
        ["Spent 48500\nlimit 50000", "Buy item", "Total 50000"],
        ["Spent 19900", "Transfer 500\nto a friend", "Counter 19900"],
    ]

    doc = parsed(make_pdf([single_table_page(rows)]), backend)

    assert table_rows(doc) == {
        1: [
            ["Spent 48500 limit 50000", "Buy item", "Total 50000"],
            ["Spent 19900", "Transfer 500 to a friend", "Counter 19900"],
        ]
    }
    first = next(b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW)
    assert first.serialized == "Condition: Spent 48500 limit 50000; Action: Buy item; Result: Total 50000"
    assert (first.anchor.page, first.anchor.table, first.anchor.row) == (1, 1, 1)


def test_without_a_rule_the_text_stays_plain_with_page_anchors(
    make_pdf: Callable[..., Path], backend: str
) -> None:
    rows = [["Spent 48500", "Buy item", "Total 50000"]]

    doc = parsed(make_pdf([single_table_page(rows, rule=False)]), backend)

    assert table_rows(doc) == {}
    assert all(b.anchor.page == 1 for b in doc.blocks)
    assert "Buy item" in doc.text


def test_paragraph_after_the_table_is_not_a_row(make_pdf: Callable[..., Path], backend: str) -> None:
    rows = [["Spent 48500", "Buy item", "Total 50000"]]
    texts, rules = single_table_page(rows)
    after = [(72.0, 230.0, "The service shall send a push within one minute of the event."),
             (72.0, 243.0, "Nothing else is required here at all, this is plain prose.")]  # fmt: skip

    doc = parsed(make_pdf([(texts + after, rules)]), backend)

    assert len(table_rows(doc)[1]) == 1
    paragraph = next(b for b in doc.blocks if "shall send" in b.text)
    assert paragraph.kind is BlockKind.PARAGRAPH and paragraph.anchor.table is None


def test_paragraph_across_columns_directly_under_the_header_is_not_a_table_row(
    make_pdf: Callable[..., Path], backend: str
) -> None:
    head, _ = line_items([HEADER], 100)
    prose = [(72.0, 121.0, "This long sentence runs straight across the three columns of the table above.")]

    doc = parsed(make_pdf([(head + prose, [106])]), backend)

    rows = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert [r.serialized for r in rows] == ["Condition: (пусто); Action: (пусто); Result: (пусто)"]
    assert any("runs straight across" in b.text and b.anchor.table is None for b in doc.blocks)


def test_heading_underline_over_one_column_is_not_a_table(
    make_pdf: Callable[..., Path], backend: str
) -> None:
    texts: list[Cell] = [(72.0, 100.0, "Document title"), (72.0, 140.0, "Plain text under the title rule.")]

    doc = parsed(make_pdf([(texts, [106])]), backend)

    assert table_rows(doc) == {}
    assert "Plain text under the title rule." in doc.text


def test_rows_with_equal_spacing_are_one_row_per_line(make_pdf: Callable[..., Path], backend: str) -> None:
    rows = [["k1", "v1", "x"], ["k2", "v2", "y"], ["k3", "v3", "z"]]
    texts, rules = single_table_page(rows)
    # Те же строки, но без отступа между ними: зазоры равны, поэтому каждая строка текста это строка таблицы.
    tight = [
        (x, 124.0 + i * STEP, cell)
        for i, row in enumerate(rows)
        for x, cell in zip(COLUMNS, row, strict=True)
    ]
    head = [t for t in texts if t[1] < 110]

    doc = parsed(make_pdf([(head + tight, rules)]), backend)

    assert [r[0] for r in table_rows(doc)[1]] == ["k1", "k2", "k3"]


def test_table_continued_on_the_next_page_gets_one_number_and_merges_the_tail(
    make_pdf: Callable[..., Path], backend: str
) -> None:
    head, _ = line_items([HEADER], 80)
    first_page_rows = [["Alpha", "yes", "ok"], ["Beta", "no", "see appendix and"]]
    body1, end = line_items(first_page_rows, 80 + STEP + PAD + 4)
    # Таблица доходит до низа страницы.
    low = [(x, y + 560.0, t) for x, y, t in body1]
    head_low = [(x, y + 560.0, t) for x, y, t in head]
    second_head, _ = line_items([HEADER], 80)
    tail = [(330.0, 105.0 + 4, "the rest of cell")]
    body2, _ = line_items([["Gamma", "yes", "ok"]], 105.0 + 4 + STEP + PAD)
    del end

    doc = parsed(make_pdf([(head_low + low, [646.0]), (second_head + tail + body2, [86.0])]), backend)

    rows = table_rows(doc)
    assert list(rows) == [1]
    assert [r[0] for r in rows[1]] == ["Alpha", "Beta", "Gamma"]
    assert rows[1][1][2] == "see appendix and the rest of cell"
    assert {b.anchor.page for b in doc.blocks if b.anchor.table == 1} == {1, 2}


def test_different_header_on_the_next_page_is_a_new_table(
    make_pdf: Callable[..., Path], backend: str
) -> None:
    head, _ = line_items([HEADER], 80)
    body, _ = line_items([["Alpha", "yes", "ok"]], 80 + STEP + PAD + 4)
    other_head, _ = line_items([["Name", "Kind", "Notes"]], 80)
    other_body, _ = line_items([["Delta", "x", "y"]], 80 + STEP + PAD + 4)
    low = [(x, y + 560.0, t) for x, y, t in head + body]

    doc = parsed(make_pdf([(low, [646.0]), (other_head + other_body, [86.0])]), backend)

    assert sorted(table_rows(doc)) == [1, 2]


def test_cell_text_is_not_mixed_between_columns(make_pdf: Callable[..., Path], backend: str) -> None:
    """Главная ошибка текстового вывода: слова соседних ячеек перемешаны или склеены в одну строку."""
    rows = [["left one", "middle two", "right three"]]

    doc = parsed(make_pdf([single_table_page(rows)]), backend)

    assert table_rows(doc)[1] == [["left one", "middle two", "right three"]]
    assert "left one | middle two | right three" in doc.text


def test_hyphen_at_the_end_of_a_cell_line_is_joined(make_pdf: Callable[..., Path], backend: str) -> None:
    rows = [["Support for multi-\ndevice access", "yes", "ok"], ["Other", "no", "ok"]]

    doc = parsed(make_pdf([single_table_page(rows)]), backend)

    assert table_rows(doc)[1][0][0] == "Support for multi-device access"


# ---------------------------------------------------------------- регрессии по ревью


def test_rules_between_rows_and_under_the_last_row_do_not_split_the_table(
    make_pdf: Callable[..., Path], backend: str
) -> None:
    """Таблица с разделителями строк и нижней чертой остаётся одной таблицей без придуманных строк."""
    rows = [["Alpha", "yes", "ok"], ["Beta", "no", "ok"], ["Gamma", "yes", "no"]]
    texts, _ = single_table_page(rows)
    ys = sorted({y for _, y, _ in texts})
    separators = [(a + b) / 2 + 2 for a, b in pairwise(ys)]  # черта между строками текста

    doc = parsed(make_pdf([(texts, [106, *separators[1:], ys[-1] + 6])]), backend)

    assert [r[0] for r in table_rows(doc)[1]] == ["Alpha", "Beta", "Gamma"]
    assert sorted(table_rows(doc)) == [1]
    assert " |  | " not in doc.text


def test_note_beside_the_table_stays_in_the_text(make_pdf: Callable[..., Path], backend: str) -> None:
    rows = [["Alpha", "yes", "ok"], ["Beta", "no", "ok"]]
    texts, rules = single_table_page(rows)
    note: list[Cell] = [(545.0, 128.0, "SIDENOTE")]

    doc = parsed(make_pdf([(texts + note, rules)]), backend)

    assert "SIDENOTE" in doc.text
    assert [r[0] for r in table_rows(doc)[1]] == ["Alpha", "Beta"]


def test_text_between_two_parts_of_a_table_keeps_the_reading_order(
    make_pdf: Callable[..., Path], backend: str
) -> None:
    head, _ = line_items([HEADER], 80)
    body, _ = line_items([["Alpha", "yes", "ok"], ["Beta", "no", "ok"]], 80 + STEP + PAD + 4)
    low = [(x, y + 560.0, t) for x, y, t in head + body]
    note: list[Cell] = [(72.0, 790.0 - 20, "Note: the table goes on over the page.")]
    next_head, _ = line_items([HEADER], 80)
    next_body, _ = line_items([["Gamma", "yes", "ok"]], 80 + STEP + PAD + 4)

    doc = parsed(make_pdf([(low + note, [646.0]), (next_head + next_body, [86.0])]), backend)

    order = [b.anchor.row or 0 for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert order == [1, 2, 3]  # строки продолжаются, счёт не сбрасывается
    assert doc.text.index("Beta") < doc.text.index("Note:") < doc.text.index("Gamma")
    gamma = next(b for b in doc.blocks if "Gamma" in b.text)
    assert gamma.serialized.startswith("Condition: Gamma")  # заголовок таблицы известен и на второй странице


def test_page_number_below_the_table_is_not_a_row(make_pdf: Callable[..., Path], backend: str) -> None:
    head, _ = line_items([HEADER], 80)
    body, _ = line_items([["Alpha", "yes", "ok"], ["Beta", "no", "ok"]], 80 + STEP + PAD + 4)
    low = [(x, y + 640.0, t) for x, y, t in head + body]
    footer: list[Cell] = [(300.0, 640.0 + 138.0, "7")]  # в нижней полосе, ближе двух кеглей к строке «Beta»

    doc = parsed(make_pdf([(low + footer, [726.0])]), backend)

    assert [r[0] for r in table_rows(doc)[1]] == ["Alpha", "Beta"]


def test_table_parser_failure_leaves_the_page_as_plain_text(
    make_pdf: Callable[..., Path], backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object) -> list[object]:
        raise ValueError("странная геометрия")

    monkeypatch.setattr("validator.ingest.pdf.find_tables", broken)

    doc = parsed(make_pdf([single_table_page([["Alpha", "yes", "ok"]])]), backend)

    assert table_rows(doc) == {}
    assert "Alpha" in doc.text
