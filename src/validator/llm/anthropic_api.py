"""Клиент модели через Claude API: официальный SDK ``anthropic`` (D-006, #32).

Тот же протокол ``LLMClient``, что у подписки ``claude -p``: шаги не знают, каким путём идёт вызов.
Повтор после паузы, кеш, лог вызовов и лимит цены делает ``calllog.RecordingClient`` поверх этого клиента.

Как устроен вызов (имена сверены со справкой SDK в скилле claude-api, Claude Code 2.1.332, и с
установленным ``anthropic`` 2.14):

- всегда потоком (``client.beta.messages.stream`` и ``get_final_message``): проход поиска на xhigh
  пишет десятки тысяч токенов, без потока SDK отказывается от большого ``max_tokens``, а соединение
  рвётся по таймауту; потоку лимит в 128 тыс. токенов вывода не мешает;
- модель и effort из ``LLMRequest`` (а там из ``STEP_MODELS``, D-010); ``thinking`` не передаём:
  у Opus 5.5, Sonnet 5.5 и Haiku 5.5 без него включено адаптивное мышление, глубину задаёт effort;
- схема шага уходит в ``output_config.format`` (структурированный ответ), предварительно через
  ``anthropic.transform_schema``: API не принимает ``pattern``, ``minLength`` и т. п., SDK переносит их
  в описание поля. Ответ всё равно проверяем по исходной схеме в ``base.parse_cli_payload``;
- отказ модели (``stop_reason: refusal``) включает запасную модель на стороне сервера
  (``fallbacks: "default"``, бета ``server-side-fallback-2026-07-01``): классификаторы безопасности
  Opus 5.5 иногда срабатывают на безобидные требования к защите данных. Если отказала и запасная,
  это ``LLM-013``. Какая модель ответила, видно в ``model`` ответа и в паспорте;
- ошибки SDK переводим в ``LLMError`` по типу исключения и полю ``error.type``: 401/403 это ключ
  (``LLM-001``), лимит расходов и пустой баланс это ``LLM-002``, 429/5xx/529 и обрыв сети это
  перегрузка (``LLM-004``, можно повторить), таймаут ``LLM-012``. SDK сам повторяет 429 и 5xx
  (``max_retries``) с паузой из ``retry-after``;
- ответ приводим к формату ``claude -p --output-format json`` (``result``, ``usage``, ``model``),
  поэтому кеш, replay и расчёт цены по тарифу работают с ним так же, как с подпиской.

Кеширование префикса (``cache_control``) не включаем: по замеру исследования 21 вывод даёт 94–97 %
цены прогона, а проходы поиска идут параллельно и кеш друг другу не отдают; запись в кеш стоит 1,25×
входа. Ключ берём только из окружения (``ANTHROPIC_API_KEY``, его подхватывает ``.env``), передаём SDK
явно и нигде не печатаем.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

import anthropic
import httpx2
from anthropic.types.beta import BetaFallbackBlock

from validator.errors import (
    LLM_BAD_RESPONSE,
    LLM_CALL_FAILED,
    LLM_LIMIT,
    LLM_OVERLOADED,
    LLM_REFUSED,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
    LLMError,
)
from validator.llm.base import classify_error, parse_cli_payload
from validator.models import Backend, LLMRequest, LLMResponse, TokenUsage

if TYPE_CHECKING:
    from anthropic.types.beta import BetaMessage

logger = logging.getLogger("validator")

API_KEY_ENV = "ANTHROPIC_API_KEY"
MAX_OUTPUT_TOKENS = 128_000
"""Предел вывода Opus 5.5, Sonnet 5.5 и Haiku 5.5 (справка SDK): мышление на xhigh тоже идёт в вывод."""

SDK_MAX_RETRIES = 2
"""Повторы самого SDK на 408/409/429/5xx и обрыве соединения, с паузой из ``retry-after``."""

FALLBACK_BETA = "server-side-fallback-2026-07-01"

_HINT_KEY = (
    "Проверьте ANTHROPIC_API_KEY в .env (ключ: https://platform.claude.com/ → API keys) "
    "или запустите через подписку: --backend cli"
)
_HINT_LIMIT = (
    "На ключе кончились деньги или достигнут лимит расходов: пополните баланс в Claude Console, "
    "запустите через подписку (--backend cli) или пересоберите отчёт из кеша (--cache use)"
)
_HINT_RETRY = "Повторите прогон; подробности в report.log"
_HINT_MODEL = "Модель недоступна этому ключу: проверьте доступ в Claude Console или запустите --backend cli"
_HINT_TRUNCATED = "Ответ не уместился в предел вывода модели: возьмите профиль quick или разделите документ"

_LIMIT_CODES = frozenset({"enforced_spend_limit_reached"})
_LIMIT_TYPES = frozenset({"billing_error"})
_TRANSIENT_TYPES = frozenset({"overloaded_error", "rate_limit_error", "api_error", "timeout_error"})


def api_key_from_env() -> str | None:
    """Ключ из окружения (``.env`` уже загружен ``config.load_config``) или None. Пустая строка это None."""
    key = os.environ.get(API_KEY_ENV, "").strip()
    return key or None


def _error(
    message: str,
    code: str,
    req: LLMRequest,
    *,
    hint: str,
    retryable: bool,
    spent: LLMResponse | None = None,
) -> LLMError:
    return LLMError(message, code=code, hint=hint, location=req.call_id, retryable=retryable, spent=spent)


def _status_error(exc: anthropic.APIStatusError, req: LLMRequest) -> LLMError:
    """Ошибка ответа API (в том числе событие ``error`` посреди потока, у него статус 200)."""
    detail = (exc.api_message or exc.message or "")[:300]
    where = f"HTTP {exc.status_code}" + (f", {exc.type}" if exc.type else "")
    message = f"Claude API вернул ошибку ({where}): {detail}"
    if exc.request_id:
        message += f" [request-id {exc.request_id}]"
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return _error(message, LLM_UNAVAILABLE, req, hint=_HINT_KEY, retryable=False)
    if (
        exc.error_code in _LIMIT_CODES
        or exc.type in _LIMIT_TYPES
        or isinstance(exc, anthropic.PaymentRequiredError)
    ):
        return _error(message, LLM_LIMIT, req, hint=_HINT_LIMIT, retryable=False)
    if isinstance(exc, anthropic.NotFoundError):
        return _error(message, LLM_CALL_FAILED, req, hint=_HINT_MODEL, retryable=False)
    if exc.type in _TRANSIENT_TYPES:
        failure = classify_error("overloaded")
        return _error(message, failure.code, req, hint=failure.hint, retryable=True)
    failure = classify_error(detail, status=exc.status_code)
    hint = _HINT_LIMIT if failure.code == LLM_LIMIT else failure.hint
    if failure.code == LLM_UNAVAILABLE:
        hint = _HINT_KEY
    return _error(message, failure.code, req, hint=hint, retryable=failure.retryable)


def _connection_error(exc: Exception, req: LLMRequest) -> LLMError:
    """Сеть: соединение не открылось или оборвалось посреди потока ответа."""
    failure = classify_error("connection error")
    text = str(exc) or type(exc).__name__
    return _error(
        f"Нет связи с Claude API: {text[:300]}", failure.code, req, hint=failure.hint, retryable=True
    )


def _text(blocks: Iterable[Any]) -> str:
    """Текст ответа: все блоки ``text`` по порядку. Блоки мышления и метки ``fallback`` пропускаем.

    После отказа посреди потока запасная модель продолжает частичный текст (справка SDK, раздел про
    отказы): текст до метки и после неё это один ответ.
    """
    return "".join(block.text for block in blocks if getattr(block, "type", None) == "text")


_ATTEMPT_TYPES = frozenset({"message", "fallback_message"})


def _model_usage(message: BetaMessage) -> dict[str, dict[str, int]] | None:
    """Токены по моделям, если ответила запасная модель: каждую попытку считаем по тарифу её модели.

    У попытки без поля ``model`` модель берём из метки ``fallback`` (``from``): ``message.model`` после
    отказа это уже запасная модель, а отказавшая попытка шла на исходной.
    """
    iterations = [it for it in message.usage.iterations or [] if getattr(it, "type", None) in _ATTEMPT_TYPES]
    if not any(it.type == "fallback_message" for it in iterations):
        return None
    refused = next(
        (block.from_.model for block in message.content if isinstance(block, BetaFallbackBlock)),
        message.model,
    )
    totals: dict[str, dict[str, int]] = {}
    for it in iterations:
        model = getattr(it, "model", None) or refused
        row = totals.setdefault(
            str(model),
            {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0},
        )
        row["inputTokens"] += it.input_tokens or 0
        row["outputTokens"] += it.output_tokens or 0
        row["cacheReadInputTokens"] += it.cache_read_input_tokens or 0
        row["cacheCreationInputTokens"] += it.cache_creation_input_tokens or 0
    return totals


def to_cli_payload(message: BetaMessage, duration_ms: int) -> dict[str, Any]:
    """Ответ API в формате ``claude -p --output-format json``: его пишут кеш и replay, читает ``base``.

    ``structured_output`` не кладём: при схеме ответ API это JSON в тексте, его разбирает и сверяет
    со схемой ``base.parse_cli_payload``. ``total_cost_usd`` API не сообщает, цену считает ``pricing``.
    """
    payload: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": _text(message.content),
        "stop_reason": message.stop_reason,
        "duration_ms": duration_ms,
        "model": message.model,
        "usage": message.usage.to_dict(mode="json", exclude_none=True),
        "message_id": message.id,
    }
    model_usage = _model_usage(message)
    if model_usage:
        payload["modelUsage"] = model_usage
    return payload


def _request_args(req: LLMRequest) -> dict[str, Any]:
    output_config: dict[str, Any] = {"effort": req.effort.value}
    if req.json_schema is not None:
        try:
            schema = anthropic.transform_schema(req.json_schema)
        except ValueError:
            # Схема, которую SDK не умеет привести (нет type): отдаём как есть, ответ API скажет, что не так.
            schema = req.json_schema
        output_config["format"] = {"type": "json_schema", "schema": schema}
    return {
        "model": req.model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": req.system,
        "messages": [{"role": "user", "content": req.prompt}],
        "output_config": output_config,
        "betas": [FALLBACK_BETA],
        "fallbacks": "default",
    }


class AnthropicAPI:
    """Вызов модели через Claude API по ключу ``ANTHROPIC_API_KEY``."""

    backend = Backend.API

    def __init__(
        self,
        api_key: str | None = None,
        *,
        http_client: httpx2.Client | None = None,
        max_retries: int = SDK_MAX_RETRIES,
    ) -> None:
        key = api_key or api_key_from_env()
        if key is None:
            raise LLMError(
                "Для --backend api нужен ключ Claude API, а ANTHROPIC_API_KEY не задан",
                code=LLM_UNAVAILABLE,
                hint=(
                    "Положите ключ в .env (cp .env.example .env, строка ANTHROPIC_API_KEY=...) или "
                    "запустите через подписку: --backend cli; без модели: --backend replay --replay ПАПКА"
                ),
            )
        # Ключ передаём явно: иначе SDK взял бы ANTHROPIC_AUTH_TOKEN или профиль `ant auth login`,
        # и в паспорте было бы неясно, чей доступ работал.
        self._client = anthropic.Anthropic(api_key=key, max_retries=max_retries, http_client=http_client)

    def _stream(self, req: LLMRequest) -> BetaMessage:
        client = self._client.with_options(timeout=float(req.timeout_s))
        with client.beta.messages.stream(**_request_args(req)) as stream:
            return stream.get_final_message()

    def call(self, req: LLMRequest) -> LLMResponse:
        started = time.monotonic()
        try:
            message = self._stream(req)
        except Exception as exc:  # ошибки SDK и транспорта становятся LLMError, прочие летят дальше
            raise _sdk_error(exc, req, time.monotonic() - started) from exc
        duration_ms = int((time.monotonic() - started) * 1000)
        return self._response(message, req, duration_ms)

    def _response(self, message: BetaMessage, req: LLMRequest, duration_ms: int) -> LLMResponse:
        payload = to_cli_payload(message, duration_ms)
        # Ответ, который мы превращаем в ошибку, всё равно оплачен: его токены идут в лог и лимит цены.
        spent = LLMResponse(
            call_id=req.call_id,
            text=payload["result"],
            model=message.model,
            usage=TokenUsage(
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
                cache_creation_input_tokens=message.usage.cache_creation_input_tokens or 0,
                cache_read_input_tokens=message.usage.cache_read_input_tokens or 0,
            ),
            duration_ms=duration_ms,
            backend=self.backend,
            raw=payload,
        )
        stop = message.stop_reason
        if stop == "refusal":
            category = message.stop_details.category if message.stop_details is not None else None
            raise _error(
                f"Модель {message.model} отказалась отвечать (категория: {category or 'не указана'}); "
                "запасная модель тоже отказала или недоступна",
                LLM_REFUSED,
                req,
                hint="Проверьте документ и сообщите команде; запуск через подписку: --backend cli",
                retryable=False,
                spent=spent,
            )
        if stop is None:
            raise _error(
                "Поток ответа Claude API закончился без завершения сообщения",
                LLM_OVERLOADED,
                req,
                hint=classify_error("overloaded").hint,
                retryable=True,
            )
        if stop == "max_tokens":
            raise _error(
                f"Ответ модели обрезан на пределе {MAX_OUTPUT_TOKENS} токенов вывода",
                LLM_CALL_FAILED,
                req,
                hint=_HINT_TRUNCATED,
                retryable=False,
                spent=spent,
            )
        if stop not in ("end_turn", "stop_sequence"):
            logger.warning("Claude API: необычная причина остановки %s (%s)", stop, req.call_id)
        try:
            return parse_cli_payload(payload, req, self.backend)
        except LLMError as exc:
            exc.spent = spent
            raise


def _sdk_error(exc: Exception, req: LLMRequest, elapsed_s: float) -> LLMError:
    """Исключение SDK или транспорта ``httpx2`` как ``LLMError`` с кодом, подсказкой и правом на повтор."""
    if isinstance(exc, anthropic.APITimeoutError):
        return _error(
            f"Claude API не ответил: таймаут {req.timeout_s} с, прошло {elapsed_s:.0f} с с повторами SDK",
            LLM_TIMEOUT,
            req,
            hint="Повторите прогон; если повторяется, возьмите профиль quick",
            retryable=True,
        )
    if isinstance(exc, anthropic.APIStatusError):
        return _status_error(exc, req)
    if isinstance(exc, anthropic.APIConnectionError | httpx2.TransportError | httpx2.StreamError):
        # Обрыв посреди тела ответа: httpx2 бросает свою ошибку, SDK её не оборачивает.
        return _connection_error(exc, req)
    if isinstance(exc, anthropic.APIResponseValidationError | httpx2.HTTPError | ValueError):
        # Ответ, который SDK не разобрал: испорченный поток, битый JSON события, ошибка распаковки.
        text = str(exc) or type(exc).__name__
        return _error(
            f"Claude API вернул ответ, который не удалось разобрать: {text[:300]}",
            LLM_BAD_RESPONSE,
            req,
            hint=_HINT_RETRY,
            retryable=True,
        )
    if isinstance(exc, anthropic.AnthropicError):
        return _error(
            f"Ошибка SDK anthropic: {type(exc).__name__}: {str(exc)[:300]}",
            LLM_CALL_FAILED,
            req,
            hint=_HINT_RETRY,
            retryable=False,
        )
    raise exc


__all__ = ["API_KEY_ENV", "AnthropicAPI", "api_key_from_env", "to_cli_payload"]
