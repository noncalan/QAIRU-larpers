"""Шаг 8г: критик-самоответ, второй вызов шага вопросов (исследование 17, п. 4.2, шаг 4; ConRAP).

Модель (D-010: Opus 5.5 ``high``) получает весь документ с ID блоков и готовые карточки и для каждой
пытается ответить на вопрос по документу, а заодно проверяет формулировку по списку ошибок п. 4.4.
Решает код, а не модель:

- вопрос снимается, только если критик дал цитату-ответ не короче трёх слов, ``locate_quote`` нашёл её
  в документе и она не из тех блоков, о которых спрашивает сам вопрос (иначе критик «отвечает» неясным
  местом); номер снятого вопроса не переиспользуется, отчёт показывает его в «Снятых вопросах»;
- цитата-ответ не найдена → вопрос остаётся (CLAUDE.md, правило 4: без сверенной цитаты нет вывода);
- «здравый смысл», «правка однозначна», сценарий и ошибки формулировки не снимают вопрос: код не может
  их проверить, поэтому они идут в метрики вопросов (``metrics.py``) и в ``Question.review``;
- критик не ответил, ответ без JSON или ни одного отзыва на вопросы прогона → вопросы остаются как были,
  диагностика ``LLM-052``.

Формат ответа как у редактора: рассуждение текстом, затем один блок ```json со списком ``reviews``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import Field, ValidationError, ValidationInfo, field_validator

from validator.errors import (
    LLM_BAD_RESPONSE,
    LLM_QUESTIONS_CRITIC_FAILED,
    LLM_QUESTIONS_WITHDRAWN,
    LLMError,
)
from validator.ingest.blocks import render_for_model
from validator.llm.base import LLMClient
from validator.models import (
    DiagLevel,
    Diagnostic,
    LLMRequest,
    LLMResponse,
    LLMStep,
    ParsedDocument,
    Question,
    QuestionReview,
    Quote,
    RunConfig,
    WithdrawnQuestion,
)
from validator.questions.editor import _Lenient, extract_json
from validator.questions.quotes import span, verify_quote
from validator.text import MIN_FUZZY_CHARS, locate_quote, normalize

logger = logging.getLogger("validator")

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
SYSTEM_PROMPT = PROMPTS_DIR / "questions_critic_system.md"
USER_PROMPT = PROMPTS_DIR / "questions_critic_user.md"
CALL_INDEX = 1
"""Критик вызывается один раз за прогон, пакетно на все вопросы."""
STEP = "questions"
REPLY_KEY = "reviews"
MIN_ANSWER_WORDS = 3
"""Цитата-ответ короче трёх слов (или ``MIN_FUZZY_CHARS`` символов) не снимает вопрос: найдётся где угодно."""

FORMULATION_ERRORS = {
    1: "общий, без привязки к месту документа",
    2: "несколько решений в одном вопросе",
    3: "длиннее одного предложения",
    4: "жаргон не по профилю адресата",
    5: "просит техническое решение вместо правила",
    6: "наводящий",
    7: "варианты не различаются или неполны",
    8: "непонятно, что изменится в документе",
}
"""Ошибки формулировки п. 4.4 исследования 17 в нумерации промпта критика (без «ответ есть в документе»:
это проверяет сам самоответ)."""

_PLACEHOLDER = re.compile(r"\{(document|questions)\}")
_QUESTION_ID = re.compile(r"Q\s*-?\s*0*(\d+)", re.IGNORECASE)


# ---------------------------------------------------------------- схема ответа


class CriticReview(_Lenient):
    """Отзыв критика на один вопрос. Разбор мягкий: запись не по схеме выпадает одна."""

    id: str
    answered: bool = False
    answer: str = ""
    answer_quote: str | None = None
    common_sense: bool = False
    obvious_fix: bool = False
    scenario: bool | None = None
    errors: list[int] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _id_as_text(cls, raw: object) -> object:
        """``1`` числом вместо ``"Q-01"``: номер вопроса."""
        return f"Q-{raw:02d}" if isinstance(raw, int) and not isinstance(raw, bool) else raw

    @field_validator("answered", "common_sense", "obvious_fix", "answer", mode="before")
    @classmethod
    def _null_is_default(cls, raw: object, info: ValidationInfo) -> object:
        """``null`` вместо пустого значения («оставь пустыми» модель читает и так)."""
        if raw is not None:
            return raw
        return "" if info.field_name == "answer" else False

    @field_validator("errors", mode="before")
    @classmethod
    def _known_errors(cls, raw: object) -> object:
        """Только номера из списка промпта, без повторов; «3» строкой тоже годится."""
        if not isinstance(raw, list):
            return []
        numbers = (int(x) for x in raw if isinstance(x, int | str) and str(x).strip().isdigit())
        return sorted({n for n in numbers if n in FORMULATION_ERRORS})


class CriticReplySchema(_Lenient):
    """Схема ответа критика целиком: образец в ``questions_critic_system.md`` обязан ей соответствовать."""

    reviews: list[CriticReview]


# ---------------------------------------------------------------- промпт


def normalize_question_id(raw: str) -> str | None:
    """``Q-01``, ``q1``, ``Q 1`` → ``Q-01``; не ID вопроса → None."""
    m = _QUESTION_ID.fullmatch(raw.strip())
    return f"Q-{int(m.group(1)):02d}" if m else None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_question(q: Question) -> str:
    """Вопрос для критика: формулировка, адресат, места документа, варианты. Приоритет и находки не нужны."""
    lines = [f"### {q.id}", f"Адресат: {', '.join(a.label for a in q.addressee) or 'не указан'}"]
    if q.topic:
        lines.append(f"Тема: {_one_line(q.topic)}")
    lines.append(f"Вопрос: {_one_line(q.question)}")
    if q.why:
        lines.append(f"Почему важно: {_one_line(q.why)}")
    if q.quotes:
        lines.append("Места документа, о которых вопрос:")
        for x in q.quotes:
            block = x.anchor.block_ids[0] if x.anchor and x.anchor.block_ids else "?"
            lines.append(f"- [{block}] «{_one_line(x.located or x.text)}»")
    if q.options:
        lines.append("Варианты ответа:")
        lines += [f"- {o.label}. {_one_line(o.text)} → {_one_line(o.change)}" for o in q.options]
    return "\n".join(lines)


def build_request(questions: Sequence[Question], doc: ParsedDocument, cfg: RunConfig) -> LLMRequest:
    model = cfg.model_for(LLMStep.QUESTIONS_CRITIC)
    values = {
        "document": render_for_model(doc).rstrip("\n"),
        "questions": "\n\n".join(render_question(q) for q in questions),
    }
    prompt = _PLACEHOLDER.sub(lambda m: values[m.group(1)], USER_PROMPT.read_text(encoding="utf-8"))
    return LLMRequest(
        step=LLMStep.QUESTIONS_CRITIC,
        call_index=CALL_INDEX,
        model=model.model,
        effort=model.effort,
        system=SYSTEM_PROMPT.read_text(encoding="utf-8").strip(),
        prompt=prompt,
        json_schema=None,
        timeout_s=cfg.call_timeout_s,
    )


# ---------------------------------------------------------------- разбор ответа


def parse_reply(resp: LLMResponse) -> list[CriticReview]:
    """Отзывы из ответа критика. Нет JSON со списком ``reviews`` → ``LLMError``."""
    data = extract_json(resp.text, REPLY_KEY)
    if data is None:
        raise LLMError(
            "В ответе критика вопросов нет JSON со списком reviews",
            code=LLM_BAD_RESPONSE,
            hint="Повторите прогон; если повторяется, сообщите команде",
            location=resp.call_id,
            retryable=True,
        )
    reviews: list[CriticReview] = []
    for raw in data[REPLY_KEY]:
        try:
            reviews.append(CriticReview.model_validate(raw))
        except ValidationError:
            continue
    return reviews


# ---------------------------------------------------------------- решение кода


@dataclass
class CriticLog:
    """Что код сделал с отзывами критика: из этого собирается строка диагностики."""

    withdrawn: int = 0
    unverified: int = 0
    own_quote: int = 0
    missing: int = 0

    def details(self) -> list[str]:
        labels = (
            (self.unverified, "ответ критика без сверенной цитаты, вопрос оставлен"),
            (self.own_quote, "критик сослался на то самое неясное место, вопрос оставлен"),
            (self.missing, "вопросов без отзыва критика"),
        )
        return [f"{text}: {count}" for count, text in labels if count]


@dataclass(frozen=True)
class CriticOutcome:
    """Итог критика: оставленные вопросы, снятые с цитатой-ответом, диагностика."""

    questions: list[Question] = field(default_factory=list)
    withdrawn: list[WithdrawnQuestion] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    reviewed: bool = False


def _blocks(quotes: Sequence[Quote]) -> list[str]:
    """Блоки верхнего уровня (``B012``, без номера предложения), где лежат цитаты."""
    ids = (b.split(".")[0] for x in quotes if x.anchor for b in x.anchor.block_ids)
    return list(dict.fromkeys(ids))


def _overlaps(answer: Quote, own: Sequence[Quote]) -> bool:
    """Цитата-ответ задевает место, о котором спрашивает сам вопрос: тот же блок или пересечение."""
    a = span(answer)
    if a is not None and any(s is not None and a[0] < s[1] and s[0] < a[1] for s in map(span, own)):
        return True
    return bool(set(_blocks([answer])) & set(_blocks(own)))


def _too_short(raw: str) -> bool:
    """«да», «30», «API» найдутся почти в любом документе: такая цитата ничего не доказывает."""
    text = normalize(raw)[0]
    return len(text) < MIN_FUZZY_CHARS or len(text.split()) < MIN_ANSWER_WORDS


def _locate_answer(raw: str, q: Question, doc: ParsedDocument) -> Quote | None:
    """Цитата-ответ в документе. Если тот же текст есть в местах самого вопроса, берём его там:
    тогда ``_overlaps`` увидит самоподтверждение, даже когда текст повторяется в документе."""
    own = _blocks(q.quotes)
    if own:
        inside = locate_quote(doc, raw, within=own)
        if inside.verified:
            return inside
    return verify_quote(doc, raw, ())


def _review(r: CriticReview, answer: Quote | None) -> QuestionReview:
    return QuestionReview(
        answered=r.answered,
        answer=_one_line(r.answer),
        answer_quote=answer,
        common_sense=r.common_sense,
        obvious_fix=r.obvious_fix,
        scenario=r.scenario,
        errors=list(r.errors),
    )


def _judge(q: Question, r: CriticReview, doc: ParsedDocument, log: CriticLog) -> Question | WithdrawnQuestion:
    """Карточка с отзывом критика или снятый вопрос, если ответ подтверждён сверенной цитатой."""
    raw = (r.answer_quote or "").strip()
    if not r.answered:
        return q.model_copy(update={"review": _review(r, None)})
    found = None if not raw or _too_short(raw) else _locate_answer(raw, q, doc)
    if found is None:
        log.unverified += 1
        return q.model_copy(update={"review": _review(r, None)})
    if _overlaps(found, q.quotes):
        log.own_quote += 1
        return q.model_copy(update={"review": _review(r, found)})
    log.withdrawn += 1
    reviewed = q.model_copy(update={"review": _review(r, found)})
    reason = _one_line(r.answer).rstrip(".") or "Ответ есть в документе"
    return WithdrawnQuestion(question=reviewed, reason=reason, answer=found)


def _note(log: CriticLog, kept: int) -> list[Diagnostic]:
    details = log.details()
    if not log.withdrawn and not details:
        return []
    return [
        Diagnostic(
            code=LLM_QUESTIONS_WITHDRAWN,
            level=DiagLevel.INFO,
            message=(
                f"Самопроверка вопросов: снято вопросов: {log.withdrawn} "
                "(ответ есть в документе, цитату-ответ код сверил)"
            ),
            action="; ".join([f"В отчёте осталось вопросов: {kept}", *details]),
            step=STEP,
        )
    ]


def apply_reviews(
    questions: Sequence[Question], reviews: Sequence[CriticReview], doc: ParsedDocument
) -> CriticOutcome:
    """Решение кода по отзывам критика: снять подтверждённые, остальным приписать отзыв.

    Номера не меняются: у снятого вопроса свой ``Q-NN``, и в отчёте видно, какой номер снят и почему.
    """
    by_id: dict[str, CriticReview] = {}
    for r in reviews:
        qid = normalize_question_id(r.id)
        if qid is not None:
            by_id.setdefault(qid, r)
    log = CriticLog()
    kept: list[Question] = []
    withdrawn: list[WithdrawnQuestion] = []
    for q in questions:
        found = by_id.get(q.id)
        if found is None:
            log.missing += 1
            kept.append(q)
            continue
        judged = _judge(q, found, doc, log)
        if isinstance(judged, WithdrawnQuestion):
            withdrawn.append(judged)
        else:
            kept.append(judged)
    logger.info(
        "Критик вопросов: вопросов %d, снято %d, ответ без сверенной цитаты %d, без отзыва %d",
        len(questions),
        log.withdrawn,
        log.unverified,
        log.missing,
    )
    return CriticOutcome(
        questions=kept, withdrawn=withdrawn, diagnostics=_note(log, len(kept)), reviewed=True
    )


def _failed(questions: Sequence[Question], reason: str, hint: str) -> CriticOutcome:
    logger.warning("Критик вопросов не сработал (%s): вопросы без самопроверки", reason)
    return CriticOutcome(
        questions=list(questions),
        diagnostics=[
            Diagnostic(
                code=LLM_QUESTIONS_CRITIC_FAILED,
                level=DiagLevel.WARNING,
                message=f"Самопроверка вопросов не сработала: {reason}",
                action="Вопросы вошли в отчёт без самопроверки: на часть из них ответ может быть в документе",
                hint=hint or "Повторите прогон; если повторяется, сообщите команде",
                step=STEP,
            )
        ],
    )


def review(
    questions: Sequence[Question], doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> CriticOutcome:
    """Один пакетный вызов критика на все вопросы. Без вопросов модель не вызывается."""
    if not questions:
        return CriticOutcome()
    try:
        reviews = parse_reply(llm.call(build_request(questions, doc, cfg)))
        known = {normalize_question_id(r.id) for r in reviews} & {q.id for q in questions}
        if not known:
            return _failed(questions, "ни одного отзыва на вопросы прогона", "")
        return apply_reviews(questions, reviews, doc)
    except LLMError as exc:
        return _failed(questions, exc.message, exc.hint)
    except Exception as exc:  # граница критика: шаг необязательный, вопросы редактора терять нельзя
        logger.exception("Критик вопросов: внутренняя ошибка")
        return _failed(
            questions,
            f"внутренняя ошибка: {type(exc).__name__}",
            "Подробности в report.log; сообщите команде",
        )
