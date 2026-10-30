"""Opening a crewmate's pinned DM thread: ``POST /api/members/{slug}/thread``.

The idempotent get-or-create of the thread: which crew owns the slug, the derived slot
key and its memory generation, the refusals that keep one crew's transcript from being
handed to another, the private-store pin, the workspace the thread follows, the title
it is named with, the binding it records, and the approval mode its profile seeds.

Served through :mod:`kiro_crew.dashboard.handlers.members`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig, default_project_dir
from kiro_crew.config.sections import MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
from kiro_crew.dashboard.chat_persistence import (
    pin_private_agent_store,
    rehydrate_slot_from_history_async,
)
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers.members_access import (
    _deny_app_caller,
    _member_names_for_slug,
    _sel,
)
from kiro_crew.dashboard.state import DashboardState, request_slot_origin
from kiro_crew.members import MemberSlugError

logger = logging.getLogger("kiro_crew.dashboard.handlers.members")


def _member_thread_slot(cfg, member: str, slug: str) -> tuple[str, str]:
    """Choose the member's deterministic DM key without opening learned memory."""
    from kiro_crew.memory_stores import require_member_memory_store

    store = require_member_memory_store(cfg, member, require_directory=False)
    record = cfg.memory_stores.get(store)
    if record is None or record.memory_version != 2:
        return members_mod.member_slot_key(slug), ""
    return members_mod.member_slot_key(slug, store), store


def _member_workspace_project(cfg: KiroCrewConfig, member: str) -> tuple[str, str]:
    """Resolve a crew member's bound workspace: ``(workspace name, project dir)``.

    The member's ``workspace`` name, falling back to ``default_workspace`` when
    that name is undeclared, through the shared ``default_project_dir``
    validation -- so provider cwd and project essentials refer to the same
    directory. Blocking filesystem IO (realpath, isdir, sensitive-path fence):
    callers run it off the event loop.
    """
    member_workspace = cfg.agents[member].workspace
    if member_workspace not in cfg.workspaces:
        member_workspace = cfg.default_workspace
    return member_workspace, default_project_dir(member_workspace)


async def _bind_member_workspace(
    state: DashboardState, slot: Any, cfg: KiroCrewConfig, member: str
) -> None:
    """Point a member thread at its crew's configured workspace.

    A member chat has no project concept. The slot's ``project`` field is
    where the dashboard (Files tab, terminal cwd, project essentials) and the
    provider cwd read a directory from, and for a member thread that directory
    IS the workspace the crew is bound to in ``agents.<name>.workspace`` --
    derived from config on every open, never a per-thread choice read back
    from the transcript. That is what makes a thread written before member
    slots carried the directory (restored with ``project == ""``, so the
    pinned Files tab read "no project directory") converge on the right one,
    and what makes a crew whose binding was later edited follow it.

    Resolution runs off the event loop; the write is a compare-and-set so an
    already-bound slot is not re-marked dirty, and a changed binding reaches
    disk through the next periodic flush and the client through a push. When
    the directory CHANGES -- including from empty to bound: a thread that ran
    a turn while its workspace directory was missing has a provider warm in
    the fallback work dir -- the deferred provider reset is armed exactly as
    every other writer of ``slot.project`` arms it (``_set_project``,
    ``api_chat_slot_project``): a warm kiro-cli process keeps the cwd it was
    spawned in, so without the reset the agent would keep writing files in
    the old directory while the Files tab shows the new one. The reset is
    deferred, so an active turn is never torn down; the next message
    cold-starts in the new directory.
    """
    member_workspace, project = await asyncio.to_thread(_member_workspace_project, cfg, member)
    if not project:
        # The workspace directory is missing or fenced: nothing to point at.
        # ``default_project_dir`` already logged the fall-through; leave the
        # thread as it is rather than clearing a directory the user can see.
        return
    if slot.workspace == member_workspace and slot.project == project:
        return
    old_project = slot.project
    slot.workspace = member_workspace
    slot.project = project
    if old_project != project:
        slot._pending_reset_history_key = effective_session_key(slot)
    slot._dirty = True
    state.push_slots_update()


async def _guard_and_pin_private_store(
    state: "DashboardState",
    slug_owners: list[str],
    member_store: str,
    canonical_key: str,
    member_name: str,
    cfg: "KiroCrewConfig",
) -> "tuple[str, web.Response | None]":
    """Run the v2 slug-collision guard and private-store pin.

    Returns ``(assigned_store, None)`` on success and ``("", error_response)``
    when the collision guard fires (409) or the pin raises (503).  The caller
    is responsible for setting ``slot._memory_assignment_from_history`` and
    for applying ``slot.memory_store = assigned_store`` on success.

    Both the pre-mint path (no slot yet) and the post-publication lock path
    (existing slot under ``slot._lock``) call this helper so the collision
    guard and the 503 mapping stay in one place.
    """
    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response
    from kiro_crew.member_memory_auth import read_private_session_store

    try:
        if (
            len(slug_owners) != 1
            and (await asyncio.to_thread(read_private_session_store, canonical_key)) != member_store
        ):
            return "", web.json_response(
                {
                    "error": "choose distinct member names before opening this private thread",
                    "code": "member_pin_mismatch",
                },
                status=409,
            )
        assigned = await pin_private_agent_store(state, canonical_key, member_name, cfg)
        return assigned, None
    except Exception as exc:
        return "", _store_unavailable_response(member_store, exc)


async def api_member_thread(request: web.Request) -> web.Response:
    """POST /api/members/{slug}/thread — idempotent get-or-create of a DM thread.

    Returns the thread's slot key. Safe to call every time the page opens a
    member: an existing binding and slot are returned as-is; a missing half is
    re-created (the slot key is a pure derivation of the slug, so re-creation
    always converges on the same thread).
    """
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await _deny_app_caller(request, "members.thread")
    if denied is not None:
        return denied
    # An app token is already refused above with the members routes' existence-hiding
    # 404. This gate covers the other half: a dashboard token with an empty app
    # identity but a non-owner subject (the `!dashboard` Slack case), which
    # would otherwise bind a session slot to a crew member. Kept below the app
    # denial so app callers keep the 404 they get on every other member route.
    owner_denied = await require_owner_dashboard_request(request, "members.thread")
    if owner_denied is not None:
        return owner_denied
    state: DashboardState | None = request.app.get("state")
    if state is None:
        return web.json_response(
            {"error": "dashboard state unavailable", "code": "state_unavailable"}, status=503
        )
    slug = request.match_info["slug"]
    try:
        members_mod.validate_slug(slug)
    except MemberSlugError:
        return web.json_response(
            {"error": "invalid member slug", "code": "invalid_member_slug"}, status=400
        )

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    binding = await asyncio.to_thread(members_mod.read_dm_binding, slug)

    # The bound member wins as long as it still exists AND still derives this
    # slug — dm.json's `member` field is operator-editable state, so it is
    # honored only when the registry independently corroborates it (the name
    # exists and folds to the slug being opened). This keeps a colliding
    # slug's thread stably attributed to whoever bound it first. With no
    # binding at all, the first crew in config order whose name derives this
    # slug takes the thread. An uncorroborated binding resolves to that same
    # crew here — but only far enough to look up its slot; the branches below
    # refuse the open rather than rebinding the slug to it.
    slug_owners = _member_names_for_slug(cfg, slug)
    if binding is not None and binding.get("member") in slug_owners:
        member_name = binding["member"]
    elif slug_owners:
        member_name = slug_owners[0]
    else:
        member_name = ""
    slot_key, generation = "", ""
    if member_name:
        try:
            slot_key, generation = await asyncio.to_thread(
                _member_thread_slot, cfg, member_name, slug
            )
        except Exception as exc:
            from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

            return _store_unavailable_response(cfg.agents[member_name].memory_store, exc)
    if binding is not None:
        if binding.get("member") not in slug_owners:
            # The binding names a crew absent from the registry (renamed, or
            # deleted). It still derives this slug — `read_dm_binding`
            # refuses any binding that does not — so falling through to a
            # same-slug successor here would hand it the SAME derived key,
            # and with it the previous crew's entire transcript, rendered
            # under the successor's name with the pin chip vouching for it.
            # The live-slot mismatch check below cannot catch this (after a
            # restart no live slot exists), so the refusal must key off the
            # BINDING itself. Fail closed, leave dm.json untouched
            # (re-entrant), and let the user resolve it in the crew manager.
            # A binding whose slug has no owner left lands here too, and is
            # refused the same way rather than reaching the 404 below.
            try:
                _sel().log_api_access(
                    caller=request.remote or "",
                    operation="member_thread_open",
                    outcome="denied",
                    source="member_pin",
                    resources=f"slug={slug}",
                    error="binding names a crew outside the slug's owners",
                )
            except Exception:  # pragma: no cover - audit must never change the outcome
                logger.debug("SEL audit for member_pin denial failed", exc_info=True)
            return web.json_response(
                {
                    "error": "the thread is bound to a crew the registry no longer names",
                    "code": "member_pin_mismatch",
                },
                status=409,
            )
    else:
        # No binding, but the canonical history key already holds a
        # transcript: rebinding here would hand whoever currently derives the
        # slug the PREVIOUS occupant's entire conversation (ChatPane hydrates
        # from disk history by key). Attribution is lost with the binding —
        # it is not re-derivable when names collide — so fail closed and let
        # the user resolve it (delete the old thread from History, or restore
        # the crew). A member key with NO history binds fresh as usual.
        if member_name and state.conversation_log is not None:
            _log = state.conversation_log
            _history_key = members_mod.member_thread_session_alias(slug, generation)
            # STRUCTURAL existence, not metadata truthiness: get_metadata
            # answers {} for both "never persisted" and "present but
            # malformed/unreadable", and treating the second as the first
            # would rebind the slug and hand the on-disk transcript to the
            # successor the moment its metadata line is corrupt.
            _history_exists = await asyncio.to_thread(_log.has_log, _history_key)
            if _history_exists:
                try:
                    _sel().log_api_access(
                        caller=request.remote or "",
                        operation="member_thread_open",
                        outcome="denied",
                        source="member_pin",
                        resources=f"slug={slug}",
                        error="orphan history: binding gone, transcript survives",
                    )
                except Exception:  # pragma: no cover - audit must never change the outcome
                    logger.debug("SEL audit for member_pin denial failed", exc_info=True)
                return web.json_response(
                    {
                        "error": "this thread's history exists but its binding is gone",
                        "code": "member_binding_missing",
                    },
                    status=409,
                )
    if not member_name:
        return web.json_response(
            {"error": "no crew member for this slug", "code": "member_not_found"}, status=404
        )

    slot = state._slots.get(slot_key)
    if slot is None:
        # A dormant thread (gateway restart outside the restore window, or a
        # thread the user closed) still has its canonical transcript on disk.
        # Minting a bare slot here would reopen the DM with EMPTY in-memory
        # context — the next reply would run without any prior conversation.
        # Rehydrate first: the restore path resolves identity from dm.json
        # (never transcript metadata) and reads off the event loop.
        # adopt_closed: this endpoint IS the deliberate reopen path for a
        # member thread, so a ✕-closed transcript reopens with its history.
        slot = await rehydrate_slot_from_history_async(state, slot_key, adopt_closed=True)
    minted = False
    if slot is None:
        member_workspace, project = await asyncio.to_thread(
            _member_workspace_project, cfg, member_name
        )
        # Resolve before publication, then re-check: another opener can create
        # the slot while path validation waits; that slot is kept and bound to
        # the same workspace below (both openers derive it from one config).
        slot = state._slots.get(slot_key)
        if slot is None:
            # Pin the v2 private store BEFORE minting the slot.  A refused
            # pin returns without ever creating a slot, leaving nothing
            # registered or broadcast.  A concurrent opener can still create
            # the slot during the awaits below, so we re-check after them:
            # if it is already there we fall through to the non-minted path,
            # which validates it under the slot lock as usual.
            _pre_member_store = getattr(cfg.agents[member_name], "memory_store", "")
            _pre_store_record = (
                cfg.memory_stores.get(_pre_member_store) if _pre_member_store else None
            )
            _pre_assigned = ""
            if _pre_store_record is not None and _pre_store_record.memory_version == 2:
                _pre_canonical = members_mod.member_thread_session_alias(slug, generation)
                _pre_assigned, _pre_err = await _guard_and_pin_private_store(
                    state,
                    slug_owners,
                    _pre_member_store,
                    _pre_canonical,
                    member_name,
                    cfg,
                )
                if _pre_err is not None:
                    return _pre_err
            # Re-check: a concurrent opener may have published the same key
            # during the awaits above.  If so, fall through to the non-minted
            # path so the lock does its full existing-slot validation.
            slot = state._slots.get(slot_key)
            if slot is None:
                minted = True
                with state.suspend_slots_push():
                    slot = state.get_or_create_slot(
                        name=slot_key,
                        agent=member_name,
                        workspace=member_workspace,
                        mode=members_mod.DM_SLOT_MODE,
                        origin=request_slot_origin(request.get("app", "")),
                    )
                    slot.project = project
                    if _pre_assigned:
                        # Stamp synchronously before the slot is visible:
                        # no await follows inside this block, so no other
                        # code can observe the slot before the assignment.
                        slot._memory_assignment_from_history = True
                        slot.memory_store = _pre_assigned
    if slot.mode != members_mod.DM_SLOT_MODE:
        # The derived key is already occupied by a foreign slot (mode is set at
        # creation only, so a pre-existing non-member slot keeps its own). Never
        # adopt it: speaking into it would not be the member's pinned thread.
        return web.json_response(
            {"error": "slot key occupied by a non-member session", "code": "member_slot_conflict"},
            status=409,
        )
    if not slot.agent:
        # A member slot is only ever born with its crew pinned; an empty agent
        # here means the slot predates the binding (e.g. restored from history
        # metadata that lost it). Nothing has run as anyone on it, so adopting
        # the resolved member is a pure repair with no session semantics.
        slot.agent = member_name
    elif slot.agent != member_name:
        # The registry moved under the binding (crew renamed/deleted with a
        # same-slug successor). Re-pinning here would be an agent switch that
        # skips every invariant the real switch endpoint holds (slot lock,
        # workspace/project re-resolution, pending-wait unblocking, metadata
        # persistence, client broadcast) — so FAIL CLOSED instead and leave
        # the binding untouched, keeping this branch re-entrant: the user
        # resolves it in the crew manager (restore the name, or delete the
        # thread), and until then the thread refuses to speak as anyone else.
        try:
            _sel().log_api_access(
                caller=request.remote or "",
                operation="member_thread_open",
                outcome="denied",
                source="member_pin",
                resources=f"slug={slug}",
                error="live slot pinned to a crew the registry no longer names",
            )
        except Exception:  # pragma: no cover - audit must never change the outcome
            logger.debug("SEL audit for member_pin denial failed", exc_info=True)
        return web.json_response(
            {
                "error": "the thread is pinned to a crew the registry no longer names",
                "code": "member_pin_mismatch",
            },
            status=409,
        )

    # The thread is named after its crewmate, as a final title no background
    # path rewrites (see chat_title.name_member_thread). The first crewmate with no
    # display name reads "Mate", as it does in the sidebar.
    from kiro_crew.agent_files import ASSISTANT_DEFAULT_DISPLAY_NAME, is_assistant_member
    from kiro_crew.dashboard.chat_title import name_member_thread

    named_crew = cfg.agents[member_name]
    name_member_thread(
        state,
        slot,
        getattr(named_crew, "display_name", "")
        or (
            ASSISTANT_DEFAULT_DISPLAY_NAME
            if is_assistant_member(member_name, named_crew)
            else member_name
        ),
    )

    if not minted:
        # Live (restart-restored) and rehydrated threads alike: the fresh mint
        # above already carries the binding, every other path converges here.
        await _bind_member_workspace(state, slot, cfg, member_name)

    member_store = getattr(cfg.agents[member_name], "memory_store", "")
    store_record = cfg.memory_stores.get(member_store) if member_store else None
    if store_record is not None and store_record.memory_version == 2 and not minted:
        from kiro_crew.member_memory_auth import read_private_session_store

        canonical_key = members_mod.member_thread_session_alias(slug, generation)

        async def reopen_bound_thread() -> web.Response:
            """Return a running member thread validated against current ownership.

            Read-only: it writes no assignment and does not touch the turn. Both
            reachable running paths share it, so the opener that finds a turn
            already in flight and the one whose turn starts during its slot-lock
            wait are answered by the same validation rather than by two.
            """
            from kiro_crew.dashboard.chat_persistence import member_store_ownership_holds
            from kiro_crew.dashboard.handlers.agents import _get_config_lock
            from kiro_crew.memory_stores import memory_store_namespace_lock

            # Do not hold the slot lock while waiting for config: member
            # updates already take config before slot.
            try:
                assigned_store = await asyncio.to_thread(read_private_session_store, canonical_key)
            except Exception as exc:
                from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                return _store_unavailable_response(member_store, exc)
            async with _get_config_lock(), slot._lock:

                @memory_store_namespace_lock()
                def current_binding():
                    current = KiroCrewConfig.load()
                    if not member_store_ownership_holds(current, member_name, member_store):
                        return None
                    return members_mod.read_dm_binding(slug)

                try:
                    binding = await asyncio.to_thread(current_binding)
                except Exception as exc:
                    from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                    return _store_unavailable_response(member_store, exc)
                if (
                    state._slots.get(slot_key) is not slot
                    or effective_session_key(slot) != canonical_key
                    or slot.agent != member_name
                    or slot.mode != members_mod.DM_SLOT_MODE
                    or slot.memory_store != member_store
                    or assigned_store != member_store
                    or binding is None
                    or binding.get("slot_key") != slot.key
                    or binding.get("member") != member_name
                ):
                    error = "member thread changed or its private binding is inconsistent"
                    if effective_session_key(slot) != canonical_key:
                        error = "the member thread is linked to another session"
                    elif slot.memory_store != member_store or assigned_store != member_store:
                        error = "the member thread has a different member memory assignment"
                    return web.json_response(
                        {"error": error, "code": "member_slot_conflict"},
                        status=409,
                    )
                return web.json_response(
                    {"slot_key": slot.key, "slug": slug, "member": member_name}
                )

        if slot.running:
            return await reopen_bound_thread()
        running_after_wait = False
        async with slot._lock:
            if effective_session_key(slot) != canonical_key:
                return web.json_response(
                    {
                        "error": "the member thread is linked to another session",
                        "code": "member_slot_conflict",
                    },
                    status=409,
                )
            try:
                if slot.running:
                    # The turn started while this opener waited for the slot.
                    # Its entry snapshot cannot authorize reuse, and taking the
                    # config lock here would invert the config -> slot order,
                    # so re-enter the running path once after this lock is
                    # released: it validates current ownership under config
                    # then slot, which is the answer this window deserves
                    # rather than a conflict the caller has to retry through.
                    running_after_wait = True
                else:
                    # The owner selected this slug, not an editable transcript
                    # or DM binding. A collision needs a protected assignment.
                    slot._memory_assignment_from_history = True
                    assigned_store, _lock_err = await _guard_and_pin_private_store(
                        state,
                        slug_owners,
                        member_store,
                        canonical_key,
                        member_name,
                        cfg,
                    )
                    if _lock_err is not None:
                        return _lock_err
            except Exception as exc:
                from kiro_crew.dashboard.handlers.memory import _store_unavailable_response

                return _store_unavailable_response(member_store, exc)
            if not running_after_wait:
                if slot.running:
                    # A turn started while this opener awaited the namespaced
                    # assignment above. ``slot._lock`` does not exclude it:
                    # ``api_chat`` reads ``slot.running`` and publishes
                    # ``slot.task`` without taking the slot lock, so the window
                    # is the await, not the lock. Publishing an assignment onto
                    # a thread whose turn is already in flight is the write this
                    # route exists to avoid, so drop it -- the same treatment
                    # the pre-assignment window above gets -- and answer through
                    # the read-only reopen path instead. The assignment
                    # ``pin_private_agent_store`` already published cannot
                    # repoint that turn: ``bind_session_execution`` compares the
                    # record it read and refuses a differing member or store.
                    running_after_wait = True
                elif (
                    state._slots.get(slot_key) is not slot
                    or effective_session_key(slot) != canonical_key
                    or slot.agent != member_name
                ):
                    return web.json_response(
                        {
                            "error": "member thread changed during assignment",
                            "code": "member_slot_conflict",
                        },
                        status=409,
                    )
                else:
                    slot.memory_store = assigned_store
        if running_after_wait:
            return await reopen_bound_thread()

    created = (
        binding is None
        or binding.get("slot_key") != slot.key
        or binding.get("member") != member_name
    )
    if created:
        try:
            await asyncio.to_thread(
                lambda: members_mod.write_dm_binding(
                    slug, member=member_name, slot_key=slot.key, memory_store=generation
                )
            )
        except OSError:
            logger.warning("failed to persist dm binding for %r", slug, exc_info=True)
            return web.json_response(
                {
                    "error": "could not persist thread binding",
                    "code": "member_binding_write_failed",
                },
                status=500,
            )
        # Record the binding in the member's append-only log. The trust-file
        # write above is the security fence and stays authoritative; this is
        # the durable projection input for the roster's slot_key. Best-effort
        # and off-loop (ensure/append are synchronous file IO); a logging fault
        # never fails a binding the fence already persisted.

        def _emit_binding() -> None:
            from kiro_crew import eventlog_hooks
            from kiro_crew.eventlog.types import MEMBER_BINDING

            eventlog_hooks.emit(slug, member_name, MEMBER_BINDING, {"slot_key": slot.key})

        await asyncio.to_thread(_emit_binding)

    if not slot._member_approval_seeded:
        # Re-read, not the snapshot loaded at entry: a profile pick saved during
        # the awaits above must decide the seed. No await between this read and
        # the seed, so a later pick's slot write always lands after it.
        fresh = await asyncio.to_thread(KiroCrewConfig.load)
        crew = fresh.agents.get(member_name)
        if crew is not None:
            _seed_member_approval(state, slot, crew.approval_mode)
    return web.json_response({"slot_key": slot.key, "slug": slug, "member": member_name})


def _seed_member_approval(state: DashboardState, slot: Any, mode: str) -> None:
    """Give a crewmate's thread the approval mode its profile names, once per live slot.

    ``mode`` is the crew record's ``approval_mode``; ``""`` means the user never
    chose one, and the thread opens in ``trust``. A slot that already holds a
    grant keeps it, and the seed runs once per in-memory slot, so a later change
    on this slot (the profile's own write, an approval card's "trust this
    session") stands. Slot trust is not persisted: a restart seeds again from
    the record, which is where the profile writes the user's choice.
    """
    if slot._member_approval_seeded:
        return
    slot._member_approval_seeded = True
    effective = mode or MEMBER_APPROVAL_MODE_UNSET_EFFECTIVE
    if effective == "normal" or slot._trust or slot._trust_reads or slot._trust_scope:
        return
    key = effective_session_key(slot)
    for sharing in state._slots.values():
        if effective_session_key(sharing) == key:
            if effective == "trust":
                sharing._trust = True
            else:
                sharing._trust_reads = True
            # The Profile decides this slot's mode, not its creator's changes.
            sharing._posture_inherited = False
    state.sessions.set_approval_policy(key, "auto" if effective == "trust" else "")
    try:
        _sel().log_api_access(
            caller="dashboard:member_profile",
            operation=f"mode_change:{effective}",
            outcome="enabled",
            resources=slot.key,
        )
    except Exception:  # pragma: no cover - audit must never change the outcome
        logger.warning("SEL audit failed for member approval seed", exc_info=True)
