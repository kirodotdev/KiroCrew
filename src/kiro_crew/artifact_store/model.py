"""Artifact records: the error hierarchy, the generation sentinel and the dataclasses.

These are the types every other artifact owner exchanges. They carry data and the
``Artifact.to_dict`` projection only: persistence lives in :mod:`kiro_crew.artifacts`
and :mod:`kiro_crew.artifact_store.records`, validation in
:mod:`kiro_crew.artifact_store.rules`. ``kiro_crew.artifacts`` re-exports every name
here, so the classes keep one identity whichever module a caller imports them from.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from typing import List as _List

from kiro_crew.deploy.webapp_types import WebAppMetadata
from kiro_crew.publish_provider import DEFAULT_PROVIDER


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


class ArtifactStillPublishedError(ArtifactError):
    """Raised by ``delete(refuse_if_published=True)`` when the artifact is published.

    The artifact's publication record is the only handle able to withdraw a copy that
    may still be served, so a caller destroying artifacts in bulk uses this to be told
    "not this one" instead of silently erasing that handle. Distinct from the base
    error so such a caller can separate "refused, and correctly" from a real failure.
    """


class ArtifactConflictError(ArtifactError):
    """Raised by ``update(expected_token=...)`` when the content changed since that read.

    Another writer -- a second dashboard window, an agent edit -- saved after the
    caller read the content its token names. Carries the stored current token so
    the client can refetch and re-base instead of overwriting the newer content.
    """

    def __init__(self, message: str, *, current_token: str) -> None:
        super().__init__(message)
        self.current_token = current_token


class ArtifactReplacedError(ArtifactError):
    """Raised when a slug does not hold the artifact generation the caller named.

    A slug is a NAME, not an identity: :meth:`ArtifactStore._unique_slug` re-mints a
    freed slug identically, so an artifact created under the same title after an
    earlier one at that slug is gone lands on exactly that slug. A caller that decided
    what to do while holding a slug therefore has to say WHICH artifact it decided
    about, and ``created_at`` is that generation stamp.

    Passing ``expect_created_at`` asks for the decision to be re-checked against the
    record under the store lock; this is raised instead of acting when a different
    generation now answers to the name. Distinct from the base error so a caller can
    separate "a replacement arrived, so I left it alone" from a real failure -- the
    replacement is a live artifact nobody asked to destroy.
    """


class _ExpectAbsent:
    """Sentinel for ``expect_created_at``: the caller read this slug as holding NOTHING.

    ``None`` there means "I have no generation to compare", which is the honest answer for
    a caller that never resolved the artifact -- and it disables the check. A delete that
    read the slug as ABSENT needs the opposite: any artifact present by the time the lock
    is held appeared after that read, so it is one nobody asked to delete. Those are two
    different statements and a single ``None`` cannot carry both, which is why absence gets
    its own value rather than sharing one.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover -- diagnostics only
        return "EXPECT_ABSENT"


#: The value a caller passes as ``expect_created_at`` when it read the slug as empty.
EXPECT_ABSENT = _ExpectAbsent()


@dataclass
class ForkMetadata:
    """Provenance tracking for an artifact forked from the remote store.

    Present (non-``None`` on :class:`Artifact`) once an artifact has been
    forked from a remote artifact. Records the upstream identity
    so pull-latest and upstream linking work.
    """

    upstream_artifact_id: str = ""  # provider-native id of the source
    upstream_url: str = ""  # stable view URL of the source
    upstream_owner: str = ""  # upstream owner alias at fork time
    upstream_version: int = 0  # versionNumber at fork/last-pull time
    forked_at: str = ""  # ISO timestamp of initial fork
    # Publish provider the fork came from. Empty on older records; readers then
    # use DEFAULT_PROVIDER, the only provider those records could have used.
    upstream_provider: str = ""


@dataclass
class ArtifactPublication:
    """Publication state for an artifact.

    Present (non-``None`` on :class:`Artifact`) once an artifact has been
    published to the remote store. The ``artifact_id`` and ``view_url`` are
    *stable* across every version — pushing a new version reuses the same id
    and URL. This block holds only data; all networking lives in the
    publish-sync / publish-client layer (the purity rule the
    :mod:`kiro_crew.artifacts` module docstring states).

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
    auto_sync: bool = True  # push a new remote version on every local version bump
    #: Who owns the remote copy: ``"mirror"`` (Kiro Crew is the only writer and the
    #: remote is a guarded mirror) or ``"live"`` (the remote holds a live CRDT and
    #: Kiro Crew is one participant). Set from ``provider.sync_model().collab_mode``
    #: at publish or clone time. Picks the push path (CRDT edit vs guarded blob
    #: replace) and the conflict / force-push UI. Missing in meta.json: ``"mirror"``.
    collab_mode: str = "mirror"
    last_pushed_sha256: str = ""  # concurrency guard for the next version push
    last_synced_kirocrew_version: int = 0
    #: Wrapper envelope revision at the time of the last push — compared against
    #: ``publish_sync.WRAPPER_REVISION`` to detect wrapper-only staleness.
    wrapper_revision: int = 0
    # Maps str(kirocrew_version) -> remote_version_number.
    version_map: dict[str, int] = field(default_factory=dict)
    published_at: str = ""
    published_by: str = ""  # gateway owner alias, as the remote store reports it
    last_error: str = ""  # conflict / sync-failure surfaced to the UI
    #: Status line for a publish that SUCCEEDED but whose link is not usable yet
    #: (e.g. CloudFront is still rolling out the first deploy). Never write it to
    #: ``last_error``: every reader treats that as failure, shows the publish red
    #: and withholds the URL.
    notice: str = ""
    #: Which case :attr:`notice` is, so the frontend can pick per-case text:
    #: ``"rolling_out"``, ``"distribution_disabled"``, ``"origin_mismatch"``,
    #: ``"unknown"``, or ``""`` with no notice. Set from the publish result's
    #: ``notice_code`` and cleared wherever ``notice`` is cleared. Missing in
    #: meta.json loads as ``""``.
    notice_code: str = ""
    #: sha256 of the LIVE (CRDT) remote body as of the last sync (publish / push
    #: / pull / clone / overwrite). A live CRDT provider canonicalizes markdown on write, so
    #: drift is detected remote-vs-remote against this hash — snapshot_seq bumps
    #: on mere viewing and is unreliable. Empty for mirror providers / legacy.
    last_synced_remote_hash: str = ""


@dataclass
class ArtifactComment:
    """A durable comment on an artifact (the canonical store).

    Comments can be local-only (never leave Kiro Crew) or synced to/from a
    provider (the publishing provider). The ``origin`` + ``scope`` fields determine sync
    behavior.
    """

    id: str  # local uuid
    origin: str = "local"  # "local" | "<provider>:<remote_id>"
    provider: str | None = None  # "<provider>" | None (for local)
    scope: str = "private"  # "private" | "shared"
    author: str = ""  # author user alias
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
    #: True when ``anchor_quote`` is not found in the content. The store's
    #: anchor rescan sets and clears it on every content write. It is not a
    #: ``sync_state`` value because that field tracks provider push status, and a
    #: ``pending_push`` comment can be orphaned too.
    anchor_orphaned: bool = False
    created_at: str = ""
    updated_at: str = ""
    # Transient: set on inbound provider mirrors that came back as tombstones so
    # merge_remote_comments can drop the local copy. Never persisted.
    deleted: bool = False


@dataclass
class ImageMetadata:
    """Sidecar description of a ``kind="image"`` artifact's raster bytes.

    The bytes themselves live next to ``meta.json`` in
    ``artifacts/<slug>/asset.<ext>`` — NOT in ``current.html``, which stays
    empty for image kind. This record is the JSON-serializable metadata the
    dashboard needs to render and lay out the image (natural dimensions for
    aspect-ratio boxing, mime for the ``<img>`` type, size/hash for cache and
    integrity) without having to fetch the bytes first.

    Every field has a default so a partial or legacy ``image`` block in
    meta.json is tolerant-loaded rather than raising — the same contract the
    other nested metadata blocks (``publication`` / ``fork_metadata`` /
    ``webapp_metadata``) follow.
    """

    #: Raster mime — one of the create-time allowlist (png/jpeg/webp/gif).
    mime: str = ""
    #: File extension used for the sidecar (``asset.<ext>``), derived from mime.
    ext: str = ""
    #: Byte length of the stored asset.
    size_bytes: int = 0
    #: Natural pixel dimensions, or ``None`` when the header sniff could not
    #: determine them (a truncated/odd file is stored anyway, just unmeasured).
    width: int | None = None
    height: int | None = None
    #: SHA-256 of the bytes — content-addressed cache key + integrity check.
    sha256: str = ""
    #: The uploaded/source filename, when known. Provenance only.
    original_filename: str = ""
    #: Alt text for accessibility, carried from the markdown ``![alt](...)``.
    alt: str = ""


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
    #: True when the store picked ``kind`` instead of the caller. Set only for a
    #: document created blank with no pinned kind (the library's "New artifact").
    #: While true, every content write re-runs :func:`detect_editor_kind`, so the
    #: document can settle into ``json`` / ``svg`` once there is enough text. An
    #: explicit ``kind`` passed to :meth:`ArtifactStore.update` pins the kind and
    #: clears this flag. Records without the field load as ``False``: never re-typed.
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
    #: File path of an artifact saved from the file viewer ("Save as artifact");
    #: empty for chat artifacts. It is the dedup key: saving the same path again
    #: offers to bump that artifact's version instead of creating a second one.
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
    #: True when ``source_path`` is provenance only: the artifact owns a COPY of
    #: the bytes, so reads never touch the file and writes are never mirrored to it.
    #:
    #: The path is still recorded because :meth:`find_by_source_path` dedups on
    #: it; dropping it would let one file be promoted twice. Keeping it as a live
    #: pointer instead would serve a stale snapshot as healthy whenever the file
    #: falls outside the authorized root. Defaults to False, so the other
    #: ``source_path`` producers (``/materialize``, ``/relocate``, a direct
    #: ``store.create``) keep the live pointer; only the copy verdict sets it.
    source_copy_only: bool = False
    #: Folder membership; ``""`` means unfiled (library root). An opaque folder id
    #: (see :class:`ArtifactFolderStore`), never a path, so renaming a folder never
    #: rewrites artifact records. Set by :meth:`ArtifactStore.set_folder`, which
    #: does NOT bump the version. Missing in meta.json loads as unfiled. Only
    #: local artifacts have a folder; remote/shared ones have no local record.
    folder_id: str = ""
    #: User favourite mark, persisted to meta.json. Toggling it does NOT bump the
    #: version or add a lifecycle event (like retag, rename and set_folder).
    #: Missing in meta.json loads as unpinned. The library can filter on it.
    pinned: bool = False
    #: Session key of the chat/session that saved this artifact (session
    #: provenance). Persisted; the dashboard live-resolves it to the
    #: session's current title for the Source column (falling back to
    #: "(deleted session)" once the session is gone). Empty for
    #: non-session origins (bulk import, older artifacts).
    session_key: str = ""
    #: True when the store created this record from a chat-emitted ``<mcwidget>``
    #: instead of an explicit save. :meth:`ArtifactStore.prune_auto_widgets` may
    #: sweep it while it is unpinned; pinning keeps this flag but exempts the
    #: record. Records without the field load as ``False`` and are never swept.
    auto_registered: bool = False
    #: True for a crewmate's weekly report or monthly review, saved through
    #: ``save_goal_report`` (:func:`kiro_crew.dashboard.goal_reminder.save_report`).
    #: The artifact viewer opens such a page in the same sandbox as the crewmate
    #: Dashboard page. Set only at create time by that one caller, never through
    #: the artifact API, and kept across every later write. Tolerant-loaded:
    #: every artifact that predates the field defaults to ``False``.
    goal_report: bool = False
    #: Publication state; ``None`` until published. Holds the stable provider
    #: id/URL, visibility, shared-with aliases and version-sync bookkeeping.
    #: Missing in meta.json loads as ``None``.
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
    #: Set by ``get()``, never persisted: True when the live content differs from
    #: the latest numbered snapshot (a file-backed source edited outside Kiro Crew
    #: counts), so the frontend can enable the Snapshot button.
    live_dirty: bool = False
    #: Set by ``get()``, never persisted: True when ``source_path`` is set but its
    #: live read FAILED (deleted, moved, unreadable, or outside the authorizing
    #: roots). The store then serves the last snapshot, so without this flag a dead
    #: pointer looks healthy (``live_dirty`` compares against that fallback).
    source_missing: bool = False
    #: Set by ``create()`` when it had to suffix the slug derived from ``name``
    #: because that slug was taken: names the plain slug that was already in
    #: use, and is empty otherwise. Reported by the uniquifier rather than
    #: inferred by a caller, because only the create knows a suffix happened —
    #: ``update()`` renames without recomputing the slug, and a reused record
    #: read from disk would compare as collided when nothing collided.
    #: Not persisted; a create-time fact, meaningless on a later read.
    slug_collided_with: str = ""
    #: Structured metadata for ``kind="webapp"`` artifacts — a deployed application
    #: (deploy target, architecture, lifecycle/TTL, cost estimate, teardown handle).
    #: ``None`` for every other kind. Tolerant-loaded from meta.json.
    webapp_metadata: "WebAppMetadata | None" = None
    #: Structured metadata for ``kind="image"`` artifacts. ``None`` for every
    #: other kind. The raster bytes live in the ``asset.<ext>`` sidecar (see
    #: :class:`ImageMetadata`); this block is what the dashboard renders from.
    #: Tolerant-loaded from meta.json (older/other-kind artifacts default to
    #: ``None``).
    image: "ImageMetadata | None" = None
    #: Random optimistic-concurrency token for the current store-backed content.
    #: Persisted in meta.json and replaced on every content write. Clients receive
    #: it only beside current store-owned content and echo it as ``expected_token``.
    #: ``None`` for untokened legacy records, versioned reads and live pointers.
    content_token: str | None = None

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
        # The token only means something next to the current content it names.
        if not d.get("content_token") or (not persist and not include_content):
            d.pop("content_token", None)
        # slug_collided_with is an internal create-time signal read off the
        # attribute, never through this dict: a response that reports it composes
        # the key itself, and serializing it here would leak it into every later
        # GET as though the collision had just happened.
        d.pop("slug_collided_with", None)
        if persist:
            # GET-time fields: a stored value goes stale as soon as the live state
            # changes through a path the store did not see (a stale
            # source_missing keeps flagging a file that came back, or hides one
            # that just died).
            d.pop("live_dirty", None)
            d.pop("source_missing", None)
        return d
