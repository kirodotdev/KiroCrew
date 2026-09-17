"""Offline PresentationML (.pptx) read, template-create and in-place edit.

Pure standard library (``zipfile`` + hardened XML): ``python-pptx`` is
deliberately NOT a dependency of the gateway process, and adding it — or adding
``.pptx`` to the knowledge folder-scan ``SUPPORTED`` set — is an out-of-scope
behavior change (Dependency License Gate, bundle size, folder-scan semantics).
This module reads a deck by walking its slide parts, exactly matching how a pptx
is actually structured: there is no single presentation-body endpoint, each
slide is its own ``ppt/slides/slideN.xml`` part, and a slide's speaker notes
live in a separate ``ppt/notesSlides/notesSlideN.xml`` part reached through the
slide's relationships file.

Edits and template creation follow the docx module's discipline: rewrite only
the addressed slide part(s), copy every other part byte-for-byte.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

# Parsing of untrusted bytes always goes through defusedxml.fromstring (the XXE
# surface); serialising uses defusedxml.tostring. The edit mutates existing
# elements in place (no new elements built), so no ElementTree builder or
# namespace helper is imported — the module imports no stdlib xml symbol.
from defusedxml.ElementTree import fromstring as _xml_fromstring
from defusedxml.ElementTree import tostring as _xml_tostring

from . import constants as C
from . import container
from .errors import DocumentEditError, MalformedDocument
from .rejection import DocumentKind, ensure_editable

__all__ = [
    "Slide",
    "PptxContent",
    "read_presentation",
    "create_from_template",
    "replace_slide_text",
]

_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_NOTES_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/notesSlide"
_SLIDE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"


@dataclass(frozen=True)
class Slide:
    """One slide: its number, body text runs, and speaker notes."""

    number: int
    text: str = ""
    notes: str = ""


@dataclass(frozen=True)
class PptxContent:
    """The structured read of a .pptx: slides in presentation order."""

    slides: list[Slide] = field(default_factory=list)

    @property
    def text(self) -> str:
        """All slide body text joined, one block per slide."""
        return "\n\n".join(s.text for s in self.slides if s.text)


def _slides_by_filename_number(names: list[str]) -> list[str]:
    """Slide parts ordered by their numeric filename (a stable fallback order)."""
    numbered: list[tuple[int, str]] = []
    for name in names:
        m = _SLIDE_RE.match(name)
        if m:
            numbered.append((int(m.group(1)), name))
    numbered.sort(key=lambda pair: pair[0])
    return [name for _, name in numbered]


def _slide_parts(path: str, names: list[str]) -> list[tuple[int, str]]:
    """Return ``(position, part_name)`` for each slide in PRESENTATION order.

    A deck's presentation order is defined by ``<p:sldIdLst>`` in
    ``ppt/presentation.xml`` — each ``<p:sldId r:id=...>`` resolves through
    ``ppt/_rels/presentation.xml.rels`` to a slide part — NOT by the numeric
    ``slideN.xml`` filename: a reordered deck keeps its filenames and only
    rewrites the id list, so filename order would report the wrong slide. The
    returned ``position`` is 1-based presentation position, which is what a user
    means by "slide N".

    Two failure modes, deliberately different:

    * **No stated order** (``<p:sldIdLst>`` / its rels / the presentation part is
      ABSENT): there is no authority to contradict, so filename-number order is
      the legitimate fallback.
    * **A stated order that is BROKEN** (a ``<p:sldId>`` whose ``r:id`` resolves
      to no slide): :func:`_slide_order_via_sldidlst` raises
      :class:`MalformedDocument` and we do NOT fall back — guessing filename
      order there would silently redirect a read/edit to the wrong slide, a
      data-corruption fail-open. Failing closed is consistent with the gate.
    """
    name_set = set(names)
    ordered = _slide_order_via_sldidlst(path, name_set)
    if ordered is None:
        ordered = _slides_by_filename_number(names)
    return [(pos, part) for pos, part in enumerate(ordered, start=1)]


def _slide_order_via_sldidlst(path: str, names: set[str]) -> list[str] | None:
    """Resolve slide parts in ``sldIdLst`` order.

    Returns the ordered slide part names when a ``<p:sldIdLst>`` states the
    order. Returns ``None`` when NO authoritative order is stated at all — the
    presentation part, its rels, or the ``<p:sldIdLst>`` element is simply
    ABSENT — in which case filename order is the only available signal and the
    caller may legitimately use it.

    Raises :class:`MalformedDocument` when an order IS stated but is BROKEN — a
    ``<p:sldId>`` whose ``r:id`` resolves to no slide relationship (a dangling
    id). That is present-but-invalid, and guessing filename order there would
    silently edit or read the wrong slide, so we fail closed rather than fall
    back.
    """
    if C.PPTX_PRESENTATION_PART not in names:
        return None
    rels_part = "ppt/_rels/presentation.xml.rels"
    if rels_part not in names:
        return None
    # Both parts are PRESENT (checked above). A parse failure of a present part
    # is a BROKEN package, not an absent order: let parse_xml_part's
    # MalformedDocument propagate (fail closed) rather than swallowing it into a
    # filename-order guess that could target the wrong slide. Absence is handled
    # by the two early returns above; only a genuinely missing part falls back.
    pres_root = container.parse_xml_part(path, C.PPTX_PRESENTATION_PART)
    rels_root = container.parse_xml_part(path, rels_part)

    rid_to_part: dict[str, str] = {}
    for rel in rels_root:
        if rel.get("Type") == _SLIDE_REL_TYPE:
            target = _normalize_rel_target("ppt/", rel.get("Target", ""))
            rid = rel.get("Id")
            if rid and target in names:
                rid_to_part[rid] = target

    r_embed = f"{C.R}id"
    ordered: list[str] = []
    sld_id_lst = pres_root.find(f"{C.P}sldIdLst")
    if sld_id_lst is None:
        return None
    for sld_id in sld_id_lst:
        if sld_id.tag != f"{C.P}sldId":
            continue
        rid = sld_id.get(r_embed)
        part = rid_to_part.get(rid or "")
        if part is None:
            # A stated slide whose r:id resolves to nothing: present-but-invalid.
            # Fail closed — filename order would silently target the wrong slide.
            raise MalformedDocument(
                f"presentation lists a slide (r:id={rid!r}) that resolves to no "
                "slide relationship; refusing to guess slide order"
            )
        ordered.append(part)
    # sldIdLst IS present (the absent case returned None above). Return the
    # resolved order even when it is EMPTY — a present-but-empty <p:sldIdLst>
    # means the deck states it has no slides in its order, so filename-order
    # fallback would read/edit an orphan slide part the presentation excludes.
    # None is reserved for a genuinely ABSENT list.
    return ordered


def _text_of(root) -> str:
    """Concatenate DrawingML ``<a:t>`` runs, one line per run-bearing paragraph."""
    lines: list[str] = []
    for para in root.iter(f"{C.A}p"):
        runs = [t.text for t in para.iter(f"{C.A}t") if t.text]
        if runs:
            lines.append("".join(runs))
    return "\n".join(lines)


def _notes_part_for_slide(path: str, slide_part: str, names: set[str]) -> str | None:
    """Resolve a slide's notes part via its ``.rels``, or None if it has none.

    A slide ``ppt/slides/slideN.xml`` carries its relationships in
    ``ppt/slides/_rels/slideN.xml.rels``; the notes relationship's Target is
    resolved relative to ``ppt/slides/``.

    Returns ``None`` ONLY when the slide states no notes relationship at all (no
    rels part, or a rels part with no ``notesSlide`` relationship) — that is a
    slide legitimately without speaker notes. A notes relationship that IS
    stated but is BROKEN — a rels part that will not parse, or a ``notesSlide``
    Target that resolves to a part absent from the package (dangling) — fails
    closed with :class:`MalformedDocument`, because silently returning ``None``
    there would report a slide that HAS notes as having none, losing content.
    """
    m = _SLIDE_RE.match(slide_part)
    if not m:
        return None
    rels_part = f"ppt/slides/_rels/slide{m.group(1)}.xml.rels"
    if rels_part not in names:
        return None
    # rels part is PRESENT: a parse failure is a broken package (parse_xml_part
    # raises MalformedDocument), not "no notes" — let it propagate (fail closed).
    rels_root = container.parse_xml_part(path, rels_part)
    for rel in rels_root:
        if rel.get("Type") == _NOTES_REL_TYPE:
            target = rel.get("Target", "")
            # Targets are relative to ppt/slides/; normalize ../notesSlides/...
            normalized = _normalize_rel_target("ppt/slides/", target)
            if normalized in names:
                return normalized
            # A notes relationship IS stated but its Target resolves to no part:
            # dangling. Fail closed rather than silently report "no notes".
            raise MalformedDocument(
                f"slide {slide_part!r} states a notes relationship whose target "
                f"{target!r} resolves to no package part; refusing to report it "
                "as having no notes"
            )
    return None


def _normalize_rel_target(base_dir: str, target: str) -> str:
    """Resolve a relationship Target against *base_dir* into a package part path.

    OOXML package part names are always POSIX-style ("/"-separated) regardless
    of host OS, so ``posixpath`` is the correct tool: ``join`` composes the
    reference and ``normpath`` collapses ``.``/``..`` and redundant separators.
    A ``..`` sequence that would escape the package root cannot name a real
    part, so a normalized result that still points above the root ("" / "."
    / a leading "..") resolves to no part (empty string).
    """
    normalized = posixpath.normpath(posixpath.join(base_dir, target))
    if normalized in (".", "") or normalized == ".." or normalized.startswith("../"):
        return ""
    return normalized


def read_presentation(path: str) -> PptxContent:
    """Read a .pptx into structured slides with body text and speaker notes.

    Rejection-gated. Pure stdlib: walks each slide part in PRESENTATION order
    (resolved through ``presentation.xml``'s ``sldIdLst`` + rels, not filename
    number) and, per slide, resolves its notes part through the slide's
    ``.rels`` file. A slide with no notes carries ``notes == ""``.
    """
    ensure_editable(path, expected_kind=DocumentKind.PPTX)
    names = container.part_names(path)
    name_set = set(names)
    slides: list[Slide] = []
    for number, part in _slide_parts(path, names):
        root = container.parse_xml_part(path, part)
        body_text = _text_of(root)
        notes_text = ""
        notes_part = _notes_part_for_slide(path, part, name_set)
        if notes_part is not None:
            # A notes part the slide's rels point at that will not parse is a
            # malformed package, not "no notes" — surface it as MalformedDocument
            # rather than silently blanking the notes (which would hide corruption
            # and drop real speaker-notes content without a signal). A slide with
            # genuinely no notes has notes_part is None and keeps notes == "".
            notes_root = container.parse_xml_part(path, notes_part)
            notes_text = _text_of(notes_root)
        slides.append(Slide(number=number, text=body_text, notes=notes_text))
    return PptxContent(slides=slides)


def _rewrite_slide_xml(raw: bytes, new_text: str) -> bytes:
    """Return a slide part's bytes with its FIRST text run set to *new_text*.

    Replaces the text of the first ``<a:t>`` run found (document order) and
    drops any additional runs within that same paragraph, leaving the shape,
    its properties and every other paragraph/shape untouched. Raises
    :class:`DocumentEditError` if the slide carries no text run to replace.
    """
    bad = C.find_illegal_xml_char(new_text)
    if bad is not None:
        raise DocumentEditError(
            f"replacement contains an XML-illegal character U+{ord(bad):04X}; "
            "refusing to write unreopenable output"
        )

    try:
        root = _xml_fromstring(raw)
    except Exception as exc:  # defusedxml raises several distinct types
        raise MalformedDocument(f"slide part is not well-formed XML: {exc}") from exc
    # Find the first paragraph that carries at least one run.
    for para in root.iter(f"{C.A}p"):
        runs = [r for r in para if r.tag == f"{C.A}r"]
        if not runs:
            continue
        first = runs[0]
        t = first.find(f"{C.A}t")
        if t is None:
            continue
        t.text = new_text
        # Remove trailing runs in this paragraph so the new text stands alone.
        for extra in runs[1:]:
            para.remove(extra)
        return _xml_tostring(root, encoding="UTF-8", xml_declaration=True)
    raise DocumentEditError("slide has no text run to replace")


def replace_slide_text(
    src_path: str,
    dst_path: str,
    edits: dict[int, str],
) -> None:
    """Replace the first text run of specific slides, byte-preserving other parts.

    *edits* maps a one-based PRESENTATION position to its new text (the same
    order :func:`read_presentation` reports, resolved through ``sldIdLst``, not
    filename number). ``src_path`` and ``dst_path`` may be equal. Rejection-gated.
    Raises :class:`DocumentEditError` for an unknown slide position or a slide
    with no editable run, BEFORE writing.
    """
    ensure_editable(src_path, expected_kind=DocumentKind.PPTX)
    # Pin the source signature BEFORE reading, so a source autosave between our
    # read and rewrite_parts' reopen is caught rather than published as a hybrid
    # (rewrite_parts reads every untouched part from the source at publish time,
    # so the race exists for a distinct destination too) — pin unconditionally.
    sig = container.source_signature(src_path)
    names = container.part_names(src_path)
    by_number = {num: part for num, part in _slide_parts(src_path, names)}

    if not edits:
        # No-op edit still produces a faithful copy.
        first_part = next(iter(by_number.values()), None)
        if first_part is None:
            raise DocumentEditError("presentation has no slides")
        raw = container.read_part(src_path, first_part)
        container.rewrite_parts(src_path, dst_path, {first_part: raw}, expect_source_signature=sig)
        return

    replacements: dict[str, bytes] = {}
    for number, new_text in edits.items():
        part = by_number.get(number)
        if part is None:
            raise DocumentEditError(f"slide {number} does not exist (have {sorted(by_number)})")
        raw = container.read_part(src_path, part)
        replacements[part] = _rewrite_slide_xml(raw, new_text)
    container.rewrite_parts(src_path, dst_path, replacements, expect_source_signature=sig)


def create_from_template(
    template_path: str,
    dst_path: str,
    edits: dict[int, str] | None = None,
) -> None:
    """Create a new .pptx from a local template, optionally substituting text.

    Faithful copy of the template's every part plus targeted per-slide text
    fills through :func:`replace_slide_text`; never a fresh synthesis.
    """
    ensure_editable(template_path, expected_kind=DocumentKind.PPTX)
    replace_slide_text(template_path, dst_path, edits or {})
