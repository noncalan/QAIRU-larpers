"""Операторы мутаций M1–M10 и нейтральные N1–N3 (#50, исследование 20, раздел 3).

Каждый оператор возвращает все места, куда можно внести дефект; выбор места делает ``evals.mutate``.
Места ищутся по форме текста, слов проверяемого документа здесь нет (CLAUDE.md, правило 3).
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Sequence

from evals.mutation_text import (
    _LIST_LABEL,
    _ORDERED,
    NEUTRAL,
    PROSE,
    SECTION_DEPTH,
    Edit,
    Line,
    Span,
    parse,
    sentence_at,
    sentences,
)

# --- Общие шаблоны формы текста -----------------------------------------------------------------

NUM = r"\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?"
TIME_UNIT = (
    r"(?:миллисекунд\w*|мс|секунд\w*|сек\.?|минут\w*|мин\.?|час\w*|суток|сутки|дн(?:я|ей)|день|"
    r"недел\w+|месяц\w*|seconds?|minutes?|hours?|days?|ms)"
)
UNIT = (
    r"(?:₸|₽|\$|€|%|руб(?:\.|л\w*)?|тенге|KZT|RUB|USD|EUR|" + TIME_UNIT + r"|"
    r"[КМГТ]Б|[KMGT]B|бит\w*|bits?|байт\w*|bytes?|символ\w*|знак\w*|цифр\w*|RPS|TPS|rps|tps|запрос\w*)"
)
_NUM_UNIT = re.compile(rf"(?<![\w.,/])({NUM})\s?({UNIT})(?![\w])")
_WITHIN = re.compile(rf"\bв течение\s+(?:(?:{NUM})\s?)?{TIME_UNIT}(?![\w])", re.IGNORECASE)
_BOUND = re.compile(
    rf"\b(?P<prep>не более|не менее|не позднее|не ранее|не выше|не ниже|максимум|минимум|до|от|за)\s+"
    rf"(?:{NUM})\s?{UNIT}(?![\w])",
    re.IGNORECASE,
)
_RANGE = re.compile(
    rf"\bот\s+(?:{NUM})(?:\s?{UNIT})?\s+до\s+(?:{NUM})\s?{UNIT}(?P<incl>\s+включительно)?",
    re.IGNORECASE,
)
_INCLUSIVE = re.compile(r"\s+включительно\b|,?\s+(?:обе|оба|все)\s+(?:границы|предела|конца)\s+\w+", re.I)
_OBLIGATION = re.compile(r"\b(?:долж(?:ен|на|но|ны)|обязан\w*|shall|must)\b", re.IGNORECASE)
_SUBJ_MODAL = re.compile(
    r"^(?P<subj>[A-ZА-ЯЁ][\w-]*(?:\s+[\w-]+){0,2}?)\s+(?P<modal>долж(?:ен|на|но|ны)|обязан[аоы]?)\s+"
    r"(?P<rest>\S.*)$"
)
_ACTOR = re.compile(
    r"^(?P<subj>(?:Система|Сервис|Приложение|Сервер|Модуль|Платформа|Компонент|Шлюз|Бэкенд|Backend|"
    r"Пользователь|Клиент|Оператор|Администратор|Банк))\s+(?P<verb>[а-яё]+(?:ает|яет|еет|ёт|ует|ит))"
    r"(?P<more>\s+и\s+[а-яё]+(?:ает|яет|еет|ёт|ует|ит))?(?P<rest>\s[^→]*)$"
)
"""Предложение «Актор глагол …» без стрелок схемы: глагол 3-го лица ед. ч. можно сделать возвратным."""
_ERROR_BRANCH = re.compile(
    r"\b(?:при (?:ошибке|сбое|отказе|отклонении|неудаче|недоступности)|в случае (?:ошибки|отказа|сбоя|"
    r"неудачи)|если\b[^.]*\bне\b|on error|if\b[^.]*\bnot\b|обработк\w+ (?:ошиб|исключ)\w+|"
    r"HTTP\s+[45]\d\d)",
    re.IGNORECASE,
)
_STATUS_CELL = re.compile(r"^`?[45]\d\d`?(?:\s+[A-Za-z][\w -]*)?$")
"""Ячейка таблицы с кодом ответа 4xx/5xx: «404», «`500`», «401 Unauthorized» (не сумма «500 ₸»)."""

VAGUE_TIME = "своевременно"
VAGUE_UPPER = "в разумных пределах"
VAGUE_LOWER = "в достаточном объёме"
VAGUE_DEADLINE = "в кратчайшие сроки"
VAGUE_VALUE = "приемлемое значение"
DANGLING = (
    " Порядок обработки описан в разделе {n}.",
    " Исключения обрабатываются в соответствии с регламентом {code}.",
)
TECH = (
    " Реализация — с использованием Redis и PostgreSQL 16.",
    " Хранение реализуется на Apache Kafka и MongoDB 7.",
    " Решение строится на Spring Boot 3 с кешем в Redis.",
)
UNVERIFIABLE = (
    "Система должна быть удобной и надёжной.",
    "Интерфейс должен быть интуитивно понятным, а работа системы стабильной.",
)
PLACEHOLDER = "TBD"
IMPERSONAL = "Обеспечивается возможность "


# --- Операторы: все кандидаты места -----------------------------------------------------------

Candidates = Callable[[str, list[Line]], list[Edit]]


def _prose(lines: list[Line]) -> list[Line]:
    return [ln for ln in lines if ln.kind in PROSE]


_LOWER_PREPS = {"не менее", "не ниже", "минимум", "от"}
_DEADLINE_PREPS = {"не позднее", "за", "не ранее"}


def _vague(rx: re.Pattern[str], m: re.Match[str]) -> str:
    if rx is _WITHIN:
        return VAGUE_TIME
    if rx is _RANGE:
        return VAGUE_UPPER
    prep = m.group("prep").lower()
    if prep in _LOWER_PREPS:
        return VAGUE_LOWER
    return VAGUE_DEADLINE if prep in _DEADLINE_PREPS else VAGUE_UPPER


def _m1(text: str, lines: list[Line]) -> list[Edit]:
    out: list[Edit] = []
    for ln in _prose(lines):
        seg = text[ln.start : ln.end]
        taken: list[Span] = []
        for rx in (_WITHIN, _RANGE, _BOUND):
            for m in rx.finditer(seg):
                s, e = ln.start + m.start(), ln.start + m.end()
                if any(a < e and s < b for a, b in taken):
                    continue
                taken.append((s, e))
                out.append(Edit("M1", s, e, _vague(rx, m), ln.section, (ln.section,), note=m.group(0)))
    # запасной путь: голое число с единицей, в том числе в таблицах
    for ln in lines:
        if ln.kind in PROSE | {"table"}:
            for m in _NUM_UNIT.finditer(text[ln.start : ln.end]):
                s, e = ln.start + m.start(), ln.start + m.end()
                if not any(c.start < e and s < c.end for c in out):
                    out.append(Edit("M1", s, e, VAGUE_VALUE, ln.section, (ln.section,), tier=1))
    return out


def _m2(text: str, lines: list[Line]) -> list[Edit]:
    out: list[Edit] = []
    for ln in lines:
        if ln.kind not in PROSE | {"table"}:
            continue
        seg = text[ln.start : ln.end]
        for m in _INCLUSIVE.finditer(seg):
            s, e = ln.start + m.start(), ln.start + m.end()
            ctx = [sentence_at(text, ln, s)] if ln.kind in PROSE else [(ln.start, ln.end)]
            out.append(Edit("M2", s, e, "", ln.section, (ln.section,), ctx, "граница", match_new=False))
    for ln in _prose(lines):
        for m in _NUM_UNIT.finditer(text[ln.start : ln.end]):
            if re.fullmatch(TIME_UNIT, m.group(2)):
                continue
            s, e = ln.start + m.start(2), ln.start + m.end(2)
            while s > ln.start and text[s - 1] in "  ":
                s -= 1
            ctx = [sentence_at(text, ln, s)]
            out.append(Edit("M2", s, e, "", ln.section, (ln.section,), ctx, "единица", False, tier=1))
    return out


def _other_number(raw: str) -> str:
    digits = re.sub(r"\D", "", re.split(r"[.,]", raw, maxsplit=1)[0])
    value = int(digits) if digits else 1
    new = value * 2 if value > 1 else value + 4
    if re.search(r"[   ]", raw):
        return f"{new:,}".replace(",", " ")
    return str(new)


def _section_ends(lines: list[Line]) -> dict[str, Line]:
    """Последняя строка прозы каждого раздела: туда вставляем новый абзац."""
    ends: dict[str, Line] = {}
    for ln in lines:
        if ln.kind in PROSE and ln.section:
            ends[ln.section] = ln
    return ends


_PLURAL = (
    ("миллисекунд", "миллисекунд"),
    ("секунд", "секунд"),
    ("минут", "минут"),
    ("час", "часов"),
    ("сут", "суток"),
    ("д", "дней"),
    ("недел", "недель"),
    ("месяц", "месяцев"),
)
_BARE_WITHIN = re.compile(rf"\bв течение\s+(?P<unit>{TIME_UNIT})(?![\w])", re.IGNORECASE)


def _changed_value(sent: str) -> tuple[str, str] | None:
    """То же утверждение с другим числом: (новое предложение, исходное значение)."""
    if m := _NUM_UNIT.search(sent):
        return sent[: m.start(1)] + _other_number(m.group(1)) + sent[m.end(1) :], m.group(0)
    if m := _BARE_WITHIN.search(sent):  # «в течение минуты» = одна единица времени
        unit = m.group("unit").lower()
        plural = next((p for stem, p in _PLURAL if unit.startswith(stem)), None)
        if plural:
            return sent[: m.start("unit")] + f"5 {plural}" + sent[m.end("unit") :], m.group(0)
    return None


def _m3(text: str, lines: list[Line]) -> list[Edit]:
    out: list[Edit] = []
    ends = _section_ends(lines)
    for ln in _prose(lines):
        for a, b in sentences(text, ln):
            sent = text[a:b]
            found = _changed_value(sent) if len(sent) <= 300 else None
            if not found:
                continue
            changed, value = found
            for target, last in sorted(ends.items()):
                if target == ln.section:
                    continue
                pos = last.end
                out.append(
                    Edit(
                        "M3",
                        pos,
                        pos,
                        "\n\n" + changed.strip(),
                        target,
                        (target, ln.section),
                        [(a, b)],
                        f"исходное значение: {value}",
                    )
                )
    return out


def _m4(text: str, lines: list[Line]) -> list[Edit]:
    n = next(f"{k}.{k - 3}" for k in range(9, 99) if f"{k}.{k - 3}" not in text)
    code = next(f"РГ-{k:02d}" for k in range(17, 99) if f"РГ-{k:02d}" not in text)
    out: list[Edit] = []
    for ln in _prose(lines):
        if not ln.text.rstrip().endswith((".", "!", "?")):
            continue
        for tmpl in DANGLING:
            new = tmpl.format(n=n, code=code)
            out.append(Edit("M4", ln.end, ln.end, new, ln.section, (ln.section,)))
    return out


def _m5(text: str, lines: list[Line]) -> list[Edit]:
    out: list[Edit] = []
    for idx, ln in enumerate(lines):
        if (ln.kind == "table" and _has_status_cell(ln.text)) or (
            ln.kind == "para" and _STATUS_LINE.match(ln.text)
        ):
            out.append(_delete_line(text, lines, idx, tier=1))
        elif ln.kind in PROSE:
            sents = sentences(text, ln)
            for a, b in sents:
                if not _ERROR_BRANCH.search(text[a:b]):
                    continue
                if len(sents) == 1:
                    out.append(_delete_line(text, lines, idx, tier=0))
                    continue
                s, e = a, b
                if e < ln.end and text[e] == " ":
                    e += 1
                elif s > ln.start and text[s - 1] == " ":
                    s -= 1
                ctx = [(ln.start, s), (e, ln.end)]
                out.append(Edit("M5", s, e, "", ln.section, (ln.section,), ctx, "предложение", False))
    return out


_STATUS_LINE = re.compile(r"^\s*\*{0,2}[45]\d\d\b[.*\s]*[^\s.*]")
"""Строка-описание кода ответа: «**401.** Ответ в случае…» (голый заголовок кода не берём)."""


def _delete_line(text: str, lines: list[Line], idx: int, tier: int) -> Edit:
    ln = lines[idx]
    s, e = ln.start, min(ln.end + 1, len(text))
    ctx: list[Span] = []
    prev = next((lines[j] for j in range(idx - 1, -1, -1) if lines[j].kind != "blank"), None)
    nxt = next((lines[j] for j in range(idx + 1, len(lines)) if lines[j].kind != "blank"), None)
    for other in (prev, nxt):
        if other and other.section == ln.section and other.kind not in {"heading", "table_sep", "hr", "code"}:
            ctx.append((other.start, other.end))
    return Edit("M5", s, e, "", ln.section, (ln.section,), ctx, "строка", False, tier)


_MIN_ITEM_WORDS = 3


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def _strip_item(text: str) -> str:
    m = _LIST_LABEL.match(text)
    return text[m.end() :] if m else text


def _m6(text: str, lines: list[Line]) -> list[Edit]:
    out: list[Edit] = []
    for first, second in itertools.pairwise(lines):
        if first.kind != "list" or second.kind != "list" or first.section != second.section:
            continue
        if _indent(first.text) != _indent(second.text):  # вложенный пункт и пункт уровнем выше не склеиваем
            continue
        head = first.text.rstrip()
        head = head[:-1].rstrip() if head.endswith((".", ";")) else head
        tail = _strip_item(second.text).strip()
        words = (len(_strip_item(first.text).split()), len(tail.split()))
        if min(words) < _MIN_ITEM_WORDS or "`" in first.text + second.text:
            continue
        if not re.match(r"^[A-ZА-ЯЁ]{2}", tail):
            tail = tail[0].lower() + tail[1:]
        both = bool(_OBLIGATION.search(first.text) and _OBLIGATION.search(second.text))
        tier = 0 if both else 1
        new = f"{head} и/или {tail}"
        out.append(Edit("M6", first.start, second.end, new, first.section, (first.section,), tier=tier))
    return out


def _m7(text: str, lines: list[Line]) -> list[Edit]:
    functional = re.compile(r"(?<!не)функциональн|functional", re.IGNORECASE)
    out: list[Edit] = []
    for ln in _prose(lines):
        if not ln.text.rstrip().endswith("."):
            continue
        tier = 0 if ln.kind == "list" and functional.search(ln.section) else (1 if ln.kind == "list" else 2)
        out.extend(Edit("M7", ln.end, ln.end, t, ln.section, (ln.section,), tier=tier) for t in TECH)
    return out


def _m8(text: str, lines: list[Line]) -> list[Edit]:
    nfr = re.compile(r"нефункциональн|non-functional|NFR|качеств", re.IGNORECASE)
    fr = re.compile(r"функциональн|functional|требовани", re.IGNORECASE)
    out: list[Edit] = []
    for section, last in sorted(_section_ends(lines).items()):
        tier = 0 if nfr.search(section) else (1 if fr.search(section) else 2)
        out.extend(
            Edit("M8", last.end, last.end, "\n\n" + s, section, (section,), tier=tier) for s in UNVERIFIABLE
        )
    return out


def _m9(text: str, lines: list[Line]) -> list[Edit]:
    out: list[Edit] = []
    for ln in _prose(lines):
        for a, b in sentences(text, ln):
            sent = text[a:b]
            if (m := _SUBJ_MODAL.match(sent)) and not m.group("rest").lower().startswith(("быть ", "be ")):
                new, tier = IMPERSONAL + m.group("rest"), 0
            elif m := _ACTOR.match(sent):
                new, tier = _passive(m), 1
            else:
                continue
            out.append(Edit("M9", a, b, new, ln.section, (ln.section,), note=m.group("subj"), tier=tier))
    return out


def _passive(m: re.Match[str]) -> str:
    """«Сервис передаёт X» → «Передаётся X»: субъект убран, глагол возвратный, исполнителя нет."""
    verb = m.group("verb") + "ся"
    more = re.sub(r"(\S+)$", r"\1ся", m.group("more")) if m.group("more") else ""
    return verb[0].upper() + verb[1:] + more + m.group("rest")


def _has_status_cell(line: str) -> bool:
    return any(_STATUS_CELL.match(line[a:b]) for a, b in _cells(line))


def _cells(line: str) -> list[tuple[int, int]]:
    """Ячейки строки таблицы: смещения содержимого без пробелов по краям."""
    out = []
    bars = [i for i, ch in enumerate(line) if ch == "|"]
    for a, b in itertools.pairwise(bars):
        cell = line[a + 1 : b]
        lead = len(cell) - len(cell.lstrip())
        trail = len(cell.rstrip())
        out.append((a + 1 + lead, a + 1 + max(trail, lead)))
    return out


_EMPTY_CELL = {"", "—", "-", "–", PLACEHOLDER}


def _m10(text: str, lines: list[Line]) -> list[Edit]:
    out: list[Edit] = []
    for ln in lines:
        if ln.kind != "table":
            continue
        for k, (a, b) in enumerate(_cells(ln.text)):
            value = ln.text[a:b]
            if k == 0 or value in _EMPTY_CELL:
                continue
            tier = 0 if re.search(r"\d", value) else 1
            s, e = ln.start + a, ln.start + b
            out.append(Edit("M10", s, e, PLACEHOLDER, ln.section, (ln.section,), note=value, tier=tier))
    for ln in _prose(lines):
        for m in _NUM_UNIT.finditer(text[ln.start : ln.end]):
            s, e = ln.start + m.start(), ln.start + m.end()
            out.append(Edit("M10", s, e, PLACEHOLDER, ln.section, (ln.section,), note=m.group(0), tier=2))
    return out


CANDIDATES: dict[str, Candidates] = {
    "M1": _m1,
    "M2": _m2,
    "M3": _m3,
    "M4": _m4,
    "M5": _m5,
    "M6": _m6,
    "M7": _m7,
    "M8": _m8,
    "M9": _m9,
    "M10": _m10,
}


def candidates(operator: str, text: str, depth: int = SECTION_DEPTH) -> list[Edit]:
    """Все места, куда оператор может внести дефект. Пустой список значит «места нет»."""
    return CANDIDATES[operator](text, parse(text, depth))


# --- Нейтральные мутации -------------------------------------------------------------------------


def neutral_swap_sections(text: str) -> tuple[str, str] | None:
    """N1: поменять местами два соседних раздела одного уровня (уровень 3, иначе 2)."""
    lines = parse(text)
    for level in (3, 2):
        heads = [i for i, ln in enumerate(lines) if ln.kind == "heading" and ln.heading_level == level]
        for a, b in itertools.pairwise(heads):
            end_b = next(
                (
                    j
                    for j in range(b + 1, len(lines))
                    if lines[j].kind == "heading" and lines[j].heading_level <= level
                ),
                len(lines),
            )
            between = [ln for ln in lines[a + 1 : b] if ln.kind == "heading" and ln.heading_level < level]
            if between or end_b - b < 2 or b - a < 2:
                continue
            if any(ln.kind == "hr" for ln in lines[a:end_b]):
                continue
            s1, s2 = lines[a].start, lines[b].start
            e2 = lines[end_b].start if end_b < len(lines) else len(text)
            first, second = text[s1:s2], text[s2:e2]
            if not second.endswith("\n"):
                second += "\n"
                first = first.rstrip("\n")
            return text[:s1] + second + first + text[e2:], f"{lines[a].text} ⇄ {lines[b].text}"
    return None


def neutral_renumber(text: str) -> tuple[str, str] | None:
    """N2: нумерованные списки 1, 2, 3 → 10, 20, 30 (шаг нумерации, без пропусков)."""
    count = 0

    def repl(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}{int(m.group(2)) * 10}{m.group(3)}{m.group(4)}"

    out = []
    for ln in parse(text):
        out.append(_ORDERED.sub(repl, ln.text, count=1) if ln.kind == "list" else ln.text)
    return ("\n".join(out), f"пунктов: {count}") if count else None


def neutral_table_to_list(text: str) -> tuple[str, str] | None:
    """N3: первая таблица из двух столбцов «ключ | значение» становится списком «ключ: значение»."""
    lines = parse(text)
    for i, ln in enumerate(lines):
        if ln.kind != "table_head" or len(_cells(ln.text)) != 2:
            continue
        j = i + 1
        if j >= len(lines) or lines[j].kind != "table_sep":
            continue
        rows = []
        k = j + 1
        while k < len(lines) and lines[k].kind == "table":
            a, b = _cells(lines[k].text)
            rows.append(f"* {lines[k].text[a[0] : a[1]]}: {lines[k].text[b[0] : b[1]]}")
            k += 1
        if not rows:
            continue
        start, end = ln.start, lines[k - 1].end
        return text[:start] + "\n".join(rows) + text[end:], f"строк: {len(rows)}"
    return None


NEUTRAL_OPS: dict[str, Callable[[str], tuple[str, str] | None]] = {
    "N1": neutral_swap_sections,
    "N2": neutral_renumber,
    "N3": neutral_table_to_list,
}


def neutralize(text: str, ops: Sequence[str]) -> tuple[str, list[dict[str, str]]]:
    log = []
    for op in ops:
        res = NEUTRAL_OPS[op](text)
        if res is None:
            log.append({"operator": op, "name": NEUTRAL[op], "result": "места нет"})
            continue
        text, what = res
        log.append({"operator": op, "name": NEUTRAL[op], "result": what})
    return text, log
