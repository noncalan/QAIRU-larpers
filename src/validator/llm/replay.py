"""Бэкенд ``replay``: ответы модели из папки ``run/`` прошлого прогона, без модели и без ключа.

Так жюри пересобирает отчёт из репозитория: ``validator ВХОД ВЫХОД --backend replay --replay out/run``.
Ответы лежат в ``run/responses/*.json`` (конверты из ``llm.cache``). Поиск ответа:

1. по ключу запроса (шаг, номер вызова, модель, effort, системный промпт, промпт, схема): ответ ровно
   на этот запрос;
2. ключ не совпал: в записи нет ответа на этот запрос, ``LLMError`` ``LLM-015``, шаг срабатывает по своей
   политике отказа (отчёт честно неполный, код 5). Ключ не совпадает, когда подан другой документ, другие
   ответы автора (``--answers``) или промпт агента изменился после записи (#132): подставлять ответ про
   чужой вход нельзя, отчёт выглядел бы настоящим;
3. только для разработки и тестов (``--replay-by-position``, ``by_position=True``): ответ по паре «шаг,
   номер вызова» с предупреждением ``LLM-016``. Так проигрывается запись, отставшая от промптов.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from validator.errors import LLM_REPLAY_BY_POSITION, LLM_REPLAY_MISSING, LLMError
from validator.llm.base import parse_cli_payload
from validator.llm.cache import read_envelope
from validator.models import (
    Backend,
    DiagLevel,
    Diagnostic,
    Effort,
    LLMRequest,
    LLMResponse,
    LLMStep,
    ReplaySource,
    StepModel,
)

logger = logging.getLogger("validator")

RESPONSES_DIR = "responses"
CALLS_LOG = "calls.jsonl"
SHOWN_CALL_IDS = 10


def resolve_run_dir(path: Path) -> Path:
    """Папка с ``responses/``: можно передать и ``out/run``, и саму папку отчёта ``out``."""
    if (path / RESPONSES_DIR).is_dir():
        return path
    if (path / "run" / RESPONSES_DIR).is_dir():
        return path / "run"
    return path


def _shown_path(path: Path) -> str:
    """Путь к записи для паспорта: от рабочей папки, чтобы отчёт не выдавал домашний каталог машины."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


class ReplayLLM:
    """Отвечает записанными ответами прошлого прогона."""

    backend = Backend.REPLAY

    def __init__(self, run_dir: Path, *, by_position: bool = False) -> None:
        self.run_dir = resolve_run_dir(run_dir)
        self.by_position = by_position
        self._by_key: dict[str, dict[str, Any]] = {}
        self._by_position: dict[tuple[str, int], dict[str, Any]] = {}
        self._recorded: dict[str, dict[int, StepModel]] = {}
        self._logged: dict[str, set[int]] = {}
        self._spans: list[tuple[datetime, datetime]] = []
        self._timing: dict[str, list[tuple[str, datetime, datetime]]] = {}
        self._served: set[str] = set()
        self._backends: set[Backend] = set()
        self.by_position_calls: list[str] = []
        self._index()
        self._read_log()
        if not self._by_key:
            raise LLMError(
                f"В папке {run_dir} нет записанных ответов модели ({RESPONSES_DIR}/*.json)",
                code=LLM_REPLAY_MISSING,
                hint="Укажите в --replay папку run/ прогона, который делался с моделью (например, out/run)",
            )

    def _index(self) -> None:
        folder = self.run_dir / RESPONSES_DIR
        if not folder.is_dir():
            return
        for path in sorted(folder.glob("*.json")):
            envelope = read_envelope(path)
            if envelope is None:
                continue
            payload: dict[str, Any] = envelope["payload"]
            self._by_key[str(envelope["key"])] = payload
            step, index = envelope.get("step"), envelope.get("call_index")
            if isinstance(step, str) and isinstance(index, int):
                self._by_position[(step, index)] = payload
                model, effort = envelope.get("model"), envelope.get("effort")
                if isinstance(model, str) and isinstance(effort, str) and effort in {e.value for e in Effort}:
                    self._recorded.setdefault(step, {})[index] = StepModel(model=model, effort=Effort(effort))

    def _read_log(self) -> None:
        """Номера вызовов из ``calls.jsonl``: там есть и упавшие вызовы, которых нет в ``responses/``."""
        path = self.run_dir / CALLS_LOG
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            step, index = (
                (record.get("step"), record.get("call_index")) if isinstance(record, dict) else (None, None)
            )
            if isinstance(step, str) and isinstance(index, int):
                self._logged.setdefault(step, set()).add(index)
            if isinstance(record, dict):
                self._remember_call(record)

    def _remember_call(self, record: dict[str, Any]) -> None:
        """Когда и чем вызывали модель при записи: для паспорта (``source``)."""
        try:
            start = datetime.fromisoformat(str(record.get("started_at")))
        except ValueError:
            return
        duration = record.get("duration_ms")
        end = start + timedelta(milliseconds=duration if isinstance(duration, int | float) else 0)
        self._spans.append((start, end))
        key, step = record.get("key"), record.get("step")
        if isinstance(key, str) and isinstance(step, str):
            self._timing.setdefault(key, []).append((step, start, end))
        backend = record.get("backend")
        if backend in {b.value for b in Backend} and backend != Backend.REPLAY.value:
            self._backends.add(Backend(backend))

    def source(self) -> ReplaySource:
        """Что за запись: папка, сколько в ней ответов, когда и чем её писали (по ``calls.jsonl``)."""
        return ReplaySource(
            path=_shown_path(self.run_dir),
            responses=self.responses_total,
            recorded_from=min((s for s, _ in self._spans), default=None),
            recorded_to=max((e for _, e in self._spans), default=None),
            backends=sorted(self._backends, key=lambda b: b.value),
            model_seconds=self._model_seconds(),
        )

    def _model_seconds(self) -> float | None:
        """Время модели при записи на взятых ответах: шаг от начала первого до конца последнего вызова.

        Вызовы одного шага (пачки попытки теста) идут параллельно, поэтому шаг считается отрезком, а не
        суммой вызовов: так же считают время модели в README отчёта на сдачу. ``None``, пока ответов не брали.
        """
        steps: dict[str, tuple[datetime, datetime]] = {}
        for key in self._served:
            for step, start, end in self._timing.get(key, []):
                first, last = steps.get(step, (start, end))
                steps[step] = (min(first, start), max(last, end))
        if not steps:
            return None
        return round(sum((end - start).total_seconds() for start, end in steps.values()), 1)

    def calls_in_recording(self, step: LLMStep) -> int:
        """Сколько вызовов шага было в записанном прогоне, считая и упавшие (по наибольшему номеру)."""
        indices = set(self._recorded.get(step.value, {})) | self._logged.get(step.value, set())
        return max(indices, default=0)

    @property
    def responses_total(self) -> int:
        return len(self._by_key)

    def has_key(self, key: str) -> bool:
        """Есть ли в записи ответ ровно на этот запрос (по ключу, а не по номеру вызова)."""
        return key in self._by_key

    def recorded(self, step: LLMStep) -> dict[int, StepModel]:
        """Какие вызовы шага есть в записи: номер вызова → модель и effort, с которыми он делался."""
        return dict(self._recorded.get(step.value, {}))

    def call(self, req: LLMRequest) -> LLMResponse:
        payload = self._by_key.get(req.key)
        if payload is None:
            positioned = self._by_position.get((req.step.value, req.call_index))
            if positioned is not None and not self.by_position:
                raise LLMError(
                    f"В записи нет ответа на этот запрос ({req.call_id}): запись сделана для другого "
                    "документа, других ответов автора или другой версии промпта",
                    code=LLM_REPLAY_MISSING,
                    hint=(
                        "Replay воспроизводит только тот документ и те ответы, с которыми сделана запись; "
                        "для своего документа нужна модель: --backend cli или --backend api"
                    ),
                    location=req.call_id,
                )
            payload = positioned
            if payload is None:
                raise LLMError(
                    f"В записанном прогоне нет ответа для шага {req.step.value}, вызов {req.call_index}",
                    code=LLM_REPLAY_MISSING,
                    hint="Пересоберите прогон с моделью: в записи нет этого вызова",
                    location=req.call_id,
                )
            logger.warning("Replay %s: ответ взят по шагу и номеру, ключ запроса не совпал", req.call_id)
            self.by_position_calls.append(req.call_id)
        else:
            self._served.add(req.key)
        return parse_cli_payload(payload, req, self.backend)

    @property
    def diagnostics(self) -> list[Diagnostic]:
        """Предупреждение, если часть ответов найдена не по ключу запроса, а по шагу и номеру."""
        if not self.by_position_calls:
            return []
        shown = ", ".join(sorted(self.by_position_calls, key=_call_order)[:SHOWN_CALL_IDS])
        more = len(self.by_position_calls) - SHOWN_CALL_IDS
        return [
            Diagnostic(
                code=LLM_REPLAY_BY_POSITION,
                level=DiagLevel.WARNING,
                message=(
                    f"Записанные ответы не совпали с запросами по ключу у {len(self.by_position_calls)} "
                    f"вызовов ({shown}{f' и ещё {more}' if more > 0 else ''}): поменялся документ, "
                    "ответы автора, промпт или модель"
                ),
                action=(
                    "Ответы взяты по шагу и номеру вызова (--replay-by-position, только для разработки); "
                    "цитаты всё равно сверены с документом"
                ),
                hint="Для точного воспроизведения возьмите ту же версию агента и тот же документ",
                step="llm",
            )
        ]


_STEP_ORDER = {step.value: n for n, step in enumerate(LLMStep)}


def _call_order(call_id: str) -> tuple[int, int]:
    """Шаги по порядку конвейера, вызовы шага по номеру. Проходы поиска идут параллельно, и порядок,
    в котором они доходят до replay, от запуска к запуску разный, а отчёт из одной записи один."""
    step, _, index = call_id.rpartition(".")
    return _STEP_ORDER.get(step, len(_STEP_ORDER)), int(index) if index.isdigit() else 0


__all__ = ["RESPONSES_DIR", "ReplayLLM", "resolve_run_dir"]
