"""Asyncio unix-socket server for the KiroCrew MCP gateway.

This module is the entry point for ``python -m
kiro_crew.mcp_gateway.gatewayd`` and for in-process use by
:class:`kiro_crew.mcp_gateway.manager.GatewayManager`.
The daemon wires the full bidirectional JSON-RPC pump on top of the
register skeleton:

* Register handshake (unchanged from M1) produces the :class:`PoolKey`.
* First non-register message triggers a lazy backend spawn through
  :meth:`BackendPool.get_or_create` — concurrent stubs with the same key
  share one backend, with spawn-dedup handled inside the pool.
* Stub→gateway pump reads line-delimited JSON-RPC and forwards through
  :meth:`Backend.forward_from_stub`, which handles id rewriting, caller-
  identity injection, and initialize caching.
* Gateway→stub pump drains the per-stub inbox queue populated by the
  backend's stdout task.
* Handshake phase has a timeout; the bridge phase is NOT timeout-wrapped
  (learned correction — a single timeout around the bridge silently kills
  healthy long-lived sessions).

Graceful shutdown: setting the ``stop_event`` stops accepts, drains
in-flight connection handlers up to ``_SHUTDOWN_DRAIN_SECS``, shuts the
pool down, and unlinks the socket before return. SIGTERM/SIGINT handlers
installed by the caller should just forward into ``stop_event.set()``.

This module is the daemon's executable, its only import path and its one patch
surface. The rules live in private owners under :mod:`kiro_crew.mcp_gateway.daemon`,
one per responsibility (``docs/system-specs/modules/mcp-gateway-daemon-lifecycle.md``
maps them), and every name an owner holds is re-exported below. What stays here is
what repository guards read in this file by path or by source, and the state it
writes: :func:`run_gatewayd` (bootstrap, task wiring and shutdown, with the
lifecycle identity it publishes and the shutdown drain's write accounting),
:func:`_acquire_backend` (the spawn transaction), :func:`main` (the process's one
``asyncio.run``) and the ``_ensure_ssl_certs()`` call that must precede every
import. An owner reads each name a test patches here through
``daemon.facade`` at call time, so a patch of ``gatewayd.X`` reaches every caller.
"""

from __future__ import annotations

# System-trust injection is process-local and must run before imports below can
# create or cache an SSLContext. Environment-only CA settings are inherited
# from GatewayManager, but Security.framework-backed contexts are not.
from kiro_crew._ssl_compat import _ensure_ssl_certs

_ensure_ssl_certs()

import asyncio
import contextlib
import logging
import os
import site  # noqa: F401 - tests patch user-site probing through gatewayd.site
import sys
import time
from pathlib import Path
from typing import Any, Iterator, Optional

import kiro_crew  # noqa: F401 - tests read the package root through gatewayd.kiro_crew
from kiro_crew.code_fingerprint import warm_code_fingerprint
from kiro_crew.config.loader import (  # noqa: F401 - patched as gatewayd.KiroCrewConfig
    KiroCrewConfig,
)
from kiro_crew.config.loader import config_dir as _config_dir
from kiro_crew.env import mcp_search_path, spec_path_key
from kiro_crew.executors import maintenance_executor

# A name imported from outside the daemon with ``noqa: F401`` is a patch seam: this
# module does not call it, an owner reads it through ``daemon.facade``.
from kiro_crew.mcp_caller import _parent_pid as _ppid_fn  # noqa: F401
from kiro_crew.mcp_gateway import socketsec  # noqa: F401
from kiro_crew.mcp_gateway import credwatch, hazards, launch_approval, transport
from kiro_crew.mcp_gateway.admission import (
    DEFAULT_CAPACITY,
    DEFAULT_CEILING,
    DEFAULT_FLOOR,
    OUTCOME_FAILURE,
    OUTCOME_NEUTRAL,
    Admission,
    OnQueued,
    Permit,
    SpawnGate,
)
from kiro_crew.mcp_gateway.apps import sweep_spool as apps_sweep_spool
from kiro_crew.mcp_gateway.backend import INTERNAL_STUB_PREFIXES  # noqa: F401
from kiro_crew.mcp_gateway.backend import Backend, spawn_backend
from kiro_crew.mcp_gateway.backend_tmp import sweep_all_backend_tmp  # noqa: F401
from kiro_crew.mcp_gateway.breaker import CircuitBreaker

# Every name an owner holds is bound here to the object the owner holds.
from kiro_crew.mcp_gateway.daemon import _FACADE
from kiro_crew.mcp_gateway.daemon.admission_protocol import (  # noqa: F401
    _CAPACITY_FAILURES,
    _CAPACITY_RETRY_AFTER_SECS,
    _LEGACY_SPAWN_WAIT_SECS,
    _MAX_PENDING_BYTES,
    _MAX_PENDING_FRAMES,
    _PRESSURE_ERRNOS,
    _QUEUE_REFUSAL_MARGIN_SECS,
    REJECT_CLASS_CAPACITY,
    REJECT_CLASS_COMPAT,
    REJECT_CLASS_ISOLATION,
    _await_answering_pings,
    _capacity_rejection,
    _classify_rejection,
    _negotiated_wait_budget,
    _PeerGone,
    _refuse_ensure_backend,
    _refuse_lazy_spawn,
    _Rejection,
    _reply_rejected,
)
from kiro_crew.mcp_gateway.daemon.audit import (  # noqa: F401
    _audit_abort_applied,
    _audit_caller_claimed,
    _audit_caller_rekey,
    _audit_disconnect_cancel,
    _audit_peer_allowed,
    _audit_peer_denied,
    _audit_peer_identity_denied,
    _audit_peer_identity_resolved,
    _audit_pool_fallback,
    _audit_pool_rejected,
    _audit_prewarm_spawn,
    _audit_recaller_rejected,
    _audit_replacement_validated,
    _audit_reserved_stub_prefix_denied,
    _audit_stand_down,
)
from kiro_crew.mcp_gateway.daemon.cli import (  # noqa: F401
    _DEFAULT_SOCKET_NAME,
    _DEFAULT_SOCKET_SUBDIR,
    _amain,
    _build_argparser,
    _default_cli_socket_path,
)
from kiro_crew.mcp_gateway.daemon.connection import (  # noqa: F401
    REGISTERED_CAPABILITIES,
    _handle_connection,
    _teardown_connection,
)
from kiro_crew.mcp_gateway.daemon.control import (  # noqa: F401
    _apply_abort,
    _apply_claim,
    _apply_set_spawn_capacity,
    _apply_stand_down,
    _pong_payload,
    _serve_control_frame,
)
from kiro_crew.mcp_gateway.daemon.control_plane import (  # noqa: F401
    CONTROL_PLANE_BACKENDS,
    _caller_for_backend,
    _deny_control_plane,
    _kiro_crew_import_is_shadowed,
    _spawns_own_control_plane,
    _user_site_holds_our_package,
    _user_site_roots,
)
from kiro_crew.mcp_gateway.daemon.diagnostics import (  # noqa: F401
    _ZOMBIE_PROBE_INTERVAL_SECS,
    _collect_task_stacks,
    _count_open_fds,
    _emit_backend_acquire_metric,
    _emit_lazy_load_metrics,
    _read_rss_kb,
    _snapshot_state,
    _write_diagnostic,
    _zombie_diagnostic,
    _zombie_diagnostic_path,
)
from kiro_crew.mcp_gateway.daemon.identity import (  # noqa: F401
    _CONN_INDEX,
    _MAX_TOKEN_BINDINGS,
    _TOKEN_BINDINGS,
    _apply_recaller,
    _bind_token,
    _caller_from_register,
    _conn_index_add,
    _conn_index_discard,
    _peer_admitted,
    _register_pids,
    _resolve_peer_identity,
    _resolve_register_identity,
    _StubConn,
    _token_caller,
    _token_is_unbound,
)
from kiro_crew.mcp_gateway.daemon.launch import (  # noqa: F401
    _LAUNCH_APPROVAL_SNAPSHOT,
    TargetResolver,
    _approval_env_identity,
    _call_with_approval_snapshot,
    _declared_env_for_private_backend,
    _declared_env_pairs,
    _declared_env_to_forward,
    _declared_non_secret_env,
    _launch_approved_from_snapshot,
    _read_declared_env_sidecar,
    _resolve_once_home,
    _resolve_target_off_loop,
    _TargetUnknown,
    env_target_resolver,
    resolvable_target_stems,
    resolve_once_resolver,
)
from kiro_crew.mcp_gateway.daemon.replacement import (  # noqa: F401
    _init_timeout_of,
    _live_session_key,
    _refuse_replacement,
    _rekey_refusal_reason,
    _ReplacementRefused,
    _respawn_backend_for_stub,
    _respawn_backend_for_stub_unrecorded,
)
from kiro_crew.mcp_gateway.daemon.sweepers import (  # noqa: F401
    _HEARTBEAT_SWEEP_INTERVAL_SECS,
    _OWNER_LIVENESS_INTERVAL_SECS,
    _OWNER_LIVENESS_MISSES,
    _SOCKET_LIVENESS_MISSES,
    _backend_tmp_sweeper,
    _heartbeat_sweeper,
    _idle_sweeper,
    _owner_liveness_sweeper,
    _socket_liveness_sweeper,
)
from kiro_crew.mcp_gateway.daemon.warm_pool import (  # noqa: F401
    _CREDENTIAL_WATCH_INTERVAL_SECS,
    _HOT_KEYS_FLUSH_INTERVAL_SECS,
    _PREWARM_SPAWN_WAIT_SECS,
    _PREWARM_TOPUP_INTERVAL_SECS,
    _drain_and_rewarm_on_credential_change,
    _hot_keys_flush_sweeper,
    _prewarm_topup_sweeper,
    _Prewarmer,
    _PrewarmStoodDown,
)
from kiro_crew.mcp_gateway.daemon.wire import (  # noqa: F401
    _MAX_FRAME_BYTES,
    _REGISTER_TIMEOUT_SECS,
    _STUB_KEEPALIVE_TIMEOUT_SECS,
    _STUB_PROBES,
    _WRITE_REPLY_TIMEOUT_SECS,
    STUB_KEEPALIVE_TYPE,
    _drain_inbox_to_stub,
    _is_ping_frame,
    _jsonrpc_error,
    _probe_stub_transports,
    _read_first_frame,
    _stub_probe_add,
    _stub_probe_discard,
    _StubProbe,
    _write_json_line,
)
from kiro_crew.mcp_gateway.host_budget import (
    HostBudget,
    HostBudgetLimits,
    HostCharge,
    resolve_limits,
)
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES, BackendPool, PoolKey
from kiro_crew.mcp_gateway.prewarm import HotKeyStore, default_hot_keys_path
from kiro_crew.mcp_gateway.resolve_once import resolved_launch  # noqa: F401
from kiro_crew.mcp_gateway.rewriter import (  # noqa: F401
    forward_declared_env_enabled,
    pool_identity_env_keys,
    records_dir,
)
from kiro_crew.mcp_gateway.secret_uri import SECRET_URI_PREFIX, resolve_secret_uris
from kiro_crew.mcp_gateway.shutdown_budget import DRAIN_SECS, POOL_SHUTDOWN_SECS
from kiro_crew.mcp_gateway.spill import cleanup_old_spill_files
from kiro_crew.mcp_gateway.stub import fallback_counts as stub_fallback_counts  # noqa: F401
from kiro_crew.metrics.provider import get_recorder  # noqa: F401
from kiro_crew.platform_compat import _UTF8_PROCESS_ENV, IS_WINDOWS
from kiro_crew.platform_compat import count_open_fds as _shared_count_open_fds  # noqa: F401
from kiro_crew.platform_compat import get_process_start_id as _get_process_start_id  # noqa: F401
from kiro_crew.platform_compat import pid_exists as _pid_exists  # noqa: F401
from kiro_crew.platform_compat import proc_rss_bytes as _proc_rss_bytes  # noqa: F401
from kiro_crew.platform_compat import process_start_time as _process_start_time
from kiro_crew.sandbox import warm_backend  # noqa: F401
from kiro_crew.sandbox import (
    CANONICAL_TEMP_KEYS,
    classify_declared_temp_env,
    declared_temp_refusal_reasons,
    format_declared_temp_refusals,
)
from kiro_crew.security import redact
from kiro_crew.sel import SecurityEventLog  # noqa: F401

logger = logging.getLogger(__name__)

# Graceful-shutdown drain window: how long in-flight tool calls get to finish
# their current JSON-RPC round-trip before gatewayd cancels them and tears down
# the pool. Sourced from the shared budget module so the supervisor's
# SIGTERM→SIGKILL grace is always derived from (and therefore covers) it.
_SHUTDOWN_DRAIN_SECS = DRAIN_SECS

#: The gateway PID this daemon was spawned for; 0 when run by hand. Read by
#: :func:`_pong_payload` so a pinger can tell an ORPHAN (owner dead) from a
#: daemon another live gateway still owns, and by the stand-down handler.
_OWNER_PID: int = 0

#: This daemon's own ``process_start_time`` token, computed once at startup
#: (off the loop) so the pong can publish it without a syscall or a ``ps``.
_OWN_START_TIME: str = ""


#: Number of stub writers currently inside their write+drain critical section
#: (see :func:`_drain_inbox_to_stub`). A frame there has been dequeued but not
#: yet flushed, so it is invisible to BOTH the inbox depth and the pending map.
#: Process-global by design: the shutdown drain asks a process-global question
#: ("is any reply mid-flight?"), and the writer coroutine holds no backend
#: reference to hang per-backend state on.
_active_stub_writes = 0


@contextlib.contextmanager
def _counted_stub_write() -> Iterator[None]:
    """Mark a stub write+drain as in progress for the shutdown drain predicate.

    Sync context manager wrapped around an ``async with`` block: the increment
    lands before the awaits and the ``finally`` decrement runs on completion,
    error, AND cancellation, so a cancelled writer cannot leak the counter and
    wedge every future shutdown into the full drain window.
    """
    global _active_stub_writes
    _active_stub_writes += 1
    try:
        yield
    finally:
        _active_stub_writes -= 1


def _has_outstanding_work(pool: BackendPool) -> bool:
    """Return ``True`` if any client response is still undelivered.

    This is the shutdown drain predicate, and it covers every stage a reply can
    occupy between the backend and the stub socket:

    1-3. :attr:`Backend.outstanding_work` — awaiting the backend reply, mid
         MCP-Apps delivery, or queued for the stub writer.
    4.   :data:`_active_stub_writes` — dequeued and inside the write+drain
         critical section, so invisible to both the pending map and the queue
         depth.

    Stage 4 is the LAST application-level stage: once ``drain()`` returns the
    bytes are in the kernel socket buffer and delivery is not ours to
    guarantee. So this predicate is complete, not merely one stage deeper.

    ``all_backends()`` deliberately includes DRAINING backends (a blue-green
    credential cutover may be mid-flight), so a restart cannot cut a call a
    draining backend still serves.
    """
    if _active_stub_writes:
        return True
    return any(backend.outstanding_work for backend in pool.all_backends())


async def run_gatewayd(
    socket_path: Path | str,
    *,
    max_backends: int,
    idle_timeout_secs: int,
    stop_event: asyncio.Event,
    target_resolver: Optional[TargetResolver] = None,
    prewarm_count: int = 0,
    credential_watch_paths: Optional[list[Path]] = None,
    owner_pid: int = 0,
    spawn_concurrency: int = DEFAULT_CAPACITY,
    spawn_concurrency_min: int = DEFAULT_FLOOR,
    spawn_concurrency_max: int = DEFAULT_CEILING,
    spawn_queue_wait_secs: float = 600.0,
    initialize_timeout_secs: float = 10.0,
    host_budget_limits: Optional[HostBudgetLimits] = None,
) -> None:
    """Run the gateway until ``stop_event`` is set.

    Args:
        socket_path: Absolute path for the unix socket. Parent directories
            are created if missing; a stale socket left by a prior crash
            is removed before bind.
        max_backends: Pool capacity. When the pool is full and a new key
            arrives, :meth:`BackendPool.get_or_create` evicts the least-
            recently-used idle entry before spawning the new one.
        idle_timeout_secs: A backend whose stubs have all detached and
            whose ``last_used_at`` is older than this is evicted by the
            idle sweeper (runs every ``idle_timeout_secs / 4``, minimum
            500 ms).
        stop_event: Caller-owned event. Setting it triggers graceful
            shutdown: accept loop exits, in-flight handlers get
            ``_SHUTDOWN_DRAIN_SECS`` to finish, then everything cancels,
            the pool shuts down, and the socket is unlinked.
        target_resolver: Callable mapping :class:`PoolKey` to the spawn
            4-tuple ``(command, args, env, work_dir)``. Pass ``None`` to
            use the default :func:`env_target_resolver`. Tests supply a
            custom resolver to avoid coupling to environment variables.
        prewarm_count: Number of hottest observed PoolKeys to spawn at
            startup before the first stub connects, closing the
            cold-after-restart / cold-after-idle new-chat latency gap. The
            list of hot keys is learned from prior registers and persisted
            beside the socket in ``hot-keys.json``. ``0`` (default) disables
            prewarming entirely — no file is read or written, no extra task
            runs. Clamped to ``max_backends - 1`` if set at or above pool
            capacity, since prewarmed backends are pinned and would otherwise
            leave no reclaimable slot for a live, non-warm session.
        credential_watch_paths: Credential files to watch for content
            changes. On a real rotation (content digest change — a no-op
            rewrite with identical bytes never fires), ALL pooled backends
            are drained via a blue-green cutover so they respawn with the
            fresh credential, then the warm pool is re-warmed. ``None`` or
            empty (the public default) creates no watcher task — the run
            flow is byte-identical to the pre-watcher daemon. The paths are
            caller-supplied (typically threaded through the seam-resolved
            ``--credential-watch-path`` argv flags); the daemon never
            hardcodes or interprets any credential path.
        spawn_concurrency: Daemon-wide number of backend spawn+initialize
            windows allowed in flight at once (the :class:`SpawnGate`
            capacity); ``spawn_concurrency_min`` / ``_max`` clamp it and are
            the bounds the adaptive controller moves it within. Every spawn
            path -- pooled, private, respawn, prewarm -- takes a permit.
        spawn_queue_wait_secs: Longest a stub that negotiated ``spawn_queue``
            may be held in the gate's FIFO before a ``capacity`` rejection.
            A stub's own ``wait_budget_secs`` can only shorten it.
        initialize_timeout_secs: Bound on a backend's first ``initialize``
            window, threaded onto every spawned :class:`Backend`. The gate
            permit is held for the same window.
        host_budget_limits: Ceilings for the host budget every backend --
            pooled, private, fallback -- is charged against. ``None`` derives
            them (``0`` = auto) from ``max_backends`` and this process's
            descriptor limit.

    The function never raises on normal shutdown. Startup failures (e.g.
    socket directory not creatable, another daemon already bound to the
    path) propagate so the caller can surface a clear error.
    """
    # Published for the ping reply before anything can connect.
    global _OWNER_PID
    _OWNER_PID = int(owner_pid) if owner_pid > 0 else 0
    # The fingerprint the pong and the stand-down handler read is computed
    # once, HERE, off the loop: its first computation runs git (or walks the
    # package tree), and the connection handler that reads it must not pay
    # that on the event loop.
    await warm_code_fingerprint()
    # The daemon's own start-time identity, read ONCE here off the loop: on
    # macOS ``process_start_time`` is a ``ps`` subprocess, and the pong that
    # publishes it is answered from the connection handler on the loop.
    global _OWN_START_TIME
    _own_start = await asyncio.to_thread(_process_start_time, os.getpid())
    _OWN_START_TIME = _own_start or ""
    socket_path = Path(socket_path)
    # Off the event loop for the same reason as the manager's call: the
    # owner-only step is blocking filesystem work (the Windows DACL is applied
    # in-process). Startup is the least contended moment in this process, but
    # the daemon's signal handlers and supervising ping are already live, so it
    # is offloaded here too.
    await asyncio.to_thread(transport.prepare_dir, socket_path)
    # Singleton guard (race-free): acquire an exclusive advisory lock on a
    # lockfile beside the endpoint BEFORE probing/unlinking/binding. Without it,
    # two daemons that start in the same instant both pass the connect-probe
    # in remove_stale, both unlink+bind, and the later bind silently
    # steals the socket from the earlier — leaving the earlier daemon
    # orphaned-but-listening. Repeated, this leaks N daemons on one socket
    # path and splits stub<->backend routing across them, surfacing to
    # kiro-cli as intermittent "transport closed". The lock lets exactly one
    # daemon win; losers exit cleanly below. The OS releases the lock on
    # process death, so there is no stale-lock mode.
    lock_fd = transport.acquire_singleton_lock(socket_path)
    if lock_fd is None:
        logger.warning(
            "gatewayd: another instance already owns %s — exiting without "
            "binding (singleton guard)",
            socket_path,
        )
        return
    await transport.remove_stale(socket_path)

    resolver = target_resolver if target_resolver is not None else env_target_resolver
    # Pre-resolved npm specs launch straight from the store; everything else is
    # handed through unchanged. Wrapping an INJECTED resolver too keeps the
    # behaviour identical whether the daemon resolves from env or a test's stub.
    resolver = resolve_once_resolver(resolver)
    # Shared circuit breaker keyed by server name: a server
    # that crash-loops on spawn trips OPEN and get_or_create rejects further
    # spawns so the stub falls back to per-session exec instead of churning.
    breaker = CircuitBreaker()
    pool = BackendPool(max_backends=max_backends, breaker=breaker)
    # One admission state for the daemon: the global spawn gate (bounds
    # spawn+initialize windows in flight, FIFO past that) and the host budget
    # (charges every process this daemon is answerable for). Built beside the
    # pool because every spawn path -- pooled, private, respawn, prewarm --
    # runs through ``_acquire_backend`` and takes both.
    if host_budget_limits is None:
        host_budget_limits = resolve_limits(
            max_procs=0,
            max_rss_mb=0,
            max_fds=0,
            available_mb=None,
            max_backends=max_backends,
        )
    admission = Admission(
        gate=SpawnGate(
            spawn_concurrency,
            floor=max(1, spawn_concurrency_min),
            ceiling=max(max(1, spawn_concurrency_min), spawn_concurrency_max),
        ),
        budget=HostBudget(host_budget_limits),
        initialize_timeout_secs=initialize_timeout_secs,
        spawn_queue_wait_secs=spawn_queue_wait_secs,
    )
    connections: set[asyncio.Task[None]] = set()

    # MCP Apps spool hygiene: reap records past their 24h TTL at every daemon
    # start (write_spool also sweeps opportunistically per write). Offloaded —
    # it walks a directory — and best-effort: a failed sweep must never stop
    # the daemon from serving.
    try:
        swept = await asyncio.to_thread(apps_sweep_spool)
        if swept:
            logger.info("mcp-apps: startup sweep removed %d expired spool record(s)", swept)
    except Exception:
        logger.debug("mcp-apps: startup spool sweep failed", exc_info=True)

    # Clamp prewarm_count below pool capacity. Prewarmed backends are pinned —
    # exempt from the idle sweeper and LRU eviction — so prewarming every slot
    # would leave no reclaimable capacity for a live stub whose key isn't in the
    # warm set, and get_or_create would raise PoolAtCapacity for real sessions.
    # Reserve at least one unpinned slot. (A misconfigured prewarm_count must
    # never be able to starve live traffic.)
    if prewarm_count > 0 and prewarm_count >= max_backends:
        clamped = max(0, max_backends - 1)
        logger.warning(
            "prewarm_count=%d >= max_backends=%d would pin the whole pool; "
            "clamping to %d to reserve capacity for live sessions",
            prewarm_count,
            max_backends,
            clamped,
        )
        prewarm_count = clamped

    # Hot-key store powers warm-pool prewarming. Only instantiated when
    # prewarming is enabled; otherwise ``None`` and the record path is a
    # no-op so the default (disabled) build pays nothing.
    hot_keys: Optional[HotKeyStore] = (
        HotKeyStore(default_hot_keys_path(socket_path)) if prewarm_count > 0 else None
    )

    # Observed-hazard sink. Unconditional, unlike the hot-key store: this is
    # how a server that misbehaves under sharing gets its recommendation
    # withdrawn, and that must not depend on prewarming being enabled. Prior
    # observations are loaded so a daemon restart does not forget them — and
    # that read is offloaded, because a slow ledger store would otherwise delay
    # socket readiness and every task already on this loop.
    await asyncio.to_thread(hazards.install_sink, records_dir(socket_path))

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        try:
            await _handle_connection(
                reader,
                writer,
                pool,
                resolver,
                socket_path,
                hot_keys,
                stop_event=stop_event,
                admission=admission,
            )
        except asyncio.CancelledError:
            # Normal on shutdown — propagate for the gather() below.
            raise
        except ConnectionError:
            # Abrupt peer disconnect (ECONNRESET / EPIPE from a hard-killed
            # client) is routine — the clean-EOF sibling is already handled
            # inside _handle_connection — so don't log it as a crash.
            logger.debug("client disconnected abruptly", exc_info=True)
        except Exception:
            logger.exception("connection handler crashed")
        finally:
            if task is not None:
                connections.discard(task)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def _on_client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # asyncio.start_unix_server's callback isn't async; spawn the real
        # handler as a tracked task so shutdown can cancel it. Any
        # exception raised here (rare — create_task and set.add only fail
        # under resource exhaustion) would otherwise propagate into
        # asyncio's server internals and wedge the accept loop silently.
        # Explicit try/except + exception-level log keeps those failures
        # attributable.
        try:
            task = asyncio.create_task(_handle(reader, writer))
            connections.add(task)
        except Exception:
            logger.exception(
                "accept callback crashed while spawning handler; " "closing connection"
            )
            try:
                writer.close()
            except Exception:
                pass

    # --- Resource-guarded startup block ---
    # The singleton lock (lock_fd) and the bound endpoint are acquired/created
    # below. If ANY step between bind and the main await-stop_event raises
    # (EADDRINUSE from the bind, a hardening failure, a create_task OOM),
    # the finally block ensures both the lock and the endpoint are
    # released/torn down — preventing a leaked lock that blocks restart and
    # a dangling socket that confuses the next startup probe.
    server: Optional[transport.TransportServer] = None
    sweeper: Optional[asyncio.Task[None]] = None
    tmp_sweeper: Optional[asyncio.Task[None]] = None
    socket_liveness: Optional[asyncio.Task[None]] = None
    owner_liveness: Optional[asyncio.Task[None]] = None
    diagnostic: Optional[asyncio.Task[None]] = None
    heartbeat: Optional[asyncio.Task[None]] = None
    flush_sweeper: Optional[asyncio.Task[None]] = None
    topup_sweeper: Optional[asyncio.Task[None]] = None
    credential_watchers: list[asyncio.Task[None]] = []
    # The warm-pool passes and their tasks (a no-op owner when prewarming is off).
    prewarmer = _Prewarmer(pool, resolver, admission, hot_keys, prewarm_count)

    try:
        # Local IPC endpoint: an AF_UNIX socket on POSIX, a named pipe on
        # Windows. ``transport`` owns the platform split so nothing here (or in
        # the stub, the manager, claim or abort) has to know which is in play.
        server = await transport.serve(
            socket_path,
            _on_client_connected,
            limit=READ_BUFFER_LIMIT_BYTES,
        )
        # Endpoint hardening: restrict the freshly-bound endpoint to the owning
        # user. POSIX tightens the socket file to 0600 here; Windows applies an
        # owner-only DACL at creation instead, because a default-descriptor pipe
        # is readable by Everyone and fixing it after the fact would leave a
        # window. Defense-in-depth on top of the 0700 $KIROCREW_HOME directory;
        # the per-connection peer check in _handle_connection is the second
        # layer.
        transport.harden_endpoint(socket_path)
        # Clean up stale spill files from prior runs (older than 24h).
        try:
            await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(), cleanup_old_spill_files
            )
        except Exception:  # pragma: no cover — defensive
            logger.debug("spill cleanup failed at startup", exc_info=True)
        logger.info(
            "gatewayd listening socket=%s max_backends=%d idle_timeout=%ds",
            socket_path,
            max_backends,
            idle_timeout_secs,
        )

        # Idle sweeper — wakes every ``idle_timeout_secs / 4`` (bounded to
        # 500 ms minimum) and evicts any backend whose stubs have all detached
        # and whose ``last_used_at`` is past the deadline.
        sweep_interval = max(0.5, float(idle_timeout_secs) / 4.0)
        sweeper = asyncio.create_task(
            _idle_sweeper(pool, idle_timeout_secs, sweep_interval, stop_event),
            name="mcp-gateway-idle-sweeper",
        )

        # Backend temp containment: reclaim per-process temp dirs
        # whose owner is dead AND whose content is idle (see backend_tmp --
        # deletion deliberately lives ONLY here, never on a shutdown path,
        # because a launcher's exit is not proof its process tree is gone).
        # First pass at task start (same boot posture as the spool sweep),
        # then hourly; offloaded and best-effort.
        tmp_sweeper = asyncio.create_task(_backend_tmp_sweeper(), name="mcp-gateway-tmp-sweeper")

        # Socket-liveness self-exit: the daemon is its own session/group
        # leader, so a launcher that dies without signalling it (pytest
        # teardown is the common case) leaves it resident forever — and the
        # tracked-PID and orphan sweeps both exclude gateway entrypoints by
        # design. The one unreachability signal observable from inside is the
        # listening socket path this daemon created: once it is gone, no stub
        # can ever connect again. Armed HERE, only after ``transport.serve``
        # bound the endpoint — before bind, an absent path is a startup race,
        # not unreachability. POSIX-only: a Windows named pipe has no
        # directory entry to observe.
        if not IS_WINDOWS:
            socket_liveness = asyncio.create_task(
                _socket_liveness_sweeper(socket_path, sweep_interval, stop_event),
                name="mcp-gateway-socket-liveness",
            )

        # Owner-liveness self-exit: this daemon exists to serve ONE gateway
        # process -- the one that spawned it -- and has no business outliving
        # it. Its socket is spawned ``start_new_session=True`` so a SIGKILLed
        # gateway never signals it, and the next gateway to start then found
        # a healthy daemon on the socket and ADOPTED it: a daemon running the
        # code of a checkout two days old, pooling backends that spoke a
        # control-frame shape the new gateway did not read. Watching the
        # owner's PID (with its start time, so a recycled PID is not mistaken
        # for the owner) closes that: the owner dying takes the daemon down the
        # same graceful path SIGTERM takes. Not armed when no owner was named
        # (an operator running the module by hand).
        if owner_pid > 0:
            owner_liveness = asyncio.create_task(
                _owner_liveness_sweeper(owner_pid, _OWNER_LIVENESS_INTERVAL_SECS, stop_event),
                name="mcp-gateway-owner-liveness",
            )

        # Zombie diagnostic: probes
        # ``server.is_serving()`` every 30 s and dumps a post-mortem JSONL on
        # divergence. Costs ~0 in the healthy case; captures the cause of
        # accept-loop death on the first zombie event.
        diagnostic = asyncio.create_task(
            _zombie_diagnostic(server, pool, connections, stop_event),
            name="mcp-gateway-zombie-diagnostic",
        )

        # Per-backend heartbeat sweep: recycle gone/wedged
        # backends and feed the circuit breaker. First sweep fires one interval
        # after startup.
        heartbeat = asyncio.create_task(
            _heartbeat_sweeper(
                pool,
                _HEARTBEAT_SWEEP_INTERVAL_SECS,
                stop_event,
                backends_pidfile=Path(f"{socket_path}.backends"),
            ),
            name="mcp-gateway-heartbeat-sweeper",
        )

        # Warm-pool prewarming (optional): persist observed hot keys and keep the
        # hottest backends warm. All prewarm tasks are background tasks created
        # AFTER the socket is listening, so none delays the daemon becoming
        # reachable. Disabled (hot_keys is None) => no prewarm task is created and
        # the record/IO paths are no-ops. The three triggers that keep the warm set
        # ready -- startup, the top-up sweeper and a credential rotation -- all run
        # ``prewarmer``'s one idempotent pass.
        if hot_keys is not None:
            flush_sweeper = asyncio.create_task(
                _hot_keys_flush_sweeper(hot_keys, _HOT_KEYS_FLUSH_INTERVAL_SECS, stop_event),
                name="mcp-gateway-hot-keys-flush",
            )
            topup_sweeper = asyncio.create_task(
                _prewarm_topup_sweeper(
                    prewarmer.schedule, _PREWARM_TOPUP_INTERVAL_SECS, stop_event
                ),
                name="mcp-gateway-prewarm-topup",
            )
            # (a) Warm once at startup -- initial=True loads persisted hot keys.
            prewarmer.schedule(initial=True)

        # Credential-rotation drain: on a content change of any watched
        # credential file, drain ALL pooled backends (blue-green cutover) so
        # they respawn with the fresh credential, then re-warm. Watcher tasks
        # exist ONLY when the caller supplied watch paths — the public default
        # (no paths) creates no task and the run flow is byte-identical.
        async def _on_credential_change() -> None:
            await _drain_and_rewarm_on_credential_change(pool, prewarmer.schedule)

        for cred_path in credential_watch_paths or []:
            credential_watchers.append(
                asyncio.create_task(
                    credwatch.watch_credential(
                        cred_path,
                        _CREDENTIAL_WATCH_INTERVAL_SECS,
                        stop_event,
                        _on_credential_change,
                        logger,
                    ),
                    name="mcp-gateway-credential-watcher",
                )
            )

        await stop_event.wait()
    finally:
        logger.info("gatewayd shutting down (connections=%d)", len(connections))
        # Admission closes FIRST: every queued spawn is failed with
        # SpawnGateClosed (its stub gets a capacity rejection and can retry
        # against the next daemon), every initialize watcher is cancelled
        # (releasing its permit as neutral) and every host charge is dropped.
        # Doing this before the accept loop stops means no waiter is admitted
        # into a spawn that the pool teardown below would immediately reap.
        with contextlib.suppress(Exception):
            await admission.close()
        # Stop accepting first, but do NOT await wait_closed() yet: since
        # Python 3.12 it waits for every accepted connection to finish, so
        # awaiting it here would block for as long as any stub stayed
        # connected -- the drain and cancel below are what let it return.
        if server is not None:
            server.close()

        # Phase 1: let outstanding CLIENT WORK finish. The wait condition is
        # deliberately "some backend still owes a response", NOT "the connection
        # set is empty". A pooled stub's bridge connection is long-lived and
        # never self-closes, so the old ``while connections`` form burned the
        # entire window on every restart that had attached stubs — and because
        # the supervisor's grace period was shorter than this window, gatewayd
        # was SIGKILLed mid-drain and never reached ``pool.shutdown_all()``.
        # ``outstanding_work`` covers all three stages a response can sit in
        # (awaiting the backend, mid-MCP-Apps delivery, queued for the stub
        # writer), so a completed-but-undelivered reply still holds the drain
        # open. An idle bridge owes nothing and is cancelled in Phase 2 below.
        if connections:
            drain_deadline = time.monotonic() + _SHUTDOWN_DRAIN_SECS
            while time.monotonic() < drain_deadline and _has_outstanding_work(pool):
                await asyncio.sleep(0.05)

        # Phase 2: cancel whatever is still in-flight.
        for task in list(connections):
            task.cancel()
        if connections:
            await asyncio.gather(*connections, return_exceptions=True)
        connections.clear()

        # Now that nothing is holding a connection open, the server can finish
        # closing. Suppressed: a teardown-time error here is not worth failing
        # a shutdown that has already stopped serving.
        if server is not None:
            with contextlib.suppress(Exception):
                await server.wait_closed()

        for background in (
            sweeper,
            tmp_sweeper,
            socket_liveness,
            owner_liveness,
            diagnostic,
            heartbeat,
            topup_sweeper,
            *credential_watchers,
        ):
            await _retire_task(background)
        credential_watchers.clear()

        # Cancel any in-flight warm passes (startup / top-up / credential-triggered)
        # so a slow handshake cannot stall shutdown.
        await prewarmer.cancel_all()

        await _retire_task(flush_sweeper)

        # Final flush so the last observation window isn't lost on a clean
        # shutdown. Off the loop; best-effort (we're tearing down anyway).
        if hot_keys is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(hot_keys.flush)

        await pool.shutdown_all(timeout=POOL_SHUTDOWN_SECS)
        # Clean shutdown drained every backend; drop the out-of-band reap list
        # so a supervising manager never killpg's now-dead pids.
        with contextlib.suppress(OSError):
            Path(f"{socket_path}.backends").unlink()

        # Only tear down the endpoint WE bound. On the EADDRINUSE path a foreign
        # live daemon already owns it (server stays None, transport.remove_stale
        # deliberately refused to remove the live socket) — tearing down here
        # would delete the running daemon's socket and send every stub to
        # per-session fallback. Mirror the ``server.close()`` guard above.
        if server is not None:
            transport.teardown(socket_path)
        # Release the singleton lock (the OS also releases it on process
        # death; this is the clean-path release).
        with contextlib.suppress(OSError):
            os.close(lock_fd)
        logger.info("gatewayd stopped")


async def _retire_task(task: Optional[asyncio.Task[None]]) -> None:
    """Cancel one of ``run_gatewayd``'s background tasks and wait for it to end."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _acquire_backend(
    pool: BackendPool,
    pool_key: PoolKey,
    resolver: TargetResolver,
    *,
    exclusive_stub_uuid: str = "",
    admission: Optional[Admission] = None,
    wait_deadline: Optional[float] = None,
    on_queued: Optional[OnQueued] = None,
    prewarm: bool = False,
) -> tuple[Backend, bool]:
    """Return ``(backend, was_spawned)`` for ``pool_key`` — spawning one via
    the resolver if absent.

    ``was_spawned`` is ``True`` iff THIS call actually created a new
    subprocess (the ``_spawn`` closure ran), ``False`` on a pool reuse. It is
    set inside ``pool.get_or_create`` under the per-key create lock, so it is
    the authoritative, race-free signal of a real spawn — callers can gate a
    spawn-only SEL audit on it without a racy ``pool.get()`` pre-check.

    ``exclusive_stub_uuid`` non-empty routes to a backend bound to that
    connection alone: no reuse lookup, no pooling capacity budget, and released
    when the connection ends. ``was_spawned`` is then always ``True``, because a
    private backend has nothing to reuse by construction.

    ``admission`` (``None`` = ungated, the shape unit tests use) is the
    daemon's :class:`Admission`. With it, a REAL spawn takes, in this order and
    inside the per-key spawn lock after the breaker check: a spawn-gate permit
    (FIFO, bounded by ``wait_deadline`` on the monotonic clock, ``on_queued``
    called every keepalive tick while waiting), a host-budget charge, and -- for
    a pooled backend -- a resident pool slot. Each is released in reverse on any
    failure before the fork.

    The GATE first, and only then the budget: the gate is the one step that
    waits, and a waiter that already held a charge would be charging the host for
    a process that does not exist for as long as it waited -- so a queue of ten
    reaches the ceiling with nothing running, and the eleventh stub is refused
    ``capacity`` on an idle host, which authorises no fallback. The budget answers
    without waiting, so taking it after the permit means it is read against what
    the host carries at the moment of the fork.

    After the fork the charge follows the process
    (released when it is reaped) and the permit follows the first
    ``initialize`` (released by a detached watcher), except under ``prewarm``,
    where the permit settles neutral at once: nothing will initialise a warm
    backend until a stub attaches.

    Raises :class:`_TargetUnknown` when the resolver has no mapping for the
    server (a clean rejection, not a crash); :class:`HostBudgetExhausted`,
    :class:`SpawnGateTimeout`, :class:`SpawnGateClosed` and
    :class:`PoolAtCapacity` from the three admission steps.
    """
    approval_snapshot = await asyncio.to_thread(launch_approval.load_approvals)
    target = await _resolve_target_off_loop(resolver, pool_key, approval_snapshot)
    if target is None:
        raise _TargetUnknown(
            f"no target mapping for server {pool_key.server_name!r}; "
            "set KIROCREW_MCP_TARGET_<SERVER> env var or pass a target_resolver"
        )
    command, args, env, work_dir = target

    was_spawned = False
    label = pool_key.human_readable()
    if prewarm:
        charge_kind = "prewarm"
    elif exclusive_stub_uuid:
        charge_kind = "exclusive"
    else:
        charge_kind = "pooled"

    async def _spawn() -> Backend:
        # Runs only when the pool creates a new backend (guarded by the
        # per-key create lock), so this flag reports a real spawn 1:1.
        nonlocal was_spawned
        was_spawned = True
        # --- admission: permit -> budget -> resident slot, nothing forked yet ---
        # The permit is FIRST because the wait lives there: a charge taken before
        # it would price a process that does not exist for the whole wait, and a
        # queue deep enough exhausts the ceiling with an idle host. The two steps
        # after it answer immediately, so they are read against the host as it is
        # when the fork is about to happen. See this function's docstring.
        charge: Optional[HostCharge] = None
        permit: Optional[Permit] = None
        slot: Any = None
        if admission is not None:
            permit = await admission.gate.acquire(
                label=label, deadline=wait_deadline, on_queued=on_queued
            )
            try:
                charge = admission.budget.reserve(label=label, kind=charge_kind)
                if not exclusive_stub_uuid:
                    slot = await pool.reserve_resident_slot(pool_key)
            except BaseException:
                if charge is not None:
                    charge.release()
                # Our own ceilings, not the host's verdict on a fork: neutral, the
                # same outcome a resident-slot refusal has always recorded.
                permit.settle(OUTCOME_NEUTRAL)
                permit.release()
                raise
        try:
            backend = await _spawn_admitted()
        except BaseException as exc:
            # Strictly the reverse of the acquisition above: the permit is
            # released LAST because releasing it admits the next waiter, which
            # reserves the budget the line before has just given back.
            if slot is not None:
                slot.release()
            if charge is not None:
                charge.release()
            if permit is not None:
                # A fork the OS refused is what congestion looks like from
                # here; a cancellation or any other failure teaches nothing.
                permit.settle(
                    OUTCOME_FAILURE
                    if isinstance(exc, OSError) and not isinstance(exc, asyncio.CancelledError)
                    else OUTCOME_NEUTRAL
                )
                permit.release()
            raise
        if admission is not None and charge is not None and permit is not None:
            admission.track_process(charge, backend.process.wait)
            if prewarm:
                permit.settle(OUTCOME_NEUTRAL)
                permit.release()
            else:
                admission.gate.watch_initialize(
                    permit,
                    init_done=backend._init_done_event,
                    init_state=lambda: backend._init_state,
                    process_exited=backend.process.wait,
                    timeout=backend.initialize_timeout_secs,
                )
        return backend

    async def _spawn_admitted() -> Backend:
        spawn_env = dict(env)
        # Cold-spawn only (never per request), and entirely off the event loop:
        # the flag check reads config and the sidecar read touches the
        # filesystem, either of which would stall gateway traffic and heartbeat
        # processing if done inline after a config invalidation.
        declared = dict(
            await asyncio.to_thread(
                _call_with_approval_snapshot,
                (
                    _declared_env_for_private_backend
                    if exclusive_stub_uuid
                    else _declared_env_to_forward
                ),
                pool_key,
                approval_snapshot,
            )
        )
        declared_path_key = spec_path_key(declared)
        if declared_path_key is not None:
            declared_path = await asyncio.to_thread(
                mcp_search_path,
                declared[declared_path_key],
            )
            # The declared VALUE carries the operator's pin; the variable a
            # child reads is the one the daemon already carries (``PATH`` on
            # POSIX, where ``Path`` is a distinct variable). Writing under the
            # spec's spelling would leave a POSIX backend with no PATH at all.
            declared = {key: value for key, value in declared.items() if key.upper() != "PATH"}
            spawn_env[spec_path_key(spawn_env) or "PATH"] = declared_path
        accepted_temp_keys: tuple[str, ...] = ()
        if declared:
            # A ``secret://`` temp has no path until resolution. Classifying
            # the raw reference can both misjudge URI text as a local path and
            # echo a hostile secret name into this warning. Keep its canonical
            # key provisionally declared; ``spawn_backend`` classifies the
            # resolved value and masks it through ``secret_env_keys``.
            secret_temp_keys = {
                key.upper()
                for key, value in declared.items()
                if key.upper() in CANONICAL_TEMP_KEYS and value.startswith(SECRET_URI_PREFIX)
            }
            checkable_declared = {
                key: value
                for key, value in declared.items()
                if not (key.upper() in CANONICAL_TEMP_KEYS and value.startswith(SECRET_URI_PREFIX))
            }
            accepted_checked, refused, failure = await asyncio.to_thread(
                classify_declared_temp_env,
                checkable_declared,
            )
            accepted_set = set(accepted_checked) | secret_temp_keys
            accepted_temp_keys = tuple(key for key in CANONICAL_TEMP_KEYS if key in accepted_set)
            if refused:
                accepted_temp_keys = ()
                logger.warning(
                    "MCP gateway backend [%s]: ignoring spec-declared %s — %s; "
                    "spawning with the managed temp instead",
                    pool_key.server_name,
                    format_declared_temp_refusals(refused, redactor=redact),
                    "; ".join(
                        declared_temp_refusal_reasons(
                            refused,
                            failure,
                            redactor=redact,
                        )
                    ),
                )
                declared = {
                    key: value
                    for key, value in declared.items()
                    if key.upper() not in CANONICAL_TEMP_KEYS
                }
                spawn_env = {
                    key: value
                    for key, value in spawn_env.items()
                    if key.upper() not in CANONICAL_TEMP_KEYS
                }
            elif accepted_temp_keys:
                declared_temp_values = {
                    key.upper(): value
                    for key, value in declared.items()
                    if key.upper() in accepted_temp_keys
                }
                declared = {
                    key: value
                    for key, value in declared.items()
                    if key.upper() not in CANONICAL_TEMP_KEYS
                }
                declared.update(declared_temp_values)
            # Declared env wins over the daemon's inherited value: the
            # operator wrote it in the agent spec for this server. Safe to
            # let it win because every key here is in the PoolKey, so no
            # co-tenant of this backend declared a different value.
            spawn_env.update(declared)
            logger.info(
                "forwarding %d declared env key(s) to backend %s: %s",
                len(declared),
                pool_key.server_name,
                # Key NAMES only. A private backend forwards secret-bearing keys
                # too, so no value may reach the log.
                ", ".join(sorted(declared)),
            )
        # Resolve secret:// URIs in env values — ephemeral, in-memory only.
        # The sidecar on disk retains the raw URI template; resolution happens
        # at spawn time so values are always fresh from the vault.
        spawn_env, _secret_keys = await asyncio.to_thread(
            resolve_secret_uris,
            spawn_env,
            Path(_config_dir()),
        )
        # Decided BEFORE the child exists, from the command about to be exec'd
        # plus the env and cwd it will run in, and never again: the connection
        # handler reads this flag to decide whether the session token may ride
        # in the caller block. The order is the guarantee: the verdict reads
        # the child's final env and fixed local root before foreign code can
        # run. Off the loop: the check imports ``kiro_crew.agent``, reads config
        # and stats the filesystem, and a cold spawn must not stall gateway
        # traffic or heartbeats.
        denial: list[str] = []
        control_plane = await asyncio.to_thread(
            _spawns_own_control_plane,
            pool_key.server_name,
            command,
            list(args),
            env=spawn_env,
            work_dir=work_dir,
            denial=denial,
        )
        if control_plane:
            # Defense in depth after the verdict: the fence above already
            # rejects declared launcher-control variables and checks the fixed
            # CWD or launcher directory without relying on interpreter-version
            # rules. Control planes only: a third-party backend may rely on the
            # interpreter's default ``sys.path[0]``.
            spawn_env["PYTHONSAFEPATH"] = "1"
            # Per-user site-packages runs code at interpreter startup through
            # its ``.pth`` entries, under any filename -- so inspecting it for a
            # foreign ``kiro_crew`` cannot see a startup hook. Disabling it
            # removes the whole surface, and is safe precisely when nothing
            # legitimate is there to lose: a ``--user`` install keeps user-site
            # enabled because that is where its own package lives, which the
            # fence has already matched against this process's.
            if not _user_site_holds_our_package():
                spawn_env["PYTHONNOUSERSITE"] = "1"
        # Kiro Crew's own UTF-8 pinning, applied after the verdict for every
        # pooled backend. The classifier must see every launcher-injection
        # namespace removed, so these Python keys cannot be present earlier
        # without every control plane denying its own token; and the child must
        # still build its stdio from UTF-8 rather than a Windows ANSI codepage or
        # a hostile inherited encoding. The one constant keeps this site and the
        # gateway's own process environment in step.
        spawn_env.update(_UTF8_PROCESS_ENV)
        # The gate above may have queued this spawn. Re-read the operator's
        # approval at the last await before fork, then resolve the same PoolKey
        # again so both its environment identity and the exact command remain
        # approved. A revoked or changed launch takes the ordinary no-target path.
        fresh_approval_snapshot = await asyncio.to_thread(launch_approval.load_approvals)
        fresh_target = await _resolve_target_off_loop(
            resolver,
            pool_key,
            fresh_approval_snapshot,
        )
        if fresh_target is None or fresh_target[:2] != (command, args):
            raise _TargetUnknown(f"target approval changed for server {pool_key.server_name!r}")
        backend = await spawn_backend(
            pool_key=pool_key,
            command=command,
            args=list(args),
            env=spawn_env,
            work_dir=work_dir,
            # Containment yields only to temp keys cleared by the shared rule.
            # ``spawn_backend`` checks them again after secret resolution, so a
            # path hidden behind ``secret://`` cannot bypass the runtime check.
            declared_temp_keys=accepted_temp_keys,
            secret_env_keys=tuple(_secret_keys),
            **(
                {"initialize_timeout_secs": admission.initialize_timeout_secs}
                if admission is not None
                else {}
            ),
        )
        # Security note: resolved secrets exist ONLY in the local spawn_env
        # dict passed to the child via Popen(env=...).  They are NEVER written
        # to the parent's os.environ, so /proc/<gateway_pid>/environ cannot
        # leak them — the /proc concern is architecturally moot.  The pop
        # below is defense-in-depth: it removes the plaintext from the
        # parent's Python heap once the child has inherited it at exec.
        for _sk in _secret_keys:
            spawn_env.pop(_sk, None)
        backend.control_plane = control_plane
        backend.control_plane_denial = denial[0] if not control_plane and denial else ""
        # Start the stdout pump immediately so replies to the first
        # forwarded message can route back. The task is owned by the
        # Backend and cancelled at shutdown().
        backend._stdout_task = asyncio.create_task(
            backend.run_stdout_pump(),
            name=f"mcp-gateway-backend-stdout-{backend.pid}",
        )
        return backend

    if exclusive_stub_uuid:
        backend = await pool.acquire_exclusive(pool_key, exclusive_stub_uuid, _spawn)
        return backend, was_spawned

    backend = await pool.get_or_create(pool_key, _spawn)
    return backend, was_spawned


def main() -> None:
    """Sync entry point for ``python -m kiro_crew.mcp_gateway.gatewayd``."""
    # Resolve (and, on first launch of an upgraded install, MIGRATE) the data home
    # NOW — synchronously, on the main thread, before the event loop starts below.
    # This is a SEPARATE process entrypoint from cli.main() (the MCP-gateway daemon
    # is spawned directly as ``python -m kiro_crew.mcp_gateway.gatewayd``), so its
    # migration cache starts empty; without this, the first config_dir() would fire
    # lazily on the event loop (e.g. via _zombie_diagnostic_path() or the pool's
    # cfg_dir lookup) and the blocking legacy→~/.kiro/crew migration (copytree +
    # os.walk under a file lock) would freeze the loop and could trip the stall
    # watchdog (no-blocking-call-on-event-loop). Idempotent + process-cached, so
    # every later config_dir() is a cheap lookup; a fresh install with no legacy
    # home just creates the directory.
    from kiro_crew.config.paths import ensure_data_home

    ensure_data_home()
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":
    # ``python -m`` executes this file as ``__main__``, a module object beside the
    # one ``import kiro_crew.mcp_gateway.gatewayd`` creates. The owners under
    # ``daemon`` find the facade by that import name, so the running module is
    # registered under it first: the daemon keeps ONE facade namespace -- the one
    # its own ``run_gatewayd`` writes and its owners read -- and never imports a
    # second copy of this file.
    sys.modules[_FACADE] = sys.modules[__name__]
    main()
