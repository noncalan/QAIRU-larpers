"""Разбор Markdown (шаг 1): структура, якоря, кодировки, пустые документы."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import REQUIREMENTS
from validator.errors import IN_ENCODING, IN_ENCODING_UNCERTAIN, IN_HEADINGS_ONLY, IN_NO_TEXT, InputError
from validator.ingest import parse
from validator.ingest.md import decode_text
from validator.models import Block, BlockKind, DiagLevel, DocKind, ParsedDocument
from validator.text import span_anchor

MakeMd = Callable[[str], ParsedDocument]
PROSE = (
    "# Требования к системе\n\n## Хранение данных\n\n"
    "Система должна хранить настройку пользователя не менее одного года. "
    "Удаление настройки возможно только по запросу владельца.\n\n"
    "## Доступ\n\nДоступ к настройкам имеет только владелец учётной записи.\n"
)


def codes(doc: ParsedDocument) -> set[str]:
    return {d.code for d in doc.diagnostics}


def kinds(doc: ParsedDocument) -> list[BlockKind]:
    return [b.kind for b in doc.blocks]


def block_with(doc: ParsedDocument, phrase: str) -> Block:
    start = doc.text.index(phrase)
    anchor = span_anchor(doc, start, start + len(phrase))
    assert anchor.block_ids, phrase
    block = doc.block(anchor.block_ids[0])
    assert block is not None
    return block


# ---------------------------------------------------------------- наш документ


def test_requirements_md_has_expected_blocks_and_tables() -> None:
    doc = parse(REQUIREMENTS[DocKind.MD], DocKind.MD)

    by_kind = {k: kinds(doc).count(k) for k in set(kinds(doc))}
    assert len(doc.blocks) == 117
    assert by_kind[BlockKind.TABLE_ROW] == 44
    assert by_kind[BlockKind.CODE] == 7
    assert max(b.anchor.table or 0 for b in doc.blocks) == 7
    assert not doc.diagnostics


@pytest.mark.parametrize(
    ("phrase", "section"),
    [
        ("Месяц определяется по дате", "Функциональные требования"),
        ("Сервис передаёт push на отправку", "Нефункциональные требования"),
        ("заполняются тестировщиками", "Тестирование требований"),
    ],
)
def test_requirements_md_phrase_anchors_point_to_source_line(phrase: str, section: str) -> None:
    path = REQUIREMENTS[DocKind.MD]
    doc = parse(path, DocKind.MD)

    block = block_with(doc, phrase)

    source_lines = path.read_text(encoding="utf-8").splitlines()
    assert phrase in source_lines[(block.anchor.line_start or 0) - 1]
    assert block.section[-1] == section


def test_requirements_md_table_cell_anchor_has_table_row_and_cell() -> None:
    doc = parse(REQUIREMENTS[DocKind.MD], DocKind.MD)
    phrase = "Месячный порог расходов по карте"
    start = doc.text.index(phrase, doc.text.index("Наименование задачи"))

    anchor = span_anchor(doc, start, start + len(phrase))

    assert (anchor.table, anchor.row, anchor.cell, anchor.column) == (1, 1, 2, "Значение")


# ---------------------------------------------------------------- разметка


def test_setext_headings_join_the_section_stack(make_md: MakeMd) -> None:
    doc = make_md("Название\n========\n\nПодраздел\n---------\n\nТекст подраздела.\n")

    assert kinds(doc) == [BlockKind.HEADING, BlockKind.HEADING, BlockKind.PARAGRAPH]
    assert doc.blocks[2].section == ["Название", "Подраздел"]
    assert [b.anchor.line_start for b in doc.blocks] == [1, 4, 7]


def test_long_paragraph_above_dashes_is_not_a_heading(make_md: MakeMd) -> None:
    sentence = "Система должна сохранять настройку пользователя и возвращать её при следующем входе. " * 4
    doc = make_md(f"# Док\n\n{sentence.strip()}\n---\nСледующий абзац.\n")

    assert kinds(doc) == [BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.PARAGRAPH]


def test_thematic_break_after_blank_line_is_skipped(make_md: MakeMd) -> None:
    doc = make_md("Первый абзац.\n\n---\n\nВторой абзац.\n")

    assert kinds(doc) == [BlockKind.PARAGRAPH, BlockKind.PARAGRAPH]


def test_quote_block_keeps_lines_and_is_labelled(make_md: MakeMd) -> None:
    doc = make_md("Текст до.\n\n> Цитата строка один\n> строка два\n\nТекст после.\n")

    quote = doc.blocks[1]
    assert quote.kind is BlockKind.QUOTE
    assert (quote.anchor.line_start, quote.anchor.line_end) == (3, 4)
    assert doc.text[quote.anchor.char_start : quote.anchor.char_end] == quote.text


def test_fenced_code_is_one_block_and_longer_fence_survives_inner_fence(make_md: MakeMd) -> None:
    doc = make_md("````\n```\nвложенный блок\n```\n````\n\nТекст после кода.\n")

    assert kinds(doc) == [BlockKind.CODE, BlockKind.PARAGRAPH]
    assert "вложенный блок" in doc.blocks[0].text


def test_front_matter_is_code_not_heading(make_md: MakeMd) -> None:
    doc = make_md("---\ntitle: Спецификация\nversion: 3\n---\n\n# Раздел\n\nТекст.\n")

    assert kinds(doc) == [BlockKind.CODE, BlockKind.HEADING, BlockKind.PARAGRAPH]
    assert doc.blocks[0].text == "title: Спецификация\nversion: 3"


def test_table_without_outer_pipes_is_split_into_cells(make_md: MakeMd) -> None:
    doc = make_md("Метод | Путь\n--- | ---\nGET | /cards\nPUT | /cards/limit\n\nПосле таблицы.\n")

    rows = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert [(r.anchor.table, r.anchor.row) for r in rows] == [(1, 1), (1, 2)]
    assert [c.text for c in rows[1].cells] == ["PUT", "/cards/limit"]
    assert rows[1].serialized == "Метод: PUT; Путь: /cards/limit"
    assert doc.blocks[-1].kind is BlockKind.PARAGRAPH


def test_two_tables_get_consecutive_numbers_and_cell_offsets_are_exact(make_md: MakeMd) -> None:
    doc = make_md(
        "| a | b |\n|---|---|\n| один | два |\n\nТекст.\n\n| c | d |\n|---|---|\n| три | четыре |\n"
    )

    rows = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert [r.anchor.table for r in rows] == [1, 2]
    for row in rows:
        for c in row.cells:
            assert doc.text[c.char_start : c.char_end] == c.text


def test_anchor_lines_match_source_for_crlf_files(tmp_path: Path) -> None:
    path = tmp_path / "crlf.md"
    path.write_bytes(PROSE.replace("\n", "\r\n").encode("utf-8"))

    doc = parse(path, DocKind.MD)

    lines = PROSE.splitlines()
    for block in doc.blocks:
        assert block.text.splitlines()[0] in lines[(block.anchor.line_start or 0) - 1]


# ---------------------------------------------------------------- кодировки


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp1251", "koi8-r", "utf-16"])
def test_encodings_are_read_with_same_text(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / f"{encoding}.md"
    path.write_bytes(PROSE.encode(encoding))

    doc = parse(path, DocKind.MD)

    assert doc.text.rstrip("\n") == PROSE.rstrip("\n")
    assert IN_ENCODING_UNCERTAIN not in codes(doc)
    assert (IN_ENCODING in codes(doc)) is (encoding != "utf-8-sig")


def test_utf8_bom_is_dropped_without_diagnostic(tmp_path: Path) -> None:
    path = tmp_path / "bom.md"
    path.write_bytes(b"\xef\xbb\xbf" + PROSE.encode("utf-8"))

    doc = parse(path, DocKind.MD)

    assert doc.text.startswith("# Требования")
    assert not doc.diagnostics


def test_short_text_in_ambiguous_encoding_gets_low_confidence_warning(tmp_path: Path) -> None:
    path = tmp_path / "short.md"
    path.write_bytes("# Требования\n\nСистема должна работать.\n".encode("koi8-r"))

    doc = parse(path, DocKind.MD)

    warning = next(d for d in doc.diagnostics if d.code == IN_ENCODING_UNCERTAIN)
    assert warning.level is DiagLevel.WARNING
    assert warning.hint


def test_mixed_utf8_and_cp1251_is_read_with_warning_not_rejected(tmp_path: Path) -> None:
    """Ревью #144: UTF-8-файл с заметной долей строк в cp1251 давал IN-061 и код 4; теперь предупреждение."""
    path = tmp_path / "mixed.md"
    good = PROSE.encode("utf-8")
    foreign = "Система должна хранить настройку не менее года.\n".encode("cp1251")
    path.write_bytes(good + b"\n" + foreign * 2 + good)

    doc = parse(path, DocKind.MD)

    warning = next(d for d in doc.diagnostics if d.code == IN_ENCODING_UNCERTAIN)
    assert warning.level is DiagLevel.WARNING
    assert "UTF-8" in warning.message
    assert "Требования к системе" in doc.text
    assert not any(d.level is DiagLevel.ERROR for d in doc.diagnostics)


def test_stray_nul_bytes_are_removed_and_reported(tmp_path: Path) -> None:
    """Ревью #144: нулевой байт из входа попадал в report.md, а диагностика врала про «не UTF-8»."""
    path = tmp_path / "nul.md"
    damaged = PROSE.replace("Доступ к", "Доступ\x00 к", 1).replace("Система", "Систе\x00ма", 1)
    path.write_bytes(damaged.encode("utf-8"))

    doc = parse(path, DocKind.MD)

    assert "\x00" not in doc.text and all("\x00" not in b.text for b in doc.blocks)
    assert "Доступ к настройкам" in doc.text
    assert IN_ENCODING not in codes(doc)
    warning = next(d for d in doc.diagnostics if d.code == IN_ENCODING_UNCERTAIN)
    assert "нулев" in warning.message and "удалены" in warning.action


def test_binary_garbage_still_has_no_encoding() -> None:
    with pytest.raises(InputError) as excinfo:
        decode_text(bytes(range(128, 256)) * 8)

    assert excinfo.value.code == IN_ENCODING


# ---------------------------------------------------------------- пустые документы


@pytest.mark.parametrize("text", ["  \n\n\t\n", "---\n\n| |\n|---|\n| |\n", "```\n```\n", "#\n"])
def test_document_without_text_blocks_gives_error_and_empty_result(make_md: MakeMd, text: str) -> None:
    doc = make_md(text)

    assert doc.text == "" and doc.blocks == []
    error = next(d for d in doc.diagnostics if d.code == IN_NO_TEXT)
    assert error.level is DiagLevel.ERROR


def test_document_with_only_headings_is_warned_as_template(make_md: MakeMd) -> None:
    doc = make_md("# Спецификация\n\n## Цели\n\n## Требования\n")

    assert doc.blocks
    assert [d.code for d in doc.diagnostics] == [IN_HEADINGS_ONLY]
    assert doc.diagnostics[0].level is DiagLevel.WARNING


def test_requirement_in_blockquote_keeps_sentences_and_obligation_hint(make_md: MakeMd) -> None:
    doc = make_md("> ФТ-1. Система должна хранить запись один год. Срок не меняется.\n")

    quote = doc.blocks[0]
    assert quote.kind is BlockKind.QUOTE
    assert [s.text for s in quote.sentences][-1] == "Срок не меняется."
    assert quote.sentences[0].text.startswith("ФТ-1.")
    assert "obligation" in quote.hints


def test_short_dash_separator_without_outer_pipes_is_a_separator_not_a_row(make_md: MakeMd) -> None:
    doc = make_md("A | B\n-- | --\nx | y\n")

    rows = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert [r.serialized for r in rows] == ["A: x; B: y"]


def test_prose_between_dashes_is_not_front_matter(make_md: MakeMd) -> None:
    doc = make_md("---\nПримечание: все сроки ориентировочные\n---\n\nСистема должна хранить запись.\n")

    assert BlockKind.CODE not in kinds(doc)
    assert any("Система должна" in b.text for b in doc.blocks)


def test_underline_below_long_paragraph_is_not_a_stray_block(make_md: MakeMd) -> None:
    long = "\n".join(["Система должна сохранять настройку пользователя."] * 3)
    doc = make_md(f"{long}\n=======\n\nСледующий абзац.\n")

    assert [b.text for b in doc.blocks][-1] == "Следующий абзац."
    assert all("=====" not in b.text for b in doc.blocks)
