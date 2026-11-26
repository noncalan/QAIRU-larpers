"""Цикл уточнения на записанном живом прогоне (#42): MD-запись #36 плюс живой вызов сверки с ответами.

``tests/fixtures/e2e/md/run/`` содержит и ответ шага ``clarify`` на ``examples/answers.yaml`` (записан
26.11.2026 одним живым вызовом, остальное из кеша той же записи). Тест пересобирает прогон 2 бэкендом
``replay`` и сравнивает ``report.json`` со снапшотом ``tests/fixtures/e2e/md/report_answers.json``. Так видно,
что пример для README и демо воспроизводится без модели, а правка кода, меняющая итог, заметна в CI.

Обновить снапшот: ``UPDATE_E2E_SNAPSHOT=1 uv run pytest tests/test_e2e_clarify.py``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REQUIREMENTS, ROOT
from test_e2e_replay import E2E, STALE_RECORDINGS, UPDATE_ENV, _differences, _normalize, _rel
from validator.errors import LLM_REPLAY_BY_POSITION
from validator.models import ClarifyEffect, DocKind, ExitCode, Report

ANSWERS = ROOT / "examples" / "answers.yaml"
SNAPSHOT = E2E / "md" / "report_answers.json"


@pytest.fixture(scope="module")
def replayed(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, int]:
    out = tmp_path_factory.mktemp("e2e-answers")
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["PYTHONIOENCODING"] = "utf-8"
    args = [
        _rel(REQUIREMENTS[DocKind.MD]),
        str(out / "report.md"),
        "--backend",
        "replay",
        "--replay",
        _rel(E2E / "md" / "run"),
        "--answers",
        _rel(ANSWERS),
    ]
    result = subprocess.run(
        [sys.executable, "-m", "validator", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
        timeout=300,
        check=False,
    )
    assert "Traceback" not in result.stderr, result.stderr
    return out, result.returncode


def _report(out: Path) -> Report:
    return Report.model_validate_json((out / "report.json").read_text(encoding="utf-8"))


STALE = pytest.mark.xfail(
    DocKind.MD in STALE_RECORDINGS,
    reason=f"запись MD отстала: {STALE_RECORDINGS.get(DocKind.MD)}; examples/answers.yaml под старые номера",
    strict=True,
)
"""Пока MD-запись в ``STALE_RECORDINGS``, вопросы проигрываются по номеру вызова и ответы примера не совпадают
с номерами вопросов: проверки ключей и смысла ответов ожидаемо падают, снапшот сравнивается."""


@STALE
def test_replay_with_answers_finds_every_answer_by_key(replayed: tuple[Path, int]) -> None:
    out, code = replayed
    report = _report(out)

    assert code == ExitCode.OK
    assert LLM_REPLAY_BY_POSITION not in {d.code for d in report.diagnostics}
    assert report.passport.calls_failed == 0


@STALE
def test_answers_close_findings_and_leave_unanswered_questions_open(replayed: tuple[Path, int]) -> None:
    out, _ = replayed
    report = _report(out)
    c = report.clarification

    assert c is not None and c.reviewed
    assert c.by_effect(ClarifyEffect.CLOSED)
    assert c.after is not None and c.before is not None
    assert c.after.findings < c.before.findings
    assert c.after.open_questions < c.before.open_questions
    open_ids = {q.id for q in report.questions}
    unanswered = {a.question_id for a in c.answers if not a.kind.answered}
    assert open_ids == unanswered
    closed = {e.finding_id for e in c.by_effect(ClarifyEffect.CLOSED)}
    assert not closed & {f.id for f in report.verification.findings}  # type: ignore[union-attr]


def test_replay_with_answers_matches_snapshot(replayed: tuple[Path, int]) -> None:
    out, _ = replayed
    actual_path = out / "report.json"
    if os.environ.get(UPDATE_ENV):
        shutil.copyfile(actual_path, SNAPSHOT)
    paths = [REQUIREMENTS[DocKind.MD], E2E / "md" / "run", ANSWERS]

    actual = _normalize(json.loads(actual_path.read_text(encoding="utf-8")), paths)
    expected = _normalize(json.loads(SNAPSHOT.read_text(encoding="utf-8")), paths)

    differences = _differences(expected, actual)
    if differences:
        pytest.fail(
            f"report.json прогона с ответами разошёлся со снапшотом {SNAPSHOT.relative_to(ROOT)}:\n"
            + "\n".join(differences)
            + f"\nЕсли так задумано: {UPDATE_ENV}=1 uv run pytest tests/test_e2e_clarify.py",
            pytrace=False,
        )
