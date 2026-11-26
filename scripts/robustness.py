"""Устойчивость агента: отчёты на битых файлах, сбоях модели и неудобных документах (issue #44).

    uv run python scripts/robustness.py                       # все случаи → reports/robustness/
    uv run python scripts/robustness.py --only model-limit    # один или несколько случаев
    uv run python scripts/robustness.py --record injection    # живой прогон (claude -p), запись для replay

Четыре группы случаев (``CASES``):

- ``broken-*``: 10 битых файлов из ``tests/fixtures/broken/`` (zip-бомба собирается на лету и в репозиторий
  не попадает). Модель до них не доходит, поэтому бэкенд ``fake``;
- ``partial-*``: вход с оговорками, код 3 (#126): PDF обрезан, но страницы спасены; страница без текста;
  пустой шаблон; текст не похож на требования. Проверка идёт, модель подменена бэкендом ``fake``;
- ``model-*``: сбои модели на обычном документе. Подменён только процесс ``claude`` (``robustness_sim.py``),
  остальной путь агента настоящий;
- ``injection``, ``english``, ``mixed-language``: документы с командой модели внутри, на английском и на двух
  языках сразу. Ответы модели лежат в ``tests/fixtures/robustness/<случай>/run`` (записаны живым прогоном
  профиля quick) и проигрываются без модели; ``--record`` записывает их заново.

На выходе ``reports/robustness/<случай>/report.md`` и сводка ``reports/robustness/README.md``. Код возврата
скрипта 0, если у всех случаев совпали код возврата и ожидаемая диагностика.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT)]

import broken_factory  # noqa: E402  (фабрика битых файлов лежит в tests/)
from scripts.robustness_sim import FAILURES, ModelFailure, simulated_claude  # noqa: E402
from validator.config import load_config  # noqa: E402
from validator.models import Backend, CacheMode, DiagLevel, Diagnostic, Profile, RunOutcome  # noqa: E402
from validator.pipeline import run  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "robustness"
REPORTS = ROOT / "reports" / "robustness"
BROKEN_FILES = (
    "empty.pdf",
    "truncated_hard.pdf",
    "password.pdf",
    "scan.pdf",
    "garbled_text.pdf",
    "image.pdf",
    "truncated.docx",
    "password.docx",
    "binary.md",
    "zip_bomb.docx",
)
PARTIAL_FILES = ("truncated.pdf", "mixed_scan.pdf", "headings_only.md", "not_requirements.md")
"""Вход с оговорками (код 3, #126): прочитан частично, без текста на странице, пустой шаблон, не ТЗ."""
INJECTION_CODE = "IN-082"


@dataclass(frozen=True)
class Case:
    """Один случай: что подаём, какой код возврата и какую диагностику ждём."""

    slug: str
    group: str
    title: str
    source: Callable[[Path], Path]
    exit_code: int
    code: str | None
    failure: ModelFailure | None = None
    recorded: bool = False
    profile: Profile = Profile.QUICK
    notes: list[str] = field(default_factory=list)


def _fixture(name: str) -> Callable[[Path], Path]:
    return lambda _work: FIXTURES / name


def _broken(name: str) -> Callable[[Path], Path]:
    def make(work: Path) -> Path:
        path = work / name
        path.write_bytes(broken_factory.CASES[name].make())
        return path

    return make


def build_cases() -> list[Case]:
    cases = [
        Case(
            f"broken-{name.replace('.', '-')}",
            "Битые файлы",
            f"{name}: {broken_factory.CASES[name].what}",
            _broken(name),
            broken_factory.CASES[name].exit_code,
            broken_factory.CASES[name].code,
        )
        for name in BROKEN_FILES
    ]
    cases += [
        Case(
            f"partial-{name.replace('.', '-')}",
            "Вход с оговорками",
            f"{name}: {broken_factory.CASES[name].what}",
            _broken(name),
            broken_factory.CASES[name].exit_code,
            broken_factory.CASES[name].code,
        )
        for name in PARTIAL_FILES
    ]
    cases += [
        Case(f.slug, "Сбои модели", f.title, _fixture("compact.md"), 5, f.code, failure=f) for f in FAILURES
    ]
    cases += [
        Case(
            "injection",
            "Неудобные документы",
            "Команды модели внутри документа: «игнорируй инструкции», строка system:, поддельная метка",
            _fixture("injection.md"),
            0,
            INJECTION_CODE,
            recorded=True,
        ),
        Case(
            "english",
            "Неудобные документы",
            "Документ на английском языке",
            _fixture("english.md"),
            0,
            None,
            recorded=True,
        ),
        Case(
            "mixed-language",
            "Неудобные документы",
            "Русский и английский вперемешку",
            _fixture("mixed.md"),
            0,
            None,
            recorded=True,
        ),
    ]
    return cases


@dataclass
class Result:
    case: Case
    outcome: RunOutcome
    report_md: Path
    problems: list[str]

    @property
    def diagnostics(self) -> list[Diagnostic]:
        return _all_diagnostics(self.outcome)

    @property
    def headline(self) -> Diagnostic | None:
        """Диагностика, ради которой случай существует; иначе первая по важности."""
        found = [d for d in self.diagnostics if d.code == self.case.code]
        if found:
            return found[0]
        if self.case.code is None:  # ждали чистый прогон: заметка о записи не диагностика, она не в счёт
            return next((d for d in self.diagnostics if d.level is not DiagLevel.INFO), None)
        order = {DiagLevel.ERROR: 0, DiagLevel.WARNING: 1, DiagLevel.INFO: 2}
        ranked = sorted(self.diagnostics, key=lambda d: order[d.level])
        return ranked[0] if ranked else None


def _backend(case: Case, record: bool) -> Backend:
    if case.recorded:
        return Backend.CLI if record else Backend.REPLAY
    return Backend.AUTO if case.failure else Backend.FAKE


def run_case(case: Case, work: Path, *, record: bool = False) -> Result:
    """Прогоняет один случай целиком: тот же ``pipeline.run``, что и у команды ``validator``."""
    folder = work / case.slug
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "report.md"
    cfg = load_config(
        case.source(folder),
        out,
        profile=case.profile,
        backend=_backend(case, record),
        replay_dir=(FIXTURES / case.slug / "run").relative_to(ROOT) if case.recorded and not record else None,
        # Записи injection, english и mixed-language сделаны до правок промптов (в отчётах LLM-016): пока их
        # не перезаписали, они проигрываются по шагу и номеру вызова, как раньше (#132, флаг разработки).
        replay_by_position=case.recorded and not record,
        cache=CacheMode.USE if record else CacheMode.OFF,
        # Без распознавания сканов (#38): отчёты не зависят от того, стоит ли на машине Tesseract.
        ocr=False,
    )
    cfg = cfg.model_copy(update={"has_api_key": False if not record else cfg.has_api_key})
    with contextlib.chdir(ROOT), simulated_claude(case.failure) if case.failure else contextlib.nullcontext():
        outcome = run(cfg)
    problems = check(case, outcome, record=record)
    return Result(case, outcome, out, problems)


def _all_diagnostics(outcome: RunOutcome) -> list[Diagnostic]:
    report = outcome.report
    return [*report.input_diagnostics, *report.diagnostics] if report else outcome.diagnostics


def check(case: Case, outcome: RunOutcome, *, record: bool = False) -> list[str]:
    problems: list[str] = []
    if int(outcome.exit_code) != case.exit_code:
        problems.append(f"код возврата {int(outcome.exit_code)}, ожидали {case.exit_code}")
    report = outcome.report
    codes = {d.code for d in _all_diagnostics(outcome)}
    if case.code and case.code not in codes:
        problems.append(
            f"в отчёте нет диагностики {case.code} (есть: {', '.join(sorted(codes)) or 'ничего'})"
        )
    if record:  # в запись для replay не должны попасть упавшие вызовы: ответа на них в записи не будет
        failed = sorted(
            {
                d.code
                for d in _all_diagnostics(outcome)
                if d.code.startswith("LLM") and d.level is not DiagLevel.INFO
            }
        )
        if failed:
            problems.append(f"в живом прогоне сбои модели ({', '.join(failed)}): запись не годится")
    if report is None or outcome.report_path is None or not outcome.report_path.is_file():
        problems.append("отчёт не записан")
    return problems


def _replace_recording(new: Path, target: Path) -> None:
    """Запись заменяется целиком и только после успешного копирования: старая не пропадёт при сбое."""
    staging = target.with_name(target.name + ".new")
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(new, staging)
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def write_summary(results: list[Result], target: Path, *, version: str) -> None:
    """``reports/robustness/README.md``: таблица «случай → код → что в отчёте»."""
    lines = [
        "# Устойчивость агента: отчёты на сбоях",
        "",
        "Каждая строка это один прогон агента той же командой, что и обычная проверка, и отчёт, который "
        "он записал. Ни один случай не кончается трассировкой: агент пишет отчёт с причиной и подсказкой, "
        "а код возврата говорит, что случилось: 0 проверка выполнена, 3 вход прочитан с оговорками "
        "(частично, без текста на части страниц, пустой шаблон или не похож на требования), "
        "4 вход не обработать, 5 сбой модели. Как это устроено: раздел «Устойчивость» в "
        "[README](../../README.md).",
        "",
        f"Версия агента: `{version}`. Собрано {datetime.now(UTC):%d.%m.%Y %H:%M} UTC командой "
        "`uv run python scripts/robustness.py`.",
        "",
        "Прогоны идут без распознавания сканов (`ocr=False`), чтобы отчёты не зависели от машины: с "
        "Tesseract и русским языком агент распознаёт `scan.pdf` сам и проверяет его с кодом 3 (#38).",
    ]
    for group in dict.fromkeys(r.case.group for r in results):
        lines += [
            "",
            f"## {group}",
            "",
            "| Случай | Что подали | Код | Диагностика | Что в отчёте |",
            "|---|---|---|---|---|",
        ]
        for r in (x for x in results if x.case.group == group):
            head = r.headline
            what = f"`{head.code}` {_cell(head.message)}" if head else "предупреждений и ошибок нет"
            action = _cell(head.action) if head and head.action else "Полный отчёт: все разделы заполнены"
            link = f"[{r.case.slug}/report.md]({r.case.slug}/report.md)"
            lines.append(
                f"| {link} | {_cell(r.case.title)} | {int(r.outcome.exit_code)} | {what} | {action} |"
            )
    lines.append("")
    target.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    """Прогоняет случаи; журнал приглушён (подробности в ``report.log``) и потом возвращается."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        return _main(argv)
    finally:
        logging.disable(previous)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out", type=Path, default=REPORTS, help="Папка отчётов (по умолчанию reports/robustness)"
    )
    parser.add_argument("--only", nargs="+", metavar="СЛУЧАЙ", help="Только эти случаи")
    parser.add_argument("--record", nargs="+", metavar="СЛУЧАЙ", help="Живой прогон и запись ответов модели")
    args = parser.parse_args(argv)
    wanted = set(args.only or []) | set(args.record or [])
    cases = [c for c in build_cases() if not wanted or c.slug in wanted]
    unknown = wanted - {c.slug for c in cases}
    if unknown:
        parser.error(f"нет таких случаев: {', '.join(sorted(unknown))}")
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="qairu-robustness-") as tmp:
        work = Path(tmp)
        for case in cases:
            record = case.slug in (args.record or [])
            result = run_case(case, work, record=record)
            results.append(result)
            mark = "ok " if not result.problems else "ОШИБКА"
            print(f"{mark} {case.slug:34} код {int(result.outcome.exit_code)} {'; '.join(result.problems)}")
            if record and not result.problems:
                _replace_recording(work / case.slug / "run", FIXTURES / case.slug / "run")
        out: Path = args.out
        for r in results:
            target = out / r.case.slug
            target.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(r.report_md, target / "report.md")
        if not wanted:
            version = results[0].outcome.report.passport.agent_version if results[0].outcome.report else "?"
            write_summary(results, out / "README.md", version=version)
    return 1 if any(r.problems for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
