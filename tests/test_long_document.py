"""Длинный документ (#38): план частей, нарезка и поиск по частям, сверка между частями, оценка цены.

Модель подменяет ``ScriptedLLM``: отвечает по тексту запроса (какие блоки и требования ему показали),
поэтому тест проверяет, что каждая часть видит только свои блоки, а ID блоков и ``R-NN`` глобальные.
Документ выдуман (учебный портал) и не связан с проверяемым ТЗ.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from validator import evidence_critic, evidence_witness, pipeline
from validator.config import LONG_DOC_TOKENS, estimate_tokens, load_config
from validator.estimate import estimate_run
from validator.ingest import parse
from validator.ingest.blocks import render_for_model
from validator.llm import parse_cli_payload
from validator.merge import merge
from validator.models import (
    Backend,
    BlockKind,
    DefectType,
    DiagLevel,
    DocKind,
    ExitCode,
    LLMRequest,
    LLMResponse,
    LLMStep,
    MergedFinding,
    ParsedDocument,
    Profile,
    Property,
    RunConfig,
    RunOutcome,
    Scope,
    Severity,
    SourceKind,
    Verdict,
)
from validator.parts import FOCUS_WINDOW_BLOCKS, PartPlan, focus_view, plan_parts, render_part
from validator.prompting import load_prompt
from validator.search import search
from validator.segment import fallback_segmentation, segment
from validator.segment import step as segment_step
from validator.set_check import (
    SetCheckReplySchema,
    SetContext,
    SetIssue,
    parse_reply,
    requirement_parts,
    set_check,
    to_candidates,
)
from validator.verify import verify

SECTIONS = 8
PARAGRAPHS = 14
FILLER = (
    "Запись хранится в журнале курса, куратор видит её в карточке слушателя, а отчёт по группе "
    "обновляется после каждого занятия без участия администратора портала"
)


def long_markdown(sections: int = SECTIONS, paragraphs: int = PARAGRAPHS) -> str:
    """Учебный портал: раздел терминов и ``sections`` разделов по ``paragraphs`` требований."""
    lines = [
        "# Учебный портал",
        "",
        "## Термины и определения",
        "",
        "Слушатель: человек, записанный на курс.",
        "",
        "Куратор: сотрудник, который ведёт группу слушателей.",
        "",
    ]
    for s in range(1, sections + 1):
        lines += [f"## {s}. Модуль {s}", ""]
        for p in range(1, paragraphs + 1):
            lines += [f"Портал должен сохранять отметку {s}.{p} о посещении занятия. {FILLER} ({s}.{p}).", ""]
    return "\n".join(lines)


@pytest.fixture
def long_doc(make_md: Callable[[str], ParsedDocument]) -> ParsedDocument:
    return make_md(long_markdown())


@pytest.fixture
def plan(long_doc: ParsedDocument) -> PartPlan:
    return plan_parts(long_doc)


def cfg_for(tmp_path: Path, passes: int = 2, **extra: Any) -> RunConfig:
    return load_config(
        tmp_path / "doc.md",
        tmp_path / "out" / "report.md",
        backend=Backend.FAKE,
        profile=Profile.STANDARD,
        passes=passes,
        **extra,
    )


# ---------------------------------------------------------------- модель по сценарию

_BLOCK_LINE = re.compile(r"^\[(B\d+)\]", re.MULTILINE)
_REQ_LINE = re.compile(r"^(R-\d+) · часть (\d+)", re.MULTILINE)


def _envelope(text: str = "", structured: object = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "duration_ms": 10,
        "total_cost_usd": 0.0,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    if structured is not None:
        data["structured_output"] = structured
    return data


class ScriptedLLM:
    """Отвечает по содержанию запроса: нарезка берёт абзацы части, поиск даёт одну находку на часть,
    сверка связывает первое требование первой части с первым требованием последней."""

    backend = Backend.FAKE

    def __init__(self, doc: ParsedDocument) -> None:
        self.doc = doc
        self.blocks = {b.id: b for b in doc.blocks}
        self.requests: list[LLMRequest] = []
        self._lock = threading.Lock()

    def shown(self, req: LLMRequest) -> list[str]:
        return _BLOCK_LINE.findall(req.prompt)

    def call(self, req: LLMRequest) -> LLMResponse:
        with self._lock:
            self.requests.append(req)
        return parse_cli_payload(self._payload(req), req, self.backend)

    def _payload(self, req: LLMRequest) -> dict[str, Any]:
        if req.step is LLMStep.SEGMENT:
            return _envelope(structured=self._segment(req))
        if req.step is LLMStep.SEARCH:
            return _envelope(self._search(req))
        if req.step is LLMStep.SET_CHECK:
            return _envelope(self._set_check(req))
        return _envelope('## Ответ\n```json\n{"questions": [], "reviews": []}\n```')

    def _segment(self, req: LLMRequest) -> dict[str, Any]:
        items, excluded = [], []
        for bid in self.shown(req):
            block = self.blocks[bid]
            if block.kind is BlockKind.PARAGRAPH and "должен" in block.text:
                items.append(
                    {
                        "refs": [bid],
                        "part_start": "",
                        "part_end": "",
                        "type": "functional",
                        "subtype": "",
                        "source_id": "",
                        "summary": "Портал сохраняет отметку о посещении",
                        "entities": [],
                        "links": [],
                        "depends_on": "",
                    }
                )
            else:
                label = "heading" if block.kind is BlockKind.HEADING else "context"
                excluded.append({"block": bid, "label": label, "reason": "не требование"})
        return {"requirements": items, "excluded": excluded}

    def _search(self, req: LLMRequest) -> str:
        own = [b for b in self.shown(req) if "должен" in self.blocks[b].text]
        bid = own[0]
        quote = self.blocks[bid].text.split(". ", 1)[0]
        return "\n".join(
            [
                "#### Н-1. Не сказано, когда сохранять отметку",
                f"- Блоки: {bid}",
                f"- Цитата: «{quote}»",
                "- Свойство: Verifiable (проверяемость)",
                "- Тип дефекта: непроверяемость",
                "- Вердикт: нарушение",
                "- Почему мешает: Нельзя написать тест на момент сохранения.",
                "- Тест: мешает, нет момента сохранения",
                "- Важность: средне",
                "- Как исправить: «Портал сохраняет отметку в течение [уточнить: секунд]»",
            ]
        )

    def _set_check(self, req: LLMRequest) -> str:
        rows = _REQ_LINE.findall(req.prompt)
        first, first_part = rows[0]
        last_part = max(int(p) for _, p in rows)
        last = next(r for r, p in rows if int(p) == last_part)
        same = [r for r, p in rows if p == first_part][:2]
        issues = [
            {
                "kind": "contradiction",
                "requirements": [first, last],
                "title": "Разные отметки",
                "explanation": "Два места задают разное.",
                "scenario": "Занятие 1: по одному месту да, по другому нет.",
                "suggestion": "Оставить одно место",
            },
            {
                "kind": "duplicate",
                "requirements": same,
                "title": "Повтор в одной части",
                "explanation": "Повтор.",
            },
            {
                "kind": "duplicate",
                "requirements": ["R-999", "R-01"],
                "title": "Выдуманный ID",
                "explanation": "-",
            },
        ]
        reply = json.dumps({"issues": issues}, ensure_ascii=False)
        return f"## Рассуждение\nСравнил отметки.\n## Ответ\n```json\n{reply}\n```"


# ---------------------------------------------------------------- план частей


def test_short_document_is_one_part_and_rendered_as_before(make_md: Callable[[str], ParsedDocument]) -> None:
    doc = make_md(long_markdown(sections=1, paragraphs=3))

    plan = plan_parts(doc)

    assert not plan.split and len(plan.parts) == 1
    assert render_part(doc, plan, plan.parts[0], with_context=True) == render_for_model(doc)


def test_long_document_is_split_by_top_level_sections(long_doc: ParsedDocument, plan: PartPlan) -> None:
    assert estimate_tokens(len(render_for_model(long_doc))) > LONG_DOC_TOKENS
    assert plan.split

    owned = [b.id for part in plan.parts for b in part.blocks]
    assert owned == [b.id for b in long_doc.blocks], "каждый блок ровно в одной части, порядок документа"
    for part in plan.parts:
        assert part.tokens <= LONG_DOC_TOKENS
        for block in part.blocks:
            top = block.section[1] if len(block.section) > 1 else None
            assert top is None or top in part.sections, "раздел верхнего уровня не режется между частями"


def test_glossary_is_shown_to_every_part_for_reference(long_doc: ParsedDocument, plan: PartPlan) -> None:
    glossary = {b.id for b in plan.glossary}
    assert glossary

    for part in plan.parts[1:]:
        rendered = render_part(long_doc, plan, part, with_context=True)
        shown = set(_BLOCK_LINE.findall(rendered))
        assert glossary <= shown
        assert f"Это часть {part.index} из {len(plan.parts)}" in rendered
        assert shown - glossary == {b.id for b in part.blocks}


# ---------------------------------------------------------------- нарезка и поиск по частям


def test_segmentation_runs_per_part_with_global_ids(
    long_doc: ParsedDocument, plan: PartPlan, tmp_path: Path
) -> None:
    llm = ScriptedLLM(long_doc)

    seg = segment(long_doc, llm, cfg_for(tmp_path), plan)

    calls = [r for r in llm.requests if r.step is LLMStep.SEGMENT]
    assert len(calls) == len(plan.parts)
    for req, part in zip(sorted(calls, key=lambda r: r.call_index), plan.parts, strict=True):
        assert set(llm.shown(req)) == {b.id for b in part.blocks}, "часть видит только свои блоки"
    paragraphs = [b for b in long_doc.blocks if "должен" in b.text]
    assert [r.anchor.block_ids[0] for r in seg.requirements] == [b.id for b in paragraphs]
    assert [r.id for r in seg.requirements] == [f"R-{n:02d}" for n in range(1, len(paragraphs) + 1)]
    assert all(long_doc.text[r.anchor.char_start : r.anchor.char_end] == r.quote for r in seg.requirements)


def test_search_runs_each_pass_on_each_part(long_doc: ParsedDocument, plan: PartPlan, tmp_path: Path) -> None:
    llm = ScriptedLLM(long_doc)
    cfg = cfg_for(tmp_path, passes=2)

    passes = search(long_doc, llm, cfg, plan)

    calls = sorted(r.call_index for r in llm.requests if r.step is LLMStep.SEARCH)
    assert calls == list(range(1, len(plan.parts) * cfg.passes + 1))
    assert [p.index for p in passes] == [1, 2] and all(p.ok for p in passes)
    for p in passes:
        assert len(p.candidates) == len(plan.parts), "по находке от каждой части"
        owners = [plan.part_of(c.block_ids[0]) for c in p.candidates]
        assert owners == [part.index for part in plan.parts], "ID блоков в находках глобальные"
        assert len({c.cid for c in p.candidates}) == len(p.candidates)


def test_failed_part_of_a_pass_keeps_other_parts(
    long_doc: ParsedDocument, plan: PartPlan, tmp_path: Path
) -> None:
    llm = ScriptedLLM(long_doc)
    real = llm._payload

    def fail_second_part(req: LLMRequest) -> dict[str, Any]:
        if req.step is LLMStep.SEARCH and req.call_index == 2:  # часть 2 прохода 1 при одном проходе
            return {"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "сбой"}
        return real(req)

    llm._payload = fail_second_part  # type: ignore[method-assign]
    passes = search(long_doc, llm, cfg_for(tmp_path, passes=1), plan)

    assert passes[0].ok
    assert len(passes[0].candidates) == len(plan.parts) - 1
    assert any("части 2" in d.action for d in passes[0].diagnostics if d.code == "LLM-030")


# ---------------------------------------------------------------- сверка между частями


def test_set_check_keeps_only_known_pairs_from_different_parts(
    long_doc: ParsedDocument, plan: PartPlan, tmp_path: Path
) -> None:
    llm = ScriptedLLM(long_doc)
    cfg = cfg_for(tmp_path)
    seg = segment(long_doc, llm, cfg, plan)

    outcome = set_check(seg, plan, llm, cfg)

    calls = [r for r in llm.requests if r.step is LLMStep.SET_CHECK]
    assert len(calls) == 1 and calls[0].call_index == 1
    assert "R-01 · часть 1" in calls[0].prompt
    assert len(outcome.candidates) == 1
    cand = outcome.candidates[0]
    assert cand.source.kind is SourceKind.SET_CHECK and cand.scope is Scope.SET
    assert cand.property is Property.SET_CONSISTENT and cand.defect_type is DefectType.CONTRADICTION
    assert cand.blocks_testing and cand.scenario
    owner = requirement_parts(seg.requirements, plan)
    last = next(r for r in seg.requirements if owner[r.id] == len(plan.parts))
    assert cand.quotes == [seg.requirements[0].quote, last.quote], "цитаты подставил код из требований"
    dropped = [d for d in outcome.diagnostics if d.code == "LLM-063"]
    assert dropped and "из одной части" in dropped[0].message and "известных требований" in dropped[0].message


def test_set_check_is_not_called_for_a_whole_document(
    make_md: Callable[[str], ParsedDocument], tmp_path: Path
) -> None:
    doc = make_md(long_markdown(sections=1, paragraphs=3))
    llm = ScriptedLLM(doc)
    cfg = cfg_for(tmp_path)
    plan = plan_parts(doc)

    outcome = set_check(segment(doc, llm, cfg, plan), plan, llm, cfg)

    assert outcome.candidates == [] and outcome.diagnostics == []
    assert not [r for r in llm.requests if r.step is LLMStep.SET_CHECK]


def test_set_check_failure_is_a_warning_not_a_crash(
    long_doc: ParsedDocument, plan: PartPlan, tmp_path: Path
) -> None:
    llm = ScriptedLLM(long_doc)
    cfg = cfg_for(tmp_path)
    seg = segment(long_doc, llm, cfg, plan)
    real = llm._payload
    llm._payload = lambda req: _envelope("без JSON") if req.step is LLMStep.SET_CHECK else real(req)  # type: ignore[method-assign]

    outcome = set_check(seg, plan, llm, cfg)

    assert outcome.candidates == []
    assert [(d.code, d.level) for d in outcome.diagnostics] == [("LLM-062", DiagLevel.WARNING)]


def test_set_check_reply_parsing_is_lenient(long_doc: ParsedDocument, plan: PartPlan, tmp_path: Path) -> None:
    seg = segment(long_doc, ScriptedLLM(long_doc), cfg_for(tmp_path), plan)
    owner = requirement_parts(seg.requirements, plan)
    last = seg.requirements[-1].id
    ctx = SetContext({r.id: r for r in seg.requirements}, owner, "set_check.1")
    issues = [
        SetIssue(kind="Противоречие", requirements=["R-1", f"{last} (часть {owner[last]})"], explanation="x"),
        SetIssue(kind="дублирование", requirements=f"R-01, {last}", explanation="y"),  # type: ignore[arg-type]
        SetIssue(kind="неполнота", requirements=["R-01", last], explanation="z"),
    ]

    cands = to_candidates(issues, ctx)

    assert [c.defect_type for c in cands] == [DefectType.CONTRADICTION, DefectType.REDUNDANCY]
    assert cands[1].property is Property.SET_MODIFIABLE and not cands[1].blocks_testing
    assert [c.source.rule_id for c in cands] == ["set.001", "set.002"], "у каждой записи свой ID правила"


def test_set_check_example_in_prompt_matches_schema() -> None:
    text = load_prompt("set_check_system.md")
    example = re.findall(r"```json\n(.*?)```", text, re.DOTALL)[-1]

    SetCheckReplySchema.model_validate_json(example)
    reply = LLMResponse(call_id="set_check.1", backend=Backend.FAKE, text=f"```json\n{example}```")
    assert len(parse_reply(reply)) == 2


# ---------------------------------------------------------------- вопросы и оценка цены


def test_focus_view_keeps_whole_document_when_it_fits(long_doc: ParsedDocument) -> None:
    assert focus_view(long_doc, ["B005"]) == render_for_model(long_doc)


def test_focus_view_shows_only_sections_with_places_when_too_long(
    long_doc: ParsedDocument, plan: PartPlan
) -> None:
    target = next(b for b in long_doc.blocks if "отметку 4.3 " in b.text)

    view = focus_view(long_doc, [target.id + ".1"], limit=5_000)

    shown = set(_BLOCK_LINE.findall(view))
    section = {b.id for b in long_doc.blocks if b.section[:2] == target.section[:2]}
    assert shown == section | {b.id for b in plan.glossary}
    assert "Оглавление всего документа" in view and "- 8. Модуль 8" in view


def test_focus_view_narrows_to_neighbours_when_sections_are_too_long(long_doc: ParsedDocument) -> None:
    order = {b.id: k for k, b in enumerate(long_doc.blocks)}
    target = next(b for b in long_doc.blocks if "отметку 4.3 " in b.text)

    view = focus_view(long_doc, [target.id], limit=1_000)

    shown = set(_BLOCK_LINE.findall(view))
    assert target.id in shown
    glossary = {b.id for b in long_doc.blocks if "Термины" in " ".join(b.section)}
    assert all(abs(order[b] - order[target.id]) <= FOCUS_WINDOW_BLOCKS for b in shown - glossary)


def test_estimate_counts_parts_and_set_check(
    long_doc: ParsedDocument, plan: PartPlan, tmp_path: Path
) -> None:
    cfg = cfg_for(tmp_path, passes=3)

    estimate = estimate_run(plan, cfg)

    steps = {s.step: s.calls for s in estimate.steps}
    assert steps[LLMStep.SEGMENT] == len(plan.parts)
    assert steps[LLMStep.SEARCH] == 3 * len(plan.parts)
    assert steps[LLMStep.SET_CHECK] == 1
    assert estimate.usd is not None and estimate.usd > 0


# ---------------------------------------------------------------- конвейер целиком


def run_long(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra: Any) -> tuple[RunOutcome, ScriptedLLM]:
    path = tmp_path / "doc.md"
    path.write_text(long_markdown(), encoding="utf-8")
    holder: dict[str, ScriptedLLM] = {}

    def client(cfg: RunConfig) -> tuple[ScriptedLLM, list[Any]]:
        holder["llm"] = ScriptedLLM(parse(cfg.input_path, DocKind.MD))
        return holder["llm"], []

    monkeypatch.setattr(pipeline, "make_client", client)
    outcome = pipeline.run(cfg_for(tmp_path, **extra))
    return outcome, holder["llm"]


def test_pipeline_checks_long_document_by_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, llm = run_long(tmp_path, monkeypatch)

    assert outcome.exit_code is ExitCode.OK
    report = outcome.report
    assert report is not None and report.document is not None
    parts = report.document.parts
    assert len(parts) > 1
    steps = [r.step for r in llm.requests]
    assert steps.count(LLMStep.SEGMENT) == len(parts)
    assert steps.count(LLMStep.SEARCH) == 2 * len(parts)
    assert steps.count(LLMStep.SET_CHECK) == 1
    info = [d for d in outcome.diagnostics if d.code == "LLM-060"]
    assert info and info[0].level is DiagLevel.INFO and f"частей: {len(parts)}" in info[0].message
    assert report.verification is not None
    cross = [
        f for f in report.verification.findings if any(s.kind is SourceKind.SET_CHECK for s in f.sources)
    ]
    assert len(cross) == 1 and len(cross[0].requirement_ids) == 2
    markdown = (tmp_path / "out" / "report.md").read_text(encoding="utf-8")
    assert "| Часть | Разделы |" in markdown
    assert "сверка требований между частями документа; проходы поиска видят по одной части" in markdown


def test_budget_below_estimate_warns_before_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ScriptedLLM, "backend", Backend.CLI)
    notices: list[str] = []
    monkeypatch.setattr(pipeline.notice, "warning", lambda msg, *args: notices.append(msg % args))

    outcome, _ = run_long(tmp_path, monkeypatch, max_budget_usd=0.5)

    warning = [d for d in outcome.diagnostics if d.code == "LLM-061"]
    assert warning and warning[0].level is DiagLevel.WARNING
    assert notices and "--max-budget-usd" in notices[0]


# ---------------------------------------------------------------- синтетический документ на 60+ страниц

LONG_DIR = Path(__file__).resolve().parent / "fixtures" / "long"


def test_long_fixture_is_up_to_date_and_split_into_parts() -> None:
    """``long.md`` собран текущим генератором и действительно длинный: больше 60 стандартных страниц."""
    spec = importlib.util.spec_from_file_location("make_long", LONG_DIR / "make_long.py")
    assert spec is not None and spec.loader is not None
    make_long = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = make_long  # dataclass ищет свой модуль в sys.modules
    spec.loader.exec_module(make_long)
    text = (LONG_DIR / "long.md").read_text(encoding="utf-8")
    rounds = text.count("\n## Подсистема ") // len(make_long.SOURCES)

    assert text == make_long.compose(rounds), (
        "перегенерируйте: uv run python tests/fixtures/long/make_long.py"
    )
    plan = plan_parts(parse(LONG_DIR / "long.md", DocKind.MD))
    assert len(plan.parts) > 10 and all(p.tokens <= LONG_DOC_TOKENS for p in plan.parts)
    pdf = LONG_DIR / "long.pdf"
    if pdf.is_file():  # PDF в репозиторий не кладём: проверяем страницы, если его собрали
        assert (parse(pdf, DocKind.PDF).pages or 0) >= make_long.MIN_PAGES


# ---------------------------------------------------------------- после ревью


def test_replay_of_a_long_run_aligns_passes_by_parts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Запись прогона по частям (вызовов поиска parts × K) пересобирается replay с теми же K проходами."""
    with monkeypatch.context() as patch:
        first, _ = run_long(tmp_path, patch, passes=2)
    cfg = load_config(
        tmp_path / "doc.md",
        tmp_path / "again" / "report.md",
        backend=Backend.REPLAY,
        replay_dir=tmp_path / "out" / "run",
    )

    again = pipeline.run(cfg)

    assert again.exit_code is ExitCode.OK
    codes = {d.code for d in again.diagnostics}
    assert not codes & {"LLM-015", "LLM-016", "LLM-030"}
    assert first.report is not None and again.report is not None
    assert again.report.passport.passes == 2
    assert first.report.verification is not None and again.report.verification is not None
    assert len(again.report.verification.findings) == len(first.report.verification.findings)


@pytest.mark.parametrize(
    ("title", "glossary"),
    [
        ("Термины и определения", True),
        ("5. Глоссарий", True),
        ("Список сокращений", True),
        ("2. Требования к платёжному терминалу", False),
        ("Определение статуса платежа", False),
    ],
)
def test_glossary_is_found_by_title_start_only(
    title: str, glossary: bool, make_md: Callable[[str], ParsedDocument]
) -> None:
    doc = make_md(
        f"# Портал\n\n## {title}\n\nСлушатель: человек, записанный на курс.\n\n" + long_markdown()[13:]
    )

    plan = plan_parts(doc)

    found = {" ".join(b.section) for b in plan.glossary}
    assert any(title in s for s in found) is glossary


def test_duplicate_with_identical_text_cites_both_places(
    make_md: Callable[[str], ParsedDocument], tmp_path: Path
) -> None:
    same = "Портал должен хранить журнал посещений не меньше одного учебного года."
    doc = make_md(f"# Портал\n\n## Модуль 1\n\n{same}\n\n## Модуль 2\n\n{same}\n")
    seg = fallback_segmentation(doc)
    first, second = seg.requirements
    ctx = SetContext({r.id: r for r in seg.requirements}, {first.id: 1, second.id: 2}, "set_check.1")
    issue = SetIssue(kind="duplicate", requirements=[first.id, second.id], explanation="Повтор.")

    merged = merge([], to_candidates([issue], ctx), doc)
    ver = verify(merged, doc, seg)

    finding = ver.findings[0]
    places = [q.anchor.block_ids[0] for q in finding.quotes if q.anchor]
    assert places == [first.anchor.block_ids[0], second.anchor.block_ids[0]]
    assert finding.requirement_ids == [first.id, second.id]


def test_internal_error_in_one_part_falls_back_only_that_part(
    long_doc: ParsedDocument, plan: PartPlan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    llm = ScriptedLLM(long_doc)
    real = segment_step.decide

    def broken_on_second_part(view: ParsedDocument, *args: Any) -> Any:
        if view.blocks[0].id == plan.parts[1].blocks[0].id:
            raise KeyError("сбой разбора")
        return real(view, *args)

    monkeypatch.setattr(segment_step, "decide", broken_on_second_part)
    seg = segment(long_doc, llm, cfg_for(tmp_path), plan)

    assert seg.mode.value == "model"
    failures = [d for d in seg.diagnostics if d.code == "INT-002"]
    assert len(failures) == 1 and "части 2" in failures[0].message
    owners = {plan.part_of(r.anchor.block_ids[0]) for r in seg.requirements}
    assert owners == {p.index for p in plan.parts}


def test_focus_view_windows_stay_within_limit(long_doc: ParsedDocument) -> None:
    places = [b.id for b in long_doc.blocks if "должен" in b.text]

    view = focus_view(long_doc, places, limit=2_000)

    assert estimate_tokens(len(view)) <= 2_000 + 1_000, "пометка с оглавлением сверх лимита блоков"
    shown = set(_BLOCK_LINE.findall(view))
    assert places[0] in shown and places[-1] not in shown, "места берутся по порядку важности"


def _finding_at(block_id: str, quote: str) -> MergedFinding:
    return MergedFinding(
        mid="M-001",
        property=Property.UNAMBIGUOUS,
        defect_type=DefectType.AMBIGUOUS,
        verdict=Verdict.VIOLATION,
        scope=Scope.REQUIREMENT,
        block_ids=[block_id],
        quotes=[quote],
        explanation="Пояснение находки.",
        scenario="Два прочтения дают разный результат.",
    )


def _evidence_requests(long_doc: ParsedDocument, cfg: RunConfig) -> tuple[list[LLMRequest], str]:
    target = next(b for b in long_doc.blocks if "отметку 4.3 " in b.text)
    found = _finding_at(target.id, target.text)
    pick = evidence_critic.Pick(found, Severity.MAJOR, ("тест",))
    requests = [
        evidence_witness.build_request([found], {}, long_doc, cfg),
        evidence_critic.build_request([pick], {}, long_doc, cfg),
    ]
    return requests, target.id


def test_witness_and_critic_see_only_the_sections_of_their_findings(
    long_doc: ParsedDocument, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Шаг 6 на документе длиннее вида целиком: свидетель и критик находок видят разделы своих мест (#38)."""
    for module in (evidence_witness, evidence_critic):
        monkeypatch.setattr(module, "focus_view", lambda doc, places: focus_view(doc, places, limit=5_000))

    requests, target = _evidence_requests(long_doc, cfg_for(tmp_path))

    for req in requests:
        shown = set(_BLOCK_LINE.findall(req.prompt))
        assert target in shown and len(shown) < len(long_doc.blocks) // 2
        assert "Оглавление всего документа" in req.prompt


def test_witness_and_critic_timeout_grows_with_input(long_doc: ParsedDocument, tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path)

    requests, _ = _evidence_requests(long_doc, cfg)

    assert all(req.timeout_s > cfg.call_timeout_s for req in requests)
