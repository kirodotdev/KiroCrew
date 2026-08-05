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

from kiro_crew.workflows.agent_pool import build_pooled_agent_fn


class _FakeProvider:
    """Stands in for an ACP provider: counts cold starts vs cheap resets."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.new_conversation_calls = 0
        self.alive = True

    async def new_conversation(self) -> None:
        # Cheap warm reset — the win. No cold start here.
        self.new_conversation_calls += 1

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

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None):
        # A live key returns instantly (SessionManager's warm per-key fast path).
        if key in self.live:
            return (self.live[key],)
        self.cold_starts += 1
        self.created_identities.append((agent, model, cwd))
        prov = _FakeProvider(tag=key)
        self.live[key] = prov
        self.keys_seen.add(key)
        return (prov,)

    def release(self, key, *, cleanup=False):
        self.releases += 1
        self.live.pop(key, None)

    async def reset(self, key):
        self.resets += 1
        self.live.pop(key, None)

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
    monkeypatch.setattr("kiro_crew.workflows.agent_pool.redact_credentials", lambda t: (t, []))
    monkeypatch.setattr(
        "kiro_crew.workflows.agent_pool.redact_exfiltration_urls", lambda t: (t, [])
    )


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
    finally:
        await pool.shutdown()


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
# Regression for the blocking review finding on the upstream pool review.
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

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None):
        if key in self.live:
            return (self.live[key],)
        self.cold_starts += 1
        self.clock_ms += _COLD_START_MS
        prov = _ClockProvider(self, tag=key)
        self.live[key] = prov
        return (prov,)

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
    proving the worker no longer ignores the protocol's timeout argument."""

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


def test_max_turns_constant_is_shared_with_agent_exec():
    """agent_pool must reuse agent_exec._MAX_TURNS_PER_STEP (one source of truth),
    not hand-duplicate it — else the pooled and per-call ceilings can diverge."""
    from kiro_crew.workflows import agent_exec, agent_pool

    assert agent_pool._MAX_TURNS_PER_STEP is agent_exec._MAX_TURNS_PER_STEP
