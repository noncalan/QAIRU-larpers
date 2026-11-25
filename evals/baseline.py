"""Базовая линия «один промпт» (конфигурация A протокола D-011, docs/plan.md).

Промпт исследования 11 дословно (системный и пользовательский), Opus 5.5 ``medium``, вход
``data/input/requirements.md``, вызов через клиент модели агента (``ClaudeCLI``) и тот же лог вызовов
``calls.jsonl``. Три прогона; сырые ответы лежат в ``evals/results/baseline/run-1..3/``:

- ``answer.md``: текст ответа модели без правок;
- ``response.json``: метаданные ответа (модель, токены, цена, время);
- ``calls.jsonl``: лог вызовов в формате агента;
- ``quotes.json``: сверка цитат из сырого текста (``evals.quotes``).

Не менять: промпт, модель, effort. Повторный запуск не вызывает модель там, где ``answer.md`` уже есть.

Запуск: ``uv run python -m evals.baseline [--runs 3] [--out evals/results/baseline]``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

from evals.quotes import QuoteStats, score_quotes
from validator.errors import LLMError
from validator.llm.base import LLMClient
from validator.llm.calllog import RecordingClient, write_calls_jsonl
from validator.llm.claude_cli import ClaudeCLI
from validator.models import Effort, LLMRequest, LLMResponse, LLMStep

ROOT = Path(__file__).resolve().parents[1]
DOCUMENT = ROOT / "data" / "input" / "requirements.md"
RESULTS = ROOT / "evals" / "results" / "baseline"

MODEL = "claude-opus-5-5"
EFFORT = Effort.MEDIUM
# Дословно из experiments/11-baseline-one-prompt/ (run_baseline.py и cli.py).
SYSTEM_PROMPT = "Ты аналитик требований к программному обеспечению. Отвечай на русском языке."
USER_PROMPT = (
    "Проверь документ требований по ISO/IEC/IEEE 29148:2018 (п. 5.2.5 и 5.2.6) и IEEE 830-1998 (п. 4.3). "
    "Выпиши все дефекты. Для каждого: точная цитата из документа, какое свойство нарушено, почему, "
    "насколько критично."
)
RUNS = 3


def build_request(document: str, run: int) -> LLMRequest:
    """Запрос базовой линии. Документ подаётся как в исследовании 11, без ID блоков."""
    return LLMRequest(
        step=LLMStep.SEARCH,
        call_index=run,
        model=MODEL,
        effort=EFFORT,
        system=SYSTEM_PROMPT,
        prompt=USER_PROMPT + "\n\n---\nДокумент:\n\n" + document,
    )


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _meta(resp: LLMResponse) -> dict[str, object]:
    return {
        "model": resp.model,
        "backend": resp.backend.value,
        "usage": resp.usage.model_dump(),
        "cost_usd": resp.cost_usd,
        "duration_ms": resp.duration_ms,
        "answer_chars": len(resp.text),
    }


def run_once(client: LLMClient, document: str, run: int, out: Path) -> dict[str, object]:
    """Один прогон: вызов модели (если ответа ещё нет), сверка цитат, файлы прогона."""
    run_dir = out / f"run-{run}"
    run_dir.mkdir(parents=True, exist_ok=True)
    answer_path = run_dir / "answer.md"
    meta_path = run_dir / "response.json"
    if not (answer_path.exists() and meta_path.exists()):
        recorder = RecordingClient(client)
        resp = recorder.call(build_request(document, run))
        answer_path.write_text(resp.text, encoding="utf-8")
        _write_json(meta_path, _meta(resp))
        write_calls_jsonl(run_dir / "calls.jsonl", recorder.records)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    stats, by_kind = score_quotes(document, answer_path.read_text(encoding="utf-8"))
    quotes = {"total": stats.as_dict(), "by_kind": {k: v.as_dict() for k, v in sorted(by_kind.items())}}
    _write_json(run_dir / "quotes.json", quotes)
    return {"run": run, **meta, "quotes": quotes["total"]}


def summarize(rows: list[dict[str, object]]) -> dict[str, object]:
    """Сводка по прогонам: среднее по времени, цене и сверке цитат."""
    total = QuoteStats()
    for row in rows:
        q = row["quotes"]
        assert isinstance(q, dict)  # noqa: S101
        total.merge(
            QuoteStats(total=q["total"], exact=q["exact"], fuzzy=q["fuzzy"], not_found=q["not_found"])
        )
    costs = [float(r["cost_usd"]) for r in rows if isinstance(r["cost_usd"], int | float)]
    times = [int(r["duration_ms"]) / 1000 for r in rows if isinstance(r["duration_ms"], int | float)]
    return {
        "model": MODEL,
        "effort": EFFORT.value,
        "runs": rows,
        "mean_cost_usd": round(mean(costs), 4) if costs else None,
        "mean_duration_s": round(mean(times), 1) if times else None,
        "quotes_overall": total.as_dict(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Базовая линия «один промпт» (конфигурация A)")
    parser.add_argument("--runs", type=int, default=RUNS)
    parser.add_argument("--out", type=Path, default=RESULTS)
    args = parser.parse_args(argv)
    document = DOCUMENT.read_text(encoding="utf-8")
    try:
        client: LLMClient = ClaudeCLI()
        rows = [run_once(client, document, n, args.out) for n in range(1, args.runs + 1)]
    except LLMError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    summary = summarize(rows)
    _write_json(args.out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
