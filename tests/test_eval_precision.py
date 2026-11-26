"""Precision по разметке двумя моделями вслепую (#49): пачки, контрольные находки, вход разметчика, метрики.

Модель не вызывается: разметчика подменяет клиент-заглушка с записанным правилом ответа.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from pathlib import Path

import pytest

from conftest import ROOT
from evals.gold import GOLD_DOCUMENT, load_gold, read_gold_md
from evals.judge import EvalError
from evals.precision import Mode, label, main, write_result
from evals.precision_items import (
    MAX_OVERLAP,
    ControlKind,
    LabelItem,
    Origin,
    control_count,
    items_for_run,
    make_batches,
    overlap,
)
from evals.precision_labeler import (
    LABEL_PROMPT,
    LABEL_SYSTEM,
    Label,
    Labeler,
    label_prompt,
    label_request,
    parse_labels,
)
from evals.precision_metrics import agreement, consensus, control_score, kappa, precision
from evals.runs import load_document, load_run
from validator.models import Backend, LLMRequest, LLMResponse, LLMStep, ParsedDocument, QuoteMatch
from validator.text import locate_quote

AGENT_RUN = ROOT / "tests" / "fixtures" / "e2e" / "md"
BASELINE_RUN = ROOT / "tests" / "fixtures" / "eval" / "baseline-run"
SHOWN_KEYS = {"id", "quote", "place", "property", "explanation"}
_WORD = re.compile(r"[0-9a-zа-я]+")


class StubLabeler:
    """Разметчик без модели: метку даёт правило по номеру находки в пачке; запросы запоминает."""

    def __init__(self, rule: Callable[[int], str], backend: Backend = Backend.CLI) -> None:
        self.rule = rule
        self.backend = backend
        self.requests: list[LLMRequest] = []

    def call(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        shown = json.loads(req.prompt.split("=== НАХОДКИ ===\n", 1)[1])
        labels = [
            {"id": i["id"], "label": self.rule(i["id"]), "reason": f"правило для {i['id']}"} for i in shown
        ]
        return LLMResponse(
            call_id=req.call_id, backend=self.backend, structured={"labels": labels}, cost_usd=0.01
        )


@pytest.fixture(scope="module")
def doc() -> ParsedDocument:
    return load_document(GOLD_DOCUMENT)


@pytest.fixture(scope="module")
def pool(doc: ParsedDocument) -> list[LabelItem]:
    from evals.precision import _agent_findings  # noqa: PLC0415

    agent = load_run(AGENT_RUN, GOLD_DOCUMENT)
    baseline = load_run(BASELINE_RUN, GOLD_DOCUMENT)
    return items_for_run(agent, _agent_findings(AGENT_RUN), doc) + items_for_run(baseline, None, doc)


def _labels_by_uid(stubs: dict[Labeler, StubLabeler]) -> Callable[[Labeler], StubLabeler]:
    return lambda lab: stubs[lab]


# ---------------------------------------------------------------- пачки и контрольные находки


def test_batches_and_controls_are_deterministic_for_seed(pool: list[LabelItem], doc: ParsedDocument) -> None:
    first = make_batches(pool, doc, seed=7)
    again = make_batches(pool, doc, seed=7)
    other = make_batches(pool, doc, seed=8)

    assert first == again
    assert first[0] != other[0]


def test_every_real_finding_lands_in_exactly_one_batch(pool: list[LabelItem], doc: ParsedDocument) -> None:
    batches, controls = make_batches(pool, doc)

    uids = [u for b in batches for u in b.uids]
    assert sorted(uids) == sorted([i.uid for i in pool] + [c.uid for c in controls])
    assert len(uids) == len(set(uids))


def test_control_share_per_batch_is_10_to_15_percent(pool: list[LabelItem], doc: ParsedDocument) -> None:
    batches, controls = make_batches(pool, doc, batch_size=40)
    control_uids = {c.uid for c in controls}

    for batch in batches:
        share = sum(u in control_uids for u in batch.uids) / len(batch.uids)
        assert 0.10 <= share <= 0.15, (batch.index, share)


@pytest.mark.parametrize(("real", "expected"), [(0, 0), (1, 1), (8, 1), (40, 6), (39, 6)])
def test_control_count(real: int, expected: int) -> None:
    assert control_count(real) == expected


def test_controls_are_verbatim_and_point_away_from_their_donors(
    pool: list[LabelItem], doc: ParsedDocument
) -> None:
    _, controls = make_batches(pool, doc)
    by_uid = {i.uid: i for i in pool}

    assert {c.control for c in controls} == set(ControlKind)
    for c in controls:
        assert c.origin is Origin.CONTROL
        if c.control is ControlKind.SWAPPED_CLAIM:
            a, b = (by_uid[d] for d in c.donors)
            assert (c.quote, c.place) == (a.quote, a.place)
            assert (c.property, c.explanation) == (b.property, b.explanation)
            assert a.place != b.place
            continue
        assert locate_quote(doc, c.quote).match is QuoteMatch.EXACT, c.quote
        if c.control is ControlKind.MISPLACED_QUOTE:
            donor = by_uid[c.donors[0]]
            assert c.explanation == donor.explanation
            assert overlap(c.quote, f"{donor.quote} {donor.explanation}") <= MAX_OVERLAP


# ---------------------------------------------------------------- что видит разметчик


def _shingles(text: str, n: int = 6) -> set[tuple[str, ...]]:
    words = _WORD.findall(text.lower().replace("ё", "е"))
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def test_labeler_request_is_document_and_findings_only(pool: list[LabelItem], doc: ParsedDocument) -> None:
    batches, controls = make_batches(pool, doc)
    by_uid = {i.uid: i for i in [*pool, *controls]}
    items = [by_uid[u] for u in batches[0].uids]

    req = label_request(Labeler.FABLE, doc.text, items, 1)

    assert (
        req.prompt
        == LABEL_PROMPT
        + "\n\n=== ДОКУМЕНТ ===\n"
        + doc.text
        + "\n\n=== НАХОДКИ ===\n"
        + json.dumps([i.shown(n) for n, i in enumerate(items, start=1)], ensure_ascii=False, indent=1)
    )
    assert req.prompt == label_prompt(doc.text, items)
    assert (req.model, req.effort.value, req.step) == ("claude-fable-5-2", "high", LLMStep.PRECISION)


def test_labeler_sees_no_source_ids_or_control_marks(pool: list[LabelItem], doc: ParsedDocument) -> None:
    batches, controls = make_batches(pool, doc)
    by_uid = {i.uid: i for i in [*pool, *controls]}
    items = [by_uid[u] for u in batches[0].uids]

    shown = json.loads(label_prompt(doc.text, items).split("=== НАХОДКИ ===\n", 1)[1])

    assert all(set(entry) == SHOWN_KEYS for entry in shown)
    assert [e["id"] for e in shown] == list(range(1, len(items) + 1))
    dumped = json.dumps(shown, ensure_ascii=False)
    for marker in ("agent:", "baseline:", "control:", "e2e/md", "baseline-run", '"F-'):
        assert marker not in dumped


def test_labeler_gets_no_gold(pool: list[LabelItem], doc: ParsedDocument) -> None:
    gold = set().union(*(_shingles(f"{d.where}. {d.defect}") for d in load_gold().defects))
    gold_md = read_gold_md()
    batches, controls = make_batches(pool, doc)
    by_uid = {i.uid: i for i in [*pool, *controls]}

    prompt = label_prompt(doc.text, [by_uid[u] for u in batches[0].uids])

    assert not gold & _shingles(LABEL_SYSTEM + "\n" + LABEL_PROMPT)
    assert gold_md not in prompt
    assert "ЭТАЛОН" not in prompt.upper()


@pytest.mark.parametrize("module", ["precision_items", "precision_labeler"])
def test_labeler_modules_do_not_read_gold(module: str) -> None:
    tree = ast.parse((ROOT / "evals" / f"{module}.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}

    assert "evals.gold" not in imported
    assert "evals.overrides" not in imported


# ---------------------------------------------------------------- разбор ответа


def test_parse_labels_drops_bad_entries_and_reports_missing() -> None:
    warnings: list[str] = []
    structured = {
        "labels": [
            {"id": 1, "label": "real", "reason": "  есть   в тексте "},
            {"id": 2, "label": "maybe", "reason": ""},
            {"id": 9, "label": "false", "reason": ""},
            {"id": True, "label": "false", "reason": ""},
            {"id": 3, "label": "disputed", "reason": "a"},
            {"id": 3, "label": "false", "reason": "b"},
        ]
    }

    out = parse_labels(structured, 4, warnings, "тест")

    assert {n: m.label for n, m in out.items()} == {1: Label.REAL, 3: Label.FALSE}
    assert out[1].reason == "есть в тексте"
    assert any("неизвестная метка" in w for w in warnings)
    assert any("размечена дважды" in w for w in warnings)
    assert any("без метки находки [2, 4]" in w for w in warnings)


def test_parse_labels_rejects_answer_without_list() -> None:
    with pytest.raises(EvalError):
        parse_labels({"items": []}, 3, [], "тест")


# ---------------------------------------------------------------- метрики


def test_precision_strict_and_soft() -> None:
    marks = {"a": Label.REAL, "b": Label.DISPUTED, "c": Label.FALSE, "d": Label.REAL}

    p = precision(["a", "b", "c", "d", "e"], marks)

    assert (p.total, p.labeled, p.real, p.disputed, p.false) == (5, 4, 2, 1, 1)
    assert p.strict == 0.5
    assert p.soft == 0.75


def test_consensus_needs_both_real_and_any_false_wins() -> None:
    a = {"x": Label.REAL, "y": Label.REAL, "z": Label.DISPUTED, "w": Label.REAL}
    b = {"x": Label.REAL, "y": Label.FALSE, "z": Label.REAL}

    assert consensus(a, b) == {"x": Label.REAL, "y": Label.FALSE, "z": Label.DISPUTED}


def test_kappa_matches_hand_calculation() -> None:
    # наблюдаемое 0,75; ожидаемое (2·1 + 2·3) / 16 = 0,5; каппа (0,75 − 0,5) / 0,5 = 0,5
    assert kappa([("a", "a"), ("a", "b"), ("b", "b"), ("b", "b")]) == pytest.approx(0.5)
    assert kappa([]) is None
    assert kappa([("a", "a"), ("a", "a")]) is None


def test_agreement_counts_only_items_labeled_by_both() -> None:
    a = {"x": Label.REAL, "y": Label.FALSE, "z": Label.REAL}
    b = {"x": Label.REAL, "y": Label.DISPUTED}

    got = agreement(["x", "y", "z"], a, b)

    assert (got.both, got.same, got.share) == (2, 1, 0.5)


def test_control_score_counts_false_as_caught() -> None:
    marks = {"c1": Label.FALSE, "c2": Label.DISPUTED, "c3": Label.REAL}

    got = control_score(["c1", "c2", "c3", "c4"], marks)

    assert (got.total, got.labeled, got.caught, got.not_real) == (4, 3, 1, 2)
    assert got.caught_share == pytest.approx(1 / 3)


# ---------------------------------------------------------------- прогон целиком


def test_label_end_to_end_then_cache_only_gives_same_result(tmp_path: Path) -> None:
    stubs = {
        Labeler.OPUS: StubLabeler(lambda n: "real" if n % 3 else "false"),
        Labeler.FABLE: StubLabeler(lambda n: "real" if n % 2 else "disputed"),
    }
    cache = tmp_path / "cache"

    first = label([AGENT_RUN, BASELINE_RUN], name="t", cache_dir=cache, client_factory=_labels_by_uid(stubs))
    cached = label([AGENT_RUN, BASELINE_RUN], name="t", mode=Mode.CACHE, cache_dir=cache)

    assert all(s.requests for s in stubs.values())
    assert {c.source for c in first.calls} == {"cli"}
    assert {c.source for c in cached.calls} == {"cache"}
    assert cached.labels == first.labels
    assert cached.overall == first.overall
    real = [i for i in first.items if i.origin is not Origin.CONTROL]
    assert first.overall.consensus.total == len(real)
    expected = sum(first.labels[Labeler.OPUS][i.uid].label is Label.REAL for i in real) / len(real)
    assert first.overall.by_labeler[Labeler.OPUS].strict == pytest.approx(expected)
    assert first.controls[Labeler.OPUS].labeled == len(first.items) - len(real)


def test_cache_mode_without_answers_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(EvalError, match="--mode cli"):
        label([BASELINE_RUN], name="t", mode=Mode.CACHE, cache_dir=tmp_path)


def test_fake_backend_answers_are_not_cached(tmp_path: Path) -> None:
    stub = StubLabeler(lambda _: "real", backend=Backend.FAKE)

    result = label(
        [BASELINE_RUN], name="t", labelers=[Labeler.OPUS], cache_dir=tmp_path, client_factory=lambda _: stub
    )

    assert {c.source for c in result.calls} == {"fake"}
    assert not list(tmp_path.glob("*.json"))


def test_result_files_and_cli_from_cache(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cache = tmp_path / "cache"
    stubs = {lab: StubLabeler(lambda n: "false" if n == 1 else "real") for lab in Labeler}
    result = label([BASELINE_RUN], name="t", cache_dir=cache, client_factory=_labels_by_uid(stubs))
    json_path, md_path = write_result(result, tmp_path / "out")

    code = main(["label", str(BASELINE_RUN), "--name", "t", "--mode", "cache", "--cache-dir", str(cache),
                 "--out-dir", str(tmp_path / "out")])  # fmt: skip

    assert code == 0
    assert json_path.name.endswith("-precision-t.json")
    markdown = md_path.read_text(encoding="utf-8")
    assert "Разметка моделями, не людьми" in markdown
    assert "Ложные по мнению всех разметчиков" in markdown
    assert len(list((tmp_path / "out").glob("*.md"))) == 2  # второй запуск не затёр первый
    assert "Precision «t»" in capsys.readouterr().out


def test_unknown_labeler_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["label", str(BASELINE_RUN), "--name", "t", "--labelers", "opus,gpt"])

    assert code == 1
    assert "Неизвестный разметчик" in capsys.readouterr().err
