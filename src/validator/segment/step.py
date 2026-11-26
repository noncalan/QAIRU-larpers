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

Длинный документ (#38, ``parts.plan_parts``): вызов на каждую часть (не больше ``MAX_PARALLEL_PARTS``
одновременно), модель видит только блоки части с пометкой ``prompts/part_note.md``. Черновики всех частей
нумерует код по всему документу сразу, поэтому ``R-NN`` сквозные, а якоря глобальные. Часть, нарезка
которой не удалась, идёт запасным путём с ``LLM-020``, остальные части остаются нарезкой модели.
Номер вызова части ``k``: ``(k − 1) · MAX_ATTEMPTS + попытка``, у документа целиком как раньше.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

from validator.config import call_timeout
from validator.errors import (
    INT_STEP_DEGRADED,
    LLM_BAD_RESPONSE,
    LLM_SEGMENT_FALLBACK,
    InternalError,
    LLMError,
    ValidatorError,
)
from validator.ingest.blocks import render_for_model
from validator.llm.base import LLMClient
from validator.models import (
    Block,
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
from validator.parts import Part, PartPlan, part_view, render_part
from validator.segment.answer import API_SUBTYPES, ENTITY_KEYS, EXCLUDE_LABELS, AnswerError, parse_answer
from validator.segment.build import Draft, block_draft, build_requirements, fallback_label
from validator.segment.checks import Notes, assemble, decide, merge_notes, no_requirements

logger = logging.getLogger("validator")

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
SYSTEM_PROMPT = "segment_system.md"
USER_PROMPT = "segment_user.md"
DOCUMENT_SLOT = "{document}"

MAX_PARALLEL_PARTS = 3
"""Не больше трёх вызовов нарезки частей одновременно, как у проходов поиска: лимит подписки."""

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


def build_prompt(doc: ParsedDocument, rendered: str | None = None) -> str:
    """Пользовательский промпт: описание разметки и документ с ID блоков внутри меток.

    ``rendered``: готовый текст для модели (часть длинного документа с пометкой); по умолчанию весь документ.
    """
    text = render_for_model(doc) if rendered is None else rendered
    # replace, а не format: в документе бывают фигурные скобки (JSON-примеры).
    return _prompt_file(USER_PROMPT).replace(DOCUMENT_SLOT, text.rstrip("\n"))


def _request(cfg: RunConfig, prompt: str, index: int, timeout_s: int | None = None) -> LLMRequest:
    model = cfg.model_for(LLMStep.SEGMENT)
    return LLMRequest(
        step=LLMStep.SEGMENT,
        call_index=index,
        model=model.model,
        effort=model.effort,
        system=_prompt_file(SYSTEM_PROMPT),
        prompt=prompt,
        json_schema=RESPONSE_SCHEMA,
        timeout_s=timeout_s or cfg.call_timeout_s,
    )


def _ask_model(doc: ParsedDocument, llm: LLMClient, cfg: RunConfig, call_ids: list[str]) -> Segmentation:
    """Вызов модели и сборка нарезки; негодный по смыслу ответ даёт один повтор. Неудача — ``LLMError``.

    ``LLMError`` клиента (отказ, лимит, сбой после его собственного повтора) идёт наружу сразу.
    """
    drafts, kept, notes = _decide_by_model(doc, render_for_model(doc), llm, cfg, call_ids, offset=0)
    return assemble(doc, drafts, kept, notes)


def _decide_by_model(
    view: ParsedDocument, rendered: str, llm: LLMClient, cfg: RunConfig, call_ids: list[str], offset: int
) -> tuple[list[Draft], list[ExcludedBlock], Notes]:
    """Решения модели по блокам ``view`` с одним повтором; номера вызовов ``offset + попытка``."""
    base = build_prompt(view, rendered)
    prompt = base
    timeout_s = call_timeout(cfg, rendered)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = _request(cfg, prompt, offset + attempt, timeout_s)
        call_ids.append(req.call_id)
        resp = llm.call(req)
        notes = Notes()
        try:
            drafts, kept = decide(view, parse_answer(resp.structured), notes)
            return drafts, kept, notes
        except AnswerError as exc:
            if attempt == MAX_ATTEMPTS:
                raise LLMError(
                    f"Ответ нарезки не годится: {exc}",
                    code=LLM_BAD_RESPONSE,
                    hint="Повторите прогон; если повторяется, сообщите команде",
                    location=req.call_id,
                ) from exc
            prompt = base + RETRY_NOTE.format(reason=exc)
    raise AssertionError("unreachable")  # pragma: no cover - цикл всегда возвращает или бросает


def _fallback_decisions(doc: ParsedDocument, blocks: list[Block]) -> tuple[list[Draft], list[ExcludedBlock]]:
    """Решения запасной нарезки по блокам: каждый содержательный блок от 3 слов становится требованием."""
    drafts: list[Draft] = []
    excluded: list[ExcludedBlock] = []
    for block in blocks:
        label, reason = fallback_label(block)
        if label is BlockLabel.REQUIREMENT:
            drafts.append(block_draft(doc, block))
        else:
            excluded.append(ExcludedBlock(block_id=block.id, label=label, reason=reason))
    return drafts, excluded


def fallback_segmentation(doc: ParsedDocument) -> Segmentation:
    """Запасная нарезка без модели: каждый содержательный блок от 3 слов становится требованием."""
    drafts, excluded = _fallback_decisions(doc, list(doc.blocks))
    requirements = build_requirements(doc, drafts)
    return Segmentation(
        requirements=requirements,
        excluded=excluded,
        mode=SegmentationMode.FALLBACK,
        diagnostics=[] if requirements else no_requirements(doc),
    )


@dataclass
class _PartResult:
    """Решения по одной части длинного документа."""

    drafts: list[Draft] = field(default_factory=list)
    kept: list[ExcludedBlock] = field(default_factory=list)
    notes: Notes = field(default_factory=Notes)
    call_ids: list[str] = field(default_factory=list)
    failure: Diagnostic | None = None


def _segment_part(
    doc: ParsedDocument, plan: PartPlan, part: Part, llm: LLMClient, cfg: RunConfig
) -> _PartResult:
    out = _PartResult()
    view = part_view(doc, plan, part, with_context=False)
    rendered = render_part(doc, plan, part, with_context=False)
    try:
        out.drafts, out.kept, out.notes = _decide_by_model(
            view, rendered, llm, cfg, out.call_ids, offset=(part.index - 1) * MAX_ATTEMPTS
        )
    except LLMError as exc:
        _part_fallback(doc, plan, part, out, exc)
    except Exception as exc:  # граница части: сбой кода на одной части не отменяет нарезку остальных
        logger.exception("Нарезка части %d: внутренняя ошибка", part.index)
        failure = InternalError(
            f"внутренняя ошибка агента при разборе ответа ({type(exc).__name__})",
            code=INT_STEP_DEGRADED,
            hint="Подробности в report.log; сообщите команде",
            location=out.call_ids[-1] if out.call_ids else None,
        )
        _part_fallback(doc, plan, part, out, failure)
    return out


def _part_fallback(
    doc: ParsedDocument, plan: PartPlan, part: Part, out: _PartResult, exc: ValidatorError
) -> None:
    """Часть нарезается запасным путём, остальные части остаются нарезкой модели."""
    out.drafts, out.kept, out.notes = *_fallback_decisions(doc, list(part.blocks)), Notes()
    sections = "; ".join(part.sections)
    out.failure = Diagnostic(
        code=LLM_SEGMENT_FALLBACK if isinstance(exc, LLMError) else exc.code,
        level=DiagLevel.WARNING,
        message=(
            f"Нарезка моделью части {part.index} из {len(plan.parts)} не удалась ({exc.code}): {exc.message}"
        ),
        action=(
            f"Часть {part.index} ({sections}) нарезана упрощённо, без модели: каждый содержательный блок "
            "стал требованием; остальные части нарезала модель"
        ),
        hint=exc.hint or "Повторите прогон; если повторяется, сообщите команде",
        location=exc.location,
        step="segment",
    )


def _segment_parts(doc: ParsedDocument, llm: LLMClient, cfg: RunConfig, plan: PartPlan) -> Segmentation:
    """Нарезка длинного документа по частям, номера ``R-NN`` по всему документу сразу."""
    with ThreadPoolExecutor(
        max_workers=min(MAX_PARALLEL_PARTS, len(plan.parts)), thread_name_prefix="segment"
    ) as pool:
        results = list(pool.map(lambda part: _segment_part(doc, plan, part, llm, cfg), plan.parts))
    notes = Notes()
    drafts: list[Draft] = []
    kept: list[ExcludedBlock] = []
    for r in results:
        merge_notes(notes, r.notes)
        drafts.extend(r.drafts)
        kept.extend(r.kept)
    by_model = any(r.failure is None for r in results)
    seg = assemble(
        doc, drafts, kept, notes, SegmentationMode.MODEL if by_model else SegmentationMode.FALLBACK
    )
    failures = [r.failure for r in results if r.failure is not None]
    return seg.model_copy(
        update={
            "call_ids": [cid for r in results for cid in r.call_ids],
            "diagnostics": [*failures, *seg.diagnostics],
        }
    )


def segment(
    doc: ParsedDocument, llm: LLMClient, cfg: RunConfig, plan: PartPlan | None = None
) -> Segmentation:
    """Нарезка моделью с проверками кодом; если модель не справилась, запасной путь с ``LLM-020``.

    ``plan``: части длинного документа (#38); без него или с одной частью документ идёт одним вызовом.
    """
    if plan is not None and plan.split:
        return _segment_parts(doc, llm, cfg, plan)
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
