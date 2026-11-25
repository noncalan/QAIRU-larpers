"""Шаг 4 (#18): проходы поиска, разбор кодом, перенос в JSON моделью, отказ прохода «K−1 из K»."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from conftest import FIXTURES_LLM, REQUIREMENTS
from validator import search as search_module
from validator.config import load_config
from validator.errors import LLMError
from validator.llm import FakeLLM, RecordingClient, parse_cli_payload
from validator.models import (
    Backend,
    DefectType,
    DiagLevel,
    DocKind,
    Effort,
    ExitCode,
    LLMRequest,
    LLMResponse,
    LLMStep,
    ParsedDocument,
    PassResult,
    Profile,
    Property,
    RunConfig,
    Scope,
    SourceKind,
    Verdict,
)
from validator.pipeline import run
from validator.prompting import PROMPTS_DIR, fill, load_prompt
from validator.search import MAX_PARALLEL_PASSES, search
from validator.search_parse import Draft, draft_from_raw, split_findings
from validator.search_transfer import SEVERITY_LABELS, TRANSFER_SCHEMA, Fragment, transferred
from validator.text import locate_quote

LIBRARY = """# Библиотека

## Выдача книг

Книга выдаётся на срок, достаточный для прочтения.

Читатель может держать на руках не более пяти книг. Выдача оформляется в читальном зале.

## Лимиты

| Параметр | Значение |
|---|---|
| Максимум выдачи | 3 книги |
"""

Responses = Mapping[tuple[LLMStep, int], Mapping[str, Any]]


def config(passes: int = 3, profile: Profile = Profile.STANDARD) -> RunConfig:
    return load_config(
        REQUIREMENTS[DocKind.MD], Path("out/report.md"), profile=profile, backend=Backend.FAKE, passes=passes
    )


def envelope(text: str = "", structured: object = None, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        **extra,
    }
    if structured is not None:
        data["structured_output"] = structured
    return data


def failure(message: str = "Claude AI usage limit reached") -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": True, "result": message}


def bid(doc: ParsedDocument, fragment: str) -> str:
    """ID блока с этим текстом: тесты не зависят от нумерации блоков разбора."""
    return next(b.id for b in doc.blocks if fragment in b.text)


def finding(n: int, *, blocks: str, quote: str | None, prop: str = "Verifiable (проверяемость)") -> str:
    lines = [f"#### Н-{n}. Находка {n}", f"- Блоки: {blocks}"]
    if quote is not None:
        lines.append(f"- Цитата: «{quote}»")
    lines += [
        f"- Свойство: {prop}",
        "- Тип дефекта: непроверяемость",
        "- Вердикт: нарушение",
        "- Почему мешает: Нельзя написать тест.",
        "- Тест: мешает, нет числа",
        "- Важность: средне",
    ]
    return "\n".join(lines)


def answer(*findings: str) -> str:
    return "## Обзор документа\n\n- заметки\n\n## Находки\n\n### Отдельные требования\n\n" + "\n\n".join(
        findings
    )


def record(
    n: int, *, blocks: list[str], quotes: list[str], fragment: int = 1, **extra: Any
) -> dict[str, Any]:
    """Запись ответа переноса в JSON по ``TRANSFER_SCHEMA``: находка ``n`` из фрагмента ``fragment``."""
    return {
        "fragment": fragment,
        "title": f"Находка {n}",
        "block_ids": blocks,
        "quotes": quotes,
        "property": "IND-VERIF",
        "defect_type": "непроверяемость",
        "verdict": "нарушение",
        "explanation": "Нельзя написать тест.",
        "blocks_testing": True,
        "test_gap": "нет числа",
        "severity": "средне",
        **extra,
    }


@pytest.fixture
def library(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(LIBRARY)


def run_search(
    doc: ParsedDocument, responses: Responses, passes: int = 1
) -> tuple[list[PassResult], FakeLLM]:
    llm = FakeLLM(Path("/nonexistent"), responses)
    return search(doc, llm, config(passes)), llm


# ---------------------------------------------------------------- проходы по фикстурам


def test_three_passes_from_fixtures_give_candidates_with_sources(requirements_md: ParsedDocument) -> None:
    llm = FakeLLM(FIXTURES_LLM)

    passes = search(requirements_md, llm, config(3))

    assert [p.index for p in passes] == [1, 2, 3]
    assert all(p.ok and p.candidates and "## Находки" in p.raw_text for p in passes)
    assert [p.call_ids for p in passes] == [["search.1"], ["search.2"], ["search.3"]]
    for p in passes:
        assert all(c.cid.startswith(f"p{p.index}.") for c in p.candidates)
        assert {(c.source.kind, c.source.pass_index, c.source.call_id) for c in p.candidates} == {
            (SourceKind.PASS, p.index, f"search.{p.index}")
        }
        assert all(c.title and c.explanation for c in p.candidates)
    found = [c for p in passes for c in p.candidates]
    assert any(c.scenario for c in found) and any(c.test_gap for c in found)
    assert {c.scope for c in found} == {Scope.REQUIREMENT, Scope.SET}
    assert all(r.step is LLMStep.SEARCH for r in llm.requests), "все находки разобраны кодом, переноса нет"


def test_pass_request_is_prompt_14_free_text(requirements_md: ParsedDocument) -> None:
    llm = FakeLLM(FIXTURES_LLM)

    search(requirements_md, llm, config(1))

    [req] = llm.requests
    assert req.system == (PROMPTS_DIR / "search_system.md").read_text(encoding="utf-8")
    assert "{document}" not in req.prompt
    assert "<документ>\n[B001]" in req.prompt
    assert req.json_schema is None
    assert (req.model, req.effort, req.timeout_s) == (
        "claude-opus-5-5",
        Effort.MEDIUM,
        config(1).call_timeout_s,
    )


def test_submit_profile_searches_on_xhigh(requirements_md: ParsedDocument) -> None:
    llm = FakeLLM(FIXTURES_LLM)

    search(requirements_md, llm, config(1, Profile.SUBMIT))

    assert llm.requests[0].effort is Effort.XHIGH


def test_document_braces_are_not_template_slots(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md("# Шаблон\n\nПоле {findings} и {document} остаются как в документе.\n")
    llm = FakeLLM(Path("/nonexistent"), {(LLMStep.SEARCH, 1): envelope(answer())})

    search(doc, llm, config(1))

    assert "Поле {findings} и {document} остаются" in llm.requests[0].prompt


# ---------------------------------------------------------------- отказ проходов


def test_failed_pass_gives_k_minus_one_of_k(requirements_md: ParsedDocument) -> None:
    llm = FakeLLM(FIXTURES_LLM, {(LLMStep.SEARCH, 2): failure()})

    passes = search(requirements_md, llm, config(3))

    assert [p.ok for p in passes] == [True, False, True]
    failed = passes[1]
    assert failed.candidates == [] and failed.error
    [diag] = failed.diagnostics
    assert diag.code == "LLM-030" and diag.level is DiagLevel.WARNING
    assert diag.message.startswith("Проход 2 из 3 не удался: модель вернула ошибку")
    assert diag.action == "Находки собраны по 2 из 3 проходов"
    assert diag.hint, "подсказка про лимит подписки дошла до человека"


def test_all_passes_failed_gives_no_candidates(requirements_md: ParsedDocument) -> None:
    passes, _ = run_search(requirements_md, {}, passes=2)

    assert [p.ok for p in passes] == [False, False]
    assert all(p.diagnostics[-1].action == "Находок модели в отчёте нет" for p in passes)


def test_pipeline_continues_when_all_passes_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "fixtures"
    empty.mkdir()
    monkeypatch.setenv("VALIDATOR_FAKE_FIXTURES", str(empty))
    cfg = load_config(
        REQUIREMENTS[DocKind.MD], tmp_path / "out" / "report.md", backend=Backend.FAKE, passes=2
    )

    outcome = run(cfg)

    assert outcome.exit_code is ExitCode.MODEL
    codes = [d.code for d in outcome.diagnostics]
    assert codes.count("LLM-030") == 2 and "LLM-031" in codes
    assert outcome.report_path is not None and outcome.report_path.is_file()


def test_empty_answer_fails_the_pass(library: ParsedDocument) -> None:
    passes, _ = run_search(library, {(LLMStep.SEARCH, 1): envelope("   ")})

    assert not passes[0].ok
    assert "пустой ответ" in passes[0].diagnostics[-1].message


def test_bug_on_one_answer_loses_only_that_pass(
    requirements_md: ParsedDocument, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Ошибка кода разбора на одном ответе: проход упал с INT-002, остальные целы, трассировка в лог."""
    real = search_module.split_findings

    def broken_on_second(text: str) -> object:
        if "Описание метода называет и сохранение, и чтение" in text and "**Блоки:**" in text:
            raise ZeroDivisionError
        return real(text)

    monkeypatch.setattr(search_module, "split_findings", broken_on_second)

    passes = search(requirements_md, FakeLLM(FIXTURES_LLM), config(3))

    assert [p.ok for p in passes] == [True, False, True]
    failed = passes[1]
    assert "**Блоки:**" in failed.raw_text and failed.call_ids == ["search.2"], "ответ для разбора сбоя цел"
    diag = failed.diagnostics[-1]
    assert diag.code == "INT-002" and diag.level is DiagLevel.WARNING
    assert "ZeroDivisionError" in diag.message and diag.action == "Находки собраны по 2 из 3 проходов"
    assert "ZeroDivisionError" in caplog.text


# ---------------------------------------------------------------- параллельность


class SlowClient:
    """Клиент, который отвечает с задержкой и считает одновременные вызовы."""

    backend = Backend.CLI

    def __init__(self, text: str) -> None:
        self.text = text
        self.active = 0
        self.peak = 0
        self.lock = threading.Lock()

    def call(self, req: LLMRequest) -> LLMResponse:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        return LLMResponse(call_id=req.call_id, text=self.text, backend=Backend.CLI)


def test_passes_run_in_parallel_but_not_more_than_three(library: ParsedDocument) -> None:
    client = SlowClient(answer(finding(1, blocks="B003", quote="Книга выдаётся")))

    passes = search(library, client, config(7))

    assert client.peak == MAX_PARALLEL_PASSES == 3
    assert [p.index for p in passes] == list(range(1, 8))
    assert all(p.ok and len(p.candidates) == 1 for p in passes)


# ---------------------------------------------------------------- перенос в JSON моделью


def test_unparsed_finding_is_transferred_by_model_in_place(library: ParsedDocument) -> None:
    first = bid(library, "Книга выдаётся")
    hands = bid(library, "пяти книг")
    text = answer(
        finding(1, blocks=first, quote="Книга выдаётся на срок, достаточный для прочтения"),
        finding(2, blocks=hands, quote=None),
        finding(3, blocks=first, quote="достаточный для прочтения"),
    )
    moved = record(2, blocks=[f"{hands}.1"], quotes=["Читатель может держать на руках не более пяти книг"])
    responses = {
        (LLMStep.SEARCH, 1): envelope(text),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(structured={"findings": [moved]}),
    }

    [result], llm = run_search(library, responses)

    assert result.ok
    assert [c.title for c in result.candidates] == ["Находка 1", "Находка 2", "Находка 3"]
    assert [c.cid for c in result.candidates] == ["p1.001", "p1.002", "p1.003"]
    assert result.candidates[1].block_ids == [f"{hands}.1"]
    assert result.call_ids == ["search.1", "search_transfer.1"]
    transfer = llm.requests[1]
    assert (transfer.model, transfer.effort, transfer.json_schema) == (
        "claude-haiku-5-5",
        Effort.MEDIUM,
        TRANSFER_SCHEMA,
    )
    assert '<фрагмент номер="1">\n#### Н-2. Находка 2' in transfer.prompt
    assert "Н-1. Находка 1" not in transfer.prompt and "Н-3." not in transfer.prompt
    [diag] = result.diagnostics
    assert diag.code == "LLM-032" and diag.level is DiagLevel.INFO
    assert "нет цитаты: 1" in diag.message and "claude-haiku-5-5" in diag.action


def test_unknown_block_ids_are_dropped_with_diagnostic(library: ParsedDocument) -> None:
    real = bid(library, "Книга выдаётся")
    text = answer(finding(1, blocks=f"{real}, B999, {real}.7", quote="Книга выдаётся"))

    [result], _ = run_search(library, {(LLMStep.SEARCH, 1): envelope(text)})

    assert result.candidates[0].block_ids == [real]
    [diag] = result.diagnostics
    assert diag.code == "LLM-034" and diag.level is DiagLevel.INFO
    assert "B999" in diag.message and f"{real}.7" in diag.message


def test_transfer_unknown_block_ids_are_dropped_too(library: ParsedDocument) -> None:
    responses = {
        (LLMStep.SEARCH, 1): envelope(answer(finding(1, blocks="B003", quote=None))),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(
            structured={"findings": [record(1, blocks=["B998"], quotes=["Книга выдаётся"])]}
        ),
    }

    [result], _ = run_search(library, responses)

    assert result.candidates[0].block_ids == []
    assert [d.code for d in result.diagnostics] == ["LLM-032", "LLM-034"]


def test_unusable_transfer_is_retried_then_pass_fails(library: ParsedDocument) -> None:
    """Ответ по схеме, но без годных записей: шаг повторяет перенос один раз, затем проход падает."""
    blank = {"findings": [record(1, blocks=[], quotes=["  "])]}
    responses = {
        (LLMStep.SEARCH, 1): envelope(answer(finding(1, blocks="B003", quote=None))),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(structured=blank),
        (LLMStep.SEARCH_TRANSFER, 2): envelope(structured=blank),
    }

    [result], _ = run_search(library, responses)

    assert not result.ok
    assert result.call_ids == ["search.1", "search_transfer.1", "search_transfer.2"]
    assert "перенос в JSON не удался" in result.diagnostics[-1].message
    assert result.raw_text, "сырой текст прохода сохранён и для упавшего прохода"


def test_transfer_retry_can_succeed(library: ParsedDocument) -> None:
    """Пустой список, хотя во фрагменте находка «Н-1»: перенос повторяется, повтор её возвращает."""
    responses = {
        (LLMStep.SEARCH, 1): envelope(answer(finding(1, blocks="B003", quote=None))),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(structured={"findings": []}),
        (LLMStep.SEARCH_TRANSFER, 2): envelope(
            structured={"findings": [record(1, blocks=[], quotes=["Книга"])]}
        ),
    }

    [result], _ = run_search(library, responses)

    assert result.ok and len(result.candidates) == 1
    assert result.call_ids == ["search.1", "search_transfer.1", "search_transfer.2"]


@pytest.mark.parametrize("bad", [envelope("это не JSON"), failure("Not logged in · Please run /login")])
def test_failed_transfer_call_is_not_repeated_by_the_step(
    library: ParsedDocument, bad: dict[str, Any]
) -> None:
    """Сбой вызова шаг не повторяет: временный повторит клиент модели, а вход и лимит повтор не лечит."""
    responses = {
        (LLMStep.SEARCH, 1): envelope(answer(finding(1, blocks="B003", quote=None))),
        (LLMStep.SEARCH_TRANSFER, 1): bad,
    }

    [result], _ = run_search(library, responses)

    assert not result.ok
    assert result.call_ids == ["search.1", "search_transfer.1"]
    assert "перенос в JSON не удался" in result.diagnostics[-1].message


class CliStub:
    """Живой бэкенд без сети: отдаёт записанные ответы ``claude -p`` и считает вызовы."""

    backend = Backend.CLI

    def __init__(self, responses: Responses) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def call(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req.call_id)
        return parse_cli_payload(self.responses[(req.step, req.call_index)], req, Backend.CLI)


def test_live_transfer_with_bad_json_is_retried_once_by_the_client(library: ParsedDocument) -> None:
    """Невалидный JSON переноса на живом бэкенде: один повтор (клиент модели), затем отказ прохода."""
    stub = CliStub(
        {
            (LLMStep.SEARCH, 1): envelope(answer(finding(1, blocks="B003", quote=None))),
            (LLMStep.SEARCH_TRANSFER, 1): envelope("{обрыв"),
        }
    )

    [result] = search(library, RecordingClient(stub, retry_delay_s=0), config(1))

    assert stub.calls == ["search.1", "search_transfer.1", "search_transfer.1"]
    assert not result.ok and "перенос в JSON не удался" in result.diagnostics[-1].message


def test_lost_transfer_findings_are_a_warning(library: ParsedDocument) -> None:
    text = answer(
        finding(1, blocks="B003", quote="Книга выдаётся"),
        finding(2, blocks="B003", quote=None),
        finding(3, blocks="B004", quote=None),
    )
    responses = {
        (LLMStep.SEARCH, 1): envelope(text),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(
            structured={
                "findings": [record(2, blocks=[], quotes=["Книга"]), record(9, blocks=[], quotes=[" "])]
            }
        ),
    }

    [result], _ = run_search(library, responses)

    assert result.ok and [c.title for c in result.candidates] == ["Находка 1", "Находка 2"]
    [diag] = result.diagnostics
    assert diag.code == "LLM-033" and diag.level is DiagLevel.WARNING
    assert "по находкам Н-3 перенос не вернул записей" in diag.message
    assert "записей не по схеме: 1" in diag.message


def test_transfer_records_return_to_their_fragment(library: ParsedDocument) -> None:
    """Запись встаёт на место своего фрагмента и берёт его подраздел, даже если номера «Н-k» совпали."""
    hands = bid(library, "пяти книг")
    text = (
        answer(finding(3, blocks="B003", quote=None), finding(4, blocks="B003", quote="Книга выдаётся"))
        + "\n\n### Набор требований\n\n**3) Лимит выдачи задан дважды**: «не более пяти книг» и «3 книги».\n"
    )
    responses = {
        (LLMStep.SEARCH, 1): envelope(text),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(
            structured={
                "findings": [
                    record(3, blocks=[hands], quotes=["не более пяти книг", "3 книги"], fragment=2),
                    record(3, blocks=[], quotes=["Книга"], fragment=1),
                ]
            }
        ),
    }

    [result], llm = run_search(library, responses)

    assert [(c.title, c.scope) for c in result.candidates] == [
        ("Находка 3", Scope.REQUIREMENT),
        ("Находка 4", Scope.REQUIREMENT),
        ("Находка 3", Scope.SET),
    ]
    assert result.candidates[2].quotes == ["не более пяти книг", "3 книги"]
    assert '<фрагмент номер="2">\n### Набор' not in llm.requests[1].prompt
    assert '<фрагмент номер="2">\n**3) Лимит выдачи' in llm.requests[1].prompt


def test_table_in_findings_section_goes_to_transfer(library: ParsedDocument) -> None:
    """Находки таблицей вместо формата промпта: код не молчит с нулём находок, таблицу переносит модель."""
    table = (
        "| Н | Блок | Цитата | Свойство |\n|---|---|---|---|\n"
        "| 1 | B003 | «достаточный для прочтения» | Verifiable |\n"
    )
    responses = {
        (LLMStep.SEARCH, 1): envelope("## Обзор документа\n\n- заметки\n\n## Находки\n\n" + table),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(
            structured={"findings": [record(1, blocks=["B003"], quotes=["достаточный для прочтения"])]}
        ),
    }

    [result], llm = run_search(library, responses)

    assert result.ok and len(result.candidates) == 1
    assert "«достаточный для прочтения»" in llm.requests[1].prompt
    assert "не в формате находки: 1" in result.diagnostics[0].message


def test_failed_transfer_keeps_findings_parsed_by_code(library: ParsedDocument) -> None:
    text = answer(finding(1, blocks="B003", quote="Книга выдаётся"), finding(2, blocks="B004", quote=None))

    [result], _ = run_search(library, {(LLMStep.SEARCH, 1): envelope(text)})

    assert result.ok
    assert [c.title for c in result.candidates] == ["Находка 1"]
    [diag] = result.diagnostics
    assert diag.code == "LLM-033" and diag.level is DiagLevel.WARNING
    assert diag.message.startswith("Проход 1: 1 из 2 находок не разобраны кодом (нет цитаты: 1)")


def test_answer_without_format_is_transferred_whole(library: ParsedDocument) -> None:
    text = "Нашёл одну проблему: срок выдачи «достаточный для прочтения» нельзя проверить."
    responses = {
        (LLMStep.SEARCH, 1): envelope(text),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(
            structured={"findings": [record(1, blocks=[], quotes=["достаточный для прочтения"])]}
        ),
    }

    [result], llm = run_search(library, responses)

    assert result.ok and len(result.candidates) == 1
    assert text in llm.requests[1].prompt
    assert result.diagnostics[0].message == "Проход 1: ответ не в формате промпта"


def test_answer_without_format_and_findings_fails_the_pass(library: ParsedDocument) -> None:
    responses = {
        (LLMStep.SEARCH, 1): envelope("Не могу помочь с этим."),
        (LLMStep.SEARCH_TRANSFER, 1): envelope(structured={"findings": []}),
    }

    [result], _ = run_search(library, responses)

    assert not result.ok
    assert "находок в нём нет" in result.diagnostics[-1].message


def test_empty_findings_section_is_a_successful_pass_without_transfer(library: ParsedDocument) -> None:
    text = "## Обзор документа\n\n- заметки\n\n## Находки\n\nДефектов не найдено.\n"

    [result], llm = run_search(library, {(LLMStep.SEARCH, 1): envelope(text)})

    assert result.ok and result.candidates == [] and result.diagnostics == []
    assert len(llm.requests) == 1


# ---------------------------------------------------------------- ответ переноса и промпт


def test_transfer_records_off_schema_are_skipped_and_counted() -> None:
    items = [
        record(1, blocks=[], quotes=["Книга"]),
        record(2, blocks=[], quotes=["  "]),
        record(3, blocks=[], quotes=["Книга"], property="IND-WHAT"),
        "не объект",
    ]

    moved = transferred({"findings": items}, [], "search_transfer.1")

    assert moved.invalid == 3
    [(position, draft)] = moved.drafts
    assert draft.property is Property.VERIFIABLE
    assert draft.defect_type is DefectType.UNVERIFIABLE
    assert draft.verdict is Verdict.VIOLATION
    assert draft.test_gap == "нет числа"
    assert position >= 1_000_000, "запись без своего фрагмента идёт после разобранных кодом"


def test_transfer_where_nothing_fits_schema_is_an_error() -> None:
    with pytest.raises(LLMError):
        transferred({"findings": [{"quotes": []}]}, [], "search_transfer.1")


def test_transfer_prompt_and_schema_cover_every_property_and_label() -> None:
    text = load_prompt("search_transfer_system.md")
    item = TRANSFER_SCHEMA["properties"]["findings"]["items"]["properties"]

    for prop in Property:
        assert f"| {prop.value} | {prop.info.english} (" in text, prop
    assert item["property"]["enum"] == [p.value for p in Property]
    assert item["defect_type"]["enum"] == [d.label for d in DefectType]
    assert item["verdict"]["enum"] == ["нарушение", "неясно"]
    assert list(SEVERITY_LABELS) == item["severity"]["enum"]


def test_search_prompt_lists_the_same_defect_types_as_the_contract() -> None:
    line = load_prompt("search_system.md").split("Типы дефекта:", 1)[1].split("\n", 1)[0]

    labels = [x.strip(" .") for x in line.split(";")]

    assert labels == [d.label for d in DefectType]


def test_fill_substitutes_slots_once() -> None:
    assert fill("<{document}|{findings}>", document="{findings}", findings="Н-1") == "<{findings}|Н-1>"
    assert fill("без слотов {x}") == "без слотов {x}"


def test_fixture_envelopes_are_claude_cli_format() -> None:
    for path in sorted((FIXTURES_LLM / "search").glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["type"] == "result" and data["subtype"] == "success" and not data["is_error"], path
        assert "structured_output" not in data, "проход отвечает текстом без схемы"


# ---------------------------------------------------------------- записанный живой проход


LIVE = FIXTURES_LLM / "search" / "live"
"""Живой проход Opus 5.5 medium и перенос всех его находок Haiku 5.5 medium (эксперимент 23)."""


def live_payload(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((LIVE / name).read_text(encoding="utf-8"))
    return data


def test_live_pass_is_parsed_entirely_by_code(requirements_md: ParsedDocument) -> None:
    """Настоящий ответ прохода по промпту #14: код разбирает все находки, перенос моделью не нужен."""
    llm = FakeLLM(Path("/nonexistent"), {(LLMStep.SEARCH, 1): live_payload("pass-opus-medium.json")})

    [result] = search(requirements_md, llm, config(1))

    assert result.ok and result.diagnostics == []
    assert [r.step for r in llm.requests] == [LLMStep.SEARCH]
    assert len(result.candidates) == 49
    assert all(c.title and c.explanation and c.block_ids and c.quotes for c in result.candidates)
    quotes = [q for c in result.candidates for q in c.quotes]
    assert all(locate_quote(requirements_md, q).verified for q in quotes), "цитаты Opus дословные"
    assert {c.scope for c in result.candidates} == {Scope.REQUIREMENT, Scope.SET}
    assert sum(c.verdict is Verdict.UNCLEAR for c in result.candidates) == 2


def test_live_haiku_transfer_fits_schema_and_agrees_with_code(requirements_md: ParsedDocument) -> None:
    """Перенос всех 49 находок живого прохода Haiku: все записи по схеме, свойство и тип как у кода."""
    split = split_findings(live_payload("pass-opus-medium.json")["result"])
    code = {raw.position: draft_from_raw(raw) for raw in split.findings}
    fragments = [Fragment.of(raw) for raw in split.findings]

    moved = transferred(
        live_payload("transfer-haiku-medium.json")["structured_output"], fragments, "search_transfer.1"
    )

    assert (len(moved.drafts), moved.invalid, moved.lost) == (49, 0, [])
    assert [int(pos) for pos, _ in moved.drafts] == [raw.position for raw in split.findings]
    pairs = [(code[int(pos)], d) for pos, d in moved.drafts]
    assert all(isinstance(c, Draft) for c, _ in pairs)
    agree = {
        name: sum(getattr(c, name) == getattr(d, name) for c, d in pairs)
        for name in ("property", "defect_type", "verdict", "severity_hint", "blocks_testing")
    }
    # Расхождения: у Н-9 в строке «Свойство» два свойства (код берёт первое), у Н-18 «Traceable» без уровня
    # (код берёт уровень по подразделу ответа, Haiku выбрал набор).
    assert agree == {
        "property": 47,
        "defect_type": 49,
        "verdict": 49,
        "severity_hint": 49,
        "blocks_testing": 49,
    }
