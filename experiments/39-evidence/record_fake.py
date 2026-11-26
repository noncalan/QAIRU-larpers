"""Записать фикстуры свидетеля и критика для бэкенда fake (#39): tests/fixtures/llm/{evidence,critic}/1.json.

Все шаги, кроме шага 6, отвечает ``FakeLLM`` из tests/fixtures/llm (как в smoke-прогоне), свидетель и
критик идут живым ``claude -p`` (подписка) на те же находки, что видит smoke-прогон по requirements.md.
Из ответа CLI убираются служебные поля, ``session_id`` обнуляется, как у фикстуры нарезки.

    uv run python experiments/39-evidence/record_fake.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from validator import pipeline
from validator.config import load_config
from validator.llm import FakeLLM
from validator.llm.claude_cli import ClaudeCLI
from validator.models import Backend, Diagnostic, LLMRequest, LLMResponse, LLMStep, RunConfig

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
LIVE = {LLMStep.EVIDENCE, LLMStep.CRITIC}
KEEP_FIELDS = ("type", "subtype", "is_error", "result", "usage", "modelUsage", "total_cost_usd", "session_id")


class Recorder:
    backend = Backend.FAKE

    def __init__(self) -> None:
        self.fake = FakeLLM(FIXTURES)
        self.live = ClaudeCLI()

    def call(self, req: LLMRequest) -> LLMResponse:
        if req.step not in LIVE:
            return self.fake.call(req)
        print(f"[39] живой вызов {req.call_id}: {req.model} {req.effort.value}, промпт {len(req.prompt)} зн.")
        resp = self.live.call(req)
        raw = resp.raw if isinstance(resp.raw, dict) else {}
        payload = {k: raw[k] for k in KEEP_FIELDS if k in raw}
        payload["session_id"] = "00000000-0000-0000-0000-000000000000"
        path = FIXTURES / req.step.value / f"{req.call_index}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[39] записан {path.relative_to(ROOT)}")
        return resp


def main() -> int:
    out = ROOT / "out" / "39" / "fake-record" / "report.md"
    cfg = load_config(ROOT / "data" / "input" / "requirements.md", out, backend=Backend.FAKE, critic=True)
    recorder = Recorder()

    def client(_: RunConfig) -> tuple[Recorder, list[Diagnostic]]:
        return recorder, []

    pipeline.make_client = client  # type: ignore[assignment]
    outcome = pipeline.run(cfg)
    print(f"[39] {outcome.message}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
