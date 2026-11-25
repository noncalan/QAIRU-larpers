"""Шаг 6: пример-свидетель, критик и попытка теста (заглушка каркаса).

Контракт: ``evidence(found, doc, llm, cfg) -> list[MergedFinding]``. До P1 возвращает вход без изменений
(docs/plan.md). Свидетеля и критика делает #39, попытку теста #40: они заполняют поля
``witness``, ``critic`` и ``test_draft`` у ``MergedFinding``.
"""

from __future__ import annotations

from collections.abc import Sequence

from validator.llm.base import LLMClient
from validator.models import MergedFinding, ParsedDocument, RunConfig


def evidence(
    found: Sequence[MergedFinding], doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> list[MergedFinding]:
    del doc, llm, cfg
    return list(found)
