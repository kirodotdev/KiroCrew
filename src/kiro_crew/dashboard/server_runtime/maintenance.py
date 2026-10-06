"""The tracked background maintenance both entrypoints start once the listener serves.

The warm-mint scavenge and its cleanup, the local decision model, the session search
indexer, the knowledge orphan reclaim, the data home's owner-only mode repair, and the
own-address read for the ssh self-target floor.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _OWN_HOST_WARM_TASKS,
        DashboardState,
        data_home,
        logger,
        shutdown_event,
        tighten_data_home,
        warm_own_host_names,
    )


def _register_connections_warm_lifecycle(app: web.Application, state: DashboardState) -> None:
    """Retire warm generations on cleanup; startup scavenging is kicked post-bind.

    The import sits inside the hook deliberately, against ``top-level-imports``, because
    ``no-new-work-on-gateway-boot-path`` governs this file and wins: importing
    ``connections.warm`` at module scope would pull its whole dependency graph -- the mint
    table, the provider registry, tool aliases, MCP discovery -- onto the one ordered thread
    between process start and the socket accepting requests. Startup scavenging is NOT an
    ``on_startup`` hook for the same reason: aiohttp runs those inside ``runner.setup()``,
    BEFORE the listener binds, so even a hook that only created the scavenge task put the
    synchronous import in front of the bind. Both entrypoints instead call
    ``_kick_connections_warm_scavenge`` strictly after ``_start_site`` returns.

    The cleanup hook is registered here, before ``runner.setup()`` freezes aiohttp's signal
    lists; it resolves the import only when a gateway is already stopping.
    """

    async def _connections_warm_shutdown(_app: web.Application) -> None:
        try:
            from kiro_crew.connections.warm import shutdown_warm_mint

            await shutdown_warm_mint()
        except Exception:  # noqa: BLE001 — one cleanup hook must not suppress later hooks
            logger.warning("Connections warm shutdown failed", exc_info=True)

    app.on_cleanup.append(_connections_warm_shutdown)


def _kick_connections_warm_scavenge(state: DashboardState) -> None:
    """Start the crash-residue scavenge as a tracked background task, post-bind.

    Called by both gateway entrypoints only after ``_start_site`` has returned, so the
    listener is already accepting requests. The deferred ``connections.warm`` import
    happens INSIDE the worker thread: resolving that dependency graph on the event loop
    would stall in-flight requests just as it would have stalled the bind.
    """

    def _scavenge_in_thread() -> None:
        from kiro_crew.connections.warm import scavenge_warm_mint_artifacts

        scavenge_warm_mint_artifacts()

    async def _connections_warm_scavenge() -> None:
        try:
            await asyncio.to_thread(_scavenge_in_thread)
        except Exception:  # noqa: BLE001 — fail closed by retaining unproved residue
            logger.warning("Connections warm artifact scavenging failed", exc_info=True)

    task = asyncio.create_task(_connections_warm_scavenge())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _kick_local_decision_model(state: DashboardState) -> None:
    """Start the local decision model the provider names, post-bind.

    Called by both gateway entrypoints only after ``_start_site`` has returned. The
    import and the config read happen off the event loop, under the provider-switch
    lock, so a gateway with no local model configured pays one thread hop after the
    listener is serving and a switch made meanwhile is never undone by a stale read.
    """

    async def _resume() -> None:
        try:
            from kiro_crew.dashboard.handlers.decisions import resume_local_decision_model

            preset = await resume_local_decision_model()
        except Exception:  # noqa: BLE001 - an optional subsystem never fails the gateway
            logger.warning("local decision model: resume at startup failed", exc_info=True)
            return
        if preset:
            logger.info("local decision model: starting %s", preset)

    task = asyncio.create_task(_resume())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _kick_owner_only_sweep(state: DashboardState) -> None:
    """Repair the data home's file modes as a tracked background task, post-bind.

    ``tighten_data_home`` walks the data home's session, log and memory stores, so
    its cost grows with the user's history, and on a healthy install it finds
    nothing to change: it must not sit between process start and the socket
    accepting requests (``no-new-work-on-gateway-boot-path``, items 3 and 4). Both
    gateway entrypoints call this only after ``_start_site`` has returned, and the
    walk runs on a worker thread. Nothing waits for it: what the services create is
    owner-only from creation, and the sweep changes modes without opening a file,
    so it releases no lock a store's connection holds. ``shutdown_event`` ends the
    walk at its next entry, so a stopping gateway does not wait out the sweep's
    budget in the executor.
    """

    def _sweep_in_thread() -> None:
        tighten_data_home(data_home(), should_stop=shutdown_event.is_set)

    async def _owner_only_sweep() -> None:
        try:
            await asyncio.to_thread(_sweep_in_thread)
        except Exception:  # noqa: BLE001 -- a permission repair never fails the gateway
            logger.warning("owner-only sweep of the data home failed", exc_info=True)

    task = asyncio.create_task(_owner_only_sweep())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _kick_session_search_index(state: DashboardState) -> None:
    """Keep the session search candidate index caught up, in its OWN process.

    Without an indexer the index never gets built and search silently stays on
    the scan path — correct, and as slow as it was (measured 6.7 s per keystroke
    on a 2.96 GB corpus, against ~0.3 s indexed).

    The indexing itself runs in a spawned child process, not here. It is
    pure-Python CPU (read, ``casefold``, project, insert), so as an
    ``asyncio.to_thread`` call it held the GIL that this gateway's event loop
    needs: ``py-spy top --gil`` attributed 28% of all GIL-holding samples to it,
    and the loop showed ``event-loop heartbeat: lag 1.0-6.6s`` on an 84%-idle
    machine. See ``kiro_crew.history_index_worker`` for why a process rather than
    a thread, why spawn rather than fork, and why deletion stays here.

    This gateway keeps only the read-only query path, plus the one index write
    that belongs to deletion (``delete_session`` removes a session's indexed text
    before unlinking the transcript and aborts if it cannot).

    What is left here is supervision: start the child, restart it if it dies,
    stop it when this gateway stops. Three things retire the child, and the
    order matters because the first two can be skipped: this task's ``finally``
    asks it to stop, ``daemon=True`` has ``multiprocessing`` reap it at
    interpreter exit, and failing both the child retires ITSELF once it sees this
    process is gone. Only the third survives a hard exit — the shutdown and
    restart paths can end this process with ``os._exit``, which runs no
    ``atexit`` handler, so neither parent-side path is guaranteed to run. That is
    why the child re-checks its parent on a short slice rather than once per pass:
    it bounds how long an orphan can keep indexing beside its replacement.

    A failure to keep a child running is logged once and then left alone: a
    missing row costs one scanned file, so the honest response to an indexer that
    will not stay up is to keep serving searches from the transcripts.
    """

    # The child's own pass cadence lives with the loop that honours it, in
    # ``history_index_worker``. Blocking calls (``Process.start`` costs a fresh
    # interpreter, ``stop`` waits on a signal) go through ``to_thread`` so the
    # event loop this change exists to protect is never the thing that waits.
    def _migrate_index_schema() -> None:
        """Bring the index schema to the current version from ONE process.

        ``SessionSearchIndex._init_schema`` DROPs the tables when the stored
        ``user_version`` is stale, and the only thing guarding that is a
        per-PROCESS lock. This change introduces a second opener, so after a
        version bump the gateway and the child can both read the stale version
        and both run the DROP — the later one discarding the tables the earlier
        one just built, along with anything indexed in between. Nothing
        authoritative is lost (the rows derive from transcripts) but search falls
        back to scanning until a later pass repopulates it.

        Opening it here, before the child is started, means the on-disk version is
        already current when the child first opens and the gate cannot fire in two
        processes at once. Blocking, so the caller hands it to a thread.
        """
        log = state.conversation_log
        if log is None:
            return
        try:
            index = log._catalog_projection.search_index
        except Exception:  # noqa: BLE001 — search must survive a bad index
            logger.warning("Session search index schema migration failed", exc_info=True)
            return
        if not index.available:
            logger.warning(
                "Session search index is unavailable; the indexer will run but "
                "search falls back to scanning the transcripts"
            )

    async def _session_index_supervisor() -> None:
        log = state.conversation_log
        if log is None:
            # No transcript store on this gateway: nothing to index, and the
            # search path it would serve does not exist either.
            return
        # Deferred import, per ``no-new-work-on-gateway-boot-path``: this module
        # is reached only once the listener is already serving.
        from kiro_crew.history_index_worker import SessionIndexWorkerSupervisor

        # The supervisor refuses a transcript directory that is not an existing
        # absolute path, because the child would otherwise resolve it against
        # its own working directory and create it there.
        supervisor = SessionIndexWorkerSupervisor(log._dir)
        try:
            await asyncio.to_thread(_migrate_index_schema)
            # A failed FIRST spawn is not treated differently from a child that
            # dies later: both fall into the poll loop, which retries with backoff
            # and eventually gives up for good. Returning here instead would let a
            # transient failure at boot -- memory pressure, an fd limit, the very
            # conditions this change exists to ease -- leave search scanning
            # transcripts for the whole life of the gateway. A refusal that cannot
            # improve by retrying, such as a transcript directory that is not
            # there, sets ``gave_up`` inside ``start`` and so exits immediately.
            await asyncio.to_thread(supervisor.start)
            while not supervisor.gave_up:
                wait_secs = await asyncio.to_thread(supervisor.poll)
                await asyncio.sleep(wait_secs)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — search must survive a bad indexer
            logger.warning("Session search index supervisor failed", exc_info=True)
        finally:
            # The child is reaped OFF this loop. ``terminate`` and ``join``
            # block, and a shutdown that freezes the loop for seconds is the
            # exact failure this change exists to remove
            # (no-blocking-call-on-event-loop).
            #
            # ``request_stop`` is the non-blocking half — one ``waitpid`` and one
            # SIGTERM — so the child is already on its way down before anything
            # is awaited. The waiting half goes to a thread, shielded so that
            # cancelling THIS task does not cancel the reap with it.
            supervisor.request_stop()
            try:
                await asyncio.shield(asyncio.to_thread(supervisor.reap))
            except asyncio.CancelledError:
                # Cancelled mid-reap. The signal is already delivered and the
                # child is daemonic, so ``multiprocessing`` reaps it at
                # interpreter exit regardless; blocking the loop to wait here
                # would trade a leak that cannot happen for a stall that can.
                raise
            except Exception:  # noqa: BLE001 — the loop may already be closing
                logger.warning("Session search index writer did not stop cleanly", exc_info=True)

    task = asyncio.create_task(_session_index_supervisor())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _kick_knowledge_orphan_reclaim(state: DashboardState) -> None:
    """Run the knowledge store's orphan sweep as a tracked background task, post-bind.

    ``KnowledgeStore.reclaim_orphans`` is data-scaled and takes SQLite's writer
    lock; run inside the constructor, on the event loop, before the socket
    bound, a large store stalls boot long enough for the runtime's timeouts to
    kill the gateway. Called only after ``_start_site``
    has returned, and the sweep itself runs on a worker thread (the store's
    connection is thread-local, so the worker gets its own), never on the loop.

    Only a store that construction already built is swept: ``setup_knowledge_routes``
    reads the lazy ``knowledge_store`` property at route registration, so on the
    dashboard entrypoint one always exists. Building one here would be new work
    on the boot path for an entrypoint that never registered the routes.

    Requests are being served while the sweep waits for its worker, and an
    ingest in progress is committed in several steps (source row, job, items,
    mentions), each of which reads as an orphan to the sweep's predicates. The
    sweep therefore runs inside the store's ``maintenance_window``: it waits
    for in-flight ingestion to drain, holds new ingestion off while it runs,
    and is skipped (logged, never forced) if ingestion does not drain in time.
    """

    def _reclaim_in_thread() -> None:
        store = state._knowledge_store
        if store is None:
            return
        with store.maintenance_window() as quiescent:
            if quiescent:
                store.reclaim_orphans()
                # Same drained window: an interrupted agent-document ingest can
                # leave committed items that no agent_item_state row names, and
                # nothing on that path reaps them (see
                # KnowledgeStore.reclaim_agent_source_residue). Running it here
                # means it only ever sees fully-settled ingests.
                store.reclaim_agent_source_residue()

    async def _knowledge_orphan_reclaim() -> None:
        try:
            await asyncio.to_thread(_reclaim_in_thread)
        except Exception:  # noqa: BLE001 -- hygiene must never take the gateway down
            logger.warning("Knowledge store orphan reclaim failed", exc_info=True)

    task = asyncio.create_task(_knowledge_orphan_reclaim())
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


def _register_own_host_warm(app: web.Application) -> None:
    """Start reading this machine's own addresses at boot, without waiting on it.

    The ssh self-target floor denies every IP literal until the address table
    is read.  Left to the first ssh check, that check is what starts the read
    and it sees the unpublished flag in the same instant, so the first IP-literal
    ssh of every process is refused.  This hook starts the read in a worker
    thread and returns at once: nothing is awaited in front of the listener
    (``no-new-work-on-gateway-boot-path``).  The netlink dump is a kernel-local
    read of about a millisecond, so it has published long before an agent's
    first command; until it does, the floor stays fail-closed.
    """

    async def _own_host_warm(_app: web.Application) -> None:
        task = asyncio.ensure_future(asyncio.to_thread(warm_own_host_names))
        _OWN_HOST_WARM_TASKS.add(task)
        task.add_done_callback(_own_host_warm_done)

    app.on_startup.append(_own_host_warm)


def _own_host_warm_done(task: "asyncio.Future[None]") -> None:
    """Drop the finished warm task and log a failure instead of raising it."""
    _OWN_HOST_WARM_TASKS.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("own-address read failed at startup", exc_info=exc)
