"""Шаг 9б: отчёт в Markdown из ``Report`` на Jinja2.

Шаблон ``templates/report.md.j2`` только раскладывает данные по разделам ТЗ п. 6. Всё, что требует
выбора или подсчёта по данным (статус каждого требования, место находки, связи находка ↔ вопрос),
готовит этот модуль, чтобы шаблон оставался плоским и его можно было проверить тестами.
Важность, светофор и счётчики здесь не считаются: они пришли от верификатора.
"""

from __future__ import annotations

import functools
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from validator.checklists import load_checklists
from validator.lint.rules import RULE_BY_ID
from validator.models import (
    Addressee,
    ChecklistUse,
    CriticAction,
    CriticDecision,
    DiagLevel,
    DiscardReason,
    EvidenceMetrics,
    Finding,
    QuestionPriority,
    Quote,
    Report,
    ReportStatus,
    Requirement,
    Scope,
    Witness,
    WitnessReading,
)
from validator.report import triage
from validator.report.clarify_view import CLARIFY_TITLE, CLOSED_NOTE, clarify_view
from validator.report.coverage import (
    LEGEND,
    NO_REQUIREMENTS_NOTE,
    NOT_CHECKED_NOTE,
    SET_COLUMN,
    SET_COLUMN_LABEL,
    SET_COLUMN_REFS,
    SHORT_LABELS,
    STATE_LABELS,
    cell_text,
    coverage_totals,
    part_search_text,
    row_cells,
    unchecked_parts_note,
)
from validator.report.first_page import (
    TZ_CLASSES_TITLE,
    critical_items,
    fix_intro,
    fix_items,
    tz_class_findings,
)
from validator.report.fmt import FILTERS, ChecklistItems, found_text, plain, where
from validator.report.passport_view import (
    TECH_TITLE,
    calls_text,
    duration_line,
    passport_rows,
    reuse_cell,
    reuse_header,
    tokens_in,
    tokens_text,
)
from validator.report.run_metrics import step_label
from validator.report.testdraft_view import draft_stats, draft_view, stats_line

SEGMENTATION_NOTE = (
    "Требования выделила модель, а цитату и место каждого требования код взял из текста документа."
)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
NO_VIOLATIONS = "нарушений нет"
"""Статус требования без находок: слова из ТЗ п. 6, по ним тесты ищут каждое R-NN."""
NOT_SEARCHED = "не проверено моделью"
"""Статус требования, по месту которого не отработал ни один проход поиска (#126): «нарушений нет»
здесь было бы неправдой, без модели нарушения искали только правила линтера."""
NOT_SEARCHED_REST = "остальное не проверено моделью"

_BLANK_RUNS = re.compile(r"\n{3,}")

Resolver = Callable[[Quote, Sequence[str]], list[str]]
"""Раздел документа для цитаты (с запасным разделом находки): ``section_of`` в шаблонах."""


@dataclass(frozen=True)
class RequirementRow:
    """Строка раздела «Проверка каждого требования»: у каждого R-NN есть статус."""

    requirement: Requirement
    finding_ids: tuple[str, ...]
    status: str
    searched: bool = True


def _environment() -> Environment:
    # Шаблон выдаёт Markdown, а не HTML: экранирование HTML здесь исказило бы цитаты.
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=False,  # noqa: S701
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    env.filters.update(FILTERS)
    return env


def requirement_rows(report: Report) -> list[RequirementRow]:
    """Статус каждого требования: список F-NN с важностью и вердиктом, «нарушений нет» или, если поиск
    по месту требования не отработал, «не проверено моделью» (как «–» в матрице покрытия)."""
    ver, seg = report.verification, report.segmentation
    if ver is None or seg is None:
        return []
    by_id = {f.id: f for f in ver.findings}
    searched = {r.requirement_id: r.searched for r in report.coverage.rows} if report.coverage else {}
    rows: list[RequirementRow] = []
    for req in seg.requirements:
        ids = tuple(ver.requirement_status.get(req.id, ()))
        known = [by_id[i] for i in ids if i in by_id]
        parts = [f"{f.id} ({f.severity.label}, {f.verdict.label})" for f in known]
        parts += [i for i in ids if i not in by_id]
        checked = searched.get(req.id, True)
        if parts:
            status = ", ".join(parts) + ("" if checked else f"; {NOT_SEARCHED_REST}")
        else:
            status = NO_VIOLATIONS if checked else NOT_SEARCHED
        rows.append(RequirementRow(req, ids, status, checked))
    return rows


def requirements_intro(rows: Sequence[RequirementRow]) -> str:
    """Строка над таблицей раздела 4: сколько требований без нарушений и сколько не проверено моделью."""
    clean = sum(1 for r in rows if not r.finding_ids and r.searched)
    text = (
        "Статус есть у каждого требования из таблицы нарезки. Подробности по каждой находке ниже. "
        f"Требований без нарушений: {clean} из {len(rows)}."
    )
    missed = sum(1 for r in rows if not r.searched)
    if missed:
        text += (
            f" Не проверено моделью: {missed} (ни один проход поиска по их месту не отработал, "
            "смотрите диагностику прогона)."
        )
    return text


def _finding_sections(
    findings: Sequence[Finding], requirements: Sequence[Requirement]
) -> dict[str, list[str]]:
    """Раздел документа для каждой находки: у набора свой, у требования берём раздел первого требования."""
    by_req = {r.id: r.section_path for r in requirements}
    sections: dict[str, list[str]] = {}
    for f in findings:
        path = list(f.section_path)
        if not path and f.scope is Scope.REQUIREMENT:
            path = next((by_req[i] for i in f.requirement_ids if by_req.get(i)), [])
        sections[f.id] = path
    return sections


def _section_resolver(requirements: Sequence[Requirement]) -> Resolver:
    """Раздел документа для цитаты: путь заголовков из её якоря (#119).

    Якорь хранит раздел блока, где цитата найдена, в том числе блока-примера или шаблона, который
    не вошёл ни в одно требование. Без раздела в якоре (отчёт старой версии) берём раздел самого
    узкого требования, в которое попадает цитата, иначе запасной раздел находки.
    """
    spans = [(r.anchor.char_start, r.anchor.char_end, r.section_path) for r in requirements]

    def resolve(quote: Quote, fallback: Sequence[str] = ()) -> list[str]:
        if quote.anchor is None:
            return list(fallback)
        if quote.anchor.section:
            return list(quote.anchor.section)
        start = quote.anchor.char_start
        inside = [(end - begin, path) for begin, end, path in spans if begin <= start < end and path]
        return list(min(inside, key=lambda item: item[0])[1]) if inside else list(fallback)

    return resolve


@dataclass(frozen=True)
class EvidenceLine:
    """Строка карточки находки о шаге 6 (свидетель или критик): заголовок, текст, подпункты. Без разметки:
    Markdown экранирует её фильтром ``cell``, PDF показывает как есть."""

    head: str
    text: str
    items: tuple[str, ...] = ()
    item_label: str = "Прочтение"


CRITIC_DROPPED_INTRO = (
    "Критик перепроверил рискованные находки по документу. Эти находки сняты: критик указал место документа, "
    "которое на них отвечает, код сверил цитату-основание с документом и проверил, что она не повторяет "
    "цитату самой находки."
)


def _place(q: Quote, resolve: Resolver, fallback: Sequence[str] = ()) -> str:
    return where(q.anchor, resolve(q, fallback))


def _same_quote(a: Quote, b: Quote) -> bool:
    if a.anchor is not None and b.anchor is not None:
        return (a.anchor.char_start, a.anchor.char_end) == (b.anchor.char_start, b.anchor.char_end)
    return plain(a.located or a.text) == plain(b.located or b.text)


def _reading_source(
    r: WitnessReading, quotes: Sequence[Quote], resolve: Resolver, section: Sequence[str]
) -> str:
    """Опора прочтения: «цитата 2» карточки, если это она (место уже есть выше), иначе цитата с местом."""
    located = r.located
    if located is not None:
        number = next((i for i, q in enumerate(quotes, 1) if q.verified and _same_quote(q, located)), None)
        if number is not None:
            return f"по цитате {number}"
    place = f" ({_place(located, resolve, section)})" if located else ""
    return f"по «{plain(r.quote, 200)}»{place}"


def witness_line(
    w: Witness | None, resolve: Resolver, section: Sequence[str] = (), quotes: Sequence[Quote] = ()
) -> EvidenceLine | None:
    """Пример-свидетель в карточке: проверенный со сценарием и прочтениями, отклонённый или не построенный.

    Прочтение, которое опирается на цитату карточки, ссылается на неё номером, без повтора текста и места.
    """
    if w is None:
        return None
    if not w.checked:
        if not w.scenario.strip():
            return EvidenceLine("Пример-свидетель", plain(w.note))
        return EvidenceLine(
            "Пример-свидетель не подтверждён", f"{plain(w.note)} Сценарий модели: {plain(w.scenario)}"
        )
    items = []
    for r in w.readings:
        how = f"если понимать как «{plain(r.reading)}»: " if r.reading else ""
        items.append(f"{how}{_reading_source(r, quotes, resolve, section)} → {plain(r.expectation)}")
    return EvidenceLine(
        "Пример-свидетель (код сверил цитаты и различие ожиданий)", plain(w.scenario), tuple(items)
    )


def _basis(d: CriticDecision, resolve: Resolver, section: Sequence[str]) -> str:
    if d.basis is None:
        return ""
    return (
        f" Основание: «{plain(d.basis.located or d.basis.text, 200)}» ({_place(d.basis, resolve, section)})."
    )


def critic_line(
    d: CriticDecision | None, resolve: Resolver, section: Sequence[str] = ()
) -> EvidenceLine | None:
    """Решение критика в карточке. Понижение с причиной показывает и примечание верификатора о важности."""
    if d is None:
        return None
    basis = _basis(d, resolve, section)
    if d.proposed is not None:
        text = f"предлагал «{d.proposed.label}». {_sentence(d.reason)}{basis} {plain(d.note)}"
        return EvidenceLine("Критик", text.strip())
    if d.action is CriticAction.DOWNGRADE:
        return EvidenceLine("Критик", f"понизил важность, причина в примечании.{basis}")
    return EvidenceLine("Критик", f"подтвердил находку. {_sentence(d.reason)}{basis}")


def _sentence(text: str) -> str:
    """Фраза модели как предложение: с заглавной буквы и с точкой в конце."""
    value = plain(text).rstrip(".")
    return f"{value[:1].upper()}{value[1:]}." if value else ""


def critic_dropped(report: Report, resolve: Resolver) -> list[str]:
    """Находки, которые снял критик: суть, цитата находки, причина и сверенная цитата-основание."""
    ver = report.verification
    if ver is None:
        return []
    out: list[str] = []
    for d in ver.discarded:
        m, decision = d.finding, d.finding.critic
        if d.reason is not DiscardReason.CRITIC_REJECTED or decision is None:
            continue
        what = plain(m.title or m.explanation, 200)
        quote = f" Цитата находки: «{plain(m.quotes[0], 160)}»." if m.quotes else ""
        out.append(
            f"{m.mid} · {m.defect_type.label} · {m.property.label}: {what}.{quote} "
            f"Причина: {_sentence(decision.reason)}{_basis(decision, resolve, ())}"
        )
    return out


def evidence_metric_rows(m: EvidenceMetrics | None) -> list[tuple[str, str]]:
    """Строки «Метрик прогона» о шаге 6; пусто, если шаг модель не вызывал."""
    if m is None:
        return []
    rows: list[tuple[str, str]] = []
    if m.witness_requested:
        rows.append(
            (
                "Пример-свидетель",
                f"модель ответила по {m.witness_requested} находкам: проверен кодом {m.witness_checked}, "
                f"отклонён кодом {m.witness_rejected}, сценария нет {m.witness_declined}",
            )
        )
    if not m.critic_enabled:
        rows.append(("Критик находок", "выключен в этом прогоне, включается флагом --critic"))
    elif m.critic_reviewed:
        rows.append(
            (
                "Критик находок",
                f"проверено {m.critic_reviewed}: оставлено {m.critic_kept}, понижено {m.critic_downgraded}, "
                f"снято {m.critic_dropped}; код не принял как есть предложений: {m.critic_overruled}",
            )
        )
    return rows


def _evidence_lines(
    findings: Sequence[Finding], resolve: Resolver, sections: dict[str, list[str]]
) -> dict[str, list[EvidenceLine]]:
    out: dict[str, list[EvidenceLine]] = {}
    for f in findings:
        section = sections.get(f.id, [])
        lines = [witness_line(f.witness, resolve, section, f.quotes), critic_line(f.critic, resolve, section)]
        out[f.id] = [line for line in lines if line is not None]
    return out


def _questions_by_finding(report: Report) -> dict[str, list[str]]:
    links: dict[str, list[str]] = defaultdict(list)
    for q in report.questions:
        for fid in q.related_findings:
            links[fid].append(q.id)
    return dict(links)


def _addressee_label(value: str) -> str:
    """Адресат из метрик вопросов (``by_addressee`` хранит значения перечисления) по-русски."""
    try:
        return Addressee(value).label
    except ValueError:
        return value


def _input_status(report: Report) -> str:
    if report.status is ReportStatus.NOT_PERFORMED:
        return "НЕ ПРОЧИТАН: проверка невозможна"
    if any(d.level is not DiagLevel.INFO for d in report.input_diagnostics):
        return "ЧАСТИЧНО: проверка выполнена, есть предупреждения"
    return "ПРОЧИТАН ПОЛНОСТЬЮ"


CHECKLISTS_NOTE = (
    "Доменный чек-лист включается, только если слова его области встретились хотя бы в нескольких блоках "
    "документа. Пункты включённых чек-листов проход поиска получил как подсказки, где искать; находка по "
    "пункту проходит ту же сверку цитат, что и остальные."
)


def checklist_reason(use: ChecklistUse) -> str:
    """Почему чек-лист включён или нет: число блоков со словами области и примеры слов."""
    words = ", ".join(f"«{w}»" for w in use.triggers)
    found = f"слова области в {use.matched_blocks} блоках (нужно от {use.min_blocks})"
    return f"{found}: {words}" if words else found


def checklist_state(use: ChecklistUse, enabled: bool) -> str:
    if use.used:
        return "включён"
    if not enabled and use.triggered:
        return "сработал, но чек-листы выключены"
    return "не включён"


def checklists_passport(report: Report) -> str:
    """Строка паспорта: какие чек-листы ушли в промпт поиска."""
    p, uses = report.passport, report.checklists
    failed = any(d.step == "checklists" and d.level is not DiagLevel.INFO for d in report.diagnostics)
    if failed:
        return "не выбраны: шаг выбора чек-листов упал, смотрите диагностику прогона"
    if not uses:
        return "—"
    if not p.checklists_enabled:
        return "выключены (--no-checklists)"
    used = [u for u in uses if u.used]
    if not used:
        return "ни один не включён: слов их областей в документе мало"
    items = sum(u.items for u in used)
    return f"{', '.join(u.title for u in used)} ({items} пунктов)"


@functools.cache
def checklist_items() -> ChecklistItems:
    """Пункты доменных чек-листов: ID → (название чек-листа, вопрос), чтобы карточка говорила словами."""
    return {i.id: (c.title, i.question) for c in load_checklists() for i in c.items}


def lint_rules() -> dict[str, str]:
    return {rule_id: rule.name for rule_id, rule in RULE_BY_ID.items()}


def found(f: Finding) -> str:
    return found_text(f, checklist_items(), lint_rules())


def next_steps(report: Report) -> list[str]:
    """Что сделать человеку, если проверка не выполнена: подсказки ошибок входа без повторов (#38).

    В таблице диагностики подсказка стоит в узкой ячейке; длинную инструкцию (скан: где взять текстовый
    PDF, как включить распознавание) отчёт повторяет отдельным списком под итогом.
    """
    if report.status is not ReportStatus.NOT_PERFORMED:
        return []
    hints = (d.hint.strip() for d in report.input_diagnostics if d.level is DiagLevel.ERROR)
    return list(dict.fromkeys(h for h in hints if h))


PARTS_NOTE = (
    "Документ длинный, поэтому нарезка и поиск дефектов шли по частям: части собраны из целых разделов, "
    "ID блоков, номера требований и места в отчёте общие для всего документа. Противоречия и повторы "
    "между частями сверены отдельным вызовом модели по списку всех требований."
)
"""Пояснение к таблице частей длинного документа (#38), общее для Markdown и PDF."""


def withdrawn_line(report: Report) -> str | None:
    """Почему в нумерации вопросов дыра (#170): снятые самопроверкой номера не переиспользуются.

    Строка нужна только для дыры, то есть для снятого номера меньше последнего оставшегося: снятый
    последний номер в нумерации не виден. ``None``, если дыр нет.
    """
    last = max((_question_number(q.id) for q in report.questions), default=0)
    ids = [w.question.id for w in report.questions_withdrawn if 0 < _question_number(w.question.id) < last]
    if not ids:
        return None
    many = len(ids) > 1
    return (
        f"{'Номера' if many else 'Номер'} {', '.join(ids)} {'пропущены' if many else 'пропущен'}: "
        f"{'эти вопросы сняла' if many else 'этот вопрос сняла'} самопроверка, ответ есть в документе "
        "(раздел 6, «Снятые вопросы»)."
    )


def _question_number(qid: str) -> int:
    """Номер вопроса из «Q-09»; 0 для номера в другом виде (такой в дыру не считаем)."""
    tail = qid.rpartition("-")[2]
    return int(tail) if tail.isdigit() else 0


def report_context(report: Report) -> dict[str, Any]:
    """Всё, что нужно шаблонам отчёта: готовые списки, связи и подписи. Общий источник для MD и PDF."""
    ver = report.verification
    findings: list[Finding] = list(ver.findings) if ver else []
    requirements: list[Requirement] = list(report.segmentation.requirements) if report.segmentation else []
    passport_main, passport_tech = passport_rows(report, checklists_passport(report))
    blocking = {
        priority: [q.id for q in report.questions if q.priority is priority]
        for priority in (QuestionPriority.P0, QuestionPriority.P1)
    }
    sections = _finding_sections(findings, requirements)
    resolve = _section_resolver(requirements)
    by_finding = _questions_by_finding(report)
    order = report.conclusion.fix_first
    first = fix_items(order, {f.id: f for f in findings}, resolve, sections, by_finding)
    rows = requirement_rows(report)
    parent = {r.id: r.parent_id for r in requirements}
    keep = set(report.conclusion.fix_first) | {
        fid for q in report.questions if q.priority in blocking for fid in q.related_findings
    }
    req_main, req_minor = triage.split_minor(
        triage.reading_order([f for f in findings if f.scope is Scope.REQUIREMENT], parent), keep
    )
    set_main, set_minor = triage.split_minor(
        triage.reading_order([f for f in findings if f.scope is Scope.SET], parent), keep
    )
    return {
        "report": report,
        "findings_by_id": {f.id: f for f in findings},
        "requirements_by_id": {r.id: r for r in requirements},
        "requirement_rows": rows,
        "requirements_intro": requirements_intro(rows),
        "req_findings": req_main,
        "req_minor": req_minor,
        "set_findings": set_main,
        "set_minor": set_minor,
        "minor_title": triage.MINOR_BLOCK_TITLE,
        "minor_note_req": triage.MINOR_BLOCK_NOTE_REQ,
        "minor_note_set": triage.MINOR_BLOCK_NOTE_SET,
        "empty_req": triage.NO_REQ_MAIN if req_minor else triage.NO_REQ_FINDINGS,
        "empty_set": triage.NO_SET_MAIN if set_minor else triage.NO_SET_FINDINGS,
        "review_line": triage.review_line(ver.summary, len(req_minor) + len(set_minor)) if ver else None,
        "tz_classes": tz_class_findings(findings),
        "tz_classes_title": TZ_CLASSES_TITLE,
        "questions_by_id": {q.id: q for q in report.questions},
        "finding_sections": sections,
        "section_of": resolve,
        "evidence_lines": _evidence_lines(findings, resolve, sections),
        "critic_dropped": critic_dropped(report, resolve),
        "critic_dropped_intro": CRITIC_DROPPED_INTRO,
        "evidence_rows": evidence_metric_rows(report.metrics.evidence),
        "questions_by_finding": by_finding,
        "fix_items": first,
        "fix_intro": fix_intro(first),
        "critical_items": critical_items(findings, order, resolve, sections, by_finding),
        "blocks_development": blocking[QuestionPriority.P0],
        "blocks_testing": blocking[QuestionPriority.P1],
        "input_status": _input_status(report),
        "checklists_passport": checklists_passport(report),
        "checklist_reason": checklist_reason,
        "checklist_state": checklist_state,
        "checklists_note": CHECKLISTS_NOTE,
        "next_steps": next_steps(report),
        "found": found,
        "segmentation_note": SEGMENTATION_NOTE,
        "passport_main": passport_main,
        "passport_tech": passport_tech,
        "passport_tech_title": TECH_TITLE,
        "passport_duration": duration_line(report),
        "withdrawn_line": withdrawn_line(report),
        "tokens_text": tokens_text,
        "calls_text": calls_text,
        "reuse_header": reuse_header,
        "reuse_cell": reuse_cell,
        "tokens_in": tokens_in,
        "parts_note": PARTS_NOTE,
        "no_violations": NO_VIOLATIONS,
        "addressee_label": _addressee_label,
        "clarify": clarify_view(report),
        "clarify_title": CLARIFY_TITLE,
        "closed_note": CLOSED_NOTE,
        "step_label": step_label,
        "coverage_text": cell_text,
        "coverage_short": SHORT_LABELS,
        "coverage_legend": LEGEND,
        "coverage_not_checked_note": NOT_CHECKED_NOTE,
        "coverage_no_requirements_note": NO_REQUIREMENTS_NOTE,
        "coverage_parts_note": unchecked_parts_note(report.coverage, report.document.parts)
        if report.coverage and report.document
        else None,
        "coverage_set_column": SET_COLUMN,
        "coverage_set_column_label": SET_COLUMN_LABEL,
        "coverage_set_column_refs": "; ".join(SET_COLUMN_REFS),
        "coverage_row_cells": row_cells,
        "part_search": part_search_text,
        "coverage_state_label": STATE_LABELS,
        "coverage_totals": {k.value: n for k, n in coverage_totals(report.coverage).items()}
        if report.coverage
        else {},
        "test_drafts": {f.id: v for f in findings if (v := draft_view(f)) is not None},
        "test_draft_line": stats_line(stats) if (stats := draft_stats(findings)) else None,
    }


def render_md(report: Report) -> str:
    text = _environment().get_template("report.md.j2").render(**report_context(report))
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return _BLANK_RUNS.sub("\n\n", text) + "\n"
