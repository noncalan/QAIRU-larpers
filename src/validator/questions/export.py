"""Файл ``questions.yaml`` рядом с отчётом: вопросы автору с пустым полем ответа.

Автор ТЗ отвечает прямо в этом файле, а цикл уточнения (``--answers``, задача про ``answers.yaml``)
читает ответы. Схема, версия 1::

    schema_version: 1
    document: имя проверенного файла
    document_sha256: хеш документа; ответы относятся только к этой версии документа
    answered_by: null            # кто отвечал (заполняет автор)
    questions:
      - id: Q-01                 # не менять: по нему ответ сопоставляется с вопросом
        priority: P0             # P0 блокирует разработку, P1 тестирование, P2 уточнить параллельно
        blocks: [development, testing]
        addressee: [business, analyst]   # business, analyst, architect, security
        topic: тема в 2–4 словах
        question: вопрос одним предложением со сценарием
        why: что неизвестно и что от этого зависит
        where:                   # места в документе, о которых вопрос (цитаты сверены кодом)
          - quote: дословный фрагмент документа
            place: раздел, строка или страница
        options:
          - label: A
            text: правило поведения
            change: что поменять в документе или реализации
            for_discussion: true # вариант предложила модель, в документе ему опоры нет
        if_unanswered: какое допущение примет команда без ответа
        related_findings: [F-01, F-03]   # находки отчёта, которые закроет ответ
        answer: null             # буква варианта (A, B, …) или свой ответ текстом; null: нет ответа
        comment: null            # пояснение автора, по желанию

Файл пишется всегда, даже без вопросов (тогда ``questions: []``), чтобы у прогона был один набор выходов.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from validator.models import Question, QuestionOption
from validator.report.fmt import where

SCHEMA_VERSION = 1
YAML_WIDTH = 120

HEADER = (
    "# Вопросы агента QAIRU к автору ТЗ.\n"
    "# Как ответить: в поле answer впишите букву варианта (A, B, …) или свой ответ текстом,\n"
    "# пояснение можно добавить в comment. null значит «ответа нет».\n"
    "# Не меняйте id и document_sha256: по ним ответ сопоставляется с вопросом и версией документа.\n"
)


def _option(o: QuestionOption) -> dict[str, Any]:
    return {"label": o.label, "text": o.text, "change": o.change, "for_discussion": o.grounded_in is None}


def _question(q: Question) -> dict[str, Any]:
    return {
        "id": q.id,
        "priority": q.priority.value,
        "blocks": [s.value for s in q.blocks],
        "addressee": [a.value for a in q.addressee],
        "topic": q.topic,
        "question": q.question,
        "why": q.why,
        "where": [{"quote": quote.located or quote.text, "place": where(quote.anchor)} for quote in q.quotes],
        "options": [_option(o) for o in q.options],
        "if_unanswered": q.if_unanswered,
        "related_findings": list(q.related_findings),
        "answer": None,
        "comment": None,
    }


def questions_payload(questions: list[Question], doc_name: str, doc_sha256: str | None) -> dict[str, Any]:
    """Содержимое ``questions.yaml`` как словарь (схема в docstring модуля)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "document": doc_name,
        "document_sha256": doc_sha256,
        "answered_by": None,
        "questions": [_question(q) for q in questions],
    }


def write_questions_yaml(
    path: Path, questions: list[Question], doc_name: str, doc_sha256: str | None
) -> None:
    payload = questions_payload(questions, doc_name, doc_sha256)
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=YAML_WIDTH)
    path.write_text(HEADER + body, encoding="utf-8")
