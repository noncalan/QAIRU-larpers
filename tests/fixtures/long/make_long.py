"""Синтетический длинный документ требований (#38): сводное ТЗ из открытых ТЗ, больше 60 страниц.

Источники (лицензии и происхождение в ``README.md`` рядом):

- ``data/input/requirements.md``: документ кейса (уведомление о расходах по карте и его REST API);
- ``sources/c4/README.md``: ``TalosDX/c4_tech_task_example``, MIT, копия вместе с ``sources/c4/LICENSE``;
- ``docs/case/task.md``: ТЗ хакатона (агент проверки требований).

Документ это «сводное ТЗ» из подсистем: каждая подсистема это один источник целиком, заголовки
источника опущены на уровень ниже, источники идут по кругу, пока не наберётся ``MIN_PAGES`` страниц.
Повтор подсистем намеренный: в больших ТЗ из шаблонов это обычное дело, а сверка требований между
частями должна найти повторы и расхождения, которые проход по одной части не видит.

Запуск::

    uv run python tests/fixtures/long/make_long.py            # long.md и long.pdf (pandoc, LibreOffice)
    uv run python tests/fixtures/long/make_long.py --md-only  # только long.md

Страницы считает PDF, собранный pandoc (MD → DOCX) и LibreOffice (DOCX → PDF), как у ``requirements.pdf``.
Без них число страниц оценивается по стандартной странице 1800 знаков.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT_MD = HERE / "long.md"
OUT_PDF = HERE / "long.pdf"
MIN_PAGES = 60
CHARS_PER_PAGE = 1800
"""Стандартная машинописная страница: оценка, если PDF собрать нечем."""
MAX_ROUNDS = 12
CONVERT_TIMEOUT_S = 300

TITLE = "Сводное техническое задание: несколько подсистем"
INTRO = (
    "Синтетический документ для проверки длинного входа агента (#38). Собран генератором "
    "`tests/fixtures/long/make_long.py` из открытых ТЗ: каждая подсистема ниже это один исходный "
    "документ целиком, без правок текста. Подсистемы повторяются по кругу, как в больших ТЗ, собранных "
    "из шаблонов. Источники и лицензии: `tests/fixtures/long/README.md`."
)


@dataclass(frozen=True)
class Source:
    path: Path
    title: str
    origin: str


SOURCES = (
    Source(
        ROOT / "data" / "input" / "requirements.md",
        "Уведомление о расходах по карте",
        "документ кейса BCC-HUB, `data/input/requirements.md`",
    ),
    Source(
        HERE / "sources" / "c4" / "README.md",
        "REST API для управления компаниями и сотрудниками",
        "TalosDX/c4_tech_task_example, лицензия MIT, Copyright (c) 2025 Roman Ushakov",
    ),
    Source(
        ROOT / "docs" / "case" / "task.md",
        "Агент проверки требований",
        "ТЗ хакатона, копия README.md из BCC-HUB/ai-requirements-validator-task",
    ),
)

_HEADING = re.compile(r"^(#{1,6})(\s+.*)$")
_QUOTE_NOTE = re.compile(r"^>\s*Копия ТЗ кейса")


def _body(source: Source) -> list[str]:
    """Текст источника без его заголовка первого уровня и пометки о копии; заголовки ниже на уровень."""
    lines = source.path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        m = _HEADING.match(line)
        if m and len(m.group(1)) == 1:
            continue
        if _QUOTE_NOTE.match(line):
            continue
        out.append("#" * min(len(m.group(1)) + 1, 6) + m.group(2) if m else line)
    while out and not out[0].strip():
        out.pop(0)
    return out


def compose(rounds: int) -> str:
    """Сводное ТЗ: ``rounds`` кругов по всем источникам, подсистемы пронумерованы подряд."""
    lines = [f"# {TITLE}", "", INTRO, ""]
    number = 0
    for _ in range(rounds):
        for source in SOURCES:
            number += 1
            lines += [f"## Подсистема {number}. {source.title}", "", f"Источник: {source.origin}.", ""]
            lines += _body(source)
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def to_pdf(markdown: Path, target: Path) -> int | None:
    """PDF через pandoc и LibreOffice; число страниц или None, если конвертеров нет или сбой."""
    pandoc, soffice = shutil.which("pandoc"), shutil.which("soffice")
    if pandoc is None or soffice is None:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        docx = Path(tmp) / f"{target.stem}.docx"
        try:
            subprocess.run(
                [pandoc, "--from", "gfm", "--to", "docx", "-o", str(docx), str(markdown)],
                check=True,
                timeout=CONVERT_TIMEOUT_S,
            )
            subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(docx)],
                check=True,
                timeout=CONVERT_TIMEOUT_S,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"PDF не собран: {exc}", file=sys.stderr)
            return None
        built = Path(tmp) / f"{target.stem}.pdf"
        shutil.copyfile(built, target)
    with pymupdf.open(target) as doc:
        return int(doc.page_count)


def estimate_pages(text: str) -> int:
    return -(-len(text) // CHARS_PER_PAGE)


def _pages(text: str, md_only: bool) -> tuple[int, bool]:
    OUT_MD.write_text(text, encoding="utf-8", newline="\n")
    pages = None if md_only else to_pdf(OUT_MD, OUT_PDF)
    return (estimate_pages(text), False) if pages is None else (pages, True)


def build(md_only: bool = False) -> tuple[int, int, bool]:
    """Собирает документ не короче ``MIN_PAGES`` страниц: (кругов, страниц, страницы по PDF).

    Число кругов прикидывается по одному кругу, затем добавляется по кругу, пока страниц мало.
    """
    one, _ = _pages(compose(1), md_only)
    rounds = max(1, -(-MIN_PAGES // max(one, 1)))
    while rounds <= MAX_ROUNDS:
        pages, by_pdf = _pages(compose(rounds), md_only)
        if pages >= MIN_PAGES:
            return rounds, pages, by_pdf
        rounds += 1
    raise SystemExit(f"За {MAX_ROUNDS} кругов не набралось {MIN_PAGES} страниц")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--md-only", action="store_true", help="не собирать PDF")
    args = parser.parse_args()
    rounds, pages, by_pdf = build(md_only=args.md_only)
    how = "по PDF" if by_pdf else f"оценка по {CHARS_PER_PAGE} знаков"
    print(f"{OUT_MD.name}: кругов {rounds}, подсистем {rounds * len(SOURCES)}, страниц {pages} ({how})")


if __name__ == "__main__":
    main()
