"""The bundle's format and the staging files a transfer is written through.

The wire constants (``BUNDLE_VERSION``, the versions the importer accepts, the visible
roles, the import title marker, this instance's origin label, ``TransferBundle``), the
two staging directories with their once-per-process orphan sweep, and the writers that
stream a bundle to a staged JSON or gzip file and release it again.

Owner of :mod:`kiro_crew.dashboard.session_transfer`, which is its only import path:
callers and tests reach every name here as ``session_transfer.<name>``.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import platform
import stat
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.config.paths import data_home

logger = logging.getLogger("kiro_crew.dashboard.session_transfer")


#: Bundle schema version. Bump on any incompatible change to the payload shape;
#: the importer refuses a version it does not know rather than guessing, because
#: the two ends of a transfer are independently-updated installs and a silently
#: misread field would land as corrupted conversation.
BUNDLE_VERSION = 2


#: Versions this importer still accepts. v1 = transcript-only (the peer rebuilds
#: context via the lossy ~8K history prefix); v2 additionally carries **Layer B**
#: — the kiro-cli context window (``<sid>.json`` + ``<sid>.jsonl``) — so an
#: imported session resumes with full fidelity via ``session/load`` instead of a
#: replayed prefix. Accepting both lets a newer instance still receive a copy
#: from an older one; anything OUTSIDE the set is refused rather than
#: best-effort parsed, because a silently misread field lands as corrupted
#: conversation.
_SUPPORTED_BUNDLE_VERSIONS = (1, 2)


class TransferBundle(dict[str, Any]):
    """Wire bundle plus the transcript chain validated during assembly."""

    __slots__ = ("publication_keys",)

    def __init__(self, payload: dict[str, Any], *, publication_keys: Sequence[str]) -> None:
        super().__init__(payload)
        self.publication_keys = tuple(publication_keys)


#: Structural cap on the title. NOT a size wall: an overlong title is TRUNCATED
#: to this many characters, never rejected, because a title is a label and losing
#: its tail costs a reader nothing. Session transfer places no ceiling on message
#: count or content size — the body streams to disk on arrival, so memory is
#: bounded by the write rather than by refusing large sessions
#: (:func:`_read_bundle_body`), and a large session is copied, not blocked.
_MAX_TITLE_CHARS = 500


#: Read/write granularity for streaming the body to disk and for the bounded
#: gunzip. Small enough that each disk-room check and each chunk held in memory
#: stays cheap, large enough that a real
#: multi-megabyte bundle is a few hundred iterations rather than a few hundred
#: thousand.
_CHUNK_BYTES = 256 * 1024


#: When this process loaded the module. A staging file older than this cannot
#: belong to a transfer this process is running, so it was orphaned by a crash
#: or a kill of an earlier gateway process.
_PROCESS_STARTED_AT = time.time()


#: Staging directories already swept by this process.
_swept_staging_dirs: set[Path] = set()


def _sweep_orphaned_staging(d: Path) -> None:
    """Remove files an earlier gateway process left in staging dir *d*.

    Runs once per directory per process, on first use. Only direct-child regular
    files older than this process are removed: every file a live transfer in this
    process writes is newer than :data:`_PROCESS_STARTED_AT`. The directory is
    pinned without following links (:func:`platform_compat.pin_directory`) and
    every entry is examined and removed through that pin, so a link planted at
    *d*, or at an entry, can never turn the sweep onto files outside it.
    Fail-open: a directory that cannot be pinned, or an entry that cannot be read,
    is left alone. Without it, orphans accumulate toward
    :data:`_DISK_HEADROOM_BYTES` and the disk gate refuses imports the volume
    could otherwise hold.
    """
    if d in _swept_staging_dirs:
        return
    _swept_staging_dirs.add(d)
    try:
        pinned = platform_compat.PinnedDirectory(platform_compat.pin_directory(d), str(d))
    except OSError:
        logger.debug("session_transfer: staging dir %s is not a real directory", d.name)
        return
    with pinned:
        try:
            names = pinned.names()
        except OSError:
            return
        for name in names:
            try:
                if pinned.is_link(name):
                    continue
                info = pinned._lstat(name)
                if info is None or not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_mtime >= _PROCESS_STARTED_AT:
                    continue
                pinned.unlink(name)
            except OSError:
                logger.debug("session_transfer: could not sweep %s", name, exc_info=True)


def _import_tmp_dir() -> Path:
    """Where an arriving bundle is streamed to before it is parsed.

    Under the crew data home rather than the system temp, so it inherits the home's
    own posture and is reclaimed with it, and is created lazily on first use. A
    function (not a module constant) so a test can point it at an isolated
    directory, the same lever :func:`kiro_sessions_dir` offers.
    """
    d = data_home() / "tmp" / "session-import"
    d.mkdir(parents=True, exist_ok=True)
    _sweep_orphaned_staging(d)
    return d


def _egress_tmp_dir() -> Path:
    """Where an outgoing bundle's Layer B snapshot and serialised body are staged.

    The sending half of :func:`_import_tmp_dir`: same home, same lazy creation,
    same test lever. Everything written here is removed by
    :func:`release_bundle_files` or by the writer that made it.
    """
    d = data_home() / "tmp" / "session-export"
    d.mkdir(parents=True, exist_ok=True)
    _sweep_orphaned_staging(d)
    return d


class LayerBEvents:
    """Layer B's event log carried as a FILE rather than as text.

    The log is the largest part of a bundle, hundreds of MiB on a long session,
    and nothing on the sending side reads it except to write it back out. So the
    bundle holds a private snapshot of the file, taken once and validated as it
    was copied, and :func:`write_bundle_json` streams it into the wire document
    a chunk at a time. The peer receives the same JSON string it always did.

    The snapshot, not the live file, because kiro-cli keeps appending to its log
    and the copy must be the one that was validated.
    """

    __slots__ = ("path",)

    def __init__(self, path: Path) -> None:
        self.path = path


def release_bundle_files(bundle: dict[str, Any] | None) -> None:
    """Remove the temp file an outgoing *bundle* carries, if it carries one.

    Synchronous and idempotent, so it can run in a ``finally`` whatever raised.
    """
    layer_b = (bundle or {}).get("layer_b")
    events = layer_b.get("events") if isinstance(layer_b, dict) else None
    if isinstance(events, LayerBEvents):
        _rm_import_temps(events.path)


def _write_json_string_from_file(src: Path, write: Any) -> None:
    """Write the file *src* as one JSON string literal, a chunk at a time.

    The same bytes ``json.dumps`` would produce for the whole text, because
    escaping is per character and ``ensure_ascii`` escapes a character outside
    the BMP as a surrogate pair on its own: encoding chunk by chunk and joining
    is identical to encoding the joined text. The incremental decoder under the
    text handle is what keeps a multi-byte character split across a chunk
    boundary whole.
    """
    write(b'"')
    with open(src, encoding="utf-8", newline="") as f:
        while True:
            chunk = f.read(_CHUNK_BYTES)
            if not chunk:
                break
            write(json.dumps(chunk)[1:-1].encode("ascii"))
    write(b'"')


def write_bundle_json(bundle: dict[str, Any], write: Any) -> None:
    """Serialise *bundle* as compact JSON through *write*. **Blocking, thread-safe.**

    Byte-for-byte what ``json.dumps(bundle, separators=(",", ":"))`` produces, so
    every importer reads it unchanged, but never holds the whole document: each
    message is encoded on its own, and a Layer B log carried as
    :class:`LayerBEvents` streams from its snapshot. Peak memory is one message
    or one chunk of the log, not the body.

    ``ensure_ascii`` stays at its DEFAULT, a correctness choice rather than a
    stylistic one: a transcript can legitimately carry a lone surrogate
    (``json.loads('"\\ud800"')`` yields one, and ``_validate_bundle`` accepts it).
    Unescaped it cannot be encoded as UTF-8, so the export would fail for a
    session the user can read; escaped it round-trips through ``json.loads``.
    """
    sep = (",", ":")
    write(b"{")
    for i, (key, value) in enumerate(bundle.items()):
        if i:
            write(b",")
        write(json.dumps(key).encode("ascii") + b":")
        if key == "messages" and isinstance(value, list):
            write(b"[")
            for j, message in enumerate(value):
                if j:
                    write(b",")
                write(json.dumps(message, separators=sep).encode("ascii"))
            write(b"]")
        elif key == "layer_b" and isinstance(value, dict):
            write(b"{")
            for k, (lkey, lvalue) in enumerate(value.items()):
                if k:
                    write(b",")
                write(json.dumps(lkey).encode("ascii") + b":")
                if isinstance(lvalue, LayerBEvents):
                    _write_json_string_from_file(lvalue.path, write)
                else:
                    write(json.dumps(lvalue, separators=sep).encode("ascii"))
            write(b"}")
        else:
            write(json.dumps(value, separators=sep).encode("ascii"))
    write(b"}")


def write_bundle_file(bundle: dict[str, Any], *, compress: bool) -> Path:
    """Serialise *bundle* to a temp file and return its path. **Blocking.**

    Gzip for the file a user downloads, plain JSON for a peer, which every
    importer release accepts. ``mtime=0`` because the export instant is already
    inside the document as ``source.exported_at``; a second copy in the gzip
    header would only make two identical exports differ in their bytes. The caller owns the file and removes it with :func:`_rm_import_temps`.
    """
    fd, name = tempfile.mkstemp(
        dir=str(_egress_tmp_dir()), suffix=".json.gz" if compress else ".json"
    )
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as raw:
            if compress:
                with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                    write_bundle_json(bundle, gz.write)
            else:
                write_bundle_json(bundle, raw.write)
    except BaseException:
        _rm_import_temps(path)
        raise
    return path


#: Roles that make up a visible conversation. Tool/system frames are not carried:
#: they reference local tool state that means nothing on the target instance.
_VISIBLE_ROLES = ("user", "assistant")


#: Prefix marking an imported session in the sidebar, so a transferred tab is
#: never mistaken for one that originated locally.
_IMPORT_TITLE_MARKER = "⇄ "


def local_instance_label() -> str:
    """A short human label for THIS instance, used as a transfer's ``origin``.

    The local instance is implicit in the registry and has no configured name
    (instances.md §1), so there is nothing to read: the host's first DNS label
    is the most recognisable stand-in and is short enough to sit in a session
    title. Falls back to ``"another instance"`` rather than raising, because a
    missing label must never fail a transfer.
    """
    try:
        return platform.node().split(".")[0] or "another instance"
    except Exception:
        return "another instance"


def _rm_import_temps(*paths: Path | None) -> None:
    """Delete the arrival's temp files. Synchronous so a cancellation cannot skip it."""
    for p in paths:
        if p is None:
            continue
        try:
            p.unlink(missing_ok=True)
        except Exception:
            logger.debug("session_transfer: could not remove import temp %s", p, exc_info=True)
