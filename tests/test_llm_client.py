"""Клиент модели (#21) без сети: кеш, replay, повтор, лимит цены, лог вызовов, тарифы, выбор бэкенда.

Вместо ``claude`` подкладываем маленький скрипт на Python: он пишет в stdout JSON в формате
``claude -p --output-format json`` и считает свои запуски, так видно, сколько было «живых» вызовов.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import FIXTURES_LLM, REQUIREMENTS
from validator.config import load_config
from validator.errors import LLM_LIMIT, LLMError
from validator.llm import (
    FakeLLM,
    RecordingClient,
    ReplayLLM,
    ResponseCache,
    align_config,
    classify_error,
    instrument,
    make_client,
    read_calls_jsonl,
    response_cost,
    to_payload,
)
from validator.llm.base import parse_cli_payload
from validator.llm.calllog import response_file_name
from validator.llm.pricing import Tokens, price
from validator.llm.schema import schema_errors
from validator.models import (
    Backend,
    CacheMode,
    CallStatus,
    DiagLevel,
    DocKind,
    Effort,
    ExitCode,
    LLMRequest,
    LLMResponse,
    LLMStep,
    Profile,
    RunConfig,
    TokenUsage,
)
from validator.pipeline import run

POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="скрипт с shebang вместо claude только в POSIX"
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def request(call_index: int = 1, *, prompt: str = "Документ: пример.") -> LLMRequest:
    return LLMRequest(
        step=LLMStep.SEARCH,
        call_index=call_index,
        model="claude-haiku-5-5",
        effort=Effort.MEDIUM,
        system="Отвечай JSON.",
        prompt=prompt,
        json_schema=SCHEMA,
    )


def payload(**extra: Any) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "structured_output": {"ok": True},
        "duration_ms": 1500,
        "total_cost_usd": 0.5,
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 5}},
        **extra,
    }


class LiveStub:
    """Живой бэкенд без сети: отвечает по очереди заданными ответами или ошибками, считает вызовы."""

    backend = Backend.CLI

    def __init__(self, *answers: dict[str, Any] | LLMError) -> None:
        self.answers = list(answers) or [payload()]
        self.calls: list[LLMRequest] = []

    def call(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        answer = self.answers[min(len(self.calls), len(self.answers)) - 1]
        if isinstance(answer, LLMError):
            raise answer
        return parse_cli_payload(answer, req, self.backend)


def no_sleep(_: float) -> None:
    return None


# ---------------------------------------------------------------- кеш


def test_cache_miss_then_hit_skips_the_model(tmp_path: Path) -> None:
    live = LiveStub()
    cache = ResponseCache(tmp_path / "cache", CacheMode.USE)

    first = RecordingClient(live, cache=cache).call(request())
    second_client = RecordingClient(live, cache=cache)
    second = second_client.call(request())

    assert len(live.calls) == 1
    assert (first.cached, second.cached) == (False, True)
    assert second.structured == {"ok": True}
    assert [r.status for r in second_client.records] == [CallStatus.CACHED]


def test_changed_prompt_is_a_cache_miss(tmp_path: Path) -> None:
    live = LiveStub()
    client = RecordingClient(live, cache=ResponseCache(tmp_path, CacheMode.USE))

    client.call(request(prompt="Документ: версия 1."))
    client.call(request(prompt="Документ: версия 2."))

    assert len(live.calls) == 2


def test_passes_with_the_same_prompt_get_their_own_answers(tmp_path: Path) -> None:
    live = LiveStub(payload(structured_output={"ok": True}), payload(structured_output={"ok": False}))
    cache = ResponseCache(tmp_path, CacheMode.USE)
    RecordingClient(live, cache=cache).call(request(1))
    RecordingClient(live, cache=cache).call(request(2))

    again = RecordingClient(live, cache=cache)
    answers = [again.call(request(i)).structured for i in (1, 2)]

    assert answers == [{"ok": True}, {"ok": False}]
    assert len(live.calls) == 2


def test_cache_refresh_calls_the_model_and_rewrites(tmp_path: Path) -> None:
    live = LiveStub(payload(structured_output={"ok": True}), payload(structured_output={"ok": False}))
    RecordingClient(live, cache=ResponseCache(tmp_path, CacheMode.USE)).call(request())

    RecordingClient(live, cache=ResponseCache(tmp_path, CacheMode.REFRESH)).call(request())
    reread = RecordingClient(live, cache=ResponseCache(tmp_path, CacheMode.USE)).call(request())

    assert len(live.calls) == 2
    assert reread.cached
    assert reread.structured == {"ok": False}


def test_cache_off_neither_reads_nor_writes(tmp_path: Path) -> None:
    live = LiveStub()
    client = RecordingClient(live, cache=ResponseCache(tmp_path, CacheMode.OFF))

    client.call(request())
    client.call(request())

    assert len(live.calls) == 2
    assert not any(tmp_path.rglob("*.json"))


def test_broken_cache_file_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path, CacheMode.USE)
    path = cache.path_for(request().key)
    path.parent.mkdir(parents=True)
    path.write_text("{обрыв", encoding="utf-8")
    live = LiveStub()

    resp = RecordingClient(live, cache=cache).call(request())

    assert not resp.cached
    assert len(live.calls) == 1
    assert json.loads(path.read_text(encoding="utf-8"))["key"] == request().key


def test_failed_call_is_not_cached(tmp_path: Path) -> None:
    error = LLMError("лимит", code="LLM-002")
    cache = ResponseCache(tmp_path, CacheMode.USE)

    with pytest.raises(LLMError):
        RecordingClient(LiveStub(error), cache=cache).call(request())

    assert not any(tmp_path.rglob("*.json"))


# ---------------------------------------------------------------- повтор и лимит цены


def test_transient_error_is_retried_once_and_both_attempts_logged() -> None:
    transient = LLMError("перегрузка", code="LLM-004", retryable=True)
    live = LiveStub(transient, payload())
    pauses: list[float] = []
    client = RecordingClient(live, sleep=pauses.append, retry_delay_s=7)

    resp = client.call(request())

    assert resp.structured == {"ok": True}
    assert [(r.attempt, r.status) for r in client.records] == [(1, CallStatus.RETRIED), (2, CallStatus.OK)]
    assert client.records[0].error is not None
    assert pauses == [7]
    assert client.diagnostics == []  # вызов в итоге удался: в отчёте это не сбой


def test_second_transient_failure_gives_up() -> None:
    transient = LLMError("перегрузка", code="LLM-004", retryable=True)
    client = RecordingClient(LiveStub(transient, transient), sleep=no_sleep)

    with pytest.raises(LLMError) as err:
        client.call(request())

    assert err.value.code == "LLM-004"
    assert [r.status for r in client.records] == [CallStatus.RETRIED, CallStatus.ERROR]
    [diag] = client.diagnostics
    assert diag.code == "LLM-004"


def test_limit_is_not_retried() -> None:
    live = LiveStub(LLMError("лимит", code="LLM-002", retryable=False))
    client = RecordingClient(live, sleep=no_sleep)

    with pytest.raises(LLMError):
        client.call(request())

    assert len(live.calls) == 1


def test_fixture_backend_is_not_retried() -> None:
    fake = FakeLLM(responses={(LLMStep.SEARCH, 1): payload(structured_output={"bad": 1})})
    pauses: list[float] = []

    with pytest.raises(LLMError) as err:
        RecordingClient(fake, sleep=pauses.append).call(request())

    assert err.value.code == "LLM-011"
    assert pauses == []


def test_budget_stops_calls_after_the_limit() -> None:
    expensive = payload(modelUsage={"claude-opus-5-5": {"inputTokens": 0, "outputTokens": 30_000}})
    live = LiveStub(expensive)  # 30 000 токенов вывода Opus = $0,60 по тарифу
    client = RecordingClient(live, max_budget_usd=1.0)

    client.call(request(1))
    client.call(request(2))
    with pytest.raises(LLMError) as err:
        client.call(request(3))

    assert err.value.code == "LLM-003"
    assert "$1.00" in err.value.message
    assert len(live.calls) == 2
    assert client.spent_usd() == pytest.approx(1.2)
    assert client.records[-1].status is CallStatus.ERROR


def test_cache_hits_do_not_count_toward_budget(tmp_path: Path) -> None:
    expensive = payload(modelUsage={"claude-opus-5-5": {"inputTokens": 0, "outputTokens": 60_000}})
    cache = ResponseCache(tmp_path, CacheMode.USE)
    RecordingClient(LiveStub(expensive), cache=cache).call(request())

    client = RecordingClient(LiveStub(expensive), cache=cache, max_budget_usd=0.5)
    client.call(request())
    client.call(request())

    assert client.spent_usd() == 0
    assert [r.status for r in client.records] == [CallStatus.CACHED, CallStatus.CACHED]


# ---------------------------------------------------------------- лог вызовов и ответы на диске


def test_records_carry_tariff_cost_and_reported_cost(tmp_path: Path) -> None:
    client = RecordingClient(LiveStub(), responses_dir=tmp_path / "responses")

    client.call(request())

    record = client.records[0]
    assert record.cost_usd == pytest.approx(10 * 0.10 / 1e6 + 5 * 0.50 / 1e6)
    assert record.cost_reported_usd == 0.5
    assert record.model_actual == "claude-haiku-5-5"
    assert record.duration_ms == 1500
    assert record.response_path == f"responses/{response_file_name(request())}"
    saved = json.loads((tmp_path / record.response_path).read_text(encoding="utf-8"))
    assert saved["key"] == request().key
    assert saved["payload"]["structured_output"] == {"ok": True}


# ---------------------------------------------------------------- тарифы


def test_haiku_price_matches_live_claude_answer() -> None:
    # Живой ответ claude -p 25.11.2026 (Haiku 5.5, часовой кеш): total_cost_usd = 0.0003296.
    live = payload(
        total_cost_usd=0.0003296,
        usage={"input_tokens": 2, "output_tokens": 10, "cache_creation_input_tokens": 1622},
        modelUsage={
            "claude-haiku-5-5": {
                "inputTokens": 2,
                "outputTokens": 10,
                "cacheReadInputTokens": 0,
                "cacheCreationInputTokens": 1622,
                "cacheCreationEphemeral5mInputTokens": 0,
                "cacheCreationEphemeral1hInputTokens": 1622,
            }
        },
    )

    resp = parse_cli_payload(live, request(), Backend.CLI)

    assert response_cost(resp) == pytest.approx(0.0003296)


def test_price_uses_usage_when_model_usage_is_missing() -> None:
    raw = payload(usage={"input_tokens": 1000, "output_tokens": 1000, "cache_read_input_tokens": 1000})
    raw.pop("modelUsage")
    resp = parse_cli_payload(raw, request(), Backend.CLI).model_copy(update={"model": "claude-opus-5-5"})

    assert response_cost(resp) == pytest.approx((1000 * 4 + 1000 * 20 + 1000 * 0.2) / 1e6)


def test_unknown_model_has_no_tariff_and_falls_back_to_reported_cost() -> None:
    raw = payload(modelUsage={"claude-unknown-9": {"inputTokens": 1, "outputTokens": 1}})
    client = RecordingClient(LiveStub(raw))

    client.call(request())

    assert response_cost(parse_cli_payload(raw, request(), Backend.CLI)) is None
    assert client.records[0].cost_usd == 0.5


def test_haiku_long_prompt_uses_higher_tariff() -> None:
    short = price("claude-haiku-5-5", Tokens(input=100_000, output=1000))
    long = price("claude-haiku-5-5", Tokens(input=100_001, output=1000))

    assert short == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
    assert long == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)


# ---------------------------------------------------------------- схема ответа


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ({}, "нет обязательного поля «ok»"),
        ({"ok": "да"}, "ожидался тип boolean"),
        ({"ok": True, "extra": 1}, "лишнее поле «extra»"),
    ],
)
def test_schema_violations_are_named(value: Any, fragment: str) -> None:
    assert any(fragment in e for e in schema_errors(value, SCHEMA))


def test_schema_checks_nested_items_and_enum() -> None:
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string", "enum": ["a", "b"]}}},
    }

    assert schema_errors({"items": ["a", "b"]}, schema) == []
    assert schema_errors({"items": ["a", "c"]}, schema) == [
        "$.items[1]: значение 'c' не из списка ['a', 'b']"
    ]


def test_answer_against_schema_is_rejected_as_bad_response() -> None:
    with pytest.raises(LLMError) as err:
        parse_cli_payload(payload(structured_output={"ok": "да"}), request(), Backend.CLI)

    assert err.value.code == "LLM-011"
    assert err.value.retryable


def test_refusal_stop_reason_is_an_error() -> None:
    with pytest.raises(LLMError) as err:
        parse_cli_payload(payload(stop_reason="refusal", result=""), request(), Backend.CLI)

    assert err.value.code == "LLM-013"
    assert not err.value.retryable


def test_http_status_from_answer_decides_retry() -> None:
    with pytest.raises(LLMError) as err:
        parse_cli_payload(
            payload(is_error=True, result="API Error", api_error_status=529), request(), Backend.CLI
        )

    assert err.value.code == "LLM-004"
    assert err.value.retryable


# ---------------------------------------------------------------- replay


def _record_run(tmp_path: Path, *requests: LLMRequest) -> Path:
    run_dir = tmp_path / "run"
    client = RecordingClient(LiveStub(), responses_dir=run_dir / "responses")
    for req in requests:
        client.call(req)
    return run_dir


def test_replay_finds_answer_by_key(tmp_path: Path) -> None:
    run_dir = _record_run(tmp_path, request(1), request(2))
    replay = ReplayLLM(run_dir)
    client = RecordingClient(replay)

    resp = client.call(request(2))

    assert resp.structured == {"ok": True}
    assert resp.backend is Backend.REPLAY
    assert client.records[0].status is CallStatus.REPLAYED
    assert replay.diagnostics == []


def test_replay_falls_back_to_step_and_index_with_warning(tmp_path: Path) -> None:
    run_dir = _record_run(tmp_path, request(1))
    replay = ReplayLLM(tmp_path)  # папку отчёта тоже понимает: ищет в ней run/

    resp = RecordingClient(replay).call(request(1, prompt="Документ: изменили."))

    assert resp.structured == {"ok": True}
    assert replay.run_dir == run_dir
    [diag] = replay.diagnostics
    assert diag.code == "LLM-016"
    assert "search.1" in diag.message


def test_replay_warning_lists_calls_in_pipeline_order_not_arrival_order(tmp_path: Path) -> None:
    """Проходы поиска параллельны: порядок их прихода в replay случаен, а отчёт должен быть одинаковым."""
    _record_run(tmp_path, request(1), request(2), request(3))
    replay = ReplayLLM(tmp_path)
    client = RecordingClient(replay)

    for index in (3, 1, 2):
        client.call(request(index, prompt="Документ: изменили."))

    [diag] = replay.diagnostics
    assert "(search.1, search.2, search.3)" in diag.message


def test_replay_without_answer_gives_clear_error(tmp_path: Path) -> None:
    replay = ReplayLLM(_record_run(tmp_path, request(1)))

    with pytest.raises(LLMError) as err:
        replay.call(request(5))

    assert err.value.code == "LLM-015"
    assert "вызов 5" in err.value.message


def test_replay_of_empty_folder_fails_at_start(tmp_path: Path) -> None:
    with pytest.raises(LLMError) as err:
        ReplayLLM(tmp_path)

    assert err.value.code == "LLM-015"


# ---------------------------------------------------------------- выбор бэкенда


def _cfg(tmp_path: Path, **update: Any) -> RunConfig:
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "out" / "report.md")
    return cfg.model_copy(update={"cache_dir": tmp_path / "cache", "has_api_key": False, **update})


def test_auto_without_claude_uses_replay_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: None)
    run_dir = _record_run(tmp_path, request(1))

    client, diagnostics = make_client(_cfg(tmp_path, backend=Backend.AUTO, replay_dir=run_dir))

    assert isinstance(client, ReplayLLM)
    assert [d.code for d in diagnostics] == ["LLM-005"]


def test_auto_with_logged_out_claude_still_tries_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr("validator.llm.factory.login_status", lambda _exe: False)

    client, diagnostics = make_client(_cfg(tmp_path, backend=Backend.AUTO))

    assert client.backend is Backend.CLI
    [diag] = diagnostics
    assert (diag.code, diag.level) == ("LLM-001", DiagLevel.INFO)


def test_auto_with_logged_out_claude_prefers_given_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr("validator.llm.factory.login_status", lambda _exe: False)
    run_dir = _record_run(tmp_path, request(1))

    client, _ = make_client(_cfg(tmp_path, backend=Backend.AUTO, replay_dir=run_dir))

    assert isinstance(client, ReplayLLM)


def test_auto_without_any_model_reports_llm_001_with_replay_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: None)

    client, diagnostics = make_client(_cfg(tmp_path, backend=Backend.AUTO))

    with pytest.raises(LLMError):
        client.call(request())
    [diag] = diagnostics
    assert (diag.code, diag.level) == ("LLM-001", DiagLevel.ERROR)
    assert "--backend replay" in diag.hint


def test_auto_with_working_claude_says_recording_was_not_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr("validator.llm.factory.login_status", lambda _exe: True)

    client, diagnostics = make_client(_cfg(tmp_path, backend=Backend.AUTO, replay_dir=tmp_path))

    assert client.backend is Backend.CLI
    assert [d.code for d in diagnostics] == ["LLM-005"]


def test_replay_backend_without_folder_is_a_config_error(tmp_path: Path) -> None:
    client, diagnostics = make_client(_cfg(tmp_path, backend=Backend.REPLAY))

    with pytest.raises(LLMError):
        client.call(request())
    assert [d.code for d in diagnostics] == ["CFG-001"]


def test_cache_is_off_for_fixtures_and_replay(tmp_path: Path) -> None:
    client = instrument(_cfg(tmp_path, backend=Backend.FAKE), FakeLLM())

    assert client.cache is None


# ---------------------------------------------------------------- сквозные прогоны


def _fake_claude(tmp_path: Path, mode: str = "ok") -> tuple[str, Path]:
    """Скрипт вместо claude: ``ok`` отвечает по схеме, без схемы как проход поиска без находок (#18)."""
    counter = tmp_path / "calls.txt"
    errors = {
        "limit": {"is_error": True, "subtype": "success", "result": "You've hit your limit · resets 3am"},
        "nologin": {
            "is_error": True,
            "result": "Not logged in · Please run /login",
            "api_error_status": None,
        },
    }
    script = tmp_path / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"open({str(counter)!r}, 'a').write('x')\n"
        "args = sys.argv[1:]\n"
        "system = open(args[args.index('--system-prompt-file') + 1], encoding='utf-8').read()\n"
        "prompt = sys.stdin.read()\n"
        f"mode = {mode!r}\n"
        "if mode == 'sleep':\n    time.sleep(5)\n"
        f"errors = {errors!r}\n"
        "if mode in errors:\n    print(json.dumps(errors[mode])); sys.exit(1)\n"
        "answer = {'type': 'result', 'subtype': 'success', 'is_error': False,\n"
        "          'result': '## Обзор документа\\n\\nПроверка выполнена. ' + str(len(system) > 0)\n"
        "                    + '\\n\\n## Находки\\n\\nНаходок нет.\\n', 'duration_ms': 10,\n"
        "          'usage': {'input_tokens': 100, 'output_tokens': 10},\n"
        "          'modelUsage': {args[args.index('--model') + 1]:\n"
        "                         {'inputTokens': 100, 'outputTokens': 10}}}\n"
        # нарезка отвечает записанным живым ответом, остальные шаги пустым списком находок
        f"segment = json.load(open({str(FIXTURES_LLM / 'segment' / '1.json')!r}, encoding='utf-8'))\n"
        "if '--json-schema' in args:\n"
        "    schema = json.loads(args[args.index('--json-schema') + 1])\n"
        "    is_segment = 'requirements' in schema.get('properties', {})\n"
        "    answer['structured_output'] = segment['structured_output'] if is_segment else {'findings': []}\n"
        "print(json.dumps(answer))\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script), counter


def _cli_run(tmp_path: Path, out: str, **update: Any) -> RunConfig:
    return _cfg(tmp_path, backend=Backend.CLI, profile=Profile.QUICK, passes=1, **update).model_copy(
        update={"output_path": tmp_path / out / "report.md"}
    )


@POSIX_ONLY
def test_second_cli_run_takes_everything_from_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe, counter = _fake_claude(tmp_path)
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: exe)

    first = run(_cli_run(tmp_path, "out"))
    live_calls = len(counter.read_text())
    second = run(_cli_run(tmp_path, "out2"))

    assert first.exit_code is second.exit_code is ExitCode.OK
    assert live_calls >= 1
    assert len(counter.read_text()) == live_calls
    first_log = read_calls_jsonl(tmp_path / "out" / "run" / "calls.jsonl")
    second_log = read_calls_jsonl(tmp_path / "out2" / "run" / "calls.jsonl")
    assert [r.status for r in first_log] == [CallStatus.OK] * live_calls
    assert [r.status for r in second_log] == [CallStatus.CACHED] * live_calls
    assert all((tmp_path / "out2" / "run" / r.response_path).is_file() for r in second_log if r.response_path)


@POSIX_ONLY
@pytest.mark.parametrize(("mode", "code"), [("limit", "LLM-002"), ("nologin", "LLM-001")])
def test_recorded_cli_errors_reach_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, code: str
) -> None:
    exe, counter = _fake_claude(tmp_path, mode)
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: exe)

    outcome = run(_cli_run(tmp_path, "out"))

    assert outcome.exit_code is ExitCode.MODEL
    assert any(d.code == code for d in outcome.diagnostics)
    assert code in (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert len(counter.read_text()) >= 1  # без повторов: каждый вызов шага ровно один раз
    log = read_calls_jsonl(tmp_path / "out" / "run" / "calls.jsonl")
    assert all(r.attempt == 1 and r.status is CallStatus.ERROR for r in log)


@POSIX_ONLY
def test_cli_timeout_is_retried_then_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe, _ = _fake_claude(tmp_path, "sleep")
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: exe)
    monkeypatch.setattr("validator.llm.calllog.RETRY_DELAY_S", 0.0)

    outcome = run(_cli_run(tmp_path, "out", call_timeout_s=1))

    assert outcome.exit_code is ExitCode.MODEL
    assert any(d.code == "LLM-012" for d in outcome.diagnostics)
    log = read_calls_jsonl(tmp_path / "out" / "run" / "calls.jsonl")
    assert [r.attempt for r in log][:2] == [1, 2]


@POSIX_ONLY
def test_budget_option_stops_the_run_with_diagnostic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe, counter = _fake_claude(tmp_path)
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: exe)
    cfg = _cli_run(tmp_path, "out", max_budget_usd=0.0000001).model_copy(update={"passes": 3})

    outcome = run(cfg)

    assert len(counter.read_text()) == 1
    assert any(d.code == "LLM-003" for d in outcome.diagnostics)


def test_replay_rebuilds_the_same_report(tmp_path: Path) -> None:
    recorded = run(_cfg(tmp_path, backend=Backend.FAKE))
    replay_cfg = _cfg(tmp_path, backend=Backend.REPLAY, replay_dir=tmp_path / "out" / "run").model_copy(
        update={"output_path": tmp_path / "out2" / "report.md"}
    )

    replayed = run(replay_cfg)

    assert recorded.report is not None and replayed.report is not None
    assert replayed.exit_code is ExitCode.OK
    assert replayed.report.verification == recorded.report.verification
    assert replayed.report.questions == recorded.report.questions
    log = read_calls_jsonl(tmp_path / "out2" / "run" / "calls.jsonl")
    assert log and all(r.status is CallStatus.REPLAYED for r in log)
    assert any(d.code == "LLM-005" for d in replayed.diagnostics)


def test_replay_takes_passes_and_search_model_from_the_recording(tmp_path: Path) -> None:
    run(_cfg(tmp_path, backend=Backend.FAKE, passes=1))
    replay_cfg = _cfg(tmp_path, backend=Backend.REPLAY, replay_dir=tmp_path / "out", passes=3).model_copy(
        update={"output_path": tmp_path / "out2" / "report.md"}
    )

    replayed = run(replay_cfg)

    assert replayed.report is not None
    assert replayed.report.passport.passes == 1
    assert not any(d.code in ("LLM-015", "LLM-016", "LLM-030") for d in replayed.diagnostics)
    assert any(
        d.code == "LLM-005" and "Проходов поиска в записи: 1" in d.message for d in replayed.diagnostics
    )


def test_new_run_clears_old_responses(tmp_path: Path) -> None:
    stale = tmp_path / "out" / "run" / "responses" / "search.9-000000000000.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}", encoding="utf-8")

    run(_cfg(tmp_path, backend=Backend.FAKE))

    assert not stale.exists()
    assert any(stale.parent.glob("search.1-*.json"))


def test_calls_log_is_valid_and_points_to_saved_answers(tmp_path: Path) -> None:
    run(_cfg(tmp_path, backend=Backend.FAKE))

    run_dir = tmp_path / "out" / "run"
    records = read_calls_jsonl(run_dir / "calls.jsonl")

    assert records
    for record in records:
        assert record.response_path is not None
        saved = json.loads((run_dir / record.response_path).read_text(encoding="utf-8"))
        assert saved["key"] == record.key
        assert saved["call_id"] == record.call_id


# ---------------------------------------------------------------- замечания ревью кода


@pytest.mark.parametrize(
    "message", ["API Error: 429 rate limit reached", "read ECONNRESET 104", "connection reset 3 times"]
)
def test_short_network_and_rate_errors_are_not_subscription_limit(message: str) -> None:
    failure = classify_error(message, status=429 if "429" in message else None)

    assert failure.code != "LLM-002"


def test_rate_limit_status_is_retried_even_with_limit_words() -> None:
    assert classify_error("limit reached", status=429).retryable


def test_replay_keeps_pass_count_when_last_pass_failed(tmp_path: Path) -> None:
    run_dir = _record_run(tmp_path, request(1), request(2))
    log = RecordingClient(LiveStub(LLMError("таймаут", code="LLM-012")))
    with pytest.raises(LLMError):
        log.call(request(3))
    (run_dir / "calls.jsonl").write_text(log.records[0].model_dump_json() + "\n", encoding="utf-8")

    replay = ReplayLLM(run_dir)

    assert replay.calls_in_recording(LLMStep.SEARCH) == 3
    cfg, _ = align_config(_cfg(tmp_path, backend=Backend.REPLAY, replay_dir=run_dir, passes=1), replay)
    assert cfg.passes == 3


def test_response_without_raw_keeps_its_price_after_cache(tmp_path: Path) -> None:
    resp = LLMResponse(
        call_id="search.1",
        structured={"ok": True},
        model="claude-opus-5-5",
        usage=TokenUsage(input_tokens=1000, output_tokens=1000),
        backend=Backend.API,
    )
    cached = parse_cli_payload(to_payload(resp), request(), Backend.API, cached=True)

    assert response_cost(cached) == pytest.approx((1000 * 4 + 1000 * 20) / 1e6)
    assert cached.model == "claude-opus-5-5"


def test_whole_float_is_an_integer() -> None:
    assert schema_errors(3.0, {"type": "integer"}) == []
    assert schema_errors(3.5, {"type": "integer"})


def test_replay_into_the_same_folder_adds_no_duplicates(tmp_path: Path) -> None:
    run(_cfg(tmp_path, backend=Backend.FAKE))
    responses = tmp_path / "out" / "run" / "responses"
    before = sorted(p.name for p in responses.iterdir())

    again = run(_cfg(tmp_path, backend=Backend.REPLAY, replay_dir=tmp_path / "out" / "run"))

    assert again.exit_code is ExitCode.OK
    assert sorted(p.name for p in responses.iterdir()) == before


@pytest.mark.parametrize(
    "message",
    [
        "You've hit your 5-hour limit · resets 2:10am (UTC)",
        "You've hit your weekly limit · resets Fri 9am",
        "You've hit your limit · resets 3am",
    ],
)
def test_subscription_limit_with_status_429_is_llm_002_and_not_retried(message: str) -> None:
    """Живой ответ claude -p при исчерпанном лимите: is_error и api_error_status 429; это не перегрузка."""
    failure = classify_error(message, status=429)

    assert failure.code == LLM_LIMIT
    assert not failure.retryable
