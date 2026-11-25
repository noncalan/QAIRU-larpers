"""Шаг 4: проходы поиска дефектов, разбор ответа и перенос в JSON (issue #18).

Контракт: ``search(doc, llm, cfg) -> list[PassResult]``.

- ``cfg.passes`` независимых проходов Opus свободным текстом по промпту #14 (``prompts/search_system.md``,
  ``prompts/search_user.md``), без JSON-схемы: со схемой модель думает меньше и находит меньше
  (исследование 21). Проходы идут параллельно, не больше ``MAX_PARALLEL_PASSES`` одновременно.
- Ответ прохода разбирает код (``search_parse.py``): формат ответа строгий, разбор бесплатный и
  детерминированный. Находки, которые код не разобрал (нет цитаты, свойства или типа дефекта, сбит
  заголовок), переносит в JSON модель шага ``search_transfer`` (Haiku, D-010) по промпту
  ``prompts/search_transfer.md`` и схеме из ``search_transfer.py`` с одним повтором. Это отклонение от D-010,
  где Haiku переносит каждый проход: так дешевле и стабильнее, а Haiku остаётся запасным путём
  (рекомендация интегратора в #18).
- ID блоков, которых нет в документе, отбрасываются с диагностикой; ID несуществующего предложения
  заменяется ID его блока. Цитаты здесь не сверяются: это делает верификатор (шаг 7).
- Упавший проход это ``PassResult(ok=False)`` с диагностикой ``LLM-030`` «Проход k из K не удался»,
  остальные идут дальше («K−1 из K»). Если упали все, шаг возвращает упавшие проходы без находок,
  а конвейер собирает отчёт без находок модели (``LLM-031``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from validator.errors import (
    LLM_BAD_RESPONSE,
    LLM_PASS_FAILED,
    LLM_PASS_TRANSFERRED,
    LLM_TRANSFER_FAILED,
    LLM_UNKNOWN_BLOCKS,
    LLMError,
)
from validator.ingest.blocks import render_for_model
from validator.llm.base import LLMClient
from validator.models import (
    CandidateFinding,
    DiagLevel,
    Diagnostic,
    FindingSource,
    LLMRequest,
    LLMStep,
    ParsedDocument,
    PassResult,
    RunConfig,
    SourceKind,
)
from validator.prompting import fill, load_prompt
from validator.search_parse import Draft, RawFinding, draft_from_raw, split_findings
from validator.search_transfer import (
    TRANSFER_ATTEMPTS,
    TRANSFER_SYSTEM,
    TRANSFER_USER,
    transfer_request,
    transferred,
)

SEARCH_SYSTEM = "search_system.md"
SEARCH_USER = "search_user.md"
MAX_PARALLEL_PASSES = 3
"""Не больше трёх проходов одновременно: лимит подписки и нагрузка на машину (issue #18)."""
EXAMPLES_IN_DIAGNOSTIC = 5
"""Сколько примеров ID показать в диагностике."""


def search_request(rendered: str, cfg: RunConfig, index: int) -> LLMRequest:
    """Запрос одного прохода: свободный текст, без схемы, модель и effort из профиля."""
    model = cfg.model_for(LLMStep.SEARCH)
    return LLMRequest(
        step=LLMStep.SEARCH,
        call_index=index,
        model=model.model,
        effort=model.effort,
        system=load_prompt(SEARCH_SYSTEM),
        prompt=fill(load_prompt(SEARCH_USER), document=rendered),
        timeout_s=cfg.call_timeout_s,
    )


# ---------------------------------------------------------------- один проход


@dataclass(frozen=True)
class _Context:
    doc: ParsedDocument
    rendered: str
    llm: LLMClient
    cfg: RunConfig


@dataclass
class _Outcome:
    """Итог прохода до сборки ``PassResult``: текст ошибки «K−1 из K» известен только в конце шага."""

    index: int
    raw_text: str = ""
    candidates: list[CandidateFinding] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    error: LLMError | None = None


def _transfer(
    ctx: _Context, index: int, chunks: Sequence[RawFinding], findings: str, out: _Outcome
) -> tuple[list[tuple[float, Draft]], int]:
    errors: list[LLMError] = []
    for attempt in range(TRANSFER_ATTEMPTS):
        req = transfer_request(ctx.rendered, findings, ctx.cfg, index, attempt)
        out.call_ids.append(req.call_id)
        try:
            resp = ctx.llm.call(req)
            return transferred(resp.structured, chunks, req.call_id)
        except LLMError as exc:
            errors.append(exc)
    raise errors[-1]


def _reasons(counter: Counter[str]) -> str:
    """«нет поля цитата: 2, одна находка на фрагмент: 1»."""
    return ", ".join(f"{reason}: {count}" for reason, count in counter.most_common())


def _drafts(ctx: _Context, index: int, text: str, out: _Outcome) -> list[Draft]:
    """Черновики находок прохода: код разбирает формат, остальное переносит модель."""
    split = split_findings(text)
    parsed: list[tuple[float, Draft]] = []
    pending: list[RawFinding] = []
    why: Counter[str] = Counter()
    for raw in split.findings:
        draft = draft_from_raw(raw)
        if isinstance(draft, Draft):
            parsed.append((raw.position, draft))
        else:
            pending.append(raw)
            why.update(draft)
    if split.has_section and not pending:
        return [d for _, d in parsed]

    whole = not split.has_section
    findings = text if whole else "\n\n".join(raw.text for raw in pending)
    try:
        moved, invalid = _transfer(ctx, index, pending, findings, out)
    except LLMError as exc:
        if whole or not parsed:
            raise LLMError(
                f"ответ не разобран кодом, а перенос в JSON не удался ({exc.message})",
                code=exc.code,
                hint=exc.hint,
                location=exc.location,
            ) from exc
        out.diagnostics.append(
            Diagnostic(
                code=LLM_TRANSFER_FAILED,
                level=DiagLevel.WARNING,
                message=(
                    f"Проход {index}: {len(pending)} из {len(pending) + len(parsed)} находок "
                    f"не разобраны кодом ({_reasons(why)}), а перенос в JSON не удался: {exc.message}"
                ),
                action="Эти находки не вошли в отчёт, остальные находки прохода взяты в работу",
                hint=exc.hint,
                location=exc.location,
                step="search",
            )
        )
        return [d for _, d in parsed]
    if whole and not moved:
        raise LLMError(
            "ответ не в формате промпта, и находок в нём нет",
            code=LLM_BAD_RESPONSE,
            location=f"search.{index}",
        )
    what = "ответ не в формате промпта" if whole else f"не разобраны кодом: {_reasons(why)}"
    model = ctx.cfg.model_for(LLMStep.SEARCH_TRANSFER).model
    skipped = f"; записей не по схеме: {invalid}" if invalid else ""
    out.diagnostics.append(
        Diagnostic(
            code=LLM_PASS_TRANSFERRED,
            level=DiagLevel.INFO,
            message=f"Проход {index}: {what}{skipped}",
            action=f"Перенесено в JSON моделью {model}: {len(moved)} находок",
            step="search",
        )
    )
    return [d for _, d in sorted(parsed + moved, key=lambda pd: pd[0])]


def _known_blocks(
    doc: ParsedDocument, ids: Sequence[str], unknown: list[str], coarse: list[str]
) -> list[str]:
    """ID, которые есть в документе. Несуществующее предложение B012.9 заменяется блоком B012."""
    kept: list[str] = []
    for bid in ids:
        block = doc.block(bid)
        if block is None:
            unknown.append(bid)
            continue
        known = bid
        if "." in bid and doc.sentence(bid) is None:
            coarse.append(bid)
            known = block.id
        if known not in kept:
            kept.append(known)
    return kept


def _candidates(ctx: _Context, index: int, call_id: str, drafts: Sequence[Draft], out: _Outcome) -> None:
    unknown: list[str] = []
    coarse: list[str] = []
    source = FindingSource(kind=SourceKind.PASS, pass_index=index, call_id=call_id)
    for seq, d in enumerate(drafts, 1):
        out.candidates.append(
            CandidateFinding(
                cid=f"p{index}.{seq:03d}",
                source=source,
                property=d.property,
                defect_type=d.defect_type,
                verdict=d.verdict,
                scope=d.scope,
                block_ids=_known_blocks(ctx.doc, d.block_ids, unknown, coarse),
                quotes=list(d.quotes),
                explanation=d.explanation,
                suggestion=d.suggestion,
                blocks_testing=d.blocks_testing,
                severity_hint=d.severity_hint,
                title=d.title,
                scenario=d.scenario,
                test_gap=d.test_gap,
            )
        )
    if unknown or coarse:
        parts = []
        if unknown:
            parts.append(
                f"отброшены ID блоков, которых нет в документе: {len(unknown)} ({_examples(unknown)})"
            )
        if coarse:
            parts.append(
                f"ID несуществующих предложений заменены ID блока: {len(coarse)} ({_examples(coarse)})"
            )
        out.diagnostics.append(
            Diagnostic(
                code=LLM_UNKNOWN_BLOCKS,
                level=DiagLevel.INFO,
                message=f"Проход {index}: " + "; ".join(parts),
                action="Находки оставлены: место в документе по цитате найдёт верификатор",
                location=call_id,
                step="search",
            )
        )


def _examples(ids: Sequence[str]) -> str:
    shown = ", ".join(list(dict.fromkeys(ids))[:EXAMPLES_IN_DIAGNOSTIC])
    return shown + (", …" if len(set(ids)) > EXAMPLES_IN_DIAGNOSTIC else "")


def run_pass(ctx: _Context, index: int) -> _Outcome:
    """Один проход: вызов модели, разбор ответа, при нужде перенос в JSON, проверка ID блоков."""
    out = _Outcome(index=index)
    req = search_request(ctx.rendered, ctx.cfg, index)
    out.call_ids.append(req.call_id)
    try:
        resp = ctx.llm.call(req)
        out.raw_text = resp.text
        if not resp.text.strip():
            raise LLMError("модель вернула пустой ответ", code=LLM_BAD_RESPONSE, location=req.call_id)
        drafts = _drafts(ctx, index, resp.text, out)
    except LLMError as exc:
        out.error = exc
        return out
    _candidates(ctx, index, req.call_id, drafts, out)
    return out


# ---------------------------------------------------------------- шаг целиком


def _lower_first(message: str) -> str:
    """«Модель не ответила» → «модель не ответила» после двоеточия; «JSON …» и «LLM …» не трогаем."""
    if len(message) > 1 and message[0].isupper() and message[1].islower():
        return message[0].lower() + message[1:]
    return message


def _result(o: _Outcome, ok: int, total: int) -> PassResult:
    if o.error is None:
        return PassResult(
            index=o.index,
            ok=True,
            raw_text=o.raw_text,
            candidates=o.candidates,
            call_ids=o.call_ids,
            diagnostics=o.diagnostics,
        )
    exc = o.error
    failed = Diagnostic(
        code=LLM_PASS_FAILED,
        level=DiagLevel.WARNING,
        message=f"Проход {o.index} из {total} не удался: {_lower_first(exc.message)}",
        action=f"Находки собраны по {ok} из {total} проходов" if ok else "Находок модели в отчёте нет",
        hint=exc.hint,
        location=exc.location or f"search.{o.index}",
        step="search",
    )
    return PassResult(
        index=o.index,
        ok=False,
        raw_text=o.raw_text,
        call_ids=o.call_ids,
        error=exc.message,
        diagnostics=[*o.diagnostics, failed],
    )


def search(doc: ParsedDocument, llm: LLMClient, cfg: RunConfig) -> list[PassResult]:
    """``cfg.passes`` проходов по документу. Упавший проход это ``PassResult(ok=False)``, а не исключение."""
    for name in (SEARCH_SYSTEM, SEARCH_USER, TRANSFER_SYSTEM, TRANSFER_USER):
        load_prompt(name)
    ctx = _Context(doc=doc, rendered=render_for_model(doc), llm=llm, cfg=cfg)
    indexes = range(1, cfg.passes + 1)
    with ThreadPoolExecutor(
        max_workers=min(MAX_PARALLEL_PASSES, cfg.passes), thread_name_prefix="search"
    ) as pool:
        outcomes = list(pool.map(lambda k: run_pass(ctx, k), indexes))
    ok = sum(o.error is None for o in outcomes)
    return [_result(o, ok, cfg.passes) for o in outcomes]


__all__ = ["MAX_PARALLEL_PASSES", "search", "search_request"]
