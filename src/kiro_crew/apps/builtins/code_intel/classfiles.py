"""Library classes jdtls answers with as ``jdt://`` URIs, written out as files.

A definition that lands in a jar has no file path: jdtls returns a ``jdt://``
URI and serves the class's source (attached, or a decompiled stub) through its
``java/classFileContents`` request, once the client declares
``classFileContentsSupport``. The editor opens files, so each such class is
written read-only under ``$XDG_CACHE_HOME/kirocrew/code-intel/java-classes``,
in a folder named by the URI's hash, beside an ``origin.json`` naming the URI and
the server root it came from. A request on that file then goes to the same
server with the ``jdt://`` URI as its document, so navigation keeps working
inside library code, across gateway restarts too.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from urllib.parse import unquote, urlsplit

JDT_SCHEME = "jdt://"
# Above this, a class's source is not written out (a generated giant, say).
MAX_CLASS_BYTES = 2 * 1024 * 1024
_ORIGIN = "origin.json"
_UNSAFE = re.compile(r"[^A-Za-z0-9_$.-]")


def classes_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "kirocrew", "code-intel", "java-classes")


def is_class_uri(uri: str) -> bool:
    return uri.startswith(JDT_SCHEME)


def _file_name(uri: str) -> str:
    """``Name.java`` for a ``jdt://contents/<jar>/<package>/Name.{class,java}?...`` URI."""
    last = unquote(urlsplit(uri).path).rstrip("/").rsplit("/", 1)[-1]
    # jdtls names a class with attached source ``Name.java``, one without ``Name.class``.
    stem = last
    for suffix in (".class", ".java"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = _UNSAFE.sub("_", stem).lstrip(".") or "Class"
    return f"{stem[:120]}.java"


def path_for(uri: str) -> str:
    """Where the source for class *uri* lives (whether or not written yet)."""
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:20]
    return os.path.join(classes_dir(), digest, _file_name(uri))


def write(uri: str, root: str, contents: str) -> str | None:
    """Write *contents* for *uri* (from the server at *root*); return its path.

    None when there is nothing worth opening. Blocking (writes files).
    """
    data = contents.encode("utf-8", "replace")
    if not contents.strip() or len(data) > MAX_CLASS_BYTES:
        return None
    path = path_for(uri)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    # Two requests can write the same class at once: each file is written whole
    # beside its final name and moved into place, so a reader never sees a
    # partial origin.json or source.
    origin_path = os.path.join(folder, _ORIGIN)
    origin_tmp = f"{origin_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(origin_tmp, "w", encoding="utf-8") as fh:
        json.dump({"uri": uri, "server": "java", "root": root}, fh)
    os.replace(origin_tmp, origin_path)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    # Read-only: an edit there would change nothing jdtls sees.
    os.chmod(tmp, 0o400)
    os.replace(tmp, path)
    return path


def origin(path: str) -> tuple[str, str] | None:
    """``(jdt uri, server root)`` for a written class file, or None. Blocking."""
    folder = os.path.dirname(path)
    if os.path.realpath(os.path.dirname(folder)) != os.path.realpath(classes_dir()):
        return None
    try:
        with open(os.path.join(folder, _ORIGIN), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return None
    uri, root = meta.get("uri"), meta.get("root")
    if not (isinstance(uri, str) and is_class_uri(uri) and isinstance(root, str) and root):
        return None
    # The file must be the one that URI names, not a neighbour planted beside it.
    expected = path_for(uri)
    if (os.path.basename(os.path.dirname(expected)), os.path.basename(expected)) != (
        os.path.basename(folder),
        os.path.basename(path),
    ):
        return None
    return uri, root
