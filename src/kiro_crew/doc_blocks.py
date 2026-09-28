"""Structured block extraction for .docx OOXML documents.

:mod:`kiro_crew.doc_parser` flattens a document to one plaintext string, which is
what an agent reading an attachment wants. The dashboard's file viewer wants the
structure back, so this module returns a JSON-serializable block list instead.

Hardening is inherited, not re-spelled: the archive-inventory preflight and the
bounded per-entry reader are doc_parser's, imported directly so a change to
either lands here too, and XML parsing goes through the same defusedxml
``fromstring`` (the stdlib parser resolves external entities, an XXE on a
user-supplied document). What this module adds is a bound on what ONE document
can turn INTO -- an element count and an aggregate character budget -- because a
structured payload becomes DOM elements, so an unbounded element count is a
render-time denial of service and not merely a large response.

Best-effort like doc_parser: on malformed input a function returns whatever it
already has rather than raising, and callers treat an empty list as "no
structured preview available" and fall back to text.
"""

from __future__ import annotations

import logging
import os
import re
import zipfile
from typing import IO, Any, Iterator

from kiro_crew.doc_parser import (
    _read_zip_entry,
    _vet_archive_inventory,
    _xml_fromstring,
)
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

# ── Budgets ──

#: Elements one document may become in the DOM: every block, every formatted
#: run inside a paragraph, every list item, every table cell. One counter for
#: all of them, because the cost being bounded is the same -- an element the
#: browser has to lay out -- whichever kind of element it is. A 100-page report
#: is a few thousand paragraphs of a few runs each; a 200-row, 40-column table
#: is 8,000 cells; 40,000 elements lay out in well under a second, and half a
#: million do not lay out at all.
MAX_NODES = 40_000
#: Aggregate characters across every block's text. Equal to the text preview's own
#: cap (pinned by test, since this module sits below the dashboard handlers and
#: cannot import it), so choosing a format cannot widen what one request extracts.
MAX_CHARS = 512_000
#: Columns kept per table row. A display ceiling, not a size one: past this a
#: grid is unreadable in a side panel, and the trim is reported on the table.
MAX_TABLE_COLS = 40

# ── OOXML namespaces ──

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
#: ``Heading3``, ``heading 3`` and ``Heading_3`` all name level 3. Word writes
#: the style id without a space; documents from other writers do not.
_HEADING_RE = re.compile(r"^heading([1-9])$")

Block = dict[str, Any]


class _Budget:
    """Shared element/character allowance for one document's extraction.

    Held by the walkers rather than checked by their caller because a cap has to
    stop work BEFORE the next member is decompressed and parsed: a caller that
    trims an over-budget result has already paid to build it.
    """

    def __init__(self) -> None:
        # Read at construction rather than bound as defaults: the caps are
        # module-level policy, and a default argument would freeze whatever they
        # were at import time.
        self.nodes_left = MAX_NODES
        self.chars_left = MAX_CHARS
        #: Set when a cap stopped extraction, so the caller can tell a short
        #: document from a trimmed one and say so in the UI.
        self.truncated = False

    def stop(self) -> bool:
        """Is the allowance spent? Records that the result is truncated if so.

        Deliberately not a pure predicate: every caller checks it exactly to
        decide whether to stop, so the place that learns the result is partial is
        the place that has to remember it.
        """
        if self.nodes_left > 0 and self.chars_left > 0:
            return False
        self.truncated = True
        return True

    def spend(self, text: str, nodes: int) -> bool:
        """Charge one unit -- *text* and the *nodes* it renders as -- or refuse it.

        Charges BEFORE the unit is appended, because one paragraph or one table
        row can be megabytes by itself -- bounded only by doc_parser's 50 MB
        per-entry decompression cap -- so charging afterwards left the allowance
        overrun by that whole string while every counter still read as though it
        had not.

        WHOLE OR NOTHING, never a prefix. Redaction runs on these strings later
        and downstream, and it matches patterns: a string cut to fit can carry a
        partial secret that those patterns miss, so the fragment survives
        redaction and ships. Refusing the unit outright covers every secret shape
        at once, and leaves this module free of any notion of what a secret
        looks like.

        A refusal spends the REST of the allowance, so ``stop()`` halts
        extraction immediately after and the refused unit is the last one. Left
        positive, a later, smaller unit would still fit and render past the gap
        -- a document with an omitted middle, which ``truncated`` ("only the
        beginning") does not describe. This is also why there is no separate
        per-table row ceiling: a cap that refused a row without exhausting the
        allowance let the paragraphs after the table render, and the preview
        presented the two pieces as one continuous document.

        Elements are charged alongside characters because the two are
        independent: 512,000 one-character runs alternating bold and plain fit
        the character budget as ONE paragraph, and the browser then has half a
        million elements to lay out.
        """
        if len(text) > self.chars_left or nodes > self.nodes_left:
            self.refuse()
            return False
        self.chars_left -= len(text)
        self.nodes_left -= nodes
        return True

    def refuse(self) -> None:
        """Spend the REST of the allowance: the unit being built does not fit.

        What ``spend`` does on a unit that does not fit, callable by a walker that
        can already see the unit will not fit and so need not finish building it.
        """
        self.truncated = True
        self.chars_left = 0
        self.nodes_left = 0

    def emit(self, blocks: list[Block], block: Block, text: str = "", nodes: int = 1) -> bool:
        """Append *block*, charging it and what it arrives with, or REFUSE.

        *nodes* counts the block's own element plus any children it already
        carries, and *text* is their text: a list is emitted WITH its first item
        and a table WITH its first row, as one unit. Charged apart, the container
        could fit where its first child did not -- and the document then rendered
        as one empty list or table, which the viewer takes as "structure present"
        and so shows neither the items nor the plain-text fallback. Refusing the
        pair together means a container never exists without its first child.

        Returns False without appending when the unit does not fit, so a caller
        in a loop can stop. Refusing here rather than trusting each caller's own
        check is what makes the cap hold: every walker appends through this one
        method, so no loop can emit past the allowance by forgetting to check at
        its own top.
        """
        if not self.spend(text, nodes):
            return False
        blocks.append(block)
        return True


# ── Public API ──


def extract_blocks(
    path: str,
    filename: str = "",
    fileobj: IO[bytes] | None = None,
) -> tuple[list[Block], bool]:
    """Extract a structured block list from a .docx.

    Returns ``(blocks, truncated)``; ``([], False)`` for an unsupported
    extension, a malformed container, or a document with no extractable
    content — callers treat an empty list as "fall back to text". *truncated*
    says a budget stopped extraction, not that the document was short.

    *fileobj*, when given, is an ALREADY-OPEN binary handle the ZIP is read from
    instead of re-opening *path*, so a caller that stat-gates the file parses
    exactly the bytes it measured (no stat→open TOCTOU window). *path* is still
    used for the sensitive-path screen, format detection and logging.
    """
    if is_sensitive_path(path):
        logger.warning("Refusing to read sensitive path: %s", path)
        return [], False
    if _xml_fromstring is None:
        logger.warning(
            "Cannot parse %s: defusedxml is not installed (checkout newer than "
            "installed deps?). Fix: pip install -e .",
            filename or path,
        )
        return [], False
    ext = os.path.splitext(filename or path)[1].lower()
    if ext != ".docx":
        return [], False
    if not _vet_archive_inventory(path, fileobj):
        return [], False
    budget = _Budget()
    try:
        with zipfile.ZipFile(fileobj if fileobj is not None else path, "r") as zf:
            blocks = _docx_blocks(zf, budget)
    except Exception:
        logger.warning("Failed to extract blocks from %s", path, exc_info=True)
        return [], False
    return blocks, budget.truncated


# ── Shared helpers ──


def _parse_member(zf: zipfile.ZipFile, member: str) -> Any | None:
    """Read and XML-parse one ZIP member, or None when it is absent/unusable.

    Absent, over the per-entry decompression cap and malformed all collapse to
    None: there is no way to render half a part, and doc_parser's contract is
    that an unreadable document degrades rather than raises.
    """
    assert _xml_fromstring is not None
    if member not in zf.namelist():
        return None
    data = _read_zip_entry(zf, member)
    if data is None:
        return None
    try:
        return _xml_fromstring(data)
    except Exception:
        logger.warning("unreadable OOXML part: %s", member, exc_info=True)
        return None


def _toggle_on(props: Any, tag: str) -> bool:
    """Is a boolean OOXML toggle property present and not explicitly off?

    A toggle is on when the element exists with no ``val``; ``val="0"``/
    ``"false"``/``"off"`` turns it off, which is how Word cancels a property
    inherited from the paragraph's style.
    """
    node = props.find(tag)
    if node is None:
        return False
    return node.get(f"{_W_NS}val", "1") not in ("0", "false", "off")


class _ListRun:
    """Accumulator that turns consecutive list paragraphs into one list block.

    Word marks list membership per PARAGRAPH, so a three-bullet list arrives as
    three paragraphs; without this the renderer emits three single-item lists,
    each with its own marker column.

    The block is emitted when the run STARTS, together with its first item, and
    later items are appended into it as they arrive. Emitting at the end, after
    the items had each been charged, meant a list that spent the last of the
    allowance was refused as a block and every item already paid for vanished
    with it. Same shape as a table, whose block is emitted with its first row.
    """

    def __init__(self) -> None:
        self.block: Block | None = None

    def add(self, text: str, ordered: bool, out: list[Block], budget: _Budget) -> None:
        """Append one item, opening a new block when the kind changes.

        The item's text was charged by the paragraph walk; what is charged here is
        the ``<li>`` it becomes, plus the list's own element for the first one. A
        refused item ends extraction, so no ceiling of its own is needed to keep
        one list from becoming the whole allowance.
        """
        if self.block is not None and ordered != self.block["ordered"]:
            self.block = None
        if self.block is None:
            block: Block = {"type": "list", "ordered": ordered, "items": [text]}
            if budget.emit(out, block, nodes=2):
                self.block = block
            return
        if budget.spend("", 1):
            self.block["items"].append(text)

    def flush(self) -> None:
        """Close the run; the next list paragraph opens a new block."""
        self.block = None


# ── DOCX ──


def _docx_numbering_ordered(zf: zipfile.ZipFile) -> dict[str, bool]:
    """Map ``w:numId`` → is-ordered, read from ``word/numbering.xml``.

    A list's marker style sits two indirections away from the paragraph using it:
    the paragraph names a ``numId``, which names an ``abstractNumId``, whose level
    0 carries the ``numFmt``. Without this a numbered list and a bulleted one are
    indistinguishable. Level 0 stands for the whole list because the block shape
    is flat, so a deeper level's format is not representable anyway.

    An absent or unreadable part yields ``{}`` and every list renders unordered,
    the safer wrong answer: a bullet in front of numbered text still reads as a
    bullet, where "1." in front of bulleted text invents an order the document
    never had.
    """
    root = _parse_member(zf, "word/numbering.xml")
    if root is None:
        return {}
    abstract_ordered: dict[str, bool] = {}
    for abstract in root.findall(f"{_W_NS}abstractNum"):
        aid = abstract.get(f"{_W_NS}abstractNumId")
        if aid is None:
            continue
        for lvl in abstract.findall(f"{_W_NS}lvl"):
            if lvl.get(f"{_W_NS}ilvl") not in (None, "0"):
                continue
            fmt = lvl.find(f"{_W_NS}numFmt")
            val = fmt.get(f"{_W_NS}val", "") if fmt is not None else ""
            abstract_ordered[aid] = val not in ("bullet", "none")
            break
    out: dict[str, bool] = {}
    for num in root.findall(f"{_W_NS}num"):
        nid = num.get(f"{_W_NS}numId")
        ref = num.find(f"{_W_NS}abstractNumId")
        if nid is None or ref is None:
            continue
        out[nid] = abstract_ordered.get(ref.get(f"{_W_NS}val", ""), False)
    return out


def _docx_style_num_ids(zf: zipfile.ZipFile) -> dict[str, str]:
    """Map paragraph styleId -> the ``w:numId`` its definition in styles.xml carries.

    Word's own "List Bullet" and "List Number" styles put the numbering on the
    STYLE, not on the paragraph: a paragraph using one carries nothing but
    ``w:pStyle``, so reading ``w:numPr`` off the paragraph alone misses every
    list a real document produces this way and renders it as ordinary body
    paragraphs. A style inheriting its numbering through ``w:basedOn`` is not
    chased, because Word writes the numId on the list style itself.
    """
    root = _parse_member(zf, "word/styles.xml")
    if root is None:
        return {}
    out: dict[str, str] = {}
    for style in root.findall(f"{_W_NS}style"):
        style_id = style.get(f"{_W_NS}styleId")
        num_id = style.find(f"{_W_NS}pPr/{_W_NS}numPr/{_W_NS}numId")
        value = num_id.get(f"{_W_NS}val", "") if num_id is not None else ""
        if style_id and value:
            out[style_id] = value
    return out


def _docx_list_num_id(props: Any, style_nums: dict[str, str]) -> str:
    """The ``w:numId`` that makes one paragraph a list item, or "" if it is not one.

    Two mechanisms, both ordinary in real documents: numbering set directly on
    the paragraph (Word's toolbar bullet button) and numbering inherited from
    the paragraph's style. A direct ``numId`` of 0 is Word CANCELLING inherited
    numbering, and a direct ``w:numPr`` outranks the style either way -- so a
    cancelled list paragraph must not be resurrected from its style.
    """
    if props is None:
        return ""
    if props.find(f"{_W_NS}numPr") is not None:
        direct = props.find(f"{_W_NS}numPr/{_W_NS}numId")
        num_id = direct.get(f"{_W_NS}val", "") if direct is not None else ""
        return "" if num_id == "0" else num_id
    style = props.find(f"{_W_NS}pStyle")
    return style_nums.get(style.get(f"{_W_NS}val", ""), "") if style is not None else ""


def _sdt_children(parent: Any, tag: str = "") -> Iterator[Any]:
    """Children of *parent*, descending through the wrappers a template writes.

    A template-produced document wraps things -- whole body sections, a
    repeating row, a single cell -- in a structured document tag (``w:sdt``,
    children under ``w:sdtContent``) or a custom XML element (``w:customXml``,
    children directly inside), so what the walker wants is not a direct child
    of where it looks, and reading only direct children silently drops
    everything inside. The text preview reaches through both, so a structured
    walk that reached through only one returned a non-empty block list with
    those sections absent -- no fallback, no marker. One rule for every level,
    because each level that had its own copy missed the one the copy was not
    written for.

    *tag* narrows the yield to that element; empty yields every non-wrapper
    child. Deliberately NOT ``parent.iter(tag)``: that would also collect a
    NESTED table's rows or cells as the outer one's own.
    """
    for child in parent:
        if child.tag == f"{_W_NS}sdt":
            content = child.find(f"{_W_NS}sdtContent")
            if content is not None:
                yield from _sdt_children(content, tag)
            continue
        if child.tag == f"{_W_NS}customXml":
            yield from _sdt_children(child, tag)
            continue
        if not tag or child.tag == tag:
            yield child


def _docx_para_runs(para: Any, budget: _Budget) -> list[Block]:
    """Formatted runs of one ``w:p``, bold/italic preserved.

    The paragraph is charged as ONE unit -- its joined text and the elements its
    runs become. Word splits a sentence at every formatting change, so a
    credential that crosses a bold boundary arrives as two runs; charging run by
    run could keep the first and refuse the second, and the redactor -- which
    matches whole tokens on the joined paragraph -- would then see a prefix its
    patterns do not match. The unit of refusal has to be the unit the redactor
    reads, so a paragraph that does not fit is dropped whole.

    Runs are merged AS THEY ARE WALKED, and the walk stops the moment the
    paragraph is past what can fit. Word also splits a plain sentence at every
    revision and spell-check span, so merging keeps the payload proportional to
    the text; and building every run first, to charge them all at the end, held
    a crafted paragraph of millions of runs in memory before refusing it -- the
    allocation the budget exists to prevent, paid in full and then thrown away.
    What is held here is bounded by the budget itself: at most one element more
    than fit, and one run's text more than fit.
    """
    # Each merged run is ``[bold, italic, parts]``; the parts are joined once at
    # the end, since concatenating onto a growing string per run is quadratic.
    runs: list[tuple[bool, bool, list[str]]] = []
    chars = 0
    for run in para.iter(f"{_W_NS}r"):
        text = "".join(t.text for t in run.iter(f"{_W_NS}t") if t.text)
        if not text:
            continue
        props = run.find(f"{_W_NS}rPr")
        bold = props is not None and _toggle_on(props, f"{_W_NS}b")
        italic = props is not None and _toggle_on(props, f"{_W_NS}i")
        if runs and runs[-1][0] == bold and runs[-1][1] == italic:
            runs[-1][2].append(text)
        else:
            runs.append((bold, italic, [text]))
        chars += len(text)
        if chars > budget.chars_left or len(runs) > budget.nodes_left:
            # Past the boundary: this paragraph will be refused whole, so refuse
            # it now -- the same charge, with the same exhaustion -- instead of
            # building the rest of it first.
            budget.refuse()
            return []
    if not runs:
        return []
    texts = ["".join(parts) for _, _, parts in runs]
    if not budget.spend("".join(texts), len(texts)):
        return []
    return [
        {"text": text, "bold": bold, "italic": italic}
        for text, (bold, italic, _) in zip(texts, runs, strict=True)
    ]


def _docx_heading_level(para: Any) -> int | None:
    """Outline level of one ``w:p``, or None when it is body text."""
    props = para.find(f"{_W_NS}pPr")
    style = props.find(f"{_W_NS}pStyle") if props is not None else None
    if style is None:
        return None
    name = style.get(f"{_W_NS}val", "").replace(" ", "").replace("_", "").lower()
    if name == "title":
        return 1
    match = _HEADING_RE.match(name)
    return int(match.group(1)) if match else None


def _cell_text(cell: Any) -> str:
    """Flattened text of one table cell, paragraphs joined by newline.

    A cell's own paragraph structure is not representable in a row-major grid,
    and a nested table's text rides along on the same descendant walk rather
    than being lost.
    """
    return "\n".join(
        line
        for line in (
            "".join(t.text for t in para.iter(f"{_W_NS}t") if t.text)
            for para in cell.iter(f"{_W_NS}p")
        )
        if line
    )


def _table_block(table: Any, out: list[Block], budget: _Budget) -> None:
    """Emit one ``w:tbl`` into *out* as a row-major grid of cell text.

    The block is emitted together with its FIRST row, as one unit, and later rows
    are appended into it; a table with no rows to show is not emitted at all. A
    ROW is the unit charged and refused: its cells' text together, and one
    element per cell. Charging cell by cell left a refused row half-present --
    its first cells filled, the rest blank -- which reads as a table with empty
    cells rather than a table that was cut. A cell costs an element whether or
    not it holds text, so a grid of empty cells is bounded like any other.

    EVERY ceiling records the trim. Dropping columns silently produced a
    complete-LOOKING grid whose right-hand columns were simply absent, with
    nothing in the UI to say so -- worse than a visibly partial table, because
    nothing tells the reader to open the original.
    """
    block: Block | None = None
    for row in _sdt_children(table, f"{_W_NS}tr"):
        cells = list(_sdt_children(row, f"{_W_NS}tc"))
        # A row with no cells at all is skipped, not treated as a stop: reading
        # "nothing wanted" as "allowance spent" ended the whole loop on the first
        # such row, dropping every row after it with nothing marking the gap --
        # the silent omission this docstring exists to prevent.
        if not cells:
            continue
        texts = [_cell_text(cell) for cell in cells[:MAX_TABLE_COLS]]
        if block is None:
            block = {"type": "table", "rows": [texts], "truncated_cols": False}
            if not budget.emit(out, block, "".join(texts), len(texts) + 1):
                return
        elif budget.spend("".join(texts), len(texts)):
            block["rows"].append(texts)
        else:
            return
        if len(cells) > MAX_TABLE_COLS:
            # Recorded ON THE TABLE, not on the document. The document-level flag
            # says "only the beginning of this document", which describes running
            # out of budget part-way through; dropping right-hand columns from a
            # table that is otherwise complete is a different loss, and saying the
            # first about the second sends the reader looking for missing pages.
            block["truncated_cols"] = True


def _docx_blocks(zf: zipfile.ZipFile, budget: _Budget) -> list[Block]:
    """Walk ``word/document.xml``'s body in document order."""
    root = _parse_member(zf, "word/document.xml")
    body = root.find(f"{_W_NS}body") if root is not None else None
    if body is None:
        return []
    ordered = _docx_numbering_ordered(zf)
    style_nums = _docx_style_num_ids(zf)
    blocks: list[Block] = []
    lists = _ListRun()
    for child in _sdt_children(body):
        if budget.stop():
            break
        if child.tag == f"{_W_NS}tbl":
            lists.flush()
            _table_block(child, blocks, budget)
            continue
        if child.tag != f"{_W_NS}p":
            continue
        props = child.find(f"{_W_NS}pPr")
        num_id = _docx_list_num_id(props, style_nums)
        runs = _docx_para_runs(child, budget)
        text = "".join(run["text"] for run in runs)
        if num_id and text:
            lists.add(text, ordered.get(num_id, False), blocks, budget)
            continue
        lists.flush()
        if not runs:
            # A genuinely empty paragraph is spacing, not content. Emitting it
            # would let a document of blank paragraphs spend the block budget
            # and render as a tall column of nothing.
            continue
        level = _docx_heading_level(child)
        if level is not None:
            budget.emit(blocks, {"type": "heading", "level": level, "text": text})
        else:
            budget.emit(blocks, {"type": "paragraph", "runs": runs})
    lists.flush()
    return blocks
