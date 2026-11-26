"""Порядок находок в отчёте для человека, который их проверяет (docs/value.md, раздел 7).

Почти вся стоимость пути «агент + человек» это время человека на проверку находок, поэтому отчёт
раскладывает их так, чтобы читать было быстрее:

* **Незначительные отдельным блоком.** В разделах 4 и 5 сначала критичные и важные, затем блок
  «Незначительные замечания (по желанию)». Находки не удаляются и не прячутся: у каждой есть карточка
  с цитатой и местом (ТЗ кейса, п. 6), счётчики и статусы считаются по всем находкам.
* **Находки одного места подряд.** Внутри одного уровня важности находки одного требования (или одного
  блока документа, если требования нет) стоят подряд, чтобы контекст читался один раз. Порядок между
  уровнями важности не меняется.

Номера ``F-NN`` ставит верификатор (``verify.number``) и здесь они не меняются: перестановка только
в отчёте, поэтому eval, replay и ``report.json`` видят прежний порядок. Модуль общий для Markdown и PDF.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from validator.models import Finding, Severity, Summary, Verdict

MINOR_BLOCK_TITLE = "Незначительные замечания (по желанию)"
MINOR_BLOCK_NOTE = (
    "Незначительные нарушения: светофор и ответ «можно ли начинать разработку и тестирование» от них "
    "не зависят, их можно проверить после остальных. Каждое учтено в сводке и матрице покрытия"
)
MINOR_BLOCK_NOTE_REQ = MINOR_BLOCK_NOTE + " и в статусе своего требования в таблице выше."
MINOR_BLOCK_NOTE_SET = MINOR_BLOCK_NOTE + "."
NO_REQ_FINDINGS = "Нарушений в отдельных требованиях не найдено."
NO_REQ_MAIN = "Нарушений в отдельных требованиях, кроме незначительных замечаний ниже, не найдено."
NO_SET_FINDINGS = "Нарушений уровня набора (29148 п. 5.2.6, IEEE 830 п. 4.3) не найдено."
NO_SET_MAIN = "Нарушений уровня набора, кроме незначительных замечаний ниже, не найдено."


def _root(rid: str, parent: Mapping[str, str | None]) -> str:
    """Корень требования: атом R-07.2 и его родитель R-07 считаются одним местом."""
    seen = {rid}
    current = rid
    while (up := parent.get(current)) and up not in seen:
        seen.add(up)
        current = up
    return current


def place_key(f: Finding, parent: Mapping[str, str | None]) -> str:
    """Место находки для группировки: первое требование, иначе первый блок цитаты, иначе сама находка."""
    if f.requirement_ids:
        return "req:" + _root(f.requirement_ids[0], parent)
    for q in f.quotes:
        if q.anchor and q.anchor.block_ids:
            return "block:" + q.anchor.block_ids[0]
    return "finding:" + f.id


def reading_order(findings: Sequence[Finding], parent: Mapping[str, str | None]) -> list[Finding]:
    """Важность по-прежнему первая; внутри уровня группы одного места подряд, в порядке первой находки.

    Внутри группы и между группами сохраняется порядок верификатора (место в документе).
    """
    ordered: list[Finding] = []
    for severity in sorted(Severity, key=lambda s: s.rank):
        groups: dict[str, list[Finding]] = {}
        for f in findings:
            if f.severity is severity:
                groups.setdefault(place_key(f, parent), []).append(f)
        ordered += [f for group in groups.values() for f in group]
    return ordered


def is_optional(f: Finding, keep: Collection[str]) -> bool:
    """В блок «по желанию» идёт только незначительное нарушение, которое ни на что не влияет.

    Остаются в основном списке: «неясно» (оно делает светофор жёлтым), находки из «Что исправить первым»
    и находки с блокирующим вопросом автору (``keep``).
    """
    return f.severity is Severity.MINOR and f.verdict is Verdict.VIOLATION and f.id not in keep


def split_minor(
    findings: Sequence[Finding], keep: Collection[str] = ()
) -> tuple[list[Finding], list[Finding]]:
    """Основной список и блок «по желанию», порядок внутри каждого сохраняется."""
    main = [f for f in findings if not is_optional(f, keep)]
    optional = [f for f in findings if is_optional(f, keep)]
    return main, optional


def _findings_word(n: int) -> str:
    """«1 находка», «3 находки», «5 находок»."""
    if n % 10 == 1 and n % 100 != 11:
        return "находка"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "находки"
    return "находок"


def review_line(summary: Summary, optional: int) -> str | None:
    """Строка первой страницы: сколько находок проверять и с чего начать.

    Без минут: оценка времени проверки взята из соседней области
    (docs/research/22-review-cost-benchmarks.md) и в отчёт как факт не идёт. Число критичных берётся
    из ``summary.critical``, как в счётчиках над строкой; ``optional`` это размер блоков «по желанию».
    """
    total = summary.findings_total
    if total == 0:
        return None
    levels = ", ".join(f"{sev.label} {n}" for sev, n in summary.by_severity.items())
    line = f"Объём проверки: {total} {_findings_word(total)} ({levels})."
    if summary.critical:
        line += (
            f" Начните с критичных нарушений ({summary.critical}): они первыми в разделах 4 и 5"
            " и решают, можно ли начинать работу."
        )
    if optional:
        line += (
            f" Незначительные нарушения ({optional}) собраны в конце этих разделов"
            f" в блоке «{MINOR_BLOCK_TITLE}»."
        )
    return line
