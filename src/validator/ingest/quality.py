"""Проверки входа после разбора (шаг 1): можно ли доверять извлечённому тексту и похож ли он на требования.

Признаки берём из ``ParsedDocument.signals`` (их честно считает разборщик PDF) или считаем по тексту для
MD и DOCX. Здесь только выводы и диагностика (исследование 04, строки 5–8 и 27 матрицы отказов):

- ``IN-052`` мусорный текстовый слой (шрифты без таблицы Unicode): ОШИБКА, если мусор почти весь
  текст (цитировать нечего), иначе ПРЕДУПРЕЖДЕНИЕ;
- ``IN-051`` страницы без текста: разборщик сообщает их число, здесь добавляем номера страниц, чтобы
  отчёт честно сказал, какие страницы не проверены;
- ``IN-080`` документ не похож на спецификацию требований: нет формулировок-обязательств и ID
  требований. Проверка идёт дальше, но с оговоркой.

Эвристики общие для любого документа требований: без терминов конкретного документа (правило 3 CLAUDE.md).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from validator.errors import (
    IN_GARBLED_TEXT,
    IN_HEADINGS_ONLY,
    IN_NO_TEXT,
    IN_NOT_REQUIREMENTS,
    IN_PAGES_WITHOUT_TEXT,
)
from validator.ingest.injection import review as injection_review
from validator.models import BlockKind, DiagLevel, Diagnostic, ParsedDocument, TextSignals

GARBLED_FATAL_SHARE = 0.3
"""Доля служебных символов (управляющие, частная область Unicode, «�»), с которой текст нечитаем целиком.
У испорченного ``/ToUnicode`` она 0,8–0,9 (исследование 04: 5817 управляющих из 8715 символов)."""
GARBLED_WARN_SHARE = 0.05
"""Доля служебных символов, с которой часть текста считаем искажённой. В нормальном тексте их нет."""
MIN_OBLIGATIONS_PER_1000_WORDS = 1.0
"""Меньше формулировок-обязательств на 1000 слов и нет ни одного ID: документ не похож на требования
(исследование 04, строка 27). В спецификациях их десятки на 1000 слов. Планка низкая нарочно: ложная
тревога на настоящем ТЗ (код 3 вместо 0) хуже пропущенного романа."""
MIN_WORDS_FOR_GENRE = 150
"""Короче этого о жанре не судим: в коротком тексте ноль обязательств ничего не значит."""

_GARBAGE_CATEGORIES = frozenset({"Cc", "Co", "Cn", "Cs"})
_OBLIGATION = re.compile(
    r"(?i)\b(?:"
    r"долж(?:ен|на|но|ны)|обязан\w*|необходимо|требуется|нужно|следует|"
    r"не\s+допускается|запрещ\w*|не\s+(?:более|менее|позднее|ранее)|"
    r"обеспечива\w*|позволя\w*|поддержива\w*|может|могут|хочу|"
    r"тиіс|қажет|керек|міндетті|"
    r"shall|must|should|will|can|may|required?|requires|want"
    r")\b"
)
_STRUCTURED = frozenset({BlockKind.LIST_ITEM, BlockKind.TABLE_ROW})
_REQUIREMENT_ID = re.compile(r"\b[A-ZА-ЯЁ]{1,6}-\d{1,4}\b")
_WORD = re.compile(r"\w+")


def _measure(text: str) -> TextSignals:
    """Доли букв и служебных символов по тексту: для MD и DOCX, у которых разборщик их не считает."""
    total = letters = garbage = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if ch.isalpha():
            letters += 1
        elif ch == "�" or unicodedata.category(ch) in _GARBAGE_CATEGORIES:
            garbage += 1
    return TextSignals(
        letters_share=round(letters / total, 4) if total else None,
        garbage_share=round(garbage / total, 4) if total else None,
    )


def _percent(share: float) -> str:
    return f"{share * 100:.0f} %"


def _garbled(doc: ParsedDocument) -> list[Diagnostic]:
    signals = doc.signals if doc.signals is not None else _measure(doc.text)
    garbage, letters = signals.garbage_share, signals.letters_share
    if garbage is None or letters is None:
        return []
    if garbage >= GARBLED_FATAL_SHARE:
        return [
            Diagnostic(
                code=IN_GARBLED_TEXT,
                level=DiagLevel.ERROR,
                message=(
                    f"Текст документа нечитаем: {_percent(garbage)} символов служебные, "
                    f"букв {_percent(letters)} "
                    "(в PDF шрифты без таблицы Unicode или файл испорчен)"
                ),
                action="Проверка не выполнялась: по такому тексту нельзя цитировать требования",
                hint=(
                    "Пересохраните PDF из исходника (Word: «Сохранить как PDF») или пришлите DOCX; "
                    "распознавания (OCR) в этой версии агента нет"
                ),
                location=doc.name,
                step="parse",
            )
        ]
    # По одной доле букв не судим: таблицы чисел и примеры JSON честно дают мало букв.
    if garbage > GARBLED_WARN_SHARE:
        return [
            Diagnostic(
                code=IN_GARBLED_TEXT,
                level=DiagLevel.WARNING,
                message=(
                    f"Часть текста извлечена с искажениями: служебных символов {_percent(garbage)}, "
                    f"букв {_percent(letters)}"
                ),
                action="Проверка идёт по тексту как есть: цитаты с искажёнными символами могут не совпасть",
                hint="Сверьте цитаты отчёта с оригиналом или пришлите документ в DOCX",
                location=doc.name,
                step="parse",
            )
        ]
    return []


def _ranges(numbers: Iterable[int]) -> str:
    """[2, 3, 4, 7] → «2–4, 7»."""
    out: list[str] = []
    ordered = sorted(numbers)
    start = prev = ordered[0]
    for n in [*ordered[1:], None]:
        if n is not None and n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else f"{start}–{prev}")
        if n is not None:
            start = prev = n
    return ", ".join(out)


def _missing_pages(doc: ParsedDocument) -> list[int] | None:
    """Номера страниц без единого блока, если их число совпало с подсчётом разборщика (иначе None).

    Страница только с колонтитулом тоже остаётся без блоков, но разборщик не считает её пустой:
    при расхождении честнее не называть номера, чем назвать неверные.
    """
    signals = doc.signals
    if not doc.pages or signals is None or not signals.pages_without_text:
        return None
    with_text = {b.anchor.page for b in doc.blocks if b.anchor.page is not None}
    missing = [n for n in range(1, doc.pages + 1) if n not in with_text]
    return missing if len(missing) == signals.pages_without_text else None


def _name_pages(doc: ParsedDocument, diagnostics: list[Diagnostic]) -> list[Diagnostic]:
    """Дописывает номера страниц в ``IN-051`` разборщика: какие именно страницы не проверены."""
    missing = _missing_pages(doc)
    if missing is None:
        return diagnostics
    where = f"стр. {_ranges(missing)}"
    return [
        d.model_copy(
            update={
                "message": f"Нет текстового слоя на {where} ({len(missing)} из {doc.pages} стр.)",
                "action": f"Проверка идёт только по страницам с текстом, не проверено: {where}",
                "location": where,
            }
        )
        if d.code == IN_PAGES_WITHOUT_TEXT
        else d
        for d in diagnostics
    ]


def _not_requirements(doc: ParsedDocument, known: set[str]) -> list[Diagnostic]:
    if known & {IN_NO_TEXT, IN_HEADINGS_ONLY}:
        return []  # пустой документ и пустой шаблон уже названы точнее
    if any(b.kind in _STRUCTURED for b in doc.blocks):
        return []  # списки и таблицы: так пишут требования, даже без модальных слов и ID
    words = len(_WORD.findall(doc.text))
    obligations = len(_OBLIGATION.findall(doc.text))
    ids = len(_REQUIREMENT_ID.findall(doc.text))
    if words < MIN_WORDS_FOR_GENRE or ids or obligations * 1000 / words >= MIN_OBLIGATIONS_PER_1000_WORDS:
        return []
    return [
        Diagnostic(
            code=IN_NOT_REQUIREMENTS,
            level=DiagLevel.WARNING,
            message=(
                f"Документ не похож на спецификацию требований: на {words} слов {obligations} "
                "формулировок-обязательств («должен», «необходимо», shall, must) и ни одного ID требования"
            ),
            action="Проверка выполнена, но её выводы могут быть неприменимы к такому документу",
            hint="Убедитесь, что подан нужный документ: техническое задание, спецификация или user stories",
            location=doc.name,
            step="parse",
        )
    ]


def review(doc: ParsedDocument) -> ParsedDocument:
    """Документ с уточнённой и дополненной диагностикой входа. ОШИБКА в ней значит «проверку не выполнять»."""
    diagnostics = _name_pages(doc, list(doc.diagnostics)) + _garbled(doc)
    if not any(d.level is DiagLevel.ERROR for d in diagnostics):
        diagnostics += _not_requirements(doc, {d.code for d in diagnostics})
    diagnostics += injection_review(doc)
    return doc.model_copy(update={"diagnostics": diagnostics})
