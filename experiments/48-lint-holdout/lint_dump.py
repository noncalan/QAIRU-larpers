"""Срабатывания линтера на документе кейса и чужих документах без вызовов модели (#48).

Нарезку берём из сохранённых прогонов (``report.json``: снапшоты e2e и прогоны холдаута), документ
разбираем заново, линтер запускаем текущим кодом. Сравнение «до и после» правки: запустить на ``main``
и на ветке и сравнить JSON::

    uv run python experiments/48-lint-holdout/lint_dump.py /tmp/before.json   # на main
    uv run python experiments/48-lint-holdout/lint_dump.py /tmp/after.json    # на ветке
    cmp /tmp/before.json /tmp/after.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from validator.ingest import parse
from validator.lint import lint
from validator.models import DocKind, Segmentation

DOCS = {
    "req-md": ("data/input/requirements.md", "tests/fixtures/e2e/md/report.json"),
    "req-pdf": ("data/input/requirements.pdf", "tests/fixtures/e2e/pdf/report.json"),
    "req-docx": ("data/input/requirements.docx", "tests/fixtures/e2e/docx/report.json"),
    "c4": ("data/holdout/c4/c4_tech_task.md", "evals/holdout/c4/report.json"),
    "task": ("data/holdout/task/task.md", "evals/holdout/task/report.json"),
    "finuchet": ("data/holdout/finuchet/FINUCHET.001-01-TZ.pdf", "evals/holdout/finuchet/report.json"),
}


def main(out: Path) -> None:
    result: dict[str, list[dict[str, object]]] = {}
    for name, (doc_path, report) in DOCS.items():
        path = Path(doc_path)
        doc = parse(path, DocKind(path.suffix[1:]))
        seg = Segmentation.model_validate(
            json.loads(Path(report).read_text(encoding="utf-8"))["segmentation"]
        )
        shifted = sum(doc.text[r.anchor.char_start : r.anchor.char_end] != r.quote for r in seg.requirements)
        hits = [c.model_dump(mode="json") for c in lint(doc, seg)]
        result[name] = hits
        counts = f"требований {len(seg.requirements)}, сдвинутых якорей {shifted}, срабатываний {len(hits)}"
        print(f"{name}: {counts}")
        for hit in hits:
            print(f"    {hit['source']['rule_id']} {hit['title']}")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
