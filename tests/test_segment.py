"""Нарезка на требования (#19): модель называет ID блоков и предложений, цитаты вырезает код.

Ответы модели здесь собраны вручную для маленького документа о складе (домен нарочно чужой), плюс
записанный живой ответ на ``data/input/requirements.md`` из ``tests/fixtures/llm/segment/1.json``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from conftest import FIXTURES_LLM
from validator.config import load_config
from validator.errors import (
    IN_NO_REQUIREMENTS,
    LLM_BAD_RESPONSE,
    LLM_CALL_FAILED,
    LLM_SEGMENT_BAD_REFS,
    LLM_SEGMENT_FALLBACK,
    LLM_SEGMENT_OVERLAP,
    LLM_SEGMENT_UNCOVERED,
)
from validator.llm import FakeLLM
from validator.models import (
    Backend,
    BlockLabel,
    DecidedBy,
    DiagLevel,
    LLMStep,
    ParsedDocument,
    RequirementType,
    RunConfig,
    Segmentation,
    SegmentationMode,
)
from validator.segment import RESPONSE_SCHEMA, build_prompt, fallback_segmentation, segment
from validator.segment.answer import AnswerError, parse_answer
from validator.segment.checks import from_answer
from validator.segment.spans import normalize_ref

WAREHOUSE = """# Склад

Система учёта склада для небольшого магазина.

## Требования

**WH-1.** Система должна принимать поставку за один шаг. Кладовщик сканирует накладную.

- Остаток товара обновляется сразу после приёмки.
- Отчёт по остаткам формируется раз в сутки.

| Параметр | Значение |
|---|---|
| sku | строка до 20 символов |
| qty | целое число больше нуля |
"""
"""Блоки: B001 и B003 заголовки, B002 вводный абзац, B004 (метка WH-1, предложения B004.1 и B004.2),
B005 и B006 пункты списка, B007 и B008 строки таблицы."""


def item(*refs: str, **fields: Any) -> dict[str, Any]:
    """Запись требования в ответе модели: все поля схемы, по умолчанию пустые."""
    base: dict[str, Any] = {
        "refs": list(refs),
        "part_start": "",
        "part_end": "",
        "type": "functional",
        "subtype": "",
        "source_id": "",
        "summary": "",
        "entities": [],
        "links": [],
        "depends_on": "",
    }
    return base | fields


def exclusion(block: str, label: str = "context", reason: str = "вводный текст") -> dict[str, str]:
    return {"block": block, "label": label, "reason": reason}


def answer(items: list[dict[str, Any]], excluded: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {"requirements": items, "excluded": excluded if excluded is not None else [exclusion("B002")]}


def full_items() -> list[dict[str, Any]]:
    return [
        item("B004.1", source_id="WH-1", summary="Приёмка поставки за один шаг"),
        item("B004.2", type="process"),
        item("B005"),
        item("B006", type="nfr"),
        item("B007", type="data"),
        item("B008", type="data"),
    ]


def run(doc: ParsedDocument, structured: object) -> Segmentation:
    return from_answer(doc, parse_answer(structured))


def codes(seg: Segmentation) -> list[tuple[str, DiagLevel]]:
    return [(d.code, d.level) for d in seg.diagnostics]


def envelope(**extra: Any) -> dict[str, Any]:
    """Ответ ``claude -p --output-format json`` для фейкового клиента."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "duration_ms": 10,
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "modelUsage": {"claude-sonnet-5-5": {}},
        **extra,
    }


@pytest.fixture
def doc(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(WAREHOUSE)


@pytest.fixture
def cfg(tmp_path: Path, doc: ParsedDocument) -> RunConfig:
    return load_config(tmp_path / "doc.md", tmp_path / "out" / "report.md", backend=Backend.FAKE)


def assert_quotes_from_document(doc: ParsedDocument, seg: Segmentation) -> None:
    for req in seg.requirements:
        assert req.quote == doc.text[req.anchor.char_start : req.anchor.char_end], req.id
        assert req.quote.strip() == req.quote and req.quote, req.id


# ---------------------------------------------------------------- сборка из ответа модели


def test_full_answer_gives_clean_segmentation_in_document_order(doc: ParsedDocument) -> None:
    seg = run(doc, answer(full_items()))

    assert seg.mode is SegmentationMode.MODEL
    assert seg.diagnostics == []
    assert [r.id for r in seg.requirements] == ["R-01.1", "R-01.2", "R-02", "R-03", "R-04", "R-05"]
    assert [r.parent_id for r in seg.requirements[:2]] == ["R-01", "R-01"]
    assert all(r.decided_by is DecidedBy.MODEL for r in seg.requirements)
    assert [e.block_id for e in seg.excluded] == ["B001", "B002", "B003"]
    assert_quotes_from_document(doc, seg)


def test_requirement_label_is_kept_as_source_id_but_not_in_quote(doc: ParsedDocument) -> None:
    first = run(doc, answer(full_items())).requirements[0]

    assert first.source_id == "WH-1"
    assert first.quote == "Система должна принимать поставку за один шаг."
    assert first.source_locator == "WH-1, предл. 1/2"
    assert first.summary == "Приёмка поставки за один шаг"


def test_quote_comes_from_document_even_if_model_writes_text(doc: ParsedDocument) -> None:
    items = full_items()
    items[2] = item("B005", summary="Остаток обновляется раз в неделю", source_id="Остаток")

    req = run(doc, answer(items)).requirements[2]

    assert req.quote == "Остаток товара обновляется сразу после приёмки."
    assert req.source_id is None  # слово из текста меткой не становится


def test_table_rows_are_named_by_key_column_and_typed(doc: ParsedDocument) -> None:
    rows = run(doc, answer(full_items())).requirements[-2:]

    assert [r.source_locator for r in rows] == ["Требования: sku", "Требования: qty"]
    assert [r.type for r in rows] == [RequirementType.DATA, RequirementType.DATA]
    assert rows[0].quote.startswith("sku") and rows[0].quote.endswith("символов")


def test_unknown_block_id_is_dropped_with_diagnostic(doc: ParsedDocument) -> None:
    items = [*full_items(), item("B099"), item("B005", "B777")]

    seg = run(doc, answer(items))

    assert len(seg.requirements) == len(full_items())
    bad = next(d for d in seg.diagnostics if d.code == LLM_SEGMENT_BAD_REFS and d.level is DiagLevel.WARNING)
    assert "B099" in bad.message and "B777" in bad.message
    assert_quotes_from_document(doc, seg)


def test_unknown_sentence_of_known_block_falls_back_to_whole_block(doc: ParsedDocument) -> None:
    items = [item("B004.9"), *full_items()[2:]]

    seg = run(doc, answer(items))

    assert seg.requirements[0].quote == (
        "Система должна принимать поставку за один шаг. Кладовщик сканирует накладную."
    )
    assert (LLM_SEGMENT_BAD_REFS, DiagLevel.INFO) in codes(seg)


def test_answer_with_only_unknown_ids_is_an_error(doc: ParsedDocument) -> None:
    with pytest.raises(AnswerError):
        run(doc, answer([item("B099"), item("X1")]))


def test_sentence_subrange_gives_exact_substring(doc: ParsedDocument) -> None:
    items = full_items()
    items[0] = item("B004.1", part_start="принимать поставку", part_end="один шаг")

    req = run(doc, answer(items)).requirements[0]

    assert req.quote == "принимать поставку за один шаг"
    assert req.source_locator.endswith("часть")
    assert req.anchor.char_start == doc.text.index("принимать поставку")


def test_subrange_with_unknown_words_falls_back_to_sentence(doc: ParsedDocument) -> None:
    items = full_items()
    items[0] = item("B004.1", part_start="такого текста нет", part_end="")

    seg = run(doc, answer(items))

    assert seg.requirements[0].quote == "Система должна принимать поставку за один шаг."
    note = next(d for d in seg.diagnostics if d.code == LLM_SEGMENT_BAD_REFS)
    assert "границы части не найдены" in note.message


def test_ids_and_stable_keys_do_not_depend_on_answer_order_or_wording(doc: ParsedDocument) -> None:
    first = run(doc, answer(full_items()))
    shuffled = [{**it, "summary": f"другая формулировка {k}"} for k, it in enumerate(full_items())]
    second = run(doc, answer(shuffled[::-1]))

    def key(seg: Segmentation) -> list[tuple[str, str, str]]:
        return [(r.id, r.stable_key, r.quote) for r in seg.requirements]

    assert key(first) == key(second)
    assert len({r.stable_key for r in first.requirements}) == len(first.requirements)


def test_block_without_decision_is_taken_by_code_and_flagged(doc: ParsedDocument) -> None:
    items = [it for it in full_items() if not it["refs"][0].startswith("B004")]

    seg = run(doc, answer(items))

    taken = next(r for r in seg.requirements if r.source_id == "WH-1")
    assert taken.decided_by is DecidedBy.CODE
    missed = next(d for d in seg.diagnostics if d.code == LLM_SEGMENT_UNCOVERED)
    assert missed.level is DiagLevel.WARNING
    assert "возможно пропущены моделью: B004" in missed.message


def test_excluded_block_is_not_reported_as_missed(doc: ParsedDocument) -> None:
    items = [it for it in full_items() if it["refs"] != ["B006"]]

    seg = run(doc, answer(items, [exclusion("B002"), exclusion("B006", "example", "пример отчёта")]))

    assert seg.diagnostics == []
    assert any(e.block_id == "B006" and e.label is BlockLabel.EXAMPLE for e in seg.excluded)


def test_overlapping_requirements_give_warning(doc: ParsedDocument) -> None:
    seg = run(doc, answer([item("B004"), *full_items()]))

    overlap = next(d for d in seg.diagnostics if d.code == LLM_SEGMENT_OVERLAP)
    assert overlap.level is DiagLevel.WARNING
    assert_quotes_from_document(doc, seg)


def test_no_requirements_gives_diagnostic(doc: ParsedDocument) -> None:
    excluded = [exclusion(b, "template") for b in ("B002", "B004", "B005", "B006", "B007", "B008")]

    seg = run(doc, answer([], excluded))

    assert seg.requirements == []
    assert (IN_NO_REQUIREMENTS, DiagLevel.WARNING) in codes(seg)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("B4", "B004"), ("[b004.2]", "B004.2"), ("В004", "B004"), (" B0004 . 1 ", "B004.1"), ("R-01", None)],
)
def test_ref_variants_are_normalized(raw: str, expected: str | None) -> None:
    assert normalize_ref(raw) == expected


@pytest.mark.parametrize("structured", [None, [], {"excluded": []}, {"requirements": [{"no_refs": 1}]}])
def test_answer_not_by_schema_is_an_error(structured: object) -> None:
    with pytest.raises(AnswerError):
        parse_answer(structured)


def test_response_schema_is_strict() -> None:
    def objects(node: object) -> list[dict[str, Any]]:
        if isinstance(node, dict):
            found = [node] if node.get("type") == "object" else []
            return found + [o for v in node.values() for o in objects(v)]
        if isinstance(node, list):
            return [o for v in node for o in objects(v)]
        return []

    found = objects(RESPONSE_SCHEMA)
    assert len(found) >= 4  # ответ, требование, сущность, исключение
    for obj in found:
        assert obj["additionalProperties"] is False
        assert sorted(obj["required"]) == sorted(obj["properties"])


def test_prompt_contains_marked_document(doc: ParsedDocument) -> None:
    prompt = build_prompt(doc)

    assert "[B004.1]" in prompt and "{document}" not in prompt


# ---------------------------------------------------------------- шаг целиком: вызовы и запасной путь


def test_valid_answer_uses_one_call(doc: ParsedDocument, cfg: RunConfig) -> None:
    llm = FakeLLM(responses={(LLMStep.SEGMENT, 1): envelope(structured_output=answer(full_items()))})

    seg = segment(doc, llm, cfg)

    assert seg.mode is SegmentationMode.MODEL
    assert seg.call_ids == ["segment.1"]
    assert llm.requests[0].json_schema == RESPONSE_SCHEMA


def test_invalid_answer_is_retried_once(doc: ParsedDocument, cfg: RunConfig) -> None:
    llm = FakeLLM(
        responses={
            (LLMStep.SEGMENT, 1): envelope(structured_output={"requirements": "нет"}),
            (LLMStep.SEGMENT, 2): envelope(structured_output=answer(full_items())),
        }
    )

    seg = segment(doc, llm, cfg)

    assert seg.mode is SegmentationMode.MODEL
    assert seg.call_ids == ["segment.1", "segment.2"]
    assert "Предыдущий ответ не прошёл проверку" in llm.requests[1].prompt


def test_refusal_twice_gives_fallback_with_diagnostic(doc: ParsedDocument, cfg: RunConfig) -> None:
    refusal = envelope(result="Не могу выполнить эту задачу.")
    llm = FakeLLM(responses={(LLMStep.SEGMENT, 1): refusal, (LLMStep.SEGMENT, 2): refusal})

    seg = segment(doc, llm, cfg)

    assert seg.mode is SegmentationMode.FALLBACK
    assert seg.call_ids == ["segment.1", "segment.2"]
    diag = seg.diagnostics[0]
    assert (diag.code, diag.level) == (LLM_SEGMENT_FALLBACK, DiagLevel.WARNING)
    assert LLM_BAD_RESPONSE in diag.message
    assert [r.decided_by for r in seg.requirements] == [DecidedBy.CODE] * len(seg.requirements)
    assert_quotes_from_document(doc, seg)


def test_call_error_is_not_retried(doc: ParsedDocument, cfg: RunConfig) -> None:
    llm = FakeLLM(responses={(LLMStep.SEGMENT, 1): envelope(is_error=True, result="API Error: 500")})

    seg = segment(doc, llm, cfg)

    assert seg.mode is SegmentationMode.FALLBACK
    assert seg.call_ids == ["segment.1"]
    assert LLM_CALL_FAILED in seg.diagnostics[0].message


def test_usage_limit_is_not_retried(doc: ParsedDocument, cfg: RunConfig) -> None:
    llm = FakeLLM(responses={(LLMStep.SEGMENT, 1): envelope(is_error=True, result="usage limit reached")})

    seg = segment(doc, llm, cfg)

    assert seg.mode is SegmentationMode.FALLBACK
    assert seg.call_ids == ["segment.1"]


def test_fallback_takes_content_blocks_and_skips_short_ones(doc: ParsedDocument) -> None:
    seg = fallback_segmentation(doc)

    assert seg.mode is SegmentationMode.FALLBACK
    assert [r.source_locator for r in seg.requirements][:2] == ["Склад", "WH-1"]
    assert {e.block_id for e in seg.excluded} == {"B001", "B003"}
    assert_quotes_from_document(doc, seg)


def test_fallback_on_empty_document_gives_diagnostic(make_md: Callable[[str], ParsedDocument]) -> None:
    seg = fallback_segmentation(make_md("# Шаблон\n\n## Раздел\n"))

    assert seg.requirements == []
    assert (IN_NO_REQUIREMENTS, DiagLevel.WARNING) in codes(seg)


# ---------------------------------------------------------------- записанный живой ответ


def test_recorded_answer_on_requirements_document(requirements_md: ParsedDocument) -> None:
    payload = json.loads((FIXTURES_LLM / "segment" / "1.json").read_text(encoding="utf-8"))

    seg = run(requirements_md, payload["structured_output"])

    assert seg.diagnostics == []
    assert len(seg.requirements) == len(payload["structured_output"]["requirements"])
    assert all(r.decided_by is DecidedBy.MODEL for r in seg.requirements)
    assert_quotes_from_document(requirements_md, seg)
