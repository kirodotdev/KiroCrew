"""The session tree is PUSHED: a change is announced, re-cited and sent, never polled.

Three promises, one test group each. The projection announces every change on the
crew-log bus. A log opened without a witness re-cites the edge the tree already holds,
so the edge outlives the slot's first log. And the dashboard turns an announcement into
one ``slot_patch`` carrying only the rows whose ``parent`` moved.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from turn_harness import Do, ScriptedProvider, SlotSpec, TurnScript, run_turn

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, STOP_REASON_END_TURN, AcpEvent
from kiro_crew.crew_log import bus, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree import EDGE_NAMED, EDGE_NONE, EdgeRecord, OpenedRecord
from kiro_crew.dashboard.state import DashboardState, SlotOrigin
from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    bus.reset_for_tests()
    yield
    bus.reset_for_tests()
    stp.reset_for_tests()
    emit.reset_caches()


def _rec(
    sid: str,
    slot: str,
    created: int = 1,
    parent: str | None = None,
    previous: str | None = None,
) -> OpenedRecord:
    return OpenedRecord(
        sid=sid,
        slot=slot,
        created_at=created,
        parent_slot=parent,
        previous_sid=previous,
        previous_edge=EDGE_NAMED if previous else EDGE_NONE,
    )


def _heard() -> list[object]:
    events: list[object] = []
    bus.subscribe(bus.TREE_ADVANCED, events.append)
    return events


# ── the announcement ───────────────────────────────────────────────────────


def test_every_change_is_announced():
    heard = _heard()
    proj = stp.SessionTreeProjection()

    proj.apply(_rec("s-lead", "lead"))
    proj.apply(_rec("s-worker", "worker", created=2, parent="lead"))
    proj.forget("s-worker")

    assert len(heard) == 3


def test_a_record_already_held_is_not_announced():
    heard = _heard()
    proj = stp.SessionTreeProjection()
    proj.apply(_rec("s-lead", "lead"))

    proj.apply(_rec("s-lead", "lead"))

    assert len(heard) == 1


def test_a_seed_is_announced_even_when_it_finds_nothing():
    """The pending frame a sidebar painted is settled by this event, not by a re-read."""
    heard = _heard()
    proj = stp.SessionTreeProjection()

    proj.ensure_seeded()
    proj.ensure_seeded()

    assert len(heard) == 1


# ── the re-citation ────────────────────────────────────────────────────────


def test_a_restored_slot_inherits_the_edge_the_tree_holds():
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead"))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))

    assert stp.inherited_parent("worker", "s-worker-1") == "lead"


def test_the_edge_survives_losing_the_slot_s_first_log():
    """The bug this fixes: the only log naming the creator is the first one dropped."""
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead"))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))
    # The gateway restarts: the worker's next log has no witness of its own and
    # re-cites what the tree holds.
    inherited = stp.inherited_parent("worker", "s-worker-1")
    proj.apply(_rec("s-worker-2", "worker", created=3, parent=inherited, previous="s-worker-1"))

    proj.forget("s-worker-1")

    assert proj.nodes()["worker"].parent_slot == "lead"
    # And the next restart still finds it, from the log that re-cited it.
    assert stp.inherited_parent("worker", "s-worker-2") == "lead"


def test_nothing_is_inherited_before_the_tree_is_seeded():
    proj = stp.projection()
    proj.apply(_rec("s-lead", "lead"))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))

    assert stp.inherited_parent("worker", "s-worker-1") == ""


def test_a_restored_slot_s_first_turn_seeds_before_it_reads():
    """After a restart the projection is unseeded on the very turn that reads it.

    The real path: two logs on disk, a fresh process image of the tree, and the
    call ``chat_runner`` makes for a slot with no witness of its own.
    """
    from kiro_crew.dashboard import chat_runner

    emit.on_session_opened("s-lead", agent="kirocrew-lead", slot="lead")
    emit.on_session_opened("s-worker-1", agent="kirocrew-worker", slot="worker", parent_slot="lead")
    stp.reset_for_tests()
    emit.reset_caches()
    assert not stp.projection().seeded_for_current_store

    restored = SimpleNamespace(key="worker", _created_by="lead")
    asyncio.run(chat_runner._crew_log_seed_tree(restored))
    assert chat_runner._crew_log_inherited_parent(restored, "s-worker-1") == "lead"


@pytest.mark.asyncio
async def test_a_restored_child_that_never_ran_writes_no_creator_into_its_first_log():
    """The nesting a just-dispatched child gets is IN MEMORY, and a restart ends it.

    The sidebar nests such a child from the row's own ``created_by``, gated on
    ``lineage_minted`` -- a witness this process holds and never persists. After a
    restart the slot comes back with the claim and without the witness, so it nests
    nowhere and its first ``session/opened`` cites no creator.

    That is base behaviour and it is pinned here on purpose, because the obvious
    improvement is unsafe: the claim is restored from a transcript an agent's file
    tools can edit, and a slot KEY is reusable, so promoting it would let a fenced
    caller that forged an archived tab's ``created_by`` be recorded as that tab's
    creator in an append-only entry -- which is what
    ``session_control.revive_session``'s ownership check then reads.
    """
    emit.on_session_opened("s-lead", agent="kirocrew-lead", slot="lead")
    assert emit.flush(timeout=5.0) is True
    stp.reset_for_tests()
    emit.reset_caches()

    record = await run_turn(
        TurnScript(events=_LANDS, setup=_restored_worker), slot=SlotSpec(key="worker")
    )

    # ``_restored_worker`` carries the claim and not the witness, which is exactly the
    # shape a restart leaves -- so the refusal below is the rule, not a missing input.
    [opened] = _opened(record)
    assert opened["previous_sid"] == ""
    assert opened["parent_slot"] == ""


def test_a_new_slot_on_a_reused_key_inherits_nothing():
    """A key is reusable; the old worker's edge must not attach to a fresh tab."""
    from kiro_crew.dashboard import chat_runner

    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead"))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))

    fresh = SimpleNamespace(key="worker", _created_by="")
    forged = SimpleNamespace(key="worker", _created_by="someone-else")
    assert chat_runner._crew_log_inherited_parent(fresh, "s-worker-1") == ""
    assert chat_runner._crew_log_inherited_parent(forged, "s-worker-1") == ""
    # No predecessor named, nothing to check the tree against.
    restored = SimpleNamespace(key="worker", _created_by="lead")
    assert chat_runner._crew_log_inherited_parent(restored, "") == ""


def test_an_adopter_is_never_re_cited_as_the_creator():
    """A takeover moves the node; a restored slot then cites no creator at all."""
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead"))
    proj.apply(_rec("s-other", "other", created=2))
    proj.apply(_rec("s-worker-1", "worker", created=3, parent="lead"))
    proj.apply_edge(EdgeRecord(slot="worker", parent_slot="other", at=4, sid="s-other"))

    assert proj.nodes()["worker"].parent_slot == "other"
    assert stp.inherited_parent("worker", "s-worker-1") == ""


def test_a_released_slot_does_not_re_cite_its_creator():
    """Re-citing would bring the edge back once the log recording the release is gone."""
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead"))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))
    proj.apply_edge(EdgeRecord(slot="worker", parent_slot=None, at=3, sid="s-worker-1"))

    assert proj.nodes()["worker"].parent_slot is None
    assert stp.inherited_parent("worker", "s-worker-1") == ""


def test_a_failed_seed_is_retried_without_a_reader(monkeypatch):
    """Nothing polls the tree, so the projection re-attempts a failed seed itself.

    On a timer of its own: the maintenance pool is stubbed to drop every task here, so
    a retry that queued on it would never run.
    """
    import threading
    import time

    monkeypatch.setattr(stp, "SEED_RETRY_COOLDOWN_SECS", 0.05)
    proj = stp.SessionTreeProjection()
    real_seed = proj._seed
    calls: list[int] = []
    done = threading.Event()

    def flaky_seed(live_sids):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("store unreadable")
        real_seed(live_sids)
        done.set()

    monkeypatch.setattr(proj, "_seed", flaky_seed)
    heard = _heard()
    try:
        proj.ensure_seeded()
        assert proj.reading().incomplete
        assert done.wait(5)
        deadline = time.monotonic() + 5
        while len(heard) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        proj.cancel_pending_checkpoint()

    assert len(calls) == 2
    assert not proj.reading().incomplete
    assert len(heard) == 2
    assert proj._seed_retry_timer is None


def test_discarding_the_projection_cancels_an_armed_retry(monkeypatch):
    monkeypatch.setattr(stp, "SEED_RETRY_COOLDOWN_SECS", 60)
    proj = stp.SessionTreeProjection()
    monkeypatch.setattr(proj, "_seed", lambda live_sids: (_ for _ in ()).throw(OSError("x")))
    proj.ensure_seeded()
    timer = proj._seed_retry_timer
    assert timer is not None and timer.is_alive()

    proj.cancel_pending_checkpoint()

    timer.join(1)
    assert not timer.is_alive()


def test_a_store_past_the_cap_still_inherits_for_an_intact_slot(monkeypatch):
    """The review's case: past the cap the store is always partial, and that is not
    a reason to drop a slot whose own logs are all held."""
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 3)
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-old", "old", created=1))
    proj.apply(_rec("s-lead", "lead", created=2))
    proj.apply(_rec("s-worker-1", "worker", created=3, parent="lead"))
    proj.apply(_rec("s-new", "new", created=4))

    assert proj.reading().incomplete
    assert "s-old" not in {r.sid for r in proj.reading().records}
    assert stp.inherited_parent("worker", "s-worker-1") == "lead"


def test_a_gap_in_the_slot_s_own_chain_inherits_nothing(monkeypatch):
    """The citing log is gone and the log after it does not cite: nothing proves it."""
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead", created=1))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))
    proj.apply(_rec("s-worker-2", "worker", created=3, previous="s-worker-1"))
    assert stp.inherited_parent("worker", "s-worker-2") == "lead"

    proj.forget("s-worker-1")

    assert stp.inherited_parent("worker", "s-worker-2") == ""


def test_a_suspect_log_on_the_slot_s_span_inherits_nothing():
    """A decision evicted or unread on the slot's own logs could be the one that moved it."""
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead", created=1))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))
    with proj._lock:
        proj._suspect_sids.add("s-worker-1")

    assert stp.inherited_parent("worker", "s-worker-1") == ""


def test_a_suspect_log_older_than_the_citation_does_not_matter():
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead", created=1))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))
    proj.apply(_rec("s-worker-2", "worker", created=3, parent="lead", previous="s-worker-1"))
    with proj._lock:
        proj._suspect_sids.add("s-worker-1")

    assert stp.inherited_parent("worker", "s-worker-2") == "lead"


def test_a_fault_with_no_owner_inherits_nothing_for_anyone():
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-lead", "lead", created=1))
    proj.apply(_rec("s-worker-1", "worker", created=2, parent="lead"))
    with proj._lock:
        proj._unattributed_gap = True

    assert stp.inherited_parent("worker", "s-worker-1") == ""


def test_an_evicted_decision_makes_its_log_suspect(monkeypatch):
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-worker-1", "worker", created=1, parent="lead"))
    proj.apply_edge(EdgeRecord(slot="worker", parent_slot="lead", at=1, sid="s-worker-1"))
    proj.apply_edge(EdgeRecord(slot="third", parent_slot=None, at=2, sid="s-third"))
    proj.apply_edge(EdgeRecord(slot="other", parent_slot=None, at=2, sid="s-other"))

    assert "s-worker-1" in proj._suspect_sids


def test_nothing_is_inherited_from_a_cycle_or_a_root():
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-a", "a", parent="b"))
    proj.apply(_rec("s-b", "b", created=2, parent="a"))
    proj.apply(_rec("s-root", "root", created=3))

    assert stp.inherited_parent("a", "s-a") == ""
    assert stp.inherited_parent("root", "s-root") == ""
    assert stp.inherited_parent("", "s-root") == ""
    assert stp.inherited_parent("root", "") == ""


# ── the push ───────────────────────────────────────────────────────────────


class _WS:
    def __init__(self) -> None:
        self.closed = False
        self.send_str = AsyncMock()
        self._flags = {"_is_dashboard_user": True, SLOT_PATCH_WS_FLAG: True}

    def get(self, key, default=None):
        return self._flags.get(key, default)

    def patches(self) -> list[list[dict]]:
        frames = [json.loads(call.args[0]) for call in self.send_str.call_args_list]
        return [f["data"]["slots"] for f in frames if f["type"] == "slot_patch"]


@pytest.fixture
def state(monkeypatch, tmp_path):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    yield DashboardState(
        sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    loop.close()
    asyncio.set_event_loop(None)


def test_a_moved_parent_is_pushed_once_as_a_patch(state, monkeypatch):
    for key in ("lead", "worker"):
        state.get_or_create_slot(key, origin=SlotOrigin.USER)
    ws = _WS()
    state.register_ws(ws)  # type: ignore[arg-type]
    parents: dict[str, object] = {"lead": None, "worker": None}

    def fake_attach(rows, _aliases=None):
        for row in rows:
            row["parent"] = parents.get(row["key"])

    monkeypatch.setattr("kiro_crew.dashboard.state._attach_slot_parents", fake_attach)

    state.push_lineage_patch()
    parents["worker"] = {"slot": "lead", "key": "lead"}
    state.push_lineage_patch()
    state.push_lineage_patch()

    assert ws.patches() == [
        [
            {"key": "lead", "parent": None, "lineage_pending": False},
            {"key": "worker", "parent": None, "lineage_pending": False},
        ],
        [{"key": "worker", "parent": {"slot": "lead", "key": "lead"}, "lineage_pending": False}],
    ]


def _opened_log(sid: str, slot: str, *, parent: str | None = None) -> None:
    """One session log on disk, opened the way the emitter opens one."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog

    handle = CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)
    data: dict[str, object] = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": False,
    }
    if parent:
        data["parent"] = {"slot": parent}
    handle.append("session/opened", data, src="gateway")
    del handle


def _dispatch(state, key: str, creator: str) -> None:
    """A child minted by *creator* through ``session_create``, before its first turn."""
    child = state.get_or_create_slot(key, origin=SlotOrigin.USER)
    child._created_by = creator
    child._lineage_minted = True


def test_a_sibling_s_first_log_keeps_a_pending_child_nested(state):
    """Two dispatched workers; A opens its log first and the tree's patch fires.

    B still has no node, so only its own mint witness can place it. The patch must
    nest B under the lead exactly as the full frame does, not send ``parent: null``.
    """
    state.get_or_create_slot("lead", origin=SlotOrigin.USER)
    _dispatch(state, "worker-a", "lead")
    _dispatch(state, "worker-b", "lead")
    ws = _WS()
    state.register_ws(ws)  # type: ignore[arg-type]
    _opened_log("s-lead", "lead")
    _opened_log("s-a", "worker-a", parent="lead")
    stp.projection().ensure_seeded()
    assert "worker-b" not in stp.projection().nodes(), "B must not have a node yet"

    state.push_lineage_patch()

    rows = {row["key"]: row for frame in ws.patches() for row in frame}
    assert rows["worker-a"]["parent"] == {"slot": "lead", "key": "lead"}
    assert rows["worker-b"]["parent"] == {"slot": "lead", "key": "lead"}


def test_a_removed_lead_re_states_its_pending_child_as_orphaned(state):
    """The lead closes before its dispatched child ran: the removal patch must carry
    the child with ``parent.key: None``, the value the full frame now gives it."""
    state.get_or_create_slot("lead", origin=SlotOrigin.USER)
    _dispatch(state, "worker-b", "lead")
    ws = _WS()
    state.register_ws(ws)  # type: ignore[arg-type]
    _opened_log("s-lead", "lead")
    stp.projection().ensure_seeded()

    state._slots.pop("lead")
    state.push_slot_removed("lead")

    assert [{"key": "worker-b", "parent": {"slot": "lead", "key": None}}] in ws.patches()


def test_the_publisher_coalesces_a_burst_into_one_push():
    from kiro_crew.dashboard.handlers.crew_log import COALESCE_SECONDS, CrewLogPublisher

    loop = asyncio.new_event_loop()
    try:
        fake_state = MagicMock()
        publisher = CrewLogPublisher(fake_state)
        publisher._loop = loop
        for _ in range(3):
            publisher.on_tree_advanced(bus.TreeAdvanced())
        loop.run_until_complete(asyncio.sleep(COALESCE_SECONDS * 2 + 0.05))
    finally:
        loop.close()

    assert fake_state.push_lineage_patch.call_count == 1


@pytest.mark.asyncio
async def test_installing_the_publisher_pushes_once_for_a_seed_it_missed(monkeypatch):
    """The bus keeps nothing: a seed announced before the subscription reached nobody."""
    from unittest.mock import patch

    from kiro_crew.dashboard.handlers import crew_log as routes

    monkeypatch.setenv(routes.CREW_LOG_ENV, "1")
    fake_state = MagicMock()
    with (
        patch.object(routes, "_publisher", None),
        patch.object(emit, "_growth_listeners", []),
    ):
        stp.projection().ensure_seeded()
        routes.install_crew_log_publisher(fake_state)
        await asyncio.sleep(routes.COALESCE_SECONDS * 2 + 0.05)

    assert fake_state.push_lineage_patch.call_count == 1


def test_a_cold_scan_marks_a_log_whose_decision_could_not_be_read(monkeypatch):
    """The real cold path: logs on disk, no checkpoint, one unit's tail faults."""
    from kiro_crew.crew_log.session_tree import SessionTree

    emit.on_session_opened("s-lead", agent="kirocrew-lead", slot="lead")
    emit.on_session_opened("s-worker-1", agent="kirocrew-worker", slot="worker", parent_slot="lead")
    stp.reset_for_tests()
    emit.reset_caches()
    real = SessionTree._read_edge

    def faulting(self, directory, record):
        if record.slot == "worker":
            return None, True
        return real(self, directory, record)

    monkeypatch.setattr(SessionTree, "_read_edge", faulting)
    proj = stp.projection()
    proj.ensure_seeded()

    assert "s-worker-1" in proj._suspect_sids
    assert not proj._unattributed_gap
    assert stp.inherited_parent("worker", "s-worker-1") == ""


def test_the_suspect_set_holds_only_units_the_tree_still_holds(monkeypatch):
    """Bounded like the records: an evicted or forgotten unit leaves the set."""
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    proj = stp.projection()
    proj.ensure_seeded()
    proj.apply(_rec("s-a", "a", created=1))
    proj.apply(_rec("s-b", "b", created=2))
    with proj._lock:
        proj._suspect_sids.update({"s-a", "s-b"})

    proj.apply(_rec("s-c", "c", created=3))
    proj.forget("s-b")
    for n in range(10):
        proj.apply_edge(EdgeRecord(slot=f"x{n}", parent_slot=None, at=10 + n, sid=f"s-x{n}"))

    assert proj._suspect_sids <= {r.sid for r in proj.reading().records}
    assert proj._suspect_sids == set()


def test_the_walk_passes_through_logs_loaded_from_a_checkpoint():
    """A checkpoint keeps ``previous_sid`` and not the scanner's edge kind; the walk must
    still follow it, or a parentless successor would stop it on every warm restart."""
    emit.on_session_opened("s-lead", agent="kirocrew-lead", slot="lead")
    emit.on_session_opened("s-worker-1", agent="kirocrew-worker", slot="worker", parent_slot="lead")
    emit.on_session_opened(
        "s-worker-2", agent="kirocrew-worker", slot="worker", previous_sid="s-worker-1"
    )
    stp.reset_for_tests()
    emit.reset_caches()
    cold = stp.projection()
    cold.ensure_seeded()
    assert cold.flush_checkpoint() is True
    stp.reset_for_tests()

    warm = stp.projection()
    warm.ensure_seeded()

    held = {r.sid: r for r in warm.reading().records}
    assert held["s-worker-2"].previous_sid == "s-worker-1"
    assert stp.inherited_parent("worker", "s-worker-2") == "lead"


class _SeedHook:
    """Stands in for the tree's ``ensure_seeded`` -- the one await before a restored
    slot's log is opened -- and runs *during* it, on the worker thread it runs on."""

    def __init__(self, during=None) -> None:
        self._real = stp.projection().ensure_seeded
        self._during = during
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1
        if self._during is not None and self.calls == 1:
            self._during()
        self._real()


def _restored_worker(ctx, *, predecessor: str = "") -> None:
    """A slot restored after a restart: its creator is known, no lineage minted."""
    ctx.slot._created_by = "lead"
    ctx.slot._lineage_minted = False
    if predecessor:
        ctx.slot._crew_log_opened_sid = predecessor


def _opened(record) -> list[dict]:
    return [dict(call.kwargs) for call in record.crew("on_session_opened")]


_LANDS = [
    AcpEvent(kind=EVENT_TEXT_CHUNK, text="ok"),
    AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
]


class _NewStore(ScriptedProvider):
    """The slot's next turn lands on a new store."""

    @property
    def session_id(self) -> str:
        return "acp-new"


@pytest.mark.asyncio
async def test_a_channel_bound_while_the_tree_seeds_is_in_the_opening_class(monkeypatch):
    """The class is read AFTER the seed's await, so a channel binding that commits
    while the tree seeds is in the opening entry -- not a stale "unpublished" class
    whose later move had no log to land in."""
    held: dict = {}

    def _bind_now() -> None:
        # The binding commits on the loop while the seed runs on its worker.
        bound = threading.Event()

        def _bind() -> None:
            held["slot"].linked_session_key = "telegram:-100999"
            bound.set()

        held["loop"].call_soon_threadsafe(_bind)
        assert bound.wait(timeout=5)

    seed = _SeedHook(during=_bind_now)
    monkeypatch.setattr(stp.projection(), "ensure_seeded", seed)

    def _arrange(ctx) -> None:
        _restored_worker(ctx)
        held["slot"], held["loop"] = ctx.slot, asyncio.get_running_loop()

    def _unbind(ctx) -> None:
        # Back to unlinked once the log is open: delivery is not under test.
        ctx.slot.linked_session_key = ""

    record = await run_turn(
        TurnScript(events=[Do(_unbind), *_LANDS], setup=_arrange), slot=SlotSpec(key="worker")
    )
    assert seed.calls == 1, "the tree was never seeded, so nothing was tested"
    [opened] = _opened(record)
    assert opened["channel"] is True


@pytest.mark.asyncio
async def test_a_turn_cancelled_while_the_tree_seeds_keeps_its_predecessor(monkeypatch):
    """The predecessor latch is read-and-clear, owed to exactly one log. The take
    comes after the seed's await, so a turn cancelled there (Stop, tab close,
    shutdown) leaves it for the slot's NEXT log to cite -- taken first, it would
    be lost and that log would cite the wrong store, a gap no chain walker sees."""
    held: dict = {}

    def _cancel_the_turn() -> None:
        held["loop"].call_soon_threadsafe(held["slot"].task.cancel)

    monkeypatch.setattr(stp.projection(), "ensure_seeded", _SeedHook(during=_cancel_the_turn))

    def _arrange(ctx) -> None:
        _restored_worker(ctx, predecessor="s-prev")
        held["slot"], held["loop"] = ctx.slot, asyncio.get_running_loop()

    record = await run_turn(
        TurnScript(
            events=_LANDS,
            setup=_arrange,
            then=TurnScript(events=_LANDS, message="again", provider=_NewStore),
        ),
        slot=SlotSpec(key="worker"),
    )
    # The cancel landed in the seed: no log opened and the provider was never prompted.
    assert _opened(record) == []
    assert record.calls("stream") == []
    assert record.then is not None
    [opened] = _opened(record.then)
    assert opened["previous_sid"] == "s-prev"


@pytest.mark.asyncio
async def test_a_cancel_requested_while_the_class_is_read_finds_the_log_opened(monkeypatch):
    """Nothing yields from the class read to the emit: the class, the predecessor
    take and the ``session/opened`` entry are one moment. A cancel requested while
    the class is being read therefore lands AFTER the log is open; a yield inside
    that span would deliver it there and lose both the opening and the latch."""
    held: dict = {"seeded": False, "armed": False}

    def _seeded() -> None:
        held["seeded"] = True

    monkeypatch.setattr(stp.projection(), "ensure_seeded", _SeedHook(during=_seeded))

    def _arrange(ctx) -> None:
        _restored_worker(ctx, predecessor="s-prev")
        read_mirror = ctx.state.sessions.get_mirror_link

        def _get_mirror_link(key):
            # The opening class read is the first mirror probe after the seed.
            if held["seeded"] and not held["armed"]:
                held["armed"] = True
                asyncio.get_running_loop().call_soon(ctx.slot.task.cancel)
            return read_mirror(key)

        ctx.state.sessions.get_mirror_link = _get_mirror_link

    record = await run_turn(TurnScript(events=_LANDS, setup=_arrange), slot=SlotSpec(key="worker"))
    assert held["armed"], "the class read never probed the mirror, so nothing was tested"
    [opened] = _opened(record)
    assert opened["previous_sid"] == "s-prev"
    # ...and the cancel did land, right after: the provider was never prompted.
    assert record.calls("stream") == []


def test_a_replay_marks_every_unit_whose_decision_it_did_not_read(monkeypatch):
    """Past the cap the replay keeps records it reads no decision for; those are suspect."""
    from kiro_crew.crew_log.session_tree import _ScanFaults

    emit.on_session_opened("s-a", agent="kirocrew-worker", slot="a")
    emit.on_session_opened("s-b", agent="kirocrew-worker", slot="b")
    emit.on_session_opened("s-c", agent="kirocrew-worker", slot="c")
    emit.reset_caches()
    from kiro_crew.crew_log import session_tree

    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    monkeypatch.setattr(session_tree, "TREE_UNIT_CAP", 2)
    proj = stp.SessionTreeProjection()
    faults = _ScanFaults()
    loaded = {sid: _rec(sid, sid[2:]) for sid in ("s-a", "s-b", "s-c")}

    records, *_ = proj._replay_tail(loaded, {}, faults=faults)

    unread = [r.sid for r in list(records.values())[2:]]
    assert unread
    assert set(unread) <= set(faults.sids)
