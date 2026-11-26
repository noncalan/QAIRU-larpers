"""Таблица «один промпт против агента» и стабильность между прогонами (issue #51).

Берёт готовые результаты ``evals.eval`` (A: базовая линия, B: агент выбранным профилем, при желании C:
агент с тремя проходами) и их папки прогонов. Судью и модель не вызывает: все вердикты из кеша,
сеть не нужна. Пишет:

- ``evals/results/<дата>-<имя>.md``: стабильность по прогонам, каждый прогон отдельной строкой;
- ``evals/results/summary.md``: одна таблица для README и слайдов;
- ``docs/img/<имя>.svg``: график для слайдов.

Прогоны не отбираются: в таблицу идёт каждый прогон, который есть в результатах eval.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import fmean, pstdev
from typing import Annotated, Any

import typer

from evals.chart import Bar, Group, render_svg
from evals.freetext import quote_segments
from evals.judge import EvalError, read_utf8
from evals.metrics import mean_pairwise_jaccard
from evals.runs import REPO_ROOT, RunData, find_agent_report, finding_keys, load_document, load_run
from validator.models import ParsedDocument, Report, Severity
from validator.text import locate_quote

ASTANA = timezone(timedelta(hours=5))
EMPTY = "—"
NOTE_NAME = "summary-note.md"
PRECISION_PENDING = "ждёт раунда #49"
RESULT_FIELDS = ("config", "runs", "settings", "name")


@dataclass(frozen=True)
class ConfigResult:
    """Результат eval одной конфигурации плюс то, что считается заново по папкам прогонов."""

    key: str
    title: str
    path: Path
    data: dict[str, Any]
    place_jaccard: float | None
    critical_keys: tuple[int, int] | None  # (найдены во всех прогонах, объединение): только агент
    precision: str | None = None  # строгая / мягкая по разметке #49, готовый текст ячейки

    @property
    def config(self) -> dict[str, Any]:
        config: dict[str, Any] = self.data["config"]
        return config

    @property
    def runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = self.data["runs"]
        return runs

    @property
    def is_agent(self) -> bool:
        return bool(self.runs[0]["source"] == "agent")


# ---------------------------------------------------------------- форматирование


def pct(value: float | None, digits: int = 1) -> str:
    return EMPTY if value is None else f"{value * 100:.{digits}f}".replace(".", ",") + " %"


def num(value: float | None, digits: int = 1) -> str:
    return EMPTY if value is None else f"{value:.{digits}f}".replace(".", ",")


def money(value: float | None) -> str:
    return EMPTY if value is None else "$" + f"{value:.2f}".replace(".", ",")


def seconds(value: float | None) -> str:
    return EMPTY if value is None else f"{value:.0f} с"


def spread(values: Sequence[float]) -> str:
    """«мин–макс, σ»: разброс числа находок между прогонами."""
    return f"{min(values):.0f}–{max(values):.0f}, σ {pstdev(values):.1f}".replace(".", ",")


# ---------------------------------------------------------------- загрузка и метрики


def _read_result(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(read_utf8(path, "Результат eval"))
    except ValueError as exc:
        raise EvalError(f"{path} не результат eval (не JSON)") from exc
    if not isinstance(data, dict) or any(f not in data for f in RESULT_FIELDS) or not data["runs"]:
        raise EvalError(f"{path} не результат evals.eval: нет полей {', '.join(RESULT_FIELDS)} или прогонов")
    return data


def _repo_path(label: str) -> Path:
    path = Path(label)
    return path if path.is_absolute() else REPO_ROOT / path


def place_set(run: RunData, doc: ParsedDocument) -> set[str]:
    """Блоки документа, которые покрывают цитаты находок (все блоки якоря): общая мера для обоих."""
    places: set[str] = set()
    for item in run.items:
        for segment in quote_segments(item.quote):
            anchor = locate_quote(doc, segment).anchor
            if anchor:
                places.update(anchor.block_ids)
    return places


def _critical_key_sets(run_dirs: Sequence[Path]) -> list[set[str]]:
    sets = []
    for folder in run_dirs:
        report_path = find_agent_report(folder)
        if report_path is None:
            raise EvalError(f"В прогоне {folder} нет отчёта агента")
        try:
            report = Report.model_validate_json(read_utf8(report_path, "Отчёт агента"))
        except ValueError as exc:
            raise EvalError(f"{report_path} не отчёт агента: {exc.__class__.__name__}") from exc
        sets.append(set(finding_keys(report, severities={Severity.CRITICAL})))
    return sets


def _precision_cell(entry: dict[str, Any]) -> str:
    consensus = entry["consensus"]
    labeled = consensus["labeled"]
    if not labeled:
        return EMPTY
    strict = consensus["real"] / labeled
    soft = (consensus["real"] + consensus["disputed"]) / labeled
    return f"{pct(strict)} / {pct(soft)} ᵖ"


def read_precision(path: Path) -> dict[str, str]:
    """Precision из ``evals.precision`` по согласию двух моделей: ключи ``agent`` и ``baseline``."""
    try:
        data = json.loads(read_utf8(path, "Результат precision"))
        by_run = data["by_run"]
    except (ValueError, KeyError, TypeError) as exc:
        raise EvalError(f"{path} не результат evals.precision (нет by_run)") from exc
    cells: dict[str, str] = {}
    for entry in by_run:
        name = str(entry.get("name", ""))
        if name.startswith("агент"):
            cells["agent"] = _precision_cell(entry)
        elif name.startswith("базовая линия"):
            cells["baseline"] = _precision_cell(entry)
    if not cells:
        raise EvalError(f"В {path} нет срезов «агент» и «базовая линия»")
    return cells


def load_config(key: str, title: str, path: Path, document: Path) -> ConfigResult:
    """Результат eval и метрики, которых в нём нет: Жаккар мест и повторяемость критичных находок агента."""
    data = _read_result(path)
    run_dirs = [_repo_path(r["label"]) for r in data["runs"]]
    doc = load_document(document)
    places = [place_set(load_run(d, document), doc) for d in run_dirs]
    critical: tuple[int, int] | None = None
    if data["runs"][0]["source"] == "agent":
        sets = _critical_key_sets(run_dirs)
        critical = (len(set.intersection(*sets)), len(set.union(*sets))) if sets else None
    return ConfigResult(
        key=key,
        title=title,
        path=path,
        data=data,
        place_jaccard=mean_pairwise_jaccard(places),
        critical_keys=critical,
    )


def live_wall_s(cfg: ConfigResult) -> float | None:
    """Среднее время прогона по тем прогонам, где все ответы живые (из кеша время не настоящее)."""
    live = [r["calls"]["wall_s"] for r in cfg.runs if not r["calls"]["reused"] and r["calls"]["wall_s"]]
    return fmean(live) if live else None


def unstable_gold(cfg: ConfigResult) -> list[int]:
    """Дефекты эталона, которые нашли не во всех прогонах конфигурации (found или partial)."""
    found = [{int(g) for g, s in r["gold"].items() if s in ("found", "partial")} for r in cfg.runs]
    return sorted(frozenset.union(*map(frozenset, found)) - frozenset.intersection(*map(frozenset, found)))


def questions(cfg: ConfigResult) -> dict[str, Any] | None:
    return cfg.config.get("questions")


# ---------------------------------------------------------------- сводная таблица


def _row(label: str, cells: Sequence[str]) -> str:
    return "| " + " | ".join([label, *cells]) + " |"


def _per_config(cfgs: Sequence[ConfigResult], fn: Callable[[ConfigResult], str]) -> list[str]:
    return [fn(c) for c in cfgs]


def precision_text(cfg: ConfigResult) -> str:
    return cfg.precision or PRECISION_PENDING


def critical_keys_text(cfg: ConfigResult) -> str:
    if cfg.critical_keys is None or cfg.critical_keys[1] == 0:
        return EMPTY
    stable, total = cfg.critical_keys
    return f"{stable} из {total}"


def _questions_text(cfg: ConfigResult) -> str:
    q = questions(cfg)
    return EMPTY if q is None else f"{num(q['total_after'])} (закрытые {pct(q['closed_after'], 0)})"


def summary_table(cfgs: Sequence[ConfigResult]) -> list[str]:
    """Строки по порядку из issue: доверие к находкам, стабильность, вопросы, цена; recall последним."""

    def c(fn: Callable[[ConfigResult], str]) -> list[str]:
        return _per_config(cfgs, fn)

    header = _row("Метрика", [x.title for x in cfgs])
    sep = "|---|" + "---|" * len(cfgs)
    return [
        header,
        sep,
        _row("**Доверие к находкам**", [""] * len(cfgs)),
        _row("Цитаты в отчёте есть в документе", c(lambda x: pct(x.config["quotes_final_verified"]))),
        _row("Цитаты дословно, без правок модели", c(lambda x: pct(x.config["quotes_exact"]))),
        _row("Шум (судья счёл находку пустой)", c(lambda x: pct(x.config["noise_share"]))),
        _row("Precision строгая / мягкая (две модели вслепую, не люди)", c(precision_text)),
        _row("**Стабильность между прогонами**", [""] * len(cfgs)),
        _row("Жаккар найденных дефектов эталона", c(lambda x: num(x.config["gold_jaccard"], 3))),
        _row("Жаккар мест документа в находках", c(lambda x: num(x.place_jaccard, 3))),
        _row("Жаккар находок (требование + свойство)", c(lambda x: num(x.config["key_jaccard"], 3))),
        _row("pass^k критичных дефектов эталона", c(lambda x: pct(x.config["critical_pass_all"], 0))),
        _row("Критичные находки агента, повторившиеся во всех прогонах", c(critical_keys_text)),
        _row("Число находок: мин–макс, σ", c(lambda x: spread([r["findings"] for r in x.runs]))),
        _row("**Ценность для автора ТЗ**", [""] * len(cfgs)),
        _row("Вопросы автору ТЗ на прогон", c(_questions_text)),
        _row("**Время и цена одного прогона**", [""] * len(cfgs)),
        _row("Время (живые прогоны)", c(lambda x: seconds(live_wall_s(x)))),
        _row("Цена по тарифу API", c(lambda x: money(x.config["cost_usd"]))),
        _row("**Полнота (оценка по эталону, который составлял Claude)**", [""] * len(cfgs)),
        _row("Критичные дефекты, найдено или частично", c(lambda x: pct(x.config["critical_recall"], 0))),
        _row("Критичные строго", c(lambda x: pct(x.config["critical_recall_strict"]))),
        _row("Recall, найдено или частично", c(lambda x: pct(x.config["recall"]))),
        _row("Recall строго", c(lambda x: pct(x.config["recall_strict"]))),
    ]


def render_summary(cfgs: Sequence[ConfigResult], note: str, precision_note: str = "") -> str:
    runs = ", ".join(f"{c.key} {len(c.runs)}" for c in cfgs)
    lines = [
        "# Один промпт против агента",
        "",
        f"{setup_text(cfgs[0])} Прогонов: {runs}. "
        "Все прогоны взяты, ни один не исключён. Подробности и каждый прогон отдельно: "
        + ", ".join(f"`{_rel(c.path)}`" for c in cfgs)
        + ".",
        "",
        *summary_table(cfgs),
        "",
        *([f"ᵖ {precision_note}", ""] if precision_note else []),
        note,
    ]
    return "\n".join(lines) + "\n"


def setup_text(cfg: ConfigResult) -> str:
    """Документ, судья и размер эталона из результата eval, а не из памяти."""
    st = cfg.data["settings"]
    return (
        f"Документ `{st.get('document', '?')}`, судья `{st.get('judge_model', '?')}` "
        f"`{st.get('judge_effort', '?')}`, один для всех, эталон из {st.get('gold_total', '?')} дефектов."
    )


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------- стабильность по прогонам


def render_stability(cfgs: Sequence[ConfigResult], title: str, now: datetime) -> str:
    out = [
        f"# {title}, {now:%Y-%m-%d %H:%M} (Астана)",
        "",
        "Метрики описаны в `evals/README.md`. Каждая строка это один прогон; исключённых прогонов нет.",
        "",
    ]
    for cfg in cfgs:
        out += _stability_section(cfg)
    out += [
        "## Как читать",
        "",
        "- **Жаккар дефектов эталона**: насколько совпадают наборы найденных дефектов эталона между "
        "прогонами. Применим к обеим конфигурациям.",
        "- **Жаккар мест документа**: то же по блокам документа, на которые указывают цитаты находок. "
        "Тоже применим к обеим, цитаты сверены `locate_quote`.",
        "- **Жаккар находок** считается по `stable_key` требования и свойству, только у агента: у свободного "
        "текста нет привязки к требованию.",
        "- **pass^k**: доля дефектов (находок), найденных во всех k прогонах: жюри видит один прогон.",
        "",
    ]
    return "\n".join(out)


def _stability_section(cfg: ConfigResult) -> list[str]:
    head = (
        "| Прогон | Находок | Recall | Строго | Критичные строго | Цитаты в отчёте | "
        "Шум | Время | Цена |\n|---|---|---|---|---|---|---|---|---|"
    )
    rows = [
        f"| `{r['label']}` | {r['findings']} | {pct(r['recall'])} | {pct(r['recall_strict'])} | "
        f"{pct(r['critical_recall_strict'])} | {pct(_final_share(r))} | {pct(r['noise_share'])} | "
        f"{seconds(r['calls']['wall_s'])}{'' if not r['calls']['reused'] else ' ᵃ'} | "
        f"{money(r['calls']['cost_usd'])} |"
        for r in cfg.runs
    ]
    lines = [f"## {cfg.title}", "", f"Результат: `{_rel(cfg.path)}`", "", head, *rows, ""]
    if any(r["calls"]["reused"] for r in cfg.runs):
        lines += [
            "ᵃ часть ответов взята из кеша своего прогона после сброса лимита: время не живое, цена полная.",
            "",
        ]
    findings = [r["findings"] for r in cfg.runs]
    lines += [
        f"- Число находок: {spread(findings)}.",
        f"- Жаккар дефектов эталона {num(cfg.config['gold_jaccard'], 3)}, "
        f"мест документа {num(cfg.place_jaccard, 3)}, находок {num(cfg.config['key_jaccard'], 3)}.",
        f"- pass^k критичных дефектов эталона {pct(cfg.config['critical_pass_all'], 0)}; "
        f"критичных находок агента, найденных во всех прогонах: {critical_keys_text(cfg)}.",
        f"- Дефекты эталона, найденные не во всех прогонах: "
        f"{', '.join(map(str, unstable_gold(cfg))) or 'таких нет'}.",
        "",
    ]
    return lines


def _final_share(run: dict[str, Any]) -> float | None:
    q = run.get("quotes_final") or run["quotes"]
    return (q["exact"] + q["fuzzy"]) / q["total"] if q["total"] else None


# ---------------------------------------------------------------- график


def chart_subtitle(cfg: ConfigResult) -> str:
    st = cfg.data["settings"]
    name = Path(str(st.get("document", "?"))).name
    return f"Документ {name}, эталон {st.get('gold_total', '?')} дефектов, судья один для всех."


def chart_groups(cfgs: Sequence[ConfigResult]) -> list[Group]:
    """Одна простая картинка: только A и B, шесть метрик, среди них те, где выигрывает A."""

    def bars(fn: Callable[[ConfigResult], float | None]) -> list[Bar]:
        return [Bar(c.title.split(" (")[0], fn(c)) for c in cfgs[:2]]

    return [
        Group("Цитаты есть в документе", bars(lambda c: c.config["quotes_final_verified"])),
        Group("Цитаты дословно", bars(lambda c: c.config["quotes_exact"])),
        Group("Совпадение дефектов между прогонами", bars(lambda c: c.config["gold_jaccard"])),
        Group("Совпадение мест между прогонами", bars(lambda c: c.place_jaccard)),
        Group("Критичные дефекты найдены", bars(lambda c: c.config["critical_recall"])),
        Group("Recall по эталону", bars(lambda c: c.config["recall"])),
    ]


# ---------------------------------------------------------------- CLI

app = typer.Typer(add_completion=False, help="Таблица «один промпт против агента» и стабильность (#51).")


@app.command()
def main(
    baseline: Annotated[Path, typer.Option("--a", help="Результат eval базовой линии")],
    agent: Annotated[Path, typer.Option("--b", help="Результат eval агента выбранным профилем")],
    agent_alt: Annotated[Path | None, typer.Option("--c", help="Результат eval агента, 3 прохода")] = None,
    precision: Annotated[
        Path | None, typer.Option(help="Результат evals.precision (#49): заполняет строку precision")
    ] = None,
    precision_for: Annotated[
        str, typer.Option(help="Какой конфигурации агента принадлежит прогон из precision: B или C")
    ] = "B",
    precision_note: Annotated[
        str, typer.Option(help="Сноска к строке precision: на каком прогоне и сколько проходов")
    ] = "",
    name: Annotated[str, typer.Option(help="Имя: <дата>-<имя>.md и docs/img/<имя>.svg")] = "stability",
    title: Annotated[str, typer.Option(help="Заголовок файла стабильности")] = "Стабильность",
    note_file: Annotated[
        Path | None,
        typer.Option(help="Markdown под таблицей: выводы и оговорки; по умолчанию summary-note.md рядом"),
    ] = None,
    document: Annotated[Path, typer.Option(help="Документ эталона")] = REPO_ROOT
    / "data/input/requirements.md",
    results_dir: Annotated[Path, typer.Option(help="Куда писать .md")] = REPO_ROOT / "evals/results",
    img_dir: Annotated[Path, typer.Option(help="Куда писать график")] = REPO_ROOT / "docs/img",
) -> None:
    try:
        cfgs = [
            load_config("A", "Один промпт (A)", baseline, document),
            load_config("B", "Агент, 1 проход (B)", agent, document),
        ]
        if agent_alt is not None:
            cfgs.append(load_config("C", "Агент, 3 прохода (C)", agent_alt, document))
        cells = read_precision(precision) if precision is not None else {}
        cfgs = [
            replace(
                c,
                precision=cells.get(
                    "baseline" if c.key == "A" else "agent" if c.key == precision_for else ""
                ),
            )
            for c in cfgs
        ]
        default_note = results_dir / NOTE_NAME
        used_note = note_file or (default_note if default_note.is_file() else None)
        note = read_utf8(used_note, "Примечание") if used_note is not None else ""
    except EvalError as exc:
        typer.echo(f"Ошибка: {exc}", err=True)
        raise typer.Exit(2) from exc
    now = datetime.now(ASTANA)
    results_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)
    stability = results_dir / f"{now:%Y-%m-%d}-{name}.md"
    stability.write_text(render_stability(cfgs, title, now), encoding="utf-8")
    (results_dir / "summary.md").write_text(
        render_summary(cfgs, note, precision_note if cells else ""), encoding="utf-8"
    )
    svg = render_svg(
        "Один промпт против агента",
        "Выше лучше. " + chart_subtitle(cfgs[0]),
        chart_groups(cfgs),
    )
    (img_dir / f"{name}.svg").write_text(svg, encoding="utf-8")
    typer.echo(f"Записано: {_rel(stability)}, {_rel(results_dir / 'summary.md')}, docs/img/{name}.svg")


if __name__ == "__main__":
    app()
