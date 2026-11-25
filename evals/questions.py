"""Метрики вопросов автору в eval (исследование 17, п. 4.3; issue #41): до и после критика-самоответа.

Считает заново по ``report.json`` прогона агента, той же функцией, что и отчёт
(``validator.questions.metrics.question_metrics``), и не доверяет числам из ``report.metrics``:

- **до критика**: вопросы в отчёте плюс снятые критиком (``questions_withdrawn``);
- **после критика**: вопросы в отчёте, снятые считаются отдельно;
- **стабильность**: средний попарный Жаккар множеств блоков документа, о которых спрашивают вопросы
  (по исследованию 17 цель от 0,7).

Эталон здесь не нужен: метрики вопросов не зависят от судьи, поэтому их можно считать отдельно и без
сети: ``python -m evals.questions ПАПКА [ПАПКА ...] --name ИМЯ`` пишет
``evals/results/<дата>-questions-<имя>.json`` и ``.md``. Те же метрики входят в обычный
``python -m evals.eval`` (раздел «Вопросы автору» результата).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path
from statistics import fmean

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.judge import EvalError, display_path, read_utf8
from validator.models import Question, QuestionMetrics, Report
from validator.questions.metrics import question_metrics
from validator.report.fmt import cell

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS = EVALS_DIR / "results"
ASTANA = timezone(timedelta(hours=5))
REPORT_NAME = "report.json"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class QuestionScore(BaseModel):
    """Вопросы одного прогона агента: метрики до и после критика и места документа."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    before: QuestionMetrics
    after: QuestionMetrics
    places: list[str] = Field(default_factory=list, description="ID блоков, о которых вопросы после критика")
    withdrawn: list[str] = Field(default_factory=list, description="Снятые вопросы: «вопрос → ответ»")


class QuestionsConfig(BaseModel):
    """Средние по прогонам одной конфигурации: до и после критика."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runs: int
    total_before: float | None
    total_after: float | None
    withdrawn_share: float | None
    with_scenario_before: float | None
    with_scenario_after: float | None
    closed_before: float | None
    closed_after: float | None
    without_errors_before: float | None
    without_errors_after: float | None
    obvious_fix_before: float | None
    obvious_fix_after: float | None
    duplicate_pairs_before: float | None
    duplicate_pairs_after: float | None
    place_jaccard: float | None = Field(
        description="Жаккар мест документа, о которых вопросы, между прогонами"
    )


def _places(questions: Iterable[Question]) -> list[str]:
    return sorted({b for q in questions for x in q.quotes if x.anchor for b in x.anchor.block_ids})


def score_questions(report: Report, label: str) -> QuestionScore | None:
    """Метрики вопросов прогона; ``None``, если проверка не выполнялась (вопросов быть не могло)."""
    if report.verification is None:
        return None
    before = [*report.questions, *(w.question for w in report.questions_withdrawn)]
    return QuestionScore(
        label=label,
        before=question_metrics(before),
        after=question_metrics(report.questions, report.questions_withdrawn),
        places=_places(report.questions),
        withdrawn=[
            f"{w.question.id} {cell(w.question.question)} → «{cell(w.answer.located or w.answer.text)}»"
            for w in report.questions_withdrawn
        ],
    )


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(fmean(present), 4) if present else None


def _jaccard(sets: Sequence[set[str]]) -> float | None:
    pairs = list(combinations(sets, 2))
    if not pairs:
        return None
    return round(fmean((len(a & b) / len(a | b)) if a | b else 1.0 for a, b in pairs), 4)


def aggregate_questions(scores: Sequence[QuestionScore]) -> QuestionsConfig | None:
    if not scores:
        return None

    def both(get: Callable[[QuestionMetrics], float | int | None]) -> tuple[float | None, float | None]:
        return _mean(get(s.before) for s in scores), _mean(get(s.after) for s in scores)

    total = both(lambda m: m.total)
    scenario = both(lambda m: m.with_scenario)
    closed = both(lambda m: m.closed)
    errors = both(lambda m: m.without_errors)
    obvious = both(lambda m: m.obvious_fix)
    duplicates = both(lambda m: m.duplicate_pairs)
    return QuestionsConfig(
        runs=len(scores),
        total_before=total[0],
        total_after=total[1],
        withdrawn_share=_mean(s.after.withdrawn_share for s in scores),
        with_scenario_before=scenario[0],
        with_scenario_after=scenario[1],
        closed_before=closed[0],
        closed_after=closed[1],
        without_errors_before=errors[0],
        without_errors_after=errors[1],
        obvious_fix_before=obvious[0],
        obvious_fix_after=obvious[1],
        duplicate_pairs_before=duplicates[0],
        duplicate_pairs_after=duplicates[1],
        place_jaccard=_jaccard([set(s.places) for s in scores]),
    )


# ---------------------------------------------------------------- Markdown


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f} %".replace(".", ",")


def _num(value: float | None, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}".replace(".", ",")


def _counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in counts.items()) or "—"


def render_questions(config: QuestionsConfig | None, runs: Sequence[QuestionScore]) -> list[str]:
    """Раздел «Вопросы автору» для результата eval: сводка до и после критика и таблица прогонов."""
    if config is None:
        return ["Нет прогонов агента с проверкой: вопросов нет."]
    c = config
    rows = [
        ("Вопросов в среднем", _num(c.total_before), _num(c.total_after)),
        ("Снято самоответом (доля)", "—", _pct(c.withdrawn_share)),
        ("Со сценарием (по критику)", _pct(c.with_scenario_before), _pct(c.with_scenario_after)),
        ("Закрытые: 2–4 варианта с последствиями", _pct(c.closed_before), _pct(c.closed_after)),
        ("Без ошибок формулировки (по критику)", _pct(c.without_errors_before), _pct(c.without_errors_after)),
        ("Дубли с находками (правка однозначна)", _num(c.obvious_fix_before), _num(c.obvious_fix_after)),
        ("Повторы: пары похожих вопросов", _num(c.duplicate_pairs_before), _num(c.duplicate_pairs_after)),
    ]
    lines = [
        "| Метрика | До критика | После критика |",
        "|---|---|---|",
        *(f"| {k} | {b} | {a} |" for k, b, a in rows),
        "",
        f"Жаккар мест документа, о которых вопросы, между прогонами: {_num(c.place_jaccard, 3)}.",
        "",
        "| Прогон | Вопросов до / после | Снято | Сценарий | Закрытые | Без ошибок | Дубли с находками "
        "| Приоритеты | Адресаты |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for s in runs:
        m = s.after
        lines.append(
            f"| `{s.label}` | {m.before_critic} / {m.total} | {m.withdrawn} | {_pct(m.with_scenario)} | "
            f"{_pct(m.closed)} | {_pct(m.without_errors)} | {m.obvious_fix} | {_counts(m.by_priority)} | "
            f"{_counts(m.by_addressee)} |"
        )
    withdrawn = [f"- `{s.label}`: {w}" for s in runs for w in s.withdrawn]
    if withdrawn:
        lines += ["", "Снятые вопросы:", "", *withdrawn]
    return lines


# ---------------------------------------------------------------- CLI


class QuestionsResult(BaseModel):
    """Содержимое ``evals/results/<дата>-questions-<имя>.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    created_at: datetime
    config: QuestionsConfig | None
    runs: list[QuestionScore]


def _report_path(path: Path) -> Path:
    if path.is_file():
        return path
    if (path / REPORT_NAME).is_file():
        return path / REPORT_NAME
    raise EvalError(f"В {path} нет report.json агента")


def load_scores(paths: Sequence[Path]) -> list[QuestionScore]:
    scores = []
    for path in paths:
        report_path = _report_path(path)
        try:
            report = Report.model_validate_json(read_utf8(report_path, "Отчёт агента"))
        except (ValidationError, ValueError) as exc:
            raise EvalError(f"{report_path} не отчёт агента: {exc.__class__.__name__}") from exc
        score = score_questions(report, display_path(path))
        if score is not None:
            scores.append(score)
    return scores


def render_result(result: QuestionsResult) -> str:
    header = [
        f"# Вопросы автору «{result.name}», {result.created_at:%Y-%m-%d %H:%M} (Астана)",
        "",
        "Метрики исследования 17, п. 4.3: считает код по `report.json` (`evals/questions.py`). "
        "Сценарий и ошибки формулировки отмечает критик-самоответ, снятие вопроса решает код по сверенной "
        "цитате-ответу.",
        "",
    ]
    return "\n".join([*header, *render_questions(result.config, result.runs)]) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m evals.questions", description="Метрики вопросов автору (#41)."
    )
    p.add_argument("runs", nargs="+", type=Path, help="папки прогонов агента (report.json)")
    p.add_argument("--name", required=True)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS)
    args = p.parse_args(argv)
    if not NAME_PATTERN.match(args.name):
        print(f"Ошибка eval: имя «{args.name}» не подходит: латиница, цифры, точка, дефис", file=sys.stderr)
        return 1
    try:
        scores = load_scores(args.runs)
    except EvalError as exc:
        print(f"Ошибка eval: {exc}", file=sys.stderr)
        return 1
    result = QuestionsResult(
        name=args.name, created_at=datetime.now(ASTANA), config=aggregate_questions(scores), runs=scores
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.created_at:%Y-%m-%d}-questions-{args.name}"
    json_path, md_path = args.out_dir / f"{stem}.json", args.out_dir / f"{stem}.md"
    json_path.write_text(result.model_dump_json(indent=1) + "\n", encoding="utf-8")
    text = render_result(result)
    md_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"Результаты: {display_path(json_path)}, {display_path(md_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
