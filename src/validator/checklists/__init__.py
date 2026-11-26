"""Доменные чек-листы с триггерами (issue #35): выбор по документу и раздел для промпта прохода поиска.

Чек-лист (``*.yaml`` в этой папке) описывает типовые пробелы одной предметной области: деньги, даты и
периоды, API, персональные данные, статусы, уведомления. У чек-листа есть триггеры: шаблоны общих слов
этой области. Чек-лист включается, только если его триггеры встретились хотя бы в ``min_blocks`` блоках
документа: так он не шумит на документах без своей сущности. Включённые чек-листы добавляются в промпт
прохода отдельным разделом после документа (``prompts/search_checklists.md``), а их список идёт в паспорт
и в отчёт вместе с выключенными («триггеры не встретились»).

Чек-листы общие (CLAUDE.md, правила 2 и 3): ни слов проверяемого документа, ни пунктов эталона.
``tests/test_prompt_leak.py`` проверяет эту папку.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from validator.errors import InternalError
from validator.models import ChecklistUse, ParsedDocument, Property
from validator.prompting import fill, load_prompt

CHECKLISTS_DIR = Path(__file__).resolve().parent
SECTION_PROMPT = "search_checklists.md"
"""Шаблон раздела с чек-листами в промпте прохода (``prompts/``): слоты ``{titles}`` и ``{checklists}``."""
TRIGGER_EXAMPLES = 5
"""Сколько разных сработавших слов показать в отчёте как причину включения."""
MAX_WORD = 40
"""Длиннее слово сработавшего триггера в отчёте не показываем."""
ITEM_ID = re.compile(r"\b([A-Z]{2,4})-(\d{1,2})\b")
"""ID пункта в строке «Чек-лист:» ответа модели: «MON-02», «API-7» (номер дополняется нулём)."""
_LIST_ID = re.compile(r"^[A-Z]{2,4}$")
_ITEM_ID_FULL = re.compile(r"^[A-Z]{2,4}-\d{2}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChecklistItem(_Strict):
    """Пункт чек-листа: вопрос к документу, свойство стандарта и почему пробел мешает тесту."""

    id: str
    property: Property
    question: str = Field(min_length=10)
    why: str = Field(min_length=10)

    @field_validator("id")
    @classmethod
    def _id_format(cls, value: str) -> str:
        if not _ITEM_ID_FULL.match(value):
            raise ValueError(f"ID пункта «{value}» не в формате XXX-NN")
        return value


class Triggers(_Strict):
    min_blocks: int = Field(default=2, ge=1, description="Сколько блоков документа должны дать совпадение")
    patterns: list[str] = Field(min_length=1)

    @field_validator("patterns")
    @classmethod
    def _compiles(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"триггер «{pattern}» не компилируется: {exc}") from exc
        return value


class Checklist(_Strict):
    id: str
    title: str
    triggers: Triggers
    items: list[ChecklistItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids(self) -> Checklist:
        if not _LIST_ID.match(self.id):
            raise ValueError(f"ID чек-листа «{self.id}» не в формате XXX")
        ids = [i.id for i in self.items]
        wrong = [i for i in ids if not i.startswith(f"{self.id}-")]
        if wrong:
            raise ValueError(f"пункты {wrong} не начинаются с «{self.id}-»")
        if len(set(ids)) != len(ids):
            raise ValueError(f"повторяются ID пунктов в чек-листе {self.id}")
        return self

    @functools.cached_property
    def regex(self) -> re.Pattern[str]:
        return re.compile("|".join(f"(?:{p})" for p in self.triggers.patterns), re.IGNORECASE)


def _load_file(path: Path) -> Checklist:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return Checklist.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise InternalError(
            f"Чек-лист {path.name} не читается или не по формату",
            hint="Переустановите агент или исправьте файл по src/validator/checklists/README.md",
        ) from exc


@functools.cache
def _load_dir(root: Path) -> tuple[Checklist, ...]:
    lists = tuple(_load_file(p) for p in sorted(root.glob("*.yaml"), key=lambda p: p.name))
    ids = [c.id for c in lists]
    if len(set(ids)) != len(ids):
        raise InternalError("ID чек-листов повторяются", hint="Исправьте src/validator/checklists/*.yaml")
    return lists


def load_checklists(root: Path = CHECKLISTS_DIR) -> tuple[Checklist, ...]:
    """Все чек-листы папки в порядке имён файлов. Битый файл это ошибка установки (``InternalError``)."""
    return _load_dir(root.resolve())


# ---------------------------------------------------------------- выбор по документу


@dataclass(frozen=True)
class Selection:
    """Итог выбора: какие чек-листы включены и почему (для промпта, паспорта и отчёта)."""

    uses: tuple[ChecklistUse, ...]
    active: tuple[Checklist, ...]

    @property
    def item_ids(self) -> frozenset[str]:
        return frozenset(i.id for c in self.active for i in c.items)


def _block_texts(doc: ParsedDocument) -> Iterable[str]:
    for block in doc.blocks:
        yield block.serialized or block.text


def _word(text: str, match: re.Match[str]) -> str:
    """Сработавшее слово целиком: триггер это основа («уведомл»), человеку понятнее «уведомления»."""
    end = match.end()
    while end < len(text) and (text[end].isalnum() or text[end] == "-") and end - match.start() < MAX_WORD:
        end += 1
    return text[match.start() : end].strip(" -")


def _use(checklist: Checklist, texts: Sequence[str], enabled: bool) -> ChecklistUse:
    blocks = 0
    words: dict[str, None] = {}
    for text in texts:
        found = [_word(text, m) for m in checklist.regex.finditer(text)]
        if found:
            blocks += 1
            words.update(dict.fromkeys(w.strip().lower() for w in found))
    triggered = blocks >= checklist.triggers.min_blocks
    return ChecklistUse(
        id=checklist.id,
        title=checklist.title,
        items=len(checklist.items),
        min_blocks=checklist.triggers.min_blocks,
        matched_blocks=blocks,
        triggers=list(words)[:TRIGGER_EXAMPLES],
        triggered=triggered,
        used=enabled and triggered,
    )


def select_checklists(
    doc: ParsedDocument, *, enabled: bool = True, checklists: Sequence[Checklist] | None = None
) -> Selection:
    """Какие чек-листы включить для документа: триггеры встретились хотя бы в ``min_blocks`` блоках.

    ``enabled=False`` (``--no-checklists``) считает триггеры для отчёта, но ни один чек-лист не идёт в
    промпт. Совпадения ищутся по тексту блоков (строки таблиц в виде «Колонка: значение»), чтобы число
    блоков было понятным человеку: «слова области встретились в 7 блоках».
    """
    lists = tuple(checklists) if checklists is not None else load_checklists()
    texts = list(_block_texts(doc))
    uses = tuple(_use(c, texts, enabled) for c in lists)
    active = tuple(c for c, u in zip(lists, uses, strict=True) if u.used)
    return Selection(uses=uses, active=active)


# ---------------------------------------------------------------- раздел промпта


def _render_item(item: ChecklistItem) -> str:
    prop = item.property
    question = " ".join(item.question.split())
    why = " ".join(item.why.split())
    return f"- {item.id} [{prop.info.english} ({prop.label.lower()})]. {question} Почему мешает: {why}"


def render_section(active: Sequence[Checklist]) -> str:
    """Раздел промпта прохода с включёнными чек-листами; пустая строка, если включённых нет."""
    if not active:
        return ""
    blocks = ["\n".join([f"### {c.title} ({c.id})", *(_render_item(i) for i in c.items)]) for c in active]
    titles = ", ".join(f"«{c.title}»" for c in active)
    return fill(load_prompt(SECTION_PROMPT), titles=titles, checklists="\n\n".join(blocks))


def item_id(value: str, known: frozenset[str]) -> str | None:
    """ID пункта из строки «Чек-лист:» ответа модели, если такой пункт был в промпте; иначе None."""
    for m in ITEM_ID.finditer(value.upper()):
        candidate = f"{m.group(1)}-{int(m.group(2)):02d}"
        if candidate in known:
            return candidate
    return None


__all__ = [
    "CHECKLISTS_DIR",
    "Checklist",
    "ChecklistItem",
    "Selection",
    "item_id",
    "load_checklists",
    "render_section",
    "select_checklists",
]
