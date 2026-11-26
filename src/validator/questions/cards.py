"""Шаг 8в: карточки вопросов. Проверки кодом, запасной путь без модели, приоритет, отбор и номера.

«Модель предлагает, код проверяет» (исследование 17, п. 4.2, шаг 3):

- цитаты карточки сверяются ``locate_quote`` (сначала в блоках кандидатов, затем во всём документе),
  несверенные убираются; если не сверилась ни одна, берём сверенные цитаты связанных находок;
- у карточки 2–4 варианта, у каждого непустое последствие (``change``), повторы вариантов убираются;
- основание варианта (``grounded_in``) тоже сверяется: несверенное значит «вариант для обсуждения»;
- карточка без связанных находок (нет известных кандидатов) отбрасывается;
- дубликаты (почти та же формулировка или те же места и тема) сливаются, каждая находка закрывается
  не больше чем одним вопросом;
- приоритет считает код, порядок: приоритет, ценность ответа, место в документе; затем ``Q-NN``;
  в отчёт идёт не больше ``MAX_QUESTIONS`` карточек.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from rapidfuzz import fuzz

from validator.models import (
    Addressee,
    Finding,
    ParsedDocument,
    Question,
    QuestionOption,
    QuestionOrigin,
    Quote,
)
from validator.questions.candidates import ORIGIN_ORDER, Candidate, same_decision
from validator.questions.editor import EditorCard, EditorOption, normalize_candidate_id
from validator.questions.quotes import span, verify_quote
from validator.questions.scoring import blocks, priority, rank_key, standard_refs
from validator.text import normalize

MAX_QUESTIONS = 15
"""Карточек в отчёте не больше 15 (исследование 17, п. 3.4: 8–15 без набивки)."""
MIN_OPTIONS = 2
MAX_OPTIONS = 4
MAX_QUOTES = 3
MAX_ADDRESSEES = 2
DUPLICATE_SIMILARITY = 85.0
"""Сходство формулировок (rapidfuzz token_sort_ratio), с которого два вопроса считаются одним."""
OPTION_LABELS = "ABCDEFGH"

_ADDRESSEE_ALIASES = {
    **{a.value: a for a in Addressee},
    **{a.label.lower(): a for a in Addressee},
    "автор": Addressee.ANALYST,
    "безопасность": Addressee.SECURITY,
}


@dataclass(frozen=True)
class CardDraft:
    """Карточка до приоритета и номера: всё, что прошло проверки кода."""

    finding_ids: tuple[str, ...]
    origin: QuestionOrigin
    addressee: tuple[Addressee, ...]
    topic: str
    quotes: tuple[Quote, ...]
    question: str
    why: str
    options: tuple[QuestionOption, ...]
    if_unanswered: str
    candidate_ids: tuple[str, ...] = ()

    @property
    def position(self) -> int:
        return min((q.anchor.char_start for q in self.quotes if q.anchor), default=1 << 30)


@dataclass
class CheckLog:
    """Сколько раз код поправил редактора: из этого собирается одна строка диагностики."""

    invalid: int = 0
    unknown_candidates: int = 0
    no_findings: int = 0
    few_options: int = 0
    duplicates: int = 0
    over_limit: int = 0
    quotes_removed: int = 0
    quotes_replaced: int = 0
    options_removed: int = 0
    options_trimmed: int = 0
    ignored: int = 0
    rescued: int = 0
    links_removed: int = 0

    def problems(self) -> list[str]:
        labels = (
            (self.invalid, "карточек не по схеме или без вопроса"),
            (self.no_findings, "карточек без связанных находок"),
            (self.few_options, "карточек меньше чем с двумя вариантами"),
            (self.duplicates, "повторов слито с другими вопросами"),
            (self.over_limit, f"вопросов сверх лимита {MAX_QUESTIONS}"),
            (self.unknown_candidates, "ссылок на несуществующих кандидатов"),
            (self.quotes_removed, "несверенных цитат убрано"),
            (self.quotes_replaced, "карточек, где цитаты взяты из связанных находок"),
            (self.options_removed, "вариантов без последствия или повторных убрано"),
            (self.options_trimmed, f"карточек, где вариантов было больше {MAX_OPTIONS}"),
            (self.ignored, "кандидатов редактор не взял и не отклонил"),
            (self.rescued, "из них блокируют разработку и собраны кодом без редактуры"),
            (self.links_removed, "связей с находками без общей цитаты или требования убрано"),
        )
        return [f"{text}: {count}" for count, text in labels if count]


# ---------------------------------------------------------------- проверки карточки редактора


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _quotes(raw: Sequence[str], doc: ParsedDocument, within: Sequence[str], log: CheckLog) -> list[Quote]:
    out: list[Quote] = []
    seen: set[tuple[int, int] | None] = set()
    for text in raw:
        if not text.strip():
            continue
        found = verify_quote(doc, text, within)
        if found is None:
            log.quotes_removed += 1
            continue
        if span(found) not in seen:
            seen.add(span(found))
            out.append(found)
    return out[:MAX_QUOTES]


def _grounding(raw: str | None, doc: ParsedDocument, within: Sequence[str]) -> str | None:
    found = verify_quote(doc, raw, within) if raw else None
    return found.located if found is not None else None


def _options(
    raw: Sequence[EditorOption], doc: ParsedDocument, within: Sequence[str], log: CheckLog
) -> list[QuestionOption]:
    kept: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for option in raw:
        text, change = _clean(option.text), _clean(option.change)
        key = normalize(text)[0].lower()
        if not text or not change or key in seen:
            log.options_removed += 1
            continue
        seen.add(key)
        kept.append((text, change, _grounding(option.grounded_in, doc, within)))
    if len(kept) > MAX_OPTIONS:
        log.options_trimmed += 1
        kept = kept[:MAX_OPTIONS]
    return [
        QuestionOption(label=OPTION_LABELS[i], text=text, change=change, grounded_in=grounded)
        for i, (text, change, grounded) in enumerate(kept)
    ]


def _addressees(raw: Sequence[str]) -> tuple[Addressee, ...]:
    found = (_ADDRESSEE_ALIASES.get(_clean(a).lower()) for a in raw)
    unique = list(dict.fromkeys(a for a in found if a is not None))
    return tuple(unique[:MAX_ADDRESSEES]) or (Addressee.ANALYST,)


def _strongest(origins: Sequence[QuestionOrigin]) -> QuestionOrigin:
    return min(origins, key=ORIGIN_ORDER.index, default=QuestionOrigin.FINDING)


def _related(cands: Sequence[Candidate]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(fid for c in cands for fid in c.finding_ids))


def draft_from_card(
    card: EditorCard, by_id: Mapping[str, Candidate], doc: ParsedDocument, log: CheckLog
) -> CardDraft | None:
    """Карточка редактора после проверок кода или None (причина учтена в ``log``)."""
    ids = [normalize_candidate_id(c) for c in card.candidates]
    cands = [by_id[i] for i in dict.fromkeys(ids) if i is not None and i in by_id]
    log.unknown_candidates += sum(1 for i in ids if i is None or i not in by_id)
    question = _clean(card.question)
    if not question:
        log.invalid += 1
        return None
    if not cands:
        log.no_findings += 1
        return None
    within = list(dict.fromkeys(b for c in cands for b in c.block_ids))
    options = _options(card.options, doc, within, log)
    if len(options) < MIN_OPTIONS:
        log.few_options += 1
        return None
    quotes = _quotes(card.quotes, doc, within, log)
    if not quotes:
        log.quotes_replaced += 1
        quotes = _candidate_quotes(cands)
    return CardDraft(
        finding_ids=_related(cands),
        origin=_strongest([o for c in cands for o in c.origins]),
        addressee=_addressees(card.addressee),
        topic=_clean(card.topic),
        quotes=tuple(quotes),
        question=question,
        why=_clean(card.why),
        options=tuple(options),
        if_unanswered=_clean(card.if_unanswered),
        candidate_ids=tuple(c.cid for c in cands),
    )


def _candidate_quotes(cands: Sequence[Candidate]) -> list[Quote]:
    by_span = {span(q): q for c in cands for q in c.quotes}
    return sorted(by_span.values(), key=lambda q: q.anchor.char_start if q.anchor else 0)[:MAX_QUOTES]


# ---------------------------------------------------------------- отбор, приоритет, номера


def _similar(a: CardDraft, b: CardDraft) -> bool:
    """Повтор: почти та же формулировка или те же места документа с той же темой.

    ``token_sort_ratio``, а не ``token_set_ratio``: у второго короткий вопрос, слова которого целиком
    входят в длинный, даёт 100, и разные вопросы с общим началом склеились бы.
    """
    text_a, text_b = normalize(a.question)[0].lower(), normalize(b.question)[0].lower()
    if fuzz.token_sort_ratio(text_a, text_b) >= DUPLICATE_SIMILARITY:
        return True
    same_places = bool(a.quotes) and {span(q) for q in a.quotes} == {span(q) for q in b.quotes}
    same_topic = bool(a.topic) and normalize(a.topic)[0].lower() == normalize(b.topic)[0].lower()
    return same_places and same_topic


def _merge_duplicates(drafts: Sequence[CardDraft], log: CheckLog) -> list[CardDraft]:
    """Повтор сливается с первой похожей карточкой: её текст остаётся, находки объединяются."""
    kept: list[CardDraft] = []
    for draft in drafts:
        twin = next((i for i, k in enumerate(kept) if _similar(k, draft)), None)
        if twin is None:
            kept.append(draft)
            continue
        log.duplicates += 1
        base = kept[twin]
        merged = tuple(dict.fromkeys(base.finding_ids + draft.finding_ids))
        kept[twin] = replace(
            base,
            finding_ids=merged,
            origin=_strongest([base.origin, draft.origin]),
            candidate_ids=tuple(dict.fromkeys(base.candidate_ids + draft.candidate_ids)),
        )
    return kept


def _finding_number(fid: str) -> int:
    digits = fid.rpartition("-")[2]
    return int(digits) if digits.isdigit() else 1 << 30


class _Ranker:
    """Приоритет и ценность карточки по её находкам; ключ сортировки."""

    def __init__(self, findings: Mapping[str, Finding]) -> None:
        self.findings = findings

    def related(self, draft: CardDraft) -> list[Finding]:
        return [self.findings[i] for i in draft.finding_ids if i in self.findings]

    def key(self, draft: CardDraft) -> tuple[int, int, int, int, str]:
        return (*rank_key(self.related(draft)), draft.position, draft.question)

    def question(self, draft: CardDraft, number: int) -> Question:
        related = sorted(self.related(draft), key=lambda f: _finding_number(f.id))
        p = priority(related)
        return Question(
            id=f"Q-{number:02d}",
            priority=p,
            blocks=blocks(p),
            addressee=list(draft.addressee),
            topic=draft.topic,
            quotes=list(draft.quotes),
            question=draft.question,
            why=draft.why,
            options=list(draft.options),
            if_unanswered=draft.if_unanswered,
            related_findings=[f.id for f in related],
            origin=draft.origin,
            standard=standard_refs(related),
        )


def _touches(f: Finding, quotes: Sequence[Quote]) -> bool:
    """Цитата находки пересекается с местом документа, о котором вопрос."""
    places = [p for p in (span(q) for q in quotes) if p is not None]
    found = [p for p in (span(q) for q in f.quotes if q.verified) if p is not None]
    return any(s1 < e2 and s2 < e1 for s1, e1 in places for s2, e2 in found)


def _linked(draft: CardDraft, findings: Mapping[str, Finding], log: CheckLog) -> CardDraft:
    """Оставить связи «вопрос → находка», которые ответ действительно закрывает (#122).

    Опора вопроса это находки, чья цитата пересекается с его местами. Остальная находка остаётся, только
    если у неё с опорой общее требование или то же решение автора (правило склейки кандидатов). Редактор
    и слияние повторов объединяют кандидатов, и без этой проверки вопрос об одном правиле «закрывал» бы
    находку о другом месте документа, которое ответ не меняет. Нет опоры (места вопроса не совпали ни
    с одной находкой): судить не по чему, связи остаются как есть.
    """
    related = [findings[i] for i in draft.finding_ids if i in findings]
    anchors = [f for f in related if _touches(f, draft.quotes)]
    if not anchors:
        return draft
    reqs = {r for f in anchors for r in f.requirement_ids}
    keep = tuple(
        f.id
        for f in related
        if f in anchors or reqs & set(f.requirement_ids) or any(same_decision(f, a) for a in anchors)
    )
    log.links_removed += len(related) - len(keep)
    return replace(draft, finding_ids=keep) if len(keep) < len(related) else draft


def finalize(
    drafts: Sequence[CardDraft],
    findings: Mapping[str, Finding],
    log: CheckLog,
    *,
    merge_similar: bool = True,
) -> list[Question]:
    """Слить повторы, закрыть каждую находку одним вопросом, отсортировать, обрезать, пронумеровать.

    ``merge_similar=False`` для шаблонных карточек запасного пути: у них общий шаблон формулировки,
    поэтому сходство текста ничего не говорит; повторы там снимает правило «одна находка, один вопрос».
    """
    ranker = _Ranker(findings)
    unique = _merge_duplicates(drafts, log) if merge_similar else list(drafts)
    ranked = sorted((_linked(d, findings, log) for d in unique), key=ranker.key)
    kept: list[CardDraft] = []
    claimed: set[str] = set()
    for draft in ranked:
        free = tuple(i for i in draft.finding_ids if i in findings and i not in claimed)
        if not free:
            log.duplicates += 1
            continue
        claimed.update(free)
        kept.append(replace(draft, finding_ids=free))
    kept.sort(key=ranker.key)  # снятые находки могли понизить приоритет
    if len(kept) > MAX_QUESTIONS:
        log.over_limit += len(kept) - MAX_QUESTIONS
        kept = kept[:MAX_QUESTIONS]
    return [ranker.question(d, n) for n, d in enumerate(kept, start=1)]
