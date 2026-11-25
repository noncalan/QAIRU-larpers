"""Детерминированная предобработка Markdown для нарезки требований (эксперимент, не код агента).

Режет документ на блоки с якорями: раздел (путь заголовков), строки, тип блока.
Таблицы превращает в строки «Колонка: значение», ловит метки вида «Ф-1», «REQ-12», «FR.3»,
режет абзацы и пункты на предложения (razdel, если установлен, иначе regex).

Запуск: uv run --with razdel python experiments/18-segmentation/preseg.py data/input/requirements.md
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict

try:
    from razdel import sentenize as _razdel_sentenize
except ImportError:  # запасной путь без зависимостей
    _razdel_sentenize = None

HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
LIST_ITEM = re.compile(r"^\s*(?:[*+-]|\d+[.)])\s+")
ORDINAL = re.compile(r"^\s*(\d+)[.)]\s+")
FENCE = re.compile(r"^\s*(```|~~~)")
HR = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")
# Метка требования в начале пункта: «**Ф-1.**», «REQ-12:», «FR.3», «ТР-2.1)». Буквы 1–6, разделитель, номер.
SOURCE_ID = re.compile(
    r"^\s*(?:[*+-]\s+|\d+[.)]\s+)?(?:\*\*|__)?\s*"
    r"(?P<id>[A-Za-zА-Яа-яЁё]{1,6}[-‑.‐_ ]?\d+(?:\.\d+)*)"
    r"\s*[.:)]?\s*(?:\*\*|__)?[.:)]?\s+"
)
# Жирная числовая метка «**200.**» — кандидат в код статуса или номер пункта, не ID требования.
BOLD_NUM = re.compile(r"^\s*\*\*(?P<num>\d{3})\.?\*\*")
ABBR = ("т.п.", "т.д.", "т.е.", "т.ч.", "г.", "стр.", "п.", "пп.", "рис.", "табл.", "см.", "др.", "e.g.", "i.e.")


@dataclass
class Block:
    bid: str
    kind: str                 # heading | paragraph | list_item | table_row | code
    line_start: int           # 1-based, включительно
    line_end: int
    section: list[str]        # путь заголовков
    text: str                 # исходный текст блока (как в документе)
    serialized: str = ""      # для таблиц: «Колонка: значение; …»
    source_id: str | None = None
    ordinal: int | None = None  # номер пункта нумерованного списка в разделе
    sentences: list[dict] = field(default_factory=list)  # {text, line}


def split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if _razdel_sentenize is not None:
        return [s.text for s in _razdel_sentenize(text)]
    parts = re.split(r"(?<=[.!?…])\s+(?=[«\"(]?[А-ЯЁA-Z0-9*])", text)
    out: list[str] = []
    for p in parts:  # склеиваем обратно после сокращений
        if out and out[-1].lower().endswith(ABBR):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", s)]


def _plain(s: str) -> str:
    return re.sub(r"[*_`]", "", s).strip()


def parse(md: str) -> list[Block]:
    lines = md.splitlines()
    blocks: list[Block] = []
    section: list[tuple[int, str]] = []
    i, n = 0, len(lines)
    header: list[str] | None = None

    def add(kind, a, b, text, **kw):
        blk = Block(bid=f"B{len(blocks) + 1:03d}", kind=kind, line_start=a + 1, line_end=b + 1,
                    section=[t for _, t in section], text=text, **kw)
        blocks.append(blk)
        return blk

    while i < n:
        line = lines[i]
        if not line.strip() or HR.match(line):
            header = None
            i += 1
            continue
        m = HEADING.match(line)
        if m:
            level, title = len(m.group(1)), _plain(m.group(2))
            section = [(lv, t) for lv, t in section if lv < level] + [(level, title)]
            add("heading", i, i, line.strip())
            header = None
            i += 1
            continue
        if FENCE.match(line):
            j = i + 1
            while j < n and not FENCE.match(lines[j]):
                j += 1
            body = "\n".join(lines[i + 1:j])
            add("code", i, min(j, n - 1), body)
            i = j + 1
            continue
        if TABLE_ROW.match(line):
            if TABLE_SEP.match(line):
                i += 1
                continue
            cells = _cells(line)
            nxt = lines[i + 1] if i + 1 < n else ""
            if header is None and TABLE_SEP.match(nxt):
                header = [_plain(c) for c in cells]
                i += 2
                continue
            if all(not _plain(c) for c in cells):  # пустая строка таблицы
                i += 1
                continue
            hdr = header or [f"col{k + 1}" for k in range(len(cells))]
            if len(hdr) == 2 and hdr[0].lower() in ("поле", "field", "параметр", "parameter", "ключ", "key"):
                ser = f"{_plain(cells[0])}: {_plain(cells[1]) if len(cells) > 1 else ''}"
            else:
                ser = "; ".join(f"{h}: {_plain(c)}" for h, c in zip(hdr, cells) if _plain(c))
            add("table_row", i, i, line.strip(), serialized=ser)
            i += 1
            continue
        header = None
        if LIST_ITEM.match(line):
            j = i + 1  # продолжение пункта: строки с отступом
            while j < n and lines[j].strip() and lines[j].startswith(("  ", "\t")) and not LIST_ITEM.match(lines[j]):
                j += 1
            text = " ".join(l.strip() for l in lines[i:j])
            om = ORDINAL.match(line)
            blk = add("list_item", i, j - 1, text, ordinal=int(om.group(1)) if om else None)
            i = j
        else:
            j = i + 1
            while (j < n and lines[j].strip() and not HEADING.match(lines[j]) and not TABLE_ROW.match(lines[j])
                   and not LIST_ITEM.match(lines[j]) and not FENCE.match(lines[j])):
                j += 1
            text = "\n".join(lines[i:j])
            blk = add("paragraph", i, j - 1, text)
            i = j
        sm = SOURCE_ID.match(blk.text)
        if sm and not BOLD_NUM.match(blk.text):
            blk.source_id = sm.group("id").replace("‑", "-")
        # предложения с номером строки: жёсткий перенос строки внутри абзаца — граница предложения
        for k, ln in enumerate(blk.text.split("\n")):
            body = LIST_ITEM.sub("", ln, count=1) if k == 0 and blk.kind == "list_item" else ln
            for s in split_sentences(body):
                blk.sentences.append({"text": s, "line": blk.line_start + k})
    return blocks


def stats(blocks: list[Block]) -> dict:
    from collections import Counter
    kinds = Counter(b.kind for b in blocks)
    content = [b for b in blocks if b.kind != "heading"]
    sents = sum(len(b.sentences) for b in content if b.kind in ("paragraph", "list_item"))
    return {
        "blocks": len(blocks), "by_kind": dict(kinds), "content_blocks": len(content),
        "sentences_in_paragraphs_and_lists": sents,
        "table_rows": kinds.get("table_row", 0),
        "with_source_id": [b.source_id for b in blocks if b.source_id],
    }


if __name__ == "__main__":
    path = sys.argv[1]
    md = open(path, encoding="utf-8").read()
    bl = parse(md)
    if "--json" in sys.argv:
        print(json.dumps([asdict(b) for b in bl], ensure_ascii=False, indent=1))
    else:
        print(f"splitter: {'razdel' if _razdel_sentenize else 'regex'}; chars: {len(md)}")
        print(json.dumps(stats(bl), ensure_ascii=False))
        for b in bl:
            if b.kind == "heading":
                continue
            show = b.serialized or b.text.replace("\n", " ⏎ ")
            print(f"{b.bid} L{b.line_start:<3} {b.kind:9} {('[' + b.source_id + ']') if b.source_id else '':7} "
                  f"{' / '.join(b.section[-2:])[:38]:38} | {show[:90]}")
