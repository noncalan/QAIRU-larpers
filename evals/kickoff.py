"""Сопоставление вопросов участников kick-off с вопросами агента двумя независимыми судьями (#48)."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Never

from pydantic import ValidationError

from evals.eval import ASTANA
from evals.gold import sha256_text
from evals.judge import EvalError, VerdictCache, display_path, read_utf8
from evals.kickoff_report import (
    AgentQuestion,
    Agreement,
    Category,
    Coverage,
    KickoffQuestion,
    KickoffResult,
    KickoffSettings,
    Labels,
    Match,
    MatchLabel,
    ReverseCoverage,
    Score,
    Slice,
    summary,
    write_result,
)
from evals.precision import ClientFactory, LabelerInfo, Mode, _default_factory, _labelers
from evals.precision_labeler import LABEL_TIMEOUT_S, LabelCall, Labeler, LabelerRunner
from evals.precision_metrics import kappa
from evals.runs import _find_report
from validator.models import LLMRequest, LLMStep, Report

__all__ = [
    "Category",
    "KickoffQuestion",
    "KickoffResult",
    "Match",
    "MatchLabel",
    "Mode",
    "agreement",
    "kickoff_prompt",
    "main",
    "make_slice",
    "match_questions",
    "parse_matches",
    "parse_questions",
    "reverse_coverage",
    "score",
    "write_result",
]

EVAL_DIR = Path(__file__).resolve().parent
DEFAULT_CACHE_DIR = EVAL_DIR / "cache" / "kickoff"
DEFAULT_QUESTIONS = EVAL_DIR / "holdout" / "kickoff_questions.md"
DEFAULT_RUN = EVAL_DIR / "holdout" / "task"
DEFAULT_DOCUMENT = EVAL_DIR.parent / "data" / "holdout" / "task" / "task.md"
DEFAULT_OUT = EVAL_DIR / "holdout" / "kickoff"
QUESTION_ID = re.compile(r"K-\d{2}")
COLUMN_COUNT = 7
REASON_CHARS = 400
BATCH = 1


KICKOFF_SYSTEM = (
    "Ты аккуратный аналитик требований. Ты сопоставляешь два списка вопросов к автору одного и того же ТЗ. "
    "Ты не автор ни одного из списков. Отвечай на русском языке."
)
KICKOFF_PROMPT = """Ниже ТЗ и два списка вопросов к его автору.
Список A: вопросы, которые участники хакатона задали организаторам на установочной встрече. Это расшифровка устной речи, формулировки приблизительные.
Список B: вопросы, которые ревьюер требований письменно задал автору ТЗ после чтения документа. У вопроса есть пояснение (why), цитаты из ТЗ (where) и варианты ответа (options).

Для КАЖДОГО вопроса из списка A поставь одну метку:
- match (совпало): в списке B есть вопрос о том же пробеле или той же неясности ТЗ, и ответ автора на него снял бы вопрос из A (или наоборот). Формулировки и уровень детализации могут сильно отличаться;
- partial (частично): в списке B есть вопрос о том же месте или той же теме ТЗ, но о другом аспекте, заметно уже или шире: ответ на него снимает вопрос из A лишь отчасти;
- none (нет): в списке B нет вопроса, ответ на который хоть отчасти отвечает на вопрос из A.

Для match и partial перечисли в b_ids номера вопросов из списка B, на которые опираешься; для none список пустой. Причину дай одной строкой, до 200 символов. Не оценивай, хороши ли сами вопросы, и не угадывай, что ответил бы автор: оценивай только совпадение того, о чём спрашивают. Ответь про все вопросы списка A. Работай только с этими данными."""  # noqa: E501

KICKOFF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string", "enum": [label.value for label in MatchLabel]},
                    "b_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": ["id", "label", "b_ids", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["matches"],
    "additionalProperties": False,
}


def parse_questions(text: str) -> list[KickoffQuestion]:
    """Читает семь ячеек только из строк таблицы с идентификатором вопроса."""
    questions: dict[str, KickoffQuestion] = {}
    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [s.strip() for s in line.strip().removeprefix("|").removesuffix("|").split("|")]
        if not QUESTION_ID.fullmatch(cells[0]):
            continue
        if len(cells) != COLUMN_COUNT:
            raise EvalError(f"{cells[0]}: ожидалось {COLUMN_COUNT} столбцов, получено {len(cells)}")
        key, time, category, question, sections, transcript, note = cells
        if key in questions:
            raise EvalError(f"Вопрос {key} указан дважды")
        if not question:
            raise EvalError(f"{key}: пустой вопрос")
        try:
            kind = Category(category)
        except ValueError as exc:
            raise EvalError(f"{key}: неизвестная категория «{category}»") from exc
        questions[key] = KickoffQuestion(
            id=key,
            time=time,
            category=kind,
            question=question,
            sections=sections,
            transcript=transcript,
            note=note,
        )
    if not questions:
        raise EvalError("В таблице нет вопросов с номерами K-NN")
    return list(questions.values())


def _load_report(run_dir: Path) -> Report:
    path = _find_report(run_dir)
    if path is None:
        raise EvalError(f"В папке {display_path(run_dir)} не найден отчёт агента")
    try:
        report = Report.model_validate_json(read_utf8(path, "Отчёт агента"))
    except ValidationError as exc:
        raise EvalError(f"Отчёт агента {display_path(path)} не соответствует схеме") from exc
    if not report.questions:
        raise EvalError("В отчёте агента нет вопросов")
    return report


def kickoff_prompt(document: str, questions: Sequence[KickoffQuestion], report: Report) -> str:
    kickoff = [{"id": q.id, "question": q.question} for q in questions]
    agent = [
        {
            "id": q.id,
            "question": q.question,
            "why": q.why,
            "where": [quote.text for quote in q.quotes],
            "options": [o.text for o in q.options],
        }
        for q in report.questions
    ]
    return (
        KICKOFF_PROMPT
        + "\n\n=== ТЗ ===\n"
        + document
        + "\n\n=== СПИСОК A ===\n"
        + json.dumps(kickoff, ensure_ascii=False, indent=1)
        + "\n\n=== СПИСОК B ===\n"
        + json.dumps(agent, ensure_ascii=False, indent=1)
    )


def _references(
    raw: object, label: MatchLabel, agent_ids: Sequence[str], warnings: list[str], who: str
) -> list[str]:
    if not isinstance(raw, list):
        warnings.append(f"{who}: b_ids не список")
        raw = []
    valid = []
    for key in raw:
        if isinstance(key, str) and key in agent_ids:
            valid.append(key)
        else:
            warnings.append(f"{who}: неизвестный номер B {key!r}, пропущен")
    if label is MatchLabel.NONE and raw:
        warnings.append(f"{who}: при метке «нет» ссылки на B отброшены")
        return []
    if label is not MatchLabel.NONE and not valid:
        warnings.append(f"{who}: метка «{label.label}» без ссылок на вопросы B")
    return valid


def parse_matches(
    structured: object, kickoff_ids: Sequence[str], agent_ids: Sequence[str], warnings: list[str], who: str
) -> dict[str, Match]:
    if not isinstance(structured, Mapping) or not isinstance(structured.get("matches"), list):
        raise EvalError(f"{who}: ответ не по схеме, нет списка matches")
    out: dict[str, Match] = {}
    for entry in structured["matches"]:
        if not isinstance(entry, Mapping):
            warnings.append(f"{who}: запись не объект, пропущена")
            continue
        key = entry.get("id")
        if not isinstance(key, str) or key not in kickoff_ids:
            warnings.append(f"{who}: неизвестный номер A {key!r}, пропущен")
            continue
        try:
            label = MatchLabel(str(entry.get("label")))
        except ValueError:
            warnings.append(f"{who}: {key}: неизвестная метка {entry.get('label')!r}, пропущена")
            continue
        if key in out:
            warnings.append(f"{who}: {key} размечен дважды, берём последнюю запись")
        refs = _references(entry.get("b_ids"), label, agent_ids, warnings, f"{who}: {key}")
        reason = " ".join(str(entry.get("reason") or "").split())[:REASON_CHARS]
        out[key] = Match(label=label, b_ids=refs, reason=reason)
    if missing := [key for key in kickoff_ids if key not in out]:
        warnings.append(f"{who}: без метки {', '.join(missing)}, в метрики не идут")
    return out


def score(marks: Iterable[MatchLabel]) -> Score:
    counts = Counter(marks)
    n = counts.total()
    return Score(
        n_labeled=n,
        counts={label: counts[label] for label in MatchLabel},
        strict=counts[MatchLabel.MATCH] / n if n else None,
        soft=(n - counts[MatchLabel.NONE]) / n if n else None,
    )


def _pairs(ids: Sequence[str], labels: Labels) -> list[tuple[MatchLabel, MatchLabel]]:
    a, b = (labels.get(lab, {}) for lab in Labeler)
    return [(a[key].label, b[key].label) for key in ids if key in a and key in b]


def _consensus(a: MatchLabel, b: MatchLabel) -> MatchLabel:
    if a == b:
        return a
    return MatchLabel.NONE if MatchLabel.NONE in (a, b) else MatchLabel.PARTIAL


def make_slice(ids: Sequence[str], labels: Labels) -> Slice:
    """Доли каждого судьи считаются по его ответам, совместные доли — по общим вопросам."""
    return Slice(
        per_judge={lab: score(row[k].label for k in ids if k in row) for lab, row in labels.items()},
        consensus=score(_consensus(a, b) for a, b in _pairs(ids, labels)),
    )


def agreement(ids: Sequence[str], labels: Labels) -> Agreement:
    pairs = _pairs(ids, labels)
    binary = {label: "no" if label is MatchLabel.NONE else "yes" for label in MatchLabel}
    return Agreement(
        n_both=len(pairs),
        share=sum(a == b for a, b in pairs) / len(pairs) if pairs else None,
        kappa3=kappa(pairs),
        kappa2=kappa([(binary[a], binary[b]) for a, b in pairs]),
    )


def reverse_coverage(agent_ids: Sequence[str], labels: Labels) -> ReverseCoverage:
    """Собирает ссылки на вопросы агента и сохраняет порядок вопросов в отчёте."""

    def coverage(refs: set[str]) -> Coverage:
        ids = list(dict.fromkeys(key for key in agent_ids if key in refs))
        return Coverage(ids=ids, count=len(ids), share=len(ids) / len(agent_ids))

    refs = {
        lab: {key for m in marks.values() if m.label is not MatchLabel.NONE for key in m.b_ids}
        for lab, marks in labels.items()
    }
    return ReverseCoverage(
        per_judge={lab: coverage(ids) for lab, ids in refs.items()},
        both=coverage(set.intersection(*refs.values())) if len(refs) == len(Labeler) else None,
    )


def _judge_all(
    prompt: str,
    kickoff_ids: Sequence[str],
    agent_ids: Sequence[str],
    labelers: Sequence[Labeler],
    factory: ClientFactory | None,
    cache: VerdictCache,
) -> tuple[Labels, list[LabelCall], list[str]]:
    def one(lab: Labeler) -> tuple[dict[str, Match], LabelCall, list[str]]:
        req = LLMRequest(
            step=LLMStep.JUDGE,
            call_index=BATCH,
            model=lab.model,
            effort=lab.effort,
            system=KICKOFF_SYSTEM,
            prompt=prompt,
            json_schema=KICKOFF_SCHEMA,
            timeout_s=LABEL_TIMEOUT_S,
        )
        runner = LabelerRunner(partial(factory, lab) if factory else None, cache)
        meta = {"labeler": lab.value, "kickoff": len(kickoff_ids), "agent": len(agent_ids)}
        structured, call = runner.ask(req, lab, BATCH, meta)
        warnings: list[str] = []
        return parse_matches(structured, kickoff_ids, agent_ids, warnings, lab.display), call, warnings

    with ThreadPoolExecutor(max_workers=len(labelers)) as pool:
        done = list(pool.map(one, labelers))
    labels = {lab: marks for lab, (marks, _, _) in zip(labelers, done, strict=True)}
    return labels, [call for _, call, _ in done], [w for _, _, warnings in done for w in warnings]


def match_questions(
    questions_path: Path,
    run_dir: Path,
    document_path: Path,
    *,
    mode: Mode = Mode.CLI,
    labelers: Sequence[Labeler] = tuple(Labeler),
    cache_dir: Path = DEFAULT_CACHE_DIR,
    client_factory: ClientFactory | None = None,
    now: datetime | None = None,
) -> KickoffResult:
    """Сопоставляет вопросы по ТЗ и возвращает разметку, её происхождение и метрики."""
    if not labelers or len(set(labelers)) != len(labelers):
        raise EvalError("Нужен хотя бы один судья, без повторов")
    mode = Mode(mode)
    text = read_utf8(questions_path, "Вопросы kick-off")
    questions = parse_questions(text)
    report = _load_report(run_dir)
    document = read_utf8(document_path, "ТЗ")
    ids = [q.id for q in questions]
    agent_ids = [q.id for q in report.questions]
    factory = None if mode is Mode.CACHE else client_factory or _default_factory
    labels, calls, warnings = _judge_all(
        kickoff_prompt(document, questions, report),
        ids,
        agent_ids,
        labelers,
        factory,
        VerdictCache(cache_dir),
    )
    settings = KickoffSettings(
        labelers=[LabelerInfo(labeler=lab, model=lab.model, effort=lab.effort) for lab in labelers],
        mode=mode,
        document=display_path(document_path),
        document_sha256=sha256_text(document),
        questions_file=display_path(questions_path),
        questions_sha256=sha256_text(text),
        run=display_path(run_dir),
        agent_version=report.passport.agent_version,
        n_kickoff=len(ids),
        n_agent=len(agent_ids),
    )
    overall = make_slice(ids, labels)
    return KickoffResult(
        created_at=(now or datetime.now(ASTANA)).astimezone(ASTANA),
        settings=settings,
        kickoff_questions=questions,
        agent_questions=[AgentQuestion(id=q.id, question=q.question) for q in report.questions],
        labels=labels,
        calls=calls,
        per_judge=overall.per_judge,
        consensus=overall.consensus,
        by_category={c: make_slice([q.id for q in questions if q.category is c], labels) for c in Category},
        agreement=agreement(ids, labels),
        reverse_coverage=reverse_coverage(agent_ids, labels),
        warnings=warnings,
        notes=[]
        if len(labelers) == len(Labeler)
        else ["Разметку выполнил один судья. Для оценки согласия нужны оба."],
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise EvalError(f"Некорректные аргументы: {message}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS, help="таблица вопросов встречи")
    parser.add_argument("--run", type=Path, default=DEFAULT_RUN, help="папка прогона агента")
    parser.add_argument("--document", type=Path, default=DEFAULT_DOCUMENT, help="текст ТЗ")
    parser.add_argument("--mode", type=Mode, choices=list(Mode), default=Mode.CLI)
    parser.add_argument("--labelers", default="opus,fable", help="судьи через запятую: opus, fable")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="путь к результату без расширения")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR, help="папка кеша судей")
    try:
        args = parser.parse_args(argv)
        result = match_questions(
            args.questions,
            args.run,
            args.document,
            mode=args.mode,
            labelers=_labelers(args.labelers),
            cache_dir=args.cache_dir,
        )
        json_path, md_path = write_result(result, args.out)
    except (EvalError, OSError) as exc:
        print(f"Ошибка kickoff: {exc}", file=sys.stderr)
        return 1
    print(summary(result))
    print(f"Результаты: {display_path(json_path)}, {display_path(md_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
