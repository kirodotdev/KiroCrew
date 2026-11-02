"""Reclaiming the chat-image copies of permanently deleted sessions.

A chat image an agent showed is copied into the artifact store at finalize time
(``image_artifacts.register_images``), so the picture survives its scratch file.
Those copies are exempt from the widget-preview sweep, so every route that
permanently deletes a transcript reaps them here: :func:`_fork_lineage` is the
snapshot taken BEFORE the unlink, :func:`_reap_session_images` the reap after it,
and :func:`_image_reap_set` the decision, which keeps an ancestor's copies while
any fork that renders them survives (live, staged in the trash, or recreated).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field as _dc_field
from typing import TYPE_CHECKING, Any, Callable, Collection, Mapping

from kiro_crew.artifacts import get_default_store
from kiro_crew.dashboard import fork_lineage
from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.image_artifacts import drain_registrations
from kiro_crew.session_storage import staged_transcript_lineage

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

# The family logs under the facade's name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.handlers.sessions")


def _image_artifact_session_keys(keys: set[str]) -> set[str]:
    """The spellings under which an image copy may record one of ``keys``.

    ``register_images`` has one caller (``chat_runner``) and it passes
    ``slot.key`` — the tab name. For a dashboard-born tab that is the bare slot,
    whose transcript stem is ``dashboard_<slot>``; the delete handlers receive
    that transcript's history key (``dashboard:<slot>``) or its file stem, so the
    copy's key is the input with the transport prefix removed. A cron-born tab is
    named ``cron-<id>`` while its transcript is ``cron:<id>`` / ``cron_<id>``, so
    that spelling is added too. All forms are kept so a record written with the
    history key itself (none today) is still found. One owner:
    :func:`fork_lineage.owner_spellings`.
    """
    out: set[str] = set()
    for key in keys:
        if key:
            out |= fork_lineage.owner_spellings(key)
    return out


@dataclass
class _ForkLineage:
    """Fork edges over the session catalog, as transcript stems.

    ``parent_of`` is the immediate ``forked_from`` edge; ``known_ancestors`` is
    the materialized ``fork_ancestors`` chain each fork carries, which keeps
    ancestry provable after an intermediate fork's own record is gone.
    ``unreadable`` lists sessions whose metadata could not be read; the reap
    fails CLOSED on any of them, since a missing edge could expose an image copy
    a surviving fork still renders.
    """

    keys: set[str] = _dc_field(default_factory=set)
    parent_of: dict[str, str] = _dc_field(default_factory=dict)
    known_ancestors: dict[str, set[str]] = _dc_field(default_factory=dict)
    unreadable: set[str] = _dc_field(default_factory=set)


def _staged_transcript_lineage() -> dict[str, dict[str, Any] | None]:
    """Fork lineage of the transcripts staged in the session trash (restorable,
    so survivors for the image reap, with their edges). ``None`` for a staged
    transcript whose metadata could not be read. A module-level seam so tests
    can stand it in; raises when the trash root cannot be listed, which the
    reap treats as unreadable."""
    return staged_transcript_lineage()


def _transcript_stems_on_disk(log: Any) -> set[str]:
    """Raw file stems of every transcript the store's directory holds — the
    enumeration the lineage snapshot reads its metadata by (a stem is the key
    the store's own reader resolves). Bounded inside the scan, before anything
    is retained. A store that cannot enumerate its directory (test doubles)
    contributes nothing; the real ``ConversationLog`` always can."""
    lister = getattr(log, "transcript_stems_on_disk", None)
    if lister is None:
        return set()
    try:
        return set(lister())
    except OSError:
        # An unlistable directory, or one past the shared bounds: nothing about
        # the lineage is provable, so mark a stem that no session can have and
        # let the reap keep everything.
        return {"<unlistable>"}


def _metadata_line_within_bound(log: Any, key: str) -> bool:
    """Whether transcript ``key``'s first line fits
    :data:`fork_lineage.MAX_METADATA_LINE_BYTES`; the one check every lineage
    reader shares (:func:`fork_lineage.metadata_line_within_bound`).

    The lineage snapshot decodes the metadata line of every transcript in an
    agent-writable directory; ``recorded_chain`` bounds the ``fork_ancestors``
    array only after the decoder has materialized it, so the line is bounded
    FIRST, here.
    """
    return fork_lineage.metadata_line_within_bound(log, key)


def _fork_lineage(log: Any) -> _ForkLineage:
    """Read the fork edges of every transcript in the store.

    Taken BEFORE a permanent delete: an unlinked transcript takes its
    ``forked_from`` with it, which would break any chain that runs through a
    deleted intermediate. Blocking (reads every session's metadata) — call off
    the loop.

    The transcripts are enumerated from the directory itself
    (:meth:`ConversationLog.transcript_stems_on_disk`), not from the catalog
    projection: the projection materializes every row before anything could
    bound it and drops a file whose ``stat`` fails mid-pass, while the directory
    scan is lazy, applies the shared count and name bounds before it retains an
    entry, and raises past them. An unlistable directory (or one past the
    bounds) contributes one unreadable marker, so the reap keeps everything.
    """
    # (stem, forked_from, fork_ancestors, readable)
    entries: list[tuple[str, str | None, list[str], bool]] = []
    for key in sorted(_transcript_stems_on_disk(log)):
        if key == "<unlistable>":
            entries.append((key, None, [], False))
            continue
        if len(key) > fork_lineage.MAX_ANCESTOR_KEY_CHARS:
            entries.append(("<over-bound>", None, [], False))
            continue
        if not _metadata_line_within_bound(log, key):
            # The first line is longer than any metadata record the lineage
            # readers admit, so it is never handed to the JSON decoder: the
            # transcript directory is agent-writable and a huge
            # `fork_ancestors` array would otherwise be materialized (and
            # cached) whole before `recorded_chain` could refuse it.
            entries.append((fork_lineage.fold(key), None, [], False))
            continue
        try:
            meta, ok = log.get_metadata_status(key)
        except Exception:
            meta, ok = {}, False
        if not ok or not isinstance(meta, dict):
            entries.append((fork_lineage.fold(key), None, [], False))
            continue
        parent = fork_lineage.parent_key(meta)
        chain = fork_lineage.recorded_chain(meta)
        if chain is None or parent is fork_lineage.UNPROVABLE:
            # Over the shared bounds or not a string (chain or parent key): real
            # ancestors may be hidden in it, and nothing is materialized from it.
            entries.append((fork_lineage.fold(key), None, [], False))
            continue
        entries.append(
            (
                fork_lineage.fold(key),
                parent if isinstance(parent, str) else None,
                chain,
                True,
            )
        )
    # A `forked_from` key may name a transcript that still lives under a legacy
    # stem (pre-canonical Slack threads); alias each legacy stem onto the
    # canonical one so the catalog entry and the edge meet.
    alias: dict[str, str] = {}
    for _stem, parent, chain, _ok in entries:
        for k in [parent, *chain]:
            if k:
                alias.update(fork_lineage.legacy_aliases(k))
    out = _ForkLineage()
    for stem, parent, chain, ok in entries:
        stem = alias.get(stem, stem)
        out.keys.add(stem)
        if not ok:
            out.unreadable.add(stem)
            continue
        if parent:
            out.parent_of[stem] = fork_lineage.fold(parent)
        if chain:
            out.known_ancestors[stem] = {fork_lineage.fold(a) for a in chain}
    return out


def _ancestors(lineage: _ForkLineage, start: str) -> set[str] | None:
    """Every ancestor of ``start`` in the snapshot — the shared
    :func:`fork_lineage.walk_ancestors` fed by the snapshot's edges, so the
    reap and the asset endpoint share one definition of "ancestor". ``None``
    when the walk ran over the shared bounds (unprovable)."""
    return fork_lineage.walk_ancestors(
        start,
        parent_of=lineage.parent_of.get,
        known_of=lambda s: lineage.known_ancestors.get(s, ()),
    )


def _staged_fork_lineage(staged: Mapping[str, dict[str, Any] | None]) -> _ForkLineage:
    """The trash's transcripts as lineage entries, folded like the catalog's.
    A ``None`` (unreadable) or over-bounds record lands in ``unreadable``."""
    out = _ForkLineage()
    for stem, meta in staged.items():
        folded = fork_lineage.fold(stem)
        out.keys.add(folded)
        if meta is None:
            out.unreadable.add(folded)
            continue
        parent = fork_lineage.parent_key(meta)
        chain = fork_lineage.recorded_chain(meta)
        if chain is None or parent is fork_lineage.UNPROVABLE:
            out.unreadable.add(folded)
            continue
        if isinstance(parent, str):
            out.parent_of[folded] = fork_lineage.fold(parent)
        if chain:
            out.known_ancestors[folded] = {fork_lineage.fold(a) for a in chain}
    return out


def with_staged_lineage(
    lineage: _ForkLineage, staged: Mapping[str, dict[str, Any] | None]
) -> _ForkLineage:
    """``lineage`` plus the edges of ``staged`` transcripts, for a snapshot taken
    BEFORE a permanent delete of staged (trashed) transcripts: the catalog walk
    never sees a trashed fork's ``forked_from``, so without this the ancestors
    that were kept for that fork's sake are never candidates once it is destroyed
    and their copies are orphaned. Unreadable staged records carry over, so the
    reap still fails closed on them."""
    extra = _staged_fork_lineage(staged)
    return _ForkLineage(
        keys=lineage.keys | extra.keys,
        parent_of={**extra.parent_of, **lineage.parent_of},
        known_ancestors={**extra.known_ancestors, **lineage.known_ancestors},
        unreadable=lineage.unreadable | extra.unreadable,
    )


def _image_reap_set(
    before: _ForkLineage,
    after: _ForkLineage,
    deleted: set[str],
    staged: Mapping[str, dict[str, Any] | None] | None = None,
    live: Collection[str] | None = (),
) -> set[str] | None:
    """Which sessions' image copies to reap after ``deleted`` were unlinked.

    A fork copies messages with their ``ts`` intact, and an image copy's slug is
    derived from ``ts`` alone, so a fork renders the very same artifacts as its
    source (and its source's source, …). Therefore:

    * a deleted session with a SURVIVING descendant is kept;
    * an ancestor that is itself already gone from the catalog (it was kept
      earlier because of a fork) is reaped as soon as its last descendant goes;
    * ``None`` (reap nothing) when any lineage metadata was unreadable, because
      an unknown edge could hide a live descendant.

    ``after`` is read once the unlink has happened: a fork acknowledged between
    the snapshot and the delete shows up there, and the merged edge set (fresh
    reads win) is what the decision uses. ``staged`` maps every transcript stem
    sitting in the session trash to its lineage metadata: those are RESTORABLE,
    so they count as survivors AND their edges join the walk — a source staged
    in the trash must not lose its copies when its last live fork is deleted,
    and a staged fork must keep protecting its live source, or a restore would
    render broken images. A staged transcript whose metadata is unreadable
    (``None``) fails the whole decision closed, like an unreadable live one.
    Only emptying the trash (which runs its own reap for what it destroyed) ends
    that protection. ``live`` is the set of session keys that are LIVE in the
    process right now (registered slots, running sessions), folded here: a
    session recreated under a deleted key while the delete ran may not have
    written its transcript yet, so it is absent from ``after`` even though its
    next finalize will register images under that key; it is a survivor, and so
    is everything it descends from. ``None`` for ``live`` (the liveness table
    could not be read) fails the decision closed. Everything is compared as
    transcript stems (:func:`fork_lineage.fold`); the result is every spelling
    (:func:`fork_lineage.owner_spellings`) under which a reaped session's copies
    may be recorded, minus any tab spelling a surviving transcript claims.
    """
    if before.unreadable or after.unreadable:
        return None
    if live is None:
        return None
    staged_lineage = _staged_fork_lineage(staged or {})
    if staged_lineage.unreadable:
        return None
    # A stem staged in the trash AND present in a live snapshot with DIFFERENT
    # lineage is two distinct sessions sharing one stem (a session recreated
    # under a trashed session's key and forked from elsewhere). Neither record
    # may win the merge below: taking the live edge would drop the staged fork's
    # source from the walk, and deleting that source would reap copies the
    # staged fork renders once restored. Fail closed instead; the trash-internal
    # duplicate takes the same path in ``session_storage._retain_staged_lineage``.
    for snapshot in (before, after):
        for stem, staged_parent in staged_lineage.parent_of.items():
            live_parent = snapshot.parent_of.get(stem)
            if live_parent is not None and live_parent != staged_parent:
                return None
        for stem, staged_chain in staged_lineage.known_ancestors.items():
            live_chain = snapshot.known_ancestors.get(stem)
            if live_chain is not None and live_chain != staged_chain:
                return None
    merged = _ForkLineage(
        keys=before.keys | after.keys | staged_lineage.keys,
        parent_of={**before.parent_of, **staged_lineage.parent_of, **after.parent_of},
        known_ancestors={
            **before.known_ancestors,
            **staged_lineage.known_ancestors,
            **after.known_ancestors,
        },
    )
    deleted_stems = {fork_lineage.fold(k) for k in deleted}
    # ``after`` is authoritative for what is live NOW. A deleted stem that is
    # back in it was recreated under the same key while the delete ran (a
    # channel session receiving a new message, a slot re-registered), so its
    # images — including any the new incarnation registered — must stay.
    survivors = set(after.keys) | staged_lineage.keys | {fork_lineage.fold(k) for k in live}
    protected: set[str] = set()
    for s in survivors:
        walked = _ancestors(merged, s)
        if walked is None:
            return None
        protected |= walked
    candidates = set(deleted_stems)
    for d in deleted_stems:
        # Ancestors absent from the catalog were kept for this line's sake.
        walked = _ancestors(merged, d)
        if walked is None:
            return None
        candidates |= {a for a in walked if a not in survivors}
    reap_stems = candidates - protected - survivors
    reap_keys = reap_stems | {k for k in deleted if fork_lineage.fold(k) in reap_stems}
    # The store is keyed by the spelling `register_images` received (the TAB
    # name), so the result is every spelling of every reaped session — EXCEPT a
    # tab spelling that a SURVIVING transcript claims as its own. A linked
    # session's tab (`cron-<id>`, `task-review-<token>`) is also a name any
    # dashboard chat can be given, and that chat's images are owned by that very
    # string; deleting the linked session must not take them. A spelling whose
    # fold is a survivor (and not itself being reaped) is withheld.
    out: set[str] = set()
    for key in reap_keys:
        for spelling in fork_lineage.owner_spellings(key):
            folded = fork_lineage.fold(spelling)
            if folded in survivors and folded not in reap_stems:
                continue
            out.add(spelling)
    return out


#: Reaps deferred behind a straggling image registration. Held here so the task
#: is not garbage-collected mid-flight; each removes itself when done.
_DEFERRED_REAPS: set["asyncio.Task[None]"] = set()
#: How long a deferred reap waits for a straggling registration before giving
#: up loudly. A registration copies local files, so ten minutes is far past any
#: honest run — beyond it the task is wedged and parking the reap behind it
#: forever would just hide the leak.
_DEFERRED_REAP_WAIT_SECS = 600.0


def _live_session_keys(state: "DashboardState") -> set[str] | None:
    """Every session key live in the process right now, in all its spellings.

    The survivor source for the image reap that the catalog cannot provide: a
    slot registered under a just-deleted key whose new incarnation has not yet
    written a transcript. Covers the slot registry keys, each slot's history and
    effective session keys, and the session manager's own registry (channel
    sessions have no slot). ``None`` when any of it cannot be read, which the
    reap treats as unreadable and keeps everything.
    """
    try:
        keys: set[str] = set(state.sessions.session_keys())
        for registry_key, slot in list(state._slots.items()):
            keys.add(registry_key)
            keys.add(slot_history_key(slot))
            keys.add(effective_session_key(slot))
        return keys
    except Exception:
        logger.warning("image cleanup: live session table unreadable", exc_info=True)
        return None


async def _reap_session_images(
    log: Any,
    before: _ForkLineage,
    keys: set[str],
    *,
    live: Callable[[], set[str] | None] | None = None,
) -> None:
    """Delete the untouched chat-image copies of permanently deleted sessions.

    Chat images are exempt from the widget-preview sweep, so this is their ONLY
    reclamation path: every handler that permanently deletes a transcript must
    call it, passing the :func:`_fork_lineage` snapshot it took BEFORE deleting
    and, when it has a :class:`DashboardState`, ``live=lambda:
    _live_session_keys(state)`` so a session recreated under a deleted key
    during the delete is kept even before its first transcript write. ``live``
    is evaluated at decision time (after the drain), on the event loop.
    The decision is :func:`_image_reap_set`; the store singleton's first
    construction does ``resolve``+``mkdir``, so the whole call runs off the loop.
    """
    if not keys:
        return
    # Image registration is dispatched as a detached task at finalize time. A
    # delete issued right after a turn ends must wait for it, or the late copy
    # would land after the reap and outlive the transcript. Registrations are
    # keyed by the slot key (what `register_images` receives). The delete
    # request itself waits a bounded time; if a registration is still running
    # past that, the reap is DEFERRED behind it (a background task with its own,
    # much longer bound) rather than dropped, so the late copy is still reclaimed.
    bare = _image_artifact_session_keys(keys)
    drained = await drain_registrations(bare)
    if not drained:
        logger.warning(
            "image cleanup for %s deferred: an image registration is still running",
            sorted(keys),
        )

        async def _deferred() -> None:
            # Generous second wait: a registration is a local file copy, so
            # anything still running after this is wedged. Say so LOUDLY, then
            # keep waiting for it anyway: the reap is the transcript's only
            # reclamation path, and dropping it would let the copies outlive the
            # deleted transcript for good. This second wait has no timeout: the
            # reap task stays held in _DEFERRED_REAPS until the registration
            # finishes or the process exits.
            if not await drain_registrations(bare, timeout=_DEFERRED_REAP_WAIT_SECS):
                logger.error(
                    "image cleanup for %s still waiting: an image registration is wedged "
                    "(over %ss); the reap runs when it completes",
                    sorted(keys),
                    _DEFERRED_REAP_WAIT_SECS,
                )
                await drain_registrations(bare, timeout=None)
            await _reap_session_images(log, before, keys, live=live)

        task = asyncio.create_task(_deferred())
        _DEFERRED_REAPS.add(task)
        task.add_done_callback(_DEFERRED_REAPS.discard)
        return

    # The GENERATION being reaped is fixed FIRST, as an exact slug snapshot taken
    # off the loop: only copies that exist now can belong to the sessions judged
    # below. Liveness is sampled AFTER it (on the loop, after the drain): a slot
    # re-registered under a deleted key is then either in the live sample (and
    # every copy it registered before the snapshot is protected as a survivor's)
    # or arrived after the snapshot, in which case its copies carry slugs the
    # snapshot does not hold and are kept. Sampling liveness first would leave a
    # window where an incarnation born between the sample and the snapshot has
    # copies in the snapshot but no survivor entry. No clock is compared.
    def _snapshot() -> tuple[Any, dict[str, str]]:
        # The store singleton's first construction does ``resolve`` + ``mkdir``,
        # so it is acquired in the same worker call as the snapshot it feeds.
        st = get_default_store()
        return st, st.auto_image_slugs()

    try:
        store, snapshot = await asyncio.to_thread(_snapshot)
    except Exception:
        logger.warning("image artifact cleanup skipped for %s: store unreadable", sorted(keys))
        return
    live_keys: set[str] | None = live() if live is not None else set()

    def _run() -> int:
        after = _fork_lineage(log)
        # Transcripts staged in the session trash are restorable, so they are
        # survivors here; if the trash cannot be read, nothing is provable and
        # the reap keeps everything (a leak, logged; never a broken restore).
        try:
            staged = _staged_transcript_lineage()
        except Exception:
            logger.warning(
                "image cleanup skipped for %s: session trash unreadable",
                sorted(keys),
                exc_info=True,
            )
            return 0
        reap = _image_reap_set(before, after, keys, staged, live=live_keys)
        if reap is None:
            logger.warning(
                "image cleanup skipped for %s: fork lineage unreadable for %s%s",
                sorted(keys),
                sorted(before.unreadable | after.unreadable),
                "" if live_keys is not None else " (live session table unreadable)",
            )
            return 0
        reaped_stems = {fork_lineage.fold(k) for k in reap}
        kept = {k for k in keys if fork_lineage.fold(k) not in reaped_stems}
        if kept:
            logger.info("image cleanup: keeping copies of %s (surviving forks)", sorted(kept))
        if not reap:
            return 0
        return store.delete_auto_images_for_sessions(
            reap, only_slugs={slug for slug, owner in snapshot.items() if owner in reap}
        )

    try:
        await asyncio.to_thread(_run)
    except Exception:
        logger.warning("image artifact cleanup failed for sessions %s", sorted(keys), exc_info=True)
