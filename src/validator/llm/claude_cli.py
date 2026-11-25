"""Клиент модели через подписку: ``claude -p`` (D-008), минимальная версия каркаса.

Порт ``call_claude`` из ``experiments/11-baseline-one-prompt/cli.py`` и ``call`` из
``experiments/21-model-effort/run_matrix.py``:

- ``--safe-mode --tools "" --no-session-persistence --output-format json``, свой ``--system-prompt``;
- модель полным именем и ``--effort`` всегда явно (D-010);
- промпт через stdin в UTF-8, запуск из пустой временной папки вне репозитория, чтобы в контекст
  не попали CLAUDE.md и эталон;
- из окружения убираем ``CLAUDE_EFFORT`` (в облачной сессии он xhigh), ключи API и доп. папки;
- ``--json-schema`` даёт ответ в ``structured_output``; ошибки приходят JSON в stdout.

Кеш, лог вызовов, replay, ретраи, ``--max-budget-usd`` делает #21.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from validator.errors import LLM_BAD_RESPONSE, LLM_TIMEOUT, LLM_UNAVAILABLE, LLMError
from validator.llm.base import parse_cli_payload
from validator.models import Backend, LLMRequest, LLMResponse

DROP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_EFFORT",
    "CLAUDE_ADDITIONAL_DIRECTORIES",
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
)
STDERR_TAIL_CHARS = 2000


def clean_env() -> dict[str, str]:
    """Окружение для ``claude -p`` без ключей, effort по умолчанию и дополнительных папок."""
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env["PYTHONUTF8"] = "1"
    return env


def find_claude() -> str | None:
    return shutil.which("claude")


def build_args(exe: str, req: LLMRequest) -> list[str]:
    args = [
        exe,
        "-p",
        "--safe-mode",
        "--tools",
        "",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--model",
        req.model,
        "--effort",
        req.effort.value,
        "--system-prompt",
        req.system,
    ]
    if req.json_schema is not None:
        args += ["--json-schema", json.dumps(req.json_schema, ensure_ascii=False)]
    return args


class ClaudeCLI:
    """Вызов модели через установленный и залогиненный Claude Code."""

    backend = Backend.CLI

    def __init__(self, executable: str | None = None) -> None:
        exe = executable or find_claude()
        if exe is None:
            raise LLMError(
                "Команда claude не найдена",
                code=LLM_UNAVAILABLE,
                hint="Установите Claude Code и выполните вход, или задайте ANTHROPIC_API_KEY в .env",
            )
        self.executable = exe

    def call(self, req: LLMRequest) -> LLMResponse:
        args = build_args(self.executable, req)
        with tempfile.TemporaryDirectory(prefix="qairu-call-", ignore_cleanup_errors=True) as cwd:
            try:
                result = subprocess.run(
                    args,
                    input=req.prompt.encode("utf-8"),
                    capture_output=True,
                    cwd=cwd,
                    env=clean_env(),
                    timeout=req.timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise LLMError(
                    f"Модель не ответила за {req.timeout_s} с",
                    code=LLM_TIMEOUT,
                    location=req.call_id,
                    retryable=True,
                ) from exc
            except OSError as exc:
                raise LLMError(
                    f"Не удалось запустить claude: {exc.strerror}",
                    code=LLM_UNAVAILABLE,
                    location=req.call_id,
                ) from exc
        stdout = result.stdout.decode("utf-8", errors="replace")
        try:
            payload = json.loads(stdout)
        except ValueError:
            stderr = result.stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]
            raise LLMError(
                f"claude -p вернул не JSON (код {result.returncode}): {stderr.strip()[:300] or stdout[:300]}",
                code=LLM_BAD_RESPONSE,
                location=req.call_id,
                retryable=True,
            ) from None
        return parse_cli_payload(payload, req, self.backend)
