"""Поиск двух колонок в тексте вне таблиц по устойчивой свободной полосе между ними."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass
from itertools import pairwise
from statistics import median

from validator.ingest.pdf_backends import Glyph, Rule

GUTTER_EM = 1.0  # Полоса шириной в кегль заметно шире обычного пробела.
NARROW_GUTTER_EM = 0.7  # Более тесный, но повторяющийся зазор требует сверки с оригиналом.
ALIGN_EM = 0.4  # Допуск выравнивания левого края правой колонки.
MARGIN_SHARE = 0.15  # Ищем внутри текстовой области, чтобы не принять номера списка за колонку.
NEIGHBOUR_EM = 0.85  # Допускаем сдвиг базовых линий одной колонки относительно другой.
MIN_PAIRED_LINES = 2  # Меньше двух соседств по высоте не свидетельствуют о колонках.
MIN_COLUMN_LINES = 4
MIN_HEIGHT_SHARE = 0.55  # Общая высота колонок относительно занятой текстом высоты.
MIN_PROSE_EM = 7.0  # Короткие значения в двух столбцах могут быть ячейками таблицы без линеек.
MAX_CROSSING_SHARE = 0.4  # Широкие пробелы в отдельных строках выключки не образуют полосу.
MIN_TABLE_RULES = 2  # Несколько горизонтальных границ в теле и у края ячеек указывают на сетку.


@dataclass(frozen=True)
class Columns:
    split: float
    first: int  # Строки до и после этой области сохраняют физический порядок.
    stop: int
    certain: bool


def _aligned_edges(rows: list[list[Glyph]], em: float) -> list[list[float]]:
    """Начала строк и фрагментов после широкого пробела, сгруппированные по X."""
    align = ALIGN_EM * em
    x0 = min(row[0].x0 for row in rows)
    x1 = max(row[-1].x1 for row in rows)
    margin = (x1 - x0) * MARGIN_SHARE
    starts: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        edges = [row[0].x0, *(b.x0 for a, b in pairwise(row) if b.x0 - a.x1 >= NARROW_GUTTER_EM * em)]
        for x in edges:
            if x0 + margin < x < x1 - margin:
                starts[round(x / align)].append(x)
    return list(starts.values())


def _inside_cell_grid(rules: list[Rule], split: float, paired: list[float], em: float) -> bool:
    """У сетки из отдельных отрезков может не быть общей черты под шапкой, которую ищет #33."""
    by_height: dict[float, set[tuple[float, float]]] = defaultdict(set)
    for rule in rules:
        by_height[rule.y].add((rule.x0, rule.x1))
    levels: dict[tuple[float, float], list[float]] = defaultdict(list)
    for y, segments in by_height.items():
        for x0, x1 in segments:
            if x0 - em <= split <= x1 + em and (
                len(segments) > 1 or min(abs(split - x0), abs(split - x1)) <= em
            ):
                levels[x0, x1].append(y)
    bands = [(min(ys) - em, max(ys) + em) for ys in levels.values() if len(ys) >= MIN_TABLE_RULES]
    return sum(not any(top <= y <= bottom for top, bottom in bands) for y in paired) < MIN_PAIRED_LINES


def _lonely(mine: list[float], other: list[float], radius: float, top: float, bottom: float) -> int:
    """Строки колонки внутри общей высоты, напротив которых в соседней колонке пусто."""
    other = sorted(other)
    return sum(
        top < y < bottom and bisect_left(other, y - radius) == bisect_right(other, y + radius) for y in mine
    )


def _borderless_table(left: list[float], right: list[float], radius: float) -> bool:
    """Таблица без линеек («термин | определение»), а не колонки текста.

    Ячейки такой таблицы разной высоты: напротив хвоста длинной ячейки в соседней колонке пусто, а сама
    колонка длинных ячеек идёт сплошь. У колонок текста пустые места напротив бывают с обеих сторон (это
    промежутки между абзацами, они в двух колонках на разной высоте) или их нет совсем. Читать таблицу
    колонками значит отделить термины от определений, поэтому такая раскладка неуверенная.
    """
    top, bottom = max(min(left), min(right)), min(max(left), max(right))
    holes = (_lonely(left, right, radius, top, bottom), _lonely(right, left, radius, top, bottom))
    return min(holes) == 0 < max(holes)


def detect_columns(rows: list[list[Glyph]], rules: list[Rule]) -> Columns | None:
    """Границы колонок и уверенность в порядке чтения. None означает обычную построчную раскладку."""
    if len(rows) < MIN_PAIRED_LINES:
        return None
    ordered = [sorted(row, key=lambda g: g.x0) for row in rows]
    em = median(g.size for row in rows for g in row)
    if em <= 0:
        return None
    starts = _aligned_edges(ordered, em)
    edge = max(starts, key=len, default=[])
    if len(edge) < MIN_PAIRED_LINES:
        return None
    hi = min(edge)
    lo = hi - NARROW_GUTTER_EM * em
    split = (lo + hi) / 2
    left: list[float] = []
    right: list[float] = []
    left_widths: list[float] = []
    right_widths: list[float] = []
    crossing: list[int] = []
    narrow: list[int] = []
    centers = [(min(g.top for g in row) + max(g.bottom for g in row)) / 2 for row in rows]
    for i, row in enumerate(ordered):
        cut = bisect_left(row, split, key=lambda g: g.x0)
        if (cut and row[cut - 1].x1 > lo) or (cut < len(row) and row[cut].x0 < hi):
            crossing.append(i)
            continue
        if cut and row[cut - 1].x1 > hi - GUTTER_EM * em:
            narrow.append(i)
        if any(g.char.isalpha() for g in row[:cut]):
            left.append(centers[i])
            left_widths.append(row[cut - 1].x1 - row[0].x0)
        if any(g.char.isalpha() for g in row[cut:]):
            right.append(centers[i])
            right_widths.append(row[-1].x1 - row[cut].x0)

    # Одного совпадения проекций по Y мало: абзац с большим отступом тоже лежит между обычными.
    right.sort()
    radius = NEIGHBOUR_EM * em
    paired = [y for y in left if bisect_left(right, y - radius) < bisect_right(right, y + radius)]
    if len(paired) < MIN_PAIRED_LINES or (
        len(paired) < MIN_COLUMN_LINES and len(crossing) > MAX_CROSSING_SHARE * len(rows)
    ):
        return None
    top, bottom = min(paired), max(paired)
    first = max((i + 1 for i in crossing if centers[i] < top), default=0)
    stop = min((i for i in crossing if centers[i] > bottom), default=len(rows))
    interruptions = sum(first <= i < stop for i in crossing)
    if interruptions > MAX_CROSSING_SHARE * (stop - first) or _inside_cell_grid(rules, split, paired, em):
        return None

    certain = (
        len(paired) >= MIN_COLUMN_LINES
        and bottom - top >= MIN_HEIGHT_SHARE * (max(centers) - min(centers))
        and not interruptions
        and not any(first <= i < stop for i in narrow)
        and not any(top < r.y < bottom and r.x0 - em <= split <= r.x1 + em for r in rules)
        and median(left_widths) >= MIN_PROSE_EM * em
        and median(right_widths) >= MIN_PROSE_EM * em
        and not _borderless_table(left, right, radius)
        and not any(
            len(other) >= MIN_COLUMN_LINES and abs(min(other) - hi) >= MIN_PROSE_EM * em for other in starts
        )
    )
    return Columns(split, first, stop, certain)
