"""Regression: a caller cancelled while ``WorkerPool._get_or_create`` is reaping
a dead idle worker must not strand a slot.

The reuse branch pops a live worker out of the idle set (still counted in
``_created``); the cold-start branch bumps ``_created`` for a worker it has not
built yet. The dead-worker retirement that follows runs outside the lock, and a
cancel landing in that await used to leak the live worker (never returned, never
retired, still PID-shielded) or leave a phantom ``_created`` slot that never
decremented.
"""

import asyncio

from kiro_crew.acp import worker_pool as wp


class _W:
    seq = 0

    def __init__(self, slow_shutdown: asyncio.Event | None = None) -> None:
        _W.seq += 1
        self.pid_ = 5000 + _W.seq
        self.alive = False
        self.shutdowns = 0
        self.slow = slow_shutdown

    async def start(self) -> None:
        self.alive = True

    async def send_message(self, prompt: str, timeout: float = 0) -> str:
        return "ok"

    async def reset(self) -> None:
        return None

    async def shutdown(self) -> None:
        self.shutdowns += 1
        if self.slow is not None:
            # Graceful process teardown takes time (the cancel lands here).
            await self.slow.wait()
        self.alive = False

    def is_alive(self) -> bool:
        return self.alive

    def pid(self) -> int:
        return self.pid_


def _patch_shield(monkeypatch, shielded: set[int]) -> None:
    monkeypatch.setattr(wp, "register_protected_pid", shielded.add)
    monkeypatch.setattr(wp, "unregister_protected_pid", shielded.discard)


def test_cancel_while_reaping_dead_idle_leaks_live_worker(monkeypatch):
    shielded: set[int] = set()
    _patch_shield(monkeypatch, shielded)

    async def scenario():
        gate = asyncio.Event()  # never set: dead worker's shutdown is slow
        live = _W()
        dead = _W(slow_shutdown=gate)
        pool = wp.WorkerPool(lambda: live, max_workers=2)
        # Seed two warm workers through the real acquire/release path.
        pool._factory = lambda: live
        w1, _ = await pool.acquire()
        pool._factory = lambda: dead
        w2, _ = await pool.acquire()
        await pool.release(w1)
        await pool.release(w2)  # idle == [live, dead]
        dead.alive = False  # dead worker's process exited while idle
        assert pool.stats() == {"workers": 2, "idle": 2, "busy": 0, "max": 2, "starting_max": 2}

        # A caller (e.g. a workflow agent task) is cancelled mid-acquire.
        t = asyncio.create_task(pool.send("hi"))
        for _ in range(20):
            await asyncio.sleep(0)  # pops dead (reap), pops live, awaits dead.shutdown()
        assert dead.shutdowns == 1
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass

        await pool.shutdown()
        return pool, live

    pool, live = asyncio.run(scenario())
    st = pool.stats()
    # Contract: every acquire returns or reaps exactly once. After a full
    # shutdown with zero callers in flight, nothing may remain live/shielded.
    assert live.shutdowns == 1, (
        f"live worker leaked: popped from idle, never released or retired "
        f"(shutdowns={live.shutdowns}, alive={live.alive}, stats={st})"
    )
    assert st["workers"] == 0 and st["busy"] == 0, f"accounting drift: {st}"
    assert live.pid_ not in shielded, "leaked worker's PID still sweep-shielded"


def test_cancel_while_reaping_on_cold_start_branch_leaves_phantom_slot(monkeypatch):
    _patch_shield(monkeypatch, set())

    async def scenario():
        gate = asyncio.Event()
        dead = _W(slow_shutdown=gate)
        pool = wp.WorkerPool(lambda: dead, max_workers=2)
        w, _ = await pool.acquire()
        await pool.release(w)  # idle == [dead]
        dead.alive = False
        pool._factory = lambda: _W()

        t = asyncio.create_task(pool.send("hi"))
        for _ in range(20):
            await asyncio.sleep(0)  # reaps dead, _created += 1 for the new one, awaits shutdown
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        await pool.shutdown()
        return pool

    st = asyncio.run(scenario()).stats()
    assert (
        st["workers"] == 0 and st["busy"] == 0
    ), f"phantom worker slot after cancel: no worker exists, stats={st}"
