"""Срабатывание правила до превращения в ``CandidateFinding``."""

from __future__ import annotations

from dataclasses import dataclass

from validator.lint.rules import Rule


@dataclass(frozen=True)
class Hit:
    """Одно срабатывание: правило, куда указывать и что сказать человеку.

    ``spans`` это диапазоны ``doc.text``, из которых код берёт цитаты (уже суженные до окна), по одному
    на каждое место. ``block_id`` нужен, когда цитата короче блока: движок сам находит блоки по диапазонам.
    """

    rule: Rule
    spans: tuple[tuple[int, int], ...]
    title: str
    explanation: str
    suggestion: str | None = None

    @property
    def order(self) -> tuple[int, str]:
        return self.spans[0][0], self.rule.id
