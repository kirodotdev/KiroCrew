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
from dataclasses import fields as fields_of
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from typing import List as _List

from kiro_crew import hooks
from kiro_crew.artifact_source import is_verifiable_root
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.deploy.webapp_types import (  # noqa: F401 — re-export for API compatibility
    WebAppArchitecture,
    WebAppCost,
    WebAppDeployTarget,
    WebAppLifecycle,
    WebAppMetadata,
    WebAppTeardown,
    webapp_metadata_from_dict,
)
from kiro_crew.publish_provider import DEFAULT_PROVIDER
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

#: Maximum number of versions retained per artifact (older versions are pruned
#: when the cap is exceeded).
MAX_VERSIONS = 50

#: Maximum size of an artifact's content blob, in bytes. Locally-authored
#: widget HTML rarely tops a few KB, but cloned/pulled remote artifacts (rich
#: HTML reports, CSVs) routinely exceed 1 MiB — at 1 MiB clone/pull would
#: silently fail on exactly the shared-HTML artifacts bidirectional sync
#: targets. 25 MiB is large enough to bring those down locally while still
#: refusing truly unbounded content. Keep in lockstep with
#: ``validation.ARTIFACT_CONTENT_MAX`` (the MCP tool-arg cap) so a save's limit
#: doesn't depend on its entry path — guarded by a regression test.
MAX_CONTENT_BYTES = 26_214_400  # 25 MiB

#: Maximum length of human-readable name / description fields.
MAX_NAME_LEN = 200
MAX_DESCRIPTION_LEN = 2_000

#: Maximum length of a file-backed artifact's ``source_path`` / ``source_root``.
#: These are REJECTED past the cap rather than truncated: a truncated path is a
#: different path — usually a nonexistent one — so silently storing it produced
#: an artifact whose live pointer could never resolve, which is precisely the
#: dead-pointer failure ``source_missing`` exists to expose. Comfortably above
#: PATH_MAX on the platforms we support for any realistic project layout.
MAX_SOURCE_PATH_LEN = 512

#: Retention cap for auto-registered widget artifacts (see
#: :mod:`kiro_crew.widget_artifacts`). Every chat-emitted ``<mcwidget>`` becomes
#: an artifact automatically, so without a cap a chat-heavy user accumulates one
#: three-file directory per throwaway widget forever and every library listing
#: is an O(N) scan over them. Past this many, the oldest STILL-UNPINNED
#: auto-registered widgets are deleted; pinning one exempts it permanently. Sized
#: so a long working session's widgets all remain addressable for later
#: iteration while the tail is reclaimed.
MAX_AUTO_WIDGET_ARTIFACTS = 200

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
        "image",  # generated image (source_path points to PNG/JPEG)
        "webapp",  # a deployed web application; rendered as an infra control card
    }
)

#: Allowed source markers (provenance).
ALLOWED_SOURCES = frozenset(
    {
        # Provenance/creation buckets (back-compat) + actual session origins from
        # ``infer_use_case`` so an artifact's source can reflect WHERE the saving
        # session came from rather than a generic "manual".
        "chat",
        "cron",
        "subagent",
        "manual",
        "import",
        "dashboard",
        "slack",
        "cli",
        "task-runner",
        "unknown",
    }
)

#: Allowed lifecycle event types. ``referenced`` is reserved for chat-mention
#: scanning (added in a follow-up CR); the in-line save/update path emits
#: ``created`` / ``edited`` / ``iterated`` / ``reverted``.
ALLOWED_EVENT_TYPES = frozenset(
    {"created", "edited", "iterated", "referenced", "reverted", "comment"}
)

#: Max lifecycle events retained per artifact. FIFO eviction keeps meta.json
#: bounded — at ~150 bytes per event entry, this caps each meta file at
#: roughly 75KB on top of the static metadata, which is well within the
#: tolerable read cost.
MAX_EVENTS_PER_ARTIFACT = 500

#: Maximum number of comments retained per artifact (FIFO, like versions/events).
#: ``add_comment`` does load->append->rewrite of the whole comments.json, so an
#: unbounded agent loop (``artifact_post_comment``) would be O(N^2) I/O and grow
#: the sidecar without limit. When the cap is exceeded the OLDEST full thread(s)
#: are dropped (a thread root + its replies together), never a reply orphaned.
MAX_COMMENTS_PER_ARTIFACT = 500

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
class ForkMetadata:
    """Provenance tracking for an artifact forked from the remote store.

    Present (non-``None`` on :class:`Artifact`) once an artifact has been
    forked from a remote artifact. Records the upstream identity
    so pull-latest and upstream linking work.
    """

    upstream_artifact_id: str = ""  # provider-native id of the source
    upstream_url: str = ""  # stable view URL of the source
    upstream_owner: str = ""  # ownerAlias at fork time
    upstream_version: int = 0  # versionNumber at fork/last-pull time
    forked_at: str = ""  # ISO timestamp of initial fork
    # Publish provider the fork originated from. Empty on records written before
    # multi-provider support; readers fall back to DEFAULT_PROVIDER so legacy
    # single-provider forks keep resolving their origin correctly.
    upstream_provider: str = ""


@dataclass
class ArtifactPublication:
    """Publication state for an artifact.

    Present (non-``None`` on :class:`Artifact`) once an artifact has been
    published to the remote store. The ``artifact_id`` and ``view_url`` are
    *stable* across every version — pushing a new version reuses the same id
    and URL. This block holds only data; all networking lives in the
    publish-sync / publish-client layer (mirrors the module-purity
    rule documented at the top of this file).

    ``last_pushed_sha256`` is the optimistic-concurrency guard required by
    the provider's ``upload_artifact_version`` contract: it is the sha of the
    artifact's current latest remote version. On a version push we pass it
    as ``expectedCurrentSha256``; a mismatch means the remote artifact was
    changed out-of-band, which we surface via ``last_error`` rather than
    force-pushing over.
    """

    artifact_id: str  # provider UUID — STABLE across versions
    view_url: str  # https://.../artifact/<uuid> — STABLE across versions
    provider: str = DEFAULT_PROVIDER  # publish destination (PublishProvider name)
    visibility: str = "PRIVATE"  # PRIVATE | SHARED | PUBLIC
    shared_with: _List[str] = field(default_factory=list)  # aliases (role EDITOR)
    auto_sync: bool = True  # push a new remote version on every KiroCrew bump
    #: Sync authority for the bound provider: ``"mirror"`` (KiroCrew is the sole
    #: writer; remote is a guarded/blind mirror — a mirror provider) or
    #: ``"live"`` (the remote owns a live CRDT; KiroCrew is a participant —
    #: a live CRDT provider). Derived from ``provider.sync_model().collab_mode`` at publish /
    #: clone time. Gates the push path (CRDT edit vs guarded blob replace) and
    #: the conflict/Force-push UI. Tolerant-loaded; legacy meta.json defaults to
    #: ``"mirror"``.
    collab_mode: str = "mirror"
    last_pushed_sha256: str = ""  # concurrency guard for the next version push
    last_synced_kirocrew_version: int = 0
    # Maps str(kirocrew_version) -> remote_version_number.
    version_map: dict[str, int] = field(default_factory=dict)
    published_at: str = ""
    published_by: str = ""  # gateway owner alias (ownerAlias from the remote store)
    last_error: str = ""  # conflict / sync-failure surfaced to the UI
    #: sha256 of the LIVE (CRDT) remote body as of the last sync (publish / push
    #: / pull / clone / overwrite). A live CRDT provider canonicalizes markdown on write, so
    #: drift is detected remote-vs-remote against this hash — snapshot_seq bumps
    #: on mere viewing and is unreliable. Empty for mirror providers / legacy.
    last_synced_remote_hash: str = ""


@dataclass
class ArtifactComment:
    """A durable comment on an artifact (the canonical store).

    Comments can be local-only (never leaves KiroCrew) or synced to/from a
    provider (the publishing provider). The ``origin`` + ``scope`` fields determine sync
    behavior.
    """

    id: str  # local uuid
    origin: str = "local"  # "local" | "<provider>:<remote_id>"
    provider: str | None = None  # "<provider>" | None (for local)
    scope: str = "private"  # "private" | "shared"
    author: str = ""  # Amazon alias
    is_agent: bool = False  # authored by an AI agent
    body: str = ""
    anchor_quote: str | None = None  # anchored text (portable key)
    anchor_prefix: str | None = None
    anchor_suffix: str | None = None
    anchor_start_offset: int | None = None
    anchor_end_offset: int | None = None
    anchor_version: int | None = None
    thread_id: str = ""  # root comment id (self for roots)
    parent_id: str | None = None  # parent comment id
    status: str = "open"  # "open" | "review" | "resolved"
    target_provider: str | None = None  # which publication to sync to
    target_external_id: str | None = None
    sync_state: str = "local_only"  # local_only|pending_push|synced|push_failed
    #: True when the anchored text (``anchor_quote``) can no longer be found in
    #: the artifact's content — set/cleared by the store's anchor rescan on
    #: every content write. A dedicated field (not a ``sync_state`` value)
    #: because ``sync_state`` tracks provider push status and the two signals
    #: must not clobber each other (a pending_push comment can also be
    #: orphaned).
    anchor_orphaned: bool = False
    created_at: str = ""
    updated_at: str = ""
    # Transient: set on inbound provider mirrors that came back as tombstones so
    # merge_remote_comments can drop the local copy. Never persisted.
    deleted: bool = False


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
    #: True when ``kind`` was assigned by the store rather than chosen by the
    #: caller. Set only for a document created BLANK — no content to sniff and
    #: no pinned kind — which is the library's "New artifact" action. While it
    #: stays true, every content write re-runs :func:`detect_editor_kind` so the
    #: document can settle into ``json`` / ``svg`` once the user has typed
    #: enough to tell what it is. Passing an explicit ``kind`` to
    #: :meth:`ArtifactStore.update` pins the kind and clears this flag.
    #: Tolerant-loaded: every artifact that predates the field defaults to
    #: ``False`` and is therefore never re-typed.
    kind_auto: bool = False
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
    #: re-saved (re-saving offers to bump the existing
    #: artifact's version rather than creating a parallel one).
    source_path: str = ""
    #: The validated directory that authorizes reads of ``source_path`` — the
    #: project root (or git repo root) a promoted file was linked from. Empty
    #: for chat-backed artifacts and for copied (snapshot) promotions.
    #:
    #: Recorded at CREATE time on purpose: :class:`ArtifactStore` re-validates
    #: root containment on every read, but it has no session handle then and so
    #: cannot ask "what project is open?". Without a recorded root, a linked
    #: file outside ``$HOME`` (e.g. ``/workplace/user/repo/doc.md``) is refused
    #: on read and the artifact silently serves its stale snapshot instead.
    #: Added to the allowed-roots set by :meth:`allowed_source_roots`; the
    #: sensitive-path denylist still applies inside it.
    source_root: str = ""
    #: True when ``source_path`` is PROVENANCE ONLY -- the artifact owns a COPY
    #: of the bytes, so reads never touch the file and content writes are never
    #: mirrored back to it.
    #:
    #: Two different questions were being collapsed into one, and that broke one
    #: of them. Dropping ``source_path`` on a copy (to avoid the dead-pointer
    #: bug, where a linked file outside the authorized root is refused and the
    #: stale snapshot is served as though healthy) also dropped the only key
    #: :meth:`find_by_source_path` dedups on, so promoting the same disposable
    #: file twice minted a duplicate artifact. Recording the path but gating
    #: liveness here keeps dedup identity AND real copy semantics.
    #:
    #: Defaults to False so every pre-existing ``source_path`` producer
    #: (``/materialize``, ``/relocate``, a direct ``store.create``) keeps
    #: today's live-pointer behaviour; only the copy verdict opts in.
    source_copy_only: bool = False
    #: Nested-folder membership. ``""`` = unfiled (library root).
    #: An opaque folder id (see :class:`ArtifactFolderStore`), never a path —
    #: so renaming a folder never rewrites artifact records. Setting it is a
    #: metadata-only mutation (:meth:`ArtifactStore.set_folder`) that does NOT
    #: bump the version. Tolerant-loaded for legacy meta.json (defaults to
    #: unfiled). Only local artifacts carry a folder; remote/shared artifacts
    #: have no local record to hang one on.
    folder_id: str = ""
    #: User "pin"/favorite mark (Mesh — artifact pinning). Metadata-only,
    #: persisted to meta.json; toggling it does NOT bump the version or emit a
    #: lifecycle event (same class as retag/rename/set_folder). Tolerant-loaded
    #: for legacy meta.json (defaults to unpinned). Lets the library UI filter
    #: to pinned-only vs all.
    pinned: bool = False
    #: Session key of the chat/session that saved this artifact (Mesh — artifact
    #: session provenance). Persisted; the dashboard live-resolves it to the
    #: session's current title for the Source column (falling back to
    #: "(deleted session)" when the session no longer exists). Empty for
    #: non-session origins (bulk import, older artifacts).
    session_key: str = ""
    #: True when the store created this record automatically from a chat-emitted
    #: ``<mcwidget>`` rather than from an explicit user/agent save. Marks it as
    #: sweepable by :meth:`ArtifactStore.prune_auto_widgets` while it stays
    #: unpinned; starring clears nothing but takes it out of the sweep (the
    #: sweep only considers unpinned records). Tolerant-loaded — every artifact
    #: that predates auto-registration defaults to ``False`` and is therefore
    #: never swept.
    auto_registered: bool = False
    #: Publication state. ``None`` until the artifact
    #: is published; carries the stable provider id/URL, visibility,
    #: shared-with aliases, and version-sync bookkeeping once published.
    #: Persisted in meta.json (nested object); tolerant-loaded so older
    #: meta.json files without the field default to ``None``.
    publication: "ArtifactPublication | None" = None
    #: Fork provenance. ``None`` until the artifact is forked
    #: from a remote artifact. Records the upstream identity so
    #: pull-latest and upstream linking work. Persisted in meta.json.
    fork_metadata: "ForkMetadata | None" = None
    #: Per-version render kind, mapping ``str(version) -> kind`` at the moment
    #: that version was snapshotted. ``kind`` is otherwise artifact-global, but
    #: pulling upstream-ahead content can flip a widget to ``html`` (the cloud
    #: bytes are an already-wrapped standalone document that can't be
    #: re-wrapped). Recording the kind per version means reverting to a pre-pull
    #: widget snapshot restores widget render-mode instead of the raw inner
    #: HTML. Tolerant-loaded; absent entries fall back to the current ``kind``.
    version_kinds: dict[str, str] = field(default_factory=dict)
    #: Computed at GET time: True when the current live content differs
    #: from the latest numbered snapshot. Lets the frontend enable the
    #: Snapshot button anytime live has drifted from history — including
    #: cases where a file-backed artifact's source changed externally
    #: between the last snapshot and now. Not persisted; set by ``get()``.
    live_dirty: bool = False
    #: Computed at GET time: True when this artifact has a ``source_path`` but
    #: the live read of it FAILED — the file was deleted or moved, is no longer
    #: readable, or resolves outside the roots that authorize it. The store
    #: falls back to the last snapshot in that case so the artifact stays
    #: viewable, which used to make a dead pointer indistinguishable from a
    #: healthy one (``live_dirty`` was computed against the fallback and so
    #: read "in sync"). This field is the signal that the pointer is dead.
    #: Not persisted; set by ``get()`` — same contract as ``live_dirty``.
    source_missing: bool = False
    #: Structured metadata for ``kind="webapp"`` artifacts — a deployed application
    #: (deploy target, architecture, lifecycle/TTL, cost estimate, teardown handle).
    #: ``None`` for every other kind. Tolerant-loaded from meta.json.
    webapp_metadata: "WebAppMetadata | None" = None

    def to_dict(self, *, include_content: bool = False, persist: bool = False) -> dict[str, Any]:
        """Render as a JSON-friendly dict, optionally including the content blob.

        ``persist=True`` strips fields that should never be written to
        meta.json (``live_dirty`` and ``source_missing`` — both are computed at
        GET time and persisting them would leave stale values lying around when
        the live state changes via a path the store didn't observe).
        """
        d = asdict(self)
        if not include_content:
            d.pop("content", None)
        if persist:
            # live_dirty is a transient, GET-time-computed
            # field. Persisting it via meta.json would create staleness
            # bugs (e.g. silent save flips it to True, but we'd write False
            # if the meta is touched again before a snapshot).
            d.pop("live_dirty", None)
            # source_missing is the same class of field: a stale "True" would
            # keep flagging a dead pointer after the file came back (and a
            # stale "False" would hide one that just died).
            d.pop("source_missing", None)
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


#: Kinds a HUMAN may select for an artifact from the dashboard's type control.
#: A strict subset of :data:`ALLOWED_KINDS`, and deliberately the same set the
#: frontend's ``isEditableKind`` treats as inline-editable: ``widget`` / ``html``
#: render in a sandboxed iframe with no editor, so offering them would let the
#: user strand the document they are typing in, and ``webapp`` carries deploy
#: metadata that a hand-flip would desynchronise. Agents and importers still
#: reach the full :data:`ALLOWED_KINDS` set; this bounds the UI surface only.
USER_SELECTABLE_KINDS = frozenset({"markdown", "text", "json", "svg"})


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

#: An ``<svg>`` document root, optionally preceded by an XML declaration and/or
#: an SVG doctype. Anchored so a stray ``<svg>`` in the middle of a markdown
#: document never re-types it.
_SVG_ROOT_RE = re.compile(
    r"^(?:<\?xml[^>]*\?>\s*|<!DOCTYPE\s+svg[^>]*>\s*)*<svg[\s>]",
    re.IGNORECASE,
)


def detect_editor_kind(content: str) -> str | None:
    """Detect the structured kind of hand-authored ``content``, or ``None``.

    Used by :meth:`ArtifactStore.update` to re-type a document that was created
    blank (``kind_auto``) once its content becomes recognizable. It only ever
    returns an **editable** kind — ``json`` or ``svg`` — and returns ``None``
    for anything it cannot positively identify.

    Two deliberate exclusions:

    * ``html`` / ``widget`` are never returned. Both render in a sandboxed
      iframe and are NOT inline-editable (see ``isEditableKind`` on the
      frontend), so promoting a document the user is *typing in* to either one
      would rip the editor away mid-sentence. Markdown passes raw HTML through
      to its renderer anyway, so a hand-written HTML document still renders
      while staying editable.
    * a bare JSON scalar (``42``, ``true``, a quoted string) is valid JSON but
      far more likely to be prose, so only a whole document parsing as an
      object or an array counts.

    Returning ``None`` rather than ``"markdown"`` for unrecognized content is
    what makes re-typing stable: a JSON document left mid-edit with a syntax
    error detects as nothing and keeps the kind it already had, instead of
    flapping back to markdown on every save.
    """
    sniff = (content or "").strip()
    if not sniff:
        return None
    if _SVG_ROOT_RE.match(sniff):
        return "svg"
    if sniff[0] in "{[":
        try:
            parsed = json.loads(sniff)
        except ValueError:
            return None
        if isinstance(parsed, (dict, list)):
            return "json"
    return None


#: Text document extensions used by the "session docs" feature (virtual All
#: list + materialize-on-save). Deliberately text-only: binary docs (.pdf,
#: .docx) don't fit the content-backed artifact model without extraction, so
#: they're excluded here for now.
DOC_EXTENSIONS = frozenset({".md", ".markdown", ".mdx", ".txt", ".rst"})


def is_document_path(path: str) -> bool:
    """True when ``path`` looks like a (text) document, not code/config/binary."""
    if not path:
        return False
    return os.path.splitext(path)[1].lower() in DOC_EXTENSIONS


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


def _markdown_misclassification_reason(content: str, source_path: str) -> str | None:
    """Return why a ``widget``-kind artifact actually looks like ``markdown``.

    Returns a short human-readable reason string, or ``None`` if the artifact
    is a genuine widget. Used by the corrective migration
    (:meth:`ArtifactStore.migrate_kinds`) to find artifacts saved as ``widget``
    before ``kind`` inference existed. Deliberately conservative — it triggers
    only on a ``.md`` / ``.markdown`` ``source_path`` or content with **no HTML
    tags at all**, so a genuine widget (whose content always contains HTML) is
    never reclassified. Only ever implies ``markdown``; the richer kinds are
    out of migration scope.
    """
    if source_path:
        ext = os.path.splitext(source_path)[1].lower()
        if ext in (".md", ".markdown"):
            return "source_path .md"
    if content and "<" not in content:
        return "no HTML tags"
    return None


def _validate_source(source: str) -> str:
    if source not in ALLOWED_SOURCES:
        raise ArtifactValidationError(
            f"invalid source {source!r}: must be one of {sorted(ALLOWED_SOURCES)}"
        )
    return source


def _strip_session_scope(key: str) -> str:
    """Canonicalize a session key so both spellings of ONE conversation match.

    The store's two provenance fields disagree by construction, and neither is
    wrong on its own:

    * ``Artifact.session_key`` is written from the dashboard's BARE slot key —
      ``chat-7-1785396512`` for a dashboard-born tab, ``slack_1785370133.085469``
      for a channel-born one — on the browser create path.
    * an event's ``session_id`` comes from the caller's FULL session key —
      ``dashboard:chat-7-1785396512``, or the channel's own
      ``slack:1785370133.085469`` — because MCP callers resolve identity through
      ``KIROCREW_SESSION_KEY`` / the signed pid sidecar, which are
      scope-qualified.

    A literal comparison therefore matches the origin case and *never* the event
    case — the involvement scope would silently degrade to the plain origin
    filter it exists to widen. Each family needs the fold that turns its session
    key into its slot name, because the slot name is what the origin field
    holds:

    * a dashboard slot's name is its session key minus the ``dashboard:`` scope.
    * a channel-born slot's name is its session key run through
      ``history._safe_key`` (see ``channel_slots.channel_slot_name``) — the same
      fold that names the transcript, which is why the tab and the thread share
      one file.

    The namespace itself is never dropped: ``slack:X`` canonicalizes to
    ``slack_X``, never to ``X``, so a channel conversation cannot collide with a
    dashboard slot. Keys with no second spelling to reconcile (``cron:``,
    ``subagent:``, ``taskrunner:``) are returned untouched.

    Both channel helpers are imported lazily: ``validation`` reads this module's
    ``MAX_CONTENT_BYTES`` at import time, so a top-level ``kiro_crew.messaging``
    import here closes a cycle through ``messaging.driver`` -> ``acp`` ->
    ``validation``.
    """
    prefix = "dashboard:"
    if key.startswith(prefix):
        return key[len(prefix):]
    from kiro_crew.history import _safe_key
    from kiro_crew.messaging.link import is_channel_session_key

    return _safe_key(key) if is_channel_session_key(key) else key


def _session_touched(art: "Artifact", session_key: str) -> bool:
    """True when ``session_key`` originated OR left any event on ``art``.

    "Touched" is the union of the two provenance records the store keeps:
    the create-time ``session_key`` field, and the ``session_id`` stamped on
    each lifecycle event (``created`` / ``edited`` / ``iterated`` /
    ``referenced`` / ``reverted``). That union is what makes the in-session
    Artifacts tab able to list what a session *consumed*, not only what it
    authored — a read records a ``referenced`` event and an agent edit records
    an ``iterated`` one, so both surface here without a second index.

    Both sides are compared scope-normalized (see ``_strip_session_scope``)
    because the two fields are persisted in different formats.

    Note the event log is FIFO-capped at ``MAX_EVENTS_PER_ARTIFACT``, so a
    very old association on a heavily-edited artifact can age out. That is
    acceptable for a recency-ordered panel and is why this is a best-effort
    "did this session touch it" predicate, not an audit-grade join.
    """
    if not session_key:
        return False
    want = _strip_session_scope(session_key)
    if art.session_key and _strip_session_scope(art.session_key) == want:
        return True
    return any(
        _strip_session_scope(str(e.get("session_id") or "")) == want
        for e in art.events
        if e.get("session_id")
    )


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


def _validate_source_path(value: str | None, field_name: str = "source_path") -> str:
    """Validate a filesystem-pointer field (``source_path`` / ``source_root``).

    REJECTS an over-long value instead of truncating it. Truncation used to be
    silent (``source_path[:512]``), which turned a too-long-but-valid path into
    a shorter path that points somewhere else — practically always somewhere
    that doesn't exist. The artifact then looked file-backed while its live read
    could never succeed. Failing the save is the honest outcome: the caller
    learns immediately instead of the user discovering a hollow artifact later.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ArtifactValidationError(f"{field_name} must be str, got {type(value).__name__}")
    if len(value) > MAX_SOURCE_PATH_LEN:
        raise ArtifactValidationError(
            f"{field_name} exceeds {MAX_SOURCE_PATH_LEN} chars "
            f"({len(value)}); refusing to truncate a filesystem path"
        )
    return value


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
        # remote pull/clone paths all funnel through this store in the
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
            # Lazy backfill: legacy artifacts written
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
                    # it: the fallback keeps the artifact viewable, which
                    # previously made a dead pointer look completely healthy.
                    meta.source_missing = True
                    meta.content = self._read_text(self._artifact_dir(slug) / "current.html")
            else:
                meta.content = self._read_text(self._artifact_dir(slug) / "current.html")
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
            # for invalid sequences). Previously called
            # p.read_text() which loads the entire file into memory before
            # the size check — a multi-GB file pointed to by source_path
            # would exhaust memory before truncation triggered. Bounding
            # the read caps memory at MAX_CONTENT_BYTES+1 regardless of
            # file size.
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
            if not hooks.safe_write_file_nolink(
                str(p), content, within_root=str(containing)
            ):
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
        """
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
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
                    if event_type is not None and event_type not in ALLOWED_EVENT_TYPES:
                        raise ArtifactValidationError(
                            f"invalid event type {event_type!r}: "
                            f"must be one of {sorted(ALLOWED_EVENT_TYPES)}"
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
        # ``list()`` sorts by ``updated_at`` alone, which is not a total order:
        # two widgets registered in the same microsecond tie-break by directory
        # scan order, making WHICH of them gets deleted nondeterministic. Re-sort
        # on ``(updated_at, slug)`` so the kept/dropped boundary is stable and
        # testable. Kept local to the sweep — ``list()``'s ordering is shared with
        # the library UI and is not this change's to redefine.
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
        results.sort(key=lambda a: a.updated_at, reverse=True)
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
                json.JSONDecodeError,
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
                json.JSONDecodeError,
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
                json.JSONDecodeError,
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
        _fork_field_types = {
            "upstream_artifact_id": str,
            "upstream_url": str,
            "upstream_owner": str,
            "upstream_version": int,
            "forked_at": str,
            "upstream_provider": str,
        }
        with self._lock:
            art = self._load_meta(slug)
            if art.fork_metadata is None:
                raise ArtifactError(f"artifact {slug!r} has no fork_metadata")
            for k, v in fields.items():
                if k not in _fork_field_types:
                    raise ArtifactError(f"ForkMetadata has no field {k!r}")
                expected = _fork_field_types[k]
                if not isinstance(v, expected):
                    raise ArtifactError(
                        f"ForkMetadata.{k} expects {expected.__name__}, got {type(v).__name__}"
                    )
                setattr(art.fork_metadata, k, v)
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

    def clear_publication(self, slug: str) -> Artifact:
        """Remove an artifact's publication block (after unpublish/delete)."""
        slug = _validate_slug(slug)
        with self._lock:
            art = self._load_meta(slug)
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
                comments = self._prune_oldest_threads(comments, keep_id=comment.id)
            self._write_comments(slug, comments)
            return comment

    @staticmethod
    def _prune_oldest_threads(
        comments: _List["ArtifactComment"], *, keep_id: str
    ) -> _List["ArtifactComment"]:
        """Drop whole oldest threads until <= MAX_COMMENTS_PER_ARTIFACT.

        A thread is a root plus every comment carrying its id as ``thread_id``.
        Threads are ranked by their root's list position (append order == age),
        oldest first; the thread containing ``keep_id`` is never dropped.
        """

        # Map each comment to its thread key (root id). A root's thread_id is its
        # own id (or empty -> itself); a reply carries the root's id.
        def thread_key(c: "ArtifactComment") -> str:
            return c.thread_id or c.id

        keep_thread = next((thread_key(c) for c in comments if c.id == keep_id), keep_id)
        # Oldest-first order of thread keys by first appearance.
        order: _List[str] = []
        for c in comments:
            k = thread_key(c)
            if k not in order:
                order.append(k)
        drop: set[str] = set()
        remaining = len(comments)
        for k in order:
            if remaining <= MAX_COMMENTS_PER_ARTIFACT:
                break
            if k == keep_thread:
                continue
            members = sum(1 for c in comments if thread_key(c) == k)
            drop.add(k)
            remaining -= members
        if not drop:
            return comments
        return [c for c in comments if thread_key(c) not in drop]

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
            target = next((c for c in comments if c.id == comment_id), None)
            if target is not None and (not target.parent_id or target.thread_id == target.id):
                # Root: drop the root and every reply in its thread.
                comments = [c for c in comments if c.thread_id != comment_id and c.id != comment_id]
            else:
                comments = [c for c in comments if c.id != comment_id]
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
            incoming = {
                rc.origin: rc for rc in remote_comments if rc.origin and rc.origin != "local"
            }
            deleted_origins = {
                origin for origin, rc in incoming.items() if getattr(rc, "deleted", False)
            }
            # Threads whose ROOT was deleted upstream — cascade-drop the whole
            # subtree, not just the tombstoned root. Two confirmed realities:
            #   * some remotes do NOT cascade-delete replies when a parent is
            #     deleted — the replies keep coming back deleted=False — so we drop
            #     them by thread rather than waiting for per-reply tombstones.
            #   * the remote retains the deleted ROOT as a tombstone in *every*
            #     fetch, so seed the drop-set from the INCOMING tombstones (the
            #     authoritative, always-present signal), not only the local mirror
            #     — that mirror is gone after the first cascade, so relying on it
            #     would let the orphans come back on the next fetch.
            # Replies share the root's thread_id (provider: thread_id =
            # parentCommentId or commentId), so this drops the subtree on every
            # fetch — self-healing, no persistent local tombstone needed. Local
            # threads can't collide: a deleted root is always a provider comment.
            dropped_threads: set[str] = set()
            for irc in incoming.values():
                irc_root = not irc.parent_id or irc.thread_id == irc.id
                if getattr(irc, "deleted", False) and irc_root:
                    dropped_threads.add(irc.thread_id)
                    dropped_threads.add(irc.id)
            for c in existing:
                if c.origin in deleted_origins and (not c.parent_id or c.thread_id == c.id):
                    dropped_threads.add(c.thread_id)
                    dropped_threads.add(c.id)
            result: _List["ArtifactComment"] = []
            seen: set[str] = set()
            changed = False
            for c in existing:
                if c.thread_id in dropped_threads:
                    # Reply (local or provider) under a root deleted upstream —
                    # cascade-drop it too, even if it's local-origin (orphaned).
                    changed = True
                    continue
                if not c.origin or c.origin == "local":
                    result.append(c)  # local comment — never touched by remote sync
                    continue
                if c.origin in deleted_origins:
                    changed = True  # tombstoned on the provider — drop the mirror
                    continue
                rc = incoming.get(c.origin)
                if rc is None:
                    result.append(c)  # not in this fetch — keep (don't wipe)
                    continue
                seen.add(c.origin)
                if c.status != rc.status or c.body != rc.body or c.author != rc.author:
                    c.status, c.body, c.author = rc.status, rc.body, rc.author
                    c.updated_at = rc.updated_at or c.updated_at
                    changed = True
                result.append(c)
            for origin, rc in incoming.items():
                if origin in seen or origin in deleted_origins:
                    continue
                if rc.thread_id in dropped_threads:
                    continue  # belongs to a thread whose root was deleted upstream
                result.append(rc)
                changed = True
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

        Matching is a plain substring check on ``anchor_quote`` — the same
        exactness contract as the frontend highlighter's ``indexOf`` matcher
        (``useMarkdownCommentHighlights``). No fuzzy matching in v1: a quote
        either exists in the content or the thread is flagged. Resolved
        threads are skipped (their anchors are historical by definition).
        The flag is symmetric: content that brings the quote back (e.g. a
        revert) clears it. Tolerant — an anchor-rescan failure must never
        break the content write it piggybacks on.
        """
        try:
            comments = self._load_comments(slug)
            changed = False
            for c in comments:
                if not c.anchor_quote or c.status == "resolved":
                    continue
                orphaned = c.anchor_quote not in content
                if orphaned != c.anchor_orphaned:
                    c.anchor_orphaned = orphaned
                    c.updated_at = _now_iso()
                    changed = True
            if changed:
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
        if not isinstance(raw_list, list):
            return []
        comments: _List["ArtifactComment"] = []
        for raw in raw_list:
            if not isinstance(raw, dict):
                continue
            cid = raw.get("id")
            if not cid:
                continue
            comments.append(
                ArtifactComment(
                    id=str(cid),
                    origin=str(raw.get("origin") or "local"),
                    provider=raw.get("provider"),
                    scope=str(raw.get("scope") or "private"),
                    author=str(raw.get("author") or ""),
                    is_agent=bool(raw.get("is_agent")),
                    body=str(raw.get("body") or ""),
                    anchor_quote=raw.get("anchor_quote"),
                    anchor_prefix=raw.get("anchor_prefix"),
                    anchor_suffix=raw.get("anchor_suffix"),
                    anchor_start_offset=raw.get("anchor_start_offset"),
                    anchor_end_offset=raw.get("anchor_end_offset"),
                    anchor_version=raw.get("anchor_version"),
                    thread_id=str(raw.get("thread_id") or cid),
                    parent_id=raw.get("parent_id"),
                    status=str(raw.get("status") or "open"),
                    target_provider=raw.get("target_provider"),
                    target_external_id=raw.get("target_external_id"),
                    sync_state=str(raw.get("sync_state") or "local_only"),
                    anchor_orphaned=bool(raw.get("anchor_orphaned")),
                    created_at=str(raw.get("created_at") or ""),
                    updated_at=str(raw.get("updated_at") or ""),
                )
            )
        return comments

    def _write_comments(self, slug: str, comments: _List["ArtifactComment"]) -> None:
        """Persist comments list to comments.json sidecar."""
        path = self._artifact_dir(slug) / "comments.json"
        data = []
        for c in comments:
            entry: dict[str, Any] = {
                "id": c.id,
                "origin": c.origin,
                "scope": c.scope,
                "author": c.author,
                "is_agent": c.is_agent,
                "body": c.body,
                "thread_id": c.thread_id,
                "status": c.status,
                "sync_state": c.sync_state,
                "created_at": c.created_at,
                "updated_at": c.updated_at,
            }
            if c.provider:
                entry["provider"] = c.provider
            if c.parent_id:
                entry["parent_id"] = c.parent_id
            if c.target_provider:
                entry["target_provider"] = c.target_provider
            if c.target_external_id:
                entry["target_external_id"] = c.target_external_id
            if c.anchor_orphaned:
                entry["anchor_orphaned"] = True
            if c.anchor_quote:
                entry["anchor_quote"] = c.anchor_quote
            if c.anchor_prefix:
                entry["anchor_prefix"] = c.anchor_prefix
            if c.anchor_suffix:
                entry["anchor_suffix"] = c.anchor_suffix
            if c.anchor_start_offset is not None:
                entry["anchor_start_offset"] = c.anchor_start_offset
            if c.anchor_end_offset is not None:
                entry["anchor_end_offset"] = c.anchor_end_offset
            if c.anchor_version is not None:
                entry["anchor_version"] = c.anchor_version
            data.append(entry)
        self._write_text(path, json.dumps(data, indent=2))

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
            valid = {f.name for f in fields_of(ArtifactPublication)}
            for key, value in fields.items():
                if key not in valid:
                    raise ArtifactValidationError(f"unknown publication field: {key}")
                setattr(art.publication, key, value)
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
        # Per the security-controls rule: all file reads must go through
        # hooks.py which enforces is_sensitive_path().
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
        # before the field existed — they get an empty
        # list and pick up a synthetic backfilled history on next get().
        raw_events = raw.get("events", []) or []
        events: _List[dict] = []
        if isinstance(raw_events, list):
            for ev in raw_events:
                if isinstance(ev, dict):
                    events.append(dict(ev))
        # Publication: provider state. Tolerate older meta.json without the
        # field (defaults to None) and a malformed/partial block (a missing
        # artifact_id means the artifact isn't really published — treat as
        # unpublished rather than raising).
        publication = self._parse_publication(raw.get("publication"))
        fork_metadata = self._parse_fork_metadata(raw.get("fork_metadata"))
        # Per-version render kinds (tolerant: keys + values must be str).
        raw_vk = raw.get("version_kinds") or {}
        version_kinds: dict[str, str] = {}
        if isinstance(raw_vk, dict):
            for vk_k, vk_v in raw_vk.items():
                if isinstance(vk_k, str) and isinstance(vk_v, str):
                    version_kinds[vk_k] = vk_v
        return Artifact(
            slug=str(slug),
            name=str(raw.get("name", slug)),
            kind=str(raw.get("kind", "widget")),
            kind_auto=bool(raw.get("kind_auto", False)),
            source=str(raw.get("source", "chat")),
            description=str(raw.get("description", "")),
            tags=list(raw.get("tags", []) or []),
            version=int(raw.get("version", 1)),
            created_at=str(raw.get("created_at", "")),
            updated_at=str(raw.get("updated_at", "")),
            events=events,
            events_backfilled=bool(raw.get("events_backfilled", False)),
            source_path=str(raw.get("source_path", "")),
            # Tolerant: every artifact written before source_root existed
            # defaults to "" and therefore keeps exactly today's allowed-roots
            # behavior (home / data home / configured relocate roots).
            source_root=str(raw.get("source_root", "")),
            source_copy_only=bool(raw.get("source_copy_only", False)),
            folder_id=str(raw.get("folder_id") or ""),
            pinned=bool(raw.get("pinned", False)),
            session_key=str(raw.get("session_key", "")),
            auto_registered=bool(raw.get("auto_registered", False)),
            publication=publication,
            fork_metadata=fork_metadata,
            version_kinds=version_kinds,
            webapp_metadata=webapp_metadata_from_dict(raw.get("webapp_metadata")),
        )

    @staticmethod
    def _parse_publication(raw_pub: Any) -> "ArtifactPublication | None":
        """Build an ArtifactPublication from a meta.json sub-object.

        Returns None when absent or when the block lacks the stable
        ``artifact_id`` (the one field without which the publication is
        meaningless). All other fields fall back to dataclass defaults so a
        forward/backward schema drift never crashes a load.
        """
        if not isinstance(raw_pub, dict):
            return None
        artifact_id = raw_pub.get("artifact_id")
        if not artifact_id or not isinstance(artifact_id, str):
            return None
        raw_version_map = raw_pub.get("version_map") or {}
        version_map: dict[str, int] = {}
        if isinstance(raw_version_map, dict):
            for k, v in raw_version_map.items():
                try:
                    version_map[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        raw_shared = raw_pub.get("shared_with") or []
        shared_with = (
            [str(a) for a in raw_shared if isinstance(a, str)]
            if isinstance(raw_shared, list)
            else []
        )
        # Tolerant parse: a corrupted/hand-edited meta.json may store a
        # non-numeric value here; honor the "schema drift never crashes a load"
        # contract by falling back to 0 (same pattern as version_map above).
        try:
            last_synced = int(raw_pub.get("last_synced_kirocrew_version", 0) or 0)
        except (TypeError, ValueError):
            last_synced = 0
        return ArtifactPublication(
            artifact_id=str(artifact_id),
            view_url=str(raw_pub.get("view_url") or ""),
            provider=str(raw_pub.get("provider") or DEFAULT_PROVIDER),
            visibility=str(raw_pub.get("visibility") or "PRIVATE"),
            shared_with=shared_with,
            auto_sync=bool(raw_pub.get("auto_sync", True)),
            collab_mode=("live" if raw_pub.get("collab_mode") == "live" else "mirror"),
            last_pushed_sha256=str(raw_pub.get("last_pushed_sha256") or ""),
            last_synced_kirocrew_version=last_synced,
            version_map=version_map,
            published_at=str(raw_pub.get("published_at") or ""),
            published_by=str(raw_pub.get("published_by") or ""),
            last_error=str(raw_pub.get("last_error") or ""),
            last_synced_remote_hash=str(raw_pub.get("last_synced_remote_hash") or ""),
        )

    @staticmethod
    def _parse_fork_metadata(raw_fm: Any) -> "ForkMetadata | None":
        """Build a ForkMetadata from a meta.json sub-object.

        Returns None when absent or when the block lacks the upstream
        ``upstream_artifact_id``. All other fields fall back to defaults.
        """
        if not isinstance(raw_fm, dict):
            return None
        upstream_id = raw_fm.get("upstream_artifact_id")
        if not upstream_id or not isinstance(upstream_id, str):
            return None
        try:
            upstream_version = int(raw_fm.get("upstream_version") or 0)
        except (ValueError, TypeError):
            upstream_version = 0
        return ForkMetadata(
            upstream_artifact_id=str(upstream_id),
            upstream_url=str(raw_fm.get("upstream_url") or ""),
            upstream_owner=str(raw_fm.get("upstream_owner") or ""),
            upstream_version=upstream_version,
            forked_at=str(raw_fm.get("forked_at") or ""),
            upstream_provider=str(raw_fm.get("upstream_provider") or ""),
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


# ── Artifact folders ────────────────────────────────────────────

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
                    self._folders = [f for f in raw if isinstance(f, dict) and f.get("id")]
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
            logger.info(
                "artifact folder created: id=%s name=%s parent=%s",
                folder["id"],
                name,
                parent_id or "(root)",
            )
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
