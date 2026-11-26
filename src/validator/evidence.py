"""Шаг 6: доказательства к находкам — пример-свидетель и критик (#39); попытку теста добавляет #40.

Контракт плана: ``evidence(found, doc, llm, cfg) -> list[MergedFinding]``. Конвейер зовёт ``gather``: то же
плюс диагностика шага. Порядок вызовов (каждый один на прогон, пакетом):

1. ``evidence_witness``: пример-свидетель для противоречий и находок со сценарием поиска
   (``LLMStep.EVIDENCE``);
2. ``evidence_critic``: критик рискованных находок (``LLMStep.CRITIC``). Он идёт после свидетеля и видит его
   итог: противоречие без проверенного свидетеля — повод перепроверить находку.

Модель предлагает, код проверяет: цитаты свидетеля и цитату-основание критика код сверяет с документом,
снимает или понижает находку только код (правила в модулях шагов). Политика отказа: упал свидетель — критик
всё равно работает; упал критик — находки идут без его решения; в обоих случаях диагностика ``LLM-040`` или
``LLM-042`` и отчёт без потерь. Внутренняя ошибка кода одного вызова не роняет второй.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from validator.errors import INT_STEP_DEGRADED
from validator.evidence_common import Located, locate_findings
from validator.evidence_critic import critic
from validator.evidence_witness import witness
from validator.llm.base import LLMClient
from validator.models import (
    DiagLevel,
    Diagnostic,
    MergedFinding,
    ParsedDocument,
    RunConfig,
)

logger = logging.getLogger("validator")

STEP = "evidence"


@dataclass(frozen=True)
class EvidenceOutcome:
    """Находки после шага 6 и его диагностика."""

    findings: list[MergedFinding] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def _internal(part: str, exc: Exception) -> Diagnostic:
    return Diagnostic(
        code=INT_STEP_DEGRADED,
        level=DiagLevel.WARNING,
        message=f"Внутренняя ошибка шага доказательств ({part}): {type(exc).__name__}",
        action=f"Находки вошли в отчёт без результата: {part}",
        hint="Подробности в report.log; сообщите команде",
        step=STEP,
    )


def _with_witness(
    found: list[MergedFinding], located: Located, doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> tuple[list[MergedFinding], list[Diagnostic]]:
    try:
        out = witness(found, located, doc, llm, cfg)
    except Exception as exc:  # граница вызова: свидетель необязателен, находки терять нельзя
        logger.exception("Шаг доказательств: внутренняя ошибка свидетеля")
        return found, [_internal("пример-свидетель", exc)]
    return out.findings, out.diagnostics


def _with_critic(
    found: list[MergedFinding], located: Located, doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> tuple[list[MergedFinding], list[Diagnostic]]:
    try:
        out = critic(found, located, doc, llm, cfg)
    except Exception as exc:  # граница вызова: критик необязателен, находки терять нельзя
        logger.exception("Шаг доказательств: внутренняя ошибка критика")
        return found, [_internal("решение критика", exc)]
    return out.findings, out.diagnostics


def gather(
    found: Sequence[MergedFinding], doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> EvidenceOutcome:
    """Свидетель, затем критик. Не бросает исключений: сбой вызова становится диагностикой."""
    findings = list(found)
    if not findings:
        return EvidenceOutcome()
    located = locate_findings(findings, doc)
    findings, witness_diags = _with_witness(findings, located, doc, llm, cfg)
    findings, critic_diags = _with_critic(findings, located, doc, llm, cfg)
    return EvidenceOutcome(findings=findings, diagnostics=[*witness_diags, *critic_diags])


def evidence(
    found: Sequence[MergedFinding], doc: ParsedDocument, llm: LLMClient, cfg: RunConfig
) -> list[MergedFinding]:
    """Контракт плана: находки со свидетелем и решением критика (диагностику отдаёт ``gather``)."""
    return gather(found, doc, llm, cfg).findings


__all__ = ["EvidenceOutcome", "evidence", "gather"]
