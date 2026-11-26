"""Файл ``questions.yaml`` рядом с отчётом: вопросы автору с пустым полем ответа.

Автор ТЗ отвечает прямо в этом файле, а цикл уточнения (``--answers``, пакет ``validator.clarify``)
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
        answer: null             # буква варианта (A, B, …), свой ответ или «пропустить»; null: нет ответа
        comment: null            # пояснение автора, по желанию

Файл пишется всегда, даже без вопросов (тогда ``questions: []``), чтобы у прогона был один набор выходов.

Прогон с ответами (``--answers``) переносит в конец списка вопросы, на которые автор уже ответил (#142), с
их ответом и полем ``answered_in`` (из какого файла ответ). Тогда этот файл годится для следующего круга:
автор отвечает на оставшиеся открытые вопросы и передаёт в ``--answers`` только его, ответы прошлых кругов
не теряются. Перенесённый вопрос записан коротко: ``id``, ``question``, ``where``, выбранный вариант,
``related_findings``, ``answer``, ``comment``, ``answered_in``.

Ответы автора не затираются. Если в ``questions.yaml`` рядом с отчётом уже что-то вписал человек
(``answered_by``, ``answer`` или ``comment`` хотя бы у одного вопроса) или файл не похож на наш, вопросы
этого прогона пишутся в первый свободный ``questions-2.yaml``, ``questions-3.yaml``, …
(``safe_questions_path``). Так повторный прогон ``--answers out/questions.yaml`` в ту же папку не стирает
ответы, по которым он сделан.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from validator.models import AnswerKind, AuthorAnswer, Question, QuestionOption, Quote
from validator.report.fmt import where

SCHEMA_VERSION = 1
YAML_WIDTH = 120
MAX_SPARE_FILES = 99
"""Сколько запасных имён ``questions-N.yaml`` перебирать; дальше имя с отметкой времени."""

HEADER = (
    "# Вопросы агента QAIRU к автору ТЗ.\n"
    "# Как ответить: в поле answer впишите букву варианта (A, B, …), свой ответ текстом или «пропустить»,\n"
    "# пояснение можно добавить в comment. null значит «ответа нет». «не знаю», «?», «уточню» вопрос не\n"
    "# закрывают: он остаётся открытым.\n"
    "# Повторная проверка с ответами: validator ВХОД ВЫХОД --answers questions.yaml\n"
    "# Не меняйте id и document_sha256: по ним ответ сопоставляется с вопросом и версией документа.\n"
)


def _option(o: QuestionOption) -> dict[str, Any]:
    return {"label": o.label, "text": o.text, "change": o.change, "for_discussion": o.grounded_in is None}


def _place(quote: Quote) -> str:
    """Место цитаты как в отчёте: раздел блока, где она стоит, и страница или строка (#119)."""
    return where(quote.anchor, quote.anchor.section if quote.anchor else None)


def _question(q: Question) -> dict[str, Any]:
    return {
        "id": q.id,
        "priority": q.priority.value,
        "blocks": [s.value for s in q.blocks],
        "addressee": [a.value for a in q.addressee],
        "topic": q.topic,
        "question": q.question,
        "why": q.why,
        "where": [{"quote": quote.located or quote.text, "place": _place(quote)} for quote in q.quotes],
        "options": [_option(o) for o in q.options],
        "if_unanswered": q.if_unanswered,
        "related_findings": list(q.related_findings),
        "answer": None,
        "comment": None,
    }


def carried_answer(a: AuthorAnswer, answered_in: str) -> dict[str, Any]:
    """Отвеченный в прошлом круге вопрос для ``questions-N.yaml``: следующий круг прочитает его ответ.

    Вариант записан вместе с правилом и правкой: если в следующем прогоне такого вопроса не будет, ответ
    всё равно сопоставится по тексту вопроса и местам, а буква не потеряет смысл.
    """
    chosen = a.kind is AnswerKind.OPTION and a.option
    return {
        "id": a.question_id,
        "question": a.question,
        "where": [{"quote": w} for w in a.where],
        "options": [{"label": a.option, "text": a.text, "change": a.change}] if chosen else [],
        "related_findings": list(a.related_findings),
        "answer": a.option if chosen else a.text,
        "comment": a.comment or None,
        "answered_in": answered_in,
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


def _filled(value: object) -> bool:
    """Поле заполнено человеком: не ``null`` и не пустая строка (``0`` и ``false`` тоже ответ)."""
    return value is not None and str(value).strip() != ""


def has_author_input(path: Path) -> bool:
    """Есть ли в файле то, что нельзя затирать: ответ, пояснение или имя отвечавшего.

    Файл, который не читается, не YAML или не похож на ``questions.yaml``, тоже считаем чужим: доказать,
    что в нём нет работы человека, нельзя, поэтому и перезаписывать его нельзя.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return True
    if not isinstance(data, dict) or not isinstance(data.get("questions"), list):
        return True
    if _filled(data.get("answered_by")):
        return True
    return any(
        not isinstance(q, dict) or _filled(q.get("answer")) or _filled(q.get("comment"))
        for q in data["questions"]
    )


def _spare_names(path: Path) -> list[Path]:
    return [path.with_name(f"{path.stem}-{n}{path.suffix}") for n in range(2, MAX_SPARE_FILES + 2)]


def safe_questions_path(path: Path) -> Path:
    """Куда писать вопросы прогона, не затирая ответы автора.

    ``path``, если файла нет или в нём нет ответов человека (прошлый прогон без ответов перезаписываем, как
    раньше); иначе первое такое же безопасное имя ``questions-2.yaml``, ``questions-3.yaml``, …
    """
    for candidate in (path, *_spare_names(path)):
        if not candidate.exists() or not has_author_input(candidate):
            return candidate
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def write_questions_yaml(
    path: Path,
    questions: list[Question],
    doc_name: str,
    doc_sha256: str | None,
    kept: Path | None = None,
    answered: Sequence[AuthorAnswer] = (),
    answers_file: str | None = None,
) -> None:
    """Файл вопросов по схеме модуля.

    ``kept``: файл с ответами автора, ради которого выбрано другое имя. ``answered``: ответы автора из
    ``answers_file`` (прогон с ``--answers``); отвеченные переносятся в конец списка (#142).
    """
    payload = questions_payload(questions, doc_name, doc_sha256)
    open_ids = {q.id for q in questions}
    carried = [a for a in answered if a.kind.answered and a.question_id not in open_ids]
    if carried:
        source = answers_file or "прошлый круг"
        payload["questions"] += [carried_answer(a, source) for a in carried]
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=YAML_WIDTH)
    note = (
        f"# Ответы в {kept.name} не тронуты: вопросы этого прогона записаны сюда, чтобы их не затереть.\n"
        if kept is not None
        else ""
    )
    if carried:
        note += (
            f"# В конце списка уже отвеченные вопросы ({len(carried)}), ответы взяты из {source}\n"
            "# (поле answered_in). Следующий круг: ответьте на открытые вопросы и передайте этот файл\n"
            "# в --answers, прежние ответы тоже учтутся.\n"
        )
    path.write_text(HEADER + note + body, encoding="utf-8")
