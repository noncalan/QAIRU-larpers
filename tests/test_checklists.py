"""Доменные чек-листы с триггерами (#35): формат файлов, выбор по документу, промпт прохода, отчёт."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import REQUIREMENTS
from validator.checklists import (
    CHECKLISTS_DIR,
    Checklist,
    item_id,
    load_checklists,
    render_section,
    select_checklists,
)
from validator.config import load_config
from validator.llm import FakeLLM
from validator.models import (
    Backend,
    ChecklistUse,
    DocKind,
    FindingSource,
    LLMStep,
    ParsedDocument,
    Property,
    QuestionOrigin,
    RunConfig,
    SourceKind,
)
from validator.prompting import fill, load_prompt
from validator.search import SEARCH_USER, search, search_prompt
from validator.search_parse import draft_from_raw, split_findings

# Документ без денег, дат, API и уведомлений: читальный зал библиотеки.
NO_DOMAIN = """# Читальный зал

Читатель берёт книгу с полки сам.

Книги стоят по алфавиту авторов.

Справочники из зала не выносят.
"""

# Деньги в двух блоках: чек-лист «Деньги и суммы» должен включиться.
WITH_MONEY = """# Оплата заказа

Клиент оплачивает заказ картой.

Сумма заказа не больше 500 000 KZT, комиссия 1 %.
"""

# Деньги только в одном блоке: случайное слово не включает чек-лист.
ONE_MONEY_BLOCK = """# Каталог

Каталог показывает книги по жанрам.

Цена книги видна на карточке, стоимость доставки считается отдельно.
"""


def _uses(doc: ParsedDocument, **kwargs: bool) -> dict[str, ChecklistUse]:
    return {u.id: u for u in select_checklists(doc, **kwargs).uses}


def _config(checklists: bool = True) -> RunConfig:
    return load_config(
        REQUIREMENTS[DocKind.MD],
        Path("out/report.md"),
        backend=Backend.FAKE,
        passes=1,
        checklists=checklists,
    )


# ---------------------------------------------------------------- файлы чек-листов


def test_four_to_six_checklists_with_unique_items_and_known_properties() -> None:
    lists = load_checklists()

    assert 4 <= len(lists) <= 6
    ids = [i.id for c in lists for i in c.items]
    assert len(ids) == len(set(ids))
    assert all(isinstance(i.property, Property) for c in lists for i in c.items)
    assert all(c.triggers.min_blocks >= 2 for c in lists), "одно случайное слово не должно включать чек-лист"


def test_every_yaml_in_folder_is_loaded() -> None:
    files = sorted(CHECKLISTS_DIR.glob("*.yaml"))

    assert len(files) == len(load_checklists())


def test_broken_checklist_file_is_installation_error(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("id: X\ntitle: t\n", encoding="utf-8")

    with pytest.raises(Exception, match="не по формату"):
        load_checklists(tmp_path)


def test_item_id_must_start_with_checklist_id() -> None:
    with pytest.raises(ValueError, match="не начинаются"):
        Checklist.model_validate(
            {
                "id": "AAA",
                "title": "t",
                "triggers": {"patterns": ["x"]},
                "items": [{"id": "BBB-01", "property": "IND-COMPL", "question": "q" * 12, "why": "w" * 12}],
            }
        )


# ---------------------------------------------------------------- выбор по документу


def test_money_checklist_is_off_for_document_without_money(make_md: Callable[[str], ParsedDocument]) -> None:
    uses = _uses(make_md(NO_DOMAIN))

    assert not uses["MON"].used
    assert not any(u.used for u in uses.values())
    assert uses["MON"].matched_blocks == 0


def test_money_checklist_is_on_when_money_words_in_two_blocks(make_md: Callable[[str], ParsedDocument]) -> None:
    sel = select_checklists(make_md(WITH_MONEY))
    uses = {u.id: u for u in sel.uses}

    assert uses["MON"].used
    assert uses["MON"].matched_blocks >= 2
    assert "kzt" in uses["MON"].triggers
    assert "MON" in {c.id for c in sel.active}
    assert "MON-01" in sel.item_ids


def test_one_block_with_money_words_does_not_switch_checklist_on(
    make_md: Callable[[str], ParsedDocument],
) -> None:
    uses = _uses(make_md(ONE_MONEY_BLOCK))

    assert uses["MON"].matched_blocks == 1
    assert not uses["MON"].triggered


def test_triggers_show_whole_words_not_stems(make_md: Callable[[str], ParsedDocument]) -> None:
    uses = _uses(make_md(WITH_MONEY))

    assert "сумма" in uses["MON"].triggers


def test_disabled_checklists_still_report_triggers_but_send_nothing(
    make_md: Callable[[str], ParsedDocument],
) -> None:
    sel = select_checklists(make_md(WITH_MONEY), enabled=False)
    uses = {u.id: u for u in sel.uses}

    assert uses["MON"].triggered
    assert not uses["MON"].used
    assert sel.active == ()


# ---------------------------------------------------------------- промпт прохода


def test_section_lists_items_with_property_and_is_empty_without_checklists() -> None:
    money = next(c for c in load_checklists() if c.id == "MON")

    section = render_section([money])

    assert render_section([]) == ""
    assert "MON-01" in section and "MON-06" in section
    assert "Consistent (согласованность)" in section
    assert "Чек-лист: <ID пункта>" in section
    assert "{" not in section, "все слоты шаблона подставлены"


def test_prompt_without_checklists_is_the_same_as_before() -> None:
    document = "[B001] Текст"

    assert search_prompt(document) == fill(load_prompt(SEARCH_USER), document=document)
    assert search_prompt(document, "## Доменные чек-листы").endswith("## Доменные чек-листы\n")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("MON-02", "MON-02"), ("mon-2", "MON-02"), ("MON-02, DT-01", "MON-02"), ("XYZ-01", None), ("нет", None)],
)
def test_item_id_accepts_only_items_from_prompt(value: str, expected: str | None) -> None:
    assert item_id(value, frozenset({"MON-02", "DT-01"})) == expected


def test_checklist_line_is_its_own_field_and_does_not_glue_to_previous() -> None:
    text = (
        "## Находки\n\n#### Н-1. Нет валюты\n- Блоки: B002\n- Цитата: «Сумма заказа»\n"
        "- Свойство: Consistent (согласованность)\n- Тип дефекта: неполнота\n- Чек-лист: MON-01\n"
        "- Вердикт: нарушение\n- Как исправить: «Сумма в KZT»"
    )

    [raw] = split_findings(text).findings
    draft = draft_from_raw(raw)

    assert not isinstance(draft, list)
    assert draft.checklist == "MON-01"
    assert draft.suggestion == "Сумма в KZT"


def _answer(quote: str, checklist: str) -> str:
    return (
        "## Обзор документа\n\n- заметки\n\n## Находки\n\n### Набор требований\n\n"
        f"#### Н-1. Нет валюты суммы\n- Блоки: B002\n- Цитата: «{quote}»\n"
        "- Свойство: Complete (полнота набора)\n- Тип дефекта: неполнота\n"
        f"- Чек-лист: {checklist}\n- Вердикт: нарушение\n- Почему мешает: Нельзя посчитать.\n"
        "- Тест: мешает, нет валюты\n- Важность: средне\n"
    )


@pytest.mark.parametrize(("tag", "expected"), [("MON-01", "MON-01"), ("API-01", None)])
def test_search_sends_checklists_and_marks_finding_with_known_item(
    make_md: Callable[[str], ParsedDocument], tag: str, expected: str | None
) -> None:
    doc = make_md(WITH_MONEY)
    active = select_checklists(doc).active
    llm = FakeLLM(Path("/nonexistent"), {(LLMStep.SEARCH, 1): _envelope(_answer("Сумма заказа", tag))})

    [result] = search(doc, llm, _config(), active)

    [req] = llm.requests
    assert "## Доменные чек-листы" in req.prompt and "MON-01" in req.prompt
    assert req.prompt.index("</документ>") < req.prompt.index("## Доменные чек-листы")
    [cand] = result.candidates
    assert cand.source.kind is SourceKind.PASS
    assert cand.source.checklist_item == expected  # API не включён: его пункт код не принимает


def test_search_without_active_checklists_ignores_checklist_line(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md(WITH_MONEY)
    llm = FakeLLM(Path("/nonexistent"), {(LLMStep.SEARCH, 1): _envelope(_answer("Сумма заказа", "MON-01"))})

    [result] = search(doc, llm, _config(checklists=False))

    assert "Доменные чек-листы" not in llm.requests[0].prompt
    assert result.candidates[0].source.checklist_item is None


def _envelope(text: str) -> dict[str, object]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text}


# ---------------------------------------------------------------- источник находки и вопросы


def test_source_label_names_checklist_item() -> None:
    src = FindingSource(kind=SourceKind.PASS, pass_index=1, checklist_item="MON-02")

    assert src.label == "проход 1 (чек-лист MON-02)"


def test_finding_marked_by_checklist_gives_checklist_question_origin() -> None:
    from report_factory import findings

    from validator.questions.candidates import finding_origins

    marked = findings()[0]  # F-01: один проход отметил пунктом DT-04, в правке есть [уточнить]
    plain = marked.model_copy(update={"sources": [FindingSource(kind=SourceKind.PASS, pass_index=1)]})

    assert QuestionOrigin.CHECKLIST in finding_origins(marked)
    assert QuestionOrigin.CHECKLIST not in finding_origins(plain)
    assert QuestionOrigin.FINDING in finding_origins(plain)


def test_cli_config_switches_checklists_off() -> None:
    assert _config().checklists is True
    assert _config(checklists=False).checklists is False


def test_origin_order_knows_checklist() -> None:
    from validator.questions.candidates import ORIGIN_ORDER

    assert QuestionOrigin.CHECKLIST in ORIGIN_ORDER
