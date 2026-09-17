"""Offline SpreadsheetML (.xlsx) read, targeted cell write, and reopen-verify.

This is the *local, offline* half of xlsx support. What it is NOT is as much
the point of the slice as what it is, so the contract is stated up front:

* **openpyxl does not calculate formulas.** It hands back either the value the
  writing application cached in the file, or the formula text — never a value
  this engine computed. So every number this module surfaces from a formula
  cell is labelled ``cached`` (read from the file's own value cache) and, when
  no cache is present, the formula text is returned labelled ``formula`` with
  no value at all. This module never fabricates a computed number.
* **It is not the Graph workbook engine.** Microsoft's cloud xlsx path has a
  native Graph ``workbook`` API that computes formulas server-side and speaks
  live-session semantics; that path is a *different* slice and is strictly
  preferred for the cloud scenario. This module refuses any request framed as
  "recalculate", "evaluate", or "live", because honouring it here would be
  impersonating an engine it is not.

Reads reuse the file-sheet endpoint's hardening SHAPE (magic-byte precheck,
:mod:`kiro_crew.zip_vet` inventory bound, decompressed-size caps, defusedxml
via the shared container) rather than re-deriving any of it. Targeted writes go
through :func:`container.rewrite_parts`, so an edit rewrites ONLY the worksheet
part it changes and every other part — styles, theme, sharedStrings, calcChain,
media, drawings — is carried through byte-for-byte. The write is atomic (temp
file + ``os.replace``); it never truncates the destination in place.

The write-back is verified the way the contract demands: after writing, the
destination is reopened through an INDEPENDENT read path
(:func:`read_cells`) and the edited cells are asserted to hold the new values,
so a caller never has to trust the writer's own bookkeeping.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Parsing of untrusted bytes goes through defusedxml.fromstring (the XXE
# surface); the edited worksheet subtree is re-serialised through
# defusedxml.tostring. New rows/cells are built with the parsed root's OWN
# ``makeelement`` factory (the Element API's builder — it imports nothing and
# parses nothing), so this module needs no stdlib ``xml.etree`` import at all,
# the same shape the sibling docx.py/pptx.py engines use.
from defusedxml.ElementTree import fromstring as _xml_fromstring
from defusedxml.ElementTree import tostring as _xml_tostring

from . import constants as C
from . import container
from .errors import DocumentEditError, MalformedDocument, UnsupportedDocument
from .rejection import DocumentKind, ensure_editable

__all__ = [
    "XLSX_KIND",
    "WORKBOOK_PART",
    "Cell",
    "SheetGrid",
    "XlsxContent",
    "ensure_xlsx_editable",
    "read_cells",
    "write_cells",
    "RecalculationUnsupported",
    "CloudSemanticsUnsupported",
    "refuse_recalculation",
    "refuse_cloud_semantics",
]

# The mandatory main part of a SpreadsheetML package, named by the shared
# constants module (its presence is what the shared rejection gate now resolves
# to DocumentKind.XLSX).
WORKBOOK_PART = C.XLSX_WORKBOOK_PART
# The relationships part that maps sheet r:id -> worksheet part path.
WORKBOOK_RELS_PART = "xl/_rels/workbook.xml.rels"
SHARED_STRINGS_PART = "xl/sharedStrings.xml"

# The kind the shared rejection gate resolves for a workbook.
XLSX_KIND = DocumentKind.XLSX

# SpreadsheetML namespaces (ECMA-376 / ISO-29500). Not in the sibling's
# constants.py because that module is docx/pptx-scoped; these are xlsx's own.
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
S = "{%s}" % S_NS
# The relationship id attribute lives in the officeDocument relationships ns,
# which the sibling already names as R_NS.
R_ID = "{%s}id" % C.R_NS
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
PR = "{%s}" % PKG_REL_NS

# A single cell reference like ``B12`` splits into a column-letters run and a
# 1-based row number.
_CELL_REF_RE = re.compile(r"^([A-Z]+)([1-9][0-9]*)$")

# SpreadsheetML grid limits (ECMA-376): columns A..XFD (16384), rows 1..1048576.
# A reference beyond these is not addressable in a worksheet, so writing it
# would emit an out-of-grid cell Excel treats as invalid content.
_MAX_COL_INDEX = 16384  # XFD
_MAX_ROW = 1048576


class RecalculationUnsupported(UnsupportedDocument):
    """Raised when a caller asks this offline engine to (re)compute a formula.

    openpyxl does not evaluate formulas; this module never fabricates a
    computed value. A caller wanting real recalculation must use the Graph
    workbook engine (a different slice), not this one.
    """

    reason = "recalculation_unsupported"


class CloudSemanticsUnsupported(UnsupportedDocument):
    """Raised when a caller asks this engine for live / cloud workbook semantics.

    This module handles a local file's bytes offline. It does not open a live
    Graph workbook session and must not claim to; that is a separate slice.
    """

    reason = "cloud_semantics_unsupported"


@dataclass(frozen=True)
class Cell:
    """One worksheet cell as read from the file, never as computed here.

    ``ref`` is the A1 reference (e.g. ``"B2"``). Exactly one of the value
    kinds describes it:

    * ``kind="value"`` — a literal cell value read straight from the file.
    * ``kind="cached"`` — a formula cell whose value is the number/string the
      writing application CACHED in the file. ``formula`` carries the formula
      text; ``value`` carries the cache. This engine did not compute it.
    * ``kind="formula"`` — a formula cell with NO cached value in the file.
      ``value`` is ``None`` and ``formula`` carries the text; nothing was
      computed and nothing is fabricated.
    * ``kind="empty"`` — no value and no formula.
    """

    ref: str
    kind: str
    value: object = None
    formula: str | None = None


@dataclass(frozen=True)
class SheetGrid:
    """One worksheet: its name and its populated cells in row-major order."""

    name: str
    cells: list[Cell] = field(default_factory=list)


@dataclass(frozen=True)
class XlsxContent:
    """The structured read of an .xlsx: its worksheets in workbook order."""

    sheets: list[SheetGrid] = field(default_factory=list)


# ── Rejection gate (fully delegated to the shared gate, which now resolves ───
#    the xlsx kind natively) ──────────────────────────────────────────────────


def ensure_xlsx_editable(path: str) -> str:
    """Refuse anything that is not a plain, unlocked .xlsx this engine handles.

    The whole gate is delegated to :func:`rejection.ensure_editable` with
    ``expected_kind=DocumentKind.XLSX``, not re-implemented here. That shared
    entry point refuses a sensitive path and a non-local (UNC/remote) path
    BEFORE any byte is read — so an LLM-supplied UNC target never triggers an
    outbound SMB/NTLM probe — then rejects legacy OLE2 binaries, encrypted-OOXML
    wrappers, macro-enabled extensions, signature parts, macro projects and IRM
    layers, and finally resolves the container kind from its mandatory main
    part. The shared ``classify`` now has a native ``xl/workbook.xml`` ->
    ``DocumentKind.XLSX`` branch, so a valid workbook resolves as XLSX and a
    docx/pptx handed to this entry point is refused as the wrong kind by
    ``expected_kind``.

    Returns :data:`XLSX_KIND` on success; raises the matching typed error
    (sensitive/remote, protected, unsupported, malformed) otherwise, BEFORE any
    write path touches bytes.
    """
    return ensure_editable(path, expected_kind=DocumentKind.XLSX)


# ── Read path (reuses the container's hardened part reader + defusedxml) ─────


def _shared_strings(path: str) -> list[str]:
    """Return the workbook's shared-string table, or [] when there is none.

    Cells of type ``s`` reference this table by index. The sharedStrings part is
    OPTIONAL — a workbook may inline every string — so a genuinely ABSENT part
    yields ``[]``. But a part that is PRESENT and malformed/oversized must NOT
    be swallowed into ``[]``: doing so would make every ``s``-typed cell resolve
    to ``None`` and silently drop real data from a "successful" read. So absence
    is distinguished from corruption via the member list, and only true absence
    is tolerated; a malformed present part propagates :class:`MalformedDocument`.
    """
    if SHARED_STRINGS_PART not in container.part_names(path):
        # No sharedStrings part is legal: a workbook may inline all strings.
        return []
    # Present: parse it. A malformed/oversized present part raises
    # MalformedDocument, which propagates rather than degrading to [].
    root = container.parse_xml_part(path, SHARED_STRINGS_PART)
    out: list[str] = []
    for si in root.findall(f"{S}si"):
        # A shared-string item is either a single <t> or a sequence of <r><t>.
        texts = [t.text or "" for t in si.iter(f"{S}t")]
        out.append("".join(texts))
    return out


def _worksheet_paths(path: str) -> list[tuple[str, str]]:
    """Resolve ``(sheet_name, worksheet_part_path)`` pairs in workbook order.

    Reads ``xl/workbook.xml`` for the sheet names and their r:id, then the
    workbook rels for the r:id -> part-path mapping. Both parts go through the
    hardened parser.
    """
    wb = container.parse_xml_part(path, WORKBOOK_PART)
    sheets_el = wb.find(f"{S}sheets")
    if sheets_el is None:
        return []

    rels = container.parse_xml_part(path, WORKBOOK_RELS_PART)
    rid_to_target: dict[str, str] = {}
    for rel in rels.findall(f"{PR}Relationship"):
        rid = rel.get("Id")
        target = rel.get("Target")
        if rid and target:
            rid_to_target[rid] = target

    pairs: list[tuple[str, str]] = []
    for sheet in sheets_el.findall(f"{S}sheet"):
        name = sheet.get("name") or ""
        rid = sheet.get(R_ID)
        if rid is None:
            continue
        target = rid_to_target.get(rid)
        if target is None:
            continue
        pairs.append((name, _normalize_target(target)))
    return pairs


def _normalize_target(target: str) -> str:
    """Resolve a workbook-rels ``Target`` to a package part path.

    Producers emit three shapes for the same worksheet: package-absolute
    (``/xl/worksheets/sheet1.xml``, leading slash rooted at the package),
    xl-relative (``worksheets/sheet1.xml``, relative to the workbook part's own
    ``xl/`` folder), or already ``xl/…``. All three must map to the stored
    member name ``xl/worksheets/sheet1.xml``.
    """
    if target.startswith("/"):
        # Package-absolute: strip the leading slash to get the member name.
        return target.lstrip("/")
    if target.startswith("xl/"):
        return target
    # xl-relative: the workbook part lives in xl/, so its rels resolve there.
    return f"xl/{target}"


def _cell_from_element(c_el, shared: list[str]) -> Cell:
    """Build a :class:`Cell` from a worksheet ``<c>`` element.

    Never computes anything. A formula cell (``<f>`` present) reports its cache
    only if the ``<v>`` sibling exists; otherwise it reports the formula text
    with no value. The cell type attribute ``t`` selects how ``<v>`` is read
    (shared-string index, inline string, boolean, or numeric/other literal).
    """
    ref = c_el.get("r") or ""
    ctype = c_el.get("t")  # None => numeric; "s" shared; "str"/"inlineStr" string; "b" bool
    f_el = c_el.find(f"{S}f")
    v_el = c_el.find(f"{S}v")
    is_el = c_el.find(f"{S}is")

    def _literal() -> object:
        if ctype == "s":
            # Shared-string index into the table.
            try:
                idx = int(v_el.text) if v_el is not None and v_el.text is not None else -1
            except ValueError:
                return None
            return shared[idx] if 0 <= idx < len(shared) else None
        if ctype == "inlineStr":
            if is_el is not None:
                return "".join(t.text or "" for t in is_el.iter(f"{S}t"))
            return None
        if ctype == "b":
            return bool(int(v_el.text)) if v_el is not None and v_el.text is not None else None
        if ctype == "str":
            return v_el.text if v_el is not None else None
        # Default (numeric / date-serial / error string): return the raw text.
        if v_el is None or v_el.text is None:
            return None
        raw = v_el.text
        try:
            # Prefer int when the serialization is integral, else float.
            return int(raw) if raw.lstrip("-").isdigit() else float(raw)
        except ValueError:
            return raw

    if f_el is not None:
        formula = "=" + (f_el.text or "")
        # A cached value counts ONLY when the <v> carries actual text (or an
        # inline-string cache is present). openpyxl and other producers emit an
        # EMPTY <v></v> placeholder next to a formula they did not evaluate;
        # that empty element is NOT a cache, so it must read as "formula" with
        # no value — never as a cached None the caller might mistake for a real
        # computed result.
        has_cache = (v_el is not None and v_el.text is not None and v_el.text != "") or (
            is_el is not None
        )
        if has_cache:
            return Cell(ref=ref, kind="cached", value=_literal(), formula=formula)
        # No cache: formula text only, no value fabricated.
        return Cell(ref=ref, kind="formula", value=None, formula=formula)

    if (v_el is None or v_el.text is None) and is_el is None:
        return Cell(ref=ref, kind="empty", value=None, formula=None)
    return Cell(ref=ref, kind="value", value=_literal(), formula=None)


def read_cells(path: str, *, recalculate: bool = False, live: bool = False) -> XlsxContent:
    """Read an .xlsx into structured, non-computed cell content.

    Rejection-gated. Reads the workbook, its rels, its shared strings and each
    worksheet part directly through the container's hardened, XXE-safe parser,
    so it does not depend on a whole-workbook library for the read path. Every
    formula cell is reported as ``cached`` (file's own cache) or ``formula``
    (no cache) — this function never returns a value it computed.

    The non-computation / non-cloud contract is ENFORCED at this entry point,
    not merely documented: a caller asking this offline engine to
    ``recalculate`` a formula, or for ``live`` (Graph cloud session) semantics,
    is refused up front via :func:`refuse_recalculation` /
    :func:`refuse_cloud_semantics` before any file is touched. Those refusals
    are the real seam that keeps this module from silently impersonating the
    Graph workbook engine — the ``cached``/``formula`` cell labelling is the
    other half of the same contract.
    """
    if recalculate:
        refuse_recalculation("recalculate")
    if live:
        refuse_cloud_semantics("live")
    ensure_xlsx_editable(path)
    shared = _shared_strings(path)
    known_parts = set(container.part_names(path))
    sheets: list[SheetGrid] = []
    for name, part in _worksheet_paths(path):
        if part not in known_parts:
            # A sheet is declared in workbook.xml + rels but its worksheet part
            # is absent from the package: a genuinely malformed workbook. Do NOT
            # silently drop the sheet from a "successful" read — that is exactly
            # the lossy behaviour the contract forbids.
            raise MalformedDocument(f"worksheet {name!r} references missing part {part!r}")
        # A present-but-malformed worksheet propagates MalformedDocument from
        # the hardened parser rather than being swallowed.
        ws = container.parse_xml_part(path, part)
        sheet_data = ws.find(f"{S}sheetData")
        cells: list[Cell] = []
        if sheet_data is not None:
            for row in sheet_data.findall(f"{S}row"):
                for c_el in row.findall(f"{S}c"):
                    cell = _cell_from_element(c_el, shared)
                    if cell.kind != "empty":
                        cells.append(cell)
        sheets.append(SheetGrid(name=name, cells=cells))
    return XlsxContent(sheets=sheets)


# ── Write path (targeted cell write, byte-preserving every other part) ───────


def _col_row(ref: str) -> tuple[str, int]:
    """Split an A1 reference into ``(column_letters, row_number)``.

    Raises :class:`DocumentEditError` for a malformed reference OR one outside
    the SpreadsheetML grid (column past XFD, row past 1048576), before any bytes
    are written. The column is upper-cased, so the returned tuple is the
    NORMALIZED form callers must de-duplicate on.
    """
    m = _CELL_REF_RE.match(ref.strip().upper())
    if not m:
        raise DocumentEditError(f"invalid cell reference: {ref!r}")
    col, rownum = m.group(1), int(m.group(2))
    if _col_to_index(col) > _MAX_COL_INDEX:
        raise DocumentEditError(
            f"cell reference {ref!r} is past the last column (XFD/{_MAX_COL_INDEX})"
        )
    if rownum > _MAX_ROW:
        raise DocumentEditError(f"cell reference {ref!r} is past the last row ({_MAX_ROW})")
    return col, rownum


def _col_to_index(col: str) -> int:
    """Convert column letters (``A``, ``Z``, ``AA``) to a 1-based index."""
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _resolve_worksheet_part(path: str, sheet_name: str) -> str:
    """Return the worksheet part path for *sheet_name*, or raise if absent."""
    for name, part in _worksheet_paths(path):
        if name == sheet_name:
            return part
    raise DocumentEditError(f"workbook has no sheet named {sheet_name!r}")


def _rewrite_worksheet_xml(raw: bytes, edits: dict[str, object]) -> bytes:
    """Return worksheet-part bytes with the cells in *edits* set to literal values.

    *edits* maps an A1 reference to a value (str/int/float/bool). Each addressed
    cell is rewritten as an inline value cell: a string becomes ``t="inlineStr"``
    with an ``<is><t>`` payload, a bool becomes ``t="b"``, a number becomes a
    bare numeric ``<v>``. Any prior formula/value on that cell is dropped for
    that cell only — this engine does not compute, so it will not leave a stale
    cached value beside a value it just overwrote. Rows/cells are created in
    the correct sorted position when absent. Every non-addressed cell, row,
    column definition, merge, style and the sheet's other elements are left
    exactly as parsed.

    Uses the parsed root's own ``makeelement`` factory for the surgical edit and
    re-serializes ONLY this one part through defusedxml.tostring; the container
    carries every OTHER part through byte-for-byte, so the whole-package fidelity
    is preserved even though this part is re-emitted. No stdlib xml import and no
    scan suppression are needed: parsing is defusedxml.fromstring, building is
    the parsed element's own factory, serialising is defusedxml.tostring.
    Raised through :class:`DocumentEditError` before any write.
    """
    root = _xml_fromstring(raw)
    sheet_data = root.find(f"{S}sheetData")
    if sheet_data is None:
        raise DocumentEditError("worksheet has no <sheetData> to edit")

    # Index existing rows by their 1-based row number.
    rows_by_num: dict[int, object] = {}
    for row in sheet_data.findall(f"{S}row"):
        r_attr = row.get("r")
        if r_attr and r_attr.isdigit():
            rows_by_num[int(r_attr)] = row

    for ref, value in edits.items():
        col, rownum = _col_row(ref)
        norm_ref = f"{col}{rownum}"
        row = rows_by_num.get(rownum)
        if row is None:
            row = root.makeelement(f"{S}row", {"r": str(rownum)})
            rows_by_num[rownum] = row
            _insert_sorted_row(sheet_data, row, rownum)
        c_el = _find_or_make_cell(root, row, norm_ref, col)
        _set_cell_value(root, c_el, value)

    return _xml_tostring(root, encoding="UTF-8", xml_declaration=True)


def _insert_sorted_row(sheet_data, row, rownum: int) -> None:
    """Insert *row* into *sheet_data* keeping ascending row-number order."""
    insert_at = len(list(sheet_data))
    for pos, child in enumerate(list(sheet_data)):
        if child is row:
            continue
        r_attr = child.get("r")
        if r_attr and r_attr.isdigit() and int(r_attr) > rownum:
            insert_at = pos
            break
    sheet_data.insert(insert_at, row)


def _find_or_make_cell(root, row, norm_ref: str, col: str):
    """Return the ``<c>`` for *norm_ref* in *row*, creating it in column order.

    New cells are built with the parsed root's own ``makeelement`` factory, so
    no stdlib element constructor is imported.
    """
    target_idx = _col_to_index(col)
    insert_at = len(list(row))
    for pos, c in enumerate(list(row)):
        cref = c.get("r") or ""
        if cref == norm_ref:
            return c
        m = _CELL_REF_RE.match(cref)
        if m and _col_to_index(m.group(1)) > target_idx:
            insert_at = pos
            break
    c_el = root.makeelement(f"{S}c", {"r": norm_ref})
    row.insert(insert_at, c_el)
    return c_el


def _set_cell_value(root, c_el, value: object) -> None:
    """Set *c_el* to an inline literal *value*, dropping any prior f/v children.

    Never writes a formula and never writes a cached value for a formula: this
    engine does not compute, so an edited cell carries only the literal it was
    given. New value elements are built with the parsed root's ``makeelement``
    factory.
    """
    for child in list(c_el):
        c_el.remove(child)
    # Reset the type attribute; set it per value kind below.
    if "t" in c_el.attrib:
        del c_el.attrib["t"]

    if isinstance(value, bool):
        c_el.set("t", "b")
        v = root.makeelement(f"{S}v", {})
        v.text = "1" if value else "0"
        c_el.append(v)
    elif isinstance(value, (int, float)):
        v = root.makeelement(f"{S}v", {})
        v.text = repr(value) if isinstance(value, float) else str(value)
        c_el.append(v)
    else:
        # String: inline string so no sharedStrings surgery is needed. The
        # container carries the existing sharedStrings part through untouched.
        c_el.set("t", "inlineStr")
        is_el = root.makeelement(f"{S}is", {})
        t_el = root.makeelement(
            f"{S}t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}
        )
        t_el.text = str(value)
        is_el.append(t_el)
        c_el.append(is_el)


def write_cells(
    src_path: str,
    dst_path: str,
    edits: dict[str, dict[str, object]],
    *,
    verify: bool = True,
    recalculate: bool = False,
) -> XlsxContent | None:
    """Write literal cell values into named sheets, byte-preserving every other part.

    *edits* maps ``sheet_name -> {A1_ref -> value}``. ``src_path`` and
    ``dst_path`` may be equal (in-place). Rejection-gated. Only the worksheet
    parts that actually change are rewritten; every other part — styles, theme,
    sharedStrings, calcChain, media — is carried through byte-for-byte by
    :func:`container.rewrite_parts`, which is atomic (temp file + ``os.replace``)
    so a failure never truncates the destination in place.

    This engine does NOT compute: it writes the literal values it is given. A
    leading ``=`` is written verbatim as text, not as a formula cell. The
    contract is ENFORCED at the seam, not just documented: passing
    ``recalculate=True`` (asking the engine to compute the values it writes) is
    refused up front via :func:`refuse_recalculation` before any byte moves,
    because openpyxl does not compute and this module fabricates nothing.
    Callers wanting real recalculation must use the Graph workbook engine (see
    :class:`RecalculationUnsupported`).

    When *verify* is true (the default), the destination is reopened through the
    INDEPENDENT :func:`read_cells` path and every edited cell is asserted to
    hold the written value; the verified content is returned. A mismatch raises
    :class:`DocumentEditError` — the writer's own success is never trusted
    blindly.
    """
    if recalculate:
        refuse_recalculation("recalculate")
    ensure_xlsx_editable(src_path)
    # Validate ALL edits up front so a bad edit fails before any bytes move.
    for sheet_name, cell_edits in edits.items():
        seen: dict[str, str] = {}
        for ref, value in cell_edits.items():
            col, rownum = _col_row(ref)  # grid-limit + syntax check
            norm = f"{col}{rownum}"
            # Reject reference aliasing: "a1" and "A1" both normalize to A1, so
            # writing both would overwrite one cell twice and desync the
            # reopen-verify. A duplicate normalized ref in one sheet's edits is
            # an ambiguous request, refused before any write.
            if norm in seen:
                raise DocumentEditError(
                    f"duplicate cell reference in edits for sheet {sheet_name!r}: "
                    f"{seen[norm]!r} and {ref!r} both address {norm}"
                )
            seen[norm] = ref
            # Reject XML-illegal characters in string values BEFORE any bytes
            # move: an unfiltered control char would serialize to invalid XML,
            # the atomic replace would commit it (destroying the original when
            # src==dst), and only the post-write reopen-verify would then fail —
            # too late. The docx/pptx siblings validate the same way.
            if not isinstance(value, bool) and isinstance(value, str):
                bad = C.find_illegal_xml_char(value)
                if bad is not None:
                    raise DocumentEditError(
                        f"edit for {sheet_name}!{norm} contains an XML-illegal "
                        f"character {bad!r}; refusing before any write"
                    )

    replacements: dict[str, bytes] = {}
    for sheet_name, cell_edits in edits.items():
        if not cell_edits:
            continue
        part = _resolve_worksheet_part(src_path, sheet_name)
        raw = container.read_part(src_path, part)
        replacements[part] = _rewrite_worksheet_xml(raw, cell_edits)

    if not replacements:
        # An empty edit set (no sheets, or every sheet mapping empty) is a no-op
        # write. Rather than silently rewrite the workbook to itself — an
        # undeclared file-copy side effect — refuse it: a "write" that changes
        # nothing is a caller error, and this engine never moves bytes it was
        # not asked to change.
        raise DocumentEditError("no cell edits supplied; nothing to write")

    container.rewrite_parts(src_path, dst_path, replacements)

    if not verify:
        return None
    return _verify_written(dst_path, edits)


def _verify_written(dst_path: str, edits: dict[str, dict[str, object]]) -> XlsxContent:
    """Reopen *dst_path* via the independent read path and assert every edit landed.

    Raises :class:`DocumentEditError` if any edited cell is missing or holds a
    value other than the one written (numbers compared numerically, everything
    else by string form). Returns the freshly-read content on success.
    """
    content = read_cells(dst_path)
    by_sheet: dict[str, dict[str, Cell]] = {}
    for grid in content.sheets:
        by_sheet[grid.name] = {c.ref: c for c in grid.cells}

    for sheet_name, cell_edits in edits.items():
        got_cells = by_sheet.get(sheet_name, {})
        for ref, expected in cell_edits.items():
            col, rownum = _col_row(ref)
            norm_ref = f"{col}{rownum}"
            cell = got_cells.get(norm_ref)
            if cell is None:
                raise DocumentEditError(
                    f"reopen-verify failed: {sheet_name}!{norm_ref} absent after write"
                )
            if not _values_match(cell.value, expected):
                raise DocumentEditError(
                    f"reopen-verify failed: {sheet_name}!{norm_ref} holds "
                    f"{cell.value!r}, expected {expected!r}"
                )
    return content


def _values_match(got: object, expected: object) -> bool:
    """Whether a read-back value matches what was written."""
    if isinstance(expected, bool):
        return bool(got) == expected
    if isinstance(expected, (int, float)) and isinstance(got, (int, float)):
        return float(got) == float(expected)
    return str(got) == str(expected)


# ── Explicit non-computation / non-cloud contract, enforced at the seams ─────
#
# These are NOT standalone helpers: read_cells(recalculate=/live=) and
# write_cells(recalculate=) call them, so they are the real enforcement point
# of this offline engine's contract. The other half of the contract is the
# read path's cached/formula cell labelling (never a fabricated computed value)
# and the write path's literal-only cells (a leading "=" is text, not a
# formula). Together they are what keeps this module from silently
# impersonating the Graph workbook engine.


def refuse_recalculation(what: str = "recalculate") -> None:
    """Refuse a request to compute/evaluate formulas in this offline engine.

    Called by :func:`read_cells` (``recalculate=True``) and :func:`write_cells`
    (``recalculate=True``): anything framed as recalculation, evaluation, or
    "give me the computed result" is a request this module cannot honestly
    answer, because openpyxl does not compute and this module fabricates
    nothing. Always raises :class:`RecalculationUnsupported`.
    """
    raise RecalculationUnsupported(
        f"this offline xlsx engine cannot {what} formulas; it only reports the "
        "value cached in the file or the formula text. Use the Graph workbook "
        "engine for server-side calculation."
    )


def refuse_cloud_semantics(what: str = "live") -> None:
    """Refuse a request for live / cloud (Graph workbook) semantics.

    Called by :func:`read_cells` (``live=True``). Always raises
    :class:`CloudSemanticsUnsupported`; this module is the local, offline path
    and does not impersonate the Graph workbook engine.
    """
    raise CloudSemanticsUnsupported(
        f"this offline xlsx engine does not provide {what} workbook semantics; "
        "that is the Graph workbook engine's slice."
    )
