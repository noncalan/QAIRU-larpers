"""Насколько сигналы обязательства отделяют блоки с требованиями от остальных (эксперимент).

Метки блоков берутся из gold_segmentation.yaml (только eval). Сигналы общие, без терминов документа.
Запуск: uv run --group experiments --with razdel python experiments/18-segmentation/guard_signals.py
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "03-proto"))
import preseg  # noqa: E402
import seg_eval  # noqa: E402

SIG = {
    "modal": re.compile(r"(?i)\b(долж\w*|необходимо|обязан\w*|не допускается|запрещ\w*|требуется|shall|must)\b"),
    "imperative": re.compile(r"^\s*(?:[*+-]|\d+[.)])?\s*(?:\*\*[^*]+\*\*\s*)?[А-ЯЁ][а-яё]+(?:ить|ать|ять|еть|уть|ти)\b"),
    "number_unit": re.compile(r"\d[\d\s]*\s?(₸|тенге|тиын\w*|сек\w*|мин\w*|час\w*|%|мс|бит|bit|знак\w*)"),
    "http": re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\b|\b[1-5]\d\d\b"),
    "arrow": re.compile(r"→"),
}

doc = open(ROOT / "data/input/requirements.md", encoding="utf-8").read()
units, _, _ = seg_eval.load_gold(doc, str(HERE / "gold_segmentation.yaml"))
starts = [0]
for ln in doc.splitlines(keepends=True):
    starts.append(starts[-1] + len(ln))
res = {"req": [0, 0], "optional": [0, 0], "none": [0, 0]}
for b in preseg.parse(doc):
    if b.kind == "heading":
        continue
    span = (starts[b.line_start - 1], starts[b.line_end])
    lab = "none"
    for u in units:
        if any(seg_eval.overlap(span, g) for g in u["spans"]):
            lab = "req" if (u["label"] == "req" or lab == "req") else "optional"
    hits = [k for k, r in SIG.items() if r.search(b.text)]
    res[lab][0] += 1
    res[lab][1] += bool(hits)
    if lab == "none" and hits:
        print("ложная тревога:", b.line_start, hits, (b.serialized or b.text)[:60])
print({k: f"сигнал в {v[1]} из {v[0]}" for k, v in res.items()})
