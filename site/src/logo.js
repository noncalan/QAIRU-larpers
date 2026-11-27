// Пиксельный логотип QAIRU: буква Q, внутри галочка «сверено», хвост Q уходит вправо вниз.
// Одна сетка для заставки (canvas), шапки и favicon (SVG).

export const LOGO_N = 15;

function segDist(px, py, ax, ay, bx, by) {
  const dx = bx - ax, dy = by - ay;
  const t = Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)));
  const x = ax + t * dx, y = ay + t * dy;
  return Math.hypot(px - x, py - y);
}

// kind: "ring" | "check" | "tail"
export const LOGO_CELLS = (() => {
  const cells = [];
  const c = 7;
  for (let y = 0; y < LOGO_N; y++) {
    for (let x = 0; x < LOGO_N; x++) {
      const d = Math.hypot(x - c, y - c);
      const tail = segDist(x, y, 10, 10, 13.6, 13.6) < 0.8;
      const check = Math.min(segDist(x, y, 4, 7.4, 6.2, 9.6), segDist(x, y, 6.2, 9.6, 10.2, 5)) < 0.62;
      const ring = d >= 5 && d <= 6.95 && !(x >= 10 && y >= 10 && x - y > -2 && y - x > -2);
      if (tail) cells.push({ x, y, kind: "tail" });
      else if (check) cells.push({ x, y, kind: "check" });
      else if (ring) cells.push({ x, y, kind: "ring" });
    }
  }
  return cells;
})();

export const LOGO_COLORS = { ring: "#e9ede6", check: "#b8f35a", tail: "#b8f35a" };

export function logoSvgRects(gap = 0.12) {
  const s = 1 - gap;
  return LOGO_CELLS.map(({ x, y, kind }) => ({ x: x + gap / 2, y: y + gap / 2, s, fill: LOGO_COLORS[kind], kind }));
}
