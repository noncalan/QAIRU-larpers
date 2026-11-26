"""Паспорт прогона (ТЗ п. 6): версия агента, хеши входа и промптов, окружение, модели, вызовы.

Версия агента: ``git describe --tags --always --dirty`` в репозитории агента; если git нет
(архив, контейнер жюри), переменная ``AGENT_VERSION``; иначе версия пакета с пометкой.
Ключи API и окружение целиком в паспорт не попадают.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
from collections.abc import Sequence
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version
from pathlib import Path

from validator.models import (
    Backend,
    CallRecord,
    CallStatus,
    DocKind,
    ExitCode,
    LLMStep,
    Passport,
    RunConfig,
    TokenUsage,
)

PACKAGE_DIR = Path(__file__).resolve().parent
PROMPTS_DIR = PACKAGE_DIR / "prompts"
AGENT_VERSION_ENV = "AGENT_VERSION"
GIT_TIMEOUT_S = 10


def _git_describe() -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "describe", "--tags", "--always", "--dirty"],
            cwd=PACKAGE_DIR,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = result.stdout.strip()
    return out if result.returncode == 0 and out else None


def agent_version() -> tuple[str, str, bool]:
    """(версия, откуда взята: git | AGENT_VERSION | package, есть ли незакоммиченные правки)."""
    described = _git_describe()
    if described:
        return described, "git", described.endswith("-dirty")
    env = os.environ.get(AGENT_VERSION_ENV)
    if env:
        return env, AGENT_VERSION_ENV, env.endswith("-dirty")
    try:
        return f"{package_version('qairu-validator')} (без git)", "package", False
    except PackageNotFoundError:
        return "unknown", "unknown", False


def prompts_sha256(root: Path = PROMPTS_DIR) -> str:
    """Хеш папки промптов: путь и содержимое каждого файла по порядку. Меняется при любой правке.

    Порядок по строке пути, а не по ``Path``: на Windows ``Path`` сравнивается без учёта регистра,
    ``README.md`` встал бы в середину, и хеш тех же промптов вышел бы другим, чем на Linux и macOS.
    """
    digest = hashlib.sha256()
    if root.is_dir():
        files = {p.relative_to(root).as_posix(): p for p in root.rglob("*") if p.is_file()}
        for name in sorted(files):
            digest.update(name.encode("utf-8") + b"\0")
            digest.update(files[name].read_bytes() + b"\0")
    return digest.hexdigest()


def _models(cfg: RunConfig) -> dict[str, str]:
    """Шаг → «модель · effort». Сверку с ответами автора показываем, только если ответы переданы."""
    return {
        step.value: model.label
        for step, model in cfg.step_models.items()
        if step is not LLMStep.CLARIFY or cfg.answers_path is not None
    }


def _file_sha256(path: Path | None) -> str | None:
    """SHA-256 файла ответов автора для паспорта; None, если файла нет или он не читается."""
    if path is None:
        return None
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _cost(calls: Sequence[CallRecord]) -> float | None:
    costs = [c.cost_usd for c in calls if c.cost_usd is not None]
    return round(sum(costs), 4) if costs else None


def build_passport(
    cfg: RunConfig,
    *,
    document_name: str,
    document_kind: DocKind,
    document_sha256: str | None,
    document_size_bytes: int | None,
    document_pages: int | None,
    started_at: datetime,
    finished_at: datetime | None,
    backend_used: Backend | None,
    calls: Sequence[CallRecord],
    exit_code: ExitCode,
) -> Passport:
    version, source, dirty = agent_version()
    tokens = sum((c.usage for c in calls), TokenUsage())
    return Passport(
        document_name=document_name,
        document_kind=document_kind,
        document_sha256=document_sha256,
        document_size_bytes=document_size_bytes,
        document_pages=document_pages,
        started_at=started_at,
        finished_at=finished_at,
        duration_s=round((finished_at - started_at).total_seconds(), 2) if finished_at else None,
        agent_version=version,
        agent_version_source=source,
        agent_dirty=dirty,
        prompts_sha256=prompts_sha256(),
        os=f"{platform.system()} {platform.release()} ({platform.machine()})",
        python=platform.python_version(),
        backend_requested=cfg.backend,
        backend_used=backend_used,
        profile=cfg.profile,
        passes=cfg.passes,
        models=_models(cfg),
        models_actual=sorted({c.model_actual for c in calls if c.model_actual}),
        # Повторённая попытка не отдельный вызов: считаем вызовы по их последней попытке.
        calls_total=sum(c.status is not CallStatus.RETRIED for c in calls),
        calls_failed=sum(c.status is CallStatus.ERROR for c in calls),
        tokens=tokens,
        cost_usd=_cost(calls),
        exit_code=exit_code,
        answers_file=cfg.answers_path.name if cfg.answers_path is not None else None,
        answers_sha256=_file_sha256(cfg.answers_path),
    )
