"""The one-time crewmate prune at startup.

The barrier that holds mutating requests until the pass settles, the pass itself, the
wait every session writer takes before it binds a crewmate, and the channel transcript
merge whose copies are removed only once the pass has returned.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _CREWMATE_PRUNE_GATE_HELD_PREFIXES,
        _CREWMATE_PRUNE_GATE_SAFE_METHODS,
        _CREWMATE_PRUNE_GATE_TIMEOUT_S,
        DashboardState,
        logger,
        migrate_channel_transcripts,
        prune_synced_crewmates,
    )


def _claimed_dashboard_slots(state: DashboardState) -> frozenset[str]:
    """Slot names the persisted session map holds a ``dashboard:`` session for.

    Read off the live map so the transcript migration can tell a real dashboard
    session from an orphan of a same-named channel session. Blocking (reads the
    map file), so callers on the event loop must offload it.
    """
    try:
        sessions = getattr(state, "sessions", None)
        smap = getattr(sessions, "_session_map", None)
        data = getattr(smap, "_data", None)
        if not isinstance(data, dict):
            return frozenset()
        return frozenset(k[len("dashboard:") :] for k in data if k.startswith("dashboard:"))
    except Exception:
        logger.debug("could not read claimed dashboard slots", exc_info=True)
        return frozenset()


def _register_change_card_hook(app: web.Application) -> None:
    """Innermost middleware on the settings routes a change card's plan may name.

    Registered after the explicit chain, so the auth middleware has already
    classified the caller. Only a request whose matched route is in
    ``change_card_catalog.HOOKED_ROUTES`` reaches the hook module, so every other
    request pays one set lookup and the module loads on first use.
    """
    from kiro_crew.change_card_catalog import HOOKED_ROUTES

    @web.middleware  # type: ignore[misc]
    async def _change_card_hook(request: web.Request, handler: Any) -> web.StreamResponse:
        resource = getattr(request.match_info.route, "resource", None)
        template = getattr(resource, "canonical", None)
        if (request.method.upper(), template) not in HOOKED_ROUTES:
            return await handler(request)
        from kiro_crew.dashboard.handlers.change_cards import change_card_middleware

        return await change_card_middleware(request, handler)

    app.middlewares.append(_change_card_hook)


def _register_crewmate_prune_gate(app: web.Application, state: DashboardState) -> None:
    """Arm the crewmate-prune barrier before bind; the pass itself runs after.

    The startup prune (``crewmate_prune_migration``) decides from each
    candidate's Crewmates-page DM thread which sync-generated crewmates were
    never chatted with, then
    deletes their rows. Every writer that can bind an agent to a session while
    the gateway is up reaches it through a mutating request -- the chat send,
    slot create, slot agent switch, member thread, channel and import routes
    under ``/api/``, and the OpenAI-compatible ``POST /v1/chat/completions`` --
    so ONE middleware holds every non-safe-method request until the pass
    settles, with no path list to keep in step with the route table. The
    member roster is held too, whatever its method, so it is read once the
    pass has settled and never lists a row the pass is removing
    (``_CREWMATE_PRUNE_GATE_HELD_PREFIXES``). The writers that do not come
    through HTTP wait in ``await_crewmate_prune_settled`` instead.

    Armed HERE, before ``_start_site`` binds the listener, so no request can
    pass between the bind and the pass. The pass itself is kicked as a tracked
    background task right after the bind (``_kick_crewmate_prune``) and sets
    ``crewmate_prune_settled`` in its ``finally``, so the hold is the pass
    alone and readiness is not gated by it. Other reads are never held, and
    the fast path is one ``is_set()`` read, which is what every request pays
    once the pass has settled. A held request that outlives the budget is
    answered 503 and writes nothing; it does not abandon the pass.
    """
    state.crewmate_prune_settled.clear()

    @web.middleware
    async def _crewmate_prune_gate(request: web.Request, handler: Any) -> web.StreamResponse:
        if not state.crewmate_prune_settled.is_set() and (
            request.method not in _CREWMATE_PRUNE_GATE_SAFE_METHODS
            or _crewmate_prune_gate_holds_path(request.path)
        ):
            try:
                await asyncio.wait_for(
                    state.crewmate_prune_settled.wait(), timeout=_CREWMATE_PRUNE_GATE_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                return web.json_response(
                    {
                        "error": "Crewmates are being tidied; retry shortly.",
                        "code": "prune_in_progress",
                    },
                    status=503,
                )
        return await handler(request)

    app.middlewares.append(_crewmate_prune_gate)


def _crewmate_prune_gate_holds_path(path: str) -> bool:
    """Whether *path* is one the gate holds whatever the request's method."""
    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in _CREWMATE_PRUNE_GATE_HELD_PREFIXES
    )


def _kick_crewmate_prune(state: DashboardState) -> None:
    """Run the one-time crewmate prune as a tracked background task, post-bind.

    ``prune_synced_crewmates`` scans the first line of every session file, so
    its cost scales with the user's history and it must not sit between the
    bind and ``KIROCREW_READY`` (``no-new-work-on-gateway-boot-path``, item 3).
    Same shape as ``_kick_knowledge_orphan_reclaim``: kicked after ``_start_site``
    returns, run on a worker thread (config lock and file reads are IO). The
    gate armed before the bind holds every mutating request until the event is
    set, which happens in ``finally`` whatever the pass does. Nothing on the
    readiness path waits for it -- not even the slot restores: a row the pass
    removes has, by its own evidence rule, no DM binding and no session whose
    metadata names it, so no restore can rebuild a slot for it. The writers
    that do NOT come through HTTP -- channel agent resume, cron dispatch, the
    subagent pump -- start only after ``await_crewmate_prune_settled`` returns
    (``GatewayOrchestrator.run`` after the memory barrier, past
    ``KIROCREW_READY``; the standalone dashboard before its inline channel
    resume), so none of them can bind a crewmate while the pass is judging it.
    The one startup step that DELETES a transcript, the channel transcript
    migration, merges but keeps its copies while the event is clear and
    removes them from ``_kick_deferred_transcript_removal`` once it is set, so
    the pass reads every first line the boot started with; a transcript that
    still vanishes under the pass voids it (nothing removed).
    The pass reads ``crewmate_prune_abandon`` before each candidate and again
    inside the config lock before each delete; that helper sets it when the
    pass outlives its budget, and the pass then finishes without deleting.
    The pass itself takes a cross-process lock beside its marker for its whole
    length, so a second gateway on the same data home cannot run its own pass
    beside this one -- its pass waits on that lock (its writers held by its
    own barrier meanwhile) and then finds the marker. On every boot but the
    first after the upgrade the pass is that lock and one marker stat.
    """

    async def _run() -> None:
        try:
            prune = await asyncio.to_thread(
                prune_synced_crewmates,
                state.conversation_log,
                abandoned=state.crewmate_prune_abandon.is_set,
            )
            if prune.removed:
                logger.info(
                    "removed %d unused auto-generated crewmates: %s",
                    len(prune.removed),
                    ", ".join(prune.removed),
                )
        except Exception:  # noqa: BLE001 -- the pass never raises for unreadable history
            logger.warning("crewmate prune migration failed", exc_info=True)
        finally:
            state.crewmate_prune_settled.set()

    task = asyncio.create_task(_run())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


async def await_crewmate_prune_settled(state: DashboardState, *, before: str) -> None:
    """Wait for the startup crewmate prune before starting a session writer.

    Every writer that binds an agent to a session without an HTTP request --
    the channel agent resume, cron dispatch, the subagent pump -- calls this
    first, so the pass's history snapshot cannot be overtaken by a binding it
    never saw. Returns only once ``crewmate_prune_settled`` is set, which the
    pass does in ``finally`` after it has RETURNED -- so no writer ever runs
    beside a pass that can still delete. The budget bounds how long the pass
    may keep deleting, not how long the writer waits: when the pass outlives
    it, this sets ``crewmate_prune_abandon`` -- the pass reads it before each
    candidate and inside the config lock before each delete, keeps whatever it
    has not judged, writes its marker and returns -- and then waits for the
    event. The pass always returns: its file opens are non-blocking and its
    lock acquires are bounded (``platform_compat.file_lock`` raises rather
    than waits on a stuck holder), and either outcome ends in ``finally``.
    ``before`` names the writer for the log line.
    """
    try:
        await asyncio.wait_for(
            state.crewmate_prune_settled.wait(), timeout=_CREWMATE_PRUNE_GATE_TIMEOUT_S
        )
        return
    except asyncio.TimeoutError:
        state.crewmate_prune_abandon.set()
        logger.warning(
            "crewmate prune has not settled in %.0fs; it will keep its unjudged "
            "crewmates, and %s starts once it has returned",
            _CREWMATE_PRUNE_GATE_TIMEOUT_S,
            before,
        )
    await state.crewmate_prune_settled.wait()


def _kick_deferred_transcript_removal(state: DashboardState, claimed: frozenset[str]) -> None:
    """Remove the channel transcript copies the startup merge left for the prune.

    ``start_dashboard`` merges every orphaned dashboard copy into its channel
    transcript before the session restores read it, but while the crewmate
    prune has not settled it passes ``remove=False``. The prune itself reads
    only each crewmate's own DM-thread transcript (``dashboard_<member slot
    key>``), never these copies, so keeping them does not protect its
    evidence. The delete still waits here, off the readiness path, for
    the pass to RETURN (``crewmate_prune_settled`` is set in its ``finally``),
    then re-runs the migration with removal on; the re-merge is byte-identical
    and only the deletes are new. Best-effort like the startup call: a failure
    leaves the copies for the next start, which merges and removes them again.
    """

    async def _run() -> None:
        await state.crewmate_prune_settled.wait()
        try:
            removed = await asyncio.to_thread(
                migrate_channel_transcripts, dashboard_slots=claimed, remove=True
            )
            if removed:
                logger.info(
                    "Removed %d leftover channel transcript copies after the crewmate prune",
                    removed,
                )
        except Exception:  # noqa: BLE001 -- the copies stay for the next start
            logger.warning("deferred channel transcript removal failed", exc_info=True)

    task = asyncio.create_task(_run())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


async def _converge_channel_transcripts(state: DashboardState) -> None:
    """Merge leftover dashboard copies into their channel transcripts before a restore.

    Converge any leftover copy transcripts BEFORE the restores read them. On an
    install carrying a second transcript for a channel conversation under a
    derived dashboard key, its dashboard-authored turns exist nowhere else, so
    they must be merged into the channel transcript before a slot is built
    from it. Idempotent, so it is a cheap no-op on
    every subsequent boot. Off-loop: it takes the per-session cross-process
    flock, which must never block the event loop.
    """
    try:
        # Slot names the session map claims as real dashboard sessions, so a
        # dashboard session that merely happens to be named like a channel
        # stem is never mistaken for an orphan of it.
        _claimed = await asyncio.to_thread(_claimed_dashboard_slots, state)
        # While the crewmate prune has not settled the merge is written but
        # the copy stays; a follow-up removes the copies once the pass has
        # returned. The prune reads only each crewmate's own DM-thread
        # transcript, never these copies, so this is ordering, not evidence. Nothing
        # here waits for the pass: the readiness path stays as it was.
        _remove = state.crewmate_prune_settled.is_set()
        merged = await asyncio.to_thread(
            migrate_channel_transcripts, dashboard_slots=_claimed, remove=_remove
        )
        if merged:
            logger.info("Merged %d leftover channel transcript copies", merged)
        if not _remove:
            _kick_deferred_transcript_removal(state, _claimed)
    except Exception:
        # A failed migration leaves the orphan in place rather than losing
        # messages, so starting up without it is safe.
        logger.warning("channel transcript migration failed", exc_info=True)
