"""Write the tables of a text or markdown artifact as a new spreadsheet workbook.

Each pipe table is one sheet, named after the heading above it (or ``Table N``).
Tables are found from the Markdown parser's block tokens, so a table-looking
line inside a code block is not one. Every cell is stored as text, exactly as
typed: a cell that starts with ``=``, ``+``, ``-`` or ``@`` stays text and is
never a formula, and ``007`` keeps its zeros. Inline marks stay characters.

Only the tables are written; the rest of the artifact is not.
"""

from __future__ import annotations

import io
from typing import Any

from kiro_crew.artifact_docx import MAX_CHARS, _xml_safe

#: Largest workbook written. Checked while the tables are read, so a refused
#: artifact builds nothing.
MAX_TABLES = 50
MAX_CELLS = 100_000
#: The longest text one cell can hold in the common spreadsheet format.
MAX_CELL_CHARS = 32_767
#: The longest sheet title, and the characters a title cannot hold.
_TITLE_CHARS = 31
_TITLE_FORBIDDEN = "[]:*?/\\"


class XlsxTooLarge(ValueError):
    """The artifact is over a size cap, or a cell is over :data:`MAX_CELL_CHARS`."""


class NoTables(ValueError):
    """The artifact holds no table."""


def _tables(text: str) -> list[tuple[str, list[list[str]]]]:
    """Every table of markdown *text* in order: (heading above it, rows of cell text).

    One pass over the block tokens. The inline pass is turned off, so a cell is
    the inline token's ``content`` as typed.
    """
    from markdown_it import MarkdownIt

    parser = (
        MarkdownIt("commonmark", {"html": True}).enable("table").disable(["inline", "text_join"])
    )
    tokens = parser.parse(text)
    found: list[tuple[str, list[list[str]]]] = []
    heading = ""
    rows: list[list[str]] = []
    row: list[str] | None = None
    in_cell = False
    cells = 0
    for index, token in enumerate(tokens):
        kind = token.type
        if kind == "heading_open" and index + 1 < len(tokens):
            heading = " ".join(part.strip() for part in tokens[index + 1].content.split("\n"))
        elif kind == "table_open":
            if len(found) >= MAX_TABLES:
                raise XlsxTooLarge("tables")
            rows = []
            found.append((heading, rows))
        elif kind == "tr_open":
            row = []
            rows.append(row)
        elif kind in ("th_open", "td_open"):
            in_cell = True
        elif kind in ("th_close", "td_close"):
            in_cell = False
        elif kind == "inline" and in_cell and row is not None:
            cells += 1
            if cells > MAX_CELLS:
                raise XlsxTooLarge("cells")
            if len(token.content) > MAX_CELL_CHARS:
                raise XlsxTooLarge("cell")
            row.append(token.content)
    return found


def _title(heading: str, number: int, taken: set[str]) -> str:
    """A sheet title for the *number*th table: unique among *taken* without regard to case."""
    title = "".join(" " if ch in _TITLE_FORBIDDEN or ch < " " else ch for ch in heading)
    title = title.strip().strip("'").strip()[:_TITLE_CHARS].strip().strip("'").strip()
    if not title or title.lower() == "history":
        title = f"Table {number}"
    base, suffix = title, 1
    while title.lower() in taken:
        suffix += 1
        tail = f" {suffix}"
        title = base[: _TITLE_CHARS - len(tail)].rstrip() + tail
    taken.add(title.lower())
    return title


def build(text: str) -> bytes:
    """The workbook for the tables in *text*, in memory.

    Raises :class:`XlsxTooLarge` over a cap and :class:`NoTables` with no table.
    """
    if len(text) > MAX_CHARS:
        raise XlsxTooLarge("characters")
    tables = _tables(_xml_safe(text).replace("\r\n", "\n").replace("\r", "\n"))
    if not tables:
        raise NoTables("no table")
    # openpyxl is imported here, not at module scope: only a spreadsheet export needs it.
    from openpyxl import Workbook  # type: ignore[import-untyped]
    from openpyxl.cell import WriteOnlyCell  # type: ignore[import-untyped]

    book: Any = Workbook(write_only=True)
    taken: set[str] = set()
    for number, (heading, rows) in enumerate(tables, start=1):
        sheet = book.create_sheet(_title(heading, number, taken))
        for row in rows:
            out = []
            for value in row:
                cell = WriteOnlyCell(sheet, value=value)
                cell.data_type = "s"  # text, whatever the value starts with
                out.append(cell)
            sheet.append(out)
    data = io.BytesIO()
    book.save(data)
    return data.getvalue()
