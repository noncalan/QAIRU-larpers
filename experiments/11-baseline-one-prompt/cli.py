"""Shared process, cache and metrics handling for experiment 11."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack
from datetime import datetime, timezone
from functools import cache
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "out" / "11"
DOCUMENT = ROOT / "data" / "input" / "requirements.md"
SYSTEM_PROMPT = "Ты аналитик требований к программному обеспечению. Отвечай на русском языке."
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
_version_lock = Lock()


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def clean_env(overrides: dict | None = None) -> tuple[dict, dict]:
    env = os.environ.copy()
    present = {key: env.pop(key, None) is not None for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    for key, value in (overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env["PYTHONUTF8"] = "1"
    return env, present


def executable() -> str:
    exe = shutil.which("claude")
    if exe is None:
        raise FileNotFoundError("Claude Code CLI was not found on PATH")
    return exe


def clean_directory() -> tempfile.TemporaryDirectory:
    if Path(tempfile.gettempdir()).resolve().is_relative_to(ROOT):
        raise RuntimeError("Set TEMP to a directory outside the repository")
    return tempfile.TemporaryDirectory(prefix="qairu-clean-", ignore_cleanup_errors=True)


def cli_info(exe: str, flag: str, cwd: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([exe, flag], stdin=subprocess.DEVNULL, capture_output=True,
                          cwd=cwd, env=env, timeout=30, encoding="utf-8", errors="replace")


@cache
def _version(exe: str) -> str:
    with clean_directory() as cwd:
        result = cli_info(exe, "--version", cwd, clean_env()[0])
    result.check_returncode()
    return result.stdout.strip()


def run_cached(name: str, build_args: Callable[[str, ExitStack], list[str]],
               stdin_text: str | None, env_overrides: dict | None = None, timeout: int = 900) -> dict:
    """build_args may register temporary input files with stack before the call."""
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    if path.exists():
        return {**json.loads(path.read_text(encoding="utf-8")), "cached": True}

    with ExitStack() as stack:
        exe = executable()
        cwd = stack.enter_context(clean_directory())
        args = build_args(exe, stack)
        env, present = clean_env(env_overrides)
        with _version_lock:
            version = _version(exe)

        # An interrupted writer leaves a marker so a retry cannot spend twice.
        pending = path.with_suffix(".pending")
        try:
            pending.touch(exist_ok=False)
        except FileExistsError:
            if path.exists():
                return {**json.loads(path.read_text(encoding="utf-8")), "cached": True}
            raise RuntimeError(f"Call in progress or interrupted: {pending}") from None
        if path.exists():
            pending.unlink()
            return {**json.loads(path.read_text(encoding="utf-8")), "cached": True}

        started = datetime.now(timezone.utc)
        clock = time.perf_counter()
        failure = None
        try:
            result = subprocess.run(args, input=stdin_text.encode("utf-8") if stdin_text is not None else None,
                                    capture_output=True, cwd=cwd, env=env, timeout=timeout)
            stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, returncode = exc.stdout or b"", exc.stderr or b"", None
            failure = f"Timed out after {timeout} seconds"
        except OSError as exc:
            stdout, stderr, returncode = b"", b"", None
            failure = str(exc)
        elapsed = time.perf_counter() - clock
        ended = datetime.now(timezone.utc)
        stdout = stdout.decode("utf-8", errors="replace")
        stderr = stderr.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(stdout)
        except ValueError:
            parsed = None
        record = {
            "name": name, "exe": exe, "args": args, "returncode": returncode,
            "started_at": started.isoformat(), "ended_at": ended.isoformat(), "wall_s": elapsed,
            "claude_version": version, "auth_env_present": present, "cached": False,
            "stdout_raw": stdout, "stderr_tail": stderr[-2000:], "parsed": parsed,
            "stdout_has_replacement": "\ufffd" in stdout, "stderr_has_replacement": "\ufffd" in stderr,
            "process_error": failure,
        }
        if name == "b5_no_login":
            record["stderr_raw"] = stderr
        save_json(path, record)
        pending.unlink()
        return record


def call_claude(name: str, stdin_text: str, system_prompt: str, extra_args: Sequence[str] = (),
                safe_mode: bool = True, env_overrides: dict | None = None, timeout: int = 900) -> dict:
    def build_args(exe: str, stack: ExitStack) -> list[str]:
        args = [exe, "-p", "--model", "opus", "--tools", "", "--system-prompt", system_prompt,
                "--no-session-persistence", "--output-format", "json"]
        if safe_mode:
            args.append("--safe-mode")
        return [*args, *extra_args]

    return run_cached(name, build_args, stdin_text, env_overrides, timeout)


def payload(rec: dict) -> dict:
    parsed = rec.get("parsed")
    return parsed if isinstance(parsed, dict) else {}


def answer_text(rec: dict) -> str:
    result = payload(rec).get("result")
    return result if isinstance(result, str) else ""


def metrics(rec: dict) -> dict:
    parsed = payload(rec)
    usage = parsed.get("usage")
    models = parsed.get("modelUsage")
    return {
        **{key: parsed.get(key) for key in
           ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd", "is_error", "subtype")},
        "answer_chars": len(answer_text(rec)),
        "usage": {key: usage.get(key) if isinstance(usage, dict) else None for key in TOKEN_FIELDS},
        "models": list(models) if isinstance(models, dict) else [],
    }
