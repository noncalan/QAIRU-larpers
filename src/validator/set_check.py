"""Шаг 4в: сверка требований между частями длинного документа (#38).

Длинный документ нарезается и проверяется по частям (``parts.py``): проход поиска видит одну часть и
не может сравнить требование из части 1 с требованием из части 4. Этот шаг закрывает разрыв одним
вызовом на прогон: модель получает сжатый список всех требований (``R-NN``, часть, раздел, текст или
краткая формулировка) и ищет противоречия и повторы между частями. Документ целиком (одна часть) этот
шаг пропускает: там проход поиска видит весь набор сам.

Решает код, а не модель:

- запись без двух известных требований из разных частей отбрасывается (пары внутри части уже проверены
  поиском, а выдуманный ID не превращается в находку);
- цитаты не от модели: код берёт дословный текст требований (``Requirement.quote``), поэтому каждая
  находка ссылается на конкретное место документа (CLAUDE.md, правило 4);
- противоречие идёт как ``SET-CONS``/``contradiction`` и мешает тесту, повтор как ``SET-MOD``/``redundancy``;
  важность считает код (``severity.py``), как у любой находки.

Каждая находка получает свой ``rule_id``: при слиянии (``merge.py``) находки сверки ведут себя как правила
линтера и между собой не склеиваются. Сбой вызова или ответ без JSON не роняет прогон: диагностика
``LLM-072``, находки частей остаются.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import Field, ValidationError, field_validator

from validator.config import FULL_VIEW_MAX_TOKENS, call_timeout, estimate_tokens
from validator.errors import LLM_BAD_RESPONSE, LLM_SET_CHECK_DROPPED, LLM_SET_CHECK_FAILED, LLMError
from validator.llm.base import LLMClient
from validator.models import (
    CandidateFinding,
    DefectType,
    DiagLevel,
    Diagnostic,
    FindingSource,
    LLMRequest,
    LLMResponse,
    LLMStep,
    Property,
    Requirement,
    RunConfig,
    Scope,
    Segmentation,
    SourceKind,
    Verdict,
)
from validator.parts import PartPlan
from validator.prompting import fill, load_prompt
from validator.questions.editor import _Lenient, extract_json

logger = logging.getLogger("validator")

SYSTEM_PROMPT = "set_check_system.md"
USER_PROMPT = "set_check_user.md"
CALL_INDEX = 1
"""Сверка вызывается один раз за прогон."""
STEP = "set_check"
REPLY_KEY = "issues"
FULL_QUOTE_CHARS = 300
"""Требование короче идёт в список дословно, длиннее краткой формулировкой с пометкой «(кратко)»."""
TIGHT_QUOTE_CHARS = 140
"""Если список целиком длиннее ``FULL_VIEW_MAX_TOKENS``, дословно идут только требования короче этого."""
SUMMARY_MARK = " (кратко)"
CID_PREFIX = "set"

_KINDS: tuple[tuple[re.Pattern[str], DefectType, Property], ...] = (
    (re.compile(r"(?i)contradict|противореч|конфликт"), DefectType.CONTRADICTION, Property.SET_CONSISTENT),
    (re.compile(r"(?i)duplic|redund|повтор|дубл"), DefectType.REDUNDANCY, Property.SET_MODIFIABLE),
)
_REQ_ID = re.compile(r"R\s*-?\s*0*(\d+)(?:\.(\d+))?", re.IGNORECASE)


# ---------------------------------------------------------------- схема ответа


class SetIssue(_Lenient):
    """Одна запись сверки. Разбор мягкий: запись не по схеме выпадает одна."""

    kind: str
    requirements: list[str] = Field(default_factory=list)
    title: str = ""
    explanation: str
    scenario: str = ""
    suggestion: str = ""

    @field_validator("requirements", mode="before")
    @classmethod
    def _as_list(cls, raw: object) -> object:
        """«R-03, R-41» строкой вместо списка."""
        return re.split(r"[,;\s]+(?=R)", raw.strip()) if isinstance(raw, str) else raw

    @field_validator("title", "scenario", "suggestion", mode="before")
    @classmethod
    def _null_is_empty(cls, raw: object) -> object:
        return "" if raw is None else raw


class SetCheckReplySchema(_Lenient):
    """Ответ сверки целиком: образец в ``set_check_system.md`` обязан ей соответствовать."""

    issues: list[SetIssue]


# ---------------------------------------------------------------- промпт


def _section(r: Requirement) -> str:
    path = [" ".join(s.split()) for s in r.section_path]
    return " > ".join(path[-2:]) if path else "без раздела"


def _text(r: Requirement, limit: int) -> str:
    quote = " ".join(r.quote.split())
    if len(quote) <= limit:
        return quote
    summary = " ".join(r.summary.split())
    if summary:
        return summary + SUMMARY_MARK
    return quote[:limit].rsplit(" ", 1)[0] + "…" + SUMMARY_MARK


def requirement_parts(requirements: Sequence[Requirement], plan: PartPlan) -> dict[str, int]:
    """Номер части каждого требования: по первому блоку его якоря."""
    out: dict[str, int] = {}
    for r in requirements:
        part = next((plan.part_of(b) for b in r.anchor.block_ids if plan.part_of(b) is not None), None)
        if part is not None:
            out[r.id] = part
    return out


def render_requirements(requirements: Sequence[Requirement], owner: dict[str, int]) -> str:
    """Сжатый список требований для модели. Слишком длинный список сжимается сильнее."""
    rendered = ""
    for limit in (FULL_QUOTE_CHARS, TIGHT_QUOTE_CHARS):
        rendered = "\n".join(
            f"{r.id} · часть {owner[r.id]} · «{_section(r)}»: {_text(r, limit)}"
            for r in requirements
            if r.id in owner
        )
        if estimate_tokens(len(rendered)) <= FULL_VIEW_MAX_TOKENS:
            break
    return rendered


def build_request(rendered: str, plan: PartPlan, cfg: RunConfig) -> LLMRequest:
    model = cfg.model_for(LLMStep.SET_CHECK)
    prompt = fill(load_prompt(USER_PROMPT), parts=str(len(plan.parts)), requirements=rendered)
    system = load_prompt(SYSTEM_PROMPT).strip()
    return LLMRequest(
        step=LLMStep.SET_CHECK,
        call_index=CALL_INDEX,
        model=model.model,
        effort=model.effort,
        system=system,
        prompt=prompt,
        json_schema=None,
        timeout_s=call_timeout(cfg, system, prompt),
    )


# ---------------------------------------------------------------- разбор и решение кода


def parse_reply(resp: LLMResponse) -> list[SetIssue]:
    """Записи из ответа сверки. Нет JSON со списком ``issues`` → ``LLMError``."""
    data = extract_json(resp.text, REPLY_KEY)
    if data is None:
        raise LLMError(
            "В ответе сверки требований нет JSON со списком issues",
            code=LLM_BAD_RESPONSE,
            hint="Повторите прогон; если повторяется, сообщите команде",
            location=resp.call_id,
            retryable=True,
        )
    issues: list[SetIssue] = []
    for raw in data[REPLY_KEY]:
        try:
            issues.append(SetIssue.model_validate(raw))
        except ValidationError:
            continue
    return issues


def _known_ids(raw: Sequence[str], requirements: dict[str, Requirement]) -> list[str]:
    """ID требований из ответа как в прогоне (``R-7`` → ``R-07``), без повторов и выдуманных."""
    by_number = {_key(rid): rid for rid in requirements}
    out: list[str] = []
    for text in raw:
        m = _REQ_ID.search(str(text))
        rid = by_number.get((int(m.group(1)), int(m.group(2)) if m.group(2) else None)) if m else None
        if rid is not None and rid not in out:
            out.append(rid)
    return out


def _key(rid: str) -> tuple[int, int | None]:
    m = _REQ_ID.fullmatch(rid)
    if m is None:  # ID требований задаёт код (R-NN), сюда попасть нельзя
        return (-1, None)
    return (int(m.group(1)), int(m.group(2)) if m.group(2) else None)


def _kind(raw: str) -> tuple[DefectType, Property] | None:
    return next(((d, p) for pattern, d, p in _KINDS if pattern.search(raw)), None)


@dataclass
class SetCheckLog:
    """Что код отбросил из ответа: из этого собирается диагностика ``LLM-073``."""

    unknown_kind: int = 0
    unknown_ids: int = 0
    same_part: int = 0

    def details(self) -> list[str]:
        labels = (
            (self.unknown_kind, "вид не противоречие и не повтор"),
            (self.unknown_ids, "меньше двух известных требований"),
            (self.same_part, "требования из одной части, их уже проверил поиск"),
        )
        return [f"{text}: {count}" for count, text in labels if count]


@dataclass
class SetCheckOutcome:
    """Итог шага: кандидаты в находки уровня набора, вызовы и диагностика."""

    candidates: list[CandidateFinding] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def _candidate(
    seq: int, issue: SetIssue, ids: list[str], kind: tuple[DefectType, Property], ctx: SetContext
) -> CandidateFinding:
    defect, prop = kind
    cid = f"{CID_PREFIX}.{seq:03d}"
    reqs = [ctx.requirements[rid] for rid in ids]
    blocks = list(dict.fromkeys(b for r in reqs for b in r.anchor.block_ids))
    contradiction = defect is DefectType.CONTRADICTION
    return CandidateFinding(
        cid=cid,
        source=FindingSource(kind=SourceKind.SET_CHECK, rule_id=cid, call_id=ctx.call_id),
        property=prop,
        defect_type=defect,
        verdict=Verdict.VIOLATION,
        scope=Scope.SET,
        block_ids=blocks,
        quotes=[r.quote for r in reqs],
        quote_blocks=[list(r.anchor.block_ids) for r in reqs],
        explanation=issue.explanation.strip(),
        suggestion=issue.suggestion.strip() or None,
        blocks_testing=contradiction,
        title=issue.title.strip() or None,
        scenario=(issue.scenario.strip() or None) if contradiction else None,
    )


@dataclass(frozen=True)
class SetContext:
    requirements: dict[str, Requirement]
    owner: dict[str, int]
    call_id: str


def to_candidates(
    issues: Sequence[SetIssue], ctx: SetContext, log: SetCheckLog | None = None
) -> list[CandidateFinding]:
    """Записи модели → кандидаты: только пары известных требований из разных частей."""
    log = log if log is not None else SetCheckLog()
    out: list[CandidateFinding] = []
    for issue in issues:
        kind = _kind(issue.kind)
        if kind is None:
            log.unknown_kind += 1
            continue
        ids = _known_ids(issue.requirements, ctx.requirements)
        if len(ids) < 2:
            log.unknown_ids += 1
            continue
        if len({ctx.owner.get(rid) for rid in ids}) < 2:
            log.same_part += 1
            continue
        out.append(_candidate(len(out) + 1, issue, ids, kind, ctx))
    return out


def _failed(reason: str, hint: str, call_ids: list[str]) -> SetCheckOutcome:
    return SetCheckOutcome(
        call_ids=call_ids,
        diagnostics=[
            Diagnostic(
                code=LLM_SET_CHECK_FAILED,
                level=DiagLevel.WARNING,
                message=f"Сверка требований между частями документа не удалась: {reason}",
                action="Противоречия и повторы между частями не проверены; находки внутри частей в отчёте",
                hint=hint or "Повторите прогон; если повторяется, сообщите команде",
                step=STEP,
            )
        ],
    )


def _dropped(log: SetCheckLog, kept: int) -> list[Diagnostic]:
    details = log.details()
    if not details:
        return []
    return [
        Diagnostic(
            code=LLM_SET_CHECK_DROPPED,
            level=DiagLevel.INFO,
            message=f"Сверка требований между частями: код отбросил записи модели ({'; '.join(details)})",
            action=f"В отчёт пошли записи о парах из разных частей: {kept}",
            step=STEP,
        )
    ]


def set_check(seg: Segmentation, plan: PartPlan, llm: LLMClient, cfg: RunConfig) -> SetCheckOutcome:
    """Один вызов на длинный документ; документ целиком или меньше двух частей с требованиями: без вызова."""
    if not plan.split:
        return SetCheckOutcome()
    owner = requirement_parts(seg.requirements, plan)
    if len(set(owner.values())) < 2:
        return SetCheckOutcome()
    request = build_request(render_requirements(seg.requirements, owner), plan, cfg)
    try:
        resp = llm.call(request)
        issues = parse_reply(resp)
    except LLMError as exc:
        return _failed(exc.message, exc.hint, [request.call_id])
    except Exception as exc:  # граница шага: сверка дополнительная, находки частей терять нельзя
        logger.exception("Сверка требований между частями: внутренняя ошибка")
        return _failed(
            f"внутренняя ошибка: {type(exc).__name__}",
            "Подробности в report.log; сообщите команде",
            [request.call_id],
        )
    ctx = SetContext({r.id: r for r in seg.requirements}, owner, resp.call_id)
    log = SetCheckLog()
    candidates = to_candidates(issues, ctx, log)
    logger.info("Сверка между частями: записей %d, кандидатов %d", len(issues), len(candidates))
    return SetCheckOutcome(candidates, [resp.call_id], _dropped(log, len(candidates)))


__all__ = [
    "SetCheckOutcome",
    "SetCheckReplySchema",
    "SetContext",
    "SetIssue",
    "build_request",
    "parse_reply",
    "render_requirements",
    "requirement_parts",
    "set_check",
    "to_candidates",
]
