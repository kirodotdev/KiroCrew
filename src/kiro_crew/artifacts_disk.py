"""How an ``ArtifactStore`` keeps its artifacts on disk.

The directory layout (slug to directory, claiming a new slug), the store's fenced,
descriptor-pinned and durable file IO (``_read_text`` / ``_write_text`` /
``_read_bytes`` / ``_write_bytes`` and the primitives under them), the
``meta.json`` / version / image-asset writes, the lifecycle event append, version
listing and pruning, the ``meta.json`` scan and the pinned ``rmtree``; and the values
those writes apply: the retention caps, the clock, the content token and the content
check.

Each function here whose first parameter is ``self`` is an ``ArtifactStore`` method:
the class binds it by name, so it reads the store's own state on the instance, and a
patch of the method on the class or on an instance reaches every caller. The
module-level names are re-exported by :mod:`kiro_crew.artifacts`, which is where
callers import and patch them.

Instance fields read: ``_root``, ``_lock`` and ``_owner_only``.
Instance fields written: none.
"""

from __future__ import annotations

import errno as _errno
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator
from typing import List as _List

from kiro_crew import hooks, pinned_fs, platform_compat
from kiro_crew.artifact_store import records as _records
from kiro_crew.artifact_store.model import (
    Artifact,
    ArtifactAlreadyExistsError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
)
from kiro_crew.artifact_store.rules import _validate_slug, slugify
from kiro_crew.constants import ARTIFACT_MAX_CONTENT_BYTES
from kiro_crew.owner_only_files import GROUP_OTHER_BITS as _GROUP_OTHER_BITS
from kiro_crew.owner_only_files import mkdirs_owner_only as _mkdirs_owner_only
from kiro_crew.owner_only_files import owner_only_opener as _owner_only_opener
from kiro_crew.security import (
    canonical_path_refusal,
    is_sensitive_canonical_path,
    is_unverifiable_path_refusal,
)

if TYPE_CHECKING:
    from kiro_crew.artifacts import ArtifactStore

#: The store's own logger, shared with :mod:`kiro_crew.artifacts`, so a log filter or
#: capture keyed on that name sees this code's records.
logger = logging.getLogger("kiro_crew.artifacts")


# ── Constants ────────────────────────────────────────────────────────────────

#: Maximum number of versions retained per artifact (older versions are pruned
#: when the cap is exceeded).
MAX_VERSIONS = 50

#: Maximum size of an artifact's content blob, in bytes. Locally-authored
#: widget HTML rarely tops a few KB, but cloned/pulled remote artifacts (rich
#: HTML reports, CSVs) routinely exceed 1 MiB — at 1 MiB clone/pull would
#: silently fail on exactly the shared-HTML artifacts bidirectional sync
#: targets. 25 MiB is large enough to bring those down locally while still
#: refusing truly unbounded content. Owned by
#: ``constants.ARTIFACT_MAX_CONTENT_BYTES`` (a leaf) so
#: ``validation.ARTIFACT_CONTENT_MAX`` -- the MCP tool-arg cap -- reads the same
#: name without importing this module; re-exported here for the store's callers.
MAX_CONTENT_BYTES = ARTIFACT_MAX_CONTENT_BYTES

#: Retention cap for auto-registered widget artifacts (see
#: :mod:`kiro_crew.widget_artifacts`). Every chat-emitted ``<mcwidget>`` becomes
#: an artifact automatically, so without a cap a chat-heavy user accumulates one
#: three-file directory per throwaway widget forever and every library listing
#: is an O(N) scan over them. Past this many, the oldest STILL-UNPINNED
#: auto-registered widgets are deleted; pinning one exempts it permanently. Sized
#: so a long working session's widgets all remain addressable for later
#: iteration while the tail is reclaimed.
MAX_AUTO_WIDGET_ARTIFACTS = 200

#: Maximum number of comments retained per artifact (FIFO, like versions/events).
#: ``add_comment`` does load->append->rewrite of the whole comments.json, so an
#: unbounded agent loop (``artifact_post_comment``) would be O(N^2) I/O and grow
#: the sidecar without limit. When the cap is exceeded the OLDEST full thread(s)
#: are dropped (a thread root + its replies together), never a reply orphaned.
MAX_COMMENTS_PER_ARTIFACT = 500

#: Max lifecycle events retained per artifact. FIFO eviction keeps meta.json
#: bounded — at ~150 bytes per event entry, this caps each meta file at
#: roughly 75KB on top of the static metadata, which is well within the
#: tolerable read cost.
MAX_EVENTS_PER_ARTIFACT = 500

_VERSION_FILE_RE = re.compile(r"^v(\d+)\.html$")


# ── Helpers ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 microsecond-precision string.

    Microsecond precision so artifacts created in rapid succession sort
    deterministically by ``updated_at``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


#: Shape of a wire ``content_token`` / ``expected_token``: 256 random bits as hex.
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")


def _new_content_token() -> str:
    """Return a fresh random optimistic-concurrency token."""
    return os.urandom(32).hex()


def _validate_content(content: str) -> str:
    if not isinstance(content, str):
        raise ArtifactValidationError(f"content must be str, got {type(content).__name__}")
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        # The MEASUREMENT is an encode, and content holding an unpaired surrogate
        # cannot be encoded at all. So the one line that asks how big the content
        # is also decides whether it can exist, and this function's whole job is
        # to answer "can this be stored" with an ``ArtifactValidationError`` when
        # the answer is no. Letting ``UnicodeEncodeError`` escape instead turns an
        # artifact a caller can fix into a 500 at every caller that answers a bad
        # artifact with a 400, and a file write meets the same wall: nothing
        # storable comes out of such content.
        #
        # Answered HERE rather than per kind or per route, because the promise is
        # this function's and every path through the store makes it -- ``create``,
        # ``update`` and ``settle_blank`` all open with this call.
        raise ArtifactValidationError(
            "content holds an unpaired surrogate code point "
            f"({content[exc.start:exc.start + 1]!r} at index {exc.start}) that UTF-8 "
            "cannot encode, so it cannot be stored; write a paired character or drop it"
        ) from None
    if len(encoded) > MAX_CONTENT_BYTES:
        raise ArtifactValidationError(f"content exceeds {MAX_CONTENT_BYTES} bytes ({len(encoded)})")
    return content


def _fence_refuses(resolved: Path) -> bool:
    """Ask the sensitive-path fence about a path the store already canonicalised.

    *resolved* MUST be the output of ``os.path.realpath`` computed by the caller
    on the line above, in the same function: that is the precondition of
    ``security.is_sensitive_canonical_path`` (see its docstring), and the
    store's file helpers are pinned to it by ``test_artifacts_pathres.py``.

    Which gate answers is the shared entry point's decision, by thread: off the
    event loop -- a ``run_in_executor`` / ``to_thread`` worker, or a plain
    synchronous caller -- the pre-resolved gate answers with no ``mc-pathres``
    submission. ``list()`` reaches this once per ``meta.json``, and the bounded
    gate costs two pool hops per call, so a listing over a few hundred
    artifacts would fill the two-worker pool with resolutions of paths this
    store has already canonicalised; the fail-closed stall then reads as a
    sensitive-path refusal and drops healthy artifacts from the listing. On the
    loop the bounded gate stays in place, so an on-loop store call behaves as
    it always has, and a caller earns the off-pool gate by offloading, never by
    declaring anything.
    """
    return is_sensitive_canonical_path(str(resolved))


def _fence_refusal(resolved: Path, verb: str) -> str | None:
    """:func:`_fence_refuses` with wording: a resolver stall is passed through as a stall."""
    reason = canonical_path_refusal(str(resolved))
    if reason and not is_unverifiable_path_refusal(reason):
        return f"refusing to {verb} sensitive path: {resolved}"
    return reason


def _open_pinned_for_read(resolved: Path) -> int:
    """Open a store file for reading, pinned to the descriptor it returns.

    *resolved* is a path the caller has already canonicalised and judged with
    :func:`_fence_refuses`. :func:`pinned_fs.open_fenced_for_read` refuses a
    link at the final component, requires a regular file with a single link,
    and asks :func:`_fence_refuses` about the kernel's own path for the opened
    inode exactly when that path differs from the judged one. Refusals raise
    :class:`ArtifactError`; a missing file raises ``FileNotFoundError``.
    """
    return pinned_fs.open_fenced_for_read(
        resolved,
        fence=lambda fd_real: _fence_refuses(Path(fd_real)),
        refusal=ArtifactError,
    )


def _owner_only_tmp_opener(path: str, flags: int) -> int:
    """:func:`_owner_only_opener` for a fixed-name ``.tmp`` that a crash can leave behind.

    ``os.open`` applies the mode only when it creates the file, so a ``.tmp`` an
    earlier version left at ``0644`` would be truncated, reused and then
    published at that mode by the rename. The descriptor belongs to this write
    alone (no lock is ever taken on a ``.tmp``), so it is narrowed here before
    any byte is written.
    """
    fd = _owner_only_opener(path, flags)
    if not platform_compat.IS_POSIX:
        return fd
    try:
        mode = os.fstat(fd).st_mode & 0o7777
        if mode & _GROUP_OTHER_BITS:
            os.fchmod(fd, mode & ~_GROUP_OTHER_BITS)
    except BaseException:
        os.close(fd)
        raise
    return fd


# The errnos that mean "this descriptor or filesystem has no fsync", as opposed
# to "the writeback failed". Only the first class may be ignored: a descriptor
# that cannot be synced on a given platform must not make the store unusable
# there. Built with ``getattr`` because the errno module only defines the name
# the host's C library has, and the set collapses duplicates on the platforms
# where ENOTSUP and EOPNOTSUPP share a value.
#
# The import is aliased private (``_errno``) as in :mod:`kiro_crew.artifacts`:
# the facade's ``__all__`` is derived from what it binds, so a bare ``import
# errno`` would publish the errno module as part of the store's facade.
_SYNC_UNSUPPORTED_ERRNOS = frozenset(
    code
    for code in (
        getattr(_errno, name, None) for name in ("EINVAL", "ENOSYS", "ENOTSUP", "EOPNOTSUPP")
    )
    if code is not None
)


def _sync_before_publish(fh: Any) -> None:
    """Flush *fh* to the device before its temp file is renamed into place.

    A temp file plus a rename makes the SWAP atomic; it does not make the bytes
    durable. The rename can land while the content is still only in the page
    cache, so a crash leaves the artifact's name pointing at a file the kernel
    never finished writing -- a torn read that the atomic rename was supposed to
    rule out. Flushing the Python buffer and then ``fsync`` is what makes "the
    rename published real bytes" true rather than likely.

    The failure this raises on is the one that matters. A filesystem with delayed
    allocation reports a full disk or a failing device at ``fsync``, not at
    ``write``; treating that as advisory would let the caller's rename replace
    the last good file with a partial one and report success, which is the exact
    data loss the sync is here to prevent. So a writeback error propagates, the
    caller never reaches its rename, and the previous version stays published.

    An errno in :data:`_SYNC_UNSUPPORTED_ERRNOS` says the platform has no sync
    for this descriptor rather than that the write failed, and that is logged and
    ignored: the atomicity the rename gives still holds where the durability
    cannot be had.
    """
    fh.flush()
    try:
        os.fsync(fh.fileno())
    except OSError as exc:
        if exc.errno not in _SYNC_UNSUPPORTED_ERRNOS:
            raise
        logger.debug("fsync is unsupported for fd %s: %s", fh.fileno(), exc)


def _write_tmp_then_publish(
    tmp: Path,
    resolved: Path,
    write_body: Callable[[Any], None],
    mode: str,
    **open_kwargs: Any,
) -> None:
    """Write *tmp*, sync it to the device, then rename it onto *resolved*.

    The temp file is the write's alone; nothing publishes it until the rename, so
    a failure before the rename must not leave it behind. A writeback error
    reported at ``fsync`` (:func:`_sync_before_publish`) is exactly such a
    failure: on a full disk the sync raises, the rename is skipped, and the
    half-written ``meta.json.tmp`` would otherwise sit on a disk that is already
    out of room. Unlink it on any error before the rename, best-effort -- the
    original error is what the caller must see, so a failure to clean up is
    swallowed.
    """
    try:
        with open(tmp, mode, opener=open_kwargs.pop("opener", None), **open_kwargs) as fh:
            write_body(fh)
            _sync_before_publish(fh)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    tmp.replace(resolved)


def _prepare_store_write(resolved: Path, *, owner_only: bool) -> Callable[[str, int], int] | None:
    """Create *resolved*'s parent and return the opener for its tmp file."""
    if owner_only:
        _mkdirs_owner_only(resolved.parent)
        return _owner_only_tmp_opener
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return None


def _ensure_content_token_locked(self: ArtifactStore, art: Artifact) -> None:
    """Assign and persist a token for an untokened store-backed artifact.

    Called under the store lock by paths that serve or compare the token.
    """
    if not art.content_token:
        art.content_token = _new_content_token()
        self._write_meta(art)


def _rotate_content_token_locked(self: ArtifactStore, slug: str) -> str:
    """Persist a fresh token for *slug* before its content is rewritten.

    The token lives in meta.json and the content in current.html, and the
    two writes are not atomic together. Rotating first means a failure
    between them leaves a token that refuses every earlier read (a false
    409), never one that still admits a stale save over the new content.
    Only the token is written here; the caller persists its other changes
    with the rest of its write. Called under the store lock.
    """
    token = _new_content_token()
    on_disk = self._load_meta(slug)
    on_disk.content_token = token
    self._write_meta(on_disk)
    return token


def list_versions(self: ArtifactStore, slug: str) -> _List[int]:
    """Return the sorted list of stored version numbers for a slug.

    Pruned-out older versions are absent.
    """
    slug = _validate_slug(slug)
    with self._lock:
        adir = self._artifact_dir(slug)
        if not adir.exists():
            raise ArtifactNotFoundError(f"artifact not found: {slug}")
        versions_dir = adir / "versions"
        stored: _List[int] = []
        if versions_dir.exists():
            for f in versions_dir.iterdir():
                m = _VERSION_FILE_RE.match(f.name)
                if m:
                    stored.append(int(m.group(1)))
        return sorted(set(stored))


def _artifact_dir(self: ArtifactStore, slug: str) -> Path:
    adir = (self._root / slug).resolve(strict=False)
    # Defense in depth: ensure resolved path is still under root.
    if self._root.resolve(strict=False) not in adir.parents and adir != self._root.resolve(
        strict=False
    ):
        raise ArtifactValidationError(f"slug escapes artifact root: {slug}")
    return adir


def _unique_slug(self: ArtifactStore, base: str) -> str:
    """Append ``-2``, ``-3`` ... until an unused slug is found."""
    candidate = base
    n = 1
    while self._artifact_dir(candidate).exists():
        n += 1
        candidate = f"{base[:75]}-{n}"
    return candidate


def _claim_slug(self: ArtifactStore, slug: str | None, name: str) -> tuple[str, str]:
    """Resolve the slug a new artifact takes, as ``(slug, collided_with)``.

    With no ``slug`` one is derived from ``name`` and suffixed until free;
    ``collided_with`` then names the derived slug when a suffix was needed, and
    is empty otherwise -- the uniquifier is the only place that knows it
    suffixed. An explicit ``slug`` is validated and refused, never renamed,
    when taken. The caller must hold ``self._lock`` until the directory exists.
    """
    if slug is None:
        derived = slugify(name)
        slug = self._unique_slug(derived)
        return slug, derived if slug != derived else ""
    slug = _validate_slug(slug)
    if self._artifact_dir(slug).exists():
        raise ArtifactAlreadyExistsError(f"artifact already exists: {slug}")
    return slug, ""


def _write_artifact(self: ArtifactStore, art: Artifact, content: str) -> None:
    adir = self._artifact_dir(art.slug)
    self._mkdirs(adir / "versions")  # creates adir too
    self._write_text(adir / "current.html", content)
    self._snapshot_version(art.slug, art.version, adir / "current.html")
    art.content_token = _new_content_token()
    self._write_meta(art)


def _write_image_artifact(self: ArtifactStore, art: Artifact, data: bytes) -> None:
    """Write an image artifact: empty text body + the raster asset sidecar.

    Mirrors :meth:`_write_artifact` so image records share the exact same
    directory shape (``current.html`` + ``versions/`` + ``meta.json``) and
    every text-store helper — ``get``, version snapshotting, ``_rmtree``
    delete — works on them unchanged. ``current.html`` is written empty per
    the image-kind contract; the bytes go to ``asset.<ext>`` through the
    gated byte writer.
    """
    adir = self._artifact_dir(art.slug)
    self._mkdirs(adir / "versions")  # creates adir too
    self._write_text(adir / "current.html", art.content or "")
    self._snapshot_version(art.slug, art.version, adir / "current.html")
    assert art.image is not None  # set by create_image before this is called
    self._write_bytes(adir / f"asset.{art.image.ext}", data)
    art.content_token = _new_content_token()
    self._write_meta(art)


def _snapshot_version(self: ArtifactStore, slug: str, version: int, src: Path) -> None:
    target = self._artifact_dir(slug) / "versions" / f"v{version}.html"
    # Defense in depth: route the read through the gated helper so the
    # sensitive-path check fires on every filesystem read, even when
    # ``src`` is a store-internal path constructed by the store itself.
    # Per security rule 1: a read either goes through hooks.py or, as
    # here, asks ``is_sensitive_canonical_path`` on the canonicalised
    # path and opens through ``pinned_fs.open_fenced_for_read``.
    self._write_text(target, self._read_text(src))


def _write_meta(self: ArtifactStore, art: Artifact) -> None:
    path = self._artifact_dir(art.slug) / "meta.json"
    self._write_text(path, _records.encode_meta(art))


def _append_event(
    self: ArtifactStore,
    art: Artifact,
    *,
    type: str,
    by: str | None = None,
    session_id: str | None = None,
    version: int | None = None,
    from_version: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Append a lifecycle entry to ``art.events``, stamped by this module's clock.

    The entry shape and the type allowlist are
    :func:`kiro_crew.artifact_store.records.append_event`'s; the timestamp and
    the FIFO cap (``MAX_EVENTS_PER_ARTIFACT``) come from this module. The caller
    persists.
    """
    _records.append_event(
        art,
        clock=_now_iso,
        cap=MAX_EVENTS_PER_ARTIFACT,
        type=type,
        by=by,
        session_id=session_id,
        version=version,
        from_version=from_version,
        metadata=metadata,
    )


def _load_meta(self: ArtifactStore, slug: str) -> Artifact:
    path = self._artifact_dir(slug) / "meta.json"
    if not path.exists():
        raise ArtifactNotFoundError(f"artifact not found: {slug}")
    return self._read_meta_file(path)


def _read_meta_file(self: ArtifactStore, path: Path) -> Artifact:
    return _records.decode_meta(json.loads(self._read_text(path)), path)


def _read_text(self: ArtifactStore, path: Path) -> str:
    """Read a store-internal text file through a pinned descriptor.

    The fence is asked with the ``realpath`` computed on the line above (see
    :func:`_fence_refuses` for which gate answers, and why). The opened
    descriptor is checked again so a replacement at the final name cannot
    redirect the read after that first decision.
    """
    resolved = Path(os.path.realpath(path))
    if reason := _fence_refusal(resolved, "read"):
        raise ArtifactError(reason)
    fd = _open_pinned_for_read(resolved)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        return fh.read()


def _write_text(self: ArtifactStore, path: Path, text: str) -> None:
    """Atomically write a store-internal file through the sensitive-path fence.

    Same fence and same precondition as :meth:`_read_text`. This is the
    read+write fence (``_SENSITIVE_HOME_DIRS`` plus the keystone publish
    artifacts): the write-only superset ``is_sensitive_write_path`` guards the
    agent's file-edit tool, has no pre-resolved form, and adopting it here
    would change the decision rather than the submission path.
    """
    resolved = Path(os.path.realpath(path))
    if reason := _fence_refusal(resolved, "write"):
        raise ArtifactError(reason)
    self._refuse_write_outside_root(path, resolved)
    # Atomic write: tmp file + rename. Inside the data home the tmp is 0600
    # before the first byte is written (created so, or narrowed when a crash
    # left one behind), so the published file is never readable by others.
    opener = _prepare_store_write(resolved, owner_only=self._owner_only)
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    _write_tmp_then_publish(
        tmp, resolved, lambda fh: fh.write(text), "w", encoding="utf-8", opener=opener
    )


def _read_bytes(self: ArtifactStore, path: Path) -> bytes:
    """Binary sibling of :meth:`_read_text` (image asset reads).

    Same sensitive-path gate and the same descriptor checks as text reads.
    """
    resolved = Path(os.path.realpath(path))
    if reason := _fence_refusal(resolved, "read"):
        raise ArtifactError(reason)
    fd = _open_pinned_for_read(resolved)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _read_image_asset_bytes(self: ArtifactStore, path: Path) -> bytes:
    """Read an image sidecar with the open descriptor as the unit of trust.

    :meth:`_read_bytes` resolves the path, checks it, then opens it by name —
    which leaves a window where the sidecar is replaced with a link to
    something sensitive between the check and the open. The asset endpoint is
    reachable with nothing but a slug, so that window is worth closing here:
    the open is ``O_NOFOLLOW`` and the inode it actually opened is validated
    (regular file, not hardlinked), so a swapped sidecar is refused rather
    than followed.

    ``within_root`` is deliberately NOT passed to the helper: its containment
    check reads the descriptor's real path via ``/proc/self/fd`` or
    ``F_GETPATH`` and fails closed when neither is available — which is every
    Windows host, so requiring it would make image assets permanently
    unreadable there. Containment is enforced here instead, with a
    ``realpath`` comparison that behaves the same on every platform. That
    check is load-bearing rather than belt-and-braces: the helper resolves
    the path before opening it, so ``O_NOFOLLOW`` alone never sees a swapped
    symlink — it sees the target.
    """
    root = Path(os.path.realpath(self._root))
    resolved = Path(os.path.realpath(path))
    if resolved != root and root not in resolved.parents:
        raise ArtifactNotFoundError(f"image asset escapes the store root: {path.name}")
    data = hooks.safe_read_file_bytes_nolink(str(resolved), max_bytes=MAX_CONTENT_BYTES)
    if data is None:
        # Refused: not a regular file, hardlinked, or unreadable.
        # Indistinguishable from "gone" to the caller by design.
        raise ArtifactNotFoundError(f"image asset is not readable: {path.name}")
    return data


def _refuse_write_outside_root(self: ArtifactStore, asked: Path, resolved: Path) -> None:
    """Refuse a write whose RESOLVED path has left the store.

    Both writers resolve the path before opening it, which is what keeps the
    sensitive-path fence honest -- but resolving is also what follows a link.
    A symlink planted at a store file (``current.html``, a version, a
    ``meta.json``) resolves to the link's target, and the fence only refuses a
    target it considers sensitive: anywhere else, the write lands outside the
    store at a path the artifact's own name does not point to.

    The claim is scoped to what the CALLER asked for, which is what keeps
    these two general-purpose helpers general: a path that never pointed into
    the store makes no containment promise and is left to the fence alone
    (``test_artifacts_pathres`` exercises them exactly that way). A path that
    DID point into the store must still be in it once resolved -- so the only
    thing refused here is a link that moved a store file out of the store.

    The root's own realpath is the comparison, because a store rooted at a
    symlinked data home is an ordinary setup and must keep working.
    """
    root = Path(os.path.realpath(self._root))
    asked_abs = Path(os.path.abspath(asked))
    if root not in asked_abs.parents:
        return
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ArtifactError(
            f"refusing to write {asked}: it resolves to {resolved}, outside the "
            f"artifact store at {root}. A link at that name would send the write "
            f"somewhere the path does not name"
        ) from None


def _write_bytes(self: ArtifactStore, path: Path, data: bytes) -> None:
    """Binary sibling of :meth:`_write_text` (image asset writes).

    Same sensitive-path gate and same atomic tmp-file + rename so a reader
    never observes a half-written asset.
    """
    resolved = Path(os.path.realpath(path))
    if reason := _fence_refusal(resolved, "write"):
        raise ArtifactError(reason)
    self._refuse_write_outside_root(path, resolved)
    opener = _prepare_store_write(resolved, owner_only=self._owner_only)
    tmp = resolved.with_suffix(resolved.suffix + ".tmp")
    _write_tmp_then_publish(tmp, resolved, lambda fh: fh.write(data), "wb", opener=opener)


def _prune_versions(self: ArtifactStore, slug: str) -> None:
    versions_dir = self._artifact_dir(slug) / "versions"
    if not versions_dir.exists():
        return
    files: _List[tuple[int, Path]] = []
    for f in versions_dir.iterdir():
        m = _VERSION_FILE_RE.match(f.name)
        if m:
            files.append((int(m.group(1)), f))
    if len(files) <= MAX_VERSIONS:
        return
    files.sort(key=lambda t: t[0])
    for _v, f in files[: len(files) - MAX_VERSIONS]:
        try:
            f.unlink()
        except OSError as exc:
            logger.warning("prune failed for %s: %s", f, exc)


def _iter_meta_paths(self: ArtifactStore) -> Iterator[Path]:
    if not self._root.exists():
        return
    for child in self._root.iterdir():
        if child.is_dir():
            meta = child / "meta.json"
            if meta.exists():
                yield meta


def _rmtree(path: Path) -> None:
    """Remove *path* and everything under it, anchored to PINNED directories.

    Stdlib-only (no ``shutil``), and deliberately not a walker. Screening a name
    for a link and then acting on that name are two operations on two objects,
    and every walker in the stdlib re-resolves the name in between:
    ``os.walk``'s own descent-time re-check is ``os.path.islink``, which answers
    False for a Windows junction, and ``rglob`` descends one unconditionally. A
    junction planted at a child that screened clean was therefore still
    descended, and this function unlinked the link target's files -- outside the
    artifact store. Creating a junction needs no elevation, and the agent both
    triggers a delete and can retry it, so the window is ordinary.

    :class:`platform_compat.PinnedDirectory` is what closes it, and it closes
    BOTH halves: the descent refuses a link in the open itself rather than in a
    check before it, and each removal is anchored to the directory that was
    inspected -- ``dir_fd``-relative on POSIX, and by a path the Windows pin
    holds still. A parent stays pinned while its child is being emptied, so the
    whole chain is pinned for the length of the sweep.

    Failures: each entry that will not go is logged and the sweep continues, so
    the warnings name every residual rather than stopping at the first. The
    removal of *path* itself is NOT guarded -- a residual anywhere keeps it
    non-empty, so it fails, and the caller must see that: it logs a successful
    delete and fires its ``"delete"`` event unconditionally, and a Windows
    sharing violation on a store file is an ordinary occurrence.
    """

    def _empty(pinned: platform_compat.PinnedDirectory) -> None:
        for name in sorted(pinned.names()):
            try:
                if pinned.is_link(name) or not pinned.is_dir(name):
                    pinned.unlink(name)
                    continue
                child = pinned.child_if_real_dir(name)
                if child is None:
                    # Replaced between the screen above and the open, and the open
                    # refusing IS the protection working. Whatever is at the name
                    # now is a link or a plain file, so remove it as one; a real
                    # directory (including a chain too deep to sweep) re-raises out
                    # of the helper and is reported as a residual below.
                    pinned.unlink(name)
                    continue
                with child:
                    _empty(child)
                pinned.rmdir(name)
            except OSError as exc:
                logger.warning(
                    "rmtree partial failure at %s: %s", os.path.join(pinned.path, name), exc
                )

    # The PARENT is pinned too, so even the root's own removal is anchored
    # rather than a by-name ``rmdir`` the pinning above would leave as the one
    # unprotected step.
    with platform_compat.pinned_directory(path.parent) as parent:
        with parent.child(path.name) as root:
            _empty(root)
        parent.rmdir(path.name)
