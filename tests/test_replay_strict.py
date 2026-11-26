"""#132: replay записи не выдаёт ответы модели про чужой вход за отчёт о своём.

Запись ``tests/fixtures/e2e/md/run`` сделана на ``data/input/requirements.md`` с ``examples/answers.yaml``.
Другой документ или другие ответы автора дают другие ключи запросов: в записи на них ответа нет, и отчёт
честно неполный, а не собран из ответов про другой вход.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from conftest import REQUIREMENTS, ROOT
from validator.config import load_config
from validator.errors import LLM_CLARIFY_FAILED, LLM_REPLAY_BY_POSITION, LLM_REPLAY_MISSING
from validator.models import Backend, DocKind, ExitCode
from validator.pipeline import run

RECORDING = ROOT / "tests" / "fixtures" / "e2e" / "md" / "run"
OTHER_DOCUMENT = ROOT / "docs" / "case" / "task.md"
EXAMPLE_ANSWERS = ROOT / "examples" / "answers.yaml"


def test_replay_of_recording_on_another_document_is_a_model_failure(tmp_path: Path) -> None:
    cfg = load_config(OTHER_DOCUMENT, tmp_path / "report.md", backend=Backend.REPLAY, replay_dir=RECORDING)

    outcome = run(cfg)

    assert outcome.exit_code is ExitCode.MODEL
    assert outcome.report is not None
    codes = {d.code for d in outcome.report.diagnostics}
    assert LLM_REPLAY_MISSING in codes and LLM_REPLAY_BY_POSITION not in codes
    calls = outcome.report.passport.calls_total
    assert calls > 0 and outcome.report.passport.calls_failed == calls  # ни одного ответа про чужой документ
    assert "другого документа" in (tmp_path / "report.md").read_text(encoding="utf-8")


def test_replay_with_other_answers_does_not_apply_recorded_clarification(tmp_path: Path) -> None:
    """Пример из README со своими ответами: сверка записана для других ответов и не применяется."""
    own = yaml.safe_load(EXAMPLE_ANSWERS.read_text(encoding="utf-8"))
    for q in own["questions"]:
        q["answer"] = "B" if q["id"] in ("Q-01", "Q-05") else None
    answers = tmp_path / "own.yaml"
    answers.write_text(yaml.safe_dump(own, allow_unicode=True), encoding="utf-8")
    md = REQUIREMENTS[DocKind.MD]

    before = run(load_config(md, tmp_path / "a" / "report.md", backend=Backend.REPLAY, replay_dir=RECORDING))
    after = run(
        load_config(
            md,
            tmp_path / "b" / "report.md",
            backend=Backend.REPLAY,
            replay_dir=RECORDING,
            answers_path=answers,
        )
    )

    assert before.report is not None and after.report is not None
    c = after.report.clarification
    assert c is not None and not c.reviewed and c.effects == []
    assert LLM_CLARIFY_FAILED in {d.code for d in after.report.diagnostics}
    assert after.report.verification is not None and before.report.verification is not None
    assert after.report.verification.summary.critical == before.report.verification.summary.critical
