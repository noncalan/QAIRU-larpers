"""Готовые ответы модели в метриках отчёта: запись (replay) не называется кешем (#170).

Отчёт на сдачу писал «8 (с ошибкой 0, из кеша 0)», а таблица шагов под этой строкой показывала
«Из кеша» 1, 1, 1, 3, 1, 1: ответы из записи считались кешем в одном месте и не считались в другом.
"""

from __future__ import annotations

from validator.models import RunMetrics, StepMetrics
from validator.report.passport_view import calls_text, reuse_cell, reuse_header


def _steps(cached: tuple[int, ...], replayed: tuple[int, ...]) -> list[StepMetrics]:
    return [
        StepMetrics(step=f"s{i}", calls=max(c, r, 1), cached=c, replayed=r)
        for i, (c, r) in enumerate(zip(cached, replayed, strict=True))
    ]


def test_replay_run_says_from_recording_in_both_places() -> None:
    steps = _steps(cached=(0, 0), replayed=(1, 3))
    metrics = RunMetrics(calls_total=4, calls_replayed=4, steps=steps)

    assert calls_text(metrics) == "4 (с ошибкой 0, из записи 4)"
    assert reuse_header(steps) == "Из записи"
    assert [reuse_cell(st, steps) for st in steps] == ["1", "3"]


def test_live_run_keeps_cache_wording() -> None:
    steps = _steps(cached=(1, 0), replayed=(0, 0))
    metrics = RunMetrics(calls_total=5, calls_cached=1, steps=steps)

    assert calls_text(metrics) == "5 (с ошибкой 0, из кеша 1)"
    assert reuse_header(steps) == "Из кеша"
    assert [reuse_cell(st, steps) for st in steps] == ["1", "0"]


def test_live_run_without_cache_still_says_zero_from_cache() -> None:
    metrics = RunMetrics(calls_total=8)

    assert calls_text(metrics) == "8 (с ошибкой 0, из кеша 0)"


def test_cache_and_recording_together_are_shown_apart() -> None:
    steps = _steps(cached=(1, 0), replayed=(0, 2))
    metrics = RunMetrics(calls_total=3, calls_cached=1, calls_replayed=2, steps=steps)

    assert calls_text(metrics) == "3 (с ошибкой 0, из кеша 1, из записи 2)"
    assert reuse_header(steps) == "Из кеша / из записи"
    assert [reuse_cell(st, steps) for st in steps] == ["1 / 0", "0 / 2"]
