"""Один живой вызов редактора вопросов (#20) на кандидатах из фикстур конвейера и запись фикстуры.

Конвейер до верификатора идёт на бэкенде ``fake`` (ответы поиска из ``tests/fixtures/llm/search/``),
кандидатов собирает код, редактор вызывается через ``claude -p`` (подписка, Opus 5.5 ``high`` по D-010).
Сырой ответ ``claude -p --output-format json`` пишется как есть в ``tests/fixtures/llm/questions/1.json``,
затем тот же ответ разбирается кодом агента и печатается итог: число карточек и первая карточка.

Запуск: ``uv run python experiments/20-questions-editor/record.py [--dry-run]``.
``--dry-run`` только печатает промпт и ничего не вызывает. Ключи API не нужны и не читаются.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from validator.config import load_config
from validator.evidence import evidence
from validator.ingest import parse
from validator.lint import lint
from validator.llm import FakeLLM
from validator.llm.claude_cli import SYSTEM_PROMPT_FILE, build_args, clean_env, find_claude
from validator.merge import merge
from validator.models import Backend, DocKind
from validator.questions import build_questions
from validator.questions.candidates import collect_candidates
from validator.questions.editor import build_request
from validator.search import search
from validator.segment import segment
from validator.verify import verify

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "data" / "input" / "requirements.md"
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
OUT = FIXTURES / "questions" / "1.json"


def main() -> int:
    cfg = load_config(DOC, ROOT / "out" / "record" / "report.md", backend=Backend.FAKE)
    doc = parse(DOC, DocKind.MD)
    fake = FakeLLM(FIXTURES)
    seg = segment(doc, fake, cfg)
    merged = evidence(merge(search(doc, fake, cfg), lint(doc, seg), doc), doc, fake, cfg)
    ver = verify(merged, doc, seg)
    cands = collect_candidates(ver)
    req = build_request(cands, doc, {r.id: r.type for r in seg.requirements}, cfg)
    print(
        f"Кандидатов: {len(cands)}; модель {req.model} · {req.effort.value}; промпт {len(req.prompt)} симв."
    )
    if "--dry-run" in sys.argv:
        print(req.prompt[req.prompt.find("<кандидаты>") :])
        return 0
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
    try:
        payload = json.loads(result.stdout.decode("utf-8"))
    except ValueError:
        payload = {"is_error": True, "result": result.stderr.decode("utf-8", errors="replace")[-500:]}
    if result.returncode != 0 or payload.get("is_error"):
        print(f"Вызов не удался (код {result.returncode}), фикстура не тронута: {str(payload.get('result'))[:300]}")
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Записано: {OUT.relative_to(ROOT)}; цена ${payload.get('total_cost_usd')}, {payload.get('duration_ms')} мс"
    )
    outcome = build_questions(ver, doc, seg, FakeLLM(FIXTURES), cfg)
    print(f"Карточек: {len(outcome.questions)}; диагностика: {[d.message for d in outcome.diagnostics]}")
    if outcome.questions:
        print(outcome.questions[0].model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
