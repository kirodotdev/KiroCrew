"""Build a read-only SQLite URI from a filesystem path."""

from __future__ import annotations

from pathlib import Path


def with_empty_authority(uri: str) -> str:
    """Rewrite a ``file://host/share/...`` URI to SQLite's empty-authority form.

    ``Path.as_uri()`` turns a Windows UNC path into ``file://host/share/...``, and
    SQLite rejects any authority other than empty or ``localhost``. The
    ``file:////host/share/...`` spelling carries the same path behind an empty
    authority, which Windows reads as the UNC share. Every other URI is returned as is.
    """
    if uri.startswith("file://") and not uri.startswith("file:///"):
        return "file:////" + uri[len("file://") :]
    return uri


def read_only_uri(path: Path) -> str:
    """A ``mode=ro`` SQLite URI for *path* that opens exactly that file.

    ``as_uri()`` percent-encodes ``#``, ``%`` and ``?``, which a raw ``file:{path}``
    would hand to SQLite as a fragment, an escape and a query. Pass the result to
    ``sqlite3.connect(..., uri=True)``.
    """
    return f"{with_empty_authority(path.absolute().as_uri())}?mode=ro"
