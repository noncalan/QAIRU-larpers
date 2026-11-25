"""Эталон не утекает в агента (CLAUDE.md, правило 2; issue #24, «Чего не делать»).

Код агента в ``src/validator`` не импортирует ``evals`` и не ссылается на файлы эталона, а в его
промптах, шаблонах и правилах нет ни одной последовательности из 6 слов описаний дефектов эталона.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from conftest import ROOT
from evals.gold import load_gold

PACKAGE = ROOT / "src" / "validator"
TEXT_SUFFIXES = {".py", ".md", ".j2", ".txt", ".yaml", ".yml", ".json", ".toml", ".typ"}
GOLD_REFERENCES = re.compile(r"docs[/\\]gold|gold\.yaml|defects\.md|gold_segmentation|\bevals[/\\.]")
SHINGLE = 6
_WORD = re.compile(r"[0-9a-zа-я]+")


def _package_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES)


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


@pytest.mark.parametrize(
    "path", [p for p in _package_files() if p.suffix == ".py"], ids=lambda p: str(p.relative_to(PACKAGE))
)
def test_agent_code_does_not_import_evals(path: Path) -> None:
    imported = _imported_modules(path)

    assert not {m for m in imported if m == "evals" or m.startswith("evals.")}


@pytest.mark.parametrize("path", _package_files(), ids=lambda p: str(p.relative_to(PACKAGE)))
def test_agent_files_do_not_reference_gold(path: Path) -> None:
    found = sorted({m.group(0) for m in GOLD_REFERENCES.finditer(path.read_text(encoding="utf-8"))})

    assert not found, f"{path.relative_to(ROOT)} ссылается на эталон: {found}"


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("ё", "е"))


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = _words(text)
    return {tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def test_no_six_word_sequence_from_gold_descriptions_in_agent() -> None:
    gold = set().union(*(_shingles(f"{d.where}. {d.defect}") for d in load_gold().defects))
    leaks: dict[str, list[str]] = {}

    for path in _package_files():
        hits = gold & _shingles(path.read_text(encoding="utf-8"))
        if hits:
            leaks[str(path.relative_to(ROOT))] = [" ".join(h) for h in sorted(hits)[:3]]

    assert not leaks, f"Фразы эталона в коде агента: {leaks}"


def test_isolation_checks_see_the_whole_package() -> None:
    names = {p.relative_to(PACKAGE).as_posix() for p in _package_files()}

    assert {"pipeline.py", "models.py", "prompts/search_system.md"} <= names
