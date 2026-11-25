"""Снапшот сквозного прогона: записанный живой прогон пересобирается бэкендом ``replay`` (issue #36).

В ``tests/fixtures/e2e/<формат>/run/`` лежит папка ``run/`` живого прогона агента через подписку
(``claude -p``, профиль ``standard``) на ``data/input/requirements.<формат>``. Тест запускает CLI одной
командой с ``--backend replay`` и сравнивает ``report.json`` со снапшотом
``tests/fixtures/e2e/<формат>/report.json`` без времени, версии агента и окружения. Так правка кода после
модели (разбор ответа, слияние, верификатор, вопросы, отчёт), которая меняет итог сквозного прогона,
видна в CI на всех ОС, а модель при этом не вызывается.

Если отчёт поменялся намеренно, обновите снапшот и посмотрите его ``git diff`` в PR::

    UPDATE_E2E_SNAPSHOT=1 uv run pytest tests/test_e2e_replay.py

Если поменялся промпт или документ, ответы найдутся только по шагу и номеру вызова (``LLM-016``): это
тоже провал теста. Тогда либо запишите новый живой прогон (``uv run validator data/input/requirements.md
out/report.md`` и скопируйте ``out/run/`` сюда), либо обновите снапшот, чтобы предупреждение вошло в него.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import FORMATS, REQUIREMENTS, ROOT
from test_smoke import REPORT_SECTIONS
from validator.errors import LLM_REPLAY_BY_POSITION
from validator.models import Backend, DocKind, ExitCode, Report, ReportStatus

E2E = ROOT / "tests" / "fixtures" / "e2e"
UPDATE_ENV = "UPDATE_E2E_SNAPSHOT"

VOLATILE = {
    "passport": (
        "started_at",
        "finished_at",
        "duration_s",
        "agent_version",
        "agent_version_source",
        "agent_dirty",
        "os",
        "python",
    ),
    "metrics": ("duration_s",),
}
"""Поля, которые меняются от запуска к запуску или от машины к машине: время, версия агента, окружение."""

MAX_DIFFERENCES_SHOWN = 20
SHORT_VALUE = 300

STALE_RECORDINGS: dict[DocKind, str] = {}
"""Записи, которые отстали от кода и ждут живой перезаписи. Пока запись в этом списке, проверка «каждый
ответ по ключу» для формата помечена как ожидаемо падающая (``strict``): после перезаписи она пройдёт,
и pytest напомнит убрать формат отсюда. Снапшот отчёта сравнивается для всех форматов."""


def _rel(path: Path) -> str:
    """Путь от корня репозитория в записи текущей ОС: так его покажет CLI на Windows и на Linux."""
    return str(path.relative_to(ROOT))


def _normalize(data: dict[str, Any], paths: list[Path]) -> dict[str, Any]:
    """Отчёт без изменчивых полей; пути в текстах в виде с прямой косой чертой, как на Linux."""
    text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    for path in paths:
        native = json.dumps(_rel(path), ensure_ascii=False)[1:-1]
        text = text.replace(native, path.relative_to(ROOT).as_posix())
    normalized: dict[str, Any] = json.loads(text)
    for section, keys in VOLATILE.items():
        for key in keys:
            normalized.get(section, {}).pop(key, None)
    return normalized


def _differences(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Где отчёты разошлись, по одной строке на место: «путь: было → стало». Без этого CI печатает оба
    отчёта целиком (тысячи строк), и само расхождение не попадает в хвост лога."""
    if isinstance(expected, dict) and isinstance(actual, dict):
        out: list[str] = []
        for key in sorted(set(expected) | set(actual)):
            if key not in actual or key not in expected:
                out.append(f"{path}.{key}: {'нет в прогоне' if key not in actual else 'нет в снапшоте'}")
            else:
                out += _differences(expected[key], actual[key], f"{path}.{key}")
        return out[:MAX_DIFFERENCES_SHOWN]
    if isinstance(expected, list) and isinstance(actual, list) and len(expected) == len(actual):
        out = []
        for i, (old, new) in enumerate(zip(expected, actual, strict=True)):
            out += _differences(old, new, f"{path}[{i}]")
        return out[:MAX_DIFFERENCES_SHOWN]
    if expected == actual:
        return []
    return [f"{path}: {_short(expected)} → {_short(actual)}"]


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= SHORT_VALUE else text[:SHORT_VALUE] + "…"


def run_replay(kind: DocKind, out_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    env["PYTHONIOENCODING"] = "utf-8"
    args = [
        _rel(REQUIREMENTS[kind]),
        str(out_dir / "report.md"),
        "--backend",
        "replay",
        "--replay",
        _rel(E2E / kind.value / "run"),
    ]
    return subprocess.run(
        [sys.executable, "-m", "validator", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=ROOT,
        timeout=300,
        check=False,
    )


@pytest.fixture(scope="module", params=FORMATS, ids=lambda k: k.value)
def replayed(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[DocKind, Path, int]:
    kind: DocKind = request.param
    if not (E2E / kind.value / "run" / "responses").is_dir():
        pytest.fail(f"Нет записи живого прогона {kind.value}: tests/fixtures/e2e/{kind.value}/run/")
    out = tmp_path_factory.mktemp(f"e2e-{kind.value}")
    result = run_replay(kind, out)
    assert "Traceback" not in result.stderr, result.stderr
    return kind, out, result.returncode


def test_replay_exits_with_zero(replayed: tuple[DocKind, Path, int]) -> None:
    _, _, code = replayed

    assert code == ExitCode.OK


def test_replay_finds_every_answer_by_request_key(
    replayed: tuple[DocKind, Path, int], request: pytest.FixtureRequest
) -> None:
    """Промпты, документ и модели те же, что в записи: каждый ответ найден по ключу запроса."""
    kind, out, _ = replayed
    if kind in STALE_RECORDINGS:
        request.applymarker(pytest.mark.xfail(reason=STALE_RECORDINGS[kind], strict=True))
    report = Report.model_validate_json((out / "report.json").read_text(encoding="utf-8"))

    assert report.passport.backend_used is Backend.REPLAY
    assert LLM_REPLAY_BY_POSITION not in {d.code for d in report.diagnostics}
    assert report.passport.calls_failed == 0


def test_replayed_report_has_all_sections(replayed: tuple[DocKind, Path, int]) -> None:
    _, out, _ = replayed
    report_md = (out / "report.md").read_text(encoding="utf-8")
    report = Report.model_validate_json((out / "report.json").read_text(encoding="utf-8"))

    assert not [s for s in REPORT_SECTIONS if s not in report_md]
    assert report.status is ReportStatus.COMPLETE
    assert report.segmentation is not None and report.segmentation.requirements
    assert report.verification is not None and report.verification.findings


def test_replayed_report_matches_snapshot(replayed: tuple[DocKind, Path, int]) -> None:
    kind, out, _ = replayed
    actual_path = out / "report.json"
    snapshot_path = E2E / kind.value / "report.json"
    if os.environ.get(UPDATE_ENV):
        shutil.copyfile(actual_path, snapshot_path)
    paths = [REQUIREMENTS[kind], E2E / kind.value / "run"]

    actual = _normalize(json.loads(actual_path.read_text(encoding="utf-8")), paths)
    expected = _normalize(json.loads(snapshot_path.read_text(encoding="utf-8")), paths)

    differences = _differences(expected, actual)

    if differences:
        pytest.fail(
            f"report.json прогона {kind.value} разошёлся со снапшотом {snapshot_path.relative_to(ROOT)} "
            f"(снапшот → прогон):\n" + "\n".join(differences) + "\n"
            f"Если так задумано: {UPDATE_ENV}=1 uv run pytest tests/test_e2e_replay.py",
            pytrace=False,
        )
