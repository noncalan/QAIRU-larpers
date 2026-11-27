"""Паспорт и первая страница глазами жюри (#170, PR 3).

- При replay «Длительность 0,4 с» читалась как прогон агента за 0,4 с: рядом теперь время модели при
  записи, взятое из ``calls.jsonl`` записи, а не из кода.
- «незначительно 16» и «Незначительные нарушения (15)» в одной строке выглядели как ошибка счёта.
- Дыра в нумерации вопросов (Q-09) объясняется одной строкой в итоге.
- Числа по-русски: «$2,75», «205 814 байт», «0,4 с».
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pymupdf

from conftest import REQUIREMENTS, ROOT
from validator.config import load_config
from validator.models import Backend, DocKind, ReplaySource, Report, Severity
from validator.pipeline import run
from validator.report.fmt import digits, money, seconds
from validator.report.passport_view import TECH_TITLE, duration_line, passport_rows
from validator.report.render_md import render_md, withdrawn_line
from validator.report.render_pdf import NBSP, keep_percent, render_pdf
from validator.report.triage import review_line

RECORDING = ROOT / "tests" / "fixtures" / "e2e" / "md" / "run"
FIXTURES = ROOT / "tests" / "fixtures" / "report"
STEP_SECONDS = 10


def load(name: str) -> Report:
    return Report.model_validate_json((FIXTURES / f"{name}.report.json").read_text(encoding="utf-8"))


def test_numbers_are_written_the_russian_way() -> None:
    assert money(2.75) == "$2,75"
    assert digits(205814) == "205 814"
    assert digits(984) == "984"
    assert seconds(0.38) == "0,38"
    assert seconds(240.5) == "240,5"
    assert seconds(752.0) == "752"
    assert seconds(None) == "—"


def _with_replay(report: Report, model_seconds: float | None) -> Report:
    replay = ReplaySource(path="reports/v0.9-rc2/run", responses=9, model_seconds=model_seconds)
    passport = report.passport.model_copy(
        update={"replay": replay, "duration_s": 0.38, "calls_replayed": 8, "calls_total": 8}
    )
    return report.model_copy(update={"passport": passport})


def test_replay_duration_shows_rebuild_and_recorded_model_time() -> None:
    report = _with_replay(load("full"), model_seconds=752.4)

    line = duration_line(report)

    assert line == "0,38 с (пересборка по записи; при записи модель работала 752 с)"
    assert f"| Длительность | {line} |" in render_md(report)


def test_replay_without_timing_in_recording_still_says_rebuild() -> None:
    assert duration_line(_with_replay(load("full"), model_seconds=None)) == "0,38 с (пересборка по записи)"


def test_live_run_duration_is_unchanged() -> None:
    assert duration_line(load("full")) == "240,5 с"


def test_passport_keeps_task_items_on_top_and_moves_tech_details_below() -> None:
    main, tech = passport_rows(load("full"), "—")
    top = [name for name, _ in main]
    below = [name for name, _ in tech]

    assert top[:4] == ["Документ", "Дата прогона", "Модели", "Версия агента"]
    assert {"Длительность", "Цена по тарифу API", "Код завершения"} <= set(top)
    assert {"SHA-256 документа", "Хеш промптов", "Модели по шагам", "Токены"} <= set(below)
    assert not set(top) & set(below)
    md = render_md(load("full"))
    assert md.index("| Код завершения |") < md.index(f"### {TECH_TITLE}") < md.index("| Хеш промптов |")


def _shifted_recording(tmp_path: Path) -> Path:
    """Копия записи e2e: каждый шаг идёт ``STEP_SECONDS`` с, вызовы одного шага параллельно."""
    run_dir = tmp_path / "run"
    shutil.copytree(RECORDING, run_dir)
    base = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    log = run_dir / "calls.jsonl"
    records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    steps = list(dict.fromkeys(r["step"] for r in records))
    for r in records:
        start = base + timedelta(hours=steps.index(r["step"]))
        r["started_at"] = start.isoformat().replace("+00:00", "Z")
        r["duration_ms"] = STEP_SECONDS * 1000
    log.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return run_dir


def test_replay_takes_model_time_from_recording_per_step_of_served_answers(tmp_path: Path) -> None:
    """Параллельные вызовы шага считаются отрезком, а сверка с ответами без ``--answers`` не входит."""
    cfg = load_config(
        REQUIREMENTS[DocKind.MD],
        tmp_path / "out" / "report.md",
        backend=Backend.REPLAY,
        replay_dir=_shifted_recording(tmp_path),
    )

    outcome = run(cfg)

    assert outcome.report is not None
    replay = outcome.report.passport.replay
    served_steps = {st.step for st in outcome.report.metrics.steps if st.replayed}
    assert replay is not None and "clarify" not in served_steps
    assert replay.model_seconds == STEP_SECONDS * len(served_steps)
    md = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert f"при записи модель работала {STEP_SECONDS * len(served_steps)} с" in md


def test_withdrawn_question_inside_numbering_gets_one_line() -> None:
    report = load("full")
    gap = report.questions_withdrawn[0].question.model_copy(update={"id": "Q-02"})
    kept = [q.model_copy(update={"id": "Q-03" if q.id == "Q-02" else q.id}) for q in report.questions]
    withdrawn = [report.questions_withdrawn[0].model_copy(update={"question": gap})]
    report = report.model_copy(update={"questions": kept, "questions_withdrawn": withdrawn})

    line = withdrawn_line(report)

    assert line == (
        "Номер Q-02 пропущен: этот вопрос сняла самопроверка, ответ есть в документе "
        "(раздел 6, «Снятые вопросы»)."
    )
    assert f"- {line}" in render_md(report)


def test_withdrawn_last_number_leaves_no_gap_and_no_line() -> None:
    report = load("full")  # снят Q-04, остались Q-01–Q-03
    assert withdrawn_line(report) is None


def test_review_line_explains_minor_findings_kept_in_main_list() -> None:
    """Как в отчёте на сдачу: «незначительно 16», в блоке 15, одна осталась в основном списке."""
    base = load("full").verification.summary  # type: ignore[union-attr]
    s = base.model_copy(update={"by_severity": {**base.by_severity, Severity.MINOR: 16}})

    line = review_line(s, optional=15)
    all_in_block = review_line(s, optional=16)
    two_kept = review_line(s, optional=14)

    assert line is not None and all_in_block is not None and two_kept is not None
    assert "незначительно 16" in line and "Незначительные нарушения (15)" in line
    assert "Ещё 1 незначительная находка осталась в основном списке" in line
    assert "в основном списке" not in all_in_block
    assert "Ещё 2 незначительные находки остались в основном списке" in two_kept


def test_pdf_keeps_number_and_percent_on_one_line() -> None:
    """В PDF «сверено 100» и «%» разъезжались по строкам: перед «%» теперь неразрывный пробел."""
    assert keep_percent({"a": ["цитат сверено 100 %", 3], "b": "5 % и 7,5 %"}) == {
        "a": [f"цитат сверено 100{NBSP}%", 3],
        "b": f"5{NBSP}% и 7,5{NBSP}%",
    }
    doc = pymupdf.open(stream=render_pdf(load("full")), filetype="pdf")
    lines = [line for page in doc for line in page.get_text().splitlines()]

    assert not [line for line in lines if line.lstrip().startswith("%")]
