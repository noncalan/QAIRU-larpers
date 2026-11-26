"""Шаг 4г: проход на пропуски (опция ``--gaps``, по умолчанию выключен).

Проход поиска (шаг 4) идёт от написанного текста и хуже видит то, чего в требовании нет: убранную
границу или единицу, действие без исполнителя, перечень без ветки ошибки (замер на мутациях документа
в описании PR шага). Этот шаг делает один узкий вызов на часть документа: модель идёт
по блокам и заполняет карточку «исполнитель, числа и границы, условие, отказ, перечни»
(``prompts/gaps_system.md``), пустое поле карточки становится находкой. Промпт основного поиска не меняется.

Контракт: ``find_gaps(doc, plan, known, llm, cfg) -> GapsOutcome``.

- Вызов видит находки поиска и линтера (место и суть, ``render_known``) и не повторяет их: шаг идёт
  после поиска и добавляет только то, чего в них нет.
- Формат ответа тот же, что у поиска, его разбирает тот же код (``search_parse.py``). Запасного переноса
  в JSON моделью здесь нет: находка, которую код не разобрал, отбрасывается и считается в диагностике.
- Решает код, а не модель: находка не о пропуске (тип дефекта не «неполнота» и не «двусмысленность»)
  отбрасывается, ID блоков сверяются с документом, цитаты дальше сверяет верификатор (шаг 7).
- Источник находки ``SourceKind.GAPS``: в «найдено в N из K» проход не считается, при слиянии
  (``merge.py``) его находка склеивается с другой, только если у них одна суть.
- Сбой вызова не роняет прогон: диагностика ``LLM-074``, находки поиска остаются.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from validator.config import call_timeout
from validator.errors import LLM_BAD_RESPONSE, LLM_GAPS_CHECKED, LLM_GAPS_FAILED, LLMError
from validator.llm.base import LLMClient
from validator.models import (
    CandidateFinding,
    DefectType,
    DiagLevel,
    Diagnostic,
    FindingSource,
    LLMRequest,
    LLMStep,
    ParsedDocument,
    RunConfig,
    SourceKind,
)
from validator.parts import Part, PartPlan, part_view, render_part
from validator.prompting import fill, load_prompt
from validator.search_parse import Draft, draft_from_raw, split_findings

logger = logging.getLogger(__name__)

GAPS_SYSTEM = "gaps_system.md"
GAPS_USER = "gaps_user.md"
MAX_PARALLEL_PARTS = 3
"""Как у поиска: не больше трёх вызовов одновременно (лимит подписки)."""
KNOWN_LIMIT = 200
"""Сколько уже найденных находок показываем вызову: на длинном документе их сотни."""
KNOWN_QUOTE_CHARS = 160
GAP_TYPES = frozenset({DefectType.INCOMPLETE, DefectType.AMBIGUOUS})
"""Типы дефекта, которые шаг вправе вернуть: остальное ищет основной поиск."""
NOTHING_KNOWN = "(находок пока нет)"


@dataclass
class GapsOutcome:
    candidates: list[CandidateFinding] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def render_known(known: Sequence[CandidateFinding], blocks: frozenset[str] | None = None) -> str:
    """Уже найденное для промпта: «- [блоки] «цитата» — суть», без повторов, в порядке ``known``.

    ``blocks``: блоки части длинного документа; находка без блоков этой части в её вызов не идёт.
    """
    lines: list[str] = []
    for c in known:
        if (
            blocks is not None
            and c.block_ids
            and not blocks.intersection(b.split(".", 1)[0] for b in c.block_ids)
        ):
            continue
        gist = _one_line(c.title or c.explanation, KNOWN_QUOTE_CHARS)
        quote = _one_line(c.quotes[0], KNOWN_QUOTE_CHARS) if c.quotes else ""
        where = f"[{', '.join(c.block_ids[:3])}] " if c.block_ids else ""
        line = f"- {where}«{quote}» — {gist}" if quote else f"- {where}{gist}"
        if line not in lines:
            lines.append(line)
        if len(lines) >= KNOWN_LIMIT:
            break
    return "\n".join(lines) or NOTHING_KNOWN


def gaps_request(rendered: str, known: str, cfg: RunConfig, index: int) -> LLMRequest:
    """Запрос прохода на пропуски по одной части: свободный текст в формате поиска, без схемы."""
    model = cfg.model_for(LLMStep.GAPS)
    return LLMRequest(
        step=LLMStep.GAPS,
        call_index=index,
        model=model.model,
        effort=model.effort,
        system=load_prompt(GAPS_SYSTEM),
        prompt=fill(load_prompt(GAPS_USER), document=rendered, found=known),
        timeout_s=call_timeout(cfg, rendered),
    )


@dataclass
class _PartResult:
    part: int
    call_id: str
    candidates: list[CandidateFinding] = field(default_factory=list)
    unparsed: int = 0
    off_topic: int = 0
    error: LLMError | None = None


def _block_ids(doc: ParsedDocument, ids: Sequence[str]) -> list[str]:
    """ID блоков, которые есть в документе; несуществующее предложение заменяется своим блоком."""
    kept: list[str] = []
    for bid in ids:
        block = doc.block(bid)
        if block is None:
            continue
        known = bid if "." not in bid or doc.sentence(bid) is not None else block.id
        if known not in kept:
            kept.append(known)
    return kept


def _candidate(d: Draft, cid: str, call_id: str, block_ids: list[str]) -> CandidateFinding:
    return CandidateFinding(
        cid=cid,
        source=FindingSource(kind=SourceKind.GAPS, call_id=call_id),
        property=d.property,
        defect_type=d.defect_type,
        verdict=d.verdict,
        scope=d.scope,
        block_ids=block_ids,
        quotes=list(d.quotes),
        explanation=d.explanation,
        suggestion=d.suggestion,
        blocks_testing=d.blocks_testing,
        severity_hint=d.severity_hint,
        title=d.title,
        test_gap=d.test_gap,
    )


def parse_gaps(text: str, view: ParsedDocument, part: int, parts: int, call_id: str) -> _PartResult:
    """Находки из ответа прохода: формат поиска, только пропуски, только известные блоки."""
    out = _PartResult(part=part, call_id=call_id)
    for raw in split_findings(text).findings:
        draft = draft_from_raw(raw)
        if not isinstance(draft, Draft):
            out.unparsed += bool(raw.fields)  # строка «Пропусков нет» без полей не находка и не потеря
            continue
        if draft.defect_type not in GAP_TYPES:
            out.off_topic += 1
            continue
        seq = len(out.candidates) + 1
        cid = f"gap.{seq:03d}" if parts == 1 else f"gap.{part}.{seq:03d}"
        out.candidates.append(_candidate(draft, cid, call_id, _block_ids(view, draft.block_ids)))
    return out


def _run_part(
    doc: ParsedDocument,
    plan: PartPlan,
    part: Part,
    known: Sequence[CandidateFinding],
    llm: LLMClient,
    cfg: RunConfig,
) -> _PartResult:
    rendered = render_part(doc, plan, part, with_context=True)
    blocks = part.ids if plan.split else None
    req = gaps_request(rendered, render_known(known, blocks), cfg, part.index)
    try:
        resp = llm.call(req)
        if not resp.text.strip():
            raise LLMError("модель вернула пустой ответ", code=LLM_BAD_RESPONSE, location=req.call_id)
    except LLMError as exc:
        return _PartResult(part=part.index, call_id=req.call_id, error=exc)
    view = part_view(doc, plan, part, with_context=True)
    out = parse_gaps(resp.text, view, part.index, len(plan.parts), req.call_id)
    context = frozenset(b.id for b in plan.context_for(part)) if plan.split else frozenset()
    # Находка только о блоках раздела терминов из другой части: её даст своя часть.
    out.candidates = [c for c in out.candidates if not (c.block_ids and context.issuperset(c.block_ids))]
    return out


def _diagnostics(results: Sequence[_PartResult], parts: int) -> list[Diagnostic]:
    diags: list[Diagnostic] = []
    for r in results:
        where = f" (часть {r.part} из {parts})" if parts > 1 else ""
        if r.error is not None:
            diags.append(
                Diagnostic(
                    code=LLM_GAPS_FAILED,
                    level=DiagLevel.WARNING,
                    message=f"Проход на пропуски не удался{where}: {r.error.message}",
                    action="Находки основного поиска остаются, пропуски отдельно не искались",
                    hint=r.error.hint,
                    location=r.error.location or r.call_id,
                    step="gaps",
                )
            )
        elif r.unparsed or r.off_topic:
            diags.append(
                Diagnostic(
                    code=LLM_GAPS_CHECKED,
                    level=DiagLevel.INFO,
                    message=(
                        f"Проход на пропуски{where}: не разобрано кодом {r.unparsed}, "
                        f"не о пропуске {r.off_topic}"
                    ),
                    action=f"Эти записи отброшены, взято находок: {len(r.candidates)}",
                    location=r.call_id,
                    step="gaps",
                )
            )
    return diags


def find_gaps(
    doc: ParsedDocument,
    plan: PartPlan,
    known: Sequence[CandidateFinding],
    llm: LLMClient,
    cfg: RunConfig,
) -> GapsOutcome:
    """Пропуски по каждой части документа. ``known``: кандидаты поиска и линтера, их не повторяем."""
    for name in (GAPS_SYSTEM, GAPS_USER):
        load_prompt(name)
    with ThreadPoolExecutor(
        max_workers=min(MAX_PARALLEL_PARTS, len(plan.parts)), thread_name_prefix="gaps"
    ) as pool:
        results = list(pool.map(lambda part: _run_part(doc, plan, part, known, llm, cfg), plan.parts))
    return GapsOutcome(
        candidates=[c for r in results for c in r.candidates],
        diagnostics=_diagnostics(results, len(plan.parts)),
        call_ids=[r.call_id for r in results],
    )


__all__ = ["GAP_TYPES", "GapsOutcome", "find_gaps", "gaps_request", "parse_gaps", "render_known"]
