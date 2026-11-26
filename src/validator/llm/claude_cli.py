"""Клиент модели через подписку: ``claude -p`` (D-008).

Порт ``call_claude`` из ``experiments/11-baseline-one-prompt/cli.py`` и ``call`` из
``experiments/21-model-effort/run_matrix.py``:

- ``--safe-mode --tools "" --no-session-persistence --output-format json``;
- системный промпт файлом ``--system-prompt-file`` во временной папке вызова: длинный промпт поиска
  с чек-листами не упрётся в предел длины командной строки Windows (32 767 символов);
- модель полным именем и ``--effort`` всегда явно (D-010);
- промпт через stdin в UTF-8, запуск из пустой временной папки вне репозитория, чтобы в контекст
  не попали CLAUDE.md и эталон;
- из окружения убираем ``CLAUDE_EFFORT`` (в облачной сессии он xhigh), ключи API и доп. папки;
- ``--json-schema`` даёт ответ в ``structured_output``; ошибки приходят JSON в stdout
  (``is_error``, ``subtype``, ``api_error_status``), их разбирает ``base.parse_cli_payload``.

Повтор, кеш, лог и лимит цены делает ``calllog.RecordingClient``, выбор бэкенда ``factory``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from validator.errors import LLM_BAD_RESPONSE, LLM_TIMEOUT, LLM_UNAVAILABLE, LLMError
from validator.llm.base import classify_error, parse_cli_payload
from validator.models import Backend, LLMRequest, LLMResponse

logger = logging.getLogger("validator")

DROP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_EFFORT",
    "CLAUDE_ADDITIONAL_DIRECTORIES",
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
)
STDERR_TAIL_CHARS = 2000
SYSTEM_PROMPT_FILE = "system-prompt.md"
AUTH_STATUS_TIMEOUT_S = 15


def clean_env() -> dict[str, str]:
    """Окружение для ``claude -p`` без ключей, effort по умолчанию и дополнительных папок."""
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env["PYTHONUTF8"] = "1"
    return env


def find_claude() -> str | None:
    return shutil.which("claude")


def build_args(exe: str, req: LLMRequest, system_prompt_file: Path) -> list[str]:
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
        "--system-prompt-file",
        str(system_prompt_file),
    ]
    if req.json_schema is not None:
        args += ["--json-schema", json.dumps(req.json_schema, ensure_ascii=False)]
    return args


def login_status(exe: str) -> bool | None:
    """Вошёл ли ``claude`` в аккаунт (``claude auth status``, без вызова модели, доли секунды).

    None, если узнать не удалось (старая версия CLI, таймаут): тогда считаем, что вход есть, и честную
    ошибку покажет первый вызов.
    """
    with tempfile.TemporaryDirectory(prefix="qairu-auth-", ignore_cleanup_errors=True) as cwd:
        try:
            result = subprocess.run(
                [exe, "auth", "status", "--json"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                cwd=cwd,
                env=clean_env(),
                timeout=AUTH_STATUS_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.info("claude auth status не ответил: %s", exc)
            return None
    try:
        data = json.loads(result.stdout.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    logged_in = data.get("loggedIn") if isinstance(data, dict) else None
    return logged_in if isinstance(logged_in, bool) else None


def _not_json_error(req: LLMRequest, returncode: int, stdout: str, stderr: str) -> LLMError:
    """stdout не JSON: CLI упал до ответа. Причину ищем в stderr и stdout (лимит, вход, сеть)."""
    detail = (stderr.strip() or stdout.strip())[:300] or "пустой вывод"
    failure = classify_error(f"{stderr}\n{stdout}")
    if failure != classify_error(""):
        return LLMError(
            f"claude -p завершился с кодом {returncode}: {detail}",
            code=failure.code,
            hint=failure.hint,
            location=req.call_id,
            retryable=failure.retryable,
        )
    return LLMError(
        f"claude -p вернул не JSON (код {returncode}): {detail}",
        code=LLM_BAD_RESPONSE,
        hint="Повторите прогон; если повторяется, обновите Claude Code (claude update) и сообщите команде",
        location=req.call_id,
        retryable=True,
    )


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

    def _run(self, req: LLMRequest) -> subprocess.CompletedProcess[bytes]:
        with tempfile.TemporaryDirectory(prefix="qairu-call-", ignore_cleanup_errors=True) as tmp:
            cwd = Path(tmp)
            system_file = cwd / SYSTEM_PROMPT_FILE
            system_file.write_text(req.system, encoding="utf-8")
            try:
                return subprocess.run(
                    build_args(self.executable, req, system_file),
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
                    hint="Повторите прогон; если повторяется, возьмите профиль quick",
                    location=req.call_id,
                    retryable=True,
                ) from exc
            except OSError as exc:
                raise LLMError(
                    f"Не удалось запустить claude: {exc.strerror}",
                    code=LLM_UNAVAILABLE,
                    hint="Проверьте установку Claude Code: команда claude --version",
                    location=req.call_id,
                ) from exc

    def call(self, req: LLMRequest) -> LLMResponse:
        result = self._run(req)
        stdout = result.stdout.decode("utf-8", errors="replace")
        try:
            payload = json.loads(stdout)
        except ValueError:
            stderr = result.stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]
            raise _not_json_error(req, result.returncode, stdout, stderr) from None
        if result.returncode != 0 and isinstance(payload, dict) and not payload.get("is_error"):
            logger.warning(
                "claude -p вернул код %s при ответе без ошибки (%s)", result.returncode, req.call_id
            )
        return parse_cli_payload(payload, req, self.backend)
