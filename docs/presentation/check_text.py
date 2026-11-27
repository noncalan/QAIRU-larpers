"""Сверяет текст двух PDF слайдов: после смены дизайна слова и числа не должны измениться.

    uv run python docs/presentation/check_text.py old.pdf new.pdf [--dump DIR]

По каждой странице берёт слова в порядке, в котором они записаны в PDF, и
сравнивает две вещи: последовательность слов целиком и набор слов с учётом
повторов (на случай, если вёрстка поменяла порядок вывода блоков). Код возврата 0,
только если совпало всё. С --dump пишет полный текст обоих файлов для diff.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from collections import Counter
from pathlib import Path

import pymupdf

MAX_DIFF_LINES = 40


LINE_BREAK_DASHES = ("-", "‐", "–", "—")


def join_line_breaks(words: list[str]) -> list[str]:
    """Склеивает слово, которое браузер перенёс по дефису или тире: «44–» + «66» = «44–66»."""
    joined: list[str] = []
    for word in words:
        if joined and len(joined[-1]) > 1 and joined[-1].endswith(LINE_BREAK_DASHES):
            joined[-1] += word
        else:
            joined.append(word)
    return joined


def page_words(pdf: Path) -> list[list[str]]:
    with pymupdf.open(pdf) as doc:
        return [join_line_breaks([w[4] for w in page.get_text("words", sort=False)]) for page in doc]


def compare_page(old: list[str], new: list[str]) -> tuple[bool, bool, list[str]]:
    same_sequence = old == new
    same_multiset = Counter(old) == Counter(new)
    diff = [] if same_sequence else list(difflib.unified_diff(old, new, "старый", "новый", lineterm="", n=2))
    return same_sequence, same_multiset, diff


def dump(pages: list[list[str]], path: Path) -> None:
    text = "\n".join(f"=== страница {i}\n" + "\n".join(words) for i, words in enumerate(pages, 1))
    path.write_text(text + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("--dump", type=Path, help="папка для полного текста old.txt и new.txt")
    args = parser.parse_args()

    old_pages, new_pages = page_words(args.old), page_words(args.new)
    if args.dump:
        args.dump.mkdir(parents=True, exist_ok=True)
        dump(old_pages, args.dump / "old.txt")
        dump(new_pages, args.dump / "new.txt")

    ok = len(old_pages) == len(new_pages)
    print(f"Страниц: {len(old_pages)} и {len(new_pages)}")
    print("Стр. | слов было | слов стало | порядок слов | набор слов")
    for number, (old, new) in enumerate(zip(old_pages, new_pages, strict=False), 1):
        same_sequence, same_multiset, diff = compare_page(old, new)
        ok = ok and same_sequence and same_multiset
        print(
            f"{number:>4} | {len(old):>9} | {len(new):>10} | "
            f"{'совпадает' if same_sequence else 'РАЗНЫЙ':<12} | {'совпадает' if same_multiset else 'РАЗНЫЙ'}"
        )
        if diff:
            print("\n".join(diff[:MAX_DIFF_LINES]))
    total_old = sum(map(len, old_pages))
    total_new = sum(map(len, new_pages))
    print(f"Всего слов: {total_old} и {total_new}")
    print("ИТОГ: тексты совпадают до слова" if ok else "ИТОГ: тексты РАЗЛИЧАЮТСЯ")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
