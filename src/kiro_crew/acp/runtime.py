"""ACP Runtime for multiplexed kiro-cli sessions.

Single-reader demux architecture: one AcpRuntime owns the subprocess and a
reader task that routes frames by sessionId to per-session queues. Each
``AcpSessionHandle`` (in ``session_handle.py``) owns one sessionId + queue and
provides the prompt/cancel/approve/reject API.

The per-session handle, the runtime protocol it depends on, and the runtime
exceptions live in ``session_handle.py`` (the lower layer); they are re-exported
here so ``from kiro_crew.acp.runtime import AcpSessionHandle`` (and the
exceptions) keeps working for existing callers and tests.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import weakref
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, NamedTuple, TypeVar

from kiro_crew import acp_tool_gate, agent_scratch, platform_compat
from kiro_crew.acp._dispatch import (
    agent_version_from_init,
    attach_kas_custom_agents,
    build_session_new_params,
    parse_session_modes,
    redact_text,
)
from kiro_crew.acp._dispatch import reject_option_id as _reject_option_id
from kiro_crew.acp._dispatch import (
    set_mode_params,
)
from kiro_crew.acp._frame_record import record_frame
from kiro_crew.acp.client import (
    ChildRecord,
    OversizeLineUnrecoverable,
    _apply_pod_home_remap,
    _capture_child_records,
    _drain_oversize_line,
    _get_child_pids,
    _KiroExecutableTrustError,
    apply_pod_bundle_spawn,
    finish_suspended_spawn,
    is_auth_failure_output,
    is_sandbox_init_failure_output,
)
from kiro_crew.acp.harness import (
    HarnessAdapter,
    NotificationAliases,
    SessionExtras,
    SpawnContext,
    SpawnPlan,
    harness_for,
)
from kiro_crew.acp.harness.kas import PROTOCOL_VERSION_KAS
from kiro_crew.acp.harness.kiro import KIRO_CLI_SUBCMD, PROTOCOL_VERSION
from kiro_crew.acp.kas_agents import hoist_managed_servers, load_agent_spec
from kiro_crew.acp.kas_host_auth import HostAuthCallbackError
from kiro_crew.acp.kas_transport import (
    KAS_AUTH_CALLBACK_ERROR_CODE,
    METHOD_KAS_AUTH_GET_ACCESS_TOKEN,
)
from kiro_crew.acp.mcp_ref_guard import warn_unresolved_server_refs
from kiro_crew.acp.mcp_session_report import (
    active_custom_agent,
    required_managed_servers,
    roster_names,
)
from kiro_crew.acp.session_handle import (
    NATIVE_CHILD_ROSTER_CAP,
    AcpRequestTimeout,
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeOverloaded,
    AcpRuntimeProtocol,
    AcpSessionHandle,
    _load_watchdog_settings,
    advertised_models_from_session,
)
from kiro_crew.acp.session_mcp import (
    agent_spec_snapshot,
    session_mcp_disabled_tools,
    session_mcp_server_is_disabled,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_MARKDOWN_AGENT_SPECS,
    MCP_ROSTER_COMPLETE_NOTE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SET_MODE,
    JsonRpcMessage,
    JsonRpcRequest,
    backends_retired_by_host_logout,
    overlay_project_scope,
)
from kiro_crew.agent import ensure_agent_materialized, markdown_spec_for_agent
from kiro_crew.agent_sdk.tool_search import (
    ToolSearchSettings,
    kas_client_meta_settings,
    spec_grants_tool_search,
    with_client_meta_settings,
)
from kiro_crew.browser_cli.launch import browser_session_env, browser_socket_env
from kiro_crew.config import live
from kiro_crew.config.paths import default_work_dir, kiro_agents_dir
from kiro_crew.constants import (
    KIROCREW_SPAWN_INSTANCE_ENV,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.env import augmented_path, resolve_krb5_ccname
from kiro_crew.executors import subprocess_executor
from kiro_crew.mcp_gateway.claim import mint_stub_session_token, send_claim
from kiro_crew.mcp_gateway.session_servers import (
    attach_stub_session_token,
    injection_server_names,
    pooled_session_servers,
)
from kiro_crew.metrics.events import (
    CHILD_PERMISSION_DENIED,
    CHILD_PERMISSION_ROUTED,
    DROPPED_FRAMES,
    emit_counter,
)
from kiro_crew.providers.mirrors.registry import has_mirror, mirror_for
from kiro_crew.resource_status import inject_xdist_auto_cap
from kiro_crew.sandbox import (
    RLIMIT_PROFILE_SESSION_HOST,
    BoundWorkspaceMismatch,
    _forward_ssh_auth_sock,
    agents_slice_throttling,
    assert_voice_runtime_outside_agent_workspace,
    bind_voice_safe_agent_workspace_async,
    cgroup_scope_argv,
    create_subprocess_limited,
    release_bound_agent_workspace,
    resolve_bound_session_workspace,
    scrub_agent_subprocess_env,
    wrap_argv,
    wrap_argv_async,
    wrapped_by_crew_sandbox,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.session_pid import (
    _pgroup_has_member_besides,
    _pid_gone_or_unmanaged,
    _replace_child_pids,
    _signal_orphaned_runtime_group,
    _track_pid,
    _track_session_pid,
    _untrack_child_pids,
    _untrack_pid,
    _untrack_session_pid,
    group_vouching_available,
    register_protected_pid,
    unregister_protected_pid,
)
from kiro_crew.session_token_sig import publish_session_token
from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN, MODEL_ID_RE

logger = logging.getLogger(__name__)

#: Whether the spawn pins the caller-named work dir's directory chain across
#: process creation (Windows: a re-resolved pathname can be aimed at a share).
#: A module attribute rather than an inline platform read so a test can drive
#: the composition on any host.
_PIN_WORK_DIR_CHAIN: bool = platform_compat.IS_WINDOWS


def _escapee_is_still_ours(pid: int, record: ChildRecord) -> bool:
    """Whether a descendant outside the walk is still the process we recorded.

    Liveness alone cannot answer this, and answering it with liveness is how a
    stranger gets killed: a descendant that exited and had its number taken
    reads as alive, and carrying its record forward would publish the number as
    ours. The sweep then finds live identity and recorded identity in agreement
    -- both the stranger's -- and signals it.

    So compare the identity instead. A pid that is gone reads ``None`` and fails
    the same comparison, which is the answer we want for it too. An unreadable
    identity (a live process we may not introspect) also fails: we cannot vouch
    for it, and a record we cannot vouch for must not be written.
    """
    recorded = record[0] if isinstance(record, tuple) and record else None
    if recorded is None:
        return False
    return platform_compat.get_process_start_id(pid) == recorded


def _prune_dead_descendants(saved: dict[int, ChildRecord]) -> list[int]:
    """Untrack the descendants that are gone; return the ones still alive.

    A descendant's entry is pruned by ITS OWN liveness, never by the root's
    fate. One that escaped the group kill (it called setsid, so killpg never
    reached it) must keep its entry: that entry is the only handle the periodic
    sweep and the next startup cleanup have on it, and dropping it is precisely
    the leak this tracking exists to close.

    A synchronous unit so the caller can hand the whole thing to a worker
    thread: the liveness probes read ``/proc`` and the untrack takes the PID
    file's exclusive lock, neither of which may run on the event loop.
    """
    dead = {pid: rec for pid, rec in saved.items() if _pid_gone_or_unmanaged(pid)}
    if dead:
        try:
            _untrack_child_pids(dead)
        except Exception:
            logger.debug(
                "AcpRuntime: untracking descendant PIDs %s failed", list(dead), exc_info=True
            )
    return [pid for pid in saved if pid not in dead]


__all__ = [
    "AcpRuntime",
    "AcpRuntimeError",
    "AcpSessionStartTimeout",
    "AcpToolSurfaceBindingError",
    "AcpWorkspaceBindingError",
    "AcpRuntimeDead",
    "AcpRequestTimeout",
    "AcpRuntimeOverloaded",
    "SessionStartGate",
    "StartCollector",
    "AcpRuntimeProtocol",
    "AcpSessionHandle",
    # Re-exported from the harnesses that own them, so a caller that read them
    # off this module keeps working and there is still exactly one declaration
    # of each per-host value.
    "KIRO_CLI_SUBCMD",
    "PROTOCOL_VERSION",
    "PROTOCOL_VERSION_KAS",
]


# ── AcpRuntime ──

_T = TypeVar("_T")


class AcpWorkspaceBindingError(AcpRuntimeError):
    """A live runtime cannot safely serve this session; give it a runtime of its own.

    Raised for another cwd on a descriptor-bound runtime, and by the subclass
    below for a tool surface the process-wide settings would break. The
    run-runtime caller answers both the same way: a dedicated runtime.
    """


class AcpToolSurfaceBindingError(AcpWorkspaceBindingError):
    """A deferral-enabled process cannot serve an agent whose spec grants no loader."""


_STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024  # 10MB
# How many in-flight request ids to name in the oversize-frame warning. A dropped
# frame can carry a response, and the caller then fails as an opaque
# _send_and_await timeout — naming what was in flight at the drop makes that
# timeout attributable instead of a mystery. Capped so the line stays bounded.
_DROP_IDS_IN_LOG = 8

# Cap on the stderr text folded into a process-exit death reason. The reason
# is what the chat error card shows, so it must stay one readable line: the
# LAST non-empty stderr line the drain captured, truncated to this many
# characters -- roughly one card line. The 20-line ring itself is unchanged.
_STDERR_REASON_TAIL_CHARS = 200
# The one stderr signature that names a host fault rather than a kiro-cli
# fault: every sandboxed spawn fails with it once the runtime tmpfs the
# launcher stages mount sources on runs out of space or inodes. Point the
# operator at the doctor check that measures that filesystem.
_ENOSPC_MARKER = "no space left on device"
_ENOSPC_HINT = (
    "the runtime tmp filesystem is out of space or inodes; run `kirocrew doctor` "
    "(Runtime tmpfs section)"
)
# JSON-RPC 2.0 "Method not found" — the reader loop answers an ownerless
# server→client request with this itself (see _answer_ownerless_request);
# mirrors the private constant AcpClient keeps for its own dispatch sites.
_JSONRPC_METHOD_NOT_FOUND = -32601
_REQUEST_TIMEOUT = 30.0
# ``initialize`` budget while the kernel is throttling the agents slice. A
# throttled kiro-cli is alive and making progress, only slowly, so the fixed
# budget above kills work that would have finished; each retry then pays the
# same startup into the same throttle and deepens it. Three times the plain
# budget, not unbounded: the cold-start semaphore below is held for the whole
# wait. The value MUST stay strictly below the subagent startup watchdog
# (``subagent._STARTUP_TIMEOUT_SECS``, 120s from ``_exec_started``): a
# subagent's ``info._pid`` is recorded only after ``provider.start()`` returns,
# i.e. after this handshake, so the watchdog sees "no runtime yet" for the
# whole wait and force-reaps the live process the moment its deadline passes.
# The budget therefore has to expire first, with room for the spawn that
# precedes the handshake, so ``AcpRuntimeOverloaded`` is what the caller sees
# rather than a reaper kill. ``test_agents_slice_admission`` pins the ordering.
_INIT_TIMEOUT_UNDER_THROTTLE = 90.0
# One gateway event loop owns many independent SessionManager and worker-pool
# callers. Keep their expensive subprocess spawn + initialize handshakes behind
# one low process-wide-per-loop bound; worker pools use the same default.
_COLD_START_MAX_CONCURRENT = 2


class _ColdStartAdmission:
    """Loop-affine admission state for runtime spawn + initialize."""

    def __init__(self, limit: int) -> None:
        self.semaphore = asyncio.Semaphore(limit)
        self.active = 0
        self.queued = 0

    async def acquire(self) -> float:
        started = time.monotonic()
        self.queued += 1
        acquired = False
        try:
            await self.semaphore.acquire()
            acquired = True
        finally:
            self.queued -= 1
        if acquired:
            self.active += 1
        return (time.monotonic() - started) * 1000.0

    def release(self) -> None:
        self.active = max(0, self.active - 1)
        self.semaphore.release()


# asyncio synchronization primitives are loop-affine. Gateways normally have one
# loop, while tests and embedded callers can create several; keying by loop keeps
# the production bound gateway-wide without binding a semaphore to the wrong loop.
_cold_start_admissions: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[_ColdStartAdmission]
] = weakref.WeakKeyDictionary()
_cold_start_admissions_lock = threading.Lock()


def _cold_start_admission() -> _ColdStartAdmission:
    loop = asyncio.get_running_loop()
    with _cold_start_admissions_lock:
        admission_ref = _cold_start_admissions.get(loop)
        admission = admission_ref() if admission_ref is not None else None
        if admission is None:
            admission = _ColdStartAdmission(_COLD_START_MAX_CONCURRENT)
            _cold_start_admissions[loop] = weakref.ref(admission)
        return admission


def _cold_start_counts() -> tuple[int, int]:
    """Current-loop active and queued starts for bounded diagnostics."""
    admission = _cold_start_admission()
    return admission.active, admission.queued


# ── Session-start gate (RFC §4.4) ─────────────────────────────────────────────
#
# ``_ColdStartAdmission`` above bounds runtime spawn + initialize. This bounds
# the OTHER expensive start: ``session/new`` on an already-running runtime,
# which blocks while kiro-cli initializes the session's MCP servers. Under a
# burst of subagent starts every session/new competes for the same process,
# each one gets slower, and the 90s budget is hit by requests that would have
# completed in isolation -- a timeout that says nothing about the runtime's
# health. The gate keeps at most ``agent.session_start_concurrency`` (default 2)
# session/new requests outstanding per event loop; waiters queue in FIFO order.
# It is a FIXED semaphore on purpose: the adaptive loop lives in the gatewayd
# spawn gate and the execution-cap controller, and two adapting loops on one
# resource oscillate. The same gate serves every harness (kiro-cli, KAS, a
# later Claude host): it wraps ``create_session``, which every backend's
# session start runs through.
_SESSION_START_CONCURRENCY_DEFAULT = 2
_SESSION_START_CONCURRENCY_FLOOR = 1

# How many of the gate's permits are RESERVED for a ``session/new`` that has not
# gone out yet, expressed as a shortfall from the limit: a
# :class:`StartCollector` may hold at most ``limit - this`` permits.
#
# Without the reservation the gate starves. A timed-out start does not release
# its permit -- it hands it to a collector that keeps it for
# ``agent.start_collect_timeout_secs`` (default 300 s) -- so at the default
# limit of 2, two slow starts park BOTH permits for five minutes and every
# session/new on the whole gateway queues behind them, including the retries
# those failures produce, whose own timeouts create more collectors. Observed on
# an operator host: one member slot spent 15 consecutive auto-nudge cycles on
# ``session/new timed out after 90s (0/10 MCP server(s) reported)`` while no new
# agent process was ever spawned -- every attempt was waiting in this queue.
#
# Reserving one permit bounds what the collecting population can claim instead
# of letting it become the whole gate. A collector denied the hand-off still
# runs and still owns its request (the session it may yet receive is still
# adopted or torn down); it simply does not hold back-pressure it cannot
# release. At ``limit == 1`` the ceiling is 0, so no collector holds a permit --
# which is the only reading of "always keep one free" that a single-permit gate
# admits.
_COLLECTOR_PERMIT_HEADROOM = 1


def _resolve_session_start_concurrency() -> int:
    """Snapshot ``agent.session_start_concurrency`` from config (off-loop caller)."""
    try:
        from kiro_crew.config import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        return max(_SESSION_START_CONCURRENCY_FLOOR, int(cfg.agent.session_start_concurrency))
    except Exception:
        logger.debug("session_start_concurrency unreadable -- using default", exc_info=True)
        return _SESSION_START_CONCURRENCY_DEFAULT


def _record_session_start(start_t0: float, *, ok: bool, attributable_timeout: bool = False) -> None:
    """Feed one ``session/new`` outcome to the adaptive controller, when one runs.

    The controller is process-wide (``adaptive.controller.current``); without
    one this is a no-op. ``key`` groups the samples by the start kind so a slow
    ACP handshake reads apart from a slow MCP backend spawn.
    """
    try:
        from kiro_crew.adaptive.controller import current as _current_controller

        controller = _current_controller()
        if controller is None:
            return
        controller.record_start(
            (time.monotonic() - start_t0) * 1000.0,
            ok=ok,
            attributable_timeout=attributable_timeout,
            key="acp:session/new",
        )
    except Exception:
        logger.debug("session start sample not recorded", exc_info=True)


class SessionStartGate:
    """Loop-affine FIFO semaphore around ``session/new``.

    ``acquire()`` returns a :class:`StartPermit` carrying the queue wait in
    milliseconds so the caller can set the run's start clock at gate EXIT --
    time spent waiting here is queue time and must not count against the
    session-start budget or the startup watchdog. ``StartPermit.release()`` is
    idempotent, which is what makes "released exactly once on every path"
    checkable: ``releases`` counts real releases.
    """

    def __init__(self, limit: int) -> None:
        self.limit = max(_SESSION_START_CONCURRENCY_FLOOR, int(limit))
        self._semaphore = asyncio.Semaphore(self.limit)
        self.active = 0
        self.queued = 0
        self.releases = 0
        # Permits currently held by a StartCollector rather than by a live
        # ``session/new``. Bounded by ``collector_hold_ceiling`` so a fresh start
        # always has somewhere to go -- see _COLLECTOR_PERMIT_HEADROOM.
        self.collector_holds = 0

    @property
    def collector_hold_ceiling(self) -> int:
        """How many permits :class:`StartCollector` instances may hold at once."""
        return max(0, self.limit - _COLLECTOR_PERMIT_HEADROOM)

    async def acquire(self) -> "StartPermit":
        started = time.monotonic()
        self.queued += 1
        try:
            await self._semaphore.acquire()
        finally:
            self.queued -= 1
        self.active += 1
        return StartPermit(self, (time.monotonic() - started) * 1000.0)

    def _reserve_collector_hold(self) -> bool:
        """Claim one collector hold, or refuse when the ceiling is reached."""
        if self.collector_holds >= self.collector_hold_ceiling:
            return False
        self.collector_holds += 1
        return True

    def _release(self, *, collector_held: bool = False) -> None:
        if collector_held:
            self.collector_holds = max(0, self.collector_holds - 1)
        self.active = max(0, self.active - 1)
        self.releases += 1
        self._semaphore.release()


class StartPermit:
    """One acquired gate slot; ``release()`` is a no-op after the first call."""

    def __init__(self, gate: SessionStartGate, queue_wait_ms: float) -> None:
        self._gate = gate
        self.queue_wait_ms = queue_wait_ms
        self.released = False
        # True once a StartCollector owns this permit for the rest of its life,
        # which is what the gate counts against ``collector_hold_ceiling``.
        self.collector_held = False

    def hold_for_collector(self) -> bool:
        """Let a :class:`StartCollector` keep this permit, if the gate allows it.

        False when the permit is already released or already collector-held, or
        when collectors hold the gate's whole collector budget. The caller then
        releases the permit itself and gives the collector none: the collector is
        still created and still owns its request, but a start that has not gone
        out yet is never made to queue behind one that already gave up.
        """
        if self.released or self.collector_held:
            return False
        if not self._gate._reserve_collector_hold():
            return False
        self.collector_held = True
        return True

    def release(self) -> bool:
        if self.released:
            return False
        self.released = True
        self._gate._release(collector_held=self.collector_held)
        return True


_session_start_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, SessionStartGate] = (
    weakref.WeakKeyDictionary()
)
_session_start_gates_lock = threading.Lock()


async def session_start_gate() -> SessionStartGate:
    """The current loop's gate, sized from config the first time it is asked for.

    Config is resolved off-loop (``KiroCrewConfig.load`` is disk I/O) unless the
    live watcher's snapshot is armed. The size is fixed for the loop's lifetime;
    ``agent.session_start_concurrency`` is ``restart=True``. The gate is a
    strong value keyed weakly by loop, so it lives exactly as long as its loop.
    """
    loop = asyncio.get_running_loop()
    with _session_start_gates_lock:
        gate = _session_start_gates.get(loop)
    if gate is not None:
        return gate
    snap = live.snapshot()
    limit: int | None = None
    if snap is not None:
        try:
            limit = int(snap.agent.session_start_concurrency)
        except Exception:
            limit = None
    if limit is None:
        limit = await asyncio.to_thread(_resolve_session_start_concurrency)
    with _session_start_gates_lock:
        gate = _session_start_gates.get(loop)
        if gate is None:
            gate = SessionStartGate(limit)
            _session_start_gates[loop] = gate
    return gate


def session_start_gate_counts() -> tuple[int, int]:
    """``(active, queued)`` for the current loop's gate; ``(0, 0)`` when none exists."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return (0, 0)
    with _session_start_gates_lock:
        gate = _session_start_gates.get(loop)
    return (gate.active, gate.queued) if gate is not None else (0, 0)


class _PendingRequests(dict):
    """``{req_id: Future}`` for awaited control-plane requests, with ownership transfer.

    A request that timed out is normally popped: nobody will read its answer.
    ``adopt(req_id)`` is the other choice -- keep the entry so the late answer
    still resolves the future, and record who owns it now. The reader loop
    keeps popping on response exactly as before; only the timeout path changes.
    """

    def __init__(self) -> None:
        super().__init__()
        self.adopted: set[int] = set()

    def adopt(self, req_id: int) -> "asyncio.Future[dict[str, Any]] | None":
        future = self.get(req_id)
        if future is None:
            return None
        self.adopted.add(req_id)
        return future

    def pop(self, key, default=None):  # type: ignore[override]
        self.adopted.discard(key)
        return super().pop(key, default)

    def clear(self) -> None:
        self.adopted.clear()
        super().clear()


class AcpSessionStartTimeout(AcpRequestTimeout):
    """``session/new`` exceeded its budget while a :class:`StartCollector` still owns it.

    Distinct from a bare :class:`AcpRequestTimeout` so a caller can tell "the
    answer was lost" from "the request never went out": the session may still
    be created by the runtime, the collector holds the request id, and the
    caller may :meth:`StartCollector.adopt` the late session or let the
    collector tear it down. Re-queuing the task, or starting a dedicated
    process in its place, would leave that session running unowned.
    ``collector`` is None when the request never reached the wire (nothing to
    own).
    """

    def __init__(self, message: str, *, collector: "StartCollector | None") -> None:
        super().__init__(message)
        self.collector = collector
        # Every instance of this type is by definition a session start that did
        # not answer in time; see AcpRequestTimeout.session_start_failed.
        self.session_start_failed = True


# Bounds every init-frame holder below. A frame is staged only while the session
# id that would claim it is still unknown, so one that nobody ever claims must
# not accumulate for the runtime's life.
_INIT_NOTIFICATION_BUFFER_LIMIT = 100


def _split_init_frames(
    staged: "deque[JsonRpcMessage]", session_id: str
) -> tuple[list[JsonRpcMessage], "deque[JsonRpcMessage]"]:
    """Partition staged init frames into *session_id*'s and everyone else's.

    An empty *session_id* claims NOTHING: a start whose request never answered
    has no id to match on, and matching everything there would hand it a
    concurrent start's registrations.
    """
    matched: list[JsonRpcMessage] = []
    retained: deque[JsonRpcMessage] = deque(maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT)
    for msg in staged:
        params = msg.params if isinstance(msg.params, dict) else {}
        if session_id and str(params.get("sessionId") or "") == session_id:
            matched.append(msg)
        else:
            retained.append(msg)
    return matched, retained


StartAdopter = Callable[[str, dict[str, Any]], Awaitable[bool]]

START_OUTCOME_ADOPTED = "adopted"
START_OUTCOME_TORN_DOWN = "torn_down"
START_OUTCOME_ABANDONED = "abandoned"
START_OUTCOME_RUNTIME_DEAD = "runtime_dead"
START_OUTCOME_ERROR = "error"


class StartCollector:
    """Owns a ``session/new`` whose answer outlived the caller's budget.

    Created by :meth:`AcpRuntime.create_session` on timeout. Holds the adopted
    request future and the gate permit, and waits up to ``timeout``
    (``agent.start_collect_timeout_secs``) for the answer:

    * late result + an adopter registered -> the adopter decides; ``True``
      means the session continues under its original owner (``adopted``);
    * late result, no adopter (or the adopter declined) -> the session is torn
      down through the runtime's normal per-session teardown (``torn_down``),
      never by killing the shared runtime;
    * the runtime dies -> nothing to tear down (``runtime_dead``);
    * the cleanup deadline passes -> the request is dropped (``abandoned``).

    Whichever path settles it releases the gate permit exactly once and
    unregisters the collector. ``settled`` is an ``asyncio.Event`` for callers
    that want to wait for the verdict.

    It also holds the MCP-init frames its session may still send, because those
    frames name a session id nobody can claim until this request answers -- see
    :meth:`stage_init_frame`.
    """

    def __init__(
        self,
        runtime: "AcpRuntime",
        req_id: int,
        future: "asyncio.Future[dict[str, Any]]",
        *,
        permit: "StartPermit | None",
        timeout: float,
        context: dict[str, Any] | None = None,
        memory_mode: str = "persistent",
    ) -> None:
        self._runtime = runtime
        self.req_id = req_id
        self._future = future
        self._permit = permit
        self.timeout = float(timeout)
        self.context = dict(context or {})
        self.memory_mode = memory_mode
        self._adopter: StartAdopter | None = None
        self.outcome: str | None = None
        self.session_id: str = ""
        self.settled = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.created_at = time.monotonic()
        self._staged_init: deque[JsonRpcMessage] = deque(maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT)

    def start(self) -> "StartCollector":
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())
        return self

    @property
    def is_settled(self) -> bool:
        return self.settled.is_set()

    def adopt(self, adopter: StartAdopter) -> bool:
        """Register who takes the session if it arrives late; False once settled."""
        if self.is_settled:
            return False
        self._adopter = adopter
        return True

    def gate_released(self) -> bool:
        return self._permit is None or self._permit.released

    def seed_init_frames(self, staged: "deque[JsonRpcMessage]") -> None:
        """Copy the frames the timed-out start already staged into this collector.

        A copy, not a move: a CONCURRENT start's frames sit in the same runtime
        deque and only the id inside a frame says whose it is, so both holders
        keep every candidate and each claims by id (:meth:`take_init_frames`).
        """
        self._staged_init.extend(staged)

    def stage_init_frame(self, msg: JsonRpcMessage) -> None:
        """Hold one MCP-init frame that may belong to this start's late session.

        Held HERE rather than in the runtime's own staging deque because
        ``_mcp_init_progress`` reads that one un-keyed, by server NAME: a frame
        left there past its own init scope would be reported as the next
        session-start timeout's progress, which is the one diagnostic that has to
        stay attributable.
        """
        self._staged_init.append(msg)

    def take_init_frames(self, session_id: str) -> list[JsonRpcMessage]:
        """Take the staged frames that name *session_id*, leaving the rest."""
        matched, self._staged_init = _split_init_frames(self._staged_init, session_id)
        return matched

    def drop_init_frames(self) -> None:
        """Forget the staged frames: no claimant is left."""
        self._staged_init.clear()

    async def _run(self) -> None:
        outcome = START_OUTCOME_ERROR
        try:
            try:
                resp = await asyncio.wait_for(asyncio.shield(self._future), timeout=self.timeout)
            except asyncio.TimeoutError:
                self._runtime._pending_requests.pop(self.req_id, None)
                if not self._future.done():
                    self._future.cancel()
                # A dead runtime is dead whichever clock fired first. ``_mark_dead``
                # resolves this future with ``AcpRuntimeDead``, so the arm below
                # names the death only when that resolution WON the race against
                # this timeout -- and on a slow host it loses, which would report
                # the one outcome an operator can act on ("the process is gone")
                # as the one they cannot ("it never answered"). The runtime's own
                # flag is not a clock, so read that instead of ordering two.
                if self._runtime._dead:
                    outcome = START_OUTCOME_RUNTIME_DEAD
                    logger.warning(
                        "start collector: session/new req_id=%d unanswered after %gs and the "
                        "runtime is dead; settling as runtime_dead",
                        self.req_id,
                        self.timeout,
                    )
                    return
                outcome = START_OUTCOME_ABANDONED
                logger.warning(
                    "start collector: session/new req_id=%d never answered within %gs; "
                    "attempt abandoned",
                    self.req_id,
                    self.timeout,
                )
                return
            except AcpRuntimeDead:
                outcome = START_OUTCOME_RUNTIME_DEAD
                return
            except Exception:
                logger.debug(
                    "start collector: session/new req_id=%d failed late",
                    self.req_id,
                    exc_info=True,
                )
                return
            session_id = str((resp or {}).get("sessionId") or "")
            self.session_id = session_id
            if not session_id:
                return
            adopted = False
            if self._adopter is not None:
                try:
                    adopted = bool(await self._adopter(session_id, resp))
                except Exception:
                    logger.warning(
                        "start collector: adopter for late session %s raised; tearing down",
                        session_id,
                        exc_info=True,
                    )
                    adopted = False
            if adopted:
                outcome = START_OUTCOME_ADOPTED
                return
            try:
                await self._runtime._teardown_late_session(session_id)
            finally:
                if self.memory_mode != "persistent":
                    await asyncio.to_thread(AcpSessionHandle.cleanup_transcript_files, session_id)
            outcome = START_OUTCOME_TORN_DOWN
        finally:
            self.outcome = outcome
            # Settled on EVERY outcome: an adopted session already took its own
            # frames, and on any other outcome nothing will ever claim them.
            # Holding them would keep one attempt's registrations alive for the
            # runtime's life.
            self.drop_init_frames()
            released = self._permit.release() if self._permit is not None else False
            self._runtime._start_collectors.pop(self.req_id, None)
            logger.info(
                "start collector settled: req_id=%d outcome=%s session=%s gate_released=%s "
                "after %.1fs",
                self.req_id,
                outcome,
                self.session_id or "-",
                released,
                time.monotonic() - self.created_at,
            )
            self.settled.set()


# Session start (session/new, session/load) gets its own budget because kiro-cli
# blocks the response while it initializes the session's MCP servers, and a
# remote server pending OAuth holds that initialization for its FULL 30s
# authorization wait. _REQUEST_TIMEOUT is also 30s, so sharing it turns session
# start into a race the client usually loses: kiro-cli creates the session, the
# client gives up a beat earlier, and the slot dies. This must stay comfortably
# ABOVE the backend's 30s OAuth wait plus the initialization tail that follows
# it (observed: remaining servers register within ~1s after the wait; a
# 71-server agent with no pending OAuth completes in ~14s) — do NOT "tidy" it
# back down to _REQUEST_TIMEOUT.
# This is the built-in default AND floor; ``agent.session_start_timeout_secs``
# raises it for agents whose MCP fleet legitimately needs longer (see
# _resolve_session_start_timeout below).
_SESSION_NEW_TIMEOUT = 90.0

# Caps for the MCP progress line attached to a session-start timeout: a
# 70-server agent must not turn one error into a multi-kilobyte string, and
# neither a server's own error text nor its NAME is trusted for length. Both are
# config-derived, and an installed app supplies its own server names.
_MCP_PROGRESS_NAME_CAP = 8
_MCP_PROGRESS_ERROR_CAP = 120
_MCP_PROGRESS_NAME_LEN_CAP = 64


def _strip_unprintable(text: str) -> str:
    """Drop the control characters a whitespace collapse cannot reach.

    ``str.split`` removes whitespace controls (newline, tab, CR), but ESC and
    the other non-whitespace controls survive it, and a terminal rendering the
    gateway log interprets them -- an MCP server's failure text could forge or
    recolor terminal output. Spaces are printable, so a collapsed string keeps
    its word separation.
    """
    return "".join(ch for ch in text if ch.isprintable())


def _sanitize_progress_name(name: str) -> str:
    """Make one MCP server name safe to put in a log line and an exception.

    A name is config-derived, so an installed app chooses it. Four hazards, all
    closed here rather than at each use: an embedded newline would forge a line
    in the gateway log, a non-whitespace control (ESC) would inject terminal
    escapes into it, an unbounded name would defeat the count cap that keeps
    one error from becoming a wall of text, and a name carrying
    credential-shaped text would leak it into a sink the error message reaches.
    Whitespace collapse and the control strip run AFTER redaction so a
    redaction marker cannot reintroduce a break.
    """
    scrubbed, _ = redact_exfiltration_urls(name)
    scrubbed, _ = redact_credentials(scrubbed)
    return _strip_unprintable(" ".join(scrubbed.split()))[:_MCP_PROGRESS_NAME_LEN_CAP]


def _capped_names(names: list[str]) -> str:
    """Join names for an error line, truncating the tail to a countable summary.

    A pure formatter: names arrive already sanitized from the two points that
    admit them, so a composite like ``name (error)`` keeps its own error cap
    instead of being re-truncated to a name's length.
    """
    head = names[:_MCP_PROGRESS_NAME_CAP]
    rest = len(names) - len(head)
    joined = ", ".join(head)
    return f"{joined} (+{rest} more)" if rest > 0 else joined


# Teardown must be snappy: a session is usually terminated on a hot path
# (background task done, subagent reaped). kiro-cli's terminate handler responds
# as soon as it enqueues the eviction (the actual shutdown runs in its actor
# loop), so a healthy runtime acks well under this bound; a slow/dead one must
# not turn teardown into a multi-second stall.
_TERMINATE_TIMEOUT = 5.0

# Default recycling thresholds for long-lived multiplexed runtimes (see
# _is_stale()). These are conservative defaults chosen to recycle well before
# the unbounded growth observed in production (multi-GB RSS after ~24h of
# uptime with no per-turn compaction) while still amortizing process-spawn
# cost across many background prompts.
_DEFAULT_MAX_AGE_SECS = 6 * 3600  # 6 hours
_DEFAULT_MAX_RSS_MB = 500.0  # 500 MiB
# The Kiro CLI replaces its own executable in place during an update. A spawn
# that lands in that short window can fail with OSError and succeeds after this delay.
_ACP_RUNTIME_RESPAWN_BACKOFF_S = 2.0

# Below this uptime the RSS staleness probe is skipped entirely (see
# _is_stale()). A freshly-(re)used runtime has not had time to grow, so this
# keeps the hot get_bg_session reuse path — which holds _bg_runtime_lock —
# CPU-only for young runtimes and only pays the offloaded RSS probe once a
# runtime has lived long enough to plausibly have ballooned.
_RSS_PROBE_MIN_AGE_SECS = 300.0  # 5 minutes

# ── Awaited-request error formatting ──
#
# kiro-cli returns this when session/set_mode names an agent it cannot resolve,
# i.e. no ``<name>.json`` in its agents directory. The wire shape is a bare
# -32603 "Internal error", so nothing about the frame itself says "missing file".
# The name charset is bounded to what a real spec filename can hold (see
# validation of agent names elsewhere) rather than a greedy match, so a hostile
# or malformed backend string is not echoed back into a user-facing message.
_MODE_NOT_FOUND_RE = re.compile(r"""Mode ['"](?P<name>[A-Za-z0-9._-]{1,64})['"] not found""")


def _format_runtime_rpc_error(error: object) -> str:
    """Format an awaited-request JSON-RPC error into user-facing text.

    Awaited requests are the handshake ones — ``initialize``, ``session/new``,
    ``session/set_mode`` — so this is NOT the same population as
    ``client._format_acp_error``, which rewrites PROMPT-time provider failures
    (throttling, auth, 5xx) and has no branch that matches a missing agent spec.
    The two are deliberately separate rather than merged: their inputs come from
    different protocol phases and share no shape.

    Exactly one shape is rewritten today: a missing agent spec. Left raw it
    surfaces to the user as ``RPC error: {'code': -32603, 'message': 'Internal
    error', 'data': "Mode 'kirocrew' not found"}`` — which names an internal ACP
    concept, reads as a backend bug, and hides that the cause is a local file and
    the fix is one command. Every other shape falls through to the raw dict, so a
    shape nobody has classified is surfaced rather than swallowed.
    """
    if isinstance(error, dict):
        match = _MODE_NOT_FOUND_RE.search(str(error.get("data", "") or ""))
        if match:
            name = match.group("name")
            return (
                f"Agent spec '{name}' is not installed: kiro-cli found no "
                f"'{name}.json' in {kiro_agents_dir()}. Every turn fails until it "
                f"is restored — repair with `kirocrew setup --agent-only --clean`, "
                f"then restart the gateway."
            )
    return f"RPC error: {error}"


# ── Unroutable-frame drop accounting ──
#
# The reader drops any frame it cannot route (see _reader_loop). That is
# CORRECT behaviour, but logging it per frame is not: kiro-cli is multiplexed,
# so every frame for a torn-down or not-yet-registered sessionId takes the drop
# branch, and a backend that keeps streaming after teardown makes that an
# unbounded STEADY STATE, not a burst. Measured on an operator host: ~60
# lines/second for 6+ hours from one gateway PID, taking 33–59% of every
# gateway.log rotation — which, at RotatingFileHandler(maxBytes=2MB,
# backupCount=3) (see cli.py), rolls the genuine diagnostics needed for an
# incident out of the retained 8MB window before anyone can read them.
#
# So the per-frame line is collapsed into a periodic count keyed by
# (sessionId, method). The key must stay PER SESSION: the incident's decisive
# signal was that two DIFFERENT session UUIDs were flooding at once, which a
# single global tally would hide.
_DROP_SUMMARY_INTERVAL_SECS = 60.0
# Hard cap on distinct (sessionId, method) keys held between flushes. Both
# halves of the key are backend-controlled, so an unbounded map would be a
# memory sink; reaching the cap forces an early flush instead of growing.
_DROP_SUMMARY_MAX_KEYS = 64
# Backend-controlled key text is truncated before it is stored, so a
# pathological sessionId/method (a stdout line may be up to
# _STDOUT_BUFFER_LIMIT) cannot be retained at full length by the map either.
_DROP_SUMMARY_KEY_MAX_CHARS = 80
# Stands in for the sessionId half of the key on the no-sessionId broadcast
# path, which has no session to name.
_DROP_NO_SESSION = "-"
# Stands in for EITHER half of the key when the backend supplied no usable
# string: an absent `method`, or a value of the wrong JSON type (see
# _drop_key_part).
_DROP_KEY_PLACEHOLDER = "?"

# SEL/metric reason for a permission request auto-rejected while the last
# subagent roster named more children than NATIVE_CHILD_ROSTER_CAP. Distinct
# from `unregistered_session_auto_reject` because the two say different things:
# that reason claims the backend never announced the session, and past the cap
# the backend may well have — the id simply is not remembered, so it cannot be
# verified as this owner's child. Auditing an announced child's denial as
# "unregistered" would make the one signal that a cap truncation cost a real
# approval indistinguishable from an ordinary unknown-session frame.
_ROSTER_OVERFLOW_REJECT_REASON = "roster_overflow_auto_reject"
# The default: the sessionId is in no roster this runtime remembers, and no
# roster it remembers was truncated either, so the backend genuinely never
# announced this child to us.
_UNREGISTERED_REJECT_REASON = "unregistered_session_auto_reject"
# Minimum seconds between throttled DEBUG summaries of repeated roster
# truncation. A `subagent/list_update` is a backend-controlled notification
# re-broadcast on every child status change, so above the cap the WARNING it
# earns is a steady state at the frame rate, not a burst — the same retention
# hazard `_note_dropped_frame` exists to avoid, one level louder. Bound to the
# unroutable-frame interval rather than given a second value: both answer the
# one question "how often may a suppressed high-frequency condition restate
# itself in gateway.log", and a second knob is a second throttle to reason
# about in the same module.
_ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS = _DROP_SUMMARY_INTERVAL_SECS

# Entitlement probe (probe_advertised_models). The probe session carries no MCP
# servers and activates no mode, so it is far cheaper than a real session start;
# the timeout is still generous because the probe runs exactly when something is
# already wrong (a rejection is being revalidated) and a loaded host must not
# turn a recoverable verdict into a spurious probe failure.
_ENTITLEMENT_PROBE_TIMEOUT = 30.0
# A fresh answer is reused for this window so a burst of rejections (several
# chats revalidating at once) costs one round-trip, not one per rejection.
_ENTITLEMENT_PROBE_TTL_SECS = 20.0


# Re-exported from the module that owns the resolver, not re-declared. A second literal
# here is free to drift from the name the spawn actually uses, and the reclaim sweep
# projects its marker set from the same registry the owner's value is asserted against.
# ``CLIENT_NAME``/``CLIENT_VERSION`` ride along for the same reason: BOTH transports
# send them in one ``clientInfo`` object, so a second pair here would be free to
# report a different client to the same host -- which is exactly how the flat
# ``clientName`` regression stayed invisible on one transport while the other was
# correct.
from kiro_crew.acp.client import (  # noqa: E402  (re-export, not a new name)
    CLIENT_NAME,
    CLIENT_VERSION,
    KIRO_CLI_BIN,
)

# Re-exported, not re-declared. Each host's ACP revision and its own ``acp``
# subcommand belong to that host's harness, which is what the handshake now
# reads; a second copy here would be free to drift from the value actually sent,
# and these two disagree on the field's TYPE as well as its value.


def _drop_key_part(value: object) -> str:
    """Bounded, hashable string for one half of a (sessionId, method) drop key.

    BOTH halves arrive verbatim from backend JSON: `JsonRpcMessage.from_dict`
    copies `method` and `params` with no validation, so the `str | None`
    annotation is documentation, not enforcement — `{"method": 123}` yields an
    int, and `params.sessionId` is `Any`. Slicing such a value raises TypeError
    *inside* `_reader_loop`, the single owner of this process's stdout, which
    marks the runtime dead and tears down EVERY multiplexed session on it. One
    malformed frame must not cost every session, so anything that is not a
    `str` (including the legitimate absent-`method` `None`) becomes the
    placeholder before it is sliced or used as a dict key.
    """
    if not isinstance(value, str):
        return _DROP_KEY_PLACEHOLDER
    return value[:_DROP_SUMMARY_KEY_MAX_CHARS]


def _get_rss_mb(pid: int) -> float | None:
    """Get resident set size (RSS) of a process in MiB, or None if unavailable.

    Linux: reads /proc/<pid>/status. macOS (no /proc): shells out to
    ``ps -o rss= -p <pid>`` (ps reports RSS in KiB on both platforms).
    Windows: WorkingSetSize through the ``platform_compat`` shim, since no
    ``ps`` is resolvable there. Returns None on any failure (missing /proc,
    permission error, process gone, ps not found) so callers can treat
    "unknown" the same as "not over threshold" rather than raising.
    """
    if sys.platform == "linux":
        try:
            with open(f"/proc/{pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # Format: "VmRSS:\t   123456 kB"
                        parts = line.split()
                        return int(parts[1]) / 1024.0
        except (OSError, IndexError, ValueError):
            return None
        return None

    if platform_compat.IS_WINDOWS:
        # Windows ships no `ps` in the fixed system directories the POSIX
        # fallback below resolves through (trusted_system_bin ignores PATH on
        # purpose), so that fallback can only ever answer None here. Read
        # WorkingSetSize via GetProcessMemoryInfo through the shim instead.
        # The watchdog's RSS-recycle ceiling does not depend on this branch —
        # _get_rss_tree_mb serves Windows from proc_rss_tree_mb_for_pid and
        # never calls this function — so this keeps a direct single-pid read
        # honest for a direct caller.
        rss = platform_compat.proc_rss_bytes_for_pid(pid)
        return None if rss is None else rss / (1024.0 * 1024.0)

    # macOS / other: no /proc, fall back to ps (mirrors the sysctl/ps pattern
    # used elsewhere in this codebase for darwin system info).
    ps_bin = platform_compat.trusted_system_bin("ps")
    if ps_bin is None:
        return None
    try:
        out = (
            subprocess.check_output([ps_bin, "-o", "rss=", "-p", str(pid)], timeout=2)
            .decode()
            .strip()
        )
        return int(out) / 1024.0
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _own_children(pid: int) -> list[int]:
    """Direct children of *pid*, asked of the kernel one thread at a time."""
    kids: list[int] = []
    try:
        entries = os.listdir(f"/proc/{pid}/task")
    except OSError:
        return kids
    for tid in entries:
        try:
            with open(f"/proc/{pid}/task/{tid}/children") as f:
                tokens = f.read().split()
        except OSError:
            continue
        for tok in tokens:
            try:
                kids.append(int(tok))
            except ValueError:
                continue
    return kids


def _iter_descendant_pids(
    pid: int,
    max_depth: int | None = None,
    *,
    children: "dict[int, list[int]] | None" = None,
) -> list[int]:
    """Return ``[pid, *descendants]`` (Linux only), best-effort.

    Walks ``/proc/<pid>/task/<tid>/children`` breadth-first. Returns ``[pid]``
    when the interface is unavailable. Used so RSS accounting can cover a
    sandbox launcher's exec'd child — see _get_rss_tree_mb().

    ``max_depth`` bounds the walk in generations below *pid*: ``None`` is the
    whole subtree, ``0`` is *pid* alone, ``1`` adds its direct children. The queue
    carries each pid's own depth rather than the loop tracking a level, so a
    process reachable at two depths is counted once, at whichever it is reached
    first — the same single-visit rule the unbounded walk has.

    ``children`` supplies a parent map (``platform_compat.proc_child_map``) to
    read the edges from instead of asking the kernel per process. Same walk and
    same rules; only where an edge comes from changes. It is for a caller that
    needs MANY roots' trees in one pass: the kernel route costs one read per
    thread of every process visited, which a per-root caller pays again on every
    root, while one map answers all of them. A map that is missing a process
    yields the root alone for it, exactly as an unreadable ``children`` file does.
    """
    order: list[int] = []
    visited: set[int] = set()
    queue: list[tuple[int, int]] = [(pid, 0)]
    while queue:
        p, depth = queue.pop()
        if p in visited:
            continue
        visited.add(p)
        order.append(p)
        if max_depth is not None and depth >= max_depth:
            continue
        for cpid in children.get(p, ()) if children is not None else _own_children(p):
            if cpid not in visited:
                queue.append((cpid, depth + 1))
    return order


#: A whole-machine process table: ``(children_by_ppid, rss_kib_by_pid)``.
_ProcessTable = tuple[dict[int, list[int]], dict[int, int]]

#: How long one ``ps -A`` snapshot may be reused.
#:
#: This exists because the snapshot is WHOLE-MACHINE while its consumer asks
#: per-pid. ``session_memory._blocking_sample`` samples every live runtime pid in
#: one pass, so an uncached snapshot enumerated every process on the host once
#: PER SESSION — 8 sessions on a host with ~150 MCP processes meant 8 full
#: process-table walks every 5s, serialized in one worker. Measured cost on a
#: typical Mac (875 procs): ~33ms per ``ps -Ao``, so 8 walks ≈ 272ms duty cycle
#: per 5s poll — linear amplification that wastes a thread worker and grows with
#: session count (macOS only: the Linux branch above uses ``/proc`` directly and
#: never spawns anything).
#:
#: One second is chosen against the two consumers, not arbitrarily: the Sessions
#: panel polls at 5s and the watchdog's RSS ceiling is a multi-GB threshold
#: checked on a timer, so neither can tell a 1s-old measurement from a fresh
#: one — while a sampling pass over N pids completes well inside the window and
#: therefore pays for exactly one snapshot.
_PS_TABLE_TTL_S = 1.0

_ps_table_lock = threading.Lock()
#: ``(monotonic_taken_at, table)``, or None before the first snapshot. A cached
#: FAILURE is not stored — a transient ``ps`` error must not pin every caller to
#: the single-pid fallback for a whole second.
_ps_table_cache: tuple[float, _ProcessTable] | None = None


def _reset_ps_table_cache() -> None:
    """Drop the memoized process table. Test seam: the cache is keyed on wall
    time only, so a test that fakes ``ps`` output would otherwise inherit the
    previous test's snapshot."""
    global _ps_table_cache
    with _ps_table_lock:
        _ps_table_cache = None


def _ps_process_table() -> _ProcessTable | None:
    """One ``ps -Ao pid=,ppid=,rss=`` snapshot as a parent map + RSS map.

    Memoized for :data:`_PS_TABLE_TTL_S` so a caller that needs the tree for many
    pids pays for ONE process-table walk rather than one per pid. Returns None
    when ``ps`` is unavailable or fails, so callers fall back to a single-pid
    read instead of reporting a phantom-empty tree.

    The snapshot is taken under the lock rather than merely published under it:
    concurrent first-callers would otherwise each spawn ``ps`` before any of them
    stored a result, which is the exact amplification this cache exists to
    remove.
    """
    global _ps_table_cache
    with _ps_table_lock:
        cached = _ps_table_cache
        if cached is not None and (time.monotonic() - cached[0]) < _PS_TABLE_TTL_S:
            return cached[1]
        ps_bin = platform_compat.trusted_system_bin("ps")
        if ps_bin is None:
            return None
        try:
            out = (
                subprocess.check_output([ps_bin, "-Ao", "pid=,ppid=,rss="], timeout=2)
                .decode()
                .strip()
            )
        except (OSError, subprocess.SubprocessError):
            return None
        children: dict[int, list[int]] = {}
        rss_kib: dict[int, int] = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            try:
                cpid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
            except ValueError:
                continue
            children.setdefault(ppid, []).append(cpid)
            rss_kib[cpid] = rss
        table: _ProcessTable = (children, rss_kib)
        _ps_table_cache = (time.monotonic(), table)
        return table


def _rss_tree_mb_for_pids(pids: list[int]) -> float | None:
    """Sum RSS (MiB) over pids already walked (Linux), or None if none answered.

    Split out of _get_rss_tree_mb so a caller that ALREADY holds the descendant
    set can sum it without walking again. Nearly all of the cost is the walk,
    not the sum: measured over 245 live session trees of 2 to 31 processes, the
    walk took a median 10.3ms while summing RSS over the set it returned took
    0.8ms. So a caller that needs both the set and the total, and cannot hand
    the set over, walks twice and roughly doubles its own cost.
    """
    total = 0.0
    found = False
    for p in pids:
        r = _get_rss_mb(p)
        if r is not None:
            total += r
            found = True
    return total if found else None


def _get_rss_tree_mb(
    pid: int, max_depth: int | None = None, *, pids: list[int] | None = None
) -> float | None:
    """Sum RSS (MiB) of *pid* and its descendants, or None if unavailable.

    ``max_depth`` bounds the sum in generations below *pid*, for a host that
    declares one through ``SpawnPlan.rss_depth``. ``None``, the default, is
    the whole subtree and is what every kiro-family host uses.

    Windows answers None for any bounded request rather than a subtree total. The
    bound is not available there: the tree is summed through
    ``proc_rss_tree_mb_for_pid``, whose lineage-VALIDATED walk returns a flat set
    of genuine descendants with no generation attached, and the naive parent-map
    walk that would carry depth is the unsafe one that walk exists to avoid.
    Answering with the subtree instead would judge a bounded host's ceiling
    against an unbounded measurement — and for a host that declares a bound
    because its subtree is dominated by a per-session fleet, that reads as a leak
    on the first session and recycles a healthy process. None is the "unknown, do
    not judge" answer this probe's caller already handles, so the age ceiling
    still governs while the RSS ceiling abstains.

    On Linux the kirocrew-lite background runtime is spawned through the
    namespace sandbox launcher, which ``fork()``s: ``self._pid`` is the
    launcher parent (small, stable, blocked in ``waitpid``) while the real
    kiro-cli that accumulates multi-GB RSS is a child. Measuring only
    ``self._pid`` therefore misses the growth entirely, so we sum the whole
    descendant tree.

    On macOS the tree is walked too, and it is NOT redundant: kiro-cli spawns
    MCP-server / tool children there exactly as it does on Windows (see that
    branch's note), so measuring only ``pid`` under-reports a session's real
    footprint and blinds the watchdog's leak ceiling. The macOS tree is NOT "just
    the process itself" — believing otherwise is what makes the per-pid
    whole-machine snapshot look free.

    ``pids`` lets a caller that has ALREADY walked the descendants hand the set
    over so it is not walked a second time. Linux only, because that is the one
    branch whose total is reached from a pid list at all.
    """
    if sys.platform == "linux":
        if pids is None:
            pids = _iter_descendant_pids(pid, max_depth)
        return _rss_tree_mb_for_pids(pids)

    if platform_compat.IS_WINDOWS:
        if max_depth is not None:
            # See the docstring: no depth-carrying validated walk exists here, and
            # a subtree total would be judged against a bounded host's ceiling.
            return None
        # Windows spawns kiro-cli WITHOUT a launcher fork, but it still spawns
        # MCP-server / tool children that can leak. Sum the tree via
        # proc_rss_tree_mb_for_pid, which enumerates descendants through
        # descendant_termination_handles — the lineage-VALIDATED walk (exact
        # creation/exit-time edge checks across two snapshots). A raw Toolhelp
        # parent-map walk is unsafe here: th32ParentProcessID is never cleared
        # when a parent dies and Windows recycles PIDs, so it would sum unrelated
        # subtrees rooted at a recycled PID into a kill/health decision. The
        # validated walk always counts the root, so an unreadable descendant
        # (another session / higher integrity) narrows the total rather than
        # producing a phantom-low tree attached to a recycled root.
        return platform_compat.proc_rss_tree_mb_for_pid(pid)

    # macOS / other: sum the descendant subtree rooted at pid off a SHARED
    # whole-machine snapshot (ps reports RSS in KiB). The snapshot is memoized in
    # _ps_process_table, so sampling N pids costs one process-table walk, not N.
    table = _ps_process_table()
    if table is None:
        return _get_rss_mb(pid)
    children, rss_kib = table
    if pid not in rss_kib:
        return None
    total_kib = 0
    visited: set[int] = set()
    queue: list[tuple[int, int]] = [(pid, 0)]
    while queue:
        p, depth = queue.pop()
        if p in visited:
            continue
        visited.add(p)
        total_kib += rss_kib.get(p, 0)
        if max_depth is not None and depth >= max_depth:
            continue
        queue.extend((c, depth + 1) for c in children.get(p, []))
    return total_kib / 1024.0


#: ``agent.start_collect_timeout_secs`` built-in default: how long a
#: StartCollector keeps a timed-out session/new before abandoning the attempt.
_START_COLLECT_TIMEOUT_DEFAULT = 300.0


def _resolve_start_collect_timeout() -> float:
    """Snapshot ``agent.start_collect_timeout_secs`` from config (off-loop caller)."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        return max(10.0, float(cfg.agent.start_collect_timeout_secs))
    except Exception:
        logger.debug("start_collect_timeout_secs unreadable -- using default", exc_info=True)
        return _START_COLLECT_TIMEOUT_DEFAULT


def _resolve_session_start_timeout() -> float:
    """Snapshot ``agent.session_start_timeout_secs`` from config.

    Function-level import (mirrors ``_load_watchdog_settings`` in
    session_handle.py) avoids the config -> dashboard -> acp import cycle;
    any failure falls back to the built-in default rather than breaking a
    runtime. The loader clamps the on-disk value to
    [SESSION_START_TIMEOUT_MIN, SESSION_START_TIMEOUT_MAX]; the ``max`` here
    is belt-and-braces so a degraded load can never shrink the budget below
    the built-in floor — a session-start budget under the backend's 30s OAuth
    wait recreates the race that floor exists to prevent.
    """
    try:
        # circular import: config.loader -> dashboard -> session -> acp
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig.load()
        return max(_SESSION_NEW_TIMEOUT, float(cfg.agent.session_start_timeout_secs))
    except Exception:
        logger.debug("session-start timeout load failed — using default", exc_info=True)
        return _SESSION_NEW_TIMEOUT


def _ref_spec_snapshot(agent: str | None, work_dir: str | Path) -> dict[str, Any] | None:
    """The unresolved-ref guard's spec snapshot, or ``None`` -- never an exception.

    The runtime twin of ``AcpClient._read_mcp_ref_spec``, and best-effort for the
    same reason: this rides inside the hop that builds a session's MCP array, on
    every host's establishment path including kiro's. ``agent_spec_snapshot`` goes
    through the derived-spec freshness gate, which refuses a stale mirror it could
    not repair -- the right answer for the projection, whose output IS the
    session's MCP surface, and the wrong one for a diagnostic: a guard that can
    fail ``session/new`` is a worse defect than the unresolved ref it reports.
    ``None`` makes the guard silent and leaves the session's fate to the array.
    """
    try:
        return agent_spec_snapshot(agent, work_dir=work_dir)
    except Exception:
        logger.debug("unresolved-ref guard: agent spec unreadable", exc_info=True)
        return None


def _disable_check_scope(backend: str, work_dir: Any) -> Any:
    """The checkout a switched-off-server check may read *backend*'s spec from.

    ``None`` for a host that resolves its agent at the USER level only, and the
    session's checkout for every other. Read from :func:`overlay_project_scope`, the
    one decider, rather than spelled again here: the question is the same one the
    array's own projection asks, and answering it twice is how the two scopes come
    apart.

    The mismatch this exists to prevent is specific. ``session_mcp`` resolves a spec
    project-nearest and does NOT fall back, so on a user-level host a same-named file
    in the checkout would decide the answer for a session running the user-level
    agent: a switch-off written where that session's agent actually lives would read
    as "not disabled", and the server it withdraws would mount.
    """
    return overlay_project_scope(backend, work_dir).get("work_dir")


def _pooled_session_servers_and_ref_spec(
    overlay: Any, agent: str | None, backend: str, work_dir: str | Path
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The pooled stub array AND the guard's spec snapshot, from one off-loop hop.

    Module-level so the hop is one ``to_thread`` call with no closure, and so the
    two reads stay together: adding the snapshot as a second hop would be the
    scheduling point H13 forbids on the kiro path. The array is resolved FIRST
    and the snapshot is the passenger: a snapshot failure resolves to ``None``
    and cannot cost the session its array.

    The overlay lookup's scope is :func:`overlay_project_scope`'s answer for
    *backend*, splatted at the call itself: the ``session_servers`` ratchet reads
    the scope off the call that resolves the overlay, and this is that call for
    both of the runtime's session-array paths. The snapshot reads the spec against
    the session's checkout regardless -- a user-level-only host scopes its overlay
    lookup to nothing, and the guard still judges the spec it is running.
    """
    servers = pooled_session_servers(overlay, agent, **overlay_project_scope(backend, work_dir))
    return servers, _ref_spec_snapshot(agent, work_dir)


class _MirroredSessionMcp(NamedTuple):
    """One mirrored host's ``session/new`` MCP array and what came with it.

    Four things fall out of ONE agent-spec parse, and only the first is wire data.
    Returning them together is what stops a second parse from being needed for the
    others -- which would be the consistency window the projection exists to close,
    since the spec is a user-writable file that can change between two reads.

    ``denied_tools`` is the driver obligation: the ``(server, tool)`` pairs this
    session's spec switched off, spelled as the backend registers them, refused at
    the approval request because this transport has no wire channel for a per-tool
    restriction. ``stub_token`` names this session's brokered servers to gatewayd.
    ``derived_spec_snapshot`` is the generation the array was built from, typed
    loosely so this module stays clear of :mod:`kiro_crew.agent` and its config
    import chain.
    """

    servers: list[dict[str, Any]]
    denied_tools: frozenset[tuple[str, str]]
    stub_token: str
    derived_spec_snapshot: Any
    ref_spec: Any = None
    """The agent spec as the unresolved-ref detector reads it, or ``None``.

    Read in the same off-loop hop as the projection, so the guard that consumes
    it costs session start no scheduling point of its own (H13). ``None`` when the
    spec is unreadable, which the guard treats as nothing to say.
    """


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


class AcpRuntime:
    """Owns one kiro-cli acp subprocess with single-reader demux.

    The _reader_task is the ONLY coroutine that reads from stdout.
    It routes frames by:
      - 'id' field in _pending_requests → resolve Future (for send_and_await)
      - 'id' field in _routed_requests → put in session queue (for prompt responses)
      - params.sessionId → _session_queues[sessionId].put(msg)
      - no sessionId → broadcast to all session queues
    """

    def __init__(
        self,
        work_dir: str | Path | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        extra_env: dict[str, str] | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
        max_age_secs: float = _DEFAULT_MAX_AGE_SECS,
        max_rss_mb: float = _DEFAULT_MAX_RSS_MB,
        model: str | None = None,
        expect_mcp_reports: bool = True,
        acp_backend: str = ACP_BACKEND_KIRO,
        crew_agent: str = "",
        member_context: bool = False,
        memory_mode: str = "persistent",
        tool_search: ToolSearchSettings | None = None,
        shared_scratch: Path | None = None,
    ):
        if work_dir:
            self._work_dir = Path(work_dir)
        else:
            self._work_dir = default_work_dir()
        # A caller-named work dir (a slot's project, a folder's project, a
        # cron job's ``project_dir``, an alias workspace) is operator-chosen and
        # was handed over as a canonical STRING; on Windows the spawn pins its
        # directory chain so that string still reaches the verified
        # directories when the child enters it (``_pin_work_dir_chain``).
        # The runtime's own default tree is not operator-named and is left alone.
        self._work_dir_is_named = bool(work_dir)
        self._agent = agent
        # Canonical Kiro Crew agent identity (a cfg.agents key) resolved by the
        # surface that created this runtime — a DIFFERENT namespace from
        # ``agent`` (the kiro template the process spawns with). Default for
        # sessions created on this runtime; a warm-pool rekey overwrites it so
        # later sessions inherit the claiming crew, not the pool's spawn state.
        self._crew_agent = crew_agent
        self._acp_backend = acp_backend
        # The operator's MCP Tool Search choice, for hosts that take it over the
        # wire (``client_meta_settings``). None leaves the handshake as the harness
        # declares it. ``_tool_search_wire`` is what was actually sent, kept so a
        # later session on this process can be judged against the process-wide
        # setting it inherits (see create_session).
        self._tool_search = tool_search
        self._tool_search_wire: dict[str, Any] = {}
        # Resolved on FIRST USE, never here: ``ACP_BACKENDS_KNOWN`` admits
        # backends the shared-process runtime has no harness for, and provider
        # safety constructs a runtime for every one of them to prove the reader
        # loop survives a recorder fault. Resolving in __init__ would make those
        # constructions raise, so the failure is deferred to the first seam that
        # actually needs a host's answer -- a runtime that never spawns and never
        # starts a session never needs one.
        self._harness_resolved: HarnessAdapter | None = None
        # Whether THIS process was spawned with Crew as the engine's auth owner
        # (relay started without ``--auth-method cli`` because the Crew vault
        # held an identity at spawn). Decided once in _resolve_spawn_plan and
        # read by the reader loop: a credential callback is answered from the
        # vault only on a process that was spawned expecting it.
        self._kas_host_auth = False
        # First answered credential callback per runtime is logged at INFO as a
        # positive "the engine is drawing its credential from Crew" signal; later
        # ones (the engine refreshes ahead of expiry) drop to DEBUG.
        self._kas_host_auth_logged = False
        if model is not None:
            if not MODEL_ID_RE.match(model):
                raise ValueError(
                    f"Invalid model identifier: {model!r} — must match "
                    f"^[a-zA-Z0-9][a-zA-Z0-9._-]{{0,127}}$"
                )
        self._model = model
        self._sandbox_mode = sandbox_mode
        self._member_context = member_context
        if memory_mode not in {"persistent", "incognito", "temporary"}:
            raise ValueError("Invalid session memory mode")
        self.recording_allowed = memory_mode == "persistent"
        self._native_launch_sources: dict[str, str] = {}
        self._extra_env = extra_env or {}
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        # Whether sessions on this runtime should hold drain_init() open for
        # slow MCP servers (the no-report ceiling). A runtime whose agent is
        # KNOWN to have zero MCP servers — the kirocrew-lite background runtime,
        # whose config Kiro Crew itself writes with an empty mcpServers map —
        # opts out so hot one-liner paths (chat titles, suggestions, STT
        # endpointing) don't pay a full ceiling wait that can never be armed.
        self._expect_mcp_reports = expect_mcp_reports
        self._sandbox_cleanup: str | None = None
        self._bound_workspace_fd: int | None = None
        self._work_dir_chain: list[int] = []
        self._spawn_work_dir = str(self._work_dir)
        # The session tree's work directory when this runtime is not the tree's
        # first process (a companion runtime spawned for a parent's subagents,
        # or the successor of a recycled ``_bg`` runtime). Re-validated at spawn
        # (``agent_scratch.shared_scratch_window``); ``None`` means this process
        # starts a tree and its own directory is the work directory. Once live,
        # the process adds itself to the tree's owner marker beside every other
        # live user (``agent_scratch.adopt_owner``), so the sweep keeps the tree
        # while any of them runs.
        self._shared_scratch: Path | None = Path(shared_scratch) if shared_scratch else None
        self._scratch_dir: Path | None = None
        # What the pre-spawn freshness check verified, for the post-handshake half of
        # the bracket. ``None`` until a spawn takes it, and ``None`` for every agent
        # that mirrors no other spec.
        self._derived_spec_snapshot: Any = None

        # Recycling thresholds — see _is_stale(). Long-lived multiplexed
        # runtimes (e.g. the kirocrew-lite background runtime) have no
        # per-turn compaction, so age/RSS are the only signals available to
        # bound unbounded growth.
        #
        # The operator's values, held as the INPUT to the host's own policy: a
        # spawn passes them through ``harness.reclaim_policy`` so a host known to
        # leak faster can narrow them with no branch here, and a host that does
        # not narrow leaves the operator's configuration as the whole answer.
        # Applied at spawn rather than here because a runtime is constructible for
        # a backend the shared-process runtime has no harness for, and because
        # neither threshold means anything before a process exists.
        self._max_age_secs = max_age_secs
        self._max_rss_mb = max_rss_mb
        # Which processes the ceiling above is measured over. None = the whole
        # descendant subtree, which is every kiro-family host. The spawn plan carries
        # a bounded host's depth relative to the exact pid Crew launches.
        self._max_rss_depth: int | None = None

        # session/new + session/load budget — resolved lazily on first use
        # (never in __init__: KiroCrewConfig.load() is a synchronous disk
        # read + schema validation on a cache miss, and runtimes are
        # constructed on the event loop) and cached for the runtime's
        # lifetime. See _session_start_budget().
        self._session_start_timeout: float | None = None

        # Process state
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._start_time: str | None = None
        self._spawn_monotonic: float | None = None
        # pid -> (start_id, basename): the record shape session_pid verifies a
        # descendant's identity against before it signals one, so a recycled pid
        # is skipped. Written by _snapshot_descendants at spawn and on every
        # session start -- nothing populated it before, which left
        # _provider_descendant_records with only its live walk, and a walk can
        # run only while the root is alive.
        self._child_pids: dict[int, ChildRecord] = {}
        # Names THIS spawn of the shared child process (fresh per spawn, cleared
        # with the process) — the identity a resource minted by the child is
        # compared against later. See AcpClient.process_instance for why the
        # session id cannot serve: a resume reuses it on a new process.
        self._process_instance: str = ""

        # Single reader task — the ONLY coroutine that reads stdout
        self._reader_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]

        # Demux routing
        self._pending_requests: _PendingRequests = _PendingRequests()
        # session/new requests whose caller timed out but whose answer is still
        # owned (RFC §4.4); keyed by request id, settled by the collector.
        self._start_collectors: dict[int, StartCollector] = {}
        # Cleanup deadline for those collectors; resolved off-loop like the
        # session-start budget and cached for the runtime's lifetime.
        self._start_collect_timeout: float | None = None
        # Maps req_id → sessionId for responses that should be routed to a session queue
        # (e.g. session/prompt response signals turn completion and must reach the session)
        self._routed_requests: dict[int, str] = {}
        self._session_queues: dict[str, asyncio.Queue[JsonRpcMessage | None]] = {}
        # OAuth notifications can precede the session/new or session/load
        # response that reveals which queue to register. Stage only those
        # frames while an init is active, then transfer the matching session's
        # frames into its queue. The bounded buffer is cleared when the last
        # concurrent init finishes so an abandoned URL cannot reach a later
        # session that happens to reuse the same id. A start whose caller timed
        # out keeps its own copy on its StartCollector instead, so this deque
        # never outlives the init scope whose progress it describes (see
        # _stage_init_frame).
        self._session_inits_in_flight = 0
        self._pending_init_notifications: deque[JsonRpcMessage] = deque(
            maxlen=_INIT_NOTIFICATION_BUFFER_LIMIT
        )
        self._next_id = 1
        self._initialized = False
        # Whether kiro-cli advertised session/load support in its initialize
        # response. Mirrors AcpClient._can_load_session — load_session() guards
        # on it so we never issue session/load against a backend that lacks it.
        self._can_load_session = False
        # promptCapabilities from the initialize response (e.g. {"image": true}).
        # Empty until the handshake completes, so callers fail CLOSED and send
        # text-only rather than guessing a modality the agent never advertised.
        self._prompt_capabilities: dict = {}
        # The whole ``agentCapabilities`` object from the handshake. Retained
        # because a host's session-level MCP array has to be narrowed against
        # what it actually advertised: sending an element whose transport it
        # never declared can make it refuse the entire session/new rather than
        # skip that one server. Empty before the handshake, and empty on a host
        # that advertises nothing -- which the harness must read as "nothing is
        # known", never as "nothing is supported".
        self._agent_capabilities: dict = {}
        # agentInfo.version from the initialize response: the version of the
        # binary THIS process is executing, which can differ from the one on
        # disk after an in-place upgrade. Empty until the handshake completes.
        self._agent_version = ""
        # Entitlement probe state (probe_advertised_models): single-flight lock
        # plus a short-TTL cache of the last non-empty answer.
        self._entitlement_probe_lock = asyncio.Lock()
        # One descendant pass at a time. Each pass reads the tree and rewrites
        # this root's block from what it read, so two overlapping passes race:
        # the one that finishes last wins, and if that is the OLDER read, the
        # newer pass's descendants are dropped from both the record and the file.
        # Sessions start concurrently on a shared runtime, so this is reachable.
        self._descendant_scan_lock = asyncio.Lock()
        # Two independent clocks so a failure never extends the life of an old
        # success: ``_result_at`` is stamped ONLY when a non-empty result is
        # stored (that result may be replayed until it expires), while
        # ``_attempt_at`` is stamped on EVERY completed attempt incl.
        # failure/empty (it suppresses re-opening a session within the TTL but
        # never replays a stale result).
        self._entitlement_probe_result_at = 0.0
        self._entitlement_probe_attempt_at = 0.0
        self._entitlement_probe_result: list[dict[str, str]] = []
        self._dead = False
        self._death_summary: str | None = None
        # The composed summary's parts, so the post-reap amendment rebuilds the
        # line instead of editing its text -- a tail carrying this format's own
        # shape must never be mistaken for the status field.
        self._death_reason = ""
        self._death_label = ""
        self._death_tail = ""
        # Severity _mark_dead settled on, after its refuse-the-downgrade
        # guard. The post-reap amendment logs at the SAME severity, so an
        # operator filtering on WARNING never sees the death without the
        # exit code that followed it.
        self._death_expected = False
        self._last_activity: float = 0.0
        self._stderr_lines: list[str] = []
        # Latched auth-failure observation. ``_stderr_lines`` is a 20-line ring,
        # so on a noisy startup the auth line can be evicted before anything asks
        # about it — and the question is only ever asked LATER, once a request
        # times out or the runtime dies. Re-scanning a buffer that no longer holds
        # the evidence answers "no auth problem", which is indistinguishable from
        # a real negative. Latch on arrival instead: an auth failure observed once
        # stays observed for the life of this runtime, which is correct because
        # nothing about a rejected credential un-rejects itself mid-process.
        self._saw_auth_failure = False
        # Latched sandbox-init-refusal observation, latched for the same reason
        # and at the same sink as the auth latch above: the question ("why is this
        # runtime dead?") is asked after the ring has already turned over on a
        # chatty startup, and a re-scan that misses the line answers "not a
        # sandbox problem" indistinguishably from a real negative.
        #
        # Its LIFETIME is narrower than the auth latch's, though, and deliberately:
        # this one is a verdict about THIS CHILD'S STARTUP, and it is spent once
        # startup has demonstrably SUCCEEDED -- which is the first session handle,
        # not the ``initialize`` handshake. Startup continues past the handshake
        # through ``session/new``, and a sandboxed MCP launcher that the child
        # starts for that session can refuse there: closing the window at the
        # handshake would leave every such refusal unclassified, on the very
        # translation site (``create_session``) added to catch it.
        #
        # See the clear in ``_finish_create_session`` and the arming guard in
        # ``_drain_stderr``.
        self._saw_sandbox_init_failure = False
        # Whether a session handle has ever been produced on this runtime. The
        # startup window the latch above arms in, and the reason it is a separate
        # flag from ``_initialized``: the handshake is the middle of startup, not
        # its end.
        self._first_session_ready = False
        # Which isolation layer wrapped this runtime's child, recorded by
        # ``_spawn_admitted`` off the argv its wrap returned. False until then --
        # also the safe default for the classifier, since a spawn that never
        # reached the wrap cannot have been refused by it.
        self._sandbox_wrapped_by_crew = False
        # The mask set this spawn asked for, so a trusted corroboration run can
        # exercise the same mounts rather than a weaker profile.
        self._sandbox_hidden_dirs: tuple[str, ...] = ()
        # Unroutable-frame drop accounting: (sessionId, method) → count since
        # the last flush, plus the monotonic timestamp of that flush (0.0 = no
        # window open yet; the first counted drop opens it). Written ONLY from
        # _reader_loop (the single stdout owner) and its flush helper, so a plain
        # dict needs no lock — asyncio.ensure_future(self._reader_loop()) is
        # called exactly once, in spawn(), and never re-entered.
        self._dropped_frames: dict[tuple[str, str], int] = {}
        # In-flight SEL audit tasks for auto-rejected permission requests;
        # held only to keep them alive (see _answer_unroutable_permission).
        self._audit_tasks: set[asyncio.Task] = set()
        # In-flight ANSWER tasks (the coroutines that write the rejection
        # response), tracked separately from the SEL audit tasks above: the
        # flood cap below must count only tasks that can block on stdin
        # drain() — audit tasks are short-lived thread offloads, and letting
        # them satisfy the cap would let a burst of ordinary audits trip a
        # false mark_dead that kills every multiplexed session.
        self._answer_tasks: set[asyncio.Task] = set()
        # Volume bound for in-flight auto-answer tasks. Each task can block
        # on stdin drain() against a backend that floods permission frames
        # while never reading its stdin — unbounded, that grows the task set
        # until the gateway OOMs. Awaiting an answer inline on the reader
        # would hand the same hostile backend a demux freeze for every
        # session, so the bound treats a capacity timeout as a dead pipe
        # (see _wait_for_answer_capacity).
        self._max_answer_tasks: int = 128
        # Bounded discrimination wait at the cap (see
        # _wait_for_answer_capacity):
        # small enough that a wedged pipe is condemned promptly, large enough
        # that a responsive backend's in-flight answers can complete.
        self._answer_cap_wait_secs: float = 5.0
        # Backend-internal subagent session ids, snapshotted from each
        # `_kiro.dev/subagent/list_update` frame (a FULL list every time, so
        # replacement — not accumulation — keeps it bounded and current).
        # Membership proves an unregistered sessionId is a real backend child.
        # `_subagent_owner` records WHICH registered session was the sole
        # consumer when the announce arrived — routing later requires the sole
        # queue to still be that exact session, so a warm-reused runtime whose
        # session was swapped can never inherit a stale child's approvals.
        # Both are cleared when the owning session unregisters.
        self._subagent_sessions: set[str] = set()
        self._subagent_owner: str | None = None
        # How many ids the LAST roster named past NATIVE_CHILD_ROSTER_CAP.
        # Non-zero means an announced child may be unrecognisable here, so a
        # permission request for an unknown session is audited under
        # _ROSTER_OVERFLOW_REJECT_REASON instead of being called unregistered.
        self._subagent_roster_overflow: int = 0
        # Roster-truncation LOG accounting, the same shape _note_dropped_frame
        # uses: the first truncated snapshot of an episode warns, every later
        # one inside that episode is tallied here and folded into a throttled
        # DEBUG summary. `_roster_overflow_repeats` counts those later
        # snapshots, `_roster_overflow_peak` is the largest tail any of them
        # named, and `_roster_overflow_summary_at` is the monotonic timestamp
        # of the last summary — 0.0 means no episode is open, so the next
        # truncation is loud again. Written only from the demux loop's snapshot
        # handler and from unregister_session, so no lock is needed.
        self._roster_overflow_repeats: int = 0
        self._roster_overflow_peak: int = 0
        self._roster_overflow_summary_at: float = 0.0
        # Sessions with an ACTIVELY CONSUMING prompt dispatch loop, marked by
        # AcpSessionHandle.prompt() around its dispatch (all exit paths,
        # including timeout/cancel/synthetic completion, unmark in a finally).
        # _routed_requests is NOT usable for this: it also holds set_mode /
        # steer / config request ids, and a timed-out prompt leaves its entry
        # until the backend response arrives — either would make routing
        # believe a consumer exists and park a child request unread.
        self._turn_active_sessions: set[str] = set()
        self._dropped_frames_flushed_at: float = 0.0

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def spawn_monotonic(self) -> float | None:
        """Monotonic time this process was spawned, or ``None`` before spawn."""
        return self._spawn_monotonic

    @property
    def entitlement_probe_result_at(self) -> float:
        """Monotonic time the stored probe answer arrived (0.0 before any).

        :meth:`probe_advertised_models` serves either a fresh answer or a replay
        of this stored one; in both cases the answer is dated by this clock, so a
        caller that stores what it was served dates its snapshot from here rather
        than from its own call time, which for a replay would be LATER than the
        data and would raise its own freshness floor above it.
        """
        return self._entitlement_probe_result_at

    @property
    def work_scratch_dir(self) -> Path | None:
        """The session tree's work directory this process exposes as ``$KIROCREW_SCRATCH``.

        The inherited directory when this runtime joined an existing tree, else
        its own allocation; ``None`` before spawn or when allocation failed. This
        is what a spawn made ON BEHALF of a session on this runtime -- a
        companion runtime, a dedicated subagent process, a recycled successor --
        is handed as its ``shared_scratch``, so the whole tree keeps one work
        directory however many processes it spans.
        """
        return self._shared_scratch or self._scratch_dir

    @property
    def process_instance(self) -> str:
        """Identity of the CURRENT child process instance (``""`` when none).

        Fresh per spawn, so equality distinguishes the process that minted a
        resource from any successor — including one that resumed the same ACP
        session id. Liveness is asked separately (:meth:`is_alive`).
        """
        return self._process_instance if self._process is not None else ""

    @property
    def acp_backend(self) -> str:
        """Which ACP backend this runtime's process speaks.

        Public because the backend has to survive being read back off a
        started provider: the runtime is the only object that still knows it
        once ``AcpProvider`` swaps its placeholder client for a session
        provider.
        """
        return self._acp_backend

    @property
    def _harness(self) -> HarnessAdapter:
        """This backend's strategy object -- the runtime's only per-host answer.

        Every per-host question is asked here, so a host added later answers all
        of them in one file rather than in scattered arms nothing points at.

        ``harness_for`` raises ``ValueError`` for a backend with no harness, and
        that is the intended outcome: silently inheriting kiro-cli's argv,
        protocol version and teardown verb would start a session that then
        behaves wrongly, which is far harder to attribute than a refusal here.
        """
        # ``getattr`` for the same reason the projection path uses it: a caller
        # that only needs the agent projection constructs a bare runtime with
        # ``object.__new__`` and sets the two fields it cares about, so the cache
        # slot may not exist. Same lazy resolution either way.
        harness = getattr(self, "_harness_resolved", None)
        if harness is None:
            harness = harness_for(self._acp_backend)
            self._harness_resolved = harness
        return harness

    @property
    def uses_kiro_identity_store(self) -> bool:
        """True when this runtime's process signs in from kiro-cli's own store.

        Membership in ``backends_retired_by_host_logout()`` (harness-parity
        H5/H14). ``AcpRuntime`` is not an ``LLMProvider``, but the identity-change
        sweep reaches shared runtimes as well as session providers, so it
        declares the same capability under the same name -- letting that sweep
        ask both families one question instead of probing private attributes.
        """
        return self._acp_backend in backends_retired_by_host_logout()

    @property
    def supports_image_prompt(self) -> bool:
        """True when the agent advertised ``promptCapabilities.image``.

        Fails closed: an un-handshaked or silent backend reports False, so the
        prompt path sends text only instead of an image block the agent may
        reject.
        """
        return bool(self._prompt_capabilities.get("image", False))

    @property
    def agent_version(self) -> str:
        """``agentInfo.version`` the agent reported at ``initialize`` (``""`` until then).

        This is the version of the binary the process is RUNNING, which is what
        a capability decision about a live session must key on: after an
        in-place kiro-cli upgrade the file on disk is newer than every process
        spawned before it.
        """
        return self._agent_version

    def is_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return self._process is not None and self._process.returncode is None and not self._dead

    def death_summary(self) -> str | None:
        """One-line death attribution, or None while alive.

        Composed once by ``_mark_dead`` (reason + returncode + stderr tail).
        Lets a consumer that only observes the death through a poisoned
        session queue — e.g. a turn's frame wait — report WHO/WHY instead
        of a bare "process died".
        """
        return self._death_summary

    def _stale_by_age(self) -> bool:
        """True if uptime exceeds max_age_secs. Cheap, no I/O — safe to call
        under a lock. Does NOT consider RSS (see _is_stale for that).

        NOT a recycle predicate: RSS, not age, is the growth mode this class
        was observed failing on, so a reuse decision MUST ask _is_stale().
        Reaching for this one because it is cheaper is what left the shared
        background runtime unbounded. No production caller today.
        """
        if self._pid is None or self._spawn_monotonic is None:
            return False
        return (time.monotonic() - self._spawn_monotonic) > self._max_age_secs

    async def _is_stale(self) -> str | None:
        """Return the recycle reason ('age' or 'rss'), or None if not stale.

        Distinct from is_alive(): a runtime can be perfectly healthy (process
        running, protocol responsive) yet still be "stale" — e.g. the
        kirocrew-lite background runtime observed growing unbounded (multi-GB
        RSS) over ~24h of uptime because the multiplexed design has no per-turn
        compaction or lifetime cap. Callers should check this alongside
        is_alive() and stop reusing a stale process: kill() and respawn when the
        active session count is 0, and otherwise DETACH it (park it to drain,
        respawn for new callers, reap on its last unregister) rather than
        deferring. Waiting for an idle window is not a bound — a multiplexed
        runtime under sustained background load never has one, which is how the
        multi-GB growth above went unchecked.

        RSS is measured across the whole descendant tree (_get_rss_tree_mb):
        under the Linux namespace sandbox self._pid is the launcher parent, and
        the real kiro-cli child is what grows. The RSS probe shells out / reads
        /proc, so it is offloaded to subprocess_executor() to keep the event
        loop free.

        The RSS probe is gated behind _RSS_PROBE_MIN_AGE_SECS: a freshly-(re)used
        runtime returns None without any executor round-trip, so the hot reuse
        path in get_bg_session (which holds _bg_runtime_lock) stays CPU-only for
        young runtimes. The lock IS deliberately held across the probe for older
        runtimes, busy or idle; the age gate bounds how often that happens, and a
        runtime that answers "stale" is displaced rather than re-probed.
        """
        if self._pid is None:
            return None

        if self._spawn_monotonic is not None:
            age = time.monotonic() - self._spawn_monotonic
            if age > self._max_age_secs:
                return "age"
            if age < _RSS_PROBE_MIN_AGE_SECS:
                # Too young to have grown — skip the offloaded RSS probe.
                return None

        rss_mb = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), _get_rss_tree_mb, self._pid, self._max_rss_depth
        )
        if self._max_rss_depth is not None:
            # A bounded scope rests on a structural fact about the host's process
            # tree (which generation the resident process sits at). Record what the
            # bound captured against the ceiling it is judged by, so a host release
            # that moves that process out of scope is attributable from the log
            # rather than showing up only as a ceiling that never trips.
            logger.debug(
                "rss probe: backend=%r pid=%s depth=%s rss_mb=%s ceiling_mb=%s",
                self.acp_backend,
                self._pid,
                self._max_rss_depth,
                rss_mb,
                self._max_rss_mb,
            )
        if rss_mb is not None and rss_mb > self._max_rss_mb:
            return "rss"

        return None

    def has_active_sessions(self) -> bool:
        """True if any session is currently registered on this runtime.

        Killing a runtime while a co-tenant session is registered drops that
        session's in-flight prompt/response. Every recycle path now asks
        ``has_active_or_initializing_sessions`` instead, which closes the
        registration window this one leaves open; no production caller remains.
        """
        return bool(self._session_queues)

    def has_active_or_initializing_sessions(self) -> bool:
        """True if any session is registered OR still being created.

        ``has_active_sessions`` sees only REGISTERED queues, and
        ``create_session`` registers outside the runtime lock -- so a co-tenant
        whose ``session/new`` is in flight is momentarily invisible to it, and
        killing the runtime under it surfaces as ``AcpRuntimeDead`` on work the
        user never connected to whatever prompted the kill.

        This is therefore the predicate every recycle and displacement decision
        asks, because it also counts ``_session_inits_in_flight``: a runtime with
        an initializing session is treated as busy and parked to drain rather
        than killed, so no caller has to absorb that window with a respawn.
        The init scope opens before ``create_session``'s admission gate, so a
        claim still queued behind the gate already counts.
        """

        return bool(self._session_queues) or self._session_inits_in_flight > 0

    # ── Lifecycle ──

    def _discard_sandbox_cleanup(self) -> None:
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

    async def _discard_bound_workspace(self) -> None:
        """Close the parent copy of a macOS workspace identity off-loop.

        Also releases the Windows work-dir chain pins (``_pin_work_dir_chain``):
        both are the parent's hold on the directory the child entered, and both
        end with the process.
        """
        descriptor = getattr(self, "_bound_workspace_fd", None)
        self._bound_workspace_fd = None
        chain = getattr(self, "_work_dir_chain", [])
        self._work_dir_chain = []
        work_dir = getattr(self, "_work_dir", None)
        if work_dir is not None:
            self._spawn_work_dir = str(work_dir)
        if descriptor is not None:
            await release_bound_agent_workspace(descriptor)
        if chain:
            await asyncio.to_thread(platform_compat.release_directory_chain, chain)

    async def _pin_work_dir_chain(self) -> None:
        """Windows: hold every directory on the caller-named work dir across the spawn.

        The work dir arrived as a canonical string, validated moments or minutes
        earlier by whichever surface named it. A pathname is re-resolved by
        ``CreateProcess`` and again by the child, and a same-user writer who can
        replace a component with a junction in between aims that resolution at
        a share -- an outbound SMB/NTLM authentication carrying the gateway's
        credentials, irreversible once emitted. There is no handle-based
        ``lpCurrentDirectory``, so the chain is pinned instead
        (:func:`platform_compat.pin_directory_chain`): every component is opened
        without following and held without ``FILE_SHARE_DELETE``, so nothing on
        the path can be renamed or replaced while the child is created and for
        as long as the process lives (released with the macOS binding in
        ``_discard_bound_workspace``). A component that is ALREADY a reparse
        point fails the spawn: the string the surface validated does not name
        what it validated. POSIX is covered by the macOS descriptor binding and
        the Linux sandbox and is left as is.
        """
        if not (_PIN_WORK_DIR_CHAIN and self._work_dir_is_named):
            return

        def _pin() -> tuple[list[int], str]:
            # The session pool hands its default cwd over EXPLICITLY too -- the
            # realpath of the default work dir -- and that is the runtime's own
            # tree, not an operator-named one: pinning it would hold the data
            # home's directories from the gateway for as long as any pooled
            # agent lives, which is exactly the hold a shutdown or a pod
            # teardown must not meet. Compared with ``compare_key``: on the
            # Windows rule that is string work only, so neither the default (a
            # tree the agent can write under) nor the named work dir is
            # resolved by name inside the very guard against a re-resolved name.
            own_default = platform_compat.compare_key(default_work_dir())
            if platform_compat.compare_key(self._spawn_work_dir) == own_default:
                return [], self._spawn_work_dir
            return platform_compat.pin_directory_chain_bound(self._spawn_work_dir)

        try:
            # The child is created under the spelling the chain was opened
            # under: on the binding rule the volume's own identity, so the
            # kernel's open of the current directory at process creation goes
            # through the volume, not through a letter a writer can rebind
            # between the pin and the create. The peer-facing session cwd
            # (``_session_work_dir``) stays the validated DOS spelling.
            self._work_dir_chain, self._spawn_work_dir = await asyncio.to_thread(_pin)
        except OSError as exc:
            raise RuntimeError(
                f"refusing to start the agent in {self._spawn_work_dir!r}: a directory on "
                f"that path could not be pinned ({exc.strerror or exc.__class__.__name__}); "
                "a component may have been replaced by a symlink or junction since the "
                "directory was validated."
            ) from exc

    async def _session_work_dir(self, cwd: str | Path | None = None) -> str | Path:
        """Resolve an ACP cwd without re-authorizing a mutable macOS pathname."""
        if self._bound_workspace_fd is None:
            return cwd if cwd else self._work_dir
        requested = cwd if cwd else self._work_dir
        # The shared rule lives in sandbox.resolve_bound_session_workspace; only the
        # error mapping is this front end's. What the peer receives is the BOUND
        # DESCRIPTOR's own name, not the caller's spelling -- that one is what a
        # symlink swap controls, and it can name a descendant this check never
        # covered.
        #
        # What no string here can do is bind the PEER's own resolution.
        # ``session/new`` carries a cwd STRING that a separate process re-resolves
        # after this returns, so a same-UID rename of the canonical directory in that
        # window remains open; that is a property of the protocol boundary, not of the
        # spelling. ``/dev/fd/<n>`` is not the alternative: the binding is darwin-only
        # (see bind_voice_safe_agent_workspace, which returns no descriptor off
        # macOS), and macOS cannot resolve those entries at all -- the very bug this
        # change exists to fix, i.e. that spelling never delivered a working session
        # cwd, let alone a safer one. The agent PROCESS's own cwd is pinned by
        # descriptor at spawn (create_subprocess_limited's chdir_fd), which is the
        # part that does not go through a name.
        try:
            return await resolve_bound_session_workspace(self._bound_workspace_fd, requested)
        except BoundWorkspaceMismatch as exc:
            raise AcpWorkspaceBindingError(
                "A delegated macOS Kiro runtime is bound to one exact workspace; "
                "create a runtime bound to the requested workspace"
            ) from exc
        except OSError as exc:
            raise AcpWorkspaceBindingError(
                "Cannot verify the requested macOS session workspace"
            ) from exc

    async def _to_thread_guarding_sandbox(
        self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
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

    async def _resolve_spawn_plan(self) -> SpawnPlan:
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
        return plan

    async def _initialize_handshake(self, client_capabilities: dict[str, Any]) -> dict[str, Any]:
        """Send ``initialize`` with a budget sized to the host's throttle state.

        ``client_capabilities`` is the declaration :meth:`spawn` resolved before
        the process existed (the harness constant, or the wire-filled variant a
        ``client_meta_settings`` host gets); this helper only owns the budget.

        The plain budget assumes an unthrottled process. When the agents slice
        is being throttled at spawn time the budget is
        :data:`_INIT_TIMEOUT_UNDER_THROTTLE` instead, because a throttled
        kiro-cli is alive and answering slowly, not hung -- killing it at the
        plain deadline discards its startup and the retry pays the same
        startup into the same throttle. When the throttle is first seen at the
        plain deadline with the process still ALIVE, the same ``initialize``
        request stays pending for the remainder of the extended budget (a
        fresh gateway's first probe can only baseline the counter, so the
        spawn-time read can miss a throttle already under way). A timeout that
        lands while the process is still ALIVE and the slice is throttling is
        re-raised as :class:`AcpRuntimeOverloaded`, so the failure names
        overload instead of a killed process. Any other timeout, and an exited
        process, propagate unchanged.
        """
        throttled_at_spawn = agents_slice_throttling()
        timeout = _INIT_TIMEOUT_UNDER_THROTTLE if throttled_at_spawn else _REQUEST_TIMEOUT
        if throttled_at_spawn:
            logger.warning(
                "acp_startup_stage stage=initialize outcome=throttled_budget "
                "timeout_budget_s=%g pid=%s: the agents slice is being throttled, "
                "so this handshake gets the extended budget",
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
            pending = getattr(exc, "adopted_future", None)
            if not throttled_at_spawn and pending is not None:
                # The throttle surfaced only at the plain deadline (a fresh
                # gateway's first probe can only baseline the counter, and
                # reclaim can hold usage just under the line at spawn). The
                # process is alive and the request is still registered, so
                # give it the rest of the extended budget instead of reaping
                # a startup that is merely slow.
                extension = _INIT_TIMEOUT_UNDER_THROTTLE - timeout
                logger.warning(
                    "acp_startup_stage stage=initialize outcome=late_throttle_extension "
                    "timeout_budget_s=%g extension_s=%g pid=%s: the agents slice began "
                    "throttling during the handshake; keeping the request pending",
                    timeout,
                    extension,
                    self._pid,
                )
                try:
                    return await asyncio.wait_for(pending, timeout=extension)
                except asyncio.TimeoutError:
                    self._pending_requests.pop(getattr(exc, "req_id", None), None)
                    timeout = _INIT_TIMEOUT_UNDER_THROTTLE
            raise AcpRuntimeOverloaded(
                f"initialize went unanswered for {timeout:g}s while the agents slice is "
                "being throttled at its memory ceiling; the process was alive, not hung. "
                "Free agent memory (close idle sessions, run fewer concurrent subagents) "
                "and retry."
            ) from exc

    async def spawn(self) -> None:
        """Start the ACP runtime behind the gateway-wide cold-start admission gate."""
        if self._process is not None:
            raise AcpRuntimeError("Runtime already spawned")

        admission = _cold_start_admission()
        wait_ms = await admission.acquire()
        logger.info(
            "acp_cold_start stage=queue_wait outcome=admitted wait_ms=%.1f "
            "active_starts=%d queued_starts=%d",
            wait_ms,
            admission.active,
            admission.queued,
        )
        started = time.monotonic()
        outcome = "error"
        try:
            await self._spawn_admitted_rederiving_once()
            outcome = "ready"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            process = self._process
            if process is None:
                process_state = "absent"
            elif process.returncode is None:
                process_state = "running"
            else:
                process_state = "exited"
            logger.info(
                "acp_cold_start stage=spawn outcome=%s duration_ms=%.1f backend=%s "
                "active_starts=%d queued_starts=%d process_state=%s",
                outcome,
                (time.monotonic() - started) * 1000.0,
                self._acp_backend or "kiro",
                admission.active,
                admission.queued,
                process_state,
            )
            admission.release()

    async def _spawn_admitted_rederiving_once(self) -> None:
        """``_spawn_admitted``, retried ONCE when the post-handshake bracket fires.

        The bracket around a derived spec's load fails CLOSED: a write to the default
        spec landing between the pre-spawn gate and the ``initialize`` response kills
        the child, because the spec it loaded may be a generation nobody verified. That
        is the right answer for a revocation. It is the WRONG surface for a benign write
        -- the dashboard's MCP sync, an app registration, the periodic rebuild -- whose
        only effect on the worker is that its mirror needs re-deriving, and which
        happens to land inside a spawn's few-hundred-millisecond window. Those are
        ordinary events, and surfacing each as a failed dispatch would make the fleet
        flaky exactly when an operator is changing things.

        So: one retry, and only for THAT refusal. The second attempt runs the pre-spawn
        gate again, which re-derives the mirror from the default as it now stands, and
        spawns a fresh child on it. If the default is still moving the second bracket
        fires too and the refusal propagates -- a file that will not hold still across
        two spawns is not a benign write. Never a loop: the exit condition is another
        process leaving the file alone.

        Narrow on purpose. ``DerivedSpecStale`` from ``_initialize`` is the post-check;
        the pre-spawn gate's own refusal (a mirror that CANNOT be re-derived) arrives as
        ``AcpRuntimeError`` and is not retried, because a second attempt would fail the
        same way for the same reason.
        """
        from kiro_crew.agent import DerivedSpecStale

        try:
            await self._spawn_admitted()
        except DerivedSpecStale as first:
            logger.info(
                "acp_cold_start stage=rederive outcome=retry backend=%s reason=%s",
                self._acp_backend or "kiro",
                first,
            )
            try:
                await self._spawn_admitted()
            except DerivedSpecStale as second:
                raise AcpRuntimeError(str(second)) from second

    async def _spawn_admitted(self) -> None:
        """Spawn and initialize after the caller has acquired cold-start admission."""
        if self._process is not None:
            raise AcpRuntimeError("Runtime already spawned")

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
        await asyncio.to_thread(self._work_dir.mkdir, parents=True, exist_ok=True)
        # Delegated Kiro agents on macOS do not inherit Kiro Crew's Seatbelt
        # deny rules. Keep their workspace disjoint from the named voice-decoder
        # runtime so verified executable bytes cannot be replaced before spawn.
        if self._harness.internal_sandbox:
            await asyncio.to_thread(assert_voice_runtime_outside_agent_workspace, self._work_dir)

        try:
            plan = await self._resolve_spawn_plan()
            self._max_rss_depth = plan.rss_depth
            argv = plan.argv
            if self.acp_backend == ACP_BACKEND_KIRO:
                from kiro_crew.acp.skill_projection import prepare_native_skill_projection

                self._native_skill_projection = await asyncio.to_thread(
                    prepare_native_skill_projection, self._work_dir
                )
                if self._native_skill_projection is not None:
                    argv = list(argv)
                    agent_position = argv.index("--agent") + 1
                    argv[agent_position] = self._native_skill_projection.agent(self._agent)
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

        # OSS sandbox.wrap_argv supports (argv, mode, strip_python_env). The
        # MCP-gateway overlay is NOT delivered through the sandbox: its broker
        # stubs are injected at ACP session/new (see new_session), so pooling
        # needs no bind-mount and works with sandbox mode "off". strip_python_env
        # IS applied to keep the host PYTHONPATH/PYTHONHOME out of kiro-cli's
        # foreign MCP subprocesses (which bundle their own interpreter + deps).
        # is_kiro_cli drives the reviewed Kiro internal-sandbox delegation: on
        # macOS wrap_argv skips its seatbelt because the two cannot nest; on
        # Windows the official Kiro backend delegates by default because Crew
        # has no native OS sandbox there. Answered by the harness, which reads
        # ACP_BACKENDS_INTERNAL_SANDBOX (harness-parity H7) — never as "not KAS":
        # that test fails OPEN, so a harness inheriting a negative test would have
        # Crew's seatbelt skipped in favour of an internal sandbox that never
        # starts. KAS is a Node process with no internal sandbox, so it takes
        # Crew's seatbelt directly, and so does every harness added later.
        #
        # Inside a pod apply_pod_bundle_spawn answers both questions instead, from
        # the single reason recorded on that function: the pod HOME remap breaks
        # the toolbox shim's own sandbox, so the child runs the bundle binary the
        # shim itself falls back to and Crew's launcher wraps it. Off-loop because
        # the resolution stats the candidate path.
        argv, delegate_internal_sandbox = await asyncio.to_thread(
            apply_pod_bundle_spawn, argv, backend=self._acp_backend
        )
        # The host's credential mask, resolved with its argv and applied here.
        # Empty for a host whose privileged tools ask by construction; for one this
        # core's tool gate ENFORCES it is the compensating control, so a spawn that
        # dropped it would hand a third-party binary the operator's credential homes.
        # Per-process scratch containment (twin of acp/client.py). Allocated
        # BEFORE the wrap: the scratch ROOT is masked for every sandboxed
        # process, so this runtime's own directory is carved back out.
        if self._shared_scratch is None and self._scratch_dir is not None:
            # A respawn of this runtime: its previous process's directory IS
            # the tree its sessions and their children use (twin of
            # acp/client.py) -- join it rather than start an empty one.
            self._shared_scratch = self._scratch_dir
        self._scratch_dir = None
        try:
            self._scratch_dir = await self._to_thread_guarding_sandbox(
                agent_scratch.allocate_scratch, "runtime"
            )
        except (OSError, agent_scratch.ScratchBoundaryError):
            # The boundary refusal joins OSError HERE and deliberately not at
            # record_owner below: no child exists yet, so there is nothing to
            # stop, and scratch is hygiene rather than a spawn prerequisite.
            logger.warning(
                "agent-scratch: could not allocate; spawning with inherited temp",
                exc_info=True,
            )
        scratch_window = (str(self._scratch_dir),) if self._scratch_dir is not None else ()
        # The session tree's work directory, when this runtime is not the tree's
        # first process: a second window into the masked root, re-validated now
        # because the allocation it names may have been swept since it was
        # recorded (``shared_scratch_window`` answers None for anything that is
        # not a plain directory under the root, and the spawn then carries on
        # with the runtime's own directory alone).
        if self._shared_scratch is not None:
            self._shared_scratch = await self._to_thread_guarding_sandbox(
                agent_scratch.shared_scratch_window, self._shared_scratch
            )
        if self._shared_scratch is not None:
            scratch_window = (*scratch_window, str(self._shared_scratch))
        # Resolve the SSH_AUTH_SOCK forward opt-in off-loop ONCE
        # (config read) and pass it to both the sandbox wrap and the parent scrub
        # below, so neither reads config on the loop. Scoped to this agent spawn.
        forward_ssh_auth_sock = await asyncio.to_thread(_forward_ssh_auth_sock)
        argv, self._sandbox_cleanup = await wrap_argv_async(
            argv,
            mode=self._sandbox_mode,
            strip_python_env=True,
            forward_ssh_auth_sock=forward_ssh_auth_sock,
            is_kiro_cli=delegate_internal_sandbox,
            extra_hidden_dirs=plan.extra_hidden_dirs,
            extra_private_dirs=scratch_window,
            extra_expose_files=plan.extra_expose_files,
            _prepare=wrap_argv,
        )
        # Twin of acp/client.py's record: the wrap's own account of the branch it
        # took, read before the cgroup scope below prepends its tokens. A later
        # re-derivation from mode + platform + settings cannot match it -- the
        # delegated branch still falls back to Crew's seatbelt for a masked spawn,
        # and the audit-or-deny step can refuse a delegation after it was chosen.
        self._sandbox_wrapped_by_crew = wrapped_by_crew_sandbox(argv)
        self._sandbox_hidden_dirs = tuple(plan.extra_hidden_dirs)
        # cgroup v2 scope (OUTERMOST): bound this agent + all its MCP-server /
        # tool descendants with pids.max (fork bomb) + memory.max (RSS balloon).
        # No-op + loud warning where cgroup delegation is unavailable. --scope
        # execs into the target, so self._pid below is still the real child.
        # Off-loop: first call probes /proc + /sys and the config read touches
        # the config dir (mkdir + file read) — blocking syscalls that must not
        # run on the loop. Guarded: wrap_argv above allocated the sandbox temp
        # file, so a cancellation here must not orphan it.
        argv = await self._to_thread_guarding_sandbox(cgroup_scope_argv, argv)

        env = {**os.environ}
        if self._extra_env:
            env.update(self._extra_env)

        env["PATH"] = augmented_path(env.get("PATH", ""))

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
            # kiro-cli is handed it for its v2 agent loop. The KAS relay has it
            # REMOVED even though its process is now a kiro-cli: the v3 engine
            # authenticates either from kiro-cli's OIDC store
            # (--auth-method cli) or from Crew's vault over the
            # _kiro/auth/getAccessToken callback, and in BOTH shapes the
            # variable must be absent — the engine gives an API key in its
            # environment precedence over the callback, so leaving it set would
            # silently override the credential the operator signed in with.
            # Called here, before the scrub below, so a host can both add its own
            # variables and remove one this generic path would pass through.
            self._harness.apply_spawn_env(env)

        await self._to_thread_guarding_sandbox(_resolve_env_off_loop)
        # Parent-side equivalent of the launcher scrub. This is required on
        # Windows where the positively classified Kiro backend delegates to the
        # CLI's internal sandbox without a POSIX `env -u` wrapper. Do it after
        # credential-pointer/API-key resolution so no resolver can reintroduce a
        # denied variable; KIRO_API_KEY itself is intentionally not denied.
        env = scrub_agent_subprocess_env(env, forward_ssh_auth_sock=forward_ssh_auth_sock)
        # Bundled skill scripts must not depend on a system ``python`` name.
        # The desktop bundles carry their interpreter outside the user's PATH,
        # while this path is already running under the exact environment that
        # can import ``kiro_crew``. Overwrite after the scrub and after
        # ``extra_env`` so agent configuration cannot redirect the trusted read
        # gate to a foreign interpreter.
        env["KIROCREW_RUNTIME_PYTHON"] = sys.executable
        # Pod-scoped kiro-cli children write their OWN MCP OAuth grants,
        # confined to the pod's tree instead of the real host's -- see
        # acp.client._apply_pod_home_remap's docstring. No-op outside a pod and
        # for every host whose harness answers False. That answer reads
        # ACP_BACKENDS_POD_HOME_REMAP, its own membership set rather than a reuse
        # of the internal-sandbox one: "carries its own OS sandbox" and
        # "relocating HOME moves its credential store" are different questions
        # (harness-parity H6), and conflating them is what this gate is against.
        env = _apply_pod_home_remap(env, pod_home_remap=self._harness.pod_home_remap)
        # Positive-identity marker for the orphan sweep: kiro-cli and every MCP
        # server it spawns inherit this, so escaped launcher trees (``npx
        # @playwright/mcp`` -> node) are identifiable as ours.
        env[KIROCREW_SPAWNED_ENV] = KIROCREW_SPAWNED_VALUE
        # The incarnation this spawn is. Minted here, before the process exists,
        # because it has to travel in the child's environment: it is what a
        # teardown reads back out of /proc/<pid>/environ to prove a process is
        # THIS spawn's descendant once the root itself is gone. Random rather
        # than pid-derived so a recycled pid cannot false-match.
        spawn_instance = uuid.uuid4().hex[:16]
        env[KIROCREW_SPAWN_INSTANCE_ENV] = spawn_instance
        # Own browser session per agent process, matching AcpClient._spawn (see
        # browser_session_env). Per PROCESS, not per agent: with session sharing
        # on (the default) an eligible subagent's session is created on the
        # PARENT's runtime, so a parent and its subagents share this process and
        # therefore one browser; a task-runner run is a separate family sharing
        # one run-scoped process. What this buys is isolation BETWEEN families,
        # which is where the reported corruption came from. The docs tell an
        # agent sharing a process with a concurrent browser user to pass -s=.
        browser_env = browser_session_env(env)
        env.update(browser_env)
        if browser_env:
            lifecycle_env = {**os.environ, **browser_env}
            env.update(await self._to_thread_guarding_sandbox(browser_socket_env, lifecycle_env))
        # Per-process scratch containment: the agent's temp AND its
        # prompt-guided work products land in the owned directory allocated
        # before the sandbox wrap, instead of the shared system temp dir.
        # Fail-open -- scratch is hygiene, not a spawn prerequisite. The
        # owner pid is recorded after spawn; reclamation is liveness-keyed
        # (agent_scratch.sweep_dead_scratch), never age-keyed.
        if self._scratch_dir is not None:
            env.update(agent_scratch.scratch_env(self._scratch_dir, shared=self._shared_scratch))
        elif self._shared_scratch is not None:
            # Own allocation failed (inherited temp) but the tree's work
            # directory is mounted: the prompt-visible name still points there.
            env["KIROCREW_SCRATCH"] = str(self._shared_scratch)
        # Memory-aware cap for pytest-xdist's ``-n auto`` (subagent spawn path —
        # mirrors acp/client.py): xdist sizes auto to the CPU count, ignoring
        # memory; PYTEST_XDIST_AUTO_NUM_WORKERS bounds ONLY auto resolution.
        # Respects a pre-set value; see resource_status.inject_xdist_auto_cap.
        # Off-loop: resolving the cap reads the raw config, and that read
        # enters config_dir() (mkdir + file IO + JSON parse) — blocking
        # syscalls that must not run on the loop. Guarded: the sandbox temp
        # file is live, so a cancellation here must not orphan it.
        await self._to_thread_guarding_sandbox(inject_xdist_auto_cap, env)

        await self._discard_bound_workspace()
        if self._harness.internal_sandbox:
            self._spawn_work_dir, self._bound_workspace_fd = (
                await bind_voice_safe_agent_workspace_async(self._work_dir)
            )
        try:
            await self._pin_work_dir_chain()
            self._process = await platform_compat.create_windows_cleanup_owned_process(
                functools.partial(
                    _retrying_spawn_factory,
                    functools.partial(
                        create_subprocess_limited,
                        *argv,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self._spawn_work_dir,
                        limit=_STDOUT_BUFFER_LIMIT,
                        # POSIX: setsid so kill() can killpg the whole tree. Windows:
                        # start_new_session is silently ignored; CREATE_NEW_PROCESS_GROUP
                        # makes the child tree taskkill /T-reapable (see platform_compat
                        # spawn-isolation note). CREATE_NO_WINDOW suppresses the console
                        # window Windows would otherwise pop for this console child spawned
                        # from the windowless gateway (0 on POSIX, so no effect there).
                        start_new_session=platform_compat.IS_POSIX,
                        creationflags=(
                            platform_compat.CREATE_NEW_PROCESS_GROUP
                            | platform_compat._SUBPROCESS_NO_WINDOW
                            | platform_compat.CREATE_SUSPENDED
                        ),
                        # None off macOS, where nothing binds. When set, the child enters
                        # the workspace through this verified descriptor instead of
                        # resolving ``cwd``'s pathname, which a same-UID symlink retarget
                        # could aim elsewhere in between; ``cwd`` stays the same directory
                        # by name so the spawn keeps reporting a real path.
                        chdir_fd=self._bound_workspace_fd,
                        env=env,
                        profile=RLIMIT_PROFILE_SESSION_HOST,
                    ),
                ),
            )
        except BaseException:
            await self._discard_bound_workspace()
            self._discard_sandbox_cleanup()
            raise
        self._pid = self._process.pid
        # The same token the child carries in its environment (minted above, so
        # it could be passed in); random, not pid-derived, so it cannot
        # false-match a later spawn that the OS handed a recycled pid.
        self._process_instance = spawn_instance
        # The subprocess is LIVE from here on but nothing has recorded it yet, so
        # this window needs the same guard AcpClient._spawn has. finish_suspended_spawn
        # documents its own resume failure as FATAL, and the identity read can fail;
        # all four runtime.spawn() callers (providers/acp.py:726, :825 catch
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
        try:
            _track_pid(self._pid)
            _track_session_pid(self._pid)
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

        # Everything after the subprocess exists must be guarded: if reader
        # startup or the initialize handshake fails (kiro-cli hang / auth stall),
        # the process, its reader/stderr tasks, its PID-file entries AND its
        # _PROTECTED_PIDS shield would all leak. kill() reaps them (and
        # unregisters the protected PID via _mark_dead) before we re-raise.
        # BaseException so CancelledError during the 30s handshake also cleans up.
        try:
            # Start stderr drain
            if self._process.stderr:
                self._stderr_task = asyncio.ensure_future(self._drain_stderr())

            # Start the single reader task — owns stdout exclusively
            self._reader_task = asyncio.ensure_future(self._reader_loop())

            # Protocol handshake ("initialize"); the budget follows the host's
            # throttle state -- see _initialize_handshake. The capabilities
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
            logger.info("AcpRuntime initialized (PID %d)", self._pid)
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
            # BEFORE the kill, which is the whole point of the ordering. The
            # cleanup below cancels the stderr drain, and a line still in the pipe
            # when it does is a line nobody will ever read -- so the caller's own
            # settle finds the task already done and learns nothing. Draining here
            # is the last moment the child's own account of why it could not start
            # is still reachable, and a sandbox refusal is exactly the failure that
            # arrives this way: the child writes its signature and closes stdout
            # together. Bounded and swallowing (see ``settle_stderr``); this is
            # already the failure path.
            await self.settle_stderr()
            try:
                # This death IS abnormal (failed spawn/handshake): kill()'s
                # expected=False default keeps its log at WARNING.
                await self.kill(reason="failed init handshake cleanup")
            except Exception:
                logger.debug(
                    "AcpRuntime: cleanup kill after failed spawn/handshake failed", exc_info=True
                )
            raise

    #: One retry for a descendant scan that came back empty. The spawned root
    #: is a launcher that forks the agent, which forks again, so a scan racing a
    #: cold start can legitimately see nothing. A single retry is enough because
    #: every later session start scans again. Class attribute so tests can zero
    #: it rather than pay it.
    _DESCENDANT_RESCAN_DELAY = 0.5

    #: Why a teardown reached nothing, per ``_root_identity`` verdict. "holds" is
    #: reachable here too: the tree kill ran and the root exited under it, which
    #: is a race, not an identity failure.
    _ROOT_UNREACHED_REASON_BY_VERDICT = {
        "holds": "its root exited between the identity check and the signal",
        "mismatch": "its number is another process's now",
        "unknown": "its identity could not be read",
    }

    def _root_identity(self) -> str:
        """``"holds"``, ``"mismatch"`` or ``"unknown"`` for this runtime's root.

        ``_start_time`` is read once, at spawn (``get_process_start_id``), and a
        pid plus a start instant name one process for good: two processes on the
        same number at different times cannot share it.

        The three answers are not two. ``"mismatch"`` is a MEASUREMENT: both
        identities were read and they differ, so the number is provably someone
        else's. ``"unknown"`` is the absence of one -- nothing was recorded at
        spawn, or the live read failed, which
        :func:`platform_compat.get_process_start_id` also returns for a pid that
        is simply gone. Both refuse authorization, and callers must treat them
        alike when deciding, but only the first may DESCRIBE the root: saying "no
        longer the process spawned" about a read that never happened sends a
        diagnostic after the wrong cause.
        """
        pid = self._pid
        recorded = self._start_time
        if pid is None or recorded is None:
            return "unknown"
        live = platform_compat.get_process_start_id(pid)
        if live is None:
            return "unknown"
        return "holds" if live == recorded else "mismatch"

    def _root_identity_holds(self) -> bool:
        """Whether the root's identity is PROVEN to still be ours.

        Refuses on ``"unknown"`` as firmly as on ``"mismatch"`` -- recording or
        signalling a tree we cannot prove is ours is how a stranger gets
        signalled -- and logs only what it measured.
        """
        verdict = self._root_identity()
        if verdict == "mismatch":
            logger.warning(
                "AcpRuntime: root PID %d is no longer the process spawned for this "
                "runtime -- recording nothing",
                self._pid,
            )
        elif verdict == "unknown":
            logger.debug(
                "AcpRuntime: root PID %s identity is unreadable -- treated as not "
                "ours, so nothing is recorded or resolved from its number",
                self._pid,
            )
        return verdict == "holds"

    async def _snapshot_descendants(self, *, retry_when_empty: bool = False) -> None:
        """Record this runtime's descendant PIDs in the tracking file.

        The PID this runtime registers is the sandbox launcher, not the agent:
        the tree is ``launcher -> agent -> agent chat process -> MCP servers``,
        and every one of those below the root held no entry in either PID file.
        A root that died before its subtree -- a teardown race, a crash mid-init
        -- therefore left a multi-hundred-MB subtree reparented to init that no
        reaper could act on, because every sweep keys off those files.

        Every pass re-enumerates the tree and writes the whole answer, because
        nothing tells this process when a descendant exits: they are its
        grandchildren, so there is no ``SIGCHLD`` and no wait to reap. Looking is
        the only way to learn, and the record has to be corrected in both
        directions -- a pid that left keeps or loses its line by its own
        liveness, and a pid still there gets the identity read on THIS pass, not
        the one recorded earlier. A stale identity is what would make the
        teardown sweep read a live descendant as recycled and skip it.

        A pid the walk cannot reach but which is still alive keeps its record:
        that is the child that left the process group, and its record is the only
        handle the teardown and the sweeps have on it. One confirmed gone is
        dropped from both the record and the file.

        The identity captured is re-confirmed against a second walk before it is
        persisted. A pid released between the enumeration and the capture can be
        held by an unrelated process by the time its identity is read, and that
        identity would then be recorded as OURS -- self-consistent, so every
        later ownership check passes and the teardown signals a stranger. A pid
        absent from the second walk is not a descendant of this root and is
        dropped. The window is not closed, but crossing it now needs a pid to
        become a stranger and then become our descendant again.

        The file is written BEFORE the in-memory record is replaced, and a failed
        write publishes nothing: the next pass re-reads the tree and tries again.

        Never raises on failure. A failed scan must not fail the spawn or the
        session start that called it -- the cost is a leak the sweep still
        reports, not a broken session -- but it logs at WARNING, because that
        report is then the only signal left. A CANCELLATION is not a failure and
        is deliberately NOT swallowed: it reaches the caller's cleanup guard,
        which owns the half-built runtime or session it must tear down.

        POSIX-shaped: ``_get_child_pids`` short-circuits on Windows, where the
        tree kill walks descendants itself through ``taskkill /T``.
        """
        pid = self._pid
        if pid is None:
            return
        try:
            loop = asyncio.get_running_loop()
            async with self._descendant_scan_lock:
                # The root's own number can be reused too, and a walk from a
                # recycled root enumerates a stranger's whole tree -- which would
                # then be recorded as ours and signalled at teardown. Verified
                # before the walk and again before the write, because the root can
                # exit while the walk runs. No recorded identity means it cannot
                # be verified at all, so nothing is recorded.
                if not self._root_identity_holds():
                    return
                # /proc reads on Linux, `pgrep`/`ps` subprocesses on macOS:
                # blocking either way, so they ride the same dedicated executor
                # the client path and the teardown sweep use, not the event loop.
                descendants = await loop.run_in_executor(
                    subprocess_executor(), _get_child_pids, pid
                )
                if not descendants and retry_when_empty:
                    await asyncio.sleep(self._DESCENDANT_RESCAN_DELAY)
                    descendants = await loop.run_in_executor(
                        subprocess_executor(), _get_child_pids, pid
                    )
                if not descendants:
                    return
                fresh: dict[int, ChildRecord] = await loop.run_in_executor(
                    subprocess_executor(), _capture_child_records, descendants
                )
                still_ours = set(
                    await loop.run_in_executor(subprocess_executor(), _get_child_pids, pid)
                )
                confirmed = {p: rec for p, rec in fresh.items() if p in still_ours}
                escaped = {
                    p: rec
                    for p, rec in self._child_pids.items()
                    if p not in confirmed and _escapee_is_still_ours(p, rec)
                }
                merged = {**escaped, **confirmed}
                gone = [p for p in self._child_pids if p not in merged]
                if merged == self._child_pids:
                    return
                if not self._root_identity_holds():
                    return
                if not await loop.run_in_executor(
                    subprocess_executor(),
                    functools.partial(
                        _replace_child_pids, merged, parent_pid=pid, drop=tuple(gone)
                    ),
                ):
                    logger.warning(
                        "AcpRuntime: could not write the descendant PIDs of root %d -- "
                        "retrying on the next scan",
                        pid,
                    )
                    return
                self._child_pids = merged
            logger.info(
                "AcpRuntime: tracking %d descendant PID(s) of root %d (%d outside the tree)",
                len(merged),
                pid,
                len(escaped),
            )
        except Exception:
            logger.warning(
                "AcpRuntime: descendant PID snapshot failed for root %s -- a tree "
                "that outlives this runtime would be invisible to every reaper",
                pid,
                exc_info=True,
            )

    async def _signal_tree(
        self,
        pid: int,
        sig: int,
        *,
        instance: str,
        expected: dict[int, str | None] | None = None,
    ) -> dict[int, str | None]:
        """Signal this runtime's process tree; return the orphans it reached.

        ``kill_process_tree`` is ``killpg(getpgid(pid))``, and ``getpgid`` raises
        once the root has exited. Read as "already dead", that leaves every
        process still in the group -- the launcher's children, the agent, its
        chat process -- unsignalled, reparented to init and holding their
        memory. A root that dies a few seconds into its life, before any
        descendant was recorded, is exactly the tree nothing else can find.

        So a reaped root is not the end of the teardown. The root was spawned as
        a session leader, so its pid IS the group id, and
        :func:`_signal_orphaned_runtime_group` signals that group once a live
        member vouches for it by identity -- and by *instance*: the per-spawn
        token this runtime put in its child's environment, which is what tells
        the root's own tree from a fresh runtime that took the root's recycled
        pid and leads a group that vouches just as well. The members it returns are empty for
        a tree that really is gone, which is what the caller needs to decide
        whether the grace-and-escalate that ``wait()`` would otherwise have
        driven is still owed -- and, when it is, they are what the escalation
        hands back as *expected*, so a group SIGKILL after the grace lands only
        on the group the SIGTERM did, never on a fresh runtime that took the
        root's number in between.

        Windows has no process groups, so this ladder is the POSIX half of the
        teardown and a Windows runtime does not reach it: ``_kill_inner`` drains
        the tree through its owned handles first and returns. That drain is what
        closes the reaped-root gap described above on Windows -- the descendants
        are pinned when the tree is SPAWNED, so a root that dies before any of
        them was recorded is still reachable, where ``kill_process_tree``'s
        ``taskkill /T`` walk would find nothing to tear down. The vouched-group
        path below is therefore guarded by ``IS_POSIX``, not merely
        platform-agnostic code that happens to no-op.
        Every call is off-loop: ``taskkill`` is a blocking spawn, and the group
        walk reads ``/proc``.
        """
        loop = asyncio.get_running_loop()
        # kill_process_tree resolves the group FROM the root's number, so it may
        # run only while that number is provably still ours, and the one proof
        # is the live start id matching the one recorded at spawn. ``returncode``
        # is NOT that proof: asyncio's child watcher does the waitpid in the
        # background and propagates the code to the Process object a callback
        # later, so a root can be reaped -- its number free for a fresh session
        # leader whose getpgid SUCCEEDS -- while ``returncode`` still reads None.
        # A root whose identity cannot be read is treated as gone: the vouched
        # path below reaches its members where it can, and where it cannot the
        # cost is a leak the sweep reports, never a signal to a stranger. The
        # same reasoning bars re-resolving on the escalation (``expected`` set).
        identity = self._root_identity()
        if expected is None and identity == "holds":
            recorded = self._start_time
            assert recorded is not None  # implied by identity == "holds"
            try:
                # PINNED, not merely checked. The tree kill is deferred to an
                # executor, so the identity verified here has to stay pinned
                # across that hop: a check that ends with its handle closed
                # leaves the root free to exit and the number free to be
                # recycled in between, and a kill that re-resolves the pid would
                # then tear down whatever holds it now.
                # kill_process_tree_pinned keeps the identity PINNED across the
                # terminate, which is what makes the number still mean this
                # process. On Windows it re-resolves nothing from the number at
                # all: it drains the tree through process handles opened against
                # this exact creation identity and held open until every member's
                # exit is confirmed. POSIX delegates straight through, where
                # os.killpg is issued in-process by this same interpreter.
                pinned = await loop.run_in_executor(
                    subprocess_executor(),
                    functools.partial(platform_compat.kill_process_tree_pinned, pid, recorded, sig),
                )
                if pinned:
                    return {}
                # False is "identity unconfirmed" and NO signal was sent. Treat it
                # exactly as an identity that does not hold: fall through to the
                # vouched path, which names members by an inherited token instead
                # of by the root's number.
                logger.warning(
                    "AcpRuntime kill: not resolving the tree of root PID %d from its "
                    "number -- its identity could not be pinned across the terminate",
                    pid,
                )
            except ProcessLookupError:
                pass
            except OSError:
                return {}
        reached: dict[int, str | None] = {}
        if platform_compat.IS_POSIX:
            reached = await loop.run_in_executor(
                subprocess_executor(),
                functools.partial(
                    _signal_orphaned_runtime_group, pid, sig, instance, expected=expected
                ),
            )
        if reached:
            logger.warning(
                "AcpRuntime kill: root PID %d was already gone; signalled %d orphaned "
                "member(s) of its process group with signal %d",
                pid,
                len(reached),
                sig,
            )
        else:
            # Nothing was reached. The tree is leaked to the orphan sweep -- the
            # deliberate trade, a leak over a signal to a stranger -- but a silent
            # return made that trade invisible in the field, where it reads as a
            # teardown that worked.
            #
            # The guard is the GROUP, not the platform. A host that cannot read the
            # token never reaches a member, and so does a Linux host whose members
            # are there but do not vouch -- a sandbox that scrubbed the token, an
            # unreadable environ, a missing argv identity. The second is the
            # reported leak's own shape, so the line must cover it too; keying this
            # on the platform hid exactly that case on the one platform where the
            # vouched path runs.
            #
            # Still only when something is plausibly there: a root that exited
            # cleanly before a routine kill() leaves an empty group, and a line on
            # that path is noise on the ordinary teardown.
            # _pgroup_has_member_besides answers on every platform and is
            # conservative on a failed read, so an unreadable group still speaks. It
            # scans /proc or sysctl, hence the executor. Reporting only, never
            # routing: the attempt above is unconditional on POSIX and
            # _marked_group_members owns the platform answer.
            leaked = await loop.run_in_executor(
                subprocess_executor(),
                functools.partial(_pgroup_has_member_besides, pid, pid),
            )
            if leaked:
                logger.warning(
                    "AcpRuntime kill: root PID %d could not be reached with signal %d -- "
                    "%s, and %s, so its tree is left to the orphan sweep",
                    pid,
                    sig,
                    self._ROOT_UNREACHED_REASON_BY_VERDICT[identity],
                    (
                        "no member of its group vouched for this spawn's incarnation"
                        if group_vouching_available()
                        else "this host cannot vouch a process group by incarnation token"
                    ),
                )
        return reached

    # Grace window for SIGTERM before escalating, and the post-SIGKILL reap
    # window. Class attributes so tests can shrink them.
    _KILL_TERM_TIMEOUT = 5.0
    _KILL_REAP_TIMEOUT = 2.0

    async def kill(self, *, expected: bool = False, reason: str = "") -> None:
        """Kill the subprocess and release spawn resources even when cancelled.

        ``reason`` names the caller's intent ("warm mint teardown", "failed
        session setup cleanup", ...) and flows into the death log line and
        ``death_summary()``. Unattributed kills proved undiagnosable in the
        field: a runtime killed under a live turn surfaces to the turn only
        as a bare "process died during prompt", and a log line that says
        "killed" without saying WHO killed leaves nothing to correlate.
        """
        try:
            await self._kill_inner(expected=expected, reason=reason)
        finally:
            self._discard_sandbox_cleanup()
            await self._discard_bound_workspace()

    async def _kill_inner(self, *, expected: bool = False, reason: str = "") -> None:
        """Kill the subprocess and clean up all state.

        ``expected`` changes log severity only: a deliberate teardown of a
        healthy runtime (pool TTL recycle, session shutdown, logout) passes
        ``expected=True`` to log the death at INFO. The default is False —
        matching ``_mark_dead`` — so every cleanup kill on a failure path
        (``initialize()``'s failed-spawn cleanup, a failed session setup) and
        any future call site stays a WARNING without having to opt in.
        ``_mark_dead`` additionally refuses to downgrade when the process
        already exited on its own, so a reap-after-death can never log INFO.
        """
        # Fail pending futures + poison session queues FIRST. _mark_dead sets
        # self._dead internally; doing it up front (before teardown) ensures any
        # waiters learn the runtime died. Calling it after setting _dead=True
        # would hit its early-return guard and skip all cleanup.
        self._mark_dead(f"killed ({reason})" if reason else "killed", expected=expected)

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._process and platform_compat.IS_WINDOWS:
            # The Windows completion of the same teardown ``_signal_tree`` drives
            # on POSIX, and it runs INSTEAD of that ladder rather than inside it.
            # Both close the one failure: a root that exits while its agent and
            # MCP descendants keep running and holding their memory. They cannot
            # share a seam, because the evidence arrives at different times. POSIX
            # can name the survivors AFTER the fact -- the root was a session
            # leader, so its pid is still the group id and a live member vouches
            # for the group by an inherited token. Windows has no group and no
            # such token, and a reaped root leaves ``taskkill /T`` nothing to
            # walk, so the tree is instead pinned by handle when it is SPAWNED
            # and drained from those handles here. That also makes the ladder
            # below vestigial on Windows and not merely unused: the drain returns
            # only once every member's exit is CONFIRMED, so there is no grace
            # left to serve and no escalation owed. A drain that cannot confirm
            # raises, keeping the pins and the tracking for maintenance to retry,
            # which is why this must not be softened into a best-effort call.
            process = self._process
            pid = process.pid
            try:
                await platform_compat.terminate_windows_asyncio_tree(process)
            except (OSError, asyncio.TimeoutError):
                logger.warning(
                    "AcpRuntime Windows tree cleanup incomplete for PID %s; retaining process",
                    pid,
                    exc_info=True,
                )
                raise
            # Before the handle is dropped, and on this branch rather than at the
            # ladder's own amendment below: the death line was written pre-signal
            # and says returncode=<not reaped>, and the drain above returns only
            # once every member's exit is CONFIRMED, so the status is knowable at
            # exactly this point. The drain's failure path raises instead, keeping
            # the placeholder true for a tree it could not confirm.
            self._note_reaped_after_kill(process.returncode)
            self._process = None
            self._process_instance = ""
            # Tracking was retired by the shared drain under the original pin.
            return

        if self._process:
            pid = self._process.pid
            # platform_compat.kill_process_tree: killpg on POSIX (the spawn
            # sets start_new_session=IS_POSIX, so the group is the tree);
            # taskkill /T on Windows, where os.getpgid/os.killpg do not exist
            # (a raw call raises AttributeError, which the OSError guard here
            # would NOT catch — the kiro-cli tree then leaks on every session
            # recycle). Offloaded to the subprocess executor: on Windows the
            # shim shells out to taskkill (a blocking subprocess.run), which
            # must not run on the event loop (no blocking call on the event
            # loop).
            # Read before the kill clears it: the group fallback needs the
            # incarnation this process was spawned as, not the empty successor.
            instance = self._process_instance
            orphaned_group = await self._signal_tree(
                pid, platform_compat.SIGTERM, instance=instance
            )
            escalated = False
            try:
                await asyncio.wait_for(self._process.wait(), timeout=self._KILL_TERM_TIMEOUT)
            except asyncio.TimeoutError:
                escalated = True
                await self._signal_tree(pid, platform_compat.SIGKILL, instance=instance)
                # Reap the child so a delivered SIGKILL doesn't leave a zombie
                # that the liveness probe below would misread as a survivor.
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=self._KILL_REAP_TIMEOUT)
                except asyncio.TimeoutError:
                    pass
            # Only when the wait did NOT time out. The block below exists because
            # a root that was already gone makes wait() return at once, so the
            # escalation never ran; if it DID run, repeating it here would pay a
            # second grace and send the members a duplicate SIGKILL.
            if orphaned_group and not escalated:
                # The root was already gone, so wait() above returned at once and
                # the escalation never ran for the members left in the group.
                # Give them the same grace a live tree gets, then escalate to
                # the group -- aimed by the members the SIGTERM vouched, not by
                # the root's number, which a fresh runtime can hold by now.
                escalate = functools.partial(
                    self._signal_tree,
                    pid,
                    platform_compat.SIGKILL,
                    instance=instance,
                    expected=orphaned_group,
                )
                try:
                    await asyncio.sleep(self._KILL_TERM_TIMEOUT)
                except asyncio.CancelledError:
                    # A shutdown that cancels this teardown inside the grace must
                    # not leave SIGTERM-ignoring members alive: they were vouched
                    # and signalled, and the SIGKILL is the only thing still
                    # owed. Shielded so THIS cancellation cannot cut it short; a
                    # further cancel raises at the await and leaves it running
                    # unawaited, which is the bound this gives, not immunity. It
                    # is one identity re-check per member and a signal each.
                    await asyncio.shield(escalate())
                    raise
                await escalate()
            # Before the handle is dropped: the death line above was
            # written pre-signal and says returncode=<not reaped>.
            self._note_reaped_after_kill(self._process.returncode)
            self._process = None
            # The id names the process that just ended; the next spawn mints its
            # own, and nothing may answer with this one in between.
            self._process_instance = ""
            if platform_compat.pid_exists(pid):
                # Both kill_process_tree calls above swallow OSError by design
                # (racing a normal exit), which makes a signal-delivery failure
                # (EPERM through a launcher wrapper, pgid drift) look identical
                # to success. Verify instead of assuming: a survivor must stay
                # PID-tracked so the startup/periodic sweeps keep a handle on
                # it — untracking here would leak the process until reboot.
                logger.warning(
                    "AcpRuntime kill: PID %d survived SIGTERM/SIGKILL escalation; "
                    "leaving PID tracked for sweep",
                    pid,
                )
            else:
                logger.info("AcpRuntime killed (PID %d)", pid)

                # Untrack the PID so the orphan sweep doesn't chase a dead entry
                # (mirrors AcpClient._reset_state). Best-effort — a leftover entry
                # is only pruned lazily otherwise.
                try:
                    _untrack_pid(pid)
                    _untrack_session_pid(pid)
                    unregister_protected_pid(pid)
                except Exception:
                    logger.debug("AcpRuntime: PID untracking failed for %s", pid, exc_info=True)

            # Runs on BOTH branches above: whether the root died or survived says
            # nothing about a descendant that left the process group, and the
            # entry of one still running is what the sweep needs to reap it.
            saved_children = dict(self._child_pids)
            self._child_pids = {}
            if saved_children:
                survivors = await asyncio.to_thread(_prune_dead_descendants, saved_children)
                if survivors:
                    logger.warning(
                        "AcpRuntime: retained tracking for %d descendant PID(s) that "
                        "survived teardown; the orphan sweep will reap them: %s",
                        len(survivors),
                        survivors,
                    )

    # ── Reader Task (single owner of stdout) ──

    def _snapshot_subagent_sessions(self, params: dict) -> None:
        """Replace the known backend-subagent session-id set from a list_update.

        The frame carries the backend's FULL current subagent list (kiro-cli
        rebuilds it from `orchestrated_sessions` on every change), so replacing
        the set keeps it bounded and self-cleaning: terminated children vanish
        from the next update. Ids are backend-controlled — length-capped and
        type-checked so a hostile payload cannot grow memory unboundedly.

        Membership here is what the routing branch decides a child's approvals
        on, so the entry bound is the SHARED native-child cap
        `NATIVE_CHILD_ROSTER_CAP` — the same number `AcpSessionHandle` remembers
        child ids under — and never a tighter per-frame slice. A tighter bound
        would deny mode parity to children the very same payload made the
        handle count: an id the set does not hold is unrecognisable, its
        `session/update` is a counted drop and its permission request is
        auto-rejected, so a cap below the handle's turns one announced roster
        into two governance classes by list position alone.

        Ids past the cap are counted in `_subagent_roster_overflow`, and the
        truncation is reported through `_note_roster_overflow`: a truncated tail
        otherwise reads exactly like a roster that never named those children.
        The count is per SNAPSHOT, not cumulative — the frame is the full list,
        so replacement makes it idempotent under repeated identical rosters even
        above the cap (the handle's counter accumulates instead, because ids
        reach it one frame at a time and only a per-turn total can describe that
        stream). The LOG is episode-scoped rather than per-snapshot, which is a
        different lifetime and deliberately so: see `_note_roster_overflow`.

        The scan visits every entry even above the cap, on the SHARED demux
        loop. That is a constant factor on work already done: `json.loads` has
        walked the whole list before this method is called, and the frame is
        already bounded by `_STDOUT_BUFFER_LIMIT`. Slicing to save the pass is
        what cost the tail its governance.
        """
        raw = params.get("subagents")
        if not isinstance(raw, list):
            return
        ids: set[str] = set()
        overflow = 0
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            sid = entry.get("sessionId") or entry.get("session_id")
            if not isinstance(sid, str) or not sid or len(sid) > MAX_ACP_SESSION_ID_LEN:
                continue
            if sid in ids:
                continue
            if len(ids) >= NATIVE_CHILD_ROSTER_CAP:
                # Past the cap an id is counted, never stored: recognising a
                # repeat would mean remembering it, which is the one thing the
                # cap refuses.
                overflow += 1
                continue
            ids.add(sid)
        self._subagent_sessions = ids
        self._subagent_roster_overflow = overflow
        if overflow:
            self._note_roster_overflow(overflow)
        else:
            self._end_roster_overflow_episode()
        # Ownership is provable only when exactly one session is registered:
        # the announce demonstrably belongs to it. Otherwise no owner, and
        # routing stays fail-closed.
        self._subagent_owner = (
            next(iter(self._session_queues)) if len(self._session_queues) == 1 else None
        )

    def _note_roster_overflow(self, overflow: int) -> None:
        """Report a truncated roster snapshot: loud ONCE per episode, then counted.

        The first truncation an episode sees is a `WARNING`, because a truncated
        tail is otherwise invisible — nothing else in the log distinguishes it
        from a roster that never named those children, and an operator has to
        see it to decide whether the cap needs raising. What must not recur is
        the per-FRAME repeat: `subagent/list_update` is a backend-controlled
        notification re-broadcast on every child status change, so above the cap
        each rebroadcast re-earns the same warning at the backend's frame rate.
        Measured on this handler: 10 over-cap snapshots → 10 identical WARNING
        records at ~245 message bytes each, whether the frames are byte-identical
        or differ only in a child's `status`. That is the retention hazard
        `_note_dropped_frame` documents above (~60 lines/second rolling incident
        evidence out of `gateway.log`'s 8MB window), one level louder.

        So later truncations inside the episode are tallied and flushed as one
        throttled `DEBUG` summary, at most every
        `_ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS`, carrying how many snapshots
        repeated and the LARGEST tail they named. The peak, not the latest
        value: sizing the cap reads the worst case, and which value happens to
        be current at an arbitrary flush instant is noise.

        **The reset boundary is the truncation EPISODE** — it re-arms when the
        overflow count returns to 0, which is exactly the two events that
        already retire the snapshot attribution: a later roster that truncated
        nothing, and the owning session unregistering. One lifetime governs both
        halves of the same signal, so the loud line and the auto-reject reason
        can never disagree about whether the cap is under pressure. A plain
        per-interval re-arm is the alternative, and it is what the drop counter
        does, but the drop summary is `DEBUG`: re-arming a WARNING on a timer
        restates it every interval for as long as the steady state lasts, which
        is the volume this throttle exists to remove.

        The residual, stated rather than implied: a tail that GROWS inside one
        episode (40 ids, later 40000) is loud only at its first value, and the
        growth is visible in the summary's peak at `DEBUG`. Re-warning on growth
        would need a second threshold — a second throttle shape in a module that
        already has one.

        Synchronous and awaitless, like the drop counter, because the caller is
        on the shared demux loop.
        """
        now = time.monotonic()
        if self._roster_overflow_summary_at == 0.0:
            # No episode open: this snapshot is the loud one, and it opens the
            # window the repeats below are measured against. __init__ cannot
            # supply that baseline — a runtime may be constructed long before
            # spawn(), and a stale timestamp would make the first repeat flush
            # immediately instead of aggregating.
            self._roster_overflow_summary_at = now
            logger.warning(
                "subagent roster announced %d children past the %d-id recognition "
                "cap; their session/update frames are counted drops and their "
                "permission requests are auto-rejected (reason %s) rather than "
                "reaching the approval pipeline. Further truncated snapshots are "
                "summarized at DEBUG until a roster inside the cap arrives",
                overflow,
                NATIVE_CHILD_ROSTER_CAP,
                _ROSTER_OVERFLOW_REJECT_REASON,
            )
            return
        self._roster_overflow_repeats += 1
        self._roster_overflow_peak = max(self._roster_overflow_peak, overflow)
        if now - self._roster_overflow_summary_at >= _ROSTER_OVERFLOW_SUMMARY_INTERVAL_SECS:
            self._flush_roster_overflow(now)

    def _flush_roster_overflow(self, now: float | None = None) -> None:
        """Emit the repeated-truncation summary and reopen the throttle window."""
        self._roster_overflow_summary_at = time.monotonic() if now is None else now
        if not self._roster_overflow_repeats:
            return
        logger.debug(
            "subagent roster truncated on %d further snapshot(s); largest tail %d "
            "id(s) past the %d-id recognition cap",
            self._roster_overflow_repeats,
            self._roster_overflow_peak,
            NATIVE_CHILD_ROSTER_CAP,
        )
        self._roster_overflow_repeats = 0
        self._roster_overflow_peak = 0

    def _end_roster_overflow_episode(self) -> None:
        """Close a truncation episode: flush the residual count, re-arm the warning.

        Called wherever `_subagent_roster_overflow` returns to 0 — a roster
        inside the cap, or the owning session unregistering. Flushing first is
        what keeps a SHORT episode honest: repeats accrued in under one interval
        would otherwise be dropped on the floor, and then a truncation storm
        that ends quickly would report only its first frame. Re-arming is what
        keeps the NEXT truncation loud; without it the throttle would swallow a
        genuinely new one for the rest of the runtime's life.
        """
        self._flush_roster_overflow()
        self._roster_overflow_summary_at = 0.0

    async def _wait_for_answer_capacity(
        self,
        msg: JsonRpcMessage,
        *,
        request_kind: str,
        session_id: str = "",
        audit_reason: str | None = None,
    ) -> bool:
        """Wait briefly for shared answer capacity or condemn a wedged pipe.

        Server-to-client requests require a response, so overflowing answers
        cannot take the notification counted-drop path. A responsive backend
        may fill the set with already-buffered requests before completed-task
        callbacks run; one completion admits the current request. No
        completion within the bound means writes are wedged, so marking the
        runtime dead resolves every pending wait instead of leaving the remote
        requester unanswered indefinitely.
        """

        def _deny() -> bool:
            """Refuse admission, recording the decision first.

            A refusal that reaches a caller which had already been admitted to
            wait must leave a SEL record, or a permission decision that denied a
            real tool invocation is indistinguishable from one never made.
            """
            if audit_reason is not None:
                self._audit_denied_off_loop(msg, session_id, audit_reason)
            return False

        # Deliberately NOT audited: on an already-dead runtime a flooding
        # backend's frames are gated out here, and auditing each one would grow
        # audit tasks without bound — the very failure the cap prevents.
        if self._dead:
            return False
        if len(self._answer_tasks) < self._max_answer_tasks:
            return True

        done, _pending = await asyncio.wait(
            set(self._answer_tasks),
            timeout=self._answer_cap_wait_secs,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if done:
            # asyncio schedules task callbacks separately from waking waiters.
            # Remove completed entries here so admitting the replacement never
            # transiently exceeds the shared cap; the callbacks remain an
            # idempotent cleanup backstop.
            self._answer_tasks.difference_update(done)
            if self._dead:
                # A concurrent waiter condemned the runtime while this one was
                # parked: capacity freed but admission still fails, so this
                # refusal owes an audit like any other.
                return _deny()
            return True

        logger.error(
            "answer-task cap (%d) reached at %s request id=%s%s and no "
            "in-flight answer completed in %gs — backend is flooding frames "
            "while not reading stdin; marking runtime dead so every pending "
            "wait resolves",
            self._max_answer_tasks,
            request_kind,
            msg.id,
            f" for session {session_id}" if session_id else "",
            self._answer_cap_wait_secs,
        )
        # Audit before condemning the runtime, so the record for this decision
        # cannot race the wait-resolution _mark_dead triggers.
        refusal = _deny()
        self._mark_dead(f"{request_kind}-answer task cap reached (backend not reading)")
        return refusal

    async def _spawn_answer_task(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        *,
        reason: str = _UNREGISTERED_REJECT_REASON,
    ) -> None:
        """Spawn a bounded off-loop auto-answer for an unroutable permission request.

        Off-loop because the answer's ``send_response`` can block on stdin
        ``drain()`` against a backend that is not reading — awaiting inline
        would freeze the shared reader (every session's demux) on one hostile
        or wedged backend. Bounded because each blocked task is retained in
        ``_answer_tasks``: a backend that floods permission frames while never
        reading stdin would otherwise grow that set until the gateway OOMs.
        At capacity the shared admission wait either observes progress or
        marks the runtime dead so the requester cannot remain unanswered.
        """
        if not await self._wait_for_answer_capacity(
            msg,
            request_kind="permission",
            session_id=session_id,
            audit_reason="answer_task_cap_runtime_dead",
        ):
            return
        _t = asyncio.ensure_future(
            self._answer_unroutable_permission(msg, session_id, reason=reason)
        )
        self._answer_tasks.add(_t)
        _t.add_done_callback(self._answer_tasks.discard)

    async def _answer_unroutable_permission(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        *,
        reason: str = _UNREGISTERED_REJECT_REASON,
    ) -> None:
        """Answer a permission REQUEST for a session with no registered queue.

        The ACP contract for a server→client request is that the client always
        replies; kiro-cli's own TUI answers even unowned-session permission
        requests (``cancelled``) rather than dropping them. Auto-reject is
        deliberate and conservative: never auto-approve here — no policy engine
        has seen this tool call, and an approve would grant an invisible
        escalation. Per-frame WARNING is safe (unlike the drop counter's flood
        case): each request corresponds to one pending tool approval and the
        backend cannot re-emit it without a new turn.
        """
        params = msg.params if isinstance(msg.params, dict) else {}
        option_id = _reject_option_id(params)
        if option_id is not None:
            result = {"outcome": {"outcome": "selected", "optionId": option_id}}
        else:
            result = {"outcome": {"outcome": "cancelled"}}
        tool_call = params.get("toolCall")
        raw_title = tool_call.get("title") if isinstance(tool_call, dict) else None
        # The title is backend/LLM-authored and may embed a credential-bearing
        # command line — redact BEFORE truncating (truncation first could clip
        # a secret mid-token so the redaction patterns no longer match, leaking
        # a credential prefix into the logs).
        # Bound the redaction input (backend-controlled) BEFORE the regex
        # passes, generously above the display cap so a clipped secret
        # cannot straddle the boundary the display truncation makes.
        title = redact_text(str(raw_title)[:4096])[:120] if raw_title else "<unknown>"
        logger.warning(
            "auto-rejected permission request id=%s for session %s "
            "(tool: %s, reason: %s): no surface on this client can answer it "
            "right now; answering with %s so the backend subagent gets a tool "
            "error instead of hanging",
            msg.id,
            session_id,
            title,
            reason,
            result["outcome"]["outcome"],
        )
        try:
            # Bounded send: an answer that cannot be written within the
            # timeout means the backend is not reading its stdin at all —
            # the pipe is wedged, and every further frame from it would
            # stack another blocked task (the OOM vector). Marking the
            # runtime dead resolves EVERY pending wait by teardown, so no
            # request is left unanswered and nothing accumulates.
            await asyncio.wait_for(self.send_response(msg.id, result), timeout=30.0)
        except asyncio.TimeoutError:
            logger.error(
                "answer for permission request id=%s could not be written in "
                "30s — backend not reading stdin; marking runtime dead",
                msg.id,
            )
            # Audit BEFORE returning: the denial DECISION was made even
            # though delivery failed — mandatory SEL coverage applies to
            # every decision, not just successfully delivered ones.
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:send_stalled_runtime_dead", title=title
            )
            self._mark_dead("permission-answer write stalled (backend not reading)")
            return
        except AcpRuntimeDead:
            # Runtime died mid-answer; the backend's wait dies with it.
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:runtime_dead_mid_answer", title=title
            )
            return
        except Exception:
            # This coroutine runs as a RETAINED TASK off the reader loop, so
            # an unexpected send failure would otherwise be swallowed with
            # the task — the child never gets an answer and waits on a
            # stranded oneshot, the exact hang this path exists to prevent.
            # A response write that fails for any reason other than the
            # already-handled dead-runtime case means the pipe cannot be
            # trusted: log and mark the runtime dead so the child's wait
            # dies with the process instead of hanging invisibly.
            logger.exception(
                "failed to answer unroutable permission request id=%s — "
                "marking runtime dead so the requester cannot hang",
                msg.id,
            )
            self._audit_denied_off_loop(
                msg, session_id, f"{reason}:send_failed_runtime_dead", title=title
            )
            self._mark_dead("unroutable-permission answer failed")
            return
        # Every permission decision is SEL-audited (repo convention; see
        # _audit_denied_off_loop for the off-loop/lazy-import rationale).
        self._audit_denied_off_loop(msg, session_id, reason, title=title)

    def _audit_denied_off_loop(
        self,
        msg: JsonRpcMessage,
        session_id: str,
        reason: str,
        *,
        title: str | None = None,
    ) -> None:
        """SEL-audit a denied permission decision without blocking the caller.

        Every permission decision leaves a SEL record (repo convention; the
        dashboard deny path does the same). Off the calling task because
        ``sel()`` may do blocking filesystem work on first use (e.g. Windows
        ACLs). The decision is already made, so an audit failure must not
        undo or delay it; the failure is swallowed after logging. Lazy
        import: a module-level import of ``kiro_crew.sel`` would be circular
        (same pattern as sandbox.py).
        """
        if title is None:
            _params = msg.params if isinstance(msg.params, dict) else {}
            _tc = _params.get("toolCall")
            _raw = _tc.get("title") if isinstance(_tc, dict) else None
            title = redact_text(str(_raw)[:4096])[:120] if _raw else "<unknown>"
        # Hang-resilience series: every runtime-side denial funnels through
        # here, so one emit covers unroutable/between-turns/cap/send-failure
        # denials. ``reason`` is the closed SEL enum (low-cardinality).
        emit_counter(
            CHILD_PERMISSION_DENIED,
            {"surface": "runtime", "reason": reason},
        )
        request_id = msg.id if isinstance(msg.id, (str, int)) else ""
        # SNAPSHOT the attribution key NOW: the audit closure runs later on a
        # worker thread, and `_subagent_owner` is mutable (unregister/session
        # swap). Reading it at execution time would write the wrong owner —
        # or the bare PID — into an immutable SEL row.
        session_key = f"acp:{self._subagent_owner or self._pid}:{session_id}"

        def _audit() -> None:
            try:
                from kiro_crew.sel import sel

                sel().log_tool_invocation(
                    session_key=session_key,
                    agent="kirocrew",
                    source="acp_runtime",
                    tool_name=title,
                    outcome="denied",
                    request_id=request_id,
                    error=reason,
                )
            except Exception:
                logger.exception("SEL audit for auto-rejected permission failed")

        audit_task = asyncio.ensure_future(asyncio.to_thread(_audit))
        # Retain the task so it cannot be garbage-collected mid-flight; the
        # done callback drops the reference and surfaces nothing (audit
        # failures are already logged inside _audit).
        self._audit_tasks.add(audit_task)
        audit_task.add_done_callback(self._audit_tasks.discard)

    def _note_dropped_frame(self, session_id: object, method: object) -> None:
        """Count one unroutable frame, flushing a summary at most once per interval.

        Replaces a per-frame log line (see the drop-accounting constants above).
        Cheap and synchronous by design: it is called from the hot demux path
        and must not await, so there is no timer task to leak and no blocking
        I/O beyond the throttled ``logger.debug`` the flush itself emits.

        Both arguments are backend-controlled and deliberately typed `object`:
        they are normalized through `_drop_key_part`, which is the only thing
        that keeps a wrong-typed value from raising in the shared reader.
        """
        key = (_drop_key_part(session_id), _drop_key_part(method))
        # Hang-resilience series: classify the drop by method so dashboards
        # can alert on the pre-fix hang signature. ``method_class`` is a
        # closed 3-value enum — the raw method (backend-controlled) never
        # becomes an attribute value.
        _m = method if isinstance(method, str) else ""
        if _m == METHOD_REQUEST_PERMISSION:
            _mclass = "permission"
        elif _m in self._notification_aliases().session_update:
            # EVERY session-update spelling this host uses classifies as
            # "update": a dashboard alerting on the pre-fix hang signature must
            # see a dropped extension-method child update the same way it sees
            # the plain spelling, and a spelling the host's aliases omit would be
            # counted as "other" and lost.
            _mclass = "update"
        else:
            _mclass = "other"
        emit_counter(DROPPED_FRAMES, {"method_class": _mclass})
        now = time.monotonic()
        if self._dropped_frames_flushed_at == 0.0:
            # First drop of this runtime's life opens the window. __init__ cannot
            # supply the baseline (a runtime may be constructed long before
            # spawn()), and a stale 0.0 would make every first drop flush
            # immediately instead of aggregating.
            self._dropped_frames_flushed_at = now
        counts = self._dropped_frames
        if key not in counts and len(counts) >= _DROP_SUMMARY_MAX_KEYS:
            # A wide fan-out of distinct keys inside one interval must not grow
            # the map; report what we have and start a fresh window.
            self._flush_dropped_frames(now)
        counts[key] = counts.get(key, 0) + 1
        if now - self._dropped_frames_flushed_at >= _DROP_SUMMARY_INTERVAL_SECS:
            self._flush_dropped_frames(now)

    def _flush_dropped_frames(self, now: float | None = None) -> None:
        """Emit one summary record per (sessionId, method) and reset the window.

        Called on the interval from _note_dropped_frame and unconditionally when
        the reader loop exits, so a low-rate trickle is reported late rather
        than swallowed. A key seen once in an otherwise idle hour is therefore
        reported at the next drop or at loop exit — deliberately traded for
        having no wakeup timer on the event loop.
        """
        self._dropped_frames_flushed_at = time.monotonic() if now is None else now
        counts = self._dropped_frames
        if not counts:
            return
        for (session_id, method), count in counts.items():
            logger.debug(
                "Dropped %d unroutable frame(s) for session %s (method=%s)",
                count,
                session_id,
                method,
            )
        counts.clear()

    async def _reader_loop(self) -> None:
        """Single reader task — owns stdout exclusively. Routes frames by type.

        Routing:
          1. Response with id in _pending_requests → resolve Future
          2. Response with id in _routed_requests → put in session queue
          3. Notification with params.sessionId → session queue
          4. Request (method + id) with no sessionId → answered ONCE at
             connection level (-32601), never broadcast
          5. No sessionId → broadcast to all queues
        """
        assert self._process and self._process.stdout
        stdout = self._process.stdout
        # This host's spellings for the three aliasable inbound events, read ONCE
        # per reader rather than per frame: they are fixed for the process's life
        # and this is the hot demux path.
        _aliases = self._notification_aliases()
        _session_update = _aliases.session_update
        _subagent_list_update = _aliases.subagent_list_update
        _mcp_init = _aliases.mcp_init

        try:
            while True:
                try:
                    line = await stdout.readuntil(b"\n")
                except asyncio.IncompleteReadError as exc:
                    # EOF, possibly holding a trailing unterminated line. Keep
                    # readline()'s old shape: hand the partial to the parser, and
                    # an empty partial falls through to the exit branch below.
                    line = exc.partial
                except asyncio.LimitOverrunError as exc:
                    # ONE oversize frame must not kill the demux — same invariant
                    # as the non-dict and non-numeric-id guards below. Tearing
                    # the runtime down here ends EVERY multiplexed session
                    # mid-turn, which is what users see as "process exited /
                    # chat failure" after a single huge tool result.
                    #
                    # _drain_oversize_line consumes the whole line THROUGH its
                    # terminating newline and discards it, so the stream is back
                    # on a frame boundary and no byte-slice of the oversize line
                    # ever reaches json.loads. Its budget is per call and needs no
                    # cross-iteration state, because every call that returns ends
                    # on a boundary — so a replay of oversize-but-terminated
                    # frames is survivable frame after frame.
                    #
                    # An awaited request whose response was in a dropped frame is
                    # not orphaned: _send_and_await wraps every future in
                    # wait_for(timeout=...), so the caller gets a timeout instead
                    # of hanging. The ids in flight at the drop are logged so that
                    # timeout is attributable.
                    try:
                        dropped = await _drain_oversize_line(stdout, exc)
                    except asyncio.IncompleteReadError:
                        self._mark_dead("stdout closed mid-oversize-line")
                        return
                    except OversizeLineUnrecoverable as fatal:
                        logger.error("stdout unrecoverable: %s", fatal)
                        self._mark_dead(f"stdout overrun: {fatal}")
                        return
                    logger.warning(
                        "dropped an oversize stdout frame (%d bytes); resynced at "
                        "next frame (in-flight awaited=%s routed=%s): %s",
                        dropped,
                        sorted(self._pending_requests)[:_DROP_IDS_IN_LOG],
                        sorted(self._routed_requests)[:_DROP_IDS_IN_LOG],
                        exc,
                    )
                    continue

                if not line:
                    rc = self._process.returncode if self._process else "?"
                    self._mark_dead(self._exit_reason(rc))
                    return

                self._last_activity = time.monotonic()

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    if self.recording_allowed:
                        logger.debug("non-JSON stdout line: %s", line[:200])
                    continue

                # Valid JSON is not necessarily a JSON-RPC object: a bare scalar
                # or array (e.g. `123`, `"foo"`, `[1,2]`, `true`, `null`) would
                # make JsonRpcMessage.from_dict -> data.get(...) raise
                # AttributeError, crashing this single-owner reader and tearing
                # down EVERY multiplexed session. Skip anything that isn't an
                # object so one stray line can't kill the demux.
                if not isinstance(data, dict):
                    if self.recording_allowed:
                        logger.debug("non-object JSON stdout line: %s", line[:200])
                    continue

                # Opt-in raw-frame recording for the replay corpus. A no-op
                # unless KIROCREW_ACP_RECORD_FRAMES names a directory: it
                # returns before awaiting anything, so an ordinary run pays one
                # env lookup. When it IS set the frame is queued for the
                # recorder's own writer thread rather than written here,
                # because a filesystem syscall on this loop stalls every
                # multiplexed session. It never raises -- see
                # kiro_crew.acp._frame_record.
                if self.recording_allowed:
                    await record_frame(self._acp_backend, data, len(line))

                projection = getattr(self, "_native_skill_projection", None)
                if projection is not None:
                    data = projection.frame(data)
                msg = JsonRpcMessage.from_dict(data)

                # Route responses
                if msg.id is not None and (msg.result is not None or msg.error is not None):
                    # JSON-RPC allows string ids, and this runtime only ever
                    # issues int ids — but the id in the response is agent-
                    # controlled. int("req-1") / int([...]) raises ValueError/
                    # TypeError, which the catch-all below turns into
                    # _mark_dead, poisoning EVERY multiplexed session over one
                    # unmatched frame. Same invariant as the non-dict guard
                    # above: skip the frame, don't kill the demux.
                    try:
                        req_id = msg.id if isinstance(msg.id, int) else int(msg.id)
                    except (TypeError, ValueError, OverflowError):
                        # OverflowError: json parses 1e9999 to float("inf"),
                        # which int() rejects differently from a bad string.
                        #
                        # Left per-frame on purpose (same for the unmatched-id
                        # line below), unlike the two session-routing drops:
                        # here the ID is the whole diagnostic value, and it is a
                        # distinct value per frame — aggregating by it would give
                        # the counter an unbounded key space, while aggregating
                        # without it would throw away the only datum that
                        # identifies the correlation bug. Both branches also
                        # require a response-shaped frame, i.e. one per request
                        # THIS runtime issued (bounded by turns), so neither has
                        # the after-teardown steady state that made the
                        # unknown-session line a flood.
                        logger.debug("Response with non-numeric id %r dropped", msg.id)
                        continue

                    # Check awaited requests first (init, session/new, set_mode)
                    future = self._pending_requests.pop(req_id, None)
                    if future and not future.done():
                        if msg.error:
                            future.set_exception(
                                AcpRuntimeError(_format_runtime_rpc_error(msg.error))
                            )
                        else:
                            future.set_result(msg.result or {})
                        continue

                    # Check routed requests (prompt response → session queue)
                    session_id = self._routed_requests.pop(req_id, None)
                    if session_id and session_id in self._session_queues:
                        await self._session_queues[session_id].put(msg)
                        continue

                    logger.debug("Unmatched response id=%d", req_id)
                    continue

                # Inbound server→client REQUEST (method + id, no result/error).
                # A connection-level request Crew answers ITSELF is one this host's
                # harness names, and today that is the engine's credential callback
                # (_kiro/auth/getAccessToken) — answered only on a process spawned
                # with Crew as the auth owner (see _resolve_spawn_plan /
                # kas_transport.build_kas_argv). It carries no sessionId — the
                # first one arrives before session/new has even returned — so it is
                # handled here, OFF this loop: resolving and possibly refreshing a
                # token must not block stdout demux for every other multiplexed
                # session. On a cli-owned spawn the frame never arrives, and a
                # method the harness does not name falls through to the -32601
                # ownerless answer below rather than ever being paid with a
                # credential. The harness resolves that list from
                # ACP_BACKENDS_HOST_AUTH_CALLBACK, so it and this guard cannot
                # disagree about who answers what.
                if (
                    msg.id is not None
                    and msg.result is None
                    and msg.error is None
                    and self._kas_host_auth
                    and msg.method in self._harness.host_answered_methods
                ):
                    # Same bounded progress-or-dead admission as permission
                    # answers: this is a request, so the counted-drop path that
                    # is valid for notifications is not — and it shares the one
                    # answer-task set so the combined total stays under the real
                    # resource ceiling.
                    if not await self._wait_for_answer_capacity(msg, request_kind="KAS auth"):
                        continue
                    _auth_task = asyncio.ensure_future(
                        self._answer_host_request(msg.id, msg.method or "")
                    )
                    self._answer_tasks.add(_auth_task)
                    _auth_task.add_done_callback(self._answer_tasks.discard)
                    continue
                # Any other request that arrives without a sessionId is
                # unroutable and is answered -32601 by
                # _answer_ownerless_request below, rather than being left to
                # hang.

                # Route notifications by sessionId
                session_id = (msg.params or {}).get("sessionId")
                if not session_id and _subagent_list_update and msg.method == _subagent_list_update:
                    # Snapshot backend-internal subagent session ids before the
                    # broadcast below delivers the frame to the UI consumers.
                    # Each frame carries the FULL current list, so replace.
                    self._snapshot_subagent_sessions(msg.params or {})
                if session_id:
                    # A frame tagged with a sessionId belongs to exactly one
                    # session. Route to it if registered; otherwise DROP it.
                    # Broadcasting a known-but-unregistered session's frame to
                    # every other session would be cross-talk.
                    queue = self._session_queues.get(session_id)
                    if queue is not None:
                        await queue.put(msg)
                    elif (
                        session_id in self._subagent_sessions
                        and self._subagent_owner is not None
                        and list(self._session_queues) == [self._subagent_owner]
                        and (
                            msg.is_method(METHOD_REQUEST_PERMISSION)
                            or msg.method in _session_update
                        )
                    ):
                        # A frame for a backend-internal subagent the backend
                        # itself announced via `subagent/list_update`, on a
                        # runtime with an UNAMBIGUOUS consumer (exactly one
                        # registered session — the dashboard-slot shape).
                        #
                        # - session/update — under EITHER spelling: kiro-cli
                        #   2.21.x emits child updates as the extension method
                        #   `_kiro.dev/session/update` where earlier versions
                        #   used plain `session/update`. Routed so the
                        #   consumer's per-toolCallId caches capture the
                        #   child's REAL command bytes; the handle re-tags
                        #   them as crew activity, never as parent transcript.
                        #   Both spellings must route: a dropped child update
                        #   leaves the caches empty, child MCP identity
                        #   unverified, and every auto-approve path falls to
                        #   the interactive card.
                        # - session/request_permission: routed so the child's
                        #   approval flows through the exact policy pipeline a
                        #   main-agent approval takes — with the command bytes
                        #   above, mode behavior (normal/read/trust/yolo) is
                        #   IDENTICAL to the main agent's. Dropping a REQUEST
                        #   is never an option: it strands the backend's
                        #   response oneshot and wedges the child's whole tool
                        #   batch until process teardown, with every approval in
                        #   that batch hanging invisibly for as long as that
                        #   runtime lives.
                        #
                        # With several registered sessions the frame names no
                        # owner; a permission request then falls to the
                        # fail-closed auto-answer below and updates are
                        # counted drops as before.
                        #
                        # A permission REQUEST is routed only while the owner
                        # has an in-flight prompt (an outstanding routed
                        # request = the dispatch loop is consuming the queue).
                        # Between turns nothing reads the queue until the next
                        # prompt's drain, so a background child's request
                        # would sit unanswered — the original hang with extra
                        # steps. Answer it fail-closed NOW instead.
                        _owner_turn_active = self._subagent_owner in self._turn_active_sessions
                        if (
                            msg.id is not None
                            and msg.is_method(METHOD_REQUEST_PERMISSION)
                            and not _owner_turn_active
                        ):
                            await self._spawn_answer_task(
                                msg,
                                session_id,
                                # Registered + announced — the owner just
                                # has no in-flight prompt. A distinct SEL
                                # tag keeps normal background-child
                                # behavior distinguishable from a real
                                # misconfiguration in the audit trail.
                                reason="owner_no_active_turn",
                            )
                            # Yield so spawned answer tasks actually RUN
                            # between frames: with 129+ frames already
                            # buffered, readline() returns without
                            # suspending, and the reader would hit the
                            # flood cap before any answer task had a chance
                            # to complete — falsely killing a responsive
                            # runtime. One loop-tick lets quick answers
                            # drain; a genuinely wedged backend still
                            # accumulates blocked tasks and trips the cap.
                            await asyncio.sleep(0)
                        elif not _owner_turn_active:
                            # An UPDATE between the owner's turns (either
                            # session/update spelling). Nothing reads the
                            # queue until the next prompt's dispatch loop,
                            # and _run_turn clears the per-toolCallId caches
                            # at turn start and then discards stale
                            # non-permission frames from the queue — so a
                            # between-turns update can never contribute a
                            # cache write or an activity event. Queueing it
                            # would only grow an unbounded queue in gateway
                            # memory while the slot idles (session queues
                            # have no depth cap). Unlike a REQUEST there is
                            # no protocol obligation to answer, so take the
                            # counted-drop path.
                            self._note_dropped_frame(session_id, msg.method)
                        else:
                            # Hang-resilience series: a child permission
                            # request delivered to the mode-parity pipeline.
                            # Counted so routed requests are observable next
                            # to the dropped-frame counter — a permission
                            # request that is neither routed nor answered
                            # leaves its crew waiting on a prompt nobody can
                            # see.
                            if msg.id is not None and msg.is_method(METHOD_REQUEST_PERMISSION):
                                emit_counter(CHILD_PERMISSION_ROUTED, {"surface": "runtime"})
                            await next(iter(self._session_queues.values())).put(msg)
                    elif msg.id is not None and msg.is_method(METHOD_REQUEST_PERMISSION):
                        # Unannounced, ambiguous, or announced past the
                        # recognition cap: nobody on this client can see or
                        # answer the prompt — answer NOW with the request's own
                        # least-destructive reject option so the backend
                        # subagent gets a tool error instead of hanging. Never
                        # auto-APPROVE: no policy engine has seen the tool call
                        # and, past the cap, the id cannot be shown to be this
                        # owner's child at all.
                        #
                        # The reason names WHICH of those it was. Past the cap a
                        # roster truncation, not the backend's silence, is what
                        # cost this child its approval card, and that is the one
                        # thing an operator can act on (the two counts in the
                        # snapshot warning above are the same signal).
                        await self._spawn_answer_task(
                            msg,
                            session_id,
                            reason=(
                                _ROSTER_OVERFLOW_REJECT_REASON
                                if self._subagent_roster_overflow
                                else _UNREGISTERED_REJECT_REASON
                            ),
                        )
                        # Same yield rationale as the routed-owner branch above.
                        await asyncio.sleep(0)
                    elif msg.method in _mcp_init and (
                        # Read live, never hoisted beside _mcp_init: collectors
                        # appear and settle during one reader lifetime.
                        self._session_inits_in_flight
                        or self._start_collectors
                    ):
                        self._stage_init_frame(msg)
                    else:
                        # Counted, not logged per frame: this is the measured
                        # flood (transcript replay during session/load, plus any
                        # backend still streaming after teardown).
                        self._note_dropped_frame(session_id, msg.method)
                    continue

                # No sessionId. An id-carrying frame that still has a method is
                # a server→client REQUEST that names no session — it expects
                # exactly ONE response, so the runtime answers it at connection
                # level (same shape as the KAS auth callback above) instead of
                # broadcasting. Broadcasting would hand it to EVERY registered
                # session's dispatch loop, each of which replies -32601 on the
                # shared stdin: one id, N responses — a JSON-RPC protocol
                # violation that widens with session sharing. Frames with an id
                # but NO method are responses (e.g. a result of null slips past
                # the result/error check above); their handling is unchanged.
                if msg.id is not None and msg.method is not None:
                    # Same volume bound as the permission auto-answers: each
                    # reply can block on stdin drain() against a backend that
                    # floods frames while never reading, so the task must be
                    # retained (a bare ensure_future can be GC'd mid-flight)
                    # and counted. Past the cap the frame takes the counted-
                    # drop path — the flooding backend hangs on its own
                    # unanswered request instead of growing the task set.
                    if len(self._answer_tasks) >= self._max_answer_tasks:
                        self._note_dropped_frame(_DROP_NO_SESSION, msg.method)
                        continue
                    _t = asyncio.ensure_future(self._answer_ownerless_request(msg.id, msg.method))
                    self._answer_tasks.add(_t)
                    _t.add_done_callback(self._answer_tasks.discard)
                    continue

                # No sessionId → genuinely global notification; broadcast to all.
                if self._session_queues:
                    # Snapshot: `await queue.put` yields, and a concurrent
                    # unregister_session() could pop mid-iteration otherwise.
                    _queues = list(self._session_queues.values())
                    # Fanning one ownerless frame out to SEVERAL sessions means
                    # at most one recipient produced it and nothing says which,
                    # so mark it: a consumer that measures its own activity (the
                    # subagent idle-stall clock) must not count another tenant's
                    # traffic. A lone session IS the sole owner, so it is left
                    # unmarked and keeps reading the frame as its own.
                    if len(_queues) > 1:
                        msg.fanout_no_owner = True
                    for queue in _queues:
                        await queue.put(msg)
                else:
                    # Same unbounded shape as the unknown-session branch: with
                    # zero registered sessions EVERY global notification lands
                    # here, so a backend that keeps streaming after the last
                    # teardown floods at frame rate. Counted the same way, with
                    # a sentinel for the session half of the key.
                    self._note_dropped_frame(_DROP_NO_SESSION, msg.method)

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error(
                "Reader loop crashed: %s",
                exc if self.recording_allowed else type(exc).__name__,
                exc_info=self.recording_allowed,
            )
            self._mark_dead(f"reader crash: {exc}")
        finally:
            # Report the residual count on EVERY exit (EOF, overrun, cancel,
            # crash) so a trickle that never reached the interval is still
            # accounted for instead of vanishing with the task. Both throttled
            # summaries: the reader is the only thing that feeds either, so its
            # exit is the last chance to state what they were holding.
            self._flush_dropped_frames()
            self._flush_roster_overflow()

    def _notification_aliases(self) -> NotificationAliases:
        """This host's inbound method spellings, or none when no harness serves it.

        The reader loop is the one seam that must not demand a harness. It is
        driven for every id in ``ACP_BACKENDS_KNOWN``, two of which the
        shared-process runtime has no harness for, and a refusal inside the single
        stdout owner would mark the runtime dead and take every multiplexed session
        with it. The SPAWN is where an unserved backend is refused, before any
        process exists.

        Declaring no aliases DROPS the three aliasable events rather than reading
        them as kiro's, which is the direction that cannot mistake one host for
        another. Routing a frame that names its own session sits upstream of this
        and is unaffected.
        """
        try:
            return self._harness.notification_aliases
        except ValueError:
            return NotificationAliases()

    async def _answer_get_access_token(self, request_id: int | str) -> None:
        """Answer this host's credential callback, by its own method name.

        Kept under this name because it is the shape callers and tests ask for;
        the method the host actually sent is resolved from its harness.
        """
        await self._answer_host_request(request_id, METHOD_KAS_AUTH_GET_ACCESS_TOKEN)

    async def _answer_host_request(self, request_id: int | str, method: str) -> None:
        """Answer a connection-level request this host's harness claims.

        Runs OFF the reader loop. The response is built by the HARNESS that named
        ``method`` in ``host_answered_methods``, never by a hardcoded answerer:
        the guard upstream already accepts any method a host claims, so a fixed
        answerer would hand one host's credential to another host that happened to
        claim a different method — the mistaken-identity failure this layer exists
        to prevent. For KAS that answer resolves and refreshes under the
        cross-process lock and withholds the refresh token; it is never cached
        here and never logged.

        On any failure the host is sent a JSON-RPC error, which it treats as an
        expired credential and turns into its sign-in prompt, rather than being
        left to hang on the callback. The INFO line is the positive "host is
        drawing its credential from Crew" signal, logged once per runtime and
        only AFTER a response was actually written, so a callback that failed
        never counts as served.
        """
        try:
            result = await self._harness.answer_request(method)
        except HostAuthCallbackError as exc:
            # str(exc) is token-free by construction (see kas_host_auth).
            logger.warning("KAS auth callback failed: %s", exc)
            try:
                await self.send_error(request_id, KAS_AUTH_CALLBACK_ERROR_CODE, str(exc))
            except AcpRuntimeDead:
                pass
            return
        try:
            await self.send_response(request_id, result)
        except AcpRuntimeDead:
            # Process gone before the answer could be written; nothing to do.
            return
        if not self._kas_host_auth_logged:
            self._kas_host_auth_logged = True
            logger.info(
                "KAS auth callback served from Crew vault — agent=%s (PID %s)",
                self._agent or "<none>",
                self._pid,
            )
        else:
            logger.debug(
                "KAS auth callback served from Crew vault — agent=%s (PID %s)",
                self._agent or "<none>",
                self._pid,
            )

    async def _answer_ownerless_request(self, request_id: int | str, method: str) -> None:
        """Answer a server→client request that names no session with -32601.

        Runs OFF the reader loop (same shape as the KAS auth callback) so a
        stalled stdin drain cannot block stdout demux for every multiplexed
        session. The routed case — an unknown request WITH a sessionId — is
        deliberately not handled here: it is delivered to that session's queue
        and answered once by its dispatch loop (``server_request_unknown``).
        """
        logger.debug(
            "Ownerless server request answered -32601 — method=%s id=%r",
            method,
            request_id,
        )
        try:
            await self.send_error(request_id, _JSONRPC_METHOD_NOT_FOUND, "Method not found")
        except AcpRuntimeDead:
            pass

    def saw_not_logged_in(self) -> bool:
        """True if kiro-cli reported an auth failure on stderr.

        Lets callers translate a runtime death into AcpAuthRequired (an
        actionable login prompt) instead of a generic process-death error —
        parity with AcpClient, which inspects stderr the same way.

        Recognises the full auth vocabulary rather than the literal banner
        ``not logged in``: a real expired bearer token writes
        ``AccessDeniedException: "Invalid token"`` and ``the bearer token
        included in the request is invalid`` and says ``not logged in`` nowhere,
        so a single-banner regex answers False on exactly the state this exists
        to detect, and the operator is shown a ``session/new`` timeout instead.

        Reads the latch, not the ring buffer: see ``_saw_auth_failure``.
        """
        return self._saw_auth_failure

    async def settle_stderr(self, timeout: float = 0.5) -> None:
        """Give the stderr drain a bounded chance to finish before its latches are read.

        The latches (:meth:`saw_sandbox_init_failure`, :meth:`saw_not_logged_in`)
        are written by the ``_drain_stderr`` TASK, while a death is discovered on
        the stdout side: ``_reader_loop`` sees EOF and ``_mark_dead`` fails the
        pending ``initialize`` future SYNCHRONOUSLY. Both tasks become runnable
        together -- which is the ordinary shape of a real refusal, since the child
        writes its signature and closes stdout at once -- so a caller that reads a
        latch straight off that failure can win the race and see ``False`` for a
        line already in the pipe.

        Bounded and swallowing, because the caller is already on a failure path:
        the worst case of not settling is the generic error it would have produced
        anyway, and no failure here may become a second failure. The same shape
        and the same budget as ``AcpClient._read_message``'s own EOF drain.
        """
        task = self._stderr_task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (Exception, asyncio.CancelledError):
            pass

    def saw_sandbox_init_failure(self) -> bool:
        """True if an OS sandbox told this runtime's child it could not initialize.

        Lets callers translate a runtime death into ``AcpSandboxInitFailed`` -- a
        non-retryable error naming the layer that refused and its switch --
        instead of a generic process-death error the retry ladder then reproduces
        on a host that will refuse identically. Parity with
        :meth:`saw_not_logged_in`, including reading the latch rather than the
        ring buffer.
        """
        return self._saw_sandbox_init_failure

    @property
    def sandbox_wrapped_by_crew(self) -> bool:
        """Whether Kiro Crew's own sandbox wrapped this runtime's child.

        The one fact a sandbox refusal's stderr cannot carry: WHICH layer to turn
        off. A harness's internal sandbox nested inside Crew's wrap fails with the
        harness's wording while the layer the operator must change is Crew's.
        """
        return self._sandbox_wrapped_by_crew

    def redacted_stderr_tail(self) -> str:
        """The retained stderr lines, newline-joined and redacted.

        A SEPARATE reader from :meth:`death_summary`, which exists to be read by a
        person: it folds the same lines onto one line behind a ``returncode``
        prefix. Anything that classifies PER LINE -- the launcher's own refusal
        test -- sees nothing in that shape, so it needs the lines as lines.

        Redacted like every other path out of this buffer: child stderr is
        untrusted subprocess output that can echo a credential. Empty for a
        restricted session, which retains nothing.
        """
        if not self._stderr_lines:
            return ""
        tail = "\n".join(self._stderr_lines)
        tail, _ = redact_exfiltration_urls(tail)
        tail, _ = redact_credentials(tail)
        return tail

    @property
    def sandbox_mode(self) -> str:
        """The sandbox tier this runtime's child was spawned under."""
        return self._sandbox_mode

    @property
    def sandbox_hidden_dirs(self) -> tuple[str, ...]:
        """The extra path masks this spawn asked its sandbox for.

        Read with :attr:`sandbox_mode` when a trusted corroboration run has to
        rebuild the profile this spawn was actually refused under.
        """
        return self._sandbox_hidden_dirs

    def _exit_reason(self, rc: object) -> str:
        """The death reason for a process that exited: rc plus what it last said.

        A bare ``rc=1`` told the operator nothing when every tool started
        failing because the runtime tmpfs had run out of inodes. The last
        non-empty stderr line the drain captured is appended (bounded by
        ``_STDERR_REASON_TAIL_CHARS``), and an ENOSPC signature in it earns a
        pointer at the doctor check that measures that filesystem. Best-effort:
        the stderr drain is a separate task, so a line still in flight when
        stdout closed is not seen here, and the reason then stays ``rc=N``.
        """
        reason = f"process exited (rc={rc})"
        if not self.recording_allowed:
            return reason
        last = next((ln for ln in reversed(self._stderr_lines) if ln.strip()), "")
        if not last:
            return reason
        # The marker is matched on the whole line, so a signature past the cap
        # still earns the hint; only what the card shows is cut.
        enospc = _ENOSPC_MARKER in last.lower()
        # A child's stderr is untrusted text that can echo a token or an
        # authority-bearing URL (a failed login prints the header it sent), and
        # the reason travels to the session card and the SEL, so it is redacted
        # before the cut; the cut then cannot split a secret into a half the
        # redactor cannot recognise.
        last, _ = redact_credentials(last)
        last, _ = redact_exfiltration_urls(last)
        if len(last) > _STDERR_REASON_TAIL_CHARS:
            last = last[:_STDERR_REASON_TAIL_CHARS] + "…"
        reason = f"{reason}: {last}"
        if enospc:
            reason = f"{reason} — {_ENOSPC_HINT}"
        return reason

    # Rendered in place of a returncode that is not knowable YET. Both say
    # "no exit status", and an operator reading only the log cannot tell them
    # from a signal-killed child whose status was never captured -- which is
    # exactly the report this labelling answers.
    _RC_NOT_REAPED = "<not reaped>"
    _RC_NO_PROCESS = "<no process>"

    def _returncode_label(self) -> str:
        """This runtime's exit status for the death log, or why there is none.

        ``_kill_inner`` marks the death BEFORE it signals and reaps (pending
        waiters must learn of the death first), so every kill of a live
        runtime reads ``returncode is None`` here. So does a reader crash or a
        broken pipe on a child that is still running, and a never-spawned
        runtime has no process to ask at all. None of the three is "died by
        signal, status unknown", so none of them prints a bare ``None``.
        """
        if self._process is None:
            return self._RC_NO_PROCESS
        rc = self._process.returncode
        return self._RC_NOT_REAPED if rc is None else str(rc)

    def _compose_death_summary(self, reason: str, rc: str, tail: str) -> None:
        """Retain the one-line attribution ``death_summary()`` hands out.

        The parts are kept alongside it so the post-reap amendment can
        RECOMPOSE the line from them. Rewriting the composed text instead
        would search the child's stderr tail as well, and a tail that happened
        to carry this method's own ``[returncode=...]`` shape would be edited
        into an exit status -- the diagnostic corrupting the evidence it exists
        to carry.
        """
        self._death_reason = reason
        self._death_tail = tail
        self._death_summary = f"{reason} [returncode={rc}] stderr_tail: {tail}"

    def _note_reaped_after_kill(self, rc: object) -> None:
        """Fill in the exit status the kill path's death line could not know.

        Called once the reap has completed, while the process handle is still
        held. The retained summary is amended because it OUTLIVES the log --
        it rides ``AcpProcessDied`` into a turn's error and a cron's
        ``last_error`` -- and one line is logged at the death's own severity
        so the gateway log holds the code too. Silent when the status is still
        unknown (both waits timed out): ``<not reaped>`` is then still true.
        """
        if rc is None or self._death_summary is None:
            return
        if self._death_label != self._RC_NOT_REAPED:
            return
        self._death_label = str(rc)
        self._compose_death_summary(self._death_reason, self._death_label, self._death_tail)
        log = logger.info if self._death_expected else logger.warning
        log("AcpRuntime reaped after kill (PID %s): returncode=%s", self._pid, rc)

    def _mark_dead(self, reason: str, *, expected: bool = False) -> None:
        """Mark runtime dead, fail all pending requests, poison all session queues.

        ``expected`` selects only the log severity: a deliberate teardown (a
        warm-pool TTL recycle, a session shutdown) logs at INFO, while every
        genuine death path (process exit, reader crash, broken pipe, ...) keeps
        today's WARNING. The default is False so any death path added later is
        a WARNING without having to opt in. Everything else — the ``_dead``
        early return, PID unshielding, failing pending futures, poisoning
        session queues — is identical on both paths.
        """
        if self._dead:
            return
        self._dead = True
        # A process that already exited on its own is a genuine death being
        # reaped, not a teardown this caller initiated — refuse the downgrade
        # regardless of call site. This closes the race where a replacement
        # path observes is_alive() == False (returncode set by the child
        # watcher) and kill()s before the reader loop has marked the death.
        if expected and self._process is not None and self._process.returncode is not None:
            expected = False
        self._death_expected = expected
        # Release the sweep-protection shield on ANY death path (EOF, rc!=0,
        # stdout overrun, reader crash, broken pipe) — not just kill(). Otherwise
        # the dead PID lingers in _PROTECTED_PIDS forever and, after PID reuse,
        # could shield a genuinely-orphaned process from the orphan sweep.
        if self._pid:
            try:
                unregister_protected_pid(self._pid)
            except Exception:
                logger.debug(
                    "AcpRuntime: unregister protected pid failed for %s", self._pid, exc_info=True
                )
        # Diagnostic context: process returncode + tail of captured stderr so
        # operators can tell an OOM/crash from a clean exit without DEBUG logs.
        rc = self._returncode_label()
        if self.recording_allowed:
            tail = " | ".join(self._stderr_lines[-5:]) if self._stderr_lines else "<none>"
        else:
            # Reader exceptions and child stderr can echo session content.
            # Restricted runs retain lifecycle facts, including the exit code.
            reason = "runtime stopped" if expected else "runtime failed"
            tail = "<not retained>"
            self._stderr_lines.clear()
        # Redact BEFORE composing: the summary outlives this method — it is
        # retained for death_summary(), appended to AcpProcessDied, and a
        # cron turn's failure stringifies that exception into job.last_error,
        # which persists to sandbox-visible crons.json. Child stderr is
        # external-subprocess output that can carry credential material
        # (same treatment as the send-path's 'ACP process exited' detail).
        tail, _ = redact_exfiltration_urls(tail)
        tail, _ = redact_credentials(tail)
        # Retain the summary for death_summary(): consumers that learn of the
        # death only through a poisoned queue (a live turn's frame wait) can
        # then attach WHO/WHY to their own error instead of raising bare.
        self._death_label = rc
        self._compose_death_summary(reason, rc, tail)
        log = logger.info if expected else logger.warning
        log(
            "AcpRuntime dead (PID %s): %s [returncode=%s] stderr_tail: %s",
            self._pid,
            reason,
            rc,
            tail,
        )

        exc = AcpRuntimeDead(reason)
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(exc)
        self._pending_requests.clear()
        self._pending_init_notifications.clear()
        # Also drop routed-request correlations: on death no reader will pop
        # them, and if a session is never destroyed the entry would otherwise
        # linger. unregister_session() also sweeps these per-session; this is
        # belt-and-suspenders for the process-death-before-response case.
        self._routed_requests.clear()

        for queue in list(self._session_queues.values()):
            try:
                queue.put_nowait(None)  # poison sentinel
            except asyncio.QueueFull:
                pass

    # ── Protocol Interface (used by AcpSessionHandle) ──

    async def send_request(self, method: str, params: dict[str, Any]) -> int:
        """Send a JSON-RPC request and return the request id.

        The response will be routed to the session's queue (via _routed_requests)
        so AcpSessionHandle can detect turn completion. For requests that need
        an immediate response (init, session/new), use _send_and_await instead.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        projection = getattr(self, "_native_skill_projection", None)
        if projection is not None:
            params = projection.request(method, params)
        req_id = self._next_id
        self._next_id += 1

        # Register for session routing so the response goes to the right queue
        session_id = params.get("sessionId")
        if session_id and session_id in self._session_queues:
            self._routed_requests[req_id] = session_id

        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._routed_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()
        return req_id

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected).

        Unlike send_request, this does NOT allocate an id or register routing,
        so it leaves no _routed_requests entry to leak when the server (per the
        ACP spec) sends no response back (e.g. session/cancel).
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()

    async def send_response(self, request_id: str | int, result: dict[str, Any]) -> None:
        """Send a JSON-RPC response (for server→client requests like permission)."""
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    async def send_error(self, request_id: str | int, code: int, message: str) -> None:
        """Send a JSON-RPC error response."""
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session queue (called by AcpSessionHandle.destroy)."""
        self._session_queues.pop(session_id, None)
        # Clean up any pending routed requests for this session
        stale = [k for k, v in self._routed_requests.items() if v == session_id]
        for k in stale:
            del self._routed_requests[k]
        # The departing session takes its subagent ownership with it: a later
        # session on this warm runtime must never inherit a stale child's
        # approvals (the announce set is re-learned from the next
        # subagent/list_update, which re-establishes ownership explicitly).
        self._turn_active_sessions.discard(session_id)
        if self._subagent_owner == session_id:
            self._subagent_owner = None
            self._subagent_sessions = set()
            # The overflow count describes the set that just went away, so it
            # travels with it: a stale non-zero value would keep auditing later
            # unknown-session denials as cap truncations on a runtime whose
            # roster is empty.
            self._subagent_roster_overflow = 0
            # Same boundary for the log: the departing roster's truncation
            # episode is over, so its residual repeat count is flushed and the
            # next owner's first truncation is loud again.
            self._end_roster_overflow_episode()
        logger.debug("Removed session %s", session_id)

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        """Record whether a session's prompt dispatch loop is consuming.

        Called by ``AcpSessionHandle.prompt()`` on entry and (in a finally)
        on every exit path. Child permission requests are routed only while
        the owner is marked active; otherwise they are answered fail-closed
        immediately, because nothing reads the queue until the next turn.
        """
        if active:
            self._turn_active_sessions.add(session_id)
        else:
            self._turn_active_sessions.discard(session_id)

    async def terminate_session(self, session_id: str) -> None:
        """Evict a session from kiro-cli (freeing its memory), then unregister locally.

        Sends the ``_kiro.dev/session/terminate`` request so the multiplexed
        kiro-cli process drops this session from its in-memory session map and
        shuts the session's agent down (reaping its MCP child processes). WITHOUT
        this, a finished session's transcript + context stay resident in the
        shared process for its entire lifetime — so RSS grows without bound as
        background tasks and subagents accumulate (the multiplexed design has no
        per-turn compaction, so per-session eviction is the only reclaim signal).

        This is co-tenant-safe: it targets exactly one ``sessionId`` and never
        touches the process, unlike ``kill()`` (which would take every sibling
        session down with it).

        Best-effort and bounded: teardown must never hang or raise. If the
        runtime is already dead the session's memory died with the process, so
        the round-trip is skipped. The local ``unregister_session`` ALWAYS runs
        (``finally``) so the reader loop stops routing to an abandoned queue even
        when the terminate request could not be delivered — including when the
        enclosing task is cancelled mid-await (``asyncio.CancelledError`` is a
        ``BaseException``, so it would otherwise slip past the ``except Exception``).

        The DELIVERY comes from the harness's own ``TeardownPolicy``, not from one
        shape applied to every host: a verb the host answers is a request bounded by
        ``_TERMINATE_TIMEOUT``, and a verb it does not answer is a notification. The
        distinction is invisible in the method name, and getting it wrong is expensive
        in the direction that looks like nothing is wrong — the eviction still happens,
        just a whole budget later, with a timeout logged against it.
        """
        try:
            if not self._dead and self._process is not None:
                policy = self._harness.teardown
                try:
                    if policy.notification:
                        # A verb the host does not answer. Awaiting a reply here would
                        # spend the whole teardown budget on every eviction and report
                        # it as a control-plane timeout, so the delivery follows what
                        # the harness declares rather than one shape for every host.
                        await self.send_notification(policy.method, {"sessionId": session_id})
                    else:
                        await self._send_and_await(
                            policy.method,
                            {"sessionId": session_id},
                            timeout=_TERMINATE_TIMEOUT,
                        )
                except Exception:
                    logger.debug(
                        "session teardown failed for %s (runtime dead/slow); "
                        "proceeding with local unregister",
                        session_id,
                        exc_info=True,
                    )
        finally:
            self.unregister_session(session_id)

    def _session_teardown_method(self) -> str:
        """The verb that frees one session on this backend.

        kiro-cli's terminate evicts the session from the process and leaves the
        transcript on disk for the caller to deal with. KAS offers no evict-only
        equivalent, so its delete does both at once.

        That difference is invisible here but matters to the caller: on KAS the
        local ``AcpSessionHandle._cleanup_transcript`` is a NO-OP, because it
        unlinks from kiro-cli's sessions dir and KAS keeps its own store. So the
        ``keep_transcript`` guard does not protect anything on KAS — a KAS
        session's record is gone once this verb returns. The only capability that
        loses is opportunistic ``spawn_continue`` on a shared subagent, which
        degrades to a typed ``conversation_gone`` and a re-spawn; explicitly
        continuable runs are dedicated sessions that never reach this path.
        """
        return self._harness.teardown.method

    # ── Session Management ──

    def _mcp_init_progress(self, expected: Any) -> str:
        """Describe MCP registration progress for a session start that stalled.

        Reads the frames the reader loop already staged in
        ``_pending_init_notifications`` so a session-start timeout can name the
        servers that never reported, instead of reporting only the elapsed
        budget. Must run BEFORE ``_finish_session_init``, which drops that
        buffer once the last in-flight init closes.

        ``expected`` is the ``mcpServers`` array sent with the request. Its
        entries carry the roster names, and that is what makes the ABSENT
        servers nameable rather than only the present ones.

        The text says what that roster IS. On kiro-cli the array holds only the
        broker stubs Kiro Crew injects (``pooled_session_servers``); the agent
        spec's own servers are started by the backend and are not in it, and
        the backend's session-start steps after MCP init are not observable
        from here at all. A bare ``4/4 MCP server(s) reported`` therefore read
        as "all MCP is up, so MCP is the problem" -- a field report was
        triaged that way on the strength of the suffix alone -- when it only
        ever meant that the four injected servers had spoken. The count is
        now labelled ``session-injected``, and a complete roster is followed
        by what it does and does not cover, so a reader is not sent to chase
        MCP for a stall that is past it.

        Reports are runtime-wide rather than per-session: a request that never
        answered has no session id to match its frames against, so a concurrent
        init is called out in the text instead of being silently folded in. What
        keeps that bounded to the CONCURRENT inits is that a start whose caller
        already gave up keeps its own frames on its :class:`StartCollector`
        instead of here — un-keyed by name, they would otherwise be read as this
        attempt's progress and hide the servers that never reported.
        Likewise the staging deque is bounded, so on a very large fleet the
        reported count is a floor, not an exact tally.
        """
        roster = [
            _sanitize_progress_name(str(e.get("name") or ""))
            for e in (expected if isinstance(expected, list) else [])
            if isinstance(e, dict) and e.get("name")
        ]
        ready: list[str] = []
        failed: list[str] = []
        failure_text: dict[str, str] = {}
        awaiting_auth: list[str] = []
        for msg in self._pending_init_notifications:
            params = msg.params if isinstance(msg.params, dict) else {}
            name = _sanitize_progress_name(
                str(params.get("serverName") or params.get("name") or "")
            )
            if not name:
                continue
            if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
                if name not in ready:
                    ready.append(name)
            elif msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
                # A failed server's error text can carry connection strings or
                # tokens from its startup, so it takes the same scrub the
                # dashboard banner applies before it lands in an exception.
                err, _ = redact_exfiltration_urls(str(params.get("error") or ""))
                err, _ = redact_credentials(err)
                err = _strip_unprintable(" ".join(err.split()))[:_MCP_PROGRESS_ERROR_CAP]
                if name not in failed:
                    failed.append(name)
                if err:
                    failure_text[name] = err
            elif msg.is_method(METHOD_MCP_OAUTH_REQUEST):
                if name not in awaiting_auth:
                    awaiting_auth.append(name)

        reported = set(ready) | set(failed)
        parts: list[str] = []
        if roster:
            # Count only reports that belong to the roster. kiro-cli initializes
            # the agent spec's own servers as well as the session-injected ones,
            # so the staged frames are a SUPERSET of the roster and a raw
            # len(reported) can exceed the denominator -- "2/1 reported". The
            # out-of-roster servers still appear by name in the failed and
            # awaiting-authorization buckets, where naming them is the point.
            parts.append(
                f"{len(reported & set(roster))}/{len(roster)} session-injected "
                "MCP server(s) reported"
            )
            silent = [n for n in roster if n not in reported]
            if silent:
                parts.append(f"no report from {_capped_names(silent)}")
            elif not set(failed) & set(roster):
                # Every roster member reported READY. A member that reported an
                # init failure counts as reported (so it is never chased as
                # silent) but is named under ``failed:`` below, and the stall
                # may be in it -- so the "not in those servers" verdict is
                # withheld then.
                parts.append(MCP_ROSTER_COMPLETE_NOTE)
        else:
            # No roster to attribute against: these reports belong to the agent
            # spec's own servers or to a concurrent start, so the count is not
            # labelled "session-injected" here.
            parts.append(f"{len(reported)} MCP server report(s), roster unknown")
        if failed:
            parts.append(
                "failed: "
                + _capped_names(
                    [f"{n} ({failure_text[n]})" if n in failure_text else n for n in failed]
                )
            )
        if awaiting_auth:
            parts.append(f"awaiting authorization: {_capped_names(awaiting_auth)}")
        if self._session_inits_in_flight > 1:
            parts.append(
                f"{self._session_inits_in_flight} session inits in flight, "
                "so these reports are runtime-wide"
            )
        return "; ".join(parts)

    def _session_start_stalled(
        self, exc: AcpRequestTimeout, method: str, expected: Any
    ) -> AcpRequestTimeout:
        """Attach MCP progress to a session-start timeout before it reaches the user.

        Session start is the one request whose cost is dominated by work the
        runtime can observe, so the bare budget is the least useful half of the
        answer. Returns a replacement to raise rather than raising here, so the
        caller keeps the ``from exc`` chain.
        """
        progress = self._mcp_init_progress(expected)
        logger.warning("%s stalled: %s", method, progress or "no MCP reports staged")
        # Tag the exception the caller will raise as a SESSION-START failure,
        # whichever of the two it is: both reach a self-driving caller as "the
        # cycle never got a session", and the tag is how that caller counts the
        # streak without reading the message text.
        exc.session_start_failed = True
        if not progress:
            return exc
        replacement = AcpRequestTimeout(f"{exc} ({progress})")
        replacement.session_start_failed = True
        return replacement

    def _stage_init_frame(self, msg: JsonRpcMessage) -> None:
        """Hold one MCP-init frame until the session id that claims it is known.

        ``session/new`` can emit OAuth and MCP registration frames before its
        response, and the response is what gives ``create_session`` the id needed
        to register the queue. Registration frames matter beyond logging:
        ``drain_init()`` arms its idle shortcut on the first one, so dropping them
        here makes the session look report-less and pay the full no-report
        ceiling.

        Two holders, and a frame goes to both — the in-flight init scope's deque,
        which ``_mcp_init_progress`` also reads un-keyed, and every live
        :class:`StartCollector`, which owns a start whose id is still unknown.
        Neither can hand a frame to the other's session (both claim by the id in
        the frame, and two ``session/new`` answers never carry the same one), and
        both are bounded, so a frame nobody claims cannot accumulate.
        """
        if self._session_inits_in_flight:
            self._pending_init_notifications.append(msg)
        for collector in self._start_collectors.values():
            collector.stage_init_frame(msg)

    def _finish_session_init(self, session_id: str) -> list[JsonRpcMessage]:
        """Take staged init frames for one session and close its init scope."""
        matched, self._pending_init_notifications = _split_init_frames(
            self._pending_init_notifications, session_id
        )
        self._session_inits_in_flight -= 1
        if self._session_inits_in_flight == 0:
            # Anything unmatched belongs to a failed/abandoned init. Never let
            # it survive into the next session creation attempt.
            self._pending_init_notifications.clear()
        return matched

    def _activates_agent_by_mode(self) -> bool:
        """Whether ``session/set_mode`` names something this host can resolve.

        Read from ``ACP_BACKEND_ROUTING`` rather than declared here or asked of
        the harness: the hosts whose privileged tools are governed by an agent
        spec are exactly the hosts that HAVE an agent to activate, and a second
        copy of that membership would be free to disagree with the one that
        decides. kiro-cli and the KAS relay are that population.

        A host routed another way has no agent id to send. codex's modes are its
        own permission tiers (``read-only`` / ``agent``), so a Crew agent name
        resolves to nothing there and the request faults — taking down, on the
        cleanup path below, a session that had started fine.
        """
        return acp_tool_gate.routing_for(self.acp_backend) is acp_tool_gate.Routing.AGENT_SPEC

    @staticmethod
    def _mode_available(agent: str, resp: dict[str, Any]) -> bool:
        """Whether ``set_mode`` should be attempted for ``agent`` given a
        ``session/new``|``session/load`` response.

        True when the backend advertised no ``modes`` list at all (older kiro-cli
        / offline fake backend — attempt for backward compatibility) OR the agent
        is in the advertised ``availableModes``. False when a modes list WAS
        advertised (even an empty one) and the agent is absent — the case that
        would otherwise fault with ``-32603 "Mode '<agent>' not found"``. An
        explicitly-empty ``availableModes: []`` therefore fails closed, not open.
        """
        ids, _current, advertised = parse_session_modes(resp)
        if not advertised:
            return True
        return agent in ids

    async def _verify_spawn_agent_active(
        self,
        session_id: str,
        resp: dict[str, Any],
        *,
        override: str | None,
    ) -> None:
        """Fail closed when the agent the ``--agent`` flag selected never loaded.

        Guard (A2) — the spawn-flag half of Guard (A) in :meth:`create_session`.
        ``set_mode`` only ever activates an EXPLICIT override (or, on KAS, the
        injected default), so on kiro-cli the agent chosen by ``--agent`` — the
        agent of every ordinary session — reaches no availability check at all.
        It needs one, because that spec can fail to load silently: kiro-cli
        validates ``~/.kiro/agents/<agent>.json`` with ``deny_unknown_fields``
        and, on ANY unknown field, rejects the spec wholesale and runs its own
        default agent instead. :func:`kiro_crew.agent.migrate_agent_specs` exists
        to strip the two keys already known to trip it; nothing validates the
        rest, and a spec written by another tool (or by a product this install
        superseded) can carry more.

        Nothing downstream notices the substitution. The ``set_mode`` response is
        never read back, ``currentModeId`` is never re-compared, and
        :mod:`kiro_crew.acp.mcp_session_report` only LOGS — its own docstring
        forbids reading a missing report as "not mounted". The session then runs
        with NONE of Kiro Crew's control plane, while the global provider
        ``mcp.json`` that Kiro Crew pins off only on specs IT writes stays
        merged. So third-party MCP servers declared there keep working and every
        Kiro Crew tool the injected prompt names — ``learn_add`` among them —
        answers "does not exist", which the agent reports to the user as its
        memory being unavailable.

        Skipped when an override is requested: Guard (A) checks the agent
        ``set_mode`` will activate.

        ``currentModeId`` is read as PROOF, in both directions. A live probe of
        this backend settles what it means: a spec that loads is reported as the
        current mode AND listed in ``availableModes``, while a spec the backend
        refuses is absent from the list and ``currentModeId`` names the backend's
        own default instead. So a non-empty ``currentModeId`` naming something
        OTHER than the spawn agent is positive evidence of the substitution, and
        fails closed even when no advertised list came back. Treating a
        current-mode mismatch as an extra ADMIT is the hole this avoids: a
        ``currentModeId``-only response naming a substituted agent would otherwise
        sail through the compatibility escape.

        That escape is therefore narrow. It applies only when the response names
        NO current mode, where the advertised list is the sole signal and its
        absence is no evidence of a substitution -- so older kiro-cli and the
        offline fake backend behave exactly as before.

        Asked of the harness, which answers yes only for a host whose argv
        actually carries ``--agent``. Never phrased as an inequality: a negative
        test would silently capture every host added later (harness-parity H5),
        and a host that activates its agent by ``set_mode`` already has that
        response as proof -- on KAS the agent travels over the wire as an injected
        custom agent and Guard (A) covers it. Asked of the host rather than of
        "the KAS projection came back None" so the same call serves
        :meth:`load_session`, which never builds that projection.
        """
        if override or not self._agent:
            return
        if self._harness.verifies_agent_activation:
            spawn_agent = self._agent
            ids, current, _adv = parse_session_modes(resp)
            if current:
                if current == spawn_agent:
                    return
            elif self._mode_available(spawn_agent, resp):
                return
            await self.terminate_session(session_id)
            raise AcpRuntimeError(
                f"Agent {spawn_agent!r} was spawned with --agent but is not the "
                f"agent this session is running (current mode: "
                f"{current or '(none reported)'}; advertised: {ids or 'none'}). "
                + await self._spawn_agent_not_loaded_reason(spawn_agent)
            )

    async def _spawn_agent_not_loaded_reason(self, spawn_agent: str) -> str:
        """Why the ``--agent`` spec did not load, and the remedy, for Guard (B).

        Crew's roster lists an agent defined as one markdown file (``<name>.md``,
        the v3 / Kiro IDE form) for every backend, but a host that answers False
        to ``reads_markdown_agent_specs`` discovers ``*.json`` alone, so such an
        agent reaches this guard exactly like a missing JSON spec does -- and the
        generic advice ("run setup to rewrite the agent config") would send the
        operator to repair a file that is not the problem. The markdown check is
        asked only once the failure has ALREADY happened, on the refusal branch,
        so the spawn path itself gains no gate and no failure mode
        (harness-parity H13): the host answers from
        ``ACP_BACKENDS_MARKDOWN_AGENT_SPECS``, never "is kiro", and a host added
        later that reads markdown is handed the generic text unchanged.
        """
        if not self._harness.reads_markdown_agent_specs:
            markdown_spec = await asyncio.to_thread(
                markdown_spec_for_agent, spawn_agent, self._work_dir
            )
            if markdown_spec is not None:
                capable = ", ".join(repr(b) for b in sorted(ACP_BACKENDS_MARKDOWN_AGENT_SPECS))
                return (
                    f"{spawn_agent!r} is defined in markdown ({markdown_spec.name}); the "
                    f"{self._harness.backend or 'kiro'!r} backend loads JSON agent specs "
                    f"only, so it ran its own default agent instead, which would "
                    f"silently drop every Kiro Crew tool the agent's prompt relies on. "
                    f"Switch agent.acp_backend to {capable} or add a JSON spec for this "
                    f"agent."
                )
        return (
            f"Its ~/.kiro/agents/{spawn_agent}.json is missing, or the backend "
            f"refused to load it. Refusing to run the backend's own default "
            f"agent in its place, which would silently drop every Kiro Crew "
            f"tool the agent's prompt relies on. Run `kirocrew setup "
            f"--agent-only` to rewrite the agent config."
        )

    async def _activate_mode_bracketed(
        self,
        session_id: str,
        mode_agent: str,
        *,
        budget: float,
        payload_snapshot: Any,
        wire_registered: bool,
    ) -> None:
        """Send ``session/set_mode`` for *mode_agent* inside the derived-spec bracket.

        ONE body for both session-start paths (create and resume), because the bracket
        is a protocol and a protocol written twice is two protocols the moment one copy
        is edited. Any failure terminates *session_id* first: ``session/new`` or
        ``session/load`` already succeeded, so the session exists in the host and a
        plain local unregister would leak it in the shared process.

        A ``set_mode`` naming an agent activates that agent's spec, and the spec it
        activates needs the same bracket the spawn's does. Neither other gate reaches
        here: the spawn gate is keyed to ``self._agent``, so a SHARED runtime spawned
        as one agent and switched to another on this line passes no gate, and a host
        that builds no in-process tool surface never runs ``session_mcp``'s gate.

        WHERE the spec is consumed differs by host, and that decides which snapshot the
        post-check may use -- exactly ONE per consumed load:

        * ``wire_registered`` -- the spec was consumed at ``session/new``, in the wire
          payload built under the projection's own gate; ``set_mode`` activates what is
          already registered and re-reads nothing. A fresh read HERE would judge the
          file while the host holds the payload, so a revocation landing between the
          build and this line would pass that read while the registered definition
          still carried the grants it removed. The post-check compares against the
          payload's OWN snapshot. No gate call on this path: a second snapshot for one
          consumed load is the defect, not a safeguard.
        * otherwise -- the spec is consumed HERE: kiro-cli reads it from disk at
          ``set_mode`` and boots that agent's MCP servers. Gated BEFORE the send,
          because a stale mirror activated here mounts and auto-approves a server the
          default agent does not have.

        After the send returns the host has consumed the spec, which closes the window
        the snapshot opened: a write landing before that point is caught, and one
        landing after cannot change what was already consumed. Same answer as the
        ``initialize`` bracket -- a session that may have activated an unverified spec
        must not survive.
        """
        from kiro_crew.agent import (
            DerivedSpecStale,
            require_fresh_derived_spec,
            require_unchanged_derived_spec,
        )

        if wire_registered:
            mode_snapshot = payload_snapshot
        else:
            try:
                mode_snapshot = await asyncio.to_thread(
                    require_fresh_derived_spec, mode_agent, self._work_dir
                )
            except DerivedSpecStale as exc:
                await self.terminate_session(session_id)
                raise AcpRuntimeError(str(exc)) from exc
        try:
            if getattr(self, "_native_skill_projection", None) is not None:
                from kiro_crew.acp.skill_projection import prepare_native_skill_projection

                # Keep the transport mode selected at spawn for this process.
                # The rollback environment switch takes effect after restart.
                self._native_skill_projection = await asyncio.to_thread(
                    prepare_native_skill_projection, self._work_dir, enabled=True
                )
            # set_mode is a handshake request: switching to an agent boots THAT
            # agent's MCP servers, the same server (re-)initialization that gives
            # session/new and session/load their 90s budget. A switched-to server
            # pending OAuth holds the response for its full 30s wait, so the generic
            # _REQUEST_TIMEOUT would turn set_mode into the SAME race the
            # session-start floor exists to prevent (see _SESSION_NEW_TIMEOUT).
            await self._send_and_await(
                METHOD_SET_MODE, set_mode_params(session_id, mode_agent), timeout=budget
            )
        except Exception:
            await self.terminate_session(session_id)
            raise
        try:
            await asyncio.to_thread(require_unchanged_derived_spec, mode_snapshot)
        except DerivedSpecStale as exc:
            await self.terminate_session(session_id)
            raise AcpRuntimeError(str(exc)) from exc

    async def _handshake_client_capabilities(self) -> dict[str, Any]:
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
        else the user-level file ``load_agent_spec`` reads -- never a project
        checkout's ``.kiro/agents`` spec, which that projection does not consult:
        a project spec granting the loader while the projected user-level spec
        does not would otherwise turn deferral on for a session with no loader.
        An unreadable spec grants nothing (fail closed: ``enabled: false``).
        """
        base = self._harness.client_capabilities
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

    def _projected_spawn_spec(self) -> dict[str, Any] | None:
        """The spawn agent's spec exactly as the wire projection will send it.

        Blocking (a file read); callers run it off the loop. The derived-agent
        branch reads NOTHING: the freshness gate that ran in ``_resolve_spawn_plan``
        already verified those bytes, and a second read here would be a second
        observation of a file a revocation could land in between. Every other
        agent is read the way ``KasHarness.session_extras`` reads it, from the
        user-level agents directory, so the two cannot disagree about which spec
        a session runs.
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
        try:
            return load_agent_spec(kiro_agents_dir(), self._agent)
        except Exception:
            logger.warning(
                "agent %r: spec unreadable at spawn; MCP Tool Search stays off for this process",
                self._agent or "<none>",
                exc_info=True,
            )
            return None

    def _refuse_if_loader_unreachable(self, active_agent: str, kas_agents: Any) -> None:
        """Refuse a session whose projected spec cannot load what this process defers.

        The Tool Search setting is process-wide on a wire-settings host: it was
        decided at spawn from the spawn agent's spec as it stood then. What a
        session RUNS is the projection built now -- a different agent, or the same
        agent whose user-level spec has since lost the grant -- and if that grants
        no loader its MCP specs would be deferred with no way back, the exact shape
        the handshake gate prevents. So the projected payload is judged every
        time, never the agent's name. There is no per-session knob to send, so the
        session is refused as a binding error, which the run-runtime caller already
        answers by giving the session a runtime of its own (whose handshake then
        decides afresh); a foreground caller surfaces it as the session error.
        """
        # ``getattr``: a bare runtime built with ``object.__new__`` for the
        # projection alone (the same shape ``_kas_custom_agents`` tolerates for
        # ``_work_dir``) has never handshaken and so has nothing to enforce.
        wire = getattr(self, "_tool_search_wire", {}).get("toolSearch")
        if not (isinstance(wire, dict) and wire.get("enabled")) or not kas_agents:
            return
        if any(spec_grants_tool_search(a) for a in kas_agents if isinstance(a, dict)):
            return
        raise AcpToolSurfaceBindingError(
            f"agent {active_agent!r} grants no tool_search loader, but this process "
            f"(spawned for {self._agent!r}) runs with MCP Tool Search deferral on; "
            "its MCP tools would be unreachable -- create a runtime for the agent"
        )

    async def _kas_custom_agents(
        self,
        agent: str,
        *,
        member_dispatch: bool = False,
        crew_panel: bool = False,
        session_key: str = "",
    ) -> SessionExtras:
        """The per-session payload for a wire-registered host, and what built it.

        ``custom_agents`` is None when the host took its agent at spawn time.
        ``derived_spec_snapshot`` is the generation that payload was built from, and the
        activation check compares against IT rather than re-reading the file: a
        ``set_mode`` activates a definition that is already registered, so a fresh read
        there would judge a different artifact than the one the host holds.

        A thin read of the harness's per-session extras, kept under this name
        because both session-start paths and the tests around them ask for it
        here. The overlay is handed down rather than looked up by the harness:
        only this layer holds it, and the projection has to subtract the servers
        that will ALSO arrive as session-level broker stubs, because a
        session-injected server outranks an agent-declared one and declaring both
        is a double registration.
        """
        extras = await self._harness.session_extras(
            agent,
            work_dir=getattr(self, "_work_dir", None),
            mcp_gateway_overlay=self._mcp_gateway_overlay,
            member_dispatch=member_dispatch,
            crew_panel=crew_panel,
            session_key=session_key,
        )
        # Judged HERE, on the payload, so every path that builds one -- session/new
        # and session/load alike -- is covered, and a host that builds none (kiro:
        # ``custom_agents`` is None) never reaches the check.
        self._refuse_if_loader_unreachable(agent, extras.custom_agents)
        return extras

    async def _mount_member_panel(
        self,
        mcp_servers: list[dict[str, Any]],
        *,
        member_session_key: str,
        agent_name: str,
        session_work_dir: Any,
        stub_token: str,
        resuming: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Mount the crew-panel server into a member DM session's server array.

        Returns the array and whether the GRANT may follow it. The two answers are
        one call because they must agree: a grant that outlived the mount would
        leave a switched-off server both named in ``tools`` and pre-approved on the
        very session that is not mounting it, which is the shape
        ``member_dispatch`` already avoids by deriving its flag from its own
        withhold.

        Asked on the resume path as well as on create, and it matters MORE there:
        ``session/load`` re-initializes the session's servers, so an unasked
        question would re-mount a switched-off server onto a conversation whose
        ``session/new`` withheld it.

        Two withholds, each the operator's own, and BOTH spellings of the switch:

        * ``agent.crew_panel`` -- the config ceiling, read through
          :func:`~kiro_crew.members.crew_panel_enabled`, which fails closed on an
          unreadable or degraded config.
        * a whole-server ``disabled`` on ``kirocrew-panel``. ``disabled`` has no
          per-tool or per-call spelling, so a harness handed the server cannot
          refuse a call to it, and the ``tools`` allowlist that keeps a disabled
          server out of a projected array does not reach an entry appended here.
        * a per-tool ``disabledTools`` naming any panel verb. Asked HERE and not
          only on the client sibling, because the two paths serve different
          backends and this one is the only path KAS takes: ``disabledTools`` is a
          hand-editable documented key in the global ``settings/mcp.json`` that
          :func:`~kiro_crew.acp.session_mcp.session_mcp_disabled_tools` reads, and
          the KAS grant that follows this mount puts ``panel_publish`` into
          ``allowedTools`` approval-free. KAS has no wire slot for hooks, so there
          is no later point at which a call to the switched-off verb could be
          refused -- withholding is the only faithful answer, and an operator's
          per-tool switch-off would otherwise be silently undone.

        The per-tool withhold takes the WHOLE server on every runtime-served
        backend rather than only where withholding is the sole deny channel. The
        mount and the grant are one answer here by construction, so keeping the
        mount for a backend that can refuse per call (codex) while withholding the
        grant would need two, and a grant that outlived a withhold is the failure
        this coupling exists to prevent. Withholding a server is an availability
        cost; forwarding an un-narrowed one is a capability the user switched off.

        Asked PER SERVER rather than inherited from the dashboard server's answer:
        the panel and session control are separate capabilities with separate
        switches, so an operator who withdrew session control keeps the drawer
        they never asked to lose, and one who switched the panel off loses only
        the panel.
        """
        if not member_session_key:
            return mcp_servers, False
        # circular import: members' module graph is heavy; resolved at call time
        # like the dispatch seam on both paths.
        from kiro_crew.members import (
            MEMBER_PANEL_SERVER,
            crew_panel_enabled,
            member_panel_session_server,
        )

        where = " on resume" if resuming else ""
        if not await asyncio.to_thread(crew_panel_enabled):
            logger.info(
                "member session %s: agent.crew_panel is off, so the crew panel is "
                "not mounted%s; the member keeps its other tools",
                member_session_key,
                where,
            )
            return mcp_servers, False
        if await asyncio.to_thread(
            session_mcp_server_is_disabled,
            MEMBER_PANEL_SERVER,
            agent_name,
            work_dir=_disable_check_scope(self.acp_backend, session_work_dir),
        ):
            logger.warning(
                "member session %s: %s is switched off for this session (disabled), "
                "so the crew panel is not mounted%s; re-enable that server to restore it",
                member_session_key,
                MEMBER_PANEL_SERVER,
                where,
            )
            return mcp_servers, False
        narrowed = await asyncio.to_thread(
            session_mcp_disabled_tools,
            agent_name,
            work_dir=_disable_check_scope(self.acp_backend, session_work_dir),
        )
        if any(server == MEMBER_PANEL_SERVER for server, _tool in narrowed):
            logger.warning(
                "member session %s: one of %s's tools is switched off, and the grant "
                "that follows this mount is approval-free with no later point to "
                "refuse the call, so the crew panel is not mounted%s; stop narrowing "
                "that server to restore it",
                member_session_key,
                MEMBER_PANEL_SERVER,
                where,
            )
            return mcp_servers, False
        entry = await asyncio.to_thread(member_panel_session_server, member_session_key, stub_token)
        if entry is None:
            logger.warning(
                "member session %s: panel server unresolved%s -- the member runs "
                "without a panel this session",
                member_session_key,
                where,
            )
            return mcp_servers, False
        # Session-level entries outrank same-named spec entries, so drop any stub
        # for the same server rather than registering it twice.
        return [e for e in mcp_servers if e.get("name") != entry["name"]] + [entry], True

    async def _session_start_budget(self) -> float:
        """The session/new + session/load budget, resolved per session start.

        The config watcher's snapshot is a plain attribute read, so when it is
        armed every session start on this runtime reads the CURRENT
        ``agent.session_start_timeout_secs`` with no I/O -- a config write from
        any writer governs the next session/new on an already-running runtime.
        Before the watcher is armed (early boot, CLI, tests) the value is
        resolved once off-loop and cached: ``_resolve_session_start_timeout``
        calls ``KiroCrewConfig.load()``, which on a cache miss is a synchronous
        disk read + schema validation, and the request paths must not pay that
        per call. The same floor applies on both paths.
        """
        snap = live.snapshot()
        if snap is not None:
            try:
                return max(_SESSION_NEW_TIMEOUT, float(snap.agent.session_start_timeout_secs))
            except Exception:
                logger.debug(
                    "session-start timeout snapshot unreadable — using cache", exc_info=True
                )
        if self._session_start_timeout is None:
            self._session_start_timeout = await asyncio.to_thread(_resolve_session_start_timeout)
        if getattr(self, "_start_collect_timeout", None) is None:
            # Same off-loop resolve, so the timeout path (which cannot block on
            # disk) finds the collector's cleanup budget already cached.
            self._start_collect_timeout = await asyncio.to_thread(_resolve_start_collect_timeout)
        return self._session_start_timeout

    async def _mirrored_session_mcp(
        self,
        agent: str | None,
        *,
        work_dir: str | Path,
        session_key: str,
        channel_id: str,
    ) -> _MirroredSessionMcp | None:
        """This session's ``mcpServers``, built by the host's agent-config MIRROR.

        **Only called when the host HAS a mirror.** The decision is a synchronous
        in-memory registry read at the call site
        (:func:`~kiro_crew.providers.mirrors.registry.has_mirror`), not an await that
        returns ``None``, because H13 asks the shared construction path to stay a
        synchronous read: kiro and KAS reach their servers natively and must not gain
        an awaited step, a new call frame or a new failure mode in service of an
        adapter. ``load_session`` states the same requirement for its own resume path
        in as many words. The guard below is therefore a second line of defence rather
        than the gate, and it reads the registry rather than naming a backend, so a
        host joining ``providers.mirrors`` inherits the projection.

        A MIRRORED host cannot take the pooled array directly. The projection does two
        things the pooled resolution does not: it WITHHOLDS a broker stub for a server
        the agent's ``tools`` never references, and it returns the per-tool deny set
        the driver enforces at the approval request. Handing the raw array to the
        harness applies neither -- the harness narrows transports and nothing else --
        and a mirrored host approves its own tools internally, so an unprojected stub
        is a live tool surface Crew never granted.

        Both halves of the array go through ONE owner. The stubs are handed down as
        ``stub_elements`` rather than appended afterwards, because a stub carries the
        same ``name`` as the spec entry it rewrites: an append after the projection
        withheld that name un-withholds it, as the UNRESTRICTED server of the two.

        The stub token is minted BEFORE the projection, so it rides the stubs the
        projection keeps and no spec-translated element ever carries it -- the token
        names this session's brokered servers, and :meth:`_own_stub_session` would
        stamp every element of an array it was handed.

        ``session_key`` and ``channel_id`` reach the ELEMENT because they cannot reach
        the child any other way: a codex stdio server starts from ``env_clear()`` plus
        an allowlist and inherits nothing, so Crew's own control plane comes up with no
        session to act on unless the element carries the identity.

        ``permission_surface_owned`` is False because this runtime authors no native
        permission file. That is the fail-CLOSED direction: a mirror in claude's class
        -- one whose tools could be pre-approved in a file Crew does not own, where
        Crew's gate never fires -- withholds its array here rather than delivering it.

        Blocking work (an agent-spec parse, an overlay read) runs off the loop, the
        same as the pooled resolution it replaces.
        """
        # ONE registry read, not two. ``has_mirror`` is ``backend in MIRRORS`` and this
        # returns ``MIRRORS[backend]()`` when present, so a second read could only ever
        # agree -- and a branch for the disagreement would be dead code whose only
        # possible direction is wrong: falling through to the caller's pooled branch
        # mounts the unprojected broker stubs this whole method exists to withhold.
        # There is no degraded-but-running option worth having for that class, so the
        # state is made unrepresentable instead of handled.
        mirror = mirror_for(self.acp_backend)
        if mirror is None:
            return None
        active_agent = agent or self._agent
        try:
            stubbed = await asyncio.to_thread(
                injection_server_names,
                self._mcp_gateway_overlay,
                active_agent,
                # Same checkout the projection below resolves the agent SPEC
                # against, so the withheld set and the injected set are read from
                # one agent file rather than two.
                **overlay_project_scope(self.acp_backend, work_dir),
            )
        except Exception:
            # Same direction as the AcpClient path: an empty set re-declares a stubbed
            # server, where the session-level injection still outranks it, rather than
            # withholding a server nothing else supplies.
            logger.warning(
                "could not resolve pooled stub names; the session MCP array may re-declare one",
                exc_info=True,
            )
            stubbed = frozenset()
        stubs = await asyncio.to_thread(
            pooled_session_servers,
            self._mcp_gateway_overlay,
            active_agent,
            channel_id or None,
            **overlay_project_scope(self.acp_backend, work_dir),
        )
        stubs, stub_token = await self._own_stub_session(stubs, session_key)

        def _project_and_snapshot() -> tuple[Any, Any]:
            # One hop, two reads of the same file: the projection the array is
            # built from and the snapshot the unresolved-ref guard judges against.
            # Both go through session_mcp's own resolution order, so they resolve
            # the same spec FILE; the bytes can still differ if a save lands
            # between the two reads. That window is accepted for the snapshot,
            # because its only consumer is a diagnostic: a mismatch costs one
            # possibly-wrong warning, never the array, and threading the
            # projection's own parse through every mirror is a wider change than
            # that warning is worth. The projection may refuse (a stale derived
            # spec is the session's MCP surface, so refusing IS the answer); the
            # snapshot may not, and resolves to None instead.
            projection = mirror.session_projection(
                active_agent,
                stub_server_names=stubbed,
                stub_elements=stubs,
                permission_surface_owned=False,
                work_dir=work_dir,
                session_key=session_key,
                channel_id=channel_id,
                # THIS session's own name, minted just above. It reaches the projection
                # for the same reason ``session_key`` does -- the control-plane elements
                # are the only carriers a mirrored host has -- but it answers a question
                # the key cannot: on a shared runtime the key of the session that
                # CLAIMED the process is not the key of the subagent session running on
                # it, and after a warm-pool rekey the key baked into an element names the
                # previous owner. The token is per session and its mapping is
                # republished, so it stays right in both cases.
                session_token=stub_token,
            )
            return projection, _ref_spec_snapshot(active_agent, work_dir)

        projection, ref_spec = await asyncio.to_thread(_project_and_snapshot)
        servers = projection.params.get("mcpServers") or []
        return _MirroredSessionMcp(
            servers=list(servers) if isinstance(servers, list) else [],
            denied_tools=projection.denied_tools,
            stub_token=stub_token,
            derived_spec_snapshot=projection.derived_spec_snapshot,
            ref_spec=ref_spec,
        )

    def _mirrored_spec_check_needed(self, snapshot: Any) -> bool:
        """Whether this session has a derived spec to re-check. Synchronous.

        Split from :meth:`_require_unchanged_mirrored_spec` so the two session-start
        paths ask the question with an ordinary attribute test instead of awaiting a
        coroutine that would answer ``None`` for every host with no mirror -- the same
        H13 requirement that keeps the array decision a synchronous read.
        """
        return snapshot is not None

    async def _require_unchanged_mirrored_spec(self, session_id: str, snapshot: Any) -> None:
        """End the session when the spec it was built from changed under it.

        The other end of the bracket :meth:`_mirrored_session_mcp` opens. For a
        mirrored host the array IS the derived spec -- the child reads no spec of its
        own -- so a write landing between the array's build and the host consuming it
        would leave a session running restrictions nobody agreed to. Called once
        ``session/new`` / ``session/load`` has returned: a write landing after that
        point cannot change what the host already registered.

        Only called when :meth:`_mirrored_spec_check_needed` said there is one, so the
        kiro and KAS paths never reach it. The guard stays as a second line of defence.
        """
        if snapshot is None:
            return
        from kiro_crew.agent import DerivedSpecStale, require_unchanged_derived_spec

        try:
            await asyncio.to_thread(require_unchanged_derived_spec, snapshot)
        except DerivedSpecStale as exc:
            # session/new already succeeded, so the shared process holds this session;
            # a plain local unregister would leak it (the same reason the activation
            # bracket terminates rather than returns).
            await self.terminate_session(session_id)
            raise AcpRuntimeError(str(exc)) from exc

    async def _own_stub_session(
        self, entries: list[dict[str, Any]], session_key: str
    ) -> tuple[list[dict[str, Any]], str]:
        """Mint the token that names ONE session on this shared runtime.

        Returns *entries* carrying the token plus the token itself, which the
        caller records on the session handle so a later ``rekey()`` can name the
        same session — and stamps onto this session's control-plane elements.

        Every OTHER identity channel a Crew MCP child has is keyed on the process
        tree, and this runtime multiplexes N sessions over ONE kiro-cli process, so
        each of them resolves a ``spawn_run`` subagent's server to the PARENT slot
        and lets a parent re-claim overwrite it. The token is the per-SESSION name
        that tree cannot supply.

        Minted UNCONDITIONALLY. A reachable gatewayd socket is not a precondition,
        because gatewayd is not the token's only reader: the token -> session-key
        mapping is published to a MAC-signed file that the strict identity resolver
        reads with no daemon (:mod:`kiro_crew.session_token_sig`). The socket gates
        the CLAIM alone — with the gateway off, ``entries`` is empty and this still
        returns a live token for the control-plane elements to carry.

        Publication happens here, before ``session/new``, and off the event loop:
        a child launched while that request is served may resolve its identity
        before the first turn's republication, so the mapping has to exist by the
        time the element does.

        When the owning session key is already known, the claim is pushed HERE,
        before ``session/new``, and awaited: kiro-cli launches this session's
        stubs while serving that request, so a claim sent afterwards would race
        the register it exists to inform. Best-effort — ``send_claim`` swallows a
        missing/wedged gatewayd under its own timeout and returns False, and a
        session whose key is not known yet (a warm-pool worker, claimed later)
        is named by the ``rekey()`` claim instead.
        """
        token = mint_stub_session_token()
        entries = attach_stub_session_token(entries, token)
        if session_key:
            await asyncio.to_thread(publish_session_token, token, session_key)
        if entries and self._mcp_gateway_socket and session_key and self.pid:
            await send_claim(
                self._mcp_gateway_socket,
                self.pid,
                session_key,
                None,
                token,
            )
        return entries, token

    async def _unpooled_control_planes(
        self, entries: list[dict[str, Any]], agent: str | None, work_dir: str | Path
    ) -> list[dict[str, Any]]:
        # A shared Kiro process has no session-valued environment. Its native
        # managed servers need per-element identity even with the broker off.
        if self.acp_backend == ACP_BACKEND_KIRO:
            from kiro_crew.acp.session_mcp import kiro_control_plane_servers

            projection = getattr(self, "_native_skill_projection", None)
            projection_kwargs: dict[str, Any] = (
                {"spec_override": projection.specs.get(agent or self._agent)}
                if projection is not None
                else {}
            )
            native = await asyncio.to_thread(
                kiro_control_plane_servers,
                agent,
                work_dir=work_dir,
                existing_names={str(entry.get("name")) for entry in entries},
                **projection_kwargs,
            )
            if (
                projection is not None
                and (agent or self._agent) in projection.search_agents
                and not any(entry.get("name") == "kirocrew-core" for entry in [*entries, *native])
            ):
                raise AcpRuntimeError(
                    "Cannot bind skill_search to this session without losing native MCP restrictions. "
                    "Check the agent's kirocrew-core server configuration."
                )
            return [*entries, *native]
        return entries

    async def create_session(
        self,
        cwd: str | Path | None = None,
        agent: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        crew_agent: str | None = None,
        member_session_key: str = "",
        session_key: str = "",
        channel_id: str = "",
        memory_mode: str = "persistent",
        on_gate_acquired: Callable[[float], None] | None = None,
        late_adopter: "Callable[[AcpSessionHandle], Awaitable[bool]] | None" = None,
        on_gate_queued: Callable[[], None] | None = None,
    ) -> AcpSessionHandle:
        """Create a new ACP session on this runtime. Returns a session handle.

        ``crew_agent`` is the canonical Kiro Crew identity for THIS session;
        None falls back to the runtime's own (spawn-time or rekeyed) identity.

        ``session_key`` is the Kiro Crew session that will OWN this ACP session.
        It is what makes the session's broker stubs resolvable as this session
        rather than as the runtime — see :meth:`_own_stub_session`. Empty when
        the owner is not known yet (a pooled worker claimed later), and the
        ``rekey()`` claim then carries the token.

        ``member_session_key`` marks a crew member's DM session and carries its
        session key: the dashboard session-control server is mounted as a
        session-level entry (identity via ``KIROCREW_SESSION_KEY``), and the
        KAS wire agent's projection widens to grant its tools. Empty — every
        non-member session — leaves both paths byte-identical to before.

        ``channel_id`` is the channel this session belongs to, and it reaches a
        MIRRORED host's ``mcpServers`` ELEMENTS rather than the process env: a
        codex stdio server starts from ``env_clear()`` plus an allowlist, so a
        server that reports its caller's channel can only learn it from the
        element. Empty — and unread — for a host with no mirror.

        ``session/new`` runs under the loop's :class:`SessionStartGate`
        (``agent.session_start_concurrency``). ``on_gate_queued()`` fires
        immediately before the wait for a permit begins and
        ``on_gate_acquired(queue_wait_ms)`` at gate EXIT, so the caller can
        stop its own clocks for exactly the span spent queued and restart them
        at acquisition: the queue wait is not start time. On a ``session/new`` timeout the request
        is NOT abandoned: a :class:`StartCollector` keeps it for
        ``agent.start_collect_timeout_secs`` and either hands the late session
        to ``late_adopter`` (which returns True to keep it) or tears it down;
        the raised :class:`AcpSessionStartTimeout` carries that collector. The
        gate permit is released exactly once on every path.
        """
        if memory_mode not in {"persistent", "incognito", "temporary"}:
            raise ValueError("Invalid session memory mode")
        if memory_mode != "persistent":
            # A mixed runtime cannot attribute every raw diagnostic frame to a
            # session. Latch recording off before session/new can emit a payload.
            self.recording_allowed = False
            self._stderr_lines.clear()
        if not self._initialized:
            raise AcpRuntimeError("Runtime not initialized — call spawn() first")

        # Inject the shared gateway's broker stubs unless the caller supplied an
        # explicit list. A session-injected server outranks the same-named entry
        # in the agent spec, so this is what actually pools the servers — no file
        # is written anywhere. Empty when the gateway is disabled.
        # Resolved here rather than beside the params below, because a mirrored
        # host's projection needs it: an agent spec can live in the project, and
        # resolving a narrower spec set silently drops the ``tools`` allowlist that
        # spec declared. One call either way -- no path resolves it twice.
        session_work_dir = await self._session_work_dir(cwd)
        denied_tools: frozenset[tuple[str, str]] = frozenset()
        mirrored_snapshot: Any = None
        ref_spec: Any = None
        if mcp_servers is None:
            # A mirrored host takes its whole array from the mirror; every other host
            # takes the pooled stubs it always took. Which one is a synchronous
            # in-memory registry read, so the kiro path reaches a comparison and stops
            # rather than awaiting an adapter seam (H13). Both branches resolve their
            # own I/O off the event loop: the lookup stats/reads files, and blocking
            # the loop stalls every other session's I/O.
            mirrored = None
            if has_mirror(self.acp_backend):
                mirrored = await self._mirrored_session_mcp(
                    agent,
                    work_dir=session_work_dir,
                    session_key=session_key,
                    channel_id=channel_id,
                )
            if mirrored is not None:
                mcp_servers = mirrored.servers
                stub_token = mirrored.stub_token
                denied_tools = mirrored.denied_tools
                mirrored_snapshot = mirrored.derived_spec_snapshot
                ref_spec = mirrored.ref_spec
            else:
                pooled, ref_spec = await asyncio.to_thread(
                    _pooled_session_servers_and_ref_spec,
                    self._mcp_gateway_overlay,
                    agent or self._agent,
                    self.acp_backend,
                    session_work_dir,
                )
                mcp_servers = await self._unpooled_control_planes(
                    pooled, agent or self._agent, session_work_dir
                )
                mcp_servers, stub_token = await self._own_stub_session(mcp_servers, session_key)
        else:
            # An explicit array is the caller's own composition (a mirror's
            # projection, a test double); it is not this method's to re-key, and it
            # carries no spec snapshot, so the unresolved-ref guard has nothing to
            # judge against and stays silent -- as the client does with no warmed
            # snapshot.
            stub_token = ""
        member_withheld = False
        # False for every non-member session, set without an awaited call so the
        # Kiro construction path is untouched by this capability (H13).
        panel_mounted = False
        if member_session_key:
            # circular import: members' module graph is heavy; resolved at call
            # time like the projection seams below.
            from kiro_crew.members import MEMBER_DISPATCH_SERVER, member_dispatch_session_server

            # The operator's switch-off of the dashboard server, asked on THIS path
            # too. ``disabled`` has no per-tool or per-call spelling, so a harness
            # handed the server cannot refuse a call to it, and the ``tools``
            # allowlist that keeps a disabled server out of a projected array does
            # not reach an entry appended here. Only a member session reaches this
            # branch, so no other host gains a suspension point (H13).
            member_withheld = await asyncio.to_thread(
                session_mcp_server_is_disabled,
                MEMBER_DISPATCH_SERVER,
                agent or self._agent,
                work_dir=_disable_check_scope(self.acp_backend, session_work_dir),
            )
            member_entry = (
                None
                if member_withheld
                else await asyncio.to_thread(
                    member_dispatch_session_server, member_session_key, stub_token
                )
            )
            if member_entry is not None:
                # Session-level entries outrank same-named spec entries, so drop
                # any stub for the same server rather than registering it twice.
                mcp_servers = [e for e in mcp_servers if e.get("name") != member_entry["name"]] + [
                    member_entry
                ]
            elif member_withheld:
                logger.warning(
                    "member session %s: %s is switched off for this session "
                    "(disabled), so session control is not mounted — the DM thread "
                    "runs as plain chat; re-enable that server to restore it",
                    member_session_key,
                    MEMBER_DISPATCH_SERVER,
                )
            else:
                logger.warning(
                    "member session %s: dashboard server unresolved — the DM "
                    "thread runs as plain chat this session",
                    member_session_key,
                )
            # INSIDE the member branch, like the mount above it: a session with no
            # member key reaches no part of this composition, so the Kiro
            # construction path gains no conditional, no awaited step and no new
            # failure mode from the panel capability (harness-parity H13).
            mcp_servers, panel_mounted = await self._mount_member_panel(
                mcp_servers,
                member_session_key=member_session_key,
                agent_name=agent or self._agent,
                session_work_dir=session_work_dir,
                stub_token=stub_token,
            )
        # The agent to run: an explicit request, else the runtime default. KAS
        # has no --agent spawn flag, so its default must be BOTH injected (below)
        # and activated (via set_mode after session/new); the kiro default is
        # already active from the --agent spawn.
        active_agent = agent or self._agent
        # Adapter-only seam: _kas_custom_agents returns None on the kiro backend,
        # so the kiro construction path gains no conditional, no new required
        # argument, and no new failure mode (harness-parity H13).
        kas_extras = await self._kas_custom_agents(
            active_agent,
            # The GRANT follows the same answer the mount does. This widening adds
            # ``@kirocrew-dashboard`` to the KAS agent's ``tools`` and merges the member
            # verbs into ``allowedTools``, which is an approval-free path: a grant that
            # outlived the withhold would leave the switched-off server both named and
            # pre-approved on the very session that is not mounting it.
            member_dispatch=bool(member_session_key) and not member_withheld,
            # Same rule, its own withhold: see _mount_member_panel, which answers
            # the mount and the grant together so the two cannot disagree.
            crew_panel=panel_mounted,
            session_key=session_key,
        )
        kas_agents = kas_extras.custom_agents
        # The generation the wire payload was built from, or None when this host takes its
        # agent at spawn time. Consumed by the activation bracket below.
        payload_snapshot = kas_extras.derived_spec_snapshot
        # Crew's managed servers travel in the session-level array, the one
        # declaration site the captured 2.18.0 release honours over a same-named
        # global/workspace entry and 2.20.0 reports as the client's own (see
        # ``hoist_managed_servers``). The kiro path returns None here and is
        # untouched.
        kas_agents, mcp_servers = hoist_managed_servers(kas_agents, active_agent, mcp_servers)
        # The host's last word on its own tool surface. A host that reads an
        # agent spec passes the list straight back; one that has nothing else
        # describing its tools narrows it to the transports it advertised at
        # handshake, because a single unsupported element can cost the whole
        # session/new rather than that one server.
        # Bound ONCE and threaded to every consumer that means "what this session
        # was sent": the request, the stall diagnostic, the session report and the
        # unresolved-ref guard. The pre-filter roster is not that; a host that
        # narrows its array would otherwise be reported as having been sent servers
        # the wire never carried, and the guard would judge refs against them.
        wire_servers = self._harness.session_mcp_servers(
            mcp_servers, agent_capabilities=self._agent_capabilities
        )
        params = build_session_new_params(
            session_work_dir,
            mcp_servers=wire_servers,
            kas_custom_agents=kas_agents,
        )

        projected_sources: dict[str, str] = {}
        if self._member_context:
            from kiro_crew.member_essential_context import projected_resource_documents

            for definition in kas_agents or ():
                if definition.get("id") == active_agent:
                    projected_sources.update(
                        await asyncio.to_thread(
                            projected_resource_documents, definition, str(session_work_dir)
                        )
                    )

        budget = await self._session_start_budget()
        # The init scope opens BEFORE the admission gate, not after: the gate's
        # queue is unbounded in practice, and ``has_active_or_initializing_
        # sessions`` is the predicate every recycle and displacement decision
        # asks -- a runtime whose claim is still queued behind the gate must
        # already read as busy, or a concurrent spawn-identity displacement
        # pass sees it idle and kills it under the claim. A gate failure or a
        # cancellation landing in the wait closes the scope on the way out.
        self._session_inits_in_flight += 1
        try:
            # Gate BEFORE the request goes out, released exactly once: on success
            # right after the answer (the rest of session setup is not what the
            # gate protects), on a timeout by the collector that now owns the
            # request, on any other failure here.
            gate = await session_start_gate()
            if on_gate_queued is not None:
                try:
                    on_gate_queued()
                except Exception:
                    logger.debug("on_gate_queued callback raised", exc_info=True)
            permit = await gate.acquire()
            if on_gate_acquired is not None:
                try:
                    on_gate_acquired(permit.queue_wait_ms)
                except Exception:
                    logger.debug("on_gate_acquired callback raised", exc_info=True)
        except BaseException:
            self._finish_session_init("")
            raise
        session_id = ""
        # Start latency is measured from gate EXIT: the queue wait is admission's
        # cost, not the runtime's, and the adaptive controller reads these
        # samples for its ``start_latency`` / ``timeouts`` signals.
        start_t0 = time.monotonic()
        try:
            resp = await self._send_and_await(METHOD_SESSION_NEW, params, timeout=budget)
            session_id = str(resp.get("sessionId") or "")
            permit.release()
            if not session_id:
                # One failed start, one sample: the ``except`` below records it.
                raise AcpRuntimeError(f"session/new did not return sessionId: {resp}")
            _record_session_start(start_t0, ok=True)
        except AcpRequestTimeout as exc:
            # A start that outlived its budget is the congestion signal the
            # controller keys its decrease on (attributable timeout).
            _record_session_start(start_t0, ok=False, attributable_timeout=True)
            # Read the staged MCP reports before the finally below clears them.
            stalled = self._session_start_stalled(exc, METHOD_SESSION_NEW, wire_servers)
            collector = self._collect_late_start(
                exc,
                permit,
                agent=agent,
                crew_agent=crew_agent,
                kas_agents=kas_agents,
                mcp_servers=wire_servers,
                budget=budget,
                stub_token=stub_token,
                denied_tools=denied_tools,
                mirrored_snapshot=mirrored_snapshot,
                ref_spec=ref_spec,
                active_agent=active_agent,
                session_work_dir=session_work_dir,
                projected_sources=projected_sources,
                payload_snapshot=payload_snapshot,
                late_adopter=late_adopter,
                memory_mode=memory_mode,
                session_key=session_key,
            )
            if collector is None:
                permit.release()
            raise AcpSessionStartTimeout(str(stalled), collector=collector) from exc
        except BaseException:
            permit.release()
            _record_session_start(start_t0, ok=False)
            raise
        finally:
            buffered_init = self._finish_session_init(session_id)

        return await self._finish_create_session(
            session_id,
            resp,
            buffered_init=buffered_init,
            memory_mode=memory_mode,
            agent=agent,
            crew_agent=crew_agent,
            kas_agents=kas_agents,
            mcp_servers=wire_servers,
            budget=budget,
            stub_token=stub_token,
            denied_tools=denied_tools,
            mirrored_snapshot=mirrored_snapshot,
            ref_spec=ref_spec,
            active_agent=active_agent,
            session_work_dir=session_work_dir,
            projected_sources=projected_sources,
            payload_snapshot=payload_snapshot,
            session_key=session_key,
        )

    def _collect_late_start(
        self,
        exc: AcpRequestTimeout,
        permit: StartPermit,
        *,
        agent: str | None,
        crew_agent: str | None,
        kas_agents: Any,
        mcp_servers: list[dict[str, Any]],
        budget: float,
        stub_token: str,
        denied_tools: frozenset[tuple[str, str]],
        mirrored_snapshot: Any,
        ref_spec: Any,
        active_agent: str,
        session_work_dir: str | Path,
        projected_sources: dict[str, str],
        payload_snapshot: Any,
        late_adopter: "Callable[[AcpSessionHandle], Awaitable[bool]] | None",
        memory_mode: str = "persistent",
        session_key: str = "",
    ) -> StartCollector | None:
        """Hand a timed-out ``session/new`` to a :class:`StartCollector`.

        None when the request never went out (a patched or pre-wire failure
        leaves no ``req_id`` on the exception): there is nothing to own, and
        the caller releases the permit itself. The collector's cleanup budget
        is read from the live snapshot when armed, else the cached value from
        the last off-loop resolve (default 300s) -- a collector is created on
        the timeout path and must not block on disk there.
        """
        req_id = getattr(exc, "req_id", None)
        future = getattr(exc, "adopted_future", None)
        if req_id is None or future is None:
            return None
        # Hand the permit over only while the gate still has a reserved slot for
        # a start that has not gone out. Denied, the permit is released here and
        # the collector gets none -- it keeps owning the request either way, so
        # the late session is still adopted or torn down; what it stops doing is
        # holding a permit for up to ``start_collect_timeout_secs`` that a fresh
        # session/new is queued behind (see _COLLECTOR_PERMIT_HEADROOM).
        collector_permit: StartPermit | None = permit
        if not permit.hold_for_collector():
            permit.release()
            collector_permit = None
            logger.warning(
                "acp_startup_stage stage=session_new outcome=gate_permit_returned "
                "req_id=%d collector_holds=%d ceiling=%d -- the collecting "
                "population already holds the gate's collector budget, so this "
                "permit is released instead of parked",
                int(req_id),
                permit._gate.collector_holds,
                permit._gate.collector_hold_ceiling,
            )
        timeout = getattr(self, "_start_collect_timeout", None)
        if timeout is None:
            snap = live.snapshot()
            try:
                timeout = (
                    max(10.0, float(snap.agent.start_collect_timeout_secs))
                    if snap is not None
                    else _START_COLLECT_TIMEOUT_DEFAULT
                )
            except Exception:
                timeout = _START_COLLECT_TIMEOUT_DEFAULT
        collector = StartCollector(
            self,
            int(req_id),
            future,
            permit=collector_permit,
            timeout=timeout,
            context={"agent": agent or "", "crew_agent": crew_agent or ""},
            memory_mode=memory_mode,
        )
        # Seeded and registered with no await in between, so the reader loop
        # cannot stage a frame into only one of the two holders: what this start
        # already staged is copied here, and everything from now on is handed to
        # the collector as well as to any init still in flight. The caller's
        # ``finally`` closes the in-flight scope a moment from now and clears that
        # deque; the collector's copy is what survives to be claimed.
        collector.seed_init_frames(self._pending_init_notifications)
        if late_adopter is not None:

            async def _adopt(session_id: str, resp: dict[str, Any]) -> bool:
                handle = await self._finish_create_session(
                    session_id,
                    resp,
                    buffered_init=collector.take_init_frames(session_id),
                    memory_mode=memory_mode,
                    agent=agent,
                    crew_agent=crew_agent,
                    kas_agents=kas_agents,
                    mcp_servers=mcp_servers,
                    budget=budget,
                    stub_token=stub_token,
                    denied_tools=denied_tools,
                    mirrored_snapshot=mirrored_snapshot,
                    ref_spec=ref_spec,
                    active_agent=active_agent,
                    session_work_dir=session_work_dir,
                    projected_sources=projected_sources,
                    payload_snapshot=payload_snapshot,
                    session_key=session_key,
                )
                # A declining (or raising) adopter answers False and the
                # collector performs the one teardown.
                return bool(await late_adopter(handle))

            collector.adopt(_adopt)
        self._start_collectors[int(req_id)] = collector
        logger.warning(
            "acp_startup_stage stage=session_new outcome=collecting req_id=%d "
            "collect_budget_s=%g gate_active=%d gate_queued=%d",
            int(req_id),
            timeout,
            *session_start_gate_counts(),
        )
        return collector.start()

    async def _teardown_late_session(self, session_id: str) -> None:
        """Tear down a session that arrived after its caller gave up.

        Per-session teardown only (the harness's cancel/terminate verb plus the
        local unregister); the shared runtime and its other sessions are never
        touched. A dead runtime has nothing to tear down.
        """
        if self._dead or self._process is None:
            return
        await self.terminate_session(session_id)

    def start_collectors(self) -> list[StartCollector]:
        """Live collectors, for diagnostics and tests."""
        return list(self._start_collectors.values())

    def _guard_unresolved_mcp_refs(
        self,
        handle: AcpSessionHandle,
        spec: Any,
        agent: str | None,
        wire_servers: Any,
    ) -> None:
        """Warn when the spec's ``@server`` refs name nothing this session gets.

        The runtime-path twin of ``AcpClient._guard_unresolved_mcp_refs``, and it
        exists because a host served here rather than by the client would otherwise
        be the one host whose unresolved refs are never reported -- which for a host
        that reads no agent file of Crew's is the normal case the guard was written
        for, not a corner.

        *wire_servers* is the FINAL array -- harness-filtered projection plus broker
        stubs -- so this is the last point at which "which servers does this session
        actually get" can be known. Judging the pre-filter roster would report a ref
        as satisfied by a server the wire never carried.

        Synchronous, in-memory and non-raising, in that order of importance (H13).
        *spec* was read in the same off-loop hop that resolved the array, so this
        adds no scheduling point to any host's session start; ``None`` -- no hop
        (a caller-supplied array) or an unreadable spec -- means nothing to say.
        Every failure resolves to silence rather than a failed session, because a
        diagnostic that can fail a session is a worse defect than the one it
        detects. It changes nothing: not the array, not the session's fate.
        """
        if spec is None:
            return
        try:
            unresolved = warn_unresolved_server_refs(
                spec,
                wire_servers,
                backend=self.acp_backend,
                agent=agent or "",
                gateway_enabled=self._mcp_gateway_overlay is not None,
            )
            if unresolved:
                handle.mcp_session_report().record_unresolved_refs(unresolved)
        except Exception:
            logger.debug("unresolved-ref guard: evaluation failed", exc_info=True)

    async def _finish_create_session(
        self,
        session_id: str,
        resp: dict[str, Any],
        *,
        buffered_init: list[JsonRpcMessage],
        agent: str | None,
        crew_agent: str | None,
        kas_agents: Any,
        mcp_servers: list[dict[str, Any]],
        budget: float,
        stub_token: str,
        denied_tools: frozenset[tuple[str, str]],
        mirrored_snapshot: Any,
        ref_spec: Any,
        active_agent: str,
        session_work_dir: str | Path,
        projected_sources: dict[str, str],
        payload_snapshot: Any,
        memory_mode: str = "persistent",
        session_key: str = "",
    ) -> AcpSessionHandle:
        """Everything after a successful ``session/new``: queue, handle, mode, drain.

        Shared by the direct path and a late adoption through
        :class:`StartCollector`. ``buffered_init`` comes from whichever holder
        staged the frames while this id was unknown: the runtime's in-flight
        scope on the direct path, the collector on an adoption.
        """
        # Register session queue
        queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
        self._session_queues[session_id] = queue
        for msg in buffered_init:
            queue.put_nowait(msg)

        # Resolve the watchdog snapshot OFF the loop before constructing the
        # handle: the load is config file reads + jsonschema validation on a
        # config change, and the handle constructor is synchronous. The crew
        # identity is canonical (a cfg.agents key) — the kiro ``agent`` name
        # is a different namespace and is not stored on the handle.
        _crew = crew_agent if crew_agent is not None else self._crew_agent
        _wd = await asyncio.to_thread(_load_watchdog_settings, _crew)
        handle = AcpSessionHandle(
            session_id=session_id,
            queue=queue,
            runtime=self,
            watchdog=_wd,
            crew_agent=_crew,
            session_key=session_key,
        )
        handle.memory_mode = memory_mode
        # The token this session's stubs carry, so a later claim (warm-pool
        # rekey) can name THIS session instead of every session on the runtime.
        handle.stub_session_token = stub_token
        # The projection's client obligation, on the driver that answers this
        # session's permission requests. Empty for a host with no mirror and for a
        # caller-supplied array, and the handle's check is a no-op on empty.
        handle.spec_denied_tools = denied_tools
        if self._mirrored_spec_check_needed(mirrored_snapshot):
            await self._require_unchanged_mirrored_spec(session_id, mirrored_snapshot)

        # Populate state from session/new response (configOptions, available models)
        handle.store_session_config(resp)
        # Both halves of that snapshot are now known, which is what makes the
        # served-default check answerable: the model the backend picked for
        # this session can be one the account's partition does not serve.
        await handle.ensure_served_default()
        # Make a SESSION_CONFIG host actually ask, before anything can prompt it.
        # Wired HERE and not earlier because the call reads the option list
        # ``store_session_config`` just parsed: it has to know whether the option
        # was advertised to tell "not advertised" (INDETERMINATE) from "the write
        # was rejected" (BYPASSED). Self-gating on the routing table, so kiro-cli
        # and the KAS relay send nothing extra — they route through their agent
        # spec. An enforced host that cannot be routed refuses, and the refusal
        # travels the same cleanup path as a failed set_mode below: session/new
        # already succeeded, so a plain local unregister would leak the session in
        # the shared process.
        try:
            await handle.apply_session_permission_routing()
        except Exception:
            await self.terminate_session(session_id)
            raise
        # The roster this session put on the wire. Set BEFORE drain_init so the
        # report can be read as "of the N we sent, these reported" rather than
        # as a bare list of names.
        handle.mcp_session_report().begin_session(mcp_servers)
        self._guard_unresolved_mcp_refs(handle, ref_spec, active_agent, mcp_servers)

        mode_switched = False
        staged_before_switch = 0
        # Set agent mode if specified. If set_mode raises, no handle is returned
        # to the caller, so terminate the session we just created above —
        # session/new already succeeded so the session exists in kiro-cli; a
        # plain local unregister would leak it in the shared process. terminate_
        # session also unregisters the queue. Mirrors the same cleanup in
        # load_session().
        #
        # Guard (A): only activate the mode when the backend advertised it in the
        # session/new `modes` list, or advertised no modes at all (older kiro-cli
        # / fake backend → attempt, backward-compatible). If modes ARE advertised
        # but the requested agent is absent, its ~/.kiro/agents/<agent>.json never
        # loaded (pre-spawn self-heal covers only the managed default). FAIL CLOSED
        # rather than silently leaving the session on kiro-cli's default mode: for
        # a restricted/app agent that would run a BROADER agent than requested (a
        # privilege escalation), so we terminate and raise an actionable error.
        #
        # Guard (A2) runs FIRST: the guard below never sees the agent `--agent`
        # selected, which on kiro-cli is every ordinary session's agent. See
        # _verify_spawn_agent_active.
        await self._verify_spawn_agent_active(session_id, resp, override=agent)
        # The agent to ACTIVATE. An explicit request always applies. When a KAS
        # custom agent was injected (``kas_agents`` non-empty) the runtime
        # default must be activated too: KAS has no --agent flag, so an injected
        # default that is not set here stays registered-but-inactive and the
        # session silently runs KAS's own default mode. On kiro ``kas_agents`` is
        # None and the --agent spawn already selected the default, so only an
        # explicit override reaches set_mode here.
        #
        # Asked of the routing table first (see _activates_agent_by_mode): a host
        # that governs its privileged tools some other way has no agent for
        # set_mode to resolve, so there is nothing to activate and nothing for
        # Guard (A) to fail closed on.
        mode_agent = (
            agent or (self._agent if kas_agents else None)
            if self._activates_agent_by_mode()
            else None
        )
        # Guard (C): the id may be advertised, yet as the HOST's own agent; a
        # set_mode would succeed and run that agent under the crewmate's name.
        # Asked of the harness as a seam (H13): the spawn-time hosts answer None
        # and the wire-registered one reads the stamp the engine put on the mode.
        refusal = self._harness.activation_refusal(mode_agent, resp) if mode_agent else None
        if refusal:
            await self.terminate_session(session_id)
            raise AcpRuntimeError(refusal)
        if mode_agent and self._mode_available(mode_agent, resp):
            # Measured BEFORE the request goes out, which is the only moment the
            # answer is unambiguous: everything queued right now initialized
            # under the pre-switch mode. Reading it after set_mode returns would
            # count the switched-to agent's own registrations -- which kiro-cli
            # can emit before it answers -- as pre-switch, and those frames are
            # then consumed without being recorded, leaving the panel at a false
            # "no report" for the rest of the session.
            staged_before_switch = handle.queued_frame_count()
            await self._activate_mode_bracketed(
                session_id,
                mode_agent,
                budget=budget,
                payload_snapshot=payload_snapshot,
                wire_registered=kas_agents is not None,
            )
            handle.active_agent = mode_agent
            # Whether set_mode actually SWITCHED modes: the servers that
            # initialized during session/new belong to the mode kiro-cli
            # started the session on. If the requested agent differs, those
            # staged registration frames describe the pre-switch roster and
            # must not arm the drain's idle shortcut while the switched-to
            # agent's own servers may still be booting.
            _ids, _current, _adv = parse_session_modes(resp)
            mode_switched = mode_agent != _current and (
                bool(_current) or self._harness.notification_aliases.mcp_readiness
            )
        elif mode_agent:
            _ids, _current, _adv = parse_session_modes(resp)
            await self.terminate_session(session_id)
            raise AcpRuntimeError(
                f"Agent mode {mode_agent!r} is not available on this session "
                f"(advertised modes: {_ids or 'none'}); its "
                f"~/.kiro/agents/{mode_agent}.json is likely missing. Refusing to run "
                f"the backend default mode {_current or '(unknown)'} in its place. "
                f"Run `kirocrew setup --agent-only` to materialize the agent config."
            )

        # Drain MCP-server-init / oauth / config notifications before the first
        # prompt so they don't race into the first turn (parity with
        # AcpClient._drain_notifications). Best-effort, bounded: exits shortly
        # after the servers report, or at the no-report ceiling if none do.
        # A runtime declared MCP-free skips the ceiling — nothing can arm it.
        # After a real mode SWITCH, reports staged during session/new describe
        # the pre-switch roster, so they must not arm the idle shortcut.
        if self._harness.notification_aliases.mcp_readiness:
            readiness_params: dict[str, Any] = {"mcpServers": mcp_servers}
            attach_kas_custom_agents(readiness_params, kas_agents)
            await self._wait_managed_mcp(
                handle,
                readiness_params,
                active_agent,
                budget,
                staged_before_switch if mode_switched else 0,
            )
        elif self._expect_mcp_reports:
            await handle.drain_init(
                stale_report_frames=staged_before_switch if mode_switched else 0
            )
        else:
            await handle.drain_init(no_report_ceiling=0.0)

        if active_agent == self._agent and str(session_work_dir) == str(self._work_dir):
            handle.native_context_documents.update(self._native_launch_sources)
        handle.native_context_documents.update(projected_sources)
        # Inline prompt bytes and file resources come from the same activated
        # wire definition. Conditional and indexed resources remain native.
        for definition in kas_agents or ():
            if definition.get("id") == active_agent and isinstance(definition.get("prompt"), str):
                handle.native_context_documents[f"template://{active_agent}#prompt"] = definition[
                    "prompt"
                ]

        # Each session start forks another agent process under the root, and
        # its MCP servers have just reported, so scan again: the spawn snapshot
        # predates all of them. Safe to repeat -- see _snapshot_descendants.
        #
        # Guarded like every other post-session/new step here: session/new has
        # already succeeded, so a cancellation inside the scan would leave the
        # session live in the shared process with no handle returned to anyone.
        # Only a cancellation can reach this arm; the scan swallows its own
        # failures.
        #
        # Startup is over by this point: session/new has succeeded, which is the
        # proof no sandbox refusal on the way here was fatal. So the startup latch
        # is spent, and spending it -- rather than merely ceasing to arm it -- is
        # what makes every consumer correct by construction: a translator reached
        # after this can only ever see False, so none of them re-derives the window.
        #
        # First session only. A later session/new on a warm runtime is ordinary
        # mid-life work, and a refusal its child prints then says nothing about
        # whether the agent process can start.
        self._first_session_ready = True
        self._saw_sandbox_init_failure = False
        try:
            await self._snapshot_descendants()
        except BaseException:
            await self.terminate_session(session_id)
            raise

        logger.info("Created session %s on runtime PID %d", session_id, self._pid or 0)
        return handle

    async def probe_advertised_models(
        self, *, force: bool = False, not_before: float = 0.0
    ) -> list[dict[str, str]]:
        """Fetch a fresh advertised-model (entitlement) snapshot from this backend.

        A session's ``availableModels`` is captured once, from its own
        ``session/new`` response, and the backend resolves that answer from the
        account state it holds at that instant — a lookup racing a token refresh
        or a cold start can answer with the default (free-tier) set. A long-lived
        session holding such an answer refuses models the account actually has,
        and nothing ever corrects it. This re-asks the question on the SAME live
        process with a throwaway minimal session (no MCP servers, no mode
        activation), terminated before returning.

        Single-flight + short TTL, on TWO clocks. A non-empty SUCCESS is replayed
        for :data:`_ENTITLEMENT_PROBE_TTL_SECS` (its result clock); an empty or
        FAILED attempt replays as ``[]`` (no evidence) for the same window (its
        attempt clock) without re-opening a session, so a burst of failures on
        the picker read path costs one round-trip. A failure never revives an
        expired success — the two clocks are independent.

        ``force=True`` skips ONLY the attempt-clock replay: a USER ACTION (an
        explicit ``set_model`` pick, the spawn-time pin withhold) must earn a
        fresh probe rather than be refused on a recent no-evidence failure: an
        explicit action always earns a real answer. It still honours the
        result-clock replay of a recent non-empty success (fresh evidence —
        nothing is gained by re-probing) and still serializes on the
        single-flight lock. The picker read path leaves ``force=False`` so it
        keeps the burst cap.

        ``not_before`` is a freshness floor on the result-clock replay: a caller
        passes the monotonic time of the snapshot it already holds, and a replay
        never answers with a result older than that snapshot. The shared result
        cache spans every session on this process, so without the floor a broad
        answer cached before an entitlement downgrade would overwrite a newer
        session's correctly narrower ``session/new`` snapshot. A cached result
        older than the floor is not evidence for that caller: the attempt-clock
        logic below decides between ``[]`` and a fresh probe, whose result is
        always newer than the floor.

        Returns the normalized advertised list, or ``[]`` when the probe fails
        or advertises nothing. An empty return is NOT evidence about
        entitlement — callers must keep whatever snapshot they already hold.
        """
        async with self._entitlement_probe_lock:
            now = time.monotonic()
            # Two-clock guard so a failure never extends the life of an old
            # success. If the last SUCCESSFUL result is still within TTL, replay
            # it (even under force: a fresh success is fresh evidence). Otherwise,
            # if the last ATTEMPT of any outcome (incl. a failure that re-stamped
            # only the attempt clock) is within TTL, return [] — no evidence, fail
            # open — WITHOUT opening a fresh session/new, UNLESS force=True, which
            # a user action passes to earn a fresh probe rather than be refused on
            # a recent no-evidence failure. Only past both windows (or forced past
            # the attempt window) do we probe again. Both clocks are 0.0 until the
            # first completed attempt, so neither branch fires before one. Both
            # replays also require their clock to be at least as new as the
            # caller's own snapshot (``not_before``): a cached answer never
            # replaces a newer one, and a failed attempt that predates the
            # caller's snapshot never stands in for the probe that snapshot has
            # yet to receive.
            if (
                self._entitlement_probe_result
                and self._entitlement_probe_result_at > 0.0
                and self._entitlement_probe_result_at >= not_before
                and now - self._entitlement_probe_result_at < _ENTITLEMENT_PROBE_TTL_SECS
            ):
                return list(self._entitlement_probe_result)
            if (
                not force
                and self._entitlement_probe_attempt_at > 0.0
                and self._entitlement_probe_attempt_at >= not_before
                and now - self._entitlement_probe_attempt_at < _ENTITLEMENT_PROBE_TTL_SECS
            ):
                return []
            if not self._initialized or self._dead or self._process is None:
                return []
            params = build_session_new_params(await self._session_work_dir(), mcp_servers=[])
            session_id = ""
            self._session_inits_in_flight += 1
            try:
                try:
                    resp = await self._send_and_await(
                        METHOD_SESSION_NEW, params, timeout=_ENTITLEMENT_PROBE_TIMEOUT
                    )
                    session_id = str(resp.get("sessionId") or "")
                finally:
                    # Close the init scope even on failure so staged init
                    # notifications from this probe never leak into a later
                    # real session's queue.
                    self._finish_session_init(session_id)
            except Exception:
                logger.debug("entitlement probe session/new failed", exc_info=True)
                # Stamp the ATTEMPT clock only (never the result clock), so a
                # burst of failing reads costs one session/new within the TTL
                # while the last SUCCESSFUL result keeps expiring on its own
                # clock — a failure can never revive a stale success.
                self._entitlement_probe_attempt_at = time.monotonic()
                return []
            # The answer's freshness is the moment it ARRIVED, stamped before the
            # teardown below. Stamping after the terminate round-trip would date
            # the answer later than a real session/new that completed during
            # that await, and the freshness floor would then let this older
            # answer replay over that session's newer snapshot.
            completed_at = time.monotonic()
            try:
                # Reads BOTH shapes, through the same fold the session-init capture
                # uses. Reading only ``models`` answers [] for a host whose list is a
                # ``configOptions`` select, and [] is contractually "no evidence" --
                # so the degraded snapshot this probe exists to correct would be the
                # one thing it could never correct.
                fresh = advertised_models_from_session(resp, self.acp_backend)
            finally:
                if session_id:
                    # Evict the probe session from the shared process; never
                    # raises (best-effort by contract).
                    await self.terminate_session(session_id)
            # A non-empty answer updates the stored result and its OWN clock, so
            # it is replayed until that clock expires. An empty answer leaves the
            # stored result (and its clock) untouched — no evidence, fail open.
            if fresh:
                self._entitlement_probe_result = list(fresh)
                self._entitlement_probe_result_at = completed_at
            # The attempt clock is stamped on EVERY completed probe, empty
            # included, so a burst of reads within the TTL costs one session/new
            # whether or not the backend advertised anything.
            self._entitlement_probe_attempt_at = completed_at
            return fresh

    async def load_session(
        self,
        session_file: str,
        resume_sid: str,
        cwd: str | Path | None = None,
        agent: str | None = None,
        crew_agent: str | None = None,
        member_session_key: str = "",
        session_key: str = "",
        channel_id: str = "",
    ) -> AcpSessionHandle:
        """Resume a prior session via session/load — mirrors AcpClient.

        Unlike create_session()+handle.load(), this issues session/load
        DIRECTLY (no session/new first), using the ORIGINAL sid as sessionId
        and passing cwd + the pooled broker stubs + the full transcript path,
        exactly as AcpClient._initialize_session does. This avoids the
        double-session footgun (fresh session/new context replayed on top of
        the loaded transcript) that produced stopReason='refusal'. Raises on
        failure so the caller can fall back to create_session().

        ``member_session_key`` mirrors create_session(): session/load
        re-initializes the session's MCP servers and re-registers the wire
        agent, so a member session resumed WITHOUT the same injection loses
        its dispatch tools mid-conversation — the mount must ride every path
        that (re)establishes the session's tool set, not just the first one.

        ``session_key`` mirrors create_session() for the same reason: load
        re-declares the broker stubs, so it re-launches them, and a resumed
        session whose stubs carried no token would fall back to resolving as the
        runtime — the parent slot — for the rest of its life.

        ``channel_id`` mirrors create_session() for the same reason the array
        does: session/load re-initializes this session's MCP servers, so a resume
        that dropped it would take the channel identity away from a conversation
        that already had it.
        """
        if not self._initialized:
            raise AcpRuntimeError("Runtime not initialized — call spawn() first")
        if not self._can_load_session:
            raise AcpRuntimeError("Backend does not advertise session/load support")

        # Re-declare the pooled broker stubs so a resumed session keeps talking
        # to the broker — same injection as create_session() and the AcpClient
        # resume path (client.py). session/load re-initializes the session's MCP
        # servers (see the budget note below), so an empty list here is APPLIED,
        # not ignored: the stubs stop shadowing the agent spec's same-named
        # entries and kiro-cli spawns its own copy of every pooled server,
        # silently un-pooling the session for the rest of its life. Resolved off
        # the event loop — the overlay lookup stats and reads files. Empty when
        # the shared gateway is disabled, so non-pooled installs still send [].
        active_agent = agent or self._agent
        # Hoisted above the array for the same reason create_session() hoists it: a
        # mirrored host's projection resolves the agent spec against this directory.
        session_work_dir = str(await self._session_work_dir(cwd))
        denied_tools: frozenset[tuple[str, str]] = frozenset()
        mirrored_snapshot: Any = None
        ref_spec: Any = None
        # A mirrored host re-declares the array its projection built, not the raw
        # pooled one: session/load re-initializes the session's servers, so an
        # unprojected array here does not merely fail to withhold a stub -- it MOUNTS
        # one on a conversation whose session/new withheld it.
        #
        # Gated on the registry read rather than entered unconditionally, for the
        # reason this method already gives for gating the KAS re-attach on the
        # backend: the kiro resume path must reach a comparison and STOP -- no awaited
        # step, nothing to unwind, no shared coroutine that could grow a failure mode
        # later. A membership read is the sanctioned form of that comparison.
        mirrored = None
        if has_mirror(self.acp_backend):
            mirrored = await self._mirrored_session_mcp(
                active_agent,
                work_dir=session_work_dir,
                session_key=session_key,
                channel_id=channel_id,
            )
        if mirrored is not None:
            mcp_servers = mirrored.servers
            stub_token = mirrored.stub_token
            denied_tools = mirrored.denied_tools
            mirrored_snapshot = mirrored.derived_spec_snapshot
            ref_spec = mirrored.ref_spec
        else:
            pooled, ref_spec = await asyncio.to_thread(
                _pooled_session_servers_and_ref_spec,
                self._mcp_gateway_overlay,
                active_agent,
                self.acp_backend,
                session_work_dir,
            )
            mcp_servers = await self._unpooled_control_planes(
                pooled, active_agent, session_work_dir
            )
            mcp_servers, stub_token = await self._own_stub_session(mcp_servers, session_key)
        member_withheld = False
        # False for every non-member session, set without an awaited call so the
        # Kiro construction path is untouched by this capability (H13).
        panel_mounted = False
        if member_session_key:
            # circular import: members' module graph is heavy; resolved at call
            # time, same as create_session().
            from kiro_crew.members import MEMBER_DISPATCH_SERVER, member_dispatch_session_server

            # Asked on the resume path for the reason it is asked on create, and it
            # matters MORE here: session/load re-initializes the session's servers, so
            # an unasked question would re-mount a switched-off server onto a
            # conversation whose session/new withheld it.
            member_withheld = await asyncio.to_thread(
                session_mcp_server_is_disabled,
                MEMBER_DISPATCH_SERVER,
                active_agent,
                work_dir=_disable_check_scope(self.acp_backend, session_work_dir),
            )
            member_entry = (
                None
                if member_withheld
                else await asyncio.to_thread(
                    member_dispatch_session_server, member_session_key, stub_token
                )
            )
            if member_entry is not None:
                mcp_servers = [e for e in mcp_servers if e.get("name") != member_entry["name"]] + [
                    member_entry
                ]
            elif member_withheld:
                logger.warning(
                    "member session %s: %s is switched off for this session "
                    "(disabled), so session control is not mounted on resume — the DM "
                    "thread runs as plain chat; re-enable that server to restore it",
                    member_session_key,
                    MEMBER_DISPATCH_SERVER,
                )
            else:
                logger.warning(
                    "member session %s: dashboard server unresolved on resume — "
                    "the DM thread runs as plain chat this session",
                    member_session_key,
                )
            # INSIDE the branch, for the reason create_session() states.
            mcp_servers, panel_mounted = await self._mount_member_panel(
                mcp_servers,
                member_session_key=member_session_key,
                agent_name=active_agent,
                session_work_dir=session_work_dir,
                stub_token=stub_token,
                resuming=True,
            )
        # Narrowed by the host for the same reason session/new is, and it matters
        # MORE here: session/load re-initializes the session's servers, so a
        # rejected array does not just fail to add tools -- it takes them away from
        # a conversation that already had them.
        # Bound once, for the same consumers as session/new: see the note there.
        wire_servers = self._harness.session_mcp_servers(
            mcp_servers, agent_capabilities=self._agent_capabilities
        )
        load_params: dict[str, Any] = {
            "sessionId": resume_sid,
            "cwd": session_work_dir,
            "mcpServers": wire_servers,
        }
        if session_file:
            # The CALLER decides, because the caller is what knows whether a
            # transcript exists: a host that locates the session from its id is
            # called with an empty path and the field never reaches it. Asking the
            # HOST instead would send the same requests today and cost something
            # real -- the _meta merge invariant below is pinned by a test that puts
            # a transcript path on KAS deliberately, and a per-host gate deletes
            # the only way to construct that collision.
            load_params["_meta"] = {"_kiro.dev/session_file": session_file}
        # Re-inject the agent definition, for the same reason create_session()
        # does: KAS registers client agents per session and has no --agent flag,
        # so a resumed session that is not handed them again advertises only the
        # modes it can find on disk. That set is NOT a superset of what
        # session/new had — KAS skips an agent profile written for kiro-cli — so
        # omitting this made the requested mode genuinely absent on resume, and
        # Guard A below then refused the load rather than run the backend default.
        #
        # Guarded on the backend rather than on the projection answering None, so
        # the kiro resume path reaches a comparison and STOPS: no awaited step,
        # nothing to unwind, no shared coroutine that could grow a failure mode
        # later. create_session() enters the same seam unconditionally, which is
        # the shape this one deliberately does not copy. Reading the backend and
        # stopping is the smallest non-zero delta the kiro path can take for KAS
        # behaviour to exist here at all, and the harness exposes no property
        # meaning "this host needs its agent re-sent on resume" that could answer
        # it instead — the projection's own None is the only such signal, and
        # consuming it is what would cost the kiro path the awaited step.
        # None on the kiro path, where the host took its agent at spawn time; on KAS it is
        # the generation the wire payload was built from, and the activation bracket below
        # compares against it rather than re-reading the file.
        kas_agents = None
        payload_snapshot = None
        if self._acp_backend == ACP_BACKEND_KAS:
            kas_extras = await self._kas_custom_agents(
                active_agent,
                # The grant follows the withhold here too -- see create_session().
                member_dispatch=bool(member_session_key) and not member_withheld,
                crew_panel=panel_mounted,
                session_key=session_key,
            )
            kas_agents = kas_extras.custom_agents
            payload_snapshot = kas_extras.derived_spec_snapshot
            # Same carriage as create_session: a resumed session re-initializes
            # its servers, and the managed ones must win the same-name contest
            # on load exactly as they did on new.
            kas_agents, mcp_servers = hoist_managed_servers(kas_agents, active_agent, mcp_servers)
            # REBIND, not just re-assign the param: the hoist changes the array, and
            # wire_servers is what the stall diagnostic and the session report read.
            # Setting only load_params would leave both describing the pre-hoist roster
            # -- naming servers this resume did not send.
            wire_servers = self._harness.session_mcp_servers(
                mcp_servers, agent_capabilities=self._agent_capabilities
            )
            load_params["mcpServers"] = wire_servers
            attach_kas_custom_agents(load_params, kas_agents)
        budget = await self._session_start_budget()
        self._session_inits_in_flight += 1
        loaded_session_id = ""
        try:
            # session/load is gated by the SAME MCP (re-)initialization as
            # session/new — kiro-cli re-initializes the session's servers on
            # load, and the runtime stages mcp/oauth_request frames while
            # EITHER request is in flight (the _session_inits_in_flight-keyed
            # staging in _reader_loop, closed by _finish_session_init; see
            # docs/system-specs/modules/acp-client.md "loading a session
            # triggers MCP re-initialization") — so it gets the same budget.
            resp = await self._send_and_await(METHOD_SESSION_LOAD, load_params, timeout=budget)

            # A genuine resume echoes "modes" in the response (same signal AcpClient
            # keys on). Anything else means load did not actually restore state.
            if "modes" not in resp:
                raise AcpRuntimeError(f"session/load did not resume session {resume_sid}: {resp}")
            loaded_session_id = resume_sid
        except AcpRequestTimeout as exc:
            # Read the staged MCP reports before the finally below clears them.
            raise self._session_start_stalled(exc, METHOD_SESSION_LOAD, wire_servers) from exc
        finally:
            buffered_init = self._finish_session_init(loaded_session_id)

        # Register the queue AFTER _send_and_await returns. During session/load
        # kiro-cli replays the full prior transcript on stdout; without a
        # registered queue those replay frames hit the "unknown session -> drop"
        # path in the reader loop and are silently discarded. Only frames
        # arriving AFTER this point (from future prompt() calls) reach the queue.
        # The load response itself routes via _pending_requests, not the session
        # queue, so this reorder is safe.
        queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
        self._session_queues[resume_sid] = queue
        for msg in buffered_init:
            queue.put_nowait(msg)

        # Mirrors create_session: a resumed session gets the same
        # canonical-crew watchdog snapshot, resolved off-loop.
        _crew = crew_agent if crew_agent is not None else self._crew_agent
        _wd = await asyncio.to_thread(_load_watchdog_settings, _crew)
        handle = AcpSessionHandle(
            session_id=resume_sid,
            queue=queue,
            runtime=self,
            watchdog=_wd,
            crew_agent=_crew,
            session_key=session_key,
        )
        # Mirrors create_session: the resumed session's own stub token.
        handle.stub_session_token = stub_token
        # Mirrors create_session: the resumed session re-declares the array, so it
        # re-derives the deny set that array came with and re-checks the generation.
        handle.spec_denied_tools = denied_tools
        if self._mirrored_spec_check_needed(mirrored_snapshot):
            await self._require_unchanged_mirrored_spec(resume_sid, mirrored_snapshot)
        handle.store_session_config(resp)
        # session/load echoes ``currentModelId`` exactly like session/new, and a
        # session persisted before the account's served list changed can come
        # back on a default the account does not serve — so the resumed session gets
        # the same served-default check as a fresh one.
        await handle.ensure_served_default()
        # Same as create_session, and for the same reason a resumed session gets
        # the served-default check: the option list came back on THIS response,
        # and a resumed session prompts the host exactly as a fresh one does, so
        # its permission boundary has to be armed here too. Refusal terminates
        # the resume_sid session rather than leaking it, matching the set_mode
        # cleanup below.
        try:
            await handle.apply_session_permission_routing()
        except Exception:
            await self.terminate_session(resume_sid)
            raise
        # session/load re-initializes this session's servers, so the resumed
        # session gets its own report against the roster load re-declared.
        handle.mcp_session_report().begin_session(wire_servers)
        self._guard_unresolved_mcp_refs(handle, ref_spec, active_agent, wire_servers)

        mode_switched = False
        staged_before_switch = 0
        # Activate the agent (mirrors AcpClient step 4 — set_mode applies to a
        # resumed session too, not just fresh ones). If set_mode raises, the
        # caller falls back to create_session() (a fresh sid + its own queue),
        # so terminate this resume_sid session first — session/load already
        # succeeded so kiro-cli holds it; a plain local unregister would leak it
        # in the shared process (and leave the reader routing late transcript-
        # replay frames to an abandoned queue). terminate_session unregisters too.
        #
        # Guard (A2), same as create_session: the check below reads `agent`, and
        # this method's only caller passes `agent=agent or None`, so a resume with
        # no override reaches no check at all. A fresh runtime resuming a session
        # re-reads the spec from disk, so the spawn agent can fail to load here
        # exactly as it can on a cold start.
        await self._verify_spawn_agent_active(resume_sid, resp, override=agent)
        # Same routing-table question as create_session: a host with no agent
        # spec has no mode to resume onto either.
        mode_agent = agent if self._activates_agent_by_mode() else None
        # Guard (C) -- see create_session: advertised, but as the host's own.
        refusal = self._harness.activation_refusal(mode_agent, resp) if mode_agent else None
        if refusal:
            await self.terminate_session(resume_sid)
            raise AcpRuntimeError(refusal)
        if mode_agent and self._mode_available(mode_agent, resp):
            # Measured BEFORE the request goes out, which is the only moment the
            # answer is unambiguous: everything queued right now initialized
            # under the pre-switch mode. Reading it after set_mode returns would
            # count the switched-to agent's own registrations -- which kiro-cli
            # can emit before it answers -- as pre-switch, and those frames are
            # then consumed without being recorded, leaving the panel at a false
            # "no report" for the rest of the session.
            staged_before_switch = handle.queued_frame_count()
            await self._activate_mode_bracketed(
                resume_sid,
                mode_agent,
                budget=budget,
                payload_snapshot=payload_snapshot,
                wire_registered=kas_agents is not None,
            )
            handle.active_agent = mode_agent
            # See create_session: after a real mode switch, registration frames
            # staged during session/load describe the pre-switch roster.
            _ids, _current, _adv = parse_session_modes(resp)
            mode_switched = mode_agent != _current and (
                bool(_current) or self._harness.notification_aliases.mcp_readiness
            )
        elif mode_agent:
            # Guard (A) — see create_session. A resumed session always echoes a
            # `modes` list (checked above), so an absent agent means its config
            # isn't loaded. Fail closed rather than silently resuming on a
            # different (broader) default agent than the one requested.
            _ids, _current, _adv = parse_session_modes(resp)
            await self.terminate_session(resume_sid)
            raise AcpRuntimeError(
                f"Agent mode {agent!r} is not available for resumed session "
                f"{resume_sid} (advertised modes: {_ids or 'none'}); its "
                f"~/.kiro/agents/{agent}.json is likely missing. Refusing to run "
                f"the backend default mode {_current or '(unknown)'} in its place. "
                f"Run `kirocrew setup --agent-only` to materialize the agent config."
            )

        # Drain MCP-init / oauth / config notifications before the first prompt
        # (parity with AcpClient). Transcript-replay frames were already dropped
        # before the queue was registered above, so only genuine init frames
        # remain to drain here. MCP-free runtimes skip the no-report ceiling.
        # After a real mode SWITCH, staged reports are pre-switch — don't arm.
        if self._harness.notification_aliases.mcp_readiness:
            await self._wait_managed_mcp(
                handle,
                load_params,
                active_agent,
                budget,
                staged_before_switch if mode_switched else 0,
            )
        elif self._expect_mcp_reports:
            await handle.drain_init(
                stale_report_frames=staged_before_switch if mode_switched else 0
            )
        else:
            await handle.drain_init(no_report_ceiling=0.0)

        # A resume re-initializes the MCP servers and forks the same agent
        # processes a fresh session does, so it needs the same scan; without it
        # every descendant a resumed session created stays unrecorded. Guarded
        # for the same reason as create_session: session/load already succeeded.
        try:
            await self._snapshot_descendants()
        except BaseException:
            await self.terminate_session(resume_sid)
            raise

        logger.info("Resumed session %s on runtime PID %d", resume_sid, self._pid or 0)
        return handle

    # ── Internal Helpers ──

    async def _wait_managed_mcp(
        self,
        handle: AcpSessionHandle,
        params: dict[str, Any],
        agent: str,
        timeout: float,
        stale_report_frames: int,
    ) -> None:
        required = required_managed_servers(params, agent)
        try:
            if required:
                await handle.wait_mcp_ready(
                    required,
                    timeout,
                    stale_report_frames=stale_report_frames,
                    tool_policy=active_custom_agent(params, agent),
                    # The names THIS request injected at session level, as sent:
                    # the one declaration site a provenance-less backend is
                    # known to honour over a same-named global server.
                    injected=frozenset(roster_names(params.get("mcpServers"))) & set(required),
                )
            else:
                # An external-only agent still needs the ordinary OAuth/config
                # drain, even though it has no managed readiness requirement.
                await handle.drain_init(
                    stale_report_frames=stale_report_frames,
                    no_report_ceiling=None if self._expect_mcp_reports else 0.0,
                )
        except BaseException:
            # No handle escapes on failure. A fresh session/new has no history
            # to preserve, so evict it from the host and reap its MCP children.
            # KAS has no evict-only verb: session/load is therefore unregistered
            # locally so its existing native history survives for a later resume.
            if "sessionId" in params:
                self.unregister_session(handle.session_id)
            else:
                await self.terminate_session(handle.session_id)
            raise

    async def _send_and_await(
        self, method: str, params: dict[str, Any], timeout: float = _REQUEST_TIMEOUT
    ) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response via _pending_requests.

        Used for control-plane requests (initialize, session/new, set_mode)
        where we need the response immediately rather than routing it to a
        session queue. ``timeout`` bounds the wait — teardown paths pass a
        tighter value than the default so an unresponsive runtime can't stall
        session eviction.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        req_id = self._next_id
        self._next_id += 1

        projection = getattr(self, "_native_skill_projection", None)
        if projection is not None:
            params = projection.request(method, params)
        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = future

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()

        stage = {
            "initialize": "initialize",
            METHOD_SESSION_NEW: "session_new",
            METHOD_SESSION_LOAD: "session_load",
            METHOD_SET_MODE: "set_mode",
        }.get(method)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            if method == METHOD_SESSION_NEW or method == "initialize":
                # The answer may still come. For ``session/new`` it names a
                # session the runtime has CREATED, so keep the future registered
                # for the reader loop and hand ownership to the caller's
                # StartCollector via ``adopt`` (RFC §4.4) instead of leaving an
                # unowned session in the shared process. For ``initialize`` the
                # caller (:meth:`_initialize_handshake`) may decide the deadline
                # was too short -- the slice started throttling after the
                # budget was chosen -- and keep waiting on the SAME request
                # rather than reap a process that is alive and answering
                # slowly. ``wait_for`` cancelled the future; register a fresh
                # one bound to the same id either way.
                fresh: asyncio.Future[dict[str, Any]] = loop.create_future()
                self._pending_requests[req_id] = fresh
                if method == METHOD_SESSION_NEW:
                    adopt = getattr(self._pending_requests, "adopt", None)
                    adopted = adopt(req_id) if adopt is not None else fresh
                else:
                    adopted = fresh
            else:
                self._pending_requests.pop(req_id, None)
                adopted = None
            active_starts, queued_starts = _cold_start_counts()
            if self._process is None:
                process_state = "absent"
            elif self._process.returncode is None:
                process_state = "running"
            else:
                process_state = "exited"
            logger.warning(
                "acp_startup_stage stage=%s outcome=timeout timeout_method=%s "
                "timeout_budget_s=%g duration_ms=%.1f active_starts=%d "
                "queued_starts=%d process_state=%s stderr_lines=%d",
                stage or "control",
                method,
                timeout,
                (time.monotonic() - started) * 1000.0,
                active_starts,
                queued_starts,
                process_state,
                min(len(self._stderr_lines), 20),
            )
            # Name the budget: a session-start timeout (90s) must be
            # distinguishable from a generic control-plane one (30s).
            timeout_exc = AcpRequestTimeout(f"Request {method} timed out after {timeout:g}s")
            if adopted is not None:
                # What create_session needs to build the collector.
                setattr(timeout_exc, "req_id", req_id)
                setattr(timeout_exc, "adopted_future", adopted)
            raise timeout_exc
        if stage is not None:
            active_starts, queued_starts = _cold_start_counts()
            logger.info(
                "acp_startup_stage stage=%s outcome=ready timeout_method=%s "
                "timeout_budget_s=%g duration_ms=%.1f active_starts=%d queued_starts=%d",
                stage,
                method,
                timeout,
                (time.monotonic() - started) * 1000.0,
                active_starts,
                queued_starts,
            )
        return result

    async def _drain_stderr(self) -> None:
        """Drain stderr to prevent subprocess blocking."""
        assert self._process and self._process.stderr
        stderr = self._process.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    if self.recording_allowed:
                        self._stderr_lines.append(text)
                        if len(self._stderr_lines) > 20:
                            self._stderr_lines = self._stderr_lines[-20:]
                    # Latch here, at the sink, because this is the only point at
                    # which every line is guaranteed to have been seen. The
                    # trim above is what makes it necessary: nobody asks about
                    # auth until a request has already timed out, by which time a
                    # chatty startup can have pushed the auth line out of the ring.
                    # Deliberately does not log: the line below already emits this
                    # text at debug, so a second record here would add no content
                    # and only raise arbitrary matched stderr to a default-visible
                    # level, against this sink's own convention. The condition is
                    # surfaced where it is actionable instead -- as AcpAuthRequired.
                    if not self._saw_auth_failure and is_auth_failure_output(text):
                        self._saw_auth_failure = True
                    # Same sink, same reason as the auth latch: matched
                    # unconditionally (not only when ``recording_allowed``), so a
                    # restricted session that retains no stderr still gets the
                    # actionable classification rather than a bare exit code.
                    #
                    # SCOPED TO THE PRE-INITIALIZE WINDOW, unlike the auth latch,
                    # and the asymmetry is the point. A rejected credential does
                    # not un-reject itself, so latching it for the runtime's life
                    # is correct. This signature is not that: the child's OWN
                    # sandbox can refuse mid-life when the HARNESS spawns a tool
                    # subprocess, which says nothing about whether the agent
                    # process can start. A life-long latch would turn the next
                    # unrelated death -- a broken pipe, an OOM kill -- into a
                    # permanent "your sandbox is broken", and permanently is
                    # exactly how long the wrong verdict would last. A refusal
                    # that genuinely stops the child from starting is always
                    # printed before ``initialize`` completes, so the window that
                    # matters closes there.
                    if (
                        not self._first_session_ready
                        and not self._saw_sandbox_init_failure
                        and is_sandbox_init_failure_output(text)
                    ):
                        self._saw_sandbox_init_failure = True
                    if self.recording_allowed:
                        logger.debug("stderr: %s", text[:200])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An overlong stderr line (ValueError / LimitOverrunError from
            # readline when no newline fits the buffer) or a low-level read
            # error must not kill this task with an unhandled exception. Log and
            # exit the drain cleanly rather than leaving a dead task behind.
            logger.debug(
                "stderr drain task exiting on error: %s",
                exc if self.recording_allowed else type(exc).__name__,
                exc_info=self.recording_allowed,
            )
