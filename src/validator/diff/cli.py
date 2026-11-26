"""CLI сравнения версий: ``validator-diff СТАРЫЙ.json НОВЫЙ.json ВЫХОД.md [--json ВЫХОД.json]``.

Коды возврата как у ``validator``: 0 сравнение сделано; 2 ошибка аргументов; 4 отчёт или документ не
прочитать. Разница между версиями код не меняет: решение «пускать или нет» принимает CI по JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from validator.diff.load import ReportReadError, load_document_text, load_report
from validator.diff.match import compare
from validator.diff.render import render_json, render_markdown, totals
from validator.models import ExitCode

USAGE = (
    "Использование: validator-diff СТАРЫЙ.json НОВЫЙ.json ВЫХОД.md [--json ВЫХОД.json] "
    "[--old-doc СТАРЫЙ_ДОКУМЕНТ --new-doc НОВЫЙ_ДОКУМЕНТ]"
)


class _App(typer.Typer):
    """Ошибки аргументов по-русски и с кодом 2, как у ``validator`` (``validator.cli._App``)."""

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure is not None:
                reconfigure(errors="replace")
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


app = _App(
    add_completion=False,
    pretty_exceptions_enable=False,
    help="Сравнение двух версий документа по отчётам validator: что исправлено, что осталось, что нового.",
)


def _fail(code: ExitCode, message: str, hint: str = "") -> typer.Exit:
    typer.echo(f"{message}. {hint}".rstrip(". ") + ("." if hint else ""), err=True)
    return typer.Exit(int(code))


def _write(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise _fail(ExitCode.USAGE, f"Не удалось записать {path}: {exc.strerror}", "Проверьте путь") from exc


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve() or a.samefile(b)
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError) as exc:
        raise _fail(ExitCode.USAGE, "Не удалось проверить совпадение путей", str(exc)) from exc


@app.command()
def main(
    old_report: Annotated[
        Path | None, typer.Argument(metavar="СТАРЫЙ.json", help="report.json старой версии")
    ] = None,
    new_report: Annotated[
        Path | None, typer.Argument(metavar="НОВЫЙ.json", help="report.json новой версии")
    ] = None,
    output: Annotated[Path | None, typer.Argument(metavar="ВЫХОД.md", help="Отчёт сравнения, .md")] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Машинный результат сравнения, .json")
    ] = None,
    old_doc: Annotated[
        Path | None, typer.Option("--old-doc", help="Старая версия документа: точнее видно, что правили")
    ] = None,
    new_doc: Annotated[Path | None, typer.Option("--new-doc", help="Новая версия документа")] = None,
) -> None:
    """Сравнить отчёты двух версий документа и записать отчёт сравнения."""
    if old_report is None or new_report is None or output is None:
        raise _fail(ExitCode.USAGE, f"Не указаны СТАРЫЙ.json, НОВЫЙ.json и ВЫХОД.md.\n{USAGE}")
    if output.suffix.lower() != ".md":
        raise _fail(ExitCode.USAGE, f"Отчёт сравнения должен иметь расширение .md: {output}")
    if json_out is not None and json_out.suffix.lower() != ".json":
        raise _fail(ExitCode.USAGE, f"Файл --json должен иметь расширение .json: {json_out}")
    if (old_doc is None) != (new_doc is None):
        raise _fail(ExitCode.USAGE, "Укажите обе версии документа (--old-doc и --new-doc) или ни одной")
    inputs = [path for path in (old_report, new_report, old_doc, new_doc) if path is not None]
    if json_out is not None and _same_path(output, json_out):
        raise _fail(ExitCode.USAGE, "Укажите разные файлы для отчётов Markdown и JSON")
    for target in (path for path in (output, json_out) if path is not None):
        if any(_same_path(target, source) for source in inputs):
            raise _fail(ExitCode.USAGE, f"Выходной файл {target} совпадает с входным")
    try:
        old, new = load_report(old_report), load_report(new_report)
        texts = (load_document_text(old_doc), load_document_text(new_doc)) if old_doc and new_doc else None
    except ReportReadError as exc:
        raise _fail(ExitCode.INPUT, exc.message, exc.hint) from exc
    result = compare(old, new, *(texts or (None, None)))
    _write(output, render_markdown(result))
    if json_out is not None:
        _write(json_out, json.dumps(render_json(result), ensure_ascii=False, indent=2) + "\n")
    t = totals(result)
    typer.echo(
        f"Сравнение записано в {output}. Исправлено {t.fixed} из {t.old_findings} (критичных "
        f"{t.fixed_critical} из {t.old_critical}), осталось {t.remained}, новых на изменённых местах "
        f"{t.regressions}."
    )
    for warning in result.warnings:
        typer.echo(f"Предупреждение: {warning}", err=True)
