"""Чтение ``report.json`` для сравнения версий: только нужные поля, незнакомые терпим.

Схема отчёта ещё меняется между релизами, поэтому сравнение не валидирует ``Report`` целиком, а
берёт из словаря то, что ему нужно. Нет обязательного поля или файл не JSON: ``ReportReadError``
с понятным сообщением по-русски, без трассировки.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from validator.errors import InputError
from validator.ingest import parse
from validator.models import DocKind

MAX_REPORT_BYTES = 64 * 1024 * 1024
"""Больше этого ``report.json`` не бывает: файл крупнее читать не будем."""

_DOC_KINDS = {".md": DocKind.MD, ".pdf": DocKind.PDF, ".docx": DocKind.DOCX}


class ReportReadError(Exception):
    """Отчёт или документ не прочитать: что случилось и что сделать человеку."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class ReqView:
    """Требование отчёта: то, что нужно для сопоставления версий."""

    id: str
    quote: str
    stable_key: str
    source_id: str | None
    source_locator: str
    section_path: tuple[str, ...]


@dataclass(frozen=True)
class QuoteView:
    text: str
    """Дословный фрагмент документа, если код его нашёл, иначе цитата модели."""
    where: str
    """Место в документе словами: строка, страница, таблица."""


@dataclass(frozen=True)
class FindingView:
    """Находка отчёта: свойство, важность, цитаты и требования."""

    id: str
    property: str
    defect_type: str
    severity: str
    verdict: str
    scope: str
    title: str
    quotes: tuple[QuoteView, ...]
    requirement_ids: tuple[str, ...]
    section_path: tuple[str, ...]


@dataclass(frozen=True)
class PassportView:
    """Паспорт прогона: что нужно, чтобы понять, чем и когда сделан отчёт."""

    document_name: str
    document_sha256: str
    agent_version: str
    prompts_sha256: str
    started_at: str
    profile: str
    passes: int | None
    models: dict[str, str] = field(default_factory=dict)
    cost_usd: float | None = None
    models_actual: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportView:
    """Отчёт одной версии документа в виде, удобном для сравнения."""

    path: str
    passport: PassportView
    requirements: tuple[ReqView, ...]
    findings: tuple[FindingView, ...]
    traffic_light: str
    headline: str
    questions: int
    status: str


def _need(
    data: Any, key: str, where: str, path: Path, expected: type | tuple[type, ...] | None = None
) -> Any:
    hint = "Сравнение читает report.json агента validator. Проверьте, что это он, а не другой JSON."
    if not isinstance(data, dict) or key not in data:
        raise ReportReadError(f"В отчёте {path} нет поля {where}{key}", hint)
    value = data[key]
    if expected is not None:
        types = expected if isinstance(expected, tuple) else (expected,)
        if type(value) not in types:
            raise ReportReadError(f"В отчёте {path} неверный тип поля {where}{key}", hint)
    return value


def _strings(data: Any, key: str, where: str, path: Path) -> tuple[str, ...]:
    items = _need(data, key, where, path, list)
    if any(not isinstance(item, str) for item in items):
        raise ReportReadError(f"В отчёте {path} поле {where}{key} должно быть массивом строк")
    return tuple(items)


def _str(data: Any, key: str, default: str = "") -> str:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, str) else default


def _where(anchor: Any) -> str:
    if not isinstance(anchor, dict):
        return ""
    parts: list[str] = []
    if anchor.get("page") is not None:
        parts.append(f"стр. {anchor['page']}")
    if anchor.get("table") is not None and anchor.get("row") is not None:
        parts.append(f"таблица {anchor['table']}, строка {anchor['row']}")
    elif anchor.get("paragraph") is not None:
        parts.append(f"абзац {anchor['paragraph']}")
    elif anchor.get("page") is None and anchor.get("line_start") is not None:
        parts.append(f"строка {anchor['line_start']}")
    return ", ".join(parts)


def _quote(raw: Any, where: str, path: Path) -> QuoteView | None:
    if isinstance(raw, str):
        return QuoteView(raw, "") if raw else None
    text = _need(raw, "text", where, path, str)
    located = _need(raw, "located", where, path, (str, type(None))) if "located" in raw else None
    text = located or text
    return QuoteView(text, _where(raw.get("anchor"))) if text else None


def _requirement(raw: Any, path: Path) -> ReqView:
    rid = _need(raw, "id", "segmentation.requirements[].", path, str)
    where = f"segmentation.requirements[{rid}]."
    return ReqView(
        id=rid,
        quote=_need(raw, "quote", where, path, str),
        stable_key=_need(raw, "stable_key", where, path, str),
        source_id=_need(raw, "source_id", where, path, (str, type(None))) if "source_id" in raw else None,
        source_locator=_str(raw, "source_locator"),
        section_path=_strings(raw, "section_path", where, path),
    )


def _finding(raw: Any, path: Path) -> FindingView:
    fid = _need(raw, "id", "verification.findings[].", path, str)
    where = f"verification.findings[{fid}]."
    quotes = _need(raw, "quotes", where, path, list)
    return FindingView(
        id=fid,
        property=_need(raw, "property", where, path, str),
        defect_type=_need(raw, "defect_type", where, path, str),
        severity=_need(raw, "severity", where, path, str),
        verdict=_need(raw, "verdict", where, path, str),
        scope=_need(raw, "scope", where, path, str),
        title=_str(raw, "title") or _str(raw, "explanation"),
        quotes=tuple(q for q in (_quote(x, f"{where}quotes[{i}].", path) for i, x in enumerate(quotes)) if q),
        requirement_ids=_strings(raw, "requirement_ids", where, path),
        section_path=_strings(raw, "section_path", where, path),
    )


def _passport(raw: Any, path: Path) -> PassportView:
    where = "passport."
    models = _need(raw, "models", where, path, dict)
    if any(not isinstance(value, str) for value in models.values()):
        raise ReportReadError(f"В отчёте {path} поле passport.models должно содержать строки")
    started_at = _need(raw, "started_at", where, path, str)
    try:
        datetime.fromisoformat(started_at)
    except ValueError as exc:
        raise ReportReadError(f"В отчёте {path} неверная дата в поле passport.started_at") from exc
    cost = _need(raw, "cost_usd", where, path, (int, float, type(None))) if "cost_usd" in raw else None
    try:
        cost_usd = float(cost) if cost is not None else None
    except OverflowError as exc:
        raise ReportReadError(f"В отчёте {path} неверное число в поле passport.cost_usd") from exc
    if cost_usd is not None and not math.isfinite(cost_usd):
        raise ReportReadError(f"В отчёте {path} неверное число в поле passport.cost_usd")
    return PassportView(
        document_name=_need(raw, "document_name", where, path, str),
        document_sha256=_need(raw, "document_sha256", where, path, (str, type(None))) or "",
        agent_version=_need(raw, "agent_version", where, path, str),
        prompts_sha256=_need(raw, "prompts_sha256", where, path, str),
        started_at=started_at,
        profile=_need(raw, "profile", where, path, str),
        passes=_need(raw, "passes", where, path, int),
        models=models,
        cost_usd=cost_usd,
        models_actual=_strings(raw, "models_actual", where, path) if "models_actual" in raw else (),
    )


def _read_json(path: Path) -> Any:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReportReadError(f"Не удалось открыть отчёт {path}", "Проверьте путь к report.json.") from exc
    if size == 0:
        raise ReportReadError(f"Отчёт {path} пустой", "Укажите report.json, который записал validator.")
    if size > MAX_REPORT_BYTES:
        raise ReportReadError(f"Отчёт {path} слишком большой ({size} байт)", "Это точно report.json?")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ReportReadError(f"Отчёт {path} не в UTF-8", "Укажите report.json агента.") from exc
    except json.JSONDecodeError as exc:
        raise ReportReadError(
            f"Отчёт {path} не читается как JSON: строка {exc.lineno}, позиция {exc.colno}",
            "Файл повреждён или обрезан: перезапишите его прогоном validator.",
        ) from exc
    except (ValueError, RecursionError) as exc:
        raise ReportReadError(f"Отчёт {path} содержит недопустимое число или вложенность JSON") from exc
    except OSError as exc:
        raise ReportReadError(f"Не удалось прочитать отчёт {path}", "Проверьте права на файл.") from exc


def load_report(path: Path) -> ReportView:
    """``report.json`` → ``ReportView``. Ошибки чтения и схемы: ``ReportReadError``."""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ReportReadError(f"Отчёт {path} не объект JSON", "Укажите report.json агента validator.")
    passport = _need(data, "passport", "", path, dict)
    segmentation = data.get("segmentation")
    verification = data.get("verification")
    if not isinstance(segmentation, dict) or not isinstance(verification, dict):
        raise ReportReadError(
            f"В отчёте {path} нет нарезки или проверки: прогон не дошёл до проверки требований",
            "Сравнивать можно только отчёты, где проверка выполнена. Смотрите раздел «Диагностика».",
        )
    conclusion = data.get("conclusion") if isinstance(data.get("conclusion"), dict) else {}
    if "traffic_light" in verification:
        traffic_light = _need(verification, "traffic_light", "verification.", path, str)
    else:
        traffic_light = _need(conclusion, "traffic_light", "conclusion.", path, str)
    requirements = _need(segmentation, "requirements", "segmentation.", path, list)
    findings = _need(verification, "findings", "verification.", path, list)
    return ReportView(
        path=str(path),
        passport=_passport(passport, path),
        requirements=tuple(_requirement(r, path) for r in requirements),
        findings=tuple(_finding(f, path) for f in findings),
        traffic_light=traffic_light,
        headline=_str(conclusion, "headline"),
        questions=len(_need(data, "questions", "", path, list)),
        status=_need(data, "status", "", path, str),
    )


def load_document_text(path: Path) -> str:
    """Текст документа той же разборкой, что у агента (MD, PDF, DOCX), без вызовов модели."""
    kind = _DOC_KINDS.get(path.suffix.lower())
    if kind is None:
        raise ReportReadError(
            f"Документ {path}: формат {path.suffix or 'без расширения'} не поддерживается",
            "Укажите документ .md, .pdf или .docx.",
        )
    if not path.is_file():
        raise ReportReadError(f"Не найден документ {path}", "Проверьте путь к документу.")
    try:
        return parse(path, kind).text
    except (InputError, OSError, ValueError) as exc:
        raise ReportReadError(
            f"Не удалось прочитать документ {path}: {exc}",
            "Сравнение можно сделать и без документов, только по отчётам.",
        ) from exc
