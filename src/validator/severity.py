"""Важность находки: таблица «тип дефекта × мешает ли написать тест-кейс» и ограничения кода (шаг 7).

Модель даёт только подсказку важности (``severity_hint``), окончательную важность считает код:

1. ``SEVERITY_TABLE``: важность по типу дефекта и тому, мешает ли он написать тест-кейс;
2. ограничения: «неясно» не выше «важно»; противоречие, у которого сверено одно место, не выше «важно»;
3. решение критика (шаг 6) «понизить»: важность только снижается.

Повысить важность может только таблица. Если подсказка модели расходится с таблицей больше чем на
уровень, находка получает пометку (``severity_disputed``): по ней критик выбирает, что перепроверить.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, NamedTuple

from validator.models import CriticAction, DefectType, MergedFinding, Quote, Severity, Verdict

# ---------------------------------------------------------------- таблица и ограничения

_MEANING = (Severity.MAJOR, Severity.CRITICAL)
_FORM = (Severity.MINOR, Severity.MAJOR)

SEVERITY_TABLE: Final[Mapping[DefectType, tuple[Severity, Severity]]] = MappingProxyType(
    {
        # Пара значений: (не мешает написать тест-кейс, мешает). Откуда правило:
        # - важность считаем по типу дефекта и влиянию на тестирование, а не по свойству стандарта
        #   (docs/ideas/final-idea.md, «Правила, которые держим во всём конвейере»);
        # - «критично»: по документу нельзя однозначно разработать или протестировать сценарий, два
        #   исполнителя сделают по-разному (шкала исследования 01, п. 4.3, она же в промпте поиска);
        # - ТЗ п. 6 называет критичными непроверяемые, противоречивые и неполные формулировки.
        #
        # Дефекты смысла: из текста не выводится одно поведение. Мешают тесту: «критично», иначе «важно».
        DefectType.CONTRADICTION: _MEANING,
        DefectType.AMBIGUOUS: _MEANING,
        DefectType.INCOMPLETE: _MEANING,
        DefectType.UNVERIFIABLE: _MEANING,
        DefectType.INFEASIBLE: _MEANING,
        DefectType.INCORRECT: _MEANING,
        # Дефекты формы: поведение понятно, страдают единообразие и сопровождение документа
        # (исследование 01, п. 4.3: оформление, прослеживаемость, дубли без расхождения, пустые служебные
        # поля это «низко»). Если такой дефект всё же мешает тесту, он «важно», но не «критично»: когда
        # два места дают разные ожидаемые результаты, это уже противоречие, и поиск пишет его отдельно.
        DefectType.TERMINOLOGY: _FORM,
        DefectType.PLACEHOLDER: _FORM,
        DefectType.REDUNDANCY: _FORM,
        DefectType.NOT_ATOMIC: _FORM,
        DefectType.DESIGN_DETAIL: _FORM,
        DefectType.TRACEABILITY: _FORM,
        # Текст, обращённый к проверяющей модели: тестам не мешает, но искажает автоматическую проверку
        # и не должен жить в документе требований. Всегда «важно».
        DefectType.INJECTED_INSTRUCTION: (Severity.MAJOR, Severity.MAJOR),
    }
)
"""Важность по типу дефекта и тому, мешает ли он написать тест-кейс (``blocks_testing``)."""

UNCLEAR_MAX_SEVERITY: Final = Severity.MAJOR
"""«Неясно» это вопрос автору, а не подтверждённое нарушение: не выше «важно» и в красный свет не идёт."""

CONTRADICTION_MIN_PLACES: Final = 2
"""Противоречие подтверждают два сверенных места документа; с одним местом важность не выше «важно»."""

UNPROVEN_CONTRADICTION_MAX_SEVERITY: Final = Severity.MAJOR

SEVERITY_DISPUTE_GAP: Final = 2
"""Подсказка модели и важность кода расходятся «больше чем на уровень»: «критично» против «незначительно»."""

_BY_RANK: Final = sorted(Severity, key=lambda s: s.rank)


# ---------------------------------------------------------------- расчёт


def table_severity(defect_type: DefectType, blocks_testing: bool) -> Severity:
    """Важность по таблице ``SEVERITY_TABLE``, до ограничений и решения критика."""
    return SEVERITY_TABLE[defect_type][int(blocks_testing)]


def severity_disputed(hint: Severity | None, severity: Severity) -> bool:
    """Подсказка модели расходится с важностью кода больше чем на уровень.

    Шаг 6 (критик) вызывает её до верификатора, чтобы выбрать находки на перепроверку:
    ``severity_disputed(m.severity_hint, table_severity(m.defect_type, m.blocks_testing))``.
    """
    return hint is not None and abs(hint.rank - severity.rank) >= SEVERITY_DISPUTE_GAP


def _milder(a: Severity, b: Severity) -> Severity:
    return a if a.rank >= b.rank else b


def _one_level_down(severity: Severity) -> Severity:
    return _BY_RANK[min(severity.rank + 1, len(_BY_RANK) - 1)]


def distinct_places(quotes: Sequence[Quote]) -> int:
    """Сколько разных мест документа подтверждают цитаты: пересекающиеся якоря считаем одним местом."""
    spans = sorted((q.anchor.char_start, q.anchor.char_end) for q in quotes if q.anchor)
    places, end = 0, -1
    for start, stop in spans:
        if start >= end:
            places += 1
        end = max(end, stop)
    return places


_Step = tuple[Severity, str | None, str | None]
"""Шаг расчёта важности: новая важность, строка для ``severity_basis``, пометка для отчёта."""


def _unclear_cap(m: MergedFinding, verified: Sequence[Quote], severity: Severity) -> _Step:
    del verified
    if m.verdict is not Verdict.UNCLEAR or severity.rank >= UNCLEAR_MAX_SEVERITY.rank:
        return severity, None, None
    return UNCLEAR_MAX_SEVERITY, f"вердикт «неясно»: не выше «{UNCLEAR_MAX_SEVERITY.label}»", None


def _contradiction_cap(m: MergedFinding, verified: Sequence[Quote], severity: Severity) -> _Step:
    capped = _milder(severity, UNPROVEN_CONTRADICTION_MAX_SEVERITY)
    if m.defect_type is not DefectType.CONTRADICTION or capped is severity:
        return severity, None, None
    if distinct_places(verified) >= CONTRADICTION_MIN_PLACES:
        return severity, None, None
    return (
        capped,
        f"сверено одно место противоречия: не выше «{capped.label}»",
        f"В документе сверено только одно место противоречия, поэтому важность не выше «{capped.label}».",
    )


def _critic_downgrade(m: MergedFinding, verified: Sequence[Quote], severity: Severity) -> _Step:
    del verified
    critic = m.critic
    if critic is None or critic.action is not CriticAction.DOWNGRADE:
        return severity, None, None
    lowered = _milder(severity, critic.severity) if critic.severity else _one_level_down(severity)
    if lowered is severity:
        return severity, None, None
    note = f"Критик понизил важность с «{severity.label}» до «{lowered.label}». Причина: {critic.reason}"
    return lowered, f"критик понизил до «{lowered.label}»", note


_SEVERITY_STEPS = (_unclear_cap, _contradiction_cap, _critic_downgrade)
"""Порядок важен: ограничения кода, затем критик. Повысить важность может только таблица."""


class SeverityResult(NamedTuple):
    severity: Severity
    basis: str
    """Как посчитано, для ``Finding.severity_basis``: «тип «…», мешает написать тест-кейс: «критично»; …»."""
    notes: list[str]
    """Пометки для карточки находки в отчёте."""
    disputed: bool
    """Подсказка модели расходится с таблицей больше чем на уровень."""


def severity_for(m: MergedFinding, verified: Sequence[Quote]) -> SeverityResult:
    """Важность находки после сверки цитат: таблица, ограничения кода, решение критика.

    ``verified``: сверенные цитаты находки (нужны правилу о двух местах противоречия).
    """
    table = table_severity(m.defect_type, m.blocks_testing)
    test = "мешает" if m.blocks_testing else "не мешает"
    basis = [f"тип «{m.defect_type.label}», {test} написать тест-кейс: «{table.label}»"]
    notes: list[str] = []
    severity = table
    for step in _SEVERITY_STEPS:
        severity, why, note = step(m, verified, severity)
        basis.extend([why] if why else [])
        notes.extend([note] if note else [])
    disputed = severity_disputed(m.severity_hint, table)
    if disputed and m.severity_hint is not None:
        final = "" if severity is table else f", итог после правил кода «{severity.label}»"
        notes.insert(
            0,
            f"Модель оценила важность как «{m.severity_hint.label}», а таблица кода по типу дефекта и "
            f"влиянию на тест дала «{table.label}»{final}: оценки расходятся больше чем на уровень.",
        )
    return SeverityResult(severity, "; ".join(basis), notes, disputed)


__all__ = [
    "CONTRADICTION_MIN_PLACES",
    "SEVERITY_DISPUTE_GAP",
    "SEVERITY_TABLE",
    "UNCLEAR_MAX_SEVERITY",
    "SeverityResult",
    "distinct_places",
    "severity_disputed",
    "severity_for",
    "table_severity",
]
