"""Самопроверка отчёта по ТЗ п. 6 (#43): отчёт сам доказывает, что он полный и внутренне сходится.

Проверки идут по готовому ``Report`` и по его Markdown: что видит читатель, то и проверяем. Модуль
только проверяет и сообщает. Отчёт задним числом он не правит: если счётчик не сошёлся, это видно в
паспорте, в разделе «Самопроверка отчёта» и в диагностике, а не спрятано правкой.

Проверки делятся на два рода (``SelfCheckItem.integrity``):

- целостность: в отчёт попало неверное (находка без якоря или сверенной цитаты, счётчик сводки не равен
  числу строк, требование выпало из таблицы, находка без карточки, матрица не по находкам). Диагностика
  ``INT-003``, код возврата 1;
- полнота формы: нет раздела ТЗ п. 6, а то, что есть, верно. Диагностика ``INT-004``, код не меняется.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence

from validator.errors import INT_SELFCHECK_FAILED, INT_SELFCHECK_WARNING
from validator.models import (
    DiagLevel,
    Diagnostic,
    Finding,
    Report,
    ReportStatus,
    Scope,
    SelfCheck,
    SelfCheckItem,
    Severity,
    Verdict,
)
from validator.report.coverage import grid_finding_ids
from validator.report.fmt import pct
from validator.report.render_md import render_md

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


def _recheck_quote(finding: Finding, doc_text: str | None) -> tuple[int, list[str]]:
    """Сверка с текстом документа заново: фрагмент по якорю равен тому, что сверил верификатор."""
    if doc_text is None:
        return 0, []
    checked, failures = 0, []
    for q in finding.quotes:
        if not q.verified or q.anchor is None:
            continue
        checked += 1
        if doc_text[q.anchor.char_start : q.anchor.char_end] != q.located:
            failures.append(
                f"{finding.id}: фрагмент по якорю не совпадает с цитатой «{(q.located or '')[:40]}»"
            )
    return checked, failures


def _check_anchors(findings: Sequence[Finding], doc_text: str | None) -> tuple[list[SelfCheckItem], int, int]:
    """У каждой находки есть якорь и сверенная цитата; цитаты находок сверены и заново с документом."""
    unanchored = [f"{f.id}: нет якоря или сверенной цитаты" for f in findings if not _has_anchor(f)]
    quotes = [q for f in findings for q in f.quotes]
    unverified = [
        f"{f.id}: цитата не найдена в документе" for f in findings for q in f.quotes if not q.verified
    ]
    rechecked, mismatched = 0, []
    for f in findings:
        n, bad = _recheck_quote(f, doc_text)
        rechecked += n
        mismatched += bad
    anchored = sum(_has_anchor(f) for f in findings)
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
                + (f"; заново по тексту документа и якорю: {rechecked}" if doc_text is not None else "")
                if quotes
                else "цитат нет"
            ),
        ),
    ]
    return items, rechecked, anchored


def _check_requirements(report: Report, sections: dict[str, list[str]] | None) -> SelfCheckItem:
    """Каждый ``R-NN`` нарезки есть в таблице раздела «Проверка каждого требования»."""
    seg, ver = report.segmentation, report.verification
    failures: list[str] = []
    if seg is None or ver is None:
        return _item("requirements", "Каждое R-NN есть в проверке требований", [], "нарезки или проверки нет")
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


def _summary_failures(report: Report, sections: dict[str, list[str]] | None) -> list[str]:
    ver, seg = report.verification, report.segmentation
    if ver is None:
        return []
    s, findings = ver.summary, ver.findings
    failures: list[str] = []
    violations = sum(f.verdict is Verdict.VIOLATION for f in findings)
    unclear = sum(f.verdict is Verdict.UNCLEAR for f in findings)
    critical = [f.id for f in findings if f.severity is Severity.CRITICAL and f.verdict is Verdict.VIOLATION]
    with_findings = sum(bool(ids) for ids in ver.requirement_status.values())
    _equal(failures, "находок в сводке", s.findings_total, len(findings), "находок в списке")
    _equal(failures, "нарушений в сводке", s.violations, violations, "нарушений в списке")
    _equal(failures, "«неясно» в сводке", s.unclear, unclear, "«неясно» в списке")
    _equal(failures, "нарушений и «неясно»", s.violations + s.unclear, s.findings_total, "всего находок")
    _equal(
        failures,
        "находок набора в сводке",
        s.set_level,
        sum(f.scope is Scope.SET for f in findings),
        "в списке",
    )
    _equal(failures, "критичных в сводке", s.critical, len(critical), "критичных в списке")
    _equal(failures, "ID критичных в сводке", sorted(s.critical_ids), sorted(critical), "в списке")
    _equal(failures, "сумма по важности", sum(s.by_severity.values()), s.findings_total, "всего находок")
    _equal(
        failures, "требований с находками", s.requirements_with_findings, with_findings, "в таблице статусов"
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
        cards = sum(
            len(_ids(_FINDING_CARD, sections.get(title, ())))
            for title in ("Проверка каждого требования", "Проверка всего набора требований")
        )
        _equal(failures, "находок в сводке", s.findings_total, cards, "карточек в отчёте")
        rows = len(_ids(_REQ_ROW, sections.get("Проверка каждого требования", ())))
        if "Проверка каждого требования" in sections and seg is not None:
            _equal(failures, "требований в сводке", s.requirements_total, rows, "строк в таблице проверки")
    return failures


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
        "находок после слияния",
        m.merged_total,
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


def _check_matrix(report: Report) -> SelfCheckItem:
    """Матрица покрытия совпадает с находками: каждая находка видна в ней, у ячеек верное свойство."""
    matrix, ver, seg = report.coverage, report.verification, report.segmentation
    title = "Матрица покрытия совпадает с находками"
    if matrix is None or ver is None or seg is None:
        return _item("matrix", title, [], "матрицы нет: нет нарезки или проверки")
    failures: list[str] = []
    by_id = {f.id: f for f in ver.findings}
    shown = grid_finding_ids(matrix)
    failures += [f"{fid}: находки нет в матрице" for fid in by_id if fid not in shown]
    failures += [f"{fid}: в матрице есть, а в отчёте такой находки нет" for fid in shown if fid not in by_id]
    _equal(
        failures,
        "строк матрицы",
        [r.requirement_id for r in matrix.rows],
        [r.id for r in seg.requirements],
        "в нарезке",
    )
    for row in matrix.rows:
        for prop, cell in row.cells.items():
            for fid in cell.finding_ids:
                f = by_id.get(fid)
                if f is not None and (
                    f.property is not prop or fid not in ver.requirement_status.get(row.requirement_id, ())
                ):
                    failures.append(f"{row.requirement_id} × {prop.value}: {fid} не относится к этой ячейке")
            if bool(cell.finding_ids) != (cell.state.value in ("violation", "unclear")):
                failures.append(
                    f"{row.requirement_id} × {prop.value}: состояние «{cell.state.value}» не по находкам"
                )
    for set_row in matrix.set_rows:
        for fid in set_row.finding_ids:
            f = by_id.get(fid)
            if f is not None and (f.property is not set_row.property or f.scope is not Scope.SET):
                failures.append(f"{set_row.property.value}: {fid} не относится к этой строке")
    cells = sum(len(r.cells) for r in matrix.rows)
    return _item(
        "matrix",
        title,
        failures,
        f"{len(matrix.rows)} требований × {len(matrix.requirement_properties)} свойств ({cells} ячеек), "
        f"{len(matrix.set_rows)} свойств набора",
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


def check_report(report: Report, markdown: str | None = None, doc_text: str | None = None) -> SelfCheck:
    """Самопроверка готового отчёта.

    ``markdown``: отчёт в том виде, в каком его прочтут; без него рендерится из ``report``.
    ``doc_text``: канонический текст документа; с ним цитаты находок сверяются ещё раз по якорям.
    """
    if report.status is ReportStatus.NOT_PERFORMED or not report.conclusion.performed:
        sc = SelfCheck(applicable=False)
        return sc.model_copy(update={"summary": _summary(sc, report)})
    text = markdown if markdown is not None else render_md(report)
    sections = _sections(text)
    findings = list(report.verification.findings) if report.verification else []

    sections_item, found = _check_sections(sections)
    anchor_items, rechecked, anchored = _check_anchors(findings, doc_text)
    checks: list[Callable[[], SelfCheckItem]] = [
        lambda: _check_requirements(report, sections),
        lambda: _check_cards(report, sections),
        lambda: _check_counters(report, sections),
        lambda: _check_metrics(report),
        lambda: _check_matrix(report),
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
