"""Собирает qairu-larpers-slides.pdf из исходника слайдов в deck/.

Исходник в формате артефакта Slides: deck/deck.json (порядок слайдов и шрифты),
deck/deck.css (общий стиль) и deck/slides/<id>.html (по одному <section> на слайд,
заметки докладчика в <aside> в PDF не попадают).

Запуск из корня репозитория:

    uv run python docs/presentation/build_pdf.py

Нужен установленный Chrome, Chromium или Edge (путь можно задать в CHROME)
и доступ к fonts.googleapis.com: шрифты подключаются оттуда.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DECK = HERE / "deck"
DEFAULT_OUT = HERE / "qairu-larpers-slides.pdf"

# Слайд в артефакте Slides рисует сам плеер. Для печати повторяем его основу:
# холст 1920×1080, сброс отступов, скрытые заметки и фигуры <x-shape>.
PRINT_CSS = """
@page { size: 1920px 1080px; margin: 0; }
html, body { margin: 0; padding: 0; background: #ffffff; }
body { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
section, section * { box-sizing: border-box; }
section {
  position: relative; width: 1920px; height: 1080px; overflow: hidden;
  break-after: page; page-break-after: always;
}
section:last-of-type { break-after: auto; page-break-after: auto; }
section h1, section h2, section h3, section p, section ul, section ol,
section table, section figure { margin: 0; }
section table { border-collapse: collapse; border-spacing: 0; }
section th, section td { padding: 0; font-weight: inherit; }
section > aside { display: none; }
x-shape { display: block; flex: none; }
x-shape[kind="ellipse"] { border-radius: 50%; }
x-shape[kind="rounded"] { border-radius: 16px; }
x-shape[kind="arrow-right"] {
  clip-path: polygon(0 35%, 62% 35%, 62% 0, 100% 50%, 62% 100%, 62% 65%, 0 65%);
}
"""

BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]
BROWSER_NAMES = ["google-chrome", "chromium", "chromium-browser", "microsoft-edge", "chrome", "msedge"]


def find_browser() -> str:
    env = os.environ.get("CHROME")
    if env:
        return env
    for path in BROWSER_CANDIDATES:
        if Path(path).exists():
            return path
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    sys.exit("Не найден Chrome, Chromium или Edge: укажите путь в переменной CHROME")


def build_html(deck_dir: Path) -> str:
    index = json.loads((deck_dir / "deck.json").read_text(encoding="utf-8"))
    links = [
        f'<link rel="stylesheet" href="{html.escape(face["href"])}">'
        for face in index.get("faces", {}).values()
        if face.get("href", "").startswith("https://fonts.googleapis.com/")
    ]
    shared = [
        (deck_dir / Path(p).name).read_text(encoding="utf-8")
        for p in index.get("format", {}).get("shared", [])
    ]
    slides = []
    for slide_id in index["order"]:
        path = deck_dir / "slides" / f"{slide_id}.html"
        if not path.exists():
            sys.exit(f"Нет файла слайда {path}")
        slides.append(path.read_text(encoding="utf-8").strip())
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="ru"><head><meta charset="utf-8">',
            f"<title>{html.escape(index.get('title', ''))}</title>",
            '<link rel="preconnect" href="https://fonts.googleapis.com">',
            '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
            *links,
            f"<style>{PRINT_CSS}</style>",
            *(f"<style>{css}</style>" for css in shared),
            "</head><body>",
            *slides,
            "</body></html>",
        ]
    )


def print_pdf(browser: str, page: Path, out: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="qairu-pdf-") as profile:
        cmd = [
            browser,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            "--no-pdf-header-footer",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=30000",
            f"--print-to-pdf={out}",
            page.as_uri(),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deck", type=Path, default=DECK, help="папка с deck.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="куда записать PDF")
    parser.add_argument("--html", type=Path, help="сохранить собранную HTML-страницу для просмотра")
    args = parser.parse_args()

    page_html = build_html(args.deck)
    if args.html:
        args.html.write_text(page_html, encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="qairu-deck-") as tmp:
        page = Path(tmp) / "deck.html"
        page.write_text(page_html, encoding="utf-8")
        out = args.out.resolve()
        out.unlink(missing_ok=True)
        print_pdf(find_browser(), page, out)
    if not out.exists():
        sys.exit("Браузер не записал PDF")
    print(f"PDF: {out} ({out.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main()
