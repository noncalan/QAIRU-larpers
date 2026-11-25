"""Клиент модели: протокол и реализации (подписка, фикстуры; SDK и replay в #32 и #21)."""

from __future__ import annotations

from validator.llm.base import LLMClient, parse_cli_payload
from validator.llm.calllog import RecordingClient, write_calls_jsonl
from validator.llm.claude_cli import ClaudeCLI
from validator.llm.factory import UnavailableLLM, make_client
from validator.llm.fake import FakeLLM

__all__ = [
    "ClaudeCLI",
    "FakeLLM",
    "LLMClient",
    "RecordingClient",
    "UnavailableLLM",
    "make_client",
    "parse_cli_payload",
    "write_calls_jsonl",
]
