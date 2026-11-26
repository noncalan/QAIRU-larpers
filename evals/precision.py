"""Precision по независимой разметке двумя моделями вслепую (#49).

``python -m evals.precision label ПАПКА [ПАПКА ...] --name ИМЯ``

Находки всех прогонов (агент и базовая линия вперемешку) режутся на пачки, в каждую подмешиваются
контрольные ложные находки (``evals.precision_items``). Два разметчика, Opus 5.5 и Fable 5.2 (оба
``high``), независимо ставят каждой находке «настоящий», «спорно» или «ложный» с причиной одной
строкой (``evals.precision_labeler``). Разметчики не видят эталон, вердикты судьи, разметку друг
друга и не знают, кто писал находки. Метрики: ``evals.precision_metrics``.

Для сравнения рядом шум по судье из #24 (он видит эталон): только из кеша вердиктов, если eval
этих прогонов уже был. Результат: ``evals/results/<дата>-precision-<имя>.json`` и ``.md``.
Подробности: ``evals/README.md``, раздел «Precision (#49)».
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evals.eval import ASTANA, DEFAULT_RESULTS, NAME_PATTERN
from evals.gold import GOLD_DOCUMENT, sha256_text
from evals.judge import DEFAULT_CACHE_DIR as JUDGE_CACHE_DIR
from evals.judge import (
    EvalError,
    VerdictCache,
    display_path,
    read_utf8,
)
from evals.precision_items import (
    CONTROL_SHARE,
    DEFAULT_BATCH,
    DEFAULT_SEED,
    Batch,
    ControlKind,
    LabelItem,
    items_for_run,
    make_batches,
)
from evals.precision_judge import JudgeCompare, judge_compare
from evals.precision_labeler import (
    DEFAULT_CACHE_DIR,
    ItemLabel,
    Label,
    LabelCall,
    Labeler,
    LabelerRunner,
    label_request,
    parse_labels,
)
from evals.precision_metrics import (
    ControlScore,
    Slice,
    control_score,
    make_slice,
    real_items,
    slices_by,
)
from evals.runs import RunData, Source, _find_report, load_document, load_run
from validator.llm import ClaudeCLI
from validator.llm.base import LLMClient
from validator.models import Finding, ParsedDocument, Report, Severity

SCHEMA_VERSION = "1"
ClientFactory = Callable[[Labeler], LLMClient]


class Mode(StrEnum):
    CLI = "cli"  # кеш, при промахе живой вызов claude -p
    CACHE = "cache"  # только кеш, без сети


class LabelerInfo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labeler: Labeler
    model: str
    effort: str


class PrecisionSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    labelers: list[LabelerInfo]
    mode: Mode
    seed: int
    batch_size: int
    control_share: float
    document: str
    document_sha256: str
    runs: list[str]


class PrecisionResult(BaseModel):
    """Содержимое ``evals/results/<дата>-precision-<имя>.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    name: str
    created_at: datetime
    settings: PrecisionSettings
    items: list[LabelItem] = Field(description="Настоящие находки пула и контрольные")
    batches: list[Batch]
    labels: dict[Labeler, dict[str, ItemLabel]]
    calls: list[LabelCall]
    overall: Slice
    by_run: list[Slice]
    by_severity: list[Slice]
    by_finder: list[Slice]
    by_passes: list[Slice]
    controls: dict[Labeler, ControlScore]
    controls_by_kind: dict[ControlKind, dict[Labeler, ControlScore]]
    judge: list[JudgeCompare]
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _agent_findings(path: Path) -> list[Finding]:
    report_path = path if path.is_file() else _find_report(path)
    if report_path is None:
        return []
    report = Report.model_validate_json(read_utf8(report_path, "Отчёт агента"))
    return list(report.verification.findings) if report.verification else []


def _pool(
    runs: Sequence[tuple[Path, RunData]], doc: ParsedDocument, document: str, notes: list[str]
) -> list[LabelItem]:
    pool: list[LabelItem] = []
    for path, run in runs:
        findings = _agent_findings(path) if run.source is Source.AGENT else None
        own = run.document in (None, document)
        if run.source is Source.AGENT and not own:
            notes.append(
                f"{run.label}: прогон шёл по {run.document}, разметка по {document}: место находки "
                "ищет код в документе разметки"
            )
        pool.extend(items_for_run(run, findings, doc, own_anchors=own))
    return pool


def _label_all(
    labelers: Sequence[Labeler],
    batches: Sequence[Batch],
    by_uid: dict[str, LabelItem],
    document: str,
    runner_for: Callable[[Labeler], LabelerRunner],
    warnings: list[str],
) -> tuple[dict[Labeler, dict[str, ItemLabel]], list[LabelCall]]:
    """Каждый разметчик в своём потоке (два вызова одновременно), пачки у разметчика по очереди."""

    def one(lab: Labeler) -> tuple[dict[str, ItemLabel], list[LabelCall], list[str]]:
        runner = runner_for(lab)
        got: dict[str, ItemLabel] = {}
        calls: list[LabelCall] = []
        warns: list[str] = []
        for batch in batches:
            items = [by_uid[u] for u in batch.uids]
            print(f"[precision] {lab.display}: пачка {batch.index}, находок {len(items)}…", file=sys.stderr)
            req = label_request(lab, document, items, batch.index)
            meta = {"labeler": lab.value, "batch": batch.index, "items": len(items)}
            structured, call = runner.ask(req, lab, batch.index, meta)
            calls.append(call)
            parsed = parse_labels(structured, len(items), warns, f"{lab.display}, пачка {batch.index}")
            got.update({batch.uids[n - 1]: mark for n, mark in parsed.items()})
        return got, calls, warns

    with ThreadPoolExecutor(max_workers=max(1, len(labelers))) as pool:
        done = list(pool.map(one, labelers))
    labels = {lab: got for lab, (got, _, _) in zip(labelers, done, strict=True)}
    for _, _, warns in done:
        warnings.extend(warns)
    return labels, [c for _, calls, _ in done for c in calls]


def _control_scores(
    controls: Sequence[LabelItem], marks: dict[Labeler, dict[str, Label]]
) -> tuple[dict[Labeler, ControlScore], dict[ControlKind, dict[Labeler, ControlScore]]]:
    overall = {lab: control_score([c.uid for c in controls], m) for lab, m in marks.items()}
    by_kind = {
        kind: {
            lab: control_score([c.uid for c in controls if c.control is kind], m) for lab, m in marks.items()
        }
        for kind in ControlKind
        if any(c.control is kind for c in controls)
    }
    return overall, by_kind


def _severity_order(name: str) -> tuple[int, str]:
    order = [s.label for s in sorted(Severity, key=lambda s: s.rank)]
    return (order.index(name) if name in order else len(order), name)


def _passes_order(name: str) -> tuple[int, str]:
    """«3 из 3» раньше «1 из 3»: сначала устойчивые находки."""
    head = name.split(maxsplit=1)[0]
    return (-int(head) if head.isdigit() else 0, name)


def _default_factory(labeler: Labeler) -> LLMClient:
    del labeler  # модель задаёт запрос, клиент один
    return ClaudeCLI()


def label(
    runs: Sequence[Path],
    *,
    name: str,
    mode: Mode = Mode.CLI,
    document: Path = GOLD_DOCUMENT,
    labelers: Sequence[Labeler] = (Labeler.OPUS, Labeler.FABLE),
    seed: int = DEFAULT_SEED,
    batch_size: int = DEFAULT_BATCH,
    control_share: float = CONTROL_SHARE,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    judge_cache_dir: Path = JUDGE_CACHE_DIR,
    client_factory: ClientFactory | None = None,
    now: datetime | None = None,
) -> PrecisionResult:
    if not NAME_PATTERN.match(name):
        raise EvalError(f"Имя «{name}» не подходит: латиница, цифры, точка, дефис, подчёркивание")
    if not runs:
        raise EvalError("Нужна хотя бы одна папка прогона")
    warnings: list[str] = []
    notes: list[str] = []
    doc = load_document(document)
    loaded = [(p, load_run(p, document)) for p in runs]
    seen = [run.label for _, run in loaded]
    if len(set(seen)) != len(seen):
        raise EvalError(f"Прогон указан дважды: {sorted({x for x in seen if seen.count(x) > 1})}")
    pool = _pool(loaded, doc, display_path(document), notes)
    if not pool:
        raise EvalError("В прогонах нет ни одной находки: размечать нечего")
    batches, controls = make_batches(pool, doc, seed=seed, batch_size=batch_size, control_share=control_share)
    items = pool + controls
    factory = client_factory or _default_factory
    cache = VerdictCache(cache_dir)

    def runner_for(lab: Labeler) -> LabelerRunner:
        return LabelerRunner(None if mode is Mode.CACHE else (lambda: factory(lab)), cache)

    labels, calls = _label_all(labelers, batches, {i.uid: i for i in items}, doc.text, runner_for, warnings)
    marks = {lab: {u: m.label for u, m in got.items()} for lab, got in labels.items()}
    overall, by_kind = _control_scores(controls, marks)
    real = real_items(items)
    return PrecisionResult(
        name=name,
        created_at=(now or datetime.now(ASTANA)).astimezone(ASTANA),
        settings=PrecisionSettings(
            labelers=[LabelerInfo(labeler=lab, model=lab.model, effort=lab.effort.value) for lab in labelers],
            mode=mode,
            seed=seed,
            batch_size=batch_size,
            control_share=control_share,
            document=display_path(document),
            document_sha256=sha256_text(doc.text),
            runs=[run.label for _, run in loaded],
        ),
        items=items,
        batches=batches,
        labels=labels,
        calls=calls,
        controls=overall,
        controls_by_kind=by_kind,
        judge=judge_compare([r for _, r in loaded], items, labels, notes, judge_cache_dir),
        warnings=warnings,
        notes=notes,
        overall=make_slice("все находки", [i.uid for i in real], marks),
        by_run=slices_by(real, lambda i: f"{i.origin.label}: {i.run}", marks),
        by_severity=slices_by(real, lambda i: i.severity, marks, _severity_order),
        by_finder=slices_by(real, lambda i: i.finder, marks, lambda name: (0, name)),
        by_passes=slices_by(real, lambda i: i.passes, marks, _passes_order),
    )


def write_result(result: PrecisionResult, out_dir: Path) -> tuple[Path, Path]:
    from evals.precision_report import render_markdown  # noqa: PLC0415 - отчёт импортирует этот модуль

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.created_at:%Y-%m-%d}-precision-{result.name}"
    if (out_dir / f"{stem}.json").exists() or (out_dir / f"{stem}.md").exists():
        stem += f"-{result.created_at:%H%M%S}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(result.model_dump_json(indent=1) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.precision",
        description="Precision: независимая разметка находок двумя моделями вслепую (issue #49).",
    )
    sub = p.add_subparsers(dest="command", required=True)
    lab = sub.add_parser("label", help="разметить находки прогонов и посчитать precision")
    lab.add_argument("runs", nargs="+", type=Path, help="папки прогонов агента и базовой линии")
    lab.add_argument("--name", required=True, help="имя разметки: v0.8, rc1 …")
    lab.add_argument("--mode", type=Mode, choices=list(Mode), default=Mode.CLI)
    lab.add_argument("--document", type=Path, default=GOLD_DOCUMENT, help="документ, по которому размечаем")
    lab.add_argument(
        "--labelers", default="opus,fable", help="разметчики через запятую: opus, fable (по умолчанию оба)"
    )
    lab.add_argument("--seed", type=int, default=DEFAULT_SEED)
    lab.add_argument("--batch-size", type=int, default=DEFAULT_BATCH, help="настоящих находок в пачке")
    lab.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS)
    lab.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    return p


def _labelers(text: str) -> list[Labeler]:
    try:
        out = [Labeler(part.strip()) for part in text.split(",") if part.strip()]
    except ValueError as exc:
        raise EvalError(f"Неизвестный разметчик в «{text}»: есть opus и fable") from exc
    if not out or len(set(out)) != len(out):
        raise EvalError(f"Разметчики «{text}»: нужен хотя бы один и без повторов")
    return out


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f} %".replace(".", ",")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = label(
            args.runs,
            name=args.name,
            mode=args.mode,
            document=args.document,
            labelers=_labelers(args.labelers),
            seed=args.seed,
            batch_size=args.batch_size,
            cache_dir=args.cache_dir,
        )
        json_path, md_path = write_result(result, args.out_dir)
    except EvalError as exc:
        print(f"Ошибка precision: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Ошибка precision: файл недоступен: {exc}", file=sys.stderr)
        return 1
    o = result.overall
    parts = [f"{lab.display} {_pct(p.strict)} / {_pct(p.soft)}" for lab, p in o.by_labeler.items()]
    print(f"Precision «{result.name}» (строгая / мягкая): {', '.join(parts)}; оба {_pct(o.consensus.strict)}")
    print(f"Результаты: {display_path(json_path)}, {display_path(md_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
