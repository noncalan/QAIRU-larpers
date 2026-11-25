#!/usr/bin/env bash
# Прогоны контрольной точки D-011 (issue #37, docs/plan.md «Протокол eval и правило D-011»):
#   B  агент, 1 проход medium            × 3   (профиль quick)
#   B′ то же на тексте без ID блоков     × 2   (--search-input raw)
#   C  агент, 3 прохода medium           × 3   (профиль standard)
# A (базовая линия) уже есть: evals/results/2026-11-26-baseline.json.
#
# Не больше трёх прогонов одновременно. У каждого прогона своя папка и свой кеш ответов (.cache/ в ней,
# в git не идёт): прогоны независимы, а упавший можно запустить заново, удачные вызовы возьмутся из его кеша.
#
# Использование: evals/checkpoint.sh [ПАПКА] [БЭКЕНД] [ПРОГОНЫ...]
#   ПАПКА    по умолчанию evals/results/checkpoint
#   БЭКЕНД   cli (подписка, по умолчанию), fake (проверка команд без сети), replay, api
#   ПРОГОНЫ  например «b/run-2 c/run-1»; по умолчанию все восемь
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$ROOT/evals/results/checkpoint}
BACKEND=${2:-cli}
shift $(($# < 2 ? $# : 2))
DOC=$ROOT/data/input/requirements.md
MAX_PARALLEL=3

args_for() {
  case "$1" in
    b/*) echo "--profile quick --passes 1" ;;
    b-raw/*) echo "--profile quick --passes 1 --search-input raw" ;;
    c/*) echo "--profile standard --passes 3" ;;
    *) echo "Неизвестный прогон: $1" >&2; return 1 ;;
  esac
}

one_run() {
  local name=$1 dir=$OUT/$1 opts
  opts=$(args_for "$name") || return 1
  mkdir -p "$dir/.cache"
  local started=$SECONDS
  # shellcheck disable=SC2086  # opts это список флагов
  (cd "$dir" && uv run --project "$ROOT" validator "$DOC" report.md --backend "$BACKEND" $opts \
    >"$dir/.cache/cli-output.txt" 2>&1)
  local code=$?
  echo "$name: код $code, $((SECONDS - started)) с"
}

RUNS=("$@")
if [ ${#RUNS[@]} -eq 0 ]; then
  RUNS=(b/run-1 b/run-2 b/run-3 b-raw/run-1 b-raw/run-2 c/run-1 c/run-2 c/run-3)
fi

for name in "${RUNS[@]}"; do
  while [ "$(jobs -rp | wc -l)" -ge "$MAX_PARALLEL" ]; do
    wait -n
  done
  one_run "$name" &
done
wait
