"""OOXML constants: namespaces, part names, container limits, format markers.

Kept in one place so the reader, the editor and the rejection gate all agree on
the same XML namespaces and part paths. Every namespace URI here is from the
ECMA-376 / ISO-29500 Office Open XML specification; the strings are the format's
own, not this project's.
"""

from __future__ import annotations

# ── WordprocessingML (docx) ──
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
# ── DrawingML (shared text runs inside pptx shapes) ──
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
# ── PresentationML (pptx) ──
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
# ── Relationships ──
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

# Brace-prefixed forms for ElementTree's ``{ns}tag`` addressing.
W = "{%s}" % W_NS
A = "{%s}" % A_NS
P = "{%s}" % P_NS
R = "{%s}" % R_NS

# ── Canonical part paths ──
DOCX_MAIN_PART = "word/document.xml"
PPTX_PRESENTATION_PART = "ppt/presentation.xml"
XLSX_WORKBOOK_PART = "xl/workbook.xml"
CONTENT_TYPES_PART = "[Content_Types].xml"

# A pptx slide part path: ``ppt/slides/slide<N>.xml``; its notes part is
# ``ppt/notesSlides/notesSlide<N>.xml`` reached through the slide's rels.
SLIDE_PATH_PREFIX = "ppt/slides/slide"
NOTES_PATH_PREFIX = "ppt/notesSlides/notesSlide"

# ── Markers of a deliberately-locked or unsupported container ──
#
# Presence-only signals; the engine never attempts to unlock or verify any of
# them (see rejection.py). Each is a part name, a part-name substring, or a
# magic-byte prefix.

# Password / agile encryption wraps the whole OOXML zip inside a compound
# (OLE2) file whose stream is named "EncryptedPackage"; the outer file is a
# CFB, not a zip, so it also fails the "is a legal zip" test.
ENCRYPTED_PACKAGE_STREAM = b"EncryptedPackage"
# OLE2 / Compound File Binary magic — legacy .doc/.ppt AND the encrypted-OOXML
# wrapper both begin with it.
OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
# A ZIP local-file-header magic; a real OOXML container starts here.
ZIP_MAGIC = b"PK\x03\x04"
# An empty archive (no members) still starts with the EOCD magic.
ZIP_EMPTY_MAGIC = b"PK\x05\x06"

# Macro project part — its presence makes a document macro-enabled regardless
# of extension. .docm/.pptm are the macro-enabled extensions.
VBA_PROJECT_PART = "word/vbaProject.bin"
VBA_PROJECT_SUFFIX = "vbaProject.bin"
MACRO_ENABLED_EXTS = frozenset({".docm", ".pptm", ".xlsm", ".dotm", ".potm"})
LEGACY_BINARY_EXTS = frozenset({".doc", ".ppt", ".xls"})

# Digital-signature parts. Presence means the package is signed; an edit would
# invalidate the signature, so the engine refuses rather than silently break it.
SIGNATURE_ORIGIN_PART = "_xmlsignatures/origin.sigs"
SIGNATURE_PART_PREFIX = "_xmlsignatures/"
# IRM / rights-management protection leaves a DRM part in the package.
DRM_ENCRYPTED_PART_SUFFIX = "DataSpaces/DataSpaceMap.xml"
DRM_TRANSFORM_SUFFIX = "\x06DataSpaces"

# ── Container safety limits (shared with kiro_crew.zip_vet caps) ──
MAX_ARCHIVE_MEMBERS = 20000
MAX_PART_BYTES = 50 * 1024 * 1024  # 50 MB per decompressed part


def find_illegal_xml_char(text: str) -> str | None:
    """Return the first XML-1.0-forbidden character in *text*, or ``None``.

    XML 1.0 (§2.2 Char) forbids most C0 control characters — only tab (0x09),
    line feed (0x0A) and carriage return (0x0D) are legal below 0x20 — plus
    the surrogate block, 0xFFFE and 0xFFFF. A run of replacement text carrying
    such a character would serialise into a ``<w:t>``/``<a:t>`` element that no
    conformant reader can reopen, so the edit is refused BEFORE any bytes are
    written rather than producing silently corrupt output.
    """
    for ch in text:
        code = ord(ch)
        if code in (0x09, 0x0A, 0x0D):
            continue
        if 0x20 <= code <= 0xD7FF:
            continue
        if 0xE000 <= code <= 0xFFFD:
            continue
        if 0x10000 <= code <= 0x10FFFF:
            continue
        return ch
    return None
