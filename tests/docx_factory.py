"""Сборка маленьких DOCX из XML для тестов разбора: нумерация, сноски, колонтитулы, правки, ячейки.

python-docx не умеет создавать эти части, поэтому архив собираем вручную. Файлы получаются настоящими:
их открывает та же ``docx.Document``, что и в агенте.
"""

# ruff: noqa: E501
from __future__ import annotations

import zipfile
from pathlib import Path

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
HEAD = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<w:{{tag}} xmlns:w="{W_NS}" xmlns:r="{R_NS}">'
)
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "application/vnd.openxmlformats-officedocument.wordprocessingml"

STYLES = """
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="MyHeading"><w:name w:val="Мой заголовок"/><w:basedOn w:val="Heading2"/></w:style>
<w:style w:type="paragraph" w:styleId="Outlined"><w:name w:val="Outlined"/>
  <w:pPr><w:outlineLvl w:val="2"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Numbered"><w:name w:val="Numbered"/>
  <w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="SourceCode"><w:name w:val="Source Code"/></w:style>
<w:style w:type="paragraph" w:styleId="Loop"><w:name w:val="Loop"/><w:basedOn w:val="Loop"/></w:style>
"""

NUMBERING = """
<w:abstractNum w:abstractNumId="0">
  <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
  <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1.%2."/></w:lvl>
  <w:lvl w:ilvl="2"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/><w:lvlText w:val="%3)"/></w:lvl>
</w:abstractNum>
<w:abstractNum w:abstractNumId="1">
  <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val=""/></w:lvl>
</w:abstractNum>
<w:abstractNum w:abstractNumId="2">
  <w:lvl w:ilvl="0"><w:start w:val="5"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl>
</w:abstractNum>
<w:num w:numId="4"><w:abstractNumId w:val="2"/></w:num>
<w:num w:numId="5"><w:abstractNumId w:val="2"/>
  <w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride></w:num>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>
<w:num w:numId="3"><w:abstractNumId w:val="0"/>
  <w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/></w:lvlOverride></w:num>
"""


def run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def para(
    text: str = "", *, style: str | None = None, num: tuple[int, int] | None = None, raw: str = ""
) -> str:
    props = ""
    if style:
        props += f'<w:pStyle w:val="{style}"/>'
    if num:
        props += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
    ppr = f"<w:pPr>{props}</w:pPr>" if props else ""
    return f"<w:p>{ppr}{run(text) if text else ''}{raw}</w:p>"


def cell(text: str = "", *, span: int = 1, vmerge: str | None = None, inner: str = "") -> str:
    props = ""
    if span > 1:
        props += f'<w:gridSpan w:val="{span}"/>'
    if vmerge is not None:
        props += f'<w:vMerge w:val="{vmerge}"/>' if vmerge else "<w:vMerge/>"
    tcpr = f"<w:tcPr>{props}</w:tcPr>" if props else ""
    return f"<w:tc>{tcpr}{para(text)}{inner}</w:tc>"


def row(*cells: str, header: bool = False) -> str:
    trpr = "<w:trPr><w:tblHeader/></w:trPr>" if header else ""
    return f"<w:tr>{trpr}{''.join(cells)}</w:tr>"


def table(*rows: str) -> str:
    return f"<w:tbl><w:tblPr/><w:tblGrid/>{''.join(rows)}</w:tbl>"


def footnote_ref(note_id: int) -> str:
    return f'<w:r><w:footnoteReference w:id="{note_id}"/></w:r>'


def make_docx(
    path: Path,
    body: str,
    *,
    footnotes: dict[int, str] | None = None,
    header: str | None = None,
    footer: str | None = None,
) -> Path:
    """Собрать DOCX с телом ``body`` (XML абзацев и таблиц), по желанию со сносками и колонтитулами."""
    sect = ""
    rels = [("rIdS", "styles", "styles.xml"), ("rIdN", "numbering", "numbering.xml")]
    parts = {
        "word/styles.xml": HEAD.format(tag="styles") + STYLES + "</w:styles>",
        "word/numbering.xml": HEAD.format(tag="numbering") + NUMBERING + "</w:numbering>",
    }
    overrides = {"styles": "styles", "numbering": "numbering"}
    if header is not None:
        sect += '<w:headerReference w:type="default" r:id="rIdH"/>'
        rels.append(("rIdH", "header", "header1.xml"))
        parts["word/header1.xml"] = HEAD.format(tag="hdr") + header + "</w:hdr>"
        overrides["header1"] = "header"
    if footer is not None:
        sect += '<w:footerReference w:type="default" r:id="rIdF"/>'
        rels.append(("rIdF", "footer", "footer1.xml"))
        parts["word/footer1.xml"] = HEAD.format(tag="ftr") + footer + "</w:ftr>"
        overrides["footer1"] = "footer"
    if footnotes is not None:
        notes = '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        notes += "".join(f'<w:footnote w:id="{k}">{para(v)}</w:footnote>' for k, v in footnotes.items())
        rels.append(("rIdFn", "footnotes", "footnotes.xml"))
        parts["word/footnotes.xml"] = HEAD.format(tag="footnotes") + notes + "</w:footnotes>"
        overrides["footnotes"] = "footnotes"
    parts["word/document.xml"] = (
        HEAD.format(tag="document") + f"<w:body>{body}<w:sectPr>{sect}</w:sectPr></w:body></w:document>"
    )
    parts["word/_rels/document.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(f'<Relationship Id="{i}" Type="{REL}/{t}" Target="{target}"/>' for i, t, target in rels)
        + "</Relationships>"
    )
    parts["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="word/document.xml"/></Relationships>'
    )
    types = "".join(
        f'<Override PartName="/word/{name}.xml" ContentType="{CT}.{kind}+xml"/>'
        for name, kind in overrides.items()
    )
    parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/word/document.xml" ContentType="{CT}.document.main+xml"/>{types}</Types>'
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return path
