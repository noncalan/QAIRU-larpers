# Попытка спасти обрезанный PDF: найти объекты страниц и собрать их в новый документ через дописанный trailer
import re, sys, pikepdf, pymupdf
b = open(sys.argv[1], "rb").read()
objs = {int(m.group(1)): m.start() for m in re.finditer(rb"(\d+) 0 obj", b)}
pages = [n for n, off in objs.items() if re.match(rb"\d+ 0 obj\s*<<[^>]*?/Type\s*/Page[^s]", b[off:off+400], re.S)]
print("objects:", len(objs), "page objs:", len(pages))
# дописываем каталог + дерево страниц + trailer и даём qpdf/pikepdf перестроить xref
mx = max(objs) + 1
kids = " ".join(f"{n} 0 R" for n in sorted(pages))
tail = (f"\nendobj\n{mx} 0 obj << /Type /Pages /Kids [{kids}] /Count {len(pages)} >> endobj\n"
        f"{mx+1} 0 obj << /Type /Catalog /Pages {mx} 0 R >> endobj\n"
        f"trailer << /Root {mx+1} 0 R /Size {mx+2} >>\n%%EOF\n").encode()
open(sys.argv[2], "wb").write(b + tail)
try:
    with pikepdf.open(sys.argv[2]) as pdf:
        for p in pdf.pages: p.obj.Parent = pdf.Root.Pages  # починить ссылки на родителя
        pdf.save(sys.argv[2] + ".fixed.pdf")
    d = pymupdf.open(sys.argv[2] + ".fixed.pdf")
    ok = [i for i, pg in enumerate(d) if pg.get_text().strip()]
    print("salvaged pages:", d.page_count, "with text:", ok, "chars:", sum(len(pg.get_text()) for pg in d))
except Exception as e:
    print("salvage failed:", type(e).__name__, str(e)[:120])
