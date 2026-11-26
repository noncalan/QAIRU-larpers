"""Цикл уточнения: ответы автора ТЗ → повторная проверка → раздел «Уточнения» (issue #42).

Прогон 1 пишет рядом с отчётом ``questions.yaml``. Автор вписывает ответы (или отвечает в терминале,
``--ask``), и прогон 2 ``validator ВХОД ВЫХОД --answers answers.yaml`` делает всё то же самое, что прогон 1
(с кешем ответов модели это бесплатно), а затем один вызов сверки:

1. ``answers.py`` читает файл ответов, ошибки схемы дают понятный текст и код 2 (CLI);
2. ответы сопоставляются с вопросами этого прогона (по тексту вопроса, затем по номеру и местам);
   отвеченный вопрос больше не открыт, отложенный и пустой остаются открытыми;
3. ``model.py``: модель (Opus 5.5 ``high``) видит документ, находки и ответы как данные и предлагает,
   какие находки ответ закрыл, какие уточнил и какие новые дефекты из-за ответа появились;
4. ``apply.py``: код проверяет связь находки с вопросом и цитаты новых находок, пересчитывает сводку,
   статус требований и светофор; закрытые находки уходят в раздел «Уточнения»;
5. если сверка не сработала, ответы всё равно видны в отчёте, находки остаются как были (``LLM-060``).

Исходный документ не меняется, цитаты по-прежнему сверяются только с ним: ответ автора это решение,
которое ещё нужно внести в текст, поэтому у закрытой находки есть предложенная правка.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from validator.clarify.answers import AnswersError, AnswersFile, RawAnswer, load_answers, resolve_answer
from validator.clarify.apply import AnswerContext, apply_reply, recount
from validator.clarify.model import build_request, parse_reply
from validator.errors import (
    CFG_ANSWERS_MISMATCH,
    LLM_CLARIFY_CHECKED,
    LLM_CLARIFY_FAILED,
    LLMError,
)
from validator.llm.base import LLMClient
from validator.models import (
    AuthorAnswer,
    Clarification,
    ClarifyEffect,
    ClarifySnapshot,
    DiagLevel,
    Diagnostic,
    ParsedDocument,
    Question,
    Quote,
    RunConfig,
    Segmentation,
    Verdict,
    Verification,
)
from validator.questions.quotes import verify_quote
from validator.text import MIN_FUZZY_CHARS, normalize
from validator.verify import BLOCKING_PRIORITIES, light_with_questions

logger = logging.getLogger("validator")
STEP = "clarify"


@dataclass(frozen=True)
class ClarifyOutcome:
    """Итог шага: проверка после ответов, открытые вопросы, раздел «Уточнения» и диагностика."""

    verification: Verification
    questions: list[Question]
    clarification: Clarification
    diagnostics: list[Diagnostic] = field(default_factory=list)


# ---------------------------------------------------------------- ответы ↔ вопросы прогона


def _norm(text: str) -> str:
    return " ".join(normalize(text)[0].lower().split())


def _match(raw: RawAnswer, questions: Sequence[Question]) -> Question | None:
    """Вопрос этого прогона, на который отвечает автор: тот же текст, иначе тот же номер и те же места.

    Если в файле нет ни текста вопроса, ни мест (автор написал только ``id`` и ``answer``), хватает номера.
    """
    text = _norm(raw.question)
    if text:
        same_text = next((q for q in questions if _norm(q.question) == text), None)
        if same_text is not None:
            return same_text
    same_id = next((q for q in questions if q.id == raw.question_id), None)
    if same_id is None:
        return None
    if not text and not raw.where:
        return same_id
    places = {_norm(w) for w in raw.where}
    if places & {_norm(x.located or x.text) for x in same_id.quotes}:
        return same_id
    return None


def _context(answer: AuthorAnswer, question: Question | None, doc: ParsedDocument) -> AnswerContext:
    """С чем код сверяет связь «находка ↔ ответ»: места вопроса в документе и его находки в этом прогоне.

    Находки из файла (``related_findings``) берём, только если вопрос задан и в этом прогоне: номера ``F-NN``
    другого прогона могут указывать на другие находки.
    """
    places: list[Quote] = list(question.quotes) if question is not None else []
    if question is None:  # вопрос не задан в этом прогоне: места берём из файла, короткие найдутся где угодно
        located = (verify_quote(doc, w, ()) for w in answer.where if len(normalize(w)[0]) >= MIN_FUZZY_CHARS)
        places += [q for q in located if q is not None]
    findings = frozenset(question.related_findings) if question is not None else frozenset()
    return AnswerContext(answer=answer, places=places, findings=findings)


@dataclass
class _Matched:
    answers: list[AuthorAnswer] = field(default_factory=list)
    contexts: dict[str, AnswerContext] = field(default_factory=dict)
    closed_questions: set[str] = field(default_factory=set)
    warnings: list[str] = field(default_factory=list)


def _match_all(file: AnswersFile, questions: Sequence[Question], doc: ParsedDocument) -> _Matched:
    out = _Matched()
    for raw in file.answers:
        question = _match(raw, questions)
        options = {o.label.upper(): (o.text, o.change) for o in question.options} if question else None
        answer, warning = resolve_answer(raw, options)
        if question is not None:
            answer = answer.model_copy(
                update={
                    "question_id": question.id,
                    "asked_again": True,
                    "question": answer.question or question.question,
                }
            )
        if warning:
            out.warnings.append(warning)
        out.answers.append(answer)
        if answer.kind.answered:
            out.contexts[answer.question_id] = _context(answer, question, doc)
            if question is not None:
                out.closed_questions.add(question.id)
    return out


# ---------------------------------------------------------------- диагностика


def _mismatch(file: AnswersFile, doc: ParsedDocument, matched: _Matched) -> list[Diagnostic]:
    problems = list(matched.warnings)
    if file.document_sha256 and doc.sha256 and file.document_sha256 != doc.sha256.lower():
        problems.insert(0, "ответы даны к другой версии документа (SHA-256 в файле не совпал с проверенным)")
    if not matched.contexts:
        problems.append("в файле нет ни одного ответа (варианта или своего текста)")
    if not problems:
        return []
    return [
        Diagnostic(
            code=CFG_ANSWERS_MISMATCH,
            level=DiagLevel.WARNING,
            message=f"Файл ответов {file.path.name}: {'; '.join(problems)}",
            action="Учтены только ответы с известным вариантом или своим текстом; вопросы сверены по тексту",
            hint="Отвечайте в questions.yaml последнего прогона этого документа",
            location=file.path.name,
            step=STEP,
        )
    ]


def _failed(reason: str, hint: str) -> Diagnostic:
    logger.warning("Сверка с ответами автора не сработала: %s", reason)
    return Diagnostic(
        code=LLM_CLARIFY_FAILED,
        level=DiagLevel.WARNING,
        message=f"Сверка находок с ответами автора не сработала: {reason}",
        action="Ответы показаны в разделе «Уточнения», находки остались как без ответов",
        hint=hint or "Повторите прогон; если повторяется, сообщите команде",
        step=STEP,
    )


def _checked(details: list[str], kept: int) -> list[Diagnostic]:
    if not details:
        return []
    return [
        Diagnostic(
            code=LLM_CLARIFY_CHECKED,
            level=DiagLevel.INFO,
            message=f"Сверка с ответами автора: код не принял часть выводов модели ({'; '.join(details)})",
            action=f"Принято выводов: {kept}",
            step=STEP,
        )
    ]


# ---------------------------------------------------------------- шаг целиком


def snapshot(ver: Verification, questions: Sequence[Question]) -> ClarifySnapshot:
    return ClarifySnapshot(
        traffic_light=ver.traffic_light,
        findings=len(ver.findings),
        violations=sum(f.verdict is Verdict.VIOLATION for f in ver.findings),
        critical=ver.summary.critical,
        open_questions=len(questions),
        open_blocking=sum(q.priority in BLOCKING_PRIORITIES for q in questions),
    )


def clarify(
    ver: Verification,
    questions: Sequence[Question],
    file: AnswersFile,
    doc: ParsedDocument,
    seg: Segmentation,
    llm: LLMClient,
    cfg: RunConfig,
) -> ClarifyOutcome:
    """Проверка после ответов автора. Модель вызывается один раз и только если есть хоть один ответ.

    ``ver`` и ``questions``: результат этого прогона до ответов (светофор уже с учётом вопросов).
    """
    before = snapshot(ver, questions)
    matched = _match_all(file, questions, doc)
    open_questions = [q for q in questions if q.id not in matched.closed_questions]
    diagnostics = _mismatch(file, doc, matched)
    result, effects, reviewed = recount(ver, ver.findings, seg), [], False
    if matched.contexts:
        answered = [c.answer for c in matched.contexts.values()]
        try:
            reply = parse_reply(llm.call(build_request(ver.findings, answered, doc, cfg)))
            applied = apply_reply(reply, ver, matched.contexts, doc, seg)
            result, effects, reviewed = applied.verification, applied.effects, True
            diagnostics += _checked(applied.log.details(), len(effects))
        except LLMError as exc:
            diagnostics.append(_failed(exc.message, exc.hint))
        except Exception as exc:  # граница сверки: ответы автора и находки прогона терять нельзя
            logger.exception("Сверка с ответами автора: внутренняя ошибка")
            diagnostics.append(
                _failed(
                    f"внутренняя ошибка: {type(exc).__name__}", "Подробности в report.log; сообщите команде"
                )
            )
    final = light_with_questions(result, open_questions)
    clarification = Clarification(
        answers_file=file.path.name,
        answers_sha256=file.sha256,
        answered_by=file.answered_by,
        document_matches=(file.document_sha256 == doc.sha256.lower())
        if file.document_sha256 and doc.sha256
        else None,
        answers=matched.answers,
        effects=effects,
        reviewed=reviewed,
        before=before,
        after=snapshot(final, open_questions),
    )
    closed = len(clarification.by_effect(ClarifyEffect.CLOSED))
    logger.info(
        "Уточнения: ответов %d из %d, закрыто находок %d, уточнено %d, новых %d, открытых вопросов %d",
        len(matched.contexts),
        len(file.answers),
        closed,
        len(clarification.by_effect(ClarifyEffect.REFINED)),
        len(clarification.by_effect(ClarifyEffect.NEW)),
        len(open_questions),
    )
    return ClarifyOutcome(
        verification=final, questions=open_questions, clarification=clarification, diagnostics=diagnostics
    )


__all__ = ["AnswersError", "AnswersFile", "ClarifyOutcome", "clarify", "load_answers", "snapshot"]
