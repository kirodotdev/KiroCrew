"""Prompt text for the documents attached to a dashboard chat message.

For each ``.pdf`` / ``.docx`` / ``.pptx`` path in a turn's ``meta.files``, this
module extracts the file's text and returns it as a
``[Document: name] … [End of document]`` block, the format
``messaging.attachments.ingest_attachments`` uses for channel attachments.

- PDFs are extracted by :func:`kiro_crew.pdf_extract.extract_pdf_segments`, which
  runs pdfplumber in a memory- and CPU-limited child process.
- ``.docx`` / ``.pptx`` are extracted by
  :func:`kiro_crew.office_extract.extract_office_text`, which runs
  :func:`kiro_crew.doc_parser.extract_text` in the same kind of child process.
- All documents of one message share one deadline; a child still running when it
  passes is killed.
- Only regular files directly inside the data home's ``uploads/`` directory are
  read. Any other path in ``meta.files`` is skipped.
- Each file is read once, through a :class:`~kiro_crew.platform_compat.PinnedDirectory`
  on ``uploads/``: the open refuses a link at the name, and the size and file type
  are checked on the opened descriptor. The parsers receive those bytes and never
  reopen the path.
- Extracted text is redacted, then truncated, with the limits in
  :class:`kiro_crew.messaging.attachments.IngestLimits`.
"""

from __future__ import annotations

import errno
import logging
import re
import time
from pathlib import Path
from typing import Iterable

from kiro_crew.hooks import validate_file_path
from kiro_crew.messaging.attachments import IngestLimits, _clean_text
from kiro_crew.office_extract import extract_office_text
from kiro_crew.pdf_extract import extract_pdf_segments
from kiro_crew.platform_compat import PinnedDirectory, pinned_directory

logger = logging.getLogger(__name__)

#: Document formats extracted. Matches ``doc_parser.DOC_EXTENSIONS``.
DOCUMENT_SUFFIXES: frozenset[str] = frozenset({".pdf", ".docx", ".pptx"})

#: Bytes read per file, characters injected per file, and files per message.
_LIMITS = IngestLimits()

#: Wall-clock limit, in seconds, for extracting every document of one message.
_EXTRACT_DEADLINE_SECS = 30.0

#: The ``<uuid4 hex>_`` prefix ``api_upload_file`` adds to stored file names.
#: Removed from the name shown to the model.
_UPLOAD_PREFIX_RE = re.compile(r"^[0-9a-f]{32}_")


def _display_name(name: str) -> str:
    return _UPLOAD_PREFIX_RE.sub("", name) or name


def _upload_root() -> Path | None:
    # Deferred: the handlers package imports the chat modules that import this one.
    from kiro_crew.dashboard.handlers.files import _upload_dir

    try:
        return _upload_dir().resolve()
    except (OSError, ValueError):
        return None


def _admitted_name(raw: str, root: Path) -> str | None:
    """The entry name of *raw* in *root*, when it names a document there; else None.

    *raw* is canonicalized by :func:`kiro_crew.hooks.validate_file_path` before any
    other filesystem call: it refuses an unrepresentable path (an embedded NUL), a
    Windows UNC path outside the trusted roots and a Windows link aimed at one,
    then resolves the path and applies :func:`is_sensitive_path`. The canonical
    path must be an existing direct child of *root*.

    This screen does not authorize the read on its own: :func:`_read_upload` opens
    the name through the pinned directory and refuses a link there, so an entry
    replaced after this check is not followed.
    """
    if Path(raw).suffix.lower() not in DOCUMENT_SUFFIXES:
        return None
    try:
        canonical = validate_file_path(raw)
        if canonical is None:
            return None
        resolved = Path(canonical)
        if resolved.parent != root or not resolved.is_file():
            return None
    except (OSError, ValueError):
        return None
    return resolved.name


def _read_upload(uploads: PinnedDirectory, name: str) -> bytes | str:
    """The bytes of upload *name*, or the notice block to send instead.

    Read through *uploads*: a link at the name, a non-regular entry and a hardlink
    are refused, and the size limit is checked on the opened descriptor.
    """
    try:
        return uploads.read_bytes(name, max_bytes=_LIMITS.max_document_bytes)
    except OSError as exc:
        shown = _display_name(name)
        if exc.errno == errno.EFBIG:
            limit_mb = _LIMITS.max_document_bytes // (1024 * 1024)
            return f"[Attached document: {shown} — too large to extract (limit {limit_mb} MB)]"
        logger.info("attachment document: %s not read (%s)", name, exc)
        return f"[Attachment {shown} — could not be processed]"


def _extract_pdf(data: bytes, name: str, max_chars: int, deadline: float) -> str:
    outcome = extract_pdf_segments(data, max_chars=max_chars, deadline=deadline)
    if outcome.failure is not None:
        logger.info("attachment document: PDF %s not extracted (%s)", name, outcome.failure)
        return ""
    # ``page 3`` -> ``--- Page 3 ---``, matching extract_text's ``--- Slide N ---``.
    return "\n\n".join(
        f"--- {label.capitalize()} ---\n{text}" for label, text in outcome.segments if text.strip()
    )


def _extract(data: bytes, name: str, max_chars: int, deadline: float) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(data, name, max_chars, deadline)
    outcome = extract_office_text(data, suffix[1:], max_chars=max_chars, deadline=deadline)
    if outcome.failure is not None:
        logger.info("attachment document: %s not extracted (%s)", name, outcome.failure)
        return ""
    return outcome.text


def _document_block(uploads: PinnedDirectory, name: str, deadline: float) -> str:
    shown = _display_name(name)
    data = _read_upload(uploads, name)
    if isinstance(data, str):
        return data
    try:
        # One character past the cap, so ``_clean_text`` marks text that hit it.
        raw = _extract(data, name, _LIMITS.max_text_inject + 1, deadline)
    except Exception:
        logger.warning("attachment document: extraction failed for %s", name, exc_info=True)
        raw = ""
    if not raw.strip():
        return f"[Attached document: {shown} — could not extract text]"
    body = _clean_text(raw, _LIMITS.max_text_inject)
    return f"[Document: {shown}]\n{body}\n[End of document]"


def attachment_document_blocks(paths: Iterable[str]) -> list[str]:
    """Prompt blocks for the uploaded documents among *paths*, in order.

    Blocking (file reads, a parser child per document): call it off the event
    loop. Every document shares one deadline, :data:`_EXTRACT_DEADLINE_SECS` from
    the call; a child still running at the deadline is killed. Never raises. A
    path that is not an uploaded .pdf/.docx/.pptx contributes nothing. A document
    that is too large, cannot be read, yields no text or is not extracted before
    the deadline contributes a one-line notice instead of a block.
    """
    deadline = time.monotonic() + _EXTRACT_DEADLINE_SECS
    names: list[str] = []
    try:
        root = _upload_root()
        if root is None:
            return []
        for raw in paths:
            if len(names) >= _LIMITS.max_attachments:
                break
            if not isinstance(raw, str) or not raw:
                continue
            name = _admitted_name(raw, root)
            if name is not None and name not in names:
                names.append(name)
        if not names:
            return []
        uploads = pinned_directory(root)
    except Exception:
        logger.warning("attachment document: uploads directory unavailable", exc_info=True)
        return []
    with uploads:
        return [_document_block(uploads, name, deadline) for name in names]


def attachment_document_context(paths: Iterable[str]) -> str:
    """:func:`attachment_document_blocks` joined as request-prefix context, or ``""``.

    The result starts and ends with a blank line, separating it from the context
    before it and from the user's text after it.
    """
    blocks = attachment_document_blocks(paths)
    if not blocks:
        return ""
    return "\n\n" + "\n\n".join(blocks) + "\n\n"


__all__ = [
    "DOCUMENT_SUFFIXES",
    "attachment_document_blocks",
    "attachment_document_context",
]
