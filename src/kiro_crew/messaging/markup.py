"""Markdown reductions for a surface that renders none of the source form.

Three constructs an agent writes that a chat bubble cannot show usefully. Each
reduction rewrites one of them into ordinary Markdown, so a channel keeps exactly
one dialect converter and this module never has to know which dialect that is:

* **``<thinking>`` blocks** -- an inline reasoning artifact some models embed in
  their answer text. No chat platform collapses it, so a channel that leaves it
  in delivers the model's scratchpad as prose, with no way for the reader to fold
  it away. :func:`strip_thinking_tags`.
* **pipe tables** -- a GFM table reads as columns only in a monospace grid. Every
  chat bubble is a proportional font, so the pipes line up with nothing and the
  widest column decides where each row wraps: on a phone that is the least
  readable form the same data has. :func:`flatten_pipe_tables`.
* **mermaid diagrams** -- a fenced diagram is source, not a picture, on any
  channel that cannot render an image from text. :func:`flatten_mermaid_body`.

This lives in ``messaging`` rather than in one channel package because none of
the three hazards is channel-specific: Discord, Telegram, iMessage, Weixin and
WhatsApp all deliver a ``<thinking>`` block and a pipe table as written.

Stdlib-only leaf: regex and string work with no imports from this package, so it
stays importable from any channel and adds nothing to a boot path.
"""

from __future__ import annotations

import re

__all__ = [
    "MERMAID_INFO",
    "flatten_mermaid_body",
    "flatten_pipe_tables",
    "strip_thinking_tags",
]

# ── <thinking> blocks ──

_THINKING_TAG_RE = re.compile(
    r"<(?:thinking|antml:thinking)>.*?</(?:thinking|antml:thinking)>",
    re.DOTALL,
)
#: An opener with no closer after it, so it owns the rest of the text. Applied
#: only after the paired-block substitution above, which is what makes "no closer
#: after it" true of whatever this matches.
_THINKING_OPEN_RE = re.compile(r"<(?:thinking|antml:thinking)>.*\Z", re.DOTALL)
_THINKING_EDGE_TAG_RE = re.compile(r"^<[^>]+>|<[^>]+>$")


def strip_thinking_tags(
    text: str, *, strip_whitespace: bool = True, hide_partial: bool = False
) -> tuple[str, str]:
    """Split *text* into ``(visible answer, extracted reasoning)``.

    ``hide_partial`` additionally drops an opener with no closer after it. It is
    for a channel that shows the answer WHILE it streams: without it a
    still-arriving block is visible for one frame, and on a channel where a frame
    is a real send that frame is also a push notification carrying the model's
    scratchpad. The same reasoning, and the same direction of error, as
    ``outbound_files.hide_local_refs``: an unterminated opener owns the remainder,
    because a buffer read while the reply is still arriving legitimately ends
    mid-construct.
    """
    thinking_parts: list[str] = []
    for match in _THINKING_TAG_RE.finditer(text):
        inner = _THINKING_EDGE_TAG_RE.sub("", match.group(0)).strip()
        if inner:
            thinking_parts.append(inner)
    cleaned = _THINKING_TAG_RE.sub("", text)
    if hide_partial:
        partial = _THINKING_OPEN_RE.search(cleaned)
        if partial:
            inner = _THINKING_EDGE_TAG_RE.sub("", partial.group(0)).strip()
            if inner:
                thinking_parts.append(inner)
            cleaned = cleaned[: partial.start()]
    if strip_whitespace:
        cleaned = cleaned.strip()
    return cleaned, "\n\n".join(thinking_parts)


# ── pipe tables ──

#: A candidate row: leading and trailing pipe, with at least one cell between.
#: Both delimiters are required so a prose line that merely contains a pipe is
#: never read as a table.
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+\|)\s*$")
#: Characters a GFM separator row may contain (``| --- |``, ``|:--|--:|``).
_TABLE_SEP_CHARS = frozenset("-:| \t")


def _is_separator(line: str) -> bool:
    """True when *line* is a GFM separator row.

    A dash and a pipe are both required: without the dash, a row of empty cells
    would be read as the separator and the real header row lost.
    """
    stripped = line.strip()
    if "-" not in stripped or "|" not in stripped:
        return False
    return bool(stripped) and set(stripped) <= _TABLE_SEP_CHARS


def _row_cells(row: str) -> list[str]:
    """The cells of a pipe row, outer delimiters and per-cell padding dropped."""
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


def _flatten_rows(header: list[str], rows: list[list[str]]) -> list[str]:
    """One Markdown bullet per data row, each cell labelled by its column.

    A row with MORE cells than the header has keeps the surplus unlabelled: a
    cell the author wrote is worth more than the label it lacks. An empty cell is
    skipped, so a sparse row does not read as a list of empty labels.
    """
    out: list[str] = []
    for row in rows:
        fields: list[str] = []
        for index, cell in enumerate(row):
            if not cell:
                continue
            label = header[index] if index < len(header) else ""
            fields.append(f"**{label}:** {cell}" if label else cell)
        if fields:
            out.append(f"- {' | '.join(fields)}")
    return out


def flatten_pipe_tables(text: str) -> str:
    """Rewrite each GFM pipe table in *text* as one labelled bullet per row.

    Output is Markdown, not a channel dialect, so the caller's own inline
    converter finishes the job.

    A separator row is REQUIRED for a run of pipe lines to count as a table, and
    a table with no data rows is passed through verbatim. Both rules exist so the
    function can never lose an authored line: keying the header off the first
    pipe-bearing line instead means a lone ``| a | b |`` is consumed as a header
    for a table that never arrives, and vanishes.

    The caller owns fence state -- a code sample full of pipes must not reach
    here, because the reduction cannot tell it from a table.
    """
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if (
            not _TABLE_ROW_RE.match(line)
            or index + 1 >= len(lines)
            or not _is_separator(lines[index + 1])
            or not _TABLE_ROW_RE.match(lines[index + 1])
        ):
            out.append(line)
            index += 1
            continue
        end = index + 2
        while end < len(lines) and _TABLE_ROW_RE.match(lines[end]):
            end += 1
        flattened = _flatten_rows(_row_cells(line), [_row_cells(r) for r in lines[index + 2 : end]])
        out.extend(flattened if flattened else lines[index:end])
        index = end
    return "\n".join(out)


# ── mermaid diagrams ──

#: The fence info string that marks a diagram. A caller compares its own opener's
#: info string against this rather than re-deriving the spelling.
MERMAID_INFO = "mermaid"

# One graph/flowchart node: ``A``, ``A[label]``, ``A{label}`` or ``A(label)``.
# Only the square and brace labels are read; a round one is consumed unread.
_GRAPH_NODE = r"\w+(?:\[[^\]]*\]|\{[^}]*\}|\([^)]*\))?"
_GRAPH_NODE_RE = re.compile(r"(\w+)(?:\[([^\]]*)\]|\{([^}]*)\}|\([^)]*\))?")
# Nodes joined by ``&`` (``B & C``) share every edge into and out of the group.
_GRAPH_GROUP = rf"{_GRAPH_NODE}(?:[ \t]*&[ \t]*{_GRAPH_NODE})*"
# One group followed by one arrow, ``A[label] -->|text|``: a line is a chain of
# these steps ending in a bare group, ``A --> B --> C`` being two steps and ``C``.
_GRAPH_STEP_RE = re.compile(rf"({_GRAPH_GROUP})[ \t]*(?:-->|---|-\.->|==>)(?:\|([^|]*)\|)?[ \t]*")
_GRAPH_GROUP_RE = re.compile(_GRAPH_GROUP)
_GRAPH_STATEMENT_SEPARATOR_RE = re.compile(r"[ \t]*;[ \t]*")
# sequenceDiagram messages: ``Actor->>Actor: message``. A ``+`` or ``-`` before
# the target activates or deactivates it; it is not part of the target's name.
_SEQ_RE = re.compile(r"(\S+?)\s*(->>|-->>|->|-->)\s*[+-]?(\S+?):\s*(.+)")

#: Indent every flattened edge carries, so the diagram reads as a block under
#: whatever heading the caller puts above it rather than as loose sentences.
_EDGE_INDENT = "  "

#: Most edges one flowchart is flattened into. ``&`` groups on both sides of an
#: arrow draw every node pair, so a short line can name millions of edges; past
#: this many the diagram reads as ``""`` and the caller shows the source.
_MAX_GRAPH_EDGES = 500


def _graph_nodes(group: str, labels: dict[str, str]) -> list[str]:
    """The node ids in one ``&`` *group*, recording each square or brace label."""
    ids: list[str] = []
    for match in _GRAPH_NODE_RE.finditer(group):
        node, square, brace = match.groups()
        if square or brace:
            labels[node] = square or brace
        ids.append(node)
    return ids


def _graph_line_edges(
    line: str, labels: dict[str, str], budget: int
) -> list[tuple[str, str | None, str]] | None:
    """Every ``(src, edge label, dst)`` one flowchart *line* draws, in order.

    A line can contain semicolon-separated statements. Each statement is a chain
    whose ``&`` groups draw one edge per node pair. Bare node statements record
    labels without drawing edges, and ``%%`` comment lines draw nothing. ``None``
    means the line draws more than *budget* edges, counted before pairs are built.
    """
    edges: list[tuple[str, str | None, str]] = []
    position = 0
    edge_count = 0
    while True:
        groups: list[str] = []
        edge_labels: list[str | None] = []
        end = position
        step = _GRAPH_STEP_RE.match(line, end)
        while step:
            groups.append(step.group(1))
            edge_labels.append(step.group(2))
            end = step.end()
            step = _GRAPH_STEP_RE.match(line, end)

        tail = _GRAPH_GROUP_RE.match(line, end)
        if tail:
            groups.append(tail.group(0))
            end = tail.end()
        elif groups:
            edge_labels.pop()  # the last arrow points at nothing, as in ``A --> B -->``
        else:
            break

        nodes = [_graph_nodes(group, labels) for group in groups]
        statement_edge_count = sum(
            len(nodes[index]) * len(nodes[index + 1]) for index in range(len(edge_labels))
        )
        edge_count += statement_edge_count
        if edge_count > budget:
            return None
        edges.extend(
            (src, edge_labels[index], dst)
            for index in range(len(edge_labels))
            for src in nodes[index]
            for dst in nodes[index + 1]
        )

        separator = _GRAPH_STATEMENT_SEPARATOR_RE.match(line, end)
        if not separator:
            break
        position = separator.end()
    return edges


def _graph_arrows(body: str) -> str:
    """A ``graph``/``flowchart`` body as one indented arrow per edge, or ``""``.

    ``""`` too when the body draws more than :data:`_MAX_GRAPH_EDGES` edges.
    """
    labels: dict[str, str] = {}
    edges: list[str] = []
    for line in body.split("\n")[1:]:  # the first line is the graph declaration
        line_edges = _graph_line_edges(line.strip(), labels, _MAX_GRAPH_EDGES - len(edges))
        if line_edges is None:
            return ""
        for src, edge_label, dst in line_edges:
            joint = f" ({edge_label.strip()}) " if edge_label else " "
            edges.append(f"{_EDGE_INDENT}{labels.get(src, src)} →{joint}{labels.get(dst, dst)}")
    return "\n".join(edges)


def _sequence_arrows(body: str) -> str:
    """A ``sequenceDiagram`` body as one indented message per line, or ``""``."""
    lines: list[str] = []
    for line in body.split("\n")[1:]:  # the first line is the diagram declaration
        match = _SEQ_RE.match(line.strip())
        if not match:
            continue
        src, arrow_type, dst, message = match.groups()
        # Mermaid arrows read left-to-right, so every glyph points at dst. ">>" is
        # a solid arrowhead and "--" a dashed (reply) line; keeping the four types
        # visually distinct is what stops a dashed reply and a dashed open arrow
        # from rendering identically.
        if "--" in arrow_type:
            arrow = "⇒" if ">>" in arrow_type else "⤳"
        else:
            arrow = "→" if ">>" in arrow_type else "⇢"
        lines.append(f"{_EDGE_INDENT}{src} {arrow} {dst}: {message.strip()}")
    return "\n".join(lines)


def flatten_mermaid_body(body: str) -> str:
    """Readable text for a mermaid *body*, or ``""`` when it cannot be read.

    ``""`` is the caller's signal to keep the source as written. A grammar this
    does not parse, and a diagram whose every edge line is unrecognised, are both
    still worth showing as the author wrote them: inventing a reading of a
    diagram is worse than showing it as code.
    """
    text = body.strip()
    if not text:
        return ""
    first = text.split("\n", 1)[0].strip().lower()
    if first.startswith(("graph ", "flowchart ")):
        return _graph_arrows(text)
    if first.startswith("sequencediagram"):
        return _sequence_arrows(text)
    return ""
