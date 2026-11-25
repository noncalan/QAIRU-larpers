# 05. LLM API для рантайма агента: что бесплатно и что дёшево (состояние на 2026-11-25)

Исследование сделано 2026-11-25. Метки достоверности:
- **[офиц.]**: официальная страница вендора, прочитана сегодня;
- **[БД litellm]**: `model_prices_and_context_window.json` из репозитория BerriAI/litellm (ветка main, скачан 2026-11-25). Это поддерживаемая сообществом база цен, у каждой записи есть поле `source` со ссылкой на вендора. Надёжная, но не первоисточник;
- **[3-я сторона, дата]**: статья или форум с датой замера;
- **[не проверено]**: по памяти или только по заголовку поисковой выдачи. Проверить руками.

Ограничение работы: общий на всех агентов лимит WebFetch (100 запросов в час) кончился на середине. Поэтому страницы Mistral, GitHub Models, Cloudflare, NVIDIA, DeepSeek, Qwen, Together и Fireworks не прочитаны, а сведения о них помечены как непроверенные. Gemini, Groq, OpenRouter, Cerebras, Anthropic и библиотеки проверены.

---

## TL;DR

1. **`gemini-1.5-flash` больше нет.** Её нет ни в списке моделей, ни среди выведенных. `gemini-2.0-flash` отключена 2026-06-01. `gemini-2.5-*` закрыты для новых пользователей: «access limited to past users», а с 2026-12-01 запросы к `gemini-2.5-flash` будут перенаправляться на `gemini-3.5-flash-lite` [офиц.]. В `.env.example` ставим **`gemini-3.5-flash-lite`**.
2. **Бесплатный тариф Gemini сильно урезан, Pro в нём нет.** Flash-модели (3.8/3.6/3 Flash) дают **5 RPM и 20 RPD**. Flash-Lite (3.5/3.1) дают **15 RPM, 250k TPM и 500 RPD**. У Gemini 4 Argon, 3.1 Pro и 2.5 Pro бесплатная квота нулевая [3-я сторона, 21.10 и 15.11.2026]. Google больше не публикует цифры по моделям, точные значения видны только в AI Studio → Rate limit. Вывод из case-analysis «Pro для проверок, Flash для нарезки» устарел: **рабочая лошадка теперь Flash-Lite**.
3. **Следствие для конвейера.** Прогон в 15–40 вызовов на Flash (20 RPD) не помещается даже один раз в сутки. На Flash-Lite помещается 12–33 прогона в сутки на проект, но агент должен сам держать темп не выше 15 RPM, то есть прогон займёт 2–4 минуты. Жюри с новым ключом пройдёт, если основная модель Flash-Lite.
4. **Казахстан в списке регионов Gemini API есть** (страница обновлена 2026-10-22). Anthropic API в Казахстане тоже доступен [офиц.]. На бесплатном тарифе Google использует запросы и ответы для улучшения продуктов, и их могут читать люди. Для синтетического документа кейса это допустимо, но в README стоит написать, что настоящие банковские ТЗ нужно гонять через платный ключ.
5. **У Gemini есть всё, что нужно агенту.** Structured output по подмножеству JSON Schema (`response_mime_type` + `response_json_schema`, Pydantic) в `google-genai` 3.6.1. PDF можно отдавать напрямую: до 50 МБ или 1000 страниц, 258 токенов на страницу, встроенный текст извлекается бесплатно. Контекст 1M, thinking с уровнями low/medium/high (у 3.8 Flash уровень `minimal` даёт ошибку) [офиц.].
6. **Запасные варианты слабее.** Groq Free (`openai/gpt-oss-120b`, `qwen/qwen3.8-27b`): 30 RPM, 1K RPD, но **8K TPM и 200K TPD**, так что документ целиком туда едва влезает [офиц.]. У OpenRouter `:free`: 20 RPM, 50 RPD без кредитов и 1000 RPD после покупки на $10 и больше [офиц.]. Лучший запасной вариант **внутри того же ключа Gemini**: `gemini-3.1-flash-lite` и `gemma-4-31b-it`, у каждой модели своя квота.
7. **Платный прогон стоит центы.** Базовый сценарий (25 вызовов, около 200k токенов на вход и 80k на выход): Claude Haiku 5.5 около $0.06, Gemini 3.5 Flash-Lite около $0.26, Gemini 3.8 Flash около $0.45, Claude Sonnet 5.5 около $1.20, Claude Opus 5.5 около $2.40. Это готовая цифра для раздела «бизнес-ценность».
8. **Библиотеки.** Основной путь: `google-genai` 3.6.1, в нём есть все возможности Gemini. Для смены провайдера подойдёт `litellm` 1.111.0 (fallbacks и `num_retries`) или `pydantic-ai` 2.74.0 (FallbackModel). `instructor` 1.17.0 повторяет запрос при ошибке валидации Pydantic. Все версии сверены с PyPI 2026-11-25.

---

## 1. Google Gemini API: подробно

### 1.1 Какие модели живы (aistudio.google.com/docs/models, прочитано 2026-11-25)

Документация переехала. `ai.google.dev/gemini-api/docs/*` теперь отдаёт 302 на `aistudio.google.com/docs/*` (замечено 2026-11-25), поэтому старые ссылки в README лучше заменить.

| Группа | Модели (ID) | Статус |
|---|---|---|
| Текущие (Stable/New) | `gemini-4-argon`, `gemini-3.8-flash`, `gemini-3.6-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite` (+ TTS/Live/Image/Transcribe) | Актуальные |
| Preview | `gemini-3-flash-preview`, `gemini-3.1-pro-preview` | Preview, у них жёстче лимиты |
| 2.5 | `gemini-2.5-flash`, `gemini-2.5-flash-lite`, `gemini-2.5-pro` | «Not deprecated, access limited to past users». У `gemini-2.5-flash` и `gemini-2.5-pro` дата отключения не раньше 2026-12-01 [БД litellm + офиц. deprecations] |
| Отключены | `gemini-2.0-flash`, `gemini-2.0-flash-lite` (2026-06-01, замена `gemini-3.6-flash`), `gemini-3-pro-preview`, `gemini-3.1-flash-lite-preview` | Не работают |
| `gemini-1.5-flash` | Нет ни в одном списке | Мертва, из `.env.example` кейса её надо убрать |
| Gemma (через Gemini API) | `gemma-4-31b-it`, `gemma-4-26b-a4b-it` | Бесплатно, платного тарифа нет [3-я сторона, 22.10.2026]. Контекст 262k [БД litellm] |

Алиасы. В БД litellm есть `gemini-flash-latest` (цены 3.8 Flash) и `gemini-flash-lite-latest` (цены 3.5 Flash-Lite). На официальной странице `gemini-flash-latest` упомянут только как пример схемы именования, отдельного подтверждения нет. **Для воспроизводимости лучше закрепить конкретное имя**, а не `-latest`: паспорт прогона должен показывать точную модель.

Карточки моделей [офиц., aistudio.google.com/docs/models/...]:
- `gemini-3.5-flash-lite`: вход 1 048 576 токенов, выход 65 536. Structured outputs, thinking, function calling и PDF поддерживаются. Обновлена в июле 2026.
- `gemini-3.8-flash`: вход 1 048 576, выход 65 536. Structured outputs и PDF поддерживаются. Thinking: low, medium или high, **`minimal` не поддерживается и возвращает ошибку**. Обновлена в сентябре 2026.

### 1.2 Лимиты бесплатного тарифа

Официально (aistudio.google.com/docs/rate-limits, 2026-11-25):
- лимиты считаются **на проект, а не на ключ**, поэтому несколько ключей в одном проекте не помогают;
- RPD обнуляется в полночь по тихоокеанскому времени, то есть в 13:00 или 14:00 по Астане;
- «Specified rate limits are not guaranteed and actual capacity may vary»;
- цифры по моделям на странице не опубликованы, их показывает только https://aistudio.google.com/rate-limit внутри аккаунта.

Замеры третьих сторон (свежие, но не официальные):

| Модель | RPM | TPM (вход) | RPD | Источник |
|---|---|---|---|---|
| gemini-3.8-flash / 3.6-flash / 3-flash-preview / 2.5-flash | 5 | 250 000 | **20** | imisofts.com, замер в AI Studio 2026-10-21; aifreeapi.com, обновлено 2026-11-15; ветка на discuss.ai.google.dev «Gemini 3.8 Flash Free Tier 20 RPD…» (2026-09-03, ответа Google нет) |
| gemini-3.5-flash-lite / 3.1-flash-lite | **15** | **250 000** | **500** | Те же источники. В БД litellm для этих моделей тоже rpm=15, tpm=250000 |
| gemini-2.5-flash-lite | 10 | ? | 20 | imisofts.com, 2026-10-21 |
| Gemma 4 | 30 | ? | 14 400 | imisofts.com, 2026-10-21 [не проверено, TPM неизвестен] |
| gemini-4-argon, 3.1-pro-preview, 2.5-pro | 0 | 0 | 0 | imisofts.com; на странице цен Argon и 3.1 Pro отмечены «Free tier: Not available» [офиц.] |

**Другу с ключом:** сверить эту таблицу со страницей Rate limit в AI Studio, отдельно посмотреть TPM у Gemma 4. У Gemma 3 на Gemini API TPM был очень мал, около 15k [не проверено, по памяти]. Если у Gemma 4 так же, она годится только для коротких вызовов.

Tier 1 (после привязки биллинга, по данным aifreeapi, 2026-11-15): 3.8 Flash даёт 1000 RPM, 2M TPM, 10 000 RPD. Flash-Lite даёт 4000 RPM и 150 000 RPD. Кроме того, действует ограничение по расходам: $10 за 10 минут и $250 в месяц [офиц.].

### 1.3 Цены платного тарифа (aistudio.google.com/docs/pricing, 2026-11-25; совпадает с БД litellm)

| Модель | Free tier | Вход $/1M | Выход $/1M | Примечание |
|---|---|---|---|---|
| gemini-3.5-flash-lite | да | 0.30 | 2.50 | |
| gemini-3.1-flash-lite | да | 0.25 | 1.50 | В БД litellm указана дата вывода 2027-05-07 |
| gemini-3.8-flash / 3.6-flash | да | 0.75 | 3.75 | **С 2027-01-01 цена удваивается до 1.50 / 7.50** |
| gemini-3-flash-preview | да | 0.50 | 3.00 | preview |
| gemini-4-argon | нет | 2.00 | 10.00 | **С 2026-12-01 цена удваивается до 4.00 / 20.00** |
| gemini-3.1-pro-preview | нет | 2.00 | 12.00 | промпт ≤200k |
| gemini-2.5-flash-lite | да | 0.10 | 0.40 | Только для прежних пользователей |
| gemma-4-* | бесплатно | — | — | Платного тарифа нет |

Данные: в колонке «Used to improve our products» для бесплатного тарифа стоит **Yes**, для платного **No** [офиц.]. По условиям Unpaid Services запросы и ответы могут читать люди [3-я сторона, 22.10.2026, цитата из ToS]. В ЕЭЗ, Швейцарии и Великобритании продукт для пользователей можно строить только на платных сервисах. Казахстана это не касается.

### 1.4 Доступность из Казахстана

Казахстан есть в списке «Available regions» (ai.google.dev/gemini-api/docs/available-regions, обновлено 2026-10-22) [офиц.]. России и Беларуси в списке нет: если кто-то из жюри или команды выходит в сеть через российский IP, будет ошибка `User location is not supported`. Ключ создаётся в AI Studio с обычным Google-аккаунтом, карта для бесплатного тарифа не нужна [3-я сторона].

### 1.5 Structured output

- В SDK `google-genai` 3.6.1 (README на PyPI, 2026-11-18) используется `config=types.GenerateContentConfig(response_mime_type='application/json', response_json_schema=MyModel.model_json_schema())`, после чего ответ валидируется через `MyModel.model_validate_json(response.text)`.
- На странице документации structured-output (обновлена 2026-05-18) показан и новый синтаксис `response_format={"text": {"mime_type": ..., "schema": ...}}` [офиц., но пересказ WebFetch]. **Какой вариант работает в нашей закреплённой версии SDK, надо проверить руками.** README текущего SDK использует `response_json_schema`.
- Поддерживается подмножество JSON Schema: типы string, number, integer, boolean, object, array и null (через `["string","null"]`); `enum`, `required`, `additionalProperties`, `minItems` и `maxItems`, `minimum` и `maximum`, `format` для дат. Неподдерживаемые поля **молча игнорируются**. Слишком глубокие или большие схемы отклоняются. Синтаксическая валидность JSON гарантирована, смысловая нет [офиц.].
- Совет: схема плоская, вложенность не больше 2–3 уровней. Критерии ISO 29148 и серьёзность задаются через `enum`. После ответа всегда валидировать Pydantic-моделью.
- По БД litellm, у `gemma-4-31b-it` выставлено `supports_response_schema: true`. У Gemma 3 на API JSON-режима не было [не проверено]. **Проверить руками.**

### 1.6 Thinking (ai.google.dev/gemini-api/docs/generate-content/thinking, обновлено 2026-10-14) [офиц.]

- Gemini 3.x управляется параметром `thinking_level`:
  - **3.5 и 3.1 Flash-Lite:** minimal (**по умолчанию**), low, medium, high;
  - **3.8 Flash:** low, medium (по умолчанию), high. `minimal` возвращает ошибку;
  - **3.6 Flash:** minimal, low, medium (по умолчанию), high;
  - **4 Argon:** low, medium (по умолчанию), high. Отключить рассуждения нельзя.
- Даже `minimal` не гарантирует нулевых рассуждений: на сложных задачах модель всё равно немного думает.
- `thinking_budget` используется в линейке 2.5. На Gemini 4 он устарел, а передача вместе с `thinking_level` даёт ошибку 400.
- Токены рассуждений оплачиваются как выходные (`thoughtsTokenCount`). **`max_output_tokens` включает рассуждения:** если лимит кончился во время рассуждений, ответ будет обрезан или пуст, а рассуждения всё равно оплачиваются. Поэтому лимит нельзя ставить слишком низким. Для батча проверок нужно около 8k, а пустой ответ обрабатывать как повод для ретрая.
- Риск: в ветке discuss.ai.google.dev «thinkingLevel: 'low' producing unpredictable 60k+ thought-token spikes» жалуются на всплески до 60k токенов рассуждений.
- Рекомендация: для нарезки ставить Flash-Lite с `minimal` или `low`. Для проверки требований ставить `low` или `medium`. Для противоречий во всём наборе ставить `medium`, или `high` на 3.8 Flash. Выбор сравнить на эталоне.
- Рекомендации по temperature для Gemini 3 на этой странице нет.

### 1.7 PDF напрямую (ai.google.dev/gemini-api/docs/document-processing, обновлено 2026-10-08) [офиц.]

- Лимит один для inline и для Files API: до 50 МБ или 1000 страниц. Files API бесплатен, файлы хранятся 2 дня.
- Каждая страница стоит 258 токенов как изображение. **Встроенный текст PDF извлекается и передаётся модели, и эти токены не тарифицируются.**
- Это хороший запасной путь для сканов без текстового слоя и для таблиц, которые плохо извлекаются локально.

---

## 2. Таблица провайдеров

Качество на русском нигде нормально не измерено. Оценки в колонке взяты из общей репутации моделей [не проверено] и должны сравниваться на нашем эталоне дефектов.

| Провайдер / модель | Бесплатно / цена | Structured output | Русский (оценка) | Контекст | Плюсы | Минусы | Источник, дата |
|---|---|---|---|---|---|---|---|
| **Gemini `gemini-3.5-flash-lite`** | Free: 15 RPM / 250k TPM / 500 RPD. Paid: $0.30 / $2.50 | JSON Schema (подмножество), Pydantic | Хороший (Gemini сильна в мультиязычности) [не проверено] | 1M / 65k вых. | Рекомендована кейсом, ключ за 2 минуты, PDF на вход, KZ поддерживается | На бесплатном тарифе данные идут в обучение, лимиты «не гарантированы» | Цены и фичи [офиц. 2026-11-25]; лимиты [imisofts 2026-10-21, aifreeapi 2026-11-15] |
| Gemini `gemini-3.1-flash-lite` | Free как у 3.5 Flash-Lite. Paid: $0.25 / $1.50 | то же | Хороший [не проверено] | 1M | Отдельная квота, запасная модель в том же ключе | Предыдущее поколение | то же |
| Gemini `gemini-3.8-flash` | Free: **5 RPM / 20 RPD**. Paid: $0.75 / $3.75, с 01.01.2027 $1.50 / $7.50 | то же, thinking low/med/high | Очень хороший [не проверено] | 1M / 65k | Сильнейшая бесплатная Gemini | 20 RPD хватает на 1–3 «трудных» вызова в день | [офиц.] + [3-я сторона] |
| Gemini `gemma-4-31b-it` | Бесплатно, около 30 RPM и 14 400 RPD [3-я сторона], TPM неизвестен | По БД litellm есть | Неплохой (Gemma мультиязычна) [не проверено] | 262k | Самый большой дневной лимит в том же ключе | TPM и JSON-режим не подтверждены, качество ниже Flash | [3-я сторона 2026-10-21], [БД litellm] |
| **Groq** `openai/gpt-oss-120b`, `qwen/qwen3.8-27b` | Free: 30 RPM / 1K RPD / **8K TPM / 200K TPD**. Paid: gpt-oss-120b $0.15 / $0.60 | JSON Schema (OpenAI-совместимо) [БД litellm: supports_response_schema] | gpt-oss на русском средне, Qwen хорошо [не проверено] | 131k | Очень быстрый, OpenAI-совместимый API | 8K TPM: документ с инструкциями занимает почти всю минуту. Llama из бесплатного тарифа убрали [заголовки 3-х сторон] | console.groq.com/docs/rate-limits [офиц. 2026-11-25] (таблица «Free/Developer base limits») |
| **OpenRouter** `*:free` | **20 RPM; 50 RPD**, если за всё время куплено меньше $10 кредитов, и **1000 RPD** при $10 и больше. Лишние ключи или аккаунты лимит не поднимают | Зависит от модели | Зависит от модели | разный | Один ключ на много моделей, OpenAI-совместимо. 429 приходит с `Retry-After` и `X-RateLimit-*` | Набор бесплатных моделей часто меняется, бесплатные эндпоинты могут логировать запросы | openrouter.ai/docs/api/reference/limits.md [офиц. 2026-11-25] |
| **Cerebras** `gpt-oss-120b`, `qwen-3.8-27b` | **Free Trial: $5 кредитов на 30 дней.** gpt-oss-120b: 5 RPM, 30K TPM без кеша, 1M TPD. qwen-3.8-27b: 300 RPM, 150K TPM без кеша (временно увеличено). Paid: gpt-oss-120b $0.35 / $0.75 [БД litellm] | Есть [БД litellm] | как выше | 64–131k | Очень быстрый, у Qwen щедрый TPM | Это пробный период, а не постоянный бесплатный тариф: жюри придётся регистрироваться | inference-docs.cerebras.ai/support/rate-limits [офиц. 2026-11-25, дата лимитов не указана] |
| Mistral La Plateforme (план Experiment) | Бесплатно с подтверждением телефона. Порядка 1 RPS и 1B токенов в месяц [не проверено, по памяти 2024–2025]. Paid: mistral-large-latest $0.70 / $2.10, mistral-small-latest $0.15 / $0.60 [БД litellm] | JSON Schema | Хороший у Large [не проверено] | 262k–1M | EU-вендор | На бесплатном плане данные, вероятно, идут в обучение [не проверено] | docs.mistral.ai/deployment/ai-studio/tier не прочитан |
| GitHub Models | Бесплатно с GitHub-аккаунтом, лимиты по классам моделей (около 10–15 RPM, 50–150 RPD, около 8k токенов на вход за запрос) [не проверено, по памяти 2025] | OpenAI-совместимо | зависит от модели | ограничен лимитом на запрос | Есть у всех студентов | Ограничение на размер запроса может не вместить документ | docs.github.com/.../prototyping-with-ai-models не прочитан |
| Cloudflare Workers AI | Около 10 000 «neurons» в день бесплатно [не проверено]. В changelog от 2026-07-28 есть запись «models require Workers Paid» | Частично | слабо или средне | разный | — | Часть моделей перевели на платный план | developers.cloudflare.com (заголовок changelog) |
| NVIDIA NIM (build.nvidia.com) | Бесплатно для прототипов, **40 RPM**, около 1000 кредитов [заголовки на форуме NVIDIA, 2026]. Модели: gemma-4-31b-it, deepseek-v4.1-flash [БД litellm] | OpenAI-совместимо | зависит от модели | разный | Много открытых моделей | Нужна регистрация в NVIDIA, условия для продакшена неясны | forums.developer.nvidia.com [не проверено] |
| DeepSeek | Бесплатного тарифа нет. `deepseek-v4-flash` $0.30 / $1.20 [БД litellm]. `deepseek-chat` и `deepseek-reasoner` выводятся с 2026-07-24 [БД litellm]. В апреле 2026 V4 Pro подешевел на 75% [Engadget/TNW, заголовки] | JSON mode | Хороший [не проверено] | 1M | Очень дёшево | Данные хранятся в КНР, для банка это минус. Официальная страница цен не прочитана | api-docs.deepseek.com не прочитан |
| Qwen (Alibaba Model Studio, intl) | Для новых пользователей есть бесплатная квота (страница «Free quota for new users» существует, условия срока меняли). `qwen3.8-flash` $0.15 / $0.47 [БД litellm] | JSON | Хороший [не проверено] | около 1M | Дёшево | Регистрация в Alibaba Cloud, нужна карта [не проверено] | alibabacloud.com/help/en/model-studio/new-free-quota не прочитан |
| Together / Fireworks | Стартовые кредиты (порядка $1–5) [не проверено] | JSON | зависит от модели | — | — | Нужна карта или пополнение [не проверено] | не проверено |
| **Anthropic Claude** | Бесплатного тарифа нет. **Haiku 5.5: $0.10 / $0.50** (промпт ≤100k). Sonnet 5.5: $2 / $10. Opus 5.5: $4 / $20. Batch −50%, cache hit 0.05–0.1× | Tool use / structured outputs | Отличный [не проверено] | 1M / 128k вых. | Казахстан поддерживается, сильное качество | Нужна карта. **Подписки Claude Code не дают API-ключ.** Новый токенизатор даёт примерно +30% токенов | platform.claude.com/docs/en/about-claude/pricing.md и models/overview.md [офиц. 2026-11-25] |
| OpenAI | Бесплатного тарифа нет. `gpt-6-luna` $0.10 / $0.50, `gpt-5.4-nano` $0.20 / $1.25, `gpt-5.4-mini` $0.75 / $4.50, `gpt-6.2-sol` $2 / $10, `gpt-5.5` $5 / $30. `gpt-5-mini` и `gpt-5-nano` выводятся с 2026-12-11 | JSON Schema strict | Отличный [не проверено] | 272k–1M | — | Нужна карта. Цены не сверены с openai.com | [БД litellm 2026-11-25] |
| Ollama (локально) | Бесплатно, нужен ПК с 16+ ГБ RAM или GPU | `format=<json schema>` | Gemma 4, Qwen 3.x: хорошо. gpt-oss: средне | зависит от модели | Офлайн, данные не уходят наружу | Медленно на ноутбуке. Жюри вряд ли станет ставить. Теги моделей не проверены | заголовки: «Лучшие локальные нейросети в 2026: Qwen3.6, Gemma 4, gpt-oss и Phi-4» [не проверено] |

Про Ollama: в `.env.example` кейса указан `qwen2.5-coder:7b`. Это устаревшая модель для кода, для русской прозы требований она не подходит. Если нужен локальный вариант, стоит попробовать Gemma 4 (например, 26B-A4B MoE) или Qwen 3.x instruct среднего размера. Точные теги нужно сверить на ollama.com/library [не проверено]. Есть и русскоязычные открытые модели (T-lite/T-pro от T-Bank, Vikhr, YandexGPT-5-Lite) [по памяти 2025, не проверено], но в README для жюри это лишняя сложность.

---

## 3. Рекомендуемая цепочка primary → fallback

Логика: жюри поднимет агента со своим бесплатным ключом Gemini. Значит, весь прогон по умолчанию должен укладываться в квоту одного **нового** проекта AI Studio. Квоты считаются отдельно для каждой модели («Each model variation has an associated rate limit» [офиц. rate-limits.md.txt]), поэтому запасной вариант внутри Gemini даёт дополнительный запас.

1. **Primary:** `gemini-3.5-flash-lite` для всех шагов (нарезка, проверка требований батчами, проверка набора, отчёт). Клиент сам держит темп не выше 12 RPM, с запасом к 15.
2. **Усиленная модель (опционально):** `gemini-3.8-flash` только для 1–3 «дорогих» вызовов: противоречия и полнота набора, финальная сводка. При 429 или исчерпанном RPD переключаемся на primary без падения.
3. **Fallback 1 (тот же ключ):** `gemini-3.1-flash-lite`, у неё отдельная квота.
4. **Fallback 2 (тот же ключ):** `gemma-4-31b-it`, если TPM у неё окажется достаточным.
5. **Внешний fallback (опционально, свой ключ):** OpenRouter `:free`: 50 RPD хватает примерно на 1 прогон в день, после покупки на $10 будет 1000 RPD. Groq `openai/gpt-oss-120b` / `qwen/qwen3.8-27b` подходит только для коротких вызовов из-за 8K TPM.
6. **Платный режим для демо (опционально):** `claude-haiku-5-5` (около $0.06 за прогон) или `claude-sonnet-5-5` для сравнения качества на демо. Сдача от него не зависит.
7. **Если все провайдеры отказали:** отчёт с паспортом, результатами линтера правил и разделом «Диагностика» (лимит или ключ). Ненулевой код возврата, но файл отчёта создаётся всегда.

Предлагаемый `.env.example`:

```dotenv
# Получить ключ: https://aistudio.google.com/ → Get API key (бесплатно, карта не нужна)
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_gemini_api_key_here
# Основная модель: бесплатный тариф около 15 запросов в минуту и 500 в день (сверить в AI Studio → Rate limit)
GEMINI_MODEL=gemini-3.5-flash-lite
# Модель для 1–3 сложных вызовов (бесплатно около 5 RPM и 20 RPD); пусто = использовать GEMINI_MODEL
GEMINI_MODEL_STRONG=gemini-3.8-flash
# Запасные модели в том же ключе (у каждой своя квота)
GEMINI_FALLBACK_MODELS=gemini-3.1-flash-lite,gemma-4-31b-it

# Ограничение темпа на стороне клиента и кеш ответов
LLM_MAX_RPM=12
LLM_CACHE_DIR=.cache/llm
LLM_MAX_OUTPUT_TOKENS=8192

# --- Другие провайдеры (необязательно) ---
# OPENROUTER_API_KEY=
# OPENROUTER_MODEL=<модель с суффиксом :free из https://openrouter.ai/models?q=free>
# GROQ_API_KEY=
# GROQ_MODEL=openai/gpt-oss-120b
# ANTHROPIC_API_KEY=
# ANTHROPIC_MODEL=claude-haiku-5-5
# OPENAI_API_KEY=
# OPENAI_MODEL=gpt-6-luna          # имя из БД litellm, сверить с openai.com
# OLLAMA_BASE_URL=http://localhost:11434
# OLLAMA_MODEL=<gemma4 / qwen3.x instruct; сверить тег на ollama.com/library>
```

---

## 4. Бюджет одного прогона

### 4.1 Токены

Документ около 13 КБ русского текста, это 7–13 тысяч символов (кириллица в UTF-8 занимает 2 байта на символ). Ориентир: **3–5 тысяч токенов** [оценка; измерить через `client.models.count_tokens`]. У Claude 4.7+ токенизатор выдаёт примерно на 30% больше токенов [офиц.].

| Шаг | Вызовов | Вход на вызов | Выход на вызов (с рассуждениями) |
|---|---|---|---|
| Нарезка на атомарные требования | 1–2 | около 7k (документ + инструкция) | 3–5k (цитаты) |
| Проверка требований батчами по 6–8 | 5–8 | около 6–8k (критерии + контекст + батч) | 2–3k |
| Проверка набора (противоречия, полнота, трассировка) | 2–4 | около 10k | 2–4k |
| Тест-оракул (опционально) | 5–20 | около 5k | 1.5–2k |
| Сводка или резюме отчёта | 1–2 | около 10k | 2–3k |
| **Итого** | **15–40** | **120k–320k** | **40k–180k** |

### 4.2 Сравнение с бесплатными лимитами

| Модель (free) | Укладывается ли прогон | Минимальное время прогона | Прогонов в сутки на проект |
|---|---|---|---|
| gemini-3.5-flash-lite (15 RPM, 250k TPM, 500 RPD) | Да | 15 вызовов около 1 мин, 40 вызовов около 3–4 мин при темпе 12 RPM; по TPM 320k входа растягивается на 2 минуты и больше | **12–33** |
| gemini-3.8-flash (5 RPM, 20 RPD) | Нет, если гнать все вызовы | 40 вызовов при 5 RPM занимают 8+ минут | 0–1. Брать только для 1–3 вызовов |
| Связка: Flash-Lite + 2 вызова на 3.8 Flash | Да | 2–4 мин | около 10 (ограничивает Flash) |
| gemma-4-31b-it (30 RPM, 14 400 RPD, TPM ?) | Зависит от TPM | ? | много, если TPM позволяет |
| Groq gpt-oss-120b (30 RPM, 8K TPM, 200K TPD) | С трудом: 8K TPM означает примерно 1 вызов в минуту, а 200K TPD около одного прогона в день | 15–40 мин | около 1 |
| OpenRouter :free (20 RPM, 50 RPD без покупки или 1000 RPD после $10) | Да | 1–2 мин | 1 без покупки, около 25 после $10 |

Для разработки: 2–3 студента с отдельными проектами AI Studio получают по 500 RPD на Flash-Lite каждый. Этого хватает на 30–90 полных прогонов в день, если включён кеш. Создавать много проектов, чтобы обойти лимиты, не стоит: условия Google могут это запрещать [не проверено].

### 4.3 Стоимость прогона на платных моделях

Сценарии: минимальный (15 вызовов, 120k вход и 40k выход), базовый (25 вызовов, 200k и 80k), максимальный (40 вызовов, 320k и 180k). Расчёт без кеша и batch-скидок.

| Модель | $ вход / выход за 1M | мин | база | макс | 100 документов (база) |
|---|---|---|---|---|---|
| Gemini 3.5 Flash-Lite | 0.30 / 2.50 | $0.14 | $0.26 | $0.55 | $26 |
| Gemini 3.1 Flash-Lite | 0.25 / 1.50 | $0.09 | $0.17 | $0.35 | $17 |
| Gemini 3.8 Flash (до 31.12.2026) | 0.75 / 3.75 | $0.24 | $0.45 | $0.92 | $45 |
| Gemini 3.8 Flash (с 01.01.2027) | 1.50 / 7.50 | $0.48 | $0.90 | $1.83 | $90 |
| Claude Haiku 5.5 | 0.10 / 0.50 | $0.03 | $0.06 | $0.12 | $6 |
| Claude Sonnet 5.5 | 2 / 10 | $0.64 | $1.20 | $2.44 | $120 |
| Claude Opus 5.5 | 4 / 20 | $1.28 | $2.40 | $4.88 | $240 |
| OpenAI gpt-6-luna [БД litellm] | 0.10 / 0.50 | $0.03 | $0.06 | $0.12 | $6 |
| OpenAI gpt-5.4-mini [БД litellm] | 0.75 / 4.50 | $0.27 | $0.51 | $1.05 | $51 |
| DeepSeek V4 Flash [БД litellm] | 0.30 / 1.20 | $0.08 | $0.16 | $0.31 | $16 |
| Groq gpt-oss-120b (paid) [БД litellm] | 0.15 / 0.60 | $0.04 | $0.08 | $0.16 | $8 |

Для Claude с документом в кешируемом префиксе (cache hit 0.05–0.1× от цены входа) входная часть дешевеет в 5–10 раз. Batch API даёт −50%, но для CLI с ожиданием результата он не подходит.

**Для слайда «бизнес-ценность»:** автоматическое первое ревью одного ТЗ стоит от $0 (бесплатный тариф) до $0.03–2.50 на платных моделях и занимает 2–4 минуты. Ручное ревью аналитиком занимает часы (замер команды). Даже на Sonnet 5.5 100 документов в месяц обходятся примерно в $120.

---

## 5. Ретраи, кеш, rate limit, детерминизм

**Rate limit на стороне клиента**
- Ограничитель темпа (token bucket) на модель: `LLM_MAX_RPM=12` для Flash-Lite, примерно 4 для 3.8 Flash. Дополнительно учитывать TPM: перед вызовом прикидывать токены (`count_tokens` или длина/3) и ждать, если минутный бюджет исчерпан.
- Запросы отправлять последовательно или максимум по 2–3 параллельно. Параллельная отправка 40 запросов сразу гарантированно упрётся в 429.

**Ретраи**
- Повторять при 429, 500, 503 и таймаутах: экспоненциальная задержка с джиттером (1, 2, 4, 8, 16 с), максимум 5 попыток (`tenacity` 9.2.1 или встроенные ретраи litellm `num_retries`).
- В 429 от Gemini обычно приходит `RetryInfo.retryDelay`. Если он есть, ждать столько, сколько указано [не проверено на текущем API].
- **Исчерпание суточной квоты (RPD) отличать от минутного лимита.** Если в теле ошибки квота дневная (`PerDay`), повторять бессмысленно: сразу переходить на запасную модель или писать отчёт с диагностикой.
- Невалидный JSON или ошибка Pydantic: один повтор с текстом ошибки в промпте (так делает `instructor`, `max_retries`), затем переход на запасную модель.

**Кеш**
- Дисковый кеш `.cache/llm/<sha256(model + версия промпта + схема + входной текст + параметры)>.json`. Повторный прогон того же документа тратит 0 запросов и даёт идентичный отчёт. Для разработки это главная экономия квоты.
- В паспорт отчёта писать число вызовов, cache hits, токены и стоимость. Это пригодится для раздела «бизнес-ценность».
- Context caching Gemini и prompt caching Claude экономят деньги на платных тарифах. На бесплатном тарифе Gemini явный кеш, скорее всего, недоступен [не проверено].

**Детерминизм**
- Закрепить точное имя модели (не `-latest`) и версию SDK. Структурированный вывод по схеме. Стабильная сортировка находок (ID требования, затем критерий).
- `temperature`: для классификации обычно ставят 0–0.2. **Но для моделей Gemini 3.x Google рекомендовал оставлять temperature по умолчанию (1.0), потому что низкие значения могут приводить к зацикливанию и деградации рассуждений** [по памяти, документация Gemini 3 от ноября 2025; для 3.5/3.8 не проверено]. Нужно сравнить на 3–5 прогонах при 0, 0.2 и 1.0 и посчитать разброс находок.
- В `GenerateContentConfig` есть `seed` [по памяти, не проверено для 3.x]. Полного детерминизма он не гарантирует. У Claude seed нет. Настоящую воспроизводимость даёт кеш. Голосование большинством (2 из 3 прогонов) удваивает или утраивает число вызовов, на бесплатном тарифе это дорого.
- Ограничить `max_output_tokens`, чтобы не ловить всплески рассуждений.

---

## 6. Библиотеки (версии с PyPI на 2026-11-25)

| Библиотека | Версия (дата) | Для чего | Ретраи и rate limit | Замечания |
|---|---|---|---|---|
| `google-genai` | 3.6.1 (2026-11-18), Python ≥3.11 | Официальный SDK Gemini: `response_json_schema`, Pydantic, Files API (PDF), `count_tokens`, thinking | Параметры HTTP-ретраев в README не найдены. Надёжнее обернуть вызов в `tenacity` | Лучший вариант, если основной провайдер Gemini. Пакет `google-generativeai` устарел, его не использовать [по памяти] |
| `litellm` | 1.111.0 (2026-11-25), Python 3.10–3.15 | Единый OpenAI-подобный интерфейс к Gemini, Groq, OpenRouter, Anthropic, Ollama и др., `response_format` с JSON Schema, учёт стоимости | `num_retries`, `fallbacks`, Router с rpm и tpm на модель | Тяжёлая зависимость, обновляется ежедневно, поэтому версию закрепить. Есть БД цен для паспорта прогона |
| `instructor` | 1.17.0 (2026-09-09) | Pydantic-модель на выходе, `instructor.from_provider("google/...")`, повтор при ошибке валидации | `max_retries` (повтор с ошибкой валидации в промпте) | Подходит, если хочется «схема → объект» без своей обвязки |
| `pydantic-ai` | 2.74.0 (2026-11-25), Python ≥3.11 | Агентный каркас, типизированный вывод, смена модели одной строкой | Встроенный FallbackModel (переход на другую модель при сбое) | Избыточен для конвейера из 15–40 вызовов, но fallback «из коробки» удобен |
| `openai` | 3.59.0 (2026-11-24) | OpenAI-совместимые API: Groq, OpenRouter, Cerebras, NVIDIA, Ollama (`/v1`) | `max_retries` в клиенте | Один клиент на всех запасных провайдеров |
| `tenacity` | 9.2.1 (2026-10-07) | Ретраи с экспоненциальной задержкой | — | — |
| `ollama` | 0.6.3 (2026-09-29) | Локальный запуск, `format=` с JSON-схемой | — | — |

Практичный вариант: тонкий собственный интерфейс `LLMClient.generate(prompt, schema) -> pydantic` с двумя реализациями, на `google-genai` (основная) и на `openai` (все OpenAI-совместимые запасные). Плюс свои кеш, ограничитель темпа и `tenacity`. Альтернатива: `litellm` вместо обеих реализаций.

---

## 7. Риски и что не удалось проверить

**Риски**
1. Бесплатные квоты Google за 2026 год уже несколько раз урезали: у Flash было 1500 RPD, стало 20 [3-я сторона]. К дате проверки жюри они могут снова измениться. Нужны запасные модели и понятная диагностика в отчёте вместо трейсбека.
2. «Specified rate limits are not guaranteed». Даже в пределах лимита возможны 429 и 503 в часы пик. Обязательны ретраи.
3. С 2026-12-01 `gemini-2.5-flash` перенаправляется на `gemini-3.5-flash-lite`, а 3.8 Flash и Argon дорожают. Модели 2.5 не использовать.
4. Бесплатный тариф означает, что данные идут в обучение и их могут читать люди. Для настоящего банковского ТЗ нужен платный ключ. Это стоит упомянуть в README и на питче.
5. Синтаксис structured output в SDK менялся: `response_schema`, `response_json_schema`, `response_format`. Закрепить версию `google-genai` и проверить на ней.
6. Всплески токенов рассуждений: задавать `max_output_tokens`.
7. Сетевой доступ: из Казахстана работает, через российский IP или VPN будет отказ по региону.

**Не проверено (общий лимит WebFetch исчерпан)**
- Точные лимиты бесплатного тарифа Gemini (только замеры третьих сторон), TPM и JSON-режим у `gemma-4-31b-it`, есть ли `seed`, рекомендации по temperature для 3.x (на странице thinking их нет), официальный статус алиасов `-latest`.
- Официальные страницы Mistral, GitHub Models, Cloudflare Workers AI, NVIDIA NIM, DeepSeek, Qwen/Alibaba, Together и Fireworks (в таблице только заголовки выдачи, БД litellm и память).
- Цены OpenAI (только БД litellm, openai.com не прочитан).
- Качество моделей на русском: бенчмарков не нашёл, нужно сравнение на эталоне дефектов команды.
- Теги моделей Ollama.

**Чек-лист для друга с ключом Gemini:**
1. AI Studio → Rate limit: выписать RPM, TPM и RPD для `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`, `gemini-3.8-flash` и `gemma-4-31b-it`.
2. Вызов с `response_json_schema` на `gemini-3.5-flash-lite` и на `gemma-4-31b-it`.
3. `count_tokens` на нашем документе (MD и PDF).
4. Отправить 20 запросов подряд и посмотреть, когда и с каким телом приходит 429 (есть ли `retryDelay` и как отличить дневную квоту от минутной).
5. Сравнить `thinking_level` minimal, low и medium на 3.5 Flash-Lite: качество находок, время, `thoughtsTokenCount`.
6. Проверить, работает ли `seed`.

---

## 8. Источники

Официальные (прочитаны 2026-11-25):
- https://aistudio.google.com/docs/models: список моделей Gemini (сюда редиректит ai.google.dev/gemini-api/docs/models)
- https://aistudio.google.com/docs/models/gemini-3.5-flash-lite
- https://aistudio.google.com/docs/models/gemini-3.8-flash
- https://aistudio.google.com/docs/pricing: цены, Free tier, «Used to improve our products»
- https://aistudio.google.com/docs/rate-limits и https://ai.google.dev/gemini-api/docs/rate-limits.md.txt: принципы лимитов, Tier 1
- https://aistudio.google.com/docs/deprecations: даты отключения
- https://ai.google.dev/gemini-api/docs/available-regions (обновлено 2026-10-22): Казахстан
- https://ai.google.dev/gemini-api/docs/structured-output (обновлено 2026-05-18)
- https://ai.google.dev/gemini-api/docs/document-processing (обновлено 2026-10-08)
- https://ai.google.dev/gemini-api/docs/generate-content/thinking (обновлено 2026-10-14)
- https://console.groq.com/docs/rate-limits
- https://openrouter.ai/docs/api/reference/limits.md
- https://inference-docs.cerebras.ai/support/rate-limits
- https://platform.claude.com/docs/en/about-claude/pricing.md
- https://platform.claude.com/docs/en/about-claude/models/overview.md
- https://platform.claude.com/docs/en/api/supported-regions.md: Казахстан
- https://pypi.org/project/google-genai/ , https://pypi.org/project/litellm/ , https://pypi.org/project/instructor/ , https://pypi.org/project/pydantic-ai/ , https://pypi.org/project/openai/ , https://pypi.org/project/tenacity/ , https://pypi.org/project/ollama/

База цен:
- https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json (скачан 2026-11-25)

Третьи стороны:
- https://imisofts.com/blog/gemini-api-free-tier-limits/ (2026-10-21, замер в AI Studio)
- https://www.aifreeapi.com/en/posts/gemini-api-rate-limits-per-tier (обновлено 2026-11-15)
- https://dev.to/hassann/gemini-api-free-tier-in-2026-limits-models-and-when-you-get-billed-4081 (проверено 2026-10-22)
- https://discuss.ai.google.dev/t/gemini-3-8-flash-free-tier-20-rpd-is-too-limited-for-practical-evaluation/180609 (2026-09-03)
- https://discuss.ai.google.dev/t/thinkinglevel-low-producing-unpredictable-60k-thought-token-spikes-on-trivial-prompts-systemic-billing-impact/169408 (только заголовок)
- https://www.datastudios.org/post/openrouter-api-key-free-limits-free-routes-paid-access-and-byok (только заголовок)
- https://www.getaiperks.com/en/ai/cerebras-free-tier-guide (только заголовок)
- https://klymentiev.com/blog/groq-pricing и https://ampm-aiops.com/en/guides/groq-free-tier-llama-removed-gpt-oss-2026/ (только заголовки: Llama убрали из бесплатного тарифа Groq)
- https://developers.cloudflare.com/changelog/post/2026-07-28-models-require-workers-paid/ (только заголовок)
- https://forums.developer.nvidia.com/t/request-to-credit-rate-limit-increase-from-1-000-5-000-credits-40-200-rpm/377291 (только заголовок: 40 RPM, 1000 кредитов)
- https://engadget.com/2180062/deepseek-permanently-reduces-the-price-of-its-flagship-v4-model-by-75-percent (только заголовок)
- https://www.alibabacloud.com/help/en/model-studio/new-free-quota (не прочитан)
- https://docs.mistral.ai/deployment/ai-studio/tier (не прочитан)
- https://docs.github.com/en/github-models/use-github-models/prototyping-with-ai-models (не прочитан)
- https://omidsaffari.com/ru/blog/best-local-llms-2026-ru (только заголовок)
