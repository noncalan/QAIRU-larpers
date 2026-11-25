"""Запрос базовой линии (evals/baseline.py) не меняется: промпт исследования 11, Opus 5.5 medium."""

from __future__ import annotations

from evals.baseline import MODEL, SYSTEM_PROMPT, USER_PROMPT, build_request
from validator.models import Effort


def test_baseline_request_is_frozen_config() -> None:
    req = build_request("ТЕКСТ", 2)
    assert (req.model, req.effort, req.call_id) == (MODEL, Effort.MEDIUM, "search.2")
    assert req.system == SYSTEM_PROMPT
    assert req.prompt == USER_PROMPT + "\n\n---\nДокумент:\n\nТЕКСТ"
    assert req.json_schema is None
