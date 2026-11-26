"""Шаг 6 в цифрах: свидетели и решения критика по находкам после шага (для метрик прогона в отчёте).

Отдельный модуль без зависимостей от шагов: его читает сборка отчёта (``report/build.py``), а шаг 6 сам
зависит от пакета вопросов, который зависит от отчёта.
"""

from __future__ import annotations

from collections.abc import Sequence

from validator.models import CriticAction, EvidenceMetrics, MergedFinding


def evidence_metrics(
    found: Sequence[MergedFinding], *, critic_enabled: bool = True
) -> EvidenceMetrics | None:
    """Шаг 6 в цифрах по находкам после шага; None, если ни свидетеля, ни решения критика нет.

    Критик выключен (``--critic`` не задан): метрики есть всегда, когда были находки, чтобы отчёт сказал
    об этом.
    """
    witnesses = [m.witness for m in found if m.witness is not None]
    decisions = [m.critic for m in found if m.critic is not None]
    if not found or (not witnesses and not decisions and critic_enabled):
        return None
    checked = sum(w.checked for w in witnesses)
    declined = sum(not w.checked and not w.scenario.strip() for w in witnesses)
    return EvidenceMetrics(
        witness_requested=len(witnesses),
        witness_checked=checked,
        witness_rejected=len(witnesses) - checked - declined,
        witness_declined=declined,
        critic_reviewed=len(decisions),
        critic_kept=sum(d.action is CriticAction.KEEP for d in decisions),
        critic_downgraded=sum(d.action is CriticAction.DOWNGRADE for d in decisions),
        critic_dropped=sum(d.action is CriticAction.DROP for d in decisions),
        critic_overruled=sum(d.proposed is not None for d in decisions),
        critic_enabled=critic_enabled,
    )


__all__ = ["evidence_metrics"]
