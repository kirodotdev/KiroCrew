"""Session-keyed folds are eager: advanced as entries land, pushed with a revision.

The slot half of eager folding lives in ``test_crew_log_fold_eager.py``. This file pins
the half the dashboard's crew-log panel reads -- ``status``, ``usage``, ``timeline``,
``tools``, ``approvals``, ``subagents`` and the internal ``class`` -- which rides its own
warm memo (``projection.fold_session_warm``), the same revision counter as the slot memo,
and a byte budget of its own.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, bus
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers import crew_log as routes

GATEWAY = "gateway"
SESSION = "s-session-eager"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, with no warm cell or subscriber left over."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()
    yield
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()


def _log(unit_id: str = SESSION, **fields) -> CrewLog:
    fields.setdefault("owner", "raymond")
    fields.setdefault("agent", "kirocrew")
    return CrewLog.create(lg.KIND_SESSION, unit_id, **fields)


def _turn(handle: CrewLog, turn: int) -> int:
    """One whole turn, which moves ``status``, ``usage`` and ``timeline``; its last seq."""
    handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY)
    done = handle.append(
        "turn/completed",
        {
            "turn": turn,
            "stop_reason": "end_turn",
            "depth": 0,
            "duration_ms": 900,
            "model": "opus",
            "provider": "kiro",
            "credits": 0.5,
            "tokens": {"input": 100, "output": 20, "cache_read": 5, "cache_write": 1},
        },
        src=GATEWAY,
    )
    return int(done.seq)


def _events() -> list:
    """Subscribe a sink to the bus and return it: every SESSION event, in order."""
    seen: list = []
    bus.subscribe(
        bus.FOLD_ADVANCED,
        lambda event: seen.append(event) if event.scope == bus.SCOPE_SESSION else None,
    )
    return seen


def _wake(unit_id: str, entry_type: str, seq: int) -> None:
    eager.note_commit(unit_id, entry_type, seq)
    assert eager.drain(timeout=10.0)


def _memo_sessions() -> set[str]:
    return {key[1] for key in crew_log._session_memos}


# --------------------------------------------------------------------------- #
# The fold advances off the append path
# --------------------------------------------------------------------------- #


def test_a_wake_advances_the_units_session_folds_with_no_reader():
    """The default posture: a committed entry folds ``status`` before anyone asks."""
    handle = _log()
    seq = _turn(handle, 1)
    _wake(SESSION, "turn/completed", seq)

    assert SESSION in _memo_sessions(), "no warm session cell after a wake; still lazy"
    held = crew_log._session_memos[(str(crew_log.data_home()), SESSION)]
    assert held.bundle.checkpoints["status"].last_seq == seq
    assert held.revisions["usage"] > 0


def test_a_wake_publishes_each_advertised_fold_it_moved_once():
    """One event per (unit, fold) per batch, carrying the rendered value and a revision."""
    seen = _events()
    handle = _log()
    eager.stop_for_tests()
    seqs = [_turn(handle, turn) for turn in (1, 2, 3)]
    for seq in seqs:
        eager.note_commit(SESSION, "turn/completed", seq)
    queued = eager._queue
    assert queued is not None
    batch, closers, _taken = eager._coalesce(queued.get_nowait())
    eager._fold_batch(batch, closers)

    names = [event.fold for event in seen]
    assert len(names) == len(set(names)), f"a fold published twice in one batch: {names}"
    assert {"status", "usage", "timeline"} <= set(names)
    usage = next(event for event in seen if event.fold == "usage")
    assert usage.key == SESSION and usage.revision > 0 and usage.seq == seqs[-1]
    assert usage.value == crew_log.read_projection(SESSION, "usage").value


def test_a_fold_the_batch_did_not_move_publishes_nothing():
    """MUTATION-SENSITIVE: an unchanged fold keeps its revision, so it costs no event."""
    seen = _events()
    handle = _log()
    _wake(SESSION, "turn/completed", _turn(handle, 1))
    tools_before = crew_log.fold_session_warm(SESSION).revisions["tools"]
    seen.clear()

    _wake(SESSION, "turn/completed", _turn(handle, 2))

    assert "tools" not in {event.fold for event in seen}, "tools did not move but was sent"
    assert crew_log.fold_session_warm(SESSION).revisions["tools"] == tools_before


def test_each_revision_of_one_fold_is_higher_than_the_last():
    seen = _events()
    handle = _log()
    for turn in (1, 2, 3):
        _wake(SESSION, "turn/completed", _turn(handle, turn))
    revisions = [event.revision for event in seen if event.fold == "usage"]
    assert len(revisions) == 3
    assert revisions == sorted(set(revisions))


def test_the_revision_rises_across_a_unit_recreated_under_the_same_id():
    """MUTATION-SENSITIVE: a recreated unit restarts its seqs, never its revisions.

    Planted as the memo shape a recreation leaves -- a held bundle whose ORIGIN names a
    retired file -- so it behaves the same on every platform. A memo that compared seqs
    would read the rebuilt fold as unchanged and push nothing.
    """
    handle = _log()
    _turn(handle, 1)
    first = crew_log.fold_session_warm(SESSION)
    key = (str(crew_log.data_home()), SESSION)
    held = crew_log._session_memos[key]
    crew_log._session_memos[key] = held._replace(
        bundle=crew_log.SessionProjections(
            session_id=SESSION,
            last_seq=held.bundle.last_seq,
            checkpoints=held.bundle.checkpoints,
            origin="a-retired-file",
        )
    )
    second = crew_log.fold_session_warm(SESSION)
    assert "usage" in second.changed, "a rebuilt fold must mint, not be read as unchanged"
    assert second.revisions["usage"] > first.revisions["usage"]


def test_a_closer_publishes_the_closed_status_then_drops_the_cell():
    """Session folds READ the closer, so its value is sent before the cell goes."""
    seen = _events()
    handle = _log()
    _wake(SESSION, "turn/completed", _turn(handle, 1))
    closed = handle.append("session/closed", {"reason": "done"}, src=GATEWAY)
    seen.clear()
    _wake(SESSION, "session/closed", int(closed.seq))

    status = [event for event in seen if event.fold == "status"]
    assert status, "the closing status was never published"
    assert status[-1].seq == int(closed.seq)
    assert SESSION not in _memo_sessions(), "a closed unit's warm cell was kept"


def test_the_disk_savepoint_is_brought_forward_by_the_warm_path():
    """A memo that always reused ``since=`` would leave the savepoint where it started."""
    handle = _log()
    _turn(handle, 1)
    crew_log.fold_session_warm(SESSION)
    turns = savepoints.MIN_ADVANCE_ENTRIES // 2 + 4
    for turn in range(2, turns + 2):
        _turn(handle, turn)
        crew_log.fold_session_warm(SESSION)
    path = savepoints.checkpoint_path(handle.kind, handle.id, "status")
    assert path.exists(), "the warm path never wrote a savepoint"
    reloaded = savepoints.load(handle, ("status",))
    assert reloaded is not None
    assert handle.last_seq - reloaded.saved_seq < savepoints.MIN_ADVANCE_ENTRIES


# --------------------------------------------------------------------------- #
# The byte budget
# --------------------------------------------------------------------------- #


def test_the_session_table_is_bounded_in_bytes_least_recently_advanced_first(monkeypatch):
    units = [f"s-budget-{index}" for index in range(3)]
    for unit in units:
        _turn(_log(unit), 1)
    crew_log.fold_session_warm(units[0])
    one = crew_log.session_fold_memo_bytes()
    assert one > 0
    monkeypatch.setenv(crew_log.SESSION_FOLD_CACHE_BYTES_ENV, str(one * 2))
    crew_log.forget_session_folds()
    for unit in units:
        crew_log.fold_session_warm(unit)

    assert _memo_sessions() == set(
        units[1:]
    ), f"held {sorted(_memo_sessions())}; a two-cell budget keeps the two newest"
    assert crew_log.session_fold_memo_bytes() <= one * 2
    # Correctness is untouched: the evicted unit still folds its own record.
    assert crew_log.fold_session_warm(units[0]).bundle.checkpoints["status"].last_seq > 0


def test_a_session_cell_above_the_whole_ceiling_is_served_but_not_kept(monkeypatch):
    _turn(_log(), 1)
    monkeypatch.setenv(crew_log.SESSION_FOLD_CACHE_BYTES_ENV, "1")
    warm = crew_log.fold_session_warm(SESSION)
    assert warm.bundle.checkpoints["usage"].last_seq > 0
    assert SESSION not in _memo_sessions()


def test_a_session_cell_is_charged_its_serialized_size():
    _turn(_log(), 1)
    warm = crew_log.fold_session_warm(SESSION)
    expected = sum(
        crew_log.session_fold_state_bytes(warm.bundle.checkpoints[name].state)
        for name in crew_log.EAGER_SESSION_FOLD_NAMES
    )
    assert crew_log.session_fold_memo_bytes() == expected


# --------------------------------------------------------------------------- #
# The bus
# --------------------------------------------------------------------------- #


def test_the_bus_fans_out_in_registration_order_past_a_raising_subscriber():
    order: list[str] = []
    bus.subscribe("k", lambda _event: order.append("first"))
    bus.subscribe("k", lambda _event: (_ for _ in ()).throw(RuntimeError("no")))
    bus.subscribe("k", lambda _event: order.append("third"))
    bus.publish("k", object())
    assert order == ["first", "third"]
    assert bus.subscriber_count("k") == 3


# --------------------------------------------------------------------------- #
# The WS exporter and the route
# --------------------------------------------------------------------------- #


class _Sockets:
    def __init__(self) -> None:
        self.frames: list[tuple[str, dict]] = []

    def dashboard_user_ws_count(self) -> int:
        return 1

    def broadcast_ws_owners(self, frame: str, data: dict) -> None:
        self.frames.append((frame, data))


def _session_event(fold: str, revision: int, value: dict | None = None) -> bus.FoldAdvanced:
    return bus.FoldAdvanced(
        scope=bus.SCOPE_SESSION,
        key=SESSION,
        fold=fold,
        revision=revision,
        value=value if value is not None else {"n": revision},
        seq=revision,
    )


@pytest.mark.asyncio
async def test_the_exporter_sends_the_newest_value_per_fold_once_per_window(monkeypatch):
    """A burst of session events is ONE frame per fold, carrying the newest revision."""
    monkeypatch.setattr(routes, "COALESCE_SECONDS", 0.01)
    _log(SESSION, slot="dashboard:panel")
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    for revision in (5, 7, 6):
        publisher.on_fold_advanced(_session_event("status", revision))
    publisher.on_fold_advanced(_session_event("class", 8))
    await asyncio.sleep(0.2)

    frames = [data for name, data in state.frames if name == routes.FRAME]
    assert [(f["name"], f["revision"]) for f in frames] == [("status", 7)]
    assert frames[0]["slot"] == "dashboard:panel"
    assert frames[0]["session_id"] == SESSION

    # A lower revision arriving later is refused, whichever source sends it.
    assert not await publisher._send_session(SESSION, "status", 6, 6, {"n": 6})


@pytest.mark.asyncio
async def test_a_rebind_inside_the_window_delivers_held_values_on_the_new_loop(monkeypatch):
    """A restart that lands while a session value is held must not silence the exporter.

    The window's timer was armed on the retired loop and will never fire there, so a
    rebind that kept the armed flag would hold every later value for a window that
    never closes, and the held map would only grow.
    """
    monkeypatch.setattr(routes, "COALESCE_SECONDS", 0.01)
    _log(SESSION, slot="dashboard:panel")
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    retired = asyncio.new_event_loop()
    try:
        publisher.bind(retired)
        publisher._schedule_session(SESSION, "status", 3, 3, {"n": 3})
        assert publisher._sessions_armed
    finally:
        retired.close()

    publisher.bind(asyncio.get_running_loop(), state)
    await asyncio.sleep(0.2)
    publisher.on_fold_advanced(_session_event("status", 4))
    await asyncio.sleep(0.2)

    frames = [data for name, data in state.frames if name == routes.FRAME]
    assert [(f["name"], f["revision"]) for f in frames] == [("status", 3), ("status", 4)]
    assert publisher._pending == {}


@pytest.mark.asyncio
async def test_the_growth_backstop_sends_nothing_the_bus_already_sent():
    handle = _log()
    _wake(SESSION, "turn/completed", _turn(handle, 1))
    state = _Sockets()
    publisher = routes.CrewLogPublisher(state)
    publisher.bind(asyncio.get_running_loop())
    warm = crew_log.fold_session_warm(SESSION)
    for name in crew_log.PROJECTION_NAMES:
        rendered = warm.projection(name)
        if rendered.revision:
            await publisher._send_session(
                SESSION, name, rendered.revision, rendered.seq, rendered.value
            )
    state.frames.clear()

    assert not await publisher._publish(SESSION)
    assert state.frames == []


def test_the_panel_route_serves_the_warm_folds_with_their_revisions_and_unit():
    """The baseline carries the same revisions a push does, so the panel can order them."""
    handle = _log()
    _turn(handle, 1)
    warm = crew_log.fold_session_warm(SESSION)
    for name in crew_log.PROJECTION_NAMES:
        assert crew_log.read_projection(SESSION, name).revision == warm.revisions[name]


def test_the_published_revision_floor_is_bounded(monkeypatch):
    """The floor handed to a new socket keeps at most ``MAX_CACHED_SLOT_OWNERS`` slots."""
    monkeypatch.setattr(routes, "MAX_CACHED_SLOT_OWNERS", 2)
    publisher = routes.CrewLogPublisher(_Sockets())
    for index in range(4):
        publisher._record_revision(f"dashboard:{index}", "work", index + 1)
    publisher._record_revision("dashboard:3", "work", 1)
    assert publisher.known_revisions() == {"dashboard:2": {"work": 3}, "dashboard:3": {"work": 4}}


def test_the_shutdown_barrier_waits_for_the_eager_folder(monkeypatch):
    """MUTATION-SENSITIVE: ``drain_for_shutdown`` settles the fold worker, not just the writer.

    Every committed entry wakes the worker, which reads the unit's log and writes its
    savepoint into the data home. A caller that tears the home down right after the
    barrier -- a test's temp directory, an exiting gateway -- would otherwise race a fold
    still holding files there (``WinError 32`` on Windows).
    """
    settled: list[float] = []
    monkeypatch.setattr(eager, "drain", lambda timeout: settled.append(timeout) or True)
    emit.drain_for_shutdown(0.1)
    assert settled, "the shutdown barrier returned without waiting for the eager folder"
    assert settled[0] >= emit._EAGER_SETTLE_FLOOR_SECONDS


# --------------------------------------------------------------------------- #
# A removal never unlinks a file a fold has open
# --------------------------------------------------------------------------- #


def test_a_removal_waits_for_a_fold_that_has_the_log_open(monkeypatch):
    """Windows refuses to unlink a file any handle holds, and a fold READS the log
    without the lease a removal takes. So a removal waits for the batch in flight.

    Simulated where the platform does not refuse: the stand-in fold holds the log open
    until released, and the order of "handle closed" and "unlink started" is the answer.
    """
    import threading

    from kiro_crew.crew_log import store

    handle = _log()
    seq = _turn(handle, 1)
    path = handle.path
    del handle
    emit.reset_caches()
    order: list[str] = []
    folding, release = threading.Event(), threading.Event()

    def holding_fold(batch, closers=None) -> None:
        with open(path, "rb"):
            folding.set()
            release.wait(10)
            order.append("fold closed the log")

    real_contents = store._remove_unit_contents

    def recorded_contents(directory):
        order.append("unlink started")
        return real_contents(directory)

    monkeypatch.setattr(eager, "_fold_batch", holding_fold)
    monkeypatch.setattr(store, "_remove_unit_contents", recorded_contents)
    eager.note_commit(SESSION, "turn/completed", seq)
    assert folding.wait(10), "the stand-in fold never started"
    outcome: list[str] = []
    remover = threading.Thread(
        target=lambda: outcome.append(
            store.remove_unit(lg.KIND_SESSION, SESSION, guard=lambda _dir: True)
        )
    )
    remover.start()
    remover.join(0.3)
    assert order == [], "the removal unlinked while a fold had the log open"
    release.set()
    remover.join(10)
    assert order == ["fold closed the log", "unlink started"]
    assert outcome == [store.REMOVE_REMOVED]


def test_a_removal_on_the_event_loop_does_not_wait_for_the_folder():
    """On the loop the pause is one attempt: a held folder yields ``False`` at once."""
    assert eager._fold_gate.acquire(timeout=1)
    try:

        async def _try() -> bool:
            with eager.paused() as held:
                return held

        assert asyncio.run(_try()) is False
    finally:
        eager._fold_gate.release()
    with eager.paused() as held:
        assert held is True
