"""Разбор DOCX (шаг 1): стили, нумерация, таблицы по ячейкам, сноски, колонтитулы, правки, пустые файлы."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import docx
import pytest
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from typer.testing import CliRunner

from conftest import REQUIREMENTS
from docx_factory import cell, footnote_ref, make_docx, para, row, run, table
from validator.cli import app
from validator.errors import IN_DOCX_TEXT_BOXES, IN_DOCX_TRACKED_CHANGES, IN_HEADINGS_ONLY, IN_NO_TEXT
from validator.ingest import parse
from validator.models import (
    Block,
    BlockKind,
    DiagLevel,
    DocKind,
    Extraction,
    ParsedDocument,
    Report,
)
from validator.report.fmt import where
from validator.text import span_anchor


def read(tmp_path: Path, body: str, **parts: object) -> ParsedDocument:
    return parse(make_docx(tmp_path / "doc.docx", body, **parts), DocKind.DOCX)  # type: ignore[arg-type]


def texts(doc: ParsedDocument, kind: BlockKind) -> list[str]:
    return [b.text for b in doc.blocks if b.kind is kind]


def block_with(doc: ParsedDocument, phrase: str) -> Block:
    start = doc.text.index(phrase)
    anchor = span_anchor(doc, start, start + len(phrase))
    block = doc.block(anchor.block_ids[0])
    assert block is not None
    return block


# ---------------------------------------------------------------- наш документ


@pytest.fixture(scope="module")
def requirements_docx() -> ParsedDocument:
    return parse(REQUIREMENTS[DocKind.DOCX], DocKind.DOCX)


def test_requirements_docx_has_expected_blocks_and_tables(requirements_docx: ParsedDocument) -> None:
    counts = Counter(b.kind for b in requirements_docx.blocks)

    assert len(requirements_docx.blocks) == 120
    assert counts[BlockKind.TABLE_ROW] == 44
    assert counts[BlockKind.HEADING] == 34
    assert counts[BlockKind.CODE] == 7  # соседние абзацы «Source Code» не склеены с обычным текстом
    assert max(b.anchor.table or 0 for b in requirements_docx.blocks) == 7
    assert not requirements_docx.diagnostics


def test_requirements_docx_paragraph_anchor(requirements_docx: ParsedDocument) -> None:
    block = block_with(requirements_docx, "Месяц определяется по дате")

    assert block.kind is BlockKind.LIST_ITEM
    assert block.anchor.paragraph == 17
    assert block.source_id == "Ф-2"
    assert block.section[-1] == "Функциональные требования"
    assert where(block.anchor) == "абзац 17"


def test_requirements_docx_table_cell_anchor(requirements_docx: ParsedDocument) -> None:
    phrase = "Сохранение порога. Функциональные требования: чтение порога"

    start = requirements_docx.text.index(phrase)
    anchor = span_anchor(requirements_docx, start, start + len(phrase))

    assert (anchor.table, anchor.row, anchor.cell, anchor.column) == (5, 1, 2, "Значение")
    assert anchor.extraction is Extraction.TABLE
    assert where(anchor) == "таблица 5, строка 1, ячейка 2, колонка «Значение»"


def test_md_and_docx_share_at_least_95_percent_of_words(requirements_docx: ParsedDocument) -> None:
    md = parse(REQUIREMENTS[DocKind.MD], DocKind.MD)
    words = lambda doc: Counter(re.findall(r"\w+", doc.text.lower()))  # noqa: E731

    common = sum((words(md) & words(requirements_docx)).values())

    assert common / sum(words(md).values()) >= 0.95
    assert common / sum(words(requirements_docx).values()) >= 0.95


def test_md_and_docx_have_same_table_structure(requirements_docx: ParsedDocument) -> None:
    md = parse(REQUIREMENTS[DocKind.MD], DocKind.MD)

    def shape(doc: ParsedDocument) -> list[tuple[int | None, int | None]]:
        return [(b.anchor.table, b.anchor.row) for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]

    assert shape(md) == shape(requirements_docx)


def test_all_anchors_point_into_text(requirements_docx: ParsedDocument) -> None:
    for b in requirements_docx.blocks:
        assert requirements_docx.text[b.anchor.char_start : b.anchor.char_end] == b.text
        for c in b.cells:
            assert requirements_docx.text[c.char_start : c.char_end] == c.text


# ---------------------------------------------------------------- заголовки и нумерация


def test_headings_by_style_name_based_style_and_outline_level(tmp_path: Path) -> None:
    body = (
        para("Основной", style="Heading1")
        + para("Через основу стиля", style="MyHeading")
        + para("По уровню структуры", style="Outlined")
        + para("Обычный абзац.")
    )

    doc = read(tmp_path, body)

    assert [b.kind for b in doc.blocks] == [BlockKind.HEADING] * 3 + [BlockKind.PARAGRAPH]
    assert doc.blocks[3].section == ["Основной", "Через основу стиля", "По уровню структуры"]


def test_style_based_on_itself_does_not_hang(tmp_path: Path) -> None:
    doc = read(tmp_path, para("Текст со стилем с циклом.", style="Loop"))

    assert [b.kind for b in doc.blocks] == [BlockKind.PARAGRAPH]


def test_numbering_labels_follow_word_counters(tmp_path: Path) -> None:
    body = (
        para("Первый пункт.", num=(1, 0))
        + para("Вложенный пункт.", num=(1, 1))
        + para("Буква.", num=(1, 2))
        + para("Второй пункт.", num=(1, 0))
        + para("Снова первый вложенный.", num=(1, 1))
        + para("Маркер.", num=(2, 0))
        + para("С переопределением начала.", num=(3, 0))
        + para("Нумерация из стиля.", style="Numbered")
    )

    doc = read(tmp_path, body)

    assert texts(doc, BlockKind.LIST_ITEM) == [
        "1. Первый пункт.",
        "1.1. Вложенный пункт.",
        "a) Буква.",
        "2. Второй пункт.",
        "2.1. Снова первый вложенный.",
        "• Маркер.",
        "5. С переопределением начала.",
        "6. Нумерация из стиля.",
    ]
    assert [b.ordinal for b in doc.blocks] == [1, None, None, 2, None, None, 5, 6]
    assert all(b.kind is BlockKind.LIST_ITEM for b in doc.blocks)


def test_numbered_heading_label_goes_into_title_and_section_path(tmp_path: Path) -> None:
    body = para("Хранение", style="Heading1", num=(1, 0)) + para("Срок", style="Heading2", num=(1, 1))
    body += para("Система хранит запись один год.")

    doc = read(tmp_path, body)

    assert doc.blocks[-1].section == ["1. Хранение", "1.1. Срок"]


def test_requirement_label_is_found_after_list_label(tmp_path: Path) -> None:
    doc = read(tmp_path, para("ТР-4. Система должна хранить запись.", num=(1, 1)))

    block = doc.blocks[0]
    assert block.text.startswith("1.1. ТР-4.")
    assert block.source_id == "ТР-4"
    assert block.sentences[0].text.startswith("ТР-4")  # метка списка «1.1.» в предложение не входит


def test_line_break_inside_list_item_stays_in_one_block(tmp_path: Path) -> None:
    body = '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr>'
    body += f"{run('Первая строка пункта')}<w:r><w:br/></w:r>{run('вторая строка пункта')}</w:p>"

    doc = read(tmp_path, body)

    assert len(doc.blocks) == 1
    assert doc.blocks[0].text == "• Первая строка пункта\nвторая строка пункта"
    assert doc.blocks[0].anchor.paragraph == 1


# ---------------------------------------------------------------- таблицы


REQUIREMENT_ROWS = (
    ("REQ-1", "Система должна архивировать закрытые заявки."),
    ("REQ-2", "Портал должен уведомлять автора об отклонении."),
    ("REQ-3", "Клиент должен подписывать акт электронной подписью."),
)


def _write_table(
    path: Path,
    rows: tuple[tuple[str, ...], ...],
    *,
    look: str = "default",
    header: str | None = None,
) -> Path:
    document = docx.Document()
    word_table = document.add_table(rows=len(rows), cols=len(rows[0]))
    for word_row, values in zip(word_table.rows, rows, strict=True):
        for word_cell, value in zip(word_row.cells, values, strict=True):
            word_cell.text = value
    style = word_table._tbl.tblPr.find(qn("w:tblLook"))
    assert style is not None
    if look != "default":
        style.attrib.clear()
        if look == "absent":
            word_table._tbl.tblPr.remove(style)
        elif look == "firstRow":
            style.set(qn("w:firstRow"), "1")
        elif look == "bitmask":
            style.set(qn("w:val"), "0020")
    if header is not None:
        marker = OxmlElement("w:tblHeader")
        if header:
            marker.set(qn("w:val"), header)
        word_table.rows[0]._tr.get_or_add_trPr().append(marker)
    document.save(path)
    return path


@pytest.mark.parametrize("look", ["default", "empty", "absent", "firstRow", "bitmask"])
def test_table_look_keeps_all_requirement_rows(tmp_path: Path, look: str) -> None:
    path = _write_table(tmp_path / "requirements.docx", REQUIREMENT_ROWS, look=look)

    doc = parse(path, DocKind.DOCX)
    assert [b.id for b in doc.blocks] == ["B001", "B002", "B003"]
    for index, (block, (label, text)) in enumerate(zip(doc.blocks, REQUIREMENT_ROWS, strict=True), 1):
        assert block.kind is BlockKind.TABLE_ROW
        assert block.cells[0].text == label
        assert block.text == f"{label} | {text}"
        assert block.serialized == f"колонка 1: {label}; колонка 2: {text}"
        assert (block.anchor.table, block.anchor.row) == (1, index)
        assert doc.text[block.anchor.char_start : block.anchor.char_end] == block.text


def test_inferred_columns_keep_the_first_row_citable(tmp_path: Path) -> None:
    path = _write_table(tmp_path / "labels.docx", (("ID", "Описание"), *REQUIREMENT_ROWS))

    doc = parse(path, DocKind.DOCX)
    assert [b.kind for b in doc.blocks] == [BlockKind.TABLE_ROW] * 4
    assert [b.anchor.row for b in doc.blocks] == [1, 2, 3, 4]
    assert doc.blocks[0].serialized == "колонка 1: ID; колонка 2: Описание"
    assert doc.blocks[1].serialized == f"ID: {REQUIREMENT_ROWS[0][0]}; Описание: {REQUIREMENT_ROWS[0][1]}"
    for index, expected in ((0, "колонка 2"), (1, "Описание")):
        cell = doc.blocks[index].cells[1]
        anchor = span_anchor(doc, cell.char_start, cell.char_end)
        assert (anchor.table, anchor.row, anchor.cell, anchor.column) == (1, index + 1, 2, expected)


def test_ragged_table_keeps_every_cell_in_model_input(tmp_path: Path) -> None:
    doc = read(
        tmp_path,
        table(
            row(cell("Поле"), cell("Значение")),
            row(cell("Период"), cell("30 дней")),
            row(cell("Срок"), cell("10 дней"), cell("После уведомления")),
        ),
    )

    assert [b.anchor.row for b in doc.blocks] == [1, 2, 3]
    assert doc.blocks[2].serialized == "колонка 1: Срок; колонка 2: 10 дней; колонка 3: После уведомления"


@pytest.mark.parametrize("header", ["", "1", "true", "on"])
def test_explicit_table_header_supplies_columns_without_a_block(tmp_path: Path, header: str) -> None:
    path = _write_table(tmp_path / "labels.docx", (("ID", "Описание"), *REQUIREMENT_ROWS), header=header)

    doc = parse(path, DocKind.DOCX)
    assert doc.text.startswith("ID | Описание\n")
    assert [b.anchor.row for b in doc.blocks] == [1, 2, 3]
    assert [c.column for c in doc.blocks[0].cells] == ["ID", "Описание"]


@pytest.mark.parametrize("header", ["0", "false", "off"])
def test_disabled_table_header_keeps_fallback_columns(tmp_path: Path, header: str) -> None:
    path = _write_table(tmp_path / "labels.docx", (("ID", "Описание"), *REQUIREMENT_ROWS), header=header)

    doc = parse(path, DocKind.DOCX)
    assert [b.anchor.row for b in doc.blocks] == [1, 2, 3, 4]
    assert [c.column for c in doc.blocks[1].cells] == ["колонка 1", "колонка 2"]


def test_single_explicit_header_row_is_still_data(tmp_path: Path) -> None:
    path = _write_table(tmp_path / "one-row.docx", (("ID", "Описание"),), header="")

    doc = parse(path, DocKind.DOCX)
    assert [(b.kind, b.anchor.row, b.text) for b in doc.blocks] == [(BlockKind.TABLE_ROW, 1, "ID | Описание")]


@pytest.mark.parametrize(
    "first",
    [
        ("Сервис", "Должен отвечать"),
        ("Сервис", "Может отвечать"),
        ("Service", "May respond"),
        ("Service", "Can respond"),
        ("REQ-1", "Архивация заявок"),
    ],
)
def test_short_requirement_row_does_not_supply_columns(tmp_path: Path, first: tuple[str, ...]) -> None:
    path = _write_table(tmp_path / "requirements.docx", (first, REQUIREMENT_ROWS[1]))

    doc = parse(path, DocKind.DOCX)
    assert [b.anchor.row for b in doc.blocks] == [1, 2]
    assert [cell.column for cell in doc.blocks[1].cells] == ["колонка 1", "колонка 2"]


def test_fake_cli_segments_all_unmarked_table_rows(tmp_path: Path) -> None:
    path = _write_table(tmp_path / "requirements.docx", REQUIREMENT_ROWS)
    out = tmp_path / "report.md"

    result = CliRunner().invoke(app, [str(path), str(out), "--backend", "fake"])

    assert result.exit_code == 0, result.output
    report = Report.model_validate_json(out.with_suffix(".json").read_text(encoding="utf-8"))
    assert report.segmentation is not None
    assert [(r.source_id, r.anchor.row, r.quote) for r in report.segmentation.requirements] == [
        (label, index, f"{label} | {text}") for index, (label, text) in enumerate(REQUIREMENT_ROWS, 1)
    ]


def test_table_cells_have_columns_offsets_and_row_numbers_without_header(tmp_path: Path) -> None:
    body = para("Сразу перед таблицей.") + table(
        row(cell("Поле"), cell("Условие"), cell("Результат"), header=True),
        row(cell("Порог"), cell("Меньше 1000"), cell("Ошибка")),
        row(cell("Месяц"), cell("Граница"), cell("")),
    )

    doc = read(tmp_path, body)

    rows = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert [(r.anchor.table, r.anchor.row) for r in rows] == [(1, 1), (1, 2)]
    assert [(c.index, c.column, c.text) for c in rows[0].cells] == [
        (1, "Поле", "Порог"),
        (2, "Условие", "Меньше 1000"),
        (3, "Результат", "Ошибка"),
    ]
    assert rows[1].serialized == "Поле: Месяц; Условие: Граница; Результат: (пусто)"
    anchor = span_anchor(doc, doc.text.index("Меньше 1000"), doc.text.index("Меньше 1000") + 5)
    assert (anchor.table, anchor.row, anchor.cell, anchor.paragraph) == (1, 1, 2, None)
    assert doc.blocks[0].anchor.paragraph == 1


def test_vertically_merged_cell_is_not_reported_as_empty(tmp_path: Path) -> None:
    body = table(
        row(cell("Код"), cell("Описание"), header=True),
        row(cell("Ошибка", vmerge="restart"), cell("Первый случай")),
        row(cell("", vmerge=""), cell("Второй случай")),
    )

    doc = read(tmp_path, body)

    assert [b.serialized for b in doc.blocks] == [
        "Код: Ошибка; Описание: Первый случай",
        "Код: Ошибка; Описание: Второй случай",
    ]
    assert all("(пусто)" not in b.serialized for b in doc.blocks)


def test_horizontally_merged_cell_appears_once(tmp_path: Path) -> None:
    body = table(
        row(cell("A"), cell("B"), cell("C"), header=True),
        row(cell("Весь ряд одной ячейкой", span=3)),
    )

    doc = read(tmp_path, body)

    assert [c.text for c in doc.blocks[0].cells] == ["Весь ряд одной ячейкой"]
    assert doc.text.count("Весь ряд одной ячейкой") == 1


def test_nested_table_text_is_not_lost(tmp_path: Path) -> None:
    inner = table(row(cell("вложено А"), cell("вложено Б")))
    body = table(
        row(cell("Имя"), cell("Значение"), header=True), row(cell("Ключ"), cell("Внешний", inner=inner))
    )

    doc = read(tmp_path, body)

    assert "вложено А | вложено Б" in doc.text
    assert doc.blocks[0].cells[1].text == "Внешний вложено А | вложено Б"


def test_table_inside_content_control_is_read(tmp_path: Path) -> None:
    content = para("Абзац в элементе управления.") + table(row(cell("А"), cell("Б")))
    body = f"<w:sdt><w:sdtContent>{content}</w:sdtContent></w:sdt>"

    doc = read(tmp_path, body)

    assert [b.kind for b in doc.blocks] == [BlockKind.PARAGRAPH, BlockKind.TABLE_ROW]


# ---------------------------------------------------------------- сноски, колонтитулы, правки


def test_footnote_is_a_labelled_block_after_its_paragraph(tmp_path: Path) -> None:
    body = para("Срок хранения указан в приложении.", raw=footnote_ref(1)) + para("Следующий абзац.")

    doc = read(tmp_path, body, footnotes={1: "Приложение Б, таблица сроков."})

    assert [b.kind for b in doc.blocks] == [BlockKind.PARAGRAPH, BlockKind.FOOTNOTE, BlockKind.PARAGRAPH]
    assert doc.blocks[1].text == "Приложение Б, таблица сроков."
    assert doc.blocks[2].anchor.paragraph == 2  # сноска не сдвигает нумерацию абзацев


def test_header_and_footer_are_furniture_at_the_start_without_page_numbers(tmp_path: Path) -> None:
    body = para("Раздел", style="Heading1") + para("Текст раздела.")

    doc = read(tmp_path, body, header=para("Конфиденциально, версия 2"), footer=para("12"))

    assert [b.kind for b in doc.blocks] == [BlockKind.FURNITURE, BlockKind.HEADING, BlockKind.PARAGRAPH]
    assert doc.blocks[0].section == []
    assert doc.blocks[0].text == "Конфиденциально, версия 2"


def test_tracked_changes_are_read_as_accepted_with_info(tmp_path: Path) -> None:
    body = (
        "<w:p>"
        + run("Система должна хранить ")
        + '<w:ins w:id="1" w:author="a">'
        + run("настройку ")
        + "</w:ins>"
        + '<w:del w:id="2" w:author="a"><w:r><w:delText>удалённое слово </w:delText></w:r></w:del>'
        + run("пользователя.")
        + "</w:p>"
    )

    doc = read(tmp_path, body)

    assert doc.blocks[0].text == "Система должна хранить настройку пользователя."
    info = next(d for d in doc.diagnostics if d.code == IN_DOCX_TRACKED_CHANGES)
    assert info.level is DiagLevel.INFO


def test_text_boxes_are_reported_not_silently_dropped(tmp_path: Path) -> None:
    inner = "<w:p><w:r><w:t>Текст в рамке</w:t></w:r></w:p>"
    box = f"<w:r><w:drawing><w:txbxContent>{inner}</w:txbxContent></w:drawing></w:r>"

    doc = read(tmp_path, para("Основной текст документа.", raw=box))

    assert "Текст в рамке" not in doc.text
    warning = next(d for d in doc.diagnostics if d.code == IN_DOCX_TEXT_BOXES)
    assert warning.level is DiagLevel.WARNING


def test_hyperlink_and_tab_text_is_kept(tmp_path: Path) -> None:
    link = f"<w:hyperlink>{run('по ссылке')}</w:hyperlink>"
    body = f"<w:p>{run('Смотрите')}<w:r><w:tab/></w:r>{link}{run(' подробнее.')}</w:p>"

    doc = read(tmp_path, body)

    assert doc.blocks[0].text == "Смотрите по ссылке подробнее."


# ---------------------------------------------------------------- пустые и битые


def test_empty_docx_gives_error_diagnostic_and_empty_result(tmp_path: Path) -> None:
    doc = read(tmp_path, para("") + para("   "))

    assert doc.text == "" and doc.blocks == []
    assert [d.code for d in doc.diagnostics] == [IN_NO_TEXT]
    assert doc.diagnostics[0].level is DiagLevel.ERROR


def test_docx_of_empty_table_cells_has_no_text_blocks(tmp_path: Path) -> None:
    doc = read(tmp_path, table(row(cell("Поле"), cell("Значение"), header=True), row(cell(""), cell(""))))

    assert [d.code for d in doc.diagnostics] == [IN_NO_TEXT]


def test_docx_with_only_headings_is_warned_as_template(tmp_path: Path) -> None:
    doc = read(tmp_path, para("Цели", style="Heading1") + para("Требования", style="Heading1"))

    assert [d.code for d in doc.diagnostics] == [IN_HEADINGS_ONLY]


def test_list_that_restarts_at_one_shows_one_even_if_level_starts_at_five(tmp_path: Path) -> None:
    body = (
        para("Обычный старт.", num=(4, 0)) + para("Следующий.", num=(4, 0)) + para("С единицы.", num=(5, 0))
    )

    doc = read(tmp_path, body)

    assert texts(doc, BlockKind.LIST_ITEM) == ["5. Обычный старт.", "6. Следующий.", "1. С единицы."]


def test_empty_body_with_header_is_still_an_empty_document(tmp_path: Path) -> None:
    doc = read(tmp_path, para(""), header=para("Конфиденциально"))

    assert doc.blocks == []
    assert [d.code for d in doc.diagnostics] == [IN_NO_TEXT]


def test_text_box_in_header_is_reported(tmp_path: Path) -> None:
    inner = "<w:p><w:r><w:t>В рамке</w:t></w:r></w:p>"
    box = f"<w:r><w:drawing><w:txbxContent>{inner}</w:txbxContent></w:drawing></w:r>"

    doc = read(tmp_path, para("Основной текст документа."), header=f"<w:p>{box}</w:p>")

    assert IN_DOCX_TEXT_BOXES in {d.code for d in doc.diagnostics}


def test_paragraph_mark_revision_alone_is_not_reported_as_tracked_change(tmp_path: Path) -> None:
    mark = '<w:pPr><w:rPr><w:ins w:id="1" w:author="a"/></w:rPr></w:pPr>'

    doc = read(tmp_path, f"<w:p>{mark}{run('Текст без правок.')}</w:p>")

    assert IN_DOCX_TRACKED_CHANGES not in {d.code for d in doc.diagnostics}
