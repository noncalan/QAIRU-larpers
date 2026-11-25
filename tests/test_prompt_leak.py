"""Проверяемый документ и эталон не утекают в код агента (CLAUDE.md, правила 2 и 3).

Промпты, чек-листы и правила линтера должны быть общими: ни одной последовательности из 6 слов
проверяемого документа или эталона ``docs/gold/defects.md`` и ни одного слова из списка «хардкода
под документ». Проверяем весь пакет ``src/validator``, а не только ``prompts/``, ``checklists/`` и
``lint/``: так строже и дешевле. Эталон здесь читает только тест (это код eval, правило 2 это
разрешает); при падении тест показывает совпавшие слова из кода агента, а не текст эталона.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import REQUIREMENTS, ROOT
from validator.models import DocKind

PACKAGE = ROOT / "src" / "validator"
GUARDED_DIRS = ("prompts", "checklists", "lint")
TEXT_SUFFIXES = {".py", ".md", ".j2", ".txt", ".yaml", ".yml", ".json", ".toml"}
GOLD = ROOT / "docs" / "gold" / "defects.md"
SHINGLE = 6
# Ссылки на пункты стандартов общие для эталона и кода агента («ISO/IEC/IEEE 29148:2018, п. 5.2.5»,
# «IEEE 830-1998, п. 4.3.2»): перед сравнением с эталоном убираем их с обеих сторон.
STANDARD_REF = re.compile(
    r"(?:ISO/IEC/IEEE|ISO|IEEE)\s*(?:29148|830)(?:[-:]\d{4})?"
    r"(?:\s*,?\s*(?:пп?\.|§|раздел|clause)?\s*\d+(?:\.\d+)*[a-z]?)?",
    re.IGNORECASE,
)
FORBIDDEN = re.compile(
    r"порог|тиын|\bФ-\d|AlertsController|saveLimit|limit_kzt|cardId|Алмат",
    re.IGNORECASE,
)
_WORD = re.compile(r"[0-9a-zа-я]+")


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower().replace("ё", "е"))


def _shingles(words: list[str]) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def _package_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES)


def test_guarded_dirs_exist() -> None:
    for name in GUARDED_DIRS:
        assert (PACKAGE / name).is_dir(), name


def test_no_six_word_sequence_from_document_in_package() -> None:
    document = _shingles(_words(REQUIREMENTS[DocKind.MD].read_text(encoding="utf-8")))
    leaks: dict[str, list[str]] = {}

    for path in _package_files():
        hits = document & _shingles(_words(path.read_text(encoding="utf-8")))
        if hits:
            leaks[str(path.relative_to(ROOT))] = [" ".join(h) for h in sorted(hits)[:3]]

    assert not leaks, f"Фразы проверяемого документа в коде агента: {leaks}"


@pytest.mark.skipif(not GOLD.exists(), reason="эталона нет в этой копии репозитория")
def test_no_six_word_sequence_from_gold_in_package() -> None:
    gold = _shingles(_words(STANDARD_REF.sub(" ", GOLD.read_text(encoding="utf-8"))))
    leaks: dict[str, int] = {}
    shown: dict[str, list[str]] = {}

    for path in _package_files():
        hits = gold & _shingles(_words(STANDARD_REF.sub(" ", path.read_text(encoding="utf-8"))))
        if hits:
            name = str(path.relative_to(ROOT))
            leaks[name] = len(hits)
            shown[name] = [" ".join(h) for h in sorted(hits)[:3]]

    assert not leaks, (
        f"Фразы эталона в коде агента (число совпадений по файлам {leaks}; слова из кода: {shown})"
    )


@pytest.mark.skipif(not GOLD.exists(), reason="эталона нет в этой копии репозитория")
def test_gold_check_catches_a_planted_phrase() -> None:
    """Сам тест работает: фраза эталона в «промпте» ловится (текст эталона не печатаем)."""
    gold_words = _words(STANDARD_REF.sub(" ", GOLD.read_text(encoding="utf-8")))
    planted = "Ищи дефекты. " + " ".join(gold_words[200 : 200 + SHINGLE]) + "."

    assert _shingles(gold_words) & _shingles(_words(planted))


def test_standard_references_are_ignored_in_gold_check() -> None:
    text = "Нарушено: ISO/IEC/IEEE 29148:2018, п. 5.2.5; IEEE 830-1998, п. 4.3.2. Дальше текст."

    assert _words(STANDARD_REF.sub(" ", text)) == ["нарушено", "дальше", "текст"]


@pytest.mark.parametrize("path", _package_files(), ids=lambda p: str(p.relative_to(PACKAGE)))
def test_no_document_specific_words(path: Path) -> None:
    found = sorted({m.group(0) for m in FORBIDDEN.finditer(path.read_text(encoding="utf-8"))})

    assert not found, f"{path.relative_to(ROOT)}: слова под конкретный документ {found}"


def test_leak_check_catches_a_planted_phrase() -> None:
    """Сам тест работает: фраза из документа в «промпте» ловится."""
    document_words = _words(REQUIREMENTS[DocKind.MD].read_text(encoding="utf-8"))
    planted = "Ищи дефекты. " + " ".join(document_words[100 : 100 + SHINGLE]) + "."

    assert _shingles(document_words) & _shingles(_words(planted))
