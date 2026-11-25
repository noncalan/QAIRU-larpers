"""Метки автоматической нумерации DOCX: «1.», «2.3.», «а)», «•», как их показывает Word.

Текст абзаца в файле метки не содержит: номер хранится в ``numbering.xml`` и считается при показе.
Без метки в каноническом тексте не видно, что это «пункт 3.2», а ссылки вида «см. п. 3.2» в
требованиях не к чему привязать. Считаем метку так же, как Word: счётчик на уровень списка,
уровни глубже сбрасываются при новом пункте выше, «%1.%2» подставляет счётчики уровней.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
BULLET = "•"
ROMAN = (
    (1000, "m"),
    (900, "cm"),
    (500, "d"),
    (400, "cd"),
    (100, "c"),
    (90, "xc"),
    (50, "l"),
    (40, "xl"),
    (10, "x"),
    (9, "ix"),
    (5, "v"),
    (4, "iv"),
    (1, "i"),
)
LEVELS = 9


def _attr(element: etree._Element | None, name: str) -> str | None:
    return None if element is None else element.get(f"{{{W}}}{name}")


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def format_number(value: int, fmt: str) -> str:
    """Число в формате нумерации Word: decimal, буквы, римские; неизвестный формат это decimal."""
    if fmt == "decimalZero":
        return f"{value:02d}"
    if fmt in ("lowerLetter", "upperLetter"):
        base = "abcdefghijklmnopqrstuvwxyz"
        text = base[(value - 1) % 26] * ((value - 1) // 26 + 1)
        return text.upper() if fmt == "upperLetter" else text
    if fmt in ("russianLower", "russianUpper"):
        base = "абвгдежзиклмнопрстуфхцчшэюя"
        text = base[(value - 1) % len(base)] * ((value - 1) // len(base) + 1)
        return text.upper() if fmt == "russianUpper" else text
    if fmt in ("lowerRoman", "upperRoman"):
        rest, out = value, ""
        for number, symbol in ROMAN:
            while rest >= number:
                out += symbol
                rest -= number
        return out.upper() if fmt == "upperRoman" else out
    return str(value)


@dataclass(frozen=True)
class Level:
    fmt: str
    text: str
    start: int


@dataclass
class Numbering:
    """Определения нумерации документа и счётчики; ``label`` вызывается по абзацам в порядке документа."""

    abstract: dict[str, list[Level]] = field(default_factory=dict)
    nums: dict[str, str] = field(default_factory=dict)
    overrides: dict[tuple[str, int], int] = field(default_factory=dict)
    counters: dict[str, list[int | None]] = field(default_factory=dict)
    seen: set[str] = field(default_factory=set)

    @classmethod
    def from_xml(cls, root: etree._Element | None) -> Numbering:
        out = cls()
        if root is None:
            return out
        for node in root.findall("w:abstractNum", NS):
            levels = [Level("decimal", f"%{k + 1}.", 1) for k in range(LEVELS)]
            for lvl in node.findall("w:lvl", NS):
                index = _int(_attr(lvl, "ilvl"), -1)
                if 0 <= index < LEVELS:
                    levels[index] = Level(
                        fmt=_attr(lvl.find("w:numFmt", NS), "val") or "decimal",
                        text=_attr(lvl.find("w:lvlText", NS), "val") or "",
                        start=_int(_attr(lvl.find("w:start", NS), "val"), 1),
                    )
            out.abstract[_attr(node, "abstractNumId") or ""] = levels
        for num in root.findall("w:num", NS):
            num_id = _attr(num, "numId") or ""
            out.nums[num_id] = _attr(num.find("w:abstractNumId", NS), "val") or ""
            for override in num.findall("w:lvlOverride", NS):
                start = override.find("w:startOverride", NS)
                if start is not None:
                    out.overrides[(num_id, _int(_attr(override, "ilvl"), 0))] = _int(_attr(start, "val"), 1)
        return out

    def label(self, num_id: str, level: int) -> str | None:
        """Метка пункта или None, если нумерации нет (``numId`` 0, неизвестный список, формат «none»)."""
        abstract_id = self.nums.get(num_id)
        levels = self.abstract.get(abstract_id or "")
        if num_id == "0" or levels is None or abstract_id is None or not 0 <= level < LEVELS:
            return None
        counters = self.counters.setdefault(abstract_id, [None] * LEVELS)
        if num_id not in self.seen:  # первый абзац списка с переопределением начала: нумерация с него
            self.seen.add(num_id)
            for lvl in range(LEVELS):
                if (num_id, lvl) in self.overrides:
                    counters[lvl] = self.overrides[(num_id, lvl)] - 1  # следующий пункт получит это число
                    counters[lvl + 1 :] = [None] * (LEVELS - lvl - 1)
        last = counters[level]
        counters[level] = levels[level].start if last is None else last + 1
        counters[level + 1 :] = [None] * (LEVELS - level - 1)
        current = levels[level]
        if current.fmt == "none":
            return None
        if current.fmt == "bullet" or not current.text:
            return BULLET
        text = current.text
        for k in range(LEVELS):
            if f"%{k + 1}" in text:
                value = counters[k]
                shown = levels[k].start if value is None else value
                text = text.replace(f"%{k + 1}", format_number(shown, levels[k].fmt))
        return text.strip() or None
