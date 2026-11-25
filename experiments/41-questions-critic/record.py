"""Критик-самоответ вопросов (#41): живые вызовы и два прогона для метрик «до и после критика».

Конвейер до вопросов идёт на бэкенде ``fake`` (фикстуры поиска и нарезки из ``tests/fixtures/llm/``),
живые вызовы идут через ``claude -p`` (подписка, Opus 5.5 ``high`` по D-010). Ключи API не нужны.

Команды (каждая печатает, сколько живых вызовов сделает; ``--dry-run`` только печатает промпт):

- ``critic-1``: критик на вопросах фейкового конвейера (редактор из фикстуры #20), 1 вызов; ответ
  пишется фикстурой ``tests/fixtures/llm/questions_critic/1.json`` (на ней идут smoke и тесты);
- ``run-2``: заново редактор и критик на тех же кандидатах, 2 вызова; ответы в ``run-2/llm/``
  этой папки (второй прогон шага вопросов, фикстуры тестов не трогает);
- ``reports``: без вызовов собирает ``out/run-1`` и ``out/run-2`` (``report.json`` и отчёт) на бэкенде
  ``fake``; второй прогон берёт ответы вопросов из ``run-2/llm/``. Метрики: ``python -m evals.questions``.

Запуск: ``uv run python experiments/41-questions-critic/record.py КОМАНДА [--dry-run]``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from validator.config import load_config
from validator.evidence import evidence
from validator.ingest import parse
from validator.lint import lint
from validator.llm import FakeLLM
from validator.llm.claude_cli import SYSTEM_PROMPT_FILE, build_args, clean_env, find_claude
from validator.merge import merge
from validator.models import Backend, DocKind, LLMRequest, LLMStep, Question
from validator.pipeline import run as run_pipeline
from validator.questions import draft_questions
from validator.questions.candidates import collect_candidates
from validator.questions.critic import build_request as critic_request
from validator.questions.editor import build_request as editor_request
from validator.search import search
from validator.segment import segment
from validator.verify import verify

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DOC = ROOT / "data" / "input" / "requirements.md"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
RUN2 = HERE / "run-2" / "llm"
OUT = HERE / "out"


def _live(req: LLMRequest, out: Path) -> dict[str, Any] | None:
    """Один вызов ``claude -p`` как у клиента агента; сырой ответ пишется в ``out`` как есть."""
    exe = find_claude()
    if exe is None:
        print("claude не найден")
        return None
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
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except ValueError:
        payload = {"is_error": True, "result": result.stderr.decode("utf-8", errors="replace")[-500:]}
    if result.returncode != 0 or payload.get("is_error"):
        print(f"Вызов не удался (код {result.returncode}): {str(payload.get('result'))[:300]}")
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Записано: {out.relative_to(ROOT)}; ${payload.get('total_cost_usd')}, {payload.get('duration_ms')} мс"
    )
    return payload  # type: ignore[no-any-return]


def _inputs():  # type: ignore[no-untyped-def]
    cfg = load_config(DOC, OUT / "record" / "report.md", backend=Backend.FAKE)
    doc = parse(DOC, DocKind.MD)
    fake = FakeLLM(FIXTURES)
    seg = segment(doc, fake, cfg)
    ver = verify(evidence(merge(search(doc, fake, cfg), lint(doc, seg), doc), doc, fake, cfg), doc, seg)
    return cfg, doc, seg, ver


def _show(questions: list[Question]) -> None:
    for q in questions:
        print(f"  {q.id} {q.priority.value}: {q.question}")


def critic_1(dry: bool) -> int:
    cfg, doc, seg, ver = _inputs()
    drafted = draft_questions(ver, doc, seg, FakeLLM(FIXTURES), cfg)
    print(f"Вопросов до критика: {len(drafted.questions)}; живых вызовов: 1")
    _show(drafted.questions)
    req = critic_request(drafted.questions, doc, cfg)
    if dry:
        print(req.prompt[req.prompt.find("<вопросы>") :])
        return 0
    return 0 if _live(req, FIXTURES / LLMStep.QUESTIONS_CRITIC.value / "1.json") else 1


def run_2(dry: bool) -> int:
    cfg, doc, seg, ver = _inputs()
    cands = collect_candidates(ver)
    print(f"Кандидатов: {len(cands)}; живых вызовов: 2 (редактор, критик)")
    req = editor_request(cands, doc, {r.id: r.type for r in seg.requirements}, cfg)
    if dry:
        return 0
    if _live(req, RUN2 / LLMStep.QUESTIONS.value / "1.json") is None:
        return 1
    fixtures = _overlay()
    drafted = draft_questions(ver, doc, seg, FakeLLM(fixtures), cfg)
    print(f"Вопросов до критика: {len(drafted.questions)}")
    _show(drafted.questions)
    return (
        0 if _live(critic_request(drafted.questions, doc, cfg), RUN2 / "questions_critic" / "1.json") else 1
    )


def _overlay() -> Path:
    """Фикстуры тестов, поверх которых лежат ответы вопросов второго прогона."""
    target = OUT / "fixtures-run-2"
    shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(FIXTURES, target)
    for step in (LLMStep.QUESTIONS, LLMStep.QUESTIONS_CRITIC):
        src = RUN2 / step.value / "1.json"
        if src.is_file():
            shutil.copy2(src, target / step.value / "1.json")
    return target


def reports() -> int:
    for name, fixtures in (("run-1", FIXTURES), ("run-2", _overlay())):
        shutil.rmtree(OUT / name, ignore_errors=True)
        cfg = load_config(DOC, OUT / name / "report.md", backend=Backend.FAKE)
        outcome = run_pipeline(cfg.model_copy(update={"fake_fixtures": fixtures}))
        print(f"{name}: {outcome.message}")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    dry = "--dry-run" in sys.argv
    if command == "critic-1":
        return critic_1(dry)
    if command == "run-2":
        return run_2(dry)
    if command == "reports":
        return reports()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
