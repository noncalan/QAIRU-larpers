"""Склейка строк ячейки таблицы PDF (#121): слово, разорванное у края узкой колонки, и обычный перенос."""

from __future__ import annotations

from validator.ingest.pdf_cell_text import CellLine, join_lines, join_tail, vocabulary

VOCAB = frozenset({"идентификатор", "карты", "архитектура", "системы", "взаимодействия"})


def test_without_vocabulary_lines_join_with_a_space() -> None:
    lines = [CellLine("Иденти", single=True, at_edge=True), CellLine("фикатор карты")]

    assert join_lines(lines) == "Иденти фикатор карты"


def test_fragment_glued_when_the_whole_word_is_in_the_document() -> None:
    lines = [CellLine("Иденти", single=True), CellLine("фикатор карты")]

    assert join_lines(lines, VOCAB) == "Идентификатор карты"


def test_short_tail_glued_to_a_fragment_at_the_column_edge() -> None:
    lines = [CellLine("Признак"), CellLine("включен", single=True, at_edge=True), CellLine("ия")]

    assert join_lines(lines, VOCAB) == "Признак включения"


def test_short_tail_is_not_glued_when_the_line_ends_before_the_edge() -> None:
    lines = [CellLine("включен", single=True, at_edge=False), CellLine("ия")]

    assert join_lines(lines, VOCAB) == "включен ия"


def test_known_word_at_the_edge_is_a_normal_wrap() -> None:
    lines = [CellLine("взаимодействия", single=True, at_edge=True), CellLine("систем,")]

    assert join_lines(lines, VOCAB) == "взаимодействия систем,"


def test_service_word_on_the_next_line_is_a_normal_wrap() -> None:
    lines = [CellLine("архитектору", single=True, at_edge=True), CellLine("и часть")]

    assert join_lines(lines, VOCAB) == "архитектору и часть"


def test_line_of_several_words_is_never_glued() -> None:
    lines = [CellLine("проверить идентифи", at_edge=True), CellLine("катор")]

    assert join_lines(lines, VOCAB) == "проверить идентифи катор"


def test_hyphen_before_a_digit_keeps_the_hyphen_and_drops_the_space() -> None:
    assert join_tail("--data 'cardId=demo-", "42&amount=1'") == "--data 'cardId=demo-42&amount=1'"


def test_vocabulary_skips_fragments_and_their_tails() -> None:
    cells = [
        (CellLine("Признак включен", at_edge=True), CellLine("ия")),
        (CellLine("Иденти", single=True), CellLine("фикатор")),
    ]

    vocab = vocabulary(["Хранить идентификатор карты."], cells)

    assert {"идентификатор", "признак", "включен", "ия"} <= vocab
    assert "иденти" not in vocab and "фикатор" not in vocab
