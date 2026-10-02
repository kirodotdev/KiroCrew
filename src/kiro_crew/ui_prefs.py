"""Durable backup for the dashboard's browser-side UI preferences.

Why this module exists
---------------------
Most dashboard settings live only in the renderer's ``localStorage``: the whole
chat-preferences blob (``mc-chat-config``: send-key mode, timestamps, content
width, ...), the diff and file-viewer toggles, the artifact view/sort, the app
nav order, and dozens more. ``localStorage`` is partitioned per ORIGIN and, in
the desktop app, stored inside Electron's ``userData`` directory. Either can
change without the user doing anything wrong:

* the dashboard port moves (``KIROCREW_PORT`` set in one launch context and not
  another, an edited ``dashboard.url``), so the origin — and with it every key —
  changes;
* ``userData`` is relocated (the ``kirocrew-electron-mac`` -> ``kirocrew-desktop``
  package rename did exactly this) and only an allowlist of *electron-store*
  keys is carried across, never ``Local Storage/leveldb``;
* the user switches between the stable and nightly builds, which have separate
  ``userData`` directories by design;
* the browser evicts the origin's storage.

In all of those the user sees the same thing: "after the upgrade most of my
settings are gone and I had to set them again". Nothing on the host had actually
lost the settings — nothing had ever written them to the host in the first place.

This store is that host-side copy. It is deliberately a SEPARATE file rather
than a section of ``config.json``:

* ``KiroCrewConfig.save()`` re-emits the whole dataclass, so a key the running
  build does not model is dropped on the next save. A bag of client-owned UI
  keys is exactly the shape that loses that fight.
* ``config.json`` is an operator-facing file. Forty renderer layout keys do not
  belong in it.

Shape and trust model
---------------------
Values are opaque UTF-8 strings, because ``localStorage`` values are strings —
the server never parses or interprets them, so nothing here can grow into a code
path. The client owns which keys are durable (see ``website/src/lib/uiPrefs.ts``);
this module only enforces size bounds and refuses keys that are known to be
credentials, so a buggy or malicious client cannot use the backup as a place to
land the dashboard token in plaintext.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

#: File name inside the data home. Sits next to ``config.json``.
UI_PREFS_FILENAME = "ui-prefs.json"

#: Bounds. These exist so an authenticated but buggy client cannot fill the
#: disk, not as a security boundary — the endpoint is already auth-gated.
MAX_KEYS = 200
MAX_KEY_LEN = 128
MAX_VALUE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 512 * 1024

#: Request-body ceiling for the PUT endpoint, owned HERE so it cannot drift from
#: the store's own limits. The dashboard's shared default is 64 KiB — exactly
#: ``MAX_VALUE_BYTES`` — so a value this store would happily accept could never
#: reach it: the JSON envelope and escaping push it past the cap, the PUT 413s,
#: and the per-key retry 413s too, leaving the preference silently un-backed-up.
#:
#: Sized at twice ``MAX_TOTAL_BYTES`` because a patch may legitimately carry the
#: whole document and JSON escaping can up to double a string in the pathological
#: case (every character a quote or control char). A body larger than that is
#: rejected before decoding, and the per-value and total checks in
#: :func:`merge_ui_prefs` remain the real limits.
MAX_REQUEST_BYTES = 2 * MAX_TOTAL_BYTES

#: Keys that MUST NEVER be persisted here, whatever the client asks for. The
#: dashboard token is a bearer credential: it belongs in the browser's own
#: storage for exactly as long as that origin lives, and writing it to a plain
#: file in the data home would turn a UI-convenience backup into a credential
#: store. Matching is exact plus a substring guard so a renamed variant of the
#: same secret ("kiro_crew_token_v2") is refused too.
DENY_KEYS = frozenset({"kiro_crew_token"})
DENY_SUBSTRINGS = ("token", "secret", "password", "credential", "api_key", "apikey")


#: Serializes every in-process read-modify-write of the file. The PUT handler
#: also holds its own asyncio lock, which orders requests against each other but
#: not against the settings import, which installs an archive's copy from a
#: worker thread (`portability.apply_import_zip`) only where no file exists.
#: Without this a PUT could create the file between that check and the write,
#: and the import would replace it.
_write_lock = threading.Lock()


@contextmanager
def replacing_file() -> Iterator[None]:
    """Hold the writer lock while something other than this module swaps the file.

    The settings import's replace mode installs ``ui-prefs.json`` under a running
    gateway. A PUT from another open tab reads the document under this lock and
    writes its merge back, so without the lock it could read the old file and then
    publish that stale copy over the restored one. Nothing is cached in memory, so
    holding the lock across the swap is the whole fix.
    """
    with _write_lock:
        yield


class UiPrefsError(ValueError):
    """A patch was rejected. The message is safe to return to the client."""


def ui_prefs_path() -> Path:
    """Absolute path of the UI-preferences file inside the data home."""
    return config_dir() / UI_PREFS_FILENAME


def _is_denied(key: str) -> bool:
    if key in DENY_KEYS:
        return True
    lowered = key.lower()
    return any(marker in lowered for marker in DENY_SUBSTRINGS)


def _read_raw(path: Path, *, strict: bool) -> str | None:
    """Read *path* defensively, or return ``None`` when there is nothing usable.

    This file lives in the data home, which an AGENT can write, so the read is
    hardened the same way the superseded-default ack file is:

    * ``O_NOFOLLOW`` refuses a symlink at the final component, and the ``fstat``
      check refuses anything that is not a regular file — otherwise an agent
      could point ``ui-prefs.json`` at ``/dev/zero`` and an owner GET would read
      forever until the gateway died.
    * ``O_NONBLOCK`` means a FIFO planted here returns instead of parking the
      event loop on an open that never completes.
    * The size is capped before the read, so a large regular file cannot be
      slurped either. A backup this store would refuse to WRITE is one it has no
      reason to READ.

    A refused file is "no usable backup" on both paths, not a read failure: there
    was no legitimate content to lose, and the next :func:`merge_ui_prefs` writes
    a real file over whatever was planted (``atomic_write`` renames onto the path
    rather than writing through it). *strict* still governs a genuine read error —
    see :func:`_read_prefs`.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)  # Windows: no implicit newline translation
    if os.path.islink(path):
        # O_NOFOLLOW is POSIX-only; Windows would follow the link. Same verdict.
        logger.warning("ui-prefs: refusing to read %s: not a regular file", path)
        return None
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # ELOOP/ENXIO land here: a symlink refused by O_NOFOLLOW, or a device that
        # cannot be opened non-blocking. Neither is a legitimate backup.
        if exc.errno in (errno.ELOOP, errno.ENXIO, errno.EMLINK):
            logger.warning("ui-prefs: refusing to read %s: not a regular file", path)
            return None
        if strict:
            raise
        logger.warning("ui-prefs: cannot read %s: %s", path, exc)
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            logger.warning("ui-prefs: refusing to read %s: not a regular file", path)
            return None
        if st.st_size > MAX_TOTAL_BYTES:
            logger.warning(
                "ui-prefs: refusing to read %s: %d bytes exceeds the %d-byte store limit",
                path,
                st.st_size,
                MAX_TOTAL_BYTES,
            )
            return None
        chunks: list[bytes] = []
        remaining = MAX_TOTAL_BYTES
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        if strict:
            raise
        logger.warning("ui-prefs: cannot read %s: %s", path, exc)
        return None
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as exc:
        logger.warning("ui-prefs: %s is not valid UTF-8, ignoring it: %s", path, exc)
        return None


def _read_prefs(path: Path, *, strict: bool) -> dict[str, str]:
    """Parse the store at *path*, or return ``{}`` when there is nothing usable.

    *strict* selects what a READ FAILURE means. For a plain read (``strict=False``)
    an unreadable file is just "no backup available", which the client already
    handles. For a read-modify-write (``strict=True``) it is not: returning ``{}``
    there would make the merge write a document holding only the new patch and
    atomically replace a file whose contents it never saw — destroying every
    other preference in it. A temporarily unreadable file in a writable directory
    (EACCES, EIO, a lock) is exactly that case, so the merge lets the OSError out
    and the endpoint answers 503 instead of quietly truncating the backup.

    A MISSING, MALFORMED or REFUSED file legitimately means empty on both paths:
    there is no prior content to lose.
    """
    raw = _read_raw(path, strict=strict)
    if raw is None:
        return {}
    try:
        doc: Any = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # ValueError covers malformed JSON; RecursionError covers a hand-written
        # file nested past CPython's parse depth. Either way there is no usable
        # backup, which is not an error.
        logger.warning("ui-prefs: %s is not usable JSON, ignoring it: %s", path, exc)
        return {}
    if not isinstance(doc, dict):
        return {}
    prefs = doc.get("prefs")
    if not isinstance(prefs, dict):
        return {}
    # Drop anything that is not a str->str pair rather than handing the client a
    # value it cannot put back into localStorage — and drop any VALUE the shared
    # redactors would alter. The key denylist above catches a credential filed
    # under an honest name; this catches one smuggled inside an innocent key by
    # whoever can write the data home (an agent). Dropping rather than serving
    # the redacted form: a redacted preference is not a preference the client
    # can use, and re-syncing it would write the sentinel back over the file.
    return {
        k: v
        for k, v in prefs.items()
        if isinstance(k, str) and isinstance(v, str) and not _is_denied(k) and _is_clean(v)
    }


def _is_clean(value: str) -> bool:
    """True when neither redactor would change *value*."""
    cleaned, _ = redact_exfiltration_urls(value)
    cleaned, _ = redact_credentials(cleaned)
    return cleaned == value


def load_ui_prefs() -> dict[str, str]:
    """Return the stored preferences, or ``{}`` when there are none.

    Never raises: a missing, unreadable, truncated or hand-mangled file means
    "no backup available", which the client already handles (it falls back to
    whatever ``localStorage`` holds). Raising here would break a boot for a
    file that is pure convenience.
    """
    return _read_prefs(ui_prefs_path(), strict=False)


def _utf8_len(text: str, what: str) -> int:
    """Byte length of *text*, or a client-safe rejection.

    A lone surrogate (``"\\ud800"``) is valid JSON and a perfectly ordinary
    ``str`` after parsing, but it cannot be encoded as UTF-8. Left to reach
    ``encode()``/``json.dumps().encode()`` it raises ``UnicodeEncodeError``, which
    is a ``ValueError`` but NOT a :class:`UiPrefsError`, so it escaped the handler
    as a 500 and took the whole patch down with it.
    """
    try:
        return len(text.encode("utf-8"))
    except UnicodeEncodeError:
        raise UiPrefsError(f"{what} is not encodable as UTF-8 (a lone surrogate?)") from None


def _validate_patch(patch: Mapping[str, Any]) -> dict[str, str | None]:
    """Normalize *patch* to ``{key: str | None}``, rejecting anything unusable.

    ``None`` means "delete this key". A rejected patch is rejected WHOLE: a
    partial apply would leave the client believing settings landed that did not.
    """
    cleaned: dict[str, str | None] = {}
    for key, value in patch.items():
        if not isinstance(key, str) or not key:
            raise UiPrefsError("every key must be a non-empty string")
        if len(key) > MAX_KEY_LEN:
            raise UiPrefsError(f"key exceeds {MAX_KEY_LEN} characters: {key[:32]}...")
        _utf8_len(key, "key")
        if _is_denied(key):
            raise UiPrefsError(f"key looks like a credential and is not storable: {key}")
        if value is None:
            cleaned[key] = None
            continue
        if not isinstance(value, str):
            raise UiPrefsError(f"value for {key} must be a string or null")
        if _utf8_len(value, f"value for {key}") > MAX_VALUE_BYTES:
            raise UiPrefsError(f"value for {key} exceeds {MAX_VALUE_BYTES} bytes")
        cleaned[key] = value
    return cleaned


def render_document(prefs: Mapping[str, str]) -> str:
    """Serialize a whole-document *prefs* as the store writes it, or reject it.

    The ONE place the aggregate bounds live. Every path that ends in a
    ``ui-prefs.json`` -- a PUT's merge, and the settings import's parse of an
    archive copy -- goes through it, so a document can only land on disk when
    the SAME ``MAX_KEYS`` / ``MAX_TOTAL_BYTES`` the PUT enforces hold for it.

    The size bound measures the EXACT bytes that land on disk: the trailing
    newline is part of the payload, and the payload is written as bytes so no
    platform newline translation can add to it (text mode turned each "\\n"
    into "\\r\\n" on Windows and pushed a near-cap file over the read limit).
    """
    if len(prefs) > MAX_KEYS:
        raise UiPrefsError(f"too many keys ({len(prefs)} > {MAX_KEYS})")
    text = json.dumps({"prefs": prefs}, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if _utf8_len(text, "stored preferences") > MAX_TOTAL_BYTES:
        raise UiPrefsError(f"stored preferences would exceed {MAX_TOTAL_BYTES} bytes")
    return text


def merge_ui_prefs(patch: Mapping[str, Any]) -> dict[str, str]:
    """Apply *patch* onto the stored preferences and persist the result.

    A ``None`` value deletes its key. Returns the merged mapping. Raises
    :class:`UiPrefsError` when the patch or the resulting document is out of
    bounds, in which case NOTHING is written.

    In-process writers are serialized by ``_write_lock`` (the dashboard handler
    also holds an asyncio lock); the write itself is atomic, so a crash mid-write
    leaves the previous file intact rather than a truncated one. A read failure PROPAGATES
    as ``OSError`` (see ``_read_prefs``): this is a read-modify-write, so
    starting from ``{}`` because the existing file could not be read would
    replace it with just this patch.
    """
    cleaned = _validate_patch(patch)
    with _write_lock:
        return _merge_locked(cleaned)


def _merge_locked(cleaned: Mapping[str, str | None]) -> dict[str, str]:
    """Write an already validated patch; the caller holds ``_write_lock``."""
    merged = _read_prefs(ui_prefs_path(), strict=True)
    for key, value in cleaned.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value

    text = render_document(merged)

    # 0o600 plus restrict_to_owner: nothing here is a credential (see DENY_*), but
    # these are one user's personal settings on a possibly shared host and some
    # name real paths, and the data home's other per-user files are owner-only
    # too. The POSIX mode does nothing on Windows, where a new file inherits the
    # directory's DACL — restrict_to_owner is what narrows it there.
    atomic_write(ui_prefs_path(), text.encode("utf-8"), mode=0o600, restrict_to_owner=True)
    return merged


def parse_imported_ui_prefs(source: Path) -> tuple[dict[str, str], int]:
    """Read an archive's ``ui-prefs.json`` into a patch this store accepts.

    Returns ``(patch, dropped)``. Raises :class:`UiPrefsError` when the file is
    not this store's document at all -- not a regular file within the store's
    size limit, not UTF-8 JSON, or not ``{"prefs": {...}}`` -- or when what
    survives the per-entry filter is more than this store would hold, so the
    import reports it and installs nothing.

    The archive is untrusted input, so each entry is held to exactly what a PUT is
    held to, one key at a time: a credential-shaped key, a non-string value, an
    over-long key or an oversized value is DROPPED and counted, never written,
    and so is a value the shared redactors would alter (the same test the read
    path applies). Per key, so one unstorable entry does not cost the restore of
    every other preference -- the rule the client's per-key retry follows too.

    The surviving entries are then held, AS A WHOLE, to the same ``MAX_KEYS`` /
    ``MAX_TOTAL_BYTES`` a PUT's merge enforces (:func:`render_document`). An
    overflow is a refusal of the whole document, not a further drop: there is no
    principled choice of WHICH entries to shed (the archive's order is the
    archive author's), and a Merge already ends the same way -- the store's
    writer raises on the install and the import reports the document skipped.
    Without this a Replace, which copies the staged file instead of going
    through the writer, installed a ``ui-prefs.json`` the PUT endpoint would
    never have accepted: one over ``MAX_KEYS`` makes every later PUT fail with
    "too many keys", and one over ``MAX_TOTAL_BYTES`` is refused by the next read.
    """
    raw = _read_raw(source, strict=True)
    if raw is None:
        raise UiPrefsError(f"not a readable regular UTF-8 file of at most {MAX_TOTAL_BYTES} bytes")
    try:
        doc: Any = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise UiPrefsError(f"not valid JSON ({exc})") from None
    prefs = doc.get("prefs") if isinstance(doc, dict) else None
    if not isinstance(prefs, dict):
        raise UiPrefsError('not a {"prefs": {...}} document')
    patch: dict[str, str] = {}
    dropped = 0
    for key, value in prefs.items():
        # A null would be a DELETE in a PUT; an archive deletes nothing.
        if not isinstance(value, str) or not _is_clean(value):
            dropped += 1
            continue
        try:
            _validate_patch({key: value})
        except UiPrefsError:
            dropped += 1
            continue
        patch[key] = value
    render_document(patch)
    return patch, dropped


def install_imported_ui_prefs(patch: Mapping[str, str]) -> bool:
    """Install a :func:`parse_imported_ui_prefs` patch, only where this host has no file.

    The dashboard Merge's path in, so it follows that merge's never-overwrite
    contract: a host that already keeps a ``ui-prefs.json`` keeps it whole (the
    browser adopts the host copy on its next load, so a write here would replace
    choices made on this machine). Decided under ``_write_lock``, so a PUT that
    creates the file first wins. Returns whether the file was written; bounds
    apply exactly as for a PUT, so a patch that would breach them raises
    :class:`UiPrefsError` and nothing is written.
    """
    cleaned = _validate_patch(patch)
    with _write_lock:
        if os.path.lexists(ui_prefs_path()):
            return False
        _merge_locked(cleaned)
        return True
