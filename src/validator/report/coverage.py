"""Матрица покрытия критериев (ТЗ п. 5): требования × свойства 29148 п. 5.2.5 и набор × 29148 п. 5.2.6.

Матрицу строит код из находок после верификатора, модель в ней не участвует. «Нарушений нет» в ячейке
значит одно: проходы поиска получили рубрику со всеми свойствами и по этому месту ничего не вернули.
Это не доказательство отсутствия дефекта, поэтому там, где ни один проход не отработал, ячейки без
находок помечаются «не проверялось», а не «нарушений нет»: весь документ, если не отработал ни один
проход (остался только линтер), или требования части длинного документа, на которой упали все проходы.

Строка требования показывает все его находки, как раздел 4 отчёта: находки уровня требования в
колонках свойств, находки уровня набора (противоречие, повтор, пропуск с участием требования) в колонке
«Набор» (#118). Находки уровня набора идут ещё и в строки матрицы набора по своему свойству.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from validator.models import (
    CellState,
    CoverageCell,
    CoverageMatrix,
    CoverageRow,
    DocumentPart,
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

SET_COLUMN = "Набор"
"""Колонка находок уровня набора в строке требования (#118)."""
SET_COLUMN_LABEL = "Свойства набора с участием требования: противоречия, повторы, пропуски"
SET_COLUMN_REFS = ("ISO/IEC/IEEE 29148:2018, п. 5.2.6", "IEEE 830-1998, п. 4.3")

SYMBOLS: dict[CellState, str] = {
    CellState.CLEAN: "√",
    CellState.VIOLATION: "",  # вместо символа идут номера находок
    CellState.UNCLEAR: "",
    CellState.NOT_CHECKED: "–",
    CellState.PARTIAL: "–",
}
"""Знаки ячеек. Только то, что есть в шрифте Liberation: иначе Typst подставил бы чужой шрифт."""

LEGEND = (
    "√ проверено, нарушений нет; F-NN нарушение (номер находки); F-NN? «неясно» (повод уточнить у автора); "
    "– не проверялось. Колонка «Набор»: находки проверки всего набора, в которых участвует требование "
    "(противоречия, повторы, пропуски), они же в таблице набора ниже."
)

STATE_LABELS: dict[CellState, str] = {
    CellState.CLEAN: "нарушений нет",
    CellState.VIOLATION: "нарушение",
    CellState.UNCLEAR: "неясно",
    CellState.NOT_CHECKED: "не проверялось",
    CellState.PARTIAL: "проверено не полностью",
}

NOT_CHECKED_NOTE = "Ни один проход поиска не отработал, проверка свойств выполнена только правилами линтера."
NO_REQUIREMENTS_NOTE = (
    "Требований в документе не найдено: проверять свойства не на чем, поэтому строки набора без находок "
    "помечены «не проверялось», а не «нарушений нет»."
)


def _state(findings: Sequence[Finding], *, checked: bool, partial: bool = False) -> CellState:
    if any(f.verdict is Verdict.VIOLATION for f in findings):
        return CellState.VIOLATION
    if findings:
        return CellState.UNCLEAR
    if not checked:
        return CellState.NOT_CHECKED
    return CellState.PARTIAL if partial else CellState.CLEAN


def cell_text(cell: CoverageCell | SetCoverageRow) -> str:
    """Что стоит в ячейке: знак проверки или номера находок (у «неясно» к номеру добавлен «?»)."""
    if not cell.finding_ids:
        return SYMBOLS[cell.state]
    mark = "?" if cell.state is CellState.UNCLEAR else ""
    return ", ".join(f"{fid}{mark}" for fid in cell.finding_ids)


def is_requirement_level(f: Finding) -> bool:
    """Находка идёт в колонку свойства требования; иначе это находка уровня набора (колонка «Набор»)."""
    return f.scope is Scope.REQUIREMENT and f.property.scope is Scope.REQUIREMENT


def part_search_text(part: DocumentPart, passes_total: int) -> str:
    """Ячейка «Поиск» в таблице частей длинного документа: сколько проходов отработало по части."""
    if not part.passes_ok:
        return "не проверена моделью"
    return f"{part.passes_ok} из {passes_total}" if passes_total else str(part.passes_ok)


def unchecked_parts_note(matrix: CoverageMatrix, parts: Sequence[DocumentPart]) -> str | None:
    """Пояснение под обозначениями: какие части длинного документа поиском не проверены."""
    missed = [p for p in parts if p.index in matrix.unchecked_parts]
    if not matrix.checked or not missed:
        return None
    total = len(parts)
    shown = "; ".join(
        f"часть {p.index} из {total} ({'; '.join(p.sections) or 'без разделов'}"
        + (f", требования {p.requirement_ids[0]}–{p.requirement_ids[-1]}" if p.requirement_ids else "")
        + ")"
        for p in missed
    )
    return (
        f"Поиском не проверены: {shown}. Ни один проход по ним не отработал (см. диагностику прогона), "
        "поэтому у их требований ячейки без находок помечены «–», а строки набора без находок: "
        "«проверено не полностью»."
    )


def _cell_ids(findings: Sequence[Finding]) -> list[str]:
    """Номера находок ячейки: сначала нарушения, потом «неясно», внутри по порядку отчёта."""
    ordered = sorted(findings, key=lambda f: f.verdict is not Verdict.VIOLATION)
    return [f.id for f in ordered]


def _part_checker(passes: Sequence[PassResult], parts: Sequence[DocumentPart]) -> Callable[[str], bool]:
    """Отработал ли хоть один проход по месту требования: по его части или по всему документу."""
    owner = {rid: p.index for p in parts for rid in p.requirement_ids}
    if not parts:
        whole = any(p.ok for p in passes)
        return lambda _rid: whole
    return lambda rid: any(p.covers(owner.get(rid)) for p in passes)


def _requirement_row(
    req: Requirement, linked: Sequence[Finding], *, searched: bool
) -> tuple[CoverageRow, set[str]]:
    cells: dict[Property, CoverageCell] = {}
    shown: set[str] = set()
    for prop in REQUIREMENT_PROPERTIES:
        hits = [f for f in linked if f.property is prop and is_requirement_level(f)]
        shown.update(f.id for f in hits)
        cells[prop] = CoverageCell(state=_state(hits, checked=searched), finding_ids=_cell_ids(hits))
    # Всё остальное, что раздел 4 показывает у требования, это уровень набора: колонка «Набор».
    rest = [f for f in linked if f.id not in shown]
    shown.update(f.id for f in rest)
    set_cell = CoverageCell(state=_state(rest, checked=searched), finding_ids=_cell_ids(rest))
    row = CoverageRow(requirement_id=req.id, cells=cells, set_cell=set_cell, searched=searched)
    return row, shown


def build_coverage(
    verification: Verification,
    requirements: Sequence[Requirement],
    passes: Sequence[PassResult],
    parts: Sequence[DocumentPart] = (),
) -> CoverageMatrix:
    """Матрица по находкам: строка на каждое требование нарезки, строка на каждое свойство набора.

    Строки требований берут находки из ``requirement_status`` верификатора (как таблица раздела 4):
    находка атома R-07.2 видна и у родителя R-07, находка уровня набора в колонке «Набор». ``parts``:
    части длинного документа с требованиями каждой; у требований части, по которой не отработал ни один
    проход, ячейки без находок «не проверялось». Находка уровня набора без строки (свойство требования)
    идёт в строку набора по ``Property.set_level``. Находка уровня требования без требований попадает в
    ``unlinked_finding_ids``: из отчёта она не исчезает.
    """
    checked = any(p.ok for p in passes)
    unchecked = [p.index for p in parts if not any(x.covers(p.index) for x in passes)] if checked else []
    searched = _part_checker(passes, parts)
    by_id = {f.id: f for f in verification.findings}
    grid_findings: set[str] = set()

    rows: list[CoverageRow] = []
    for req in requirements:
        linked = [by_id[i] for i in verification.requirement_status.get(req.id, ()) if i in by_id]
        row, shown = _requirement_row(req, linked, searched=searched(req.id))
        grid_findings |= shown
        rows.append(row)

    set_level = [f for f in verification.findings if not is_requirement_level(f)]
    set_rows: list[SetCoverageRow] = []
    for prop in SET_PROPERTIES:
        hits = [f for f in set_level if f.property.set_level is prop]
        grid_findings.update(f.id for f in hits)
        set_rows.append(
            SetCoverageRow(
                property=prop,
                state=_state(hits, checked=checked and bool(requirements), partial=bool(unchecked)),
                finding_ids=_cell_ids(hits),
                refs=list(prop.refs),
            )
        )

    unlinked = [
        f.id
        for f in verification.findings
        if f.id not in grid_findings and is_requirement_level(f) and not f.requirement_ids
    ]
    extra = [f.id for f in verification.findings if f.id not in grid_findings and f.id not in unlinked]
    return CoverageMatrix(
        requirement_properties=list(REQUIREMENT_PROPERTIES),
        rows=rows,
        set_rows=set_rows,
        extra_finding_ids=extra,
        unlinked_finding_ids=unlinked,
        checked=checked,
        unchecked_parts=unchecked,
    )


def row_cells(row: CoverageRow) -> list[CoverageCell]:
    """Все ячейки строки требования в порядке таблицы: свойства, затем «Набор»."""
    return [*row.cells.values(), row.set_cell]


def row_finding_ids(row: CoverageRow) -> set[str]:
    """F-NN, которые видны в строке требования: в колонках свойств и в колонке «Набор»."""
    return {fid for cell in row_cells(row) for fid in cell.finding_ids}


def coverage_totals(matrix: CoverageMatrix) -> dict[CellState, int]:
    """Сколько ячеек матрицы требований (с колонкой «Набор») в каждом состоянии: для строки итога."""
    totals = dict.fromkeys(CellState, 0)
    for row in matrix.rows:
        for cell in row_cells(row):
            totals[cell.state] += 1
    return totals


def grid_finding_ids(matrix: CoverageMatrix) -> set[str]:
    """Все F-NN, которые видны в матрице: в ячейках, в строках набора и в списке «вне сетки»."""
    ids: set[str] = set(matrix.extra_finding_ids) | set(matrix.unlinked_finding_ids)
    for row in matrix.rows:
        ids |= row_finding_ids(row)
    for set_row in matrix.set_rows:
        ids.update(set_row.finding_ids)
    return ids
