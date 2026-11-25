"""Eval агента и базовой линии по эталону: ``python -m evals.eval ПАПКА [ПАПКА ...] --name ИМЯ``.

Все папки одного вызова это прогоны одной конфигурации (например, три прогона «агент, 1 проход»):
по ним считаются средние, объединение, pass^k критичных и Жаккар между прогонами. Результат:
``evals/results/<дата>-<имя>.json`` и ``.md``. Подробности: ``evals/README.md``.

Судья (``--judge``):

- ``auto`` (по умолчанию): прогону на бэкенде ``fake`` отвечает судья-фикстура, остальным кеш, а при
  промахе живой вызов ``claude -p`` (Opus 5.5 ``high``, несколько минут, тратит лимит подписки);
- ``cache``: только кеш, без сети; промах это ошибка;
- ``cli``: кеш, при промахе живой вызов;
- ``fake``: фикстуры ``evals/fixtures/llm/judge/<номер прогона>.json``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from functools import partial
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evals.findings import JudgeItem, render_items
from evals.gold import GOLD_DOCUMENT, GOLD_MD, Gold, load_gold, read_gold_md, sha256_text
from evals.judge import (
    DEFAULT_CACHE_DIR,
    FREE_TEXT_PREAMBLE,
    JUDGE_EFFORT,
    JUDGE_MODEL,
    EvalError,
    Judge,
    VerdictCache,
    display_path,
    judge_prompt,
    parse_verdict,
    read_utf8,
)
from evals.metrics import ConfigScore, RunScore, aggregate, count_findings, score_run
from evals.overrides import DEFAULT_OVERRIDES, Overrides, apply_overrides, load_overrides
from evals.report import render_markdown
from evals.runs import RunData, Source, load_run
from validator.llm import ClaudeCLI, FakeLLM
from validator.models import Backend

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_RESULTS = EVALS_DIR / "results"
DEFAULT_FIXTURES = EVALS_DIR / "fixtures" / "llm"
ASTANA = timezone(timedelta(hours=5))
"""Даты в именах результатов по времени Астаны: сроки хакатона в нём (CLAUDE.md)."""
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SCHEMA_VERSION = "1"


class JudgeMode(StrEnum):
    AUTO = "auto"
    CLI = "cli"
    CACHE = "cache"
    FAKE = "fake"


class JudgeFormat(StrEnum):
    UNIFORM = "uniform"  # протокол: одинаковый пронумерованный список для агента и базовой линии
    RESEARCH21 = "research21"  # как исследование 21: свободный текст целиком, судья нумерует сам


class EvalSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    judge_model: str = JUDGE_MODEL
    judge_effort: str = JUDGE_EFFORT.value
    judge_mode: JudgeMode
    judge_format: JudgeFormat
    gold_version: str
    gold_sha256: str
    gold_total: int
    gold_critical: list[int]
    document: str
    document_sha256: str


class EvalResult(BaseModel):
    """Содержимое ``evals/results/<дата>-<имя>.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    name: str
    created_at: datetime
    settings: EvalSettings
    config: ConfigScore
    runs: list[RunScore]
    notes: list[str] = Field(default_factory=list)


def _block(run: RunData, fmt: JudgeFormat) -> tuple[str, list[JudgeItem] | None]:
    """Блок находок для судьи и список, по номерам которого он отвечает.

    ``None``: судье ушёл свободный текст (research21), находки он нумерует сам. Пустой список это
    прогон без находок: номера в ответе судьи тогда вне списка и отбрасываются.
    """
    if fmt is JudgeFormat.RESEARCH21 and run.source is Source.BASELINE and run.free_text is not None:
        return FREE_TEXT_PREAMBLE + run.free_text, None
    return render_items(run.items), list(run.items)


class JudgePicker:
    """Судья для прогона по режиму: фикстуры, только кеш или кеш плюс живой вызов."""

    def __init__(self, mode: JudgeMode, cache_dir: Path, fixtures: Path) -> None:
        self.mode = mode
        cache = VerdictCache(cache_dir)
        self.fake = Judge(partial(FakeLLM, fixtures), None)
        self.offline = Judge(None, cache)
        self.live = Judge(ClaudeCLI, cache)

    def for_run(self, run: RunData) -> Judge:
        if self.mode is JudgeMode.FAKE or (self.mode is JudgeMode.AUTO and run.backend is Backend.FAKE):
            return self.fake
        if self.mode is JudgeMode.CACHE:
            return self.offline
        return self.live


def _score(
    index: int,
    run: RunData,
    *,
    gold: Gold,
    judge: Judge,
    prompt_parts: tuple[str, str],
    fmt: JudgeFormat,
    overrides: Overrides,
) -> RunScore:
    block, items = _block(run, fmt)
    document, gold_md = prompt_parts
    n_items = None if items is None else len(items)
    meta = {"run": run.label, "source": run.source.value, "items": n_items, "format": fmt.value}
    structured, call = judge.ask(judge_prompt(document, gold_md, block), index, meta)
    verdict = parse_verdict(structured, gold.ids, n_items)
    verdict, applied = apply_overrides(verdict, run.label, items, overrides)
    return score_run(
        run,
        verdict,
        gold,
        call,
        findings=count_findings(verdict, items),
        applied=applied,
        judge_numbered=items is None,
    )


def evaluate(
    runs: Sequence[Path],
    *,
    name: str,
    judge_mode: JudgeMode = JudgeMode.AUTO,
    judge_format: JudgeFormat = JudgeFormat.UNIFORM,
    document: Path = GOLD_DOCUMENT,
    gold_md_path: Path = GOLD_MD,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    fixtures: Path = DEFAULT_FIXTURES,
    overrides_path: Path | None = DEFAULT_OVERRIDES,
    now: datetime | None = None,
) -> EvalResult:
    if not NAME_PATTERN.match(name):
        raise EvalError(f"Имя «{name}» не подходит: латиница, цифры, точка, дефис, подчёркивание")
    if not runs:
        raise EvalError("Нужна хотя бы одна папка прогона")
    gold = load_gold()
    gold_md = read_gold_md(gold_md_path)
    if not document.is_file():
        raise EvalError(f"Документ не найден: {document}")
    document_text = read_utf8(document, "Документ для судьи (нужен текст, как в эталоне: MD или TXT)")
    overrides = load_overrides(overrides_path)
    loaded = [load_run(p, document) for p in runs]
    picker = JudgePicker(judge_mode, cache_dir, fixtures)
    scores = []
    for index, run in enumerate(loaded, start=1):
        print(f"[eval] {run.label}: {run.source.label}, находок {len(run.items)}, судья…", file=sys.stderr)
        scores.append(
            _score(
                index,
                run,
                gold=gold,
                judge=picker.for_run(run),
                prompt_parts=(document_text, gold_md),
                fmt=judge_format,
                overrides=overrides,
            )
        )
    notes = []
    if len({r.source for r in loaded}) > 1:
        notes.append("в одной конфигурации смешаны агент и базовая линия: средние считать нельзя")
    return EvalResult(
        name=name,
        created_at=(now or datetime.now(ASTANA)).astimezone(ASTANA),
        settings=EvalSettings(
            judge_mode=judge_mode,
            judge_format=judge_format,
            gold_version=gold.version,
            gold_sha256=sha256_text(gold_md),
            gold_total=len(gold.ids),
            gold_critical=gold.critical_ids,
            document=display_path(document),
            document_sha256=sha256_text(document_text),
        ),
        config=aggregate(scores, gold),
        runs=scores,
        notes=notes,
    )


def write_result(result: EvalResult, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.created_at:%Y-%m-%d}-{result.name}"
    if (out_dir / f"{stem}.json").exists() or (out_dir / f"{stem}.md").exists():
        stem += f"-{result.created_at:%H%M%S}"  # не затираем прошлый результат с тем же именем
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(result.model_dump_json(indent=1) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.eval",
        description="Eval прогонов агента или базовой линии по эталону docs/gold (issue #24).",
    )
    p.add_argument("runs", nargs="+", type=Path, help="папки прогонов одной конфигурации")
    p.add_argument("--name", required=True, help="имя конфигурации: baseline, agent-1pass, smoke …")
    p.add_argument("--judge", type=JudgeMode, choices=list(JudgeMode), default=JudgeMode.AUTO)
    p.add_argument("--judge-format", type=JudgeFormat, choices=list(JudgeFormat), default=JudgeFormat.UNIFORM)
    p.add_argument("--document", type=Path, default=GOLD_DOCUMENT, help="документ эталона")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS)
    p.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    p.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="ответы судьи для --judge fake")
    p.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES)
    return p


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{100 * value:.1f} %".replace(".", ",")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = evaluate(
            args.runs,
            name=args.name,
            judge_mode=args.judge,
            judge_format=args.judge_format,
            document=args.document,
            cache_dir=args.cache_dir,
            fixtures=args.fixtures,
            overrides_path=args.overrides,
        )
        json_path, md_path = write_result(result, args.out_dir)
    except EvalError as exc:
        print(f"Ошибка eval: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Ошибка eval: файл недоступен: {exc}", file=sys.stderr)
        return 1
    c = result.config
    print(
        f"Eval «{result.name}»: прогонов {c.runs}, recall {_pct(c.recall)} "
        f"(строго {_pct(c.recall_strict)}), критичные {_pct(c.critical_recall)}, "
        f"цитаты дословно {_pct(c.quotes_exact)}, шум {_pct(c.noise_share)}"
    )
    print(f"Результаты: {display_path(json_path)}, {display_path(md_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
