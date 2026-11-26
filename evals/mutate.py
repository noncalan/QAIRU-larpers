"""Мутационное тестирование (issue #50): код вносит в копию документа дефект известного типа.

Операторы M1–M10 и нейтральные N1–N3 из исследования 20, раздел 3. Место для мутации ищется по форме
текста (число с единицей, пункт списка, строка таблицы, код HTTP, глагол обязательства), а не по
содержанию проверяемого документа (CLAUDE.md, правило 3). Эталон модуль не читает (правило 2).
Модель не вызывается: всё детерминированно от ``seed``.

Каждая мутация пишет в манифест исходный и новый фрагмент, смещения символов в итоговой копии и
``match_spans``: места, которые должна процитировать находка, чтобы мутация считалась пойманной.
Для вставок и замен это новый фрагмент, для удалений это соседний текст (удалённого текста в копии
нет, процитировать можно только место, где он был).

Запуск::

    uv run python -m evals.mutate data/input/requirements.md evals/mutations --prefix req
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from evals.mutation_ops import NEUTRAL_OPS, candidates, neutralize
from evals.mutation_text import SECTION_DEPTH, Edit, Mutation, apply_edits, to_mutations

# --- Копия с несколькими мутациями -------------------------------------------------------------


@dataclass
class Pin:
    """Закрепить место: та же правка, что в другой копии (сравнение «одна мутация против нескольких»)."""

    operator: str
    original: str
    replacement: str
    section: str


@dataclass
class CopyResult:
    name: str
    text: str
    mutations: list[Mutation]
    skipped: list[dict[str, str]]
    seed: int
    sites: set[tuple[str, str, str, str]]


def mutate(
    text: str,
    operators: Sequence[str],
    seed: int,
    name: str = "copy",
    pins: Sequence[Pin] = (),
    avoid: frozenset[tuple[str, str, str, str]] = frozenset(),
    depth: int = SECTION_DEPTH,
) -> CopyResult:
    """Вносит мутации в разные разделы. Оператор без свободного места попадает в ``skipped``.

    ``avoid``: места, уже занятые этим оператором в других копиях; их берём, только если других нет.
    """
    rng = random.Random(seed)  # noqa: S311 (детерминированный выбор места, не криптография)
    used: set[str] = set()
    edits: list[Edit] = []
    sites: set[tuple[str, str, str, str]] = set()
    skipped: list[dict[str, str]] = []
    current = text
    plan: list[tuple[str, Pin | None]] = [(p.operator, p) for p in pins]
    rest = list(operators)
    # редкие операторы первыми: у них меньше мест, а разделы общие
    rest.sort(key=lambda op: (len({c.section for c in candidates(op, text, depth) if c.tier == 0}), op))
    plan.extend((op, None) for op in rest)
    for op, pin in plan:
        found = [
            c
            for c in candidates(op, current, depth)
            if c.section and not (set(c.sections_used) & used) and _fits(current, c, pin)
        ]
        if not found:
            reason = "места нет" if not candidates(op, current, depth) else "свободного раздела нет"
            skipped.append(
                {"operator": op, "reason": reason if pin is None else "закреплённое место не найдено"}
            )
            continue
        fresh = [c for c in found if site_key(current, c) not in avoid] or found
        best = min(c.tier for c in fresh)
        edit = rng.choice([c for c in fresh if c.tier == best])
        sites.add(site_key(current, edit))
        used.update(edit.sections_used)
        edits.append(edit)
        current, _ = apply_edits(current, [edit])
    final, applied = apply_edits(text, edits)
    if final != current:  # защита от ошибки в пересчёте смещений
        raise AssertionError("пересчёт смещений разошёлся с последовательной правкой")
    return CopyResult(name, final, to_mutations(final, applied, name), skipped, seed, sites)


def site_key(text: str, edit: Edit) -> tuple[str, str, str, str]:
    """Место правки без смещений: оператор, исходный фрагмент, пометка и раздел-источник."""
    return edit.operator, text[edit.start : edit.end], edit.note, edit.sections_used[-1]


def _fits(text: str, edit: Edit, pin: Pin | None) -> bool:
    if pin is None:
        return True
    return (
        text[edit.start : edit.end] == pin.original
        and edit.new == pin.replacement
        and edit.section == pin.section
    )


# --- План для документа и запись на диск ------------------------------------------------------

MULTI_PLAN: tuple[tuple[str, ...], ...] = (
    ("M1", "M4", "M6", "M9", "M10"),
    ("M2", "M3", "M5", "M7", "M8"),
    ("M1", "M5", "M6", "M7", "M10"),
    ("M2", "M3", "M4", "M8", "M9"),
)
"""Четыре копии по пять мутаций: каждый оператор дважды, в разных копиях и разделах."""

SINGLE_FROM: tuple[tuple[int, str], ...] = ((1, "M1"), (1, "M6"), (2, "M5"))
"""Копии с одной мутацией: та же правка, что в многомутантной копии (номер копии, оператор)."""


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest(result: CopyResult, source: Path, source_text: str, kind: str) -> dict[str, object]:
    return {
        "schema": "mutations/1",
        "copy": result.name,
        "kind": kind,
        "source": source.as_posix(),
        "source_sha256": sha256(source_text),
        "copy_sha256": sha256(result.text),
        "seed": result.seed,
        "mutations": [asdict(m) for m in result.mutations],
        "skipped": result.skipped,
    }


def generate(
    source: Path, out_dir: Path, prefix: str, seed: int = 50, multi_only: int = 0, depth: int = SECTION_DEPTH
) -> list[Path]:
    """Пишет копии и манифесты. ``multi_only=N``: только первые N многомутантных копий плана."""
    text = source.read_text(encoding="utf-8")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    multi: list[CopyResult] = []
    taken: set[tuple[str, str, str, str]] = set()
    plan = MULTI_PLAN[:multi_only] if multi_only else MULTI_PLAN
    for i, ops in enumerate(plan, start=1):
        res = mutate(text, ops, seed + i, f"{prefix}-multi-{i}", avoid=frozenset(taken), depth=depth)
        taken |= res.sites
        multi.append(res)
        written += _write(out_dir, res, manifest(res, source, text, "multi"))
    if multi_only:
        return written
    for k, (copy_no, op) in enumerate(SINGLE_FROM, start=1):
        base = next((m for m in multi[copy_no - 1].mutations if m.operator == op), None)
        if base is None:
            continue
        pin = Pin(op, base.original, base.replacement, base.section)
        res = mutate(text, [], seed, f"{prefix}-single-{k}", pins=[pin], depth=depth)
        data = manifest(res, source, text, "single")
        data["same_as"] = base.id
        written += _write(out_dir, res, data)
    neutral_text, log = neutralize(text, list(NEUTRAL_OPS))
    name = f"{prefix}-neutral"
    (out_dir / f"{name}.md").write_text(neutral_text, encoding="utf-8")
    data = {
        "schema": "mutations/1",
        "copy": name,
        "kind": "neutral",
        "source": source.as_posix(),
        "source_sha256": sha256(text),
        "copy_sha256": sha256(neutral_text),
        "neutral": log,
        "mutations": [],
        "skipped": [],
    }
    (out_dir / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    written += [out_dir / f"{name}.md", out_dir / f"{name}.json"]
    return written


def _write(out_dir: Path, res: CopyResult, data: dict[str, object]) -> list[Path]:
    md, js = out_dir / f"{res.name}.md", out_dir / f"{res.name}.json"
    md.write_text(res.text, encoding="utf-8")
    js.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return [md, js]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", type=Path, help="исходный документ Markdown")
    parser.add_argument("out", type=Path, help="папка для копий и манифестов")
    parser.add_argument("--prefix", default="doc", help="префикс имён копий")
    parser.add_argument("--seed", type=int, default=50)
    parser.add_argument("--section-depth", type=int, default=SECTION_DEPTH, help="уровень заголовков раздела")
    parser.add_argument("--multi-only", type=int, default=0, help="только первые N копий с мутациями")
    args = parser.parse_args(argv)
    for path in generate(args.source, args.out, args.prefix, args.seed, args.multi_only, args.section_depth):
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
