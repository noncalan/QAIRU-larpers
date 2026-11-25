"""Прогоняет фикстуры через детекторы типа и парсеры, печатает короткую таблицу поведения."""
import os, sys, time, io, warnings, logging, contextlib
warnings.filterwarnings("ignore"); logging.disable(logging.CRITICAL)
d = sys.argv[1]; only = sys.argv[2:] or None

def run(fn):
    t = time.perf_counter()
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            r = fn()
        return f"OK {r}"[:90] + f" [{time.perf_counter()-t:.2f}s]"
    except Exception as e:
        return f"ERR {type(e).__name__}: {str(e)[:70]}".replace("\n", " ") + f" [{time.perf_counter()-t:.2f}s]"

import filetype, puremagic
from magika import Magika; mk = Magika()
import pymupdf, pypdf, pdfplumber, pikepdf, docx, mammoth
from charset_normalizer import from_bytes

def mu(p):
    doc = pymupdf.open(p)
    if doc.needs_pass: return f"needs_pass enc={doc.is_encrypted} pages={doc.page_count}"
    n = sum(len(pg.get_text().strip()) for pg in doc)
    return f"pdf={doc.is_pdf} repaired={getattr(doc,'is_repaired',None)} pages={doc.page_count} chars={n} perm={doc.permissions}"
def pp(p):
    r = pypdf.PdfReader(p, strict=False)
    if r.is_encrypted:
        res = r.decrypt("")
        if not res: return "encrypted, decrypt('') failed"
    return f"pages={len(r.pages)} chars={sum(len((pg.extract_text() or '').strip()) for pg in r.pages)}"
def pl(p):
    with pdfplumber.open(p) as pdf:
        return f"pages={len(pdf.pages)} chars={sum(len((pg.extract_text() or '').strip()) for pg in pdf.pages)}"
def pk(p):
    with pikepdf.open(p) as pdf:
        return f"pages={len(pdf.pages)} enc={pdf.is_encrypted}"
def dx(p):
    dd = docx.Document(p); return f"paras={len(dd.paragraphs)} tables={len(dd.tables)}"
def mm(p):
    with open(p, "rb") as f: r = mammoth.convert_to_markdown(f)
    return f"chars={len(r.value)} msgs={len(r.messages)}"
def cn(p):
    b = open(p, "rb").read()[:200000]; m = from_bytes(b).best()
    return "None" if m is None else f"{m.encoding} lang={m.language} chaos={m.chaos:.2f}"

pdfs = lambda f: f.endswith(".pdf")
for f in sorted(os.listdir(d)):
    if only and not any(o in f for o in only): continue
    p = os.path.join(d, f); print(f"\n### {f} ({os.path.getsize(p)} B)")
    head = open(p, "rb").read(8192)
    print("  filetype :", run(lambda: filetype.guess_mime(head)))
    print("  puremagic:", run(lambda: puremagic.from_string(head, mime=True)))
    print("  magika   :", run(lambda: (lambda r: f"{r.output.label} score={r.score:.2f}")(mk.identify_path(p))))
    if f.endswith(".pdf") and "big" not in f or (only and f.endswith(".pdf")):
        for name, fn in [("pymupdf", mu), ("pypdf", pp), ("pdfplumber", pl), ("pikepdf", pk)]:
            print(f"  {name:9}:", run(lambda: fn(p)))
    if f.endswith(".docx"):
        for name, fn in [("python-docx", dx), ("mammoth", mm)]:
            if "bomb" in f: continue
            print(f"  {name:9}:", run(lambda: fn(p)))
    if f.endswith((".md", ".txt", ".html", ".rtf")):
        print("  charset  :", run(lambda: cn(p)))
