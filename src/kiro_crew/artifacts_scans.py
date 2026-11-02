"""Lookups and sweeps over every ``meta.json`` of an ``ArtifactStore``.

The lookups by upstream artifact id (one and the batch index) and by source path, the
kind migration, the retention sweep of auto-registered widgets, and the reap of
auto-registered chat-image copies owned by permanently deleted sessions.

Each function here is an ``ArtifactStore`` method: the class binds it by name, so it
reads the store's own state on the instance, and a patch of the method on the class or
on an instance reaches every caller.

Instance fields read: ``_lock`` and ``_root``.
Instance fields written: none.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Iterable, Iterator
from typing import List as _List

from kiro_crew.artifact_store.model import (
    Artifact,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactValidationError,
)
from kiro_crew.artifact_store.rules import _markdown_misclassification_reason, _validate_slug
from kiro_crew.artifacts_disk import (
    MAX_AUTO_WIDGET_ARTIFACTS,
    MAX_META_BYTES,
    MAX_SESSION_KEY_CHARS,
)

if TYPE_CHECKING:
    from kiro_crew.artifacts import ArtifactStore

#: The store's own logger, shared with :mod:`kiro_crew.artifacts`, so a log filter or
#: capture keyed on that name sees this code's records.
logger = logging.getLogger("kiro_crew.artifacts")

#: Longest slug ``_SLUG_RE`` admits (its ``{0,78}`` body plus the two anchoring
#: characters). Every slug the store WRITES passes ``_SLUG_RE``; this names the
#: same bound for the one reader that retains slugs decoded straight from an
#: agent-writable ``meta.json`` without re-validating them (``_iter_auto_images``).
MAX_SLUG_CHARS = 80

#: Bound on the artifact directories one session-image reap walks. The reap runs
#: as a side effect of deleting a session, so it must not be the thing that
#: takes the gateway down on a huge store: past this many entries the walk
#: raises and the reap is skipped fail-closed (a leak, logged; never a crash).
MAX_REAP_SCAN_ENTRIES = 200_000


def _slug_well_formed(slug: str) -> bool:
    try:
        _validate_slug(slug)
    except ArtifactValidationError:
        return False
    return True


def _is_sweepable_auto_widget(self: ArtifactStore, art: Artifact) -> bool:
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
    # Chat images are durable transcript assets, not rolling widget previews.
    # Their lifecycle is the owning session; count-based pruning must never
    # make a still-existing chat render a broken image.
    if art.kind == "image":
        return False
    return self._is_unclaimed_auto_artifact(art)


def _is_unclaimed_auto_artifact(self: ArtifactStore, art: Artifact) -> bool:
    """True when ``art`` is auto-registered and carries NO investment signal.

    The single spelling of the "nobody has claimed this" test, shared by the
    widget sweep and the per-session image reap so the two can never drift.
    The signals are documented on :func:`_is_sweepable_auto_widget`.
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


def prune_auto_widgets(self: ArtifactStore, *, keep: int = MAX_AUTO_WIDGET_ARTIFACTS) -> int:
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


def migrate_kinds(self: ArtifactStore, *, apply: bool = False) -> _List[dict[str, Any]]:
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
    run finds nothing because a rewritten candidate's kind is not ``widget``.
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
    self: ArtifactStore, artifact_id: str, *, provider: str | None = None
) -> Artifact | None:
    """Locate the local artifact linked to a cloud ``artifact_id``.

    Matches either a ``publication`` (my push target) or ``fork_metadata``
    (forked-from origin) that carries this upstream id. Bridges the cloud
    id to the local slug — it serves to reconcile the "my cloud artifacts" list
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

    from kiro_crew.publish_provider import DEFAULT_PROVIDER  # noqa: F811

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


def artifact_index_key(provider: str, artifact_id: str) -> str:
    """Compound index key for :meth:`index_by_artifact_id` lookups.

    Provider-native ids collide across providers, so the browse-annotation
    index is keyed by ``provider\\x00artifact_id``. A NUL separator can't
    appear in a provider name or id, so the key is unambiguous."""
    return f"{provider}\x00{artifact_id}"


def index_by_artifact_id(self: ArtifactStore) -> dict[str, str]:
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


def find_by_source_path(self: ArtifactStore, source_path: str) -> Artifact | None:
    """Locate an existing artifact already saved from this filesystem path.

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


def auto_image_slugs(self: ArtifactStore) -> dict[str, str]:
    """Every auto-registered image copy in the store right now: ``{slug: owner}``.

    A reap snapshots this BEFORE it decides which sessions are live, then
    deletes only slugs from the snapshot (``delete_auto_images_for_sessions(
    only_slugs=...)``): a session recreated under a deleted key after the
    snapshot registers copies whose slugs are not in it, so they are not that
    reap's to take, with no clock involved.
    """
    return {art.slug: art.session_key for art in self._iter_auto_images() if art.session_key}


def _iter_auto_images(self: ArtifactStore) -> Iterator[Artifact]:
    """Walk the store incrementally, yielding auto-registered image copies.

    Nothing is materialized store-wide: the directory listing is consumed
    lazily and each ``meta.json`` is read and either yielded or dropped. The
    walk is bounded by :data:`MAX_REAP_SCAN_ENTRIES` directory entries and
    raises :class:`ArtifactError` past it, so the reap that drives it aborts
    instead of growing without bound; an unreadable meta is skipped. The two
    fields a caller retains per entry, ``slug`` and ``session_key``, are
    bounded HERE as well, at the point of retention, and so is the record
    they are decoded from: the store directory is agent-writable, so a
    ``meta.json`` over :data:`MAX_META_BYTES` (sized before it is read,
    and read through a bounded descriptor in any case) or a record carrying
    an over-long slug or owner aborts the walk the same way an over-long
    listing does. The write door (``create_image``) refuses such owners,
    so a conforming store never trips this.
    """
    if not self._root.exists():
        return
    seen = 0
    for child in self._root.iterdir():
        seen += 1
        if seen > MAX_REAP_SCAN_ENTRIES:
            raise ArtifactError(
                f"artifact store holds more than {MAX_REAP_SCAN_ENTRIES} entries; "
                "session image reap aborted"
            )
        meta = child / "meta.json"
        try:
            if not child.is_dir() or not meta.exists():
                continue
            oversized = meta.stat().st_size > MAX_META_BYTES
        except OSError:
            continue
        if oversized:
            raise ArtifactError(
                f"artifact store holds a meta.json over {MAX_META_BYTES} bytes; "
                "session image reap aborted"
            )
        try:
            art = self._read_meta_file(meta)
        except (ArtifactError, OSError, ValueError):
            continue
        if art.kind == "image" and art.auto_registered:
            if len(art.slug) > MAX_SLUG_CHARS or len(art.session_key or "") > MAX_SESSION_KEY_CHARS:
                raise ArtifactError(
                    f"artifact store holds an auto-image record with a slug over "
                    f"{MAX_SLUG_CHARS} chars or an owner over {MAX_SESSION_KEY_CHARS} "
                    "chars; session image reap aborted"
                )
            # The slug is agent-writable too, and the reap turns it into a
            # directory to remove: a record whose slug is not a well-formed
            # slug, or does not name the directory it was read from (".",
            # a sibling's slug), is never retained.
            if not _slug_well_formed(art.slug) or art.slug != child.name:
                continue
            yield art


def delete_auto_images_for_sessions(
    self: ArtifactStore, session_keys: set[str], *, only_slugs: set[str] | None = None
) -> int:
    """Delete untouched chat-image copies owned by permanently deleted sessions.

    Saved/invested images survive: pinning, filing, publishing, editing,
    describing, tagging, forking, or commenting turns an automatic transcript
    copy into a user-owned artifact. The eligibility re-check and removal share
    one lock acquisition so a concurrent pin cannot be overwritten.

    ``only_slugs`` bounds the GENERATION being reaped to an exact set of slugs
    the caller snapshotted (:func:`auto_image_slugs`) before it decided who is
    live; a copy outside that set was registered after the decision and is
    kept whatever its owner says.
    """
    keys = {key for key in session_keys if key}
    if not keys:
        return 0

    def eligible(art: Artifact) -> bool:
        if only_slugs is not None and art.slug not in only_slugs:
            return False
        if not _slug_well_formed(art.slug):
            return False
        return (
            art.kind == "image"
            and art.session_key in keys
            and self._is_unclaimed_auto_artifact(art)
        )

    # With a snapshot the candidates are exactly its slugs, loaded one by
    # one. Without one the store is walked incrementally under the reap
    # bound and the walk completes BEFORE the first delete, so an overflow
    # aborts with nothing reaped rather than half a generation gone. Only
    # eligible copies are retained from the walk, never the whole store.
    candidates: Iterable[Artifact]
    if only_slugs is not None:
        candidates = self._load_each(sorted(only_slugs))
    else:
        candidates = [art for art in self._iter_auto_images() if eligible(art)]

    deleted = 0
    for art in candidates:
        if not eligible(art):
            continue
        try:
            with self._lock:
                fresh = self._load_meta(art.slug)
                if not eligible(fresh):
                    continue
                adir = self._artifact_dir(art.slug)
                if not adir.exists():
                    continue
                self._rmtree(adir)
        except (ArtifactNotFoundError, ArtifactError, OSError) as exc:
            logger.warning("session image cleanup skipped %s: %s", art.slug, exc)
            continue
        self._fire_change("delete", art.slug)
        deleted += 1
    return deleted


def _load_each(self: ArtifactStore, slugs: Iterable[str]) -> Iterator[Artifact]:
    for slug in slugs:
        try:
            yield self._load_meta(slug)
        except (ArtifactError, OSError, ValueError):
            continue
