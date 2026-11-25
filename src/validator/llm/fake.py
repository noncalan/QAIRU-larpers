"""Фейковый клиент модели: отвечает из фикстур, в сеть не ходит.

Фикстура это JSON в формате ``claude -p --output-format json`` (поля ``result``, ``structured_output``,
``usage``, ``modelUsage``, ``total_cost_usd``) и лежит в ``tests/fixtures/llm/<шаг>/<номер>.json``,
где шаг это ``LLMStep`` (``search``, ``segment``, …), а номер это ``LLMRequest.call_index`` с 1.
Живой ответ ``claude -p`` можно положить в фикстуру как есть.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from validator.errors import LLM_NO_FIXTURE, LLMError
from validator.llm.base import parse_cli_payload
from validator.models import Backend, LLMRequest, LLMResponse, LLMStep

FIXTURES_ENV = "VALIDATOR_FAKE_FIXTURES"


def default_fixtures_dir() -> Path:
    """``$VALIDATOR_FAKE_FIXTURES`` или ``tests/fixtures/llm`` в корне репозитория (src-раскладка)."""
    override = os.environ.get(FIXTURES_ENV)
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "llm"


class FakeLLM:
    """Ответы из папки фикстур или из словаря ``{(шаг, номер): payload}`` для юнит-тестов."""

    backend = Backend.FAKE

    def __init__(
        self,
        root: Path | None = None,
        responses: Mapping[tuple[LLMStep, int], Mapping[str, Any]] | None = None,
    ) -> None:
        self.root = root if root is not None else default_fixtures_dir()
        self.responses = dict(responses or {})
        self.requests: list[LLMRequest] = []

    def _payload(self, req: LLMRequest) -> object:
        key = (req.step, req.call_index)
        if key in self.responses:
            return dict(self.responses[key])
        path = self.root / req.step.value / f"{req.call_index}.json"
        if not path.is_file():
            raise LLMError(
                f"Нет записанного ответа для шага {req.step.value}, вызов {req.call_index}",
                code=LLM_NO_FIXTURE,
                hint=f"Положите ответ в {path}",
                location=req.call_id,
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def call(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        return parse_cli_payload(self._payload(req), req, self.backend)
