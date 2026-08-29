"""Channel-neutral Markdown table rendering for OUTBOUND messages.

A GFM pipe table is unreadable on a channel that renders Markdown but not
tables: the pipes arrive literally and every column wraps, so a three-column
table becomes a wall of ragged text on a phone. This module converts such a
table into something the channel *can* render, and it is deliberately pure and
stdlib-only so it stays usable from every renderer without a dependency.

**It is an OUTBOUND presentation transform, not a rewrite of the turn.** The
canonical assistant text -- what ``TurnDriver.run`` returns, what the session
transcript and the dashboard show -- never passes through here. A renderer
applies it to the bytes it is about to hand its platform client, so the same
turn keeps pipes on the dashboard and gets cards on Discord.

Policies (``TABLE_POLICIES``), all resolved by :func:`resolve_table_policy`:

``off``
    No conversion. The floor, so a channel that never opts in is unchanged.
``cards``
    Every row becomes a mobile-safe card: the first column as a bold heading,
    the remaining headers as labels.
``grid``
    The table becomes an aligned monospace grid inside a fenced code block.
``native``
    The target renders tables itself, so pass through. On a target that does
    NOT (``native_tables=False``) this resolves to ``cards`` -- an unsupported
    ``native`` must never degrade to raw pipes, which is the exact output the
    policy exists to avoid.
``auto``
    Per table: a grid while its DISPLAY width fits
    ``GRID_MAX_DISPLAY_COLUMNS``, cards once it does not. On a target that
    renders tables natively, ``auto`` passes through.

Two properties the callers depend on:

* **Idempotent.** Neither rendering contains a table run of its own (cards
  carry no pipes; a grid lives inside a fence, which is never entered), so
  converting twice is converting once. A streaming renderer can therefore
  convert its buffer eagerly and re-convert what it retains.
* **Conservative.** Anything that is not unambiguously a GFM table is left
  byte-for-byte alone: prose, a malformed table whose separator row does not
  match its header's cell count, a pipe-bearing sentence, an indented code
  line, and anything inside a real fenced code block or CommonMark raw HTML
  block. Whitespace outside a converted run is preserved exactly, because
  re-indenting a caller's text was never asked for.
"""

from __future__ import annotations

import re
import unicodedata

from kiro_crew.constants import starts_with_marker_head

#: No conversion.
TABLE_POLICY_OFF = "off"
#: One card per row (first column = heading, later headers = labels).
TABLE_POLICY_CARDS = "cards"
#: Aligned monospace grid inside a fenced code block.
TABLE_POLICY_GRID = "grid"
#: The target renders tables itself; pass through when it really does.
TABLE_POLICY_NATIVE = "native"
#: Grid while it fits ``GRID_MAX_DISPLAY_COLUMNS``, else cards.
TABLE_POLICY_AUTO = "auto"

TABLE_POLICIES = frozenset(
    {
        TABLE_POLICY_OFF,
        TABLE_POLICY_CARDS,
        TABLE_POLICY_GRID,
        TABLE_POLICY_NATIVE,
        TABLE_POLICY_AUTO,
    }
)

#: Widest grid ``auto`` will emit, in DISPLAY columns (not characters).
#:
#: A phone renders a chat message at roughly 40 monospace columns, and a grid
#: wider than the viewport does not degrade gracefully -- the platform reflows
#: it and the alignment that was the whole point of a grid is gone. 42 leaves a
#: two-column allowance over that ~40 so a table sized for a phone is not
#: pushed to cards by a single trailing space, while anything genuinely wide
#: goes to cards, which reflow by construction.
GRID_MAX_DISPLAY_COLUMNS = 42

#: Cell separator in a rendered grid (`` | ``), and its display cost.
_GRID_SEP = " | "
_GRID_SEP_WIDTH = 3

#: Characters a GFM separator row may contain (``| --- |``, ``|:--|--:|``).
_SEPARATOR_CHARS = frozenset("-:| \t")

#: Zero-width characters that occupy no column. Combining marks are handled
#: separately via ``unicodedata.combining``.
_ZERO_WIDTH = frozenset("\u200b\u200c\u200d\u2060\ufeff")

#: East Asian widths that occupy two columns in a monospace cell.
_WIDE_EAST_ASIAN = frozenset("WF")

#: Max leading spaces before a line stops being a paragraph line: 4+ spaces is
#: an indented code block, whose pipes are code and not a table.
_MAX_INDENT = 3

# CommonMark HTML block types 1 and 6. Type 1 closes on its matching end tag;
# type 6 (and the generic type-7 tag syntax below) closes at a blank line.
_RAW_HTML_TAGS = ("script", "pre", "style", "textarea")
_BLOCK_HTML_TAGS = (
    "address",
    "article",
    "aside",
    "base",
    "basefont",
    "blockquote",
    "body",
    "caption",
    "center",
    "col",
    "colgroup",
    "dd",
    "details",
    "dialog",
    "dir",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "frame",
    "frameset",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "head",
    "header",
    "hgroup",
    "hr",
    "html",
    "iframe",
    "legend",
    "li",
    "link",
    "main",
    "menu",
    "menuitem",
    "nav",
    "noframes",
    "ol",
    "optgroup",
    "option",
    "p",
    "param",
    "search",
    "section",
    "summary",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "title",
    "tr",
    "track",
    "ul",
)
_BLOCK_HTML_START_RE = re.compile(
    r"^</?(?:" + "|".join(_BLOCK_HTML_TAGS) + r")(?:[ \t]|/?>|$)",
    re.IGNORECASE,
)
_COMPLETE_HTML_TAG_RE = re.compile(
    r"""
    ^(?:
      <[A-Za-z][A-Za-z0-9-]*
      (?:
        [ \t]+[A-Za-z_:][A-Za-z0-9_.:-]*
        (?:
          [ \t]*=[ \t]*
          (?:[^ "'=<>`]+|'[^']*'|"[^"]*")
        )?
      )*
      [ \t]*/?>
      |
      </[A-Za-z][A-Za-z0-9-]*[ \t]*>
    )[ \t]*$
    """,
    re.VERBOSE,
)


def display_width(text: str) -> int:
    """Columns *text* occupies in a monospace cell.

    ``len`` is the wrong measure for alignment and for the ``auto`` threshold:
    a CJK ideograph and a fullwidth form each occupy two columns while counting
    as one character, and a combining mark occupies none while counting as one.
    Padding by ``len`` therefore produces a grid whose columns visibly step,
    and thresholding by ``len`` sends a CJK table that is twice the viewport
    down the grid path.
    """
    total = 0
    for ch in text:
        if ch in _ZERO_WIDTH or unicodedata.combining(ch):
            continue
        total += 2 if unicodedata.east_asian_width(ch) in _WIDE_EAST_ASIAN else 1
    return total


def normalize_table_policy(policy: str) -> str:
    """Return *policy* if it is a known policy, else ``auto``.

    An unknown value is a caller bug, and the two safe answers are opposite:
    falling back to ``off`` would ship raw pipes to a channel that cannot
    render them, so the fallback is the adaptive policy instead.
    """
    value = (policy or "").strip().lower()
    return value if value in TABLE_POLICIES else TABLE_POLICY_AUTO


def resolve_table_policy(policy: str, *, native_tables: bool) -> str:
    """Resolve a declared policy against the target's real capability.

    Returns one of ``off`` / ``cards`` / ``grid`` / ``auto``; ``native`` never
    survives, because it is a claim about the TARGET rather than a rendering.

    * ``native`` on a target that renders tables -> ``off`` (pass through).
    * ``native`` on a target that does NOT -> ``cards``. This is the coercion
      the capability check exists for: the alternative is raw pipes, which is
      strictly worse than a card on every such channel.
    * ``auto`` on a native target -> ``off``, since the platform's own table
      beats anything rendered here.
    * ``cards`` / ``grid`` are explicit operator intent and are honoured on any
      target, native or not.
    """
    resolved = normalize_table_policy(policy)
    if resolved == TABLE_POLICY_NATIVE:
        return TABLE_POLICY_OFF if native_tables else TABLE_POLICY_CARDS
    if resolved == TABLE_POLICY_AUTO and native_tables:
        return TABLE_POLICY_OFF
    return resolved


def render_tables(
    text: str,
    *,
    policy: str,
    native_tables: bool = False,
    final: bool = True,
) -> str:
    """Render every GFM table in *text* per *policy*; leave the rest verbatim.

    ``final=False`` is the streaming contract: a table run that reaches the end
    of *text* may still be growing, so it is left raw. Converting it would
    freeze a half-arrived row as a card and strand the rows that follow, which
    then have no header to belong to. A run terminated by real content is
    converted either way, so a streaming caller converges without ever
    re-parsing what it already emitted.
    """
    rendered, _ = render_tables_with_metadata(
        text,
        policy=policy,
        native_tables=native_tables,
        final=final,
    )
    return rendered


def render_tables_with_metadata(
    text: str,
    *,
    policy: str,
    native_tables: bool = False,
    final: bool = True,
) -> tuple[str, bool]:
    """Render tables and report whether conversion generated a fenced grid."""
    resolved = resolve_table_policy(policy, native_tables=native_tables)
    # "|" is required by every table row, so its absence is a cheap exact
    # rejection of the overwhelmingly common case (prose).
    if resolved == TABLE_POLICY_OFF or not text or "|" not in text:
        return text, False

    lines = text.split("\n")
    out: list[str] = []
    has_generated_grid = False
    fence_run = 0  # length of the run that opened the fence now open; 0 = closed
    fence_char = ""
    fence_min_indent = 0
    fence_max_indent = _MAX_INDENT
    i = 0
    while i < len(lines):
        line = lines[i]
        run, char, rest, close_min_indent, close_max_indent = _fence_delimiter(
            line,
            min_indent=fence_min_indent if fence_run else 0,
            max_indent=fence_max_indent if fence_run else _MAX_INDENT,
            allow_list_container=not fence_run,
        )
        if fence_run:
            # Fence content is opaque -- a table inside it is a code sample.
            out.append(line)
            if char == fence_char and run >= fence_run and not rest.strip():
                fence_run, fence_char = 0, ""
                fence_min_indent, fence_max_indent = 0, _MAX_INDENT
            i += 1
            continue
        if run and not (char == "`" and "`" in rest):
            fence_run, fence_char = run, char
            fence_min_indent, fence_max_indent = close_min_indent, close_max_indent
            out.append(line)
            i += 1
            continue
        html_end = _html_block_end(lines, i)
        if html_end is not None:
            # Raw HTML blocks are opaque for the same reason as fenced code:
            # Markdown-looking source inside them is content, not a table.
            out.extend(lines[i:html_end])
            i = html_end
            continue
        end = _table_run_end(lines, i)
        if end is None:
            out.append(line)
            i += 1
            continue
        trailing_unterminated = end == len(lines) - 1 and not text.endswith("\n")
        if not final and (trailing_unterminated or all(not ln.strip() for ln in lines[end:])):
            # Blank trailing lines can still receive another body row. The
            # current unterminated final line can also become one after its next
            # chunk (for example ``Row `` followed by ``1 | ok``). Emit the
            # remainder untouched until either boundary is settled.
            #
            # Callers with a hard message limit must keep this trailing run
            # buffered until it is terminated or final. Splitting it while raw
            # strands headerless rows; forcing it final freezes a partial card.
            out.extend(lines[i:])
            i = len(lines)
            continue
        rendered, generated_grid = _render_run(lines[i:end], resolved)
        out.extend(rendered)
        has_generated_grid = has_generated_grid or generated_grid
        i = end
    return "\n".join(out), has_generated_grid


# -- fence + table grammar -------------------------------------------------


def _fence_parts(content: str) -> tuple[int, str, str]:
    """Return a fence run from already de-indented *content*, if present."""
    char = content[:1]
    if char not in ("`", "~"):
        return 0, "", ""
    run = len(content) - len(content.lstrip(char))
    if run < 3:
        return 0, "", ""
    return run, char, content[run:]


def _list_marker_end(content: str) -> int:
    """Index after one CommonMark bullet or ordered-list marker, else zero."""
    if len(content) >= 2 and content[0] in "-+*" and content[1] in " \t":
        return 1

    digits = 0
    while digits < min(9, len(content)) and content[digits].isdigit():
        digits += 1
    if (
        digits > 0
        and digits < len(content)
        and content[digits] in ".)"
        and digits + 1 < len(content)
        and content[digits + 1] in " \t"
    ):
        return digits + 1
    return 0


def _fence_delimiter(
    line: str,
    *,
    min_indent: int = 0,
    max_indent: int = _MAX_INDENT,
    allow_list_container: bool = False,
) -> tuple[int, str, str, int, int]:
    """Describe a fence delimiter and the valid indentation for its closer.

    A normal fence permits zero through three leading columns. A fence opened
    on the first line of a list item carries the marker's content indentation;
    subsequent lines must first satisfy that container indentation, then may
    add CommonMark's usual zero through three fence columns. Tracking both
    bounds avoids treating a four-space code line as a top-level closer while
    keeping multi-digit ordered-list fences opaque.
    """
    indent, stripped = _leading_indent_columns(line)
    if min_indent <= indent <= max_indent:
        run, char, rest = _fence_parts(stripped)
        if run:
            return run, char, rest, min_indent, max_indent

    if not allow_list_container or indent > _MAX_INDENT:
        return 0, "", "", min_indent, max_indent

    marker_end = _list_marker_end(stripped)
    if not marker_end:
        return 0, "", "", min_indent, max_indent

    index = marker_end
    column = indent + marker_end
    marker_column = column
    while index < len(stripped) and stripped[index] in " \t":
        if stripped[index] == " ":
            column += 1
        else:
            column += 4 - (column % 4)
        index += 1
    if not 1 <= column - marker_column <= 4:
        return 0, "", "", min_indent, max_indent

    run, char, rest = _fence_parts(stripped[index:])
    if not run:
        return 0, "", "", min_indent, max_indent
    return run, char, rest, column, column + _MAX_INDENT


def _html_block_marker(content: str) -> tuple[str, bool] | None:
    """Classify already de-indented HTML block *content*."""
    lowered = content.lower()
    for tag in _RAW_HTML_TAGS:
        prefix = f"<{tag}"
        if lowered.startswith(prefix) and (
            len(content) == len(prefix) or content[len(prefix)] in " \t>"
        ):
            return f"</{tag}>", True

    if content.startswith("<!--"):
        return "-->", False
    if content.startswith("<?"):
        return "?>", False
    if content.startswith("<![CDATA["):
        return "]]>", False
    if len(content) >= 3 and content.startswith("<!") and "A" <= content[2] <= "Z":
        return ">", False
    if _BLOCK_HTML_START_RE.match(content) or _COMPLETE_HTML_TAG_RE.match(content):
        return "", False
    return None


def _html_block_opener(line: str) -> tuple[str, bool] | None:
    """Return ``(end marker, case-insensitive)`` for a CommonMark HTML block.

    An empty marker denotes the block types that run through the next blank
    line. The first line may carry a list marker; the same one-to-four-column
    content indent accepted for list-contained fences applies here.
    """
    indent, content = _leading_indent_columns(line)
    if indent > _MAX_INDENT:
        return None

    direct = _html_block_marker(content)
    if direct is not None:
        return direct

    marker_end = _list_marker_end(content)
    if not marker_end:
        return None
    index = marker_end
    column = indent + marker_end
    marker_column = column
    while index < len(content) and content[index] in " \t":
        if content[index] == " ":
            column += 1
        else:
            column += 4 - (column % 4)
        index += 1
    if not 1 <= column - marker_column <= 4:
        return None
    return _html_block_marker(content[index:])


def _html_block_end(lines: list[str], start: int) -> int | None:
    """Exclusive end of the CommonMark raw HTML block at *start*, if any."""
    opener = _html_block_opener(lines[start])
    if opener is None:
        return None

    marker, case_insensitive = opener
    if marker:
        expected = marker.lower() if case_insensitive else marker
        for index in range(start, len(lines)):
            candidate = lines[index].lower() if case_insensitive else lines[index]
            if expected in candidate:
                return index + 1
        return len(lines)

    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    return end


def _code_spans(row: str) -> list[tuple[int, int]]:
    """Half-open ``[start, end)`` ranges of the inline code spans in *row*.

    CommonMark pairs a run of N backticks with the next run of EXACTLY N. An
    unpaired run is literal text and opens nothing -- otherwise one stray
    backtick would swallow the rest of the row and merge every cell after it.
    A backslash-escaped backtick is skipped for the same reason.
    """
    runs: list[tuple[int, int]] = []  # (start, run length)
    i, n = 0, len(row)
    while i < n:
        if row[i] == "\\":
            i += 2  # an escape pair; its second char is literal
            continue
        if row[i] != "`":
            i += 1
            continue
        j = i
        while j < n and row[j] == "`":
            j += 1
        runs.append((i, j - i))
        i = j
    spans: list[tuple[int, int]] = []
    opener = 0
    while opener < len(runs):
        start, length = runs[opener]
        closer = next((k for k in range(opener + 1, len(runs)) if runs[k][1] == length), None)
        if closer is None:
            opener += 1
            continue
        close_start, close_len = runs[closer]
        spans.append((start, close_start + close_len))
        opener = closer + 1
    return spans


def _row_cells(row: str, *, gfm_boundaries: bool = False) -> list[str]:
    """Split a table row on its cell boundaries.

    Two kinds of pipe are content rather than a boundary during rendering, and
    both cost a character if missed -- which a rendering, unlike a transport
    choice, can never afford:

    * ``\\|``. Escaping is decided by WALKING the row, not by a lookbehind:
      ``\\\\`` is a literal backslash that leaves a following ``|`` a real
      boundary, and a fixed-width lookbehind reads the second backslash of an
      even run as an escape and merges two cells.
    * a pipe inside an inline code span. GFM would split there and leave the
      backticks unpaired, so this is deliberately LOOSER than GFM -- and the
      looseness is safe because the decision here is only a RENDERING. On a
      channel with no table support the platform renders that markup as a
      paragraph either way, so the choice is between a card that keeps the
      author's ``a|b`` and a card that has silently deleted the pipe.

    ``gfm_boundaries`` disables only the code-span exception. Shape validation
    must count every unescaped pipe exactly as GFM does; otherwise malformed
    markup can pass the header/separator width check and lose authored cells
    during conversion.
    """
    spans = [] if gfm_boundaries else _code_spans(row)
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    span_idx = 0
    for pos, ch in enumerate(row):
        while span_idx < len(spans) and spans[span_idx][1] <= pos:
            span_idx += 1
        in_span = span_idx < len(spans) and spans[span_idx][0] <= pos
        if escaped:
            buf.append(ch)
            escaped = False
        elif ch == "\\" and not in_span:
            buf.append(ch)
            escaped = True
        elif ch == "|" and not in_span:
            cells.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    cells.append("".join(buf))
    return cells


def _trim_outer(cells: list[str]) -> list[str]:
    """Drop the empty cells produced by optional leading/trailing pipes."""
    if len(cells) > 1 and cells[0].strip() == "":
        cells = cells[1:]
    if len(cells) > 1 and cells[-1].strip() == "":
        cells = cells[:-1]
    return cells


def _cells(row: str) -> list[str]:
    """Cell TEXT of *row*: outer pipes dropped, trimmed, ``\\|`` unescaped."""
    return [c.strip().replace("\\|", "|") for c in _trim_outer(_row_cells(row.strip()))]


def _cell_count(row: str) -> int:
    return len(_trim_outer(_row_cells(row.strip(), gfm_boundaries=True)))


def _leading_indent_columns(line: str) -> tuple[int, str]:
    """Return visual indent columns and the line after leading spaces/tabs.

    Markdown expands a tab to the next four-column stop. Counting only literal
    spaces would treat a tab-indented code sample as an unindented table and
    rewrite source code into outbound data.
    """
    columns = 0
    index = 0
    while index < len(line):
        ch = line[index]
        if ch == " ":
            columns += 1
        elif ch == "\t":
            columns += 4 - (columns % 4)
        else:
            break
        index += 1
    return columns, line[index:]


def _is_separator_row(line: str) -> bool:
    """True if *line* is a GFM separator row (``| --- |``, ``---|:--:``)."""
    indent, content = _leading_indent_columns(line)
    if indent > _MAX_INDENT:
        return False
    stripped = content.strip()
    if not stripped or _is_markdown_block_starter(stripped):
        return False
    if not set(stripped) <= _SEPARATOR_CHARS:
        return False
    if "-" not in stripped or "|" not in stripped:
        return False
    cells = _trim_outer(_row_cells(stripped))
    return bool(cells) and all(re.fullmatch(r":?-+:?", cell.strip()) is not None for cell in cells)


def _is_markdown_block_starter(content: str) -> bool:
    """Whether unindented *content* starts a block that cannot be a table row."""
    if content.startswith(">"):
        return True
    if len(content) >= 2 and content[0] in "-+*" and content[1] in " \t":
        return True

    hashes = len(content) - len(content.lstrip("#"))
    if 1 <= hashes <= 6 and (hashes == len(content) or content[hashes] in " \t"):
        return True

    digits = 0
    while digits < min(9, len(content)) and content[digits].isdigit():
        digits += 1
    return (
        digits > 0
        and digits < len(content)
        and content[digits] in ".)"
        and digits + 1 < len(content)
        and content[digits + 1] in " \t"
    )


def _is_row_candidate(line: str) -> bool:
    """True if *line* could be a table row: pipe-bearing, not indented code,
    not a block/fence starter, not a protocol trailer that merely holds pipes."""
    if "|" not in line:
        return False
    indent, stripped = _leading_indent_columns(line)
    if indent > _MAX_INDENT:
        return False
    if _is_markdown_block_starter(stripped):
        return False
    if starts_with_marker_head(stripped) or stripped.startswith("[STEERING"):
        return False
    if _html_block_opener(line) is not None:
        return False
    return _fence_delimiter(line)[0] == 0


def _table_run_end(lines: list[str], start: int) -> int | None:
    """Exclusive end index of the table run at *start*, or None if there is
    none.

    Requires GFM's own shape: a pipe-bearing header, a separator row directly
    below it, and matching GFM cell counts. The lossless rendering parser must
    agree with that validated header width; if code-span opacity merges cells
    that GFM separates, conversion cannot preserve both the authored markup and
    the validated columns, so the run remains byte-identical.
    """
    if start + 1 >= len(lines):
        return None
    header = lines[start]
    if not _is_row_candidate(header) or _is_separator_row(header):
        return None
    separator = lines[start + 1]
    if not _is_separator_row(separator):
        return None
    gfm_width = _cell_count(header)
    if gfm_width != _cell_count(separator) or gfm_width != len(_cells(header)):
        return None
    end = start + 2
    while end < len(lines) and _is_row_candidate(lines[end]):
        end += 1
    return end


# -- renderings ------------------------------------------------------------


def _render_run(run: list[str], policy: str) -> tuple[list[str], bool]:
    """Render one table run and report whether it became a fenced grid."""
    _, header_content = _leading_indent_columns(run[0])
    prefix = run[0][: len(run[0]) - len(header_content)]
    header = _cells(run[0])
    width = len(header)
    body = [_normalize_row(_cells(row), width) for row in run[2:]]
    render_as_grid = not body or policy == TABLE_POLICY_GRID
    if not render_as_grid and policy != TABLE_POLICY_CARDS:
        columns = _column_widths(header, body)
        render_as_grid = _grid_display_width(columns) <= GRID_MAX_DISPLAY_COLUMNS

    if render_as_grid:
        rendered = _grid_lines(header, body)
    else:
        rendered = _card_lines(header, body)
        if not rendered:
            # Sparse rows can contain no heading or labeled values. Replacing a
            # valid table with an empty card list would erase both its headers
            # and its rows; a grid preserves the authored structure instead.
            rendered = _grid_lines(header, body)
            render_as_grid = True
    return ([prefix + line if line else line for line in rendered], render_as_grid)


def _normalize_row(cells: list[str], width: int) -> list[str]:
    """Pad/truncate *cells* to the header's column count, as GFM does."""
    if len(cells) < width:
        return cells + [""] * (width - len(cells))
    return cells[:width]


def _column_widths(header: list[str], body: list[list[str]]) -> list[int]:
    widths = [display_width(cell) for cell in header]
    for row in body:
        for idx, cell in enumerate(row):
            if idx < len(widths):
                widths[idx] = max(widths[idx], display_width(cell))
    return widths


def _grid_display_width(columns: list[int]) -> int:
    if not columns:
        return 0
    return sum(columns) + _GRID_SEP_WIDTH * (len(columns) - 1)


def _pad(cell: str, width: int) -> str:
    return cell + " " * max(0, width - display_width(cell))


def _longest_backtick_run(lines: list[str]) -> int:
    """Length of the longest consecutive backtick run in *lines*."""
    longest = 0
    current = 0
    for ch in "\n".join(lines):
        if ch == "`":
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _grid_lines(header: list[str], body: list[list[str]]) -> list[str]:
    """An aligned monospace grid, fenced so the channel keeps the alignment.

    The fence is load-bearing, not decoration: without it a proportional font
    makes equal-character columns visibly unequal, and the platform is free to
    collapse the runs of spaces doing the alignment. It also makes the output
    idempotent -- a later pass sees fence content and never re-enters it.
    """
    columns = _column_widths(header, body)
    rows = [header, *body]
    content = [
        _GRID_SEP.join(_pad(c, w) for c, w in zip(rows[0], columns)).rstrip(),
        "-+-".join("-" * w for w in columns),
    ]
    for row in rows[1:]:
        content.append(_GRID_SEP.join(_pad(c, w) for c, w in zip(row, columns)).rstrip())
    fence = "`" * max(3, _longest_backtick_run(content) + 1)
    return [fence, *content, fence]


def _card_lines(header: list[str], body: list[list[str]]) -> list[str]:
    """One card per row: first column bolded as the heading, later headers as
    labels.

    Cards are the wide-table answer because they REFLOW. A grid that overflows
    a phone's ~40 columns is wrapped by the platform at whatever point fits and
    the columns stop lining up, whereas a card is short lines that were never
    aligned to begin with, so a narrow viewport costs it nothing.

    An empty cell is omitted rather than rendered as a bare label: a sparse
    table would otherwise produce cards that are mostly punctuation. An
    entirely empty body returns no cards so the caller preserves the table as a
    grid. In a mixed body, an empty row becomes an em-dash card: its position is
    retained without forcing populated label/value pairs out of the card form
    that display-stage redaction scans.
    """
    cards: list[list[str]] = []
    for row in body:
        card: list[str] = []
        heading = row[0] if row else ""
        if heading:
            card.append(f"**{heading}**")
        for idx in range(1, len(row)):
            value = row[idx]
            if not value:
                continue
            label = header[idx] if idx < len(header) else ""
            card.append(f"- {label}: {value}" if label else f"- {value}")
        cards.append(card)

    if not any(cards):
        return []

    lines: list[str] = []
    for card in cards:
        if lines:
            lines.append("")
        lines.extend(card or ["—"])
    return lines
