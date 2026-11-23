"""Write a markdown or text artifact as a Word (``.docx``) file for ``artifact show --docx``.

Markdown is read with the CommonMark parser the session export uses (``markdown-it-py``,
here with its table rule on), never with a hand-written scanner. Headings, paragraphs,
lists, code, block quotes, tables and strong, emphasis and code spans keep their shape;
anything else keeps its plain text. A ``text`` artifact becomes one paragraph a line.
Both size caps are checked before the document is built, and the document is built in
memory first, so a refusal leaves no partial file.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

#: Paragraphs plus table cells; one table cell or one paragraph counts one.
MAX_CELLS = 5000
#: Characters of source text, checked before it is parsed.
MAX_CHARS = 1_000_000
_CODE_FONT = "Courier New"


class DocxTooLarge(ValueError):
    """More than :data:`MAX_CELLS` cells or :data:`MAX_CHARS` characters."""


def _xml_safe(text: str) -> str:
    """Drop what a Word file cannot carry: control characters and lone surrogates."""
    keep = (c for c in text if c >= " " or c in "\t\n\r")
    return "".join(c for c in keep if c not in "\ufffe\uffff" and not "\ud800" <= c < "\ue000")


@cache
def _parser() -> Any:
    """CommonMark as ``dashboard.session_markdown`` parses it, plus pipe tables."""
    from markdown_it import MarkdownIt

    return MarkdownIt("commonmark", {"html": True}).enable("table")


def _add_inline(paragraph: Any, inline: Any) -> None:
    """Add *inline*'s children to *paragraph* as runs; unknown tokens keep their text."""
    bold = italic = 0
    for child in inline.children or ():
        kind = child.type
        if kind in ("strong_open", "strong_close"):
            bold += 1 if kind == "strong_open" else -1
        elif kind in ("em_open", "em_close"):
            italic += 1 if kind == "em_open" else -1
        elif kind == "softbreak":
            paragraph.add_run(" ")
        elif kind == "hardbreak":
            paragraph.add_run().add_break()
        elif child.content:
            run = paragraph.add_run(child.content)
            run.bold = bold > 0 or None
            run.italic = italic > 0 or None
            if kind == "code_inline":
                run.font.name = _CODE_FONT


def _bullet_style(depth: int) -> str:
    depth = min(depth, 3)
    return "List Bullet" + (f" {depth}" if depth > 1 else "")


def _add_table(doc: Any, tokens: list[Any], start: int) -> int:
    """Add the table opening at ``tokens[start]``; return the index of its close."""
    rows: list[list[Any]] = []
    i = start
    while tokens[i].type != "table_close":
        if tokens[i].type == "tr_open":
            rows.append([])
        elif tokens[i].type == "inline":
            rows[-1].append(tokens[i])
        i += 1
    if rows:
        width = max(len(row) for row in rows)
        table = doc.add_table(rows=len(rows), cols=width, style="Table Grid")
        for row, inlines in zip(table.rows, rows):
            for cell, inline in zip(row.cells, inlines):
                _add_inline(cell.paragraphs[0], inline)
    return i


def _render(doc: Any, tokens: list[Any]) -> None:
    numbers: list[int | None] = []  # next label of each open list; None for a bullet list
    quote = 0
    item_style: str | None = None
    label = ""
    heading: int | None = None
    i = 0
    while i < len(tokens):
        token = tokens[i]
        kind = token.type
        if kind == "table_open":
            i = _add_table(doc, tokens, i)
        elif kind in ("bullet_list_open", "ordered_list_open"):
            raw = token.attrGet("start")  # None for a list from 1; 0 is a start of its own
            numbers.append(None if kind == "bullet_list_open" else 1 if raw is None else int(raw))
        elif kind in ("bullet_list_close", "ordered_list_close"):
            numbers.pop()
        elif kind == "list_item_open":
            if (number := numbers[-1]) is None:
                item_style, label = _bullet_style(len(numbers)), ""
            else:
                item_style, label, numbers[-1] = None, f"{number}. ", number + 1  # counted per list
            if tokens[i + 1].type != "paragraph_open":  # the marker gets a paragraph of its own
                doc.add_paragraph(label, style=item_style or ("Quote" if quote else None))
                item_style, label = None, ""
        elif kind in ("blockquote_open", "blockquote_close"):
            quote += 1 if kind == "blockquote_open" else -1
        elif kind == "heading_open":
            heading = int(token.tag[1:])
        elif kind == "inline" and heading is not None:
            _add_inline(doc.add_heading("", heading), token)
            heading = None
        elif kind == "inline":
            paragraph = doc.add_paragraph(label, style=item_style or ("Quote" if quote else None))
            _add_inline(paragraph, token)
            item_style, label = None, ""
        elif kind in ("fence", "code_block"):
            run = doc.add_paragraph().add_run(token.content.rstrip("\n"))
            run.font.name = _CODE_FONT
        elif token.nesting == 0 and token.content:
            doc.add_paragraph(token.content.rstrip("\n"))
        i += 1


def _check(chars: int, cells: int) -> None:
    if chars > MAX_CHARS or cells > MAX_CELLS:
        raise DocxTooLarge(MAX_CELLS)


def markdown_to_document(text: str, *, markdown: bool = True) -> Any:
    """Build the in-memory Word document for *text* (imports ``docx`` on first use)."""
    _check(len(text), 0)
    text = _xml_safe(text)
    tokens = _parser().parse(text) if markdown else []
    lines = [] if markdown else text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the newline that ends the last line starts no new one
    _check(0, len(lines) + sum(t.nesting == 0 for t in tokens))  # each block _render can add

    from docx import Document

    doc = Document()
    _render(doc, tokens)
    for line in lines:
        doc.add_paragraph(line)
    return doc


def write_docx(text: str, target: Path, *, markdown: bool = True) -> None:
    """Write *text* to *target* as Word; never overwrites, always owner-only (0600)."""
    from contextlib import suppress

    from kiro_crew import platform_compat
    from kiro_crew.owner_only_files import owner_only_opener

    doc = markdown_to_document(text, markdown=markdown)
    created = False
    try:
        with open(target, "xb", opener=owner_only_opener) as handle:
            created = True
            platform_compat.restrict_to_owner(target)  # lockdown-ok: new empty file
            doc.save(handle)
    except BaseException:
        if created:
            with suppress(OSError):
                target.unlink(missing_ok=True)
        raise
