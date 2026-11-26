"""Чужие документы (holdout, #48): сводная таблица по прогонам и разметке.

Документы лежат в ``data/holdout/<имя>/`` (источник, лицензия, SHA-256: ``data/holdout/README.md``).
Агент при разработке их не проверял, промпты и правила по этим результатам не правятся
(``docs/no-overfitting.md``). Эталона дефектов у чужих документов нет, поэтому recall здесь не
считается. Меряем то, что можно без эталона: долю сверенных цитат и precision по слепой разметке
двумя моделями (#49), а для ТЗ кейса ещё совпадение вопросов агента с вопросами kick-off.

Папка одного документа: ``evals/holdout/<имя>/`` (``report.json``, ``report.md``, ``questions.yaml``,
``run/``, ``<дата>-precision-holdout-<имя>.json``). Таблица собирается только из этих файлов::

    uv run python -m evals.holdout_summary      # → evals/holdout/summary.md
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from evals.judge import EvalError, display_path
from evals.kickoff_report import KickoffResult
from evals.precision import PrecisionResult
from evals.precision_labeler import Labeler
from evals.precision_metrics import Precision, Slice
from evals.runs import RunData, load_document, load_run
from validator.models import Report, Severity

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "holdout"
RUNS = ROOT / "evals" / "holdout"
SUMMARY = RUNS / "summary.md"
KICKOFF = RUNS / "kickoff.json"
PRECISION_GLOB = "*-precision-holdout-{name}.json"
NO_RUN = "прогона нет"
NO_LABELS = "разметки нет"


@dataclass(frozen=True)
class HoldoutDoc:
    name: str
    file: str
    title: str

    @property
    def path(self) -> Path:
        return DATA / self.name / self.file

    @property
    def run_dir(self) -> Path:
        return RUNS / self.name


DOCS: tuple[HoldoutDoc, ...] = (
    HoldoutDoc("c4", "c4_tech_task.md", "REST API компаний и сотрудников, тестовое задание"),
    HoldoutDoc("task", "task.md", "ТЗ кейса хакатона"),
    HoldoutDoc("finuchet", "FINUCHET.001-01-TZ.pdf", "«ФинУчёт»: учёт личных финансов, ТЗ по ГОСТ"),
    HoldoutDoc("repair-webapp", "repair_webapp_tz.md", "Модуль «Ремонт» для Telegram WebApp"),
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


def load_precision(doc: HoldoutDoc) -> PrecisionResult | None:
    """Последняя разметка документа двумя разметчиками (файлы ``-opus-only`` сюда не попадают)."""
    found = sorted(doc.run_dir.glob(PRECISION_GLOB.format(name=doc.name)))
    if not found:
        return None
    return PrecisionResult.model_validate_json(found[-1].read_text(encoding="utf-8"))


def _try_run(doc: HoldoutDoc) -> RunData | None:
    if not (doc.run_dir / "report.json").is_file():
        return None
    try:
        return load_run(doc.run_dir, doc.path)
    except EvalError as exc:
        print(f"{display_path(doc.run_dir)}: {exc}", file=sys.stderr)
        return None


def _found_row(doc: HoldoutDoc, run: RunData | None) -> str:
    if run is None:
        return f"| `{doc.name}` | {doc.title} | {NO_RUN} |"
    report = Report.model_validate_json((doc.run_dir / "report.json").read_text(encoding="utf-8"))
    passport = report.passport
    summary = report.verification.summary if report.verification else None
    by_sev = summary.by_severity if summary else {}
    sev = " / ".join(str(by_sev.get(s, 0)) for s in (Severity.CRITICAL, Severity.MAJOR, Severity.MINOR))
    pages = f", {passport.document_pages} стр." if passport.document_pages else ""
    size = f"{passport.document_kind.value.upper()}{pages}, {len(load_document(doc.path).text)} зн."
    quotes = (
        f"{_pct(run.quotes.share_verified)} ({run.quotes.exact + run.quotes.fuzzy} из {run.quotes.total})"
    )
    return (
        f"| `{doc.name}` | {doc.title} | {size} "
        f"| {summary.requirements_total if summary else '—'} | {summary.findings_total if summary else '—'} "
        f"| {sev} | {summary.discarded_total if summary else '—'} | {quotes} "
        f"| {len(report.questions)} | {report.conclusion.traffic_light.value} |"
    )


def _ran_row(doc: HoldoutDoc, run: RunData | None) -> str:
    if run is None:
        return f"| `{doc.name}` | {NO_RUN} |"
    passport = Report.model_validate_json((doc.run_dir / "report.json").read_text(encoding="utf-8")).passport
    minutes = passport.duration_s / 60 if passport.duration_s else None
    return (
        f"| `{doc.name}` | `{passport.agent_version}` | {passport.profile.value}, проходов {passport.passes} "
        f"| {passport.calls_total} (сбоев {passport.calls_failed}) | {_num(minutes, 1)} мин "
        f"| ${_num(passport.cost_usd)} | {passport.exit_code.value} "
        f"| {', '.join(passport.checklists) or '—'} |"
    )


def _slice_cells(s: Slice) -> str:
    return (
        f"{_pair(s.by_labeler.get(Labeler.OPUS))} | {_pair(s.by_labeler.get(Labeler.FABLE))} "
        f"| {_pair(s.consensus)} | {_counts(s.consensus)} | {_pct(s.agreement.share)}"
    )


def _precision_row(doc: HoldoutDoc, result: PrecisionResult | None) -> str:
    if result is None:
        return f"| `{doc.name}` | {NO_LABELS} |"
    return f"| `{doc.name}` | {result.overall.consensus.total} | {_slice_cells(result.overall)} |"


def _severity_rows(doc: HoldoutDoc, result: PrecisionResult | None) -> list[str]:
    if result is None:
        return []
    return [
        f"| `{doc.name}` | {s.name} | {s.consensus.total} | {_slice_cells(s)} |" for s in result.by_severity
    ]


def _control_row(doc: HoldoutDoc, result: PrecisionResult | None) -> str:
    if result is None:
        return f"| `{doc.name}` | {NO_LABELS} |"
    cells = " | ".join(
        f"{c.caught} из {c.labeled}" if (c := result.controls.get(lab)) else "—" for lab in Labeler
    )
    total = next(iter(result.controls.values())).total if result.controls else 0
    return f"| `{doc.name}` | {total} | {cells} |"


def _kickoff_lines() -> list[str]:
    if not KICKOFF.is_file():
        return ["Сравнение не запускалось: файла `kickoff.json` нет.", ""]
    result = KickoffResult.model_validate_json(KICKOFF.read_text(encoding="utf-8"))
    rows = [
        f"| {lab.display} | {s.n_labeled} | {_pct(s.strict)} / {_pct(s.soft)} |"
        for lab, s in result.per_judge.items()
    ]
    both = result.consensus
    return [
        f"Вопросов участников kick-off: {result.settings.n_kickoff}, вопросов агента к ТЗ кейса: "
        f"{result.settings.n_agent}. Строгая доля: судья сказал «совпало»; мягкая: «совпало» или «частично».",
        "",
        "| Судья | Вопросов kick-off размечено | Нашлись среди вопросов агента: строгая / мягкая |",
        "|---|---:|---:|",
        *rows,
        f"| оба | {both.n_labeled} | {_pct(both.strict)} / {_pct(both.soft)} |",
        "",
        f"Согласие судей: {_pct(result.agreement.share)}. Подробности: `kickoff.md`.",
        "",
    ]


PRECISION_HEAD = (
    "Opus 5.5: строгая / мягкая | Fable 5.2: строгая / мягкая | Оба: строгая / мягкая "
    "| Оба: настоящих / спорных / ложных | Согласие |"
)


def render_summary() -> str:
    runs = {doc.name: _try_run(doc) for doc in DOCS}
    labels = {doc.name: load_precision(doc) for doc in DOCS}
    return "\n".join(
        [
            "# Чужие документы: сводка",
            "",
            "Собрано кодом: `uv run python -m evals.holdout_summary`. Числа берутся из `report.json`",
            "прогонов, из разметки `<документ>/*-precision-holdout-<документ>.json` и из `kickoff.json`.",
            "Пояснения и оговорки: `README.md` рядом.",
            "",
            "## Что нашёл агент",
            "",
            "| Документ | Что это | Формат и объём | Требований | Находок | Критично / важно / незначительно "
            "| Отброшено верификатором | Цитаты сверены кодом | Вопросов автору | Светофор |",
            "|---|---|---|---:|---:|---|---:|---|---:|---|",
            *(_found_row(doc, runs[doc.name]) for doc in DOCS),
            "",
            "## Как прошёл прогон",
            "",
            "| Документ | Версия агента | Профиль | Вызовов модели | Время | Цена по тарифу API "
            "| Код возврата | Чек-листы |",
            "|---|---|---|---|---:|---:|---:|---|",
            *(_ran_row(doc, runs[doc.name]) for doc in DOCS),
            "",
            "## Precision: слепая разметка двумя моделями",
            "",
            "Строгая: «настоящий» от размеченных. Мягкая: «настоящий» или «спорно». «Оба»: строгая, если оба",
            "разметчика сказали «настоящий», мягкая, если ни один не сказал «ложный».",
            "",
            "| Документ | Находок | " + PRECISION_HEAD,
            "|---|---:|---:|---:|---:|---|---:|",
            *(_precision_row(doc, labels[doc.name]) for doc in DOCS),
            "",
            "### По важности",
            "",
            "| Документ | Важность | Находок | " + PRECISION_HEAD,
            "|---|---|---:|---:|---:|---:|---|---:|",
            *(row for doc in DOCS for row in _severity_rows(doc, labels[doc.name])),
            "",
            "## Контрольные ложные находки",
            "",
            "Сколько подмешанных заведомо ложных находок разметчик пометил «ложный».",
            "",
            "| Документ | Подмешано | " + " | ".join(lab.display for lab in Labeler) + " |",
            "|---|---:|---|---|",
            *(_control_row(doc, labels[doc.name]) for doc in DOCS),
            "",
            "## Вопросы агента к ТЗ кейса и вопросы участников kick-off",
            "",
            *_kickoff_lines(),
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # аргументов нет: состав документов задан в DOCS
    SUMMARY.write_text(render_summary(), encoding="utf-8")
    print(f"Сводка: {display_path(SUMMARY)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
