"""Бэкенд ``api`` (SDK ``anthropic``, #32) без сети: HTTP-транспорт клиента SDK подменён.

Ключа API у команды нет (D-007, D-009), поэтому живых вызовов здесь нет. SDK получает
``httpx2.MockTransport``, который отдаёт синтетические потоки SSE и тела ошибок из
``tests/fixtures/llm/api/`` (формат по справке SDK, см. README фикстур). Так проверяется всё, что
делает наш код: какой запрос уходит в API, как разбирается поток, как ошибки SDK становятся
``LLMError`` с кодом и подсказкой, и что прогон агента через этот бэкенд доходит до отчёта.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest

from conftest import FIXTURES_LLM, REQUIREMENTS
from validator.config import load_config
from validator.errors import LLMError
from validator.llm import (
    AnthropicAPI,
    RecordingClient,
    UnavailableLLM,
    make_client,
    response_cost,
    to_payload,
)
from validator.llm.anthropic_api import FALLBACK_BETA, MAX_OUTPUT_TOKENS
from validator.llm.base import parse_cli_payload
from validator.models import (
    Backend,
    CallStatus,
    DiagLevel,
    DocKind,
    Effort,
    ExitCode,
    LLMRequest,
    LLMStep,
    RunConfig,
)
from validator.pipeline import run

API_FIXTURES = FIXTURES_LLM / "api"
PROMPTS = Path(__file__).resolve().parents[1] / "src" / "validator" / "prompts"
FAKE_KEY = "not-a-key-not-a-key-not-a-key"  # низкая энтропия: gitleaks не примет за ключ
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "refID": {"type": "string", "pattern": r"^B\d{3}(\.\d+)?$"},
                    "summary": {"type": "string", "maxLength": 200},
                },
                "required": ["refID", "summary"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["requirements"],
    "additionalProperties": False,
}

Handler = Callable[[httpx2.Request], httpx2.Response]


def request(*, schema: dict[str, Any] | None = None, model: str = "claude-opus-5-5") -> LLMRequest:
    return LLMRequest(
        step=LLMStep.SEGMENT if schema else LLMStep.SEARCH,
        call_index=1,
        model=model,
        effort=Effort.XHIGH,
        system="Ты проверяешь требования.",
        prompt="Документ: Система должна быстро обрабатывать платежи.",
        json_schema=schema,
        timeout_s=30,
    )


def sse_body(name: str) -> bytes:
    return (API_FIXTURES / f"{name}.sse").read_bytes()


def stream_response(body: bytes) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def error_response(status: int, name: str) -> httpx2.Response:
    body = json.loads((API_FIXTURES / f"error_{name}.json").read_text(encoding="utf-8"))
    # retry-after-ms: SDK повторяет 429 и 5xx с этой паузой, тесты не ждут секундами.
    return httpx2.Response(
        status, headers={"retry-after-ms": "1", "request-id": body["request_id"]}, json=body
    )


class Recorder:
    """Транспорт SDK: отдаёт ответы по очереди (последний повторяется) и запоминает запросы."""

    def __init__(self, *responses: Callable[[], httpx2.Response]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []
        self._lock = threading.Lock()

    def __call__(self, req: httpx2.Request) -> httpx2.Response:
        with self._lock:
            self.requests.append(req)
            make = self.responses[min(len(self.requests), len(self.responses)) - 1]
        return make()

    def body(self, index: int = 0) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.requests[index].content)
        return data


def api(handler: Handler, *, max_retries: int = 2) -> AnthropicAPI:
    return AnthropicAPI(
        FAKE_KEY, http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=max_retries
    )


def failure(handler: Handler, req: LLMRequest | None = None, *, max_retries: int = 2) -> LLMError:
    with pytest.raises(LLMError) as caught:
        api(handler, max_retries=max_retries).call(req or request())
    assert FAKE_KEY not in caught.value.message and FAKE_KEY not in caught.value.hint
    return caught.value


# ---------------------------------------------------------------- обычный ответ


def test_streamed_text_answer_becomes_response_with_usage_and_tariff_cost() -> None:
    rec = Recorder(lambda: stream_response(sse_body("text")))

    resp = api(rec).call(request())

    assert resp.backend is Backend.API
    assert resp.text.startswith("### Находка 1\n**Цитата:** «Система должна быстро обрабатывать платежи»")
    assert resp.structured is None
    assert resp.model == "claude-opus-5-5"
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.cache_read_input_tokens) == (
        1840,
        912,
        1200,
    )
    assert resp.duration_ms is not None and resp.cost_usd is None  # API цену не сообщает
    assert response_cost(resp) == pytest.approx((1840 * 4 + 912 * 20 + 1200 * 0.2) / 1_000_000)


def test_request_carries_model_effort_prompt_fallback_and_streams() -> None:
    rec = Recorder(lambda: stream_response(sse_body("text")))

    api(rec).call(request())

    body = rec.body()
    assert body["model"] == "claude-opus-5-5"
    assert body["output_config"] == {"effort": "xhigh"}
    assert body["max_tokens"] == MAX_OUTPUT_TOKENS
    assert body["stream"] is True
    assert body["system"] == "Ты проверяешь требования."
    assert body["messages"] == [
        {"role": "user", "content": "Документ: Система должна быстро обрабатывать платежи."}
    ]
    assert body["fallbacks"] == "default"
    assert "thinking" not in body  # у моделей 5.5 адаптивное мышление включено по умолчанию
    assert "temperature" not in body
    assert FALLBACK_BETA in rec.requests[0].headers["anthropic-beta"]


def test_answer_survives_cache_and_replay_format() -> None:
    resp = api(Recorder(lambda: stream_response(sse_body("text")))).call(request())

    again = parse_cli_payload(json.loads(json.dumps(to_payload(resp))), request(), Backend.API, cached=True)

    assert again.text == resp.text
    assert again.usage == resp.usage
    assert again.model == resp.model
    assert response_cost(again) == response_cost(resp)


# ---------------------------------------------------------------- ответ по схеме


def test_schema_goes_to_output_config_in_a_form_the_api_accepts() -> None:
    rec = Recorder(lambda: stream_response(sse_body("structured")))

    resp = api(rec).call(request(schema=SCHEMA, model="claude-sonnet-5-5"))

    assert resp.structured == {
        "requirements": [{"refID": "B012.1", "summary": "Перевод между своими счетами"}]
    }
    fmt = rec.body()["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    item = fmt["schema"]["properties"]["requirements"]["items"]
    assert item["additionalProperties"] is False
    assert "pattern" not in item["properties"]["refID"]  # SDK переносит неподдерживаемое в описание
    assert "maxLength" not in item["properties"]["summary"]
    assert SCHEMA["properties"]["requirements"]["items"]["properties"]["refID"]["pattern"]  # исходник цел


def test_answer_that_breaks_the_original_schema_is_a_retryable_bad_response() -> None:
    err = failure(Recorder(lambda: stream_response(sse_body("structured_bad"))), request(schema=SCHEMA))

    assert err.code == "LLM-011"
    assert err.retryable


# ---------------------------------------------------------------- отказ и запасная модель


def test_refusal_is_llm_013_and_not_retried() -> None:
    err = failure(Recorder(lambda: stream_response(sse_body("refusal"))))

    assert err.code == "LLM-013"
    assert not err.retryable
    assert "cyber" in err.message


def test_fallback_answer_is_used_and_priced_per_model() -> None:
    resp = api(Recorder(lambda: stream_response(sse_body("fallback")))).call(request())

    assert resp.text.startswith("### Находка 1")
    assert resp.model == "claude-opus-5"
    assert resp.raw is not None and set(resp.raw["modelUsage"]) == {"claude-opus-5-5", "claude-opus-5"}
    assert response_cost(resp) == pytest.approx((1840 * 4 + 1840 * 5 + 800 * 25) / 1_000_000)


# ---------------------------------------------------------------- ошибки API


def test_rate_limit_then_success_is_retried_by_the_sdk() -> None:
    rec = Recorder(lambda: error_response(429, "429"), lambda: stream_response(sse_body("text")))

    resp = api(rec).call(request())

    assert resp.text
    assert len(rec.requests) == 2


def test_overload_that_does_not_pass_is_llm_004_and_retryable() -> None:
    rec = Recorder(lambda: error_response(529, "529"))

    err = failure(rec)

    assert len(rec.requests) == 3  # первая попытка и два повтора SDK
    assert err.code == "LLM-004"
    assert err.retryable
    assert "529" in err.message and "req_011SyntheticFixture0000" in err.message


@pytest.mark.parametrize(
    ("status", "name", "code", "retryable", "hint_part"),
    [
        (401, "401", "LLM-001", False, "ANTHROPIC_API_KEY"),
        (429, "429_spend_limit", "LLM-002", False, "баланс"),
        (400, "400_credit", "LLM-002", False, "баланс"),
        (400, "400_too_long", "LLM-010", False, "разделите"),
        (404, "404", "LLM-010", False, "недоступна"),
    ],
)
def test_api_errors_get_code_and_hint(
    status: int, name: str, code: str, retryable: bool, hint_part: str
) -> None:
    err = failure(Recorder(lambda: error_response(status, name)), max_retries=0)

    assert (err.code, err.retryable) == (code, retryable)
    assert hint_part in err.hint
    assert err.location == "search.1"


def test_error_event_in_the_middle_of_the_stream_is_retryable_overload() -> None:
    err = failure(Recorder(lambda: stream_response(sse_body("error_event"))))

    assert err.code == "LLM-004"
    assert err.retryable


def test_stream_that_ends_without_message_stop_is_retryable() -> None:
    err = failure(Recorder(lambda: stream_response(sse_body("cut"))))

    assert err.code == "LLM-004"
    assert err.retryable
    assert "без завершения" in err.message


def test_connection_dropped_mid_stream_is_retryable() -> None:
    def dropped() -> httpx2.Response:
        def chunks() -> Iterator[bytes]:
            yield sse_body("cut")
            raise httpx2.RemoteProtocolError("peer closed connection without sending complete message body")

        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=chunks())

    err = failure(Recorder(dropped))

    assert err.code == "LLM-004"
    assert err.retryable


def test_timeout_is_llm_012() -> None:
    def slow(req: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=req)

    err = failure(slow, max_retries=0)

    assert err.code == "LLM-012"
    assert err.retryable


def test_truncated_answer_is_an_error_not_a_partial_report() -> None:
    err = failure(Recorder(lambda: stream_response(sse_body("max_tokens"))))

    assert err.code == "LLM-010"
    assert not err.retryable


def test_recording_client_retries_once_more_after_sdk_gives_up() -> None:
    rec = Recorder(lambda: error_response(529, "529"), lambda: stream_response(sse_body("text")))
    client = RecordingClient(api(rec, max_retries=0), sleep=lambda _s: None)

    resp = client.call(request())

    assert resp.text
    assert [r.status for r in client.records] == [CallStatus.RETRIED, CallStatus.OK]
    assert client.records[-1].cost_usd == pytest.approx(response_cost(resp))
    assert client.spent_usd() > 0


def test_rejected_but_billed_answers_count_toward_the_budget() -> None:
    rec = Recorder(lambda: stream_response(sse_body("max_tokens")))
    client = RecordingClient(api(rec), max_budget_usd=0.5, sleep=lambda _s: None)

    with pytest.raises(LLMError):
        client.call(request())

    [record] = client.records
    assert record.status is CallStatus.ERROR
    assert record.usage.output_tokens == 128000
    assert record.cost_usd == pytest.approx((1840 * 4 + 128000 * 20) / 1_000_000)
    assert client.spent_usd() == pytest.approx(record.cost_usd)
    with pytest.raises(LLMError) as caught:
        client.call(request())
    assert caught.value.code == "LLM-003"  # лимит цены исчерпан оплаченным обрезанным ответом
    assert len(rec.requests) == 1


def test_answer_against_schema_still_carries_its_tokens() -> None:
    err = failure(Recorder(lambda: stream_response(sse_body("structured_bad"))), request(schema=SCHEMA))

    assert err.spent is not None and err.spent.usage.output_tokens == 40


@pytest.mark.parametrize(
    ("broken", "code"),
    [
        (
            lambda: httpx2.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"event: message_start\ndata: {not json\n\n",
            ),
            "LLM-011",
        ),
        (
            lambda: httpx2.Response(
                200,
                headers={"content-type": "text/event-stream", "content-encoding": "gzip"},
                content=b"\x00 not gzip at all",
            ),
            "LLM-004",  # httpx2 считает битое сжатие ошибкой транспорта: как обрыв, повторяем
        ),
    ],
    ids=["bad-json-event", "bad-gzip"],
)
def test_garbled_stream_is_a_retryable_error_not_a_crash(
    broken: Callable[[], httpx2.Response], code: str
) -> None:
    err = failure(Recorder(broken))

    assert err.code == code
    assert err.retryable


# ---------------------------------------------------------------- ключ и выбор бэкенда


def _cfg(tmp_path: Path, backend: Backend) -> RunConfig:
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "out" / "report.md")
    return cfg.model_copy(update={"backend": backend, "cache_dir": tmp_path / "cache"})


def test_api_backend_without_key_is_a_clear_llm_001(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    client, diagnostics = make_client(_cfg(tmp_path, Backend.API))

    assert isinstance(client, UnavailableLLM)
    [diag] = diagnostics
    assert (diag.code, diag.level) == ("LLM-001", DiagLevel.ERROR)
    assert ".env" in diag.hint and "--backend cli" in diag.hint


def test_blank_key_counts_as_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")

    with pytest.raises(LLMError) as caught:
        AnthropicAPI()

    assert caught.value.code == "LLM-001"


def test_api_backend_with_key_uses_the_sdk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)

    client, diagnostics = make_client(_cfg(tmp_path, Backend.API))

    assert isinstance(client, AnthropicAPI) and not diagnostics


@pytest.mark.parametrize("claude", ["missing", "logged_out"])
def test_auto_with_key_uses_the_sdk_when_subscription_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude: str
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    exe = None if claude == "missing" else "/usr/bin/claude"
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: exe)
    monkeypatch.setattr("validator.llm.factory.login_status", lambda _exe: False)

    client, diagnostics = make_client(_cfg(tmp_path, Backend.AUTO).model_copy(update={"has_api_key": True}))

    assert isinstance(client, AnthropicAPI)
    assert not any(d.level is DiagLevel.ERROR for d in diagnostics)


def test_auto_prefers_logged_in_subscription_over_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setattr("validator.llm.claude_cli.find_claude", lambda: "/usr/bin/claude")
    monkeypatch.setattr("validator.llm.factory.login_status", lambda _exe: True)

    client, _ = make_client(_cfg(tmp_path, Backend.AUTO).model_copy(update={"has_api_key": True}))

    assert client.backend is Backend.CLI


def test_dotenv_key_with_only_spaces_is_no_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")

    assert not _cfg(tmp_path, Backend.AUTO).has_api_key


def test_run_with_api_backend_and_no_key_gives_exit_5_and_a_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    outcome = run(_cfg(tmp_path, Backend.API))

    assert outcome.exit_code is ExitCode.MODEL
    assert (tmp_path / "out" / "report.md").exists()
    assert any(d.code == "LLM-001" and "ANTHROPIC_API_KEY" in d.message for d in outcome.diagnostics)


# ---------------------------------------------------------------- прогон агента через API


def _step_of(system: str) -> str:
    """Шаг по системному промпту запроса: у каждого шага свой файл в ``src/validator/prompts``."""
    for path in sorted(PROMPTS.glob("*_system.md")):
        if system.startswith(path.read_text(encoding="utf-8")[:80]):
            return path.name.removesuffix("_system.md")
    raise AssertionError(f"неизвестный системный промпт: {system[:80]!r}")


def _as_sse(fixture: dict[str, Any], model: str) -> bytes:
    """Ответ фикстуры ``claude -p`` (тот, что берёт FakeLLM) как поток SSE от API."""
    structured = fixture.get("structured_output")
    text = json.dumps(structured, ensure_ascii=False) if structured is not None else fixture["result"]
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_01SyntheticPipeline",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 5000, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        *(
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text[i : i + 500]},
            }
            for i in range(0, len(text), 500)
        ),
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 3000},
        },
        {"type": "message_stop"},
    ]
    return "".join(
        f"event: {e['type']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events
    ).encode()


def test_whole_run_through_the_api_backend_reaches_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counters: dict[str, int] = {}
    seen: list[dict[str, Any]] = []
    lock = threading.Lock()

    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        step = _step_of(body["system"])
        with lock:
            counters[step] = counters.get(step, 0) + 1
            index = counters[step]
            seen.append(body)
        fixture = json.loads((FIXTURES_LLM / step / f"{index}.json").read_text(encoding="utf-8"))
        return stream_response(_as_sse(fixture, body["model"]))

    monkeypatch.setattr("validator.pipeline.make_client", lambda _cfg: (api(handler), []))

    outcome = run(_cfg(tmp_path, Backend.API))

    assert outcome.exit_code is ExitCode.OK, [d.code for d in outcome.diagnostics]
    assert outcome.report is not None
    assert outcome.report.verification
    assert counters["search"] == outcome.report.passport.passes
    assert counters.get("segment") == 1
    with_schema = {_step_of(b["system"]) for b in seen if "format" in b["output_config"]}
    assert "segment" in with_schema and "search" not in with_schema
    log = (tmp_path / "out" / "run" / "calls.jsonl").read_text(encoding="utf-8")
    assert '"backend":"api"' in log.replace(" ", "")
    assert FAKE_KEY not in log
    assert FAKE_KEY not in (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
