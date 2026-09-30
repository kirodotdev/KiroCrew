"""The MCP gateway broker's lifecycle in this process.

Launch-approval load, filtering and deferred persistence, the agent-overlay
rewrite, the broker start and stop, the npm pre-resolve prefetch and its refresh
window, and the dashboard callbacks that enable, disable or re-stub the broker
live.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import (
        GatewayManager,
        GatewayOrchestrator,
        GatewaySpec,
        KiroCrewConfig,
        LaunchApprovals,
        asyncio,
        contextlib,
        filter_target_env,
        is_gateway_supported,
        live,
        load_approvals,
        logger,
        maintenance_executor,
        resolve_prefetch,
        rewrite_agents,
        rewrite_kwargs,
        save_pass,
        sel,
    )


def _init_mcp_discovery(self: GatewayOrchestrator) -> None:
    """Log configured MCP servers at startup.

    The actual config merge is handled by rebuild_agent_config() which
    runs earlier in __init__. This just logs what's configured for
    debugging visibility.
    """
    try:
        from kiro_crew.mcp_discovery import list_servers  # circular import

        servers = list_servers()
        if servers:
            srv_names = [s.name for s in servers]
            logger.info("Configured MCP servers: %s", ", ".join(srv_names))
        else:
            logger.info("No MCP servers configured")
    except Exception:
        logger.debug("MCP server listing failed", exc_info=True)


def _schedule_mcp_launch_approval_persist(
    self: GatewayOrchestrator, approvals: LaunchApprovals
) -> None:
    """Persist one rewrite pass after the process is ready to serve."""
    ready = getattr(self, "_mcp_launch_approval_ready", None)
    if ready is None:
        ready = asyncio.Event()
        self._mcp_launch_approval_ready = ready

    async def _persist() -> None:
        await ready.wait()  # type: ignore[union-attr]  # narrowed above
        try:
            await asyncio.to_thread(save_pass, approvals)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "mcp launch approvals: could not persist the approval store",
                exc_info=True,
            )

    task = asyncio.create_task(_persist(), name="mcp-launch-approval-persist")
    self._background_tasks.add(task)
    task.add_done_callback(self._background_tasks.discard)


async def _init_mcp_gateway(
    self: GatewayOrchestrator, stub_servers: frozenset[str] | None = None
) -> None:
    """Start the MCP gateway sidecar and populate the agent-JSON overlay.

    Runs iff at least one server gets a stub
    (``mcp_gateway.stub_servers``). Routing is what interposes a stub, and
    the stub is what carries both the render/callback path and any sharing —
    so nothing stubbed means there is nothing for a broker to serve. Sharing
    (``mcp_gateway.enabled``) is deliberately NOT part of this condition: it
    decides how a stubbed server's backend is acquired, and on its own routes
    nothing. Any failure downgrades to today's per-session MCP path — the
    stub's graceful fallback keeps kiro-cli sessions working even when the
    broker is unreachable.

    ``stub_servers`` overrides the configured set. A caller restarting the
    broker for an unrelated reason passes the set already being served, so a
    stub change recorded for the next gateway start is not applied early as a
    side effect of that unrelated restart.
    """
    cfg_gw = self._cfg.mcp_gateway
    stubs = frozenset(cfg_gw.stub_servers) if stub_servers is None else stub_servers
    if not stubs:
        return
    self._mcp_stub_servers_started = stubs
    # Runs on every platform the transport layer covers -- an AF_UNIX socket
    # on POSIX, a named pipe on Windows. Stub delivery is ACP session/new
    # injection, not a bind-mount, so no mount namespace is needed anywhere.
    if not is_gateway_supported():
        return

    rewrite_inputs = rewrite_kwargs(self._cfg, stubs)
    socket_path = rewrite_inputs["socket_path"]

    try:
        # The operator's approved launch fingerprints. The server NAME in
        # ``stub_servers`` is not proof of what runs: the command behind it
        # comes from agent-writable files, and gatewayd execs it outside
        # the sandbox. See ``mcp_gateway.launch_approval``.
        approvals = await asyncio.to_thread(load_approvals)

        # rewrite_agents() walks ~/.kiro/agents, parses every JSON spec and
        # rewrites the overlay — pure-sync file I/O. The target filter is
        # the last gateway-side stop before that map becomes gatewayd's
        # process env. Run both through one bounded maintenance-pool job so
        # neither blocks the event loop when triggered post-startup.
        def _rewrite_and_filter():
            rewrite_result, target_env = rewrite_agents(
                **rewrite_inputs,
                approvals=approvals,
            )
            target_env, dropped_targets = filter_target_env(target_env, approvals)
            return rewrite_result, target_env, dropped_targets

        (
            _rewrite_result,
            target_env,
            dropped_targets,
        ) = await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), _rewrite_and_filter
        )
    except Exception:
        logger.exception("mcp-gateway rewriter failed — falling back")
        return
    if dropped_targets:
        logger.warning(
            "mcp launch approvals: withheld %d unapproved target(s) from the gateway daemon",
            len(dropped_targets),
        )
    if approvals.refused:
        logger.warning(
            "mcp launch approvals: %d server launch(es) refused; review them in "
            "Settings > MCP Management",
            len(approvals.refused),
        )
    if approvals.captured or approvals.refused:
        # Names and counts only; a launch's args and env may carry tokens.
        try:
            sel().log_api_access(
                caller="gateway",
                operation="mcp_launch_approval",
                outcome="denied" if approvals.refused else "recorded",
                source="gateway",
                resources=(
                    f"recorded={','.join(sorted(approvals.names.get(s, s) for s in approvals.captured))} "
                    f"refused={','.join(sorted(approvals.names.get(s, s) for s in approvals.refused))}"
                ),
            )
        except Exception:
            logger.debug("mcp launch approvals: SEL write failed", exc_info=True)

    manager = GatewayManager(
        GatewaySpec(
            socket_path=socket_path,
            idle_timeout_secs=cfg_gw.idle_timeout_secs,
            max_backends=cfg_gw.max_backends,
            mcp_target_env=target_env,
            prewarm_count=cfg_gw.prewarm_count,
            # Admission keys ride the daemon's argv like max_backends.
            spawn_concurrency_initial=cfg_gw.spawn_concurrency_initial,
            spawn_concurrency_min=cfg_gw.spawn_concurrency_min,
            spawn_concurrency_max=cfg_gw.spawn_concurrency_max,
            spawn_queue_wait_secs=cfg_gw.spawn_queue_wait_secs,
            initialize_timeout_secs=cfg_gw.initialize_timeout_secs,
            host_budget_max_procs=cfg_gw.host_budget_max_procs,
            host_budget_max_rss_mb=cfg_gw.host_budget_max_rss_mb,
            host_budget_max_fds=cfg_gw.host_budget_max_fds,
        )
    )
    # Pre-resolve npm-launcher targets in the background. An npx spec asks the
    # registry what it means on every launch; once resolved, the daemon execs
    # the installed tree instead, so session start does no resolution and
    # needs no network. Fired detached and never awaited: a launch that beats
    # the prefetch just uses today's path, so blocking startup on installs
    # would trade the stall we are removing for one at boot.
    self._mcp_target_env = dict(target_env)
    self._mcp_resolve_prefetch = asyncio.create_task(
        self._mcp_resolve_prefetch_loop(dict(target_env))
    )
    if await manager.start():
        self._mcp_gateway_manager = manager
        # Admission already uses the in-memory, fail-closed filtered map.
        # Persistence waits for the process readiness boundary and cannot
        # extend the boot path.
        self._schedule_mcp_launch_approval_persist(approvals)
        # Report the stub set and the sharing decision. There is one
        # trigger now (something is stubbed), so the useful line is WHAT it
        # serves: "N routed" beside a live daemon explains itself, and the
        # sharing suffix stops "sharing: off" next to a running broker from
        # reading as a contradiction.
        #
        # Counts ``stubs``, not the configured list. The two differ whenever a
        # stub change is recorded for the next gateway start, and this line is
        # read during exactly that diagnosis ("why is my stub not live?") --
        # reporting the configured count there would answer it wrongly.
        logger.info(
            "mcp-gateway: broker ready (socket=%s) for %d stubbed server(s), " "backend sharing %s",
            socket_path,
            len(stubs),
            "on" if cfg_gw.enabled else "off",
        )


def _mcp_resolve_refresh_secs(self: GatewayOrchestrator) -> float:
    """``mcp_gateway.resolve_once_refresh_hours`` as of the latest config, in seconds.

    Read from the live snapshot rather than the boot ``self._cfg`` so the
    prefetch loop's per-iteration re-read is a real re-read: a longer or
    shorter window written to config.json is honoured on the next pass, as
    the loop's docstring promises. Before the watcher has primed it falls
    back to the boot copy -- never a ``load()`` here, which parses and
    validates the file on the event loop.
    """
    cfg = live.snapshot() or self._cfg
    return float(cfg.mcp_gateway.resolve_once_refresh_hours) * 3600.0


async def _mcp_resolve_prefetch_loop(self: GatewayOrchestrator, target_env: dict[str, str]) -> None:
    """Run the pre-resolve pass on the configured cadence until cancelled.

    A single startup pass is not enough to make
    ``resolve_once_refresh_hours`` mean what it says. Staleness is consulted
    only when a pass runs, and ``resolved_launch`` ignores it by design, so
    on a gateway that stays up for weeks -- the normal case -- an unpinned
    ``@latest`` spec would freeze at whatever it resolved to on boot and
    silently stop picking up upstream fixes. The window needs something to
    tick it.

    Sleeps for the refresh window itself between passes: each pass already
    skips anything not yet stale, so the cadence only has to be fine enough
    that "expires after N hours" is honoured within about one window.

    Never returns normally -- ``_stop_mcp_broker`` cancels it, which is the
    only exit. The window is re-read every iteration so a config reload takes
    effect on the next pass without a broker restart.
    """
    while True:
        await self._prefetch_mcp_resolutions(target_env)
        window = self._mcp_resolve_refresh_secs()
        await asyncio.sleep(max(window, self._MCP_RESOLVE_MIN_SLEEP_SECS))


async def _prefetch_mcp_resolutions(
    self: GatewayOrchestrator, target_env: dict[str, str], *, force: bool = False
) -> dict[str, str]:
    """Pre-resolve npm-launcher MCP targets so launches skip dependency resolution.

    ``force`` bypasses the freshness window -- that is the operator asking to
    go to the registry now, rather than asking whether it is time to.

    Errors are logged and swallowed: a resolution that does not land leaves
    the server launching exactly the way it does today.
    """

    refresh_secs = self._mcp_resolve_refresh_secs()
    try:
        outcomes = await resolve_prefetch(
            self._mcp_resolve_home, target_env, refresh_secs=refresh_secs, force=force
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("mcp-gateway: pre-resolve pass failed")
        return {}
    ready = sorted(pkg for pkg, state in outcomes.items() if state == "ready")
    if ready:
        logger.info(
            "mcp-gateway: %d npm target(s) pre-resolved; their launches now "
            "skip dependency resolution (%s)",
            len(ready),
            ", ".join(ready),
        )
    return outcomes


async def _refresh_mcp_resolutions(self: GatewayOrchestrator) -> dict:
    """Dashboard callback: re-resolve every npm-launcher MCP target now.

    This is the explicit half of the freshness policy. The timed pass asks
    "is it time to check upstream?"; pressing this says "check upstream",
    so it forces past the window even for a pinned spec.

    Awaited rather than detached, because the caller is a person waiting for
    an answer -- unlike the startup pass, whose whole point is not to block.
    Reports which packages are now ready so the UI can say what happened
    instead of only that something was attempted.
    """

    self._cfg = KiroCrewConfig.load()
    target_env = dict(self._mcp_target_env)
    if not target_env:
        # No broker start has computed a target set, so there is nothing this
        # could refresh. Say so rather than reporting an empty success.
        return {"ok": False, "reason": "no_targets", "resolved": {}}
    outcomes = await self._prefetch_mcp_resolutions(target_env, force=True)
    return {
        "ok": True,
        "resolved": outcomes,
        "ready": sorted(pkg for pkg, state in outcomes.items() if state == "ready"),
    }


async def _stop_mcp_broker(self: GatewayOrchestrator) -> None:
    """Stop the MCP gateway broker if running and clear the handle."""
    task = self._mcp_resolve_prefetch
    self._mcp_resolve_prefetch = None
    if task is not None and not task.done():
        # An install in flight has nothing left to serve once the broker is
        # gone, and leaving it running would race the next pass.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    mgr = self._mcp_gateway_manager
    self._mcp_gateway_manager = None
    if mgr is not None:
        try:
            await mgr.shutdown()
        except Exception:
            logger.exception("mcp-gateway: broker shutdown failed")


async def _apply_mcp_gateway_enabled(self: GatewayOrchestrator, enabled: bool) -> dict:
    """Dashboard callback: apply the persisted ``mcp_gateway.enabled``
    flag in-process (start/stop the broker), no gateway restart.

    Reloads config so it acts on the value the handler just wrote.
    Returns ``{enabled, running, ping_ok}``.

    The flag governs backend SHARING, not the broker's existence: a routed
    server needs its stub either way. So turning sharing off restarts the
    broker rather than stopping it whenever something is still routed — a
    plain stop would take away the render and callback paths of servers the
    operator never unstubbed. The restart is required, not incidental: the
    rewriter reads the sharing flag when the broker starts, so re-running it
    is what re-emits every stub WITHOUT ``--poolable`` and actually stops the
    sharing the operator just turned off.
    The restart re-emits the stub set the broker is ALREADY serving, not the
    configured one. A stub change is recorded for the next gateway start, so
    consuming it here would apply it early as a side effect of an unrelated
    sharing edit -- the operator was told that change is waiting, and the
    broker cycle that carried it would also cancel the in-flight tool calls
    of every session attached to the old daemon.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    self._cfg = KiroCrewConfig.load()
    serving = self._mcp_stub_servers_started
    if self._mcp_gateway_manager is not None:
        await self._stop_mcp_broker()
    if serving:
        await self._init_mcp_gateway(stub_servers=serving)
    mgr = self._mcp_gateway_manager
    if self.dashboard_state is not None:
        self.dashboard_state._mcp_gateway_manager = mgr
    # Rebuild the provider factory so new sessions resolve the overlay
    # path from the CURRENT config, not the value captured at boot.
    # refresh_defaults() rebuilds the factory and drains the warm pool
    # without killing live sessions — the correct semantics since a
    # running session has already sent session/new and cannot be
    # retrofitted.
    #
    # Skipped while the configured set disagrees with what is being served,
    # because the factory's overlay decision is all-or-nothing on the
    # CONFIGURED list: ``config.loader`` passes ``mcp_gateway_overlay`` only
    # ``if _gw.stub_servers`` and otherwise passes None, which drops the
    # gateway out of the path entirely. So refreshing right after the last
    # server was unstubbed would hand new sessions no overlay at all -- they
    # would bypass the broker that is still serving that stub, which is the
    # pending change taking effect early on an unrelated sharing edit. When
    # the two agree the refresh is a no-op for the overlay, so the guard only
    # ever suppresses the disagreeing case.
    if self.sessions is not None and frozenset(self._cfg.mcp_gateway.stub_servers) == serving:
        await self.sessions.refresh_defaults()
    if mgr is None:
        return {"enabled": enabled, "running": False, "ping_ok": False}
    running = bool(mgr.is_running)
    ping_ok = bool(running and await mgr.ping())
    return {"enabled": enabled, "running": running, "ping_ok": ping_ok}


async def _apply_mcp_stub(self: GatewayOrchestrator) -> dict:
    """Dashboard callback: record a stub change for the NEXT gateway start.

    Deliberately leaves the running broker alone, because there is nothing
    useful to do to it. A session's MCP toolset is fixed at ``session/new``,
    so no running session can adopt a new stub set however the broker is
    cycled; the change only ever matters to sessions created later, and the
    next start builds their routing from this config.

    Restarting to shorten that wait still destroys work: the drain gives
    in-flight tool calls ``DRAIN_SECS`` to finish and then cancels them.
    The stub re-attaches to the replacement daemon afterwards, so those
    servers are not lost for the session's life -- but a cancelled call
    is still a cancelled call, and the restart buys the running session
    nothing, because its toolset was fixed at ``session/new``.

    Rewriting the agent specs without restarting is worse still: a new
    session would route a server through the stub while the running daemon
    has no target for it, and an unknown target is a TERMINAL rejection in
    ``stub.py``, not a fallback. The spec rewrite and the daemon's routing
    environment are built together at startup and must stay that way, so
    this callback persists intent only and reports that a restart is needed.
    """
    from kiro_crew.config.loader import KiroCrewConfig

    self._cfg = KiroCrewConfig.load()
    return {
        "applied": False,
        "restart_required": True,
        "stub_servers": sorted(self._cfg.mcp_gateway.stub_servers),
    }


def _wire_mcp_gateway_dashboard(self: GatewayOrchestrator) -> None:
    """Publish the broker + apply callbacks onto DashboardState.

    _init_mcp_gateway runs at boot before dashboard_state exists, so
    the manager and the enable/poolable callbacks are attached here
    (post dashboard init). The /api/mcp-gateway/* handlers read these
    off ``request.app['state']``.
    """
    if self.dashboard_state is None:
        return
    self.dashboard_state._mcp_gateway_manager = self._mcp_gateway_manager
    self.dashboard_state._mcp_gateway_apply = self._apply_mcp_gateway_enabled
    self.dashboard_state._mcp_gateway_apply_stub = self._apply_mcp_stub
    self.dashboard_state._mcp_resolve_refresh = self._refresh_mcp_resolutions
    # Read by the dashboard restart handler, which execs without running
    # ``_shutdown``: the broker this process owns must still die with it.
    self.dashboard_state._mcp_gateway_stop = self._stop_mcp_broker
