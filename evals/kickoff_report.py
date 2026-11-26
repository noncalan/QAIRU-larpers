"""Схема отчёта о сопоставлении вопросов, его таблицы и сводка для командной строки."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from evals.precision import LabelerInfo, Mode, _pct
from evals.precision_labeler import LabelCall, Labeler


class Category(StrEnum):
    REQUIREMENT = "требование"
    IMPLEMENTATION = "реализация"


class MatchLabel(StrEnum):
    MATCH = "match"
    PARTIAL = "partial"
    NONE = "none"

    @property
    def label(self) -> str:
        return {"match": "совпало", "partial": "частично", "none": "нет"}[self.value]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AgentQuestion(FrozenModel):
    id: str
    question: str


class KickoffQuestion(FrozenModel):
    id: str
    time: str
    category: Category
    question: str
    sections: str
    transcript: str
    note: str


class Match(FrozenModel):
    label: MatchLabel
    b_ids: list[str]
    reason: str


Labels = dict[Labeler, dict[str, Match]]


class Score(FrozenModel):
    n_labeled: int
    counts: dict[MatchLabel, int]
    strict: float | None
    soft: float | None


class Slice(FrozenModel):
    per_judge: dict[Labeler, Score]
    consensus: Score


class Agreement(FrozenModel):
    n_both: int
    share: float | None
    kappa3: float | None
    kappa2: float | None


class Coverage(FrozenModel):
    ids: list[str]
    count: int
    share: float


class ReverseCoverage(FrozenModel):
    per_judge: dict[Labeler, Coverage]
    both: Coverage | None


class KickoffSettings(FrozenModel):
    labelers: list[LabelerInfo]
    mode: Mode
    document: str
    document_sha256: str
    questions_file: str
    questions_sha256: str
    run: str
    agent_version: str
    n_kickoff: int
    n_agent: int


class KickoffResult(Slice):
    schema_version: Literal["1"] = "1"
    created_at: datetime
    settings: KickoffSettings
    kickoff_questions: list[KickoffQuestion]
    agent_questions: list[AgentQuestion]
    labels: Labels
    calls: list[LabelCall]
    by_category: dict[Category, Slice]
    agreement: Agreement
    reverse_coverage: ReverseCoverage
    warnings: list[str]
    notes: list[str]


def summary(result: KickoffResult) -> str:
    scores = [(lab.display, s) for lab, s in result.per_judge.items()]
    scores.append(("Оба", result.consensus))
    parts = [f"{name} {_pct(s.strict)} / {_pct(s.soft)}" for name, s in scores]
    return f"Совпадения (строгая / мягкая): {', '.join(parts)}; согласие {_pct(result.agreement.share)}"


def _row(*cells: object) -> str:
    escaped = [" ".join(str(cell).splitlines()).replace("|", r"\|") for cell in cells]
    return "| " + " | ".join(escaped) + " |"


def _table(headers: list[str], rows: Iterable[str]) -> list[str]:
    return [_row(*headers), _row(*(["---"] * len(headers))), *rows]


def _score_row(name: str, s: Score) -> str:
    return _row(
        name,
        s.n_labeled,
        s.counts[MatchLabel.MATCH],
        s.counts[MatchLabel.PARTIAL],
        s.counts[MatchLabel.NONE],
        _pct(s.strict),
        _pct(s.soft),
    )


def _scores(metrics: Slice) -> list[str]:
    rows = [_score_row(lab.display, s) for lab, s in metrics.per_judge.items()]
    rows.append(_score_row("Оба (согласие)", metrics.consensus))
    return _table(
        [
            "Судья",
            "Размечено",
            "Совпало",
            "Частично",
            "Нет",
            "Доля совпадений строгая",
            "Доля совпадений мягкая",
        ],
        rows,
    )


def _mark(match: Match | None) -> str:
    if match is None:
        return "—"
    return ", ".join([match.label.label, *match.b_ids])


def _questions(result: KickoffResult) -> list[str]:
    rows = []
    for question in result.kickoff_questions:
        opus, fable = (result.labels.get(lab, {}).get(question.id) for lab in Labeler)
        agree = "—" if opus is None or fable is None else ("да" if opus.label == fable.label else "нет")
        rows.append(
            _row(
                question.id,
                question.category,
                question.question,
                _mark(opus),
                _mark(fable),
                agree,
                opus.reason if opus else "—",
                fable.reason if fable else "—",
            )
        )
    return _table(
        [
            "ID",
            "Категория",
            "Вопрос kick-off",
            Labeler.OPUS.display,
            Labeler.FABLE.display,
            "Согласны",
            "Причина Opus",
            "Причина Fable",
        ],
        rows,
    )


def _reverse(result: KickoffResult) -> list[str]:
    texts = {q.id: q.question for q in result.agent_questions}
    groups: list[tuple[str, Coverage | None]] = [
        (lab.display, cov) for lab, cov in result.reverse_coverage.per_judge.items()
    ]
    groups.append(("Оба судьи", result.reverse_coverage.both))
    lines = []
    for name, cov in groups:
        lines.extend([f"### {name}", ""])
        if cov is None:
            lines.extend(["Недостаточно разметки.", ""])
            continue
        total = result.settings.n_agent
        lines.extend([f"Упомянуто вопросов агента: {cov.count} из {total} ({_pct(cov.share)}).", ""])
        if cov.ids:
            lines.extend(_table(["ID", "Вопрос агента"], (_row(key, texts[key]) for key in cov.ids)))
            lines.append("")
    return lines


def _kappa(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}".replace(".", ",")


def render_markdown(result: KickoffResult) -> str:
    s = result.settings
    models = ", ".join(f"{m.labeler.display} (`{m.model}`, effort `{m.effort}`)" for m in s.labelers)
    lines = [
        "# Вопросы kick-off и вопросы агента",
        "",
        "Сравниваем вопросы участников встречи с итоговыми вопросами агента к автору того же ТЗ. "
        "Это сопоставление моделями, не людьми. Каждый судья получает один и тот же запрос.",
        "",
        f"- Вопросы встречи ({s.n_kickoff}): `{s.questions_file}`, SHA-256 `{s.questions_sha256}`.",
        f"- ТЗ: `{s.document}`, SHA-256 `{s.document_sha256}`.",
        f"- Прогон ({s.n_agent} вопросов): `{s.run}`, версия агента `{s.agent_version}`.",
        f"- Судьи: {models}. Режим: `{s.mode}`.",
        "",
        "## Сводка",
        "",
        *_scores(result),
        "",
        "Строгая доля учитывает только «совпало», мягкая также учитывает «частично». "
        "В строке «Оба» в знаменателе вопросы, размеченные обоими. Строгое совпадение требует "
        "двух меток «совпало», мягкое — чтобы оба нашли хотя бы частичное совпадение.",
        "",
        "## По категориям",
        "",
    ]
    for category, metrics in result.by_category.items():
        lines.extend([f"### {category}", "", *_scores(metrics), ""])
    a = result.agreement
    lines.extend(
        [
            "## Согласие судей",
            "",
            f"Размечено обоими: {a.n_both}. Доля одинаковых меток: {_pct(a.share)}. "
            f"κ (3 метки): {_kappa(a.kappa3)}. κ (да/нет): {_kappa(a.kappa2)}.",
            "",
            "## По вопросам",
            "",
            *_questions(result),
            "",
            "## Обратное покрытие",
            "",
            *_reverse(result),
        ]
    )
    for title, items in (("Предупреждения", result.warnings), ("Примечания", result.notes)):
        if items:
            lines.extend([f"## {title}", "", *(f"- {item}" for item in items), ""])
    return "\n".join(lines)


def write_result(result: KickoffResult, out_stem: Path) -> tuple[Path, Path]:
    out_stem.parent.mkdir(parents=True, exist_ok=True)
    json_path, md_path = (Path(f"{out_stem}.{ext}") for ext in ("json", "md"))
    json_path.write_text(result.model_dump_json(indent=1) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path
