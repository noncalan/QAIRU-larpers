"""Находятся ли цитаты эталона нарезки в тексте из PDF и DOCX (эксперимент).

Для каждой цитаты (unit, atoms, also) ищем точное вхождение после нормализации, затем rapidfuzz >= 90.
Для PDF дополнительно определяем номер страницы по смещению.

Запуск: uv run --group experiments python experiments/18-segmentation/format_anchor_check.py
"""
import sys
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "03-proto"))
from quote_check import locate  # noqa: E402

gold = yaml.safe_load(open(HERE / "gold_segmentation.yaml", encoding="utf-8"))
quotes = []
for u in gold["units"]:
    quotes.append((u["gid"], u["type"], u["quote"]))
    quotes += [(f"{u['gid']}.{k}", u["type"], q) for k, q in enumerate(u.get("atoms") or [], 1)]
    quotes += [(f"{u['gid']}+", u["type"], q) for q in u.get("also", [])]


def texts():
    import pymupdf
    doc = pymupdf.open(ROOT / "data/input/requirements.pdf")
    pages = [p.get_text() for p in doc]
    yield "pdf get_text", pages
    chunks = pymupdf.to_markdown(doc, page_chunks=True, use_ocr=False) if hasattr(pymupdf, "to_markdown") else None
    if chunks is None:
        import pymupdf4llm
        chunks = pymupdf4llm.to_markdown(str(ROOT / "data/input/requirements.pdf"), page_chunks=True)
    yield "pdf to_markdown", [c["text"] for c in chunks]
    from markitdown import MarkItDown
    yield "docx markitdown", [MarkItDown().convert(str(ROOT / "data/input/requirements.docx")).text_content]
    yield "md (исходник)", [open(ROOT / "data/input/requirements.md", encoding="utf-8").read()]


for name, pages in texts():
    full, bounds = "", []
    for p in pages:
        bounds.append(len(full)); full += p + "\n"
    res, bad, page_hits = Counter(), [], Counter()
    for gid, typ, q in quotes:
        kind, score, s, e = locate(full, q)
        res[kind] += 1
        if kind != "exact":
            bad.append((gid, typ, kind, round(score, 1), q[:60]))
        if s is not None:
            page_hits[sum(1 for b in bounds if b <= s)] += 1
    print(f"\n== {name}: страниц {len(pages)}, символов {len(full)}; цитат {len(quotes)}: {dict(res)}")
    if len(pages) > 1:
        print("   цитат по страницам:", dict(sorted(page_hits.items())))
    for b in bad:
        print("   ", b)
