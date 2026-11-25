"""Эксперимент 22: промпт и рубрика поиска дефектов (шаг 4 агента, issue #14).

Один прогон = один проход поиска: системный промпт `src/validator/prompts/search_system.md`,
шаблон `search_user.md` и документ `data/input/requirements.md`, показанный блоками с ID
(`render.py`) или сырым Markdown (`--mode raw`, то же, что B' в протоколе контрольной точки).
Для сравнения в тот же день `--mode baseline` гоняет промпт базовой линии исследования 21
(free-opus-medium: свободный текст, судья сам нумерует находки).
Модель Opus 5.5 `medium`, ответ свободным текстом без схемы (D-010). Затем тот же судья, что
в исследовании 21 (промпт, схема, Opus 5.5 `high`), вслепую сопоставляет находки с эталоном.
Эталон попадает только в запрос судьи и никогда в запрос поиска (CLAUDE.md, правило 2).

Запуск из корня репозитория (нужен вход в Claude Code по подписке):
    uv run --no-project --with rapidfuzz --with razdel python experiments/22-search-prompt/run.py run it1 --runs 2
    uv run --no-project --with rapidfuzz --with razdel python experiments/22-search-prompt/run.py run raw --runs 2 --mode raw
    uv run --no-project --with rapidfuzz --with razdel python experiments/22-search-prompt/run.py run base --runs 2 --mode baseline
    uv run --no-project --with rapidfuzz --with razdel python experiments/22-search-prompt/run.py score
    uv run --no-project --with rapidfuzz --with razdel python experiments/22-search-prompt/run.py leak

Ответы лежат в experiments/out/22/ (в git не попадают) и повторно не запрашиваются. Тексты находок
и эталона скрипт не печатает: только счётчики, номера эталонных дефектов и их столбец «Критерий».
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "experiments" / "03-proto"))
sys.path.insert(0, str(ROOT / "experiments" / "11-baseline-one-prompt"))
sys.path.insert(0, str(ROOT / "experiments" / "21-model-effort"))
import render
from quote_check import line_of, locate
from run_matrix import DROP_ENV, JUDGE_PROMPT, JUDGE_SCHEMA, JUDGE_SYSTEM
from run_matrix import PROMPT as BASELINE_PROMPT
from run_matrix import SYSTEM_PROMPT as BASELINE_SYSTEM
from score_quotes import SPLIT
from score_quotes import extract as extract_free_quotes

OUT = ROOT / "experiments" / "out" / "22"
DOCUMENT = ROOT / "data" / "input" / "requirements.md"
GOLD = ROOT / "docs" / "gold" / "defects.md"
PROMPTS = ROOT / "src" / "validator" / "prompts"
SYSTEM_FILE = PROMPTS / "search_system.md"
USER_FILE = PROMPTS / "search_user.md"
PLACEHOLDER = "{document}"

SEARCH_MODEL, SEARCH_EFFORT = "claude-opus-5-5", "medium"
# Судья исследования 21: Opus 5.5 (там алиас `opus`, на 25.11.2026 это claude-opus-5-5), effort high.
JUDGE_MODEL, JUDGE_EFFORT = "claude-opus-5-5", "high"
SEARCH_TIMEOUT_S, JUDGE_TIMEOUT_S = 1800, 2400
MAX_PARALLEL = 2
GOLD_TOTAL = 45
MIN_SEG = 6  # как в score.py исследования 21: сегменты цитаты короче не проверяются
# Тот же текст, что у судьи исследования 21 для свободного текста.
JUDGE_PREAMBLE = (
    "Ответ проверяющего свободным текстом. Пронумеруй его находки по порядку, "
    "начиная с 1, и используй эти номера как id.\n\n"
)

GOLD_ROW = re.compile(r"^\|\s*(\d+)\s*\|(.*)\|\s*$", re.MULTILINE)
FINDINGS_SECTION = re.compile(r"^#{1,3}\s*\**\s*Находки", re.MULTILINE)
FINDING_HEAD = re.compile(r"^[ \t]{0,3}#{0,6}[ \t]*\**[ \t]*[НH]-(\d+)\b", re.MULTILINE)
FIELD = re.compile(r"^\s*[-*]\s*\**([А-Яа-яЁё ]{3,20}?)\**\s*:\s*\**\s*(.*)$")
BLOCK_ID = re.compile(r"\bB\d{3}(?:\.\d+)?\b")

# Слова проверяемого документа и правила 3 CLAUDE.md, которых не должно быть в промптах.
FORBIDDEN = re.compile(
    r"порог|тиын|тенге|₸|\bф-\d|push|алмат|kzt|alertscontroller|savelimit|limit_kzt",
    re.IGNORECASE,
)
SHINGLE = 6


# --------------------------------------------------------------------------- вызов модели


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)


def load(name: str) -> dict | None:
    path = OUT / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def claude(
    name: str,
    model: str,
    effort: str,
    system: str,
    stdin_text: str,
    schema: dict | None,
    timeout: int,
    meta: dict,
) -> dict:
    """Один вызов `claude -p` (как в исследовании 21) с кешем на диске по имени."""
    cached = load(name)
    if cached is not None:
        return cached
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
        prefix="qairu-22-", ignore_cleanup_errors=True
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
            stdout, stderr, code = exc.stdout or b"", exc.stderr or b"", None
            err = f"timeout {timeout}s"
        wall = time.perf_counter() - clock
    out = stdout.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(out)
    except ValueError:
        parsed = None
    rec = {
        "name": name,
        "model": model,
        "effort": effort,
        "meta": meta,
        "started_at": started.isoformat(),
        "wall_s": wall,
        "returncode": code,
        "process_error": err,
        "stderr_tail": stderr.decode("utf-8", errors="replace")[-2000:],
        "parsed": parsed,
    }
    if parsed is None:
        rec["stdout_raw"] = out[-5000:]
    answer = (parsed or {}).get("structured_output" if schema else "result")
    ok = isinstance(parsed, dict) and not parsed.get("is_error") and bool(answer)
    save_json(OUT / (f"{name}.json" if ok else f"{name}.failed.json"), rec)
    return rec


# --------------------------------------------------------------------------- промпт и документ


def prompt_texts() -> tuple[str, str]:
    system = SYSTEM_FILE.read_text(encoding="utf-8").strip()
    user = USER_FILE.read_text(encoding="utf-8")
    if user.count(PLACEHOLDER) != 1:
        raise ValueError(
            f"{USER_FILE.name}: placeholder {PLACEHOLDER} must occur exactly once"
        )
    return system, user


def prompt_sha() -> str:
    system, user = prompt_texts()
    return hashlib.sha256((system + "\0" + user).encode("utf-8")).hexdigest()[:12]


def document_for_model(raw: bool) -> str:
    md = DOCUMENT.read_text(encoding="utf-8")
    return md if raw else render.render(render.parse(md))


def search(name: str, mode: str) -> dict:
    """mode: blocks (наш промпт, ID блоков), raw (наш промпт, сырой MD), baseline (исследование 21)."""
    if mode == "baseline":
        system = BASELINE_SYSTEM
        text = BASELINE_PROMPT + "\n\n---\nДокумент:\n\n" + document_for_model(raw=True)
        meta = {"prompt_sha": "baseline-21", "mode": mode}
    else:
        system, user = prompt_texts()
        doc = document_for_model(raw=mode == "raw").rstrip("\n")
        text = user.replace(PLACEHOLDER, doc)
        meta = {"prompt_sha": prompt_sha(), "mode": mode}
    rec = claude(
        f"search-{name}",
        SEARCH_MODEL,
        SEARCH_EFFORT,
        system,
        text,
        None,
        SEARCH_TIMEOUT_S,
        meta,
    )
    if rec.get("meta", {}).get("prompt_sha") != meta["prompt_sha"]:
        print(
            f"[warn] search-{name}: cached answer was made with another prompt",
            flush=True,
        )
    print(
        f"[search] {name}: rc={rec['returncode']} wall={rec['wall_s']:.0f}s", flush=True
    )
    return rec


# --------------------------------------------------------------------------- разбор ответа


@dataclass
class Finding:
    num: int
    text: str
    fields: dict[str, list[str]] = field(default_factory=dict)

    def get(self, key: str) -> str:
        values = self.fields.get(key) or [""]
        return values[0]

    @property
    def quotes(self) -> list[str]:
        return [unquote(v) for v in self.fields.get("цитата", []) if unquote(v)]

    @property
    def blocks(self) -> list[str]:
        return BLOCK_ID.findall(" ".join(self.fields.get("блоки", [])))


def unquote(value: str) -> str:
    """Содержимое между первой « и последней » (внутри цитаты бывают свои ёлочки)."""
    value = value.strip().rstrip(".;").strip()
    start, end = value.find("«"), value.rfind("»")
    if start != -1 and end > start:
        return value[start + 1 : end].strip()
    return value.strip("\"'“”` ")


def findings_part(answer: str) -> str:
    m = FINDINGS_SECTION.search(answer)
    if m:
        return answer[m.start() :]
    first = FINDING_HEAD.search(answer)
    return answer[first.start() :] if first else ""


def parse_findings(answer: str) -> list[Finding]:
    part = findings_part(answer)
    heads = list(FINDING_HEAD.finditer(part))
    findings = []
    for k, m in enumerate(heads):
        end = heads[k + 1].start() if k + 1 < len(heads) else len(part)
        chunk = part[m.start() : end].strip()
        # Подзаголовки «### Набор требований» между находками в находку не входят.
        chunk = re.sub(r"\n#{1,3}\s[^\n]*$", "", chunk).strip()
        f = Finding(num=int(m.group(1)), text=chunk)
        for line in chunk.splitlines()[1:]:
            fm = FIELD.match(line)
            if fm:
                f.fields.setdefault(fm.group(1).strip().lower(), []).append(
                    fm.group(2).strip()
                )
        findings.append(f)
    return findings


def judge_block(findings: list[Finding]) -> str:
    """Находки, перенумерованные подряд с 1: номер судьи = порядковый номер находки."""
    parts = []
    for k, f in enumerate(findings, 1):
        head, _, body = f.text.partition("\n")
        title = re.sub(r"^[#\s*]*[НH]-\d+[.:)]?\s*", "", head).strip(" *")
        parts.append(f"#### Н-{k}. {title}\n{body}")
    return JUDGE_PREAMBLE + "\n\n".join(parts)


def judge(name: str, search_rec: dict) -> dict | None:
    answer = (search_rec.get("parsed") or {}).get("result") or ""
    baseline = search_rec.get("meta", {}).get("mode") == "baseline"
    findings = [] if baseline else parse_findings(answer)
    if not answer or not (baseline or findings):
        print(f"[judge] {name}: no findings parsed, skipped", flush=True)
        return None
    # Базовую линию судья нумерует сам, как free-* в исследовании 21.
    block = JUDGE_PREAMBLE + answer if baseline else judge_block(findings)
    text = (
        JUDGE_PROMPT
        + "\n\n=== (1) ДОКУМЕНТ ===\n"
        + DOCUMENT.read_text(encoding="utf-8")
        + "\n\n=== (2) ЭТАЛОН ===\n"
        + GOLD.read_text(encoding="utf-8")
        + "\n\n=== (3) НАХОДКИ ===\n"
        + block
    )
    meta = {
        "findings": len(findings) or None,
        "search_sha": search_rec.get("meta", {}).get("prompt_sha"),
    }
    rec = claude(
        f"judge-{name}",
        JUDGE_MODEL,
        JUDGE_EFFORT,
        JUDGE_SYSTEM,
        text,
        JUDGE_SCHEMA,
        JUDGE_TIMEOUT_S,
        meta,
    )
    print(
        f"[judge] {name}: rc={rec['returncode']} wall={rec['wall_s']:.0f}s", flush=True
    )
    return rec


def pipeline(name: str, mode: str) -> None:
    try:
        rec = search(name, mode)
        if (rec.get("parsed") or {}).get("result"):
            judge(name, rec)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        print(
            f"[error] {name}: {exc!r}", flush=True
        )  # один упавший прогон не останавливает другие


# --------------------------------------------------------------------------- метрики


def gold_columns() -> dict[int, dict[str, str]]:
    rows = {}
    for m in GOLD_ROW.finditer(GOLD.read_text(encoding="utf-8")):
        cells = [c.strip() for c in m.group(2).split("|")]
        rows[int(m.group(1))] = {
            "criterion": cells[-2],
            "severity": cells[-1].strip("* ").lower(),
        }
    return rows


def quote_verdict(doc: str, quote: str) -> tuple[str, int | None]:
    """exact / fuzzy / not_found / skip и смещение первого найденного сегмента."""
    segments = [s.strip() for s in SPLIT.split(quote) if len(s.strip()) >= MIN_SEG]
    if not segments:
        return "skip", None
    located = [locate(doc, s) for s in segments]
    kinds = [x[0] for x in located]
    start = next((x[2] for x in located if x[2] is not None), None)
    if "not_found" in kinds:
        return "not_found", start
    return ("fuzzy" if "fuzzy" in kinds else "exact"), start


def block_lines() -> dict[str, tuple[int, int]]:
    blocks = render.parse(DOCUMENT.read_text(encoding="utf-8"))
    return {b.bid: (b.line_start, b.line_end) for b in blocks}


def anchor_metrics(findings: list[Finding], doc: str) -> dict:
    lines, valid_ids = (
        block_lines(),
        render.block_ids(render.parse(DOCUMENT.read_text(encoding="utf-8"))),
    )
    ids = [i for f in findings for i in f.blocks]
    located = inside = 0
    for f in findings:
        spans = [lines[i.split(".")[0]] for i in f.blocks if i.split(".")[0] in lines]
        for q in f.quotes:
            verdict, start = quote_verdict(doc, q)
            if verdict in ("exact", "fuzzy") and start is not None:
                located += 1
                ln = line_of(doc, start)
                inside += any(a <= ln <= b for a, b in spans)
    return {
        "ids_total": len(ids),
        "ids_invalid": sum(i not in valid_ids for i in ids),
        "findings_with_id": sum(bool(f.blocks) for f in findings),
        "quotes_in_cited_blocks": inside / located if located else 0.0,
    }


def format_metrics(answer: str, judged: dict, doc: str) -> dict:
    """Счётчики по нашему формату находок (Н-1…) или по свободному тексту базовой линии."""
    findings = parse_findings(answer)
    if (
        not findings
    ):  # базовая линия: цитаты по эвристике исследования 11, число находок по судье
        ids = {i for g in judged["gold"] for i in g["finding_ids"]}
        ids |= {e["finding_id"] for e in judged["extra"]}
        quotes = Counter(
            quote_verdict(doc, q)[0] for _, q in extract_free_quotes(answer)
        )
        return {"findings": max(ids, default=0), "quotes": quotes}
    verdicts = Counter(f.get("вердикт").strip(" *.").lower() for f in findings)
    return {
        "findings": len(findings),
        "quotes": Counter(quote_verdict(doc, q)[0] for f in findings for q in f.quotes),
        "findings_without_quote": sum(not f.quotes for f in findings),
        "verdict_violation": verdicts.get("нарушение", 0),
        "verdict_unclear": verdicts.get("неясно", 0),
        "blocks_testing": sum(
            f.get("тест").lower().startswith("мешает") for f in findings
        ),
        "severity": dict(
            Counter(f.get("важность").strip(" *.").lower() for f in findings)
        ),
        "types": dict(
            Counter(f.get("тип дефекта").strip(" *.").lower() for f in findings)
        ),
        **anchor_metrics(findings, doc),
    }


def run_metrics(name: str, gold: dict[int, dict[str, str]]) -> dict | None:
    s, j = load(f"search-{name}"), load(f"judge-{name}")
    if not s or not j:
        return None
    doc = DOCUMENT.read_text(encoding="utf-8")
    p, judged = s["parsed"], j["parsed"]["structured_output"]
    status = {
        g["id"]: g["status"] for g in judged["gold"] if 1 <= g["id"] <= GOLD_TOTAL
    }
    extra = Counter(e["verdict"] for e in judged["extra"])
    fm = format_metrics(p["result"], judged, doc)
    quotes = fm.pop("quotes")
    checked = sum(v for k, v in quotes.items() if k != "skip")
    crit = [i for i, row in gold.items() if row["severity"].startswith("крит")]
    n = fm["findings"]
    return {
        "name": name,
        "mode": s.get("meta", {}).get("mode"),
        "prompt_sha": s.get("meta", {}).get("prompt_sha"),
        "models": list(p.get("modelUsage", {})),
        "found": sorted(i for i, v in status.items() if v == "found"),
        "partial": sorted(i for i, v in status.items() if v == "partial"),
        "missed": sorted(i for i, v in status.items() if v == "missed"),
        "recall": sum(v != "missed" for v in status.values()) / GOLD_TOTAL,
        "recall_strict": sum(v == "found" for v in status.values()) / GOLD_TOTAL,
        "crit_recall": sum(status.get(i) not in (None, "missed") for i in crit)
        / len(crit),
        "crit_strict": sum(status.get(i) == "found" for i in crit) / len(crit),
        "extra_real": extra["real"],
        "extra_noise": extra["noise"],
        "noise_share": extra["noise"] / n if n else 0.0,
        "quotes_total": checked,
        "quotes_exact": quotes["exact"] / checked if checked else 0.0,
        "quotes_found": (quotes["exact"] + quotes["fuzzy"]) / checked
        if checked
        else 0.0,
        **fm,
        "wall_s": s["wall_s"],
        "cost_usd": p.get("total_cost_usd", 0.0),
        "output_tokens": p.get("usage", {}).get("output_tokens"),
        "thinking_tokens": (p.get("usage", {}).get("output_tokens_details") or {}).get(
            "thinking_tokens"
        ),
    }


def config_of(name: str) -> str:
    return name.rsplit("-", 1)[0]


def union(runs: list[dict], strict: bool) -> set[int]:
    hit: set[int] = set()
    for r in runs:
        hit |= set(r["found"]) | (set() if strict else set(r["partial"]))
    return hit


def pct(x: float) -> str:
    return f"{100 * x:.1f}"


def ids_cell(r: dict) -> str:
    return f"{r['ids_invalid']} из {r['ids_total']}" if r.get("ids_total") else "—"


def in_block_cell(r: dict) -> str:
    return pct(r["quotes_in_cited_blocks"]) if r.get("ids_total") else "—"


def score() -> None:
    gold = gold_columns()
    assert len(gold) == GOLD_TOTAL, f"gold rows: {len(gold)}"
    names = sorted(
        p.stem.removeprefix("search-")
        for p in OUT.glob("search-*.json")
        if not p.stem.endswith(".failed")
    )
    runs = [m for m in (run_metrics(n, gold) for n in names) if m]
    print(
        "| Прогон | Режим | Находок | Recall | Строгий | Критичные | Критичные строго | "
        "Лишние: настоящие / шум | Доля шума | Цитаты дословно | Цитаты найдены | "
        "Неясно | Мешает тесту | ID невалидны | Цитата в своём блоке | Время, с | Цена, $ |"
    )
    print("|" + "---|" * 17)
    for r in runs:
        print(
            f"| {r['name']} | {r['mode']} | {r['findings']} | {pct(r['recall'])} | "
            f"{pct(r['recall_strict'])} | {pct(r['crit_recall'])} | {pct(r['crit_strict'])} | "
            f"{r['extra_real']} / {r['extra_noise']} | {pct(r['noise_share'])} | "
            f"{pct(r['quotes_exact'])} | {pct(r['quotes_found'])} | "
            f"{r.get('verdict_unclear', '—')} | {r.get('blocks_testing', '—')} | "
            f"{ids_cell(r)} | {in_block_cell(r)} | {r['wall_s']:.0f} | {r['cost_usd']:.3f} |"
        )
    by_cfg: dict[str, list[dict]] = {}
    for r in runs:
        by_cfg.setdefault(config_of(r["name"]), []).append(r)
    print(
        "\n| Конфигурация | Прогонов | Recall ср. | Строгий ср. | Критичные ср. | "
        "Объединение (строго) | Жаккар найденного | Находок ср. | Доля шума ср. | "
        "Цитаты дословно ср. | Время ср., с | Цена ср., $ |"
    )
    print("|" + "---|" * 12)
    summary = {}
    for cfg, rs in by_cfg.items():
        mean = {
            k: sum(r[k] for r in rs) / len(rs)
            for k in (
                "recall",
                "recall_strict",
                "crit_recall",
                "findings",
                "noise_share",
                "quotes_exact",
                "wall_s",
                "cost_usd",
            )
        }
        sets = [union([r], False) for r in rs]
        pairs = [len(a & b) / len(a | b) for a, b in combinations(sets, 2) if a | b]
        mean["jaccard"] = sum(pairs) / len(pairs) if pairs else 1.0
        mean["union"] = len(union(rs, False)) / GOLD_TOTAL
        mean["union_strict"] = len(union(rs, True)) / GOLD_TOTAL
        summary[cfg] = {**mean, "runs": len(rs)}
        print(
            f"| {cfg} | {len(rs)} | {pct(mean['recall'])} | {pct(mean['recall_strict'])} | "
            f"{pct(mean['crit_recall'])} | {pct(mean['union'])} ({pct(mean['union_strict'])}) | "
            f"{mean['jaccard']:.2f} | {mean['findings']:.1f} | {pct(mean['noise_share'])} | "
            f"{pct(mean['quotes_exact'])} | {mean['wall_s']:.0f} | {mean['cost_usd']:.3f} |"
        )
    print("\nПропуски (номер эталона: столбец «Критерий»), без текста дефекта:")
    for cfg, rs in by_cfg.items():
        missed = Counter(i for r in rs for i in r["missed"])
        items = ", ".join(
            f"№{i}×{n} ({gold[i]['criterion'][:40]})" for i, n in sorted(missed.items())
        )
        print(f"- {cfg}: {items or 'нет'}")
    save_json(OUT / "summary.json", {"runs": runs, "configs": summary})


# --------------------------------------------------------------------------- утечка в промпт


def words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


def leak() -> int:
    """Грубая копия будущего tests/test_prompt_leak.py: 6 слов подряд из документа и слова правила 3."""
    doc = words(DOCUMENT.read_text(encoding="utf-8"))
    shingles = {tuple(doc[i : i + SHINGLE]) for i in range(len(doc) - SHINGLE + 1)}
    problems = 0
    for path in sorted(PROMPTS.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        w = words(text)
        hits = {
            tuple(w[i : i + SHINGLE]) for i in range(len(w) - SHINGLE + 1)
        } & shingles
        bad = sorted({m.group(0).lower() for m in FORBIDDEN.finditer(text)})
        problems += len(hits) + len(bad)
        print(
            f"{path.relative_to(ROOT)}: shingles={len(hits)} forbidden={bad or 'нет'}"
        )
        for h in sorted(hits):
            print("   ", " ".join(h))
    return 1 if problems else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="search + judge for <tag>-1..N")
    r.add_argument("tag")
    r.add_argument("--runs", type=int, default=2)
    r.add_argument(
        "--mode",
        choices=("blocks", "raw", "baseline"),
        default="blocks",
        help="blocks: our prompt with block IDs; raw: without IDs (B'); "
        "baseline: prompt of research 21 (free-opus-medium)",
    )
    sub.add_parser("score")
    sub.add_parser("leak")
    sub.add_parser("render")
    args = ap.parse_args()
    if args.cmd == "run":
        OUT.mkdir(parents=True, exist_ok=True)
        names = [f"{args.tag}-{k}" for k in range(1, args.runs + 1)]
        print(f"prompt {prompt_sha()}, runs: {', '.join(names)}", flush=True)
        with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as pool:
            list(pool.map(lambda n: pipeline(n, args.mode), names))
        return 0
    if args.cmd == "score":
        score()
        return 0
    if args.cmd == "render":
        print(document_for_model(raw=False), end="")
        return 0
    return leak()


if __name__ == "__main__":
    sys.exit(main())
