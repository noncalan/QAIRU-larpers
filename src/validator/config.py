"""Настройки прогона: профили, модели по шагам (D-010) и загрузка ``.env``.

Ключ API здесь не хранится и не печатается: в ``RunConfig`` попадает только признак, что он есть.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dotenv import find_dotenv, load_dotenv

from validator.ingest.pdf_backends import BACKEND_NAMES, DEFAULT_BACKEND
from validator.llm.fake import default_fixtures_dir
from validator.models import (
    Backend,
    CacheMode,
    Effort,
    LLMStep,
    PdfBackendName,
    Profile,
    RunConfig,
    StepModel,
)

# Полные имена моделей, а не алиасы: алиас «opus» переедет на новую модель (D-010).
OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-5-5"

STEP_MODELS: dict[LLMStep, StepModel] = {
    # Нарезка: JSON по схеме, модель указывает блоки, код цитирует.
    LLMStep.SEGMENT: StepModel(model=SONNET, effort=Effort.MEDIUM),
    # Проходы поиска свободным текстом: в JSON модель думает меньше (93 % → 73 %, исследование 21).
    LLMStep.SEARCH: StepModel(model=OPUS, effort=Effort.MEDIUM),
    # Перенос прохода в JSON: на замере 0 потерь, 100 % дословных цитат.
    LLMStep.SEARCH_TRANSFER: StepModel(model=HAIKU, effort=Effort.MEDIUM),
    # Свидетель, критик, вопросы: рассуждение текстом, затем JSON (не замерялись, выбор по смыслу).
    LLMStep.EVIDENCE: StepModel(model=OPUS, effort=Effort.HIGH),
    LLMStep.CRITIC: StepModel(model=OPUS, effort=Effort.HIGH),
    LLMStep.TEST_DRAFT: StepModel(model=SONNET, effort=Effort.HIGH),
    LLMStep.QUESTIONS: StepModel(model=OPUS, effort=Effort.HIGH),
    LLMStep.QUESTIONS_CRITIC: StepModel(model=OPUS, effort=Effort.HIGH),
}


@dataclass(frozen=True)
class ProfileSpec:
    passes: int
    search_effort: Effort
    description: str


# Число проходов по умолчанию для standard и submit окончательно задаст D-011 (контрольная точка 26.11).
PROFILES: dict[Profile, ProfileSpec] = {
    Profile.QUICK: ProfileSpec(1, Effort.MEDIUM, "1 проход, Opus medium: отладка и быстрый прогон"),
    Profile.STANDARD: ProfileSpec(3, Effort.MEDIUM, "3 прохода, Opus medium: обычный прогон"),
    Profile.SUBMIT: ProfileSpec(3, Effort.XHIGH, "3 прохода, Opus xhigh: отчёт на сдачу и финал"),
}

CALL_TIMEOUT_S = 1200
"""Таймаут одного вызова: проход Opus xhigh идёт до 7,5 минуты (исследование 21), берём с запасом."""

CACHE_DIR = Path(".cache/validator")
API_KEY_ENV = "ANTHROPIC_API_KEY"
PDF_BACKEND_ENV = "VALIDATOR_PDF_BACKEND"
PDF_BACKEND = DEFAULT_BACKEND
"""Движок PDF по умолчанию: ``mupdf`` (PyMuPDF, AGPL) или ``pdfium`` (pypdfium2, без копилефта).

Переключается флагом ``--pdf-backend`` или переменной ``VALIDATOR_PDF_BACKEND`` (зачем два: исследование 04).
"""


def step_models(profile: Profile) -> dict[LLMStep, StepModel]:
    """Модели шагов для профиля: профиль меняет только effort проходов поиска."""
    models = dict(STEP_MODELS)
    models[LLMStep.SEARCH] = StepModel(model=OPUS, effort=PROFILES[profile].search_effort)
    return models


def load_env(env_file: Path | None = None) -> None:
    """Подхватывает ``.env`` (из текущей папки или выше), не перетирая уже заданное окружение."""
    path = str(env_file) if env_file is not None else find_dotenv(usecwd=True)
    if path:
        load_dotenv(path, override=False)


def load_config(
    input_path: Path,
    output_path: Path,
    *,
    profile: Profile = Profile.STANDARD,
    passes: int | None = None,
    backend: Backend = Backend.AUTO,
    replay_dir: Path | None = None,
    cache: CacheMode = CacheMode.USE,
    answers_path: Path | None = None,
    ask: bool = False,
    max_budget_usd: float | None = None,
    pdf_backend: str | None = None,
    env_file: Path | None = None,
    critic: bool = False,
) -> RunConfig:
    load_env(env_file)
    pdf_engine = (pdf_backend or os.environ.get(PDF_BACKEND_ENV) or PDF_BACKEND).lower()
    if pdf_engine not in BACKEND_NAMES:
        raise ValueError(f"Неизвестный движок PDF «{pdf_engine}», доступны: {', '.join(BACKEND_NAMES)}")
    spec = PROFILES[profile]
    return RunConfig(
        input_path=input_path,
        output_path=output_path,
        profile=profile,
        passes=passes if passes is not None else spec.passes,
        backend=backend,
        replay_dir=replay_dir,
        cache=cache,
        cache_dir=CACHE_DIR,
        answers_path=answers_path,
        ask=ask,
        critic=critic,
        max_budget_usd=max_budget_usd,
        pdf_backend=cast(PdfBackendName, pdf_engine),
        step_models=step_models(profile),
        fake_fixtures=default_fixtures_dir() if backend is Backend.FAKE else None,
        call_timeout_s=CALL_TIMEOUT_S,
        has_api_key=bool(os.environ.get(API_KEY_ENV, "").strip()),
    )


__all__ = ["PROFILES", "STEP_MODELS", "RunConfig", "load_config", "step_models"]
