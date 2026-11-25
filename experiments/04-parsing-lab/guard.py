"""Рецепт «защищённого входа»: определяет тип по сигнатуре и проблемы файла ДО парсинга.
Проверено на fx/ (см. make_fixtures.py). Зависимости: pymupdf, charset-normalizer (magika опционально)."""
import os, re, sys, zipfile
from dataclasses import dataclass, field

MAX_BYTES = 50 * 1024 * 1024          # лимит входа; у Claude PDF-вход до 32 МБ на запрос, уточнить в плане
MAX_PAGES = 300
MAX_UNZIPPED = 200 * 1024 * 1024      # zip-бомба: сумма распакованных размеров
MAX_RATIO = 100                       # zip-бомба: коэффициент сжатия

@dataclass
class Diag:
    code: str; level: str; msg: str; action: str = ""

@dataclass
class Probe:
    kind: str = "unknown"; diags: list = field(default_factory=list)
    def add(self, *a): self.diags.append(Diag(*a))

def sniff(head: bytes) -> str:
    if not head: return "empty"
    if head.startswith(b"%PDF-") or b"%PDF-" in head[:1024]: return "pdf"
    if head.startswith(b"PK\x03\x04"): return "zip"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"): return "ole"   # .doc ИЛИ зашифрованный .docx
    if head.startswith(b"{\\rtf"): return "rtf"
    if head[:8] in (b"\x89PNG\r\n\x1a\n",) or head[:3] == b"\xff\xd8\xff" or head[:4] in (b"II*\x00", b"MM\x00*"): return "image"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")): return "text"           # UTF-16 BOM
    if re.match(rb"\s*(<!doctype html|<html)", head[:512], re.I): return "html"
    if b"\x00" in head[:4096]:
        # UTF-16 без BOM: каждый второй байт нулевой
        from charset_normalizer import from_bytes
        best = from_bytes(head).best()   # Кириллица в UTF-16LE: старший байт 0x04, не 0x00
        return "text" if best and best.encoding.startswith(("utf_16", "utf_32")) else "binary"
    return "text"

def check(path: str) -> Probe:
    pr = Probe()
    if not os.path.exists(path): pr.add("IN-001", "ERROR", f"Файл не найден: {path}", "Проверьте путь"); return pr
    size = os.path.getsize(path)
    if size == 0: pr.add("IN-002", "ERROR", "Файл пустой (0 байт)", "Передайте исходный документ заново"); return pr
    if size > MAX_BYTES: pr.add("IN-003", "ERROR", f"Файл {size/2**20:.0f} МБ больше лимита {MAX_BYTES/2**20:.0f} МБ", "Разбейте документ"); return pr
    with open(path, "rb") as f: head = f.read(8192)
    kind = sniff(head); pr.kind = kind
    ext = os.path.splitext(path)[1].lower().lstrip(".")
    if kind == "zip":
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                total = sum(i.file_size for i in z.infolist()); packed = sum(i.compress_size for i in z.infolist()) or 1
                if total > MAX_UNZIPPED or total / packed > MAX_RATIO:
                    pr.add("IN-030", "ERROR", f"Архив разворачивается в {total/2**20:.0f} МБ (сжатие ×{total/packed:.0f}) — похоже на zip-бомбу", "Пересохраните документ в Word"); return pr
                if "word/document.xml" in names: pr.kind = "docx"
                elif "content.xml" in names and "mimetype" in names: pr.kind = "odt"
                else: pr.add("IN-031", "ERROR", "ZIP-архив, но не DOCX/ODT (нет word/document.xml)", "Сохраните документ как DOCX или PDF"); return pr
        except zipfile.BadZipFile:
            pr.add("IN-032", "ERROR", "Файл похож на DOCX, но ZIP-структура повреждена (обрезан?)", "Пересохраните документ или пришлите PDF"); return pr
    if kind == "ole":
        try:
            import olefile
            streams = {"/".join(s) for s in olefile.OleFileIO(path).listdir()}
            if "EncryptedPackage" in streams:
                pr.add("IN-021", "ERROR", "Документ Office защищён паролем", "Снимите пароль и пришлите снова"); return pr
        except Exception: pass
        pr.add("IN-033", "ERROR", "Старый формат Word (.doc) не поддерживается напрямую", "Сохраните как DOCX или PDF (или установите LibreOffice для автоконвертации)"); return pr
    if pr.kind not in ("pdf", "docx", "odt", "text", "html", "rtf"):
        pr.add("IN-010", "ERROR", f"Тип содержимого «{pr.kind}» не поддерживается", "Нужен PDF, DOCX, MD/TXT, HTML или RTF"); return pr
    if ext and ext not in (pr.kind, "md", "txt", "markdown", "htm") and not (pr.kind == "text" and ext in ("md", "txt")):
        pr.add("IN-011", "WARN", f"Расширение .{ext} не совпадает с содержимым ({pr.kind}); читаю как {pr.kind}", "")
    if pr.kind == "pdf":
        import pymupdf
        try:
            doc = pymupdf.open(path, filetype="pdf")
        except Exception as e:
            pr.add("IN-040", "ERROR", f"PDF не открывается: {type(e).__name__}", "Файл повреждён; пересохраните или пришлите исходник"); return pr
        if doc.needs_pass:
            pr.add("IN-020", "ERROR", "PDF зашифрован паролем на открытие", "Снимите пароль (или передайте --password)"); return pr
        if doc.is_encrypted or doc.permissions != -4 and not doc.permissions & pymupdf.PDF_PERM_COPY:
            pr.add("IN-022", "INFO", "PDF с ограничениями владельца (копирование запрещено); текст извлечён для проверки", "")
        if doc.is_repaired: pr.add("IN-041", "WARN", "PDF был повреждён и восстановлен автоматически (битый xref)", "Сверьте цитаты с оригиналом")
        if doc.page_count == 0: pr.add("IN-042", "ERROR", "В PDF не найдено ни одной страницы (файл обрезан?)", "Пришлите полный файл"); return pr
        if doc.page_count > MAX_PAGES: pr.add("IN-004", "WARN", f"{doc.page_count} страниц > {MAX_PAGES}: обработаны первые {MAX_PAGES}", "")
        empty = [i + 1 for i, p in enumerate(doc) if i < MAX_PAGES and len(p.get_text().strip()) < 20]
        n = min(doc.page_count, MAX_PAGES)
        if len(empty) == n: pr.add("IN-050", "WARN", f"Нет текстового слоя ни на одной из {n} страниц (скан/картинки)", "Включаю OCR")
        elif empty: pr.add("IN-051", "WARN", f"Нет текста на страницах {empty}", "Эти страницы идут через OCR")
    if pr.kind in ("text", "html", "rtf"):
        from charset_normalizer import from_path
        best = from_path(path).best()
        if best is None: pr.add("IN-060", "ERROR", "Не удалось определить кодировку текста", "Сохраните в UTF-8"); return pr
        if best.encoding not in ("utf_8", "ascii"): pr.add("IN-061", "INFO", f"Кодировка {best.encoding} перекодирована в UTF-8", "")
        if not str(best).strip(): pr.add("IN-070", "ERROR", "Файл содержит только пробелы", ""); return pr
    return pr

if __name__ == "__main__":
    for f in sorted(os.listdir(sys.argv[1])):
        pr = check(os.path.join(sys.argv[1], f))
        print(f"{f:28} kind={pr.kind:7} " + "; ".join(f"{d.code} {d.level}: {d.msg[:60]}" for d in pr.diags))
