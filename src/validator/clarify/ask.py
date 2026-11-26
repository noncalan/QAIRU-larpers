"""``--ask``: вопросы автору в терминале и ``answers.yaml`` для повторной проверки (issue #42, демо).

После прогона 1 агент по одному показывает 3–5 главных вопросов (сначала блокирующие разработку, затем
тестирование), принимает номер варианта, букву или свой ответ текстом. Пустая строка пропускает вопрос,
конец ввода (Ctrl+D, закрытый stdin) заканчивает опрос. Ответы пишутся в ``answers.yaml`` рядом с отчётом
в формате ``questions.yaml``, и CLI сразу запускает прогон 2 с ``--answers``.

Ввод и вывод передаются функциями: так опрос проверяется тестом без терминала.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import yaml

from validator.clarify.answers import MAX_ANSWER_CHARS
from validator.models import Question, Report
from validator.questions.export import HEADER, YAML_WIDTH, questions_payload

MAX_ASKED = 5
MIN_ASKED = 3
ANSWERS_FILE = "answers.yaml"

Reader = Callable[[str], str | None]
"""Читает строку ответа с приглашением; None: ввод закончился."""
Writer = Callable[[str], None]


def pick_questions(report: Report) -> list[Question]:
    """3–5 главных вопросов: что агент советует спросить первым, а если таких меньше трёх, по порядку."""
    by_id = {q.id: q for q in report.questions}
    chosen = [by_id[i] for i in report.conclusion.ask_first if i in by_id][:MAX_ASKED]
    for q in report.questions:
        if len(chosen) >= MIN_ASKED:
            break
        if q not in chosen:
            chosen.append(q)
    return chosen


def _card(q: Question, n: int, total: int) -> str:
    who = ", ".join(a.label for a in q.addressee)
    lines = [
        "",
        f"Вопрос {n} из {total} · {q.id} · {q.priority.value} ({q.priority.label})"
        + (f" · {who}" if who else ""),
        q.question,
    ]
    lines += [f"  {i}. {o.label}. {o.text}" for i, o in enumerate(q.options, 1)]
    return "\n".join(lines)


def _parse(raw: str, q: Question) -> str | None:
    """Номер варианта → буква; буква варианта как есть; иначе свой ответ. Пусто → None (пропуск)."""
    text = raw.strip()
    if not text:
        return None
    if text.isdigit() and 1 <= int(text) <= len(q.options):
        return q.options[int(text) - 1].label
    return text[:MAX_ANSWER_CHARS]


def ask_questions(questions: Sequence[Question], read: Reader, write: Writer) -> dict[str, str]:
    """Опрос в терминале: ``Q-NN`` → ответ (буква варианта или текст). Пропущенных вопросов в словаре нет."""
    answers: dict[str, str] = {}
    write(
        f"Агент задаст {len(questions)} главных вопросов. Ответ: номер варианта, свой текст "
        "или пустая строка, чтобы пропустить."
    )
    for n, q in enumerate(questions, 1):
        write(_card(q, n, len(questions)))
        raw = read("Ответ: ")
        if raw is None:
            write("Ввод закончился: остальные вопросы пропущены.")
            break
        answer = _parse(raw, q)
        if answer is not None:
            answers[q.id] = answer
    return answers


def write_answers(path: Path, report: Report, answers: dict[str, str]) -> None:
    """``answers.yaml`` в формате ``questions.yaml``: все вопросы прогона, ответ только у отвеченных."""
    doc = report.document
    payload = questions_payload(
        list(report.questions),
        doc.name if doc else report.passport.document_name,
        doc.sha256 if doc else None,
    )
    payload["answered_by"] = "ответы в терминале (--ask)"
    for item in payload["questions"]:
        item["answer"] = answers.get(item["id"])
    body = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=YAML_WIDTH)
    path.write_text(HEADER + body, encoding="utf-8")


__all__ = ["ANSWERS_FILE", "MAX_ASKED", "ask_questions", "pick_questions", "write_answers"]
