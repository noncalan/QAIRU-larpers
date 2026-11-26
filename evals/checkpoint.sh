#!/usr/bin/env bash
# Прогоны контрольной точки D-011 (issue #37, docs/plan.md «Протокол eval и правило D-011»):
#   B  агент, 1 проход medium            × 3   (профиль quick)
#   B′ то же на тексте без ID блоков     × 2   (--search-input raw)
#   C  агент, 3 прохода medium           × 3   (профиль standard)
# Доменные чек-листы (#35) включены по умолчанию; B, B′ и C идут с --no-checklists, как на контрольной точке:
#   cl/…    B с чек-листами (--checklists) на requirements.md
#   task/…  B с чек-листами на ТЗ кейса docs/case/task.md (чужой документ, эталона нет)
# A (базовая линия) уже есть: evals/results/2026-11-26-baseline.json.
#
# Не больше трёх прогонов одновременно. У каждого прогона своя папка и свой кеш ответов (.cache/ в ней,
# в git не идёт): прогоны независимы, а упавший можно запустить заново, удачные вызовы возьмутся из его кеша.
# Код выхода 1, если хоть один прогон не завершился с кодом 0 или в нём есть упавшие вызовы модели:
# такой прогон нельзя отдавать в eval, сначала перезапуск.
#
# Использование: evals/checkpoint.sh [ПАПКА] [БЭКЕНД] [ПРОГОНЫ...]
#   ПАПКА    по умолчанию evals/results/checkpoint
#   БЭКЕНД   cli (подписка, по умолчанию) или fake (проверка команд без сети, на фикстурах тестов)
#   ПРОГОНЫ  например «b/run-2 c/run-1» или «cl/run-1 task/run-1»; по умолчанию восемь прогонов D-011
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${1:-$ROOT/evals/results/checkpoint}
BACKEND=${2:-cli}
shift $(($# < 2 ? $# : 2))
mkdir -p "$OUT" && OUT=$(cd "$OUT" && pwd)  # абсолютный путь: прогон идёт из своей папки
DOC=$ROOT/data/input/requirements.md
TASK_DOC=$ROOT/docs/case/task.md
MAX_PARALLEL=3

case "$BACKEND" in
  cli | fake) ;;
  *) echo "Бэкенд «$BACKEND» не подходит: cli или fake" >&2; exit 2 ;;
esac

args_for() {
  case "$1" in
    b/*) echo "--profile quick --passes 1 --no-checklists" ;;
    b-raw/*) echo "--profile quick --passes 1 --search-input raw --no-checklists" ;;
    c/*) echo "--profile standard --passes 3 --no-checklists" ;;
    cl/* | task/*) echo "--profile quick --passes 1 --checklists" ;;
    *) echo "Неизвестный прогон: $1 (нужно b/…, b-raw/…, c/…, cl/… или task/…)" >&2; return 2 ;;
  esac
}

doc_for() {
  case "$1" in
    task/*) echo "$TASK_DOC" ;;
    *) echo "$DOC" ;;
  esac
}

one_run() {
  local name=$1 dir=$OUT/$1 opts doc code started=$SECONDS
  opts=$(args_for "$name") || return 2
  doc=$(doc_for "$name")
  mkdir -p "$dir/.cache"
  # shellcheck disable=SC2086  # opts это список флагов
  (cd "$dir" && uv run --project "$ROOT" validator "$doc" report.md --backend "$BACKEND" $opts \
    >"$dir/.cache/cli-output.txt" 2>&1)
  code=$?
  echo "$name: код $code, $((SECONDS - started)) с"
  return "$code"
}

RUNS=("$@")
if [ ${#RUNS[@]} -eq 0 ]; then
  RUNS=(b/run-1 b/run-2 b/run-3 b-raw/run-1 b-raw/run-2 c/run-1 c/run-2 c/run-3)
fi

# Очередь по PID без «wait -n»: тот есть только в bash 4.3+, а на macOS по умолчанию bash 3.2.
pids=()
for name in "${RUNS[@]}"; do
  if [ ${#pids[@]} -ge "$MAX_PARALLEL" ]; then
    wait "${pids[0]}"
    pids=("${pids[@]:1}")
  fi
  one_run "$name" &
  pids+=("$!")
done
for pid in "${pids[@]}"; do
  wait "$pid"
done

# Проверка после прогонов: код возврата и упавшие вызовы по report.json (проход мог упасть при коде 0).
failed=()
for name in "${RUNS[@]}"; do
  report=$OUT/$name/report.json
  status=$(uv run --project "$ROOT" python - "$report" <<'PY'
import json, sys
try:
    p = json.load(open(sys.argv[1], encoding="utf-8"))["passport"]
except (OSError, ValueError, KeyError):
    print("нет report.json"); sys.exit(1)
bad = p["exit_code"] != 0 or p["calls_failed"] > 0
print(f"код {p['exit_code']}, вызовов {p['calls_total']}, упало {p['calls_failed']}, "
      f"{p['duration_s']} с, ${p['cost_usd'] or 0:.2f}")
sys.exit(1 if bad else 0)
PY
  ) || failed+=("$name")
  echo "$name: $status"
done

if [ ${#failed[@]} -gt 0 ]; then
  echo "Перезапустить до eval: ${failed[*]}" >&2
  echo "Команда: evals/checkpoint.sh $OUT $BACKEND ${failed[*]}" >&2
  exit 1
fi
echo "Все прогоны без сбоев, можно запускать eval (evals/README.md, «Контрольная точка»)."
