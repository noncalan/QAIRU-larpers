"""Шаг 8: вопросы автору ТЗ (исследование 17; критерий хакатона «Уточнение»).

Контракт шага: ``ask(ver, doc, seg, llm, cfg) -> list[Question]``. Конвейер вызывает
``build_questions`` с теми же аргументами: он возвращает ещё и диагностику шага для отчёта.

Как устроено (модель предлагает, код проверяет):

1. ``candidates.py``: кандидатов собирает код из находок верификатора, 0 вызовов модели;
2. ``editor.py``: один вызов редактора (Opus 5.5 ``high``, рассуждение текстом, затем JSON) переписывает
   кандидатов в 8–15 карточек по п. 3.1 исследования 17;
3. ``cards.py``: код сверяет цитаты и основания вариантов, проверяет 2–4 варианта с последствиями,
   отбрасывает карточки без связанных находок, сливает повторы, считает приоритет и ставит ``Q-NN``;
4. ``fallback.py``: если редактор не ответил, карточки собираются кодом из кандидатов, без редактуры;
5. ``export.py``: ``questions.yaml`` для ответов автора (схема в docstring модуля).

У ``QuestionOption.grounded_in`` три значения: дословный фрагмент документа (вариант опирается на
цитату, код её сверил), ``F-NN`` (шаблонный вариант запасного пути по находке) или None (вариант
предложила модель, в отчёте он помечается как вариант для обсуждения).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from validator.errors import LLM_QUESTIONS_CHECKED, LLM_QUESTIONS_FAILED, LLMError
from validator.llm.base import LLMClient
from validator.models import (
    DiagLevel,
    Diagnostic,
    ParsedDocument,
    Question,
    QuestionPriority,
    RunConfig,
    Segmentation,
    Verification,
)
from validator.questions.candidates import Candidate, collect_candidates
from validator.questions.cards import CardDraft, CheckLog, draft_from_card, finalize
from validator.questions.editor import EditorReply, edit, normalize_candidate_id
from validator.questions.export import questions_payload, write_questions_yaml
from validator.questions.fallback import draft_from_candidate
from validator.questions.scoring import priority

logger = logging.getLogger("validator")
STEP = "questions"


@dataclass(frozen=True)
class QuestionsOutcome:
    """Итог шага 8: карточки, диагностика для отчёта и как они получены."""

    questions: list[Question] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    candidates: int = 0
    edited: bool = False


def _check_diagnostic(log: CheckLog, candidates: int, questions: int) -> list[Diagnostic]:
    problems = log.problems()
    if not problems:
        return []
    return [
        Diagnostic(
            code=LLM_QUESTIONS_CHECKED,
            level=DiagLevel.INFO,
            message=f"Вопросы автору: код поправил карточки ({'; '.join(problems)})",
            action=f"В отчёт вошло вопросов: {questions}, кандидатов было: {candidates}",
            step=STEP,
        )
    ]


def _fallback(
    cands: list[Candidate], ver: Verification, seg: Segmentation, reason: str, hint: str
) -> QuestionsOutcome:
    log = CheckLog()
    req_types = {r.id: r.type for r in seg.requirements}
    drafts = [draft_from_candidate(c) for c in cands]
    questions = finalize(drafts, {f.id: f for f in ver.findings}, req_types, log, merge_similar=False)
    failed = Diagnostic(
        code=LLM_QUESTIONS_FAILED,
        level=DiagLevel.WARNING,
        message=f"Редактор вопросов не сработал: {reason}",
        action="Вопросы собраны кодом из находок без редактуры моделью: формулировки шаблонные",
        hint=hint or "Повторите прогон; если повторяется, сообщите команде",
        step=STEP,
    )
    logger.warning("Вопросы: запасной путь без редактора (%s), вопросов %d", reason, len(questions))
    return QuestionsOutcome(
        questions=questions,
        diagnostics=[failed, *_check_diagnostic(log, len(cands), len(questions))],
        candidates=len(cands),
    )


def _unanswered(
    reply: EditorReply, cands: list[Candidate], drafts: list[CardDraft], log: CheckLog
) -> tuple[set[str], list[Candidate]]:
    """Кого редактор отклонил с причиной, и кого не взял и не отклонил (молча пропустил)."""
    known = {c.cid for c in cands}
    dropped = {i for i in (normalize_candidate_id(d.candidate) for d in reply.dropped) if i in known}
    used = {cid for d in drafts for cid in d.candidate_ids}
    ignored = [c for c in cands if c.cid not in used and c.cid not in dropped]
    log.ignored += len(ignored)
    return dropped - used, ignored


def _empty_note(dropped: int, ignored: int) -> Diagnostic:
    return Diagnostic(
        code=LLM_QUESTIONS_CHECKED,
        level=DiagLevel.INFO,
        message=(
            "Редактор вопросов не задал ни одного вопроса: отклонил кандидатов "
            f"(ответ есть в документе или правка однозначна): {dropped}, пропустил без причины: {ignored}"
        ),
        action="Раздел вопросов пуст, находки остались в отчёте",
        step=STEP,
    )


def _from_reply(
    reply: EditorReply, cands: list[Candidate], doc: ParsedDocument, ver: Verification, seg: Segmentation
) -> QuestionsOutcome | None:
    """Карточки редактора после проверок; None, если ответ непригоден и нужен запасной путь.

    Кандидатов, которых редактор молча пропустил, считаем в диагностике; те из них, что блокируют
    разработку (P0), не теряем: для них карточка собирается кодом, как на запасном пути.
    """
    log = CheckLog(invalid=reply.invalid)
    by_id = {c.cid: c for c in cands}
    drafts = [d for d in (draft_from_card(card, by_id, doc, log) for card in reply.cards) if d is not None]
    dropped, ignored = _unanswered(reply, cands, drafts, log)
    if not drafts and (reply.cards or reply.invalid or not dropped):
        return None
    req_types = {r.id: r.type for r in seg.requirements}
    rescued = [c for c in ignored if priority(c.findings, req_types) is QuestionPriority.P0]
    log.rescued += len(rescued)
    drafts += [draft_from_candidate(c) for c in rescued]
    questions = finalize(drafts, {f.id: f for f in ver.findings}, req_types, log)
    logger.info(
        "Вопросы: кандидатов %d, карточек редактора %d, отклонил %d, пропустил %d, в отчёте %d",
        len(cands),
        len(reply.cards) + reply.invalid,
        len(dropped),
        len(ignored),
        len(questions),
    )
    diagnostics = _check_diagnostic(log, len(cands), len(questions))
    if not questions:
        diagnostics.append(_empty_note(len(dropped), len(ignored)))
    return QuestionsOutcome(questions=questions, diagnostics=diagnostics, candidates=len(cands), edited=True)


def build_questions(
    ver: Verification, doc: ParsedDocument, seg: Segmentation, llm: LLMClient, cfg: RunConfig
) -> QuestionsOutcome:
    """Вопросы автору и диагностика шага. Без кандидатов модель не вызывается."""
    cands = collect_candidates(ver)
    if not cands:
        logger.info("Вопросы: кандидатов нет, редактор не вызывался")
        return QuestionsOutcome()
    req_types = {r.id: r.type for r in seg.requirements}
    try:
        outcome = _from_reply(edit(cands, doc, req_types, llm, cfg), cands, doc, ver, seg)
    except LLMError as exc:
        return _fallback(cands, ver, seg, exc.message, exc.hint)
    except Exception as exc:  # граница редактора: ответ модели внешний, карточки кода терять нельзя
        logger.exception("Вопросы: сбой при обработке ответа редактора")
        return _fallback(
            cands,
            ver,
            seg,
            f"внутренняя ошибка при обработке ответа: {type(exc).__name__}",
            "Подробности в report.log; сообщите команде",
        )
    if outcome is None:
        return _fallback(
            cands,
            ver,
            seg,
            "ни одна карточка не прошла проверку кодом",
            "Повторите прогон; если повторяется, сообщите команде",
        )
    return outcome


def ask(
    ver: Verification, doc: ParsedDocument, seg: Segmentation, llm: LLMClient, cfg: RunConfig
) -> list[Question]:
    """Контракт шага 8 из плана: только карточки (диагностику даёт ``build_questions``)."""
    return build_questions(ver, doc, seg, llm, cfg).questions


__all__ = [
    "QuestionsOutcome",
    "ask",
    "build_questions",
    "questions_payload",
    "write_questions_yaml",
]
