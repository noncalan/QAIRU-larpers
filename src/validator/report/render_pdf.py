"""Шаг 9в: PDF-версия отчёта через Typst из того же ``Report``, что и Markdown.

PDF не получается из Markdown (pandoc и LaTeX у жюри не стоят). Здесь тот же ``Report`` и тот же
контекст (``render_md.report_context``) превращаются в плоскую модель вида: готовые строки, без
разметки и без вычислений, а ``report.typ`` только раскладывает её на страницы. Данные уходят в Typst
как JSON (``sys.inputs``), поэтому знаки ``#``, ``$``, ``*``, ``_`` в цитатах документа ничего не
значат для вёрстки и не требуют экранирования.

Шрифты с кириллицей лежат в ``fonts/`` рядом (Liberation, лицензия OFL; запасной DejaVu Sans для знаков
вроде ₸ и ₽, которых нет в Liberation) и передаются в Typst явно, системные шрифты отключены: PDF выглядит
одинаково на Windows, macOS и Linux.
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
    DocumentPart,
    Finding,
    Passport,
    Question,
    QuestionMetrics,
    QuestionOption,
    Quote,
    Report,
    Requirement,
    Scope,
    SearchInput,
    SelfCheck,
)
from validator.report.clarify_view import CLARIFY_TITLE, CLOSED_NOTE, ClarifyView, clarify_view
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
    part_search_text,
)
from validator.report.first_page import FixItem
from validator.report.fmt import money, pct, plain, quote_plain, status_label, when, where
from validator.report.passport_view import (
    backend_line,
    calls_text,
    replay_line,
    reuse_cell,
    reuse_header,
    tokens_in,
    tokens_text,
)
from validator.report.render_md import PARTS_NOTE, SEGMENTATION_NOTE, report_context
from validator.report.run_metrics import step_label
from validator.report.testdraft_view import LABEL, DraftView

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
    "coverage": "11. Матрица покрытия критериев",
    "selfcheck": "12. Самопроверка отчёта",
}
"""Заголовки разделов те же, что в Markdown (тест сверяет их с ``## `` из ``render_md``)."""

Json = dict[str, Any]

CRITICAL_LINE = 3
"""Номер строки «Критичных: N» в списке сводки: под ней вложенный список критичных нарушений."""


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


def _bullets(items: Iterable[str], indent: int = 0) -> Json:
    return {"t": "bullets", "items": list(items), "indent": indent}


def _note(text: str) -> Json:
    return {"t": "note", "text": text}


def _card(tone: str, head: str, rows: Iterable[tuple[str, Any]], anchor: str = "") -> Json:
    """Карточка находки или вопроса; ``anchor`` (F-NN, Q-NN) делает её целью ссылок из всего отчёта."""
    return {"t": "card", "tone": tone, "head": head, "rows": [[k, v] for k, v in rows], "anchor": anchor}


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
    for e in ctx["evidence_lines"].get(f.id, []):
        rows.append((e.head, e.text))
        rows += [(f"{e.item_label} {i}", item) for i, item in enumerate(e.items, 1)]
    if ctx["questions_by_finding"].get(f.id):
        rows.append(("Вопрос автору", ", ".join(ctx["questions_by_finding"][f.id])))
    rows += _draft_rows(ctx["test_drafts"].get(f.id))
    how = ctx["found"](f)
    if how:
        rows.append(("Найдено", how))
    rows += [("Примечание", plain(n)) for n in f.notes]
    head = f"{f.id} · {f.severity.label} · {f.verdict.label} · {f.property.label}"
    return _card(f.severity.value, head, rows, f.id)


def _draft_rows(td: DraftView | None) -> list[tuple[str, Any]]:
    """Блок «Попытка теста» строками карточки: те же строки, что в Markdown (``testdraft_view``)."""
    if td is None:
        return []
    rows: list[tuple[str, Any]] = [(LABEL, td.status)]
    rows += list(td.parts)
    rows += [("Пропуск", line) for line in td.gaps]
    rows += [("Из документа", line) for line in td.values]
    if td.note:
        rows.append(("Примечание модели", td.note))
    return rows


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
    return _card(q.priority.value, head, rows, q.id)


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
        fix = [_fix_item(item) for item in ctx["fix_items"]]
        for qid in c.ask_first:
            q = ctx["questions_by_id"].get(qid)
            if q is None:
                ask.append(
                    {
                        "id": qid,
                        "tone": "P2",
                        "head": "",
                        "text": "вопроса с таким номером нет в списке вопросов: ошибка сборки отчёта, "
                        "подробности в разделе «Самопроверка отчёта».",
                        "options": [],
                    }
                )
                continue
            head = f"{q.priority.value} ({q.priority.label})"
            head += f" · кому: {', '.join(a.label for a in q.addressee)}" if q.addressee else ""
            head += f" · к {', '.join(q.related_findings)}" if q.related_findings else ""
            ask.append(
                {
                    "id": q.id,
                    "tone": q.priority.value,
                    "head": head,
                    "text": plain(q.question, 260),
                    "options": [f"{o.label}. {plain(o.text, 200)}" for o in q.options],
                }
            )
    blocking = len(ctx["blocks_development"])
    return {
        "light": c.traffic_light.value,
        "light_label": c.traffic_light.label,
        "headline": c.headline,
        "performed": c.performed,
        "stats": stats,
        "blocking": f"Вопросов автору, которые блокируют разработку: {blocking}." if blocking else "",
        "fix": fix,
        "fix_intro": ctx["fix_intro"],
        "fix_empty": "Нарушений, которые нужно исправить до начала работ, не найдено.",
        "ask": ask,
        "ask_empty": "Вопросов, блокирующих разработку или тестирование, нет.",
        "clarify": _first_page_clarify(ctx["clarify"]),
        "review": ctx["review_line"] or "",
    }


def _fix_item(item: FixItem) -> Json:
    return {
        "id": item.id,
        "tone": item.tone,
        "head": f"{item.severity} · {item.kind}",
        "text": item.title,
        "quote": quote_plain(item.quote, 160) if item.quote else "",
        "place": item.place + (f"; требования: {item.requirements}" if item.requirements else ""),
        "questions": item.questions,
    }


def _first_page_clarify(view: ClarifyView | None) -> str:
    if view is None:
        return ""
    before = f" Светофор до ответов: {view.compare[0][1]}." if view.compare else ""
    return f"{view.summary_line}{before} Подробности в разделе 6."


# ---------------------------------------------------------------- разделы


def _search_input(p: Passport) -> str:
    """Хвост строки «Профиль»: вход поиска виден, только если он не по умолчанию (как в Markdown)."""
    return ", поиск по тексту без ID блоков" if p.search_input is SearchInput.RAW else ""


def _passport_section(report: Report, ctx: dict[str, Any]) -> Json:
    p = report.passport
    rows = [
        ("Документ", ctx["passport_document"]),
        ("SHA-256 документа", p.document_sha256 or "—"),
        ("Дата прогона", when(p.started_at)),
        ("Длительность", f"{p.duration_s if p.duration_s is not None else '—'} с"),
        ("Версия агента", ctx["passport_version"]),
        ("Хеш промптов", p.prompts_sha256[:12]),
        ("Способ вызова модели", backend_line(report)),
        *([("Запись ответов модели", replay)] if (replay := replay_line(report)) else []),
        ("Модели по шагам", ctx["passport_models"]),
        ("Фактические модели", ", ".join(p.models_actual) or "—"),
        ("Профиль", f"{p.profile.value}, проходов поиска: {p.passes}{_search_input(p)}"),
        ("Доменные чек-листы", plain(ctx["checklists_passport"])),
        ("Вызовов модели", f"{p.calls_total} (с ошибкой: {p.calls_failed})"),
        ("Токены", tokens_text(p.tokens)),
        ("Цена по тарифу API", money(p.cost_usd)),
        ("Окружение", f"{p.os}, Python {p.python}"),
        ("Код завершения", f"{p.exit_code.value}: {p.exit_code.meaning}"),
        ("Состояние проверки", status_label(report.status)),
    ]
    if report.selfcheck is not None:
        rows.append(("Самопроверка отчёта", plain(report.selfcheck.summary)))
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


def _unperformed_section(report: Report, ctx: dict[str, Any]) -> Json:
    text = "Разделы о требованиях не сформированы: документ не удалось прочитать. "
    text += "Исправьте вход по таблице выше и запустите проверку снова."
    blocks = [_para(report.conclusion.headline), _para(text)]
    if ctx["next_steps"]:
        blocks += [_para("Что сделать:"), _bullets(plain(step) for step in ctx["next_steps"])]
    return {"title": "Итог", "blocks": blocks}


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
        how = SEGMENTATION_NOTE

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
    return {
        "title": SECTION_TITLES["segmentation"],
        "blocks": [_para(how), *_parts(report), table, _para(tail)],
    }


def _parts(report: Report) -> list[Json]:
    """Части длинного документа (#38): как в Markdown, пояснение и таблица."""
    parts = report.document.parts if report.document else []
    if not parts:
        return []

    def blocks(part: DocumentPart) -> str:
        extra = f", плюс {part.context_blocks} из раздела терминов для справки" if part.context_blocks else ""
        return f"{part.first_block}–{part.last_block} ({part.blocks}{extra})"

    total = report.metrics.passes_total
    rows = [
        [
            str(p.index),
            plain("; ".join(p.sections), 300),
            blocks(p),
            p.pages or "—",
            str(p.tokens),
            part_search_text(p, total),
        ]
        for p in parts
    ]
    table = _table(
        ["Часть", "Разделы", "Блоки", "Страницы", "Токенов, оценка", "Проходов поиска"],
        rows,
        [0.07, 0.33, 0.22, 0.11, 0.12, 0.15],
        mono=[0],
        hyph=[1, 2, 5],
    )
    return [_para(PARTS_NOTE), table]


def _requirements_section(report: Report, ctx: dict[str, Any]) -> Json:
    title = SECTION_TITLES["requirements"]
    if report.verification is None or report.segmentation is None:
        return {
            "title": title,
            "blocks": [_para("Проверка требований не выполнена, смотрите диагностику прогона.")],
        }
    rows = ctx["requirement_rows"]
    intro = ctx["requirements_intro"]
    table = _table(
        ["Требование", "ID в документе", "Статус"],
        ([r.requirement.id, r.requirement.source_id or "нет номера", r.status] for r in rows),
        [0.14, 0.2, 0.66],
        mono=[0, 1],
    )
    blocks: list[Json] = [_para(intro), table]
    cards = [_finding_card(f, ctx) for f in ctx["req_findings"]] or [_para(ctx["empty_req"])]
    blocks += cards + _minor_blocks(ctx["req_minor"], ctx["minor_note_req"], ctx)
    return {"title": title, "blocks": blocks}


def _minor_blocks(items: Sequence[Finding], note: str, ctx: dict[str, Any]) -> list[Json]:
    """Блок «Незначительные замечания (по желанию)» в конце раздела 4 или 5, как в Markdown."""
    if not items:
        return []
    head = [
        {"t": "h2", "text": ctx["minor_title"]},
        _note(f"{note} Находок в блоке: {len(items)}."),
    ]
    return head + [_finding_card(f, ctx) for f in items]


def _set_section(report: Report, ctx: dict[str, Any]) -> Json:
    title = SECTION_TITLES["set"]
    if report.verification is None:
        return {
            "title": title,
            "blocks": [_para("Проверка набора не выполнена, смотрите диагностику прогона.")],
        }
    cards = [_finding_card(f, ctx) for f in ctx["set_findings"]] or [_para(ctx["empty_set"])]
    blocks = cards + _minor_blocks(ctx["set_minor"], ctx["minor_note_set"], ctx)
    return {"title": title, "blocks": blocks + _checklist_blocks(report, ctx)}


def _checklist_blocks(report: Report, ctx: dict[str, Any]) -> list[Json]:
    """Доменные чек-листы (#35): включённые и нет, почему, находки с пунктом чек-листа. Как в Markdown."""
    if not report.checklists:
        return []
    enabled = report.passport.checklists_enabled
    note = ctx["checklists_note"] + (
        "" if enabled else " В этом прогоне чек-листы выключены (--no-checklists)."
    )
    rows = [
        [
            f"{u.title} ({u.id})",
            ctx["checklist_state"](u, enabled),
            plain(ctx["checklist_reason"](u)),
            str(u.items),
            ", ".join(u.findings) or "—",
        ]
        for u in report.checklists
    ]
    head = ["Чек-лист", "Статус", "Почему", "Пунктов", "Находки с пунктом чек-листа"]
    return [
        {"t": "h2", "text": "Доменные чек-листы"},
        _para(note),
        _table(head, rows, [0.2, 0.14, 0.38, 0.08, 0.2], mono=(4,), hyph=(1,)),
    ]


def _withdrawn_blocks(report: Report, ctx: dict[str, Any]) -> list[Json]:
    """Вопросы, снятые самопроверкой: ответ нашёлся в документе, цитату-ответ сверил код."""
    if not report.questions_withdrawn:
        return []
    items = [
        f"{w.question.id}. {plain(w.question.question, 200)} Ответ: {plain(w.reason, 200)}. "
        f"Документ: «{quote_plain(w.answer, 200)}» ({where(w.answer.anchor, ctx['section_of'](w.answer))})."
        for w in report.questions_withdrawn
    ]
    intro = (
        "Перед отчётом агент попытался сам ответить на каждый вопрос по документу. Эти вопросы сняты: "
        "ответ нашёлся, и код сверил цитату-ответ с документом."
    )
    return [{"t": "h2", "text": "Снятые вопросы: ответ есть в документе"}, _para(intro), _bullets(items)]


def _clarify_blocks(view: ClarifyView | None, has_questions: bool) -> list[Json]:
    """Раздел «Уточнения» в начале раздела вопросов: те же строки, что в Markdown (``clarify_view``)."""
    if view is None:
        return []
    blocks: list[Json] = [{"t": "h2", "text": CLARIFY_TITLE}, _para(view.intro)]
    blocks += [_note(w) for w in view.warnings]
    if view.compare:
        blocks.append(_table(["", "До ответов", "После ответов"], view.compare, [0.46, 0.27, 0.27]))
    blocks.append(_para("Ответы автора"))
    blocks.append(
        _table(["Вопрос", "Ответ", "Что изменилось"], view.answers, [0.1, 0.55, 0.35], mono=[0], hyph=[2])
    )
    if view.reviewed:
        blocks.append(_para(f"Закрыто ответами. {CLOSED_NOTE}"))
        if view.closed:
            blocks.append(
                _table(
                    ["Находка", "Ответ", "Почему закрыто", "Правка текста", "Место в документе"],
                    view.closed,
                    [0.17, 0.09, 0.26, 0.26, 0.22],
                    hyph=[0, 1, 4],
                )
            )
        else:
            blocks.append(_para("Ответы не закрыли ни одной находки."))
    if view.refined:
        blocks.append(_para("Уточнено ответами. Находки остались в разделах 4 и 5 с примечанием."))
        blocks.append(
            _table(
                ["Находка", "Ответ", "Что уточнено", "Что осталось открытым"],
                view.refined,
                [0.1, 0.1, 0.4, 0.4],
                mono=[0],
                hyph=[1],
            )
        )
    if view.new:
        blocks.append(
            _para("Новые находки из-за ответов. Цитаты сверены с документом, карточки в разделах 4 и 5.")
        )
        blocks.append(
            _table(
                ["Находка", "Ответ", "Суть", "Как исправить"], view.new, [0.2, 0.1, 0.35, 0.35], hyph=[0, 1]
            )
        )
    blocks.append(_para(f"Какие вопросы остались открытыми: {view.open_questions}."))
    if has_questions:
        blocks.append({"t": "h2", "text": "Открытые вопросы"})
    return blocks


def _questions_section(report: Report, ctx: dict[str, Any]) -> Json:
    section = _questions_body(report, ctx)
    clarify = _clarify_blocks(ctx["clarify"], bool(report.questions))
    return {"title": section["title"], "blocks": [*clarify, *section["blocks"]]}


def _questions_body(report: Report, ctx: dict[str, Any]) -> Json:
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
        blocks.append(_para(f"Ответы можно вписать в {report.questions_file} рядом с отчётом."))
        return {"title": title, "blocks": [*blocks, *_withdrawn_blocks(report, ctx)]}
    failed = [d for d in report.diagnostics if d.step == "questions" and d.level.value != "info"]
    if report.questions_withdrawn:
        text = (
            "Открытых вопросов нет: самопроверка нашла в документе ответы на все вопросы, "
            "они перечислены ниже."
        )
    elif failed:
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
    return {"title": title, "blocks": [_para(text), *_withdrawn_blocks(report, ctx)]}


def _summary_section(report: Report, ctx: dict[str, Any]) -> Json:
    title = SECTION_TITLES["summary"]
    v = report.verification
    if v is None:
        return {"title": title, "blocks": [_para("Сводка недоступна: проверка выполнена не полностью.")]}
    s = v.summary
    by_sev = ", ".join(f"{k.label} {n}" for k, n in s.by_severity.items()) or "нет находок"
    classes = ", ".join(f"{f.id} {f.defect_type.label}" for f in ctx["tz_classes"])
    classes = f"{len(ctx['tz_classes'])}" + (f" ({classes})" if classes else "")
    discarded = str(s.discarded_total)
    if s.discarded_by_reason:
        discarded += " (" + "; ".join(f"{k.label}: {n}" for k, n in s.discarded_by_reason.items()) + ")"
    items = [  # строка «Критичных» стоит под номером CRITICAL_LINE: под ней вложенный список
        f"Требований: {s.requirements_total}, из них с нарушениями или неясностями: "
        f"{s.requirements_with_findings}.",
        f"Находок: {s.findings_total} (нарушений {s.violations}, «неясно» {s.unclear}, "
        f"уровня набора {s.set_level}).",
        f"По важности: {by_sev}.",
        f"Критичных: {s.critical}" + (":" if ctx["critical_items"] else "."),
        f"{ctx['tz_classes_title']}: {classes}.",
        f"Вопросов автору: {len(report.questions)}.",
        *([ctx["test_draft_line"]] if ctx["test_draft_line"] else []),
        *([f"Уточнения автора: {ctx['clarify'].summary_line}"] if ctx["clarify"] else []),
        f"Отброшено кодом: {discarded}.",
        f"Цитаты: сверено {s.quotes_exact + s.quotes_fuzzy} из {s.quotes_total} "
        f"(точно {s.quotes_exact}, нечётко {s.quotes_fuzzy}).",
    ]
    critical = [
        f"{i.id} · {i.kind} · {i.title}" + (f" · {i.place}" if i.place else "") for i in ctx["critical_items"]
    ]
    # Список критичных вложен под строкой «Критичных: N», как в Markdown.
    head, tail = items[: CRITICAL_LINE + 1], items[CRITICAL_LINE + 1 :]
    blocks: list[Json] = [_bullets(head), _bullets(critical, indent=1), _bullets(tail)]
    if ctx["critic_dropped"]:
        blocks += [
            {"t": "h2", "text": "Находки, снятые критиком"},
            _para(ctx["critic_dropped_intro"]),
            _bullets(ctx["critic_dropped"]),
        ]
    return {"title": title, "blocks": blocks}


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
    if c.fix_first:
        items.append(
            f"Что исправить первым, по порядку: {', '.join(c.fix_first)}. Суть, цитата и место каждого "
            "нарушения на первой странице, подробности в карточках разделов 4 и 5."
        )
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


def _questions_count(qm: QuestionMetrics) -> str:
    if not qm.reviewed:
        return f"{qm.total} (самопроверка не выполнена)"
    return f"{qm.total} (до самопроверки {qm.before_critic}, снято {qm.withdrawn}: ответ есть в документе)"


def _metrics_section(report: Report, ctx: dict[str, Any]) -> Json:
    m = report.metrics
    rows = [
        ("Доля сверенных цитат", f"{pct(m.verified_quote_share)} ({m.quotes_verified} из {m.quotes_total})"),
        ("Кандидатов от проходов и линтера", str(m.candidates_total)),
        ("Находок после слияния", str(m.merged_total)),
        ("Находок после верификатора", str(m.findings_total)),
        ("Отброшено верификатором", str(m.discarded_total)),
        (
            "Вызовов модели",
            calls_text(m),
        ),
        ("Токены", tokens_text(m.tokens)),
        ("Время прогона", f"{m.duration_s if m.duration_s is not None else '—'} с"),
        ("Цена по тарифу API", money(m.cost_usd)),
        *ctx["evidence_rows"],
    ]
    qm = m.questions
    if qm is not None and qm.before_critic:
        rows += [
            ("Вопросов автору", _questions_count(qm)),
            ("Вопросы: со сценарием", pct(qm.with_scenario)),
            ("Вопросы: закрытые (2–4 варианта с последствиями)", pct(qm.closed)),
            ("Вопросы: без ошибок формулировки", pct(qm.without_errors)),
            ("Вопросы: дубли с находками, повторы", f"{qm.obvious_fix}, {qm.duplicate_pairs}"),
            (
                "Вопросы: адресаты",
                ", ".join(f"{ctx['addressee_label'](a)} {n}" for a, n in qm.by_addressee.items()) or "—",
            ),
        ]
    blocks: list[Json] = [_kv(rows), *_step_blocks(report), *_funnel_blocks(report)]
    return {"title": SECTION_TITLES["metrics"], "blocks": blocks}


def _step_blocks(report: Report) -> list[Json]:
    """Вызовы, время и цена по шагам: та же таблица, что в Markdown."""
    m = report.metrics
    if not m.steps:
        return []
    rows = [
        [
            step_label(st.step),
            plain(report.passport.models.get(st.step, "—")),
            str(st.calls),
            str(st.failed),
            reuse_cell(st, m.steps),
            str(st.duration_s),
            f"{tokens_in(st.tokens)} / {st.tokens.output_tokens}",
            money(st.cost_usd),
        ]
        for st in m.steps
    ]
    table = _table(
        [
            "Шаг",
            "Модель",
            "Вызовов",
            "С ошибкой",
            reuse_header(m.steps),
            "Время, с",
            "Токены: вход / выход",
            "Цена",
        ],
        rows,
        [0.17, 0.2, 0.08, 0.09, 0.08, 0.1, 0.15, 0.13],
        hyph=[0, 1],
    )
    return [{"t": "h2", "text": "Вызовы модели по шагам"}, table]


def _funnel_blocks(report: Report) -> list[Json]:
    """Воронка находок и распределение «найдено в N из K проходов»."""
    m = report.metrics
    rows = [
        [
            "Кандидаты от проходов поиска",
            f"{m.candidates_passes} (проходов отработало {m.passes_ok} из {m.passes_total})",
        ],
        ["Кандидаты от правил линтера", str(m.candidates_lint)],
        *(
            [
                [
                    "Из них с пунктом доменного чек-листа",
                    f"{m.candidates_checklist} (в отчёте находок с пунктом: {m.findings_checklist})",
                ]
            ]
            if m.candidates_checklist
            else []
        ),
        ["После слияния", str(m.merged_total)],
        *([f"Отброшено: {reason.label}", str(n)] for reason, n in m.discarded_by_reason.items()),
    ]
    view = clarify_view(report)
    if view is not None:
        rows.append(["Сверка с ответами автора", view.funnel])
    rows.append(["После верификатора, в отчёте", str(m.findings_total)])
    if m.critic_reviewed:
        rows.append(
            [
                "Критик: просмотрено, понижено, снято",
                f"{m.critic_reviewed}, {m.critic_downgraded}, {m.critic_dropped}",
            ]
        )
    blocks: list[Json] = [
        {"t": "h2", "text": "Воронка находок"},
        _table(["Этап", "Находок"], rows, [0.6, 0.4]),
    ]
    if any(b.of > 1 for b in m.found_in):
        buckets = [[f"в {b.found_in} из {b.of}", str(b.count)] for b in m.found_in]
        blocks += [
            {"t": "h2", "text": "«Найдено в N из K проходов»"},
            _table(["Найдено", "Находок"], buckets, [0.6, 0.4]),
        ]
    return blocks


def _coverage_section(report: Report, ctx: dict[str, Any]) -> Json:
    """Матрица покрытия критериев: требования × свойства 5.2.5 и набор × свойства 5.2.6 (как в Markdown)."""
    title = SECTION_TITLES["coverage"]
    cov = report.coverage
    if cov is None:
        return {
            "title": title,
            "blocks": [
                _para(
                    "Матрица не построена: нет нарезки или проверки требований, смотрите диагностику прогона."
                )
            ],
        }
    intro = (
        "Матрицу строит код из находок: модель в ней не участвует. «Нарушений нет» значит, что проходы "
        "поиска получили рубрику со всеми свойствами и по этому месту ничего не вернули; это не "
        "доказательство, что дефекта нет."
    )
    blocks: list[Json] = [_para(intro)]
    if not cov.checked:
        blocks.append(_para(NOT_CHECKED_NOTE))
    if ctx["coverage_parts_note"]:
        blocks.append(_para(ctx["coverage_parts_note"]))
    if cov.checked and not cov.rows:
        blocks.append(_para(NO_REQUIREMENTS_NOTE))
    blocks.append(_para(f"Обозначения: {LEGEND}"))
    props = cov.requirement_properties
    count = len(props) + 1  # колонки свойств и «Набор»
    first, rest = 0.11, 0.89 / count
    matrix = _table(
        ["Требование", *(SHORT_LABELS[p] for p in props), SET_COLUMN],
        (
            [row.requirement_id, *(cell_text(row.cells[p]) for p in props), cell_text(row.set_cell)]
            for row in cov.rows
        ),
        [first, *([rest] * count)],
        mono=[0],
        hyph=list(range(1, count + 1)),
    )
    totals = ctx["coverage_totals"]
    tail = (
        f"Ячеек: {sum(totals.values())}, нарушений нет в {totals['clean']}, "
        f"нарушение в {totals['violation']}, «неясно» в {totals['unclear']}, "
        f"не проверялось в {totals['not_checked']}."
    )
    legend = _table(
        ["Колонка", "Свойство", "Пункты стандартов"],
        [
            *([SHORT_LABELS[p], p.label, "; ".join(p.refs)] for p in props),
            [SET_COLUMN, SET_COLUMN_LABEL, "; ".join(SET_COLUMN_REFS)],
        ],
        [0.14, 0.26, 0.6],
    )
    blocks += [
        {"t": "h2", "text": "Требования × свойства: 29148 п. 5.2.5 и IEEE 830 п. 4.3"},
        matrix,
        _para(tail),
    ]
    if cov.unlinked_finding_ids:
        blocks.append(
            _para(
                "Находки, которые не привязаны ни к одному требованию (цитата вне таблицы нарезки), "
                "поэтому в ячейках требований их нет: " + ", ".join(cov.unlinked_finding_ids) + "."
            )
        )
    if cov.extra_finding_ids:
        blocks.append(
            _para(
                "Находки, у которых нет ячейки в сетке (свойство другого уровня): "
                + ", ".join(cov.extra_finding_ids)
                + "."
            )
        )
    blocks.append(legend)
    set_table = _table(
        ["Свойство", "Пункты стандартов", "Статус", "Находки"],
        (
            [
                sr.property.label,
                "; ".join(sr.refs),
                STATE_LABELS[sr.state],
                cell_text(sr) if sr.finding_ids else "—",
            ]
            for sr in cov.set_rows
        ),
        [0.2, 0.45, 0.2, 0.15],
        hyph=[1],
    )
    blocks += [
        {"t": "h2", "text": "Набор требований × свойства: 29148 п. 5.2.6 и IEEE 830 п. 4.3"},
        set_table,
    ]
    return {"title": title, "blocks": blocks}


def _selfcheck_section(sc: SelfCheck) -> Json:
    note = (
        "Агент проверил готовый отчёт по ТЗ п. 6 и не правил его задним числом: провал виден здесь, "
        "в паспорте и в диагностике прогона."
    )
    rows = [
        [
            item.title,
            "пройдена" if item.passed else "НЕ ПРОЙДЕНА",
            plain(item.detail if item.passed else "; ".join(item.failures), 600),
        ]
        for item in sc.items
    ]
    blocks: list[Json] = [
        _para(plain(sc.summary)),
        _para(note),
        _table(
            ["Проверка", "Результат", "Что проверено или что не сошлось"],
            rows,
            [0.36, 0.14, 0.5],
            hyph=[0, 2],
        ),
    ]
    if sc.quotes_rechecked:
        blocks.append(
            _para(
                f"Цитаты находок ({sc.quotes_rechecked}) код сверил ещё раз по тексту документа: "
                "фрагмент по якорю совпал с цитатой."
            )
        )
    return {"title": SECTION_TITLES["selfcheck"], "blocks": blocks}


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
        sections.append(_unperformed_section(report, ctx))
    else:
        sections += [
            _segmentation_section(report),
            _requirements_section(report, ctx),
            _set_section(report, ctx),
            _questions_section(report, ctx),
            _summary_section(report, ctx),
            _conclusion_section(report, ctx),
            _metrics_section(report, ctx),
        ]
    sections.append(_diagnostics_section(report))
    if report.conclusion.performed:
        sections.append(_coverage_section(report, ctx))
        if report.selfcheck is not None and report.selfcheck.applicable:
            sections.append(_selfcheck_section(report.selfcheck))
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
        "anchors": _anchors(sections),
    }


def _anchors(sections: Sequence[Json]) -> list[str]:
    """F-NN и Q-NN, у которых в PDF есть карточка: только на них ``report.typ`` ставит ссылки.

    Список собирается из готовых карточек, а не из отчёта: ссылка на метку, которой нет, ломает сборку PDF.
    """
    return [b["anchor"] for s in sections for b in s["blocks"] if b.get("t") == "card" and b.get("anchor")]


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
