# Прототипы из исследования 03

- `quote_check.py`: верификатор цитат (нормализация с картой смещений, точный поиск, затем rapidfuzz). Запуск: `uv run python experiments/03-proto/quote_check.py data/input/requirements.md`.
- `z3_conflicts.py`: иллюстрация «доказательства» противоречий через Z3 на маленьких моделях. Нужен пакет `z3-solver` (в группу experiments не входит: `uv run --with z3-solver python experiments/03-proto/z3_conflicts.py`).

Подробности и выводы: `docs/research/03-contradictions-and-grounding.md`. Это не код агента.
