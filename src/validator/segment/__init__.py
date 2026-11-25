"""Шаг 2: нарезка документа на требования. Модель указывает блоки, код цитирует.

Контракт: ``segment(doc, llm, cfg) -> Segmentation`` (docs/plan.md, «Архитектура и контракты»).

- ``step``: вызов модели (Sonnet 5.5 ``medium``, JSON по схеме), один повтор, запасной путь без модели;
- ``answer``: схема записей ответа и разбор без доверия к содержимому;
- ``spans``: ID блоков и предложений → фрагменты канонического текста, границы части предложения;
- ``build``: номера ``R-NN`` и ``R-NN.k``, цитата и якорь кодом, локатор, ``stable_key``;
- ``checks``: несуществующие ID, полнота разметки, пересечения, диагностика.
"""

from validator.segment.build import requirement_id
from validator.segment.step import RESPONSE_SCHEMA, build_prompt, fallback_segmentation, segment

__all__ = ["RESPONSE_SCHEMA", "build_prompt", "fallback_segmentation", "requirement_id", "segment"]
