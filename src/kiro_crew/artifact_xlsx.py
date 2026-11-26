"""Write the tables of a text or markdown artifact as a new spreadsheet workbook.

Each pipe table, found from the Markdown parser's block tokens (so not one inside a
code block), is one sheet named after the heading above it (or ``Table N``). Every
cell is text as typed: one that starts with ``=``, ``+``, ``-`` or ``@`` is never a
formula, ``007`` keeps its zeros, and inline marks stay characters.
"""

from __future__ import annotations

import io
from typing import Any

from kiro_crew.artifact_docx import MAX_CHARS, _xml_safe

#: Largest workbook written, checked before the parser builds a table. MAX_CELLS is the
#: table rule's own limit on padded cells (0x10000), so no table under it is cut short.
MAX_TABLES = 50
MAX_CELLS = 0x10000
MAX_CELL_CHARS = 32_767  # the longest text one cell holds in the common spreadsheet format
_TITLE_CHARS = 31  # the longest sheet title
_TITLE_FORBIDDEN = "[]:*?/\\"  # the characters a sheet title cannot hold


class XlsxTooLarge(ValueError):
    """The artifact is over a size cap, or a cell is over :data:`MAX_CELL_CHARS`."""


class NoTables(ValueError):
    """The artifact holds no table."""


def _tables(text: str) -> list[tuple[str, list[list[str]]]]:
    """Every table of markdown *text* in order: (heading above it, rows of cell text).

    One pass over the block tokens; with the inline pass off, a cell is ``content`` as typed.
    """
    from markdown_it import MarkdownIt
    from markdown_it.rules_block.table import table as table_rule

    parser = (
        MarkdownIt("commonmark", {"html": True}).enable("table").disable(["inline", "text_join"])
    )
    # The table rule pads every body row to the header's width. A rule run just before it,
    # in the block that parses the table, reads the table's lines as the table rule ends
    # them (blank, less indented, code, or another block's start) and refuses past MAX_CELLS.
    bound = [0]

    def cell_bound(state: Any, start: int, end: int, silent: bool) -> bool:
        if silent or not table_rule(state, start, end, True):
            return False
        line = start + 1
        delimiter = state.src[state.bMarks[line] + state.tShift[line] : state.eMarks[line]]
        enders = state.md.block.ruler.getRules("blockquote")
        line += 1
        while line < end and 0 <= state.sCount[line] - state.blkIndent < 4:
            body = state.src[state.bMarks[line] + state.tShift[line] : state.eMarks[line]]
            if not body.strip() or any(ender(state, line, end, True) for ender in enders):
                break
            line += 1
        columns = sum(1 for part in delimiter.split("|") if part.strip())
        bound[0] += columns * (line - start - 1)
        if bound[0] > MAX_CELLS:
            raise XlsxTooLarge("cells")
        return False

    parser.block.ruler.before("table", "cell_cap", cell_bound)
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
