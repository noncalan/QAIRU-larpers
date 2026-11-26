"""CLI агента: ``validator ВХОД ВЫХОД [опции]`` (docs/plan.md, «CLI и выходы»).

Коды возврата: 0 проверка выполнена; 1 внутренняя ошибка; 2 ошибка аргументов; 3 вход прочитан
частично; 4 вход не обработать; 5 сбой модели или конфигурации. Найденные дефекты код не меняют.
Сообщения по-русски и без трассировок.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from validator.config import load_config
from validator.models import Backend, CacheMode, ExitCode, Profile, SearchInput
from validator.passport import agent_version
from validator.pipeline import run

OUTPUT_SUFFIXES = (".md", ".pdf")
USAGE = (
    "Использование: validator ВХОД ВЫХОД [опции]. Пример: validator data/input/requirements.pdf out/report.md"
)


class _App(typer.Typer):
    """Typer с ошибками аргументов по-русски и кодом 2 вместо английского текста Click.

    Typer 0.29 несёт свою копию Click внутри (``typer._click``), поэтому ловим публичные
    ``typer.TyperException`` (база ``UsageError``) и ``typer.Abort``, а не ``click.*``.
    """

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        _configure_stdio()
        try:
            result = super().__call__(*args, standalone_mode=False, **kwargs)
        except typer.TyperException as exc:
            code = int(getattr(exc, "exit_code", ExitCode.USAGE))
            typer.echo(f"Ошибка в аргументах: {exc.format_message()}\n{USAGE}", err=True)
            sys.exit(code)
        except typer.Abort:
            typer.echo("Прервано.", err=True)
            sys.exit(ExitCode.INTERNAL)
        except typer.Exit as exc:
            sys.exit(exc.exit_code)
        sys.exit(result if isinstance(result, int) else ExitCode.OK)


def _configure_stdio() -> None:
    """Кириллица в консоли Windows: нечитаемый символ заменяем, а не падаем."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


app = _App(
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Проверка документа требований по ISO/IEC/IEEE 29148 и IEEE 830, отчёт на русском.",
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"QAIRU validator {agent_version()[0]}")
        raise typer.Exit(0)


@app.command()
def main(
    input_path: Annotated[
        Path | None, typer.Argument(metavar="ВХОД", help="Документ: PDF, DOCX или MD")
    ] = None,
    output_path: Annotated[
        Path | None, typer.Argument(metavar="ВЫХОД", help="Файл отчёта: .md (или .pdf)")
    ] = None,
    profile: Annotated[Profile, typer.Option(help="standard и quick: 1 проход; submit: 1 на xhigh")] = (
        Profile.STANDARD
    ),
    passes: Annotated[
        int | None, typer.Option(min=1, max=9, help="Число проходов поиска (3 стабильнее)")
    ] = None,
    backend: Annotated[Backend, typer.Option(help="Как вызывать модель")] = Backend.AUTO,
    replay: Annotated[Path | None, typer.Option(help="Папка run/ прошлого прогона для replay")] = None,
    cache: Annotated[CacheMode, typer.Option(help="Кеш ответов модели")] = CacheMode.USE,
    answers: Annotated[Path | None, typer.Option(help="answers.yaml с ответами автора")] = None,
    ask: Annotated[bool, typer.Option("--ask", help="Задать вопросы автору в терминале")] = False,
    max_budget_usd: Annotated[float | None, typer.Option(min=0.01, help="Стоп по цене прогона, $")] = None,
    pdf_backend: Annotated[
        str | None,
        typer.Option("--pdf-backend", help="Движок PDF: mupdf (по умолчанию) или pdfium (без копилефта)"),
    ] = None,
    search_input: Annotated[
        SearchInput | None,
        typer.Option(
            "--search-input",
            help="Что видит поиск: blocks (документ с ID блоков) или raw (текст без ID, блоки по цитате)",
        ),
    ] = None,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Версия агента")
    ] = False,
) -> None:
    """Проверить документ требований и записать отчёт."""
    del version
    if input_path is None or output_path is None:
        typer.echo(f"Не указаны ВХОД и ВЫХОД.\n{USAGE}", err=True)
        raise typer.Exit(ExitCode.USAGE)
    if output_path.suffix.lower() not in OUTPUT_SUFFIXES:
        typer.echo(f"Файл отчёта должен иметь расширение .md или .pdf: {output_path}", err=True)
        raise typer.Exit(ExitCode.USAGE)
    try:
        cfg = load_config(
            input_path,
            output_path,
            profile=profile,
            passes=passes,
            backend=backend,
            replay_dir=replay,
            cache=cache,
            answers_path=answers,
            ask=ask,
            max_budget_usd=max_budget_usd,
            pdf_backend=pdf_backend,
            search_input=search_input,
        )
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.USAGE) from exc
    outcome = run(cfg)
    typer.echo(outcome.message, err=outcome.exit_code is not ExitCode.OK)
    raise typer.Exit(int(outcome.exit_code))
