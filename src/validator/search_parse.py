"""Разбор ответа прохода поиска кодом (шаг 4): формат ``prompts/search_system.md`` → черновики находок.

Ответ прохода размечен строго (#14): раздел «## Находки», подразделы «### Отдельные требования» и
«### Набор требований», каждая находка начинается строкой «#### Н-k. <суть>», дальше строки
«- Поле: значение» (Блоки, Цитата (одна или две), Свойство, Тип дефекта, Вердикт, Сценарий,
Почему мешает, Тест, Важность, Как исправить; с доменными чек-листами ещё «Чек-лист», #35).
Значения полей разбирает ``search_values.py``.

Разбор детерминированный и бесплатный. Находка, в которой код не нашёл обязательных полей
(цитата, свойство, тип дефекта), не теряется: ``search.py`` отдаёт её текст на перенос в JSON
моделью (запасной путь, issue #18). ID блоков и цитаты здесь не сверяются с документом: ID проверяет
``search.py``, цитаты верификатор (шаг 7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from validator.models import DefectType, Property, Scope, Severity, Verdict
from validator.search_values import (
    block_ids_in,
    clean,
    norm,
    parse_defect_type,
    parse_property,
    parse_severity,
    parse_test,
    parse_verdict,
    quotes_in,
    unwrap,
)

# ---------------------------------------------------------------- разметка ответа

FINDINGS_SECTION = re.compile(r"^\s{0,3}#{1,3}\s*\**\s*Находки", re.MULTILINE)
"""Начало раздела находок: всё выше (обзор документа) находками не считается."""

FINDING_HEAD = re.compile(
    r"^[ \t]{0,3}(?P<hashes>#{1,6}[ \t]*)?(?P<bold>\*+)?[ \t]*[НH][-‑–][ \t]?(?P<number>\d+)\b\**"
    r"(?P<punct>[.:)])?(?P<rest>.*)$"
)
"""Заголовок находки: «#### Н-12. Суть», «**Н-12.** Суть», «Н-12. Суть»; латинская H и разные дефисы тоже."""
NUMBERED_HEAD = re.compile(
    r"^[ \t]{0,3}(?P<hashes>#{1,6}[ \t]*)?(?P<bold>\*+)?[ \t]*(?P<number>\d{1,3})"
    r"(?P<punct>[.)])\**[ \t]+(?P<rest>\S.*)$"
)
"""Заголовок находки без «Н-»: «1) Суть», «2. Суть», «### 3. Суть»; «99.9%» не номер (нужен пробел)."""
_DASH_TITLE = re.compile(r"^\s+[—–-]\s")
"""«Н-12 — Суть»: без заголовка и звёздочек номер отделён от сути тире."""

HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]*(.*)$")
SECTION_LEVEL = 3
"""Подразделы ответа («### Набор требований») не глубже третьего уровня; «####» это всегда находка."""
RULE = re.compile(r"^[ \t]{0,3}(?:[-*_][ \t]*){3,}$")
"""Горизонтальная черта «---» после находки: дальше уже не находка."""

FIELD = re.compile(
    r"^\s*(?:[-*•][ \t]+|\d{1,2}[.)][ \t]+)?\**\s*([А-Яа-яЁё][^:\n]{0,40}?)\s*\**\s*:\s*\**\s*(.*)$"
)
"""Строка поля «- Поле: значение», в том числе «- **Поле:** значение» и без маркера списка."""

QUOTED = re.compile(r"«[^»\n]+»|“[^”\n]+”|\"[^\"\n]+\"")
"""Цитата в тексте фрагмента: фрагмент без полей, но с цитатой (таблица, абзац) может быть находкой."""

_LIST_NUMBER = re.compile(r"^(?:[-*•]\s+)?(?:\d{1,3}[.)](?!\d)\s*)?")
"""Нумерация списка «3) » или «- 5. » перед сутью; «99.9%» и «3.5 секунды» нумерацией не считаются."""
_LEAD = re.compile(r"^(?:[.:)]|[—–-](?=\s))\s*")
"""Знак между номером и сутью: «. », «: », « — »; минус числа («-5 °C») остаётся."""


class Key:
    """Канонические имена полей находки."""

    BLOCKS = "blocks"
    QUOTE = "quote"
    PROPERTY = "property"
    DEFECT_TYPE = "defect_type"
    VERDICT = "verdict"
    SCENARIO = "scenario"
    WHY = "why"
    TEST = "test"
    SEVERITY = "severity"
    FIX = "fix"
    CHECKLIST = "checklist"


_FIELD_NAMES = {
    "блоки": Key.BLOCKS,
    "блок": Key.BLOCKS,
    "цитата": Key.QUOTE,
    "цитаты": Key.QUOTE,
    "свойство": Key.PROPERTY,
    "свойства": Key.PROPERTY,
    "тип дефекта": Key.DEFECT_TYPE,
    "тип": Key.DEFECT_TYPE,
    "вердикт": Key.VERDICT,
    "сценарий": Key.SCENARIO,
    "почему мешает": Key.WHY,
    "тест": Key.TEST,
    "важность": Key.SEVERITY,
    "как исправить": Key.FIX,
    "исправление": Key.FIX,
    "чек-лист": Key.CHECKLIST,
    "чек лист": Key.CHECKLIST,
    "чеклист": Key.CHECKLIST,
    "пункт чек-листа": Key.CHECKLIST,
}
_FIELD_NAMES.update({"место": Key.BLOCKS, "места": Key.BLOCKS})

SINGLE_FIELDS = (Key.PROPERTY, Key.DEFECT_TYPE, Key.VERDICT, Key.TEST, Key.SEVERITY, Key.FIX)
"""Поля, которые у находки бывают только один раз (цитат и строк «Блоки» может быть несколько)."""


def _field_key(label: str) -> str | None:
    """Имя поля по подписи: «Цитата 1», «Цитата (B012)» и «**Тип дефекта**» тоже узнаются."""
    plain = re.sub(r"\([^)]*\)|\d+", " ", norm(label))
    return _FIELD_NAMES.get(" ".join(plain.split()))


def _section_scope(level: int, title: str) -> Scope | None:
    """Подраздел ответа по заголовку второго или третьего уровня: «Набор …», «… по набору», «Отдельные …»."""
    if level > SECTION_LEVEL:
        return None
    low = norm(title)
    if "набор" in low:
        return Scope.SET
    if "отдельн" in low or "кажд" in low:
        return Scope.REQUIREMENT
    return None


def _title(text: str, *, numbered: bool) -> str:
    """Суть из строки заголовка: без звёздочек, знака после «Н-k» и (без «Н-k») нумерации списка."""
    title = text.strip().strip("*").strip()
    if not numbered:
        title = _LIST_NUMBER.sub("", title, count=1).strip().strip("*").strip()
    return _LEAD.sub("", title, count=1).strip()


def _is_field_line(line: str) -> bool:
    """Строка «- Поле: значение» с известным именем поля (в том числе «1. Цитата: …» с нумерацией полей)."""
    m = FIELD.match(line)
    return bool(m and _field_key(m.group(1)))


def _head(line: str, *, in_field: bool, numbered: bool = False) -> tuple[int, str] | None:
    """Номер и суть, если строка открывает находку «Н-k».

    Строка без «#» и звёздочек («Н-3. Суть») открывает находку, только если за номером знак «.», «:»,
    «)» или тире и строка не продолжает поле: «Н-3 и Н-7 описывают одно место» это текст, а не находка.
    ``numbered``: ответ без «Н-k», находки нумеруются «1) Суть» или «2. Суть» (см. ``_uses_numbers``).
    """
    m = FINDING_HEAD.match(line)
    if m is None:
        return _numbered_head(line, in_field=in_field) if numbered else None
    marked = bool(m.group("hashes") or m.group("bold"))
    if not marked and (in_field or not (m.group("punct") or _DASH_TITLE.match(m.group("rest")))):
        return None
    return int(m.group("number")), _title(m.group("rest"), numbered=True)


def _numbered_head(line: str, *, in_field: bool) -> tuple[int, str] | None:
    """«1) Суть» или «### 2. Суть»; нумерованное поле («1. Цитата: …») и продолжение поля не заголовок."""
    m = NUMBERED_HEAD.match(line)
    if m is None or _is_field_line(line):
        return None
    if in_field and not (m.group("hashes") or m.group("bold")):
        return None
    return int(m.group("number")), _title(m.group("rest"), numbered=True)


def _uses_numbers(lines: list[str]) -> bool:
    """Находки нумеруются без «Н-»: «Н-k» нигде нет, зато есть заголовок «1) Суть» с полями ниже."""
    if any(_head(line, in_field=False) for line in lines):
        return False
    for i, line in enumerate(lines):
        if _numbered_head(line, in_field=False) is not None and any(
            _is_field_line(nxt) for nxt in lines[i + 1 : i + 8]
        ):
            return True
    return False


# ---------------------------------------------------------------- находки как текст


@dataclass
class RawFinding:
    """Одна находка из ответа прохода, как её написала модель, до проверки полей."""

    position: int
    """Порядковый номер находки в ответе, с 1."""
    number: int | None
    """Номер из заголовка «Н-k»; None у фрагмента без такого заголовка (формат нарушен)."""
    heading: str
    """Суть из заголовка, без «Н-k.»."""
    section: Scope | None
    """Подраздел ответа, в котором стоит находка; None, если подразделов нет."""
    lines: list[str] = field(default_factory=list)
    fields: dict[str, list[str]] = field(default_factory=dict)

    @property
    def text(self) -> str:
        """Текст находки целиком: его получает модель переноса, если код не разобрал поля."""
        return "\n".join(self.lines).strip()

    @property
    def merged(self) -> bool:
        """Одиночное поле («Свойство», «Тип дефекта», «Вердикт»…) встречается больше одного раза.

        Так выглядят несколько слипшихся находок (заголовок «Н-k» потерян у второй и следующих):
        код их не делит, такой фрагмент целиком уходит на перенос моделью.
        """
        return any(len(self.fields.get(key, ())) > 1 for key in SINGLE_FIELDS)

    def first(self, key: str) -> str:
        values = self.fields.get(key) or [""]
        return values[0]


@dataclass(frozen=True)
class SplitAnswer:
    findings: list[RawFinding]
    """Находки и фрагменты с полями или цитатами по порядку ответа."""
    has_section: bool
    """Есть раздел «Находки» или хотя бы одна «Н-k»: ответ в формате промпта."""


class _Splitter:
    """Построчный разбор: заголовки находок, подразделы, поля и продолжения многострочных полей.

    Строки вне находок с заголовком «Н-k» собираются во фрагменты. Фрагмент с полями находки (заголовок
    без «Н-», нумерованный список вместо заголовков) или с цитатой (таблица, абзац) тоже идёт в разбор,
    а не теряется; фрагмент без того и другого («Находок нет.») отбрасывается.
    """

    def __init__(self, *, numbered: bool = False) -> None:
        self.numbered = numbered
        self.findings: list[RawFinding] = []
        self.current: RawFinding | None = None
        self.section: Scope | None = None
        self.last_key: str | None = None

    def close(self) -> None:
        current = self.current
        if current is not None and (
            current.number is not None or current.fields or QUOTED.search(current.text)
        ):
            current.position = len(self.findings) + 1
            self.findings.append(current)
        self.current, self.last_key = None, None

    def open(self, number: int | None, heading: str, line: str | None = None) -> RawFinding:
        self.close()
        self.current = RawFinding(position=0, number=number, heading=heading, section=self.section)
        if line is not None:
            self.current.lines.append(line)
        return self.current

    def feed(self, line: str) -> None:
        head = _head(line, in_field=self.last_key is not None, numbered=self.numbered)
        if head is not None:
            self.open(*head, line)
            return
        heading = HEADING.match(line)
        if heading:
            self._heading(line, len(heading.group(1)), heading.group(2))
            return
        if RULE.match(line):
            self.close()
            return
        current = self.current
        if current is None:
            if not line.strip():
                return
            current = self.open(None, "")
        current.lines.append(line)
        self._field(current, line)

    def _heading(self, line: str, level: int, text: str) -> None:
        scope = _section_scope(level, text)
        if scope is not None or FINDINGS_SECTION.match(line):
            self.close()
            self.section = scope or self.section
        else:
            self.open(None, _title(text, numbered=False), line)

    def _field(self, current: RawFinding, line: str) -> None:
        if not line.strip():
            self.last_key = None
            return
        m = FIELD.match(line)
        key = _field_key(m.group(1)) if m else None
        if m and key:
            current.fields.setdefault(key, []).append(m.group(2).strip())
            self.last_key = key
        elif self.last_key:
            values = current.fields[self.last_key]
            values[-1] = f"{values[-1]} {line.strip()}".strip()
        elif current.number is None and not current.heading and not current.fields:
            current.heading = _title(line, numbered=False)


def split_findings(answer: str) -> SplitAnswer:
    """Находки из ответа прохода: от раздела «Находки» (или первой «Н-k») до конца ответа."""
    lines = answer.splitlines()
    start = next((i for i, line in enumerate(lines) if FINDINGS_SECTION.match(line)), None)
    has_section = start is not None
    numbered = _uses_numbers(lines[start or 0 :])
    if start is None:
        start = next(
            (i for i, line in enumerate(lines) if _head(line, in_field=False, numbered=numbered)), len(lines)
        )
    splitter = _Splitter(numbered=numbered)
    for line in lines[start:]:
        splitter.feed(line)
    splitter.close()
    found = splitter.findings
    return SplitAnswer(findings=found, has_section=has_section or any(f.number is not None for f in found))


# ---------------------------------------------------------------- черновик находки


@dataclass(frozen=True)
class Draft:
    """Находка, разобранная кодом или перенесённая моделью, до проверки ID блоков и номера в прогоне."""

    title: str | None
    block_ids: tuple[str, ...]
    quotes: tuple[str, ...]
    property: Property
    defect_type: DefectType
    verdict: Verdict
    scope: Scope
    explanation: str
    scenario: str | None = None
    blocks_testing: bool = False
    test_gap: str | None = None
    severity_hint: Severity | None = None
    suggestion: str | None = None
    checklist: str | None = None
    """Строка «Чек-лист:» как её написала модель (#35); ID пункта проверяет ``search.py``."""


NOT_A_FINDING = "не в формате находки"
"""Причина переноса фрагмента без строк «Поле: значение» (таблица, абзац с цитатой)."""


def draft_from_raw(raw: RawFinding) -> Draft | list[str]:
    """Черновик находки или список того, что мешает разбору кодом (тогда находку переносит модель).

    Обязательны цитата, свойство и тип дефекта: без них находку нельзя проверить и показать.
    Без вердикта, важности, теста и остального находка разбирается, пустые поля остаются пустыми.
    """
    if not raw.fields:
        return [NOT_A_FINDING]
    if raw.merged:
        return ["одна находка на фрагмент"]
    quotes = tuple(q for value in raw.fields.get(Key.QUOTE, []) for q in quotes_in(value))
    prop = parse_property(raw.first(Key.PROPERTY), raw.section)
    defect = parse_defect_type(raw.first(Key.DEFECT_TYPE))
    if not quotes or prop is None or defect is None:
        problems = [] if quotes else ["нет цитаты"]
        for key, value, label in ((Key.PROPERTY, prop, "свойство"), (Key.DEFECT_TYPE, defect, "тип дефекта")):
            if value is None:
                problems.append(f"{label}: {'не распознано' if raw.first(key) else 'нет строки'}")
        return problems
    blocks_testing, test_gap = parse_test(raw.first(Key.TEST))
    title = clean(raw.heading)
    return Draft(
        title=title,
        block_ids=tuple(i for value in raw.fields.get(Key.BLOCKS, []) for i in block_ids_in(value)),
        quotes=quotes,
        property=prop,
        defect_type=defect,
        verdict=parse_verdict(raw.first(Key.VERDICT), raw.heading),
        scope=raw.section or prop.scope,
        explanation=clean(raw.first(Key.WHY)) or title or "",
        scenario=clean(raw.first(Key.SCENARIO)),
        blocks_testing=blocks_testing,
        test_gap=test_gap,
        severity_hint=parse_severity(raw.first(Key.SEVERITY)),
        suggestion=clean(unwrap(raw.first(Key.FIX))),
        checklist=clean(raw.first(Key.CHECKLIST)),
    )


__all__ = ["NOT_A_FINDING", "Draft", "RawFinding", "SplitAnswer", "draft_from_raw", "split_findings"]
