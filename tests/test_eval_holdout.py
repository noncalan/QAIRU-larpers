"""Чужие документы (holdout, #48): лежат без правок, не утекли в агента, сводка собрана из прогонов."""

from __future__ import annotations

import hashlib
import re

import pytest

from conftest import ROOT
from evals.holdout import DATA, DOCS, SUMMARY, HoldoutDoc, render_summary
from evals.runs import load_document
from test_prompt_leak import SHINGLE, _package_files, _shingles, _words

README = DATA / "README.md"
SHA = re.compile(r"\b[0-9a-f]{64}\b")
DOC_IDS = [d.name for d in DOCS]


def _recorded_hashes() -> dict[str, str]:
    """SHA-256 из таблицы ``data/holdout/README.md``: строка таблицы начинается с пути к файлу."""
    out: dict[str, str] = {}
    for line in README.read_text(encoding="utf-8").splitlines():
        found = SHA.search(line)
        if line.startswith("| `") and found:
            out[line.split("`")[1]] = found.group(0)
    return out


@pytest.mark.parametrize("doc", DOCS, ids=DOC_IDS)
def test_holdout_document_is_unchanged_and_has_a_license(doc: HoldoutDoc) -> None:
    recorded = _recorded_hashes()

    actual = hashlib.sha256(doc.path.read_bytes()).hexdigest()

    assert recorded.get(f"{doc.name}/{doc.file}") == actual
    assert (doc.path.parent / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


@pytest.mark.parametrize("doc", DOCS, ids=DOC_IDS)
def test_no_six_word_sequence_from_holdout_in_package(doc: HoldoutDoc) -> None:
    """Агент не подогнан и под чужие документы: их фраз нет в промптах, чек-листах и правилах."""
    document = _shingles(_words(load_document(doc.path).text))
    leaks: dict[str, list[str]] = {}

    for path in _package_files():
        hits = document & _shingles(_words(path.read_text(encoding="utf-8")))
        if hits:
            leaks[str(path.relative_to(ROOT))] = [" ".join(h) for h in sorted(hits)[:3]]

    assert not leaks, f"Фразы документа {doc.name} в коде агента: {leaks}"


def test_holdout_leak_check_catches_a_planted_phrase() -> None:
    words = _words(load_document(DOCS[0].path).text)
    planted = "Ищи дефекты. " + " ".join(words[50 : 50 + SHINGLE]) + "."

    assert _shingles(words) & _shingles(_words(planted))


@pytest.mark.skipif(not SUMMARY.exists(), reason="прогонов на чужих документах в этой копии нет")
def test_summary_is_built_from_committed_runs() -> None:
    """Таблица в репозитории равна тому, что код считает по ``report.json`` и разметке."""
    assert render_summary() == SUMMARY.read_text(encoding="utf-8")
