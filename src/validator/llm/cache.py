"""Кеш ответов модели на диске и общий формат сохранённого ответа (кеш, ``run/responses/``, replay).

Ключ кеша это ``LLMRequest.key``: sha256 от шага, номера вызова, модели, effort, системного промпта,
промпта и схемы. Любая правка промпта, документа или модели даёт новый ключ, поэтому устаревший ответ
не подхватится; номер вызова в ключе не даёт проходам поиска с одним промптом получить один ответ.
Файл ``.cache/validator/<бэкенд>/<первые 2 знака ключа>/<ключ>.json`` это «конверт»: метаданные вызова и
ответ бэкенда целиком в формате ``claude -p --output-format json`` (``payload``). В кеш попадают только
успешные ответы живого бэкенда; битый файл кеша считается промахом, а не ошибкой прогона.

Кеш у каждого бэкенда свой (ревью Fable, 26.11). Ключ запроса бэкенда не содержит, и раньше прогон
``--backend api`` в папке после прогонов через подписку брал все ответы из кеша подписки: ни одного запроса
в API, а отчёт писал «Claude API». Теперь ``api`` и ``cli`` не видят ответов друг друга. Ключ запроса
не менялся: по нему же ищет replay, и записи e2e и eval остаются в силе. Файлы старой раскладки без папки
бэкенда (``<2 знака>/<ключ>.json``) читаются, только если в конверте тот же бэкенд.

Режимы ``--cache``: ``use`` читает и пишет, ``refresh`` только пишет (перезапросить всё заново),
``off`` не трогает кеш.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from validator.models import Backend, CacheMode, LLMRequest

logger = logging.getLogger("validator")

ENVELOPE_VERSION = 1


def make_envelope(req: LLMRequest, payload: dict[str, Any], backend: Backend) -> dict[str, Any]:
    """Сохранённый ответ: по ``key`` его находит кеш и replay, по ``step`` и ``call_index`` replay."""
    return {
        "envelope_version": ENVELOPE_VERSION,
        "key": req.key,
        "call_id": req.call_id,
        "step": req.step.value,
        "call_index": req.call_index,
        "model": req.model,
        "effort": req.effort.value,
        "backend": backend.value,
        "saved_at": datetime.now(UTC).isoformat(),
        "payload": payload,
    }


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    """Запись через временный файл и ``replace``: прерванный прогон не оставит половину JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=1)
            stream.write("\n")
        tmp.replace(path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


def read_envelope(path: Path) -> dict[str, Any] | None:
    """Конверт из файла или None, если файла нет, он битый или это не наш формат."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Сохранённый ответ %s не читается: %s", path, exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("payload"), dict) or "key" not in data:
        logger.warning("Сохранённый ответ %s не в формате конверта, пропущен", path)
        return None
    return data


class ResponseCache:
    """Кеш ответов в папке ``root`` с режимом ``use``, ``refresh`` или ``off``.

    ``backend``: чей это кеш. Ответы лежат в ``root/<бэкенд>/`` и чужому бэкенду не отдаются. ``None``
    это раскладка без папки бэкенда (тесты и старые записи).
    """

    def __init__(self, root: Path, mode: CacheMode, backend: Backend | None = None) -> None:
        self.root = root
        self.mode = mode
        self.backend = backend

    def path_for(self, key: str) -> Path:
        base = self.root / self.backend.value if self.backend is not None else self.root
        return base / key[:2] / f"{key}.json"

    def _legacy_path(self, key: str) -> Path | None:
        return self.root / key[:2] / f"{key}.json" if self.backend is not None else None

    def get(self, req: LLMRequest) -> dict[str, Any] | None:
        """Ответ из кеша (``payload``) или None. В режимах ``refresh`` и ``off`` всегда None."""
        if self.mode is not CacheMode.USE:
            return None
        envelope = self._read(self.path_for(req.key), req.key)
        legacy = self._legacy_path(req.key)
        if envelope is None and legacy is not None and self.backend is not None:
            old = self._read(legacy, req.key)
            # Старая раскладка общая для всех бэкендов: берём только ответ, полученный этим же бэкендом.
            envelope = old if old is not None and old.get("backend") == self.backend.value else None
        if envelope is None:
            return None
        payload: dict[str, Any] = envelope["payload"]
        return payload

    @staticmethod
    def _read(path: Path, key: str) -> dict[str, Any] | None:
        if not path.is_file():
            return None
        envelope = read_envelope(path)
        return envelope if envelope is not None and envelope.get("key") == key else None

    def put(self, req: LLMRequest, payload: dict[str, Any], backend: Backend) -> Path | None:
        """Сохранить успешный ответ. Ошибка записи кеша не роняет прогон: только строка в лог."""
        if self.mode is CacheMode.OFF:
            return None
        path = self.path_for(req.key)
        try:
            write_json_atomic(path, make_envelope(req, payload, backend))
        except OSError as exc:
            logger.warning("Кеш ответа %s не записан: %s", req.call_id, exc)
            return None
        return path


__all__ = ["ResponseCache", "make_envelope", "read_envelope", "write_json_atomic"]
