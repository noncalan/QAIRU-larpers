"""Клиенты модели без сети: разбор ответа ``claude -p``, фейковый клиент, аргументы и окружение CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from validator.errors import LLMError
from validator.llm import ClaudeCLI, FakeLLM, RecordingClient, parse_cli_payload
from validator.llm.claude_cli import DROP_ENV, build_args, clean_env
from validator.models import Backend, CallStatus, Effort, LLMRequest, LLMStep

SCHEMA: dict[str, Any] = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
# Фикстуры поиска отвечают по схеме заглушки поиска; тестам клиента хватает её верхнего уровня.
FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"findings": {"type": "array"}},
    "required": ["findings"],
}


def request(
    *, schema: dict[str, Any] | None = SCHEMA, call_index: int = 1, timeout_s: int = 60
) -> LLMRequest:
    return LLMRequest(
        step=LLMStep.SEARCH,
        call_index=call_index,
        model="claude-haiku-5-5",
        effort=Effort.MEDIUM,
        system="Отвечай JSON.",
        prompt="Документ: пример.",
        json_schema=schema,
        timeout_s=timeout_s,
    )


def envelope(**extra: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "duration_ms": 1234,
        "total_cost_usd": 0.0012,
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3, "unknown": 1},
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10}},
        **extra,
    }


def test_structured_output_is_parsed() -> None:
    resp = parse_cli_payload(envelope(structured_output={"ok": True}), request(), Backend.CLI)

    assert resp.structured == {"ok": True}
    assert resp.call_id == "search.1"
    assert resp.model == "claude-haiku-5-5"
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cache_read_input_tokens) == (
        10,
        5,
        3,
    )
    assert resp.cost_usd == pytest.approx(0.0012)
    assert resp.duration_ms == 1234


def test_fenced_json_in_text_is_accepted() -> None:
    resp = parse_cli_payload(envelope(result='```json\n{"ok": false}\n```'), request(), Backend.CLI)

    assert resp.structured == {"ok": False}


def test_missing_structured_answer_is_bad_response() -> None:
    with pytest.raises(LLMError) as err:
        parse_cli_payload(envelope(result="не JSON"), request(), Backend.CLI)

    assert err.value.code == "LLM-011"
    assert err.value.retryable


def test_text_answer_without_schema_is_fine() -> None:
    resp = parse_cli_payload(envelope(result="ok"), request(schema=None), Backend.CLI)

    assert resp.text == "ok"
    assert resp.structured is None


@pytest.mark.parametrize(
    ("message", "code", "retryable"),
    [
        # Лимит подписки снимается через часы: повтор через 20 с только потратит время.
        ("Claude AI usage limit reached|1764000000", "LLM-002", False),
        ("You've hit your limit · resets 3am (UTC)", "LLM-002", False),
        ("Invalid API key · Please run /login", "LLM-001", False),
        ("Not logged in · Please run /login", "LLM-001", False),
        ('API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}', "LLM-004", True),
        ("API Error: Connection error.", "LLM-004", True),
        (
            "API Error: Claude Code is unable to respond to this request, "
            "which appears to violate our Usage Policy",
            "LLM-013",
            False,
        ),
        ("Something unexpected happened", "LLM-010", False),
    ],
)
def test_error_payload_is_classified(message: str, code: str, retryable: bool) -> None:
    with pytest.raises(LLMError) as err:
        parse_cli_payload(envelope(is_error=True, result=message), request(), Backend.CLI)

    assert err.value.code == code
    assert err.value.retryable is retryable
    assert err.value.hint


def test_non_object_payload_is_bad_response() -> None:
    with pytest.raises(LLMError) as err:
        parse_cli_payload(["not", "a", "dict"], request(), Backend.CLI)

    assert err.value.code == "LLM-011"


def test_fake_llm_answers_from_fixture_files(fake_llm: FakeLLM) -> None:
    """Проход поиска отвечает свободным текстом без схемы (#18): фикстура это ответ в формате промпта."""
    resp = fake_llm.call(request(schema=None, call_index=2))

    assert resp.backend is Backend.FAKE
    assert resp.structured is None
    assert "## Находки" in resp.text


def test_fake_llm_without_fixture_raises(fake_llm: FakeLLM) -> None:
    with pytest.raises(LLMError) as err:
        fake_llm.call(request(call_index=99))

    assert err.value.code == "LLM-014"


def test_fake_llm_accepts_inline_responses() -> None:
    fake = FakeLLM(responses={(LLMStep.SEARCH, 1): envelope(structured_output={"ok": True})})

    assert fake.call(request()).structured == {"ok": True}


def test_recording_client_logs_success_and_failure(fake_llm: FakeLLM) -> None:
    client = RecordingClient(fake_llm)

    client.call(request(schema=None, call_index=1))
    with pytest.raises(LLMError):
        client.call(request(call_index=99))

    assert [(r.call_id, r.status) for r in client.records] == [
        ("search.1", CallStatus.OK),
        ("search.99", CallStatus.ERROR),
    ]
    assert client.records[1].error is not None
    assert "LLM-014" in client.records[1].error


def test_cli_args_follow_safe_flags(tmp_path: Path) -> None:
    args = build_args("claude", request(), tmp_path / "system.md")

    assert args[:2] == ["claude", "-p"]
    for flag in ("--safe-mode", "--no-session-persistence", "--system-prompt-file", "--json-schema"):
        assert flag in args
    assert "--system-prompt" not in args
    assert args[args.index("--system-prompt-file") + 1] == str(tmp_path / "system.md")
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--model") + 1] == "claude-haiku-5-5"
    assert args[args.index("--effort") + 1] == "medium"
    assert json.loads(args[args.index("--json-schema") + 1]) == SCHEMA


def test_cli_env_drops_keys_and_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in DROP_ENV:
        monkeypatch.setenv(name, "must-not-leak")

    env = clean_env()

    assert not set(DROP_ENV) & set(env)
    assert "ANTHROPIC_API_KEY" in DROP_ENV


def _fake_claude(tmp_path: Path, body: str) -> str:
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\nimport json, sys, time\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


@pytest.mark.skipif(sys.platform == "win32", reason="скрипт с shebang вместо claude работает только в POSIX")
def test_claude_cli_reads_stdin_and_parses_stdout(tmp_path: Path) -> None:
    exe = _fake_claude(
        tmp_path,
        "prompt = sys.stdin.read()\n"
        "answer = {'ok': 'Документ' in prompt}\n"
        "print(json.dumps({'subtype': 'success', 'result': '', 'structured_output': answer}))",
    )

    resp = ClaudeCLI(exe).call(request())

    assert resp.structured == {"ok": True}
    assert resp.backend is Backend.CLI


@pytest.mark.skipif(sys.platform == "win32", reason="скрипт с shebang вместо claude работает только в POSIX")
def test_claude_cli_timeout_is_retryable(tmp_path: Path) -> None:
    exe = _fake_claude(tmp_path, "time.sleep(5)")

    with pytest.raises(LLMError) as err:
        ClaudeCLI(exe).call(request(timeout_s=1))

    assert err.value.code == "LLM-012"
    assert err.value.retryable


@pytest.mark.skipif(sys.platform == "win32", reason="скрипт с shebang вместо claude работает только в POSIX")
def test_claude_cli_garbage_stdout_is_bad_response(tmp_path: Path) -> None:
    exe = _fake_claude(tmp_path, "print('not json'); sys.exit(1)")

    with pytest.raises(LLMError) as err:
        ClaudeCLI(exe).call(request())

    assert err.value.code == "LLM-011"
