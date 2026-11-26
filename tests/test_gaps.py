"""Шаг 4г: проход на пропуски (``--gaps``): разбор ответа, отказ вызова, слияние с находками поиска."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from conftest import REQUIREMENTS
from validator.config import GAPS, load_config
from validator.errors import LLM_GAPS_CHECKED, LLM_GAPS_FAILED
from validator.gaps import KNOWN_LIMIT, NOTHING_KNOWN, find_gaps, gaps_request, parse_gaps, render_known
from validator.llm import FakeLLM
from validator.merge import merge
from validator.models import (
    Backend,
    CandidateFinding,
    DefectType,
    DiagLevel,
    DocKind,
    FindingSource,
    LLMStep,
    ParsedDocument,
    PassResult,
    Property,
    RunConfig,
    Scope,
    SourceKind,
)
from validator.parts import plan_parts
from validator.passport import build_passport

LIBRARY = """# Библиотека

## Выдача книг

Книга выдаётся на срок до 14 дней. Продление оформляется в читальном зале.

Читатель может держать на руках не более пяти книг.
"""

TERM = "Книга выдаётся на срок до 14 дней."
RENEWAL = "Продление оформляется в читальном зале."


@pytest.fixture
def doc(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(LIBRARY)


def config(*, gaps: bool = True) -> RunConfig:
    return load_config(REQUIREMENTS[DocKind.MD], Path("out/report.md"), backend=Backend.FAKE, gaps=gaps)


def bid(doc: ParsedDocument, fragment: str) -> str:
    return next(b.id for b in doc.blocks if fragment in b.text)


def gap(
    n: int, title: str, *, blocks: str, quote: str, kind: str = "неполнота", scope: str = "требования"
) -> str:
    return "\n".join(
        [
            f"#### Н-{n}. {title}",
            f"- Блоки: {blocks}",
            f"- Цитата: «{quote}»",
            f"- Свойство: Complete (полнота {scope}), ISO/IEC/IEEE 29148:2018, п. 5.2.5",
            f"- Тип дефекта: {kind}",
            "- Вердикт: нарушение",
            "- Почему мешает: Неизвестен ожидаемый результат теста.",
            "- Тест: мешает, нет ожидаемого результата на границе",
            "- Важность: низко",
            "- Как исправить: «Книга выдаётся на срок не более 14 календарных дней»",
        ]
    )


def answer(*findings: str) -> str:
    walk = "## Разбор\n\n- B003.1: исполнитель нет; числа: единица есть, граница нет\n\n"
    return walk + "## Находки\n\n### Отдельные требования\n\n" + "\n\n".join(findings)


def envelope(text: str) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text}


def pass_candidate(
    doc: ParsedDocument, title: str, quote: str, prop: Property = Property.COMPLETE
) -> CandidateFinding:
    return CandidateFinding(
        cid="p1.001",
        source=FindingSource(kind=SourceKind.PASS, pass_index=1, call_id="search.1"),
        property=prop,
        defect_type=DefectType.INCOMPLETE,
        scope=Scope.REQUIREMENT,
        block_ids=[bid(doc, quote)],
        quotes=[quote],
        explanation=title,
        title=title,
    )


def test_gaps_are_off_by_default_and_hidden_from_passport() -> None:
    cfg = config(gaps=False)
    default = load_config(REQUIREMENTS[DocKind.MD], Path("out/report.md"), backend=Backend.FAKE)

    assert GAPS is False
    assert default.gaps is False
    assert LLMStep.GAPS.value not in _passport_models(cfg)
    assert LLMStep.GAPS.value in _passport_models(config(gaps=True))


def _passport_models(cfg: RunConfig) -> dict[str, str]:
    now = datetime.now(UTC)
    passport = build_passport(
        cfg,
        document_name="x.md",
        document_kind=DocKind.MD,
        document_sha256=None,
        document_size_bytes=None,
        document_pages=None,
        started_at=now,
        finished_at=now,
        backend_used=Backend.FAKE,
        calls=[],
        exit_code=0,  # type: ignore[arg-type]
        checklists=[],
    )
    return passport.models


def test_answer_is_parsed_into_gap_candidates_with_known_blocks(doc: ParsedDocument) -> None:
    block = bid(doc, TERM)
    text = answer(
        gap(
            1,
            "Не сказано, входит ли 14-й день",
            blocks=f"{block}.1, B999",
            quote=TERM,
            kind="двусмысленность",
        ),
        gap(2, "Не назван исполнитель продления", blocks=f"{block}.2", quote=RENEWAL),
    )

    out = parse_gaps(text, doc, part=1, parts=1, call_id="gaps.1")

    assert [c.cid for c in out.candidates] == ["gap.001", "gap.002"]
    first, second = out.candidates
    assert first.source.kind is SourceKind.GAPS and first.source.call_id == "gaps.1"
    assert first.source.label == "проход на пропуски"
    assert first.block_ids == [f"{block}.1"]  # B999 в документе нет
    assert first.property is Property.COMPLETE and first.defect_type is DefectType.AMBIGUOUS
    assert second.quotes == [RENEWAL] and second.blocks_testing


def test_finding_that_is_not_a_gap_is_dropped_by_code(doc: ParsedDocument) -> None:
    block = bid(doc, TERM)
    text = answer(
        gap(1, "Срок задан по-разному", blocks=block, quote=TERM, kind="противоречие"),
        gap(2, "Не назван исполнитель продления", blocks=block, quote=RENEWAL),
        "#### Н-3. Находка без цитаты\n- Блоки: B003\n- Тип дефекта: неполнота",
    )

    out = parse_gaps(text, doc, part=1, parts=1, call_id="gaps.1")

    assert [c.title for c in out.candidates] == ["Не назван исполнитель продления"]
    assert (out.off_topic, out.unparsed) == (1, 1)


def test_answer_without_gaps_gives_no_candidates_and_no_loss(doc: ParsedDocument) -> None:
    text = "## Разбор\n\n- B003.1: всё есть\n\n## Находки\n\nПропусков сверх уже найденного нет"

    out = parse_gaps(text, doc, part=1, parts=1, call_id="gaps.1")

    assert out.candidates == [] and out.unparsed == 0


def test_known_findings_go_into_prompt_once_and_are_limited(doc: ParsedDocument) -> None:
    known = pass_candidate(doc, "Не задана реакция на просрочку", TERM)
    many = [known.model_copy(update={"title": f"Находка {n}"}) for n in range(KNOWN_LIMIT + 5)]

    rendered = render_known([known, known])
    req = gaps_request("[B003] текст", rendered, config(), 1)

    assert rendered.count("Не задана реакция на просрочку") == 1
    assert TERM in rendered and bid(doc, TERM) in rendered
    assert render_known([]) == NOTHING_KNOWN
    assert len(render_known(many).splitlines()) == KNOWN_LIMIT
    assert req.step is LLMStep.GAPS and req.call_id == "gaps.1"
    assert "<уже_найдено>" in req.prompt and rendered in req.prompt and "[B003] текст" in req.prompt
    assert req.model == config().model_for(LLMStep.SEARCH).model


def test_find_gaps_calls_model_once_and_returns_candidates(doc: ParsedDocument) -> None:
    block = bid(doc, TERM)
    text = answer(gap(1, "Не назван исполнитель продления", blocks=block, quote=RENEWAL))
    llm = FakeLLM(responses={(LLMStep.GAPS, 1): envelope(text)})

    out = find_gaps(doc, plan_parts(doc), [], llm, config())

    assert [c.title for c in out.candidates] == ["Не назван исполнитель продления"]
    assert out.diagnostics == [] and out.call_ids == ["gaps.1"]
    assert len(llm.requests) == 1 and NOTHING_KNOWN in llm.requests[0].prompt


def test_failed_call_is_a_warning_not_a_crash(doc: ParsedDocument) -> None:
    llm = FakeLLM(root=Path("нет-такой-папки"))

    out = find_gaps(doc, plan_parts(doc), [], llm, config())

    assert out.candidates == []
    assert [(d.code, d.level) for d in out.diagnostics] == [(LLM_GAPS_FAILED, DiagLevel.WARNING)]


def test_dropped_records_are_reported_as_info(doc: ParsedDocument) -> None:
    text = answer(gap(1, "Срок задан по-разному", blocks=bid(doc, TERM), quote=TERM, kind="противоречие"))
    llm = FakeLLM(responses={(LLMStep.GAPS, 1): envelope(text)})

    out = find_gaps(doc, plan_parts(doc), [], llm, config())

    assert [(d.code, d.level) for d in out.diagnostics] == [(LLM_GAPS_CHECKED, DiagLevel.INFO)]


def _gap_candidate(doc: ParsedDocument, title: str, quote: str) -> CandidateFinding:
    text = answer(gap(1, title, blocks=bid(doc, quote), quote=quote))
    return parse_gaps(text, doc, part=1, parts=1, call_id="gaps.1").candidates[0]


def test_gap_on_the_same_quote_as_other_finding_stays_a_separate_finding(doc: ParsedDocument) -> None:
    """Проход на пропуски цитирует фразу, о которой у поиска уже есть находка про другое: не склеиваем."""
    found = pass_candidate(doc, "Не задана реакция на просрочку возврата книги", TERM)
    missing = _gap_candidate(doc, "Не сказано, входит ли 14-й день в срок", TERM)
    passes = [PassResult(index=1, ok=True, candidates=[found])]

    merged = merge(passes, [missing], doc)

    assert len(merged) == 2
    by_title = {m.title: m for m in merged}
    gap_finding = by_title["Не сказано, входит ли 14-й день в срок"]
    assert [s.kind for s in gap_finding.sources] == [SourceKind.GAPS]
    assert (gap_finding.found_in, gap_finding.of) == (0, 1)  # в «N из K» проход на пропуски не считается


def test_gap_with_the_same_gist_as_search_finding_is_merged_into_it(doc: ParsedDocument) -> None:
    title = "Не сказано, входит ли 14-й день в срок"
    found = pass_candidate(doc, title, TERM)
    missing = _gap_candidate(doc, title, TERM)
    missing = missing.model_copy(update={"explanation": title})

    merged = merge([PassResult(index=1, ok=True, candidates=[found])], [missing], doc)

    assert len(merged) == 1
    assert {s.kind for s in merged[0].sources} == {SourceKind.PASS, SourceKind.GAPS}
    assert merged[0].found_in == 1
