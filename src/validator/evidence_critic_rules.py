"""Критик находок: схема ответа модели и правила, по которым код принимает её решения (шаг 6б, #39).

Правила (подробнее в ``evidence_critic``): «снять» только по сверенной цитате-основанию с текстом вне цитат
находки и не для находки, которую нашли все проходы; «понизить» только со сверенной цитатой-основанием;
повысить важность критик не может; без основания находка остаётся как есть.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import Field, ValidationError, field_validator

from validator.errors import LLM_BAD_RESPONSE, LLMError
from validator.evidence_common import Lenient, locate_model_quote, new_chars, one_line, top_blocks
from validator.models import (
    CriticAction,
    CriticDecision,
    LLMResponse,
    MergedFinding,
    ParsedDocument,
    Quote,
    Severity,
)
from validator.questions.editor import extract_json

REPLY_KEY: Final = "decisions"
MIN_NEW_CHARS: Final = 12
"""Сколько символов цитаты-основания «снять» должно лежать вне цитат находки (как ``MIN_FUZZY_CHARS``)."""


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
    consensus: int = 0
    missing: int = 0

    def details(self) -> list[str]:
        labels = (
            (self.no_basis, "предложение без сверенной цитаты-основания, находка оставлена как есть"),
            (self.own_basis, "«снять» с основанием почти из цитаты самой находки: код не снял"),
            (self.consensus, "«снять» находку, которую нашли все проходы: код не снял"),
            (self.missing, "находок без решения критика"),
        )
        return [f"{text}: {count}" for count, text in labels if count]


def _basis(raw: str, own: Sequence[Quote], doc: ParsedDocument) -> Quote | None:
    return locate_model_quote(doc, raw, top_blocks(own))


OWN_BASIS_NOTE: Final = (
    "Основание почти целиком повторяет цитату самой находки и не показывает ответ в другом месте документа."
)


CONSENSUS_NOTE: Final = "Находку нашли все проходы поиска, одного мнения критика мало, чтобы её снять."


def _why_not_dropped(elsewhere: bool) -> str:
    return CONSENSUS_NOTE if elsewhere else OWN_BASIS_NOTE


def _lower(current: Severity, proposed: Severity | None) -> Severity | None:
    """Важность после понижения или None, если понижать некуда (предложена та же или выше)."""
    order = sorted(Severity, key=lambda s: s.rank)
    target = proposed or order[min(current.rank + 1, len(order) - 1)]
    return target if target.rank > current.rank else None


def unanimous(m: MergedFinding) -> bool:
    """Находку нашли все проходы поиска (K > 1): одного мнения критика мало, чтобы её снять."""
    return m.of > 1 and m.found_in >= m.of


def judge(
    r: ReplyDecision,
    current: Severity,
    own: Sequence[Quote],
    doc: ParsedDocument,
    log: CriticLog,
    *,
    consensus: bool = False,
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
    elsewhere = new_chars(basis, own) >= MIN_NEW_CHARS
    if r.decision is CriticAction.DROP and elsewhere and not consensus:
        log.dropped += 1
        return CriticDecision(action=CriticAction.DROP, reason=reason, basis=basis)
    own_basis = r.decision is CriticAction.DROP
    log.own_basis += own_basis and not elsewhere
    log.consensus += own_basis and elsewhere
    lowered = _lower(current, None if own_basis else r.severity)
    if lowered is None:
        log.kept += 1
        why = _why_not_dropped(elsewhere) + " " if own_basis else ""
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
    note = f"{_why_not_dropped(elsewhere)} Код не снял находку, а понизил её важность на уровень."
    return CriticDecision(
        action=CriticAction.DOWNGRADE,
        reason=reason,
        severity=lowered,
        basis=basis,
        proposed=CriticAction.DROP,
        note=note,
    )


__all__ = [
    "MIN_NEW_CHARS",
    "REPLY_KEY",
    "CriticLog",
    "CriticReplySchema",
    "ReplyDecision",
    "judge",
    "parse_reply",
    "unanimous",
]
