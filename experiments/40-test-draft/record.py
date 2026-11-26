"""Попытка теста (#40): живой вызов пачки находок и разбор записанного ответа.

Конвейер до шага «попытка теста» идёт на бэкенде ``fake`` (фикстуры нарезки и поиска из
``tests/fixtures/llm/``), живой вызов идёт через ``claude -p`` (подписка, Sonnet 5.5 ``high`` по D-010).
Ключи API не нужны.

Команды:

- ``dry``: печатает, сколько находок получат черновик и сколько пачек, и первые строки промпта; без вызовов;
- ``live``: живые вызовы по пачкам, ответы пишутся в ``OUT/test_draft/N.json`` (формат ``claude -p``);
  ``--fixture`` кладёт их ещё и в ``tests/fixtures/llm/test_draft/`` (на них идут smoke и тесты);
- ``show``: без вызовов разбирает записанные фикстуры кодом агента и печатает черновики.

Запуск: ``uv run python experiments/40-test-draft/record.py КОМАНДА [--fixture]``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from validator.config import load_config
from validator.evidence import evidence
from validator.evidence_testdraft import BATCH_SIZE, build_request, draft_tests, needs_draft
from validator.ingest import parse
from validator.lint import lint
from validator.llm import FakeLLM
from validator.llm.claude_cli import SYSTEM_PROMPT_FILE, build_args, clean_env, find_claude
from validator.merge import merge
from validator.models import Backend, DocKind, LLMRequest, LLMStep
from validator.search import search
from validator.segment import segment

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
FIXTURES = ROOT / "tests" / "fixtures" / "llm"
DOC = ROOT / "data" / "input" / "requirements.md"
OUT = HERE / "out" / "test_draft"


def _inputs():  # type: ignore[no-untyped-def]
    cfg = load_config(DOC, HERE / "out" / "report.md", backend=Backend.FAKE)
    doc = parse(DOC, DocKind.MD)
    fake = FakeLLM(FIXTURES)
    seg = segment(doc, fake, cfg)
    found = evidence(merge(search(doc, fake, cfg), lint(doc, seg), doc), doc, fake, cfg)
    return cfg, doc, found


def _live(req: LLMRequest, out: Path) -> dict[str, Any] | None:
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
    print(f"Записано: {out}; ${payload.get('total_cost_usd')}, {payload.get('duration_ms')} мс")
    return payload  # type: ignore[no-any-return]


def dry() -> int:
    cfg, doc, found = _inputs()
    targets = [m for m in found if needs_draft(m)]
    batches = -(-len(targets) // BATCH_SIZE)
    print(f"Находок {len(found)}, получат черновик {len(targets)}, пачек {batches} (живых вызовов столько же)")
    if targets:
        req = build_request(targets[:BATCH_SIZE], doc, cfg, 1)
        print(req.prompt[req.prompt.find("<находки>") :][:3000])
    return 0


def live(fixture: bool) -> int:
    cfg, doc, found = _inputs()
    targets = [m for m in found if needs_draft(m)]
    for index in range(1, -(-len(targets) // BATCH_SIZE) + 1):
        batch = targets[(index - 1) * BATCH_SIZE : index * BATCH_SIZE]
        payload = _live(build_request(batch, doc, cfg, index), OUT / f"{index}.json")
        if payload is None:
            return 1
        if fixture:
            target = FIXTURES / LLMStep.TEST_DRAFT.value / f"{index}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


def show() -> int:
    cfg, doc, found = _inputs()
    fake = FakeLLM(FIXTURES)
    outcome = draft_tests(found, doc, fake, cfg)
    for d in outcome.diagnostics:
        print(f"{d.code} {d.level.value}: {d.message}; {d.action}")
    for m in outcome.findings:
        t = m.test_draft
        if t is None:
            continue
        print(f"\n{m.mid} {m.property.value} blocks_testing={m.blocks_testing} complete={t.complete}")
        print(f"  Предусловие: {t.precondition}")
        for i, s in enumerate(t.steps, 1):
            print(f"  Шаг {i}: {s}")
        print(f"  Ожидается: {t.expected}")
        for v in t.values:
            print(f"  Значение «{v.value}» ← «{v.quote}» [{'ok' if v.verified else 'нет'}]")
        for g in t.gap_items:
            print(f"  [?{g.number}] {g.missing}" + (f" (заменено: {g.replaced})" if g.replaced else ""))
        if t.note:
            print(f"  Примечание: {t.note}")
    print(f"\nЖурнал: {outcome.log}")
    return 0


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "dry":
        return dry()
    if command == "live":
        return live("--fixture" in sys.argv)
    if command == "show":
        return show()
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
