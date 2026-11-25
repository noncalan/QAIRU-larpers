"""Генератор битых/пограничных входов для агента проверки требований.
Запуск: python make_fixtures.py fx/   (в fx/ уже лежат req.md, req.docx, req.pdf, req.doc, req.rtf, req.html, req.odt)
Зависимости: pymupdf, pikepdf.
"""
import io, os, sys, zipfile, random
import pymupdf, pikepdf

d = sys.argv[1] if len(sys.argv) > 1 else "fx"
P = lambda n: os.path.join(d, n)
src_pdf, src_docx, src_md = P("req.pdf"), P("req.docx"), P("req.md")
pdf_bytes = open(src_pdf, "rb").read()
md_text = open(src_md, encoding="utf-8").read()

# 1. Пустые
open(P("zero.pdf"), "wb").close()
open(P("zero.docx"), "wb").close()
open(P("zero.md"), "wb").close()
open(P("whitespace.md"), "w", encoding="utf-8").write("\n\n   \n\t\n")
doc = pymupdf.open(); doc.new_page(); doc.save(P("blank_page.pdf"))

# 2. Скан: рендерим страницы в картинки, собираем PDF без текстового слоя
src = pymupdf.open(src_pdf); scan = pymupdf.open()
for page in src:
    pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    p = scan.new_page(width=page.rect.width, height=page.rect.height)
    p.insert_image(p.rect, stream=pix.tobytes("png"))
scan.save(P("scan.pdf"), deflate=True)
# Смешанный: 1-я страница текстом, остальные сканом
mixed = pymupdf.open(); mixed.insert_pdf(src, from_page=0, to_page=0); mixed.insert_pdf(scan, from_page=1)
mixed.save(P("mixed_text_scan.pdf"))

# 3. Шифрование
with pikepdf.open(src_pdf) as pdf:
    pdf.save(P("enc_user.pdf"), encryption=pikepdf.Encryption(user="secret", owner="owner", R=6))
    pdf.save(P("enc_owner_only.pdf"), encryption=pikepdf.Encryption(
        user="", owner="owner", R=6, allow=pikepdf.Permissions(extract=False, accessibility=False)))

# 3a. Текстовый слой есть, но нечитаемый (удалены /ToUnicode у шрифтов) -> извлекается мусор \x01\x02...
with pikepdf.open(src_pdf) as pdf:
    for page in pdf.pages:
        for _, font in (page.Resources.get("/Font") or {}).items():
            if "/ToUnicode" in font: del font["/ToUnicode"]
    pdf.save(P("no_tounicode.pdf"))

# 4. Повреждения PDF
open(P("truncated_60.pdf"), "wb").write(pdf_bytes[: int(len(pdf_bytes) * 0.6)])
open(P("truncated_5.pdf"), "wb").write(pdf_bytes[: int(len(pdf_bytes) * 0.05)])
b = bytearray(pdf_bytes); i = b.rfind(b"startxref")
b[i + 10 : i + 16] = b"999999"  # битое смещение xref
open(P("bad_xref.pdf"), "wb").write(bytes(b))
b = bytearray(pdf_bytes); i = b.find(b"xref\n")
if i > 0: b[i : i + 200] = b"\x00" * 200  # затёртая таблица xref
open(P("zeroed_xref.pdf"), "wb").write(bytes(b))
open(P("garbage.pdf"), "wb").write(random.Random(1).randbytes(50_000))
open(P("header_only.pdf"), "wb").write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")

# 5. Неверное расширение
open(P("pdf_named.docx"), "wb").write(pdf_bytes)
open(P("docx_named.pdf"), "wb").write(open(src_docx, "rb").read())
open(P("md_named.pdf"), "w", encoding="utf-8").write(md_text)
png = src[0].get_pixmap(dpi=72).tobytes("png")
open(P("image_named.pdf"), "wb").write(png)
open(P("image.png"), "wb").write(png)

# 6. Битый DOCX
db = open(src_docx, "rb").read()
open(P("docx_truncated.docx"), "wb").write(db[: len(db) // 2])
open(P("docx_not_zip.docx"), "w").write("это не zip")
with zipfile.ZipFile(src_docx) as zin, zipfile.ZipFile(P("docx_no_document_xml.docx"), "w") as zout:
    for it in zin.infolist():
        if it.filename != "word/document.xml":
            zout.writestr(it, zin.read(it.filename))
with zipfile.ZipFile(src_docx) as zin, zipfile.ZipFile(P("docx_bad_xml.docx"), "w") as zout:
    for it in zin.infolist():
        data = zin.read(it.filename)
        if it.filename == "word/document.xml": data = data[: len(data) // 2]
        zout.writestr(it, data)
try:  # DOCX с паролем на открытие (становится OLE-контейнером, как .doc)
    import msoffcrypto
    with open(src_docx, "rb") as i, open(P("docx_password.docx"), "wb") as o:
        msoffcrypto.OfficeFile(i).encrypt("secret", o)
except ImportError:
    print("pip install msoffcrypto-tool для docx_password.docx")
# Zip-бомба: document.xml ~ 1 ГБ пробелов внутри валидного DOCX (сжимается до ~1 МБ)
with zipfile.ZipFile(src_docx) as zin, zipfile.ZipFile(P("docx_bomb.docx"), "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
    for it in zin.infolist():
        if it.filename == "word/document.xml":
            head = zin.read(it.filename)
            cut = head.find(b"<w:body>") + len(b"<w:body>")
            with zout.open("word/document.xml", "w", force_zip64=True) as f:
                f.write(head[:cut]); chunk = b" " * (1 << 20)
                for _ in range(1024): f.write(chunk)
                f.write(head[cut:])
        else:
            zout.writestr(it, zin.read(it.filename))

# 7. Кодировки
open(P("req_cp1251.md"), "wb").write(md_text.encode("cp1251", errors="replace"))
open(P("req_utf16.md"), "wb").write(md_text.encode("utf-16"))          # с BOM
open(P("req_utf16le_nobom.md"), "wb").write(md_text.encode("utf-16-le"))
open(P("req_utf8_bom.md"), "wb").write(md_text.encode("utf-8-sig"))
open(P("req_koi8r.txt"), "wb").write(md_text.encode("koi8-r", errors="replace"))

# 8. Не про требования
novel = ("Глава первая. Было раннее утро, когда Анна вышла из дома и направилась к реке. "
         "Туман лежал над водой, и где-то вдалеке кричали птицы. ") * 200
open(P("novel.md"), "w", encoding="utf-8").write(novel)
open(P("template_empty.md"), "w", encoding="utf-8").write(
    "# Техническое задание\n\n## Цели\n\n<заполнить>\n\n## Функциональные требования\n\n| ID | Требование |\n|---|---|\n| | |\n")
open(P("mixed_lang.md"), "w", encoding="utf-8").write(
    "# Требования / Талаптар / Requirements\n\n* **R-1.** Система должна отправлять push.\n"
    "* **R-2.** Жүйе клиентке хабарлама жіберуі тиіс.\n* **R-3.** The system shall log every event.\n")

# 9. Большой: 2000 страниц текста
big = pymupdf.open()
for k in range(2000):
    big.insert_pdf(src, from_page=k % len(src), to_page=k % len(src))
big.save(P("big_2000p.pdf"), garbage=0)
print("ok", len(os.listdir(d)), "files")
