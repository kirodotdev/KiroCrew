"""The process exit path.

The run marker's writer and self-clear, the signal handlers, the bounded cleanup of
every service, and the final exit.

Orchestrator fields read: ``_adaptive_controller``, ``_auto_migrate_task``,
``_channel_handles``, ``_channel_restart_tasks``, ``_console_script_repair_task``,
``_dashboard_port``, ``_dashboard_runner``, ``_feature_video_task``, ``_handler_tasks``,
``_inbound_replay_task``, ``_marker_clear_pending``, ``_marker_write_task``,
``_memory_repair_stop``, ``_memory_repair_task``, ``_memory_startup``,
``_model_download_task``, ``_profile_watch_task``, ``_sign_in_refresher_task``,
``_socket_client``, ``_update_check_task``, ``cron_svc``, ``dashboard_state``,
``heartbeat_svc``, ``sessions``, ``subagent_mgr``.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import contextlib
    import os
    import signal

    from kiro_crew.slack.gateway import (
        GRACEFUL_SHUTDOWN_SECS,
        PERSISTENCE_DRAIN_GRACE_SECS,
        UPDATE_INSTALLER_STOP_SECS,
        Any,
        GatewayOrchestrator,
        _stop_session_environments,
        drain_sign_in_refresher,
        flush_audit_queue_before_hard_exit,
        listener_guard_exit_code,
        logger,
        persistence_lost_exit_code,
        platform_compat,
        registry,
        shutdown_event,
        shutdown_exit_code,
        subprocess_executor,
        wait_for_unwinding_turns,
    )


async def _shutdown(self: GatewayOrchestrator) -> None:
    """Graceful cleanup of all services."""
    # ``_shutdown_and_exit`` runs this whole method under
    # ``wait_for(..., GRACEFUL_SHUTDOWN_SECS)``, so this is that bound's deadline.
    graceful_deadline = asyncio.get_running_loop().time() + GRACEFUL_SHUTDOWN_SECS
    # First: a stop owns any apply in flight (cancelled here), and its build
    # child dies now rather than after the teardown below.
    platform_compat.cancel_wheel_applies_in_flight("shutdown")
    # Then, before anything spends the shutdown budget: start stopping the
    # update coordinator. An installer it runs (a non-managed-venv shape) gets SIGTERM so it can restore the
    # install it moved aside, and that rollback must finish before this
    # process exits (past the GRACEFUL_SHUTDOWN_SECS cap the process is
    # force-exited with the installer orphaned mid-write). The stop runs
    # alongside the steps below, which do not touch the install, and is
    # awaited before the handler and service teardown, bounded by
    # UPDATE_INSTALLER_STOP_SECS.
    # A policy apply the dashboard started (``POST /api/update``) is the same
    # installer shape, so it is stopped and awaited here with the coordinator,
    # inside the same budget: aiohttp cancels its request handler only after
    # its own shutdown timeout, which the graceful cap exits ahead of.
    update_task = self._update_check_task
    installer_tasks: set[asyncio.Task[Any]] = set(self._dashboard_policy_applies())
    if update_task is not None:
        installer_tasks.add(update_task)
    installer_tasks = {task for task in installer_tasks if not task.done()}
    update_stop_deadline = asyncio.get_running_loop().time() + UPDATE_INSTALLER_STOP_SECS
    for task in installer_tasks:
        task.cancel()
    # Stop speculative session starts before anything below awaits. One still
    # debouncing would otherwise spawn an agent mid-teardown that close_all()
    # cannot see. They are awaited with the session teardown further down.
    eager_spawns: list[asyncio.Task[None]] = []
    if self.dashboard_state:
        from kiro_crew.dashboard.chat_runner import stop_eager_spawns

        eager_spawns = stop_eager_spawns(self.dashboard_state)
    self._memory_repair_stop.set()
    if self._memory_repair_task is not None:
        self._memory_repair_task.cancel()
        await asyncio.gather(self._memory_repair_task, return_exceptions=True)
    if self._memory_startup is not None:
        self._memory_startup.stop()
    # Stop the boot-time inbound-spool notice pass before the transports it
    # sends through are closed. Nothing is lost by cancelling: an entry is
    # removed from disk only AFTER its notice is confirmed, so an entry cut
    # off mid-send is noticed again on the next start (at most one duplicate
    # line). Awaited with a small budget so a slow platform send cannot spend
    # the GRACEFUL_SHUTDOWN_SECS that saves active chat slots.
    replay = self._inbound_replay_task
    if replay is not None and not replay.done():
        replay.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(replay, timeout=1.0)
    # Stop the governance profile watch for the same reason the refresher below is
    # stopped: no re-projection may start into a closing gateway. A rebuild already
    # running in its worker thread finishes on its own; this only stops new ones.
    profile_watch = self._profile_watch_task
    if profile_watch is not None and not profile_watch.done():
        profile_watch.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(profile_watch, timeout=1.0)
    # Stop polling the central policy source, so a fetch in flight cannot
    # install a ceiling into a context the rest of this teardown is dismantling.
    # The join budget is deliberately small: the thread waits on an Event, so
    # setting it wakes an idling refresher immediately and the join costs
    # nothing, while a refresher mid-fetch must not spend the shutdown budget
    # that saves active chat slots (this whole method runs under
    # GRACEFUL_SHUTDOWN_SECS). It is a daemon thread, so anything still running
    # after that dies at exit anyway.
    with contextlib.suppress(Exception):
        from kiro_crew.platform.policy_distribution import stop_refresher

        # ``stop_refresher`` JOINS a thread, which is a blocking call and must
        # not run on the event loop. Offloaded with its own deadline so a
        # refresher mid-fetch cannot eat the GRACEFUL_SHUTDOWN_SECS budget that
        # saves active chat slots; it is a daemon thread, so whatever is still
        # running after that dies at exit anyway.
        await asyncio.wait_for(asyncio.to_thread(stop_refresher, 0.5), timeout=1.5)

    # Disarm the loop-stall watchdog FIRST, before any of the teardown below.
    # close_all()/cancel_all() deliberately kill every kiro-cli child, which
    # is exactly the os.waitpid reaping burst that can wedge the loop for
    # >exit_after seconds. If the armed faulthandler dump-then-exit timer is
    # still live, that wedge would _exit(1) the process mid-shutdown — a clean
    # quit would look like a crash. The watchdog's own on_cleanup hook only
    # runs inside _dashboard_runner.cleanup(), which is gathered concurrently
    # with the reaping burst (too late), so we stop it explicitly here and
    # cancel the heartbeat that keeps re-arming it.
    if self.dashboard_state:
        wd = getattr(self.dashboard_state, "_loop_watchdog", None)
        if wd is not None:
            wd.stop()
        hb = getattr(self.dashboard_state, "_loop_heartbeat", None)
        if hb is not None:
            hb.cancel()

    # Save all active chat slots to history before shutdown
    if self.dashboard_state:
        from kiro_crew.dashboard.chat import save_all_slots_to_history

        # save_all_slots_to_history does synchronous per-slot file I/O that
        # takes the per-session cross-process lock; on the event loop a
        # contended session would raise HistoryLockTimeout (and a wedged
        # disk would block the loop). Offload to the bounded
        # subprocess_executor with a deadline so a slot's final save is
        # attempted off-loop and cannot stall the shutdown path.
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    save_all_slots_to_history,
                    self.dashboard_state,
                ),
                timeout=5.0,
            )
        except Exception:
            logger.debug("Dashboard slot save before shutdown failed", exc_info=True)
        self.dashboard_state.file_indexes.stop_all()

    if installer_tasks:
        remaining = update_stop_deadline - asyncio.get_running_loop().time()
        _done, pending = await asyncio.wait(installer_tasks, timeout=max(0.0, remaining))
        if pending:
            # Named, because the stuck task may be the update coordinator or
            # a dashboard-started policy apply; whoever chases an orphaned
            # install must be pointed at the right one.
            logger.warning(
                "Update work did not stop within %.0fs (%s); continuing shutdown",
                UPDATE_INSTALLER_STOP_SECS,
                ", ".join(sorted(task.get_name() for task in pending)),
            )

    # The general _background_tasks set is retention, not lifecycle ownership.
    # This task can own a dep_sync child plus pip/build descendants, so cancel
    # and await it explicitly while _check_console_script still has a live loop
    # on which to kill the process tree and perform its bounded reap.
    repair_task = self._console_script_repair_task
    if repair_task is not None and not repair_task.done():
        repair_task.cancel()
        await asyncio.gather(repair_task, return_exceptions=True)

    for t in list(self._handler_tasks):
        t.cancel()

    # Close AutoNudge admission before awaiting cancelled handlers. AutoNudge
    # deliberately re-raises cancellation only
    # AFTER its shielded persistence drain has reached durability, or after
    # the drain's post-cancellation window closes on a write that cannot
    # finish (gateway_shutdown_budget.PERSISTENCE_DRAIN_GRACE_SECS). Record
    # that outer cancellation here and propagate it at the end, so the
    # gateway's bounded wait keeps its cancellation/timeout contract.
    #
    # The drain runs as its own task on a budget of its own. Its
    # post-cancellation window opens only on a cancellation, so an uncut drain
    # on a wedged write would hold this method until the graceful bound itself
    # cut it, leaving the teardown below none of GRACEFUL_SHUTDOWN_SECS. It is
    # cut after one PERSISTENCE_DRAIN_GRACE_SECS window, or sooner when less than
    # one window would remain for the teardown once the drain's own window after
    # the cut closes. A cut made here is not the graceful bound, so the teardown
    # then runs inline under it.
    autonudge_shutdown_interrupted = False
    if self.autonudge_svc:
        drain_task = asyncio.create_task(
            self.autonudge_svc.shutdown(), name="gateway-shutdown-autonudge-drain"
        )
        drain_budget = min(
            PERSISTENCE_DRAIN_GRACE_SECS,
            max(
                0.0,
                graceful_deadline
                - asyncio.get_running_loop().time()
                - 2 * PERSISTENCE_DRAIN_GRACE_SECS,
            ),
        )
        drain_cut = False
        try:
            await asyncio.wait({drain_task}, timeout=drain_budget)
            if not drain_task.done():
                drain_cut = True
                drain_task.cancel()
                logger.warning(
                    "AutoNudge drain still pending after %.1fs; cancelled it so the "
                    "gateway teardown can start",
                    drain_budget,
                )
                await asyncio.wait({drain_task})
        except asyncio.CancelledError:
            # The graceful bound (or an earlier cancellation): hand it to the
            # drain, which still waits only its post-cancellation window.
            autonudge_shutdown_interrupted = True
            drain_task.cancel()
            await asyncio.wait({drain_task})
        if drain_task.cancelled():
            if not drain_cut:
                autonudge_shutdown_interrupted = True
        elif autonudge_shutdown_interrupted:
            # Retrieved so it is not reported unretrieved; the remembered
            # cancellation is what this method propagates.
            drain_task.exception()
        else:
            drain_task.result()
    if not autonudge_shutdown_interrupted:
        autonudge_svc = self.autonudge_svc
        await self._stop_services_and_sessions(eager_spawns=eager_spawns)
        if autonudge_svc:
            # A cut made above frees the teardown; it does not make the drained
            # work durable. Whatever it left in flight gets the rest of the
            # graceful budget before the hard exit that follows this method.
            await autonudge_svc.settle_admitted_writes(graceful_deadline)
        return
    # The absorbed cancellation was the graceful bound itself, or arrived
    # before it. Run the rest of teardown in its own task and wait only for
    # what remains of GRACEFUL_SHUTDOWN_SECS. The deadline is independent of
    # any one step accepting cancellation: at expiry the waiter cancels the
    # owned task and does not await it, so a step that swallows that cut cannot
    # turn the bound into an unbounded wait. The exit path's orphan sweep still
    # reaps the kiro-cli children.
    teardown_task = asyncio.create_task(
        self._stop_services_and_sessions(eager_spawns=eager_spawns),
        name="gateway-shutdown-teardown",
    )
    remaining = max(0.0, graceful_deadline - asyncio.get_running_loop().time())
    done, _pending = await asyncio.wait({teardown_task}, timeout=remaining)
    if teardown_task in done:
        await teardown_task
    else:
        teardown_task.cancel()
        logger.warning(
            "Gateway teardown after the AutoNudge drain reached the graceful "
            "shutdown deadline; the remaining steps were cut"
        )
    raise asyncio.CancelledError()


async def _stop_services_and_sessions(
    self: GatewayOrchestrator, *, eager_spawns: list[asyncio.Task[None]] | None = None
) -> None:
    """The teardown ``_shutdown`` runs once AutoNudge has drained.

    Cancelled handlers first, then cron and heartbeat, the MCP broker, the
    session, channel and dashboard closes, and last the subagent store and
    memory startup those closes release. Only then is the closed AutoNudge
    service withdrawn from its process-wide singleton. ``eager_spawns`` are the
    speculative session starts ``_shutdown`` stopped before its first await;
    they are awaited here with the session closes.
    """
    # Drain handlers only after AutoNudge has closed admission and drained
    # every mutation that entered before closure. A handler
    # still running after cancellation may reach a closed mutation entry; the
    # shared mutation authorizer maps that expected shutdown refusal to 503. This
    # await is the first bounded teardown step so a removal that keeps absorbing
    # cancellation cannot prevent AutoNudge's drain from running.
    if self._handler_tasks:
        await asyncio.gather(*self._handler_tasks, return_exceptions=True)

    if self.cron_svc:
        await self.cron_svc.stop()
    if self.heartbeat_svc:
        self.heartbeat_svc.stop()

    # Stop the pooled MCP gateway broker + its backends. gatewayd is
    # spawned with start_new_session (and no PR_SET_PDEATHSIG), so on a
    # clean Kiro Crew exit it and its pooled MCP subprocesses would
    # otherwise leak orphaned until the next start's flock adoption.
    await self._stop_mcp_broker()

    # Kill all ACP processes and close connections
    cleanup_tasks: list = []
    if self._adaptive_controller is not None:
        # Reached only past a live controller, so the module is resident
        # already: this import is a dict lookup, never the load a
        # module-scope spelling puts on the boot path.
        from kiro_crew.adaptive import controller as adaptive_controller

        adaptive_controller.register(None)
        self._unwire_overload_health()
        cleanup_tasks.append(self._adaptive_controller.stop())
    if self.subagent_mgr:
        cleanup_tasks.append(self.subagent_mgr.cancel_all())
    if self.sessions:
        cleanup_tasks.append(self.sessions.close_all())
    if eager_spawns:
        cleanup_tasks.append(asyncio.gather(*eager_spawns, return_exceptions=True))
    if self._dashboard_runner:
        # Close WS connections first so handlers exit promptly
        if self.dashboard_state:
            await self.dashboard_state.close_all_ws()
        cleanup_tasks.append(self._dashboard_runner.cleanup())
    if self._socket_client:
        cleanup_tasks.append(asyncio.wait_for(self._socket_client.close(), timeout=1.0))
    # A reconnect the config applier scheduled must not race the close below
    # by starting a fresh transport into a gateway that is going away.
    for restart in list(self._channel_restart_tasks):
        restart.cancel()
    cleanup_tasks.extend(registry.shutdown_tasks(self._channel_handles, timeout=2.0))
    # Cancel background model download if still in flight
    if self._model_download_task is not None and not self._model_download_task.done():
        self._model_download_task.cancel()
    # Cancel the feature-video transfer if still in flight
    if self._feature_video_task is not None and not self._feature_video_task.done():
        self._feature_video_task.cancel()
    # Cancel background auto-migration if still in flight
    if self._auto_migrate_task is not None and not self._auto_migrate_task.done():
        self._auto_migrate_task.cancel()
    # The update coordinator was stopped first (top of this method); the
    # dashboard's own check is cancelled here with the other tails.
    # The managed-venv applies cancelled at the top of this method get a
    # bounded grace to unwind, concurrently with the teardown above.
    cleanup_tasks.append(self._settle_update_work(self._update_check_task))
    from kiro_crew.dashboard.handlers.updates import _cancel_update_check

    cleanup_tasks.append(_cancel_update_check())

    if cleanup_tasks:
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    # AFTER the gather, not beside cancel_all() above: cancel_all() is what stops
    # the runs that still write to the durable task queue, so closing the store
    # before it finishes would pull the connection out from under them. Off-loop,
    # because ``close()`` is synchronous and waits for the store's writer lock --
    # on the loop that stalls shutdown behind an in-flight executor write.
    if self.subagent_mgr:
        await asyncio.to_thread(self.subagent_mgr.close)

    await asyncio.to_thread(self._stop_memory_startup)

    # close_all() above ended every ACP session, so a dashboard turn that was
    # mid-reply is now unwinding, and it saves the text its user watched stream
    # before it ends; the hard exit after this method would beat the save. Last,
    # so the steps above keep their share of the shutdown deadline.
    await wait_for_unwinding_turns(self.dashboard_state)
    # Keep the closed service reachable until every dashboard handler and
    # connection is gone. A slot close admitted during teardown must reach
    # the service's closed-admission refusal instead of silently treating a
    # missing singleton as "no loop" and persisting a resumable closed tab.
    # This method is shared by the normal and owned interrupted paths, so
    # both withdraw the singleton only after the same teardown boundary.
    if self.autonudge_svc:
        self.autonudge_svc._unpublish()


def _write_marker_worker(self: GatewayOrchestrator, run_marker: Any, port: int) -> None:
    """Write the run marker; self-clear if shutdown flagged a clear.

    ``run_marker`` is the :mod:`kiro_crew.instances.run_marker` module,
    passed in by ``run()`` (which already imports it lazily) so this
    worker adds no import of its own. Runs on a ``to_thread`` worker.
    If graceful shutdown timed out waiting for this write, it sets
    ``_marker_clear_pending`` BEFORE clearing the marker itself — so
    whichever order the write and the shutdown-side clear land in, this
    thread re-clears its own late write. The clear lives in the same
    thread as the write (not an event-loop callback) because
    ``os._exit`` can beat any callback still queued on the loop.

    The clear here is ``clear_late_marker_write``, not ``clear_marker``,
    and the difference is what makes this thread safe to run at an
    arbitrary time. The shutdown-side clear holds the listener while it
    runs, so a location still identifies its owner; this thread may run
    after the listener is free and a replacement gateway has bound the
    port, where it does not. ``clear_late_marker_write`` therefore deletes
    no credential at all, so a replacement gateway's clients keep
    authenticating whatever this thread does, and it declines the marker
    files outright when the pid record names another process. Its own
    docstring states what that second scope does not reach: a late write
    of this generation rewrites the pid record, so marker files a landed
    late write produced are always removable.
    """
    try:
        run_marker.write_marker(port)
    finally:
        if self._marker_clear_pending.is_set():
            try:
                if not run_marker.clear_late_marker_write(port):
                    logger.debug(
                        "Late run-marker self-clear declined for port %s: "
                        "the pid record names another gateway",
                        port,
                    )
            except Exception:
                logger.debug("Late run-marker self-clear skipped", exc_info=True)


def _install_shutdown_signal_handlers(self: GatewayOrchestrator) -> None:
    """Make owner stop effective before waiting on memory preparation."""
    from kiro_crew.session import cleanup_orphaned_sessions

    # ── Signal handlers ──
    # Installed after READY and before memory preparation or update checks
    # are awaited. A slow worker must not prevent Ctrl-C from setting the
    # same event used by the authenticated owner shutdown route.
    loop = asyncio.get_running_loop()
    _shutting_down = False

    def _on_signal(*_args: object) -> None:
        nonlocal _shutting_down
        # Before anything else, on either signal: a stop owns any apply in
        # flight (cancelled here), so its build child dies here, ahead of
        # any exit path.
        platform_compat.cancel_wheel_applies_in_flight("shutdown")
        if _shutting_down:
            print("\n👻 Force exit!")
            # Synchronous by necessity: a signal handler cannot await.
            # The process calls os._exit immediately below, so loop latency
            # does not matter on this path.
            #
            # ``narrow_with_leaders=False`` so this handler does exactly the
            # work it did before the recycled-pid change: killing leftover
            # processes is what this path is for, and a handler that reaches
            # for extra work before its os._exit is a handler that may not
            # get there.
            cleanup_orphaned_sessions(narrow_with_leaders=False)
            # Same reason as the log queue below: os._exit skips atexit, so the
            # member event log's own drain hook never runs. Synchronous because
            # a signal handler cannot await, and bounded inside the module for
            # the same reason the log-queue drain is bounded here -- a wedged
            # disk must delay this exit, never hold it.
            try:
                from kiro_crew import eventlog_hooks

                eventlog_hooks.drain_for_shutdown()
            except Exception:
                pass  # force exit must never be blocked by bookkeeping
            # os._exit skips atexit, so the log queue's drain hook never
            # runs — flush the queued gateway.log tail here, bounded so a
            # wedged disk cannot hang the force exit.
            try:
                from kiro_crew.cli import _stop_log_queue_listener

                _stop_log_queue_listener(timeout=2.0)
            except Exception:
                pass  # force exit must never be blocked by logging
            platform_compat.hard_exit(0)
        _shutting_down = True
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            # Windows ProactorEventLoop does not support add_signal_handler.
            # Fall back to signal.signal for SIGINT so shutdown_event still
            # gets set; SIGTERM is not meaningfully deliverable on Windows.
            if sig == signal.SIGINT:

                def _sigint_fallback(*_a: object) -> None:
                    try:
                        loop.call_soon_threadsafe(_on_signal)
                    except RuntimeError:
                        _on_signal()  # loop already closed

                try:
                    signal.signal(sig, _sigint_fallback)
                except (ValueError, OSError):
                    pass  # not in main thread
        except (RuntimeError, ValueError):
            # Not in main thread (e.g. pytest-xdist worker) — skip.
            pass


async def _shutdown_and_exit(
    self: GatewayOrchestrator, watchdog: asyncio.Future | None = None
) -> None:
    """Use the same bounded cleanup for early owner stop and normal shutdown."""
    from kiro_crew.session import cleanup_orphaned_sessions

    print("👻 Shutting down…")

    # Exit status for the os._exit below. 0 for an operator stop (SIGTERM,
    # `systemctl stop`, Ctrl-C) so a restart-on-failure supervisor leaves
    # the gateway down as asked. Non-zero when the stale-asset watchdog is
    # what set the event: that shutdown exists ONLY to be restarted, and
    # a supervisor with `Restart=on-failure` semantics never relaunches an
    # exit 0, and an older unit file may lack `Restart=always`, leaving
    # the gateway down. The watchdog has already
    # returned (True on the vanish path) by the time it sets the event, so
    # its task result is the signal; see shutdown_exit_code.
    #
    # The listener guard is the other self-initiated shutdown: it set the
    # event because the TCP listener died and could not be rebound, so the
    # process was alive but unreachable. That state must never be an
    # exit 0 either -- the supervisor has to relaunch it.
    # BOTH guards, because either can be the one that gave up. The second
    # loopback family's guard normally degrades instead of exiting, but it
    # escalates to this exit when its sidecar can be neither removed nor
    # blanked -- a live credential readable for an address nothing holds --
    # and reading only the primary would turn that escalation into an exit 0
    # the supervisor does not relaunch.
    #
    # The persistence watchdog is a third self-initiated shutdown: it set
    # the event because the data home stopped persisting after a healthy
    # boot (a seccomp filter inherited from a torn-down sandbox turning
    # file opens into ENOSYS). That cannot be cured in-process, so it must
    # exit non-zero too for the supervisor to relaunch a fresh process.
    exit_code = (
        shutdown_exit_code(watchdog)
        or persistence_lost_exit_code(getattr(self, "_persistence_watchdog_task", None))
        or listener_guard_exit_code(getattr(self.dashboard_state, "_listener_guard", None))
        or listener_guard_exit_code(
            getattr(self.dashboard_state, "_secondary_listener_guard", None)
        )
    )

    # Drop this gateway's run-marker BEFORE _shutdown() releases the
    # listener: once the port is free a replacement gateway can bind it
    # and publish its own marker + credential, which this clear would
    # then delete (clear_marker is unconditional — consumers verify
    # ownership on read, but deleting the successor's credential 403s
    # its clients). The clear itself is best-effort; a stale marker is
    # harmless — the next startup overwrites it. The wait for the
    # in-flight write is bounded so a stalled write cannot eat into the
    # graceful-shutdown deadline below (which saves active slots). On
    # timeout the detached writer thread may still republish the marker
    # after the clear, so _marker_clear_pending is set FIRST: the
    # writer thread (see _write_marker_worker) then re-clears its own
    # late write in the same thread, with no event-loop callback that
    # os._exit could beat. TimeoutError and a failed write are both
    # caught HERE (not by the outer except) so they still fall through
    # to the clear.
    # The clear itself is filesystem work -- unlinking the marker, the pid and
    # start sidecars, and every credential sidecar the listeners published --
    # so it is offloaded rather than run on the loop. It is AWAITED, not
    # queued, because the ordering above is the point: the clear has to land
    # before _shutdown() frees the listener. An awaited thread also cannot be
    # beaten by os._exit the way a loop callback can.
    try:
        from kiro_crew.instances import run_marker

        if self._dashboard_port:
            if self._marker_write_task is not None:
                try:
                    await asyncio.wait_for(
                        self._marker_write_task,
                        timeout=_MARKER_WRITE_WAIT_SECS,
                    )
                except asyncio.TimeoutError:
                    self._marker_clear_pending.set()
                    logger.warning(
                        "Run-marker write did not finish within %ss; "
                        "clearing marker without waiting",
                        _MARKER_WRITE_WAIT_SECS,
                    )
                except Exception:
                    logger.debug(
                        "Run-marker write failed; clearing anyway",
                        exc_info=True,
                    )
            await asyncio.to_thread(run_marker.clear_marker, self._dashboard_port)
    except Exception:
        logger.debug("Gateway run-marker clear skipped", exc_info=True)

    # The sign-in tick drains alongside the graceful shutdown, inside the
    # same budget: an in-flight renewal finishes and persists the rotated
    # refresh credential instead of being cut off by os._exit below.
    _sign_in_drain = asyncio.create_task(drain_sign_in_refresher(self._sign_in_refresher_task))
    # Remote session environments stop with the gateway that drives them, in
    # parallel with the graceful shutdown and under their own ceiling.
    _env_stop = asyncio.create_task(_stop_session_environments())
    try:
        await asyncio.wait_for(self._shutdown(), timeout=GRACEFUL_SHUTDOWN_SECS)
    except (asyncio.TimeoutError, Exception):
        logger.warning("Graceful shutdown timed out — force exiting")
    await _sign_in_drain
    await _env_stop

    print("👻 Goodbye!")
    # Kill any kiro-cli processes that survived graceful shutdown.
    # Off-loop: still inside the orchestrator coroutine here.
    #
    # The one call site that asks for the leaders narrowing. Nothing spawns a
    # session by this point, so the sweep is not racing a mapping publisher.
    await asyncio.to_thread(cleanup_orphaned_sessions)
    # Shared Crew per-principal slices. The dashboard's cleanup hook releases
    # them, but a graceful shutdown that ran out of time never reaches it, and
    # os._exit below skips it. The runtimes are gone by now, so their scopes
    # are empty; a release that already ran leaves nothing to do here. Bounded
    # like the drains below.
    try:
        from kiro_crew.shared_crew.admission_gate import shutdown_cleanup

        await asyncio.wait_for(shutdown_cleanup(), timeout=6.0)
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.debug("shared crew slice release at exit failed", exc_info=True)
    # The skill-usage ledgers debounce their writes to one per 60 s, and their
    # flush thread is a daemon: a hit accepted inside that window lives only in
    # memory, and os._exit below kills it. Flush them here, off-loop and
    # bounded, so a restart or upgrade keeps the ranking signal it just earned.
    try:
        from kiro_crew import skill_usage

        if not await asyncio.wait_for(
            asyncio.to_thread(skill_usage.flush_all_for_shutdown),
            timeout=skill_usage.SHUTDOWN_FLUSH_SECS,
        ):
            logger.warning("the skill-usage ledger did not fully flush before exit")
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.debug("the skill-usage ledger flush failed during shutdown", exc_info=True)
    # The session's log's buffered appends, for EVERY gateway mode. The
    # dashboard registers its own cleanup hook, but a mode that builds no
    # dashboard app -- slack-only is the plain case -- never runs one, and
    # os._exit below skips atexit, so without this the buffer dies with the
    # process. What it drops is the last thing each session did, which is
    # exactly what a reader looks for after a restart. Bounded inside the
    # emitter and off-loop, like the log-queue drain that follows; calling it
    # twice is a no-op, so the dashboard hook stays as it is.
    try:
        # Imported HERE, not at module scope: AUTOSDE's
        # no-new-work-on-gateway-boot-path rule asks for an optional subsystem's
        # import to be gated, and a shutdown drain is the only use in this module.
        from kiro_crew.crew_log import emit as crew_log_emit

        if not await asyncio.to_thread(crew_log_emit.drain_for_shutdown):
            logger.warning("the session's log did not fully drain before exit")
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.debug("the session's log drain failed during shutdown", exc_info=True)
    # The member event log's queued appends, for the same reason and on the same
    # terms. Its appends are ORDERED on one executor, so the tail sitting there
    # at exit is the newest transitions -- a patrol stop, a slot close -- and
    # they are exactly what a reader looks for after a restart. It registers an
    # atexit hook of its own, which os._exit skips, so this is the only drain
    # that runs on this path. Bounded inside the module and off-loop, like the
    # drain above; calling it twice is a no-op.
    try:
        from kiro_crew import eventlog_hooks

        if not await asyncio.to_thread(eventlog_hooks.drain_for_shutdown):
            logger.warning("the member event log did not fully drain before exit")
    except Exception:  # noqa: BLE001 - shutdown must not raise
        logger.debug("the member event log drain failed during shutdown", exc_info=True)
    # This is a hard exit too: os._exit skips atexit, so the log queue's
    # drain hook never runs here either. Without this the whole shutdown
    # tail is lost -- including the "Graceful shutdown timed out" warning
    # logged a few lines up, the one record a stuck-shutdown post-mortem
    # actually needs. Bounded and off-loop so a wedged disk cannot delay
    # the exit (see drain_log_queue_before_hard_exit).
    #
    # Flush SEL BEFORE the gateway.log tail (the order slack/events_slash.py uses):
    # draining gateway.log first stops its listener, and a SEL write failure
    # then logs its "dropped N events" line into a dead queue, so the audit
    # loss would leave no trace.
    from kiro_crew.cli import drain_log_queue_before_hard_exit

    await flush_audit_queue_before_hard_exit()
    await drain_log_queue_before_hard_exit()
    os._exit(exit_code)


# Budget for awaiting the in-flight run-marker write during shutdown. Bounded
# so a stalled write can never eat into GRACEFUL_SHUTDOWN_SECS (which saves
# active slots) — the marker is best-effort, the slot save is not.
_MARKER_WRITE_WAIT_SECS = 5.0
