"""Разбор DOCX в строки для ``build_blocks`` (шаг 1).

Тело документа обходим по порядку, включая содержимое полей и элементов управления (``w:sdt``).
Абзац получает номер (якорь «абзац N»), строка таблицы номер таблицы и номер строки, а каждая ячейка
свои смещения в тексте (якорь «таблица M, строка R, ячейка C»). Заголовки определяем по стилю и
уровню структуры, метки автонумерации («1.», «3.2.», «•») добавляем в текст абзаца, как их показывает
Word. Сноски идут блоками сразу после абзаца со ссылкой на них, колонтитулы блоками в начале.

Что не читаем и о чём говорим в диагностике: текстовые рамки (``IN-037``). Исправления в режиме
правки читаем как принятые: вставки включены, удалённый текст пропущен (``IN-036``).
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import docx
from docx.document import Document as DocxDocument
from docx.enum.style import WD_STYLE_TYPE
from docx.opc.exceptions import PackageNotFoundError
from lxml import etree

from validator.errors import IN_DOCX_BROKEN, IN_DOCX_TEXT_BOXES, IN_DOCX_TRACKED_CHANGES, InputError
from validator.ingest.blocks import OBLIGATION, ParsedLines, text_diagnostics
from validator.ingest.docx_numbering import NS, Numbering, W
from validator.models import BlockKind, DiagLevel, Diagnostic, DocKind, Extraction, SourceLine

_HEADING_STYLE = re.compile(r"^(?:Heading|Заголовок)\s*(\d)$", re.IGNORECASE)
CELL_SEPARATOR = " | "
NESTED_ROW_SEPARATOR = "; "
MAX_STYLE_DEPTH = 20
"""Глубина цепочки стилей: защита от цикла «стиль основан на самом себе» в испорченном файле."""
MAX_HEADER_CELL_CHARS = 32
MAX_HEADER_CELL_WORDS = 4
_HEADER_LABEL = re.compile(r"(?:[^\W\d_]|[ №/_()\-–‑])+")
_HEADER_OBLIGATION = re.compile(r"\b(?:должен|может|may|can)\b", re.IGNORECASE)
_SKIP_IN_TEXT = frozenset({"pPr", "rPr", "del", "moveFrom", "Fallback", "drawing", "pict", "object"})
_NESTED_IN_TEXT = frozenset(
    {
        "r",
        "hyperlink",
        "ins",
        "moveTo",
        "smartTag",
        "fldSimple",
        "sdt",
        "sdtContent",
        "AlternateContent",
        "Choice",
    }
)
_CHAR_TAGS = {"tab": " ", "noBreakHyphen": "-"}
_CODE_STYLE = re.compile(
    r"^(?:source code|code|preformatted|html preformatted|verbatim|листинг|исходный код)", re.I
)
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


def _w(name: str) -> str:
    return f"{{{W}}}{name}"


def _local(element: etree._Element) -> str:
    return etree.QName(element).localname if isinstance(element.tag, str) else ""


def _val(element: etree._Element | None) -> str | None:
    return None if element is None else element.get(_w("val"))


def _iter_blocks(parent: etree._Element) -> Iterator[etree._Element]:
    """Абзацы и таблицы по порядку, с заходом в элементы управления содержимым и разметку."""
    for child in parent:
        name = _local(child)
        if name in ("p", "tbl"):
            yield child
        elif name == "sdt":
            content = child.find("w:sdtContent", NS)
            if content is not None:
                yield from _iter_blocks(content)
        elif name == "customXml":
            yield from _iter_blocks(child)


def _xml_part(document: DocxDocument, partname: str) -> etree._Element | None:
    for part in document.part.package.iter_parts():
        if str(part.partname) == partname:
            return etree.fromstring(part.blob, _PARSER)
    return None


@dataclass(frozen=True)
class _StyleInfo:
    """Что стиль абзаца (вместе с предками) говорит о структуре: заголовок, нумерация, код."""

    level: int | None = None
    numbering: tuple[str, int | None] | None = None
    code: bool = False


@dataclass
class _Context:
    """Состояние одного разбора: стили, нумерация, сноски и счётчики для якорей."""

    document: DocxDocument
    numbering: Numbering
    footnotes: dict[str, str]
    paragraphs: int = 0
    tables: int = 0
    lines: list[SourceLine] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    styles: dict[str, _StyleInfo] = field(default_factory=dict)

    # ------------------------------------------------------------ текст абзаца

    def text(self, paragraph: etree._Element) -> str:
        """Текст абзаца как в Word: включая вставки правок и гиперпереходы, без удалённого."""
        parts: list[str] = []
        self._collect(paragraph, parts)
        return "".join(parts)

    def _collect(self, element: etree._Element, out: list[str]) -> None:
        for child in element:
            name = _local(child)
            if name in _SKIP_IN_TEXT:
                continue
            if name == "t":
                out.append(child.text or "")
            elif name in _CHAR_TAGS:
                out.append(_CHAR_TAGS[name])
            elif name in ("br", "cr"):
                out.append("\n" if child.get(_w("type")) in (None, "textWrapping") else "")
            elif name in ("footnoteReference", "endnoteReference"):
                self._note(name, child.get(_w("id")))
            elif name in _NESTED_IN_TEXT:
                self._collect(child, out)

    def _note(self, tag: str, note_id: str | None) -> None:
        key = note_id if tag == "footnoteReference" else f"endnote:{note_id}"
        if key in self.footnotes:
            self.pending.append(self.footnotes[key])

    # ------------------------------------------------------------ стили, заголовки, нумерация

    def _style_info(self, style_id: str | None) -> _StyleInfo:
        """Свойства стиля с учётом цепочки «основан на»."""
        key = style_id or ""
        if key in self.styles:
            return self.styles[key]
        level: int | None = None
        numbering: tuple[str, int | None] | None = None
        code = False
        seen: set[str] = set()
        style = self._style(style_id)
        while style is not None and len(seen) < MAX_STYLE_DEPTH and style.style_id not in seen:
            seen.add(style.style_id)
            level = level if level is not None else _level_from_style(style)
            numbering = numbering or _numbering_of(style.element.find("w:pPr", NS))
            code = code or bool(_CODE_STYLE.match(style.name or ""))
            style = style.base_style
        info = self.styles[key] = _StyleInfo(level, numbering, code)
        return info

    def _style(self, style_id: str | None) -> Any:
        styles = self.document.styles
        try:
            if style_id:
                return styles.get_by_id(style_id, WD_STYLE_TYPE.PARAGRAPH)
            return styles.default(WD_STYLE_TYPE.PARAGRAPH)
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _style_id(ppr: etree._Element | None) -> str | None:
        return _val(ppr.find("w:pStyle", NS)) if ppr is not None else None

    def is_code(self, paragraph: etree._Element) -> bool:
        return self._style_info(self._style_id(paragraph.find("w:pPr", NS))).code

    def heading_level(self, paragraph: etree._Element) -> int | None:
        ppr = paragraph.find("w:pPr", NS)
        style_level = self._style_info(self._style_id(ppr)).level
        outline = _val(ppr.find("w:outlineLvl", NS)) if ppr is not None else None
        if outline is not None and outline.isdigit():
            return int(outline) + 1 if int(outline) < 9 else None
        return style_level

    def label(self, paragraph: etree._Element) -> str | None:
        """Метка автонумерации абзаца (из самого абзаца или его стиля) или None."""
        ppr = paragraph.find("w:pPr", NS)
        from_style = self._style_info(self._style_id(ppr)).numbering
        own = _numbering_of(ppr)
        chosen = own or from_style  # нумерация абзаца важнее нумерации стиля; numId 0 её отключает
        if chosen is None or chosen[0] == "0":
            return None
        return self.numbering.label(chosen[0], chosen[1] or 0)


def _level_from_style(style: Any) -> int | None:
    name = style.name or ""
    if name == "Title":
        return 1
    match = _HEADING_STYLE.match(name)
    if match:
        return int(match.group(1))
    outline = _val(style.element.find("w:pPr/w:outlineLvl", NS))
    return int(outline) + 1 if outline is not None and outline.isdigit() and int(outline) < 9 else None


def _numbering_of(ppr: etree._Element | None) -> tuple[str, int | None] | None:
    """``numPr`` свойств абзаца или стиля: (numId, ilvl); ilvl None, если он не указан."""
    num_pr = ppr.find("w:numPr", NS) if ppr is not None else None
    if num_pr is None:
        return None
    num_id = _val(num_pr.find("w:numId", NS))
    if num_id is None:
        return None
    level = _val(num_pr.find("w:ilvl", NS))
    return num_id, int(level) if level is not None and level.isdigit() else None


# ---------------------------------------------------------------- абзацы и таблицы


def _paragraph_lines(ctx: _Context, element: etree._Element) -> list[SourceLine]:
    ctx.paragraphs += 1
    number = ctx.paragraphs
    label = ctx.label(element)  # счётчик нумерации идёт и по пустым абзацам, как в Word
    parts = [part.strip() for part in ctx.text(element).split("\n") if part.strip()]
    if not parts:
        return []
    level = ctx.heading_level(element)
    if level is not None:
        title = " ".join(parts)
        return [
            SourceLine(text=f"{label} {title}" if label else title, paragraph=number, heading_level=level)
        ]
    if ctx.is_code(element):
        return [SourceLine(text=part, paragraph=number, kind=BlockKind.CODE) for part in parts]
    lines = [SourceLine(text=part, paragraph=number) for part in parts]
    if label:
        first = lines[0]
        lines[0] = first.model_copy(update={"text": f"{label} {first.text}", "kind": BlockKind.LIST_ITEM})
    return lines


def _flush_notes(ctx: _Context) -> list[SourceLine]:
    notes = [SourceLine(text=text, kind=BlockKind.FOOTNOTE) for text in ctx.pending if text.strip()]
    ctx.pending.clear()
    return notes


def _is_vmerge_continue(tc: etree._Element) -> bool:
    merge = tc.find("w:tcPr/w:vMerge", NS)
    return merge is not None and merge.get(_w("val")) in (None, "continue")


def _span(tc: etree._Element) -> int:
    value = _val(tc.find("w:tcPr/w:gridSpan", NS))
    return int(value) if value is not None and value.isdigit() and int(value) > 0 else 1


def _cell_text(ctx: _Context, tc: etree._Element) -> str:
    """Текст ячейки одной строкой; вложенная таблица входит в текст ячейки, чтобы ничего не терять."""
    parts: list[str] = []
    for item in _iter_blocks(tc):
        if _local(item) == "p":
            parts.append(ctx.text(item))
        else:
            rows = [
                CELL_SEPARATOR.join(_cell_text(ctx, cell) for cell in row.findall("w:tc", NS))
                for row in item.findall("w:tr", NS)
            ]
            parts.append(NESTED_ROW_SEPARATOR.join(rows))
    return " ".join(" ".join(parts).split())


def _is_header_label(text: str) -> bool:
    return (
        len(text) <= MAX_HEADER_CELL_CHARS
        and len(text.split()) <= MAX_HEADER_CELL_WORDS
        and bool(_HEADER_LABEL.fullmatch(text))
        and (text == "№" or any(char.isalpha() for char in text))
        and not OBLIGATION.search(text)
        and not _HEADER_OBLIGATION.search(text)
    )


def _has_label_row(lines: list[SourceLine]) -> bool:
    """Подписи должны отличаться по форме от первой непустой строки данных."""
    labels = lines[0].cells
    if len(labels) < 2 or not all(_is_header_label(cell) for cell in labels):
        return False
    if any(len(line.cells) != len(labels) for line in lines[1:]):
        return False
    body = next((line.cells for line in lines[1:] if any(line.cells)), [])
    return any(cell and not _is_header_label(cell) for cell in body)


def _table_lines(ctx: _Context, table: etree._Element) -> list[SourceLine]:
    ctx.tables += 1
    rows = table.findall("w:tr", NS)
    # w:tblLook задаёт оформление. Только w:tblHeader явно помечает строку заголовком.
    marker = rows[0].find("w:trPr/w:tblHeader", NS) if len(rows) > 1 else None
    header = marker is not None and _val(marker) in (None, "1", "true", "on")
    above: dict[int, str] = {}
    out: list[SourceLine] = []
    for index, row in enumerate(rows):
        cells: list[str] = []
        column = 0
        for tc in row.findall("w:tc", NS):
            # Продолжение вертикально объединённой ячейки показываем текстом верхней: пустым оно не является.
            text = above.get(column, "") if _is_vmerge_continue(tc) else _cell_text(ctx, tc)
            above[column] = text
            cells.append(text)
            column += _span(tc)
        out.append(
            SourceLine(
                text=CELL_SEPARATOR.join(cells),
                table=ctx.tables,
                row=index,
                cells=cells,
                header=header and index == 0,
                extraction=Extraction.TABLE,
            )
        )
    if marker is None and len(out) > 1 and _has_label_row(out):
        out[0] = out[0].model_copy(update={"inferred_header": True})
    return out


# ---------------------------------------------------------------- сноски и колонтитулы


def _note_texts(ctx_text: _Context, root: etree._Element | None, tag: str) -> dict[str, str]:
    notes: dict[str, str] = {}
    if root is None:
        return notes
    for note in root.findall(f"w:{tag}", NS):
        if note.get(_w("type")) not in (None, "normal"):  # разделители сносок
            continue
        text = " ".join(" ".join(ctx_text.text(p) for p in note.iter(_w("p"))).split())
        if text:
            notes[note.get(_w("id")) or ""] = text
    return notes


def _furniture_lines(ctx: _Context) -> list[SourceLine]:
    """Колонтитулы всех разделов: свои (не «как в предыдущем»), без повторов и без одних цифр."""
    seen: set[str] = set()
    out: list[SourceLine] = []
    for section in ctx.document.sections:
        for story in (
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ):
            if story.is_linked_to_previous:
                continue
            for paragraph in story._element.iter(_w("p")):
                text = " ".join(ctx.text(paragraph).split())
                if text in seen or not any(ch.isalpha() for ch in text):  # номер страницы это не текст
                    continue
                seen.add(text)
                out.append(SourceLine(text=text, kind=BlockKind.FURNITURE))
    return out


# ---------------------------------------------------------------- вход


def _continues_code(previous: list[SourceLine], group: list[SourceLine]) -> bool:
    last = previous[-1].kind if previous else None
    return last is BlockKind.CODE and bool(group) and group[0].kind is BlockKind.CODE


def _read(path: Path) -> tuple[list[SourceLine], list[Diagnostic]]:
    document = docx.Document(str(path))
    body = document.element.body
    scratch = _Context(document, Numbering(), {})
    footnotes = {
        **_note_texts(scratch, _xml_part(document, "/word/footnotes.xml"), "footnote"),
        **{
            f"endnote:{key}": value
            for key, value in _note_texts(
                scratch, _xml_part(document, "/word/endnotes.xml"), "endnote"
            ).items()
        },
    }
    ctx = _Context(document, Numbering.from_xml(_xml_part(document, "/word/numbering.xml")), footnotes)
    lines: list[SourceLine] = []
    groups = [_furniture_lines(ctx)]
    for item in _iter_blocks(body):
        paragraph = _local(item) == "p"
        group = _paragraph_lines(ctx, item) if paragraph else _table_lines(ctx, item)
        notes = _flush_notes(ctx)
        if _continues_code(groups[-1], group):
            groups[-1].extend([*group, *notes])  # соседние абзацы кода это один блок кода
        else:
            groups.append([*group, *notes])
    for group in groups:
        if not group:
            continue
        if lines:
            lines.append(SourceLine(text=""))
        lines.extend(group)
    return lines, _feature_diagnostics(document, path.name)


_STORY_PARTS = re.compile(r"^/word/(?:header|footer|footnotes|endnotes)\d*\.xml$")
_CHANGE_MARKS = frozenset({"rPr", "trPr", "tblPrEx"})
"""Отметки правки внутри свойств (абзаца, строки) не значат, что текст менялся."""


def _story_roots(document: DocxDocument) -> list[etree._Element]:
    """Тело документа, сноски и колонтитулы: везде ищем то, что агент не читает или читает с оговоркой."""
    roots = [document.element]
    for part in document.part.package.iter_parts():
        if _STORY_PARTS.match(str(part.partname)):
            roots.append(etree.fromstring(part.blob, _PARSER))
    return roots


def _has_tracked_changes(root: etree._Element) -> bool:
    return any(
        _local(parent) not in _CHANGE_MARKS
        for mark in root.iter(_w("ins"), _w("del"))
        if (parent := mark.getparent()) is not None
    )


def _feature_diagnostics(document: DocxDocument, name: str) -> list[Diagnostic]:
    roots = _story_roots(document)
    out: list[Diagnostic] = []
    if any(_has_tracked_changes(r) for r in roots):
        out.append(
            Diagnostic(
                code=IN_DOCX_TRACKED_CHANGES,
                level=DiagLevel.INFO,
                message="В документе есть исправления в режиме правки",
                action="Прочитан текст с принятыми исправлениями: вставки включены, удалённый текст пропущен",
                hint="Примите или отклоните исправления в Word, чтобы проверка шла по итоговому тексту",
                location=name,
                step="parse",
            )
        )
    if any(r.find(".//w:txbxContent", NS) is not None for r in roots):
        out.append(
            Diagnostic(
                code=IN_DOCX_TEXT_BOXES,
                level=DiagLevel.WARNING,
                message="В документе есть текстовые рамки (надписи на рисунках и схемах)",
                action="Текст внутри рамок не прочитан, проверка идёт по основному тексту",
                hint="Вынесите текст из рамок в обычные абзацы или пришлите PDF",
                location=name,
                step="parse",
            )
        )
    return out


def parse_docx(path: Path) -> ParsedLines:
    """Абзацы, списки, таблицы, сноски и колонтитулы. Битый архив или XML дают ``InputError`` IN-032."""
    try:
        lines, diagnostics = _read(path)
    except (PackageNotFoundError, zipfile.BadZipFile, KeyError, ValueError, etree.XMLSyntaxError) as exc:
        raise InputError(
            f"DOCX не открывается: {type(exc).__name__}",
            code=IN_DOCX_BROKEN,
            hint="Пересохраните документ в Word или пришлите PDF",
        ) from exc
    problems = text_diagnostics(lines, DocKind.DOCX, path.name)
    if any(d.level is DiagLevel.ERROR for d in problems):
        lines = []
    return ParsedLines(
        lines=lines,
        extraction="python-docx: абзацы, списки, таблицы по ячейкам, сноски и колонтитулы",
        diagnostics=[*diagnostics, *problems],
    )
