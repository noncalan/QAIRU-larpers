"""Чужие документы (holdout, #48): базовая линия на них и сводная таблица.

Документы лежат в ``data/holdout/<имя>/`` вместе с лицензией автора. Агент при разработке их не
видел: промпты и правила по результатам этих прогонов не правятся (``docs/no-overfitting.md``).
Эталона дефектов у чужих документов нет, поэтому recall здесь не считается. Меряем то, что можно
без эталона: долю сверенных цитат и precision по слепой разметке двумя моделями (#49).

Папки прогонов одного документа::

    evals/results/holdout/<имя>/agent/            report.json, report.md, questions.yaml, run/
    evals/results/holdout/<имя>/baseline/run-1/   answer.md, calls.jsonl, responses/

Запуск::

    uv run validator data/holdout/c4/c4_tech_task.md evals/results/holdout/c4/agent/report.md \
        --profile standard --max-budget-usd 5 --backend cli
    uv run python -m evals.holdout baseline            # «один промпт» на каждом документе
    uv run python -m evals.precision label evals/results/holdout/c4/agent \
        evals/results/holdout/c4/baseline/run-1 --document data/holdout/c4/c4_tech_task.md \
        --name holdout-c4
    uv run python -m evals.holdout summary             # evals/results/holdout/summary.md
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from evals.baseline import run_once
from evals.judge import EvalError, display_path
from evals.precision import PrecisionResult
from evals.precision_labeler import Labeler
from evals.precision_metrics import Precision, Slice
from evals.runs import CallStats, RunData, Source, load_document, load_run
from validator.errors import LLMError
from validator.models import Report, Severity

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "holdout"
RESULTS = ROOT / "evals" / "results"
RUNS = RESULTS / "holdout"
SUMMARY = RUNS / "summary.md"
PRECISION_GLOB = "*-precision-holdout-{name}.json"
AGENT_DIR = "agent"
BASELINE_DIR = "baseline"
BASELINE_RUN = 1


@dataclass(frozen=True)
class HoldoutDoc:
    name: str
    file: str
    title: str

    @property
    def path(self) -> Path:
        return DATA / self.name / self.file

    @property
    def agent_dir(self) -> Path:
        return RUNS / self.name / AGENT_DIR

    @property
    def baseline_dir(self) -> Path:
        return RUNS / self.name / BASELINE_DIR / f"run-{BASELINE_RUN}"


DOCS: tuple[HoldoutDoc, ...] = (
    HoldoutDoc("c4", "c4_tech_task.md", "REST API компаний и сотрудников (тестовое задание)"),
    HoldoutDoc("repair-webapp", "repair_webapp_tz.md", "Модуль «Ремонт» для Telegram WebApp"),
    HoldoutDoc("finuchet", "FINUCHET.001-01-TZ.pdf", "«ФинУчёт»: учёт личных финансов, ТЗ по ГОСТ"),
)


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f} %".replace(".", ",")


def _num(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def _pair(p: Precision | None) -> str:
    return "—" if p is None or not p.labeled else f"{_pct(p.strict)} / {_pct(p.soft)}"


def _counts(p: Precision | None) -> str:
    """«настоящих / спорных / ложных» по согласию двух разметчиков."""
    return "—" if p is None or not p.labeled else f"{p.real} / {p.disputed} / {p.false}"


def run_baselines(names: Sequence[str]) -> int:
    """Базовая линия «один промпт» (тот же промпт и модель, что в ``evals.baseline``), один прогон."""
    for doc in DOCS:
        if names and doc.name not in names:
            continue
        text = load_document(doc.path).text
        try:
            folder = run_once(text, BASELINE_RUN, doc.baseline_dir.parent)
        except LLMError as exc:
            print(f"{doc.name}: {exc.code}: {exc.message}", file=sys.stderr)
            return 1
        print(f"{doc.name}: базовая линия в {display_path(folder)}")
    return 0


def _load_precision(name: str) -> PrecisionResult | None:
    found = sorted(RESULTS.glob(PRECISION_GLOB.format(name=name)))
    if not found:
        return None
    return PrecisionResult.model_validate_json(found[-1].read_text(encoding="utf-8"))


def _slice(result: PrecisionResult | None, source: Source) -> Slice | None:
    if result is None:
        return None
    prefix = f"{source.label}: "
    return next((s for s in result.by_run if s.name.startswith(prefix)), None)


def _try_run(folder: Path, document: Path) -> RunData | None:
    try:
        return load_run(folder, document) if folder.is_dir() else None
    except EvalError as exc:
        print(f"{display_path(folder)}: {exc}", file=sys.stderr)
        return None


def _agent_rows(doc: HoldoutDoc, run: RunData | None) -> tuple[str, str]:
    """Строки таблиц «что нашёл агент» и «как прошёл прогон»."""
    report_path = doc.agent_dir / "report.json"
    if run is None or not report_path.is_file():
        return f"| `{doc.name}` | прогона нет |", f"| `{doc.name}` | прогона нет |"
    report = Report.model_validate_json(report_path.read_text(encoding="utf-8"))
    passport = report.passport
    summary = report.verification.summary if report.verification else None
    by_sev = summary.by_severity if summary else {}
    sev = " / ".join(str(by_sev.get(s, 0)) for s in (Severity.CRITICAL, Severity.MAJOR, Severity.MINOR))
    pages = f", {passport.document_pages} стр." if passport.document_pages else ""
    parsed = load_document(doc.path)
    minutes = passport.duration_s / 60 if passport.duration_s else None
    found = (
        f"| `{doc.name}` | {passport.document_kind.value.upper()}{pages}, {len(parsed.text)} зн. "
        f"| {summary.requirements_total if summary else '—'} | {summary.findings_total if summary else '—'} "
        f"| {sev} | {summary.discarded_total if summary else '—'} "
        f"| {_pct(run.quotes.share_verified)} ({run.quotes.exact + run.quotes.fuzzy} из {run.quotes.total}) "
        f"| {len(report.questions)} | {report.conclusion.traffic_light.value} |"
    )
    ran = (
        f"| `{doc.name}` | `{passport.agent_version}` | {passport.profile.value}, проходов {passport.passes} "
        f"| {passport.calls_total} (сбоев {passport.calls_failed}) | {_num(minutes, 1)} мин "
        f"| ${_num(passport.cost_usd)} | {passport.exit_code.value} "
        f"| {', '.join(c for c in passport.checklists) or '—'} |"
    )
    return found, ran


def _precision_rows(doc: HoldoutDoc, runs: dict[Source, RunData | None]) -> list[str]:
    result = _load_precision(doc.name)
    rows: list[str] = []
    for source in (Source.AGENT, Source.BASELINE):
        run = runs[source]
        s = _slice(result, source)
        calls: CallStats | None = run.calls if run else None
        rows.append(
            f"| `{doc.name}` | {source.label} | {len(run.items) if run else '—'} "
            f"| {_pct(run.quotes_final.share_verified) if run and run.quotes_final else '—'} "
            f"| {_pair(s.by_labeler.get(Labeler.OPUS) if s else None)} "
            f"| {_pair(s.by_labeler.get(Labeler.FABLE) if s else None)} "
            f"| {_pair(s.consensus if s else None)} | {_counts(s.consensus if s else None)} "
            f"| {_pct(s.agreement.share) if s else '—'} "
            f"| ${_num(calls.cost_usd if calls else None)} |"
        )
    return rows


def _control_row(doc: HoldoutDoc) -> str:
    result = _load_precision(doc.name)
    if result is None:
        return f"| `{doc.name}` | разметки нет |"
    cells = " | ".join(
        f"{c.caught} из {c.labeled} ({_pct(c.not_real_share)} не «настоящий»)"
        for c in result.controls.values()
    )
    total = next(iter(result.controls.values())).total if result.controls else 0
    return f"| `{doc.name}` | {total} | {cells} |"


def render_summary() -> str:
    found: list[str] = []
    ran: list[str] = []
    precision: list[str] = []
    controls: list[str] = []
    for doc in DOCS:
        runs = {
            Source.AGENT: _try_run(doc.agent_dir, doc.path),
            Source.BASELINE: _try_run(doc.baseline_dir, doc.path),
        }
        a, b = _agent_rows(doc, runs[Source.AGENT])
        found.append(a)
        ran.append(b)
        precision.extend(_precision_rows(doc, runs))
        controls.append(_control_row(doc))
    return "\n".join(
        [
            "# Чужие документы: сводка",
            "",
            "Собрано кодом: `uv run python -m evals.holdout summary`. Числа из `report.json` прогонов и из",
            "результатов разметки `evals/results/*-precision-holdout-*.json`. Пояснения и оговорки:",
            "`README.md` рядом.",
            "",
            "## Что нашёл агент",
            "",
            "| Документ | Формат и объём | Требований | Находок | Критично / важно / незначительно "
            "| Отброшено верификатором | Цитаты сверены кодом | Вопросов автору | Светофор |",
            "|---|---|---:|---:|---|---:|---|---:|---|",
            *found,
            "",
            "## Как прошёл прогон",
            "",
            "| Документ | Версия агента | Профиль | Вызовов модели | Время | Цена по тарифу API "
            "| Код возврата | Чек-листы |",
            "|---|---|---|---|---:|---:|---:|---|",
            *ran,
            "",
            "## Precision: слепая разметка двумя моделями",
            "",
            "Строгая: «настоящий» от размеченных. Мягкая: «настоящий» или «спорно». «Оба»: строгая, если оба",
            "разметчика сказали «настоящий», мягкая, если ни один не сказал «ложный».",
            "",
            "| Документ | Чьи находки | Находок | Цитаты итоговых находок в документе "
            "| Opus 5.5: строгая / мягкая | Fable 5.2: строгая / мягкая | Оба: строгая / мягкая "
            "| Оба: настоящих / спорных / ложных | Согласие | Цена прогона |",
            "|---|---|---:|---:|---:|---:|---:|---|---:|---:|",
            *precision,
            "",
            "## Контрольные ложные находки",
            "",
            "Поймано: разметчик поставил «ложный».",
            "",
            "| Документ | Подмешано | " + " | ".join(lab.display for lab in Labeler) + " |",
            "|---|---:|---|---|",
            *controls,
            "",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.holdout", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    base = sub.add_parser("baseline", help="базовая линия «один промпт» на чужих документах")
    base.add_argument("names", nargs="*", help="имена документов (по умолчанию все)")
    sub.add_parser("summary", help="сводная таблица в evals/results/holdout/summary.md")
    args = parser.parse_args(argv)
    if args.command == "baseline":
        return run_baselines(args.names)
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(render_summary(), encoding="utf-8")
    print(f"Сводка: {display_path(SUMMARY)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
