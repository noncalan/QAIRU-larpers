"""Разбор PDF (шаг 1, #26): порядок чтения, якоря страниц, колонтитулы, заголовки, признаки качества.

Один и тот же набор проверок идёт на двух движках (``mupdf`` и ``pdfium``): общий код раскладки должен
давать один и тот же текст. Маленькие PDF собираем на лету латиницей: кириллического шрифта в CI может
не быть.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pikepdf
import pymupdf
import pytest
from pydantic import ValidationError

from conftest import REQUIREMENTS
from test_smoke import run_cli
from validator.config import load_config
from validator.errors import IN_FURNITURE_REMOVED, IN_PDF_BROKEN, IN_PDF_ENCRYPTED, InputError
from validator.ingest import parse
from validator.ingest.pdf_backends import BACKEND_NAMES, _combine_surrogates, get_backend
from validator.ingest.pdf_layout import join_hyphenation
from validator.models import BlockKind, DiagLevel, DocKind, ParsedDocument, RunConfig
from validator.text import locate_quote

PDF = REQUIREMENTS[DocKind.PDF]
MD = REQUIREMENTS[DocKind.MD]
MIN_SHARED_WORDS = 0.95
WORD = re.compile(r"[А-Яа-яЁёA-Za-z]{3,}")
Item = tuple[float, str, float, str]  # y базовой линии, текст, кегль, шрифт

# Фразы документа и страница, на которой их видит человек (проверено по самому PDF, не по агенту).
PHRASES = {
    "Дать клиенту возможность узнать о достижении личного бюджета": 1,
    "Хранить идентификатор карты, месяц, идентификатор операции": 2,
    "Приложение читает и сохраняет настройки через дочерний документ": 2,
    "Проверить принадлежность карты клиенту": 6,
    "Тело не используется": 7,
    "Ошибка сервиса": 8,
}


@pytest.fixture(scope="module", params=BACKEND_NAMES)
def pdf_doc(request: pytest.FixtureRequest) -> ParsedDocument:
    return parse(PDF, DocKind.PDF, pdf_backend=request.param)


def make_pdf(path: Path, pages: list[list[Item]]) -> Path:
    with pymupdf.open() as doc:
        for items in pages:
            page = doc.new_page()
            for y, text, size, font in items:
                page.insert_text((72, y), text, fontsize=size, fontname=font)
        doc.save(path)
    return path


def body(y: float, text: str) -> Item:
    return (y, text, 11, "helv")


def words(text: str) -> set[str]:
    return {w.lower() for w in WORD.findall(text)}


# ---------------------------------------------------------------- requirements.pdf


def test_pages_and_extraction_note(pdf_doc: ParsedDocument) -> None:
    assert pdf_doc.pages == 8
    assert pdf_doc.extraction
    assert pdf_doc.signals is not None and pdf_doc.signals.pages_without_text == 0


def test_text_shares_words_with_markdown_source(pdf_doc: ParsedDocument) -> None:
    source = words(MD.read_text(encoding="utf-8"))

    shared = len(words(pdf_doc.text) & source) / len(source)

    assert shared >= MIN_SHARED_WORDS


def test_no_space_before_punctuation(pdf_doc: ParsedDocument) -> None:
    """Знаки препинания в PDF лежат отдельными фрагментами: «в <город> .» в тексте быть не должно."""
    assert not re.findall(r"\S [.,;:!?)»]", pdf_doc.text)
    assert not re.findall(r"[(«] \S", pdf_doc.text)
    assert "Ф-1." in pdf_doc.text and "Бизнес-процесс" in pdf_doc.text


@pytest.mark.parametrize("phrase", PHRASES, ids=lambda p: p[:30])
def test_anchor_points_to_the_right_page(pdf_doc: ParsedDocument, phrase: str) -> None:
    quote = locate_quote(pdf_doc, phrase)

    assert quote.anchor is not None, phrase
    assert quote.anchor.page == PHRASES[phrase]


def test_every_block_has_page_and_line_on_page(pdf_doc: ParsedDocument) -> None:
    for block in pdf_doc.blocks:
        assert block.anchor.page is not None and block.anchor.page_line is not None, block.id
    first_on_page = {b.anchor.page: b.anchor.page_line for b in reversed(pdf_doc.blocks)}
    assert first_on_page[1] == 1  # заголовок документа это первая строка первой страницы


def test_headings_match_the_markdown_structure(pdf_doc: ParsedDocument) -> None:
    """Уровни заголовков берутся из кегля, жирности и курсива PDF и совпадают с исходным Markdown."""
    md = parse(MD, DocKind.MD)

    def shape(doc: ParsedDocument) -> list[tuple[int, str]]:
        return [
            (len(b.section), b.text.lstrip("# ").strip()) for b in doc.blocks if b.kind is BlockKind.HEADING
        ]

    assert shape(pdf_doc) == shape(md)


def test_wrapped_list_item_is_one_block(pdf_doc: ParsedDocument) -> None:
    item = next(b for b in pdf_doc.blocks if b.kind is BlockKind.LIST_ITEM and "Ф-1." in b.text)

    assert "1 000 000 ₸ включительно" in " ".join(item.text.split())
    assert item.source_id == "Ф-1"


def test_two_backends_give_the_same_canonical_text() -> None:
    texts = {name: parse(PDF, DocKind.PDF, pdf_backend=name).text for name in BACKEND_NAMES}

    assert texts["mupdf"] == texts["pdfium"]


# ---------------------------------------------------------------- колонтитулы, абзацы, переносы


def furnished_pages() -> list[list[Item]]:
    """Пять страниц: шапка «Acme Confidential v1.2» и подвал «Page N of 5», предложение через стык."""
    pages: list[list[Item]] = []
    for n in range(1, 6):
        items: list[Item] = [
            (40, "Acme Confidential v1.2", 9, "helv"),
            (800, f"Page {n} of 5", 9, "helv"),
            body(150, f"The service shall store record number {n} for the user."),
            body(164, "The service shall confirm the record to the caller."),
            body(700, "The audit trail must be kept for all"),
        ]
        items.insert(0, body(125, "operations of the user in the system.") if n > 1 else body(125, "Scope"))
        pages.append(items)
    return pages


@pytest.fixture(scope="module", params=BACKEND_NAMES)
def furnished(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> ParsedDocument:
    path = make_pdf(tmp_path_factory.mktemp("furnished") / "f.pdf", furnished_pages())
    return parse(path, DocKind.PDF, pdf_backend=request.param)


def test_headers_and_page_numbers_do_not_reach_the_text(furnished: ParsedDocument) -> None:
    assert "Acme Confidential" not in furnished.text
    assert not re.search(r"Page \d of 5", furnished.text)
    assert "The service shall store record number 3" in furnished.text


def test_removed_furniture_is_reported_as_info(furnished: ParsedDocument) -> None:
    info = [d for d in furnished.diagnostics if d.code == IN_FURNITURE_REMOVED]

    assert len(info) == 1 and info[0].level is DiagLevel.INFO
    assert "Acme Confidential" in info[0].message and "5 стр." in info[0].message


def test_sentence_continues_over_page_break_in_one_block(furnished: ParsedDocument) -> None:
    quote = locate_quote(furnished, "The audit trail must be kept for all operations of the user")

    assert quote.anchor is not None
    assert len(quote.anchor.block_ids) == 1
    assert quote.anchor.page == 1


def test_page_numbers_alone_are_furniture_even_in_a_short_document(tmp_path: Path) -> None:
    path = make_pdf(
        tmp_path / "two.pdf",
        [
            [body(120, "The system shall work."), (800, "- 1 -", 9, "helv")],
            [body(120, "It shall also log."), (800, "2", 9, "helv")],
        ],
    )

    doc = parse(path, DocKind.PDF)

    assert "- 1 -" not in doc.text and doc.text.splitlines()[-1] == "It shall also log."


def test_heading_by_bold_and_size_with_levels(tmp_path: Path) -> None:
    lines = [
        body(150 + 14 * k, f"The system shall support operation number {k} for the customer.")
        for k in range(5)
    ]
    path = make_pdf(
        tmp_path / "h.pdf",
        [
            [
                (100, "Requirements Document", 18, "hebo"),
                (125, "Payments", 11, "hebo"),
                *lines,
                (260, "Not a heading, it is an ordinary short sentence.", 11, "helv"),
                (290, "Limits", 11, "hebo"),
                body(310, "The limit shall be configurable by the operator of the service."),
            ]
        ],
    )

    doc = parse(path, DocKind.PDF)
    headings = [(len(b.section), b.text) for b in doc.blocks if b.kind is BlockKind.HEADING]

    assert headings == [(1, "Requirements Document"), (2, "Payments"), (2, "Limits")]
    assert doc.blocks[-1].section == ["Requirements Document", "Limits"]


def test_numbered_list_item_wrapped_over_lines_stays_one_block(tmp_path: Path) -> None:
    path = make_pdf(
        tmp_path / "list.pdf",
        [
            [
                body(120, "1. The system shall accept an amount from 1 to"),
                body(134, "1000 units inclusive."),
                body(148, "2. The system shall reject other amounts."),
            ]
        ],
    )

    items = [b for b in parse(path, DocKind.PDF).blocks if b.kind is BlockKind.LIST_ITEM]

    assert [b.ordinal for b in items] == [1, 2]
    assert "1000 units inclusive." in items[0].text


@pytest.mark.parametrize(
    ("prev", "cur", "expected"),
    [
        ("передава-", "ть данные", "передавать данные"),
        ("un-", "acknowledged", "unacknowledged"),
        ("бизнес-", "требование", "бизнес-требование"),
        ("web-", "service", "web-service"),
        ("слово­", "продолжение", "словопродолжение"),
        ("Ф-", "1. текст", None),
        ("обычная строка", "продолжение", None),
        ("диапазон 1-", "10", None),
        ("по-", "другому", "по-другому"),
        ("из-", "за ошибки", "из-за ошибки"),
        ("что-", "то", "что-то"),
        ("Введите SMS-", "сообщение", "Введите SMS-сообщение"),
        ("ключ API-", "ключ", "ключ API-ключ"),
    ],
)
def test_hyphenation_join(prev: str, cur: str, expected: str | None) -> None:
    assert join_hyphenation(prev, cur) == expected


def test_hyphenated_word_is_glued_in_the_text(tmp_path: Path) -> None:
    path = make_pdf(
        tmp_path / "hy.pdf",
        [[body(120, "The gateway must retransmit un-"), body(134, "acknowledged messages after a timeout.")]],
    )

    doc = parse(path, DocKind.PDF)

    assert "retransmit unacknowledged\nmessages after a timeout." in doc.text
    assert locate_quote(doc, "retransmit unacknowledged messages").anchor is not None


# ---------------------------------------------------------------- признаки качества текста


def damaged_copies(tmp_path: Path) -> dict[str, Path]:
    """Скан (страницы картинками), мусорный текстовый слой (нет ToUnicode), битая таблица xref."""
    out: dict[str, Path] = {}
    with pymupdf.open(PDF) as src, pymupdf.open() as scan:
        for page in list(src)[:3]:
            pix = page.get_pixmap(dpi=40, colorspace=pymupdf.csGRAY)
            sheet = scan.new_page(width=page.rect.width, height=page.rect.height)
            sheet.insert_image(sheet.rect, stream=pix.tobytes("png"))
        out["scan"] = tmp_path / "scan.pdf"
        scan.save(out["scan"])
    with pikepdf.open(PDF) as pdf:
        for page in pdf.pages:
            for font in (page.Resources.get("/Font") or {}).values():
                if "/ToUnicode" in font:
                    del font["/ToUnicode"]
        out["no_tounicode"] = tmp_path / "no_tounicode.pdf"
        pdf.save(out["no_tounicode"])
    data = bytearray(PDF.read_bytes())
    at = data.rfind(b"startxref")
    data[at + 10 : at + 16] = b"999999"
    out["bad_xref"] = tmp_path / "bad_xref.pdf"
    out["bad_xref"].write_bytes(bytes(data))
    return out


@pytest.fixture(scope="module")
def damaged(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    return damaged_copies(tmp_path_factory.mktemp("damaged"))


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_scan_signals_pages_without_text(damaged: dict[str, Path], backend: str) -> None:
    doc = parse(damaged["scan"], DocKind.PDF, pdf_backend=backend)

    assert doc.signals is not None
    assert (doc.signals.pages_total, doc.signals.pages_without_text) == (3, 3)
    assert doc.signals.letters_share is None
    assert any(d.level is DiagLevel.ERROR for d in doc.diagnostics)


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_garbled_text_layer_signals_no_cyrillic_and_garbage(damaged: dict[str, Path], backend: str) -> None:
    clean = parse(PDF, DocKind.PDF, pdf_backend=backend).signals
    broken = parse(damaged["no_tounicode"], DocKind.PDF, pdf_backend=backend).signals

    assert clean is not None and broken is not None
    assert clean.cyrillic_share is not None and clean.cyrillic_share > 0.5
    assert clean.garbage_share == 0
    assert broken.cyrillic_share is not None and broken.cyrillic_share < 0.05
    assert broken.garbage_share is not None and broken.garbage_share > 0.1


def test_clean_pdf_is_not_marked_repaired_and_broken_xref_is(damaged: dict[str, Path]) -> None:
    clean = parse(PDF, DocKind.PDF)
    repaired = parse(damaged["bad_xref"], DocKind.PDF)

    assert clean.signals is not None and not clean.signals.repaired
    assert repaired.signals is not None and repaired.signals.repaired
    assert repaired.pages == 8


# ---------------------------------------------------------------- битые и защищённые файлы, переключатель


@pytest.fixture
def encrypted(tmp_path: Path) -> Path:
    plain = make_pdf(tmp_path / "plain.pdf", [[body(100, "Secret requirements text")]])
    path = tmp_path / "locked.pdf"
    with pikepdf.open(plain) as pdf:
        pdf.save(path, encryption=pikepdf.Encryption(user="u", owner="o"))
    return path


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_encrypted_pdf_gives_input_error(encrypted: Path, backend: str) -> None:
    with pytest.raises(InputError) as caught:
        parse(encrypted, DocKind.PDF, pdf_backend=backend)

    assert caught.value.code == IN_PDF_ENCRYPTED


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_garbage_pdf_gives_input_error(tmp_path: Path, backend: str) -> None:
    path = tmp_path / "garbage.pdf"
    path.write_bytes(b"%PDF-1.7\n\x00garbage without objects")

    with pytest.raises(InputError) as caught:
        parse(path, DocKind.PDF, pdf_backend=backend)

    assert caught.value.code == IN_PDF_BROKEN


def test_unknown_backend_is_a_clear_error() -> None:
    with pytest.raises(ValueError, match="mupdf, pdfium"):
        get_backend("tika")


def test_backend_switch_in_config(monkeypatch: pytest.MonkeyPatch) -> None:
    make: Callable[..., str] = lambda **kw: load_config(PDF, Path("out/r.md"), **kw).pdf_backend  # noqa: E731

    monkeypatch.delenv("VALIDATOR_PDF_BACKEND", raising=False)
    assert make() == "mupdf"
    assert make(pdf_backend="PDFium") == "pdfium"
    monkeypatch.setenv("VALIDATOR_PDF_BACKEND", "pdfium")
    assert make() == "pdfium"
    assert make(pdf_backend="mupdf") == "mupdf"
    with pytest.raises(ValueError, match="Неизвестный движок"):
        make(pdf_backend="tika")


def test_cli_has_pdf_backend_flag(tmp_path: Path) -> None:
    out = tmp_path / "r.md"
    ok = run_cli(PDF, out, "--backend", "fake", "--pdf-backend", "pdfium")
    bad = run_cli(PDF, out, "--backend", "fake", "--pdf-backend", "tika")

    assert ok.returncode == 0 and "стр. 2" in out.read_text(encoding="utf-8")
    assert bad.returncode == 2 and "Неизвестный движок" in bad.stderr


# ---------------------------------------------------------------- правки по ревью кода


@pytest.mark.parametrize("backend", BACKEND_NAMES)
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_page_is_read_as_displayed(tmp_path: Path, backend: str, rotation: int) -> None:
    """Текст нарисован повёрнутым, страница повёрнута так же: на экране он прямой, читаем как человек."""
    path = tmp_path / "rot.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page()
        page.set_rotation(rotation)
        for y, text in ((120, "The first line of the page."), (134, "The second line follows it.")):
            at = (
                pymupdf.Point(72, y) * page.derotation_matrix
            )  # точка на экране → точка в страницы без поворота
            page.insert_text(at, text, fontsize=11, fontname="helv", rotate=rotation)
        doc.save(path)

    parsed = parse(path, DocKind.PDF, pdf_backend=backend)

    assert parsed.text == "The first line of the page.\nThe second line follows it."


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_soft_hyphen_at_line_end_joins_the_word(tmp_path: Path, backend: str) -> None:
    path = make_pdf(
        tmp_path / "soft.pdf",
        [
            [
                body(120, "The gateway shall retransmit per\u00ad"),
                body(134, "sistent messages after a timeout."),
            ]
        ],
    )

    parsed = parse(path, DocKind.PDF, pdf_backend=backend)

    assert "retransmit persistent\nmessages after" in parsed.text


def test_adjacent_headings_with_numbers_are_not_merged(tmp_path: Path) -> None:
    path = make_pdf(
        tmp_path / "adj.pdf",
        [
            [
                (125, "1. General provisions", 14, "hebo"),
                (141, "1.1 Purpose of the document", 14, "hebo"),
                *[
                    body(180 + 14 * k, f"The system shall support operation number {k} for the customer.")
                    for k in range(6)
                ],
            ]
        ],
    )

    headings = [b.text for b in parse(path, DocKind.PDF).blocks if b.kind is BlockKind.HEADING]

    assert headings == ["1. General provisions", "1.1 Purpose of the document"]


def test_wrapped_heading_in_two_lines_is_one_heading(tmp_path: Path) -> None:
    path = make_pdf(
        tmp_path / "wrap.pdf",
        [
            [
                (125, "Requirements for the interaction of", 14, "hebo"),
                (140, "services and external systems", 14, "hebo"),
                *[
                    body(180 + 14 * k, f"The system shall support operation number {k} for the customer.")
                    for k in range(6)
                ],
            ]
        ],
    )

    headings = [b.text for b in parse(path, DocKind.PDF).blocks if b.kind is BlockKind.HEADING]

    assert headings == ["Requirements for the interaction of services and external systems"]


@pytest.mark.parametrize("backend", BACKEND_NAMES)
def test_short_real_page_is_not_reported_as_scan(tmp_path: Path, backend: str) -> None:
    path = make_pdf(tmp_path / "short.pdf", [[body(120, "The system shall work.")]])

    parsed = parse(path, DocKind.PDF, pdf_backend=backend)

    assert parsed.signals is not None and parsed.signals.pages_without_text == 0
    assert not parsed.diagnostics


def test_pdfium_surrogate_halves_become_one_character() -> None:
    codes = [ord("a"), 0xD83D, 0xDE00, 0xD83D, ord("b")]

    assert [char for _, char in _combine_surrogates(codes)] == ["a", "\U0001f600", "\ufffd", "b"]


def test_run_config_rejects_unknown_pdf_backend() -> None:
    with pytest.raises(ValidationError):
        RunConfig(input_path=PDF, output_path=Path("out/r.md"), pdf_backend="tika")  # type: ignore[arg-type]
