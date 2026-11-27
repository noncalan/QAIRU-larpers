#!/usr/bin/env bash
# Демо агента для записи терминала (issue #55): те же четыре шага, что в docs/demo.md.
#
# Модель не вызывается ни разу: проверка документа идёт через --backend replay по записи
# живого прогона (reports/v0.9-rc2/run и tests/fixtures/e2e/md/run), а битый файл
# отсекается до любого вызова модели. Для надёжности скрипт убирает `claude` из PATH
# и ключи API из окружения: даже ошибка в команде не потратит лимит или деньги.
#
# Запуск из корня репозитория:
#   bash docs/demo/demo.sh            # с паузами и «набором» команд, как на записи
#   DEMO_FAST=1 bash docs/demo/demo.sh  # без пауз, для проверки
# Запись: docs/demo/README.md.
set -u

cd "$(dirname "$0")/../.." || exit 2

# Никаких живых вызовов модели: папки с `claude` убираем из PATH, а uv оставляем
# через отдельную папку со ссылкой (uv и claude часто лежат вместе в ~/.local/bin).
uv_bin=$(command -v uv) || { echo "Нужен uv: https://docs.astral.sh/uv/" >&2; exit 2; }
shim=$(mktemp -d)
trap 'rm -rf "$shim"' EXIT
ln -s "$uv_bin" "$shim/uv"
for d in $(type -ap claude 2>/dev/null | xargs -r -n1 dirname | sort -u); do
  PATH=$(printf '%s' "$PATH" | tr ':' '\n' | grep -vxF "$d" | paste -sd:)
done
export PATH="$shim:$PATH"
if command -v claude >/dev/null; then
  echo "claude всё ещё в PATH, демо не запускаю" >&2
  exit 2
fi
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL
export VALIDATOR_OCR=off
unset UV_NATIVE_TLS
uv sync -q >/dev/null 2>&1 || true   # чтобы на записи не было строк сборки

OUT=out/demo
rm -rf "$OUT"

fast=${DEMO_FAST:-0}
pause() { [ "$fast" = 1 ] || sleep "$1"; }

# Печатает строку-пояснение серым.
say() {
  printf '\033[90m# %s\033[0m\n' "$1"
  pause 1.2
}

# «Набирает» команду и выполняет её.
run() {
  local cmd=$1
  printf '\033[1;32m$\033[0m '
  if [ "$fast" = 1 ]; then
    printf '%s' "$cmd"
  else
    local i
    for ((i = 0; i < ${#cmd}; i++)); do
      printf '%s' "${cmd:i:1}"
      sleep 0.018
    done
    sleep 0.4
  fi
  printf '\n'
  bash -c "$cmd"
  local code=$?
  printf '\033[90m(код возврата %s)\033[0m\n' "$code"
  pause "${2:-2.5}"
}

clear
say "QAIRU validator: проверка требований по ISO/IEC/IEEE 29148 и IEEE 830"
say "Демо без вызова модели: ответы берутся из записи живого прогона (replay)"
pause 0.8

say "1. Одна команда: проверить PDF с требованиями и получить отчёт"
run "uv run validator data/input/requirements.pdf $OUT/pdf/report.pdf --backend replay --replay reports/v0.9-rc2/run" 4

say "2. Первая страница отчёта: светофор и что исправить первым"
run "sed -n '/^## Итог на первой/,/^3\\. /p' $OUT/pdf/report.md | tr -d '*'" 7

say "3. Битый файл: PDF под паролем. Агент не падает, а объясняет"
run "uv run validator tests/fixtures/broken/password.pdf $OUT/broken/report.md" 3
run "grep '^| IN-' $OUT/broken/report.md" 5

say "4. Вопросы автору. Берём тот же документ в Markdown: для него есть пример ответов"
run "uv run validator data/input/requirements.md $OUT/md/report.md --backend replay --replay tests/fixtures/e2e/md/run" 3
run "sed -n '/^### Какие вопросы/,/^3\\. /p' $OUT/md/report.md | tr -d '*'" 6
say "Автор вписал ответы в questions.yaml (пример: examples/answers.yaml)"
run "grep -E '^- id:|^  answer:' examples/answers.yaml | paste - - | head -4" 3
say "Повторная проверка с ответами автора"
run "uv run validator data/input/requirements.md $OUT/answers/report.md --backend replay --replay tests/fixtures/e2e/md/run --answers examples/answers.yaml" 3
run "sed -n '/^| | До ответов/,/^| Открытых/p' $OUT/answers/report.md" 7

say "Паспорт отчёта честно говорит, откуда ответы модели"
run "grep -E '^\\| (Способ вызова|Запись ответов)' $OUT/pdf/report.md" 5
say "Все отчёты лежат в $OUT/"
pause 2
