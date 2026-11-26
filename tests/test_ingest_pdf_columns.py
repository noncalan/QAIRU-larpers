"""Порядок чтения PDF (#140). Typst собирает документы во временном каталоге без сети."""

from __future__ import annotations

from pathlib import Path

import pytest
import typst

from test_smoke import run_cli
from validator.errors import IN_PDF_COLUMNS_UNCERTAIN, exit_code
from validator.ingest import parse
from validator.ingest.pdf_backends import BACKEND_NAMES
from validator.models import BlockKind, DiagLevel, DocKind, ExitCode

TITLE = "Требования к сервису"
FULL_TITLE = "Требования к регистрации и доставке уведомлений"
REQUIREMENTS = (
    "ФТ-1. Система должна отправлять уведомление клиенту не позднее чем через пять секунд после "
    "проведения операции по карте.",
    "ФТ-2. Система должна хранить историю отправленных уведомлений в течение девяноста календарных дней.",
    "ФТ-3. Клиент может отключить уведомления в настройках мобильного приложения в любой момент.",
    "ФТ-4. Администратор видит журнал ошибок доставки уведомлений с фильтром по дате и типу ошибки.",
    "ФТ-5. При недоступности сервиса push система повторяет отправку три раза с интервалом одна минута.",
    "ФТ-6. Текст уведомления содержит сумму операции и последние четыре цифры номера карты.",
)


def make_pdf(tmp_path: Path, source: str) -> Path:
    path = tmp_path / "layout.pdf"
    typ = path.with_suffix(".typ")
    typ.write_text('#set page(paper: "a4")\n#set text(lang: "ru", size: 10pt)\n' + source, encoding="utf-8")
    typst.compile(typ, output=path)
    return path


@pytest.mark.parametrize("backend", BACKEND_NAMES)
@pytest.mark.parametrize("full_title", [False, True], ids=["issue-140", "spanning-heading"])
def test_columns_preserve_complete_requirements(tmp_path: Path, backend: str, full_title: bool) -> None:
    body = "\n\n".join([*REQUIREMENTS[:3], "#colbreak()", *REQUIREMENTS[3:]])
    title = FULL_TITLE if full_title else TITLE
    source = (
        f"= {title}\n\n#columns(2)[{body}]" if full_title else f"#set page(columns: 2)\n= {title}\n\n{body}"
    )
    doc = parse(make_pdf(tmp_path, source), DocKind.PDF, pdf_backend=backend)

    assert doc.blocks[0].kind is BlockKind.HEADING
    assert [" ".join(b.text.split()) for b in doc.blocks] == [title, *REQUIREMENTS]
    assert " ".join(doc.text.split()) == " ".join((title, *REQUIREMENTS))
    anchors = [b.anchor.page_line for b in doc.blocks]
    assert all(b.anchor.page == 1 for b in doc.blocks)
    assert anchors == sorted(set(anchors))
    assert exit_code(doc.diagnostics) == ExitCode.OK


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_single_column_keeps_paragraphs_and_anchors(tmp_path: Path, backend: str) -> None:
    lines = ["Система должна сохранять запись.", "Оператор может просмотреть запись."]
    doc = parse(make_pdf(tmp_path, "\n\n#v(20pt)\n\n".join(lines)), DocKind.PDF, pdf_backend=backend)

    assert doc.text == "\n\n".join(lines)
    assert [(b.text, b.anchor.page_line) for b in doc.blocks] == [(lines[0], 1), (lines[1], 2)]
    assert exit_code(doc.diagnostics) == ExitCode.OK


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_table_gutter_keeps_rows_and_cells(tmp_path: Path, backend: str) -> None:
    rows = [("Январь", "Создать"), ("Февраль", "Изменить"), ("Март", "Повторить"), ("Апрель", "Удалить")]
    cells = ", ".join(f"[{month}], [{action}]" for month, action in rows)
    source = f"#table(columns: (1fr, 1fr), stroke: none, [Месяц], [Действие], table.hline(), {cells})"
    doc = parse(make_pdf(tmp_path, source), DocKind.PDF, pdf_backend=backend)

    parsed = [b for b in doc.blocks if b.kind is BlockKind.TABLE_ROW]
    assert [tuple(c.text for c in b.cells) for b in parsed] == rows
    assert all(b.anchor.table == 1 for b in parsed)
    assert exit_code(doc.diagnostics) == ExitCode.OK


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_narrow_gutter_warns_on_justified_columns(tmp_path: Path, backend: str) -> None:
    body = "\n\n".join([*REQUIREMENTS[:3], "#colbreak()", *REQUIREMENTS[3:]])
    path = make_pdf(tmp_path, f"#set par(justify: true)\n#columns(2, gutter: 8pt)[{body}]")
    doc = parse(path, DocKind.PDF, pdf_backend=backend)

    assert any(d.code == IN_PDF_COLUMNS_UNCERTAIN for d in doc.diagnostics)
    assert exit_code(doc.diagnostics) == ExitCode.PARTIAL_INPUT


def test_interrupted_gutter_reports_partial_input(tmp_path: Path) -> None:
    # Полноширинная строка попала между двумя областями с общим зазором.
    columns = "#columns(2)[" + "\n\n".join([*REQUIREMENTS[:2], "#colbreak()", *REQUIREMENTS[3:5]]) + "]"
    path = make_pdf(tmp_path, f"{columns}\n\n= {FULL_TITLE}\n\n{columns}")
    for backend in BACKEND_NAMES:
        doc = parse(path, DocKind.PDF, pdf_backend=backend)
        warning = next(d for d in doc.diagnostics if d.code == IN_PDF_COLUMNS_UNCERTAIN)
        assert warning.level is DiagLevel.WARNING and warning.location == "стр. 1"
        assert "одну колонку" in warning.hint
        assert FULL_TITLE in doc.text

    out = tmp_path / "report.md"
    assert run_cli(path, out, "--backend", "fake").returncode == ExitCode.PARTIAL_INPUT
    report = out.read_text(encoding="utf-8")
    assert "Статус входа | ЧАСТИЧНО" in report and IN_PDF_COLUMNS_UNCERTAIN in report


GLOSSARY = (
    ("Читательский билет", "Документ, по которому читатель получает книги на дом и в читальном зале."),
    ("Абонемент на дом", "Отдел, где книги выдают на срок до тридцати дней с правом одного продления."),
    ("Электронный каталог фонда", "Поиск изданий по автору, названию и теме с наличием в каждом филиале."),
    ("Бронирование издания", "Запрос отложить экземпляр на три рабочих дня, пока читатель не придёт."),
    ("Продление срока выдачи", "Перенос даты возврата на две недели, если на издание нет очереди."),
    ("Штраф за просрочку возврата", "Плата за каждый день задержки, начисляется при возврате издания."),
)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_borderless_glossary_is_not_read_as_columns(tmp_path: Path, backend: str) -> None:
    """Таблица без линеек «термин | определение»: читать её колонками значит отделить термины от определений.

    Ячейки разной высоты: напротив хвоста определения в колонке терминов пусто. Разбор оставляет строки
    как раньше (термин рядом со своим определением) и честно предупреждает.
    """
    cells = ", ".join(f"[{term}], [{meaning}]" for term, meaning in GLOSSARY)
    grid = f"#grid(columns: (1fr, 2fr), column-gutter: 1.5em, row-gutter: 0.8em, {cells})"
    doc = parse(make_pdf(tmp_path, f"= Термины\n\n{grid}"), DocKind.PDF, pdf_backend=backend)

    order = [doc.text.index(word) for term, meaning in GLOSSARY for word in (term, meaning.split()[0])]
    assert order == sorted(order)
    assert any(d.code == IN_PDF_COLUMNS_UNCERTAIN for d in doc.diagnostics)
    assert exit_code(doc.diagnostics) == ExitCode.PARTIAL_INPUT


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_long_justified_columns_are_read_column_by_column(tmp_path: Path, backend: str) -> None:
    """Абзацы разной длины в двух колонках: промежутки между абзацами слева и справа на разной высоте.

    Пустые места напротив есть в обеих колонках, поэтому это колонки текста, а не таблица без линеек:
    каждый абзац читается целиком, без строк соседней колонки.
    """
    paragraphs = [" ".join(REQUIREMENTS[(i + j) % 6] for j in range(1 + (i * 7) % 3)) for i in range(16)]
    # Без переносов слов: pdfium оставляет дефис переноса в склеенной строке («ин-тервалом»), к колонкам это
    # отношения не имеет.
    head = "#set page(columns: 2)\n#set par(justify: true)\n#set text(hyphenate: false)\n= Требования\n\n"
    source = head + "\n\n".join(paragraphs)
    doc = parse(make_pdf(tmp_path, source), DocKind.PDF, pdf_backend=backend)

    text = " ".join(doc.text.split())
    assert all(p in text for p in paragraphs)
    assert exit_code(doc.diagnostics) == ExitCode.OK
