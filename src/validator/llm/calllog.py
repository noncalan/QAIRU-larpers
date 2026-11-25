"""Лог вызовов модели и всё, что нужно воспроизводимому прогону: кеш, повтор, лимит цены, ответы на диске.

``RecordingClient`` оборачивает любой бэкенд (``LLMClient``) и для каждого вызова:

1. смотрит кеш (``--cache use``): попадание отвечает без модели, статус ``cached``;
2. проверяет лимит ``--max-budget-usd`` перед живым вызовом: потрачено по тарифу уже не меньше лимита,
   значит ``LLMError`` ``LLM-003`` без вызова (лимит может быть превышен на вызовы, которые уже шли:
   проходы поиска идут параллельно, до трёх одновременно);
3. вызывает бэкенд; на временной ошибке живого бэкенда (таймаут, перегрузка, ответ не по схеме)
   повторяет один раз после паузы;
4. пишет каждую попытку строкой ``run/calls.jsonl`` (``CallRecord``): цена по тарифу, токены, статус,
   ошибка, путь к ответу; успешный ответ целиком кладёт в ``run/responses/`` (для replay) и в кеш.

Класс потокобезопасен: проходы поиска могут идти параллельно.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from validator.errors import LLM_BUDGET, LLMError
from validator.llm.base import LLMClient, parse_cli_payload, to_payload
from validator.llm.cache import ResponseCache, make_envelope, write_json_atomic
from validator.llm.pricing import response_cost
from validator.models import (
    Backend,
    CallRecord,
    CallStatus,
    DiagLevel,
    Diagnostic,
    LLMRequest,
    LLMResponse,
    TokenUsage,
)

logger = logging.getLogger("validator")

MAX_ATTEMPTS = 2
RETRY_DELAY_S = 20.0
"""Пауза перед повтором: перегрузка и обрывы сети обычно проходят за секунды."""

SHOWN_CALL_IDS = 10
LIVE_BACKENDS = frozenset({Backend.CLI, Backend.API})
"""Бэкенды, которые тратят лимит: только их ответы идут в кеш и в счёт ``--max-budget-usd``."""
_FREE_STATUSES = frozenset({CallStatus.CACHED, CallStatus.REPLAYED})


def response_file_name(req: LLMRequest) -> str:
    """Имя ответа в ``run/responses/``: шаг, номер и начало ключа, чтобы два разных запроса не слиплись."""
    return f"{req.call_id}-{req.key[:12]}.json"


class RecordingClient:
    """Пропускает вызовы во внутренний клиент, ведёт ``CallRecord`` и бережёт лимит."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        cache: ResponseCache | None = None,
        responses_dir: Path | None = None,
        max_budget_usd: float | None = None,
        retry_delay_s: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        summarize_failures: bool = True,
        notes: list[Diagnostic] | None = None,
    ) -> None:
        self.inner = inner
        self.cache = cache
        self.responses_dir = responses_dir
        self.max_budget_usd = max_budget_usd
        self.retry_delay_s = RETRY_DELAY_S if retry_delay_s is None else retry_delay_s
        self._sleep = sleep
        self.summarize_failures = summarize_failures
        self.notes = list(notes or [])
        self._lock = threading.Lock()
        self.records: list[CallRecord] = []
        self._failures: dict[str, list[tuple[str, LLMError]]] = {}

    @property
    def backend(self) -> Backend:
        return self.inner.backend

    @property
    def diagnostics(self) -> list[Diagnostic]:
        """Итог по сбоям модели (по строке на код ошибки) и предупреждения бэкенда за прогон.

        Шаг сам решает, что делать со сбоем, и пишет свою диагностику; здесь причина с исходным кодом
        (лимит, вход, таймаут, лимит цены) и подсказкой человеку, чтобы её было видно в отчёте всегда.
        """
        found = getattr(self.inner, "diagnostics", None)
        backend_diags = list(found) if isinstance(found, list) else []
        with self._lock:
            failures = {code: list(items) for code, items in self._failures.items()}
        return self.notes + [_failure_diagnostic(items) for items in failures.values()] + backend_diags

    def spent_usd(self) -> float:
        """Сколько стоили живые вызовы этого прогона по тарифу (кеш и replay бесплатны).

        Считаются и упавшие попытки с ценой: ответ, который стал ошибкой, всё равно оплачен.
        """
        with self._lock:
            return sum(
                r.cost_usd or 0.0
                for r in self.records
                if r.status not in _FREE_STATUSES and r.backend in LIVE_BACKENDS
            )

    # ------------------------------------------------------------ вызов

    def call(self, req: LLMRequest) -> LLMResponse:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                return self._attempt(req, attempt)
            except LLMError as exc:
                # Фикстуры и replay отвечают одинаково: повтор там только потерял бы время.
                if not exc.retryable or attempt == MAX_ATTEMPTS or self.backend not in LIVE_BACKENDS:
                    self._remember_failure(req, exc)
                    raise
                self._mark_retried(req, attempt)
                logger.warning(
                    "Вызов %s, попытка %d: %s (%s); повтор через %.0f с",
                    req.call_id,
                    attempt,
                    exc.message,
                    exc.code,
                    self.retry_delay_s,
                )
                self._sleep(self.retry_delay_s)
        raise AssertionError("недостижимо: цикл попыток всегда возвращает ответ или бросает ошибку")

    def _mark_retried(self, req: LLMRequest, attempt: int) -> None:
        """Упавшая попытка, за которой будет повтор: статус ``retried``, в число сбоев она не идёт."""
        with self._lock:
            for i in range(len(self.records) - 1, -1, -1):
                rec = self.records[i]
                if rec.call_id == req.call_id and rec.attempt == attempt and rec.status is CallStatus.ERROR:
                    self.records[i] = rec.model_copy(update={"status": CallStatus.RETRIED})
                    return

    def _remember_failure(self, req: LLMRequest, exc: LLMError) -> None:
        if not self.summarize_failures:
            return
        with self._lock:
            self._failures.setdefault(exc.code, []).append((req.call_id, exc))

    def _attempt(self, req: LLMRequest, attempt: int) -> LLMResponse:
        started = datetime.now(UTC)
        clock = time.perf_counter()
        try:
            resp = self._from_cache(req)
            if resp is None:
                self._check_budget(req)
                resp = self.inner.call(req)
                if self.cache is not None and resp.backend in LIVE_BACKENDS:
                    self.cache.put(req, to_payload(resp), resp.backend)
        except LLMError as exc:
            self._record(req, attempt, started, clock, None, exc)
            raise
        self._record(req, attempt, started, clock, resp, None)
        return resp

    def _from_cache(self, req: LLMRequest) -> LLMResponse | None:
        if self.cache is None:
            return None
        payload = self.cache.get(req)
        if payload is None:
            return None
        try:
            return parse_cli_payload(payload, req, self.backend, cached=True)
        except LLMError as exc:  # испорченный или устаревший ответ в кеше: просто идём к модели
            logger.warning("Ответ %s в кеше не годится (%s), вызываем модель", req.call_id, exc.message)
            return None

    def _check_budget(self, req: LLMRequest) -> None:
        if self.max_budget_usd is None or self.backend not in LIVE_BACKENDS:
            return
        spent = self.spent_usd()
        if spent >= self.max_budget_usd:
            raise LLMError(
                f"Лимит цены прогона ${self.max_budget_usd:.2f} исчерпан: потрачено ${spent:.2f} "
                "по тарифу API, вызов не сделан",
                code=LLM_BUDGET,
                hint="Увеличьте --max-budget-usd, возьмите профиль quick или пересоберите из кеша",
                location=req.call_id,
            )

    # ------------------------------------------------------------ лог

    def _status(self, resp: LLMResponse | None, error: LLMError | None) -> CallStatus:
        if error is not None or resp is None:
            return CallStatus.ERROR
        if resp.cached:
            return CallStatus.CACHED
        if resp.backend is Backend.REPLAY:
            return CallStatus.REPLAYED
        return CallStatus.OK

    def _save_response(self, req: LLMRequest, resp: LLMResponse) -> str | None:
        """Ответ целиком в ``run/responses/``; путь относительно папки ``run/``."""
        if self.responses_dir is None:
            return None
        path = self.responses_dir / response_file_name(req)
        try:
            write_json_atomic(path, make_envelope(req, to_payload(resp), resp.backend))
        except OSError as exc:
            logger.warning("Ответ %s не сохранён в %s: %s", req.call_id, path, exc)
            return None
        return f"{self.responses_dir.name}/{path.name}"

    def _record(
        self,
        req: LLMRequest,
        attempt: int,
        started: datetime,
        clock: float,
        resp: LLMResponse | None,
        error: LLMError | None,
    ) -> None:
        elapsed_ms = int((time.perf_counter() - clock) * 1000)
        status = self._status(resp, error)
        live = status is CallStatus.OK and resp is not None and resp.duration_ms is not None
        # Ответ, ставший ошибкой (обрезан, отказ, не по схеме), оплачен: считаем его токены и цену.
        billed = resp if resp is not None else (error.spent if error is not None else None)
        tariff = response_cost(billed) if billed is not None else None
        record = CallRecord(
            call_id=req.call_id,
            step=req.step,
            call_index=req.call_index,
            key=req.key,
            model_requested=req.model,
            model_actual=billed.model if billed else None,
            effort=req.effort,
            backend=self.backend,
            started_at=started,
            duration_ms=resp.duration_ms if live and resp is not None else elapsed_ms,
            usage=billed.usage if billed else TokenUsage(),
            cost_usd=tariff if tariff is not None else (billed.cost_usd if billed else None),
            cost_reported_usd=billed.cost_usd if billed else None,
            status=status,
            error=f"{error.code}: {error.message}" if error else None,
            response_path=self._save_response(req, resp) if resp is not None else None,
            prompt_chars=len(req.system) + len(req.prompt),
            response_chars=len(resp.text) if resp else 0,
            attempt=attempt,
        )
        with self._lock:
            self.records.append(record)


def _failure_diagnostic(items: list[tuple[str, LLMError]]) -> Diagnostic:
    call_ids = [call_id for call_id, _ in items]
    shown = ", ".join(call_ids[:SHOWN_CALL_IDS]) + (" и др." if len(call_ids) > SHOWN_CALL_IDS else "")
    first = items[0][1]
    return Diagnostic(
        code=first.code,
        level=DiagLevel.WARNING,
        message=f"Вызовов модели не удалось: {len(call_ids)} ({shown}). Причина: {first.message}",
        action="Шаги с этими вызовами сработали по своей политике отказа (см. диагностику шагов)",
        hint=first.hint,
        step="llm",
    )


def clear_responses(responses_dir: Path) -> None:
    """Убирает ответы прошлого прогона из ``run/responses/``, чтобы replay не подхватил чужое."""
    if not responses_dir.is_dir():
        return
    for path in responses_dir.glob("*.json"):
        with contextlib.suppress(OSError):
            path.unlink()


def write_calls_jsonl(path: Path, records: Iterable[CallRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(record.model_dump_json() + "\n")


def read_calls_jsonl(path: Path) -> list[CallRecord]:
    """Строки ``calls.jsonl`` как ``CallRecord`` (для тестов, eval и проверки replay)."""
    lines: list[Any] = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [CallRecord.model_validate_json(line) for line in lines]


__all__ = [
    "LIVE_BACKENDS",
    "MAX_ATTEMPTS",
    "RecordingClient",
    "clear_responses",
    "read_calls_jsonl",
    "response_file_name",
    "write_calls_jsonl",
]
