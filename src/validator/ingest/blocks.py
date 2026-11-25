"""Блоки документа с якорями и вид документа для модели (шаг 1, общий для MD, PDF и DOCX).

Порт ``experiments/18-segmentation/preseg.py``: заголовки ведут стек разделов, абзацы, пункты списков,
строки таблиц и блоки кода получают ID ``B001``, предложения ``B001.2`` (razdel). Каждый блок
это дословный фрагмент канонического текста ``"\\n".join(line.text for line in lines)``, поэтому
смещения якорей указывают в ``ParsedDocument.text``.

Разборщики форматов (``ingest/md.py``, ``pdf.py``, ``docx.py``) отдают строки ``SourceLine``.
Для MD разметку (``#``, ``|``, списки, ``````) распознаёт этот модуль, для PDF и DOCX структуру
передают поля строки (``heading_level``, ``table``, ``cells``, ``kind``).

Меняем только добавлением и с тестами: модуль общий для #25 и #26.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from razdel import sentenize

from validator.models import (
    Anchor,
    Block,
    BlockKind,
    Diagnostic,
    DocKind,
    ParsedDocument,
    Sentence,
    SourceLine,
    TableCell,
    TextSignals,
)

HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[*+•-]|\d+[.)])\s+")
ORDINAL = re.compile(r"^\s*(\d+)[.)]\s+")
FENCE = re.compile(r"^\s*(```|~~~)")
HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
# Метка требования в начале пункта: «**ABC-1.**», «REQ-12:», «FR.3», «ТР-2.1)». Буквы 1–6, разделитель, номер.
SOURCE_ID = re.compile(
    r"^\s*(?:[*+-]\s+|\d+[.)]\s+)?(?:\*\*|__)?\s*"
    r"(?P<id>[A-Za-zА-Яа-яЁё]{1,6}[-‑.‐_ ]?\d+(?:\.\d+)*)"
    r"\s*[.:)]?\s*(?:\*\*|__)?[.:)]?\s+"
)
# Жирная числовая метка «**200.**» — номер пункта или код статуса, а не ID требования.
BOLD_NUM = re.compile(r"^\s*\*\*\d{3}\.?\*\*")
# Общие признаки обязательства (подсказка нарезке, не решение). Только общий язык ТЗ, без слов документа.
OBLIGATION = re.compile(
    r"\b(должн\w*|обязан\w*|необходимо|требуется|не допускается|запрещ\w*|не более|не менее|"
    r"shall|must|should|required)\b",
    re.IGNORECASE,
)
LIST_KINDS = frozenset({BlockKind.PARAGRAPH, BlockKind.LIST_ITEM})


@dataclass(frozen=True)
class ParsedLines:
    """Что отдаёт разборщик формата: строки и сведения для паспорта и диагностики."""

    lines: list[SourceLine]
    extraction: str
    diagnostics: list[Diagnostic] = field(default_factory=list)
    pages: int | None = None
    signals: TextSignals | None = None


def canonical_text(lines: Sequence[SourceLine]) -> str:
    return "\n".join(line.text for line in lines)


_EMPHASIS = re.compile(r"[*`]|(?<!\w)_+|_+(?!\w)")


def _plain(text: str) -> str:
    """Убрать разметку выделения, но не подчёркивания внутри слов (``user_id``)."""
    return _EMPHASIS.sub("", text).strip()


def _md_cells(line: str) -> list[str]:
    body = line.strip()
    body = body.removeprefix("|").removesuffix("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", body)]


@dataclass
class _Builder:
    lines: Sequence[SourceLine]
    source: DocKind
    starts: list[int] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    section: list[tuple[int, str]] = field(default_factory=list)
    header: list[str] | None = None
    table_no: int = 0
    table_row: int = 0
    in_table: bool = False
    full: str = ""

    def __post_init__(self) -> None:
        self.full = canonical_text(self.lines)
        offset = 0
        for line in self.lines:
            self.starts.append(offset)
            offset += len(line.text) + 1

    # ---------------------------------------------------------------- распознавание строк

    @property
    def is_md(self) -> bool:
        return self.source is DocKind.MD

    def text(self, i: int) -> str:
        return self.lines[i].text

    def heading_level(self, i: int) -> int | None:
        line = self.lines[i]
        if line.heading_level:
            return line.heading_level
        m = HEADING.match(line.text) if self.is_md else None
        return len(m.group(1)) if m else None

    def is_table(self, i: int) -> bool:
        line = self.lines[i]
        return line.table is not None or (self.is_md and bool(TABLE_ROW.match(line.text)))

    def is_fence(self, i: int) -> bool:
        return self.is_md and bool(FENCE.match(self.text(i)))

    def is_list(self, i: int) -> bool:
        return bool(LIST_ITEM.match(self.text(i)))

    def is_special(self, i: int) -> bool:
        return self.lines[i].kind is not None

    def breaks_paragraph(self, i: int) -> bool:
        return (
            not self.text(i).strip()
            or self.heading_level(i) is not None
            or self.is_table(i)
            or self.is_list(i)
            or self.is_fence(i)
            or self.is_special(i)
            or (self.is_md and bool(HR.match(self.text(i))))
        )

    # ---------------------------------------------------------------- сборка блока

    def span(self, first: int, last: int) -> tuple[int, int]:
        """Смещения фрагмента строк first..last без отступов по краям."""
        head = self.text(first)
        start = self.starts[first] + len(head) - len(head.lstrip())
        end = self.starts[last] + len(self.text(last).rstrip())
        return start, max(start, end)

    def add(
        self,
        kind: BlockKind,
        first: int,
        last: int,
        *,
        table: int | None = None,
        row: int | None = None,
        serialized: str = "",
        cells: list[TableCell] | None = None,
    ) -> Block:
        start, end = self.span(first, last)
        line = self.lines[first]
        bid = f"B{len(self.blocks) + 1:03d}"
        anchor = Anchor(
            char_start=start,
            char_end=end,
            block_ids=[bid],
            line_start=first + 1,
            line_end=last + 1,
            page=line.page,
            paragraph=line.paragraph,
            table=table,
            row=row,
            extraction=line.extraction,
        )
        block = Block(
            id=bid,
            kind=kind,
            text=self.full[start:end],
            anchor=anchor,
            section=[title for _, title in self.section],
            serialized=serialized,
            cells=cells or [],
        )
        if kind in LIST_KINDS:
            block = self.enrich(block, first, last)
        self.blocks.append(block)
        return block

    def enrich(self, block: Block, first: int, last: int) -> Block:
        """Метка требования, номер пункта, предложения и подсказки для абзацев и пунктов списка."""
        sm = SOURCE_ID.match(block.text)
        source_id = sm.group("id").replace("‑", "-") if sm and not BOLD_NUM.match(block.text) else None
        om = ORDINAL.match(block.text) if block.kind is BlockKind.LIST_ITEM else None
        sentences = self.sentences(block.id, first, last, block.kind)
        hints = ["obligation"] if OBLIGATION.search(block.text) else []
        if source_id:
            hints.append("source_id")
        return block.model_copy(
            update={
                "source_id": source_id,
                "ordinal": int(om.group(1)) if om else None,
                "sentences": sentences,
                "hints": hints,
            }
        )

    def sentences(self, bid: str, first: int, last: int, kind: BlockKind) -> list[Sentence]:
        out: list[Sentence] = []
        for i in range(first, last + 1):
            raw = self.text(i)
            shift = 0
            if i == first and kind is BlockKind.LIST_ITEM:
                m = LIST_ITEM.match(raw)
                shift = m.end() if m else 0
            for piece in sentenize(raw[shift:]):
                if not piece.text.strip():
                    continue
                start = self.starts[i] + shift + piece.start
                out.append(
                    Sentence(
                        id=f"{bid}.{len(out) + 1}",
                        text=piece.text,
                        char_start=start,
                        char_end=start + len(piece.text),
                        line=i + 1,
                    )
                )
        return out

    # ---------------------------------------------------------------- проход по строкам

    def run(self) -> list[Block]:
        i, n = 0, len(self.lines)
        while i < n:
            i = self.step(i, n)
        return self.blocks

    def step(self, i: int, n: int) -> int:
        text = self.text(i)
        if not self.is_table(i):
            self.in_table = False
            if self.is_md:
                self.header = None
        if not text.strip() or (self.is_md and HR.match(text)):
            return i + 1
        level = self.heading_level(i)
        if level is not None:
            return self.heading(i, level)
        if self.is_special(i):
            kind = self.lines[i].kind or BlockKind.PARAGRAPH
            self.add(kind, i, i)
            return i + 1
        if self.is_fence(i):
            return self.fence(i, n)
        if self.is_table(i):
            return self.table(i, n)
        if self.is_list(i):
            return self.list_item(i, n)
        j = i + 1
        while j < n and not self.breaks_paragraph(j):
            j += 1
        self.add(BlockKind.PARAGRAPH, i, j - 1)
        return j

    def heading(self, i: int, level: int) -> int:
        raw = self.text(i)
        m = HEADING.match(raw) if self.is_md else None
        title = _heading_title(raw) if m else _plain(raw)
        self.section = [(lv, t) for lv, t in self.section if lv < level] + [(level, title)]
        self.add(BlockKind.HEADING, i, i)
        return i + 1

    def fence(self, i: int, n: int) -> int:
        j = i + 1
        while j < n and not FENCE.match(self.text(j)):
            j += 1
        body_last = min(j, n) - 1
        if body_last >= i + 1 and any(self.text(k).strip() for k in range(i + 1, body_last + 1)):
            first = next(k for k in range(i + 1, body_last + 1) if self.text(k).strip())
            last = max(k for k in range(i + 1, body_last + 1) if self.text(k).strip())
            self.add(BlockKind.CODE, first, last)
        return j + 1

    def list_item(self, i: int, n: int) -> int:
        j = i + 1
        while (
            self.is_md
            and j < n
            and self.text(j).strip()
            and self.text(j).startswith(("  ", "\t"))
            and not self.is_list(j)
        ):
            j += 1
        self.add(BlockKind.LIST_ITEM, i, j - 1)
        return j

    def table(self, i: int, n: int) -> int:
        line = self.lines[i]
        if not self.in_table or (line.table is not None and line.table != self.table_no):
            self.in_table = True
            self.table_no = line.table if line.table is not None else self.table_no + 1
            self.table_row = 0
            self.header = None
        if self.is_md and TABLE_SEP.match(line.text):
            return i + 1
        cells = list(line.cells) if line.table is not None else _md_cells(line.text)
        is_header = line.header or (
            self.is_md and self.header is None and i + 1 < n and bool(TABLE_SEP.match(self.text(i + 1)))
        )
        if is_header:
            self.header = [_plain(c) for c in cells]
            return i + 1
        if not any(_plain(c) for c in cells):
            return i + 1
        self.table_row += 1
        hdr = self.header or [f"col{k + 1}" for k in range(len(cells))]
        serialized = "; ".join(f"{h}: {_plain(c)}" for h, c in zip(hdr, cells, strict=False) if _plain(c))
        start = self.starts[i]
        table_cells = _locate_cells(line.text, cells, hdr, start)
        self.add(
            BlockKind.TABLE_ROW,
            i,
            i,
            serialized=serialized,
            cells=table_cells,
            table=self.table_no,
            row=self.table_row,
        )
        return i + 1


def _locate_cells(text: str, cells: Sequence[str], header: Sequence[str], base: int) -> list[TableCell]:
    """Ячейки строки таблицы со своими смещениями: ищем текст ячейки слева направо."""
    out: list[TableCell] = []
    cursor = 0
    for k, cell in enumerate(cells):
        value = cell.strip()
        if not value:
            continue
        pos = text.find(value, cursor)
        if pos < 0:
            continue
        cursor = pos + len(value)
        column = header[k] if k < len(header) else f"col{k + 1}"
        out.append(
            TableCell(index=k + 1, column=column, text=value, char_start=base + pos, char_end=base + cursor)
        )
    return out


def build_blocks(lines: Sequence[SourceLine], source: DocKind) -> list[Block]:
    """Режет строки на блоки с ID, якорями, разделами и предложениями. Детерминированно."""
    return _Builder(lines, source).run()


def make_document(path: Path, kind: DocKind, parsed: ParsedLines) -> ParsedDocument:
    """Собирает ``ParsedDocument`` из строк разборщика: канонический текст, блоки, хеш и размер."""
    data = path.read_bytes()
    return ParsedDocument(
        path=str(path),
        name=path.name,
        kind=kind,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        text=canonical_text(parsed.lines),
        blocks=build_blocks(parsed.lines, kind),
        pages=parsed.pages,
        extraction=parsed.extraction,
        signals=parsed.signals,
        diagnostics=list(parsed.diagnostics),
    )


# ---------------------------------------------------------------- документ для модели


def _where(block: Block) -> str:
    a = block.anchor
    parts = []
    if a.page is not None:
        parts.append(f"стр. {a.page}")
    if a.line_start is not None:
        same = a.line_end in (None, a.line_start)
        parts.append(f"строка {a.line_start}" if same else f"строки {a.line_start}–{a.line_end}")
    return ", ".join(parts)


def _header(block: Block) -> str:
    parts = [f"[{block.id}]"]
    head = []
    if block.section:
        head.append("§" + " › ".join(block.section))
    kind = block.kind.label
    if block.kind is BlockKind.TABLE_ROW and block.anchor.row is not None:
        kind = f"{kind} {block.anchor.row}"
    head.append(kind)
    where = _where(block)
    if where:
        head.append(where)
    if block.source_id:
        head.append(f"метка: {block.source_id}")
    parts.append(" | ".join(head))
    return " ".join(parts)


def _heading_title(text: str) -> str:
    m = HEADING.match(text)
    return _plain(m.group(2) if m else text)


def render_for_model(doc: ParsedDocument) -> str:
    """Документ с ID блоков и предложений для модели (исследование 18, п. 3.2).

    Модель ссылается на ``B…`` и ``B….N``, а цитату по ним подставляет код. Текст документа здесь
    это данные, а не инструкции: обрамление и предупреждение об этом добавляет промпт шага.
    """
    out: list[str] = []
    for block in doc.blocks:
        out.append(_header(block))
        if block.kind is BlockKind.HEADING:
            out.append(f"  {_heading_title(block.text)}")
        elif block.kind is BlockKind.TABLE_ROW:
            out.append(f"  {block.serialized}")
        elif block.sentences:
            out.extend(f"  [{s.id}] {s.text}" for s in block.sentences)
        else:
            out.extend(f"  {line}" for line in block.text.splitlines() if line.strip())
    return "\n".join(out) + "\n"
