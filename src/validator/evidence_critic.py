"""Шаг 6б: критик находок (docs/ideas/final-idea.md, шаг 6; исследование 02, п. 8: внешняя проверка, а не
самокритика).

Модель (D-010: Opus 5.5 ``high``) получает документ с ID блоков и пачку рискованных находок и по каждой
решает: «оставить», «понизить» или «снять», с причиной и дословной цитатой-основанием. Какие находки
рискованные, решает код (``select``):

- «критично» по правилам кода (таблица ``severity`` и её ограничения, до решения критика);
- найдены только в одном проходе из нескольких («1 из K», K > 1);
- подсказка важности модели и таблица кода расходятся больше чем на уровень (``severity_disputed``);
- противоречие без проверенного примера-свидетеля (шаг 6а).

Находки только линтера критик не смотрит: это правила кода, а не мнение модели. Решает код:

- «снять» принимается, только если цитата-основание сверена с документом и в ней есть текст вне цитат самой
  находки (``MIN_NEW_CHARS``): документ отвечает на находку в другом месте или в той части фразы, которую
  находка не учла. Основание почти целиком из цитаты находки: код не снимает, а понижает важность на уровень.
- «понизить» принимается со сверенной цитатой-основанием; понижает важность верификатор (``severity``),
  повысить критик не может.
- Нет сверенной цитаты-основания: находка остаётся как есть, предложение критика видно в отчёте.

Снятая находка уходит в «отброшено» с причиной критика (``DiscardReason.CRITIC_REJECTED``, шаг 7).
Отказ модели или ответ без JSON: находки идут без решения критика, ``LLM-042``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

from pydantic import Field, ValidationError, field_validator

from validator.errors import LLM_BAD_RESPONSE, LLM_CRITIC_DECIDED, LLM_CRITIC_FAILED, LLMError
from validator.evidence_common import (
    Lenient,
    Located,
    from_model,
    locate_model_quote,
    new_chars,
    normalize_mid,
    one_line,
    render_finding,
    top_blocks,
)
from validator.ingest.blocks import render_for_model
from validator.llm.base import LLMClient
from validator.models import (
    CriticAction,
    CriticDecision,
    DefectType,
    DiagLevel,
    Diagnostic,
    LLMRequest,
    LLMResponse,
    LLMStep,
    MergedFinding,
    ParsedDocument,
    Quote,
    RunConfig,
    Severity,
    Witness,
)
from validator.prompting import PROMPTS_DIR, fill, load_prompt
from validator.questions.editor import extract_json
from validator.questions.quotes import verify_quote
from validator.severity import severity_disputed, severity_for, table_severity

logger = logging.getLogger("validator")

SYSTEM_PROMPT = PROMPTS_DIR / "critic_system.md"
USER_PROMPT = PROMPTS_DIR / "critic_user.md"
CALL_INDEX: Final = 1
"""Критик вызывается один раз за прогон, пакетно на все выбранные находки."""
STEP: Final = "evidence"
REPLY_KEY: Final = "decisions"
MAX_FINDINGS: Final = 60
"""Сколько находок в одном вызове. Три прохода дают до ~40 рискованных; сверх лимита идут самые важные."""
MIN_BASIS_CHARS: Final = 3
"""Сколько букв и цифр должно быть в цитате-основании. Короткое основание («0» или «1», значение ячейки)
код принимает только дословным: нечёткий поиск коротких цитат ничего не доказывает."""
MIN_NEW_CHARS: Final = 12
"""Сколько символов цитаты-основания «снять» должно лежать вне цитат находки (как ``MIN_FUZZY_CHARS``)."""


# ---------------------------------------------------------------- выбор находок


@dataclass(frozen=True)
class Pick:
    """Находка для критика и почему код её выбрал (для промпта и метрик)."""

    finding: MergedFinding
    severity: Severity
    reasons: tuple[str, ...]


WHY_CRITICAL = "критично по правилам кода"
WHY_SINGLE = "найдена только в одном проходе поиска из нескольких"
WHY_DISPUTED = "оценка важности модели и таблица кода расходятся"
WHY_NO_WITNESS = "противоречие без проверенного примера-свидетеля"


def _reasons(m: MergedFinding, severity: Severity) -> tuple[str, ...]:
    out: list[str] = []
    if severity is Severity.CRITICAL:
        out.append(WHY_CRITICAL)
    if m.of > 1 and m.found_in == 1:
        out.append(WHY_SINGLE)
    if severity_disputed(m.severity_hint, table_severity(m.defect_type, m.blocks_testing)):
        out.append(WHY_DISPUTED)
    if m.defect_type is DefectType.CONTRADICTION and not (m.witness and m.witness.checked):
        out.append(WHY_NO_WITNESS)
    return tuple(out)


def select(found: Sequence[MergedFinding], located: Located) -> list[Pick]:
    """Рискованные находки модели со сверенными цитатами: самые важные первыми, не больше ``MAX_FINDINGS``."""
    picks: list[Pick] = []
    for m in found:
        quotes = located.get(m.mid, ())
        if not quotes or not from_model(m):
            continue
        severity = severity_for(m, quotes).severity
        reasons = _reasons(m, severity)
        if reasons:
            picks.append(Pick(m, severity, reasons))
    picks.sort(key=lambda p: (p.severity.rank, -len(p.reasons)))  # устойчиво: порядок слияния внутри
    return picks[:MAX_FINDINGS]


# ---------------------------------------------------------------- промпт


def _witness_line(w: Witness | None) -> list[str]:
    if w is None:
        return []
    if w.checked:
        lines = [f"Пример-свидетель (код сверил цитаты и различие ожиданий): {one_line(w.scenario)}"]
        lines += [f"  - по «{one_line(r.quote)}»: {one_line(r.expectation)}" for r in w.readings]
        return lines
    return [f"Пример-свидетель: {one_line(w.note)}"]


def render_pick(p: Pick, located: Located) -> str:
    m = p.finding
    extra = [f"Важность по правилам кода: {p.severity.label}", *_witness_line(m.witness)]
    if m.suggestion:
        extra.append(f"Предложенная формулировка: {one_line(m.suggestion)}")
    return render_finding(m, located.get(m.mid, ()), extra)


def build_request(batch: Sequence[Pick], located: Located, doc: ParsedDocument, cfg: RunConfig) -> LLMRequest:
    model = cfg.model_for(LLMStep.CRITIC)
    prompt = fill(
        load_prompt(USER_PROMPT.name),
        document=render_for_model(doc).rstrip("\n"),
        findings="\n\n".join(render_pick(p, located) for p in batch),
    )
    return LLMRequest(
        step=LLMStep.CRITIC,
        call_index=CALL_INDEX,
        model=model.model,
        effort=model.effort,
        system=load_prompt(SYSTEM_PROMPT.name).strip(),
        prompt=prompt,
        json_schema=None,
        timeout_s=cfg.call_timeout_s,
    )


# ---------------------------------------------------------------- схема и разбор ответа


class ReplyDecision(Lenient):
    """Решение по одной находке. Разбор мягкий: запись не по схеме выпадает одна."""

    id: str
    decision: CriticAction
    severity: Severity | None = None
    reason: str = ""
    basis_quote: str = ""

    @field_validator("decision", mode="before")
    @classmethod
    def _action(cls, raw: object) -> object:
        return CriticAction.from_label(raw) if isinstance(raw, str) else raw

    @field_validator("severity", mode="before")
    @classmethod
    def _severity(cls, raw: object) -> object:
        """«важно», «незначительно» или значение перечисления; иное (null, «—») → None."""
        if not isinstance(raw, str):
            return None
        return Severity.from_label(raw) or next((s for s in Severity if s.value == raw.strip()), None)

    @field_validator("reason", "basis_quote", mode="before")
    @classmethod
    def _null_is_empty(cls, raw: object) -> object:
        return "" if raw is None else raw


class CriticReplySchema(Lenient):
    """Схема ответа целиком: образец в ``critic_system.md`` обязан ей соответствовать."""

    decisions: list[ReplyDecision] = Field(default_factory=list)


def parse_reply(resp: LLMResponse) -> list[ReplyDecision]:
    """Решения из ответа критика. Нет JSON со списком ``decisions`` → ``LLMError``."""
    data = extract_json(resp.text, REPLY_KEY)
    if data is None:
        raise LLMError(
            "В ответе критика находок нет JSON со списком decisions",
            code=LLM_BAD_RESPONSE,
            hint="Повторите прогон; если повторяется, сообщите команде",
            location=resp.call_id,
        )
    out: list[ReplyDecision] = []
    for raw in data[REPLY_KEY]:
        try:
            out.append(ReplyDecision.model_validate(raw))
        except ValidationError:
            continue
    return out


# ---------------------------------------------------------------- решение кода


@dataclass
class CriticLog:
    kept: int = 0
    downgraded: int = 0
    dropped: int = 0
    no_basis: int = 0
    own_basis: int = 0
    missing: int = 0

    def details(self) -> list[str]:
        labels = (
            (self.no_basis, "предложение без сверенной цитаты-основания, находка оставлена как есть"),
            (self.own_basis, "«снять» с основанием почти из цитаты самой находки: код не снял"),
            (self.missing, "находок без решения критика"),
        )
        return [f"{text}: {count}" for count, text in labels if count]


def _basis(raw: str, own: Sequence[Quote], doc: ParsedDocument) -> Quote | None:
    text = raw.strip()
    if sum(ch.isalnum() for ch in text) < MIN_BASIS_CHARS:
        return None
    within = top_blocks(own)
    return locate_model_quote(doc, text, within) or verify_quote(doc, text, within)


OWN_BASIS_NOTE: Final = (
    "Основание почти целиком повторяет цитату самой находки и не показывает ответ в другом месте документа."
)


def _lower(current: Severity, proposed: Severity | None) -> Severity | None:
    """Важность после понижения или None, если понижать некуда (предложена та же или выше)."""
    order = sorted(Severity, key=lambda s: s.rank)
    target = proposed or order[min(current.rank + 1, len(order) - 1)]
    return target if target.rank > current.rank else None


def judge(
    r: ReplyDecision, current: Severity, own: Sequence[Quote], doc: ParsedDocument, log: CriticLog
) -> CriticDecision:
    """Решение критика, которое принимает код (см. описание модуля). ``current``: важность до критика."""
    reason = one_line(r.reason).rstrip(".") or "критик не указал причину"
    basis = _basis(r.basis_quote, own, doc)
    if r.decision is CriticAction.KEEP:
        log.kept += 1
        return CriticDecision(action=CriticAction.KEEP, reason=reason, basis=basis)
    if basis is None:
        log.no_basis += 1
        log.kept += 1
        note = "Цитата-основание не найдена в документе, поэтому код оставил находку как есть."
        return CriticDecision(action=CriticAction.KEEP, reason=reason, proposed=r.decision, note=note)
    if r.decision is CriticAction.DROP and new_chars(basis, own) >= MIN_NEW_CHARS:
        log.dropped += 1
        return CriticDecision(action=CriticAction.DROP, reason=reason, basis=basis)
    own_basis = r.decision is CriticAction.DROP
    log.own_basis += own_basis
    lowered = _lower(current, None if own_basis else r.severity)
    if lowered is None:
        log.kept += 1
        why = OWN_BASIS_NOTE + " " if own_basis else ""
        if r.severity is not None and not own_basis and current is not Severity.MINOR:
            limit = f"Предложенная важность «{r.severity.label}» не ниже текущей «{current.label}»"
        else:
            limit = f"Ниже «{current.label}» понижать некуда"
        note = f"{why}{limit}, поэтому код оставил находку как есть."
        return CriticDecision(
            action=CriticAction.KEEP, reason=reason, basis=basis, proposed=r.decision, note=note
        )
    log.downgraded += 1
    if not own_basis:
        return CriticDecision(action=CriticAction.DOWNGRADE, reason=reason, severity=lowered, basis=basis)
    note = f"{OWN_BASIS_NOTE} Код не снял находку, а понизил её важность на уровень."
    return CriticDecision(
        action=CriticAction.DOWNGRADE,
        reason=reason,
        severity=lowered,
        basis=basis,
        proposed=CriticAction.DROP,
        note=note,
    )


@dataclass(frozen=True)
class CriticOutcome:
    findings: list[MergedFinding] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def _note(log: CriticLog, reviewed: int) -> list[Diagnostic]:
    return [
        Diagnostic(
            code=LLM_CRITIC_DECIDED,
            level=DiagLevel.INFO,
            message=(
                f"Критик находок: проверено {reviewed}, оставлено {log.kept}, понижено {log.downgraded}, "
                f"снято {log.dropped} (снимает и понижает код, только по сверенной цитате-основанию)"
            ),
            action="; ".join(["Снятые находки перечислены в сводке отчёта с причиной", *log.details()]),
            step=STEP,
        )
    ]


def apply(
    found: Sequence[MergedFinding],
    replies: Sequence[ReplyDecision],
    located: Located,
    doc: ParsedDocument,
    batch: Sequence[Pick],
) -> CriticOutcome:
    """Решения критика в находках пачки; остальные находки как были."""
    by_id: dict[str, ReplyDecision] = {}
    for r in replies:
        mid = normalize_mid(r.id)
        if mid is not None:
            by_id.setdefault(mid, r)
    asked = {p.finding.mid: p for p in batch}
    log = CriticLog()
    out: list[MergedFinding] = []
    for m in found:
        reply = by_id.get(m.mid) if m.mid in asked else None
        if m.mid in asked and reply is None:
            log.missing += 1
        if reply is None:
            out.append(m)
            continue
        decision = judge(reply, asked[m.mid].severity, located.get(m.mid, ()), doc, log)
        out.append(m.model_copy(update={"critic": decision}))
    reviewed = len(asked) - log.missing
    logger.info(
        "Критик находок: в пачке %d, оставлено %d, понижено %d, снято %d, без основания %d, без решения %d",
        len(batch),
        log.kept,
        log.downgraded,
        log.dropped,
        log.no_basis,
        log.missing,
    )
    return CriticOutcome(findings=out, diagnostics=_note(log, reviewed))


def failed(found: Sequence[MergedFinding], reason: str, hint: str) -> CriticOutcome:
    logger.warning("Критик находок не сработал (%s): находки без его решения", reason)
    return CriticOutcome(
        findings=list(found),
        diagnostics=[
            Diagnostic(
                code=LLM_CRITIC_FAILED,
                level=DiagLevel.WARNING,
                message=f"Критик находок не сработал: {reason}",
                action="Находки вошли в отчёт без перепроверки критиком: среди рискованных могут быть ложные",
                hint=hint or "Повторите прогон; если повторяется, сообщите команде",
                step=STEP,
            )
        ],
    )


def critic(
    found: Sequence[MergedFinding], located: Located, doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> CriticOutcome:
    """Один пакетный вызов на рискованные находки. Таких нет: модель не вызывается."""
    batch = select(found, located)
    if not batch:
        return CriticOutcome(findings=list(found))
    try:
        replies = parse_reply(llm.call(build_request(batch, located, doc, cfg)))
    except LLMError as exc:
        return failed(found, exc.message, exc.hint)
    known = {normalize_mid(r.id) for r in replies} & {p.finding.mid for p in batch}
    if not known:
        return failed(found, "в ответе нет ни одного решения по находкам прогона", "")
    return apply(found, replies, located, doc, batch)


__all__ = [
    "MAX_FINDINGS",
    "CriticOutcome",
    "CriticReplySchema",
    "Pick",
    "apply",
    "build_request",
    "critic",
    "judge",
    "parse_reply",
    "select",
]
