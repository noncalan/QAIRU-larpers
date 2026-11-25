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
    assert rows[0].serialized == "Поле: Имя; Значение: Значение один"


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
    shown = render_for_model(sample)

    assert "[B003] §Документ › Раздел один | абзац | строка 5" in shown
    assert "  [B003.2] Второе предложение абзаца." in shown
    assert "Поле: Имя; Значение: Значение один" in shown
    assert "метка: REQ-7" in shown
    assert set(re.findall(r"^\[(B\d{3})\]", shown, flags=re.MULTILINE)) == {b.id for b in sample.blocks}


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
    assert all(b.anchor.extraction is Extraction.TABLE and b.cells for b in rows)
    assert all(b.anchor.table is not None and b.anchor.row for b in rows)
