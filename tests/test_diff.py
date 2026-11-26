"""Сравнение версий документа ``validator-diff`` (#59): сопоставление, классы находок, CLI. Без сети.

Синтетические отчёты собраны из общих формулировок требований, не из проверяемого документа.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import ROOT
from validator.diff import (
    FindingClass,
    ReportReadError,
    ReqStatus,
    compare,
    load_document_text,
    load_report,
    render_json,
    render_markdown,
    totals,
)
from validator.diff.load import ReqView
from validator.diff.match import REQ_SIMILARITY, match_requirements
from validator.models import ExitCode
from validator.text import stable_key

RC1 = ROOT / "reports" / "v0.9-rc1" / "report.json"
RC2 = ROOT / "reports" / "v0.9-rc2" / "report.json"

LOGIN = "Пользователь входит в систему по логину и паролю."
EXPORT = "Система быстро выгружает отчёт в CSV."
EXPORT_FIXED = "Система выгружает отчёт в CSV за 5 секунд при 10 000 строк."
AUDIT = "Каждое действие администратора записывается в журнал аудита."
BACKUP = "Резервная копия базы создаётся каждую ночь."
NOTIFY = "Система уведомляет пользователя о важных событиях."
NOTIFY_NEW = "Система уведомляет пользователя о событиях как можно скорее."
SECTION = ["Требования", "Функциональные"]


def req(rid: str, quote: str, source_id: str | None = None) -> dict[str, Any]:
    return {
        "id": rid,
        "quote": quote,
        "stable_key": stable_key(quote),
        "source_id": source_id,
        "source_locator": "",
        "section_path": SECTION,
    }


def finding(
    fid: str,
    quote: str,
    reqs: list[str],
    *,
    prop: str = "IND-UNAMB",
    severity: str = "major",
    scope: str = "requirement",
) -> dict[str, Any]:
    return {
        "id": fid,
        "property": prop,
        "defect_type": "vague_term",
        "verdict": "violation",
        "scope": scope,
        "severity": severity,
        "title": f"Находка {fid}",
        "explanation": "пояснение",
        "quotes": [{"text": quote, "match": "exact", "score": 100.0, "located": quote, "anchor": None}],
        "requirement_ids": reqs,
        "section_path": SECTION if scope == "set" else [],
    }


def report(
    reqs: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    *,
    version: str = "v1.0",
    sha: str = "aaa",
    light: str = "red",
    models: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "status": "complete",
        "passport": {
            "document_name": "spec.md",
            "document_sha256": sha,
            "agent_version": version,
            "prompts_sha256": "p" * 64,
            "started_at": "2026-11-26T10:00:00Z",
            "profile": "standard",
            "passes": 1,
            "models": models or {"search": "claude-opus-5-5 · medium"},
            "cost_usd": 1.5,
        },
        "segmentation": {"requirements": reqs},
        "verification": {"findings": findings, "traffic_light": light},
        "conclusion": {"traffic_light": light, "headline": "Итог"},
        "questions": [],
    }


def write(tmp_path: Path, name: str, data: Any) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def diff(tmp_path: Path, old: dict[str, Any], new: dict[str, Any], texts: tuple[str, str] | None = None):  # type: ignore[no-untyped-def]
    a = load_report(write(tmp_path, "old.json", old))
    b = load_report(write(tmp_path, "new.json", new))
    return compare(a, b, *(texts or (None, None)))


def classes(result: Any) -> dict[str, FindingClass]:
    return {(d.old or d.new).id + ("_old" if d.old else "_new"): d.cls for d in result.findings}


# --- требования ---------------------------------------------------------------------------------


def test_unchanged_requirements_match_by_stable_key_even_if_renumbered(tmp_path: Path) -> None:
    old = report([req("R-01", LOGIN), req("R-02", AUDIT)], [])
    new = report([req("R-01", BACKUP), req("R-02", LOGIN), req("R-03", AUDIT)], [], sha="bbb")

    result = diff(tmp_path, old, new)

    pairs = {(p.old.id if p.old else None, p.new.id if p.new else None): p for p in result.requirements}
    assert pairs[("R-01", "R-02")].status is ReqStatus.UNCHANGED
    assert pairs[("R-01", "R-02")].how.value == "stable_key"
    assert pairs[(None, "R-01")].status is ReqStatus.ADDED


def test_edited_requirement_matches_by_similarity(tmp_path: Path) -> None:
    old = report([req("R-01", NOTIFY)], [])
    new = report([req("R-01", NOTIFY_NEW)], [], sha="bbb")

    (pair,) = diff(tmp_path, old, new).requirements

    assert pair.status is ReqStatus.CHANGED
    assert pair.how.value == "similarity"
    assert pair.score is not None and pair.score >= REQ_SIMILARITY


def test_edited_requirement_matches_by_source_id_when_text_differs_a_lot(tmp_path: Path) -> None:
    old = report([req("R-01", EXPORT, "REQ-7")], [])
    new = report([req("R-01", "Выгрузка отчёта: формат CSV, не дольше 5 секунд.", "REQ-7")], [], sha="b")

    (pair,) = diff(tmp_path, old, new).requirements

    assert (pair.status, pair.how.value) == (ReqStatus.CHANGED, "source_id")


def test_removed_and_added_requirements_below_similarity(tmp_path: Path) -> None:
    old = report([req("R-01", LOGIN), req("R-02", BACKUP)], [])
    new = report([req("R-01", LOGIN), req("R-02", AUDIT)], [], sha="bbb")

    statuses = sorted(p.status.value for p in diff(tmp_path, old, new).requirements)

    assert statuses == ["added", "removed", "unchanged"]


def test_requirement_order_follows_old_version_then_added() -> None:
    def view(rid: str, quote: str) -> ReqView:
        return ReqView(rid, quote, stable_key(quote), None, "", ())

    pairs = match_requirements(
        [view("R-01", LOGIN), view("R-02", AUDIT)], [view("R-01", AUDIT), view("R-02", BACKUP)]
    )

    assert [p.old.id if p.old else None for p in pairs] == ["R-01", "R-02", None]


# --- классы находок -----------------------------------------------------------------------------


def _two_versions() -> tuple[dict[str, Any], dict[str, Any]]:
    """Старая версия: 4 находки. Новая: экспорт исправлен, уведомление правили неудачно."""
    old = report(
        [req("R-01", LOGIN), req("R-02", EXPORT), req("R-03", AUDIT), req("R-04", NOTIFY)],
        [
            finding("F-01", "быстро выгружает", ["R-02"], severity="critical"),
            finding("F-02", LOGIN, ["R-01"], prop="IND-COMPL"),
            finding("F-03", AUDIT, ["R-03"], prop="IND-VERIF"),
            finding("F-04", "о важных событиях", ["R-04"]),
        ],
    )
    new = report(
        [req("R-01", LOGIN), req("R-02", EXPORT_FIXED), req("R-03", AUDIT), req("R-04", NOTIFY_NEW)],
        [
            finding("F-01", LOGIN, ["R-01"], prop="IND-COMPL"),
            finding("F-02", "как можно скорее", ["R-04"], prop="IND-VERIF", severity="critical"),
            finding("F-03", "в журнал аудита", ["R-03"], prop="IND-NEC", severity="minor"),
        ],
        sha="bbb",
        light="yellow",
    )
    return old, new


def test_each_finding_class_is_detected(tmp_path: Path) -> None:
    old, new = _two_versions()

    got = classes(diff(tmp_path, old, new))

    assert got == {
        "F-01_old": FindingClass.FIXED,  # место изменено, находки нет
        "F-02_old": FindingClass.REMAINED,
        "F-03_old": FindingClass.VANISHED,  # цитата осталась дословно, находки нет
        "F-04_old": FindingClass.FIXED,  # требование правили, находки по нему с этим свойством нет
        "F-02_new": FindingClass.NEW_CHANGED,  # цитата из правки: возможная регрессия
        "F-03_new": FindingClass.NEW_UNCHANGED,  # место не менялось: шум модели
    }


def test_vanished_without_edit_is_not_counted_as_fixed(tmp_path: Path) -> None:
    old, new = _two_versions()

    t = totals(diff(tmp_path, old, new))

    assert (t.fixed, t.old_findings, t.fixed_critical, t.old_critical) == (2, 4, 1, 1)
    assert (t.vanished, t.noise_new, t.regressions, t.regressions_critical) == (1, 1, 1, 1)


def test_document_texts_decide_fixed_for_quotes_outside_requirements(tmp_path: Path) -> None:
    example = "Пример: выгрузка 10 строк занимает ровно мгновение."
    old = report([req("R-01", LOGIN)], [finding("F-01", example, [], scope="set")])
    new = report([req("R-01", LOGIN)], [], sha="bbb")
    old_text = f"{LOGIN}\n\n{example}\n"
    new_text = f"{LOGIN}\n\nПример: выгрузка 10 строк занимает 2 секунды.\n"

    without = classes(diff(tmp_path, old, new))
    with_texts = classes(diff(tmp_path, old, new, (old_text, new_text)))

    assert without["F-01_old"] is FindingClass.VANISHED  # без текстов место не сверить, не засчитываем
    assert with_texts["F-01_old"] is FindingClass.FIXED


def test_quote_match_ignores_case_quotes_and_line_breaks(tmp_path: Path) -> None:
    old = report([req("R-01", AUDIT)], [finding("F-01", "в журнал\nаудита", ["R-01"])])
    new = report([req("R-01", AUDIT)], [finding("F-07", "В ЖУРНАЛ аудита", [])], sha="bbb")

    (d,) = diff(tmp_path, old, new).findings

    assert d.cls is FindingClass.REMAINED
    assert (d.old.id, d.new.id) == ("F-01", "F-07")  # type: ignore[union-attr]


def test_remained_on_edited_requirement_says_edit_did_not_help(tmp_path: Path) -> None:
    old = report([req("R-01", NOTIFY)], [finding("F-01", "уведомляет пользователя", ["R-01"])])
    new = report([req("R-01", NOTIFY_NEW)], [finding("F-01", "уведомляет пользователя", ["R-01"])], sha="b")

    (d,) = diff(tmp_path, old, new).findings

    assert d.cls is FindingClass.REMAINED
    assert "изменено, но находка не закрыта" in d.reason


# --- предупреждения и отчёт ---------------------------------------------------------------------


def test_different_agent_versions_and_models_give_warnings(tmp_path: Path) -> None:
    old = report([req("R-01", LOGIN)], [], version="v0.9-rc1")
    new = report([req("R-01", LOGIN)], [], version="v1.0", sha="bbb", models={"search": "другая · high"})

    warnings = " ".join(diff(tmp_path, old, new).warnings)

    assert "разными версиями агента (v0.9-rc1 и v1.0" in warnings
    assert "разными моделями" in warnings


def test_same_agent_and_documents_given_give_no_warnings(tmp_path: Path) -> None:
    old, new = _two_versions()

    assert diff(tmp_path, old, new, ("a", "b")).warnings == ()


def test_markdown_first_page_answers_can_we_start(tmp_path: Path) -> None:
    old, new = _two_versions()

    md = render_markdown(diff(tmp_path, old, new))

    first = md.split("## 1. Паспорт сравнения")[0]
    assert "🔴 → 🟡" in first
    assert "Можно ли теперь начинать разработку?** Нет" in first
    assert "Исправлено находок: **2 из 4**, из них критичных **1 из 1**" in first
    assert (
        md.index("### 3.1. Критичные") < md.index("### 3.2. Новое на изменённом месте") < md.index("### 3.3")
    )
    assert "## 4. Нестабильность модели" in md


def test_json_is_serializable_and_has_summary(tmp_path: Path) -> None:
    old, new = _two_versions()

    data = json.loads(json.dumps(render_json(diff(tmp_path, old, new)), ensure_ascii=False))

    assert data["summary"]["fixed"] == 2
    assert data["summary"]["regressions_critical"] == 1
    assert {f["class"] for f in data["findings"]} == {c.value for c in FindingClass}


# --- устойчивость входа -------------------------------------------------------------------------


def test_unknown_fields_are_tolerated(tmp_path: Path) -> None:
    old, new = _two_versions()
    new["future_section"] = {"x": 1}
    new["verification"]["findings"][0]["new_field"] = [1, 2]
    new["passport"]["something_new"] = "y"

    assert len(diff(tmp_path, old, new).findings) == 6


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "пустой"),
        ("{oops", "не читается как JSON"),
        ("[1, 2]", "не объект JSON"),
        ('{"status": "complete"}', "нет поля passport"),
        ('{"passport": {"agent_version": "v"}}', "нет нарезки или проверки"),
    ],
)
def test_broken_report_gives_clear_message(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ReportReadError) as err:
        load_report(path)

    assert message in err.value.message


def test_missing_finding_property_names_the_field(tmp_path: Path) -> None:
    old, _ = _two_versions()
    del old["verification"]["findings"][0]["property"]

    with pytest.raises(ReportReadError) as err:
        load_report(write(tmp_path, "old.json", old))

    assert "verification.findings[F-01].property" in err.value.message


# --- настоящие отчёты ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [RC1, RC2], ids=["rc1", "rc2"])
def test_real_report_against_itself_has_nothing_fixed(path: Path) -> None:
    if not path.exists():
        pytest.skip(f"{path.parent.name} ещё не в репозитории")
    view = load_report(path)

    result = compare(view, view)
    t = totals(result)

    assert t.remained == len(view.findings) > 0
    assert (t.fixed, t.vanished, t.regressions, t.noise_new) == (0, 0, 0, 0)
    assert all(p.status is ReqStatus.UNCHANGED for p in result.requirements)


# --- CLI ----------------------------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "validator.diff", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_cli_writes_markdown_and_json(tmp_path: Path) -> None:
    old, new = _two_versions()
    out, js = tmp_path / "out" / "diff.md", tmp_path / "diff.json"

    proc = _cli(
        str(write(tmp_path, "o.json", old)), str(write(tmp_path, "n.json", new)), str(out), "--json", str(js)
    )

    assert proc.returncode == ExitCode.OK, proc.stderr
    assert "Исправлено 2 из 4" in proc.stdout
    assert out.read_text(encoding="utf-8").startswith("# Сравнение версий документа")
    assert json.loads(js.read_text(encoding="utf-8"))["summary"]["old_findings"] == 4


@pytest.mark.parametrize(
    "args",
    [[], ["a.json", "b.json", "out.txt"], ["a.json", "b.json", "out.md", "--old-doc", "x.md"]],
    ids=["no-args", "wrong-suffix", "one-doc"],
)
def test_cli_bad_arguments_exit_2(tmp_path: Path, args: list[str]) -> None:
    proc = _cli(*args)

    assert proc.returncode == ExitCode.USAGE
    assert "Traceback" not in proc.stderr


def test_cli_unreadable_report_exit_4_without_traceback(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")

    proc = _cli(str(bad), str(bad), str(tmp_path / "d.md"))

    assert proc.returncode == ExitCode.INPUT
    assert "не читается как JSON" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_cli_reads_document_versions(tmp_path: Path) -> None:
    old, new = _two_versions()
    (tmp_path / "v1.md").write_text(f"# Spec\n\n{LOGIN}\n\n{EXPORT}\n", encoding="utf-8")
    (tmp_path / "v2.md").write_text(f"# Spec\n\n{LOGIN}\n\n{EXPORT_FIXED}\n", encoding="utf-8")
    out = tmp_path / "d.md"

    proc = _cli(
        str(write(tmp_path, "o.json", old)),
        str(write(tmp_path, "n.json", new)),
        str(out),
        "--old-doc",
        str(tmp_path / "v1.md"),
        "--new-doc",
        str(tmp_path / "v2.md"),
    )

    assert proc.returncode == ExitCode.OK, proc.stderr
    assert "| Тексты документов | переданы |" in out.read_text(encoding="utf-8")


def test_verbatim_quote_beats_edited_neighbour_sentence(tmp_path: Path) -> None:
    old_req = f"{LOGIN} {EXPORT}"
    new_req = f"{LOGIN} {EXPORT_FIXED}"
    old = report([req("R-01", old_req)], [finding("F-01", LOGIN, ["R-01"], prop="IND-COMPL")])
    new = report([req("R-01", new_req)], [], sha="bbb")

    (d,) = diff(tmp_path, old, new, (old_req, new_req)).findings

    assert d.cls is FindingClass.VANISHED  # требование правили, но не в этой фразе


def test_unknown_quote_falls_back_to_requirement_status(tmp_path: Path) -> None:
    old = report([req("R-01", NOTIFY)], [finding("F-01", "цитата, которой нет в тексте", ["R-01"])])
    new = report([req("R-01", NOTIFY_NEW)], [], sha="bbb")

    (d,) = diff(tmp_path, old, new).findings

    assert d.cls is FindingClass.FIXED
    assert "R-01 изменено" in d.reason


def test_parts_of_one_numbered_item_match_with_soft_similarity(tmp_path: Path) -> None:
    rewritten = "Отчёт в формате CSV формируется не дольше 5 секунд для 10 000 строк."
    old = report([req("R-01.1", NOTIFY, "REQ-7"), req("R-01.2", EXPORT, "REQ-7")], [])
    new = report([req("R-01.1", NOTIFY_NEW, "REQ-7"), req("R-01.2", rewritten, "REQ-7")], [], sha="bbb")

    pairs = {p.old.id: p for p in diff(tmp_path, old, new).requirements if p.old}

    assert (pairs["R-01.2"].status, pairs["R-01.2"].how.value) == (ReqStatus.CHANGED, "source_id")
    assert pairs["R-01.2"].score is not None and pairs["R-01.2"].score < REQ_SIMILARITY


def test_same_quote_and_severity_with_other_property_is_the_same_finding(tmp_path: Path) -> None:
    old = report([req("R-01", AUDIT)], [finding("F-01", AUDIT, ["R-01"], prop="SET-CONS")])
    new = report([req("R-01", AUDIT)], [finding("F-05", AUDIT, ["R-01"], prop="SET-COMPL")], sha="bbb")

    (d,) = diff(tmp_path, old, new).findings

    assert d.cls is FindingClass.REMAINED
    assert "SET-CONS → SET-COMPL" in d.reason


def test_new_critical_does_not_hide_behind_old_minor_on_same_quote(tmp_path: Path) -> None:
    old = report([req("R-01", AUDIT)], [finding("F-01", AUDIT, ["R-01"], prop="SET-COMPR", severity="minor")])
    new = report(
        [req("R-01", AUDIT)],
        [finding("F-01", AUDIT, ["R-01"], prop="IND-COMPL", severity="critical")],
        sha="b",
    )

    got = classes(diff(tmp_path, old, new))

    assert got == {"F-01_old": FindingClass.VANISHED, "F-01_new": FindingClass.NEW_UNCHANGED}


# --- демо «было → стало» ------------------------------------------------------------------------

DEMO = ROOT / "examples" / "diff-demo"


def test_demo_diff_is_reproducible_from_committed_reports() -> None:
    """``examples/diff-demo/diff.json`` получается из лежащих рядом отчётов и документов: демо не врёт."""
    old, new = load_report(DEMO / "v1" / "report.json"), load_report(DEMO / "v2" / "report.json")
    old_text = load_document_text(ROOT / "data" / "input" / "requirements.md")
    new_text = load_document_text(DEMO / "requirements-v2.md")

    result = compare(old, new, old_text, new_text)

    committed = json.loads((DEMO / "diff.json").read_text(encoding="utf-8"))
    assert render_json(result)["summary"] == committed["summary"]
    assert result.warnings == ()  # один код агента, тексты переданы
    assert totals(result).fixed_critical >= 2 and totals(result).regressions >= 1


def test_demo_planted_regression_is_reported_on_changed_place() -> None:
    old, new = load_report(DEMO / "v1" / "report.json"), load_report(DEMO / "v2" / "report.json")
    old_text = load_document_text(ROOT / "data" / "input" / "requirements.md")
    new_text = load_document_text(DEMO / "requirements-v2.md")
    added = [p.new.quote for p in compare(old, new, old_text, new_text).reqs(ReqStatus.ADDED) if p.new]

    regressions = compare(old, new, old_text, new_text).of(FindingClass.NEW_CHANGED)

    quotes = {q.text for d in regressions for q in d.main.quotes}
    assert quotes & set(added)  # находка стоит на требовании, которого не было в старой версии
