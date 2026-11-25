"""Базовая линия «один промпт» (конфигурация A протокола D-011, docs/plan.md).

Промпт исследования 11 дословно (системный и пользовательский), Opus 5.5 ``medium``, вход
``data/input/requirements.md``, вызов через клиент модели агента (``ClaudeCLI``) и тот же лог вызовов
``calls.jsonl``. Три прогона; сырые ответы лежат в ``evals/results/baseline/run-1..3/``:

- ``responses/``: ответ модели целиком в формате агента (то же, что ``run/responses/``, годится для replay);
- ``answer.md``: текст ответа модели без правок (его же читают судья и сверка цитат);
- ``calls.jsonl``: лог вызовов в формате агента;
- ``quotes.json``: сверка цитат из сырого текста (``evals.quotes``).

Не менять: промпт, модель, effort. Повторный запуск не вызывает модель там, где ответ в ``responses/`` уже есть.

Запуск: ``uv run python -m evals.baseline [--runs 3] [--out evals/results/baseline]``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

from evals.quotes import QuoteStats, score_quotes
from validator.errors import LLM_REPLAY_MISSING, LLMError
from validator.llm.base import parse_cli_payload
from validator.llm.cache import ResponseCache, read_envelope
from validator.llm.calllog import RecordingClient, read_calls_jsonl, response_file_name, write_calls_jsonl
from validator.llm.claude_cli import ClaudeCLI
from validator.models import Backend, CacheMode, CallStatus, Effort, LLMRequest, LLMStep

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
RESPONSES_DIR = "responses"


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


def _live_call(req: LLMRequest, run_dir: Path) -> None:
    """Живой вызов через клиент агента: кеш, ответ в ``responses/``, ``calls.jsonl`` (#72)."""
    recorder = RecordingClient(
        ClaudeCLI(),
        cache=ResponseCache(ROOT / ".cache" / "validator", CacheMode.USE),
        responses_dir=run_dir / RESPONSES_DIR,
    )
    try:
        recorder.call(req)
    finally:
        write_calls_jsonl(run_dir / "calls.jsonl", recorder.records)


def run_once(document: str, run: int, out: Path) -> dict[str, object]:
    """Один прогон: ответ из ``responses/`` (если его нет, живой вызов), сверка цитат, файлы прогона."""
    run_dir = out / f"run-{run}"
    req = build_request(document, run)
    response_path = run_dir / RESPONSES_DIR / response_file_name(req)
    if not response_path.is_file():
        _live_call(req, run_dir)
    envelope = read_envelope(response_path)
    if envelope is None:
        raise LLMError(f"Нет ответа базовой линии: {response_path}", code=LLM_REPLAY_MISSING)
    resp = parse_cli_payload(envelope["payload"], req, Backend.CLI)
    (run_dir / "answer.md").write_text(resp.text, encoding="utf-8")
    call = next(r for r in reversed(read_calls_jsonl(run_dir / "calls.jsonl")) if r.status is CallStatus.OK)
    stats, by_kind = score_quotes(document, resp.text)
    quotes = {"total": stats.as_dict(), "by_kind": {k: v.as_dict() for k, v in sorted(by_kind.items())}}
    _write_json(run_dir / "quotes.json", quotes)
    return {
        "run": run,
        "model": call.model_actual,
        "usage": call.usage.model_dump(),
        "cost_usd": call.cost_usd,
        "duration_ms": call.duration_ms,
        "answer_chars": len(resp.text),
        "quotes": quotes["total"],
    }


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
        rows = [run_once(document, n, args.out) for n in range(1, args.runs + 1)]
    except LLMError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1
    summary = summarize(rows)
    _write_json(args.out / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
