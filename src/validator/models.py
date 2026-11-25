"""Общий словарь данных конвейера: pydantic-контракты и перечисления.

Это контракт между ~15 задачами, которые пишутся параллельно (docs/plan.md, «Архитектура и контракты»).
Правила изменения (plan.md, «Как работаем», п. 5):

- поля и значения перечислений только добавляем, с умолчанием, чтобы старый код и старый report.json
  продолжали работать;
- переименование или удаление поля: отдельный маленький PR «Меняет контракт», вливается раньше зависимых;
- схемы ответов модели живут в модуле, который делает вызов, а не здесь.

Модели неизменяемые (frozen): чтобы поменять поле, делаем копию через ``model_copy(update=...)``.
Списки внутри моделей тоже не меняем на месте.
"""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_right
from datetime import datetime
from enum import IntEnum, StrEnum
from functools import cached_property
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------- базовый класс


class Contract(BaseModel):
    """Неизменяемая модель без лишних полей: опечатка в имени поля сразу даёт ошибку."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- перечисления: вход и разбор


class DocKind(StrEnum):
    """Формат входного документа по содержимому (шаг 0), а не только по расширению."""

    MD = "md"
    PDF = "pdf"
    DOCX = "docx"
    UNKNOWN = "unknown"


class DiagLevel(StrEnum):
    """Уровень диагностики (исследование 04, раздел 4)."""

    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def label(self) -> str:
        return {"error": "ОШИБКА", "warning": "ПРЕДУПРЕЖДЕНИЕ", "info": "ИНФО"}[self.value]


class ExitCode(IntEnum):
    """Коды возврата CLI (исследование 04, раздел 4; docs/plan.md, «CLI и выходы»)."""

    OK = 0
    INTERNAL = 1
    USAGE = 2
    PARTIAL_INPUT = 3
    INPUT = 4
    MODEL = 5

    @property
    def meaning(self) -> str:
        return _EXIT_MEANING[self]


_EXIT_MEANING = {
    ExitCode.OK: "вход прочитан, проверка выполнена",
    ExitCode.INTERNAL: "внутренняя ошибка агента",
    ExitCode.USAGE: "ошибка аргументов или пути отчёта",
    ExitCode.PARTIAL_INPUT: "вход прочитан частично, проверка выполнена с оговорками",
    ExitCode.INPUT: "вход невозможно обработать",
    ExitCode.MODEL: "сбой модели или конфигурации",
}


class BlockKind(StrEnum):
    """Вид блока документа после разбора."""

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_ROW = "table_row"
    CODE = "code"
    QUOTE = "quote"
    FOOTNOTE = "footnote"
    FURNITURE = "furniture"  # колонтитул, номер страницы

    @property
    def label(self) -> str:
        return _BLOCK_KIND_LABELS[self]


_BLOCK_KIND_LABELS = {
    BlockKind.HEADING: "заголовок",
    BlockKind.PARAGRAPH: "абзац",
    BlockKind.LIST_ITEM: "пункт списка",
    BlockKind.TABLE_ROW: "строка таблицы",
    BlockKind.CODE: "блок кода",
    BlockKind.QUOTE: "цитата",
    BlockKind.FOOTNOTE: "сноска",
    BlockKind.FURNITURE: "колонтитул",
}


class Extraction(StrEnum):
    """Откуда взят текст якоря: от этого зависит, насколько строго сверять цитату (исследование 04)."""

    TEXT = "text"
    TABLE = "table"
    OCR = "ocr"
    REPAIRED = "repaired"


class QuoteMatch(StrEnum):
    """Результат сверки цитаты с документом."""

    EXACT = "exact"
    FUZZY = "fuzzy"
    NOT_FOUND = "not_found"

    @property
    def label(self) -> str:
        return {"exact": "точно", "fuzzy": "нечётко", "not_found": "не найдено"}[self.value]


# ---------------------------------------------------------------- перечисления: требования и находки


class RequirementType(StrEnum):
    """Тип требования (исследование 18, п. 3.3)."""

    GOAL = "goal"
    UI = "ui"
    PROCESS = "process"
    FUNCTIONAL = "functional"
    INTERACTION = "interaction"
    NFR = "nfr"
    DATA = "data"
    INTEGRATION = "integration"
    ACCEPTANCE = "acceptance"
    API = "api"
    CONSTRAINT = "constraint"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _REQUIREMENT_TYPE_LABELS[self]


_REQUIREMENT_TYPE_LABELS = {
    RequirementType.GOAL: "цель",
    RequirementType.UI: "интерфейс",
    RequirementType.PROCESS: "процесс",
    RequirementType.FUNCTIONAL: "функциональное",
    RequirementType.INTERACTION: "взаимодействие систем",
    RequirementType.NFR: "нефункциональное",
    RequirementType.DATA: "данные",
    RequirementType.INTEGRATION: "интеграция",
    RequirementType.ACCEPTANCE: "критерий приёмки",
    RequirementType.API: "API",
    RequirementType.CONSTRAINT: "ограничение",
    RequirementType.OTHER: "прочее",
}


class BlockLabel(StrEnum):
    """Решение нарезки по блоку: требование или почему нет (исследование 18, п. 3.1)."""

    REQUIREMENT = "requirement"
    CONTEXT = "context"
    METADATA = "metadata"
    TEMPLATE = "template"
    EXAMPLE = "example"
    PLACEHOLDER = "placeholder"
    HEADING = "heading"

    @property
    def label(self) -> str:
        return _BLOCK_LABEL_LABELS[self]


_BLOCK_LABEL_LABELS = {
    BlockLabel.REQUIREMENT: "требование",
    BlockLabel.CONTEXT: "пояснение",
    BlockLabel.METADATA: "служебные сведения",
    BlockLabel.TEMPLATE: "шаблон",
    BlockLabel.EXAMPLE: "пример",
    BlockLabel.PLACEHOLDER: "заглушка",
    BlockLabel.HEADING: "заголовок",
}


class SegmentationMode(StrEnum):
    MODEL = "model"  # нарезку сделала модель, цитаты подставил код
    FALLBACK = "fallback"  # запасной путь без модели: каждый блок с признаками требования


class DecidedBy(StrEnum):
    CODE = "code"
    MODEL = "model"


class Scope(StrEnum):
    """Уровень находки: отдельное требование (29148 п. 5.2.5) или набор (п. 5.2.6)."""

    REQUIREMENT = "requirement"
    SET = "set"


class PropertyInfo(NamedTuple):
    label: str
    english: str
    scope: Scope
    refs: tuple[str, ...]


_ISO = "ISO/IEC/IEEE 29148:2018"
_IEEE = "IEEE 830-1998"


class Property(StrEnum):
    """Свойство стандарта, которое нарушено. Значение это код критерия из исследования 01, раздел 2.

    Ссылки на пункты держим в коде (``Property.refs``), модель ставит только код (исследование 01,
    «Справочник ссылок держать в коде»). Редакция 29148 только 2018: Traceable и Ranked берём из IEEE 830.
    """

    NECESSARY = "IND-NEC"
    APPROPRIATE = "IND-APPR"
    UNAMBIGUOUS = "IND-UNAMB"
    COMPLETE = "IND-COMPL"
    SINGULAR = "IND-SING"
    FEASIBLE = "IND-FEAS"
    VERIFIABLE = "IND-VERIF"
    CORRECT = "IND-CORR"
    CONFORMING = "IND-CONF"
    TRACEABLE = "IND-TRACE"
    RANKED = "IND-RANK"
    SET_COMPLETE = "SET-COMPL"
    SET_CONSISTENT = "SET-CONS"
    SET_FEASIBLE = "SET-FEAS"
    SET_COMPREHENSIBLE = "SET-COMPR"
    SET_VALIDATABLE = "SET-VALID"
    SET_MODIFIABLE = "SET-MOD"
    SET_TRACEABLE = "SET-TRACE"
    SET_RANKED = "SET-RANK"

    @property
    def info(self) -> PropertyInfo:
        return _PROPERTY_INFO[self]

    @property
    def label(self) -> str:
        return self.info.label

    @property
    def scope(self) -> Scope:
        return self.info.scope

    @property
    def refs(self) -> tuple[str, ...]:
        return self.info.refs


def _p(label: str, english: str, scope: Scope, *refs: str) -> PropertyInfo:
    return PropertyInfo(label, english, scope, refs)


_R, _S = Scope.REQUIREMENT, Scope.SET
_PROPERTY_INFO: dict[Property, PropertyInfo] = {
    Property.NECESSARY: _p("Необходимость", "Necessary", _R, f"{_ISO}, п. 5.2.5 (Necessary)"),
    Property.APPROPRIATE: _p("Уместность", "Appropriate", _R, f"{_ISO}, п. 5.2.5 (Appropriate)"),
    Property.UNAMBIGUOUS: _p(
        "Однозначность", "Unambiguous", _R, f"{_ISO}, п. 5.2.5 (Unambiguous)", f"{_IEEE}, п. 4.3.2"
    ),
    Property.COMPLETE: _p(
        "Полнота требования", "Complete", _R, f"{_ISO}, п. 5.2.5 (Complete)", f"{_IEEE}, п. 4.3.3"
    ),
    Property.SINGULAR: _p("Единичность", "Singular", _R, f"{_ISO}, п. 5.2.5 (Singular)"),
    Property.FEASIBLE: _p("Выполнимость", "Feasible", _R, f"{_ISO}, п. 5.2.5 (Feasible)"),
    Property.VERIFIABLE: _p(
        "Проверяемость", "Verifiable", _R, f"{_ISO}, п. 5.2.5 (Verifiable)", f"{_IEEE}, п. 4.3.6"
    ),
    Property.CORRECT: _p("Корректность", "Correct", _R, f"{_ISO}, п. 5.2.5 (Correct)", f"{_IEEE}, п. 4.3.1"),
    Property.CONFORMING: _p("Соответствие шаблону", "Conforming", _R, f"{_ISO}, п. 5.2.5 (Conforming)"),
    Property.TRACEABLE: _p("Прослеживаемость", "Traceable", _R, f"{_IEEE}, п. 4.3.8"),
    Property.RANKED: _p("Ранжирование", "Ranked for importance and/or stability", _R, f"{_IEEE}, п. 4.3.5"),
    Property.SET_COMPLETE: _p(
        "Полнота набора", "Complete", _S, f"{_ISO}, п. 5.2.6 (Complete)", f"{_IEEE}, п. 4.3.3"
    ),
    Property.SET_CONSISTENT: _p(
        "Согласованность", "Consistent", _S, f"{_ISO}, п. 5.2.6 (Consistent)", f"{_IEEE}, п. 4.3.4"
    ),
    Property.SET_FEASIBLE: _p("Выполнимость набора", "Feasible", _S, f"{_ISO}, п. 5.2.6 (Feasible)"),
    Property.SET_COMPREHENSIBLE: _p("Понятность", "Comprehensible", _S, f"{_ISO}, п. 5.2.6 (Comprehensible)"),
    Property.SET_VALIDATABLE: _p(
        "Валидируемость", "Able to be validated", _S, f"{_ISO}, п. 5.2.6 (Able to be validated)"
    ),
    Property.SET_MODIFIABLE: _p("Модифицируемость", "Modifiable", _S, f"{_IEEE}, п. 4.3.7"),
    Property.SET_TRACEABLE: _p("Прослеживаемость набора", "Traceable", _S, f"{_IEEE}, п. 4.3.8"),
    Property.SET_RANKED: _p("Ранжирование набора", "Ranked", _S, f"{_IEEE}, п. 4.3.5"),
}


class DefectType(StrEnum):
    """Тип дефекта формулировки (ТЗ п. 1). Важность считает код по таблице «тип × мешает тесту» (#22)."""

    AMBIGUOUS = "ambiguous"
    UNVERIFIABLE = "unverifiable"
    CONTRADICTION = "contradiction"
    INCOMPLETE = "incomplete"
    OMISSION = "omission"
    NOT_ATOMIC = "not_atomic"
    DESIGN_DETAIL = "design_detail"
    INFEASIBLE = "infeasible"
    REDUNDANCY = "redundancy"
    TERMINOLOGY = "terminology"
    TRACEABILITY = "traceability"
    PLACEHOLDER = "placeholder"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _DEFECT_LABELS[self]

    @property
    def critical_class(self) -> bool:
        """Класс, который ТЗ п. 6 называет критичным: непроверяемые, противоречивые, неполные."""
        return self in (DefectType.UNVERIFIABLE, DefectType.CONTRADICTION, DefectType.INCOMPLETE)


_DEFECT_LABELS = {
    DefectType.AMBIGUOUS: "двусмысленность",
    DefectType.UNVERIFIABLE: "непроверяемость",
    DefectType.CONTRADICTION: "противоречие",
    DefectType.INCOMPLETE: "неполнота",
    DefectType.OMISSION: "пропуск в наборе",
    DefectType.NOT_ATOMIC: "неединичность",
    DefectType.DESIGN_DETAIL: "проектное решение в требовании",
    DefectType.INFEASIBLE: "невыполнимость",
    DefectType.REDUNDANCY: "повтор",
    DefectType.TERMINOLOGY: "разнобой терминов",
    DefectType.TRACEABILITY: "прослеживаемость",
    DefectType.PLACEHOLDER: "незаполненное место",
    DefectType.OTHER: "прочее",
}


class Verdict(StrEnum):
    VIOLATION = "violation"  # нарушение
    UNCLEAR = "unclear"  # неясно: модель не уверена, это кандидат в вопрос автору

    @property
    def label(self) -> str:
        return {"violation": "нарушение", "unclear": "неясно"}[self.value]


class Severity(StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"

    @property
    def label(self) -> str:
        return {"critical": "критично", "major": "важно", "minor": "незначительно"}[self.value]

    @property
    def rank(self) -> int:
        """0 самая высокая важность: удобно сортировать."""
        return {"critical": 0, "major": 1, "minor": 2}[self.value]


class TrafficLight(StrEnum):
    """Светофор первой страницы: можно ли начинать разработку и тестирование (считает код, #22)."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"

    @property
    def label(self) -> str:
        return _LIGHT_LABELS[self]

    @property
    def icon(self) -> str:
        return {"green": "🟢", "yellow": "🟡", "red": "🔴"}[self.value]


_LIGHT_LABELS = {
    TrafficLight.GREEN: "можно начинать разработку и тестирование",
    TrafficLight.YELLOW: "можно начинать с оговорками",
    TrafficLight.RED: "начинать разработку и тестирование нельзя",
}


class SourceKind(StrEnum):
    PASS = "pass"  # noqa: S105 - проход поиска (шаг 4), не пароль
    LINT = "lint"  # правило линтера (шаг 3)
    CHECKLIST = "checklist"  # доменный чек-лист


class DiscardReason(StrEnum):
    QUOTE_NOT_FOUND = "quote_not_found"
    UNKNOWN_BLOCKS = "unknown_blocks"
    DUPLICATE = "duplicate"
    CRITIC_REJECTED = "critic_rejected"
    LIMIT = "limit"
    OTHER = "other"

    @property
    def label(self) -> str:
        return _DISCARD_LABELS[self]


_DISCARD_LABELS = {
    DiscardReason.QUOTE_NOT_FOUND: "цитата не найдена в документе",
    DiscardReason.UNKNOWN_BLOCKS: "ссылается на несуществующие блоки",
    DiscardReason.DUPLICATE: "повтор другой находки",
    DiscardReason.CRITIC_REJECTED: "снята критиком",
    DiscardReason.LIMIT: "сверх лимита находок на требование",
    DiscardReason.OTHER: "другое",
}


class CriticAction(StrEnum):
    KEEP = "keep"
    DOWNGRADE = "downgrade"
    DROP = "drop"


class QuestionPriority(StrEnum):
    """Приоритет вопроса автору (исследование 17, п. 3.2): считает код."""

    P0 = "P0"  # блокирует разработку
    P1 = "P1"  # блокирует тестирование
    P2 = "P2"  # уточнить параллельно

    @property
    def label(self) -> str:
        return {
            "P0": "блокирует разработку",
            "P1": "блокирует тестирование",
            "P2": "уточнить параллельно",
        }[self.value]


class WorkStage(StrEnum):
    DEVELOPMENT = "development"
    TESTING = "testing"


class Addressee(StrEnum):
    BUSINESS = "business"
    ANALYST = "analyst"
    ARCHITECT = "architect"
    SECURITY = "security"

    @property
    def label(self) -> str:
        return {"business": "бизнес", "analyst": "аналитик", "architect": "архитектор", "security": "ИБ"}[
            self.value
        ]


class QuestionOrigin(StrEnum):
    UNCLEAR_VERDICT = "uncertain_verdict"
    WITNESS = "witness_scenario"
    TEST_DRAFT = "test_draft"
    CHECKLIST = "checklist"
    LINT = "lint"
    SET_CHECK = "set_check"
    FINDING = "finding"


# ---------------------------------------------------------------- перечисления: прогон и модель


class Backend(StrEnum):
    """Как вызываем модель (D-008, D-009): подписка, API, записанные ответы, фикстуры."""

    AUTO = "auto"
    CLI = "cli"
    API = "api"
    REPLAY = "replay"
    FAKE = "fake"

    @property
    def label(self) -> str:
        return _BACKEND_LABELS[self]


_BACKEND_LABELS = {
    Backend.AUTO: "автовыбор",
    Backend.CLI: "подписка Claude, команда claude -p",
    Backend.API: "Claude API, SDK anthropic",
    Backend.REPLAY: "записанные ответы (replay)",
    Backend.FAKE: "фикстуры тестов (без модели)",
}


class Profile(StrEnum):
    QUICK = "quick"
    STANDARD = "standard"
    SUBMIT = "submit"


class CacheMode(StrEnum):
    USE = "use"
    REFRESH = "refresh"
    OFF = "off"


class Effort(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class LLMStep(StrEnum):
    """Шаги с вызовом модели. Значение это ещё и имя папки фикстур: tests/fixtures/llm/<шаг>/<номер>.json."""

    SEGMENT = "segment"
    SEARCH = "search"
    SEARCH_TRANSFER = "search_transfer"
    EVIDENCE = "evidence"
    CRITIC = "critic"
    TEST_DRAFT = "test_draft"
    QUESTIONS = "questions"
    QUESTIONS_CRITIC = "questions_critic"


class CallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    CACHED = "cached"
    REPLAYED = "replayed"


class ReportStatus(StrEnum):
    COMPLETE = "complete"  # проверка выполнена полностью
    PARTIAL = "partial"  # проверка выполнена, но часть шагов упала или вход прочитан частично
    NOT_PERFORMED = "not_performed"  # вход не обработать: только паспорт, диагностика и итог


# ---------------------------------------------------------------- диагностика

DIAG_CODE_PATTERN = r"^(IN|LLM|CFG|INT|OUT)-\d{3}$"


class Diagnostic(Contract):
    """Строка раздела «Диагностика»: что увидели, что сделал агент, что сделать человеку (исследование 04)."""

    code: str = Field(
        pattern=DIAG_CODE_PATTERN,
        description="Стабильный код: IN вход, LLM модель, CFG настройки, INT внутренняя ошибка, OUT запись",
    )
    level: DiagLevel = Field(description="ОШИБКА, ПРЕДУПРЕЖДЕНИЕ или ИНФО")
    message: str = Field(description="Что обнаружено, по-русски, без трассировки")
    action: str = Field(default="", description="Что сделал агент")
    hint: str = Field(default="", description="Что сделать человеку")
    location: str | None = Field(default=None, description="Где: файл, страница, строка, шаг")
    step: str | None = Field(default=None, description="Шаг конвейера, на котором возникла диагностика")


# ---------------------------------------------------------------- документ


class Anchor(Contract):
    """Место в документе. Смещения указывают в ``ParsedDocument.text`` (канонический текст)."""

    char_start: int = Field(ge=0, description="Начало фрагмента в ParsedDocument.text")
    char_end: int = Field(ge=0, description="Конец фрагмента (не включительно)")
    block_ids: list[str] = Field(default_factory=list, description="ID блоков: B001")
    sentence_ids: list[str] = Field(default_factory=list, description="ID предложений: B001.2")
    line_start: int | None = Field(default=None, description="Строка канонического текста, с 1")
    line_end: int | None = Field(default=None, description="Последняя строка, включительно")
    page: int | None = Field(default=None, description="Страница PDF, с 1")
    paragraph: int | None = Field(default=None, description="Абзац DOCX, с 1")
    table: int | None = Field(default=None, description="Номер таблицы в документе, с 1")
    row: int | None = Field(default=None, description="Строка таблицы, с 1 (без строки заголовка)")
    cell: int | None = Field(default=None, description="Ячейка в строке таблицы, с 1")
    column: str | None = Field(default=None, description="Заголовок колонки ячейки")
    extraction: Extraction = Field(default=Extraction.TEXT, description="Откуда взят текст")


class SourceLine(Contract):
    """Строка, которую разборщик формата отдаёт в ``build_blocks``.

    Канонический текст документа это ``"\\n".join(line.text for line in lines)``. Пустая строка
    разделяет блоки. Для MD структуру (заголовки ``#``, таблицы ``|``, списки, код) распознаёт
    ``build_blocks``, для PDF и DOCX её передают поля ниже.
    """

    text: str
    page: int | None = None
    paragraph: int | None = None
    heading_level: int | None = Field(default=None, ge=1, le=9, description="Заголовок по стилю или шрифту")
    table: int | None = Field(default=None, description="Номер таблицы, если строка это строка таблицы")
    row: int | None = None
    cells: list[str] = Field(default_factory=list, description="Ячейки строки таблицы")
    header: bool = Field(default=False, description="Строка заголовка таблицы")
    kind: BlockKind | None = Field(default=None, description="Явный вид блока: сноска, колонтитул, код")
    extraction: Extraction = Extraction.TEXT


class Sentence(Contract):
    id: str = Field(description="B001.2: блок и номер предложения с 1")
    text: str
    char_start: int
    char_end: int
    line: int


class TableCell(Contract):
    index: int = Field(description="Номер ячейки в строке, с 1")
    column: str = Field(description="Заголовок колонки или col<N>")
    text: str
    char_start: int
    char_end: int


class Block(Contract):
    id: str = Field(description="B001 по порядку в документе")
    kind: BlockKind
    text: str = Field(description="Дословный фрагмент ParsedDocument.text[anchor.char_start:anchor.char_end]")
    anchor: Anchor
    section: list[str] = Field(default_factory=list, description="Путь заголовков раздела")
    serialized: str = Field(default="", description="Для строк таблиц: «Колонка: значение; …»")
    source_id: str | None = Field(default=None, description="Метка требования в документе, дословно")
    ordinal: int | None = Field(default=None, description="Номер пункта нумерованного списка")
    sentences: list[Sentence] = Field(default_factory=list)
    cells: list[TableCell] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list, description="Подсказки кода для нарезки, не решения")


class TextSignals(Contract):
    """Признаки качества извлечённого текста для диагностики (#26, #27)."""

    pages_total: int | None = None
    pages_without_text: int = 0
    letters_share: float | None = Field(default=None, description="Доля букв среди непробельных символов")
    cyrillic_share: float | None = Field(default=None, description="Доля кириллицы среди букв")
    garbage_share: float | None = Field(default=None, description="Доля мусорных символов")
    repaired: bool = False


class GuardResult(Contract):
    """Итог шага 0: можно ли читать файл и что с ним не так."""

    path: str
    name: str
    kind: DocKind
    size_bytes: int | None = None
    sha256: str | None = None
    fatal: bool = False
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class ParsedDocument(Contract):
    """Документ после разбора (шаг 1). ``text`` канонический: цитаты сверяются с ним."""

    path: str
    name: str
    kind: DocKind
    sha256: str
    size_bytes: int
    text: str
    blocks: list[Block] = Field(default_factory=list)
    pages: int | None = None
    extraction: str = Field(default="", description="Чем и как извлечён текст, для паспорта")
    signals: TextSignals | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)

    @cached_property
    def _blocks_by_id(self) -> dict[str, Block]:
        return {b.id: b for b in self.blocks}

    @cached_property
    def _line_starts(self) -> list[int]:
        starts = [0]
        starts.extend(i + 1 for i, ch in enumerate(self.text) if ch == "\n")
        return starts

    def block(self, block_id: str) -> Block | None:
        """Блок по ID; для ID предложения (B001.2) возвращает его блок."""
        return self._blocks_by_id.get(block_id.split(".", 1)[0])

    def sentence(self, sentence_id: str) -> Sentence | None:
        blk = self.block(sentence_id)
        if blk is None:
            return None
        return next((s for s in blk.sentences if s.id == sentence_id), None)

    def line_of(self, offset: int) -> int:
        """Номер строки (с 1) для смещения в ``text``."""
        return bisect_right(self._line_starts, offset)


# ---------------------------------------------------------------- нарезка


class Requirement(Contract):
    """Требование после нарезки (шаг 2). Цитату подставляет код, а не модель."""

    id: str = Field(pattern=r"^R-\d{2,}(\.\d+)?$", description="R-07 или атом R-07.2, по порядку документа")
    parent_id: str | None = None
    source_id: str | None = Field(default=None, description="ID в документе дословно; None, если номера нет")
    source_locator: str = Field(default="", description="«раздел, п. N» или «таблица: ключ строки»")
    section_path: list[str] = Field(default_factory=list)
    type: RequirementType = RequirementType.OTHER
    subtype: str | None = None
    quote: str = Field(description="Дословный текст из документа, копирует код")
    summary: str = Field(default="", description="Краткая формулировка до 15 слов")
    anchor: Anchor
    atomic_parts: list[str] = Field(default_factory=list)
    entities: dict[str, list[str]] = Field(default_factory=dict)
    links: list[str] = Field(default_factory=list)
    depends_on: str | None = None
    decided_by: DecidedBy = DecidedBy.CODE
    stable_key: str = Field(description="Хеш нормализованной цитаты: сравнение прогонов и версий")


class ExcludedBlock(Contract):
    block_id: str
    label: BlockLabel
    reason: str = ""


class Segmentation(Contract):
    requirements: list[Requirement] = Field(default_factory=list)
    excluded: list[ExcludedBlock] = Field(default_factory=list)
    mode: SegmentationMode = SegmentationMode.FALLBACK
    call_ids: list[str] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)


# ---------------------------------------------------------------- цитаты и находки


class Quote(Contract):
    """Цитата после сверки с документом (``text.locate_quote``)."""

    text: str = Field(description="Цитата как её дала модель или правило")
    match: QuoteMatch
    score: float = Field(ge=0, le=100, description="Балл сверки: 100 точно, иначе rapidfuzz")
    located: str | None = Field(default=None, description="Дословный фрагмент документа, если найден")
    anchor: Anchor | None = None

    @property
    def verified(self) -> bool:
        return self.match is not QuoteMatch.NOT_FOUND


class FindingSource(Contract):
    kind: SourceKind
    pass_index: int | None = Field(default=None, description="Номер прохода поиска, с 1")
    rule_id: str | None = Field(default=None, description="ID правила линтера или пункта чек-листа")
    call_id: str | None = None

    @property
    def label(self) -> str:
        if self.kind is SourceKind.PASS:
            return f"проход {self.pass_index}"
        if self.kind is SourceKind.LINT:
            return f"линтер, правило {self.rule_id}"
        return f"чек-лист, пункт {self.rule_id}"


class CandidateFinding(Contract):
    """Находка одного прохода или одного правила, до слияния (шаги 3–4)."""

    cid: str = Field(description="ID кандидата в прогоне: p1.003, lint.L-01.002")
    source: FindingSource
    property: Property
    defect_type: DefectType
    verdict: Verdict = Verdict.VIOLATION
    scope: Scope = Scope.REQUIREMENT
    block_ids: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list, description="Цитаты как их дала модель, сверит шаг 7")
    explanation: str = Field(description="Почему это дефект и чем мешает разработке и тестированию")
    suggestion: str | None = Field(default=None, description="Формулировка с плейсхолдерами [уточнить: …]")
    blocks_testing: bool = Field(default=False, description="Мешает ли написать тест-кейс")
    severity_hint: Severity | None = Field(default=None, description="Подсказка модели, решает код")


class PassResult(Contract):
    """Итог одного прохода поиска (шаг 4)."""

    index: int = Field(ge=1)
    ok: bool
    raw_text: str = Field(default="", description="Сырой текст прохода: для replay и базовой линии")
    candidates: list[CandidateFinding] = Field(default_factory=list)
    call_ids: list[str] = Field(default_factory=list)
    error: str | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class WitnessReading(Contract):
    quote: str
    expectation: str


class Witness(Contract):
    """Пример-свидетель противоречия (шаг 6): сценарий и как его читает каждое требование."""

    scenario: str
    readings: list[WitnessReading] = Field(default_factory=list)
    checked: bool = Field(default=False, description="Код сверил цитаты и различие ожиданий")
    note: str = ""


class TestDraft(Contract):
    """Попытка тест-кейса (шаг 6): где значения нет, пропуск [?] с тем, чего не хватает."""

    __test__ = False  # не путать pytest: это не тестовый класс

    precondition: str = ""
    action: str = ""
    expected: str = ""
    gaps: list[str] = Field(default_factory=list)
    complete: bool = False


class CriticDecision(Contract):
    action: CriticAction
    reason: str
    severity: Severity | None = None


class MergedFinding(Contract):
    """Находка после слияния проходов и линтера (шаг 5): «найдено в N из K»."""

    mid: str = Field(description="ID в прогоне: M-001")
    property: Property
    defect_type: DefectType
    verdict: Verdict = Verdict.VIOLATION
    scope: Scope = Scope.REQUIREMENT
    block_ids: list[str] = Field(default_factory=list)
    quotes: list[str] = Field(default_factory=list)
    explanation: str
    suggestion: str | None = None
    blocks_testing: bool = False
    severity_hint: Severity | None = None
    sources: list[FindingSource] = Field(default_factory=list)
    members: list[str] = Field(default_factory=list, description="cid кандидатов кластера")
    found_in: int = Field(default=1, ge=0, description="В скольких проходах найдено")
    of: int = Field(default=1, ge=0, description="Сколько проходов было успешных")
    disagreements: list[str] = Field(default_factory=list, description="Разногласия для критика")
    witness: Witness | None = None
    test_draft: TestDraft | None = None
    critic: CriticDecision | None = None


class Finding(Contract):
    """Находка после верификатора (шаг 7): цитаты сверены, важность и F-NN поставил код."""

    id: str = Field(pattern=r"^F-\d{2,}$")
    merged_id: str
    property: Property
    defect_type: DefectType
    verdict: Verdict
    scope: Scope
    severity: Severity
    severity_hint: Severity | None = None
    blocks_testing: bool = False
    quotes: list[Quote] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    section_path: list[str] = Field(default_factory=list, description="Для находок уровня набора")
    explanation: str
    suggestion: str | None = None
    found_in: int = 1
    of: int = 1
    sources: list[FindingSource] = Field(default_factory=list)
    witness: Witness | None = None
    test_draft: TestDraft | None = None
    critic: CriticDecision | None = None
    anchor_corrected: bool = False
    notes: list[str] = Field(default_factory=list)


class DiscardedFinding(Contract):
    finding: MergedFinding
    reason: DiscardReason
    detail: str = ""


class Summary(Contract):
    """Сводка (ТЗ п. 6): все числа считает код."""

    requirements_total: int = 0
    requirements_with_findings: int = 0
    findings_total: int = 0
    violations: int = 0
    unclear: int = 0
    critical: int = 0
    critical_ids: list[str] = Field(default_factory=list)
    by_severity: dict[Severity, int] = Field(default_factory=dict)
    by_property: dict[Property, int] = Field(default_factory=dict)
    by_defect_type: dict[DefectType, int] = Field(default_factory=dict)
    set_level: int = Field(default=0, description="Находок уровня набора")
    discarded_total: int = 0
    discarded_by_reason: dict[DiscardReason, int] = Field(default_factory=dict)
    quotes_total: int = 0
    quotes_exact: int = 0
    quotes_fuzzy: int = 0
    quotes_not_found: int = 0


class Verification(Contract):
    findings: list[Finding] = Field(default_factory=list)
    discarded: list[DiscardedFinding] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    traffic_light: TrafficLight = TrafficLight.YELLOW
    traffic_reason: str = ""
    requirement_status: dict[str, list[str]] = Field(
        default_factory=dict, description="R-NN → список F-NN; пустой список значит «нарушений нет»"
    )
    diagnostics: list[Diagnostic] = Field(default_factory=list)


# ---------------------------------------------------------------- вопросы автору


class QuestionOption(Contract):
    label: str = Field(description="A, B, C …")
    text: str = Field(description="Операционное правило")
    change: str = Field(default="", description="Что поменять в документе или реализации")
    grounded_in: str | None = None


class Question(Contract):
    """Карточка вопроса автору ТЗ (исследование 17, п. 3.1)."""

    id: str = Field(pattern=r"^Q-\d{2,}$")
    priority: QuestionPriority
    blocks: list[WorkStage] = Field(default_factory=list)
    addressee: list[Addressee] = Field(default_factory=list)
    topic: str = ""
    quotes: list[Quote] = Field(default_factory=list)
    question: str
    why: str = ""
    options: list[QuestionOption] = Field(default_factory=list)
    if_unanswered: str = ""
    related_findings: list[str] = Field(default_factory=list)
    origin: QuestionOrigin = QuestionOrigin.FINDING
    standard: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------- модель: запрос, ответ, лог


class StepModel(Contract):
    """Модель и effort шага (D-010). Имя модели полное, effort всегда явно."""

    model: str
    effort: Effort

    @property
    def label(self) -> str:
        return f"{self.model} · {self.effort.value}"


class TokenUsage(Contract):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens + other.cache_creation_input_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


class LLMRequest(Contract):
    step: LLMStep
    call_index: int = Field(ge=1, description="Номер вызова внутри шага, с 1: проход 2 это 2")
    model: str
    effort: Effort
    system: str
    prompt: str
    json_schema: dict[str, Any] | None = None
    timeout_s: int = Field(default=1200, ge=1)

    @property
    def call_id(self) -> str:
        return f"{self.step.value}.{self.call_index}"

    @property
    def key(self) -> str:
        """Ключ кеша: sha256 от модели, effort, системного промпта, промпта и схемы (docs/plan.md)."""
        material = json.dumps(
            [self.model, self.effort.value, self.system, self.prompt, self.json_schema],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class LLMResponse(Contract):
    call_id: str
    text: str = Field(default="", description="Текст ответа (поле result у claude -p)")
    structured: Any = Field(default=None, description="Ответ по схеме (structured_output), если была схема")
    model: str | None = Field(default=None, description="Фактическая модель из ответа")
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = Field(default=None, description="Цена, которую сообщил бэкенд")
    duration_ms: int | None = None
    backend: Backend
    cached: bool = False


class CallRecord(Contract):
    """Строка run/calls.jsonl: одна попытка вызова модели."""

    call_id: str
    step: LLMStep
    call_index: int
    key: str
    model_requested: str
    model_actual: str | None = None
    effort: Effort
    backend: Backend
    started_at: datetime
    duration_ms: int | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = None
    status: CallStatus
    error: str | None = None
    response_path: str | None = None
    prompt_chars: int = 0
    response_chars: int = 0


# ---------------------------------------------------------------- прогон


class RunConfig(Contract):
    """Настройки одного прогона. Собирает ``config.load_config``; модели шагов по D-010."""

    input_path: Path
    output_path: Path
    profile: Profile = Profile.STANDARD
    passes: int = Field(default=3, ge=1, le=9)
    backend: Backend = Backend.AUTO
    replay_dir: Path | None = None
    cache: CacheMode = CacheMode.USE
    cache_dir: Path = Path(".cache/validator")
    answers_path: Path | None = None
    ask: bool = False
    max_budget_usd: float | None = Field(default=None, gt=0)
    step_models: dict[LLMStep, StepModel] = Field(default_factory=dict)
    fake_fixtures: Path | None = Field(default=None, description="Папка фикстур для бэкенда fake")
    pdf_backend: str = Field(default="mupdf", description="mupdf или pdfium (#26)")
    call_timeout_s: int = Field(default=1200, ge=1)
    has_api_key: bool = Field(default=False, description="Есть ли ANTHROPIC_API_KEY (сам ключ не храним)")

    @property
    def out_dir(self) -> Path:
        return self.output_path.parent

    @property
    def report_md_path(self) -> Path:
        return self.output_path.with_suffix(".md")

    @property
    def report_json_path(self) -> Path:
        return self.output_path.with_suffix(".json")

    @property
    def log_path(self) -> Path:
        return self.output_path.with_suffix(".log")

    @property
    def questions_path(self) -> Path:
        return self.out_dir / "questions.yaml"

    @property
    def run_dir(self) -> Path:
        return self.out_dir / "run"

    def model_for(self, step: LLMStep) -> StepModel:
        return self.step_models[step]


class Passport(Contract):
    """Паспорт прогона (ТЗ п. 6): документ, дата, модель, версия агента и окружение."""

    document_name: str
    document_kind: DocKind
    document_sha256: str | None = None
    document_size_bytes: int | None = None
    document_pages: int | None = None
    started_at: datetime
    finished_at: datetime | None = None
    duration_s: float | None = None
    agent_version: str
    agent_version_source: str = Field(description="git, AGENT_VERSION или unknown")
    agent_dirty: bool = False
    prompts_sha256: str
    os: str
    python: str
    backend_requested: Backend
    backend_used: Backend | None = None
    profile: Profile
    passes: int
    models: dict[str, str] = Field(default_factory=dict, description="Шаг → «модель · effort»")
    models_actual: list[str] = Field(default_factory=list, description="Фактические модели из ответов")
    calls_total: int = 0
    calls_failed: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = Field(default=None, description="Цена по тарифу API (подписка не платит)")
    exit_code: ExitCode = ExitCode.OK


class RunMetrics(Contract):
    duration_s: float | None = None
    calls_total: int = 0
    calls_failed: int = 0
    calls_cached: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = None
    candidates_total: int = 0
    merged_total: int = 0
    findings_total: int = 0
    discarded_total: int = 0
    quotes_total: int = 0
    quotes_verified: int = 0
    verified_quote_share: float | None = Field(default=None, description="Доля сверенных цитат, 0–1")


class Conclusion(Contract):
    """Итог (ТЗ п. 6): можно ли начинать разработку и тестирование и что исправить первым."""

    traffic_light: TrafficLight
    headline: str
    performed: bool = True
    fix_first: list[str] = Field(default_factory=list, description="F-NN по порядку важности")
    ask_first: list[str] = Field(default_factory=list, description="Q-NN, блокирующие работу")


class DocumentInfo(Contract):
    name: str
    path: str
    kind: DocKind
    sha256: str | None = None
    size_bytes: int | None = None
    pages: int | None = None
    chars: int = 0
    blocks: int = 0
    extraction: str = ""


class Report(Contract):
    """Всё содержимое отчёта. report.json это сериализация этой модели, из неё рендерятся MD и PDF."""

    schema_version: str = "1"
    status: ReportStatus
    passport: Passport
    document: DocumentInfo | None = None
    input_diagnostics: list[Diagnostic] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(
        default_factory=list, description="Диагностика прогона, кроме входа"
    )
    segmentation: Segmentation | None = None
    verification: Verification | None = None
    questions: list[Question] = Field(default_factory=list)
    conclusion: Conclusion
    metrics: RunMetrics = Field(default_factory=RunMetrics)


class RunContext(Contract):
    """Всё, что собрал конвейер: вход ``build_report``."""

    cfg: RunConfig
    started_at: datetime
    finished_at: datetime
    exit_code: ExitCode
    guard: GuardResult | None = None
    doc: ParsedDocument | None = None
    segmentation: Segmentation | None = None
    lint_hits: list[CandidateFinding] = Field(default_factory=list)
    passes: list[PassResult] = Field(default_factory=list)
    merged: list[MergedFinding] = Field(default_factory=list)
    verification: Verification | None = None
    questions: list[Question] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)
    passport: Passport


class RunOutcome(Contract):
    """Что вернул ``pipeline.run``: код возврата, пути выходов и короткое сообщение для терминала."""

    exit_code: ExitCode
    message: str
    report_path: Path | None = None
    outputs: dict[str, Path] = Field(default_factory=dict)
    report: Report | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
