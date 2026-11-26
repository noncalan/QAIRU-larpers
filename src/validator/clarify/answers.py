"""Чтение ``answers.yaml``: ответы автора ТЗ на вопросы агента (issue #42).

Формат тот же, что у ``questions.yaml`` рядом с отчётом (схема в ``questions/export.py``): автор вписывает
в поле ``answer`` букву варианта (A, B, …), свой ответ текстом или «пропустить», пояснение в ``comment``.
Агент сам пишет такой файл в режиме ``--ask``.

Файл приходит от человека, поэтому это недоверенные данные:

- файл не читается, не YAML, не та схема, нет ``id`` у вопроса, повтор ``id``, ответ не строка, слишком
  длинный ответ → ``AnswersError`` с понятным текстом по-русски (CLI: код 2, отчёт не пишется);
- ответ буквой, которой нет среди вариантов, или файл к другой версии документа → предупреждение в отчёте
  (``CFG-021``), прогон идёт дальше; такой ответ считается отсутствующим;
- текст ответа агент не выполняет: он идёт в сверку с находками как данные между метками.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from validator.errors import CFG_ANSWERS_INVALID, ConfigError
from validator.models import AnswerKind, AuthorAnswer

SCHEMA_VERSION = 1
MAX_FILE_BYTES = 1_000_000
"""Файл ответов больше мегабайта это не ответы на 5–15 вопросов: читать не будем."""
MAX_ANSWER_CHARS = 2000
"""Свой ответ длиннее ~страницы это уже правка документа, а не ответ на вопрос."""
MAX_QUESTIONS = 200

SKIP_WORDS = frozenset({"skip", "пропустить", "пропуск", "отложить", "-", "—"})
"""Ответ «пропустить»: автор видел вопрос и отложил его, вопрос остаётся открытым."""

_SKIP_PUNCT = " .,;:!?«»\"'"
_TAIL = r"(?:\s+\S+){0,5}"
"""До пяти слов после «не знаю» или «уточню»: «не знаю, спрошу у бизнеса», «уточню у юристов к пятнице»."""
_DEFER_PHRASES = (
    r"(?:я\s+)?не\s*знаю|не\s*знаем|не\s+уверен[аы]?|затрудняюсь(?:\s+ответить)?"
    r"|нет\s+ответа|ответа\s+(?:пока\s+)?нет|не\s+решено|не\s+решили"
    r"|(?:решим|ответим|отвечу|скажу|скажем)\s+(?:позже|потом)"
    r"|уточню|уточним|уточняем|уточняю|уточняется|уточнят|(?:нужно|надо)\s+уточнить"
    r"|tbd|tba|todo|unknown|idk|(?:i\s+)?don\s?t\s+know|not\s+sure"
)
_DEFER_ALONE = r"позже|потом|later|n\s*a"
"""Только как весь ответ: «потом отправляем письмо» это уже правило, а не отсрочка."""
_DEFERRED = re.compile(rf"(?:пока\s+)?(?:(?:{_DEFER_PHRASES}){_TAIL}|{_DEFER_ALONE})")
"""Ответ-отсрочка: автор ответа не знает. Вопрос остаётся открытым, как при «пропустить» (#142).

Ошибиться в эту сторону безопасно: открытый вопрос не даёт ложного зелёного светофора, а текст автора виден
в отчёте рядом с пометкой «отложен». Наоборот, «не знаю» как ответ закрыл бы вопрос и мог сделать светофор
зелёным: «можно начинать разработку».
"""
_QUESTION_ID = re.compile(r"Q\s*-?\s*0*(\d+)", re.IGNORECASE)
_OPTION_PREFIX = r"(?:(?:вариант|variant|option)\s*[:№#-]?\s*)?"
_OPTION_LETTER = re.compile(rf"{_OPTION_PREFIX}\(?\s*([A-Za-zА-Яа-яЁё])\s*[.):]?", re.IGNORECASE)
"""«B», «b.», «B)», «(B)», «вариант B», «Вариант: B»."""
_OPTION_NUMBER = re.compile(r"(?:вариант|variant|option)\s*[:№#-]?\s*\(?\s*([1-9])\s*[.):]?", re.IGNORECASE)
"""«вариант 2» → второй вариант (B). Голое «2» остаётся своим ответом: это значение («2 попытки»)."""
_LOOKALIKES = str.maketrans("АВСЕавсе", "ABCEABCE")
"""Кириллические буквы, неотличимые от латинских: автор мог набрать вариант «А» в русской раскладке."""


class _StringLoader(yaml.SafeLoader):
    """YAML, где все скаляры строки (кроме ``null``).

    Обычный YAML 1.1 читает ответ ``10:30`` как число 630, ``yes`` как True, ``2026-12-01`` как дату.
    Автор пишет ответы как текст, поэтому неявные типы, кроме ``null``, отключены.
    """


_StringLoader.yaml_implicit_resolvers = {
    first: [(tag, rx) for tag, rx in resolvers if tag == "tag:yaml.org,2002:null"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class AnswersError(ConfigError):
    """Файл ответов нельзя использовать: CLI завершается с кодом 2 и этим текстом."""

    default_code = CFG_ANSWERS_INVALID


@dataclass(frozen=True)
class AnswersFile:
    """Разобранный ``answers.yaml``."""

    path: Path
    sha256: str
    document_sha256: str | None
    answered_by: str | None
    answers: list[RawAnswer] = field(default_factory=list)

    @property
    def filled(self) -> list[RawAnswer]:
        """Вопросы, у которых в файле что-то вписано в ``answer`` (вариант, текст или «пропустить»)."""
        return [a for a in self.answers if not a.empty]


def normalize_question_id(raw: object) -> str | None:
    """``Q-01``, ``q1``, ``Q 1`` → ``Q-01``; не ID вопроса → None."""
    if not isinstance(raw, str):
        return None
    m = _QUESTION_ID.fullmatch(raw.strip())
    return f"Q-{int(m.group(1)):02d}" if m else None


def _fail(path: Path, problem: str, hint: str = "") -> AnswersError:
    return AnswersError(
        f"Файл ответов {path.name}: {problem}",
        hint=hint
        or "Исправьте файл по образцу questions.yaml рядом с отчётом (поле answer у каждого вопроса)",
        location=str(path),
    )


def _read(path: Path) -> tuple[bytes, Any]:
    if not path.is_file():
        raise _fail(
            path, "файл не найден", "Укажите путь к answers.yaml, например out/questions.yaml с ответами"
        )
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise _fail(path, "файл не читается", "Проверьте права доступа к файлу") from exc
    if len(data) > MAX_FILE_BYTES:
        raise _fail(path, f"слишком большой ({len(data)} байт, допустимо до {MAX_FILE_BYTES})")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise _fail(path, "файл не в кодировке UTF-8", "Сохраните файл в UTF-8") from exc
    try:
        return data, yaml.load(text, Loader=_StringLoader)  # noqa: S506 (наследник SafeLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (строка {mark.line + 1})" if mark is not None else ""
        raise _fail(
            path,
            f"это не YAML{where}",
            "Проверьте отступы и кавычки; ответ с двоеточием возьмите в кавычки",
        ) from exc


def _text(value: object, path: Path, what: str) -> str:
    """Строка из YAML: число тоже годится (ответ «30»), остальное — ошибка схемы."""
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise _fail(path, f"{what}: ожидается текст, а не {type(value).__name__}")
    return str(value).strip()


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = item.get("quote") if isinstance(item, Mapping) else item
        if isinstance(text, str | int) and not isinstance(text, bool) and str(text).strip():
            out.append(str(text).strip())
    return out


def _options(raw: object) -> dict[str, tuple[str, str]]:
    """Буква → (правило, что поменять). Кривые варианты пропускаем: ответ своим текстом всё равно читается."""
    options: dict[str, tuple[str, str]] = {}
    if not isinstance(raw, list):
        return options
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "").strip().upper()
        if label:
            options[label] = (str(item.get("text") or "").strip(), str(item.get("change") or "").strip())
    return options


def _option_letter(answer: str) -> str | None:
    text = answer.strip()
    m = _OPTION_LETTER.fullmatch(text)
    if m:
        return m.group(1).translate(_LOOKALIKES).upper()
    n = _OPTION_NUMBER.fullmatch(text)
    return chr(ord("A") + int(n.group(1)) - 1) if n else None


def _plain_words(answer: str) -> str:
    """Ответ без знаков препинания и регистра: «Не знаю...» → «не знаю», «?» → пустая строка."""
    return " ".join(re.sub(r"[^\w]+", " ", answer.lower().replace("ё", "е")).split())


def is_deferred(answer: str) -> bool:
    """Ответ откладывает вопрос: «пропустить», «не знаю», «?», «TBD», «уточню позже» и т. п."""
    if answer.lower().strip(_SKIP_PUNCT) in SKIP_WORDS:
        return True
    words = _plain_words(answer)
    return not words or _DEFERRED.fullmatch(words) is not None


@dataclass(frozen=True)
class RawAnswer:
    """Ответ из файла до сопоставления с вопросами прогона: буква варианта ещё не разобрана."""

    question_id: str
    question: str
    answer: str
    comment: str
    where: list[str]
    related_findings: list[str]
    options: dict[str, tuple[str, str]]

    @property
    def empty(self) -> bool:
        return not self.answer


def _raw(entry: Mapping[str, Any], qid: str, path: Path) -> RawAnswer:
    answer = _text(entry.get("answer"), path, f"ответ {qid}")
    comment = _text(entry.get("comment"), path, f"пояснение {qid}")
    if len(answer) > MAX_ANSWER_CHARS or len(comment) > MAX_ANSWER_CHARS:
        raise _fail(path, f"ответ или пояснение {qid} длиннее {MAX_ANSWER_CHARS} символов")
    return RawAnswer(
        question_id=qid,
        question=_text(entry.get("question"), path, f"вопрос {qid}"),
        answer=answer,
        comment=comment,
        where=_strings(entry.get("where")),
        related_findings=[s for s in _strings(entry.get("related_findings")) if s.startswith("F-")],
        options=_options(entry.get("options")),
    )


def _deferral_note(raw: RawAnswer) -> str:
    """Пояснение к отложенному ответу: слова автора («не знаю, спрошу у бизнеса») не теряются в отчёте.

    Голое «пропустить» и «?» ничего не добавляют, поэтому пояснение остаётся как в файле.
    """
    bare = raw.answer.lower().strip(_SKIP_PUNCT) in SKIP_WORDS or not _plain_words(raw.answer)
    if bare:
        return raw.comment
    return f"{raw.answer}; {raw.comment}" if raw.comment else raw.answer


def resolve_answer(
    raw: RawAnswer, options: Mapping[str, tuple[str, str]] | None = None
) -> tuple[AuthorAnswer, str | None]:
    """Ответ автора и предупреждение, если буквы варианта нет среди вариантов.

    ``options``: варианты вопроса этого прогона, если в файле их нет (автор написал только ``id`` и
    ``answer``). Буква без такого варианта считается отсутствием ответа, а не своим текстом «B».
    """
    base: dict[str, Any] = {
        "question_id": raw.question_id,
        "question": raw.question,
        "comment": raw.comment,
        "where": list(raw.where),
        "related_findings": list(raw.related_findings),
    }
    if raw.empty:
        return AuthorAnswer(kind=AnswerKind.NONE, **base), None
    if is_deferred(raw.answer):
        return AuthorAnswer(kind=AnswerKind.SKIPPED, **(base | {"comment": _deferral_note(raw)})), None
    letter = _option_letter(raw.answer)
    if letter is None:
        return AuthorAnswer(kind=AnswerKind.TEXT, text=raw.answer, **base), None
    known = raw.options or dict(options or {})
    if letter not in known:
        listed = ", ".join(known) or "у вопроса нет вариантов"
        warning = f"{raw.question_id}: варианта «{raw.answer}» нет ({listed}), ответ не учтён"
        return AuthorAnswer(kind=AnswerKind.NONE, **base), warning
    rule, change = known[letter]
    return AuthorAnswer(kind=AnswerKind.OPTION, option=letter, text=rule, change=change, **base), None


def _entries(payload: object, path: Path) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        raise _fail(path, "в начале файла ожидаются поля schema_version, document_sha256 и questions")
    version = payload.get("schema_version", SCHEMA_VERSION)
    if str(version).strip() != str(SCHEMA_VERSION):
        raise _fail(path, f"версия схемы {version} не поддерживается, ожидается {SCHEMA_VERSION}")
    entries = payload.get("questions")
    if not isinstance(entries, list):
        raise _fail(path, "нет списка questions с вопросами и ответами")
    if len(entries) > MAX_QUESTIONS:
        raise _fail(path, f"слишком много вопросов ({len(entries)}, допустимо до {MAX_QUESTIONS})")
    for n, entry in enumerate(entries, 1):
        if not isinstance(entry, Mapping):
            raise _fail(path, f"вопрос №{n} в списке questions записан не как набор полей (id, answer, …)")
    return entries


def load_answers(path: Path) -> AnswersFile:
    """Разобранный файл ответов или ``AnswersError`` с понятным текстом."""
    data, payload = _read(path)
    answers: list[RawAnswer] = []
    seen: set[str] = set()
    for n, entry in enumerate(_entries(payload, path), 1):
        qid = normalize_question_id(entry.get("id"))
        if qid is None:
            raise _fail(
                path, f"у вопроса №{n} нет id вида Q-01", "Не меняйте поле id: по нему ответ находит вопрос"
            )
        if qid in seen:
            raise _fail(path, f"вопрос {qid} записан дважды", "Оставьте один ответ на каждый вопрос")
        seen.add(qid)
        answers.append(_raw(entry, qid, path))
    header = payload if isinstance(payload, Mapping) else {}
    doc_sha = header.get("document_sha256")
    by = header.get("answered_by")
    return AnswersFile(
        path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        document_sha256=doc_sha.strip().lower() if isinstance(doc_sha, str) and doc_sha.strip() else None,
        answered_by=str(by).strip() if isinstance(by, str | int) and str(by).strip() else None,
        answers=answers,
    )


__all__ = [
    "AnswersError",
    "AnswersFile",
    "RawAnswer",
    "is_deferred",
    "load_answers",
    "normalize_question_id",
    "resolve_answer",
]
