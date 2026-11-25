# 12. Парсеры и битые файлы на моей ОС (Windows 11)

Автор: helknow (с Claude Code), 25.11.2026. Issue #2, тема 12.
Статус: **часть (а) готова, часть (б) «OCR скана через Gemini» не сделана**: нет ключа Gemini (см. «Что не проверено»).

Скрипт: `experiments/12-parsers-on-my-os/run_os_check.py`. Сырые результаты (не в git): `experiments/out/12/results.{md,json}`.
Обзор и ожидания, с которыми сравниваю: [04-parsing-and-broken-files.md](04-parsing-and-broken-files.md) (разделы 1, 3, 5).

## TL;DR

- На Windows 11 + Python 3.12 весь стек из файла 04 **ставится через `uv` без проблем**: `uv sync --group experiments` за 9 с, плюс `pikepdf`, `pypdfium2`, `msoffcrypto-tool`, `olefile` через `--with` за 8 с. Компилятор не нужен, все колёса `win_amd64`. Размер `.venv` 319 МБ (в 04 оценка ≈400 МБ).
- `make_fixtures.py` на Windows отработал без правок: **37 файлов из 3 исходников за 6,1 с**, пик памяти 313 МБ.
- `guard.py` на всех фикстурах: **37 совпадений с таблицей из 04, 0 расхождений, 3 без ожидания** (N/A). Zip-бомба отклонена за 0,02 с при 25 МБ памяти.
- Путь с кириллицей и пробелом (`папка с пробелом/требования.pdf`) читают guard и все три PDF-парсера, полнота та же, что у обычного пути.
- Полнота извлечения (recall) **совпала с облаком до третьего знака**: PyMuPDF / pdfplumber / pypdfium2 0,977, `to_markdown` 0,969, python-docx 1,0.
- Скорость на тёплом процессе сопоставима с облаком. Исключение: `pymupdf4llm.to_markdown` **≈4,8 с на 8 страниц против 2,5 с в облаке**. Холодный старт (первый `import pymupdf`) стоит ≈0,7 с и ≈140 МБ памяти.

## Машина

| Параметр | Значение |
|---|---|
| ОС | Windows 11 Pro, `Windows-11-10.0.26300-SP0` |
| CPU / RAM | Intel Core i7-13650HX / 16 ГБ |
| Python | 3.12.10 (CPython, MSC, 64 bit) |
| uv | 0.13.6 |
| Версии | pymupdf 2.1, pymupdf4llm 2.1, pdfplumber 0.11.11, pypdfium2 5.15.2, pikepdf 10.18.2, python-docx 1.2.0, pypdf 7.1.1, markitdown 0.1.9, charset-normalizer 3.5.3, msoffcrypto-tool 6.1.0, olefile 0.47 |

Версии те же, что в таблице файла 04, поэтому разница «облако Linux против Windows» не объясняется версиями.

## Что работает на моей ОС

| Компонент | Ставится через uv | Работает | Замечание |
|---|---|---|---|
| PyMuPDF 2.1 (`get_text`) | да | да | тёплый вызов 0,008 с на 8 стр. (облако 0,014 с); холодный с `import` 0,7 с |
| PyMuPDF `to_markdown` (pymupdf4llm) | да | да | 4,8 с тёплый, 5,6 с холодный (облако 2,5 с); recall 0,969 |
| pdfplumber 0.11.11 | да | да | 0,17 с тёплый (облако 0,32 с) |
| pypdfium2 5.15.2 | да (`--with`; в pyproject приходит транзитивно с pdfplumber) | да | 0,006 с тёплый |
| pikepdf 10.18.2 | да (`--with`; в pyproject его нет) | да | нужен `make_fixtures.py` для шифрования и удаления ToUnicode |
| python-docx 1.2.0 | да | да | 0,013 с тёплый; на `docx_bad_xml.docx` падает `XMLSyntaxError`, как в 04 |
| charset-normalizer 3.5.3 | да | да | cp1251, koi8-r, UTF-16 с BOM и без BOM распознаны, recall 1,0 |
| msoffcrypto-tool, olefile | да (`--with`) | да | `docx_password.docx` создан и распознан как IN-021 |
| Пути с кириллицей и пробелами | — | да | из Python всё читается; консоль PowerShell показывает русские сообщения кракозябрами без `PYTHONUTF8=1`, но файлы и JSON в UTF-8 корректны |

## Guard на фикстурах: сравнение с таблицей 04

Ожидания взяты из раздела 5 файла 04. Сравниваю только коды, которые выдаёт `guard.check`; коды поздних этапов (IN-052, IN-080 и т.п.) guard и не должен выдавать. Каждый файл проверялся в отдельном процессе с таймаутом 60 с.

| Группа | Файлы | Получено | Совпало |
|---|---|---|---|
| Пустые | `zero.pdf/.docx/.md`, `whitespace.md` | IN-002, IN-070 | 4/4 |
| PDF без текста | `blank_page.pdf`, `scan.pdf`, `mixed_text_scan.pdf` | IN-050, IN-050, IN-051 | 3/3 |
| Шифрование | `enc_user.pdf`, `enc_owner_only.pdf`, `docx_password.docx` | IN-020, IN-022, IN-021 | 3/3 |
| Повреждённый PDF | `bad_xref.pdf`, `zeroed_xref.pdf`, `truncated_60.pdf`, `truncated_5.pdf`, `header_only.pdf` | IN-041, IN-041, IN-040, IN-041+IN-042, IN-040 | 5/5 |
| Не тот тип | `garbage.pdf`, `image.png`, `image_named.pdf` | IN-010 | 3/3 |
| Неверное расширение | `pdf_named.docx`, `docx_named.pdf`, `md_named.pdf`, `docx_not_zip.docx` | IN-011 | 4/4 |
| Битый DOCX | `docx_truncated.docx`, `docx_no_document_xml.docx`, `docx_bomb.docx` | IN-032, IN-031, IN-030 (0,02 с) | 3/3 |
| Кодировки | `req_cp1251.md`, `req_koi8r.txt`, `req_utf16.md`, `req_utf16le_nobom.md`, `req_utf8_bom.md` | IN-061 ×4, у UTF-8 BOM без диагностики | 5/5 |
| Нормальные и «не про требования» | `req.md/.docx/.pdf`, `novel.md`, `template_empty.md`, `mixed_lang.md`, кириллический путь | без ERROR | 7/7 |
| Без ожидания (N/A) | `no_tounicode.pdf`, `docx_bad_xml.docx`, `big_2000p.pdf` | OK, OK, IN-003 | — |

Пояснения к N/A:
- `no_tounicode.pdf`: guard пропускает, три парсера извлекают 8–49 тыс. символов с recall 0,000. Совпадает с 04: нужна проверка качества текста (IN-052), её в guard пока нет.
- `docx_bad_xml.docx`: guard пропускает (ZIP цел), python-docx падает `XMLSyntaxError`. Это ожидаемый IN-034 на этапе парсера.
- `big_2000p.pdf` (166 МБ): guard мгновенно отклоняет по размеру (IN-003). Генератор создаёт его, хотя в README лаборатории он не упомянут.

Файлы `req.doc`, `req.rtf`, `req.odt`, `req.html` не проверены: генератор их не делает, а LibreOffice и pandoc для их создания я не ставил.

## Память и время

| Этап | Время | Пик памяти |
|---|---|---|
| `make_fixtures.py` (37 файлов, вкл. PDF на 166 МБ) | 6,1 с | 313 МБ |
| guard на PDF (с первым `import pymupdf`) | ≈0,7 с | ≈163 МБ |
| guard на DOCX/ZIP/пустых | 0,01–0,04 с | ≈25 МБ |
| guard на тексте (charset-normalizer) | 0,03–0,14 с | ≈30 МБ |
| guard на `docx_password.docx` (olefile) | 0,43 с | 30 МБ |
| Весь прогон (генерация + guard + парсеры, 41 файл, каждый в своём процессе) | ≈55 с | — |

Почти вся «стоимость» PDF-пути уходит на импорт PyMuPDF (≈0,7 с и ≈140 МБ сверх базового процесса). Для CLI-агента, который запускается на один документ, это незаметно.

## Рекомендации для агента

1. Стек из 04 можно брать как есть: на Windows он ставится через `uv` без системных зависимостей. В `pyproject.toml` агента явно добавить `pikepdf`, `pypdfium2`, `olefile`: сейчас их нет в группе `experiments`, и без `--with` `make_fixtures.py` не запускается.
2. `guard.py` переносить в агента почти без изменений: на Windows он ведёт себя так же, как в облаке. Не хватает только проверки качества текста (IN-052) и перехвата исключения парсера DOCX (IN-034).
3. `to_markdown` на Windows вдвое медленнее, чем в облаке (≈5 с на 8 страниц), для документа на 50–100 страниц это до минуты. Держать `get_text()` (0,01 с) как быстрый путь, а `to_markdown` вызывать, только если нужна структура заголовков.
4. При печати в консоль на Windows ставить `PYTHONUTF8=1` или `sys.stdout.reconfigure(encoding="utf-8")`, иначе русские диагностики в PowerShell превращаются в кракозябры. Файлы отчёта всегда писать с `encoding="utf-8"`.

## Что не проверено

- **Часть (б): OCR скана через Gemini не делал.** На момент работы ключа Gemini в `.env` не было. Скан без текстового слоя уже генерируется (`experiments/out/fx/scan.pdf`, 8 стр., 0 символов у всех трёх парсеров). Осталось отправить его в `gemini-3.5-flash-lite` и сравнить с `requirements.md` через rapidfuzz.
- macOS не проверял (у меня Windows).
- `req.doc`, `req.rtf`, `req.odt`, `req.html` (нужны LibreOffice и pandoc).
- Tesseract на Windows не ставил.
- `markitdown` на фикстурах не прогонял: в задаче 12(а) его нет.
- Время замерено двумя прогонами без статистики разброса; между прогонами цифры отличались не больше чем на ~10 %.

## Источники

- Собственные замеры, 25.11.2026: `experiments/12-parsers-on-my-os/run_os_check.py`; выход `experiments/out/12/results.md` и `results.json` (генерируются заново командой из README папки).
- Ожидания и облачные цифры: [04-parsing-and-broken-files.md](04-parsing-and-broken-files.md), `experiments/04-parsing-lab/make_fixtures.py`, `experiments/04-parsing-lab/guard.py`.
