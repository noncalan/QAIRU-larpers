"""#124: противоречие, для которого модель свидетеля не нашла сценария, в отчёте называется неполнотой.

Находки собраны вручную: шаг работает только с готовым отчётом верификатора, без модели.
"""

from __future__ import annotations

from typing import Any

from validator.models import (
    DefectType,
    Finding,
    Property,
    Scope,
    Severity,
    Summary,
    Verdict,
    Verification,
    Witness,
    WitnessReading,
)
from validator.retype import retype_unwitnessed

DECLINED = Witness(scenario="", checked=False, note="Свидетель не построен: второе место уточняет первое.")
REJECTED = Witness(
    scenario="Встреча в 15:00, отмена в 14:30",
    readings=[
        WitnessReading(quote="за час", expectation="отказ"),
        WitnessReading(quote="в любое время", expectation="отказ"),
    ],
    checked=False,
    note="Код отклонил свидетеля: ожидания совпадают или не указаны.",
)
CHECKED = REJECTED.model_copy(update={"checked": True, "note": ""})


def finding(n: int, **fields: Any) -> Finding:
    base: dict[str, Any] = {
        "id": f"F-{n:02d}",
        "merged_id": f"M-{n:03d}",
        "property": Property.SET_CONSISTENT,
        "defect_type": DefectType.CONTRADICTION,
        "verdict": Verdict.VIOLATION,
        "scope": Scope.SET,
        "severity": Severity.MAJOR,
        "explanation": "Объяснение.",
    }
    return Finding.model_validate({**base, **fields})


def verification(*found: Finding) -> Verification:
    summary = Summary(
        by_property={Property.SET_CONSISTENT: len(found)},
        by_defect_type={DefectType.CONTRADICTION: len(found)},
    )
    return Verification(findings=list(found), summary=summary)


def test_contradiction_without_scenario_becomes_set_incompleteness() -> None:
    out = retype_unwitnessed(verification(finding(1, witness=DECLINED)))

    f = out.findings[0]
    assert f.defect_type is DefectType.INCOMPLETE and f.property is Property.SET_COMPLETE
    assert f.severity is Severity.MAJOR
    assert f.witness is not None and "второе место уточняет первое" in f.witness.note
    assert "«противоречие» → «неполнота»" in f.witness.note
    assert out.summary.by_defect_type == {DefectType.INCOMPLETE: 1}
    assert out.summary.by_property == {Property.SET_COMPLETE: 1}


def test_requirement_level_contradiction_becomes_requirement_incompleteness() -> None:
    out = retype_unwitnessed(verification(finding(1, property=Property.CORRECT, witness=DECLINED)))

    assert out.findings[0].property is Property.COMPLETE


def test_rejected_checked_or_missing_witness_keeps_contradiction() -> None:
    ver = verification(finding(1, witness=REJECTED), finding(2, witness=CHECKED), finding(3))

    out = retype_unwitnessed(ver)

    assert out == ver


def test_other_types_without_scenario_keep_their_type() -> None:
    ambiguous = finding(1, defect_type=DefectType.AMBIGUOUS, property=Property.UNAMBIGUOUS, witness=DECLINED)

    assert retype_unwitnessed(verification(ambiguous)).findings[0].defect_type is DefectType.AMBIGUOUS
