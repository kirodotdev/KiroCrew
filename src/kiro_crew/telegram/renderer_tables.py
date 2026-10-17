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

    A table is a pipe-bearing line immediately followed by a separator row whose
    cell count MATCHES it. The count check is what GFM (and therefore Telegram's
    Rich Markdown) uses to decide a table exists at all, so skipping it sends
    content down the rich path that the server then renders as a plain paragraph
    -- newlines collapsed, pipes literal, worse than the monospace fallback.

    Outer pipes are optional on BOTH rows, because GFM accepts ``a | b`` /
    ``--- | ---`` with no leading or trailing pipe -- anchoring on a leading
    ``|`` silently missed those and rendered them as literal pipes.

    The separator row must contain a dash (so it is a separator, not more data)
    and a pipe (so a bare ``-----`` horizontal rule under a pipe-bearing
    sentence is not mistaken for a table). The header must contain a pipe for
    the same reason: a one-cell separator would otherwise promote any ordinary
    sentence above it to a table.

    Deliberately does NOT exclude fenced code blocks. Table markup inside a
    fence only means one extra rich send, not wrong output: Rich Markdown parses
    fences itself and renders the sample as the code block it is. Screening for
    fences here would mean maintaining CommonMark's fence rules (delimiter
    character, run length, indentation, info strings) as a second parser beside
    the one the HTML renderer already owns, and every gap between the two is a
    bug -- for a check whose only job is deciding which transport to use.
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

    A table run starts on a pipe-bearing line whose successor is a separator row
    and extends while lines carry a pipe. This is the single run detector behind
    both the degraded seal (``_seal_table_fallback``) and table-aware splitting,
    so the two can never disagree about where a table begins or ends.

    It is deliberately looser than ``_has_table``, which additionally requires
    the separator's cell count to match its header. The two are not
    interchangeable and must not be unified: this one decides how a run is
    FRAMED, so over-matching costs nothing (``<pre>`` reproduces its input
    verbatim), while ``_has_table`` decides which TRANSPORT a segment takes and
    so must agree with the server's own GFM rule.

    Callers must screen out fenced text first (see
    ``_split_markdown_table_aware``).
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
