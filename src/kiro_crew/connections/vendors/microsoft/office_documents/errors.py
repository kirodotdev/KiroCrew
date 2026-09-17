"""Typed errors for the offline OOXML document engine.

Every failure this engine can produce is one of these, so a caller maps a
document problem onto its own channel by ``.reason`` (a short machine-readable
discriminator) without parsing prose. The hierarchy has one root,
:class:`OfficeDocumentError`, so a caller that only wants "this document could
not be handled" catches the base and a caller that must tell a *refused* file
(password/macro/signature) from a merely *malformed* one switches on the
subclass.
"""

from __future__ import annotations

__all__ = [
    "OfficeDocumentError",
    "UnsupportedDocument",
    "ProtectedDocument",
    "MalformedDocument",
    "DocumentEditError",
]


class OfficeDocumentError(Exception):
    """Root of every error this engine raises.

    ``reason`` is a stable, machine-readable discriminator; ``message`` is the
    human-readable detail. Subclasses set ``reason`` to a fixed value so a
    caller can branch on the class OR the string.
    """

    reason = "office_document_error"

    def __init__(self, message: str = "", *, reason: str | None = None):
        super().__init__(message or (reason or self.reason))
        if reason is not None:
            self.reason = reason
        self.message = message or self.reason


class UnsupportedDocument(OfficeDocumentError):
    """The container is a legal OOXML file but not one this engine handles.

    Reserved for a *format* the engine does not support at all (e.g. a legacy
    OLE2 ``.doc``/``.ppt`` binary, or a ``.xlsx`` handed to a docx entry point):
    the file is intact, the engine simply has no reader for it. Distinct from
    :class:`ProtectedDocument`, which is a file the engine *could* read were it
    not deliberately locked, and from :class:`MalformedDocument`, which is a
    file that is not a valid container at all.
    """

    reason = "unsupported_format"


class ProtectedDocument(UnsupportedDocument):
    """A file whose content is deliberately locked against plain OOXML reads.

    Password/agile encryption, an embedded macro project, IRM/rights
    management, or a digital signature whose presence means an edit would
    invalidate it. The engine REFUSES rather than silently corrupting: a
    write-back that dropped the signature part or re-zipped an encrypted
    package would produce a file that opens broken. It is an
    :class:`UnsupportedDocument` because a caller that only wants "cannot
    handle this" need not distinguish, but it carries its own ``reason`` and
    subclass so a caller that wants to explain *why* can.
    """

    reason = "protected_document"


class MalformedDocument(OfficeDocumentError):
    """The bytes are not a usable OOXML container.

    Not a ZIP at all, a truncated/oversized inventory the zip vet rejected, or
    an archive missing the part every document of its kind must contain
    (``word/document.xml``, ``ppt/presentation.xml``). Distinct from
    :class:`UnsupportedDocument`: that one is intact-but-unhandled, this one is
    broken.
    """

    reason = "malformed_document"


class DocumentEditError(OfficeDocumentError):
    """An in-place edit could not be applied as requested.

    A target the edit named (a paragraph index, a run, a slide) does not exist,
    or the requested rewrite would not round-trip. Raised BEFORE any bytes are
    written, so a failed edit never leaves a half-written file.
    """

    reason = "document_edit_error"
