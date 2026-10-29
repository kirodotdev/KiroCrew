"""The headless API server's boot sequencer, ``start_api_server``.

The ``--slack-only`` gateway's app: its routes, the API middleware chain, the
lifecycle hooks, the bind and the bound-port publication, and the post-bind work.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

from kiro_crew.dashboard.state import _DEFAULT_PORT

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _MAX_HEADER_FIELD_SIZE,
        ContextBuilder,
        CronService,
        DashboardState,
        KiroCrewConfig,
        LessonStore,
        ScriptHookStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
        _arm_listener_guard,
        _arm_prevent_sleep_poll,
        _arm_secondary_listener_guard,
        _dispatch_healthy_boot_marker,
        _export_bound_port,
        _install_api_middlewares,
        _kick_config_watch,
        _kick_connections_warm_scavenge,
        _kick_display_verdicts,
        _kick_local_decision_model,
        _kick_owner_only_sweep,
        _kick_pinned_order_load,
        _kick_session_search_index,
        _kick_workflow_initialization,
        _make_csrf_middleware,
        _make_deny_audit_middleware,
        _make_host_validation_middleware,
        _note_listener_sidecar,
        _precompute_telemetry,
        _reconcile_listener_publication,
        _register_browser_install_cleanup,
        _register_config_watch,
        _register_connections_warm_lifecycle,
        _register_deploy_routes,
        _register_kiro_service_shutdown,
        _register_listener_guard_shutdown,
        _register_mcp_routes,
        _register_own_host_warm,
        _register_prevent_sleep_shutdown,
        _register_stt_hooks,
        _register_unix_socket_cleanup,
        _register_workflow_lifecycle,
        _resolve_tailnet_trust,
        _resolved_bound_host,
        _resolved_bound_port,
        _start_secondary_loopback_site,
        _start_site,
        _start_unix_site,
        _tailnet_origin_enabled,
        _write_instance_credentials,
        bind_address_for,
        build_allowed_origins,
        build_hardened_runner,
        crewmate_feed,
        data_home,
        handlers,
        logger,
        record_boot_to_ready,
        register_skill_read_observer,
        set_global_hook_store,
        subprocess_executor,
        tailnet,
        warm_auth_singletons,
        warm_sel_singleton,
        wire_session_subagent_probe,
    )


async def start_api_server(
    sessions: SessionManager,
    crons: CronService,
    lessons: LessonStore,
    port: int = _DEFAULT_PORT,
    subagents: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    slack_client: Any = None,
    owner_id: str = "",
    local_only: bool = True,
    configured_host: str = "",
    assume_kiro_ready: bool = False,
    # The KiroPrerequisiteService whose enterprise boot probe saw the login.
    kiro_prerequisite_service: Any = None,
    conversation_log: Any = None,
    schedule_memory_preparation: "Callable[[], asyncio.Task[None] | None] | None" = None,
    context_builder: ContextBuilder | None = None,
) -> tuple[web.AppRunner, DashboardState]:
    """Start a minimal API-only server for MCP tool transport (no UI).

    Headless (``--slack-only``) mode. This server exposes the SAME
    state-changing MCP tool routes as the dashboard (``_register_mcp_routes``),
    so it MUST authenticate them at parity with ``start_dashboard``: loopback is
    NOT a trust boundary (local port forwarders and any web page the user opens
    can reach 127.0.0.1), so the internal MCP routes require the
    ``X-Internal-Secret`` machine-to-machine handshake, and state-changing
    requests are guarded against DNS-rebinding (Host) and cross-site browsers
    (Origin). Every in-repo caller (mcp-core, cron) already sends the secret.
    """
    if task_runner is not None:
        task_runner.defer_workflow_attachment()
    state = DashboardState(
        sessions=sessions,
        crons=crons,
        lessons=lessons,
        start_time=time.time(),
        subagents=subagents,
        task_runner=task_runner,
        slack_client=slack_client,
        owner_id=owner_id,
        # Headless mode has no UI, but it still runs Slack turns -- and anything
        # that reasons about how far a conversation has got reads the transcript
        # through here. Leaving it unset made those readers fall back to their
        # can't-tell branch: an OPTIONS control posted in this mode carried no
        # position and every click on it was honoured, however stale.
        conversation_log=conversation_log,
        context_builder=context_builder,
    )
    state._hook_store = ScriptHookStore()
    set_global_hook_store(state._hook_store)

    # API-only gateways share the orchestrator's context builder. Standalone
    # callers may omit it; try the task runner's loader before reporting a miss.
    if not register_skill_read_observer(state.context_builder, getattr(task_runner, "_ctx", None)):
        logger.info("skill-read observer not registered: no skills loader reachable")

    # Wire script hooks into subagent tool execution path
    if state.subagents is not None:
        state.subagents.hook_store = state._hook_store

    # Visible notice + pct reset when auto-compaction fires on a dashboard session
    state.wire_session_compact_callback()
    # Visible notice when the watchdog recycles a dashboard session (e.g. RSS)
    state.wire_session_recycle_callback()
    # The RSS ceiling must not recycle a parent whose sub-agents are still
    # running on its runtime; the manager cannot see them without this probe.
    wire_session_subagent_probe(state)
    # Visible notice in a channel that just lost its session-resume binding
    state.wire_session_unbind_listener()
    # Crew-log class record for a binding that just COMMITTED, taken before anything
    # can be routed through it
    state.wire_session_bind_listener()

    app = web.Application(
        client_max_size=60 * 1024 * 1024
    )  # 60 MB: covers a 50 MB BUFFERED upload + multipart overhead. NOT a
    # ceiling on every upload: aiohttp enforces this in Request.read()/.post(),
    # not on the streaming multipart() reader, so the video path in
    # handlers/files.py streams past it under its own _MAX_VIDEO_UPLOAD_BYTES
    # (pinned by test_streaming_bypasses_the_app_client_max_size). Reading this
    # number as a global request cap is the false invariant to avoid.
    app["state"] = state
    # Bind the serving loop once, here: this runs ON that loop, so every
    # surface that later hands work in from a foreign thread -- slots
    # coalescing, an off-loop websocket send, the log handler's fan-out --
    # resolves the same loop instead of each latching its own copy from
    # whichever thread happens to arrive first.
    state.bind_serving_loop(asyncio.get_running_loop())
    crewmate_feed.start(state)  # the goal and waiting folds a crewmate's page reads
    # Voice settings live in slack/handler's module state and are otherwise
    # loaded only on the Slack startup path (set_orch_cfg) — without this a
    # dashboard-only gateway (no Slack tokens) resets TTS to defaults on
    # every restart (see load_voice_reply_config).
    from kiro_crew.slack.handler import load_voice_reply_config

    await asyncio.to_thread(load_voice_reply_config)
    # Enterprise build only: no kiro login, no Crew. The gateway hands in the
    # service whose boot probe saw the login; a caller that did not run that
    # check gets it here, still before the bind. A failure exits non-zero.
    from kiro_crew.enterprise_boot import require_kiro_login
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

    if kiro_prerequisite_service is None:
        kiro_prerequisite_service = await require_kiro_login(assume_ready=assume_kiro_ready)
    if kiro_prerequisite_service is None:
        kiro_prerequisite_service = await asyncio.to_thread(
            KiroPrerequisiteService,
            assume_ready=assume_kiro_ready,
        )
    app["kiro_prerequisite_service"] = kiro_prerequisite_service
    state.kiro_prerequisite_service = app["kiro_prerequisite_service"]
    # Enterprise build only: the gate starts open on that verified login and
    # closes if it expires. The middleware chain installed below reads it.
    from kiro_crew.dashboard.enterprise_gate_middleware import install_enterprise_gate

    install_enterprise_gate(app, app["kiro_prerequisite_service"])
    # A signed-out bundled kiro-cli-chat fails a prompt as a bare connection
    # error; the ACP layer asks this service's whoami verdict to tell it apart.
    from kiro_crew.kiro_prerequisite import register_turn_auth_service

    register_turn_auth_service(state.kiro_prerequisite_service)
    # Seed the retirement baseline with the account on disk RIGHT NOW, before
    # anything can spawn a kiro-backed child. Every child postdates this read,
    # so the once-per-lifetime unset-baseline boot sweep -- which on a live
    # gateway can never satisfy its completion precondition and degenerates
    # into a retire/respawn loop -- is unnecessary: a real account change after
    # this still compares unequal and sweeps. A store that cannot be
    # fingerprinted refuses the seed and keeps the fail-safe sweep.
    await app["kiro_prerequisite_service"].seed_sessions_baseline()
    # Stamp every kiro-backed spawn with the account the store holds at that
    # moment: the turn gate compares those stamps against its fresh read, so a
    # child from an account round trip NO read ever observed -- the one case
    # the seeded baseline and the interim latch are both blind to -- is still
    # retired before reuse (see flag_identity_stamp_mismatches). Unwired (the
    # CLI, tests), spawns stay unstamped and keep the pre-stamping behavior.
    state.sessions.spawn_identity_reader = app["kiro_prerequisite_service"].read_spawn_identity
    # Probe Kiro readiness during boot rather than on the dashboard's first
    # status request: the cold probe spawns sandboxed CLI subprocesses and can
    # take seconds, which is what made the first-run setup chrome visible to
    # returning users. Fire-and-forget — a warm-up is never a boot dependency,
    # and the task is cancelled by the service's shutdown hook.
    app["kiro_prerequisite_service"].warm_up()
    state.load_folders()
    # Off-loop: a large cron_folders.json would otherwise block the event
    # loop with synchronous file I/O + JSON parsing during startup.
    await asyncio.to_thread(state.load_cron_folders)
    # Off-loop: a large chat_pins.json must not block the event loop at startup.
    await asyncio.to_thread(state.load_chat_pins)
    # Off-loop: load_tags runs a synchronous save_tags() during load (status
    # back-fill / seed) which fsyncs on the event loop; a large tags.json —
    # including preserved-but-malformed rows — must not stall startup.
    await asyncio.to_thread(state.load_tags)
    app["port"] = port

    _precompute_telemetry(state)

    # ── Auth parity with start_dashboard ─────────────────────────────────────
    # The MCP route surface is identical to the dashboard's, so the middleware
    # chain must be too. Host-allowlist source of truth is shared with the CSRF
    # Origin check via build_allowed_origins/build_allowed_hosts (see origin.py).
    _cfg = KiroCrewConfig.load()
    _ts_cfg = _cfg.dashboard.tailscale
    _tailnet_host = await tailnet.resolve_tailnet_host(_ts_cfg.enabled)
    # Same identity-trust value as start_dashboard, via the same shared helper
    # — the auth surface is identical, so the middleware inputs must be too.
    _tailnet_trust = await _resolve_tailnet_trust(_cfg)
    app["allowed_origins"] = build_allowed_origins(
        port,
        local_only,
        configured_host,
        tailnet_host=_tailnet_host,
    )
    # Stashed for the same reason as in start_dashboard, and set here too even
    # though /api/tailnet/status is registered on the dashboard app: leaving one of
    # the two startup paths without the keys is exactly the class of bug an earlier
    # round of this feature already shipped, and a handler moved into the MCP
    # surface later would silently read "" as "nothing was trusted".
    app["tailnet_host"] = _tailnet_host
    app["tailnet_resolved_at"] = int(time.time()) if _tailnet_host else 0
    # The governance-filtered identity-trust value the middleware was built
    # with, for handlers the middleware bypasses (POST /api/auth/refresh must
    # re-bind a rotated access token to the same verified peer identity).
    app["tailnet_trust"] = _tailnet_trust
    app["local_only"] = local_only
    # Parity with the full dashboard: headless gateways have the same live
    # Origin/Host boundary and must recover the same boot race without restart.
    tailnet.install_tailnet_origin_recovery(
        app,
        enabled=_ts_cfg.enabled,
        initial_host=_tailnet_host,
        load_enabled=_tailnet_origin_enabled,
    )

    # Per-session internal secret for machine-to-machine (mcp-core, cron) auth.
    # Deferred file write (and parent mkdir) until after the port binds (mirrors
    # start_dashboard): both live in _write_secret_file, offloaded below, so a
    # failed second instance never poisons the live gateway's secret file and no
    # blocking fs I/O runs on the event loop.
    _secret_path = data_home() / ".local_secret"
    _internal_secret = os.urandom(16).hex()
    app["local_secret"] = _internal_secret

    # DNS-rebinding defense-in-depth, parity with start_dashboard by
    # construction — the SAME factory builds both barriers, including the
    # orchestrator probe exemption (see _make_host_validation_middleware /
    # origin.PROBE_PATHS): headless gateways are the instances most likely to
    # sit behind an orchestrator addressing them by pod/container IP.
    host_validation_middleware = _make_host_validation_middleware("mcp_tool")
    # Cross-site CSRF barrier at parity with start_dashboard by construction —
    # the SAME factory builds both, including the self-authenticating-webhook
    # exemption (see _make_csrf_middleware).
    csrf_middleware = _make_csrf_middleware("mcp_tool")
    # Audit boundary at parity with start_dashboard by construction — the SAME
    # factory builds both, so a pre-audit refusal cannot be positional on one
    # entrypoint and per-site on the other (see _make_deny_audit_middleware).
    deny_audit_middleware = _make_deny_audit_middleware("mcp_tool")

    # Warm the auth singletons off the event loop before building the chain
    # (parity with start_dashboard) so no blocking key-file I/O hits the loop.
    await warm_auth_singletons()

    # Warm the SecurityEventLog singleton off the loop (parity with
    # start_dashboard) so the first audit on this entrypoint is also a
    # non-blocking enqueue, never an on-loop ``_init_locked``.
    await warm_sel_singleton()

    # The ordered chain lives beside the dashboard's in one owner, so the two cannot
    # drift apart unseen.
    _install_api_middlewares(
        app,
        deny_audit_middleware=deny_audit_middleware,
        host_validation_middleware=host_validation_middleware,
        csrf_middleware=csrf_middleware,
        internal_secret=_internal_secret,
        port=port,
        local_only=local_only,
        tailnet_trust=_tailnet_trust,
    )

    _register_mcp_routes(app)

    # Probe parity with the full dashboard server. Headless gateways are often
    # the instances most likely to sit behind an orchestrator, so they must
    # expose the same unauthenticated, secret-free liveness/readiness surface.
    app.router.add_get("/api/health", handlers.api_health)
    app.router.add_get("/api/live", handlers.api_live)
    app.router.add_get("/api/ready", handlers.api_ready)

    # R16 F6: Deploy routes must be registered in api-only mode too, otherwise
    # the deploy_artifact MCP tool 404s in Slack-only/headless mode.
    _register_deploy_routes(app)

    _register_kiro_service_shutdown(app)

    # Releases the resident speech model (148MB default, 1.6GB largest) when idle
    # and at shutdown. Registered here, before runner.setup freezes the signal lists.
    _register_stt_hooks(app)
    # Own-address read for the ssh self-target floor, started at boot.
    _register_own_host_warm(app)
    # Same live-config watcher as start_dashboard: a headless gateway must pick
    # up a CLI or $EDITOR write identically.
    _register_config_watch(app, state, _cfg)

    # Prevent-sleep shutdown hook — registered before runner.setup freezes the
    # signal lists; the poll itself is armed after the port binds (below). This
    # is what makes headless --slack-only keep the host awake during a long
    # Slack task, identically to the full dashboard.
    _register_prevent_sleep_shutdown(app, state)
    _register_listener_guard_shutdown(app, state)
    _register_browser_install_cleanup(app, state)
    _register_connections_warm_lifecycle(app, state)
    _register_workflow_lifecycle(app, state)

    # Unix-socket cleanup hook — same holder pattern as start_dashboard,
    # registered before runner.setup freezes the signal lists.
    _unix_socket_holder: dict[str, Path | None] = {"path": None}
    _register_unix_socket_cleanup(app, _unix_socket_holder)

    # Hardened runner: same slowloris / CWE-400 mitigation as start_dashboard,
    # plus the raised max_field_size (see start_dashboard for the cookie-jar
    # rationale).
    runner = build_hardened_runner(app, max_field_size=_MAX_HEADER_FIELD_SIZE)
    await runner.setup()
    # Same bind resolution as start_dashboard: loopback unless the operator
    # widened it (dashboard.url opt-out of local_only, or the KIROCREW_BIND
    # container override honored inside bind_address_for). Without this the
    # documented `gateway --slack-only` container path would silently bind
    # loopback and be unreachable through a published Docker port.
    bind_addr = bind_address_for(local_only)
    site = web.TCPSite(runner, bind_addr, port)
    await _start_site(site, port)
    # After the bind, never on the boot path (no-new-work-on-gateway-boot-path):
    # from here an app cron naming a missing folder creates it through the
    # dashboard's own folder service. Boot-time registration stays read-only.
    from kiro_crew.dashboard.handlers.cron_board import wire_app_cron_folder_creator

    wire_app_cron_folder_creator(state)
    # Same listener guard as start_dashboard: a headless gateway loses its
    # listener to a failed accept() exactly the same way.
    _arm_listener_guard(state, runner, site)
    _kick_pinned_order_load(state)
    # Export the actually-bound port for child processes (parity with
    # start_dashboard — headless gateways spawn the same MCP stdio children).
    _export_bound_port(runner, port)
    # Additional kernel-verifiable transport for the internal API (parity with
    # start_dashboard; POSIX only, degrades to TCP-only on any failure).
    _unix_socket_holder["path"] = await _start_unix_site(runner, port)
    # Parity with start_dashboard: hold the other loopback family so a client
    # dialling a NAME cannot be answered by anyone else.
    # Same resolve-once rule as start_dashboard: `--port auto` leaves the
    # requested port at 0, and the second family must bind the port the sidecar
    # will name.
    _bound_port = _resolved_bound_port(runner, port)
    _second_loopback = await _start_secondary_loopback_site(
        runner, _bound_port, _resolved_bound_host(runner, bind_addr)
    )

    # Port bind succeeded — now safe to persist the secret file (parity with
    # start_dashboard: write deferred so a failed bind can't poison it).
    # Offloaded: _write_secret_file does blocking fs I/O (os.open/os.close and,
    # on Windows, the owner-only DACL), so it must not run
    # on the event loop (no-blocking-call-on-event-loop). Same per-listener
    # publication as start_dashboard: both surfaces must pair the credential
    # with the address AND the port, or a client that dialled one address can
    # resolve the credential of a listener sharing only the port number.
    try:
        await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(),
            _write_instance_credentials,
            _secret_path,
            _bound_port,
            _resolved_bound_host(runner, bind_addr),
            _internal_secret,
            (_second_loopback.address,) if _second_loopback else (),
        )
    except OSError:
        await runner.cleanup()
        raise

    # Parity with start_dashboard: record the claims only after the write landed,
    # then guard the second family so its sidecar cannot outlive its listener.
    _note_listener_sidecar(
        state,
        "primary",
        _bound_port,
        _resolved_bound_host(runner, bind_addr),
        _internal_secret,
    )
    # Both startup paths publish through the same executor await, so both own the
    # same window and both reconcile every claim in it. Arming alone leaves the 60s
    # probe as the only recovery, which is a live sidecar for an address this
    # gateway does not hold for up to a minute -- and a guard armed without a
    # reconcile is the harder defect to find, because the code looks complete.
    _reconcile_listener_publication(
        state, "primary", _bound_port, _resolved_bound_host(runner, bind_addr)
    )
    if _second_loopback is not None:
        _note_listener_sidecar(
            state, "secondary", _bound_port, _second_loopback.address, _internal_secret
        )
        _arm_secondary_listener_guard(state, runner, _second_loopback, _bound_port)
        _reconcile_listener_publication(state, "secondary", _bound_port, _second_loopback.address)

    # Listener is bound — kick the warm crash-residue scavenge (parity with
    # start_dashboard: never an on_startup hook, which would run the deferred
    # import before the bind).
    _kick_workflow_initialization(state)
    _kick_connections_warm_scavenge(state)
    _kick_session_search_index(state)
    _kick_config_watch(app, state)
    _kick_local_decision_model(state)
    _kick_owner_only_sweep(state)
    _kick_display_verdicts(state)

    logger.info("API-only server listening on %s:%d", bind_addr, port)

    # Arm the prevent-sleep poll now the loop is up and the port is bound
    # (shutdown hook already registered above). Headless --slack-only mode keeps
    # the host awake during a long Slack task exactly as the full dashboard does.
    _arm_prevent_sleep_poll(state, port)

    # Boot-to-ready (rec #1): headless API server is bound and ready. Privacy-safe
    # fixed labels only; best-effort.
    # Publish the gateway's shared memory task at the same no-yield boundary as
    # the full dashboard. Headless MCP/chat callers therefore see the barrier
    # whenever they can observe ready=True.
    if schedule_memory_preparation is not None:
        state.memory_startup_task = schedule_memory_preparation()
    state.ready = True
    record_boot_to_ready((time.time() - state.start_time) * 1000.0, server="api")
    # Tells the NEXT boot that this instance got the whole startup battery
    # away, so a stall from here on does not implicate the battery.
    _dispatch_healthy_boot_marker(state)

    return runner, state
