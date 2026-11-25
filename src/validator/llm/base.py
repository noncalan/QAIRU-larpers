"""Протокол клиента модели и общий разбор ответа ``claude -p --output-format json``.

У клиента одна операция ``call``; реализации взаимозаменяемы (D-008): подписка ``claude -p``,
SDK ``anthropic`` (#32), записанные ответы (replay, #21) и фикстуры тестов (``FakeLLM``).
Типы запроса, ответа и записи лога лежат в ``validator.models`` и реэкспортируются отсюда.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from validator.errors import (
    LLM_BAD_RESPONSE,
    LLM_CALL_FAILED,
    LLM_LIMIT,
    LLM_UNAVAILABLE,
    LLMError,
)
from validator.models import Backend, CallRecord, LLMRequest, LLMResponse, TokenUsage


class LLMClient(Protocol):
    """Клиент модели. ``call`` бросает только ``LLMError``; всё остальное это ошибка в коде."""

    @property
    def backend(self) -> Backend: ...

    def call(self, req: LLMRequest) -> LLMResponse: ...


_FENCED_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)
# Коды HTTP только отдельным числом: «14290 tokens» не лимит и не ошибка входа.
_LIMIT_MARKERS = re.compile(r"usage limit|rate[ _]limit|limit reached|overloaded|\b(?:429|529)\b")
_AUTH_MARKERS = re.compile(r"not logged in|/login|authentication|invalid api key|unauthorized|\b401\b")


def _classify(message: str) -> tuple[str, bool, str]:
    """Код диагностики, можно ли повторить и подсказку человеку по тексту ошибки."""
    low = message.lower()
    if _LIMIT_MARKERS.search(low):
        return LLM_LIMIT, True, "Подождите восстановления лимита подписки или задайте ANTHROPIC_API_KEY"
    if _AUTH_MARKERS.search(low):
        return (
            LLM_UNAVAILABLE,
            False,
            "Выполните вход: claude, затем /login, или задайте ANTHROPIC_API_KEY в .env",
        )
    return LLM_CALL_FAILED, False, "Повторите прогон; подробности в report.log"


def _usage(payload: dict[str, Any]) -> TokenUsage:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return TokenUsage()
    fields = TokenUsage.model_fields
    return TokenUsage(**{k: int(v) for k, v in raw.items() if k in fields and isinstance(v, int)})


def _structured(payload: dict[str, Any], text: str) -> Any:
    """structured_output, а если его нет, JSON из текста ответа (в том числе в ```json```)."""
    data = payload.get("structured_output")
    if data is not None:
        return data
    m = _FENCED_JSON.match(text)
    try:
        return json.loads(m.group(1) if m else text)
    except ValueError:
        return None


def parse_cli_payload(
    payload: object, req: LLMRequest, backend: Backend, *, cached: bool = False
) -> LLMResponse:
    """Разбор JSON, который печатает ``claude -p --output-format json`` (и который лежит в фикстурах)."""
    if not isinstance(payload, dict):
        raise LLMError("Ответ модели не JSON-объект", code=LLM_BAD_RESPONSE, location=req.call_id)
    text = payload.get("result")
    text = text if isinstance(text, str) else ""
    if payload.get("is_error") or payload.get("subtype", "success") != "success":
        detail = text or str(payload.get("subtype") or "ошибка без текста")
        code, retryable, hint = _classify(detail)
        raise LLMError(
            f"Модель вернула ошибку: {detail[:300]}",
            code=code,
            hint=hint,
            location=req.call_id,
            retryable=retryable,
        )
    structured = None
    if req.json_schema is not None:
        structured = _structured(payload, text)
        if structured is None:
            raise LLMError(
                "Ответ модели не соответствует JSON-схеме",
                code=LLM_BAD_RESPONSE,
                hint="Повторите прогон; если повторяется, сообщите команде",
                location=req.call_id,
                retryable=True,
            )
    model_usage = payload.get("modelUsage")
    model = next(iter(model_usage), None) if isinstance(model_usage, dict) and model_usage else None
    cost = payload.get("total_cost_usd")
    duration = payload.get("duration_ms")
    return LLMResponse(
        call_id=req.call_id,
        text=text,
        structured=structured,
        model=model,
        usage=_usage(payload),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        duration_ms=int(duration) if isinstance(duration, int | float) else None,
        backend=backend,
        cached=cached,
    )


__all__ = ["CallRecord", "LLMClient", "LLMRequest", "LLMResponse", "TokenUsage", "parse_cli_payload"]
