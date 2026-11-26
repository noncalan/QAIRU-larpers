"""Что было бы с обязательным списком находок при другой калибровке важности. Без вызовов модели.

Берёт готовую разметку precision (#49, две модели вслепую) из ``evals/results/*precision*.json`` и
``report.json`` тех же прогонов (как они записаны в git, до правки важности) и пересчитывает, какие
«важные» находки стали бы «незначительными» при двух общих правилах:

* R1: дефект формы (терминология, пустое поле, дубль, составное требование, деталь реализации,
  прослеживаемость) не выше «незначительно», даже если модель написала «мешает тесту»;
* R2: модель сама дала подсказку важности «низко»: не выше «незначительно».

Находку, которую подтвердил код, правила не трогают, как в ``validator.severity``: правило линтера,
которое само ставит «мешает тесту», или проверенный пример-свидетель.

Разметка пилота (``tests/fixtures/e2e/md``) в расчёт не идёт: она сделана для прежней записи с тремя
проходами, а ``report.json`` по этому пути с тех пор перезаписан. Эталон (``docs/gold``) скрипт не читает.
Запуск из корня репозитория: ``uv run python experiments/43-severity-calibration/whatif.py``.
Проверка настоящим кодом: ``replay_check.py`` рядом.
"""

from __future__ import annotations

import json
from pathlib import Path

from validator.lint.rules import RULE_BY_ID

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "evals" / "results"
FORM = {"terminology", "placeholder", "redundancy", "not_atomic", "design_detail", "traceability"}
RULES = {"сейчас": set(), "R1": {"R1"}, "R2": {"R2"}, "R1 + R2": {"R1", "R2"}}
STALE_RUNS = ("tests/fixtures/e2e/",)


def consensus_real() -> dict[tuple[str, str], bool]:
    """(прогон, F-NN) → оба разметчика сказали «настоящий»."""
    real: dict[tuple[str, str], bool] = {}
    for path in sorted(RESULTS.glob("*precision*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        labels = data["labels"]
        for item in data["items"]:
            if item["origin"] != "agent" or item["run"].startswith(STALE_RUNS):
                continue
            marks = [labels[name][item["uid"]]["label"] for name in labels if item["uid"] in labels[name]]
            if len(marks) == len(labels):
                real[(item["run"], item["ref"])] = all(mark == "real" for mark in marks)
    return real


def labeled_runs(real: dict[tuple[str, str], bool]) -> list[str]:
    return sorted({run for run, _ in real})


def confirmed_by_code(finding: dict) -> bool:
    if (finding.get("witness") or {}).get("checked"):
        return True
    rules = (RULE_BY_ID.get(src.get("rule_id") or "") for src in finding["sources"] if src["kind"] == "lint")
    return any(rule is not None and rule.blocks_testing for rule in rules)


def group(finding: dict) -> str | None:
    """К какой группе «важных» относится находка: «форма», «подсказка низко» или None."""
    if finding["severity"] != "major" or confirmed_by_code(finding):
        return None
    if finding["defect_type"] in FORM:
        return "R1"
    if finding.get("severity_hint") == "minor" and finding["defect_type"] != "injected_instruction":
        return "R2"
    return None


def severity(finding: dict, rules: set[str]) -> str:
    return "minor" if group(finding) in rules else finding["severity"]


def findings_of(run: str) -> list[dict]:
    return json.loads((ROOT / run / "report.json").read_text(encoding="utf-8"))["verification"]["findings"]


def row(runs: list[str], real: dict[tuple[str, str], bool], rules: set[str]) -> tuple[float, float, float, float]:
    """Находок в обязательном списке на прогон, его строгая precision, настоящих вне списка, перенесено."""
    main = main_real = total_real = moved = 0
    for run in runs:
        for f in findings_of(run):
            is_real = real[(run, f["id"])]
            total_real += is_real
            new = severity(f, rules)
            moved += new != f["severity"]
            if new != "minor":
                main += 1
                main_real += is_real
    n = len(runs)
    return main / n, main_real / main, (total_real - main_real) / n, moved / n


def main() -> None:
    real = consensus_real()
    runs = labeled_runs(real)
    print(f"### Группы «важных» находок ({len(runs)} прогонов)\n")
    print("| Группа | Находок | Настоящих (оба разметчика) |\n|---|---:|---:|")
    counts = {key: [0, 0] for key in ("R1", "R2", None)}
    for run in runs:
        for f in findings_of(run):
            if f["severity"] == "major":
                counts[group(f)][0] += 1
                counts[group(f)][1] += real[(run, f["id"])]
    names = {"R1": "дефект формы, кодом не подтверждён", "R2": "подсказка модели «низко»", None: "остальные"}
    for key, (total, good) in counts.items():
        print(f"| {names[key]} | {total} | {good} |")
    groups = {"все размеченные прогоны": runs, "только recall/v3": [r for r in runs if "/recall/v3/" in r]}
    for title, selected in groups.items():
        print(f"\n### {title} ({len(selected)})\n")
        print(
            "| Правило | Обязательный список, находок на прогон | Строгая precision списка |"
            " Настоящих вне списка, на прогон | Перенесено, на прогон |"
        )
        print("|---|---:|---:|---:|---:|")
        for name, rules in RULES.items():
            size, precision, outside, moved = row(selected, real, rules)
            print(f"| {name} | {size:.1f} | {precision:.1%} | {outside:.1f} | {moved:.1f} |".replace(".", ","))


if __name__ == "__main__":
    main()
