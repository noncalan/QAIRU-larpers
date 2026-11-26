"""Самопроверка на настоящих отчётах (#141): порча одного поля ловится, целый отчёт проходит.

Отчёты: снапшоты ``tests/fixtures/e2e/<формат>/report.json``. Их собирает ``--backend replay`` из записанных
ответов модели, ``test_e2e_replay.py`` держит их в актуальном виде; ``md/report_answers.json`` это прогон с
ответами автора. Сеть и модель не нужны. Самопроверка вызывается так же, как в конвейере: с Markdown отчёта,
текстом и блоками документа. Каждая порча из issue #141 трогает одно место отчёта; находку для порчи
выбираем по её свойствам (первая важная, первая критичная), а не по номеру из нашего документа.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import FORMATS, ROOT
from validator.errors import INT_SELFCHECK_FAILED
from validator.models import (
    BlockKind,
    DefectType,
    DocKind,
    ExitCode,
    Finding,
    MergedFinding,
    ParsedDocument,
    Property,
    Quote,
    Report,
    ReportStatus,
    SelfCheck,
    Severity,
    TrafficLight,
    Verdict,
    Verification,
)
from validator.report import render_md
from validator.report.render_pdf import build_view
from validator.report.selfcheck import (
    _model_quotes,
    _place_difference,
    check_report,
    selfcheck_diagnostics,
)
from validator.verify import check, light_with_questions, locate, summarize

E2E = ROOT / "tests" / "fixtures" / "e2e"
NEW_ITEMS = {"traffic", "first_page", "refs", "passport"}
QUOTE_FIELDS = ("quotes_total", "quotes_exact", "quotes_fuzzy", "quotes_not_found")

Real = dict[str, tuple[Report, ParsedDocument]]


@pytest.fixture(scope="module")
def real(requirements_docs: dict[DocKind, ParsedDocument]) -> Real:
    def load(path: Path) -> Report:
        return Report.model_validate_json(path.read_text(encoding="utf-8"))

    reports = {
        kind.value: (load(E2E / kind.value / "report.json"), requirements_docs[kind]) for kind in FORMATS
    }
    md = requirements_docs[DocKind.MD]
    return {**reports, "answers": (load(E2E / "md" / "report_answers.json"), md)}


def run(report: Report, doc: ParsedDocument, *, blocks: bool = True) -> SelfCheck:
    """Как в конвейере: ``check_report(report, render_md(report), doc.text, doc.blocks)``."""
    return check_report(report, render_md(report), doc.text, doc.blocks if blocks else None)


def failed_ids(sc: SelfCheck) -> set[str]:
    return {i.id for i in sc.items if not i.passed}


def failures(sc: SelfCheck, item_id: str) -> list[str]:
    return next(i.failures for i in sc.items if i.id == item_id)


def assert_caught(sc: SelfCheck, item_id: str) -> None:
    assert item_id in failed_ids(sc), [(i.id, i.failures) for i in sc.items]
    assert next(i for i in sc.items if i.id == item_id).integrity
    assert not sc.integrity_ok
    assert INT_SELFCHECK_FAILED in {d.code for d in selfcheck_diagnostics(sc)}


def ver_of(report: Report) -> Verification:
    assert report.verification is not None
    return report.verification


def with_ver(report: Report, **changes: object) -> Report:
    return report.model_copy(update={"verification": ver_of(report).model_copy(update=changes)})


def with_finding(report: Report, pick: Callable[[Finding], bool], **changes: object) -> Report:
    """Отчёт, где первая подходящая находка изменена, а всё остальное как было."""
    findings = list(ver_of(report).findings)
    index = next(n for n, f in enumerate(findings) if pick(f))
    findings[index] = findings[index].model_copy(update=changes)
    return with_ver(report, findings=findings)


def is_critical(f: Finding) -> bool:
    return f.severity is Severity.CRITICAL and f.verdict is Verdict.VIOLATION


# ---------------------------------------------------------------- целые отчёты


@pytest.mark.parametrize("name", ["md", "pdf", "docx", "answers"])
def test_real_report_passes_every_check(real: Real, name: str) -> None:
    report, doc = real[name]

    sc = run(report, doc)

    assert sc.passed, [(i.id, i.failures) for i in sc.items if not i.passed]
    assert {i.id for i in sc.items} >= NEW_ITEMS
    assert selfcheck_diagnostics(sc) == []


def test_real_report_passes_without_blocks_too(real: Real) -> None:
    """Скрипт мутаций из issue вызывает самопроверку с текстом документа, но без блоков."""
    report, doc = real["md"]

    assert run(report, doc, blocks=False).passed


# ---------------------------------------------------------------- мутации из issue #141


def severity_card_changed(report: Report) -> Report:
    """1. Важная находка стала незначительной, а строка «По важности» прежняя."""
    return with_finding(report, lambda f: f.severity is Severity.MAJOR, severity=Severity.MINOR)


def by_severity_moved(report: Report) -> Report:
    """2. В ``by_severity`` пять находок переброшены из «важно» в «незначительно»: сумма та же."""
    s = ver_of(report).summary
    counts = dict(s.by_severity)
    assert counts[Severity.MAJOR] >= 5
    counts[Severity.MAJOR] -= 5
    counts[Severity.MINOR] = counts.get(Severity.MINOR, 0) + 5
    return with_ver(report, summary=s.model_copy(update={"by_severity": counts}))


def critical_demoted_light_kept(report: Report) -> Report:
    """3. Критичная стала важной, сводка пересчитана, а светофор и вывод по-прежнему её называют."""
    changed = with_finding(report, is_critical, severity=Severity.MAJOR)
    ver, old = ver_of(changed), ver_of(report).summary
    assert report.segmentation is not None
    summary = summarize(ver.findings, ver.discarded, report.segmentation, status=ver.requirement_status)
    summary = summary.model_copy(update={k: getattr(old, k) for k in QUOTE_FIELDS})
    return with_ver(changed, summary=summary)


def green_with_criticals(report: Report) -> Report:
    """4. Светофор зелёный и «нарушений не найдено» при критичных нарушениях."""
    green = TrafficLight.GREEN
    headline = f"{green.label[:1].upper()}{green.label[1:]}: нарушений не найдено"
    changed = with_ver(report, traffic_light=green, traffic_reason="нарушений не найдено")
    conclusion = report.conclusion.model_copy(update={"traffic_light": green, "headline": headline})
    return changed.model_copy(update={"conclusion": conclusion})


def fix_first_minor(report: Report) -> Report:
    """5а. «Что исправить первым» из пяти незначительных нарушений."""
    minor = [
        f.id
        for f in ver_of(report).findings
        if f.severity is Severity.MINOR and f.verdict is Verdict.VIOLATION
    ]
    assert len(minor) >= 5
    conclusion = report.conclusion.model_copy(update={"fix_first": minor[:5]})
    return report.model_copy(update={"conclusion": conclusion})


def fix_first_empty(report: Report) -> Report:
    """5б. «Что исправить первым» пусто при критичных нарушениях."""
    return report.model_copy(update={"conclusion": report.conclusion.model_copy(update={"fix_first": []})})


def card_points_to_missing_requirement(report: Report) -> Report:
    """6а. Карточка ссылается на требование, которого нет в нарезке."""
    assert report.segmentation is not None
    assert "R-99" not in {r.id for r in report.segmentation.requirements}
    return with_finding(report, lambda f: bool(f.requirement_ids), requirement_ids=["R-99"])


def question_closes_missing_findings(report: Report) -> Report:
    """6б. Вопрос «закрывает» находки, которых нет."""
    questions = list(report.questions)
    index = next(n for n, q in enumerate(questions) if q.related_findings)
    questions[index] = questions[index].model_copy(update={"related_findings": ["F-98", "F-99"]})
    return report.model_copy(update={"questions": questions})


def anchors_shifted(report: Report) -> Report:
    """7. Строка, страница и абзац якоря каждой цитаты сдвинуты на +500; текст цитаты тот же."""

    def shift(value: int | None) -> int | None:
        return None if value is None else value + 500

    def shifted(q: Quote) -> Quote:
        a = q.anchor
        if a is None:
            return q
        fields = {k: shift(getattr(a, k)) for k in ("line_start", "line_end", "page", "paragraph")}
        return q.model_copy(update={"anchor": a.model_copy(update=fields)})

    findings = [
        f.model_copy(update={"quotes": [shifted(q) for q in f.quotes]}) for f in ver_of(report).findings
    ]
    return with_ver(report, findings=findings)


def requirement_quote_invented(report: Report) -> Report:
    """8а. Цитата требования в таблице нарезки заменена выдуманной."""
    assert report.segmentation is not None
    reqs = list(report.segmentation.requirements)
    reqs[0] = reqs[0].model_copy(update={"quote": "Система поддерживает функцию, которой в документе нет."})
    seg = report.segmentation.model_copy(update={"requirements": reqs})
    return report.model_copy(update={"segmentation": seg})


def requirement_source_id_invented(report: Report) -> Report:
    """8б. «ID в документе» заменён несуществующим."""
    assert report.segmentation is not None
    reqs = list(report.segmentation.requirements)
    reqs[0] = reqs[0].model_copy(update={"source_id": "Ф-777"})
    seg = report.segmentation.model_copy(update={"requirements": reqs})
    return report.model_copy(update={"segmentation": seg})


def passport_calls_inflated(report: Report) -> Report:
    """9а. В паспорте 99 вызовов модели вместо записанных."""
    return report.model_copy(update={"passport": report.passport.model_copy(update={"calls_total": 99})})


def partial_status_with_code_0(report: Report) -> Report:
    """9б. Статус отчёта «частичный» при коде 0."""
    assert report.status is ReportStatus.COMPLETE and report.passport.exit_code is ExitCode.OK
    return report.model_copy(update={"status": ReportStatus.PARTIAL})


def code_5_with_complete_report(report: Report) -> Report:
    """9в. Код 5 при полном отчёте."""
    passport = report.passport.model_copy(update={"exit_code": ExitCode.MODEL})
    return report.model_copy(update={"passport": passport})


def blocking_flags_dropped(report: Report) -> Report:
    """10. У всех находок снято «мешает тесту», а вывод по-прежнему «мешают написать тест-кейсы»."""
    findings = [f.model_copy(update={"blocks_testing": False}) for f in ver_of(report).findings]
    return with_ver(report, findings=findings)


def quotes_total_inflated(report: Report) -> Report:
    """11. «Цитат всего» +10, сводка и метрики подогнаны: сумма точных, нечётких и ненайденных сходится."""
    s = ver_of(report).summary
    lie = {"quotes_total": s.quotes_total + 10, "quotes_not_found": s.quotes_not_found + 10}
    metrics = report.metrics.model_copy(update={"quotes_total": report.metrics.quotes_total + 10})
    return with_ver(report, summary=s.model_copy(update=lie)).model_copy(update={"metrics": metrics})


MUTATIONS = [
    pytest.param(severity_card_changed, "counters", id="01-severity-of-a-card"),
    pytest.param(by_severity_moved, "counters", id="02-by-severity-levels"),
    pytest.param(critical_demoted_light_kept, "traffic", id="03-critical-demoted-light-kept"),
    pytest.param(green_with_criticals, "traffic", id="04-green-with-criticals"),
    pytest.param(fix_first_minor, "first_page", id="05a-fix-first-minor"),
    pytest.param(fix_first_empty, "first_page", id="05b-fix-first-empty"),
    pytest.param(card_points_to_missing_requirement, "refs", id="06a-card-to-missing-requirement"),
    pytest.param(question_closes_missing_findings, "refs", id="06b-question-closes-missing-findings"),
    pytest.param(anchors_shifted, "quotes", id="07-anchor-place-shifted"),
    pytest.param(requirement_quote_invented, "requirements", id="08a-requirement-quote-invented"),
    pytest.param(requirement_source_id_invented, "requirements", id="08b-requirement-id-invented"),
    pytest.param(passport_calls_inflated, "passport", id="09a-passport-calls"),
    pytest.param(partial_status_with_code_0, "passport", id="09b-partial-with-code-0"),
    pytest.param(code_5_with_complete_report, "passport", id="09c-code-5-complete"),
    pytest.param(blocking_flags_dropped, "traffic", id="10-testing-flags-removed"),
    pytest.param(quotes_total_inflated, "counters", id="11-quotes-total"),
]


@pytest.mark.parametrize(("mutate", "item_id"), MUTATIONS)
def test_mutation_from_issue_is_caught(real: Real, mutate: Callable[[Report], Report], item_id: str) -> None:
    report, doc = real["md"]

    assert_caught(run(mutate(report), doc), item_id)


@pytest.mark.parametrize("name", ["pdf", "docx"])
def test_shifted_page_or_paragraph_is_caught_in_pdf_and_docx(real: Real, name: str) -> None:
    report, doc = real[name]

    sc = run(anchors_shifted(report), doc)

    assert_caught(sc, "quotes")
    field = "страница" if name == "pdf" else "абзац"
    assert any(f"{field} " in line for line in failures(sc, "quotes"))


def test_shifted_lines_are_caught_without_blocks(real: Real) -> None:
    report, doc = real["md"]

    assert_caught(run(anchors_shifted(report), doc, blocks=False), "quotes")


def test_card_severity_change_is_caught_in_markdown_line_too(real: Real) -> None:
    report, doc = real["md"]

    lines = failures(run(severity_card_changed(report), doc), "counters")

    assert any(line.startswith("строка «По важности»") for line in lines)
    assert any(line.startswith("по важности в сводке") for line in lines)


def test_demoted_critical_with_fixed_summary_breaks_only_the_light_side(real: Real) -> None:
    """Сводка пересчитана честно, значит ловит именно светофор, а не счётчики."""
    report, doc = real["md"]

    sc = run(critical_demoted_light_kept(report), doc)

    assert "counters" not in failed_ids(sc)
    assert_caught(sc, "traffic")


def test_empty_fix_first_names_the_missing_criticals(real: Real) -> None:
    report, doc = real["md"]
    critical = [f.id for f in ver_of(report).findings if is_critical(f)]
    assert critical

    lines = failures(run(fix_first_empty(report), doc), "first_page")

    assert any(line.startswith("критичные нарушения не в «Что исправить первым»") for line in lines)
    assert all(fid in " ".join(lines) for fid in critical)


# ---------------------------------------------------------------- вопрос, которого нет в списке


def test_missing_question_in_ask_first_does_not_crash_and_is_caught(real: Real) -> None:
    report, doc = real["md"]
    assert "Q-99" not in {q.id for q in report.questions}
    ask_first = [*report.conclusion.ask_first, "Q-99"]
    broken = report.model_copy(
        update={"conclusion": report.conclusion.model_copy(update={"ask_first": ask_first})}
    )

    markdown = render_md(broken)
    view = build_view(broken)
    sc = check_report(broken, markdown, doc.text, doc.blocks)

    assert "**Q-99**" in markdown and "нет в списке вопросов" in markdown
    assert "Q-99" in str(view)
    assert_caught(sc, "refs")
    assert_caught(sc, "first_page")


# ---------------------------------------------------------------- без ложных тревог


def test_note_about_lost_quotes_is_what_the_recount_reads(real: Real) -> None:
    """Число цитат модели у находки, где часть цитат не нашлась, есть только в примечании верификатора."""
    report, doc = real["md"]
    seg = report.segmentation
    assert seg is not None
    merged = MergedFinding(
        mid="M-900",
        property=Property.VERIFIABLE,
        defect_type=DefectType.UNVERIFIABLE,
        quotes=[seg.requirements[0].quote, "Этой фразы в документе нет и не было никогда."],
        explanation="Проверка примечания о ненайденной цитате.",
    )

    finding, quotes = check(merged, doc, seg)

    assert isinstance(finding, Finding)
    assert len(finding.quotes) == 1
    assert _model_quotes(finding) == len(quotes) == 2


def test_empty_cell_quote_keeps_the_row_anchor_and_passes_the_place_check(
    make_md: Callable[[str], ParsedDocument],
) -> None:
    """Цитата пустых ячеек якорится на строку таблицы целиком: место всё равно сходится с документом."""
    doc = make_md("# Согласование\n\n| Подразделение | ФИО | Подпись |\n|---|---|---|\n| Отдел карт | | |\n")
    row = next(b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW)

    quote, _ = locate(doc, "ФИО: (пусто)", [row.id])

    assert quote.verified and quote.anchor is not None and quote.anchor == row.anchor
    assert _place_difference(doc, quote.anchor, blocks_known=True) == ""


def test_light_after_failed_questions_step_is_not_a_false_alarm(real: Real) -> None:
    """Шаг вопросов упал: вопросов в отчёте нет, зелёный свет конвейер сделал жёлтым. Это не ошибка отчёта."""
    report, doc = real["md"]
    assert report.segmentation is not None
    ver = ver_of(report)
    clean = ver.model_copy(
        update={
            "findings": [],
            "summary": summarize([], ver.discarded, report.segmentation),
            "requirement_status": {r.id: [] for r in report.segmentation.requirements},
            "traffic_light": TrafficLight.GREEN,
            "traffic_reason": "нарушений не найдено",
        }
    )
    yellow = light_with_questions(clean, None)
    light = yellow.traffic_light
    headline = f"{light.label[:1].upper()}{light.label[1:]}: {yellow.traffic_reason}"
    conclusion = report.conclusion.model_copy(
        update={"traffic_light": light, "headline": headline, "fix_first": [], "ask_first": []}
    )
    report = report.model_copy(update={"verification": yellow, "questions": [], "conclusion": conclusion})

    sc = run(report, doc)

    assert light is TrafficLight.YELLOW
    assert "traffic" not in failed_ids(sc) and "first_page" not in failed_ids(sc)
