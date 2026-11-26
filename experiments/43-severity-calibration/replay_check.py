"""Проверка правила настоящим кодом: размеченные прогоны пересобираются через replay. Без модели.

Для каждого прогона с разметкой precision (#49) запускает ``validator … --backend replay`` на его записи
``run/`` текущим кодом и сравнивает обязательный список (критичные и важные) до и после. Находки
сопоставляются со старой разметкой по ``merged_id``: номера ``F-NN`` после правки важности другие.
«До» это ``report.json`` прогона в git. Вопросы автору в таком replay находятся по номеру вызова
(``LLM-016``), на находки и их важность это не влияет.

Запуск из корня репозитория: ``uv run python experiments/43-severity-calibration/replay_check.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from whatif import ROOT, consensus_real, findings_of, labeled_runs

DOCUMENT = "data/input/requirements.md"


def replayed(run: str, out: Path) -> list[dict]:
    target = out / run.replace("/", "_")
    command = [sys.executable, "-m", "validator", DOCUMENT, str(target / "report.md")]
    command += ["--backend", "replay", "--replay", str(ROOT / run / "run")]
    subprocess.run(command, cwd=ROOT, check=True, capture_output=True)  # noqa: S603 - свой CLI, без оболочки
    return json.loads((target / "report.json").read_text(encoding="utf-8"))["verification"]["findings"]


def main() -> None:
    real = consensus_real()
    runs = labeled_runs(real)
    totals = {"before": [0, 0, 0], "after": [0, 0, 0]}
    print("| Прогон | Список до | Precision до | Список после | Precision после | Настоящих вне списка: до → после |")
    print("|---|---:|---:|---:|---:|---:|")
    with tempfile.TemporaryDirectory() as tmp:
        for run in runs:
            before = {f["merged_id"]: f for f in findings_of(run)}
            after = replayed(run, Path(tmp))
            assert {f["merged_id"] for f in after} == set(before), f"{run}: набор находок изменился"
            all_real = sum(real[(run, f["id"])] for f in before.values())
            cells = []
            for key, findings in (("before", list(before.values())), ("after", after)):
                main_list = [f for f in findings if f["severity"] != "minor"]
                good = sum(real[(run, before[f["merged_id"]]["id"])] for f in main_list)
                totals[key][0] += len(main_list)
                totals[key][1] += good
                totals[key][2] += all_real - good
                cells.append((len(main_list), good / len(main_list), all_real - good))
            (n0, p0, out0), (n1, p1, out1) = cells
            print(f"| {run} | {n0} | {p0:.1%} | {n1} | {p1:.1%} | {out0} → {out1} |".replace(".", ","))
    n = len(runs)
    b, a = totals["before"], totals["after"]
    summary = (
        f"| **В среднем** | **{b[0] / n:.1f}** | **{b[1] / b[0]:.1%}** | **{a[0] / n:.1f}** |"
        f" **{a[1] / a[0]:.1%}** | **{b[2] / n:.1f} → {a[2] / n:.1f}** |"
    )
    print(summary.replace(".", ","))


if __name__ == "__main__":
    main()
