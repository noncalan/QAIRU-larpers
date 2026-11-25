# 08. Стек агента и eval-driven development за 2 дня

2026-11-25 · исследование для команды (кейс AI Requirements Validator). Опирается на `case-analysis.md` (эталон из 45 дефектов, конвейер D) и не пересматривает его.

Метки: **[проверено]** — проверил сам (скрипт в `research/stack-lab/` или официальная страница); **[по источнику]** — видел в стороннем источнике или в заголовке результата поиска, сам не перепроверял; **[по памяти]** — мои знания, их нужно сверить. Версии пакетов взяты из PyPI JSON API 25.11.2026 [проверено].

---

## TL;DR

1. **Каркас: голый `google-genai` 3.6.1 + `pydantic` 2.14 + свой тонкий адаптер `LLMClient` (≈150 строк: ретраи, кеш, JSONL-лог).** В SDK уже есть всё нужное: `response_schema` / `response_json_schema`, `response.parsed`, `seed`, `HttpRetryOptions(attempts, initial_delay, max_delay, exp_base, jitter, http_status_codes)`, `response.model_version`, `usage_metadata` с `thoughts_token_count` [проверено интроспекцией]. Запасной вариант: **PydanticAI** (2.74, вышел сегодня): мультипровайдерность, ретраи валидации, OTel/Logfire и `pydantic-evals` из коробки, но API быстро меняется, версию нужно закрепить. LangGraph, ADK, CrewAI, smolagents и DSPy-оптимизаторы за 2 дня окупиться не успеют.
2. **litellm не брать.** В 2026 году его дважды компрометировали на PyPI: в марте (TeamPCP) и **21.10.2026 (версия 1.107.0, крадёт учётные данные)** [по источнику: StepSecurity]. Жюри — лид QA банка, и для него это красный флаг. Если провайдер всё же нужно менять одной переменной, это делается собственным адаптером или через `instructor`.
3. **Бюджет вызовов определяет и архитектуру, и eval.** По сообщениям на форуме Google, у Gemini 3.8 Flash на free tier 20 RPD, у Flash-Lite 500 RPD [по источнику, сверить в AI Studio]. Лимиты считаются **на проект, а не на ключ**, RPD сбрасывается в полночь по Тихоокеанскому времени, то есть **в 13:00 по Алматы** [проверено: страница rate-limits]. Значит, один полный прогон в 10–25 вызовов съедает дневной лимит Flash. Отсюда три следствия: кеш ответов на диск обязателен, eval-сопоставление должно быть в основном детерминированным, LLM-судью кешируем или заменяем разметкой через Claude Code на стороне разработчика.
4. **Для Gemini 3 «temperature 0» не годится.** Google прямо рекомендует оставлять `temperature=1.0`: ниже возможны зацикливание и падение качества [проверено: Gemini 3 developer guide]. Воспроизводимость обеспечивают не температурой, а **кешем и режимом replay**, паспортом прогона (запрошенная модель и `model_version` из ответа, хеш входа, хеш промптов, `git describe`), стабильной сортировкой находок и замером стабильности на k прогонах.
5. **Eval — свой `eval.py` + `gold.yaml`, без promptfoo, DeepEval, Inspect и Ragas.** Наша метрика сопоставляет множество находок с множеством эталонных дефектов, а их готовые метрики под это не заточены. Сопоставление идёт так: цитата → номера строк, пересечение по месту, совместимость критерия, затем LLM-судья только для спорных пар, с кешем и ручным override. Метрики: recall (общий, по критичным, взвешенный по важности), precision после триажа, доля «висячих» цитат, pass^k для критичных, Jaccard между прогонами, стоимость.
6. **Защита от подгонки.** Эталон не попадает в промпты (pytest-тест на «утечку» доменных слов), holdout из 2–3 своих документов (другой домен, «чистый» документ для замера ложных срабатываний, документ с мутациями), мутационное тестирование требований: 12 операторов, каждый привязан к критерию 29148. Оптимизатор DSPy на одном документе даёт ровно ту подгонку, которой мы боимся, да ещё и на тысячах вызовов.
7. **PDF: Typst через pip-пакет `typst` 0.15.2.** Готовые колёса на ~30 МБ есть для Windows, macOS (x86/arm) и Linux, системные зависимости не нужны, кириллица, ₸ и казахские буквы работают на встроенных шрифтах. Данные идут как JSON через `sys.inputs`, поэтому экранировать ничего не нужно. С фиксированным `timestamp` PDF выходит побайтно одинаковым [проверено]. WeasyPrint на Windows требует MSYS2+Pango, Playwright тянет Chromium.
8. **Проект на `uv`:** `src`-layout, `[project.scripts] validator = …`, `uv.lock`, `exclude-newer` против свежих скомпрометированных релизов. Для жюри три пути запуска: `uv run validator IN OUT`, `uvx --from git+…@v1.0 validator IN OUT` (без клонирования) и `pip install -r requirements.txt` (экспорт из lock с хешами).

---

## 1. Фреймворки

### Таблица

Оценки от 1 (плохо) до 5 (отлично) применительно к нашей задаче: линейный конвейер из 5–8 шагов, структурированный JSON, бесплатный Gemini, 2 дня, 2–3 человека с Claude Code.

| Вариант | Версия (PyPI, 25.11) | Освоение | Structured output | Ретраи | Наблюдаемость | Воспроизводимость | «Архитектура» для жюри | Вес и риски | Вердикт |
|---|---|---|---|---|---|---|---|---|---|
| **google-genai + pydantic (+ свой адаптер)** | 3.6.1 (18.11) | 5: 1–2 ч | 5: `response_schema=Model`, `response.parsed` [проверено, поля есть] | 4: `HttpRetryOptions` для сетевых и 429/5xx; повтор при невалидном JSON пишем сами (~20 строк) | 3→5: `usage_metadata` и `model_version` есть, JSONL-лог пишем сами | 4: `seed` есть, детерминизм не гарантирован; кеш и replay наши | 4: явный конвейер с типизированными контрактами шагов, видно инженерную работу | Лёгкий; привязка к Gemini снимается интерфейсом `LLMClient` | **Основной** |
| **PydanticAI** | 2.74.0 (25.11, сегодня) | 3: 3–5 ч | 5: `output_type`, режимы `NativeOutput` / `ToolOutput` / `PromptedOutput` | 5: ретраи вывода с отправкой ошибки валидации обратно модели (`retries={'output': N}`) | 5: OTel/Logfire встроены, `pydantic-evals` (Dataset/Case из YAML, LLMJudge) | 4 | 4 | Средний; релизы почти ежедневно (2.x), закрепить версию; ключ читается из `GOOGLE_API_KEY` | **Альтернатива** (если нужен второй провайдер для сравнения или OTel из коробки) |
| instructor | 1.17.0 (09.09) | 5: 1 ч | 5: `from_provider("google/gemini-3.8-flash")` | 5: `max_retries` с текстом ошибки валидации | 2 | 3 | 3 | Лёгкий; релизы реже, последний 2,5 мес. назад | Надстройка, если надоест писать повтор при невалидном JSON |
| litellm | 1.111.0 (25.11) | 4 | 4 | 4 | 4 | 3 | 2 | **Две компрометации PyPI в 2026** (март; 21.10 — 1.107.0) [по источнику] | Не брать |
| LangGraph | 1.2.24 | 2: ~1 день | 3: через langchain-google-genai | 4 | 4: с LangSmith (внешний SaaS) | 4: чекпоинты | 4: граф красиво смотрится | Тяжёлый (экосистема LangChain); для линейного конвейера оверкилл | Не брать |
| Google ADK | 2.15.1 (25.11) | 2: ~1 день | 4 | 4 | 4: веб-UI, трассировка | 3 | 4: «нативно для Gemini», `adk eval` [по памяти] | Тяжёлый, сделан под сессии и агентов с инструментами; ADK 2.0 добавил граф Workflow Runtime [по источнику: README] | Только если кто-то уже знает |
| smolagents | 1.26.0 (29.05) | 3 | 2: агенты пишут код, а не JSON по схеме | 2 | 3 | 2 | 2 | Полгода без релизов | Не подходит |
| DSPy | 3.5.0 (10.11) | 2 | 4: типизированные `Signature` | 3 | 3 | 3 | 4: «программирование, а не промптинг» | Оптимизаторы (MIPROv2, GEPA) требуют сотни и тысячи вызовов и на одном документе дают подгонку | Только идею Signature, оптимизатор не запускать |
| CrewAI | 1.15.45 | 3 | 3 | 3 | 3 | 1: ролевые диалоги нестабильны | 3: «мультиагентность» | Много вызовов (free tier), тяжёлые зависимости, Python <3.14 | Не брать |
| AutoGen | 0.7.5 (09.2025) | — | — | — | — | — | — | **Архивирован**; Microsoft советует Agent Framework 1.x [проверено: README] | Не брать |

### Почему именно так

- ТЗ: «жёсткий каркас из нескольких агентов не требуется». Лида QA впечатлит не название фреймворка, а **контракты шагов (pydantic-схемы), трассируемость каждой находки до строки, измеренное качество и воспроизводимость**. Голый SDK показывает это лучше всего: в коде нет «магии», каждый шаг можно отдельно протестировать.
- **Проверить в первый час (не проверено вживую, ключа не было):** что Gemini принимает нашу pydantic-схему. Структурированный вывод поддерживает только подмножество JSON Schema. Держите схемы плоскими: `enum` для критерия и важности, `list[Finding]`, без `dict[str, Any]`, рекурсии и сложных `Union`. Если схема не проходит, есть `response_json_schema` (сырой JSON Schema) [поле есть, проверено].
- Ключ: `google-genai` читает и `GEMINI_API_KEY`, и `GOOGLE_API_KEY`; если заданы обе, побеждает `GOOGLE_API_KEY` [проверено в `_api_client.py`]. В `.env.example` кейса указано `GEMINI_API_KEY` и устаревшая `gemini-1.5-flash`.
- `thinking_level` (Gemini 3) по умолчанию `high`. Для нарезки ставьте `low`: так быстрее и меньше токенов. Для проверки набора оставьте `high`. Записывайте значение в паспорт.
- Модель: все Gemini 3 в статусе preview [проверено: developer guide], а Gemini 2.5, по стороннему блогу, отключили 16.10.2026 [по источнику]. В конфиге нужна цепочка запасных моделей, а в паспорт пишем фактический `response.model_version`.

---

## 2. Дизайн eval

### 2.1. Выбор инструмента

| Инструмент | Версия | Плюсы | Почему не основной |
|---|---|---|---|
| **Свой `eval.py` (~250 строк) + pytest** | — | Метрика ровно под задачу «множество находок против множества эталонных дефектов»; нулевой вес; понятен жюри | — **Рекомендую** |
| pydantic-evals | 2.74.0 | Dataset/Case в YAML, LLMJudge, отчёт | Имеет смысл только вместе с PydanticAI; set-to-set-сопоставление всё равно писать самим |
| promptfoo | npm; MIT, с 2026 часть OpenAI [проверено: README] | Сравнение промптов A/B, YAML | Node-рантайм; наша метрика требует собственного assert-скрипта |
| DeepEval | 4.3.5 | Похож на pytest, G-Eval/DAG | Судья по умолчанию OpenAI; метрики сделаны под ответы и RAG |
| Inspect AI (UK AISI) | 0.3.292 | Логи, просмотрщик, повторы (epochs) [по памяти] | Конвейер придётся заворачивать в solver: ~полдня освоения |
| Ragas | 0.4.4 | — | Только RAG-метрики |

### 2.2. Схема `evals/gold/requirements.gold.yaml`

Эталон описывает **и нарезку, и дефекты**. Если требование не нарезано, все его дефекты пропадут, так что нарезку меряем отдельно. Места задаются **дословными цитатами**, а `eval.py` сам превращает их в номера строк. Так эталон переживает правку пробелов и конвертацию MD→PDF.

```yaml
schema_version: 1
gold_version: "2026-11-25.1"          # меняется при любой правке; хеш файла пишется в eval-отчёт
document:
  path: docs/requirements.md
  sha256: "<sha256 входного файла>"   # eval.py откажется считать метрики, если хеш не совпал
criteria_taxonomy: [necessary, implementation_free, unambiguous, consistent, complete,
  singular, feasible, traceable, verifiable,                     # 29148 п. 5.2.5
  set_complete, set_consistent, set_feasible, set_bounded,       # 29148 п. 5.2.6
  ranked, modifiable]                                            # IEEE 830 п. 4.3
severity_weights: {critical: 3, medium: 2, low: 1}

requirements:                       # эталон нарезки (фрагмент)
  - gid: REQ-F2
    doc_id: "Ф-2"
    quote: "Месяц определяется по дате совершения покупки в Алматы"
  - gid: REQ-AC2
    doc_id: null                    # в документе без номера
    quote: "Покупка 31 января в 23:58 на 4 000 ₸"
  - gid: REQ-API-METHOD
    doc_id: null
    quote: "**REST type** | GET"

defects:
  - id: G01                         # = №1 эталона из case-analysis
    title: "Ф-2 и критерий приёмки 2 по-разному относят покупку 31.01 к месяцу"
    kind: cross                     # single | cross | set
    severity: critical
    criteria: [consistent, set_consistent]       # любой из них засчитывается
    criteria_soft: [unambiguous, verifiable]     # засчитывается с пометкой «критерий неточен»
    locations:                      # для cross нужно попасть хотя бы в одно место, лучше в оба
      - quote: "Месяц определяется по дате совершения покупки в Алматы"
      - quote: "Февральский счётчик вырос на 4 000 ₸"
    must_mention_any: ["январ", "феврал", "31"]  # дешёвый детерминированный фильтр для судьи
    disputed: false
  - id: G04
    title: "Сумма покупки в тиынах, порог в тенге, правило пересчёта не задано"
    kind: single
    severity: critical
    criteria: [consistent, complete]
    criteria_soft: [unambiguous]
    locations:
      - quote: "сумму покупки в тиынах, признак отправки и порог в тенге"
    must_mention_any: ["тиын", "тенге", "100", "единиц"]
  - id: G06
    title: "Сохранение через GET, данные карты попадают в query и логи"
    kind: single
    severity: critical
    criteria: [feasible, implementation_free, consistent]
    criteria_soft: [complete]
    locations:
      - quote: "**REST type** | GET"
      - quote: "curl -G \"$BASE/alerts/saveLimit\""
    must_mention_any: ["GET", "POST", "PUT", "идемпотент", "query", "лог"]
  - id: G40
    title: "Держатель или владелец: кто вправе настраивать"
    kind: cross
    severity: low
    criteria: [unambiguous]
    locations:
      - quote: "Держатель активной карты KZT"
      - quote: "Bearer, сессия владельца карты"
    disputed: true                  # не входит в знаменатель recall; находка не считается ложной
```

Шаблон для `set`-дефектов (№25, №44): `kind: set`, `locations: []`, `scope: [acceptance_criteria]`. Такой дефект сопоставляется только с находками из раздела «Проверка набора».

### 2.3. Алгоритм сопоставления

На входе `run.json` агента. Каждая находка: `id`, `requirement_ids`, `criterion`, `severity`, `quotes[]`, `explanation`, `section` (single или set).

1. **Привязка к месту.** Каждую цитату находки и эталона нормализуем: NFKC, `ё→е`, кавычки и тире к одному виду, удаляем markdown-разметку (`**`, `|`, `` ` ``), схлопываем пробелы. Затем ищем в нормализованном документе: сначала точное вхождение, потом `rapidfuzz.partial_ratio ≥ 90`. Получаем интервал строк. Цитата, которая не нашлась, делает находку **висячей**: она сразу идёт в ложные и считается отдельной метрикой. Верификатор агента должен держать эту долю на нуле, и eval это проверяет.
2. **Кандидаты.** Пара (находка f, эталон g) становится кандидатом, если интервал f пересекается с любым интервалом g (допуск ±1 строка). Для `kind: set` вместо места проверяем `f.section == set` и совпадение `scope`.
3. **Детерминированная оценка.** `score = 0.5·место + 0.3·критерий + 0.2·ключевые слова`. Критерий: 1 для `criteria`, 0.5 для `criteria_soft`, 0 для остальных. Ключевые слова: 1, если в `explanation` есть хотя бы одно из `must_mention_any`. При `score ≥ 0.8` пара засчитывается без судьи, при `< 0.5` отбрасывается.
4. **LLM-судья только для серой зоны** (0.5–0.8). Промпт: «Это один и тот же дефект? yes / no / partial + одна фраза». Ответ кешируется по `sha256(f.explanation + f.quotes + g.id + gold_version)` в `evals/judgments.jsonl`. Ручные поправки лежат в `evals/overrides.yaml` и важнее судьи. При лимите 20 RPD у судьи два варианта: дешёвая модель (Flash-Lite) или **разметка в сессии Claude Code**. Второе возможно, потому что eval работает на стороне разработки, а не в рантайме агента: `eval.py --export-pairs` выгружает спорные пары, Claude Code или человек размечает их, и разметка попадает в `overrides.yaml`.
5. **Назначение.** Жадно по убыванию score, по правилам: один эталонный дефект засчитывается один раз (лишние совпадения помечаются `duplicate`); одна находка закрывает не больше 2 эталонных (иначе находку вида «всё плохо» засчитают за пять).
6. **Триаж несопоставленных находок.** Их отпечаток `sha1(criterion + нормализованная первая цитата)` ищется в `evals/triage.yaml`. Метки: `valid_new` (реальный дефект, которого нет в эталоне, после ревью уходит в эталон с `source: discovered` и новым `gold_version`), `nitpick` (формально верно, но бесполезно), `duplicate`, `false`. Новые отпечатки получают статус `untriaged`, и отчёт выводит их списком, чтобы P3 их разметил.

### 2.4. Метрики

| Метрика | Формула | Цель (ориентир) |
|---|---|---|
| Recall критичных | найденные critical / все critical (без `disputed`) | ≥ 0.9; **главная метрика** |
| Recall общий и по важности | по critical, medium и low отдельно | общий ≥ 0.6 |
| Взвешенный recall | Σ w·found / Σ w, где w = 3/2/1 | растёт от версии к версии |
| Recall нарезки | сопоставленные `requirements` / все | ≥ 0.95 |
| Precision (строгая) | (matched + valid_new) / (всё − duplicate − nitpick) | ≥ 0.8 |
| Доля шума | (nitpick + duplicate) / все находки | ≤ 0.2 |
| Доля висячих цитат | висячие / все находки | **0** |
| Точность места | доля matched, где цитата внутри интервала эталона | ≥ 0.9 |
| Совпадение важности | доля matched, где severity агента = эталону; отдельно счётчик «critical, помеченный как low» | второе = 0 |
| F1 и F2 | F2 добавляет веса recall: пропуск дефекта до разработки дороже лишнего замечания | для README |
| Стабильность | k = 3 прогона без кеша: для каждого g частота p_g; **pass^k** (найден во всех k) для critical; pass@k; средний попарный Jaccard множеств найденных G-id и отпечатков находок | pass^3(critical) ≥ 0.8, Jaccard ≥ 0.7 |
| Стоимость | вызовы, входные, выходные и thinking-токены, секунды, $ по прайсу | для раздела «бизнес-ценность» |

pass^k важнее pass@k: жюри видит **один** прогон, и критичный дефект должен находиться всегда, а не хотя бы раз из трёх.

### 2.5. Формат eval-отчёта

`evals/results/2026-11-26T1830_abc1234/{eval.md, eval.json}` + `evals/baseline.json` (лучший результат, коммитится). Каждый PR с изменением промпта содержит в описании строку дельт.

```markdown
# Eval 2026-11-26 18:30 · agent v0.3-5-gabc1234 · gold 2026-11-25.1 (sha 9f2c…)
Модель: gemini-3.8-flash (model_version из ответа: …) · thinking: low/high · k=3 · кеш: off
| Набор | Recall crit | Recall all | wRecall | Precision | Висячие | pass^3 crit | Jaccard | Вызовы | Токены | Время |
| requirements.md | 0.92 (+0.08) | 0.64 | 0.71 | 0.83 | 0 | 0.83 | 0.74 | 14 | 210k | 3m10s |
| holdout-loans.docx | 0.80 | 0.55 | … |
| mutants-clean.md | mutation score 9/12 | FP на чистом: 2 |
## Пропущенные critical
- G34 «проведённые: авторизация или клиринг» — не найден в 3/3; ближайшая находка F-12 (score 0.42, другой критерий)
## Матрица обнаружения (G × прогон)
G01 ✓✓✓  G02 ✓✗✓  G03 ✓✓✓ …
## Несопоставленные находки (untriaged: 4)
- F-07 [verifiable] «Сервис передаёт push на отправку в течение минуты» — …
```

### 2.6. Как не подогнать агента под один документ

1. **Эталон не попадает в промпты.** `tests/test_prompt_leak.py` ищет в `prompts/` токены документа («порог», «тиын», «Алматы», «saveLimit», «Ф-», «push», «AlertsController») и падает, если нашёл. Доменный чек-лист пишем общими словами: «единицы измерения сумм», «момент списания или авторизации», «часовой пояс», «семантика HTTP-методов».
2. **Holdout из 2–3 своих документов, эталон к ним P3 пишет ДО первого прогона агента:**
   - H1, другой домен и другой формат: например, «Онлайн-заявка на кредит» в DOCX, 10–15 намеренных дефектов, из них 3–4 на стыке разделов.
   - H2, «чистая» спецификация (аккуратно написанный документ того же размера): мерим **ложные срабатывания**. Критичных находок должно быть около нуля.
   - H3, мутанты из H2 (см. ниже). Тот же H2 в PDF заодно проверяет парсер.
   - Holdout прогоняем только на вехах (26.11 вечером и 27.11 до 16:00), а не после каждой правки, иначе подгонка перейдёт на holdout. Если recall на основном документе растёт, а на holdout падает, это и есть сигнал подгонки.
3. **Мутационное тестирование требований.** Детерминированный `evals/mutate.py` вносит в чистый документ по одной мутации на разные требования и сам пишет эталон с местом и критерием. Мутаций 10–12 в одном файле, а не по файлу на каждую: так хватает 1–2 прогонов вместо 12, что важно при RPD. Mutation score считаем по операторам.

   | Оператор | Пример правки | Ожидаемый критерий |
   |---|---|---|
   | M1 размытое слово | «в течение 2 с» → «быстро» | unambiguous, verifiable |
   | M2 убрать единицу или допуск | «2 с (p95)» → «2» | unambiguous, complete |
   | M3 числовое противоречие | дубль требования с 5 000 вместо 1 000 | consistent |
   | M4 смешение единиц | «в тиынах» в одном месте и «в тенге» в другом | consistent |
   | M5 склейка двух требований | «… и …» с разными субъектами | singular |
   | M6 проектное решение | «хранить в PostgreSQL 14 в таблице X» | implementation_free |
   | M7 удалить обработку ошибок | вычеркнуть ветку «если карта заблокирована» | complete |
   | M8 TBD или прочерк | значение поля → «—» / «заполняется позже» | complete |
   | M9 нарушение HTTP-семантики | POST → GET для изменяющего метода | feasible |
   | M10 висячая ссылка | «см. раздел 7.3», которого нет | traceable |
   | M11 дрейф термина | «порог» → «лимит» в одном месте | unambiguous, consistent |
   | M12 критерий приёмки против правила | изменить ожидаемый результат так, чтобы он противоречил Ф-требованию | consistent (cross) |

   Оговорка: мутанты проще настоящих дефектов. Это нижняя планка, а не замена эталону. Возможны «эквивалентные мутанты», которые не создают дефекта, поэтому P3 просматривает их глазами.
4. **DSPy-оптимизатор и подбор few-shot по эталону не используем.** Если нужны примеры в промпте, берём их с holdout-домена или из мутантов, но не из `requirements.md`.
5. **В README честно пишем:** чек-листы промптов составлены с учётом этого документа, обобщаемость проверена на H1–H3 (цифры приводим).

### 2.7. Бюджет вызовов на 2 дня (ориентир)

Если лимит действительно 20 RPD на Flash (сверить в AI Studio в своём проекте), то:

- разработка промптов идёт на кеше и на Flash-Lite (по тем же сведениям 500 RPD), но промпты, настроенные на Lite, могут вести себя иначе на Flash;
- целевая модель тратится только на вехи (k = 3 для стабильности — это 3 прогона, то есть почти весь дневной лимит);
- у каждого участника свой проект Google Cloud, а лимит считается на проект. Не проверял, разрешают ли условия использования заводить несколько проектов ради лимита, так что на это не рассчитываем;
- RPD обнуляется в **13:00 по Алматы**: дорогие прогоны ставим после 13:00;
- платный Tier 1 только для разработки, если у кого-то есть карта: тогда прогоны с k = 5 и финальная проверка без страха перед лимитами. Для сдачи платный ключ не нужен.

---

## 3. Наблюдаемость и воспроизводимость

**Решение: свой JSONL-лог с именами полей из OTel GenAI + кеш на диске + режим replay + автоматический паспорт. Langfuse — опционально, включается переменными окружения.**

1. **Лог вызовов** `runs/<run_id>/calls.jsonl`, одна строка на вызов. Поля названы по конвенциям OpenTelemetry GenAI [по памяти, сверить актуальные имена]: `ts`, `run_id`, `stage` (segment / check_single / check_set / oracle / judge), `gen_ai.request.model`, `gen_ai.response.model` (= `response.model_version`), `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `thoughts_tokens`, `temperature`, `seed`, `thinking_level`, `prompt_id`, `prompt_sha`, `schema`, `cache` (hit / miss / replay), `attempt`, `latency_ms`, `status`, `error`. Полные запрос и ответ лежат в `runs/<run_id>/calls/NNN.json`. Из лога считаются стоимость и время для паспорта и раздела «бизнес-ценность».
2. **Кеш** `.cache/llm/<sha256>.json`. Ключ: `model + prompt_sha + schema_sha + input_chunk_sha + temperature + seed + thinking_level`. Флаг `--cache use|refresh|off`. В разработке кеш по умолчанию включён: рефакторинг кода не тратит RPD. Для замера стабильности ставим `off`.
3. **Replay.** `validator --replay runs/v1.0 docs/requirements.md out.pdf` собирает отчёт из записанных ответов без ключа и сети и сверяет хеши. Это сильный довод о воспроизводимости для жюри: «ваш отчёт побайтно пересобирается из лога прогона» (PDF детерминирован, см. п. 4). Папку `runs/v1.0/` финального прогона коммитим, предварительно убедившись, что в логах нет ключей.
4. **Паспорт прогона** заполняется кодом и пишется в отчёт и в `run.json`: название и путь документа, `sha256`, размер и формат входа, число страниц и символов; дата и время (UTC+5 и UTC); `git describe --tags --always --dirty`. Если `.git` нет (Docker или архив), берём версию из переменной `AGENT_VERSION`, вшитой при сборке, или `importlib.metadata.version`. Если рабочее дерево `--dirty`, это видно сразу. Ещё в паспорте: провайдер, запрошенная модель и фактическая `model_version`, версия SDK, `temperature`, `seed`, `thinking_level`, хеш папки `prompts/`, `run_id`, число вызовов, токены, попадания в кеш, длительность, оценка стоимости, Python и ОС.
5. **Детерминизм там, где он возможен:** находки сортируются по (номер строки, критерий, id), ID присваиваются после сортировки; Jinja и Typst без случайностей; `typst.compile(..., timestamp=фиксированное время)` даёт одинаковый PDF [проверено: два компилирования, совпали sha256].
6. **Температура:** для Gemini 3 оставляем 1.0 (рекомендация Google) и задаём `seed`, хотя гарантий он не даёт. Нестабильность лечим не температурой, а (а) структурированным выводом, (б) верификатором цитат, (в) при наличии бюджета голосованием 2 из 3 для **критичных** находок. Если выбрана модель не из семейства Gemini 3, можно поставить 0, но это снова нужно сверить с документацией модели.
7. **Ретраи:** `HttpRetryOptions(attempts=5, initial_delay=2, max_delay=60, exp_base=2, jitter=1, http_status_codes=[429, 500, 502, 503, 504])` на клиенте [поля проверены, поведение вживую не проверено]. Плюс прикладной повтор (до 2 раз) при `ValidationError` с текстом ошибки в промпте. Если RPD исчерпан, агент не падает: пишет отчёт с паспортом и диагностикой «лимит модели исчерпан, повторите после 13:00 по Алматы или укажите другую модель в `.env`».
8. **Langfuse** (SDK 5.2.0, OTel-based, с января 2026 часть ClickHouse, есть облачный free tier и self-host через docker compose [проверено: README]) включаем, только если кто-то хочет красивые трейсы на демо. Не делайте его обязательным: жюри не станет заводить ещё один ключ.

---

## 4. Генерация PDF

**Рекомендация: Typst через PyPI-пакет `typst` (0.15.2), шаблон `report.typ` читает `report.json` через `sys.inputs`. Markdown-версию отчёта рендерим отдельно Jinja2 из того же JSON.**

Проверено в `research/stack-lab/` (Linux, Python 3.13) на тексте с кириллицей, ₸, казахскими буквами (қ, ә, ғ, ү, ұ, һ, і, ө, ң), «ёлочками» и таблицей:

| Движок | Установка | Системные зависимости | Кириллица и ₸ | Таблицы | Итог |
|---|---|---|---|---|---|
| **Typst (`typst` 0.15.2)** | `pip`/`uv add typst`; колёса ~30 МБ для win_amd64, macOS x86/arm, manylinux и musllinux | Нет; встроенные шрифты Libertinus Serif, New Computer Modern, DejaVu Sans Mono | ✓ (₸ берётся из NCM как запасного шрифта) | ✓ красивые | **Основной.** JSON через `sys.inputs` → спецсимволы `# $ * _ @ < [ \` выводятся как есть, экранирование не нужно; `timestamp` даёт детерминированный PDF; есть PDF/A и PDF/UA (`pdf_standards`) |
| markdown-pdf 1.14 (на PyMuPDF) | `pip`, 32 КБ + PyMuPDF ~65 МБ | Нет | ✓ | ✓ | **Запасной**, если PyMuPDF и так нужен для чтения PDF на входе. Лицензия **AGPL-3.0**: для публичного репозитория допустимо, но банк это заметит |
| fpdf2 2.9.0 | `pip`, чистый Python, 0,4 МБ, LGPL | Нужен TTF-шрифт в репозитории | ✓ | `write_html` падает на `<code>` внутри ячейки; без него колонки одной ширины и выглядит слабо | Только через программный API таблиц |
| pandoc + typst | `pypandoc-binary` (26–41 МБ) + `typst` | Нет | ✓ | ✓ | Работает, но это лишние ~35 МБ и тонкости конвертации; если отчёт строится из JSON, не нужен |
| WeasyPrint 70.0 | `pip` | **Pango**: на Windows MSYS2 и `pacman -S mingw-w64-ucrt-x86_64-pango` либо отдельный exe; на macOS `brew` [проверено: docs] | ✓ (на Linux) | ✓ | Не брать: у жюри на Windows сломается |
| Playwright / Chromium | `pip install playwright` + `playwright install chromium` (~150+ МБ, на Linux ещё `--with-deps` через sudo) [по памяти] | Браузер | ✓ | ✓ | Не брать: тяжело и хрупко |
| reportlab 5.1.1 | `pip` | TTF регистрировать вручную | ✓ (с TTF) | вручную | Многословно, не стоит времени |

Практические советы:
- Порядок такой: сначала `report.json` (истина), из него `report.md` (Jinja2) и `report.pdf` (Typst). Если PDF упал, агент всё равно оставляет MD и JSON и пишет предупреждение, но не падает.
- Шаблон: `#let r = json(bytes(sys.inputs.report))`, `#set text(lang: "ru")`, `table(columns: (auto, auto, 1fr, auto), ..)`. Пример лежит в `research/stack-lab/t_report.typ` и `t_report.py`.
- Встроенным шрифтам доверяем, но задаём явный список `font: ("Libertinus Serif", "New Computer Modern")` и `ignore_system_fonts`, чтобы PDF выглядел одинаково у всех.

---

## 5. Скелет репозитория и команды uv

```text
req-validator/
├── pyproject.toml              # [project.scripts] validator = "validator.cli:app"; requires-python = ">=3.11"
├── uv.lock                     # коммитится
├── .python-version             # 3.12
├── .env.example                # GEMINI_API_KEY=, GEMINI_MODEL=<актуальная модель>, GEMINI_FALLBACK_MODELS=
├── .gitignore                  # .env, .cache/, runs/* (кроме runs/v1.0/)
├── README.md                   # на русском: ключ, установка, одна команда, архитектура, качество (цифры eval), бизнес-ценность
├── Dockerfile                  # опционально: python:3.12-slim + uv sync --locked; ARG AGENT_VERSION
├── src/validator/
│   ├── __main__.py             # python -m validator
│   ├── cli.py                  # typer: validator INPUT OUTPUT [--model] [--cache use|refresh|off] [--replay DIR] [--format pdf|md|json]
│   ├── config.py               # .env → pydantic-модель настроек
│   ├── models.py               # pydantic-контракты: Requirement, Finding, SetFinding, Report, Passport
│   ├── ingest/                 # guard.py (сигнатуры, пустые/битые/зашифрованные), md.py, pdf.py, docx.py
│   ├── pipeline.py             # оркестрация шагов конвейера D
│   ├── segment.py · lint.py · check_single.py · check_set.py · oracle.py · verify.py
│   ├── llm/                    # client.py (протокол LLMClient + GeminiClient), cache.py, calllog.py, replay.py
│   ├── passport.py             # git describe, хеши, версия SDK
│   ├── prompts/*.md            # версионируются; sha папки идёт в паспорт
│   └── report/                 # render_md.py (+ report.md.j2), render_pdf.py (+ report.typ)
├── tests/
│   ├── fixtures/broken/        # 0 байт, мусор, зашифрованный PDF, скан без текста, неверное расширение, битый DOCX
│   ├── test_ingest_guard.py · test_verify_quotes.py · test_report_render.py · test_passport.py
│   ├── test_prompt_leak.py     # промпты не содержат токенов эталонного документа
│   └── test_pipeline_replay.py # сквозной прогон на записанных ответах, без сети
├── evals/
│   ├── gold/requirements.gold.yaml · holdout-*.gold.yaml
│   ├── docs/                   # holdout-*.docx|pdf|md, mutants/
│   ├── eval.py · mutate.py
│   ├── judgments.jsonl · overrides.yaml · triage.yaml
│   ├── baseline.json
│   └── results/<ts>_<sha>/     # eval.md, eval.json
├── runs/v1.0/                  # лог и ответы финального прогона (для --replay)
└── reports/                    # requirements-report.pdf/.md/.json; robustness/ (отчёты на битых файлах)
```

```bash
# старт проекта
uv init --package --name req-validator --python 3.12
uv add google-genai pydantic typer jinja2 typst python-dotenv pyyaml rapidfuzz python-docx pymupdf   # pymupdf: AGPL, см. парсер
uv add --dev pytest ruff
# в pyproject: [tool.uv] exclude-newer = "2026-11-20T00:00:00Z"  — не ставить пакеты новее даты (защита от свежих троянов вроде litellm 1.107.0)
uv lock && uv sync --locked

# ежедневная работа
uv run validator docs/requirements.md reports/requirements-report.pdf
uv run pytest -q
uv run python evals/eval.py --gold evals/gold/requirements.gold.yaml --k 3 --cache off

# для жюри (README)
uv sync --locked && uv run validator <in.pdf|md|docx> <out.pdf>
uvx --from git+https://github.com/<team>/<repo>@v1.0 validator in.pdf out.pdf   # без клонирования
uv export --format requirements.txt -o requirements.txt                          # для pip, с хешами
pip install -r requirements.txt && pip install -e . && validator in.pdf out.pdf
```

Заметки:
- `exclude-newer` принимает и относительный срок (`"7 days"`, в lock пишется `exclude-newer-span = "P7D"`) [проверено на uv 0.11.32]. Для жюри надёжнее **абсолютная дата**: старая версия uv может не прочитать новое поле lock-файла [не проверено].
- Нижняя граница Python — 3.11: её требуют `google-genai`, `typer` 0.29, `fpdf2`, `pymupdf` 2.1. `uv` при необходимости сам ставит Python (`uv python install`), так что жюри достаточно установить только uv.
- Последние версии: uv 0.13.10, typer 0.29.2, pydantic 2.14.4, jinja2 3.1.6, pytest (не смотрел), rapidfuzz 3.14.6, python-docx 1.2.0, pymupdf 2.1, docling 2.152.0 [проверено: PyPI].

---

## 6. Риски и что не удалось проверить

| Риск или пробел | Что делать |
|---|---|
| **Лимиты free tier.** Цифра 20 RPD для Gemini 3.8 Flash взята с форума (сентябрь–октябрь 2026), официальная таблица лимитов больше не публикуется, их показывает AI Studio по проекту | В первый час открыть AI Studio → Rate limits в своём проекте и записать фактические RPM/RPD; под них подстроить число вызовов на прогон и план eval |
| Структурированный вывод Gemini с нашими pydantic-схемами вживую не проверял (не было ключа) | Первый же смоук-тест: одна схема `list[Finding]` с enum-полями |
| `seed` в Gemini не обещает детерминизма | Стабильность меряем k-прогонами, воспроизводимость обеспечиваем replay |
| Gemini 3 в статусе preview: модель могут переименовать или отключить в день сдачи | Цепочка запасных моделей в `.env`, `model_version` в паспорте, replay финального прогона |
| Модель для разработки (Lite) и для сдачи (Flash) ведут себя по-разному | Финальные eval-цифры снимать только на сдаваемой модели |
| Имена полей OTel GenAI привёл по памяти | Сверить со спецификацией OTel semantic conventions for GenAI, если будем упоминать OTel в README |
| Inspect AI (epochs) и ADK (`adk eval`): возможности по памяти | Не критично, мы их не берём |
| Мартовская компрометация litellm: номера версий (по памяти 1.82.7 и 1.82.8) не перепроверял; октябрьская (1.107.0) подтверждена статьёй StepSecurity | Для аргумента в README хватит октябрьского случая |
| Не прочитаны статьи (arXiv заблокирован прокси): «Two Truths and A Lie? Benchmarking Off-the-Shelf LLMs for Requirements Quality Assessment: Performance, False Alarms, and Misses» (2609.03230), «MARQ: A Modular, LLM-Agnostic Requirement Quality Assessor» (2610.36234), «Leveraging LLMs for the Quality Assurance of Software Requirements» (2408.10886) | Если будет время: метрики «false alarms / misses» из первой статьи можно процитировать в README как обоснование eval |
| Typst проверен только на Linux; на Windows и macOS не запускал (колёса есть) | Один участник с Windows или macOS ставит проект по README в первый день |
| Лицензия `typst-py` по метаданным PyPI не видна (сам Typst — Apache-2.0 [по памяти]) | Глянуть LICENSE в репозитории messense/typst-py |
| Не замерял холодную установку на чистой Windows (`uv sync`, ~100 МБ колёс: pymupdf + typst) | Чек-лист «чистая машина» 27.11 |
| Eval-сопоставление на настоящих выходах агента не проверялось: пороги 0.5 и 0.8 подобраны умозрительно | Откалибровать на первом прогоне: 20 пар разметить руками, сравнить с решением алгоритма |

---

## Источники

- ТЗ кейса и документ требований: `/home/user/bcc-hub/ai-requirements-validator-task/README.md`, `docs/requirements.md`; разбор: `scratchpad/case-analysis.md`
- Gemini API, rate limits (лимиты на проект, сброс RPD в полночь PT, таблицы нет): https://ai.google.dev/gemini-api/docs/rate-limits
- Gemini 3 developer guide (temperature 1.0, thinking_level, список моделей, все в preview): https://ai.google.dev/gemini-api/docs/gemini-3
- Форум Google, «Gemini 3.8 Flash Free Tier 20 RPD Is Too Limited»: https://discuss.ai.google.dev/t/gemini-3-8-flash-free-tier-20-rpd-is-too-limited-for-practical-evaluation/180609
- Сторонний обзор free tier (октябрь 2026, отключение 2.5): https://neuraplus-ai.github.io/blog/gemini-api-free-tier.html
- StepSecurity, litellm 1.107.0 compromised (21.10.2026): https://www.stepsecurity.io/blog/litellm-1-107-0-compromised-pypi-backdoored-wheels-credential-stealer
- Мартовский инцидент litellm (только заголовки): https://openwall.com/lists/oss-security/2026/03/25/5 , https://infoq.com/news/2026/03/litellm-supply-chain-attack/ , https://research.jfrog.com/post/litellm-compromised-teampcp/
- google-genai SDK: https://github.com/googleapis/python-genai (поля `GenerateContentConfig`, `HttpRetryOptions` и логика ключей проверены на установленном пакете 3.6.1)
- PydanticAI (Google, output, evals): https://github.com/pydantic/pydantic-ai , https://pydantic.dev/docs/ai/changelog/
- instructor: https://github.com/567-labs/instructor , https://python.useinstructor.com/integrations/genai/
- AutoGen (архив, переход на Agent Framework): https://github.com/microsoft/autogen
- Google ADK 2.0: https://github.com/google/adk-python
- DSPy: https://github.com/stanfordnlp/dspy
- promptfoo (часть OpenAI, MIT): https://github.com/promptfoo/promptfoo , https://openai.com/index/openai-to-acquire-promptfoo/
- DeepEval: https://github.com/confident-ai/deepeval
- Inspect AI: https://github.com/UKGovernmentBEIS/inspect_ai , https://inspect.aisi.org.uk/
- Langfuse: https://github.com/langfuse/langfuse
- WeasyPrint, установка: https://github.com/Kozea/WeasyPrint/blob/main/docs/first_steps.rst
- typst-py (`sys_inputs`, `timestamp`, `pdf_standards`): https://github.com/messense/typst-py
- uv (`exclude-newer`, `export`, `init --package`): https://docs.astral.sh/uv/ (проверено через `uv help`, 0.11.32)
- Версии пакетов: PyPI JSON API, `https://pypi.org/pypi/<pkg>/json`, снято 25.11.2026 (таблица в `research/stack-lab/pypi-versions.tsv`)
- Статьи (найдены поиском, не прочитаны): https://arxiv.org/abs/2609.03230 , https://arxiv.org/abs/2610.36234 , https://arxiv.org/abs/2408.10886
- Лабораторные скрипты и PDF-образцы: `experiments/08-stack-lab/` (`t_report.typ`, `t_report.py`, `t_typst.py`, `t_fpdf.py`, `out/*.pdf|png`)
