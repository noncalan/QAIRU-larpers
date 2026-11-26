"""Разметчик precision (#49): промпт, схема ответа, запрос, разбор и кеш.

Промпт один для обоих разметчиков (Opus 5.5 и Fable 5.2, оба ``high``). Разметчик получает только
документ и пачку находок (``LabelItem.shown``): ни эталона ``docs/gold``, ни вердиктов судьи, ни
разметки другого разметчика, ни того, кто писал находки. ``tests/test_eval_precision.py`` это
проверяет на собранном запросе.

Ответы кешируются в ``evals/cache/precision/`` тем же ключом, что у судьи (``verdict_key``: модель,
effort, системный промпт, промпт, схема), и коммитятся: пересчёт метрик лимит не тратит.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from evals.judge import EvalError, VerdictCache, display_path, verdict_key
from evals.precision_items import LabelItem
from validator.config import OPUS
from validator.errors import LLMError
from validator.llm.base import LLMClient
from validator.models import Backend, Effort, LLMRequest, LLMStep

FABLE = "claude-fable-5-2"
"""Fable 5.2 полным именем: второй разметчик другой модели, чем писала находки (Opus 5.5)."""
DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "precision"
LABEL_TIMEOUT_S = 2400
"""Как у судьи: пачка из 40–50 находок у модели с effort high идёт несколько минут."""
REASON_CHARS = 200


class Labeler(StrEnum):
    """Разметчики: две разные модели, чтобы ошибки одной не совпадали с ошибками другой."""

    OPUS = "opus"
    FABLE = "fable"

    @property
    def model(self) -> str:
        return {"opus": OPUS, "fable": FABLE}[self.value]

    @property
    def effort(self) -> Effort:
        return Effort.HIGH

    @property
    def display(self) -> str:
        return {"opus": "Opus 5.5", "fable": "Fable 5.2"}[self.value]


class Label(StrEnum):
    REAL = "real"
    DISPUTED = "disputed"
    FALSE = "false"

    @property
    def label(self) -> str:
        return {"real": "настоящий", "disputed": "спорно", "false": "ложный"}[self.value]


LABEL_SYSTEM = (
    "Ты придирчивый ведущий QA-инженер банка. Ты проверяешь чужое ревью требований перед тем, как "
    "команда возьмёт его в работу. Ты не автор этих находок и не обязан их защищать: каждая ложная "
    "находка стоит команде времени. Отвечай на русском языке."
)

LABEL_PROMPT = """Ниже документ требований и пронумерованный список находок, которые прислал проверяющий. У находки есть цитата из документа (quote), место (place), свойство стандарта ISO/IEC/IEEE 29148 или IEEE 830 (property) и объяснение (explanation).

Для КАЖДОЙ находки поставь одну метку:
- real (настоящий дефект): процитированный текст есть в документе, проблема из объяснения действительно есть именно в этом тексте, и она мешает разработке или тестированию: без исправления разработчик может сделать не то, или тестировщик не сможет написать однозначный тест-кейс с ожидаемым результатом;
- disputed (спорно): проблема есть, но слабая или зависит от трактовки, или это скорее пожелание; исправить полезно, но разработку и тест она почти не задевает;
- false (ложная находка): проблемы нет; объяснение не относится к процитированному тексту; документ уже отвечает на это в другом месте; придирка к словам, которая не влияет ни на разработку, ни на тест.

Свойство стандарта это подсказка проверяющего: если суть проблемы описана верно, а свойство названо неточно, это не повод для false. Оценивай каждую находку саму по себе: повторы и пересечения между находками не важны.

Для каждой находки дай причину одной строкой, до 200 символов: что в документе подтверждает или опровергает находку. Ответь про все номера из списка. Работай только с этими данными."""  # noqa: E501

LABEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "labels": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "label": {"type": "string", "enum": [label.value for label in Label]},
                    "reason": {"type": "string"},
                },
                "required": ["id", "label", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["labels"],
    "additionalProperties": False,
}


def render_batch(items: Sequence[LabelItem]) -> str:
    """Пачка для разметчика: JSON-массив ``shown`` с номерами с 1 по порядку пачки."""
    return json.dumps([item.shown(n) for n, item in enumerate(items, start=1)], ensure_ascii=False, indent=1)


def label_prompt(document: str, items: Sequence[LabelItem]) -> str:
    """Запрос разметчику: инструкция, документ, находки. Других входов у него нет."""
    return LABEL_PROMPT + "\n\n=== ДОКУМЕНТ ===\n" + document + "\n\n=== НАХОДКИ ===\n" + render_batch(items)


def label_request(labeler: Labeler, document: str, items: Sequence[LabelItem], batch: int) -> LLMRequest:
    return LLMRequest(
        step=LLMStep.PRECISION,
        call_index=batch,
        model=labeler.model,
        effort=labeler.effort,
        system=LABEL_SYSTEM,
        prompt=label_prompt(document, items),
        json_schema=LABEL_SCHEMA,
        timeout_s=LABEL_TIMEOUT_S,
    )


class ItemLabel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: Label
    reason: str = ""


class LabelCall(BaseModel):
    """Как получена разметка пачки: живой вызов, кеш или подставленный клиент тестов."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    labeler: Labeler
    batch: int
    key: str
    source: str = Field(description="cli, cache или fake")
    model: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    cache_path: str | None = None


def parse_labels(structured: object, n_items: int, warnings: list[str], who: str) -> dict[int, ItemLabel]:
    """Ответ по ``LABEL_SCHEMA`` → метки по номеру в пачке. Чужие номера и метки отбрасываются."""
    if not isinstance(structured, Mapping) or not isinstance(structured.get("labels"), list):
        raise EvalError(f"{who}: ответ не по схеме, нет списка labels")
    out: dict[int, ItemLabel] = {}
    for entry in structured["labels"]:
        if not isinstance(entry, Mapping):
            warnings.append(f"{who}: запись не объект, пропущена")
            continue
        number = entry.get("id")
        if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= n_items:
            warnings.append(f"{who}: номер {number!r} вне пачки, пропущен")
            continue
        try:
            label = Label(str(entry.get("label")))
        except ValueError:
            warnings.append(f"{who}: находка {number}: неизвестная метка {entry.get('label')!r}, пропущена")
            continue
        if number in out:
            warnings.append(f"{who}: находка {number} размечена дважды, берём последнюю запись")
        reason = " ".join(str(entry.get("reason") or "").split())[: REASON_CHARS * 2]
        out[number] = ItemLabel(label=label, reason=reason)
    missing = [n for n in range(1, n_items + 1) if n not in out]
    if missing:
        warnings.append(f"{who}: без метки находки {missing}, в метрики не идут")
    return out


class LabelerRunner:
    """Разметка пачки: сначала кеш, затем клиент модели. Без клиента работает только из кеша."""

    def __init__(self, client_factory: Callable[[], LLMClient] | None, cache: VerdictCache | None) -> None:
        self._factory = client_factory
        self._client: LLMClient | None = None
        self.cache = cache

    def _get_client(self) -> LLMClient | None:
        if self._client is None and self._factory is not None:
            try:
                self._client = self._factory()
            except LLMError as exc:
                raise EvalError(
                    f"Разметчик недоступен: {exc.code}: {exc.message}. {exc.hint}".strip()
                ) from exc
        return self._client

    def ask(
        self, req: LLMRequest, labeler: Labeler, batch: int, meta: Mapping[str, Any]
    ) -> tuple[object, LabelCall]:
        key = verdict_key(req)
        cached = self.cache.get(req) if self.cache is not None else None
        if cached is not None:
            path = display_path(self.cache.path(key)) if self.cache is not None else None
            return cached.structured, LabelCall(
                labeler=labeler,
                batch=batch,
                key=key,
                source="cache",
                model=cached.model,
                cost_usd=cached.cost_usd,
                duration_ms=cached.duration_ms,
                cache_path=path,
            )
        client = self._get_client()
        if client is None:
            raise EvalError(
                f"Нет разметки {labeler.display} в кеше для пачки {batch} (ключ {key[:12]}…). "
                "Запустите с --mode cli, чтобы вызвать разметчика (несколько минут на пачку)"
            )
        try:
            resp = client.call(req)
        except LLMError as exc:
            raise EvalError(f"{labeler.display}, пачка {batch}: {exc.code}: {exc.message}") from exc
        path = None
        if self.cache is not None and resp.backend is not Backend.FAKE:
            path = display_path(self.cache.put(req, resp, meta))
        source = "fake" if resp.backend is Backend.FAKE else "cli"
        return resp.structured, LabelCall(
            labeler=labeler,
            batch=batch,
            key=key,
            source=source,
            model=resp.model,
            cost_usd=resp.cost_usd,
            duration_ms=resp.duration_ms,
            cache_path=path,
        )
