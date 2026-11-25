"""Эксперимент 21: какая модель Claude и какой effort для проходов поиска дефектов.

Каждый прогон = один «проход поиска» будущего агента: весь документ, промпт базовой линии
(исследование 11) и строгий JSON по схеме. Модель и effort меняются, остальное одинаково.
Затем отдельный судья (Opus 5.5, effort high) вслепую сопоставляет находки с эталоном.
Эталон попадает только в запрос судьи и никогда в запрос проверяющей модели (правило 2).

Запуск из корня репозитория:
    python3 experiments/21-model-effort/run_matrix.py            # прогоны + судья
    uv run --with rapidfuzz python experiments/21-model-effort/score.py   # сводка

Сырые ответы: experiments/out/21/ (в git не попадают). Готовый ответ повторно не запрашивается.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "experiments" / "out" / "21"
DOCUMENT = ROOT / "data" / "input" / "requirements.md"
GOLD = ROOT / "docs" / "gold" / "defects.md"

# Промпт базовой линии (исследование 11) без изменений; формат ответа задаёт схема.
SYSTEM_PROMPT = (
    "Ты аналитик требований к программному обеспечению. Отвечай на русском языке."
)
PROMPT = (
    "Проверь документ требований по ISO/IEC/IEEE 29148:2018 (п. 5.2.5 и 5.2.6) и IEEE 830-1998 "
    "(п. 4.3). Выпиши все дефекты. Для каждого: точная цитата из документа, какое свойство "
    "нарушено, почему, насколько критично."
)

FINDINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote": {
                        "type": "string",
                        "description": "Точная цитата из документа, дословно",
                    },
                    "property": {
                        "type": "string",
                        "description": "Нарушенное свойство",
                    },
                    "explanation": {
                        "type": "string",
                        "description": "Почему это дефект",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["критично", "средне", "низко"],
                    },
                },
                "required": ["quote", "property", "explanation", "severity"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["findings"],
    "additionalProperties": False,
}

JUDGE_SYSTEM = "Ты строгий и беспристрастный оценщик качества ревью требований. Отвечай на русском языке."
JUDGE_PROMPT = """Ниже три блока: (1) документ требований, (2) эталонный список дефектов (номер в столбце «#»), (3) находки проверяющего (номер в поле id).

Задача 1. Для КАЖДОГО эталонного дефекта (их 45) реши:
- found: есть находка, которая указывает на то же место документа и ту же суть проблемы;
- partial: находка задевает дефект только частично (половина составного дефекта, упоминание вскользь, то же место, но другая суть);
- missed: такой находки нет.
Укажи номера находок (finding_ids). Совпадение только по теме без совпадения сути не засчитывай.

Задача 2. Для каждой находки, которая не сопоставлена ни с одним эталонным дефектом, реши: real (настоящий дефект документа, которого нет в эталоне) или noise (не дефект, придирка, пожелание, ошибка проверяющего).

Работай только с этими данными."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "gold": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "status": {
                        "type": "string",
                        "enum": ["found", "partial", "missed"],
                    },
                    "finding_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["id", "status", "finding_ids"],
                "additionalProperties": False,
            },
        },
        "extra": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding_id": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["real", "noise"]},
                },
                "required": ["finding_id", "verdict"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["gold", "extra"],
    "additionalProperties": False,
}

# (имя, модель-алиас CLI, effort). Алиасы на 25.11.2026: opus = Opus 5.5, sonnet = Sonnet 5.5,
# haiku = Haiku 5.5, fable = Fable 5.2 (фактическая модель берётся из modelUsage ответа).
MATRIX = [
    ("opus-low-1", "opus", "low"),
    ("opus-medium-1", "opus", "medium"),
    ("opus-high-1", "opus", "high"),
    ("opus-high-2", "opus", "high"),
    ("opus-xhigh-1", "opus", "xhigh"),
    ("opus-xhigh-2", "opus", "xhigh"),
    ("sonnet-high-1", "sonnet", "high"),
    ("sonnet-high-2", "sonnet", "high"),
    ("haiku-medium-1", "haiku", "medium"),
    ("haiku-high-1", "haiku", "high"),
    ("fable-high-1", "fable", "high"),
    ("fable-high-2", "fable", "high"),
    # Второй заход: третий прогон лидеров (объединение трёх = три прохода агента) и соседние effort.
    ("opus-xhigh-3", "opus", "xhigh"),
    ("sonnet-high-3", "sonnet", "high"),
    ("fable-high-3", "fable", "high"),
    ("sonnet-xhigh-1", "sonnet", "xhigh"),
    ("sonnet-xhigh-2", "sonnet", "xhigh"),
    ("fable-medium-1", "fable", "medium"),
    ("opus-max-1", "opus", "max"),
]

# Свободный текст без схемы, как в исследовании 11: сверка с цифрами helknow и цена схемы.
FREE_TEXT = [
    ("free-opus-high-1", "opus", "high"),
    ("free-opus-xhigh-1", "opus", "xhigh"),
    ("free-opus-medium-1", "opus", "medium"),
    ("free-sonnet-high-1", "sonnet", "high"),
    ("free-fable-high-1", "fable", "high"),
    ("free-haiku-high-1", "haiku", "high"),
    # Третий заход: по три прогона главных кандидатов в свободном тексте и Sonnet xhigh.
    ("free-opus-medium-2", "opus", "medium"),
    ("free-opus-medium-3", "opus", "medium"),
    ("free-opus-xhigh-2", "opus", "xhigh"),
    ("free-opus-xhigh-3", "opus", "xhigh"),
    ("free-sonnet-xhigh-1", "sonnet", "xhigh"),
]

# Двухшаговый вариант: свободный текст сильной модели -> перенос в JSON более дешёвой моделью.
EXTRACT_SYSTEM = "Ты аккуратно переносишь результаты ревью требований в структурированный вид. Отвечай на русском языке."
EXTRACT_PROMPT = """Ниже документ требований и ответ ревьюера свободным текстом.
Перенеси в JSON КАЖДУЮ находку ревьюера: ничего не добавляй от себя, не объединяй разные находки и не пропускай.
Поле quote: точный фрагмент документа, на который указывает находка, скопированный из документа дословно (не из ответа ревьюера).
Поля property, explanation и severity возьми из ответа ревьюера."""

# (имя, источник свободного текста, модель, effort)
EXTRACT = [
    ("xhaiku-free-opus-xhigh-1", "free-opus-xhigh-1", "haiku", "medium"),
    ("xsonnet-free-opus-xhigh-1", "free-opus-xhigh-1", "sonnet", "medium"),
    ("xsonnet-free-opus-high-1", "free-opus-high-1", "sonnet", "medium"),
]

# Переменные окружения облачной сессии, которые могут подмешать контекст или effort.
DROP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_EFFORT",
    "CLAUDE_ADDITIONAL_DIRECTORIES",
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def call(
    name: str,
    model: str,
    effort: str,
    system: str,
    stdin_text: str,
    schema: dict | None,
    timeout: int = 2400,
) -> dict:
    path = OUT / f"{name}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    exe = shutil.which("claude")
    if exe is None:
        raise FileNotFoundError("claude CLI not found")
    env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
    env["PYTHONUTF8"] = "1"
    args = [
        exe,
        "-p",
        "--model",
        model,
        "--effort",
        effort,
        "--tools",
        "",
        "--system-prompt",
        system,
        "--no-session-persistence",
        "--output-format",
        "json",
        "--safe-mode",
    ]
    if schema is not None:
        args += ["--json-schema", json.dumps(schema, ensure_ascii=False)]
    with tempfile.TemporaryDirectory(
        prefix="qairu-21-", ignore_cleanup_errors=True
    ) as cwd:
        started = datetime.now(UTC)
        clock = time.perf_counter()
        try:
            res = subprocess.run(
                args,
                check=False,
                input=stdin_text.encode("utf-8"),
                capture_output=True,
                cwd=cwd,
                env=env,
                timeout=timeout,
            )
            stdout, stderr, code, err = res.stdout, res.stderr, res.returncode, None
        except subprocess.TimeoutExpired as exc:
            stdout, stderr, code, err = (
                exc.stdout or b"",
                exc.stderr or b"",
                None,
                f"timeout {timeout}s",
            )
        wall = time.perf_counter() - clock
    stdout = stdout.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(stdout)
    except ValueError:
        parsed = None
    rec = {
        "name": name,
        "model_alias": model,
        "effort": effort,
        "started_at": started.isoformat(),
        "wall_s": wall,
        "returncode": code,
        "process_error": err,
        "stderr_tail": stderr.decode("utf-8", errors="replace")[-2000:],
        "parsed": parsed,
    }
    if parsed is None:
        rec["stdout_raw"] = stdout[-5000:]
    answer = (
        (parsed or {}).get("structured_output")
        if schema is not None
        else (parsed or {}).get("result")
    )
    ok = isinstance(parsed, dict) and not parsed.get("is_error") and bool(answer)
    save_json(path if ok else path.with_suffix(".failed.json"), rec)
    return rec


def search(entry: tuple[str, str, str]) -> dict:
    name, model, effort = entry
    text = PROMPT + "\n\n---\nДокумент:\n\n" + DOCUMENT.read_text(encoding="utf-8")
    schema = None if name.startswith("free-") else FINDINGS_SCHEMA
    rec = call(f"search-{name}", model, effort, SYSTEM_PROMPT, text, schema)
    print(
        f"[search] {name}: rc={rec['returncode']} wall={rec['wall_s']:.0f}s", flush=True
    )
    return rec


def judge(name: str, search_rec: dict) -> dict | None:
    parsed = search_rec.get("parsed") or {}
    if name.startswith("free-"):
        # Свободный текст: судья сам нумерует находки по порядку их появления в ответе.
        findings_block = (
            "Ответ проверяющего свободным текстом. Пронумеруй его находки по порядку, "
            "начиная с 1, и используй эти номера как id.\n\n"
            + (parsed.get("result") or "")
        )
        if not parsed.get("result"):
            return None
    else:
        findings = (parsed.get("structured_output") or {}).get("findings")
        if not findings:
            return None
        numbered = [
            {
                "id": i + 1,
                "quote": f["quote"],
                "property": f["property"],
                "explanation": f["explanation"],
            }
            for i, f in enumerate(findings)
        ]
        findings_block = json.dumps(numbered, ensure_ascii=False, indent=1)
    text = (
        JUDGE_PROMPT
        + "\n\n=== (1) ДОКУМЕНТ ===\n"
        + DOCUMENT.read_text(encoding="utf-8")
        + "\n\n=== (2) ЭТАЛОН ===\n"
        + GOLD.read_text(encoding="utf-8")
        + "\n\n=== (3) НАХОДКИ ===\n"
        + findings_block
    )
    rec = call(f"judge-{name}", "opus", "high", JUDGE_SYSTEM, text, JUDGE_SCHEMA)
    print(
        f"[judge] {name}: rc={rec['returncode']} wall={rec['wall_s']:.0f}s", flush=True
    )
    return rec


def pipeline(entry: tuple[str, str, str]) -> None:
    try:
        rec = search(entry)
        judge(entry[0], rec)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        # один упавший прогон не должен останавливать остальные
        print(f"[error] {entry[0]}: {exc!r}", flush=True)


def extract(entry: tuple[str, str, str, str]) -> None:
    name, source, model, effort = entry
    src = json.loads((OUT / f"search-{source}.json").read_text(encoding="utf-8"))
    text = (
        EXTRACT_PROMPT
        + "\n\n=== ДОКУМЕНТ ===\n"
        + DOCUMENT.read_text(encoding="utf-8")
        + "\n\n=== ОТВЕТ РЕВЬЮЕРА ===\n"
        + src["parsed"]["result"]
    )
    rec = call(f"search-{name}", model, effort, EXTRACT_SYSTEM, text, FINDINGS_SCHEMA)
    print(
        f"[extract] {name}: rc={rec['returncode']} wall={rec['wall_s']:.0f}s",
        flush=True,
    )
    judge(name, rec)


def rejudge(name: str) -> None:
    """Повторная оценка того же ответа: разброс самого судьи."""
    rec = json.loads((OUT / f"search-{name}.json").read_text(encoding="utf-8"))
    judge(f"{name}--r2", rec)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if sys.argv[1:2] == ["extract"]:
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(extract, EXTRACT))
        return
    if sys.argv[1:2] == ["rejudge"]:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(rejudge, sys.argv[2:]))
        return
    only = set(sys.argv[1:])
    entries = [
        e for e in MATRIX + FREE_TEXT if (not only and e in MATRIX) or e[0] in only
    ]
    workers = int(os.environ.get("QAIRU_WORKERS", "4"))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(pipeline, entries))


if __name__ == "__main__":
    main()
