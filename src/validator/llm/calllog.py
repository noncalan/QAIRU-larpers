"""Лог вызовов модели: каждая попытка становится строкой ``run/calls.jsonl`` (``CallRecord``).

Минимальная версия каркаса: обёртка над любым клиентом пишет запись и для успеха, и для ошибки.
Сохранение ответов целиком в ``run/responses/``, цену по тарифу и replay добавляет #21.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from validator.errors import LLMError
from validator.llm.base import LLMClient
from validator.models import Backend, CallRecord, CallStatus, LLMRequest, LLMResponse, TokenUsage


class RecordingClient:
    """Пропускает вызовы во внутренний клиент и копит ``CallRecord``."""

    def __init__(self, inner: LLMClient) -> None:
        self.inner = inner
        self.records: list[CallRecord] = []

    @property
    def backend(self) -> Backend:
        return self.inner.backend

    def _record(
        self,
        req: LLMRequest,
        started: datetime,
        elapsed_ms: int,
        resp: LLMResponse | None,
        error: LLMError | None,
    ) -> None:
        status = CallStatus.ERROR if error else (CallStatus.CACHED if resp and resp.cached else CallStatus.OK)
        self.records.append(
            CallRecord(
                call_id=req.call_id,
                step=req.step,
                call_index=req.call_index,
                key=req.key,
                model_requested=req.model,
                model_actual=resp.model if resp else None,
                effort=req.effort,
                backend=self.backend,
                started_at=started,
                duration_ms=(resp.duration_ms if resp and resp.duration_ms is not None else elapsed_ms),
                usage=resp.usage if resp else TokenUsage(),
                cost_usd=resp.cost_usd if resp else None,
                status=status,
                error=f"{error.code}: {error.message}" if error else None,
                prompt_chars=len(req.system) + len(req.prompt),
                response_chars=len(resp.text) if resp else 0,
            )
        )

    def call(self, req: LLMRequest) -> LLMResponse:
        started = datetime.now(UTC)
        clock = time.perf_counter()
        try:
            resp = self.inner.call(req)
        except LLMError as exc:
            self._record(req, started, int((time.perf_counter() - clock) * 1000), None, exc)
            raise
        self._record(req, started, int((time.perf_counter() - clock) * 1000), resp, None)
        return resp


def write_calls_jsonl(path: Path, records: Iterable[CallRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(record.model_dump_json() + "\n")
