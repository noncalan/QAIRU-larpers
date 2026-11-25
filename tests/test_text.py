"""Сверка цитат с документом: ``normalize`` с картой смещений и ``locate_quote``."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from rapidfuzz import fuzz

from validator.models import ParsedDocument, QuoteMatch
from validator.text import MIN_FUZZY_CHARS, line_of, locate_quote, normalize, snap_to_words, stable_key

DOC = (
    "Система сохраняет настройку пользователя.\n"
    "Счётчик обновляется после «подтверждения» операции.\n"
    "Сумма от 1 000 до 5 000 ₸ включительно.\n"
    "Модуль отправки сообщений прове-\n"
    "ряет статус доставки.\n"
    "**Важно.** Клиент → сервер → база."
)


def test_exact_quote_returns_document_slice() -> None:
    quote = locate_quote(DOC, "Система сохраняет настройку пользователя.")

    assert quote.match is QuoteMatch.EXACT
    assert quote.score == 100.0
    assert quote.verified
    assert quote.located == "Система сохраняет настройку пользователя."
    assert quote.anchor is not None
    assert DOC[quote.anchor.char_start : quote.anchor.char_end] == quote.located
    assert quote.anchor.line_start == 1


def test_yo_and_ye_are_equal() -> None:
    quote = locate_quote(DOC, "Счетчик обновляется после")

    assert quote.match is QuoteMatch.EXACT
    assert quote.located == "Счётчик обновляется после"


@pytest.mark.parametrize("variant", ['"подтверждения"', "„подтверждения“", "'подтверждения'"])
def test_quote_marks_are_unified(variant: str) -> None:
    quote = locate_quote(DOC, f"после {variant} операции")

    assert quote.match is QuoteMatch.EXACT
    assert quote.located == "после «подтверждения» операции"


def test_line_break_inside_quote_matches_space() -> None:
    quote = locate_quote(DOC, "операции. Сумма от 1 000")

    assert quote.match is QuoteMatch.EXACT
    assert quote.anchor is not None
    assert (quote.anchor.line_start, quote.anchor.line_end) == (2, 3)


def test_hyphenation_at_line_end_is_joined() -> None:
    quote = locate_quote(DOC, "Модуль отправки сообщений проверяет статус доставки.")

    assert quote.match is QuoteMatch.EXACT
    assert quote.located == "Модуль отправки сообщений прове-\nряет статус доставки."


@pytest.mark.parametrize("variant", ["от 1 000 до 5 000 ₸", "от 1000 до 5000 ₸", "от 1 000 до 5 000 ₸"])
def test_non_breaking_and_thousand_spaces_are_ignored(variant: str) -> None:
    quote = locate_quote(DOC, variant)

    assert quote.match is QuoteMatch.EXACT
    assert quote.located == "от 1 000 до 5 000 ₸"


def test_markdown_emphasis_and_arrows_are_ignored() -> None:
    quote = locate_quote(DOC, "Важно. Клиент -> сервер -> база.")

    assert quote.match is QuoteMatch.EXACT
    assert quote.located is not None
    assert quote.located.startswith("Важно.**")


def test_space_before_punctuation_is_ignored() -> None:
    doc = "Бизнес -процесс описан ( кратко ) , без схемы ."

    quote = locate_quote(doc, "Бизнес-процесс описан (кратко), без схемы.")

    assert quote.match is QuoteMatch.EXACT


def test_small_typo_gives_fuzzy_match() -> None:
    quote = locate_quote(DOC, "Система сохраняет настройки пользователя.")

    assert quote.match is QuoteMatch.FUZZY
    assert 90.0 <= quote.score < 100.0
    assert quote.anchor is not None
    assert quote.located is not None
    assert "настройку пользователя" in quote.located


def test_invented_quote_is_not_found() -> None:
    quote = locate_quote(DOC, "Платёж проводится мгновенно в любой валюте мира.")

    assert quote.match is QuoteMatch.NOT_FOUND
    assert not quote.verified
    assert quote.anchor is None
    assert quote.located is None


def test_short_quote_is_never_fuzzy() -> None:
    short = "Систма"
    assert len(short) < MIN_FUZZY_CHARS

    assert locate_quote(DOC, short).match is QuoteMatch.NOT_FOUND


@pytest.mark.parametrize("empty", ["", "   ", "**"])
def test_empty_quote_is_not_found(empty: str) -> None:
    assert locate_quote(DOC, empty).match is QuoteMatch.NOT_FOUND


def test_within_limits_search_to_given_blocks(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md("# Раздел\n\nПервый абзац про сохранение.\n\nВторой абзац про удаление записи.\n")
    first, second = (b.id for b in doc.blocks[1:3])

    found = locate_quote(doc, "удаление записи", within=[second])
    missed = locate_quote(doc, "удаление записи", within=[first])
    unknown = locate_quote(doc, "удаление записи", within=["B999"])

    assert found.match is QuoteMatch.EXACT
    assert found.anchor is not None
    assert found.anchor.block_ids == [second]
    assert missed.match is QuoteMatch.NOT_FOUND
    assert unknown.match is QuoteMatch.NOT_FOUND


def test_within_accepts_sentence_ids(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md("Первое предложение. Второе предложение про отчёт.\n")
    block = doc.blocks[0]

    quote = locate_quote(doc, "про отчёт", within=[block.sentences[1].id])

    assert quote.match is QuoteMatch.EXACT
    assert quote.anchor is not None
    assert quote.anchor.sentence_ids == [block.sentences[1].id]


def test_within_requires_parsed_document() -> None:
    with pytest.raises(TypeError):
        locate_quote(DOC, "Система", within=["B001"])


def test_offset_map_survives_nfkc_expansion() -> None:
    text = "ﬁnal ½ step"  # лигатура fi и дробь раскрываются NFKC в несколько символов

    norm, index = normalize(text)

    assert norm.startswith("final")
    assert len(index) == len(norm)
    assert index[0] == index[1] == 0
    assert all(0 <= i < len(text) for i in index)
    assert index == sorted(index)


def test_locate_returns_original_text_after_expansion() -> None:
    text = "Шаг один. ﬁnal check of the release candidate."

    quote = locate_quote(text, "final check of the release")

    assert quote.match is QuoteMatch.EXACT
    assert quote.located == "ﬁnal check of the release"


def test_stable_key_ignores_case_spacing_and_yo() -> None:
    assert stable_key("Счётчик  обновляется.") == stable_key("счетчик обновляется.")
    assert stable_key("Счётчик", "IND-UNAMB") != stable_key("Счётчик", "SET-CONS")


def test_line_of_counts_from_one() -> None:
    text = "a\nb\nc"

    assert [line_of(text, i) for i in (0, 2, 4)] == [1, 2, 3]


BOOKS = "Правила выдачи. Читатель может держать на руках не более пяти книг одновременно. Срок две недели."


@pytest.mark.parametrize(
    "quote",
    [
        "Читатель может держать на руках не более пяти книг одновремено",  # опечатка: конец слова откушен
        "читатель может держать на руках не более 5 книг одновременно.",  # другое число
        "Читатель может держать на руках не более пяти книжек одновременно",  # пробел по краю
    ],
)
def test_fuzzy_match_is_snapped_to_whole_words(quote: str) -> None:
    found = locate_quote(BOOKS, quote)

    assert found.match is QuoteMatch.FUZZY
    assert found.located is not None and found.anchor is not None
    assert found.located.startswith("Читатель")
    assert found.located.rstrip(".").endswith("одновременно")
    assert BOOKS[found.anchor.char_start : found.anchor.char_end] == found.located


def test_snapping_keeps_fuzzy_score() -> None:
    quote = "Читатель может держать на руках не более пяти книг одновремено"
    norm_doc, norm_quote = normalize(BOOKS)[0], normalize(quote)[0]

    found = locate_quote(BOOKS, quote)

    assert found.score == round(fuzz.partial_ratio_alignment(norm_quote, norm_doc).score, 1)


def test_exact_match_is_not_changed_by_snapping() -> None:
    found = locate_quote(BOOKS, "держать на руках не более пяти книг")

    assert found.match is QuoteMatch.EXACT
    assert found.located == "держать на руках не более пяти книг"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (3, 9, "Читатель"),  # «татель» → целое слово
        (2, 6, "Читатель"),  # середина слова
        (0, 11, "Читатель может"),  # пробел и «м» по краям
        (9, 13, "может"),
        (0, 1, " "),  # от фрагмента остался бы только пробел: границы не трогаем
    ],
)
def test_snap_to_words(start: int, end: int, expected: str) -> None:
    text = " Читатель может, user_id."

    s, e = snap_to_words(text, start, end)

    assert text[s:e] == expected


def test_snap_to_words_keeps_identifiers_and_stops_at_punctuation() -> None:
    text = "Поле user_id, затем дата."

    s, e = snap_to_words(text, 8, 11)

    assert text[s:e] == "user_id"
