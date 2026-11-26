"""Что было бы с обязательным списком находок при другой калибровке важности. Без вызовов модели.

Берёт готовую разметку precision (#49, две модели вслепую) из ``evals/results/*precision*.json`` и
``report.json`` тех же прогонов и пересчитывает, какие «важные» находки стали бы «незначительными»
при двух общих правилах:

* R1: дефект формы (терминология, пустое поле, дубль, составное требование, деталь реализации,
  прослеживаемость) не выше «незначительно», даже если модель написала «мешает тесту»;
* R2: модель сама дала подсказку важности «низко»: не выше «незначительно».

Эталон (``docs/gold``) скрипт не читает. Запуск из корня репозитория:
``uv run python experiments/43-severity-calibration/whatif.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "evals" / "results"
FORM = {"terminology", "placeholder", "redundancy", "not_atomic", "design_detail", "traceability"}
RULES = {"сейчас": set(), "R1": {"R1"}, "R2": {"R2"}, "R1 + R2": {"R1", "R2"}}


def consensus_real() -> dict[tuple[str, str], bool]:
    """(прогон, F-NN) → оба разметчика сказали «настоящий». Находка из нескольких файлов: последний."""
    real: dict[tuple[str, str], bool] = {}
    for path in sorted(RESULTS.glob("*precision*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        labels = data["labels"]
        for item in data["items"]:
            if item["origin"] != "agent":
                continue
            marks = [labels[name][item["uid"]]["label"] for name in labels if item["uid"] in labels[name]]
            if len(marks) == len(labels):
                real[(item["run"], item["ref"])] = all(mark == "real" for mark in marks)
    return real


def severity(finding: dict, rules: set[str]) -> str:
    current = finding["severity"]
    if current != "major":
        return current
    if "R1" in rules and finding["defect_type"] in FORM:
        return "minor"
    if "R2" in rules and finding.get("severity_hint") == "minor":
        return "minor"
    return current


def row(runs: list[str], real: dict[tuple[str, str], bool], rules: set[str]) -> tuple[float, float, float, float]:
    """Находок в обязательном списке на прогон, его строгая precision, настоящих вне списка, перенесено."""
    main = main_real = total_real = moved = 0
    for run in runs:
        findings = json.loads((ROOT / run / "report.json").read_text(encoding="utf-8"))["verification"]["findings"]
        for f in findings:
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
    runs = sorted({run for run, _ in real})
    groups = {"все размеченные прогоны": runs, "только recall/v3": [r for r in runs if "/recall/v3/" in r]}
    for title, group in groups.items():
        print(f"\n### {title} ({len(group)})\n")
        print("| Правило | Обязательный список, находок на прогон | Строгая precision списка |"
              " Настоящих вне списка, на прогон | Перенесено, на прогон |")
        print("|---|---:|---:|---:|---:|")
        for name, rules in RULES.items():
            size, precision, outside, moved = row(group, real, rules)
            print(f"| {name} | {size:.1f} | {precision:.1%} | {outside:.1f} | {moved:.1f} |".replace(".", ","))


if __name__ == "__main__":
    main()
