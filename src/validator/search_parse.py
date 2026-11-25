"""Разбор ответа прохода поиска кодом (шаг 4): формат ``prompts/search_system.md`` → черновики находок.

Ответ прохода размечен строго (#14): раздел «## Находки», подразделы «### Отдельные требования» и
«### Набор требований», каждая находка начинается строкой «#### Н-k. <суть>», дальше строки
«- Поле: значение» (Блоки, Цитата (одна или две), Свойство, Тип дефекта, Вердикт, Сценарий,
Почему мешает, Тест, Важность, Как исправить).

Разбор детерминированный и бесплатный. Находка, в которой код не нашёл обязательных полей
(цитата, свойство, тип дефекта), не теряется: ``search.py`` отдаёт её текст на перенос в JSON
моделью (запасной путь, issue #18). ID блоков и цитаты здесь не сверяются с документом: ID проверяет
``search.py``, цитаты верификатор (шаг 7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from validator.models import DefectType, Property, Scope, Severity, Verdict

# ---------------------------------------------------------------- разметка ответа

FINDINGS_SECTION = re.compile(r"^\s{0,3}#{1,3}\s*\**\s*Находки", re.MULTILINE)
"""Начало раздела находок: всё выше (обзор документа) находками не считается."""

FINDING_HEAD = re.compile(r"^[ \t]{0,3}(?:#{1,6}[ \t]*)?\**[ \t]*[НH][-‑–][ \t]?(\d+)\b\**[.:)]?(.*)$")
"""Заголовок находки: «#### Н-12. Суть», «**Н-12.** Суть», латинская H и разные дефисы тоже."""

HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]*(.*)$")
RULE = re.compile(r"^[ \t]{0,3}(?:[-*_][ \t]*){3,}$")
"""Горизонтальная черта «---» после находки: дальше уже не находка."""

FIELD = re.compile(
    r"^\s*(?:[-*•][ \t]+|\d{1,2}[.)][ \t]+)?\**\s*([А-Яа-яЁё][^:\n]{0,40}?)\s*\**\s*:\s*\**\s*(.*)$"
)
"""Строка поля «- Поле: значение», в том числе «- **Поле:** значение» и без маркера списка."""

BLOCK_ID = re.compile(r"(?<![\w.])[BВ](\d{3,}(?:\.\d+)?)(?![\w])")
"""ID блока или предложения: B012, B012.3. Кириллическая «В» вместо латинской B тоже принимается."""


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
}

SINGLE_FIELDS = (Key.PROPERTY, Key.DEFECT_TYPE, Key.VERDICT, Key.TEST, Key.SEVERITY, Key.FIX)
"""Поля, которые у находки бывают только один раз (цитат и строк «Блоки» может быть несколько)."""


def _norm(text: str) -> str:
    """Нижний регистр, «ё» как «е», без звёздочек разметки и лишних пробелов."""
    return " ".join(text.lower().replace("ё", "е").replace("*", " ").split())


def _field_key(label: str) -> str | None:
    """Имя поля по подписи: «Цитата 1», «Цитата (B012)» и «**Тип дефекта**» тоже узнаются."""
    clean = re.sub(r"\([^)]*\)|\d+", " ", _norm(label))
    return _FIELD_NAMES.get(" ".join(clean.split()))


def _section_scope(title: str) -> Scope | None:
    low = _norm(title)
    if low.startswith("отдельн"):
        return Scope.REQUIREMENT
    if low.startswith("набор"):
        return Scope.SET
    return None


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
    """Находки и фрагменты с полями находок по порядку ответа."""
    has_section: bool
    """Есть раздел «Находки» или хотя бы одна «Н-k»: ответ в формате промпта."""


_NUMBERING = re.compile(r"^\s*(?:[-*•]\s*)?(?:\d{1,3}[.)]\s*)?")


def _title(text: str) -> str:
    """Суть из строки заголовка: без звёздочек, нумерации «5.» и знаков в начале."""
    clean = text.strip().strip("*").strip()
    clean = _NUMBERING.sub("", clean, count=1).strip().strip("*").strip()
    return clean.lstrip(".:)—–- ").strip()


class _Splitter:
    """Построчный разбор: заголовки находок, подразделы, поля и продолжения многострочных полей.

    Строки вне находок с заголовком «Н-k» собираются во фрагменты: фрагмент с полями находки
    (заголовок без «Н-», нумерованный список вместо заголовков) тоже идёт в разбор, а не теряется.
    """

    def __init__(self) -> None:
        self.findings: list[RawFinding] = []
        self.current: RawFinding | None = None
        self.section: Scope | None = None
        self.last_key: str | None = None

    def close(self) -> None:
        if self.current is not None and (self.current.number is not None or self.current.fields):
            self.current.position = len(self.findings) + 1
            self.findings.append(self.current)
        self.current, self.last_key = None, None

    def open(self, number: int | None, heading: str, line: str | None = None) -> RawFinding:
        self.close()
        self.current = RawFinding(position=0, number=number, heading=heading, section=self.section)
        if line is not None:
            self.current.lines.append(line)
        return self.current

    def feed(self, line: str) -> None:
        head = FINDING_HEAD.match(line)
        if head:
            self.open(int(head.group(1)), _title(head.group(2)), line)
            return
        heading = HEADING.match(line)
        if heading:
            scope = _section_scope(heading.group(1))
            if scope is not None or FINDINGS_SECTION.match(line):
                self.close()
                self.section = scope or self.section
            else:
                self.open(None, _title(heading.group(1)), line)
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
            current.heading = _title(line)


def split_findings(answer: str) -> SplitAnswer:
    """Находки из ответа прохода: от раздела «Находки» (или первой «Н-k») до конца ответа."""
    lines = answer.splitlines()
    start = next((i for i, line in enumerate(lines) if FINDINGS_SECTION.match(line)), None)
    has_section = start is not None
    if start is None:
        start = next((i for i, line in enumerate(lines) if FINDING_HEAD.match(line)), len(lines))
    splitter = _Splitter()
    for line in lines[start:]:
        splitter.feed(line)
    splitter.close()
    found = splitter.findings
    return SplitAnswer(findings=found, has_section=has_section or any(f.number is not None for f in found))


# ---------------------------------------------------------------- значения полей


def quotes_in(value: str) -> list[str]:
    """Цитаты в «ёлочках» верхнего уровня: «A» и «B» дают две, вложенные «…» остаются внутри.

    Без ёлочек берутся пары “…” или "…", а если нет и их, значение целиком без кавычек по краям.
    """
    found: list[str] = []
    depth, start = 0, 0
    for i, ch in enumerate(value):
        if ch == "«":
            depth += 1
            if depth == 1:
                start = i + 1
        elif ch == "»" and depth:
            depth -= 1
            if depth == 0:
                found.append(value[start:i].strip())
    if not found:
        found = [m.group(1).strip() for m in re.finditer(r"[“„\"]([^“”„\"]+)[”“\"]", value)]
    found = [q for q in found if q]
    if found:
        return found
    bare = value.strip().rstrip(";").strip().strip("\"'“”„`«» ")
    return [bare] if bare else []


def block_ids_in(value: str) -> list[str]:
    """ID блоков в порядке появления, без повторов; кириллическая «В» заменяется на латинскую B."""
    return list(dict.fromkeys(f"B{m.group(1)}" for m in BLOCK_ID.finditer(value)))


def _first_match(
    text: str, table: tuple[tuple[str, Property, Property | None], ...]
) -> tuple[Property, Property | None] | None:
    best: tuple[int, Property, Property | None] | None = None
    for pattern, single, of_set in table:
        m = re.search(pattern, text)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), single, of_set)
    return (best[1], best[2]) if best else None


_P = Property
# (образец, свойство требования, свойство набора). Если свойства набора нет, свойство одно на оба уровня.
_ENGLISH: tuple[tuple[str, Property, Property | None], ...] = (
    (r"necessar", _P.NECESSARY, None),
    (r"appropriat", _P.APPROPRIATE, None),
    (r"ambigu", _P.UNAMBIGUOUS, None),
    (r"singular", _P.SINGULAR, None),
    (r"verifiab", _P.VERIFIABLE, None),
    (r"conform", _P.CONFORMING, None),
    (r"\bcomplete", _P.COMPLETE, _P.SET_COMPLETE),
    (r"feasib", _P.FEASIBLE, _P.SET_FEASIBLE),
    (r"\bcorrect", _P.CORRECT, None),
    (r"consisten", _P.SET_CONSISTENT, None),
    (r"comprehensib", _P.SET_COMPREHENSIBLE, None),
    (r"validat", _P.SET_VALIDATABLE, None),
    (r"modifiab", _P.SET_MODIFIABLE, None),
    (r"traceab", _P.TRACEABLE, _P.SET_TRACEABLE),
    (r"\brank", _P.RANKED, _P.SET_RANKED),
)
_RUSSIAN: tuple[tuple[str, Property, Property | None], ...] = (
    (r"необходим", _P.NECESSARY, None),
    (r"уместн", _P.APPROPRIATE, None),
    (r"однозначн", _P.UNAMBIGUOUS, None),
    (r"единичн|атомарн", _P.SINGULAR, None),
    (r"проверяем", _P.VERIFIABLE, None),
    (r"шаблон", _P.CONFORMING, None),
    (r"полнот", _P.COMPLETE, _P.SET_COMPLETE),
    (r"выполним", _P.FEASIBLE, _P.SET_FEASIBLE),
    (r"корректн", _P.CORRECT, None),
    (r"согласованн|непротиворечив", _P.SET_CONSISTENT, None),
    (r"понятн", _P.SET_COMPREHENSIBLE, None),
    (r"валидир", _P.SET_VALIDATABLE, None),
    (r"модифицир", _P.SET_MODIFIABLE, None),
    (r"прослежив", _P.TRACEABLE, _P.SET_TRACEABLE),
    (r"ранжир|приоритет", _P.RANKED, _P.SET_RANKED),
)
_IEEE_CLAUSE: dict[str, tuple[Property, Property | None]] = {
    "1": (_P.CORRECT, None),
    "2": (_P.UNAMBIGUOUS, None),
    "3": (_P.COMPLETE, _P.SET_COMPLETE),
    "4": (_P.SET_CONSISTENT, None),
    "5": (_P.RANKED, _P.SET_RANKED),
    "6": (_P.VERIFIABLE, None),
    "7": (_P.SET_MODIFIABLE, None),
    "8": (_P.TRACEABLE, _P.SET_TRACEABLE),
}


def _property_level(value: str, section: Scope | None) -> Scope:
    """Уровень свойства, которое есть и у требования, и у набора: Complete, Feasible, Traceable, Ranked."""
    low = _norm(value)
    if "набор" in low:
        return Scope.SET
    individual, of_set = "5.2.5" in low, "5.2.6" in low
    if of_set and not individual:
        return Scope.SET
    if individual and not of_set:
        return Scope.REQUIREMENT
    return section or Scope.REQUIREMENT


def parse_property(value: str, section: Scope | None = None) -> Property | None:
    """Свойство из строки «Свойство: Verifiable (проверяемость), ISO/IEC/IEEE 29148:2018, п. 5.2.5».

    Порядок: английское название (первое по тексту), русское, пункт IEEE 830-1998 «4.3.x».
    Уровень (требование или набор) для общих названий берётся из пункта 5.2.5 или 5.2.6, слова
    «набора» или подраздела ответа.
    """
    low = _norm(value)
    found = _first_match(low, _ENGLISH) or _first_match(low, _RUSSIAN)
    if found is None:
        clause = re.search(r"4\.3\.(\d)", low)
        found = _IEEE_CLAUSE.get(clause.group(1)) if clause else None
    if found is None:
        return None
    single, of_set = found
    if of_set is not None and _property_level(value, section) is Scope.SET:
        return of_set
    return single


_DEFECT_STEMS: tuple[tuple[str, DefectType], ...] = (
    ("противореч", DefectType.CONTRADICTION),
    ("двусмысл", DefectType.AMBIGUOUS),
    ("неоднознач", DefectType.AMBIGUOUS),
    ("незаполн", DefectType.PLACEHOLDER),
    ("неполн", DefectType.INCOMPLETE),
    ("непровер", DefectType.UNVERIFIABLE),
    ("невыполн", DefectType.INFEASIBLE),
    ("некоррект", DefectType.INCORRECT),
    ("разнобой", DefectType.TERMINOLOGY),
    ("дублир", DefectType.REDUNDANCY),
    ("составн", DefectType.NOT_ATOMIC),
    ("неуместн", DefectType.DESIGN_DETAIL),
    ("прослежив", DefectType.TRACEABILITY),
    ("оформлен", DefectType.TRACEABILITY),
    ("посторон", DefectType.INJECTED_INSTRUCTION),
)


def parse_defect_type(value: str) -> DefectType | None:
    """Тип дефекта из закрытого списка промпта: точная метка, метка внутри строки, затем основа слова."""
    low = _norm(value).strip(" .,;:«»\"'`")
    exact = DefectType.from_label(low)
    if exact is not None:
        return exact
    for kind in sorted(DefectType, key=lambda k: -len(k.label)):
        if _norm(kind.label) in low:
            return kind
    hits = [(low.find(stem), kind) for stem, kind in _DEFECT_STEMS if stem in low]
    return min(hits, key=lambda h: h[0])[1] if hits else None


def parse_verdict(value: str, heading: str) -> Verdict:
    """Вердикт; если строки нет или она не разобрана, «неясно» при заголовке «Неясно…», иначе «нарушение»."""
    low = _norm(value)
    if low:
        word = Verdict.from_label(low.split()[0].strip(" .,;:"))
        if word is not None:
            return word
        if "неясн" in low:
            return Verdict.UNCLEAR
        if "наруш" in low:
            return Verdict.VIOLATION
    return Verdict.UNCLEAR if _norm(heading).startswith("неясн") else Verdict.VIOLATION


def parse_severity(value: str) -> Severity | None:
    low = _norm(value)
    if not low:
        return None
    word = Severity.from_label(low.split()[0].strip(" .,;:"))
    if word is not None:
        return word
    for stem, severity in (
        ("критич", Severity.CRITICAL),
        ("средн", Severity.MAJOR),
        ("низк", Severity.MINOR),
    ):
        if stem in low:
            return severity
    return None


_BLOCKS_TEST = re.compile(r"^(?:да|мешает)\b[\s,:;.—–-]*(.*)$", re.IGNORECASE | re.DOTALL)


def parse_test(value: str) -> tuple[bool, str | None]:
    """«Тест: мешает, <чего не хватает>» → (True, чего не хватает); «не мешает» → (False, None)."""
    text = value.strip().strip("*").strip()
    low = _norm(text)
    if not low or low.startswith("не мешает"):
        return False, None
    m = _BLOCKS_TEST.match(text)
    if m:
        return True, m.group(1).strip() or None
    if "не мешает" in low or "мешает" not in low:
        return False, None
    return True, text


def unwrap(value: str) -> str:
    """Снимает внешние ёлочки, если значение целиком одна цитата: «Текст» → Текст."""
    text = value.strip()
    inner = quotes_in(text)
    if len(inner) == 1 and text.startswith("«") and text.endswith("»") and text[1:-1].strip() == inner[0]:
        return inner[0]
    return text


def _clean(value: str) -> str | None:
    text = value.strip().strip("*").strip()
    return text or None


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


def draft_from_raw(raw: RawFinding) -> Draft | list[str]:
    """Черновик находки или список того, что мешает разбору кодом (тогда находку переносит модель).

    Обязательны цитата, свойство и тип дефекта: без них находку нельзя проверить и показать.
    Без вердикта, важности, теста и остального находка разбирается, пустые поля остаются пустыми.
    """
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
    title = _clean(raw.heading)
    return Draft(
        title=title,
        block_ids=tuple(i for value in raw.fields.get(Key.BLOCKS, []) for i in block_ids_in(value)),
        quotes=quotes,
        property=prop,
        defect_type=defect,
        verdict=parse_verdict(raw.first(Key.VERDICT), raw.heading),
        scope=raw.section or prop.scope,
        explanation=_clean(raw.first(Key.WHY)) or title or "",
        scenario=_clean(raw.first(Key.SCENARIO)),
        blocks_testing=blocks_testing,
        test_gap=test_gap,
        severity_hint=parse_severity(raw.first(Key.SEVERITY)),
        suggestion=_clean(unwrap(raw.first(Key.FIX))),
    )


__all__ = [
    "Draft",
    "RawFinding",
    "SplitAnswer",
    "block_ids_in",
    "draft_from_raw",
    "parse_defect_type",
    "parse_property",
    "parse_severity",
    "parse_test",
    "parse_verdict",
    "quotes_in",
    "split_findings",
]
