# Эксперимент 21: модель и effort для проходов поиска

Выводы и таблицы: [`docs/research/21-model-and-effort.md`](../../docs/research/21-model-and-effort.md), решение D-010.

Один прогон = один «проход поиска» будущего агента: промпт базовой линии (исследование 11) и весь документ `data/input/requirements.md`. Меняются только модель, effort и формат ответа (строгий JSON через `--json-schema`, свободный текст, текст с переносом в JSON второй моделью). Отдельный судья (Opus 5.5, `high`) вслепую сопоставляет находки с эталоном. Эталон попадает только в запрос судьи (правило 2).

```bash
# из корня репозитория; нужен вход в Claude Code по подписке
python3 experiments/21-model-effort/run_matrix.py                  # матрица в режиме JSON
python3 experiments/21-model-effort/run_matrix.py free-opus-medium-1   # отдельный прогон по имени
python3 experiments/21-model-effort/run_matrix.py extract          # перенос текста в JSON (Haiku, Sonnet)
python3 experiments/21-model-effort/run_matrix.py rejudge opus-xhigh-1  # повторная оценка судьёй
uv run --no-project --with rapidfuzz python experiments/21-model-effort/score.py  # сводка
```

Готовые ответы лежат в `experiments/out/21/` и повторно не запрашиваются. Скрипт убирает из окружения дочернего `claude` переменные `ANTHROPIC_API_KEY`, `CLAUDE_EFFORT` и дополнительные папки с `CLAUDE.md`. Тексты находок и эталона `score.py` не печатает, только счётчики.
