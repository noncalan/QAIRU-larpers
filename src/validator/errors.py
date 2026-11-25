"""Иерархия исключений агента, каталог кодов диагностики и код возврата из диагностики.

Исключения нужны внутри шагов: конвейер ловит их на границе шага и превращает в ``Diagnostic``
(``ValidatorError.to_diagnostic``). Наружу из ``pipeline.run`` исключения не выходят.

Коды диагностики стабильные (их сверяет лид QA и eval). Каталог ниже это коды каркаса; задачи шагов
добавляют свои коды сюда же, номера берут из таблицы исследования 04 (раздел 3), где она их задаёт.
"""

from __future__ import annotations

from collections.abc import Iterable

from validator.models import DiagLevel, Diagnostic, ExitCode

# ---------------------------------------------------------------- каталог кодов

# Вход (шаги 0–1). Номера по исследованию 04, раздел 3.
IN_NOT_FOUND = "IN-001"
IN_EMPTY = "IN-002"
IN_TOO_LARGE = "IN-003"
IN_NOT_A_FILE = "IN-005"
IN_UNSUPPORTED = "IN-010"
IN_EXTENSION_MISMATCH = "IN-011"
IN_PDF_ENCRYPTED = "IN-020"
IN_DOCX_BROKEN = "IN-032"
IN_DOCX_TRACKED_CHANGES = "IN-036"
IN_DOCX_TEXT_BOXES = "IN-037"
IN_PDF_BROKEN = "IN-040"
IN_PAGES_WITHOUT_TEXT = "IN-051"
IN_FURNITURE_REMOVED = "IN-054"
IN_ENCODING = "IN-061"
IN_ENCODING_UNCERTAIN = "IN-062"
IN_NO_TEXT = "IN-071"
IN_HEADINGS_ONLY = "IN-081"
IN_PARSE_FAILED = "IN-099"

# Модель.
LLM_UNAVAILABLE = "LLM-001"
LLM_LIMIT = "LLM-002"
LLM_BUDGET = "LLM-003"
LLM_OVERLOADED = "LLM-004"
LLM_REPLAYED = "LLM-005"  # ИНФО: ответы взяты из записи прогона, модель не вызывалась
LLM_CALL_FAILED = "LLM-010"
LLM_BAD_RESPONSE = "LLM-011"
LLM_TIMEOUT = "LLM-012"
LLM_REFUSED = "LLM-013"
LLM_NO_FIXTURE = "LLM-014"
LLM_REPLAY_MISSING = "LLM-015"
LLM_REPLAY_BY_POSITION = "LLM-016"
LLM_SEGMENT_FALLBACK = "LLM-020"
LLM_PASS_FAILED = "LLM-030"  # noqa: S105 - код диагностики, не пароль
LLM_ALL_PASSES_FAILED = "LLM-031"
LLM_PASS_TRANSFERRED = "LLM-032"  # noqa: S105 - код диагностики; часть прохода перенесена в JSON моделью
LLM_TRANSFER_FAILED = "LLM-033"  # перенос в JSON не удался или вернул не все находки прохода
LLM_UNKNOWN_BLOCKS = "LLM-034"  # модель сослалась на ID блоков, которых нет в документе
LLM_EVIDENCE_FAILED = "LLM-040"
LLM_QUESTIONS_FAILED = "LLM-050"

# Настройки и флаги.
CFG_BACKEND_UNAVAILABLE = "CFG-001"
CFG_NOT_IMPLEMENTED = "CFG-010"

# Внутренние ошибки агента.
INT_STEP_FAILED = "INT-001"
INT_STEP_DEGRADED = "INT-002"

# Запись отчёта.
OUT_WRITE_FAILED = "OUT-001"
OUT_FORMAT_FALLBACK = "OUT-002"
OUT_OVERWRITES_INPUT = "OUT-003"


# ---------------------------------------------------------------- исключения


class ValidatorError(Exception):
    """Базовое исключение агента: несёт код диагностики и понятный текст по-русски."""

    default_code = INT_STEP_FAILED

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        hint: str = "",
        location: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.hint = hint
        self.location = location

    def to_diagnostic(
        self,
        *,
        action: str,
        level: DiagLevel = DiagLevel.ERROR,
        step: str | None = None,
    ) -> Diagnostic:
        return Diagnostic(
            code=self.code,
            level=level,
            message=self.message,
            action=action,
            hint=self.hint,
            location=self.location,
            step=step,
        )


class InputError(ValidatorError):
    """Вход нельзя прочитать или прочитан с потерями."""

    default_code = IN_PARSE_FAILED


class LLMError(ValidatorError):
    """Сбой вызова модели: нет доступа, лимит, таймаут, ответ не по схеме."""

    default_code = LLM_CALL_FAILED

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        hint: str = "",
        location: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code, hint=hint, location=location)
        self.retryable = retryable


class ConfigError(ValidatorError):
    """Неверная или неподдержанная настройка прогона."""

    default_code = CFG_BACKEND_UNAVAILABLE


class InternalError(ValidatorError):
    """Ошибка в коде агента."""

    default_code = INT_STEP_FAILED


# ---------------------------------------------------------------- код возврата

_ERROR_EXIT_BY_PREFIX = {
    "INT": ExitCode.INTERNAL,
    "OUT": ExitCode.USAGE,
    "IN": ExitCode.INPUT,
    "LLM": ExitCode.MODEL,
    "CFG": ExitCode.MODEL,
}
# Если ошибок несколько, побеждает первая по этому порядку: внутренняя ошибка важнее всего,
# затем невозможность записать отчёт, затем вход, затем модель.
_ERROR_PRIORITY = (ExitCode.INTERNAL, ExitCode.USAGE, ExitCode.INPUT, ExitCode.MODEL)


def exit_code(diagnostics: Iterable[Diagnostic]) -> ExitCode:
    """Код возврата из диагностики (исследование 04, раздел 4).

    - ОШИБКА с префиксом INT → 1, OUT → 2, IN → 4, LLM или CFG → 5;
    - иначе ПРЕДУПРЕЖДЕНИЕ входа (IN) → 3: вход прочитан частично, проверка сделана;
    - иначе 0. Найденные дефекты требований код не меняют.
    """
    errors: set[ExitCode] = set()
    partial_input = False
    for diag in diagnostics:
        prefix = diag.code.split("-", 1)[0]
        if diag.level is DiagLevel.ERROR:
            errors.add(_ERROR_EXIT_BY_PREFIX.get(prefix, ExitCode.INTERNAL))
        elif diag.level is DiagLevel.WARNING and prefix == "IN":
            partial_input = True
    for code in _ERROR_PRIORITY:
        if code in errors:
            return code
    return ExitCode.PARTIAL_INPUT if partial_input else ExitCode.OK
