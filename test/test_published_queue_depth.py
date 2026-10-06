"""The slot list carries the queued depth the manager last published.

``subagent_queued`` frames are the dashboard's fast signal and nothing re-sends
one a client missed. ``subagents_queued`` on each slot row is the same value,
read back from the manager without a store read, so a slots push reconciles a
client that holds a stale count. These tests pin the table that holds it, the
publish path that writes it, the lifecycle edges that re-derive it, the parent
end that forgets it, and the routing of each entry onto the tab its frames reach.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from overload_fakes import mock_ctx, mock_sessions, wait_taskq_open

from kiro_crew.session_surface import set_dashboard_surfaced
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager import published_depth
from kiro_crew.subagent_manager.published_depth import PublishedQueueDepths

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

PARENT = "dashboard:s1"


def _queue_info(parent: str = PARENT) -> SubagentInfo:
    return SubagentInfo(id="_queue", task="", parent_session_key=parent)


def _published(mgr: SubagentManager, parent: str = PARENT) -> int:
    return mgr.published_queued_depths().get(parent, 0)


@pytest.fixture(autouse=True)
def _reset_surface_registry():
    """The routing tests publish to the process-global surface registry."""
    set_dashboard_surfaced(())
    yield
    set_dashboard_surfaced(())


class TestPublishedQueueDepths:
    def test_records_reads_and_clears_at_zero(self) -> None:
        depths = PublishedQueueDepths()
        depths.record(PARENT, 2)
        assert depths.get(PARENT) == 2
        depths.record(PARENT, 0)
        assert depths.get(PARENT) == 0
        # Zero is the absent state, not a stored row: the table holds only
        # parents that still have something waiting.
        assert len(depths.snapshot()) == 0

    def test_a_malformed_depth_keeps_the_last_value(self) -> None:
        depths = PublishedQueueDepths()
        depths.record(PARENT, 3)
        for bad in (None, "2", 1.5, True):
            depths.record(PARENT, bad)
        assert depths.get(PARENT) == 3

    def test_forget_drops_one_parent(self) -> None:
        depths = PublishedQueueDepths()
        depths.record(PARENT, 1)
        depths.record("dashboard:s2", 4)
        depths.forget(PARENT)
        assert depths.get(PARENT) == 0
        assert depths.get("dashboard:s2") == 4

    def test_is_bounded_and_drops_the_oldest_publisher(self, monkeypatch) -> None:
        monkeypatch.setattr(published_depth, "MAX_PUBLISHED_PARENTS", 2)
        depths = PublishedQueueDepths()
        depths.record("a", 1)
        depths.record("b", 1)
        # A re-publish makes "a" the newest, so "b" is the one that goes.
        depths.record("a", 5)
        depths.record("c", 1)
        assert len(depths.snapshot()) == 2
        assert (depths.get("a"), depths.get("b"), depths.get("c")) == (5, 0, 1)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_published_frame_is_readable_before_its_consumer_sees_it() -> None:
    """The slot list and the frame stream never disagree about order: the depth
    is recorded before ``on_event`` runs, so a slots push the consumer triggers
    from inside the frame already reads that frame's value."""
    seen: list[tuple[int, int]] = []
    mgr: SubagentManager

    async def on_event(etype: str, info: SubagentInfo, extra: dict) -> None:
        if etype == "subagent_queued":
            seen.append((extra["queued"], _published(mgr, info.parent_session_key)))

    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), on_event=on_event)
    try:
        await mgr._fire_event("subagent_queued", _queue_info(), {"queued": 2})
        await mgr._fire_event("subagent_queued", _queue_info(), {"queued": 0})
        assert seen == [(2, 2), (0, 0)]
        # Other lifecycle events never record their own payload into the table.
        await mgr._fire_event("subagent_spawn", _queue_info(), {"queued": 7})
        assert _published(mgr) == 0
    finally:
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_real_emit_path_records_what_it_publishes() -> None:
    """``_emit_queue_depth`` is the only producer of the frame; whatever it
    publishes is what the slot row reports, down to the 0 that clears it."""
    frames: list[int] = []

    async def on_event(etype: str, info: SubagentInfo, extra: dict) -> None:
        if etype == "subagent_queued" and info.parent_session_key == PARENT:
            frames.append(extra["queued"])

    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), on_event=on_event)
    try:
        await wait_taskq_open(mgr)
        mgr._queue.append({"parent_session_key": PARENT, "_preassigned_id": "q1"})
        mgr._emit_queue_depth(PARENT)
        await asyncio.wait_for(_until(lambda: frames[-1:] == [1]), 5)
        assert _published(mgr) == 1

        mgr._queue.clear()
        mgr._emit_queue_depth(PARENT)
        await asyncio.wait_for(_until(lambda: frames[-1:] == [0]), 5)
        assert _published(mgr) == 0
    finally:
        mgr._queue.clear()
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_parent_end_forgets_its_published_depth() -> None:
    """A parent that ended advertises nothing waiting on its slot: its entry
    goes at the teardown, even when no 0 frame was ever published."""
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx())
    try:
        await mgr._fire_event("subagent_queued", _queue_info(), {"queued": 1})
        await mgr._fire_event("subagent_queued", _queue_info("dashboard:s2"), {"queued": 3})
        await mgr.cancel_for_teardown([], parent_session_key=PARENT, verb="reset")
        assert _published(mgr) == 0
        assert _published(mgr, "dashboard:s2") == 3
    finally:
        await mgr.cancel_all()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_child_start_re_derives_a_depth_no_path_cleared() -> None:
    """A row popped with no emit of its own (the insider.1 ghost) must not stay
    in the table: the child's start re-counts from state and publishes the 0, so
    neither the frame stream nor any later slots push keeps the stale 1."""
    frames: list[int] = []

    async def on_event(etype: str, info: SubagentInfo, extra: dict) -> None:
        if etype == "subagent_queued" and info.parent_session_key == PARENT:
            frames.append(extra["queued"])

    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), on_event=on_event)
    try:
        await wait_taskq_open(mgr)
        # Published 1, then the row left the window with no emit.
        await mgr._fire_event("subagent_queued", _queue_info(), {"queued": 1})
        assert _published(mgr) == 1
        child = SubagentInfo(id="c1", task="t", parent_session_key=PARENT)
        await mgr._fire_event("subagent_spawn", child, {})
        await asyncio.wait_for(_until(lambda: frames[-1:] == [0]), 5)
        assert _published(mgr) == 0

        # A parent with nothing held is not re-counted on every lifecycle edge.
        frames.clear()
        await mgr._fire_event("subagent_done", child, {})
        await asyncio.sleep(0.05)
        assert frames == []
    finally:
        await mgr.cancel_all()


async def _until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
class TestSerializeSlotsSubagentsQueued:
    """Each serialized slot row carries ``subagents_queued`` for its session."""

    async def test_the_row_carries_the_published_depth(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        subs = MagicMock()
        subs.running_agents_for = MagicMock(return_value=[])
        subs.published_queued_depths = MagicMock(return_value={PARENT: 2, "dashboard:s9": 4})
        state = _make_state(tmp_path, subagents=subs)
        state.get_or_create_slot("s1")

        slots = state.serialize_slots()

        assert [d["subagents_queued"] for d in slots] == [2]

    async def test_a_cron_tab_sums_the_runs_that_route_to_it(self, tmp_path, monkeypatch) -> None:
        """A stateless run (``cron:<job>:<run>``) and an agent sequence
        (``cron:<job>:<agent>``) publish under their own keys, and their frames
        reach the job's ``cron-<job>`` tab. The row must report what those frames
        told it, not the 0 its linked ``cron:<job>`` key holds."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat_utils import subagent_event_slot

        mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx())
        try:
            await mgr._fire_event("subagent_queued", _queue_info("cron:job7:run1"), {"queued": 2})
            await mgr._fire_event("subagent_queued", _queue_info("cron:job7:writer"), {"queued": 1})
            state = _make_state(tmp_path, subagents=mgr)
            slot = state.get_or_create_slot("cron-job7")
            slot.linked_session_key = "cron:job7"
            set_dashboard_surfaced(["cron:job7"])
            assert subagent_event_slot("cron:job7:run1") == slot.key

            rows = {d["key"]: d["subagents_queued"] for d in state.serialize_slots()}

            assert rows[slot.key] == 3
        finally:
            await mgr.cancel_all()

    async def test_no_manager_reads_as_zero(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path, subagents=None)
        state.get_or_create_slot("s1")

        assert [d["subagents_queued"] for d in state.serialize_slots()] == [0]
