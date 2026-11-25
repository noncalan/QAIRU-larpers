# Эксперименты

Одноразовые скрипты фазы исследования: бенчмарки парсеров, пробные вызовы LLM, замеры. Это **не код агента**: его начнём после выбора идеи.

- Одна папка на эксперимент: `experiments/<тема>/` (например `experiments/parsers/`, `experiments/baseline-one-prompt/`).
- Окружение: `uv sync --group experiments`, запуск: `uv run python experiments/<тема>/<скрипт>.py`.
- Большие выходные файлы клади в `experiments/out/` (в `.gitignore`). Выводы и таблицы пиши в `docs/research/`.
- Ключ берётся из `.env` через `python-dotenv`. Ключ не печатаем, не логируем и не коммитим.
- Тестовые входы: `data/input/requirements.{md,docx,pdf}`. Битые файлы для тестов клади в `data/broken/` с описанием, как получен каждый.
