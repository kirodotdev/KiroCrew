"""Structured block extraction for .docx OOXML documents.

:mod:`kiro_crew.doc_parser` flattens a document to one plaintext string, which is
what an agent reading an attachment wants. The dashboard's file viewer wants the
structure back, so this module returns a JSON-serializable block list instead.

Hardening is inherited, not re-spelled: the archive-inventory preflight and the
bounded per-entry reader are doc_parser's, imported directly so a change to
either lands here too, and XML parsing goes through the same defusedxml
``fromstring`` (the stdlib parser resolves external entities, an XXE on a
user-supplied document). What this module adds is a bound on what ONE document
can turn INTO -- a block count and an aggregate character budget -- because a
structured payload becomes DOM nodes, so an unbounded block count is a
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

#: Blocks returned for one document. Well above a long report: a 100-page
#: document is in the low thousands of paragraphs.
MAX_BLOCKS = 4000
#: Aggregate characters across every block's text. Mirrors the text preview's own
#: cap, so choosing a format cannot widen what one request extracts.
MAX_CHARS = 512_000
#: Per-block ceilings, so one crafted list or table cannot become the whole
#: block budget by itself.
MAX_LIST_ITEMS = 500
MAX_TABLE_ROWS = 200
MAX_TABLE_COLS = 40

# ── OOXML namespaces ──

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
#: ``Heading3``, ``heading 3`` and ``Heading_3`` all name level 3. Word writes
#: the style id without a space; documents from other writers do not.
_HEADING_RE = re.compile(r"^heading([1-9])$")

Block = dict[str, Any]


class _Budget:
    """Shared block/character allowance for one document's extraction.

    Held by the walkers rather than checked by their caller because a cap has to
    stop work BEFORE the next member is decompressed and parsed: a caller that
    trims an over-budget result has already paid to build it.
    """

    def __init__(self) -> None:
        # Read at construction rather than bound as defaults: the caps are
        # module-level policy, and a default argument would freeze whatever they
        # were at import time.
        self.blocks_left = MAX_BLOCKS
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
        if self.blocks_left > 0 and self.chars_left > 0:
            return False
        self.truncated = True
        return True

    def spend(self, text: str) -> str:
        """Charge *text*, or return "" and charge nothing when it does not fit.

        Charges BEFORE the string is appended, because one run, one table cell or
        one slide paragraph can be megabytes by itself -- bounded only by
        doc_parser's 50 MB per-entry decompression cap -- so charging afterwards
        left the allowance overrun by that whole string, the payload exceeding
        MAX_CHARS a hundredfold while every counter still read as though it had
        not.

        WHOLE OR NOTHING, never a prefix. Redaction runs on these strings later
        and downstream, and it matches patterns: a string cut to fit can carry a
        partial secret that those patterns miss, so the fragment survives
        redaction and ships. Cutting at a "safe" boundary only narrows the
        window, since a configured literal secret value may itself contain
        whitespace; refusing the string outright covers every secret shape at
        once, and leaves this module free of any notion of what a secret looks
        like. The cost is bounded: ``stop()`` halts extraction immediately after,
        so the refused string is the LAST one, and ``truncated`` already reports
        the result as partial.
        """
        if len(text) > self.chars_left:
            self.truncated = True
            return ""
        self.chars_left -= len(text)
        return text

    def spend_cell(self, text: str) -> str:
        """Charge one table cell, never less than one character.

        A cell costs the frontend a ``<td>`` node whether or not it holds text,
        so charging only ``len(text)`` let an EMPTY cell through free: empty
        cells spent nothing, ``stop()`` never fired, and a document of empty
        tables could build MAX_TABLE_ROWS x MAX_TABLE_COLS grid entries for every
        one of MAX_BLOCKS blocks -- tens of millions of them from an 88 KB
        upload, bounded by no counter at all. A minimum charge puts cells under
        the same aggregate ceiling as text, which is one counter rather than a
        private ceiling of their own.
        """
        kept = self.spend(text)
        if not kept:
            self.chars_left = max(self.chars_left - 1, 0)
        return kept

    def emit(self, blocks: list[Block], block: Block) -> bool:
        """Append *block* and charge it, or REFUSE once the allowance is spent.

        Returns False without appending when there is no allowance left, so a
        caller in a loop can stop. Refusing here rather than trusting each
        caller's own check is what makes the cap hold: every walker appends
        through this one method, so no loop can emit past the allowance by
        forgetting to check at its own top.
        """
        if self.blocks_left <= 0:
            self.truncated = True
            return False
        self.blocks_left -= 1
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


def _merge_runs(runs: list[Block]) -> list[Block]:
    """Collapse adjacent runs with identical formatting into one.

    Word splits a run at every revision, spell-check span and language change, so
    a plain sentence routinely arrives as a dozen identically-formatted runs.
    Merging keeps the payload proportional to the text.
    """
    out: list[Block] = []
    for run in runs:
        if out and out[-1]["bold"] == run["bold"] and out[-1]["italic"] == run["italic"]:
            out[-1]["text"] += run["text"]
        else:
            out.append(run)
    return out


class _ListRun:
    """Accumulator that turns consecutive list paragraphs into one list block.

    Word and PowerPoint both mark list membership per PARAGRAPH, so a
    three-bullet list arrives as three paragraphs; without this the renderer
    emits three single-item lists, each with its own marker column.
    """

    def __init__(self) -> None:
        self.items: list[str] = []
        self.ordered = False

    def add(self, text: str, ordered: bool, out: list[Block], budget: _Budget) -> None:
        """Extend the run, flushing first when the kind changes or it is full."""
        if self.items and (ordered != self.ordered or len(self.items) >= MAX_LIST_ITEMS):
            self.flush(out, budget)
        self.ordered = ordered
        self.items.append(text)

    def flush(self, out: list[Block], budget: _Budget) -> None:
        """Emit the accumulated items as one block, if there are any."""
        if not self.items:
            return
        budget.emit(out, {"type": "list", "ordered": self.ordered, "items": self.items})
        self.items = []


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


def _docx_body_children(body: Any) -> Iterator[Any]:
    """Body-level paragraphs and tables, descending through content controls.

    A template-produced document wraps whole sections in ``w:sdt`` (a structured
    document tag), whose paragraphs are not direct children of the body, so
    walking only direct children silently drops everything inside one.
    """
    for child in body:
        if child.tag == f"{_W_NS}sdt":
            content = child.find(f"{_W_NS}sdtContent")
            if content is not None:
                yield from _docx_body_children(content)
            continue
        yield child


def _docx_para_runs(para: Any, budget: _Budget) -> list[Block]:
    """Formatted runs of one ``w:p``, bold/italic preserved."""
    runs: list[Block] = []
    for run in para.iter(f"{_W_NS}r"):
        text = "".join(t.text for t in run.iter(f"{_W_NS}t") if t.text)
        if not text:
            continue
        props = run.find(f"{_W_NS}rPr")
        bold = props is not None and _toggle_on(props, f"{_W_NS}b")
        italic = props is not None and _toggle_on(props, f"{_W_NS}i")
        text = budget.spend(text)
        if not text:
            # The run had text before the charge, so an empty result means the
            # allowance is gone -- not an empty run, which is skipped above.
            break
        runs.append({"text": text, "bold": bold, "italic": italic})
        if budget.stop():
            break
    return _merge_runs(runs)


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


def _cell_text(cell: Any, ns: str, budget: _Budget) -> str:
    """Flattened text of one table cell, paragraphs joined by newline.

    A cell's own paragraph structure is not representable in a row-major grid,
    and a nested table's text rides along on the same descendant walk rather
    than being lost.
    """
    text = "\n".join(
        line
        for line in (
            "".join(t.text for t in para.iter(f"{ns}t") if t.text) for para in cell.iter(f"{ns}p")
        )
        if line
    )
    return budget.spend_cell(text)


def _row_cells(row: Any, ns: str) -> Iterator[Any]:
    """Cells of one table row, descending through content controls.

    The same rule ``_docx_body_children`` applies to the body, for the same
    reason: a template wraps cells in ``w:sdt``, so reading only direct children
    finds none and drops the row. Deliberately NOT ``row.iter(tc)`` -- that would
    also collect a NESTED table's cells as the outer row's own, duplicating text
    a cell's descendant walk already flattens and inventing columns.
    """
    for child in row:
        if child.tag == f"{ns}sdt":
            content = child.find(f"{ns}sdtContent")
            if content is not None:
                yield from _row_cells(content, ns)
            continue
        if child.tag == f"{ns}tc":
            yield child


def _table_block(table: Any, ns: str, budget: _Budget) -> Block:
    """One ``w:tbl`` / ``a:tbl`` as a row-major grid of cell text.

    EVERY ceiling records the trim. Dropping columns silently produced a
    complete-LOOKING grid whose right-hand columns were simply absent, with
    nothing in the UI to say so -- worse than a visibly partial table, because
    nothing tells the reader to open the original.
    """
    rows: list[list[str]] = []
    narrowed = False
    for row in table.findall(f"{ns}tr"):
        cells = list(_row_cells(row, ns))
        # A row with no cells at all is skipped, not treated as a stop: reading
        # "nothing wanted" as "allowance spent" ended the whole loop on the first
        # such row, dropping every row after it with nothing marking the gap --
        # the silent omission this docstring exists to prevent.
        if not cells:
            continue
        if len(cells) > MAX_TABLE_COLS:
            # Recorded ON THE TABLE, not on the document. The document-level flag
            # says "only the beginning of this document", which describes running
            # out of budget part-way through; dropping right-hand columns from a
            # table that is otherwise complete is a different loss, and saying the
            # first about the second sends the reader looking for missing pages.
            narrowed = True
        rows.append([_cell_text(cell, ns, budget) for cell in cells[:MAX_TABLE_COLS]])
        if len(rows) >= MAX_TABLE_ROWS:
            budget.truncated = True
            break
        if budget.stop():
            break
    return {"type": "table", "rows": rows, "truncated_cols": narrowed}


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
    for child in _docx_body_children(body):
        if budget.stop():
            break
        if child.tag == f"{_W_NS}tbl":
            lists.flush(blocks, budget)
            budget.emit(blocks, _table_block(child, _W_NS, budget))
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
        lists.flush(blocks, budget)
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
    lists.flush(blocks, budget)
    return blocks
