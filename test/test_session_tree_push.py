"""The session tree is PUSHED: a change is announced, re-cited and sent, never polled.

Three promises, one test group each. The projection announces every change on the
crew-log bus. A log opened without a witness re-cites the edge the tree already holds,
so the edge outlives the slot's first log. And the dashboard turns an announcement into
one ``slot_patch`` carrying only the rows whose ``parent`` moved.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

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


def test_nothing_yields_between_the_class_read_and_the_emit():
    """The take clears the slot's predecessor latch. A turn cancelled at an await after it
    would lose the predecessor the next log has to cite, so the seed is awaited first."""
    import inspect

    from kiro_crew.dashboard import chat_runner

    source = inspect.getsource(chat_runner._run_chat)
    take = source.index("slot.take_crew_log_previous(")
    emit_at = source.index("crew_log_emit.on_session_opened(", take)
    seed = source.index("await _crew_log_seed_tree(slot)")
    # The class read is recorded in the same entry, so a yield after it would let a
    # channel binding commit unseen and the opening would state a stale class.
    class_read = source.index("= _crew_log_class(state, slot)")
    assert seed < class_read < take
    assert "await " not in source[class_read:emit_at]


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
