"""Шаг 6 (#39): пример-свидетель и критик находок. Модель предлагает, код проверяет.

Документ маленький и из другой предметной области (бронирование переговорных), ответы модели собраны
вручную в формате ``claude -p``: так видно каждое правило кода отдельно, без живых вызовов.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pymupdf
import pytest

import report_factory as factory
from conftest import REQUIREMENTS
from questions_helpers import cli_payload, make_cfg
from validator import pipeline
from validator.config import load_config
from validator.errors import (
    LLM_CRITIC_DECIDED,
    LLM_CRITIC_FAILED,
    LLM_EVIDENCE_FAILED,
    LLM_WITNESS_CHECKED,
    LLMError,
)
from validator.evidence import gather
from validator.evidence_common import locate_findings
from validator.evidence_critic import ReplyDecision
from validator.evidence_critic import parse_reply as parse_critic
from validator.evidence_critic import select as select_for_critic
from validator.evidence_metrics import evidence_metrics
from validator.evidence_witness import parse_reply as parse_witness
from validator.evidence_witness import select as select_for_witness
from validator.llm import FakeLLM
from validator.models import (
    Backend,
    CriticAction,
    DefectType,
    DiscardReason,
    DocKind,
    ExitCode,
    FindingSource,
    LLMRequest,
    LLMResponse,
    LLMStep,
    MergedFinding,
    ParsedDocument,
    Property,
    Report,
    Severity,
    SourceKind,
)
from validator.questions.candidates import finding_origins
from validator.questions.scoring import priority
from validator.refs import humanize
from validator.report import render_md, render_pdf
from validator.segment import fallback_segmentation
from validator.verify import verify

ROOMS_DOC = """# Требования к бронированию переговорных

## Бронирование

Сотрудник бронирует переговорную не дольше чем на четыре часа подряд.

Бронь можно отменить не позднее чем за час до начала встречи.

## Ограничения

Отмена брони возможна в любое время до начала встречи.

Комната вмещает не больше двенадцати человек.

Система быстро показывает свободные комнаты.
"""

HOUR = "Бронь можно отменить не позднее чем за час до начала встречи."
ANYTIME = "Отмена брони возможна в любое время до начала встречи."
CAPACITY = "Комната вмещает не больше двенадцати человек."
FAST = "Система быстро показывает свободные комнаты."
FOUR_HOURS = "Сотрудник бронирует переговорную не дольше чем на четыре часа подряд."
SCENARIO = "Встреча начинается в 15:00, сотрудник отменяет бронь в 14:30"

PASSES = [FindingSource(kind=SourceKind.PASS, pass_index=i) for i in (1, 2, 3)]


@pytest.fixture
def doc(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(ROOMS_DOC)


def merged(mid: int, quotes: Sequence[str], **fields: Any) -> MergedFinding:
    base: dict[str, Any] = {
        "mid": f"M-{mid:03d}",
        "property": Property.UNAMBIGUOUS,
        "defect_type": DefectType.AMBIGUOUS,
        "quotes": list(quotes),
        "explanation": f"Объяснение находки {mid}.",
        "found_in": 3,
        "of": 3,
        "sources": PASSES,
    }
    return MergedFinding.model_validate({**base, **fields})


def contradiction(mid: int = 1, quotes: Sequence[str] = (HOUR, ANYTIME), **fields: Any) -> MergedFinding:
    risky = {"blocks_testing": True, "severity_hint": Severity.CRITICAL}
    return merged(
        mid,
        quotes,
        property=Property.SET_CONSISTENT,
        defect_type=DefectType.CONTRADICTION,
        **{**risky, **fields},
    )


def missing_capacity(mid: int = 2, **fields: Any) -> MergedFinding:
    """Ложная находка: «вместимость не задана», хотя документ её задаёт в другом разделе (2 прохода из 3)."""
    base = {"blocks_testing": True, "severity_hint": Severity.CRITICAL, "found_in": 2, "sources": PASSES[:2]}
    return merged(
        mid,
        [FOUR_HOURS],
        property=Property.COMPLETE,
        defect_type=DefectType.INCOMPLETE,
        title="Не задано, сколько человек вмещает переговорная",
        **{**base, **fields},
    )


def answer(key: str, records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    body = json.dumps({key: list(records)}, ensure_ascii=False, indent=2)
    return cli_payload(f"## Рассуждение\n\n- Разобрано.\n\n## Ответ\n\n```json\n{body}\n```\n")


def witness_rec(
    mid: str, readings: Sequence[tuple[str, str]], scenario: str | None = SCENARIO
) -> dict[str, Any]:
    return {
        "id": mid,
        "scenario": scenario,
        "readings": [{"quote": q, "reading": "", "expectation": e} for q, e in readings],
        "reason": "",
    }


def decision(mid: str, action: str, basis: str = "", severity: str | None = None) -> dict[str, Any]:
    return {
        "id": mid,
        "decision": action,
        "severity": severity,
        "reason": "Проверено по документу",
        "basis_quote": basis,
    }


def llm(
    witnesses: Sequence[dict[str, Any]] | None = None, decisions: Sequence[dict[str, Any]] | None = None
) -> FakeLLM:
    responses: dict[tuple[LLMStep, int], dict[str, Any]] = {}
    if witnesses is not None:
        responses[(LLMStep.EVIDENCE, 1)] = answer("witnesses", witnesses)
    if decisions is not None:
        responses[(LLMStep.CRITIC, 1)] = answer("decisions", decisions)
    return FakeLLM(root=Path("/nonexistent"), responses=responses)


def run(
    doc: ParsedDocument, tmp_path: Path, found: Sequence[MergedFinding], client: Any, *, critic: bool = True
) -> Any:
    cfg = make_cfg(doc, tmp_path).model_copy(update={"critic": critic})
    return gather(found, doc, client, cfg)


def codes(outcome: Any) -> set[str]:
    return {d.code for d in outcome.diagnostics}


# ---------------------------------------------------------------- свидетель


def test_contradiction_witness_with_two_places_and_different_expectations_is_checked(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    client = llm([witness_rec("M-001", [(HOUR, "отмена отклонена"), (ANYTIME, "бронь отменена")])])

    out = run(doc, tmp_path, [contradiction()], client)

    w = out.findings[0].witness
    assert w is not None and w.checked, w
    assert [r.expectation for r in w.readings] == ["отмена отклонена", "бронь отменена"]
    assert all(r.located is not None and r.located.anchor is not None for r in w.readings)
    assert LLM_WITNESS_CHECKED in codes(out)


def test_witness_with_invented_quote_is_rejected_and_invented_quote_is_dropped(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    invented = "Отмена брони запрещена в день встречи без согласования руководителя."
    client = llm([witness_rec("M-001", [(HOUR, "отмена отклонена"), (invented, "бронь отменена")])])

    w = run(doc, tmp_path, [contradiction()], client).findings[0].witness

    assert w is not None and not w.checked
    assert "цитата не найдена" in w.note
    assert [r.quote for r in w.readings] == [HOUR]


def test_expectations_with_same_words_in_other_order_are_different(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    readings = [(HOUR, "отмена отклонена, бронь не снята"), (ANYTIME, "отмена не отклонена, бронь снята")]

    w = run(doc, tmp_path, [contradiction()], llm([witness_rec("M-001", readings)])).findings[0].witness

    assert w is not None and w.checked, w.note


def test_witness_with_same_expectations_is_rejected(doc: ParsedDocument, tmp_path: Path) -> None:
    client = llm([witness_rec("M-001", [(HOUR, "Бронь отменена."), (ANYTIME, "бронь  ОТМЕНЕНА")])])

    w = run(doc, tmp_path, [contradiction()], client).findings[0].witness

    assert w is not None and not w.checked
    assert "ожидания совпадают" in w.note


def test_contradiction_witness_from_one_place_is_rejected(doc: ParsedDocument, tmp_path: Path) -> None:
    client = llm([witness_rec("M-001", [(HOUR, "отмена отклонена"), (HOUR, "бронь отменена")])])

    w = run(doc, tmp_path, [contradiction()], client).findings[0].witness

    assert w is not None and not w.checked
    assert "меньше двух разных мест" in w.note


def test_ambiguity_witness_may_read_one_place_two_ways(doc: ParsedDocument, tmp_path: Path) -> None:
    found = merged(3, [FAST], scenario="страница открылась за 3 с → быстро или нет?")
    rec = {
        "id": "M-003",
        "scenario": "Список свободных комнат открылся через 3 секунды после запроса",
        "readings": [
            {"quote": FAST, "reading": "быстро — до 1 с", "expectation": "тест провален"},
            {"quote": FAST, "reading": "быстро — до 5 с", "expectation": "тест пройден"},
        ],
    }

    w = run(doc, tmp_path, [found], llm([rec])).findings[0].witness

    assert w is not None and w.checked, w.note
    assert [r.reading for r in w.readings] == ["быстро — до 1 с", "быстро — до 5 с"]


def test_witness_about_other_places_than_the_finding_is_rejected(doc: ParsedDocument, tmp_path: Path) -> None:
    found = merged(4, [FAST], scenario="страница открылась за 3 с")
    client = llm([witness_rec("M-004", [(HOUR, "отмена отклонена"), (ANYTIME, "бронь отменена")])])

    w = run(doc, tmp_path, [found], client).findings[0].witness

    assert w is not None and not w.checked
    assert "мест самой находки" in w.note


def test_model_without_scenario_gives_declined_witness_with_reason(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    rec = {"id": "M-001", "scenario": None, "readings": [], "reason": "Второе место уточняет первое"}

    out = run(doc, tmp_path, [contradiction()], llm([rec]))

    w = out.findings[0].witness
    assert w is not None and not w.checked and not w.scenario
    assert "Второе место уточняет первое" in w.note
    metrics = evidence_metrics(out.findings)
    assert metrics is not None and metrics.witness_declined == 1 and metrics.witness_rejected == 0


def test_witness_is_asked_for_contradictions_and_findings_with_scenario_only(doc: ParsedDocument) -> None:
    lint_only = contradiction(5, found_in=0, sources=[FindingSource(kind=SourceKind.LINT, rule_id="L-1")])
    invented = contradiction(6, quotes=["Такой фразы в документе нет совсем."])
    found = [missing_capacity(), merged(3, [FAST], scenario="сценарий"), contradiction(), lint_only, invented]

    picked = select_for_witness(found, locate_findings(found, doc))

    assert [m.mid for m in picked] == ["M-001", "M-003"]


# ---------------------------------------------------------------- критик


def test_critic_drop_with_basis_elsewhere_goes_to_discarded_with_reason(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    client = llm([], [decision("M-002", "снять", CAPACITY)])

    out = run(doc, tmp_path, [missing_capacity()], client)
    ver = verify(out.findings, doc, fallback_segmentation(doc))

    assert not ver.findings
    [dropped] = ver.discarded
    assert dropped.reason is DiscardReason.CRITIC_REJECTED
    assert dropped.detail == "Проверено по документу"
    critic = dropped.finding.critic
    assert critic is not None and critic.basis is not None and critic.basis.located == CAPACITY
    assert ver.summary.discarded_by_reason == {DiscardReason.CRITIC_REJECTED: 1}
    assert LLM_CRITIC_DECIDED in codes(out)


def test_critic_does_not_drop_a_finding_that_every_pass_found(doc: ParsedDocument, tmp_path: Path) -> None:
    client = llm([], [decision("M-002", "снять", CAPACITY)])

    out = run(doc, tmp_path, [missing_capacity(found_in=3, sources=PASSES)], client)
    [finding] = verify(out.findings, doc, fallback_segmentation(doc)).findings

    critic = finding.critic
    assert critic is not None and critic.action is CriticAction.DOWNGRADE
    assert critic.proposed is CriticAction.DROP and "все проходы" in critic.note
    assert finding.severity is Severity.MAJOR


def test_critic_is_off_by_default_and_report_says_so(doc: ParsedDocument, tmp_path: Path) -> None:
    client = llm([witness_rec("M-001", [(HOUR, "отмена отклонена"), (ANYTIME, "бронь отменена")])])

    out = run(doc, tmp_path, [contradiction(), missing_capacity()], client, critic=False)

    assert [r.step for r in client.requests] == [LLMStep.EVIDENCE]
    assert all(f.critic is None for f in out.findings)
    metrics = evidence_metrics(out.findings, critic_enabled=False)
    assert metrics is not None and not metrics.critic_enabled and metrics.witness_checked == 1
    assert evidence_metrics([], critic_enabled=False) is None  # нечитаемый документ: строки о критике нет


def test_critic_drop_based_on_the_findings_own_quote_only_downgrades(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    client = llm([], [decision("M-002", "снять", FOUR_HOURS)])

    out = run(doc, tmp_path, [missing_capacity()], client)
    [finding] = verify(out.findings, doc, fallback_segmentation(doc)).findings

    critic = finding.critic
    assert critic is not None and critic.action is CriticAction.DOWNGRADE
    assert critic.proposed is CriticAction.DROP and "повторяет цитату" in critic.note
    assert finding.severity is Severity.MAJOR  # было «критично»


def test_critic_proposal_without_verified_basis_keeps_finding_as_is(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    client = llm([], [decision("M-002", "снять", "Вместимость комнаты указана в паспорте здания.")])

    out = run(doc, tmp_path, [missing_capacity()], client)
    [finding] = verify(out.findings, doc, fallback_segmentation(doc)).findings

    critic = finding.critic
    assert critic is not None and critic.action is CriticAction.KEEP
    assert critic.proposed is CriticAction.DROP and "не найдена" in critic.note
    assert finding.severity is Severity.CRITICAL


def test_short_basis_counts_only_verbatim_and_inside_the_findings_own_places(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    client = llm(
        [],
        [
            decision("M-002", "понизить", "четыре часа", severity="важно"),
            decision("M-003", "понизить", "пять часов", severity="важно"),
            decision("M-004", "понизить", "свободные", severity="важно"),
        ],
    )

    out = run(doc, tmp_path, [missing_capacity(2), missing_capacity(3), missing_capacity(4)], client)

    verbatim, invented, elsewhere = (f.critic for f in out.findings)
    assert verbatim is not None and verbatim.action is CriticAction.DOWNGRADE
    assert verbatim.basis is not None and verbatim.basis.located == "четыре часа"
    assert invented is not None and invented.action is CriticAction.KEEP and invented.basis is None
    assert elsewhere is not None and elsewhere.action is CriticAction.KEEP and elsewhere.basis is None


def test_critic_downgrade_lowers_severity_through_verifier(doc: ParsedDocument, tmp_path: Path) -> None:
    client = llm([], [decision("M-002", "понизить", FOUR_HOURS, severity="незначительно")])

    out = run(doc, tmp_path, [missing_capacity()], client)
    [finding] = verify(out.findings, doc, fallback_segmentation(doc)).findings

    assert finding.severity is Severity.MINOR
    assert "критик понизил" in finding.severity_basis


def test_critic_cannot_raise_severity(doc: ParsedDocument, tmp_path: Path) -> None:
    found = merged(3, [FAST], of=3, found_in=1, severity_hint=Severity.MAJOR)
    client = llm([], [decision("M-003", "понизить", FAST, severity="критично")])

    out = run(doc, tmp_path, [found], client)

    critic = out.findings[0].critic
    assert critic is not None and critic.action is CriticAction.KEEP
    assert critic.proposed is CriticAction.DOWNGRADE


def test_critic_reviews_critical_single_pass_disputed_and_unproven_contradictions(
    doc: ParsedDocument,
) -> None:
    found = [
        missing_capacity(),  # критично по таблице и по подсказке
        merged(3, [FAST], found_in=1),  # 1 из 3
        merged(4, [FAST], defect_type=DefectType.TRACEABILITY, severity_hint=Severity.CRITICAL),  # спор
        contradiction(5, severity_hint=Severity.MAJOR, blocks_testing=False),  # без свидетеля
        merged(6, [FAST]),  # ничем не рискованна
        merged(7, [FAST], found_in=0, sources=[FindingSource(kind=SourceKind.LINT, rule_id="L-1")], of=3),
    ]

    picked = select_for_critic(found, locate_findings(found, doc))

    assert {p.finding.mid for p in picked} == {"M-002", "M-003", "M-004", "M-005"}
    assert picked[0].finding.mid == "M-002" and picked[0].severity is Severity.CRITICAL


def test_critic_sees_witness_outcome_of_the_same_run(doc: ParsedDocument, tmp_path: Path) -> None:
    client = llm(
        [witness_rec("M-001", [(HOUR, "отмена отклонена"), (ANYTIME, "бронь отменена")])],
        [decision("M-001", "оставить", HOUR)],
    )

    run(doc, tmp_path, [contradiction()], client)

    critic_req = next(r for r in client.requests if r.step is LLMStep.CRITIC)
    assert "Пример-свидетель (код сверил цитаты и различие ожиданий)" in critic_req.prompt
    assert SCENARIO in critic_req.prompt


# ---------------------------------------------------------------- отказ модели


def test_model_failure_keeps_findings_and_reports_both_steps(doc: ParsedDocument, tmp_path: Path) -> None:
    found = [contradiction(), missing_capacity()]

    out = run(doc, tmp_path, found, llm())

    assert out.findings == found
    assert {LLM_EVIDENCE_FAILED, LLM_CRITIC_FAILED} <= codes(out)
    assert evidence_metrics(out.findings) is None


def test_reply_without_json_is_a_diagnostic_not_a_crash(doc: ParsedDocument, tmp_path: Path) -> None:
    client = FakeLLM(
        root=Path("/nonexistent"),
        responses={
            (LLMStep.EVIDENCE, 1): cli_payload("Не могу помочь с этим."),
            (LLMStep.CRITIC, 1): cli_payload('## Ответ\n\n```json\n{"decisions": [\n```'),
        },
    )

    out = run(doc, tmp_path, [contradiction()], client)

    assert out.findings[0].witness is None and out.findings[0].critic is None
    assert {LLM_EVIDENCE_FAILED, LLM_CRITIC_FAILED} <= codes(out)


def test_internal_error_in_witness_does_not_stop_critic(
    doc: ParsedDocument, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object) -> None:
        raise ZeroDivisionError

    monkeypatch.setattr("validator.evidence.witness", broken)
    client = llm(None, [decision("M-002", "снять", CAPACITY)])

    out = run(doc, tmp_path, [missing_capacity()], client)

    assert "INT-002" in codes(out)
    assert out.findings[0].critic is not None and out.findings[0].critic.action is CriticAction.DROP


def test_no_risky_findings_means_no_model_calls(doc: ParsedDocument, tmp_path: Path) -> None:
    class NoCalls:
        backend = FakeLLM.backend

        def call(self, req: LLMRequest) -> LLMResponse:
            raise LLMError(f"не ждали вызова {req.call_id}")

    out = run(doc, tmp_path, [merged(6, [FAST])], NoCalls())

    assert not out.diagnostics and out.findings[0].critic is None


# ---------------------------------------------------------------- разбор ответа и связи


def test_replies_are_parsed_leniently() -> None:
    text = (
        '```json\n{"decisions": [{"id": "m2", "decision": "Снять", "basis_quote": null, "reason": null},'
        ' {"id": "M-003", "decision": "выбросить"},'
        ' {"id": "M 4", "decision": "downgrade", "severity": "важно"}]}\n```'
    )
    resp = LLMResponse(call_id="critic.1", text=text, model="m", backend=FakeLLM.backend)

    parsed = parse_critic(resp)

    assert [(r.id, r.decision) for r in parsed] == [
        ("m2", CriticAction.DROP),
        ("M 4", CriticAction.DOWNGRADE),
    ]
    assert parsed[1].severity is Severity.MAJOR
    assert isinstance(parsed[0], ReplyDecision) and parsed[0].basis_quote == ""
    wit = LLMResponse(
        call_id="evidence.1",
        text='{"witnesses": [{"id": "M-001", "readings": "нет"}]}',
        model="m",
        backend=FakeLLM.backend,
    )
    assert parse_witness(wit)[0].readings == []


def test_block_ids_in_witness_and_critic_texts_become_requirement_ids(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    seg = fallback_segmentation(doc)
    hour_block = next(b.id for b in doc.blocks if HOUR in b.text)
    rec = witness_rec("M-001", [(HOUR, f"по {hour_block} отмена отклонена"), (ANYTIME, "бронь отменена")])
    client = llm(
        [rec], [decision("M-001", "оставить", HOUR) | {"reason": f"Место {hour_block} спорит с другим"}]
    )

    out = run(doc, tmp_path, [contradiction()], client)
    ver, _, _ = humanize(verify(out.findings, doc, seg), [], [], doc, seg)

    assert ver is not None
    [finding] = ver.findings
    assert finding.witness is not None and finding.critic is not None
    assert hour_block not in finding.witness.readings[0].expectation
    assert hour_block not in finding.critic.reason


def test_block_ids_in_critic_downgrade_reason_do_not_reach_severity_note(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    seg = fallback_segmentation(doc)
    block = next(b.id for b in doc.blocks if CAPACITY in b.text)
    reply = decision("M-002", "понизить", FOUR_HOURS, severity="важно") | {
        "reason": f"Место {block} уточняет"
    }

    out = run(doc, tmp_path, [missing_capacity()], llm([], [reply]))
    ver, _, _ = humanize(verify(out.findings, doc, seg), [], [], doc, seg)

    assert ver is not None
    [finding] = ver.findings
    assert any("Критик понизил" in n for n in finding.notes)
    assert not any(block in n for n in finding.notes)


def test_only_checked_witness_makes_a_question_and_raises_priority(
    doc: ParsedDocument, tmp_path: Path
) -> None:
    seg = fallback_segmentation(doc)
    found = merged(3, [FAST], scenario="сценарий", severity_hint=Severity.MAJOR)
    bad = witness_rec("M-003", [(FAST, "тест провален"), (FAST, "тест провален")])
    good = {
        "id": "M-003",
        "scenario": "Список свободных комнат открылся через 3 секунды после запроса",
        "readings": [
            {"quote": FAST, "reading": "до 1 с", "expectation": "тест провален"},
            {"quote": FAST, "reading": "до 5 с", "expectation": "тест пройден"},
        ],
    }

    def finding_with(rec: dict[str, Any]) -> Any:
        out = run(doc, tmp_path, [found], llm([rec]))
        [f] = verify(out.findings, doc, seg).findings
        return f

    rejected, checked = finding_with(bad), finding_with(good)

    assert rejected.witness is not None and not rejected.witness.checked
    assert finding_origins(rejected) == () and priority([rejected]).value == "P2"
    assert checked.witness is not None and checked.witness.checked
    # Важная, не критичная: свидетель блокирует тест, но не разработку.
    assert finding_origins(checked) and priority([checked]).value == "P1"


def test_metrics_count_kept_downgraded_dropped_and_overruled(doc: ParsedDocument, tmp_path: Path) -> None:
    found = [missing_capacity(2), missing_capacity(3), missing_capacity(4), missing_capacity(5)]
    client = llm(
        [],
        [
            decision("M-002", "снять", CAPACITY),
            decision("M-003", "снять", FOUR_HOURS),
            decision("M-004", "оставить", FOUR_HOURS),
            decision("M-005", "понизить", "Такого текста в документе нет совсем.", severity="важно"),
        ],
    )

    metrics = evidence_metrics(run(doc, tmp_path, found, client).findings)

    assert metrics is not None
    assert (
        metrics.critic_reviewed,
        metrics.critic_kept,
        metrics.critic_downgraded,
        metrics.critic_dropped,
    ) == (
        4,
        2,
        1,
        1,
    )
    assert metrics.critic_overruled == 2


# ---------------------------------------------------------------- отчёт


def _pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        pages = [page.get_text() for page in pdf]
    return re.sub(r"\s+", " ", " ".join(pages))


def test_witness_critic_and_dropped_findings_appear_in_markdown_and_pdf() -> None:
    report = Report.model_validate_json((factory.FIXTURES / "full.report.json").read_text(encoding="utf-8"))

    markdown, text = render_md(report), _pdf_text(render_pdf(report))

    for needle in (
        "Пример-свидетель (код сверил цитаты и различие ожиданий)",
        "Встреча начинается в 15:00, пользователь отменяет бронь в 14:30",
        "отмена отклонена",
        "Два места задают разный срок отмены, общего правила в документе нет",
        "Ночное окно задаёт администратор, это не дефект требования",
        "Находки, снятые критиком",
        "Вместимость задана в разделе об ограничениях",
        "модель ответила по",
        "Критик находок",
    ):
        assert needle in markdown, needle
        assert needle in text, needle


def test_report_says_when_critic_was_off() -> None:
    report = Report.model_validate_json((factory.FIXTURES / "full.report.json").read_text(encoding="utf-8"))
    assert report.metrics.evidence is not None
    off = report.metrics.evidence.model_copy(update={"critic_enabled": False})
    report = report.model_copy(update={"metrics": report.metrics.model_copy(update={"evidence": off})})

    markdown, text = render_md(report), _pdf_text(render_pdf(report))

    assert "выключен в этом прогоне, включается флагом --critic" in markdown
    assert "выключен в этом прогоне, включается флагом --critic" in text


# ---------------------------------------------------------------- конвейер


@pytest.mark.parametrize("critic", [False, True], ids=["default", "critic"])
def test_pipeline_runs_step_6_on_fake_backend(tmp_path: Path, critic: bool) -> None:
    cfg = load_config(REQUIREMENTS[DocKind.MD], tmp_path / "report.md", backend=Backend.FAKE, critic=critic)

    outcome = pipeline.run(cfg)

    assert outcome.exit_code is ExitCode.OK and outcome.report is not None
    ev = outcome.report.metrics.evidence
    assert ev is not None and ev.witness_checked >= 1 and ev.critic_enabled is critic
    assert (ev.critic_reviewed >= 1) is critic
    assert outcome.report.passport.calls_failed == 0
    codes = {d.code for d in outcome.report.diagnostics}
    assert LLM_WITNESS_CHECKED in codes and (LLM_CRITIC_DECIDED in codes) is critic
    markdown = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "Пример-свидетель (код сверил цитаты и различие ожиданий)" in markdown
    assert ("**Критик:**" in markdown) is critic


def test_replay_of_a_critic_run_turns_the_critic_on(tmp_path: Path) -> None:
    recorded = pipeline.run(
        load_config(REQUIREMENTS[DocKind.MD], tmp_path / "a" / "report.md", backend=Backend.FAKE, critic=True)
    )
    replayed = pipeline.run(
        load_config(
            REQUIREMENTS[DocKind.MD],
            tmp_path / "b" / "report.md",
            backend=Backend.REPLAY,
            replay_dir=tmp_path / "a" / "run",
        )
    )

    assert recorded.report is not None and replayed.report is not None
    assert replayed.report.verification == recorded.report.verification
    ev = replayed.report.metrics.evidence
    assert ev is not None and ev.critic_enabled and ev.critic_reviewed >= 1
    assert any("критиком находок" in d.message for d in replayed.report.diagnostics)
