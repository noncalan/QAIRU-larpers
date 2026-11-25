"""Показ Markdown-документа модели блоками с ID (исследование 18, п. 3.2). Эксперимент, не код агента.

Порт разбора из experiments/18-segmentation/preseg.py с тремя правками для поиска дефектов:
- подчёркивания и обратные кавычки в ячейках таблиц сохраняются (`card_id` не превращается в `cardid`);
- пустые ячейки и полностью пустые строки таблиц видны модели как «(пусто)»: незаполненное поле
  тоже факт документа;
- заголовки показываются отдельными блоками, потому что в них тоже бывают дефекты.

Запуск: uv run --no-project --with razdel python experiments/22-search-prompt/render.py data/input/requirements.md
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments" / "18-segmentation"))
from preseg import (
    BOLD_NUM,
    FENCE,
    HEADING,
    HR,
    LIST_ITEM,
    ORDINAL,
    SOURCE_ID,
    TABLE_ROW,
    TABLE_SEP,
    split_sentences,
)

EMPTY_CELL = "(пусто)"
KEY_VALUE_HEADERS = ("поле", "field", "параметр", "parameter", "ключ", "key")
KIND_RU = {
    "paragraph": "абзац",
    "list_item": "пункт списка",
    "table_row": "строка таблицы",
    "code": "код",
}


@dataclass
class Block:
    bid: str
    kind: str  # heading | paragraph | list_item | table_row | code
    line_start: int  # 1-based, включительно
    line_end: int
    section: list[str]
    text: str  # исходный текст блока
    shown: str = ""  # как блок показан модели (для таблиц: «Колонка: значение; …»)
    source_id: str | None = None
    row: int | None = (
        None  # номер строки данных в таблице или пункта нумерованного списка
    )
    sentences: list[str] = field(default_factory=list)


def _cells(line: str) -> list[str]:
    s = line.strip().removeprefix("|").removesuffix("|")
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _clean_cell(cell: str) -> str:
    return cell.replace("**", "").replace("__", "").strip()


def _plain_title(title: str) -> str:
    return re.sub(r"\*\*|__|`", "", title).strip()


def _serialize_row(header: list[str], cells: list[str]) -> str:
    values = [_clean_cell(c) or EMPTY_CELL for c in cells]
    values += [EMPTY_CELL] * (len(header) - len(values))  # строка короче заголовка
    if len(header) == 2 and header[0].lower() in KEY_VALUE_HEADERS:
        return f"{values[0]}: {values[1] if len(values) > 1 else EMPTY_CELL}"
    names = header + [f"колонка {k + 1}" for k in range(len(header), len(values))]
    return "; ".join(f"{h}: {v}" for h, v in zip(names, values, strict=False))


class _Parser:
    def __init__(self, md: str) -> None:
        self.lines = md.splitlines()
        self.blocks: list[Block] = []
        self.section: list[tuple[int, str]] = []

    def add(self, kind: str, a: int, b: int, text: str, **kw: object) -> Block:
        blk = Block(
            bid=f"B{len(self.blocks) + 1:03d}",
            kind=kind,
            line_start=a + 1,
            line_end=b + 1,
            # Заголовок первого уровня (название документа) в путь не идёт: он общий для всех блоков.
            section=[t for lv, t in self.section if lv > 1],
            text=text,
            **kw,
        )
        self.blocks.append(blk)
        return blk

    def table(self, i: int) -> int:
        """Таблица с строки i: заголовок, разделитель, строки данных (в том числе пустые)."""
        lines, n = self.lines, len(self.lines)
        has_header = i + 1 < n and TABLE_SEP.match(lines[i + 1])
        header = [_clean_cell(c) for c in _cells(lines[i])] if has_header else []
        j = i + 2 if has_header else i
        row = 0
        while j < n and TABLE_ROW.match(lines[j]):
            if not TABLE_SEP.match(lines[j]):
                row += 1
                cells = _cells(lines[j])
                hdr = header or [f"колонка {k + 1}" for k in range(len(cells))]
                shown = _serialize_row(hdr, cells)
                self.add("table_row", j, j, lines[j].strip(), shown=shown, row=row)
            j += 1
        return j

    def text_block(self, i: int) -> int:
        lines, n = self.lines, len(self.lines)
        if LIST_ITEM.match(lines[i]):
            j = i + 1
            while (
                j < n
                and lines[j].strip()
                and lines[j].startswith(("  ", "\t"))
                and not LIST_ITEM.match(lines[j])
            ):
                j += 1
            om = ORDINAL.match(lines[i])
            text = " ".join(x.strip() for x in lines[i:j])
            blk = self.add(
                "list_item", i, j - 1, text, row=int(om.group(1)) if om else None
            )
        else:
            j = i + 1
            while (
                j < n
                and lines[j].strip()
                and not HEADING.match(lines[j])
                and not TABLE_ROW.match(lines[j])
                and not LIST_ITEM.match(lines[j])
                and not FENCE.match(lines[j])
                and not HR.match(lines[j])
            ):
                j += 1
            blk = self.add("paragraph", i, j - 1, "\n".join(lines[i:j]))
        sm = SOURCE_ID.match(blk.text)
        if sm and not BOLD_NUM.match(blk.text):
            blk.source_id = sm.group("id").replace("‑", "-")
        for ln in blk.text.split("\n"):
            body = LIST_ITEM.sub("", ln, count=1) if blk.kind == "list_item" else ln
            blk.sentences.extend(split_sentences(body))
        return j

    def run(self) -> list[Block]:
        lines, n, i = self.lines, len(self.lines), 0
        while i < n:
            line = lines[i]
            if not line.strip() or HR.match(line):
                i += 1
                continue
            m = HEADING.match(line)
            if m:
                level, title = len(m.group(1)), _plain_title(m.group(2))
                self.section = [(lv, t) for lv, t in self.section if lv < level]
                self.section.append((level, title))
                self.add("heading", i, i, line.strip())
                i += 1
            elif FENCE.match(line):
                j = i + 1
                while j < n and not FENCE.match(lines[j]):
                    j += 1
                self.add("code", i, min(j, n - 1), "\n".join(lines[i + 1 : j]))
                i = j + 1
            elif TABLE_ROW.match(line):
                i = self.table(i)
            else:
                i = self.text_block(i)
        return self.blocks


def parse(md: str) -> list[Block]:
    return _Parser(md).run()


def _where(blk: Block) -> str:
    lines = (
        f"строка {blk.line_start}"
        if blk.line_start == blk.line_end
        else f"строки {blk.line_start}–{blk.line_end}"
    )
    kind = KIND_RU[blk.kind]
    if blk.row is not None:
        kind = f"{kind} {blk.row}"
    parts = [f"§ {' › '.join(blk.section)}" if blk.section else "§ —", kind, lines]
    if blk.source_id:
        parts.append(f"метка: {blk.source_id}")
    return " | ".join(parts)


def render(blocks: list[Block]) -> str:
    out: list[str] = []
    for blk in blocks:
        if blk.kind == "heading":
            out.append(f"[{blk.bid}] {blk.text}")
            continue
        out.append(f"[{blk.bid}] {_where(blk)}")
        if blk.kind == "table_row":
            out.append(f"  {blk.shown}")
        elif blk.kind == "code":
            out.extend(f"  {ln}" for ln in blk.text.splitlines())
        elif len(blk.sentences) == 1:
            out.append(f"  {blk.sentences[0]}")
        else:
            out.extend(f"  [{blk.bid}.{k}] {s}" for k, s in enumerate(blk.sentences, 1))
    return "\n".join(out) + "\n"


def block_ids(blocks: list[Block]) -> set[str]:
    ids = {b.bid for b in blocks}
    ids |= {f"{b.bid}.{k}" for b in blocks for k in range(1, len(b.sentences) + 1)}
    return ids


if __name__ == "__main__":
    print(render(parse(Path(sys.argv[1]).read_text(encoding="utf-8"))), end="")
