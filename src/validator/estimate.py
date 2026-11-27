"""Оценка вызовов модели и цены прогона до первого вызова (#38).

Длинный документ идёт по частям: нарезка и каждый проход поиска вызываются на каждую часть, плюс один
вызов сверки между частями. Пользователь должен знать цену заранее, а ``--max-budget-usd`` должен
предупредить до старта, а не оборвать прогон на середине. Оценка грубая, по тарифу API (``pricing.py``):
вход считается записью в кеш (как у ``claude -p``), выход пропорционален входу шага.

Коэффициенты замерены на записанных живых прогонах ``tests/fixtures/e2e`` (MD, PDF, DOCX; 26.11.2026,
документ около 8,5 тыс. токенов, профиль standard): нарезка отвечает 7,2–7,3 тыс. токенов, проход поиска
23–32 тыс., пример-свидетель 14–16 тыс., редактор вопросов 21–23 тыс., критик вопросов 5–6 тыс.; служебная
часть промпта около 7 тыс. токенов.
"""

from __future__ import annotations

from dataclasses import dataclass

from validator.config import FULL_VIEW_MAX_TOKENS
from validator.llm.pricing import Tokens, price, usd_text
from validator.models import LLMStep, RunConfig
from validator.parts import PartPlan

PROMPT_OVERHEAD_TOKENS = 7_000
"""Системный промпт и инструкции одного вызова сверх текста документа."""
SEGMENT_OUTPUT_PER_TOKEN = 0.9
"""Ответ нарезки на токен входа: 7,3 тыс. на 8,3 тыс."""
SEARCH_OUTPUT_PER_TOKEN = 3.3
"""Ответ прохода поиска (с рассуждением) на токен входа: около 27 тыс. на 8,3 тыс."""
QUESTIONS_OVERHEAD_TOKENS = 17_000
"""Вход редактора вопросов сверх документа: карточки кандидатов и инструкции (25–29 тыс. всего)."""
QUESTIONS_OUTPUT_TOKENS = 23_000
QUESTIONS_CRITIC_OUTPUT_TOKENS = 6_000
WITNESS_OVERHEAD_TOKENS = 8_000
"""Вход примера-свидетеля сверх документа: до 30 находок с цитатами и инструкции (16–17 тыс. всего)."""

WITNESS_OUTPUT_TOKENS = 16_000
"""Ответ свидетеля: 14–16 тыс. токенов. Критик находок (``--critic``) не замерен, считаем как свидетеля."""

CLARIFY_OVERHEAD_TOKENS = 24_000
"""Вход сверки с ответами автора (``--answers``) сверх документа: находки и ответы (32 тыс. в записи MD)."""

CLARIFY_OUTPUT_TOKENS = 12_000
"""Ответ сверки с ответами автора: 11,4 тыс. токенов в записи MD."""

SET_CHECK_INPUT_SHARE = 0.35
"""Сжатый список требований для сверки на токен документа (короткие формулировки вместо блоков)."""
SET_CHECK_OUTPUT_TOKENS = 12_000


@dataclass(frozen=True)
class StepEstimate:
    step: LLMStep
    calls: int
    usd: float | None


@dataclass(frozen=True)
class RunEstimate:
    """Сколько вызовов и сколько долларов по тарифу API; ``usd`` None, если тариф модели неизвестен."""

    steps: tuple[StepEstimate, ...]

    @property
    def calls(self) -> int:
        return sum(s.calls for s in self.steps)

    @property
    def usd(self) -> float | None:
        costs = [s.usd for s in self.steps if s.calls]
        return None if any(c is None for c in costs) else sum(c for c in costs if c is not None)


def _cost(cfg: RunConfig, step: LLMStep, prompt: int, output: int) -> float | None:
    return price(cfg.model_for(step).model, Tokens(cache_write_5m=prompt, output=output))


def _per_part(cfg: RunConfig, step: LLMStep, plan: PartPlan, ratio: float, calls: int) -> StepEstimate:
    usd: float | None = 0.0
    for part in plan.parts:
        one = _cost(cfg, step, part.tokens + PROMPT_OVERHEAD_TOKENS, round(part.tokens * ratio))
        usd = None if usd is None or one is None else usd + one * calls
    return StepEstimate(step, len(plan.parts) * calls, usd)


def estimate_run(plan: PartPlan, cfg: RunConfig) -> RunEstimate:
    """Оценка по плану частей: нарезка и поиск по частям, сверка (если частей несколько), свидетель (и критик
    находок с ``--critic``), вопросы и их критик, сверка с ответами автора (с ``--answers``)."""
    view = min(plan.tokens, FULL_VIEW_MAX_TOKENS)
    steps = [
        _per_part(cfg, LLMStep.SEGMENT, plan, SEGMENT_OUTPUT_PER_TOKEN, 1),
        _per_part(cfg, LLMStep.SEARCH, plan, SEARCH_OUTPUT_PER_TOKEN, cfg.passes),
    ]
    if plan.split:
        listed = round(plan.tokens * SET_CHECK_INPUT_SHARE) + PROMPT_OVERHEAD_TOKENS
        steps.append(
            StepEstimate(LLMStep.SET_CHECK, 1, _cost(cfg, LLMStep.SET_CHECK, listed, SET_CHECK_OUTPUT_TOKENS))
        )
    evidence = [LLMStep.EVIDENCE, *([LLMStep.CRITIC] if cfg.critic else [])]
    steps += [
        StepEstimate(step, 1, _cost(cfg, step, view + WITNESS_OVERHEAD_TOKENS, WITNESS_OUTPUT_TOKENS))
        for step in evidence
    ]
    steps += [
        StepEstimate(
            LLMStep.QUESTIONS,
            1,
            _cost(cfg, LLMStep.QUESTIONS, view + QUESTIONS_OVERHEAD_TOKENS, QUESTIONS_OUTPUT_TOKENS),
        ),
        StepEstimate(
            LLMStep.QUESTIONS_CRITIC,
            1,
            _cost(
                cfg,
                LLMStep.QUESTIONS_CRITIC,
                view + PROMPT_OVERHEAD_TOKENS,
                QUESTIONS_CRITIC_OUTPUT_TOKENS,
            ),
        ),
    ]
    if cfg.answers_path is not None:
        steps.append(
            StepEstimate(
                LLMStep.CLARIFY,
                1,
                _cost(cfg, LLMStep.CLARIFY, view + CLARIFY_OVERHEAD_TOKENS, CLARIFY_OUTPUT_TOKENS),
            )
        )
    return RunEstimate(tuple(steps))


def describe_estimate(estimate: RunEstimate) -> str:
    """«около 26 вызовов модели, около $14 по тарифу API»."""
    usd = estimate.usd
    cost = f", около {usd_text(usd, 0)} по тарифу API" if usd is not None and usd >= 1 else ""
    if usd is not None and usd < 1:
        cost = f", около {usd_text(usd)} по тарифу API"
    return f"около {estimate.calls} вызовов модели{cost}"


__all__ = ["RunEstimate", "StepEstimate", "describe_estimate", "estimate_run"]
