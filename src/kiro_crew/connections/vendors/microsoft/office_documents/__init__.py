"""Offline Office Open XML (OOXML) document engine — W07 · office_documents.

Local, zero-network, zero-credential read/create/edit of ``.docx`` and ``.pptx``
files. This is the offline half of the Office capability set: it never calls
Microsoft Graph, never authenticates, and never uploads. Cloud round-trip and
Graph-backed operations are separate, later slices.

What it does:

* **read** — structured docx (paragraphs + tables) and pptx (slides + speaker
  notes) content;
* **create** — a new file from a local template, carrying the template's parts
  through byte-for-byte with optional targeted text fills;
* **edit** — targeted in-place text edits that rewrite ONLY the changed part and
  preserve every other part byte-for-byte (never a lossy whole-document
  re-serialize);
* **refuse** — a rejection gate that explicitly declines password/agile-
  encrypted, macro-enabled, digitally-signed, IRM-protected and legacy-binary
  files rather than silently corrupting them. Signature detection is
  presence-only; validity is deliberately not asserted.

pptx parsing is pure standard library (``zipfile`` + hardened XML): the engine
does NOT depend on ``python-pptx`` and does NOT add ``.pptx`` to the knowledge
folder-scan ``SUPPORTED`` set — both would be out-of-scope behavior changes.
"""

from __future__ import annotations

from .docx import DocxContent, Paragraph, Table
from .engine import (
    classify,
    create_from_template,
    edit_in_place,
    read,
    read_document,
    read_presentation,
)
from .errors import (
    DocumentEditError,
    MalformedDocument,
    OfficeDocumentError,
    ProtectedDocument,
    UnsupportedDocument,
)
from .pptx import PptxContent, Slide
from .rejection import Classification, DocumentKind

__all__ = [
    # facade
    "read",
    "read_document",
    "read_presentation",
    "create_from_template",
    "edit_in_place",
    "classify",
    # types
    "DocumentKind",
    "Classification",
    "DocxContent",
    "Paragraph",
    "Table",
    "PptxContent",
    "Slide",
    # errors
    "OfficeDocumentError",
    "UnsupportedDocument",
    "ProtectedDocument",
    "MalformedDocument",
    "DocumentEditError",
]
