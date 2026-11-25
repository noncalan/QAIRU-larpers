"""Блоки документа (``build_blocks``), разбор трёх форматов и показ документа модели."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from conftest import FORMATS
from validator.ingest import build_blocks, render_for_model
from validator.models import BlockKind, DiagLevel, DocKind, Extraction, ParsedDocument, SourceLine

SAMPLE = """# Документ

## Раздел один

Первое предложение абзаца. Второе предложение абзаца.

* Пункт списка без номера.
1. Первый нумерованный пункт.
2. Второй нумерованный пункт.

| Поле | Значение |
|---|---|
| Имя | Значение один |
| Тип | Значение два |

```
код не требование
```

### Подраздел

**REQ-7.** Система должна сохранять настройку пользователя.
"""


@pytest.fixture
def sample(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(SAMPLE)


def assert_anchors_match_text(doc: ParsedDocument) -> None:
    """Главный инвариант: текст блока, предложения и ячейки это срез канонического текста."""
    for block in doc.blocks:
        a = block.anchor
        assert doc.text[a.char_start : a.char_end] == block.text, block.id
        for s in block.sentences:
            assert doc.text[s.char_start : s.char_end] == s.text, s.id
        for c in block.cells:
            assert doc.text[c.char_start : c.char_end] == c.text, (block.id, c.index)


def test_block_ids_are_sequential(sample: ParsedDocument) -> None:
    ids = [b.id for b in sample.blocks]

    assert ids == [f"B{n:03d}" for n in range(1, len(ids) + 1)]


def test_block_kinds_follow_markdown_structure(sample: ParsedDocument) -> None:
    kinds = [b.kind for b in sample.blocks]

    assert kinds == [
        BlockKind.HEADING,
        BlockKind.HEADING,
        BlockKind.PARAGRAPH,
        BlockKind.LIST_ITEM,
        BlockKind.LIST_ITEM,
        BlockKind.LIST_ITEM,
        BlockKind.TABLE_ROW,
        BlockKind.TABLE_ROW,
        BlockKind.CODE,
        BlockKind.HEADING,
        BlockKind.PARAGRAPH,
    ]


def test_sections_track_heading_stack(sample: ParsedDocument) -> None:
    paragraph = sample.blocks[2]
    last = sample.blocks[-1]

    assert paragraph.section == ["Документ", "Раздел один"]
    assert last.section == ["Документ", "Раздел один", "Подраздел"]


def test_sentences_get_block_scoped_ids(sample: ParsedDocument) -> None:
    paragraph = sample.blocks[2]

    assert [s.id for s in paragraph.sentences] == [f"{paragraph.id}.1", f"{paragraph.id}.2"]
    assert paragraph.sentences[1].text == "Второе предложение абзаца."
    assert sample.sentence(f"{paragraph.id}.2") == paragraph.sentences[1]


def test_numbered_list_items_keep_ordinal(sample: ParsedDocument) -> None:
    items = [b for b in sample.blocks if b.kind is BlockKind.LIST_ITEM]

    assert [b.ordinal for b in items] == [None, 1, 2]


def test_table_rows_have_cells_and_serialized_text(sample: ParsedDocument) -> None:
    rows = [b for b in sample.blocks if b.kind is BlockKind.TABLE_ROW]

    assert [(r.anchor.table, r.anchor.row) for r in rows] == [(1, 1), (1, 2)]
    assert [(c.column, c.text) for c in rows[0].cells] == [("Поле", "Имя"), ("Значение", "Значение один")]
    assert rows[0].serialized == "Имя: Значение один"  # таблица «поле | значение» без названий колонок


def test_table_serialization_keeps_code_marks_and_shows_empty_cells(
    make_md: Callable[[str], ParsedDocument],
) -> None:
    doc = make_md(
        "| Параметр запроса | Формат | Описание |\n|---|---|---|\n"
        "| `user_id` | **string** | |\n| | | |\n| extra | int | x | лишняя |\n"
    )
    rows = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]

    assert [r.serialized for r in rows] == [
        "Параметр запроса: `user_id`; Формат: string; Описание: (пусто)",
        "Параметр запроса: (пусто); Формат: (пусто); Описание: (пусто)",
        "Параметр запроса: extra; Формат: int; Описание: x; колонка 4: лишняя",
    ]
    assert [r.anchor.row for r in rows] == [1, 2, 3]


def test_source_label_and_obligation_become_hints(sample: ParsedDocument) -> None:
    labelled = sample.blocks[-1]

    assert labelled.source_id == "REQ-7"
    assert "obligation" in labelled.hints


def test_anchor_lines_point_into_document(sample: ParsedDocument) -> None:
    labelled = sample.blocks[-1]
    lines = sample.text.split("\n")

    assert labelled.anchor.line_start is not None
    assert lines[labelled.anchor.line_start - 1] == labelled.text
    assert sample.line_of(labelled.anchor.char_start) == labelled.anchor.line_start


def test_sample_anchors_match_text(sample: ParsedDocument) -> None:
    assert_anchors_match_text(sample)


def test_render_for_model_shows_ids_sections_and_sentences(sample: ParsedDocument) -> None:
    shown = render_for_model(sample).splitlines()

    assert "[B001] # Документ" in shown
    assert "[B002] ## Раздел один" in shown
    assert "[B003] § Раздел один | абзац | строка 5" in shown  # название документа в путь не идёт
    assert "  [B003.2] Второе предложение абзаца." in shown
    assert "[B005] § Раздел один | пункт списка 1 | строка 8" in shown
    assert "  Первый нумерованный пункт." in shown  # одно предложение показано без ID
    assert "[B007] § Раздел один | строка таблицы 1 | строка 13" in shown
    assert "  Имя: Значение один" in shown
    assert "[B011] § Раздел один › Подраздел | абзац | строка 22 | метка: REQ-7" in shown
    ids = {m for line in shown for m in re.findall(r"^\[(B\d{3})\]", line)}
    assert ids == {b.id for b in sample.blocks}


def test_render_keeps_full_path_without_common_title(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md("Вступление без заголовка.\n\n# Часть один\n\nТекст части.\n")

    shown = render_for_model(doc)

    assert "[B001] § — | абзац | строка 1" in shown
    assert "[B003] § Часть один | абзац | строка 5" in shown


def test_markdown_syntax_is_plain_text_for_other_formats() -> None:
    lines = [
        SourceLine(text="# не заголовок, а текст из PDF", page=1),
        SourceLine(text="| и не таблица |", page=1),
    ]

    blocks = build_blocks(lines, DocKind.PDF)

    assert [b.kind for b in blocks] == [BlockKind.PARAGRAPH]
    assert blocks[0].anchor.page == 1


def test_structured_table_lines_become_rows() -> None:
    lines = [
        SourceLine(text="Поле | Значение", table=1, row=0, cells=["Поле", "Значение"], header=True),
        SourceLine(text="Имя | Ответ", table=1, row=1, cells=["Имя", "Ответ"], extraction=Extraction.TABLE),
    ]

    blocks = build_blocks(lines, DocKind.DOCX)
    rows = [b for b in blocks if b.kind is BlockKind.TABLE_ROW]

    assert len(rows) == 1
    assert [(c.column, c.text) for c in rows[0].cells] == [("Поле", "Имя"), ("Значение", "Ответ")]


@pytest.mark.parametrize("kind", FORMATS, ids=lambda k: k.value)
def test_requirements_document_parses_with_valid_anchors(
    requirements_docs: dict[DocKind, ParsedDocument], kind: DocKind
) -> None:
    doc = requirements_docs[kind]

    assert doc.kind is kind
    assert len(doc.blocks) > 50
    assert not [d for d in doc.diagnostics if d.level is DiagLevel.ERROR]
    assert_anchors_match_text(doc)


def test_pdf_blocks_know_their_page(requirements_docs: dict[DocKind, ParsedDocument]) -> None:
    doc = requirements_docs[DocKind.PDF]

    assert doc.pages is not None and doc.pages > 1
    assert all(b.anchor.page is not None for b in doc.blocks)
    assert {b.anchor.page for b in doc.blocks} == set(range(1, doc.pages + 1))


def test_docx_tables_have_cells(requirements_docs: dict[DocKind, ParsedDocument]) -> None:
    rows = [b for b in requirements_docs[DocKind.DOCX].blocks if b.kind is BlockKind.TABLE_ROW]

    assert rows
    assert all(b.anchor.extraction is Extraction.TABLE for b in rows)
    assert all(b.cells for b in rows if b.text.replace("|", "").strip())  # у пустой строки ячеек нет
    assert all(b.anchor.table is not None and b.anchor.row for b in rows)


# ---------------------------------------------------------------- строки-переносы PDF (#26)


def _pdf_lines(*rows: tuple[str, bool]) -> list[SourceLine]:
    return [
        SourceLine(text=text, page=1, page_line=n, wrapped=wrapped)
        for n, (text, wrapped) in enumerate(rows, 1)
    ]


def test_wrapped_lines_continue_a_list_item() -> None:
    lines = _pdf_lines(
        ("• Система должна принимать сумму от 1 до", False),
        ("1000 единиц включительно.", True),
        ("• Система должна отклонять остальные суммы.", False),
    )

    items = build_blocks(lines, DocKind.PDF)

    assert [b.kind for b in items] == [BlockKind.LIST_ITEM, BlockKind.LIST_ITEM]
    assert items[0].text.endswith("включительно.")
    assert [b.anchor.page_line for b in items] == [1, 3]


def test_wrapped_line_that_looks_like_a_list_marker_stays_in_its_paragraph() -> None:
    lines = _pdf_lines(("Приказ вступает в силу с пункта", False), ("5. Остальное без изменений.", True))

    blocks = build_blocks(lines, DocKind.PDF)

    assert [b.kind for b in blocks] == [BlockKind.PARAGRAPH]


def test_bullet_glyph_does_not_hide_the_requirement_label() -> None:
    blocks = build_blocks(_pdf_lines(("• Ф-1. Система должна работать.", False)), DocKind.PDF)

    assert blocks[0].source_id == "Ф-1"


def test_furniture_blocks_are_not_shown_to_the_model() -> None:
    lines = [
        SourceLine(text="Система должна работать.", page=1),
        SourceLine(text="Стр. 1 из 3", page=1, kind=BlockKind.FURNITURE),
    ]
    doc = ParsedDocument(
        path="x.pdf",
        name="x.pdf",
        kind=DocKind.PDF,
        sha256="0",
        size_bytes=1,
        text="\n".join(line.text for line in lines),
        blocks=build_blocks(lines, DocKind.PDF),
    )

    shown = render_for_model(doc)

    assert "Система должна работать." in shown and "Стр. 1" not in shown
