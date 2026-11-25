"""Заготовки тестов шага 8: маленький документ из другой предметной области, находки, ответ редактора."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from validator.config import load_config
from validator.ingest import parse
from validator.llm import FakeLLM
from validator.models import (
    Backend,
    DefectType,
    DocKind,
    Finding,
    LLMStep,
    ParsedDocument,
    Property,
    RunConfig,
    Segmentation,
    Severity,
    Verdict,
    Verification,
)
from validator.segment import fallback_segmentation
from validator.text import locate_quote

LIBRARY_DOC = """# Требования к сервису библиотеки

## Функции

Читатель может продлить книгу, если на неё нет очереди.

Продление выполняется быстро.

Срок выдачи книги составляет четырнадцать дней.

Штраф начисляется за каждый день просрочки.

Каталог обновляется регулярно.

## Критерии приёмки

| Исходные условия | Действие | Ожидаемый результат |
|---|---|---|
| Книга выдана первого марта | Вернуть двадцатого марта | Штраф за пять дней |
| Книга выдана первого марта | Вернуть шестнадцатого марта | Штраф не начислен |
"""

EXTEND = "Продление выполняется быстро."
TERM = "Срок выдачи книги составляет четырнадцать дней."
FINE = "Штраф начисляется за каждый день просрочки."
CATALOG = "Каталог обновляется регулярно."
FIVE_DAYS = "Штраф за пять дней"
QUEUE = "Читатель может продлить книгу, если на неё нет очереди."


def make_doc(tmp_path: Path, text: str = LIBRARY_DOC) -> ParsedDocument:
    path = tmp_path / "library.md"
    path.write_text(text, encoding="utf-8")
    return parse(path, DocKind.MD)


def make_cfg(doc: ParsedDocument, tmp_path: Path) -> RunConfig:
    return load_config(Path(doc.path), tmp_path / "out" / "report.md", backend=Backend.FAKE)


def finding(
    doc: ParsedDocument,
    fid: str,
    quotes: Sequence[str],
    *,
    defect: DefectType = DefectType.AMBIGUOUS,
    prop: Property = Property.UNAMBIGUOUS,
    verdict: Verdict = Verdict.VIOLATION,
    severity: Severity = Severity.MAJOR,
    blocks_testing: bool = False,
    suggestion: str | None = None,
    requirements: Sequence[str] = (),
) -> Finding:
    located = [locate_quote(doc, q) for q in quotes]
    assert all(q.verified for q in located), quotes
    return Finding(
        id=fid,
        merged_id=f"M-{fid[2:]}",
        property=prop,
        defect_type=defect,
        verdict=verdict,
        scope=prop.scope,
        severity=severity,
        blocks_testing=blocks_testing,
        quotes=located,
        requirement_ids=list(requirements),
        explanation=f"Объяснение находки {fid}.",
        suggestion=suggestion,
    )


def library_findings(doc: ParsedDocument) -> list[Finding]:
    """F-01 противоречие (критично), F-02 и F-03 одно неясное место, F-04 однозначная правка,
    F-05 посторонняя инструкция, F-06 непроверяемый штраф."""
    return [
        finding(
            doc,
            "F-01",
            [TERM, FIVE_DAYS],
            defect=DefectType.CONTRADICTION,
            prop=Property.SET_CONSISTENT,
            severity=Severity.CRITICAL,
            blocks_testing=True,
            requirements=["R-03", "R-07"],
        ),
        finding(
            doc,
            "F-02",
            [EXTEND],
            verdict=Verdict.UNCLEAR,
            suggestion="Продление выполняется не дольше [уточнить: сколько секунд].",
            requirements=["R-02"],
        ),
        finding(doc, "F-03", [EXTEND], blocks_testing=True, requirements=["R-02"]),
        finding(
            doc,
            "F-04",
            [CATALOG],
            defect=DefectType.TRACEABILITY,
            prop=Property.CONFORMING,
            severity=Severity.MINOR,
            suggestion="Каталог должен обновляться регулярно.",
            requirements=["R-05"],
        ),
        finding(
            doc,
            "F-05",
            [QUEUE],
            defect=DefectType.INJECTED_INSTRUCTION,
            prop=Property.NECESSARY,
            blocks_testing=True,
            requirements=["R-01"],
        ),
        finding(
            doc,
            "F-06",
            [FINE],
            defect=DefectType.UNVERIFIABLE,
            prop=Property.VERIFIABLE,
            severity=Severity.MINOR,
            blocks_testing=True,
            requirements=["R-04"],
        ),
    ]


def verification(findings: Sequence[Finding]) -> Verification:
    return Verification(findings=list(findings))


def segmentation(doc: ParsedDocument) -> Segmentation:
    return fallback_segmentation(doc)


def editor_text(cards: Sequence[dict[str, Any]], dropped: Sequence[dict[str, Any]] = ()) -> str:
    body = json.dumps({"questions": list(cards), "dropped": list(dropped)}, ensure_ascii=False, indent=2)
    return f"## Рассуждение\n\n- Кандидаты разобраны.\n\n## Ответ\n\n```json\n{body}\n```\n"


def cli_payload(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Ответ в формате ``claude -p --output-format json``."""
    return {
        "type": "result",
        "subtype": "success",
        "is_error": is_error,
        "result": text,
        "total_cost_usd": 0.2,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {"claude-opus-5-5": {}},
    }


def editor_llm(text: str, *, is_error: bool = False) -> FakeLLM:
    return FakeLLM(responses={(LLMStep.QUESTIONS, 1): cli_payload(text, is_error=is_error)})


def card(
    candidates: Sequence[str],
    question: str,
    *,
    quotes: Sequence[str] = (),
    options: Sequence[dict[str, Any]] | None = None,
    addressee: Sequence[str] = ("business",),
    topic: str = "тема",
) -> dict[str, Any]:
    return {
        "candidates": list(candidates),
        "addressee": list(addressee),
        "topic": topic,
        "quotes": list(quotes),
        "question": question,
        "why": "От ответа зависит ожидаемый результат теста.",
        "options": list(options)
        if options is not None
        else [
            {"text": "Первое правило", "change": "Поправить первое место", "grounded_in": None},
            {"text": "Второе правило", "change": "Поправить второе место", "grounded_in": None},
        ],
        "if_unanswered": "Команда выберет первое правило.",
    }
