"""Прогоны для eval: папка агента (``report.json``) или папка базовой линии (``answer.md``).

Агент: находки после верификатора из ``report.json``, лог вызовов ``run/calls.jsonl`` рядом.
Базовая линия: сырой ответ модели в ``answer.md`` (или ``answer.txt``), лог ``calls.jsonl``
в той же папке или в ``run/``. Формат папок описан в ``evals/README.md``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.findings import JudgeItem, agent_items, free_items
from evals.freetext import check_quote, extract_quotes, split_findings
from evals.judge import EvalError, display_path, read_utf8
from evals.questions import QuestionScore, score_questions
from validator.errors import InputError
from validator.ingest import parse
from validator.models import (
    Backend,
    CallRecord,
    CallStatus,
    DocKind,
    Finding,
    ParsedDocument,
    QuoteMatch,
    Report,
)
from validator.text import locate_quote, stable_key

REPO_ROOT = Path(__file__).resolve().parents[1]
ANSWER_NAMES = ("answer.md", "answer.txt")
REPORT_NAME = "report.json"
CALLS_NAME = "calls.jsonl"


class Source(StrEnum):
    AGENT = "agent"
    BASELINE = "baseline"

    @property
    def label(self) -> str:
        return {"agent": "агент", "baseline": "базовая линия"}[self.value]


class QuoteStats(BaseModel):
    """Сверка цитат: сколько точно, нечётко и не найдено (``locate_quote``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = 0
    exact: int = 0
    fuzzy: int = 0
    not_found: int = 0

    @classmethod
    def from_matches(cls, matches: Iterable[QuoteMatch | None]) -> QuoteStats:
        checked = [m for m in matches if m is not None]
        return cls(
            total=len(checked),
            exact=checked.count(QuoteMatch.EXACT),
            fuzzy=checked.count(QuoteMatch.FUZZY),
            not_found=checked.count(QuoteMatch.NOT_FOUND),
        )

    @property
    def share_exact(self) -> float | None:
        return self.exact / self.total if self.total else None

    @property
    def share_verified(self) -> float | None:
        """Доля сверенных: найдены точно или нечётко."""
        return (self.exact + self.fuzzy) / self.total if self.total else None


FAILED_STATUSES = frozenset({CallStatus.ERROR, CallStatus.RETRIED})
REUSED_STATUSES = frozenset({CallStatus.CACHED, CallStatus.REPLAYED})


class CallStats(BaseModel):
    """Время и цена из ``calls.jsonl`` (одна строка на попытку вызова).

    Цена это тариф API за успешные ответы, в том числе взятые из кеша или replay: так она равна цене
    живого прогона той же конфигурации. Время таких ответов не живое, поэтому они считаются отдельно.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    calls: int = Field(default=0, description="Успешных вызовов: живых, из кеша и replay")
    failed: int = Field(default=0, description="Неудачных попыток: ошибка или повтор после сбоя")
    reused: int = Field(default=0, description="Ответов из кеша или replay: их время не живое")
    cost_usd: float | None = None
    cost_missing: int = Field(default=0, description="Успешных вызовов без цены в логе")
    model_s: float = Field(default=0.0, description="Сумма длительностей всех попыток")
    wall_s: float | None = Field(default=None, description="Время прогона по часам")

    @classmethod
    def from_records(cls, records: Sequence[CallRecord], wall_s: float | None) -> CallStats:
        ok = [r for r in records if r.status not in FAILED_STATUSES]
        costs = [r.cost_usd for r in ok if r.cost_usd is not None]
        model_s = sum((r.duration_ms or 0) for r in records) / 1000
        return cls(
            calls=len(ok),
            failed=len(records) - len(ok),
            reused=sum(r.status in REUSED_STATUSES for r in ok),
            cost_usd=round(sum(costs), 6) if costs else None,
            cost_missing=len(ok) - len(costs),
            model_s=round(model_s, 3),
            wall_s=wall_s if wall_s is not None else round(model_s, 3),
        )

    def notes(self) -> list[str]:
        if not self.reused:
            return []
        return [f"ответов из кеша или replay: {self.reused} из {self.calls}, время прогона не живое"]


class RunData(BaseModel):
    """Всё, что eval берёт из одного прогона, до судьи."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(description="Папка прогона, как её передали eval (POSIX)")
    source: Source
    items: list[JudgeItem]
    free_text: str | None = Field(default=None, description="Сырой ответ базовой линии")
    quotes: QuoteStats = Field(description="Все цитаты модели до фильтра верификатора")
    quotes_final: QuoteStats | None = Field(default=None, description="Цитаты итоговых находок, заново")
    calls: CallStats
    keys: list[str] | None = Field(default=None, description="stable_key требования и свойство, агент")
    backend: Backend | None = None
    agent_version: str | None = None
    document: str | None = Field(default=None, description="Документ, по которому сверялись цитаты")
    layout: str | None = Field(default=None, description="Как нарезан свободный текст")
    questions: QuestionScore | None = Field(default=None, description="Вопросы автору, только агент")
    notes: list[str] = Field(default_factory=list)


def run_label(path: Path) -> str:
    """Метка прогона для результатов и ``overrides.yaml``: путь от корня репозитория, если возможно."""
    return display_path(path)


def _read_calls(paths: Sequence[Path], notes: list[str]) -> list[CallRecord]:
    path = next((p for p in paths if p.is_file()), None)
    if path is None:
        notes.append("лог вызовов calls.jsonl не найден: время и цена неизвестны")
        return []
    records: list[CallRecord] = []
    for n, line in enumerate(read_utf8(path, "Лог вызовов").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(CallRecord.model_validate_json(line))
        except ValidationError:
            notes.append(f"{path.name}, строка {n}: не запись вызова, пропущена")
    return records


_DOC_KINDS = {
    ".md": DocKind.MD,
    ".markdown": DocKind.MD,
    ".txt": DocKind.MD,
    ".pdf": DocKind.PDF,
    ".docx": DocKind.DOCX,
}


def load_document(path: Path) -> ParsedDocument:
    """Документ тем же разбором, что у агента: цитаты сверяются с тем же каноническим текстом."""
    kind = _DOC_KINDS.get(path.suffix.lower())
    if kind is None or not path.is_file():
        raise EvalError(f"Документ для сверки цитат не найден или формат не поддерживается: {path}")
    try:
        return parse(path, kind)
    except InputError as exc:
        raise EvalError(f"Документ для сверки цитат не читается: {path}: {exc.message}") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _report_document(report: Report, fallback: Path, notes: list[str]) -> Path:
    """Документ прогона агента, если он на месте и не менялся, иначе ``fallback``."""
    info = report.document
    if info is None:
        notes.append(f"в report.json нет документа: цитаты сверяются с {fallback}")
        return fallback
    for candidate in (Path(info.path), REPO_ROOT / info.path):
        if candidate.is_file() and (info.sha256 is None or _sha256(candidate) == info.sha256):
            return candidate
    notes.append(f"документ прогона {info.path} не найден или изменился: цитаты сверяются с {fallback}")
    return fallback


def finding_keys(report: Report) -> list[str]:
    """Ключи для Жаккара: ``stable_key`` требования и код свойства (находка набора: раздел или цитата)."""
    requirements = (
        {r.id: r.stable_key for r in report.segmentation.requirements} if report.segmentation else {}
    )
    findings = report.verification.findings if report.verification else []
    keys: set[str] = set()
    for f in findings:
        anchors = [requirements[r] for r in f.requirement_ids if r in requirements]
        if not anchors:
            anchors = [_set_anchor(f)]
        keys.update(f"{a}|{f.property.value}" for a in anchors)
    return sorted(keys)


def _set_anchor(f: Finding) -> str:
    if f.section_path:
        return "section:" + stable_key(" / ".join(f.section_path))
    if f.quotes:
        return "quote:" + stable_key(f.quotes[0].located or f.quotes[0].text)
    return "text:" + stable_key(f.explanation)


def _load_agent(report_path: Path, label: str, fallback_document: Path) -> RunData:
    notes: list[str] = []
    try:
        report = Report.model_validate_json(read_utf8(report_path, "Отчёт агента"))
    except (ValidationError, ValueError) as exc:
        raise EvalError(f"{report_path} не отчёт агента (report.json): {exc.__class__.__name__}") from exc
    findings = report.verification.findings if report.verification else []
    if report.verification is None:
        notes.append(f"проверка не выполнена (статус {report.status.value}): находок нет")
    summary = report.verification.summary if report.verification else None
    quotes = QuoteStats(
        total=summary.quotes_total if summary else 0,
        exact=summary.quotes_exact if summary else 0,
        fuzzy=summary.quotes_fuzzy if summary else 0,
        not_found=summary.quotes_not_found if summary else 0,
    )
    doc_path = _report_document(report, fallback_document, notes)
    doc = load_document(doc_path)
    final = QuoteStats.from_matches(locate_quote(doc, q.text).match for f in findings for q in f.quotes)
    records = _read_calls([report_path.parent / "run" / CALLS_NAME, report_path.parent / CALLS_NAME], notes)
    calls = CallStats.from_records(records, report.passport.duration_s)
    notes.extend(calls.notes())
    return RunData(
        label=label,
        source=Source.AGENT,
        items=agent_items(findings),
        quotes=quotes,
        quotes_final=final,
        calls=calls,
        keys=finding_keys(report),
        backend=report.passport.backend_used,
        agent_version=report.passport.agent_version,
        document=run_label(doc_path),
        questions=score_questions(report, label),
        notes=notes,
    )


def _load_baseline(answer_path: Path, label: str, document: Path) -> RunData:
    notes: list[str] = []
    text = read_utf8(answer_path, "Ответ базовой линии")
    split = split_findings(text)
    notes.extend(split.notes)
    if not split.findings:
        notes.append("в ответе не найдено ни одной находки с цитатой")
    doc = load_document(document)
    quotes = QuoteStats.from_matches(check_quote(doc, q) for q in extract_quotes(text))
    folder = answer_path.parent
    records = _read_calls([folder / CALLS_NAME, folder / "run" / CALLS_NAME], notes)
    calls = CallStats.from_records(records, None)
    notes.extend(calls.notes())
    return RunData(
        label=label,
        source=Source.BASELINE,
        items=free_items(split.findings),
        free_text=text,
        quotes=quotes,
        quotes_final=quotes,
        calls=calls,
        backend=records[0].backend if records else None,
        document=run_label(document),
        layout=split.layout,
        notes=notes,
    )


def _find_report(folder: Path) -> Path | None:
    if (folder / REPORT_NAME).is_file():
        return folder / REPORT_NAME
    for candidate in sorted(folder.glob("*.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and "passport" in data and "conclusion" in data:
            return candidate
    return None


def load_run(path: Path, document: Path) -> RunData:
    """Прогон по пути: папка или файл. ``document``: с чем сверять цитаты, если прогон не знает сам."""
    label = run_label(path)
    if path.is_file():
        if path.suffix.lower() == ".json":
            return _load_agent(path, label, document)
        return _load_baseline(path, label, document)
    if not path.is_dir():
        raise EvalError(f"Прогон не найден: {path}")
    report = _find_report(path)
    if report is not None:
        return _load_agent(report, label, document)
    answer = next((path / n for n in ANSWER_NAMES if (path / n).is_file()), None)
    if answer is not None:
        return _load_baseline(answer, label, document)
    raise EvalError(f"В папке {path} нет ни report.json агента, ни answer.md базовой линии")
