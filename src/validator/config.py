"""Настройки прогона: профили, модели по шагам (D-010) и загрузка ``.env``.

Ключ API здесь не хранится и не печатается: в ``RunConfig`` попадает только признак, что он есть.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from dotenv import find_dotenv, load_dotenv

from validator.ingest.ocr import ocr_enabled
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
    SearchInput,
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
    # Сверка требований между частями длинного документа (#38): как проход поиска, effort из профиля.
    LLMStep.SET_CHECK: StepModel(model=OPUS, effort=Effort.MEDIUM),
    # Сверка находок с ответами автора (--answers, #42): та же модель, что у вопросов.
    LLMStep.CLARIFY: StepModel(model=OPUS, effort=Effort.HIGH),
}


@dataclass(frozen=True)
class ProfileSpec:
    passes: int
    search_effort: Effort
    description: str


# Число проходов по умолчанию задало правило D-011 (контрольная точка 26.11, docs/decisions.md): один проход.
# Три прохода стабильнее (Жаккар находок 0,62 против 0,51), но правило их не включило; они остаются
# опцией ``--passes 3``. Профили quick и standard сейчас совпадают: quick оставлен для обратной совместимости.
PROFILES: dict[Profile, ProfileSpec] = {
    Profile.QUICK: ProfileSpec(1, Effort.MEDIUM, "1 проход, Opus medium: отладка и быстрый прогон"),
    Profile.STANDARD: ProfileSpec(1, Effort.MEDIUM, "1 проход, Opus medium: обычный прогон (D-011)"),
    Profile.SUBMIT: ProfileSpec(1, Effort.XHIGH, "1 проход, Opus xhigh: отчёт на сдачу и финал (D-011)"),
}

SEARCH_INPUT = SearchInput.BLOCKS
"""Вход поиска по умолчанию: документ с ID блоков. ``raw`` (текст без ID, блоки по цитате) нужен для
замера B′ на контрольной точке (#37); что выбрать по умолчанию, решает D-011, п. 2."""

CHECKLISTS = True
"""Доменные чек-листы с триггерами в промпте поиска по умолчанию (#35). Выключаются ``--no-checklists``;
решение по умолчанию и цифры eval: описание PR #35 и ``src/validator/checklists/README.md``."""

CALL_TIMEOUT_S = 1200
"""Таймаут одного вызова: проход Opus xhigh идёт до 7,5 минуты (исследование 21), берём с запасом."""
MAX_CALL_TIMEOUT_S = 3600
"""Потолок таймаута вызова с большим входом (``call_timeout``): дольше часа ответа не ждём."""

CHARS_PER_TOKEN = 2.0
"""Оценка токенов по символам. Замер на записанных живых прогонах (#36, ``tests/fixtures/e2e``): русский
текст с разметкой блоков даёт 1,8–2,2 символа на токен. Берём меньшее круглое: оценка с запасом вверх."""
LONG_DOC_TOKENS = 12_000
"""Граница длинного документа (#38): документ для модели (с ID блоков) длиннее стольких токенов режется по
разделам на части. Наш документ около 8,5 тыс.: проход поиска по нему уже выдаёт 22–32 тыс. токенов
ответа (с рассуждением), а ``claude -p`` ограничивает ответ; часть больше этого рискует упереться в него
и терять находки в середине («Lost in the Middle», исследование 18, п. 3.5). Таймаут вызова откалиброван
на документ такого размера (``call_timeout``)."""
PART_TARGET_TOKENS = 8_000
"""Целевой размер части с запасом до ``LONG_DOC_TOKENS``: части набираются из целых разделов до этого
размера, раздел больше ``LONG_DOC_TOKENS`` делится по подразделам, а без них по блокам."""
GLOSSARY_MAX_TOKENS = 3_000
"""Раздел терминов и определений добавляется в каждую часть для справки, если не длиннее этого."""
FULL_VIEW_MAX_TOKENS = 60_000
"""Документ длиннее этого вопросы автору и их критик видят не целиком, а разделы с цитатами кандидатов,
раздел терминов и оглавление: иначе вход вызова подходит к пределу контекста модели."""

CACHE_DIR = Path(".cache/validator")
API_KEY_ENV = "ANTHROPIC_API_KEY"
PDF_BACKEND_ENV = "VALIDATOR_PDF_BACKEND"
PDF_BACKEND = DEFAULT_BACKEND
"""Движок PDF по умолчанию: ``mupdf`` (PyMuPDF, AGPL) или ``pdfium`` (pypdfium2, без копилефта).

Переключается флагом ``--pdf-backend`` или переменной ``VALIDATOR_PDF_BACKEND`` (зачем два: исследование 04).
"""


def step_models(profile: Profile) -> dict[LLMStep, StepModel]:
    """Модели шагов для профиля: профиль меняет effort проходов поиска и сверки частей длинного документа."""
    models = dict(STEP_MODELS)
    models[LLMStep.SEARCH] = StepModel(model=OPUS, effort=PROFILES[profile].search_effort)
    models[LLMStep.SET_CHECK] = StepModel(model=OPUS, effort=PROFILES[profile].search_effort)
    return models


def estimate_tokens(chars: int) -> int:
    """Оценка токенов по числу символов (``CHARS_PER_TOKEN``), с округлением вверх."""
    return math.ceil(chars / CHARS_PER_TOKEN)


def call_timeout(cfg: RunConfig, *texts: str) -> int:
    """Таймаут вызова растёт с размером входа (#38): базовый ``cfg.call_timeout_s`` рассчитан на вход до
    ``LONG_DOC_TOKENS``, больше пропорционально, но не дольше ``MAX_CALL_TIMEOUT_S``."""
    tokens = estimate_tokens(sum(len(t) for t in texts))
    if tokens <= LONG_DOC_TOKENS:
        return cfg.call_timeout_s
    scaled = cfg.call_timeout_s * tokens // LONG_DOC_TOKENS
    return max(cfg.call_timeout_s, min(MAX_CALL_TIMEOUT_S, scaled))


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
    search_input: SearchInput | None = None,
    checklists: bool | None = None,
    ocr: bool | None = None,
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
        search_input=search_input if search_input is not None else SEARCH_INPUT,
        checklists=checklists if checklists is not None else CHECKLISTS,
        step_models=step_models(profile),
        fake_fixtures=default_fixtures_dir() if backend is Backend.FAKE else None,
        call_timeout_s=CALL_TIMEOUT_S,
        has_api_key=bool(os.environ.get(API_KEY_ENV, "").strip()),
        ocr=ocr_enabled(ocr),
    )


__all__ = [
    "CHECKLISTS",
    "PROFILES",
    "SEARCH_INPUT",
    "STEP_MODELS",
    "RunConfig",
    "call_timeout",
    "estimate_tokens",
    "load_config",
    "ocr_enabled",
    "step_models",
]
