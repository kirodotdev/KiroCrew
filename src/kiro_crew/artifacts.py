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
tree). This module keeps the store itself -- the shared per-root lock, the
directory layout, the fenced file IO, versions and the live ``source_path``
pointer -- together with the retention caps, the clock and the default
singletons, and keeps its whole import surface by re-exporting each moved name
with one identity, so callers import from here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from dataclasses import fields as fields_of
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Iterator
from typing import List as _List
from typing import Mapping

from kiro_crew import hooks, pinned_fs, platform_compat
from kiro_crew.artifact_source import is_verifiable_root
from kiro_crew.artifact_store import comments as _threads
from kiro_crew.artifact_store import records as _records
from kiro_crew.artifact_store import rules as _rules
from kiro_crew.artifact_store.comments import filter_comments_for_forward
from kiro_crew.artifact_store.folders import (  # noqa: F401 — re-export for API compatibility
    _NO_GENERATIONS,
    FOLDER_PATH_SEP,
    MAX_FOLDER_DEPTH,
    ArtifactFolderStore,
)
from kiro_crew.artifact_store.images import (  # noqa: F401 — re-export for API compatibility
    _IMAGE_MIME_EXT,
    _sniff_image_dimensions,
    _sniff_jpeg_dimensions,
    _sniff_webp_dimensions,
)
from kiro_crew.artifact_store.model import (
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
from kiro_crew.artifact_store.records import ALLOWED_EVENT_TYPES
from kiro_crew.artifact_store.rules import (  # noqa: F401 — re-export for API compatibility
    _EXT_KIND_MAP,
    _HARDCODED_COLOR_RE,
    _HREF_ATTR_RE,
    _HTML_SNIFF_MARKERS,
    _MD_HEADING_RE,
    _SLUG_NORMALIZE_RE,
    _SLUG_RE,
    _SVG_ROOT_RE,
    _TAG_RE,
    ALLOWED_KINDS,
    ALLOWED_SOURCES,
    DOC_EXTENSIONS,
    MAX_DESCRIPTION_LEN,
    MAX_NAME_LEN,
    MAX_SOURCE_PATH_LEN,
    MAX_TAGS,
    USER_SELECTABLE_KINDS,
    _infer_kind,
    _markdown_misclassification_reason,
    _session_touched,
    _strip_session_scope,
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
    slugify,
)
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.constants import ARTIFACT_MAX_CONTENT_BYTES
from kiro_crew.deploy.webapp_types import (
    WebAppArchitecture,
    WebAppCost,
    WebAppDeployTarget,
    WebAppLifecycle,
    WebAppMetadata,
    WebAppTeardown,
    webapp_metadata_from_dict,
)
from kiro_crew.metrics.events import ARTIFACTS_CREATED, emit_counter
from kiro_crew.publish_provider import DEFAULT_PROVIDER
from kiro_crew.security import (
    canonical_path_refusal,
    is_sensitive_canonical_path,
    is_sensitive_path,
    is_unverifiable_path_refusal,
    sensitive_path_refusal,
)
from kiro_crew.slugs import slug_hash_fallback

# The facade's whole star-import surface, the names its owner modules define included.
__all__ = [
    "ALLOWED_EVENT_TYPES",
    "ALLOWED_KINDS",
    "ALLOWED_SOURCES",
    "ARTIFACTS_CREATED",
    "ARTIFACT_MAX_CONTENT_BYTES",
    "Any",
    "Artifact",
    "ArtifactAlreadyExistsError",
    "ArtifactComment",
    "ArtifactConflictError",
    "ArtifactError",
    "ArtifactFolderStore",
    "ArtifactNotFoundError",
    "ArtifactPublication",
    "ArtifactReplacedError",
    "ArtifactStillPublishedError",
    "ArtifactStore",
    "ArtifactValidationError",
    "Callable",
    "DEFAULT_PROVIDER",
    "DOC_EXTENSIONS",
    "EXPECT_ABSENT",
    "FOLDER_PATH_SEP",
    "ForkMetadata",
    "ImageMetadata",
    "Iterator",
    "KiroCrewConfig",
    "MAX_AUTO_WIDGET_ARTIFACTS",
    "MAX_COMMENTS_PER_ARTIFACT",
    "MAX_CONTENT_BYTES",
    "MAX_DESCRIPTION_LEN",
    "MAX_EVENTS_PER_ARTIFACT",
    "MAX_FOLDER_DEPTH",
    "MAX_NAME_LEN",
    "MAX_SOURCE_PATH_LEN",
    "MAX_TAGS",
    "MAX_VERSIONS",
    "Mapping",
    "MappingProxyType",
    "Path",
    "USER_SELECTABLE_KINDS",
    "WebAppArchitecture",
    "WebAppCost",
    "WebAppDeployTarget",
    "WebAppLifecycle",
    "WebAppMetadata",
    "WebAppTeardown",
    "annotations",
    "asdict",
    "canonical_path_refusal",
    "config_dir",
    "dataclass",
    "datetime",
    "detect_editor_kind",
    "emit_counter",
    "field",
    "fields_of",
    "filter_comments_for_forward",
    "get_default_folder_store",
    "get_default_store",
    "has_unthemed_hardcoded_colors",
    "hashlib",
    "hooks",
    "is_document_path",
    "is_sensitive_canonical_path",
    "is_sensitive_path",
    "is_unverifiable_path_refusal",
    "is_verifiable_root",
    "json",
    "logger",
    "logging",
    "os",
    "pinned_fs",
    "re",
    "sensitive_path_refusal",
    "slug_hash_fallback",
    "slug_is_well_formed",
    "slugify",
    "tempfile",
    "threading",
    "timezone",
    "unicodedata",
    "uuid",
    "webapp_metadata_from_dict",
]

logger = logging.getLogger(__name__)

# The classes keep naming this module as their home, so tracebacks, qualified type
# names in logs and pickled references read the same whichever owner defines them.
for _moved in (
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactAlreadyExistsError,
    ArtifactValidationError,
    ArtifactStillPublishedError,
    ArtifactReplacedError,
    ArtifactConflictError,
    _ExpectAbsent,
    ForkMetadata,
    ArtifactPublication,
    ArtifactComment,
    ImageMetadata,
    Artifact,
    ArtifactFolderStore,
):
    _moved.__module__ = __name__
del _moved


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


#: Shape of a wire ``content_token`` / ``expected_token``: hex HMAC-SHA256.
_TOKEN_RE = re.compile(r"[0-9a-f]{64}")


def _new_content_salt() -> str:
    """A fresh :attr:`Artifact.content_salt`: 128 random bits as lowercase hex."""
    return os.urandom(16).hex()


def _validate_content(content: str) -> str:
    if not isinstance(content, str):
        raise ArtifactValidationError(f"content must be str, got {type(content).__name__}")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_CONTENT_BYTES:
        raise ArtifactValidationError(f"content exceeds {MAX_CONTENT_BYTES} bytes ({len(encoded)})")
    return content


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
        self._root.mkdir(parents=True, exist_ok=True)

    # ── public API ────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def _content_token(art: Artifact, content: str) -> str:
        """The opaque token for *content* as held by *art* (see ``Artifact.content_token``)."""
        assert art.content_salt, "content_salt is assigned before any token is minted"
        return hmac.new(
            bytes.fromhex(art.content_salt), content.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def _ensure_content_salt_locked(self, art: Artifact) -> None:
        """Give an artifact saved before content tokens existed (or whose salt was
        reset as malformed) a salt, persisted so later reads mint the same token.
        Called under the store lock by every path that can mint a token."""
        if not art.content_salt:
            art.content_salt = _new_content_salt()
            self._write_meta(art)

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

    def create(
        self,
        *,
        name: str,
        content: str,
        slug: str | None = None,
        kind: str | None = None,
        source: str = "chat",
        description: str = "",
        tags: list[str] | None = None,
        source_path: str = "",
        source_root: str = "",
        source_copy_only: bool = False,
        folder_id: str = "",
        session_key: str = "",
        webapp_metadata: "WebAppMetadata | None" = None,
        auto_registered: bool = False,
    ) -> Artifact:
        """Persist a new artifact and return it.

        If ``slug`` is omitted, one is derived from ``name`` and disambiguated
        (``foo``, ``foo-2``, ``foo-3`` ...) so concurrent saves of artifacts
        with the same name don't collide.

        ``source_path`` is the original filesystem path for file-backed
        artifacts (e.g. when 'Save as artifact' is used from the file
        viewer). It's stored as metadata only — the artifact's authoritative
        content lives in ``current.html`` from then on; we never write back
        to ``source_path``.

        ``source_root`` is the directory that AUTHORIZES reads of
        ``source_path`` — the project (or git repo) root a promoted file was
        linked from, as decided by
        :func:`kiro_crew.artifact_source.classify_source`. Required for a link
        outside ``$HOME`` and the data home; ignored (forced empty) when
        ``source_path`` is empty, since a root with nothing to authorize is
        meaningless metadata.

        ``auto_registered=True`` marks the record as machine-created from a
        chat-emitted widget, making it sweepable by
        :meth:`prune_auto_widgets` while it remains unpinned. Only
        :mod:`kiro_crew.widget_artifacts` should set it.
        """
        name = _validate_name(name)
        content = _validate_content(content)
        source_path = _validate_source_path(source_path)
        source_root = _validate_source_path(source_root, "source_root") if source_path else ""
        # Infer the kind when the caller didn't pin one (explicit kind always
        # wins). Validated content is needed for the inline content sniff, so
        # this runs after _validate_content.
        #
        # A document created BLANK is the one case inference cannot help with —
        # there is nothing to sniff — and it is exactly what the library's "New
        # artifact" action creates. Default it to markdown (the editable prose
        # default) and record that the kind was auto-assigned, so the first real
        # content write can settle it into json / svg (see
        # :func:`detect_editor_kind`). A caller that pinned a kind, or supplied
        # real content or a source_path, has a genuine signal and is left alone.
        kind_auto = kind is None and not source_path and not content.strip()
        if kind_auto:
            kind = "markdown"
        kind = _validate_kind(_infer_kind(content, source_path, kind))
        source = _validate_source(source)
        description = _validate_description(description)
        tags_list = _validate_tags(tags)

        with self._lock:
            slug, collided_with = self._claim_slug(slug, name)

            now = _now_iso()
            art = Artifact(
                slug=slug,
                name=name,
                kind=kind,
                kind_auto=kind_auto,
                source=source,
                description=description,
                tags=tags_list,
                version=1,
                created_at=now,
                updated_at=now,
                content=content,
                source_path=source_path,
                source_root=source_root,
                source_copy_only=source_copy_only,
                folder_id=folder_id or "",
                session_key=session_key[:256] if session_key else "",
                auto_registered=bool(auto_registered),
                version_kinds={"1": kind},
                webapp_metadata=webapp_metadata,
                slug_collided_with=collided_with,
            )
            # Lifecycle: emit `created` event. New artifacts are tagged
            # `events_backfilled=True` because their history starts here —
            # there is nothing pre-existing to synthesize.
            self._append_event(
                art,
                type="created",
                by=source if source != "chat" else "agent",
                version=1,
            )
            art.events_backfilled = True
            self._write_artifact(art, content)
            logger.info("artifact created: slug=%s name=%s kind=%s", slug, name, kind)
        self._fire_change("upsert", slug)
        # After the write, so a failed create contributes nothing. ``kind`` and
        # ``source`` are the values ``_validate_kind`` / ``_validate_source``
        # already restrict to closed sets, and ``kind_auto`` says whether the
        # kind was inferred rather than pinned by the caller.
        emit_counter(
            ARTIFACTS_CREATED,
            {"kind": kind, "source": source, "kind_auto": bool(kind_auto)},
        )
        return art

    def create_image(
        self,
        *,
        name: str,
        image_bytes: bytes,
        mime: str,
        slug: str | None = None,
        source: str = "chat",
        session_key: str = "",
        auto_registered: bool = False,
        alt: str = "",
        original_filename: str = "",
        description: str = "",
        tags: list[str] | None = None,
        folder_id: str = "",
    ) -> Artifact:
        """Persist a raster image as a first-class ``kind="image"`` artifact.

        The bytes are stored in an ``asset.<ext>`` sidecar next to ``meta.json``;
        ``current.html`` stays empty (image artifacts carry no text body). This
        keeps the text store untouched — the same three-file directory shape,
        the same slug/version/event machinery — with the bytes riding alongside
        as an extra file that :meth:`delete`'s whole-directory ``_rmtree`` cleans
        up for free.

        ``mime`` must be one of the raster allowlist (:data:`_IMAGE_MIME_EXT`:
        png / jpeg / webp / gif). SVG is intentionally rejected — it is markup,
        belongs to ``kind="svg"``, and serving attacker-authored SVG as an image
        is an XSS vector. ``image_bytes`` must be non-empty and within
        :data:`MAX_CONTENT_BYTES`. The SHA-256 and (best-effort) pixel
        dimensions are recorded in :class:`ImageMetadata`.

        ``auto_registered=True`` marks the record machine-created (from a
        chat-emitted ``![](...)``), making it sweepable by
        :meth:`prune_auto_widgets` while unpinned — the same lifecycle as
        auto-registered widgets. Only :mod:`kiro_crew.image_artifacts` sets it.
        """
        if not isinstance(image_bytes, (bytes, bytearray)):
            raise ArtifactValidationError(
                f"image_bytes must be bytes, got {type(image_bytes).__name__}"
            )
        data = bytes(image_bytes)
        norm_mime = (mime or "").strip().lower()
        if norm_mime not in _IMAGE_MIME_EXT:
            raise ArtifactValidationError(
                f"unsupported image mime {mime!r}: must be one of {sorted(_IMAGE_MIME_EXT)}"
            )
        if not data:
            raise ArtifactValidationError("image bytes are empty")
        if len(data) > MAX_CONTENT_BYTES:
            raise ArtifactValidationError(f"image exceeds {MAX_CONTENT_BYTES} bytes ({len(data)})")
        name = _validate_name(name)
        source = _validate_source(source)
        description = _validate_description(description)
        tags_list = _validate_tags(tags)
        ext = _IMAGE_MIME_EXT[norm_mime]
        width, height = _sniff_image_dimensions(data, norm_mime)
        image_meta = ImageMetadata(
            mime=norm_mime,
            ext=ext,
            size_bytes=len(data),
            width=width,
            height=height,
            sha256=hashlib.sha256(data).hexdigest(),
            original_filename=str(original_filename or "")[: _rules.MAX_NAME_LEN],
            alt=str(alt or "")[: _rules.MAX_DESCRIPTION_LEN],
        )

        with self._lock:
            slug, collided_with = self._claim_slug(slug, name)

            now = _now_iso()
            art = Artifact(
                slug=slug,
                name=name,
                kind="image",
                source=source,
                description=description,
                tags=tags_list,
                version=1,
                created_at=now,
                updated_at=now,
                content="",  # image body lives in the asset sidecar, not here
                folder_id=folder_id or "",
                session_key=session_key[:256] if session_key else "",
                auto_registered=bool(auto_registered),
                version_kinds={"1": "image"},
                image=image_meta,
                slug_collided_with=collided_with,
            )
            self._append_event(
                art,
                type="created",
                by=source if source != "chat" else "agent",
                version=1,
            )
            art.events_backfilled = True
            try:
                self._write_image_artifact(art, data)
            except Exception:
                # A half-written directory still reserves the slug, and the slug
                # is deterministic — so every retry would raise
                # ArtifactAlreadyExistsError and the image would be lost for
                # good. Roll the reservation back, then let the caller see the
                # real failure.
                try:
                    self._rmtree(self._artifact_dir(slug))
                except Exception:  # pragma: no cover — cleanup is best-effort
                    logger.warning("could not clean up partial image artifact %s", slug)
                raise
            logger.info(
                "image artifact created: slug=%s name=%s mime=%s bytes=%d",
                slug,
                name,
                norm_mime,
                len(data),
            )
        self._fire_change("upsert", slug)
        return art

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

    def get(self, slug: str, *, version: int | None = None) -> Artifact:
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
            else:
                meta.content = self._read_text(self._artifact_dir(slug) / "current.html")
                self._ensure_content_salt_locked(meta)
                meta.content_token = self._content_token(meta, meta.content)
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

    def allowed_source_roots(self, source_root: str = "") -> list[Path]:
        """Resolved directory roots a file-backed ``source_path`` may live under.

        SINGLE producer of this set. Three sites consume it — the live read
        (:meth:`_try_read_source_path`), the live write
        (:meth:`_try_write_source_path`), and the relocate handler's containment
        barrier in ``dashboard/handlers/artifacts.py`` — and they had drifted
        apart: the handler's copy omitted the data-home root, so relocate
        refused paths the store would happily read.

        The set is:

        * the user's home directory;
        * the data home (this store's parent — ``~/.kiro/crew`` in production,
          a tmp dir under test);
        * every operator-configured ``publish.relocate_roots`` entry;
        * when supplied, the artifact's own ``source_root`` — the project root
          that authorized the link at create time. This is what lets a linked
          project file outside ``$HOME`` (``/workplace/user/repo/doc.md``) be
          read at all, without widening the default set for every artifact.

        Callers MUST keep the ``p == r or p.is_relative_to(r)`` comparison
        INLINE at the call site. CodeQL's path-injection taint tracker only
        recognizes that sanitizer intra-procedurally, so moving the comparison
        into this method would reopen the alert
        (``handlers/artifacts.py`` documents the same constraint). This method
        only assembles the roots; it never decides containment.
        """
        allowed = [Path.home().resolve(), self._root.resolve().parent]
        try:

            for extra in KiroCrewConfig.load().publish.relocate_roots:
                if isinstance(extra, str) and extra.strip():
                    allowed.append(Path(extra).expanduser().resolve())
        except Exception:
            pass
        # A persisted source_root is a HINT, not authority: meta.json lives in
        # the agent-writable data home, so honouring it verbatim would let a
        # forged record (source_root="/", source_path="/etc/passwd") widen this
        # boundary to the whole filesystem. Re-verify it against something a
        # metadata write cannot fake.
        if source_root and isinstance(source_root, str) and source_root.strip():
            if is_verifiable_root(source_root):
                try:
                    allowed.append(Path(source_root).expanduser().resolve())
                except (OSError, ValueError):  # pragma: no cover — defensive
                    logger.warning("unusable source_root %r; ignoring", source_root)
            else:
                logger.warning(
                    "source_root %r no longer verifies as a project root; ignoring",
                    source_root,
                )
        return allowed

    def _try_read_source_path(self, source_path: str, source_root: str = "") -> str | None:
        """Read the source file for a file-backed artifact (live pointer).

        Returns None on any failure (missing file, permission denied,
        sensitive path, outside allowed roots, oversize). Caller falls back
        to the artifact's own snapshot in that case so a missing/moved
        source doesn't break the artifact view — and MUST surface
        ``source_missing`` so that fallback isn't mistaken for a healthy,
        in-sync live pointer.

        ``source_root`` is the artifact's recorded authorizing root; pass
        ``meta.source_root`` so a linked project file outside ``$HOME``
        resolves. See :meth:`allowed_source_roots`.
        """
        try:
            # Resolve before the security check so traversal segments
            # (`..`) and symlinks pointing into sensitive locations can't
            # bypass is_sensitive_path. A benign-looking
            # `source_path` with `../../etc/shadow` would otherwise sneak
            # past, since is_sensitive_path() inspects the literal string.
            p = Path(source_path).expanduser().resolve()
            if not p.is_absolute():
                return None
            if is_sensitive_path(str(p)):
                return None
            # Root-confinement re-check on every read: a symlink replacement
            # after relocate could escape the allowed roots if we only checked
            # at set time. Re-validate that the RESOLVED path is under the
            # user's home, the KIROCREW_HOME tree, a configured relocate root,
            # or the artifact's own recorded source_root.
            allowed = self.allowed_source_roots(source_root)
            # Comparison stays INLINE (not in a helper) — CodeQL's taint
            # tracker needs the is_relative_to sanitizer to guard the SAME
            # `p` the reads below use.
            containing = next((r for r in allowed if p == r or p.is_relative_to(r)), None)
            within_root = containing is not None
            if not within_root:
                logger.warning(
                    "source_path %r resolved outside allowed roots; refusing read", source_path
                )
                return None
            if not p.exists() or not p.is_file():
                # Dead pointer. Logged at WARNING (not silence) because the
                # caller falls back to the snapshot, which makes this
                # invisible in the artifact view unless it's reported.
                logger.warning(
                    "source_path %r no longer exists (or is not a regular file); "
                    "artifact falls back to its last snapshot",
                    source_path,
                )
                return None
            # Bound the read at the FILE level, not after-the-fact: read
            # MAX_CONTENT_BYTES+1 bytes from disk, decode (errors='replace'
            # for invalid sequences). p.read_text() would load the entire
            # file into memory before the size check — a multi-GB file
            # pointed to by source_path would exhaust memory before
            # truncation triggered. Bounding the read caps memory at
            # MAX_CONTENT_BYTES+1 regardless of file size.
            # Read through the descriptor-pinned helper rather than by name.
            # The containment check above is on a RESOLVED path, which still
            # leaves a check-to-use window: the final component, or an ancestor
            # directory, can be swapped for a symlink before the open, so the
            # bytes could come from a file outside `containing`. The helper opens
            # with O_NOFOLLOW and re-verifies the OPENED inode's real path
            # against the same root, so the inode validated is the inode read.
            raw = hooks.safe_read_file_bytes_nolink(
                str(p),
                within_root=str(containing),
                max_bytes=MAX_CONTENT_BYTES,
                allow_truncate=True,
            )
            if raw is None:
                logger.warning(
                    "source_path %r refused by the descriptor-pinned read gate", source_path
                )
                return None
            if len(raw) == MAX_CONTENT_BYTES:
                logger.warning("source file %s hit MAX_CONTENT_BYTES; view is truncated", p)
            # errors='replace' keeps the artifact viewable even when the
            # file contains malformed UTF-8 sequences. The byte-level
            # truncation may chop a multi-byte character at the boundary;
            # the replace handler emits U+FFFD for that case.
            return raw.decode("utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            logger.warning("failed to read source_path %r: %s", source_path, exc)
            return None

    def _try_write_source_path(self, source_path: str, content: str, source_root: str = "") -> bool:
        """Write to the source file for a file-backed artifact.

        Returns True on success, False if the path is unwritable. Caller
        proceeds to update the artifact's own storage either way — the
        snapshot remains authoritative even when the source can't be
        kept in sync.

        ``source_root`` mirrors the read side: the same recorded root that
        authorizes reads authorizes the write-back, so an edit to a linked
        project file lands in the project rather than being silently dropped.
        """
        try:
            # Same canonicalization as the read side — `.resolve()` prevents
            # symlink-based bypass of is_sensitive_path. Writing through a
            # symlink to a sensitive file is arguably worse than reading.
            p = Path(source_path).expanduser().resolve()
            if not p.is_absolute():
                return False
            if is_sensitive_path(str(p)):
                return False
            # Root-confinement re-check (same set as _try_read_source_path, via
            # the single producer): a symlink swap after relocate must not allow
            # writes outside the allowed roots.
            allowed = self.allowed_source_roots(source_root)
            # Comparison stays INLINE — see allowed_source_roots() on why the
            # CodeQL sanitizer cannot be factored out.
            containing = next((r for r in allowed if p == r or p.is_relative_to(r)), None)
            within_root = containing is not None
            if not within_root:
                logger.warning(
                    "source_path %r resolved outside allowed roots; refusing write",
                    source_path,
                )
                return False
            # Don't create the file if it never existed — that would be
            # surprising. The 'Add to artifacts' flow always saves an
            # existing file, so the file should exist.
            if not p.exists():
                return False
            # Write through the descriptor-pinned helper, not by name. The
            # containment check above is on a RESOLVED path, so a symlink
            # swapped into the final component -- or into an ancestor directory
            # -- between that check and the open would land these bytes on a
            # file outside `containing`. Writing through a symlink is strictly
            # worse than reading through one, so the same fd-pinned gate the
            # read side uses applies here: O_NOFOLLOW open first, then hardlink
            # / regular-file / real-path / sensitive checks on that descriptor.
            if not hooks.safe_write_file_nolink(str(p), content, within_root=str(containing)):
                logger.warning(
                    "source_path %r refused by the descriptor-pinned write gate", source_path
                )
                return False
            return True
        except (OSError, ValueError) as exc:
            logger.warning("failed to write source_path %r: %s", source_path, exc)
            return False

    def update(
        self,
        slug: str,
        *,
        content: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        name: str | None = None,
        kind: str | None = None,
        webapp_metadata: "WebAppMetadata | None" = None,
        actor: str = "user",
        session_id: str | None = None,
        event_type: str | None = None,
        from_version: int | None = None,
        snapshot: bool = False,
        expected_token: str | None = None,
    ) -> Artifact:
        """Update an artifact in place. Content writes always update the live
        state (source_path on disk for file-backed artifacts, current.html
        for chat-backed). When ``snapshot`` is True the new state is also
        captured as a numbered version with a lifecycle event. When False
        (the default), the save is silent — the version dropdown stays the
        same and no event is emitted (explicit-snapshot
        model).

        ``actor`` distinguishes lifecycle event types when a snapshot is
        taken: ``"user"`` (default) emits an ``edited`` event; ``"agent"``
        emits ``iterated``. ``session_id`` is captured on the event so the
        activity timeline can deep-link back to the originating chat.

        ``event_type`` overrides the actor-based default — used by the
        revert flow to mark events as ``reverted`` even though the actor is
        ``user``. Must be in :data:`ALLOWED_EVENT_TYPES` if provided.
        ``from_version`` is recorded on ``reverted`` events so the timeline
        can show "Reverted to vN".

        ``expected_token`` makes a content write conditional: it is the
        ``content_token`` a prior ``get()`` returned, and when the live content
        mints a different token the write raises :class:`ArtifactConflictError`
        and changes nothing. Omitted, the write is last-write-wins.
        It applies to store-backed artifacts only; a live file-backed artifact
        mints no token and ignores it, because its content is owned by the
        source file, which writers outside the store change without the lock.
        """
        slug = _validate_slug(slug)
        if expected_token is not None and not (
            isinstance(expected_token, str) and _TOKEN_RE.fullmatch(expected_token)
        ):
            raise ArtifactValidationError(
                "expected_token must be the 64-character hex content_token from a prior read"
            )
        with self._lock:
            art = self._load_meta(slug)
            # Unconditional: this call can demote a live pointer to copy-only and
            # then mint a token for it.
            self._ensure_content_salt_locked(art)
            guarded = (
                expected_token is not None
                and content is not None
                and not self._is_live_pointer(art)
            )
            if guarded:
                assert expected_token is not None  # narrowed by ``guarded``
                live_token = self._content_token(
                    art, self._read_text(self._artifact_dir(slug) / "current.html")
                )
                if not hmac.compare_digest(live_token, expected_token):
                    raise ArtifactConflictError(
                        f"artifact {slug!r} changed since it was read; refetch and re-base",
                        current_token=live_token,
                    )
            changed_content = False
            # True when this call READ art.content off source_path (snapshot path),
            # which makes writing it back unsafe -- see the snapshot branch below.
            snapshot_derived = False
            name_changed = False
            kind_changed = False
            if content is not None:
                content = _validate_content(content)
                changed_content = True
                art.content = content
            if description is not None:
                art.description = _validate_description(description)
            if tags is not None:
                art.tags = _validate_tags(tags)
            if name is not None:
                new_name = _validate_name(name)
                name_changed = new_name != art.name
                art.name = new_name
            if kind is not None:
                # Render kind may change on a pull (a widget's upstream bytes
                # are an already-wrapped document → ``html``). A kind change
                # alone does not bump the version; the per-version kind is
                # recorded in the snapshot branch below when content changes.
                new_kind = _validate_kind(kind)
                kind_changed = new_kind != art.kind
                art.kind = new_kind
                # An explicit kind is the caller's decision — stop re-detecting
                # from content on subsequent saves.
                art.kind_auto = False
            if webapp_metadata is not None:
                art.webapp_metadata = webapp_metadata
            art.updated_at = _now_iso()

            # Snapshot of current live state (no new content provided).
            # the user can click Snapshot at any time
            # to capture the live state — including after silent saves OR
            # after the source file changed externally for file-backed
            # artifacts. We read live content the same way get() does and
            # then fall through to the changed_content branch below, which
            # handles writing current.html, source_path, and the version
            # snapshot uniformly.
            if snapshot and not changed_content:
                # A copy owns its bytes: re-reading the original here would
                # overwrite the user's edits with the source file's content.
                if art.source_path and not art.source_copy_only:
                    live = self._try_read_source_path(art.source_path, art.source_root)
                    if live is not None:
                        art.content = live
                    else:
                        # Same dead-pointer flag as get(): a snapshot taken off
                        # the fallback must not look like it captured live state.
                        art.source_missing = True
                        art.content = self._read_text(self._artifact_dir(slug) / "current.html")
                else:
                    art.content = self._read_text(self._artifact_dir(slug) / "current.html")
                changed_content = True  # treat as a change for the snapshot path
                # This content came FROM disk, so mirroring it back is at best a
                # no-op -- and at worst DATA LOSS: the live read is bounded at
                # MAX_CONTENT_BYTES, so a source file larger than that yields a
                # PREFIX, and writing the prefix back would truncate the user's
                # file. A snapshot is a read operation; it must never write out.
                snapshot_derived = True

            if changed_content:
                # Always update the live state — current.html for chat-backed
                # artifacts, plus source_path on disk for file-backed (so
                # MarkdownPanel and the artifact viewer stay in sync).
                # Use art.content (not the local ``content`` arg) because the
                # snapshot-without-content path sets art.content from disk
                # without populating ``content``.
                live_content = art.content or ""
                # Settle an auto-assigned kind now that there is something to
                # look at. A document created blank starts as markdown; the
                # first save that makes it recognizably JSON or SVG re-types it
                # so it gets the right renderer. Detection only ever returns an
                # editable kind and returns None for anything unrecognized, so
                # this can neither strand the user's editor nor flap the kind
                # back on a mid-edit syntax error. An explicit ``kind`` on this
                # call already cleared ``kind_auto`` above, so a pinned artifact
                # is never touched here.
                if art.kind_auto:
                    detected = detect_editor_kind(live_content)
                    if detected and detected != art.kind:
                        logger.info(
                            "artifact kind auto-detected: slug=%s %s->%s",
                            slug,
                            art.kind,
                            detected,
                        )
                        art.kind = detected
                prev = self._artifact_dir(slug) / "current.html"
                self._write_text(prev, live_content)
                if content is not None:
                    # A new write is a new token domain (see ``Artifact.content_salt``).
                    art.content_salt = _new_content_salt()
                # Never mirror back for a copy — editing it must not rewrite
                # the user's original file — and never for content this call
                # just READ off that same file (see snapshot_derived above).
                if art.source_path and not art.source_copy_only and not snapshot_derived:
                    if not self._try_write_source_path(
                        art.source_path, live_content, art.source_root
                    ):
                        # The mirror was REFUSED (read-only file, a concurrent save
                        # that would have been clobbered, ownership we may not
                        # reassign, a source too large to roll back, a path no
                        # longer inside its authorizing root...). The user's edit is
                        # already in current.html, but while this artifact still
                        # claims to be a live pointer the next read prefers the
                        # SOURCE -- which would serve the old text back and report
                        # itself clean, silently discarding the edit.
                        #
                        # So the artifact takes ownership of its own copy. It keeps
                        # source_path as provenance and stops pretending the file
                        # tracks it. Persisted below with the rest of the metadata,
                        # so the demotion survives a restart rather than being
                        # re-attempted and re-lost on every save.
                        logger.warning(
                            "artifact %s could not mirror to %s; keeping the artifact's own "
                            "copy authoritative (source_copy_only)",
                            slug,
                            art.source_path,
                        )
                        art.source_copy_only = True
                if not self._is_live_pointer(art):
                    art.content_token = self._content_token(art, live_content)
                # Content changed — re-validate comment anchors so threads
                # whose quoted text no longer exists get flagged as orphaned
                # (and restored if the text comes back, e.g. on a revert).
                # Every content write funnels through here: agent iterations,
                # dashboard saves, reverts, and upstream pulls.
                self._rescan_comment_anchors_locked(slug, live_content)

                if snapshot:
                    # Validate event_type BEFORE side effects.
                    # Otherwise an invalid event_type raises after the
                    # version bump and versions/v{N}.html write, leaving an
                    # orphaned file on disk because _write_meta is never
                    # reached. Validate first; commit second.
                    if event_type is not None and event_type not in _records.ALLOWED_EVENT_TYPES:
                        raise ArtifactValidationError(
                            f"invalid event type {event_type!r}: "
                            f"must be one of {sorted(_records.ALLOWED_EVENT_TYPES)}"
                        )
                    # Bump version + capture the new state under
                    # versions/v{N}.html so it's preserved in history.
                    art.version += 1
                    self._snapshot_version(slug, art.version, prev)
                    # Record the render kind for this version so a later revert
                    # restores the correct render-mode (widget vs html).
                    art.version_kinds[str(art.version)] = art.kind
                    # Lifecycle event. Caller-specified event_type wins
                    # (revert flow uses 'reverted'); otherwise actor-based
                    # default: agent → iterated, user → edited.
                    if event_type is not None:
                        resolved_event_type = event_type
                    elif actor == "agent":
                        resolved_event_type = "iterated"
                    else:
                        resolved_event_type = "edited"
                    self._append_event(
                        art,
                        type=resolved_event_type,
                        by=actor,
                        session_id=session_id,
                        version=art.version,
                        from_version=from_version,
                    )

            # Keep version_kinds bounded in lockstep with version-file pruning
            # (we only ever ADD on snapshot; trim stale keys for pruned
            # versions so meta.json doesn't grow unbounded on long-lived
            # artifacts).
            if len(art.version_kinds) > MAX_VERSIONS:
                cutoff = art.version - MAX_VERSIONS
                art.version_kinds = {
                    k: v for k, v in art.version_kinds.items() if k.isdigit() and int(k) > cutoff
                }
            self._write_meta(art)
            self._prune_versions(slug)
            logger.info(
                "artifact updated: slug=%s version=%s changed_content=%s snapshot=%s",
                slug,
                art.version,
                changed_content,
                snapshot,
            )
            fire_upsert = changed_content or kind_changed
            fire_rename = name_changed and not fire_upsert
        # A content change is worth re-ingesting; a metadata-only rename just
        # refreshes the KB group label (no chunk churn). Description/tag-only
        # updates fire nothing. Fire outside the lock so the listener may call
        # get().
        #
        # A KIND change fires too, even with identical content: Knowledge
        # eligibility is keyed on kind, so switching markdown → svg has to be
        # reconciled or the obsolete chunks stay searchable. The listener owns
        # that decision (re-ingest or remove); the store only reports the change.
        if fire_upsert:
            self._fire_change("upsert", slug)
        elif fire_rename:
            self._fire_change("rename", slug)
        return art

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

    def _is_sweepable_auto_widget(self, art: Artifact) -> bool:
        """True when ``art`` is a machine-created widget record nobody has claimed.

        This predicate is the ONLY thing standing between the auto-registration
        firehose and deleting data a user cares about, so it is deliberately
        conservative: ANY signal of human or agent investment exempts the record
        permanently. Sweeping is for untouched throwaway widgets only.

        Exempt when the artifact:

        * was not auto-registered (an explicit save is never swept);
        * is ``pinned`` — the star is the explicit "keep this" signal;
        * has been filed into a folder (``folder_id``) — filing is curation;
        * has been published/shared (``publication``) — a live URL points at it,
          so deleting it breaks someone else's link;
        * is a fork (``fork_metadata``) — it carries upstream provenance;
        * has been edited at all — the user or the agent iterated on it, which is
          investment even without a star;
        * carries a description or tags — only a deliberate call sets those;
        * has any comment — commenting is unambiguous investment, and comments
          live in a ``comments.json`` sidecar that ``add_comment`` writes WITHOUT
          touching ``meta.json``, so the ``updated_at`` test below cannot see it.

        The edit test is ``updated_at != created_at``, NOT ``version > 1``:
        :meth:`update` only bumps ``version`` when ``snapshot=True``, so a plain
        content save (the common agent-iteration path) leaves the version at 1
        while rewriting the body. Keying on the version would have let the sweep
        delete freshly-iterated widgets. Metadata-only flips (``set_pinned`` /
        ``set_folder``) deliberately do NOT touch ``updated_at``, which is why
        they are checked as separate signals above.

        A widget that is merely *rendered* is not claimed; a widget that was
        touched in any of the above ways is.
        """
        if not art.auto_registered or art.pinned:
            return False
        if art.folder_id or art.publication is not None or art.fork_metadata is not None:
            return False
        if art.description or art.tags:
            return False
        # Any content/metadata edit stamps updated_at (see docstring).
        if art.created_at and art.updated_at and art.updated_at != art.created_at:
            return False
        # Comments live in a sidecar that add_comment writes without touching
        # meta.json, so they are invisible to every check above. A stat is enough
        # — the file only exists once something was written to it.
        if (self._artifact_dir(art.slug) / "comments.json").exists():
            return False
        return True

    def prune_auto_widgets(self, *, keep: int = MAX_AUTO_WIDGET_ARTIFACTS) -> int:
        """Delete the oldest unclaimed auto-registered widgets past ``keep``.

        Every chat-emitted ``<mcwidget>`` is registered automatically (see
        :mod:`kiro_crew.widget_artifacts`), so this sweep is what keeps that from
        growing without bound. Eligibility is decided by
        :meth:`_is_sweepable_auto_widget`, which exempts every record showing a
        sign of investment (starred, filed, published, forked, edited, described,
        tagged) — read that docstring before widening this.

        Ordering is newest-first, so the oldest untouched widgets go first (every
        candidate is by definition unedited, so this is effectively creation
        order). Returns the number deleted. Best-effort per artifact: a delete
        that fails (already gone, permissions) is logged and skipped rather than
        aborting the sweep.

        Race note: the candidate snapshot is taken unlocked, so eligibility is
        re-checked **and the directory removed in a single lock acquisition**. A
        re-check that released the lock before calling :meth:`delete` would leave
        the same window it was meant to close — a star landing between the two
        acquisitions would be overwritten by a delete acting on a stale verdict.
        """
        keep = max(0, int(keep))
        candidates = [art for art in self.list() if self._is_sweepable_auto_widget(art)]
        if len(candidates) <= keep:
            return 0
        # ``list()`` already sorts on ``(updated_at, slug)``, so the kept/dropped
        # boundary is stable. Re-sorting here is belt-and-braces: this sweep DELETES,
        # so it must not inherit an ordering assumption from a caller-supplied list.
        candidates.sort(key=lambda a: (a.updated_at, a.slug), reverse=True)
        # Newest-first, so everything past `keep` is the oldest tail.
        deleted = 0
        for art in candidates[keep:]:
            try:
                # Re-check eligibility and remove the directory in ONE lock
                # acquisition. The snapshot above is unlocked, so the user may
                # have starred this widget (or the agent edited it) in the
                # interim — but re-checking under the lock and then calling
                # ``delete()`` (which takes the lock again) would reopen the same
                # window between the two acquisitions: a pin landing there would
                # be overwritten by a delete acting on an already-stale verdict.
                # So the removal is inlined here rather than delegating.
                with self._lock:
                    fresh = self._load_meta(art.slug)
                    if not self._is_sweepable_auto_widget(fresh):
                        continue
                    adir = self._artifact_dir(art.slug)
                    if not adir.exists():
                        continue
                    self._rmtree(adir)
                    logger.info("auto-widget pruned: slug=%s", art.slug)
            except (ArtifactNotFoundError, ArtifactError, OSError) as exc:
                logger.warning("auto-widget prune skipped %s: %s", art.slug, exc)
                continue
            # Fired outside the lock, matching ``delete()``.
            self._fire_change("delete", art.slug)
            deleted += 1
        return deleted

    def settle_blank(
        self,
        slug: str,
        *,
        untitled_name: str,
        draft: str = "",
        allow_delete: bool = True,
    ) -> str:
        """Atomically resolve a just-created blank document that is being left.

        The library's "New artifact" action creates a document empty and opens it.
        When the user navigates away, one of three things should happen — keep it
        (they invested something), save the draft still sitting in the editor, or
        delete the empty shell they abandoned. This decides which, and acts, in a
        SINGLE lock acquisition.

        That atomicity is the whole point. Deciding client-side means reading the
        artifact, then acting on it, with a window in between — and a save landing
        in that window from a popout window or an agent gets overwritten or
        deleted. No amount of re-reading closes it, because the gap is between the
        read and the write, not inside the read. :meth:`prune_auto_widgets` faces
        the identical hazard and resolves it the same way: re-check and act without
        letting go of the lock.

        "Untouched" means untouched on every axis the record can report:

        * still at version 1 — a snapshot is history worth keeping even if the live
          body was emptied again afterwards;
        * still carrying the caller's untitled placeholder as its name;
        * empty content;
        * no tags, no description;
        * not filed, not starred, not published, not a fork;
        * no ``comments.json`` sidecar. Comments are written WITHOUT touching
          ``meta.json``, so they are invisible to every field above — the same
          reason :meth:`_is_sweepable_auto_widget` stats that file separately.

        The two questions are answered INDEPENDENTLY, and that matters:

        * *Should the draft be written?* Only that the stored content is still
          empty. Nothing else. A document can be renamed, tagged or filed and
          still be holding the user's first paragraph in an editor buffer that was
          never saved — refusing to write it because the name changed loses their
          typing.
        * *Should the shell be deleted?* Only when it is untouched on EVERY axis
          AND there is no draft AND the caller permits it. ``allow_delete=False``
          is how a client says "I have issued writes you may not have applied
          yet": it cannot make deletion safe, but it does not prevent the rescue.

        Returns what it did: ``"saved"`` (``draft`` became the content), ``"kept"``
        (left exactly as it was), or ``"deleted"``.
        """
        slug = _validate_slug(slug)
        draft = _validate_content(draft)
        with self._lock:
            art = self._load_meta(slug)
            live_empty = self._read_text(self._artifact_dir(slug) / "current.html") == ""
            if draft.strip() and live_empty:
                self._write_settled_draft(art, draft)
                outcome = "saved"
            else:
                outcome = ""
            pristine = outcome == "" and (
                allow_delete
                and art.version == 1
                and art.name == untitled_name
                and not art.description
                and not art.tags
                and not art.folder_id
                and not art.pinned
                and art.publication is None
                and art.fork_metadata is None
                and not (self._artifact_dir(slug) / "comments.json").exists()
                and live_empty
            )
            if outcome == "":
                if not pristine:
                    return "kept"
                self._rmtree(self._artifact_dir(slug))
                outcome = "deleted"
            logger.info("blank artifact settled: slug=%s outcome=%s", slug, outcome)
        # Fired outside the lock so a listener is free to call back into get().
        self._fire_change("upsert" if outcome == "saved" else "delete", slug)
        return outcome

    def _write_settled_draft(self, art: Artifact, draft: str) -> None:
        """Persist an unsaved editor buffer as the document's first content.

        The caller must hold ``self._lock`` and must have just confirmed that the
        stored content is empty — that confirmation is what makes this safe, since
        there is nothing to overwrite and therefore no concurrent save to lose.
        Metadata is preserved untouched: the user may well have named or tagged the
        document before typing, and this is a content write, not a reset.
        """
        self._write_text(self._artifact_dir(art.slug) / "current.html", draft)
        art.content = draft
        art.content_salt = _new_content_salt()
        # A settled draft is this document's first content, so it gets the same
        # kind detection an ordinary save would have applied. Without this, typing
        # JSON into a blank and navigating away stored it as markdown and rendered
        # it with the wrong viewer. Gated on ``kind_auto`` so a kind the user picked
        # explicitly still wins.
        if art.kind_auto:
            detected = detect_editor_kind(draft)
            if detected and detected != art.kind:
                art.kind = detected
                art.version_kinds[str(art.version)] = detected
        art.updated_at = _now_iso()
        self._write_meta(art)

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
        pinned: bool | None = None,
    ) -> _List[Artifact]:
        """List all artifacts matching the given filters (sorted newest first).

        The lock is held only long enough to snapshot the artifact-directory
        listing; meta.json reads happen outside the lock so concurrent
        ``create()`` / ``update()`` / ``delete()`` calls don't block behind
        an O(N) filesystem scan. Atomic meta.json writes (tmp + rename) make
        unlocked reads safe — the worst case is a stale-but-valid snapshot
        for an artifact that was just renamed.

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
        """
        with self._lock:
            meta_paths = list(self._iter_meta_paths())
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

    def migrate_kinds(self, *, apply: bool = False) -> _List[dict[str, Any]]:
        """Corrective one-time migration: reclassify markdown artifacts that
        were mis-saved as ``widget`` before ``kind`` inference existed.

        A ``widget`` artifact is treated as mis-saved markdown when
        :func:`_markdown_misclassification_reason` returns a reason — its
        ``source_path`` ends in ``.md`` / ``.markdown`` OR its current content
        has no HTML tags at all. Matching artifacts have their global ``kind``
        and any ``widget`` ``version_kinds`` entries flipped to ``markdown`` via
        a direct ``meta.json`` rewrite (the store reads ``meta.json`` per call,
        so the change is picked up with no cache invalidation or restart).

        With ``apply=False`` (default) this is a **dry run**: it returns the
        list of artifacts that *would* be flipped without writing anything. With
        ``apply=True`` it performs the rewrites. Idempotent — a second ``apply``
        run finds nothing because the candidates are no longer ``widget``.
        Returns one ``{slug, name, from_kind, to_kind, reason}`` entry per
        candidate. The directory listing is snapshotted under the lock; reads
        and per-artifact rewrites re-acquire it, matching ``list()``'s pattern.
        """
        with self._lock:
            meta_paths = list(self._iter_meta_paths())
        changed: _List[dict[str, Any]] = []
        for meta_path in meta_paths:
            try:
                art = self._read_meta_file(meta_path)
            except (
                ArtifactError,
                OSError,
                ValueError,
                TypeError,
            ) as exc:
                logger.warning("migrate_kinds: skipping unreadable %s: %s", meta_path, exc)
                continue
            if art.kind != "widget":
                continue
            try:
                content = self.get(art.slug).content or ""
            except ArtifactError as exc:
                logger.warning("migrate_kinds: cannot read content for %s: %s", art.slug, exc)
                continue
            reason = _markdown_misclassification_reason(content, art.source_path)
            if reason is None:
                continue
            changed.append(
                {
                    "slug": art.slug,
                    "name": art.name,
                    "from_kind": "widget",
                    "to_kind": "markdown",
                    "reason": reason,
                }
            )
            if not apply:
                continue
            with self._lock:
                fresh = self._load_meta(art.slug)
                if fresh.kind != "widget":
                    continue  # raced with another writer — leave it alone
                fresh.kind = "markdown"
                fresh.version_kinds = {
                    k: ("markdown" if v == "widget" else v) for k, v in fresh.version_kinds.items()
                }
                self._write_meta(fresh)
                logger.info("migrate_kinds: flipped %s widget->markdown", art.slug)
        return changed

    def find_by_artifact_id(
        self, artifact_id: str, *, provider: str | None = None
    ) -> Artifact | None:
        """Locate the local artifact linked to a cloud ``artifact_id``.

        Matches either a ``publication`` (my push target) or ``fork_metadata``
        (forked-from origin) that carries this upstream id. Bridges the cloud
        id to the local slug — used to reconcile the "my cloud artifacts" list
        against the local store and to keep clone/fork idempotent (don't create
        a second local copy of an artifact already tracked locally). Returns
        None when no local artifact tracks this id.

        Provider-native ids are NOT globally unique — providers A and B may both
        expose id ``123``. When *provider* is given, a candidate matches only if
        its own recorded provider equals *provider*; a record that predates
        multi-provider tracking (empty provider) still matches as a legacy
        fallback so old single-provider forks/publications keep resolving. When
        *provider* is None the match is id-only (callers that genuinely don't
        know the provider).

        Like ``find_by_source_path``, the scan runs outside the lock — atomic
        meta.json writes make stale-but-valid snapshots harmless.
        """
        if not artifact_id:
            return None

        from kiro_crew.publish_provider import DEFAULT_PROVIDER

        def _provider_ok(rec_provider: str) -> bool:
            # No provider filter → id-only match. Exact provider match → ok.
            # A legacy record with NO provider recorded originated from the
            # default provider, so it may only satisfy a request for that same
            # default provider — NOT an arbitrary provider B (which would let
            # provider B's clone bind an unrelated legacy artifact sharing the id).
            if provider is None or rec_provider == provider:
                return True
            return not rec_provider and provider == DEFAULT_PROVIDER

        with self._lock:
            meta_paths = list(self._iter_meta_paths())
        for meta_path in meta_paths:
            try:
                art = self._read_meta_file(meta_path)
            except (
                ArtifactError,
                OSError,
                ValueError,
                TypeError,
            ):
                continue
            if (
                art.publication is not None
                and art.publication.artifact_id == artifact_id
                and _provider_ok(art.publication.provider)
            ):
                return art
            if (
                art.fork_metadata is not None
                and art.fork_metadata.upstream_artifact_id == artifact_id
                and _provider_ok(art.fork_metadata.upstream_provider)
            ):
                return art
        return None

    @staticmethod
    def artifact_index_key(provider: str, artifact_id: str) -> str:
        """Compound index key for :meth:`index_by_artifact_id` lookups.

        Provider-native ids collide across providers, so the browse-annotation
        index is keyed by ``provider\\x00artifact_id``. A NUL separator can't
        appear in a provider name or id, so the key is unambiguous."""
        return f"{provider}\x00{artifact_id}"

    def index_by_artifact_id(self) -> dict[str, str]:
        """Build a ``{key: local_slug}`` map in a SINGLE store scan.

        The batch counterpart to :meth:`find_by_artifact_id` — annotating a
        browse page of N rows with per-row ``find_by_artifact_id`` is O(N × store)
        (a fresh full scan + meta.json parse per row); this pays one scan total.
        Each local artifact contributes its ``publication.artifact_id`` (my push
        target) and/or ``fork_metadata.upstream_artifact_id`` (forked-from origin).

        Keys are **provider-namespaced** via :meth:`artifact_index_key`
        (``provider\\x00id``) because provider-native ids are not globally
        unique — keying on the bare id would let a browse against provider B
        annotate provider A's local copy. A bare-id key is ALSO emitted (legacy
        fallback) so a lookup for a record that predates provider tracking still
        resolves. On collision the newest-first ``list()`` order wins.

        Scan runs outside the lock like ``list()`` — atomic meta.json writes
        make a stale-but-valid snapshot harmless.
        """
        index: dict[str, str] = {}
        with self._lock:
            meta_paths = list(self._iter_meta_paths())
        for meta_path in meta_paths:
            try:
                art = self._read_meta_file(meta_path)
            except (
                ArtifactError,
                OSError,
                ValueError,
                TypeError,
            ):
                continue
            pub = art.publication
            if pub is not None and pub.artifact_id:
                index.setdefault(self.artifact_index_key(pub.provider, pub.artifact_id), art.slug)
                # Bare-id key ONLY for a legacy record with no provider recorded.
                # Emitting it for every record would let a browse against provider
                # B fall back to provider A's slug on a shared id, wrongly marking
                # B's artifact as already-local and hiding its clone/fork action.
                if not pub.provider:
                    index.setdefault(pub.artifact_id, art.slug)
            fm = art.fork_metadata
            if fm is not None and fm.upstream_artifact_id:
                index.setdefault(
                    self.artifact_index_key(fm.upstream_provider, fm.upstream_artifact_id),
                    art.slug,
                )
                if not fm.upstream_provider:  # legacy bare-id fallback only
                    index.setdefault(fm.upstream_artifact_id, art.slug)
        return index

    def find_by_source_path(self, source_path: str) -> Artifact | None:
        """Locate an existing artifact previously saved from this filesystem path.

        Used by the 'Save as artifact' flow to detect re-saves of the same
        file and offer the caller a chance to bump the existing artifact's
        version rather than creating a parallel duplicate. Returns None when
        no artifact has this path recorded.

        Like ``list()``, the heavy filesystem scan happens outside the lock —
        meta.json atomic writes make stale-but-valid snapshots harmless.
        """
        if not source_path:
            return None
        with self._lock:
            meta_paths = list(self._iter_meta_paths())
        for meta_path in meta_paths:
            try:
                art = self._read_meta_file(meta_path)
            except (
                ArtifactError,
                OSError,
                ValueError,
                TypeError,
            ):
                # Same tolerance as list(): a single corrupted meta.json
                # shouldn't break this lookup.
                continue
            if art.source_path == source_path:
                return art
        return None

    def list_versions(self, slug: str) -> _List[int]:
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

    def relocate(self, slug: str, source_path: str, source_root: str = "") -> Artifact:
        """Update the source_path (live file pointer) for an artifact.

        ``source_root`` is rewritten in lockstep — it describes the root that
        authorizes THIS ``source_path``, so carrying the previous pointer's root
        forward would leave a record claiming an authorization it no longer has.
        The default (``""``) clears it, which is correct for the relocate
        handler: it validates against the store's base root set (home / data
        home / configured relocate roots), none of which need recording.
        """
        slug = _validate_slug(slug)
        source_path = _validate_source_path(source_path)
        source_root = _validate_source_path(source_root, "source_root") if source_path else ""
        with self._lock:
            art = self._load_meta(slug)
            art.source_path = source_path
            art.source_root = source_root
            # Relocate is an explicit "this artifact tracks THIS file" act, so
            # it promotes a copy into a live pointer. Leaving the flag set would
            # make the relocation silently inert -- reads and edits would keep
            # ignoring the very file the user just pointed at.
            art.source_copy_only = False
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

    def list_comments(self, slug: str) -> _List["ArtifactComment"]:
        """Load all comments for an artifact from comments.json sidecar."""
        slug = _validate_slug(slug)
        with self._lock:
            return self._load_comments(slug)

    def add_comment(self, slug: str, comment: "ArtifactComment") -> "ArtifactComment":
        """Persist a new comment to the sidecar store.

        Bounded at :data:`MAX_COMMENTS_PER_ARTIFACT` (FIFO). When appending would
        exceed the cap, the OLDEST thread roots (and their replies) are dropped
        as whole threads — never orphaning a reply — until the list fits. The
        just-added comment is always kept.
        """
        slug = _validate_slug(slug)
        with self._lock:
            comments = self._load_comments(slug)
            comments.append(comment)
            if len(comments) > MAX_COMMENTS_PER_ARTIFACT:
                comments = _threads.prune_oldest_threads(
                    comments, keep_id=comment.id, cap=MAX_COMMENTS_PER_ARTIFACT
                )
            self._write_comments(slug, comments)
            return comment

    #: Fields a caller may patch via :meth:`update_comment`. A narrow allowlist
    #: (mirroring ``update_publication`` / ``update_fork_metadata``) so a future
    #: MCP tool forwarding user/LLM-controlled keys cannot mass-assign identity/
    #: provenance fields (``is_agent`` / ``author`` / ``origin`` / ``sync_state`` /
    #: ``target_*``). Only the mutable lifecycle + body fields are patchable;
    #: ``updated_at`` is always refreshed by the method itself.
    _MUTABLE_COMMENT_FIELDS = frozenset({"status", "body", "anchor_orphaned"})

    def update_comment(self, slug: str, comment_id: str, **fields: Any) -> "ArtifactComment | None":
        """Patch fields on an existing comment. Returns updated or None.

        Only fields in :data:`_MUTABLE_COMMENT_FIELDS` may be set; an unknown or
        disallowed field name raises :class:`ArtifactError` rather than silently
        writing it (consistent with ``update_publication`` /
        ``update_fork_metadata``).
        """
        slug = _validate_slug(slug)
        with self._lock:
            comments = self._load_comments(slug)
            for c in comments:
                if c.id == comment_id:
                    for k, v in fields.items():
                        if k not in self._MUTABLE_COMMENT_FIELDS:
                            raise ArtifactError(f"comment field {k!r} is not patchable")
                        setattr(c, k, v)
                    c.updated_at = _now_iso()
                    self._write_comments(slug, comments)
                    return c
            return None

    def delete_comment(self, slug: str, comment_id: str) -> bool:
        """Remove a comment from the sidecar store. Returns True if found.

        Deleting a thread ROOT cascades to its replies: threads are one level
        deep and replies carry the root's id as their ``thread_id``, so a parent
        delete removes the whole thread rather than orphaning the children into
        top-level comments. Deleting a reply removes only that reply.
        """
        slug = _validate_slug(slug)
        with self._lock:
            comments = self._load_comments(slug)
            before = len(comments)
            comments = _threads.remove_comment(comments, comment_id)
            if len(comments) < before:
                self._write_comments(slug, comments)
                return True
            return False

    def merge_remote_comments(
        self, slug: str, provider: str, remote_comments: _List["ArtifactComment"]
    ) -> _List["ArtifactComment"]:
        """Reconcile remote (provider) comments into the local mirror.

        The provider is authoritative for its own comments. This:
          * drops local mirrors that came back tombstoned (``deleted``) — so a
            comment deleted on the remote disappears locally;
          * syncs mutable fields (status/body/author) of changed provider
            comments — so an inbound resolve/edit is reflected;
          * adds newly-seen provider comments;
          * leaves local comments (``origin == "local"``) untouched, and keeps
            provider comments absent from this fetch (avoids wiping on a
            transient/paginated empty — real deletes return as tombstones).
        """
        slug = _validate_slug(slug)
        with self._lock:
            existing = self._load_comments(slug)
            result, changed = _threads.merge_remote(existing, remote_comments)
            if changed:
                self._write_comments(slug, result)
                return result
            return existing

    def _rescan_comment_anchors_locked(self, slug: str, content: str) -> None:
        """Re-validate every anchored comment against ``content`` and flip
        ``anchor_orphaned`` accordingly. MUST be called with ``self._lock``
        held (``update()`` calls it from inside its locked content-write
        branch; the lock is non-reentrant so this cannot go through the
        public ``list_comments``/``update_comment`` wrappers).

        The matching rule is :func:`kiro_crew.artifact_store.comments.rescan_anchors`'s.
        Tolerant — an anchor-rescan failure must never break the content write it
        piggybacks on.
        """
        try:
            comments = self._load_comments(slug)
            if _threads.rescan_anchors(comments, content, clock=_now_iso):
                self._write_comments(slug, comments)
        except Exception:  # pragma: no cover — best-effort side scan
            logger.warning("comment anchor rescan failed for %s", slug, exc_info=True)

    def record_comment_event(
        self,
        slug: str,
        *,
        action: str,
        by: str = "user",
        session_id: str | None = None,
        comment_snippet: str | None = None,
        reason: str | None = None,
    ) -> None:
        """Append a ``comment`` lifecycle event to ``slug``'s activity log.

        ``action`` says what happened to the comment (``deleted`` /
        ``reviewed`` / ``resolved``); ``comment_snippet`` is a short excerpt
        of the affected comment body so the timeline entry is readable
        without the (possibly deleted) comment; ``reason`` carries the
        agent's one-line justification on deletes. Tolerant — a timeline
        failure must never break the comment operation it annotates.
        """
        try:
            with self._lock:
                meta = self._load_meta(slug)
                metadata: dict[str, Any] = {"action": action}
                if comment_snippet:
                    metadata["comment_snippet"] = comment_snippet
                if reason:
                    metadata["reason"] = reason
                self._append_event(
                    meta,
                    type="comment",
                    by=by,
                    session_id=session_id,
                    version=meta.version,
                    metadata=metadata,
                )
                self._write_meta(meta)
        except Exception:  # pragma: no cover — timeline is best-effort
            logger.warning("comment event append failed for %s", slug, exc_info=True)

    def _load_comments(self, slug: str) -> _List["ArtifactComment"]:
        """Load comments.json sidecar (tolerant — missing file = empty)."""
        path = self._artifact_dir(slug) / "comments.json"
        if not path.exists():
            return []
        try:
            raw_list = json.loads(self._read_text(path))
        except (json.JSONDecodeError, ArtifactError):
            return []
        return _records.decode_comments(raw_list)

    def _write_comments(self, slug: str, comments: _List["ArtifactComment"]) -> None:
        """Persist comments list to comments.json sidecar."""
        path = self._artifact_dir(slug) / "comments.json"
        self._write_text(path, _records.encode_comments(comments))

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

    def _artifact_dir(self, slug: str) -> Path:
        adir = (self._root / slug).resolve(strict=False)
        # Defense in depth: ensure resolved path is still under root.
        if self._root.resolve(strict=False) not in adir.parents and adir != self._root.resolve(
            strict=False
        ):
            raise ArtifactValidationError(f"slug escapes artifact root: {slug}")
        return adir

    def _unique_slug(self, base: str) -> str:
        """Append ``-2``, ``-3`` ... until an unused slug is found."""
        candidate = base
        n = 1
        while self._artifact_dir(candidate).exists():
            n += 1
            candidate = f"{base[:75]}-{n}"
        return candidate

    def _claim_slug(self, slug: str | None, name: str) -> tuple[str, str]:
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

    def _write_artifact(self, art: Artifact, content: str) -> None:
        adir = self._artifact_dir(art.slug)
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "versions").mkdir(parents=True, exist_ok=True)
        self._write_text(adir / "current.html", content)
        self._snapshot_version(art.slug, art.version, adir / "current.html")
        art.content_salt = _new_content_salt()
        self._write_meta(art)

    def _write_image_artifact(self, art: Artifact, data: bytes) -> None:
        """Write an image artifact: empty text body + the raster asset sidecar.

        Mirrors :meth:`_write_artifact` so image records share the exact same
        directory shape (``current.html`` + ``versions/`` + ``meta.json``) and
        every text-store helper — ``get``, version snapshotting, ``_rmtree``
        delete — works on them unchanged. ``current.html`` is written empty per
        the image-kind contract; the bytes go to ``asset.<ext>`` through the
        gated byte writer.
        """
        adir = self._artifact_dir(art.slug)
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "versions").mkdir(parents=True, exist_ok=True)
        self._write_text(adir / "current.html", art.content or "")
        self._snapshot_version(art.slug, art.version, adir / "current.html")
        assert art.image is not None  # set by create_image before this is called
        self._write_bytes(adir / f"asset.{art.image.ext}", data)
        art.content_salt = _new_content_salt()
        self._write_meta(art)

    def _snapshot_version(self, slug: str, version: int, src: Path) -> None:
        target = self._artifact_dir(slug) / "versions" / f"v{version}.html"
        # Defense in depth: route the read through the gated helper so the
        # sensitive-path check fires on every filesystem read, even when
        # ``src`` is a store-internal path constructed by the store itself.
        # Per security rule 1: a read either goes through hooks.py or, as
        # here, asks ``is_sensitive_canonical_path`` on the canonicalised
        # path and opens through ``pinned_fs.open_fenced_for_read``.
        self._write_text(target, self._read_text(src))

    def _write_meta(self, art: Artifact) -> None:
        path = self._artifact_dir(art.slug) / "meta.json"
        self._write_text(path, _records.encode_meta(art))

    def _append_event(
        self,
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

    def _load_meta(self, slug: str) -> Artifact:
        path = self._artifact_dir(slug) / "meta.json"
        if not path.exists():
            raise ArtifactNotFoundError(f"artifact not found: {slug}")
        return self._read_meta_file(path)

    def _read_meta_file(self, path: Path) -> Artifact:
        return _records.decode_meta(json.loads(self._read_text(path)), path)

    # Kept on the class for callers that parse a single meta.json block directly.
    _parse_publication = staticmethod(_records.parse_publication)
    _parse_fork_metadata = staticmethod(_records.parse_fork_metadata)
    _parse_image_metadata = staticmethod(_records.parse_image_metadata)

    def _read_text(self, path: Path) -> str:
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

    def _write_text(self, path: Path, text: str) -> None:
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
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp file + rename.
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(resolved)

    def _read_bytes(self, path: Path) -> bytes:
        """Binary sibling of :meth:`_read_text` (image asset reads).

        Same sensitive-path gate and the same descriptor checks as text reads.
        """
        resolved = Path(os.path.realpath(path))
        if reason := _fence_refusal(resolved, "read"):
            raise ArtifactError(reason)
        fd = _open_pinned_for_read(resolved)
        with os.fdopen(fd, "rb") as fh:
            return fh.read()

    def _read_image_asset_bytes(self, path: Path) -> bytes:
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

    def _write_bytes(self, path: Path, data: bytes) -> None:
        """Binary sibling of :meth:`_write_text` (image asset writes).

        Same sensitive-path gate and same atomic tmp-file + rename so a reader
        never observes a half-written asset.
        """
        resolved = Path(os.path.realpath(path))
        if reason := _fence_refusal(resolved, "write"):
            raise ArtifactError(reason)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(resolved)

    def _prune_versions(self, slug: str) -> None:
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

    def _iter_meta_paths(self) -> Iterator[Path]:
        if not self._root.exists():
            return
        for child in self._root.iterdir():
            if child.is_dir():
                meta = child / "meta.json"
                if meta.exists():
                    yield meta

    @staticmethod
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
