"""Шаг 8: вопросы автору ТЗ (заглушка каркаса) и файл ``questions.yaml``.

Контракт: ``ask(ver, doc, seg, llm, cfg) -> list[Question]``. Кандидатов кодом и редактора (Opus 5.5 high)
делает #20, он же окончательно задаёт схему ``questions.yaml``. Заглушка превращает в вопрос каждую
находку с вердиктом «неясно» или с плейсхолдером ``[уточнить: …]`` в формулировке, без модели.

Схема ``questions.yaml`` (каркас)::

    document: имя файла
    document_sha256: хеш документа, к которому вопросы
    questions:
      - id: Q-01
        priority: P0 | P1 | P2
        question: текст вопроса
        options: [{label: A, text: …}, …]
        related_findings: [F-01]
        answer: null        # автор пишет сюда букву варианта или свой текст
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from validator.llm.base import LLMClient
from validator.models import (
    Addressee,
    Finding,
    ParsedDocument,
    Question,
    QuestionOption,
    QuestionOrigin,
    QuestionPriority,
    RunConfig,
    Segmentation,
    Severity,
    Verdict,
    Verification,
    WorkStage,
)

MAX_QUESTIONS = 15
CLARIFY_MARKER = "[уточнить"


def _priority(f: Finding) -> QuestionPriority:
    if f.severity is Severity.CRITICAL:
        return QuestionPriority.P0
    return QuestionPriority.P1 if f.blocks_testing else QuestionPriority.P2


def _question(f: Finding, number: int) -> Question:
    priority = _priority(f)
    blocks = (
        [WorkStage.DEVELOPMENT, WorkStage.TESTING] if priority is QuestionPriority.P0 else [WorkStage.TESTING]
    )
    options = [
        QuestionOption(
            label="A",
            text=f.suggestion or "Уточнить формулировку в документе",
            change="Заменить формулировку в документе",
        ),
        QuestionOption(
            label="B", text="Оставить как есть", change="Зафиксировать текущую трактовку в документе"
        ),
    ]
    return Question(
        id=f"Q-{number:02d}",
        priority=priority,
        blocks=blocks,
        addressee=[Addressee.ANALYST],
        topic=f.property.label.lower(),
        quotes=[q for q in f.quotes if q.verified],
        question=f"Как правильно понимать это место? {f.explanation}",
        why=f"От ответа зависит реализация и ожидаемый результат тестов ({f.property.label.lower()}).",
        options=options,
        if_unanswered="Команда возьмёт трактовку по текущему тексту; ошибка выяснится на приёмке.",
        related_findings=[f.id],
        origin=QuestionOrigin.UNCLEAR_VERDICT if f.verdict is Verdict.UNCLEAR else QuestionOrigin.FINDING,
        standard=list(f.property.refs),
    )


def ask(
    ver: Verification, doc: ParsedDocument, seg: Segmentation, llm: LLMClient, cfg: RunConfig
) -> list[Question]:
    """Заглушка: вопрос на каждую находку «неясно» или с ``[уточнить: …]``, без модели (#20 заменит)."""
    del doc, seg, llm, cfg
    picked = [
        f
        for f in ver.findings
        if f.verdict is Verdict.UNCLEAR or (f.suggestion and CLARIFY_MARKER in f.suggestion)
    ]
    picked.sort(key=lambda f: (_priority(f).value, f.id))
    return [_question(f, n) for n, f in enumerate(picked[:MAX_QUESTIONS], start=1)]


def questions_payload(questions: list[Question], doc_name: str, doc_sha256: str | None) -> dict[str, Any]:
    return {
        "document": doc_name,
        "document_sha256": doc_sha256,
        "questions": [
            {
                "id": q.id,
                "priority": q.priority.value,
                "question": q.question,
                "options": [{"label": o.label, "text": o.text} for o in q.options],
                "related_findings": list(q.related_findings),
                "answer": None,
            }
            for q in questions
        ],
    }


def write_questions_yaml(
    path: Path, questions: list[Question], doc_name: str, doc_sha256: str | None
) -> None:
    payload = questions_payload(questions, doc_name, doc_sha256)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
