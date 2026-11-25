"""Шаг 8а: кандидаты в вопросы автору собирает код, без вызова модели (исследование 17, п. 4.1, 3.4).

Модели сами почти не спрашивают (исследование 17, TL;DR п. 1), поэтому вопрос порождает не «желание»
модели, а признаки, которые уже есть у находок верификатора:

- вердикт «неясно»: модель не уверена, что это дефект, значит это вопрос, а не нарушение;
- предложенная формулировка с местами ``[уточнить: …]``: исправление требует выбора автора;
- находка мешает написать тест-кейс (``blocks_testing``) или в черновике теста остались пропуски;
- противоречие между требованиями (тип «противоречие» или свойство набора Consistent), пример-свидетель.

Однозначные правки (без «неясно», без ``[уточнить]``, не мешают тесту, не противоречие) остаются
находками без вопроса. Посторонняя инструкция в документе вопросом автору не становится.

Склейка консервативная, чтобы не задавать один вопрос дважды и не склеить два разных решения в одно:
находки попадают в один кандидат, если их цитаты пересекаются в документе и тема одна (тип дефекта
или свойство), или если у них общее требование и похожие места ``[уточнить: …]``. Дубликаты по смыслу
дальше сливает редактор вопросов.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from rapidfuzz import fuzz

from validator.models import (
    DefectType,
    Finding,
    QuestionOrigin,
    Quote,
    SourceKind,
    Verdict,
    Verification,
)
from validator.questions.scoring import draft_gaps, is_contradiction, requirement_ids

CLARIFY = re.compile(r"\[\s*уточнить\b\s*:?\s*([^\]]*)\]", re.IGNORECASE)
"""Плейсхолдер в предложенной формулировке: ``[уточнить: что именно]``."""

PLACEHOLDER_SIMILARITY = 85.0
"""Похожие места ``[уточнить]`` у находок одного требования это один вопрос (rapidfuzz token_set_ratio)."""

MIN_PLACEHOLDER_CHARS = 8
"""Короткие плейсхолдеры («N», «срок») похожи у всех, по ним не склеиваем."""

ORIGIN_ORDER: tuple[QuestionOrigin, ...] = (
    QuestionOrigin.WITNESS,
    QuestionOrigin.SET_CHECK,
    QuestionOrigin.UNCLEAR_VERDICT,
    QuestionOrigin.TEST_DRAFT,
    QuestionOrigin.CHECKLIST,
    QuestionOrigin.LINT,
    QuestionOrigin.FINDING,
)
"""Источники по силе: главный источник карточки это первый из них."""


_SOURCE_ORIGIN = {SourceKind.LINT: QuestionOrigin.LINT, SourceKind.CHECKLIST: QuestionOrigin.CHECKLIST}


@dataclass(frozen=True)
class Candidate:
    """Кандидат в вопрос: находки об одном решении автора, их сверенные цитаты и что неизвестно."""

    cid: str
    findings: tuple[Finding, ...]
    origins: tuple[QuestionOrigin, ...]
    quotes: tuple[Quote, ...]
    placeholders: tuple[str, ...]

    @property
    def finding_ids(self) -> tuple[str, ...]:
        return tuple(f.id for f in self.findings)

    @property
    def origin(self) -> QuestionOrigin:
        return self.origins[0]

    @property
    def requirement_ids(self) -> list[str]:
        return requirement_ids(self.findings)

    @property
    def block_ids(self) -> list[str]:
        """Блоки цитат: здесь код сначала ищет цитаты карточки, потом во всём документе."""
        return list(dict.fromkeys(b for q in self.quotes if q.anchor for b in q.anchor.block_ids))

    @property
    def position(self) -> int:
        return min((q.anchor.char_start for q in self.quotes if q.anchor), default=1 << 30)


def placeholders(f: Finding) -> tuple[str, ...]:
    """Содержимое мест ``[уточнить: …]`` в предложенной формулировке, без пустых."""
    if not f.suggestion:
        return ()
    found = (" ".join(m.group(1).split()) for m in CLARIFY.finditer(f.suggestion))
    return tuple(dict.fromkeys(p for p in found if p))


def finding_origins(f: Finding) -> tuple[QuestionOrigin, ...]:
    """Почему находка годится в вопрос; пусто значит, что это однозначная правка, а не вопрос."""
    if f.defect_type is DefectType.INJECTED_INSTRUCTION:
        return ()
    found: set[QuestionOrigin] = set()
    if f.witness is not None:
        found.add(QuestionOrigin.WITNESS)
    if is_contradiction(f):
        found.add(QuestionOrigin.SET_CHECK)
    if f.verdict is Verdict.UNCLEAR:
        found.add(QuestionOrigin.UNCLEAR_VERDICT)
    if draft_gaps(f):
        found.add(QuestionOrigin.TEST_DRAFT)
    has_clarify = bool(f.suggestion and CLARIFY.search(f.suggestion))
    if has_clarify or f.blocks_testing:
        kinds = [s.kind for s in f.sources]
        found.add(next((_SOURCE_ORIGIN[k] for k in kinds if k in _SOURCE_ORIGIN), QuestionOrigin.FINDING))
    return tuple(o for o in ORIGIN_ORDER if o in found)


def reasons(c: Candidate) -> list[str]:
    """Почему кандидат попал в вопросы, словами для редактора: по признакам его находок."""
    checks = (
        (any(f.witness is not None for f in c.findings), "пример-свидетель: один сценарий, разные ожидания"),
        (any(is_contradiction(f) for f in c.findings), "противоречие между требованиями"),
        (any(f.verdict is Verdict.UNCLEAR for f in c.findings), "вердикт «неясно»: не решить без автора"),
        (any(draft_gaps(f) for f in c.findings), "в черновике тест-кейса не хватает значений"),
        (bool(c.placeholders), "исправление требует выбора автора: места [уточнить]"),
        (any(f.blocks_testing for f in c.findings), "мешает написать тест-кейс"),
        (QuestionOrigin.LINT in c.origins, "правило линтера"),
        (QuestionOrigin.CHECKLIST in c.origins, "пункт доменного чек-листа"),
    )
    return [text for hit, text in checks if hit]


def _spans(f: Finding) -> list[tuple[int, int]]:
    return [(q.anchor.char_start, q.anchor.char_end) for q in f.quotes if q.verified and q.anchor]


def _overlap(a: Finding, b: Finding) -> bool:
    return any(s1 < e2 and s2 < e1 for s1, e1 in _spans(a) for s2, e2 in _spans(b))


def _similar_placeholders(a: Finding, b: Finding) -> bool:
    pa = [p for p in placeholders(a) if len(p) >= MIN_PLACEHOLDER_CHARS]
    pb = [p for p in placeholders(b) if len(p) >= MIN_PLACEHOLDER_CHARS]
    return any(fuzz.token_set_ratio(x.lower(), y.lower()) >= PLACEHOLDER_SIMILARITY for x in pa for y in pb)


def same_decision(a: Finding, b: Finding) -> bool:
    """Одно решение автора: то же место и та же тема, или то же требование и тот же пропуск."""
    same_topic = a.defect_type is b.defect_type or a.property is b.property
    if same_topic and _overlap(a, b):
        return True
    shared_requirement = bool(set(a.requirement_ids) & set(b.requirement_ids))
    return shared_requirement and _similar_placeholders(a, b)


def _clusters(findings: Sequence[Finding]) -> list[list[Finding]]:
    parent = list(range(len(findings)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(findings)):
        for j in range(i + 1, len(findings)):
            if same_decision(findings[i], findings[j]):
                parent[root(j)] = root(i)
    groups: dict[int, list[Finding]] = {}
    for i, f in enumerate(findings):
        groups.setdefault(root(i), []).append(f)
    return list(groups.values())


def _unique_quotes(findings: Iterable[Finding]) -> tuple[Quote, ...]:
    by_span: dict[tuple[int, int], Quote] = {}
    for f in findings:
        for q in f.quotes:
            if q.verified and q.anchor:
                by_span.setdefault((q.anchor.char_start, q.anchor.char_end), q)
    return tuple(by_span[k] for k in sorted(by_span))


def _candidate(group: Sequence[Finding]) -> Candidate:
    origins = {o for f in group for o in finding_origins(f)}
    return Candidate(
        cid="C-00",
        findings=tuple(group),
        origins=tuple(o for o in ORIGIN_ORDER if o in origins),
        quotes=_unique_quotes(group),
        placeholders=tuple(dict.fromkeys(p for f in group for p in placeholders(f))),
    )


def collect_candidates(ver: Verification) -> list[Candidate]:
    """Кандидаты по порядку в документе, ID ``C-01``…; находки внутри кандидата по порядку ``F-NN``."""
    eligible = [f for f in ver.findings if finding_origins(f)]
    built = [_candidate(group) for group in _clusters(eligible)]
    built.sort(key=lambda c: (c.position, c.finding_ids))
    return [replace(c, cid=f"C-{n:02d}") for n, c in enumerate(built, start=1)]
