"""Разбор ответа прохода поиска кодом (#18): формат промпта #14 и его частые отклонения."""

from __future__ import annotations

import pytest

from validator.models import DefectType, Property, Scope, Severity, Verdict
from validator.search_parse import NOT_A_FINDING, Draft, draft_from_raw, split_findings
from validator.search_values import (
    block_ids_in,
    parse_defect_type,
    parse_property,
    parse_severity,
    parse_test,
    parse_verdict,
    quotes_in,
    unwrap,
)

# Примеры из другого домена (библиотека), как в промпте: тесты разбора не зависят от документа.
CANONICAL = """## Обзор документа

- Сущности: читатель, книга, выдача (B010–B031). Н-1 здесь не находка: это заметки.

## Находки

### Отдельные требования

#### Н-1. Срок выдачи не задан
- Блоки: B014.2
- Цитата: «Книга выдаётся на срок, достаточный для прочтения»
- Свойство: Verifiable (проверяемость), ISO/IEC/IEEE 29148:2018, п. 5.2.5; IEEE 830-1998, п. 4.3.6
- Тип дефекта: непроверяемость
- Вердикт: нарушение
- Почему мешает: Нельзя написать тест на возврат с конкретной датой.
- Тест: мешает, нет числа дней выдачи
- Важность: средне
- Как исправить: «Книга выдаётся на [уточнить: N] дней»

### Набор требований

#### Н-2. Число книг на руках задано по-разному
- Блоки: B014.2, B031
- Цитата: «Читатель может держать на руках не более пяти книг»
- Цитата: «3 книги»
- Свойство: Consistent (согласованность), ISO/IEC/IEEE 29148:2018, п. 5.2.6; IEEE 830-1998, п. 4.3.4.1
- Тип дефекта: противоречие
- Вердикт: нарушение
- Сценарий: у читателя три книги, он просит четвёртую → по B014.2 выдать → по B031 отказать
- Почему мешает: У теста на четвёртую книгу нет одного ожидаемого результата.
- Тест: мешает, неизвестно, какое число верное: 5 или 3
- Важность: критично
- Как исправить: «Читатель может держать на руках не более [уточнить: 3 или 5] книг»
"""


def drafts(answer: str) -> list[Draft]:
    out = []
    for raw in split_findings(answer).findings:
        draft = draft_from_raw(raw)
        assert isinstance(draft, Draft), draft
        out.append(draft)
    return out


def test_canonical_answer_gives_all_fields() -> None:
    first, second = drafts(CANONICAL)

    assert first == Draft(
        title="Срок выдачи не задан",
        block_ids=("B014.2",),
        quotes=("Книга выдаётся на срок, достаточный для прочтения",),
        property=Property.VERIFIABLE,
        defect_type=DefectType.UNVERIFIABLE,
        verdict=Verdict.VIOLATION,
        scope=Scope.REQUIREMENT,
        explanation="Нельзя написать тест на возврат с конкретной датой.",
        scenario=None,
        blocks_testing=True,
        test_gap="нет числа дней выдачи",
        severity_hint=Severity.MAJOR,
        suggestion="Книга выдаётся на [уточнить: N] дней",
    )
    assert second.quotes == ("Читатель может держать на руках не более пяти книг", "3 книги")
    assert second.block_ids == ("B014.2", "B031")
    assert second.property is Property.SET_CONSISTENT
    assert second.scope is Scope.SET
    assert second.scenario is not None and second.scenario.startswith("у читателя три книги")
    assert second.severity_hint is Severity.CRITICAL


def test_overview_is_not_parsed_as_findings() -> None:
    split = split_findings(CANONICAL)

    assert [raw.number for raw in split.findings] == [1, 2]
    assert split.has_section


def test_bold_field_names_and_heading() -> None:
    answer = CANONICAL.replace("- Блоки:", "- **Блоки:**").replace("- Цитата:", "- **Цитата**:")
    answer = answer.replace("#### Н-1. Срок выдачи не задан", "**Н-1. Срок выдачи не задан**")

    assert drafts(answer) == drafts(CANONICAL)


def test_missing_verdict_follows_heading() -> None:
    unclear = CANONICAL.replace("#### Н-1. Срок выдачи не задан", "#### Н-1. Неясно, какой срок выдачи")
    unclear = unclear.replace("- Вердикт: нарушение\n- Почему мешает: Нельзя", "- Почему мешает: Нельзя")

    first, second = drafts(unclear)

    assert first.verdict is Verdict.UNCLEAR
    assert second.verdict is Verdict.VIOLATION


def test_multiline_value_is_joined_and_nested_quotes_kept() -> None:
    answer = """## Находки
#### Н-1. Термин «выдача» без определения
- Цитата: «Выдача «на дом» оформляется
  в читальном зале»
- Свойство: Unambiguous (однозначность)
- Тип дефекта: двусмысленность
- Как исправить: «Выдача — это [уточнить: определение]»
"""
    [draft] = drafts(answer)

    assert draft.quotes == ("Выдача «на дом» оформляется в читальном зале",)
    assert draft.suggestion == "Выдача — это [уточнить: определение]"
    assert draft.block_ids == ()
    assert draft.explanation == "Термин «выдача» без определения"


def test_rule_after_last_finding_ends_it() -> None:
    answer = CANONICAL.rstrip("\n") + "\n\n---\nВсего находок: 2.\n"

    assert len(split_findings(answer).findings) == 2
    assert (
        drafts(answer)[-1].suggestion == "Читатель может держать на руках не более [уточнить: 3 или 5] книг"
    )


def test_finding_without_required_fields_is_reported_not_dropped() -> None:
    answer = """## Находки
#### Н-1. Срок не задан
- Блоки: B014
- Свойство: что-то своё
- Тип дефекта: непроверяемость
"""
    [raw] = split_findings(answer).findings

    assert draft_from_raw(raw) == ["нет цитаты", "свойство: не распознано"]


def test_heading_without_number_is_still_a_fragment() -> None:
    answer = CANONICAL.replace(
        "#### Н-2. Число книг на руках задано по-разному", "#### 2. Число книг задано по-разному"
    )

    split = split_findings(answer)

    assert [raw.number for raw in split.findings] == [1, None]
    assert split.findings[1].heading == "Число книг задано по-разному"
    assert isinstance(draft_from_raw(split.findings[1]), Draft)


def test_glued_findings_go_to_transfer() -> None:
    answer = """## Находки
1. **Срок выдачи не задан**
- Цитата: «Книга выдаётся на срок, достаточный для прочтения»
- Свойство: Verifiable
- Тип дефекта: непроверяемость
2. **Число книг задано по-разному**
- Цитата: «3 книги»
- Свойство: Consistent
- Тип дефекта: противоречие
"""
    [raw] = split_findings(answer).findings

    assert raw.merged
    assert draft_from_raw(raw) == ["одна находка на фрагмент"]


def test_answer_without_format_has_no_section() -> None:
    split = split_findings("Не могу выполнить проверку: документ пуст.")

    assert split.findings == []
    assert not split.has_section


def test_empty_findings_section_is_valid_answer() -> None:
    split = split_findings("## Обзор документа\n\n- заметки\n\n## Находки\n\nДефектов не найдено.\n")

    assert split.findings == []
    assert split.has_section


def test_text_with_quote_in_findings_section_is_kept_for_transfer() -> None:
    """Таблица или абзац с цитатой вместо формата находки: фрагмент не теряется, его переносит модель."""
    answer = "## Находки\n\n| Блок | Цитата |\n|---|---|\n| B014 | «Книга выдаётся» |\n"

    [raw] = split_findings(answer).findings

    assert raw.number is None and "«Книга выдаётся»" in raw.text
    assert draft_from_raw(raw) == [NOT_A_FINDING]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("#### Н-12. 99.9% выдач без периода", "99.9% выдач без периода"),
        ("#### Н-5. 3.5 секунды на поиск не обоснованы", "3.5 секунды на поиск не обоснованы"),
        ("#### Н-6 — -5 °C в читальном зале", "-5 °C в читальном зале"),
        ("**Н-7.** Срок выдачи не задан", "Срок выдачи не задан"),
        ("Н-8. Срок выдачи не задан", "Срок выдачи не задан"),
        ("#### 3) Дубли терминов", "Дубли терминов"),
    ],
)
def test_title_keeps_leading_numbers_of_the_title(line: str, expected: str) -> None:
    answer = (
        f"## Находки\n\n{line}\n- Цитата: «Книга»\n- Свойство: Verifiable\n- Тип дефекта: непроверяемость\n"
    )

    [raw] = split_findings(answer).findings

    assert raw.heading == expected


def test_mention_of_other_findings_is_not_a_new_finding() -> None:
    """«Н-3 и Н-7 …» в тексте и продолжение поля с «Н-2.» не режут находку на части."""
    answer = CANONICAL.replace(
        "- Почему мешает: Нельзя написать тест на возврат с конкретной датой.",
        "- Почему мешает: Нельзя написать тест на возврат с конкретной датой;\n"
        "Н-2. тоже об этом месте.\n\nН-3 и Н-7 описывают одно место.",
    )

    split = split_findings(answer)

    assert [raw.number for raw in split.findings] == [1, 2]
    assert split.findings[0].first("why").endswith("Н-2. тоже об этом месте.")
    assert all(isinstance(draft_from_raw(raw), Draft) for raw in split.findings)


@pytest.mark.parametrize(
    ("heading", "scope"),
    [
        ("### Находки по набору требований", Scope.SET),
        ("### Требования к набору", Scope.SET),
        ("### Находки по отдельным требованиям", Scope.REQUIREMENT),
        ("## Каждое требование", Scope.REQUIREMENT),
    ],
)
def test_section_headings_in_other_words(heading: str, scope: Scope) -> None:
    answer = (
        f"## Находки\n\n{heading}\n\n#### Н-1. Полнота\n- Цитата: «Книга»\n"
        "- Свойство: Complete (полнота)\n- Тип дефекта: неполнота\n"
    )

    [raw] = split_findings(answer).findings

    assert raw.section is scope
    draft = draft_from_raw(raw)
    assert isinstance(draft, Draft) and draft.scope is scope


def test_level_four_heading_about_the_set_is_a_finding_not_a_section() -> None:
    answer = (
        "## Находки\n\n### Отдельные требования\n\n#### Набор требований не задаёт срок\n"
        "- Цитата: «Книга»\n- Свойство: Complete\n- Тип дефекта: неполнота\n"
    )

    [raw] = split_findings(answer).findings

    assert raw.heading == "Набор требований не задаёт срок"
    assert raw.section is Scope.REQUIREMENT


@pytest.mark.parametrize(
    ("value", "section", "expected"),
    [
        ("Necessary (необходимость), ISO/IEC/IEEE 29148:2018, п. 5.2.5", None, Property.NECESSARY),
        (
            "Appropriate (уместность), ISO/IEC/IEEE 29148:2018, п. 5.2.5; IEEE 830-1998, п. 4.7",
            None,
            Property.APPROPRIATE,
        ),
        (
            "Unambiguous (однозначность), ISO/IEC/IEEE 29148:2018, п. 5.2.5 и 5.2.7",
            None,
            Property.UNAMBIGUOUS,
        ),
        ("Complete (полнота требования), ISO/IEC/IEEE 29148:2018, п. 5.2.5", Scope.SET, Property.COMPLETE),
        (
            "Complete (полнота набора), ISO/IEC/IEEE 29148:2018, п. 5.2.6; IEEE 830-1998, п. 4.3.3.1",
            None,
            Property.SET_COMPLETE,
        ),
        ("Complete (полнота), IEEE 830-1998, п. 4.3.3", Scope.SET, Property.SET_COMPLETE),
        ("Complete (полнота), IEEE 830-1998, п. 4.3.3", None, Property.COMPLETE),
        ("Singular (единичность), ISO/IEC/IEEE 29148:2018, п. 5.2.5", None, Property.SINGULAR),
        ("Feasible (выполнимость), ISO/IEC/IEEE 29148:2018, п. 5.2.5", None, Property.FEASIBLE),
        ("Feasible (выполнимость набора), ISO/IEC/IEEE 29148:2018, п. 5.2.6", None, Property.SET_FEASIBLE),
        ("Verifiable (проверяемость), ISO/IEC/IEEE 29148:2018, п. 5.2.5", None, Property.VERIFIABLE),
        (
            "Correct (корректность), ISO/IEC/IEEE 29148:2018, п. 5.2.5; IEEE 830-1998, п. 4.3.1",
            None,
            Property.CORRECT,
        ),
        ("Conforming (соответствие шаблону), ISO/IEC/IEEE 29148:2018, п. 5.2.5", None, Property.CONFORMING),
        (
            "Consistent (согласованность), ISO/IEC/IEEE 29148:2018, п. 5.2.6",
            Scope.REQUIREMENT,
            Property.SET_CONSISTENT,
        ),
        ("Comprehensible (понятность), ISO/IEC/IEEE 29148:2018, п. 5.2.6", None, Property.SET_COMPREHENSIBLE),
        (
            "Able to be validated (валидируемость), ISO/IEC/IEEE 29148:2018, п. 5.2.6",
            None,
            Property.SET_VALIDATABLE,
        ),
        ("Modifiable (модифицируемость), IEEE 830-1998, п. 4.3.7", None, Property.SET_MODIFIABLE),
        ("Traceable (прослеживаемость), IEEE 830-1998, п. 4.3.8", None, Property.TRACEABLE),
        ("Traceable (прослеживаемость), IEEE 830-1998, п. 4.3.8", Scope.SET, Property.SET_TRACEABLE),
        (
            "Ranked for importance and/or stability (ранжирование), IEEE 830-1998, п. 4.3.5",
            None,
            Property.RANKED,
        ),
        ("**Verifiable / Unambiguous**", None, Property.VERIFIABLE),
        ("проверяемость (ISO/IEC/IEEE 29148, п. 5.2.5)", None, Property.VERIFIABLE),
        ("согласованность набора", None, Property.SET_CONSISTENT),
        ("IEEE 830-1998, п. 4.3.7", None, Property.SET_MODIFIABLE),
        ("что-то своё", None, None),
        ("", None, None),
    ],
)
def test_property_from_line(value: str, section: Scope | None, expected: Property | None) -> None:
    assert parse_property(value, section) is expected


@pytest.mark.parametrize("kind", list(DefectType))
def test_every_defect_label_of_the_prompt_is_known(kind: DefectType) -> None:
    assert parse_defect_type(kind.label) is kind
    assert parse_defect_type(f"**{kind.label.capitalize()}.**") is kind


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("неполнота (не хватает реакции на ошибку)", DefectType.INCOMPLETE),
        ("разнобой терминов", DefectType.TERMINOLOGY),
        ("неоднозначность", DefectType.AMBIGUOUS),
        ("незаполненное место / неполнота", DefectType.PLACEHOLDER),
        ("опечатка", None),
    ],
)
def test_defect_type_variants(value: str, expected: DefectType | None) -> None:
    assert parse_defect_type(value) is expected


@pytest.mark.parametrize(
    ("value", "heading", "expected"),
    [
        ("нарушение", "", Verdict.VIOLATION),
        ("**Неясно**", "", Verdict.UNCLEAR),
        ("неясно (нужно уточнить у автора)", "", Verdict.UNCLEAR),
        ("", "Неясно, что считается выдачей", Verdict.UNCLEAR),
        ("", "Срок не задан", Verdict.VIOLATION),
        ("скорее нарушение", "", Verdict.VIOLATION),
    ],
)
def test_verdict(value: str, heading: str, expected: Verdict) -> None:
    assert parse_verdict(value, heading) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("критично", Severity.CRITICAL),
        ("Средне.", Severity.MAJOR),
        ("низко (оформление)", Severity.MINOR),
        ("скорее критичная", Severity.CRITICAL),
        ("", None),
        ("не знаю", None),
    ],
)
def test_severity(value: str, expected: Severity | None) -> None:
    assert parse_severity(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("мешает, нет числа дней выдачи", (True, "нет числа дней выдачи")),
        ("Мешает: неизвестно, какое число верное", (True, "неизвестно, какое число верное")),
        ("мешает", (True, None)),
        ("не мешает", (False, None)),
        ("**не мешает**", (False, None)),
        ("", (False, None)),
        (
            "тест можно написать, но он мешает понять срок",
            (True, "тест можно написать, но он мешает понять срок"),
        ),
    ],
)
def test_test_line(value: str, expected: tuple[bool, str | None]) -> None:
    assert parse_test(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("«А» и «Б»", ["А", "Б"]),
        ("«Выдача «на дом» оформляется»", ["Выдача «на дом» оформляется"]),
        ("“Книга выдаётся”", ["Книга выдаётся"]),
        ('"Книга выдаётся"', ["Книга выдаётся"]),
        ("Книга выдаётся;", ["Книга выдаётся"]),
        ('Выдаются "редкие" книги', ['Выдаются "редкие" книги']),
        ('"А" (B014) и "Б"', ["А", "Б"]),
        ("", []),
    ],
)
def test_quotes(value: str, expected: list[str]) -> None:
    assert quotes_in(value) == expected


def test_block_ids_latin_cyrillic_and_sentences() -> None:
    assert block_ids_in("B014.2, В031 (кириллица), B014.2 повтор, раздел 3.1, AB123") == ["B014.2", "B031"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("B075–B078", ["B075", "B076", "B077", "B078"]),
        ("B009-011, B020", ["B009", "B010", "B011", "B020"]),
        ("B014.2–B014.4", ["B014.2", "B014.4"]),
        ("B001 — B999", ["B001", "B999"]),
        ("B015–B012", ["B015", "B012"]),
    ],
)
def test_block_ranges(value: str, expected: list[str]) -> None:
    assert block_ids_in(value) == expected


def test_unwrap_only_single_quote() -> None:
    assert unwrap("«Текст [уточнить: N]»") == "Текст [уточнить: N]"
    assert unwrap("«А» или «Б»") == "«А» или «Б»"
    assert unwrap("Текст без кавычек") == "Текст без кавычек"


# ---------------------------------------------------------------- отклонения от формата (ревью #144)


def test_straight_quotes_around_text_with_guillemets_keep_whole_quote() -> None:
    value = '"Приложение читает … через документ «Уведомление … REST API»."'

    assert quotes_in(value) == ["Приложение читает … через документ «Уведомление … REST API»."]


def test_curly_quotes_around_text_with_guillemets_keep_whole_quote() -> None:
    assert quotes_in("“Читает данные из «Журнала» ежедневно”") == ["Читает данные из «Журнала» ежедневно"]


def test_comment_after_quote_is_not_a_second_quote() -> None:
    assert quotes_in("«Система должна работать» — см. также «Сохранить»") == ["Система должна работать"]


@pytest.mark.parametrize("joiner", [" и ", " или ", ", ", "; "])
def test_two_quotes_with_plain_joiner_stay_two(joiner: str) -> None:
    assert quotes_in(f"«первая цитата»{joiner}«вторая цитата»") == ["первая цитата", "вторая цитата"]


@pytest.mark.parametrize("template", ["{n})", "{n}.", "### {n}.", "**{n}.**"])
def test_numbered_headings_without_n_prefix_split_findings(template: str) -> None:
    answer = CANONICAL
    for n in (1, 2):
        answer = answer.replace(f"#### Н-{n}.", template.format(n=n))

    split = split_findings(answer)

    assert [f.number for f in split.findings] == [1, 2]
    assert [d.defect_type for d in drafts(answer)] == [DefectType.UNVERIFIABLE, DefectType.CONTRADICTION]


def test_numbered_fields_are_not_taken_for_headings() -> None:
    answer = CANONICAL.replace("- ", "1. ")

    assert [f.number for f in split_findings(answer).findings] == [1, 2]


def test_place_label_is_read_as_blocks() -> None:
    first = drafts(CANONICAL.replace("- Блоки:", "- Место:"))[0]

    assert first.block_ids == ("B014.2",)
