"""ID блоков в текстах моделей → номера требований и места в документе (issue #36).

Модели видят документ с ID блоков (``B012``, ``B012.3``) и иногда пишут их в объяснение находки,
предложенную формулировку или вариант ответа на вопрос автору. В отчёте ID блоков нет, и читатель их
не найдёт. Код заменяет такой ID на номера требований из таблицы нарезки (``R-07``, ``R-07.2``), а если
фрагмент не входит ни в одно требование, на место в документе: «фрагмент в разделе «…» (строка 31)».

Цитаты не трогаем: они дословно из документа, и строка вида ``B012`` в них это текст документа.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from validator.models import (
    Block,
    BlockKind,
    Finding,
    ParsedDocument,
    Question,
    QuestionOption,
    Segmentation,
    Verification,
    WithdrawnQuestion,
)

_ID = r"B\d{3,}(?:\.\d+)?"
BLOCK_REF = re.compile(rf"(?<![\w.\-‑]){_ID}(?:\s*[-–‑]\s*{_ID})?(?![\w\-‑])")
"""ID блока или предложения, как его видит модель: B012, B012.3 или диапазон B012-B015. Не часть
слова или кода вроде «X-B012» и «B012-A»."""
_ONE_ID = re.compile(_ID)

MAX_IDS_SHOWN = 3
"""Сколько R-NN перечислить вместо одного ID блока, остальные «и ещё N»."""

FINDING_TEXT_FIELDS = ("title", "explanation", "suggestion", "scenario", "test_gap")
QUESTION_TEXT_FIELDS = ("topic", "question", "why", "if_unanswered")
OPTION_TEXT_FIELDS = ("text", "change", "grounded_in")
WITHDRAWN_TEXT_FIELDS = ("reason",)


class RefNamer:
    """Знает, в какие требования входит каждый блок и предложение документа и где лежит блок."""

    def __init__(self, doc: ParsedDocument, seg: Segmentation | None) -> None:
        self._blocks: dict[str, Block] = {b.id: b for b in doc.blocks}
        # Код вида B250 из самого документа (тариф, артикул) это не ссылка модели на блок.
        self._in_document = set(re.findall(rf"(?<![\w.]){_ID}(?!\w)", doc.text))
        self._by_ref: dict[str, list[str]] = {}
        requirements = seg.requirements if seg is not None else []
        for req in requirements:
            for ref in [*req.anchor.block_ids, *req.anchor.sentence_ids]:
                ids = self._by_ref.setdefault(ref, [])
                if req.id not in ids:
                    ids.append(req.id)
        # Как ``verify.requirement_ids``: если совпали и атом, и его родитель, остаётся атом.
        parent = {r.id: r.parent_id for r in requirements}
        for ref, ids in self._by_ref.items():
            parents = {parent[i] for i in ids if parent.get(i)}
            self._by_ref[ref] = [i for i in ids if i not in parents]

    def name(self, ref: str) -> str | None:
        """Понятное читателю имя для ID блока или предложения; None, если это не ссылка на блок."""
        if ref in self._in_document:
            return None
        block = self._blocks.get(ref.split(".", 1)[0])
        if block is None:
            return None
        ids = self._by_ref.get(ref, [])
        if ids:
            shown = ", ".join(ids[:MAX_IDS_SHOWN])
            rest = len(ids) - MAX_IDS_SHOWN
            return f"{shown} и ещё {rest}" if rest > 0 else shown
        return _place(block)

    def text(self, value: str) -> str:
        return BLOCK_REF.sub(
            lambda m: _ONE_ID.sub(lambda r: self.name(r.group(0)) or r.group(0), m.group(0)), value
        )


def _place(block: Block) -> str:
    """«раздел «A › B»» для заголовка, «фрагмент в разделе «A › B» (стр. 2)» для остального."""
    if block.kind is BlockKind.HEADING:
        title = block.text.lstrip("#").strip()
        return f"раздел «{title}»"
    anchor = block.anchor
    if anchor.page is not None:
        where = f"стр. {anchor.page}"
    elif anchor.line_start is not None:
        where = f"строка {anchor.line_start}"
    else:
        where = ""
    section = f"в разделе «{' › '.join(block.section[-2:])}»" if block.section else "в документе"
    return f"фрагмент {section}" + (f" ({where})" if where else "")


def _replace(
    model: Finding | Question | QuestionOption | WithdrawnQuestion, fields: Sequence[str], namer: RefNamer
) -> dict[str, object]:
    changes: dict[str, object] = {}
    for name in fields:
        value = getattr(model, name)
        if isinstance(value, str):
            fixed = namer.text(value)
            if fixed != value:
                changes[name] = fixed
    return changes


def humanize_finding(finding: Finding, namer: RefNamer) -> Finding:
    changes = _replace(finding, FINDING_TEXT_FIELDS, namer)
    return finding.model_copy(update=changes) if changes else finding


def humanize_question(question: Question, namer: RefNamer) -> Question:
    changes = _replace(question, QUESTION_TEXT_FIELDS, namer)
    options = [o.model_copy(update=_replace(o, OPTION_TEXT_FIELDS, namer)) for o in question.options]
    if options != question.options:
        changes["options"] = options
    return question.model_copy(update=changes) if changes else question


def humanize_withdrawn(withdrawn: WithdrawnQuestion, namer: RefNamer) -> WithdrawnQuestion:
    """Снятый вопрос: текст карточки и ответ критика; цитата-ответ остаётся дословной."""
    changes = _replace(withdrawn, WITHDRAWN_TEXT_FIELDS, namer)
    question = humanize_question(withdrawn.question, namer)
    if question is not withdrawn.question:
        changes["question"] = question
    return withdrawn.model_copy(update=changes) if changes else withdrawn


def humanize(
    ver: Verification | None,
    questions: Sequence[Question],
    withdrawn: Sequence[WithdrawnQuestion],
    doc: ParsedDocument,
    seg: Segmentation | None,
) -> tuple[Verification | None, list[Question], list[WithdrawnQuestion]]:
    """Находки, вопросы и снятые вопросы без ID блоков в текстах: R-NN или место в документе вместо B012."""
    namer = RefNamer(doc, seg)
    if ver is not None:
        ver = ver.model_copy(update={"findings": [humanize_finding(f, namer) for f in ver.findings]})
    return (
        ver,
        [humanize_question(q, namer) for q in questions],
        [humanize_withdrawn(w, namer) for w in withdrawn],
    )


__all__ = ["BLOCK_REF", "RefNamer", "humanize", "humanize_finding", "humanize_question", "humanize_withdrawn"]
