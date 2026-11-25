"""Эвристика цитат базовой линии (evals/quotes.py) и запрос базовой линии (evals/baseline.py)."""

from __future__ import annotations

from evals.baseline import MODEL, SYSTEM_PROMPT, USER_PROMPT, build_request
from evals.quotes import QuoteKind, check_quote, extract_quotes, score_quotes

from validator.models import Effort, QuoteMatch

DOC = "Система должна быстро отвечать пользователю.\n\nПароль хранится в открытом виде.\n"


def test_extracts_guillemets_blockquotes_and_backticks() -> None:
    answer = (
        "1. «Система должна быстро отвечать пользователю»: неизмеримо.\n"
        "> Ф-2: Пароль хранится в открытом виде.\n"
        "Поле `amount` и `Пароль хранится в открытом виде` в коде.\n"
    )
    kinds = [q.kind for q in extract_quotes(answer)]
    assert kinds == [QuoteKind.GUILLEMET, QuoteKind.BLOCKQUOTE, QuoteKind.BACKTICK]


def test_proposals_section_is_cut() -> None:
    answer = "«Система должна быстро отвечать»\n\n## Предлагаемые формулировки\n«Система отвечает за 2 с»\n"
    assert [q.text for q in extract_quotes(answer)] == ["Система должна быстро отвечать"]


def test_check_quote_verdicts() -> None:
    assert check_quote(DOC, "Система должна быстро отвечать пользователю") is QuoteMatch.EXACT
    assert check_quote(DOC, "Система должна … Пароль хранится") is QuoteMatch.EXACT
    assert check_quote(DOC, "Совсем другая фраза, которой нет") is QuoteMatch.NOT_FOUND
    assert check_quote(DOC, "ab / cd") is None  # сегменты короче порога


def test_score_quotes_counts() -> None:
    answer = "«Система должна быстро отвечать пользователю» и «Выдуманная цитата про слона»"
    stats, by_kind = score_quotes(DOC, answer)
    assert (stats.total, stats.exact, stats.not_found) == (2, 1, 1)
    assert stats.share_exact == 0.5
    assert by_kind[QuoteKind.GUILLEMET.value].total == 2


def test_baseline_request_is_frozen_config() -> None:
    req = build_request("ТЕКСТ", 2)
    assert (req.model, req.effort, req.call_id) == (MODEL, Effort.MEDIUM, "search.2")
    assert req.system == SYSTEM_PROMPT
    assert req.prompt == USER_PROMPT + "\n\n---\nДокумент:\n\nТЕКСТ"
    assert req.json_schema is None
