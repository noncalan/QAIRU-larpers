import time, re, warnings, io, contextlib; warnings.filterwarnings("ignore")
W = lambda s: set(w.lower() for w in re.findall(r"[А-Яа-яЁё]{3,}", s))
orig = W(open("fx/req.md", encoding="utf-8").read())
def rep(name, fn):
    t = time.perf_counter()
    try:
        s = fn(); w = W(s)
        print(f"{name:28} {time.perf_counter()-t:5.2f}s chars={len(s):6} recall={len(orig&w)/len(orig):.3f} headings={sum(1 for l in s.splitlines() if l.startswith('#'))} tablerows={sum(1 for l in s.splitlines() if l.lstrip().startswith('|'))} cyr_ok={'Держатель' in s}")
        return s
    except Exception as e: print(f"{name:28} ERR {type(e).__name__}: {str(e)[:80]}")
import mammoth, docx
from markitdown import MarkItDown; mdi = MarkItDown()
rep("mammoth docx->md", lambda: mammoth.convert_to_markdown(open("fx/req.docx","rb")).value)
rep("python-docx paras+tables", lambda: "\n".join([p.text for p in docx.Document("fx/req.docx").paragraphs] + ["|"+"|".join(c.text for c in r.cells) for t in docx.Document("fx/req.docx").tables for r in t.rows]))
s = rep("markitdown docx", lambda: mdi.convert("fx/req.docx").text_content); open("out_markitdown_docx.md","w").write(s or "")
rep("markitdown pdf", lambda: mdi.convert("fx/req.pdf").text_content)
rep("markitdown html", lambda: mdi.convert("fx/req.html").text_content)
rep("markitdown rtf", lambda: mdi.convert("fx/req.rtf").text_content)
rep("markitdown doc", lambda: mdi.convert("fx/req.doc").text_content)
rep("markitdown cp1251 md", lambda: mdi.convert("fx/req_cp1251.md").text_content)
rep("markitdown utf16 md", lambda: mdi.convert("fx/req_utf16.md").text_content)
from striprtf.striprtf import rtf_to_text
rep("striprtf", lambda: rtf_to_text(open("fx/req.rtf", encoding="ascii", errors="ignore").read()))
import subprocess
rep("pandoc rtf->md", lambda: subprocess.run(["pandoc","-f","rtf","-t","gfm","fx/req.rtf"],capture_output=True,text=True,check=True).stdout)
rep("pandoc docx->md", lambda: subprocess.run(["pandoc","-t","gfm","fx/req.docx"],capture_output=True,text=True,check=True).stdout)
rep("pandoc html->md", lambda: subprocess.run(["pandoc","-f","html","-t","gfm","fx/req.html"],capture_output=True,text=True,check=True).stdout)
import pymupdf
rep("pymupdf open(docx) no key", lambda: "".join(p.get_text() for p in pymupdf.open("fx/req.docx")))
rep("pymupdf open(html)", lambda: "".join(p.get_text() for p in pymupdf.open("fx/req.html")))
rep("pdfplumber pdf", lambda: "\n".join(p.extract_text() for p in __import__("pdfplumber").open("fx/req.pdf").pages))
rep("pypdf pdf", lambda: "\n".join(p.extract_text() for p in __import__("pypdf").PdfReader("fx/req.pdf").pages))
from charset_normalizer import from_path
rep("charset_normalizer cp1251", lambda: str(from_path("fx/req_cp1251.md").best()))
