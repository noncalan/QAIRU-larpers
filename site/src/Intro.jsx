import { useEffect, useRef, useState } from "react";
import { LOGO_CELLS, LOGO_N, LOGO_COLORS } from "./logo.js";

// Заставка: пиксели слетаются в логотип, под ним печатается название,
// потом экран рассыпается крупными пикселями и открывает страницу.
// Показывается один раз за сессию вкладки (в dev каждый раз), ?intro показывает принудительно,
// клик, Esc или пробел пропускают. При prefers-reduced-motion не показывается.

const KEY = "qairu-intro-seen";
const BG = "#0a0b0a";
const ACC = "#b8f35a";
const T_GATHER = 1150;
const T_HOLD = 650;
const T_DISSOLVE = 750;

function shouldShow() {
  if (typeof window === "undefined") return false;
  if (new URLSearchParams(location.search).has("intro")) return true;
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return false;
  if (import.meta.env.DEV) return true;
  try { return !sessionStorage.getItem(KEY); } catch { return true; }
}

const ease = (t) => 1 - Math.pow(1 - t, 3);
const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

export default function Intro() {
  const [show, setShow] = useState(shouldShow);
  const ref = useRef(null);

  useEffect(() => {
    if (!show) return;
    try { sessionStorage.setItem(KEY, "1"); } catch { /* приватный режим */ }
    document.documentElement.style.overflow = "hidden";

    const cv = ref.current;
    const ctx = cv.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    let w, h, cell, ox, oy, blocks, cols, rows, bs;

    const rnd = (a, b) => a + Math.random() * (b - a);
    const resize = () => {
      w = window.innerWidth; h = window.innerHeight;
      cv.width = w * dpr; cv.height = h * dpr;
      cv.style.width = `${w}px`; cv.style.height = `${h}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      cell = Math.max(6, Math.round(Math.min(w, h) * 0.2 / LOGO_N));
      ox = Math.round(w / 2 - (cell * LOGO_N) / 2);
      oy = Math.round(h / 2 - (cell * LOGO_N) / 2 - cell * 1.5);
      bs = Math.max(28, Math.round(Math.min(w, h) / 14));
      cols = Math.ceil(w / bs); rows = Math.ceil(h / bs);
      blocks = [];
      for (let y = 0; y < rows; y++) for (let x = 0; x < cols; x++) {
        // от центра к краям, с шумом
        const d = Math.hypot(x * bs + bs / 2 - w / 2, y * bs + bs / 2 - h / 2) / Math.hypot(w / 2, h / 2);
        blocks.push({ x, y, t: Math.min(0.95, d * 0.65 + Math.random() * 0.35) });
      }
    };
    resize();

    // частицы логотипа + шум, который рассеивается
    const R = Math.min(w, h) * 0.42;
    const parts = LOGO_CELLS.map((c) => {
      const a = Math.random() * Math.PI * 2, r = rnd(0.25, 1) * R;
      return { ...c, sx: w / 2 + Math.cos(a) * r, sy: h / 2 + Math.sin(a) * r, delay: rnd(0, 0.35), white: Math.random() < 0.12 };
    });
    const dust = Array.from({ length: 140 }, () => {
      const a = Math.random() * Math.PI * 2, r = rnd(0.1, 1.1) * R;
      return { x: w / 2 + Math.cos(a) * r, y: h / 2 + Math.sin(a) * r, vx: rnd(-0.3, 0.3), vy: rnd(-0.3, 0.3), s: rnd(2, 4), a: rnd(0.3, 0.9) };
    });

    const label = "qairu validator";
    let raf;
    const t0 = performance.now();
    const total = T_GATHER + T_HOLD + T_DISSOLVE;
    const finish = () => {
      cancelAnimationFrame(raf);
      document.documentElement.style.overflow = "";
      setShow(false);
    };

    const drawLogo = (k, alpha) => {
      for (const p of parts) {
        const kk = ease(Math.max(0, Math.min(1, (k - p.delay) / (1 - p.delay))));
        const tx = ox + p.x * cell, ty = oy + p.y * cell;
        const x = p.sx + (tx - p.sx) * kk, y = p.sy + (ty - p.sy) * kk;
        const s = (cell - 1) * (0.45 + 0.55 * kk);
        ctx.globalAlpha = alpha * (0.35 + 0.65 * kk);
        ctx.fillStyle = kk < 1 && p.white ? "#ffffff" : LOGO_COLORS[p.kind];
        ctx.fillRect(Math.round(x), Math.round(y), Math.ceil(s), Math.ceil(s));
      }
      ctx.globalAlpha = 1;
    };

    const frame = (now) => {
      const t = now - t0;
      ctx.clearRect(0, 0, w, h);

      // фон: до растворения сплошной, потом блоками
      const dk = Math.max(0, (t - T_GATHER - T_HOLD) / T_DISSOLVE);
      ctx.fillStyle = BG;
      if (dk === 0) ctx.fillRect(0, 0, w, h);
      else {
        const p = easeInOut(Math.min(1, dk));
        for (const b of blocks) {
          const left = (b.t + 0.12 - p) / 0.12; // 1 → блок целый, 0 → исчез
          if (left <= 0) continue;
          const s = bs * Math.min(1, left);
          ctx.globalAlpha = Math.min(1, left * 1.4);
          ctx.fillRect(b.x * bs + (bs - s) / 2, b.y * bs + (bs - s) / 2, Math.ceil(s) + 0.5, Math.ceil(s) + 0.5);
          if (left < 1 && left > 0.4 && (b.x + b.y) % 7 === 0) {
            ctx.fillStyle = ACC; ctx.globalAlpha = 0.25 * left;
            ctx.fillRect(b.x * bs + (bs - s) / 2, b.y * bs + (bs - s) / 2, Math.ceil(s), 2);
            ctx.fillStyle = BG;
          }
        }
        ctx.globalAlpha = 1;
      }

      // пыль
      const gk = Math.min(1, t / T_GATHER);
      for (const d of dust) {
        d.x += d.vx; d.y += d.vy;
        ctx.globalAlpha = d.a * (1 - gk) * (dk ? 0 : 1);
        ctx.fillStyle = Math.random() < 0.02 ? "#fff" : ACC;
        ctx.fillRect(d.x, d.y, d.s, d.s);
      }
      ctx.globalAlpha = 1;

      // свечение под логотипом
      if (gk > 0.6 && dk < 1) {
        const ga = Math.min(1, (gk - 0.6) / 0.4) * (1 - dk);
        const cx = w / 2, cy = oy + (cell * LOGO_N) / 2;
        const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, cell * LOGO_N);
        g.addColorStop(0, `rgba(184,243,90,${0.16 * ga})`);
        g.addColorStop(1, "rgba(184,243,90,0)");
        ctx.fillStyle = g;
        ctx.fillRect(cx - cell * LOGO_N, cy - cell * LOGO_N, cell * LOGO_N * 2, cell * LOGO_N * 2);
      }

      // логотип: при растворении рассыпается вверх
      if (dk === 0) drawLogo(gk, 1);
      else {
        const p = ease(Math.min(1, dk * 1.3));
        for (const q of parts) {
          const tx = ox + q.x * cell, ty = oy + q.y * cell;
          const jitter = (q.x * 13 + q.y * 7) % 11 / 11;
          ctx.globalAlpha = Math.max(0, 1 - p * (1 + jitter));
          ctx.fillStyle = LOGO_COLORS[q.kind];
          ctx.fillRect(Math.round(tx + (q.x - 7) * p * cell * 0.9), Math.round(ty - p * cell * 6 * (0.5 + jitter)), cell - 1, cell - 1);
        }
        ctx.globalAlpha = 1;
      }

      // подпись печатается
      const tk = (t - T_GATHER * 0.7) / 600;
      if (tk > 0) {
        const n = Math.min(label.length, Math.floor(tk * label.length));
        const fs = Math.max(13, Math.round(cell * 1.35));
        ctx.font = `500 ${fs}px "JetBrains Mono Variable", ui-monospace, Menlo, monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.globalAlpha = 1 - Math.min(1, dk * 2.5);
        ctx.fillStyle = "#8c948b";
        const text = label.slice(0, n) + (n < label.length || Math.floor(t / 400) % 2 ? "▍" : " ");
        ctx.fillText(text, w / 2, oy + cell * LOGO_N + cell * 2.2);
        ctx.globalAlpha = 1;
      }

      if (t >= total) return finish();
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);

    const skip = (e) => { if (!e.key || ["Escape", " ", "Enter"].includes(e.key)) finish(); };
    window.addEventListener("resize", resize);
    window.addEventListener("keydown", skip);
    cv.addEventListener("pointerdown", skip);
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener("resize", resize);
      window.removeEventListener("keydown", skip);
      document.documentElement.style.overflow = "";
    };
  }, [show]);

  if (!show) return null;
  return <canvas ref={ref} className="intro" aria-hidden="true" />;
}
