"""Workflow warm session pool — proves it kills per-call cold-start.

The un-pooled path (``agent_exec.build_agent_fn``) cold-starts a fresh session
(``get_or_create``) for EVERY ``ctx.agent()`` call and tears it down. The pooled
path (``agent_pool.build_pooled_agent_fn``) keeps warm sessions and reuses them,
so N sequential calls trigger far fewer cold-starts than N. These tests assert
that reuse property directly (the mechanism that makes it faster) with a fake
SessionManager — no kiro-cli spawns.
"""

from __future__ import annotations

import asyncio

import pytest
from overload_fakes import settle_dependency_park, settle_store_writes

from kiro_crew.workflows.agent_pool import _WorkflowSessionWorker, build_pooled_agent_fn


class _FakeProvider:
    """Stands in for an ACP provider: counts cold starts vs cheap resets."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.new_conversation_calls = 0
        self.alive = True

    async def new_conversation(self) -> None:
        # Cheap warm reset — the win. No cold start here.
        self.new_conversation_calls += 1
        # Records the swap so a test can assert the child suppression was armed BEFORE the
        # conversation handle changed hands.
        sink = getattr(self, "order_sink", None)
        if sink is not None:
            sink.append("new_conversation")

    def is_process_alive(self) -> bool:
        return self.alive


class _FakeSessions:
    """Fake SessionManager. Records every get_or_create (== a cold start) and
    every release, so a test can compare cold-start count to call count."""

    def __init__(self) -> None:
        self.cold_starts = 0
        self.releases = 0
        self.resets = 0
        self.live: dict[str, _FakeProvider] = {}
        # Track concurrency: keys that are mid-turn simultaneously.
        self.keys_seen: set[str] = set()
        # Every (agent, model, cwd) identity a cold-started worker was created
        # with — so a test can assert per-call overrides reach get_or_create.
        self.created_identities: list[tuple] = []
        # extra_env seen on each cold start — index-aligned with
        # created_identities so a test can assert the run-level env pin threads through.
        self.created_extra_env: list = []

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None, extra_env=None):
        # A live key returns instantly (SessionManager's warm per-key fast path).
        if key in self.live:
            return self.live[key], False, False
        self.cold_starts += 1
        self.created_identities.append((agent, model, cwd))
        self.created_extra_env.append(extra_env)
        prov = _FakeProvider(tag=key)
        self.live[key] = prov
        self.keys_seen.add(key)
        return prov, True, False

    def release(self, key, *, cleanup=False):
        self.releases += 1
        if cleanup:
            self.live.pop(key, None)

    async def reset(self, key, *, ends_conversation=False):
        self.resets += 1
        self.reset_intents = getattr(self, "reset_intents", [])
        self.reset_intents.append(ends_conversation)
        self.live.pop(key, None)

    async def end_children_for(self, key):
        self.children_ended = getattr(self, "children_ended", [])
        self.children_ended.append(key)
        self.order = getattr(self, "order", [])
        self.order.append("end_children")

    async def destroy(self, key):
        self.destroys = getattr(self, "destroys", 0) + 1
        self.live.pop(key, None)


# stream_and_collect is patched to a no-op producer so no model is called.
async def _fake_stream(provider, prompt, **kwargs):
    # Return the worker tag so tests can see WHICH session served the call.
    await asyncio.sleep(0)  # yield, so concurrent tasks actually interleave
    return f"[{provider.tag}] {prompt}"


@pytest.fixture(autouse=True)
def _patch_stream(monkeypatch):
    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _fake_stream)
    # redaction is a no-op passthrough for these tests
    monkeypatch.setattr("kiro_crew.workflows.agent_pool.redact", lambda t: t)


@pytest.mark.asyncio
async def test_sequential_calls_reuse_one_warm_session():
    """8 SEQUENTIAL ctx.agent() calls → exactly 1 cold start (the rest reuse)."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="r1", max_workers=4, max_starting=2)
    for i in range(8):
        out = await agent_fn(f"task-{i}", {})
        assert out.startswith("[wf-pool:r1:")

    # THE WIN: 8 calls, 1 cold start (vs 8 in the un-pooled path).
    assert sessions.cold_starts == 1, sessions.cold_starts
    # The single warm session served all 8 → 7 sequential reuses, each a cheap
    # new_conversation() reset (0 extra cold starts, 0 hard resets).
    assert len(sessions.live) == 1
    prov = next(iter(sessions.live.values()))
    assert prov.new_conversation_calls == 7, prov.new_conversation_calls
    assert sessions.resets == 0
    await pool.shutdown()


@pytest.mark.asyncio
async def test_reuse_triggers_cheap_reset_not_cold_start():
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="r2", max_workers=2)
    # Capture the provider created on first call.
    await agent_fn("first", {})
    assert sessions.cold_starts == 1
    created = list(sessions.live.values())
    assert len(created) == 1
    prov = created[0]
    # Second sequential call reuses it → new_conversation() (cheap), not a cold start.
    await agent_fn("second", {})
    assert sessions.cold_starts == 1  # still 1 — no new cold start
    assert prov.new_conversation_calls == 1  # reused via cheap reset
    await pool.shutdown()


@pytest.mark.asyncio
async def test_concurrent_calls_get_distinct_sessions():
    """Parallel ctx.agent() calls must run on DISTINCT sessions (isolation)."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="r3", max_workers=4)
    try:
        results = await asyncio.gather(*(agent_fn(f"p{i}", {}) for i in range(4)))
    finally:
        await pool.shutdown()
    # 4 concurrent tasks → 4 distinct worker sessions cold-started (isolation).
    tags = {r.split("]")[0] for r in results}
    assert len(tags) == 4, tags
    assert sessions.cold_starts == 4


@pytest.mark.asyncio
async def test_bounded_by_max_workers():
    """More concurrent tasks than max_workers → no more than max_workers cold
    starts (excess tasks queue and reuse released workers)."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="r4", max_workers=2)
    try:
        await asyncio.gather(*(agent_fn(f"t{i}", {}) for i in range(6)))
    finally:
        await pool.shutdown()
    # At most 2 live workers ever → at most 2 cold starts for 6 tasks.
    assert sessions.cold_starts <= 2, sessions.cold_starts


@pytest.mark.asyncio
async def test_identity_cap_falls_back_to_unpooled():
    """Distinct (agent, model, cwd) identities beyond max_identities do NOT each
    mint a fresh max_workers-sized pool (which would let a run with unique model
    strings spawn unbounded processes). Excess identities run unpooled — a
    create-run-DESTROY session that never lingers."""
    sessions = _FakeSessions()
    # cap=2 identities; default identity counts as one, so the 3rd+ distinct
    # model overflows to the unpooled path.
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="rc", max_workers=1, max_identities=2)
    try:
        # 5 distinct model identities, sequentially (each finishes before the
        # next), so warm reuse within an identity is irrelevant — this measures
        # identity fan-out, not concurrency.
        for i in range(5):
            await agent_fn(f"p{i}", {"model": f"model-{i}"})
    finally:
        await pool.shutdown()
    # The overflow identities were destroyed (not retained). At least the 3
    # beyond the 2-identity cap ran unpooled and were torn down.
    assert getattr(sessions, "destroys", 0) >= 3
    # No live sessions leaked after shutdown.
    assert sessions.live == {}


@pytest.mark.asyncio
async def test_stateful_session_bypasses_pool():
    """A session=<key> call uses a dedicated named session, NOT the pool."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="r5")
    try:
        out = await agent_fn("hi", {"session": "chain-A"})
        assert "[chain-A]" in out
        assert "chain-A" in sessions.live  # named session created directly
        provider = sessions.live["chain-A"]
        assert sessions.releases == 1  # lease returned, conversation retained
        assert "[chain-A]" in await agent_fn("again", {"session": "chain-A"})
        assert sessions.live["chain-A"] is provider
        assert sessions.cold_starts == 1
        assert sessions.releases == 2
    finally:
        await pool.shutdown()
        await sessions.destroy("chain-A")


@pytest.mark.asyncio
async def test_shutdown_releases_warm_sessions():
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="r6", max_workers=3)
    await asyncio.gather(*(agent_fn(f"t{i}", {}) for i in range(3)))
    assert sessions.cold_starts == 3
    assert sessions.releases == 0
    await pool.shutdown()
    # Every warm worker released on shutdown (no leaked sessions).
    assert sessions.releases == 3


# --------------------------------------------------------------------------- #
# Per-call agent=/model=/cwd= overrides (parity with build_agent_fn). The
# ephemeral path must honor ctx.agent(prompt, agent=…, model=…, cwd=…) instead
# of collapsing every call to the pool default — otherwise a multi-specialist
# fan-out (a primary dynamic-workflow use case) all runs as one agent/model.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_per_call_agent_model_override_reaches_get_or_create():
    """ctx.agent(prompt, agent=…, model=…) must thread that identity through to
    the worker's get_or_create — NOT silently use the pool default."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="ov1", max_workers=4)
    try:
        await agent_fn("research", {"agent": "researcher", "model": "claude-opus-4-8"})
    finally:
        await pool.shutdown()
    assert (
        "researcher",
        "claude-opus-4-8",
        None,
    ) in sessions.created_identities, sessions.created_identities


@pytest.mark.asyncio
async def test_distinct_specialists_get_distinct_warm_subpools():
    """Two different specialists → two identities cold-started; repeat calls to
    the SAME specialist reuse its warm sub-pool (no extra cold start)."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="ov2", max_workers=4)
    try:
        # researcher twice (sequential → 1 cold start + 1 warm reset), critic once.
        await agent_fn("r1", {"agent": "researcher"})
        await agent_fn("r2", {"agent": "researcher"})
        await agent_fn("c1", {"agent": "critic"})
    finally:
        await pool.shutdown()
    identities = set(sessions.created_identities)
    assert ("researcher", None, None) in identities
    assert ("critic", None, None) in identities
    # researcher reused its warm worker (2 calls, 1 cold start); critic 1 →
    # 2 cold starts total, researcher's 2nd call is a warm reset.
    assert sessions.cold_starts == 2, sessions.cold_starts


@pytest.mark.asyncio
async def test_no_override_uses_default_pool_single_cold_start():
    """Calls with no per-call override all share the default sub-pool (unchanged
    behavior — sequential reuse, one cold start)."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(
        sessions, run_id="ov3", default_agent="wf-default", max_workers=4
    )
    try:
        await agent_fn("a", {})
        await agent_fn("b", {})
        # An explicit agent= that EQUALS the default must also reuse the default pool.
        await agent_fn("c", {"agent": "wf-default"})
    finally:
        await pool.shutdown()
    assert sessions.cold_starts == 1, sessions.cold_starts
    assert sessions.created_identities == [("wf-default", None, None)]


@pytest.mark.asyncio
async def test_shutdown_releases_all_subpools():
    """pool.shutdown() must tear down the default AND every identity sub-pool."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="ov4", max_workers=4)
    await agent_fn("a", {})  # default pool
    await agent_fn("b", {"agent": "researcher"})  # researcher sub-pool
    await agent_fn("c", {"agent": "critic"})  # critic sub-pool
    assert sessions.cold_starts == 3
    assert sessions.releases == 0
    await pool.shutdown()
    assert sessions.releases == 3  # all three warm sessions released


# ── Modeled wall-clock: PROVE it's faster (deterministic, no real sleeps) ──

# Representative costs (ms): a cold start (subprocess spawn + ACP initialize +
# session/new MCP-toolset load) dominates; a warm reset (new_conversation =
# session/new only, no spawn/initialize) is a fraction of it; the per-turn model
# cost is equal on both paths so it cancels out of the comparison (set to 0 here
# to isolate the loading-time delta).
_COLD_START_MS = 8000.0  # ~8s cold-start component per agent (profiled)
_WARM_RESET_MS = 800.0  # session/new-only reset — ~10% of a cold start


class _ClockSessions:
    """Fake SessionManager that accumulates a VIRTUAL wall-clock (ms) instead of
    sleeping — so the benchmark is deterministic and instant. Cold start
    (get_or_create of a new key) adds _COLD_START_MS; a warm new_conversation()
    adds _WARM_RESET_MS."""

    def __init__(self) -> None:
        self.clock_ms = 0.0
        self.cold_starts = 0
        self.live: dict[str, "_ClockProvider"] = {}

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None, extra_env=None):
        if key in self.live:
            return self.live[key], False, False
        self.cold_starts += 1
        self.clock_ms += _COLD_START_MS
        prov = _ClockProvider(self, tag=key)
        self.live[key] = prov
        return prov, True, False

    def release(self, key, *, cleanup=False):
        self.live.pop(key, None)

    async def reset(self, key):
        self.live.pop(key, None)


class _ClockProvider:
    def __init__(self, sessions: _ClockSessions, tag: str) -> None:
        self._sessions = sessions
        self.tag = tag

    async def new_conversation(self) -> None:
        self._sessions.clock_ms += _WARM_RESET_MS

    def is_process_alive(self) -> bool:
        return True


@pytest.mark.asyncio
async def test_pooled_is_faster_than_cold_start_per_call():
    """Modeled wall-clock: N SEQUENTIAL ctx.agent() calls cost far less pooled
    (1 cold start + N-1 cheap resets) than un-pooled (N cold starts). This is the
    'make sure it is faster' assertion — deterministic, no real sleeps."""
    n = 12

    # ── Un-pooled baseline: SessionManager cold-starts a fresh key per call
    #    (emulates agent_exec.build_agent_fn: get_or_create(wf:{i}) + release). ──
    base = _ClockSessions()
    for i in range(n):
        await base.get_or_create(f"wf:run:{i}")  # fresh key → cold start each time
        base.release(f"wf:run:{i}", cleanup=True)
    baseline_ms = base.clock_ms

    # ── Pooled path: real build_pooled_agent_fn over the clock sessions. ──
    pooled = _ClockSessions()

    async def _stream(provider, prompt, **kwargs):
        return f"[{provider.tag}] {prompt}"

    import kiro_crew.workflows.agent_pool as ap

    _orig = ap.stream_and_collect
    ap.stream_and_collect = _stream  # type: ignore[assignment]
    try:
        agent_fn, pool = build_pooled_agent_fn(pooled, run_id="run", max_workers=4)
        for i in range(n):
            await agent_fn(f"task-{i}", {})  # sequential → reuse one warm worker
        await pool.shutdown()
    finally:
        ap.stream_and_collect = _orig  # type: ignore[assignment]
    pooled_ms = pooled.clock_ms

    # Baseline = N cold starts; pooled = 1 cold start + (N-1) cheap resets.
    assert base.cold_starts == n
    assert pooled.cold_starts == 1
    expected_pooled = _COLD_START_MS + (n - 1) * _WARM_RESET_MS
    assert pooled_ms == expected_pooled
    # THE SPEEDUP: pooled must be at least ~3x faster on this loading-bound workload.
    speedup = baseline_ms / pooled_ms
    assert (
        speedup >= 3.0
    ), f"pooled not faster enough: {speedup:.1f}x ({baseline_ms} vs {pooled_ms})"


# ── End-to-end through the REAL WorkflowService (exercises _runner → pool →
#    on_complete wiring, not agent_pool in isolation) ──

# A script that makes 6 SEQUENTIAL ctx.agent() calls — the shape the pool helps.
_SIX_AGENTS = (
    'META = {"name": "six", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    outs = []\n"
    "    for i in range(6):\n"
    "        r = await ctx.agent('step-' + str(i))\n"
    "        outs.append(r)\n"
    "    return {'n': len(outs)}\n"
)


async def _run_service(pool_agents: bool, sessions) -> None:
    """Drive one real WorkflowService run of _SIX_AGENTS to terminal state."""
    from kiro_crew.workflows.service import WorkflowService

    svc = WorkflowService(sessions=sessions, persist=False, pool_agents=pool_agents)
    out = await svc.start(_SIX_AGENTS, name="six")
    rid = out["run_id"]
    # Poll to terminal (mirrors test_workflows_service._wait_terminal).
    for _ in range(200):
        snap = svc.status(rid)
        if snap and snap["status"] != "running":
            assert snap["status"] == "finished", snap
            return
        await asyncio.sleep(0.02)
    raise AssertionError("run did not finish")


@pytest.mark.asyncio
async def test_end_to_end_service_pooled_cold_starts_fewer_sessions(monkeypatch):
    """Real WorkflowService: a 6-agent run cold-starts far fewer sessions with
    pool_agents=True than with pool_agents=False (which cold-starts one per call).
    Proves the shipped _runner→pool wiring — not just agent_pool in isolation."""

    async def _stream(provider, prompt, **kwargs):
        return f"[{provider.tag}] {prompt}"

    # Both the pooled path (agent_pool) and the un-pooled path (agent_exec, used
    # by service when pool_agents=False) call their module-local stream_and_collect.
    import kiro_crew.workflows.agent_exec as ae
    import kiro_crew.workflows.agent_pool as ap

    monkeypatch.setattr(ap, "stream_and_collect", _stream)
    monkeypatch.setattr(ae, "stream_and_collect", _stream)

    # Un-pooled: one cold start per ctx.agent() call (6 distinct wf:{run}:{i} keys).
    unpooled = _ClockSessions()
    await _run_service(pool_agents=False, sessions=unpooled)
    assert unpooled.cold_starts == 6, unpooled.cold_starts

    # Pooled: sequential calls reuse a warm worker → far fewer cold starts.
    pooled = _ClockSessions()
    await _run_service(pool_agents=True, sessions=pooled)
    assert pooled.cold_starts < unpooled.cold_starts
    # Sequential fan-out on a fresh pool → exactly 1 warm session serves all 6.
    assert pooled.cold_starts == 1, pooled.cold_starts
    # And the modeled wall-clock is lower (loading-bound).
    assert pooled.clock_ms < unpooled.clock_ms


# --------------------------------------------------------------------------- #
# Per-task timeout is honored (regression). WorkerPool.send threads its
# per-task bound into worker.send_message(prompt, timeout=…); the worker MUST
# enforce it so a wedged agent turn is terminated instead of holding a
# concurrency permit until the far-larger run-level ceiling fires.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_send_message_enforces_timeout(monkeypatch):
    """A stream that never completes must be cut off at the per-task timeout —
    the worker wraps stream_and_collect in asyncio.wait_for(timeout)."""

    async def _hang(provider, prompt, **kwargs):
        await asyncio.sleep(3600)  # never returns within the test's timeout
        return "unreachable"

    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _hang)

    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="to1", max_workers=2)
    # Drive the worker directly so we control the timeout value (the pool's
    # default is 1800s — too long to wait on in a unit test).
    from kiro_crew.workflows.agent_pool import _WorkflowSessionWorker

    worker = _WorkflowSessionWorker(sessions, key="wf-pool:to1:0", agent=None, model=None, cwd=None)
    await worker.start()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await worker.send_message("wedged", timeout=0.05)
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_pool_send_timeout_reaches_worker(monkeypatch):
    """End-to-end via WorkerPool.send: the pool's per-task timeout is enforced,
    proving the worker honors the protocol's timeout argument."""

    async def _hang(provider, prompt, **kwargs):
        await asyncio.sleep(3600)
        return "unreachable"

    monkeypatch.setattr("kiro_crew.workflows.agent_pool.stream_and_collect", _hang)

    sessions = _FakeSessions()
    from kiro_crew.acp.worker_pool import WorkerPool
    from kiro_crew.workflows.agent_pool import _WorkflowSessionWorker

    ids = iter(range(100))

    def _factory():
        return _WorkflowSessionWorker(
            sessions, key=f"wf-pool:to2:{next(ids)}", agent=None, model=None, cwd=None
        )

    wp = WorkerPool(_factory, max_workers=1, default_timeout=0.05, name="wf-pool:to2")
    try:
        with pytest.raises(asyncio.TimeoutError):
            await wp.send("wedged")
    finally:
        await wp.shutdown()


@pytest.mark.asyncio
async def test_extra_env_pin_reaches_all_three_pool_call_sites():
    """A run-level extra_env pin threads into every get_or_create the
    pooled adapter makes — the warm pooled worker, the named-session bypass, and
    the identity-cap unpooled overflow."""
    env = {"CORRELATION_ID": "xyz", "MC_ENDPOINT": "https://example.test"}
    sessions = _FakeSessions()
    # max_identities=1 so a second distinct identity overflows to the unpooled path.
    agent_fn, pool = build_pooled_agent_fn(
        sessions, run_id="renv", max_workers=1, max_identities=1, extra_env=env
    )

    await agent_fn("pooled default", {})  # warm pooled worker
    await agent_fn("named chain", {"session": "chain-A"})  # named-session bypass
    await agent_fn("overflow", {"model": "other-model"})  # unpooled overflow valve

    # All three cold starts carried the run-level env pin.
    assert sessions.created_extra_env, "no sessions were created"
    assert all(e == env for e in sessions.created_extra_env), sessions.created_extra_env
    await pool.shutdown()


@pytest.mark.asyncio
async def test_no_extra_env_pin_stays_none():
    """Default (no pin) must not inject env — no accidental leakage."""
    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="rnone", max_workers=1)
    await agent_fn("p", {})
    assert sessions.created_extra_env == [None]
    await pool.shutdown()


def test_max_turns_constant_is_shared_with_agent_exec():
    """agent_pool must reuse agent_exec._MAX_TURNS_PER_STEP (one source of truth),
    not hand-duplicate it — else the pooled and per-call ceilings can diverge."""
    from kiro_crew.workflows import agent_exec, agent_pool

    assert agent_pool._MAX_TURNS_PER_STEP is agent_exec._MAX_TURNS_PER_STEP


# ── taskq admission: the effective cap bounds the pool ────────────────────────


class _Peak:
    """Counts overlapping calls through a wrapped agent_fn."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def wrap(self, agent_fn):
        async def _fn(prompt, opts):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                return await agent_fn(prompt, opts)
            finally:
                self.active -= 1

        return _fn


def _admission(cap: int, *, mode: str = "aimd", store=None):
    from kiro_crew.taskq.adapters.runner import RunnerAdmission, RunnerLane

    return RunnerAdmission(store, lane=RunnerLane(cap, mode=mode))


@pytest.mark.asyncio
async def test_worker_pool_honours_the_effective_cap_beneath_max_workers():
    """The lane's effective cap, not the pool's max_workers, is the live bound."""
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="cap1", max_workers=4)
    adm = _admission(2)
    peak = _Peak()
    fn = admitted_agent_fn(peak.wrap(agent_fn), adm, run_id="cap1", session_key="chat:a")
    try:
        await asyncio.gather(*(fn(f"t{i}", {}) for i in range(6)))
    finally:
        await pool.shutdown()
    assert peak.peak == 2  # pool would allow 4; the effective cap says 2
    assert sessions.cold_starts <= 2
    assert adm.lane.running == 0 and adm.lane.stats()["granted"] == 6


@pytest.mark.asyncio
async def test_worker_pool_follows_a_cap_change_mid_run():
    """set_effective_cap (the adaptive controller's actuator) lowers the bound live."""
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="cap2", max_workers=4)
    adm = _admission(3)
    peak = _Peak()
    fn = admitted_agent_fn(peak.wrap(agent_fn), adm, run_id="cap2", session_key="chat:a")
    try:
        first = [asyncio.create_task(fn(f"a{i}", {})) for i in range(3)]
        await asyncio.sleep(0)
        assert adm.lane.running == 3
        assert adm.lane.set_effective_cap(1) == 1  # pressure: 3 -> 1
        await asyncio.gather(*first)
        peak.peak = 0
        await asyncio.gather(*(fn(f"b{i}", {}) for i in range(4)))
        assert peak.peak == 1  # the lowered cap holds for the rest of the run
        adm.lane.set_effective_cap(None)
        peak.peak = 0
        await asyncio.gather(*(fn(f"c{i}", {}) for i in range(4)))
        assert peak.peak == 3  # lifted: back to the ceiling
        assert adm.lane.set_effective_cap(0) == 0  # paused: nothing new is granted
        parked = asyncio.create_task(fn("d0", {}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert adm.lane.waiting == 1 and not parked.done()
        adm.lane.set_effective_cap(2)
        assert (await parked).endswith("d0")
    finally:
        await pool.shutdown()


@pytest.mark.asyncio
async def test_worker_pool_fixed_mode_ignores_the_actuator():
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    sessions = _FakeSessions()
    agent_fn, pool = build_pooled_agent_fn(sessions, run_id="fix", max_workers=4)
    adm = _admission(2, mode="fixed")
    adm.lane.set_effective_cap(1)  # pinned: the controller cannot move it
    peak = _Peak()
    fn = admitted_agent_fn(peak.wrap(agent_fn), adm, run_id="fix")
    try:
        await asyncio.gather(*(fn(f"t{i}", {}) for i in range(5)))
    finally:
        await pool.shutdown()
    assert peak.peak == 2


@pytest.mark.asyncio
async def test_workflow_agent_calls_are_rows_in_the_launching_sessions_lane(tmp_path):
    """Every ctx.agent() call is a workflow_agent row: written before it runs, settled after."""
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:
        sessions = _FakeSessions()
        agent_fn, pool = build_pooled_agent_fn(sessions, run_id="rows", max_workers=2)
        adm = _admission(2, store=store)
        fn = admitted_agent_fn(agent_fn, adm, run_id="rows", session_key="chat:bob")
        try:
            out = await asyncio.gather(fn("p1", {}), fn("p2", {"model": "m2"}))
        finally:
            await pool.shutdown()
        assert all(o.endswith(("p1", "p2")) for o in out)
        rows = sorted(store.list_rows(kind=m.KIND_WORKFLOW_AGENT), key=lambda x: x.id)
        assert [x.id for x in rows] == ["workflow:rows:agent1", "workflow:rows:agent2"]
        assert all(x.state == m.DONE and x.params["lane"] == "chat:bob" for x in rows)
        assert rows[1].provider == "m2"
        kinds = [e.kind for e in store.events("workflow:rows:agent1")]
        assert kinds[:2] == ["accepted", "claimed"] and kinds.count("claimed") == 1

        # A cron-launched run (no session) queues in the system lane.
        agent_fn2, pool2 = build_pooled_agent_fn(sessions, run_id="rows2", max_workers=1)
        try:
            await admitted_agent_fn(agent_fn2, adm, run_id="rows2", source="cron")("p", {})
        finally:
            await pool2.shutdown()
        assert store.get("workflow:rows2:agent1").params["lane"] == "system"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_workflow_agent_failure_settles_the_row_failed(tmp_path):
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:

        async def _boom(prompt, opts):
            raise RuntimeError("model exploded")

        adm = _admission(1, store=store)
        fn = admitted_agent_fn(_boom, adm, run_id="bad", session_key="chat:c")
        with pytest.raises(RuntimeError, match="model exploded"):
            await fn("p", {})
        row = store.get("workflow:bad:agent1")
        assert row.state == m.FAILED and adm.lane.running == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_workflow_agent_rate_limit_waits_then_retries(tmp_path):
    """A 429 parks the call in waiting_dependency (slot released) and re-runs it on wake."""
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.adapters.runner import RunnerAdmission, RunnerLane
    from kiro_crew.taskq.dependency import KIND_RATE_LIMITED, SIGNAL_ATTR, DependencySignal
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    now = {"t": 100.0}
    store = TaskStore(tmp_path / "tasks.db", network_fs=False, clock=lambda: now["t"]).open()
    try:
        adm = RunnerAdmission(store, lane=RunnerLane(1), clock=lambda: now["t"])
        calls = []
        raised = asyncio.Event()

        async def _flaky(prompt, opts):
            calls.append(prompt)
            if len(calls) == 1:
                exc = RuntimeError("HTTP 429")
                setattr(
                    exc,
                    SIGNAL_ATTR,
                    DependencySignal(
                        kind=KIND_RATE_LIMITED, dependency_scope="prov", source="t", retry_at=130.0
                    ),
                )
                raised.set()  # the 429 is leaving the call: the park follows it
                raise exc
            return "ok"

        fn = admitted_agent_fn(_flaky, adm, run_id="rl", session_key="chat:d")
        task = asyncio.create_task(fn("p", {}))
        await settle_dependency_park(adm, store, raised)
        assert store.get("workflow:rl:agent1").state == m.WAITING_DEPENDENCY
        assert adm.lane.running == 0
        now["t"] = 131.0
        assert adm.tick() == ["workflow:rl:agent1"]
        assert await task == "ok"
        assert len(calls) == 2 and store.get("workflow:rl:agent1").state == m.DONE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_cancelled_call_waiting_for_a_lane_slot_leaves_no_queued_row(tmp_path):
    """A run cancel (or the wall-clock ceiling) landing while a ``ctx.agent()``
    call waits for its lane slot: the row was already accepted, so it must be
    settled -- ``workflow_agent`` has no dispatcher and a workflow resumes only
    through its own registry, never by re-dispatching one call row."""
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:
        gate = asyncio.Event()

        async def _gated(prompt, opts):
            await gate.wait()
            return prompt

        adm = _admission(1, store=store)
        fn = admitted_agent_fn(_gated, adm, run_id="cx", session_key="chat:c")
        first = asyncio.create_task(fn("p1", {}))
        for _ in range(20):
            await settle_store_writes(store)
            if store.state_of("workflow:cx:agent1") == m.RUNNING:
                break
        second = asyncio.create_task(fn("p2", {}))
        for _ in range(20):
            await settle_store_writes(store)
            if adm.lane.waiting == 1:
                break
        assert store.state_of("workflow:cx:agent2") == m.QUEUED

        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second

        assert store.state_of("workflow:cx:agent2") == m.CANCELLED
        gate.set()
        assert await first == "p1"
        assert store.state_of("workflow:cx:agent1") == m.DONE
        # The two numbers a forever-queued row corrupts on /api/tasks/summary.
        assert store.oldest_wait_secs() == 0.0
        assert set(store.count_by_state()) == {m.DONE, m.CANCELLED}
        assert adm.lane.running == 0 and adm.lane.waiting == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_cancelled_call_parked_on_a_dependency_leaves_no_orphan_wait(tmp_path):
    """Same for a call parked in ``waiting_dependency``: the coroutine that was
    waiting for the scope's wake is the only thing that could have resumed it."""
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.adapters.runner import RunnerAdmission, RunnerLane
    from kiro_crew.taskq.dependency import KIND_RATE_LIMITED, SIGNAL_ATTR, DependencySignal
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    now = {"t": 100.0}
    store = TaskStore(tmp_path / "tasks.db", network_fs=False, clock=lambda: now["t"]).open()
    try:
        adm = RunnerAdmission(store, lane=RunnerLane(1), clock=lambda: now["t"])

        async def _throttled(prompt, opts):
            exc = RuntimeError("HTTP 429")
            setattr(
                exc,
                SIGNAL_ATTR,
                DependencySignal(
                    kind=KIND_RATE_LIMITED, dependency_scope="prov", source="t", retry_at=130.0
                ),
            )
            raise exc

        fn = admitted_agent_fn(_throttled, adm, run_id="dep", session_key="chat:d")
        call = asyncio.create_task(fn("p", {}))
        for _ in range(20):
            await settle_store_writes(store)
            if store.state_of("workflow:dep:agent1") == m.WAITING_DEPENDENCY:
                break
        assert store.state_of("workflow:dep:agent1") == m.WAITING_DEPENDENCY

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

        row = store.get("workflow:dep:agent1")
        assert row.state == m.CANCELLED and row.wait is None
        assert store.oldest_wait_secs() == 0.0
        assert adm.lane.running == 0 and adm.lane.waiting == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_service_attach_settles_a_call_row_that_was_never_claimed(tmp_path):
    """Crash after accept: the row is ``queued`` under a dead incarnation, which
    the boot reconciler never examines. The attach sweep gives it an explicit
    terminal state, and a repeat sweep settles nothing twice."""
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.reconcile import reconcile_on_boot
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.service import WorkflowService

    path = tmp_path / "tasks.db"
    store_a = TaskStore(path, network_fs=False).open()
    adm_a = _admission(1, store=store_a)
    accepted = adm_a.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:old:agent1")
    assert store_a.state_of(accepted.id) == m.QUEUED
    store_a.close()  # -- crash before the lane granted a slot

    store_b = TaskStore(path, network_fs=False).open()
    try:
        assert reconcile_on_boot(store_b).examined == 0  # a queued row is not ACTIVE
        svc = WorkflowService(sessions=_FakeSessions(), persist=False, pool_agents=True)
        svc.attach_task_admission(_admission(2, store=store_b))

        report = await svc.adopt_task_rows()

        assert report.cancelled == [accepted.id]
        assert store_b.state_of(accepted.id) == m.CANCELLED
        assert store_b.oldest_wait_secs() == 0.0
        again = await svc.adopt_task_rows()
        assert again.examined == 0 and again.cancelled == []
    finally:
        store_b.close()


@pytest.mark.asyncio
async def test_service_attach_settles_orphaned_workflow_agent_rows(tmp_path):
    """A workflow restarts through its registry: orphaned call rows never re-dispatch."""
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.service import WorkflowService

    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:
        for row_id, safe in (("workflow:old:agent1", True), ("workflow:old:agent2", False)):
            store.accept_one(
                m.TaskRecord(id=row_id, kind=m.KIND_WORKFLOW_AGENT, params={"safe_retry": safe})
            )
            store.claim(row_id, owner="dead")
            store.transition(row_id, m.STARTING)
            store.transition(row_id, m.RUNNING)
        svc = WorkflowService(sessions=_FakeSessions(), persist=False, pool_agents=True)
        adm = _admission(2, store=store)
        svc.attach_task_admission(adm)
        assert svc.task_admission is adm
        report = await svc.adopt_task_rows()
        assert report.failed == ["workflow:old:agent1"]
        assert report.unknown_side_effect == ["workflow:old:agent2"]
        assert store.get("workflow:old:agent1").state == m.FAILED
        assert store.get("workflow:old:agent2").state == m.UNKNOWN_SIDE_EFFECT
        # The runner the service builds meters its agent_fn through the lane.
        runner = svc._runner("new", session_key="chat:z")
        assert runner is not None
        svc.attach_task_admission(None)
        assert svc.task_admission is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_workflow_agent_call_writes_its_row_off_loop(tmp_path, monkeypatch):
    """``ctx.agent()``'s whole row lifecycle under the STRICT on-loop guard.

    Nothing is stubbed: the accept, the claim, the ``starting``/``running``
    marks and the terminal write all go through the store's writer thread, so a
    site that slips back onto the loop reds here rather than costing an operator
    a contended ``BEGIN IMMEDIATE`` on the gateway's only loop.
    """
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq import store as store_mod
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:
        adm = _admission(1, store=store)

        async def _agent_fn(prompt, opts):
            return f"out:{prompt}"

        fn = admitted_agent_fn(_agent_fn, adm, run_id="off", session_key="chat:zoe")
        before = store.loop_thread_calls
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        try:
            assert await fn("p1", {}) == "out:p1"
        finally:
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
        assert store.loop_thread_calls == before  # the reads below are the test's own
        row = store.get("workflow:off:agent1")
        assert row is not None and row.state == m.DONE
        kinds = [e.kind for e in store.events(row.id)]
        assert kinds[:2] == ["accepted", "claimed"]
        assert [e.data["to"] for e in store.events(row.id) if e.kind == "transition"] == [
            m.STARTING,
            m.RUNNING,
            m.DONE,
        ]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_workflow_call_whose_running_mark_is_refused_never_runs(tmp_path):
    """The row is the authority for "a runtime is executing under this call". A
    refused ``starting -> running`` leaves a row that reaches no WAITING state --
    so the dependency park above could not persist -- and may belong to a newer
    owner, so the call ends instead of running under it."""
    import sqlite3

    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.adapters.runner import RunnerAdmissionRefused
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    class _LockedOnRunning(TaskStore):
        """A REAL store whose FILE another connection locks for the ``running``
        write only, so that ONE transition gets ``database is locked``."""

        def transition(self, task_id, state, **kw):
            if state != m.RUNNING:
                return super().transition(task_id, state, **kw)
            blocker = sqlite3.connect(str(self.path), timeout=0, check_same_thread=False)
            blocker.execute("BEGIN EXCLUSIVE")
            try:
                return super().transition(task_id, state, **kw)
            finally:
                blocker.execute("ROLLBACK")
                blocker.close()

    store = _LockedOnRunning(tmp_path / "tasks.db", network_fs=False, busy_timeout_secs=0.05).open()
    try:
        calls = []

        async def _agent_fn(prompt, opts):
            calls.append(prompt)
            return f"out:{prompt}"

        adm = _admission(1, store=store)
        fn = admitted_agent_fn(_agent_fn, adm, run_id="mark", session_key="chat:e")
        with pytest.raises(RunnerAdmissionRefused, match="running mark"):
            await fn("p1", {})
        assert calls == [], "the call ran under a row the store left starting"
        assert store.get("workflow:mark:agent1").state == m.FAILED
        assert adm.lane.running == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_nested_agent_call_is_refused_not_parked_behind_its_ancestor(tmp_path):
    """ONE RunnerLane serves both runner consumers, and a ``ctx.agent()`` call
    holds its slot for the whole model turn. A call whose row descends from the
    row holding that slot therefore waits on a release only its own ancestor can
    make, and ``RunnerLane.acquire`` parks with no timeout -- so the fence
    refuses it instead, and the run reports the refusal.

    The wait is bounded HERE so a regression is this assertion, not a lost
    worker. ``accept`` hands back a row that already exists and is still
    claimable (the resume path), which is how the parentage is recorded.
    """
    from kiro_crew.taskq import model as m
    from kiro_crew.taskq.adapters.runner import RunnerAdmissionRefused
    from kiro_crew.taskq.store import TaskStore
    from kiro_crew.workflows.agent_pool import admitted_agent_fn

    store = TaskStore(tmp_path / "tasks.db", network_fs=False).open()
    try:
        calls: list[str] = []

        async def _agent_fn(prompt, opts):
            calls.append(prompt)
            return f"out:{prompt}"

        adm = _admission(1, store=store)
        # The ancestor: a TaskRunner step holding the lane's only slot.
        step = adm.accept(kind=m.KIND_TASKRUNNER_STEP, task_id="taskrunner:r:task1")
        holder = await adm.admit(step.id, lane="chat:n")
        # The nested call's row, recorded as the step's child before the call runs.
        adm.accept(kind=m.KIND_WORKFLOW_AGENT, task_id="workflow:nest:agent1", parent_id=step.id)
        fn = admitted_agent_fn(_agent_fn, adm, run_id="nest", session_key="chat:n")
        call = asyncio.ensure_future(fn("p1", {}))
        # Bounded here, with headroom for a loaded runner: the refusal resolves
        # on the loop's next tick, so no green run spends this.
        await asyncio.wait({call}, timeout=5.0)
        assert call.done(), "the nested call parked behind its own ancestor's slot"
        with pytest.raises(RunnerAdmissionRefused):
            call.result()
        from kiro_crew.taskq.adapters.runner import RunnerLaneSelfBlocked

        assert isinstance(call.exception(), RunnerLaneSelfBlocked)
        assert calls == [], "the nested call ran without a slot"
        await settle_store_writes(store)
        assert store.state_of("workflow:nest:agent1") == m.CANCELLED
        # The ancestor is untouched and the ceiling held throughout.
        assert holder.slot_held and adm.lane.running == 1 and adm.lane.waiting == 0
        holder.done()
        assert adm.lane.running == 0

        # And once the ancestor lets go, the SAME call runs: the fence declines a
        # wait nothing but the ancestor could end, never nesting as such.
        again = admitted_agent_fn(_agent_fn, adm, run_id="nest2", session_key="chat:n")
        assert (await again("p2", {})) == "out:p2"
        assert calls == ["p2"]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_cheap_reuse_path_still_ends_the_previous_conversations_children():
    """Replacing the conversation on a warm process still ends that conversation's children.

    ``reset`` has two paths and both must end the children. The cheap path calls
    ``provider.new_conversation()`` and keeps the process, so nothing about the process
    dying stands in for the teardown -- and a child of the step that just finished would
    otherwise inject into whatever this warm worker is handed next. The hard-reset fallback
    reaches the teardown through ``ends_conversation=True``; this pins the other path.
    """
    sessions = _FakeSessions()
    worker = _WorkflowSessionWorker(
        sessions,
        key="wf-pool:run-1:0",
        agent="kirocrew",
        model=None,
        cwd=None,
    )
    await worker.start()
    prov = sessions.live["wf-pool:run-1:0"]
    sessions.order = []
    prov.order_sink = sessions.order

    await worker.reset()

    assert prov.new_conversation_calls == 1, "the cheap path did not run"
    assert sessions.order == ["end_children", "new_conversation"], (
        "the suppression was armed after the conversation handle was swapped, so a child "
        f"finishing during the swap delivers into the NEXT task's conversation: {sessions.order}"
    )
    assert sessions.resets == 0, "the cheap path fell back to a hard reset"
    assert getattr(sessions, "children_ended", []) == ["wf-pool:run-1:0"], (
        "the reused worker kept the previous conversation's children, so one of them can "
        "inject into the next task's conversation"
    )
