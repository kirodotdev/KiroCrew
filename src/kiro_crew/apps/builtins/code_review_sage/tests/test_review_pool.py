"""Unit tests for the reusable review worker pool (lazy, bounded, isolated).

Workers are faked so the pool's concurrency/lifecycle logic is exercised without
spawning real ACP processes.
"""
import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path

from sage_lib.review_pool import (
    _DEFAULT_REVIEW_MODEL,
    MAX_CONCURRENT,
    MAX_STARTING,
    REVIEW_EFFORT,
    ReviewPool,
    _resolve_review_agent,
    _review_work_dir,
    _reviewer_model,
    _write_effort_overlay,
    make_sync_dispatch,
    pool_stats,
    reviewer_info,
    shutdown_pool,
)


class FakeWorker:
    """A trivial worker: records start/reset/send/shutdown and stays alive."""

    def __init__(self) -> None:
        self.started = 0
        self.resets = 0
        self.sends: list[str] = []
        self.shutdowns = 0
        self.live = False

    async def start(self) -> None:
        self.started += 1
        self.live = True

    async def send_message(self, prompt: str, timeout: float = 0) -> str:
        self.sends.append(prompt)
        return "ok:" + prompt

    async def reset(self) -> None:
        self.resets += 1

    async def shutdown(self) -> None:
        self.shutdowns += 1
        self.live = False

    def is_alive(self) -> bool:
        return self.live


class FailingWorker(FakeWorker):
    async def send_message(self, prompt: str, timeout: float = 0) -> str:
        raise RuntimeError("boom")


class GateWorker(FakeWorker):
    """send_message blocks on a shared event — used to observe concurrency."""

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__()
        self._gate = gate

    async def send_message(self, prompt: str, timeout: float = 0) -> str:
        self.sends.append(prompt)
        await self._gate.wait()
        return "ok:" + prompt


class StartGateWorker(FakeWorker):
    """start() blocks on a shared event and tracks simultaneous startups."""

    def __init__(self, gate: asyncio.Event, counter: dict) -> None:
        super().__init__()
        self._gate = gate
        self._counter = counter

    async def start(self) -> None:
        self._counter["now"] += 1
        self._counter["max"] = max(self._counter["max"], self._counter["now"])
        await self._gate.wait()
        self._counter["now"] -= 1
        self.started += 1
        self.live = True


def _mk(lst, worker):
    """Factory helper: record the created worker and return it. Avoids the
    ``lst.append(x) or worker`` idiom, which mypy rejects (append returns None)."""
    lst.append(worker)
    return worker


def _count(lst, worker):
    """Factory helper: record one creation (append a marker) and return the worker."""
    lst.append(1)
    return worker


class TestReviewPool(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_no_workers_until_used(self):
        made: list = []
        pool = ReviewPool(worker_factory=lambda: _count(made, FakeWorker()))
        self.assertEqual(pool.created_count, 0)
        self.assertEqual(pool.idle_count, 0)
        self.assertEqual(made, [])

    async def test_reuse_and_reset_only_on_reuse(self):
        worker = FakeWorker()
        calls: list = []
        pool = ReviewPool(worker_factory=lambda: _count(calls, worker))

        out1 = await pool.send("a")
        self.assertEqual(out1, "ok:a")
        self.assertEqual(worker.resets, 0)        # fresh worker -> no reset on first use
        self.assertEqual(pool.idle_count, 1)

        out2 = await pool.send("b")
        self.assertEqual(out2, "ok:b")
        self.assertEqual(worker.resets, 1)        # reused -> clean slate before the next CR
        self.assertEqual(len(calls), 1)           # same worker reused, not recreated

    async def test_max_concurrent_cap(self):
        gate = asyncio.Event()
        made: list[GateWorker] = []
        pool = ReviewPool(
            max_workers=2, max_starting=2,
            worker_factory=lambda: _mk(made, GateWorker(gate)),
        )
        tasks = [asyncio.create_task(pool.send(f"t{i}")) for i in range(4)]
        await asyncio.sleep(0.05)
        # only 2 tasks may run at once -> only 2 workers ever created
        self.assertEqual(pool.created_count, 2)
        self.assertEqual(len(made), 2)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertEqual(len(made), 2)            # remaining tasks reused the 2 workers

    async def test_startup_throttled_to_max_starting(self):
        gate = asyncio.Event()
        counter = {"now": 0, "max": 0}
        pool = ReviewPool(
            max_workers=5, max_starting=2,
            worker_factory=lambda: StartGateWorker(gate, counter),
        )
        tasks = [asyncio.create_task(pool.send(f"t{i}")) for i in range(5)]
        await asyncio.sleep(0.05)
        self.assertEqual(counter["now"], 2)       # exactly 2 cold-starting at once
        self.assertLessEqual(counter["max"], 2)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertLessEqual(counter["max"], 2)   # never exceeded the start cap

    async def test_dead_idle_worker_replaced(self):
        made: list[FakeWorker] = []
        pool = ReviewPool(worker_factory=lambda: _mk(made, FakeWorker()))
        await pool.send("a")
        self.assertEqual(len(made), 1)
        made[0].live = False                      # worker dies while idle
        await pool.send("b")
        self.assertEqual(len(made), 2)            # dead idle worker replaced
        self.assertGreaterEqual(made[0].shutdowns, 1)
        self.assertEqual(pool.created_count, 1)   # exactly one live worker

    async def test_failed_task_retires_worker(self):
        made: list[FailingWorker] = []
        pool = ReviewPool(worker_factory=lambda: _mk(made, FailingWorker()))
        with self.assertRaises(RuntimeError):
            await pool.send("x")
        self.assertEqual(pool.created_count, 0)   # poisoned worker retired, not idled
        self.assertEqual(pool.idle_count, 0)
        self.assertEqual(made[0].shutdowns, 1)

    async def test_shutdown_retires_idle_workers(self):
        made: list[FakeWorker] = []
        pool = ReviewPool(worker_factory=lambda: _mk(made, FakeWorker()))
        await pool.send("a")
        self.assertEqual(pool.idle_count, 1)
        await pool.shutdown()
        self.assertEqual(pool.idle_count, 0)
        self.assertEqual(made[0].shutdowns, 1)

    async def test_pool_stats_reports_occupancy(self):
        await shutdown_pool()                   # ensure no singleton from other tests
        st = pool_stats()
        self.assertEqual(st["max"], MAX_CONCURRENT)
        self.assertEqual(st["starting_max"], MAX_STARTING)
        self.assertEqual(st["workers"], 0)
        self.assertEqual(st["busy"], 0)
        self.assertEqual(st["idle"], 0)

    async def test_acquire_after_shutdown_raises(self):
        pool = ReviewPool(worker_factory=lambda: FakeWorker())
        await pool.shutdown()
        with self.assertRaises(RuntimeError):
            await pool.acquire()

    async def test_cold_start_failure_releases_permit_and_slot(self):
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("spawn boom")    # first cold-start fails
            return FakeWorker()
        pool = ReviewPool(max_workers=1, worker_factory=factory)
        with self.assertRaises(RuntimeError):
            await pool.send("a")
        # The failed create must not leak the concurrency permit or the slot.
        self.assertEqual(pool.created_count, 0)
        out = await pool.send("b")                  # pool still usable
        self.assertEqual(out, "ok:b")


class TestSyncDispatchBridge(unittest.TestCase):
    """make_sync_dispatch bridges the threaded driver to the async pool."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    def _shutdown(self, pool):
        asyncio.run_coroutine_threadsafe(pool.shutdown(), self.loop).result(timeout=5)

    def test_dispatch_ok(self):
        pool = ReviewPool(worker_factory=lambda: FakeWorker())
        dispatch = make_sync_dispatch(self.loop, pool, default_timeout=5)
        out = dispatch("hello", 5)
        self.assertTrue(out["ok"])
        self.assertEqual(out["output"], "ok:hello")
        self.assertEqual(out["error"], "")
        self._shutdown(pool)

    def test_dispatch_error_never_raises(self):
        pool = ReviewPool(worker_factory=lambda: FailingWorker())
        dispatch = make_sync_dispatch(self.loop, pool, default_timeout=5)
        out = dispatch("x", 5)
        self.assertFalse(out["ok"])
        self.assertIn("boom", out["error"])
        self._shutdown(pool)


class TestReviewAgentResolution(unittest.TestCase):
    """The pool prefers the dedicated reviewer agent but degrades to kirocrew
    when it isn't installed (older builds)."""

    def test_fallback_to_kirocrew_when_dedicated_missing(self):
        # A name with no ~/.kiro/agents/<name>.json on disk -> fall back.
        self.assertEqual(
            _resolve_review_agent("definitely-not-installed-xyz"), "kirocrew")

    def test_review_work_dir_is_app_root(self):
        # Workers must run with cwd = app root so relative prompt paths
        # (sage_lib/pipeline.py, data/results/<id>.json) resolve where the driver reads.
        wd = _review_work_dir()
        self.assertIsNotNone(wd)
        self.assertTrue(wd.replace("\\", "/").endswith("apps/code-review-sage"))


class TestReviewEffort(unittest.TestCase):
    """Pool workers must run at MAX thinking effort for both the design gate and
    the deep review. On the kiro-cli backend (the pool default) effort is applied
    via a per-model workspace cli.json overlay written before spawn."""

    def test_review_effort_is_max(self):
        self.assertEqual(REVIEW_EFFORT, "max")

    def test_write_effort_overlay_writes_max_for_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_effort_overlay(tmp, "claude-sonnet-4.6")
            cli = Path(tmp) / ".kiro" / "settings" / "cli.json"
            self.assertTrue(cli.is_file(), "overlay cli.json not written")
            data = json.loads(cli.read_text(encoding="utf-8"))
            effort = (data["chat.modelDefaults"]["claude-sonnet-4.6"]
                      ["output_config"]["effort"])
            self.assertEqual(effort, REVIEW_EFFORT)
            self.assertEqual(effort, "max")

    def test_write_effort_overlay_is_merge_safe(self):
        # An existing cli.json (other models / unrelated keys) must be preserved.
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / ".kiro" / "settings"
            settings.mkdir(parents=True)
            (settings / "cli.json").write_text(json.dumps({
                "chat.modelDefaults": {"other-model": {"output_config": {"effort": "low"}}},
                "unrelated.key": 42,
            }), encoding="utf-8")
            _write_effort_overlay(tmp, "claude-sonnet-4.6")
            data = json.loads((settings / "cli.json").read_text(encoding="utf-8"))
            # New model added at max, existing model + unrelated key untouched.
            self.assertEqual(
                data["chat.modelDefaults"]["claude-sonnet-4.6"]["output_config"]["effort"],
                "max")
            self.assertEqual(
                data["chat.modelDefaults"]["other-model"]["output_config"]["effort"],
                "low")
            self.assertEqual(data["unrelated.key"], 42)

    def test_write_effort_overlay_never_raises(self):
        # Best-effort: a bad path must not raise (effort is a quality knob).
        # NUL byte makes mkdir/open fail on Linux; must be swallowed.
        _write_effort_overlay("/proc/nonexistent/\x00bad", "claude-sonnet-4.6")

    def test_reviewer_model_falls_back_to_default(self):
        # Unknown agent -> no ~/.kiro/agents/<agent>.json -> default model.
        self.assertEqual(
            _reviewer_model("definitely-not-installed-xyz"), _DEFAULT_REVIEW_MODEL)

    def test_reviewer_info_reports_agent_model_and_effort(self):
        # Surfaced to the dashboard header so users see WHICH model + effort runs.
        info = reviewer_info()
        self.assertTrue(info.get("agent"))
        self.assertTrue(isinstance(info.get("model"), str) and info["model"])
        self.assertEqual(info.get("effort"), REVIEW_EFFORT)


if __name__ == "__main__":
    unittest.main()


class TestWorkerSweepProtection(unittest.TestCase):
    """AcpReviewWorker must expose its kiro-cli PID via ``pid()`` so the shared
    pool engine can shield it from the gateway's periodic orphan sweep. Without a
    shield a busy pool worker is classified as an orphan and SIGKILLed mid-review,
    which the driver reports as "ACP process exited (code=1)" (the reported bug).
    The register/unregister lifecycle itself lives in the engine — see
    test_worker_pool.TestSweepProtection."""

    def test_worker_exposes_live_pid_for_shielding(self):
        from sage_lib import review_pool as rp

        class _FakeClient:
            backend = "kiro"          # not ACP_BACKEND_CLAUDE -> skip claude effort push
            is_ready = True

            def __init__(self, *a, **k):
                self._pid = 4242

            async def ensure_ready(self):
                return None

            async def shutdown(self):
                return None

            def is_process_alive(self):
                return True

        orig_client = rp.AcpClient
        rp.AcpClient = _FakeClient
        try:
            async def _run():
                w = rp.AcpReviewWorker()
                self.assertIsNone(w.pid(), "no PID before start")
                await w.start()
                self.assertEqual(w.pid(), 4242, "pid() must report the live process")
                await w.shutdown()
                self.assertIsNone(w.pid(), "no PID after shutdown")

            asyncio.run(_run())
        finally:
            rp.AcpClient = orig_client
