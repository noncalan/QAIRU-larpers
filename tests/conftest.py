"""Общие фикстуры тестов: документы из ``data/input/``, разбор маленьких документов, ``FakeLLM``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from validator.ingest import parse
from validator.llm import FakeLLM
from validator.models import DocKind, ParsedDocument

ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = ROOT / "data" / "input"
FIXTURES_LLM = ROOT / "tests" / "fixtures" / "llm"
FORMATS = (DocKind.MD, DocKind.PDF, DocKind.DOCX)
REQUIREMENTS = {kind: INPUT_DIR / f"requirements.{kind.value}" for kind in FORMATS}


@pytest.fixture(autouse=True)
def _fake_fixtures_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Фейковый клиент всегда берёт ответы из фикстур репозитория, а не из чужого окружения."""
    monkeypatch.setenv("VALIDATOR_FAKE_FIXTURES", str(FIXTURES_LLM))


@pytest.fixture(scope="session")
def requirements_docs() -> dict[DocKind, ParsedDocument]:
    """Проверяемый документ в трёх форматах, разобранный один раз на сессию."""
    return {kind: parse(path, kind) for kind, path in REQUIREMENTS.items()}


@pytest.fixture
def requirements_md(requirements_docs: dict[DocKind, ParsedDocument]) -> ParsedDocument:
    return requirements_docs[DocKind.MD]


@pytest.fixture
def make_md(tmp_path: Path) -> Callable[[str], ParsedDocument]:
    """Разобрать Markdown из строки: ``make_md("# Заголовок\\n\\nТекст.")``."""

    def _make(text: str) -> ParsedDocument:
        path = tmp_path / "doc.md"
        path.write_text(text, encoding="utf-8")
        return parse(path, DocKind.MD)

    return _make


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM(FIXTURES_LLM)
