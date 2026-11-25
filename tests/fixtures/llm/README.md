# Записанные ответы модели для бэкенда `fake`

`<шаг>/<номер>.json`: шаг это значение `LLMStep` (`search`, `segment`, `questions`, …), номер это `LLMRequest.call_index` с 1. Формат тот же, что печатает `claude -p --output-format json` (`result`, `structured_output`, `usage`, `modelUsage`, `total_cost_usd`), поэтому живой ответ можно положить сюда как есть.

`search/1–3.json`: три прохода поиска свободным текстом в формате промпта #14 (`src/validator/prompts/search_system.md`), без `structured_output`. Их разбирает код (`src/validator/search_parse.py`), перенос в JSON моделью для них не нужен, поэтому в smoke-прогоне ровно три вызова `search.1–3`. Находки придуманы для тестов (те же, что были у заглушки каркаса), а не взяты из эталона `docs/gold/`; цитаты из `data/input/requirements.md`, одна цитата выдумана нарочно (верификатор обязан её отбросить). Проходы нарочно оформлены по-разному, как это делает модель: проход 1 канонический, в проходе 2 имена полей жирным и у находки «Неясно…» нет строки «Вердикт», в проходе 3 ID предложений и черта после последней находки.

`search/live/`: записанные живые ответы (эксперимент 23, `experiments/23-search-pass/record.py`), их читают тесты `tests/test_search.py`:

- `pass-opus-medium.json`: один проход Opus 5.5 `medium` по `requirements.md` с промптом #14 как есть (49 находок, 220 с, $0,66 по тарифу API);
- `transfer-haiku-medium.json`: перенос всех 49 находок этого прохода в JSON моделью Haiku 5.5 `medium` по `prompts/search_transfer_system.md` и `TRANSFER_SCHEMA` (76 с, $0,017).

`FakeLLM` эту папку не читает (ему нужны файлы `<номер>.json`), поэтому smoke-прогон от неё не зависит. Если меняется нумерация блоков или промпт поиска, живые ответы записываются заново тем же скриптом.
