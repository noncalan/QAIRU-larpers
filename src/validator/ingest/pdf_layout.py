"""Раскладка PDF: символы с координатами → строки в порядке чтения, абзацы, заголовки, колонтитулы.

Работает одинаково для любого движка (``pdf_backends``). Всё общее, без слов конкретного документа:
числовые границы заданы в долях кегля и межстрочного шага, стиль заголовков берётся из самого документа
(размер, жирность, курсив), колонтитулом считается только то, что повторяется на страницах.

Шаги:

1. ``build_lines``: символы страницы → визуальные строки. Строку определяет вертикальный центр (допуск
   полкегля), порядок слева направо, пробел ставит зазор между символами (``space_gap_em``).
2. ``find_furniture``: повторяющийся верх и низ страниц и номера страниц.
3. ``LayoutStats``: основной кегль и межстрочный шаг документа.
4. ``assign_headings``: заголовки по кеглю, жирности, курсиву или нумерации; уровень по рангу стиля.
5. ``group_lines``: абзацы по вертикальному зазору, абзацному отступу и концу предложения на стыке
   страниц; склейка переносов слов.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from itertools import pairwise
from statistics import median

from validator.ingest.blocks import LIST_ITEM
from validator.ingest.pdf_backends import Glyph, RawPage

# ---------------------------------------------------------------- границы и словари

LINE_TOLERANCE_EM = 0.5
"""Символы с вертикальными центрами ближе этого (в кеглях) лежат на одной строке: знаки препинания
в другом шрифте сдвинуты на 1–2 пункта, а межстрочный шаг около 1,2 кегля."""

DUPLICATE_SHIFT_EM = 0.12
"""Тот же символ левее этого сдвига это «жирность наложением» (текст напечатан дважды), а не повтор буквы."""

HEADING_SIZE_DELTA = 1.0
"""Строка крупнее основного кегля на столько пунктов и больше считается заголовком по размеру."""

HEADING_GAP = 1.35
"""Отступ перед заголовком того же размера, что и текст, не меньше стольких межстрочных шагов."""

PARAGRAPH_GAP = 1.3
"""Межстрочный зазор больше стольких шагов начинает новый абзац."""

INDENT_EM = 0.8
"""Красная строка: отступ от левого поля страницы больше стольких кеглей."""

WRAPPED_HEADING_STEP = 1.25
"""Две строки заголовка одного стиля ближе стольких кеглей это один заголовок в две строки; у двух разных
заголовков зазор больше, а подпункт с номером («1.1 …») всегда начинает новый заголовок."""

MAX_HEADING_CHARS = 120
MAX_PLAIN_HEADING_CHARS = 80
MAX_HEADING_LEVEL = 6
STYLE_SHARE = 0.8
"""Строка жирная или курсивная, если так набрано не меньше этой доли её букв и цифр."""

BAND_SHARE = 0.12
"""Верхние и нижние 12 % высоты страницы: где ищем колонтитулы."""

MIN_PAGES_FOR_FURNITURE = 3
FURNITURE_PAGE_SHARE = 0.5
"""Строка из верхней или нижней полосы на половине страниц и не меньше чем на трёх это колонтитул."""

GLUE_EM = 0.5
"""Знак препинания, отделённый от слова зазором до стольких кеглей, всё равно приклеиваем: пробел перед
«.» или после «(» в PDF это артефакт вёрстки (код в другом шрифте, знак отдельным фрагментом)."""

CLOSING = frozenset(".,;:!?)]»”")
OPENING = frozenset("([«“")
SENTENCE_END = frozenset('.!?…:;»"”)')
BULLETS = frozenset("•●○▪■◦‣⁃")
SOFT_HYPHEN = "­"
HYPHENS = frozenset("-‐‑")
_PAGE_NUMBER = re.compile(
    r"(?:стр\.?|страница|page|p\.)?\s*\d{1,4}(?:\s*(?:из|of|/)\s*\d{1,4})?|[-–—]\s*\d{1,4}\s*[-–—]"
)
_NUMBERED = re.compile(r"(?:\d+(?:\.\d+)*\.?)\s+\S")
_DIGITS = re.compile(r"\d+")
_SPACES = re.compile(r"\s+")

MAX_SHORT_HEAD = 2
PARTICLES = frozenset({"то", "либо", "нибудь", "таки", "ка", "за"})
"""Хвосты, после которых дефис значим: «что-то», «кто-либо», «из-за». Короткое начало: «по-русски»."""

COMPOUND_PREFIXES = frozenset(
    {
        "бизнес", "веб", "кросс", "интернет", "онлайн", "офлайн", "мульти", "анти", "псевдо", "вице", "экс",
        "контр", "мини", "макс", "видео", "аудио", "мета", "web", "cross", "multi", "anti", "pseudo",
        "non", "self", "real", "full", "open", "back", "front", "e", "x",
    }
)  # fmt: skip
"""Части составных слов: при переносе «бизнес-⏎процесс» дефис остаётся. Общие слова, не слова документа."""


# ---------------------------------------------------------------- строки


@dataclass(frozen=True)
class Line:
    """Визуальная строка страницы."""

    text: str
    page: int
    page_line: int
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    bold: bool = False
    italic: bool = False
    chars: int = 0
    band: str | None = None  # "top" или "bottom": полоса колонтитулов
    heading_level: int | None = None

    @property
    def center(self) -> float:
        return (self.top + self.bottom) / 2


def _style_share(glyphs: Sequence[Glyph], attr: str) -> bool:
    solid = [g for g in glyphs if g.char.isalnum()] or list(glyphs)
    return bool(solid) and sum(1 for g in solid if getattr(g, attr)) / len(solid) >= STYLE_SHARE


def _cluster(glyphs: list[Glyph]) -> list[list[Glyph]]:
    rows: list[list[Glyph]] = []
    centers: list[float] = []
    for glyph in sorted(glyphs, key=lambda g: (g.top + g.bottom) / 2):
        center = (glyph.top + glyph.bottom) / 2
        if rows and abs(center - centers[-1]) <= LINE_TOLERANCE_EM * min(
            glyph.size or 10.0, rows[-1][0].size or 10.0
        ):
            rows[-1].append(glyph)
            centers[-1] += (center - centers[-1]) / len(rows[-1])
        else:
            rows.append([glyph])
            centers.append(center)
    return rows


def _line_text(row: list[Glyph], gap_em: float) -> str:
    out: list[str] = []
    prev: Glyph | None = None
    for glyph in sorted(row, key=lambda g: g.x0):
        if unicodedata.category(glyph.char[0]) == "Cf" and glyph.char != SOFT_HYPHEN:
            continue  # невидимые знаки форматирования; мягкий перенос нужен склейке слов
        if prev is not None:
            size = (prev.size + glyph.size) / 2 or 10.0
            if prev.char == glyph.char and 0 <= glyph.x0 - prev.x0 < DUPLICATE_SHIFT_EM * size:
                continue
            gap = glyph.x0 - prev.x1
            glued = gap <= GLUE_EM * size and (glyph.char in CLOSING or prev.char in OPENING)
            if gap > gap_em * size and not glued:
                out.append(" ")
        out.append(glyph.char)
        prev = glyph
    text = "".join(out)
    if text and text[0] in BULLETS:
        text = "• " + text[1:].lstrip()
    inner = text[:-1].replace(SOFT_HYPHEN, "") + text[-1:] if text else text
    return inner.strip()


def build_lines(page: RawPage, gap_em: float) -> list[Line]:
    """Строки страницы сверху вниз, слева направо. ``page_line`` это порядковый номер строки на странице."""
    lines: list[Line] = []
    for row in _cluster(page.glyphs):
        text = _line_text(row, gap_em)
        if not text:
            continue
        sizes = [g.size for g in row if g.char.isalnum()] or [g.size for g in row]
        top, bottom = min(g.top for g in row), max(g.bottom for g in row)
        center = (top + bottom) / 2
        band = None
        if center < BAND_SHARE * page.height:
            band = "top"
        elif center > (1 - BAND_SHARE) * page.height:
            band = "bottom"
        lines.append(
            Line(
                text=text,
                page=page.number,
                page_line=len(lines) + 1,
                x0=min(g.x0 for g in row),
                x1=max(g.x1 for g in row),
                top=top,
                bottom=bottom,
                size=float(median(sizes)),
                bold=_style_share(row, "bold"),
                italic=_style_share(row, "italic"),
                chars=len(row),
                band=band,
            )
        )
    return lines


# ---------------------------------------------------------------- колонтитулы


def _furniture_key(text: str) -> str:
    return _SPACES.sub(" ", _DIGITS.sub("#", text.casefold())).strip()


def find_furniture(pages: Sequence[Sequence[Line]]) -> set[tuple[int, int]]:
    """Строки-колонтитулы как пары (страница, номер строки на странице).

    Колонтитул это строка верхней или нижней полосы, повторяющаяся (с точностью до цифр) на половине
    страниц и не меньше чем на трёх, либо номер страницы вида «3», «стр. 3 из 8», «— 3 —».
    """
    found: set[tuple[int, int]] = set()
    seen: Counter[str] = Counter()
    for lines in pages:
        keys = {_furniture_key(line.text) for line in lines if line.band}
        seen.update(keys)
    total = len(pages)
    need = max(MIN_PAGES_FOR_FURNITURE, FURNITURE_PAGE_SHARE * total)
    for lines in pages:
        for line in lines:
            if not line.band:
                continue
            key = _furniture_key(line.text)
            repeated = total >= MIN_PAGES_FOR_FURNITURE and seen[key] >= need
            if repeated or _PAGE_NUMBER.fullmatch(key.replace("#", "1")):
                found.add((line.page, line.page_line))
    return found


# ---------------------------------------------------------------- статистика документа


@dataclass(frozen=True)
class LayoutStats:
    body_size: float
    pitch: float
    left: dict[int, float] = field(default_factory=dict)  # левое поле текста по страницам


def _round_size(size: float) -> float:
    return round(size * 2) / 2


def compute_stats(lines: Sequence[Line]) -> LayoutStats:
    """Основной кегль (по числу символов), обычный межстрочный шаг и левое поле каждой страницы."""
    if not lines:
        return LayoutStats(body_size=10.0, pitch=12.0)
    weight: Counter[float] = Counter()
    for line in lines:
        weight[_round_size(line.size)] += line.chars
    body = weight.most_common(1)[0][0]
    steps: list[float] = []
    for prev, cur in pairwise(lines):
        near_body = abs(prev.size - body) <= 0.5 and abs(cur.size - body) <= 0.5
        if prev.page == cur.page and near_body and 0.8 * body < cur.center - prev.center < 2.0 * body:
            steps.append(cur.center - prev.center)
    pitch = float(median(steps)) if steps else 1.2 * body
    left: dict[int, float] = {}
    for page in {line.page for line in lines}:
        xs = sorted(line.x0 for line in lines if line.page == page)
        left[page] = xs[len(xs) // 20]  # около 5-го процентиля: одиночный выступ поле не сдвигает
    return LayoutStats(body_size=body, pitch=pitch, left=left)


# ---------------------------------------------------------------- заголовки


def _ends_sentence(text: str) -> bool:
    return text[-1] in SENTENCE_END


def _heading_candidate(
    line: Line, prev: Line | None, nxt: Line | None, stats: LayoutStats
) -> tuple[float, bool, bool] | None:
    """Стиль заголовка (кегль, жирный, курсив) или None, если строка не заголовок."""
    text = line.text
    letters = sum(1 for ch in text if ch.isalpha())
    if letters < 2 or len(text) > MAX_HEADING_CHARS or text[0] == "•" or text[-1] in ".,;":
        return None
    gap = stats.pitch * HEADING_GAP
    isolated_before = prev is None or prev.page != line.page or line.center - prev.center > gap
    isolated_after = nxt is None or nxt.page != line.page or nxt.center - line.center > gap
    if line.size >= stats.body_size + HEADING_SIZE_DELTA:
        return _round_size(line.size), line.bold, line.italic
    if len(text) > MAX_PLAIN_HEADING_CHARS or not isolated_before:
        return None
    if line.bold or line.italic:
        return _round_size(line.size), line.bold, line.italic
    if _NUMBERED.match(text) and isolated_after:
        return _round_size(line.size), False, False
    return None


def assign_headings(lines: Sequence[Line], stats: LayoutStats) -> list[Line]:
    """Помечает заголовки; уровень это ранг стиля среди заголовков документа (крупнее и жирнее выше)."""
    styles: list[tuple[float, bool, bool] | None] = []
    for i, line in enumerate(lines):
        prev = lines[i - 1] if i else None
        nxt = lines[i + 1] if i + 1 < len(lines) else None
        styles.append(_heading_candidate(line, prev, nxt, stats))
    ranked = sorted({s for s in styles if s}, key=lambda s: (-s[0], not s[1], not s[2]))
    out: list[Line] = []
    for line, style in zip(lines, styles, strict=True):
        if style is None:
            out.append(line)
            continue
        out.append(replace(line, heading_level=min(ranked.index(style) + 1, MAX_HEADING_LEVEL)))
    return _merge_wrapped_headings(out)


def _merge_wrapped_headings(lines: list[Line]) -> list[Line]:
    """Заголовок в две строки одного стиля становится одной строкой."""
    out: list[Line] = []
    for line in lines:
        prev = out[-1] if out else None
        if (
            prev is not None
            and line.heading_level is not None
            and prev.heading_level == line.heading_level
            and prev.page == line.page
            and abs(prev.size - line.size) < 0.5
            and line.center - prev.center < WRAPPED_HEADING_STEP * line.size
            and not _NUMBERED.match(line.text)
        ):
            out[-1] = replace(
                prev, text=f"{prev.text} {line.text}", x1=max(prev.x1, line.x1), bottom=line.bottom
            )
            continue
        out.append(line)
    return out


# ---------------------------------------------------------------- абзацы и переносы


def _starts_item(text: str) -> bool:
    return bool(LIST_ITEM.match(text))


def _breaks(prev: Line, cur: Line, first: Line, stats: LayoutStats) -> bool:
    """Начинает ли ``cur`` новый абзац после ``prev`` (``first`` это первая строка текущего абзаца)."""
    if prev.heading_level is not None or cur.heading_level is not None or _starts_item(cur.text):
        return True
    if prev.page != cur.page:
        # Стык страниц: абзац продолжается, если предложение не закончено и строка идёт с маленькой буквы.
        return _ends_sentence(prev.text) or not cur.text[0].islower()
    step = cur.center - prev.center
    if step > PARAGRAPH_GAP * max(stats.pitch, 1.2 * max(prev.size, cur.size)):
        return True
    left = stats.left.get(cur.page, 0.0)
    indented = cur.x0 - left > INDENT_EM * cur.size and cur.x0 - prev.x0 > INDENT_EM * cur.size
    return indented and _ends_sentence(prev.text) and not _starts_item(first.text)


def _script(text: str) -> str:
    """Письмо слова: ``cyr``, ``lat`` или ``other`` (смешанное, цифры, заглавные)."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters or text != text.lower() or any(ch.isdigit() for ch in text):
        return "other"
    if all("\u0400" <= ch < "\u0530" for ch in letters):
        return "cyr"
    return "lat" if all(ch.isascii() for ch in letters) else "other"


def join_hyphenation(prev: str, cur: str) -> str | None:
    """«перено-» + «сами» → «переносами»; None, если переноса нет.

    Дефис остаётся, если это скорее составное слово: короткое начало («по-», «из-»), общий префикс
    («бизнес-»), частица («-то», «-либо»), смена письма («SMS-сообщение») или аббревиатура («API-ключ»).
    """
    if prev.endswith(SOFT_HYPHEN):
        return prev[:-1] + cur
    if len(prev) < 2 or prev[-1] not in HYPHENS or not prev[-2].isalpha() or not cur[0].islower():
        return None
    head = re.split(r"[\s(«\"]", prev[:-1])[-1]
    tail = re.split(r"\W", cur)[0]
    keep = (
        (len(head) <= MAX_SHORT_HEAD and _script(head.casefold()) == "cyr")
        or head.casefold() in COMPOUND_PREFIXES
        or tail in PARTICLES
        or _script(head) != _script(tail)
    )
    return prev + cur if keep else prev[:-1] + cur


@dataclass(frozen=True)
class Paragraph:
    """Абзац или заголовок: строки в порядке чтения (после склейки переносов)."""

    lines: list[Line]

    @property
    def heading_level(self) -> int | None:
        return self.lines[0].heading_level


def group_lines(lines: Sequence[Line], stats: LayoutStats) -> list[Paragraph]:
    groups: list[list[Line]] = []
    for line in lines:
        if groups and not _breaks(groups[-1][-1], line, groups[-1][0], stats):
            groups[-1].append(line)
        else:
            groups.append([line])
    return [Paragraph(_dehyphenate(group)) for group in groups]


def _dehyphenate(group: list[Line]) -> list[Line]:
    """Склеивает перенос слова: слово остаётся в строке, где началось, остаток уходит ей же."""
    out = list(group)
    for i in range(len(out) - 1):
        cur, nxt = out[i], out[i + 1]
        head, _, rest = nxt.text.partition(" ")
        joined = join_hyphenation(cur.text, head) if head else None
        if joined is not None:
            out[i] = replace(cur, text=joined)
            out[i + 1] = replace(nxt, text=rest)
    return [line for line in out if line.text]


def lines_of(pages: Iterable[Sequence[Line]]) -> list[Line]:
    return [line for page in pages for line in page]
