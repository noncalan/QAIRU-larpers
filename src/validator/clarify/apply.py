"""Решение кода по ответу сверки с ответами автора (issue #42). Модель предлагает, код проверяет.

- **Закрыто.** Находка должна быть в этом прогоне, ответ на вопрос должен быть (вариант или свой текст,
  не «пропустить»), а связь находки с вопросом код подтверждает сам: находка в ``related_findings``
  вопроса этого прогона или её цитата лежит в том же месте документа, что и места вопроса. Без связи
  «закрыто» понижается до «уточнено»: находка остаётся в отчёте, вывод модели виден в примечании.
- **Уточнено.** Находка остаётся в отчёте с примечанием «уточнено ответом Q-NN» и тем, что осталось.
- **Новое.** Новая находка проходит тот же верификатор, что и находки поиска (``verify.check``): без
  сверенной цитаты из документа она отбрасывается (CLAUDE.md, правило 4). Повтор существующей находки
  (тот же тип дефекта в том же месте) отбрасывается. Номера новых находок идут после последнего ``F-NN``
  прогона, а номера закрытых не переиспользуются: в отчёте видно, какая находка закрыта и чем.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from validator.clarify.answers import normalize_question_id
from validator.clarify.model import ClarifyReply, NewFindingRecord, normalize_finding_id
from validator.models import (
    AuthorAnswer,
    ClarifiedFinding,
    ClarifyEffect,
    DiscardedFinding,
    Finding,
    FindingSource,
    MergedFinding,
    ParsedDocument,
    Quote,
    Segmentation,
    SourceKind,
    Verdict,
    Verification,
)
from validator.questions.quotes import span
from validator.refs import RefNamer, humanize_finding
from validator.verify import check, requirement_status, summarize, traffic_light

MAX_NEW_FINDINGS = 10
"""Больше десяти новых находок от ответов это уже не уточнение, а новый проход поиска."""

_FINDING_NUMBER = re.compile(r"F-(\d+)")


@dataclass
class ApplyLog:
    """Что код не принял из ответа модели: из этого собирается строка диагностики."""

    invalid: int = 0
    unknown_finding: int = 0
    no_answer: int = 0
    unlinked: int = 0
    new_unverified: int = 0
    new_duplicate: int = 0
    new_over_limit: int = 0

    def details(self) -> list[str]:
        labels = (
            (self.invalid, "записей не по схеме"),
            (self.unknown_finding, "ссылок на находки, которых нет в этом прогоне"),
            (self.no_answer, "выводов без ответа автора на вопрос (нет вопроса, ответ пропущен)"),
            (self.unlinked, "«закрыто» без подтверждённой связи находки с вопросом, оставлено «уточнено»"),
            (self.new_unverified, "новых находок без сверенной цитаты из документа"),
            (self.new_duplicate, "новых находок, повторяющих находки прогона"),
            (self.new_over_limit, f"новых находок сверх {MAX_NEW_FINDINGS}"),
        )
        return [f"{text}: {count}" for count, text in labels if count]


@dataclass(frozen=True)
class Applied:
    verification: Verification
    effects: list[ClarifiedFinding] = field(default_factory=list)
    log: ApplyLog = field(default_factory=ApplyLog)


@dataclass(frozen=True)
class AnswerContext:
    """Ответ автора и то, с чем код сверяет связь: места вопроса в документе и находки вопроса."""

    answer: AuthorAnswer
    places: list[Quote]
    findings: frozenset[str]


# ---------------------------------------------------------------- связь находки с вопросом


def _blocks(quotes: Iterable[Quote]) -> set[str]:
    return {b.split(".")[0] for q in quotes if q.anchor for b in q.anchor.block_ids}


def _same_place(a: Sequence[Quote], b: Sequence[Quote]) -> bool:
    """Цитаты пересекаются в тексте документа или лежат в одном блоке."""
    spans_b = [s for s in map(span, b) if s is not None]
    for s in map(span, a):
        if s is not None and any(s[0] < t[1] and t[0] < s[1] for t in spans_b):
            return True
    return bool(_blocks(a) & _blocks(b))


def _linked(f: Finding, ctx: AnswerContext) -> bool:
    return f.id in ctx.findings or _same_place(f.quotes, ctx.places)


def _answer_ids(raw: Sequence[str], answers: Mapping[str, AnswerContext]) -> list[str]:
    ids = (normalize_question_id(r) for r in raw)
    return list(dict.fromkeys(i for i in ids if i is not None and i in answers))


# ---------------------------------------------------------------- закрыто и уточнено


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _effects(
    reply: ClarifyReply,
    findings: Mapping[str, Finding],
    answers: Mapping[str, AnswerContext],
    namer: RefNamer,
    log: ApplyLog,
) -> dict[str, ClarifiedFinding]:
    """F-NN → итог по находке. «Закрыто» сильнее «уточнено»; вопросы одной находки объединяются."""
    out: dict[str, ClarifiedFinding] = {}
    for r in reply.effects:
        fid = normalize_finding_id(r.finding)
        if fid is None or fid not in findings:
            log.unknown_finding += 1
            continue
        qids = _answer_ids(r.answers, answers)
        if not qids:
            log.no_answer += 1
            continue
        effect = ClarifyEffect(r.effect)
        if effect is ClarifyEffect.CLOSED and not any(_linked(findings[fid], answers[q]) for q in qids):
            log.unlinked += 1
            effect = ClarifyEffect.REFINED
        item = ClarifiedFinding(
            finding_id=fid,
            effect=effect,
            question_ids=qids,
            reason=namer.text(_one_line(r.reason)),
            fix=namer.text(_one_line(r.fix)),
            remaining=namer.text(_one_line(r.remaining)),
        )
        out[fid] = _combine(out.get(fid), item)
    return out


def _combine(old: ClarifiedFinding | None, new: ClarifiedFinding) -> ClarifiedFinding:
    if old is None:
        return new
    main, other = (
        (new, old)
        if new.effect is ClarifyEffect.CLOSED and old.effect is not ClarifyEffect.CLOSED
        else (old, new)
    )
    qids = list(dict.fromkeys([*main.question_ids, *other.question_ids]))
    return main.model_copy(update={"question_ids": qids})


def _refined_note(e: ClarifiedFinding) -> str:
    reason = e.reason.rstrip(".") or "см. раздел «Уточнения»"
    note = f"Уточнено ответом автора {', '.join(e.question_ids)}: {reason}."
    if e.remaining:
        note += f" Осталось открытым: {e.remaining.rstrip('.')}."
    return note


# ---------------------------------------------------------------- новые находки


def _merged(r: NewFindingRecord, n: int, qids: Sequence[str], of: int, call_id: str) -> MergedFinding:
    verdict = (
        Verdict.UNCLEAR if str(r.verdict).strip().lower() in ("unclear", "неясно") else Verdict.VIOLATION
    )
    return MergedFinding(
        mid=f"A-{n:03d}",
        property=r.property,
        defect_type=r.defect_type,
        verdict=verdict,
        scope=r.property.scope,
        quotes=[q for q in r.quotes if q.strip()],
        explanation=_one_line(r.explanation),
        suggestion=_one_line(r.suggestion) if r.suggestion else None,
        title=_one_line(r.title) or None,
        blocks_testing=r.blocks_testing,
        sources=[FindingSource(kind=SourceKind.ANSWER, rule_id=", ".join(qids), call_id=call_id or None)],
        found_in=0,
        of=of,
    )


def _duplicate(f: Finding, existing: Iterable[Finding]) -> bool:
    return any(e.defect_type is f.defect_type and _same_place(f.quotes, e.quotes) for e in existing)


def _next_number(findings: Iterable[Finding]) -> int:
    numbers = [int(m.group(1)) for f in findings if (m := _FINDING_NUMBER.fullmatch(f.id))]
    return max(numbers, default=0) + 1


@dataclass
class NewFindings:
    findings: list[Finding] = field(default_factory=list)
    discarded: list[DiscardedFinding] = field(default_factory=list)
    quotes: list[Quote] = field(default_factory=list)
    effects: list[ClarifiedFinding] = field(default_factory=list)


def _new_findings(
    reply: ClarifyReply,
    ver: Verification,
    answers: Mapping[str, AnswerContext],
    doc: ParsedDocument,
    seg: Segmentation,
    namer: RefNamer,
    log: ApplyLog,
) -> NewFindings:
    out = NewFindings()
    of = max((f.of for f in ver.findings), default=1)
    kept: list[tuple[Finding, list[str]]] = []
    for n, r in enumerate(reply.new_findings, 1):
        qids = _answer_ids(r.answers, answers)
        if not qids:
            log.no_answer += 1
            continue
        checked, quotes = check(_merged(r, n, qids, of, reply.call_id), doc, seg)
        out.quotes += quotes
        if isinstance(checked, DiscardedFinding):
            log.new_unverified += 1
            out.discarded.append(checked)
            continue
        if _duplicate(checked, [*ver.findings, *(f for f, _ in kept)]):
            log.new_duplicate += 1
            continue
        if len(kept) >= MAX_NEW_FINDINGS:
            log.new_over_limit += 1
            continue
        kept.append((checked, qids))
    ordered = sorted(kept, key=lambda item: (item[0].severity.rank, _position(item[0]), item[0].merged_id))
    start = _next_number(ver.findings)
    for i, (f, qids) in enumerate(ordered):
        fid = f"F-{start + i:02d}"
        note = f"Новая находка: появилась из-за ответа автора {', '.join(qids)}."
        numbered = humanize_finding(f.model_copy(update={"id": fid, "notes": [*f.notes, note]}), namer)
        out.findings.append(numbered)
        out.effects.append(
            ClarifiedFinding(
                finding_id=fid,
                effect=ClarifyEffect.NEW,
                question_ids=qids,
                reason=numbered.title or numbered.explanation,
                fix=numbered.suggestion or "",
            )
        )
    return out


# ---------------------------------------------------------------- проверка заново


def _position(f: Finding) -> int:
    return min((q.anchor.char_start for q in f.quotes if q.anchor), default=1 << 30)


def recount(
    ver: Verification,
    findings: Sequence[Finding],
    seg: Segmentation,
    *,
    discarded: Sequence[DiscardedFinding] = (),
    quotes: Sequence[Quote] = (),
) -> Verification:
    """Сводка, статус требований и светофор заново по оставшимся и новым находкам.

    Счётчики цитат складываются: цитаты первого прогона уже сверены (их нет в ``Finding``, если цитату
    не нашли), к ним добавляются цитаты новых находок.
    """
    ordered = sorted(findings, key=lambda f: (f.severity.rank, _position(f), f.merged_id))
    all_discarded = [*ver.discarded, *discarded]
    status = requirement_status(ordered, seg)
    s = summarize(ordered, all_discarded, seg, quotes, status)
    old = ver.summary
    summary = s.model_copy(
        update={
            "quotes_total": old.quotes_total + s.quotes_total,
            "quotes_exact": old.quotes_exact + s.quotes_exact,
            "quotes_fuzzy": old.quotes_fuzzy + s.quotes_fuzzy,
            "quotes_not_found": old.quotes_not_found + s.quotes_not_found,
        }
    )
    light, reason = traffic_light(ordered)
    return ver.model_copy(
        update={
            "findings": ordered,
            "discarded": all_discarded,
            "summary": summary,
            "traffic_light": light,
            "traffic_reason": reason,
            "requirement_status": status,
        }
    )


def apply_reply(
    reply: ClarifyReply,
    ver: Verification,
    answers: Mapping[str, AnswerContext],
    doc: ParsedDocument,
    seg: Segmentation,
) -> Applied:
    """Новая проверка после ответов: закрытые находки убраны, уточнённые с примечанием, новые добавлены."""
    namer = RefNamer(doc, seg)
    log = ApplyLog(invalid=reply.invalid)
    by_id = {f.id: f for f in ver.findings}
    effects = _effects(reply, by_id, answers, namer, log)
    new = _new_findings(reply, ver, answers, doc, seg, namer, log)
    remaining: list[Finding] = []
    listed: list[ClarifiedFinding] = []
    for f in ver.findings:
        e = effects.get(f.id)
        if e is None:
            remaining.append(f)
        elif e.effect is ClarifyEffect.CLOSED:
            listed.append(e.model_copy(update={"finding": humanize_finding(f, namer)}))
        else:
            remaining.append(f.model_copy(update={"notes": [*f.notes, _refined_note(e)]}))
            listed.append(e)
    rebuilt = recount(ver, [*remaining, *new.findings], seg, discarded=new.discarded, quotes=new.quotes)
    return Applied(verification=rebuilt, effects=[*listed, *new.effects], log=log)


__all__ = ["MAX_NEW_FINDINGS", "AnswerContext", "Applied", "ApplyLog", "apply_reply", "recount"]
