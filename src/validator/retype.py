"""Противоречие без примера-свидетеля становится неполнотой (#124).

Свидетель «не построен» (шаг 6а) значит: модель не нашла сценария, в котором места документа дают разные
ожидаемые результаты, одно место уточняет другое. Называть такую находку противоречием нельзя, а то, что
осталось недосказанным (когда действует общее правило), это неполнота того же уровня.

Тип меняется последним шагом, после всех вызовов модели: входы попытки теста, вопросов и уточнений остаются
прежними, записанные прогоны воспроизводятся без перезаписи. Важность не пересчитывается: в таблице
``severity`` строки «противоречие» и «неполнота» одинаковые, а потолок «сверено одно место противоречия»
уже применён и остаётся. Меняются тип, свойство стандарта, заметка свидетеля и счётчики сводки.

Отклонённый кодом свидетель (выдуманная цитата, одинаковые ожидания) тип не меняет: модель нашла
расхождение, а код не смог его подтвердить.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable
from enum import Enum
from typing import Final, TypeVar

from validator.models import DefectType, Finding, Property, Scope, Verification, Witness

logger = logging.getLogger("validator")

DECLINED_CONTRADICTION_TYPE: Final = DefectType.INCOMPLETE
_COMPLETE_BY_SCOPE: Final = {Scope.SET: Property.SET_COMPLETE, Scope.REQUIREMENT: Property.COMPLETE}

_E = TypeVar("_E", bound=Enum)


def declined_contradiction(f: Finding) -> bool:
    """Противоречие, для которого модель свидетеля ответила «сценария нет» (пустой сценарий, не проверен)."""
    w = f.witness
    return f.defect_type is DefectType.CONTRADICTION and w is not None and not w.checked and not w.scenario


def retype_finding(f: Finding) -> Finding:
    """Неполнота того же уровня: свойство набора → полнота набора, требования → полнота требования."""
    witness = f.witness or Witness(scenario="")
    note = (
        f"{witness.note} Тип изменён: «{f.defect_type.label}» → «{DECLINED_CONTRADICTION_TYPE.label}», "
        "потому что места документа в одном сценарии не расходятся."
    )
    return f.model_copy(
        update={
            "defect_type": DECLINED_CONTRADICTION_TYPE,
            "property": _COMPLETE_BY_SCOPE[f.property.scope],
            "witness": witness.model_copy(update={"note": note.strip()}),
        }
    )


def _count(values: Iterable[_E], order: Iterable[_E]) -> dict[_E, int]:
    """Как ``verify.summarize``: в порядке перечисления, без нулей."""
    counts = Counter(values)
    return {key: counts[key] for key in order if counts[key]}


def retype_unwitnessed(ver: Verification) -> Verification:
    """Отчёт, где противоречия без сценария свидетеля названы неполнотой; остальное как было."""
    if not any(declined_contradiction(f) for f in ver.findings):
        return ver
    findings = [retype_finding(f) if declined_contradiction(f) else f for f in ver.findings]
    logger.info(
        "Противоречий без сценария свидетеля переведено в неполноту: %d",
        sum(declined_contradiction(f) for f in ver.findings),
    )
    summary = ver.summary.model_copy(
        update={
            "by_property": _count((f.property for f in findings), Property),
            "by_defect_type": _count((f.defect_type for f in findings), DefectType),
        }
    )
    return ver.model_copy(update={"findings": findings, "summary": summary})


__all__ = ["DECLINED_CONTRADICTION_TYPE", "declined_contradiction", "retype_finding", "retype_unwitnessed"]
