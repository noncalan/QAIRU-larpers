"""ID блоков в текстах моделей → R-NN или место в документе (живой прогон #36)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from validator.models import (
    DecidedBy,
    DefectType,
    Finding,
    ParsedDocument,
    Property,
    Question,
    QuestionOption,
    QuestionPriority,
    Quote,
    QuoteMatch,
    Scope,
    Segmentation,
    SegmentationMode,
    Severity,
    Verdict,
    Verification,
)
from validator.refs import RefNamer, humanize
from validator.segment.build import Draft, build_requirements

DOC = (
    "# Заказы\n\n"
    "Система должна сохранять заказ. Пользователь видит статус заказа.\n\n"
    "## Справка\n\n"
    "Раздел для пояснений, тут требований нет.\n"
)


@pytest.fixture
def doc(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(DOC)


@pytest.fixture
def seg(doc: ParsedDocument) -> Segmentation:
    """Два требования из предложений блока B002; блок B004 исключён."""
    block = doc.blocks[1]
    drafts = [Draft(s.char_start, s.char_end, block, DecidedBy.MODEL) for s in block.sentences]
    return Segmentation(requirements=build_requirements(doc, drafts), mode=SegmentationMode.MODEL)


def _finding(text: str, quote: Quote) -> Finding:
    return Finding(
        id="F-01",
        merged_id="M-001",
        property=Property.UNAMBIGUOUS,
        defect_type=DefectType.AMBIGUOUS,
        verdict=Verdict.VIOLATION,
        scope=Scope.REQUIREMENT,
        severity=Severity.MAJOR,
        quotes=[quote],
        explanation=text,
        title=f"Суть: {text}",
        suggestion=text,
    )


def test_block_and_sentence_ids_become_requirement_ids(doc: ParsedDocument, seg: Segmentation) -> None:
    namer = RefNamer(doc, seg)

    assert [r.id for r in seg.requirements] == ["R-01.1", "R-01.2"]
    assert namer.text("статус (B002.2) не определён") == "статус (R-01.2) не определён"
    assert namer.text("в блоке B002 два действия") == "в блоке R-01.1, R-01.2 два действия"


def test_id_outside_requirements_becomes_place_in_document(doc: ParsedDocument, seg: Segmentation) -> None:
    namer = RefNamer(doc, seg)

    assert namer.text("см. B004") == "см. фрагмент в разделе «Заказы › Справка» (строка 7)"
    assert namer.text("см. B003") == "см. раздел «Справка»"


def test_unknown_ids_and_lookalikes_stay(doc: ParsedDocument, seg: Segmentation) -> None:
    namer = RefNamer(doc, seg)

    assert namer.text("B999 и B002x и AB002 и B12") == "B999 и B002x и AB002 и B12"


def test_humanize_rewrites_findings_and_questions_but_not_quotes(
    doc: ParsedDocument, seg: Segmentation
) -> None:
    quote = Quote(text="B002 сохраняет", match=QuoteMatch.EXACT, score=100.0, located="B002 сохраняет")
    ver = Verification(findings=[_finding("Статус в B002.2 не задан", quote)])
    question = Question(
        id="Q-01",
        priority=QuestionPriority.P1,
        question="Что показывать в B002.2?",
        options=[QuestionOption(label="A", text="Статус", change="Исправить B002.2")],
    )

    new_ver, (new_q,) = humanize(ver, [question], doc, seg)

    assert new_ver is not None
    finding = new_ver.findings[0]
    assert finding.explanation == finding.suggestion == "Статус в R-01.2 не задан"
    assert finding.title == "Суть: Статус в R-01.2 не задан"
    assert finding.quotes[0].text == "B002 сохраняет"
    assert new_q.question == "Что показывать в R-01.2?"
    assert new_q.options[0].change == "Исправить R-01.2"
