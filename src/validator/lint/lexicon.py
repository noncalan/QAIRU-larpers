"""Загрузка словарей ``lexicon.yaml`` и поиск слов в тексте.

Текст и словарь сравниваем в «свёрнутом» виде (``fold``): нижний регистр и «ё» → «е», длина строки
не меняется, поэтому смещения в свёрнутом тексте равны смещениям в исходном.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

LEXICON_PATH = Path(__file__).with_name("lexicon.yaml")
_KEYS = ("stem", "word", "phrase", "pattern")


def fold(text: str) -> str:
    """Нижний регистр и «ё» → «е» без смены длины строки (у ``str.lower`` бывают символы длиннее одного)."""
    return "".join(c if len(low := c.lower()) != 1 else low for c in text).replace("ё", "е")


@dataclass(frozen=True)
class Term:
    """Запись словаря: как искать и что сказать человеку, если нашли."""

    key: str
    regex: re.Pattern[str]
    why: str


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    term: Term
    text: str


def _phrase_regex(phrase: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(phrase):
        if ch == " ":
            out.append(r"\s*" if i > 0 and phrase[i - 1] == "." else r"\s+")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _compile(entry: dict[str, Any]) -> Term:
    keys = [k for k in _KEYS if k in entry]
    if len(keys) != 1 or not entry.get("why"):
        raise ValueError(f"Запись словаря должна иметь ровно один из ключей {_KEYS} и поле why: {entry}")
    kind = keys[0]
    # Регулярные выражения не сворачиваем: ``fold`` испортил бы ``\S``, ``\D``; пишем их строчными и с «е».
    value = str(entry[kind]).strip() if kind == "pattern" else fold(str(entry[kind]).strip())
    if kind == "stem":
        body = re.escape(value) + r"\w*"
    elif kind == "word":
        body = re.escape(value)
    elif kind == "phrase":
        body = _phrase_regex(value)
    else:
        body = f"(?:{value})"
    # Границы слова ставим, если запись сама не отключила их (``boundary: none``: «???», «[указать …]»).
    bounded = entry.get("boundary", "word") != "none"
    lead, trail = (r"(?<!\w)", r"(?!\w)") if bounded else ("", "")
    return Term(key=value, regex=re.compile(f"{lead}{body}{trail}"), why=str(entry["why"]).strip())


@cache
def load_lexicon() -> dict[str, tuple[Term, ...]]:
    """Все разделы словаря, скомпилированные один раз на процесс."""
    raw = yaml.safe_load(LEXICON_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{LEXICON_PATH.name}: ожидался словарь разделов")
    return {name: tuple(_compile(entry) for entry in entries) for name, entries in raw.items()}


def find_terms(folded_text: str, section: str) -> list[Match]:
    """Вхождения слов раздела в свёрнутый текст по порядку; из пересекающихся остаётся самое длинное."""
    found: list[Match] = []
    for term in load_lexicon()[section]:
        found.extend(Match(m.start(), m.end(), term, m.group(0)) for m in term.regex.finditer(folded_text))
    found.sort(key=lambda m: (m.start, -(m.end - m.start)))
    result: list[Match] = []
    for match in found:
        if result and match.start < result[-1].end:
            continue
        result.append(match)
    return result
