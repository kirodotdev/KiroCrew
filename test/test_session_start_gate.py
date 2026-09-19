"""SessionStartGate + StartCollector (RFC §4.4, wave 2 area D).

Every test drives the REAL ``AcpRuntime.create_session`` against a fake
subprocess (a StreamReader we feed frames into); nothing spawns kiro-cli and no
socket is opened. Budgets are shrunk through the same seams production reads
them from, never by sleeping.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.acp.runtime as runtime_mod
from kiro_crew.acp.runtime import (
    START_OUTCOME_ABANDONED,
    START_OUTCOME_ADOPTED,
    START_OUTCOME_TORN_DOWN,
    AcpRuntime,
    AcpSessionStartTimeout,
    SessionStartGate,
)
from kiro_crew.acp.types import METHOD_MCP_SERVER_INITIALIZED, METHOD_SESSION_NEW

# The run.py tests below drive ``SubagentManager.spawn``, which refuses while
# the host looks short of memory -- the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# ── harness ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fast_paths(monkeypatch):
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05, raising=False)
    # Fresh gate per test, sized by the test (never by the host's config file).
    runtime_mod._session_start_gates.clear()
    monkeypatch.setattr(runtime_mod, "_resolve_session_start_concurrency", lambda: 2)
    yield
    runtime_mod._session_start_gates.clear()


def _make_runtime(
    *, backend: str | None = None
) -> tuple[AcpRuntime, asyncio.StreamReader, MagicMock]:
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    rt._expect_mcp_reports = False
    # Sub-second budgets through the cached seams production reads.
    rt._session_start_timeout = 0.05
    rt._start_collect_timeout = 0.4
    if backend is not None:
        rt._acp_backend = backend
    return rt, reader, proc


def _feed(reader: asyncio.StreamReader, obj: dict) -> None:
    reader.feed_data((json.dumps(obj) + "\n").encode())


def _roster(*names: str) -> list[dict]:
    """An ``mcpServers`` array shaped the way session_servers builds it."""
    return [{"name": n, "command": "/bin/true", "args": [], "env": []} for n in names]


def _mcp_frame(session_id: str, server: str) -> dict:
    """One MCP registration notification, as kiro-cli emits it during a start."""
    return {
        "jsonrpc": "2.0",
        "method": METHOD_MCP_SERVER_INITIALIZED,
        "params": {"sessionId": session_id, "serverName": server},
    }


async def _await_staged(rt: AcpRuntime, count: int, *, timeout: float = 2.0) -> None:
    """Wait until the reader has staged *count* frames in the in-flight scope."""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(rt._pending_init_notifications) < count:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("frame never reached the init staging buffer")
        await asyncio.sleep(0)


async def _await_collected(collector, count: int, *, timeout: float = 2.0) -> None:
    """Wait until the reader has staged *count* frames on *collector*."""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(collector._staged_init) < count:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("frame never reached the collector")
        await asyncio.sleep(0)


async def _start_reader(rt: AcpRuntime) -> asyncio.Task:
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    return task


async def _stop_reader(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


async def _await_pending(rt: AcpRuntime, *, timeout: float = 2.0) -> int:
    deadline = asyncio.get_event_loop().time() + timeout
    while not rt._pending_requests:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("no pending request appeared")
        await asyncio.sleep(0)
    return max(rt._pending_requests)


async def _gate() -> SessionStartGate:
    return await runtime_mod.session_start_gate()


class _SteppedClock:
    """``time`` stand-in whose ``monotonic`` only moves when the test says so.

    Every other attribute is the real module's. asyncio keeps its own import
    of ``time``, so the loop's timers are untouched.
    """

    def __init__(self, real) -> None:
        self._real = real
        self._now = 1000.0

    def monotonic(self) -> float:
        return self._now

    def advance(self, secs: float) -> None:
        self._now += secs

    def __getattr__(self, name):
        return getattr(self._real, name)


# ── the gate ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_is_acquired_before_session_new_and_bounds_concurrency(monkeypatch):
    """With limit 2, three concurrent starts put at most two session/new on the
    wire; the third goes out only after one answers. The gate is held exactly
    across the request and released once per start."""
    rt, _, _ = _make_runtime()
    gate = await _gate()
    entered: list[int] = []
    answered: list[int] = []
    release = asyncio.Event()

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            entered.append(gate.active)
            await release.wait()
            answered.append(len(answered) + 1)
            return {"sessionId": f"sid-{answered[-1]}"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    tasks = [asyncio.create_task(rt.create_session(cwd="/w", mcp_servers=[])) for _ in range(3)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(entered) == 2, "third session/new must wait behind the gate"
    assert gate.queued == 1
    assert max(entered) <= 2
    release.set()
    handles = await asyncio.gather(*tasks)
    assert len(entered) == 3
    assert all(a <= 2 for a in entered)
    assert {h.session_id for h in handles} == {"sid-1", "sid-2", "sid-3"}
    assert gate.releases == 3
    assert gate.active == 0 and gate.queued == 0


@pytest.mark.asyncio
async def test_gate_exit_callback_reports_queue_wait_not_start_time(monkeypatch):
    """``on_gate_acquired`` fires at gate EXIT with the queue wait: a start that
    queued behind a held gate reports a positive wait, a free gate reports ~0.
    The caller sets its start clock there, so queue time never counts against
    the 90s start budget or the startup watchdog."""
    rt, _, _ = _make_runtime()
    release = asyncio.Event()
    waits: list[float] = []

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            await release.wait()
            return {"sessionId": "sid"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    # The gate reads ``time.monotonic`` through the runtime module; hand it a
    # clock the test advances, so the wait it reports is the hold the test
    # chose, not what a runner's sleep happened to deliver.
    monkeypatch.setattr(runtime_mod, "time", _SteppedClock(runtime_mod.time))
    gate = await _gate()
    # Fill the gate so the observed start has to queue.
    permits = [await gate.acquire() for _ in range(gate.limit)]
    observed = asyncio.create_task(
        rt.create_session(cwd="/w", mcp_servers=[], on_gate_acquired=waits.append)
    )
    for _ in range(10):
        await asyncio.sleep(0)
    assert waits == [], "callback must not fire while queued"
    runtime_mod.time.advance(0.02)  # the start sits queued for 20ms
    for p in permits:
        p.release()
    release.set()
    await observed
    assert len(waits) == 1, waits
    assert waits[0] == pytest.approx(20.0), waits  # ms: exactly the hold


# ── the collector ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [runtime_mod.ACP_BACKEND_KIRO, runtime_mod.ACP_BACKEND_KAS])
async def test_timeout_then_late_response_is_adopted(backend):
    """session/new times out -> AcpSessionStartTimeout carries a collector that
    still owns the request id; the late answer resolves it and the registered
    adopter receives a fully built handle. Same behaviour on both harnesses:
    the gate wraps create_session, which every backend's start runs through."""
    from kiro_crew.acp.harness import SessionExtras

    rt, reader, _ = _make_runtime(backend=backend)
    # This collector test models an MCP-free agent on either harness.
    if backend == runtime_mod.ACP_BACKEND_KAS:
        rt._kas_custom_agents = AsyncMock(
            return_value=SessionExtras(custom_agents=[{"id": rt._agent, "tools": []}])
        )
    reader_task = await _start_reader(rt)
    adopted: list = []

    async def _adopter(handle) -> bool:
        adopted.append(handle)
        return True

    gate = await _gate()
    try:
        with pytest.raises(AcpSessionStartTimeout) as ei:
            await rt.create_session(cwd="/w", mcp_servers=[], late_adopter=_adopter)
        collector = ei.value.collector
        assert collector is not None
        req_id = collector.req_id
        # Ownership transferred, not dropped: the id is still registered.
        assert req_id in rt._pending_requests
        assert req_id in rt._pending_requests.adopted
        assert not collector.gate_released(), "the collector now holds the permit"
        assert gate.active == 1
        with patch.object(rt, "terminate_session", AsyncMock()) as term:
            _feed(reader, {"id": req_id, "result": {"sessionId": "late-sid"}})
            # KAS activates the injected agent with set_mode after session/new;
            # answer whatever control-plane request the tail issues.
            deadline = asyncio.get_event_loop().time() + 2.0
            answered: set[int] = set()
            while not collector.settled.is_set():
                if asyncio.get_event_loop().time() > deadline:
                    raise AssertionError("collector never settled")
                for rid in list(rt._pending_requests):
                    if rid != req_id and rid not in answered:
                        answered.add(rid)
                        _feed(reader, {"id": rid, "result": {}})
                await asyncio.sleep(0)
            term.assert_not_awaited()
        assert collector.outcome == START_OUTCOME_ADOPTED
        assert [h.session_id for h in adopted] == ["late-sid"]
        assert "late-sid" in rt._session_queues
        assert req_id not in rt._pending_requests
        assert req_id not in rt._start_collectors
        assert gate.releases == 1 and gate.active == 0
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_a_late_adopted_session_receives_its_staged_init_frames():
    """An adopted session is handed the MCP-init frames staged under its own id:
    the ones from before the caller's budget expired AND the ones that arrive
    while the collector owns the start. Those frames are what arms drain_init's
    idle shortcut, and none of them is counted as a dropped frame."""
    rt, reader, _ = _make_runtime()
    # Room to observe the pre-timeout frame; the wait below is on the reader
    # having demuxed it, never on the clock.
    rt._session_start_timeout = 0.3
    reader_task = await _start_reader(rt)
    adopted: list = []

    async def _adopter(handle) -> bool:
        adopted.append(handle)
        return True

    try:
        start = asyncio.create_task(
            rt.create_session(cwd="/w", mcp_servers=_roster("alpha", "beta"), late_adopter=_adopter)
        )
        await _await_pending(rt)
        _feed(reader, _mcp_frame("late-sid", "alpha"))
        await _await_staged(rt, 1)
        with pytest.raises(AcpSessionStartTimeout) as ei:
            await start
        collector = ei.value.collector
        assert collector is not None
        # The in-flight scope closed with the caller and cleared; the collector
        # is the holder that survives it.
        assert rt._session_inits_in_flight == 0
        assert not rt._pending_init_notifications
        _feed(reader, _mcp_frame("late-sid", "beta"))
        await _await_collected(collector, 2)
        _feed(reader, {"id": collector.req_id, "result": {"sessionId": "late-sid"}})
        await asyncio.wait_for(collector.settled.wait(), timeout=5.0)
        assert collector.outcome == START_OUTCOME_ADOPTED
        assert [h.session_id for h in adopted] == ["late-sid"]
        assert adopted[0].mcp_session_report().payload()["ready"] == ["alpha", "beta"]
        assert rt._dropped_frames == {}
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("answer_late", [True, False])
async def test_a_settled_collector_leaks_no_frames_into_the_next_start(answer_late):
    """A collector nobody adopts -- torn down on a late answer, or abandoned at
    its deadline -- takes its staged frames with it. The next start's timeout
    reads the servers that never reported for IT: the earlier attempt's
    registration must not be folded into that diagnostic, which matches by
    server name and cannot tell two attempts apart."""
    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    try:
        with pytest.raises(AcpSessionStartTimeout) as first:
            await rt.create_session(cwd="/w", mcp_servers=_roster("alpha"))
        collector = first.value.collector
        _feed(reader, _mcp_frame("late-sid", "alpha"))
        await _await_collected(collector, 1)
        # The collector holds it; the diagnostic's own buffer never sees it.
        assert not rt._pending_init_notifications
        with patch.object(rt, "terminate_session", AsyncMock()):
            if answer_late:
                _feed(reader, {"id": collector.req_id, "result": {"sessionId": "late-sid"}})
            await asyncio.wait_for(collector.settled.wait(), timeout=5.0)
        assert collector.outcome == (
            START_OUTCOME_TORN_DOWN if answer_late else START_OUTCOME_ABANDONED
        )
        assert rt._start_collectors == {}
        assert not collector._staged_init, "a settled collector keeps no unclaimable frames"
        assert not rt._pending_init_notifications

        with pytest.raises(AcpSessionStartTimeout) as second:
            await rt.create_session(cwd="/w", mcp_servers=_roster("alpha"))
        assert "0/1 MCP server(s) reported" in str(second.value)
        assert "no report from alpha" in str(second.value)
        assert rt._session_inits_in_flight == 0
        # Settle the second collector too, so no task outlives the test.
        await asyncio.wait_for(second.value.collector.settled.wait(), timeout=5.0)
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_two_live_collectors_do_not_claim_each_others_frames():
    """Neither late session's id is known while both collectors wait, so both
    hold both frames -- and each adoption takes only the frames naming its own
    session."""
    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    adopted: dict = {}

    def _adopter(tag: str):
        async def _adopt(handle) -> bool:
            adopted[tag] = handle
            return True

        return _adopt

    try:
        with pytest.raises(AcpSessionStartTimeout) as first:
            await rt.create_session(
                cwd="/w", mcp_servers=_roster("alpha"), late_adopter=_adopter("a")
            )
        with pytest.raises(AcpSessionStartTimeout) as second:
            await rt.create_session(
                cwd="/w", mcp_servers=_roster("beta"), late_adopter=_adopter("b")
            )
        col_a, col_b = first.value.collector, second.value.collector
        _feed(reader, _mcp_frame("late-a", "alpha"))
        _feed(reader, _mcp_frame("late-b", "beta"))
        await _await_collected(col_a, 2)
        await _await_collected(col_b, 2)
        _feed(reader, {"id": col_a.req_id, "result": {"sessionId": "late-a"}})
        _feed(reader, {"id": col_b.req_id, "result": {"sessionId": "late-b"}})
        await asyncio.wait_for(col_a.settled.wait(), timeout=5.0)
        await asyncio.wait_for(col_b.settled.wait(), timeout=5.0)
        assert (col_a.outcome, col_b.outcome) == (START_OUTCOME_ADOPTED, START_OUTCOME_ADOPTED)
        assert adopted["a"].mcp_session_report().payload()["ready"] == ["alpha"]
        assert adopted["b"].mcp_session_report().payload()["ready"] == ["beta"]
        assert rt._dropped_frames == {}
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito", "temporary"])
async def test_timeout_then_late_response_without_adopter_is_torn_down(memory_mode):
    """No adopter registered: the late session is closed through the runtime's
    per-session teardown -- never by killing the shared runtime -- and the gate
    is released exactly once."""
    rt, reader, proc = _make_runtime()
    reader_task = await _start_reader(rt)
    gate = await _gate()
    try:
        with pytest.raises(AcpSessionStartTimeout) as ei:
            await rt.create_session(cwd="/w", mcp_servers=[], memory_mode=memory_mode)
        collector = ei.value.collector
        with (
            patch.object(rt, "terminate_session", AsyncMock()) as term,
            patch.object(rt, "kill", AsyncMock()) as kill,
            patch.object(runtime_mod.AcpSessionHandle, "cleanup_transcript_files") as cleanup,
        ):
            _feed(reader, {"id": collector.req_id, "result": {"sessionId": "late-sid"}})
            await asyncio.wait_for(collector.settled.wait(), timeout=2.0)
            term.assert_awaited_once_with("late-sid")
            kill.assert_not_awaited()
            if memory_mode == "persistent":
                cleanup.assert_not_called()
            else:
                cleanup.assert_called_once_with("late-sid")
        assert collector.outcome == START_OUTCOME_TORN_DOWN
        assert not rt._dead
        assert gate.releases == 1 and gate.active == 0
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_adopter_declining_tears_the_late_session_down():
    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    gate = await _gate()

    async def _decline(handle) -> bool:
        return False

    try:
        with pytest.raises(AcpSessionStartTimeout) as ei:
            await rt.create_session(cwd="/w", mcp_servers=[], late_adopter=_decline)
        collector = ei.value.collector
        with patch.object(rt, "terminate_session", AsyncMock()) as term:
            _feed(reader, {"id": collector.req_id, "result": {"sessionId": "late-sid"}})
            await asyncio.wait_for(collector.settled.wait(), timeout=2.0)
            term.assert_awaited_once_with("late-sid")
        assert collector.outcome == START_OUTCOME_TORN_DOWN
        assert gate.releases == 1
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_collector_deadline_abandons_the_attempt_and_releases_gate_once():
    """No answer within start_collect_timeout_secs: the request is dropped,
    the attempt is ``abandoned``, and the permit is released exactly once."""
    rt, reader, _ = _make_runtime()
    rt._start_collect_timeout = 0.1
    reader_task = await _start_reader(rt)
    gate = await _gate()
    try:
        with pytest.raises(AcpSessionStartTimeout) as ei:
            await rt.create_session(cwd="/w", mcp_servers=[])
        collector = ei.value.collector
        await asyncio.wait_for(collector.settled.wait(), timeout=2.0)
        assert collector.outcome == START_OUTCOME_ABANDONED
        assert collector.req_id not in rt._pending_requests
        assert rt._start_collectors == {}
        assert gate.releases == 1 and gate.active == 0
        # A second release is a no-op: exactly once.
        assert collector._permit.release() is False
        assert gate.releases == 1
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_runtime_death_settles_collector_without_teardown():
    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    gate = await _gate()
    try:
        with pytest.raises(AcpSessionStartTimeout) as ei:
            await rt.create_session(cwd="/w", mcp_servers=[])
        collector = ei.value.collector
        _feed(reader, _mcp_frame("late-sid", "alpha"))
        await _await_collected(collector, 1)
        with patch.object(rt, "terminate_session", AsyncMock()) as term:
            rt._mark_dead("test: process exited")
            await asyncio.wait_for(collector.settled.wait(), timeout=2.0)
            term.assert_not_awaited()
        assert collector.outcome == runtime_mod.START_OUTCOME_RUNTIME_DEAD
        assert gate.releases == 1
        # Death settles the staging through the same finally as every other
        # outcome: no session can arrive to claim these now.
        assert not collector._staged_init
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_a_death_the_collector_timeout_beat_is_still_runtime_dead():
    """The SAME death, with the two clocks in the other order.

    ``test_runtime_death_settles_collector_without_teardown`` above has
    ``_mark_dead`` resolve the future BEFORE the collector's own timeout expires,
    so `_run` learns of the death through ``AcpRuntimeDead``. On a slow host the
    timeout wins instead -- measured on the Windows shard, where that test read
    ``abandoned`` -- and the classification must not change with the order: the
    process is gone either way, and `abandoned` is the one outcome an operator
    cannot act on. So the death is marked FIRST and the future left unresolved,
    which is the losing order stated as a fact rather than raced for.
    """
    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    try:
        with pytest.raises(AcpSessionStartTimeout) as ei:
            await rt.create_session(cwd="/w", mcp_servers=[])
        collector = ei.value.collector
        # The runtime is dead and its future is NOT resolved with AcpRuntimeDead,
        # so the only way to the right outcome is the flag.
        rt._dead = True
        collector.timeout = 0.01
        with patch.object(rt, "terminate_session", AsyncMock()) as term:
            await asyncio.wait_for(collector.settled.wait(), timeout=2.0)
            term.assert_not_awaited()
        assert collector.outcome == runtime_mod.START_OUTCOME_RUNTIME_DEAD
    finally:
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_non_timeout_failure_releases_gate_once(monkeypatch):
    rt, _, _ = _make_runtime()
    gate = await _gate()

    async def _boom(method, params, timeout=None):
        raise runtime_mod.AcpRuntimeError("refused")

    monkeypatch.setattr(rt, "_send_and_await", _boom)
    with pytest.raises(runtime_mod.AcpRuntimeError):
        await rt.create_session(cwd="/w", mcp_servers=[])
    assert gate.releases == 1 and gate.active == 0


@pytest.mark.asyncio
async def test_patched_send_timeout_without_wire_request_has_no_collector(monkeypatch):
    """A timeout raised before anything went on the wire owns nothing: no
    collector, gate released by create_session itself."""
    rt, _, _ = _make_runtime()
    gate = await _gate()

    async def _timeout(method, params, timeout=None):
        raise runtime_mod.AcpRequestTimeout("Request session/new timed out after 0.05s")

    monkeypatch.setattr(rt, "_send_and_await", _timeout)
    with pytest.raises(AcpSessionStartTimeout) as ei:
        await rt.create_session(cwd="/w", mcp_servers=[])
    assert ei.value.collector is None
    assert gate.releases == 1 and gate.active == 0


def test_pending_requests_adopt_keeps_entry_until_response():
    loop = asyncio.new_event_loop()
    try:
        pending = runtime_mod._PendingRequests()
        fut = loop.create_future()
        pending[7] = fut
        assert pending.adopt(7) is fut
        assert 7 in pending.adopted
        assert pending.adopt(99) is None
        assert pending.pop(7) is fut
        assert 7 not in pending.adopted
    finally:
        loop.close()


# ── run.py: congestion never falls back to a dedicated process ────────────────


class _FakeCollector:
    def __init__(self, outcome: str, *, session_id: str = "") -> None:
        self.req_id = 1
        self.timeout = 0.2
        self.outcome = outcome
        self.session_id = session_id
        self.settled = asyncio.Event()
        self.settled.set()


@pytest.mark.asyncio
async def test_run_start_timeout_does_not_fall_back_to_dedicated_process():
    """The subagent start path: a session/new timeout on the shared runtime ends
    the attempt through the collector's verdict; ``get_or_create`` (a dedicated
    kiro-cli process) is never called for congestion."""
    from test_session_sharing import (
        _cfg_patch,
        _mock_ctx_builder_auto,
        _mock_sessions,
        _wait_until_done,
    )

    from kiro_crew.subagent import SubagentManager

    sessions = _mock_sessions(sharing_eligible=True)
    runtime = sessions.get_subagent_runtime.return_value
    runtime.create_session = AsyncMock(
        side_effect=AcpSessionStartTimeout(
            "Request session/new timed out after 90s",
            collector=_FakeCollector(START_OUTCOME_TORN_DOWN),
        )
    )
    manager = SubagentManager(
        sessions=sessions, ctx_builder=_mock_ctx_builder_auto(), is_yolo=lambda: True
    )
    with (
        _cfg_patch(session_sharing=True),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        info = manager.spawn("test task", parent_session_key="dashboard:slot1")
        await _wait_until_done(info)
    sessions.get_or_create.assert_not_awaited()
    assert "start_abandoned:" in info.error, info.error
    assert manager._running_count == 0


@pytest.mark.asyncio
async def test_run_start_timeout_continues_on_adopted_late_session():
    """When the collector adopts the late session into the run, the run
    continues on it: no dedicated process, and the result is the shared
    session's."""
    from test_session_sharing import (
        _cfg_patch,
        _mock_ctx_builder_auto,
        _mock_sessions,
        _wait_until_done,
    )

    from kiro_crew.subagent import SubagentManager

    sessions = _mock_sessions(sharing_eligible=True)
    runtime = sessions.get_subagent_runtime.return_value
    late_handle = runtime.create_session.return_value

    async def _timeout_then_adopt(*, late_adopter=None, on_gate_acquired=None, **kw):
        if on_gate_acquired is not None:
            on_gate_acquired(123.0)
        assert late_adopter is not None
        assert await late_adopter(late_handle) is True
        raise AcpSessionStartTimeout(
            "Request session/new timed out after 90s",
            collector=_FakeCollector(START_OUTCOME_ADOPTED, session_id="shared-session-abc"),
        )

    runtime.create_session = AsyncMock(side_effect=_timeout_then_adopt)
    manager = SubagentManager(
        sessions=sessions, ctx_builder=_mock_ctx_builder_auto(), is_yolo=lambda: True
    )
    with (
        _cfg_patch(session_sharing=True),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        info = manager.spawn("test task", parent_session_key="dashboard:slot1")
        await _wait_until_done(info)
    sessions.get_or_create.assert_not_awaited()
    assert info.error == "", info.error
    assert info.result == "shared response"
    assert info._session_sharing is True
    assert info._start_queue_wait_ms == 123.0
