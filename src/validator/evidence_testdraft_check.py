"""Шаг 6, попытка теста: проверки кода над черновиком модели (CLAUDE.md, правило 4).

Модель предлагает черновик тест-кейса, код решает, что из него остаётся (``docs/ideas/final-idea.md``,
шаг 6, «модель предлагает, код проверяет»):

- каждое значение, которое модель назвала взятым из документа, должно иметь цитату: её находит
  ``verify_quote`` и в ней должно быть само значение. Иначе значение в тексте заменяется пропуском ``[?N]``,
  а в пропуске пишется, что значение модель назвала без цитаты;
- число в ожидаемом результате, которого нет ни в одном подтверждённом значении, тоже заменяется пропуском:
  ожидаемый результат не выдумывается;
- пропуски нумеруются кодом по порядку в тексте, у каждого есть пояснение, чего не хватает;
- «тест написать можно» принимается, только если нет пропусков и есть хотя бы одно подтверждённое
  значение; иначе вывод не принят.

Чистые функции, модель здесь не вызывается.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field, field_validator

from validator.models import DraftGap, DraftValue, MergedFinding, ParsedDocument, TestDraft
from validator.questions.quotes import verify_quote
from validator.text import normalize

MARKER = re.compile(r"\[\?\s*(\d*)\s*\]")
"""Пропуск в тексте: ``[?]`` или ``[?3]``."""

NUMBER = re.compile(r"(?<![\w\[])(?:\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[.,]\d+)?(?![\w\]])")
"""Число: отдельное, не часть слова и не номер пропуска; «50 000» с пробелом тысяч одно число."""

STEP_REF = re.compile(r"(?i)\bшаг\w*\s*$")
"""Слово «шаг» перед числом: число не больше числа шагов черновика, значит это ссылка на шаг."""

PART_LABELS = {"precondition": "предусловие", "steps": "шаги", "expected": "ожидаемый результат"}
_PARTS = tuple(PART_LABELS)
_SENTINEL_BASE = 0xE000
_SENTINEL_RE = re.compile("[\ue000-\uf8ff]")
"""Метка замены во время правки текста: один символ из области частного использования (индекс пояснения
закодирован кодом символа), чтобы ни числа, ни метки ``[?N]`` в ней не нашлись."""
NO_QUOTE = "значение «{value}» модель назвала без цитаты из документа, поэтому оно заменено пропуском"
NO_NUMBER_QUOTE = "число {value} в ожидаемом результате не подтверждено цитатой из документа"
NO_EXPLANATION = "модель поставила пропуск, но не объяснила, чего не хватает"


# ---------------------------------------------------------------- схема ответа модели


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DraftValueIn(_Lenient):
    value: str
    quote: str = ""


class DraftGapIn(_Lenient):
    number: int | None = None
    missing: str = ""
    part: str = ""

    @field_validator("number", mode="before")
    @classmethod
    def _number(cls, raw: object) -> object:
        """``"1"`` и ``"?1"`` строкой тоже годятся; не число значит «без номера»."""
        if isinstance(raw, str):
            digits = re.sub(r"\D", "", raw)
            return int(digits) if digits else None
        return raw


class DraftEntry(_Lenient):
    """Черновик одной находки в ответе модели. Разбор мягкий: запись не по схеме выпадает одна."""

    finding: str
    can_write: bool = False
    precondition: str = ""
    steps: list[str] = Field(default_factory=list)
    expected: str = ""
    values: list[DraftValueIn] = Field(default_factory=list)
    gaps: list[DraftGapIn] = Field(default_factory=list)
    note: str = ""

    @field_validator("precondition", "expected", "note", "finding", mode="before")
    @classmethod
    def _text(cls, raw: object) -> object:
        return "" if raw is None else raw

    @field_validator("steps", "values", "gaps", mode="before")
    @classmethod
    def _list(cls, raw: object) -> object:
        """Одно значение вместо списка и ``null`` вместо пустого списка."""
        if raw is None:
            return []
        return raw if isinstance(raw, list) else [raw]

    @field_validator("can_write", mode="before")
    @classmethod
    def _flag(cls, raw: object) -> object:
        return False if raw is None else raw


class DraftReplySchema(_Lenient):
    """Схема ответа целиком: образец в ``test_draft_system.md`` обязан ей соответствовать."""

    drafts: list[DraftEntry]


# ---------------------------------------------------------------- журнал проверок


@dataclass
class CheckLog:
    """Что код сделал с черновиками: из этого собирается строка диагностики LLM-045."""

    drafts: int = 0
    values_total: int = 0
    values_verified: int = 0
    values_replaced: int = 0
    numbers_replaced: int = 0
    claims_rejected: int = 0
    unusable: int = 0
    no_finding: int = 0
    skipped: list[str] = field(default_factory=list)

    def add(self, other: CheckLog) -> None:
        """Складывает журнал пачки в общий (пачки идут параллельно, у каждой свой журнал)."""
        for name in (
            "drafts",
            "values_total",
            "values_verified",
            "values_replaced",
            "numbers_replaced",
            "claims_rejected",
            "unusable",
            "no_finding",
        ):
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def details(self) -> list[str]:
        labels = (
            (self.values_replaced, "значений без цитаты заменено пропуском"),
            (self.numbers_replaced, "чисел ожидаемого результата без цитаты заменено пропуском"),
            (self.claims_rejected, "выводов «тест написать можно» без подтверждённых значений не принято"),
            (self.unusable, "черновиков отброшено (нет ни результата, ни пропуска)"),
            (self.no_finding, "записей про несуществующие находки пропущено"),
        )
        return [f"{text}: {count}" for count, text in labels if count]


# ---------------------------------------------------------------- проверка значений


def _flat(text: str) -> str:
    return " ".join(normalize(text)[0].split())


def _has_value(located: str, value: str) -> bool:
    """Значение входит в сверенный фрагмент документа (после нормализации и без внешних кавычек)."""
    needle = _flat(value.strip(" «»\"'"))
    return bool(needle) and needle in _flat(located)


def check_value(raw: DraftValueIn, finding: MergedFinding, doc: ParsedDocument) -> DraftValue:
    """Значение черновика: подтверждено, если цитата найдена в документе и значение есть в ней."""
    quote = verify_quote(doc, raw.quote, finding.block_ids)
    shown = (quote.located or quote.text) if quote is not None else raw.quote
    confirmed = quote is not None and quote.anchor is not None and _has_value(shown, raw.value)
    return DraftValue(
        value=raw.value.strip(),
        quote=" ".join(raw.quote.split()),
        verified=confirmed,
        anchor=quote.anchor if confirmed and quote is not None else None,
    )


def _pattern(value: str) -> re.Pattern[str] | None:
    needle = value.strip()
    return re.compile(re.escape(needle), re.IGNORECASE) if needle else None


# ---------------------------------------------------------------- сборка черновика


@dataclass
class _Draft:
    """Изменяемая заготовка: тексты частей и пояснения, пока код правит значения и нумерует пропуски."""

    texts: dict[str, list[str]]  # часть → строки (шаги отдельными строками)
    explanations: list[tuple[str, str, str | None]] = field(default_factory=list)
    """Пояснения замен по порядку ``\\x00k\\x00``: (чего не хватает, часть, заменённое значение)."""

    def replace(
        self,
        pattern: re.Pattern[str],
        missing: str,
        replaced: str,
        accept: Callable[[re.Match[str]], bool] = lambda _: True,
    ) -> int:
        """Заменяет принятые ``accept`` вхождения шаблона меткой замены; возвращает, сколько их было."""
        count = 0
        for part, lines in self.texts.items():
            for i, line in enumerate(lines):
                hits: list[str] = []

                def swap(m: re.Match[str], part: str = part, hits: list[str] = hits) -> str:
                    if not accept(m):
                        return m.group(0)
                    hits.append(m.group(0))
                    return chr(_SENTINEL_BASE + len(self.explanations) + len(hits) - 1)

                new = pattern.sub(swap, line)
                if hits:
                    lines[i] = new
                    count += len(hits)
                    self.explanations += [(missing, part, replaced)] * len(hits)
        return count


def _canon(number: str) -> str:
    """«50 000» и «50000», «2,5» и «2.5» одно число."""
    return re.sub(r"[ \u00a0]", "", number).replace(",", ".")


def _is_step_ref(m: re.Match[str], steps: int) -> bool:
    """«Шаг 2» при двух и более шагах черновика: номер шага, а не значение. «Шаг 3 000» им не считается."""
    digits = m.group(0)
    if not digits.isdigit() or not 0 < int(digits) <= steps:
        return False
    return STEP_REF.search(m.string[: m.start()]) is not None


def _numbers(text: str, steps: int) -> list[str]:
    """Числа текста вне пропусков, меток замены и номеров шагов («Шаг 2»)."""
    clean = MARKER.sub(" ", _SENTINEL_RE.sub(" ", text))
    return [m.group(0) for m in NUMBER.finditer(clean) if not _is_step_ref(m, steps)]


def _expected_numbers(texts: dict[str, list[str]], confirmed: Sequence[DraftValue]) -> list[str]:
    """Числа ожидаемого результата, которых нет ни в подтверждённых значениях, ни в данных теста.

    Данные теста (предусловие и шаги) тестировщик выбирает сам, из них результат выводить можно;
    значение, которого нет и там, модель взяла неоткуда.
    """
    steps = len(texts["steps"])
    known = {_canon(n) for n in _numbers(" ".join(v.value for v in confirmed), steps)}
    known |= {
        _canon(n) for part in ("precondition", "steps") for line in texts[part] for n in _numbers(line, steps)
    }
    found = _numbers(" ".join(texts["expected"]), steps)
    return [n for n in dict.fromkeys(found) if _canon(n) not in known]


def _sub(pattern: re.Pattern[str], line: str, part: str, take: Callable[[re.Match[str], str], str]) -> str:
    return pattern.sub(lambda m: take(m, part), line)


def _order(draft: _Draft, model_gaps: Sequence[DraftGapIn]) -> tuple[dict[str, list[str]], list[DraftGap]]:
    """Нумерует пропуски по порядку в тексте и собирает объяснения: свои, из ответа модели или замен."""
    by_number = {g.number: g for g in model_gaps if g.number is not None}
    unnumbered = [g for g in model_gaps if g.number is None]
    given: dict[int, int] = {}  # номер модели → новый номер
    gaps: list[DraftGap] = []
    texts: dict[str, list[str]] = {}

    def take(token: re.Match[str], part: str) -> str:
        sentinel = _SENTINEL_RE.fullmatch(token.group(0))
        if sentinel is not None:
            missing, _, replaced = draft.explanations[ord(token.group(0)) - _SENTINEL_BASE]
            label = PART_LABELS[part]
            gaps.append(DraftGap(number=len(gaps) + 1, missing=missing, part=label, replaced=replaced))
            return f"[?{len(gaps)}]"
        old = int(token.group(1)) if token.group(1) else None
        if old is not None and old in given:
            return f"[?{given[old]}]"
        source = by_number.get(old) if old is not None else (unnumbered.pop(0) if unnumbered else None)
        missing = " ".join((source.missing if source else "").split()) or NO_EXPLANATION
        gaps.append(DraftGap(number=len(gaps) + 1, missing=missing, part=PART_LABELS[part]))
        if old is not None:
            given[old] = len(gaps)
        return f"[?{len(gaps)}]"

    pattern = re.compile(MARKER.pattern + "|" + _SENTINEL_RE.pattern)
    for part in _PARTS:
        texts[part] = [_sub(pattern, line, part, take) for line in draft.texts[part]]
    return texts, gaps


def _unplaced(gaps: list[DraftGap], model_gaps: Sequence[DraftGapIn], rendered: dict[str, list[str]]) -> None:
    """Пояснения модели без метки в тексте: код ставит метку в конец той части, которую назвала модель.

    Так каждый пропуск списка виден и в тексте черновика, и читатель не ищет ``[?N]``, которого нет.
    Часть не названа или неизвестна: ожидаемый результат.
    """
    explained = {g.missing for g in gaps}
    for g in model_gaps:
        missing = " ".join(g.missing.split())
        if not missing or missing in explained:
            continue
        part = g.part.strip().lower() if g.part.strip().lower() in PART_LABELS else "expected"
        gaps.append(DraftGap(number=len(gaps) + 1, missing=missing, part=PART_LABELS[part]))
        marker = f"[?{len(gaps)}]"
        lines = rendered[part]
        if lines:
            lines[-1] = f"{lines[-1]} {marker}"
        else:
            lines.append(marker)
        explained.add(missing)


def build_draft(
    entry: DraftEntry, finding: MergedFinding, doc: ParsedDocument, log: CheckLog
) -> TestDraft | None:
    """Черновик после проверок кода или None, если от него нет пользы (нет результата и нет пропуска)."""
    texts = {
        "precondition": [entry.precondition.strip()] if entry.precondition.strip() else [],
        "steps": [s.strip() for s in entry.steps if s and s.strip()],
        "expected": [entry.expected.strip()] if entry.expected.strip() else [],
    }
    log.drafts += 1
    values = [check_value(v, finding, doc) for v in entry.values if v.value.strip()]
    draft = _Draft(texts)
    kept: list[DraftValue] = []
    for v in values:
        log.values_total += 1
        pattern = _pattern(v.value)
        if pattern is None or not any(pattern.search(x) for lines in texts.values() for x in lines):
            continue  # значение не в тексте черновика: показывать его как основание нечего
        if v.verified:
            log.values_verified += 1
            kept.append(v)
        elif draft.replace(pattern, NO_QUOTE.format(value=v.value), v.value):
            log.values_replaced += 1
    for number in _expected_numbers(texts, kept):
        target = _canon(number)

        def same(m: re.Match[str], target: str = target) -> bool:
            return _canon(m.group(0)) == target and not _is_step_ref(m, len(texts["steps"]))

        if draft.replace(NUMBER, NO_NUMBER_QUOTE.format(value=number), number, same):
            log.numbers_replaced += 1
    rendered, gaps = _order(draft, entry.gaps)
    _unplaced(gaps, entry.gaps, rendered)
    if not gaps and not entry.can_write:
        return _fallback(entry, finding, rendered, kept, log)
    return _assemble(entry, rendered, kept, gaps, log)


def _fallback(
    entry: DraftEntry,
    finding: MergedFinding,
    rendered: dict[str, list[str]],
    kept: list[DraftValue],
    log: CheckLog,
) -> TestDraft | None:
    """Модель не поставила ни пропуска, ни «тест написать можно»: берём то, что нашёл поиск, иначе отказ."""
    missing = " ".join((finding.test_gap or "").split())
    if not missing or not (rendered["expected"] or rendered["steps"]):
        log.unusable += 1
        return None
    gap = DraftGap(number=1, missing=missing, part=PART_LABELS["expected"])
    rendered["expected"].append("[?1]")
    return _assemble(entry, rendered, kept, [gap], log)


def _assemble(
    entry: DraftEntry,
    rendered: dict[str, list[str]],
    kept: list[DraftValue],
    gaps: list[DraftGap],
    log: CheckLog,
) -> TestDraft | None:
    steps = rendered["steps"]
    expected = " ".join(rendered["expected"])
    if not steps and not expected and not gaps:
        log.unusable += 1
        return None
    note = " ".join(entry.note.split())
    complete = entry.can_write and not gaps and bool(kept)
    if entry.can_write and not complete and not gaps:
        log.claims_rejected += 1
        extra = "Вывод «тест написать можно» не принят: нет ни одного значения с цитатой из документа."
        note = f"{note} {extra}".strip()
    return TestDraft(
        precondition=" ".join(rendered["precondition"]),
        action="; ".join(steps),
        expected=expected,
        gaps=[g.missing for g in gaps],
        complete=complete,
        steps=steps,
        values=kept,
        gap_items=gaps,
        note=note,
    )


__all__ = [
    "CheckLog",
    "DraftEntry",
    "DraftReplySchema",
    "build_draft",
    "check_value",
]
