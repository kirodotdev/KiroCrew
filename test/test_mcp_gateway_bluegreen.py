"""Tests for blue-green backend cutover on credential rotation.

Verifies that when a credential rotation triggers, in-use backends are moved
to the draining list (invisible to acquire), new acquires spawn fresh
backends, and draining backends are reaped when refcount hits 0 or deadline
expires. Also covers the credential-file watcher (content-digest change
detection, first-observation baseline) and the seam-routed
``--credential-watch-path`` argv threading in :class:`GatewayManager`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from windows_sim import nonatomic_write, unlink_sharing_violation

from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import (
    _MAX_SPAWN_DRAIN_RETRIES,
    DRAIN_DEADLINE_SECS,
    BackendPool,
    BackendUnavailable,
    PoolKey,
    _DrainingBackend,
)

logger = logging.getLogger(__name__)


def _make_pool_key(server: str = "test-server", agent: str = "test-agent") -> PoolKey:
    return PoolKey(
        server_name=server,
        agent_name=agent,
        command_args_hash="abc123",
        effective_env_hash="def456",
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
        sandbox_mode="none",
        autoapprove_set_hash="ghi789",
        approval_mode="reads",
        trust_all_tools=False,
        user_identity="testuser",
        config_snapshot_hash="jkl012",
    )


def _make_mock_backend(
    pool_key: PoolKey,
    *,
    refcount: int = 0,
    is_alive: bool = True,
) -> Backend:
    """Create a mock Backend with controllable refcount and liveness."""
    proc = MagicMock()
    proc.returncode = None if is_alive else 0
    proc.pid = 12345
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=0)

    stdin = MagicMock()
    stdin.close = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()

    stdout = MagicMock()

    now = time.monotonic()
    backend = Backend(
        pool_key=pool_key,
        process=proc,
        stdin=stdin,
        stdout=stdout,
        created_at=now,
        last_used_at=now,
    )
    backend.refcount = refcount
    backend._stub_inboxes = {f"stub-{i}": asyncio.Queue() for i in range(refcount)}
    return backend


@pytest.mark.asyncio
async def test_drain_all_moves_inuse_to_draining() -> None:
    """A credential file change marks in-use backends as draining and removes
    them from the acquire index."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=2)
    await pool.add(key, backend)

    assert len(pool) == 1
    assert pool.draining_count == 0

    moved = await pool.drain_all_to_bluegreen()

    assert moved == 1
    assert len(pool) == 0  # removed from active index
    assert pool.draining_count == 1  # moved to draining
    # Backend still alive (serving in-flight)
    assert backend.is_alive
    # A draining backend must still be reachable via all_backends() so a
    # stop/abort during the drain window can find + cancel its in-flight calls.
    assert backend in pool.all_backends()


@pytest.mark.asyncio
async def test_acquire_spawns_fresh_after_drain() -> None:
    """After drain, subsequent acquire spawns a fresh backend while the
    old one is still alive in draining."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    old_backend = _make_mock_backend(key, refcount=1)
    await pool.add(key, old_backend)

    # Drain — moves old backend to draining
    await pool.drain_all_to_bluegreen()
    assert len(pool) == 0

    # New acquire should spawn a fresh backend
    new_backend = _make_mock_backend(key, refcount=0)

    async def _spawn() -> Backend:
        return new_backend

    result = await pool.get_or_create(key, _spawn)
    assert result is new_backend
    assert len(pool) == 1
    # Old backend is still draining
    assert pool.draining_count == 1


@pytest.mark.asyncio
async def test_draining_reaped_when_refcount_zero() -> None:
    """Draining backend is reaped when its refcount drops to 0."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=1)
    await pool.add(key, backend)

    await pool.drain_all_to_bluegreen()
    assert pool.draining_count == 1

    # Simulate stub detach — refcount drops to 0
    backend.refcount = 0
    backend._stub_inboxes.clear()

    reaped = await pool.reap_draining()
    assert len(reaped) == 1
    assert reaped[0] is backend
    assert pool.draining_count == 0


@pytest.mark.asyncio
async def test_deadline_expiry_force_kills_draining() -> None:
    """Deadline expiry force-kills draining backend and triggers
    gone-broadcast even with refcount > 0."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=2)
    await pool.add(key, backend)

    # Drain with a very short deadline
    await pool.drain_all_to_bluegreen(deadline_secs=0.0)
    assert pool.draining_count == 1

    # Give time for deadline to pass
    await asyncio.sleep(0.01)

    # Patch _broadcast_backend_gone to verify it's called
    backend._broadcast_backend_gone = AsyncMock()

    reaped = await pool.reap_draining()
    assert len(reaped) == 1
    assert reaped[0] is backend
    assert pool.draining_count == 0
    # With refcount > 0, broadcast_backend_gone should have been called
    backend._broadcast_backend_gone.assert_called_once_with(
        "draining deadline expired (credential-rotation cutover)"
    )


@pytest.mark.asyncio
async def test_idle_backends_still_evicted_as_before() -> None:
    """Idle backends (refcount==0) are still evicted normally by
    evict_idle — the blue-green mechanism doesn't interfere."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=0)
    # Make it look old
    backend.last_used_at = time.monotonic() - 1000
    await pool.add(key, backend)

    # Normal idle eviction still works
    evicted = await pool.evict_idle(10.0)
    assert evicted == 1
    assert len(pool) == 0


@pytest.mark.asyncio
async def test_draining_not_double_counted_in_capacity() -> None:
    """Draining backends don't count against max_backends capacity —
    fresh backends can be spawned even if draining list is non-empty."""
    pool = BackendPool(max_backends=2)
    key1 = _make_pool_key(server="server-1")
    key2 = _make_pool_key(server="server-2")
    b1 = _make_mock_backend(key1, refcount=1)
    b2 = _make_mock_backend(key2, refcount=1)
    await pool.add(key1, b1)
    await pool.add(key2, b2)

    # Pool is at capacity (2/2)
    assert len(pool) == 2

    # Drain both — now 0 active, 2 draining
    await pool.drain_all_to_bluegreen()
    assert len(pool) == 0
    assert pool.draining_count == 2

    # Can now add fresh backends without capacity rejection
    key3 = _make_pool_key(server="server-3")
    b3 = _make_mock_backend(key3, refcount=0)
    await pool.add(key3, b3)
    assert len(pool) == 1


@pytest.mark.asyncio
async def test_shutdown_all_clears_draining() -> None:
    """shutdown_all cleans up both active and draining backends."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=1)
    await pool.add(key, backend)
    await pool.drain_all_to_bluegreen()

    key2 = _make_pool_key(server="active-server")
    active_backend = _make_mock_backend(key2, refcount=0)
    await pool.add(key2, active_backend)

    assert pool.draining_count == 1
    assert len(pool) == 1

    await pool.shutdown_all()

    assert pool.draining_count == 0
    assert len(pool) == 0


@pytest.mark.asyncio
async def test_stats_includes_draining_count() -> None:
    """The stats() dict includes the draining count."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=1)
    await pool.add(key, backend)
    await pool.drain_all_to_bluegreen()

    stats = pool.stats()
    assert stats["draining"] == 1
    assert stats["size"] == 0


@pytest.mark.asyncio
async def test_drain_deadline_default_used() -> None:
    """drain_all_to_bluegreen uses the module-level DRAIN_DEADLINE_SECS by
    default (gatewayd imports the same constant for its log line)."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=1)
    await pool.add(key, backend)
    before = time.monotonic()
    await pool.drain_all_to_bluegreen()
    entry = pool._draining[0]
    assert isinstance(entry, _DrainingBackend)
    assert entry.deadline >= before + DRAIN_DEADLINE_SECS - 1.0


# --- gatewayd drain-and-rewarm handler ---------------------------------------


@pytest.mark.asyncio
async def test_drain_and_rewarm_schedules_prewarm_on_success() -> None:
    """The credential-change handler drains then schedules a re-warm."""
    from kiro_crew.mcp_gateway.gatewayd import _drain_and_rewarm_on_credential_change

    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=1)
    await pool.add(key, backend)

    scheduled: list[bool] = []
    await _drain_and_rewarm_on_credential_change(pool, lambda: scheduled.append(True))

    assert scheduled == [True]
    assert len(pool) == 0
    assert pool.draining_count == 1


@pytest.mark.asyncio
async def test_drain_and_rewarm_skips_prewarm_on_drain_failure() -> None:
    """If the drain raises, the re-warm is deliberately skipped (re-warming
    would reuse + PIN stale backends)."""
    from kiro_crew.mcp_gateway.gatewayd import _drain_and_rewarm_on_credential_change

    pool = BackendPool(max_backends=10)
    pool.drain_all_to_bluegreen = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    scheduled: list[bool] = []
    await _drain_and_rewarm_on_credential_change(pool, lambda: scheduled.append(True))

    assert scheduled == []


# --- credwatch: content-digest change detection -------------------------------


@pytest.mark.asyncio
async def test_live_backend_pids_includes_draining() -> None:
    """Draining backends keep running as live session leaders for up to the
    deadline, so their PIDs MUST appear in live_backend_pids() — otherwise a
    gatewayd SIGKILLed during the drain window orphans them."""
    pool = BackendPool(max_backends=10)
    drain_key = _make_pool_key(server="draining-server")
    draining = _make_mock_backend(drain_key, refcount=1)
    draining.process.pid = 222
    await pool.add(drain_key, draining)

    # Drain moves draining-server to the draining list.
    await pool.drain_all_to_bluegreen()
    assert len(pool) == 0
    assert pool.draining_count == 1

    # Add a fresh active backend post-cutover.
    active_key = _make_pool_key(server="active-server")
    active = _make_mock_backend(active_key, refcount=1)
    active.process.pid = 111
    await pool.add(active_key, active)

    pids = pool.live_backend_pids()
    assert 111 in pids  # active
    assert 222 in pids  # draining — must not be dropped


@pytest.mark.asyncio
async def test_reserved_draining_backend_not_reaped_until_deadline() -> None:
    """A draining backend that is still reserved (handed out, not yet attached,
    refcount==0) must NOT be reaped on the refcount==0 rule — reaping would kill
    it out from under a stub mid-attach. The deadline still force-kills it."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=0)
    await pool.add(key, backend)
    # Reserve it (simulating a get_or_create hand-out that hasn't attached yet).
    pool.reserve(key)

    await pool.drain_all_to_bluegreen()
    assert pool.draining_count == 1

    # refcount==0 but reserved → survives this reap.
    reaped = await pool.reap_draining()
    assert reaped == []
    assert pool.draining_count == 1

    # Once the reservation is released, the refcount==0 rule reaps it.
    pool.unreserve(key)
    reaped = await pool.reap_draining()
    assert len(reaped) == 1
    assert reaped[0] is backend


@pytest.mark.asyncio
async def test_reserved_draining_backend_force_killed_at_deadline() -> None:
    """A reservation that never resolves (caller bailed) cannot pin a draining
    backend forever — the deadline still force-kills it."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    backend = _make_mock_backend(key, refcount=0)
    await pool.add(key, backend)
    pool.reserve(key)

    await pool.drain_all_to_bluegreen(deadline_secs=0.0)
    await asyncio.sleep(0.01)

    reaped = await pool.reap_draining()
    assert len(reaped) == 1
    assert reaped[0] is backend


@pytest.mark.asyncio
async def test_inflight_spawn_during_drain_is_discarded_and_respawned() -> None:
    """A backend spawned with the OLD credential (spawn() suspended across a
    blue-green cutover) must NOT be pooled as active — the epoch guard rejects
    it and get_or_create respawns a fresh backend."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()

    stale = _make_mock_backend(key, refcount=0)
    stale.shutdown = AsyncMock()  # type: ignore[method-assign]
    fresh = _make_mock_backend(key, refcount=0)

    spawned: list[Backend] = []
    gate = asyncio.Event()

    async def _spawn() -> Backend:
        # First spawn: fire the cutover mid-spawn, return the stale backend.
        if not spawned:
            spawned.append(stale)
            await pool.drain_all_to_bluegreen()  # advances the drain epoch
            gate.set()
            return stale
        spawned.append(fresh)
        return fresh

    result = await pool.get_or_create(key, _spawn)

    assert gate.is_set()
    assert result is fresh  # respawned fresh, not the stale one
    assert stale.shutdown.await_count == 1  # stale backend was discarded
    # The fresh backend is the active pooled entry.
    assert (await pool.get(key)) is fresh


@pytest.mark.asyncio
async def test_persistent_cutover_storm_rejects_never_pools_stale() -> None:
    """If a blue-green cutover fires on EVERY spawn attempt (a rotation storm),
    the retries are exhausted WITHOUT ever pooling a possibly-stale backend: the
    epoch guard stays armed on every attempt (incl. the last), so get_or_create
    raises the fallback-eligible BackendUnavailable and the pool stays empty.
    Regression for the old `spawn_epoch=None if last_attempt` hole that admitted
    a stale-credential backend into the active pool after a completed drain."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()

    spawned: list[Backend] = []

    async def _spawn() -> Backend:
        b = _make_mock_backend(key, refcount=0)
        b.shutdown = AsyncMock()  # type: ignore[method-assign]
        spawned.append(b)
        # Advance the drain epoch DURING every spawn → add() always rejects with
        # _DrainedDuringSpawn, exhausting the bounded retries.
        await pool.drain_all_to_bluegreen()
        return b

    with pytest.raises(BackendUnavailable, match="rotation storm|cutover"):
        await pool.get_or_create(key, _spawn)

    # Every spawned (stale) backend was discarded; NONE was pooled active.
    assert len(spawned) == _MAX_SPAWN_DRAIN_RETRIES + 1
    assert all(b.shutdown.await_count == 1 for b in spawned)
    assert (await pool.get(key)) is None


@pytest.mark.asyncio
async def test_drain_keeps_held_spawn_lock_no_double_spawn() -> None:
    """drain_all_to_bluegreen must not pop a spawn lock held by an in-flight
    spawn — popping it lets a concurrent get_or_create create a fresh lock and
    double-spawn (hitting the pool-key-collision RuntimeError)."""
    pool = BackendPool(max_backends=10)
    key = _make_pool_key()
    digest = key.stable_hash()

    # Simulate a held spawn lock for this digest (an in-flight spawn).
    lock = asyncio.Lock()
    await lock.acquire()
    pool._spawn_locks[digest] = lock

    # Also have an active backend to drain.
    backend = _make_mock_backend(key, refcount=1)
    await pool.add(key, backend)

    await pool.drain_all_to_bluegreen()

    # The held lock must survive the drain.
    assert pool._spawn_locks.get(digest) is lock
    lock.release()


def test_credwatch_streaming_digest_matches_oneshot(tmp_path: Path) -> None:
    """The streamed block-by-block digest (never materializes the full
    credential) equals a one-shot sha256 of the same bytes — hardening the read
    must not change the fingerprint. Exercise a multi-block file to cover the
    chunk boundary."""
    import hashlib

    from kiro_crew.mcp_gateway import credwatch

    payload = b"rotate-me-" * 20000  # > _DIGEST_CHUNK_BYTES (64 KiB)
    cred = tmp_path / "cred"
    cred.write_bytes(payload)
    assert credwatch._content_digest(cred) == hashlib.sha256(payload).hexdigest()
    # Missing file → None (unchanged contract).
    assert credwatch._content_digest(tmp_path / "absent") is None


def _atomic_cred_write(cred: Path, data: bytes) -> None:
    """Create or replace the credential file ATOMICALLY (write a sibling temp,
    then ``os.replace`` it into place).

    ``Path.write_bytes`` truncates-then-writes non-atomically, leaving a brief
    window where the file exists but is empty/partial. The ~10 ms credential
    poller (which runs its digest read in a worker thread) can observe that
    transient state as a distinct content change and fire a spurious EXTRA time
    on the native Windows matrix — the same truncate race documented on
    ``test_credwatch_no_fire_on_byte_identical_rewrite``. A real credential
    refresh daemon rotates atomically; mirror that here so the watcher sees a
    single absent→present (or old→new) transition on every OS.
    """
    import os

    tmp = cred.with_name(f"{cred.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, cred)


def _resilient_cred_unlink(cred: Path) -> None:
    """Delete the credential, tolerating the transient Windows sharing violation.

    On Windows a file cannot be deleted while another handle holds it open
    without ``FILE_SHARE_DELETE`` — which the watcher's worker-thread digest read
    (a plain ``open(..., "rb")``) does not grant. The test's ``unlink`` therefore
    races that read and raises ``PermissionError`` (``WinError 32``); POSIX allows
    deleting an open file, so this never reproduces locally. Retry within a
    bounded window — exactly what an external credential deleter/rotator does —
    until the watcher releases the handle between polls. The file still fully
    EXISTS during the retries, so the watcher keeps reading the unchanged
    baseline and does not fire until the delete finally lands.
    """
    deadline = time.monotonic() + 2.0
    while True:
        try:
            cred.unlink()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


class _ProbeBarrier:
    """Counts ``watch_credential`` probe cycles so a test can await "N polls
    have completed" instead of sleeping a wall-clock guess.

    Sleeping was the root of the Windows flake in issue #1105. A test slept
    50ms hoping the baseline probe had run, then wrote the rotation. On a runner
    with ~15.6ms timer granularity and slower file IO the write could land
    BEFORE the baseline probe, so the baseline captured the rotated bytes and no
    change was ever detected.

    Pass an instance as ``on_probe_complete``. The watcher invokes it once per
    cycle, on every branch.
    """

    def __init__(self) -> None:
        self.count = 0
        self._bumped = asyncio.Event()

    def __call__(self) -> None:
        self.count += 1
        self._bumped.set()

    async def wait_for(self, n: int, timeout: float = 5.0) -> None:
        """Return once at least ``n`` probe cycles have completed.

        Clear-then-wait is safe because ``__call__`` is synchronous and runs on
        this same loop, so it cannot interleave between the count check and the
        ``wait()``. The post-clear re-check is belt and braces.
        """

        async def _spin() -> None:
            while self.count < n:
                self._bumped.clear()
                if self.count >= n:
                    return
                await self._bumped.wait()

        await asyncio.wait_for(_spin(), timeout)


@pytest.mark.asyncio
async def test_credwatch_first_observation_is_baseline_no_fire(tmp_path: Path) -> None:
    """The first observation of the credential file establishes the baseline
    and never fires on_change."""
    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"
    cred.write_bytes(b"secret-v1")
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()

    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(3)  # 3 polls: baseline + 2 unchanged confirms no fire
    stop.set()
    await task

    assert fired == []


@pytest.mark.asyncio
async def test_credwatch_baseline_established_immediately(tmp_path: Path) -> None:
    """The baseline is captured on the FIRST probe (at startup), not one full
    interval later. A rotation that lands AFTER the immediate baseline probe but
    within the first interval must fire on_change. Under the old wait-first loop
    the baseline probe ran only after one interval, so it would have adopted the
    rotated content as the baseline and never fired."""
    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"
    cred.write_bytes(b"secret-v1")
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()

    # interval 0.1s: immediate probe at t≈0 sets baseline=v1; v2 written at
    # t≈0.05 (after baseline, before the next poll); poll at t≈0.1 detects the
    # change and fires. A wait-first loop would first probe at t≈0.1, see v2,
    # and make it the baseline → no fire.
    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.1,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # baseline (v1) captured by the immediate probe
    _atomic_cred_write(cred, b"secret-v2-rotated")
    await barrier.wait_for(barrier.count + 2)  # 2 more polls to detect the rotation
    stop.set()
    await task

    assert fired == [True]  # rotation within the first interval was detected


@pytest.mark.asyncio
async def test_credwatch_no_fire_on_byte_identical_rewrite(tmp_path: Path) -> None:
    """An mtime bump with byte-identical content (a no-op rewrite by a
    credential refresh daemon) must NOT fire on_change."""
    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"
    cred.write_bytes(b"secret-v1")
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()

    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # baseline established
    # Simulate a no-op refresh (byte-identical content, moved mtime) by touching
    # ONLY the mtime — do NOT physically rewrite. The watcher's identity is
    # (mtime, content-hash), so re-writing the SAME bytes is indistinguishable
    # from an mtime touch; but a real ``write_bytes`` truncates-then-writes
    # NON-atomically, and the ~10ms poller can read that transient empty/partial
    # file (digest != baseline -> a spurious fire, then a second fire when the
    # full bytes reappear). That truncate race — not the property under test — is
    # what fails this on Windows. An mtime bump alone reproduces the exact
    # observable state a well-behaved (atomic) refresh daemon leaves behind and is
    # deterministic on every OS.
    import os

    st = cred.stat()
    os.utime(cred, (st.st_atime, st.st_mtime + 10.0))
    await barrier.wait_for(barrier.count + 2)  # 2 more polls confirm no fire
    stop.set()
    await task

    assert fired == []


@pytest.mark.asyncio
async def test_credwatch_fires_on_content_change(tmp_path: Path) -> None:
    """A real content change past the baseline fires on_change (async
    handlers are awaited)."""
    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"
    cred.write_bytes(b"secret-v1")
    stop = asyncio.Event()
    fired = asyncio.Event()
    barrier = _ProbeBarrier()

    async def _on_change() -> None:
        fired.set()

    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            _on_change,
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # baseline established
    import os

    cred.write_bytes(b"secret-v2")
    st = cred.stat()
    os.utime(cred, (st.st_atime, st.st_mtime + 10.0))  # defeat coarse mtime gate
    await asyncio.wait_for(fired.wait(), timeout=2.0)
    stop.set()
    await task


@pytest.mark.asyncio
async def test_credwatch_fires_on_content_change_with_unchanged_mtime(tmp_path: Path) -> None:
    """A rotation that rewrites the file in-place with NEW content but leaves
    st_mtime unchanged (coarse-granularity NFS/FAT timestamps) MUST still fire.
    An mtime cheap-gate would permanently miss this rotation."""
    import os

    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"
    cred.write_bytes(b"secret-v1")
    baseline_mtime = cred.stat().st_mtime
    stop = asyncio.Event()
    fired = asyncio.Event()
    barrier = _ProbeBarrier()

    async def _on_change() -> None:
        fired.set()

    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            _on_change,
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # baseline established

    # Rewrite with new content, then FORCE mtime back to the baseline value —
    # simulating a coarse-mtime filesystem where the rotation shares a tick.
    cred.write_bytes(b"secret-v2")
    os.utime(cred, (baseline_mtime, baseline_mtime))

    await asyncio.wait_for(fired.wait(), timeout=2.0)
    stop.set()
    await task


@pytest.mark.asyncio
async def test_credwatch_absent_then_appearing_fires(tmp_path: Path) -> None:
    """When the file is ABSENT at the first probe, its later appearance is a
    real 'no credential -> credential' transition and DOES fire on_change — so a
    backend prewarmed during the absent startup window gets drained/respawned.
    (The initial absent observation itself is the silent baseline.)"""
    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"  # does not exist yet
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()

    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # absent baseline captured
    _atomic_cred_write(cred, b"secret-v1")  # appearance after an absent baseline → fires
    await barrier.wait_for(barrier.count + 2)  # 2 polls: detect appearance + confirm
    stop.set()
    await task

    assert fired == [True]


@pytest.mark.asyncio
async def test_credwatch_never_appearing_file_never_fires(tmp_path: Path) -> None:
    """A file that stays ABSENT for the watcher's whole life never fires — the
    absent baseline is silent and there is no transition to report."""
    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"  # never created
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()

    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(3)  # 3 polls on absent file: confirms no spurious fire
    stop.set()
    await task

    assert fired == []


@pytest.mark.asyncio
async def test_credwatch_present_then_deleted_fires_revocation(tmp_path: Path) -> None:
    """Deleting a PRESENT credential (present -> absent) is a revocation and
    MUST fire on_change — otherwise pooled backends keep the revoked credential.
    A second delete-poll while already absent does NOT re-fire."""
    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"
    cred.write_bytes(b"secret-v1")
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()

    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # present baseline captured
    _resilient_cred_unlink(cred)  # revocation
    await barrier.wait_for(barrier.count + 3)  # 3 absent polls: detect + confirm no re-fire
    stop.set()
    await task

    # Exactly one fire for the revocation — not one per absent poll.
    assert fired == [True]


@pytest.mark.asyncio
async def test_credwatch_delete_then_reappear_fires_twice(tmp_path: Path) -> None:
    """present -> absent -> present: the delete fires (revocation) AND the later
    re-appearance fires again (new credential), because the baseline moves to
    absent on delete rather than being left stale."""
    from kiro_crew.mcp_gateway import credwatch

    cred = tmp_path / "cred"
    cred.write_bytes(b"secret-v1")
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()

    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # present baseline
    _resilient_cred_unlink(cred)  # -> absent (fire #1: revocation)
    await barrier.wait_for(barrier.count + 2)  # 2 polls: detect deletion + stabilize
    _atomic_cred_write(cred, b"secret-v2")  # -> present (fire #2: new credential)
    await barrier.wait_for(barrier.count + 2)  # 2 polls: detect reappearance + confirm
    stop.set()
    await task

    assert fired == [True, True]


# --- Windows-condition regression locks (via test/windows_sim.py) -------------
# These reproduce the two native-Windows credwatch failures DETERMINISTICALLY on
# any OS, so a regression is caught on the Mac/Linux dev loop instead of a CI
# round-trip. Each pairs the hazard (buggy pattern under the simulator) with the
# fix (the _atomic_cred_write / _resilient_cred_unlink helpers surviving it).


@pytest.mark.asyncio
async def test_credwatch_nonatomic_appearance_double_fires_but_atomic_single(
    tmp_path: Path,
) -> None:
    """A NON-ATOMIC appearance (bare write_bytes: truncate-then-write) exposes a
    transient empty file the ~10 ms poller reads as its own revision — firing an
    EXTRA time (this is the real Windows failure, made deterministic via
    ``nonatomic_write``). The atomic helper collapses it to a single
    absent→present transition and fires exactly once."""
    from kiro_crew.mcp_gateway import credwatch

    # Hazard: non-atomic appearance → the empty truncate window fires spuriously.
    cred = tmp_path / "cred"
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()
    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # absent baseline
    with nonatomic_write(cred, b"secret-v1"):
        await barrier.wait_for(barrier.count + 1)  # poller observes the EMPTY truncate window
    await barrier.wait_for(barrier.count + 1)  # poller observes the full payload
    stop.set()
    await task
    assert fired == [True, True]  # empty→fire, then full→fire: the spurious extra

    # Fix: an atomic appearance is a single transition — exactly one fire.
    cred2 = tmp_path / "cred2"
    stop2 = asyncio.Event()
    fired2: list[bool] = []
    barrier2 = _ProbeBarrier()
    task2 = asyncio.create_task(
        credwatch.watch_credential(
            cred2,
            0.01,
            stop2,
            lambda: fired2.append(True),
            logger,
            on_probe_complete=barrier2,
        )
    )
    await barrier2.wait_for(1)  # absent baseline
    _atomic_cred_write(cred2, b"secret-v1")
    await barrier2.wait_for(barrier2.count + 2)  # 2 polls: detect appearance + confirm
    stop2.set()
    await task2
    assert fired2 == [True]


@pytest.mark.asyncio
async def test_credwatch_resilient_unlink_survives_sharing_violation(
    tmp_path: Path,
) -> None:
    """Deleting the credential while the watcher holds it open raises WinError 32
    on Windows (``unlink_sharing_violation``). A bare ``unlink`` propagates it;
    ``_resilient_cred_unlink`` retries through the transient violation, the file
    is deleted, and the revocation fires exactly once."""
    from kiro_crew.mcp_gateway import credwatch

    # Hazard: a bare unlink propagates the sharing violation.
    doomed = tmp_path / "cred"
    doomed.write_bytes(b"secret-v1")
    with unlink_sharing_violation(match="cred", times=1):
        with pytest.raises(PermissionError):
            doomed.unlink()
    doomed.unlink()  # cleanup (violation window is closed)

    # Fix: the resilient helper retries past the violation while the watcher runs.
    cred = tmp_path / "cred"
    cred.write_bytes(b"secret-v1")
    stop = asyncio.Event()
    fired: list[bool] = []
    barrier = _ProbeBarrier()
    task = asyncio.create_task(
        credwatch.watch_credential(
            cred,
            0.01,
            stop,
            lambda: fired.append(True),
            logger,
            on_probe_complete=barrier,
        )
    )
    await barrier.wait_for(1)  # present baseline
    with unlink_sharing_violation(match="cred", times=1):
        _resilient_cred_unlink(cred)  # first delete faults, retry lands
    assert not cred.exists()
    await barrier.wait_for(barrier.count + 3)  # 3 absent polls: detect + confirm no re-fire
    stop.set()
    await task
    assert fired == [True]  # exactly one revocation fire, no spurious extras


# --- manager seam: --credential-watch-path argv threading ---------------------


class _WatchingIdentity:
    """Stub IdentityProvider whose credential_watch_paths is non-empty."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths

    def status(self) -> dict:
        return {}

    async def status_line(self, prefix: str = "*SSO:*") -> str:
        return ""

    def whoami(self):
        return None

    def issuer(self):
        return None

    def credential_watch_paths(self) -> list[Path]:
        return list(self._paths)


def test_default_identity_watch_paths_empty() -> None:
    """The public DefaultIdentityProvider watches nothing."""
    from kiro_crew.platform.defaults import DefaultIdentityProvider

    assert DefaultIdentityProvider().credential_watch_paths() == []


def test_manager_resolves_no_watch_paths_by_default() -> None:
    """With the standalone default context, the manager resolves no watch
    paths — the daemon command line stays byte-identical."""
    from kiro_crew.mcp_gateway.manager import GatewayManager

    assert GatewayManager._credential_watch_paths() == []


def test_manager_threads_seam_watch_paths(tmp_path: Path) -> None:
    """A context whose IdentityProvider supplies watch paths gets each one
    threaded through the manager's argv resolution."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.mcp_gateway.manager import GatewayManager
    from kiro_crew.platform import build_default_context, set_context

    cred = tmp_path / "rotated-credential"
    ctx = build_default_context(KiroCrewConfig())
    ctx = dataclasses.replace(ctx, identity=_WatchingIdentity([cred]))
    set_context(ctx)

    assert GatewayManager._credential_watch_paths() == [cred]


def test_manager_degrades_to_empty_on_adapter_failure(tmp_path: Path) -> None:
    """A pre-method companion adapter (or any non-composition adapter
    failure) degrades to [] — no watcher — instead of raising."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.mcp_gateway.manager import GatewayManager
    from kiro_crew.platform import build_default_context, set_context
    from kiro_crew.platform.defaults import DefaultIdentityProvider

    class _PreMethodIdentity(DefaultIdentityProvider):
        # Simulate an adapter built before the v1 method addition.
        credential_watch_paths = None  # type: ignore[assignment]

    ctx = build_default_context(KiroCrewConfig())
    ctx = dataclasses.replace(ctx, identity=_PreMethodIdentity())
    set_context(ctx)

    assert GatewayManager._credential_watch_paths() == []


def test_gatewayd_argparser_accepts_repeatable_watch_flag() -> None:
    """--credential-watch-path is repeatable and defaults to []."""
    from kiro_crew.mcp_gateway.gatewayd import _build_argparser

    p = _build_argparser()
    args = p.parse_args([])
    assert args.credential_watch_paths == []
    args = p.parse_args(["--credential-watch-path", "/tmp/a", "--credential-watch-path", "/tmp/b"])
    assert args.credential_watch_paths == ["/tmp/a", "/tmp/b"]
