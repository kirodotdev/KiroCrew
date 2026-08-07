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
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shlex
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.loader import config_dir as _config_dir
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.mcp_caller import CallerContext
from kiro_crew.mcp_caller import _parent_pid as _ppid_fn
from kiro_crew.mcp_gateway import credwatch, socketsec, transport
from kiro_crew.mcp_gateway.apps import sweep_spool as apps_sweep_spool
from kiro_crew.mcp_gateway.backend import Backend, BackendGone, spawn_backend
from kiro_crew.mcp_gateway.breaker import CircuitBreaker
from kiro_crew.mcp_gateway.hashing import hash_effective_env, non_secret_env
from kiro_crew.mcp_gateway.manager import _scrub_sensitive_env, is_credential_env_key
from kiro_crew.mcp_gateway.pool import (
    DRAIN_DEADLINE_SECS,
    READ_BUFFER_LIMIT_BYTES,
    BackendPool,
    BackendUnavailable,
    PoolAtCapacity,
    PoolKey,
)
from kiro_crew.mcp_gateway.prewarm import (
    HotKeyStore,
    default_hot_keys_path,
    prewarm_from_payloads,
)
from kiro_crew.mcp_gateway.rewriter import (
    env_sidecar_dir,
    env_sidecar_name,
    forward_declared_env_enabled,
    resolve_overlay_dir,
)
from kiro_crew.mcp_gateway.shutdown_budget import DRAIN_SECS, POOL_SHUTDOWN_SECS
from kiro_crew.mcp_gateway.spill import cleanup_old_spill_files
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.sandbox import warm_backend
from kiro_crew.sel import SecurityEventLog
from kiro_crew.session_pid_sig import read_session_pid_txt

logger = logging.getLogger(__name__)


def _emit_backend_acquire_metric(acquire_ms: float, *, warm: bool) -> None:
    """Emit kirocrew.mcp.backend.acquire.duration (best-effort).

    Shared by the ensure_backend + lazy-spawn paths and their unit tests so the
    metric name / attrs live in production, not duplicated in the test
    (tests must drive real production code).
    """
    try:
        get_recorder().histogram(
            "kirocrew.mcp.backend.acquire.duration",
            acquire_ms,
            unit="ms",
            attrs={"warm": warm},
        )
    except Exception:  # telemetry must never break the gateway hot path
        logger.debug("backend.acquire metric emit failed", exc_info=True)


def _emit_lazy_load_metrics(elapsed_ms: float, *, warm: bool) -> None:
    """Emit MCP lazy-load count + duration (+ backend.acquire), best-effort.

    Shared by the lazy-spawn path and its unit test.
    """
    try:
        rec = get_recorder()
        rec.counter("kirocrew.mcp.lazy_load.count", attrs={"transport": "stdio"})
        rec.histogram(
            "kirocrew.mcp.lazy_load.duration",
            elapsed_ms,
            unit="ms",
            attrs={"transport": "stdio"},
        )
    except Exception:  # telemetry must never break the gateway hot path
        logger.debug("lazy_load metric emit failed", exc_info=True)
    _emit_backend_acquire_metric(elapsed_ms, warm=warm)


# Max bytes accepted for any single stub->gateway frame. Registration
# payloads from the stub are well under 4 KiB; 1 MiB is a very loose cap
# that still guards against a malformed or hostile peer blowing memory
# with ``readuntil(b"\n")``.
_MAX_FRAME_BYTES = READ_BUFFER_LIMIT_BYTES  # 1 MiB; see pool.READ_BUFFER_LIMIT_BYTES

# How long a connection handler waits for the first Register message
# before giving up on an idle client. Keeps the event loop from
# accumulating half-open connections that never send anything.
_REGISTER_TIMEOUT_SECS = 5.0

# Upper bound on a single control/handshake reply's ``drain()`` (pong, stats,
# registered, rejected, ready, forward-error — everything sent via
# ``_write_json_line``). ``_REGISTER_TIMEOUT_SECS`` only bounds the inbound
# first-frame read; without a write bound a same-uid peer that passes the
# handshake then stops reading would pin its handler task for the daemon's
# lifetime. Generous — a peer that cannot accept a small reply in 30s is dead.
_WRITE_REPLY_TIMEOUT_SECS = 30.0

# Graceful-shutdown drain window: how long in-flight tool calls get to finish
# their current JSON-RPC round-trip before gatewayd cancels them and tears down
# the pool. Sourced from the shared budget module so the supervisor's
# SIGTERM→SIGKILL grace is always derived from (and therefore covers) it.
_SHUTDOWN_DRAIN_SECS = DRAIN_SECS

# Interval between per-backend heartbeat sweeps. A backend
# that is gone, or wedged with an in-flight request outstanding past
# ``backend.HEARTBEAT_TIMEOUT_SECS``, is recycled on the next sweep. 60s
# balances recovery latency against ping overhead; the first sweep fires one
# interval after startup so short-lived runs (tests) never trigger it.
_HEARTBEAT_SWEEP_INTERVAL_SECS = 60.0

# Interval between credential-file change probes when one or more
# ``--credential-watch-path`` flags were supplied. On a content change,
# backends spawned with the stale credential are drained (blue-green
# cutover) so they respawn with the refreshed credential. The probe is a
# cheap stat (plus a hash only when mtime moved), so 30s keeps rotation
# latency low without measurable overhead. No flag ⇒ no watcher task.
_CREDENTIAL_WATCH_INTERVAL_SECS = 30.0

# Interval between hot-key persistence flushes when prewarming is enabled.
# Recording a register hit is O(1) in-memory; the actual disk write is
# batched onto this cadence and run via ``asyncio.to_thread`` so the event
# loop never blocks on IO. 30s bounds data loss on a hard kill to one
# interval of observation while keeping write volume negligible.
_HOT_KEYS_FLUSH_INTERVAL_SECS = 30.0

# Interval between warm-pool top-up passes when prewarming is enabled. A
# prewarmed backend can be lost between passes (it died, or was reclaimed under
# capacity pressure despite pinning if the cap was genuinely exhausted), so a
# periodic re-warm restores the hot set without waiting for the next restart.
# The pass is idempotent — a still-present backend is reused, not respawned —
# so this cadence only pays for backends that actually need re-warming. Set
# above the idle timeout so a healthy warm set is not needlessly re-checked too
# often, while still recovering a lost backend well within a few minutes.
_PREWARM_TOPUP_INTERVAL_SECS = 120.0

# Subdirectory under ``$XDG_RUNTIME_DIR`` (or ``/tmp`` fallback) where the
# gateway puts its socket by default. Callers normally supply an explicit
# path via :func:`run_gatewayd`; this default is for tests and ad-hoc runs.
_DEFAULT_SOCKET_SUBDIR = "kirocrew"
_DEFAULT_SOCKET_NAME = "mcp-gateway.sock"

# --- Type aliases -----------------------------------------------------------

#: A ``target_resolver`` takes a :class:`PoolKey` and returns the
#: ``(command, args, env, work_dir)`` tuple used to spawn the backend, or
#: ``None`` if the server is unknown. The default resolver looks up
#: ``KIROCREW_MCP_TARGET_<SERVER>`` env vars (accepts legacy ``MC_MCP_TARGET_<SERVER>``
#: for backward compatibility; matches the Rust PoC and existing
#: rewriter wiring); tests inject their own resolver to avoid env-coupling.
TargetResolver = Callable[
    [PoolKey],
    Optional[tuple[str, list[str], dict[str, str], str]],
]


# --- Public API -------------------------------------------------------------


def _default_cli_socket_path() -> Path:
    """Fallback socket path for the CLI's ``--socket`` argparse default.

    This is used ONLY when ``python -m kiro_crew.mcp_gateway.gatewayd`` is
    invoked without an explicit ``--socket`` flag — a rare operator path,
    typically ad-hoc debugging. The KiroCrew production path always
    derives the socket from ``McpGatewayConfig.socket_path`` / the
    ``default_socket_path()`` in :mod:`kiro_crew.mcp_gateway.rewriter`,
    which returns ``$KIROCREW_HOME/mcp-gateway/gateway.sock``.

    Preference order for this CLI fallback:
    1. ``$XDG_RUNTIME_DIR/kirocrew/mcp-gateway.sock`` when XDG is set.
    2. ``$KIROCREW_HOME``/config-dir ``/mcp-gateway/mcp-gateway.sock``.

    There is deliberately no ``/tmp`` tier. ``XDG_RUNTIME_DIR`` is unset on
    Windows, so a ``/tmp`` fallback would resolve against the current drive and
    have the daemon create a stray ``C:\\tmp`` for its lock file. The data-home
    tier is correct on every platform and matches where production puts the
    endpoint, so the CLI default and the production default now agree on
    everything but the leaf filename.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / _DEFAULT_SOCKET_SUBDIR / _DEFAULT_SOCKET_NAME
    home = os.environ.get("KIROCREW_HOME")
    base = Path(home) if home else _config_dir()
    return base / "mcp-gateway" / _DEFAULT_SOCKET_NAME


async def run_gatewayd(
    socket_path: Path | str,
    *,
    max_backends: int,
    idle_timeout_secs: int,
    stop_event: asyncio.Event,
    target_resolver: Optional[TargetResolver] = None,
    prewarm_count: int = 0,
    credential_watch_paths: Optional[list[Path]] = None,
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

    The function never raises on normal shutdown. Startup failures (e.g.
    socket directory not creatable, another daemon already bound to the
    path) propagate so the caller can surface a clear error.
    """
    socket_path = Path(socket_path)
    # Off the event loop for the same reason as the manager's call: the
    # owner-only step shells out to icacls on Windows. Startup is the least
    # contended moment in this process, but the daemon's signal handlers and
    # supervising ping are already live, so it is offloaded here too.
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
    # Shared circuit breaker keyed by server name: a server
    # that crash-loops on spawn trips OPEN and get_or_create rejects further
    # spawns so the stub falls back to per-session exec instead of churning.
    breaker = CircuitBreaker()
    pool = BackendPool(max_backends=max_backends, breaker=breaker)
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

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        try:
            await _handle_connection(reader, writer, pool, resolver, socket_path, hot_keys)
        except asyncio.CancelledError:
            # Normal on shutdown — propagate for the gather() below.
            raise
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
    diagnostic: Optional[asyncio.Task[None]] = None
    heartbeat: Optional[asyncio.Task[None]] = None
    flush_sweeper: Optional[asyncio.Task[None]] = None
    topup_sweeper: Optional[asyncio.Task[None]] = None
    credential_watchers: list[asyncio.Task[None]] = []
    prewarm_tasks: set[asyncio.Task[None]] = set()
    _prewarm_lock = asyncio.Lock()  # serialize passes so unpin sees latest state

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
        # the record/IO paths are no-ops.
        #
        # The warm set is kept ready by three triggers, all routed through the same
        # idempotent pass (a backend already in the pool is reused by the acquire
        # path, so re-running is cheap and self-healing):
        #   (a) once at startup,
        #   (b) a periodic top-up sweeper that re-warms any hot key whose backend
        #       has since died or been reclaimed under capacity pressure, and
        #   (c) after a credential-cookie refresh, so a freshly-rotated credential is
        #       baked into the warm backends before the next chat attaches.

        async def _run_prewarm_pass(*, initial: bool = False) -> None:
            # Warm the top-N hottest keys through the same acquire path live stubs
            # use. Fully best-effort: any failure leaves the daemon serving lazily.
            #
            # Disk is loaded ONLY on the initial startup pass. Re-loading on every
            # top-up / cookie-rewarm would overwrite the live in-memory tally with
            # the last-flushed snapshot -- regressing hit/miss counters and any keys
            # observed since the last flush (up to one flush interval of loss). The
            # running store already holds the freshest observations, so subsequent
            # passes read straight from memory.
            #
            # Serialized via _prewarm_lock so overlapping passes (startup vs top-up
            # vs cookie-refresh) never race on pin/unpin -- the unpin loop always
            # reflects the most recently warmed set.
            assert hot_keys is not None  # guarded by the caller
            async with _prewarm_lock:
                try:
                    if initial:
                        await asyncio.to_thread(hot_keys.load)
                    payloads = hot_keys.top_register_payloads(prewarm_count)
                    if not payloads:
                        logger.info("prewarm: no hot keys yet — nothing to warm")
                        return

                    async def _acquire(pool_key: PoolKey) -> Backend:
                        # Audit only a REAL spawn (not a pool reuse) so the SEL log
                        # reports actual out-of-handshake subprocess creations 1:1.
                        #
                        # Gate on ``was_spawned`` — set inside the pool's per-key
                        # create lock — NOT a racy ``pool.get()`` pre-check. A
                        # pooled backend can die or be evicted (idle/LRU/heartbeat
                        # sweep, capacity pressure) between a pre-check and the
                        # acquire, turning a "reuse" into a real spawn whose audit
                        # a pre-check would silently skip.
                        backend, was_spawned = await _acquire_backend(pool, pool_key, resolver)
                        if was_spawned:
                            _audit_prewarm_spawn(pool_key.human_readable())
                        return backend

                    await prewarm_from_payloads(
                        payloads,
                        _acquire,
                        limit=prewarm_count,
                        unreserve=pool.unreserve,
                    )

                    # Unpin backends whose key fell out of the current top-N so
                    # the idle sweeper can reclaim them. Prevents unbounded pin
                    # accumulation across hot-set drift and config_snapshot_hash
                    # changes (only the CURRENT top-N stays pinned).
                    current_top_digests = {
                        PoolKey.from_register(p).stable_hash() for p in payloads[:prewarm_count]
                    }
                    for pool_key, backend in await pool.snapshot():
                        if (
                            getattr(backend, "pinned", False)
                            and pool_key.stable_hash() not in current_top_digests
                        ):
                            backend.pinned = False
                except asyncio.CancelledError:
                    raise
                except Exception:  # pragma: no cover -- defensive
                    logger.exception("prewarm pass failed; serving lazily")

        def _schedule_prewarm(*, initial: bool = False) -> None:
            """Fire-and-forget one warm pass, tracked so shutdown can cancel it.
            ``initial=True`` loads persisted hot keys from disk (startup only).
            No-op when prewarming is disabled."""
            if hot_keys is None:
                return
            task = asyncio.create_task(
                _run_prewarm_pass(initial=initial), name="mcp-gateway-prewarm"
            )
            prewarm_tasks.add(task)
            task.add_done_callback(prewarm_tasks.discard)

        if hot_keys is not None:
            flush_sweeper = asyncio.create_task(
                _hot_keys_flush_sweeper(hot_keys, _HOT_KEYS_FLUSH_INTERVAL_SECS, stop_event),
                name="mcp-gateway-hot-keys-flush",
            )
            topup_sweeper = asyncio.create_task(
                _prewarm_topup_sweeper(_schedule_prewarm, _PREWARM_TOPUP_INTERVAL_SECS, stop_event),
                name="mcp-gateway-prewarm-topup",
            )
            # (a) Warm once at startup -- initial=True loads persisted hot keys.
            _schedule_prewarm(initial=True)

        # Credential-rotation drain: on a content change of any watched
        # credential file, drain ALL pooled backends (blue-green cutover) so
        # they respawn with the fresh credential, then re-warm. Watcher tasks
        # exist ONLY when the caller supplied watch paths — the public default
        # (no paths) creates no task and the run flow is byte-identical.
        async def _on_credential_change() -> None:
            await _drain_and_rewarm_on_credential_change(pool, _schedule_prewarm)

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

        if sweeper is not None:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sweeper

        if diagnostic is not None:
            diagnostic.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await diagnostic

        if heartbeat is not None:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat

        if topup_sweeper is not None:
            topup_sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await topup_sweeper

        for watcher in credential_watchers:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
        credential_watchers.clear()

        # Cancel any in-flight warm passes (startup / top-up / credential-triggered)
        # so a slow handshake cannot stall shutdown.
        for task in list(prewarm_tasks):
            task.cancel()
        if prewarm_tasks:
            await asyncio.gather(*prewarm_tasks, return_exceptions=True)
        prewarm_tasks.clear()

        if flush_sweeper is not None:
            flush_sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await flush_sweeper

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


async def _idle_sweeper(
    pool: BackendPool,
    idle_timeout_secs: int,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Periodically drop idle backends from ``pool`` until ``stop_event``
    is set. One sweep per ``interval`` seconds; sweeps themselves are
    non-blocking.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                evicted = await pool.evict_idle(idle_timeout_secs)
                if evicted:
                    logger.debug("idle sweep evicted %d backends", evicted)
            except Exception:  # pragma: no cover — defensive
                logger.exception("idle sweep failed; continuing")
    except asyncio.CancelledError:
        pass


async def _hot_keys_flush_sweeper(
    hot_keys: HotKeyStore,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Persist the hot-key tally once per ``interval`` until ``stop_event``
    is set. The write runs via :func:`asyncio.to_thread` so the blocking
    file IO never stalls the event loop — the on-loop path only ever
    mutates an in-memory dict. A flush that writes nothing (no new hits) is
    a cheap no-op inside :meth:`HotKeyStore.flush`.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly (final flush at shutdown)
            except asyncio.TimeoutError:
                pass
            try:
                wrote = await asyncio.to_thread(hot_keys.flush)
                if wrote:
                    logger.debug("hot-keys: flushed to %s", hot_keys.path)
            except Exception:  # pragma: no cover — defensive
                logger.exception("hot-keys flush failed; continuing")
    except asyncio.CancelledError:
        pass


async def _prewarm_topup_sweeper(
    schedule_prewarm: Callable[[], None],
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Re-warm the hot set once per ``interval`` until ``stop_event`` is set.

    Calls ``schedule_prewarm`` (a fire-and-forget scheduler), which runs an
    idempotent pass: a hot key whose backend is still pooled is reused at no
    cost, and one whose backend has died or been reclaimed is respawned. This
    keeps the warm set populated for the daemon's whole lifetime instead of
    only at startup. The scheduler itself is non-blocking, so the sweeper just
    sleeps between triggers.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                schedule_prewarm()
            except Exception:  # pragma: no cover — defensive
                logger.exception("prewarm top-up scheduling failed; continuing")
    except asyncio.CancelledError:
        pass


async def _drain_and_rewarm_on_credential_change(
    pool: BackendPool,
    schedule_prewarm: Callable[[], None],
) -> None:
    """Handle a credential rotation via blue-green cutover: move ALL
    active backends (including in-use, refcount>0) to the draining list,
    then re-warm fresh backends with the new credential.

    Draining backends continue serving in-flight requests but are invisible
    to new acquires. The heartbeat sweeper reaps them when refcount drops to
    0 or the deadline expires, whichever first. New requests immediately cut
    over to fresh backends spawned with the rotated credential.

    If the drain itself raises, we deliberately skip the re-warm: stale
    backends may still be pooled, and re-warming would reuse + PIN them,
    making them harder to evict next cycle. Skipping leaves recovery to the
    next credential change or the top-up sweeper once they idle out.
    """
    try:
        # First evict truly idle backends (refcount==0) immediately — they
        # have no in-flight work and can be killed outright.
        idle_drained = await pool.evict_idle(0.0, include_pinned=True)
        # Move in-use backends (refcount>0) to the draining list for
        # blue-green cutover — they finish in-flight work then get reaped.
        moved = await pool.drain_all_to_bluegreen()
        logger.info(
            "credential file changed: blue-green cutover — evicted %d idle, "
            "moved %d in-use to draining (deadline=%ds)",
            idle_drained,
            moved,
            int(DRAIN_DEADLINE_SECS),
        )
    except Exception:
        logger.exception("credential-change blue-green cutover failed; skipping re-warm")
        return
    schedule_prewarm()


async def _heartbeat_sweeper(
    pool: BackendPool,
    interval: float,
    stop_event: asyncio.Event,
    backends_pidfile: Optional[Path] = None,
) -> None:
    """Probe stub transports and every pooled backend once per ``interval``,
    until ``stop_event`` is set.

    Two independent responsibilities, in this order:

    1. **Stub transports** (:func:`_probe_stub_transports`) -- write a keepalive
       to every live stub connection. A half-open transport is invisible to the
       parked reader and surfaces only on a write, so without this probe a stub
       that died mid-session never detaches and its backend's refcount never
       reaches 0 -- putting it permanently out of reach of the idle sweep. A
       failed write cancels that stub's handler, whose teardown detaches it.
    2. **Backends** -- :meth:`Backend._heartbeat_once` classifies each one:

    * ``"gone"`` / ``"wedged"`` -- the classify call has already errored every
      attached stub (via ``_broadcast_backend_gone``); the sweeper evicts the
      backend from the pool, shuts it down, and records the death against the
      circuit breaker so a crash loop trips it.
    * ``"alive"`` -- record a healthy signal that closes any OPEN breaker for
      the server.
    * ``"idle"`` -- left untouched; the idle sweeper owns eviction.

    The first sweep fires one full ``interval`` after startup, so short-lived
    runs (tests) never trigger the periodic logic.
    """
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                break  # stop_event fired — exit cleanly
            except asyncio.TimeoutError:
                pass
            try:
                now = time.monotonic()
                # Probe stub transports FIRST. A dead stub detected here
                # detaches on this same sweep, so the backend sweep below and
                # the idle sweep see the corrected refcount immediately rather
                # than one interval late.
                try:
                    await _probe_stub_transports()
                except Exception:  # pragma: no cover — defensive
                    logger.exception("stub transport probe crashed")
                for key, backend in await pool.snapshot():
                    try:
                        state = await backend._heartbeat_once(now)
                    except Exception:  # pragma: no cover — defensive
                        logger.exception("heartbeat probe crashed for %s", key.human_readable())
                        continue
                    if state in ("gone", "wedged"):
                        pool.note_backend_death(key.stable_hash(), now - backend.created_at)
                        evicted = await pool.evict(key, expected=backend)
                        if evicted is not None:
                            with contextlib.suppress(Exception):
                                await evicted.shutdown(timeout=2.0)
                        logger.warning(
                            "heartbeat recycled %s backend pool=%s",
                            state,
                            key.human_readable(),
                        )
                    elif state == "alive":
                        pool.note_backend_healthy(key.stable_hash())
                # Reap draining backends (blue-green cutover) whose refcount
                # hit 0 or whose deadline expired.
                reaped = await pool.reap_draining()
                for backend in reaped:
                    logger.info(
                        "heartbeat reaped draining backend server=%s pid=%s "
                        "refcount=%d (credential-rotation cutover)",
                        backend.pool_key.server_name,
                        backend.pid,
                        backend.refcount,
                    )
                # Persist live backend pids out-of-band so the supervising
                # manager can killpg them if it must SIGKILL a wedged gatewayd
                # (which then never runs pool.shutdown_all()).
                if backends_pidfile is not None:
                    # Offload the file write: it is otherwise a synchronous
                    # open+write+close on the event loop (every other write in
                    # this module — _write_diagnostic, hot_keys.flush, socket
                    # probes — is offloaded via to_thread for the same reason).
                    pids = "\n".join(str(p) for p in pool.live_backend_pids())
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(backends_pidfile.write_text, pids)
            except Exception:  # pragma: no cover — defensive
                logger.exception("heartbeat sweep failed; continuing")
    except asyncio.CancelledError:
        pass


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
    bytes are in the kernel socket buffer and delivery is no longer ours to
    guarantee. So this predicate is complete, not merely one stage deeper.

    ``all_backends()`` deliberately includes DRAINING backends (a blue-green
    credential cutover may be mid-flight), so a restart cannot cut a call a
    draining backend still serves.
    """
    if _active_stub_writes:
        return True
    return any(backend.outstanding_work for backend in pool.all_backends())


def _declared_non_secret_env(pool_key: PoolKey) -> dict[str, str]:
    """Return the FORWARDABLE declared env for ``pool_key``, or ``{}``.

    Reads the ``0600`` sidecar the rewriter wrote for this ``(agent, server)``
    and applies two independent filters:

    1. :func:`hashing.non_secret_env` — drops rotating-secret keys. Those are
       excluded from ``effective_env_hash``, so co-tenants of one backend can
       disagree on their values and no single value is correct to apply.
    2. :func:`manager.is_credential_env_key` — drops every key the daemon's own
       credential scrub removes (``AWS_ACCESS``, ``SSH_AUTH_SOCK``,
       ``GNUPGHOME``, ``GIT_ASKPASS``). This list is broader than (1), so
       forwarding never re-introduces a credential that ``_scrub_sensitive_env``
       deliberately stripped.

    What survives is operator-declared, non-secret, and part of the PoolKey —
    every session sharing this backend agrees on it by construction.

    BLOCKING: reads a file. Callers must run it off the event loop.
    """
    try:
        overlay_dir = resolve_overlay_dir(KiroCrewConfig.load().mcp_gateway.overlay_dir)
    except Exception:
        logger.debug("declared-env: config unreadable; using default overlay dir", exc_info=True)
        overlay_dir = resolve_overlay_dir()
    path = env_sidecar_dir(overlay_dir) / env_sidecar_name(
        pool_key.agent_name, pool_key.server_name
    )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        # No sidecar for this key: the server declared no env. Not an error.
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("declared-env: sidecar %s is not valid JSON; ignoring", path)
        return {}
    if not isinstance(decoded, dict):
        logger.warning("declared-env: sidecar %s is not a JSON object; ignoring", path)
        return {}
    pairs = {str(k): str(v) for k, v in decoded.items() if k}
    # COHERENCE GATE — the invariant that makes forwarding safe must be
    # ENFORCED, not assumed. The stub hashed the sidecar as it read it at ITS
    # start; this read happens later, at cold spawn. An operator editing
    # ``mcpServers.<name>.env`` makes ``rewrite_agents`` rewrite the sidecar
    # while already-running stubs keep their old PoolKey (an adopted daemon can
    # hold such a stub across a gateway restart). A crash/idle-reap respawn
    # would then apply the NEW values to a backend keyed by the OLD hash — so
    # co-tenants would run under configuration they never declared, exactly what
    # the PoolKey partition exists to prevent.
    #
    # Recomputing the hash here and requiring equality closes that window. The
    # construction mirrors the stub's ``_parse_env_json`` (str-coerced keys and
    # values, empty keys dropped) so a coherent sidecar always matches.
    if hash_effective_env(pairs) != pool_key.effective_env_hash:
        logger.warning(
            "declared-env: sidecar for %r no longer matches the PoolKey it was "
            "hashed under (the spec was edited after this session started); "
            "skipping forwarding for this backend",
            pool_key.server_name,
        )
        return {}
    return {
        k: v for k, v in non_secret_env(pairs).items() if not is_credential_env_key(k)
    }


def _declared_env_to_forward(pool_key: PoolKey) -> dict[str, str]:
    """Return the declared env to apply to a cold-spawned backend, or ``{}``.

    Combines the opt-in flag check with the sidecar read so the whole thing is
    ONE blocking unit the caller can hand to a single ``asyncio.to_thread`` —
    both halves read config / touch the filesystem and must stay off the event
    loop. Fails closed: flag off, unreadable config, or unreadable sidecar all
    yield ``{}``.

    BLOCKING: never call this on the event loop.
    """
    if not forward_declared_env_enabled():
        return {}
    return _declared_non_secret_env(pool_key)


def env_target_resolver(pool_key: PoolKey) -> Optional[tuple[str, list[str], dict[str, str], str]]:
    """Look up ``KIROCREW_MCP_TARGET_<SERVER>`` in the process env and return the
    spawn tuple, or ``None`` if no mapping is set.

    Wire format: ``KIROCREW_MCP_TARGET_SLACK_MCP="slack-mcp --stdio"``.
    The server name is upper-cased with ``-`` replaced by ``_``. Env is
    inherited from the gateway process with ``KIROCREW_CHANNEL_ID``
    overlaid when the pool key carries one — this keeps cron / send_message
    fallbacks pointed at the correct channel on a per-pool-key basis.

    Defense-in-depth: env is scrubbed through
    :func:`kiro_crew.mcp_gateway.manager._scrub_sensitive_env` so even if
    the gateway process somehow inherited credential vars, backends won't.
    """
    base = "KIROCREW_MCP_TARGET_" + pool_key.server_name.upper().replace("-", "_")
    # Accept the legacy MC_MCP_TARGET_ prefix for overlays/daemons written by
    # older versions that haven't been regenerated (#928).
    legacy_base = "MC_MCP_TARGET_" + pool_key.server_name.upper().replace("-", "_")
    # Prefer the args-disambiguated entry (written by
    # rewriter._collect_target_env) so two agents that share a server name but
    # declare different --target-args each spawn their OWN backend command,
    # instead of resolving to whichever agent sorted first alphabetically. Fall
    # back to the bare server-name entry for older overlays predating the
    # disambiguated keys.
    spec = (
        os.environ.get(base + "__" + pool_key.command_args_hash)
        or os.environ.get(base)
        or os.environ.get(legacy_base + "__" + pool_key.command_args_hash)
        or os.environ.get(legacy_base)
    )
    if not spec:
        return None
    parts = shlex.split(spec)
    if not parts:
        return None
    command, *args = parts
    env = _scrub_sensitive_env(dict(os.environ))
    # Strip PYTHONPATH/PYTHONHOME so the KiroCrew process's own Python
    # environment doesn't leak into Python-based MCP backends (import conflicts).
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    # No KIROCREW_CHANNEL_ID is exported into the backend env. It used to be
    # copied from PoolKey.channel_id, which only made sense while a backend was
    # owned by one channel. A pooled backend serves several channels, so a
    # single channel baked into its environment at spawn would be actively
    # wrong — it would tell the server it belongs to whichever channel happened
    # to spawn it first. The channel is delivered PER CALL instead, in
    # _meta.kirocrew.caller (see _inject_caller_meta).
    return command, args, env, pool_key.work_dir


# --- Connection handling ----------------------------------------------------


def _audit_peer_denied(reason: str) -> None:
    """Emit a SEL audit event for a denied gateway connection.

    The peer-uid / socket-perms rejection is a security-sensitive access
    decision, so it is recorded in the HMAC-chained security event log
    (:mod:`kiro_crew.sel`) in addition to the WARNING log line. Wrapped
    defensively -- an audit-log failure must never break connection handling.
    The companion :func:`_audit_peer_allowed` records accepted connections,
    so the SEL captures both outcomes of the peer access decision.
    """
    try:
        SecurityEventLog().log_api_access(
            caller="unverified-peer",
            operation="mcp-gateway.connect",
            outcome="denied",
            source="gateway",
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway denial failed", exc_info=True)


def _audit_peer_allowed(caller: str, pool_label: str) -> None:
    """Emit a SEL audit event for an accepted gateway connection.

    Accepting a stub connection is a permission decision just like rejecting
    one, so for a complete access-decision trail it is recorded in the
    HMAC-chained security event log (:mod:`kiro_crew.sel`) alongside the
    denial path. Unlike a denial -- which fires before identity is known and
    is logged as ``unverified-peer`` -- an accept runs after the Register
    handshake, so it carries the real caller identity. It fires once per stub
    connection (at registration), not per request, so the volume sits far
    below the per-tool-call events SEL already records. Wrapped defensively --
    an audit-log failure must never break connection handling.
    """
    try:
        SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.connect",
            outcome="allowed",
            source="gateway",
            resources=pool_label,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway accept failed", exc_info=True)


def _audit_caller_rekey(caller: str, pool_label: str) -> None:
    """Emit a SEL audit event when a stub's caller identity is updated
    mid-connection via a ``recaller`` frame (warm-pool caller repair).

    Re-binding the connection's caller from key-less to a real session
    identity is a security-relevant authorization change: it moves the
    connection from effectively unauthorized (no ``_meta.kirocrew.caller`` on
    forwarded tool calls, so pooled state-mutating tools are refused) to acting
    as a specific session. Recording it in the HMAC-chained SEL gives an
    auditable trail of identity transitions alongside the
    :func:`_audit_peer_allowed` event from the original register — so a stub
    that sends a spoofed recaller claiming another session leaves a record.
    Wrapped defensively -- an audit-log failure must never break connection
    handling.
    """
    try:
        SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.caller-rekey",
            outcome="allowed",
            source="gateway",
            resources=pool_label,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway caller-rekey failed", exc_info=True)


def _audit_recaller_rejected(existing_caller: str, pool_label: str, reason: str) -> None:
    """Emit a SEL audit event when a ``recaller`` frame is REJECTED — either a
    pivot attempt (the connection already carries a session identity) or a
    malformed/empty ``session_key`` claim.

    Rejecting an identity claim is a security-relevant permission decision —
    potentially a compromised or misbehaving stub — so EVERY rejection is
    recorded in the HMAC-chained SEL alongside the accept path
    (:func:`_audit_caller_rekey`), mirroring the :func:`_audit_peer_allowed` /
    :func:`_audit_peer_denied` pairing. ``reason`` describes the rejection (and
    any attempted target) for the trail. Wrapped defensively -- an audit-log
    failure must never break connection handling.
    """
    try:
        SecurityEventLog().log_api_access(
            caller=existing_caller or "unknown",
            operation="mcp-gateway.caller-rekey",
            outcome="denied",
            source="gateway",
            resources=pool_label,
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway recaller reject failed", exc_info=True)


class _StubConn:
    """Mutable per-connection identity holder, indexed by the owning runtime's
    ancestor PID chain so a ``claim`` frame (claim-push) can update the caller
    of every stub connection belonging to a just-claimed warm-pool runtime.

    ``ancestor_pids`` is the stub's parent chain (nearest first) from the
    Register frame. The connection is indexed under EVERY ancestor because
    the PID the gateway names in a claim (``AcpClient._process.pid``) can sit
    several layers above the stub's immediate parent (sandbox wrapper →
    kiro-cli → kiro-cli-chat → stub); indexing a single level was found live
    to make every claim miss.

    ``caller`` starts as the register-time identity (often ``None`` for
    warm-pool stubs) and is replaced by ``recaller`` frames (stub-initiated,
    deny-by-default) or ``claim`` frames (gateway-initiated, replace-allowed).
    Single event loop — no locking needed.
    """

    __slots__ = ("stub_uuid", "ancestor_pids", "pool_label", "caller")

    def __init__(
        self,
        stub_uuid: str,
        ancestor_pids: list[int],
        pool_label: str,
        caller: Optional[CallerContext],
    ) -> None:
        self.stub_uuid = stub_uuid
        self.ancestor_pids = ancestor_pids
        self.pool_label = pool_label
        self.caller = caller


#: Live stub connections indexed by every ancestor PID of the kiro-cli
#: process tree that spawned the stub (``ancestor_pids`` on the Register
#: frame; legacy single ``parent_pid`` accepted). Claim-push looks up this
#: index to retarget every connection of a claimed runtime at once. Entries
#: without usable PIDs (old stubs) are simply not indexed — they keep the
#: recaller-poll fallback.
_CONN_INDEX: dict[int, set[_StubConn]] = {}

# --- Stub-connection liveness probe -----------------------------------------
#
# A stub whose transport dies without a clean close leaves its connection
# handler parked in ``reader.readuntil()``. The handler's ``finally`` — which
# owns ``detach_stub`` — therefore never runs, the backend's refcount never
# drops, and the idle sweep (which keys on ``refcount == 0``) can never reclaim
# it. Backends then accumulate for the lifetime of the daemon.
#
# The asymmetry that makes this possible: a half-open transport is INVISIBLE to
# a reader and only observable on a WRITE. An idle session performs no writes,
# so the death has no way to surface. ``_drain_inbox_to_stub`` already handles
# the write error correctly — it simply never gets a frame to write.
#
# So the gateway writes one itself. Each sweep sends a reserved control frame
# to every live stub; a dead transport fails that write, and the handler is
# cancelled so its existing teardown runs. Reclamation then follows the normal
# refcount path — detach -> refcount 0 -> idle eviction — rather than a
# separate garbage-collection concept layered on top of it.
#
# This mirrors the gateway<->backend direction, which has carried a heartbeat
# under a reserved id since pooling landed. The gateway<->stub direction was
# the half without one.
#
# Reserved ``type`` field, matching the existing ``ping``/``pong`` control
# frames. The stub consumes it in its gateway->stdout pump and never forwards
# it to kiro-cli. An older stub that does not know the frame passes it through,
# where it is inert: it carries no ``jsonrpc``/``id``/``method``, so an MCP
# client has nothing to dispatch on — the same graceful-degradation property
# the ``pong`` frame already relies on.
STUB_KEEPALIVE_TYPE = "keepalive"

#: Bound on a single keepalive write+drain. A stub that has stopped reading
#: must not pin the sweeper: the drain pump uses the same bound for the same
#: reason. Exceeding it is treated as a dead transport.
_STUB_KEEPALIVE_TIMEOUT_SECS = 5.0


class _StubProbe:
    """A live stub connection's write handle plus its owning handler task.

    Registered for the full lifetime of the connection handler and removed in
    the same ``finally`` that detaches the stub, so the registry can never
    outlive the attachment it describes.
    """

    __slots__ = ("stub_uuid", "writer", "task")

    def __init__(
        self,
        stub_uuid: str,
        writer: asyncio.StreamWriter,
        task: "asyncio.Task[None]",
    ) -> None:
        self.stub_uuid = stub_uuid
        self.writer = writer
        self.task = task


#: Every live stub connection, keyed by identity of the probe record. A set of
#: records (not a dict keyed by stub_uuid) because a reconnecting stub may
#: briefly overlap with its predecessor, and clobbering the old entry would
#: leak the very handler the probe exists to tear down.
_STUB_PROBES: set[_StubProbe] = set()


def _stub_probe_add(probe: _StubProbe) -> None:
    _STUB_PROBES.add(probe)


def _stub_probe_discard(probe: _StubProbe) -> None:
    _STUB_PROBES.discard(probe)


async def _probe_stub_transports() -> int:
    """Write a keepalive to every live stub; cancel the handler of any that
    fails. Returns the number of dead transports found.

    The write is the entire point: it converts a silently half-open transport
    into an observable error. Cancelling the handler is what makes the existing
    teardown run — this function deliberately does NOT touch refcounts or the
    pool itself, so there is exactly one code path that detaches a stub.

    Never raises: a probe failure must not take down the sweeper.
    """
    payload = json.dumps({"type": STUB_KEEPALIVE_TYPE}).encode() + b"\n"
    dead = 0
    for probe in list(_STUB_PROBES):
        if probe.task.done():
            # Handler already exiting; its finally owns the teardown.
            continue
        lock = getattr(probe.writer, "_mc_write_lock", None)
        guard: Any = lock if lock is not None else contextlib.nullcontext()
        try:
            with _counted_stub_write():
                async with guard:
                    probe.writer.write(payload)
                    await asyncio.wait_for(
                        probe.writer.drain(),
                        timeout=_STUB_KEEPALIVE_TIMEOUT_SECS,
                    )
        except (ConnectionError, BrokenPipeError, asyncio.TimeoutError) as exc:
            dead += 1
            logger.info(
                "stub %s: transport dead on keepalive (%s) — cancelling handler "
                "so the stub detaches and its backend can be reclaimed",
                probe.stub_uuid or "unknown",
                type(exc).__name__,
            )
            probe.task.cancel()
        except Exception:  # pragma: no cover — defensive
            logger.warning(
                "stub %s: keepalive probe raised unexpectedly",
                probe.stub_uuid or "unknown",
                exc_info=True,
            )
    return dead


def _register_pids(register: dict[str, Any]) -> list[int]:
    """Extract the ancestor PID list from a Register frame.

    Accepts the current ``ancestor_pids`` list and the legacy single
    ``parent_pid`` int. Non-int and out-of-range entries are dropped
    (deny-by-default: garbage never lands in the index).
    """
    raw = register.get("ancestor_pids")
    if not isinstance(raw, list):
        legacy = register.get("parent_pid")
        raw = [legacy] if legacy is not None else []
    return [p for p in raw if isinstance(p, int) and not isinstance(p, bool) and p > 1]


def _conn_index_add(conn: _StubConn) -> None:
    for pid in conn.ancestor_pids:
        _CONN_INDEX.setdefault(pid, set()).add(conn)


def _conn_index_discard(conn: _StubConn) -> None:
    for pid in conn.ancestor_pids:
        conns = _CONN_INDEX.get(pid)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                _CONN_INDEX.pop(pid, None)


def _audit_caller_claimed(
    old_caller: str, new_caller: str, pool_label: str, outcome: str, reason: str = ""
) -> None:
    """Emit a SEL audit event for a ``claim`` frame (claim-push identity set).

    A claim frame re-binds — and unlike ``recaller``, may REPLACE — the caller
    identity of every connection owned by the claimed runtime PID. That is an
    authorization change and is recorded per connection in the HMAC-chained
    SEL, mirroring :func:`_audit_caller_rekey`. The trust basis for allowing
    replacement is the socket itself: it is uid-gated 0700, the same trust
    level that authenticates Register frames. Wrapped defensively — an audit
    failure must never break connection handling.
    """
    try:
        SecurityEventLog().log_api_access(
            caller=new_caller or "unknown",
            operation="mcp-gateway.caller-claim",
            outcome=outcome,
            source="gateway",
            resources=pool_label,
            error=reason or (f"replaced caller={old_caller}" if old_caller else ""),
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway caller-claim failed", exc_info=True)


def _resolve_peer_identity(peer_pid: int) -> tuple[str, list[int]]:
    """Walk the peer's real-PID ancestry (server-side): session key + host chain.

    Runs in gatewayd's own PID namespace (real pids), so it works regardless
    of how the stub sees the world. A single /proc walk returns both:

    * the session_key from the first ancestor with a ``session_pid_<pid>.txt``
      file (``""`` when none matches — normal at register time for a runtime
      that has not been claimed yet), and
    * the full HOST ancestor PID chain (peer first). The register handler
      indexes the stub connection under this chain so a later ``claim`` frame
      — which always carries the runtime's HOST pid — matches even when the
      stub's self-reported ``ancestor_pids`` are namespace-local (sandbox
      PID-namespace topology). Without the host chain in ``_CONN_INDEX`` the
      claim-push silently updates zero connections and the stub stays
      identity-less for life: orphan subagents with empty ``parent_session``
      and undeliverable completion events.

    The walk continues past a session-key match so the chain is complete for
    claim matching at any ancestry level.
    """
    session_key = ""
    chain: list[int] = []
    try:
        cfg_dir = _config_dir()
    except Exception:
        return "", []

    pid = peer_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        chain.append(pid)
        if not session_key:
            # Hardened read (symlink refusal, regular-file check, size
            # bound) — gatewayd is a trusted process reading a predictable,
            # agent-writable path, the exact symlink-planting surface
            # session_pid_sig's reader exists to close.
            try:
                session_key = read_session_pid_txt(pid, cfg_dir)
            except OSError:
                pass
        try:
            pid = _ppid_fn(pid)
        except (OSError, ValueError):
            # Target exited mid-walk (/proc/<pid>/stat gone or malformed).
            break
    return session_key, chain


def _audit_peer_identity_resolved(caller: str, peer_pid: int, stub_uuid: str) -> None:
    """SEL audit: gatewayd granted a key-less stub an identity via the
    SO_PEERCRED + /proc-ancestry mechanism. Granting identity server-side is
    a permission decision — leave a trail. Wrapped defensively; audit failure
    must never break the handshake."""
    try:
        SecurityEventLog().log_api_access(
            caller=caller,
            operation="mcp-gateway.peer-identity-resolved",
            outcome="allowed",
            source="gateway",
            resources=f"peer_pid={peer_pid} stub_uuid={stub_uuid}",
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for peer identity resolution failed", exc_info=True)


def _audit_peer_identity_denied(reason: str, peer_pid: int | None, stub_uuid: str) -> None:
    """SEL audit: a key-less peer whose credentials could not be positively
    attested was refused server-side identity resolution (potential
    unauthorized identity acquisition). Deny arm of
    :func:`_audit_peer_identity_resolved`."""
    try:
        SecurityEventLog().log_api_access(
            caller="unknown",
            operation="mcp-gateway.peer-identity-denied",
            outcome="denied",
            source="gateway",
            resources=f"peer_pid={peer_pid} stub_uuid={stub_uuid} reason={reason}",
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for peer identity denial failed", exc_info=True)


def _apply_claim(frame: dict[str, Any]) -> dict[str, Any]:
    """Apply a ``claim`` frame to every indexed connection of the target PID.

    Returns the ack frame. Validation is deny-by-default: a non-integer or
    out-of-range pid, or an empty/malformed caller, updates nothing and is
    audited as denied. A valid claim REPLACES existing identities (gateway-
    trusted; this is what keeps callers correct across warm-pool re-claims).
    """
    raw_pid = frame.get("pid")
    pid = raw_pid if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else 0
    updated_caller = _caller_from_register(frame)
    if pid <= 1 or updated_caller is None or not updated_caller.session_key:
        reason = f"malformed claim: pid={raw_pid!r} session_key={'' if updated_caller is None else updated_caller.session_key!r}"
        logger.warning("claim rejected: %s", reason)
        _audit_caller_claimed("", "", "pid-index", "denied", reason)
        return {"type": "claim-rejected", "reason": reason}
    conns = _CONN_INDEX.get(pid, set())
    if not conns:
        # A claim naming a pid with NO indexed connection is the exact silent
        # failure that produced orphan subagents (host-pid claim vs
        # namespace-pid index). It can also mean the
        # runtime's stubs disconnected — either way it deserves a loud trail,
        # not a silent {"updated": 0}.
        logger.warning(
            "claim matched ZERO connections: pid=%d session_key=%s — "
            "stub identity will stay stale (possible pid-index mismatch)",
            pid,
            updated_caller.session_key,
        )
        _audit_caller_claimed(
            "",
            updated_caller.session_key,
            "pid-index",
            "noop",
            f"claim pid={pid} matched no indexed connection",
        )
        return {"type": "claim-noop", "updated": 0, "connections": 0}
    updated = 0
    for conn in conns:
        old_key = conn.caller.session_key if conn.caller is not None else ""
        if old_key == updated_caller.session_key:
            continue  # already correct — idempotent re-claim
        conn.caller = updated_caller
        updated += 1
        _audit_caller_claimed(old_key, updated_caller.session_key, conn.pool_label, "allowed")
        logger.info(
            "stub %s claim → session_key=%s type=%s (was %s)",
            conn.stub_uuid,
            updated_caller.session_key,
            updated_caller.session_type,
            old_key or "<none>",
        )
    return {"type": "claimed", "updated": updated, "connections": len(conns)}


def _audit_abort_applied(
    pids: list[int], reason: str, outcome: str, cancelled: int = 0, stubs: int = 0
) -> None:
    """Emit a SEL audit event for an ``abort`` frame (gateway-authoritative
    cancel of in-flight tool calls, with possible backend recycle).

    Cancelling another runtime's in-flight tool work is a security-relevant
    action: it terminates executing tools and may SIGKILL a pooled backend.
    Recorded in the HMAC-chained SEL mirroring :func:`_audit_caller_claimed`.
    Trust basis: the uid-gated 0700 socket, same as Register/Claim. Wrapped
    defensively — an audit failure must never break the abort path.
    """
    try:
        SecurityEventLog().log_api_access(
            caller="gateway",
            operation="mcp-gateway.abort-in-flight",
            outcome=outcome,
            source="gateway",
            resources=f"pids={pids} stubs={stubs}",
            error=f"reason={reason} cancelled={cancelled}" if outcome == "allowed" else reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway abort failed", exc_info=True)


async def _apply_abort(frame: dict[str, Any], pool: "BackendPool") -> dict[str, Any]:
    """Apply an ``abort`` frame: cancel in-flight requests for all stubs under
    the named PIDs.

    This is the gateway-authoritative abort path:
    on session hard-stop, the gateway sends abort for the killed runtime's
    PIDs so gatewayd can propagate MCP cancel notifications to backends.
    Backend recycle happens on the subsequent stub disconnect path, not here.
    """
    raw_pids = frame.get("pids")
    if not isinstance(raw_pids, list):
        _audit_abort_applied([], "missing or invalid pids", "denied")
        return {"type": "abort-rejected", "reason": "missing or invalid pids"}
    pids = [p for p in raw_pids if isinstance(p, int) and not isinstance(p, bool) and p > 1]
    if not pids:
        _audit_abort_applied([], "no valid pids", "denied")
        return {"type": "abort-rejected", "reason": "no valid pids"}
    reason = str(frame.get("reason", "session hard-stop"))

    total_cancelled = 0
    affected_stubs = set()
    for pid in pids:
        conns = _CONN_INDEX.get(pid, set())
        for conn in list(conns):
            affected_stubs.add(conn.stub_uuid)
    # Find backends attached to the affected stubs and cancel their in-flight work
    for backend in pool.all_backends():
        for stub_uuid in affected_stubs:
            cancelled = await backend.cancel_in_flight_for_stub(stub_uuid)
            total_cancelled += len(cancelled)

    logger.info(
        "abort applied: pids=%r reason=%s cancelled=%d stubs=%d",
        pids,
        reason,
        total_cancelled,
        len(affected_stubs),
    )
    _audit_abort_applied(pids, reason, "allowed", total_cancelled, len(affected_stubs))
    return {"type": "aborted", "cancelled": total_cancelled, "stubs": len(affected_stubs)}


def _audit_pool_fallback(caller: str, pool_label: str, reason: str) -> None:
    """Emit a SEL audit event when the gateway directs a stub to fall back to a
    direct, unpooled per-session exec.

    Telling a stub to run its backend outside the pool is an operational
    degradation worth a security-audit trail: a sustained fallback storm (pool
    chronically saturated, or a server repeatedly failing to spawn under the
    jail/pool) is then visible in the HMAC-chained SEL, not just in the stub's
    best-effort jsonl + the pool ``capacity_rejects`` counter. Wrapped
    defensively -- an audit-log failure must never break connection handling.
    """
    try:
        SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.fallback",
            outcome="fallback",
            source="gateway",
            resources=pool_label,
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway fallback failed", exc_info=True)


def _audit_pool_rejected(caller: str, pool_label: str, reason: str) -> None:
    """Emit a SEL audit event for a TERMINAL backend-acquire denial.

    Refusing a stub a backend with no fallback (unknown target, breaker-open on
    the legacy lazy path, or an unexpected gateway-internal error) is a
    permission decision just like the fallback path, so for a complete
    access-decision trail it is recorded in the HMAC-chained SEL alongside
    :func:`_audit_pool_fallback`. Wrapped defensively -- an audit-log failure
    must never break connection handling.
    """
    try:
        SecurityEventLog().log_api_access(
            caller=caller or "unknown",
            operation="mcp-gateway.ensure_backend",
            outcome="denied",
            source="gateway",
            resources=pool_label,
            error=reason,
        )
    except Exception:  # pragma: no cover — audit must never break the handler
        logger.debug("SEL audit emit for gateway reject failed", exc_info=True)


def _audit_prewarm_spawn(pool_label: str) -> None:
    """Emit a SEL audit event for a backend spawned by the warm-pool prewarmer.

    Prewarming spawns a backend subprocess from a PERSISTED hot key, before any
    stub connects, so it bypasses the Register handshake that drives
    :func:`_audit_peer_allowed` on the live path. Spawning from persisted data
    is a distinct security-relevant event (new pid, new time, no live peer to
    attribute), so it gets its own access-decision record in the HMAC-chained
    SEL. ``caller`` is the synthetic ``prewarm`` principal — there is no live
    peer — and the volume is bounded by the prewarm count, far below per-call
    events. Wrapped defensively: an audit-log failure must never abort a warm.
    """
    try:
        SecurityEventLog().log_api_access(
            caller="prewarm",
            operation="mcp-gateway.prewarm-spawn",
            outcome="allowed",
            source="gateway",
            resources=pool_label,
        )
    except Exception:  # pragma: no cover — audit must never break prewarm
        logger.debug("SEL audit emit for prewarm spawn failed", exc_info=True)


async def _handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    pool: BackendPool,
    resolver: TargetResolver,
    socket_path: Path,
    hot_keys: Optional[HotKeyStore] = None,
) -> None:
    """Process one stub connection end-to-end.

    Phases:

    1. **Health probe** (optional): a client may send ``{"type": "ping"}``
       as its first frame. The gateway replies ``{"type": "pong"}`` and
       closes — used by :class:`GatewayManager` to confirm the daemon is
       serving before returning from ``start()``.
    2. **Handshake** (bounded by ``_REGISTER_TIMEOUT_SECS``): read the
       Register message, build the :class:`PoolKey`, reply with a
       Registered envelope containing a provisional ``backend_id``
       (the real backend is spawned lazily on the first MCP message —
       keeps idle stubs from pinning a backend).
    3. **Bridge** (no timeout wrapper — learned correction): stub frames
       go into :meth:`Backend.forward_from_stub`; a concurrent writer
       task drains the stub's inbox queue populated by the backend's
       stdout pump. Exits on any of: stub EOF, backend death, shutdown
       cancellation.
    """
    # Endpoint hardening: deny-by-default peer-principal check on every
    # platform. All three supported platforms can confirm the peer principal
    # (Linux SO_PEERCRED, Windows pipe-client SID comparison, macOS
    # LOCAL_PEERCRED), so any connection that is not a positively-confirmed
    # MATCH is rejected -- a MISMATCH and an UNVERIFIABLE lookup failure both
    # fail closed. The else branch below is now reached only on a POSIX platform
    # with none of those mechanisms, where the principal cannot be read at all
    # and the 0600 socket mode is the only gate available.
    if socketsec.PEER_IDENTITY_SUPPORTED:
        peer_result = socketsec.check_peer_is_self(writer)
        if peer_result is not socketsec.PeerCredResult.MATCH:
            logger.warning(
                "rejecting gateway connection: peer principal not confirmed (%s)",
                peer_result.value,
            )
            _audit_peer_denied(f"peer principal not confirmed ({peer_result.value})")
            return
    else:
        # No principal mechanism on this platform: MISMATCH-enforcing but not
        # UNVERIFIABLE-enforcing, and the asymmetry is deliberate. A positively
        # parsed foreign principal is a real intruder and is refused. A check
        # that merely FAILED must not refuse, because on a platform where the
        # lookup can never succeed that would reject every connection -- the
        # shape of the Windows impersonation defect that denied 100% of them
        # while looking merely strict. So UNVERIFIABLE falls through to the
        # filesystem gate below, which is a real check rather than a shrug: a
        # 0600 socket already prevents any other uid from connecting.
        #
        # macOS used to take this branch. It was promoted into
        # PEER_IDENTITY_SUPPORTED once the macOS CI job proved LOCAL_PEERCRED
        # returns MATCH on real hardware over an accepted socket, with that
        # canary enforced by node id so it cannot silently stop running.
        peer_result = socketsec.check_peer_is_self(writer)
        if peer_result is socketsec.PeerCredResult.MISMATCH:
            logger.warning(
                "rejecting gateway connection: peer principal is a different "
                "user (%s)",
                peer_result.value,
            )
            _audit_peer_denied(f"peer principal mismatch ({peer_result.value})")
            return
        if not socketsec.socket_owner_only(socket_path):
            logger.warning(
                "rejecting gateway connection: peer principal unverifiable on "
                "this platform and socket %s is not owner-only (0600)",
                socket_path,
            )
            _audit_peer_denied(f"peer principal unverifiable and socket not owner-only: {socket_path}")
            return
        logger.debug(
            "peer uid unverifiable on this platform; socket %s verified "
            "owner-only, proceeding on the filesystem gate",
            socket_path,
        )
    register = await _read_first_frame(reader)
    if register is None:
        logger.debug("stub disconnected before first frame")
        return

    # Health-probe short-circuit: any caller can check gatewayd is alive
    # with one round-trip without advertising a PoolKey. GatewayManager
    # uses this to confirm the daemon is serving before returning from
    # ``start()``.
    if register.get("type") == "ping":
        await _write_json_line(writer, {"type": "pong"})
        return

    # Metrics short-circuit: return a point-in-time pool snapshot (backends,
    # sessions, RSS) for the dashboard metrics panel. Read-only, no PoolKey.
    # When prewarming is enabled, fold in the cumulative warm-pool hit tally
    # so the dashboard can show a hit rate; absent (hot_keys is None) the keys
    # simply don't appear and the card omits the metric.
    if register.get("type") == "stats":
        snapshot = await pool.metrics_snapshot_async()
        if hot_keys is not None:
            snapshot.update(hot_keys.hit_stats())
        await _write_json_line(writer, {"type": "stats", **snapshot})
        return

    # Claim-push short-circuit (one-shot control connection from the main
    # gateway process): "session S now owns runtime PID P" — re-target the
    # caller identity of every live stub connection under that PID. This is
    # the event-driven replacement for the stub-side recaller poll, whose
    # bounded budget stranded pool runtimes claimed later than the budget.
    # Trust basis: the unix socket is uid-gated 0700 — the same gate that
    # authenticates Register — so a claim may REPLACE a stale identity
    # (fixes warm-pool re-claim staleness). Validation + auditing live in
    # ``_apply_claim``.
    if register.get("type") == "claim":
        await _write_json_line(writer, _apply_claim(register))
        return

    # Abort-push short-circuit (one-shot control connection from the main
    # gateway process): "cancel all in-flight tool calls for runtime PIDs X"
    # — sends MCP notifications/cancelled to each backend. Backend recycle
    # happens on the subsequent stub disconnect path, not here. Trust basis:
    # same uid-gated 0700 socket as Register/Claim.
    if register.get("type") == "abort":
        await _write_json_line(writer, await _apply_abort(register, pool))
        return

    # App-call short-circuit (one-shot control connection from the dashboard):
    # an embedded MCP App iframe invoking one of its server's app-visible
    # tools. The frame carries only an opaque spool id — the gateway re-reads
    # its own spool record for routing, enforces _meta.ui.visibility, and
    # forwards through the normal stub seam. Trust basis: same uid-gated 0700
    # socket as Register/Claim/Abort. Validation + auditing live in
    # ``app_call.handle_app_call``.
    if register.get("type") == "app-call":
        # circular import: app_call/backend pull gatewayd-adjacent modules, so
        # these stay function-scoped to avoid an import cycle at module load.
        from kiro_crew.mcp_gateway.app_call import _audit, handle_app_call
        from kiro_crew.mcp_gateway.backend import _mcp_apps_enabled

        if not _mcp_apps_enabled():
            # Feature OFF ⇒ byte-identical legacy behavior on EVERY layer: never
            # execute an app-originated tool call, even if a spool capability is
            # still live within its 24h TTL from a window when the flag was on.
            # Audit the denial like every other app-call outcome (same SEL shape
            # as app_call.handle_app_call's allow/deny events).
            _audit(
                "denied", "mcp-apps feature disabled",
                spool_id=str(register.get("spool_id") or ""),
                tool=str(register.get("tool") or ""),
            )
            await _write_json_line(
                writer, {"type": "app-call-rejected", "reason": "mcp-apps feature disabled"}
            )
            return
        await _write_json_line(writer, await handle_app_call(pool, register))
        return

    if register.get("type") not in (None, "register"):
        logger.warning(
            "stub first frame has type=%r, want 'register' or 'ping'",
            register.get("type"),
        )
        return

    try:
        pool_key = PoolKey.from_register(register)
    except ValueError as exc:
        await _write_json_line(
            writer,
            {"type": "rejected", "reason": f"malformed Register: {exc}"},
        )
        logger.warning("rejected Register: %s", exc)
        return

    stub_uuid = str(register.get("stub_uuid", ""))
    if not stub_uuid:
        await _write_json_line(
            writer,
            {"type": "rejected", "reason": "missing stub_uuid"},
        )
        logger.warning("rejected Register: missing stub_uuid")
        return

    caller = _caller_from_register(register)

    # Server-side peer identity: when the stub self-reports an empty
    # session_key, resolve it from the peer's REAL pid (SO_PEERCRED) via a
    # host-side /proc ancestry walk — and capture the host ancestor chain for
    # claim indexing below. Deny-by-default: never grant an identity (nor
    # index host pids) without the kernel positively attesting the peer uid.
    resolved_session_key = ""
    peer_host_pids: list[int] = []
    if caller is None or not caller.session_key:
        peer_pid = socketsec.get_peer_pid(writer)
        peer_uid_ok = socketsec.check_peer_is_self(writer)
        if peer_pid is None or peer_uid_ok is not socketsec.PeerCredResult.MATCH:
            _audit_peer_identity_denied(
                reason=(
                    "no peer pid (SO_PEERCRED unavailable)"
                    if peer_pid is None
                    else f"peer uid not positively verified ({peer_uid_ok.name})"
                ),
                peer_pid=peer_pid,
                stub_uuid=stub_uuid,
            )
        else:
            try:
                # subprocess_executor: a /proc read can block indefinitely on
                # a D-state target; isolate it from the default pools.
                (
                    resolved_session_key,
                    peer_host_pids,
                ) = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(), _resolve_peer_identity, peer_pid
                )
            except Exception:  # graceful degradation: identity stays empty
                logger.exception("peer identity resolution failed for peer_pid=%d", peer_pid)
                resolved_session_key, peer_host_pids = "", []
            if resolved_session_key:
                caller = CallerContext(
                    session_key=resolved_session_key,
                    session_type="peer-resolved",
                    principal_id=str(
                        register.get("principal_id") or register.get("user_identity") or ""
                    ),
                    channel_id=str(register.get("channel_id") or ""),
                    from_gateway=True,
                )
                _audit_peer_identity_resolved(resolved_session_key, peer_pid, stub_uuid)
                logger.info(
                    "peer-resolved session_key for stub %s via peer_pid=%d",
                    stub_uuid,
                    peer_pid,
                )

    # Claim-push index: record the runtime process tree that owns this stub
    # so a ``claim`` frame naming ANY level of that tree re-targets every
    # connection of the claimed runtime. Best-effort — stubs that send no
    # usable PIDs simply keep the recaller-poll fallback.
    #
    # The stub's self-reported ``ancestor_pids`` can be namespace-local
    # (sandbox PID-namespace topology) and then never match a claim frame's
    # HOST pid, so merge in the host-side ancestor chain resolved from the
    # SO_PEERCRED peer pid (empty when peer creds were not positively
    # verified — deny-by-default preserved).
    stub_pids = _register_pids(register)
    indexed_pids = stub_pids + [p for p in peer_host_pids if p not in stub_pids]

    conn = _StubConn(stub_uuid, indexed_pids, pool_key.human_readable(), caller)
    _conn_index_add(conn)

    # Register this connection for the keepalive probe. Scoped to the handler's
    # own task so a dead transport can cancel exactly the coroutine that is
    # parked on the read, letting its finally run the detach.
    _probe: Optional[_StubProbe] = None
    _self_task = asyncio.current_task()
    if _self_task is not None:
        _probe = _StubProbe(stub_uuid, writer, _self_task)
        _stub_probe_add(_probe)

    # Provisional backend_id: the real pid isn't known until the backend
    # spawns. Using the pool digest gives operators a stable grep key that
    # ties together every stub sharing the same backend even before spawn.
    provisional_id = f"pending-{pool_key.stable_hash()[:12]}"
    await _write_json_line(
        writer,
        {
            "type": "registered",
            "backend_id": provisional_id,
            "pool_label": pool_key.human_readable(),
            # Capability advertisement: lets a new stub detect a
            # new gateway and run the ensure_backend pre-flight. Absent on an
            # old gateway, so the new stub skips the pre-flight (no 25s skew
            # penalty) and falls back to the legacy lazy-spawn path.
            #
            # ``bridge_ping`` gates the stub's bridge-phase liveness monitor the
            # same way. It must be negotiated rather than assumed: a daemon that
            # outlived a package upgrade has no ``{"type": "ping"}`` handler, so
            # the frame would fall through to the forward path and no pong would
            # ever return — turning any call slower than the grace window into a
            # forced degrade of a perfectly healthy pooled session.
            "capabilities": ["ensure_backend", "bridge_ping"],
        },
    )
    logger.info(
        "registered stub_uuid=%s pool=%s",
        stub_uuid,
        pool_key.human_readable(),
    )
    # Accepting an identified stub is a permission decision; record it in the
    # SEL alongside the denial path so the audit trail covers both outcomes.
    _audit_peer_allowed(caller.session_key if caller else "", pool_key.human_readable())

    # Warm-pool observation: tally this accepted register so the hottest
    # PoolKeys can be prewarmed on the next startup. In-memory only here —
    # O(1), no IO — so it never slows the handshake; persistence is batched
    # by the flush sweeper. ``None`` when prewarming is disabled.
    if hot_keys is not None:
        hot_keys.record(register)
        # Hit-rate metric: a warm backend already pooled for this key (from a
        # prewarm or a prior chat) is a HIT; otherwise this register will fall
        # through to a lazy spawn below — a MISS. ``get`` is a non-mutating
        # lookup, so reading it here does not pin or alter the backend.
        hot_keys.record_outcome(hit=await pool.get(pool_key) is not None)

    # Bridge phase — ensure any attach is undone even if we bail early.
    backend: Optional[Backend] = None
    inbox: Optional["asyncio.Queue[bytes]"] = None
    writer_task: Optional[asyncio.Task[None]] = None
    # Per-connection write serialization. The outbound pump
    # (_drain_inbox_to_stub) and the forward loop's direct error replies both
    # write to this one StreamWriter; two concurrent writer.drain() calls trip a
    # CPython assert in _drain_helper and tear the transport down. Every
    # write+drain path acquires this lock (looked up off the writer).
    setattr(writer, "_mc_write_lock", asyncio.Lock())
    # Captured ``initialize`` frame for this connection. Stashed the first
    # time kiro-cli sends it so the transparent-respawn path can re-prime a
    # freshly spawned backend (kiro-cli never re-sends initialize after a
    # backend dies). Persists across warm-pool rekey since the stub process
    # — and this coroutine — outlive a single chat.
    captured_init: Optional[dict[str, Any]] = None
    try:
        while True:
            try:
                line = await reader.readuntil(b"\n")
            except asyncio.IncompleteReadError:
                return
            except asyncio.LimitOverrunError:
                logger.warning(
                    "stub %s frame exceeded %d bytes; dropping conn", stub_uuid, _MAX_FRAME_BYTES
                )
                return
            if not line:
                return
            if len(line) > _MAX_FRAME_BYTES:
                logger.warning("stub %s frame too large (%d bytes); dropping", stub_uuid, len(line))
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                logger.warning("stub %s sent non-JSON frame: %s", stub_uuid, exc)
                continue
            if not isinstance(msg, dict):
                logger.warning("stub %s sent non-object frame; dropping", stub_uuid)
                continue
            # Claim-push pickup: a concurrent ``claim`` connection may have
            # re-targeted this connection's identity via ``conn.caller``.
            # Sync per-frame so the very next forward carries the new caller.
            caller = conn.caller
            if msg.get("type") == "unregister":
                logger.info("stub %s sent Unregister; closing", stub_uuid)
                return

            # Warm-pool caller repair: a stub that registered key-less (its
            # kiro-cli was pool-spawned before the session was claimed) sends
            # this once its session key materializes. Update the caller used
            # for subsequent forwards so ``_meta.kirocrew.caller`` carries the
            # real identity — without it, pooled state-mutating tools see an
            # empty session key. Never forwarded to the backend. An empty /
            # malformed key yields ``None`` from ``_caller_from_register`` and
            # is ignored, so a bad recaller can never clobber a good caller.
            if msg.get("type") == "recaller":
                # Deny-by-default: the ONLY permitted transition is a key-less
                # connection adopting a valid session key. Compute the current
                # identity up front, reject every non-permitted case with an
                # explicit ``continue``, and accept only on positive
                # confirmation of that one transition (the final branch) — any
                # unexpected state falls through to rejection, not acceptance.
                # Never forwarded to the backend. Legit warm-pool stubs only
                # ever send a recaller when their Register was key-less, so this
                # never blocks the intended path.
                existing_key = caller.session_key if caller is not None else ""
                if existing_key:
                    # Connection already carries an identity — reject the pivot
                    # (a compromised stub must not re-bind to another session).
                    attempted = _caller_from_register(msg)
                    attempted_key = attempted.session_key if attempted is not None else "<none>"
                    logger.warning(
                        "stub %s sent recaller but caller already set "
                        "(session_key=%s); ignoring",
                        stub_uuid,
                        existing_key,
                    )
                    _audit_recaller_rejected(
                        existing_key,
                        pool_key.human_readable(),
                        f"recaller pivot attempt to session_key={attempted_key}",
                    )
                    continue
                updated = _caller_from_register(msg)
                if updated is None or not updated.session_key:
                    # Empty/malformed identity claim — reject and audit so ALL
                    # recaller outcomes land on the SEL trail, not just pivots.
                    logger.warning(
                        "stub %s sent recaller with no usable session_key; ignoring",
                        stub_uuid,
                    )
                    _audit_recaller_rejected(
                        "",
                        pool_key.human_readable(),
                        "recaller frame with empty/malformed session_key",
                    )
                    continue
                # Positive confirmation: key-less connection + valid recaller
                # key — the one allowed transition. Audit the identity change.
                caller = updated
                conn.caller = updated
                _audit_caller_rekey(caller.session_key, pool_key.human_readable())
                logger.info(
                    "stub %s recaller → session_key=%s type=%s",
                    stub_uuid,
                    caller.session_key,
                    caller.session_type,
                )
                continue

            # Bridge-phase liveness ping: the stub sends ``{"type": "ping"}``
            # while it has outstanding requests to verify the gateway is still
            # responsive. Reply with ``{"type": "pong"}`` — never forwarded
            # to the backend.
            if msg.get("type") == "ping":
                try:
                    await _write_json_line(writer, {"type": "pong"})
                except (OSError, ConnectionError):
                    return
                continue

            # B1 pre-flight: the stub sends ``ensure_backend``
            # before forwarding any real MCP frame. Spawning (or reusing)
            # the backend here — instead of lazily on the first real frame —
            # means a capacity / circuit-breaker rejection reaches the stub
            # BEFORE kiro-cli's ``initialize`` is consumed, so the stub can
            # fall back to a clean per-session exec (the unread ``initialize``
            # is still in its stdin). This control frame is never forwarded
            # downstream to the backend.
            if msg.get("type") == "ensure_backend":
                if backend is None:
                    _acquire_t0 = time.monotonic()
                    try:
                        backend, _was_spawned = await _acquire_backend(pool, pool_key, resolver)
                        # acquire-only duration, captured before the attach_stub
                        # + create_task overhead so the metric stays true to name.
                        _acquire_ms = (time.monotonic() - _acquire_t0) * 1000.0
                    except _TargetUnknown as exc:
                        _audit_pool_rejected(
                            caller.session_key if caller else "",
                            pool_key.human_readable(),
                            str(exc),
                        )
                        await _write_json_line(writer, {"type": "rejected", "reason": str(exc)})
                        return
                    except (BackendUnavailable, PoolAtCapacity) as exc:
                        logger.info(
                            "ensure_backend rejected (fallback-eligible) for %s: %s",
                            pool_key.human_readable(),
                            exc,
                        )
                        _audit_pool_fallback(
                            caller.session_key if caller else "",
                            pool_key.human_readable(),
                            str(exc),
                        )
                        await _write_json_line(
                            writer,
                            {"type": "rejected", "reason": str(exc), "fallback": True},
                        )
                        return
                    except OSError as exc:
                        # Spawn / fork failure (ENOMEM, EAGAIN, ENOENT, or a
                        # jail/pool-specific env mismatch). It may be transient
                        # or specific to the pooled spawn path, so a direct
                        # per-session exec can still succeed -- tag it
                        # fallback-eligible rather than dropping the server's
                        # tools for the whole session.
                        logger.warning(
                            "ensure_backend spawn failed (fallback-eligible) for %s: %s",
                            pool_key.human_readable(),
                            exc,
                        )
                        _audit_pool_fallback(
                            caller.session_key if caller else "",
                            pool_key.human_readable(),
                            f"spawn failed: {exc}",
                        )
                        await _write_json_line(
                            writer,
                            {
                                "type": "rejected",
                                "reason": f"backend spawn failed: {exc}",
                                "fallback": True,
                            },
                        )
                        return
                    except Exception as exc:
                        # Unexpected gateway-internal error (NOT an OS spawn
                        # failure) -- terminal, not fallback-eligible: surface it
                        # rather than masking a gateway bug behind an unpooled
                        # exec on every session.
                        logger.exception(
                            "ensure_backend internal error for %s",
                            pool_key.human_readable(),
                        )
                        _audit_pool_rejected(
                            caller.session_key if caller else "",
                            pool_key.human_readable(),
                            f"internal error: {exc}",
                        )
                        await _write_json_line(
                            writer,
                            {"type": "rejected", "reason": f"internal error: {exc}"},
                        )
                        return
                    # Attach BEFORE replying ``ready`` so the stub can never
                    # forward a frame before its inbox exists.
                    try:
                        inbox = await backend.attach_stub(stub_uuid)
                    finally:
                        # Release the hand-out reservation; once attached
                        # refcount>0 keeps the backend from eviction.
                        pool.unreserve(pool_key)
                    writer_task = asyncio.create_task(
                        _drain_inbox_to_stub(inbox, writer, stub_uuid),
                        name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
                    )
                    # OTEL metric: acquire-only duration (captured above, before
                    # attach_stub + create_task overhead).
                    _emit_backend_acquire_metric(_acquire_ms, warm=not _was_spawned)
                await _write_json_line(writer, {"type": "ready"})
                continue

            # Lazy backend spawn on first forwarded message. The pool
            # dedups concurrent first-attaches so even if two stubs race
            # into this block at the same tick they share one backend.
            if backend is None:
                _lazy_t0 = time.monotonic()
                try:
                    backend, _lazy_was_spawned = await _acquire_backend(pool, pool_key, resolver)
                    # acquire/spawn-only duration, captured before the attach +
                    # create_task overhead.
                    _lazy_elapsed_ms = (time.monotonic() - _lazy_t0) * 1000.0
                except _TargetUnknown as exc:
                    _audit_pool_rejected(
                        caller.session_key if caller else "",
                        pool_key.human_readable(),
                        str(exc),
                    )
                    await _write_json_line(
                        writer,
                        {
                            "type": "rejected",
                            "reason": str(exc),
                        },
                    )
                    return
                except (BackendUnavailable, PoolAtCapacity) as exc:
                    # Legacy lazy-spawn path: only pre-ensure_backend stubs
                    # reach here, and they have already forwarded a real frame,
                    # so a fallback exec would lose it — NOT tagged
                    # fallback-eligible. New stubs pre-flight via ensure_backend.
                    logger.info(
                        "lazy-spawn rejected for %s: %s",
                        pool_key.human_readable(),
                        exc,
                    )
                    _audit_pool_rejected(
                        caller.session_key if caller else "",
                        pool_key.human_readable(),
                        str(exc),
                    )
                    await _write_json_line(
                        writer,
                        {
                            "type": "rejected",
                            "reason": str(exc),
                        },
                    )
                    return
                except Exception as exc:
                    logger.exception("backend spawn failed for %s", pool_key.human_readable())
                    _audit_pool_rejected(
                        caller.session_key if caller else "",
                        pool_key.human_readable(),
                        f"spawn failed: {exc}",
                    )
                    await _write_json_line(
                        writer,
                        {
                            "type": "rejected",
                            "reason": f"backend spawn failed: {exc}",
                        },
                    )
                    return
                try:
                    inbox = await backend.attach_stub(stub_uuid)
                finally:
                    pool.unreserve(pool_key)
                writer_task = asyncio.create_task(
                    _drain_inbox_to_stub(inbox, writer, stub_uuid),
                    name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
                )
                # OTEL metrics: lazy-load count + duration + acquire duration
                # (elapsed captured above, before attach + task overhead).
                _emit_lazy_load_metrics(_lazy_elapsed_ms, warm=not _lazy_was_spawned)

            # Stash the initialize frame so a transparent respawn can re-prime
            # a fresh backend without kiro-cli re-sending initialize.
            if msg.get("method") == "initialize":
                captured_init = dict(msg)

            try:
                await backend.forward_from_stub(stub_uuid, msg, caller=caller)
            except BackendGone as exc:
                # Transparent respawn: a shared backend dying must NOT brick
                # this stub's transport (which would make kiro-cli mark the
                # MCP server dead for the whole session AND poison the warm
                # pool for new tabs). Rebuild a fresh backend, re-prime its
                # handshake from the captured initialize, re-attach this stub,
                # and fail ONLY this one in-flight request with a retryable
                # error. The transport stays open, so the next call self-heals.
                recovered = await _respawn_backend_for_stub(
                    pool,
                    pool_key,
                    resolver,
                    stub_uuid,
                    writer,
                    captured_init,
                    backend,
                    inbox,
                    writer_task,
                )
                if recovered is None:
                    # Genuinely unrecoverable (no captured init, circuit
                    # breaker open / capacity, or prime failed): fall back to
                    # the terminal error so the stub can do a clean
                    # per-session exec rather than churn against a dead server.
                    await _write_json_line(writer, _jsonrpc_error(msg, f"backend gone: {exc}"))
                    return
                backend, inbox, writer_task = recovered
                # Fail only this in-flight request; kiro-cli retries it on the
                # now-healthy transport. A duplicate error for this id from the
                # dying backend's broadcast is harmless — clients dedupe by id.
                if isinstance(msg, dict) and "method" in msg and msg.get("id") is not None:
                    await _write_json_line(
                        writer,
                        _jsonrpc_error(msg, f"backend restarted mid-call, retry: {exc}"),
                    )
                continue
            except Exception as exc:  # pragma: no cover — defensive
                logger.exception("forward_from_stub failed for %s", stub_uuid)
                await _write_json_line(writer, _jsonrpc_error(msg, f"forward failed: {exc}"))
                return
    finally:
        _conn_index_discard(conn)
        if _probe is not None:
            _stub_probe_discard(_probe)
        if backend is not None:
            # Scope A: before detaching, cancel any in-flight tool calls this
            # stub owned — the backend would otherwise run them to completion
            # with no consumer (the root cause of the stop/kill bug).
            # Best-effort: a failure here must never skip detach_stub below,
            # or the backend's refcount leaks and it can never be recycled.
            had_in_flight = any(
                p.stub_uuid == stub_uuid for p in backend._pending_requests.values()
            )
            cancelled: list = []
            try:
                cancelled = await backend.cancel_in_flight_for_stub(stub_uuid)
            except Exception:
                logger.warning(
                    "cancel_in_flight_for_stub failed for %s",
                    stub_uuid,
                    exc_info=True,
                )
            remaining = await backend.detach_stub(stub_uuid)
            if cancelled:
                logger.info(
                    "stub %s detached with %d in-flight request(s) %s -> cancelled; refcount=%d",
                    stub_uuid,
                    len(cancelled),
                    cancelled[:5],
                    remaining,
                )
                # SEL audit: cancelling in-flight tool work on a plain stub
                # disconnect is the same security-relevant action as the abort
                # frame path (which audits via _audit_abort_applied) — record
                # it so a disconnect-triggered cancellation has an audit trail.
                try:
                    SecurityEventLog().log_api_access(
                        caller="gatewayd",
                        operation="mcp-gateway.disconnect-cancel",
                        outcome="cancelled",
                        source="gateway",
                        resources=f"stub={stub_uuid} refcount={remaining}",
                        error=f"cancelled={len(cancelled)} in-flight on stub disconnect",
                    )
                except Exception:  # pragma: no cover — audit must never break detach
                    logger.debug("SEL audit for disconnect-cancel failed", exc_info=True)
            else:
                logger.debug("stub %s detached; refcount=%d", stub_uuid, remaining)
            # Scope B: if no consumers remain and the backend had in-flight
            # work, kill+respawn (the cancel notification is best-effort —
            # the backend may not honour it).
            if remaining == 0 and had_in_flight:
                await backend.recycle_if_idle()
            # Scope B: if quarantined and now drained, recycle
            elif remaining == 0 and backend.quarantined:
                await backend.recycle_if_idle()
        if writer_task is not None:
            writer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await writer_task


async def _acquire_backend(
    pool: BackendPool,
    pool_key: PoolKey,
    resolver: TargetResolver,
) -> tuple[Backend, bool]:
    """Return ``(backend, was_spawned)`` for ``pool_key`` — spawning one via
    the resolver if absent.

    ``was_spawned`` is ``True`` iff THIS call actually created a new
    subprocess (the ``_spawn`` closure ran), ``False`` on a pool reuse. It is
    set inside ``pool.get_or_create`` under the per-key create lock, so it is
    the authoritative, race-free signal of a real spawn — callers can gate a
    spawn-only SEL audit on it without a racy ``pool.get()`` pre-check.

    Raises :class:`_TargetUnknown` when the resolver has no mapping for the
    server (a clean rejection, not a crash).
    """
    target = resolver(pool_key)
    if target is None:
        raise _TargetUnknown(
            f"no target mapping for server {pool_key.server_name!r}; "
            "set KIROCREW_MCP_TARGET_<SERVER> env var or pass a target_resolver"
        )
    command, args, env, work_dir = target

    was_spawned = False

    async def _spawn() -> Backend:
        # Runs only when the pool creates a new backend (guarded by the
        # per-key create lock), so this flag reports a real spawn 1:1.
        nonlocal was_spawned
        was_spawned = True
        spawn_env = dict(env)
        # Cold-spawn only (never per request), and entirely off the event loop:
        # the flag check reads config and the sidecar read touches the
        # filesystem, either of which would stall gateway traffic and heartbeat
        # processing if done inline after a config invalidation.
        declared = await asyncio.to_thread(_declared_env_to_forward, pool_key)
        if declared:
            # Declared env wins over the daemon's inherited value: the
            # operator wrote it in the agent spec for this server. Safe to
            # let it win because every key here is in the PoolKey, so no
            # co-tenant of this backend declared a different value.
            spawn_env.update(declared)
            logger.info(
                "forwarding %d declared env key(s) to backend %s: %s",
                len(declared),
                pool_key.server_name,
                ", ".join(sorted(declared)),
            )
        backend = await spawn_backend(
            pool_key=pool_key,
            command=command,
            args=list(args),
            env=spawn_env,
            work_dir=work_dir,
        )
        # Start the stdout pump immediately so replies to the first
        # forwarded message can route back. The task is owned by the
        # Backend and cancelled at shutdown().
        backend._stdout_task = asyncio.create_task(
            backend.run_stdout_pump(),
            name=f"mcp-gateway-backend-stdout-{backend.pid}",
        )
        return backend

    backend = await pool.get_or_create(pool_key, _spawn)
    return backend, was_spawned


async def _respawn_backend_for_stub(
    pool: BackendPool,
    pool_key: PoolKey,
    resolver: TargetResolver,
    stub_uuid: str,
    writer: asyncio.StreamWriter,
    captured_init: Optional[dict[str, Any]],
    old_backend: Backend,
    old_inbox: Optional["asyncio.Queue[bytes]"],
    old_writer_task: Optional[asyncio.Task[None]],
) -> Optional[tuple[Backend, "asyncio.Queue[bytes]", asyncio.Task[None]]]:
    """Rebuild a fresh backend for ``stub_uuid`` after its shared backend
    died and re-bind this stub to it transparently.

    Returns ``(new_backend, new_inbox, new_writer_task)`` on success, or
    ``None`` when recovery is impossible / undesirable (no captured
    initialize to replay, circuit breaker open, capacity, or the prime
    handshake failed) — the caller then falls back to the terminal error so
    the stub can do a clean per-session exec instead of the gateway churning
    spawns against a broken backend.

    Never re-forwards the in-flight request itself: a ``tools/call`` may have
    executed on the old backend before it died, so replaying it could
    double-execute a non-idempotent tool. The caller fails just that one
    request with a retryable error instead.
    """
    # Stop the old inbox drain first so it cannot race the new writer task
    # onto the same socket, then flush whatever the dying backend already
    # broadcast (errors for other in-flight requests of this stub) so
    # kiro-cli does not hang waiting on those ids.
    if old_writer_task is not None:
        old_writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await old_writer_task
    if old_inbox is not None:
        _lock = getattr(writer, "_mc_write_lock", None)
        _guard: Any = _lock if _lock is not None else contextlib.nullcontext()
        with contextlib.suppress(Exception):
            async with _guard:
                while True:
                    try:
                        payload = old_inbox.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    writer.write(payload)
                # Bounded: a stub that stopped reading during the respawn flush
                # must not pin this handler forever (the outer suppress cannot
                # catch a hang). Mirrors _write_json_line's bounded drain.
                await asyncio.wait_for(writer.drain(), timeout=_WRITE_REPLY_TIMEOUT_SECS)

    with contextlib.suppress(Exception):
        await old_backend.detach_stub(stub_uuid)

    if captured_init is None:
        # Never saw an initialize on this connection — a fresh backend cannot
        # be made usable without replaying it. Give up (terminal).
        logger.info(
            "respawn give-up (no captured initialize) stub=%s pool=%s",
            stub_uuid,
            pool_key.human_readable(),
        )
        return None

    try:
        new_backend, _ = await _acquire_backend(pool, pool_key, resolver)
    except (_TargetUnknown, BackendUnavailable, PoolAtCapacity, OSError) as exc:
        logger.info(
            "respawn give-up (acquire rejected) stub=%s pool=%s: %s",
            stub_uuid,
            pool_key.human_readable(),
            exc,
        )
        return None
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "respawn acquire crashed stub=%s pool=%s",
            stub_uuid,
            pool_key.human_readable(),
        )
        return None

    # _acquire_backend reserved the pool key; release it on every path below
    # (attached -> refcount>0 guards it; bailed -> let the sweeper reclaim it).
    # Without this the reserved digest is skipped by evict_idle/LRU forever,
    # leaking a pool slot for every key that ever mid-call respawned.
    try:
        try:
            await new_backend.prime_initialize(captured_init)
        except BackendGone as exc:
            logger.info(
                "respawn give-up (prime failed) stub=%s pool=%s: %s",
                stub_uuid,
                pool_key.human_readable(),
                exc,
            )
            return None
        new_inbox = await new_backend.attach_stub(stub_uuid)
    finally:
        pool.unreserve(pool_key)
    new_writer_task = asyncio.create_task(
        _drain_inbox_to_stub(new_inbox, writer, stub_uuid),
        name=f"mcp-gateway-stub-writer-{stub_uuid[:8]}",
    )
    logger.info(
        "transparent respawn: stub=%s rebound to fresh backend pid=%s pool=%s",
        stub_uuid,
        new_backend.pid,
        pool_key.human_readable(),
    )
    return new_backend, new_inbox, new_writer_task


async def _drain_inbox_to_stub(
    inbox: "asyncio.Queue[bytes]",
    writer: asyncio.StreamWriter,
    stub_uuid: str = "",
) -> None:
    """Forward every payload queued by the backend into the stub writer.

    Each payload is already a complete newline-terminated JSON frame built
    by :meth:`Backend._deliver_to_stub`. Exits on writer error (stub
    disconnected) or task cancellation at shutdown.
    """
    lock = getattr(writer, "_mc_write_lock", None)
    try:
        while True:
            payload = await inbox.get()
            guard: Any = lock if lock is not None else contextlib.nullcontext()
            try:
                with _counted_stub_write():
                    async with guard:
                        writer.write(payload)
                        await asyncio.wait_for(
                            writer.drain(), timeout=_WRITE_REPLY_TIMEOUT_SECS
                        )
            except (ConnectionError, BrokenPipeError):
                # Scope E: log late responses dropped after stub detach
                # instead of letting BrokenPipeError propagate unlogged.
                logger.info(
                    "stub %s: response arrived after disconnect — dropped "
                    "(%d bytes); this is expected during session stop",
                    stub_uuid or "unknown",
                    len(payload),
                )
                return
            except asyncio.TimeoutError:
                # Stub passed the handshake but stopped reading; don't pin this
                # writer task (and its connection handler + fd) indefinitely.
                return
    except asyncio.CancelledError:
        raise


def _caller_from_register(register: dict[str, Any]) -> Optional[CallerContext]:
    """Build a :class:`CallerContext` from the stub's Register payload.

    The wire format is flexible to support both short and long-lived stubs:

    * Inline ``session_key`` / ``session_type`` / ``principal_id`` /
      ``channel_id`` fields on the Register envelope (tests and the Rust
      stub both use this shape).
    * A nested ``caller`` dict with the same field names — matches the
      Rust ``StubToGateway::Register { caller }`` variant.

    Missing fields default to the empty string. ``from_gateway=True`` is
    forced since this context came through the gateway register path.
    """
    nested = register.get("caller")
    src: dict[str, Any] = nested if isinstance(nested, dict) else register
    session_key = str(src.get("session_key") or src.get("sessionKey") or "")
    if not session_key:
        return None
    return CallerContext(
        session_key=session_key,
        session_type=str(src.get("session_type") or src.get("sessionType") or "unknown"),
        principal_id=str(src.get("principal_id") or src.get("principalId") or ""),
        channel_id=str(src.get("channel_id") or src.get("channelId") or ""),
        from_gateway=True,
    )


def _jsonrpc_error(msg: dict[str, Any], reason: str) -> dict[str, Any]:
    """Return a JSON-RPC 2.0 error envelope mirroring the id of ``msg``.

    Used to close the loop when a backend dies mid-forward: the stub sees
    a plain error response under its own id instead of a dangling request.
    """
    return {
        "jsonrpc": "2.0",
        "id": msg.get("id"),
        "error": {"code": -32000, "message": reason},
    }


class _TargetUnknown(RuntimeError):
    """Resolver returned no mapping — treated as a clean Register rejection
    rather than an internal error."""


async def _read_first_frame(reader: asyncio.StreamReader) -> Optional[dict[str, Any]]:
    """Read the first line-delimited JSON object from ``reader``.

    Returns ``None`` on clean EOF before a full line arrives, on malformed
    JSON, or on idle timeout. The caller dispatches on the ``type`` field:
    ``"ping"`` gets a pong reply, ``"register"`` (or no type) starts the
    handshake, anything else is logged and dropped.
    """
    try:
        line = await asyncio.wait_for(
            reader.readuntil(b"\n"),
            timeout=_REGISTER_TIMEOUT_SECS,
        )
    except asyncio.IncompleteReadError as exc:
        # Peer closed without a newline — treat as clean disconnect only
        # if we received zero bytes; partial frames are truncation errors.
        if exc.partial:
            logger.warning("stub sent partial first frame (%d bytes)", len(exc.partial))
        return None
    except asyncio.TimeoutError:
        logger.warning("stub idle for %.1fs without first frame; closing", _REGISTER_TIMEOUT_SECS)
        return None
    except asyncio.LimitOverrunError:
        logger.warning("stub first frame exceeded %d bytes; closing", _MAX_FRAME_BYTES)
        return None

    if len(line) > _MAX_FRAME_BYTES:
        logger.warning("stub first frame too large: %d bytes", len(line))
        return None

    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("stub first frame not valid JSON: %s", exc)
        return None

    if not isinstance(msg, dict):
        logger.warning("stub first frame not a JSON object: got %s", type(msg).__name__)
        return None
    return msg


async def _write_json_line(writer: asyncio.StreamWriter, obj: Any) -> None:
    """Serialize ``obj`` as one JSON line with a bounded ``drain()``.

    Backpressure (Phase-0 #2): a misbehaving peer that stops reading can
    otherwise let the kernel socket buffer fill silently, deadlocking the
    handler. ``drain()`` yields to the scheduler until the write is
    accepted or the peer's half of the connection drops.

    The drain is bounded by ``_WRITE_REPLY_TIMEOUT_SECS``: ``_REGISTER_TIMEOUT_SECS``
    only wraps the inbound first-frame read, so a same-uid peer that passes the
    handshake then stops reading could otherwise pin this handler task
    indefinitely on the registered/rejected/pong/stats reply.
    """
    payload = json.dumps(obj, separators=(",", ":")).encode("utf-8") + b"\n"
    lock = getattr(writer, "_mc_write_lock", None)
    guard: Any = lock if lock is not None else contextlib.nullcontext()
    async with guard:
        writer.write(payload)
        try:
            await asyncio.wait_for(writer.drain(), timeout=_WRITE_REPLY_TIMEOUT_SECS)
        except (ConnectionError, asyncio.TimeoutError):
            # Peer hung up or stopped reading mid-reply; nothing productive to do.
            return


# --- Utilities --------------------------------------------------------------


# --- Zombie diagnostic ------------------------------------------------------

# Chronic post-M5 issue: gatewayd's accept coroutine has been observed to
# exit silently every ~2-3 h on the dev soak. The existing heartbeat only
# proves the heartbeat task itself is alive; it does not prove the server
# is still accepting connections. The diagnostic task below closes that
# gap: it polls ``server.is_serving()`` and, on divergence from the
# expected "serving while stop_event unset" invariant, dumps a full
# post-mortem to a JSONL side-channel so the next event has a root-cause
# paper trail.

# Interval between diagnostic snapshots. A 30 s sample rate catches the
# ~90 s window between zombie death and watchdog kill without generating
# excessive log volume in the healthy case.
_ZOMBIE_PROBE_INTERVAL_SECS = 30.0


def _zombie_diagnostic_path() -> Path:
    """Return the JSONL file path that receives zombie post-mortems.

    Lives next to the soak/gatewayd logs under
    ``$KIROCREW_HOME/logs/gatewayd_zombie_diagnostic.jsonl`` so a single
    ``tail -f`` follows both heartbeat (gatewayd.log) and any detected
    zombie state.
    """
    return _config_dir() / "logs" / "gatewayd_zombie_diagnostic.jsonl"


def _count_open_fds() -> int:
    """Return the number of open file descriptors (or handles on Windows).

    FD exhaustion is one of the four hypothesised zombie causes; tracking
    the count per snapshot lets us confirm or eliminate that path without
    deploying a separate tracer.

    Platform implementations:
    - Linux: ``/proc/self/fd``
    - macOS/BSD: ``/dev/fd``
    - Windows: ``GetProcessHandleCount`` via ctypes (handle count, not
      fd count — the field documents this as platform-dependent)

    Returns ``-1`` when the platform cannot provide the value.
    """
    # Linux — preferred, most precise.
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        pass

    # macOS / BSD — /dev/fd is a per-process virtual directory.
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        pass

    # Windows — count kernel handles for the current process.
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            handle_count = wintypes.DWORD()
            current_process = kernel32.GetCurrentProcess()
            if kernel32.GetProcessHandleCount(current_process, ctypes.byref(handle_count)):
                return handle_count.value
        except (OSError, AttributeError, ValueError):
            pass

    return -1


def _read_rss_kb() -> int:
    """Return current RSS in kilobytes, or ``-1`` if unavailable.

    Platform implementations:
    - Linux: ``/proc/self/status`` VmRSS field (already in KB).
    - macOS: ``resource.getrusage`` (``ru_maxrss`` is in bytes on macOS).
    - Other POSIX: ``resource.getrusage`` (``ru_maxrss`` is in KB on Linux,
      but this branch only runs on non-Linux where it is bytes; if neither
      applies we fall through to ``-1``).
    - Windows: ``GetProcessMemoryInfo`` via ctypes (WorkingSetSize in bytes).

    Returns ``-1`` when the platform cannot provide the value.
    """
    # Linux — parse VmRSS from /proc/self/status (current RSS, not peak).
    try:
        with open("/proc/self/status", "r", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass

    # macOS / other POSIX — resource.getrusage gives ru_maxrss.
    # On macOS ru_maxrss is in bytes; on other BSDs it is in KB.
    if sys.platform != "win32":
        try:
            import resource

            ru = resource.getrusage(resource.RUSAGE_SELF)
            maxrss = ru.ru_maxrss
            if maxrss > 0:
                if sys.platform == "darwin":
                    return maxrss // 1024  # bytes → KB
                return maxrss  # already in KB on most other POSIX
        except (OSError, ValueError, AttributeError, ImportError):
            pass

    # Windows — GetProcessMemoryInfo returns WorkingSetSize in bytes.
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wintypes

            SIZE_T = ctypes.c_size_t

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", SIZE_T),
                    ("WorkingSetSize", SIZE_T),
                    ("QuotaPeakPagedPoolUsage", SIZE_T),
                    ("QuotaPagedPoolUsage", SIZE_T),
                    ("QuotaPeakNonPagedPoolUsage", SIZE_T),
                    ("QuotaNonPagedPoolUsage", SIZE_T),
                    ("PagefileUsage", SIZE_T),
                    ("PeakPagefileUsage", SIZE_T),
                ]

            psapi = ctypes.WinDLL("psapi", use_last_error=True)  # type: ignore[attr-defined]
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            handle = kernel32.GetCurrentProcess()
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize // 1024  # bytes → KB
        except (OSError, AttributeError, ValueError):
            pass

    return -1


def _collect_task_stacks() -> list[dict[str, Any]]:
    """Snapshot every live asyncio task with name + current stack.

    Used on zombie detection — gives the post-mortem enough context to
    tell whether a specific coroutine (backend pump, stub handler, idle
    sweeper) wedged the event loop versus an external cause (FD leak,
    blocking syscall, etc.).
    """
    out: list[dict[str, Any]] = []
    for task in asyncio.all_tasks():
        frames: list[str] = []
        try:
            for frame in task.get_stack(limit=10):
                frames.append(
                    "{}:{} in {}".format(
                        frame.f_code.co_filename,
                        frame.f_lineno,
                        frame.f_code.co_name,
                    )
                )
        except Exception:  # pragma: no cover — defensive
            frames = ["<stack unavailable>"]
        out.append(
            {
                "name": task.get_name(),
                "done": task.done(),
                "cancelled": task.cancelled(),
                "stack": frames,
            }
        )
    return out


def _snapshot_state(
    *,
    server: Optional[transport.TransportServer],
    pool: BackendPool,
    connections: set[asyncio.Task[None]],
    task_count: int,
) -> dict[str, Any]:
    """Gather a single health sample used by the diagnostic loop."""
    is_serving: Optional[bool]
    try:
        is_serving = bool(server.is_serving()) if server is not None else None
    except Exception:
        is_serving = None
    return {
        "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ts_epoch": time.time(),
        "pid": os.getpid(),
        "is_serving": is_serving,
        "task_count": task_count,
        "fd_count": _count_open_fds(),
        "rss_kb": _read_rss_kb(),
        "pool_size": len(pool._backends),  # type: ignore[attr-defined]
        "connections_in_flight": len(connections),
    }


def _write_diagnostic(path: Path, record: dict[str, Any]) -> None:
    """Append one JSONL line to the diagnostic side-channel.

    Never raises — the diagnostic task is defensive enough that a missing
    directory or EROFS on the log volume must not crash gatewayd itself.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except OSError as exc:  # pragma: no cover — defensive
        logger.warning("zombie diagnostic write failed: %s", exc)


async def _zombie_diagnostic(
    server: transport.TransportServer,
    pool: BackendPool,
    connections: set[asyncio.Task[None]],
    stop_event: asyncio.Event,
) -> None:
    """Polling watchdog that captures accept-loop death.

    Every :data:`_ZOMBIE_PROBE_INTERVAL_SECS` seconds:

    1. Collect a health snapshot via :func:`_snapshot_state`.
    2. Append the snapshot to the diagnostic JSONL under the ``probe`` tag
       so there is a continuous baseline to correlate against.
    3. If ``server.is_serving()`` is ``False`` while ``stop_event`` is
       still unset, the accept loop has died silently — dump every live
       task stack, log at error level, and set ``stop_event`` so the
       process exits cleanly and the watchdog respawns us.
    """
    diag_path = _zombie_diagnostic_path()
    try:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=_ZOMBIE_PROBE_INTERVAL_SECS)
                return  # stop_event fired — clean exit
            except asyncio.TimeoutError:
                pass

            # asyncio.all_tasks() must be read ON the loop (it needs the
            # running loop); capture it here before offloading the blocking
            # /proc walk — calling it inside the worker thread raises
            # RuntimeError and would kill this watchdog on its first probe.
            task_count = len(asyncio.all_tasks())
            snap = await asyncio.to_thread(
                _snapshot_state,
                server=server,
                pool=pool,
                connections=connections,
                task_count=task_count,
            )
            snap["tag"] = "probe"
            await asyncio.to_thread(_write_diagnostic, diag_path, snap)

            if snap["is_serving"] is False and not stop_event.is_set():
                snap["tag"] = "zombie_detected"
                snap["tasks"] = _collect_task_stacks()
                snap["traceback"] = traceback.format_stack()
                await asyncio.to_thread(_write_diagnostic, diag_path, snap)
                logger.error(
                    "zombie gatewayd detected: is_serving=False while stop_event unset; "
                    "tasks=%d fd=%d rss_kb=%d — diagnostic dumped to %s; setting stop_event",
                    snap["task_count"],
                    snap["fd_count"],
                    snap["rss_kb"],
                    diag_path,
                )
                stop_event.set()
                return
    except asyncio.CancelledError:
        pass


# --- CLI entry point --------------------------------------------------------


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mc-mcp-gatewayd",
        description="KiroCrew MCP gateway daemon — pools MCP backends across sessions",
    )
    p.add_argument(
        "--socket",
        dest="socket",
        default=str(_default_cli_socket_path()),
        help="Unix socket path to bind. Default: $XDG_RUNTIME_DIR/kirocrew/mcp-gateway.sock",
    )
    p.add_argument(
        "--max-backends",
        dest="max_backends",
        type=int,
        default=20,
        help="Maximum concurrent backend subprocesses. LRU-evicted beyond this.",
    )
    p.add_argument(
        "--idle-timeout-secs",
        dest="idle_timeout_secs",
        type=int,
        default=300,
        help="Seconds an unattached backend is kept before the idle sweeper drains it.",
    )
    p.add_argument(
        "--prewarm-count",
        dest="prewarm_count",
        type=int,
        default=0,
        help="Number of hottest observed (agent x server x channel) backends to "
        "spawn at startup, before the first stub connects, to remove the "
        "cold-after-restart new-chat latency. 0 (default) disables prewarming.",
    )
    p.add_argument(
        "--credential-watch-path",
        dest="credential_watch_paths",
        action="append",
        default=[],
        metavar="PATH",
        help="Credential file to watch for content changes (repeatable). On a "
        "real rotation, all pooled backends are drained via a blue-green "
        "cutover and respawned with the fresh credential. No flag "
        "(default) disables the watcher entirely.",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default=os.environ.get("MC_GATEWAYD_LOG", "INFO"),
        help="Python logging level (DEBUG, INFO, WARNING, ...).",
    )
    return p


async def _amain(argv: Optional[list[str]] = None) -> int:
    args = _build_argparser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()

    # Catch exceptions that slip past per-task handlers — e.g. a
    # fire-and-forget coroutine that blows up without ``await``. Without
    # this hook they get logged through asyncio's default handler only
    # if the task is awaited; zombie modes have been traced to exactly
    # this path.
    def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        exc = context.get("exception")
        msg = context.get("message", "unhandled event loop error")
        if exc is not None:
            logger.error("gatewayd event-loop exception: %s", msg, exc_info=exc)
        else:
            logger.error("gatewayd event-loop error: %s | context=%r", msg, context)

    loop.set_exception_handler(_loop_exception_handler)

    # Heartbeat: emit a line every 60s so a silent stdout stream becomes
    # visible proof that the daemon has zombified. Also logs pool stats
    # to give shape to load growth between heartbeats.
    async def _heartbeat() -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                logger.info("gatewayd heartbeat: alive, stop_event=unset")
            except asyncio.CancelledError:
                return

    hb_task = asyncio.create_task(_heartbeat(), name="mcp-gateway-heartbeat")

    # Fill the sandbox probe cache BEFORE any backend is spawned on-loop. See the
    # matching site in slack/gateway.py: the wait is what makes the guarantee
    # hold, since a fire-and-forget prewarm leaves the first spawn racing the
    # warm thread and reading a cold-cache transient as "no sandbox backend".
    try:
        await asyncio.to_thread(warm_backend)
    except RuntimeError:
        logger.warning("sandbox warm_backend skipped (thread exhaustion); cache stays cold")

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    try:
        await run_gatewayd(
            args.socket,
            max_backends=args.max_backends,
            idle_timeout_secs=args.idle_timeout_secs,
            stop_event=stop_event,
            prewarm_count=args.prewarm_count,
            credential_watch_paths=[Path(p) for p in args.credential_watch_paths],
        )
    except Exception:
        logger.exception("gatewayd exited with unhandled exception")
        return 1
    finally:
        hb_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb_task
    return 0


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
    main()
