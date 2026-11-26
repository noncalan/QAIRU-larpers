"""Чтение ``report.json`` для сравнения версий: только нужные поля, незнакомые терпим.

Схема отчёта ещё меняется между релизами, поэтому сравнение не валидирует ``Report`` целиком, а
берёт из словаря то, что ему нужно. Нет обязательного поля или файл не JSON: ``ReportReadError``
с понятным сообщением по-русски, без трассировки.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
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


def _need(data: Any, key: str, where: str, path: Path) -> Any:
    if not isinstance(data, dict) or key not in data:
        raise ReportReadError(
            f"В отчёте {path} нет поля {where}{key}",
            "Сравнение читает report.json агента validator. Проверьте, что это он, а не другой JSON.",
        )
    return data[key]


def _list(data: Any, key: str) -> list[Any]:
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, list) else []


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


def _quote(raw: Any) -> QuoteView | None:
    if isinstance(raw, str):
        return QuoteView(raw, "")
    if not isinstance(raw, dict):
        return None
    text = _str(raw, "located") or _str(raw, "text")
    return QuoteView(text, _where(raw.get("anchor"))) if text else None


def _requirement(raw: Any, path: Path) -> ReqView:
    rid = _need(raw, "id", "segmentation.requirements[].", path)
    quote = _need(raw, "quote", f"segmentation.requirements[{rid}].", path)
    section = raw.get("section_path")
    return ReqView(
        id=str(rid),
        quote=str(quote),
        stable_key=_str(raw, "stable_key"),
        source_id=raw.get("source_id") if isinstance(raw.get("source_id"), str) else None,
        source_locator=_str(raw, "source_locator"),
        section_path=tuple(str(s) for s in section) if isinstance(section, list) else (),
    )


def _finding(raw: Any, path: Path) -> FindingView:
    fid = str(_need(raw, "id", "verification.findings[].", path))
    prop = str(_need(raw, "property", f"verification.findings[{fid}].", path))
    quotes = tuple(q for q in (_quote(x) for x in _list(raw, "quotes")) if q is not None)
    section = raw.get("section_path")
    return FindingView(
        id=fid,
        property=prop,
        defect_type=_str(raw, "defect_type"),
        severity=_str(raw, "severity", "major"),
        verdict=_str(raw, "verdict", "violation"),
        scope=_str(raw, "scope", "requirement"),
        title=_str(raw, "title") or _str(raw, "explanation"),
        quotes=quotes,
        requirement_ids=tuple(str(r) for r in _list(raw, "requirement_ids")),
        section_path=tuple(str(s) for s in section) if isinstance(section, list) else (),
    )


def _passport(raw: Any, path: Path) -> PassportView:
    _need(raw, "agent_version", "passport.", path)
    models = raw.get("models")
    passes = raw.get("passes")
    cost = raw.get("cost_usd")
    return PassportView(
        document_name=_str(raw, "document_name"),
        document_sha256=_str(raw, "document_sha256"),
        agent_version=_str(raw, "agent_version"),
        prompts_sha256=_str(raw, "prompts_sha256"),
        started_at=_str(raw, "started_at"),
        profile=_str(raw, "profile"),
        passes=passes if isinstance(passes, int) else None,
        models={str(k): str(v) for k, v in models.items()} if isinstance(models, dict) else {},
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
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
    except OSError as exc:
        raise ReportReadError(f"Не удалось прочитать отчёт {path}", "Проверьте права на файл.") from exc


def load_report(path: Path) -> ReportView:
    """``report.json`` → ``ReportView``. Ошибки чтения и схемы: ``ReportReadError``."""
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ReportReadError(f"Отчёт {path} не объект JSON", "Укажите report.json агента validator.")
    passport = _passport(_need(data, "passport", "", path), path)
    segmentation = data.get("segmentation")
    verification = data.get("verification")
    if not isinstance(segmentation, dict) or not isinstance(verification, dict):
        raise ReportReadError(
            f"В отчёте {path} нет нарезки или проверки: прогон не дошёл до проверки требований",
            "Сравнивать можно только отчёты, где проверка выполнена. Смотрите раздел «Диагностика».",
        )
    conclusion = data.get("conclusion") if isinstance(data.get("conclusion"), dict) else {}
    return ReportView(
        path=str(path),
        passport=passport,
        requirements=tuple(_requirement(r, path) for r in _list(segmentation, "requirements")),
        findings=tuple(_finding(f, path) for f in _list(verification, "findings")),
        traffic_light=_str(verification, "traffic_light") or _str(conclusion, "traffic_light"),
        headline=_str(conclusion, "headline"),
        questions=len(_list(data, "questions")),
        status=_str(data, "status"),
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
