"""Мутации документа (#50): каждый оператор меняет ровно то, что записал в манифест."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from conftest import REQUIREMENTS, ROOT
from evals import mutations_score as score
from evals.mutate import Pin, generate, mutate
from evals.mutation_ops import CANDIDATES, candidates, neutralize
from evals.mutation_text import OPERATORS, parse
from test_prompt_leak import FORBIDDEN, _shingles, _words
from validator.models import DocKind

DOC = REQUIREMENTS[DocKind.MD].read_text(encoding="utf-8")
MUTATIONS_DIR = ROOT / "evals" / "mutations"
MUTATION_CODE = [ROOT / "evals" / n for n in ("mutate.py", "mutation_ops.py", "mutation_text.py")]

# Небольшой документ другой предметной области: операторы не должны зависеть от нашего ТЗ.
OTHER_DOC = """# Сервис бронирования

## Функциональные требования

* **REQ-1.** Пользователь должен выбрать дату заезда не позднее 30 дней до начала.
* **REQ-2.** Система должна отправлять подтверждение на почту в течение 5 минут.
* **REQ-3.** При ошибке оплаты бронь отменяется.

## Нефункциональные требования

Сервер отвечает на запрос не более 200 мс при нагрузке 100 RPS.

## Ответы API

| Код | Значение |
| --- | --- |
| 200 | Бронь создана |
| 409 | Даты заняты |
"""

EMPTY_DOC = "# Только заголовок\n"


def _undo(copy: str, mutations: list[dict[str, Any]]) -> str:
    """Откат всех правок манифеста с конца документа: должен дать исходный текст."""
    for m in sorted(mutations, key=lambda m: m["start"], reverse=True):
        assert copy[m["start"] : m["end"]] == m["replacement"]
        copy = copy[: m["start"]] + m["original"] + copy[m["end"] :]
    return copy


@pytest.mark.parametrize("op", sorted(OPERATORS))
@pytest.mark.parametrize("text", [DOC, OTHER_DOC], ids=["requirements", "other"])
def test_operator_changes_exactly_what_manifest_says(op: str, text: str) -> None:
    res = mutate(text, [op], seed=1)

    assert not res.skipped, res.skipped
    [m] = res.mutations
    assert m.operator == op
    assert m.original != m.replacement
    assert res.text[m.start : m.end] == m.replacement
    assert res.text[: m.start] + m.original + res.text[m.end :] == text
    assert m.match_spans, "некуда цитировать: мутацию нельзя поймать"
    for a, b in m.match_spans:
        assert 0 <= a < b <= len(res.text)
    assert m.line == res.text.count("\n", 0, m.start) + 1


@pytest.mark.parametrize("op", sorted(OPERATORS))
def test_operator_without_place_says_so_instead_of_failing(op: str) -> None:
    assert candidates(op, EMPTY_DOC) == []

    res = mutate(EMPTY_DOC, [op], seed=1)

    assert res.text == EMPTY_DOC
    assert res.mutations == []
    assert res.skipped == [{"operator": op, "reason": "места нет"}]


def test_operators_find_forms_not_content_of_our_document() -> None:
    expected = {
        "M1": "не позднее 30 дней",
        "M5": "При ошибке оплаты бронь отменяется.",
        "M9": "Пользователь должен выбрать",
    }
    for op, fragment in expected.items():
        originals = [OTHER_DOC[c.start : c.end] for c in candidates(op, OTHER_DOC)]
        assert any(fragment in o for o in originals), (op, originals)


def test_http_error_row_is_found_but_amount_is_not() -> None:
    rows = [OTHER_DOC[c.start : c.end] for c in candidates("M5", OTHER_DOC)]

    assert any("409" in r for r in rows)
    assert not any("200" in r for r in rows)
    text = "## Р\n\n| Действие | Сумма |\n| --- | --- |\n| Перевести | 500 ₸ |\n"
    assert candidates("M5", text) == []


def test_several_mutations_go_to_different_sections_and_undo_cleanly() -> None:
    res = mutate(DOC, ["M1", "M2", "M5", "M7", "M10"], seed=7)

    assert len(res.mutations) == 5
    sections = [m.section for m in res.mutations]
    assert len(set(sections)) == len(sections)
    as_dicts = [m.__dict__ for m in res.mutations]
    assert _undo(res.text, as_dicts) == DOC


def test_same_seed_gives_same_copy() -> None:
    a = mutate(DOC, ["M3", "M4", "M8"], seed=3)
    b = mutate(DOC, ["M3", "M4", "M8"], seed=3)

    assert a.text == b.text
    assert a.mutations == b.mutations


def test_pin_reproduces_mutation_from_other_copy() -> None:
    multi = mutate(DOC, ["M1", "M6", "M10"], seed=11)
    m = next(x for x in multi.mutations if x.operator == "M6")

    single = mutate(DOC, [], seed=0, pins=[Pin("M6", m.original, m.replacement, m.section)])

    [s] = single.mutations
    assert (s.original, s.replacement, s.section) == (m.original, m.replacement, m.section)


def test_neutral_mutations_keep_every_word() -> None:
    text, log = neutralize(DOC, ["N1", "N3"])

    assert all(entry["result"] != "места нет" for entry in log)
    assert text != DOC
    lost = Counter(_words(DOC)) - Counter(_words(text))
    head = DOC.split("\n")[next(i for i, ln in enumerate(parse(DOC)) if ln.kind == "table_head")]
    assert lost == Counter(_words(head)), "N3 теряет только строку заголовка таблицы"
    assert not Counter(_words(text)) - Counter(_words(DOC))


def test_committed_mutants_match_generator(tmp_path: Path) -> None:
    generate(REQUIREMENTS[DocKind.MD].relative_to(ROOT), tmp_path, "req")

    for path in sorted(tmp_path.iterdir()):
        committed = MUTATIONS_DIR / path.name
        assert committed.is_file(), f"нет {committed}"
        assert committed.read_text(encoding="utf-8") == path.read_text(encoding="utf-8"), path.name


def test_committed_manifests_undo_to_source() -> None:
    for manifest_path in sorted(MUTATIONS_DIR.glob("*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["kind"] == "neutral":
            continue
        copy = manifest_path.with_suffix(".md").read_text(encoding="utf-8")
        assert _undo(copy, manifest["mutations"]) == DOC, manifest_path.name


def test_every_operator_is_in_committed_multi_copies() -> None:
    ops = []
    for manifest_path in MUTATIONS_DIR.glob("*multi*.json"):
        ops += [m["operator"] for m in json.loads(manifest_path.read_text(encoding="utf-8"))["mutations"]]
    assert set(ops) == set(OPERATORS)


def test_mutation_code_has_no_words_of_checked_document() -> None:
    document = _shingles(_words(DOC))
    for path in MUTATION_CODE:
        code = path.read_text(encoding="utf-8")
        assert not FORBIDDEN.search(code), path.name
        assert not document & _shingles(_words(code)), path.name


def test_mutation_code_does_not_read_gold() -> None:
    for path in [*MUTATION_CODE, ROOT / "evals" / "mutations_score.py"]:
        code = path.read_text(encoding="utf-8")
        assert "docs/gold" not in code
        assert "gold.yaml" not in code
        assert "evals.gold" not in code


def test_parse_marks_tables_lists_and_code() -> None:
    kinds = {ln.kind for ln in parse(OTHER_DOC + "\n```\nx = 1\n```\n")}

    assert {"heading", "list", "para", "table_head", "table_sep", "table", "code"} <= kinds
    assert set(CANDIDATES) == set(OPERATORS)


# --- Подсчёт ------------------------------------------------------------------------------------


def _finding(fid: str, prop: str, quote: str, kind: str = "pass") -> dict[str, Any]:
    return {"id": fid, "property": prop, "quotes": [{"text": quote}], "sources": [{"kind": kind}]}


def _scored(findings: list[dict[str, Any]]) -> list[score.MutationScore]:
    res = mutate(OTHER_DOC, ["M1", "M10"], seed=2)
    manifest = {
        "copy": "x",
        "kind": "multi",
        "copy_sha256": hashlib.sha256(res.text.encode()).hexdigest(),
        "mutations": [m.__dict__ for m in res.mutations],
    }
    items = score.load_findings({"verification": {"findings": findings}}, res.text)
    return [score.score_mutation("x", m, items) for m in manifest["mutations"]]


def test_finding_quoting_mutation_with_right_property_is_caught() -> None:
    res = mutate(OTHER_DOC, ["M1"], seed=2)
    [m] = res.mutations
    sentence = next(s for s in res.text.split("\n") if m.replacement in s)
    found = [_finding("F-01", "IND-VERIF", sentence.split("** ", 1)[-1])]

    [s] = [x for x in _scored(found) if x.operator == "M1"]

    assert s.status == score.CAUGHT
    assert s.by_model
    assert not s.by_lint


def test_right_place_wrong_property_is_counted_apart() -> None:
    res = mutate(OTHER_DOC, ["M1"], seed=2)
    [m] = res.mutations
    found = [_finding("F-01", "SET-MOD", m.replacement)]

    [s] = [x for x in _scored(found) if x.operator == "M1"]

    assert s.status == score.PLACE_ONLY


def test_lint_catch_is_not_counted_as_model_catch() -> None:
    found = [_finding("F-02", "IND-COMPL", "TBD", kind="lint")]

    [s] = [x for x in _scored(found) if x.operator == "M10"]

    assert s.status == score.CAUGHT
    assert s.by_lint
    assert not s.by_model


def test_finding_elsewhere_misses_mutation() -> None:
    found = [_finding("F-03", "IND-VERIF", "Бронь создана")]

    assert {s.status for s in _scored(found)} == {score.MISSED}


def test_operator_table_sums_and_recall() -> None:
    rows = score.by_operator(_scored([_finding("F-02", "IND-COMPL", "TBD", kind="lint")]))
    tot = score.total(rows)

    assert (tot.inserted, tot.caught, tot.by_lint, tot.by_model) == (2, 1, 1, 0)
    assert tot.recall == 0.5
    assert "Recall" in score.to_markdown(
        {
            "operators": [r.__dict__ for r in rows],
            "total": tot.__dict__,
            "copies": [],
            "interference": [],
        }
    )
