"""Находятся ли цитаты эталона нарезки в тексте из PDF и DOCX (эксперимент).

Для каждой цитаты (unit, atoms, also) ищем точное вхождение после нормализации, затем rapidfuzz >= 90.
Варианты PDF: PyMuPDF get_text() по умолчанию и с sort=True, pymupdf4llm.to_markdown, pdfplumber.
Режим «+punct»: нормализация дополнительно убирает пробел перед знаком препинания.
Отдельно: сверка ячеек таблиц эталона с ячейками PyMuPDF find_tables() без пробелов и знаков.

Запуск: uv run --group experiments python experiments/18-segmentation/format_anchor_check.py
"""
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path

import yaml
from rapidfuzz import fuzz

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PDF = ROOT / "data/input/requirements.pdf"
sys.path.insert(0, str(HERE.parent / "03-proto"))
import quote_check as qc  # noqa: E402

gold = yaml.safe_load(open(HERE / "gold_segmentation.yaml", encoding="utf-8"))
quotes = []
for u in gold["units"]:
    quotes.append((u["gid"], u["quote"]))
    quotes += [(f"{u['gid']}.{k}", q) for k, q in enumerate(u.get("atoms") or [], 1)]
    quotes += [(f"{u['gid']}+", q) for q in u.get("also", [])]

_orig_normalize = qc.normalize


def normalize_punct(text):
    s, idx = _orig_normalize(text)
    keep = [k for k in range(len(s)) if not (s[k] == " " and k + 1 < len(s) and s[k + 1] in '.,;:!?)"')]
    return "".join(s[k] for k in keep), [idx[k] for k in keep]


def sources():
    import pdfplumber
    import pymupdf
    import pymupdf4llm
    from markitdown import MarkItDown
    doc = pymupdf.open(PDF)
    yield "md (исходник)", [open(ROOT / "data/input/requirements.md", encoding="utf-8").read()]
    yield "docx markitdown", [MarkItDown().convert(str(ROOT / "data/input/requirements.docx")).text_content]
    yield "pdf get_text()", [p.get_text() for p in doc]
    yield "pdf get_text(sort=True)", [p.get_text(sort=True) for p in doc]
    yield "pdf pymupdf4llm.to_markdown", [c["text"] for c in pymupdf4llm.to_markdown(str(PDF), page_chunks=True, use_ocr=False)]
    yield "pdf pdfplumber.extract_text", [(p.extract_text() or "") for p in pdfplumber.open(PDF).pages]


def squash(s):
    s = unicodedata.normalize("NFKC", s).lower().replace("ё", "е")
    return re.sub(r"[\W_]+", "", s)


def table_cells_check():
    import pymupdf
    cells = []
    for pn, p in enumerate(pymupdf.open(PDF), 1):
        for t in p.find_tables().tables:
            for r in t.extract():
                cells += [squash(c) for c in r if c]
    ok = tot = 0
    for u in gold["units"]:
        if "|" not in u["quote"]:
            continue
        for c in [c.strip() for c in u["quote"].split("|") if c.strip()]:
            tot += 1
            ok += max((fuzz.ratio(squash(c), cc) for cc in cells), default=0) >= 90
    return ok, tot


if __name__ == "__main__":
    for name, pages in sources():
        full, bounds = "", []
        for p in pages:
            bounds.append(len(full))
            full += p + "\n"
        for mode, fn in (("base", _orig_normalize), ("+punct", normalize_punct)):
            qc.normalize = fn
            res = [(gid, qc.locate(full, q)[0]) for gid, q in quotes]
            c = Counter(k for _, k in res)
            nf = sorted({gid.rstrip("+").split(".")[0] for gid, k in res if k == "not_found"})
            print(f"{name:30} {mode:7} цитат {len(quotes)}: exact {c['exact']}, fuzzy {c['fuzzy']}, "
                  f"not_found {c['not_found']}  {nf if nf else ''}")
        qc.normalize = _orig_normalize
    ok, tot = table_cells_check()
    print(f"\nячейки таблиц эталона в PyMuPDF find_tables() (без пробелов и знаков, ratio >= 90): {ok}/{tot}")
