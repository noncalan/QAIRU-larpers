"""Паспорт прогона словами для человека (#125): общий для Markdown и PDF.

Паспорт читает лид QA, а не разработчик агента: шаги по-русски и только те, что вызывали модель, токены
на входе одной суммой (с кешем), версия агента с коммитом, название документа из его первого заголовка.
"""

from __future__ import annotations

from datetime import UTC, datetime

from validator.models import Report, TokenUsage
from validator.report.run_metrics import step_label

_VERSION_SOURCES = {"AGENT_VERSION": " (из переменной AGENT_VERSION)"}
"""Откуда взята версия, если не из git: git и пакет видны по самой строке версии."""


def tokens_in(t: TokenUsage) -> int:
    """Токены на входе вместе с кешем: без кеша «вход 16» выглядит как ошибка счёта."""
    return t.input_tokens + t.cache_creation_input_tokens + t.cache_read_input_tokens


def tokens_text(t: TokenUsage) -> str:
    cached = t.cache_creation_input_tokens + t.cache_read_input_tokens
    head = f"на входе {tokens_in(t)}"
    if cached:
        head += f" (из них через кеш {cached}: записано {t.cache_creation_input_tokens}, "
        head += f"прочитано {t.cache_read_input_tokens})"
    return f"{head}, на выходе {t.output_tokens}"


def document_line(report: Report) -> str:
    """«Название» (файл, тип, размер, страницы); без заголовка в тексте только файл."""
    p = report.passport
    meta = [p.document_kind.value]
    if p.document_size_bytes:
        meta.append(f"{p.document_size_bytes} байт")
    if p.document_pages:
        meta.append(f"{p.document_pages} стр.")
    file = f"{p.document_name} ({', '.join(meta)})"
    title = report.document.title if report.document is not None else None
    return f"«{title}», файл {file}" if title else file


def version_line(report: Report) -> str:
    p = report.passport
    line = p.agent_version
    if p.agent_commit and p.agent_commit not in p.agent_version:
        line += f", коммит {p.agent_commit}"
    line += _VERSION_SOURCES.get(p.agent_version_source, "")
    if p.agent_dirty:
        line += ", есть незакоммиченные правки"
    return line


def models_line(report: Report) -> str:
    """Модели только тех шагов, что вызывали модель в этом прогоне (по записям вызовов), по-русски."""
    called = [st.step for st in report.metrics.steps]
    models = report.passport.models
    parts = [f"{step_label(step)}: {models[step]}" for step in called if step in models]
    return "; ".join(parts) or "модель не вызывалась"


def backend_line(report: Report) -> str:
    """Способ вызова модели. При replay: сколько ответов взято из записи и сколько в ней всего (RC2).

    Пересборка из записи не должна читаться как живой прогон: «из записи 8 из 9 ответов» говорит, что
    модель в этом прогоне не вызывалась, а лишний ответ записи (например, сверка с ответами автора) не
    понадобился.
    """
    p = report.passport
    if p.calls_total == 0:
        return f"модель не вызывалась (запрошено: {p.backend_requested.label})"
    line = (p.backend_used or p.backend_requested).label
    if p.replay is not None and p.calls_replayed:
        line += f": из записи {p.calls_replayed} из {p.replay.responses} ответов, модель не вызывалась"
    if p.calls_cached:
        line += f"; из кеша {p.calls_cached} из {p.calls_total} ответов"
    return line


def replay_line(report: Report) -> str | None:
    """Чья запись: папка, когда её писали (по ``calls.jsonl``) и чем вызывали модель. ``None`` без replay."""
    r = report.passport.replay
    if r is None:
        return None
    line = r.path
    if r.recorded_from is not None and r.recorded_to is not None:
        line += f", записана {_span(r.recorded_from, r.recorded_to)}"
    if r.backends:
        line += f" ({', '.join(b.label for b in r.backends)})"
    return line


def _span(start: datetime, end: datetime) -> str:
    """«2026-11-26 17:16–17:31 UTC»: дата один раз, если запись уложилась в сутки."""
    a, b = _utc(start), _utc(end)
    tail = f"{b:%H:%M}" if a.date() == b.date() else f"{b:%Y-%m-%d %H:%M}"
    return f"{a:%Y-%m-%d %H:%M}–{tail} UTC"


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)


__all__ = [
    "backend_line",
    "document_line",
    "models_line",
    "replay_line",
    "tokens_in",
    "tokens_text",
    "version_line",
]
