# 04. Извлечение текста (PDF/DOCX/MD и др.) и устойчивость к битым файлам

> **Поправка по решению D-006 (25.11):** модель агента — Claude API. Везде, где запасным путём для страниц без текста назван Gemini, читать «PDF напрямую в Claude» (блок `document`, до 600 страниц; лимит размера запроса сверить по документации, см. D-006; на скане сами ещё не проверяли, это тема 19). Для якорей цитат в PDF брать `get_text(sort=True)` или pdfplumber, а не `get_text()` по умолчанию: поправка из исследования 18, раздел 2.6. Пример кода с `google.genai` устарел, ключ в сообщении LLM-001 теперь `ANTHROPIC_API_KEY`. Проверка входа, парсеры и фикстуры от модели не зависят.

Дата: 25.11.2026. Опирается на `case-analysis.md` (защищённый вход, якоря, MD+PDF отчёт).
Лаборатория со всеми скриптами и фикстурами: `experiments/04-parsing-lab/`
(`make_fixtures.py` — генератор битых файлов, `probe*.py` — замеры, `guard.py` — рабочий рецепт проверки входа, `probe.out` — сырой вывод).

Пометки: **[проверено]** — запускал сам на наших файлах (Linux x86_64, Python 3.13, CPU); **[док]** — по документации или PyPI; **[не проверено]** — не удалось проверить.

---

## TL;DR

1. **PyMuPDF 2.x поменялся.** С 2.0 (2.1 вышла 02.11.2026) `pymupdf4llm` и `pymupdf-layout` встроены в `pymupdf`. Отдельный `pymupdf4llm` теперь пустая заглушка. `pip install pymupdf` тянет onnxruntime и numpy, вместе ≈200 МБ в venv (было ~25 МБ на 1.26). Нужен Python ≥3.11, лицензия AGPL-3.0 **[проверено]**. Для открытого хакатонного репо AGPL не блокер. Для банка-заказчика это красный флаг, поэтому PDF-бэкенд держим за интерфейсом, а запасной путь делаем на лицензиях без копилефта (pypdfium2 + pdfplumber).
2. **Кириллица извлекается хорошо почти везде, кроме pypdf.** Доля слов исходника в выводе: PyMuPDF / pdfplumber / pdfium / markitdown — 0,97–0,98. У pypdf 7.1 — **0,40**: в тексте с выравниванием по ширине он склеивает слова («Комплектиздвухисходныхдокументов») **[проверено]**. pypdf для текста не берём.
3. **В таблицах PDF ячейки портятся у всех.** pymupdf4llm переставляет слова и запятые («JIRA Задача в», «,<br>Мобильный банк учебный кейс»), а символ ₸ уезжает **[проверено]**. Поэтому сверка цитат должна быть нечёткой (нормализация пробелов и пунктуации, сравнение по токенам). Для DOCX и MD таблицы извлекаются без потерь.
4. **Ловушка OCR.** pymupdf4llm запускает OCR сам, но по умолчанию с `ocr_language="eng"`, и скан на русском превращается в «YBeglomsenne o pacxogax no KapTe». С `ocr_language="rus+eng", ocr_dpi=300` получается recall слов 0,90 и precision 0,86 при ≈3 с/стр на CPU **[проверено]**. У жюри Tesseract может не оказаться. Тогда основной путь OCR — отдать PDF в Gemini: модель читает PDF «native vision», до 1000 страниц и 50 МБ, 258 токенов на страницу **[док; на нашем скане не проверено — не было ключа]**.
5. **«Нет распознаваемого текста» бывает двух видов.** (а) Скан: текста нет вообще. (б) Текстовый слой есть, но это мусор. Пример: удаляем `/ToUnicode` у шрифтов, и PyMuPDF выдаёт `\x01\x02\x03…` — 0 кириллических букв из 8715 символов **[проверено]**. Нужна эвристика качества текста по доле букв и управляющих символов, а не только «текст пустой?».
6. **Повреждённые PDF.** Битый `startxref` или затёртый xref чинят все пять библиотек, у PyMuPDF при этом выставлен флаг `doc.is_repaired` **[проверено]**. PDF, обрезанный до 60 %, не открыли ни PyMuPDF, ни pypdf, pdfplumber, pikepdf/qpdf, pdfium, pdftotext. Помог приём «дописать Pages+Catalog+trailer и пересобрать через pikepdf»: восстановлены 8 из 8 страниц, 8668 из 8660 символов **[проверено на одном файле]**.
7. **Шифрование.** При пароле только владельца (owner-only, «копирование запрещено») текст без пароля читают все библиотеки. При пароле на открытие у PyMuPDF `needs_pass=True`, остальные бросают `PasswordError` **[проверено]**. DOCX с паролем — это OLE-контейнер с той же сигнатурой `D0 CF 11 E0`, что у старого .doc. Различать по потоку `EncryptedPackage` (olefile) **[проверено]**.
8. **Zip-бомба в DOCX** (1 МБ на диске, `document.xml` на 1 ГБ). python-docx занял 2 ГБ RSS и упал с `XMLSyntaxError`. mammoth и markitdown не закончили за 90–100 с **[проверено]**. Проверка `sum(i.file_size for i in ZipFile.infolist())` до парсинга занимает доли миллисекунды. Тип файла определяем своей проверкой сигнатур (~30 строк) с `charset-normalizer` для текстов. `magika` (идёт вместе с markitdown, без libmagic) верно опознала все фикстуры, а `filetype` и `puremagic` не видят MD/TXT, причём puremagic принял ODT за DOCX **[проверено]**.

---

## 1. Сравнительная таблица парсеров

Скорость и качество — на `req.pdf` (8 стр., сделан из `docs/requirements.md` через pandoc → DOCX → LibreOffice → PDF) и `req.docx`. Recall — доля уникальных кириллических слов (от 3 букв) исходного MD, найденных в выводе.

| Инструмент (версия на PyPI, ноябрь 2026) | Лицензия | Что читает | Таблицы | Структура (заголовки, списки) | Страницы для якорей | Кириллица, recall | Время, 8 стр. | Вес / зависимости | Win / macOS / Linux | Вердикт |
|---|---|---|---|---|---|---|---|---|---|---|
| **PyMuPDF 2.1** `get_text()` | AGPL-3.0 или коммерческая | PDF, XPS, EPUB, HTML, TXT; DOCX открыл даже с расширением .pdf (по содержимому) | `page.find_tables()` | нет (плоский текст) | да, постранично; bbox | 0,977 | **0,014 с**; 2000 стр. за 3,9 с | ≈110 МБ сам + onnxruntime 73 МБ + numpy 30 МБ; Py ≥3.11 | колёса для всех трёх **[док]**, проверял только Linux | основной движок PDF |
| **PyMuPDF 2.1** `to_markdown()` (бывший pymupdf4llm + layout) | AGPL-3.0 (раньше layout был PolyForm Noncommercial, в 2.x его код внутри AGPL-пакета) | PDF и всё, что открывает MuPDF | Markdown-таблицы (99 строк), в ячейках перестановки слов | **да**: 26 заголовков, списки, жирный | `page_chunks=True` → список по страницам с метаданными | 0,969 | 2,5 с (ONNX-модель layout) | то же | то же | основной путь PDF → Markdown |
| pypdfium2 5.15.2 (PDFium) | Apache-2.0 / BSD-3 | PDF | нет | нет | да | 0,969 | 0,01 с | **4 МБ**, без зависимостей | колёса для всех трёх **[док]** | запасной движок без копилефта |
| pdfplumber 0.11.11 | MIT | PDF (на pdfminer.six + pypdfium2) | **лучшие среди лёгких** (`extract_tables`) | нет | да | 0,977 | 0,32 с | лёгкий, чистый Python + pdfium | везде **[док]** | запасной путь для таблиц |
| pdfminer.six 20260107 | MIT | PDF | нет | нет | да | ≈ как pdfplumber (внутри него) | ≈0,3 с | чистый Python | везде | только через pdfplumber/markitdown |
| pypdf 7.1.1 | BSD-3 | PDF | нет | нет | да | **0,399**: склеивает слова | 0,16 с | чистый Python | везде | для текста не брать; годится для метаданных и шифрования |
| pikepdf 10.18.2 (qpdf) | MPL-2.0 | PDF: ремонт, расшифровка, пересборка | — | — | — | — | мгновенно | ≈10 МБ, колёса | везде **[док]** | ремонт и спасение обрезанных PDF |
| **markitdown 0.1.9** (Microsoft) | MIT | DOCX (через mammoth), HTML, PDF (pdfminer), XLSX, PPTX, TXT/MD с автоопределением кодировки | DOCX/HTML → Markdown-таблицы | **да** для DOCX/HTML (34 заголовка) | **нет** для PDF (один поток текста) | DOCX 1,0; PDF 0,977 | DOCX 0,16 с | ядро лёгкое + magika (onnxruntime); `[docx,pdf]` добавляет mammoth, lxml, pdfminer, pdfplumber | везде | основной путь для DOCX/HTML. RTF не понимает (вернул сырой RTF), `.doc` — `UnsupportedFormatException` |
| python-docx 1.2.0 | MIT | DOCX | `doc.tables` | стили абзацев (Heading N) | страниц в DOCX нет | 1,0 | 0,03 с | лёгкий (lxml) | везде | для якорей «абзац N, таблица M» |
| mammoth 1.13.0 | BSD-2 | DOCX → HTML/MD | в HTML да | в MD заголовков не было (MD-вывод устаревший, лучше HTML → markdownify) | нет | 1,0 | 0,07 с | лёгкий | везде | внутри markitdown |
| pandoc 3.x (pypandoc 1.17) | GPL-2.0+ (внешний бинарник) | DOCX, ODT, HTML, RTF, MD, EPUB… | да | **да** (34 заголовка из DOCX) | нет | DOCX 1,0; RTF 0,972 | 0,1–0,15 с | отдельный бинарник ~150 МБ, у жюри его может не быть | везде, но ставится отдельно | необязательно: RTF/ODT, если найден в PATH |
| striprtf 0.0.35 | BSD-3 | RTF → текст | нет | нет | нет | 1,0 | 0,02 с | чистый Python | везде | RTF без pandoc |
| docling 2.152 (IBM) | MIT (код) | PDF, DOCX, PPTX, HTML, изображения | очень хорошие (TableFormer) | да | да | **[не проверено]** | **[не проверено]**, на CPU заметно медленнее PyMuPDF | `docling-slim[standard]` тянет **torch**, accelerate, scipy, rapidocr, модели IBM с HuggingFace (ГБ) | Windows: torch и загрузка моделей при первом запуске | избыточно для нашего документа |
| marker-pdf 2.2.0 (Datalab) | код Apache-2.0; веса «modified AI Pubs Open Rail-M»: бесплатно для исследований, личного пользования и стартапов до $5 млн | PDF, изображения, PPTX, DOCX, XLSX, HTML, EPUB (`[full]`) | да, `--use_llm` склеивает таблицы | да | да | **[не проверено]** | **[не проверено]** | **torch ≥2.7**, surya-ocr, onnxruntime | GPU желателен | не брать: тяжело, оговорки по весам |
| MinerU 4.2.3 | «MinerU Open Source License» (с 3.1 на базе Apache-2.0, с условиями атрибуции и порогами коммерческого использования; раньше AGPL) | PDF, изображения | очень хорошие | да | да | **[не проверено]** | на CPU медленно | torch, onnx, модели; «GPU от 8 ГБ VRAM на практике» | Windows проблемная | не брать |
| unstructured 0.27.38 | Apache-2.0 | почти всё | с `hi_res` да | элементы Title/ListItem | да | **[не проверено]** | **[не проверено]** | `[pdf]` тянет unstructured-inference, pdf2image (poppler), tesseract, google-cloud-vision | системные зависимости на Windows | не брать |
| Tesseract 5 + `rus` | Apache-2.0 | изображения (через PyMuPDF `get_textpage_ocr` или pymupdf4llm) | плохо: слитые слова в ячейках | нет | да (по странице) | 0,90 / precision 0,86 при dpi 300 | ≈2,4–3 с/стр | системный бинарник + языковой пакет `rus` | Linux: `apt install tesseract-ocr-rus`; macOS: `brew install tesseract tesseract-lang`; Windows: инсталлятор UB Mannheim с галочкой Russian **[док]** | необязательный локальный OCR |
| ocrmypdf 17.14.2 | MPL-2.0 | PDF → PDF с текстовым слоем | — | — | да | ≈ как Tesseract | медленнее (Ghostscript) | Tesseract + Ghostscript | на Windows громоздко | не нужен |
| **Gemini (PDF напрямую)** | API Google | PDF до **1000 стр. / 50 МБ** (inline и File API); TXT/MD/HTML/XML как текст; DOCX в списке нет | понимает визуально | да | номер страницы можно попросить в ответе | **[не проверено]** | сеть + 258 токенов/стр. | ничего ставить не нужно | везде | основной путь OCR для сканов и последний запасной для «не открылось» |

Замечания по таблице:
- По Gemini: «для Gemini 3 текст, встроенный в PDF, извлекается и передаётся модели», а изображения страниц масштабируются до 768–3072 пикселей **[док, ai.google.dev/gemini-api/docs/document-processing]**. Как модель поведёт себя на зашифрованном или битом PDF, **[не проверено]**.
- MuPDF открывает файл по содержимому, а не по расширению: `pymupdf.open("x.pdf")` с DOCX внутри вернул `is_pdf=False` и 15 «страниц». Когда тип не PDF, вызывать `doc.permissions` нельзя: в 2.1 это даёт `TypeError` **[проверено]**. Вывод: сначала свой sniff, потом `pymupdf.open(path, filetype="pdf")` только для PDF.
- В README PyMuPDF 2.x есть платный модуль `pymupdf.office` (DOCX/XLSX/PPTX/HWPX). Без ключа он даёт 3 страницы и ограничен по времени. Не путать с бесплатным открытием DOCX самим MuPDF: оно сработало, но без таблиц и структуры.

### AGPL: проблема ли для нас

- **Для сдачи на хакатоне — нет.** Репо открытое. Распространяем свой исходник и `requirements.txt`, сам PyMuPDF не распространяем. AGPL-обязательства (раскрыть исходники при распространении или работе по сети) мы выполняем по построению. Проще всего указать в README, что зависимость под AGPL, и выбрать для репо лицензию, совместимую с AGPL (MIT/Apache-2.0 совместимы в одну сторону; AGPL-3.0 — без вопросов).
- **Для банка как будущего пользователя — да.** Внутренний сервис с изменённым кодом под AGPL требует либо раскрытия исходников, либо коммерческой лицензии Artifex. Поэтому: (1) интерфейс `PdfBackend` с реализациями `mupdf` (по умолчанию) и `pdfium` (pypdfium2 + pdfplumber, Apache/BSD/MIT), переключение флагом `--pdf-backend`; (2) в README абзац «лицензии зависимостей». Хороший ответ, если лид QA спросит.
- Старый `pymupdf-layout 1.27.x` был под **PolyForm Noncommercial**. Не пиннить `pymupdf<2` вместе с layout: с некоммерческой лицензией хуже, чем с AGPL.

---

## 2. Рекомендуемый стек

**Python 3.11+** (этого требует PyMuPDF 2.x). Все зависимости ставятся колёсами через pip, без системных пакетов. Tesseract, pandoc и LibreOffice подхватываются, только если уже есть в PATH.

```
pymupdf==2.1            # PDF: текст, Markdown с таблицами, ремонт, рендер страниц, OCR-хук
pypdfium2==5.15.2       # запасной движок PDF (другой парсер — другие баги)
pdfplumber==0.11.11     # запасной путь для таблиц в PDF
pikepdf==10.18.2        # расшифровка (--password), спасение обрезанных PDF
markitdown[docx]==0.1.9 # DOCX/HTML → Markdown (mammoth + markdownify), тянет magika и charset-normalizer
python-docx==1.2.0      # якоря DOCX: номер абзаца или таблицы
charset-normalizer>=3.5 # кодировки TXT/MD (cp1251, koi8-r, UTF-16 без BOM)
olefile, striprtf       # .doc и зашифрованный DOCX; RTF
```

**Путь по типу (тип определяем по сигнатуре, см. `guard.py`):**

| Тип | Основной путь | Запасной путь | Якорь в отчёте |
|---|---|---|---|
| PDF с текстом | `pymupdf.to_markdown(page_chunks=True)` → Markdown по страницам | при исключении или мусоре: `pymupdf get_text()`, затем `pypdfium2`; таблицы: `pdfplumber.extract_tables()` | «стр. N, строка K» и ID требования |
| PDF, где на части или всех страницах нет текста либо текст-мусор | страницы без текста → **Gemini** (отдать PDF только из этих страниц: `doc.select([...])`) с просьбой вернуть текст с маркерами `=== стр. N ===` | Tesseract `rus+eng` (`to_markdown(ocr_language="rus+eng", ocr_dpi=300)`), если найден в PATH | «стр. N (OCR)»: цитату помечаем как распознанную |
| PDF битый | PyMuPDF чинит сам (`is_repaired`) | `pikepdf` пересохранить → спасение обрезанного (дописать trailer) → Gemini с исходными байтами | + предупреждение «сверьте с оригиналом» |
| DOCX | `markitdown` → Markdown с заголовками и таблицами | `python-docx`, абзацы и таблицы по отдельности | «раздел „…“, абзац N» или «таблица M, строка R» |
| MD / TXT | `charset-normalizer` → UTF-8, как есть | — | «строка N» (номер строки исходного файла) |
| HTML | `markitdown` | pandoc, если есть | «раздел „…“» |
| RTF | `striprtf` | pandoc, если есть | «абзац N» |
| ODT | pandoc или LibreOffice, если есть, иначе диагностика | — | — |
| .doc | LibreOffice `soffice --headless --convert-to docx`, если найден | иначе диагностика «сохраните как DOCX/PDF» | — |

**Почему так:**
- PyMuPDF — самый быстрый и устойчивый из проверенных. Он сам чинит xref, даёт постраничный Markdown с таблицами и заголовками и умеет рендерить страницы для OCR, всё без torch. Минусы — AGPL и ~200 МБ, поэтому рядом лёгкий запасной путь без копилефта.
- Gemini как основной OCR: ключ всё равно нужен для проверки, а жюри не придётся ставить Tesseract. Tesseract остаётся вариантом для работы без сети.
- Docling, marker, MinerU и unstructured дают выигрыш на сложных научных PDF. Наш документ простой. Цена — torch, гигабайты моделей и долгий первый запуск на неизвестной машине жюри.
- Из MD парсер не нужен: исходник выдан в MD, и это лучший вход. Для PDF-версии модели стоит передавать и Markdown, и номера страниц.

---

## 3. Матрица отказов с рецептами

Общий принцип: **файл отчёта пишется всегда**. Вход проходит `check()` (рецепт целиком — в `experiments/04-parsing-lab/guard.py`, проверен на всех фикстурах), затем парсер в `try/except`, затем проверка качества текста, затем проверка «похоже ли на требования». Колонка «Факт» — что реально случилось у библиотек на наших файлах.

| # | Случай | Как детектировать | Факт [проверено] | Что пишет агент в «Диагностику» | Код | Частичная обработка |
|---|---|---|---|---|---|---|
| 1 | Файл не найден или нет прав | `os.path.exists`, `PermissionError` | — | IN-001 ERROR «Файл не найден: путь» | 2 | нет; отчёт пишется, если путь отчёта доступен |
| 2 | 0 байт | `os.path.getsize()==0` | PyMuPDF/pypdf: `EmptyFileError`; pdfplumber/pikepdf: «No /Root» / «unable to find trailer» | IN-002 ERROR «Файл пустой (0 байт). Передайте документ заново» | 4 | нет |
| 3 | Только пробелы / пустой шаблон («<заполнить>», таблица без строк) | после извлечения `len(text.strip())<N`; нет модальных слов и ID | — | IN-070 ERROR «Файл содержит только пробелы» / IN-081 WARN «Похоже на пустой шаблон: 0 требований» | 4 / 3 | отчёт с паспортом и «0 требований» |
| 4 | PDF без страниц (обрезан до заголовка, `%PDF-` и всё) | `doc.page_count==0`, либо `FileDataError` | `truncated_5.pdf`: PyMuPDF открыл, «repaired», 0 стр.; `header_only.pdf`: все падают | IN-042 ERROR «В PDF нет ни одной страницы (файл обрезан?)» | 4 | нет |
| 5 | Пустой PDF (страница без текста) | все страницы `len(get_text().strip())<20` | `blank_page.pdf`: 1 стр., 0 символов у всех | IN-050 WARN «Нет текстового слоя ни на одной из 1 стр.» → OCR → если OCR тоже пуст, IN-071 ERROR «Документ не содержит текста» | 4 | нет |
| 6 | **Скан** (только картинки) | как п. 5 плюс `page.get_images()` не пуст | `scan.pdf`: 8 стр., 0 символов. OCR rus+eng: recall 0,90 | IN-050 WARN «8/8 стр. без текста, распознано через Gemini/Tesseract; цитаты помечены (OCR)» | 3 | полная проверка по OCR-тексту с пометкой |
| 7 | **Смешанный** (часть страниц — скан) | постраничный подсчёт символов | `mixed_text_scan.pdf`: текст только на стр. 1 | IN-051 WARN «Нет текста на стр. 2–8 → OCR» | 3 | да, постранично |
| 8 | **Текстовый слой — мусор** (нет ToUnicode, «кракозябры») | доля букв < 0,5 или доля управляющих символов, PUA и U+FFFD > 0,1 | `no_tounicode.pdf`: 8715 символов, 0 кириллицы, 5817 управляющих | IN-052 WARN «Текст в PDF нечитаем (шрифты без таблицы Unicode) → OCR» | 3 | OCR, как для скана |
| 9 | PDF с паролем на открытие | `doc.needs_pass` | PyMuPDF `needs_pass=True`; pypdf `decrypt("")` → 0; pdfplumber `PDFPasswordIncorrect`; pikepdf `PasswordError` | IN-020 ERROR «PDF зашифрован паролем. Снимите пароль или запустите с `--password`» | 4 | нет (с `--password`: `doc.authenticate(pw)`) |
| 10 | PDF только с owner-паролем (запрет копирования) | `doc.is_encrypted` и `not doc.permissions & PDF_PERM_COPY` | все пять библиотек извлекли текст без пароля | IN-022 INFO «PDF с ограничениями владельца; текст извлечён для проверки» | 0 | полная |
| 11 | Битый xref / startxref | `doc.is_repaired` | все пять открыли, 8 стр. | IN-041 WARN «PDF был повреждён и восстановлен автоматически» | 3 | полная |
| 12 | **Обрезанный PDF** (нет trailer) | `FileDataError` у PyMuPDF и `PdfError` у pikepdf | никто не открыл (включая qpdf и pdftotext); приём со спасением — 8/8 стр. | IN-043 WARN «PDF обрезан; восстановлено N из M страниц, проверка по восстановленному» или IN-040 ERROR | 3 / 4 | да, по спасённым страницам |
| 13 | Мусор с расширением .pdf | sniff: нет `%PDF-`, не ZIP, не текст | все падают | IN-010 ERROR «Содержимое не распознано как документ» | 4 | нет |
| 14 | Неверное расширение (PDF→.docx, DOCX→.pdf, MD→.pdf) | sniff по сигнатуре ≠ расширение | python-docx/mammoth на PDF: `PackageNotFoundError`/`BadZipFile`; PyMuPDF на DOCX.pdf открыл как DOCX | IN-011 WARN «Расширение .pdf не совпадает с содержимым (docx); читаю как docx» | 0 | полная |
| 15 | Картинка (PNG/JPG) вместо документа | sniff по сигнатурам PNG/JPEG/TIFF | — | Вариант A: IN-010 ERROR «Изображение не поддерживается». Вариант B (лучше): обернуть в PDF (`pymupdf.open(img).convert_to_pdf()`), передать в OCR, выдать IN-053 WARN | 4 / 3 | B: да |
| 16 | DOCX — битый ZIP (обрезан) | `zipfile.BadZipFile` | python-docx `PackageNotFoundError`, mammoth `BadZipFile` | IN-032 ERROR «DOCX повреждён (ZIP-структура). Пересохраните или пришлите PDF» | 4 | нет (зачем чинить: `zip -FF` ненадёжен) |
| 17 | DOCX без `word/document.xml` | `namelist()` | python-docx `KeyError`, mammoth `OSError` | IN-031 ERROR «ZIP-архив, но не DOCX» | 4 | нет |
| 18 | DOCX с битым XML внутри | исключение парсера (`XMLSyntaxError`/`ExpatError`) | python-docx и mammoth падают | IN-034 ERROR «Внутренний XML документа повреждён» | 4 | можно попробовать `lxml.etree.XMLParser(recover=True)` **[не проверено]** |
| 19 | **Zip-бомба** в DOCX | `sum(file_size) > 200 МБ` или коэффициент сжатия > 100 | python-docx: 2 ГБ RSS и ошибка; mammoth и markitdown: таймаут >90 с | IN-030 ERROR «Архив разворачивается в 1024 МБ (×1015) — отклонено» | 4 | нет |
| 20 | DOCX с паролем | OLE-сигнатура + поток `EncryptedPackage` | — | IN-021 ERROR «Документ Office защищён паролем» | 4 | нет |
| 21 | Старый .doc | OLE без `EncryptedPackage` | markitdown: `UnsupportedFormatException` | IN-033 ERROR «Формат .doc: сохраните как DOCX/PDF» (или конвертация через `soffice`, если найден, + IN-035 INFO) | 4 / 0 | если есть LibreOffice — полная |
| 22 | RTF | `{\rtf` | markitdown вернул сырой RTF (recall 0) | IN-061 INFO «RTF прочитан как текст (форматирование и таблицы упрощены)» | 0 | striprtf: recall 1,0 |
| 23 | HTML | `<!doctype html` / `<html` | markitdown: 35 заголовков, 58 строк таблиц | — | 0 | полная |
| 24 | TXT/MD в cp1251 / koi8-r | `charset_normalizer.from_path().best()` | определены cp1251 и koi8_r | IN-061 INFO «Кодировка cp1251 → UTF-8» | 0 | полная |
| 25 | UTF-16 с BOM и без | BOM `FF FE`; без BOM — нули в байтах, затем charset-normalizer | оба определены (без BOM мой первый эвристический sniff ошибся: у кириллицы старший байт 0x04, а не 0) | IN-061 INFO | 0 | полная |
| 26 | Очень большой файл | размер > 50 МБ, страниц > 300, токенов больше бюджета | 2000 стр. (166 МБ): PyMuPDF открыл за 0,01 с, извлёк за 3,9 с | IN-003 ERROR (размер) или IN-004 WARN «обработаны первые 300 стр.» | 4 / 3 | обрезка по страницам или разделам |
| 27 | Не про требования (роман, статья) | эвристика: на 1000 слов < 1 модального слова («должен/должна/обязан/shall/must/не более/не менее»), нет ID (`[А-ЯA-Z]{1,4}-\d+`); затем подтверждение LLM | — | IN-080 WARN «Документ не похож на спецификацию требований: найдено 0 формулировок-обязательств, LLM: роман». Проверку всё равно выполнить, но с итогом «не применимо» | 3 | да, с оговоркой |
| 28 | Смешанные языки (рус + каз + англ) | доля кириллицы и латиницы; казахские буквы `әғқңөұүһі` | charset-normalizer дал «Finnish», «Bulgarian», «English» для русских текстов — для языка **не годится** | IN-090 INFO «Языки: ru 80 %, kk 10 %, en 10 %; отчёт на русском, цитаты на языке оригинала» | 0 | полная |
| 29 | LLM недоступна, нет ключа, исчерпан лимит | исключения SDK, 401/429 | — | LLM-001 ERROR «Нет ключа GEMINI_API_KEY» / LLM-002 «Лимит исчерпан, проверено 12/30 требований» | 5 | да: что успели и линтер правил |
| 30 | Неожиданное исключение | `except Exception` верхнего уровня | — | INT-001 ERROR «Внутренняя ошибка: тип; лог: путь» (без трейсбека в отчёте, трейсбек в лог) | 1 | что успели |

### Ключевые сниппеты (все API проверены на PyMuPDF 2.1, pikepdf 10.18, charset-normalizer 3.5)

**Определение типа по сигнатуре (без libmagic):**
```python
def sniff(head: bytes) -> str:
    if not head: return "empty"
    if b"%PDF-" in head[:1024]: return "pdf"
    if head.startswith(b"PK\x03\x04"): return "zip"            # docx/odt/xlsx: смотреть namelist()
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"): return "ole"  # .doc ИЛИ docx с паролем
    if head.startswith(b"{\\rtf"): return "rtf"
    if head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff": return "image"
    if head.startswith((b"\xff\xfe", b"\xfe\xff")): return "text"
    if re.match(rb"\s*(<!doctype html|<html)", head[:512], re.I): return "html"
    if b"\x00" in head[:4096]:
        best = charset_normalizer.from_bytes(head).best()
        return "text" if best and best.encoding.startswith(("utf_16", "utf_32")) else "binary"
    return "text"
```
Альтернатива — `magika` (уже есть в зависимостях markitdown, работает на Windows): `Magika().identify_path(p).output.label` → `pdf/docx/doc/odt/rtf/html/markdown/txt/png/empty/unknown`. На наших фикстурах ошибок не было, но для UTF-16 с BOM выдала `unknown` (score 0,15). Плюс это ML-модель, поэтому основным путём ставим детерминированный sniff, а magika — подсказкой. `python-magic` на Windows требует отдельной DLL libmagic (`python-magic-bin` давно не обновлялся) — не брать.

**PDF: шифрование, ремонт, пустые страницы, качество текста:**
```python
doc = pymupdf.open(path, filetype="pdf")          # FileDataError -> IN-040
if doc.needs_pass and not (pw and doc.authenticate(pw)): diag("IN-020")
if doc.is_repaired: diag("IN-041")
if doc.page_count == 0: diag("IN-042")

def text_quality(t: str) -> float:               # 1.0 = нормальный текст
    t = re.sub(r"\s", "", t)
    if not t: return 0.0
    bad = sum(1 for c in t if c == "�" or unicodedata.category(c) in ("Cc", "Co"))
    good = sum(1 for c in t if c.isalnum() or c in ".,;:!?()«»\"'-–—/%№₸$€")
    return max(0.0, (good - 5 * bad) / len(t))
ocr_pages = [p.number for p in doc if text_quality(p.get_text()) < 0.5]   # скан ИЛИ мусор
```

**Markdown по страницам (якоря) и OCR только на нужных страницах:**
```python
import pymupdf4llm  # в 2.x это пакет-заглушка, код внутри pymupdf; import работает
chunks = pymupdf4llm.to_markdown(doc, page_chunks=True,
                                 ocr_language="rus+eng", ocr_dpi=300)  # НЕ по умолчанию "eng"!
for ch in chunks: page_no = ch["metadata"]["page_number"]; md = ch["text"]  # page_number с 1, есть и page_count
# Без OCR (Tesseract нет у жюри): use_ocr=False; на mixed_text_scan.pdf дал текст на стр. 1 и пустые стр. 2–8 за 2,3 с
# Пустые страницы (ocr_pages) отдать в Gemini:
sub = pymupdf.open(); sub.insert_pdf(doc, from_page=i, to_page=i)  # или doc.select(ocr_pages)
pdf_bytes = sub.tobytes()
```
Проверено: в чанке ключи `metadata`, `toc_items`, `page_boxes`, `text`; в `metadata` есть `page_number` (с 1) и `page_count`; `use_ocr=False` отключает OCR.

**Gemini для скана (основной OCR):**
```python
from google import genai
from google.genai import types
client = genai.Client()  # GEMINI_API_KEY из окружения
resp = client.models.generate_content(
    model=MODEL,
    contents=[types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
              "Перепиши текст каждой страницы дословно, без исправлений. "
              "Перед каждой страницей строка '=== стр. N ==='. Таблицы — в Markdown."])
```
**[не проверено]** на нашем скане, потому что не было ключа. Документация: PDF до 1000 стр. и 50 МБ, 258 токенов на страницу. Внимание: на странице документации пример уже с новым `interactions` API; конкретную модель и API сверить с исследованием про LLM.

**Спасение обрезанного PDF (последний шанс перед ошибкой):**
```python
b = open(path, "rb").read()
objs = {int(m.group(1)): m.start() for m in re.finditer(rb"(\d+) 0 obj", b)}
pages = [n for n, o in objs.items()
         if re.match(rb"\d+ 0 obj\s*<<[^>]*?/Type\s*/Page[^s]", b[o:o+400], re.S)]
mx = max(objs) + 1; kids = " ".join(f"{n} 0 R" for n in sorted(pages))
tail = (f"\nendobj\n{mx} 0 obj << /Type /Pages /Kids [{kids}] /Count {len(pages)} >> endobj\n"
        f"{mx+1} 0 obj << /Type /Catalog /Pages {mx} 0 R >> endobj\n"
        f"trailer << /Root {mx+1} 0 R /Size {mx+2} >>\n%%EOF\n").encode()
open(tmp, "wb").write(b + tail)
with pikepdf.open(tmp) as pdf:                     # qpdf сам перестроит xref
    for p in pdf.pages: p.obj.Parent = pdf.Root.Pages
    pdf.save(fixed)                                # дальше обычный путь, плюс IN-043
```
Работает, когда страницы и шрифты записаны раньше точки обрыва (так пишет LibreOffice). Проверено на одном файле; на PDF из Word или Chrome **[не проверено]**. Порядок страниц — по номеру объекта, это может не совпасть с оригиналом, поэтому в отчёт идёт оговорка.

**Защита от zip-бомбы (до любого парсера DOCX/ODT):**
```python
with zipfile.ZipFile(path) as z:
    total = sum(i.file_size for i in z.infolist()); packed = sum(i.compress_size for i in z.infolist()) or 1
    if total > 200 * 2**20 or total / packed > 100: diag("IN-030")
    if "word/document.xml" not in z.namelist(): diag("IN-031")
```
Учесть: `file_size` берётся из заголовка ZIP, его можно подделать. Поэтому дополнительно таймаут на разбор (`concurrent.futures` с `timeout=60`) — **[не проверено]** на подделанных заголовках. Для XML внутри — `defusedxml`; markitdown уже его использует.

**.doc и пароль на DOCX:**
```python
import olefile
streams = {"/".join(s) for s in olefile.OleFileIO(path).listdir()}
if "EncryptedPackage" in streams: diag("IN-021")            # DOCX/XLSX с паролем
elif "WordDocument" in streams:                              # старый .doc
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice: subprocess.run([soffice, "--headless", "--convert-to", "docx", "--outdir", tmpdir, path], timeout=120)
    else: diag("IN-033")
```

**Кодировки:**
```python
best = charset_normalizer.from_path(path).best()     # cp1251, koi8_r, utf_16, utf_16_le определены верно
text = str(best); enc = best.encoding                # best.language НЕ использовать (давал Finnish/Bulgarian)
```

**Язык (вместо charset_normalizer.language):**
```python
KK = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")
letters = [c for c in text if c.isalpha()]
cyr = sum("Ѐ" <= c <= "ӿ" for c in letters) / max(1, len(letters))
kk_share = sum(c in KK for c in letters) / max(1, len(letters))
```

---

## 4. Раздел «Диагностика входа» в отчёте и коды возврата

### Формат раздела (в отчёте сразу после паспорта прогона)

```markdown
## Диагностика входа

| Параметр | Значение |
|---|---|
| Файл | requirements.pdf (205 814 байт, SHA-256: 3f1a…c9) |
| Тип по содержимому / по расширению | PDF 1.7 / .pdf ✔ |
| Способ извлечения | PyMuPDF 2.1 → Markdown (стр. 1, 3–8); OCR Gemini (стр. 2) |
| Страниц / обработано | 8 / 8 |
| Символов извлечено | 8 660 |
| Качество текста | норм. на 7 стр., OCR на 1 стр. |
| Языки | ru 94 %, en 6 % |
| Статус входа | ЧАСТИЧНО: проверка выполнена, есть предупреждения |

| Код | Уровень | Что обнаружено | Где | Что сделал агент | Что сделать вам |
|---|---|---|---|---|---|
| IN-051 | ПРЕДУПРЕЖДЕНИЕ | На стр. 2 нет текстового слоя (изображение) | стр. 2 | Распознал через Gemini; цитаты со стр. 2 помечены «(OCR)» | Сверьте цитаты со стр. 2 с оригиналом или пришлите PDF с текстом |
| IN-041 | ПРЕДУПРЕЖДЕНИЕ | Таблица ссылок PDF была повреждена и восстановлена | весь файл | Прочитал после автоматического ремонта | Пересохраните исходник |
```

Правила:
- Уровни: **ОШИБКА** (проверка невозможна), **ПРЕДУПРЕЖДЕНИЕ** (проверка сделана, но с потерями или допущениями), **ИНФО** (ничего не потеряно, просто факт).
- Каждая строка содержит три вещи: что увидели, что сделали, что сделать человеку. Никаких трейсбеков в отчёте; трейсбек пишется в `report.log` рядом.
- Если вход не читается (только ОШИБКИ), отчёт всё равно содержит паспорт, «Диагностику входа» и «Итог: проверка не выполнена, причина…», а разделов про требования нет.
- Каждая цитата из OCR, из восстановленного PDF или из таблицы PDF получает пометку `(OCR)` / `(восстановлено)` / `(таблица, порядок слов мог измениться)`. Верификатор цитат сравнивает нечётко именно для этих пометок.
- Дублируем в `report.json` (поле `input_diagnostics: [{code, level, message, location, action, hint}]`), чтобы `eval.py` проверял диагностику автоматически.
- Коды стабильные (`IN-xxx`, `LLM-xxx`, `INT-xxx`) и перечислены в README: QA-лиду удобно сверять.

### Политика кодов возврата (предложение)

| Код | Значение | Отчёт пишется? |
|---|---|---|
| 0 | Вход прочитан полностью (возможны только ИНФО); проверка выполнена. **Найденные дефекты требований не меняют код** | да |
| 1 | Внутренняя ошибка агента (непойманное исключение) | да, что успели + INT-001 |
| 2 | Ошибка использования: неверные аргументы CLI, файл не найден, путь отчёта недоступен (стандарт typer/argparse) | да, если путь отчёта доступен; иначе только stderr |
| 3 | Вход прочитан **частично** или с допущениями (OCR, ремонт, обрезка по страницам, «не похоже на требования»); проверка выполнена | да, полный + предупреждения |
| 4 | Вход **невозможно** обработать (пустой, зашифрован, битый, неподдерживаемый, zip-бомба) | да, паспорт + диагностика + итог «не выполнено» |
| 5 | Сбой LLM или конфигурации (нет ключа, исчерпан лимит, сеть) | да, что успели |

Опционально `--fail-on {never,warning,critical}` для CI: при `critical` вернуть 6, если найдены критичные дефекты требований. По умолчанию `never`, чтобы прогон жюри на нормальном документе давал 0. Коды и их смысл указать в README и в паспорте отчёта («Код завершения: 3 — вход прочитан частично»).

---

## 5. Тестовые битые файлы: что сгенерировать и как

Всё делает один скрипт **`experiments/04-parsing-lab/make_fixtures.py`** (проверен: из 3 исходников получилось 39 файлов за ~7 с; нужны `pymupdf`, `pikepdf`, опционально `msoffcrypto-tool`). Скопировать его в репо как `tests/fixtures/make_fixtures.py`. Бинарные фикстуры не коммитим, кроме маленьких; большие генерируем в `conftest.py`.

Исходники:
```bash
pandoc docs/requirements.md -o fx/req.docx
soffice --headless --convert-to pdf --outdir fx fx/req.docx    # PDF с кириллицей и таблицами
soffice --headless --convert-to doc --outdir fx fx/req.docx    # старый .doc
soffice --headless --convert-to rtf --outdir fx fx/req.docx
soffice --headless --convert-to odt --outdir fx fx/req.docx
pandoc docs/requirements.md -s -o fx/req.html --metadata title=req
cp docs/requirements.md fx/req.md
python make_fixtures.py fx
```

| Файл | Как получен | Ожидание (код / диагностика) |
|---|---|---|
| `zero.pdf`, `zero.docx`, `zero.md` | пустой файл | 4 / IN-002 |
| `whitespace.md` | только `\n \t` | 4 / IN-070 |
| `blank_page.pdf` | `pymupdf.open().new_page()` | 4 / IN-050 → IN-071 |
| `scan.pdf` | страницы `req.pdf` → PNG 200 dpi → новый PDF | 3 / IN-050 + OCR |
| `mixed_text_scan.pdf` | стр. 1 текстом + стр. 2–8 сканом | 3 / IN-051 |
| `no_tounicode.pdf` | pikepdf: удалить `/ToUnicode` у всех шрифтов | 3 / IN-052 + OCR |
| `enc_user.pdf` | `pikepdf.Encryption(user="secret", owner="owner", R=6)` | 4 / IN-020; с `--password secret` → 0 |
| `enc_owner_only.pdf` | `Encryption(user="", owner=…, allow=Permissions(extract=False))` | 0 / IN-022 |
| `bad_xref.pdf` | число после `startxref` заменено на `999999` | 3 / IN-041 |
| `zeroed_xref.pdf` | 200 байт таблицы xref затёрты нулями | 3 / IN-041 |
| `truncated_60.pdf` | первые 60 % байт | 3 / IN-043 (спасение) или 4 / IN-040 |
| `truncated_5.pdf`, `header_only.pdf` | 5 % байт; только `%PDF-1.7` | 4 / IN-042, IN-040 |
| `garbage.pdf` | 50 КБ случайных байт | 4 / IN-010 |
| `pdf_named.docx`, `docx_named.pdf`, `md_named.pdf` | переименование | 0 / IN-011 |
| `image_named.pdf`, `image.png` | PNG страницы | 4 / IN-010 или 3 / IN-053 (OCR) |
| `docx_truncated.docx` | половина байт DOCX | 4 / IN-032 |
| `docx_not_zip.docx` | текст «это не zip» | IN-011 + IN-080 (мало текста) |
| `docx_no_document_xml.docx` | ZIP без `word/document.xml` | 4 / IN-031 |
| `docx_bad_xml.docx` | `document.xml` обрезан пополам | 4 / IN-034 |
| `docx_bomb.docx` | `document.xml` на 1 ГБ пробелов, 1 МБ на диске | 4 / IN-030, < 1 с |
| `docx_password.docx` | `msoffcrypto.OfficeFile(f).encrypt("secret", out)` | 4 / IN-021 |
| `req.doc`, `req.rtf`, `req.odt`, `req.html` | LibreOffice / pandoc | .doc → 4 / IN-033 (или 0 с soffice); остальные 0 |
| `req_cp1251.md`, `req_koi8r.txt`, `req_utf16.md`, `req_utf16le_nobom.md`, `req_utf8_bom.md` | `md_text.encode(...)` | 0 / IN-061 |
| `novel.md` | художественный текст ×200 | 3 / IN-080 |
| `template_empty.md` | заголовки ТЗ + пустая таблица | 3 / IN-081 |
| `mixed_lang.md` | ru + kk + en требования | 0 / IN-090 |
| `big_2000p.pdf` | 2000 копий страниц (166 МБ) | 4 / IN-003 (по размеру) |

Ещё стоит добавить (в скрипте нет): скан с поворотом на 90° и с наклоном 3°; скан 100 dpi; PDF из Word «Сохранить как PDF» и из Chrome «Печать в PDF» (другие генераторы — другие баги); DOCX с отслеживаемыми исправлениями и комментариями (python-docx игнорирует текст внутри `w:ins` — **[не проверено]**); PDF с двухколоночной вёрсткой; файл без расширения; путь с кириллицей и пробелами (важно для Windows).

Тест в pytest: параметризовать по таблице выше и проверять три вещи — код возврата, что отчёт существует, что в `report.json` есть ожидаемый код диагностики. Отдельно проверять, что прогон не длится больше 60 с (ловит zip-бомбу и зависания).

---

## 6. Риски и что не удалось проверить

**Не проверено:**
- Gemini на скане, на зашифрованном и на битом PDF — не было API-ключа. Качество OCR, сохранение номеров страниц, точная модель и вид API (на странице документации пример уже с новым `interactions` API) — сверить в исследовании про LLM.
- docling, marker, MinerU и unstructured локально не ставил (torch, гигабайты). Оценки взяты из PyPI-зависимостей и обзоров.
- Windows и macOS: установку PyMuPDF 2.1, onnxruntime и magika на Windows не проверял. Колёса `win_amd64` есть на PyPI. Риск: onnxruntime на старом Windows или Python 3.10 (PyMuPDF 2.x требует ≥3.11). Tesseract на Windows и автопоиск PyMuPDF через `TESSDATA_PREFIX` тоже не проверял.
- Ремонт обрезанного PDF проверен на одном PDF из LibreOffice.
- Поведение python-docx и markitdown на отслеживаемых исправлениях, сносках и вложенных таблицах.
- Защита от zip-бомбы с поддельными `file_size` в заголовках ZIP.

**Риски:**
1. **PyMuPDF 2.x — свежий мажорный релиз** (2.0 → 2.1 в ноябре 2026): API `to_markdown` и ключи чанков могли поменяться. Пиннить точную версию; в CI тест на `req.pdf`.
2. **Вес установки ≈400 МБ** venv (PyMuPDF + onnxruntime + numpy + magika). Это ощутимо, но pip справится. Если жюри будет на Python 3.10, PyMuPDF 2.x не встанет. В README явно написать «Python 3.11–3.13», дать `uv`-команду и альтернативу через Docker.
3. **Таблицы в PDF искажают порядок слов**, значит, точное совпадение цитат будет давать ложные «галлюцинации». Нужна нечёткая сверка: токены, без пунктуации, порог ~0,85.
4. **OCR даёт гомоглифы:** «Часть В» с кириллической В вместо латинской B, слитные слова «Месячныйпорограсходов». Требования с ID вроде «Ф-1» могут распознаться как «Ф-l». Регэкспы ID делать устойчивыми (`[1lI|]`), цитаты из OCR — с пометкой.
5. **AGPL** — вопрос лида QA из банка. Подготовить ответ: интерфейс бэкендов и флаг `--pdf-backend pdfium`.
6. **Таймауты:** любой парсер на враждебном файле может зависнуть (как mammoth на бомбе). Парсинг запускать в отдельном процессе с таймаутом (`concurrent.futures.ProcessPoolExecutor` + `timeout`), а не в основном потоке.
7. Лицензии marker и MinerU (веса) и PolyForm у старого pymupdf-layout — не тянуть их случайно транзитивно.

---

## Источники

- ТЗ кейса: `/home/user/bcc-hub/ai-requirements-validator-task/README.md`; разбор: `case-analysis.md`
- PyPI JSON API (версии, лицензии, зависимости, размеры колёс на 25.11.2026): https://pypi.org/pypi/pymupdf/json, https://pypi.org/pypi/pymupdf-layout/1.27.2/json, https://pypi.org/pypi/pypdf/json, https://pypi.org/pypi/pdfplumber/json, https://pypi.org/pypi/pdfminer.six/json, https://pypi.org/pypi/pypdfium2/json, https://pypi.org/pypi/docling-slim/json, https://pypi.org/pypi/marker-pdf/json, https://pypi.org/pypi/mineru/json, https://pypi.org/pypi/unstructured/json, https://pypi.org/pypi/markitdown/json, https://pypi.org/pypi/magika/json, https://pypi.org/pypi/pikepdf/json, https://pypi.org/pypi/charset-normalizer/json
- PyMuPDF README (2.1, из колеса): лицензия AGPL, слияние с 4llm, onnxruntime, PyMuPDF Office — https://github.com/pymupdf/pymupdf
- pymupdf4llm (пакет-заглушка для 2.x): https://github.com/pymupdf/pymupdf4llm
- Artifex, «Introducing the New PyMuPDF4LLM: Now Including Layout» (16.03.2026): https://artifex.com/blog/introducing-the-new-pymupdf4llm-now-including-layout
- PyMuPDF OCR: https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html ; установка: https://pymupdf.readthedocs.io/en/latest/installation.html
- Gemini Document understanding: https://ai.google.dev/gemini-api/docs/document-processing
- marker-pdf (лицензии кода и весов, зависимости): https://pypi.org/project/marker-pdf/ ; https://github.com/datalab-to/marker/releases
- MinerU, лицензия и развёртывание (обзор): https://toolnavs.com/article/2457-is-mineru-worth-using-the-50k-star-pdf-to-markdown-tool-has-deployment-and-licen
- Сравнение docling / MinerU / marker: https://particula.tech/blog/docling-vs-mineru-vs-marker-pdf-parser
- python-magic и libmagic на Windows: https://pypi.org/project/python_magic ; https://pypi.python.org/pypi/python-magic-bin
- OCRmyPDF (продвинутые возможности): https://ocrmypdf.readthedocs.io/en/latest/advanced.html
- Собственные замеры: `experiments/04-parsing-lab/probe.out`, `probe*.py`, `guard.py`, `salvage.py`, `bomb.py`, `make_fixtures.py` в `experiments/04-parsing-lab/`
