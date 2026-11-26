# Быстрый старт участника команды (15–20 минут)

> Перенесено из корневого README 26.11 (#47): корневой README теперь для жюри. Здесь настройка компьютера участника: Claude Code со скиллами проекта, окружение для экспериментов, правила работы.

## 0. Что понадобится

- Аккаунт GitHub и **принятое приглашение** в этот репозиторий, пока он приватный: https://github.com/noncalan/QAIRU-larpers/invitations
- Подписка **Claude Pro или Max** (бесплатный план Claude Code не включает). Агент тоже вызывает модель через подписку (`claude -p`), ключ API не нужен.
- Git, uv (поставит Python сам).

## 1. Git

- **macOS:** `xcode-select --install` (или `brew install git`).
- **Windows:** [Git for Windows](https://git-scm.com/download/win). Он нужен и самому Claude Code.
- **Linux:** `sudo apt install git`.

## 2. Claude Code

| ОС | Команда |
|---|---|
| macOS, Linux, WSL | `curl -fsSL https://claude.ai/install.sh \| bash` |
| Windows PowerShell | `irm https://claude.ai/install.ps1 \| iex` |
| Windows (winget) | `winget install Anthropic.ClaudeCode` |
| macOS (Homebrew) | `brew install --cask claude-code` |

Проверка: `claude --version`, при проблемах `claude doctor`. Документация: https://code.claude.com/docs/en/setup

## 3. Клонировать репозиторий

```bash
git clone https://github.com/noncalan/QAIRU-larpers.git
cd QAIRU-larpers
```

Пока репозиторий приватный: если git просит пароль, войди через [GitHub CLI](https://cli.github.com/) командой `gh auth login` (или клонируй через GitHub Desktop).

## 4. uv и Python-окружение

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Перезапусти терминал, затем в папке репозитория:

```bash
uv sync                          # агент и инструменты разработки (pytest, ruff, mypy)
uv sync --group experiments      # плюс библиотеки для скриптов в experiments/
```

Проверка: `uv run validator data/input/requirements.md out/report.md --backend replay --replay tests/fixtures/e2e/md/run` за секунды собирает отчёт без модели.

## 5. Ключ Claude API (не нужен)

Оплаченного ключа у команды нет и к сдаче не будет (решения D-007–D-009 в [`docs/decisions.md`](../decisions.md)): модель вызываем через подписку. Если ключ всё же появится:

1. Создай свой `.env` из примера: `cp .env.example .env` (Windows: `copy .env.example .env`).
2. Вставь ключ после `ANTHROPIC_API_KEY=` и запускай агента с `--backend api`.

`.env` уже в `.gitignore`, а `.claude/settings.json` запрещает Claude его читать. **Никогда не коммить ключ и не вставляй его в чат, issue или код:** репозиторий станет публичным перед сдачей.

## 6. Запустить Claude Code в репозитории

```bash
claude
```

1. При первом запуске войди в аккаунт Claude в браузере.
2. На вопрос о доверии папке ответь **Yes**: без этого не включатся общие разрешения из `.claude/settings.json`.
3. Выбери модель и усилие:
   - `/model opus`: основная модель. Если лимиты подписки кончаются, `/model sonnet`.
   - `/effort high` для обычной работы, `/effort xhigh` для исследования и важных решений.
4. Проверь, что всё подхватилось: `/agents` покажет `researcher`, `fact-checker`, `idea-critic`; набери `/` и увидишь скиллы, в том числе `/research-sprint`.

Ставить плагины и скиллы отдельно **не нужно**: всё лежит в `.claude/` и подхватывается автоматически. Какие скиллы есть и зачем: [`skills.md`](skills.md).

## 7. Первый промпт

Открой своё issue на GitHub и напиши Claude, например:

```
Прочитай CLAUDE.md и issue #<номер> (gh issue view <номер>). Выполни его.
Результаты сдай через PR в ветке feat/<тема>.
```

Если `gh` не установлен, просто скопируй текст issue в чат.

## Как работаем

- **Задачи = issues.** Взял задачу, назначь себя. Шаблоны: исследование, задача, баг. План и эпик: [`docs/plan.md`](../plan.md), #13.
- **Ветка на задачу:** `research/…`, `feat/…`, `fix/…`, `docs/…`. В `main` напрямую не пушим.
- **PR** с `Closes #N`, коротким описанием и цифрой eval, если менялись промпты или логика агента ([`evals/README.md`](../../evals/README.md)).
- **Параллельная работа:** в отдельном терминале `claude -w <имя>` создаёт свою копию репозитория (git worktree) с отдельной веткой, и сессии не мешают друг другу. `.env` туда копируется автоматически (`.worktreeinclude`).
- **Решения** записываем в [`docs/decisions.md`](../decisions.md), архитектурные с альтернативами в [`docs/adr/`](../adr/README.md).

## Частые проблемы

- **`claude: command not found`:** перезапусти терминал, затем `claude doctor`.
- **git просит пароль или `403`:** прими приглашение в репозиторий и войди через `gh auth login`.
- **Агент пишет `LLM-001`:** `claude` не найден или не вошёл в аккаунт. Запусти `claude`, выполни `/login`, проверь `claude auth status`.
- **Агент пишет `LLM-002`:** кончился лимит подписки. Подожди сброса окна (время сброса в тексте диагностики) или пересобери отчёт replay.
- **Windows:** работай в PowerShell или Git Bash. WSL тоже подходит.
- **Кончились лимиты Claude:** переключись на `/model sonnet` и запускай меньше субагентов одновременно (на Pro разумно 3–5).
