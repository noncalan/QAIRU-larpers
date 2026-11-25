"""Эксперимент 23: живой проход поиска и перенос в JSON на requirements.md (issue #18).

Что делает (не больше трёх живых вызовов ``claude -p``, без xhigh):

1. ``search()`` агента с одним проходом профиля ``quick`` (Opus 5.5 ``medium``) через подписку.
   Если код не разобрал часть находок, шаг сам вызывает перенос в JSON (Haiku 5.5): это второй вызов.
2. Принудительный перенос всех находок того же прохода в JSON через Haiku 5.5 ``medium``: сравнение
   разбора кодом с переносом моделью (основание для отклонения от D-010 в PR).
3. Сводка: время, токены, цена по тарифу API, число находок, разобранных кодом, отброшенных ID блоков,
   совпадение полей «код против Haiku». Ответы сохраняются в формате ``claude -p --output-format json``
   (фикстуры ``FakeLLM``), повторный запуск берёт их с диска и модель не вызывает.

Запуск: ``uv run python experiments/23-search-pass/record.py``. Выход: ``experiments/out/23/``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from validator.config import load_config  # noqa: E402
from validator.ingest import parse  # noqa: E402
from validator.ingest.blocks import render_for_model  # noqa: E402
from validator.llm import ClaudeCLI, FakeLLM, parse_cli_payload  # noqa: E402
from validator.models import Backend, DocKind, LLMRequest, LLMResponse, Profile  # noqa: E402
from validator.search import search  # noqa: E402
from validator.search_transfer import transfer_request, transferred  # noqa: E402
from validator.search_parse import Draft, draft_from_raw, split_findings  # noqa: E402
from validator.text import locate_quote, normalize  # noqa: E402

DOCUMENT = ROOT / "data" / "input" / "requirements.md"
OUT = ROOT / "experiments" / "out" / "23"


def payload(resp: LLMResponse) -> dict[str, Any]:
    """Ответ в формате ``claude -p --output-format json``: его читает ``FakeLLM`` как фикстуру."""
    data: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": resp.duration_ms,
        "num_turns": 1,
        "result": resp.text,
        "total_cost_usd": resp.cost_usd,
        "usage": resp.usage.model_dump(),
        "modelUsage": {resp.model or "unknown": {}},
    }
    if resp.structured is not None:
        data["structured_output"] = resp.structured
    return data


class SavingClient:
    """Живой клиент, который сохраняет каждый ответ; если ответ уже сохранён, модель не вызывается."""

    backend = Backend.CLI

    def __init__(self, root: Path) -> None:
        self.root = root
        self.live: ClaudeCLI | None = None
        self.calls: list[dict[str, Any]] = []

    def call(self, req: LLMRequest) -> LLMResponse:
        path = self.root / req.step.value / f"{req.call_index}.json"
        if path.is_file():
            resp = parse_cli_payload(json.loads(path.read_text(encoding="utf-8")), req, Backend.CLI, cached=True)
        else:
            self.live = self.live or ClaudeCLI()
            started = time.perf_counter()
            resp = self.live.call(req)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload(resp), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"[live] {req.call_id}: {time.perf_counter() - started:.0f} с", flush=True)
        self.calls.append(
            {
                "call_id": req.call_id,
                "model": resp.model,
                "effort": req.effort.value,
                "cached": resp.cached,
                "duration_s": (resp.duration_ms or 0) / 1000,
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
                "cost_usd": resp.cost_usd,
            }
        )
        return resp


def norm(quotes: tuple[str, ...]) -> list[str]:
    return sorted(normalize(q)[0] for q in quotes)


def same(a: Draft, b: Draft) -> dict[str, bool]:
    """Совпадение полей черновика кода и черновика переноса."""
    return {
        "property": a.property == b.property,
        "defect_type": a.defect_type == b.defect_type,
        "verdict": a.verdict == b.verdict,
        "severity": a.severity_hint == b.severity_hint,
        "blocks_testing": a.blocks_testing == b.blocks_testing,
        "block_ids": sorted(a.block_ids) == sorted(b.block_ids),
        "quotes": norm(a.quotes) == norm(b.quotes),
    }


def main() -> int:
    doc = parse(DOCUMENT, DocKind.MD)
    cfg = load_config(DOCUMENT, OUT / "report.md", profile=Profile.QUICK, backend=Backend.CLI, passes=1)
    client = SavingClient(OUT)

    started = time.perf_counter()
    [result] = search(doc, client, cfg)
    wall = time.perf_counter() - started
    print(f"[search] ok={result.ok} wall={wall:.0f} с, кандидатов {len(result.candidates)}", flush=True)
    for d in result.diagnostics:
        print(f"  {d.code} {d.level.value}: {d.message} → {d.action}")
    if not result.ok:
        print(f"[error] {result.error}")
        return 1

    split = split_findings(result.raw_text)
    code = {raw.number: draft_from_raw(raw) for raw in split.findings}
    parsed = {n: d for n, d in code.items() if isinstance(d, Draft)}
    quotes = [q for c in result.candidates for q in c.quotes]
    verified = [q for q in quotes if locate_quote(doc, q).verified]

    # Принудительный перенос всех находок прохода: сравнение кода и Haiku.
    findings = "\n\n".join(raw.text for raw in split.findings)
    forced = SavingClient(OUT / "forced")
    req = transfer_request(render_for_model(doc), findings, cfg, 1, 0)
    resp = forced.call(req)
    moved, invalid = transferred(resp.structured, split.findings, req.call_id)
    number_at = {raw.position: raw.number for raw in split.findings}
    by_number: dict[int | None, Draft] = {}
    for position, draft in moved:
        by_number.setdefault(number_at.get(int(position)), draft)
    agree: dict[str, int] = {}
    compared = 0
    for number, draft in parsed.items():
        other = by_number.get(number)
        if other is None:
            continue
        compared += 1
        for name, ok in same(draft, other).items():
            agree[name] = agree.get(name, 0) + ok
    moved_quotes = [q for _, d in moved for q in d.quotes]
    moved_verified = [q for q in moved_quotes if locate_quote(doc, q).verified]

    summary = {
        "calls": client.calls + forced.calls,
        "search_wall_s": round(wall, 1),
        "findings_in_answer": len(split.findings),
        "parsed_by_code": len(parsed),
        "not_parsed": {n: d for n, d in code.items() if not isinstance(d, Draft)},
        "candidates": len(result.candidates),
        "candidates_by_scope": {
            s: sum(c.scope.value == s for c in result.candidates) for s in ("requirement", "set")
        },
        "with_title": sum(bool(c.title) for c in result.candidates),
        "with_scenario": sum(bool(c.scenario) for c in result.candidates),
        "with_test_gap": sum(bool(c.test_gap) for c in result.candidates),
        "blocks_testing": sum(c.blocks_testing for c in result.candidates),
        "unclear": sum(c.verdict.value == "unclear" for c in result.candidates),
        "quotes": len(quotes),
        "quotes_verified_by_code": len(verified),
        "diagnostics": [f"{d.code}: {d.message}" for d in result.diagnostics],
        "haiku_transfer": {
            "records": len(moved),
            "invalid": invalid,
            "quotes": len(moved_quotes),
            "quotes_verified": len(moved_verified),
            "compared_with_code": compared,
            "agree": agree,
        },
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    # Тот же проход через FakeLLM: сохранённые ответы читаются как фикстуры без потерь.
    replay = search(doc, FakeLLM(OUT), cfg.model_copy(update={"backend": Backend.FAKE}))
    assert replay[0].candidates == result.candidates, "фикстура читается не так, как живой ответ"
    print("[replay] фикстура даёт те же кандидаты")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
