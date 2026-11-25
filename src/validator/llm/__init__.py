"""Клиент модели: протокол, бэкенды (подписка, replay, фикстуры; SDK в #32), кеш, лог вызовов, тарифы."""

from __future__ import annotations

from validator.llm.base import LLMClient, classify_error, parse_cli_payload, to_payload
from validator.llm.cache import ResponseCache
from validator.llm.calllog import RecordingClient, read_calls_jsonl, write_calls_jsonl
from validator.llm.claude_cli import ClaudeCLI
from validator.llm.factory import UnavailableLLM, align_config, instrument, make_client
from validator.llm.fake import FakeLLM
from validator.llm.pricing import response_cost
from validator.llm.replay import ReplayLLM

__all__ = [
    "ClaudeCLI",
    "FakeLLM",
    "LLMClient",
    "RecordingClient",
    "ReplayLLM",
    "ResponseCache",
    "UnavailableLLM",
    "align_config",
    "classify_error",
    "instrument",
    "make_client",
    "parse_cli_payload",
    "read_calls_jsonl",
    "response_cost",
    "to_payload",
    "write_calls_jsonl",
]
