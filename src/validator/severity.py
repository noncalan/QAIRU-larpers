"""Важность находки: таблица «тип дефекта × мешает ли написать тест-кейс» и ограничения кода (шаг 7).

Модель даёт только подсказку важности (``severity_hint``), окончательную важность считает код:

1. ``SEVERITY_TABLE``: важность по типу дефекта и тому, мешает ли он написать тест-кейс;
2. ограничения: «критично» по таблице с подсказкой модели ниже «критично» не выше «важно» (без подсказки
   решает таблица); находка с подсказкой модели «низко» и дефект формы не выше «незначительно», если
   находку не подтвердил код (правило линтера, которое само мешает тесту, или проверенный
   пример-свидетель); «неясно» не выше «важно»; противоречие, у которого сверено одно место, не выше
   «важно»;
3. решение критика (шаг 6) «понизить»: важность только снижается.

Повысить важность может только таблица: подсказка модели её только подтверждает или ограничивает.
Если подсказка расходится с таблицей или с итогом после ограничений больше чем на уровень, находка
получает пометку (``severity_disputed``): по ней критик выбирает, что перепроверить.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Final, NamedTuple

from validator.lint.rules import RULE_BY_ID
from validator.models import CriticAction, DefectType, MergedFinding, Quote, Severity, SourceKind, Verdict

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

UNCONFIRMED_CRITICAL_MAX_SEVERITY: Final = Severity.MAJOR
"""Потолок для «критично» по таблице, если модель дала подсказку ниже «критично».

Таблица отвечает на вопрос «мешает ли тесту», а «критично» по шкале промпта поиска это только основной
сценарий, деньги, безопасность и персональные данные. Промпт ставит «Тест: мешает» большинству находок
(эксперимент 22: 31–38 из 47–52 за прогон), поэтому одна таблица дала бы десятки «критично». Подсказка
модели здесь не повышает, а только не даёт «критично» без её согласия. Нет подсказки: решает таблица.
"""

FORM_DEFECTS: Final = frozenset(t for t, levels in SEVERITY_TABLE.items() if levels == _FORM)
"""Дефекты формы по таблице: терминология, незаполненное место, дубль, составное требование и т. п."""

UNCONFIRMED_FORM_MAX_SEVERITY: Final = Severity.MINOR
"""Потолок для дефекта формы, который не подтвердил код (``_confirmed_by_code``).

Отметку «мешает тесту» модель ставит большинству находок (см. выше), поэтому для дефекта формы её одной
мало. Разметка двумя моделями вслепую (#49, пять прогонов с одним проходом, эксперимент 43): из 20 таких
«важных» находок настоящими признаны 2. Правило линтера ставит «мешает тесту» кодом (например, маркер
«TBD» в требовании), и его важность остаётся по таблице.
"""

LOW_HINT_MAX_SEVERITY: Final = Severity.MINOR
"""Потолок, если модель сама дала подсказку важности «низко», а код находку не подтвердил.

Шкала промпта поиска: «низко» это оформление, прослеживаемость, приоритеты, дубли без расхождения. Если
автор находки так её оценил, код не поднимает её выше по одной отметке «мешает тесту», в том числе с
«критично» по таблице. Разметка #49 (эксперимент 43): из 23 «важных» находок с подсказкой «низко»
настоящими признаны 4.
"""

LOW_HINT_LABEL: Final = "низко"
"""Как подсказку «низко» называет шкала промпта поиска (в отчёте этот уровень зовётся «незначительно»)."""

HINT_PROOF_DEFECTS: Final = frozenset({DefectType.INJECTED_INSTRUCTION})
"""Типы, важность которых подсказка модели не снижает: текст, обращённый к проверяющей модели, может сам
попросить её поставить «низко»."""

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


def _confirmed_by_code(m: MergedFinding) -> bool:
    """Находку подтвердил код, и мнение модели её важность не снижает.

    Подтверждение: правило линтера, которое само ставит «мешает тесту» (срабатывание правила без этой
    отметки ничего о тесте не говорит), или пример-свидетель, сверенный кодом (два разных ожидаемых
    результата одного сценария, шаг 6а).
    """
    if m.witness is not None and m.witness.checked:
        return True
    rules = (RULE_BY_ID.get(src.rule_id or "") for src in m.sources if src.kind is SourceKind.LINT)
    return any(rule is not None and rule.blocks_testing for rule in rules)


def _low_hint_cap(m: MergedFinding, verified: Sequence[Quote], severity: Severity) -> _Step:
    del verified
    capped = _milder(severity, LOW_HINT_MAX_SEVERITY)
    if m.severity_hint is not Severity.MINOR or capped is severity:
        return severity, None, None
    if m.defect_type in HINT_PROOF_DEFECTS or _confirmed_by_code(m):
        return severity, None, None
    return (
        capped,
        f"подсказка модели «{LOW_HINT_LABEL}», кодом не подтверждено: не выше «{capped.label}»",
        None,
    )


def _hint_cap(m: MergedFinding, verified: Sequence[Quote], severity: Severity) -> _Step:
    del verified
    hint = m.severity_hint
    if severity is not Severity.CRITICAL or hint is None or hint is Severity.CRITICAL:
        return severity, None, None
    capped = UNCONFIRMED_CRITICAL_MAX_SEVERITY
    return capped, f"подсказка модели «{hint.label}»: не выше «{capped.label}»", None


def _form_cap(m: MergedFinding, verified: Sequence[Quote], severity: Severity) -> _Step:
    del verified
    capped = _milder(severity, UNCONFIRMED_FORM_MAX_SEVERITY)
    if m.defect_type not in FORM_DEFECTS or capped is severity or _confirmed_by_code(m):
        return severity, None, None
    return capped, f"дефект формы, кодом не подтверждён: не выше «{capped.label}»", None


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


_SEVERITY_STEPS = (_low_hint_cap, _hint_cap, _form_cap, _unclear_cap, _contradiction_cap, _critic_downgrade)
"""Порядок важен: ограничения кода, затем критик. Повысить важность может только таблица.

Подсказка «низко» идёт первой: после неё правилу про «критично» уже нечего ограничивать, и в основании
важности остаётся одна строка про подсказку."""


class SeverityResult(NamedTuple):
    severity: Severity
    basis: str
    """Как посчитано, для ``Finding.severity_basis``: «тип «…», мешает написать тест-кейс: «критично»; …»."""
    notes: list[str]
    """Пометки для карточки находки в отчёте."""
    disputed: bool
    """Подсказка модели расходится с таблицей или с итогом после ограничений больше чем на уровень."""


def severity_for(m: MergedFinding, verified: Sequence[Quote]) -> SeverityResult:
    """Важность находки после сверки цитат: таблица, ограничения кода, решение критика.

    ``verified``: сверенные цитаты находки (нужны правилу о двух местах противоречия).
    """
    table = table_severity(m.defect_type, m.blocks_testing)
    test = "мешает" if m.blocks_testing else "не мешает"
    basis = [f"тип «{m.defect_type.label}», {test} написать тест-кейс: «{table.label}»"]
    notes: list[str] = []
    severity = table
    by_code = table
    for step in _SEVERITY_STEPS:
        severity, why, note = step(m, verified, severity)
        basis.extend([why] if why else [])
        notes.extend([note] if note else [])
        if step is not _critic_downgrade:
            by_code = severity
    # Расхождение считаем до решения критика: критик понижает уже после того, как выбрал находку по пометке.
    disputed = severity_disputed(m.severity_hint, table) or severity_disputed(m.severity_hint, by_code)
    if disputed and m.severity_hint is not None:
        # Расхождение оценок служебное: оно в report.json (основание важности и флаг), не в карточке (#125).
        basis.append(f"модель оценила «{m.severity_hint.label}»: оценки расходятся больше чем на уровень")
    return SeverityResult(severity, "; ".join(basis), notes, disputed)


__all__ = [
    "CONTRADICTION_MIN_PLACES",
    "FORM_DEFECTS",
    "HINT_PROOF_DEFECTS",
    "LOW_HINT_MAX_SEVERITY",
    "SEVERITY_DISPUTE_GAP",
    "SEVERITY_TABLE",
    "UNCLEAR_MAX_SEVERITY",
    "UNCONFIRMED_CRITICAL_MAX_SEVERITY",
    "UNCONFIRMED_FORM_MAX_SEVERITY",
    "SeverityResult",
    "distinct_places",
    "severity_disputed",
    "severity_for",
    "table_severity",
]
