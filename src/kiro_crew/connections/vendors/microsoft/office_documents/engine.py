"""Public facade for the offline OOXML document engine.

One entry point that resolves a file's kind through the rejection gate and
dispatches to the docx or pptx implementation. Callers that already know the
kind can use :mod:`.docx` / :mod:`.pptx` directly; this facade is for the
kind-agnostic case (a path arrives, read whatever it is).

Everything here is offline: no network, no credentials, no Microsoft Graph. It
reads, creates from a local template, and edits local files. Cloud round-trip
and Graph-backed operations are separate, later slices.
"""

from __future__ import annotations

from typing import Union

from . import docx as _docx
from . import pptx as _pptx
from .docx import DocxContent
from .pptx import PptxContent
from .rejection import Classification, DocumentKind, classify, ensure_editable

__all__ = [
    "DocumentKind",
    "Classification",
    "classify",
    "read",
    "read_document",
    "read_presentation",
    "create_from_template",
    "edit_in_place",
]

# Re-export the per-kind readers so a caller has one import site.
read_document = _docx.read_document
read_presentation = _pptx.read_presentation


def read(path: str) -> Union[DocxContent, PptxContent]:
    """Read *path* into its structured content, dispatching on the resolved kind.

    Rejection-gated: a protected/unsupported/malformed file raises the matching
    typed error. Returns a :class:`~.docx.DocxContent` for a Word document or a
    :class:`~.pptx.PptxContent` for a presentation.
    """
    kind = ensure_editable(path)
    if kind == DocumentKind.DOCX:
        return _docx.read_document(path)
    return _pptx.read_presentation(path)


def create_from_template(
    template_path: str,
    dst_path: str,
    edits: dict[int, str] | None = None,
) -> str:
    """Create a new file from a local template, dispatching on the template kind.

    Returns the resolved :class:`DocumentKind`. For docx, *edits* addresses
    paragraphs by zero-based index; for pptx, slides by one-based number.
    """
    kind = ensure_editable(template_path)
    if kind == DocumentKind.DOCX:
        _docx.create_from_template(template_path, dst_path, edits)
    else:
        _pptx.create_from_template(template_path, dst_path, edits)
    return kind


def edit_in_place(
    src_path: str,
    dst_path: str,
    edits: dict[int, str],
) -> str:
    """Apply targeted text edits, dispatching on kind, byte-preserving other parts.

    Returns the resolved :class:`DocumentKind`. ``src_path`` and ``dst_path`` may
    be equal. docx edits key on zero-based paragraph index; pptx edits key on
    one-based slide number.
    """
    kind = ensure_editable(src_path)
    if kind == DocumentKind.DOCX:
        _docx.replace_paragraph_text(src_path, dst_path, edits)
    else:
        _pptx.replace_slide_text(src_path, dst_path, edits)
    return kind
