"""The opt-in Instances (multi-instance) hooks and their revive policy.

The startup hook that re-takes lent hop ports and reconnects the instances the operator
left connected, and the cleanup hooks that stop the tunnels and drain the crew log.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        DashboardState,
        InstancesRegistry,
        KiroCrewConfig,
        SshTunnelManager,
        TunnelState,
        logger,
        register_pre_exec_stop,
    )


async def _retake_hops_then_revive(
    registry: InstancesRegistry,
    manager: SshTunnelManager,
    state: DashboardState | None = None,
) -> None:
    """Re-take the lent hop ports, then revive. Both off the boot path, in this order.

    A hop lease is PERSISTED and the listening socket that enforces it is not, so a
    restart arrives holding leases that keep ports out of this gateway's own allocator
    and own them in no other sense -- the window the lease alone cannot close, reopened
    by the restart. Re-taking them is therefore startup work, not a nicety.

    But it is not BOOT-PATH work. `_instances_startup` is an `on_startup` hook, so it
    runs inside `runner.setup()` before the HTTP port is bound, and the re-take costs a
    registry read plus one bind per live lease -- data-scaled work on the path the
    desktop app's gateway-wait window measures. So it moves in here, behind the same
    tracked task that already backgrounds the revive below for that exact reason, and
    the read itself goes to a thread because it is a file read on the event loop.

    Ordering is load-bearing and is why this is one task rather than two: the revive
    reconnects instances that will ALLOCATE ports, and a lease whose hold is not yet
    taken is a port the allocator already avoids but nothing owns. Re-taking first
    means no reconnect can race a lease that is still unenforced.
    """
    try:
        unheld = await asyncio.to_thread(manager.sync_hop_holds)
    except Exception:
        logger.exception("Could not re-take lent hop ports after restart")
    else:
        if unheld:
            logger.error(
                "Could not re-take %d lent hop port(s) after restart: %s. A chained "
                "credential naming each is still valid, so another process may hold "
                "it; the guard retries each until it is taken or its lease lapses.",
                len(unheld),
                sorted(unheld),
            )
    await _revive_intended_instances(registry, manager, state)


async def _revive_intended_instances(
    registry: InstancesRegistry,
    manager: SshTunnelManager,
    state: DashboardState | None = None,
) -> None:
    """Auto-reconnect every instance the operator left connected.

    ``was_connected`` is the sticky "connection intent" (set on connect, cleared
    only on explicit disconnect) — so on startup it names exactly the instances
    that had open tunnels when the gateway last stopped. We revive all of them
    so their tabs come back live, rather than reviving only the single
    last-active one (which left every other tab dead until a manual reconnect).

    Instances are revived one at a time so they don't race to bind their
    (mirrored) ports, and each attempt is wrapped so one unreachable host can
    neither abort the rest nor crash startup. A failed revive leaves
    ``was_connected`` true (the connect path never clears it on failure) and
    records a retained error, so its tab persists showing *why* it is down — the
    user re-authenticates in their own environment (SSH agent / SSO /
    whatever the host needs) and clicks Retry from the instance page. We do NOT
    pre-gate on any credential-staleness check: a failed connect simply surfaces
    its error, which is exactly the recovery affordance we want.

    Extracted to module level (rather than an inline closure) so the revive
    policy — which instances are picked and the per-instance failure isolation —
    is unit-testable without standing up the whole app.

    Stops at the first instance it reaches once a gateway restart has claimed
    ``state``: the restart's pre-exec seam has already stopped every tunnel, and
    one connected now would outlive the exec as an orphaned forwarder. The
    intent flag is untouched, so the next gateway revives the rest.
    """
    # A disabled crew keeps its intent so enabling it brings it back, but it is
    # not revived while it stays disabled.
    intended = [inst for inst in registry.list() if inst.was_connected and not inst.disabled]
    if not intended:
        return
    logger.info("Auto-reconnecting %d instance(s) on startup", len(intended))
    for inst in intended:
        if getattr(state, "_gateway_restart_in_progress", False) is True:
            logger.info("Gateway restart in progress; startup auto-reconnect stops")
            return
        try:
            st = await manager.connect(inst.id)
            if st.state == TunnelState.CONNECTED:
                logger.info("Auto-reconnected instance %s", inst.id)
            else:
                logger.warning(
                    "Startup auto-reconnect of %s did not connect (%s): %s",
                    inst.id,
                    st.state.value,
                    st.error,
                )
        except Exception:
            logger.warning("Startup auto-reconnect of %s failed", inst.id, exc_info=True)


def _register_instances_hooks(app: web.Application, state: DashboardState, port: int) -> None:
    """Register the opt-in Instances (multi-instance) startup/cleanup hooks.

    These MUST be attached before ``runner.setup()`` freezes the app's
    ``on_startup`` / ``on_cleanup`` signal lists. Appending after setup raises
    ``RuntimeError: Cannot modify frozen list`` AND the ``on_startup`` signal
    would have already fired, so a hook added late would never run.

    The registry + SSH tunnel manager are created lazily inside the startup
    hook (which fires during ``runner.setup()``), gated on ``instances.enabled``
    (default off). We then auto-reconnect every instance the operator left
    connected (``was_connected``) via :func:`_revive_intended_instances`, which
    isolates per-instance failures so a down host's tab persists in an error
    state instead of vanishing; the user re-authenticates and retries from the
    instance page.
    """

    # The startup hook's pre-exec registration, undone by the cleanup hook.
    unregister_pre_exec_stop: Callable[[], None] | None = None

    async def _instances_startup(app_: web.Application) -> None:
        nonlocal unregister_pre_exec_stop
        _cfg = KiroCrewConfig.load()
        if not _cfg.instances.enabled:
            return
        registry = InstancesRegistry()
        manager = SshTunnelManager(
            registry,
            base_port=_cfg.instances.tunnel_base_port,
            connect_timeout_secs=_cfg.instances.connect_timeout_secs,
            ssh_compression=_cfg.instances.ssh_compression,
            mint_timeout_secs=_cfg.instances.mint_timeout_secs,
            max_recovery_attempts=_cfg.instances.max_recovery_attempts,
            recover_backoff_max_secs=_cfg.instances.recover_backoff_max_secs,
            probe_failure_threshold=_cfg.instances.probe_failure_threshold,
            # The port this gateway ACTUALLY bound, not the configured guess:
            # it becomes the CSP frame-ancestor claim in every minted remote
            # token, and a claim that disagrees with the parent's real origin
            # makes the browser refuse to frame the remote pane.
            parent_port=port,
        )
        state.instances_registry = registry
        state.instances_manager = manager
        # An in-place restart's exec skips _instances_shutdown below but keeps
        # the pid, so every ssh/SSM forwarder child would outlive it still
        # parented to the new image, which then refuses to reclaim it. The
        # restart runs this same teardown first through the pre-exec seam.
        unregister_pre_exec_stop = register_pre_exec_stop("Instance tunnels", manager.shutdown)
        # First-party cookies: embedded instances load from
        # http://127.0.0.1:<port>, so the hub itself should be reached at
        # http://127.0.0.1:<port> (NOT localhost / kirocrew.localhost) — mixing
        # hosts makes the iframes render logged-out. The dashboard already binds
        # 127.0.0.1; we recommend (not force) the loopback-IP URL here so the
        # existing localhost / Slack-link flows are left untouched.
        logger.info(
            "Instances enabled — open the dashboard at http://127.0.0.1:%d for "
            "embedded instances to share first-party cookies.",
            port,
        )
        # Auto-reconnect intended instances in the BACKGROUND rather than
        # awaiting here. on_startup handlers fire during runner.setup(), BEFORE
        # site.start() binds the HTTP port, so awaiting serial SSH-tunnel
        # connects — each of which can hang for its full timeout when the
        # network/DNS is down — delayed the port bind well past the desktop
        # app's 30s gateway-wait window, producing a spurious "Retry/Quit"
        # dialog and relaunch loop. Firing it as a tracked background task lets
        # the port bind immediately; tunnels reconnect (or surface their error
        # on the instance tab, which persists on failure) without gating
        # startup.
        revive_task = asyncio.create_task(_retake_hops_then_revive(registry, manager, state))
        state._background_tasks.add(revive_task)
        revive_task.add_done_callback(state._background_tasks.discard)

    async def _instances_shutdown(app_: web.Application) -> None:
        if unregister_pre_exec_stop is not None:
            unregister_pre_exec_stop()
        manager = getattr(state, "instances_manager", None)
        if manager is not None:
            await manager.shutdown()

    async def _crew_log_drain(app_: web.Application) -> None:
        """Write out the session's log buffered appends before the process goes.

        The emitter hands appends to a writer thread so a turn never waits on the
        filesystem, which means a record can be in memory when shutdown starts.
        Exiting without this drops exactly the entries a reader most wants after a
        restart -- the last thing each session did. The drain is bounded inside
        the emitter, and runs in a thread so a slow disk delays the exit instead
        of blocking the loop that is closing everything else down.
        """
        try:
            # Imported here, not at module scope: this file is on the gateway boot
            # path, and the emitter is flag-gated behind KIROCREW_CREW_LOG.
            # AUTOSDE's no-new-work-on-gateway-boot-path rule asks for the IMPORT to
            # be gated, not just the handler, so a launch with the flag off pays
            # nothing for a subsystem it will never call.
            from kiro_crew.crew_log import emit as crew_log_emit

            await asyncio.to_thread(crew_log_emit.drain_for_shutdown)
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("crew log drain failed during shutdown", exc_info=True)

    app.on_startup.append(_instances_startup)
    app.on_cleanup.append(_instances_shutdown)
    app.on_cleanup.append(_crew_log_drain)
