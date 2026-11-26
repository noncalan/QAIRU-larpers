"""Общее для шага 6 (свидетель и критик): цитаты находок, цитаты модели, находка в промпте, разбор ответа.

Свидетель (``evidence_witness``) и критик (``evidence_critic``) видят находку одинаково: её ID слияния
(``M-012``), тип, свойство, суть, объяснение и цитаты, которые уже сверил код (дословный текст документа и
ID блока). Цитаты, которые модель пишет в ответ, код сверяет здесь же тем же способом, что и верификатор:
сначала в указанных блоках, затем во всём документе; строку таблицы в виде «Колонка: значение; …» по ячейкам.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import BaseModel, ConfigDict

from validator.models import MergedFinding, ParsedDocument, Quote, SourceKind
from validator.questions.quotes import span, verify_quote
from validator.text import MIN_FUZZY_CHARS, locate_quote, normalize
from validator.verify import empty_cell_quote, locate

MIN_QUOTE_CHARS: Final = 3
"""Сколько букв и цифр должно быть в цитате модели, чтобы её вообще искать в документе."""

_MID = re.compile(r"M\s*-?\s*0*(\d+)", re.IGNORECASE)

Located = Mapping[str, Sequence[Quote]]
"""ID слияния (``M-012``) → сверенные цитаты находки, в порядке её цитат."""


class Lenient(BaseModel):
    """Разбор ответа модели: лишние поля не мешают, запись не по схеме выпадает одна."""

    model_config = ConfigDict(extra="ignore")


def one_line(text: str | None) -> str:
    return " ".join((text or "").split())


def normalize_mid(raw: object) -> str | None:
    """``M-012``, ``m12``, ``M 12`` → ``M-012``; не ID находки → None."""
    if not isinstance(raw, str):
        return None
    m = _MID.fullmatch(raw.strip())
    return f"M-{int(m.group(1)):03d}" if m else None


def locate_findings(found: Sequence[MergedFinding], doc: ParsedDocument) -> dict[str, list[Quote]]:
    """Сверенные цитаты каждой находки, как их увидит верификатор (шаг 7)."""
    out: dict[str, list[Quote]] = {}
    for m in found:
        quotes = (locate(doc, q, m.block_ids)[0] for q in m.quotes)
        out[m.mid] = [q for q in quotes if q.verified]
    return out


def from_model(m: MergedFinding) -> bool:
    """Находку дала модель (проход поиска), а не только правило линтера: её есть что перепроверять."""
    return any(s.kind is SourceKind.PASS for s in m.sources) or m.found_in > 0


def top_blocks(quotes: Sequence[Quote]) -> list[str]:
    """Блоки верхнего уровня (``B012`` без номера предложения), где лежат цитаты."""
    ids = (b.split(".", 1)[0] for q in quotes if q.anchor for b in q.anchor.block_ids)
    return list(dict.fromkeys(ids))


def locate_model_quote(doc: ParsedDocument, raw: str, within: Sequence[str]) -> Quote | None:
    """Цитата из ответа модели после сверки с документом или None.

    Короткую цитату (значение ячейки «OK / ERROR», «0 или 1») принимаем, только если она дословно стоит в
    блоках ``within`` (местах самой находки): такая строка найдётся почти в любом месте документа, и вне мест
    находки ничего не докажет.
    """
    text = (raw or "").strip()
    if sum(ch.isalnum() for ch in text) < MIN_QUOTE_CHARS:
        return None
    if len(normalize(text)[0]) < MIN_FUZZY_CHARS:
        found = locate_quote(doc, text, within=within) if within else None
        return found if found is not None and found.verified else None
    return verify_quote(doc, text, within) or empty_cell_quote(doc, text, within)


def new_chars(quote: Quote, own: Sequence[Quote]) -> int:
    """Сколько символов цитаты лежит вне цитат находки: столько в ней нового по сравнению с находкой."""
    bounds = span(quote)
    if bounds is None:
        return 0
    start, end = bounds
    covered = 0
    for other in sorted(s for s in map(span, own) if s is not None):
        lo, hi = max(start, other[0]), min(end, other[1])
        if hi > lo:
            covered += hi - lo
            start = max(start, hi)
    return max(0, (bounds[1] - bounds[0]) - covered)


def render_quote(q: Quote) -> str:
    block = q.anchor.block_ids[0] if q.anchor and q.anchor.block_ids else "?"
    return f"- [{block}] «{one_line(q.located or q.text)}»"


def render_finding(m: MergedFinding, quotes: Sequence[Quote], extra: Sequence[str] = ()) -> str:
    """Находка для промпта свидетеля или критика: суть, объяснение, сверенные цитаты, доп. строки шага."""
    lines = [
        f"### {m.mid}",
        f"Тип дефекта: {m.defect_type.label} · Свойство: {m.property.label} ({m.property.info.english}) · "
        f"Вердикт: {m.verdict.label}",
    ]
    if m.title:
        lines.append(f"Суть: {one_line(m.title)}")
    lines.append(f"Почему это дефект: {one_line(m.explanation)}")
    if m.scenario:
        lines.append(f"Сценарий рецензента: {one_line(m.scenario)}")
    lines.extend(extra)
    lines.append("Цитаты находки (код сверил их с документом):")
    lines.extend(render_quote(q) for q in quotes)
    return "\n".join(lines)


__all__ = [
    "Lenient",
    "Located",
    "from_model",
    "locate_findings",
    "locate_model_quote",
    "new_chars",
    "normalize_mid",
    "one_line",
    "render_finding",
    "render_quote",
    "top_blocks",
]
