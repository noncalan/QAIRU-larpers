"""Доля сверенных цитат у базовой линии: цитаты достаются из сырого текста ответа эвристикой.

Порт ``experiments/11-baseline-one-prompt/score_quotes.py`` на общее ядро агента: сверяет
``validator.text.locate_quote``, тот же, что в верификаторе агента (D-011: одинаковая сверка).

Эвристика извлечения (по порядку, найденное место маскируется и повторно не берётся):

1. Всё после заголовка «Предлагаемые формулировки ...» отбрасывается: там модель пишет собственные
   формулировки, а не цитирует документ.
2. Фрагменты в «ёлочках» «...» в пределах одной строки.
3. Строки-цитаты Markdown (начинаются с «>»), в которых нет «ёлочек»: вся строка без метки вида
   «Ф-2:» и без «>».
4. Фрагменты в обратных кавычках, если в них не меньше 12 символов или есть пробел.
5. Фрагменты в прямых или типографских двойных кавычках длиной от 15 символов вне обратных кавычек.

Цитата режется на сегменты по многоточию, « / » и « | » (так модель склеивает ячейки таблиц и
пропуски); сегменты короче 6 символов после нормализации отбрасываются. Вердикт: ``exact``, если все
сегменты найдены точно; ``fuzzy``, если все найдены, но хотя бы один нечётко; иначе ``not_found``.
Модель Haiku для переноса в JSON к базовой линии не применяется.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from validator.models import QuoteMatch
from validator.text import locate_quote, normalize

MIN_SEGMENT_CHARS = 6
MIN_BACKTICK_CHARS = 12

_GUILLEMETS = re.compile(r"«([^«»\n]+)»")
_BACKTICKS = re.compile(r"`([^`\n]+)`")
_DOUBLE_QUOTES = re.compile(r"[\"“„]([^\"“”„\n]{15,})[\"”“]")
_LABEL = re.compile(r"^[^:«»]{1,25}:\s*")
_SEGMENT_SPLIT = re.compile(r"…|\.\.\.|\s/\s|\s\|\s")
_PROPOSALS = re.compile(r"^#+\s*Предлагаемые формулировки.*$", re.MULTILINE)


class QuoteKind(StrEnum):
    GUILLEMET = "guillemet"
    BLOCKQUOTE = "blockquote"
    BACKTICK = "backtick"
    DOUBLE_QUOTE = "double_quote"


@dataclass(frozen=True)
class ExtractedQuote:
    kind: QuoteKind
    text: str


@dataclass
class QuoteStats:
    """Счётчики сверки цитат: всего, точно, нечётко, не найдено."""

    total: int = 0
    exact: int = 0
    fuzzy: int = 0
    not_found: int = 0

    def add(self, match: QuoteMatch) -> None:
        self.total += 1
        if match is QuoteMatch.EXACT:
            self.exact += 1
        elif match is QuoteMatch.FUZZY:
            self.fuzzy += 1
        else:
            self.not_found += 1

    def merge(self, other: QuoteStats) -> None:
        self.total += other.total
        self.exact += other.exact
        self.fuzzy += other.fuzzy
        self.not_found += other.not_found

    @property
    def share_exact(self) -> float:
        """Доля дословных цитат (после нормализации верификатора)."""
        return self.exact / self.total if self.total else 0.0

    @property
    def share_verified(self) -> float:
        """Доля сверенных цитат: найдены точно или нечётко."""
        return (self.exact + self.fuzzy) / self.total if self.total else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "total": self.total,
            "exact": self.exact,
            "fuzzy": self.fuzzy,
            "not_found": self.not_found,
            "share_exact": round(self.share_exact, 4),
            "share_verified": round(self.share_verified, 4),
        }


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def extract_quotes(text: str) -> list[ExtractedQuote]:
    """Цитаты из сырого ответа модели (эвристика исследования 11)."""
    cut = _PROPOSALS.search(text)
    if cut:
        text = text[: cut.start()]
    text = text.replace("**", "")
    quotes = [ExtractedQuote(QuoteKind.GUILLEMET, m.group(1)) for m in _GUILLEMETS.finditer(text)]
    work = _GUILLEMETS.sub(_blank, text)

    lines = work.split("\n")
    for line, original in zip(lines, text.split("\n"), strict=True):
        if line.lstrip().startswith(">") and "«" not in original:
            body = _LABEL.sub("", line.lstrip()[1:].strip())
            if body:
                quotes.append(ExtractedQuote(QuoteKind.BLOCKQUOTE, body))
    work = "\n".join(" " * len(line) if line.lstrip().startswith(">") else line for line in lines)

    for m in _BACKTICKS.finditer(work):
        inner = m.group(1).strip()
        if len(inner) >= MIN_BACKTICK_CHARS or " " in inner:
            quotes.append(ExtractedQuote(QuoteKind.BACKTICK, m.group(1)))
    work = _BACKTICKS.sub(_blank, work)

    quotes.extend(ExtractedQuote(QuoteKind.DOUBLE_QUOTE, m.group(1)) for m in _DOUBLE_QUOTES.finditer(work))
    return quotes


def check_quote(document: str, quote: str) -> QuoteMatch | None:
    """Вердикт по цитате или ``None``, если после нарезки не осталось содержательных сегментов."""
    segments = [s for s in _SEGMENT_SPLIT.split(quote) if len(normalize(s)[0].strip()) >= MIN_SEGMENT_CHARS]
    if not segments:
        return None
    matches = [locate_quote(document, segment).match for segment in segments]
    if QuoteMatch.NOT_FOUND in matches:
        return QuoteMatch.NOT_FOUND
    if QuoteMatch.FUZZY in matches:
        return QuoteMatch.FUZZY
    return QuoteMatch.EXACT


def score_quotes(document: str, answer: str) -> tuple[QuoteStats, dict[str, QuoteStats]]:
    """Счётчики по ответу целиком и в разрезе способа извлечения."""
    total = QuoteStats()
    by_kind: dict[str, QuoteStats] = {}
    for quote in extract_quotes(answer):
        match = check_quote(document, quote.text)
        if match is None:
            continue
        total.add(match)
        by_kind.setdefault(quote.kind.value, QuoteStats()).add(match)
    return total, by_kind
