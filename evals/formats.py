"""Согласие форматов: один документ в MD, PDF и DOCX, что агент нашёл в каждом (issue #36, таблица RC1).

Запуск::

    uv run python -m evals.formats out/md out/pdf out/docx --out evals/results/formats.md

Каждая папка это прогон агента (``report.json`` внутри или путь к самому ``.json``). Таблица прогонов
показывает код возврата, требования, находки, время и цену по тарифу API. Таблица согласия делит
находки на «во всех форматах», «в двух» и «только в одном».

**Как сравниваем находки разных форматов.** Тексты MD, PDF и DOCX чуть различаются (переносы, таблицы,
нумерация), поэтому цитаты каждой находки ищем в одном опорном тексте (по умолчанию разбор
``data/input/requirements.md``) той же функцией ``locate_quote``, что у верификатора агента. Две находки
разных форматов считаем одной, если их цитаты перекрываются в опорном тексте (не меньше
``MIN_SPAN_OVERLAP`` от более короткой) и свойства из одной группы слияния (``merge.PROPERTY_GROUPS``)
или тип дефекта тот же. Цитаты, которых в опорном тексте нет, сравниваем по тексту (rapidfuzz).

**Как читать.** Каждый формат это отдельный живой прогон, а модель от прогона к прогону отвечает
по-разному. Поэтому «только в одном формате» это сумма влияния формата и разброса модели. Разброс
одного формата меряет Жаккар eval (``evals.eval``) на повторных прогонах.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

from rapidfuzz import fuzz

from evals.judge import EvalError, display_path, read_utf8
from evals.report import money, num, pct
from evals.runs import load_document
from validator.merge import PROPERTY_GROUPS
from validator.models import DocKind, Finding, ParsedDocument, Report
from validator.text import locate_quote, normalize

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCE = REPO_ROOT / "data" / "input" / "requirements.md"
REPORT_NAME = "report.json"

MIN_SPAN_OVERLAP = 0.5
"""Доля более короткой цитаты, которая должна лечь на цитату другой находки в опорном тексте."""

TEXT_SIMILARITY = 85.0
"""Балл rapidfuzz (0–100) для цитат, которых нет в опорном тексте, и для находок без цитат."""

QUOTE_PREVIEW_CHARS = 90

_GROUP_OF = {p: i for i, group in enumerate(PROPERTY_GROUPS) for p in group}

Span = tuple[int, int]


@dataclass(frozen=True)
class FormatRun:
    label: str
    path: Path
    report: Report


@dataclass(frozen=True)
class Item:
    """Находка одного прогона и её место в опорном тексте."""

    run: int
    finding: Finding
    spans: tuple[Span, ...]
    texts: tuple[str, ...]

    @property
    def group(self) -> int:
        return _GROUP_OF.get(self.finding.property, -1)


def load_report(path: Path) -> Report:
    file = path / REPORT_NAME if path.is_dir() else path
    text = read_utf8(file, "Отчёт")
    try:
        return Report.model_validate_json(text)
    except ValueError as exc:
        raise EvalError(f"Отчёт {display_path(file)} не по схеме report.json: {exc}") from exc


def load_runs(paths: Sequence[Path]) -> list[FormatRun]:
    runs = []
    for path in paths:
        report = load_report(path)
        kind = report.passport.document_kind
        label = kind.value.upper() if kind is not DocKind.UNKNOWN else display_path(path)
        runs.append(FormatRun(label=label, path=path, report=report))
    return runs


def _spans(finding: Finding, reference: ParsedDocument) -> tuple[tuple[Span, ...], tuple[str, ...]]:
    spans: list[Span] = []
    texts: list[str] = []
    for quote in finding.quotes:
        text = quote.located or quote.text
        texts.append(normalize(text)[0])
        found = locate_quote(reference, text)
        if found.anchor is not None:
            spans.append((found.anchor.char_start, found.anchor.char_end))
    return tuple(spans), tuple(texts)


def items_of(runs: Sequence[FormatRun], reference: ParsedDocument) -> list[Item]:
    items = []
    for index, run in enumerate(runs):
        findings = run.report.verification.findings if run.report.verification else []
        for finding in findings:
            spans, texts = _spans(finding, reference)
            items.append(Item(run=index, finding=finding, spans=spans, texts=texts))
    return items


def _spans_overlap(a: Sequence[Span], b: Sequence[Span]) -> bool:
    for (a0, a1), (b0, b1) in ((x, y) for x in a for y in b):
        shorter = min(a1 - a0, b1 - b0)
        if shorter > 0 and (min(a1, b1) - max(a0, b0)) / shorter >= MIN_SPAN_OVERLAP:
            return True
    return False


def _texts_similar(a: Sequence[str], b: Sequence[str]) -> bool:
    return any(fuzz.partial_ratio(x, y) >= TEXT_SIMILARITY for x in a for y in b if x and y)


def same_defect(a: Item, b: Item) -> bool:
    """Одна ли это находка в двух форматах: место в опорном тексте и свойство (см. докстринг модуля)."""
    fa, fb = a.finding, b.finding
    if a.group != b.group and fa.defect_type is not fb.defect_type:
        return False
    if a.spans and b.spans:
        return _spans_overlap(a.spans, b.spans)
    if a.texts and b.texts:
        return _texts_similar(a.texts, b.texts)
    if a.texts or b.texts:
        return False
    # Находки набора без цитат: тот же раздел и похожая суть.
    title_a, title_b = fa.title or fa.explanation, fb.title or fb.explanation
    return fa.section_path == fb.section_path and fuzz.token_set_ratio(title_a, title_b) >= TEXT_SIMILARITY


def clusters(items: Sequence[Item]) -> list[list[int]]:
    """Группы похожих находок разных прогонов (объединение по парам, без пар внутри одного прогона)."""
    parent = list(range(len(items)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in combinations(range(len(items)), 2):
        if items[i].run != items[j].run and same_defect(items[i], items[j]):
            parent[root(j)] = root(i)
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(root(i), []).append(i)
    return list(groups.values())


# ---------------------------------------------------------------- Markdown


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _preview(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= QUOTE_PREVIEW_CHARS else text[: QUOTE_PREVIEW_CHARS - 1] + "…"


def runs_table(runs: Sequence[FormatRun]) -> list[str]:
    lines = [
        "| Формат | Код | Требований | Находок | Нарушений | Неясно | Критичных | Отброшено | "
        "Цитаты сверены | Вызовов | Время, с | Цена, $ |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for run in runs:
        r = run.report
        s = r.verification.summary if r.verification else None
        reqs = len(r.segmentation.requirements) if r.segmentation else 0
        lines.append(
            f"| {run.label} | {r.passport.exit_code.value} | {reqs} | {s.findings_total if s else 0} | "
            f"{s.violations if s else 0} | {s.unclear if s else 0} | {s.critical if s else 0} | "
            f"{s.discarded_total if s else 0} | {pct(r.metrics.verified_quote_share)} | "
            f"{r.metrics.calls_total} | {num(r.passport.duration_s, 0)} | {money(r.metrics.cost_usd)} |"
        )
    return lines


def agreement_table(
    runs: Sequence[FormatRun], items: Sequence[Item], groups: Sequence[list[int]]
) -> list[str]:
    total = len(runs)
    by_formats: dict[tuple[int, ...], int] = {}
    for group in groups:
        key = tuple(sorted({items[i].run for i in group}))
        by_formats[key] = by_formats.get(key, 0) + 1
    lines = ["| В каких форматах | Находок (группы) |", "|---|---|"]
    for size in range(total, 0, -1):
        for key in sorted(k for k in by_formats if len(k) == size):
            names = " + ".join(runs[i].label for i in key)
            lines.append(f"| {names} | {by_formats[key]} |")
    return lines


def singles_table(runs: Sequence[FormatRun], items: Sequence[Item], groups: Sequence[list[int]]) -> list[str]:
    singles = [items[g[0]] for g in groups if len({items[i].run for i in g}) == 1 and len(runs) > 1]
    if not singles:
        return ["Таких находок нет."]
    lines = ["| Формат | Находка | Свойство | Важность | Суть | Цитата |", "|---|---|---|---|---|---|"]
    for item in sorted(singles, key=lambda it: (it.run, it.finding.id)):
        f = item.finding
        quote = f.quotes[0].located or f.quotes[0].text if f.quotes else ""
        lines.append(
            f"| {runs[item.run].label} | {f.id} | {f.property.label} | {f.severity.label} | "
            f"{_cell(_preview(f.title or f.explanation))} | {_cell(_preview(quote))} |"
        )
    return lines


def render(runs: Sequence[FormatRun], reference: ParsedDocument) -> str:
    items = items_of(runs, reference)
    groups = clusters(items)
    located = sum(1 for it in items if it.spans)
    lines = [
        "# Согласие форматов",
        "",
        f"Прогоны: {', '.join(f'{r.label} `{display_path(r.path)}`' for r in runs)}. "
        f"Опорный текст для сравнения цитат: `{reference.name}`.",
        "",
        "## Прогоны",
        "",
        *runs_table(runs),
        "",
        "## Согласие находок",
        "",
        f"Находок всего: {len(items)}, из них с цитатой, найденной в опорном тексте: {located}. "
        f"Групп одной находки в разных форматах: {len(groups)}.",
        "",
        *agreement_table(runs, items, groups),
        "",
        "Каждый формат это отдельный живой прогон: «только в одном формате» включает и разброс модели "
        "между прогонами, а не только влияние формата.",
        "",
        "## Находки только в одном формате",
        "",
        *singles_table(runs, items, groups),
    ]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.formats", description=__doc__.split("\n")[0])
    parser.add_argument("runs", nargs="+", type=Path, help="Папки прогонов агента (report.json внутри)")
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE, help="Опорный документ")
    parser.add_argument("--out", type=Path, help="Куда записать Markdown (иначе в stdout)")
    args = parser.parse_args(argv)
    try:
        runs = load_runs(args.runs)
        reference = load_document(args.reference)
    except EvalError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    text = render(runs, reference)
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"Записано: {display_path(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
