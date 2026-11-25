import sys, time, resource, warnings; warnings.filterwarnings("ignore")
t=time.perf_counter()
try:
    if sys.argv[1]=="python-docx":
        import docx; d=docx.Document("fx/docx_bomb.docx"); print("paras", len(d.paragraphs))
    elif sys.argv[1]=="mammoth":
        import mammoth; r=mammoth.convert_to_html(open("fx/docx_bomb.docx","rb")); print("chars", len(r.value))
    elif sys.argv[1]=="markitdown":
        from markitdown import MarkItDown; print("chars", len(MarkItDown().convert("fx/docx_bomb.docx").text_content))
    elif sys.argv[1]=="pymupdf":
        import pymupdf; d=pymupdf.open("fx/docx_bomb.docx"); print("pages", d.page_count)
except Exception as e: print("ERR", type(e).__name__, str(e)[:100])
print(f"{sys.argv[1]}: {time.perf_counter()-t:.1f}s maxrss={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f}MB")
