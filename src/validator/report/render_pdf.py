"""Шаг 9в: PDF-версия отчёта через Typst из того же ``Report``, что и Markdown.

PDF не получается из Markdown (pandoc и LaTeX у жюри не стоят). Здесь тот же ``Report`` и тот же
контекст (``render_md.report_context``) превращаются в плоскую модель вида: готовые строки, без
разметки и без вычислений, а ``report.typ`` только раскладывает её на страницы. Данные уходят в Typst
как JSON (``sys.inputs``), поэтому знаки ``#``, ``$``, ``*``, ``_`` в цитатах документа ничего не
значат для вёрстки и не требуют экранирования.

Шрифты с кириллицей лежат в ``fonts/`` рядом (Liberation, лицензия OFL) и передаются в Typst явно,
системные шрифты отключены: PDF выглядит одинаково на Windows, macOS и Linux.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import typst

from validator.errors import INT_STEP_DEGRADED, ValidatorError
from validator.models import (
    Diagnostic,
    Finding,
    Question,
    QuestionOption,
    Quote,
    Report,
    Requirement,
    Scope,
)
from validator.report.fmt import money, pct, plain, quote_plain, status_label, when, where
from validator.report.render_md import report_context

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = PACKAGE_DIR / "report.typ"
FONTS_DIR = PACKAGE_DIR / "fonts"
TITLE = "Отчёт о проверке требований"
STANDARDS = "Проверка по ISO/IEC/IEEE 29148:2018 (п. 5.2.5, 5.2.6) и IEEE 830-1998 (п. 4.3)."

FIRST_PAGE_TITLE = "Итог на первой странице"
SECTION_TITLES = {
    "passport": "1. Паспорт прогона",
    "input": "2. Диагностика входа",
    "segmentation": "3. Как агент нарезал требования",
    "requirements": "4. Проверка каждого требования",
    "set": "5. Проверка всего набора требований",
    "questions": "6. Вопросы автору ТЗ",
    "summary": "7. Сводка",
    "conclusion": "8. Итог",
    "metrics": "9. Метрики прогона",
    "diagnostics": "10. Диагностика прогона",
}
"""Заголовки разделов те же, что в Markdown (тест сверяет их с ``## `` из ``render_md``)."""

Json = dict[str, Any]


class PdfRenderError(ValidatorError):
    """Typst не собрал PDF: отчёт Markdown при этом остаётся."""

    default_code = INT_STEP_DEGRADED


# ---------------------------------------------------------------- блоки модели вида


def _table(
    head: Sequence[str],
    rows: Iterable[Sequence[str]],
    widths: Sequence[float],
    mono: Sequence[int] = (),
    hyph: Sequence[int] = (),
) -> Json:
    """Таблица: ``mono`` колонки с ID моноширинным шрифтом, ``hyph`` узкие колонки с переносом слов."""
    return {
        "t": "table",
        "head": list(head),
        "rows": [list(r) for r in rows],
        "widths": list(widths),
        "mono": list(mono),
        "hyph": list(hyph),
    }


def _kv(rows: Iterable[tuple[str, str]]) -> Json:
    return {"t": "kv", "rows": [[k, v] for k, v in rows]}


def _para(text: str) -> Json:
    return {"t": "para", "text": text}


def _bullets(items: Iterable[str]) -> Json:
    return {"t": "bullets", "items": list(items)}


def _note(text: str) -> Json:
    return {"t": "note", "text": text}


def _card(tone: str, head: str, rows: Iterable[tuple[str, Any]]) -> Json:
    return {"t": "card", "tone": tone, "head": head, "rows": [[k, v] for k, v in rows]}


def _diag_table(items: Sequence[Diagnostic]) -> Json:
    return _table(
        ["Код", "Уровень", "Что обнаружено", "Где", "Что сделал агент", "Что сделать вам"],
        (
            [
                d.code,
                d.level.label,
                plain(d.message),
                plain(d.location) or "—",
                plain(d.action) or "—",
                plain(d.hint) or "—",
            ]
            for d in items
        ),
        [0.08, 0.15, 0.26, 0.11, 0.2, 0.2],
        mono=[0],
        hyph=[1, 3, 4, 5],
    )


# ---------------------------------------------------------------- цитаты и карточки


def _quote_value(q: Quote, section: Sequence[str], limit: int = 300) -> Json:
    """Цитата и её место; если не сверена, говорим об этом прямо и показываем текст модели."""
    if not q.verified:
        return {"quote": plain(q.text, 200), "meta": "цитата не найдена в документе", "ok": False}
    meta = f"{where(q.anchor, section)}; сверено: {q.match.label}"
    if q.match.value == "fuzzy":
        meta += f", {q.score:.0f} %"
    return {"quote": quote_plain(q, limit), "meta": meta, "ok": True}


def _finding_card(f: Finding, ctx: dict[str, Any]) -> Json:
    section_of = ctx["section_of"]
    sections = ctx["finding_sections"][f.id]
    rows: list[tuple[str, Any]] = []
    if f.scope is Scope.SET:
        extra = f", затронуты {', '.join(f.requirement_ids)}" if f.requirement_ids else ""
        rows.append(("Уровень", f"весь набор требований{extra}"))
    else:
        rows.append(("Требования", ", ".join(f.requirement_ids) or "не определены"))
    rows += [(f"Цитата {i}", _quote_value(q, section_of(q, sections))) for i, q in enumerate(f.quotes, 1)]
    rows.append(("Нарушено", "; ".join(f.property.refs)))
    rows.append(
        (
            "Тип дефекта",
            f"{f.defect_type.label} · мешает написать тест-кейс: {'да' if f.blocks_testing else 'нет'}",
        )
    )
    if f.verdict.value == "unclear":
        rows.append(("Вердикт «неясно»", "в число нарушений не входит, это повод уточнить у автора"))
    if f.title:
        rows.append(("Суть", plain(f.title)))
    rows.append(("Почему это проблема", plain(f.explanation)))
    if f.suggestion:
        rows.append(("Предложенная формулировка", plain(f.suggestion)))
    if ctx["questions_by_finding"].get(f.id):
        rows.append(("Вопрос автору", ", ".join(ctx["questions_by_finding"][f.id])))
    found = f"в {f.found_in} из {f.of} проходов"
    if f.sources:
        found += f" ({', '.join(s.label for s in f.sources)})"
    rows.append(("Найдено", found))
    if f.anchor_corrected:
        rows.append(("Примечание", "место цитаты уточнил код (модель указала другой блок)"))
    rows += [("Примечание", plain(n)) for n in f.notes]
    head = f"{f.id} · {f.severity.label} · {f.verdict.label} · {f.property.label}"
    return _card(f.severity.value, head, rows)


def _option_table(options: Sequence[QuestionOption]) -> Json:
    rows = [
        [
            o.label,
            plain(o.text) + (" (вариант для обсуждения)" if o.grounded_in is None else ""),
            plain(o.change) or "—",
        ]
        for o in options
    ]
    rows.append(["Другое", "___", ""])
    return _table(["Вариант", "Правило", "Что поменять"], rows, [0.12, 0.5, 0.38], mono=[0])


def _question_card(q: Question, ctx: dict[str, Any]) -> Json:
    who = ", ".join(a.label for a in q.addressee)
    head = " · ".join(part for part in (q.id, q.priority.value, who, plain(q.topic)) if part)
    rows: list[tuple[str, Any]] = [("Вопрос", plain(q.question))]
    rows += [("Где в документе", _quote_value(qq, ctx["section_of"](qq), 200)) for qq in q.quotes]
    if q.why:
        rows.append(("Почему важно", plain(q.why)))
    if q.options:
        rows.append(("Варианты", {"table": _option_table(q.options)}))
    if q.if_unanswered:
        rows.append(("Если не ответить", plain(q.if_unanswered)))
    return _card(q.priority.value, head, rows)


# ---------------------------------------------------------------- первая страница


def _first_page(report: Report, ctx: dict[str, Any]) -> Json:
    c = report.conclusion
    v = report.verification
    stats: list[list[str]] = []
    fix: list[Json] = []
    ask: list[Json] = []
    if v is not None:
        s = v.summary
        stats = [
            ["Требований", str(s.requirements_total)],
            ["Нарушений", str(s.violations)],
            ["Критичных", str(s.critical)],
            ["«Неясно»", str(s.unclear)],
            ["Вопросов автору", str(len(report.questions))],
        ]
        findings = ctx["findings_by_id"]
        for fid in c.fix_first:
            f = findings[fid]
            first = next((q for q in f.quotes if q.verified), None)
            fix.append(
                {
                    "id": f.id,
                    "tone": f.severity.value,
                    "head": f"{f.severity.label} · {f.property.label}",
                    "text": plain(f.title or f.explanation, 220),
                    "quote": quote_plain(first, 160) if first else "",
                    "place": where(first.anchor, ctx["section_of"](first, ctx["finding_sections"][f.id]))
                    if first
                    else "",
                    "suggestion": plain(f.suggestion, 220) if f.suggestion else "",
                    "questions": ", ".join(ctx["questions_by_finding"].get(f.id, [])),
                }
            )
        for qid in c.ask_first:
            q = ctx["questions_by_id"][qid]
            ask.append(
                {
                    "id": q.id,
                    "tone": q.priority.value,
                    "head": f"{q.priority.value} ({q.priority.label})"
                    + (f" · кому: {', '.join(a.label for a in q.addressee)}" if q.addressee else ""),
                    "text": plain(q.question, 260),
                    "options": [f"{o.label}. {plain(o.text, 200)}" for o in q.options],
                }
            )
    return {
        "light": c.traffic_light.value,
        "light_label": c.traffic_light.label,
        "headline": c.headline,
        "performed": c.performed,
        "stats": stats,
        "fix": fix,
        "fix_empty": "Нарушений, которые нужно исправить до начала работ, не найдено.",
        "ask": ask,
        "ask_empty": "Вопросов, блокирующих разработку или тестирование, нет.",
    }


# ---------------------------------------------------------------- разделы


def _passport_section(report: Report, ctx: dict[str, Any]) -> Json:
    p = report.passport
    doc = f"{p.document_name} ({p.document_kind.value}"
    if p.document_size_bytes:
        doc += f", {p.document_size_bytes} байт"
    if p.document_pages:
        doc += f", {p.document_pages} стр."
    doc += ")"
    version = f"{p.agent_version} (источник: {p.agent_version_source})"
    if p.agent_dirty:
        version += ", есть незакоммиченные правки"
    if p.calls_total == 0:
        backend = f"модель не вызывалась (запрошено: {p.backend_requested.label})"
    else:
        backend = (p.backend_used or p.backend_requested).label
    t = p.tokens
    rows = [
        ("Документ", doc),
        ("SHA-256 документа", p.document_sha256 or "—"),
        ("Дата прогона", when(p.started_at)),
        ("Длительность", f"{p.duration_s if p.duration_s is not None else '—'} с"),
        ("Версия агента", version),
        ("Хеш промптов", p.prompts_sha256[:12]),
        ("Способ вызова модели", backend),
        ("Модели по шагам", "; ".join(f"{k}: {v}" for k, v in p.models.items()) or "—"),
        ("Фактические модели", ", ".join(p.models_actual) or "—"),
        ("Профиль", f"{p.profile.value}, проходов поиска: {p.passes}"),
        ("Вызовов модели", f"{p.calls_total} (с ошибкой: {p.calls_failed})"),
        (
            "Токены",
            f"вход {t.input_tokens}, выход {t.output_tokens}, запись в кеш {t.cache_creation_input_tokens}, "
            f"чтение из кеша {t.cache_read_input_tokens}",
        ),
        ("Цена по тарифу API", money(p.cost_usd)),
        ("Окружение", f"{p.os}, Python {p.python}"),
        ("Код завершения", f"{p.exit_code.value}: {p.exit_code.meaning}"),
        ("Состояние проверки", status_label(report.status)),
    ]
    return {"title": SECTION_TITLES["passport"], "blocks": [_kv(rows)]}


def _input_section(report: Report, ctx: dict[str, Any]) -> Json:
    blocks: list[Json] = []
    d = report.document
    if d is not None:
        size = d.size_bytes if d.size_bytes is not None else "размер неизвестен"
        rows = [
            ("Файл", f"{d.name} ({size} байт, SHA-256: {d.sha256 or '—'})"),
            ("Тип по содержимому", d.kind.value),
        ]
        if d.pages:
            rows.append(("Страниц", str(d.pages)))
        if d.extraction:
            rows.append(("Способ извлечения текста", plain(d.extraction)))
        rows.append(("Символов извлечено, блоков", f"{d.chars}, {d.blocks}"))
        rows.append(("Статус входа", ctx["input_status"]))
        blocks.append(_kv(rows))
    if report.input_diagnostics:
        blocks.append(_diag_table(report.input_diagnostics))
    else:
        blocks.append(_para("Вход прочитан полностью, замечаний нет."))
    return {"title": SECTION_TITLES["input"], "blocks": blocks}


def _unperformed_section(report: Report) -> Json:
    text = "Разделы о требованиях не сформированы: документ не удалось прочитать. "
    text += "Исправьте вход по таблице выше и запустите проверку снова."
    return {"title": "Итог", "blocks": [_para(report.conclusion.headline), _para(text)]}


def _segmentation_section(report: Report) -> Json:
    seg = report.segmentation
    if seg is None:
        return {
            "title": SECTION_TITLES["segmentation"],
            "blocks": [_para("Нарезка не выполнена, смотрите диагностику прогона.")],
        }
    if seg.mode.value == "fallback":
        how = "Нарезка упрощённая, без модели: каждый содержательный блок документа стал требованием. "
        how += "Цитату в таблицу подставил код."
    else:
        how = "Нарезку сделала модель по ID блоков, цитату в таблицу подставил код из текста документа."

    def source(req: Requirement) -> str:
        return req.source_id or f"нет номера ({plain(req.source_locator)})"

    rows = [
        [
            r.id,
            source(r),
            plain(r.summary, 160),
            r.type.label,
            f"«{plain(r.quote, 300)}»",
            where(r.anchor, r.section_path),
        ]
        for r in seg.requirements
    ] or [["—", "—", "требований в документе не найдено", "—", "—", "—"]]
    excluded = ""
    if seg.excluded:
        counts: dict[str, int] = {}
        for e in seg.excluded:
            counts[e.label.label] = counts.get(e.label.label, 0) + 1
        excluded = " (" + ", ".join(f"{k}: {n}" for k, n in counts.items()) + ")"
    tail = (
        f"Требований: {len(seg.requirements)}. Не вошли в требования: {len(seg.excluded)} блоков{excluded}."
    )
    table = _table(
        ["ID в отчёте", "ID в документе", "Краткая формулировка", "Тип", "Цитата", "Где"],
        rows,
        [0.07, 0.11, 0.19, 0.13, 0.3, 0.2],
        mono=[0],
        hyph=[1, 2, 3, 5],
    )
    return {"title": SECTION_TITLES["segmentation"], "blocks": [_para(how), table, _para(tail)]}


def _requirements_section(report: Report, ctx: dict[str, Any]) -> Json:
    title = SECTION_TITLES["requirements"]
    if report.verification is None or report.segmentation is None:
        return {
            "title": title,
            "blocks": [_para("Проверка требований не выполнена, смотрите диагностику прогона.")],
        }
    rows = ctx["requirement_rows"]
    clean = sum(1 for r in rows if not r.finding_ids)
    intro = (
        "Статус есть у каждого требования из таблицы нарезки. Подробности по каждой находке ниже. "
        f"Требований без нарушений: {clean} из {len(rows)}."
    )
    table = _table(
        ["Требование", "ID в документе", "Статус"],
        ([r.requirement.id, r.requirement.source_id or "нет номера", r.status] for r in rows),
        [0.14, 0.2, 0.66],
        mono=[0, 1],
    )
    blocks: list[Json] = [_para(intro), table]
    cards = [_finding_card(f, ctx) for f in ctx["req_findings"]]
    blocks += cards or [_para("Нарушений в отдельных требованиях не найдено.")]
    return {"title": title, "blocks": blocks}


def _set_section(report: Report, ctx: dict[str, Any]) -> Json:
    title = SECTION_TITLES["set"]
    if report.verification is None:
        return {
            "title": title,
            "blocks": [_para("Проверка набора не выполнена, смотрите диагностику прогона.")],
        }
    cards = [_finding_card(f, ctx) for f in ctx["set_findings"]]
    return {
        "title": title,
        "blocks": cards or [_para("Нарушений уровня набора (29148 п. 5.2.6, IEEE 830 п. 4.3) не найдено.")],
    }


def _questions_section(report: Report, ctx: dict[str, Any]) -> Json:
    title = SECTION_TITLES["questions"]
    v = report.verification
    if report.questions:
        table = _table(
            ["ID", "Приоритет", "Адресат", "Вопрос", "Закрывает находки"],
            (
                [
                    q.id,
                    f"{q.priority.value} {q.priority.label}",
                    ", ".join(a.label for a in q.addressee),
                    plain(q.question, 200),
                    ", ".join(q.related_findings) or "—",
                ]
                for q in report.questions
            ),
            [0.08, 0.16, 0.14, 0.42, 0.2],
            mono=[0],
        )
        blocks = [table, *(_question_card(q, ctx) for q in report.questions)]
        blocks.append(_para("Ответы можно вписать в questions.yaml рядом с отчётом."))
        return {"title": title, "blocks": blocks}
    failed = [d for d in report.diagnostics if d.step == "questions" and d.level.value != "info"]
    if failed:
        text = "Вопросы не сформированы: шаг вопросов завершился с ошибкой, смотрите диагностику прогона."
    elif v is not None and v.findings:
        text = (
            "Вопросов нет: агент не нашёл находок, исправление которых требует выбора автора "
            "между вариантами."
        )
    elif v is not None:
        text = "Вопросов нет: нарушений и неясностей не найдено."
    else:
        text = "Вопросы не сформированы: проверка выполнена не полностью, смотрите диагностику прогона."
    return {"title": title, "blocks": [_para(text)]}


def _summary_section(report: Report, ctx: dict[str, Any]) -> Json:
    title = SECTION_TITLES["summary"]
    v = report.verification
    if v is None:
        return {"title": title, "blocks": [_para("Сводка недоступна: проверка выполнена не полностью.")]}
    s = v.summary
    by_sev = ", ".join(f"{k.label} {n}" for k, n in s.by_severity.items()) or "нет находок"
    critical = f"{s.critical}" + (f" ({', '.join(s.critical_ids)})" if s.critical_ids else "")
    classes = ", ".join(f"{f.id} {f.defect_type.label}" for f in ctx["critical_classes"]) or "не найдены"
    discarded = str(s.discarded_total)
    if s.discarded_by_reason:
        discarded += " (" + "; ".join(f"{k.label}: {n}" for k, n in s.discarded_by_reason.items()) + ")"
    items = [
        f"Требований: {s.requirements_total}, из них с нарушениями или неясностями: "
        f"{s.requirements_with_findings}.",
        f"Находок: {s.findings_total} (нарушений {s.violations}, «неясно» {s.unclear}, "
        f"уровня набора {s.set_level}).",
        f"По важности: {by_sev}.",
        f"Критичных: {critical}.",
        f"Критичные классы по ТЗ (непроверяемые, противоречивые, неполные): {classes}.",
        f"Вопросов автору: {len(report.questions)}.",
        f"Отброшено кодом: {discarded}.",
        f"Цитаты: сверено {s.quotes_exact + s.quotes_fuzzy} из {s.quotes_total} "
        f"(точно {s.quotes_exact}, нечётко {s.quotes_fuzzy}).",
    ]
    return {"title": title, "blocks": [_bullets(items)]}


def _conclusion_section(report: Report, ctx: dict[str, Any]) -> Json:
    c = report.conclusion
    v = report.verification
    items: list[str] = []
    if ctx["blocks_development"]:
        items.append(
            f"Открытые вопросы, блокирующие разработку (P0): {', '.join(ctx['blocks_development'])}."
        )
    if ctx["blocks_testing"]:
        items.append(f"Открытые вопросы, блокирующие тестирование (P1): {', '.join(ctx['blocks_testing'])}.")
    for i, fid in enumerate(c.fix_first, 1):
        f = ctx["findings_by_id"][fid]
        first = next((q for q in f.quotes if q.verified), None)
        place = ""
        if first is not None:
            place = f" ({where(first.anchor, ctx['section_of'](first, ctx['finding_sections'][f.id]))})"
        items.append(f"Исправить {i}-м: {f.id} · {f.severity.label} · {f.property.label}{place}")
    if c.ask_first:
        items.append(f"Спросить автора в первую очередь: {', '.join(c.ask_first)}.")
    rule = (
        f"Правило светофора: {plain(v.traffic_rule)}"
        if v is not None and v.traffic_rule
        else "Светофор и важность считает код по сверенным находкам, а не модель."
    )
    blocks: list[Json] = [{"t": "verdict", "light": c.traffic_light.value, "text": c.headline}]
    if items:
        blocks.append(_bullets(items))
    blocks.append(_note(rule))
    return {"title": SECTION_TITLES["conclusion"], "blocks": blocks}


def _metrics_section(report: Report) -> Json:
    m = report.metrics
    t = m.tokens
    rows = [
        ("Доля сверенных цитат", f"{pct(m.verified_quote_share)} ({m.quotes_verified} из {m.quotes_total})"),
        ("Кандидатов от проходов и линтера", str(m.candidates_total)),
        ("Находок после слияния", str(m.merged_total)),
        ("Находок после верификатора", str(m.findings_total)),
        ("Отброшено верификатором", str(m.discarded_total)),
        (
            "Вызовов модели",
            f"{m.calls_total} (с ошибкой {m.calls_failed}, из кеша {m.calls_cached})",
        ),
        (
            "Токены",
            f"вход {t.input_tokens}, выход {t.output_tokens}, запись в кеш {t.cache_creation_input_tokens}, "
            f"чтение из кеша {t.cache_read_input_tokens}",
        ),
        ("Время прогона", f"{m.duration_s if m.duration_s is not None else '—'} с"),
        ("Цена по тарифу API", money(m.cost_usd)),
    ]
    return {"title": SECTION_TITLES["metrics"], "blocks": [_kv(rows)]}


def _diagnostics_section(report: Report) -> Json:
    title = SECTION_TITLES["diagnostics"] if report.conclusion.performed else "Диагностика прогона"
    block = _diag_table(report.diagnostics) if report.diagnostics else _para("Замечаний к прогону нет.")
    return {"title": title, "blocks": [block]}


def build_view(report: Report) -> Json:
    """Плоская модель вида для ``report.typ``: порядок и состав разделов как в Markdown."""
    ctx = report_context(report)
    p = report.passport
    sections = [_passport_section(report, ctx), _input_section(report, ctx)]
    if not report.conclusion.performed:
        sections.append(_unperformed_section(report))
    else:
        sections += [
            _segmentation_section(report),
            _requirements_section(report, ctx),
            _set_section(report, ctx),
            _questions_section(report, ctx),
            _summary_section(report, ctx),
            _conclusion_section(report, ctx),
            _metrics_section(report),
        ]
    sections.append(_diagnostics_section(report))
    return {
        "title": TITLE,
        "document": p.document_name,
        "date": when(p.started_at),
        "version": p.agent_version,
        "intro": (
            f"{STANDARDS} Отчёт сформировал агент QAIRU validator {p.agent_version}, {when(p.started_at)}."
        ),
        "first_title": FIRST_PAGE_TITLE,
        "first": _first_page(report, ctx),
        "sections": sections,
    }


# ---------------------------------------------------------------- сборка PDF


def render_pdf(report: Report) -> bytes:
    """PDF отчёта. Одинаков на любой ОС: шрифты из репозитория, дата создания из паспорта прогона."""
    try:
        view = json.dumps(build_view(report), ensure_ascii=False)
        data = typst.compile(
            input=str(TEMPLATE_PATH),
            font_paths=[str(FONTS_DIR)],
            ignore_system_fonts=True,
            sys_inputs={"report": view},
            timestamp=report.passport.started_at.replace(microsecond=0),
        )
    except Exception as exc:  # typst кидает свои SyntaxError/RuntimeError, вид строится кодом: наружу одно
        raise PdfRenderError(
            f"Typst не собрал PDF: {type(exc).__name__}",
            hint="Откройте отчёт в Markdown; подробности в журнале прогона (.log)",
        ) from exc
    if not isinstance(data, bytes) or not data.startswith(b"%PDF"):
        raise PdfRenderError("Typst вернул не PDF", hint="Откройте отчёт в Markdown")
    return data
