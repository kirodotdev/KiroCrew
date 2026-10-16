"""An ended call refuses a late member, and a held result is pending work.

Two gaps in the inline-collection registry (``subagent_inline_collection``):

* A call's commit can reach the gateway BEFORE one of its own spawns reserves:
  the stdio loop ends (stdin EOF) while a submit step's ``/api/spawn`` is in
  flight, and the loop's ``finally`` tells the parked call it was not written.
  The member reserved afterwards would be held for a call that never commits
  again, up to ``max_wait`` + grace. The commit therefore tombstones its call,
  and ``reserve`` refuses a tombstoned call (``/api/spawn`` answers 409).
* A released held result waits for its parent's turn to end. The cron turn's
  ``finally`` asks ``has_pending_work_for`` whether to defer its reset, so the
  probe must count a result still owed to the parent (``InlineCollections.owes``),
  and a deferral is performed by whichever settle leaves the parent owed
  nothing, the returned settle included.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_mcp_shared import _LoopHarness, _tools_call

from kiro_crew import mcp_core
from kiro_crew.subagent_inline_collection import (
    COLLECTED_TTL_SECS,
    DELIVERING,
    MAX_ENDED_CALLS_PER_PARENT,
    InlineCollections,
)
from kiro_crew.testing.wait import async_wait_until, wait_until

CRON = "cron:job1:run1"
TAB = "dashboard:tab1"


class _RegistryGateway:
    """The tool's two gateway routes, backed by a REAL registry.

    ``/api/spawn`` answers as ``api_spawn`` does: a refused reservation for an
    ended call is the 409 ``inline_call_ended`` body.
    """

    def __init__(self, reg: InlineCollections, ids: list[str]) -> None:
        self.reg = reg
        self._ids = iter(ids)
        self.order: list[str] = []
        self.spawn_entered = threading.Event()
        self.spawn_go = threading.Event()
        self.block_first_spawn = True
        self.lock = threading.Lock()

    def post(self, path: str, body: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        if path == "/api/spawn":
            if self.block_first_spawn:
                self.block_first_spawn = False
                self.spawn_entered.set()
                assert self.spawn_go.wait(10.0), "the test never released the held spawn"
            aid = next(self._ids)
            parent, call = body["parent_session"], body["inline_call"]
            with self.lock:
                if not self.reg.reserve(parent, aid, body["max_wait"], call=call):
                    ended = self.reg.call_ended(parent, call)
                    self.order.append(f"refused:{aid}")
                    code = "inline_call_ended" if ended else "inline_collection_full"
                    return {"error": "refused", "code": code}
                self.order.append(f"reserve:{aid}")
            return {"id": aid}
        if path == "/api/spawn/mark-collected":
            with self.lock:
                self.reg.commit(
                    body["parent_session"],
                    body.get("ids") or [],
                    released=body.get("released") or [],
                    call=body.get("call"),
                )
                self.order.append("commit")
            return {"status": "ok"}
        return {}

    def get(self, path: str, **_kw: Any) -> dict[str, Any]:
        return {"done": False, "agent": path.rsplit("/", 1)[1]}


@pytest.mark.parametrize("parent", [TAB, CRON])
def test_loop_exit_commits_before_a_running_spawn_step_reserves(monkeypatch, parent):
    """stdin EOF while a submit step's ``/api/spawn`` is in flight: the exit path
    commits the call BEFORE the running step returns, and the member the
    gateway is about to reserve is refused, so nothing is held for that call."""
    reg = InlineCollections()
    gw = _RegistryGateway(reg, ["a1", "a2"])
    harness = _LoopHarness(monkeypatch, mcp_core._call_tool_deferrable)
    call = _tools_call(1, "spawn_sub_agents")
    call["params"]["arguments"] = {"agents": [{"prompt": "t0"}, {"prompt": "t1"}]}
    with (
        patch.object(mcp_core, "_post", side_effect=gw.post),
        patch.object(mcp_core, "_get", side_effect=gw.get),
        patch.object(mcp_core, "sel"),
        patch.dict("os.environ", {"KIROCREW_SESSION_KEY": parent}),
    ):
        harness.send(call)
        assert gw.spawn_entered.wait(10.0)
        closer = threading.Thread(target=harness.close, daemon=True)
        closer.start()  # EOF: the loop's finally runs
        wait_until(lambda: "commit" in gw.order, describe=lambda: gw.order)
        gw.spawn_go.set()
        closer.join(10.0)
        wait_until(lambda: len(gw.order) >= 2, describe=lambda: gw.order)
    assert gw.order[:2] == ["commit", "refused:a1"], gw.order
    assert "a1" not in reg._records.get(parent, {})
    # Its completion is an ordinary one: nothing holds it.
    assert reg.hold(parent, "a1") is False


def test_a_reserve_after_its_calls_commit_is_refused_for_the_tombstones_lifetime():
    """The registry half: a commit remembers its call for ``COLLECTED_TTL_SECS``."""
    now = [0.0]
    reg = InlineCollections(clock=lambda: now[0])
    reg.commit(CRON, [], call="c")
    assert reg.call_ended(CRON, "c")
    assert not reg.reserve(CRON, "late", 7200, call="c")
    assert CRON not in reg._records
    # Another call of the same parent is unaffected.
    assert reg.reserve(CRON, "other", 60, call="d")
    now[0] = COLLECTED_TTL_SECS + 1.0
    reg._expire(CRON)
    assert not reg.call_ended(CRON, "c")
    assert reg.reserve(CRON, "late", 60, call="c")


def test_the_ended_calls_a_parent_keeps_are_bounded():
    """Past ``MAX_ENDED_CALLS_PER_PARENT`` the oldest tombstone goes first."""
    reg = InlineCollections(clock=lambda: 0.0)
    for i in range(MAX_ENDED_CALLS_PER_PARENT + 5):
        reg.commit(CRON, [], call=f"c{i}")
    assert len(reg._ended[CRON]) == MAX_ENDED_CALLS_PER_PARENT
    assert not reg.call_ended(CRON, "c0")
    assert reg.call_ended(CRON, f"c{MAX_ENDED_CALLS_PER_PARENT + 4}")


def test_an_expired_tombstone_leaves_nothing_for_its_parent():
    now = [0.0]
    reg = InlineCollections(clock=lambda: now[0])
    reg.commit(CRON, [], call="c")
    now[0] = COLLECTED_TTL_SECS + 1.0
    reg._expire(CRON)
    assert CRON not in reg._ended and CRON not in reg._records


def test_a_commit_naming_no_call_tombstones_nothing():
    """Main's phase-less mark sends no ``call``: it ends no call."""
    reg = InlineCollections()
    reg.commit(TAB, ["x1"], call=None)
    reg.commit(TAB, ["x1"], call="")
    assert reg._ended == {}


def test_a_reservation_with_no_call_is_refused():
    """Every member belongs to the call that collects it; nothing else ends it."""
    reg = InlineCollections()
    assert not reg.reserve(CRON, "a1", 60, call="")
    assert reg._records == {}


def _manager() -> Any:
    from overload_fakes import mock_ctx, mock_sessions

    from kiro_crew.subagent import SubagentManager

    sessions = mock_sessions()
    busy = [True]
    sessions.is_busy = lambda _k: busy[0]
    mgr = SubagentManager(sessions=sessions, ctx_builder=mock_ctx())
    mgr._on_done = AsyncMock()
    return mgr, busy


class _HeldRun:
    id = "a1"
    parent_session_key = CRON
    outcome = "completed"
    elapsed = 1.0
    credits = 0.0
    _delivery_queued = True
    _report_undelivered = False
    _report_owed = False
    done = True


@pytest.mark.asyncio
async def test_a_released_held_result_is_pending_work_until_it_is_delivered():
    """The cron turn's ``finally`` defers its reset while the probe is True.
    A member released by a cut, torn or cancelled call waits for that turn to
    end, so the probe must say so, or the reset lands first and the delivery
    cold-starts a context-free session."""
    from kiro_crew.slack.gateway import _subagent_work_pending

    mgr, busy = _manager()
    reg = mgr.inline_collections
    mgr._agents["a1"] = _HeldRun()
    assert reg.reserve(CRON, "a1", 600, call="c1")
    assert reg.hold(CRON, "a1")
    assert mgr.has_pending_work_for(CRON) is True  # held
    with patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0.01):
        reg.commit(CRON, [], released=["a1"], call="c1")  # e.g. cut at MAX_RESPONSE_LEN
        assert reg._records[CRON]["a1"].state == DELIVERING
        assert mgr.has_pending_work_for(CRON) is True
        assert await mgr.has_pending_work_for_async(CRON) is True
        assert await _subagent_work_pending(mgr, CRON) is True  # the turn defers
        busy[0] = False  # the turn has ended
        await async_wait_until(lambda: mgr._on_done.await_count == 1)
        await async_wait_until(lambda: CRON not in reg._records)
    mgr._on_done.assert_awaited_once()
    assert mgr.has_pending_work_for(CRON) is False
    assert await mgr.has_pending_work_for_async(CRON) is False


@pytest.mark.asyncio
async def test_a_deferred_cron_reset_is_performed_by_the_delivery():
    """Through the gateway route: the turn's ``finally`` defers (the probe sees
    the owed result), and the delivery's own route resets the idle cron
    parent afterwards, so the session is reset exactly once and only after the
    result reached it."""
    from test_spawn_sub_agents_inline_collection import _drain, _gateway, _info, _stream

    from kiro_crew.slack.gateway import _subagent_work_pending

    registry = InlineCollections()
    orch, on_done, mgr = _gateway(registry)
    mgr.has_pending_work_for_async = AsyncMock(side_effect=registry.owes)
    orch.sessions.is_busy = MagicMock(return_value=True)
    orch.sessions.reset = AsyncMock(return_value=True)
    assert registry.reserve(CRON, "a1", 600, call="c1")
    with (
        _stream() as stream,
        patch("kiro_crew.subagent_inline_collection._ORPHAN_IDLE_POLL_SECS", 0.01),
    ):
        await on_done(_info("a1", CRON))  # held: the parent's turn is blocked in the call
        orch.sessions.reset.reset_mock()
        registry.commit(CRON, [], released=["a1"], call="c1")  # the reply was torn
        assert await _subagent_work_pending(mgr, CRON) is True  # the finally defers
        orch.sessions.reset.assert_not_awaited()
        orch.sessions.is_busy.return_value = False  # the turn has ended
        await _drain(orch)
        stream.assert_awaited_once()
    orch.sessions.reset.assert_awaited_once_with(CRON)
    assert await _subagent_work_pending(mgr, CRON) is False


async def _hold_under_a_live_cron_turn(registry: InlineCollections, aids: list[str]) -> Any:
    """Gateway whose cron turn is live while *aids* are held, one call each."""
    from test_spawn_sub_agents_inline_collection import _gateway, _info

    orch, on_done, mgr = _gateway(registry)
    mgr.has_pending_work_for_async = AsyncMock(side_effect=registry.owes)
    orch.sessions.is_busy = MagicMock(return_value=True)
    # The hold-time reset runs under the live turn, so ``skip_if_busy`` refuses it.
    orch.sessions.reset = AsyncMock(return_value=False)
    for n, aid in enumerate(aids, 1):
        assert registry.reserve(CRON, aid, 600, call=f"c{n}")
        await on_done(_info(aid, CRON))
    orch.sessions.reset.reset_mock()
    orch.sessions.reset.return_value = True
    return orch


@pytest.mark.asyncio
async def test_a_deferred_cron_reset_is_performed_when_the_commit_returns_the_member():
    """The commit NAMES the held member: the reply carried it, so it is settled
    with no completion route. The reset the turn's ``finally`` deferred under
    the hold is then performed by that settle, once, and never under a live
    turn (``skip_if_busy``)."""
    from test_spawn_sub_agents_inline_collection import _drain, _stream

    from kiro_crew.slack.gateway import _subagent_work_pending

    registry = InlineCollections()
    with _stream() as stream:
        orch = await _hold_under_a_live_cron_turn(registry, ["a1"])
        mgr = orch.subagent_mgr
        assert await _subagent_work_pending(mgr, CRON) is True  # the finally defers
        orch.sessions.is_busy.return_value = False  # the turn has ended
        for task in registry.commit(CRON, ["a1"], released=["a1"], call="c1"):
            await task
        await _drain(orch)
        stream.assert_not_awaited()  # returned inline, never injected
    mgr.settle_queued_delivery.assert_awaited_once()
    orch.sessions.reset.assert_awaited_once_with(CRON, skip_if_busy=True)
    assert await _subagent_work_pending(mgr, CRON) is False


def test_the_gateway_installs_the_returned_hook_exactly_once():
    """``_init_subagents`` builds the manager that owns the registry, and it
    installs the returned-settle hook on that registry once, with a callable:
    a second install would replace the first, and none would leave the
    deferred cron reset unperformed."""
    from test_spawn_sub_agents_inline_collection import _gateway

    registry = InlineCollections()
    installs: list[Any] = []
    original = registry.set_returned_hook

    def _record(hook: Any) -> None:
        installs.append(hook)
        original(hook)

    registry.set_returned_hook = _record  # type: ignore[method-assign]
    _gateway(registry)
    assert len(installs) == 1
    assert callable(installs[0])
    assert registry._returned_hook is installs[0]


@pytest.mark.asyncio
async def test_a_returned_member_leaves_the_reset_to_a_sibling_still_held():
    """Another member is still held for a later call: the first settle leaves
    the reset alone, and the sibling's own settle performs it."""
    from test_spawn_sub_agents_inline_collection import _drain, _stream

    registry = InlineCollections()
    with _stream():
        orch = await _hold_under_a_live_cron_turn(registry, ["a1", "a2"])
        orch.sessions.is_busy.return_value = False
        for task in registry.commit(CRON, ["a1"], released=["a1"], call="c1"):
            await task
        await _drain(orch)
        orch.sessions.reset.assert_not_awaited()  # a2 is still owed
        for task in registry.commit(CRON, ["a2"], released=["a2"], call="c2"):
            await task
        await _drain(orch)
    orch.sessions.reset.assert_awaited_once_with(CRON, skip_if_busy=True)


def test_retention_keeps_a_held_run_past_many_newer_completions():
    """Through the manager's own eviction (the run path's terminal step): a held
    run survives more than ``MAX_RETAINED_AGENTS`` newer completions."""
    from kiro_crew.context_management import MAX_RETAINED_AGENTS
    from kiro_crew.subagent import SubagentInfo

    mgr, _busy = _manager()
    reg = mgr.inline_collections
    held = SubagentInfo(id="held", task="t", parent_session_key=CRON)
    held.done = True
    held.started = 0.0
    mgr._agents["held"] = held
    assert reg.reserve(CRON, "held", 600, call="c1")
    assert reg.hold(CRON, "held")
    for n in range(MAX_RETAINED_AGENTS + 10):
        newer = SubagentInfo(id=f"n{n}", task="t", parent_session_key=CRON)
        newer.done = True
        newer.started = float(n + 1)
        mgr._agents[newer.id] = newer
    assert mgr._run_events._evict_completed_runs() == 10
    assert "held" in mgr._agents
    reg.commit(CRON, [], released=["held"], call="c1")  # no loop: kept held, still pinned
    assert "held" in mgr._agents
