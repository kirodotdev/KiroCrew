"""PoolKey + BackendPool — the correctness-critical sharing boundary.

Two sessions sharing a single backend MUST produce the same answers as if
each had its own backend. Every attribute that changes backend behavior
MUST be in :class:`PoolKey`, or two sessions can see cross-tenant state.

The 14 dimensions captured below are the union of every spawn-time input
that influences a Kiro MCP subprocess: identity (``server_name``,
``agent_name``), execution (``command_args_hash``, ``effective_env_hash``,
``work_dir``, ``binary_version``), security (``os_uid``, ``sandbox_mode``,
``autoapprove_set_hash``, ``approval_mode``, ``trust_all_tools``,
``user_identity``), tenancy (``channel_id``), and config drift
(``config_snapshot_hash``).

Stable hashing uses SHA-256 over a JSON-serialized tuple with sorted keys.
Python's built-in ``hash()`` is intentionally non-deterministic across
processes (``PYTHONHASHSEED``) so it cannot be used for pool identity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping, Optional

from kiro_crew.mcp_gateway.breaker import CircuitBreaker

if TYPE_CHECKING:
    from kiro_crew.mcp_gateway.backend import Backend

logger = logging.getLogger(__name__)


# Per-stream byte ceiling for ``readuntil(b"\n")`` across the gateway.
# Passed as ``limit=`` to every asyncio reader the module creates
# (subprocess pipes, unix sockets). Asyncio's stdlib default is 64 KiB,
# which is below observed MCP response sizes (~100 KiB for typical
# tool-call results). 1 MiB matches the declared frame ceiling in
# ``gatewayd.py``; lines beyond this are still rejected loudly, but the
# asyncio reader no longer chokes on legitimate payloads first.
# Per-frame read/response cap for a POOLED backend (1 MiB). This bounds the
# shared daemon's RSS against a pathological backend, but it also means a
# pooled server whose response legitimately exceeds 1 MiB (e.g. a full-page
# browser snapshot or a large file read) has that response dropped — whereas
# the same server run UN-pooled (per-session exec) has no such cap. Poolability
# is opt-in, so the guidance is: do NOT pool servers that routinely emit
# >1 MiB responses; leave them non-poolable and they run per-session uncapped.
# Kept as a documented limitation rather than raised, to preserve the
# shared-daemon memory bound.
READ_BUFFER_LIMIT_BYTES = 1 << 20  # 1 MiB


# Upper bound on processes walked when summing a backend's subtree RSS. A
# pooled MCP backend's real tree is tiny (parent shim + a handful of workers);
# the cap only guards against a pathological/looping /proc graph.
_RSS_SUBTREE_MAX_PROCS = 256


def _single_proc_rss_kb(pid: int) -> int:
    """RSS (KiB) of a single ``pid`` from /proc/<pid>/status, or -1."""
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return -1


def _proc_children(pid: int) -> list[int]:
    """Direct child PIDs of ``pid`` via /proc/<pid>/task/<tid>/children.

    Uses the kernel-provided children list (CONFIG_PROC_CHILDREN), so no
    ``pgrep``/full-table scan. Returns ``[]`` if the file is unavailable.
    """
    kids: list[int] = []
    task_dir = f"/proc/{pid}/task"
    try:
        tids = os.listdir(task_dir)
    except OSError:
        return kids
    for tid in tids:
        try:
            with open(f"{task_dir}/{tid}/children", encoding="ascii") as fh:
                kids.extend(int(tok) for tok in fh.read().split())
        except (OSError, ValueError):
            continue
    return kids


def _proc_rss_kb(pid: Optional[int]) -> int:
    """Resident set size (KiB) for ``pid`` **and all its descendants**.

    A pooled MCP backend is frequently a thin launcher (e.g.
    a ``slack-mcp`` launcher or an ``example-mcp`` shim) whose real
    memory lives in a child process. Counting only ``pid``'s own ``VmRSS``
    under-reports the true footprint by ~30x, so we sum the whole subtree.

    Returns -1 if ``pid`` is falsy or its own status cannot be read; otherwise
    the summed KiB (descendants that vanish mid-walk are simply skipped, so the
    result degrades gracefully to parent-only when ``children`` is unreadable).
    """
    if not pid:
        return -1
    own = _single_proc_rss_kb(pid)
    if own < 0:
        return -1
    total = own
    seen = {pid}
    frontier = [pid]
    while frontier and len(seen) < _RSS_SUBTREE_MAX_PROCS:
        nxt: list[int] = []
        for parent in frontier:
            for child in _proc_children(parent):
                if child in seen:
                    continue
                seen.add(child)
                kb = _single_proc_rss_kb(child)
                if kb > 0:
                    total += kb
                nxt.append(child)
        frontier = nxt
    return total


# --- PoolKey ----------------------------------------------------------------


@dataclass(frozen=True)
class PoolKey:
    """Immutable identity of a poolable MCP backend.

    Fields are ordered so the dataclass repr is stable and grep-friendly in
    logs. Content-hashed fields use the ``_hash`` suffix and carry SHA-256
    hex digests of the underlying structure so two keys only collide when
    the inputs are semantically equal, not by coincidence.
    """

    # Identity
    server_name: str
    agent_name: str

    # Execution shape
    command_args_hash: str
    effective_env_hash: str
    work_dir: str
    binary_version: str

    # Security boundary
    os_uid: int
    sandbox_mode: str
    autoapprove_set_hash: str
    approval_mode: str
    trust_all_tools: bool
    user_identity: str

    # Tenancy + config drift
    channel_id: Optional[str]
    config_snapshot_hash: str

    # --- Constructors ------------------------------------------------------

    @classmethod
    def from_register(cls, register: Mapping[str, Any]) -> "PoolKey":
        """Build a :class:`PoolKey` from a stub's ``Register`` payload.

        The caller is responsible for providing pre-computed content hashes
        for the structured fields (command_args, env, auto-approve,
        config_snapshot). This mirrors the Rust stub's ``build_pool_key``
        helper: the stub has the raw inputs and knows how to hash them, the
        gateway just validates and stores.

        Raises :class:`ValueError` on missing or malformed fields.
        """
        missing = [f.name for f in cls.__dataclass_fields__.values()  # type: ignore[attr-defined]
                   if f.name != "channel_id" and f.name not in register]
        if missing:
            raise ValueError(f"Register payload missing required fields: {missing}")

        channel_id = register.get("channel_id")
        if channel_id is not None and not isinstance(channel_id, str):
            raise ValueError(f"channel_id must be str or None, got {type(channel_id).__name__}")
        if isinstance(channel_id, str) and not channel_id:
            channel_id = None  # empty string ⇒ no channel

        # Security-boundary dims: type-check rather than coerce. bool("false")
        # is True and int() on a bool silently passes, so a stub sending a JSON
        # string/number for these could land in the wrong trust/uid partition.
        # Reject a non-matching type (mirrors the channel_id check above).
        os_uid = register["os_uid"]
        if isinstance(os_uid, bool) or not isinstance(os_uid, int):
            raise ValueError(f"os_uid must be int, got {type(os_uid).__name__}")
        trust_all_tools = register["trust_all_tools"]
        if not isinstance(trust_all_tools, bool):
            raise ValueError(
                f"trust_all_tools must be bool, got {type(trust_all_tools).__name__}"
            )

        try:
            return cls(
                server_name=str(register["server_name"]),
                agent_name=str(register["agent_name"]),
                command_args_hash=str(register["command_args_hash"]),
                effective_env_hash=str(register["effective_env_hash"]),
                work_dir=str(register["work_dir"]),
                binary_version=str(register["binary_version"]),
                os_uid=os_uid,
                sandbox_mode=str(register["sandbox_mode"]),
                autoapprove_set_hash=str(register["autoapprove_set_hash"]),
                approval_mode=str(register["approval_mode"]),
                trust_all_tools=trust_all_tools,
                user_identity=str(register["user_identity"]),
                channel_id=channel_id,
                config_snapshot_hash=str(register["config_snapshot_hash"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Register payload has malformed field: {exc}") from exc

    # --- Hashing -----------------------------------------------------------

    def stable_hash(self) -> str:
        """Deterministic SHA-256 hex digest suitable as a dict key across
        processes.

        Built from ``json.dumps(asdict(self), sort_keys=True)``: sorting
        fixes key order, ``asdict`` recursively unwraps the dataclass, and
        SHA-256 is collision-resistant for security-identity purposes.
        """
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def human_readable(self) -> str:
        """Short log-friendly label. NOT collision-resistant — use
        :meth:`stable_hash` as the actual pool dict key.
        """
        cmd_short = (self.command_args_hash[:8] + "…") if len(self.command_args_hash) > 8 else self.command_args_hash
        env_short = (self.effective_env_hash[:8] + "…") if len(self.effective_env_hash) > 8 else self.effective_env_hash
        chan = f" chan={self.channel_id}" if self.channel_id else ""
        return (
            f"{self.agent_name}:{self.server_name} "
            f"uid={self.os_uid} sbx={self.sandbox_mode} "
            f"cmd={cmd_short} env={env_short} ws={self.work_dir}{chan}"
        )


# --- BackendPool skeleton ---------------------------------------------------


class BackendUnavailable(RuntimeError):
    """Raised by :meth:`BackendPool.get_or_create` when the circuit breaker
    is OPEN for a server, i.e. the backend has been crashing on spawn. The
    connection handler turns this into a clean ``rejected`` reply so the stub
    falls back to a per-session exec instead of churning the spawn loop."""


class PoolAtCapacity(RuntimeError):
    """Raised by :meth:`BackendPool.add` when the pool is full and no idle
    backend can be evicted to make room (every slot is actively attached).
    The connection handler turns this into a ``rejected`` reply tagged
    ``fallback: true`` so the stub runs the real backend directly (unpooled)
    for that session instead of dropping the server's tools for the whole
    session."""


class BackendPool:
    """Collection of running backends keyed by :meth:`PoolKey.stable_hash`.

    Milestone 1 provides the minimum surface gatewayd needs to compile:
    add/get/evict/shutdown_all. Idle-timeout sweeping, LRU eviction under
    capacity pressure, and refcount-aware drain are deferred to
    Milestone 2.

    All methods assume single-threaded access from the asyncio event loop
    and serialize mutations through ``_lock``. Attempting to share the
    pool across loops or threads is a usage error.
    """

    def __init__(
        self,
        max_backends: int,
        *,
        breaker: Optional[CircuitBreaker] = None,
    ) -> None:
        if max_backends < 1:
            raise ValueError(f"max_backends must be >= 1, got {max_backends}")
        self._max_backends = max_backends
        # Optional shared circuit breaker keyed by ``server_name``. ``None``
        # disables breaking entirely (the default, so existing call sites and
        # tests keep their behavior); ``run_gatewayd`` injects a live breaker.
        self._breaker = breaker
        self._backends: dict[str, "Backend"] = {}
        self._lock = asyncio.Lock()
        # Per-digest spawn locks dedupe concurrent first-attach for the same
        # PoolKey so the pool only ever spawns one backend per key even when
        # two stubs register at the exact same tick. Entries are cleaned up
        # when the corresponding backend is evicted from ``_backends``.
        self._spawn_locks: dict[str, asyncio.Lock] = {}
        # Digests of backends handed out by get_or_create/get that the caller
        # has not yet attached via ``attach_stub``. A reserved backend MUST
        # NOT be selected by the idle or LRU sweeper even though its refcount
        # is still 0. Callers MUST call :meth:`unreserve` after attaching (or
        # if they bail without attaching).
        # Per-digest reservation refcount (not a set): concurrent reservers of
        # the same key must not collapse, or one caller's unreserve would drop
        # another's eviction protection. A digest is reserved iff count > 0.
        self._reserved_digests: dict[str, int] = {}
        # Strong refs to fire-and-forget LRU-eviction shutdown tasks. The
        # event loop keeps only a weak reference to a bare create_task, so
        # without this an evicted backend's shutdown task can be GC'd before
        # it reaps the subprocess — leaking the process + its pipes, the exact
        # exhaustion eviction exists to prevent. Discarded via a done-callback.
        self._shutdown_tasks: set[asyncio.Task[None]] = set()
        # Counters surfaced via :meth:`stats` for tests and operator tooling.
        # Mutations happen under ``_lock`` (or inside the ``get_or_create``
        # spawn-lock for ``spawns``) so they are race-free.
        self._evictions_idle = 0
        self._evictions_lru = 0
        self._spawns = 0
        # Count of capacity-driven rejections (pool full, nothing evictable).
        # Surfaced in :meth:`stats` so a sustained-overflow fallback storm is
        # observable rather than invisible in the stub's best-effort log.
        self._capacity_rejects = 0

    @property
    def max_backends(self) -> int:
        return self._max_backends

    def __len__(self) -> int:
        # Snapshot read; concurrent add/evict may race but length is
        # advisory (used for metrics / capacity heuristics only).
        return len(self._backends)

    def stats(self) -> dict[str, int]:
        """Point-in-time counters for tests and diagnostics."""
        return {
            "size": len(self._backends),
            "max_backends": self._max_backends,
            "evictions_idle": self._evictions_idle,
            "evictions_lru": self._evictions_lru,
            "spawns": self._spawns,
            "capacity_rejects": self._capacity_rejects,
        }

    def _metrics_snapshot(self) -> dict[str, Any]:
        """Sync per-backend snapshot. PRIVATE: it does a blocking /proc RSS
        walk, so it must never run on the event loop — callers use
        :meth:`metrics_snapshot_async`, which offloads the walk via to_thread."""
        now = time.monotonic()
        backends = [
            {
                "server": b.pool_key.server_name,
                "agent": b.pool_key.agent_name,
                "pid": b.pid,
                "sessions": b.refcount,
                "idle_s": round(max(0.0, now - b.last_used_at), 1),
                "rss_kb": _proc_rss_kb(b.pid),
            }
            for b in self._backends.values()
            if b.is_alive
        ]
        return {**self.stats(), "backends": backends}

    async def metrics_snapshot_async(self) -> dict[str, Any]:
        """Race-free, off-loop variant of :meth:`_metrics_snapshot`.

        Snapshots per-backend identity under the pool lock (fast, on-loop),
        then performs the blocking ``/proc`` RSS walk off the event loop via
        ``asyncio.to_thread``. A plain ``to_thread(_metrics_snapshot)`` would
        iterate ``_backends`` in the worker thread and can race a concurrent
        add/evict ("dict changed size during iteration").
        """
        now = time.monotonic()
        async with self._lock:
            entries = [
                (
                    {
                        "server": b.pool_key.server_name,
                        "agent": b.pool_key.agent_name,
                        "pid": b.pid,
                        "sessions": b.refcount,
                        "idle_s": round(max(0.0, now - b.last_used_at), 1),
                    },
                    b.pid,
                )
                for b in self._backends.values()
                if b.is_alive
            ]
            base = self.stats()
        pids = [pid for _, pid in entries]
        rss_by_pid = await asyncio.to_thread(
            lambda: {pid: _proc_rss_kb(pid) for pid in pids}
        )
        rows: list[dict[str, Any]] = []
        for row, pid in entries:
            row["rss_kb"] = rss_by_pid.get(pid, -1)
            rows.append(row)
        return {**base, "backends": rows}

    def note_backend_death(self, breaker_key: str, uptime_secs: float) -> None:
        """Record that the backend identified by ``breaker_key`` died after
        ``uptime_secs``.

        ``breaker_key`` MUST be the PoolKey ``stable_hash()`` (a per-identity
        digest), NOT the bare ``server_name``: in the shared pool one server
        name maps to many distinct PoolKeys, so keying the breaker by name
        would let a healthy same-named sibling zero another identity's
        fast-death tally (defeating the breaker) and let one identity's crash
        loop trip the breaker for every co-tenant. Delegates to the circuit
        breaker (no-op when no breaker is wired). The breaker itself ignores
        deaths whose uptime exceeds its fast-death threshold, so callers can
        pass the real uptime unconditionally.
        """
        if self._breaker is not None:
            self._breaker.record_death(breaker_key, uptime_secs)

    def note_backend_healthy(self, breaker_key: str) -> None:
        """Record that the backend identified by ``breaker_key`` (the PoolKey
        ``stable_hash()`` — see :meth:`note_backend_death`) is healthy,
        clearing any accumulated fast-death tally and closing an OPEN breaker.
        No-op when no breaker is wired."""
        if self._breaker is not None:
            self._breaker.record_healthy(breaker_key)

    def live_backend_pids(self) -> list[int]:
        """PIDs of all currently-pooled backends. Each backend is spawned as a
        session leader (``start_new_session=True``), so ``pid == pgid``.
        Persisted out-of-band so a supervising manager can ``killpg`` these
        survivors if it has to SIGKILL a wedged gatewayd (which then never runs
        :meth:`shutdown_all`)."""
        return [b.pid for b in self._backends.values() if b.pid is not None]

    async def add(
        self, key: PoolKey, backend: "Backend", *, reserve: bool = False
    ) -> None:
        """Register a freshly-spawned backend under ``key``.

        Raises :class:`RuntimeError` if another backend is already attached
        under the same key; callers should consult :meth:`get_or_create`
        which handles dedup and LRU eviction automatically.

        If the pool is at :attr:`max_backends` capacity, one idle entry is
        evicted via LRU policy (lowest ``last_used_at``) before insertion.
        A backend with active refcount is NOT a valid eviction target —
        :meth:`_pick_lru_idle_locked` returns ``None`` if every entry is
        in use, and ``add`` then raises :class:`PoolAtCapacity`. The spawn
        path in :meth:`get_or_create` translates that into a clean
        fallback-eligible rejection so the stub runs the backend unpooled.
        """
        digest = key.stable_hash()
        async with self._lock:
            if digest in self._backends:
                raise RuntimeError(
                    f"pool key collision: {key.human_readable()} already has a backend"
                )
            if len(self._backends) >= self._max_backends:
                evicted = await self._evict_lru_locked()
                if evicted is None:
                    self._capacity_rejects += 1
                    raise PoolAtCapacity(
                        f"pool at capacity ({self._max_backends}) and no idle "
                        f"backend to evict for {key.human_readable()}"
                    )
            self._backends[digest] = backend
            if reserve:
                # Reserve UNDER the insert lock so a concurrent add() under
                # capacity pressure can't LRU-evict this brand-new idle,
                # unreserved backend in the window before the caller reserves.
                self._reserved_digests[digest] = (
                    self._reserved_digests.get(digest, 0) + 1
                )

    async def get_or_create(
        self,
        key: PoolKey,
        spawn: Callable[[], Awaitable["Backend"]],
    ) -> "Backend":
        """Return the backend for ``key``, spawning via ``spawn()`` if absent.

        Concurrent callers with the same key serialise through a per-digest
        :class:`asyncio.Lock` so ``spawn()`` only runs once even under a
        burst of simultaneous first-attaches. Dead backends (``is_alive``
        false) are treated as absent: the stale entry is evicted and
        ``spawn()`` runs again.

        The returned backend is **reserved** against idle/LRU eviction. The
        caller MUST call :meth:`unreserve` once ``backend.attach_stub``
        completes (or if the caller bails without attaching).
        """
        digest = key.stable_hash()
        existing = await self.get(key)
        if existing is not None and existing.is_alive:
            self.reserve(key)
            return existing

        # Serialise per-key so we spawn exactly once. Grabbing (or creating)
        # the per-key lock needs to be atomic against other callers that
        # would otherwise instantiate a second Lock — briefly hold _lock.
        async with self._lock:
            lock = self._spawn_locks.get(digest)
            if lock is None:
                lock = asyncio.Lock()
                self._spawn_locks[digest] = lock

        try:
            async with lock:
                # Double-check after acquiring the per-key lock: another task
                # may have completed the spawn while we were queued.
                existing = await self.get(key)
                if existing is not None and existing.is_alive:
                    self.reserve(key)
                    return existing
                if existing is not None:
                    # Stale/dead entry — record the death against the breaker
                    # (a no-op when the death was slow or the breaker disabled)
                    # and drop it so the replacement slot is free.
                    self.note_backend_death(
                        existing.pool_key.stable_hash(),
                        time.monotonic() - existing.created_at,
                    )
                    # We hold the per-key spawn lock here and respawn under it
                    # below, so keep it: dropping it while stale.shutdown() yields
                    # would let a concurrent get_or_create create a second lock
                    # and spawn a duplicate backend.
                    stale = await self.evict(key, keep_spawn_lock=True)
                    if stale is not None:
                        await stale.shutdown(timeout=2.0)

                # Circuit breaker: refuse to respawn a server
                # that is crash-looping. The stub falls back to a per-session
                # exec instead of the gateway churning spawns against a broken
                # binary. Checked AFTER recording the stale death above so the
                # death that trips the breaker blocks this very respawn.
                if self._breaker is not None and not self._breaker.allow(key.stable_hash()):
                    raise BackendUnavailable(
                        f"circuit breaker OPEN for server {key.server_name!r}; "
                        "refusing to spawn (recent crash loop)"
                    )

                backend = await spawn()
                try:
                    await self.add(key, backend, reserve=True)
                except BaseException:
                    # BaseException (incl. CancelledError): a cancel delivered
                    # during add() or the _spawns lock would otherwise skip
                    # cleanup and orphan the just-spawned subprocess (never
                    # inserted into _backends, so unreachable by evict_idle /
                    # shutdown_all). Shield the shutdown so the cancel can't
                    # abort it mid-way.
                    try:
                        await asyncio.shield(backend.shutdown(timeout=2.0))
                    except Exception:
                        pass
                    raise
                async with self._lock:
                    self._spawns += 1
                return backend
        finally:
            # Reap the per-digest spawn lock on the error paths (breaker OPEN,
            # spawn failure, capacity) where no backend landed in _backends —
            # otherwise it is never reaped (every cleanup site keys off a live
            # backend) and _spawn_locks grows unbounded under a crash loop.
            # Only reap when the lock is idle (no holder, no queued waiter): a
            # queued waiter proceeds to spawn (or reaps it on its own failure).
            async with self._lock:
                if (
                    digest not in self._backends
                    and self._spawn_locks.get(digest) is lock
                    and _lock_idle(lock)
                ):
                    self._spawn_locks.pop(digest, None)

    async def get(self, key: PoolKey) -> Optional["Backend"]:
        """Return the backend attached to ``key``, or ``None`` if absent."""
        digest = key.stable_hash()
        async with self._lock:
            return self._backends.get(digest)

    def reserve(self, key: PoolKey) -> None:
        """Mark ``key`` as in-flight (handed out, not yet attached).

        A reserved backend is invisible to the idle/LRU sweeper even at
        refcount==0. The caller MUST call :meth:`unreserve` once
        ``backend.attach_stub`` completes (or if the caller bails without
        attaching). This method is NOT async — it runs under the caller's
        existing context and does not need the pool lock because set-add on
        a small CPython set is thread-safe for single-writer (the event loop).
        """
        digest = key.stable_hash()
        self._reserved_digests[digest] = self._reserved_digests.get(digest, 0) + 1

    def unreserve(self, key: PoolKey) -> None:
        """Release the in-flight reservation for ``key``.

        Safe to call even if the key was never reserved (idempotent discard).
        """
        digest = key.stable_hash()
        remaining = self._reserved_digests.get(digest, 0) - 1
        if remaining > 0:
            self._reserved_digests[digest] = remaining
        else:
            self._reserved_digests.pop(digest, None)

    async def evict(
        self, key: PoolKey, *, keep_spawn_lock: bool = False,
        expected: Optional["Backend"] = None,
    ) -> Optional["Backend"]:
        """Detach the backend for ``key`` from the pool and return it.

        Caller is responsible for shutting the backend down. Returns
        ``None`` if no backend was registered. In-flight reservations are
        NOT cleared here — they settle through their balanced
        ``reserve``/``unreserve`` pairs (see the body comment).

        Set ``keep_spawn_lock`` when the caller already holds the per-key
        spawn lock and is about to respawn under it (the ``get_or_create``
        stale-replacement path). Popping the lock there would let a
        concurrent ``get_or_create`` for the same key create a fresh lock,
        bypass serialisation, and spawn a duplicate backend while this
        caller yields on ``stale.shutdown()``.
        """
        digest = key.stable_hash()
        async with self._lock:
            if expected is not None and self._backends.get(digest) is not expected:
                # A concurrent respawn replaced the backend under this digest
                # since the caller decided to evict (heartbeat sweep TOCTOU):
                # do not evict the innocent freshly-installed newcomer.
                return None
            backend = self._backends.pop(digest, None)
            if not keep_spawn_lock:
                self._spawn_locks.pop(digest, None)
            # Do NOT clear _reserved_digests here. Reservations are a balanced
            # refcount (every reserve() has a matching unreserve() in a finally)
            # keyed by digest, not by Backend instance. If a concurrent caller
            # reserved this digest for a soon-to-be-replaced backend, zeroing
            # the count here would let a fresh add(reserve=True) set count=1 and
            # then the original caller's stale unreserve() drop it back to 0 —
            # under-protecting the respawned backend against the LRU sweeper
            # before it is attached. Letting the balanced pairs settle keeps the
            # new generation protected.
            return backend

    async def evict_idle(self, older_than_secs: float, *, include_pinned: bool = False) -> int:
        """Evict every backend whose ``last_used_at`` is older than the
        threshold AND whose refcount has dropped to zero. A backend with
        attached stubs is never evicted — doing so would yank the socket
        out from under an active session.

        Prewarmed backends are ``pinned`` and sit at ``refcount == 0``
        forever (nothing stays attached to a warm-but-unused backend), so the
        plain idle rule would reclaim the exact backend prewarming exists to
        keep ready. The idle sweeper therefore skips pinned backends
        (``include_pinned=False``, the default). The credential-refresh drain
        passes ``include_pinned=True`` to deliberately force pinned backends
        down so they respawn with the fresh credential. The pinned set is
        bounded by the configured prewarm count (small, operator-controlled),
        so exempting it cannot starve the capacity cap.

        Returns the number of backends evicted. The caller is NOT expected
        to shut down the returned backends (this method does that
        internally) so the scheduler loop can stay a tight
        ``await pool.evict_idle(...)``.
        """
        cutoff = time.monotonic() - older_than_secs
        async with self._lock:
            stale = [
                (digest, backend)
                for digest, backend in self._backends.items()
                if backend.refcount == 0
                and digest not in self._reserved_digests
                and backend.last_used_at < cutoff
                and (include_pinned or not backend.pinned)
            ]
            for digest, _ in stale:
                del self._backends[digest]
                self._spawn_locks.pop(digest, None)
            self._evictions_idle += len(stale)
        for _, backend in stale:
            logger.info(
                "evicting idle backend pool=%s age=%.1fs",
                backend.pool_key.human_readable(),
                time.monotonic() - backend.last_used_at,
            )
            try:
                await backend.shutdown(timeout=2.0)
            except Exception:  # pragma: no cover — shutdown is defensive
                logger.exception("shutdown failed during idle eviction")
        return len(stale)

    async def _evict_lru_locked(self) -> Optional["Backend"]:
        """Pick and remove the least-recently-used idle backend. Caller
        MUST hold ``self._lock``. Returns the evicted backend (NOT yet
        shut down) or ``None`` if no entry is evictable.
        """
        victim = self._pick_lru_idle_locked()
        if victim is None:
            return None
        digest, backend = victim
        del self._backends[digest]
        # Drop the per-key spawn lock too — LRU eviction otherwise leaks a
        # Lock object per evicted digest for the life of the pool. Safe: an
        # LRU victim is idle (refcount 0, unreserved) so no in-flight spawn
        # holds this lock.
        self._spawn_locks.pop(digest, None)
        self._evictions_lru += 1
        logger.info(
            "LRU-evicting backend pool=%s last_used_age=%.2fs",
            backend.pool_key.human_readable(),
            backend._now() - backend.last_used_at,
        )
        # Shut down outside the lock — but the caller is already inside it.
        # Schedule on the event loop so we do not block ``add``; the task
        # is fire-and-forget, errors logged via the backend's own code.
        task = asyncio.create_task(_safe_shutdown(backend))
        self._shutdown_tasks.add(task)
        task.add_done_callback(self._shutdown_tasks.discard)
        return backend

    def _pick_lru_idle_locked(self) -> Optional[tuple[str, "Backend"]]:
        """Find the idle (refcount=0, unreserved) entry with the smallest
        ``last_used_at``. Returns ``None`` if every entry is in use or
        reserved. Caller MUST hold ``self._lock``.
        """
        oldest: Optional[tuple[str, "Backend"]] = None
        for digest, backend in self._backends.items():
            if backend.refcount != 0:
                continue
            if digest in self._reserved_digests:
                continue
            # A pinned (prewarmed) backend is never the LRU victim under
            # capacity pressure — it is the warm backend a later attach reuses.
            # The pinned set is bounded by the prewarm count, so skipping it
            # cannot deadlock the cap; a dead pinned slot is still recycled by
            # the heartbeat sweeper.
            if backend.pinned:
                continue
            if oldest is None or backend.last_used_at < oldest[1].last_used_at:
                oldest = (digest, backend)
        return oldest

    async def snapshot(self) -> list[tuple[PoolKey, "Backend"]]:
        """Return a point-in-time list of (key, backend) pairs. Intended
        for diagnostics and idle-sweep logic added in Milestone 2.

        The key is reconstructed via ``backend.pool_key`` — the pool does
        not store the ``PoolKey`` object separately since the digest alone
        is enough for routing.
        """
        async with self._lock:
            return [(b.pool_key, b) for b in self._backends.values()]

    async def shutdown_all(self, timeout: float = 5.0) -> None:
        """Shut down every registered backend and clear the pool.

        Errors from individual backend shutdowns are logged by the backend
        module; this method completes once every shutdown task has
        finished (successfully or not) so the server loop can ``os.unlink``
        its socket without leaving orphans behind.
        """
        async with self._lock:
            backends = list(self._backends.values())
            self._backends.clear()
            self._spawn_locks.clear()
            pending = list(self._shutdown_tasks)
        # Shutdowns are independent: fan out + join with gather.
        # ``return_exceptions=True`` keeps one slow/bad backend from
        # blocking the others (Python 3.10 has no TaskGroup).
        if backends:
            await asyncio.gather(
                *[b.shutdown(timeout=timeout) for b in backends],
                return_exceptions=True,
            )
        # Also join any in-flight LRU-eviction shutdown tasks so a backend
        # evicted moments before teardown is fully reaped, not orphaned.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _lock_idle(lock: asyncio.Lock) -> bool:
    """True if ``lock`` is unheld and has no queued waiters — safe to reap the
    per-digest spawn lock without racing a pending acquirer (a waiter released
    but not yet scheduled still shows in ``_waiters``)."""
    if lock.locked():
        return False
    waiters = getattr(lock, "_waiters", None)
    return not waiters


async def _safe_shutdown(backend: "Backend") -> None:
    """Shutdown wrapper for fire-and-forget calls from LRU eviction.

    Swallows exceptions so an uncatchable error in one backend's teardown
    does not bring down the caller's LRU-insert path.
    """
    try:
        await backend.shutdown(timeout=2.0)
    except Exception:  # pragma: no cover — defensive only
        logger.exception("background shutdown of evicted backend failed")
