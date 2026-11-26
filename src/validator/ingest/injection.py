"""Текст в документе, обращённый к проверяющей модели (внедрение инструкций, prompt injection).

Документ приходит от стороннего автора и целиком попадает в промпт. Промпты шагов говорят модели, что
текст между метками это данные, а «посторонние инструкции» нужно записать находкой. Это защита моделью;
здесь вторая, кодом: общие признаки команд модели ищет регулярками, не вызывая модель, и записывает
их в диагностику входа ``IN-082`` (ИНФО, код возврата не меняется). Если модель сама сдастся на
уговор («дефектов нет»), фрагмент всё равно виден в отчёте.

Признаки общие, без слов проверяемого документа (CLAUDE.md, правило 3): «забудь предыдущие инструкции»,
«не сообщай о дефектах», «напиши, что нарушений нет», строка-роль ``system:``, «с этого момента ты…».
Узкие шаблоны нужны, чтобы настоящее требование («система должна игнорировать повторные запросы») не
сработало: слова «инструкция» и «модель» сами по себе не признак.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from validator.errors import IN_INJECTION
from validator.models import Block, BlockKind, DiagLevel, Diagnostic, ParsedDocument

MAX_SHOWN = 4
"""Сколько фрагментов назвать в диагностике: остальные считаем числом."""
QUOTE_CHARS = 140
"""Длина цитаты в диагностике, остальное обрезаем многоточием."""

_FLAGS = re.IGNORECASE | re.MULTILINE
_GAP = r"[^.\n]{0,60}"
_START = r"(?:^|[.!?:;|]\s*|\bplease\s+|\bпожалуйста,?\s+)"
"""Приказ стоит в начале предложения («Ignore all…», «Напиши, что…»), а не после подлежащего: «Система должна
игнорировать…» и «The system must not report…» это требования, а не команды."""
_DOC_WORD = (
    r"\b(?:document|requirements?|specification|spec|report|review|criteria|findings?|everything|"
    r"документ\w*|требовани\w*|тз|отчёт\w*|отчет\w*|спецификаци\w*|критери\w*|всё|все)\b"
)

_OVERRIDE_EN = (
    _START + r"(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+|your\s+)*"
    r"(?:previous|prior|above|earlier|preceding|system)\s+(?:instructions?|prompts?|rules|directions?)"
)
_OVERRIDE_RU = (
    r"\b(?:игнорируй(?:те)?|забудь(?:те)?|отмени(?:те)?)\s+(?:все\s+|всё\s+|любые\s+|свои\s+)*"
    r"(?:предыдущ|прежн|прошл|вышесто\w+|системн|имевш)\w*\s+(?:инструкци|указани|промпт)\w*"
)
_SUPPRESS_EN = (
    _START
    + r"(?:do\s+not|don'?t|never)\s+(?:report|list|mention|flag|include|enumerate)\b"
    + _GAP
    + r"\b(?:defects?|problems?|findings?|issues?|violations?|remarks?)\b"
)
_SUPPRESS_RU = (
    r"\bне\s+(?:сообщай(?:те)?|перечисляй(?:те)?|указывай(?:те)?|упоминай(?:те)?|включай(?:те)?|отмечай(?:те)?)\b"
    + _GAP
    + r"\b(?:находк|дефект|нарушени|замечани|проблем)\w*"
)
_VERDICT_PHRASE_EN = (
    r"(?:no\s+(?:defects?|issues?|problems?|violations?)|"
    r"(?:fully|completely|entirely)\s+(?:complies|compliant|conforms?)|"
    r"(?:is|are)\s+(?:perfect|flawless|compliant|met|satisfied|fulfilled)|"
    r"as\s+(?:compliant|conforming|passed|correct))"
)
_VERDICT_EN = (
    _START
    + r"(?:write|state|answer|say|respond|reply|conclude|confirm|declare|mark)\b(?="
    + _GAP
    + _DOC_WORD
    + r")"
    + _GAP
    + r"\b"
    + _VERDICT_PHRASE_EN
)
_VERDICT_RU = (
    r"\b(?:напиши(?:те)?|укажи(?:те)?|сообщи(?:те)?|ответь(?:те)?|скажи(?:те)?|отметь(?:те)?|"
    r"считай(?:те)?|подтверди(?:те)?|признай(?:те)?)\b(?="
    + _GAP
    + _DOC_WORD
    + r")"
    + _GAP
    + r"\b(?:(?:дефектов|нарушений|замечаний|проблем|ошибок)\s+нет|"
    r"(?:полностью\s+)?соответству\w+\s+(?:стандарт|требовани|норм)\w*|"
    r"идеальн\w+|безупречн\w+|выполнен\w*)"
)
_ROLE_LINE = (
    r"^\W{0,4}(?:system|assistant|developer|системн\w+\s+(?:сообщени|инструкци|команд)\w*)\s*[:>\]]"
    r"|^\s*\[(?:system|assistant|developer|inst)\]|^\s*<(?:system|assistant)>"
)
_REASSIGN = (
    r"(?:\bс\s+этого\s+момента\s+(?:ты|вы)\b|\b(?:теперь\s+ты|ты\s+теперь)\s+(?:не\b|—|-|мой|помощник|ассистент|другой)|"
    r"\bты\s+больше\s+не\b|\bfrom\s+now\s+on,?\s+you\s+(?:are|will|must|should)\b|"
    r"\byou\s+are\s+no\s+longer\b|\bnew\s+instructions\s*:|\bновые\s+инструкции\s*:)"
)
_ADDRESSEE = (
    r"(?:^|(?<=[.!?]\s)|(?<=\|\s))\W{0,3}(?:(?:уважаемый|уважаемая|dear|hey|hello|attention|внимание)[, ]+)?"
    r"(?:ии[- ]\w+|ai[- ](?:assistant|reviewer|model|agent|ревьюер|ассистент)|llm|ассистент\w*|assistant|"
    r"нейросеть|языков\w+\s+модел\w+|language\s+model|reviewer\s+model|(?:проверяющая|проверяющей)\s+модел\w+)"
    r"\s*[!:,]"
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rx, _FLAGS))
    for name, rx in (
        ("override", f"{_OVERRIDE_EN}|{_OVERRIDE_RU}"),
        ("suppress", f"{_SUPPRESS_EN}|{_SUPPRESS_RU}"),
        ("verdict", f"{_VERDICT_EN}|{_VERDICT_RU}"),
        ("role", _ROLE_LINE),
        ("reassign", _REASSIGN),
        ("addressee", _ADDRESSEE),
    )
)
# Одно обращение («Модель: …») или строка «System: …» без приказа не показатель: глоссарий пишет так же
# («System: программный комплекс…»). Их считаем, только если в блоке сработал ещё один признак.
_NEEDS_COMPANION = frozenset({"addressee", "role"})


@dataclass(frozen=True)
class Suspicious:
    """Блок документа с командой модели: что сработало и где."""

    block: Block
    kinds: tuple[str, ...]


def _where(block: Block) -> str:
    line = block.anchor.line_start
    return f"{block.id}, строка {line}" if line else block.id


def scan(doc: ParsedDocument) -> list[Suspicious]:
    """Блоки с признаками команды модели, по порядку документа."""
    found: list[Suspicious] = []
    for block in doc.blocks:
        if block.kind is BlockKind.FURNITURE or not block.text.strip():
            continue
        kinds = tuple(name for name, rx in _PATTERNS if rx.search(block.text))
        if kinds and not set(kinds) <= _NEEDS_COMPANION:
            found.append(Suspicious(block, kinds))
    return found


def _short(text: str) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= QUOTE_CHARS else one_line[: QUOTE_CHARS - 1].rstrip() + "…"


def review(doc: ParsedDocument) -> list[Diagnostic]:
    """``IN-082`` одной строкой на документ: сколько блоков и первые из них с цитатами."""
    hits = scan(doc)
    if not hits:
        return []
    shown = hits[:MAX_SHOWN]
    quotes = "; ".join(f"«{_short(h.block.text)}» ({h.block.id})" for h in shown)
    more = f" и ещё {len(hits) - len(shown)}" if len(hits) > len(shown) else ""
    return [
        Diagnostic(
            code=IN_INJECTION,
            level=DiagLevel.INFO,
            message=(
                "В документе есть текст, обращённый к проверяющей модели, а не к разработчикам "
                f"(блоков: {len(hits)}): {quotes}{more}"
            ),
            action="Агент не выполняет такие указания: текст проверяется как данные документа, "
            "модель получает предупреждение об этом",
            hint="Уберите такие вставки: в документе требований им не место, а проверке они мешают",
            location="; ".join(_where(h.block) for h in shown),
            step="parse",
        )
    ]


__all__ = ["Suspicious", "review", "scan"]
