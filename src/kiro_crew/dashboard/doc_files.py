"""The markdown files behind doc sessions.

A doc session is a sidebar slot whose pane is a document instead of a chat. The
slot carries only the doc's id (``_ChatSlot._doc``); the text lives in one file per
doc under ``<data home>/docs/<id>.md``, so it is plain markdown any editor can open.

The id is minted here and validated on every read: it reaches a filesystem path and
every connected client's slot payload, so only the minted shape is ever accepted.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

#: The directory under the data home that holds every doc's file.
DOCS_DIRNAME = "docs"

#: The only id shape accepted: what :func:`mint_doc_id` produces.
DOC_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Largest document accepted on save, in UTF-8 bytes. Far above any hand-written
#: document, and small enough that one save cannot fill the disk or stall a read.
MAX_DOC_BYTES = 2 * 1024 * 1024


class DocConflictError(Exception):
    """The file changed on disk since the caller read it."""

    def __init__(self, current: DocFile) -> None:
        super().__init__("doc changed since it was read")
        self.current = current


@dataclass(frozen=True)
class DocFile:
    """One doc's text and the version stamp a conditional save compares against."""

    content: str
    #: SHA-256 of the file's bytes as read, or "" for a doc with no file yet. A
    #: string on purpose: it crosses JSON to the browser, where an integer stamp
    #: as large as ``st_mtime_ns`` would be rounded and never compare equal again.
    version: str


#: A board's id opens with this, so the pane knows to draw a whiteboard; the rest
#: of its id, file, save and history are a doc's. Hex, so it is still a doc id.
BOARD_ID_PREFIX = "b0a4d"

#: A codebase session's id opens with this: its pane draws a 3D model of a git work
#: tree (``codebase_scan.py``), and its file holds that model's settings and asks.
CODEBASE_ID_PREFIX = "c0deb"

#: A dashboard session's id opens with this: its pane shows an HTML page the
#: conductor writes, and its file holds that page and the asks pinned to it.
DASHBOARD_ID_PREFIX = "da5b0"

_KIND_PREFIXES = {
    "board": BOARD_ID_PREFIX,
    "codebase": CODEBASE_ID_PREFIX,
    "dashboard": DASHBOARD_ID_PREFIX,
}

#: The kinds a create may ask for; anything else is an ordinary doc.
DOC_KINDS = tuple(_KIND_PREFIXES)


def mint_doc_id(kind: str = "doc") -> str:
    raw = secrets.token_hex(16)
    prefix = _KIND_PREFIXES.get(kind)
    if prefix:
        return prefix + raw[len(prefix) :]
    # A doc never carries another kind's prefix, so the kinds cannot be confused.
    while raw.startswith(tuple(_KIND_PREFIXES.values())):
        raw = secrets.token_hex(16)
    return raw


def is_doc_id(value: object) -> bool:
    return isinstance(value, str) and DOC_ID_RE.match(value) is not None


def doc_kind(doc_id: str) -> str:
    """The kind *doc_id* was minted for: a key of ``_KIND_PREFIXES``, else "doc"."""
    for kind, prefix in _KIND_PREFIXES.items():
        if doc_id.startswith(prefix):
            return kind
    return "doc"


def docs_dir() -> Path:
    return data_home() / DOCS_DIRNAME


def doc_path(doc_id: str) -> Path:
    if not is_doc_id(doc_id):
        raise ValueError("invalid doc id")
    return docs_dir() / f"{doc_id}.md"


def read_doc(doc_id: str) -> DocFile:
    """Read a doc. A doc whose file does not exist yet reads as empty, version ""."""
    path = doc_path(doc_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return DocFile(content="", version="")
    # Bytes, not read_text: universal-newline reading would turn \r\n into \n,
    # and the next save would silently rewrite every line ending.
    return DocFile(
        content=raw.decode("utf-8", errors="replace"),
        version=hashlib.sha256(raw).hexdigest(),
    )


def write_doc(doc_id: str, content: str, *, expected_version: str | None) -> DocFile:
    """Save *content*, refusing when the file is no longer *expected_version*.

    ``expected_version=None`` skips the check; ``""`` expects no file yet. A save
    over an edit made elsewhere raises :class:`DocConflictError` carrying the file as
    it is now, so the caller can show it instead of overwriting it.
    """
    if len(content.encode("utf-8")) > MAX_DOC_BYTES:
        raise ValueError("doc too large")
    if expected_version is not None:
        current = read_doc(doc_id)
        if current.version != expected_version:
            raise DocConflictError(current)
    # newline="" keeps the bytes as written: a document is read, edited and saved
    # again, and newline translation would add a carriage return on every save.
    atomic_write(doc_path(doc_id), content, newline="")
    return read_doc(doc_id)
