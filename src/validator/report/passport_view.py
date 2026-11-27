"""Паспорт прогона словами для человека (#125): общий для Markdown и PDF.

Паспорт читает лид QA, а не разработчик агента: шаги по-русски и только те, что вызывали модель, токены
на входе одной суммой (с кешем), версия агента с коммитом, название документа из его первого заголовка.
"""

from __future__ import annotations

from datetime import UTC, datetime

from validator.models import ReplaySource, Report, RunMetrics, SearchInput, StepMetrics, TokenUsage
from validator.report.fmt import digits, money, seconds, status_label, when
from validator.report.run_metrics import step_label

_VERSION_SOURCES = {"AGENT_VERSION": " (из переменной AGENT_VERSION)"}
"""Откуда взята версия, если не из git: git и пакет видны по самой строке версии."""


def tokens_in(t: TokenUsage) -> int:
    """Токены на входе вместе с кешем: без кеша «вход 16» выглядит как ошибка счёта."""
    return t.input_tokens + t.cache_creation_input_tokens + t.cache_read_input_tokens


def tokens_text(t: TokenUsage) -> str:
    cached = t.cache_creation_input_tokens + t.cache_read_input_tokens
    head = f"на входе {digits(tokens_in(t))}"
    if cached:
        head += f" (из них через кеш {digits(cached)}: записано {digits(t.cache_creation_input_tokens)}, "
        head += f"прочитано {digits(t.cache_read_input_tokens)})"
    return f"{head}, на выходе {digits(t.output_tokens)}"


def calls_text(m: RunMetrics) -> str:
    """Строка «Вызовов модели» в метриках: ответ из записи (replay) не называем кешем (#170).

    «Из кеша» значит, что ответ уже лежал в кеше этого же бэкенда; «из записи» значит, что отчёт
    пересобран по ``run/`` прошлого живого прогона. Это разные вещи, и таблица шагов ниже называет их
    так же (:func:`reuse_header`).
    """
    parts = [f"с ошибкой {m.calls_failed}"]
    if m.calls_cached or not m.calls_replayed:
        parts.append(f"из кеша {m.calls_cached}")
    if m.calls_replayed:
        parts.append(f"из записи {m.calls_replayed}")
    return f"{m.calls_total} ({', '.join(parts)})"


def reuse_header(steps: list[StepMetrics]) -> str:
    """Заголовок колонки готовых ответов в таблице шагов: те же слова, что в :func:`calls_text`."""
    cached = any(st.cached for st in steps)
    replayed = any(st.replayed for st in steps)
    if replayed and cached:
        return "Из кеша / из записи"
    return "Из записи" if replayed else "Из кеша"


def reuse_cell(st: StepMetrics, steps: list[StepMetrics]) -> str:
    """Ячейка колонки :func:`reuse_header` для одного шага."""
    header = reuse_header(steps)
    if header == "Из записи":
        return str(st.replayed)
    if header == "Из кеша":
        return str(st.cached)
    return f"{st.cached} / {st.replayed}"


TIME_HEADER = "Время, с"
RECORDED_TIME_HEADER = "Время при записи, с"


def _recorded_time(steps: list[StepMetrics], replay: ReplaySource | None) -> bool:
    """Все вызовы таблицы взяты из записи и у записи есть время шагов: показываем его, а не пересборку."""
    if replay is None or not replay.step_seconds or not steps:
        return False
    return all(st.replayed == st.calls for st in steps)


def time_header(steps: list[StepMetrics], replay: ReplaySource | None) -> str:
    """Заголовок колонки времени в таблице шагов: при пересборке по записи это время модели при записи."""
    return RECORDED_TIME_HEADER if _recorded_time(steps, replay) else TIME_HEADER


def time_cell(st: StepMetrics, steps: list[StepMetrics], replay: ReplaySource | None) -> str:
    """Ячейка колонки :func:`time_header`: при пересборке нули времени не показываем, берём время записи."""
    if replay is not None and _recorded_time(steps, replay):
        return seconds(replay.step_seconds.get(st.step))
    return seconds(st.duration_s)


def document_line(report: Report) -> str:
    """«Название» (файл, тип, размер, страницы); без заголовка в тексте только файл."""
    p = report.passport
    meta = [p.document_kind.value]
    if p.document_size_bytes:
        meta.append(f"{digits(p.document_size_bytes)} байт")
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


def duration_line(report: Report) -> str:
    """Длительность прогона. При replay ещё и сколько работала модель при записи (#170).

    Пересборка по записи идёт доли секунды, и «Длительность 0,4 с» без пояснения читается как прогон агента
    за 0,4 с. Время модели при записи берётся из ``calls.jsonl`` записи (:class:`ReplaySource`).
    """
    p = report.passport
    line = f"{seconds(p.duration_s)} с"
    r = p.replay
    if r is not None and p.calls_replayed:
        line += " (пересборка по записи"
        if r.model_seconds is not None:
            line += f"; при записи модель работала {digits(round(r.model_seconds))} с"
        line += ")"
    return line


PassportRow = tuple[str, str]


def passport_rows(report: Report, checklists: str) -> tuple[list[PassportRow], list[PassportRow]]:
    """Строки паспорта для MD и PDF: сверху то, что требует ТЗ п. 6, ниже технические детали (#170).

    Сверху документ, дата, модель, версия агента, как вызывали модель, время, цена и итог прогона.
    Хеш документа и промптов, effort по шагам, токены кеша и окружение нужны для воспроизведения, а не
    для решения лида QA, поэтому стоят отдельной таблицей «Технические детали прогона».
    """
    p = report.passport
    main: list[PassportRow] = [
        ("Документ", document_line(report)),
        ("Дата прогона", when(p.started_at)),
        ("Модели", ", ".join(p.models_actual) or "модель не вызывалась"),
        ("Версия агента", version_line(report)),
        ("Способ вызова модели", backend_line(report)),
    ]
    if (replay := replay_line(report)) is not None:
        main.append(("Запись ответов модели", replay))
    main += [
        ("Длительность", duration_line(report)),
        ("Цена по тарифу API", money(p.cost_usd)),
        ("Код завершения", f"{p.exit_code.value}: {p.exit_code.meaning}"),
        ("Состояние проверки", status_label(report.status)),
    ]
    if report.selfcheck is not None:
        main.append(("Самопроверка отчёта", report.selfcheck.summary))
    raw = ", поиск по тексту без ID блоков" if p.search_input is SearchInput.RAW else ""
    tech: list[PassportRow] = [
        ("SHA-256 документа", p.document_sha256 or "—"),
        ("Хеш промптов", p.prompts_sha256[:12]),
        ("Модели по шагам", models_line(report)),
        ("Профиль", f"{p.profile.value}, проходов поиска: {p.passes}{raw}"),
        ("Доменные чек-листы", checklists),
        ("Вызовов модели", f"{p.calls_total} (с ошибкой: {p.calls_failed})"),
        ("Токены", tokens_text(p.tokens)),
        ("Окружение", f"{p.os}, Python {p.python}"),
    ]
    return main, tech


TECH_TITLE = "Технические детали прогона"


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
    "TECH_TITLE",
    "backend_line",
    "calls_text",
    "document_line",
    "duration_line",
    "models_line",
    "passport_rows",
    "replay_line",
    "reuse_cell",
    "reuse_header",
    "time_cell",
    "time_header",
    "tokens_in",
    "tokens_text",
    "version_line",
]
