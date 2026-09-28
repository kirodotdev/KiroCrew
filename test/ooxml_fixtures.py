"""Hand-built .docx fixtures for the structured-block tests.

The archives are written with the standard library only. A real writer
(python-docx) would produce a document whose exact XML this suite does not
control, and the whole point of these fixtures is to pin behaviour on specific
markup: a numbering definition that says "decimal", a style that carries the
numbering, a table row whose cells sit inside a content control.

Only the parts the extractor actually reads are written, which is also what makes
the "incomplete container" cases expressible.
"""

from __future__ import annotations

import zipfile

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def docx_para(text: str, style: str = "", num_id: str = "", bold: bool = False) -> str:
    """One ``w:p``: optionally styled (heading), numbered (list) or bold."""
    props = ""
    if style or num_id:
        inner = f'<w:pStyle w:val="{style}"/>' if style else ""
        if num_id:
            inner += f'<w:numPr><w:numId w:val="{num_id}"/></w:numPr>'
        props = f"<w:pPr>{inner}</w:pPr>"
    run_props = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f"<w:p>{props}<w:r>{run_props}<w:t>{text}</w:t></w:r></w:p>"


def docx_styles_xml(style_nums: dict[str, str]) -> str:
    """A ``word/styles.xml`` whose named styles carry a ``w:numPr``.

    This is how Word writes its own "List Bullet" / "List Number": a paragraph
    using one carries nothing but ``w:pStyle``, and the numbering lives here.
    """
    styles = "".join(
        f'<w:style w:type="paragraph" w:styleId="{sid}">'
        f'<w:name w:val="{sid}"/><w:basedOn w:val="Normal"/>'
        f'<w:pPr><w:numPr><w:numId w:val="{num_id}"/></w:numPr></w:pPr></w:style>'
        for sid, num_id in style_nums.items()
    )
    return f"<w:styles {_W}>{styles}</w:styles>"


def docx_table(rows: list[list[str]]) -> str:
    """One ``w:tbl`` from a row-major grid of cell strings."""
    body = "".join(
        "<w:tr>"
        + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row)
        + "</w:tr>"
        for row in rows
    )
    return f"<w:tbl>{body}</w:tbl>"


def write_docx(
    path: str,
    body: str,
    *,
    numbering: dict[str, str] | None = None,
    styles_xml: str | None = None,
) -> None:
    """Write a .docx.

    *numbering* maps ``w:numId`` → ``w:numFmt`` ("decimal", "bullet", …); the
    two-level abstractNum indirection real documents use is written out so the
    extractor's resolution is exercised rather than bypassed.
    *styles_xml* is written as ``word/styles.xml`` -- see :func:`docx_styles_xml`.
    """
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "word/document.xml",
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f"<w:document {_W} {_R}><w:body>{body}</w:body></w:document>",
        )
        if numbering:
            abstracts = "".join(
                f'<w:abstractNum w:abstractNumId="{i}">'
                f'<w:lvl w:ilvl="0"><w:numFmt w:val="{fmt}"/></w:lvl>'
                f"</w:abstractNum>"
                for i, fmt in enumerate(numbering.values())
            )
            nums = "".join(
                f'<w:num w:numId="{nid}"><w:abstractNumId w:val="{i}"/></w:num>'
                for i, nid in enumerate(numbering)
            )
            zf.writestr("word/numbering.xml", f"<w:numbering {_W}>{abstracts}{nums}</w:numbering>")
        if styles_xml is not None:
            zf.writestr("word/styles.xml", styles_xml)
