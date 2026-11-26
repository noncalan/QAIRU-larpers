"""Шаг 8: приоритет и ценность вопроса считает код, а не модель (исследование 17, п. 3.2 и 3.4).

Приоритет вопроса это то, что он блокирует, и он не выше находок, которые закрывает ответ:

- **P0, блокирует разработку:** среди связанных находок есть критичная. Критичность считает код
  (``severity.py``): дефект смысла, который мешает написать тест-кейс, подтверждён моделью, а у
  противоречия сверены оба места. Два исполнителя сделают по-разному, начинать разработку рано;
- **P1, блокирует тестирование:** находка мешает написать тест-кейс, в черновике теста остались
  пропуски, или пример-свидетель показал два прочтения одного сценария (ожидаемый результат не задан);
- **P2, уточнить параллельно:** всё остальное.

Раньше P0 давали и важной находке о поведении API, данных или интеграции, и любому сверенному
свидетелю. Тогда в отчёте все вопросы становились P0 (RC1: 15 из 15 при 8 критичных находках), и метка
переставала отличать блокер от остального. Теперь P0 следует за важностью находки: вопрос не может
блокировать разработку, если ни одна его находка не критична.

Ценность ответа (упрощённый EVPI, исследование 17, п. 3.4) упорядочивает вопросы внутри приоритета:
сумма весов связанных находок по важности, плюс бонус за противоречие, плюс по единице за каждое
затронутое требование сверх первого.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from validator.models import DefectType, Finding, Property, QuestionPriority, Severity, WorkStage

SEVERITY_WEIGHT: dict[Severity, int] = {Severity.CRITICAL: 5, Severity.MAJOR: 3, Severity.MINOR: 1}
"""Вес связанной находки в ценности ответа: критично 5, средне 3, низко 1 (исследование 17, п. 3.4)."""

CONTRADICTION_BONUS = 2
"""Ответ на вопрос о противоречии снимает сразу два расходящихся места."""

EXTRA_REQUIREMENT_WEIGHT = 1
"""За каждое затронутое требование сверх первого."""

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


def proven_witness(f: Finding) -> bool:
    """Свидетель есть и прошёл проверку кода. Отклонённый или не построенный свидетель (шаг 6а) не повод
    для вопроса: его цитаты не сверены или места документа не расходятся."""
    return f.witness is not None and f.witness.checked


def draft_gaps(f: Finding) -> tuple[str, ...]:
    """Пропуски ``[?]`` черновика тест-кейса: каких значений не хватает для ожидаемого результата."""
    draft = f.test_draft
    if draft is None or draft.complete:
        return ()
    return tuple(g.strip() for g in draft.gaps if g.strip())


def requirement_ids(findings: Iterable[Finding]) -> list[str]:
    """Затронутые требования без повторов, в порядке находок."""
    return list(dict.fromkeys(r for f in findings for r in f.requirement_ids))


def _blocks_testing(f: Finding) -> bool:
    # Сверенный свидетель (два прочтения одного сценария) не задаёт ожидаемого результата теста.
    # Отклонённый кодом свидетель (шаг 6а) тест не держит: его сценарий не подтверждён документом.
    return f.blocks_testing or bool(draft_gaps(f)) or proven_contradiction(f)


def priority(findings: Sequence[Finding]) -> QuestionPriority:
    """P0 при критичной находке, P1 если находка мешает тесту, иначе P2 (правило в docstring модуля)."""
    if any(f.severity is Severity.CRITICAL for f in findings):
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
