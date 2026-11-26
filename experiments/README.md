# Эксперименты

Одноразовые скрипты исследования и замеров: бенчмарки парсеров, пробные вызовы LLM, записи ответов модели для тестов. Это **не код агента**: агент лежит в `src/validator/`, оценка по эталону в `evals/`. Скрипты оставлены как воспроизводимое обоснование решений (`docs/decisions.md`, `docs/research/`) и местами устарели, например про Gemini, см. плашки в `docs/research/`.

- Одна папка на эксперимент: `experiments/<тема>/` (например `experiments/parsers/`, `experiments/baseline-one-prompt/`).
- Окружение: `uv sync --group experiments`, запуск: `uv run python experiments/<тема>/<скрипт>.py`.
- Большие выходные файлы клади в `experiments/out/` (в `.gitignore`). Выводы и таблицы пиши в `docs/research/`.
- Ключ берётся из `.env` через `python-dotenv`. Ключ не печатаем, не логируем и не коммитим.
- Тестовые входы: `data/input/requirements.{md,docx,pdf}`. Битые файлы для тестов клади в `data/broken/` с описанием, как получен каждый.

## Что в какой папке

| Папка | Что это | Где выводы |
|---|---|---|
| `01-lint-probe` | проба правил линтера на слабых словах | `docs/research/01-standards-criteria.md` |
| `03-proto` | проверка цитат, Z3 для противоречий | `docs/research/03-contradictions-and-grounding.md` |
| `04-parsing-lab` | битые файлы, защита входа, парсеры | `docs/research/04-parsing-and-broken-files.md` |
| `08-stack-lab` | генерация PDF (Typst, fpdf) | `docs/research/08-agent-stack-and-eval.md` |
| `11-baseline-one-prompt` | базовая линия «один промпт» через `claude -p` | `docs/research/11-baseline-one-prompt.md` |
| `12-parsers-on-my-os` | те же проверки парсеров на Windows | `docs/research/12-parsers-on-my-os.md` |
| `18-segmentation` | нарезка требований, метрики | `docs/research/18-requirements-segmentation.md` |
| `20-questions-editor` | запись ответов редактора вопросов для тестов | `docs/adr/`, `docs/architecture.md` |
| `21-model-effort` | какая модель и effort на каком шаге | `docs/research/21-model-and-effort.md`, D-010 |
| `22-search-prompt`, `23-search-pass` | промпт и живой проход поиска дефектов | README внутри папок, `docs/decisions.md` |
| `39-evidence`, `40-test-draft`, `41-questions-critic`, `42-clarify` | записи прогонов шагов: свидетель, тест-кейс, критик вопросов, уточнение | README внутри папок, `docs/architecture.md` |
