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

from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.chat_tag_grants import (
    mint_grant,
    refresh_cache,
    resolve_grant,
    revoke_grant,
)
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json
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

# Valid values for a tag's optional ``agent`` policy field.
_AGENT_TAG_POLICIES: frozenset[str] = frozenset({"add-remove", "add-only", "none"})


def agent_tag_grant(tag: dict[str, Any]) -> tuple[str, bool]:
    """Resolve a tag's ``(agent-write policy, workflow-status bit)``.

    Both values come from the PROTECTED grants store
    (:mod:`kiro_crew.dashboard.chat_tag_grants`), never from the tag dict's own
    fields: ``tags.json`` is agent-writable, so a policy or status read from it
    could be forged by the very party it authorizes and survive restart (GPT
    review finding). The dict parameter is kept so call sites keep passing the
    resolved tag; only its ``id`` is consulted. Rows are minted exclusively by
    the authenticated dashboard tag CRUD (plus a one-time boot seed from the
    code-default workflow-state IDs), and an absent or unreadable row fails closed to
    ``("none", False)`` — human-only, not a workflow state.
    """
    raw_id = tag.get("id")
    tag_id = raw_id if isinstance(raw_id, str) else ""
    return resolve_grant(tag_id)


def agent_tag_policy(tag: dict[str, Any]) -> str:
    """Resolve a tag's agent-write policy: ``"add-remove"`` | ``"add-only"`` | ``"none"``.

    Thin wrapper over :func:`agent_tag_grant` for the call sites that only
    need the policy axis. Shared by the ``chat_tag`` applier and the per-turn
    context injection so the policy lives in one place.
    """
    return agent_tag_grant(tag)[0]


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
    * AUTHORITY-GATED vocabulary intersection — ids are checked against the
      live vocabulary (``state._tags``) only when it is authoritative,
      mirroring the three sibling restore paths (``_tags_authoritative`` is
      False when ``tags.json`` was unreadable at boot). Failing OPEN there is
      load-bearing: intersecting with an unknown (empty) vocabulary would
      drop every id, the next save would persist the loss, and the sticky
      filing marker would block re-inheritance forever. A dangling id kept
      this way is pruned by the restore paths on the next authoritative boot.
    """
    if not isinstance(raw, list):
        return []
    check_vocab = getattr(state, "_tags_authoritative", True)
    # Only string ids enter the vocabulary set: a malformed persisted entry
    # (e.g. a hand-edited tags.json with a list/dict id) must degrade to
    # "unknown id" — not crash the set build with an unhashable type.
    valid_ids = (
        {i for t in state._tags if isinstance(i := t.get("id"), str)} if check_vocab else None
    )
    out: list[str] = []
    seen: set[str] = set()
    for tid in raw:
        if not isinstance(tid, str) or tid in seen:
            continue
        if valid_ids is not None and tid not in valid_ids:
            continue
        seen.add(tid)
        out.append(tid)
    return out


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
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
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


async def persist_tags_snapshot_unlocked(state: DashboardState) -> None:
    """Persist the current state._tags to disk without acquiring the lock.

    Callers MUST hold `tags_write_lock(state)` themselves. Exported for use in
    cross-module critical sections (e.g. chat_auto_tag.maybe_auto_tag).
    """
    snapshot = [dict(t) for t in state._tags]
    await asyncio.to_thread(_write_tags_snapshot, state, snapshot)


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
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            # Roll back in-memory mutation.
            state._tags = [t for t in state._tags if t.get("id") != tag["id"]]
            raise
        if status:
            # Out-of-the-box board behavior: a workflow-state tag created
            # through an authenticated surface is agent-drivable. The grant is
            # minted HERE — the sole write path an agent cannot reach — never
            # derived from the tag's own (agent-writable) fields. A mint
            # failure rolls the CREATE back entirely (memory and disk) and
            # re-raises: returning 201 for a status tag whose grant did not
            # persist would hand the caller a tag that ``chat_tag`` refuses —
            # a divergence between the two stores behind a success code (GPT
            # review finding). Same transactional standard as PATCH/DELETE.
            try:
                await asyncio.to_thread(
                    mint_grant, str(tag["id"]), policy="add-remove", status=True
                )
            except Exception:
                logger.warning("agent-tag grant mint failed for %s", tag["id"], exc_info=True)
                state._tags = [t for t in state._tags if t.get("id") != tag["id"]]
                rollback = [dict(t) for t in state._tags]
                try:
                    await asyncio.to_thread(_write_tags_snapshot, state, rollback)
                except Exception:
                    logger.warning("tag create: vocab rollback persist failed for %s", tag["id"])
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
    # Strict boolean (same reasoning as the PATCH handler): a string "false"
    # coerces truthy and would mint agent authority via the status default.
    status_raw = body.get("status", False)
    if not isinstance(status_raw, bool):
        return web.json_response({"error": "invalid status", "code": "invalid_status"}, status=400)
    status = status_raw
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

    if "agent" in body:
        # The authenticated replacement for the hand-edited ``agent`` field the
        # grants store retired: PATCH is now the only way to set a per-tag
        # agent policy, and it lands in the protected store, not tags.json.
        if not isinstance(body["agent"], str) or body["agent"] not in _AGENT_TAG_POLICIES:
            return web.json_response(
                {"error": "invalid agent policy", "code": "invalid_agent_policy"}, status=400
            )

    if "status" in body and not isinstance(body["status"], bool):
        # Strict boolean only, validated BEFORE the lock and any mutation:
        # bool("false") is True, so coercing a string would mint
        # workflow-state identity — and through the grant transition below,
        # agent authority — from malformed input (GPT review finding).
        # Rejecting AFTER the name/color assignments would leave those
        # rejected mutations live in memory (GPT review finding), so every
        # field is validated before the first write to ``tag``.
        return web.json_response({"error": "invalid status", "code": "invalid_status"}, status=400)

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
            # Validated strictly-boolean BEFORE the lock (see above).
            tag["status"] = body["status"]

        # ── Grant transition (protected store) ──────────────────────────────
        # Derived from the PATCH intent: an explicit ``agent`` value wins;
        # otherwise a ``status`` flip carries the out-of-the-box default.
        # ``agent: "none"`` MINTS a policy-none row rather than revoking, so a
        # human-only workflow state keeps its recorded STATUS bit (revoking
        # would let set_state persist two exclusive states — GPT finding);
        # revocation is reserved for status REMOVAL (and tag deletion).
        # Ordering is fail-closed by construction: the OLD row is revoked
        # before the vocabulary persist (a store failure aborts the PATCH),
        # and the replacement row is minted after it — with a mint failure
        # surfaced as HTTP 500, never swallowed, so a narrowing PATCH
        # (add-remove -> add-only) can never report success while the broader
        # authority silently survives (GPT finding). Between revoke and mint
        # the tag resolves to ("none", False): closed, never open.
        grant_mint: str | None = None
        grant_revoke = False
        if "agent" in body:
            grant_mint = body["agent"]
        elif "status" in body:
            if body["status"]:
                grant_mint = "add-remove"
            else:
                grant_revoke = True

        # The status bit recorded with a mint comes from the validated PATCH
        # body when supplied, otherwise from the EXISTING protected grant —
        # NEVER from agent-writable tags.json, whose ``status`` field an agent
        # can set to launder workflow-state authority through an agent-policy
        # PATCH (GPT review finding). ``prev`` is also the restore point: a
        # failure after the up-front revoke must put the prior grant back
        # rather than leave it permanently revoked (GPT review finding).
        prev_grant: tuple[str, bool] = ("none", False)
        if grant_mint is not None or grant_revoke:
            await asyncio.to_thread(refresh_cache)
            prev_grant = resolve_grant(tid)
        mint_status = bool(body["status"]) if "status" in body else prev_grant[1]

        async def _restore_prev_grant() -> None:
            # Best-effort compensation: re-mint the pre-PATCH grant after a
            # downstream failure. Skipped when there was nothing to restore.
            if prev_grant == ("none", False):
                return
            try:
                await asyncio.to_thread(mint_grant, tid, policy=prev_grant[0], status=prev_grant[1])
            except Exception:
                logger.warning("tag update: grant restore failed for %s", tid, exc_info=True)

        if grant_mint is not None or grant_revoke:
            # Revoke the old row up front in both paths: for a revoke it IS
            # the operation; for a mint it guarantees a store failure after
            # the vocabulary commit leaves the tag closed, not stale-open.
            try:
                await asyncio.to_thread(revoke_grant, tid)
            except Exception:
                state._tags = pre_snapshot
                logger.warning("tag update: grant revoke failed for %s", tid, exc_info=True)
                return web.json_response(
                    {"error": "persist failed", "code": "persist_failed"}, status=500
                )

        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            state._tags = pre_snapshot
            await _restore_prev_grant()
            logger.warning("tag update failed to persist: %s", tid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
        if grant_mint is not None:
            try:
                await asyncio.to_thread(mint_grant, tid, policy=grant_mint, status=mint_status)
            except Exception:
                # A failed PATCH must be a no-op on BOTH stores: put the prior
                # grant back (GPT review finding) AND roll the vocabulary back
                # to the pre-PATCH snapshot in memory and on disk — without
                # the disk write the 500 would leave the rename/status change
                # durable while reporting failure (GPT review finding). The
                # rollback write is best-effort: if it also fails, memory is
                # rolled back and the periodic persist reconverges the file.
                await _restore_prev_grant()
                state._tags = pre_snapshot
                try:
                    await asyncio.to_thread(
                        _write_tags_snapshot, state, [dict(t) for t in pre_snapshot]
                    )
                except Exception:
                    logger.warning("tag update: vocab rollback persist failed for %s", tid)
                logger.warning("tag update: grant mint failed for %s", tid, exc_info=True)
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

    ORDERING: the grant is revoked FIRST (abort on failure — a deleted
    vocabulary row whose grant survives would let an agent resurrect the
    authority by re-creating the id in agent-writable tags.json), then the
    vocabulary (``tags.json``) is persisted as the single durable commit.
    A vocabulary-persist failure re-mints the captured grant (best-effort)
    and rolls memory back. A crash between revoke and persist leaves the tag
    present but closed — fail-closed, never stale-open. A crash after the
    vocabulary commit leaves only dangling tag ids on slots/boards, which
    are harmless and pruned on the next load (see ``_prune_unknown_tag_ids``).
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

        # ── Revoke FIRST, abort on failure ────────────────────────────────
        # Deleting the vocabulary row while its grant survives lets an agent
        # re-create the same id in agent-writable tags.json and inherit the
        # stale authority after reload (GPT review finding). Revoking before
        # the vocabulary commit fails closed: if the revoke fails, nothing
        # has changed and the DELETE aborts; if the vocabulary persist then
        # fails, the captured grant is re-minted (best-effort) so a failed
        # DELETE is a no-op on authority too.
        await asyncio.to_thread(refresh_cache)
        prev_grant = resolve_grant(tid)
        try:
            await asyncio.to_thread(revoke_grant, tid)
        except Exception:
            logger.warning("tag delete: grant revoke failed for %s", tid, exc_info=True)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )

        # ── Single durable commit: remove from vocabulary and persist ────
        state._tags = [t for t in state._tags if t.get("id") != tid]
        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            # Restore memory AND the revoked grant, then abort.
            state._tags.append(removed_tag)
            if prev_grant != ("none", False):
                try:
                    await asyncio.to_thread(
                        mint_grant, tid, policy=prev_grant[0], status=prev_grant[1]
                    )
                except Exception:
                    logger.warning("tag delete: grant restore failed for %s", tid, exc_info=True)
            logger.warning("tag delete: vocab persist failed for %s", tid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )

        # ── Best-effort cleanup: strip the (now nonexistent) id ──────────
        # Failures here are tolerable: a dangling id on disk is pruned on
        # the next load; mark the slot dirty so the periodic flush retries.
        # The grant row was already revoked BEFORE the vocabulary commit
        # (see above) — deletion must never outlive the authority it removes.
        for slot in state._slots.values():
            if tid in slot.tags:
                # Pin the write to the transcript this iteration's membership
                # check covered: the save awaits inside the loop, so a rebind
                # can land mid-persist and the save would otherwise resolve
                # its target from the moved routing at write time. No await
                # between this capture and the strip below.
                authorized_history_key = slot_history_key(slot)
                slot.tags = [t for t in slot.tags if t != tid]
                try:
                    applied = await save_slot_off_loop(
                        state,
                        slot,
                        force=True,
                        best_effort=False,
                        expected_history_key=authorized_history_key,
                    )
                except Exception:
                    slot._dirty = True
                    logger.warning(
                        "tag delete: slot strip persist failed for %s; "
                        "marked dirty for periodic-flush retry",
                        getattr(slot, "key", "?"),
                        exc_info=True,
                    )
                else:
                    if not applied:
                        # Refused without writing (session permanently deleted
                        # or rebound mid-persist). The in-memory strip stands —
                        # the id is already gone from the vocabulary — so mark
                        # dirty and let the periodic flush persist wherever the
                        # slot now routes; a dangling id left on the old
                        # transcript is pruned on the next load.
                        slot._dirty = True
                        logger.warning(
                            "tag delete: slot strip save refused for %s "
                            "(session deleted or rebound); marked dirty for "
                            "periodic-flush retry",
                            getattr(slot, "key", "?"),
                        )

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
                logger.warning("tag delete: board strip persist failed for %s", tid, exc_info=True)

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_delete",
        outcome="allowed",
        source="dashboard",
        resources=tid,
    )
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
            # unconditional restore would erase (the same guard
            # _restore_unfiled applies to its rollback).
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
