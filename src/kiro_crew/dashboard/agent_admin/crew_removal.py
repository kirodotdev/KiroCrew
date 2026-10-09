"""``DELETE /api/agents/{name}``: the record delete, the crew-log unit reclaim and the private-copy prune."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        KiroCrewConfig,
        UnknownMemoryStore,
        _AmbiguousTemplateName,
        _drained_to_thread,
        _get_config_lock,
        _load_template_specs,
        _refresh_session_defaults,
        _remove_avatar_files,
        _require_owner,
        _sel,
        _spec_path_is_safe,
        _unlink_copy_unless_referenced,
        agent_state,
        clear_list_agents_cache,
        coerce_dict_section,
        kiro_agents_dir_path,
        logger,
        memory_store_namespace_lock,
        teams_mod,
        update_config_locked,
    )


def _member_slug_is_claimed(slug: str) -> bool:
    """Whether any crew in the config ON DISK NOW still derives *slug*.

    The authorization for removing a member's crew log, and the whole of it: not
    an age, not a size, not a threshold anyone can tune. A member's unit is keyed
    by its slug, so the only question that makes a removal safe is whether a live
    member still answers to that key.

    Resolved through ``member_slug``, never ``slug_for_name``, and enumerated
    without a name-grammar filter -- both for the reasons
    ``members._slug_is_claimed_by_any_member`` documents for the same question.
    The two spellings disagree for a crew whose persisted ``member_id`` is not
    what its name derives, which provisioning produces deliberately, and the
    create route validates a crew name only for credential shape, so a name the
    roster grammar rejects can still be a live crew. Either mistake reports a
    live owner as gone, and the caller reads "gone" as licence to delete that
    owner's history.

    Fails CLOSED, and the loader is why this needs saying: a ``config.json`` that
    does not parse is not an exception here -- it is logged, marked degraded, and
    the load returns DEFAULTS, so the roster reads empty and an emptiness test
    alone would take that as proof the owner is gone. So an absent owner counts
    only when the file it is absent from was actually read: the whole-config
    degradation marker answers claimed, as does a load that raises, and a crew
    whose own identity will not resolve is skipped rather than allowed to decide.
    Nothing is removed on an error, because the cost of keeping a deleted
    member's log is disk and the cost of the other answer is a live member's
    history.

    One residue, stated rather than guessed at: the loader also normalizes an
    ``agents`` value that is not an object at all to an empty roster without
    marking the file degraded, which this cannot tell from a roster that is
    genuinely empty. It costs at most the ONE slug a caller is deciding -- the
    member whose record the delete already committed -- because the predicate
    answers about that slug alone and never about the tree.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        logger.warning(
            "crew delete: cannot read the roster to decide whether %r is still claimed; "
            "keeping its crew log",
            slug,
            exc_info=True,
        )
        return True
    if DEGRADED_WHOLE_CONFIG in cfg.degraded_sections:
        logger.warning(
            "crew delete: the roster was not readable as a whole, so %r cannot be shown "
            "unclaimed; keeping its crew log",
            slug,
        )
        return True
    for candidate in cfg.agents:
        try:
            if members_mod.member_slug(candidate, cfg) == slug:
                return True
        except Exception:
            # A crew whose persisted identity will not resolve derives no valid
            # slug, so it cannot be the owner of this one. Skipped rather than
            # treated as a claim, the same stance the sibling enumeration takes.
            continue
    return False


def _reclaim_deleted_member_crew_log(name: str, cfg: KiroCrewConfig) -> None:
    """Remove the crew log of a member the roster does not hold. Never raises.

    The crew log is the last thing a deleted member leaves behind. Its config
    record, its memory store, its avatar and its private template copy all go
    with the delete; without this the log stays on disk for the life of the
    installation, with no member to read it for and no other path that collects
    it -- the retention sweep ages SESSION logs from their close entry, and a
    member log has no close to age from.

    *cfg* is the config the caller captured while it still held *name*'s record,
    and the slug is resolved from it: a member carrying an explicit ``member_id``
    keys its log by that id, so resolving the slug once the record is gone would
    fold the name instead and aim at a different unit.

    Call this while holding ``memory_store_namespace_lock``. The guard it hands
    the store re-reads the roster, which on its own makes the decision a snapshot:
    a member id allocated in another process derives the same slug and addresses
    the same unit, and the unlink has no recovery path. That lock is the one seam
    every allocator of a member id shares, so holding it is what keeps the window
    between the decision and the unlink shut.

    Best-effort, like every other step of this teardown. The record is already
    gone by the time this runs, so raising would turn a log that could not be
    collected into a failed delete against a crew that is absent. ``owned`` is
    the ordinary answer while a queued append still holds the lease, and
    ``absent`` the ordinary answer for a member that never wrote one.
    """
    try:
        from kiro_crew.crew_log.store import REMOVE_REMOVED
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.members import member_slug

        slug = member_slug(name, cfg)
        status = get_service().remove_unit(
            slug, still_unclaimed=lambda: not _member_slug_is_claimed(slug)
        )
    except Exception:
        logger.warning("crew delete: could not remove the crew log of %r", name, exc_info=True)
        return
    if status == REMOVE_REMOVED:
        logger.info("crew delete: removed the crew log of %r", slug)
    else:
        logger.info("crew delete: the crew log of %r was not removed (%s)", slug, status)


def _prune_private_copy_of_deleted_crew(crew: str, bound_template: str) -> bool:
    """Remove the private template copy that existed only for a now-deleted crew.

    Corroborated on both sides before anything is touched: the agent_state
    sidecar must name *crew* as the copy's ``private_to`` AND *bound_template*
    (the crew's persisted binding at delete time) must be that copy. Lineage
    alone is not enough — a copy the crew had already moved off may hold
    another crew's customizations — and a binding alone would take a shared
    template away. Best-effort throughout: an unreadable sidecar, an ambiguous
    name, a foreign binding, or a file that will not unlink all leave the copy
    (and its lineage, so private content never lists as shared) in place; the
    crew's removal is already durable and must not fail on cleanup. Returns
    True when the file was removed, so the caller can drop the template cache.
    """
    if not bound_template:
        return False
    agents_dir = kiro_agents_dir_path()
    try:
        _spec, copy_name, _taken, copy_path = _load_template_specs(
            agents_dir, bound_template, "api_kirocrew_agent_delete"
        )
    except _AmbiguousTemplateName:
        logger.debug(
            "crew delete: private copy name %r is ambiguous; leaving file and lineage",
            bound_template,
        )
        return False
    # Lineage is keyed by the copy's declared name while a binding resolves by
    # declared name OR file stem, so the record is looked up under every name
    # that resolves this file — a stem/name divergence must not hide it.
    lineage_keys: list[str] = []
    for key in (bound_template, copy_name, copy_path.stem if copy_path else ""):
        if key and key not in lineage_keys:
            lineage_keys.append(key)
    lineage_key = ""
    for key in lineage_keys:
        fork = agent_state.get_fork_info(key)
        if fork and fork["private_to"] == crew:
            lineage_key = key
            break
    if not lineage_key:
        return False
    if copy_path is None:
        # The name resolved to no READABLE spec. That is not proof the file is
        # gone: a malformed or unreadable copy still sits on disk (the spec
        # reader documents these dirs as user-writable and shared), and with
        # a divergent stem its real filename cannot even be named from here.
        # Pruning lineage on a guess would surface private content as shared
        # once the file is repaired, so the record is kept. A stale record
        # for a file that truly is gone is inert: nothing lists a template
        # without a spec.
        logger.debug(
            "crew delete: private copy %r did not resolve to a readable spec; "
            "keeping its lineage",
            lineage_key,
        )
        return False
    if not _spec_path_is_safe(copy_path, agents_dir):
        return False
    # The deleted crew's record is gone from config, so any binding still
    # resolving the copy is another crew's: the locked helper keeps file and
    # lineage in that case, atomically against concurrent binding writes.
    if _unlink_copy_unless_referenced(copy_path, agents_dir, *lineage_keys) != "deleted":
        return False
    with contextlib.suppress(Exception):
        agent_state.prune(lineage_key)
    return True


async def api_kirocrew_agent_delete(request: web.Request) -> web.Response:
    """DELETE /api/agents/{name} — delete a Kiro Crew agent."""

    denied = await _require_owner(request, "agent.delete")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    async with _get_config_lock():
        cfg = KiroCrewConfig.load()
        if name not in cfg.agents:
            return web.json_response({"error": f"Agent '{name}' not found"}, status=404)
        if name == cfg.default_agent:
            return web.json_response(
                {"error": f"Cannot delete default agent '{name}'. Change default_agent first."},
                status=409,
            )
        # Card-Undo identity guard: a ``crewmate.create`` Undo carries the
        # ``member_id`` its create returned, so a crewmate re-made under the same
        # name is not erased. Enforced inside ``mutate(doc)`` below against the
        # raw record read under the cross-process file lock, not the in-process
        # snapshot. Absent for an ordinary delete.
        from kiro_crew.dashboard.handlers.undo_identity_guard import (
            REQ_CARD_UNDO_CREWMATE_EXPECT,
            UNDO_IDENTITY_CHANGED_CODE,
            UNDO_IDENTITY_CHANGED_MESSAGE,
            identity_mismatch,
        )

        _raw_expect = request.get(REQ_CARD_UNDO_CREWMATE_EXPECT)
        # The parent arms a ``str`` member_id today; tolerate a ``dict`` carrier
        # (``{"member_id": ...}``) in case a future parent widens the shape — the
        # middleware stays the parent's concern. An unarmed delete leaves this
        # ``None`` and the guard adds nothing.
        if isinstance(_raw_expect, dict):
            _expect_val = _raw_expect.get("member_id")
            armed = _expect_val is not None
            expected_member_id = _expect_val if isinstance(_expect_val, str) else None
        elif isinstance(_raw_expect, str):
            armed = True
            expected_member_id = _raw_expect
        else:
            armed = False
            expected_member_id = None

        # A FUNCTION-LOCAL sentinel exception, captured by the nested ``mutate``
        # closure (via a cell, not a module global) so this owner adds no name to
        # the composed handlers namespace. ``mutate`` raises it, which
        # ``update_config_locked`` propagates before the rename, and the route
        # catches it to answer 409 — the mismatch never reaches ``_err500``.
        class _IdentityChanged(Exception):
            pass

        retired_store = ""
        bound_template = ""

        @memory_store_namespace_lock()
        def _delete_member() -> str:
            nonlocal retired_store, bound_template

            def mutate(doc: dict) -> dict:
                nonlocal retired_store, bound_template
                agents = coerce_dict_section(doc, "agents")
                if name not in agents:
                    raise UnknownMemoryStore(f"Crew Member {name!r} was removed concurrently")
                # Identity re-enforced on the RAW record this locked read
                # returned, BEFORE the del and before any crew-log / avatar /
                # private-copy cleanup. ``agents[name]["member_id"]`` is the
                # on-disk id of whatever crew now wears the name; an empty or
                # absent id, or one that differs from the armed expectation,
                # means a replacement — abort with the document untouched.
                if armed:
                    entry0 = agents[name]
                    raw_id = entry0.get("member_id") if isinstance(entry0, dict) else None
                    live_member_id = raw_id if isinstance(raw_id, str) and raw_id else None
                    if identity_mismatch(expected_member_id or None, live_member_id):
                        raise _IdentityChanged(name)
                agent_section = doc.get("agent")
                if doc.get("default_agent") == name or (
                    isinstance(agent_section, dict) and agent_section.get("default_agent") == name
                ):
                    raise UnknownMemoryStore(f"Crew Member {name!r} became the default")
                entry = agents[name]
                stores = coerce_dict_section(doc, "memory_stores")
                store_name = entry.get("memory_store", "") if isinstance(entry, dict) else ""
                record = stores.get(store_name)
                if isinstance(record, dict) and record.get("memory_version") == 2:
                    if record.get("owner_member") != name:
                        raise UnknownMemoryStore(
                            f"memory store {store_name!r} ownership changed concurrently"
                        )
                    retired_store = store_name
                # The PERSISTED binding at delete time, read inside the
                # critical section: it is one half of the corroboration the
                # private-copy cleanup below needs.
                bound = entry.get("kiro_agent") if isinstance(entry, dict) else ""
                bound_template = bound if isinstance(bound, str) else ""
                del agents[name]
                return doc

            # Drop the crew from its team AFTER the registry write has
            # committed and still INSIDE its lock. After the commit, so a
            # config write that fails leaves the membership untouched (the
            # crew stays, on its team); inside the lock, so a same-name create
            # in another process (which needs this same sidecar lock) cannot
            # land between the delete and the drop and have its fresh
            # membership dropped instead. Best-effort (drop_member swallows
            # its own failures): a team entry the drop could not remove is
            # hidden by every reader and purged by the next same-name create.
            def _drop_from_team() -> None:
                teams_mod.drop_member(name)

            update_config_locked(mutate=mutate, after_write=_drop_from_team)
            # And the crew log the member wrote its own history into, decided
            # while this function still holds the namespace lock. The removal
            # turns on a config read, and that hold is the only thing stopping
            # another process from committing a same-name record -- which derives
            # THIS unit -- between the read and the unlink. Nothing rebuilds a
            # crew log, so the window has to be closed rather than narrowed, and
            # the lock is shared with every allocator of a member id. ``cfg`` is
            # the config captured while the record was still in it, which is what
            # the slug has to be resolved from.
            _reclaim_deleted_member_crew_log(name, cfg)
            return retired_store

        try:
            retired_store = await _drained_to_thread(_delete_member)
        except _IdentityChanged:
            # The racing recreate: a replacement crewmate wears the name now, so
            # the locked mutate aborted the write with the document untouched.
            # Answer 409 ``changed_since_apply`` (not a 500) — the same outcome
            # the dashboard renders for every armed-Undo identity mismatch.
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="agent.delete",
                outcome="denied",
                source="dashboard",
                resources=f"{name} {UNDO_IDENTITY_CHANGED_CODE}",
            )
            return web.json_response(
                {"error": UNDO_IDENTITY_CHANGED_MESSAGE, "code": UNDO_IDENTITY_CHANGED_CODE},
                status=409,
            )
        if retired_store:
            from kiro_crew.context import release_cached_memory_store

            await _drained_to_thread(release_cached_memory_store, retired_store)
            if (state := request.app.get("state")) is not None:
                from kiro_crew.dashboard.handlers._shared import release_markdown_memory_store

                await release_markdown_memory_store(state, retired_store)
        # The crew is gone; its uploaded picture must not outlive it. Inside
        # the same lock so the cleanup cannot run AFTER a concurrent
        # same-name recreation has already uploaded and committed a new
        # picture under the same digest stem.
        await _drained_to_thread(_remove_avatar_files, name)
        # Likewise the crew's private template copy: a copy the sidecar marks
        # private to THIS crew, and that the crew was bound to, has no reader
        # left once the record is gone — kept, it lists in the Agent Templates
        # tab under a dead crew's name. Same lock, for the same reason as the
        # avatar: a same-name recreation must not fork a fresh copy only to
        # have this cleanup remove it. Best-effort: a locked or unreadable
        # file never blocks the crew's removal.
        if await _drained_to_thread(_prune_private_copy_of_deleted_crew, name, bound_template):
            clear_list_agents_cache()
            if (state := request.app.get("state")) is not None:
                state.push_refresh("agents")
    # A crew DISAPPEARING is the other half of the same invariant: the captured
    # config still holds the record, so a cron or messaging job still naming the
    # crew would keep resolving its old pin and binding.
    await _refresh_session_defaults(request, name)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="agent.delete",
        outcome="success",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True})
