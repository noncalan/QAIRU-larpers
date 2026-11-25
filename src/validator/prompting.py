"""Тексты промптов шагов из ``prompts/`` и подстановка данных в шаблон.

Промпт шага лежит файлом ``prompts/<шаг>_system.md`` и ``prompts/<шаг>_user.md`` (хеш папки идёт в
паспорт отчёта). Пользовательский шаблон содержит слоты вида ``{document}``; подставляем их за один
проход, поэтому фигурные скобки в тексте документа остаются как есть.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

from validator.errors import InternalError

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@functools.cache
def load_prompt(name: str) -> str:
    """Текст промпта ``prompts/<name>``. Файла нет: агент собран без промптов, это ошибка установки."""
    try:
        return (PROMPTS_DIR / name).read_text(encoding="utf-8")
    except OSError as exc:
        raise InternalError(
            f"Нет файла промпта {name}",
            hint="Переустановите агент: промпты входят в пакет (src/validator/prompts)",
        ) from exc


def fill(template: str, **values: str) -> str:
    """Подставляет ``{имя}`` из ``values`` за один проход: подставленный текст повторно не разбирается."""
    if not values:
        return template
    slot = re.compile(r"\{(" + "|".join(map(re.escape, values)) + r")\}")
    return slot.sub(lambda m: values[m.group(1)], template)


__all__ = ["PROMPTS_DIR", "fill", "load_prompt"]
