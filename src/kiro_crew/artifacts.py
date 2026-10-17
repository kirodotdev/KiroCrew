"""Artifacts — persistent identity, versioning, and iteration for LLM-generated UI.

Storage layout
--------------
``~/.kiro/crew/artifacts/<slug>/``
  ``meta.json``        canonical metadata
  ``current.html``     latest rendered content
  ``versions/v1.html`` older versions, never overwritten

The ``slug`` is a URL-safe, human-readable identifier derived from the artifact
name (e.g. ``"CR Queue Dashboard"`` -> ``"cr-queue-dashboard"``). Slugs are the
stable handle the agent uses to iterate on an artifact across sessions.

Each artifact tracks its full version history. ``update()`` writes a new
version under ``versions/`` *and* replaces ``current.html``; older versions are
retained until the configured cap (``MAX_VERSIONS``) is reached, at which
point the oldest are pruned.

Security
~~~~~~~~
- Slugs are validated against ``_SLUG_RE`` to block path-traversal attempts.
- All filesystem writes go through ``Path.resolve()`` + a parent-directory
  check to prevent escapes.
- The sensitive-path fence is queried before any read/write, so the store
  cannot accidentally land under ``~/.aws``, ``~/.ssh``, etc. The store's own
  file helpers hand it the ``realpath`` they already computed through
  ``security.canonical_path_refusal()`` (see ``_fence_refusal``), which
  answers off the event loop without a resolver-pool submission and with the
  bounded ``security.sensitive_path_refusal()`` on the loop; the root check
  asks ``sensitive_path_refusal()`` and the source-file pointers ask the
  bounded ``is_sensitive_path()`` directly.
- Store reads are pinned to the descriptor they open
  (``pinned_fs.open_fenced_for_read`` via ``_open_pinned_for_read``): the open
  refuses a link at the final name, the inode must be a regular file with one
  link, and the fence judges the kernel's own path for that inode when it
  differs from the path already judged, so a swap between the check and the
  open cannot redirect the read.
- Tool invocations emit SEL audit events via ``sel().log_tool_invocation()``.

The MCP tools (``artifact_save`` etc.) and HTTP handlers wrap this module --
this file deliberately holds no networking, validation-layer, or rendering
logic.

The rules the store applies live in :mod:`kiro_crew.artifact_store`: ``model``
(the records and errors), ``rules`` (field grammar and kind policy), ``images``
(the raster allowlist and header sniffing), ``records`` (the persisted file
formats), ``comments`` (the comment-thread rules) and ``folders`` (the folder
tree). The store's own code is in the ``kiro_crew.artifacts_*`` owners, which
``ArtifactStore`` binds member by member: ``artifacts_disk`` (the directory layout,
the fenced and durable file IO, versions, and the caps, clock and content token
those writes apply), ``artifacts_source_link`` (the live ``source_path`` pointer),
``artifacts_writes`` (create, image create, update and settle-blank),
``artifacts_scans`` (lookups and sweeps over every ``meta.json``) and
``artifacts_comments`` (the comment sidecar). This module keeps the class itself --
its construction, the shared per-root lock, the change listener, the reads, the
listing and the per-record metadata verbs -- with the default singletons, and keeps
its whole import surface by re-exporting each moved name with one identity, so
callers import from here.
"""

from __future__ import annotations

import errno as _errno  # noqa: F401 - facade surface
import hashlib  # noqa: F401 - facade surface
import hmac as _hmac  # noqa: F401 - facade surface
import importlib
import json  # noqa: F401 - facade surface
import logging
import os  # noqa: F401 - facade surface
import re  # noqa: F401 - facade surface
import sys as _sys
import tempfile  # noqa: F401 - facade surface
import threading
import unicodedata  # noqa: F401 - facade surface
import uuid  # noqa: F401 - facade surface
from dataclasses import asdict, dataclass, field  # noqa: F401 - facade surface
from dataclasses import fields as fields_of  # noqa: F401 - facade surface
from datetime import datetime, timezone  # noqa: F401 - facade surface
from pathlib import Path
from types import MappingProxyType, ModuleType  # noqa: F401 - facade surface
from typing import Iterator  # noqa: F401 - facade surface
from typing import Mapping  # noqa: F401 - facade surface
from typing import TYPE_CHECKING, Any, Callable
from typing import Iterable as _Iterable
from typing import List as _List

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import artifacts_comments as _artifacts_comments
from kiro_crew import artifacts_disk as _artifacts_disk
from kiro_crew import artifacts_scans as _artifacts_scans
from kiro_crew import artifacts_source_link as _artifacts_source_link
from kiro_crew import artifacts_writes as _artifacts_writes
from kiro_crew import hooks, pinned_fs, platform_compat  # noqa: F401 - facade surface
from kiro_crew.artifact_source import is_verifiable_root  # noqa: F401 - facade surface
from kiro_crew.artifact_store import comments as _threads  # noqa: F401 - facade surface
from kiro_crew.artifact_store import dashboard_package as _dashboard  # noqa: F401 - facade surface
from kiro_crew.artifact_store import records as _records
from kiro_crew.artifact_store import rules as _rules  # noqa: F401 - facade surface
from kiro_crew.artifact_store.comments import (  # noqa: F401 - facade surface
    filter_comments_for_forward,
)
from kiro_crew.artifact_store.folders import ArtifactFolderStore
from kiro_crew.artifact_store.images import (  # noqa: F401 - facade surface
    _IMAGE_MIME_EXT,
    _sniff_image_dimensions,
)
from kiro_crew.artifact_store.model import (  # noqa: F401 - facade surface
    EXPECT_ABSENT,
    Artifact,
    ArtifactAlreadyExistsError,
    ArtifactComment,
    ArtifactConflictError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactPublication,
    ArtifactReplacedError,
    ArtifactStillPublishedError,
    ArtifactValidationError,
    ForkMetadata,
    ImageMetadata,
    _ExpectAbsent,
)
from kiro_crew.artifact_store.rules import (  # noqa: F401 — re-export for API compatibility
    USER_SELECTABLE_KINDS,
    _fold_session_keys,
    _infer_kind,
    _markdown_misclassification_reason,
    _session_touched,
    _session_touched_any,
    _validate_description,
    _validate_kind,
    _validate_name,
    _validate_slug,
    _validate_source,
    _validate_source_path,
    _validate_tags,
    detect_editor_kind,
    has_unthemed_hardcoded_colors,
    is_document_path,
    normalize_tag,
    slugify,
)
from kiro_crew.artifacts_disk import (  # noqa: F401 - facade surface
    _SYNC_UNSUPPORTED_ERRNOS,
    _TOKEN_RE,
    _VERSION_FILE_RE,
    MAX_AUTO_WIDGET_ARTIFACTS,
    MAX_COMMENTS_PER_ARTIFACT,
    MAX_CONTENT_BYTES,
    MAX_EVENTS_PER_ARTIFACT,
    MAX_VERSIONS,
    _fence_refusal,
    _fence_refuses,
    _new_content_token,
    _now_iso,
    _open_pinned_for_read,
    _owner_only_tmp_opener,
    _prepare_store_write,
    _sync_before_publish,
    _validate_content,
    _write_tmp_then_publish,
)
from kiro_crew.config.loader import KiroCrewConfig, config_dir  # noqa: F401 - facade surface
from kiro_crew.constants import ARTIFACT_MAX_CONTENT_BYTES  # noqa: F401 - facade surface
from kiro_crew.deploy.webapp_types import (  # noqa: F401 - facade surface
    WebAppArchitecture,
    WebAppCost,
    WebAppDeployTarget,
    WebAppLifecycle,
    WebAppMetadata,
    WebAppTeardown,
    webapp_metadata_from_dict,
)
from kiro_crew.metrics.events import ARTIFACTS_CREATED, emit_counter  # noqa: F401 - facade surface
from kiro_crew.owner_only_files import (  # noqa: F401 - facade surface
    GROUP_OTHER_BITS as _GROUP_OTHER_BITS,
)
from kiro_crew.owner_only_files import is_owner_only_target as _is_owner_only_target
from kiro_crew.owner_only_files import mkdirs_owner_only as _mkdirs_owner_only
from kiro_crew.owner_only_files import (  # noqa: F401 - facade surface
    owner_only_opener as _owner_only_opener,
)
from kiro_crew.publish_provider import DEFAULT_PROVIDER  # noqa: F401 - facade surface
from kiro_crew.security import (  # noqa: F401 - facade surface
    canonical_path_refusal,
    is_sensitive_canonical_path,
    is_sensitive_path,
    is_unverifiable_path_refusal,
    sensitive_path_refusal,
)
from kiro_crew.slugs import slug_hash_fallback  # noqa: F401 - facade surface

# ── Forwarded names ──────────────────────────────────────────────────────────
# Rule data an owner's own code reads has ONE binding, in that owner. This module
# forwards those names instead of holding a copy of them: a read here answers with
# the owner's binding, and a write or a delete here (``monkeypatch``, ``mock.patch``)
# reaches it, so one patch through either module steers every reader. Both resolve
# the owner from ``sys.modules`` and import only on a miss.

#: Forwarded name -> the dotted module that DEFINES it. The mirror installed at the
#: end of this module forwards each one, so this module binds none of them itself.
_FORWARDS: dict[str, str] = {
    "FOLDER_PATH_SEP": "kiro_crew.artifact_store.folders",
    "MAX_FOLDER_DEPTH": "kiro_crew.artifact_store.folders",
    "_NO_GENERATIONS": "kiro_crew.artifact_store.folders",
    "_sniff_jpeg_dimensions": "kiro_crew.artifact_store.images",
    "_sniff_webp_dimensions": "kiro_crew.artifact_store.images",
    "ALLOWED_EVENT_TYPES": "kiro_crew.artifact_store.records",
    "ALLOWED_KINDS": "kiro_crew.artifact_store.rules",
    "ALLOWED_SOURCES": "kiro_crew.artifact_store.rules",
    "DOC_EXTENSIONS": "kiro_crew.artifact_store.rules",
    "MAX_DESCRIPTION_LEN": "kiro_crew.artifact_store.rules",
    "MAX_NAME_LEN": "kiro_crew.artifact_store.rules",
    "MAX_SOURCE_PATH_LEN": "kiro_crew.artifact_store.rules",
    "MAX_TAGS": "kiro_crew.artifact_store.rules",
    "MAX_TAG_LEN": "kiro_crew.artifact_store.rules",
    "_EXT_KIND_MAP": "kiro_crew.artifact_store.rules",
    "_HARDCODED_COLOR_RE": "kiro_crew.artifact_store.rules",
    "_HREF_ATTR_RE": "kiro_crew.artifact_store.rules",
    "_HTML_SNIFF_MARKERS": "kiro_crew.artifact_store.rules",
    "_MD_HEADING_RE": "kiro_crew.artifact_store.rules",
    "_SLUG_NORMALIZE_RE": "kiro_crew.artifact_store.rules",
    "_SLUG_RE": "kiro_crew.artifact_store.rules",
    "_SVG_ROOT_RE": "kiro_crew.artifact_store.rules",
    "_strip_session_scope": "kiro_crew.artifact_store.rules",
}

#: Name this module serves but does not bind -> the module that answers a read of it.
#: Bound empty before the install below, which fills it with every forwarded name.
_EXPORTS: dict[str, str] = {}


def _module(dotted: str) -> ModuleType:
    """Return a module from where modules are stored, importing it only on a miss.

    ``sys.modules`` is read first, so a caller that rebinds ``importlib.import_module``
    for its own reasons cannot reroute these names, and a purged and reimported owner
    is seen at once.
    """
    try:
        return _sys.modules[dotted]
    except KeyError:
        return importlib.import_module(dotted)


# Hidden from type checkers, which resolve each forwarded name from the typed imports
# at the end of this module instead of accepting any name at all.
if not TYPE_CHECKING:

    def __getattr__(name: str) -> Any:
        """Read a name added to ``_EXPORTS`` after the install from its owner (:pep:`562`).

        ``_facade_mirror.MirroredModule`` answers every name the mirror serves, so this
        declines each of them: ``install`` refuses a resolver that answers one.
        """
        if name not in _EXPORTS or name in _MIRROR.served:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(_module(_EXPORTS[name]), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


logger = logging.getLogger(__name__)

# The errors keep naming this module as their home, so a traceback and a logged error
# type read the same whichever owner raises them; ``inspect.getsource`` cannot find
# them through that name. Every other moved class keeps its owner's name, so the
# source lookup finds its definition.
for _moved in (
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactAlreadyExistsError,
    ArtifactValidationError,
    ArtifactStillPublishedError,
    ArtifactReplacedError,
    ArtifactConflictError,
):
    _moved.__module__ = __name__
del _moved


def slug_is_well_formed(slug: str) -> bool:
    """Whether this string could name an artifact, said without asking whether one exists.

    Defined on top of the same validator every store method applies, so a caller deciding
    what to do with a slug the store has not resolved cannot disagree with the store about
    which strings are slugs at all. The publication guard needs exactly this question: an
    artifact created inside a delete's own window has no record to resolve, so the guard has
    to be taken on the NAME, while a malformed name is still passed through unguarded so the
    store can answer for it.
    """
    try:
        _validate_slug(slug)
    except ArtifactValidationError:
        return False
    return True


# ── Store ────────────────────────────────────────────────────────────────────


#: One lock per resolved artifact root, shared across every ``ArtifactStore``
#: instance pointed at that root -- not just the process-wide singleton
#: (:func:`get_default_store`). A caller that constructs its own
#: ``ArtifactStore()`` against the default root (as opposed to threading the
#: singleton through) would otherwise get its own private
#: ``threading.Lock()``, unserialized against every other instance on the
#: same root: two writers (or a writer and a reader) could interleave their
#: file operations, corrupting a version or serving a stale read. Keyed by
#: the resolved root path so distinct roots (tests' isolated tmp_path stores)
#: still get independent locks.
_root_locks: dict[str, threading.Lock] = {}
_root_locks_guard = threading.Lock()


def _lock_for_root(root: Path) -> threading.Lock:
    key = str(root)
    with _root_locks_guard:
        lock = _root_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _root_locks[key] = lock
        return lock


class ArtifactStore:
    """File-system backed store for artifacts.

    Thread-safe via a coarse-grained lock, shared across every instance
    pointed at the same root (see :func:`_lock_for_root`) -- concurrent
    writes to the same artifact are serialized regardless of how many
    ``ArtifactStore`` objects address it.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or (config_dir() / "artifacts")).expanduser()
        # Optional change-listener fired after a content-affecting mutation
        # (create / content-update / delete). Lets the gateway observe every
        # write path — agent (MCP-proxied), dashboard, bookmark, CLI, and the
        # remote pull/clone paths all funnel through this store in the
        # gateway process, so one listener here catches them all without the
        # store importing (or knowing about) the knowledge package.
        self._change_listener: Callable[[str, str], None] | None = None
        # Refuse to land under any sensitive path. is_sensitive_path() handles
        # symlink resolution, so resolve() before checking.
        resolved = self._root.resolve(strict=False)
        if reason := sensitive_path_refusal(str(resolved)):
            if is_unverifiable_path_refusal(reason):
                raise ArtifactError(reason)
            raise ArtifactError(f"refusing to use sensitive path as artifact root: {resolved}")
        # Keyed by the RESOLVED root so a symlinked alias of the same
        # directory still shares the lock, not just a literal path match.
        self._lock = _lock_for_root(resolved)
        # Owner-only (0600 files, 0700 directories) when the root is in the data
        # home. Decided once, on the root as it is SPELLED: the data-home test is
        # lexical, and every path below the root is resolved (_artifact_dir), so
        # under a symlinked, relocated home a per-write test would never match.
        self._owner_only = _is_owner_only_target(self._root)
        self._mkdirs(self._root)

    def _mkdirs(self, path: Path) -> None:
        """``mkdir -p`` for a store directory, 0700 when the store is owner-only."""
        if self._owner_only:
            _mkdirs_owner_only(path)
        else:
            path.mkdir(parents=True, exist_ok=True)

    # ── public API ────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    # The store's on-disk layer: ``kiro_crew.artifacts_disk``.
    _ensure_content_token_locked = _artifacts_disk._ensure_content_token_locked
    _rotate_content_token_locked = _artifacts_disk._rotate_content_token_locked
    list_versions = _artifacts_disk.list_versions
    _artifact_dir = _artifacts_disk._artifact_dir
    _unique_slug = _artifacts_disk._unique_slug
    _claim_slug = _artifacts_disk._claim_slug
    _write_artifact = _artifacts_disk._write_artifact
    _write_image_artifact = _artifacts_disk._write_image_artifact
    _snapshot_version = _artifacts_disk._snapshot_version
    _write_meta = _artifacts_disk._write_meta
    _append_event = _artifacts_disk._append_event
    _load_meta = _artifacts_disk._load_meta
    _read_meta_file = _artifacts_disk._read_meta_file
    _read_text = _artifacts_disk._read_text
    _write_text = _artifacts_disk._write_text
    _read_bytes = _artifacts_disk._read_bytes
    _read_image_asset_bytes = _artifacts_disk._read_image_asset_bytes
    _refuse_write_outside_root = _artifacts_disk._refuse_write_outside_root
    _write_bytes = _artifacts_disk._write_bytes
    _prune_versions = _artifacts_disk._prune_versions
    _iter_meta_paths = _artifacts_disk._iter_meta_paths
    _rmtree = staticmethod(_artifacts_disk._rmtree)

    @staticmethod
    def _is_live_pointer(art: Artifact) -> bool:
        """True when reads come from ``source_path`` rather than the store's own copy."""
        return bool(art.source_path) and not art.source_copy_only

    def set_change_listener(self, listener: Callable[[str, str], None] | None) -> None:
        """Register a callback fired after a content-affecting mutation.

        The listener is invoked as ``listener(action, slug)`` where ``action``
        is ``"upsert"`` (create or content-changing update), ``"rename"``
        (metadata-only name change), or ``"delete"``.
        It runs *after* the store mutation completes and *outside* the store
        lock, so the listener may call back into the store (e.g. ``get``).
        Exceptions raised by the listener are logged and swallowed — a
        listener failure must never break an artifact write. Pass ``None`` to
        clear. The store stays dependency-free: it knows nothing about what
        the listener does.
        """
        self._change_listener = listener

    def _fire_change(self, action: str, slug: str) -> None:
        """Invoke the change-listener, isolating any failure from the writer."""
        listener = self._change_listener
        if listener is None:
            return
        try:
            listener(action, slug)
        except Exception:
            logger.exception("artifact change listener failed: action=%s slug=%s", action, slug)

    # Content writes: ``kiro_crew.artifacts_writes``.
    create = _artifacts_writes.create
    create_image = _artifacts_writes.create_image
    update = _artifacts_writes.update
    settle_blank = _artifacts_writes.settle_blank
    _write_settled_draft = _artifacts_writes._write_settled_draft

    def read_image_bytes(self, slug: str) -> tuple[bytes, str]:
        """Return ``(bytes, mime)`` for an image artifact's stored asset.

        Raises :class:`ArtifactNotFoundError` when the slug does not resolve,
        is not an image artifact, or its asset sidecar is missing. The read is
        routed through the gated :meth:`_read_bytes` so the sensitive-path
        denylist fires here as on every other store read.
        """
        slug = _validate_slug(slug)
        with self._lock:
            meta = self._load_meta(slug)
            if meta.kind != "image" or meta.image is None:
                raise ArtifactNotFoundError(f"artifact {slug!r} has no image asset")
            # Re-validate on READ, and derive the extension from the allowlist
            # rather than trusting the stored ``ext``. ``create_image`` already
            # checks the mime, but meta.json is a file: anything that can write
            # it (a prompt-injected agent, a hand edit, a restored backup) could
            # otherwise name ``text/html`` here and have the asset endpoint
            # serve same-origin HTML from an authenticated URL.
            norm_mime = (meta.image.mime or "").strip().lower()
            ext = _IMAGE_MIME_EXT.get(norm_mime, "")
            if not ext:
                raise ArtifactNotFoundError(
                    f"image asset for {slug!r} has an unsupported mime {meta.image.mime!r}"
                )
            asset = self._artifact_dir(slug) / f"asset.{ext}"
            if not asset.exists():
                raise ArtifactNotFoundError(f"image asset missing for {slug!r}")
            mime = norm_mime
        # Read OUTSIDE the lock: an asset can be tens of MiB, and holding the
        # store-wide lock across it would block every concurrent artifact
        # operation for the duration of the read. The path was resolved under
        # the lock and an image artifact's bytes are never rewritten in place.
        return self._read_image_asset_bytes(asset), mime

    def get(self, slug: str, *, version: int | None = None, assign_token: bool = False) -> Artifact:
        """Return an artifact (with content) by slug, optionally a specific version.

        Live-pointer behavior: for file-backed artifacts (those
        with a ``source_path``), the *current* read returns the live file
        content from disk, NOT the artifact storage's snapshot. This means
        edits made via the file viewer (or any other tool that writes the
        file) are reflected in the artifact view automatically. Versioned
        reads always come from the snapshot in ``versions/vN.html`` so
        history is preserved.

        If the source file is missing or unreadable, falls back to the
        last-known snapshot in ``current.html`` so the artifact stays
        viewable even after the source file moves or is deleted.

        A current store-backed read returns the stored ``content_token``. An
        artifact saved before content tokens existed has none, and only a read
        with ``assign_token`` gives it one, persisted to meta.json so the read
        can guard a later save. That is a write, so the caller must be off the
        event loop: the dashboard's detail read is the one that sets it.
        """
        slug = _validate_slug(slug)
        with self._lock:
            meta = self._load_meta(slug)
            # Lazy backfill: a record with no event log picks up a synthetic
            # created/edited history on first read. ``backfill_events`` is
            # idempotent — once events_backfilled is True, this is a no-op.
            # Persist the synthesized events so subsequent reads don't repeat
            # the work.
            if _records.backfill_events(meta):
                self._write_meta(meta)
            if version is not None:
                if version < 1 or version > meta.version:
                    raise ArtifactNotFoundError(
                        f"version {version} not found for {slug} " f"(have 1..{meta.version})"
                    )
                vfile = self._artifact_dir(slug) / "versions" / f"v{version}.html"
                if not vfile.exists():
                    raise ArtifactNotFoundError(
                        f"version {version} pruned for {slug} (oldest retained "
                        f"version may be higher; check list_versions)"
                    )
                meta.content = self._read_text(vfile)
                # Restore the render kind recorded for this version so a
                # historical widget snapshot renders as a widget, not the raw
                # inner HTML. Absent (legacy artifacts) → keep current kind.
                vk = meta.version_kinds.get(str(version))
                if vk:
                    meta.kind = vk
                meta.live_dirty = False  # historical view — not "live"
                meta.content_token = None
                return meta
            # Current view: prefer source_path for file-backed artifacts.
            # A copy never reads from disk: it records source_path purely as
            # provenance/dedup identity, so reading it would resurrect exactly
            # the dead-pointer bug source_missing exists to surface.
            if meta.source_path and not meta.source_copy_only:
                live = self._try_read_source_path(meta.source_path, meta.source_root)
                if live is not None:
                    meta.content = live
                else:
                    # Fall through to the snapshot fallback — file moved /
                    # deleted / unreadable / outside the authorized root. Flag
                    # it: the fallback keeps the artifact viewable, which on its
                    # own makes a dead pointer look completely healthy.
                    meta.source_missing = True
                    meta.content = self._read_text(self._artifact_dir(slug) / "current.html")
                meta.content_token = None
            else:
                meta.content = self._read_text(self._artifact_dir(slug) / "current.html")
                if assign_token:
                    try:
                        self._ensure_content_token_locked(meta)
                    except OSError:
                        # An untokened artifact on an unwritable data directory must
                        # still open. Serve it without a token (last-write-wins): the
                        # token assigned in memory was never persisted, so returning
                        # it would 409 every later guarded save from this client.
                        logger.warning(
                            "artifact %s: could not persist a content token; serving it unguarded",
                            slug,
                            exc_info=True,
                        )
                        meta.content_token = None
            # Compute live_dirty by comparing the live content to the
            # latest numbered snapshot. Catches both silent saves AND
            # external file edits to source_path that we never saw —
            # which is the whole point of the "snapshot anytime" behavior.
            # If versions/vN.html is missing (legacy artifact
            # before snapshots existed), default to not-dirty.
            latest_vfile = self._artifact_dir(slug) / "versions" / f"v{meta.version}.html"
            if latest_vfile.exists():
                latest_snapshot = self._read_text(latest_vfile)
                meta.live_dirty = (meta.content or "") != latest_snapshot
            else:
                meta.live_dirty = False
            if meta.source_missing:
                # A dead pointer must never report as in-sync. The comparison
                # above ran against the SNAPSHOT (the fallback), so it always
                # says "clean" — the one case where equality proves nothing,
                # because we never saw the live state at all.
                meta.live_dirty = True
            return meta

    # The live ``source_path`` pointer: ``kiro_crew.artifacts_source_link``.
    allowed_source_roots = _artifacts_source_link.allowed_source_roots
    _try_read_source_path = _artifacts_source_link._try_read_source_path
    _try_write_source_path = _artifacts_source_link._try_write_source_path
    relocate = _artifacts_source_link.relocate

    def set_pinned(self, slug: str, pinned: bool) -> Artifact:
        """Set an artifact's pin/favorite mark — metadata-only.

        Like :meth:`set_folder`, toggling ``pinned`` is a pure metadata
        mutation: it does NOT bump the version, write a snapshot, or emit a
        lifecycle event.
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            art.pinned = bool(pinned)
            self._write_meta(art)
            logger.info("artifact pin set: slug=%s pinned=%s", slug, art.pinned)
            return art

    def mark_webapp_expired(self, slug: str) -> Artifact:
        """Tombstone a kind="webapp" artifact: set lifecycle.status to "expired".

        Used by the human-triggered teardown path. FU-6: also clears
        ``lifecycle.expires_at`` and ``deploy_target.public_url`` so the card
        cannot render a live-looking countdown or a dead public link next to
        the Expired badge. Deploy history stays in the artifact's event feed.
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            if art.kind != "webapp" or art.webapp_metadata is None:
                raise ArtifactValidationError(
                    f"artifact {slug!r} is not a webapp artifact; teardown does not apply"
                )
            art.webapp_metadata.lifecycle.status = "expired"
            art.webapp_metadata.lifecycle.expires_at = None
            if art.webapp_metadata.deploy_target is not None:
                art.webapp_metadata.deploy_target.public_url = ""
            art.updated_at = _now_iso()
            self._write_meta(art)
        self._fire_change("upsert", slug)
        return art

    def unmark_webapp_expired(self, slug: str) -> Artifact:
        """Reverse a tombstone: restore lifecycle.status from "expired" to "live".

        Used when teardown manifest-expiry fails for persistent deployments and
        we need to keep the card's Tear down button available for retry.
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            if art.kind != "webapp" or art.webapp_metadata is None:
                raise ArtifactValidationError(f"artifact {slug!r} is not a webapp artifact")
            art.webapp_metadata.lifecycle.status = "live"
            art.updated_at = _now_iso()
            self._write_meta(art)
        self._fire_change("upsert", slug)
        return art

    # Lookups and sweeps over every ``meta.json``: ``kiro_crew.artifacts_scans``.
    _is_sweepable_auto_widget = _artifacts_scans._is_sweepable_auto_widget
    prune_auto_widgets = _artifacts_scans.prune_auto_widgets
    migrate_kinds = _artifacts_scans.migrate_kinds
    find_by_artifact_id = _artifacts_scans.find_by_artifact_id
    artifact_index_key = staticmethod(_artifacts_scans.artifact_index_key)
    index_by_artifact_id = _artifacts_scans.index_by_artifact_id
    find_by_source_path = _artifacts_scans.find_by_source_path

    def delete(
        self,
        slug: str,
        *,
        refuse_if_published: bool = False,
        expect_created_at: "str | _ExpectAbsent | None" = None,
    ) -> None:
        """Permanently delete an artifact and all of its versions.

        ``refuse_if_published`` raises :class:`ArtifactStillPublishedError` instead of
        deleting when the artifact holds a publication record. It defaults to False to
        keep callers that never publish unchanged, but BOTH delete paths that can reach a
        published artifact now pass it.

        The flag only means anything to a caller that has already cleared the record for
        the copy it withdrew. Once that is done, a record found here can only be a
        publication that landed AFTER the withdrawal, so refusing protects a live copy
        instead of rejecting an ordinary delete. Both callers are built that way: the
        folder cascade clears per artifact in its withdrawal pass, and the single-artifact
        handler clears immediately after its withdrawal is confirmed. A caller that
        withdrew but did NOT clear would be refused on every published artifact, which is
        why the flag is off by default rather than always on.

        ``expect_created_at`` names the artifact GENERATION the caller decided to destroy,
        and raises :class:`ArtifactReplacedError` when the slug now holds a different one.
        A caller that decided over a slug alone is not naming an artifact: a freed slug is
        re-minted identically, so between a caller's decision and this call the artifact it
        meant can be gone and a same-titled newcomer can hold the name. Destroying that
        newcomer is unrecoverable and, reported as an ordinary deletion, is
        indistinguishable from the intended victim. Any caller whose decision is older
        than this call -- one that awaited anything, a bulk pass working from a snapshot --
        passes it; a caller acting on a record it just read does not need to.

        Pass :data:`EXPECT_ABSENT` for the case a generation cannot express: the caller read
        this slug as holding NOTHING. Reaching the check below then means an artifact
        appeared after that read, so it is one nobody asked to delete and this refuses
        instead. ``None`` remains "no generation to compare", which performs no check, and
        the two are deliberately separate values rather than one overloaded ``None``.

        Both checks run inside the same lock as the removal, so unlike a pre-pass neither
        can be overtaken by a publish or a recreation landing after the decision and
        before the delete -- which is the whole reason they are here rather than at the
        caller.
        """
        slug = _validate_slug(slug)
        with self._lock:
            adir = self._artifact_dir(slug)
            if not adir.exists():
                raise ArtifactNotFoundError(f"artifact not found: {slug}")
            if isinstance(expect_created_at, _ExpectAbsent):
                raise ArtifactReplacedError(
                    f"artifact {slug} exists, and the caller read this slug as empty: it "
                    "was created after that read, so deleting it would destroy an "
                    "artifact nobody asked to delete"
                )
            if refuse_if_published or expect_created_at is not None:
                # Deliberately re-read under the lock rather than trusting anything the
                # caller passed in. `_load_meta` does not take this lock (meta reads are
                # unlocked by design), so this cannot deadlock.
                meta = self._load_meta(slug)
                if expect_created_at is not None and meta.created_at != expect_created_at:
                    raise ArtifactReplacedError(
                        f"artifact {slug} was created at {meta.created_at!r}, not "
                        f"{expect_created_at!r}: the artifact under this slug was "
                        "replaced, so deleting it would destroy one nobody asked to "
                        "delete"
                    )
                if refuse_if_published and meta.publication is not None:
                    raise ArtifactStillPublishedError(
                        f"artifact {slug} is still published; withdraw the published "
                        "copy before deleting it, or its record -- the only handle able "
                        "to take that copy down -- is lost with it"
                    )
            self._rmtree(adir)
            logger.info("artifact deleted: slug=%s", slug)
        self._fire_change("delete", slug)

    def record_impression(
        self,
        slug: str,
        *,
        by: str = "user",
        session_id: str | None = None,
        message_ts: str | None = None,
        widget_index: int | None = None,
    ) -> tuple[Artifact, bool]:
        """Append a ``referenced`` event to ``slug``'s activity log without
        modifying its content or version. Used by ``WidgetFrame`` on mount
        to record that a chat impression of this artifact has been
        rendered. The activity timeline groups
        these events to show the artifact's cross-session reach.

        ``message_ts`` and ``widget_index`` go into the event's
        ``metadata`` field as a breadcrumb to the first impression of the
        artifact in this session (clicking it could deep-link to the
        message).

        Idempotent per session: a ``referenced`` event is recorded at most
        once per ``session_id`` per artifact, and is suppressed entirely
        when the session already has any lifecycle event on the artifact
        (e.g. a CUD from an ``artifact_update``). The widget may be emitted
        in several messages of one session and reloads re-fire the POST,
        but the timeline only needs a single "referenced in session X"
        breadcrumb. The frontend also debounces via sessionStorage, but
        that is per-tab and cleared on reload, so the store enforces the
        invariant authoritatively. Distinct sessions still record
        separately.

        Returns ``(meta, appended)`` where ``appended`` is ``False`` when
        the event was suppressed (meta returned unchanged, no write) and
        ``True`` when a ``referenced`` event was actually appended. The
        flag lets the handler avoid returning a stale ``art.events[-1]``
        (a prior CUD event) as if it were the just-recorded impression.
        """
        slug = _validate_slug(slug)
        with self._lock:
            meta = self._load_meta(slug)
            # A ``referenced`` event is a per-session breadcrumb: record at
            # most one per session per artifact, and none when the session
            # already has any lifecycle event on it (a ``created`` /
            # ``iterated`` / ``edited`` / ``reverted`` CUD already
            # represents the session in the timeline). The widget can
            # appear in several messages of one session and reloads re-fire
            # the POST — the frontend's sessionStorage debounce is per-tab
            # and cleared on reload — so the store is the source of truth
            # for the one-breadcrumb-per-session invariant.
            if session_id and any(e.get("session_id") == session_id for e in meta.events):
                return meta, False
            metadata: dict[str, Any] = {}
            if message_ts:
                metadata["message_ts"] = message_ts
            if widget_index is not None:
                metadata["widget_index"] = widget_index
            self._append_event(
                meta,
                type="referenced",
                by=by,
                session_id=session_id,
                version=meta.version,
                metadata=metadata or None,
            )
            self._write_meta(meta)
            return meta, True

    def list(
        self,
        *,
        tag: str | None = None,
        kind: str | None = None,
        name_contains: str | None = None,
        source: str | None = None,
        source_path: str | None = None,
        folder: str | None = None,
        session_key: str | None = None,
        touched_by_session: str | None = None,
        touched_by_any: _Iterable[str] | None = None,
        pinned: bool | None = None,
    ) -> _List[Artifact]:
        """List all artifacts matching the given filters (sorted newest first).

        The lock is held only long enough to snapshot the artifact-directory
        listing; meta.json reads happen outside the lock so concurrent
        ``create()`` / ``update()`` / ``delete()`` calls don't block behind
        an O(N) filesystem scan. Atomic meta.json writes (tmp + rename) make
        unlocked reads safe — the worst case is a stale-but-valid snapshot
        for an artifact that was just renamed.

        ``tag`` is read through the tag rule (``normalize_tag``), so a label
        matches in whichever Unicode spelling it was typed, and a value that is
        not a well-formed tag matches nothing.

        ``session_key`` scopes to one originating chat session (the in-session
        artifact panel's query). Like ``folder``, it distinguishes absent from
        empty: ``None`` doesn't scope, while ``""`` matches only artifacts with
        no originating session. ``pinned`` filters on the star flag.

        ``touched_by_session`` is the broader *involvement* scope the in-session
        Artifacts tab needs: it matches an artifact when the session either
        ORIGINATED it (``session_key``) or appears in any lifecycle event's
        ``session_id`` — i.e. the session created, read (``referenced``),
        edited, iterated on, or reverted it. Unlike ``session_key`` this is a
        pure match filter with no empty-bucket semantics: ``None`` and ``""``
        both mean "don't scope", because "artifacts no session ever touched"
        is not a view any caller wants and would otherwise be one typo away.

        ``touched_by_any`` is the same involvement scope over a SET of sessions
        (every chat filed in one sidebar folder): an artifact matches when any
        of them touched it. Unlike ``touched_by_session`` it distinguishes
        absent from empty: ``None`` doesn't scope, while an empty collection
        matches nothing, because a scope that resolved to zero sessions is an
        empty view and must never widen to the whole library.
        """
        touched_any = None if touched_by_any is None else _fold_session_keys(touched_by_any)
        with self._lock:
            meta_paths = list(self._iter_meta_paths())
        if tag:
            # Tags are stored in their NFC spelling, so the filter is read the same
            # way; a value that is not a tag at all can match no stored tag.
            try:
                tag = normalize_tag(tag)
            except ValueError:
                return []
        results: _List[Artifact] = []
        for meta_path in meta_paths:
            try:
                art = self._read_meta_file(meta_path)
            except (
                ArtifactError,
                OSError,
                ValueError,
                TypeError,
            ) as exc:
                # ValueError catches int("abc") on bad version field;
                # TypeError catches list(non_iterable) on bad tags field.
                # A single corrupted meta.json must skip+warn, not crash list().
                # FileNotFoundError (subclass of OSError) is also tolerated:
                # an artifact deleted between the listing snapshot and the
                # read just disappears from the result, which is the
                # correct semantic for a best-effort listing.
                logger.warning("skipping unreadable artifact at %s: %s", meta_path, exc)
                continue
            if tag and tag not in art.tags:
                continue
            if kind and art.kind != kind:
                continue
            if source and art.source != source:
                continue
            if name_contains and name_contains.lower() not in art.name.lower():
                continue
            if source_path is not None and art.source_path != source_path:
                continue
            # ``folder is None`` means "don't scope" (all folders). An explicit
            # value — including ``""`` — scopes to that folder id, where ``""``
            # is the unfiled/root bucket.
            if folder is not None and art.folder_id != folder:
                continue
            # Same present-vs-empty distinction as ``folder``: ``None`` = don't
            # scope; ``""`` = only artifacts with NO originating session.
            if session_key is not None and art.session_key != session_key:
                continue
            # Involvement scope: origin OR any event this session left behind.
            # Empty/None is a no-op (see docstring) rather than an empty bucket.
            if touched_by_session and not _session_touched(art, touched_by_session):
                continue
            if touched_any is not None and not _session_touched_any(art, touched_any):
                continue
            if pinned is not None and bool(art.pinned) is not pinned:
                continue
            results.append(art)
        # ``updated_at`` alone is not a total order: it is microsecond ISO, so two
        # artifacts written inside one microsecond carry the identical stamp, and a
        # stable sort then leaves the tie to directory scan order -- "newest first"
        # becomes whatever the filesystem enumerated first, which differs per
        # platform. Windows CI failed ``test_artifacts_handlers`` on exactly that.
        # ``slug`` makes the order total, and every caller (the library UI, the MCP
        # list tool, the pruning sweep) gets the same answer on every host.
        results.sort(key=lambda a: (a.updated_at, a.slug), reverse=True)
        return results

    # ── publication (remote store) — data only, no networking ──────────────

    def set_publication(self, slug: str, pub: "ArtifactPublication") -> Artifact:
        """Attach (or replace) an artifact's publication block.

        Data-only: callers in the publish-sync layer perform the actual upload
        and then persist the resulting state here. Returns the updated
        Artifact (without content loaded).
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            art.publication = pub
            self._write_meta(art)
            logger.info(
                "artifact publication set: slug=%s artifact_id=%s visibility=%s",
                slug,
                pub.artifact_id,
                pub.visibility,
            )
            return art

    def set_fork_metadata(self, slug: str, fm: "ForkMetadata") -> Artifact:
        """Attach fork provenance to an artifact."""
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            art.fork_metadata = fm
            self._write_meta(art)
            return art

    def update_fork_metadata(self, slug: str, **fields: Any) -> Artifact:
        """Patch fields on an artifact's fork_metadata block."""
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            if art.fork_metadata is None:
                raise ArtifactError(f"artifact {slug!r} has no fork_metadata")
            _records.patch_fork_metadata(art.fork_metadata, fields)
            self._write_meta(art)
            return art

    def set_folder(self, slug: str, folder_id: str) -> Artifact:
        """Move an artifact into a folder — metadata-only.

        Setting ``folder_id`` (``""`` = unfiled/root) is a pure metadata
        mutation: it does NOT bump the version, write a snapshot, or emit a
        lifecycle event — the same class as retag/rename. The caller is
        responsible for having validated that ``folder_id`` refers to a real
        folder (or is empty); a dangling id is tolerated at read time
        (treated as unfiled) but should not be written deliberately.
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            art.folder_id = folder_id or ""
            self._write_meta(art)
            logger.info(
                "artifact folder set: slug=%s folder_id=%s", slug, art.folder_id or "(root)"
            )
            return art

    def clear_publication(
        self,
        slug: str,
        *,
        expect_created_at: str | None = None,
        expect_publication_id: str | None = None,
    ) -> Artifact:
        """Remove an artifact's publication block (after unpublish/delete).

        ``expect_created_at`` names the artifact GENERATION whose copy the caller
        withdrew, and raises :class:`ArtifactReplacedError` instead of clearing when the
        slug now holds a different one. A withdrawal is a network round trip, so a caller
        clearing afterwards is acting on a slug it read before that wait: if the artifact
        it withdrew is gone and a same-titled newcomer holds the name, clearing here
        erases the NEWCOMER's record -- the only handle able to withdraw a copy that is
        still served.

        ``expect_publication_id`` names the PUBLICATION whose copy came down, and is the
        check that actually decides it. The generation alone cannot: :meth:`set_publication`
        replaces the publication block and leaves ``created_at`` untouched, so the same
        artifact re-published during that same round trip carries an unchanged stamp and a
        brand-new live copy. Matching the record's own ``artifact_id`` is what tells the
        record the caller withdrew from a record it has never seen.

        Every caller that clears after awaiting anything passes BOTH, and both are compared
        here rather than at the caller because only this lock also performs the write.
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            if expect_created_at is not None and art.created_at != expect_created_at:
                raise ArtifactReplacedError(
                    f"artifact {slug} was created at {art.created_at!r}, not "
                    f"{expect_created_at!r}: the artifact under this slug was replaced, "
                    "so clearing its publication would discard the only handle able to "
                    "withdraw a copy nobody asked to unpublish"
                )
            if expect_publication_id is not None:
                current = art.publication.artifact_id if art.publication else None
                if current != expect_publication_id:
                    raise ArtifactReplacedError(
                        f"artifact {slug} is published as {current!r}, not "
                        f"{expect_publication_id!r}: the record under this slug names a "
                        "different copy, so clearing it would discard the only handle "
                        "able to withdraw a copy nobody asked to unpublish"
                    )
            art.publication = None
            self._write_meta(art)
            logger.info("artifact publication cleared: slug=%s", slug)
            return art

    # ── comments (durable sidecar store) ──────────────────────────────────

    # The comment sidecar: ``kiro_crew.artifacts_comments``.
    list_comments = _artifacts_comments.list_comments
    add_comment = _artifacts_comments.add_comment
    update_comment = _artifacts_comments.update_comment
    delete_comment = _artifacts_comments.delete_comment
    merge_remote_comments = _artifacts_comments.merge_remote_comments
    _rescan_comment_anchors_locked = _artifacts_comments._rescan_comment_anchors_locked
    record_comment_event = _artifacts_comments.record_comment_event
    _load_comments = _artifacts_comments._load_comments
    _write_comments = _artifacts_comments._write_comments

    #: Fields a caller may patch via :meth:`update_comment`. A narrow allowlist
    #: (mirroring ``update_publication`` / ``update_fork_metadata``) so a future
    #: MCP tool forwarding user/LLM-controlled keys cannot mass-assign identity/
    #: provenance fields (``is_agent`` / ``author`` / ``origin`` / ``sync_state`` /
    #: ``target_*``). Only the mutable lifecycle + body fields are patchable;
    #: ``updated_at`` is always refreshed by the method itself.
    _MUTABLE_COMMENT_FIELDS = frozenset({"status", "body", "anchor_orphaned"})

    def update_publication(self, slug: str, **fields: Any) -> Artifact:
        """Patch fields on an artifact's existing publication block.

        Raises :class:`ArtifactValidationError` if the artifact has no
        publication (callers must ``set_publication`` first). Unknown field
        names are rejected so a typo can't silently no-op a sync update.
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            if art.publication is None:
                raise ArtifactValidationError(
                    f"artifact {slug} is not published; cannot update publication"
                )
            _records.patch_publication(art.publication, fields)
            self._write_meta(art)
            return art

    # ── filesystem helpers ────────────────────────────────────────────────

    # Kept on the class for callers that parse a single meta.json block directly.
    _parse_publication = staticmethod(_records.parse_publication)
    _parse_image_metadata = staticmethod(_records.parse_image_metadata)


# ── Module-level singleton ──────────────────────────────────────────────────

_default_store: ArtifactStore | None = None
_default_store_lock = threading.Lock()


def get_default_store() -> ArtifactStore:
    """Return the process-wide default artifact store (lazy-initialized)."""
    global _default_store
    with _default_store_lock:
        if _default_store is None:
            _default_store = ArtifactStore()
        return _default_store


_default_folder_store: "ArtifactFolderStore | None" = None
_default_folder_store_lock = threading.Lock()


def get_default_folder_store() -> "ArtifactFolderStore":
    """Return the process-wide default artifact-folder store (lazy-initialized)."""
    global _default_folder_store
    with _default_folder_store_lock:
        if _default_folder_store is None:
            # The path comes from this module's ``config_dir``, the binding the
            # default store's root reads, so one patch of it relocates both.
            _default_folder_store = ArtifactFolderStore(
                path=config_dir() / ArtifactFolderStore._FILE
            )
        return _default_folder_store


# The ``artifacts_*`` owner modules hold code that was this module's own: the
# ``ArtifactStore`` members the class binds, and the module-level helpers re-imported
# above, which this module keeps binding. ``_facade_mirror`` keeps the whole patch
# surface: a name in ``_EXPORTS`` is forwarded to its one owner exactly as before (read,
# write and delete there; ``dir()`` lists it), and a write of any other name this module
# binds reaches every owner binding the same object, so one patch through this module
# steers every reader of the name. ``surface`` is this module's own namespace, so an
# owner's own functions are never part of the facade. The forwarding table is the one
# the install saw: a name added to ``_EXPORTS`` later is answered on a read by
# ``__getattr__`` above, and a write or delete of it acts on this module and the owners
# (the mirror's creation rule), never on that name's own module. A
# ``mock.patch(..., create=True)`` on a forwarded name still cannot be undone:
# ``test_artifacts_refactor_create_guard`` refuses one. ``test/test_artifacts_bindings.py``
# pins the reach.
#: The store's owner modules, lowest layer first.
_OWNERS: tuple[str, ...] = (
    "kiro_crew.artifacts_disk",
    "kiro_crew.artifacts_source_link",
    "kiro_crew.artifacts_comments",
    "kiro_crew.artifacts_scans",
    "kiro_crew.artifacts_writes",
)
_MIRROR = _facade_mirror.install(
    _sys.modules[__name__],
    _OWNERS,
    excluded=(),
    forwards=_FORWARDS,
    surface=frozenset(globals()),
)
_EXPORTS.update(_MIRROR.served)

# Installed last, so forwarding is live for every caller but never runs while this
# module is still binding its own names.
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule

#: Imported only to run the forwarding, so a star import leaves them out.
_MACHINERY = frozenset({"ModuleType", "TYPE_CHECKING", "importlib"})

# A star import reads this list and never reaches ``__getattr__``. Derived from what
# this module binds and the forwarding table, so it is not a third list to keep in step.
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name not in _MACHINERY
)


if TYPE_CHECKING:  # every forwarded name, for type checkers and IDEs
    from kiro_crew.artifact_store.folders import (  # noqa: F401
        _NO_GENERATIONS,
        FOLDER_PATH_SEP,
        MAX_FOLDER_DEPTH,
    )
    from kiro_crew.artifact_store.images import (  # noqa: F401
        _sniff_jpeg_dimensions,
        _sniff_webp_dimensions,
    )
    from kiro_crew.artifact_store.records import ALLOWED_EVENT_TYPES  # noqa: F401
    from kiro_crew.artifact_store.rules import (  # noqa: F401
        _EXT_KIND_MAP,
        _HARDCODED_COLOR_RE,
        _HREF_ATTR_RE,
        _HTML_SNIFF_MARKERS,
        _MD_HEADING_RE,
        _SLUG_NORMALIZE_RE,
        _SLUG_RE,
        _SVG_ROOT_RE,
        ALLOWED_KINDS,
        ALLOWED_SOURCES,
        DOC_EXTENSIONS,
        MAX_DESCRIPTION_LEN,
        MAX_NAME_LEN,
        MAX_SOURCE_PATH_LEN,
        MAX_TAG_LEN,
        MAX_TAGS,
        _strip_session_scope,
    )
