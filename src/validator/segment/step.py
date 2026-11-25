"""Шаг 2: вызов модели для нарезки, повтор, запасной путь без модели.

Контракт: ``segment(doc, llm, cfg) -> Segmentation``. Один вызов модели (Sonnet 5.5 ``medium``, D-010)
с JSON-схемой ``RESPONSE_SCHEMA``: модель видит документ с ID блоков и предложений
(``render_for_model``) и возвращает для каждого требования только ID (и границы части предложения),
тип, метку из документа, краткую формулировку и сущности, а для остальных блоков метку «не требование»
с причиной. Цитату, якорь, ``R-NN`` и ``stable_key`` строит код (``build``), он же проверяет ссылки
и полноту (``checks``; исследование 18, пп. 3.1–3.5).

Политика отказа (docs/plan.md, шаг 2): отказ модели или ответ не по схеме даёт один повтор; если и он
не удался, нарезка идёт запасным путём ``fallback_segmentation`` (каждый содержательный блок становится
требованием) с диагностикой ``LLM-020``, и отчёт помечает нарезку как упрощённую.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

from validator.errors import LLM_BAD_RESPONSE, LLM_SEGMENT_FALLBACK, LLMError
from validator.ingest.blocks import render_for_model
from validator.llm.base import LLMClient
from validator.models import (
    BlockLabel,
    DiagLevel,
    Diagnostic,
    ExcludedBlock,
    LLMRequest,
    LLMStep,
    ParsedDocument,
    RequirementType,
    RunConfig,
    Segmentation,
    SegmentationMode,
)
from validator.segment.answer import API_SUBTYPES, ENTITY_KEYS, EXCLUDE_LABELS, AnswerError, parse_answer
from validator.segment.build import Draft, block_draft, build_requirements, fallback_label
from validator.segment.checks import from_answer, no_requirements

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
SYSTEM_PROMPT = "segment_system.md"
USER_PROMPT = "segment_user.md"
DOCUMENT_SLOT = "{document}"

MAX_ATTEMPTS = 2
"""Первый вызов и один повтор с пояснением, если по ответу нельзя собрать нарезку (план, шаг 2).

Сбои самого вызова (перегрузка, ответ не по JSON-схеме, таймаут) повторяет клиент модели
(``calllog.RecordingClient``); шаг их не повторяет, чтобы не умножать живые вызовы."""

RETRY_NOTE = (
    "\n\nПредыдущий ответ не прошёл проверку: {reason}. Ответь строго по схеме: объект с полями "
    "requirements и excluded, в ссылках только ID блоков и предложений из документа."
)

_REF_PATTERN = r"^\[?[BbВв][0-9]+(\.[0-9]+)?\]?$"

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "requirements": {
            "type": "array",
            "description": "Требования по порядку документа",
            "items": {
                "type": "object",
                "properties": {
                    "refs": {
                        "type": "array",
                        "description": "ID предложений (B012.3) или блоков (B012) требования",
                        "items": {"type": "string", "pattern": _REF_PATTERN},
                    },
                    "part_start": {"type": "string", "description": "Первые слова части предложения или ''"},
                    "part_end": {"type": "string", "description": "Последние слова части предложения или ''"},
                    "type": {"type": "string", "enum": [t.value for t in RequirementType]},
                    "subtype": {"type": "string", "enum": ["", *API_SUBTYPES]},
                    "source_id": {"type": "string", "description": "Метка требования в документе или ''"},
                    "summary": {"type": "string", "description": "Краткая формулировка до 15 слов"},
                    "entities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": list(ENTITY_KEYS)},
                                "value": {"type": "string"},
                            },
                            "required": ["kind", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "links": {"type": "array", "items": {"type": "string", "pattern": _REF_PATTERN}},
                    "depends_on": {
                        "type": "string",
                        "description": "ID, без которого требование непонятно, или ''",
                    },
                },
                "required": [
                    "refs",
                    "part_start",
                    "part_end",
                    "type",
                    "subtype",
                    "source_id",
                    "summary",
                    "entities",
                    "links",
                    "depends_on",
                ],
                "additionalProperties": False,
            },
        },
        "excluded": {
            "type": "array",
            "description": "Блоки и предложения, которые не являются требованиями (кроме заголовков)",
            "items": {
                "type": "object",
                "properties": {
                    "block": {"type": "string", "pattern": _REF_PATTERN},
                    "label": {"type": "string", "enum": [label.value for label in EXCLUDE_LABELS]},
                    "reason": {"type": "string"},
                },
                "required": ["block", "label", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["requirements", "excluded"],
    "additionalProperties": False,
}


@cache
def _prompt_file(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def build_prompt(doc: ParsedDocument) -> str:
    """Пользовательский промпт: описание разметки и документ с ID блоков внутри меток."""
    # replace, а не format: в документе бывают фигурные скобки (JSON-примеры).
    return _prompt_file(USER_PROMPT).replace(DOCUMENT_SLOT, render_for_model(doc).rstrip("\n"))


def _request(cfg: RunConfig, prompt: str, index: int) -> LLMRequest:
    model = cfg.model_for(LLMStep.SEGMENT)
    return LLMRequest(
        step=LLMStep.SEGMENT,
        call_index=index,
        model=model.model,
        effort=model.effort,
        system=_prompt_file(SYSTEM_PROMPT),
        prompt=prompt,
        json_schema=RESPONSE_SCHEMA,
        timeout_s=cfg.call_timeout_s,
    )


def _ask_model(doc: ParsedDocument, llm: LLMClient, cfg: RunConfig, call_ids: list[str]) -> Segmentation:
    """Вызов модели и сборка нарезки; негодный по смыслу ответ даёт один повтор. Неудача — ``LLMError``.

    ``LLMError`` клиента (отказ, лимит, сбой после его собственного повтора) идёт наружу сразу.
    """
    base = build_prompt(doc)
    prompt = base
    for index in range(1, MAX_ATTEMPTS + 1):
        req = _request(cfg, prompt, index)
        call_ids.append(req.call_id)
        resp = llm.call(req)
        try:
            return from_answer(doc, parse_answer(resp.structured))
        except AnswerError as exc:
            if index == MAX_ATTEMPTS:
                raise LLMError(
                    f"Ответ нарезки не годится: {exc}",
                    code=LLM_BAD_RESPONSE,
                    hint="Повторите прогон; если повторяется, сообщите команде",
                    location=req.call_id,
                ) from exc
            prompt = base + RETRY_NOTE.format(reason=exc)
    raise AssertionError("unreachable")  # pragma: no cover - цикл всегда возвращает или бросает


def fallback_segmentation(doc: ParsedDocument) -> Segmentation:
    """Запасная нарезка без модели: каждый содержательный блок от 3 слов становится требованием."""
    drafts: list[Draft] = []
    excluded: list[ExcludedBlock] = []
    for block in doc.blocks:
        label, reason = fallback_label(block)
        if label is BlockLabel.REQUIREMENT:
            drafts.append(block_draft(doc, block))
        else:
            excluded.append(ExcludedBlock(block_id=block.id, label=label, reason=reason))
    requirements = build_requirements(doc, drafts)
    return Segmentation(
        requirements=requirements,
        excluded=excluded,
        mode=SegmentationMode.FALLBACK,
        diagnostics=[] if requirements else no_requirements(doc),
    )


def segment(doc: ParsedDocument, llm: LLMClient, cfg: RunConfig) -> Segmentation:
    """Нарезка моделью с проверками кодом; если модель не справилась, запасной путь с ``LLM-020``."""
    call_ids: list[str] = []
    try:
        seg = _ask_model(doc, llm, cfg, call_ids)
    except LLMError as exc:
        fallback = fallback_segmentation(doc)
        diag = Diagnostic(
            code=LLM_SEGMENT_FALLBACK,
            level=DiagLevel.WARNING,
            message=f"Нарезка моделью не удалась ({exc.code}): {exc.message}",
            action="Нарезка упрощённая, без модели: каждый содержательный блок документа стал требованием",
            hint=exc.hint or "Повторите прогон; если повторяется, сообщите команде",
            location=exc.location,
            step="segment",
        )
        return fallback.model_copy(
            update={"call_ids": call_ids, "diagnostics": [diag, *fallback.diagnostics]}
        )
    return seg.model_copy(update={"call_ids": call_ids})


__all__ = ["RESPONSE_SCHEMA", "build_prompt", "fallback_segmentation", "segment"]
