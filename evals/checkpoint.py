"""Контрольная точка #37: таблица конфигураций A, B, B′, C и правило D-011 кодом.

``python -m evals.checkpoint --a A.json --b B.json --b-raw B_RAW.json --c C.json [--name checkpoint]``

На входе результаты ``evals.eval`` по каждой конфигурации протокола (``docs/plan.md``, раздел «Протокол
eval и правило D-011»): A базовая линия, B агент с 1 проходом ``medium``, B′ то же на тексте без ID
блоков, C агент с 3 проходами ``medium``. На выходе ``evals/results/<дата>-<имя>.json`` и ``.md``:
таблица метрик и решение по каждому пункту правила.

Пороги ниже переписаны из плана дословно и записаны до прогонов. Менять их после того, как увидели
цифры, нельзя (правило D-011, п. 4): если правило кажется неверным, это пишут в D-011 отдельно, а решение
принимают по записанному правилу.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from evals.eval import ASTANA, DEFAULT_RESULTS, NAME_PATTERN, EvalResult
from evals.judge import display_path
from evals.metrics import ConfigScore
from evals.report import money, num, pct
from validator.models import SearchInput

SCHEMA_VERSION = "1"

JACCARD_GAIN = 0.10
"""П. 1: Жаккар находок у C выше, чем у B, на 0,10 и больше."""
CRITICAL_GAIN = 0.10
"""П. 1: recall критичных у C выше на 10 п. п. и больше."""
RECALL_GAIN = 0.05
"""П. 1: recall всего у C выше на 5 п. п. и больше."""
BLOCKS_LOSS = 0.05
"""П. 2: recall у B ниже, чем у B′, больше чем на 5 п. п.: поиск получает текст без ID блоков."""
BASELINE_LOSS = 0.05
"""П. 3: recall выбранной конфигурации ниже, чем у A, больше чем на 5 п. п.: доработка промпта поиска."""
EPS = 1e-9
"""Допуск сравнения: средние по прогонам это дроби вида k/135, «0,10 и больше» не должно ломаться о 0,0999…"""

LABELS = ("A", "B", "B′", "C")
TITLES = {
    "A": "Базовая линия, один промпт",
    "B": "Агент, 1 проход medium",
    "B′": "Агент, 1 проход medium, текст без ID блоков",
    "C": "Агент, 3 прохода medium",
}


class Check(BaseModel):
    """Одно условие правила: что сравнили, с какими числами и выполнено ли оно."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    met: bool


class Item(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    title: str
    checks: list[Check]
    met: bool = Field(description="Выполнено ли условие пункта: 3 прохода, сырой текст, доработка #14")
    conclusion: str


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    passes: int = Field(description="Число проходов по умолчанию для standard и submit")
    search_input: SearchInput
    chosen: str = Field(description="Конфигурация, которую сравниваем с базовой линией (п. 3)")
    reopen_search_prompt: bool = Field(description="П. 3: открыть #14 до слоя доказательств")
    items: list[Item]
    notes: list[str] = Field(default_factory=list)


class CheckpointResult(BaseModel):
    """Содержимое ``evals/results/<дата>-<имя>.json``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    name: str
    created_at: datetime
    sources: dict[str, str] = Field(description="Конфигурация → файл результата evals.eval")
    configs: dict[str, ConfigScore]
    decision: Decision


# ---------------------------------------------------------------- правило


def _pp(value: float) -> str:
    """Разница долей в процентных пунктах со знаком: «+4,4 п. п.»."""
    return f"{100 * value:+.1f} п. п.".replace(".", ",", 1).replace("-", "−", 1)


def _gain(name: str, new: float | None, old: float | None, need: float, *, points: bool) -> Check:
    """«C выше B на need и больше». Нет числа: условие не выполнено, а в тексте видно почему."""
    if new is None or old is None:
        fmt = pct if points else (lambda v: num(v, 3))
        return Check(text=f"{name}: нет данных (C {fmt(new)}, B {fmt(old)})", met=False)
    diff = new - old
    if points:
        values = f"C {pct(new)}, B {pct(old)}, разница {_pp(diff)}, нужно ≥ {_pp(need)}"
    else:
        shown = f"разница {diff:+.3f}, нужно ≥ +{need:.2f}".replace(".", ",")
        values = f"C {num(new, 3)}, B {num(old, 3)}, {shown}"
    return Check(text=f"{name}: {values}", met=diff >= need - EPS)


def _three_passes(b: ConfigScore, c: ConfigScore) -> Item:
    quotes_ok = (
        c.quotes_verified is not None
        and b.quotes_verified is not None
        and c.quotes_verified >= b.quotes_verified - EPS
    )
    quotes = Check(
        text=(
            f"Доля сверенных цитат модели (до фильтра, точно или нечётко) у C не ниже, чем у B: "
            f"C {pct(c.quotes_verified)}, B {pct(b.quotes_verified)}"
        ),
        met=quotes_ok,
    )
    gains = [
        _gain("Жаккар находок", c.key_jaccard, b.key_jaccard, JACCARD_GAIN, points=False),
        _gain("Recall критичных", c.critical_recall, b.critical_recall, CRITICAL_GAIN, points=True),
        _gain("Recall всего", c.recall, b.recall, RECALL_GAIN, points=True),
    ]
    three = quotes.met and any(g.met for g in gains)
    why = (
        "цитаты не хуже и есть прирост хотя бы по одной метрике"
        if three
        else ("доля сверенных цитат у C ниже, чем у B" if not quotes.met else "ни одного прироста до порога")
    )
    conclusion = (
        f"Три прохода по умолчанию (profiles standard и submit): {why}."
        if three
        else f"По умолчанию один проход, `--passes 3` остаётся опцией: {why}."
    )
    return Item(
        number=1,
        title="Число проходов по умолчанию (C против B)",
        checks=[quotes, *gains],
        met=three,
        conclusion=conclusion,
    )


def _blocks(b: ConfigScore, b_raw: ConfigScore) -> Item:
    if b.recall is None or b_raw.recall is None:
        check = Check(text=f"Recall: нет данных (B {pct(b.recall)}, B′ {pct(b_raw.recall)})", met=False)
    else:
        loss = b_raw.recall - b.recall
        check = Check(
            text=(
                f"Recall у B ниже, чем у B′, больше чем на {_pp(BLOCKS_LOSS)[1:]}: "
                f"B {pct(b.recall)}, B′ {pct(b_raw.recall)}, B′ − B = {_pp(loss)}"
            ),
            met=loss > BLOCKS_LOSS + EPS,
        )
    conclusion = (
        "Поиск получает текст без ID блоков (`search_input = raw`), блоки находит код по цитате."
        if check.met
        else "Разметка блоков остаётся (`search_input = blocks`): она не мешает поиску по порогу правила."
    )
    return Item(
        number=2, title="Разметка блоков (B против B′)", checks=[check], met=check.met, conclusion=conclusion
    )


def _chosen(passes: int, search_input: SearchInput) -> tuple[str, list[str]]:
    if passes == 3:
        notes = (
            ["C с текстом без ID блоков не мерили: с базовой линией сравниваем C на блоках."]
            if search_input is SearchInput.RAW
            else []
        )
        return "C", notes
    return ("B′" if search_input is SearchInput.RAW else "B"), []


def _baseline(a: ConfigScore, chosen: str, score: ConfigScore) -> Item:
    if a.recall is None or score.recall is None:
        check = Check(text=f"Recall: нет данных (A {pct(a.recall)}, {chosen} {pct(score.recall)})", met=False)
    else:
        loss = a.recall - score.recall
        check = Check(
            text=(
                f"Recall выбранной конфигурации {chosen} ниже, чем у A, больше чем на "
                f"{_pp(BASELINE_LOSS)[1:]}: {chosen} {pct(score.recall)}, A {pct(a.recall)}, "
                f"{chosen} − A = {_pp(-loss)}"
            ),
            met=loss > BASELINE_LOSS + EPS,
        )
    conclusion = (
        "Первая задача после 14:00: доработка промпта поиска, #14 открываем снова (до слоя доказательств)."
        if check.met
        else "Доработка промпта поиска до слоя доказательств не нужна: проигрыша A сверх порога нет."
    )
    return Item(
        number=3,
        title=f"Агент против базовой линии ({chosen} против A)",
        checks=[check],
        met=check.met,
        conclusion=conclusion,
    )


def decide(a: ConfigScore, b: ConfigScore, b_raw: ConfigScore, c: ConfigScore) -> Decision:
    """Правило D-011 из ``docs/plan.md`` пункт за пунктом, без поправок на увиденные цифры."""
    first = _three_passes(b, c)
    passes = 3 if first.met else 1
    second = _blocks(b, b_raw)
    search_input = SearchInput.RAW if second.met else SearchInput.BLOCKS
    chosen, notes = _chosen(passes, search_input)
    third = _baseline(a, chosen, {"B": b, "B′": b_raw, "C": c}[chosen])
    return Decision(
        passes=passes,
        search_input=search_input,
        chosen=chosen,
        reopen_search_prompt=third.met,
        items=[first, second, third],
        notes=notes,
    )


# ---------------------------------------------------------------- отчёт


ROWS: tuple[tuple[str, str], ...] = (
    ("Прогонов", "runs"),
    ("Recall (found + partial)", "recall"),
    ("Recall строгий (только found)", "recall_strict"),
    ("Критичные (found + partial)", "critical_recall"),
    ("Критичные строго", "critical_recall_strict"),
    ("Объединение прогонов (с частичными)", "union_recall"),
    ("pass^k критичных (найден во всех прогонах)", "critical_pass_all"),
    ("Находок в среднем", "findings"),
    ("Шум по судье", "noise_share"),
    ("Цитаты модели дословно", "quotes_exact"),
    ("Цитаты модели сверены (точно или нечётко)", "quotes_verified"),
    ("Цитаты итоговых находок сверены", "quotes_final_verified"),
    ("Жаккар находок (stable_key + свойство)", "key_jaccard"),
    ("Жаккар найденных дефектов эталона", "gold_jaccard"),
    ("Время прогона, с", "wall_s"),
    ("Цена прогона по тарифу API, $", "cost_usd"),
)
SHARES = {
    "recall",
    "recall_strict",
    "critical_recall",
    "critical_recall_strict",
    "union_recall",
    "critical_pass_all",
    "noise_share",
    "quotes_exact",
    "quotes_verified",
    "quotes_final_verified",
}


def _value(score: ConfigScore, field: str) -> str:
    value = getattr(score, field)
    if field == "runs":
        return str(value)
    if field in SHARES:
        return pct(value)
    if field in {"key_jaccard", "gold_jaccard"}:
        return num(value, 3)
    if field == "cost_usd":
        return money(value)
    return num(value, 0 if field == "wall_s" else 1)


def _yes(flag: bool) -> str:
    return "да" if flag else "нет"


def render_markdown(result: CheckpointResult) -> str:
    d = result.decision
    labels = [label for label in LABELS if label in result.configs]
    out = [
        f"# Контрольная точка D-011, {result.created_at:%Y-%m-%d %H:%M} (Астана)",
        "",
        "Протокол и правило: `docs/plan.md`, раздел «Протокол eval и правило D-011» (записано до прогонов). "
        "Метрики: `evals/README.md`. Recall считает судья по эталону, который тоже составлял Claude: "
        "это оценка полноты, а не precision.",
        "",
        "## Конфигурации",
        "",
        "| | Что это | Результат eval |",
        "|---|---|---|",
        *(f"| {label} | {TITLES[label]} | `{result.sources[label]}` |" for label in labels),
        "",
        "## Метрики",
        "",
        "| Метрика | " + " | ".join(labels) + " |",
        "|---|" + "---|" * len(labels),
        *(
            f"| {title} | " + " | ".join(_value(result.configs[label], field) for label in labels) + " |"
            for title, field in ROWS
        ),
        "",
        "## Правило D-011 пункт за пунктом",
        "",
    ]
    for item in d.items:
        out += [f"### {item.number}. {item.title}", ""]
        out += [f"- {'✓' if c.met else '✗'} {c.text}" for c in item.checks]
        out += ["", f"**Вывод:** {item.conclusion}", ""]
    out += [
        "## Решение",
        "",
        f"- Проходов по умолчанию (standard и submit): **{d.passes}**.",
        f"- Вход поиска: **{d.search_input.value}**.",
        f"- Доработка промпта поиска до слоя доказательств (#14): **{_yes(d.reopen_search_prompt)}**.",
        *(f"- {note}" for note in d.notes),
        "",
    ]
    return "\n".join(out)


def build(sources: dict[str, Path], *, name: str, now: datetime | None = None) -> CheckpointResult:
    configs = {
        label: EvalResult.model_validate_json(path.read_text(encoding="utf-8")).config
        for label, path in sources.items()
    }
    return CheckpointResult(
        name=name,
        created_at=(now or datetime.now(ASTANA)).astimezone(ASTANA),
        sources={label: display_path(path) for label, path in sources.items()},
        configs=configs,
        decision=decide(configs["A"], configs["B"], configs["B′"], configs["C"]),
    )


def write_result(result: CheckpointResult, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{result.created_at:%Y-%m-%d}-{result.name}"
    if (out_dir / f"{stem}.json").exists() or (out_dir / f"{stem}.md").exists():
        stem += f"-{result.created_at:%H%M%S}"
    json_path, md_path = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    json_path.write_text(result.model_dump_json(indent=1) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evals.checkpoint", description="Таблица A, B, B′, C и правило D-011 (issue #37)."
    )
    p.add_argument("--a", type=Path, required=True, help="результат eval базовой линии")
    p.add_argument("--b", type=Path, required=True, help="результат eval агента, 1 проход")
    p.add_argument("--b-raw", type=Path, required=True, help="результат eval агента, 1 проход без ID блоков")
    p.add_argument("--c", type=Path, required=True, help="результат eval агента, 3 прохода")
    p.add_argument("--name", default="checkpoint")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not NAME_PATTERN.match(args.name):
        print(f"Ошибка: имя «{args.name}» не годится для файла", file=sys.stderr)
        return 2
    sources = {"A": args.a, "B": args.b, "B′": args.b_raw, "C": args.c}
    try:
        result = build(sources, name=args.name)
        json_path, md_path = write_result(result, args.out_dir)
    except (OSError, ValueError) as exc:
        print(f"Ошибка контрольной точки: {exc}", file=sys.stderr)
        return 1
    d = result.decision
    print(
        f"D-011: проходов {d.passes}, вход поиска {d.search_input.value}, "
        f"доработка промпта поиска: {_yes(d.reopen_search_prompt)}"
    )
    print(f"Результаты: {display_path(json_path)}, {display_path(md_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
