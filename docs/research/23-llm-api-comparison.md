# 23. Какая LLM через API лучше всего подходит агенту: сравнение провайдеров (25.11.2026)

Исследование для информации. Решения D-006, D-008…D-010 (модель агента Claude, вызов через подписку `claude -p`, Opus 5.5 + Haiku 5.5) **не меняет**. Запрос Нурдаулета 25.11: «найти лучший ИИ для нас вообще, через API».

Как собирали: три субагента `researcher` параллельно (западные провайдеры; китайские, open-weight, российские и казахстанские; публичные бенчмарки), затем точечная досверка. Все страницы прочитаны 25.11.2026, если не сказано иное. Общий лимит WebFetch (100 запросов в час на всех агентов) кончался несколько раз, поэтому часть страниц не открылась: такие места помечены **«не проверено»**.

Метки: **[офиц.]** страница провайдера; **[3-я сторона]** статья или агрегатор; **[расчёт]** наш пересчёт по ценам; **[замер]** наш эксперимент на `data/input/requirements.md` (исследование 21).

---

## TL;DR

1. **Для нашего агента лучший выбор сейчас всё равно Claude (Opus 5.5 на поиске, Haiku 5.5 на переносе в JSON).** Это единственная модель, которую мы измерили на нашем документе: проход находит 92,6 % эталона, три прохода вместе 100 % [замер, иссл. 21]. У Anthropic есть то, чего нет у других: встроенные дословные цитаты по документу (Citations API) [офиц.]. Через подписку прогон стоит нам $0 денег. Менять модель за два дня до сдачи нет причин.
2. **Opus 5.5 в верхней группе, но уже не первый по общим индексам.** Artificial Analysis Intelligence Index на 25.11: Fable 5.2 65, GPT-6.2 Astra 62, Gemini 4 Neon 62, Gemini 4 Carbon 60, Opus 5.5 59 (27.10 Opus был первым с 58). Три лидера вышли 17–19.11. Индекс меряет в основном агентную работу и код, а не разбор документов. На Text Arena Opus 5.5 4-й из 421 и 3-й по следованию инструкциям (11.11), верх таблицы в пределах погрешности [офиц.].
3. **Сильнейшие альтернативы, если когда-нибудь понадобится не Claude:** GPT-6.2 Sol (OpenAI, $2/$10, индекс 57, строгий JSON, Казахстан поддерживается) как лучшее соотношение цены и качества; GPT-6.2 Astra и Gemini 4 Neon как самые сильные не-Anthropic модели. За прогон Astra вдвое дороже Opus, а Neon по нашему расчёту дешевле ($1,65 против $2,20) [исправлено фактчекером].
4. **Цена одного прогона нашего масштаба** (≈125 тыс. токенов на вход, ≈85 тыс. на выход, всё на одной модели) [расчёт]: Fable 5.2 и GPT-6.2 Astra ≈$4,4; Opus 5.5 ≈$2,2; Gemini 4 Neon ≈$1,65; Sonnet 5.5, GPT-6.2 Sol, Gemini 4 Carbon ≈$1,1; Grok 4.7 ≈$0,76; Mistral Large 4 ≈$0,27; DeepSeek Flash ≈$0,07–0,14; Haiku 5.5 и GPT-6 Luna ≈$0,06. Наша смесь из D-010 ≈$1,6. Реальная цена зависит от того, сколько модель «думает»: по данным AA, GPT-6.2 Astra тратит на задачу их индекса $4,00, а Opus 5.5 $10,04, хотя токен у Astra вдвое дороже.
5. **Бесплатных API-кредитов, которые мы успели бы получить до 27.11, нет ни у кого.** Студенческие программы Anthropic и OpenAI закрыты или только для США/Канады. Бесплатно без карты: Gemini Flash/Flash-Lite (урезанные лимиты, не хватает даже на один прогон) и GLM-4.7-Flash у Z.ai [офиц.].
6. **Из Казахстана официально доступны Anthropic (список от 12.11.2026), OpenAI и Google Gemini (22.10.2026)** [офиц.]. xAI: не проверено. GigaChat API физлицам только для резидентов РФ [офиц.]. Казахстанские модели (KazLLM, AlemLLM) по API недоступны, только свой сервер [офиц.].
7. **Для банка главный вопрос не цена, а где обрабатываются данные.** DeepSeek хранит и обрабатывает данные в КНР и указывает обучение на них как цель (есть отказ) [офиц.]: на реальных ТЗ банка нельзя. Для закрытого контура годится только open-weight модель на своём сервере (Qwen, DeepSeek, AlemLLM). Это тезис для дорожной карты к финалу, не для сдачи.
8. **По галлюцинациям Opus 5.5 средний** (Vectara: 9,9 % против 6,5 % у GPT-6 Sol и 12,1 % у Gemini 4 Argon) [офиц., обновление 24.11.2026 [исправлено фактчекером]]. Это подтверждает нашу архитектуру «модель предлагает, код проверяет»: цитату сверяет код при любой модели.

---

## 1. Что именно нужно агенту от модели

| Требование | Почему важно | Как проверяли |
|---|---|---|
| Рассуждение на русском | ТЗ русскоязычное, дефекты тонкие (противоречия между разделами, неизмеримые формулировки) | Публичные бенчмарки + наш замер (иссл. 21) |
| Дословные цитаты | Правило 4 CLAUDE.md: каждая находка с цитатой, код сверяет | Citations API, Vectara, наш замер (68–83 % дословно в тексте Opus, 100 % после переноса Haiku) |
| Длинный контекст | Документ ≈6 тыс. токенов, плюс промпт и рубрика | Окно у всех топ-моделей 500K–1M, для нас не ограничение |
| Строгий JSON | Код разбирает находки, отчёт собирается кодом | Документация каждого API |
| Цена прогона | README для жюри, слайд бизнес-ценности | Пересчёт по прайсам, раздел 4 |
| Бесплатно или кредиты | Ключ не пополняем (D-009) | Официальные страницы программ |
| Доступ из Казахстана | Команда и жюри в Астане, демо в MNU | Официальные списки стран |

## 2. Сводная таблица провайдеров

Цены за 1 млн токенов, вход / выход, стандартный тариф. Источники по каждой строке в разделе «Источники».

| Провайдер | Флагман | Средняя | Дешёвая | Строгий JSON по схеме | Цитаты по документу | Казахстан | Бесплатно / кредиты |
|---|---|---|---|---|---|---|---|
| **Anthropic** | Opus 5.5 `claude-opus-5-5` 4 / 20; Fable 5.2 8 / 40 | Sonnet 5.5 2 / 10 | Haiku 5.5 0,10 / 0,50 (промпт до 100K; выше в 5 раз дороже) | да, constrained decoding (`output_config.format`) | **да, Citations API** (`char_location`, `page_location`, блоки); с `output_config.format` одновременно нельзя (400) | да (12.11.2026) | нет; программа Claude Campus закрыта на осень 2026 |
| **OpenAI** | GPT-6.2 Astra `gpt-6.2-astra` 8 / 40 | GPT-6.2 Sol `gpt-6.2-sol` 2 / 10; GPT-6.1 Terra 1 / 5 (не проверено) | GPT-6 Luna `gpt-6-luna` 0,10 / 0,50 (не проверено) | да, Structured Outputs `strict` | прямого аналога нет (File Search не проверено) | да | нет; Codex for Students только США/Канада и не для API |
| **Google** | Gemini 4 Neon `gemini-4-neon` 3 / 15 [3-я сторона]; Gemini 4 Carbon и Argon 2 / 10, **с 01.12.2026 4 / 20** (для Carbon не проверено: на странице цен только Argon) | Gemini 3.8 Flash 0,75 / 3,75 (с 01.01.2027 вдвое дороже) | Gemini 3.5 Flash-Lite 0,30 / 2,50 | да, JSON по схеме (constrained decoding прямо не заявлен) | прямого аналога нет | да (22.10.2026) | free tier только Flash / Flash-Lite, лимиты малы |
| **xAI** | Grok 4.7 `grok-4.7` 2 / 6 (500K контекст) | Grok 4.3 1,25 / 2,50 | нет | да, `json_schema`, «guaranteed to match» для поддерживаемых конструкций | нет | не проверено | не проверено |
| **DeepSeek** | `deepseek-v4-pro` 1,32 / 3,96 | | `deepseek-flash` 0,30 / 1,20 (вне пика вдвое дешевле) | **нет**, только `json_object` | нет | оплата не проверена | нет; **данные в КНР** |
| **Qwen (Alibaba)** | `qwen4-max` 1,5 / 4,5 | `qwen3.7-plus` 0,4 / 1,6 | `qwen3.8-flash` 0,15 / 0,47 | `strict` есть, но «Singapore region models are not supported yet» (стр. от 24.11.2026) | нет | не проверено | 1M токенов на модель, только Сингапур, 90 дней |
| **Mistral (ЕС)** | `mistral-large-4` 0,70 / 2,10 | | `mistral-small-4` 0,15 / 0,60 | да, `json_schema` + `strict` | нет | не проверено | план Experiment, условия не проверено |
| **Moonshot Kimi** | `kimi-k3` 3 / 15 [3-я сторона] | | | `strict` по своей схеме MFJS, с оговорками | нет | не проверено | нет |
| **Z.ai (GLM)** | `GLM-5.3` 1,4 / 4,4 | | `GLM-5.3-Flash` 0,15 / 0,50 | нет, только `json_object` | нет | не проверено | **GLM-4.7-Flash и GLM-4.5-Flash бесплатно** |
| **Yandex AI Studio** | Alice AI LLM 4,10 / 9,84 (128K) | YandexGPT Pro 5.1 6,56 / 6,56 (окно 32K) | | `json_schema` без флага `strict` | нет | продаётся через ТОО в КЗ, нерезиденты в USD; AI Studio в регионе КЗ не подтверждено | нет |
| **Сбер GigaChat** | GigaChat-2-Max 650 ₽ за 1M | Pro 500 ₽ | Lite 65 ₽ | `json_schema` + `strict` (примеры на Max) | нет | **физлица только резиденты РФ** | 365 млн токенов на год, только резидентам РФ |
| **Казахстан: KazLLM, AlemLLM** | API нет, только self-host (AlemLLM 247B MoE, Apache 2.0, gated на Hugging Face) | | | | | | |

Batch-скидки: Anthropic, OpenAI, Google −50 % (OpenAI не проверено: таблица Batch на странице не подписана, для фоновых запросов −30 % / −40 %); xAI −20 %; Qwen −50 % (без кеша); Kimi K3 −40 %; DeepSeek вне пика −50 % [офиц.].

## 3. Качество по публичным бенчмаркам

| Модель | AA Intelligence Index, 25.11 | Text Arena (общий) | Vectara, доля галлюцинаций при пересказе (24.11 [исправлено фактчекером]) | AA: цена одной задачи индекса, $ |
|---|---|---|---|---|
| Claude Fable 5.2 | **65** | добавлена 19.11 | нет | 26,54 |
| GPT-6.2 Astra | 62 | добавлена 19.11 | GPT-6 Astra 8,7 % | 4,00 |
| Gemini 4 Neon | 62 | нет данных | нет | 6,44 |
| Gemini 4 Carbon | 60 | нет данных | нет | 7,76 |
| **Claude Opus 5.5** | 59 (27.10 был 1-м с 58) | **1507 ± 6, 4-е из 421; следование инструкциям 3-е** (11.11) | 9,9 % | 10,04 |
| GPT-6.2 Sol | 57 | нет данных | GPT-6 Sol 6,5 % | 1,01 |
| Claude Sonnet 5.5 | 55 | нет данных | нет | 10,02 |
| Gemini 4 Argon | 51 | нет данных | 12,1 % | 9,95 |
| Kimi K3.1 Pro | 47 | | Kimi K2.6 10,8 % | 3,68 |
| Qwen4 Max | 43 | | Qwen3.5-plus 10,7 % | 3,27 |
| Claude Haiku 5.5 | 37 | | нет | 0,41 |
| Grok 4.7 | 35 | | Grok 4.1 fast 19,2 % | 7,77 |
| DeepSeek V4.1 Flash | 32 | | **4,2 %** | 0,60 |
| GLM-5.3 | 32 | | GLM-5 10,1 % | 5,01 |
| Mistral, GigaChat, YandexGPT | нет в топ-30 | | mistral-large-2411 4,5 % | |

Как это читать:

- **Индекс AA** состоит из агентных задач, кода, науки и знаний. К нашей задаче ближе всего его часть про работу с документами (AA-Briefcase, GDP.pdf); в AA-Briefcase лидирует Sonnet 5.5 (статья AA о v5, 27.10.2026).
- **Vectara** ближе всего к нам по смыслу (модель работает строго по данному тексту), но тексты английские. Отрыв GPT-6 Sol от Opus 3,4 п. п.
- **Русский язык.** Читаемого свежего рейтинга нет: таблица MERA Text 2.0 (перезапуск 31.08.2026) рисуется скриптом и не открылась, срез Text Arena по русскому доступен только от 18.02.2026, где первое место у Anthropic, второе у Google (имена моделей скрыты). На русской LLM Arena 13.09.2026 лучшая российская модель GigaChat 3.5 была 7-й, то есть ниже шести зарубежных (пересказ vc.ru, сама арена не проверена). Российских моделей в топ-30 AA нет.
- **Длинный контекст и JSON-бенчмарки** нам почти ничего не говорят: документ ≈6 тыс. токенов, а влияние JSON на качество мы уже измерили сами (со схемой Opus `medium` находит 73 % против 93 % в свободном тексте, иссл. 21).
- **Требования (RE).** Единственная найденная работа с несколькими поколениями моделей (Shefa et al., arXiv 2609.03230, до Opus 4.7 и GPT-5.5) показывает, что новые поколения не дают стабильного прироста на проверке качества требований (разобрано в иссл. 02). Поэтому решать по своему eval, а не по общим рейтингам.

## 4. Цена одного прогона агента

**Модель расчёта [расчёт].** Конвейер D-009/D-010 на 10–12 вызовов: нарезка, три прохода поиска, три переноса в JSON, критик и свидетели, вопросы автору, тест-кейсы. По замерам иссл. 11 и 21 (проход Opus `medium`: ≈4,7–6 тыс. токенов на вход, ≈13–15 тыс. на выход) весь прогон ≈125 тыс. входных и ≈85 тыс. выходных токенов (вместе с размышлением на уровне Opus `medium`). Формула: 0,125 × цена входа + 0,085 × цена выхода. Кеш не учтён: по иссл. 21 он экономит 3–6 %.

| Модель (всё на ней) | $ за прогон | Прогонов на $20 |
|---|---|---|
| Claude Fable 5.2 | 4,40 | 4 [исправлено фактчекером] |
| GPT-6.2 Astra | 4,40 | 4 [исправлено фактчекером] |
| **Claude Opus 5.5** | **2,20** | 9 |
| Gemini 4 Carbon / Argon с 01.12.2026 | 2,20 | 9 |
| Gemini 4 Neon [цена от 3-й стороны] | 1,65 | 12 |
| Kimi K3 [цена от 3-й стороны] | 1,65 | 12 |
| YandexGPT Pro 5.1 / Alice AI LLM | 1,38 / 1,35 | 14 [исправлено фактчекером] |
| Claude Sonnet 5.5, GPT-6.2 Sol, Gemini 4 Carbon / Argon до 30.11 | 1,10 | 18 |
| Grok 4.7 | 0,76 | 26 |
| Qwen4 Max | 0,57 | 35 |
| GPT-6.1 Terra, GLM-5.3 | 0,55 | 36 |
| DeepSeek V4 Pro | 0,50 | 39 [исправлено фактчекером] |
| Gemini 3.8 Flash | 0,41 | 48 |
| Mistral Large 4 | 0,27 | 75 |
| DeepSeek Flash (в пик / вне пика) | 0,14 / 0,07 | 143 / 286 [исправлено фактчекером] |
| Claude Haiku 5.5, GPT-6 Luna, Qwen3.8 Flash | 0,06 | ≈350 |
| **Наша смесь D-010** (Opus `medium` + Haiku + Sonnet) | **≈1,6** (на `xhigh` ≈3,2) | 12 (6) |

**Главная оговорка.** Таблица считает, что все модели пишут одинаково много. Это не так: модели с рассуждением тратят на ту же задачу в разы разное число токенов. Пример из AA (их задачи, не наши): GPT-6.2 Astra стоит за токен вдвое дороже Opus 5.5, а за задачу в 2,5 раза дешевле ($4,00 против $10,04), значит пишет примерно в 5 раз меньше токенов. Точную цену прогона на чужой модели даст только замер на нашем документе.

**Для слайда бизнес-ценности:** даже самая дорогая конфигурация стоит единицы долларов за документ, это меньше часа работы аналитика. Цена модели для банка не главный фактор; главные: качество находок и где обрабатываются данные.

## 5. Доступ из Казахстана и данные

| Провайдер | Доступ из КЗ | Где данные | Годится для реальных ТЗ банка |
|---|---|---|---|
| Anthropic | да [офиц., 12.11.2026] | по умолчанию `inference_geo: global`, только США за +10 % | при согласовании с ИБ банка |
| OpenAI | да [офиц.] | есть региональная обработка (+10 %), детали не проверено | при согласовании с ИБ банка |
| Google Gemini | да [офиц., 22.10.2026]; на free tier запросы могут читать люди | Google | платный тариф, при согласовании |
| xAI | не проверено | не проверено | не проверено |
| DeepSeek | оплата не проверена | **КНР**, обучение на данных с правом отказа [офиц.] | нет |
| Qwen (международный регион) | не проверено | Сингапур и другие регионы, детали не проверено | не проверено |
| Mistral | не проверено | ЕС (детали не проверено) | вероятно, после проверки |
| GigaChat | физлицам только резиденты РФ [офиц.] | РФ (152-ФЗ) | нет для нас |
| Yandex AI Studio | Yandex Cloud работает в КЗ через ТОО, ЦОД в Караганде; есть ли AI Studio в регионе КЗ, не подтверждено | РФ или КЗ, не подтверждено | не проверено |
| Open-weight на своём сервере (Qwen, DeepSeek, AlemLLM, KazLLM) | да | у банка | **да**, это вариант для закрытого контура |

## 6. Рекомендации для нашего агента

1. **Ничего не менять до сдачи.** Claude остаётся (D-006, D-010). Аргументы для жюри и защиты: (а) единственная модель, измеренная на нашем документе и эталоне; (б) Citations API, встроенная проверка дословных цитат на стороне сервера, которой нет у OpenAI, Google и остальных; (в) Opus 5.5 3-й на Text Arena по следованию инструкциям; (г) Anthropic официально работает в Казахстане; (д) через подписку разработка и eval не стоят денег. Стоимость: 0 часов.
2. **В README и на слайд «почему Claude»** взять 3–4 цифры из раздела 3 с датами и честно указать, что к 25.11 вышли модели с более высоким индексом (Fable 5.2, GPT-6.2 Astra, Gemini 4 Neon), а на нашем документе Fable не лучше Opus (иссл. 21). Стоимость: 0,5 часа.
3. **Архитектура не привязана к провайдеру.** У клиента модели уже три реализации (подписка, SDK, replay, D-008). На вопрос жюри «а если банку нельзя слать ТЗ в США?» ответ: добавляется реализация для OpenAI-совместимого API, и тот же конвейер работает на модели внутри контура банка (Qwen, DeepSeek, AlemLLM на своём сервере) или на EU-провайдере (Mistral). Код валидирует JSON Pydantic и сверяет цитаты сам, поэтому слабый JSON-режим у DeepSeek или GLM не ломает агента. Это пункт дорожной карты к финалу 4.12, не к 27.11.
4. **Если после feature freeze появятся время и ключ**, самый полезный эксперимент: прогнать тот же проход поиска на GPT-6.2 Sol (лучшее соотношение индекса и цены задачи: 57 баллов за $1,01) и Gemini 4 Neon на нашем eval и положить цифры рядом с Opus. Это 6–10 вызовов, меньше $5 по API. Без ключа делать нельзя, а ключи мы не покупаем (D-007, D-009), поэтому только по решению Нурдаулета.
5. **Не тратить время** на GigaChat (резидентство РФ), YandexGPT Pro (окно 32K, дорого), DeepSeek на реальных данных (КНР), бесплатный Gemini Flash как основную модель (лимиты не тянут прогон).

## 7. Риски и что не проверено

- **Цены Gemini 4 Neon** ($3 / $15) и **Kimi K3** ($3 / $15) взяты у третьих сторон (apidog.com 19.11.2026, codersera.com): на официальной странице цен AI Studio 25.11 Neon не нашёлся.
- **Цены Gemini 4 Carbon и Argon удваиваются 01.12.2026**, Flash 01.01.2027: расчёты для Gemini устареют через неделю.
- **xAI:** доступ из Казахстана, PDF на вход, кредиты не проверены. Строгий JSON проверен по официальной странице, список моделей с поддержкой не дан.
- **Русский язык:** нет читаемого свежего независимого рейтинга (MERA 2.0, LLM Arena не открылись). Вывод «Claude силён на русском» опирается на наш замер и косвенные данные.
- **AA-Omniscience для Opus 5.5** (50,3 % «угадал вместо отказа» против 4 % у Gemini 4) взята с агрегатора benchlm.ai и в TL;DR не вынесена.
- **Расчёт цены прогона** не учитывает разную «разговорчивость» моделей; реальная цена может отличаться в разы в обе стороны.
- **Способы оплаты** из Казахстана (казахстанской картой) не проверены ни у одного провайдера.
- **Qwen strict JSON** «не поддерживается в Сингапуре» по странице от 24.11.2026, это может скоро измениться.
- **Mistral:** ID моделей взяты из ссылок страницы цен, хранение данных и бесплатный план не проверены.

## Источники

Все прочитаны 25.11.2026, если не указано иное.

Anthropic:
- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/about-claude/models/overview
- https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- https://platform.claude.com/docs/en/build-with-claude/citations
- https://platform.claude.com/docs/en/build-with-claude/pdf-support
- https://www.anthropic.com/supported-countries (политика от 12.11.2026)
- https://claude.com/programs/campus

OpenAI:
- https://developers.openai.com/api/docs/pricing
- https://developers.openai.com/api/docs/models
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/guides/reasoning
- https://developers.openai.com/api/docs/supported-countries
- https://developers.openai.com/hackathons
- https://help.openai.com/en/articles/20001147 (Codex for Students)

Google:
- https://aistudio.google.com/docs/pricing
- https://ai.google.dev/gemini-api/docs/models/gemini-4-argon
- https://ai.google.dev/gemini-api/docs/generate-content/structured-output
- https://ai.google.dev/gemini-api/docs/available-regions (обновлено 22.10.2026)
- https://apidog.com/blog/gemini-4-neon-pricing/ (3-я сторона, 19.11.2026)

xAI:
- https://docs.x.ai/developers/pricing.md
- https://docs.x.ai/developers/models.md
- https://docs.x.ai/developers/model-capabilities/text/structured-outputs.md

Остальные провайдеры:
- https://api-docs.deepseek.com/quick_start/pricing.md
- https://api-docs.deepseek.com/guides/json_mode
- https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html (ред. 10.02.2026)
- https://www.alibabacloud.com/help/en/model-studio/model-pricing.md
- https://www.alibabacloud.com/help/en/model-studio/qwen3-8-flash.md
- https://www.alibabacloud.com/help/en/model-studio/qwen-structured-output (обновлено 24.11.2026)
- https://docs.mistral.ai/inference/pricing
- https://docs.mistral.ai/inference/conversations/structured-output/custom.md
- https://platform.kimi.ai/docs/pricing/chat-k3
- https://platform.kimi.ai/docs/guide/response_format.md
- https://codersera.com/blog/kimi-k3-pricing-api-costs-2026/ (3-я сторона, обновлено 05.10.2026)
- https://docs.z.ai/guides/overview/pricing
- https://docs.z.ai/guides/capabilities/struct-output
- https://platform.minimax.io/docs/guides/pricing-paygo
- https://aistudio.yandex.ru/docs/en/ai-studio/pricing
- https://aistudio.yandex.ru/en/docs/ai-studio/concepts/generation/structured-output
- https://yandex.cloud/ru-kz/docs/billing/qa/non-resident
- https://developers.sber.ru/docs/ru/gigachat/tariffs/individual-tariffs.md
- https://developers.sber.ru/docs/ru/gigachat/tariffs/legal-tariffs.md
- https://developers.sber.ru/docs/ru/gigachat/guides/structured-output (обновлено 20.07.2026)
- https://developers.sber.ru/docs/ru/policies/gigachat-agreement/individuals (ред. 06.02.2026)
- https://huggingface.co/astanahub/alemllm
- https://issai.nu.edu.kz/2026/02/13/issai-explains-kazllm-why-kazakhstan-built-its-own-large-language-model/

Бенчмарки:
- https://artificialanalysis.ai/leaderboards/models/
- https://artificialanalysis.ai/articles/artificial-analysis-intelligence-index-v5 (27.10.2026)
- https://arena.ai/leaderboard/chat/text/models/claude-opus-5.5-high-kmtk (обновление 11.11.2026)
- https://arena.ai/ru/leaderboard/text/russian (снимок 18.02.2026)
- https://github.com/vectara/hallucination-leaderboard (обновление 24.11.2026 [исправлено фактчекером])
- https://benchlm.ai/md/models/claude-opus-5-5.md (агрегатор, 23.11.2026)
- https://habr.com/ru/companies/sberbank/articles/1075592/ (MERA Text 2.0, 31.08.2026)
- https://vc.ru/ai/3146422-reyting-rossiyskih-neyrosetey-gigachat-alice-yandexgpt (18.09.2026)
- https://arxiv.org/pdf/2609.03230 (Shefa et al., 2026)

Внутренние: `docs/research/05-llm-apis.md`, `11-baseline-one-prompt.md`, `21-model-and-effort.md`, `docs/decisions.md` (D-006…D-010).

## Фактчек (25.11.2026)

Проверял `fact-checker`. Страницы открыты 25.11.2026. Сайты OpenAI, Google, DeepSeek, Сбер открывались только через WebFetch (пересказ небольшой моделью, не дословно).

| Утверждение | Вердикт | Правильное значение | Источник |
|---|---|---|---|
| Цены Anthropic: Opus 5.5 4/20, Fable 5.2 8/40, Sonnet 5.5 2/10, Haiku 5.5 0,10/0,50 до 100K, выше в 5 раз (0,50/2,50) | верно | | platform.claude.com/docs/en/about-claude/pricing |
| Batch Anthropic −50 %; `inference_geo` по умолчанию `global`, `us` ×1,1 | верно | | там же |
| Citations нельзя вместе с `output_config.format`, ответ 400 | верно | | platform.claude.com/docs/en/build-with-claude/citations |
| Казахстан в списке стран Anthropic, политика от 12.11.2026 | верно | effectiveDate 2026-11-12 | anthropic.com/supported-countries |
| OpenAI: gpt-6.2-astra 8/40, gpt-6.2-sol 2/10 | верно | | developers.openai.com/api/docs/pricing |
| OpenAI: GPT-6.1 Terra 1/5, GPT-6 Luna 0,10/0,50 | не проверено | в стандартной таблице их нет; в неподписанной таблице (похоже, Batch) Terra 0,50/2,50, Luna 0,05/0,25, Sol 1/5, что согласуется с 1/5 и 0,10/0,50 по стандарту | там же |
| Batch OpenAI −50 % | не проверено | на странице: для фоновых запросов −30 % (окно 2 ч) и −40 % (8 ч); неподписанная таблица с ценами вдвое ниже | там же |
| Региональная обработка OpenAI +10 % | верно | для моделей после 05.03.2026 | там же |
| Gemini 4 Argon 2/10, с 01.12.2026 4/20; 3.8 Flash 0,75/3,75, с 01.01.2027 вдвое; Flash-Lite 0,30/2,50; free tier только Flash/Flash-Lite | верно | | aistudio.google.com/docs/pricing |
| Gemini 4 Carbon 2/10 и рост 01.12 | не проверено | Carbon (и Neon) на странице цен нет | там же |
| Gemini 4 Neon 3/15 | не проверено (3-я сторона, уже помечено) | | |
| AA Index: Fable 5.2 65, Astra 62, Neon 62, Carbon 60, Opus 5.5 59, Sol 57, Sonnet 5.5 55, Argon 51, Haiku 5.5 37, DeepSeek V4.1 Flash 32 | верно | для Claude взяты варианты Max, Neon Xhigh, Carbon/Argon High | artificialanalysis.ai/leaderboards/models |
| AA цена задачи: Fable 26,54, Astra 4,00, Neon 6,44, Carbon 7,76, Opus 10,04, Sol 1,01, Sonnet 10,02, Argon 9,95, Haiku 0,41, V4.1 Flash 0,60 | верно | | там же |
| Text Arena: Opus 5.5 1507 ± 6, 4-е из 421, следование инструкциям 3-е, обновление 11.11.2026 | верно | | arena.ai/…/claude-opus-5.5-high-kmtk |
| Vectara: Opus 5.5 9,9 %, GPT-6 Sol 6,5 %, Gemini 4 Argon 12,1 %, DeepSeek V4.1 Flash 4,2 %, GPT-6 Astra 8,7 %, mistral-large-2411 4,5 % | верно | | github.com/vectara/hallucination-leaderboard |
| Дата Vectara 22.10.2026 | неверно | Last updated 24.11.2026, цифры те же | там же |
| Казахстан у OpenAI и Gemini (страница Gemini от 22.10.2026) | верно | | developers.openai.com/api/docs/supported-countries; ai.google.dev/gemini-api/docs/available-regions |
| DeepSeek: данные хранятся и обрабатываются в КНР, обучение на данных с правом отказа, ред. 10.02.2026 | верно | | cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html |
| GigaChat: физлица только резиденты РФ; 365 млн токенов на год; Max 650 ₽, Pro 500 ₽, Lite ≈65 ₽ за 1M | верно | п. 1.5 соглашения, ред. 06.02.2026 | developers.sber.ru (individuals, individual-tariffs) |
| Арифметика раздела 4 (0,125 × вход + 0,085 × выход) | верно по долларам | | [расчёт] |
| Прогонов на $20 | неверно в 4 строках | Fable/Astra 4 (не 5), Yandex 14 (не 15), V4 Pro 39 (не 40), DeepSeek вне пика 286 (не 287): считаем целые прогоны | [расчёт] |
| TL;DR 3: Astra и Neon «стоят как Opus или дороже за прогон» | неверно | по таблице раздела 4 Neon $1,65 дешевле Opus $2,20 | [расчёт] |
| «Astra за задачу в 2,5 раза дешевле, пишет ≈в 5 раз меньше»; отрыв Vectara 3,4 п. п. | верно | 10,04 / 4,00 = 2,51; 9,9 − 6,5 = 3,4 | [расчёт] |
| AA v5 27.10: Opus 1-й с 58; Shefa et al., arXiv 2609.03230; цены xAI, Qwen, Mistral, Kimi, GLM, Yandex; Claude Campus | не проверено | не хватило лимита запросов | |
