"""Паспорт прогона: хеш промптов одинаков на всех ОС (#36, CI на Windows)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from validator.passport import prompts_sha256


def test_prompts_hash_orders_files_by_path_string_case_sensitive(tmp_path: Path) -> None:
    """На Windows ``Path`` сортируется без учёта регистра: ``README.md`` встал бы после ``a.md``."""
    files = {"b.md": b"second", "README.md": b"readme", "a.md": b"first"}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    expected = hashlib.sha256()
    for name in ("README.md", "a.md", "b.md"):
        expected.update(name.encode("utf-8") + b"\0" + files[name] + b"\0")

    assert prompts_sha256(tmp_path) == expected.hexdigest()


def test_prompts_hash_changes_when_any_prompt_changes(tmp_path: Path) -> None:
    (tmp_path / "search_system.md").write_text("Ищи дефекты.", encoding="utf-8")
    before = prompts_sha256(tmp_path)

    (tmp_path / "search_system.md").write_text("Ищи дефекты внимательно.", encoding="utf-8")

    assert prompts_sha256(tmp_path) != before
