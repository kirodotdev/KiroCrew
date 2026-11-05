"""GFM table structure for the Telegram renderer.

Recognises a markdown table -- its separator row, its cells and their count, and the
fenced regions a table never starts in -- groups a segment into table and prose blocks,
and splits a table by rows, so :class:`~kiro_crew.telegram.renderer.TelegramRenderer`
can send it as a Rich Message or degrade it to a sealed fallback. New table parsing goes
here. :mod:`kiro_crew.telegram.renderer` imports these and stays their import path and
patch surface.
"""

from __future__ import annotations

import re

# Characters a GFM separator row may contain (`| --- |`, `|:---|---:|`, `- | -`).
_TABLE_SEP_CHARS = set("-:| \t")


#: A line that opens or closes a fenced code block.
_FENCE_LINE_RE = re.compile(r"^[ \t]*(?:`{3,}|~{3,})", re.MULTILINE)


def _is_table_separator(line: str) -> bool:
    """True if *line* is a GFM separator row (``| --- |``, ``---|---``).

    Every cell must be non-empty and carry a dash. Both halves are what the
    server was observed to do, not what a spec reading predicts: ``| --- | |``
    comes back as a ``paragraph`` (so an empty cell must be rejected, or the
    block ships flattened), while ``| - - | --- |`` comes back as a ``table``
    (so a broken dash run must be ACCEPTED -- demanding a contiguous run here
    would degrade a table the server renders fine into a monospace block).
    """
    stripped = line.strip()
    if not stripped or not set(stripped) <= _TABLE_SEP_CHARS:
        return False
    if "-" not in stripped or "|" not in stripped:
        return False
    cells = _row_cells(stripped)
    if len(cells) > 1 and cells[0].strip() == "":
        cells = cells[1:]
    if len(cells) > 1 and cells[-1].strip() == "":
        cells = cells[:-1]
    return bool(cells) and all("-" in cell for cell in cells)


def _row_cells(row: str) -> list[str]:
    """Split a table row on its UNESCAPED cell boundaries.

    Escaping is decided by walking the row, not by a lookbehind: ``\\|`` is cell
    content, but ``\\\\`` is a literal backslash that leaves a following ``|`` as
    a real boundary. A fixed-width lookbehind cannot express that -- it reads the
    second backslash of an even run as an escape and merges two cells, which
    under-counts the row and can make a malformed header match its delimiter.
    """
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in row:
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\":
            buf.append(ch)
            escaped = True
        elif ch == "|":
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    cells.append("".join(buf))
    return cells


def _row_cell_count(row: str) -> int:
    """Number of cells in a GFM table row.

    Outer pipes are optional, so a single leading and a single trailing boundary
    -- which show up as empty first/last cells -- are dropped.
    """
    cells = _row_cells(row.strip())
    if len(cells) > 1 and cells[0] == "":
        cells = cells[1:]
    if len(cells) > 1 and cells[-1] == "":
        cells = cells[:-1]
    return len(cells)


def _has_table(text: str) -> bool:
    """True if *text* contains a GFM pipe table.

    A table is a pipe-bearing line followed by a separator row with the SAME cell
    count: that is GFM's (and Telegram Rich Markdown's) rule, and a mismatch the
    server renders as a flat paragraph with literal pipes. Outer pipes are
    optional on both rows, as in GFM. The separator needs a dash and a pipe, and
    the header a pipe, so a ``-----`` rule under a sentence is not a table.

    Fenced code is deliberately NOT excluded: a fenced sample costs one extra
    rich send (Rich Markdown renders the fence itself), while screening would
    need a second CommonMark fence parser just to pick a transport.
    """
    lines = text.split("\n")
    return any(
        "|" in header
        and _is_table_separator(sep)
        and _row_cell_count(header) == _row_cell_count(sep)
        for header, sep in zip(lines, lines[1:])
    )


def _table_blocks(text: str) -> list[tuple[bool, list[str]]]:
    """Partition *text* into alternating ``(is_table, lines)`` blocks.

    A run starts on a pipe-bearing line followed by a separator row and extends
    while lines carry a pipe. It is the one detector behind both
    ``_seal_table_fallback`` and table-aware splitting, so they always agree.

    Deliberately looser than ``_has_table`` (no cell-count match) and not to be
    unified with it: this only FRAMES a run, where ``<pre>`` over-matching costs
    nothing, while ``_has_table`` picks a TRANSPORT and must match GFM.

    Callers must screen out fenced text first (see ``_split_markdown_table_aware``).
    """
    lines = text.split("\n")
    blocks: list[tuple[bool, list[str]]] = []
    prose: list[str] = []
    i = 0
    while i < len(lines):
        if "|" in lines[i] and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            if prose:
                blocks.append((False, prose))
                prose = []
            block = [lines[i], lines[i + 1]]
            i += 2
            # Body rows run until the first line that is not part of the table.
            while i < len(lines) and "|" in lines[i]:
                block.append(lines[i])
                i += 1
            blocks.append((True, block))
            continue
        prose.append(lines[i])
        i += 1
    if prose:
        blocks.append((False, prose))
    return blocks


def _split_table_rows(rows: list[str], limit: int) -> list[str]:
    """Split one table run at ROW boundaries into chunks of <=``limit`` chars.

    Every continuation chunk repeats the header and separator row, so each
    chunk is independently detected by ``_has_table`` and seals through the
    rich path. The alternative (bare body rows) fails detection and arrives as
    literal pipes -- the exact defect table-aware splitting exists to fix.

    A single row longer than ``limit`` cannot be cut (there is no sub-row
    boundary that keeps the table valid); its chunk is returned oversize and
    the client's tag-safe truncation is the backstop.
    """
    header, sep = rows[0], rows[1]
    head_len = len(header) + len(sep) + 2  # + the two joining newlines
    chunks: list[str] = []
    cur = [header, sep]
    cur_len = head_len
    for row in rows[2:]:
        row_cost = len(row) + 1  # + the joining newline
        if cur_len + row_cost > limit and len(cur) > 2:
            chunks.append("\n".join(cur))
            cur = [header, sep]
            cur_len = head_len
        cur.append(row)
        cur_len += row_cost
    chunks.append("\n".join(cur))
    return chunks
