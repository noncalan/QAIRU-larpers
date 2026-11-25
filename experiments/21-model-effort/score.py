"""Сводка эксперимента 21: полнота, цитаты, время и цена по каждой паре «модель + effort».

Читает experiments/out/21/search-*.json и judge-*.json, печатает таблицы в Markdown и пишет
experiments/out/21/summary.json. Тексты находок и эталона не печатаются.

Запуск из корня репозитория:
    uv run --with rapidfuzz python experiments/21-model-effort/score.py
"""

import json
import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments" / "03-proto"))
from quote_check import locate

sys.path.insert(0, str(ROOT / "experiments" / "11-baseline-one-prompt"))
from score_quotes import (
    SPLIT,
)
from score_quotes import extract as extract_free_quotes

OUT = ROOT / "experiments" / "out" / "21"
DOCUMENT = (ROOT / "data" / "input" / "requirements.md").read_text(encoding="utf-8")
GOLD = (ROOT / "docs" / "gold" / "defects.md").read_text(encoding="utf-8")
GOLD_TOTAL = 45
ROW = re.compile(r"^\|\s*(\d+)\s*\|(.*)\|\s*$", re.MULTILINE)
MIN_SEG = 6


def gold_severity() -> dict[int, str]:
    sev = {}
    for m in ROW.finditer(GOLD):
        cells = [c.strip() for c in m.group(2).split("|")]
        sev[int(m.group(1))] = cells[-1].strip("* ").lower()
    return sev


def quote_verdict(quote: str) -> str:
    segments = [s.strip() for s in SPLIT.split(quote) if len(s.strip()) >= MIN_SEG]
    if not segments:
        return "skip"
    verdicts = [locate(DOCUMENT, s)[0] for s in segments]
    if "not_found" in verdicts:
        return "not_found"
    return "fuzzy" if "fuzzy" in verdicts else "exact"


def load(name: str) -> dict | None:
    path = OUT / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def run_metrics(name: str, sev: dict[int, str]) -> dict | None:
    s, j = load(f"search-{name}"), load(f"judge-{name}")
    if not s or not j:
        return None
    p, jp = s["parsed"], j["parsed"]
    judged = jp["structured_output"]
    gold = {g["id"]: g["status"] for g in judged["gold"] if 1 <= g["id"] <= GOLD_TOTAL}
    extra = [e["verdict"] for e in judged["extra"]]
    quotes = defaultdict(int)
    if p.get("structured_output"):
        findings = p["structured_output"]["findings"]
        quote_texts = [f["quote"] for f in findings]
        severity = {
            k: sum(f["severity"] == k for f in findings)
            for k in ("критично", "средне", "низко")
        }
        n_findings = len(findings)
    else:  # свободный текст: цитаты по эвристике исследования 11, число находок по нумерации судьи
        quote_texts = [q for _, q in extract_free_quotes(p["result"])]
        severity = {}
        ids = {i for g in judged["gold"] for i in g["finding_ids"]} | {
            e["finding_id"] for e in judged["extra"]
        }
        n_findings = max(ids) if ids else 0
    for q in quote_texts:
        quotes[quote_verdict(q)] += 1
    checked = sum(v for k, v in quotes.items() if k != "skip")
    crit = [i for i, v in sev.items() if v.startswith("крит")]
    usage = p["usage"]
    return {
        "name": name,
        "models": list(p.get("modelUsage", {})),
        "findings": n_findings,
        "found": sorted(i for i, v in gold.items() if v == "found"),
        "partial": sorted(i for i, v in gold.items() if v == "partial"),
        "recall_strict": sum(v == "found" for v in gold.values()) / GOLD_TOTAL,
        "recall": sum(v != "missed" for v in gold.values()) / GOLD_TOTAL,
        "crit_recall": sum(gold.get(i) != "missed" for i in crit) / len(crit),
        "crit_recall_strict": sum(gold.get(i) == "found" for i in crit) / len(crit),
        "extra_real": extra.count("real"),
        "extra_noise": extra.count("noise"),
        "quotes_exact": quotes["exact"] / checked if checked else 0.0,
        "quotes_not_found": quotes["not_found"] / checked if checked else 0.0,
        "wall_s": s["wall_s"],
        "cost_usd": p["total_cost_usd"],
        "output_tokens": usage["output_tokens"],
        "thinking_tokens": (usage.get("output_tokens_details") or {}).get(
            "thinking_tokens"
        ),
        "severity_counts": severity,
    }


def config_of(name: str) -> str:
    return name.rsplit("-", 1)[0]


def union_recall(runs: list[dict], strict: bool) -> float:
    hit = set()
    for r in runs:
        hit |= set(r["found"]) | (set() if strict else set(r["partial"]))
    return len(hit) / GOLD_TOTAL


def pct(x: float) -> str:
    return f"{100 * x:.1f}"


def main() -> None:
    sev = gold_severity()
    assert len(sev) == GOLD_TOTAL, f"gold rows: {len(sev)}"
    names = sorted(
        {
            p.stem.removeprefix("search-")
            for p in OUT.glob("search-*.json")
            if not p.stem.endswith(".failed")
        }
    )
    names = [n for n in names if (OUT / f"judge-{n}.json").exists()]
    runs = [m for m in (run_metrics(n, sev) for n in names) if m]
    by_cfg = defaultdict(list)
    for r in runs:
        by_cfg[config_of(r["name"])].append(r)

    print(
        "| Прогон | Находок | Recall (с частичными) | Recall строгий | Критичные (с частичными) | Лишние: настоящие / шум | Цитаты дословно | Цитаты не найдены | Время, с | Цена, $ | Выход, ток. | Из них thinking |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in runs:
        print(
            f"| {r['name']} | {r['findings']} | {pct(r['recall'])} | {pct(r['recall_strict'])} | "
            f"{pct(r['crit_recall'])} | {r['extra_real']} / {r['extra_noise']} | {pct(r['quotes_exact'])} | "
            f"{pct(r['quotes_not_found'])} | {r['wall_s']:.0f} | {r['cost_usd']:.3f} | {r['output_tokens']} | "
            f"{r['thinking_tokens']} |"
        )

    print(
        "\n| Конфигурация | Прогонов | Recall ср. | Recall строгий ср. | Критичные ср. | Объединение прогонов (с частичными / строго) | Цитаты дословно ср. | Время ср., с | Цена ср., $ |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    summary = {}
    for cfg, rs in by_cfg.items():
        mean = {
            k: sum(r[k] for r in rs) / len(rs)
            for k in (
                "recall",
                "recall_strict",
                "crit_recall",
                "quotes_exact",
                "wall_s",
                "cost_usd",
                "findings",
            )
        }
        mean["union"] = union_recall(rs, False)
        mean["union_strict"] = union_recall(rs, True)
        summary[cfg] = {**mean, "runs": len(rs)}
        print(
            f"| {cfg} | {len(rs)} | {pct(mean['recall'])} | {pct(mean['recall_strict'])} | "
            f"{pct(mean['crit_recall'])} | {pct(mean['union'])} / {pct(mean['union_strict'])} | "
            f"{pct(mean['quotes_exact'])} | {mean['wall_s']:.0f} | {mean['cost_usd']:.3f} |"
        )

    print("\nОбъединение пар прогонов (как два прохода агента), с частичными / строго:")
    pairs = []
    for a, b in combinations(runs, 2):
        pairs.append(
            (
                union_recall([a, b], False),
                union_recall([a, b], True),
                a["name"],
                b["name"],
            )
        )
    for u, us, a, b in sorted(pairs, reverse=True)[:12]:
        print(f"- {a} + {b}: {pct(u)} / {pct(us)}")

    (OUT / "summary.json").write_text(
        json.dumps({"runs": runs, "configs": summary}, ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
