"""Каталог правил линтера: ID, свойство стандарта, тип дефекта, описание и граница срабатывания.

Здесь только описание правил (данные). Проверки лежат рядом: ``text_rules.py`` (внутри одного предложения
или ячейки) и ``doc_rules.py`` (по всему набору: ID, ссылки, нумерация). Словари слов: ``lexicon.yaml``.

Правила общие: ни одного слова проверяемого документа (CLAUDE.md, правила 2 и 3). Важность правило не
ставит: её считает верификатор (#22) по таблице «тип дефекта × мешает ли тесту».
"""

from __future__ import annotations

from dataclasses import dataclass

from validator.models import DefectType, Property, Scope, Verdict


@dataclass(frozen=True)
class Rule:
    """Одно правило: что ищет, с каким свойством стандарта и типом дефекта это связано."""

    id: str
    name: str
    criterion: Property
    defect_type: DefectType
    description: str
    threshold: str
    blocks_testing: bool = False
    verdict: Verdict = Verdict.VIOLATION
    merges_with: frozenset[Property] = frozenset()
    """Свойства вне группы ``criterion``, под которыми проходы модели описывают тот же дефект (#124).

    Срабатывание на той же или вложенной цитате присоединяется к такой находке модели как подтверждение,
    а не идёт в отчёт отдельной находкой. Группы свойств: ``merge.PROPERTY_GROUPS``."""

    @property
    def scope(self) -> Scope:
        return self.criterion.scope

    @property
    def standard(self) -> tuple[str, ...]:
        """Пункты стандартов (ISO/IEC/IEEE 29148:2018, IEEE 830-1998) для свойства правила."""
        return self.criterion.refs


L01 = Rule(
    id="L-01",
    name="Размытое слово",
    criterion=Property.VERIFIABLE,
    defect_type=DefectType.UNVERIFIABLE,
    description=(
        "Слово из словаря размытых слов (субъективная оценка, величина без числа, время без меры: "
        "«удобно», «быстро», «несколько», «мгновенно»). По такому слову нельзя написать проверку."
    ),
    threshold="слово из словаря, числа в том же предложении или ячейке нет; слова в кавычках пропускаем",
    blocks_testing=True,
)
L02 = Rule(
    id="L-02",
    name="Лазейка без критерия",
    criterion=Property.UNAMBIGUOUS,
    defect_type=DefectType.AMBIGUOUS,
    description=(
        "Оборот, который оставляет условие или обязательность на усмотрение исполнителя: "
        "«при необходимости», «по возможности», «желательно», «по усмотрению»."
    ),
    threshold=(
        "оборот из словаря; слова в кавычках пропускаем; пожелание в начале фразы («Рекомендуется …») и "
        "выбор, отданный пользователю («по усмотрению пользователя»), лазейкой не считаем"
    ),
)
L03 = Rule(
    id="L-03",
    name="Открытый перечень",
    criterion=Property.COMPLETE,
    defect_type=DefectType.INCOMPLETE,
    description=(
        "Перечень обрывается словами «и т. д.», «и т. п.», «и так далее», «не ограничиваясь»: "
        "остальные элементы не названы, разработчик и тестировщик их не знают."
    ),
    threshold="оборот из словаря; слова в кавычках пропускаем",
    # Открытый перечень модель называет и неполнотой, и неоднозначностью («что входит в „и далее“?»).
    merges_with=frozenset({Property.UNAMBIGUOUS}),
)
L04 = Rule(
    id="L-04",
    name="«И/или»",
    criterion=Property.UNAMBIGUOUS,
    defect_type=DefectType.AMBIGUOUS,
    description=(
        "«И/или» допускает три чтения (первое, второе, оба) и столько же разных тестов; "
        "нужно сказать, какое имеется в виду."
    ),
    threshold="одно вхождение; слова в кавычках пропускаем",
)
L05 = Rule(
    id="L-05",
    name="Маркер незавершённости",
    criterion=Property.COMPLETE,
    defect_type=DefectType.PLACEHOLDER,
    description=(
        "В тексте остался маркер недописанного: «TBD», «TODO», «???», «уточняется», «будет определено», "
        "заготовка в квадратных скобках. Требование нельзя ни реализовать, ни проверить (IEEE 830, п. 4.3.3)."
    ),
    threshold="маркер из словаря; кавычки не мешают",
    blocks_testing=True,
)
L06 = Rule(
    id="L-06",
    name="Число без единицы",
    criterion=Property.UNAMBIGUOUS,
    defect_type=DefectType.AMBIGUOUS,
    description=(
        "Предел («не более», «в течение», «до», «свыше») со числом, после которого нет единицы измерения "
        "или названия того, что считаем. Тест не знает, секунды это, минуты или штуки."
    ),
    threshold=(
        "слово-предел, число, а дальше конец фразы, знак препинания или служебное слово; молчим, если "
        "перед пределом назван счёт («количество …»); у размера «N на M» единицу ищем после последнего "
        "числа, у фразы, разрезанной переносом страницы, в продолжении того же требования"
    ),
    blocks_testing=True,
)
L07 = Rule(
    id="L-07",
    name="Ссылка на несуществующее",
    criterion=Property.TRACEABLE,
    defect_type=DefectType.TRACEABILITY,
    description=(
        "Ссылка на раздел («см. раздел 4.2», «раздел «Название»») или на требование по метке "
        "(«см. FR-12»), которых в документе нет."
    ),
    threshold=(
        "в документе не меньше 2 пронумерованных заголовков (для названий: 3 заголовка) или не меньше "
        "2 меток требований с одним префиксом"
    ),
)
L08 = Rule(
    id="L-08",
    name="Повтор ID требования",
    criterion=Property.SET_TRACEABLE,
    defect_type=DefectType.TRACEABILITY,
    description=(
        "Одна и та же метка стоит у двух разных требований. Ссылка на неё неоднозначна, "
        "трассировка на тесты ломается."
    ),
    threshold="метка встречается у 2 и более разных блоков внутри требований",
)
L09 = Rule(
    id="L-09",
    name="Пропуск в нумерации",
    criterion=Property.SET_TRACEABLE,
    defect_type=DefectType.TRACEABILITY,
    description=(
        "В ряду меток с одним префиксом («ABC-1», «ABC-2», «ABC-4») нет одного из номеров: "
        "требование потеряно или удалено без следа."
    ),
    threshold="ряд из 3 и более меток, нумерация с 0 или 1, пропущено не больше 3 номеров",
    verdict=Verdict.UNCLEAR,
)
L10 = Rule(
    id="L-10",
    name="Несколько обязательств в одном предложении",
    criterion=Property.SINGULAR,
    defect_type=DefectType.NOT_ATOMIC,
    description=(
        "В одном предложении два и больше «должен» («shall», «must»): кандидат на составное требование, "
        "которое нужно разрезать на отдельные, чтобы каждое проверялось своим тестом."
    ),
    threshold="2 и более глагола обязательства в одном предложении или ячейке",
)

RULES: tuple[Rule, ...] = (L01, L02, L03, L04, L05, L06, L07, L08, L09, L10)
RULE_BY_ID: dict[str, Rule] = {rule.id: rule for rule in RULES}

__all__ = ["RULES", "RULE_BY_ID", "Rule"]
