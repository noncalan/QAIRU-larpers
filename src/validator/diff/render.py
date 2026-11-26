"""Отчёт сравнения версий: Markdown по-русски в стиле отчёта агента и машинный JSON."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from validator.diff.load import FindingView, PassportView, ReportView
from validator.diff.match import REQ_SIMILARITY
from validator.diff.model import DiffResult, FindingClass, FindingDiff, ReqPair, ReqStatus
from validator.models import Property, TrafficLight
from validator.report.fmt import cell, money

DIFF_SCHEMA = "1"
CRITICAL = "critical"
_SEVERITY_LABEL = {"critical": "критично", "major": "важно", "minor": "незначительно"}
_SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2}
_QUOTE_LIMIT = 160
_TEXT_LIMIT = 120

NOISE_NOTE = (
    "Агент между прогонами находит не ровно одно и то же: модель недетерминирована. Поэтому находка, "
    "место которой в документе не менялось, не считается ни исправленной, ни новой. Эти строки не "
    "входят в «исправлено» и в регрессии, их стоит просмотреть, но не засчитывать."
)


@dataclass(frozen=True)
class Totals:
    """Сводка сравнения: всё, что нужно первой странице и JSON."""

    old_findings: int
    old_critical: int
    fixed: int
    fixed_critical: int
    remained: int
    remained_critical: int
    regressions: int
    regressions_critical: int
    vanished: int
    noise_new: int
    reqs: dict[str, int]


def _critical(items: Sequence[FindingDiff]) -> int:
    return sum(1 for d in items if d.main.severity == CRITICAL)


def totals(result: DiffResult) -> Totals:
    fixed = result.of(FindingClass.FIXED)
    remained = result.of(FindingClass.REMAINED)
    regressions = result.of(FindingClass.NEW_CHANGED)
    return Totals(
        old_findings=len(result.old.findings),
        old_critical=sum(1 for f in result.old.findings if f.severity == CRITICAL),
        fixed=len(fixed),
        fixed_critical=sum(1 for d in fixed if d.old is not None and d.old.severity == CRITICAL),
        remained=len(remained),
        remained_critical=_critical(remained),
        regressions=len(regressions),
        regressions_critical=_critical(regressions),
        vanished=len(result.of(FindingClass.VANISHED)),
        noise_new=len(result.of(FindingClass.NEW_UNCHANGED)),
        reqs={s.value: len(result.reqs(s)) for s in ReqStatus},
    )


def _light(value: str) -> str:
    try:
        light = TrafficLight(value)
    except ValueError:
        return value or "—"
    return f"{light.icon} {light.label}"


def _icon(value: str) -> str:
    try:
        return TrafficLight(value).icon
    except ValueError:
        return "?"


def _prop(code: str) -> str:
    try:
        return Property(code).info.label
    except ValueError:
        return code


def _sorted(items: Sequence[FindingDiff]) -> list[FindingDiff]:
    return sorted(items, key=lambda d: (_SEVERITY_RANK.get(d.main.severity, 9), d.main.id))


def _quote(f: FindingView | None) -> str:
    if f is None or not f.quotes:
        return "—"
    q = f.quotes[0]
    text = f"«{cell(q.text, _QUOTE_LIMIT)}»"
    return f"{text} ({q.where})" if q.where else text


def _verdict(result: DiffResult, t: Totals) -> str:
    """Ответ первой страницы: можно ли теперь начинать разработку."""
    new_light = result.new.traffic_light
    if new_light == TrafficLight.GREEN and not t.regressions_critical:
        return "Да: новая версия прошла проверку без критичных нарушений."
    if new_light == TrafficLight.RED or t.remained_critical or t.regressions_critical:
        blockers = t.remained_critical + t.regressions_critical
        return (
            f"Нет: в новой версии остаются критичные нарушения ({blockers}), их нужно исправить до начала "
            "разработки и тестирования."
        )
    return "С оговорками: критичных нарушений нет, но важные остались. Список ниже."


def _first_page(result: DiffResult, t: Totals) -> list[str]:
    old, new = result.old, result.new
    lines = [
        "## Итог на первой странице",
        "",
        f"**Светофор: {_icon(old.traffic_light)} → {_light(new.traffic_light)}**",
        "",
        f"**Можно ли теперь начинать разработку?** {_verdict(result, t)}",
        "",
        f"- Исправлено находок: **{t.fixed} из {t.old_findings}**, из них критичных **{t.fixed_critical} "
        f"из {t.old_critical}**.",
        f"- Осталось: **{t.remained}** (критичных {t.remained_critical}).",
        f"- Новое на изменённых местах, возможная регрессия от правок: **{t.regressions}** "
        f"(критичных {t.regressions_critical}).",
        f"- Не засчитано, нестабильность модели: пропало без правки {t.vanished}, новое на неизменённом "
        f"месте {t.noise_new}.",
        f"- Требования: без изменений {t.reqs['unchanged']}, изменено {t.reqs['changed']}, удалено "
        f"{t.reqs['removed']}, добавлено {t.reqs['added']}.",
    ]
    if new.headline:
        lines += ["", f"Итог проверки новой версии: {cell(new.headline)}"]
    if result.warnings:
        lines += ["", "**Предупреждения**", ""]
        lines += [f"- ⚠ {w}" for w in result.warnings]
    return lines


def _passport_rows(a: PassportView, b: PassportView, ra: ReportView, rb: ReportView) -> list[str]:
    def row(name: str, x: object, y: object) -> str:
        return f"| {name} | {cell(x) or '—'} | {cell(y) or '—'} |"

    def passes(p: PassportView) -> str:
        return f"{p.profile or '—'}, проходов {p.passes if p.passes is not None else '—'}"

    return [
        "| Параметр | Было | Стало |",
        "|---|---|---|",
        row("Документ", a.document_name, b.document_name),
        row("SHA-256 документа", a.document_sha256, b.document_sha256),
        row("Версия агента", a.agent_version, b.agent_version),
        row("Хеш промптов", a.prompts_sha256[:12], b.prompts_sha256[:12]),
        row("Дата прогона", a.started_at, b.started_at),
        row("Профиль", passes(a), passes(b)),
        row("Цена прогона по тарифу API", money(a.cost_usd), money(b.cost_usd)),
        row("Отчёт", ra.path, rb.path),
    ]


def _summary_rows(result: DiffResult) -> list[str]:
    old, new = result.old, result.new

    def crit(v: ReportView) -> int:
        return sum(1 for f in v.findings if f.severity == CRITICAL)

    return [
        "| Показатель | Было | Стало |",
        "|---|---|---|",
        f"| Светофор | {_light(old.traffic_light)} | {_light(new.traffic_light)} |",
        f"| Требований | {len(old.requirements)} | {len(new.requirements)} |",
        f"| Находок | {len(old.findings)} | {len(new.findings)} |",
        f"| Критичных | {crit(old)} | {crit(new)} |",
        f"| Вопросов автору | {old.questions} | {new.questions} |",
    ]


def _finding_table(items: Sequence[FindingDiff], empty: str) -> list[str]:
    if not items:
        return [empty]
    lines = [
        "| Было | Стало | Важность | Свойство | Суть | Цитата | Почему так |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in _sorted(items):
        f = d.main
        lines.append(
            f"| {d.old.id if d.old else '—'} | {d.new.id if d.new else '—'} "
            f"| {_SEVERITY_LABEL.get(f.severity, f.severity)} | {cell(_prop(f.property))} "
            f"| {cell(f.title, _TEXT_LIMIT)} | {_quote(d.old or d.new)} | {cell(d.reason) or '—'} |"
        )
    return lines


def _findings_part(result: DiffResult) -> list[str]:
    remained = result.of(FindingClass.REMAINED)
    critical = [d for d in remained if d.main.severity == CRITICAL]
    other = [d for d in remained if d.main.severity != CRITICAL]
    return [
        "## 3. Находки",
        "",
        "### 3.1. Критичные, которые остались",
        "",
        *_finding_table(critical, "Критичных находок, которые остались, нет."),
        "",
        "### 3.2. Новое на изменённом месте: возможная регрессия от правки",
        "",
        "Находок не было в старой версии, а их цитата взята из исправленного текста. Проверьте, не "
        "внесла ли правка новую проблему.",
        "",
        *_finding_table(result.of(FindingClass.NEW_CHANGED), "Новых находок на изменённых местах нет."),
        "",
        "### 3.3. Исправлено",
        "",
        "Находки нет в новой версии, и её место в документе изменено или удалено.",
        "",
        *_finding_table(result.of(FindingClass.FIXED), "Исправленных находок нет."),
        "",
        "### 3.4. Осталось (не критичные)",
        "",
        *_finding_table(other, "Других оставшихся находок нет."),
        "",
        "## 4. Нестабильность модели: не засчитано",
        "",
        NOISE_NOTE,
        "",
        "### 4.1. Пропало без правки",
        "",
        *_finding_table(result.of(FindingClass.VANISHED), "Таких находок нет."),
        "",
        "### 4.2. Новое на неизменённом месте",
        "",
        *_finding_table(result.of(FindingClass.NEW_UNCHANGED), "Таких находок нет."),
    ]


def _req_rows(pairs: Sequence[ReqPair]) -> list[str]:
    changed = [p for p in pairs if p.status is not ReqStatus.UNCHANGED]
    unchanged = len(pairs) - len(changed)
    lines = [f"Без изменений: {unchanged}. Ниже только изменённые, удалённые и добавленные."]
    if not changed:
        return [*lines, "", "Изменённых требований нет."]
    lines += [
        "",
        "| Было | Стало | Статус | Как сопоставлено | Текст было | Текст стало |",
        "|---|---|---|---|---|---|",
    ]
    for p in changed:
        how = p.how.label + (f", {p.score:.0f} %" if p.score is not None else "")
        lines.append(
            f"| {p.old.id if p.old else '—'} | {p.new.id if p.new else '—'} | {p.status.label} | {how} "
            f"| {cell(p.old.quote, _TEXT_LIMIT) if p.old else '—'} "
            f"| {cell(p.new.quote, _TEXT_LIMIT) if p.new else '—'} |"
        )
    return lines


def _method() -> list[str]:
    return [
        "## 6. Как сделано сравнение",
        "",
        "Сравнение не вызывает модель: оно читает два готовых отчёта агента и, если переданы, тексты обеих "
        "версий документа. Требования сопоставлены сначала по хешу текста (stable_key), затем по номеру в "
        f"документе, затем по похожести текста не ниже {REQ_SIMILARITY:.0f} %. Находки сопоставлены по "
        "свойству стандарта и месту: общее требование или общая цитата. Подробности: docs/version-diff.md.",
    ]


def render_markdown(result: DiffResult) -> str:
    t = totals(result)
    old, new = result.old, result.new
    title = old.passport.document_name or "документ"
    if new.passport.document_name and new.passport.document_name != title:
        title = f"{title} → {new.passport.document_name}"
    lines = [
        f"# Сравнение версий документа: {cell(title)}",
        "",
        "Что из найденного агентом в старой версии исправлено, что осталось и не появилось ли нового из-за "
        "правок. Сравнение сделал инструмент validator-diff по двум отчётам агента QAIRU validator.",
        "",
        *_first_page(result, t),
        "",
        "## 1. Паспорт сравнения",
        "",
        *_passport_rows(old.passport, new.passport, old, new),
        f"| Тексты документов | {'переданы' if result.texts else 'не переданы, места по отчётам'} | |",
        "",
        "## 2. Сводка было → стало",
        "",
        *_summary_rows(result),
        "",
        *_findings_part(result),
        "",
        "## 5. Требования",
        "",
        *_req_rows(result.requirements),
        "",
        *_method(),
    ]
    return "\n".join(lines) + "\n"


def _finding_json(f: FindingView | None) -> dict[str, Any] | None:
    if f is None:
        return None
    return {
        "id": f.id,
        "property": f.property,
        "severity": f.severity,
        "title": f.title,
        "quotes": [q.text for q in f.quotes],
        "requirement_ids": list(f.requirement_ids),
    }


def _passport_json(v: ReportView) -> dict[str, Any]:
    p = v.passport
    return {
        "report": v.path,
        "document_name": p.document_name,
        "document_sha256": p.document_sha256,
        "agent_version": p.agent_version,
        "prompts_sha256": p.prompts_sha256,
        "started_at": p.started_at,
        "traffic_light": v.traffic_light,
        "cost_usd": p.cost_usd,
    }


def render_json(result: DiffResult) -> dict[str, Any]:
    t = totals(result)
    return {
        "schema_version": DIFF_SCHEMA,
        "old": _passport_json(result.old),
        "new": _passport_json(result.new),
        "documents_compared": result.texts,
        "warnings": list(result.warnings),
        "summary": {
            "fixed": t.fixed,
            "old_findings": t.old_findings,
            "fixed_critical": t.fixed_critical,
            "old_critical": t.old_critical,
            "remained": t.remained,
            "remained_critical": t.remained_critical,
            "regressions": t.regressions,
            "regressions_critical": t.regressions_critical,
            "vanished_without_edit": t.vanished,
            "new_on_unchanged_place": t.noise_new,
            "requirements": t.reqs,
        },
        "requirements": [
            {
                "status": p.status.value,
                "old_id": p.old.id if p.old else None,
                "new_id": p.new.id if p.new else None,
                "matched_by": p.how.value,
                "similarity": p.score,
            }
            for p in result.requirements
        ],
        "findings": [
            {
                "class": d.cls.value,
                "reason": d.reason,
                "old": _finding_json(d.old),
                "new": _finding_json(d.new),
            }
            for d in result.findings
        ],
    }
