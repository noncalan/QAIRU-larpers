"""Бэкенд ``replay``: ответы модели из папки ``run/`` прошлого прогона, без модели и без ключа.

Так жюри пересобирает отчёт из репозитория: ``validator ВХОД ВЫХОД --backend replay --replay out/run``.
Ответы лежат в ``run/responses/*.json`` (конверты из ``llm.cache``). Поиск ответа:

1. по ключу запроса (шаг, номер вызова, модель, effort, системный промпт, промпт, схема): ответ ровно
   на этот запрос;
2. если ключ не совпал (поменялся промпт, документ или модель), по паре «шаг, номер вызова» с
   предупреждением ``LLM-016``: отчёт пересобран, но ответы получены на другой запрос;
3. если нет и пары, ``LLMError`` ``LLM-015`` с понятным текстом, шаг срабатывает по своей политике отказа.
"""

from __future__ import annotations

import json
import logging
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


class ReplayLLM:
    """Отвечает записанными ответами прошлого прогона."""

    backend = Backend.REPLAY

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = resolve_run_dir(run_dir)
        self._by_key: dict[str, dict[str, Any]] = {}
        self._by_position: dict[tuple[str, int], dict[str, Any]] = {}
        self._recorded: dict[str, dict[int, StepModel]] = {}
        self._logged: dict[str, set[int]] = {}
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

    def calls_in_recording(self, step: LLMStep) -> int:
        """Сколько вызовов шага было в записанном прогоне, считая и упавшие (по наибольшему номеру)."""
        indices = set(self._recorded.get(step.value, {})) | self._logged.get(step.value, set())
        return max(indices, default=0)

    @property
    def responses_total(self) -> int:
        return len(self._by_key)

    def recorded(self, step: LLMStep) -> dict[int, StepModel]:
        """Какие вызовы шага есть в записи: номер вызова → модель и effort, с которыми он делался."""
        return dict(self._recorded.get(step.value, {}))

    def call(self, req: LLMRequest) -> LLMResponse:
        payload = self._by_key.get(req.key)
        if payload is None:
            payload = self._by_position.get((req.step.value, req.call_index))
            if payload is None:
                raise LLMError(
                    f"В записанном прогоне нет ответа для шага {req.step.value}, вызов {req.call_index}",
                    code=LLM_REPLAY_MISSING,
                    hint="Пересоберите прогон с моделью: в записи нет этого вызова",
                    location=req.call_id,
                )
            logger.warning("Replay %s: ответ взят по шагу и номеру, ключ запроса не совпал", req.call_id)
            self.by_position_calls.append(req.call_id)
        return parse_cli_payload(payload, req, self.backend)

    @property
    def diagnostics(self) -> list[Diagnostic]:
        """Предупреждение, если часть ответов найдена не по ключу запроса, а по шагу и номеру."""
        if not self.by_position_calls:
            return []
        shown = ", ".join(self.by_position_calls[:SHOWN_CALL_IDS])
        more = len(self.by_position_calls) - SHOWN_CALL_IDS
        return [
            Diagnostic(
                code=LLM_REPLAY_BY_POSITION,
                level=DiagLevel.WARNING,
                message=(
                    f"Записанные ответы не совпали с запросами по ключу у {len(self.by_position_calls)} "
                    f"вызовов ({shown}{f' и ещё {more}' if more > 0 else ''}): поменялся документ, "
                    "промпт или модель"
                ),
                action="Ответы взяты по шагу и номеру вызова; цитаты всё равно сверены с документом",
                hint="Для точного воспроизведения возьмите ту же версию агента и тот же документ",
                step="llm",
            )
        ]


__all__ = ["RESPONSES_DIR", "ReplayLLM", "resolve_run_dir"]
