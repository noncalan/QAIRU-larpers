# Записанные ответы модели для бэкенда `fake`

`<шаг>/<номер>.json`: шаг это значение `LLMStep` (`search`, `segment`, `questions`, …), номер это `LLMRequest.call_index` с 1. Формат тот же, что печатает `claude -p --output-format json` (`result`, `structured_output`, `usage`, `modelUsage`, `total_cost_usd`), поэтому живой ответ можно положить сюда как есть.

`search/1–3.json` сейчас ответы заглушки поиска каркаса (#15): схема `STUB_SCHEMA` из `src/validator/search.py`, цитаты из `data/input/requirements.md`, одна цитата выдумана нарочно (верификатор обязан её отбросить). Находки придуманы для тестов, а не взяты из эталона `docs/gold/`. Задача поиска (#18) заменит их записанными ответами настоящего прохода и переноса в JSON.
