"""Start an ``AcpClient`` session: spawn the child, run the initialize handshake, and create, load or fork the session.

``_spawn``, ``_initialize_params``, ``_initialize_session``, ``_new_session_following_substitution``,
``_fork_session`` and ``ensure_ready``, the entry point that drives them.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_activated_model``, ``_agent_accepts_image``,
``_agent_env_snapshot``, ``_agent_version``, ``_agent_version_read``, ``_can_load_session``, ``_child_pids``,
``_derived_spec_snapshot``, ``_fork_request``, ``_jsonl_pos``, ``_kill_sent``,
``_last_activity``, ``_last_substitution_model``, ``_mode_frame_awaited``,
``_mode_frame_superseded``, ``_model``, ``_native_skill_projection``, ``_pid``,
``_process``, ``_process_instance``, ``_process_tree_confirmed_dead``,
``_resume_session_id``, ``_resumed``, ``_session_id``, ``_session_mcp_cache``,
``_spawn_start_token``, ``_start_time``, ``_stderr_task``, ``_work_dir_ready``.

Instance fields read: ``_agent``, ``_available_mode_ids``, ``_channel_id``,
``_extra_env``, ``_modes_advertised``, ``_sandbox_mode``, ``_scratch_dir``,
``_session_key``, ``_session_mcp_snapshot``, ``_shared_scratch``, ``_side_read_only``,
``_stdin_stalled``, ``_windows_tree_cleanup_failed``, ``_work_dir``, ``last_prompt_stats``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from kiro_crew import __version__, acp_tool_gate, agent_scratch, platform_compat
from kiro_crew.acp import managed_agent_shadow as managed_shadow_mod
from kiro_crew.acp import runtime_process_tree
from kiro_crew.acp._dispatch import agent_version_from_init
from kiro_crew.acp.client_claude_session import (
    CLAUDE_ACP_FORK_POINT_MIN_VERSION,
    _adapter_version_at_least,
)
from kiro_crew.acp.client_native_children import client_capabilities_for
from kiro_crew.acp.client_spawn_env import (
    KIRO_CLI_BIN,
    KIRO_CLI_SUBCMD,
    _KiroExecutableTrustError,
    _launch_tools,
    _resolve_kiro_bin_for_spawn,
    finish_suspended_spawn,
    kiro_cli_not_found_message,
)
from kiro_crew.acp.client_timeouts import _ACP_RESPAWN_BACKOFF_S, _INIT_TIMEOUT
from kiro_crew.acp.client_turn_control import steering_advertised
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.harness.base import SpawnContext, SpawnPlan
from kiro_crew.acp.launch import LaunchRequest, launch
from kiro_crew.acp.transport_errors import (
    AcpAuthRequired,
    AcpError,
    AcpProcessDied,
    AcpTimeoutError,
    AcpToolGateUnroutable,
    registration_rate_limited_error,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKENDS_FORK_AT_MESSAGE,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_LOAD_WITHOUT_MODES,
    ACP_BACKENDS_POD_HOME_REMAP,
    ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP,
    ACP_BACKENDS_RESUME_WITHOUT_LOAD,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    KIRO_CLI_CLIENT_APPLICATION,
    METHOD_INITIALIZE,
    METHOD_SESSION_FORK,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_RESUME,
    METHOD_SET_MODE,
)
from kiro_crew.agent import (
    DerivedSpecSnapshot,
    DerivedSpecStale,
    ForkGovernanceUnresolved,
    ensure_agent_materialized,
    require_fork_governance,
    require_fresh_derived_spec,
    require_unchanged_derived_spec,
)
from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_FORBIDDEN_MODES,
    ACP_BACKEND_LAUNCH,
    launch_for,
)
from kiro_crew.config.paths import kiro_sessions_dir
from kiro_crew.dashboard.side_readonly_spec import unavailable_mode_explanation
from kiro_crew.executors import subprocess_executor
from kiro_crew.owner_only_files import ensure_directory
from kiro_crew.sandbox import (
    assert_voice_runtime_outside_agent_workspace,
    delegated_workspace_exposes_sealed_target,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.slack.team_ids import apply_slack_team_env

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")

#: Bound by ``kiro_crew.acp.client``, which defines it: the spawn audit, the
#: subprocess-encoding baseline and the cross-platform gate key it on that path.
_resolve_spawn_env: Callable[..., dict[str, str]]


#: The name sent as ``clientInfo.name``. It IS the kiro-cli client-application
#: tag, so one filter string finds Crew on both channels and the two cannot drift.
CLIENT_NAME = KIRO_CLI_CLIENT_APPLICATION
#: The version reported to the agent host in ``initialize``'s ``clientInfo``, and
#: from there into the host's own telemetry (``acp_client_version``). It is the
#: PACKAGE version, never a literal of its own: a hand-maintained literal is a
#: second version number nobody bumps, and this one was not -- it sat at
#: ``"0.1.2"`` from the first commit that named it while the product shipped
#: 0.2.0 through 0.8.0, so every Crew-driven session of every release reported
#: one indistinguishable version and no version split of Crew traffic was
#: possible. Reading ``__version__`` also inherits what that attribute already
#: resolves at import: a release lane's rewritten literal AND a repackager's
#: ``BUILD_VERSION`` stamp.
CLIENT_VERSION = __version__
# kiro-cli uses a date-stamped protocol; claude-agent-acp follows the
# upstream ACP SDK (numeric integer, currently 1).  See acp.types.
PROTOCOL_VERSION = "2025-08-22"
PROTOCOL_VERSION_CLAUDE = 1
# codex-acp has no literal here, and its absence is the point: this core drives
# only the harnesses in the table below, and codex is not one of them. Its
# handshake dialect is declared by ``acp.harness.codex``, on the core that does
# drive it. One harness, one declaration -- a second copy on a core that never
# performs the handshake is a copy nothing can keep honest.
# OpenCode answers ``initialize`` with an integer ``protocolVersion`` of 1, so it
# speaks the SPEC dialect rather than kiro-cli's date-stamped one. Verified off its
# own wire, and its own literal for the same reason codex has one (harness-parity
# H10): a divergence should be a one-line edit here, not a silent downgrade of
# whichever harness moved first.
PROTOCOL_VERSION_OPENCODE = launch_for(ACP_BACKEND_OPENCODE).protocol_version
# pi-acp answers ``initialize`` with an integer ``protocolVersion`` of 1 as well,
# verified off its own wire; its own literal for the same H10 reason.
PROTOCOL_VERSION_PI = 1
# goose answers ``initialize`` with an integer ``protocolVersion`` of 1, measured on
# goose 1.50.1's own wire (``test/fixtures/acp_frames/goose/handshake-live.jsonl``);
# its own literal for the same H10 reason.
PROTOCOL_VERSION_GOOSE = launch_for(ACP_BACKEND_GOOSE).protocol_version
# DeepSeek Harness answers ``initialize`` with an integer ``protocolVersion`` of 1,
# so it speaks the SPEC dialect too. Verified off its own wire, and its own literal
# for the same reason the two above have one (harness-parity H10).
PROTOCOL_VERSION_DEEPSEEK = launch_for(ACP_BACKEND_DEEPSEEK).protocol_version
#: Handshake dialect per harness. A TABLE, not an if-chain: the handshake runs on
#: the construction path kiro-cli shares with every adapter, and harness-parity H13
#: keeps that path free of conditionals added in service of one. A harness added
#: later is one row here; an id with no row speaks kiro-cli's date-stamped dialect.
_PROTOCOL_VERSION_BY_BACKEND: dict[str, int | str] = {
    ACP_BACKEND_CLAUDE: PROTOCOL_VERSION_CLAUDE,
    ACP_BACKEND_PI: PROTOCOL_VERSION_PI,
    # Every harness whose own binary serves ACP declares its dialect in its
    # ``ACP_BACKEND_LAUNCH`` row, so those rows are read rather than restated here.
    # The two adapters above keep explicit rows: each is a separate package with
    # its own release cadence, and none has a launch record to read.
    **{backend: record.protocol_version for backend, record in sorted(ACP_BACKEND_LAUNCH.items())},
}


async def _spawn(self: AcpClient) -> None:
    """Start the ACP backend subprocess with stdio pipes, suspended, then track it.

    A host this client starts one process per session for -- claude-agent-acp,
    OpenCode, goose, pi, the DeepSeek Harness -- resolves its plan in its own
    adapter (:func:`kiro_crew.acp.harness.process_adapter_for`); kiro-cli keeps its
    own arm below. Either plan goes through the launch tail both drivers share
    (:func:`kiro_crew.acp.launch.launch`). What happens once the process exists --
    the resume, its identity, its pids, its stderr -- stays here.
    """
    # Off-loop: mkdir is a blocking syscall and the parent dirs may live on
    # slow storage; the loop must never wait on the kernel here. The
    # unresolved-ref snapshot rides IN this hop rather than in one of its own:
    # the detector runs for every harness (the defect it catches has shipped on
    # three), and a second await would be a new suspension point on kiro-cli's
    # construction path in service of a diagnostic -- so the count of awaits
    # here is deliberately unchanged (harness-parity H13).
    await asyncio.to_thread(self._prepare_spawn_workspace)
    # A fresh spawn reads ``agent.env`` afresh, once (_agent_env_for_spawn).
    self._agent_env_snapshot = None

    # Kiro's internal macOS sandbox replaces (rather than nests inside)
    # Kiro Crew's Seatbelt profile. Refuse a delegated agent workspace that
    # can reach the protected named decoder snapshots; otherwise a same-UID
    # agent could replace verified bytes before the decoder spawn opens them.
    if self.backend in ACP_BACKENDS_INTERNAL_SANDBOX:
        await asyncio.to_thread(assert_voice_runtime_outside_agent_workspace, self._work_dir)

    adapter = process_adapter_for(self.backend)
    if adapter is not None:
        # The host's own launch knowledge -- its binary, argv, labels, credential
        # mask and gate -- in its own file. The context names this session, which
        # the host may need to prepare before it starts (its MCP array, a seed)
        # and which keeps the facts its turn-time code reads back. The
        # environment and home fields are the shared context's; no per-process
        # host reads them.
        plan = await adapter.resolve_spawn(
            SpawnContext(
                agent=self._agent,
                work_dir=self._work_dir,
                model=self._model,
                environ=dict(os.environ),
                home=Path(os.path.expanduser("~")),
                sandbox_mode=self._sandbox_mode,
                session=self,
            )
        )
    else:
        # Pin ONE reading of the environment for both the search and the
        # message that reports it. The previous code resolved against the live
        # ``os.environ`` and then, on failure, recomputed the directory set
        # from a FRESH read -- so a PATH change landing in that window (a
        # concurrent installer, a self-update, anything editing the gateway's
        # environment) would produce a "not found" message naming directories
        # that were never searched, while omitting ones that were. Caching the
        # search path WITH the resolution result is what avoids that, and is
        # the same guarantee the Claude adapter carries.
        spawn_environ = dict(os.environ)
        spawn_home = Path.home()
        try:
            kiro_bin = await _resolve_kiro_bin_for_spawn(environ=spawn_environ, home=spawn_home)
        except _KiroExecutableTrustError as exc:
            raise AcpError(str(exc)) from exc
        if not kiro_bin:
            # Pure function of the arguments, so this reproduces exactly the
            # set the resolution above walked. Still off-loop: it expands the
            # inherited PATH.
            missing = AcpError(
                await asyncio.to_thread(
                    kiro_cli_not_found_message,
                    environ=spawn_environ,
                    home=spawn_home,
                )
            )
            missing.kiro_cli_missing = True
            raise missing
        # Self-heal (B): ensure the managed default agent file exists before
        # this --agent spawn, so kiro-cli registers the mode and step 4's
        # set_mode succeeds instead of faulting "Mode not found". Best-effort,
        # off the loop; non-managed agents fall through to the step-4 guard.
        try:
            await asyncio.to_thread(ensure_agent_materialized, self._agent)
        except Exception:
            logger.warning("pre-spawn agent materialization failed", exc_info=True)
        # ALSO not best-effort, and for the same reason as the fork gate below: a
        # DERIVED spec (kirocrew-worker, kirocrew-heartbeat) that predates the default
        # agent's still mounts and auto-approves a server the default does not have, so
        # proceeding would run ungoverned grants. Repairs first and refuses only
        # when it cannot -- a refused dispatch is recoverable and reportable,
        # which a worker running on a revoked server is not.
        try:
            derived_snapshot = await asyncio.to_thread(
                require_fresh_derived_spec, self._agent, self._work_dir
            )
        except DerivedSpecStale as exc:
            raise AcpError(str(exc)) from exc
        # Captured for the post-load half of the bracket, which is NOT here: this
        # method creates the child and returns after bookkeeping, so at this point
        # the subprocess has not provably read its spec and closing the bracket
        # would prove nothing. ``_initialize_session`` closes it against this
        # object, at the ``initialize`` response -- the earliest signal that the
        # read happened. Same pairing as the AcpRuntime path.
        self._derived_spec_snapshot = derived_snapshot
        # NOT best-effort: a fork-backed agent may not spawn until fork
        # governance is re-projected, and a failed or timed-out refresh
        # ABORTS the spawn — its on-disk allowedTools/autoApprove bypass
        # the PreToolUse gate, so proceeding would run ungoverned grants.
        # The work dir is the cwd kiro-cli resolves --agent against first,
        # so the gate also refuses a fork shadowed by a project-local spec.
        try:
            await asyncio.to_thread(require_fork_governance, self._agent, self._work_dir)
        except ForkGovernanceUnresolved as exc:
            raise AcpError(str(exc)) from exc
        # The same cwd-first resolution makes a checkout that declares a MANAGED
        # agent name the spec that runs, whatever Kiro Crew installed under it.
        shadowed = await asyncio.to_thread(
            managed_shadow_mod.managed_agent_shadow_refusal, self._agent, self._work_dir
        )
        if shadowed:
            raise AcpError(shadowed)
        # The agents-tree seal is a launcher rule, and a spawn delegated to
        # kiro-cli's internal sandbox never sees the launcher — so on those
        # paths a workspace overlapping the agents directory is the one way
        # left to rewrite a spec. Refused before the spawn; off-loop because
        # the delegation predicate reads the kiro settings file.
        overlap = await asyncio.to_thread(delegated_workspace_exposes_sealed_target, self._work_dir)
        if overlap:
            raise AcpError(overlap)
        from kiro_crew.acp.skill_projection import prepare_native_skill_projection

        # One process, one session: the identity rides the process environment
        # (``_apply_session_identity_env``) and kiro-cli mounts ``kirocrew-core``
        # natively from the view, restrictions included, so no per-session
        # element replaces the declaration here -- the shared runtime's
        # element-withholding question does not arise, and a ``disabledTools``
        # naming other tools must not refuse this agent's view.
        self._native_skill_projection = await asyncio.to_thread(
            prepare_native_skill_projection, self._work_dir, per_session_element=False
        )
        if self._native_skill_projection is not None:
            # The agent this process is launched as: its own FIRST ``set_mode``
            # activation is tolerated even with no prepared view (see
            # NativeSkillProjection.request), which consumes the exemption --
            # so a later switch, including back to this same agent once its
            # view has vanished, takes the strict resolver and fails closed.
            self._native_skill_projection.spawn_agent_name = self._agent
            # Advertised independently of the request() exemption above: the
            # start must find the launch agent's mode in availableModes even
            # with no prepared view (see NativeSkillProjection.frame).
            self._native_skill_projection.advertised_launch_name = self._agent
        argv = [
            kiro_bin,
            KIRO_CLI_SUBCMD,
            "--agent",
            (
                self._resolve_spawn_agent_argv()
                if self._native_skill_projection is not None
                else self._agent
            ),
        ]
        spawn_label = f"{KIRO_CLI_BIN} {KIRO_CLI_SUBCMD}"
        stderr_label = KIRO_CLI_BIN
        plan = SpawnPlan(argv=argv, spawn_label=spawn_label, stderr_label=stderr_label)

    async def _env_before_scrub(env: dict[str, str], spawned_binary: str | None) -> dict[str, str]:
        # The host's own variables first, then this session's identity, so a
        # host value can never stand in for the identity the session carries.
        if adapter is not None:
            await adapter.prepare_spawn_env(env, to_thread=self._to_thread_guarding_sandbox)
        self._apply_session_identity_env(env)
        if self._channel_id:
            env["KIROCREW_CHANNEL_ID"] = self._channel_id
        else:
            env.pop("KIROCREW_CHANNEL_ID", None)
        # The workspace the channel lives in, beside the channel, so a hook can
        # open slack://channel?team=<TEAM>&id=<CHANNEL>. Set when known, popped
        # otherwise, exactly like the channel id (slack/team_ids.py).
        apply_slack_team_env(env, self._channel_id)
        # Resolve SSH_AUTH_SOCK dynamically — the gateway's env may be stale
        # after an ssh-agent restart — and KRB5CCNAME to a FILE: ccache (the
        # kernel keyring, the default on some Linux distros, is invisible to
        # this child, so Kerberos-gated MCP servers fail without it). Covers
        # the session agent and all ACP-provider subagents, which spawn through
        # this same path. The same hop settles the CLI's own KIRO_API_KEY:
        # re-injected from .env for the kiro-cli backend (post-scrub Docker),
        # actively stripped for a foreign backend, which must never receive it
        # (see config.loader.inject/strip_kiro_cli_api_key). All of this
        # glob/stat/reads under /tmp and the data home, so it runs off-loop in
        # ONE thread hop. Guarded: the sandbox temp file is live, so a
        # cancellation here must not orphan it. The launch tail scrubs after
        # it, and KIRO_API_KEY stays available only to the positively
        # identified Kiro backend.
        return await self._to_thread_guarding_sandbox(
            functools.partial(_resolve_spawn_env, kiro_api_key=self._is_kiro), env
        )

    def _env_after_scrub(env: dict[str, str]) -> None:
        # The auxiliary kiro-cli children never reach a harness's apply_spawn_env,
        # so they are named here.
        if self._is_kiro:
            from kiro_crew.acp.harness._common import apply_client_application_env

            apply_client_application_env(env)

    launched = await launch(
        self,
        LaunchRequest(
            argv=plan.argv,
            backend=self.backend,
            sandbox_mode=self._sandbox_mode,
            extra_env=self._extra_env,
            scratch_label=self._session_key or "session",
            env_before_scrub=_env_before_scrub,
            env_after_scrub=_env_after_scrub,
            agent_env=self._agent_env_snapshot,
            extra_hidden_dirs=plan.extra_hidden_dirs,
            extra_expose_files=plan.extra_expose_files,
            internal_sandbox=self.backend in ACP_BACKENDS_INTERNAL_SANDBOX,
            pod_home_remap=self.backend in ACP_BACKENDS_POD_HOME_REMAP,
        ),
        _launch_tools(),
    )
    self._process = launched.process
    self._pid = self._process.pid
    self._kill_sent = None
    self._process_tree_confirmed_dead = False
    # Minted with the process it names, random rather than pid-derived: a
    # pid can be reused by the OS, and the start-time disambiguator is not
    # readable on every platform, so equality on a fresh random id is the
    # comparison that cannot false-match across spawns.
    self._process_instance = uuid.uuid4().hex[:16]
    _spawn_label = plan.spawn_label
    # Everything from here to the end of _spawn runs with a LIVE subprocess
    # that nothing has recorded yet, so every step must be guarded. Without
    # this, any exception in the window — finish_suspended_spawn, the
    # start-time read, the two PID-file appends, the descendant scan — unwinds
    # out of _spawn leaving that process running and absent from both PID
    # files. It is then unreachable by every agent-runtime reaper (they all
    # read those files, and the /proc orphan scan declines managed agent
    # runtimes on purpose), so it leaks until the host reboots.
    #
    # ensure_ready()'s retry loop cannot substitute for this: it only catches
    # AcpTimeoutError / AcpError, and nothing raised in this window is either
    # of those — an OSError from the executor or a wedged file lock sails
    # straight past it and its `finally` records metrics only.
    #
    # BaseException so a CancelledError mid-window cleans up too. Mirrors the
    # twin guard in acp/runtime_spawn_launch.py around reader startup + handshake.
    try:
        # Windows resource ceiling, applied while the child is still SUSPENDED,
        # then resumed. No-op on POSIX (CREATE_SUSPENDED is 0 there). OFFLOADED
        # because the Windows path reads the config file and walks the process
        # and thread tables (see the note on finish_suspended_spawn); the child
        # is frozen until it returns, so this is the one await the spawn cannot
        # skip.
        await platform_compat.finish_windows_cleanup_owned_spawn(
            lambda: asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(
                    finish_suspended_spawn, self._process, self._pid, label=_spawn_label
                ),
            )
        )
        # Identity for the recycle guards that later decide whether this pid
        # may be signalled. ``get_process_start_id`` is in-process on every
        # platform (its own docstring pins that), so unlike the ``ps``-forking
        # reader it replaces there is nothing here to offload.
        self._start_time = platform_compat.get_process_start_id(self._pid)
        if self._scratch_dir is not None:
            # Liveness anchor for the scratch sweeps -- see acp/runtime_spawn_launch.py's
            # twin block. Off-loop, and fail-open on a write that could not
            # happen: an unowned dir is covered by the grace-window rule.
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
            # Twin of acp/runtime_spawn_launch.py: join the tree's owner marker beside
            # the parent, so the sweep keeps the tree while either lives.
            adopt_outcome = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(agent_scratch.adopt_owner, self._shared_scratch, self._pid),
            )
            if adopt_outcome in ("refused", "stale"):
                raise agent_scratch.SharedScratchJoinError(
                    "the inherited scratch owner marker could not be joined"
                    if adopt_outcome == "stale"
                    else "the inherited scratch owner marker was replaced with a link"
                )
            if adopt_outcome != "recorded":
                logger.warning(
                    "agent-scratch: could not join the owner marker of %r (%s); the tree's "
                    "work directory is left unowned and will not be swept",
                    self._shared_scratch.name,
                    adopt_outcome,
                )
        logger.info("Spawned %s (PID %d)", _spawn_label, self._pid)
        # Track root PID and do an early descendant scan.  kiro-cli forks
        # child processes quickly after launch.  Recording them here means
        # _kill_process() can clean up even if _initialize_session() fails.
        from kiro_crew.session import (  # circular: session -> config.loader -> providers.acp -> acp.client
            _track_child_pids,
            _track_pid,
            _track_session_pid,
        )
        from kiro_crew.session_pid import _pid_start_token

        # Read BEFORE the appends: the identity of the process that holds the
        # number NOW, kept for the identity-bound retirement in _reset_state
        # and handed to the tracker so it records this same token.
        self._spawn_start_token = _pid_start_token(self._pid)

        # The PID-file trackers each take an exclusive file lock and do a
        # read-modify-append under it — blocking syscalls that must not run
        # on the event loop: ensure_ready() awaits _spawn() from the loop on
        # every cold start, so a contended or wedged lock holder here would
        # stall every task including the liveness heartbeat. Ride the same
        # executor as the descendant scans below.
        _loop = asyncio.get_running_loop()
        await _loop.run_in_executor(subprocess_executor(), _track_pid, self._pid)
        # Separate file for startup cleanup.
        await _loop.run_in_executor(
            subprocess_executor(), _track_session_pid, self._pid, self._spawn_start_token
        )
        await asyncio.sleep(0.3)
        early_descendants = await _loop.run_in_executor(
            subprocess_executor(), runtime_process_tree._get_child_pids, self._pid
        )
        if early_descendants:
            self._child_pids = await _loop.run_in_executor(
                subprocess_executor(),
                runtime_process_tree._capture_child_records,
                early_descendants,
            )
            await _loop.run_in_executor(
                subprocess_executor(), _track_child_pids, self._child_pids, self._pid or 0
            )
            logger.info("Early tracking %d descendants of PID %d", len(self._child_pids), self._pid)

        if self._process.stderr:
            self._stderr_task = asyncio.ensure_future(
                self._drain_stderr(self._process.stderr, label=plan.stderr_label)
            )
    except BaseException:
        logger.error(
            "Spawn of %s (PID %s) failed after the process was live; killing it so it "
            "cannot leak untracked",
            _spawn_label,
            self._pid,
            exc_info=True,
        )
        try:
            await self._cleanup_failed_live_spawn()
        except Exception:
            logger.warning(
                "Cleanup kill after a failed spawn did not complete for PID %s",
                self._pid,
                exc_info=True,
            )
        raise


async def _new_session_following_substitution(self: AcpClient) -> dict:
    """Issue ``session/new``; if the gateway substitutes the model, adopt it
    and re-issue once so a real session is actually created.

    The admin-tier policy advisory ("Model X is restricted ... Using Y
    instead.") comes back as a ``-32603`` *error frame with no sessionId* --
    the first attempt creates nothing. ``_wait_for_response`` records the
    substitute model in ``self._last_substitution_model``; here we pin
    ``self._model`` to it, re-seed ``settings.local.json`` (the claude
    backend builds a fresh SettingsManager per session/new, so the new model
    takes effect), and re-issue. Idempotent and bounded to ONE retry -- if the
    gateway substitutes again to the same/another restricted id we stop
    rather than loop. Returns the session/new response dict (possibly still
    without a sessionId, which the caller treats as a hard failure).

    The substitution retry is a claude-only path (kiro-cli never emits this
    advisory), and so is the re-seed: the kiro-cli branch writes no
    ``settings.local.json`` at all.
    """
    new_params: dict = {
        "cwd": await self._session_work_dir(),
        # kiro-cli loads servers from --agent; a harness in
        # ACP_BACKENDS_SESSION_MCP_ARRAY must be told here -- it reads no
        # agent spec of its own, so this array is the only channel Crew has
        # onto the session's MCP surface (translated from that same spec,
        # see acp/session_mcp.py). Empty on the kiro-cli path.
        # Pooled broker stubs are appended for kiro-cli: a session-injected
        # server outranks the same-named entry in the agent spec, which is
        # how pooling takes effect without writing a spec anywhere.
        # Each per-harness hook is spliced only for ITS OWN backend. An
        # edition overriding both would otherwise hand a claude session
        # codex's entries and vice versa -- and one entry whose transport the
        # adapter does not advertise fails the whole session/new, not just
        # that server. Both hooks are in-memory reads of a cache the spawn
        # path warmed, NOT executor hops: this call site is shared with
        # kiro-cli, and adapter work must not add a scheduling or failure
        # point to that backend's construction path (harness-parity H13).
        # The pooled read stays off the loop, as it already was.
        # A claude Side Chat session mounts no server: READ_ONLY refuses every
        # MCP call, and a tool that is not mounted cannot be pre-approved by
        # a settings allow rule. Checked at the splice, not in the seam, so
        # an edition's override of the seam cannot mount servers there.
        "mcpServers": [
            *(
                self._claude_session_mcp_servers()
                if self._is_claude and not getattr(self, "_side_read_only", False)
                else []
            ),
            *(self._opencode_session_mcp_servers() if self._is_opencode else []),
            *(self._goose_session_mcp_servers() if self._is_goose else []),
            *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
            *(self._copilot_session_mcp_servers() if self._is_copilot else []),
            *(self._pi_session_mcp_servers() if self._is_pi else []),
            *(await asyncio.to_thread(self._pooled_mcp_servers)),
        ],
    }
    if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
        new_params["mcpServers"] = await self._prune_unstartable_mcp(new_params["mcpServers"])
    if self._is_claude:
        new_params["_meta"] = self._claude_session_meta()

    # The roster this session put ON THE WIRE. Distinct from the agent spec
    # on disk: the backend starts the spec's own servers too, so the frames
    # that come back are a superset of this list.
    self._begin_session_report(new_params.get("mcpServers"))
    # AFTER begin_session_report, which clears the report: the guard writes a
    # row on it, and writing before the clear would erase the finding.
    self._guard_unresolved_mcp_refs(new_params.get("mcpServers"))

    self._last_substitution_model = None
    if self._is_pi:
        await self._republish_pi_mcp_handoff()
    session_id = await self._send_request(METHOD_SESSION_NEW, new_params)
    session_resp = await self._wait_for_response(
        session_id,
        timeout=_INIT_TIMEOUT,
        method=METHOD_SESSION_NEW,
        expected_mcp=new_params.get("mcpServers"),
    )

    # Happy path: a real session came back.
    if session_resp.get("sessionId"):
        return session_resp

    # Substitution path: the gateway named a model it WILL serve. Adopt it,
    # re-seed settings so the backend resolves to it, and re-issue once.
    substitute = self._last_substitution_model
    if self._is_claude and substitute and substitute != self._model:
        # Redact the substitute name before logging. It is parsed straight
        # from the untrusted ACP backend advisory (data.details "Using X
        # instead") and the gateway log fans out to the dashboard activity
        # feed and Slack -- mirror the dual-redaction discipline applied
        # to the source advisory in _wait_for_response.
        _sub_log, _ = redact_exfiltration_urls(str(substitute))
        _sub_log, _ = redact_credentials(_sub_log)
        logger.warning(
            "ACP session/new returned a substitution advisory with no session; "
            "adopting gateway-served model %r and retrying session creation.",
            _sub_log,
        )
        self._model = substitute
        # Re-seed settings.local.json so the fresh SettingsManager the adapter
        # builds for the retry resolves the substitute model (it merges
        # settings sources each session/new).
        try:
            await asyncio.to_thread(self._write_claude_local_settings)
        except (OSError, ValueError, TypeError):
            # Narrow to realistic re-seed failure modes: OSError covers
            # disk / permission errors on the atomic write; ValueError
            # and TypeError cover registry / json shape surprises.
            # Never let re-seed failure mask the retry -- worst case, the
            # adapter resolves to whatever it had cached and we still
            # retry session/new on the substitute path.
            logger.warning("re-seed of settings.local.json failed", exc_info=True)
        # The writer owns every transition in the native permission surface
        # and invalidates the frozen MCP projection when that state changes.
        # Re-resolve off the event loop, then rebuild the exact array the retry
        # will send so a failed byte re-validation withholds rather than
        # re-delivering the first attempt's stale roster.
        resolved_mcp = await asyncio.to_thread(self._resolve_session_mcp_servers)
        self._session_mcp_cache = resolved_mcp
        new_params["mcpServers"] = [
            *(
                self._claude_session_mcp_servers()
                if self._is_claude and not getattr(self, "_side_read_only", False)
                else []
            ),
            *(self._opencode_session_mcp_servers() if self._is_opencode else []),
            *(self._goose_session_mcp_servers() if self._is_goose else []),
            *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
            *(self._copilot_session_mcp_servers() if self._is_copilot else []),
            *(self._pi_session_mcp_servers() if self._is_pi else []),
            *(await asyncio.to_thread(self._pooled_mcp_servers)),
        ]
        if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
            new_params["mcpServers"] = await self._prune_unstartable_mcp(new_params["mcpServers"])
        # Rebuilt AFTER the array, from the same re-seed: the envelope carries
        # the setting sources the array was authorized under and the inline
        # settings naming the substitute model.
        new_params["_meta"] = self._claude_session_meta()
        self._begin_session_report(new_params.get("mcpServers"))
        self._guard_unresolved_mcp_refs(new_params.get("mcpServers"))
        self._last_substitution_model = None
        retry_id = await self._send_request(METHOD_SESSION_NEW, new_params)
        session_resp = await self._wait_for_response(
            retry_id,
            timeout=_INIT_TIMEOUT,
            method=METHOD_SESSION_NEW,
            expected_mcp=new_params.get("mcpServers"),
        )

    return session_resp


def _initialize_params(self: AcpClient) -> dict[str, Any]:
    """The ``initialize`` request params, one spelling for every handshake.

    Read by :meth:`_initialize_session` and by the entitlement probe's own
    handshake (:meth:`_entitlement_probe_answer`), so the probe process is
    introduced to kiro-cli exactly as the session it probes for was.
    """
    protocol_version: int | str = _PROTOCOL_VERSION_BY_BACKEND.get(self.backend, PROTOCOL_VERSION)
    return {
        "protocolVersion": protocol_version,
        "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
        "clientCapabilities": client_capabilities_for(self.backend),
    }


async def _fork_session(
    self: AcpClient, source_sid: str, message_id: str, capabilities: dict
) -> str | None:
    """Fork *source_sid* up to and including *message_id*: the new sid, or None.

    Only a harness in ``ACP_BACKENDS_FORK_AT_MESSAGE``, at a version that reads
    the ``_meta`` fork point sent here: a fork that ignored it would copy the
    WHOLE session, including everything after *message_id* that the caller is
    discarding.
    """
    session_capabilities = capabilities.get("sessionCapabilities")
    if not (
        self.backend in ACP_BACKENDS_FORK_AT_MESSAGE
        and _adapter_version_at_least(self._agent_version, CLAUDE_ACP_FORK_POINT_MIN_VERSION)
        and isinstance(session_capabilities, dict)
        and isinstance(session_capabilities.get("fork"), dict)
    ):
        return None
    params = {
        "sessionId": source_sid,
        "cwd": await self._session_work_dir(),
        "mcpServers": [],
        "_meta": {"jetbrains": {"air": {"fork": {"version": 1, "messageId": message_id}}}},
    }
    try:
        fork_id = await self._send_request(METHOD_SESSION_FORK, params)
        fork_resp = await self._wait_for_response(fork_id, timeout=_INIT_TIMEOUT)
    except (AcpError, AcpTimeoutError):
        logger.info(
            "session/fork of %s at %s failed, falling back to session/new",
            source_sid,
            message_id,
        )
        return None
    forked_sid = fork_resp.get("sessionId")
    if not isinstance(forked_sid, str) or not forked_sid:
        return None
    logger.info("ACP session forked: %s -> %s at %s", source_sid, forked_sid, message_id)
    return forked_sid


async def _initialize_session(self: AcpClient) -> None:
    """Handshake: initialize → session/load or session/new → set_mode → set_model."""
    # 1. Initialize
    init_id = await self._send_request(METHOD_INITIALIZE, self._initialize_params())
    try:
        init_resp = await self._wait_for_response(init_id, timeout=_INIT_TIMEOUT)
    finally:
        # Copilot CLI reads its launch MCP config when it boots (measured on 1.0.100:
        # the file deleted right after the initialize answer, the stdio server it
        # names still mounted and answered tools/call). It carries a session
        # credential, so it goes now, on a failed handshake too.
        if self._is_copilot:
            await self._discard_copilot_mcp_config()
    logger.info("ACP initialized (protocol=%s)", init_resp.get("protocolVersion"))

    # Whether this harness can restore a session at all, asked of whichever
    # capability the harness advertises it under. Both spellings are standard ACP
    # v1: ``loadSession`` is the flag for ``session/load``, and
    # ``sessionCapabilities.resume`` is the object -- an empty one means supported
    # -- for ``session/resume``. Keyed on the membership set rather than probing
    # both, so a harness cannot be read as restorable through a capability it
    # never advertised and then sent a verb it does not serve.
    capabilities = init_resp.get("agentCapabilities") or {}
    if self.backend in ACP_BACKENDS_RESUME_WITHOUT_LOAD:
        session_capabilities = capabilities.get("sessionCapabilities")
        self._can_load_session = isinstance(session_capabilities, dict) and isinstance(
            session_capabilities.get("resume"), dict
        )
    else:
        self._can_load_session = bool(capabilities.get("loadSession", False))
    # Read on every handshake so a respawn onto a harness that dropped image
    # support cannot keep a stale yes. Only a literal ``true`` counts.
    prompt_capabilities = capabilities.get("promptCapabilities")
    self._agent_accepts_image = (
        isinstance(prompt_capabilities, dict) and prompt_capabilities.get("image") is True
    )
    # No harness this core drives narrows the session MCP array against the
    # advertised ``mcpCapabilities``: every one of them either reads no array
    # or accepts the shapes Crew already sends, opencode being the measured
    # case of the latter (stdio, http and sse alike -- see
    # ``_opencode_session_mcp_servers``). The harness that DOES narrow reads
    # the handshake on the core that drives it, in
    # ``acp.harness.codex.session_mcp_servers``, so nothing is held here.
    self._agent_version = agent_version_from_init(init_resp)
    self._agent_version_read = True
    self._steering_advertised = steering_advertised(init_resp)
    self._note_pi_adapter_version()
    self._note_goose_version()
    # Copilot only: a release other than the verified one is refused here, before
    # any session exists; the AcpToolGateUnroutable it raises ends the child with no
    # respawn (see ``ensure_ready``).
    self._refuse_unverified_copilot_version()

    # The subprocess has now read its agent spec, which closes the window the
    # pre-spawn snapshot opened: a write landing before this point is caught here,
    # and one landing after cannot change what kiro-cli already loaded. BEFORE
    # session/load or session/new, so a session is never created on a spec nobody
    # verified. Off-loop: one stat, and a hash only when the stat differs.
    #
    # Converted to ``AcpError`` for the SAME reason the pre-spawn gate above is:
    # that is the type ``ensure_ready`` handles, and its handler is what ends the
    # child and drops the half-registered session state --
    # ``_cleanup_failed_live_spawn`` kills the process, releases the workspace
    # binding and discards the settings seed, and ``_reset_state`` closes the
    # pipes and clears the PIDs, the session id and the per-spawn spec caches.
    # A raw ``DerivedSpecStale`` (a ``RuntimeError``) matches none of those
    # handlers and would leave a live child running on an unverified spec. The one
    # retry that handler allows is not a way past this: it respawns, so it goes
    # back through the pre-spawn gate and either re-derives from the default as it
    # now stands or refuses again. Nothing continues on this child.
    try:
        await asyncio.to_thread(require_unchanged_derived_spec, self._derived_spec_snapshot)
    except DerivedSpecStale as exc:
        raise AcpError(str(exc)) from exc

    # 2. Try session/load if we have a resume ID and kiro-cli supports it
    self._resumed = False
    resume_sid = self._resume_session_id
    self._resume_session_id = None  # consume — no retry loop
    fork_request = self._fork_request
    self._fork_request = None
    if fork_request and not resume_sid and self._can_load_session:
        # A successful fork is then restored like any resume.
        resume_sid = await self._fork_session(*fork_request, capabilities)
    sent_snapshot: DerivedSpecSnapshot | None = None

    if resume_sid and self._can_load_session:
        # Only attempt session/load when the prior session transcript
        # actually exists on disk. Without this guard a stale persisted SID
        # (e.g. a slot reopened for a brand-new conversation) triggers a
        # session/load that REPLAYS the old transcript on top of the fresh
        # system prompt + memory injection — inflating base context to
        # ~38% on turn 1. kiro-cli stores transcripts at ~/.kiro/sessions/
        # cli/<sid>.json; a missing transcript falls back to session/new
        # (a genuinely fresh start).
        if self.backend in ACP_BACKENDS_HARNESS_OWNED_SESSIONS:
            # The harness keeps its own session records and resolves them from
            # the sessionId, so there is no Crew-side file to name. Gating on
            # the kiro transcript here would make file_ok always False -- such
            # a session could never resume, it would silently start fresh every
            # time -- which is why the _meta block below gives it no
            # session_file either. For claude the SDK transcript-path resolver
            # is the internal companion's; the public core simply attempts the
            # load.
            session_file = ""
            file_ok = True
        else:
            session_file = str(kiro_sessions_dir() / f"{resume_sid}.json")
            file_ok = Path(session_file).exists()
        if file_ok:
            # WHICH restore verb, from the same membership set that chose the
            # capability above. The two calls share a contract --
            # ``ResumeSessionRequest`` carries the same fields as
            # ``LoadSessionRequest``, and their responses carry the same fields --
            # so the params built below and every check after them are unchanged,
            # and only the method name differs. That is why this is a set and a
            # constant rather than a restore path per harness (harness-parity
            # H13).
            #
            # Resolved BEFORE the try, not inside it: the failure log in the
            # handler names this method, and a params-building failure would
            # otherwise reach that log with the name unbound.
            restore_method = (
                METHOD_SESSION_RESUME
                if self.backend in ACP_BACKENDS_RESUME_WITHOUT_LOAD
                else METHOD_SESSION_LOAD
            )
            try:
                load_params: dict = {
                    "sessionId": resume_sid,
                    "cwd": await self._session_work_dir(),
                    # kiro-cli gets its servers via --agent; a session-array
                    # backend must receive them here as well -- a resumed
                    # session re-declares everything Crew mounts on it or comes
                    # back with no tools (see session/new above). Pooled stubs are
                    # re-declared so a resumed session keeps talking to the
                    # broker. Gated per backend, and in-memory here vs
                    # off-loop there, for the same reasons as session/new.
                    "mcpServers": [
                        *(
                            self._claude_session_mcp_servers()
                            if self._is_claude and not getattr(self, "_side_read_only", False)
                            else []
                        ),
                        *(self._opencode_session_mcp_servers() if self._is_opencode else []),
                        *(self._goose_session_mcp_servers() if self._is_goose else []),
                        *(self._deepseek_session_mcp_servers() if self._is_deepseek else []),
                        *(self._copilot_session_mcp_servers() if self._is_copilot else []),
                        *(self._pi_session_mcp_servers() if self._is_pi else []),
                        *(await asyncio.to_thread(self._pooled_mcp_servers)),
                    ],
                }
                if self.backend in ACP_BACKENDS_PRUNE_UNSTARTABLE_MCP:
                    load_params["mcpServers"] = await self._prune_unstartable_mcp(
                        load_params["mcpServers"]
                    )
                if self._is_claude:
                    load_params["_meta"] = self._claude_session_meta()
                elif self.backend not in ACP_BACKENDS_HARNESS_OWNED_SESSIONS:
                    # The kiro family reads its transcript path from _meta. A
                    # harness that owns its sessions reads no _meta of ours,
                    # so it gets neither key rather than a kiro session_file
                    # it would not know what to do with.
                    load_params["_meta"] = {"_kiro.dev/session_file": session_file}
                self._begin_session_report(load_params.get("mcpServers"))
                # A resumed session re-declares everything Crew mounts on it, so it
                # can be short of a referenced server exactly as a fresh one can.
                self._guard_unresolved_mcp_refs(load_params.get("mcpServers"))
                if self._is_pi:
                    await self._republish_pi_mcp_handoff()
                load_id = await self._send_request(restore_method, load_params)
                load_resp = await self._wait_for_response(
                    load_id,
                    timeout=_INIT_TIMEOUT,
                    method=restore_method,
                    expected_mcp=load_params.get("mcpServers"),
                )
                # A ``modes`` block is the shape kiro-cli and claude return on a
                # successful load, and its absence on THOSE harnesses means the
                # load did not really take. A member of
                # ACP_BACKENDS_LOAD_WITHOUT_MODES never returns one, so for it a
                # response that is not an error IS the successful load -- gating
                # it on ``modes`` would fall through to session/new and discard
                # the conversation the harness had just restored.
                if "modes" in load_resp or self.backend in ACP_BACKENDS_LOAD_WITHOUT_MODES:
                    self._session_id = resume_sid
                    self._resumed = True
                    self.last_prompt_stats.restore_cost_session(resume_sid)
                    self._capture_available_models(load_resp)
                    sent_snapshot = self._session_mcp_snapshot
                    if self._uses_advertised_model_selection:
                        await self._persist_advertised_models_if_changed()
                        await self._reseed_after_capture()
                    self._store_session_config(load_resp)
                    logger.info("ACP session resumed: %s", resume_sid)
            except (AcpError, AcpTimeoutError):
                logger.info(
                    "%s failed for %s, falling back to session/new",
                    restore_method,
                    resume_sid,
                )
            else:
                # The ``else`` of the try, so this runs ONLY on a load that did not
                # raise -- and OUTSIDE it, so the refusal below cannot be caught as
                # a failed load. Gated here rather than inside the method so the
                # first-class kiro path gains no call and no failure point in
                # service of an adapter (harness-parity H13).
                #
                # A RESTORED session carries the mode it was left in, which the
                # environment seed does not re-apply, so this is what keeps a
                # resumed session from coming back permissive. Conditioned on
                # ``_resumed`` because a load that returned no ``modes`` block did
                # not take: that path falls through to session/new, which does its
                # own read-back on the response that replaces this one.
                if self._is_goose and self._resumed:
                    self._verify_goose_routing(load_resp)
                if self._is_claude and self._resumed:
                    await self._pin_claude_starting_mode(load_resp)
        else:
            logger.info("Session file missing for %s, skipping load", resume_sid)

    # 3. Create new session if load didn't succeed. On the claude backend a
    # model-substitution advisory comes back as an error frame with NO
    # sessionId; _new_session_following_substitution adopts the substitute
    # model and re-issues once so a real session is actually created.
    if not self._session_id:
        # Capture the requested model before the helper runs. The helper resets
        # self._last_substitution_model = None on every return path, so we can't
        # use it to detect whether substitution happened. Comparing self._model
        # before vs after is the reliable signal.
        model_before = self._model
        session_resp = await self._new_session_following_substitution()
        sent_snapshot = self._session_mcp_snapshot
        self._session_id = session_resp.get("sessionId")
        self.last_prompt_stats.begin_cost_session(self._session_id or "")
        self._capture_available_models(session_resp)
        if self._uses_advertised_model_selection:
            await self._persist_advertised_models_if_changed()
            await self._reseed_after_capture()
        self._store_session_config(session_resp)
        # Before the first prompt, and before the session id is even checked: a
        # session that cannot establish the asking posture is one where none of
        # Crew's tool controls execute. Gated HERE rather than inside the method so
        # the first-class kiro path gains no call and no failure point in service of
        # an adapter (harness-parity H13).
        if self._is_goose:
            self._verify_goose_routing(session_resp)
        if not self._session_id:
            # Both the initial attempt and the substitution retry failed to
            # yield a session. Raise a clear, actionable error instead of
            # letting the next step die on the opaque "Cannot set config
            # option before session is initialized" guard.
            # self._model can be backend-derived (the substitute adopted
            # by _new_session_following_substitution from the ACP advisory),
            # and AcpError chains through to the dashboard activity feed
            # and Slack. Dual-redact before interpolating, same discipline
            # as the logger paths.
            # Standardize redacted-local naming across this file: the
            # convention is _<source>_log so the reader sees what was redacted.
            _model_for_error_log, _ = redact_exfiltration_urls(str(self._model))
            _model_for_error_log, _ = redact_credentials(_model_for_error_log)
            raise AcpError(
                "session/new returned no sessionId"
                + (
                    f" even after adopting substitute model {_model_for_error_log!r}"
                    if self._model != model_before
                    else ""
                )
                + "; the backend did not create a session."
            )
        # self._model can be backend-derived if _new_session_following_substitution
        # adopted the gateway substitute. Redact it consistently with the
        # warning-log path so any URL / credential-shaped substitute id never
        # reaches the dashboard activity feed or Slack unredacted.
        _model_log, _ = redact_exfiltration_urls(str(self._model))
        _model_log, _ = redact_credentials(_model_log)
        logger.info("ACP session created: %s (model=%s)", self._session_id, _model_log)
        if self._is_claude:
            await self._pin_claude_starting_mode(session_resp)
    self._last_activity = time.monotonic()
    # The session exists, so the pi process that read the MCP handoff file has
    # started; the file has no further reader and goes now.
    if self._is_pi:
        await self._discard_pi_mcp_handoff()
    # Copilot CLI read its launch MCP config when its process started; the file
    # carries a session credential and nothing reads it again.
    if self._is_copilot:
        await self._discard_copilot_mcp_config()

    # Seek to end of JSONL so we only read new tool results.
    # claude-agent-acp stores sessions via its own SDK, not ~/.kiro/ — skip.
    if self._session_id and self._is_kiro:
        _jpath = kiro_sessions_dir() / f"{self._session_id}.jsonl"
        try:
            self._jsonl_pos = _jpath.stat().st_size if _jpath.exists() else 0
        except OSError:
            self._jsonl_pos = 0

    # The host has now consumed the ``mcpServers`` array, by whichever route created
    # the session -- ``session/load``, ``session/new``, or the substitution retry --
    # and every route sends the same cached array. For an array-backed host that
    # array IS the derived spec: the child reads no spec of its own, so the
    # ``initialize`` check above (which judges the kiro-cli ``--agent`` read) does
    # not cover it. This is the other end of THAT bracket: a write landing between
    # the array's build and this point is caught, and one landing after cannot
    # change what the host already registered. ``None`` for every agent that
    # mirrors nothing, and for the kiro-cli path, which sends no array. Converted
    # to ``AcpError`` for the reason the two checks above are: that is the type
    # ``ensure_ready`` handles, and its handler is what ends the child and drops
    # the half-registered session state.
    try:
        await asyncio.to_thread(require_unchanged_derived_spec, sent_snapshot)
    except DerivedSpecStale as exc:
        raise AcpError(str(exc)) from exc

    # The session exists and has consumed its array, by whichever route. For a
    # session-array harness, watch in the background for a broker stub it never
    # started (goose drops one silently). Never blocks or fails the start.
    if self.backend in ACP_BACKENDS_SESSION_MCP_ARRAY:
        self._start_mount_check()

    # 4. Activate agent via set_mode -- kiro only. claude-agent-acp does handle
    #    session/set_mode, but its modeIds are permission modes (default,
    #    acceptEdits, plan, ...), never an agent name, so there is no agent to
    #    activate there. _pin_claude_starting_mode uses it for the permission mode.
    #    Guard (A): fire only when the backend advertised this agent, or
    #    advertised no modes at all (older kiro-cli / fake → attempt,
    #    backward-compatible). If modes ARE advertised but this agent is
    #    absent, its ~/.kiro/agents/<agent>.json didn't load — FAIL CLOSED
    #    with an actionable error rather than silently running kiro-cli's
    #    default (broader) mode, which for a restricted agent is a privilege
    #    escalation. Self-heal (B, in _spawn) regenerates the managed default
    #    so the common case never reaches this branch.
    if self._is_kiro:
        # The advertised ids were rewritten to each agent's DECLARED name by
        # the projection's reverse map (frame()). The launch identity is
        # ``self._agent`` -- kiro-cli resolves ``--agent`` by the declared
        # name, so that is the id the start compares against -- while the
        # modeId sent is the same launch identity, which request() tolerates
        # for the agent's own first activation.
        if not self._modes_advertised or self._agent in self._available_mode_ids:
            # The answer is not awaited, so the model this activation puts the
            # session on is judged from its agent/switched frame wherever that
            # frame is read (_note_agent_switched).
            self._activated_model = ""
            self._mode_frame_awaited = True
            self._mode_frame_superseded = False
            await self._send_request(
                METHOD_SET_MODE,
                {"sessionId": self._session_id, "modeId": self._agent},
            )
            logger.info("ACP agent activated: %s", self._agent)
        else:
            cause, remedy = await asyncio.to_thread(unavailable_mode_explanation, self._agent)
            raise AcpError(
                f"Agent mode {self._agent!r} is not available on this session "
                f"(advertised modes: {self._available_mode_ids or 'none'}); {cause} "
                f"Refusing to run the backend default mode in its place. {remedy}"
            )
    else:
        # An adapter's member session is confirmed against its saved spec in the
        # start that created it, so a caller that sends after ensure_ready respawned
        # the child (stream_events, the command paths) never reaches an unconfirmed
        # session. The else of the kiro test above, so the kiro path gains no call
        # and no failure point (harness-parity H13). A refusal is an ``AcpError``,
        # so ensure_ready's handler ends the child and drops the session. A cancel
        # skips that handler, so it is torn down here: otherwise the next
        # ensure_ready would take the warm return into the unconfirmed session.
        try:
            await self.confirm_member_projection()
        except asyncio.CancelledError:
            try:
                await self._cleanup_failed_live_spawn()
            except Exception:
                logger.warning("ACP member-confirm cleanup after a cancel failed", exc_info=True)
            finally:
                self._reset_state()
            raise

    # 5. Set model — override if Kiro Crew config specifies non-default.
    await self._apply_startup_model()

    # 6. Arm permission routing for harnesses whose asking is a session
    #    config option. AFTER the model apply (both write config options, and
    #    the permission one must land last) and before any prompt can run:
    #    _initialize_session is entirely pre-prompt, which is what makes this
    #    placement the guarantee rather than a best effort.
    #    Gated HERE rather than inside the method, so the first-class Kiro path
    #    gains no call, no await and no failure point in service of an adapter
    #    (harness-parity H13). A positive membership test, never "not claude".
    if acp_tool_gate.routing_for(self.backend) is acp_tool_gate.Routing.SESSION_CONFIG:
        await self._apply_session_permission_routing()

    # (settings.local.json is re-seeded up in step 2/3, beside the model-cache
    #  persist, rather than as a step of its own down here: see
    #  _reseed_after_capture on why it rides an EXISTING adapter-only branch.)

    # Frames buffered before the routing write answered can name the state a
    # restored session was left in; the answer supersedes them, so they are not
    # replayed, and everything the drain reads after it is checked below.
    if self.backend in ACP_BACKEND_FORBIDDEN_MODES:
        self._drop_superseded_config_frames()
    # Drain MCP server init notifications
    await self._drain_notifications()
    if self.backend in ACP_BACKEND_FORBIDDEN_MODES:
        await self._refuse_session_config_drift()

    # The drain usually reads step 4's agent/switched frame; judge the model
    # it named before the first prompt can run on it.
    await self._judge_activated_model()


async def ensure_ready(self: AcpClient) -> None:
    """Ensure process is spawned and session is initialized.

    Runs before EVERY prompt, so the warm path must stay syscall-free:
    the work dir is created once per instance (off-loop) and remembered —
    a per-prompt mkdir would tax every prompt with a blocking syscall
    whose latency scales with the parent directory's entry count.
    Re-creating the directory later could not repair a live child anyway:
    a process's cwd is bound to the inode, not the path.
    """
    if (
        platform_compat.IS_WINDOWS
        and self._process
        and (
            self._process.returncode is not None
            or getattr(self, "_windows_tree_cleanup_failed", False) is True
        )
    ) or getattr(self, "_stdin_stalled", False):
        # A stalled child stopped reading stdin with a frame possibly
        # buffered: it is killed BEFORE a replacement starts, so it cannot
        # act on that frame while the new turn runs.
        await self._kill_process(force=True)
        if (
            getattr(self, "_stdin_stalled", False)
            and self._process is not None
            and self._process.returncode is None
        ):
            # Not confirmed dead: stay stalled, so the next ensure_ready
            # retries the kill rather than spawning beside the old child.
            raise AcpProcessDied(
                "the stalled ACP child could not be confirmed dead; "
                "not starting a replacement beside it"
            )
        try:
            await self._discard_claude_settings_seed()
        finally:
            self._reset_state()
    if not self._work_dir_ready:
        await asyncio.to_thread(ensure_directory, self._work_dir)
        self._work_dir_ready = True
    if self._process and self._process.returncode is None and self._session_id:
        if getattr(self, "_activated_model", ""):
            # An agent/switched frame read after the start drain (inside a
            # prompt) is judged here, before the next prompt goes out.
            await self._judge_activated_model()
        return

    # Telemetry (kirocrew.session.startup.duration): time the cold-start work
    # below (spawn + session init) and emit in the finally so every exit path
    # — success, auth-required, error — is measured. The warm fast-path above
    # is intentionally NOT measured (no startup work). Best-effort: a
    # telemetry failure must never affect session startup.
    _startup_t0 = time.monotonic()
    _startup_spawned = False
    # Default "error": any exit that is NOT the explicit success path below
    # — including an unexpected non-Acp exception propagating through the
    # finally — is recorded as a failure, never a false "ready".
    _startup_outcome = "error"
    try:
        # Retry once — kiro-cli first launch can be slow (MCP server init),
        # and transient failures (MCP crash, bad config read) are recoverable.
        for attempt in range(2):
            try:
                if self._process and self._process.returncode is not None:
                    await self._discard_bound_workspace()
                    # `finally`, because the discard is an await and this runs on
                    # cancellable paths: the in-memory reset must land even when
                    # the cancellation arrives during the seed's settle. The
                    # settle itself is shielded, so it completes regardless.
                    try:
                        await self._discard_claude_settings_seed()
                    finally:
                        self._reset_state()

                if not self._process:
                    await self._spawn()
                    _startup_spawned = True

                await self._initialize_session()
                try:
                    await self._snapshot_process_tree()
                except Exception:
                    logger.warning("Failed to snapshot process tree", exc_info=True)

                _startup_outcome = "ready"
                return
            except AcpToolGateUnroutable:
                # Non-retryable BY CONSTRUCTION (see the class docstring): the
                # refusal is a configuration fact, so a respawn re-reads the same
                # answer and refuses again -- a wasted spawn plus teardown that
                # also spends the reconnect budget this DISTINCT type exists to
                # protect. Must sit BEFORE the generic transport handler, because
                # it subclasses AcpError and would otherwise be retried by it,
                # which is the shape that made the distinct type decorative.
                _startup_outcome = "tool_gate_unroutable"
                await self._cleanup_failed_live_spawn()
                self._reset_state()
                raise
            except (AcpTimeoutError, AcpError, OSError) as exc:
                # A sandbox that refused to initialize is DETERMINISTIC, so
                # the second attempt below would rebuild the same profile on
                # the same host and be refused identically -- paying a spawn
                # and a teardown to report the same thing later, with the one
                # line that names the fix buried in a generic exit card. Fail
                # fast with the layer named instead, exactly as the
                # ``AcpToolGateUnroutable`` arm above does for a
                # configuration refusal. Placed inside the generic handler
                # rather than as its own ``except`` clause because the
                # signature is on the CHILD's stderr, not on the exception:
                # the same refusal surfaces as an EOF ``AcpError``, as an
                # ``AcpTimeoutError`` when the child dies before answering
                # ``initialize``, and as an ``OSError`` on the write that
                # follows it.
                sandbox_failure = await self._classify_failed_start(self._sandbox_init_failure)
                if sandbox_failure is not None:
                    _startup_outcome = "sandbox_init_failed"
                    await self._cleanup_failed_live_spawn()
                    self._reset_state()
                    raise sandbox_failure from exc
                # The ``kiro-cli-chat`` delegate would not launch: deterministic
                # for the same reason, and fixed by repairing the Kiro CLI
                # install, so fail fast with the message that says so instead of
                # a second attempt and then the generic process-exited card.
                delegate_failure = await self._classify_failed_start(self._delegate_launch_failure)
                if delegate_failure is not None:
                    _startup_outcome = "delegate_launch_failed"
                    await self._cleanup_failed_live_spawn()
                    self._reset_state()
                    raise delegate_failure from exc
                # The harness answered with its OWN "no provider / not signed
                # in" words (declared in ``host_auth``). Deterministic like the
                # sandbox refusal above: a fresh process reads the same missing
                # configuration, so fail fast with the message that names the
                # fix instead of a retry and then a raw JSON-RPC frame. A plain
                # non-transient ``AcpError`` rather than ``AcpAuthRequired``: the
                # dashboard reads that type as "Kiro is signed out" and would
                # mark a valid kiro-cli login as not ready.
                if isinstance(exc, AcpError) and host_auth.reports_signed_out(
                    self.backend, str(exc)
                ):
                    _startup_outcome = "auth_required"
                    await self._cleanup_failed_live_spawn()
                    self._reset_state()
                    raise AcpError(
                        host_auth.signed_out_message(self.backend),
                        transient=False,
                        user_worded=True,
                    ) from exc
                if attempt == 0:
                    logger.warning("ACP init failed (%s), retrying with fresh process...", exc)
                    await self._cleanup_failed_live_spawn()
                    self._reset_state()
                    if isinstance(exc, OSError):
                        await asyncio.sleep(_ACP_RESPAWN_BACKOFF_S)
                else:
                    # Read the ring BEFORE the cleanup below clears it: a
                    # startup that died with a throttled registration on its
                    # stderr is pre-prompt by construction, so the typed
                    # transient subclass is the accurate verdict here too.
                    _throttled = await self._classify_failed_start(self._registration_throttle_line)
                    # AcpAuthRequired subclasses AcpError; label it distinctly
                    # so a not-logged-in exit is never counted as a generic
                    # startup error. (Only a harness's declared signed-out
                    # phrase fails fast, above; other auth answers keep the retry.)
                    if isinstance(exc, AcpAuthRequired):
                        _startup_outcome = "auth_required"
                    elif _throttled is not None:
                        _startup_outcome = "registration_rate_limited"
                    else:
                        _startup_outcome = "error"
                    await self._cleanup_failed_live_spawn()
                    self._reset_state()
                    if _throttled is not None and not isinstance(exc, AcpAuthRequired):
                        raise registration_rate_limited_error(
                            "ACP session startup failed", _throttled
                        ) from exc
                    raise
    finally:
        try:
            # circular import: importing get_recorder at module top would
            # form config.loader -> acp.types -> acp.client -> metrics.provider
            # -> config.loader (provider reads KiroCrewConfig). Keep it lazy so
            # provider is never loaded during config.loader's import chain.
            from kiro_crew.metrics.provider import get_recorder

            get_recorder().histogram(
                "kirocrew.session.startup.duration",
                (time.monotonic() - _startup_t0) * 1000.0,
                unit="ms",
                attrs={"outcome": _startup_outcome, "spawned": _startup_spawned},
            )
        except Exception:  # never let telemetry break session startup
            logger.debug("session startup metric emit failed", exc_info=True)
