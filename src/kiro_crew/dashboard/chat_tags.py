"""Session tags — dynamic vocabulary CRUD, slot assignment, and sidebar columns.

Tags are user-defined labels (id/name/color/status) attached to dashboard chat
slots. Sessions can carry multiple tags. The sidebar renders as a horizontal
strip of user-configurable columns. A column filters either by a set of tags
(any/all/none mode) or by the session's live runtime lane — needs-approval,
waiting, working, idle — which is derived from the slot payload rather than
stored on the session. Dragging a session card between columns that carry a
single status tag as filter reassigns the card's status tag; a derived state
lane refuses the drop, because only the agent's own progress moves a card there.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
import weakref
from typing import Any, Callable, TypeVar

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import (
    persist_swept_slot_meta,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.snapshot_commit import (
    commit_snapshot_while_holding_the_lock,
    sweep_to_completion_despite_cancellation,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_NAME_MAX = 60
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_COLOR = "#6b7280"
_VALID_MODES = {"any", "all", "none"}

# A column filters either by tag ("tags", the default) or by the session's live
# runtime state ("state"). A state column names one lane in _VALID_STATE_KEYS;
# membership is derived from the slot payload on every push, so a card moves
# between state lanes on its own and no tag is ever written for it.
_VALID_SOURCES = {"tags", "state"}

# The lane vocabulary. Kept server-side so an unknown key cannot reach the board
# and render an eternally-empty column: the lanes are exhaustive and mutually
# exclusive by construction, and a card that matched nothing would vanish.
_VALID_STATE_KEYS = {"needs_approval", "waiting", "working", "idle"}

_T = TypeVar("_T")


# Per-state tag-write lock. Serializes ALL mutations to state._tags + disk
# persistence so concurrent creates/updates/deletes cannot race the atomic
# fsync/os.replace sequence. Keyed weakly so a discarded state's lock is
# collectable (mirrors _RECONCILE_LOCKS in channel_slots.py).
_TAGS_WRITE_LOCKS: weakref.WeakKeyDictionary[Any, LoopBoundLock] = weakref.WeakKeyDictionary()


def validate_folder_tag_ids(raw: Any, state: DashboardState) -> list[str]:
    """Filter a folder's raw ``tags`` value down to usable tag ids.

    The definition of "a folder tag id a new chat may inherit" for the
    folder/inheritance paths, shared by the dashboard slot-create path
    (chat_handlers), the folder create/PATCH validation (chat_folders), the
    channel first-filing path, and the channel restore read (channel_slots)
    so an inheritance-rule change cannot land on one path and miss the
    others. (Three pre-existing slot-restore prunes elsewhere apply the same
    shape guards inline, and ``api_chat_slot_tags``'s strict inline filter is
    a fourth spelling differing only on the fail-open axis; consolidating all
    four onto this helper — likely via a strict-mode flag — is deliberately
    left to a follow-up. Those sites are outside this change.)
    Callers must invoke it AT THE POINT OF APPLICATION (immediately before the
    ids are written to a slot or persisted), never on a value resolved
    earlier — a tag deletion between resolve and apply would otherwise
    resurrect the deleted id.

    Two guard tiers, deliberately different in when they apply:

    * UNCONDITIONAL shape guards — ``raw`` must be a list (``folders.json``
      is hand-editable; anything else yields ``[]``), and each entry must be
      a string BEFORE the membership test (a non-string entry is unhashable
      and the test itself would raise, turning a malformed store row into a
      500 on every chat created in that folder). Order-preserving,
      de-duplicated.
    * AUTHORITY-GATED vocabulary intersection — DELEGATED to
      ``DashboardState.tag_ids_for_restore``, the one reader every restore path
      shares, which also holds the UNKNOWN-vs-KNOWN rule and why failing open is
      load-bearing. A dangling id kept that way is pruned on the next authoritative
      boot.

      Knownness therefore has exactly ONE encoding, ``_committed_tag_ids``, and this
      helper reads it through the same accessor as everything else — there is no
      second flag to keep in sync and no per-site spelling of the fail-open rule.
      The committed snapshot is also the correct SET to intersect against: live
      ``state._tags`` is a working copy that a mutation moves ahead of disk and a
      failed write restores, so validating against it can admit an id that never
      landed. In normal operation the two agree, because a tag write publishes the
      snapshot as soon as it confirms.
    """
    if not isinstance(raw, list):
        return []
    # Only string ids survive the shape tier: a malformed persisted entry (e.g. a
    # hand-edited folders.json carrying a list or dict id) must degrade to "unknown
    # id" rather than reaching a membership test that would raise on an unhashable
    # type. Order-preserving and de-duplicated.
    out: list[str] = []
    seen: set[str] = set()
    for tid in raw:
        if not isinstance(tid, str) or tid in seen:
            continue
        seen.add(tid)
        out.append(tid)
    return state.tag_ids_for_restore(out)


def _tags_write_lock(state: Any) -> LoopBoundLock:
    """Return (lazily create) the per-state lock for tag writes (loop-bound, #4800)."""
    lock = _TAGS_WRITE_LOCKS.get(state)
    if lock is None:
        lock = LoopBoundLock()
        _TAGS_WRITE_LOCKS[state] = lock
    return lock


# Public accessor — used by the tags/boards HTTP handlers in this module and by
# chat_auto_tag.maybe_auto_tag to hold the lock across an entire
# resolve→merge→persist critical section.
tags_write_lock = _tags_write_lock


async def _mutate_tags_locked(state: DashboardState, mutate: Callable[[], _T]) -> _T:
    """Serialize a tag mutation + persistence under a shared async lock.

    ``mutate`` is a sync callable that modifies ``state._tags`` in place and
    returns a result. After it runs, an immutable snapshot is atomically written
    to disk inside a worker thread (mirrors DashboardState._atomic_write_json).
    The lock is NON-REENTRANT — callers must never nest.

    Raises on persist failure (caller must catch to surface HTTP 5xx).
    The in-memory state is rolled back to the pre-mutate snapshot on failure.
    """
    async with _tags_write_lock(state):
        pre_snapshot = [dict(t) for t in state._tags]
        result = mutate()
        snapshot = [dict(t) for t in state._tags]
        try:
            await _commit_tags_snapshot(state, snapshot)
        except Exception:
            # Roll back to pre-mutate state.
            state._tags = pre_snapshot
            raise
        return result


def _write_tags_snapshot(state: DashboardState, snapshot: list[dict]) -> None:
    """Persist a pre-captured tag snapshot (runs in worker thread).

    Delegates to ``state.save_tags_snapshot`` so the file location resolves
    through ``kiro_crew.dashboard.state.config_dir`` exactly like
    ``save_tags`` (and stays patchable in tests)."""
    state.save_tags_snapshot(snapshot)


async def _commit_tags_snapshot(state: DashboardState, snapshot: list[dict]) -> None:
    """THE choke point for a confirmed tag-vocabulary write: persist, then publish.

    EVERY tag write in this module routes through here, with no exception. Publication is
    UNCONDITIONAL once the write confirms -- there is no knownness test in this helper,
    because by the time a write has landed the vocabulary IS known: these bytes are it.
    Knownness is decided at LOAD instead (``load_tags`` publishes only when the document
    parsed and the file exists), which is the only place the question is open.

    For every other path this makes "the committed vocabulary matches disk" a MECHANISM
    rather than a convention each new call site has to remember. The alternative is a
    publication per call site, which is what allows ``_mutate_tags_locked`` -- a
    confirmed-write helper in the same file -- to leave ``_committed_tag_ids`` stale
    while disk moves on. Staleness in the
    missing-a-live-id direction is the damaging one, because the adopt and restore paths
    PRUNE against this set: an id on disk but absent from the snapshot is stripped off
    the user's slots and the next save makes that loss durable. Mirrors
    ``mutate_folders`` on the folder side.

    Deliberately NOT inside ``_write_tags_snapshot``. That function is the sync body
    handed to ``asyncio.to_thread``, and the suites replace it wholesale to simulate a
    pop mid-write and an ``IOError`` -- so a publication living in there would be
    patched out exactly where the vocabulary matters most, and would also mutate state
    from a worker thread. Publishing HERE keeps it on the event loop and keeps the
    patched seam meaning what it says.

    ORDER IS THE POINT: ``save_tags_snapshot`` raises on failure, so publication is
    reached only when the bytes landed and *snapshot* IS the on-disk vocabulary. A
    failure leaves the committed set untouched, leaving each caller's own rollback to
    restore the live list.

    Publication is NOT gated on the vocabulary already being KNOWN. After a malformed
    ``tags.json`` the field is ``None``, and such a gate would skip the very write that
    ends the ignorance, leaving it ``None`` for the life of the process: every restore
    then fails open, and a tag deleted after that load is retained on resumed slots as a
    dangling id no sweep reaches. The loader itself fails open, which is correct there --
    it genuinely does not know what an unparsable file contains. ``mutate_folders``
    publishes on the same rule, so both vocabularies obey one.

    CANCELLATION-ATOMIC. ``asyncio.to_thread`` hands the body to a worker that cannot
    be interrupted, so cancelling the awaiting handler does NOT stop the write -- the
    bytes still land. Awaiting it bare therefore loses the publication on cancellation:
    disk moves to *snapshot* while ``_committed_tag_ids`` keeps the old set, which is
    the exact missing-a-live-id staleness this helper exists to prevent, and the
    fork/restore validators then prune a valid assignment. The protocol that answers
    this -- shield, publish as a statement on both exits, drain under the lock -- lives in
    ONE place, ``commit_snapshot_while_holding_the_lock``, which the folder store calls too
    so the two vocabularies cannot drift apart.

    No ``rollback`` is passed: this helper's caller (``_mutate_tags_locked``) restores
    ``state._tags`` on ``except Exception``, and the shared helper propagates the
    write's own failure rather than the cancellation precisely so that arm is reached.
    """
    write = asyncio.ensure_future(asyncio.to_thread(_write_tags_snapshot, state, snapshot))
    await commit_snapshot_while_holding_the_lock(
        write,
        publish=lambda: state.publish_committed_tag_ids(snapshot),
    )


async def persist_tags_snapshot_unlocked(state: DashboardState) -> None:
    """Persist the current state._tags to disk without acquiring the lock.

    Callers MUST hold `tags_write_lock(state)` themselves. Exported for use in
    cross-module critical sections (e.g. chat_auto_tag.maybe_auto_tag).
    """
    snapshot = [dict(t) for t in state._tags]
    await _commit_tags_snapshot(state, snapshot)


def _valid_color(value: str) -> str:
    return value if _COLOR_RE.match(value) else _DEFAULT_COLOR


def _tag_by_id(state: DashboardState, tag_id: str) -> dict | None:
    return next((t for t in state._tags if t.get("id") == tag_id), None)


def create_tag_definition(
    state: DashboardState,
    name: str,
    color: str | None = None,
    *,
    status: bool = False,
) -> dict:
    """Create a new tag definition (sync, no lock).

    Shared by the async locked callers. Mutates state._tags in place.
    Returns the created tag dict.
    """
    clean_name = name.strip()[:_NAME_MAX]
    if not clean_name:
        raise ValueError("tag name must not be empty")
    clean_color = _valid_color(str(color or _DEFAULT_COLOR))
    tag = {
        "id": uuid.uuid4().hex[:12],
        "name": clean_name,
        "color": clean_color,
        "order": len(state._tags),
        "status": bool(status),
    }
    state._tags.append(tag)
    return tag


async def create_tag_definition_off_loop(
    state: DashboardState,
    name: str,
    color: str | None = None,
    *,
    status: bool = False,
) -> dict:
    """Async wrapper: creates a tag definition under the shared write lock.

    Acquires the per-state tag-write lock (shared with update/delete) so
    concurrent callers creating the same missing tag name produce exactly one
    definition. The sync fsync write runs in a worker thread while the lock is
    held.

    Raises on persist failure so the caller can surface HTTP 5xx.
    """
    async with _tags_write_lock(state):
        # Re-check under lock: another caller may have just created it.
        lower = name.strip().lower()
        existing = next(
            (t for t in state._tags if (t.get("name") or "").lower() == lower),
            None,
        )
        if existing:
            return existing
        tag = create_tag_definition(state, name, color, status=status)
        snapshot = [dict(t) for t in state._tags]
        try:
            await _commit_tags_snapshot(state, snapshot)
        except Exception:
            # Roll back in-memory mutation.
            state._tags = [t for t in state._tags if t.get("id") != tag["id"]]
            raise
        return tag


# ── Tag vocabulary ─────────────────────────────────────────────────────────


async def api_chat_tags(request: web.Request) -> web.Response:
    """GET /api/chat/tags — list all tag definitions."""
    state: DashboardState = request.app["state"]
    return web.json_response(sorted(state._tags, key=lambda t: t.get("order", 0)))


async def api_chat_tag_create(request: web.Request) -> web.Response:
    """POST /api/chat/tags — create a new tag."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Redact credential / exfiltration patterns from the user-supplied name
    # BEFORE truncation — truncating first can slice a credential that
    # straddles the length cut into a fragment the scanners no longer
    # recognize, persisting a raw prefix. Same boundary pattern as other
    # LLM-authored sinks.
    name = (body.get("name") or "").strip()
    name, _ = redact_exfiltration_urls(name)
    name, _ = redact_credentials(name)
    name = name.strip()[:_NAME_MAX]
    if not name:
        return web.json_response({"error": "name required", "code": "name_required"}, status=400)
    color = str(body.get("color") or _DEFAULT_COLOR)
    status = bool(body.get("status", False))
    try:
        tag = await create_tag_definition_off_loop(state, name, color, status=status)
    except Exception:
        logger.warning("tag create failed to persist: %s", name)
        return web.json_response({"error": "persist failed", "code": "persist_failed"}, status=500)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_create",
        outcome="allowed",
        source="dashboard",
        resources=str(tag["id"]),
    )
    return web.json_response(tag, status=201)


async def api_chat_tag_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/tags/{id} — rename / recolor / reorder.

    Resolution of the tag dict happens INSIDE the write lock so a concurrent
    DELETE cannot remove the tag between lookup and mutation (preventing a
    silent lost update on a detached dict).
    """
    state: DashboardState = request.app["state"]
    tid = request.match_info["id"]
    # Early unlocked check for fast 404 on obviously invalid ids (avoids
    # JSON parse + lock contention for non-existent tags).
    if not _tag_by_id(state, tid):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    if "name" in body:
        new_name = str(body["name"]).strip()[:_NAME_MAX]
        if not new_name:
            return web.json_response(
                {"error": "name required", "code": "name_required"}, status=400
            )

    async with _tags_write_lock(state):
        # Re-resolve under lock — a concurrent DELETE may have removed it.
        tag = _tag_by_id(state, tid)
        if not tag:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)

        pre_snapshot = [dict(t) for t in state._tags]

        if "name" in body:
            # Redact BEFORE truncation (same reasoning as tag create).
            safe_name = str(body["name"]).strip()
            safe_name, _ = redact_exfiltration_urls(safe_name)
            safe_name, _ = redact_credentials(safe_name)
            safe_name = safe_name.strip()[:_NAME_MAX]
            if not safe_name:
                # Parity with create: never persist an empty name (e.g. the
                # whole input was a redacted credential).
                return web.json_response(
                    {"error": "name required", "code": "name_required"}, status=400
                )
            tag["name"] = safe_name
        if "color" in body:
            tag["color"] = _valid_color(str(body["color"]))
        if "order" in body:
            try:
                tag["order"] = int(body["order"])
            except (TypeError, ValueError, OverflowError):
                pass
        if "status" in body:
            tag["status"] = bool(body["status"])

        snapshot = [dict(t) for t in state._tags]
        try:
            await _commit_tags_snapshot(state, snapshot)
        except Exception:
            state._tags = pre_snapshot
            logger.warning("tag update failed to persist: %s", tid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
        updated = tag

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_update",
        outcome="allowed",
        source="dashboard",
        resources=tid,
    )
    return web.json_response(updated)


async def api_chat_tag_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/tags/{id} — delete a tag; strip it from all slots.

    Holds the per-state tag-write lock across the ENTIRE sequence so a
    concurrent tag_session directive cannot resolve the tag between the
    vocabulary removal and the slot strip.

    CRASH-ATOMIC ordering: the vocabulary (``tags.json``) is persisted FIRST.
    Once that single write commits, the deletion is durable — a crash at any
    later point leaves only dangling tag ids on slots/boards, which are
    harmless and pruned on the next load (see ``_prune_unknown_tag_ids``).
    If the vocabulary write fails, nothing else has been touched, so the
    in-memory removal is simply rolled back and 500 returned. No multi-write
    compensation is needed in either direction.
    """
    state: DashboardState = request.app["state"]
    tid = request.match_info["id"]
    if not _tag_by_id(state, tid):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)

    async with _tags_write_lock(state):
        # Re-check under lock — another concurrent delete may have won.
        removed_tag = _tag_by_id(state, tid)
        if not removed_tag:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)

        # CAPTURED BEFORE THE WRITE, NOT AFTER, and the ordering is the whole point.
        # The strip pass below reads ``state._slots`` AFTER the vocabulary write has
        # awaited, so it cannot see a slot a concurrent close popped DURING that
        # await (``chat_handlers.py`` ``_slots.pop``, then its own
        # ``save_slot_off_loop`` await). When that save FAILS the close handler puts
        # the SAME object back, still carrying this tag id, and the periodic flush
        # then full-saves it from memory -- so the deleted id reaches disk. A later
        # pass cannot repair it either: the object is in no snapshot while it is out.
        # It is pruned on the next AUTHORITATIVE boot, because the restore validators
        # intersect against the committed vocabulary; it therefore persists as
        # in-session staleness, and survives restarts only for as long as that
        # vocabulary reads UNKNOWN, where the fail-open rule keeps every id.
        #
        # A capture only READS, so it is safe to take before the commit: if the write
        # below raises, this list is discarded having mutated nothing.
        #
        # Mirrors ``chat_folders``' pre-commit capture for the identical hazard.
        closing: list[Any] = [s for s in state._slots.values() if tid in s.tags]

        # The committed vocabulary AS IT STANDS BEFORE THIS DELETE, captured so the
        # adopt callback below does not read the live field. Publication happens at the
        # write rather than at the end of this handler, because for the whole strip
        # ``_committed_tag_ids`` would otherwise still advertise the deleted id: a slot
        # RESUMED in that window -- in neither the capture above nor the live view the
        # strip reads -- prunes against a set that admits the id, keeps it, and persists
        # it as a dangling tag no later sweep touches. Capturing here lets the adopt keep
        # validating against exactly the vocabulary this transaction started from.
        pre_delete_committed = getattr(state, "_committed_tag_ids", None)

        # ── Single durable commit: remove from vocabulary and persist ────
        state._tags = [t for t in state._tags if t.get("id") != tid]
        snapshot = [dict(t) for t in state._tags]
        # CAPTURED, NOT PROPAGATED, then CONFIRMED below -- a cancellation can arrive
        # before any write, so it does not prove the removal landed.
        commit_cancellation: BaseException | None = None
        try:
            await _commit_tags_snapshot(state, snapshot)
        except asyncio.CancelledError as exc:
            commit_cancellation = exc
        except Exception:
            # Nothing else has been written — restore memory and abort.
            state._tags.append(removed_tag)
            logger.warning("tag delete: vocab persist failed for %s", tid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
        if commit_cancellation is not None:
            # Publication proves the write confirmed; ``state._tags`` was edited before it
            # and is not rolled back, so it cannot serve. UNKNOWN refuses too.
            committed_after = getattr(state, "_committed_tag_ids", None)
            if committed_after is None or tid in committed_after:
                state._tags.append(removed_tag)
                raise commit_cancellation

        # Publication happened inside the helper, immediately after the write confirmed,
        # so no window exists in which the committed vocabulary still admits the deleted
        # id -- a slot RESUMED during the strip below would otherwise prune against a set
        # that still contained it, keep it, and persist it as a dangling tag no later
        # sweep touches. Everything below that needs the PRE-delete vocabulary uses
        # ``pre_delete_committed``, captured above, so the publication cannot disturb it.
        # That separation is what lets this write use the shared helper at all: the
        # ordering and the known-ness rule are the helper's, and the only thing local to
        # this handler is which vocabulary the adopt validates against.

        # ── Best-effort cleanup: strip the (now nonexistent) id ──────────
        # Failures here are tolerable: a dangling id on disk is pruned on
        # the next load; mark the slot dirty so the periodic flush retries.
        # Snapshot the view: the ``await`` below yields INSIDE this iteration and
        # other coroutines mutate ``state._slots`` while it runs, so iterating the
        # live view risks "dictionary changed size during iteration" — which here
        # would raise out of the handler AFTER the tag row was already deleted,
        # leaving the strip half-applied.
        #
        # TWO PASSES, mirroring chat_folders; see the protocol in
        # ``docs/system-specs/modules/history.md`` under "Vocabulary Deletes and Slot
        # Metadata Persistence". Do not restate it here; a second copy is what drifts.
        stripped: list[Any] = []
        # The captured slots first, then the live view. A slot in both is visited
        # twice and that is harmless: the first visit removes the id, so the second
        # takes the ``continue`` below and cannot double-append.
        for slot in [*closing, *state._slots.values()]:
            if tid not in slot.tags:
                continue
            # Strip in memory UNCONDITIONALLY; the in-memory strip writes nothing
            # so it cannot erase a close, and doing it before the identity test
            # (pass two) means a slot that was merely TRANSIENTLY absent — popped by
            # an in-flight close whose save then FAILS, so the close handler restores
            # it — comes back without the deleted tag id. That holds for a pop on
            # EITHER side of the vocabulary write: after it, because the object is
            # still in the view this loop reads; before it, because ``closing``
            # captured the object itself and stripping it reaches the same instance
            # the close handler will put back.
            slot.tags = [t for t in slot.tags if t != tid]
            stripped.append((slot, slot_history_key(slot)))
        # Pass two delegates the shared persist protocol -- metadata-only merge,
        # superseded adoption, dirty-on-transient, log-never-raise -- to
        # ``persist_swept_slot_meta`` so this strip cannot drift from the folder
        # one. See its docstring for why a full save would clobber a close. The
        # identity re-check lives INSIDE that helper, not here, so a sweep site
        # cannot omit it -- the same arrangement the folder sweep relies on.
        #
        # The GUARD confirms, under the record's own lock, that it still carries the
        # tag being deleted. Without it the merge writes a tag list decided from an
        # in-memory read, so a retag landing in the window is overwritten -- and an
        # absent record is UPSERT-ed back into existence by ``update_metadata``,
        # resurrecting deleted history.

        def _still_carries_the_deleted_tag(meta: dict) -> bool:
            return bool(meta) and tid in (meta.get("tags") or [])

        def _adopt_observed_tags(slot: Any, observed: dict, submitted: dict[str, object]) -> None:
            # CONDITIONAL on the slot still holding what was SUBMITTED. ``observed``
            # is what the record's lock saw BEFORE the await, so a retag landing
            # inside the window is newer than it; adopting unconditionally would
            # replace the newer list with the older one. When it changed, the newer
            # list stands and ``_dirty`` stays untouched -- whoever retagged owns
            # persisting it.
            if list(slot.tags) != submitted.get("tags"):
                return
            # TYPE **and** MEMBERSHIP. ``observed`` is persisted metadata, and
            # iterating it directly is unsafe in a way a per-entry ``isinstance``
            # cannot catch: a ``str`` persisted here is itself iterable, so
            # ``for t in "t1"`` yields ``'t'`` and ``'1'`` -- both ``str``, both
            # ``!= tid`` -- and single characters become live tag ids. A dict
            # contributes its KEYS the same way. So the container must BE a list
            # before anything is read out.
            #
            # Membership then mirrors the vocabulary prune the restore paths already
            # perform, including its fail-open rule: an unreadable ``tags.json`` is
            # not evidence of absence, and pruning against it would wipe every
            # assignment. Type is checked regardless -- fail-open covers an
            # untrustworthy VOCABULARY, never a structurally invalid value.
            #
            # Validated against the COMMITTED vocabulary, not live ``state._tags``.
            # The live list is a working copy: a tag mutation applies to it in memory
            # before persisting and is rolled back if that write raises, and this
            # handler itself strips the deleted id from it above. So it can be ahead
            # of disk in one direction and about to be restored in the other, and
            # adopting against it can import an id that never landed.
            #
            # Validated against ``pre_delete_committed`` -- the committed vocabulary as
            # this transaction found it -- rather than by re-reading the live field. Both
            # exclude uncommitted state, but the capture is also STABLE: it cannot be
            # moved by this handler's own publication, so the adopt's meaning no longer
            # depends on when that publication happens. ``None`` is UNKNOWN and fails
            # open; an empty frozenset is a KNOWN-empty vocabulary and does prune.
            observed_tags = observed.get("tags")
            if isinstance(observed_tags, list):
                kept_tags = [t for t in observed_tags if isinstance(t, str) and t != tid]
                committed = pre_delete_committed
                if committed is not None:
                    kept_tags = [t for t in kept_tags if t in committed]
            else:
                kept_tags = []
            slot.tags = kept_tags

        async def _persist_stripped() -> None:
            for slot, pinned_key in stripped:
                # The identity re-check lives inside ``persist_swept_slot_meta``: a
                # concurrent close can pop this slot while we are suspended.
                await persist_swept_slot_meta(
                    state,
                    slot,
                    # Captured per slot, so the adoption above can tell whether a retag
                    # landed while we were suspended.
                    {"tags": list(slot.tags)},
                    guard=_still_carries_the_deleted_tag,
                    adopt=_adopt_observed_tags,
                    label="tag delete",
                    expected_history_key=pinned_key,
                )

        # CAPTURED, not propagated: the helper drains the sweep and then RE-RAISES by
        # contract, so leaving it uncaught skips the dereference below and the audit.
        sweep_cancellation: BaseException | None = None
        try:
            await sweep_to_completion_despite_cancellation(_persist_stripped())
        except asyncio.CancelledError as exc:
            sweep_cancellation = exc

        # ── Best-effort cleanup: strip the deleted id from folders ───────
        # A folder can carry tags (copied onto new chats filed into it); the
        # deleted id must not linger there. Best-effort like the slot strip:
        # mutate_folders takes its own store lock (distinct from the tag-write
        # lock held here, so no re-entrancy), and a failure leaves a dangling id
        # that the next folder load ignores. The callback returns
        # (changed, None) so the store is rewritten only when something changed.
        def _strip_folder_tag(folders: list[dict]) -> tuple[bool, None]:
            changed = False
            for f in folders:
                tags = f.get("tags")
                if isinstance(tags, list) and tid in tags:
                    stripped = [t for t in tags if t != tid]
                    if stripped:
                        f["tags"] = stripped
                    else:
                        # Empty clears the key, holding "absent means no tags".
                        f.pop("tags", None)
                    changed = True
            return changed, None

        async def _strip_the_deleted_id_from_folders_and_boards() -> None:
            try:
                await state.mutate_folders(_strip_folder_tag)
            except Exception:
                # Dangling folder reference — pruned on next load.
                logger.warning("tag delete: folder strip persist failed for %s", tid, exc_info=True)

            # Strip from sidebar columns (flat list of column dicts).
            changed_boards = False
            for col in state._tag_boards:
                tag_ids = col.get("tag_ids") or []
                filtered = [t for t in tag_ids if t != tid]
                if len(filtered) != len(tag_ids):
                    col["tag_ids"] = filtered
                    changed_boards = True
            if changed_boards:
                try:
                    boards_snapshot = [dict(c) for c in state._tag_boards]
                    await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snapshot)
                except Exception:
                    # Dangling board reference — pruned on next load.
                    logger.warning(
                        "tag delete: board strip persist failed for %s", tid, exc_info=True
                    )

        # SHIELDED, not merely reordered: the task is still cancelled, so these awaits
        # raise again and ``except Exception`` cannot catch a ``CancelledError``.
        # CAPTURED for the same reason as the first sweep: the helper re-raises once
        # drained, which would escape before the audit below. See history.md.
        try:
            await sweep_to_completion_despite_cancellation(
                _strip_the_deleted_id_from_folders_and_boards()
            )
        except asyncio.CancelledError as exc:
            if sweep_cancellation is None:
                sweep_cancellation = exc

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_delete",
        outcome="allowed",
        source="dashboard",
        resources=tid,
    )
    if commit_cancellation is not None:
        raise commit_cancellation
    if sweep_cancellation is not None:
        raise sweep_cancellation
    return web.json_response({"ok": True})


# ── Slot tag assignment ────────────────────────────────────────────────────


async def api_chat_slot_tags(request: web.Request) -> web.Response:
    """PUT /api/chat/slots/{slot}/tags — replace the slot's tag list.

    Holds the tags write lock across validate→assign→persist so a concurrent
    DELETE cannot remove a tag id between validation and slot persistence
    (preventing dangling references).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    # Capture the transcript key the lookup above just covered, BEFORE the
    # body-parse and lock awaits: ``linked_session_key`` is rebound on
    # already-live slots with no ``running`` gate (cron completions, workflow
    # injections), so a slow caller can be authorized against its own session
    # and land on somebody else's conversation. The re-check below and the
    # save's expected_history_key pin together keep this request's write on
    # the transcript it was authorized against.
    authorized_history_key = slot_history_key(slot)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    raw_ids = body.get("tags")
    if not isinstance(raw_ids, list):
        return web.json_response(
            {"error": "tags must be an array", "code": "tags_not_array"}, status=400
        )

    async with _tags_write_lock(state):
        valid_ids = {t.get("id") for t in state._tags}
        new_tags: list[str] = []
        for tid in raw_ids:
            if isinstance(tid, str) and tid in valid_ids and tid not in new_tags:
                new_tags.append(tid)
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # the same slot OBJECT must still be registered under the name, and
        # its routing must still resolve to the transcript captured before
        # the first await — a rebind in either window means this request's
        # authorization no longer covers the write target.
        if state._slots.get(name) is not slot or slot_history_key(slot) != authorized_history_key:
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_tags",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"},
                status=409,
            )
        prior_tags = slot.tags
        slot.tags = new_tags
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live field — but only while
            # it still holds THIS request's value: the write span awaits, so
            # a concurrent writer may have committed a newer value that an
            # unconditional restore would erase (the same compare-and-set rule
            # every rollback here applies).
            if slot.tags == new_tags:
                slot.tags = prior_tags
            # The UNPINNED periodic flush may have persisted the provisional
            # value to the slot's current transcript while this save awaited
            # (review-caught): mark dirty so the next flush reconverges the
            # durable record to the rolled-back live state.
            slot._dirty = True
            sel().log_api_access(
                caller="dashboard",
                operation="chat.slot_tags",
                outcome="denied",
                source="dashboard",
                resources=name,
                error="session was deleted or rebound",
            )
            return web.json_response(
                {"error": "session was deleted or rebound", "code": "session_gone"},
                status=409,
            )

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_tags",
        outcome="allowed",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "tags": slot.tags})


# ── Sidebar columns (Trello-style filtered lanes) ──────────────────────────


def _normalize_column(
    state: DashboardState, raw: Any, *, existing: dict | None = None
) -> dict | None:
    """Validate + coerce a column payload. Returns None if invalid.

    An unrecognized tag id is a REJECTION, not a silent drop: quietly
    discarding it while returning 200 lands the column back on
    ``tag_ids: []`` — the deliberate match-all state — so a stale or buggy
    client presents as "the filter does nothing" with no signal anywhere.
    An empty list stays valid: it is the documented match-all/clear-filter
    state the board UI depends on.

    ``source`` discriminates the two column kinds. ``"tags"`` (the default, and
    what every column persisted without the field is read as) filters by
    ``tag_ids``/``mode``. ``"state"`` filters by the session's live runtime lane
    named in ``state_key`` and ignores the tag fields entirely.
    """
    if not isinstance(raw, dict):
        return None
    valid_ids = {t.get("id") for t in state._tags}
    cleaned: dict[str, Any] = dict(existing or {})
    if "tag_ids" in raw:
        tag_ids = raw.get("tag_ids") or []
        if not isinstance(tag_ids, list):
            return None
        if not all(isinstance(t, str) and t in valid_ids for t in tag_ids):
            return None
        cleaned["tag_ids"] = [str(t) for t in tag_ids]
    if "mode" in raw:
        mode = str(raw.get("mode") or "any")
        if mode not in _VALID_MODES:
            return None
        cleaned["mode"] = mode
    if "name" in raw:
        cleaned["name"] = str(raw.get("name") or "").strip()[:_NAME_MAX]
    if "order" in raw:
        order_val = raw.get("order")
        if order_val is not None:
            try:
                cleaned["order"] = int(order_val)
            except (TypeError, ValueError):
                pass
    if "include_untagged" in raw:
        cleaned["include_untagged"] = bool(raw.get("include_untagged"))
    if "source" in raw:
        source = str(raw.get("source") or "tags")
        if source not in _VALID_SOURCES:
            return None
        cleaned["source"] = source
    if "state_key" in raw:
        state_key = str(raw.get("state_key") or "")
        if state_key and state_key not in _VALID_STATE_KEYS:
            return None
        cleaned["state_key"] = state_key
    cleaned.setdefault("mode", "any")
    cleaned.setdefault("tag_ids", [])
    cleaned.setdefault("name", "")
    cleaned.setdefault("order", 0)
    cleaned.setdefault("include_untagged", False)
    cleaned.setdefault("source", "tags")
    cleaned.setdefault("state_key", "")
    # A state column without a lane key would match nothing, and a tag column
    # carrying one would claim a lane it does not filter by. Refuse both rather
    # than coercing: a silently-corrected column reads as a broken filter.
    if cleaned["source"] == "state" and not cleaned["state_key"]:
        return None
    if cleaned["source"] == "tags" and cleaned["state_key"]:
        return None
    return cleaned


async def api_chat_tag_columns(request: web.Request) -> web.Response:
    """GET /api/chat/tag-columns — list sidebar column layout."""
    state: DashboardState = request.app["state"]
    return web.json_response(sorted(state._tag_boards, key=lambda c: c.get("order", 0)))


def _state_lane_owner(
    state: DashboardState, state_key: str, *, exclude_id: str | None = None
) -> dict | None:
    """Return the existing column already holding ``state_key``, or None.

    A lane is a singleton by nature: two columns naming the same runtime state
    would render every matching session twice and double its count. Uniqueness
    therefore has to be decided HERE, inside ``_tags_write_lock``, because that
    is the only place the board is serialized. A client cannot enforce it — any
    check it makes is against a cached column list, so two dashboards (or one
    with a stale cache) can both conclude a lane is missing and both create it.
    """
    if not state_key:
        return None
    for col in state._tag_boards:
        if col.get("source") != "state" or col.get("state_key") != state_key:
            continue
        if exclude_id is not None and col.get("id") == exclude_id:
            continue
        return col
    return None


async def api_chat_tag_column_create(request: web.Request) -> web.Response:
    """POST /api/chat/tag-columns — append a new sidebar column."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    async with _tags_write_lock(state):
        column = _normalize_column(state, {**body, "order": len(state._tag_boards)})
        if column is None:
            return web.json_response(
                {"error": "invalid column payload", "code": "invalid_column_payload"}, status=400
            )
        if column["source"] == "state":
            # Creating a lane is an ENSURE, not an append: a caller asking for a
            # lane that already exists gets the existing one back with 200 rather
            # than a duplicate. This is what makes a retry (or two dashboards
            # racing) converge instead of persisting two identical lanes, and it
            # holds because the decision is made under the write lock.
            owner = _state_lane_owner(state, column["state_key"])
            if owner is not None:
                existing = dict(owner)
                sel().log_api_access(
                    caller="dashboard",
                    operation="chat.tag_column_create",
                    outcome="allowed",
                    source="dashboard",
                    resources=f"{existing.get('id')} (existing lane {column['state_key']})",
                )
                return web.json_response(existing, status=200)
        column["id"] = uuid.uuid4().hex[:12]
        state._tag_boards.append(column)
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            state._tag_boards = [c for c in state._tag_boards if c.get("id") != column["id"]]
            logger.warning("tag column create failed to persist: %s", column["id"])
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_column_create",
        outcome="allowed",
        source="dashboard",
        resources=str(column["id"]),
    )
    return web.json_response(column, status=201)


async def api_chat_tag_column_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/tag-columns/{id} — rename / retag / reorder."""
    state: DashboardState = request.app["state"]
    cid = request.match_info["id"]
    column = next((c for c in state._tag_boards if c.get("id") == cid), None)
    if not column:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    async with _tags_write_lock(state):
        # Re-check under lock (column may have been deleted concurrently).
        column = next((c for c in state._tag_boards if c.get("id") == cid), None)
        if not column:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        merged = _normalize_column(state, body, existing=column)
        if merged is None:
            return web.json_response(
                {"error": "invalid column payload", "code": "invalid_column_payload"}, status=400
            )
        if merged["source"] == "state":
            # Same uniqueness rule as create, but an update carries no ENSURE
            # intent -- silently collapsing it onto the existing lane would
            # discard the caller's other edits -- so this is a refusal.
            owner = _state_lane_owner(state, merged["state_key"], exclude_id=cid)
            if owner is not None:
                sel().log_api_access(
                    caller="dashboard",
                    operation="chat.tag_column_update",
                    outcome="rejected",
                    source="dashboard",
                    resources=cid,
                    error=f"lane {merged['state_key']} already exists",
                )
                return web.json_response(
                    {"error": "lane already exists", "code": "duplicate_state_lane"}, status=409
                )
        original = dict(column)
        column.update(merged)
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            column.clear()
            column.update(original)
            logger.warning("tag column update failed to persist: %s", cid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_column_update",
        outcome="allowed",
        source="dashboard",
        resources=cid,
    )
    return web.json_response(column)


async def api_chat_tag_column_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/tag-columns/{id} — remove a column."""
    state: DashboardState = request.app["state"]
    cid = request.match_info["id"]
    if not any(c.get("id") == cid for c in state._tag_boards):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    async with _tags_write_lock(state):
        # Re-check under lock.
        removed = [c for c in state._tag_boards if c.get("id") == cid]
        if not removed:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        state._tag_boards = [c for c in state._tag_boards if c.get("id") != cid]
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            state._tag_boards.extend(removed)
            logger.warning("tag column delete failed to persist: %s", cid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_column_delete",
        outcome="allowed",
        source="dashboard",
        resources=cid,
    )
    return web.json_response({"ok": True})


async def api_chat_tag_columns_reorder(request: web.Request) -> web.Response:
    """PUT /api/chat/tag-columns/order — reorder columns by id list."""
    state: DashboardState = request.app["state"]
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    ids = body.get("ids")
    if not isinstance(ids, list):
        return web.json_response(
            {"error": "ids must be an array", "code": "ids_not_array"}, status=400
        )
    async with _tags_write_lock(state):
        original_order = [(col.get("id"), col.get("order", 0)) for col in state._tag_boards]
        order_map = {str(cid): i for i, cid in enumerate(ids)}
        # Push columns not present in the reorder payload past the explicit
        # ordering so they don't collide with the new sequential indices.
        next_order = len(order_map)
        for col in state._tag_boards:
            cid = col.get("id")
            if cid in order_map:
                col["order"] = order_map[cid]
            else:
                col["order"] = next_order
                next_order += 1
        state._tag_boards.sort(key=lambda c: c.get("order", 0))
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            # Restore original order.
            for col in state._tag_boards:
                for cid, order in original_order:
                    if col.get("id") == cid:
                        col["order"] = order
                        break
            state._tag_boards.sort(key=lambda c: c.get("order", 0))
            logger.warning("tag columns reorder failed to persist")
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_columns_reorder",
        outcome="allowed",
        source="dashboard",
        resources=",".join(str(x) for x in ids[:10]),
    )
    return web.json_response({"ok": True})


# ── Drag-drop: move a session between columns (reassigns status tags) ──────


async def api_chat_slot_drop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/drop — move a session into a column.

    Destination rule: if the target column's tag_ids contains exactly one
    *status* tag, strip every status tag from the slot and add that one.
    Non-status tags are preserved. Any other configuration is a no-op so users
    can have filter-only columns without accidental data loss. A derived state
    lane is refused outright — its membership follows the session's runtime
    state and no tag write can place a card there.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    # Capture the transcript key the lookup above just covered, BEFORE the
    # body-parse await — the same rebind window PUT /tags documents. The
    # re-check below and the save's expected_history_key pin together keep
    # this request's write on the transcript it was authorized against.
    authorized_history_key = slot_history_key(slot)
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    column_id = str(body.get("column_id") or "")

    def _rejected(reason: str) -> web.Response:
        sel().log_api_access(
            caller="dashboard",
            operation="chat.slot_drop",
            outcome="rejected",
            source="dashboard",
            resources=f"{name}->{column_id}",
            error=reason,
        )
        return web.json_response({"ok": False, "reason": reason, "tags": slot.tags})

    # Serialize the whole resolve/re-check/mutate/persist/rollback span under
    # the same lock every other tags writer holds (PUT /tags, the tag-delete
    # strip, auto-tag). Two reasons, both review-caught: (1) with awaits
    # inside the span, a second concurrent writer would capture this one's
    # value as its rollback snapshot, and value-based rollback cannot tell
    # "my write survived" from "someone else wrote the same value" — a
    # refused drop could then erase an equal, acknowledged concurrent commit;
    # (2) the column and tag vocabulary must be RESOLVED under the lock too,
    # or a tag deletion completing while this request waits on the lock is
    # resurrected — the stale target id would be re-added and persisted onto
    # the slot after the vocabulary commit removed it.
    async with _tags_write_lock(state):
        column = next((c for c in state._tag_boards if c.get("id") == column_id), None)
        if not column:
            return web.json_response(
                {"error": "column not found", "code": "column_not_found"}, status=404
            )
        tag_index = {t["id"]: t for t in state._tags}
        if column.get("source") == "state":
            # A state lane's membership is derived from the session's own
            # runtime state, so there is nothing to write that would move the
            # card there — only the agent reaching that state moves it. Refuse
            # rather than silently reassigning tags the lane does not filter by.
            return _rejected("column is a derived state lane")
        col_tags = [tag_index[t] for t in column.get("tag_ids") or [] if t in tag_index]
        status_tags = [t for t in col_tags if t.get("status")]
        if len(status_tags) != 1:
            # Column doesn't carry exactly one LIVE status tag — there's no
            # unambiguous status to assign on drop, so this is a visual no-op
            # (covers the unfiltered, multi-status, and target-tag-deleted
            # cases alike; tag_index was built under the lock, so a tag
            # deleted while this request waited is already absent here).
            return _rejected("column is not a status lane")
        target_id = status_tags[0]["id"]
        # Re-authorize after the awaits above (body parse, lock acquisition):
        # same slot OBJECT still registered under the name, routing still on
        # the transcript captured before the first await. No await between
        # this check and the save dispatch; the persist window itself is
        # covered by the save's pin.
        if state._slots.get(name) is not slot or slot_history_key(slot) != authorized_history_key:
            return _rejected("session was deleted or rebound")
        kept = [t for t in slot.tags if t in tag_index and not tag_index[t].get("status")]
        prior_tags = slot.tags
        written_tags = kept + [target_id]
        slot.tags = written_tags
        if not await save_slot_off_loop(
            state, slot, force=True, expected_history_key=authorized_history_key
        ):
            # Refused without writing: the session was permanently deleted or
            # rebound mid-persist. Roll back the live field — but only while
            # it still holds THIS request's value, so a non-endpoint writer's
            # newer commit is not erased — and report the drop as rejected,
            # matching this endpoint's rejection shape (the card stays where
            # it was).
            if slot.tags == written_tags:
                slot.tags = prior_tags
            # The UNPINNED periodic flush may have persisted the provisional
            # value while this save awaited (review-caught): mark dirty so the
            # next flush reconverges the durable record to the live state.
            slot._dirty = True
            return _rejected("session was deleted or rebound")
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_drop",
        outcome="allowed",
        source="dashboard",
        resources=f"{name}->{column_id}",
    )
    return web.json_response({"ok": True, "tags": slot.tags})
