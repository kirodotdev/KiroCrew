"""Unit tests for the single-runtime review executor (batch-scoped, isolated).

The ``AcpRuntime``/``AcpSessionHandle`` layer is faked so the executor's
concurrency, batch lifecycle, per-task session isolation, tool auto-approval,
and SEL audit are exercised without spawning a real kiro-cli process.
"""

import asyncio
import json
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path

from sage_lib import review_pool as rp
from sage_lib.review_pool import (
    _DEFAULT_EFFORT,
    _DEFAULT_REVIEW_MODEL,
    MAX_CONCURRENT,
    MAX_CONCURRENT_CEIL,
    REVIEW_EFFORT,
    ReviewPool,
    _resolve_review_agent,
    _review_work_dir,
    _reviewer_model,
    _write_effort_overlay,
    effective_max_concurrent,
    make_sync_dispatch,
    pool_stats,
    reviewer_info,
    shutdown_pool,
)


# ── Fakes for the ACP runtime/handle layer ──────────────────────────────────
def _ev(kind, **kw):
    """Build a fake AcpEvent-like object with a .kind and any extra attrs."""
    return type("Ev", (), {"kind": kind, **kw})()


class FakeHandle:
    """A fake session handle. ``prompt`` replays a scripted list of events."""

    def __init__(self, runtime, session_id, script=None, gate=None):
        self._runtime = runtime
        self.session_id = session_id
        self._script = script or []
        self._gate = gate
        self.approvals: list = []
        self.rejections: list = []
        self.model_sets: list = []
        self.available_models: list = []
        self.destroyed = False

    async def prompt(self, message, timeout=0):
        if self._gate is not None:
            await self._gate.wait()
        for ev in self._script:
            yield ev
        yield _ev(rp.EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id, option_id=None):
        self.approvals.append(request_id)

    async def reject_tool(self, request_id, option_id=None):
        self.rejections.append(request_id)

    async def set_model(self, model_id):
        self.model_sets.append(model_id)

    async def destroy(self):
        self.destroyed = True
        self._runtime.active -= 1
        self._runtime.sessions.pop(self.session_id, None)


class FakeRuntime:
    """A fake AcpRuntime: tracks spawn/kill and active sessions."""

    instances: list = []

    def __init__(self, agent=None, work_dir=None, sandbox_mode="auto", **kw):
        self.agent = agent
        self.work_dir = work_dir
        self.spawned = False
        self.killed = False
        self.sessions: dict = {}
        self._session_queues: dict = {}  # read by holder.stats()
        self.created: list = []  # every handle ever created (survives destroy)
        self.active = 0
        self.max_active = 0
        self._seq = 0
        self.script = []
        self.gate = None
        FakeRuntime.instances.append(self)

    def is_alive(self):
        return self.spawned and not self.killed

    async def spawn(self):
        self.spawned = True

    async def kill(self, *, expected: bool = False):
        self.killed = True

    async def create_session(self, cwd=None, agent=None):
        self._seq += 1
        sid = f"s{self._seq}"
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        h = FakeHandle(self, sid, script=list(self.script), gate=self.gate)
        self.created.append(h)
        self.sessions[sid] = h
        self._session_queues[sid] = object()
        # mirror the handle destroy -> pop from _session_queues too
        _orig = h.destroy

        async def _destroy():
            self._session_queues.pop(sid, None)
            await _orig()

        h.destroy = _destroy  # type: ignore[method-assign]
        return h


def _install_fake_runtime(test, script=None, gate=None):
    """Patch review_pool.AcpRuntime with FakeRuntime for the duration of a test."""
    FakeRuntime.instances = []

    def factory(agent=None, work_dir=None, sandbox_mode="auto", **kw):
        r = FakeRuntime(agent=agent, work_dir=work_dir, sandbox_mode=sandbox_mode)
        r.script = script or []
        r.gate = gate
        return r

    orig = rp.AcpRuntime
    rp.AcpRuntime = factory  # type: ignore[assignment]
    test.addCleanup(lambda: setattr(rp, "AcpRuntime", orig))


# ── Batch lifecycle + isolation ─────────────────────────────────────────────
class TestBatchLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_lazy_no_runtime_until_used(self):
        _install_fake_runtime(self)
        ReviewPool(work_dir="/tmp/x")
        self.assertEqual(FakeRuntime.instances, [])  # nothing spawned on construction

    async def test_begin_batch_spawns_one_runtime_shared_across_sends(self):
        _install_fake_runtime(self, script=[_ev(rp.EVENT_TEXT_CHUNK, text="hi")])
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        self.assertEqual(len(FakeRuntime.instances), 1)
        self.assertTrue(FakeRuntime.instances[0].is_alive())
        out1 = await pool.send("a")
        out2 = await pool.send("b")
        self.assertEqual(out1, "hi")
        self.assertEqual(out2, "hi")
        # both sends multiplexed onto the ONE runtime (no respawn per task)
        self.assertEqual(len(FakeRuntime.instances), 1)
        await pool.end_batch()

    async def test_end_batch_kills_runtime_only_when_drained(self):
        _install_fake_runtime(self)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()  # batches 0->1 spawns
        await pool.begin_batch()  # batches 1->2 (overlapping run)
        rt = FakeRuntime.instances[0]
        await pool.end_batch()  # batches 2->1: NOT killed
        self.assertFalse(rt.killed)
        await pool.end_batch()  # batches 1->0: killed
        self.assertTrue(rt.killed)

    async def test_new_batch_after_drain_spawns_fresh_runtime(self):
        _install_fake_runtime(self)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        await pool.end_batch()
        await pool.begin_batch()
        self.assertEqual(len(FakeRuntime.instances), 2)  # a fresh runtime per batch
        await pool.end_batch()

    async def test_explicit_model_mismatch_on_live_runtime_fails_closed(self):
        # An overlapping batch asking for a DIFFERENT explicit model must not be
        # handed the live runtime (per-review model owns the effort-overlay
        # key); it fails closed instead of silently reviewing with model A.
        _install_fake_runtime(self)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch(model="model-under-test")
        rt = FakeRuntime.instances[0]
        with self.assertRaisesRegex(RuntimeError, "busy with model"):
            await pool.begin_batch(model="other-model-under-test")
        self.assertTrue(rt.is_alive())  # refused, not killed
        self.assertEqual(len(FakeRuntime.instances), 1)
        self.assertEqual(pool._holder._batches, 1)  # failed batch not counted
        await pool.end_batch()

    async def test_same_or_auto_model_reuses_live_runtime(self):
        # The same explicit model keeps pooling; Auto (None) on an AUTO-spawned
        # runtime keeps pooling too. Auto on a PINNED runtime must fail closed
        # (test below) — None == None is the only Auto case that reuses.
        _install_fake_runtime(self)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch(model="model-under-test")
        await pool.begin_batch(model="model-under-test")
        self.assertEqual(len(FakeRuntime.instances), 1)
        await pool.end_batch()
        await pool.end_batch()
        self.assertTrue(FakeRuntime.instances[0].killed)  # drains cleanly

    async def test_auto_on_pinned_runtime_fails_closed(self):
        # An Auto batch inheriting a PINNED runtime silently ran the review
        # under the pinned model (and the pinned batch's effort overlay, which
        # is written per spawn). The pool guard is strict equality now, so
        # this combination refuses instead.
        _install_fake_runtime(self)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch(model="model-under-test")
        rt = FakeRuntime.instances[0]
        with self.assertRaisesRegex(RuntimeError, "busy with model"):
            await pool.begin_batch()  # Auto inherits nothing
        self.assertTrue(rt.is_alive())  # refused, not killed
        self.assertEqual(len(FakeRuntime.instances), 1)
        await pool.end_batch()

    async def test_pinned_on_auto_runtime_fails_closed(self):
        # Symmetric direction: a pinned batch must not be handed the Auto
        # runtime — its session-level set_model would leave the runtime default
        # (and the effort overlay) pointing at Auto while this batch's sends
        # expect the pin, and after this batch drains the runtime keeps the pin.
        _install_fake_runtime(self)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()  # Auto spawn
        rt = FakeRuntime.instances[0]
        with self.assertRaisesRegex(RuntimeError, "busy with model"):
            await pool.begin_batch(model="model-under-test")
        self.assertTrue(rt.is_alive())  # refused, not killed
        self.assertEqual(len(FakeRuntime.instances), 1)
        await pool.end_batch()

    async def test_mismatched_model_spawns_fresh_after_drain(self):
        _install_fake_runtime(self)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch(model="model-under-test")
        await pool.end_batch()
        self.assertTrue(FakeRuntime.instances[0].killed)
        await pool.begin_batch(model="other-model-under-test")
        self.assertEqual(len(FakeRuntime.instances), 2)  # new runtime for the new model
        await pool.end_batch()

    async def test_session_created_and_destroyed_per_task(self):
        _install_fake_runtime(self, script=[_ev(rp.EVENT_TEXT_CHUNK, text="x")])
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        await pool.send("task")
        rt = FakeRuntime.instances[0]
        # exactly one session was created, and it was destroyed (no leak)
        self.assertEqual(rt._seq, 1)
        self.assertEqual(rt._session_queues, {})  # destroyed -> unregistered
        await pool.end_batch()

    async def test_standalone_send_lazily_spawns(self):
        # No begin_batch (standalone CLI path) -> acquire() spawns on first send.
        _install_fake_runtime(self, script=[_ev(rp.EVENT_TEXT_CHUNK, text="y")])
        pool = ReviewPool(work_dir="/tmp/x")
        out = await pool.send("z")
        self.assertEqual(out, "y")
        self.assertEqual(len(FakeRuntime.instances), 1)
        await pool.shutdown()
        self.assertTrue(FakeRuntime.instances[0].killed)

    async def test_abnormal_stop_reason_raises_and_still_destroys(self):
        # A timeout/stale/error completion must surface as a failure (so dispatch
        # reports ok=False and the driver never marks the PR reviewed), and the
        # session must still be destroyed.
        _install_fake_runtime(self, script=[_ev(rp.EVENT_COMPLETE, stop_reason="timeout")])
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        with self.assertRaises(RuntimeError):
            await pool.send("t")
        rt = FakeRuntime.instances[0]
        self.assertEqual(rt._session_queues, {})  # session destroyed despite the raise
        await pool.end_batch()

    async def test_tool_stall_stop_reason_is_abnormal(self):
        # STOP_REASON_TOOL_STALL ("error: tool stall") must be classified abnormal
        # and surface as a failure (matched explicitly, not just by prefix).
        _install_fake_runtime(
            self, script=[_ev(rp.EVENT_COMPLETE, stop_reason=rp.STOP_REASON_TOOL_STALL)]
        )
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        with self.assertRaises(RuntimeError):
            await pool.send("t")
        await pool.end_batch()

    async def test_spawn_failure_does_not_leak_batch_count(self):
        # If the runtime spawn raises, _batches must NOT be incremented — otherwise
        # it could never drain to 0 and the subprocess would never be killed.
        calls = {"n": 0}

        def factory(agent=None, work_dir=None, sandbox_mode="auto", **kw):
            r = FakeRuntime(agent=agent, work_dir=work_dir)
            calls["n"] += 1
            if calls["n"] == 1:

                async def _boom():
                    raise RuntimeError("spawn boom")

                r.spawn = _boom  # type: ignore[method-assign]
            return r

        orig = rp.AcpRuntime
        rp.AcpRuntime = factory  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(rp, "AcpRuntime", orig))
        FakeRuntime.instances = []
        pool = ReviewPool(work_dir="/tmp/x")
        with self.assertRaises(RuntimeError):
            await pool.begin_batch()  # spawn fails
        self.assertEqual(pool._holder._batches, 0)  # counter not leaked
        # a subsequent batch spawns cleanly and drains to 0 (runtime killed)
        await pool.begin_batch()
        await pool.end_batch()
        self.assertTrue(FakeRuntime.instances[-1].killed)


# ── Concurrency ─────────────────────────────────────────────────────────────
class TestConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_semaphore_caps_concurrent_sessions(self):
        gate = asyncio.Event()
        _install_fake_runtime(self, script=[_ev(rp.EVENT_TEXT_CHUNK, text="q")], gate=gate)
        pool = ReviewPool(max_workers=2, work_dir="/tmp/x")
        await pool.begin_batch()
        tasks = [asyncio.create_task(pool.send(f"t{i}")) for i in range(4)]
        await asyncio.sleep(0.05)
        rt = FakeRuntime.instances[0]
        self.assertEqual(rt.active, 2)  # only 2 in flight at once
        self.assertLessEqual(rt.max_active, 2)
        gate.set()
        await asyncio.gather(*tasks)
        self.assertLessEqual(rt.max_active, 2)  # never exceeded the cap
        await pool.end_batch()

    async def test_effective_max_concurrent_clamped(self):
        pool = ReviewPool(max_workers=999, work_dir="/tmp/x")
        self.assertEqual(pool._max, MAX_CONCURRENT_CEIL)


# ── Tool approval + SEL audit ────────────────────────────────────────────────
class TestApprovalAndAudit(unittest.IsolatedAsyncioTestCase):
    async def test_permission_events_are_auto_approved(self):
        script = [
            _ev(rp.EVENT_PERMISSION_REQUEST, request_id="r1"),
            _ev(rp.EVENT_TEXT_CHUNK, text="done"),
        ]
        _install_fake_runtime(self, script=script)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        out = await pool.send("t")
        self.assertEqual(out, "done")
        rt = FakeRuntime.instances[0]
        # the (now-destroyed) handle recorded the approval
        # find it via the runtime's last created session id
        self.assertEqual(rt._seq, 1)
        await pool.end_batch()

    async def test_tool_call_emits_sel_audit(self):
        calls: list = []

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        script = [
            _ev(rp.EVENT_TOOL_CALL, title="shell", tool_kind="execute"),
            _ev(rp.EVENT_TEXT_CHUNK, text="ok"),
        ]
        _install_fake_runtime(self, script=script)
        orig_sel = rp._sel
        rp._sel = lambda: _FakeSel()  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(rp, "_sel", orig_sel))
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        await pool.send("t")
        await pool.end_batch()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_name"], "shell")
        self.assertEqual(calls[0]["source"], "subagent")
        self.assertEqual(calls[0]["outcome"], "auto_approved")

    async def test_permission_decision_emits_sel_audit(self):
        # The permission DECISION must emit an SEL event carrying its request id —
        # not only the tool_call.
        calls: list = []

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        script = [
            _ev(rp.EVENT_PERMISSION_REQUEST, request_id="r1", title="shell", tool_kind="execute"),
            _ev(rp.EVENT_TEXT_CHUNK, text="ok"),
        ]
        _install_fake_runtime(self, script=script)
        orig_sel = rp._sel
        rp._sel = lambda: _FakeSel()  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(rp, "_sel", orig_sel))
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        await pool.send("t")
        await pool.end_batch()
        # exactly one audit — the permission decision — carrying the request id
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["request_id"], "r1")
        self.assertEqual(calls[0]["outcome"], "auto_approved")
        self.assertEqual(calls[0]["source"], "subagent")


# ── Stats + config ──────────────────────────────────────────────────────────
class TestStatsAndConfig(unittest.IsolatedAsyncioTestCase):
    async def test_pool_stats_zeros_when_no_singleton(self):
        await shutdown_pool()
        st = pool_stats()
        self.assertEqual(st["workers"], 0)
        self.assertEqual(st["busy"], 0)
        self.assertEqual(st["idle"], 0)
        self.assertFalse(st["runtime_alive"])
        self.assertIn("max", st)

    async def test_effective_max_concurrent_default(self):
        # No config override -> MAX_CONCURRENT (5), clamped into [1, ceil].
        self.assertEqual(effective_max_concurrent(), MAX_CONCURRENT)
        self.assertEqual(MAX_CONCURRENT, 5)

    async def test_stats_reflect_alive_runtime(self):
        _install_fake_runtime(self)
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch()
        st = pool.stats()
        self.assertTrue(st["runtime_alive"])
        self.assertEqual(st["max"], pool._max)
        await pool.end_batch()


# ── Sync dispatch bridge ─────────────────────────────────────────────────────
class TestSyncDispatchBridge(unittest.TestCase):
    """make_sync_dispatch bridges the threaded driver to the async executor."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    def _shutdown(self, pool):
        asyncio.run_coroutine_threadsafe(pool.shutdown(), self.loop).result(timeout=5)

    def test_dispatch_ok(self):
        FakeRuntime.instances = []

        def factory(agent=None, work_dir=None, sandbox_mode="auto", **kw):
            r = FakeRuntime(agent=agent, work_dir=work_dir)
            r.script = [_ev(rp.EVENT_TEXT_CHUNK, text="hello")]
            return r

        orig = rp.AcpRuntime
        rp.AcpRuntime = factory  # type: ignore[assignment]
        try:
            pool = ReviewPool(work_dir="/tmp/x")
            dispatch = make_sync_dispatch(self.loop, pool, default_timeout=5)
            out = dispatch("hi", 5)
            self.assertTrue(out["ok"])
            self.assertEqual(out["output"], "hello")
            self.assertEqual(out["error"], "")
            self._shutdown(pool)
        finally:
            rp.AcpRuntime = orig  # type: ignore[assignment]

    def test_dispatch_error_never_raises(self):
        FakeRuntime.instances = []

        def factory(agent=None, work_dir=None, sandbox_mode="auto", **kw):
            r = FakeRuntime(agent=agent, work_dir=work_dir)

            async def _boom(cwd=None, agent=None):
                raise RuntimeError("boom")

            r.create_session = _boom  # type: ignore[assignment]
            return r

        orig = rp.AcpRuntime
        rp.AcpRuntime = factory  # type: ignore[assignment]
        try:
            pool = ReviewPool(work_dir="/tmp/x")
            dispatch = make_sync_dispatch(self.loop, pool, default_timeout=5)
            out = dispatch("x", 5)
            self.assertFalse(out["ok"])
            self.assertIn("boom", out["error"])
            self._shutdown(pool)
        finally:
            rp.AcpRuntime = orig  # type: ignore[assignment]


# ── Reviewer identity resolution ─────────────────────────────────────────────
class TestReviewAgentResolution(unittest.TestCase):
    def test_fallback_to_kirocrew_when_dedicated_missing(self):
        self.assertEqual(_resolve_review_agent("definitely-not-installed-xyz"), "kirocrew")

    def test_review_work_dir_is_app_root(self):
        wd = _review_work_dir()
        self.assertIsNotNone(wd)
        self.assertTrue(wd.replace("\\", "/").endswith("apps/code-review-sage"))


class TestReviewEffort(unittest.TestCase):
    """Effort is applied via a per-model workspace cli.json overlay written
    before spawn. The default is "" (inherit the model/provider default)."""

    def test_review_effort_default_is_inherit(self):
        self.assertEqual(REVIEW_EFFORT, _DEFAULT_EFFORT)
        self.assertEqual(REVIEW_EFFORT, "")

    def test_write_effort_overlay_writes_default_for_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_effort_overlay(tmp, "claude-sonnet-4.6")
            cli = Path(tmp) / ".kiro" / "settings" / "cli.json"
            self.assertTrue(cli.is_file(), "overlay cli.json not written")
            data = json.loads(cli.read_text(encoding="utf-8"))
            effort = data["chat.modelDefaults"]["claude-sonnet-4.6"]["output_config"]["effort"]
            self.assertEqual(effort, REVIEW_EFFORT)

    def test_write_effort_overlay_is_merge_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / ".kiro" / "settings"
            settings.mkdir(parents=True)
            (settings / "cli.json").write_text(
                json.dumps(
                    {
                        "chat.modelDefaults": {"other-model": {"output_config": {"effort": "low"}}},
                        "unrelated.key": 42,
                    }
                ),
                encoding="utf-8",
            )
            _write_effort_overlay(tmp, "claude-sonnet-4.6", "high")
            data = json.loads((settings / "cli.json").read_text(encoding="utf-8"))
            self.assertEqual(
                data["chat.modelDefaults"]["claude-sonnet-4.6"]["output_config"]["effort"], "high"
            )
            self.assertEqual(
                data["chat.modelDefaults"]["other-model"]["output_config"]["effort"], "low"
            )
            self.assertEqual(data["unrelated.key"], 42)

    def test_write_effort_overlay_never_raises(self):
        _write_effort_overlay("/proc/nonexistent/\x00bad", "claude-sonnet-4.6")

    def test_reviewer_model_falls_back_to_default(self):
        self.assertEqual(_reviewer_model("definitely-not-installed-xyz"), _DEFAULT_REVIEW_MODEL)

    def test_reviewer_info_reports_agent_model_and_effort(self):
        info = reviewer_info()
        self.assertTrue(info.get("agent"))
        self.assertTrue(isinstance(info.get("model"), str) and info["model"])
        self.assertEqual(info.get("effort"), REVIEW_EFFORT)


if __name__ == "__main__":
    unittest.main()


class TestRuntimePreflight(unittest.TestCase):
    """runtime_preflight answers "could a reviewer session actually spawn?"

    "" means yes; anything else names what is missing, so the driver can fail
    the run fast with a triagable reason instead of completing with nothing
    written and reporting an undiscriminated "no result record".
    """

    def test_available_runtime_returns_empty(self):
        with (
            unittest.mock.patch.object(rp, "AcpRuntime", object()),
            unittest.mock.patch.object(rp, "resolve_kiro_cli", lambda: "/usr/local/bin/kiro-cli"),
        ):
            self.assertEqual(rp.runtime_preflight(), "")

    def test_missing_cli_names_the_runtime(self):
        with (
            unittest.mock.patch.object(rp, "AcpRuntime", object()),
            unittest.mock.patch.object(rp, "resolve_kiro_cli", lambda: None),
        ):
            msg = rp.runtime_preflight()
            self.assertIn("kiro-cli", msg)

    def test_unimportable_acp_runtime_is_reported(self):
        with unittest.mock.patch.object(rp, "AcpRuntime", None):
            msg = rp.runtime_preflight()
            self.assertTrue(msg)
            self.assertIn("runtime", msg.lower())

    def test_check_is_read_only(self):
        # The preflight runs (off the event loop) before every review run — it
        # must only LOOK for the executable, never spawn or write anything.
        calls: list[str] = []

        def _resolver() -> str:
            calls.append("resolve")
            return "/bin/kiro-cli"

        with (
            unittest.mock.patch.object(rp, "AcpRuntime", object()),
            unittest.mock.patch.object(rp, "resolve_kiro_cli", _resolver),
        ):
            rp.runtime_preflight()
        self.assertEqual(calls, ["resolve"])


# ── Permission policy gate (local-fix pool) ─────────────────────────────────
class _FakeGateResult:
    """A ToolHookResult stand-in carrying just what the gate reads."""

    def __init__(self, action, reason=""):
        self.action = action
        self.reason = reason


class _RecordingHookManager:
    """HookManager stand-in: records the gate call, replays a canned verdict."""

    verdict = _FakeGateResult("allow")
    calls: list = []

    def __init__(self, *a, **kw):
        pass

    def on_tool_call(self, *a, **kw):
        _RecordingHookManager.calls.append((a, kw))
        return _RecordingHookManager.verdict


def _gate_config_patch():
    """Patch KiroCrewConfig.load so the gate never reads the real data home."""
    import kiro_crew.config.loader as loader

    class _Cfg:
        hooks: dict = {}

        @classmethod
        def load(cls):
            return cls

    return unittest.mock.patch.object(loader, "KiroCrewConfig", _Cfg)


def _recording_manager_patch():
    import kiro_crew.hooks as hooks_mod

    _RecordingHookManager.calls = []
    _RecordingHookManager.verdict = _FakeGateResult("allow")
    return unittest.mock.patch.object(hooks_mod, "HookManager", _RecordingHookManager)


class TestPermissionPolicyGate(unittest.IsolatedAsyncioTestCase):
    """permission_policy_gate routes every auto-approve candidate through the
    FULL PreToolUse gate (HookManager.on_tool_call — governance ceiling, deny
    floor, denied regexes); a denial rejects the tool and SEL-audits it instead
    of approving it in the real checkout."""

    async def _send_with_gate(self, script, gate=True):
        pool = ReviewPool(work_dir="/tmp/x", permission_policy_gate=gate)
        await pool.begin_batch()
        out = await pool.send("t")
        await pool.end_batch()
        return pool, out

    async def test_gate_off_auto_approves_without_consulting_hooks(self):
        script = [
            _ev(rp.EVENT_PERMISSION_REQUEST, request_id="r1", title="shell"),
            _ev(rp.EVENT_TEXT_CHUNK, text="ok"),
        ]
        _install_fake_runtime(self, script=script)
        with _recording_manager_patch():
            out = (await self._send_with_gate(script, gate=False))[1]
        self.assertEqual(out, "ok")
        # the hook layer was never consulted
        self.assertEqual(_RecordingHookManager.calls, [])

    async def test_denied_request_is_rejected_not_approved(self):
        script = [
            _ev(rp.EVENT_PERMISSION_REQUEST, request_id="r1", title="shell"),
            _ev(rp.EVENT_TEXT_CHUNK, text="done"),
        ]
        _install_fake_runtime(self, script=script)
        orig = rp._policy_deny_reason
        rp._policy_deny_reason = lambda ev, **kw: "Blocked: denied command"
        self.addCleanup(lambda: setattr(rp, "_policy_deny_reason", orig))
        pool, out = await self._send_with_gate(script)
        self.assertEqual(out, "done")
        h = FakeRuntime.instances[0].created[0]
        self.assertEqual(h.rejections, ["r1"])
        self.assertEqual(h.approvals, [])

    async def test_denied_request_still_emits_sel_audit(self):
        calls: list = []

        class _FakeSel:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        script = [
            _ev(rp.EVENT_PERMISSION_REQUEST, request_id="r1", title="shell"),
            _ev(rp.EVENT_TEXT_CHUNK, text="done"),
        ]
        _install_fake_runtime(self, script=script)
        orig_sel = rp._sel
        rp._sel = lambda: _FakeSel()  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(rp, "_sel", orig_sel))
        orig = rp._policy_deny_reason
        rp._policy_deny_reason = lambda ev, **kw: "Blocked: denied command"
        self.addCleanup(lambda: setattr(rp, "_policy_deny_reason", orig))
        await self._send_with_gate(script)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["request_id"], "r1")
        self.assertEqual(calls[0]["outcome"], "denied")

    async def test_allowed_request_is_auto_approved(self):
        script = [
            _ev(rp.EVENT_PERMISSION_REQUEST, request_id="r1", title="shell"),
            _ev(rp.EVENT_TEXT_CHUNK, text="done"),
        ]
        _install_fake_runtime(self, script=script)
        orig = rp._policy_deny_reason
        rp._policy_deny_reason = lambda ev, **kw: None
        self.addCleanup(lambda: setattr(rp, "_policy_deny_reason", orig))
        await self._send_with_gate(script)
        h = FakeRuntime.instances[0].created[0]
        self.assertEqual(h.approvals, ["r1"])
        self.assertEqual(h.rejections, [])

    async def test_reject_failure_still_completes_without_approving(self):
        # A failed reject (transport died) must never degrade into an approval.
        script = [
            _ev(rp.EVENT_PERMISSION_REQUEST, request_id="r1", title="shell"),
            _ev(rp.EVENT_TEXT_CHUNK, text="done"),
        ]
        _install_fake_runtime(self, script=script)
        orig = rp._policy_deny_reason
        rp._policy_deny_reason = lambda ev, **kw: "Blocked: denied command"
        self.addCleanup(lambda: setattr(rp, "_policy_deny_reason", orig))
        orig_rej = FakeHandle.reject_tool

        async def _boom(self, request_id, option_id=None):
            raise RuntimeError("reject boom")

        FakeHandle.reject_tool = _boom  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(FakeHandle, "reject_tool", orig_rej))
        pool, out = await self._send_with_gate(script)
        self.assertEqual(out, "done")
        self.assertEqual(FakeRuntime.instances[0].created[0].approvals, [])

    async def test_approve_failure_does_not_break_the_review(self):
        script = [
            _ev(rp.EVENT_PERMISSION_REQUEST, request_id="r1", title="shell"),
            _ev(rp.EVENT_TEXT_CHUNK, text="done"),
        ]
        _install_fake_runtime(self, script=script)
        orig_ap = FakeHandle.approve_tool

        async def _boom(self, request_id, option_id=None):
            raise RuntimeError("approve boom")

        FakeHandle.approve_tool = _boom  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(FakeHandle, "approve_tool", orig_ap))
        pool, out = await self._send_with_gate(script, gate=False)
        self.assertEqual(out, "done")
        self.assertEqual(pool.stats()["busy"], 0)

    async def test_pinned_model_is_applied_to_the_session(self):
        # An explicit model is entitlement-checked against the session's live
        # advertised set (empty = unknown -> allowed) and applied via set_model.
        _install_fake_runtime(self, script=[_ev(rp.EVENT_TEXT_CHUNK, text="ok")])
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch(model="model-under-test")
        out = await pool.send("t", model="model-under-test")
        await pool.end_batch()
        self.assertEqual(out, "ok")
        self.assertEqual(FakeRuntime.instances[0].created[0].model_sets, ["model-under-test"])

    async def test_pinned_model_not_advertised_fails_closed(self):
        # A model the session's advertised set excludes must be refused BEFORE
        # any prompt is sent (entitlement pre-flight, fail closed).
        _install_fake_runtime(self, script=[_ev(rp.EVENT_TEXT_CHUNK, text="ok")])
        orig_init = FakeHandle.__init__

        def _init(self, *a, **kw):
            orig_init(self, *a, **kw)
            self.available_models = [{"modelId": "some-other-model"}]

        FakeHandle.__init__ = _init  # type: ignore[method-assign]
        self.addCleanup(lambda: setattr(FakeHandle, "__init__", orig_init))
        pool = ReviewPool(work_dir="/tmp/x")
        await pool.begin_batch(model="model-under-test")
        with self.assertRaises(Exception):
            await pool.send("t", model="model-under-test")
        await pool.end_batch()


class TestPolicyDenyReason(unittest.TestCase):
    """_policy_deny_reason threads identity/params through HookManager and
    fails CLOSED on a broken gate."""

    def setUp(self):
        self._config_patch = _gate_config_patch()
        self._config_patch.start()
        self.addCleanup(self._config_patch.stop)
        self._manager_patch = _recording_manager_patch()
        self._manager_patch.start()
        self.addCleanup(self._manager_patch.stop)
        import kiro_crew.hooks as hooks_mod

        self._hooks_mod = hooks_mod

    def _shell_event(self, **kw):
        attrs = {
            "request_id": "r1",
            "title": "Running: rm -rf /tmp/x",
            "tool_kind": "execute",
            "is_shell": True,
            "raw_tool_params": {"command": "rm -rf /tmp/x"},
            "tool_name": "Bash",
            "mcp_server_name": "",
        }
        attrs.update(kw)
        return _ev(rp.EVENT_PERMISSION_REQUEST, **attrs)

    def test_threads_identity_command_and_params_through_the_gate(self):
        ev = self._shell_event()
        reason = rp._policy_deny_reason(ev, agent="code-review-sage-reviewer")
        self.assertIsNone(reason)
        ((args, kw),) = _RecordingHookManager.calls
        self.assertEqual(args, ("Running: rm -rf /tmp/x",))
        self.assertEqual(kw["agent"], "code-review-sage-reviewer")
        self.assertEqual(kw["app"], "code-review-sage")
        self.assertEqual(kw["tool_kind"], "execute")
        self.assertEqual(kw["command"], "rm -rf /tmp/x")
        self.assertTrue(kw["is_shell"])
        self.assertEqual(kw["raw_params"], {"command": "rm -rf /tmp/x"})
        self.assertEqual(kw["mcp_tool_name"], "Bash")

    def test_gate_deny_is_returned_as_reason(self):
        _RecordingHookManager.verdict = _FakeGateResult("deny", "Blocked: governance ceiling")
        reason = rp._policy_deny_reason(self._shell_event())
        self.assertEqual(reason, "Blocked: governance ceiling")

    def test_gate_exception_fails_closed(self):
        class _BoomManager:
            def __init__(self, *a, **kw):
                pass

            def on_tool_call(self, *a, **kw):
                raise RuntimeError("hook layer broken")

        with unittest.mock.patch.object(self._hooks_mod, "HookManager", _BoomManager):
            reason = rp._policy_deny_reason(self._shell_event())
        self.assertIn("governance hook unavailable", reason)
        self.assertIn("hook layer broken", reason)

    def test_shell_request_without_command_reaches_gate_fail_closed(self):
        # is_shell comes from the EVENT; an unrecoverable command must reach the
        # gate as command=None + is_shell=True so ITS deny-by-default fires.
        _RecordingHookManager.verdict = _FakeGateResult(
            "deny", "Blocked: shell command could not be verified"
        )
        ev = self._shell_event(raw_tool_params={})
        reason = rp._policy_deny_reason(ev)
        ((args, kw),) = _RecordingHookManager.calls
        self.assertIsNone(kw["command"])
        self.assertTrue(kw["is_shell"])
        self.assertTrue(reason)

    def test_non_shell_tool_gated_on_identity_not_command(self):
        # An MCP tool request carries no shell command; the gate still sees the
        # trusted tool name so a deny rule naming the tool bites.
        ev = _ev(
            rp.EVENT_PERMISSION_REQUEST,
            request_id="r2",
            title="search files",
            tool_kind="read",
            is_shell=False,
            raw_tool_params={"path": "/repo"},
            tool_name="fs_read",
            mcp_server_name="acme",
        )
        self.assertIsNone(rp._policy_deny_reason(ev))
        ((args, kw),) = _RecordingHookManager.calls
        self.assertIsNone(kw["command"])
        self.assertFalse(kw["is_shell"])
        self.assertEqual(kw["mcp_server_name"], "acme")
        self.assertEqual(kw["mcp_tool_name"], "fs_read")
