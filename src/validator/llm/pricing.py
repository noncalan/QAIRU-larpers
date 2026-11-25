"""Цена вызова по тарифу Claude API: для паспорта, метрик прогона и лимита ``--max-budget-usd``.

Подписка (``claude -p``) денег за вызов не списывает, но цена по тарифу нужна жюри и для бизнес-ценности
(сколько стоит одна проверка через API), а лимит прогона считаем по ней же.

Тарифы за 1 млн токенов на 25.11.2026 (``docs/research/21-model-and-effort.md``, раздел «Цена»; справка
Claude API в Claude Code 2.1.332, таблица моделей от 03.11.2026):

| Модель | Вход | Выход | Чтение кеша |
|---|---|---|---|
| Opus 5.5 | $4 | $20 | $0,20 |
| Sonnet 5.5 | $2 | $10 | $0,10 |
| Haiku 5.5 | $0,10 | $0,50 | $0,01 (0,1 × вход, по общему правилу; отдельно не проверено) |

Запись в кеш: 1,25 × вход на 5 минут, 2 × вход на 1 час (``claude -p`` пишет часовой кеш). У Haiku 5.5
запрос длиннее 100 тыс. токенов идёт по тарифу $0,50 / $2,50; множители кеша для него берём те же
(не проверено). Сверено с ``total_cost_usd`` живого ответа ``claude -p`` 25.11.2026: совпало до цента.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from validator.models import LLMResponse, TokenUsage

PRICING_DATE = "2026-11-25"
MTOK = 1_000_000
CACHE_WRITE_5M = 1.25
CACHE_WRITE_1H = 2.0


@dataclass(frozen=True)
class Tariff:
    """Цена за 1 млн токенов. ``long_*`` действуют, если вход запроса длиннее ``long_threshold``."""

    input: float
    output: float
    cache_read: float
    long_threshold: int | None = None
    long_input: float | None = None
    long_output: float | None = None

    def rates(self, prompt_tokens: int) -> tuple[float, float, float]:
        """Вход, выход и чтение кеша для запроса такой длины."""
        if self.long_threshold is None or prompt_tokens <= self.long_threshold:
            return self.input, self.output, self.cache_read
        long_input = self.long_input or self.input
        scale = long_input / self.input
        return long_input, self.long_output or self.output, self.cache_read * scale


TARIFFS: dict[str, Tariff] = {
    "claude-opus-5-5": Tariff(input=4.0, output=20.0, cache_read=0.20),
    "claude-sonnet-5-5": Tariff(input=2.0, output=10.0, cache_read=0.10),
    "claude-haiku-5-5": Tariff(
        input=0.10, output=0.50, cache_read=0.01, long_threshold=100_000, long_input=0.50, long_output=2.50
    ),
}


@dataclass(frozen=True)
class Tokens:
    """Токены одного запроса к одной модели; запись в кеш разделена на 5-минутную и часовую."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    @property
    def prompt(self) -> int:
        return self.input + self.cache_read + self.cache_write_5m + self.cache_write_1h


def tariff_for(model: str | None) -> Tariff | None:
    """Тариф по полному имени модели; суффикс вида ``[1m]`` или дата снапшота не мешают."""
    if not model:
        return None
    name = model.split("[", 1)[0].strip()
    for known, tariff in TARIFFS.items():
        if name == known or name.startswith(known + "-"):
            return tariff
    return None


def price(model: str | None, tokens: Tokens) -> float | None:
    """Цена запроса по тарифу или None, если модель неизвестна."""
    tariff = tariff_for(model)
    if tariff is None:
        return None
    rate_in, rate_out, rate_read = tariff.rates(tokens.prompt)
    total = (
        tokens.input * rate_in
        + tokens.output * rate_out
        + tokens.cache_read * rate_read
        + tokens.cache_write_5m * rate_in * CACHE_WRITE_5M
        + tokens.cache_write_1h * rate_in * CACHE_WRITE_1H
    )
    return total / MTOK


def _int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _tokens_from_model_usage(entry: Mapping[str, Any]) -> Tokens:
    """Строка ``modelUsage`` ответа ``claude -p`` (camelCase, запись в кеш с разбивкой по TTL)."""
    write_5m = _int(entry, "cacheCreationEphemeral5mInputTokens")
    write_1h = _int(entry, "cacheCreationEphemeral1hInputTokens")
    if not write_5m and not write_1h:
        write_5m = _int(entry, "cacheCreationInputTokens")
    return Tokens(
        input=_int(entry, "inputTokens"),
        output=_int(entry, "outputTokens"),
        cache_read=_int(entry, "cacheReadInputTokens"),
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
    )


def _tokens_from_usage(usage: TokenUsage, raw_usage: Any) -> Tokens:
    """``usage`` ответа: если есть разбивка ``cache_creation`` по TTL, берём её, иначе считаем 5 минут."""
    split = raw_usage.get("cache_creation") if isinstance(raw_usage, Mapping) else None
    write_5m, write_1h = usage.cache_creation_input_tokens, 0
    if isinstance(split, Mapping):
        write_5m = _int(split, "ephemeral_5m_input_tokens")
        write_1h = _int(split, "ephemeral_1h_input_tokens")
    return Tokens(
        input=usage.input_tokens,
        output=usage.output_tokens,
        cache_read=usage.cache_read_input_tokens,
        cache_write_5m=write_5m,
        cache_write_1h=write_1h,
    )


def response_cost(resp: LLMResponse) -> float | None:
    """Цена ответа по тарифу: по каждой модели из ``modelUsage``, иначе по ``usage`` и модели ответа.

    None, если тариф хотя бы одной модели неизвестен: тогда вызывающий берёт цену, которую сообщил бэкенд.
    """
    raw = resp.raw or {}
    model_usage = raw.get("modelUsage")
    if isinstance(model_usage, Mapping) and model_usage:
        costs = [
            price(str(model), _tokens_from_model_usage(entry))
            for model, entry in model_usage.items()
            if isinstance(entry, Mapping)
        ]
        if costs and all(c is not None for c in costs):
            return sum(c for c in costs if c is not None)
        return None
    return price(resp.model, _tokens_from_usage(resp.usage, raw.get("usage")))


__all__ = ["PRICING_DATE", "TARIFFS", "Tariff", "Tokens", "price", "response_cost", "tariff_for"]
