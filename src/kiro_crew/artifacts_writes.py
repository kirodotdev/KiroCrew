"""Content writes of an ``ArtifactStore``: create, image create, update, settle-blank.

Each write validates its fields, takes the store lock, writes the content (the store's
copy or the live ``source_path`` file), records versions and lifecycle events, and
fires the change listener outside the lock.

Each function here is an ``ArtifactStore`` method: the class binds it by name, so it
reads the store's own state on the instance, and a patch of the method on the class or
on an instance reaches every caller.

Instance fields read: ``_lock``.
Instance fields written: none.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import logging
from typing import TYPE_CHECKING

from kiro_crew.artifact_store import dashboard_package as _dashboard
from kiro_crew.artifact_store import records as _records
from kiro_crew.artifact_store import rules as _rules
from kiro_crew.artifact_store.images import _IMAGE_MIME_EXT, _sniff_image_dimensions
from kiro_crew.artifact_store.model import (
    Artifact,
    ArtifactConflictError,
    ArtifactValidationError,
    ImageMetadata,
)
from kiro_crew.artifact_store.rules import (
    _infer_kind,
    _validate_description,
    _validate_kind,
    _validate_name,
    _validate_slug,
    _validate_source,
    _validate_source_path,
    _validate_tags,
    detect_editor_kind,
)
from kiro_crew.artifacts_disk import (
    _TOKEN_RE,
    MAX_CONTENT_BYTES,
    MAX_VERSIONS,
    _now_iso,
    _validate_content,
)
from kiro_crew.deploy.webapp_types import WebAppMetadata
from kiro_crew.metrics.events import ARTIFACTS_CREATED, emit_counter

if TYPE_CHECKING:
    from kiro_crew.artifacts import ArtifactStore

#: The store's own logger, shared with :mod:`kiro_crew.artifacts`, so a log filter or
#: capture keyed on that name sees this code's records.
logger = logging.getLogger("kiro_crew.artifacts")


def create(
    self: ArtifactStore,
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
    goal_report: bool = False,
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

    ``goal_report=True`` marks a crewmate's weekly report or monthly review
    (:attr:`Artifact.goal_report`). Only
    :func:`kiro_crew.dashboard.goal_reminder.save_report` should set it.
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
    if kind == _dashboard.DASHBOARD_KIND:
        # A dashboard artifact's content is a LAYOUT PACKAGE, so the write
        # gate runs here rather than at each caller: every create reaches
        # this line, and the stored bytes are the canonical form so a later
        # layout comparison cannot be perturbed by the author's whitespace.
        #
        # A live file pointer is refused outright. The store validates what
        # it writes, but a linked file is read back from disk on every get,
        # where an edit made outside the store would reach the page as a
        # package nothing checked.
        if source_path:
            raise ArtifactValidationError(
                "a dashboard package is store-owned: kind='dashboard' takes no "
                "source_path, because a linked file is read back unvalidated"
            )
        content = _dashboard.canonical_package_content(content)
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
            goal_report=bool(goal_report),
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
        if self._is_live_pointer(art):
            art.content_token = None
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
    self: ArtifactStore,
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


def update(
    self: ArtifactStore,
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
    snapshot_token: str | None = None,
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
    ``content_token`` a prior ``get()`` returned, and when the stored token
    differs the write raises :class:`ArtifactConflictError` and changes
    nothing. A change written to current.html outside this store is not
    detected. Omitted, the write is last-write-wins. It applies to
    store-backed artifacts only; a live file-backed artifact serves no token
    and ignores it, because its content is owned by the source file.

    ``snapshot_token`` is the ``content_token`` of the read a request-driven
    caller authorized against, carried for EVERY write it makes rather than only
    the ones it classified. It exists because a dashboard package may never be
    written against a snapshot that has moved on, and that is decided here --
    against the record as this lock finds it -- not by what the caller saw.
    """
    slug = _validate_slug(slug)
    for label, value in (
        ("expected_token", expected_token),
        ("snapshot_token", snapshot_token),
    ):
        if value is not None and not (isinstance(value, str) and _TOKEN_RE.fullmatch(value)):
            raise ArtifactValidationError(
                f"{label} must be the 64-character hex content_token from a prior read"
            )
    with self._lock:
        art = self._load_meta(slug)
        # CONTENT IS NOT THE ONLY THING WORTH FENCING, for one kind.
        #
        # A content write is guarded because it replaces bytes. A metadata-only
        # write is deliberately NOT, because a rename or a pin flip must never
        # become a conflict the person never caused -- the editor's autosave and
        # every tag edit depend on last-write-wins.
        #
        # A dashboard package is the exception, and it gets ONE rule, stated on
        # the record THIS LOCK LOADED:
        #
        #   a record whose CURRENT kind is `dashboard` is never written without a
        #   matching snapshot token, and an ABSENT token is a REFUSAL.
        #
        # WHY IT LIVES HERE AND NOWHERE ELSE. Every caller authorizes against a
        # snapshot it read a moment earlier, so every caller's judgement is of a
        # record that may already have moved. Three rounds of fixing that in the
        # caller, each scoped by what the caller had classified, each time left the
        # race in the case it had not: an event type it did not count, then a
        # content-less `kind` PATCH, then a read that found prose or nothing and so
        # sent no token at all -- which rewrote both the kind AND the bytes of
        # whatever the slug held by then. A time-of-check guard in the caller
        # cannot close a time-of-use hole here. This is the time of use.
        #
        # FAIL CLOSED ON ABSENT too, not only on a mismatch. A
        # caller repeatedly PATCHing a predictable empty slug spends most of its
        # time inside the read-to-write gap, so an owner creating a dashboard there
        # lands in it often, with nothing else having to line up. And `dashboard` is
        # not a user-selectable kind, so nothing puts the kind back without a human.
        #
        # SCOPED BY THE STORED KIND, never the submitted one. What makes a write
        # dangerous is what the slug HOLDS: the dangerous PATCH submits
        # `kind="markdown"`, so reading the submitted value would let exactly the
        # write this guards against escape the guard.
        #
        # SCOPED TO WRITES THAT CAN TAKE THE PAGE AWAY -- its `kind`, its bytes, or
        # any write whose caller named a snapshot. A rename or a tag edit that names
        # none keeps last-write-wins: `publish_sync` asks for a snapshot bump and the
        # deploy route writes `webapp_metadata`, neither can move a page to another
        # owner, and neither reads the record first.
        live_pointer = self._is_live_pointer(art)
        dashboard_fence = (
            not live_pointer
            and art.kind == _dashboard.DASHBOARD_KIND
            and (kind is not None or content is not None or snapshot_token is not None)
        )
        if dashboard_fence:
            self._ensure_content_token_locked(art)
            assert art.content_token is not None
            authorized = snapshot_token or expected_token
            if authorized is None:
                raise ArtifactConflictError(
                    f"artifact {slug!r} holds a dashboard package, which is never "
                    "written without the content_token of the read it was authorized "
                    "against; nothing was written -- refetch and re-base",
                    current_token=art.content_token,
                )
            if not _hmac.compare_digest(art.content_token, authorized):
                raise ArtifactConflictError(
                    f"artifact {slug!r} changed since it was read; refetch and re-base",
                    current_token=art.content_token,
                )
        # AND THE CALLER'S OWN COMPARE-AND-SET IS A SEPARATE QUESTION, asked of
        # every kind including this one.
        #
        # The fence above asks whether the write names the record it is landing
        # on, and a request-driven caller can always answer it with its own fresh
        # read. ``expected_token`` asks something else: whether the record still
        # holds what THAT client last saw. Satisfying the first and skipping the
        # second let an owner's stale tab overwrite a newer layout with no
        # conflict reported -- an agent and an open tab editing one dashboard is
        # the ordinary case for this feature. So both run, independently.
        if expected_token is not None and not live_pointer and content is not None:
            self._ensure_content_token_locked(art)
            assert art.content_token is not None
            if not _hmac.compare_digest(art.content_token, expected_token):
                raise ArtifactConflictError(
                    f"artifact {slug!r} changed since it was read; refetch and re-base",
                    current_token=art.content_token,
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
        # The dashboard write gate. ``art.kind`` is final by here, so this
        # one block governs every update that lands on a dashboard artifact
        # -- the dashboard tool path, the browser PATCH and a plain
        # ``artifact_update`` all arrive through this method -- and decides
        # three things the generic path cannot:
        #
        # 1. the content is a valid package, stored canonically;
        # 2. a REVERT restores layout only: ``revert_package`` keeps the
        #    LIVE ``bound_to`` so rolling back past a rebind cannot hand
        #    this page to the crewmate the stale binding names (values need
        #    no handling -- they were never in the package);
        # 3. the version. A dashboard snapshots exactly when the layout
        #    changed, overriding the caller's ``snapshot`` either way: an
        #    MCP write defaults it to True and would version every
        #    no-op recompose, while a browser write defaults it to False
        #    and would leave a real layout change with no history to
        #    revert to.
        if art.kind == _dashboard.DASHBOARD_KIND:
            # A dashboard is store-owned, and ``create`` already refuses a
            # ``source_path`` for that reason. The same has to hold on the
            # way IN to the kind: converting an artifact that keeps a live
            # file pointer would leave a dashboard whose bytes are re-read
            # from a file outside the store on every get, so the package a
            # page renders is one nothing validated -- and a revert would
            # then write the binding it recovered into that outside file.
            if kind_changed and art.source_path and not art.source_copy_only:
                raise ArtifactValidationError(
                    "switching an artifact to kind='dashboard' cannot keep a live "
                    "source_path: a dashboard package is store-owned, so clear the "
                    "pointer in the same write"
                )
            if content is None:
                if kind_changed:
                    raise ArtifactValidationError(
                        "switching an artifact to kind='dashboard' needs the package "
                        "content in the same write: there is nothing valid to store"
                    )
                # No content, so no layout change, so no version -- this
                # also disarms the explicit-Snapshot path below, which would
                # otherwise re-version the live package unchanged.
                snapshot = False
                # Disarming that path removes a SECOND thing it does: it is
                # also where a snapshot-only update loads the live bytes off
                # disk. A caller that asked for a snapshot and got a
                # meta-only record back reads ``art.content`` as empty, and
                # an auto-sync publisher then uploads that emptiness over a
                # live dashboard and records the push as a success. So the
                # package is loaded here instead: the caller gets the bytes
                # it asked for, and the version count still does not move.
                art.content = self._read_text(self._artifact_dir(slug) / "current.html")
            else:
                stored_package = self._read_text(self._artifact_dir(slug) / "current.html")
                if event_type == "reverted":
                    content = _dashboard.revert_package(stored_package, content)
                else:
                    content = _dashboard.canonical_package_content(content)
                art.content = content
                snapshot = _dashboard.layout_changed(stored_package, content)
        # EVENT TYPE VALIDATED HERE, which is the FIRST point it can be: the
        # effective ``snapshot`` flag is settled by the line above (a dashboard
        # layout change forces it on) and nothing has been written yet.
        #
        # A REJECTED REQUEST MUST CHANGE NOTHING, and that is what fixes this spot.
        # Everything downstream writes: the content token rotates, ``current.html``
        # is replaced, the comment anchors are rescanned. A refusal raised from
        # inside the version block further down therefore answers 400 for a write
        # that has already landed, and landed with no version entry naming it. So
        # the check belongs ahead of all three, not merely ahead of the version
        # bump it guards.
        #
        # OUTSIDE the snapshot block on purpose, so the check does not depend on
        # whether this particular write happens to version. An event type that is
        # not a real lifecycle event is refused on every update, which is the
        # honest answer to a caller naming one. ``None`` is not an event: it means
        # "decide from the actor" and passes through.
        if event_type is not None and event_type not in _records.ALLOWED_EVENT_TYPES:
            raise ArtifactValidationError(
                f"invalid event type {event_type!r}: "
                f"must be one of {sorted(_records.ALLOWED_EVENT_TYPES)}"
            )
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
            if content is not None:
                art.content_token = self._rotate_content_token_locked(slug)
            self._write_text(prev, live_content)
            # Never mirror back for a copy — editing it must not rewrite
            # the user's original file — and never for content this call
            # just READ off that same file (see snapshot_derived above).
            if art.source_path and not art.source_copy_only and not snapshot_derived:
                if not self._try_write_source_path(art.source_path, live_content, art.source_root):
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
            # whose quoted text is missing from the content get flagged as orphaned
            # (and restored if the text comes back, e.g. on a revert).
            # Every content write funnels through here: agent iterations,
            # dashboard saves, reverts, and upstream pulls.
            self._rescan_comment_anchors_locked(slug, live_content)

            if snapshot:
                # ``event_type`` is validated far earlier, ahead of this write's
                # first side effect. See the check beside the snapshot flag: it
                # must run before the content token and ``current.html``, both of
                # which this block is downstream of.
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
        if self._is_live_pointer(art):
            art.content_token = None
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


def settle_blank(
    self: ArtifactStore,
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


def _write_settled_draft(self: ArtifactStore, art: Artifact, draft: str) -> None:
    """Persist an unsaved editor buffer as the document's first content.

    The caller must hold ``self._lock`` and must have just confirmed that the
    stored content is empty — that confirmation is what makes this safe, since
    there is nothing to overwrite and therefore no concurrent save to lose.
    Metadata is preserved untouched: the user may well have named or tagged the
    document before typing, and this is a content write, not a reset.
    """
    art.content_token = self._rotate_content_token_locked(art.slug)
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
