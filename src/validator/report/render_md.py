"""Шаг 9б: отчёт в Markdown из ``Report`` на Jinja2.

Шаблон ``templates/report.md.j2`` только раскладывает данные по разделам ТЗ п. 6. Всё, что требует
выбора или подсчёта по данным (статус каждого требования, место находки, связи находка ↔ вопрос),
готовит этот модуль, чтобы шаблон оставался плоским и его можно было проверить тестами.
Важность, светофор и счётчики здесь не считаются: они пришли от верификатора.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from validator.models import (
    Addressee,
    CriticAction,
    CriticDecision,
    DefectType,
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
)
from validator.report.clarify_view import CLARIFY_TITLE, CLOSED_NOTE, clarify_view
from validator.report.coverage import (
    LEGEND,
    NOT_CHECKED_NOTE,
    SHORT_LABELS,
    STATE_LABELS,
    cell_text,
    coverage_totals,
)
from validator.report.fmt import FILTERS, found_text, plain, where
from validator.report.run_metrics import step_label

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
NO_VIOLATIONS = "нарушений нет"
"""Статус требования без находок: слова из ТЗ п. 6, по ним тесты ищут каждое R-NN."""

_BLANK_RUNS = re.compile(r"\n{3,}")
_CRITICAL_CLASSES = frozenset(d for d in DefectType if d.critical_class)

Resolver = Callable[[Quote, Sequence[str]], list[str]]
"""Раздел документа для цитаты (с запасным разделом находки): ``section_of`` в шаблонах."""


@dataclass(frozen=True)
class RequirementRow:
    """Строка раздела «Проверка каждого требования»: у каждого R-NN есть статус."""

    requirement: Requirement
    finding_ids: tuple[str, ...]
    status: str


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
    """Статус каждого требования: «нарушений нет» или список F-NN с важностью и вердиктом."""
    ver, seg = report.verification, report.segmentation
    if ver is None or seg is None:
        return []
    by_id = {f.id: f for f in ver.findings}
    rows: list[RequirementRow] = []
    for req in seg.requirements:
        ids = tuple(ver.requirement_status.get(req.id, ()))
        known = [by_id[i] for i in ids if i in by_id]
        parts = [f"{f.id} ({f.severity.label}, {f.verdict.label})" for f in known]
        parts += [i for i in ids if i not in by_id]
        rows.append(RequirementRow(req, ids, ", ".join(parts) if parts else NO_VIOLATIONS))
    return rows


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
    """Раздел документа для цитаты: раздел самого узкого требования, в которое она попадает."""
    spans = [(r.anchor.char_start, r.anchor.char_end, r.section_path) for r in requirements]

    def resolve(quote: Quote, fallback: Sequence[str] = ()) -> list[str]:
        if quote.anchor is None:
            return list(fallback)
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


def witness_line(w: Witness | None, resolve: Resolver, section: Sequence[str] = ()) -> EvidenceLine | None:
    """Пример-свидетель в карточке: проверенный со сценарием и прочтениями, отклонённый или не построенный."""
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
        place = f" ({_place(r.located, resolve, section)})" if r.located else ""
        items.append(f"{how}по «{plain(r.quote, 200)}»{place} → {plain(r.expectation)}")
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
        lines = [witness_line(f.witness, resolve, section), critic_line(f.critic, resolve, section)]
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


def report_context(report: Report) -> dict[str, Any]:
    """Всё, что нужно шаблонам отчёта: готовые списки, связи и подписи. Общий источник для MD и PDF."""
    ver = report.verification
    findings: list[Finding] = list(ver.findings) if ver else []
    requirements: list[Requirement] = list(report.segmentation.requirements) if report.segmentation else []
    blocking = {
        priority: [q.id for q in report.questions if q.priority is priority]
        for priority in (QuestionPriority.P0, QuestionPriority.P1)
    }
    sections = _finding_sections(findings, requirements)
    resolve = _section_resolver(requirements)
    return {
        "report": report,
        "findings_by_id": {f.id: f for f in findings},
        "requirements_by_id": {r.id: r for r in requirements},
        "requirement_rows": requirement_rows(report),
        "req_findings": [f for f in findings if f.scope is Scope.REQUIREMENT],
        "set_findings": [f for f in findings if f.scope is Scope.SET],
        "critical_classes": [f for f in findings if f.defect_type in _CRITICAL_CLASSES],
        "questions_by_id": {q.id: q for q in report.questions},
        "finding_sections": sections,
        "section_of": resolve,
        "evidence_lines": _evidence_lines(findings, resolve, sections),
        "critic_dropped": critic_dropped(report, resolve),
        "critic_dropped_intro": CRITIC_DROPPED_INTRO,
        "evidence_rows": evidence_metric_rows(report.metrics.evidence),
        "questions_by_finding": _questions_by_finding(report),
        "blocks_development": blocking[QuestionPriority.P0],
        "blocks_testing": blocking[QuestionPriority.P1],
        "input_status": _input_status(report),
        "next_steps": next_steps(report),
        "found": found_text,
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
        "coverage_state_label": STATE_LABELS,
        "coverage_totals": {k.value: n for k, n in coverage_totals(report.coverage).items()}
        if report.coverage
        else {},
    }


def render_md(report: Report) -> str:
    text = _environment().get_template("report.md.j2").render(**report_context(report))
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    return _BLANK_RUNS.sub("\n\n", text) + "\n"
