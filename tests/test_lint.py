"""Линтер правил без модели (шаг 3, #28): каждое правило срабатывает на примере из другого домена и молчит
на близком корректном; цитаты находятся в документе; шума мало.

Примеры нарочно не из нашего проверяемого документа: склад, библиотека, дроны, медицина
(CLAUDE.md, правило 3).
"""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from conftest import REQUIREMENTS
from validator.ingest import parse
from validator.lint import RULES, lint, run_rules
from validator.lint.doc_rules import DOC_CHECKS
from validator.lint.lexicon import fold, load_lexicon
from validator.lint.rules import RULE_BY_ID
from validator.lint.text_rules import UNIT_CHECKS
from validator.models import (
    BlockLabel,
    CandidateFinding,
    DocKind,
    ExcludedBlock,
    ParsedDocument,
    QuoteMatch,
    Requirement,
    Scope,
    Segmentation,
    SourceKind,
)
from validator.segment import fallback_segmentation, requirement_id
from validator.text import locate_quote

MakeMd = Callable[[str], ParsedDocument]
NOISE_LIMIT = 0.2
"""Не больше одного срабатывания на 5 требований (issue #28, защита от шума)."""


def findings_of(make_md: MakeMd, text: str) -> list[CandidateFinding]:
    doc = make_md(text)
    return lint(doc, fallback_segmentation(doc))


def rule_ids(found: list[CandidateFinding]) -> set[str]:
    return {f.source.rule_id or "" for f in found}


# ---------------------------------------------------------------- по правилу: срабатывает и молчит

DEFECT_CASES = [
    pytest.param(
        "L-01",
        "- Система поиска должна быстро находить книгу по названию.",
        "- Система поиска должна находить книгу по названию за 2 секунды.",
        id="L-01 размытое слово",
    ),
    pytest.param(
        "L-01",
        "- Диспетчер должен получать сигнал дрона мгновенно после посадки.",
        "- Диспетчер должен получать сигнал дрона не позднее 3 секунд после посадки.",
        id="L-01 время без меры",
    ),
    pytest.param(
        "L-01",
        "- The scanner must read the barcode quickly and show the title.",
        "- The scanner must read the barcode and show the title.",
        id="L-01 английский",
    ),
    pytest.param(
        "L-02",
        "- Читатель при необходимости может продлить выдачу книги.",
        "- Читатель может продлить выдачу книги один раз на 14 дней.",
        id="L-02 лазейка",
    ),
    pytest.param(
        "L-02",
        "- The nurse should call the doctor where possible after the alarm.",
        "- The nurse must call the doctor after the alarm.",
        id="L-02 английский",
    ),
    pytest.param(
        "L-03",
        "- Каталог принимает файлы форматов PDF, EPUB и т. д. для загрузки.",
        "- Каталог принимает файлы форматов PDF и EPUB для загрузки.",
        id="L-03 и т. д.",
    ),
    pytest.param(
        "L-03",
        "- Склад отказывает в приёмке при кодах брака 10, 11 и далее по списку.",
        "- Склад отказывает в приёмке при кодах брака 10 и 11 по списку.",
        id="L-03 ряд значений",
    ),
    pytest.param(
        "L-04",
        "- Дрон возвращается на базу и/или совершает аварийную посадку.",
        "- Дрон возвращается на базу или совершает аварийную посадку.",
        id="L-04 и/или",
    ),
    pytest.param(
        "L-05",
        "- Максимальная масса груза на борту дрона: TBD для всех моделей.",
        "- Максимальная масса груза на борту дрона: 5 кг для всех моделей.",
        id="L-05 TBD",
    ),
    pytest.param(
        "L-05",
        "- Срок хранения медицинских записей уточняется отдельным приказом.",
        "- Срок хранения медицинских записей равен 25 годам.",
        id="L-05 уточняется",
    ),
    pytest.param(
        "L-06",
        "- Время ожидания ответа от склада не более 30 для каждой заявки.",
        "- Время ожидания ответа от склада не более 30 секунд для каждой заявки.",
        id="L-06 число без единицы",
    ),
    pytest.param(
        "L-06",
        "- Выдача книг прекращается в течение 5, после чего читальный зал закрывается.",
        "- Выдача книг прекращается в течение 5 минут, после чего читальный зал закрывается.",
        id="L-06 после предела стоит конец фразы",
    ),
    pytest.param(
        "L-10",
        "- Дрон должен взлетать по команде и должен сообщать высоту каждые 5 секунд.",
        "- Дрон должен взлетать по команде и сообщать высоту каждые 5 секунд.",
        id="L-10 два «должен»",
    ),
]


@pytest.mark.parametrize(("rule_id", "bad", "good"), DEFECT_CASES)
def test_rule_fires_on_defect_and_stays_silent_on_correct_text(
    make_md: MakeMd, rule_id: str, bad: str, good: str
) -> None:
    assert rule_id in rule_ids(findings_of(make_md, bad))
    assert rule_id not in rule_ids(findings_of(make_md, good))


QUIET_CASES = [
    pytest.param(
        "L-01",
        "- Раздел справки называется «Быстрый старт» и открывается кнопкой F1.",
        id="L-01 слово в кавычках это название",
    ),
    pytest.param(
        "L-01",
        "- Поиск должен быстро отвечать: не дольше 2 секунд на запрос.",
        id="L-01 число в том же предложении задаёт меру",
    ),
    pytest.param(
        "L-01",
        "- Поиск по названию работает, а для просто так найденных записей показывается карточка.",
        id="L-01 «просто» в словарь не входит",
    ),
    pytest.param(
        "L-05",
        "- Регистратор показывает ошибку, если поле «Фамилия» не заполнено или не определено.",
        id="L-05 описание состояния поля это не маркер",
    ),
    pytest.param(
        "L-06",
        "- Выдача книг прекращается не позднее 15:00, после чего читальный зал закрывается.",
        id="L-06 время суток",
    ),
    pytest.param(
        "L-06",
        "- Читатель берёт одновременно не более 5 книг из читального зала.",
        id="L-06 существительное после числа это единица",
    ),
    pytest.param(
        "L-06",
        "- Скидка не более 5 % и не менее 1 000 ₸ на каждый заказ склада.",
        id="L-06 процент и валюта",
    ),
    pytest.param(
        "L-06",
        "- Запрос к реестру занимает от 1 000 до 5 000 мс при полной нагрузке.",
        id="L-06 тысячи через пробел и единица после диапазона",
    ),
    pytest.param(
        "L-10",
        "- Дрон должен взлетать по команде. Дрон должен сообщать высоту каждые 5 секунд.",
        id="L-10 в разных предложениях это не составное",
    ),
    pytest.param(
        "L-03",
        "- Склад принимает паллеты, например европаллеты, при наличии маркировки на каждой.",
        id="L-03 «например» в словарь не входит",
    ),
]


@pytest.mark.parametrize(("rule_id", "text"), QUIET_CASES)
def test_rule_stays_silent_on_close_correct_text(make_md: MakeMd, rule_id: str, text: str) -> None:
    assert rule_id not in rule_ids(findings_of(make_md, text))


# ---------------------------------------------------------------- правила по набору (L-07…L-09)

HEADINGS = "# Склад\n\n## 1. Приёмка\n\n## 2. Выдача\n\n## 3. Возвраты\n\n"


def test_l07_reference_to_missing_section_is_flagged(make_md: MakeMd) -> None:
    text = HEADINGS + "Кладовщик сверяет паллету с накладной, как описано в разделе 7 этого документа.\n"

    found = [f for f in findings_of(make_md, text) if f.source.rule_id == "L-07"]

    assert len(found) == 1
    assert "разделе 7" in found[0].quotes[0]


def test_l07_reference_to_existing_section_is_silent(make_md: MakeMd) -> None:
    text = HEADINGS + "Кладовщик сверяет паллету с накладной, как описано в разделе 2 этого документа.\n"

    assert "L-07" not in rule_ids(findings_of(make_md, text))


def test_l07_reference_to_section_by_name_is_checked_against_headings(make_md: MakeMd) -> None:
    bad = (
        HEADINGS + "Кладовщик возвращает паллету по правилам из раздела «Утилизация брака» этого документа.\n"
    )
    good = HEADINGS + "Кладовщик возвращает паллету по правилам из раздела «Возвраты» этого документа.\n"
    other = (
        HEADINGS
        + "Кладовщик возвращает паллету по правилам из раздела «Утилизация» приложенного документа.\n"
    )

    assert "L-07" in rule_ids(findings_of(make_md, bad))
    assert "L-07" not in rule_ids(findings_of(make_md, good))
    assert "L-07" not in rule_ids(findings_of(make_md, other))


def test_l07_reference_to_missing_requirement_label_is_flagged(make_md: MakeMd) -> None:
    items = (
        "- **СК-1.** Кладовщик принимает паллету по накладной.\n"
        "- **СК-2.** Кладовщик выдаёт паллету по заявке и проверяет остаток, как в СК-9.\n"
        "- **СК-3.** Кладовщик возвращает паллету по накладной возврата, как в СК-1.\n"
    )

    found = [f for f in findings_of(make_md, items) if f.source.rule_id == "L-07"]

    assert len(found) == 1
    assert "СК-9" in found[0].quotes[0]


def test_l07_is_silent_without_numbered_headings_or_labels(make_md: MakeMd) -> None:
    text = "# Склад\n\nКладовщик сверяет паллету с накладной, как описано в разделе 7 и в пункте ХХ-9.\n"

    assert "L-07" not in rule_ids(findings_of(make_md, text))


def test_l08_duplicate_label_is_flagged_with_both_places(make_md: MakeMd) -> None:
    items = (
        "- **СК-1.** Кладовщик принимает паллету по накладной.\n"
        "- **СК-2.** Кладовщик выдаёт паллету по заявке.\n"
        "- **СК-2.** Кладовщик возвращает паллету по накладной возврата.\n"
    )

    found = [f for f in findings_of(make_md, items) if f.source.rule_id == "L-08"]

    assert len(found) == 1
    assert len(found[0].quotes) == 2
    assert found[0].scope is Scope.SET


def test_l08_unique_labels_are_silent(make_md: MakeMd) -> None:
    items = (
        "- **СК-1.** Кладовщик принимает паллету по накладной.\n"
        "- **СК-2.** Кладовщик выдаёт паллету по заявке.\n"
        "- **СК-3.** Кладовщик возвращает паллету по накладной возврата.\n"
    )

    assert "L-08" not in rule_ids(findings_of(make_md, items))


def test_l09_gap_in_numbering_is_flagged_as_a_question(make_md: MakeMd) -> None:
    items = (
        "- **СК-1.** Кладовщик принимает паллету по накладной.\n"
        "- **СК-2.** Кладовщик выдаёт паллету по заявке.\n"
        "- **СК-4.** Кладовщик возвращает паллету по накладной возврата.\n"
    )

    found = [f for f in findings_of(make_md, items) if f.source.rule_id == "L-09"]

    assert len(found) == 1
    assert "СК-3" in (found[0].title or "")
    assert found[0].verdict.value == "unclear"


@pytest.mark.parametrize(
    "labels",
    [
        pytest.param(("СК-1", "СК-2", "СК-3"), id="без пропуска"),
        pytest.param(("СК-1", "СК-4"), id="ряд из двух меток не проверяем"),
        pytest.param(("СК-10", "СК-20", "СК-30"), id="нумерация шагами"),
        pytest.param(("СК-5", "СК-6", "СК-8"), id="нумерация начинается не с 1"),
    ],
)
def test_l09_silent_without_a_real_gap(make_md: MakeMd, labels: tuple[str, ...]) -> None:
    items = "".join(
        f"- **{label}.** Кладовщик выполняет операцию номер {i} по накладной.\n"
        for i, label in enumerate(labels)
    )

    assert "L-09" not in rule_ids(findings_of(make_md, items))


# ---------------------------------------------------------------- область действия и устройство находок


def test_rules_fire_only_inside_requirements_from_segmentation(make_md: MakeMd) -> None:
    doc = make_md(
        "# Удобный быстрый склад\n\n"
        "Пояснение: склад должен быть удобным и быстрым, при необходимости.\n\n"
        "- Кладовщик быстро принимает паллету по накладной.\n"
    )
    blocks = {b.id: b for b in doc.blocks}
    context, requirement = (b for b in doc.blocks if b.kind.value in {"paragraph", "list_item"})
    seg = Segmentation(
        requirements=[
            Requirement(
                id=requirement_id(1),
                quote=requirement.text,
                anchor=requirement.anchor,
                stable_key="k1",
            )
        ],
        excluded=[ExcludedBlock(block_id=context.id, label=BlockLabel.CONTEXT)],
    )

    found = lint(doc, seg)

    assert blocks and {f.block_ids[0] for f in found} == {requirement.id}


def test_empty_segmentation_gives_no_findings(make_md: MakeMd) -> None:
    doc = make_md("- Кладовщик быстро принимает паллету по накладной.\n")

    assert lint(doc, Segmentation()) == []


def test_headings_code_and_quoted_names_are_not_scanned(make_md: MakeMd) -> None:
    text = (
        "## Удобный интерфейс\n\n"
        "```\nпоиск быстро находит всё и т. д.\n```\n\n"
        "- Кнопка называется «Быстрый возврат» и стоит в шапке окна.\n"
    )

    assert findings_of(make_md, text) == []


def test_finding_carries_source_rule_property_quote_and_explanation(make_md: MakeMd) -> None:
    doc = make_md("- Система поиска должна быстро находить книгу по названию.\n")

    (finding,) = lint(doc, fallback_segmentation(doc))

    assert finding.cid == "lint.L-01.001"
    assert finding.source.kind is SourceKind.LINT
    assert finding.source.label == "линтер, правило L-01"
    assert finding.property is RULE_BY_ID["L-01"].criterion
    assert finding.block_ids == ["B001"]
    assert finding.quotes == ["Система поиска должна быстро находить книгу по названию."]
    assert "быстро" in finding.explanation
    assert finding.suggestion and "[уточнить:" in finding.suggestion
    assert finding.severity_hint is None


def test_long_sentence_is_cut_to_a_window_that_still_contains_the_word(make_md: MakeMd) -> None:
    filler = " ".join(["после проверки накладной и пересчёта остатков на складе"] * 8)
    doc = make_md(f"- Кладовщик {filler} быстро выдаёт паллету {filler} по заявке покупателя.\n")

    (finding,) = lint(doc, fallback_segmentation(doc))

    quote = finding.quotes[0]
    assert "быстро" in quote
    assert len(quote) <= 220
    assert locate_quote(doc, quote).match is QuoteMatch.EXACT


def test_findings_come_in_document_order_with_numbered_ids(make_md: MakeMd) -> None:
    doc = make_md(
        "- Поиск быстро находит книгу по названию.\n"
        "- Читатель при необходимости продлевает выдачу.\n"
        "- Поиск удобно показывает автора книги.\n"
    )

    found = lint(doc, fallback_segmentation(doc))

    assert [f.cid for f in found] == ["lint.L-01.001", "lint.L-02.001", "lint.L-01.002"]


def test_two_words_in_one_sentence_make_one_finding(make_md: MakeMd) -> None:
    doc = make_md("- Поиск должен быстро и удобно находить книгу по названию.\n")

    found = lint(doc, fallback_segmentation(doc))

    assert len(found) == 1
    assert "быстро" in found[0].explanation
    assert "удобно" in found[0].explanation


def test_lint_is_deterministic(make_md: MakeMd) -> None:
    doc = make_md(
        "- Поиск быстро находит книгу и т. д. при необходимости.\n- Поиск удобно показывает автора.\n"
    )
    seg = fallback_segmentation(doc)

    assert lint(doc, seg) == lint(doc, seg)


# ---------------------------------------------------------------- каталог и словарь


def test_every_rule_has_a_check_and_a_standard_reference() -> None:
    ids = [rule.id for rule in RULES]

    assert ids == sorted(set(ids)) and 6 <= len(ids) <= 10
    assert set(ids) == set(UNIT_CHECKS) | set(DOC_CHECKS)
    for rule in RULES:
        assert rule.description and rule.threshold and rule.standard, rule.id


def test_lexicon_entries_explain_why_and_do_not_overlap_in_sections() -> None:
    lexicon = load_lexicon()

    for section, terms in lexicon.items():
        assert terms, section
        assert all(term.why for term in terms), section
    keys = [fold(term.key) for terms in lexicon.values() for term in terms]
    assert len(keys) == len(set(keys)), "одно слово в двух разделах словаря"


# ---------------------------------------------------------------- на проверяемом документе


@pytest.mark.parametrize("kind", [DocKind.MD, DocKind.PDF, DocKind.DOCX], ids=lambda k: k.value)
def test_noise_stays_below_one_finding_per_five_requirements(kind: DocKind) -> None:
    doc = parse(REQUIREMENTS[kind], kind)
    seg = fallback_segmentation(doc)

    found = lint(doc, seg)

    assert len(found) <= NOISE_LIMIT * len(seg.requirements)


@pytest.mark.parametrize("kind", [DocKind.MD, DocKind.PDF, DocKind.DOCX], ids=lambda k: k.value)
def test_every_quote_is_found_verbatim_in_its_blocks(kind: DocKind) -> None:
    doc = parse(REQUIREMENTS[kind], kind)
    found = lint(doc, fallback_segmentation(doc))

    for finding in found:
        for quote in finding.quotes:
            assert locate_quote(doc, quote, finding.block_ids).match is QuoteMatch.EXACT, finding.cid


def test_quotes_are_exact_on_synthetic_hits_of_every_rule(make_md: MakeMd) -> None:
    text = (
        "# Склад\n\n## 1. Приёмка\n\n## 2. Выдача\n\n## 3. Возвраты\n\n"
        "- **СК-1.** Поиск быстро находит книгу и т. д. при необходимости и/или по маске.\n"
        "- **СК-2.** Срок хранения записей уточняется, а время ожидания не более 30 по заявке.\n"
        "- **СК-4.** Дрон должен взлетать и должен садиться, как описано в разделе 9 и в СК-8.\n"
        "- **СК-4.** Повтор метки у другого требования о складе.\n"
    )
    doc = make_md(text)

    found = lint(doc, fallback_segmentation(doc))

    assert {f.source.rule_id for f in found} == {rule.id for rule in RULES}
    for finding in found:
        for quote in finding.quotes:
            assert locate_quote(doc, quote, finding.block_ids).match is QuoteMatch.EXACT, finding.cid
    assert [h.order for h in run_rules(doc, fallback_segmentation(doc))] == sorted(
        h.order for h in run_rules(doc, fallback_segmentation(doc))
    )


CLEAN_DOCUMENT = """\
# Служба доставки дронами

## 1. Заказ

- **ДР-1.** Клиент оформляет заказ массой от 0,1 до 5 кг; заказ тяжелее отклоняется с кодом ошибки 422.
- **ДР-2.** Система подтверждает заказ в течение 3 секунд после оплаты, 95-й процентиль за час.
- **ДР-3.** Если адрес доставки не заполнен, система возвращает код 400 и не создаёт заказ.
- **ДР-4.** Клиент отменяет заказ до вылета дрона; после вылета отмена не принимается.

## 2. Полёт

- **ДР-5.** Дрон передаёт телеметрию не реже одного раза в 5 секунд и до 3 раз повторяет неудачную передачу.
- **ДР-6.** При заряде батареи менее 20 % дрон возвращается на базу и сообщает диспетчеру код причины.
- **ДР-7.** Диспетчер видит на карте положение каждого дрона с задержкой не более 10 секунд.
- **ДР-8.** Скорость дрона не превышает 15 м/с, высота полёта не превышает 120 м.

## 3. Приёмка

| Условие | Действие | Результат |
|---|---|---|
| Заряд 19 %, полёт идёт | Подождать 10 секунд | Дрон разворачивается на базу |
| Заказ 6 кг | Отправить заказ | Код 422, заказ не создан |

Клиент получает push-уведомление не позднее 15:00 по времени склада; см. раздел 2 и требование ДР-6.
"""


def test_clean_realistic_document_gives_no_findings(make_md: MakeMd) -> None:
    assert findings_of(make_md, CLEAN_DOCUMENT) == []


# ---------------------------------------------------------------- регрессии по ревью кода


@pytest.mark.parametrize(
    "text",
    [
        "- Время отклика склада не более 3 с на каждую заявку.",
        "- Напряжение питания датчика не более 12 В при полной нагрузке.",
        "- Ответ приходит в течение 2 с после запроса к складу.",
    ],
    ids=["секунды «с»", "вольты «В»", "«с» в середине"],
)
def test_l06_accepts_one_letter_units(make_md: MakeMd, text: str) -> None:
    assert "L-06" not in rule_ids(findings_of(make_md, text))


@pytest.mark.parametrize(
    "sentence",
    [
        "Система должна соответствовать IEEE 830, раздел 4.3, при выдаче отчёта клиенту.",
        "Склад действует по условиям из раздела 9.1 приложения к договору поставки.",
    ],
)
def test_l07_ignores_references_to_other_documents(make_md: MakeMd, sentence: str) -> None:
    text = "# Склад\n\n## 1. Приёмка\n\n### 1.1 Накладные\n\n### 1.2 Остатки\n\n" + f"{sentence}\n"

    assert "L-07" not in rule_ids(findings_of(make_md, text))


def test_l10_does_not_count_the_noun_obligation(make_md: MakeMd) -> None:
    text = "- Список обязанностей оператора склада должен быть описан в регламенте."

    assert "L-10" not in rule_ids(findings_of(make_md, text))


@pytest.mark.parametrize(
    "text",
    [
        "3.2.1 Система поиска должна быть удобной для оператора склада.",
        "Система поиска по п. 3.2 должна быть удобной для оператора склада.",
    ],
    ids=["номер пункта в начале", "номер в ссылке"],
)
def test_l01_is_not_silenced_by_a_clause_number(make_md: MakeMd, text: str) -> None:
    assert "L-01" in rule_ids(findings_of(make_md, text))


def test_l08_and_l09_see_labels_in_the_first_table_cell(make_md: MakeMd) -> None:
    text = (
        "| ID | Требование |\n|---|---|\n"
        "| СК-1 | Кладовщик принимает паллету по накладной |\n"
        "| СК-2 | Кладовщик выдаёт паллету по заявке покупателя |\n"
        "| СК-2 | Кладовщик возвращает паллету по накладной |\n"
        "| СК-5 | Кладовщик списывает бракованную паллету |\n"
    )

    ids = rule_ids(findings_of(make_md, text))

    assert {"L-08", "L-09"} <= ids


def test_l07_knows_labels_defined_in_a_table(make_md: MakeMd) -> None:
    text = (
        "- **СК-1.** Кладовщик принимает паллету по накладной.\n"
        "- **СК-2.** Кладовщик выдаёт паллету по заявке, как в СК-10.\n\n"
        "| ID | Требование |\n|---|---|\n"
        "| СК-10 | Кладовщик проверяет остаток перед выдачей паллеты |\n"
    )

    assert "L-07" not in rule_ids(findings_of(make_md, text))


def test_id_quotes_do_not_start_with_the_bullet(make_md: MakeMd) -> None:
    text = (
        "- **СК-1.** Кладовщик принимает паллету по накладной.\n"
        "- **СК-1.** Кладовщик выдаёт паллету по заявке покупателя.\n"
    )

    (finding,) = [f for f in findings_of(make_md, text) if f.source.rule_id == "L-08"]

    assert all(q.startswith(("СК-1", "**СК-1")) for q in finding.quotes)


def test_marker_glued_to_a_word_is_found(make_md: MakeMd) -> None:
    assert "L-05" in rule_ids(findings_of(make_md, "- Срок хранения записей на складе???"))


def test_stem_maximum_does_not_flag_the_plain_adjective(make_md: MakeMd) -> None:
    text = "- Максимальное время хранения паллеты на складе указано в карточке товара."

    assert "L-01" not in rule_ids(findings_of(make_md, text))


def test_lint_is_fast_on_a_large_document(make_md: MakeMd) -> None:
    text = "".join(f"- Кладовщик принимает паллету номер {i} по накладной.\n" for i in range(4000))
    doc = make_md(text)
    seg = fallback_segmentation(doc)

    started = time.perf_counter()
    lint(doc, seg)

    assert time.perf_counter() - started < 3.0
