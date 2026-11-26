#!/usr/bin/env bash
# Живые прогоны агента на мутантах (#50): 2 исходника и 8 копий, по два прогона одновременно.
# Код агента берётся из отдельной папки с релиз-кандидатом, мутанты и результаты лежат в этой ветке:
#   git worktree add --detach ../qairu-rc1 v0.9-rc1 && (cd ../qairu-rc1 && uv sync)
#   bash evals/run_mutations.sh ../qairu-rc1 rc1-a3e57a1
# Второй аргумент это имя папки результатов: evals/results/mutations/<имя>/<копия>/.
# Уже готовые прогоны (есть report.json) пропускаются, поэтому скрипт можно перезапускать.
set -u
HERE="$(cd "$(dirname "$0")/.." && pwd)"
RC="$(cd "${1:?путь к папке с кодом агента, например ../qairu-rc1}" && pwd)"
OUT="$HERE/evals/results/mutations/${2:?имя папки результатов, например main-<коммит>}"
MUT="$HERE/evals/mutations"
mkdir -p "$OUT"

run() {
  local name="$1" src="$2"
  if [[ -f "$OUT/$name/report.json" ]]; then echo "уже есть: $name"; return; fi
  echo "старт $name $(date +%H:%M:%S)"
  (cd "$RC" && uv run validator "$src" "$OUT/$name/report.md" --max-budget-usd 5 >"$OUT/$name.log" 2>&1)
  echo "готово $name, код $? $(date +%H:%M:%S)"
}
pair() { run "$1" "$2" & run "$3" "$4" & wait; }

C4="$HERE/data/holdout/c4/c4_tech_task.md"
# Десять прогонов (предел задачи #50): сначала всё, без чего нет таблицы recall, одиночные копии в конце.
pair req-original "$HERE/data/input/requirements.md" req-multi-1 "$MUT/req-multi-1.md"
pair req-multi-2 "$MUT/req-multi-2.md" req-multi-3 "$MUT/req-multi-3.md"
pair req-multi-4 "$MUT/req-multi-4.md" c4-original "$C4"
pair c4-multi-1 "$MUT/c4-multi-1.md" c4-multi-2 "$MUT/c4-multi-2.md"
pair req-single-1 "$MUT/req-single-1.md" req-single-2 "$MUT/req-single-2.md"
echo "все прогоны закончены"
