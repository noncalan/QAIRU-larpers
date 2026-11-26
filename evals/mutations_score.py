"""Подсчёт пойманных мутаций (issue #50): только код, без модели и без эталона.

Мутация поймана, если у прогона агента на копии есть находка (после верификатора), цитата которой
пересекается с одним из ``match_spans`` манифеста, и свойство находки входит в ожидаемые свойства
оператора. Если место совпало, а свойство другое, это «поймано по месту». Находки линтера и модели
считаем раздельно: линтер ловит часть форм без модели (TBD, «и/или», размытые слова), и без этого
разделения recall агента выглядит выше, чем даёт модель.

Шум: находки копии, которые не касаются ни одной мутации и которых нет в прогоне исходного документа
(ключ: свойство + нормализованные цитаты). Жаккар тех же ключей с исходником показывает, насколько
находки сдвигаются от правки документа; у нейтральной копии это мера устойчивости (исследование 20).

Запуск::

    uv run python -m evals.mutations_score evals/mutations evals/results/mutations/<папка прогонов> \\
        --out evals/results/2026-11-26-mutations

Прогон исходного документа лежит рядом с прогонами копий: ``<папка прогонов>/<документ>-original``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from validator.text import normalize

CAUGHT = "caught"
PLACE_ONLY = "place_only"
MISSED = "missed"
LINT = "lint"
MODEL = "model"


@dataclass(frozen=True)
class FoundItem:
    id: str
    prop: str
    sources: frozenset[str]  # {"lint"}, {"model"} или оба
    spans: tuple[tuple[int, int], ...]
    quotes: tuple[str, ...]
    title: str

    @property
    def key(self) -> str:
        return self.prop + "|" + "|".join(sorted(self.places))

    @property
    def places(self) -> frozenset[str]:
        """Процитированные места без свойства: нормализованный текст каждой цитаты."""
        return frozenset(normalize(q)[0] for q in self.quotes)


@dataclass
class MutationScore:
    copy: str
    id: str
    operator: str
    name: str
    status: str
    by_lint: bool
    by_model: bool
    by_model_place: bool
    findings: list[str]
    properties: list[str]
    titles: list[str]
    replacement: str
    original: str
    line: int


@dataclass
class CopyScore:
    copy: str
    kind: str
    run_dir: str
    findings: int
    findings_lint_only: int
    noise: int
    noise_new_place: int
    jaccard_with_original: float | None
    jaccard_places: float | None
    cost_usd: float | None
    duration_s: float | None
    agent_version: str | None
    mutations: list[MutationScore] = field(default_factory=list)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def report_path(run_dir: Path) -> Path:
    for name in ("report.json", f"{run_dir.name}.json"):
        if (run_dir / name).is_file():
            return run_dir / name
    found = sorted(p for p in run_dir.glob("*.json") if p.name != "questions.json")
    if not found:
        raise FileNotFoundError(f"в {run_dir} нет report.json")
    return found[0]


def _locate(doc: str, quote: str) -> list[tuple[int, int]]:
    """Все вхождения цитаты в документ после ``normalize``: смещения в исходном тексте."""
    norm_doc, index = normalize(doc)
    norm_q = normalize(quote)[0]
    if not norm_q:
        return []
    out = []
    pos = norm_doc.find(norm_q)
    while pos >= 0:
        end = pos + len(norm_q) - 1
        out.append((index[pos], index[end] + 1))
        pos = norm_doc.find(norm_q, pos + 1)
    return out


def load_findings(report: dict[str, Any], doc: str) -> list[FoundItem]:
    items = []
    for f in report.get("verification", {}).get("findings", []):
        kinds = {LINT if s.get("kind") == "lint" else MODEL for s in f.get("sources", [])} or {MODEL}
        spans: list[tuple[int, int]] = []
        texts: list[str] = []
        for q in f.get("quotes", []):
            text = q.get("located") or q.get("text") or ""
            texts.append(text)
            anchor = q.get("anchor") or {}
            start, end = anchor.get("char_start"), anchor.get("char_end")
            if isinstance(start, int) and isinstance(end, int) and doc[start:end].strip():
                spans.append((start, end))
            spans.extend(_locate(doc, text))
        items.append(
            FoundItem(
                id=str(f.get("id")),
                prop=str(f.get("property")),
                sources=frozenset(kinds),
                spans=tuple(sorted(set(spans))),
                quotes=tuple(texts),
                title=str(f.get("title") or ""),
            )
        )
    return items


def _overlaps(spans: Iterable[tuple[int, int]], targets: Iterable[Sequence[int]]) -> bool:
    targets = list(targets)
    return any(a < t[1] and t[0] < b for a, b in spans for t in targets)


def score_mutation(copy: str, m: dict[str, Any], findings: Sequence[FoundItem]) -> MutationScore:
    expected = set(m["expected_properties"])
    hits = [f for f in findings if _overlaps(f.spans, m["match_spans"])]
    strict = [f for f in hits if f.prop in expected]
    status = CAUGHT if strict else (PLACE_ONLY if hits else MISSED)
    return MutationScore(
        copy=copy,
        id=m["id"],
        operator=m["operator"],
        name=m["name"],
        status=status,
        by_lint=any(LINT in f.sources for f in strict),
        by_model=any(MODEL in f.sources for f in strict),
        by_model_place=any(MODEL in f.sources for f in hits),
        findings=[f.id for f in hits],
        properties=sorted({f.prop for f in hits}),
        titles=[f"{f.id} {f.prop}: {f.title}" for f in hits],
        replacement=m["replacement"],
        original=m["original"],
        line=m["line"],
    )


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


@dataclass(frozen=True)
class Baseline:
    """Находки прогона исходного документа: с ними сравниваем копии."""

    keys: frozenset[str]
    places: frozenset[str]


def score_copy(manifest: dict[str, Any], doc: str, run_dir: Path, original: Baseline | None) -> CopyScore:
    if hashlib.sha256(doc.encode("utf-8")).hexdigest() != manifest["copy_sha256"]:
        raise ValueError(f"{manifest['copy']}: копия документа не совпадает с манифестом")
    report = _read_json(report_path(run_dir))
    findings = load_findings(report, doc)
    passport = report.get("passport", {})
    copy = manifest["copy"]
    scores = [score_mutation(copy, m, findings) for m in manifest["mutations"]]
    touched = {fid for s in scores for fid in s.findings}
    keys = {f.key for f in findings}
    places = {p for f in findings for p in f.places}
    noise = new_place = -1
    jac = jac_places = None
    if original is not None:
        rest = [f for f in findings if f.id not in touched]
        noise = sum(1 for f in rest if f.key not in original.keys)
        new_place = sum(1 for f in rest if not f.places & original.places)
        jac = round(jaccard(keys, set(original.keys)), 3)
        jac_places = round(jaccard(places, set(original.places)), 3)
    return CopyScore(
        copy=copy,
        kind=manifest["kind"],
        run_dir=run_dir.as_posix(),
        findings=len(findings),
        findings_lint_only=sum(1 for f in findings if f.sources == {LINT}),
        noise=noise,
        noise_new_place=new_place,
        jaccard_with_original=jac,
        jaccard_places=jac_places,
        cost_usd=passport.get("cost_usd"),
        duration_s=passport.get("duration_s"),
        agent_version=passport.get("agent_version"),
        mutations=scores,
    )


@dataclass
class OperatorRow:
    operator: str
    name: str
    inserted: int
    caught: int
    place_only: int
    by_lint: int
    by_model: int

    @property
    def recall(self) -> float:
        return self.caught / self.inserted if self.inserted else 0.0

    @property
    def recall_soft(self) -> float:
        return (self.caught + self.place_only) / self.inserted if self.inserted else 0.0

    @property
    def recall_model(self) -> float:
        return self.by_model / self.inserted if self.inserted else 0.0


def by_operator(scores: Sequence[MutationScore]) -> list[OperatorRow]:
    rows: dict[str, OperatorRow] = {}
    for s in scores:
        row = rows.setdefault(s.operator, OperatorRow(s.operator, s.name, 0, 0, 0, 0, 0))
        row.inserted += 1
        row.caught += s.status == CAUGHT
        row.place_only += s.status == PLACE_ONLY
        row.by_lint += s.by_lint
        row.by_model += s.by_model
    return sorted(rows.values(), key=lambda r: int(r.operator[1:]))


def total(rows: Sequence[OperatorRow]) -> OperatorRow:
    out = OperatorRow("Σ", "Все операторы", 0, 0, 0, 0, 0)
    for r in rows:
        out.inserted += r.inserted
        out.caught += r.caught
        out.place_only += r.place_only
        out.by_lint += r.by_lint
        out.by_model += r.by_model
    return out


def _baseline(results_dir: Path, source: Path, prefix: str) -> Baseline | None:
    """Прогон исходного документа той же версией агента: ``<results>/<префикс>-original``."""
    run_dir = results_dir / f"{prefix}-original"
    if not (run_dir / "report.json").is_file() or not source.is_file():
        return None
    found = load_findings(_read_json(report_path(run_dir)), source.read_text(encoding="utf-8"))
    return Baseline(frozenset(f.key for f in found), frozenset(p for f in found for p in f.places))


def score_all(mutations_dir: Path, results_dir: Path) -> list[CopyScore]:
    out = []
    baselines: dict[str, Baseline | None] = {}
    for manifest_path in sorted(mutations_dir.glob("*.json")):
        manifest = _read_json(manifest_path)
        run_dir = results_dir / manifest["copy"]
        if not (run_dir / "report.json").is_file():  # прогона нет или он ещё идёт
            continue
        prefix = document_of(manifest["copy"])
        if prefix not in baselines:
            baselines[prefix] = _baseline(results_dir, Path(manifest["source"]), prefix)
        doc = (mutations_dir / f"{manifest['copy']}.md").read_text(encoding="utf-8")
        out.append(score_copy(manifest, doc, run_dir, baselines[prefix]))
    return out


def document_of(copy: str) -> str:
    """Префикс документа в имени копии: ``req-multi-1`` → ``req``."""
    return copy.split("-", 1)[0]


def interference(copies: Sequence[CopyScore], mutations_dir: Path) -> list[dict[str, str]]:
    """Сравнение копий с одной мутацией и той же мутации в копии с несколькими."""
    by_id = {m.id: m for c in copies for m in c.mutations}
    out = []
    for c in copies:
        if c.kind != "single" or not c.mutations:
            continue
        same_as = _read_json(mutations_dir / f"{c.copy}.json").get("same_as")
        multi = by_id.get(str(same_as))
        out.append(
            {
                "operator": c.mutations[0].operator,
                "single": c.copy,
                "single_status": c.mutations[0].status,
                "multi": str(same_as),
                "multi_status": multi.status if multi else "нет прогона",
            }
        )
    return out


def _rows(scores: Sequence[MutationScore]) -> dict[str, Any]:
    rows = by_operator(scores)
    tot = total(rows)
    return {
        "operators": [asdict(r) | {"recall": r.recall, "recall_soft": r.recall_soft} for r in rows],
        "total": asdict(tot) | {"recall": tot.recall, "recall_soft": tot.recall_soft},
    }


def to_json(copies: Sequence[CopyScore], mutations_dir: Path) -> dict[str, Any]:
    scored = [m for c in copies if c.kind == "multi" for m in c.mutations]
    documents = sorted({document_of(m.copy) for m in scored})
    return {
        "schema": "mutations-score/2",
        "copies": [asdict(c) for c in copies],
        **_rows(scored),
        "documents": {d: _rows([m for m in scored if document_of(m.copy) == d]) for d in documents},
        "interference": interference(copies, mutations_dir),
    }


def _pct(x: float) -> str:
    return f"{round(x * 100)} %"


def to_markdown(data: dict[str, Any]) -> str:
    """Таблицы для отчёта: recall по операторам, копии, помехи, пропуски."""
    lines: list[str] = []
    tables = {"все документы": data, **data.get("documents", {})}
    for name, part in tables.items():
        lines += ["", f"**Recall по операторам: {name}**", "", *_operator_table(part)]
    lines += [
        "",
        "| Копия | Вид | Мутаций | Поймано | Находок | Только линтер | Новых находок | Из них в новом месте "
        "| Жаккар находок | Жаккар мест | Цена, $ | Время, мин | Версия |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in data["copies"]:
        caught = sum(m["status"] == CAUGHT for m in c["mutations"])
        minutes = f"{c['duration_s'] / 60:.1f}" if c["duration_s"] else "—"
        lines.append(
            f"| {c['copy']} | {c['kind']} | {len(c['mutations'])} | {caught} | {c['findings']} | "
            f"{c['findings_lint_only']} | {_dash(c['noise'])} | {_dash(c['noise_new_place'])} | "
            f"{_dash(c['jaccard_with_original'])} | {_dash(c['jaccard_places'])} | "
            f"{c['cost_usd'] or '—'} | {minutes} | {c['agent_version']} |"
        )
    if data["interference"]:
        lines += [
            "",
            "| Оператор | Одна мутация | Статус | Та же мутация среди нескольких | Статус |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {i['operator']} | {i['single']} | {i['single_status']} | {i['multi']} | {i['multi_status']} |"
            for i in data["interference"]
        ]
    every = [m for c in data["copies"] for m in c["mutations"]]
    if every:
        lines += ["", "| Мутация | Оператор | Статус | Строка | Было | Стало | Находки на месте |"]
        lines += ["|---|---|---|---|---|---|---|"]
        for m in every:
            titles = "<br>".join(_cell(t, 120) for t in m["titles"]) or "—"
            lines.append(
                f"| {m['id']} | {m['operator']} | {m['status']} | {m['line']} | {_cell(m['original'])} | "
                f"{_cell(m['replacement'])} | {titles} |"
            )
    return "\n".join(lines) + "\n"


def _operator_table(part: dict[str, Any]) -> list[str]:
    lines = [
        "| Оператор | Внесено | Поймано строго | По месту, свойство другое | Линтер | Модель "
        "| Recall | Recall мягкий | Recall модели |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in [*part["operators"], part["total"]]:
        n = r["inserted"] or 1
        lines.append(
            f"| {r['operator']} {r['name']} | {r['inserted']} | {r['caught']} | {r['place_only']} | "
            f"{r['by_lint']} | {r['by_model']} | {_pct(r['caught'] / n)} | "
            f"{_pct((r['caught'] + r['place_only']) / n)} | {_pct(r['by_model'] / n)} |"
        )
    return lines


def _dash(value: float | None) -> str:
    return "—" if value is None or value < 0 else str(value)


def _cell(text: str, limit: int = 90) -> str:
    text = " ".join(text.split()).replace("|", "\\|")
    if not text:
        return "(пусто)"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Подсчёт пойманных мутаций")
    parser.add_argument("mutations", type=Path, help="папка с копиями и манифестами")
    parser.add_argument("results", type=Path, help="папка с прогонами: <results>/<копия>/report.json")
    parser.add_argument("--out", type=Path, required=True, help="путь без расширения для .json")
    args = parser.parse_args(argv)
    copies = score_all(args.mutations, args.results)
    data = to_json(copies, args.mutations)
    args.out.with_suffix(".json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    args.out.with_suffix(".tables.md").write_text(to_markdown(data), encoding="utf-8")
    print(args.out.with_suffix(".json").as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
