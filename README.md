# QAIRU larpers · AI Requirements Validator

[![CI](https://github.com/noncalan/QAIRU-larpers-/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/noncalan/QAIRU-larpers-/actions/workflows/ci.yml)

Команда **QAIRU larpers** на отборе **BCC-HUB x MNU AI Agent Challenge 2026**. Делаем ИИ-агента, который проверяет документ требований по ISO/IEC/IEEE 29148 и IEEE 830 и выпускает отчёт на русском. Кейс: [BCC-HUB/ai-requirements-validator-task](https://github.com/BCC-HUB/ai-requirements-validator-task).

> **Статус (26.11):** агент проверяет документ одной командой на MD, PDF и DOCX, см. «Как запустить агента». Ниже также настройка компьютера для участников команды. К сдаче README станет полным README агента.

Правила команды и контекст для Claude: [`CLAUDE.md`](CLAUDE.md). Сроки: сдача до **27.11, 23:59** (UTC+5), лучше до 22:00.

## Как запустить агента

Нужны [uv](https://docs.astral.sh/uv/) и [Claude Code](https://code.claude.com/docs/en/setup) с входом в подписку Claude Pro или Max (`claude`, затем `/login`). Ключ API не нужен: агент вызывает модель командой `claude -p` через подписку.

```bash
uv sync
uv run validator data/input/requirements.pdf out/report.md
```

- **Вход:** PDF, DOCX или Markdown. **Выход:** `out/report.md`, рядом `report.json` (то же для машин), `questions.yaml` (вопросы автору с местом для ответов), `report.log` и папка `run/` (каждый вызов модели и ответы целиком).
- **Профиль:** `--profile standard` по умолчанию (3 прохода поиска), `--profile quick` (1 проход, быстрее), `--profile submit` (3 прохода с effort `xhigh`, для отчёта на сдачу).
- **Без модели:** `--backend replay --replay ПАПКА/run` пересобирает отчёт из записанного прогона. Записанные живые прогоны на трёх форматах лежат в `tests/fixtures/e2e/`, например `uv run validator data/input/requirements.md out/report.md --backend replay --replay tests/fixtures/e2e/md/run`.
- **Коды возврата:** 0 проверка выполнена (найденные дефекты код не меняют), 1 внутренняя ошибка, 2 ошибка аргументов, 3 вход прочитан частично, 4 вход не прочитать (битый, пустой, защищённый файл), 5 нет доступа к модели. Отчёт с диагностикой пишется во всех случаях, кроме ошибки аргументов.
- **Сколько это занимает:** профиль `standard` на `requirements.*` (26.11) идёт 8,5–9,5 минуты, цена по тарифу API $2,7–3,2 за прогон (подписка денег не списывает). Цифры по трём форматам и согласие находок: [`evals/results/2026-11-26-formats.md`](evals/results/2026-11-26-formats.md).

---

## Быстрый старт участника (15–20 минут)

### 0. Что понадобится

- Аккаунт GitHub и **принятое приглашение** в этот репозиторий: https://github.com/noncalan/QAIRU-larpers-/invitations
- Подписка **Claude Pro или Max** (бесплатный план Claude Code не включает).
- Ключ **Claude API** понадобится позже, к первому запуску агента (см. шаг 5). Для исследования и экспериментов без модели он не нужен. Подписка Pro/Max сама по себе API-ключа не даёт.
- Git, uv (поставит Python сам).

### 1. Git

- **macOS:** `xcode-select --install` (или `brew install git`).
- **Windows:** [Git for Windows](https://git-scm.com/download/win). Он нужен и самому Claude Code.
- **Linux:** `sudo apt install git`.

### 2. Claude Code

| ОС | Команда |
|---|---|
| macOS, Linux, WSL | `curl -fsSL https://claude.ai/install.sh \| bash` |
| Windows PowerShell | `irm https://claude.ai/install.ps1 \| iex` |
| Windows (winget) | `winget install Anthropic.ClaudeCode` |
| macOS (Homebrew) | `brew install --cask claude-code` |

Проверка: `claude --version`, при проблемах `claude doctor`. Документация: https://code.claude.com/docs/en/setup

### 3. Клонировать репозиторий

```bash
git clone https://github.com/noncalan/QAIRU-larpers-.git
cd QAIRU-larpers-
```

Репозиторий приватный. Если git просит пароль, войди через [GitHub CLI](https://cli.github.com/) командой `gh auth login` (или клонируй через GitHub Desktop).

### 4. uv и Python-окружение

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Перезапусти терминал, затем в папке репозитория:

```bash
uv sync --group experiments
```

uv сам скачает нужный Python (3.11+) и поставит библиотеки для экспериментов. Зависимости агента добавим после выбора идеи.

### 5. Ключ Claude API (можно отложить)

> **25.11:** оплаченного ключа у команды пока нет. До 27.11 модель вызываем через подписку Claude командой `claude -p` (решения D-007 и D-008), поэтому этот шаг пропусти и вернись к нему, когда ключ появится.

1. Возьми ключ: свой в https://platform.claude.com/ → **API keys** → **Create key**, или командный, который тебе передали лично.
2. Создай свой `.env` из примера: `cp .env.example .env` (Windows: `copy .env.example .env`).
3. Вставь ключ после `ANTHROPIC_API_KEY=`.

Вызовы API платные (по токенам), поэтому в экспериментах не гоняй один и тот же прогон много раз: ответы кешируем на диск.

`.env` уже в `.gitignore`, а `.claude/settings.json` запрещает Claude его читать. **Никогда не коммить ключ и не вставляй его в чат, issue или код:** репозиторий станет публичным перед сдачей.

### 6. Запустить Claude Code в репозитории

```bash
claude
```

1. При первом запуске войди в аккаунт Claude в браузере.
2. На вопрос о доверии папке ответь **Yes**: без этого не включатся общие разрешения из `.claude/settings.json`.
3. Выбери модель и усилие:
   - `/model opus`: основная модель. Если лимиты подписки кончаются, `/model sonnet`.
   - `/effort high` для обычной работы, `/effort xhigh` для исследования и важных решений.
4. Проверь, что всё подхватилось: `/agents` покажет `researcher`, `fact-checker`, `idea-critic`; набери `/` и увидишь скиллы, в том числе `/research-sprint`.

Ставить плагины и скиллы отдельно **не нужно**: всё лежит в `.claude/` и подхватывается автоматически. Какие скиллы есть и зачем: [`docs/setup/skills.md`](docs/setup/skills.md).

### 7. Первый промпт

Открой своё issue на GitHub и напиши Claude, например:

```
Прочитай CLAUDE.md и issue #<номер> (gh issue view <номер>). Выполни его.
Используй скилл research-sprint и запускай субагентов параллельно.
Результаты сдай через PR в ветке research/<тема>.
```

Если `gh` не установлен, просто скопируй текст issue в чат.

---

## Запуск агента: модель, кеш и replay

```bash
uv run validator data/input/requirements.md out/report.md --profile quick        # модель через подписку claude -p
uv run validator data/input/requirements.md out2/report.md --backend replay --replay out/run   # без модели
```

- **Бэкенд** (`--backend`): `auto` по умолчанию берёт подписку `claude -p`, если Claude Code установлен и вошёл в аккаунт; если нет, идёт через Claude API при заданном `ANTHROPIC_API_KEY`; если нет и ключа, пересобирает отчёт из `--replay`, иначе пишет отчёт с диагностикой `LLM-001`. `api` только Claude API, `cli` только подписка, `replay` только запись прошлого прогона, `fake` фикстуры тестов.
- **Кеш** (`--cache use|refresh|off`): ответы модели лежат в `.cache/validator/` (в git не попадает). Повторный прогон того же документа с тем же промптом не тратит лимит: в `out/run/calls.jsonl` у вызовов статус `cached`. `refresh` перезапрашивает всё заново.
- **Запись прогона**: `out/run/calls.jsonl` (каждая попытка вызова: модель, токены, цена по тарифу API, статус, ошибка) и `out/run/responses/` (ответы целиком). По этой папке `--backend replay` пересобирает тот же отчёт без модели и без ключа; число проходов и модель поиска берутся из записи.
- **Пример-свидетель и критик находок** (шаг 6, #39): для противоречий и находок со сценарием модель строит конкретный сценарий, в котором места документа дают разные результаты; код сверяет цитаты и различие ожиданий, а отчёт показывает свидетеля в карточке находки. Критик находок (`--critic`) перепроверяет рискованные находки и может понизить важность или снять находку по сверенной цитате-основанию. По eval на записи MD он уронил recall на 2,2 п. п. и не снизил шум, поэтому по умолчанию выключен (цифры в PR по #39).
- **Лимит цены** (`--max-budget-usd X`): считаем по тарифу API (`src/validator/llm/pricing.py`); когда потрачено X, новые вызовы не делаются, в отчёте `LLM-003`. Подписка денег не списывает, цена нужна для паспорта прогона и оценки стоимости проверки.
- **Сбои модели** видны в разделе «Диагностика»: `LLM-001` нет входа или ключа, `LLM-002` лимит подписки или пустой баланс ключа, `LLM-004` перегрузка или сеть (агент сам повторяет вызов один раз), `LLM-012` таймаут, `LLM-013` отказ модели, `LLM-015` нет ответа в записи.

### Запуск через API

Если у вас свой ключ Claude API, агент работает без Claude Code:

```bash
cp .env.example .env          # впишите ключ в строку ANTHROPIC_API_KEY=
uv run validator data/input/requirements.md out/report.md --backend api --profile standard
```

- **Ключ** читается только из окружения или `.env` и никуда не печатается: ни в отчёт, ни в `out/run/`. Без ключа `--backend api` пишет отчёт с диагностикой `LLM-001` и завершается с кодом 5, без трассировки. Бэкенд `auto` берёт ключ, только если Claude Code не установлен или не вошёл в аккаунт: подписка бесплатна, поэтому при обоих путях для API нужен явный `--backend api`.
- **Цена** прогона по тарифу API видна в паспорте отчёта и в `out/run/calls.jsonl`. Оценка по замеру исследования 21: около $1,6 за прогон с проходами поиска на `medium` (`--profile standard`) и около $3,2 на `xhigh` (`--profile submit`). Ограничить расходы можно флагом `--max-budget-usd`.
- **Как вызывается модель** (`src/validator/llm/anthropic_api.py`): официальный SDK `anthropic`, потоковый ответ, модель и effort шага по D-010, ответ по JSON-схеме там, где она есть у шага. Если модель отказалась отвечать, сервер сам передаёт запрос запасной модели (`fallbacks: "default"`); какая модель ответила, видно в паспорте. SDK повторяет `429` и `5xx` сам, агент добавляет ещё один повтор через 20 с. Ответ, который агент отбросил (обрезан на пределе вывода, отказ, не по схеме), всё равно оплачен и входит в цену прогона и в `--max-budget-usd`.
- **Проверено** тестами на записанных синтетических ответах API (`tests/test_llm_api.py`, без сети): у команды нет оплаченного ключа (решение D-009), поэтому живой прогон через API не делался.

## Как работаем

- **Задачи = issues.** Взял задачу, назначь себя. Шаблоны: исследование, задача, баг.
- **Ветка на задачу:** `research/…`, `feat/…`, `fix/…`. В `main` напрямую не пушим.
- **PR** с `Closes #N`, коротким описанием и, когда появится eval, цифрами recall/precision.
- **Параллельная работа:** в отдельном терминале `claude -w <имя>` создаёт свою копию репозитория (git worktree) с отдельной веткой, и сессии не мешают друг другу. `.env` туда копируется автоматически (`.worktreeinclude`).
- **Решения** записываем в [`docs/decisions.md`](docs/decisions.md).

## Карта репозитория

| Путь | Что там |
|---|---|
| `CLAUDE.md` | Правила, сроки, роли, как работать с субагентами |
| `docs/case/task.md` | ТЗ кейса (копия) |
| `docs/case/case-analysis.md` | Разбор кейса и kick-off: критерии жюри, архитектуры, план, модели |
| `docs/case/kickoff-transcript.md` | Расшифровка kick-off 25.11 |
| `docs/gold/defects.md` | Эталон из 45 дефектов. **Спойлер**, только для eval |
| `evals/` | Eval по эталону: судья, recall, цитаты, стабильность; результаты в `evals/results/` ([`evals/README.md`](evals/README.md)) |
| `docs/research/` | Исследование, индекс в `README.md` |
| `docs/ideas/` | Кандидаты идеи |
| `docs/decisions.md` | Журнал решений |
| `docs/setup/skills.md` | Скиллы и агенты Claude Code |
| `data/input/` | Проверяемый документ: `requirements.md` и сгенерированные из него `.docx` и `.pdf` |
| `experiments/` | Скрипты экспериментов фазы исследования |
| `.claude/` | Общие настройки, агенты и скиллы Claude Code |

## Частые проблемы

- **`claude: command not found`:** перезапусти терминал, затем `claude doctor`.
- **git просит пароль или `403`:** прими приглашение в репозиторий и войди через `gh auth login`.
- **Claude API отвечает `401`:** нет ключа или он неверный, проверь `ANTHROPIC_API_KEY` в `.env`. **`429`:** лимит запросов, SDK сам повторит запрос; если не помогло, подожди минуту.
- **Windows:** работай в PowerShell или Git Bash. WSL тоже подходит.
- **Кончились лимиты Claude:** переключись на `/model sonnet` и запускай меньше субагентов одновременно (на Pro разумно 3–5).
