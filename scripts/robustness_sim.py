"""Сбои модели без модели: подменяем процесс ``claude``, а не клиента агента.

Агент ходит в модель командой ``claude -p``. Здесь подделан только сам процесс: что он печатает в
stdout, какой код возвращает, зависает ли. Весь путь агента (поиск ``claude``, проверка входа, разбор
ответа, повтор, диагностика, отчёт, код возврата) настоящий. Так тесты и ``scripts/robustness.py``
проверяют ровно то, что увидит человек, когда подписка кончилась или сеть упала.

Тексты ответов взяты из реальных ответов ``claude -p`` и SDK (см. ``tests/test_llm_client.py``).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import types
from collections.abc import Iterator
from dataclasses import dataclass
from unittest import mock

from validator.errors import (
    LLM_BAD_RESPONSE,
    LLM_LIMIT,
    LLM_OVERLOADED,
    LLM_REFUSED,
    LLM_TIMEOUT,
    LLM_UNAVAILABLE,
)


def _error(text: str, **extra: object) -> str:
    return json.dumps(
        {"type": "result", "subtype": "success", "is_error": True, "result": text, **extra},
        ensure_ascii=False,
    )


@dataclass(frozen=True)
class ModelFailure:
    """Как ведёт себя ``claude -p`` в одном сбое; ``code`` это диагностика, которую ждём в отчёте."""

    slug: str
    title: str
    code: str
    present: bool = True
    logged_in: bool = True
    stdout: str = ""
    returncode: int = 0
    hangs: bool = False


FAILURES: tuple[ModelFailure, ...] = (
    ModelFailure(
        "model-not-installed", "Команда claude не найдена, ключа API нет", LLM_UNAVAILABLE, present=False
    ),
    ModelFailure(
        "model-not-logged-in",
        "claude есть, но вход в аккаунт не выполнен",
        LLM_UNAVAILABLE,
        logged_in=False,
        stdout=_error("Not logged in · Please run /login"),
        returncode=1,
    ),
    ModelFailure(
        "model-limit",
        "Лимит подписки исчерпан (ответ claude: «You've hit your 5-hour limit», статус 429)",
        LLM_LIMIT,
        stdout=_error("You've hit your 5-hour limit · resets 2:10am (UTC)", api_error_status=429),
        returncode=1,
    ),
    ModelFailure(
        "model-overloaded",
        "Сервис модели перегружен (HTTP 529), повтор не помог",
        LLM_OVERLOADED,
        stdout=_error("API Error: Overloaded", api_error_status=529),
        returncode=1,
    ),
    ModelFailure("model-timeout", "Модель не ответила за отведённое время", LLM_TIMEOUT, hangs=True),
    ModelFailure(
        "model-invalid-json",
        "claude напечатал не JSON (оборванный вывод)",
        LLM_BAD_RESPONSE,
        stdout='Готово. {"result": "частичный отв',
    ),
    ModelFailure(
        "model-refusal",
        "Модель отказалась отвечать (stop_reason: refusal)",
        LLM_REFUSED,
        stdout=json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "stop_reason": "refusal",
                "result": "",
            }
        ),
    ),
)
FAILURE_BY_SLUG = {f.slug: f for f in FAILURES}


def _fake_subprocess(failure: ModelFailure) -> types.SimpleNamespace:
    """Модуль ``subprocess`` для ``claude_cli``: всё как настоящее, кроме запуска самого ``claude``."""

    def run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if "auth" in args:  # claude auth status --json
            out = json.dumps({"loggedIn": failure.logged_in}).encode()
            return subprocess.CompletedProcess(args, 0, out, b"")
        if failure.hangs:
            raise subprocess.TimeoutExpired(args, float(kwargs.get("timeout") or 1))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(args, failure.returncode, failure.stdout.encode(), b"")

    return types.SimpleNamespace(**{**vars(subprocess), "run": run})


@contextlib.contextmanager
def simulated_claude(failure: ModelFailure) -> Iterator[None]:
    """Внутри блока ``claude -p`` ведёт себя как в ``failure``; пауза перед повтором нулевая."""
    with (
        mock.patch("validator.llm.claude_cli.subprocess", _fake_subprocess(failure)),
        mock.patch(
            "validator.llm.claude_cli.find_claude",
            return_value="/simulated/claude" if failure.present else None,
        ),
        mock.patch("validator.llm.calllog.RETRY_DELAY_S", 0.0),
    ):
        yield


__all__ = ["FAILURES", "FAILURE_BY_SLUG", "ModelFailure", "simulated_claude"]
