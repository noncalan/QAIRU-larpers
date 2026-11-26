"""Стенд прохода на пропуски (--gaps): дешёвые замеры без шагов после поиска.

Два режима.

``only DOC OUT [--manifest M.json]``
    Только проход на пропуски по документу (один вызов модели). «Уже найдено» это срабатывания линтера
    на запасной нарезке без модели. Для отладки промпта на чужих документах: печатает находки и, если дан
    манифест мутаций, какие мутации процитированы.

``paired DOC OUT --seed-run RUN [--no-gaps] [--passes K]``
    Конвейер агента до верификатора включительно (без свидетеля, попытки теста и вопросов: находки от них
    не зависят). Ответы нарезки и поиска берутся из записанного прогона ``RUN`` через кеш, поэтому
    сравнение «с проходом и без» парное: поиск один и тот же, живой только новый вызов. Пишет
    ``OUT/report.json`` в формате агента: его читают ``evals.eval`` и подсчёт мутаций.

Запуск из корня репозитория: ``uv run python experiments/50-gaps-pass/run_gaps.py ...``.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from validator import pipeline
from validator.config import load_config
from validator.errors import LLMError
from validator.gaps import find_gaps
from validator.ingest import guard, parse
from validator.lint import lint
from validator.llm import instrument, make_client
from validator.models import Backend, CandidateFinding, RunConfig
from validator.parts import plan_parts
from validator.segment import fallback_segmentation
from validator.text import locate_quote

MAX_BUDGET_USD = 5.0


def seed_cache(cache_dir: Path, run_dir: Path, steps: tuple[str, ...] = ("segment", "search")) -> int:
    """Кладёт записанные ответы шагов ``steps`` в кеш подписки: вызовы с тем же ключом не идут в модель."""
    copied = 0
    for path in sorted((run_dir / "responses").glob("*.json")):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if envelope.get("step") not in steps:
            continue
        key = envelope["key"]
        target = cache_dir / "cli" / key[:2] / f"{key}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied += 1
    return copied


def _config(doc: Path, out: Path, *, gaps: bool, passes: int | None = None) -> RunConfig:
    cfg = load_config(
        doc, out / "report.md", backend=Backend.CLI, max_budget_usd=MAX_BUDGET_USD, gaps=gaps, passes=passes
    )
    return cfg.model_copy(update={"cache_dir": (out / ".cache").resolve()})


def _skipped(*_args: Any, **_kwargs: Any) -> Any:
    raise LLMError("шаг пропущен стендом experiments/50-gaps-pass", code="LLM-000")


def paired(args: argparse.Namespace) -> int:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    cfg = _config(args.doc, out, gaps=not args.no_gaps, passes=args.passes)
    seeded = seed_cache(cfg.cache_dir, args.seed_run)
    # Шаги после слияния не меняют состав находок (критик выключен): стенд их пропускает, конвейер пишет
    # об этом предупреждения в отчёт и идёт дальше.
    pipeline.gather = _skipped  # type: ignore[assignment]
    pipeline.draft_tests = _skipped  # type: ignore[assignment]
    pipeline.build_questions = _skipped  # type: ignore[assignment]
    outcome = pipeline.run(cfg)
    calls = [json.loads(line) for line in (cfg.run_dir / "calls.jsonl").read_text("utf-8").splitlines()]
    live = [c for c in calls if c["status"] not in ("cached",)]
    print(f"ответов из записи в кеше: {seeded}; вызовы: {[(c['call_id'], c['status']) for c in calls]}")
    print(f"живых вызовов: {len(live)}, цена живых: ${sum(c.get('cost_usd') or 0 for c in live):.4f}")
    report = outcome.report
    if report is not None and report.verification is not None:
        found = report.verification.findings
        mine = [f for f in found if any(s.kind.value == "gaps" for s in f.sources)]
        print(f"находок: {len(found)}, из них от прохода на пропуски: {len(mine)}")
    return 0


def _show(c: CandidateFinding) -> str:
    quotes = " | ".join(f"«{q}»" for q in c.quotes)
    return f"{c.cid} [{c.property.value}/{c.defect_type.value}/{c.severity_hint}] {c.title}\n      {quotes}"


def only(args: argparse.Namespace) -> int:
    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    cfg = _config(args.doc, out, gaps=True)
    g = guard(args.doc, workdir=cfg.run_dir, ocr=False)
    doc = parse(args.doc, g.kind, pdf_backend=cfg.pdf_backend, ocr=False)
    plan = plan_parts(doc)
    client, _ = make_client(cfg)
    llm = instrument(cfg, client)
    known = lint(doc, fallback_segmentation(doc))
    outcome = find_gaps(doc, plan, known, llm, cfg)
    for d in outcome.diagnostics:
        print(f"! {d.code} {d.message} {d.action}")
    records = llm.records
    cost = sum(r.cost_usd or 0 for r in records)
    print(f"вызовов: {len(records)}, цена: ${cost:.4f}, статусы: {[r.status.value for r in records]}")
    print(f"находок прохода: {len(outcome.candidates)} (линтер дал {len(known)})")
    spans: dict[str, list[tuple[int, int]]] = {}
    for c in outcome.candidates:
        print("  " + _show(c))
        spans[c.cid] = []
        for q in c.quotes:
            found = locate_quote(doc, q, c.block_ids) if c.block_ids else locate_quote(doc, q)
            if found.verified and found.anchor is not None:
                spans[c.cid].append((found.anchor.char_start, found.anchor.char_end))
            else:
                print(f"      ! цитата не найдена: «{q}»")
    result: dict[str, Any] = {
        "doc": str(args.doc),
        "cost_usd": cost,
        "candidates": [c.model_dump(mode="json") for c in outcome.candidates],
        "mutations": [],
    }
    if args.manifest:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        for m in manifest["mutations"]:
            hits = [
                cid
                for cid, ss in spans.items()
                if any(a < t[1] and t[0] < b for a, b in ss for t in m["match_spans"])
            ]
            was, now = m["original"][:60], m["replacement"][:60]
            print(f"мутация {m['id']} {m['operator']} «{was}» → «{now}»: {hits}")
            result["mutations"].append({"id": m["id"], "operator": m["operator"], "hits": hits})
    (out / "gaps_only.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    p_only = sub.add_parser("only")
    p_only.add_argument("doc", type=Path)
    p_only.add_argument("out", type=Path)
    p_only.add_argument("--manifest", type=Path)
    p_only.set_defaults(fn=only)
    p_pair = sub.add_parser("paired")
    p_pair.add_argument("doc", type=Path)
    p_pair.add_argument("out", type=Path)
    p_pair.add_argument("--seed-run", type=Path, required=True, help="папка run/ записанного прогона")
    p_pair.add_argument("--no-gaps", action="store_true")
    p_pair.add_argument("--passes", type=int, help="контроль: K обычных проходов поиска (первый из записи)")
    p_pair.set_defaults(fn=paired)
    args = parser.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    sys.exit(main())
