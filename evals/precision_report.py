"""Результат разметки precision (#49) в Markdown: для людей, описания PR, README и слайдов.

Эталона здесь нет: разметчики его не видели, а сравнение с судьёй показывает только доли.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from evals.precision_items import LabelItem, Origin
from evals.precision_labeler import ItemLabel, Label, Labeler
from evals.report import money, num, pct

if TYPE_CHECKING:
    from evals.precision import PrecisionResult
    from evals.precision_metrics import ControlScore, Precision, Slice

QUOTE_CHARS = 140
MAX_DISAGREEMENTS = 40


def _cell(text: str | None) -> str:
    return (text or "—").replace("|", "\\|").replace("\n", " ")


def _short(text: str, limit: int = QUOTE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _pair(p: Precision) -> str:
    if not p.labeled:
        return "—"
    return f"{pct(p.strict)} / {pct(p.soft)}"


def _kappa(value: float | None) -> str:
    return num(value, 2)


def _slice_rows(slices: Sequence[Slice], labelers: Sequence[Labeler]) -> list[str]:
    head = "| Срез | Находок | " + " | ".join(f"{lab.display}: строгая / мягкая" for lab in labelers)
    head += " | Оба: строгая / мягкая | Согласие | κ (3 метки) | κ (настоящий или нет) |"
    rows = [head, "|---|---:|" + "---:|" * len(labelers) + "---:|---:|---:|---:|"]
    for s in slices:
        per = " | ".join(_pair(s.by_labeler[lab]) for lab in labelers)
        a = s.agreement
        rows.append(
            f"| {_cell(s.name)} | {s.consensus.total} | {per} | {_pair(s.consensus)} | "
            f"{pct(a.share)} | {_kappa(a.kappa3)} | {_kappa(a.kappa2)} |"
        )
    return rows


def _control_row(name: str, scores: dict[Labeler, ControlScore], labelers: Sequence[Labeler]) -> str:
    cells = []
    for lab in labelers:
        c = scores.get(lab)
        cells.append(
            "—" if c is None else f"{c.caught}/{c.labeled} ({pct(c.caught_share)}) · {pct(c.not_real_share)}"
        )
    total = next(iter(scores.values())).total if scores else 0
    return f"| {name} | {total} | " + " | ".join(cells) + " |"


def _controls(result: PrecisionResult, labelers: Sequence[Labeler]) -> list[str]:
    lines = [
        "## Можно ли верить разметчикам: контрольные ложные находки",
        "",
        "Код подмешал в каждую пачку заведомо ложные находки, собранные из настоящих. В ячейке: поймано "
        "(метка «ложный») из размеченных и доля «не настоящий» (ложный или спорно).",
        "",
        "| Вид | Подмешано | " + " | ".join(lab.display for lab in labelers) + " |",
        "|---|---:|" + "---|" * len(labelers),
        _control_row("все контрольные", result.controls, labelers),
    ]
    lines += [_control_row(kind.label, scores, labelers) for kind, scores in result.controls_by_kind.items()]
    return lines


def _judge(result: PrecisionResult, labelers: Sequence[Labeler]) -> list[str]:
    lines = [
        "## Для сравнения: судья eval (#24)",
        "",
        "Судья видит эталон и оценивает находки иначе: «настоящая» у него это совпала с эталоном или "
        "real вне эталона. Совпадение: доля находок, где метка разметчика «настоящий / нет» "
        "совпала с судьёй.",
        "",
    ]
    if not result.judge:
        return [*lines, "Вердиктов судьи для этих прогонов в кеше нет."]
    lines += [
        "| Прогон | Находок | Судья: настоящие | Судья: шум | "
        + " | ".join(f"Совпадение: {lab.display}" for lab in labelers)
        + " |",
        "|---|---:|---:|---:|" + "---:|" * len(labelers),
    ]
    for j in result.judge:
        agree = " | ".join(pct(j.agree.get(lab)) for lab in labelers)
        lines.append(f"| {_cell(j.run)} | {j.items} | {pct(j.real_share)} | {pct(j.noise_share)} | {agree} |")
    return lines


def _who(item: LabelItem) -> str:
    if item.origin is Origin.CONTROL:
        return f"контрольная {item.ref} ({item.control.label if item.control else '—'})"
    return f"{item.origin.label} {item.ref}"


def _reasons(
    item: LabelItem, labels: dict[Labeler, dict[str, ItemLabel]], labelers: Sequence[Labeler]
) -> str:
    out = []
    for lab in labelers:
        mark = labels[lab].get(item.uid)
        out.append("—" if mark is None else f"{mark.label.label}: {_cell(mark.reason)}")
    return " | ".join(out)


def _listing(
    title: str, intro: str, items: Sequence[LabelItem], result: PrecisionResult, labelers: Sequence[Labeler]
) -> list[str]:
    lines = [f"## {title}", "", intro, ""]
    if not items:
        return [*lines, "Нет."]
    lines += [
        "| Находка | Место | Цитата | " + " | ".join(lab.display for lab in labelers) + " |",
        "|---|---|---|" + "---|" * len(labelers),
    ]
    for item in items:
        lines.append(
            f"| {_cell(_who(item))} | {_cell(item.place)} | {_cell(_short(item.quote))} | "
            f"{_reasons(item, result.labels, labelers)} |"
        )
    return lines


def _header(result: PrecisionResult, labelers: Sequence[Labeler]) -> list[str]:
    s = result.settings
    real = [i for i in result.items if i.origin is not Origin.CONTROL]
    controls = len(result.items) - len(real)
    cost = sum(c.cost_usd or 0 for c in result.calls)
    minutes = sum(c.duration_ms or 0 for c in result.calls) / 60000
    sources = sorted({c.source for c in result.calls})
    who = ", ".join(f"{i.labeler.display} (`{i.model}`, `{i.effort}`)" for i in s.labelers)
    return [
        f"# Precision: {result.name}",
        "",
        "**Разметка моделями, не людьми** (issue #49). Разметчики не видели эталон, вердикты судьи и "
        "разметку друг друга и не знали, кто писал находки: агент и базовая линия шли вперемешку.",
        "",
        f"- Дата: {result.created_at:%Y-%m-%d %H:%M} (Астана)",
        f"- Разметчики: {who}",
        f"- Документ: `{s.document}` (sha256 {s.document_sha256[:12]}…)",
        f"- Прогоны: {', '.join(f'`{r}`' for r in s.runs)}",
        f"- Находок: {len(real)} настоящих и {controls} контрольных, пачек {len(result.batches)} "
        f"(до {s.batch_size} настоящих в пачке, seed {s.seed})",
        f"- Вызовы: {len(result.calls)} ({', '.join(sources)}), {num(minutes)} мин моделей, "
        f"${money(cost)} по тарифу API",
        "",
        "Строгая precision: «настоящий» от размеченных. Мягкая: «настоящий» или «спорно». «Оба»: "
        "строгая, если оба разметчика сказали «настоящий», мягкая, если ни один не сказал «ложный».",
    ]


def _marks(item: LabelItem, result: PrecisionResult, labelers: Sequence[Labeler]) -> list[Label]:
    return [result.labels[lab][item.uid].label for lab in labelers if item.uid in result.labels[lab]]


FALSE_INTRO = "Кандидаты в правки агента: каждая отдельным PR с цифрой eval (CLAUDE.md, правило 5)."


def render_markdown(result: PrecisionResult) -> str:
    labelers = [i.labeler for i in result.settings.labelers]
    lines = _header(result, labelers)
    lines += ["", *_controls(result, labelers), "", "## Precision", ""]
    lines += _slice_rows([result.overall, *result.by_run], labelers)
    for title, slices in (
        ("По важности (агент)", result.by_severity),
        ("Кто нашёл (агент)", result.by_finder),
        ("В скольких проходах найдено (агент)", result.by_passes),
    ):
        if slices:
            lines += ["", f"### {title}", "", *_slice_rows(slices, labelers)]
    lines += ["", *_judge(result, labelers)]
    real = [i for i in result.items if i.origin is not Origin.CONTROL]
    all_false = [i for i in real if (m := _marks(i, result, labelers)) and set(m) == {Label.FALSE}]
    split = [i for i in real if len(set(_marks(i, result, labelers))) > 1]
    lines += ["", *_listing("Ложные по мнению всех разметчиков", FALSE_INTRO, all_false, result, labelers)]
    intro = f"Всего {len(split)}."
    if len(split) > MAX_DISAGREEMENTS:
        intro += f" Показаны первые {MAX_DISAGREEMENTS}."
    lines += ["", *_listing("Разметчики разошлись", intro, split[:MAX_DISAGREEMENTS], result, labelers)]
    controls = [i for i in result.items if i.origin is Origin.CONTROL]
    lines += ["", *_listing("Контрольные находки и что про них сказали", "", controls, result, labelers)]
    if result.warnings or result.notes:
        lines += ["", "## Предупреждения и заметки", ""]
        lines += [f"- {w}" for w in [*result.warnings, *result.notes]]
    return "\n".join(lines) + "\n"
