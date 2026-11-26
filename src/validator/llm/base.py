"""Протокол клиента модели и общий разбор ответа ``claude -p --output-format json``.

У клиента одна операция ``call``; реализации взаимозаменяемы (D-008): подписка ``claude -p``,
SDK ``anthropic`` (#32), записанные ответы (replay, #21) и фикстуры тестов (``FakeLLM``).
Типы запроса, ответа и записи лога лежат в ``validator.models`` и реэкспортируются отсюда.
"""

from __future__ import annotations

import json
import re
from typing import Any, NamedTuple, Protocol

from validator.errors import (
    LLM_BAD_RESPONSE,
    LLM_BUDGET,
    LLM_CALL_FAILED,
    LLM_LIMIT,
    LLM_OVERLOADED,
    LLM_REFUSED,
    LLM_UNAVAILABLE,
    LLMError,
)
from validator.llm.schema import schema_errors
from validator.models import Backend, CallRecord, LLMRequest, LLMResponse, TokenUsage


class LLMClient(Protocol):
    """Клиент модели. ``call`` бросает только ``LLMError``; всё остальное это ошибка в коде."""

    @property
    def backend(self) -> Backend: ...

    def call(self, req: LLMRequest) -> LLMResponse: ...


_FENCED_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.IGNORECASE | re.DOTALL)

# Тексты ошибок claude -p (JSON на stdout, поле result) и SDK. HTTP-коды ищем только как статус ответа
# или после «API Error»: «14290 tokens» не лимит и не ошибка входа.
_REFUSAL = re.compile(r"usage policy|unable to respond to this request|declined to (?:answer|respond)")
_AUTH = re.compile(
    r"not logged in|/login|authentication|invalid api key|unauthori[sz]ed|oauth token|token (?:has )?expired"
)
_USAGE_LIMIT = re.compile(
    r"usage limit|(?<!rate )limit reached|hit your [\w -]{0,20}limit|limit will reset|"
    r"\bresets? at\b|\bresets? \d{1,2}(?::\d\d)?\s*(?:am|pm)|credit balance is too low|"
    r"out of (?:extra )?usage"
)
_SUBSCRIPTION_LIMIT = re.compile(
    r"hit your [\w -]{0,20}limit|limit will reset|\bresets? (?:at\b|\d{1,2}(?::\d\d)?\s*(?:am|pm))|usage limit|"
    r"credit balance is too low|out of (?:extra )?usage"
)
"""Лимит подписки или баланса: ``claude -p`` отдаёт его со статусом 429, но повтор не поможет."""
_TRANSIENT = re.compile(
    r"overloaded|rate[ _-]?limit|too many requests|api error:?\s*(?:429|5\d\d)\b|connection error|"
    r"request timed out|econnreset|socket hang up|internal server error|service unavailable|bad gateway"
)
_TOO_LONG = re.compile(r"prompt is too long|context (?:window|length)|too many (?:input )?tokens")
_TRANSIENT_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
_AUTH_STATUS = frozenset({401, 403})


class _Failure(NamedTuple):
    code: str
    retryable: bool
    hint: str


_HINT_RETRY = "Повторите прогон; подробности в report.log"
_FAILURES = {
    "refused": _Failure(
        LLM_REFUSED, False, "Модель отказалась отвечать: проверьте документ, сообщите команде"
    ),
    "auth": _Failure(
        LLM_UNAVAILABLE, False, "Выполните вход: claude, затем /login, или задайте ANTHROPIC_API_KEY в .env"
    ),
    "limit": _Failure(
        LLM_LIMIT,
        False,
        "Лимит подписки исчерпан: дождитесь его восстановления, пересоберите отчёт из кеша (--cache use) "
        "или задайте ANTHROPIC_API_KEY",
    ),
    "transient": _Failure(
        LLM_OVERLOADED, True, "Сервис модели перегружен: агент повторил вызов; " + _HINT_RETRY
    ),
    "too_long": _Failure(
        LLM_CALL_FAILED, False, "Документ слишком большой для одного запроса: разделите его на части"
    ),
    "budget": _Failure(LLM_BUDGET, False, "Увеличьте --max-budget-usd или запустите с профилем quick"),
    "schema": _Failure(LLM_BAD_RESPONSE, True, _HINT_RETRY),
    "execution": _Failure(LLM_CALL_FAILED, True, _HINT_RETRY),
    "other": _Failure(LLM_CALL_FAILED, False, _HINT_RETRY),
}
_SUBTYPES = {
    "error_max_budget_usd": "budget",
    "error_max_structured_output_retries": "schema",
    "error_during_execution": "execution",
}


def classify_error(message: str, *, status: int | None = None, subtype: str | None = None) -> _Failure:
    """Код диагностики, можно ли повторить и подсказка человеку по ошибке модели."""
    low = message.lower()
    if _REFUSAL.search(low):
        kind = "refused"
    elif _SUBSCRIPTION_LIMIT.search(low):
        kind = "limit"
    elif status in _TRANSIENT_STATUS:
        kind = "transient"
    elif status in _AUTH_STATUS or _AUTH.search(low):
        kind = "auth"
    elif _TRANSIENT.search(low):
        kind = "transient"
    elif _USAGE_LIMIT.search(low):
        kind = "limit"
    elif _TOO_LONG.search(low):
        kind = "too_long"
    else:
        kind = _SUBTYPES.get(subtype or "", "other")
    return _FAILURES[kind]


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


def _actual_model(payload: dict[str, Any]) -> str | None:
    """Модель, которая ответила: из ``modelUsage`` та, что написала больше всего токенов."""
    usage = payload.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        model = payload.get("model")
        return model if isinstance(model, str) else None

    def written(item: tuple[str, Any]) -> int:
        out = item[1].get("outputTokens") if isinstance(item[1], dict) else None
        return out if isinstance(out, int) else 0

    return str(max(usage.items(), key=written)[0])


def _raise_error_payload(payload: dict[str, Any], text: str, req: LLMRequest) -> None:
    subtype = payload.get("subtype")
    stop = payload.get("stop_reason")
    failed = payload.get("is_error") or subtype not in (None, "success") or stop == "refusal"
    if not failed:
        return
    detail = text or (
        "модель отказалась отвечать (stop_reason: refusal)"
        if stop == "refusal"
        else str(subtype or stop or "")
    )
    detail = detail if detail and detail != "success" else "ошибка без текста"
    status = payload.get("api_error_status")
    failure = classify_error(
        detail if stop != "refusal" else f"usage policy: {detail}",
        status=status if isinstance(status, int) else None,
        subtype=subtype if isinstance(subtype, str) else None,
    )
    raise LLMError(
        f"Модель вернула ошибку: {detail[:300]}",
        code=failure.code,
        hint=failure.hint,
        location=req.call_id,
        retryable=failure.retryable,
    )


def _bad_response(message: str, req: LLMRequest) -> LLMError:
    return LLMError(
        message,
        code=LLM_BAD_RESPONSE,
        hint="Повторите прогон; если повторяется, сообщите команде",
        location=req.call_id,
        retryable=True,
    )


def _checked_structured(payload: dict[str, Any], text: str, req: LLMRequest) -> Any:
    if req.json_schema is None:
        if not text.strip():
            raise _bad_response("Модель вернула пустой ответ", req)
        return None
    structured = _structured(payload, text)
    if structured is None:
        raise _bad_response("Ответ модели не соответствует JSON-схеме: это не JSON", req)
    errors = schema_errors(structured, req.json_schema)
    if errors:
        raise _bad_response(f"Ответ модели не соответствует JSON-схеме: {'; '.join(errors)}", req)
    return structured


def parse_cli_payload(
    payload: object, req: LLMRequest, backend: Backend, *, cached: bool = False
) -> LLMResponse:
    """Разбор JSON, который печатает ``claude -p --output-format json`` (и который лежит в фикстурах).

    Ошибка в ответе даёт ``LLMError`` с кодом по тексту и статусу; ответ проверяется по схеме запроса.
    """
    if not isinstance(payload, dict):
        raise LLMError("Ответ модели не JSON-объект", code=LLM_BAD_RESPONSE, location=req.call_id)
    text = payload.get("result")
    text = text if isinstance(text, str) else ""
    _raise_error_payload(payload, text, req)
    structured = _checked_structured(payload, text, req)
    cost = payload.get("total_cost_usd")
    duration = payload.get("duration_ms")
    return LLMResponse(
        call_id=req.call_id,
        text=text,
        structured=structured,
        model=_actual_model(payload),
        usage=_usage(payload),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        duration_ms=int(duration) if isinstance(duration, int | float) else None,
        backend=backend,
        cached=cached,
        raw=payload,
    )


def to_payload(resp: LLMResponse) -> dict[str, Any]:
    """Ответ в формате ``claude -p --output-format json``: сам ответ бэкенда или собранный из полей."""
    if resp.raw is not None:
        return resp.raw
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": resp.text,
        "usage": resp.usage.model_dump(),
        "total_cost_usd": resp.cost_usd,
        "duration_ms": resp.duration_ms,
    }
    if resp.structured is not None:
        payload["structured_output"] = resp.structured
    if resp.model:
        payload["model"] = resp.model
        payload["modelUsage"] = {
            resp.model: {
                "inputTokens": resp.usage.input_tokens,
                "outputTokens": resp.usage.output_tokens,
                "cacheReadInputTokens": resp.usage.cache_read_input_tokens,
                "cacheCreationInputTokens": resp.usage.cache_creation_input_tokens,
            }
        }
    return payload


__all__ = [
    "CallRecord",
    "LLMClient",
    "LLMRequest",
    "LLMResponse",
    "TokenUsage",
    "classify_error",
    "parse_cli_payload",
    "to_payload",
]
