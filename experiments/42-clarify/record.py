"""Цикл уточнения (#42): живой вызов сверки с ответами автора для фикстуры фейкового конвейера.

Конвейер до вопросов идёт на бэкенде ``fake`` (фикстуры ``tests/fixtures/llm/``), ответы автора для тестов
лежат в ``tests/fixtures/clarify/answers.yaml``. Живой вызов один: сверка (Opus 5.5 ``high``, D-010) через
``claude -p`` по подписке; ответ пишется фикстурой ``tests/fixtures/llm/clarify/1.json`` (на ней smoke и
тесты цикла уточнения). ``--dry-run`` печатает промпт без вызова.

Запуск: ``uv run python experiments/42-clarify/record.py [--dry-run]``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from validator.clarify import _match_all, load_answers
from validator.clarify.model import build_request
from validator.config import load_config
from validator.ingest import parse
from validator.llm.claude_cli import SYSTEM_PROMPT_FILE, build_args, clean_env, find_claude
from validator.models import Backend, DocKind, LLMRequest
from validator.pipeline import run

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "data" / "input" / "requirements.md"
ANSWERS = ROOT / "tests" / "fixtures" / "clarify" / "answers.yaml"
FIXTURE = ROOT / "tests" / "fixtures" / "llm" / "clarify" / "1.json"


def _request() -> LLMRequest:
    with tempfile.TemporaryDirectory(prefix="qairu-42-") as out:
        cfg = load_config(DOC, Path(out) / "report.md", backend=Backend.FAKE, answers_path=ANSWERS)
        report = run(cfg.model_copy(update={"answers_path": None})).report
    if report is None or report.verification is None:
        raise SystemExit("Фейковый прогон не дал отчёта с находками")
    doc = parse(DOC, DocKind.MD)
    matched = _match_all(load_answers(ANSWERS), report.questions, doc)
    answered = [c.answer for c in matched.contexts.values()]
    return build_request(report.verification.findings, answered, doc, cfg)


def _live(req: LLMRequest) -> int:
    exe = find_claude()
    if exe is None:
        print("claude не найден")
        return 1
    with tempfile.TemporaryDirectory(prefix="qairu-record-") as cwd:
        system_file = Path(cwd) / SYSTEM_PROMPT_FILE
        system_file.write_text(req.system, encoding="utf-8")
        result = subprocess.run(
            build_args(exe, req, system_file),
            input=req.prompt.encode("utf-8"),
            capture_output=True,
            cwd=cwd,
            env=clean_env(),
            timeout=req.timeout_s,
            check=False,
        )
    payload = json.loads(result.stdout.decode("utf-8") or "{}")
    if result.returncode != 0 or payload.get("is_error"):
        print(f"Вызов не удался (код {result.returncode}): {str(payload.get('result'))[:300]}")
        return 1
    payload["session_id"] = "00000000-0000-0000-0000-000000000000"
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cost, ms = payload.get("total_cost_usd"), payload.get("duration_ms")
    print(f"Записано: {FIXTURE.relative_to(ROOT)}; ${cost}, {ms} мс")
    return 0


def main() -> int:
    req = _request()
    if "--dry-run" in sys.argv:
        print(req.system, "\n\n", req.prompt)
        return 0
    print("Живой вызов: 1 (сверка с ответами автора, Opus 5.5 high)")
    return _live(req)


if __name__ == "__main__":
    sys.exit(main())
