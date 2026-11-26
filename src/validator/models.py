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
from typing import Any, Literal, NamedTuple

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
    """Тип дефекта формулировки (ТЗ п. 1). Важность считает код по таблице «тип × мешает тесту» (#22).

    Один к одному с закрытым списком из промпта поиска (#14, ``prompts/search_system.md``): метка
    (``label``) это ровно то слово, которое пишет модель, ``from_label`` переводит его обратно.
    """

    CONTRADICTION = "contradiction"
    AMBIGUOUS = "ambiguous"
    INCOMPLETE = "incomplete"
    UNVERIFIABLE = "unverifiable"
    INFEASIBLE = "infeasible"
    INCORRECT = "incorrect"
    TERMINOLOGY = "terminology"
    REDUNDANCY = "redundancy"
    NOT_ATOMIC = "not_atomic"
    DESIGN_DETAIL = "design_detail"
    PLACEHOLDER = "placeholder"
    TRACEABILITY = "traceability"
    INJECTED_INSTRUCTION = "injected_instruction"

    @property
    def label(self) -> str:
        return _DEFECT_LABELS[self]

    @classmethod
    def from_label(cls, label: str) -> DefectType | None:
        """Тип по русской метке из ответа модели: регистр, «ё» и лишние пробелы не важны."""
        return _DEFECT_BY_LABEL.get(_label_key(label))

    @property
    def critical_class(self) -> bool:
        """Класс, который ТЗ п. 6 называет критичным: непроверяемые, противоречивые, неполные."""
        return self in (DefectType.UNVERIFIABLE, DefectType.CONTRADICTION, DefectType.INCOMPLETE)


_DEFECT_LABELS = {
    DefectType.CONTRADICTION: "противоречие",
    DefectType.AMBIGUOUS: "двусмысленность",
    DefectType.INCOMPLETE: "неполнота",
    DefectType.UNVERIFIABLE: "непроверяемость",
    DefectType.INFEASIBLE: "невыполнимость",
    DefectType.INCORRECT: "некорректность",
    DefectType.TERMINOLOGY: "разнобой терминов и единиц",
    DefectType.REDUNDANCY: "дублирование",
    DefectType.NOT_ATOMIC: "составное требование",
    DefectType.DESIGN_DETAIL: "неуместная детализация",
    DefectType.PLACEHOLDER: "незаполненное место",
    DefectType.TRACEABILITY: "прослеживаемость и оформление",
    DefectType.INJECTED_INSTRUCTION: "посторонняя инструкция",
}


def _label_key(label: str) -> str:
    return " ".join(label.lower().replace("ё", "е").split())


_DEFECT_BY_LABEL = {_label_key(text): kind for kind, text in _DEFECT_LABELS.items()}


class Verdict(StrEnum):
    VIOLATION = "violation"  # нарушение
    UNCLEAR = "unclear"  # неясно: модель не уверена, это кандидат в вопрос автору

    @property
    def label(self) -> str:
        return {"violation": "нарушение", "unclear": "неясно"}[self.value]

    @classmethod
    def from_label(cls, label: str) -> Verdict | None:
        """Вердикт из ответа модели: «нарушение» или «неясно» (промпт поиска #14)."""
        return {"нарушение": cls.VIOLATION, "неясно": cls.UNCLEAR}.get(_label_key(label))


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

    @classmethod
    def from_label(cls, label: str) -> Severity | None:
        """Подсказка важности из ответа модели: «критично», «средне», «низко» (промпт поиска #14)
        или метки отчёта «важно», «незначительно»."""
        return _SEVERITY_BY_LABEL.get(_label_key(label))


_SEVERITY_BY_LABEL = {
    "критично": Severity.CRITICAL,
    "средне": Severity.MAJOR,
    "важно": Severity.MAJOR,
    "низко": Severity.MINOR,
    "незначительно": Severity.MINOR,
}


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


class SearchInput(StrEnum):
    """Какой текст видят проходы поиска (#37, D-011, п. 2)."""

    BLOCKS = "blocks"
    """Документ с ID блоков и предложений (``render_for_model``), блоки называет модель."""
    RAW = "raw"
    """Канонический текст без ID блоков, блоки находит код по цитатам."""


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
    # Судья eval (#24): его вызывает только eval через тот же клиент модели, отдельно от агента.
    # Конвейер агента этот шаг не вызывает, модели для него в STEP_MODELS нет.
    JUDGE = "judge"


class CallStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    CACHED = "cached"
    REPLAYED = "replayed"
    RETRIED = "retried"  # попытка упала на временной ошибке, вызов повторён (следующая строка лога)


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
    page_line: int | None = Field(default=None, description="Строка на странице PDF сверху, с 1")
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
    page_line: int | None = Field(default=None, description="Строка на странице PDF сверху, с 1 (#26)")
    wrapped: bool = Field(
        default=False, description="Продолжение абзаца или пункта списка: перенос строки в PDF (#26)"
    )
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
    column: str = Field(description="Заголовок колонки или «колонка N»")
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
    read_path: str | None = Field(
        default=None,
        description="Откуда читать текст, если не из path: восстановленная копия обрезанного PDF (IN-043)",
    )


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
    checklist_item: str | None = Field(
        default=None, description="Пункт доменного чек-листа, по которому проход отметил находку (#35)"
    )

    @property
    def label(self) -> str:
        if self.kind is SourceKind.PASS:
            item = f" (чек-лист {self.checklist_item})" if self.checklist_item else ""
            return f"проход {self.pass_index}{item}"
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
    title: str | None = Field(
        default=None, description="Суть находки одной строкой: заголовок «#### Н-k. …» ответа поиска"
    )
    scenario: str | None = Field(
        default=None,
        description="Сценарий, на котором виден дефект (строка «Сценарий»): вход для свидетеля #39",
    )
    test_gap: str | None = Field(
        default=None, description="Чего не хватает для тест-кейса (после «Тест: мешает,»): вход для #40"
    )


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
    title: str | None = Field(
        default=None, description="Суть находки одной строкой: заголовок «#### Н-k. …» ответа поиска"
    )
    scenario: str | None = Field(
        default=None,
        description="Сценарий, на котором виден дефект (строка «Сценарий»): вход для свидетеля #39",
    )
    test_gap: str | None = Field(
        default=None, description="Чего не хватает для тест-кейса (после «Тест: мешает,»): вход для #40"
    )
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
    severity_basis: str = Field(
        default="", description="Как код посчитал важность: таблица, ограничения, критик"
    )
    severity_disputed: bool = Field(
        default=False, description="Подсказка модели и таблица кода расходятся больше чем на уровень"
    )
    blocks_testing: bool = False
    quotes: list[Quote] = Field(default_factory=list)
    requirement_ids: list[str] = Field(default_factory=list)
    section_path: list[str] = Field(default_factory=list, description="Для находок уровня набора")
    explanation: str
    suggestion: str | None = None
    title: str | None = Field(
        default=None, description="Суть находки одной строкой: заголовок «#### Н-k. …» ответа поиска"
    )
    scenario: str | None = Field(
        default=None,
        description="Сценарий, на котором виден дефект (строка «Сценарий»): вход для свидетеля #39",
    )
    test_gap: str | None = Field(
        default=None, description="Чего не хватает для тест-кейса (после «Тест: мешает,»): вход для #40"
    )
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
    anchors_corrected: int = Field(default=0, description="Находок, у которых место цитаты исправил код")


class Verification(Contract):
    findings: list[Finding] = Field(default_factory=list)
    discarded: list[DiscardedFinding] = Field(default_factory=list)
    summary: Summary = Field(default_factory=Summary)
    traffic_light: TrafficLight = TrafficLight.YELLOW
    traffic_reason: str = ""
    traffic_rule: str = Field(default="", description="Правило светофора словами, его показывает отчёт")
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
    review: QuestionReview | None = Field(
        default=None, description="Самопроверка вопроса критиком (шаг 8г); None: критик не отвечал"
    )


class QuestionReview(Contract):
    """Что критик-самоответ сказал о вопросе (исследование 17, п. 4.2, шаг 4; п. 4.4)."""

    answered: bool = Field(default=False, description="Критик считает, что документ отвечает на вопрос")
    answer: str = Field(default="", description="Ответ критика одной фразой")
    answer_quote: Quote | None = Field(default=None, description="Цитата-ответ после сверки кодом")
    common_sense: bool = Field(default=False, description="Ответ следует из здравого смысла")
    obvious_fix: bool = Field(default=False, description="Исправление однозначно: хватит находки")
    scenario: bool | None = Field(default=None, description="В вопросе есть конкретная ситуация с данными")
    errors: list[int] = Field(
        default_factory=list, description="Номера ошибок формулировки п. 4.4 в нумерации промпта критика"
    )


class WithdrawnQuestion(Contract):
    """Вопрос, который критик снял: документ на него отвечает, цитату-ответ код сверил."""

    question: Question = Field(description="Карточка до снятия, с номером до перенумерации")
    reason: str
    answer: Quote


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
        """Ключ кеша: sha256 от шага, номера вызова, модели, effort, системного промпта, промпта и схемы.

        Шаг и номер входят в ключ (#21): проходы поиска шлют один и тот же промпт, и без номера кеш
        и replay отдали бы всем проходам ответ первого, а «найдено в N из K» стало бы фикцией.
        """
        material = json.dumps(
            [
                self.step.value,
                self.call_index,
                self.model,
                self.effort.value,
                self.system,
                self.prompt,
                self.json_schema,
            ],
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
    raw: dict[str, Any] | None = Field(
        default=None,
        repr=False,
        description="Ответ бэкенда целиком в формате claude -p --output-format json: его пишут кеш и replay",
    )


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
    attempt: int = Field(default=1, ge=1, description="Номер попытки этого вызова: 2 это повтор после сбоя")
    cost_reported_usd: float | None = Field(
        default=None, description="Цена, которую сообщил бэкенд (total_cost_usd); cost_usd считаем по тарифу"
    )


# ---------------------------------------------------------------- прогон


PdfBackendName = Literal["mupdf", "pdfium"]


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
    pdf_backend: PdfBackendName = Field(default="mupdf", description="Движок PDF (#26)")
    search_input: SearchInput = Field(default=SearchInput.BLOCKS, description="Вход поиска (#37, D-011)")
    checklists: bool = Field(
        default=True, description="Доменные чек-листы с триггерами в промпте поиска (#35)"
    )
    call_timeout_s: int = Field(default=1200, ge=1)
    has_api_key: bool = Field(default=False, description="Есть ли ANTHROPIC_API_KEY (сам ключ не храним)")

    @property
    def out_dir(self) -> Path:
        return self.output_path.parent

    @property
    def report_md_path(self) -> Path:
        return self.output_path.with_suffix(".md")

    @property
    def wants_pdf(self) -> bool:
        """ВЫХОД с расширением .pdf: рядом с Markdown собираем и PDF."""
        return self.output_path.suffix.lower() == ".pdf"

    @property
    def report_pdf_path(self) -> Path:
        return self.output_path

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

    @property
    def output_files(self) -> tuple[Path, ...]:
        """Все файлы, которые пишет прогон: ни один не должен совпасть со входом."""
        files = (
            self.report_md_path,
            self.report_json_path,
            self.log_path,
            self.questions_path,
            self.run_dir / "calls.jsonl",
        )
        return (*files, self.report_pdf_path) if self.wants_pdf else files

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
    search_input: SearchInput = Field(default=SearchInput.BLOCKS, description="Вход поиска (#37, D-011)")
    checklists_enabled: bool = Field(default=False, description="Чек-листы включены настройкой прогона (#35)")
    checklists: list[str] = Field(
        default_factory=list, description="ID чек-листов, которые сработали и ушли в промпт поиска (#35)"
    )
    models: dict[str, str] = Field(default_factory=dict, description="Шаг → «модель · effort»")
    models_actual: list[str] = Field(default_factory=list, description="Фактические модели из ответов")
    calls_total: int = 0
    calls_failed: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = Field(default=None, description="Цена по тарифу API (подписка не платит)")
    exit_code: ExitCode = ExitCode.OK


class QuestionMetrics(Contract):
    """Метрики вопросов автору (исследование 17, п. 4.3): считает код по карточкам и ответу критика.

    Доли от 0 до 1. ``None``: не к чему делить (вопросов нет) или критик не проверял вопросы.
    """

    total: int = Field(default=0, description="Вопросов в отчёте, после критика")
    before_critic: int = Field(default=0, description="Вопросов до критика: в отчёте и снятые")
    withdrawn: int = Field(default=0, description="Снято критиком: ответ есть в документе")
    withdrawn_share: float | None = None
    reviewed: int = Field(default=0, description="Вопросов (до критика), на которые критик ответил")
    with_scenario: float | None = Field(default=None, description="Доля со сценарием (по критику)")
    closed: float | None = Field(default=None, description="Доля с 2–4 вариантами и последствием у каждого")
    without_errors: float | None = Field(default=None, description="Доля без ошибок формулировки п. 4.4")
    obvious_fix: int = Field(default=0, description="Дубли с находками: правка однозначна, вопрос лишний")
    common_sense: int = Field(default=0, description="Ответ следует из здравого смысла (по критику)")
    duplicate_pairs: int = Field(
        default=0, description="Пары почти одинаковых вопросов или с теми же местами"
    )
    by_priority: dict[str, int] = Field(default_factory=dict)
    by_addressee: dict[str, int] = Field(default_factory=dict, description="Разброс адресатов")


class StepMetrics(Contract):
    """Вызовы модели одного шага: сколько, сколько заняли и стоили (по записям ``calls.jsonl``)."""

    step: str = Field(description="Значение LLMStep")
    calls: int = Field(default=0, description="Вызовов (повтор после сбоя не отдельный вызов)")
    failed: int = 0
    cached: int = 0
    replayed: int = 0
    duration_s: float = Field(default=0.0, description="Сумма времени всех попыток вызовов шага")
    tokens: TokenUsage = Field(default_factory=TokenUsage)
    cost_usd: float | None = Field(
        default=None, description="Цена по тарифу API; None: цены нет ни у одной попытки"
    )


class FoundInBucket(Contract):
    """Сколько находок отчёта найдено в ``found_in`` из ``of`` проходов поиска."""

    found_in: int
    of: int
    count: int


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
    questions: QuestionMetrics | None = Field(default=None, description="Метрики вопросов автору")
    steps: list[StepMetrics] = Field(default_factory=list, description="Вызовы, время и цена по шагам")
    passes_total: int = Field(default=0, description="Проходов поиска запущено")
    passes_ok: int = Field(default=0, description="Проходов поиска, которые отработали")
    candidates_passes: int = Field(default=0, description="Кандидатов от проходов поиска")
    candidates_lint: int = Field(default=0, description="Кандидатов от правил линтера")
    candidates_checklist: int = Field(
        default=0, description="Кандидатов проходов с меткой пункта доменного чек-листа (#35)"
    )
    findings_checklist: int = Field(
        default=0, description="Находок отчёта, которые хотя бы один проход отметил пунктом чек-листа (#35)"
    )
    discarded_by_reason: dict[DiscardReason, int] = Field(
        default_factory=dict, description="Отброшено верификатором по причинам"
    )
    critic_reviewed: int = Field(default=0, description="Находок, по которым критик дал решение")
    critic_downgraded: int = Field(default=0, description="Критик понизил важность (находки в отчёте)")
    critic_dropped: int = Field(default=0, description="Критик снял находку")
    found_in: list[FoundInBucket] = Field(
        default_factory=list, description="Распределение «найдено в N из K проходов» по находкам отчёта"
    )


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


# ---------------------------------------------------------------- матрица покрытия и самопроверка (#43)


class CellState(StrEnum):
    """Состояние ячейки матрицы покрытия: свойство стандарта по требованию или по набору."""

    CLEAN = "clean"  # свойство проверяли, нарушений не нашли
    VIOLATION = "violation"
    UNCLEAR = "unclear"
    NOT_CHECKED = "not_checked"  # ни один проход поиска не отработал: «нет нарушений» было бы неправдой


class CoverageCell(Contract):
    state: CellState
    finding_ids: list[str] = Field(default_factory=list, description="F-NN, которые держат ячейку")


class CoverageRow(Contract):
    """Строка матрицы: требование × свойства 29148 п. 5.2.5 и IEEE 830 п. 4.3 уровня требования."""

    requirement_id: str
    cells: dict[Property, CoverageCell] = Field(default_factory=dict)


class SetCoverageRow(Contract):
    """Строка матрицы набора: свойство 29148 п. 5.2.6 или IEEE 830 п. 4.3 со ссылками на находки."""

    property: Property
    state: CellState
    finding_ids: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list, description="Пункты стандартов, как в карточках находок")


class CoverageMatrix(Contract):
    """Матрица покрытия критериев (ТЗ п. 5): код строит её из находок, модель в ней не участвует."""

    requirement_properties: list[Property] = Field(default_factory=list)
    rows: list[CoverageRow] = Field(default_factory=list)
    set_rows: list[SetCoverageRow] = Field(default_factory=list)
    extra_finding_ids: list[str] = Field(
        default_factory=list, description="Находки, которым в сетке нет ячейки (свойство другого уровня)"
    )
    unlinked_finding_ids: list[str] = Field(
        default_factory=list,
        description="Находки уровня требования, не привязанные ни к одному требованию нарезки",
    )
    checked: bool = Field(default=True, description="Хотя бы один проход поиска отработал")


class SelfCheckItem(Contract):
    """Одна проверка отчёта. ``integrity``: провал значит, что в отчёт попало неверное, а не неполное."""

    id: str
    title: str
    passed: bool
    integrity: bool = True
    detail: str = ""
    failures: list[str] = Field(default_factory=list, description="Что именно не сошлось, до 10 строк")


class SelfCheck(Contract):
    """Самопроверка отчёта по ТЗ п. 6: только проверяет и сообщает, отчёт задним числом не правит."""

    applicable: bool = Field(default=True, description="False: разделы о требованиях не формировались")
    sections_found: int = 0
    sections_total: int = 0
    findings_total: int = 0
    findings_anchored: int = Field(default=0, description="Находок с якорем и сверенной цитатой")
    quotes_total: int = Field(default=0, description="Цитат в находках отчёта")
    quotes_verified: int = 0
    quotes_rechecked: int = Field(
        default=0, description="Цитат, которые код ещё раз сверил с текстом документа"
    )
    items: list[SelfCheckItem] = Field(default_factory=list)
    summary: str = Field(default="", description="Строка паспорта")

    @property
    def passed(self) -> bool:
        return all(item.passed for item in self.items)

    @property
    def integrity_ok(self) -> bool:
        return all(item.passed for item in self.items if item.integrity)


class ChecklistUse(Contract):
    """Доменный чек-лист в прогоне (#35): сработали ли триггеры, ушёл ли в промпт, какие находки по нему."""

    id: str = Field(description="ID чек-листа: MON, DT, API…")
    title: str
    items: int = Field(ge=0, description="Пунктов в чек-листе")
    min_blocks: int = Field(ge=1, description="Сколько блоков с триггерами нужно для включения")
    matched_blocks: int = Field(ge=0, description="В скольких блоках документа встретились триггеры")
    triggers: list[str] = Field(default_factory=list, description="Примеры сработавших слов")
    triggered: bool = Field(description="Триггеры встретились хотя бы в min_blocks блоках")
    used: bool = Field(description="Пункты ушли в промпт поиска (сработал и чек-листы включены)")
    findings: list[str] = Field(
        default_factory=list, description="F-NN находок, которые проход отметил пунктом этого чек-листа"
    )


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
    questions_withdrawn: list[WithdrawnQuestion] = Field(
        default_factory=list, description="Вопросы, снятые критиком: ответ есть в документе"
    )
    conclusion: Conclusion
    metrics: RunMetrics = Field(default_factory=RunMetrics)
    coverage: CoverageMatrix | None = Field(default=None, description="Матрица покрытия критериев (#43)")
    selfcheck: SelfCheck | None = Field(default=None, description="Самопроверка отчёта по ТЗ п. 6 (#43)")
    checklists: list[ChecklistUse] = Field(
        default_factory=list, description="Доменные чек-листы: включённые и нет, с причиной (#35)"
    )


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
    questions_withdrawn: list[WithdrawnQuestion] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)
    passport: Passport
    checklists: list[ChecklistUse] = Field(default_factory=list, description="Выбор чек-листов (#35)")


class RunOutcome(Contract):
    """Что вернул ``pipeline.run``: код возврата, пути выходов и короткое сообщение для терминала."""

    exit_code: ExitCode
    message: str
    report_path: Path | None = None
    outputs: dict[str, Path] = Field(default_factory=dict)
    report: Report | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)
