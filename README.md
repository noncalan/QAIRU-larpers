# QAIRU larpers · AI Requirements Validator

Команда **QAIRU larpers** на отборе **BCC-HUB x MNU AI Agent Challenge 2026**. Делаем ИИ-агента, который проверяет документ требований по ISO/IEC/IEEE 29148 и IEEE 830 и выпускает отчёт на русском. Кейс: [BCC-HUB/ai-requirements-validator-task](https://github.com/BCC-HUB/ai-requirements-validator-task).

> **Статус: фаза исследования (25.11).** Агента ещё нет. Сейчас этот README для участников команды: как настроить компьютер и Claude Code. К сдаче он станет README агента: как получить ключ, куда положить, какой командой запустить, куда ляжет отчёт.

Правила команды и контекст для Claude: [`CLAUDE.md`](CLAUDE.md). Сроки: сдача до **27.11, 23:59** (UTC+5), лучше до 22:00.

---

## Быстрый старт участника (15–20 минут)

### 0. Что понадобится

- Аккаунт GitHub и **принятое приглашение** в этот репозиторий: https://github.com/noncalan/QAIRU-larpers-/invitations
- Подписка **Claude Pro или Max** (бесплатный план Claude Code не включает).
- Google-аккаунт для бесплатного ключа Gemini.
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

### 5. Ключ Gemini (бесплатно)

1. Открой https://aistudio.google.com/ → **Get API key** → **Create API key**.
2. Создай свой `.env` из примера: `cp .env.example .env` (Windows: `copy .env.example .env`).
3. Вставь ключ после `GEMINI_API_KEY=`.

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
- **Gemini отвечает `429`:** упёрлись в лимит бесплатного тарифа, подожди минуту или смени модель на более лёгкую.
- **Windows:** работай в PowerShell или Git Bash. WSL тоже подходит.
- **Кончились лимиты Claude:** переключись на `/model sonnet` и запускай меньше субагентов одновременно (на Pro разумно 3–5).
