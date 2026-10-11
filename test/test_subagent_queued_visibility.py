"""A spawn the gate deferred is visible by id and by parent until it starts.

The deferred row lives only in the task store: no ``SubagentInfo``, no run
folder. ``queued_run_async`` / ``queued_runs_async`` are what the spawn status
and list routes read for it, so a caller told "queued" is not then told the run
does not exist.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import mock_ctx, mock_sessions, wait_taskq_open

import kiro_crew.subagent as subagent_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.subagent import QUEUED_REASON_LOW_MEMORY, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_deferred_spawn_is_readable_until_it_starts(monkeypatch) -> None:
    cfg = KiroCrewConfig()
    cfg.agent.subagent_cost_gb = 0.5
    cfg.agent.spawn_min_memory_gb = 4.0
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    free = {"gb": 3.0}

    def memory_check(*, min_gb, **_kw):
        return free["gb"] >= min_gb, free["gb"]

    monkeypatch.setattr(subagent_mod, "check_memory_available", memory_check)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = 0.0
    # Long enough that the row is still parked when it is read below.
    mgr._taskq_admit_wait_secs = 30.0
    started = asyncio.Event()

    async def worker(info) -> None:
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "_run", AsyncMock(side_effect=worker))
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key="dash:vis")
        assert info is not None and info.queued is True
        assert mgr.get(info.id) is None, "fixture: a deferred row has no registered run"

        queued = await mgr.queued_run_async(info.id)
        assert queued is not None
        assert queued.id == info.id
        assert queued.parent_session_key == "dash:vis"
        assert queued.task == "summarize the log"
        assert queued.accepted_at > 0
        assert queued.reason_detail == info.queued_reason_detail
        assert queued.reason_detail, "the gate's own sentence rides on the deferred event"
        # The kind is the parent's wait label, the one ``subagent_queued``
        # carries; it is read at call time, so a relabel is seen on the next read.
        mgr._queue_wait["dash:vis"] = {"reason": QUEUED_REASON_LOW_MEMORY}
        relabelled = await mgr.queued_run_async(info.id)
        assert relabelled is not None and relabelled.reason == QUEUED_REASON_LOW_MEMORY

        assert [q.id for q in (await mgr.queued_runs_async("dash:vis")).runs] == [info.id]
        assert [q.id for q in (await mgr.queued_runs_async(None)).runs] == [info.id]
        assert (await mgr.queued_runs_async("dash:vis")).partial is False
        assert (await mgr.queued_runs_async("dash:other")).runs == ()
        assert await mgr.queued_run_async("0123456789abcdef") is None

        # Memory recovers; the row is claimed, registered and started. From then
        # on the registry answers for it, so the queued read must not.
        free["gb"] = 32.0
        mgr._taskq_admit_wait_secs = 0.05
        await mgr._taskq.run(mgr._taskq.defer, info.id, wait=-1.0, reason="test: eligible now")
        for _ in range(250):
            if started.is_set():
                break
            mgr._drain_queue()
            await asyncio.sleep(0.02)
        assert started.is_set(), "the deferred row never started"
        assert mgr.get(info.id) is not None
        assert await mgr.queued_run_async(info.id) is None
        assert (await mgr.queued_runs_async("dash:vis")).runs == ()
    finally:
        mgr._shutting_down = True
        tasks = [task for task in mgr._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
        mgr._taskq.close()


def test_a_listing_past_the_cap_says_it_is_partial(tmp_path) -> None:
    """One row past the cap is read so a full page and a cut-off one differ: a
    truncated tail read as complete says accepted spawns were never accepted."""
    from kiro_crew import taskq
    from kiro_crew.subagent_manager.admission.taskq_bridge import (
        QUEUED_LISTING_CAP,
        _read_queued_rows,
    )

    store = taskq.TaskStore(tmp_path / "t.db").open()
    try:
        store.accept(
            taskq.TaskRecord(
                id=f"row{i:04d}",
                kind=taskq.KIND_SUBAGENT,
                session_key="dash:many",
                params={"task": f"t{i}"},
            )
            for i in range(QUEUED_LISTING_CAP)
        )
        rows, truncated = _read_queued_rows(
            store, session_key="dash:many", app=None, exclude_ids=[]
        )
        assert len(rows) == QUEUED_LISTING_CAP and truncated is False, "exactly full is complete"

        store.accept_one(
            taskq.TaskRecord(
                id="row-last", kind=taskq.KIND_SUBAGENT, session_key="dash:many", params={}
            )
        )
        rows, truncated = _read_queued_rows(
            store, session_key="dash:many", app=None, exclude_ids=[]
        )
        assert len(rows) == QUEUED_LISTING_CAP and truncated is True
        assert "row-last" not in {rec.id for rec, _ in rows}, "the oldest are the page"
    finally:
        store.close()


# ── one definition of "accepted, no run yet" ─────────────────────────────────


def _store(tmp_path):
    from kiro_crew import taskq

    return taskq.TaskStore(tmp_path / "t.db").open()


def _row(row_id: str, session: str = "dash:vis", **params):
    from kiro_crew import taskq

    return taskq.TaskRecord(
        id=row_id,
        kind=taskq.KIND_SUBAGENT,
        session_key=session,
        params={"task": f"task {row_id}", **params},
    )


def _force(store, row_id: str, state: str, *, attempts: int = 0, next_run_at=None) -> None:
    """Put a row in *state* directly: these tests are about the READERS."""
    with store._lock:
        store._c().execute(
            "UPDATE tasks SET state=?, attempts=?, next_run_at=? WHERE id=?",
            (state, attempts, next_run_at, row_id),
        )


def _bridge_view(queue_wait=None):
    """The bridge methods that shape a row, on a stand-in for the coordinator."""
    import types

    from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin

    fake = types.SimpleNamespace(
        _manager=types.SimpleNamespace(
            _queue_wait=queue_wait or {}, _max_concurrent=3, _user_max_concurrent=3
        )
    )
    fake._queued_run = types.MethodType(_TaskqBridgeMixin._queued_run, fake)
    return lambda rec, detail="": _TaskqBridgeMixin._queued_run_from_row(fake, rec, detail)


def test_a_claimed_unregistered_row_is_counted_and_listed(tmp_path) -> None:
    """A claim the pump awaits, or one retained across a store outage, is still
    ``admitted``: accepted work no run exists for. The count, the listing and
    the by-id read all include it, so the synthesis gates, spawn_list and
    spawn_status agree."""
    from kiro_crew import taskq
    from kiro_crew.subagent_manager.admission.taskq_bridge import (
        _read_queued_row,
        _read_queued_rows,
    )

    store = _store(tmp_path)
    try:
        store.accept([_row("adm1"), _row("q1")])
        assert store.claim("adm1") is not None
        assert store.state_of("adm1") == taskq.ADMITTED
        assert store.count_pending(taskq.KIND_SUBAGENT, session_key="dash:vis") == 1
        assert (
            store.count_pending(taskq.KIND_SUBAGENT, session_key="dash:vis", include_admitted=True)
            == 2
        )
        rows, partial = _read_queued_rows(store, session_key="dash:vis", app=None, exclude_ids=[])
        assert [rec.id for rec, _ in rows] == ["adm1", "q1"] and partial is False
        found = _read_queued_row(store, "adm1")
        assert found is not None and found[0].id == "adm1"
    finally:
        store.close()


def test_the_queued_count_includes_a_retained_claim(tmp_path) -> None:
    """``taskq_overflow`` is the count every pending-work guard reads: a
    retained ``admitted`` row no run is registered for counts."""
    import types

    from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin

    store = _store(tmp_path)
    try:
        store.accept([_row("adm1")])
        store.claim("adm1")
        fake = types.SimpleNamespace(
            _manager=types.SimpleNamespace(_agents={}),
            taskq_store=lambda: store,
            taskq_excluded_ids=lambda: [],
        )
        fake._overflow_query = types.MethodType(_TaskqBridgeMixin._overflow_query, fake)
        assert _TaskqBridgeMixin.taskq_overflow(fake, "dash:vis") == 1
        # Its run registered (live, or since finished): the registry answers
        # for it, so the count and the listing both leave it out.
        fake._manager._agents = {"adm1": object()}
        assert _TaskqBridgeMixin.taskq_overflow(fake, "dash:vis") == 0
    finally:
        store.close()


@pytest.mark.asyncio
async def test_the_chip_count_includes_a_retained_claim(tmp_path) -> None:
    """The chip's own reader (``taskq_chip_overflow_async``) uses the same
    "accepted, no run yet" definition as ``taskq_overflow``: a retained
    ``admitted`` row counts until a run registers for it."""
    import types

    from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin

    store = _store(tmp_path)
    try:
        store.accept([_row("adm1")])
        store.claim("adm1")

        class _Bridge:
            pump_off_loop = False
            _manager = types.SimpleNamespace(_agents={})

            def taskq_store(self):
                return store

            _overflow_query = _TaskqBridgeMixin._overflow_query

            def taskq_chip_excluded_ids(self) -> list[str]:
                return []

        bridge = _Bridge()
        assert await _TaskqBridgeMixin.taskq_chip_overflow_async(bridge, "dash:vis") == 1
        bridge._manager._agents = {"adm1": object()}
        assert await _TaskqBridgeMixin.taskq_chip_overflow_async(bridge, "dash:vis") == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    "state, attempts, resuming",
    [
        ("recovering", 1, "gateway_restart"),
        ("retry_wait", 2, "retry"),
        ("queued", 0, ""),
    ],
    ids=["recovering-after-restart", "dependency-parked-retry", "queued-control"],
)
def test_a_started_run_waiting_to_go_on_is_resuming_not_unstarted(
    tmp_path, state, attempts, resuming
) -> None:
    from kiro_crew.subagent_manager.admission.taskq_bridge import _read_queued_rows

    store = _store(tmp_path)
    try:
        store.accept([_row("r1")])
        _force(store, "r1", state, attempts=attempts)
        rows, _ = _read_queued_rows(store, session_key="dash:vis", app=None, exclude_ids=[])
        ((rec, detail),) = rows
        queued = _bridge_view({"dash:vis": {"reason": "low_memory"}})(rec, detail)
        assert queued.resuming == resuming
        # A run that already started carries no "why it has not started" kind.
        assert queued.reason == ("" if resuming else "low_memory")
    finally:
        store.close()


def test_an_old_deferral_sentence_does_not_describe_a_later_wait(tmp_path) -> None:
    """Deferred for memory, then started, then parked in a RECOVERING backoff
    (``next_run_at`` in the future again): the memory sentence predates the
    claim, so it is not reported. The control keeps it."""
    import time

    from kiro_crew.subagent_manager.admission.taskq_bridge import _read_queued_rows

    store = _store(tmp_path)
    sentence = "low memory: 1.1 GB available, need 4 GB"
    try:
        store.accept([_row("d1"), _row("d2")])
        later = time.time() + 600
        for rid in ("d1", "d2"):
            assert store.defer(rid, wait=600, reason=sentence)
        store.append_event("d1", "claimed", {})
        _force(store, "d1", "recovering", attempts=1, next_run_at=later)
        rows, _ = _read_queued_rows(store, session_key="dash:vis", app=None, exclude_ids=[])
        details = {rec.id: detail for rec, detail in rows}
        assert details == {"d1": "", "d2": sentence}
    finally:
        store.close()


def test_an_apps_listing_is_its_own_rows_before_the_cap(tmp_path) -> None:
    """The app filter runs in the store read, before the cap: 105 other-app rows
    neither hide the caller's own row nor mark its page partial."""
    from kiro_crew.subagent_manager.admission.taskq_bridge import _read_queued_rows

    store = _store(tmp_path)
    try:
        store.accept(_row(f"f{i:03d}", app="other") for i in range(105))
        store.accept_one(_row("mine1", app="mine"))
        rows, partial = _read_queued_rows(store, session_key=None, app="mine", exclude_ids=[])
        assert [rec.id for rec, _ in rows] == ["mine1"] and partial is False
        _rows, other_partial = _read_queued_rows(
            store, session_key=None, app="other", exclude_ids=[]
        )
        assert other_partial is True
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_unreadable_store_makes_the_listing_partial() -> None:
    import types

    from kiro_crew import taskq
    from kiro_crew.subagent_manager.admission.taskq_bridge import _TaskqBridgeMixin

    async def _down(*_a, **_kw):
        raise taskq.TaskStoreUnavailable("locked")

    store = types.SimpleNamespace(run=_down)
    fake = types.SimpleNamespace(
        _manager=types.SimpleNamespace(_agents={}, _queue=[], _queue_wait={}),
        taskq_store=lambda: store,
    )
    fake._manager._queued_listing_partial = False
    fake._live_run_ids = lambda: []
    listing = await _TaskqBridgeMixin.taskq_queued_runs_async(fake, "dash:vis")
    assert listing.partial is True and listing.runs == ()


@pytest.mark.usefixtures("close_subagent_managers")
def test_in_memory_pending_work_is_the_arms_whole_view() -> None:
    """The arm's in-memory terms, on the real manager: a window entry, another
    child whose task is still live (its report waits on teardown), a live
    follow-up watcher -- and never the asking child's own task."""
    from kiro_crew.subagent import SubagentInfo

    loop = asyncio.new_event_loop()
    try:
        mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx())
        parent = "dash:arm"
        assert mgr.has_in_memory_pending_work_for(parent) is False
        mgr._queue.append({"parent_session_key": parent, "_preassigned_id": "w1"})
        assert mgr.has_in_memory_pending_work_for(parent) is True
        mgr._queue.clear()

        me = SubagentInfo(id="me", task="t", parent_session_key=parent)
        me.done = True
        mgr._agents[me.id] = me
        mgr._tasks[me.id] = loop.create_future()
        assert mgr.has_in_memory_pending_work_for(parent, exclude_id="me") is False
        assert mgr.has_in_memory_pending_work_for(parent) is True
    finally:
        loop.close()


# ── the status and list routes ───────────────────────────────────────────────


class _Req(dict):
    """The parts of an aiohttp request the spawn handlers read."""

    def __init__(self, state, agent_id: str = "", *, body=None, query=None) -> None:
        super().__init__()
        self.app = {"state": state}
        self.match_info = {"agent_id": agent_id}
        self.query = query or {}
        self.headers: dict[str, str] = {}
        self._body = body

    async def json(self):
        return self._body


@pytest.mark.asyncio
async def test_a_run_registered_during_the_queued_lookup_answers_from_the_registry(
    tmp_path, monkeypatch
) -> None:
    """The pump registers the run while the status route awaits the queued
    lookup: the registry answers, not the half-written folder (which would say
    ``done`` with no result, and get the member collected before it ran)."""
    import json
    import types

    import kiro_crew.subagent_persistence as sp
    from kiro_crew.dashboard.handlers.messaging import api_spawn_status

    monkeypatch.setattr(sp, "_SUBAGENTS_DIR", tmp_path / "subagents")
    (tmp_path / "subagents").mkdir()
    registered: dict[str, object] = {}

    async def queued_run_async(run_id: str):
        await asyncio.sleep(0)
        sp.create_agent_folder(run_id, task="summarize the log", parent_session="dash:v8")
        registered[run_id] = types.SimpleNamespace(
            id=run_id,
            task="summarize the log",
            done=False,
            started=1.0,
            turns=0,
            last_tool="",
            streaming_text="",
            _awaiting_approval=False,
        )
        return None

    subagents = MagicMock()
    subagents.get = MagicMock(side_effect=lambda aid: registered.get(aid))
    subagents.queued_run_async = queued_run_async
    resp = await api_spawn_status(_Req(MagicMock(subagents=subagents), "raceid1"))
    body = json.loads(resp.body)
    assert resp.status == 200 and body["done"] is False
    assert "_No result._" not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_folder_with_no_recorded_ending_is_not_done(tmp_path, monkeypatch) -> None:
    import json

    import kiro_crew.subagent_persistence as sp
    from kiro_crew.dashboard.handlers.messaging import api_spawn_status

    monkeypatch.setattr(sp, "_SUBAGENTS_DIR", tmp_path / "subagents")
    (tmp_path / "subagents").mkdir()
    sp.create_agent_folder("run1", task="t", parent_session="dash:x")
    subagents = MagicMock(get=MagicMock(return_value=None), queued_run_async=AsyncMock())
    subagents.queued_run_async.return_value = None
    body = json.loads((await api_spawn_status(_Req(MagicMock(subagents=subagents), "run1"))).body)
    assert body["done"] is False and "outcome" not in body

    sp.write_tombstone("run1", cause="error", recovery_action="notification_pending")
    body = json.loads((await api_spawn_status(_Req(MagicMock(subagents=subagents), "run1"))).body)
    assert body["done"] is True and body["outcome"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["steer", "retry"])
async def test_a_control_on_a_queued_id_says_queued_not_not_found(route) -> None:
    import json

    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.subagent_manager.admission.types import QueuedRun

    subagents = MagicMock()
    subagents.get = MagicMock(return_value=None)
    subagents.steer_run = AsyncMock(return_value=(False, "not_found"))
    subagents.queued_run_async = AsyncMock(
        return_value=QueuedRun(id="q1", task="t", parent_session_key="dash:x")
    )
    state = MagicMock(subagents=subagents, _native_cards={})
    handler = messaging.api_spawn_steer if route == "steer" else messaging.api_spawn_retry
    with patch.object(messaging, "_native_child_refusal", return_value=None):
        resp = await handler(_Req(state, "q1", body={"message": "go"}))
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "queued_not_started"


@pytest.mark.asyncio
async def test_the_list_route_reads_no_store_unless_asked() -> None:
    """The dashboard pollers call GET /api/spawn every few seconds and never read
    ``queued``: without ``?queued=1`` the route does not touch the task store."""
    import json

    from kiro_crew.dashboard.handlers.messaging import api_spawn_list

    subagents = MagicMock(all_agents=[])
    subagents.queued_runs_async = AsyncMock(side_effect=AssertionError("store read"))
    state = MagicMock(subagents=subagents)
    with patch(
        "kiro_crew.dashboard.handlers.messaging.read_panel_records",
        return_value=__import__(
            "kiro_crew.subagent_persistence", fromlist=["PanelRecords"]
        ).PanelRecords([], 0, False),
    ):
        body = json.loads((await api_spawn_list(_Req(state))).body)
    assert "queued" not in body
    subagents.queued_runs_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_spawn_is_listed_once_live_then_queued_then_persisted() -> None:
    """Read queued first and live second: a run that registers between the two
    is live (never missing from both), and a row a restart left claimable beside
    its orphan tombstone is queued (it will run again), not also a persisted
    failure."""
    import json
    import types

    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.subagent_manager.admission.types import QueuedRun, QueuedRunListing
    from kiro_crew.subagent_persistence import PanelRecords

    live = types.SimpleNamespace(
        id="live1",
        task="t",
        done=False,
        parent_session_key="dash:x",
        agent="",
        crew="",
        started=1.0,
        turns=0,
        last_tool="",
        include_memory=True,
        include_lessons=True,
        include_project=True,
        _awaiting_approval=False,
    )
    subagents = MagicMock(all_agents=[live])
    subagents.queued_runs_async = AsyncMock(
        return_value=QueuedRunListing(
            (
                QueuedRun(id="live1", task="t", parent_session_key="dash:x"),
                QueuedRun(id="orph1", task="t", parent_session_key="dash:x"),
            )
        )
    )
    seen: dict[str, object] = {}

    def _records(**kw):
        seen["exclude"] = set(kw["exclude_ids"])
        return PanelRecords([], 0, False)

    with patch.object(messaging, "read_panel_records", side_effect=_records):
        body = json.loads(
            (
                await messaging.api_spawn_list(
                    _Req(MagicMock(subagents=subagents), query={"queued": "1"})
                )
            ).body
        )
    assert [a["id"] for a in body["agents"]] == ["live1"]
    assert [q["id"] for q in body["queued"]] == ["orph1"]
    assert {"live1", "orph1"} <= seen["exclude"]


@pytest.mark.asyncio
async def test_collecting_a_member_whose_announce_already_queued_settles_it(tmp_path) -> None:
    """A slow sibling's delivery timed out waiting on the tool's turn and QUEUED
    its announce; the tool then collected it inline. The queued announce is
    removed (no duplicate completion turn), its delivery mark is written, and
    its id stays out of the inline-collected ids, where nothing would discard
    it and the 'none left, disarm' path would never fire again."""
    from chat_test_helpers import _make_state

    from kiro_crew.constants import SUBAGENT_COMPLETION_META_KEY
    from kiro_crew.dashboard.chat_utils import SUBAGENT_COMPLETION_KIND
    from kiro_crew.dashboard.handlers.messaging import api_spawn_mark_collected
    from kiro_crew.subagent import SubagentDelivery
    from kiro_crew.subagent_completion_meta import single_completion_meta
    from kiro_crew.subagent_inline_collection import InlineCollections

    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    announce = "[Subagent completion event] a1 finished"
    slot.queue_append(
        announce,
        kind=SUBAGENT_COMPLETION_KIND,
        meta={SUBAGENT_COMPLETION_META_KEY: single_completion_meta(agent_id="a1", outcome="ok")},
    )
    owed = [SubagentDelivery("a1", 1.0, 0.0)]
    slot.note_pending_subagent_delivery(announce, owed)
    registry = InlineCollections()
    state.subagents = MagicMock(settle_queued_delivery=AsyncMock(), inline_collections=registry)

    for aid in ("a1", "b1"):
        assert registry.reserve("dashboard:chat-1", aid, 600)
    for phase in ("claim", "commit"):
        body = {"ids": ["a1", "b1"], "parent_session": "dashboard:chat-1", "phase": phase}
        await api_spawn_mark_collected(_Req(state, body=body))

    assert not [q for q in slot._queue if q.get("kind") == SUBAGENT_COMPLETION_KIND]
    assert registry.consume_collected("dashboard:chat-1", "b1")
    assert not registry.consume_collected("dashboard:chat-1", "a1")
    state.subagents.settle_queued_delivery.assert_awaited_once_with(owed)


def test_spawn_list_never_says_none_running_over_a_partial_queue() -> None:
    from kiro_crew.mcp_tools import spawn as spawn_tools

    answer = {"agents": [], "queued_truncated": True}
    with (
        patch.object(spawn_tools.mcp_core, "_get", return_value=answer) as get,
        patch.object(spawn_tools.mcp_core, "list_agents", return_value=[]),
    ):
        out = spawn_tools.spawn_list("spawn_list", {})
    assert "No subagents running." not in out
    assert "partial" in out
    get.assert_called_once_with("/api/spawn?queued=1")


def test_no_child_probe_spells_a_slots_session_key_by_hand() -> None:
    """A channel- or cron-born tab runs on its linked session (``slack:<ts>``),
    so ``f"dashboard:{slot.key}"`` names a session its children never register
    under, and a probe keyed on it answers "no children" while one is queued.
    Every child probe takes ``effective_session_key(slot)`` instead."""
    import ast
    from pathlib import Path

    probes = {
        "running_agents_for",
        "subagents_attached",
        "subagents_attached_async",
        "queued_count_for",
        "queued_count_for_async",
        "queued_count_or_none_async",
        "has_pending_work_for",
        "has_pending_work_for_async",
        "has_in_memory_pending_work_for",
    }
    src = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "dashboard:" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in probes:
                continue
            for arg in node.args:
                if (
                    isinstance(arg, ast.JoinedStr)
                    and arg.values
                    and isinstance(arg.values[0], ast.Constant)
                    and str(arg.values[0].value).startswith("dashboard:")
                ):
                    offenders.append(f"{path.relative_to(src)}:{node.lineno}")
    assert offenders == [], f"hand-built dashboard session keys in child probes: {offenders}"


@pytest.mark.usefixtures("close_subagent_managers")
def test_a_row_refused_at_drain_answers_its_failure_not_not_found() -> None:
    """A drained row the pump re-checks and refuses is FAILED in the store and
    never runs; it is registered as a terminal record, so the caller that was
    told "accepted" reads the refusal instead of a 404."""
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx())
    info = mgr.spawn(
        "summarize the log",
        parent_session_key="dash:refused",
        _from_queue=True,
        _preassigned_id="refused0001",
        _memory_mode="not-a-mode",
    )
    assert info is not None and info.done and info.error.startswith("memory_unavailable")
    registered = mgr.get("refused0001")
    assert registered is info and registered.done and registered.error == info.error
    assert mgr.running_agents_for("dash:refused") == []


def test_a_row_this_build_cannot_read_is_an_outage_not_a_crash(tmp_path) -> None:
    from kiro_crew import taskq
    from kiro_crew.subagent_manager.admission.taskq_bridge import (
        _read_queued_row,
        _read_queued_rows,
    )

    store = _store(tmp_path)
    try:
        store.accept([_row("odd1")])
        with store._lock:
            store._c().execute("UPDATE tasks SET state='from_the_future' WHERE id='odd1'")
        with pytest.raises(taskq.TaskStoreUnavailable):
            _read_queued_row(store, "odd1")
        # A listing filters on state and kind in SQL, so a queued row whose
        # side-effect class is the unknown part is what reaches the model.
        with store._lock:
            store._c().execute(
                "UPDATE tasks SET state='queued', side_effect_class='future' WHERE id='odd1'"
            )
        with pytest.raises(taskq.TaskStoreUnavailable):
            _read_queued_rows(store, session_key=None, app=None, exclude_ids=[])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_an_unreadable_queue_answers_503_not_404() -> None:
    """With the store down and no window entry, "not queued" is unknowable:
    the status route answers a retryable 503, never a definitive 404."""
    import json

    from kiro_crew.dashboard.handlers.messaging import api_spawn_status
    from kiro_crew.subagent_manager.admission.types import QueuedReadUnavailable

    subagents = MagicMock(get=MagicMock(return_value=None))
    subagents.queued_run_async = AsyncMock(side_effect=QueuedReadUnavailable("locked"))
    resp = await api_spawn_status(_Req(MagicMock(subagents=subagents), "q1"))
    assert resp.status == 503
    assert json.loads(resp.body)["code"] == "taskq_unavailable"


@pytest.mark.asyncio
async def test_collected_ids_are_bounded_and_known(tmp_path) -> None:
    """Only ids a reservation covers, each of bounded length, and never more than
    the cap in total: an id no completion will match is never recorded. A call
    naming an id past the length bound is refused whole and records nothing, and
    so is a body with no ``phase``."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.subagent_inline_collection import InlineCollections

    parent = "dashboard:chat-1"
    state = _make_state(tmp_path)
    state.get_or_create_slot("chat-1")
    known = [f"k{i:04d}" for i in range(1500)]
    registry = InlineCollections()
    state.subagents = MagicMock(inline_collections=registry)
    refused = await messaging.api_spawn_mark_collected(
        _Req(state, body={"ids": ["k0000", "x" * 500], "parent_session": parent, "phase": "commit"})
    )
    assert refused.status == 400
    assert registry._records.get(parent, {}) == {}
    reserved = {aid for aid in known if registry.reserve(parent, aid, 600)}
    assert len(reserved) == messaging._COLLECTED_IDS_CAP
    phaseless = await messaging.api_spawn_mark_collected(
        _Req(state, body={"ids": known[:200], "parent_session": parent})
    )
    assert phaseless.status == 400
    assert all(rec.state == "collecting" for rec in registry._records[parent].values())
    for start in range(0, 1500, 200):
        ids = known[start : start + 200] + ["unknown1"]
        for phase in ("claim", "commit"):
            body = {"ids": ids, "parent_session": parent, "phase": phase}
            await messaging.api_spawn_mark_collected(_Req(state, body=body))
    probe = known + ["x" * 500, "unknown1"]
    kept = {aid for aid in probe if registry.consume_collected(parent, aid)}
    assert kept == reserved
