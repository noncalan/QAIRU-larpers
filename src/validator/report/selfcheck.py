"""Самопроверка отчёта по ТЗ п. 6 (#43): отчёт сам доказывает, что он полный и внутренне сходится.

Проверки идут по готовому ``Report`` и по его Markdown: что видит читатель, то и проверяем. Модуль
только проверяет и сообщает. Отчёт задним числом он не правит: если счётчик не сошёлся, это видно в
паспорте, в разделе «Самопроверка отчёта» и в диагностике, а не спрятано правкой.

Заново считаются светофор и вывод, первые правки и вопросы, разбивка по важности и число цитат.
По документу сверяются места якорей, цитаты и исходные ID требований. Ссылки ведут на объекты
отчёта, а числа и код завершения в паспорте сверяются с журналом вызовов и диагностикой.

Проверки делятся на два рода (``SelfCheckItem.integrity``):

- целостность: в отчёт попало неверное (находка без якоря или сверенной цитаты, счётчик сводки не равен
  числу строк, требование выпало из таблицы, находка без карточки, матрица не по находкам). Диагностика
  ``INT-003``, код возврата 1;
- полнота формы: нет раздела ТЗ п. 6, а то, что есть, верно. Диагностика ``INT-004``, код не меняется.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import TypeVar

from validator.clarify import snapshot
from validator.errors import INT_SELFCHECK_FAILED, INT_SELFCHECK_WARNING, exit_code
from validator.models import (
    Anchor,
    Block,
    CellState,
    ClarifyEffect,
    CoverageMatrix,
    DefectType,
    DiagLevel,
    Diagnostic,
    ExitCode,
    Finding,
    ParsedDocument,
    Property,
    Report,
    ReportStatus,
    Scope,
    Segmentation,
    SelfCheck,
    SelfCheckItem,
    Severity,
    SourceKind,
    TrafficLight,
    Verdict,
    Verification,
)
from validator.report.build import _ask_first, _fix_first  # Именно их вызывает build_report.
from validator.report.coverage import (
    SYMBOLS,
    grid_finding_ids,
    is_requirement_level,
    row_cells,
    row_finding_ids,
)
from validator.report.fmt import pct, status_label
from validator.report.render_md import NO_VIOLATIONS, NOT_SEARCHED, render_md
from validator.text import span_anchor
from validator.verify import light_with_questions, requirement_status, summarize, traffic_light

TZ_SECTIONS: tuple[str, ...] = (
    "Паспорт прогона",
    "Как агент нарезал требования",
    "Проверка каждого требования",
    "Проверка всего набора требований",
    "Сводка",
    "Итог",
)
"""Разделы отчёта по ТЗ п. 6, в порядке ТЗ. Сверяем по тексту заголовка ``##``, номер раздела не важен."""

MAX_FAILURES = 10
"""Сколько строк «что не сошлось» хранит одна проверка: остальное сводится к числу."""

_HEADING = re.compile(r"^##\s+(?:\d+\.\s*)?(?P<title>.+?)\s*$")
_REQ_ROW = re.compile(r"^\|\s*(?P<id>R-\d+(?:\.\d+)?)\s*\|")
_FINDING_CARD = re.compile(r"^###\s+(?P<id>F-\d+)\b")
_CARD_SEVERITY = re.compile(r"^###\s+F-\d+\s+·\s+(?P<severity>[^·]+?)\s+·")
SEVERITY_LINE = "- По важности:"
_SEVERITY_ENTRY = re.compile(r"(?P<label>\S+) (?P<n>\d+)")
_MISSING_QUOTES = re.compile(r"^Цитат модели не найдено в документе: \d+ из (?P<total>\d+)\.")
"""Примечание ``verify._quote_notes``: сколько цитат дала модель, если часть не нашлась в документе. Других
следов ненайденных цитат у находки нет. Формулировку закрепляет тест на настоящем результате верификатора."""
_REQUIREMENT_ID = re.compile(r"R-\d+(?:\.\d+)?")
_FINDING_ID = re.compile(r"\bF-\d+\b")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
MATRIX_SECTION = "Матрица покрытия критериев"
REQUIREMENTS_SECTION = "Проверка каждого требования"
SET_SECTION = "Проверка всего набора требований"


def _sections(markdown: str) -> dict[str, list[str]]:
    """Markdown по разделам ``##``: заголовок без номера → строки раздела. Повтор заголовка склеивается."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in markdown.splitlines():
        match = _HEADING.match(line) if line.startswith("## ") else None
        if match:
            current = sections.setdefault(match.group("title"), [])
        elif current is not None:
            current.append(line)
    return sections


def _ids(pattern: re.Pattern[str], lines: Iterable[str]) -> list[str]:
    return [m.group("id") for line in lines if (m := pattern.match(line))]


def _req_rows(lines: Iterable[str]) -> dict[str, list[str]]:
    """Строки таблиц Markdown, которые начинаются с ``R-NN``: ID → ячейки строки после ID."""
    rows: dict[str, list[str]] = {}
    for line in lines:
        match = _REQ_ROW.match(line)
        if match:
            cells = [c.strip() for c in _CELL_SPLIT.split(line.strip().strip("|"))]
            rows.setdefault(match.group("id"), cells[1:])
    return rows


def _item(
    item_id: str, title: str, failures: Sequence[str], ok_detail: str, *, integrity: bool = True
) -> SelfCheckItem:
    shown = list(failures[:MAX_FAILURES])
    if len(failures) > MAX_FAILURES:
        shown.append(f"… и ещё {len(failures) - MAX_FAILURES}")
    return SelfCheckItem(
        id=item_id,
        title=title,
        passed=not failures,
        integrity=integrity,
        detail=ok_detail if not failures else f"не сошлось: {len(failures)}",
        failures=shown,
    )


def _equal(failures: list[str], name: str, left: object, right: object, against: str) -> None:
    if left != right:
        failures.append(f"{name}: {left}, а {against}: {right}")


# ---------------------------------------------------------------- отдельные проверки


def _check_sections(sections: dict[str, list[str]]) -> tuple[SelfCheckItem, int]:
    missing = [title for title in TZ_SECTIONS if title not in sections]
    found = len(TZ_SECTIONS) - len(missing)
    item = _item(
        "sections",
        "Разделы ТЗ п. 6 на месте",
        [f"нет раздела «{title}»" for title in missing],
        f"{found} из {len(TZ_SECTIONS)}: паспорт, нарезка, проверка требований и набора, сводка, итог",
        integrity=False,
    )
    return item, found


def _has_anchor(finding: Finding) -> bool:
    return any(q.verified and q.anchor is not None and q.located for q in finding.quotes)


def _document_view(
    report: Report, doc_text: str | None, blocks: Sequence[Block] | None
) -> ParsedDocument | None:
    """Представление для span_anchor и line_of: именно они определили места исходных якорей."""
    if doc_text is None:
        return None
    info, pp = report.document, report.passport
    return ParsedDocument(
        path=info.path if info else "",
        name=info.name if info else pp.document_name,
        kind=info.kind if info else pp.document_kind,
        sha256=(info.sha256 if info else None) or pp.document_sha256 or "",
        size_bytes=info.size_bytes if info and info.size_bytes is not None else pp.document_size_bytes or 0,
        text=doc_text,
        blocks=list(blocks or ()),
    )


def _place_difference(doc: ParsedDocument, anchor: Anchor, blocks_known: bool) -> str:
    """Строки доступны по одному тексту; страницу, абзац и строку таблицы определяют блоки."""
    expected = span_anchor(doc, anchor.char_start, anchor.char_end)
    fields = [
        ("строка", anchor.line_start, expected.line_start),
        ("последняя строка", anchor.line_end, expected.line_end),
    ]
    if blocks_known:
        fields.extend(
            (
                ("страница", anchor.page, expected.page),
                ("абзац", anchor.paragraph, expected.paragraph),
                ("таблица", anchor.table, expected.table),
                ("строка таблицы", anchor.row, expected.row),
            )
        )
    return "; ".join(
        f"{label} {value}, а в документе {want}" for label, value, want in fields if value != want
    )


def _recheck_quote(finding: Finding, doc: ParsedDocument | None, blocks_known: bool) -> tuple[int, list[str]]:
    """Сверяем фрагмент, место и раздел блока, где начинается цитата (#119)."""
    if doc is None:
        return 0, []
    checked, failures = 0, []
    for q in finding.quotes:
        if not q.verified or q.anchor is None:
            continue
        checked += 1
        short = (q.located or "")[:40]
        if doc.text[q.anchor.char_start : q.anchor.char_end] != q.located:
            failures.append(f"{finding.id}: фрагмент по якорю не совпадает с цитатой «{short}»")
        block = doc.block(q.anchor.block_ids[0]) if q.anchor.block_ids else None
        if block is not None and q.anchor.section != block.section:
            failures.append(f"{finding.id}: раздел в якоре не совпадает с разделом блока цитаты «{short}»")
        if diff := _place_difference(doc, q.anchor, blocks_known):
            failures.append(f"{finding.id}: место цитаты «{short}» не совпадает с документом: {diff}")
    return checked, failures


def _check_anchors(
    findings: Sequence[Finding], doc: ParsedDocument | None, blocks_known: bool
) -> tuple[list[SelfCheckItem], int, int]:
    """У каждой находки есть якорь и сверенная цитата; цитаты находок сверены и заново с документом."""
    unanchored = [f"{f.id}: нет якоря или сверенной цитаты" for f in findings if not _has_anchor(f)]
    quotes = [q for f in findings for q in f.quotes]
    unverified = [
        f"{f.id}: цитата не найдена в документе" for f in findings for q in f.quotes if not q.verified
    ]
    rechecked, mismatched = 0, []
    for f in findings:
        n, bad = _recheck_quote(f, doc, blocks_known)
        rechecked += n
        mismatched += bad
    anchored = sum(_has_anchor(f) for f in findings)
    place = "с разделом и местом (строка, страница, абзац)" if blocks_known else "с местом (строка)"
    items = [
        _item(
            "anchors",
            "У каждой находки есть якорь и сверенная цитата",
            unanchored,
            f"{anchored} из {len(findings)}" if findings else "находок нет",
        ),
        _item(
            "quotes",
            "Цитаты в находках сверены с документом",
            unverified + mismatched,
            (
                f"цитат в находках: {len(quotes)}, все сверены верификатором"
                + (f"; заново по тексту документа и якорю {place}: {rechecked}" if doc is not None else "")
                if quotes
                else "цитат нет"
            ),
        ),
    ]
    return items, rechecked, anchored


def _requirement_places(seg: Segmentation, doc: ParsedDocument, blocks_known: bool) -> list[str]:
    """Таблица нарезки против документа: цитата по якорю дословно, «ID в документе» есть в тексте."""
    failures: list[str] = []
    collapsed = " ".join(doc.text.split())
    for req in seg.requirements:
        anchor = req.anchor
        if doc.text[anchor.char_start : anchor.char_end] != req.quote:
            failures.append(f"{req.id}: цитата в таблице нарезки не совпадает с текстом документа по якорю")
        if (
            req.source_id
            and req.source_id not in doc.text
            and " ".join(req.source_id.split()) not in collapsed
        ):
            failures.append(f"{req.id}: «ID в документе» {req.source_id} в тексте документа нет")
        if diff := _place_difference(doc, anchor, blocks_known):
            failures.append(f"{req.id}: место в якоре не совпадает с документом: {diff}")
    return failures


def _check_requirements(
    report: Report, sections: dict[str, list[str]] | None, doc: ParsedDocument | None, blocks_known: bool
) -> SelfCheckItem:
    """Каждый ``R-NN`` нарезки есть в таблице раздела «Проверка каждого требования»."""
    seg, ver = report.segmentation, report.verification
    failures = _requirement_places(seg, doc, blocks_known) if seg is not None and doc is not None else []
    if seg is None or ver is None:
        return _item(
            "requirements", "Каждое R-NN есть в проверке требований", failures, "нарезки или проверки нет"
        )
    expected = [r.id for r in seg.requirements]
    for rid in expected:
        if rid not in ver.requirement_status:
            failures.append(f"{rid}: нет статуса у требования")
    for rid in ver.requirement_status:
        if rid not in expected:
            failures.append(f"{rid}: статус есть, а в нарезке этого требования нет")
    if len(set(expected)) != len(expected):
        failures.append("в нарезке повторяются ID требований")
    if sections is not None:
        for title, label in (
            ("Как агент нарезал требования", "в таблице нарезки"),
            ("Проверка каждого требования", "в таблице проверки"),
        ):
            rows = _ids(_REQ_ROW, sections.get(title, ()))
            if title in sections:
                for rid in expected:
                    if rid not in rows:
                        failures.append(f"{rid}: нет {label}")
                failures += [f"{rid}: лишняя строка {label}" for rid in rows if rid not in expected]
    return _item(
        "requirements",
        "Каждое R-NN есть в проверке требований",
        failures,
        f"{len(expected)} из {len(expected)}" if expected else "требований нет",
    )


def _check_cards(report: Report, sections: dict[str, list[str]] | None) -> SelfCheckItem:
    """Каждая находка описана ровно одной карточкой в своём разделе (требования или набора)."""
    ver = report.verification
    if ver is None or sections is None:
        return _item("cards", "Каждая находка F-NN описана карточкой", [], "находок нет")
    failures: list[str] = []
    for scope, title in (
        (Scope.REQUIREMENT, "Проверка каждого требования"),
        (Scope.SET, "Проверка всего набора требований"),
    ):
        if title not in sections:
            continue
        shown = Counter(_ids(_FINDING_CARD, sections[title]))
        expected = [f.id for f in ver.findings if f.scope is scope]
        for fid in expected:
            if shown[fid] != 1:
                failures.append(f"{fid}: карточек в разделе «{title}»: {shown[fid]}")
        failures += [f"{fid}: лишняя карточка в разделе «{title}»" for fid in shown if fid not in expected]
    return _item(
        "cards",
        "Каждая находка F-NN описана карточкой",
        failures,
        f"{len(ver.findings)} из {len(ver.findings)}",
    )


_Level = TypeVar("_Level", Severity, Property, DefectType)


def _check_distribution(
    failures: list[str],
    name: str,
    stored: Mapping[_Level, int],
    actual: Mapping[_Level, int],
    order: type[_Level],
) -> None:
    def labels(counts: Mapping[_Level, int]) -> str:
        return ", ".join(f"{key.label} {counts[key]}" for key in order if key in counts) or "нет"

    if stored != actual:
        failures.append(f"{name} в сводке: {labels(stored)}, а по находкам: {labels(actual)}")


def _severity_line(sections: dict[str, list[str]]) -> list[str]:
    """Что видит читатель: строка «По важности» в сводке против важности в заголовках карточек F-NN."""
    line = next((s for s in sections.get("Сводка", ()) if s.startswith(SEVERITY_LINE)), None)
    if line is None or REQUIREMENTS_SECTION not in sections or SET_SECTION not in sections:
        return []
    shown = line.removeprefix(SEVERITY_LINE).strip().removesuffix(".")
    in_line = {m["label"]: int(m["n"]) for m in _SEVERITY_ENTRY.finditer(shown)}
    cards = Counter(
        m["severity"]
        for title in (REQUIREMENTS_SECTION, SET_SECTION)
        for s in sections[title]
        if (m := _CARD_SEVERITY.match(s))
    )
    if in_line == dict(cards):
        return []
    by_cards = ", ".join(f"{s.label} {cards[s.label]}" for s in Severity if cards[s.label]) or "нет находок"
    return [f"строка «По важности»: {shown}, а по карточкам: {by_cards}"]


def _model_quotes(f: Finding) -> int:
    """Сколько цитат дала модель: в находке остались только сверенные, о ненайденных говорит примечание."""
    for note in f.notes:
        if match := _MISSING_QUOTES.match(note):
            return int(match["total"])
    return len(f.quotes)


def _recount_quotes(report: Report, ver: Verification, failures: list[str]) -> None:
    """«Цитаты: … из N» в сводке: верификатор считает цитаты всех находок, и оставшихся, и отброшенных."""
    clarification = report.clarification
    closed = [e.finding for e in clarification.effects if e.finding is not None] if clarification else []
    recount = sum(_model_quotes(f) for f in [*ver.findings, *closed])
    recount += sum(len(d.finding.quotes) for d in ver.discarded)
    total = ver.summary.quotes_total
    if clarification is None:
        _equal(failures, "цитат в сводке", total, recount, "по находкам и отброшенным")
    elif recount > total:
        # Цитаты повторов и находок сверх лимита после уточнений остались только в сводке.
        failures.append(f"цитат в сводке {total}, а у находок и отброшенных их больше: {recount}")


def _summary_failures(report: Report, sections: dict[str, list[str]] | None) -> list[str]:
    ver, seg = report.verification, report.segmentation
    if ver is None:
        return []
    s, findings = ver.summary, ver.findings
    failures: list[str] = []
    actual = summarize(findings, ver.discarded, seg or Segmentation(), status=ver.requirement_status)
    _check_distribution(failures, "по важности", s.by_severity, actual.by_severity, Severity)
    _check_distribution(failures, "по свойствам", s.by_property, actual.by_property, Property)
    _check_distribution(failures, "по типам дефектов", s.by_defect_type, actual.by_defect_type, DefectType)
    _equal(
        failures, "исправлено якорей в сводке", s.anchors_corrected, actual.anchors_corrected, "по находкам"
    )
    _recount_quotes(report, ver, failures)
    _equal(failures, "находок в сводке", s.findings_total, len(findings), "находок в списке")
    _equal(failures, "нарушений в сводке", s.violations, actual.violations, "нарушений в списке")
    _equal(failures, "«неясно» в сводке", s.unclear, actual.unclear, "«неясно» в списке")
    _equal(failures, "нарушений и «неясно»", s.violations + s.unclear, s.findings_total, "всего находок")
    _equal(
        failures,
        "находок набора в сводке",
        s.set_level,
        actual.set_level,
        "в списке",
    )
    _equal(failures, "критичных в сводке", s.critical, actual.critical, "критичных в списке")
    _equal(failures, "ID критичных в сводке", sorted(s.critical_ids), sorted(actual.critical_ids), "в списке")
    _equal(failures, "сумма по важности", sum(s.by_severity.values()), s.findings_total, "всего находок")
    _equal(
        failures,
        "требований с находками",
        s.requirements_with_findings,
        actual.requirements_with_findings,
        "в таблице статусов",
    )
    _equal(failures, "отброшено в сводке", s.discarded_total, len(ver.discarded), "в списке отброшенных")
    _equal(
        failures,
        "сумма по причинам отбраковки",
        sum(s.discarded_by_reason.values()),
        s.discarded_total,
        "всего",
    )
    _equal(
        failures,
        "цитат в сводке",
        s.quotes_total,
        s.quotes_exact + s.quotes_fuzzy + s.quotes_not_found,
        "точных, нечётких и не найденных",
    )
    if seg is not None:
        _equal(
            failures,
            "требований в сводке",
            s.requirements_total,
            len(ver.requirement_status),
            "строк статуса",
        )
        _equal(failures, "требований в сводке", s.requirements_total, len(seg.requirements), "в нарезке")
    in_report = [q for f in findings for q in f.quotes]
    if sum(q.verified for q in in_report) > s.quotes_exact + s.quotes_fuzzy:
        failures.append("сверенных цитат в находках больше, чем сверенных цитат в сводке")
    if sections is not None:
        failures.extend(_severity_line(sections))
        cards = sum(
            len(_ids(_FINDING_CARD, sections.get(title, ())))
            for title in ("Проверка каждого требования", "Проверка всего набора требований")
        )
        _equal(failures, "находок в сводке", s.findings_total, cards, "карточек в отчёте")
        rows = len(_ids(_REQ_ROW, sections.get("Проверка каждого требования", ())))
        if "Проверка каждого требования" in sections and seg is not None:
            _equal(failures, "требований в сводке", s.requirements_total, rows, "строк в таблице проверки")
    return failures


def _clarify_delta(report: Report) -> int:
    """На сколько ответы автора изменили число «в отчёте + отброшено» против числа после слияния.

    Сверка с ответами (#42) убирает закрытые находки и добавляет свои: новые в отчёт, без сверенной
    цитаты в отброшенные. Повторы и находки сверх лимита не попадают ни туда, ни туда.
    """
    c, ver = report.clarification, report.verification
    if c is None or ver is None:
        return 0
    added = len(c.by_effect(ClarifyEffect.NEW)) + sum(
        any(src.kind is SourceKind.ANSWER for src in d.finding.sources) for d in ver.discarded
    )
    return added - len(c.by_effect(ClarifyEffect.CLOSED))


def _metrics_failures(report: Report) -> list[str]:
    ver, m = report.verification, report.metrics
    if ver is None:
        return []
    s = ver.summary
    failures: list[str] = []
    _equal(failures, "находок в метриках", m.findings_total, s.findings_total, "в сводке")
    _equal(failures, "отброшено в метриках", m.discarded_total, s.discarded_total, "в сводке")
    _equal(failures, "цитат в метриках", m.quotes_total, s.quotes_total, "в сводке")
    _equal(
        failures, "сверенных цитат в метриках", m.quotes_verified, s.quotes_exact + s.quotes_fuzzy, "в сводке"
    )
    _equal(
        failures,
        "находок после слияния и сверки с ответами автора",
        m.merged_total + _clarify_delta(report),
        m.findings_total + m.discarded_total,
        "в отчёте и отброшенных вместе",
    )
    _equal(
        failures,
        "причин отбраковки в метриках",
        sum(m.discarded_by_reason.values()),
        m.discarded_total,
        "всего отброшено",
    )
    if m.found_in:
        _equal(
            failures,
            "находок в «найдено в N из K»",
            sum(b.count for b in m.found_in),
            m.findings_total,
            "в отчёте",
        )
    return failures


def _check_counters(report: Report, sections: dict[str, list[str]] | None) -> SelfCheckItem:
    """Счётчики сводки против строк таблиц и карточек: расхождение значит, что отчёт врёт о себе."""
    return _item(
        "counters",
        "Счётчики сводки равны числу строк в таблицах",
        _summary_failures(report, sections),
        "сводка, таблицы и карточки сходятся",
    )


def _check_metrics(report: Report) -> SelfCheckItem:
    """Метрики прогона против сводки и воронки. Расхождение предупреждает, но не ломает находки."""
    return _item(
        "metrics",
        "Метрики прогона сходятся со сводкой и воронкой находок",
        _metrics_failures(report),
        "находки, отброшенные, цитаты и «найдено в N из K» сходятся",
        integrity=False,
    )


def _cell_failures(matrix: CoverageMatrix, ver: Verification) -> list[str]:
    """Каждая ячейка строки требования: находки своего свойства и уровня, состояние по находкам, и строка
    показывает все находки требования из раздела 4 (находки уровня набора в колонке «Набор», #118)."""
    by_id = {f.id: f for f in ver.findings}
    failures: list[str] = []
    for row in matrix.rows:
        rid = row.requirement_id
        status = ver.requirement_status.get(rid, [])
        cells = [(prop.value, cell, prop) for prop, cell in row.cells.items()]
        for where, cell, prop in [*cells, ("набор", row.set_cell, None)]:
            for fid in cell.finding_ids:
                f = by_id.get(fid)
                if f is None:
                    continue
                level = is_requirement_level(f)
                own = (f.property is prop and level) if prop is not None else not level
                if not own or fid not in status:
                    failures.append(f"{rid} × {where}: {fid} не относится к этой ячейке")
            if bool(cell.finding_ids) != (cell.state in (CellState.VIOLATION, CellState.UNCLEAR)):
                failures.append(f"{rid} × {where}: состояние «{cell.state.value}» не по находкам")
        lost = [fid for fid in status if fid not in row_finding_ids(row)]
        if lost:
            failures.append(f"{rid}: {', '.join(lost)} есть у требования в разделе 4, а в строке матрицы нет")
    return failures


def _coverage_failures(report: Report, matrix: CoverageMatrix) -> list[str]:
    """«Нарушений нет» только там, где поиск отработал: по метрикам прогона и частям документа в паспорте."""
    clean_rows = [r for r in matrix.rows if any(c.state is CellState.CLEAN for c in row_cells(r))]
    clean_set = [sr.property.label for sr in matrix.set_rows if sr.state is CellState.CLEAN]
    if not matrix.rows:
        return [f"{label}: «нарушений нет», а требований в документе нет" for label in clean_set]
    if report.metrics.passes_ok == 0:
        why = "а ни один проход поиска не отработал"
        return [f"{r.requirement_id}: «нарушений нет», {why}" for r in clean_rows] + [
            f"{label}: «нарушений нет», {why}" for label in clean_set
        ]
    parts = report.document.parts if report.document else []
    missed = {rid: p.index for p in parts if not p.passes_ok for rid in p.requirement_ids}
    failures = [
        f"{r.requirement_id}: «нарушений нет», а часть {missed[r.requirement_id]} поиском не проверена"
        for r in clean_rows
        if r.requirement_id in missed
    ]
    unsearched = [p.index for p in parts if not p.passes_ok]
    if unsearched:
        why = "а часть документа поиском не проверена"
        failures += [f"{label}: «нарушений нет», {why}" for label in clean_set]
    _equal(failures, "непроверенные части в матрице", matrix.unchecked_parts, unsearched, "в паспорте")
    return failures


def _markdown_failures(sections: dict[str, list[str]]) -> list[str]:
    """Что видит читатель: у каждого R-NN раздел 4 и строка матрицы называют одни и те же F-NN, а
    «нарушений нет» и «не проверено моделью» в разделе 4 совпадают со знаками «√» и «–» в матрице."""
    status_rows = _req_rows(sections.get(REQUIREMENTS_SECTION, ()))
    matrix_rows = _req_rows(sections.get(MATRIX_SECTION, ()))
    failures: list[str] = []
    for rid, cells in status_rows.items():
        shown = matrix_rows.get(rid)
        if shown is None or not cells:
            continue
        status = cells[-1]
        in_status, in_matrix = set(_FINDING_ID.findall(status)), set(_FINDING_ID.findall(" ".join(shown)))
        if in_status != in_matrix:
            failures.append(
                f"{rid}: в разделе 4 {', '.join(sorted(in_status)) or 'находок нет'}, "
                f"в матрице {', '.join(sorted(in_matrix)) or 'находок нет'}"
            )
        elif status == NO_VIOLATIONS and any(c != SYMBOLS[CellState.CLEAN] for c in shown):
            failures.append(f"{rid}: в разделе 4 «{NO_VIOLATIONS}», а в матрице не везде «√»")
        elif status.startswith(NOT_SEARCHED) and SYMBOLS[CellState.CLEAN] in shown:
            failures.append(f"{rid}: в разделе 4 «{NOT_SEARCHED}», а в матрице есть «√»")
    return failures


def _check_matrix(report: Report, sections: dict[str, list[str]] | None) -> SelfCheckItem:
    """Матрица покрытия совпадает с находками и с разделом 4; «нарушений нет» только там, где искали."""
    matrix, ver, seg = report.coverage, report.verification, report.segmentation
    title = "Матрица покрытия совпадает с находками"
    if matrix is None or ver is None or seg is None:
        return _item("matrix", title, [], "матрицы нет: нет нарезки или проверки")
    failures: list[str] = []
    by_id = {f.id: f for f in ver.findings}
    shown = grid_finding_ids(matrix)
    failures += [f"{fid}: находки нет в матрице" for fid in by_id if fid not in shown]
    failures += [f"{fid}: в матрице есть, а в отчёте такой находки нет" for fid in shown if fid not in by_id]
    failures += [f"{fid}: нет ячейки ни в одной матрице" for fid in matrix.extra_finding_ids]
    if seg.requirements:
        failures += [f"{fid}: без требования и не в матрице набора" for fid in matrix.unlinked_finding_ids]
    _equal(
        failures,
        "строк матрицы",
        [r.requirement_id for r in matrix.rows],
        [r.id for r in seg.requirements],
        "в нарезке",
    )
    failures += _cell_failures(matrix, ver)
    for set_row in matrix.set_rows:
        for fid in set_row.finding_ids:
            f = by_id.get(fid)
            if f is not None and (is_requirement_level(f) or f.property.set_level is not set_row.property):
                failures.append(f"{set_row.property.value}: {fid} не относится к этой строке")
    failures += _coverage_failures(report, matrix)
    if sections is not None:
        failures += _markdown_failures(sections)
    cells = sum(len(row_cells(r)) for r in matrix.rows)
    return _item(
        "matrix",
        title,
        failures,
        f"{len(matrix.rows)} требований × {len(matrix.requirement_properties)} свойств и колонка «Набор», "
        f"ячеек: {cells}; свойств набора: {len(matrix.set_rows)}; у каждого требования те же находки, "
        "что в разделе 4",
    )


def _check_traffic(report: Report, markdown: str, sections: dict[str, list[str]]) -> SelfCheckItem:
    """Светофор теми же ``traffic_light`` и ``light_with_questions``, что в конвейере; итог и первая страница.

    Итог повторяет правило ``build._conclusion``: при ошибке шага или оговорке входа он не зелёный, красный
    остаётся красным, а причина светофора дословно стоит в выводе.
    """
    title = "Светофор и вывод пересчитаны по находкам и вопросам"
    ver, seg, c = report.verification, report.segmentation, report.conclusion
    if ver is None:
        return _item("traffic", title, [], "верификатор не отработал: светофора по находкам нет")
    light, reason = traffic_light(ver.findings, requirements=len(seg.requirements) if seg else None)
    base = ver.model_copy(update={"traffic_light": light, "traffic_reason": reason})
    candidates = [light_with_questions(base, report.questions)]
    if not report.questions:
        # Пустой список в отчёте возможен и после сбоя шага вопросов.
        candidates.append(light_with_questions(base, None))
    truth = next(
        (
            v
            for v in candidates
            if (v.traffic_light, v.traffic_reason) == (ver.traffic_light, ver.traffic_reason)
        ),
        candidates[0],
    )
    failures: list[str] = []
    _equal(
        failures,
        "светофор в отчёте",
        f"{ver.traffic_light.icon} {ver.traffic_reason}",
        f"{truth.traffic_light.icon} {truth.traffic_reason}",
        "по находкам и вопросам",
    )
    expected = truth.traffic_light
    errors = any(d.level is DiagLevel.ERROR for d in report.diagnostics)
    cautions = any(d.level is DiagLevel.WARNING for d in report.input_diagnostics)
    if (errors or cautions) and expected is not TrafficLight.RED:
        expected = TrafficLight.YELLOW
    _equal(
        failures,
        "светофор итога",
        f"{c.traffic_light.icon} {c.traffic_light.label}",
        f"{expected.icon} {expected.label}",
        "по находкам и диагностике",
    )
    if truth.traffic_reason not in c.headline:
        failures.append(f"вывод на первой странице не называет причину светофора: «{truth.traffic_reason}»")
    if "Итог" in sections and f"**{c.traffic_light.icon} {c.headline}**" not in markdown:
        failures.append("на первой странице не тот вывод, что в итоге отчёта")
    if report.clarification and report.clarification.after:
        after = snapshot(ver, report.questions)
        if after != report.clarification.after:
            failures.append(
                f"раздел «Уточнения»: после ответов {report.clarification.after}, а по отчёту {after}"
            )
    return _item(
        "traffic",
        title,
        failures,
        f"{truth.traffic_light.icon} {truth.traffic_light.label}: пересчитан по находкам и вопросам автору, "
        "вывод на первой странице тот же",
    )


def _listed(ids: Sequence[str]) -> str:
    return ", ".join(ids) or "пусто"


def _check_first_page(report: Report) -> SelfCheckItem:
    """«Что исправить первым» и «Какие вопросы задать первыми» заново функциями ``build_report``."""
    ver, c = report.verification, report.conclusion
    expected_fix = _fix_first(ver) if ver else []
    expected_ask = _ask_first(report.questions, expected_fix)
    critical = (
        [f.id for f in ver.findings if f.severity is Severity.CRITICAL and f.verdict is Verdict.VIOLATION]
        if ver
        else []
    )
    failures: list[str] = []
    _equal(failures, "«Что исправить первым»", _listed(c.fix_first), _listed(expected_fix), "по находкам")
    missing = [fid for fid in critical if fid not in c.fix_first]
    if missing:
        failures.append(f"критичные нарушения не в «Что исправить первым»: {_listed(missing)}")
    _equal(
        failures, "«Какие вопросы задать первыми»", _listed(c.ask_first), _listed(expected_ask), "по вопросам"
    )
    return _item(
        "first_page",
        "«Что исправить первым» и «Какие вопросы задать первыми» пересчитаны",
        failures,
        f"исправить первым: {len(expected_fix)} (критичных {len(critical)}, все на месте); "
        f"вопросов первыми: {len(expected_ask)}",
    )


def _requirement_refs(seg: Segmentation, findings: Sequence[Finding]) -> Iterator[tuple[bool, str]]:
    requirements = {r.id for r in seg.requirements}
    for f in findings:
        for rid in f.requirement_ids:
            yield rid in requirements, f"{f.id}: требования {rid} нет в нарезке"
    for r in seg.requirements:
        linked = [*r.links, r.depends_on] if r.depends_on else r.links
        for rid in linked:
            if _REQUIREMENT_ID.fullmatch(rid):  # остальное ссылки на блоки документа
                yield rid in requirements, f"{r.id}: связанного требования {rid} нет в нарезке"


def _finding_refs(
    report: Report, ver: Verification, findings: set[str], closed: set[str]
) -> Iterator[tuple[bool, str]]:
    for rid, ids in ver.requirement_status.items():
        for fid in ids:
            yield fid in findings, f"{rid}: в статусе {fid}, а такой находки нет"
    targets = findings | closed
    for q in [*report.questions, *(w.question for w in report.questions_withdrawn)]:
        for fid in q.related_findings:
            yield fid in targets, f"{q.id}: закрывает {fid}, а такой находки нет"
    for fid in report.conclusion.fix_first:
        yield fid in findings, f"«Что исправить первым»: {fid}, а такой находки нет в списке"
    for checklist in report.checklists:
        for fid in checklist.findings:
            yield fid in findings, f"{checklist.id}: в чек-листе {fid}, а такой находки нет"


def _report_refs(report: Report) -> Iterator[tuple[bool, str]]:
    """Каждая ссылка F-, R-, Q- в отчёте: (ведёт ли на объект отчёта, что сказать, если нет)."""
    ver, seg, clarification = report.verification, report.segmentation, report.clarification
    findings = ver.findings if ver else []
    finding_ids = {f.id for f in findings}
    questions = {q.id for q in report.questions}
    withdrawn = {w.question.id for w in report.questions_withdrawn}
    if seg is not None:
        yield from _requirement_refs(seg, findings)
    if ver is not None:
        closed = (
            {e.finding_id for e in clarification.by_effect(ClarifyEffect.CLOSED)} if clarification else set()
        )
        yield from _finding_refs(report, ver, finding_ids, closed)
    for qid in report.conclusion.ask_first:
        yield qid in questions, f"«Какие вопросы задать первыми»: {qid}, а такого вопроса нет в списке"
    for f in findings:
        for gap in f.test_draft.gap_items if f.test_draft else ():
            if gap.question_id:
                targets = withdrawn if gap.question_withdrawn else questions
                yield (
                    gap.question_id in targets,
                    f"{f.id}: пропуск [?{gap.number}] ведёт на {gap.question_id}, а такого вопроса нет",
                )
    if clarification:
        # AuthorAnswer.related_findings — номера первого прогона, их здесь не сверяем.
        answers = {a.question_id for a in clarification.answers}
        for e in clarification.effects:
            if e.effect is ClarifyEffect.CLOSED:
                yield e.finding is not None, f"{e.finding_id}: нет карточки закрытой находки"
            elif ver is not None:
                yield e.finding_id in finding_ids, f"{e.finding_id}: в уточнениях есть, а в находках нет"
            for qid in e.question_ids:
                yield qid in answers, f"{e.finding_id}: в уточнениях указан {qid}, а такого ответа нет"


def _check_refs(report: Report) -> SelfCheckItem:
    """Ссылки ведут на существующие объекты, а статус требований равен пересчёту ``requirement_status``."""
    failures: list[str] = []
    count = 0
    for ok, message in _report_refs(report):
        count += 1
        if not ok:
            failures.append(message)
    ver, seg = report.verification, report.segmentation
    if ver is not None and seg is not None:
        actual = requirement_status(ver.findings, seg)
        for rid, ids in ver.requirement_status.items():
            if rid in actual and actual[rid] != ids:
                shown, linked = _listed(ids), _listed(actual[rid])
                failures.append(f"{rid}: в таблице статусов {shown}, а по требованиям находок {linked}")
    return _item(
        "refs",
        "Каждая ссылка F-, R-, Q- ведёт на объект отчёта",
        failures,
        f"ссылок проверено: {count}, все ведут на объекты отчёта",
    )


def _check_passport(report: Report) -> SelfCheckItem:
    """Паспорт против журнала вызовов (таблица шагов строится из того же ``calls.jsonl``) и диагностики.

    Код завершения заново считает ``errors.exit_code``. Для прочитанного документа «проверка выполнена
    полностью» ровно тогда, когда нет ни ошибки, ни оговорки входа, то есть ровно при коде 0.
    """
    pp, metrics = report.passport, report.metrics
    steps = metrics.steps
    failures: list[str] = []
    for label, passport_n, metrics_n, logged in (
        ("вызовов модели", pp.calls_total, metrics.calls_total, sum(s.calls for s in steps)),
        ("вызовов с ошибкой", pp.calls_failed, metrics.calls_failed, sum(s.failed for s in steps)),
        ("ответов из кеша", pp.calls_cached, metrics.calls_cached, sum(s.cached for s in steps)),
        ("ответов из записи", pp.calls_replayed, metrics.calls_replayed, sum(s.replayed for s in steps)),
    ):
        _equal(failures, f"{label} в паспорте", passport_n, logged, "в журнале вызовов по шагам")
        _equal(failures, f"{label} в метриках", metrics_n, logged, "в журнале вызовов по шагам")
    code = exit_code([*report.input_diagnostics, *report.diagnostics])
    _equal(failures, "код завершения в паспорте", pp.exit_code, code, "по диагностике отчёта")
    status = status_label(report.status)
    if (report.status is ReportStatus.COMPLETE) != (pp.exit_code is ExitCode.OK):
        failures.append(f"состояние проверки «{status}», а код завершения {pp.exit_code}")
    return _item(
        "passport",
        "Паспорт сходится с журналом вызовов и кодом завершения",
        failures,
        f"вызовов модели {pp.calls_total}, как в журнале по шагам; "
        f"код {code} и состояние «{status}» сходятся с диагностикой",
    )


# ---------------------------------------------------------------- сборка


def _summary(sc: SelfCheck, report: Report) -> str:
    """Одна строка паспорта: «ТЗ п. 6: 6 из 6 разделов; …» или список того, что не сошлось."""
    if not sc.applicable:
        return "не применима: вход не прочитан, разделы о требованиях не формировались"
    parts = [f"ТЗ п. 6: {sc.sections_found} из {sc.sections_total} разделов"]
    if sc.findings_total:
        parts.append(f"у {sc.findings_anchored} из {sc.findings_total} находок есть якорь")
    else:
        parts.append("находок нет" if report.verification is not None else "проверка требований не выполнена")
    share = sc.quotes_verified / sc.quotes_total if sc.quotes_total else None
    quotes = f"цитат в находках сверено {pct(share)}" if share is not None else "цитат в находках нет"
    model_share = report.metrics.verified_quote_share
    if model_share is not None:
        quotes += f" (цитат модели до фильтра: {pct(model_share)})"
    parts.append(quotes)
    failed = [item for item in sc.items if not item.passed]
    if failed:
        return "НАРУШЕНИЯ: " + "; ".join(f"{i.title}" for i in failed) + ". " + "; ".join(parts)
    counters = next((i for i in sc.items if i.id == "counters"), None)
    if counters is not None and counters.passed and report.verification is not None:
        parts.append("счётчики сводки сходятся с таблицами")
    return "; ".join(parts)


def check_report(
    report: Report,
    markdown: str | None = None,
    doc_text: str | None = None,
    blocks: Sequence[Block] | None = None,
) -> SelfCheck:
    """Самопроверка готового отчёта.

    ``markdown``: отчёт в том виде, в каком его прочтут; без него рендерится из ``report``.
    ``doc_text``: канонический текст документа; с ним цитаты находок сверяются ещё раз по якорям.
    ``blocks``: блоки документа; с ними сверяется и раздел в якоре каждой цитаты.
    """
    if report.status is ReportStatus.NOT_PERFORMED or not report.conclusion.performed:
        sc = SelfCheck(applicable=False)
        return sc.model_copy(update={"summary": _summary(sc, report)})
    text = markdown if markdown is not None else render_md(report)
    sections = _sections(text)
    findings = list(report.verification.findings) if report.verification else []

    sections_item, found = _check_sections(sections)
    doc = _document_view(report, doc_text, blocks)
    blocks_known = blocks is not None
    anchor_items, rechecked, anchored = _check_anchors(findings, doc, blocks_known)
    checks: list[Callable[[], SelfCheckItem]] = [
        lambda: _check_requirements(report, sections, doc, blocks_known),
        lambda: _check_cards(report, sections),
        lambda: _check_counters(report, sections),
        lambda: _check_metrics(report),
        lambda: _check_matrix(report, sections),
        lambda: _check_traffic(report, text, sections),
        lambda: _check_first_page(report),
        lambda: _check_refs(report),
        lambda: _check_passport(report),
    ]
    quotes = [q for f in findings for q in f.quotes]
    sc = SelfCheck(
        sections_found=found,
        sections_total=len(TZ_SECTIONS),
        findings_total=len(findings),
        findings_anchored=anchored,
        quotes_total=len(quotes),
        quotes_verified=sum(q.verified for q in quotes),
        quotes_rechecked=rechecked,
        items=[sections_item, *anchor_items, *(check() for check in checks)],
    )
    return sc.model_copy(update={"summary": _summary(sc, report)})


def selfcheck_diagnostics(sc: SelfCheck) -> list[Diagnostic]:
    """Диагностика провала: ``INT-003`` (ошибка, код 1) при нарушенной целостности, иначе ``INT-004``."""
    diagnostics: list[Diagnostic] = []
    broken = [i for i in sc.items if not i.passed and i.integrity]
    gaps = [i for i in sc.items if not i.passed and not i.integrity]
    if broken:
        diagnostics.append(
            Diagnostic(
                code=INT_SELFCHECK_FAILED,
                level=DiagLevel.ERROR,
                message="Самопроверка отчёта нашла нарушение целостности: "
                + "; ".join(i.title for i in broken),
                action="Отчёт записан как есть, ничего не исправлено задним числом",
                hint="Не опирайтесь на этот отчёт; подробности в разделе «Самопроверка отчёта»",
                location="; ".join(f for i in broken for f in i.failures[:2]) or None,
                step="report",
            )
        )
    if gaps:
        diagnostics.append(
            Diagnostic(
                code=INT_SELFCHECK_WARNING,
                level=DiagLevel.WARNING,
                message="Самопроверка отчёта: " + "; ".join(i.title for i in gaps),
                action="Отчёт записан, найденное в нём верно",
                hint="Подробности в разделе «Самопроверка отчёта»",
                location="; ".join(f for i in gaps for f in i.failures[:2]) or None,
                step="report",
            )
        )
    return diagnostics
