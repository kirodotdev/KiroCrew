"""Tests for the reusable WorkerPool engine's sweep-protection lifecycle.

The engine shields each live worker's agent-process PID from the gateway's
periodic orphan sweep — registering it on start, re-syncing after every reset
(which respawns under a new PID), and unregistering on retirement. Without this a
busy pool worker is misclassified as a leaked orphan and SIGKILLed mid-task,
surfacing to the caller as "ACP process exited (code=1)". These tests fake the
protection registry so they are hermetic (no real PID files / signals).
"""
import asyncio
import unittest

from kiro_crew.acp import worker_pool as wp


class _FakeWorker:
    """A worker whose PID changes on each start/reset (mirrors a respawn)."""

    _seq = 0

    def __init__(self) -> None:
        self._pid = None
        self._alive = False

    async def start(self) -> None:
        _FakeWorker._seq += 1
        self._pid = 1000 + _FakeWorker._seq
        self._alive = True

    async def send_message(self, prompt: str, timeout: float = 0) -> str:
        return "ok:" + prompt

    async def reset(self) -> None:
        # Respawn under a NEW pid, as AcpReviewWorker.reset() does.
        _FakeWorker._seq += 1
        self._pid = 1000 + _FakeWorker._seq

    async def shutdown(self) -> None:
        self._alive = False
        self._pid = None

    def is_alive(self) -> bool:
        return self._alive

    def pid(self):
        return self._pid


class _PidlessWorker(_FakeWorker):
    """A worker that does NOT expose pid() — must be left unshielded (no-op)."""

    pid = None  # type: ignore[assignment]


class TestSweepProtection(unittest.TestCase):
    def setUp(self) -> None:
        self.registered: list[int] = []
        self.unregistered: list[int] = []
        self._orig_reg = wp.register_protected_pid
        self._orig_unreg = wp.unregister_protected_pid
        wp.register_protected_pid = lambda pid: self.registered.append(pid)
        wp.unregister_protected_pid = lambda pid: self.unregistered.append(pid)

    def tearDown(self) -> None:
        wp.register_protected_pid = self._orig_reg
        wp.unregister_protected_pid = self._orig_unreg

    def test_register_on_start_resync_on_reset_unregister_on_shutdown(self):
        _FakeWorker._seq = 0
        pool = wp.WorkerPool(lambda: _FakeWorker(), max_workers=1, max_starting=1)

        async def _run():
            # 1st task: cold-start -> register the fresh PID (1001). No reset
            # (fresh worker), so no re-sync.
            await pool.send("a")
            # 2nd task: reused worker -> reset() respawns to PID 1002 -> engine
            # unregisters 1001 and registers 1002.
            await pool.send("b")
            await pool.shutdown()

        asyncio.run(_run())

        # First live pid shielded on start; second shielded after reset.
        self.assertEqual(self.registered, [1001, 1002],
                         f"unexpected register order: {self.registered}")
        # Old pid released on reset re-sync; current pid released on shutdown.
        self.assertEqual(self.unregistered, [1001, 1002],
                         f"unexpected unregister order: {self.unregistered}")

    def test_dead_worker_is_unshielded_on_reap(self):
        _FakeWorker._seq = 0
        made: list[_FakeWorker] = []

        def factory():
            w = _FakeWorker()
            made.append(w)
            return w

        pool = wp.WorkerPool(factory, max_workers=1, max_starting=1)

        async def _run():
            await pool.send("a")            # creates worker#0, registers 1001
            made[0]._alive = False          # simulate the idle worker dying
            await pool.send("b")            # reaps worker#0 (unregister 1001),
            #                                 cold-starts worker#1 (register 1002)
            await pool.shutdown()           # unregister 1002

        asyncio.run(_run())

        self.assertEqual(self.registered, [1001, 1002])
        self.assertEqual(self.unregistered, [1001, 1002])

    def test_worker_without_pid_is_not_shielded(self):
        pool = wp.WorkerPool(lambda: _PidlessWorker(), max_workers=1, max_starting=1)

        async def _run():
            await pool.send("a")
            await pool.send("b")
            await pool.shutdown()

        asyncio.run(_run())

        self.assertEqual(self.registered, [], "pidless worker must not be shielded")
        self.assertEqual(self.unregistered, [])


if __name__ == "__main__":
    unittest.main()
