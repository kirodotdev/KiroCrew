"""Own an ``AcpClient`` child process after it is spawned.

The stderr drain, process-tree tracking, kill, failed-start classification, state reset,
the workspace and sandbox discards, re-binding a live process to the session it serves,
and shutdown.

Each function here whose first parameter is ``self`` is an ``AcpClient`` method: the
class binds it by name, so it reads and writes the session's own state on the instance,
and a patch of the method on the class or on an instance reaches every caller.

Instance fields written: ``_background_launches``, ``_between_turn_credits``, ``_bound_workspace_fd``, ``_buffer``,
``_cancel_grace_secs``, ``_cancel_ts``, ``_cancelled``, ``_capability_projection_gaps``,
``_channel_id``, ``_child_pids``, ``_claude_settings_authored``,
``_claude_settings_claim_unrevoked``, ``_claude_settings_shared``,
``_claude_settings_written``, ``_confirmed_capability_session``, ``_consult_future``,
``_copilot_mcp_config_path``,
``_fork_request``, ``_kill_sent``, ``_last_activity``, ``_last_stop_reason``,
``_liveness_oracle``, ``_loaded_capability_template``, ``_mcp_ref_spec``,
``_mcp_report``, ``_mcp_report_frame_floor``, ``_member_stub_allowlist``,
``_oauth_emitted_servers``,
``_opencode_denies_in_force``, ``_opencode_denies_unenforced``,
``_opencode_native_mounts``, ``_pending_oauth_requests``,
``_permission_surface_share_validated``, ``_pi_mcp_handoff``, ``_pi_mcp_server_names``,
``_pid``, ``_process``, ``_process_instance``, ``_process_tree_confirmed_dead``,
``_prompt_or_tool_seen``, ``_resume_session_id``, ``_resumed``, ``_sandbox_cleanup``,
``_session_agent_spec``, ``_session_harness_deny_rules``, ``_session_id``,
``_session_key``, ``_session_mcp_cache``, ``_session_mcp_snapshot``,
``_session_mcp_unhonoured``, ``_spawn_start_token``, ``_spawn_work_dir``,
``_spec_denied_tools``, ``_spec_zero_tools``, ``_stderr_lines``, ``_stderr_task``,
``_stdin_stalled``, ``_windows_tree_cleanup_failed``.

Instance fields read: ``_entitlement_probe_inflight``, ``_mcp_gateway_socket``,
``_native_skill_projection``, ``_sandbox_hidden_dirs``, ``_sandbox_mode``,
``_sandbox_wrapped_by_crew``, ``_seed_owner``, ``_start_time``, ``_stub_session_token``,
``_work_dir``, ``last_prompt_stats``, ``memory_mode``.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable, TypeVar

from kiro_crew import platform_compat
from kiro_crew.acp import runtime_process_tree, seed_provenance
from kiro_crew.acp._dispatch import BackgroundLaunchRecord
from kiro_crew.acp.client_spawn_env import KIRO_CLI_BIN
from kiro_crew.acp.client_timeouts import (
    _CANCEL_GRACE_SECS,
    _SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS,
)
from kiro_crew.acp.liveness import LivenessOracle, _consume_future_exception
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime_process_tree import ChildRecord
from kiro_crew.acp.transport_errors import (
    AcpDelegateLaunchFailed,
    AcpError,
    AcpSandboxInitFailed,
    is_delegate_launch_failure_output,
    is_sandbox_init_failure_output,
    registration_throttle_line,
    sandbox_init_failure,
)
from kiro_crew.acp.transport_framing import settle_drain
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway.claim import schedule_claim
from kiro_crew.sandbox import (
    BoundWorkspaceMismatch,
    release_bound_agent_workspace,
    resolve_bound_session_workspace,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.session_pid import _replace_child_pids
from kiro_crew.session_token_sig import schedule_session_token_publish

if TYPE_CHECKING:
    from kiro_crew.acp.client import AcpClient

#: The client's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.client")


_T = TypeVar("_T")


# High-frequency, content-free adapter stderr diagnostics that _drain_stderr()
# drops instead of forwarding as per-line WARNINGs.  The driving case is the
# claude-agent-acp "Unexpected case: {...thinking_tokens...}" line.  Mechanism
# (confirmed by reading the vendored adapter, dist/acp-agent.js): the backend
# emits a `system` message with subtype `thinking_tokens`, but the adapter's
# `switch (message.subtype)` enumerates ~18 known subtypes (init, status,
# compact_boundary, memory_recall, api_retry, ...) and routes anything else to
# `default: unreachable(message)`, which does `logger.error("Unexpected case:
# " + JSON.stringify(message))` to stderr — one line per token delta.  Measured
# at ~10 lines/sec during active thinking (one line per 2-4 thinking tokens; the
# payload is only estimated_tokens/_delta/uuid/session_id — no response content,
# so dropping them loses nothing).
#
# This is a forward-compat gap in the adapter, NOT new behavior in a specific
# backend build: the `thinking_tokens` event is present in both 2.1.165.357
# and 2.1.168.358 (verified by string-matching both bundled `claude` binaries —
# identical occurrences), so it predates the .168 update that drew attention to
# it.  Exactly when it began appearing in our logs is unconfirmed.  The cleaner
# long-term fix is upstream (add a `thinking_tokens` case to the adapter / bump
# the vendored version); this filter is the version-agnostic stopgap that also
# absorbs the next unenumerated subtype's flood.
#
# Why drop rather than just downgrade the level:
#   1. Log hygiene — gateway.log uses a RotatingFileHandler(maxBytes=2MB,
#      backupCount=3) (see cli_logging.py), so a sustained burst rolls genuine
#      diagnostics out of the retained 8MB window.
#   2. Event-loop load — the file handler is a plain *synchronous* handler and
#      _drain_stderr runs as a task on the gateway event loop, so each forwarded
#      line costs a synchronous file write + two regex redaction passes on the
#      same loop that streams responses.  Per-session the cost is small; it
#      compounds across concurrent thinking sessions.
# (This is a log-volume / event-loop-load reduction, NOT a fix for any
# turn-stall or "agent not responding" symptom — no such causal link was
# established.)
#
# Match on a stable substring (not the full JSON) so the filter survives field
# changes, and keep the tuple NARROW so a genuine error line is never silently
# swallowed.
_SUPPRESSED_STDERR_MARKERS = ("thinking_tokens",)


def _is_process_alive(self: AcpClient) -> bool:
    return self._process is not None and self._process.returncode is None


def is_responsive(self: AcpClient, stale_threshold: float = 600.0) -> bool:
    """True if process is alive AND has had I/O activity within threshold seconds."""
    if not self._is_process_alive() or getattr(self, "_stdin_stalled", False):
        return False
    return (time.monotonic() - self._last_activity) < stale_threshold


def touch_activity(self: AcpClient) -> None:
    """Refresh _last_activity without I/O. Used by long-running MCP tools
    (e.g. the `wait` tool) to prevent is_responsive() from flagging a
    deliberately-idle session as stale and triggering SIGTERM."""
    self._last_activity = time.monotonic()


def set_resume_session_id(self: AcpClient, sid: str) -> None:
    """Set a kiro-cli session ID to restore via session/load on next ensure_ready()."""
    self._resume_session_id = sid


def set_fork_request(self: AcpClient, source_sid: str, message_id: str) -> None:
    """Start the next ensure_ready() from a fork of *source_sid* cut at *message_id*.

    Consumed by that handshake whether or not the fork succeeds; when it does
    not, the handshake falls back to session/new.
    """
    self._fork_request = (source_sid, message_id)


def rekey(
    self: AcpClient,
    session_key: str,
    channel_id: str | None = None,
    crew_agent: str = "",
    watchdog: object | None = None,
) -> None:
    """Re-key this client for a different session (used by warm pool).

    ``crew_agent`` and ``watchdog`` exist only for signature parity with
    AcpSessionProvider.rekey (session_allocation_parts/get_or_create.py calls
    provider.client.rekey uniformly): this client's dispatch loop carries no
    per-agent watchdog snapshot, so both are accepted and deliberately not
    stored."""
    self._session_key = session_key
    self._channel_id = channel_id
    self._last_activity = time.monotonic()
    # The prompt stats' context fields describe whatever this runtime did
    # BEFORE the handoff — carry_over() deliberately preserves them across
    # turn boundaries, so without this reset a recycled runtime hands its
    # previous session's context_pct to the new chat and the first
    # check_context_usage() compacts an empty conversation.
    self.last_prompt_stats.reset_context_state()
    # Credits held between turns belong to the session served before the
    # claim; the claiming session's first turn must not bill them.
    self._between_turn_credits = 0.0
    # Claim-push: tell gatewayd this runtime PID now belongs to
    # ``session_key`` so every MCP stub connection under it carries the
    # right ``_meta.caller`` immediately — event-driven replacement for
    # the stub-side recaller poll (whose bounded budget stranded pool
    # runtimes claimed late). Fire-and-forget; no-ops without a gateway
    # socket or a live process.
    schedule_claim(
        self._mcp_gateway_socket,
        self._process.pid if self._process else None,
        session_key,
        channel_id,
        self._stub_session_token,
    )
    # The token deliberately SURVIVES a rekey (see __init__: a fresh one would
    # leave the live MCP children carrying a name no claim will ever mention
    # again), so the signed mapping is what has to move to the claiming
    # session. Without this, a child spawned for the previous session resolves
    # its token to that session's key until the first turn republishes — and
    # the claiming session's first tool call is exactly what happens in
    # between. Fire-and-forget; offloads its own file I/O.
    schedule_session_token_publish(self._stub_session_token, session_key)


def reclaim(self: AcpClient) -> None:
    """Re-push this session's claim, naming it by its stub token.

    Idempotent: gatewayd skips a connection whose caller is already this
    session, so the steady-state effect is refreshing the token binding.
    That refresh is the point — the binding lives only in the daemon's
    memory, so a gatewayd respawn under a live session leaves its stubs
    holding a token nothing names, and a token nothing names is refused
    rather than resolved from the process tree. Called at the start of every
    turn by the shared identity publisher, so a restart costs at most the
    turn it happened in.

    No-ops without a token (no stubs injected), without a socket, or without
    a live process — the same preconditions ``schedule_claim`` enforces.
    """
    if not self._stub_session_token:
        return
    schedule_claim(
        self._mcp_gateway_socket,
        self._process.pid if self._process else None,
        self._session_key or "",
        self._channel_id,
        self._stub_session_token,
    )


# ── Process Management ──


def _discard_sandbox_cleanup(self: AcpClient) -> None:
    """Unlink and forget the sandbox temp file allocated by ``wrap_argv``.

    wrap_argv writes a launcher/profile file that the spawned child
    consumes at exec. Once no child will exec it — the spawn failed, was
    cancelled, or the process is being reset — it must be removed here, or
    each attempt leaks one file into the temp dir for the gateway's
    lifetime (nothing else references the path after ``_spawn`` reassigns
    ``self._sandbox_cleanup``).
    """
    if self._sandbox_cleanup:
        try:
            os.remove(self._sandbox_cleanup)
        except OSError:
            pass
        self._sandbox_cleanup = None


async def _discard_bound_workspace(self: AcpClient) -> None:
    """Close the parent copy of a macOS workspace identity off-loop."""
    descriptor = getattr(self, "_bound_workspace_fd", None)
    self._bound_workspace_fd = None
    work_dir = getattr(self, "_work_dir", None)
    if work_dir is not None:
        self._spawn_work_dir = str(work_dir)
    if descriptor is not None:
        await release_bound_agent_workspace(descriptor)


async def _session_work_dir(self: AcpClient) -> str:
    """Return the ACP cwd backed by the process's bound directory identity.

    Unbound -- every platform but macOS, where nothing binds -- this is the
    spawn's own pathname and there is nothing to re-check.

    Bound, the descriptor is re-verified and the peer receives the DESCRIPTOR's
    own name. The rule itself is ``sandbox.resolve_bound_session_workspace``,
    shared with ``AcpRuntime._session_work_dir`` so the two halves of this
    boundary cannot drift; only the error type is this front end's. Fails the
    session rather than falling back to the bind-time spelling; see the runtime's
    method for the residual limit a string cannot close.
    """
    if self._bound_workspace_fd is None:
        return self._spawn_work_dir
    try:
        return await resolve_bound_session_workspace(self._bound_workspace_fd, self._spawn_work_dir)
    except BoundWorkspaceMismatch as exc:
        raise AcpError(
            "A delegated macOS Kiro process is bound to one exact workspace; "
            "respawn it bound to the requested workspace"
        ) from exc
    except OSError as exc:
        raise AcpError("Cannot verify the macOS session workspace binding") from exc


async def _cleanup_failed_live_spawn(self: AcpClient) -> None:
    """Kill a failed live child and always release its workspace binding."""
    try:
        await self._kill_process(force=True)
    finally:
        await self._discard_bound_workspace()
        # A failed startup that already wrote the seed owns one too, and the
        # session it belonged to is over -- so it is discarded on exactly the
        # terms a graceful shutdown uses. In the `finally` for the same reason
        # the caller puts `_reset_state` in one: this is the last chance.
        await self._discard_claude_settings_seed()


async def _to_thread_guarding_sandbox(
    self: AcpClient, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> _T:
    """``asyncio.to_thread`` that discards the sandbox file on failure.

    After ``wrap_argv`` has allocated the sandbox temp file, every
    suspension point before the exec is a leak window: a cancellation
    (turn cancel, session close, shutdown) unwinds ``_spawn`` without
    reaching ``_reset_state``, orphaning the file. Route any offload in
    that window through here so the file is removed before re-raising.
    """
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except BaseException:
        self._discard_sandbox_cleanup()
        raise


async def _drain_stderr(
    self: AcpClient, stderr: asyncio.StreamReader, *, label: str = KIRO_CLI_BIN
) -> None:
    # Count of suppressed high-frequency marker lines (see
    # _SUPPRESSED_STDERR_MARKERS) and the monotonic timestamp of the last
    # throttled summary, so a thinking burst is observable in the log
    # without re-introducing the per-delta flood it replaced.
    suppressed = 0
    last_summary = time.monotonic()
    while True:
        line = await stderr.readline()
        if not line:
            break
        text = line.decode(errors="replace").strip()
        if not text:
            continue
        # Liveness must advance for EVERY line, including suppressed ones:
        # the adapter is provably alive while emitting them, and the idle
        # watchdog (is_responsive) must not kill an actively-thinking turn.
        # One monotonic read, reused for the throttle check below.
        now = time.monotonic()
        self._last_activity = now
        if any(marker in text for marker in _SUPPRESSED_STDERR_MARKERS):
            # Drop the line: no per-occurrence WARNING, and crucially do not
            # append to the bounded _stderr_lines ring buffer — otherwise a
            # thinking burst evicts the last real errors from diagnostics.
            suppressed += 1
            if now - last_summary >= _SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS:
                logger.debug("suppressed %d adapter stderr marker line(s)", suppressed)
                suppressed = 0
                last_summary = now
            continue
        if self.memory_mode == "persistent":
            self._stderr_lines.append(text)
            redacted, _ = redact_exfiltration_urls(text)
            redacted, _ = redact_credentials(redacted)
            logger.warning("%s stderr: %s", label, redacted)
    if suppressed:
        # Flush the residual count once the stream closes so the final burst
        # is still accounted for.
        logger.debug("suppressed %d adapter stderr marker line(s)", suppressed)


async def _snapshot_process_tree(self: AcpClient) -> None:
    """Discover and track the full process tree after MCP servers are loaded.

    Merges with any early snapshot taken in _spawn().  MCP servers
    (the internal MCP server, node) may not exist until after _initialize_session().
    """
    _loop = asyncio.get_running_loop()
    descendants = await _loop.run_in_executor(
        subprocess_executor(), runtime_process_tree._get_child_pids, self._pid
    )
    if not descendants:
        # Retry once — children may not have forked yet
        await asyncio.sleep(0.5)
        descendants = await _loop.run_in_executor(
            subprocess_executor(), runtime_process_tree._get_child_pids, self._pid
        )

    new_pids = [p for p in descendants if p not in self._child_pids]
    if new_pids:
        self._child_pids.update(
            await _loop.run_in_executor(
                subprocess_executor(), runtime_process_tree._capture_child_records, new_pids
            )
        )

    if self._child_pids:
        from kiro_crew.session import _track_child_pids

        # Exclusive file lock + read-modify-append + per-child start-id
        # reads -- blocking syscalls that must not run on the event loop.
        # Ride the same executor as the twin call site in _spawn().
        await _loop.run_in_executor(
            subprocess_executor(),
            functools.partial(_track_child_pids, self._child_pids, parent_pid=self._pid or 0),
        )
        logger.info("Tracked %d descendant PIDs for PID %d", len(self._child_pids), self._pid)


async def refresh_process_tree(self: AcpClient) -> None:
    """Record the descendants the live leader has started since the last snapshot.

    The startup snapshot runs once, after session initialise; a turn that
    runs a command which forks its own children adds processes it never
    saw. A teardown takes its kill handle from ``_child_pids``, and a
    leaderless group may only be signalled while a RECORDED member vouches
    for it (``process_identity._group_member_vouches``), so a group only
    late children keep alive was reported and never reaped. Teardown calls
    this just before it takes the handle.

    Walks only a leader that is ours and alive: the asyncio process has no
    return code yet AND the pid still reads back the recorded start id, both
    before the walk and after it. A pid recycled during the walk would name
    another tree, so the new records are dropped then. A dead leader gets no
    walk: its children are reparented and out of its reach. Each new child
    is kept only when a second walk still finds it under the leader and its
    start id still reads back as the one captured, and it is persisted with
    that captured id (``_replace_child_pids``), never re-read at write time.
    Never removes a record. A recorded pid whose start id fails to read
    back is captured again (the leader's tree reused the number), and the
    in-memory record is updated only if the client still holds the same
    process after the write.

    The walk is :func:`runtime_process_tree.guarded_tree_pass` in add-only
    mode, the same pass ``AcpRuntime._snapshot_descendants`` runs in replace
    mode. It stays add-only: the teardown checks each recorded child's
    identity before it signals one (``_kill_escaped_children``), so a pid
    that left is skipped there rather than dropped here.
    """
    # The client's own process object, not a shared runtime: one client
    # owns exactly one process, and that object is what is compared after
    # the walk to tell a respawn apart.
    process = self._process
    start = self._start_time
    if process is None or process.returncode is not None or start is None:
        return
    pid = process.pid
    if not pid:
        return

    def _leader_holds() -> bool:
        return self._process is process and platform_compat.get_process_start_id(pid) == start

    result = await runtime_process_tree.guarded_tree_pass(
        pid,
        self._child_pids,
        probes=runtime_process_tree.TreeProbes(
            walk=runtime_process_tree._get_child_pids,
            capture=runtime_process_tree._capture_child_records,
            start_id=platform_compat.get_process_start_id,
            write=_replace_child_pids,
        ),
        executor=subprocess_executor(),
        root_holds=_leader_holds,
        replace=False,
        # The write awaited: a reset or respawn may have replaced the process
        # meanwhile, and its record must not gain the previous one's children.
        still_owner=lambda: self._process is process,
    )
    if result.outcome == "write_failed":
        logger.warning(
            "Process tree refresh for PID %d: tracking write failed; %d child(ren) left "
            "unrecorded",
            pid,
            len(result.confirmed),
        )
        return
    if result.outcome != "written":
        return
    self._child_pids.update(result.confirmed)
    logger.info(
        "Refreshed process tree for PID %d: %d new descendant(s)", pid, len(result.confirmed)
    )


async def _kill_process(self: AcpClient, *, force: bool = False) -> None:
    """Kill the subprocess and wait for it to exit.

    Uses process groups (killpg) for clean tree kill, then sweeps
    child PIDs that escaped to a different PGID.

    Args:
        force: If True, kill immediately (used during shutdown).
    """
    if self._process and platform_compat.IS_WINDOWS:
        self._windows_tree_cleanup_failed = True
        await platform_compat.terminate_windows_asyncio_tree(self._process)
        self._windows_tree_cleanup_failed = False
        return
    if not self._process or self._process.returncode is not None:
        return
    pid = self._pid
    if pid is None:  # narrow for mypy — set at _spawn time under the process guard
        return
    # Close pipes first to unblock any pending reads/writes
    for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
        if pipe:
            try:
                pipe.close()  # type: ignore[union-attr]
            except Exception:
                pass

    # Snapshot child PIDs before killing — children in different
    # process groups survive killpg (kiro-cli-chat acp leak).
    # Merge stored snapshot (from init, catches reparented-to-init PIDs)
    # with fresh scan (catches children spawned after init).
    _loop = asyncio.get_running_loop()
    fresh = await _loop.run_in_executor(
        subprocess_executor(), runtime_process_tree._get_child_pids, pid
    )
    stored = self._child_pids
    # Build merged dict: pid → (start_time, basename) (stored has both, fresh needs capture)
    merged: dict[int, ChildRecord] = dict(stored)
    new_pids = [p for p in fresh if p not in merged]
    if new_pids:
        # capture (start_time, basename) off-loop — on macOS these spawn `ps`
        merged.update(
            await _loop.run_in_executor(
                subprocess_executor(), runtime_process_tree._capture_child_records, new_pids
            )
        )

    if not force:
        try:
            # POSIX: killpg(getpgid) tears down the whole group (setsid at
            # spawn). Windows: taskkill /T /F walks the child tree instead
            # (no process groups) — platform_compat dispatches both. Async
            # variant offloads the Windows taskkill spawn to
            # subprocess_executor so the event loop keeps ticking while
            # taskkill.exe runs.
            self._kill_sent = (int(platform_compat.SIGTERM), "stop requested")
            logger.info("Sending SIGTERM to ACP PID %s (reason: stop requested)", pid)
            await platform_compat.kill_process_tree_async(pid, platform_compat.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(self._process.wait(), timeout=3.0)
            # _kill_escaped_children -> _is_our_child -> _get_start_time/
            # _read_basename spawn `ps` on macOS; run it off the loop.
            await _loop.run_in_executor(
                subprocess_executor(), runtime_process_tree._kill_escaped_children, merged
            )
            return
        except asyncio.TimeoutError:
            pass
    # Force kill (async variant offloads Windows taskkill).
    reason = "forced stop" if force else "SIGTERM timed out"
    self._kill_sent = (int(platform_compat.SIGKILL), reason)
    logger.info("Sending SIGKILL to ACP PID %s (reason: %s)", pid, reason)
    try:
        await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
    except (ProcessLookupError, OSError):
        try:
            self._process.kill()
        except (ProcessLookupError, OSError):
            pass
    await _loop.run_in_executor(
        subprocess_executor(), runtime_process_tree._kill_escaped_children, merged
    )
    try:
        await asyncio.wait_for(self._process.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        logger.warning("PID %s did not exit after force kill", pid)


def _reset_state(self: AcpClient) -> None:
    """Reset all session state (call after process is dead)."""
    cleanup = getattr(self._process, "_windows_cleanup_state", None)
    if platform_compat.IS_WINDOWS and isinstance(
        cleanup, platform_compat._PendingWindowsTreeCleanup
    ):
        if not cleanup.retired:
            self._windows_tree_cleanup_failed = True
    if getattr(self, "_windows_tree_cleanup_failed", False) is True:
        logger.warning("Retaining Windows client PID %s after incomplete tree cleanup", self._pid)
        return
    if self._process:
        for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
            if pipe:
                try:
                    pipe.close()  # type: ignore[union-attr]
                except Exception:
                    pass
    # Clean up sandbox temp files (macOS seatbelt profile)
    self._discard_sandbox_cleanup()
    # The process this projection's views were prepared for is dead, so its
    # lease goes now and this home's unleased views are pruned at once,
    # rather than whenever the object is collected or the next spawn prunes
    # (the Kiro IDE lists every view left in the agents directory). The
    # object stays: a frame still being drained may name one of its aliases.
    # A respawn prepares a fresh projection. getattr: clients built without
    # __init__ in tests.
    projection = getattr(self, "_native_skill_projection", None)
    if projection is not None:
        try:
            projection.release()
        except Exception:
            logger.debug("skill projection release failed", exc_info=True)
    # The pi MCP handoff file of a spawn that never reached an established
    # session: one unlink, the same shape as the sandbox file above.
    handoff_path, _payload = getattr(self, "_pi_mcp_handoff", ("", b""))
    self._pi_mcp_handoff = ("", b"")
    self._pi_mcp_server_names = ()
    if handoff_path:
        try:
            os.remove(handoff_path)
        except OSError:
            pass
    # The Copilot CLI launch MCP config of a spawn that never reached initialize
    # (a spawn error after resolve_spawn wrote it): same shape, one unlink.
    copilot_config = getattr(self, "_copilot_mcp_config_path", "")
    self._copilot_mcp_config_path = ""
    if copilot_config:
        try:
            os.remove(copilot_config)
        except OSError:
            pass
    # The settings.local.json THIS session seeded is removed by
    # _discard_claude_settings_seed, which every caller awaits in the `try` of
    # the `finally` that reaches here -- it is async because the ownership hash,
    # the durable revoke and the unlink are all blocking, and this method is
    # synchronous and runs on the event loop. That pairing also means this may
    # run because the discard's await was CANCELLED: the flags below are already
    # cleared by then, and the shielded transaction still finishes the disk
    # half. getattr: _reset_state runs on clients built without __init__ in
    # tests, and such a client has no work_dir to derive the seed's path from.
    owner = getattr(self, "_seed_owner", "")
    if getattr(self, "_claude_settings_authored", False):
        self._claude_settings_authored = False
        self._claude_settings_written = None
    self._claude_settings_claim_unrevoked = False
    if getattr(self, "_work_dir", None) is not None:
        path = self._claude_local_settings_path()
        # The seed itself and its durable holder are removed by
        # ``_discard_claude_settings_seed``, which every production caller
        # awaits off the loop before reaching here. This synchronous fallback
        # hands back only the in-memory slot, taking no lock and doing no I/O,
        # so an interrupted discard cannot leave the event loop blocked.
        #
        # Unconditional, not gated on the instance flags: ``release_local`` is
        # a no-op unless ``_LIVE[key] == owner``, so it never touches a sibling
        # client object's lease held under a different token. It covers both
        # refused durable withdrawals -- the authored discard whose ``forget``
        # and ``release`` were refused with the seed restored, and the
        # un-revoked hand-back whose retry was refused -- and the authored one
        # is the case a flag-gated drop leaks: the refused ``release`` puts the
        # in-memory slot back while the discard's ``finally`` has already
        # cleared authorship, so nothing flag-gated runs and every later
        # session on this work_dir in this process is wedged
        # (``held_by_another`` and ``claim`` consult that slot first). The
        # persisted holder is the half this cannot reach: it carries this
        # process's live identity, so it stands until this process exits.
        seed_provenance.release_local(path, owner)
        # A sharer never wrote the file and never held the live claim; its
        # whole teardown is withdrawing the shared-reader registration, so
        # the owner's teardown (or a later adoption) stops holding the file
        # for a reader that is gone.
        if getattr(self, "_claude_settings_shared", False):
            seed_provenance.unshare_local(path, owner)
    # The trailing plain assignments are unconditional so a client built
    # without __init__ in tests resets clean too.
    self._claude_settings_shared = False
    self._permission_surface_share_validated = False
    # Drop the translated MCP array: the spec is read PER SPAWN, which is what
    # lets installing or toggling a server take effect on the next session, so
    # a replacement process must not inherit this one's snapshot.
    self._session_mcp_cache = None
    self._session_mcp_snapshot = None
    self._session_agent_spec = None
    self._member_stub_allowlist = None
    self._loaded_capability_template = ""
    self._capability_projection_gaps = ()
    # Per SESSION, not per spawn: a replacement process' session is judged on its
    # own consumed spec, even when it resumes the same id.
    self._confirmed_capability_session = ""
    # Same per-spawn freshness rule as the array above: an edited spec must be
    # what the next session's guard judges, not this one's.
    self._mcp_ref_spec = None
    self._spec_denied_tools = frozenset()
    self._spec_zero_tools = False
    self._session_mcp_unhonoured = frozenset()
    self._session_harness_deny_rules = ()
    self._opencode_denies_in_force = None
    self._opencode_denies_unenforced = frozenset()
    self._opencode_native_mounts = frozenset()
    # Save PID state before clearing it. A root is confirmed exited only
    # when its own Process reports a reaped return code; a missing or
    # unreadable PID is not enough to reclaim its working directory.
    root_confirmed_dead = bool(self._process and self._process.returncode is not None)
    saved_pid = None if platform_compat.IS_WINDOWS else self._pid
    saved_start_token = self._spawn_start_token
    saved_child_pids = self._child_pids
    self._process = None
    self._pid = None
    self._stdin_stalled = False
    self._spawn_start_token = None
    # The instance id names the process that just ended; a replacement spawn
    # mints its own, so nothing may keep answering with this one in between.
    self._process_instance = ""
    # A walk wedged on the dead PID's /proc entry can never speak for the
    # replacement process, so release it with the oracle it sampled into.
    self._retire_liveness_state()
    self._session_id = None
    # The launches this record holds died with the process whose harness
    # started them (a backgrounded command or Workflow runs in that
    # process's own tree). Carrying it across a respawn would grant the
    # FRESH tree the watchdog's background-work hold — and its hard-ceiling
    # grace — on behalf of work that is already dead. "Never reset per
    # turn" (see __init__) is a statement about turns, not processes.
    self._background_launches = BackgroundLaunchRecord()
    # The cost baseline is deliberately left as it is. What the replacement
    # process's counter starts from depends on how its session opens:
    # session/new starts a fresh ledger at zero (begin_cost_session), while a
    # restore of this same session brings the old ledger back from the
    # harness transcript (restore_cost_session keeps the baseline). Zeroing
    # here re-billed the whole lifetime on the first turn after every resume.
    self._buffer.clear()
    self._stderr_lines.clear()
    # A fresh process opens a fresh registration window: cleared WITH the
    # ring, so the latch and the evidence it gates always describe the same
    # child.
    self._prompt_or_tool_seen = False
    if self._stderr_task and not self._stderr_task.done():
        self._stderr_task.cancel()
    self._stderr_task = None
    self._cancelled = False
    self._cancel_ts = 0.0
    self._cancel_grace_secs = _CANCEL_GRACE_SECS
    self._resumed = False
    # _turn_done is deliberately NOT rebuilt here. Its state is the truth
    # about the TURN, not the process: the prompt entries clear() it before
    # ensure_ready(), and ensure_ready() reaches this reset on the
    # dead-process respawn branch -- rebuilding the Event there (set OR
    # unset) would misreport the whole respawned turn. Cleared stays
    # cleared (turn in flight, the shutdown drain must wait for it); set
    # stays set (idle, see __init__). Waiters on the old object also keep
    # their Event instead of being orphaned.
    self._last_stop_reason = ""
    self._pending_oauth_requests.clear()
    self._oauth_emitted_servers.clear()
    # A retired session's report must not be read as the next one's. This
    # view exists to be evidence, and stale evidence is worse than none:
    # the replacement process re-initializes its servers from scratch and
    # will report again.
    self._mcp_report = McpSessionReport()
    #: Index into ``_mcp_notifications`` below which frames belong to a PRIOR
    #: session attempt and must not reach the report. See
    #: ``_begin_session_report``.
    self._mcp_report_frame_floor = 0
    # Untrack PIDs from the orphan tracking files — but ONLY those confirmed
    # dead. A child or root still alive after teardown survived the kill
    # (killpg only reaches the kiro-cli process group, so children in other
    # groups can outlive it, and a mid-init crash can race the descendant
    # scan in _kill_process()). Retaining a survivor's entry keeps it visible
    # to the periodic orphan sweep and next-startup cleanup_orphaned_sessions(),
    # which reap it; untracking one would orphan it permanently (all sweep
    # mechanisms key off these files) — the memory-leak this guards against.
    # These untrack helpers live in kiro_crew.session, which imports this
    # module transitively, so they must be imported inline.
    from kiro_crew.session import _untrack_child_pids, _untrack_pid, _untrack_session_pid
    from kiro_crew.session_pid import (
        _pid_gone_or_unmanaged,
        _untrack_pid_if_dead,
        _untrack_root_by_identity,
    )

    survivors: list[int] = []
    if saved_child_pids:
        dead_children = {
            pid: rec for pid, rec in saved_child_pids.items() if _pid_gone_or_unmanaged(pid)
        }
        survivors = [p for p in saved_child_pids if p not in dead_children]
        if dead_children:
            try:
                _untrack_child_pids(dead_children)
            except Exception:
                logger.debug("untracking child PIDs %s failed", list(dead_children), exc_info=True)
        if survivors:
            logger.warning(
                "Retained tracking for %d live child PID(s) that survived "
                "teardown; orphan sweep will reap them: %s",
                len(survivors),
                survivors,
            )
    # Untrack parent kiro-cli PID (only if confirmed dead) -- by IDENTITY,
    # the way AcpRuntime retires on both its paths. "Confirmed dead" is a
    # fact about the process, not about its number: the kernel can hand the
    # number to a root spawned since, and a prefix-matched untrack would take
    # the successor's lines with it. The token read at spawn names the line
    # that is ours; the bare line goes only while the number is dead at that
    # moment. A root whose identity could not be read at spawn keeps the
    # prefix-matched untrack it always had.
    if saved_pid is not None:
        if _pid_gone_or_unmanaged(saved_pid):
            try:
                if saved_start_token:
                    if not _untrack_root_by_identity(saved_pid, saved_start_token):
                        _untrack_pid_if_dead(saved_pid)
                else:
                    _untrack_pid(saved_pid)
                    _untrack_session_pid(saved_pid)
            except Exception:
                logger.debug("untracking PID %s failed", saved_pid, exc_info=True)
        else:
            logger.warning(
                "Retained tracking for live root PID %s that survived "
                "teardown; orphan sweep will reap it",
                saved_pid,
            )
    self._child_pids = {}
    self._process_tree_confirmed_dead = root_confirmed_dead and not survivors


async def _classify_failed_start(self: AcpClient, classify: Callable[[], Awaitable[_T]]) -> _T:
    """Run one of ``ensure_ready``'s failure-arm classifiers, cleaning up on a cancel.

    Each classifier settles the stderr drain first, which waits, and a cancel
    landing in that wait propagates (see :func:`settle_drain`). The failure arm
    has already decided to discard this child by then, and leaving it would
    keep a live process with a half-built session that the next
    ``ensure_ready`` takes the warm path straight back onto. So the cleanup the
    arm would have run runs before the cancel goes on, with the reset in a
    ``finally`` so a second cancel during the kill still clears the state. A
    cleanup failure is logged rather than raised: it must not replace the
    cancel, which is what the caller is owed.
    """
    try:
        return await classify()
    except asyncio.CancelledError:
        try:
            await self._cleanup_failed_live_spawn()
        except Exception:
            logger.warning("ACP failed-start cleanup after a cancel failed", exc_info=True)
        finally:
            self._reset_state()
        raise


async def _settle_stderr(self: AcpClient, timeout: float = 0.5) -> None:
    """Bounded wait for the stderr drain, so its ring can be read consistently.

    Same budget as the EOF branch of :meth:`_read_message`, lifted here because
    the other failure shapes that reach a classifier -- an ``initialize``
    timeout, an ``OSError`` on the write after the child died -- never pass
    through it. A cancel of the caller propagates; see :func:`settle_drain`.
    """
    await settle_drain(self._stderr_task, timeout)


async def _sandbox_init_failure(self: AcpClient) -> AcpSandboxInitFailed | None:
    """The classified sandbox-init error for this child's stderr, or ``None``.

    Reads the ring buffer rather than the exception: the refusal is printed by
    the child (or by the wrapper that could not start it), and the exception
    that reaches the caller is whatever transport symptom followed -- an EOF,
    an ``initialize`` timeout, a broken pipe.

    Returns ``None`` when nothing was retained, which is the case for a
    restricted-memory session: stderr is not kept there by design, so this
    cannot classify and must not guess. Those sessions keep today's retry
    behaviour rather than getting a made-up verdict.

    Settles the drain first, for the reason the EOF branch of
    ``_read_message`` does: the ring is filled by the drain task while the
    failure that brings us here can arrive from the stdout side or from a
    timeout that never touched it, so a straight read can miss a line already
    in the pipe. Bounded, and swallowing everything except a cancel of the
    caller -- this is already a failure path (see :func:`settle_drain`).
    """
    await self._settle_stderr()
    if not self._stderr_lines:
        return None
    haystack = "\n".join(self._stderr_lines)
    if not is_sandbox_init_failure_output(haystack):
        return None
    # The WHOLE tail, not its last line: the launcher's own refusal line is
    # what corroboration keys on, and a burst can end on the child's generic
    # "failed to spawn" instead.
    detail, _ = redact_exfiltration_urls(haystack)
    detail, _ = redact_credentials(detail)
    return await sandbox_init_failure(
        detail,
        crew_wrap=self._sandbox_wrapped_by_crew,
        mode=self._sandbox_mode,
        extra_hidden_dirs=self._sandbox_hidden_dirs,
    )


async def _delegate_launch_failure(self: AcpClient) -> AcpDelegateLaunchFailed | None:
    """The delegate-launch error for this child's stderr, or ``None``.

    The ``AcpClient`` side of the classification ``AcpRuntime`` latches per
    stderr line: the same shared :func:`is_delegate_launch_failure_output`
    signature, read off the ring buffer the way :meth:`_sandbox_init_failure`
    reads it, so the two transports cannot disagree about what a
    ``kiro-cli-chat`` launch failure is. ``None`` when nothing was retained (a
    restricted-memory session keeps no stderr by design, so it keeps today's
    behaviour rather than a guessed verdict). The message is fixed wording, so
    no child output rides it and nothing needs redacting.
    """
    await self._settle_stderr()
    if not self._stderr_lines:
        return None
    if not is_delegate_launch_failure_output("\n".join(self._stderr_lines)):
        return None
    return AcpDelegateLaunchFailed()


async def _registration_throttle_line(self: AcpClient) -> str | None:
    """One redacted stderr line showing a throttled registration, or ``None``.

    Refuses to classify once this process has produced a non-thinking text
    chunk or dispatched a tool (``_prompt_or_tool_seen``): the transient
    verdict this evidence buys licenses the retry ladders to act, and a
    stale throttle line surviving in the ring past real work must never
    hand that verdict to a death whose replay could repeat side effects.
    The latch clears with the ring on respawn, so a recovered throttle
    followed by a fresh child opens a fresh window.

    Returns ``None`` when nothing was retained, which is the case for a
    restricted-memory session: stderr is not kept there by design, so this
    cannot classify and must not guess. Those sessions keep the generic
    death surface rather than getting a made-up verdict.

    Settles the drain first, for the reason :meth:`_sandbox_init_failure`
    does: the ring is filled by the drain task while the death that brings
    us here is discovered on the stdout side, so a straight read can miss a
    line already in the pipe. Redacted before it leaves: the line rides an
    exception message that reaches session cards and persisted errors, and
    child stderr is untrusted subprocess output that can echo a credential.
    """
    if getattr(self, "_prompt_or_tool_seen", True):
        return None
    await self._settle_stderr()
    if not self._stderr_lines:
        return None
    line = registration_throttle_line("\n".join(self._stderr_lines))
    if line is None:
        return None
    line, _ = redact_exfiltration_urls(line)
    line, _ = redact_credentials(line)
    return line


async def shutdown(self: AcpClient) -> None:
    """Gracefully stop the ACP process."""
    self._process_tree_confirmed_dead = False
    # Tear down an in-flight entitlement probe first: its throwaway client
    # spawned its OWN kiro-cli (and whatever MCP servers that started), and
    # nothing else reaps it -- `_kill_process` below kills only THIS session's
    # tree. Cancelling the future runs `_probe_advertised_models`'s `finally`,
    # which shields the probe client's own `shutdown()`, so the probe process
    # dies with the client that spawned it instead of outliving it ~270s (or,
    # on loop teardown, forever, until the next-startup PID sweep). Bounded so
    # a wedged probe cannot hold this shutdown.
    inflight = getattr(self, "_entitlement_probe_inflight", None)
    if inflight is not None and not inflight.done():
        from kiro_crew.acp.runtime import _TERMINATE_TIMEOUT

        inflight.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(inflight), timeout=_TERMINATE_TIMEOUT)
        except (Exception, asyncio.CancelledError):
            logger.debug("entitlement probe teardown on shutdown failed", exc_info=True)
    # `_reset_state` in a `finally`, because `_kill_process` can leave
    # through several doors: it awaits four `run_in_executor` calls (child
    # scan, record capture, escaped-child sweep) that are not individually
    # guarded, `subprocess_executor()` refuses new work once the loop is
    # tearing down, and `asyncio.CancelledError` is a `BaseException` --
    # shutdown being exactly when cancellation arrives.
    #
    # Nothing retries. Every caller treats this as terminal and drops the
    # client immediately afterwards (`AcpWorker` and `_shutdown_quietly`
    # both `except Exception: log` and then set their reference to None), so
    # a skipped reset is permanent: the pipes stay open, the sandbox temp
    # files stay on disk, and for the claude backend
    # `.claude/settings.local.json` -- written to hold `bypassPermissions`
    # for the session -- survives the process it belonged to.
    #
    # Running it after a failed kill is safe by construction: `_reset_state`
    # untracks only PIDs it confirms dead and deliberately RETAINS tracking
    # for survivors so the orphan sweep still reaps them.
    try:
        await self._kill_process(force=True)
    finally:
        await self._discard_bound_workspace()
        # `finally` for the same reason the kill above has one: `_reset_state`
        # untracks the PIDs and must run even if the seed's await is cancelled.
        # The settle is shielded, so the disk half completes either way.
        try:
            await self._discard_claude_settings_seed()
        finally:
            self._reset_state()  # untracks all PIDs (root + children)


def _retire_liveness_state(self: AcpClient) -> None:
    """Release the tracked consult and swap in a fresh, configured oracle.

    Both boundaries that drop a movement baseline — turn start in
    ``_prompt_loop`` under ``_turn_lock``, and process reset — retire rather
    than ``reset()``,
    and both must retire the future TOGETHER with the oracle. Their lifetimes
    cannot diverge: if only the oracle were replaced, a walk wedged during the
    previous turn would answer every later poll with "prior consult still in
    flight", so the new turn never samples its own process and the 90s cutoff
    completes it early — the truncation this gate exists to prevent.

    Releasing the future costs at most one abandoned worker per boundary
    instead of per silent read, which is the bound this gate is actually for.
    A walk submitted through ``_consult_liveness_model_wait`` already carries a
    retrieval callback from submission time, so its eventual exception is
    consumed even though nobody awaits it any more; the consume/attach here
    additionally covers a future that did not come from that path.

    ``fresh()`` carries the configuration over: a default-constructed
    replacement would silently repoint a caller-supplied /proc root, clock or
    sampling interval. ``getattr`` because the low-level PID lifecycle tests
    build clients with ``__new__``.
    """
    prior_consult = getattr(self, "_consult_future", None)
    self._consult_future = None
    if prior_consult is not None:
        if prior_consult.done():
            _consume_future_exception(prior_consult)
        else:
            prior_consult.add_done_callback(_consume_future_exception)
    retiring = getattr(self, "_liveness_oracle", None)
    self._liveness_oracle = retiring.fresh() if retiring is not None else LivenessOracle()
