# 18. Нарезка требований: препроцессинг, эталон нарезки, метрики

Эксперимент к `docs/research/18-requirements-segmentation.md`. Это не код агента.

| Файл | Что делает |
|---|---|
| `preseg.py` | Режет Markdown на блоки с якорями: раздел, строки, тип (абзац, пункт, строка таблицы, код). Таблицы превращает в «Колонка: значение», ловит метки вида `Ф-1`/`REQ-12`, режет на предложения (`razdel`, если установлен) |
| `gold_segmentation.yaml` | Эталонная нарезка `data/input/requirements.md`: 43 обязательных unit, 8 необязательных, 55 обязательных атомов. **Только для eval**, в промпты не класть (CLAUDE.md, правило 2) |
| `seg_eval.py` | Метрики нарезки против эталона: валидность цитат, recall по unit, P/R/F1 по атомам (IoU ≥ 0,5), склейки, «не требования», покрытие строк. `--baselines` прогоняет детерминированные базовые линии |
| `format_anchor_check.py` | Находятся ли цитаты эталона в тексте из PDF (PyMuPDF, pymupdf4llm) и DOCX (markitdown) |
| `guard_signals.py` | Насколько сигналы обязательства (модальные глаголы, инфинитив, числа с единицами, HTTP, стрелки) отделяют блоки с требованиями от остальных |
| `sample_pred_bad.json` | Синтетическое «плохое» предсказание (перефраз, выдумка, потерянный хвост, склейка, чек-лист) для проверки метрик |

Запуск из корня репозитория:

```bash
uv run --group experiments python experiments/18-segmentation/preseg.py data/input/requirements.md
uv run --group experiments python experiments/18-segmentation/seg_eval.py \
    data/input/requirements.md experiments/18-segmentation/gold_segmentation.yaml --baselines
uv run --group experiments python experiments/18-segmentation/seg_eval.py \
    data/input/requirements.md experiments/18-segmentation/gold_segmentation.yaml experiments/18-segmentation/sample_pred_bad.json
uv run --group experiments python experiments/18-segmentation/format_anchor_check.py
```

`seg_eval.py` использует `experiments/03-proto/quote_check.py` (нормализация с картой смещений и rapidfuzz).
