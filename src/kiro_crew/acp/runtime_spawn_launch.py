"""Launching the runtime's child process and its handshake.

The harness spawn plan and the spawn agent's projected spec, the sandboxed launch tail
and its collaborators (``_launch_tools``, the retrying spawn factory, the private-state
seed), the ``initialize`` handshake with the client capabilities it declares, and the
failed-start guard that reaps what a broken start left behind.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_agent``, ``_agent_capabilities``, ``_agent_version``,
``_can_load_session``, ``_child_pids``, ``_derived_spec_snapshot``, ``_exit_watch_task``,
``_initialized``, ``_kas_host_auth``, ``_last_activity``, ``_launch_copy_spec``,
``_launch_cwd_roots``, ``_launch_process_cwd``, ``_max_age_secs``,
``_max_rss_depth``, ``_max_rss_mb``, ``_native_launch_resources``,
``_native_launch_sources``, ``_native_launch_view_alias``, ``_native_skill_projection``,
``_pid``, ``_process``, ``_process_instance``, ``_process_tree_confirmed_dead``,
``_prompt_capabilities``, ``_reader_task``, ``_sandbox_cleanup``, ``_scope_unit``,
``_skill_projection_generation``, ``_spawn_monotonic``, ``_spawn_persona_snapshot``,
``_spawn_skill_projection``, ``_spawn_start_token``, ``_start_time``, ``_stderr_task``,
``_tool_search_wire``.

``_with_mcp_sign_in_storage`` (a plain function handed the runtime) also writes
``_mcp_secret_server``.

Instance fields read: ``_acp_backend``, ``_extra_env``, ``_launch_agent``, ``_member_context``,
``_model``, ``_sandbox_mode``, ``_scratch_dir``, ``_shared_scratch``, ``_tool_search``,
``_work_dir``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from kiro_crew import agent_scratch, member_essential_context, platform_compat
from kiro_crew.acp import kas_secret_storage
from kiro_crew.acp._dispatch import agent_version_from_init
from kiro_crew.acp.agent_env import agent_env_overlay
from kiro_crew.acp.client import (
    CLIENT_NAME,
    CLIENT_VERSION,
    KIRO_CLI_BIN,
    _apply_pod_home_remap,
    _KiroExecutableTrustError,
    apply_pod_bundle_spawn,
    finish_suspended_spawn,
)
from kiro_crew.acp.harness import SpawnContext, SpawnPlan
from kiro_crew.acp.launch import LaunchRequest, LaunchTools, launch
from kiro_crew.acp.runtime_process_tree import ChildRecord, _kill_escaped_children
from kiro_crew.acp.runtime_tree_kill import _prune_dead_descendants
from kiro_crew.acp.session_handle import AcpRequestTimeout, AcpRuntimeError, AcpRuntimeOverloaded
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.agent import ensure_agent_materialized
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.agent_sdk.tool_search import (
    kas_client_meta_settings,
    spec_grants_tool_search,
    with_client_meta_settings,
)
from kiro_crew.browser_cli.launch import browser_session_env, browser_socket_env
from kiro_crew.config.paths import data_home, kiro_agents_dir
from kiro_crew.constants import (
    INITIALIZE_TIMEOUT_SECS,
    KIROCREW_RUNTIME_AGENT_ENV,
    KIROCREW_SPAWN_HOME_ENV,
    KIROCREW_SPAWN_INSTANCE_ENV,
)
from kiro_crew.env import augmented_path, resolve_krb5_ccname
from kiro_crew.executors import subprocess_executor
from kiro_crew.member_essential_context import MemberEssentialContextError
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.resource_status import inject_xdist_auto_cap
from kiro_crew.sandbox import (
    _forward_ssh_auth_sock,
    agents_slice_throttling,
    assert_voice_runtime_outside_agent_workspace,
    bind_voice_safe_agent_workspace_async,
    cgroup_scope_argv,
    create_subprocess_limited,
    name_scope_unit,
    scope_unit_name,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
    wrapped_by_crew_sandbox,
)
from kiro_crew.session_pid import (
    _pid_start_token,
    _track_pid,
    _track_session_pid,
    register_protected_pid,
)

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


_T = TypeVar("_T")


# ``initialize`` budget: a cold start, not a control-plane round trip. kiro-cli
# answers ``initialize`` only after it has started up, so a slow disk, a large
# skill tree or a throttled agents slice can push the first answer past the plain
# request budget (``_REQUEST_TIMEOUT``) while the process is alive and making progress. Killing it there
# discards that startup, and every retry pays the same startup again. Three times
# the plain budget, not unbounded: the cold-start semaphore (``runtime_start``) is
# held for the whole wait. The value MUST stay strictly below the subagent startup watchdog
# (``subagent._STARTUP_TIMEOUT_SECS``, 120s from ``_exec_started``): a
# subagent's ``info._pid`` is recorded only after ``provider.start()`` returns,
# i.e. after this handshake, so the watchdog sees "no runtime yet" for the
# whole wait and force-reaps the live process the moment its deadline passes.
# The budget therefore has to expire first, with room for the spawn that
# precedes the handshake, so ``AcpRuntimeOverloaded`` is what the caller sees
# rather than a reaper kill. ``test_agents_slice_admission`` pins the ordering.
_INITIALIZE_TIMEOUT = INITIALIZE_TIMEOUT_SECS


# The Kiro CLI replaces its own executable in place during an update. A spawn
# that lands in that short window can fail with OSError and succeeds after this delay.
_ACP_RUNTIME_RESPAWN_BACKOFF_S = 2.0


def _point_private_state_at_scratch(
    env: dict[str, str], env_var: str | None, scratch: Path | None
) -> None:
    """Point the host's private-state variable at this process's scratch dir.

    *env_var* is :attr:`SpawnPlan.private_state_env`: ``None`` for a host whose
    state tolerates concurrent processes, so nothing is set. A non-empty value
    already in *env* is the caller's (the operator's environment, or a cron or
    workflow ``extra_env``): they chose that location, and it reaches the child
    as set. Otherwise the variable names the per-process scratch directory: no
    other process uses it, and it is reclaimed once this one is dead
    (``agent_scratch.sweep_dead_scratch``). Without scratch the child gets the
    host's shared default and a warning says so: a shared database home is
    better than a refused spawn.
    """
    if env_var is None or env.get(env_var):
        return
    if scratch is None:
        logger.warning(
            "AcpRuntime: no per-process scratch to point %s at; the child shares the "
            "host's default, so concurrent processes may lock each other out",
            env_var,
        )
        return
    env[env_var] = str(scratch)


def _seed_private_state(env: dict[str, str], plan: SpawnPlan, scratch: Path | None) -> None:
    """Let the host prepare its private-state directory before the child starts.

    Runs only when the plan names both a private-state variable and a seed, the
    process has scratch, and nobody already chose a location for the variable:
    an operator's value means the child uses that directory, not scratch.
    """
    if (
        plan.private_state_seed is None
        or plan.private_state_env is None
        or scratch is None
        or env.get(plan.private_state_env)
    ):
        return
    plan.private_state_seed(env, scratch)


async def _retrying_spawn_factory(
    factory: "Callable[..., Awaitable[asyncio.subprocess.Process]]", **kwargs: Any
) -> asyncio.subprocess.Process:
    """Drive a subprocess factory, retrying ONE creation failure after a backoff.

    Shaped as the factory
    :func:`kiro_crew.platform_compat.create_windows_cleanup_owned_process`
    drives: on Windows that call passes ``windows_cleanup_owner`` down to
    whatever it invokes, so the keyword has to survive the hop to the bound
    :func:`create_subprocess_limited`. The Kiro CLI replaces its own executable
    in place during an update; a spawn that lands in that short window fails
    with ``OSError`` and succeeds after ``_ACP_RUNTIME_RESPAWN_BACKOFF_S``.
    Retried before this runtime records process state, so a failed attempt is
    indistinguishable from never having tried. Never a loop: the exit condition
    is the CLI finishing its own replacement.
    """
    for attempt in range(2):
        try:
            return await factory(**kwargs)
        except OSError as exc:
            if attempt:
                raise
            logger.warning(
                "ACP runtime subprocess creation failed (%s), retrying after "
                "adapter replacement window...",
                exc,
            )
            await asyncio.sleep(_ACP_RUNTIME_RESPAWN_BACKOFF_S)
    raise AssertionError("unreachable: the second attempt returns or re-raises")


def _launch_tools() -> LaunchTools:
    """The launch tail's collaborators, as THIS module binds them right now.

    Read at each launch, so a test that rebinds one of these names on
    ``kiro_crew.acp.runtime`` reaches the launch this runtime starts. The runtime
    alone retries a failed process creation once, across an adapter replacement.
    """
    return LaunchTools(
        logger=logger,
        platform_compat=platform_compat,
        agent_scratch=agent_scratch,
        apply_pod_bundle_spawn=apply_pod_bundle_spawn,
        forward_ssh_auth_sock=_forward_ssh_auth_sock,
        wrap_argv_async=wrap_argv_async,
        wrap_argv=wrap_argv,
        wrapped_by_crew_sandbox=wrapped_by_crew_sandbox,
        cgroup_scope_argv=cgroup_scope_argv,
        augmented_path=augmented_path,
        scrub_agent_subprocess_env=scrub_agent_subprocess_env,
        apply_pod_home_remap=_apply_pod_home_remap,
        browser_session_env=browser_session_env,
        browser_socket_env=browser_socket_env,
        inject_xdist_auto_cap=inject_xdist_auto_cap,
        bind_voice_safe_agent_workspace_async=bind_voice_safe_agent_workspace_async,
        create_subprocess_limited=create_subprocess_limited,
        retrying_spawn_factory=_retrying_spawn_factory,
        agent_env_overlay=agent_env_overlay,
    )


def _discard_sandbox_cleanup(self: AcpRuntime) -> None:
    """Unlink and forget the sandbox temp file allocated by ``wrap_argv``.

    Mirrors ``AcpClient._discard_sandbox_cleanup``: once no child will
    exec the launcher/profile file — spawn failed, was cancelled, or the
    runtime is shutting down — it must be removed, or each attempt leaks
    one file into the temp dir for the gateway's lifetime.
    """
    if self._sandbox_cleanup:
        try:
            os.remove(self._sandbox_cleanup)
        except OSError:
            pass
        self._sandbox_cleanup = None


async def _to_thread_guarding_sandbox(
    self: AcpRuntime, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> _T:
    """``asyncio.to_thread`` that discards the sandbox file on failure.

    After ``wrap_argv`` has allocated the sandbox temp file, every
    suspension point before the exec is a leak window: a cancellation
    unwinds ``spawn`` without reaching the shutdown cleanup, orphaning the
    file. Route any offload in that window through here so the file is
    removed before re-raising.
    """
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except BaseException:
        self._discard_sandbox_cleanup()
        raise


async def _resolve_spawn_plan(self: AcpRuntime) -> SpawnPlan:
    """Pre-sandbox argv for this runtime's backend, built by its harness.

    Every flag and every HOST-SPECIFIC pre-spawn gate belongs to the host, and
    the two hosts shipped today share none of them: one takes its agent and
    model on the command line and needs its spec on disk first, the other
    takes both over the wire and decides per spawn who owns the credential. A
    host added later answers all of that in its own file, which is the whole
    reason the argv is not assembled here. The derived-spec freshness gate is
    the one exception, and the comment on it below says why: it is the same
    check for every host, and it opens a bracket the runtime's own handshake
    closes.

    ONE reading of the environment drives both the search and the message
    that reports it, so a "not found (searched ...)" line can never name
    directories the resolve did not walk -- the property AcpClient._spawn
    already holds. It is snapshotted here and handed over, so the harness
    cannot take a second, different reading.

    ``host_auth`` is remembered on the instance because the reader loop uses
    it: the engine's credential callback is answered only on a process that
    was started expecting Crew to own its identity, and a dashboard sign-out
    between spawns must not change that answer for a process already running.

    The whole plan is returned, not just the argv: the credential mask has to
    reach the sandbox call, and re-deriving it there would either pay a second
    thread hop for the same filesystem work or -- worse -- silently resolve a
    different mask than the one the argv was built for.
    """
    plan = await self._harness.resolve_spawn(
        SpawnContext(
            agent=self._agent,
            work_dir=self._work_dir,
            model=self._model,
            environ=dict(os.environ),
            home=Path.home(),
            sandbox_mode=self._sandbox_mode,
            member_context=self._member_context,
        )
    )
    # The ONE derived-spec gate on this path, and the one host-level gate that is
    # the runtime's rather than the harness's: it is the same check for every host,
    # and it returns the snapshot the POST-handshake check compares against, so both
    # ends of that bracket must belong to the object that drives the handshake.
    #
    # AFTER ``resolve_spawn`` on purpose. It is the LAST verification before the
    # process is created, so nothing between it and the exec can re-derive: a gate
    # that ran before the host's own pre-spawn work would let a re-derive land in
    # between, and the child would then load the NEWER spec while the
    # post-handshake check compared against the older snapshot and killed a valid
    # session. It also puts the host's materialization self-heal FIRST, so a missing
    # default spec is repaired on the path that can repair it instead of refused.
    #
    # Not inside the harness either, and not once per harness: that shape leaves one
    # hole per host nobody named, and the two shipped hosts already disagreed about
    # it -- only one of them gated.
    #
    # Converted to the runtime's abort type, like every other refusal on this path.
    # One extra stat (and at most one hash) on a path that is already spawning a
    # process.
    from kiro_crew.agent import DerivedSpecStale, require_fresh_derived_spec

    try:
        self._derived_spec_snapshot = await asyncio.to_thread(
            require_fresh_derived_spec, self._agent, self._work_dir
        )
    except DerivedSpecStale as exc:
        raise AcpRuntimeError(str(exc)) from exc
    self._kas_host_auth = plan.host_auth
    self._native_launch_sources = dict(plan.native_context_documents)
    self._native_launch_view_alias = None
    self._native_launch_resources = None
    return plan


async def _initialize_handshake(
    self: AcpRuntime, client_capabilities: dict[str, Any]
) -> dict[str, Any]:
    """Send ``initialize`` with the cold-start budget.

    ``client_capabilities`` is the declaration :meth:`spawn` resolved before
    the process existed (the harness constant, or the wire-filled variant a
    ``client_meta_settings`` host gets); this helper only owns the budget.

    The budget is :data:`_INITIALIZE_TIMEOUT` whatever the host's state:
    ``initialize`` is answered only once kiro-cli has started, so a slow
    start is a live process answering late, not a hung one. A timeout that
    lands while the process is still ALIVE and the agents slice is
    throttling is re-raised as :class:`AcpRuntimeOverloaded`, so the failure
    names overload instead of a killed process. Any other timeout, and an
    exited process, propagate unchanged.
    """
    throttled_at_spawn = agents_slice_throttling()
    timeout = _INITIALIZE_TIMEOUT
    if throttled_at_spawn:
        logger.warning(
            "acp_startup_stage stage=initialize outcome=throttled_at_spawn "
            "timeout_budget_s=%g pid=%s: the agents slice is being throttled, "
            "so this cold start may be slow",
            timeout,
            self._pid,
        )
    try:
        return await self._send_and_await(
            "initialize",
            {
                # kiro-cli reads the driving client name from `clientInfo.name`
                # (agent/acp/acp_agent.rs: `if let Some(info) = request.client_info`),
                # NOT from a flat `clientName` key. Sending it flat left every
                # AcpRuntime-driven session (the primary kiro-cli path) unnamed in
                # telemetry — bucketed as "(none)" instead of "kirocrew". Nest it to
                # match AcpClient and be picked up for acpClientName attribution.
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
                # Both fields are per-host FACTS, not negotiations: hosts
                # disagree on the protocol revision's TYPE as well as its
                # value (a date string here, an integer there) and a wrong
                # shape is rejected outright. Read from the harness so the
                # pair can never be collapsed into one handshake every host
                # accepts, which would silently downgrade what a kiro session
                # declares.
                "protocolVersion": self._harness.protocol_version,
                "clientCapabilities": client_capabilities,
            },
            timeout=timeout,
        )
    except AcpRequestTimeout as exc:
        alive = self._process is not None and self._process.returncode is None
        if not alive or not (throttled_at_spawn or agents_slice_throttling()):
            raise
        raise AcpRuntimeOverloaded(
            f"initialize went unanswered for {timeout:g}s while the agents slice is "
            "being throttled at its memory ceiling; the process was alive, not hung. "
            "Free agent memory (close idle sessions, run fewer concurrent subagents) "
            "and retry."
        ) from exc


async def _spawn_admitted(self: AcpRuntime) -> None:
    """Spawn and initialize after the caller has acquired cold-start admission."""
    if self._process is not None:
        raise AcpRuntimeError("Runtime already spawned")
    # A stored skill-view name is never the agent to spawn: map it to the
    # agent it was built from before anything reads ``self._agent``.
    self._agent = await self._source_agent(self._agent) or self._agent

    # Let this host narrow the configured recycle thresholds before the
    # process it governs exists. The operator's values go IN, so a host that
    # does not narrow changes nothing, and a caller that set a threshold
    # directly keeps it as the input.
    reclaim = self._harness.reclaim_policy(
        max_age_secs=self._max_age_secs, max_rss_mb=self._max_rss_mb
    )
    self._max_age_secs = reclaim.max_age_secs
    self._max_rss_mb = reclaim.max_rss_mb

    # Off-loop: mkdir is a blocking syscall and the parent dirs may live on
    # slow storage; the loop must never wait on the kernel here.
    await asyncio.to_thread(ensure_directory, self._work_dir)  # 0700 in the data home
    if self._launch_agent:
        # kiro-cli takes project agents from its PROCESS directory, so a launch copy's
        # process starts where no checkout spec can sit; the session's cwd stays the
        # checkout (``apps.agent_launch.launch_process_cwd``).
        from kiro_crew.apps.agent_launch import (
            AppAgentLaunchError,
            launch_cwd_roots,
            launch_process_cwd,
            read_launch_copy,
        )

        try:
            self._launch_process_cwd = str(await asyncio.to_thread(launch_process_cwd))
            self._launch_cwd_roots = await asyncio.to_thread(
                launch_cwd_roots, self._launch_process_cwd
            )
            self._launch_copy_spec = await asyncio.to_thread(
                read_launch_copy, self._launch_agent, kiro_agents_dir()
            )
        except AppAgentLaunchError as exc:
            raise AcpRuntimeError(exc.detail) from exc
    # Delegated Kiro agents on macOS do not inherit Kiro Crew's Seatbelt
    # deny rules. Keep their workspace disjoint from the named voice-decoder
    # runtime so verified executable bytes cannot be replaced before spawn.
    if self._harness.internal_sandbox:
        await asyncio.to_thread(assert_voice_runtime_outside_agent_workspace, self._work_dir)

    try:
        plan = await self._resolve_spawn_plan()
        self._max_rss_depth = plan.rss_depth
        argv = plan.argv
        if self._launch_agent and "--agent" in argv:
            # The harness built ``--agent <identity>`` and ran its spawn gates
            # on the identity; kiro-cli is handed the launch copy instead.
            argv = list(argv)
            argv[argv.index("--agent") + 1] = self._launch_agent
        # What ``--agent`` names: the launch copy when this runtime runs one, else
        # the authored name, which a view prepared below replaces with its alias.
        spawn_transport = self._launch_agent or self._agent
        if self.acp_backend == ACP_BACKEND_KIRO:
            from kiro_crew.acp.skill_projection import prepare_native_skill_projection

            async with self._skill_projection_lock():
                generation = self._issue_skill_projection_generation()
                projection = await asyncio.to_thread(
                    prepare_native_skill_projection, self._work_dir
                )
                # Issued and adopted under the lock, so nothing newer exists.
                self._native_skill_projection = projection
                self._skill_projection_generation = generation
            # Held for the process's life: the aliases kiro-cli listed at
            # startup are ones it is guaranteed to have loaded, so later
            # projections keep translating them in inbound frames
            # (_activate_mode_bracketed), and holding the object keeps its
            # lease -- and them -- out of the prune.
            self._spawn_skill_projection = projection
            if projection is not None:
                # Deliberately do NOT set spawn_agent_name here. The shared
                # runtime activates the launched agent through
                # _activate_mode_bracketed, which allows self._agent at every
                # session start explicitly (keyed on self._agent, translate
                # bypassing request()). Setting spawn_agent_name would ALSO make
                # request() -- the general outbound path a mid-session set_mode
                # takes -- tolerate the launch agent indefinitely, so a switch
                # back to it after its view vanished would reactivate a cached
                # unprojected spec the strict resolver exists to refuse. The
                # field stays empty on the shared runtime; only the direct
                # client (one session, no mid-session re-entry) sets it. The
                # launch mode must still be ADVERTISED so the session open finds
                # an activatable mode (frame reads advertised_launch_name, which
                # the shared runtime DOES set -- advertising is safe for both
                # runtimes; only request()'s mid-session tolerance is withheld).
                projection.advertised_launch_name = self._launch_agent or self._agent
                argv = list(argv)
                agent_position = argv.index("--agent") + 1
                try:
                    # A launch copy spawns under its own name: a view of the
                    # agent is built by name, and the name can reach the
                    # checkout's same-named spec.
                    launch_view_alias = self._launch_agent or projection.spawn_agent(self._agent)
                except ValueError as exc:
                    # The projection refused this agent's view -- a
                    # ``kirocrew-core`` restriction authored in its spec, a
                    # disabled ``skill_search`` -- and says so in a sentence
                    # that names the spec and the remedy. That sentence is
                    # the user's, so it travels as the error the startup
                    # paths translate (``providers/acp_runtime_start.py`` handles
                    # ``AcpRuntimeError`` around ``spawn()``); a bare
                    # ``ValueError`` would leave ``spawn()`` as an internal
                    # failure with the actionable text unread. ``spawn_agent``
                    # raises for that authored refusal (a spec that projected
                    # but excludes the search tool its view needs); an agent
                    # with no prepared view keeps its authored name and never
                    # lands here.
                    raise AcpRuntimeError(str(exc)) from exc
                argv[agent_position] = launch_view_alias
                if self._launch_agent:
                    # The copy is already this agent's skill view, published by
                    # ``apps.agent_launch``; read by its own path and held like
                    # any prepared view, so the session's control plane and the
                    # launch steering record below are judged from it.
                    await asyncio.to_thread(_adopt_launch_copy_view, projection, self._launch_agent)
                # The name kiro-cli is given: the launch copy when this runtime
                # runs one, else its skill-view alias when a view was prepared,
                # else the authored name. The persona read below reads THAT
                # spec, because it is the copy the process loads; a launch copy
                # is not alias-shaped, so that read records nothing for it.
                spawn_transport = argv[agent_position]
                if not self._member_context and not self._native_launch_sources:
                    if not await asyncio.to_thread(
                        acp_driver.inherits_default_resources, self._work_dir
                    ):
                        projected_view = projection.specs.get(self._launch_agent or self._agent)
                        if projected_view is not None:
                            # kiro-cli re-reads the view's declared guides for
                            # every request, so the record holds their paths
                            # only; the folder skips those paths and an edited
                            # guide reaches the model through kiro-cli. A
                            # record that cannot be taken degrades to an
                            # empty one: the folder then carries every guide
                            # itself, and the session still starts. The empty
                            # record lasts for the process's life, so the
                            # reason is logged once here. The path walk
                            # translates only OSError/ValueError into its own
                            # error; an undeterminable home or a pre-3.13
                            # symlink loop surfaces as RuntimeError, and a
                            # literal declaration under a directory that
                            # denies search as a bare PermissionError from
                            # ``Path.is_file()``.
                            try:
                                launch_paths = await asyncio.to_thread(
                                    member_essential_context.projected_launch_paths,
                                    projected_view,
                                    str(self._work_dir),
                                )
                            except (
                                MemberEssentialContextError,
                                OSError,
                                RuntimeError,
                                RecursionError,
                            ) as exc:
                                logger.warning(
                                    "launch steering record for agent %r skipped "
                                    "(%r); the folder sends every guide itself, "
                                    "so a guide kiro-cli loaded can arrive twice",
                                    self._agent,
                                    exc,
                                )
                                self._native_launch_sources = {}
                                self._native_launch_view_alias = None
                                self._native_launch_resources = None
                            else:
                                if launch_paths:
                                    # Paths only: no consumer of this record
                                    # reads a body (build_message reads native
                                    # bodies on the member path alone).
                                    self._native_launch_sources = dict.fromkeys(launch_paths, "")
                                    self._native_launch_view_alias = launch_view_alias
                                    self._native_launch_resources = list(
                                        projected_view.get("resources", [])
                                    )
    except _KiroExecutableTrustError as exc:
        raise AcpRuntimeError(str(exc)) from exc
    # The handshake declaration is the harness's constant. A host that takes
    # feature settings over the wire (``client_meta_settings``, a positive
    # membership answer) has its channel filled here -- BEFORE the process
    # exists, right after the freshness gate above took its snapshot, so the
    # spec read is the generation the child will load. Every other host reads
    # its constant directly: no new await, no new step on that path.
    client_capabilities = self._harness.client_capabilities
    if self._harness.client_meta_settings:
        client_capabilities = await self._handshake_client_capabilities()

    # The launch tail both drivers share (``acp.launch.launch``): the pod bundle
    # swap and the delegation verdict, this runtime's scratch window, the sandbox
    # wrap with the host's credential mask, the cgroup scope, the environment and
    # the suspended spawn. What differs on this driver is plugged in below.
    #
    # is_kiro_cli drives the reviewed Kiro internal-sandbox delegation: on macOS
    # wrap_argv skips its seatbelt because the two cannot nest; on Windows the
    # official Kiro backend delegates by default because Crew has no native OS
    # sandbox there. The verdict is membership in ACP_BACKENDS_INTERNAL_SANDBOX
    # (harness-parity H7) — never "not KAS": that test fails OPEN, so a harness
    # inheriting a negative test would have Crew's seatbelt skipped in favour of
    # an internal sandbox that never starts. KAS is a Node process with no
    # internal sandbox, so it takes Crew's seatbelt directly, and so does every
    # harness added later.
    #
    # The incarnation this spawn is. Minted BEFORE the launch because two things
    # carry it: the scope's unit name, and the child's own environment. One token
    # for both is what lets a reader holding either one reach the other -- a scope
    # resolves to the runtime incarnation inside it, and a process resolves to the
    # scope bounding it. Random rather than pid-derived so a recycled pid cannot
    # false-match.
    spawn_instance = uuid.uuid4().hex[:16]

    def _name_scope(argv: list[str]) -> tuple[list[str], str]:
        # Name the cgroup scope after this spawn, because the ceiling binds the
        # whole runtime: when the kernel OOM-kills the scope, every session the
        # runtime serves dies together, and an anonymous ``run-u<N>.scope`` in
        # that report names no runtime to resolve those sessions from. A no-op
        # where the wrap did not happen (no cgroup delegation), so it cannot turn
        # a degraded host into a failed spawn, and pure argv rewriting, so it
        # stays on the loop rather than costing a second thread hop.
        named_argv = name_scope_unit(argv, spawn_instance)
        # Whether the naming ACTUALLY happened is read off the argv, not
        # re-derived from the token. name_scope_unit returns argv unchanged on
        # every host where cgroup_scope_argv handed back the bare command (macOS,
        # Windows, Linux without cgroup-v2 --user delegation, systemd-run outside
        # a trusted dir), and the token is always spellable, so a token-derived
        # name would claim a scope for exactly those hosts that have none.
        unit = (scope_unit_name(spawn_instance) or "") if named_argv is not argv else ""
        return named_argv, unit

    async def _env_before_scrub(env: dict[str, str], spawned_binary: str | None) -> dict[str, str]:
        def _resolve_env_off_loop() -> None:
            # KRB5CCNAME resolution lstat/stats /tmp/krb5cc_<uid>, and the
            # CLI's own KIRO_API_KEY is settled here too: re-injected from the
            # data home's .env for the kiro-cli backend (post-scrub Docker),
            # actively stripped for a foreign backend, which must never
            # receive it (see config.loader.inject/strip_kiro_cli_api_key) —
            # a file read either way. Both are blocking syscalls that must not
            # run on the loop, bundled into ONE thread hop. Guarded: the
            # sandbox temp file is live, so a cancellation here must not
            # orphan it.
            resolve_krb5_ccname(env)
            # KIRO_API_KEY is one host's own MODEL credential and another
            # host's active hazard, so which way it goes is the harness's answer.
            # kiro-cli is handed it for its v2 agent loop. The KAS relay is a
            # kiro-cli too, so the answer follows the spawn plan's auth owner:
            # a cli-owned relay (--auth-method cli) authenticates itself and an
            # API key is one of its sign-ins, so it is handed the key; a
            # Crew-owned relay answers _kiro/auth/getAccessToken from Crew's
            # vault and has the key REMOVED -- the engine gives an API key in
            # its environment precedence over the callback, so leaving it set
            # would silently override the credential the operator signed in
            # with.
            # Called before the launch tail's scrub, so a host can both add its
            # own variables and remove one the generic path would pass through.
            self._harness.apply_spawn_env(
                env, spawned_binary=spawned_binary, cli_owned_auth=not plan.host_auth
            )
            # A host that keeps private state may prepare it in scratch before
            # the child exists; one more blocking file copy, so it rides here.
            _seed_private_state(env, plan, self._scratch_dir)

        await self._to_thread_guarding_sandbox(_resolve_env_off_loop)
        return env

    def _env_after_marker(env: dict[str, str]) -> None:
        # The incarnation this spawn is (minted above, which names the scope
        # after the same token). It has to travel in the child's environment:
        # it is what a teardown reads back out of /proc/<pid>/environ to prove a
        # process is THIS spawn's descendant once the root itself is gone.
        env[KIROCREW_SPAWN_INSTANCE_ENV] = spawn_instance
        # Which install spawned it: the leaked-runtime reclaim refuses a runtime
        # whose home is absent or differs, since the orphan-sweep marker is
        # shared by every install on this uid.
        env[KIROCREW_SPAWN_HOME_ENV] = str(data_home())
        # Which authored agent it is spawned for -- the same name the spawn
        # log line carries. A host that binds its agent over the wire (the KAS
        # relay) shows no ``--agent`` in argv, so the environment is the one
        # place a reader of /proc can learn which agent a live runtime runs;
        # the process-leak gate reads it to tell the gateway's own background
        # runtime from a session's. ``_agent`` is the authored name by this
        # point (a stored skill-view alias is mapped back at spawn), so the
        # value never needs the projection lookup to resolve.
        if self._agent:
            env[KIROCREW_RUNTIME_AGENT_ENV] = self._agent
        else:
            env.pop(KIROCREW_RUNTIME_AGENT_ENV, None)

    def _env_after_scratch(env: dict[str, str]) -> None:
        # A host whose state cannot be shared between processes (codex's SQLite
        # databases) keeps its own copy under this process's scratch directory.
        _point_private_state_at_scratch(env, plan.private_state_env, self._scratch_dir)

    # The persona kiro-cli is about to load, read from the spec named at
    # ``--agent`` (its skill-view alias when one was prepared) immediately
    # before the launch. One read, before: an edit landing between it and
    # the process's own read leaves the file different from this text, so
    # the block is sent rather than withheld; an inline alias prompt cannot
    # change under its name at all. ``None`` (no agent, no native prompt,
    # unreadable copy) records nothing, which keeps the block.
    self._spawn_persona_snapshot = await self._to_thread_guarding_sandbox(
        self._spawn_persona_text, spawn_transport
    )
    launched = await launch(
        self,
        LaunchRequest(
            argv=argv,
            backend=self._acp_backend,
            sandbox_mode=self._sandbox_mode,
            extra_env=self._extra_env,
            scratch_label="runtime",
            env_before_scrub=_env_before_scrub,
            env_after_marker=_env_after_marker,
            env_after_scratch=_env_after_scratch,
            scope_argv=_name_scope,
            # The host's credential mask, resolved with its argv. Empty for a host
            # whose privileged tools ask by construction; for one this core's tool
            # gate ENFORCES it is the compensating control, so a spawn that
            # dropped it would hand a third-party binary the operator's
            # credential homes.
            extra_hidden_dirs=plan.extra_hidden_dirs,
            extra_expose_files=plan.extra_expose_files,
            internal_sandbox=self._harness.internal_sandbox,
            pod_home_remap=self._harness.pod_home_remap,
            guard_scratch_hops=True,
        ),
        _launch_tools(),
    )
    self._process = launched.process
    self._pid = self._process.pid
    # The same token the child carries in its environment (minted above, so
    # it could be passed in); random, not pid-derived, so it cannot
    # false-match a later spawn that the OS handed a recycled pid.
    self._process_instance = spawn_instance
    # Recorded from the launch's own account of the scope naming above, so the
    # init log names a scope only when one exists.
    self._scope_unit = launched.scope_unit
    # The subprocess is LIVE from here on but nothing has recorded it yet, so
    # this window needs the same guard AcpClient._spawn has. finish_suspended_spawn
    # documents its own resume failure as FATAL, and the identity read can fail;
    # all four runtime.spawn() callers (providers/acp_runtime_start.py twice, catching
    # AcpRuntimeError; session.py:1416, :1490 catch AcpRuntimeDead) let anything
    # else through, so a raise here left a live process absent from both PID
    # files -- unreachable by every agent-runtime reaper and leaking until the
    # host reboots. kill() reaps it before we re-raise.
    #
    # BaseException so a cancellation mid-window cleans up too. This is the same
    # guard as the reader/handshake one below; they stay separate blocks because
    # only the later one has reader/stderr tasks to tear down.
    try:
        # FIRST in this block, before the resume below and before anything else
        # that can raise. A teardown may only resolve this root's process group
        # while the recorded identity still matches, so an identity recorded
        # after the resume would leave every failure path in between holding a
        # live root that no teardown can signal -- the exact leak this block
        # exists to reap. Inside the block because the read itself can raise.
        # It is in-process and non-blocking on every platform, so no executor.
        self._start_time = platform_compat.get_process_start_id(self._pid)
        # Windows resource ceiling, applied while the child is still SUSPENDED,
        # then resumed. No-op on POSIX (CREATE_SUSPENDED is 0 there). This shared
        # runtime multiplexes many session handles, so an unbounded fork/memory
        # blowup here takes down every session on it, not just one. Offloaded for
        # the same reason as in `AcpClient._spawn`: the Windows path reads config
        # and walks the process and thread tables, and this runtime's event loop
        # is serving every other session while it spawns.
        await platform_compat.finish_windows_cleanup_owned_spawn(
            lambda: asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(
                    finish_suspended_spawn,
                    self._process,
                    self._pid,
                    label=f"{KIRO_CLI_BIN} acp",
                ),
            )
        )
        self._spawn_monotonic = time.monotonic()
        self._last_activity = time.monotonic()
        if self._scratch_dir is not None:
            # Liveness anchor for the scratch sweeps: a dir whose recorded
            # owner is dead is reclaimable. Off-loop (file write), fail-open
            # (an unowned dir falls under the grace-window rule instead).
            owner_outcome = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(agent_scratch.record_owner, self._scratch_dir, self._pid),
            )
            if owner_outcome == "refused":
                # A LINK where this child's own marker belongs: the child
                # owns that directory and has pointed it somewhere else, so
                # it is steering an UNSANDBOXED gateway write at a path of
                # its choosing. Raising hands it to the guard below, which
                # reaps the process -- the spawn must not carry on as if the
                # owner had been recorded.
                raise agent_scratch.ScratchBoundaryError(
                    "the spawned agent replaced its scratch owner marker with a link"
                )
            if owner_outcome == "stale":
                # Not an attack: the update failed and the marker it left
                # could not be cleared, so this dir still names the GATEWAY.
                # That pid dies with the gateway while this child lives on,
                # which is the state a later sweep reads as a dead owner
                # before deleting a live agent's temp dir. Reaping now is
                # recoverable; that deletion is not.
                raise agent_scratch.ScratchBoundaryError(
                    "the scratch owner marker still names the gateway after a failed update"
                )
        if self._shared_scratch is not None:
            # Join the tree's owner marker BESIDE its other live users -- the
            # parent this companion serves, or the draining predecessor this
            # successor replaces. Naming only one side leaves a dead pid over
            # a live user whichever process dies first, and the sweep reads
            # dead-plus-idle as reclaimable.
            adopt_outcome = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(agent_scratch.adopt_owner, self._shared_scratch, self._pid),
            )
            if adopt_outcome == "refused":
                # Same guard as the own-dir marker above: a link where the
                # marker belongs is a live process steering an unsandboxed
                # gateway write, and the spawn must not carry on. The
                # subclass names WHICH marker, for the caller that inherits
                # on a slot's behalf (see SharedScratchJoinError).
                raise agent_scratch.SharedScratchJoinError(
                    "the inherited scratch owner marker was replaced with a link"
                )
            if adopt_outcome == "stale":
                # The marker still names only the other users: their exit
                # would read as a dead owner over THIS runtime's live use,
                # which is the deletion this whole mechanism exists to
                # prevent. Reaping now is recoverable; that is not.
                raise agent_scratch.SharedScratchJoinError(
                    "the inherited scratch owner marker could not be joined"
                )
            if adopt_outcome != "recorded":
                # "unwritable": the marker was discarded, so the tree is
                # UNOWNED and never swept -- a leak a human can see rather
                # than a deletion under a live runtime.
                logger.warning(
                    "agent-scratch: could not join the owner marker of %r (%s); the tree's "
                    "work directory is left unowned and will not be swept",
                    self._shared_scratch.name,
                    adopt_outcome,
                )
    except BaseException:
        logger.error(
            "AcpRuntime: spawn failed after the process was live (PID %s); reaping it "
            "so it cannot leak untracked",
            self._pid,
            exc_info=True,
        )
        try:
            await self.kill(reason="reap after failed spawn")
        except Exception:
            logger.warning(
                "AcpRuntime: cleanup reap after a failed spawn did not complete for PID %s",
                self._pid,
                exc_info=True,
            )
        raise
    logger.info(
        "AcpRuntime spawned backend=%s agent=%s (PID %d)",
        self._acp_backend,
        self._agent or "<none>",
        self._pid,
    )

    # Track the PID for orphan cleanup (mirrors AcpClient._spawn). Without
    # this, a kiro-cli process leaked by a gateway crash/restart is never
    # recorded in kiro_session_pids.txt, so startup cleanup can't reap it.
    # A LIVE runtime is already protected during the periodic sweep because
    # AcpSessionProvider._pid feeds _collect_active_pids — this only closes
    # the cross-restart leak.
    # Shield this shared runtime's PID from the periodic orphan sweep.
    # _bg_runtime and companion subagent runtimes are held only in
    # SessionManager instance attributes (not registered sessions /
    # warm-pool providers), so _collect_active_pids would otherwise
    # classify them as orphans and SIGKILL them mid-use.
    #
    # Ordered BEFORE the two file appends, which is the only ordering that
    # is safe: register_protected_pid is an in-memory set insert under a
    # threading lock with no IO, so it cannot fail for the reasons an append
    # can (ENOSPC, a wedged file lock). Behind the appends it was reachable
    # only if they both succeeded, so one failed append escalated into a
    # LIVE runtime losing its shield and being SIGKILLed mid-use by the very
    # sweep this call exists to hide it from.
    register_protected_pid(self._pid)
    # Read BEFORE the appends, so the identity held here is the one this
    # process had when the gateway took it on. It is what lets an OBSERVED
    # death retire the session line that names THIS process and no other --
    # and it is handed to the tracker below, so the line written and the
    # line later compared are ONE read of the identity, not two.
    self._spawn_start_token = _pid_start_token(self._pid)
    # Off the loop, as ``AcpClient._spawn`` does: each tracker takes an
    # exclusive file lock and now, on a recycled number, rewrites the file
    # under it -- blocking syscalls the heartbeat and every session would
    # wait behind. ONE hop for the pair, and the pair's inputs are captured
    # here so the worker never reads ``self`` after this method has moved on.
    root_pid = self._pid
    root_token = self._spawn_start_token

    def _track_root_pids() -> None:
        _track_pid(root_pid)
        _track_session_pid(root_pid, root_token)

    tracking = asyncio.get_running_loop().run_in_executor(subprocess_executor(), _track_root_pids)
    try:
        # Shielded: a cancellation delivered here must not also cancel the
        # worker's future -- the worker cannot be stopped once it has begun,
        # and the ``except BaseException`` below relies on being able to
        # WAIT for it.
        await asyncio.shield(tracking)
    except Exception:
        # A runtime that is not in the PID files is unreachable by every
        # agent-runtime reaper: cleanup_orphaned_sessions,
        # _periodic_pid_sweep and cleanup_orphaned_session_roots all read
        # those files, and the /proc orphan scan declines managed agent
        # runtimes on purpose (session_pid._MANAGED_AGENT_MARKERS is a
        # negative gate) precisely because this lifecycle is meant to own
        # them. So the process keeps working, holds hundreds of MB, and
        # leaks for the rest of the host's uptime.
        #
        # ERROR, not debug: this log line is the only signal that will ever
        # be emitted for that leak. A failed PID-file REWRITE is loud for
        # the same reason; this is the append half.
        logger.error(
            "AcpRuntime: PID tracking failed for %s — this runtime is now "
            "invisible to every reaper and will leak until the host reboots",
            self._pid,
            exc_info=True,
        )
    except BaseException:
        # The hop is an AWAIT, so it is a cancellation point -- one that sits
        # between this method's two ``except BaseException`` reap guards, so
        # a ``CancelledError`` here would otherwise leave ``spawn()`` with no
        # ``kill()``: a live child, already shielded from the sweep by
        # ``register_protected_pid``, recorded in neither registry.
        #
        # Two things, in this order. FIRST wait for the worker: it may be
        # inside ``_track_root_pids`` right now, and an append that lands
        # AFTER the reap has untracked the pid resurrects a registry line
        # for a dead, recyclable number -- the line a later sweep kills by.
        # THEN reap. Both run as ONE task that this frame only WAITS on:
        # a second ``cancel()`` (a newer slot signal, a slot deletion -- two
        # ordinary dashboard paths cancel this same eager-spawn task) lands
        # at the await below, not inside the cleanup, and is absorbed until
        # the cleanup settles, so the reap cannot be skipped by being asked
        # twice. The bound is the file-lock ceiling the worker itself lives
        # under plus the kill path's own. The cancellation propagates after.
        cleanup = asyncio.ensure_future(self._reap_after_cancelled_tracking(tracking))
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                continue
        raise

    # Everything after the subprocess exists must be guarded: if reader
    # startup or the initialize handshake fails (kiro-cli hang / auth stall),
    # the process, its reader/stderr tasks, its PID-file entries AND its
    # _PROTECTED_PIDS shield would all leak. kill() reaps them (and
    # unregisters the protected PID via _mark_dead) before we re-raise.
    # BaseException so CancelledError during the handshake also cleans up.
    try:
        # Start stderr drain
        if self._process.stderr:
            self._stderr_task = asyncio.ensure_future(self._drain_stderr())

        # Start the single reader task — owns stdout exclusively
        self._reader_task = asyncio.ensure_future(self._reader_loop())

        # Start the process-exit watcher — fails the runtime on the root's
        # own exit even when stdout stays open behind a surviving descendant.
        self._exit_watch_task = asyncio.ensure_future(self._exit_watch_loop())

        # Protocol handshake ("initialize"); the cold-start budget is
        # _INITIALIZE_TIMEOUT -- see _initialize_handshake. The capabilities
        # were resolved above, before the process existed.
        init_resp = await self._initialize_handshake(client_capabilities)
        _agent_caps = init_resp.get("agentCapabilities", {})
        self._agent_capabilities = _agent_caps if isinstance(_agent_caps, dict) else {}
        self._can_load_session = bool(self._agent_capabilities.get("loadSession", False))
        # Retain promptCapabilities so the prompt path can gate non-text
        # blocks -- without them an image block would be sent regardless of
        # whether the agent accepts one, and a refusal would surface as a
        # generic error with no fallback.
        _prompt_caps = init_resp.get("agentCapabilities", {}).get("promptCapabilities", {})
        self._prompt_capabilities = _prompt_caps if isinstance(_prompt_caps, dict) else {}
        self._agent_version = agent_version_from_init(init_resp)

        # The subprocess has now read its agent spec, which closes the window the
        # pre-spawn snapshot opened: a write landing before this point is caught
        # here, and one landing after cannot change what kiro-cli already loaded.
        # Deliberately INSIDE the guard below -- it kills the process, reaps the
        # PID-file entries and the protected-PID shield, then re-raises -- because
        # a session that may have loaded an unverified spec must not survive, and
        # leaving the process behind would be a worse outcome than the stale spec.
        from kiro_crew.agent import require_unchanged_derived_spec

        await asyncio.to_thread(require_unchanged_derived_spec, self._derived_spec_snapshot)
        self._initialized = True
        # The scope unit name is logged beside the PID because it is the only
        # durable half of the join. An OOM report or ``systemctl`` listing
        # names ``kirocrew-rt-<token>.scope``; the token otherwise lives only
        # in the child's /proc/<pid>/environ and in _process_instance, both of
        # which the kill destroys. This line survives it, so the token in the
        # report resolves to this runtime's pid. A spawn that dies BEFORE this
        # point has served no session, so there is nothing to resolve for it.
        # ``unnamed`` is the honest reading on a host where no scope was
        # created, and it is reachable: _scope_unit is empty whenever the
        # cgroup wrap did not happen, which is every non-delegated host.
        logger.info(
            "AcpRuntime initialized (PID %d, scope %s)",
            self._pid,
            self._scope_unit or "unnamed",
        )
        # INSIDE the guard, which is what makes a cancelled scan safe. The
        # runtime is already `_initialized` here and the caller does not hold
        # it yet, so a CancelledError raised inside the scan -- an ordinary
        # shutdown or a spawn-budget timeout during a /proc walk -- would
        # otherwise leave a live process nobody owns. The guard's kill is the
        # right answer to that, and it cannot fire for a merely FAILED scan:
        # _snapshot_descendants swallows every Exception itself, so only a
        # cancellation reaches this arm.
        await self._snapshot_descendants(retry_when_empty=True)
    except BaseException:
        # One task this frame only WAITS on, so a cancellation cannot cut the
        # cleanup short. Two ordinary dashboard paths (a newer slot signal, a
        # slot deletion) cancel this same eager-spawn task, and a cancel that
        # landed inside the descendant scan below would drop the only record
        # of the MCP servers the group kill cannot reach. A cancel delivered
        # while the cleanup runs is absorbed until it settles, then propagated
        # in place of the original failure. Same shape as the tracking reap
        # guard above.
        cleanup = asyncio.ensure_future(self._failed_start_cleanup())
        cancelled = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        if not cleanup.cancelled() and cleanup.exception() is not None:
            logger.warning(
                "AcpRuntime: failed-start cleanup raised",
                exc_info=cleanup.exception(),
            )
        if cancelled:
            raise asyncio.CancelledError()
        raise


async def _failed_start_cleanup(self: AcpRuntime) -> None:
    """Settle stderr, record the tree, kill it, and reap what left the group.

    Runs as its own task under ``_spawn_admitted``'s failed-start guard, which
    shields it, so every step completes even when the spawning task is
    cancelled mid-cleanup.
    """
    # BEFORE the kill, which is the whole point of the ordering. The
    # cleanup below cancels the stderr drain, and a line still in the pipe
    # when it does is a line nobody will ever read -- so the caller's own
    # settle finds the task already done and learns nothing. Draining here
    # is the last moment the child's own account of why it could not start
    # is still reachable, and a sandbox refusal is exactly the failure that
    # arrives this way: the child writes its signature and closes stdout
    # together. Bounded (see ``settle_stderr``); this is already the failure
    # path. A cancel of THIS task (loop shutdown) is absorbed here as it is
    # in the tree scan below, so the kill that follows still runs.
    try:
        await self.settle_stderr()
    except asyncio.CancelledError:
        pass
    # The group kill below reaches only kiro-cli's own process group, and
    # every stdio MCP server it launches leads a group of its own. A
    # runtime that served a session has those recorded by the descendant
    # scan at the end of a successful start; one that failed before it has
    # none, so its MCP servers outlive it unrecorded. Record the tree now,
    # while the root still links it, and kill whatever of it survives the
    # group kill.
    escapees = await self._record_tree_before_failed_start_kill()
    # Withheld from the kill's own teardown, which would otherwise report
    # every recorded MCP server as a survivor it leaves to the orphan sweep,
    # milliseconds before the reap below kills it. The reap prunes their
    # tracking entries itself, and owns the verdict on whether any survived.
    self._child_pids = {}
    try:
        # This death IS abnormal (failed spawn/handshake): kill()'s
        # expected=False default keeps its log at WARNING.
        await self.kill(reason="failed init handshake cleanup")
    except Exception:
        logger.debug("AcpRuntime: cleanup kill after failed spawn/handshake failed", exc_info=True)
    if escapees:
        await self._kill_failed_start_escapees(escapees)


async def _record_tree_before_failed_start_kill(self: AcpRuntime) -> dict[int, ChildRecord]:
    """The descendants of a runtime whose start failed, read before its kill.

    Best-effort and bounded: the scan swallows its own failures. The caller
    runs it inside a shielded cleanup task, so the spawning task's
    cancellation never reaches it; a cancellation of the cleanup task itself
    (loop shutdown) is absorbed so the kill that follows still runs, leaving
    the tree to the group kill alone.
    """
    if platform_compat.IS_WINDOWS:
        return {}
    try:
        await self._snapshot_descendants()
    except asyncio.CancelledError:
        return {}
    return dict(self._child_pids)


async def _kill_failed_start_escapees(self: AcpRuntime, escapees: dict[int, ChildRecord]) -> None:
    """SIGKILL the recorded descendants of a failed start that are still ours.

    Each is checked against the identity recorded at the scan, so a number
    reused since then is skipped. Entries of the ones confirmed gone are
    pruned from the tracking file; a survivor keeps its entry for the sweep.
    A just-killed process is not gone at once (it is a zombie until its new
    parent reaps it), so the verdict waits a bounded moment for the exits
    before calling anything a survivor.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + self._FAILED_START_REAP_WAIT
    try:
        await asyncio.to_thread(_kill_escaped_children, escapees)
        pending = escapees
        while True:
            survivors = await asyncio.to_thread(_prune_dead_descendants, pending)
            if not survivors or loop.time() >= deadline:
                break
            pending = {pid: escapees[pid] for pid in survivors}
            await asyncio.sleep(0.05)
    except Exception:
        self._process_tree_confirmed_dead = False
        logger.warning(
            "AcpRuntime: could not reap the descendants of a failed start", exc_info=True
        )
        return
    if survivors:
        self._process_tree_confirmed_dead = False
        logger.warning(
            "AcpRuntime: %d descendant PID(s) of a failed start survived SIGKILL; "
            "left tracked for the orphan sweep: %s",
            len(survivors),
            survivors,
        )


async def _handshake_client_capabilities(self: AcpRuntime) -> dict[str, Any]:
    """The ``clientCapabilities`` this spawn sends, with the settings channel filled.

    The harness declares the shape; this fills ``_meta.kiro.settings`` ONLY
    for a host that reads it (``client_meta_settings``) and only with values
    the operator threaded in. Today that is MCP Tool Search, and the value
    sent is gated on the spawn agent's spec granting the ``tool_search``
    loader: KAS defers every MCP spec when told to and never checks that a
    loader is mounted, so a spec without the grant would run with its MCP
    tools deferred and no way to load one. ``enabled`` therefore goes out as
    an explicit false for such a spec rather than being left to the host's
    default. The spec judged is the one the KAS projection will PUT ON THE
    WIRE at session/new -- the freshness gate's snapshot for a derived agent,
    else the spec ``resolve_projected_spec`` resolves nearest-first, the same
    call the projection makes: a spec judged here that differed from the one
    projected would turn deferral on for a session with no loader.
    An unreadable spec grants nothing (fail closed: ``enabled: false``).
    ``secretStorage`` is added first, when :func:`_with_mcp_sign_in_storage` says so.
    """
    base = self._harness.client_capabilities
    base = await _with_mcp_sign_in_storage(self, base)
    if self._tool_search is None:
        return base
    spec = await asyncio.to_thread(self._projected_spawn_spec)
    loader_granted = spec_grants_tool_search(spec)
    settings = kas_client_meta_settings(self._tool_search, loader_granted=loader_granted)
    self._tool_search_wire = settings
    logger.info(
        "AcpRuntime handshake: MCP Tool Search %s for agent=%s "
        "(configured=%s, spec grants tool_search=%s)",
        "enabled" if settings["toolSearch"]["enabled"] else "disabled",
        self._agent or "<none>",
        self._tool_search.enabled,
        loader_granted,
    )
    return with_client_meta_settings(base, settings)


async def _with_mcp_sign_in_storage(runtime: AcpRuntime, base: dict[str, Any]) -> dict[str, Any]:
    """*base*, plus ``_meta.kiro.secretStorage`` when this spawn keeps MCP sign-ins.

    Only a host that runs MCP sign-ins through Crew (``opens_external_urls``, KAS
    today) can be asked, and only with ``agent.kas_persist_mcp_sign_in`` on. The
    decision is made once per spawn and held on the runtime as the server that
    answers this process's ``_kiro/secret/*`` requests, so the reader loop serves
    them exactly when this handshake declared the capability, whatever config
    says later.
    """
    runtime._mcp_secret_server = None
    if not runtime._harness.opens_external_urls:
        return base
    if not await asyncio.to_thread(kas_secret_storage.persistence_enabled):
        return base
    runtime._mcp_secret_server = kas_secret_storage.SecretStorageServer()
    logger.info("AcpRuntime handshake: remote MCP sign-ins are kept in Crew's grant vault")
    return kas_secret_storage.with_secret_storage(base)


def _projected_spawn_spec(self: AcpRuntime) -> dict[str, Any] | None:
    """The spawn agent's spec exactly as the wire projection will send it.

    Blocking (a file read); callers run it off the loop. The derived-agent
    branch reads NOTHING: the freshness gate that ran in ``_resolve_spawn_plan``
    already verified those bytes, and a second read here would be a second
    observation of a file a revocation could land in between. Every other
    agent is resolved the way ``KasHarness.session_extras`` resolves it,
    through ``resolve_projected_spec``, so the two cannot disagree about which
    spec a session runs.
    """
    snapshot = self._derived_spec_snapshot
    spec = getattr(snapshot, "spec", None)
    if isinstance(spec, dict):
        return spec
    # The same best-effort self-heal the projection runs before ITS read
    # (``KasHarness.session_extras``): on a checkout that skipped setup the
    # managed default does not exist yet, and reading it as absent here would
    # decide "no loader" for the process while the projection, a moment later,
    # materializes a spec that grants one.
    ensure_agent_materialized(self._agent)
    # Deferred: the harness module imports this package's types at its own top level.
    from kiro_crew.acp.harness.kas import resolve_projected_spec

    try:
        return resolve_projected_spec(
            kiro_agents_dir(), self._agent, getattr(self, "_work_dir", None)
        )[0]
    except Exception:
        logger.warning(
            "agent %r: spec unreadable at spawn; MCP Tool Search stays off for this process",
            self._agent or "<none>",
            exc_info=True,
        )
        return None


async def _source_agent(agent: str | None) -> str | None:
    """*agent*, or the agent a stored skill-view name was built from.

    Every mode name that reaches ``session/set_mode`` enters through the spawn
    agent, ``create_session`` / ``load_session``, or a handle's ``set_mode``,
    and each maps it here first. A view name the source of which nothing
    records is refused with the user's sentence rather than sent, because
    kiro-cli would only answer that it is not found.
    """
    if not agent:
        return agent
    from kiro_crew.acp.skill_projection import RetiredSkillView, resolve_source_agent

    try:
        source = await resolve_source_agent(agent)
    except RetiredSkillView as exc:
        raise AcpRuntimeError(str(exc)) from exc
    if source != agent:
        logger.info("skill view %s maps back to agent %s", agent, source)
    return source


def _adopt_launch_copy_view(projection: Any, launch_agent: str) -> None:
    """Hold the app launch copy *launch_agent* in *projection* as a prepared view.

    Read from ``~/.kiro/agents/<launch_agent>.json`` by its path: the copy's name is
    reserved, and a name lookup is what the copy exists to avoid. It is a search
    agent exactly when the projection's verdict on publishing it said so
    (``apps.agent_launch.is_search_launch_copy``), so a session whose control plane
    cannot mount is refused as for the same agent's projected view and for no other.
    An unreadable copy adopts nothing, and the session start's own checks then decide.
    """
    from kiro_crew.agent_discovery import _read_agent_spec

    view = _read_agent_spec(
        kiro_agents_dir() / f"{launch_agent}.json", operation="app_agent_launch", source="acp"
    )
    if not isinstance(view, dict):
        return
    from kiro_crew.apps.agent_launch import is_search_launch_copy

    projection.specs[launch_agent] = view
    if is_search_launch_copy(launch_agent):
        projection.search_agents.add(launch_agent)
