"""Шаг 8: приоритет и ценность вопроса считает код, а не модель (исследование 17, п. 3.2 и 3.4).

Приоритет вопроса это то, что он блокирует:

- **P0, блокирует разработку:** среди связанных находок есть критичная; или противоречие доказано
  примером-свидетелем; или важная находка о поведении в требовании к API, данным или интеграции
  (две реализации по разным ответам несовместимы);
- **P1, блокирует тестирование:** находка мешает написать тест-кейс, в черновике теста остались пропуски
  или пример-свидетель не задаёт результата;
- **P2, уточнить параллельно:** всё остальное.

Ценность ответа (упрощённый EVPI, исследование 17, п. 3.4) упорядочивает вопросы внутри приоритета:
сумма весов связанных находок по важности, плюс бонус за противоречие, плюс по единице за каждое
затронутое требование сверх первого.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from validator.models import (
    DefectType,
    Finding,
    Property,
    QuestionPriority,
    RequirementType,
    Severity,
    WorkStage,
)

SEVERITY_WEIGHT: dict[Severity, int] = {Severity.CRITICAL: 5, Severity.MAJOR: 3, Severity.MINOR: 1}
"""Вес связанной находки в ценности ответа: критично 5, средне 3, низко 1 (исследование 17, п. 3.4)."""

CONTRADICTION_BONUS = 2
"""Ответ на вопрос о противоречии снимает сразу два расходящихся места."""

EXTRA_REQUIREMENT_WEIGHT = 1
"""За каждое затронутое требование сверх первого."""

CONTRACT_TYPES = frozenset({RequirementType.API, RequirementType.DATA, RequirementType.INTEGRATION})
"""Требования, где разные ответы дают несовместимые реализации: контракт, модель данных, обмен."""

BEHAVIOR_DEFECTS = frozenset(
    {DefectType.CONTRADICTION, DefectType.AMBIGUOUS, DefectType.INCOMPLETE, DefectType.UNVERIFIABLE}
)
"""Дефекты, при которых неизвестно само поведение системы, а не только формулировка."""

_STAGES: dict[QuestionPriority, list[WorkStage]] = {
    QuestionPriority.P0: [WorkStage.DEVELOPMENT, WorkStage.TESTING],
    QuestionPriority.P1: [WorkStage.TESTING],
    QuestionPriority.P2: [],
}
_PRIORITY_RANK = {QuestionPriority.P0: 0, QuestionPriority.P1: 1, QuestionPriority.P2: 2}


def is_contradiction(f: Finding) -> bool:
    return f.defect_type is DefectType.CONTRADICTION or f.property is Property.SET_CONSISTENT


def proven_contradiction(f: Finding) -> bool:
    """Противоречие с примером-свидетелем, который код сверил: два прочтения одного сценария."""
    return f.witness is not None and f.witness.checked and len(f.witness.readings) >= 2


def draft_gaps(f: Finding) -> tuple[str, ...]:
    """Пропуски ``[?]`` черновика тест-кейса: каких значений не хватает для ожидаемого результата."""
    draft = f.test_draft
    if draft is None or draft.complete:
        return ()
    return tuple(g.strip() for g in draft.gaps if g.strip())


def requirement_ids(findings: Iterable[Finding]) -> list[str]:
    """Затронутые требования без повторов, в порядке находок."""
    return list(dict.fromkeys(r for f in findings for r in f.requirement_ids))


def _touches_contract(f: Finding, req_types: Mapping[str, RequirementType]) -> bool:
    if f.severity.rank > Severity.MAJOR.rank or f.defect_type not in BEHAVIOR_DEFECTS:
        return False
    return any(req_types.get(r) in CONTRACT_TYPES for r in f.requirement_ids)


def _blocks_testing(f: Finding) -> bool:
    witness_open = f.witness is not None and not f.witness.checked
    return f.blocks_testing or bool(draft_gaps(f)) or witness_open


def priority(findings: Sequence[Finding], req_types: Mapping[str, RequirementType]) -> QuestionPriority:
    if any(
        f.severity is Severity.CRITICAL or proven_contradiction(f) or _touches_contract(f, req_types)
        for f in findings
    ):
        return QuestionPriority.P0
    if any(_blocks_testing(f) for f in findings):
        return QuestionPriority.P1
    return QuestionPriority.P2


def value(findings: Sequence[Finding]) -> int:
    weight = sum(SEVERITY_WEIGHT[f.severity] for f in findings)
    bonus = CONTRADICTION_BONUS if any(is_contradiction(f) for f in findings) else 0
    extra = max(0, len(requirement_ids(findings)) - 1) * EXTRA_REQUIREMENT_WEIGHT
    return weight + bonus + extra


def priority_rank(p: QuestionPriority) -> int:
    return _PRIORITY_RANK[p]


def blocks(p: QuestionPriority) -> list[WorkStage]:
    return list(_STAGES[p])


def standard_refs(findings: Iterable[Finding]) -> list[str]:
    """Пункты стандартов, которые закрывает ответ: из свойств связанных находок, без повторов."""
    return list(dict.fromkeys(ref for f in findings for ref in f.property.refs))
