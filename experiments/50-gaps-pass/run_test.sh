#!/usr/bin/env bash
# Итоговый замер прохода на пропуски: парные прогоны на записях мутантов (#50, ветка eval/mutations).
#   bash experiments/50-gaps-pass/run_test.sh <папка ветки eval/mutations> <имя выборки: base | gaps-1 | gaps-2>
# base: без прохода, все ответы из записи, модель не вызывается. gaps-N: живой только вызов gaps.1.
# Не больше двух прогонов одновременно; готовые (есть report.json) пропускаются.
set -u
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
MUT="$(cd "${1:?папка ветки eval/mutations}" && pwd)"
SAMPLE="${2:?base | gaps-1 | gaps-2}"
OUT="$HERE/experiments/50-gaps-pass/runs/test/$SAMPLE"
REC="$MUT/evals/results/mutations/main-cb0cc5c"
FLAG=""; [[ "$SAMPLE" == base ]] && FLAG="--no-gaps"
mkdir -p "$OUT"
run() {
  local name="$1" src="$2"
  if [[ -f "$OUT/$name/report.json" ]]; then echo "уже есть: $name"; return; fi
  echo "старт $SAMPLE/$name $(date +%H:%M:%S)"
  (cd "$HERE" && uv run python experiments/50-gaps-pass/run_gaps.py paired "$src" "$OUT/$name" \
      --seed-run "$REC/$name/run" $FLAG >"$OUT/$name.log" 2>&1)
  echo "готово $SAMPLE/$name, код $? $(date +%H:%M:%S): $(tail -2 "$OUT/$name.log" | tr '\n' ' ')"
}
pair() { run "$1" "$2" & run "$3" "$4" & wait; }
pair req-multi-1 "$MUT/evals/mutations/req-multi-1.md" c4-multi-1 "$MUT/evals/mutations/c4-multi-1.md"
pair req-multi-2 "$MUT/evals/mutations/req-multi-2.md" c4-multi-2 "$MUT/evals/mutations/c4-multi-2.md"
pair req-multi-3 "$MUT/evals/mutations/req-multi-3.md" c4-original "$HERE/data/holdout/c4/c4_tech_task.md"
pair req-multi-4 "$MUT/evals/mutations/req-multi-4.md" req-original "$HERE/data/input/requirements.md"
pair req-single-1 "$MUT/evals/mutations/req-single-1.md" req-single-2 "$MUT/evals/mutations/req-single-2.md"
echo "выборка $SAMPLE закончена"
