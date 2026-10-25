"""The process entry point.

``run_gateway`` with its boot preflight (the token signing key) and the background
sweeps it starts (KAS kiro-cli floor, predecessor work directories, orphaned
read-only specs), and the remote session environments' retention sweeper with its
shutdown stop.

Orchestrator fields read: none.
Orchestrator fields written: none.

Composed by :mod:`kiro_crew.slack.gateway`, whose globals its functions run on;
see :mod:`kiro_crew.slack.gateway_runtime`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    import contextlib
    import logging
    import sys
    import threading

    from kiro_crew.slack.gateway import (
        Any,
        GatewayOrchestrator,
        KiroCrewConfig,
        agent_scratch,
        apply_kas_cli_floor,
        beacon,
        boot_platform,
        configure_default_executor,
        ensure_agents_slice_limits,
        kiro_agents_dir,
        kiro_crew,
        logger,
        read_effective_agent,
        session_work_dir,
        set_publish_disabled,
        sweep_agents_dir,
        token_secret,
        work_root,
        workspace_root,
    )

    # The facade's task globals, which these functions write through ``global``
    # on the facade's namespace; declared here for the type checker only.
    _SLICE_LIMITS_TASK: asyncio.Task[None] | None
    _KAS_CLI_FLOOR_TASK: asyncio.Task[None] | None
    _AGENTS_JANITOR_TASK: asyncio.Task[None] | None
    _AGENT_SCRATCH_SWEEP_TASK: asyncio.Task[None] | None
    _KIRO_CLI_LOG_CAP_TASK: asyncio.Task[None] | None
    _CODER_RETENTION_TASK: asyncio.Task[None] | None


def _live_session_work_dirs(sessions: Any) -> list[str]:
    """The work directories of every provider the session registry holds now."""
    if sessions is None:
        return []
    return [
        path
        for path in (str(getattr(p, "cwd", "") or "") for p in sessions.active_providers())
        if path
    ]


def _live_session_agents(sessions: Any) -> set[str]:
    """The agent names every provider the session registry holds now was spawned with."""
    if sessions is None:
        return set()
    return {agent for agent in map(read_effective_agent, sessions.active_providers()) if agent}


def _sweep_predecessor_session_work_dirs(live_work_dirs: list[str]) -> int:
    """Sweep the run directories a dead predecessor of this data home left, off-loop.

    The evidence is this home's own: its pid ledger (``retained_gateway_pids``,
    read under the ledger's lock) and *live_work_dirs* from its session
    registry. An unreadable ledger raises, and the callers then sweep nothing.
    """
    from kiro_crew.session_pid import retained_gateway_pids

    return session_work_dir.sweep_predecessor_work_dirs(
        workspace_root(),
        retained_gateway_pids=retained_gateway_pids(),
        live_work_dirs=live_work_dirs,
    )


async def _run_signing_key_preflight_bounded(timeout: float | None = None) -> None:
    """Run :func:`signing_key_preflight` off the loop, with a deadline.

    A daemon thread rather than ``asyncio.to_thread``: a default-executor worker
    stuck in a filesystem call is joined at interpreter exit, so the timeout's
    ``SystemExit`` would itself hang. A daemon thread is abandoned at exit, which
    is what lets the boot fail closed when the data home stops answering.

    Raises whatever the preflight raised (its ``SystemExit`` refusal included),
    ``TimeoutError`` at the deadline, and ``RuntimeError`` when no thread can be
    started.
    """
    loop = asyncio.get_running_loop()
    outcome: asyncio.Future[BaseException | None] = loop.create_future()

    def _settle(exc: BaseException | None) -> None:
        if not outcome.done():
            outcome.set_result(exc)

    def _worker() -> None:
        result: BaseException | None = None
        try:
            signing_key_preflight()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the loop
            result = exc
        try:
            loop.call_soon_threadsafe(_settle, result)
        except RuntimeError:
            pass  # the loop already closed: the boot gave up on this thread

    threading.Thread(target=_worker, name="kc-signing-key-preflight", daemon=True).start()
    exc = await asyncio.wait_for(
        outcome, _SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS if timeout is None else timeout
    )
    if exc is not None:
        raise exc


def signing_key_preflight() -> None:
    """Refuse to boot on a truncated dashboard signing key.

    A ``token_signing.key`` shorter than 32 bytes is never accepted as a key. The
    loader's fallback for it is a random in-memory secret, so the gateway would come
    up, every dashboard browser would be asked for a fresh login token, every
    HMAC-certified tag grant would be quarantined, and the same would happen again
    on the next restart, with one WARNING line and nothing else saying why. The file
    is deliberately NOT repaired here: an in-place heal races an operator's restore,
    a sibling gateway and the sandbox, and each guard against one of those opens a
    seam for the next, while the operator holding the file is the one actor who can
    move it safely. So this fails loud, early and
    with the manual repair steps, the way the persistence preflight does.

    Absent (first boot), non-regular (``other``) and sandbox-masked keys pass
    through: the first is normal and the loader publishes a key; the others are
    reported by ``doctor`` and by the loader's own WARNING.

    Two looks, and the second is the one that counts. The ``lstat`` probe supplies
    the path and a first shape verdict, which decides only whether the load runs.
    It can be stale in either direction: an in-place rewrite
    that begins after it and stays short through the loader's retry budget would
    still reach the ephemeral fallback, and a short answer can be a sibling
    gateway's momentary empty file from the in-place creator on a link-less home,
    which the loader waits out and then reads. So every ok, absent, short or
    unstatable key is LOADED here, through the memoized getter the dashboard signs
    with. A load that fell back on a truncated file refuses with the manual repair
    steps. A load that fell back for any other reason (a key this process cannot
    read or stat) boots on the loader's WARNING and is reported by ``doctor``,
    unless the probe already saw a short key, which still refuses: the
    loader treats a Windows sharing violation as transient, and a lock held past
    its retry budget must not leave the gateway unable to start. The probe decides
    alone when an earlier caller already memoized the secret.
    Blocking filesystem I/O: call it off the event loop.
    """
    # The single confinement probe (sandbox.agent_confinement_evidence) reads the
    # kirocrew-agents.slice cgroup on Linux, so a masked key inside the sandbox is
    # not read as truncated here. Local import: cli.main() has popped the launcher
    # marker by now, and token_secret keeps kiro_crew.sandbox off its import chain,
    # so the caller supplies the verdict.
    #
    # Two bits, so a FOREIGN macOS Seatbelt sandbox (one Kiro Crew did not create,
    # which can leave the real key readable) does not skip the truncated-key
    # refusal: masked requires the open to be refused ONLY when the sole signal is
    # the Seatbelt verdict (agent_confinement_is_kirocrew_own() is false). Inside
    # Kiro Crew's own sandbox the key is masked regardless of readability — the
    # Linux mask falls back to a readable empty stand-in when its tmpfs mount fails.
    from kiro_crew.sandbox import (
        agent_confinement_evidence,
        agent_confinement_is_kirocrew_own,
    )

    confined = agent_confinement_evidence() is not None
    state, key_path = token_secret.signing_key_health(
        confined=confined,
        require_denied_open=confined and not agent_confinement_is_kirocrew_own(),
    )
    if state in ("ok", "absent", "short", "unstatable"):
        loaded = token_secret.load_boot_secret()
        if loaded in ("loaded", "short"):
            state = loaded
    if state != "short":
        return
    # The literal hint, not restart_command_hint(): that helper stats the
    # per-user unit file under HOME, which can block without bound on a
    # disconnected network mount, and the boot must refuse rather than hang.
    steps = token_secret.signing_key_remedy(key_path, BOOT_RESTART_HINT)
    logger.critical(
        "Signing key preflight failed — refusing to start: %s is shorter than 32 "
        "bytes, so every restart would sign with an ephemeral secret and log every "
        "dashboard session out. Repair it by hand: %s",
        key_path,
        " ".join(steps),
    )
    print(
        f"❌ Truncated token signing key at {key_path}. Repair it by hand:",
        *(f"   {step}" for step in steps),
        sep="\n",
        file=sys.stderr,
    )
    raise SystemExit(1)


def _start_kas_cli_floor(cfg: KiroCrewConfig) -> "asyncio.Task[None]":
    """Schedule :func:`apply_kas_cli_floor` off the boot path and return its task.

    The check pins kiro-cli and spawns ``acp --help``, so it runs in a thread and
    the gateway binds without waiting for it. Until it lands, a configured KAS
    stays selected, which is also what a check that cannot run leaves. Failure is
    logged and contained: the backend stays as configured.

    Resolved KAS roles arm :mod:`kiro_crew.agent_sdk.kas_floor_wait` using only
    in-memory fields. The task reads stored declarations off-loop and releases
    explicit choices before probing. Defaulted roles wait for the verdict
    (bounded) instead of starting KAS on a kiro-cli that rejects it. Boot itself
    never waits for either the declaration read or the probe.
    """
    global _KAS_CLI_FLOOR_TASK
    from kiro_crew.acp_backends import ACP_BACKEND_KAS
    from kiro_crew.agent_sdk import kas_floor_wait

    async def _run() -> None:
        try:
            main_default, member_default = await asyncio.to_thread(_kas_floor_gates_spawns, cfg)
            kas_floor_wait.release_kas_floor_wait(
                task, main=not main_default, member=not member_default
            )
            kas_defaulted = (
                (main or member) and (not main or main_default) and (not member or member_default)
            )
            await asyncio.to_thread(apply_kas_cli_floor, cfg, kas_defaulted=kas_defaulted)
        except Exception:
            logging.getLogger(__name__).warning(
                "KAS kiro-cli floor check failed; continuing", exc_info=True
            )

    agent = getattr(cfg, "agent", None)
    bundle = getattr(agent, "kas_bundle", False)
    main = not bundle and getattr(agent, "acp_backend", None) == ACP_BACKEND_KAS
    member = not bundle and getattr(agent, "member_acp_backend", None) == ACP_BACKEND_KAS
    task = asyncio.create_task(_run(), name="kas-cli-floor")
    _KAS_CLI_FLOOR_TASK = task
    kas_floor_wait.arm_kas_floor_wait(task, main=main, member=member)
    return _KAS_CLI_FLOOR_TASK


def _kas_floor_gates_spawns(cfg: KiroCrewConfig) -> tuple[bool, bool]:
    """Read off-loop whether each role must keep waiting for the probe.

    An explicit backend choice releases its role, and the bundle route, which
    the floor never probes, gates neither. A config that cannot be read releases
    both roles, leaving the spawn's configured backend unchanged.
    """
    from kiro_crew.acp_backends import ACP_BACKEND_KAS

    agent = getattr(cfg, "agent", None)
    if getattr(agent, "kas_bundle", False):
        return False, False
    kas_main = getattr(agent, "acp_backend", None) == ACP_BACKEND_KAS
    kas_member = getattr(agent, "member_acp_backend", None) == ACP_BACKEND_KAS
    if not (kas_main or kas_member):
        return False, False
    try:
        from kiro_crew.config.loader import acp_backend_declared, member_acp_backend_declared

        return (
            kas_main and not acp_backend_declared(),
            kas_member and not member_acp_backend_declared(),
        )
    except Exception:
        logging.getLogger(__name__).debug("backend declared-check failed", exc_info=True)
        return False, False


def _start_coder_retention(cfg: KiroCrewConfig) -> None:
    """Activate the optional Coder subsystem only after readiness/configuration."""
    global _CODER_RETENTION_TASK
    from kiro_crew.session_environment import session_environments_enabled

    if not session_environments_enabled() or not cfg.session_environments.coder_url:
        return
    if _CODER_RETENTION_TASK is not None and not _CODER_RETENTION_TASK.done():
        return

    async def run() -> None:
        from kiro_crew.coder.retention import run_sweeper

        await run_sweeper()

    _CODER_RETENTION_TASK = asyncio.create_task(run(), name="coder-retention")


async def _stop_session_environments() -> None:
    """Stop the remote session environments on shutdown; bounded, never raises."""
    sweeper = _CODER_RETENTION_TASK
    if sweeper is None:
        return
    if not sweeper.done():
        sweeper.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sweeper
    try:
        from kiro_crew.coder.retention import (
            SHUTDOWN_STOP_SECS,
            stop_for_shutdown,
            stop_on_shutdown_enabled,
        )

        if not await stop_on_shutdown_enabled():
            # Fast restarts chosen: each guest still stops on its own once no gateway
            # reaches it (the worker's orphan timeout).
            return
        await asyncio.wait_for(stop_for_shutdown(), timeout=SHUTDOWN_STOP_SECS)
    except asyncio.TimeoutError:
        logger.warning("Session environments did not all stop in time; they stop when idle")
    except Exception:  # noqa: BLE001 -- the gateway exits regardless
        logger.warning("Session environments shutdown stop failed", exc_info=True)


async def run_gateway(
    cfg: KiroCrewConfig,
    *,
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_tunnel: bool = False,
    no_open: bool = False,
    port_override: str | None = None,
    json_ready: bool = False,
    approval_mode: str | None = None,
    test_mode: bool = False,
) -> None:
    """Start the Slack Socket Mode gateway (blocks until shutdown).

    If Slack credentials are missing, starts in **dashboard-only** mode:
    all services (chat, cron, subagents, task runner) are available via
    the web dashboard, but Slack connectivity is disabled.
    """
    # ── Name the default executor ──
    # asyncio.to_thread and run_in_executor(None, ...) route onto the loop's
    # default executor, which Python names threads anonymously.  This names
    # them ``mc-default`` so profilers like py-spy can attribute blocking work
    # to this gateway.  Must run BEFORE any to_thread offload.
    configure_default_executor()

    # ── Publish surface, pinned for the process ──
    # Recorded BEFORE any service spins up, because both doors out are opened by
    # services started below: the dashboard's boot-time ``setup_tunnel`` and the
    # on-demand provisioning in ``slack.allowlist`` that a Slack message can reach
    # as soon as the gateway is listening. Set unconditionally so a False here
    # also CLEARS a value a previous gateway left behind in the same process
    # (the test harness boots more than one), rather than letting it leak.
    set_publish_disabled(no_tunnel)

    # ── Platform context boot (CPP seam) ──
    # Resolve + install the PlatformContext ONCE before any service spins up.
    # Idempotent: a no-op when ``cli.main`` already booted in this process.
    # Standalone composes the all-defaults context (identical to today); a
    # non-standalone profile that cannot compose its companion fails closed.
    try:
        boot_platform(cfg)
    except Exception as exc:
        # Enterprise build: failed governance rules exit with the fix named.
        from kiro_crew.enterprise_boot import refuse_policy_boot

        refuse_policy_boot(exc)
        raise

    # ── KAS kiro-cli floor ──
    # A configured KAS on a kiro-cli that cannot select the KAS engine falls back
    # to kiro-cli, with the notice in the log. Scheduled in the background, never
    # awaited here: it spawns ``acp --help``. Skipped in test_mode so the offline
    # E2E gate never spawns the developer's real kiro-cli.
    if not test_mode:
        _start_kas_cli_floor(cfg)

    # ── Aggregate cgroup ceiling for all agent scopes ──
    # The per-spawn scope wrapper (sandbox.cgroup_scope_argv) bounds ONE spawn
    # tree; this bounds ALL of them together by putting MemoryMax/TasksMax on
    # their shared parent slice. Scheduled as a contained background task —
    # never awaited on the boot path, so a slow user manager (the systemctl
    # call carries a 15s timeout) cannot delay dashboard binding. The module
    # global keeps a strong reference (the loop holds tasks weakly). Skipped
    # in test_mode: the offline E2E gate must not mutate the developer's real
    # user manager. Failure is non-fatal — the function logs and the
    # per-scope ceilings still apply.
    global _SLICE_LIMITS_TASK
    if not test_mode:

        async def _apply_slice_limits() -> None:
            try:
                await asyncio.to_thread(ensure_agents_slice_limits)
            except Exception:
                logging.getLogger(__name__).warning(
                    "aggregate cgroup ceiling apply failed", exc_info=True
                )

        _SLICE_LIMITS_TASK = asyncio.create_task(_apply_slice_limits(), name="agents-slice-limits")

    # ── Agents-dir janitor (fire-and-forget) ──
    # Sweep aged orphaned atomic-write temps + stale backups from the shared
    # kiro agents directory (see kiro_crew.agents_janitor). Scheduled as a
    # contained background task and never awaited, so a slow or failing sweep
    # cannot delay dashboard binding or crash boot: the coroutine offloads the
    # blocking filesystem work to a thread and swallows every error. The module
    # global keeps a strong reference (the loop holds tasks weakly). Skipped in
    # test_mode so the offline E2E gate never touches the developer's real
    # agents dir.
    global _AGENTS_JANITOR_TASK
    if not test_mode:

        async def _run_agents_janitor() -> None:
            try:

                def _sweep_in_thread() -> None:
                    # kiro_agents_dir() resolved INSIDE the worker thread, not
                    # in this coroutine body: the resolver walks env +
                    # Path.home() + .resolve(), which is blocking filesystem
                    # work — on an unavailable network home it can stall
                    # indefinitely, and this coroutine runs on the event loop
                    # during boot, between bind and serve.
                    sweep_agents_dir(
                        kiro_agents_dir(),
                        sweep_backups=cfg.agent.sweep_agents_backups,
                    )
                    # Derived read-only side-chat specs whose base agent is gone
                    # (the hourly wake below repeats it). No live-session names
                    # are passed: a side session starting meanwhile publishes
                    # its spec first, and the reap re-checks the base under the
                    # publication lock. It parses only the files named in a
                    # derived shape, and scans for a base only when no file of
                    # that base's name exists.
                    from kiro_crew.dashboard.side_readonly_spec import (
                        reap_orphaned_readonly_specs,
                    )

                    reap_orphaned_readonly_specs()
                    # The per-spawn skill-view prune is capped, so a backlog
                    # left by earlier builds would take hundreds of spawns to
                    # clear. Drain it once here, in lock-bounded batches.
                    from kiro_crew.agent_sdk.drivers import acp as acp_driver

                    acp_driver.drain_skill_view_aliases()

                await asyncio.to_thread(_sweep_in_thread)
            except Exception:
                logging.getLogger(__name__).debug(
                    "agents-dir janitor sweep failed at boot", exc_info=True
                )

        _AGENTS_JANITOR_TASK = asyncio.create_task(_run_agents_janitor(), name="agents-dir-janitor")

    # ── Agent scratch + work root sweep (fire-and-forget, hourly) ──
    # Reclaim per-process agent scratch dirs whose owner process is dead
    # (see kiro_crew.agent_scratch). Liveness-keyed, never age-keyed, so a
    # long-lived session's in-flight work is never deleted under it -- and
    # because agent processes can OUTLIVE a gateway restart, the sweep checks
    # each recorded owner pid instead of clearing wholesale at boot. Hourly
    # repeats catch processes that die while the gateway stays up (no
    # per-teardown hook: the positive liveness signal covers every death
    # path by construction). Same containment posture as the janitor above:
    # offloaded, fail-open, skipped in test_mode. The same wake also sweeps the
    # cross-process work root (see kiro_crew.work_root), which is idle-keyed
    # BECAUSE outliving its creator is that root's contract.
    global _AGENT_SCRATCH_SWEEP_TASK
    if not test_mode:

        async def _run_agent_scratch_sweep() -> None:
            while True:
                # Sleep FIRST: gateway boot must not pick up file-count-scaled
                # maintenance (no-new-work-on-gateway-boot-path); the first
                # sweep runs an hour in, and nothing here is boot-urgent --
                # scratch reclamation has no correctness deadline.
                await asyncio.sleep(3600)
                # A conductor spec the boot rebuild left unwritten (it could not be
                # read or written) rides this wake too, and FIRST: it is the one rider
                # with a security deadline. The only other retry is the policy
                # distribution poll's post-install hook, and a host with no central
                # distribution runs no poll -- so without this, that spec kept the
                # ``allowedTools`` the previous boot's ceiling wrote for the process
                # lifetime, past a local tightening. A no-op whenever the last rebuild
                # wrote every spec (``agent.retry_held_conductor_specs``), which is
                # every wake on a healthy host; its own try/except, like the sweeps.
                try:
                    from kiro_crew.agent import retry_held_conductor_specs

                    await asyncio.to_thread(retry_held_conductor_specs)
                except Exception:
                    logging.getLogger(__name__).debug(
                        "held conductor spec retry failed", exc_info=True
                    )
                try:
                    await asyncio.to_thread(agent_scratch.sweep_dead_scratch)
                except Exception:
                    logging.getLogger(__name__).debug("agent-scratch sweep failed", exc_info=True)
                # The cross-process work root rides the SAME hourly wake rather
                # than a scheduler of its own: it reclaims on an idle window
                # with no correctness deadline either, and a second timer would
                # double the wake cost for nothing. Its own try/except, so one
                # root's failure never skips the other's sweep.
                try:
                    await asyncio.to_thread(work_root.sweep_work_root)
                except Exception:
                    logging.getLogger(__name__).debug("work-root sweep failed", exc_info=True)
                # The per-run work directories of subagent and stateless cron
                # sessions ride the same wake. The ordinary end of a run reclaims
                # its own (AcpProvider.shutdown); the boot sweep took what a dead
                # predecessor of this data home left, and this re-runs the SAME
                # predecessor-only rule for whatever its bounds left over. A
                # directory this process marked is never its business; one a
                # registered provider names is skipped whatever it holds.
                # ``orchestrator`` is bound later in this function and read only
                # here, an hour in.
                try:
                    await asyncio.to_thread(
                        _sweep_predecessor_session_work_dirs,
                        _live_session_work_dirs(getattr(orchestrator, "sessions", None)),
                    )
                except Exception:
                    logging.getLogger(__name__).debug(
                        "session work-dir sweep failed", exc_info=True
                    )
                # Derived read-only side-chat specs whose base agent was removed
                # while the gateway stayed up. A spec a live session was spawned
                # under is skipped: its base may go, but its session stays bound
                # to that file until it ends.
                try:
                    from kiro_crew.dashboard.side_readonly_spec import (
                        reap_orphaned_readonly_specs,
                    )

                    await asyncio.to_thread(
                        reap_orphaned_readonly_specs,
                        _live_session_agents(getattr(orchestrator, "sessions", None)),
                    )
                except Exception:
                    logging.getLogger(__name__).debug("read-only spec reap failed", exc_info=True)

        _AGENT_SCRATCH_SWEEP_TASK = asyncio.create_task(
            _run_agent_scratch_sweep(), name="agent-scratch-sweep"
        )

    # ── kiro-cli log cap (fire-and-forget, every 5 minutes) ──
    # Each spawned kiro-cli logs into its own scratch dir (agent_scratch
    # .scratch_env pins KIRO_CHAT_LOG_FILE there) and never bounds that log
    # while running; at KIRO_LOG_LEVEL=debug one process writes ~40 MiB a
    # minute for as long as it lives. The hourly sweep above is too slow for
    # that rate, so this loop rotates any oversized log in place. Same
    # posture: offloaded, fail-open, sleep-first, skipped in test_mode.
    global _KIRO_CLI_LOG_CAP_TASK
    if not test_mode:

        async def _run_kiro_cli_log_cap() -> None:
            while True:
                await asyncio.sleep(agent_scratch.KIRO_CLI_LOG_CAP_INTERVAL_SECONDS)
                try:
                    await asyncio.to_thread(agent_scratch.cap_kiro_cli_logs)
                except Exception:
                    logging.getLogger(__name__).debug("kiro-cli log cap failed", exc_info=True)

        _KIRO_CLI_LOG_CAP_TASK = asyncio.create_task(
            _run_kiro_cli_log_cap(), name="kiro-cli-log-cap"
        )

    # ── Anonymous usage beacon (at most one HTTP GET per day) ──
    # Detached daemon thread, NOT awaited: ``beacon.send`` is blocking urllib
    # with a 5s timeout, and boot must never wait on the network (nor pin
    # interpreter exit — hence daemon=True, matching the model-download helper's
    # reasoning in embeddings_download.py). Errors are swallowed inside ``send``; a failed
    # heartbeat is invisible by design. ``test_mode`` skips it entirely so the
    # offline E2E gate can never make an outbound request.
    #
    # ``acked`` withholds the FIRST heartbeat until the user has actually been
    # shown the disclosure and its opt-out. Boot runs before the dashboard has
    # ever rendered, so on a fresh install this thread would otherwise ping
    # before the user could possibly decline, making the opt-out an offer
    # arriving after the fact. Established installs are unaffected: the gate
    # applies only while `is_first_send()` holds.
    if not test_mode:
        with contextlib.suppress(Exception):
            threading.Thread(
                target=beacon.send,
                args=(cfg.telemetry.beacon_endpoint, kiro_crew.__version__),
                kwargs={
                    "enabled": cfg.telemetry.beacon_enabled,
                    "acked": cfg.dashboard.privacy_acked,
                },
                name="kirocrew-beacon",
                daemon=True,
            ).start()

    # Off-loop: resolving a secret:// channel reference reads and decrypts the vault.
    credentials = await asyncio.to_thread(cfg.load_credentials)
    orchestrator = GatewayOrchestrator(
        cfg,
        no_dashboard=no_dashboard,
        no_crons=no_crons,
        no_open=no_open,
        port_override=port_override,
        json_ready=json_ready,
        approval_mode=approval_mode,
        test_mode=test_mode,
        credentials=credentials,
    )
    try:
        await orchestrator.run()
    finally:
        # Includes failures before READY, when the restore worker was never
        # scheduled. Release only this gateway's latch after closing its handle.
        await asyncio.to_thread(orchestrator._stop_memory_startup)


# Restart step named by the boot refusal. Deliberately static: probing for the
# installed service would stat HOME, which may be a hung network mount.
BOOT_RESTART_HINT = "kirocrew restart"


# Boot deadline for the signing-key preflight. Its lstat and key load normally
# take milliseconds (the loader's own retry budget is well under a second), so a
# preflight still running at this point is stuck on the filesystem.
_SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS = 30.0
