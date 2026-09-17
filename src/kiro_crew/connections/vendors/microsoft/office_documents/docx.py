"""Offline WordprocessingML (.docx) read, template-create and in-place edit.

Two deliberately different code paths, for the reason W07's contract states:

* **Structured read** (:func:`read_document`) walks ``word/document.xml`` for
  paragraphs and tables through the container's own hardened part parse
  (``container.parse_xml_part``, ``defusedxml``). It deliberately does NOT route
  the read through ``python-docx``: keeping read and edit on one hardened parse
  path means a crafted part meets the same XXE/zip defenses either way, and the
  read has no dependency the edit path lacks. (``python-docx`` is still declared
  and used as the INDEPENDENT reopen reader in the tests, not in this path.)

* **Targeted in-place edit** (:func:`replace_paragraph_text`) does NOT round-trip
  the whole package through a library. It rewrites ONLY ``word/document.xml`` at
  the part level and copies every other part byte-for-byte (see
  ``container.rewrite_parts``). A whole-package re-serialize would drop or
  reorder theme/styles/media/customXml that the authoring app wrote, so the
  fidelity contract requires the part-level path.

Template creation (:func:`create_from_template`) is a rejection-gated verbatim
copy of a local template file with an optional set of paragraph substitutions
applied through the same part-level editor — never a fresh synthesis that would
lose the template's styling.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Parsing of untrusted bytes always goes through defusedxml.fromstring (the XXE
# surface); serialising the edited subtree uses defusedxml.tostring. New
# elements are built with the parsed root's own ``makeelement`` factory, so no
# ElementTree builder needs importing — the module imports no stdlib xml symbol.
from defusedxml.ElementTree import fromstring as _xml_fromstring
from defusedxml.ElementTree import tostring as _xml_tostring

from . import constants as C
from . import container
from .errors import DocumentEditError, MalformedDocument
from .rejection import DocumentKind, ensure_editable

__all__ = [
    "Paragraph",
    "Table",
    "DocxContent",
    "read_document",
    "create_from_template",
    "replace_paragraph_text",
]


# The textual-content children of a <w:r> run. A paragraph edit collapses only
# these to the new text; every other run child (<w:rPr>, and embedded
# <w:drawing>/<w:pict>/<w:object> graphics or field content) is left in place so
# a text-and-picture run does not lose its picture. Namespaced with the W URI.
_RUN_TEXTUAL_TAGS = frozenset(
    f"{C.W}{tag}" for tag in ("t", "br", "tab", "cr", "noBreakHyphen", "softHyphen")
)


def _parent_of(root, target):
    """Return the element whose direct child is *target*, or ``None``.

    ElementTree elements carry no parent pointer, so removing a run that may be
    nested inside a ``<w:hyperlink>`` (not a direct ``<w:p>`` child) needs an
    explicit walk to find its container before ``parent.remove(target)``.
    """
    for parent in root.iter():
        for child in parent:
            if child is target:
                return parent
    return None


@dataclass(frozen=True)
class Paragraph:
    """One WordprocessingML paragraph: its plain text and its style name."""

    index: int
    text: str
    style: str = ""


@dataclass(frozen=True)
class Table:
    """One table as a row-major grid of cell strings."""

    index: int
    rows: list[list[str]] = field(default_factory=list)


@dataclass(frozen=True)
class DocxContent:
    """The structured read of a .docx: ordered paragraphs and tables."""

    paragraphs: list[Paragraph] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)

    @property
    def text(self) -> str:
        """All paragraph text joined with newlines (tables excluded)."""
        return "\n".join(p.text for p in self.paragraphs)


def _paragraph_text(p_elem) -> str:
    """Concatenate the ``<w:t>`` runs under a ``<w:p>`` element."""
    parts: list[str] = []
    for t in p_elem.iter(f"{C.W}t"):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


def _paragraph_style(p_elem) -> str:
    """The referenced paragraph-style id, or '' when none is set."""
    pPr = p_elem.find(f"{C.W}pPr")
    if pPr is None:
        return ""
    pStyle = pPr.find(f"{C.W}pStyle")
    if pStyle is None:
        return ""
    return pStyle.get(f"{C.W}val", "")


def read_document(path: str) -> DocxContent:
    """Read a .docx into structured paragraphs and tables.

    Rejection-gated: a protected/unsupported/malformed file raises the matching
    typed error before any parse. Reads ``word/document.xml`` directly with the
    hardened parser so it does not depend on a whole-document library for the
    read path, and walks the body in document order.
    """
    ensure_editable(path, expected_kind=DocumentKind.DOCX)
    root = container.parse_xml_part(path, C.DOCX_MAIN_PART)
    body = root.find(f"{C.W}body")
    if body is None:
        return DocxContent()

    paragraphs: list[Paragraph] = []
    tables: list[Table] = []
    p_index = 0
    t_index = 0
    for child in body:
        tag = child.tag
        if tag == f"{C.W}p":
            paragraphs.append(
                Paragraph(
                    index=p_index,
                    text=_paragraph_text(child),
                    style=_paragraph_style(child),
                )
            )
            p_index += 1
        elif tag == f"{C.W}tbl":
            rows: list[list[str]] = []
            for tr in child.findall(f"{C.W}tr"):
                cells: list[str] = []
                for tc in tr.findall(f"{C.W}tc"):
                    cell_text = "\n".join(_paragraph_text(p) for p in tc.findall(f"{C.W}p"))
                    cells.append(cell_text)
                rows.append(cells)
            tables.append(Table(index=t_index, rows=rows))
            t_index += 1
    return DocxContent(paragraphs=paragraphs, tables=tables)


def _rewrite_document_xml(raw: bytes, edits: dict[int, str]) -> bytes:
    """Return ``word/document.xml`` bytes with paragraph *edits* applied.

    *edits* maps a zero-based body-paragraph index to its new plain text. The
    rewrite touches ONLY the addressed paragraphs' runs: it collapses each
    target paragraph's runs to a single ``<w:r><w:t>`` carrying the new text,
    preserving the paragraph's ``<w:pPr>`` (style, numbering) so formatting
    survives. Non-addressed paragraphs, tables, sectPr and everything else are
    left exactly as parsed.

    Raised through :class:`DocumentEditError` when an index is out of range, so
    the caller learns before any bytes are written.
    """
    try:
        root = _xml_fromstring(raw)
    except Exception as exc:  # defusedxml raises several distinct types
        raise MalformedDocument(f"word/document.xml is not well-formed XML: {exc}") from exc
    body = root.find(f"{C.W}body")
    if body is None:
        raise DocumentEditError("document has no <w:body> to edit")

    p_elems = [child for child in body if child.tag == f"{C.W}p"]
    max_index = len(p_elems) - 1
    for idx, new_text in edits.items():
        if idx < 0 or idx > max_index:
            raise DocumentEditError(f"paragraph index {idx} out of range (0..{max_index})")
        bad = C.find_illegal_xml_char(new_text)
        if bad is not None:
            raise DocumentEditError(
                f"paragraph {idx} replacement contains an XML-illegal "
                f"character U+{ord(bad):04X}; refusing to write unreopenable output"
            )

    for idx, new_text in edits.items():
        p = p_elems[idx]
        # Replace the text of the paragraph's FIRST text-bearing run and drop the
        # OTHER text-bearing runs, so the paragraph reads as the new text — but
        # keep every non-text child (pPr, drawings, bookmarks, hyperlinks, field
        # runs, and runs that carry no <w:t>) exactly where it was. Removing all
        # children would silently delete embedded content the edit never named.
        # Runs can be direct <w:p> children OR nested inside <w:hyperlink>, so
        # scan the paragraph subtree, not just direct children. New elements are
        # built with the parsed root's own ``makeelement`` factory (the Element
        # API's builder — it imports nothing and parses nothing).
        text_runs = [r for r in p.iter(f"{C.W}r") if r.find(f"{C.W}t") is not None]
        if text_runs:
            first = text_runs[0]
            # Collapse the first text run's TEXTUAL content to one <w:t> holding
            # the new text, but keep every non-textual child exactly where it is.
            # Remove ONLY the text-content nodes (<w:t>/<w:br>/<w:tab>/<w:cr>/
            # <w:noBreakHyphen>); a run may also carry embedded graphics
            # (<w:drawing>/<w:pict>/<w:object>) or field content alongside its
            # text, and stripping everything but <w:rPr> would silently delete
            # that content the edit never named. <w:rPr> (run properties —
            # bold/italic/color/font) is a non-textual child and is preserved
            # by the same rule, keeping the edited text's character formatting.
            rPr = first.find(f"{C.W}rPr")
            for sub in list(first):
                if sub.tag in _RUN_TEXTUAL_TAGS:
                    first.remove(sub)
            t = root.makeelement(f"{C.W}t", {})
            first.append(t)
            # <w:rPr> must stay first per schema; the new <w:t> was appended after
            # it, which is already correct when rPr is present.
            if rPr is not None and list(first)[0] is not rPr:
                first.remove(rPr)
                first.insert(0, rPr)
            # The OTHER text-bearing runs: strip only their TEXTUAL nodes so the
            # paragraph reads as the single new text, but KEEP a run that also
            # carries embedded content (<w:drawing>/<w:pict>/<w:object>) — removing
            # the whole run would permanently lose the graphic. A run left with
            # nothing but <w:rPr> (or empty) after stripping carried only text, so
            # it is removed to avoid a dangling empty run.
            for extra in text_runs[1:]:
                for sub in list(extra):
                    if sub.tag in _RUN_TEXTUAL_TAGS:
                        extra.remove(sub)
                remaining = [c for c in extra if c.tag != f"{C.W}rPr"]
                if not remaining:
                    parent = _parent_of(p, extra)
                    if parent is not None:
                        parent.remove(extra)
        else:
            # No text run exists yet: append one after any existing children,
            # keeping pPr first per schema.
            pPr = p.find(f"{C.W}pPr")
            run = root.makeelement(f"{C.W}r", {})
            p.append(run)
            t = root.makeelement(f"{C.W}t", {})
            run.append(t)
            if pPr is not None and list(p)[0] is not pPr:
                p.remove(pPr)
                p.insert(0, pPr)
        # Preserve leading/trailing whitespace exactly as Word requires.
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        t.text = new_text

    return _xml_tostring(root, encoding="UTF-8", xml_declaration=True)


def replace_paragraph_text(
    src_path: str,
    dst_path: str,
    edits: dict[int, str],
) -> None:
    """Replace the text of specific paragraphs, byte-preserving every other part.

    *edits* maps zero-based body-paragraph index to new text. ``src_path`` and
    ``dst_path`` may be equal (in-place). Rejection-gated. Raises
    :class:`DocumentEditError` for an out-of-range index BEFORE writing, so a
    bad edit never truncates the destination.
    """
    ensure_editable(src_path, expected_kind=DocumentKind.DOCX)
    # Pin the source's signature BEFORE reading the part we edit, so a source
    # autosave between our read and rewrite_parts' reopen is detected rather
    # than published as a hybrid (our edited main part fused with newer other
    # parts). rewrite_parts reads EVERY untouched part from the source at
    # publish time, so this race exists for a distinct destination too, not
    # only for an in-place edit — pin unconditionally (F2).
    sig = container.source_signature(src_path)
    if not edits:
        # A no-op edit still produces dst as a faithful copy of src.
        raw = container.read_part(src_path, C.DOCX_MAIN_PART)
        container.rewrite_parts(
            src_path, dst_path, {C.DOCX_MAIN_PART: raw}, expect_source_signature=sig
        )
        return
    raw = container.read_part(src_path, C.DOCX_MAIN_PART)
    new_xml = _rewrite_document_xml(raw, edits)
    container.rewrite_parts(
        src_path, dst_path, {C.DOCX_MAIN_PART: new_xml}, expect_source_signature=sig
    )


def create_from_template(
    template_path: str,
    dst_path: str,
    edits: dict[int, str] | None = None,
) -> None:
    """Create a new .docx from a local template, optionally substituting text.

    The template is a real .docx on disk (rejection-gated). Its every part is
    carried into the new file byte-for-byte; only the paragraphs named in
    *edits* are changed, through the same part-level editor
    (:func:`replace_paragraph_text`). This preserves the template's styles,
    theme and layout — a template create is a faithful copy plus targeted fills,
    never a fresh synthesis.
    """
    ensure_editable(template_path, expected_kind=DocumentKind.DOCX)
    replace_paragraph_text(template_path, dst_path, edits or {})
