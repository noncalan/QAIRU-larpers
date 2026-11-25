"""Шаг 3: линтер правил без модели.

Контракт: ``lint(doc, seg) -> list[CandidateFinding]``. Десять правил L-01…L-10 (каталог ``rules.py``,
словари ``lexicon.yaml``) ищут поверхностные дефекты: размытые слова, лазейки, открытые перечни,
«и/или», маркеры незавершённости, числа без единиц, битые ссылки, повторы и пропуски ID, составные
обязательства. Модель не вызывается; находки детерминированы и объяснимы, цитату берёт код из документа.

Правила срабатывают только внутри требований нарезки (``seg``): заголовки, пояснения, примеры и
шаблонные таблицы без пометки «требование» пропускаются. Важность линтер не ставит: её считает
верификатор. Только общий язык ТЗ: слов проверяемого документа здесь быть не должно
(CLAUDE.md, правила 2 и 3; это проверяет ``tests/test_prompt_leak.py``).
"""

from __future__ import annotations

from validator.lint.doc_rules import DOC_CHECKS, DocContext
from validator.lint.hits import Hit
from validator.lint.rules import RULES, Rule
from validator.lint.text_rules import UNIT_CHECKS
from validator.lint.units import build_units
from validator.models import CandidateFinding, FindingSource, ParsedDocument, Segmentation, SourceKind


def run_rules(doc: ParsedDocument, seg: Segmentation) -> list[Hit]:
    """Все срабатывания по порядку документа, без превращения в находки (для тестов и отладки)."""
    units = build_units(doc, seg)
    hits: list[Hit] = []
    for unit in units:
        for check in UNIT_CHECKS.values():
            hit = check(unit, doc.text)
            if hit is not None:
                hits.append(hit)
    ctx = DocContext(doc=doc, units=units)
    for doc_check in DOC_CHECKS.values():
        hits.extend(doc_check(ctx))
    return sorted(hits, key=lambda h: h.order)


def _candidate(doc: ParsedDocument, hit: Hit, number: int) -> CandidateFinding:
    rule = hit.rule
    block_ids: list[str] = []
    for start, end in hit.spans:
        for block in doc.blocks:
            if block.anchor.char_start < end and start < block.anchor.char_end and block.id not in block_ids:
                block_ids.append(block.id)
    return CandidateFinding(
        cid=f"lint.{rule.id}.{number:03d}",
        source=FindingSource(kind=SourceKind.LINT, rule_id=rule.id),
        property=rule.criterion,
        defect_type=rule.defect_type,
        verdict=rule.verdict,
        scope=rule.scope,
        block_ids=block_ids,
        quotes=[doc.text[start:end] for start, end in hit.spans],
        explanation=hit.explanation,
        suggestion=hit.suggestion,
        blocks_testing=rule.blocks_testing,
        title=hit.title,
    )


def lint(doc: ParsedDocument, seg: Segmentation) -> list[CandidateFinding]:
    """Находки правил L-01…L-10 внутри требований нарезки, по порядку документа."""
    counters: dict[str, int] = {}
    findings: list[CandidateFinding] = []
    for hit in run_rules(doc, seg):
        counters[hit.rule.id] = counters.get(hit.rule.id, 0) + 1
        findings.append(_candidate(doc, hit, counters[hit.rule.id]))
    return findings


__all__ = ["RULES", "Hit", "Rule", "lint", "run_rules"]
