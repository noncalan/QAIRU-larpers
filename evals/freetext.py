"""Свободный текст базовой линии: находки по отдельности и цитаты из текста.

Протокол eval требует, чтобы судья видел находки агента и базовой линии одинаковым пронумерованным
списком. Базовая линия пишет свободный текст, поэтому код режет его на находки сам (без модели:
перенос в JSON через Haiku к базовой линии не применяем, docs/plan.md).

Нарезка пробует три разметки и берёт ту, где больше находок с цитатами:

1. таблицы, в заголовке которых есть столбец с цитатой («Цитата», «Фрагмент»): строка = находка;
2. заголовки Markdown одного уровня: раздел под заголовком = находка;
3. пункты нумерованного списка верхнего уровня: пункт = находка.

Разделы-итоги («Итог», «Вывод», «Шкала», «Предлагаемые формулировки» …) находками не считаются.

Цитаты для доли сверенных достаёт эвристика исследования 11 (``score_quotes.py``) без изменений,
кроме одного: экранированный разделитель таблицы ``\\|`` сначала становится ``|``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from validator.models import ParsedDocument, QuoteMatch
from validator.text import locate_quote, normalize

# ---------------------------------------------------------------- цитаты (исследование 11)

MIN_SEGMENT_CHARS = 6
"""Сегмент цитаты короче этого после нормализации не проверяем (исследование 11 и 21)."""

_GUILLEMETS = re.compile(r"«([^«»\n]+)»")
_BACKTICKS = re.compile(r"`([^`\n]+)`")
_DOUBLE_QUOTES = re.compile(r'["“„]([^"“”„\n]{15,})["”“]')
_LABEL = re.compile(r"^[^:«»]{1,25}:\s*")
_SEGMENT_SPLIT = re.compile(r"…|\.\.\.|\s/\s|\s\|\s")
_PROPOSALS = re.compile(r"^#+\s*Предлагаемые формулировки.*$", re.MULTILINE)
_ESCAPED_PIPE = re.compile(r"\\\|")
MIN_BACKTICK_CHARS = 12


def _blank(match: re.Match[str]) -> str:
    return " " * len(match.group(0))


def extract_quotes(text: str) -> list[str]:
    """Цитаты из свободного текста по эвристике исследования 11, по порядку правил.

    «Ёлочки» в одной строке; строки «> …» без «ёлочек» (без метки «Ф-2:»); `…` от 12 символов
    или с пробелом; "…" от 15 символов. Найденное маскируется и второй раз не берётся. Всё после
    заголовка «Предлагаемые формулировки» отбрасывается: там модель пишет свои формулировки.
    """
    text = _ESCAPED_PIPE.sub("|", text)
    cut = _PROPOSALS.search(text)
    if cut:
        text = text[: cut.start()]
    text = text.replace("**", "")
    quotes = [m.group(1) for m in _GUILLEMETS.finditer(text)]
    work = _GUILLEMETS.sub(_blank, text)
    lines = work.split("\n")
    for masked, original in zip(lines, text.split("\n"), strict=True):
        if masked.lstrip().startswith(">") and "«" not in original:
            body = _LABEL.sub("", masked.lstrip()[1:].strip())
            if body:
                quotes.append(body)
    work = "\n".join(" " * len(line) if line.lstrip().startswith(">") else line for line in lines)
    for m in _BACKTICKS.finditer(work):
        inner = m.group(1).strip()
        if len(inner) >= MIN_BACKTICK_CHARS or " " in inner:
            quotes.append(m.group(1))
    work = _BACKTICKS.sub(_blank, work)
    quotes.extend(m.group(1) for m in _DOUBLE_QUOTES.finditer(work))
    return quotes


def quote_segments(quote: str) -> list[str]:
    """Сегменты цитаты, склеенной через «…», « / », « | »; короткие (< ``MIN_SEGMENT_CHARS``) отброшены."""
    return [s for s in _SEGMENT_SPLIT.split(quote) if len(normalize(s)[0]) >= MIN_SEGMENT_CHARS]


def check_quote(doc: ParsedDocument | str, quote: str) -> QuoteMatch | None:
    """Сверка цитаты свободного текста тем же ``locate_quote``, что у агента.

    Модель склеивает куски через «…», « / », « | », поэтому цитата режется на сегменты; вердикт
    по худшему сегменту (исследование 11). ``None``: проверять нечего (все сегменты короткие).
    """
    segments = quote_segments(quote)
    if not segments:
        return None
    matches = [locate_quote(doc, s).match for s in segments]
    if QuoteMatch.NOT_FOUND in matches:
        return QuoteMatch.NOT_FOUND
    if QuoteMatch.FUZZY in matches:
        return QuoteMatch.FUZZY
    return QuoteMatch.EXACT


# ---------------------------------------------------------------- нарезка на находки


@dataclass(frozen=True)
class FreeFinding:
    """Находка свободного текста: весь её текст и поля, если их удалось выделить."""

    text: str
    quote: str = ""
    property: str = ""
    explanation: str = ""
    layout: str = ""


@dataclass(frozen=True)
class Split:
    findings: list[FreeFinding]
    layout: str
    notes: list[str] = field(default_factory=list)


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CELL = re.compile(r"(?<!\\)\|")
_LIST_ITEM = re.compile(r"^\s{0,3}(?:\*\*)?\s*(\d{1,3})[.)](?:\*\*)?\s+\S")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_SUMMARY_TITLE = re.compile(
    r"итог|вывод|резюме|сводк|заключени|рекомендац|предлагаем|шкал|обозначен|легенд|методик|"
    r"что исправить|приоритет исправлен|общая оценка|содержание",
    re.IGNORECASE,
)
_QUOTE_COLUMN = re.compile(r"цитат|фрагмент", re.IGNORECASE)
_PROPERTY_COLUMN = re.compile(r"свойств|критери|характеристик", re.IGNORECASE)
_NUMBER_COLUMN = re.compile(r"^(№|#|n|no\.?|номер|id)$", re.IGNORECASE)
_SEVERITY_COLUMN = re.compile(r"критичн|серь[её]зн|важност|severity", re.IGNORECASE)
_PROPERTY_FIELD = re.compile(
    r"^\s*[-*]?\s*\**\s*(?:наруш\w*\s+)?(?:свойств\w*|критери\w*|характеристик\w*)\s*\**\s*:\s*\**\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
QUOTE_JOIN = " … "


def _cells(line: str) -> list[str]:
    body = line.strip()
    body = body.removeprefix("|").removesuffix("|")
    return [_ESCAPED_PIPE.sub("|", c).strip() for c in _CELL.split(body)]


def _clean(text: str) -> str:
    return text.replace("**", "").strip()


def _iter_tables(lines: list[str]) -> Iterator[tuple[list[str], list[list[str]]]]:
    """Таблицы Markdown: (заголовок, строки). Таблица = строка, разделитель «---», строки."""
    k = 0
    while k + 1 < len(lines):
        if _TABLE_ROW.match(lines[k]) and _TABLE_SEPARATOR.match(lines[k + 1]):
            header = _cells(lines[k])
            rows: list[list[str]] = []
            j = k + 2
            while j < len(lines) and _TABLE_ROW.match(lines[j]):
                rows.append(_cells(lines[j]))
                j += 1
            yield header, rows
            k = j
        else:
            k += 1


def _column(header: list[str], pattern: re.Pattern[str]) -> int | None:
    return next((i for i, h in enumerate(header) if pattern.search(_clean(h))), None)


def _table_row(header: list[str], row: list[str], quote_col: int) -> FreeFinding:
    prop_col = _column(header, _PROPERTY_COLUMN)
    skip = {quote_col, prop_col}
    rest = []
    for i, cell in enumerate(row):
        name = _clean(header[i]) if i < len(header) else f"колонка {i + 1}"
        if i in skip or not cell or _NUMBER_COLUMN.match(name) or _SEVERITY_COLUMN.search(name):
            continue
        rest.append(_clean(cell))
    text = "\n".join(
        f"{_clean(header[i]) if i < len(header) else f'колонка {i + 1}'}: {_clean(c)}"
        for i, c in enumerate(row)
        if c
    )
    return FreeFinding(
        text=text,
        quote=_clean(row[quote_col]) if quote_col < len(row) else "",
        property=_clean(row[prop_col]) if prop_col is not None and prop_col < len(row) else "",
        explanation="\n".join(rest),
        layout="table",
    )


def _by_tables(lines: list[str]) -> list[FreeFinding]:
    out: list[FreeFinding] = []
    for header, rows in _iter_tables(lines):
        quote_col = _column(header, _QUOTE_COLUMN)
        if quote_col is None:
            continue
        out.extend(_table_row(header, row, quote_col) for row in rows if any(row))
    return out


def _chunk_finding(chunk: str, layout: str) -> FreeFinding:
    quotes = extract_quotes(chunk)
    prop = _PROPERTY_FIELD.search(chunk)
    return FreeFinding(
        text=_clean(chunk),
        quote=QUOTE_JOIN.join(q.strip() for q in quotes),
        property=_clean(prop.group(1)) if prop else "",
        explanation=_clean(chunk),
        layout=layout,
    )


def _by_headings(lines: list[str]) -> list[FreeFinding]:
    """Лучший уровень заголовков: раздел до следующего заголовка того же или старшего уровня."""
    heads = [(k, len(m.group(1)), m.group(2)) for k, line in enumerate(lines) if (m := _HEADING.match(line))]
    best: list[FreeFinding] = []
    for level in sorted({lvl for _, lvl, _ in heads}):
        found: list[FreeFinding] = []
        for n, (start, lvl, title) in enumerate(heads):
            if lvl != level or _SUMMARY_TITLE.search(title):
                continue
            end = next((k for k, lv, _ in heads[n + 1 :] if lv <= level), len(lines))
            chunk = "\n".join(lines[start:end]).strip()
            if extract_quotes(chunk):
                found.append(_chunk_finding(chunk, "headings"))
        if len(found) > len(best):
            best = found
    return best


def _ends_list(line: str) -> bool:
    return bool(_HEADING.match(line) or _RULE.match(line) or _TABLE_ROW.match(line))


def _by_list(lines: list[str]) -> list[FreeFinding]:
    """Пункты нумерованного списка; раздел-итог («Итог», «Что исправить») пропускается целиком.

    Пункт продолжается строками с отступом и строками сразу под ним; после пустой строки
    строка без отступа (новый абзац, другой список) пункт заканчивает. Вложенный нумерованный
    пункт (отступ больше, чем у пунктов верхнего уровня) остаётся частью текущей находки.
    """
    out: list[FreeFinding] = []
    current: list[str] = []
    in_summary = False
    after_blank = False
    base: int | None = None  # отступ пунктов верхнего уровня текущего списка

    def flush() -> None:
        chunk = "\n".join(current).strip()
        if chunk and not in_summary and extract_quotes(chunk):
            out.append(_chunk_finding(chunk, "list"))
        current.clear()

    for line in lines:
        heading = _HEADING.match(line)
        if heading or _ends_list(line):
            flush()
            base = None
            if heading:
                in_summary = bool(_SUMMARY_TITLE.search(heading.group(2)))
            continue
        if _LIST_ITEM.match(line):
            indent = len(line) - len(line.lstrip())
            if not (current and base is not None and indent > base):
                flush()
                base = indent
            current.append(line)
        elif current and not line.strip():
            current.append(line)
        elif current and after_blank and not line.startswith((" ", "\t")):
            flush()
            base = None
        elif current:
            current.append(line)
        after_blank = not line.strip()
    flush()
    return out


_STRATEGIES: tuple[tuple[str, Callable[[list[str]], list[FreeFinding]]], ...] = (
    ("table", _by_tables),
    ("headings", _by_headings),
    ("list", _by_list),
)


def _with_quote(findings: list[FreeFinding]) -> int:
    return sum(bool(f.quote.strip()) for f in findings)


def split_findings(text: str) -> Split:
    """Находки свободного текста. При равенстве выигрывает таблица, затем заголовки, затем список."""
    lines = text.replace("\r\n", "\n").split("\n")
    best = Split(findings=[], layout="none")
    for name, strategy in _STRATEGIES:
        found = strategy(lines)
        if (_with_quote(found), len(found)) > (_with_quote(best.findings), len(best.findings)):
            best = Split(findings=found, layout=name)
    if best.findings:
        return best
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    findings = [_chunk_finding(p, "paragraph") for p in paragraphs if extract_quotes(p)]
    note = "разметка ответа не распознана: находки нарезаны по абзацам с цитатами"
    return Split(findings=findings, layout="paragraph", notes=[note])
