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
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from razdel import sentenize

from validator.errors import IN_HEADINGS_ONLY, IN_NO_TEXT
from validator.models import (
    Anchor,
    Block,
    BlockKind,
    DiagLevel,
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
LIST_LABEL = re.compile(r"^\s*(?:[*+•-]|\(?(?:\d+(?:\.\d+)*|[A-Za-zА-Яа-яЁё]|[ivxlcdmIVXLCDM]+)[.)])\s+")
ORDINAL = re.compile(r"^\s*(\d+)[.)]\s+")
FENCE = re.compile(r"^\s*(?P<mark>`{3,}|~{3,})")
HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
QUOTE_LINE = re.compile(r"^\s{0,3}>[ \t]?")
SETEXT = re.compile(r"^\s{0,3}(?:(?P<h1>=+)|(?P<h2>-{2,}))\s*$")
# Таблица без внешних вертикальных черт: «Колонка | Колонка» и под ней «--- | ---» (GFM допускает).
PIPE_SEP = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?\s*$")
FRONT_MATTER_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*\s*:")
MAX_SETEXT_LINES = 2
MAX_SETEXT_CHARS = 200
CLOSING_HASHES = re.compile(r"\s+#+\s*$")
# Метка требования в начале пункта: «**ABC-1.**», «REQ-12:», «FR.3», «ТР-2.1)», «FR12». С заглавной буквы,
# 1–6 букв, затем дефис, точка или подчёркивание и номер; без разделителя только заглавные (FR12). Пробел
# разделителем не считаем: «В 2025 году», «До 10 попыток» это обычный текст, а не метка.
SOURCE_ID = re.compile(
    r"^\s*(?:[*+•-]\s+|\d+[.)]\s+|\d+(?:\.\d+)+[.)]?\s+)?(?:\*\*|__)?\s*"
    r"(?P<id>(?:[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё]{0,5}[-‑.‐_]|[A-ZА-ЯЁ]{1,6})\d+(?:\.\d+)*)"
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
SENTENCE_KINDS = frozenset({BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.QUOTE})
TEXT_KINDS = frozenset({BlockKind.HEADING, BlockKind.TABLE_ROW, *SENTENCE_KINDS})
"""Виды блоков с содержательным текстом документа: сноски, колонтитулы и код сюда не входят."""


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


EMPTY_CELL = "(пусто)"
"""Так модель видит пустую ячейку: незаполненное поле тоже факт документа (промпт поиска #14)."""
KEY_VALUE_HEADERS = frozenset({"поле", "field", "параметр", "parameter", "ключ", "key"})
"""Таблица «поле | значение» показывается модели как «Поле: значение», без названий колонок."""


def _clean_cell(cell: str) -> str:
    """Ячейка без жирного выделения; ``_`` и обратные кавычки остаются (``user_id`` не станет ``userid``)."""
    return cell.replace("**", "").replace("__", "").strip()


def _column_name(header: Sequence[str], k: int) -> str:
    return header[k] if k < len(header) and header[k] else f"колонка {k + 1}"


def _serialize_row(header: Sequence[str], cells: Sequence[str]) -> str:
    """Строка таблицы для модели: «Колонка: значение; …», пустые ячейки как «(пусто)»."""
    values = [_clean_cell(c) or EMPTY_CELL for c in cells]
    values += [EMPTY_CELL] * (len(header) - len(values))
    if len(header) == 2 and header[0].lower() in KEY_VALUE_HEADERS:
        return f"{values[0]}: {values[1] if len(values) > 1 else EMPTY_CELL}"
    return "; ".join(f"{_column_name(header, k)}: {v}" for k, v in enumerate(values))


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
    pipe_tables: set[int] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.full = canonical_text(self.lines)
        offset = 0
        for line in self.lines:
            self.starts.append(offset)
            offset += len(line.text) + 1
        if self.is_md:
            self.pipe_tables = self.find_pipe_tables()

    def find_pipe_tables(self) -> set[int]:
        """Строки таблиц без внешних черт: заголовок, под ним «--- | ---», затем строки до пустой."""
        found: set[int] = set()
        n = len(self.lines)
        i = 0
        while i + 1 < n:
            head, sep = self.text(i), self.text(i + 1)
            if "|" in head and not TABLE_ROW.match(head) and PIPE_SEP.match(sep):
                j = i + 2
                while j < n and self.text(j).strip() and "|" in self.text(j):
                    j += 1
                found.update(range(i, j))
                i = j
            else:
                i += 1
        return found

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
        return line.table is not None or (
            self.is_md and (bool(TABLE_ROW.match(line.text)) or i in self.pipe_tables)
        )

    def is_fence(self, i: int) -> bool:
        return self.is_md and bool(FENCE.match(self.text(i)))

    def is_list(self, i: int) -> bool:
        return self.lines[i].kind is BlockKind.LIST_ITEM or bool(LIST_ITEM.match(self.text(i)))

    def is_quote(self, i: int) -> bool:
        return self.is_md and bool(QUOTE_LINE.match(self.text(i)))

    def is_special(self, i: int) -> bool:
        return self.lines[i].kind not in (None, BlockKind.LIST_ITEM)

    def breaks_paragraph(self, i: int) -> bool:
        return (
            not self.text(i).strip()
            or self.heading_level(i) is not None
            or self.is_table(i)
            or self.is_list(i)
            or self.is_fence(i)
            or self.is_quote(i)
            or self.is_special(i)
            or (self.is_md and (bool(HR.match(self.text(i))) or bool(SETEXT.match(self.text(i)))))
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
        if kind in SENTENCE_KINDS:
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
        """Предложения по всему тексту блока: перенос строки внутри абзаца (PDF, ширина строки в MD)
        предложение не режет, разделяет только razdel."""
        start = self.starts[first]
        if kind is BlockKind.LIST_ITEM:
            labelled = self.lines[first].kind is BlockKind.LIST_ITEM  # DOCX: метка нумерации любого вида
            m = (LIST_LABEL if labelled else LIST_ITEM).match(self.text(first))
            start += m.end() if m else 0
        if kind is BlockKind.QUOTE:
            marker = QUOTE_LINE.match(self.text(first))
            start += marker.end() if marker else 0
        end = self.starts[last] + len(self.text(last))
        out: list[Sentence] = []
        for piece in sentenize(self.full[start:end]):
            if not piece.text.strip():
                continue
            begin = start + piece.start
            out.append(
                Sentence(
                    id=f"{bid}.{len(out) + 1}",
                    text=piece.text,
                    char_start=begin,
                    char_end=begin + len(piece.text),
                    line=bisect_right(self.starts, begin),
                )
            )
        return out

    # ---------------------------------------------------------------- проход по строкам

    def front_matter_end(self) -> int | None:
        """Индекс строки, закрывающей YAML-шапку Markdown в начале файла («---» … «---»), или None."""
        n = len(self.lines)
        if not self.is_md or n < 3 or self.text(0).strip() != "---":
            return None
        for k in range(2, n):
            if self.text(k).strip() in ("---", "..."):
                body = [self.text(m) for m in range(1, k) if self.text(m).strip()]
                return k if body and all(FRONT_MATTER_KEY.match(t) or t[:1] in " -#" for t in body) else None
        return None

    def run(self) -> list[Block]:
        i, n = 0, len(self.lines)
        end = self.front_matter_end()
        if end is not None:  # шапка с метаданными: один блок кода, а не «заголовок» по правилу Setext
            self.add(BlockKind.CODE, 1, end - 1)
            i = end + 1
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
            j = i + 1
            while kind is BlockKind.CODE and j < n and self.lines[j].kind is BlockKind.CODE:
                j += 1  # соседние строки кода (абзацы DOCX в стиле «Source Code») это один блок
            self.add(kind, i, j - 1)
            return j
        if self.is_fence(i):
            return self.fence(i, n)
        if self.is_quote(i):
            return self.quote(i, n)
        if self.is_table(i):
            return self.table(i, n)
        if self.is_list(i):
            return self.list_item(i, n)
        j = i + 1
        while j < n and not self.breaks_paragraph(j):
            j += 1
        underline = SETEXT.match(self.text(j)) if self.is_md and j < n else None
        if underline and self.fits_setext(i, j - 1):  # абзац, подчёркнутый «===» или «---», это заголовок
            return self.heading(i, 1 if underline.group("h1") else 2, last=j - 1) + 1
        self.add(BlockKind.PARAGRAPH, i, j - 1)
        return j + 1 if underline else j  # линию «===» или «---» под длинным абзацем пропускаем

    def fits_setext(self, first: int, last: int) -> bool:
        """Заголовок Setext короткий. Длинный абзац над «---» это абзац и линия, а не заголовок."""
        text = " ".join(self.text(k).strip() for k in range(first, last + 1))
        return last - first < MAX_SETEXT_LINES and len(text) <= MAX_SETEXT_CHARS

    def heading(self, i: int, level: int, last: int | None = None) -> int:
        last = i if last is None else last
        raw = self.text(i)
        m = HEADING.match(raw) if self.is_md else None
        if m:
            title = _heading_title(raw)
        else:
            title = " ".join(_plain(self.text(k)) for k in range(i, last + 1) if self.text(k).strip())
        self.section = [(lv, t) for lv, t in self.section if lv < level] + [(level, title)]
        self.add(BlockKind.HEADING, i, last)
        return last + 1

    def quote(self, i: int, n: int) -> int:
        j = i + 1
        while j < n and self.is_quote(j):
            j += 1
        self.add(BlockKind.QUOTE, i, j - 1)
        return j

    def fence(self, i: int, n: int) -> int:
        opening = FENCE.match(self.text(i))
        mark = opening.group("mark") if opening else "```"
        j = i + 1
        while j < n and not self._closes_fence(j, mark):
            j += 1
        body_last = min(j, n) - 1
        if body_last >= i + 1 and any(self.text(k).strip() for k in range(i + 1, body_last + 1)):
            first = next(k for k in range(i + 1, body_last + 1) if self.text(k).strip())
            last = max(k for k in range(i + 1, body_last + 1) if self.text(k).strip())
            self.add(BlockKind.CODE, first, last)
        return j + 1

    def _closes_fence(self, j: int, mark: str) -> bool:
        found = FENCE.match(self.text(j))
        if not found:
            return False
        closing = found.group("mark")
        rest = self.text(j).strip()[len(closing) :].strip()
        return closing[0] == mark[0] and len(closing) >= len(mark) and not rest

    def list_item(self, i: int, n: int) -> int:
        j = i + 1
        paragraph = self.lines[i].paragraph
        while j < n and self.text(j).strip() and not self.is_list(j) and not self.is_table(j):
            indented = self.is_md and self.text(j).startswith(("  ", "\t"))
            same_paragraph = (
                paragraph is not None and self.lines[j].paragraph == paragraph
            )  # DOCX: перенос строки
            if not (indented or same_paragraph):
                break
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
        if self.is_md and _is_separator(line.text):
            return i + 1
        cells = list(line.cells) if line.table is not None else _md_cells(line.text)
        is_header = line.header or (
            self.is_md and self.header is None and i + 1 < n and _is_separator(self.text(i + 1))
        )
        if is_header:
            self.header = [_clean_cell(c) for c in cells]
            return i + 1
        # Пустая строка таблицы тоже блок: незаполненная форма это факт документа.
        self.table_row += 1
        hdr = self.header or []
        serialized = _serialize_row(hdr, cells)
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


def _is_separator(text: str) -> bool:
    """Строка «---|---» под заголовком таблицы Markdown, с внешними чертами или без них."""
    return bool(TABLE_SEP.match(text) or PIPE_SEP.match(text))


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
        column = _column_name(header, k)
        out.append(
            TableCell(index=k + 1, column=column, text=value, char_start=base + pos, char_end=base + cursor)
        )
    return out


def build_blocks(lines: Sequence[SourceLine], source: DocKind) -> list[Block]:
    """Режет строки на блоки с ID, якорями, разделами и предложениями. Детерминированно."""
    return _Builder(lines, source).run()


def text_diagnostics(lines: Sequence[SourceLine], source: DocKind, name: str) -> list[Diagnostic]:
    """Пустой документ и документ без текстовых блоков: диагностика вместо молчаливого пустого отчёта.

    ``IN-071`` ошибка: в блоках нет ни одной буквы или цифры (пробелы, одна разметка, пустые ячейки).
    ``IN-081`` предупреждение: есть только заголовки, это похоже на пустой шаблон.
    """
    blocks = build_blocks(lines, source)
    texts = [b for b in blocks if b.kind in TEXT_KINDS and any(ch.isalnum() for ch in b.text)]
    if not texts:
        visible = any(line.text.strip() for line in lines)
        return [
            Diagnostic(
                code=IN_NO_TEXT,
                level=DiagLevel.ERROR,
                message=(
                    "В документе нет текстовых блоков: только разметка, пустые таблицы или код"
                    if visible
                    else "Документ пустой: в нём нет ни одного видимого символа"
                ),
                action="Проверка не выполнялась",
                hint="Пришлите документ с текстом требований (не пустой шаблон)",
                location=name,
                step="parse",
            )
        ]
    if all(b.kind is BlockKind.HEADING for b in texts):
        return [
            Diagnostic(
                code=IN_HEADINGS_ONLY,
                level=DiagLevel.WARNING,
                message="В документе только заголовки, текста под ними нет: похоже на пустой шаблон",
                action="Проверка идёт по тому, что есть",
                hint="Заполните разделы документа и запустите проверку снова",
                location=name,
                step="parse",
            )
        ]
    return []


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


def _header(block: Block, title: str | None) -> str:
    """Служебная строка блока: «[B023] § Раздел › Подраздел | пункт списка 2 | строка 38 | метка: X».

    Название документа (общий для всех блоков первый раздел) в путь не идёт: оно ничего не различает.
    """
    section = block.section[1:] if title is not None else block.section
    kind = block.kind.label
    number = block.anchor.row if block.kind is BlockKind.TABLE_ROW else block.ordinal
    if number is not None:
        kind = f"{kind} {number}"
    parts = [f"§ {' › '.join(section)}" if section else "§ —", kind]
    where = _where(block)
    if where:
        parts.append(where)
    if block.source_id:
        parts.append(f"метка: {block.source_id}")
    return f"[{block.id}] " + " | ".join(parts)


def _document_title(doc: ParsedDocument) -> str | None:
    """Первый раздел, общий для всех блоков (заголовок-название документа), или None."""
    firsts = {b.section[0] if b.section else None for b in doc.blocks if b.kind is not BlockKind.FURNITURE}
    return next(iter(firsts)) if len(firsts) == 1 else None


def _heading_title(text: str) -> str:
    m = HEADING.match(text)
    title = CLOSING_HASHES.sub("", m.group(2)) if m else text
    return " ".join(_plain(title).split())


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render_for_model(doc: ParsedDocument) -> str:
    """Документ с ID блоков и предложений для модели (исследование 18, п. 3.2).

    Модель ссылается на ``B…`` и ``B….N``, а цитату по ним подставляет код. Текст документа здесь
    это данные, а не инструкции: обрамление и предупреждение об этом добавляет промпт шага.
    Формат согласован с описанием разметки в ``prompts/search_user.md`` (#14): заголовок одной строкой,
    у блока из одного предложения ID предложения не ставится, у строк таблиц «Колонка: значение; …».
    """
    title = _document_title(doc)
    out: list[str] = []
    for block in doc.blocks:
        if block.kind is BlockKind.HEADING:
            level = max(1, len(block.section))
            out.append(f"[{block.id}] {'#' * level} {_heading_title(block.text)}")
            continue
        out.append(_header(block, title))
        if block.kind is BlockKind.TABLE_ROW:
            out.append(f"  {block.serialized}")
        elif len(block.sentences) == 1:
            out.append(f"  {_one_line(block.sentences[0].text)}")
        elif block.sentences:
            out.extend(f"  [{s.id}] {_one_line(s.text)}" for s in block.sentences)
        else:
            out.extend(f"  {line}" for line in block.text.splitlines() if line.strip())
    return "\n".join(out) + "\n"
