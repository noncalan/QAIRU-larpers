"""Результат eval в Markdown: сводка конфигурации, таблица прогонов, матрица дефектов, поправки.

Тексты дефектов эталона сюда не попадают: только номер, важность и критерий. Так результаты можно
читать при работе над промптами, не подглядывая в эталон (CLAUDE.md, правило 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from evals.gold import GoldSeverity, load_gold
from evals.judge import GoldStatus
from evals.questions import render_questions

if TYPE_CHECKING:
    from evals.eval import EvalResult
    from evals.metrics import RunScore
    from evals.runs import QuoteStats


def pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f} %".replace(".", ",")


def num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def money(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}".replace(".", ",")


def _quotes(q: QuoteStats | None) -> str:
    if q is None or not q.total:
        return "—"
    return f"{pct(q.share_exact)} / {pct(q.share_verified)} ({q.total})"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _summary(result: EvalResult) -> list[str]:
    c = result.config
    rows = [
        ("Прогонов", str(c.runs)),
        ("Recall (found + partial), среднее", pct(c.recall)),
        ("Recall строгий (только found)", pct(c.recall_strict)),
        ("Критичные (found + partial)", pct(c.critical_recall)),
        ("Критичные строго", pct(c.critical_recall_strict)),
        (
            "Объединение прогонов: с частичными / строго",
            f"{pct(c.union_recall)} / {pct(c.union_recall_strict)}",
        ),
        ("pass^k критичных (найден во всех прогонах)", pct(c.critical_pass_all)),
        ("pass@k критичных (хотя бы в одном)", pct(c.critical_pass_any)),
        ("Находок в среднем", num(c.findings)),
        ("Доля шума по судье", pct(c.noise_share)),
        (
            "Цитаты модели: дословно / сверены (точно или нечётко)",
            f"{pct(c.quotes_exact)} / {pct(c.quotes_verified)}",
        ),
        ("Цитаты итоговых находок сверены", pct(c.quotes_final_verified)),
        ("Жаккар найденных дефектов эталона", num(c.gold_jaccard, 3)),
        ("Жаккар находок (stable_key требования + свойство)", num(c.key_jaccard, 3)),
        ("Время прогона, с", num(c.wall_s, 0)),
        ("Цена прогона по тарифу API, $", money(c.cost_usd)),
        ("Цена живых вызовов судьи в этом eval, $", money(c.judge_cost_usd)),
    ]
    return ["| Метрика | Значение |", "|---|---|", *(f"| {k} | {v} |" for k, v in rows)]


def _runs_table(runs: list[RunScore]) -> list[str]:
    head = (
        "| Прогон | Источник | Находок | Recall | Строгий | Критичные | Крит. строго "
        "| Лишние: настоящие / шум | Шум | Цитаты: дословно / сверены (всего) | Итоговые сверены "
        "| Время, с | Цена, $ | Судья |"
    )
    lines = [head, "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        lines.append(
            f"| `{r.label}` | {r.source.label} | {r.findings} | {pct(r.recall)} | {pct(r.recall_strict)} | "
            f"{pct(r.critical_recall)} | {pct(r.critical_recall_strict)} | "
            f"{r.extra_real} / {r.extra_noise} | "
            f"{pct(r.noise_share)} | {_quotes(r.quotes)} | "
            f"{pct(r.quotes_final.share_verified) if r.quotes_final else '—'} | {num(r.calls.wall_s, 0)} | "
            f"{money(r.calls.cost_usd)} | {r.judge.source} |"
        )
    return lines


def _severity_table(runs: list[RunScore]) -> list[str]:
    lines = ["| Прогон | " + " | ".join(s.label for s in GoldSeverity) + " |", "|---|---|---|---|"]
    for r in runs:
        lines.append(
            f"| `{r.label}` | " + " | ".join(pct(r.recall_by_severity[s]) for s in GoldSeverity) + " |"
        )
    return lines


def _matrix(runs: list[RunScore]) -> list[str]:
    gold = load_gold()
    head = "| # | Важность | Критерий | " + " | ".join(str(n) for n in range(1, len(runs) + 1)) + " |"
    lines = [head, "|---|---|---|" + "---|" * len(runs)]
    for d in gold.defects:
        marks = " | ".join(r.gold.get(d.id, GoldStatus.MISSED).mark for r in runs)
        lines.append(f"| {d.id} | {d.severity.label} | {_cell(d.criterion)} | {marks} |")
    legend = "Столбцы: прогоны по порядку таблицы выше. ✓ found, ~ partial, · missed."
    return [legend, "", *lines]


def _missed(runs: list[RunScore]) -> list[str]:
    gold = load_gold()
    lines = []
    for r in runs:
        missed = r.ids_with(GoldStatus.MISSED)
        critical = [i for i in missed if i in gold.critical_ids]
        lines.append(f"- `{r.label}`: пропущено {len(missed)} {missed}; из них критичных {critical or 'нет'}")
    return lines


def _unmatched(runs: list[RunScore]) -> list[str]:
    judge_numbered = "— (нумеровал судья)"
    rows = [
        f"| `{r.label}` | {u.id} | {f'`{u.fingerprint}`' if u.fingerprint else '—'} "
        f"| {u.verdict.value if u.verdict else '—'} | {_cell(u.quote) if u.quote else judge_numbered} |"
        for r in runs
        for u in r.unmatched
    ]
    if not rows:
        return ["Нет."]
    return ["| Прогон | № | Отпечаток | Судья | Цитата (начало) |", "|---|---|---|---|---|", *rows]


def _notes(result: EvalResult) -> list[str]:
    lines = [f"- {n}" for n in result.notes]
    for r in result.runs:
        lines += [f"- `{r.label}`, поправка: {a}" for a in r.overrides]
        lines += [f"- `{r.label}`: {w}" for w in r.warnings]
        lines += [f"- `{r.label}`: {n}" for n in r.notes]
        if r.layout:
            lines.append(f"- `{r.label}`: свободный текст нарезан по разметке «{r.layout}»")
        if r.judge.cache_path:
            lines.append(f"- `{r.label}`: вердикт судьи `{r.judge.cache_path}`")
    return lines or ["Нет."]


def render_markdown(result: EvalResult) -> str:
    s = result.settings
    versions = sorted({r.agent_version for r in result.runs if r.agent_version})
    header = [
        f"# Eval «{result.name}», {result.created_at:%Y-%m-%d %H:%M} (Астана)",
        "",
        f"Судья: `{s.judge_model}` · `{s.judge_effort}`, режим `{s.judge_mode.value}`, "
        f"список находок `{s.judge_format.value}`. Эталон {s.gold_version} "
        f"(`{s.gold_sha256[:12]}`), {s.gold_total} дефектов, критичные {s.gold_critical}. "
        f"Документ `{s.document}` (`{s.document_sha256[:12]}`)."
        + (f" Версии агента: {', '.join(versions)}." if versions else ""),
        "",
        "Определения метрик: `evals/README.md`. Recall по эталону, который тоже составлял Claude: "
        "это оценка полноты, а не precision.",
    ]
    sections = [
        ("Сводка", _summary(result)),
        ("Прогоны", _runs_table(result.runs)),
        ("Recall по важности", _severity_table(result.runs)),
        ("Матрица: дефект эталона × прогон", _matrix(result.runs)),
        ("Пропуски", _missed(result.runs)),
        ("Находки вне эталона (для ручных поправок в evals/overrides.yaml)", _unmatched(result.runs)),
        (
            "Вопросы автору: до и после критика",
            render_questions(result.config.questions, [r.questions for r in result.runs if r.questions]),
        ),
        ("Поправки, предупреждения, заметки", _notes(result)),
    ]
    body: list[str] = header
    for title, lines in sections:
        body += ["", f"## {title}", "", *lines]
    return "\n".join(body) + "\n"
