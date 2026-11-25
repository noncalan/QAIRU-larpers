"""Выбор бэкенда модели (минимальная версия каркаса).

- ``fake``: фикстуры тестов;
- ``cli``: подписка ``claude -p``;
- ``auto``: ключ API есть, идём через SDK (#32, пока откат на подписку с пометкой); иначе подписка,
  если ``claude`` установлен; иначе отчёт с диагностикой ``LLM-001``;
- ``api`` и ``replay`` появятся в #32 и #21, до них понятная диагностика.

Если модели нет, конвейер получает ``UnavailableLLM``: каждый вызов даёт ``LLMError``, и шаги
срабатывают по своей политике отказа, а отчёт всё равно пишется.
"""

from __future__ import annotations

from validator.errors import CFG_NOT_IMPLEMENTED, LLM_UNAVAILABLE, LLMError
from validator.llm.base import LLMClient
from validator.llm.claude_cli import ClaudeCLI
from validator.llm.fake import FakeLLM
from validator.models import Backend, DiagLevel, Diagnostic, LLMRequest, LLMResponse, RunConfig

_PENDING = {
    Backend.API: "Бэкенд api (SDK anthropic) ещё не реализован, задача #32",
    Backend.REPLAY: "Бэкенд replay (пересборка по записанным ответам) ещё не реализован, задача #21",
}


class UnavailableLLM:
    """Модели нет: каждый вызов даёт ``LLMError`` с причиной, почему её нет."""

    def __init__(self, backend: Backend, reason: LLMError) -> None:
        self._backend = backend
        self.reason = reason

    @property
    def backend(self) -> Backend:
        return self._backend

    def call(self, req: LLMRequest) -> LLMResponse:
        raise LLMError(
            self.reason.message, code=self.reason.code, hint=self.reason.hint, location=req.call_id
        )


def _unavailable(backend: Backend, error: LLMError) -> tuple[LLMClient, list[Diagnostic]]:
    diag = error.to_diagnostic(action="Шаги с моделью пропущены, отчёт собран без них", step="llm")
    return UnavailableLLM(backend, error), [diag]


def _subscription() -> tuple[LLMClient, list[Diagnostic]]:
    try:
        return ClaudeCLI(), []
    except LLMError as exc:
        return _unavailable(Backend.CLI, exc)


def make_client(cfg: RunConfig) -> tuple[LLMClient, list[Diagnostic]]:
    """Клиент модели для прогона и диагностика выбора (что выбрано и почему)."""
    if cfg.backend is Backend.FAKE:
        return FakeLLM(cfg.fake_fixtures), []
    if cfg.backend in _PENDING:
        error = LLMError(
            _PENDING[cfg.backend],
            code=CFG_NOT_IMPLEMENTED,
            hint="Запустите с --backend cli (подписка Claude) или --backend auto",
        )
        return _unavailable(cfg.backend, error)
    if cfg.backend is Backend.CLI:
        return _subscription()
    client, diagnostics = _subscription()
    if cfg.has_api_key:
        diagnostics.insert(
            0,
            Diagnostic(
                code=CFG_NOT_IMPLEMENTED,
                level=DiagLevel.INFO,
                message="Найден ANTHROPIC_API_KEY, но бэкенд api ещё не реализован (#32)",
                action="Модель вызывается через подписку claude -p",
                step="llm",
            ),
        )
    if isinstance(client, UnavailableLLM):
        if cfg.has_api_key:
            error = LLMError(
                "Нет доступа к модели: ANTHROPIC_API_KEY найден, но бэкенд api ещё не готов (#32), "
                "а команда claude не найдена",
                code=LLM_UNAVAILABLE,
                hint="Установите Claude Code и выполните вход (claude, затем /login)",
            )
        else:
            error = LLMError(
                "Нет доступа к модели: нет ключа ANTHROPIC_API_KEY и не найден вход в Claude Code",
                code=LLM_UNAVAILABLE,
                hint=(
                    "Установите Claude Code и выполните вход, "
                    "или задайте ANTHROPIC_API_KEY в .env (см. README)"
                ),
            )
        unavailable, missing = _unavailable(Backend.AUTO, error)
        return unavailable, [d for d in diagnostics if d.level is DiagLevel.INFO] + missing
    return client, diagnostics
