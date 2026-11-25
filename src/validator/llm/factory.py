"""Выбор бэкенда модели и сборка клиента прогона (кеш, лог, лимит цены).

- ``fake``: фикстуры тестов;
- ``cli``: подписка ``claude -p``;
- ``replay``: записанные ответы из ``--replay ПАПКА`` (папка ``run/`` прошлого прогона);
- ``auto``: подписка, если ``claude`` установлен и вошёл в аккаунт; иначе SDK, если есть ключ
  ``ANTHROPIC_API_KEY``; иначе replay из ``--replay``; иначе отчёт с ``LLM-001``;
- ``api``: только SDK ``anthropic`` (#32); без ключа понятная диагностика ``LLM-001`` и код 5.

Если модели нет, конвейер получает ``UnavailableLLM``: каждый вызов даёт ``LLMError``, и шаги
срабатывают по своей политике отказа, а отчёт всё равно пишется. Ключ API нигде не печатается:
в ``RunConfig`` есть только признак ``has_api_key``.
"""

from __future__ import annotations

from pathlib import Path

from validator.errors import (
    CFG_BACKEND_UNAVAILABLE,
    CFG_NOT_IMPLEMENTED,
    LLM_REPLAYED,
    LLM_UNAVAILABLE,
    LLMError,
)
from validator.llm.anthropic_api import AnthropicAPI
from validator.llm.base import LLMClient
from validator.llm.cache import ResponseCache
from validator.llm.calllog import RecordingClient, clear_responses
from validator.llm.claude_cli import ClaudeCLI, login_status
from validator.llm.fake import FakeLLM
from validator.llm.replay import RESPONSES_DIR, ReplayLLM, resolve_run_dir
from validator.models import (
    Backend,
    CacheMode,
    DiagLevel,
    Diagnostic,
    LLMRequest,
    LLMResponse,
    LLMStep,
    RunConfig,
)

_NO_CACHE_BACKENDS = frozenset({Backend.FAKE, Backend.REPLAY})
MAX_PASSES = 9


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


def _api() -> tuple[LLMClient, list[Diagnostic]]:
    try:
        return AnthropicAPI(), []
    except LLMError as exc:
        return _unavailable(Backend.API, exc)


def _replay_unused(cfg: RunConfig, via: str) -> list[Diagnostic]:
    """``--replay`` в ``auto`` не понадобился: модель доступна. Говорим, как пересобрать из записи."""
    if cfg.replay_dir is None:
        return []
    return [
        Diagnostic(
            code=LLM_REPLAYED,
            level=DiagLevel.INFO,
            message=f"Модель доступна, поэтому запись {cfg.replay_dir} не использована",
            action=f"Модель вызывается через {via}",
            hint="Чтобы пересобрать отчёт из записи без модели, добавьте --backend replay",
            step="llm",
        )
    ]


def _replay(cfg: RunConfig) -> tuple[LLMClient, list[Diagnostic]]:
    if cfg.replay_dir is None:
        return _unavailable(
            Backend.REPLAY,
            LLMError(
                "Для --backend replay не указана папка прогона",
                code=CFG_BACKEND_UNAVAILABLE,
                hint="Добавьте --replay ПАПКА, например --replay out/run",
            ),
        )
    try:
        client = ReplayLLM(cfg.replay_dir)
    except LLMError as exc:
        return _unavailable(Backend.REPLAY, exc)
    info = Diagnostic(
        code=LLM_REPLAYED,
        level=DiagLevel.INFO,
        message=f"Ответы модели взяты из записи прогона {cfg.replay_dir} ({client.responses_total} шт.)",
        action="Модель не вызывалась, отчёт пересобран из записанных ответов",
        step="llm",
    )
    return client, [info]


def _subscription_notes(cfg: RunConfig, *, logged_in: bool | None) -> list[Diagnostic]:
    """Пометки к выбору подписки в ``auto``: вход не подтверждён, ``--replay`` не понадобился."""
    notes: list[Diagnostic] = []
    if logged_in is False:
        # claude auth status видит не все способы входа (apiKeyHelper, токен из окружения):
        # не блокируем, честную ошибку даст первый вызов.
        notes.append(
            Diagnostic(
                code=LLM_UNAVAILABLE,
                level=DiagLevel.INFO,
                message="claude auth status не видит входа в аккаунт; модель всё равно вызывается",
                action="Если вызовы упадут с LLM-001, выполните вход: claude, затем /login",
                step="llm",
            )
        )
    return notes + _replay_unused(cfg, "подписку claude -p")


def _auto(cfg: RunConfig) -> tuple[LLMClient, list[Diagnostic]]:
    """Подписка, если ``claude`` вошёл в аккаунт; иначе ключ API; иначе ``--replay``; иначе ``LLM-001``.

    Подписка раньше ключа: она не списывает деньги, а ключ в ``.env`` может быть без средств (D-009).
    Ключ при этом не теряется: для API есть ``--backend api``.
    """
    client, missing = _subscription()
    logged_in = login_status(client.executable) if isinstance(client, ClaudeCLI) else None
    if isinstance(client, ClaudeCLI) and logged_in is not False:
        return client, _subscription_notes(cfg, logged_in=logged_in)
    if cfg.has_api_key:
        api, _ = _api()
        if isinstance(api, AnthropicAPI):
            return api, _replay_unused(cfg, "Claude API (ключ ANTHROPIC_API_KEY)")
    if cfg.replay_dir is not None:
        replayed, replay_diags = _replay(cfg)
        if not isinstance(replayed, UnavailableLLM):
            return replayed, replay_diags
    if isinstance(client, ClaudeCLI):
        return client, _subscription_notes(cfg, logged_in=logged_in)
    reason = missing[0].message if missing else "Команда claude не найдена"
    reason = reason[:1].lower() + reason[1:]
    message = f"Нет доступа к модели: нет ключа ANTHROPIC_API_KEY, а {reason}"
    hint = (
        "Установите Claude Code и выполните вход, задайте ANTHROPIC_API_KEY в .env (см. README) "
        "или пересоберите отчёт из записи: --backend replay --replay ПАПКА"
    )
    return _unavailable(Backend.AUTO, LLMError(message, code=LLM_UNAVAILABLE, hint=hint))


def make_client(cfg: RunConfig) -> tuple[LLMClient, list[Diagnostic]]:
    """Клиент модели для прогона и диагностика выбора (что выбрано и почему)."""
    if cfg.backend is Backend.FAKE:
        return FakeLLM(cfg.fake_fixtures), []
    if cfg.backend is Backend.API:
        return _api()
    if cfg.backend is Backend.REPLAY:
        return _replay(cfg)
    if cfg.backend is Backend.CLI:
        return _subscription()
    return _auto(cfg)


def align_config(cfg: RunConfig, client: LLMClient) -> tuple[RunConfig, list[Diagnostic]]:
    """Для replay число проходов поиска и их модель берём из записи, а не из профиля по умолчанию.

    Иначе ``--backend replay`` без ``--profile`` пересобрал бы прогон ``quick`` как три прохода
    (два упали бы «нет ответа»), а прогон ``submit`` искал бы ответы с другим effort.
    """
    if not isinstance(client, ReplayLLM):
        return cfg, []
    recorded = client.recorded(LLMStep.SEARCH)
    if not recorded:
        return cfg, []
    passes = min(client.calls_in_recording(LLMStep.SEARCH), MAX_PASSES)
    models = set(recorded.values())
    step_models = dict(cfg.step_models)
    if len(models) == 1:
        step_models[LLMStep.SEARCH] = models.pop()
    if passes == cfg.passes and step_models == cfg.step_models:
        return cfg, []
    aligned = cfg.model_copy(update={"passes": passes, "step_models": step_models})
    info = Diagnostic(
        code=LLM_REPLAYED,
        level=DiagLevel.INFO,
        message=(
            f"Проходов поиска в записи: {passes}, модель {aligned.model_for(LLMStep.SEARCH).label}; "
            f"в настройках было {cfg.passes}, {cfg.model_for(LLMStep.SEARCH).label}"
        ),
        action="Число проходов и модель поиска взяты из записи прогона",
        step="llm",
    )
    return aligned, [info]


def _same_dir(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _unused_options(cfg: RunConfig, client: LLMClient) -> list[Diagnostic]:
    """``--cache`` и ``--max-budget-usd`` ничего не делают без живой модели: говорим об этом в отчёте."""
    if client.backend not in _NO_CACHE_BACKENDS:
        return []
    unused = [
        name
        for name, used in (
            ("--cache", cfg.cache is not CacheMode.USE),
            ("--max-budget-usd", cfg.max_budget_usd is not None),
        )
        if used
    ]
    if not unused:
        return []
    return [
        Diagnostic(
            code=CFG_NOT_IMPLEMENTED,
            level=DiagLevel.INFO,
            message=f"Параметры {', '.join(unused)} не действуют с бэкендом {client.backend.value}",
            action="Модель не вызывается: ответы берутся из записи, кешировать и тратить нечего",
            step="config",
        )
    ]


def instrument(cfg: RunConfig, client: LLMClient) -> RecordingClient:
    """Клиент прогона: лог вызовов, ответы в ``run/responses/``, кеш и лимит цены поверх бэкенда.

    Кеш не нужен фикстурам и replay: их ответы и так записаны. Ответы прошлого прогона в той же папке
    убираем, чтобы replay этой папки не подхватил чужие.
    """
    responses_dir = cfg.run_dir / RESPONSES_DIR
    source = resolve_run_dir(cfg.replay_dir) / RESPONSES_DIR if cfg.replay_dir is not None else None
    in_place = source is not None and _same_dir(responses_dir, source)
    if not in_place:
        clear_responses(responses_dir)
    use_cache = cfg.cache is not CacheMode.OFF and client.backend not in _NO_CACHE_BACKENDS
    return RecordingClient(
        client,
        cache=ResponseCache(cfg.cache_dir, cfg.cache) if use_cache else None,
        # Replay в ту же папку: ответы уже лежат там, второй экземпляр под новым ключом не пишем.
        responses_dir=None if in_place else responses_dir,
        max_budget_usd=cfg.max_budget_usd,
        notes=_unused_options(cfg, client),
        # Модели нет вовсе: причину уже назвал выбор бэкенда, повторять её по каждому вызову не нужно.
        summarize_failures=not isinstance(client, UnavailableLLM),
    )


__all__ = ["UnavailableLLM", "align_config", "instrument", "make_client"]
