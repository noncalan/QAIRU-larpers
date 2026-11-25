"""Судья eval: промпт, схема, модель и effort из исследования 21 без изменений.

Протокол (docs/plan.md, «Протокол eval и правило D-011»): один судья для агента и базовой линии,
вслепую (судья не знает, чей список), находки идут одинаковым пронумерованным списком
(``evals.findings.render_items``). Вызов идёт через клиент модели агента (``LLMClient``):
подписка ``claude -p`` или фикстуры ``FakeLLM``. Вердикт кешируется на диск по ключу запроса
(хеш модели, effort, системного промпта, промпта и схемы), поэтому повторный подсчёт лимит не тратит.

``JUDGE_SYSTEM``, ``JUDGE_PROMPT``, ``JUDGE_SCHEMA`` и ``FREE_TEXT_PREAMBLE`` дословно совпадают с
``experiments/21-model-effort/run_matrix.py``; тест ``tests/test_eval_parity.py`` это проверяет.
Менять их нельзя: цифры станут несравнимы с исследованием 21 и базовой линией.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from validator.errors import LLMError
from validator.llm.base import LLMClient
from validator.models import Backend, Effort, LLMRequest, LLMResponse, LLMStep

JUDGE_MODEL = "claude-opus-5-5"
"""Opus 5.5 полным именем: в исследовании 21 алиас «opus» на 25.11.2026 означал именно её (D-010)."""
JUDGE_EFFORT = Effort.HIGH
JUDGE_TIMEOUT_S = 2400
"""Таймаут вызова судьи, как в исследовании 21: Opus high на длинном списке идёт несколько минут."""

JUDGE_SYSTEM = "Ты строгий и беспристрастный оценщик качества ревью требований. Отвечай на русском языке."
JUDGE_PROMPT = """Ниже три блока: (1) документ требований, (2) эталонный список дефектов (номер в столбце «#»), (3) находки проверяющего (номер в поле id).

Задача 1. Для КАЖДОГО эталонного дефекта (их 45) реши:
- found: есть находка, которая указывает на то же место документа и ту же суть проблемы;
- partial: находка задевает дефект только частично (половина составного дефекта, упоминание вскользь, то же место, но другая суть);
- missed: такой находки нет.
Укажи номера находок (finding_ids). Совпадение только по теме без совпадения сути не засчитывай.

Задача 2. Для каждой находки, которая не сопоставлена ни с одним эталонным дефектом, реши: real (настоящий дефект документа, которого нет в эталоне) или noise (не дефект, придирка, пожелание, ошибка проверяющего).

Работай только с этими данными."""  # noqa: E501 - текст промпта дословно из исследования 21

JUDGE_SCHEMA: dict[str, Any] = {
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

FREE_TEXT_PREAMBLE = (
    "Ответ проверяющего свободным текстом. Пронумеруй его находки по порядку, "
    "начиная с 1, и используй эти номера как id.\n\n"
)
"""Режим исследования 21 для свободного текста (судья нумерует сам). Только для сверки с прошлыми
замерами (``--judge-format research21``): по протоколу базовая линия идёт тем же списком, что агент."""

DEFAULT_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "judge"


def display_path(path: Path) -> str:
    """Путь для результатов: от текущей папки, если возможно (без путей машины автора)."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


class EvalError(Exception):
    """Ошибка eval с понятным сообщением по-русски (без трассировки в терминале)."""


class GoldStatus(StrEnum):
    FOUND = "found"
    PARTIAL = "partial"
    MISSED = "missed"

    @property
    def mark(self) -> str:
        return {"found": "✓", "partial": "~", "missed": "·"}[self.value]


class ExtraVerdict(StrEnum):
    REAL = "real"
    NOISE = "noise"


class GoldMatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: GoldStatus
    finding_ids: list[int] = Field(default_factory=list)


class JudgeVerdict(BaseModel):
    """Вердикт судьи после проверки кодом: номера эталона и находок в допустимых границах."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gold: dict[int, GoldMatch]
    extra: dict[int, ExtraVerdict] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def matched_findings(self) -> set[int]:
        """Находки, которые судья сопоставил с эталоном (found или partial)."""
        return {i for m in self.gold.values() if m.status is not GoldStatus.MISSED for i in m.finding_ids}


class JudgeCall(BaseModel):
    """Как получен вердикт: живой вызов, кеш или фикстура."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    source: str = Field(description="cli, cache или fake")
    model: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    cache_path: str | None = None


def judge_prompt(document: str, gold_md: str, findings_block: str) -> str:
    """Запрос судье: те же три блока и разделители, что в ``run_matrix.judge`` исследования 21."""
    return (
        JUDGE_PROMPT
        + "\n\n=== (1) ДОКУМЕНТ ===\n"
        + document
        + "\n\n=== (2) ЭТАЛОН ===\n"
        + gold_md
        + "\n\n=== (3) НАХОДКИ ===\n"
        + findings_block
    )


def judge_request(prompt: str, call_index: int = 1) -> LLMRequest:
    return LLMRequest(
        step=LLMStep.JUDGE,
        call_index=call_index,
        model=JUDGE_MODEL,
        effort=JUDGE_EFFORT,
        system=JUDGE_SYSTEM,
        prompt=prompt,
        json_schema=JUDGE_SCHEMA,
        timeout_s=JUDGE_TIMEOUT_S,
    )


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _gold_entries(
    raw: object, gold_ids: Sequence[int], n_findings: int | None, warnings: list[str]
) -> dict[int, GoldMatch]:
    known = set(gold_ids)
    out: dict[int, GoldMatch] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping):
            warnings.append("судья: запись эталона не объект, пропущена")
            continue
        gid = _int(entry.get("id"))
        try:
            status = GoldStatus(str(entry.get("status")))
        except ValueError:
            warnings.append(f"судья: дефект {gid}: неизвестный статус {entry.get('status')!r}, пропущен")
            continue
        if gid not in known:
            warnings.append(f"судья: дефекта {gid} нет в эталоне, запись пропущена")
            continue
        ids = [i for i in (_int(x) for x in entry.get("finding_ids") or []) if i is not None]
        bad = [i for i in ids if i < 1 or (n_findings is not None and i > n_findings)]
        if bad:
            warnings.append(f"судья: дефект {gid}: номера находок вне списка {bad}, убраны")
        if gid in out:
            # Как score.py исследования 21: при повторе действует последняя запись.
            warnings.append(f"судья: дефект {gid} оценён дважды, берём последнюю запись")
        out[gid] = GoldMatch(status=status, finding_ids=sorted({i for i in ids if i not in bad}))
    return out


def _extra_entries(raw: object, n_findings: int | None, warnings: list[str]) -> dict[int, ExtraVerdict]:
    out: dict[int, ExtraVerdict] = {}
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, Mapping):
            warnings.append("судья: запись лишней находки не объект, пропущена")
            continue
        fid = _int(entry.get("finding_id"))
        try:
            verdict = ExtraVerdict(str(entry.get("verdict")))
        except ValueError:
            warnings.append(f"судья: находка {fid}: неизвестный вердикт {entry.get('verdict')!r}, пропущен")
            continue
        if fid is None or fid < 1 or (n_findings is not None and fid > n_findings):
            warnings.append(f"судья: лишняя находка {fid} вне списка, пропущена")
            continue
        out[fid] = verdict
    return out


def parse_verdict(structured: object, gold_ids: Sequence[int], n_findings: int | None) -> JudgeVerdict:
    """Ответ судьи по ``JUDGE_SCHEMA`` → вердикт. Эталонный дефект без записи считается пропущенным.

    ``n_findings``: длина пронумерованного списка; ``None``, если список нумеровал сам судья
    (режим ``research21`` для свободного текста).
    """
    if not isinstance(structured, Mapping) or not isinstance(structured.get("gold"), list):
        raise EvalError("Ответ судьи не соответствует схеме: нет списка gold")
    warnings: list[str] = []
    gold = _gold_entries(structured["gold"], gold_ids, n_findings, warnings)
    missing = [g for g in gold_ids if g not in gold]
    if missing:
        warnings.append(f"судья не оценил дефекты {missing}: считаем их пропущенными")
        gold.update({g: GoldMatch(status=GoldStatus.MISSED) for g in missing})
    extra = _extra_entries(structured.get("extra"), n_findings, warnings)
    return JudgeVerdict(gold=dict(sorted(gold.items())), extra=dict(sorted(extra.items())), warnings=warnings)


class VerdictCache:
    """Ответы судьи на диске: ``<папка>/<ключ запроса>.json``. Ни документа, ни эталона в файле нет."""

    def __init__(self, root: Path = DEFAULT_CACHE_DIR) -> None:
        self.root = root

    def path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, req: LLMRequest) -> LLMResponse | None:
        path = self.path(req.key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("key") != req.key:
                raise EvalError(f"Кеш судьи повреждён: ключ в {path.name} не совпадает с именем файла")
            resp = LLMResponse.model_validate(data["response"])
        except (ValueError, KeyError, ValidationError) as exc:
            raise EvalError(f"Кеш судьи повреждён: {path} ({exc.__class__.__name__})") from exc
        return resp.model_copy(update={"cached": True, "call_id": req.call_id})

    def put(self, req: LLMRequest, resp: LLMResponse, meta: Mapping[str, Any]) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.path(req.key)
        record = {
            "key": req.key,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": req.model,
            "effort": req.effort.value,
            "prompt_chars": len(req.prompt),
            "meta": dict(meta),
            "response": resp.model_dump(mode="json"),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path


class Judge:
    """Достаёт вердикт: сначала кеш, затем клиент модели. Без клиента работает только из кеша.

    Клиент создаётся лениво (``client_factory``): если все вердикты в кеше, ``claude`` не нужен.
    """

    def __init__(self, client_factory: Callable[[], LLMClient] | None, cache: VerdictCache | None) -> None:
        self._factory = client_factory
        self._client: LLMClient | None = None
        self.cache = cache

    @property
    def client(self) -> LLMClient | None:
        if self._client is None and self._factory is not None:
            try:
                self._client = self._factory()
            except LLMError as exc:
                raise EvalError(f"Судья недоступен: {exc.code}: {exc.message}. {exc.hint}".strip()) from exc
        return self._client

    def ask(self, prompt: str, call_index: int, meta: Mapping[str, Any]) -> tuple[object, JudgeCall]:
        req = judge_request(prompt, call_index)
        cached = self.cache.get(req) if self.cache is not None else None
        if cached is not None:
            return cached.structured, JudgeCall(
                key=req.key,
                source="cache",
                model=cached.model,
                cost_usd=cached.cost_usd,
                duration_ms=cached.duration_ms,
                cache_path=display_path(self.cache.path(req.key)) if self.cache else None,
            )
        client = self.client
        if client is None:
            raise EvalError(
                f"Нет вердикта судьи в кеше для {meta.get('run', 'прогона')} (ключ {req.key[:12]}…). "
                "Запустите с --judge cli, чтобы вызвать судью (Opus 5.5 high, несколько минут)"
            )
        try:
            resp = client.call(req)
        except LLMError as exc:
            raise EvalError(f"Судья не ответил: {exc.code}: {exc.message}") from exc
        path = None
        if self.cache is not None and resp.backend is not Backend.FAKE:
            path = display_path(self.cache.put(req, resp, meta))
        source = "fake" if resp.backend is Backend.FAKE else "cli"
        return resp.structured, JudgeCall(
            key=req.key,
            source=source,
            model=resp.model,
            cost_usd=resp.cost_usd,
            duration_ms=resp.duration_ms,
            cache_path=path,
        )
