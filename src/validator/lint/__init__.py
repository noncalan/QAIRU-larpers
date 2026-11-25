"""Шаг 3: линтер правил без модели.

Контракт: ``lint(doc, seg) -> list[CandidateFinding]``. Правила, словари (``lexicon.yaml``) и тесты
делает #28. Только общий язык ТЗ: слов проверяемого документа здесь быть не должно
(CLAUDE.md, правила 2 и 3; это проверяет ``tests/test_prompt_leak.py``).
"""

from __future__ import annotations

from validator.models import CandidateFinding, ParsedDocument, Segmentation


def lint(doc: ParsedDocument, seg: Segmentation) -> list[CandidateFinding]:
    """Заглушка каркаса: правил пока нет, срабатываний нет."""
    del doc, seg
    return []


__all__ = ["lint"]
