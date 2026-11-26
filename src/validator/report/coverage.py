"""Матрица покрытия критериев (ТЗ п. 5): требования × свойства 29148 п. 5.2.5 и набор × 29148 п. 5.2.6.

Матрицу строит код из находок после верификатора, модель в ней не участвует. «Нарушений нет» в ячейке
значит одно: проходы поиска получили рубрику со всеми свойствами и по этому месту ничего не вернули.
Это не доказательство отсутствия дефекта, поэтому, если ни один проход не отработал (остался только
линтер), ячейки без находок помечаются «не проверялось», а не «нарушений нет».
"""

from __future__ import annotations

from collections.abc import Sequence

from validator.models import (
    CellState,
    CoverageCell,
    CoverageMatrix,
    CoverageRow,
    Finding,
    PassResult,
    Property,
    Requirement,
    Scope,
    SetCoverageRow,
    Verdict,
    Verification,
)

REQUIREMENT_PROPERTIES: tuple[Property, ...] = tuple(p for p in Property if p.scope is Scope.REQUIREMENT)
"""Колонки матрицы требований: свойства уровня требования в порядке перечисления."""
SET_PROPERTIES: tuple[Property, ...] = tuple(p for p in Property if p.scope is Scope.SET)
"""Строки матрицы набора: свойства уровня набора (29148 п. 5.2.6, IEEE 830 п. 4.3)."""

SHORT_LABELS: dict[Property, str] = {
    Property.NECESSARY: "Необх.",
    Property.APPROPRIATE: "Умест.",
    Property.UNAMBIGUOUS: "Однозн.",
    Property.COMPLETE: "Полнота",
    Property.SINGULAR: "Единичн.",
    Property.FEASIBLE: "Выполн.",
    Property.VERIFIABLE: "Провер.",
    Property.CORRECT: "Коррект.",
    Property.CONFORMING: "Шаблон",
    Property.TRACEABLE: "Просл.",
    Property.RANKED: "Ранж.",
}
"""Короткие подписи колонок: в Markdown и PDF матрица должна влезать в ширину страницы."""

SYMBOLS: dict[CellState, str] = {
    CellState.CLEAN: "√",
    CellState.VIOLATION: "",  # вместо символа идут номера находок
    CellState.UNCLEAR: "",
    CellState.NOT_CHECKED: "–",
}
"""Знаки ячеек. Только то, что есть в шрифте Liberation: иначе Typst подставил бы чужой шрифт."""

LEGEND = (
    "√ проверено, нарушений нет; F-NN нарушение (номер находки); F-NN? «неясно» (повод уточнить у автора); "
    "– не проверялось."
)

STATE_LABELS: dict[CellState, str] = {
    CellState.CLEAN: "нарушений нет",
    CellState.VIOLATION: "нарушение",
    CellState.UNCLEAR: "неясно",
    CellState.NOT_CHECKED: "не проверялось",
}

NOT_CHECKED_NOTE = "Ни один проход поиска не отработал, проверка свойств выполнена только правилами линтера."


def _state(findings: Sequence[Finding], *, checked: bool) -> CellState:
    if any(f.verdict is Verdict.VIOLATION for f in findings):
        return CellState.VIOLATION
    if findings:
        return CellState.UNCLEAR
    return CellState.CLEAN if checked else CellState.NOT_CHECKED


def cell_text(cell: CoverageCell | SetCoverageRow) -> str:
    """Что стоит в ячейке: знак проверки или номера находок (у «неясно» к номеру добавлен «?»)."""
    if cell.state is CellState.CLEAN or cell.state is CellState.NOT_CHECKED:
        return SYMBOLS[cell.state]
    mark = "?" if cell.state is CellState.UNCLEAR else ""
    return ", ".join(f"{fid}{mark}" for fid in cell.finding_ids)


def _cell_ids(findings: Sequence[Finding]) -> list[str]:
    """Номера находок ячейки: сначала нарушения, потом «неясно», внутри по порядку отчёта."""
    ordered = sorted(findings, key=lambda f: f.verdict is not Verdict.VIOLATION)
    return [f.id for f in ordered]


def build_coverage(
    verification: Verification,
    requirements: Sequence[Requirement],
    passes: Sequence[PassResult],
) -> CoverageMatrix:
    """Матрица по находкам: строка на каждое требование нарезки, строка на каждое свойство набора.

    Строки требований берут находки из ``requirement_status`` верификатора (как таблица раздела 4):
    находка атома R-07.2 видна и у родителя R-07. Находка, свойству которой нет ячейки (свойство набора
    у находки уровня требования или наоборот), попадает в ``extra_finding_ids``: из отчёта она не исчезает.
    """
    checked = any(p.ok for p in passes)
    by_id = {f.id: f for f in verification.findings}
    grid_findings: set[str] = set()

    rows: list[CoverageRow] = []
    for req in requirements:
        linked = [by_id[i] for i in verification.requirement_status.get(req.id, ()) if i in by_id]
        cells: dict[Property, CoverageCell] = {}
        for prop in REQUIREMENT_PROPERTIES:
            hits = [f for f in linked if f.property is prop and f.scope is Scope.REQUIREMENT]
            grid_findings.update(f.id for f in hits)
            cells[prop] = CoverageCell(state=_state(hits, checked=checked), finding_ids=_cell_ids(hits))
        rows.append(CoverageRow(requirement_id=req.id, cells=cells))

    set_rows: list[SetCoverageRow] = []
    for prop in SET_PROPERTIES:
        hits = [f for f in verification.findings if f.property is prop and f.scope is Scope.SET]
        grid_findings.update(f.id for f in hits)
        set_rows.append(
            SetCoverageRow(
                property=prop,
                state=_state(hits, checked=checked),
                finding_ids=_cell_ids(hits),
                refs=list(prop.refs),
            )
        )

    unlinked = [
        f.id
        for f in verification.findings
        if f.id not in grid_findings
        and f.scope is Scope.REQUIREMENT
        and not f.requirement_ids
        and f.property.scope is Scope.REQUIREMENT
    ]
    extra = [f.id for f in verification.findings if f.id not in grid_findings and f.id not in unlinked]
    return CoverageMatrix(
        requirement_properties=list(REQUIREMENT_PROPERTIES),
        rows=rows,
        set_rows=set_rows,
        extra_finding_ids=extra,
        unlinked_finding_ids=unlinked,
        checked=checked,
    )


def coverage_totals(matrix: CoverageMatrix) -> dict[CellState, int]:
    """Сколько ячеек матрицы требований в каждом состоянии: для строки итога под таблицей."""
    totals = dict.fromkeys(CellState, 0)
    for row in matrix.rows:
        for cell in row.cells.values():
            totals[cell.state] += 1
    return totals


def grid_finding_ids(matrix: CoverageMatrix) -> set[str]:
    """Все F-NN, которые видны в матрице: в ячейках, в строках набора и в списке «вне сетки»."""
    ids: set[str] = set(matrix.extra_finding_ids) | set(matrix.unlinked_finding_ids)
    for row in matrix.rows:
        for cell in row.cells.values():
            ids.update(cell.finding_ids)
    for set_row in matrix.set_rows:
        ids.update(set_row.finding_ids)
    return ids
