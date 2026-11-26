"""График для слайдов: сгруппированные горизонтальные столбики «один промпт против агента» в SVG.

Без зависимостей: matplotlib в проект не тянем. Цвета и шрифт заданы константами; подписи значений
стоят прямо у столбиков, поэтому график читается и без цвета (серый светлее, синий темнее).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from html import escape

WIDTH = 960
LEFT = 330
RIGHT = 60
TOP = 96
GROUP_PAD = 22
BAR_H = 20
BAR_GAP = 6
FONT = "DejaVu Sans, Arial, sans-serif"
COLOR_BASE = "#9ca3af"
COLOR_AGENT = "#1d4ed8"
COLOR_TEXT = "#111827"
COLOR_MUTED = "#4b5563"
COLOR_GRID = "#e5e7eb"


@dataclass(frozen=True)
class Bar:
    """Столбик: значение 0..1 (``None``: метрики нет, рисуется прочерк)."""

    label: str
    value: float | None


@dataclass(frozen=True)
class Group:
    """Метрика с несколькими столбиками (по конфигурациям)."""

    title: str
    bars: Sequence[Bar]


def _value_text(value: float | None) -> str:
    return "нет" if value is None else f"{value * 100:.1f}".replace(".", ",") + " %"


def _bar(x0: float, y: float, plot_w: float, bar: Bar, color: str) -> str:
    width = 0.0 if bar.value is None else max(plot_w * bar.value, 1.0)
    text_x = x0 + width + 8
    parts = []
    if bar.value is not None:
        parts.append(f'<rect x="{x0}" y="{y}" width="{width:.1f}" height="{BAR_H}" fill="{color}" rx="2"/>')
    parts.append(
        f'<text x="{text_x:.1f}" y="{y + BAR_H - 6}" font-size="13" fill="{COLOR_TEXT}">'
        f'{escape(bar.label)}: <tspan font-weight="bold">{_value_text(bar.value)}</tspan></text>'
    )
    return "".join(parts)


def render_svg(
    title: str, subtitle: str, groups: Sequence[Group], colors: Sequence[str] | None = None
) -> str:
    """SVG-график. ``colors``: цвет столбика по его номеру в группе (по умолчанию серый, синий)."""
    palette = list(colors or (COLOR_BASE, COLOR_AGENT))
    group_h = max(len(g.bars) for g in groups) * (BAR_H + BAR_GAP) + GROUP_PAD
    height = TOP + group_h * len(groups) + 8
    plot_w = WIDTH - LEFT - RIGHT - 190  # место справа под подпись значения
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" '
        f'height="{height}" font-family="{FONT}" role="img" aria-label="{escape(title)}">',
        f'<rect width="{WIDTH}" height="{height}" fill="#ffffff"/>',
        f'<text x="24" y="38" font-size="22" font-weight="bold" fill="{COLOR_TEXT}">{escape(title)}</text>',
        f'<text x="24" y="62" font-size="13" fill="{COLOR_MUTED}">{escape(subtitle)}</text>',
    ]
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        x = LEFT + plot_w * tick
        out.append(
            f'<line x1="{x:.1f}" y1="{TOP - 8}" x2="{x:.1f}" y2="{height - 20}" stroke="{COLOR_GRID}"/>'
            f'<text x="{x:.1f}" y="{TOP - 14}" font-size="11" text-anchor="middle" fill="{COLOR_MUTED}">'
            f"{int(tick * 100)}</text>"
        )
    for i, group in enumerate(groups):
        top = TOP + i * group_h
        out.append(
            f'<text x="{LEFT - 14}" y="{top + BAR_H - 4}" font-size="14" text-anchor="end" '
            f'fill="{COLOR_TEXT}">{escape(group.title)}</text>'
        )
        for j, bar in enumerate(group.bars):
            y = top + j * (BAR_H + BAR_GAP)
            out.append(_bar(LEFT, y, plot_w, bar, palette[j % len(palette)]))
    out.append("</svg>")
    return "\n".join(out) + "\n"
