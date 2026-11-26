"""Метрики прогона (ТЗ идеи, п. 9): вызовы, время и цена по шагам, воронка находок, «найдено в N из K».

Всё считает код по записям вызовов и по находкам; модель в цифрах не участвует.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from validator.models import (
    CallRecord,
    CallStatus,
    CriticAction,
    DiscardReason,
    Finding,
    FoundInBucket,
    LLMStep,
    StepMetrics,
    TokenUsage,
    Verification,
)

STEP_LABELS: dict[str, str] = {
    LLMStep.SEGMENT.value: "Нарезка на требования",
    LLMStep.SEARCH.value: "Поиск дефектов (проходы)",
    LLMStep.SEARCH_TRANSFER.value: "Перенос находок в JSON",
    LLMStep.EVIDENCE.value: "Пример-свидетель",
    LLMStep.CRITIC.value: "Критик находок",
    LLMStep.TEST_DRAFT.value: "Попытка тест-кейса",
    LLMStep.QUESTIONS.value: "Вопросы автору",
    LLMStep.QUESTIONS_CRITIC.value: "Критик вопросов",
    LLMStep.JUDGE.value: "Судья eval",
}
"""Шаги по-русски; порядок словаря это порядок шагов конвейера."""

_STEP_ORDER = {step: n for n, step in enumerate(STEP_LABELS)}


def step_label(step: str) -> str:
    return STEP_LABELS.get(step, step)


def step_metrics(calls: Sequence[CallRecord]) -> list[StepMetrics]:
    """Вызовы по шагам в порядке конвейера. Повтор после сбоя не отдельный вызов (как в паспорте)."""
    by_step: dict[str, list[CallRecord]] = {}
    for call in calls:
        by_step.setdefault(call.step.value, []).append(call)
    rows: list[StepMetrics] = []
    for step in sorted(by_step, key=lambda s: _STEP_ORDER.get(s, len(_STEP_ORDER))):
        records = by_step[step]
        costs = [c.cost_usd for c in records if c.cost_usd is not None]
        rows.append(
            StepMetrics(
                step=step,
                calls=sum(c.status is not CallStatus.RETRIED for c in records),
                failed=sum(c.status is CallStatus.ERROR for c in records),
                cached=sum(c.status is CallStatus.CACHED for c in records),
                replayed=sum(c.status is CallStatus.REPLAYED for c in records),
                duration_s=round(sum(c.duration_ms or 0 for c in records) / 1000, 1),
                tokens=sum((c.usage for c in records), TokenUsage()),
                cost_usd=round(sum(costs), 4) if costs else None,
            )
        )
    return rows


def found_in_distribution(findings: Sequence[Finding]) -> list[FoundInBucket]:
    """«Найдено в N из K проходов»: сколько находок отчёта в каждой корзине, сначала самые устойчивые."""
    counts = Counter((f.found_in, f.of) for f in findings)
    return [
        FoundInBucket(found_in=found, of=of, count=count)
        for (found, of), count in sorted(counts.items(), key=lambda item: (-item[0][0], -item[0][1]))
    ]


def discarded_by_reason(verification: Verification | None) -> dict[DiscardReason, int]:
    if verification is None:
        return {}
    counts = Counter(d.reason for d in verification.discarded)
    return {reason: counts[reason] for reason in DiscardReason if counts[reason]}


def critic_counts(verification: Verification | None) -> tuple[int, int, int]:
    """Критик находок: просмотрено, понижено (в отчёте), снято. Нули, если критик выключен (#39)."""
    if verification is None:
        return 0, 0, 0
    kept = [f for f in verification.findings if f.critic is not None]
    dropped = sum(d.reason is DiscardReason.CRITIC_REJECTED for d in verification.discarded)
    downgraded = sum(f.critic is not None and f.critic.action is CriticAction.DOWNGRADE for f in kept)
    return len(kept) + dropped, downgraded, dropped
