"""Метрики качества нарезки против эталона gold_segmentation.yaml (эксперимент, не код агента).

Предсказание: JSON-список объектов {"id": "R-01", "source_id": "Ф-1"|null, "quote": "...",
"quotes": [...] (необязательно)}. Каждая цитата ищется в документе (точно, затем rapidfuzz >= 90).

Метрики:
  quote_valid      доля цитат предсказания, найденных в документе
  unit_recall      доля эталонных unit (label=req), с которыми пересекается хотя бы одна цитата
  atom P/R/F1      сопоставление один-к-одному «элемент нарезки ↔ атом эталона» при IoU спанов >= 0.5
  merged / split   элементы, покрывающие >= 2 атомов (склейка); атомы, разрезанные на части
  false_items      элементы, не пересекающие ни одной цитаты req/optional (выделено «не требование»)
  is_req_precision 1 - false_items/items: «выделенное вообще является требованием» (без учёта гранулярности)
  req_line_cover   доля строк с требованиями эталона, покрытых цитатами предсказания

Запуск: uv run --group experiments --with razdel python experiments/18-segmentation/seg_eval.py \
          data/input/requirements.md experiments/18-segmentation/gold_segmentation.yaml [pred.json | --baselines]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "03-proto"))
sys.path.insert(0, str(HERE))
from quote_check import locate  # noqa: E402  нормализация с картой смещений + rapidfuzz
import preseg  # noqa: E402

IOU = 0.5


def span_of(doc: str, quote: str):
    kind, score, s, e = locate(doc, quote)
    return (s, e, kind) if s is not None else None


def iou(a, b) -> float:
    inter = max(0, min(a[1], b[1]) - max(a[0], b[0]))
    union = max(a[1], b[1]) - min(a[0], b[0])
    return inter / union if union else 0.0


def overlap(a, b) -> int:
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def load_gold(doc: str, path: str):
    g = yaml.safe_load(open(path, encoding="utf-8"))
    units, atoms, missing = [], [], []
    for u in g["units"]:
        spans = []
        for q in [u["quote"], *u.get("also", [])]:
            sp = span_of(doc, q)
            (spans.append(sp[:2]) if sp and sp[2] == "exact" else missing.append((u["gid"], q, sp and sp[2])))
        unit = {**u, "spans": spans}
        units.append(unit)
        if u.get("atoms"):
            for k, q in enumerate(u["atoms"], 1):
                sp = span_of(doc, q)
                if not sp or sp[2] != "exact":
                    missing.append((f"{u['gid']}.{k}", q, sp and sp[2]))
                    continue
                atoms.append({"aid": f"{u['gid']}.{k}", "gid": u["gid"], "label": u["label"], "spans": [sp[:2]]})
        else:
            atoms.append({"aid": u["gid"], "gid": u["gid"], "label": u["label"], "spans": spans})
    return units, atoms, missing


def evaluate(doc: str, units, atoms, pred: list[dict]) -> dict:
    items, bad_quotes, nq = [], 0, 0
    for p in pred:
        qs = p.get("quotes") or [p["quote"]]
        spans = []
        for q in qs:
            nq += 1
            sp = span_of(doc, q)
            if sp:
                spans.append(sp[:2])
            else:
                bad_quotes += 1
        items.append({"id": p["id"], "spans": spans})
    req_units = [u for u in units if u["label"] == "req"]
    req_atoms = [a for a in atoms if a["label"] == "req"]
    # unit recall: любое пересечение
    hit_units = {u["gid"] for u in req_units for it in items for s in it["spans"] for g in u["spans"] if overlap(s, g)}
    # atom P/R: жадное сопоставление по IoU (максимум по парам спанов)
    pairs = []
    for i, it in enumerate(items):
        for j, a in enumerate(atoms):
            best = max((iou(s, g) for s in it["spans"] for g in a["spans"]), default=0.0)
            if best >= IOU:
                pairs.append((best, i, j))
    pairs.sort(reverse=True)
    used_i, used_j, match = set(), set(), {}
    for sc, i, j in pairs:
        if i in used_i or j in used_j:
            continue
        used_i.add(i); used_j.add(j); match[i] = j
    tp = sum(1 for i, j in match.items() if atoms[j]["label"] == "req")
    opt = sum(1 for i, j in match.items() if atoms[j]["label"] == "optional")
    all_gold_spans = [g for u in units for g in u["spans"]]
    false_items = [it["id"] for it in items if it["spans"] and not any(overlap(s, g) for s in it["spans"] for g in all_gold_spans)]
    # склейка: элемент, покрывающий >= 80 % каждого из >= 2 атомов
    merged = 0
    for it in items:
        covered = sum(1 for a in req_atoms
                      if any(overlap(s, g) >= 0.8 * (g[1] - g[0]) for s in it["spans"] for g in a["spans"]))
        merged += covered >= 2
    # разрезание: атом, с которым пересекаются >= 2 элемента, и ни один не совпал по IoU
    split = sum(1 for j, a in enumerate(atoms) if a["label"] == "req" and j not in used_j and
                sum(1 for it in items if any(overlap(s, g) for s in it["spans"] for g in a["spans"])) >= 2)
    # покрытие строк с требованиями
    def lines(spans):
        out = set()
        for s, e in spans:
            out.update(range(doc.count("\n", 0, s) + 1, doc.count("\n", 0, max(s, e - 1)) + 2))
        return out
    gold_lines = lines([g for u in req_units for g in u["spans"]])
    pred_lines = lines([s for it in items for s in it["spans"]])
    denom_p = len(items) - opt  # optional не штрафуем и не награждаем
    prec = tp / denom_p if denom_p else 0.0
    rec = tp / len(req_atoms) if req_atoms else 0.0
    return {
        "items": len(items), "quotes": nq, "quote_valid": round(1 - bad_quotes / nq, 3) if nq else None,
        "unit_recall": f"{len(hit_units)}/{len(req_units)} = {len(hit_units) / len(req_units):.2f}",
        "is_req_precision": round(1 - len(false_items) / len(items), 2) if items else None,
        "atom_tp": tp, "atom_precision": round(prec, 2), "atom_recall": round(rec, 2),
        "atom_f1": round(2 * prec * rec / (prec + rec), 2) if prec + rec else 0.0,
        "merged_items": merged, "split_atoms": split, "false_items": len(false_items),
        "req_line_cover": f"{len(gold_lines & pred_lines)}/{len(gold_lines)}",
        "missed_units": sorted({u['gid'] for u in req_units} - hit_units),
    }


def baselines(doc: str, units, atoms) -> dict[str, list[dict]]:
    blocks = [b for b in preseg.parse(doc) if b.kind != "heading"]
    out = {}
    out["numbered_only"] = [{"id": b.bid, "quote": b.text} for b in blocks if b.source_id]
    out["per_block"] = [{"id": b.bid, "quote": b.text} for b in blocks]
    sent = []
    for b in blocks:
        if b.kind in ("paragraph", "list_item"):
            sent += [{"id": f"{b.bid}.{k}", "quote": s["text"]} for k, s in enumerate(b.sentences, 1)]
        else:
            sent.append({"id": b.bid, "quote": b.text})
    out["per_sentence"] = sent
    # «как в эталоне, но без атомизации»: только unit-цитаты req (проверка метрики склейки)
    out["gold_units_only"] = [{"id": u["gid"], "quote": u["quote"]} for u in units if u["label"] == "req"]
    gold_atoms = []
    for u in units:
        for k, q in enumerate(u.get("atoms") or [u["quote"]], 1):
            gold_atoms.append({"id": f"{u['gid']}.{k}", "quote": q, "quotes": [q, *u.get("also", [])] if not u.get("atoms") else None})
    out["gold_atoms (sanity)"] = [{k: v for k, v in g.items() if v is not None} for g in gold_atoms]
    return out


if __name__ == "__main__":
    doc = open(sys.argv[1], encoding="utf-8").read()
    units, atoms, missing = load_gold(doc, sys.argv[2])
    req_u = sum(u["label"] == "req" for u in units)
    req_a = sum(a["label"] == "req" for a in atoms)
    print(f"gold: units {len(units)} (req {req_u}, optional {len(units) - req_u}); atoms {len(atoms)} (req {req_a})")
    from collections import Counter
    print("gold req units by type:", dict(Counter(u["type"] for u in units if u["label"] == "req")))
    print("gold quotes not found exactly:", missing or "нет")
    if len(sys.argv) > 3 and sys.argv[3] != "--baselines":
        preds = {"pred": json.load(open(sys.argv[3], encoding="utf-8"))}
    else:
        preds = baselines(doc, units, atoms)
    for name, p in preds.items():
        r = evaluate(doc, units, atoms, p)
        print(f"\n== {name}\n" + json.dumps(r, ensure_ascii=False))
