"""Artifacts — persistent identity, versioning, and iteration for LLM-generated UI.

Storage layout
--------------
``~/.kirocrew/artifacts/<slug>/``
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
- ``security.is_sensitive_path()`` is queried before any read/write, so the
  store cannot accidentally land under ``~/.aws``, ``~/.ssh``, etc.
- Tool invocations emit SEL audit events via ``sel().log_tool_invocation()``.

The MCP tools (``artifact_save`` etc.) and HTTP handlers wrap this module --
this file deliberately holds no networking, validation-layer, or rendering
logic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from typing import List as _List

from kiro_crew.config.loader import config_dir
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

#: Maximum number of versions retained per artifact (older versions are pruned
#: when the cap is exceeded).
MAX_VERSIONS = 50

#: Maximum size of an artifact's content blob, in bytes. Widget HTML rarely
#: exceeds a few KB; this cap exists to refuse unbounded LLM output.
MAX_CONTENT_BYTES = 1_048_576  # 1 MiB

#: Maximum length of human-readable name / description fields.
MAX_NAME_LEN = 200
MAX_DESCRIPTION_LEN = 2_000

#: Allowed kinds (extensible — agents pass plain strings, but a soft allow-list
#: keeps the dashboard's filter UI tractable).
ALLOWED_KINDS = frozenset(
    {
        "widget",  # default — sandboxed HTML/JS via mcwidget
        "html",  # raw html document
        "markdown",  # rendered to widget via MarkdownRenderer
        "svg",  # standalone svg
        "json",  # structured data
        "text",  # plain text
    }
)

#: Allowed source markers (provenance).
ALLOWED_SOURCES = frozenset({"chat", "cron", "subagent", "manual", "import"})

#: Allowed lifecycle event types. ``referenced`` is reserved for chat-mention
#: scanning (added in a follow-up CR); the in-line save/update path emits
#: ``created`` / ``edited`` / ``iterated`` / ``reverted``.
ALLOWED_EVENT_TYPES = frozenset({"created", "edited", "iterated", "referenced", "reverted"})

#: Max lifecycle events retained per artifact. FIFO eviction keeps meta.json
#: bounded — at ~150 bytes per event entry, this caps each meta file at
#: roughly 75KB on top of the static metadata, which is well within the
#: tolerable read cost.
MAX_EVENTS_PER_ARTIFACT = 500

#: Maximum number of tags per artifact, and max length per tag.
MAX_TAGS = 16
MAX_TAG_LEN = 64

# Slug pattern: lowercase letters, digits, hyphens. 1-80 chars. No leading or
# trailing hyphen. Single-character slugs are allowed for trivial names.
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")
_TAG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_:.-]{0,63}$")
_VERSION_FILE_RE = re.compile(r"^v(\d+)\.html$")
_SLUG_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")


# ── Exceptions ────────────────────────────────────────────────────────────────


class ArtifactError(Exception):
    """Base exception for artifact store failures."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an artifact slug does not resolve to a stored artifact."""


class ArtifactAlreadyExistsError(ArtifactError):
    """Raised when ``create()`` is called with an explicit slug that already exists.

    Distinct from the base ``ArtifactError`` so HTTP handlers can return 409
    (Conflict) for slug collisions and 500 for other store-level errors
    (sensitive-path refusal, atomic-write failure, etc.).
    """


class ArtifactValidationError(ArtifactError):
    """Raised when a field fails validation (slug, tag, kind, content, etc.)."""


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass
class Artifact:
    """In-memory representation of an artifact and its metadata.

    The ``content`` field is loaded on-demand and may be ``None`` for list
    operations to keep memory bounded.

    The ``events`` field is the lifecycle audit log — append-only structured
    entries for create / edit / iterate / reference operations. See
    :func:`ArtifactStore._append_event` for the entry shape and
    :data:`MAX_EVENTS_PER_ARTIFACT` for the FIFO retention cap.
    """

    slug: str
    name: str
    kind: str = "widget"
    source: str = "chat"
    description: str = ""
    tags: _List[str] = field(default_factory=list)
    version: int = 1
    created_at: str = ""
    updated_at: str = ""
    content: str | None = None  # loaded on demand
    events: _List[dict] = field(default_factory=list)
    events_backfilled: bool = False
    #: Original filesystem path for file-backed artifacts created via
    #: 'Save as artifact' from the file viewer. Empty for chat-backed
    #: artifacts. Used as a deduplication key when the same path is
    #: re-saved (Mesh-1654 Phase 6 — re-saving offers to bump the existing
    #: artifact's version rather than creating a parallel one).
    source_path: str = ""
    #: Nested-folder membership (Mesh-2720). ``""`` = unfiled (library root).
    #: An opaque folder id (see :class:`ArtifactFolderStore`), never a path —
    #: so renaming a folder never rewrites artifact records. Setting it is a
    #: metadata-only mutation (:meth:`ArtifactStore.set_folder`) that does NOT
    #: bump the version. Tolerant-loaded for legacy meta.json (defaults to
    #: unfiled). Only local artifacts carry a folder; remote/shared artifacts
    #: have no local record to hang one on.
    folder_id: str = ""
    #: Computed at GET time: True when the current live content differs
    #: from the latest numbered snapshot. Lets the frontend enable the
    #: Snapshot button anytime live has drifted from history — including
    #: cases where a file-backed artifact's source changed externally
    #: between the last snapshot and now (Mesh-1654 round 6, requested
    #: by nrb). Not persisted; set by ``get()``.
    live_dirty: bool = False

    def to_dict(self, *, include_content: bool = False, persist: bool = False) -> dict[str, Any]:
        """Render as a JSON-friendly dict, optionally including the content blob.

        ``persist=True`` strips fields that should never be written to
        meta.json (currently ``live_dirty`` — it's computed at GET time
        and persisting it would leave stale values lying around when the
        live state changes via a path the store didn't observe).
        """
        d = asdict(self)
        if not include_content:
            d.pop("content", None)
        if persist:
            # AutoSDE round 13: live_dirty is a transient, GET-time-computed
            # field. Persisting it via meta.json would create staleness
            # bugs (e.g. silent save flips it to True, but we'd write False
            # if the meta is touched again before a snapshot).
            d.pop("live_dirty", None)
        return d


# ── Helpers ────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    """Return the current UTC time as an ISO-8601 microsecond-precision string.

    Microsecond precision so artifacts created in rapid succession sort
    deterministically by ``updated_at``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def slugify(name: str) -> str:
    """Normalize a free-form name into a URL-safe slug.

    Falls back to ``"artifact"`` if the input contains no slug-safe characters.
    Truncated to 80 characters.
    """
    if not isinstance(name, str):
        raise ArtifactValidationError(f"name must be str, got {type(name).__name__}")
    # NFKD-normalize then drop combining marks so accented letters become ascii.
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().strip()
    text = _SLUG_NORMALIZE_RE.sub("-", text)
    text = text.strip("-")
    if not text:
        return "artifact"
    return text[:80].rstrip("-") or "artifact"


def _validate_slug(slug: str) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ArtifactValidationError(f"invalid slug {slug!r}: must match {_SLUG_RE.pattern}")
    return slug


def _validate_kind(kind: str) -> str:
    if kind not in ALLOWED_KINDS:
        raise ArtifactValidationError(
            f"invalid kind {kind!r}: must be one of {sorted(ALLOWED_KINDS)}"
        )
    return kind


#: File-extension → kind map for inferring the kind of a file-backed artifact
#: (one with a ``source_path``) when the caller didn't pin one. Keys lowercased.
_EXT_KIND_MAP = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    ".svg": "svg",
    ".json": "json",
    ".txt": "text",
}

#: Substrings whose presence marks inline content as renderable HTML (→
#: ``widget``). Matched case-insensitively against the lstripped content.
_HTML_SNIFF_MARKERS = (
    "<div",
    "<span",
    "<style",
    "<table",
    "<mcwidget",
    "<html",
    "<!doctype html",
)

#: A leading markdown ATX heading (``#`` .. ``######`` followed by whitespace).
_MD_HEADING_RE = re.compile(r"^#{1,6}\s")


def _infer_kind(content: str, source_path: str = "", explicit: str | None = None) -> str:
    """Infer an artifact ``kind`` when the caller didn't pin one.

    Resolution order (first match wins):

    1. **Explicit wins** — a non-empty ``explicit`` kind is returned unchanged
       (back-compat; the caller knows best). Validation happens downstream.
    2. **Extension** — for file-backed artifacts (``source_path`` set), map the
       file extension (``.md`` → ``markdown``, ``.json`` → ``json`` ...). An
       unknown extension on a real file falls back to ``text`` — a file on disk
       is far likelier plain text than a sandboxed widget.
    3. **Content sniff** — for inline content with no ``source_path``: HTML-ish
       markup (``<div``, ``<table``, an ``<mcwidget`` body ...) → ``widget``; a
       leading markdown heading or content with no HTML tags at all →
       ``markdown``; anything else falls back to the legacy ``widget`` default
       so ambiguous blobs keep their prior behavior.

    Only ``widget`` and ``markdown`` are inferred from inline content; the
    richer kinds (``svg`` / ``json`` / ``text``) require the extension signal.
    The returned value is not validated here — callers pass it through
    :func:`_validate_kind`.
    """
    if explicit:
        return explicit
    if source_path:
        ext = os.path.splitext(source_path)[1].lower()
        return _EXT_KIND_MAP.get(ext, "text")
    sniff = (content or "").lstrip()
    if not sniff:
        return "widget"  # empty inline content → keep the legacy default
    lowered = sniff.lower()
    if any(marker in lowered for marker in _HTML_SNIFF_MARKERS):
        return "widget"
    if _MD_HEADING_RE.match(sniff) or "<" not in sniff:
        return "markdown"
    return "widget"


def _validate_source(source: str) -> str:
    if source not in ALLOWED_SOURCES:
        raise ArtifactValidationError(
            f"invalid source {source!r}: must be one of {sorted(ALLOWED_SOURCES)}"
        )
    return source


def _validate_tags(tags: list[str] | None) -> _List[str]:
    if tags is None:
        return []
    if not isinstance(tags, list):
        raise ArtifactValidationError(f"tags must be a list, got {type(tags).__name__}")
    if len(tags) > MAX_TAGS:
        raise ArtifactValidationError(f"too many tags ({len(tags)} > {MAX_TAGS})")
    cleaned: _List[str] = []
    for t in tags:
        if not isinstance(t, str) or not _TAG_RE.match(t):
            raise ArtifactValidationError(f"invalid tag {t!r}: must match {_TAG_RE.pattern}")
        if t not in cleaned:  # preserve order, drop dupes
            cleaned.append(t)
    return cleaned


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise ArtifactValidationError(f"name must be str, got {type(name).__name__}")
    name = name.strip()
    if not name:
        raise ArtifactValidationError("name is required")
    if len(name) > MAX_NAME_LEN:
        raise ArtifactValidationError(f"name exceeds {MAX_NAME_LEN} chars")
    return name


def _validate_description(description: str | None) -> str:
    if description is None:
        return ""
    if not isinstance(description, str):
        raise ArtifactValidationError(f"description must be str, got {type(description).__name__}")
    if len(description) > MAX_DESCRIPTION_LEN:
        raise ArtifactValidationError(f"description exceeds {MAX_DESCRIPTION_LEN} chars")
    return description


def _validate_content(content: str) -> str:
    if not isinstance(content, str):
        raise ArtifactValidationError(f"content must be str, got {type(content).__name__}")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_CONTENT_BYTES:
        raise ArtifactValidationError(f"content exceeds {MAX_CONTENT_BYTES} bytes ({len(encoded)})")
    return content


# ── Store ────────────────────────────────────────────────────────────────────


class ArtifactStore:
    """File-system backed store for artifacts.

    Thread-safe via a coarse-grained lock; concurrent writes to the same
    artifact are serialized.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or (config_dir() / "artifacts")).expanduser()
        self._lock = threading.Lock()
        # Optional change-listener fired after a content-affecting mutation
        # (create / content-update / delete). Lets the gateway observe every
        # write path — agent (MCP-proxied), dashboard, bookmark, CLI, and the
        # Artifactory pull/clone paths all funnel through this store in the
        # gateway process, so one listener here catches them all without the
        # store importing (or knowing about) the knowledge package.
        self._change_listener: Callable[[str, str], None] | None = None
        # Refuse to land under any sensitive path. is_sensitive_path() handles
        # symlink resolution, so resolve() before checking.
        resolved = self._root.resolve(strict=False)
        if is_sensitive_path(str(resolved)):
            raise ArtifactError(f"refusing to use sensitive path as artifact root: {resolved}")
        self._root.mkdir(parents=True, exist_ok=True)

    # ── public API ────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

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
            logger.exception(
                "artifact change listener failed: action=%s slug=%s", action, slug
            )

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
        folder_id: str = "",
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
        """
        name = _validate_name(name)
        content = _validate_content(content)
        # Infer the kind when the caller didn't pin one (explicit kind always
        # wins). Validated content is needed for the inline content sniff, so
        # this runs after _validate_content.
        kind = _validate_kind(_infer_kind(content, source_path, kind))
        source = _validate_source(source)
        description = _validate_description(description)
        tags_list = _validate_tags(tags)

        with self._lock:
            if slug is None:
                slug = self._unique_slug(slugify(name))
            else:
                slug = _validate_slug(slug)
                if self._artifact_dir(slug).exists():
                    raise ArtifactAlreadyExistsError(f"artifact already exists: {slug}")

            now = _now_iso()
            art = Artifact(
                slug=slug,
                name=name,
                kind=kind,
                source=source,
                description=description,
                tags=tags_list,
                version=1,
                created_at=now,
                updated_at=now,
                content=content,
                source_path=source_path[:512] if source_path else "",
                folder_id=folder_id or "",
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
        return art

    def get(self, slug: str, *, version: int | None = None) -> Artifact:
        """Return an artifact (with content) by slug, optionally a specific version.

        Live-pointer behavior (Mesh-1654): for file-backed artifacts (those
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
            # Lazy backfill (Mesh-1654 Phase 5): legacy artifacts written
            # before the events field existed pick up a synthetic created/
            # edited history on first read. ``_backfill_events`` is idempotent
            # — once events_backfilled is True, this is a no-op. Persist the
            # synthesized events so subsequent reads don't repeat the work.
            if self._backfill_events(meta):
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
                meta.live_dirty = False  # historical view — not "live"
                return meta
            # Current view: prefer source_path for file-backed artifacts.
            if meta.source_path:
                live = self._try_read_source_path(meta.source_path)
                if live is not None:
                    meta.content = live
                else:
                    # Fall through to the snapshot fallback — file moved /
                    # deleted / unreadable.
                    meta.content = self._read_text(self._artifact_dir(slug) / "current.html")
            else:
                meta.content = self._read_text(self._artifact_dir(slug) / "current.html")
            # Compute live_dirty by comparing the live content to the
            # latest numbered snapshot. Catches both silent saves AND
            # external file edits to source_path that we never saw —
            # which is the whole point of round 6's "snapshot anytime"
            # request. If versions/vN.html is missing (legacy artifact
            # before snapshots existed), default to not-dirty.
            latest_vfile = self._artifact_dir(slug) / "versions" / f"v{meta.version}.html"
            if latest_vfile.exists():
                latest_snapshot = self._read_text(latest_vfile)
                meta.live_dirty = (meta.content or "") != latest_snapshot
            else:
                meta.live_dirty = False
            return meta

    def _try_read_source_path(self, source_path: str) -> str | None:
        """Read the source file for a file-backed artifact (live pointer).

        Returns None on any failure (missing file, permission denied,
        sensitive path, oversize). Caller falls back to the artifact's own
        snapshot in that case so a missing/moved source doesn't break the
        artifact view.
        """
        try:
            # Resolve before the security check so traversal segments
            # (`..`) and symlinks pointing into sensitive locations can't
            # bypass is_sensitive_path. AutoSDE round 12: a benign-looking
            # `source_path` with `../../etc/shadow` would otherwise sneak
            # past, since is_sensitive_path() inspects the literal string.
            p = Path(source_path).expanduser().resolve()
            if not p.is_absolute():
                return None
            if is_sensitive_path(str(p)):
                return None
            if not p.exists() or not p.is_file():
                return None
            # Bound the read at the FILE level, not after-the-fact: read
            # MAX_CONTENT_BYTES+1 bytes from disk, decode (errors='replace'
            # for invalid sequences). AutoSDE round 13: previously called
            # p.read_text() which loads the entire file into memory before
            # the size check — a multi-GB file pointed to by source_path
            # would exhaust memory before truncation triggered. Bounding
            # the read caps memory at MAX_CONTENT_BYTES+1 regardless of
            # file size.
            with p.open("rb") as f:
                raw = f.read(MAX_CONTENT_BYTES + 1)
            oversize = len(raw) > MAX_CONTENT_BYTES
            if oversize:
                logger.warning("source file %s exceeds MAX_CONTENT_BYTES; truncating", p)
                raw = raw[:MAX_CONTENT_BYTES]
            # errors='replace' keeps the artifact viewable even when the
            # file contains malformed UTF-8 sequences. The byte-level
            # truncation may chop a multi-byte character at the boundary;
            # the replace handler emits U+FFFD for that case.
            return raw.decode("utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            logger.warning("failed to read source_path %r: %s", source_path, exc)
            return None

    def _try_write_source_path(self, source_path: str, content: str) -> bool:
        """Write to the source file for a file-backed artifact.

        Returns True on success, False if the path is unwritable. Caller
        proceeds to update the artifact's own storage either way — the
        snapshot remains authoritative even when the source can't be
        kept in sync.
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
            # Don't create the file if it never existed — that would be
            # surprising. The 'Add to artifacts' flow always saves an
            # existing file, so the file should exist.
            if not p.exists():
                return False
            p.write_text(content, encoding="utf-8")
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
        actor: str = "user",
        session_id: str | None = None,
        event_type: str | None = None,
        from_version: int | None = None,
        snapshot: bool = False,
    ) -> Artifact:
        """Update an artifact in place. Content writes always update the live
        state (source_path on disk for file-backed artifacts, current.html
        for chat-backed). When ``snapshot`` is True the new state is also
        captured as a numbered version with a lifecycle event. When False
        (the default), the save is silent — the version dropdown stays the
        same and no event is emitted (Mesh-1654 round 5: explicit-snapshot
        model, requested by nrb).

        ``actor`` distinguishes lifecycle event types when a snapshot is
        taken: ``"user"`` (default) emits an ``edited`` event; ``"agent"``
        emits ``iterated``. ``session_id`` is captured on the event so the
        activity timeline can deep-link back to the originating chat.

        ``event_type`` overrides the actor-based default — used by the
        revert flow to mark events as ``reverted`` even though the actor is
        ``user``. Must be in :data:`ALLOWED_EVENT_TYPES` if provided.
        ``from_version`` is recorded on ``reverted`` events so the timeline
        can show "Reverted to vN".
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
            changed_content = False
            name_changed = False
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
            art.updated_at = _now_iso()

            # Snapshot of current live state (no new content provided).
            # Mesh-1654 round 6: the user can click Snapshot at any time
            # to capture the live state — including after silent saves OR
            # after the source file changed externally for file-backed
            # artifacts. We read live content the same way get() does and
            # then fall through to the changed_content branch below, which
            # handles writing current.html, source_path, and the version
            # snapshot uniformly.
            if snapshot and not changed_content:
                if art.source_path:
                    live = self._try_read_source_path(art.source_path)
                    if live is not None:
                        art.content = live
                    else:
                        art.content = self._read_text(self._artifact_dir(slug) / "current.html")
                else:
                    art.content = self._read_text(self._artifact_dir(slug) / "current.html")
                changed_content = True  # treat as a change for the snapshot path

            if changed_content:
                # Always update the live state — current.html for chat-backed
                # artifacts, plus source_path on disk for file-backed (so
                # MarkdownPanel and the artifact viewer stay in sync).
                # Use art.content (not the local ``content`` arg) because the
                # snapshot-without-content path sets art.content from disk
                # without populating ``content``.
                live_content = art.content or ""
                prev = self._artifact_dir(slug) / "current.html"
                self._write_text(prev, live_content)
                if art.source_path:
                    self._try_write_source_path(art.source_path, live_content)

                if snapshot:
                    # Validate event_type BEFORE side effects (AutoSDE round
                    # 13). Otherwise an invalid event_type raises after the
                    # version bump and versions/v{N}.html write, leaving an
                    # orphaned file on disk because _write_meta is never
                    # reached. Validate first; commit second.
                    if event_type is not None and event_type not in ALLOWED_EVENT_TYPES:
                        raise ArtifactValidationError(
                            f"invalid event type {event_type!r}: "
                            f"must be one of {sorted(ALLOWED_EVENT_TYPES)}"
                        )
                    # Bump version + capture the new state under
                    # versions/v{N}.html so it's preserved in history.
                    art.version += 1
                    self._snapshot_version(slug, art.version, prev)
                    # Lifecycle event. Caller-specified event_type wins
                    # (revert flow uses 'reverted'); otherwise actor-based
                    # default: agent → iterated, user → edited.
                    resolved_event_type = (
                        event_type
                        if event_type is not None
                        else ("iterated" if actor == "agent" else "edited")
                    )
                    self._append_event(
                        art,
                        type=resolved_event_type,
                        by=actor,
                        session_id=session_id,
                        version=art.version,
                        from_version=from_version,
                    )

            self._write_meta(art)
            self._prune_versions(slug)
            logger.info(
                "artifact updated: slug=%s version=%s changed_content=%s snapshot=%s",
                slug,
                art.version,
                changed_content,
                snapshot,
            )
            fire_upsert = changed_content
            fire_rename = name_changed and not changed_content
        # A content change is worth re-ingesting; a metadata-only rename just
        # refreshes the KB group label (no chunk churn). Description/tag-only
        # updates fire nothing. Fire outside the lock so the listener may call
        # get().
        if fire_upsert:
            self._fire_change("upsert", slug)
        elif fire_rename:
            self._fire_change("rename", slug)
        return art

    def set_folder(self, slug: str, folder_id: str) -> Artifact:
        """Move an artifact into a folder (Mesh-2720) — metadata-only.

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
            logger.info("artifact folder set: slug=%s folder_id=%s", slug, art.folder_id or "(root)")
            return art

    def delete(self, slug: str) -> None:
        """Permanently delete an artifact and all of its versions."""
        slug = _validate_slug(slug)
        with self._lock:
            adir = self._artifact_dir(slug)
            if not adir.exists():
                raise ArtifactNotFoundError(f"artifact not found: {slug}")
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
        rendered (Mesh-1715 follow-up). The activity timeline groups
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
    ) -> _List[Artifact]:
        """List all artifacts matching the given filters (sorted newest first).

        The lock is held only long enough to snapshot the artifact-directory
        listing; meta.json reads happen outside the lock so concurrent
        ``create()`` / ``update()`` / ``delete()`` calls don't block behind
        an O(N) filesystem scan. Atomic meta.json writes (tmp + rename) make
        unlocked reads safe — the worst case is a stale-but-valid snapshot
        for an artifact that was just renamed.
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
                json.JSONDecodeError,
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
            results.append(art)
        results.sort(key=lambda a: a.updated_at, reverse=True)
        return results

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
                json.JSONDecodeError,
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

    def _write_artifact(self, art: Artifact, content: str) -> None:
        adir = self._artifact_dir(art.slug)
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "versions").mkdir(parents=True, exist_ok=True)
        self._write_text(adir / "current.html", content)
        self._snapshot_version(art.slug, art.version, adir / "current.html")
        self._write_meta(art)

    def _snapshot_version(self, slug: str, version: int, src: Path) -> None:
        target = self._artifact_dir(slug) / "versions" / f"v{version}.html"
        # Defense in depth: route the read through the gated helper so the
        # is_sensitive_path() check fires on every filesystem read, even when
        # ``src`` is a store-internal path constructed by the store itself.
        # AUTOSDE security-controls rule: "all file reads must go through
        # hooks.py which enforces is_sensitive_path()".
        self._write_text(target, self._read_text(src))

    def _write_meta(self, art: Artifact) -> None:
        path = self._artifact_dir(art.slug) / "meta.json"
        # ``content`` is never persisted in meta.json — it lives in current.html.
        # ``live_dirty`` is a transient GET-time field — persist=True strips it.
        self._write_text(path, json.dumps(art.to_dict(persist=True), indent=2, sort_keys=True))

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
        """Append a lifecycle entry to ``art.events`` (FIFO-capped).

        Caller is responsible for persisting via ``_write_meta``. Events are
        validated lightly — unknown ``type`` strings are rejected so callers
        can't poison the audit trail with arbitrary text. The ``by`` field
        is a free-form string ('user' / 'agent' / cron job name); ``version``
        is the artifact version that was active immediately AFTER the event
        (for ``edited``/``iterated`` events that bump version, the new
        post-bump value); ``from_version`` is set on ``reverted`` events to
        record which historical version the new state was copied from;
        ``metadata`` carries event-type-specific extras (e.g.
        ``referenced`` events store ``message_ts`` and ``widget_index`` so
        the activity timeline can group multiple impressions by chat
        location).
        """
        if type not in ALLOWED_EVENT_TYPES:
            raise ArtifactValidationError(
                f"invalid event type {type!r}: must be one of {sorted(ALLOWED_EVENT_TYPES)}"
            )
        entry: dict[str, Any] = {"ts": _now_iso(), "type": type}
        if by:
            entry["by"] = str(by)[:64]
        if session_id:
            entry["session_id"] = str(session_id)[:128]
        if version is not None:
            entry["version"] = int(version)
        if from_version is not None:
            entry["from_version"] = int(from_version)
        if metadata:
            # Defense-in-depth: accept only string keys + simple scalar
            # values, cap each value at 256 chars. Prevents callers from
            # smuggling unbounded blobs into meta.json (which is read on
            # every artifact GET) or nested structures that would defeat
            # the activity timeline UI's flat-rendering assumption.
            cleaned: dict[str, Any] = {}
            for k, v in metadata.items():
                if not isinstance(k, str) or len(cleaned) >= 8:
                    continue
                if isinstance(v, (str, int, float, bool)) or v is None:
                    cleaned[k[:64]] = v[:256] if isinstance(v, str) else v
            if cleaned:
                entry["metadata"] = cleaned
        art.events.append(entry)
        # Cap the audit log so meta.json stays bounded — drop oldest first.
        if len(art.events) > MAX_EVENTS_PER_ARTIFACT:
            del art.events[: len(art.events) - MAX_EVENTS_PER_ARTIFACT]

    def _backfill_events(self, art: Artifact) -> bool:
        """Synthesize lifecycle events for legacy artifacts that pre-date the
        events field. Idempotent — sets ``events_backfilled=True`` so we
        only run once per artifact. Returns True if mutated.

        Generates a synthetic ``created`` event from ``created_at`` and one
        ``edited`` event per intermediate version (created_at → updated_at
        gap counts as a single edit if version > 1; we don't have per-version
        timestamps in legacy meta).
        """
        if art.events_backfilled or art.events:
            # Either explicitly backfilled before, or a fresh artifact whose
            # events were tracked from the start — nothing to do.
            return False
        if art.created_at:
            art.events.append(
                {
                    "ts": art.created_at,
                    "type": "created",
                    "by": art.source if art.source != "chat" else "agent",
                    "version": 1,
                }
            )
        if art.version > 1 and art.updated_at and art.updated_at != art.created_at:
            # We can't reconstruct per-version timestamps; collapse the gap
            # into a single edited event at updated_at.
            art.events.append(
                {
                    "ts": art.updated_at,
                    "type": "edited",
                    "by": "unknown",
                    "version": art.version,
                }
            )
        art.events_backfilled = True
        return True

    def _load_meta(self, slug: str) -> Artifact:
        path = self._artifact_dir(slug) / "meta.json"
        if not path.exists():
            raise ArtifactNotFoundError(f"artifact not found: {slug}")
        return self._read_meta_file(path)

    def _read_meta_file(self, path: Path) -> Artifact:
        raw = json.loads(self._read_text(path))
        # Tolerant load: ignore unknown keys, fill defaults for missing keys.
        slug = raw.get("slug")
        if not slug:
            raise ArtifactError(f"meta.json missing slug: {path}")
        # Events: lifecycle audit log. Tolerate older meta.json files written
        # before the field existed (Mesh-1654 Phase 5) — they get an empty
        # list and pick up a synthetic backfilled history on next get().
        raw_events = raw.get("events", []) or []
        events: _List[dict] = []
        if isinstance(raw_events, list):
            for ev in raw_events:
                if isinstance(ev, dict):
                    events.append(dict(ev))
        return Artifact(
            slug=str(slug),
            name=str(raw.get("name", slug)),
            kind=str(raw.get("kind", "widget")),
            source=str(raw.get("source", "chat")),
            description=str(raw.get("description", "")),
            tags=list(raw.get("tags", []) or []),
            version=int(raw.get("version", 1)),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            events=events,
            events_backfilled=bool(raw.get("events_backfilled", False)),
            source_path=str(raw.get("source_path", "")),
            folder_id=str(raw.get("folder_id") or ""),
        )

    def _read_text(self, path: Path) -> str:
        resolved = Path(os.path.realpath(path))
        if is_sensitive_path(str(resolved)):
            raise ArtifactError(f"refusing to read sensitive path: {resolved}")
        return resolved.read_text(encoding="utf-8")

    def _write_text(self, path: Path, text: str) -> None:
        resolved = Path(os.path.realpath(path))
        if is_sensitive_path(str(resolved)):
            raise ArtifactError(f"refusing to write sensitive path: {resolved}")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: tmp file + rename.
        tmp = resolved.with_suffix(resolved.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
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
        # Stdlib-only recursive delete (we don't depend on shutil here for clarity).
        for sub in sorted(path.rglob("*"), key=lambda p: -len(str(p))):
            try:
                if sub.is_file() or sub.is_symlink():
                    sub.unlink()
                elif sub.is_dir():
                    sub.rmdir()
            except OSError as exc:  # pragma: no cover — best-effort cleanup
                logger.warning("rmtree partial failure at %s: %s", sub, exc)
        path.rmdir()


# ── Artifact folders (Mesh-2720) ────────────────────────────────────────────

#: Human-path separator for folder addressing at the MCP/API boundary
#: (e.g. ``"Opportunity Planner/Reports"``). Distinct from the display
#: breadcrumb separator (`` › ``); paths are never stored on artifacts.
FOLDER_PATH_SEP = "/"
#: Cap on folder-tree nesting depth (mirrors the chat-folder tree guard).
MAX_FOLDER_DEPTH = 20


class ArtifactFolderStore:
    """Filesystem-backed store for the nested artifact-library folder tree.

    Mirrors the chat-folder subsystem (``dashboard/state.py`` folders): a flat
    JSON list persisted to ``artifact_folders.json`` in the config dir, with
    nesting expressed via ``parent_id`` pointers rather than nested storage or
    path strings. Membership lives on the artifact record (``Artifact.folder_id``),
    not here. Thread-safe via a coarse lock.

    A folder record is ``{id, name, order, parent_id, icon}``:

    * ``id`` — opaque 12-char hex (rename-safe handle).
    * ``name`` — display name (<= 100 chars).
    * ``order`` — sort order among siblings.
    * ``parent_id`` — ``""`` = root; a dangling id is tolerated as root.
    * ``icon`` — optional single-emoji glyph (may be absent).
    * ``color`` — optional ``#rrggbb`` display color (may be absent).
    """

    _FILE = "artifact_folders.json"

    def __init__(self, path: Path | None = None) -> None:
        self._path = (path or (config_dir() / self._FILE)).expanduser()
        self._lock = threading.Lock()
        self._folders: _List[dict[str, Any]] = []
        self._load()

    # ── persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    # Drop entries lacking an id (legacy/corrupt) so downstream
                    # walks never KeyError on a missing "id".
                    self._folders = [
                        f for f in raw if isinstance(f, dict) and f.get("id")
                    ]
        except Exception:  # noqa: BLE001 — a corrupt file must not crash boot
            logger.warning("Failed to load artifact folders", exc_info=True)

    def _save(self) -> None:
        """Atomic JSON write (tmp + rename), mirroring state persistence."""
        payload = json.dumps(self._folders, indent=2, sort_keys=True).encode()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, str(self._path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ── internal helpers (call under lock) ────────────────────────────────

    def _by_id(self) -> dict[str, dict[str, Any]]:
        return {f["id"]: f for f in self._folders if isinstance(f, dict) and f.get("id")}

    def _depth(self, folder_id: str) -> int:
        """Number of ancestors above ``folder_id`` (root folder = 0). Cycle-safe."""
        by_id = self._by_id()
        depth = 0
        seen: set[str] = set()
        fid = str(by_id.get(folder_id, {}).get("parent_id") or "")
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            depth += 1
            fid = str(by_id[fid].get("parent_id") or "")
        return depth

    def _subtree_ids(self, folder_id: str) -> set[str]:
        """All folder ids in the subtree rooted at ``folder_id`` (inclusive)."""
        by_id = self._by_id()
        if folder_id not in by_id:
            return set()
        out: set[str] = set()
        stack = [folder_id]
        while stack:
            cur = stack.pop()
            if cur in out:
                continue
            out.add(cur)
            for f in self._folders:
                if str(f.get("parent_id") or "") == cur and f["id"] not in out:
                    stack.append(f["id"])
        return out

    @staticmethod
    def _clean_name(name: Any) -> str:
        cleaned = str(name or "").strip()[:100]
        if not cleaned:
            raise ArtifactValidationError("folder name required")
        return cleaned

    # ── public API ────────────────────────────────────────────────────────

    def list(self) -> _List[dict[str, Any]]:
        """Return a copy of the folder records (sorted by depth-then-order)."""
        with self._lock:
            return [dict(f) for f in self._ordered()]

    def _ordered(self) -> _List[dict[str, Any]]:
        # Stable pre-order-ish ordering: siblings by (order, name); the frontend
        # does its own tree flatten, so this is a convenience for CLI/MCP output.
        return sorted(
            self._folders,
            key=lambda f: (self._depth(f["id"]), int(f.get("order", 0)), str(f.get("name", ""))),
        )

    def get(self, folder_id: str) -> dict[str, Any] | None:
        with self._lock:
            f = self._by_id().get(folder_id)
            return dict(f) if f else None

    def exists(self, folder_id: str) -> bool:
        if not folder_id:
            return False
        with self._lock:
            return folder_id in self._by_id()

    def create(self, name: str, parent_id: str = "", color: str = "") -> dict[str, Any]:
        """Create a folder under ``parent_id`` (``""`` = root)."""
        name = self._clean_name(name)
        color = self._clean_color(color)
        with self._lock:
            parent_id = str(parent_id or "")
            by_id = self._by_id()
            if parent_id and parent_id not in by_id:
                raise ArtifactValidationError(f"parent folder not found: {parent_id}")
            if parent_id and self._depth(parent_id) + 1 >= MAX_FOLDER_DEPTH:
                raise ArtifactValidationError(
                    f"folder nesting exceeds max depth {MAX_FOLDER_DEPTH}"
                )
            folder = {
                "id": uuid.uuid4().hex[:12],
                "name": name,
                "order": len(self._folders),
                "parent_id": parent_id,
            }
            if color:
                folder["color"] = color
            self._folders.append(folder)
            self._save()
            logger.info("artifact folder created: id=%s name=%s parent=%s", folder["id"], name, parent_id or "(root)")
            return dict(folder)

    def rename(self, folder_id: str, name: str) -> dict[str, Any]:
        name = self._clean_name(name)
        with self._lock:
            folder = self._by_id().get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            folder["name"] = name
            self._save()
            return dict(folder)

    def reparent(self, folder_id: str, new_parent: str = "") -> dict[str, Any]:
        """Move a folder under ``new_parent`` (``""`` = root). Cycle-guarded."""
        with self._lock:
            by_id = self._by_id()
            folder = by_id.get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            new_parent = str(new_parent or "")
            if new_parent:
                if new_parent not in by_id:
                    raise ArtifactValidationError(f"parent folder not found: {new_parent}")
                subtree = self._subtree_ids(folder_id)
                # Cycle guard: a folder may not become its own descendant.
                if new_parent in subtree:
                    raise ArtifactValidationError(
                        "cannot move a folder into itself or one of its descendants"
                    )
                # Depth guard: the deepest node in the moved subtree must still
                # fit under MAX_FOLDER_DEPTH at the destination — otherwise two
                # shallow subtrees could be merged past the limit that create()
                # and resolve_path() enforce.
                base_depth = self._depth(folder_id)
                subtree_height = max(self._depth(sid) for sid in subtree) - base_depth
                if self._depth(new_parent) + 1 + subtree_height >= MAX_FOLDER_DEPTH:
                    raise ArtifactValidationError(
                        f"folder nesting exceeds max depth {MAX_FOLDER_DEPTH}"
                    )
            folder["parent_id"] = new_parent
            self._save()
            return dict(folder)

    def set_icon(self, folder_id: str, icon: str) -> dict[str, Any]:
        with self._lock:
            folder = self._by_id().get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            folder["icon"] = str(icon or "")[:16]
            self._save()
            return dict(folder)

    @staticmethod
    def _clean_color(color: str) -> str:
        """Validate a folder color: ``""`` (none) or a ``#rrggbb`` hex value."""
        color = str(color or "").strip()
        if color and not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ArtifactValidationError("folder color must be a #rrggbb hex value")
        return color.lower()

    def set_color(self, folder_id: str, color: str) -> dict[str, Any]:
        """Set (or clear, with ``""``) the folder's display color."""
        color = self._clean_color(color)
        with self._lock:
            folder = self._by_id().get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            if color:
                folder["color"] = color
            else:
                folder.pop("color", None)
            self._save()
            return dict(folder)

    def reorder(self, orders: _List[dict[str, Any]]) -> None:
        """Apply ``[{id, order}, ...]`` sibling ordering (minimal patch)."""
        with self._lock:
            by_id = self._by_id()
            for entry in orders:
                if not isinstance(entry, dict):
                    continue
                fid = entry.get("id")
                if fid in by_id and "order" in entry:
                    by_id[fid]["order"] = int(entry["order"])
            self._save()

    def resolve_path(self, ref: str, *, create_missing: bool = False) -> str:
        """Resolve a folder id OR a human path to a folder id.

        ``ref`` may be:

        * ``""`` / ``None`` / ``"root"`` (case-insensitive) → ``""`` (root).
        * an existing folder id → returned as-is.
        * a ``/``-separated path (``"A/B/C"``) → walked segment-by-segment
          from the root by (case-insensitive) name. When ``create_missing``
          is True, missing segments are created (``mkdir -p`` semantics);
          otherwise a missing segment raises :class:`ArtifactNotFoundError`.

        Returns the resolved leaf folder id (``""`` for root).
        """
        ref = str(ref or "").strip()
        if not ref or ref.lower() == "root":
            return ""
        with self._lock:
            by_id = self._by_id()
            if ref in by_id:
                return ref
            segments = [s.strip() for s in ref.split(FOLDER_PATH_SEP) if s.strip()]
            if not segments:
                return ""
            parent = ""
            # All-or-nothing: if a later segment fails the depth guard (or a
            # non-create lookup misses), roll back any folders appended during
            # this attempt so in-memory state never drifts from disk.
            snapshot_len = len(self._folders)
            created_any = False
            try:
                for seg in segments:
                    # Normalize through the SAME truncation _clean_name applies
                    # on create, so a >100-char segment matches the folder it
                    # created on a prior call (lookup key == stored name) —
                    # otherwise repeated resolves would mint duplicates.
                    seg_norm = self._clean_name(seg).lower()
                    match = next(
                        (
                            f
                            for f in self._folders
                            if str(f.get("parent_id") or "") == parent
                            and str(f.get("name", "")).strip().lower() == seg_norm
                        ),
                        None,
                    )
                    if match is None:
                        if not create_missing:
                            raise ArtifactNotFoundError(f"folder path not found: {ref}")
                        if parent and self._depth(parent) + 1 >= MAX_FOLDER_DEPTH:
                            raise ArtifactValidationError(
                                f"folder nesting exceeds max depth {MAX_FOLDER_DEPTH}"
                            )
                        match = {
                            "id": uuid.uuid4().hex[:12],
                            "name": self._clean_name(seg),
                            "order": len(self._folders),
                            "parent_id": parent,
                        }
                        self._folders.append(match)
                        created_any = True
                    parent = match["id"]
            except (ArtifactValidationError, ArtifactNotFoundError):
                # Discard folders appended during this failed attempt.
                del self._folders[snapshot_len:]
                raise
            if created_any:
                self._save()
            return parent

    def breadcrumb(self, folder_id: str, sep: str = FOLDER_PATH_SEP) -> str:
        """Render a folder's ancestry root→leaf as a ``sep``-joined path."""
        if not folder_id:
            return ""
        with self._lock:
            by_id = self._by_id()
            names: _List[str] = []
            seen: set[str] = set()
            fid = folder_id
            while fid and fid in by_id and fid not in seen:
                seen.add(fid)
                names.append(str(by_id[fid].get("name", "")))
                fid = str(by_id[fid].get("parent_id") or "")
            names.reverse()
            return sep.join(n for n in names if n)

    def item_counts(self, artifact_store: "ArtifactStore") -> dict[str, int]:
        """Direct-artifact count per folder id (unfiled excluded)."""
        counts: dict[str, int] = {}
        for art in artifact_store.list():
            fid = getattr(art, "folder_id", "") or ""
            if fid:
                counts[fid] = counts.get(fid, 0) + 1
        return counts

    def list_with_counts(self, artifact_store: "ArtifactStore") -> _List[dict[str, Any]]:
        """Folders enriched with a computed, non-persisted ``item_count``."""
        counts = self.item_counts(artifact_store)
        return [{**f, "item_count": counts.get(f["id"], 0)} for f in self.list()]

    def delete(
        self,
        folder_id: str,
        *,
        delete_contents: bool,
        artifact_store: "ArtifactStore",
    ) -> dict[str, Any]:
        """Delete a folder. ``delete_contents`` picks the semantics:

        * **False (safe, default)** — delete only this folder; re-parent its
          direct child folders and artifacts to this folder's parent (root if
          none). Descendant folders travel up with their re-parented parent.
        * **True (cascade)** — permanently delete the whole subtree: every
          descendant artifact (via the guarded :meth:`ArtifactStore.delete`)
          and every descendant folder.

        Returns a summary dict describing what changed.
        """
        # Phase 1 (under folder lock): mutate the folder tree, decide the
        # affected id set. Phase 2 (outside the lock): touch the artifact
        # store, which has its own independent lock.
        with self._lock:
            by_id = self._by_id()
            folder = by_id.get(folder_id)
            if folder is None:
                raise ArtifactNotFoundError(f"folder not found: {folder_id}")
            parent = str(folder.get("parent_id") or "")
            if delete_contents:
                affected_ids = self._subtree_ids(folder_id)
            else:
                affected_ids = {folder_id}
                # Re-parent direct child folders up to this folder's parent.
                for f in self._folders:
                    if str(f.get("parent_id") or "") == folder_id:
                        f["parent_id"] = parent
            self._folders = [f for f in self._folders if f.get("id") not in affected_ids]
            self._save()

        # Phase 2: artifacts. ``affected_ids`` is the subtree for cascade, or
        # just the single folder for the safe path.
        #
        # Race window (accepted): Phase 2 runs OUTSIDE the folder lock (the
        # artifact store has its own independent lock, and holding both would
        # invite ordering deadlocks). Between Phase 1 removing the folder and
        # this scan re-parenting/deleting its artifacts, a concurrent
        # ``ArtifactStore.set_folder()`` could file an artifact into the
        # just-deleted folder id. Such an artifact simply ends up with a
        # dangling ``folder_id``, which every reader already tolerates by
        # degrading it to Unfiled (see ``list(folder=)`` and the tree view's
        # dangling-id handling). Acceptable for a single-user local tool; not
        # worth cross-lock coordination.
        deleted_slugs: _List[str] = []
        reparented_slugs: _List[str] = []
        for art in artifact_store.list():
            fid = getattr(art, "folder_id", "") or ""
            if fid not in affected_ids:
                continue
            if delete_contents:
                try:
                    artifact_store.delete(art.slug)
                    deleted_slugs.append(art.slug)
                except ArtifactError as exc:  # pragma: no cover — best-effort
                    logger.warning("cascade delete failed for %s: %s", art.slug, exc)
            else:
                artifact_store.set_folder(art.slug, parent)
                reparented_slugs.append(art.slug)
        logger.info(
            "artifact folder deleted: id=%s cascade=%s folders=%d artifacts=%d",
            folder_id,
            delete_contents,
            len(affected_ids),
            len(deleted_slugs) if delete_contents else len(reparented_slugs),
        )
        return {
            "deleted_folder_ids": sorted(affected_ids),
            "deleted_artifact_slugs": deleted_slugs,
            "reparented_artifact_slugs": reparented_slugs,
            "reparented_to": parent,
            "delete_contents": delete_contents,
        }


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


def reset_default_store() -> None:
    """Drop the cached default store (test-only helper)."""
    global _default_store
    with _default_store_lock:
        _default_store = None


_default_folder_store: "ArtifactFolderStore | None" = None
_default_folder_store_lock = threading.Lock()


def get_default_folder_store() -> "ArtifactFolderStore":
    """Return the process-wide default artifact-folder store (lazy-initialized)."""
    global _default_folder_store
    with _default_folder_store_lock:
        if _default_folder_store is None:
            _default_folder_store = ArtifactFolderStore()
        return _default_folder_store


def reset_default_folder_store() -> None:
    """Drop the cached default folder store (test-only helper)."""
    global _default_folder_store
    with _default_folder_store_lock:
        _default_folder_store = None
