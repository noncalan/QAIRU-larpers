"""Таблицы PDF по ячейкам (шаг 1, #33): строки и колонки из геометрии символов.

У большинства PDF с требованиями у таблицы нет рамки: есть только черта под строкой заголовка
(``Rule``), а колонки это столбцы текста. Текстовый вывод библиотек склеивает такую таблицу в строки
через все колонки («Исходные условия Действие Результат Расходы 48 500 …»), поэтому требования из соседних
ячеек сливаются и у цитаты нет якоря ячейки. Здесь таблица находится по черте и читается по ячейкам.

Работает одинаково для любого движка (``pdf_backends``), общие границы заданы в долях кегля:

1. ``find_tables``: черта шире ``MIN_RULE_WIDTH`` с текстом над ней в двух и больше колонках это
   заголовок таблицы. Колонки это начала фрагментов текста нижней строки заголовка (фрагмент оканчивается
   зазором шире ``COLUMN_GAP_EM``, у обычного пробела зазор около 0,25 кегля).
2. Тело это строки под чертой, пока строка не нарушает структуру: фрагмент текста пересекает границу колонки
   (это абзац, а не строки таблицы), зазор до предыдущей строки больше ``END_GAP_EM`` кеглей (следующий
   заголовок или абзац) или кончилась страница.
3. Строки тела делятся по зазору: внутри ячейки шаг строк минимальный, между строками таблицы он больше
   на межстрочный отступ ячейки. Если все зазоры равны, каждая визуальная строка это строка таблицы.
4. ``link_tables`` склеивает продолжение таблицы на следующей странице (повторённый заголовок) и присоединяет
   к последней строке прошлой страницы хвост ячейки, перенесённый на новую страницу.

Что не умеем и честно оставляем обычным текстом: таблицы без черты под заголовком, ячейки с выравниванием
по правому краю или по центру, вложенные и объединённые ячейки.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from itertools import pairwise

from validator.ingest.pdf_backends import Glyph, RawPage, Rule
from validator.ingest.pdf_layout import BAND_SHARE, cluster_rows, is_page_number, join_hyphenation, row_text

CELL_SEPARATOR = " | "
"""Ячейки строки в тексте документа (как в DOCX): «Расходы 48 500 ₸ | Провести покупку | Итог»."""

COLUMN_GAP_EM = 0.9
"""Зазор шире стольких кеглей отделяет ячейки в строке; внутри ячейки это пробел (около 0,25 кегля)."""

COLUMN_TOL_EM = 0.4
"""Фрагмент текста относится к колонке, чьё начало не правее его начала плюс столько кеглей."""

HEADER_STEP_EM = 1.3
"""Строки заголовка ячейки в несколько строк ближе стольких кеглей; заголовок раздела над таблицей дальше."""

END_GAP_EM = 2.0
"""Зазор между строками больше стольких кеглей это конец таблицы (следующий заголовок или абзац)."""

ROW_PAD_EM = 0.15
"""Скачок между зазорами «внутри ячейки» и «между строками» должен быть больше стольких кеглей, иначе все
зазоры считаем равными и каждая визуальная строка это строка таблицы."""

MIN_COLUMNS = 2
X_PAD = 6.0
"""Текст таблицы может выходить за концы черты не дальше стольких пунктов."""
SIZE_TOLERANCE = 1.0
CONTINUATION_BOTTOM = 0.7
CONTINUATION_TOP = 0.2
"""Таблица продолжается на следующей странице, если кончилась в нижних 30 % страницы, а повторённый
заголовок начинается в верхних 20 %."""


@dataclass(frozen=True)
class TableRow:
    """Строка таблицы: ячейки по колонкам (пустая ячейка это пустая строка) и вертикаль на странице."""

    cells: list[str]
    page: int
    top: float
    bottom: float


@dataclass(frozen=True)
class PageTable:
    """Таблица на одной странице: заголовок и строки тела."""

    page: int
    x0: float
    x1: float
    top: float
    bottom: float
    header: list[str]
    rows: list[TableRow] = field(default_factory=list)
    starts: tuple[float, ...] = ()

    @property
    def columns(self) -> int:
        return len(self.header)

    def contains(self, glyph: Glyph) -> bool:
        """Символ прочитан этой таблицей: его строка не пойдёт в поток абзацев."""
        centre = (glyph.top + glyph.bottom) / 2
        return (
            self.top - 1 <= centre <= self.bottom + 1
            and self.x0 - X_PAD <= glyph.x0
            and glyph.x1 <= self.x1 + X_PAD
        )


@dataclass(frozen=True)
class LogicalTable:
    """Таблица документа целиком: заголовок и все строки со всех страниц, по порядку."""

    number: int
    header: list[str]
    rows: list[TableRow]
    parts: list[PageTable]


# ---------------------------------------------------------------- фрагменты текста


@dataclass(frozen=True)
class _Run:
    """Фрагмент строки между широкими зазорами: слово или несколько слов одной ячейки."""

    glyphs: list[Glyph]
    x0: float
    x1: float

    @property
    def size(self) -> float:
        return max((g.size for g in self.glyphs), default=10.0) or 10.0


def _split_runs(row: list[Glyph]) -> list[_Run]:
    runs: list[list[Glyph]] = []
    for glyph in sorted(row, key=lambda g: g.x0):
        if runs and glyph.x0 - runs[-1][-1].x1 <= COLUMN_GAP_EM * (glyph.size or 10.0):
            runs[-1].append(glyph)
        else:
            runs.append([glyph])
    return [_Run(run, min(g.x0 for g in run), max(g.x1 for g in run)) for run in runs]


@dataclass(frozen=True)
class _Row:
    """Визуальная строка внутри таблицы."""

    runs: list[_Run]
    center: float
    top: float
    bottom: float
    size: float
    bold: bool


def _make_row(glyphs: list[Glyph]) -> _Row:
    solid = [g for g in glyphs if g.char.isalnum()] or glyphs
    sizes = sorted(g.size for g in solid)
    return _Row(
        runs=_split_runs(glyphs),
        center=(min(g.top for g in glyphs) + max(g.bottom for g in glyphs)) / 2,
        top=min(g.top for g in glyphs),
        bottom=max(g.bottom for g in glyphs),
        size=sizes[len(sizes) // 2] or 10.0,
        bold=sum(1 for g in solid if g.bold) / len(solid) >= 0.8,
    )


def _rows_in(page: RawPage, x0: float, x1: float) -> list[_Row]:
    glyphs = [g for g in page.glyphs if g.x0 >= x0 - X_PAD and g.x1 <= x1 + X_PAD]
    return [_make_row(row) for row in cluster_rows(glyphs)]


def _straddles(row: _Row, starts: list[float], size: float) -> bool:
    """Фрагмент строки перекрывает начало колонки: так выглядит абзац, а не ячейки таблицы."""
    tol = COLUMN_TOL_EM * size
    return any(
        run.x0 < start - tol and run.x1 > start + tol for run in row.runs for start in starts[1:]
    )  # fmt: skip


def _column_of(run: _Run, starts: list[float], size: float) -> int:
    tol = COLUMN_TOL_EM * size
    index = 0
    for k, start in enumerate(starts):
        if run.x0 + tol >= start:
            index = k
    return index


# ---------------------------------------------------------------- заголовок


@dataclass(frozen=True)
class _Header:
    rows: list[_Row]
    starts: list[float]


def _find_header(page: RawPage, rule: Rule) -> _Header | None:
    """Строки заголовка над чертой и начала колонок; None, если над чертой нет таблицы."""
    above = [row for row in _rows_in(page, rule.x0, rule.x1) if row.center < rule.y]
    if not above:
        return None
    bottom = above[-1]
    if bottom.center < rule.y - END_GAP_EM * bottom.size or len(bottom.runs) < MIN_COLUMNS:
        return None
    starts = [run.x0 for run in bottom.runs]
    rows = [bottom]
    for row in reversed(above[:-1]):
        near = rows[0].center - row.center <= HEADER_STEP_EM * rows[0].size
        same_style = abs(row.size - bottom.size) <= SIZE_TOLERANCE and row.bold == bottom.bold
        if not (near and same_style and not _straddles(row, starts, row.size)):
            break
        rows.insert(0, row)
        tol = COLUMN_TOL_EM * row.size
        starts += [
            run.x0 for run in row.runs if not any(b.x0 - tol <= run.x0 <= b.x1 + tol for b in bottom.runs)
        ]  # ячейка заголовка без текста в нижней строке: своя колонка
    return _Header(rows=rows, starts=sorted(starts))


# ---------------------------------------------------------------- тело


def _join_cell(parts: list[str]) -> str:
    """Строки ячейки в текст через пробел; перенос слова со знаком склеиваем, как в абзацах.

    Слово, разорванное у края узкой колонки без знака переноса («Функциональна» и «я»), остаётся с пробелом:
    отличить его от обычного переноса по пробелу можно только словарём.
    """
    text = parts[0]
    for nxt in parts[1:]:
        head, _, rest = nxt.partition(" ")
        glued = join_hyphenation(text, head) if head else None
        text = f"{glued} {rest}".strip() if glued is not None and rest else (glued or f"{text} {nxt}")
    return text


def _row_threshold(gaps: list[float], size: float) -> float | None:
    """Зазор, начиная с которого строки относятся к разным строкам таблицы; None, если зазоры равны.

    Зазоры внутри ячейки (шаг строк) и между строками таблицы (шаг плюс отступ ячейки) образуют две кучки:
    граница лежит посередине самого большого скачка среди упорядоченных зазоров.
    """
    ordered = sorted(gaps)
    jumps = [(b - a, (a + b) / 2) for a, b in pairwise(ordered)]
    if not jumps:
        return None
    jump, middle = max(jumps)
    return middle if jump > ROW_PAD_EM * size else None


def _split_rows(lines: list[_Row], size: float) -> list[list[_Row]]:
    """Делит строки тела на строки таблицы по зазору (см. шаг 3 в описании модуля)."""
    if not lines:
        return []
    gaps = [b.center - a.center for a, b in pairwise(lines)]
    threshold = _row_threshold(gaps, size)
    groups: list[list[_Row]] = [[lines[0]]]
    for gap, line in zip(gaps, lines[1:], strict=True):
        if threshold is None or gap > threshold:
            groups.append([line])
        else:
            groups[-1].append(line)
    return groups


def _is_page_number_row(row: _Row) -> bool:
    return len(row.runs) == 1 and is_page_number(row_text(row.runs[0].glyphs, 0.0))


def _body_lines(page: RawPage, rule: Rule, header: _Header, limit: float) -> list[_Row]:
    """Строки под чертой, пока они похожи на строки таблицы."""
    below = [row for row in _rows_in(page, rule.x0, rule.x1) if rule.y < row.center < limit]
    size = header.rows[-1].size
    out: list[_Row] = []
    previous = rule.y
    for row in below:
        gap = row.center - previous
        if gap > END_GAP_EM * max(size, row.size) or _straddles(row, header.starts, row.size):
            break
        if row.center > (1 - BAND_SHARE) * page.height and _is_page_number_row(row):
            break  # номер страницы под таблицей: колонтитул, не строка таблицы
        out.append(row)
        previous = row.center
    return out


def _cell_text(run: _Run, gap_em: float) -> str:
    return row_text(run.glyphs, gap_em)


def _build_rows(
    groups: list[list[_Row]], starts: list[float], size: float, page: int, gap_em: float
) -> list[TableRow]:
    rows: list[TableRow] = []
    for group in groups:
        pieces: list[list[str]] = [[] for _ in starts]
        for line in group:
            for run in line.runs:
                pieces[_column_of(run, starts, size)].append(_cell_text(run, gap_em))
        cells = [_join_cell(p) if p else "" for p in pieces]
        rows.append(TableRow(cells, page, group[0].top, group[-1].bottom))
    return rows


def _header_cells(header: _Header, gap_em: float) -> list[str]:
    starts = header.starts
    size = header.rows[-1].size
    pieces: list[list[str]] = [[] for _ in starts]
    for line in header.rows:
        for run in line.runs:
            pieces[_column_of(run, starts, size)].append(_cell_text(run, gap_em))
    return [_join_cell(p) if p else "" for p in pieces]


# ---------------------------------------------------------------- страница


def _is_interior(page: RawPage, previous: tuple[Rule, _Header], rule: Rule, header: _Header) -> bool:
    """Линейка не открывает новую таблицу: это черта между строками или под последней строкой прошлой.

    Строки над ней ложатся в те же колонки, что у прошлой таблицы, и идут к ней вплотную. Так выглядит
    таблица с рамкой или с разделителями строк: первую черту считаем заголовком, остальные пропускаем.
    """
    prev_rule, prev_header = previous
    top = header.rows[0].top
    if top < prev_rule.y:
        return True  # строки над линейкой это заголовок прошлой таблицы
    body = _body_lines(page, prev_rule, prev_header, top)
    end = body[-1].bottom if body else prev_rule.y
    size = header.rows[-1].size
    tol = COLUMN_TOL_EM * size
    same = len(header.starts) == len(prev_header.starts) and all(
        abs(a - b) <= tol for a, b in zip(header.starts, prev_header.starts, strict=True)
    )
    return same and top - end <= END_GAP_EM * size


def find_tables(page: RawPage, gap_em: float) -> list[PageTable]:
    """Таблицы страницы сверху вниз. Страница без линеек и без колонок текста даёт пустой список."""
    headers: list[tuple[Rule, _Header]] = []
    for rule in sorted(page.rules, key=lambda r: r.y):
        header = _find_header(page, rule)
        if header is None or (headers and _is_interior(page, headers[-1], rule, header)):
            continue
        headers.append((rule, header))
    tables: list[PageTable] = []
    for k, (rule, header) in enumerate(headers):
        limit = headers[k + 1][1].rows[0].top if k + 1 < len(headers) else page.height
        body = _body_lines(page, rule, header, limit)
        size = header.rows[-1].size
        rows = _build_rows(_split_rows(body, size), header.starts, size, page.number, gap_em)
        last = body[-1].bottom if body else rule.y
        tables.append(
            PageTable(
                page=page.number,
                x0=rule.x0,
                x1=rule.x1,
                top=header.rows[0].top,
                bottom=last,
                header=_header_cells(header, gap_em),
                rows=rows,
                starts=tuple(header.starts),
            )
        )
    return tables


# ---------------------------------------------------------------- документ


def _key(cells: list[str]) -> str:
    return " ".join(" ".join(cells).split()).casefold()


def _continues(prev: PageTable, cur: PageTable, heights: dict[int, float]) -> bool:
    """Таблица ``cur`` продолжает ``prev``: следующая страница, тот же заголовок, края страницы."""
    if cur.page != prev.page + 1 or _key(cur.header) != _key(prev.header) or cur.columns != prev.columns:
        return False
    return (
        prev.bottom >= CONTINUATION_BOTTOM * heights[prev.page]
        and cur.top <= CONTINUATION_TOP * heights[cur.page]
    )


def _is_tail(row: TableRow) -> bool:
    """Первая строка новой страницы это хвост ячейки прошлой, если её первая ячейка пуста или со строчной."""
    first = row.cells[0] if row.cells else ""
    return not first or first[0].islower()


def _merge_tail(last: TableRow, tail: TableRow) -> TableRow:
    cells = [f"{a} {b}".strip() if a and b else a or b for a, b in zip(last.cells, tail.cells, strict=True)]
    return replace(last, cells=cells)


def link_tables(tables: list[PageTable], heights: dict[int, float]) -> list[LogicalTable]:
    """Склеивает таблицы с повторённым заголовком на соседних страницах и нумерует таблицы документа.

    ``heights``: высоты страниц по номерам; продолжение ищем внизу одной страницы и вверху следующей.
    """
    out: list[LogicalTable] = []
    previous: PageTable | None = None
    for table in tables:
        rows = list(table.rows)
        if previous is not None and out and _continues(previous, table, heights):
            current = out[-1]
            if rows and _is_tail(rows[0]) and current.rows:
                current.rows[-1] = _merge_tail(current.rows[-1], rows.pop(0))
            current.rows.extend(rows)
            current.parts.append(table)
        else:
            out.append(LogicalTable(len(out) + 1, list(table.header), rows, [table]))
        previous = table
    return out
