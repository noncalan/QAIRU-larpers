"""Шаг 6 (#39): прогоны агента на записанном живом прогоне — свидетель и критик живые, остальное из записи.

Зачем. Eval шага 6 парный: одни и те же находки поиска с шагом 6 и без него. Так разница в recall и шуме
по судье идёт только от шага 6, а не от разброса проходов поиска (он больше, чем эффект критика).

Клиент модели гибридный:

- нарезка, поиск и перенос в JSON берутся из записи ``ЗАПИСЬ/run`` по ключу запроса (документ и промпты
  те же, что в записи), иначе по шагу и номеру вызова;
- свидетель (``evidence``) и критик (``critic``): ответ из записи по ключу, иначе живой ``claude -p``
  (с ``--live``) или ошибка шага (без ``--live``: так видно, сколько вызовов понадобится);
- вопросы автору: из записи по шагу и номеру (на находки они не влияют, живые вызовы не тратим).

Команды::

    # сколько находок уйдёт свидетелю и критику, размеры промптов; без вызовов модели
    uv run python experiments/39-evidence/run.py tests/fixtures/e2e/md out/39/md-dry --dry-run
    # с шагом 6: 2 живых вызова (Opus 5.5 high), ответы ложатся в out/.../run/responses и в кеш
    uv run python experiments/39-evidence/run.py tests/fixtures/e2e/md out/39/md-on --live
    # без шага 6 на тех же находках: 0 вызовов
    uv run python experiments/39-evidence/run.py tests/fixtures/e2e/md out/39/md-off --no-evidence

Дальше eval: ``uv run python -m evals.eval out/39/md-off ... --name ...`` (судья живой, если нет в кеше).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from validator import pipeline
from validator.config import load_config
from validator.errors import LLM_REPLAY_MISSING, LLMError
from validator.evidence import EvidenceOutcome
from validator.llm.claude_cli import ClaudeCLI
from validator.llm.replay import ReplayLLM
from validator.models import (
    Backend,
    Diagnostic,
    LLMRequest,
    LLMResponse,
    LLMStep,
    MergedFinding,
    ParsedDocument,
    Profile,
    RunConfig,
)

ROOT = Path(__file__).resolve().parents[2]
LIVE_STEPS = frozenset({LLMStep.EVIDENCE, LLMStep.CRITIC})


class Hybrid:
    """Запись для шагов до и после шага 6, живая модель (или стоп) для свидетеля и критика."""

    backend = Backend.CLI

    def __init__(self, recording: Path, *, live: bool, dump: Path | None, reuse: bool = False) -> None:
        self.replay = ReplayLLM(recording)
        self.reuse = reuse
        self.live = ClaudeCLI() if live else None
        self.dump = dump
        self.live_calls: list[str] = []

    def call(self, req: LLMRequest) -> LLMResponse:
        if req.step not in LIVE_STEPS:
            return self.replay.call(req)
        if req.key in self.replay._by_key or self.reuse:
            return self.replay.call(req)
        if self.dump is not None:
            self.dump.mkdir(parents=True, exist_ok=True)
            (self.dump / f"{req.call_id}.system.md").write_text(req.system, encoding="utf-8")
            (self.dump / f"{req.call_id}.prompt.md").write_text(req.prompt, encoding="utf-8")
        if self.live is None:
            raise LLMError(f"Живой вызов {req.call_id} не разрешён (нужен --live)", code=LLM_REPLAY_MISSING)
        self.live_calls.append(req.call_id)
        print(f"[39] живой вызов {req.call_id}: {req.model} {req.effort.value}, промпт {len(req.prompt)} зн.")
        return self.live.call(req)


def _no_evidence(
    found: Sequence[MergedFinding], doc: ParsedDocument, llm: object, cfg: RunConfig
) -> EvidenceOutcome:
    del doc, llm, cfg
    return EvidenceOutcome(findings=list(found))


def _input_for(recording: Path) -> Path:
    """Документ записи: имя формата папки e2e (md, pdf, docx) или явный --input."""
    kind = recording.name if (recording / "run").is_dir() else recording.parent.name
    return ROOT / "data" / "input" / f"requirements.{kind}"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording", type=Path, help="Папка прогона с run/ (calls.jsonl и responses/)")
    ap.add_argument("out", type=Path, help="Папка, куда писать report.md, report.json и run/")
    ap.add_argument("--input", type=Path, default=None, help="Документ записи (по умолчанию по имени папки)")
    ap.add_argument("--profile", type=Profile, default=None, help="Профиль записи (по умолчанию из паспорта)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="Свидетель и критик живые, если нет в записи")
    mode.add_argument("--no-evidence", action="store_true", help="Без шага 6 (те же находки поиска)")
    mode.add_argument(
        "--reuse", action="store_true", help="Ответы шага 6 из записи по шагу и номеру, даже если промпт другой"
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="Только промпты шага 6 в out/prompts, без модели"
    )
    ap.add_argument("--critic", action="store_true", help="Включить критика находок (по умолчанию выключен)")
    args = ap.parse_args(argv)

    recording = args.recording / "run" if (args.recording / "run").is_dir() else args.recording
    report = json.loads((recording.parent / "report.json").read_text(encoding="utf-8"))
    passport = report["passport"]
    profile = args.profile or Profile(passport["profile"])
    source = args.input or _input_for(args.recording)
    cfg = load_config(
        source, args.out / "report.md", profile=profile, passes=passport["passes"], critic=args.critic
    )
    hybrid = Hybrid(
        recording,
        live=args.live,
        dump=args.out / "prompts" if args.dry_run or args.live else None,
        reuse=args.reuse,
    )

    def client(_: RunConfig) -> tuple[Hybrid, list[Diagnostic]]:
        return hybrid, []

    pipeline.make_client = client  # type: ignore[assignment]
    if args.no_evidence:
        pipeline.gather = _no_evidence  # type: ignore[assignment]
    outcome = pipeline.run(cfg)
    print(f"[39] {outcome.message}")
    print(f"[39] живых вызовов: {len(hybrid.live_calls)} {hybrid.live_calls}")
    ev = outcome.report.metrics.evidence if outcome.report else None
    print(f"[39] шаг 6: {ev.model_dump() if ev else 'не вызывался'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
