"""The slots wire carries the ordering stamp the client needs to date a snapshot.

A WebSocket frame serialized before a
slot was popped, but delivered after the close's own GET, was applied with no ordering
data and resurrected the closed row. The client can only refuse it if the server dates
every snapshot, so these pin the stamp's presence and its monotonicity.
"""

import json

from kiro_crew.dashboard.state import SLOTS_EPOCH, DashboardState, _slots_ws_frame


def _frame(**over):
    args = dict(
        yolo=False,
        channel_trusted=False,
        gitlab_hosts_gen=1,
        folders=None,
        folders_gen=1,
        governance_gen=1,
        slots_gen=7,
    )
    args.update(over)
    return json.loads(_slots_ws_frame([], **args))


def test_the_slots_push_frame_carries_the_generation() -> None:
    assert _frame()["slotsGeneration"] == 7
    # Distinct from the other counters riding the same frame, which order their own
    # payloads: a client comparing the wrong one would refuse valid snapshots.
    assert _frame(gitlab_hosts_gen=99)["slotsGeneration"] == 7


def test_the_push_frame_carries_the_process_epoch_beside_the_counter() -> None:
    """A counter alone is not comparable across a restart: it resumes at 0 while a
    still-loaded client keeps its high value, so every fresh snapshot would read as stale."""
    assert _frame()["slotsEpoch"] == SLOTS_EPOCH
    assert len(SLOTS_EPOCH) == 32, "expected a uuid4 hex, which cannot collide across restarts"


def test_the_generation_is_monotonic_across_emissions() -> None:
    state = DashboardState.__new__(DashboardState)
    seen = [state.next_slots_generation() for _ in range(4)]
    assert seen == sorted(set(seen)), seen
    assert seen[0] >= 1, "a first emission must not be 0, which the client treats as unset"


def test_the_generation_survives_a_state_that_never_ran_init() -> None:
    """Several endpoint suites build the state with ``__new__``; the counter must still
    advance rather than raising, because the push path runs on those fixtures."""
    state = DashboardState.__new__(DashboardState)
    assert state.next_slots_generation() == 1
    assert state.next_slots_generation() == 2


def test_the_stamp_is_drawn_before_the_rows_are_read() -> None:
    """Serializing first and stamping afterwards is the resurrection defect itself.

    A close that pops a slot inside that window leaves the frame carrying PRE-pop rows
    under a number drawn later than the post-pop GET's, so the client ranks the older
    rows newer and puts the closed row back. Reading the counter at row-read time is
    what distinguishes the two orders: under the late stamp it lags the emitted value.
    """
    state = DashboardState.__new__(DashboardState)
    state._slots = {}
    counter_at_read: list[int] = []

    def _read(**_kw: object) -> list:
        counter_at_read.append(int(getattr(state, "_slots_generation", 0)))
        return []

    state.serialize_slots = _read  # type: ignore[method-assign]
    generation, rows = state.stamped_slots()

    assert rows == []
    assert counter_at_read == [generation], (
        f"rows were read at generation {counter_at_read} but the snapshot went out "
        f"stamped {generation}: the stamp must precede the read, not follow it"
    )


def test_both_emitting_paths_draw_their_stamp_through_the_seam() -> None:
    """The ordering only holds while every emitter goes through ``stamped_slots``.

    A path that calls ``next_slots_generation`` itself can reorder the two again, and
    the reordering is invisible in any single frame -- it shows up only as a race under
    load. So pin the callers, not just the seam: the counter may be drawn in exactly one
    place, the seam, which draws it before reading the rows.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"
    state_src = (root / "state.py").read_text(encoding="utf-8")
    assert state_src.count("def stamped_slots") == 1, "the seam itself must exist"
    assert state_src.count("next_slots_generation") == 2, (
        "expected exactly the definition and the single call inside stamped_slots; "
        "another site means an emitter stamps on its own and can reorder the two"
    )
    handlers_src = (root / "chat_handlers.py").read_text(encoding="utf-8")
    assert (
        "next_slots_generation" not in handlers_src
    ), "the GET path must take its stamp from stamped_slots, which reads the rows after"
    ws_src = (root / "ws.py").read_text(encoding="utf-8")
    assert (
        "next_slots_generation" not in ws_src
    ), "the connect path must take its stamp from stamped_slots too"
    assert "stamped_slots(" in ws_src, "the connect path must draw a stamp at all"


def test_the_connect_time_frame_carries_the_pair_the_client_dates_by() -> None:
    """An UNDATED snapshot is never refused, so the connect frame must be stamped.

    Checked at source rather than by driving the handler, which would need a full aiohttp
    app fixture: this frame is built inline in the websocket route rather than through
    ``_slots_ws_frame``. The keys ARE the contract -- ``useWebSocket`` reads
    ``slotsGeneration``/``slotsEpoch`` off this very dict, and a connect frame missing them
    applies unconditionally and can drop a slot a newer GET already carried.
    """
    from pathlib import Path

    ws_src = (
        Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard" / "ws.py"
    ).read_text(encoding="utf-8")
    parts = ws_src.split('"type": "slots"', 1)
    assert len(parts) == 2, "the connect-time slots frame was not found"
    window = parts[1][:1200]
    for key in ('"slotsGeneration"', '"slotsEpoch"'):
        assert key in window, f"the connect-time slots frame omits {key}"


def test_the_generation_is_drawn_under_the_stamp_lock() -> None:
    """``+= 1`` is a read-modify-write and the two emitters are on different threads.

    The event loop serves the GET and the WS connect; the leading-edge broadcast runs on
    whatever foreign thread called ``push_slots_update``. An interleave hands two distinct
    snapshots the SAME number, and the client treats an equal generation as stale, so the
    newer of the two is dropped.

    Asserted through a recording lock rather than by racing threads: a race reproduces the
    old defect only sometimes, so it could not fail reliably for the intended reason.
    """
    import threading

    real = threading.Lock()
    acquisitions = []

    class RecordingLock:
        def __enter__(self) -> object:
            acquisitions.append(1)
            return real.__enter__()

        def __exit__(self, *exc: object) -> object:
            return real.__exit__(*exc)  # type: ignore[arg-type]

    state = DashboardState.__new__(DashboardState)
    state._slots_stamp_lock = RecordingLock()  # type: ignore[assignment]

    assert state.next_slots_generation() == 1
    assert state.next_slots_generation() == 2
    assert acquisitions == [1, 1], (
        "the counter was drawn without taking the stamp lock, so two emitting "
        "threads can hand two snapshots the same generation"
    )


def test_the_generation_still_advances_without_a_lock() -> None:
    """A ``__new__``-built state carries no lock, and the seam must not crash on one.

    Negative control for the test above: it fixes the lock as the mechanism, so this
    pins that the lock is not a REQUIREMENT for drawing a stamp at all.
    """
    state = DashboardState.__new__(DashboardState)
    state._slots_stamp_lock = None
    assert [state.next_slots_generation() for _ in range(3)] == [1, 2, 3]


def test_a_closed_row_cannot_arrive_under_a_higher_generation() -> None:
    """The stamp and the MEMBERSHIP read must be taken together.

    Ordering the counter alone orders the NUMBERS, not the (number, rows) PAIRS. With
    the membership read separated from the draw, two emitters on different threads
    interleave as F.stamp, S.stamp, S.read, close, F.read -- so the frame carrying the
    HIGHER number holds the rows read BEFORE the close. The client applies that one and
    refuses the newer frame that omits the row, and the closed session reappears and
    stays.

    Discriminates on the snapshot, not on lock hold time: the stub serializes the rows
    it was HANDED when there are any, else live membership -- what the pre-fix seam did
    from inside ``serialize_slots``. So the pre-fix shape reads post-close rows under a
    lower number and fails here. The competing call runs on its own thread with a
    BOUNDED wait, so a version that blocks it cannot deadlock this test.
    """
    import itertools
    import threading
    import types

    state = DashboardState.__new__(DashboardState)
    state._slots_broadcast_lock = threading.RLock()
    state._slots_generation = 0
    state._slots = {
        "keep": types.SimpleNamespace(key="keep"),
        "closed": types.SimpleNamespace(key="closed"),
    }

    calls = itertools.count(1)
    foreign_is_reading = threading.Event()
    foreign_may_read = threading.Event()
    competing_read_done = threading.Event()

    def _serialize(**kw: object) -> list:
        if next(calls) == 1:
            # The foreign emitter has its number and is about to render its rows.
            foreign_is_reading.set()
            foreign_may_read.wait(10)
        else:
            competing_read_done.set()
        handed = kw.get("rows")
        seen = state._slots.values() if handed is None else handed
        return [{"key": s.key} for s in seen]

    state.serialize_slots = _serialize  # type: ignore[method-assign]

    out: dict[str, tuple[int, list]] = {}

    def _foreign() -> None:
        out["foreign"] = state.stamped_slots()

    def _competing() -> None:
        out["competing"] = state.stamped_slots()

    f = threading.Thread(target=_foreign)
    f.start()
    assert foreign_is_reading.wait(10), "the foreign emitter never reached its read"

    s = threading.Thread(target=_competing)
    s.start()
    # Bounded: unset is a legitimate outcome for any version that blocks it.
    competing_read_done.wait(0.5)

    del state._slots["closed"]  # the close pops the slot
    foreign_may_read.set()
    f.join(10)
    s.join(10)

    assert set(out) == {"foreign", "competing"}, out
    pairs = sorted(out.values())
    newest_gen, newest_rows = pairs[-1]
    newest_keys = {r["key"] for r in newest_rows}
    for gen, older_rows in pairs[:-1]:
        older_keys = {r["key"] for r in older_rows}
        assert older_keys >= newest_keys, (
            f"generation {newest_gen} carries {sorted(newest_keys - older_keys)}, which "
            f"generation {gen} already omits: the only mutation was a close, so a HIGHER "
            f"number must never carry a row a LOWER one dropped -- the client would apply "
            f"the closed row and refuse the frame without it"
        )


def test_the_stamp_pins_membership_without_serializing_under_the_lock() -> None:
    """The lock must NOT still be held while the rows are rendered.

    Holding it through ``serialize_slots`` puts the event loop -- which serves the GET
    and the WS connect -- behind a foreign broadcast thread mid-serialize, which the
    repo's no-blocking-call-on-event-loop anchor forbids.
    Asserted by trying to take the lock FROM ANOTHER THREAD while serialization is in
    flight: it must succeed.
    """
    import threading
    import types

    state = DashboardState.__new__(DashboardState)
    state._slots_broadcast_lock = threading.RLock()
    state._slots_generation = 0
    state._slots = {"keep": types.SimpleNamespace(key="keep")}

    lock_was_free: list[bool] = []

    def _serialize(**kw: object) -> list:
        # A DIFFERENT thread, so the RLock cannot be re-entered by this one.
        def _probe() -> None:
            got = state._slots_broadcast_lock.acquire(timeout=2.0)
            lock_was_free.append(got)
            if got:
                state._slots_broadcast_lock.release()

        probe = threading.Thread(target=_probe)
        probe.start()
        probe.join(5)
        handed = kw.get("rows")
        return [{"key": s.key} for s in (state._slots.values() if handed is None else handed)]

    state.serialize_slots = _serialize  # type: ignore[method-assign]

    generation, rows = state.stamped_slots()
    assert generation == 1
    assert rows == [{"key": "keep"}]
    assert lock_was_free == [True], (
        "the broadcast lock was still held while the rows were serialized, so a GET or "
        "WS connect on the event loop can stall behind a foreign broadcast thread"
    )


class _Row:
    """A slot stand-in carrying only what serialization reads here."""

    def __init__(self, key: str) -> None:
        self.key = key


def test_every_audience_frame_is_serialized_from_one_captured_snapshot() -> None:
    """One generation must not label two DIFFERENT membership snapshots.

    ``stamped_slots`` pairs the stamp with the membership read, but a broadcast emits
    THREE audience variants -- the bare list, the dashboard-user list and the owner list --
    and each one that re-reads live membership is still labelled with the FIRST one's
    generation. So a slot created after the stamp rides out under the older number, a
    concurrent GET draws a HIGHER number without it, the client ranks the GET newer and
    evicts a live session's cached state.

    Drives the real ``_do_slots_broadcast`` and mutates membership between audience
    serializations, which is what the interleave does. Asserts every variant was handed
    the SAME captured rows -- not merely that the frames agree, since two live re-reads
    can happen to agree when nothing races them.
    """
    import json
    from unittest.mock import MagicMock

    live = {"keep": _Row("keep")}
    seen: list[object] = []

    def _serialize(**kw: object) -> list:
        # A concurrent create lands between the audience serializations.
        live["created-after-stamp"] = _Row("created-after-stamp")
        handed = kw.get("rows")
        seen.append(None if handed is None else tuple(r.key for r in handed))
        src = live.values() if handed is None else handed
        return [{"key": r.key} for r in src]

    state = MagicMock()
    state._slots = live
    state._folders = None
    state.channel_manager = None
    state._owner_ws_clients = ["owner-socket"]
    state.is_yolo_active.return_value = False
    state.folders_generation.return_value = 1
    state.serialize_slots.side_effect = _serialize
    # Both seams are doubled, so this test drives the pre-fix shape too and its failure
    # is about the audience variants rather than a missing attribute.
    captured = tuple(live.values())
    state.stamped_slot_rows.side_effect = lambda: (1, captured)
    state.stamped_slots.side_effect = lambda **kw: (1, state.serialize_slots(rows=captured, **kw))

    frames: list[dict] = []
    state._send_ws_all.side_effect = lambda payload, **_kw: frames.append(payload)
    state._send_ws_owners.side_effect = lambda payload, **_kw: frames.append(payload)

    DashboardState._do_slots_broadcast(state)

    assert seen, "no audience was serialized"
    assert None not in seen, (
        f"an audience re-read LIVE membership instead of the captured rows: {seen} -- "
        "one generation would then label two different snapshots"
    )
    assert len(set(seen)) == 1, f"audiences disagree about membership: {seen}"
    assert all(
        "created-after-stamp" not in rows for rows in seen
    ), f"a slot created after the stamp rode out under that stamp: {seen}"
    # Positive control: the frames really carry a slots list, so the assertions above
    # were not vacuously true of an empty broadcast.
    assert frames, "no frame was sent"
    for payload in frames:
        json.dumps(payload)
