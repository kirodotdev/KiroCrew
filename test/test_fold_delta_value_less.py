"""A ``work`` publish carries no whole value, and nobody is worse off.

The bus event for a ``work`` advance is a delta or a ``snapshot_needed`` with
``value=None``. What each receiver does with that, on the real publish path:

- The WS exporter still builds an old tab's whole-value ``slot_projection`` frame, from
  the render the publisher recorded as this publish's base (``eager.published_value``,
  looked up inside the publish), and the hub serializes that frame only when a socket
  that reads it is a target.
- The bus baseline, ``conductor_wake`` and ``DashboardFeed`` apply the delta on what
  they hold, or read the board on a gap or a ``snapshot_needed``.

The tab-level end-to-end (real ``/api/ws`` for a ``fold_delta.2``, a ``fold_delta.1``
and a no-cap tab) is ``test_fold_delta_receivers_e2e.py``, which runs on these
value-less events unchanged.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from crew_log_fold_eager_helpers import (  # noqa: F401 - autouse fixture
    WORK_SLOT,
    _isolated_home,
)
from test_crew_log_fold_delta import _publish_now, _Writer

from kiro_crew import conductor_wake
from kiro_crew.crew_log import bus, eager, fold_delta
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard.handlers.crew_log import CrewLogPublisher
from kiro_crew.testing.wait import wait_until


@pytest.fixture(autouse=True)
def _fresh_bus():
    bus.reset_for_tests()
    eager.reset_published_for_tests()
    conductor_wake.dispose_all()
    yield
    bus.reset_for_tests()
    eager.reset_published_for_tests()
    conductor_wake.dispose_all()


def _raw() -> "list[bus.FoldAdvanced]":
    got: "list[bus.FoldAdvanced]" = []
    bus.subscribe(bus.FOLD_ADVANCED, got.append, scope="slot", key=WORK_SLOT, fold="work")
    return got


def _read() -> "dict[str, Any]":
    return dict(crew_log.read_slot_projection(WORK_SLOT, "work").value)


# --------------------------------------------------------------------------- #
# 1. the publisher
# --------------------------------------------------------------------------- #


def test_every_work_publish_is_value_less_and_other_folds_keep_their_value():
    raw = _raw()
    plain: "list[bus.FoldAdvanced]" = []
    bus.subscribe(bus.FOLD_ADVANCED, plain.append, scope="slot", key=WORK_SLOT, fold="ledger")
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    writer.report("it_a", "progress", "p")
    _publish_now()
    folded = crew_log.read_slot_projection(WORK_SLOT, "work")
    eager._publish_fold(WORK_SLOT, "work", folded)  # a repeated revision
    assert [e.kind for e in raw] == [
        fold_delta.KIND_SNAPSHOT_NEEDED,
        fold_delta.KIND_DELTA,
        fold_delta.KIND_SNAPSHOT_NEEDED,
    ]
    assert all(e.value is None for e in raw)

    class _Ledger:
        revision = 4
        value = {"rows": [1]}
        seq = 2

    eager._publish_fold(WORK_SLOT, "ledger", _Ledger())
    assert plain and plain[0].value == {"rows": [1]} and plain[0].kind == fold_delta.KIND_VALUE


def test_published_value_is_only_ever_the_render_of_that_revision():
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    first = eager.published_revision(WORK_SLOT, "work")
    assert eager.published_value(WORK_SLOT, "work", first) == _read()
    writer.create("it_b")
    _publish_now()
    second = eager.published_revision(WORK_SLOT, "work")
    assert second > first
    assert eager.published_value(WORK_SLOT, "work", first) is None
    assert eager.published_value(WORK_SLOT, "work", second) == _read()
    assert eager.published_value("dashboard:nobody", "work", second) is None


# --------------------------------------------------------------------------- #
# 2. the exporter: an old tab still gets the whole value
# --------------------------------------------------------------------------- #


class _Hub:
    """Records what the exporter hands the hub: ``(legacy, delta)`` per board."""

    def __init__(self) -> None:
        self.calls: "list[tuple[str, dict[str, Any], dict[str, Any] | None]]" = []

    def broadcast_slot_fold(self, slot: str, legacy: Any, delta: Any) -> None:
        self.calls.append((slot, legacy, delta))

    def broadcast_ws_owners(self, kind: str, data: Any) -> None:  # pragma: no cover
        raise AssertionError("a work delta goes through broadcast_slot_fold")


def _exporter(hub: _Hub) -> "tuple[Any, list[Any]]":
    """The real ``on_slot_fold`` on a stand-in loop that runs each hop at once."""
    pending: "list[Any]" = []
    loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: pending.append((fn, a)))
    exporter = SimpleNamespace(
        _state=hub,
        _loop=loop,
        _record_revision=lambda *_a: None,
        _unpack=CrewLogPublisher._unpack,
        _unpack_delta=CrewLogPublisher._unpack_delta,
        _delta_frame=CrewLogPublisher._delta_frame,
    )
    exporter._push_fold = lambda *a: CrewLogPublisher._push_fold(exporter, *a)
    bus.subscribe(
        bus.FOLD_ADVANCED,
        lambda event: CrewLogPublisher.on_slot_fold(exporter, event),
        scope="slot",
        key=WORK_SLOT,
        fold="work",
    )
    return exporter, pending


def test_the_exporter_builds_the_whole_old_tab_frame_from_the_publish_itself():
    hub = _Hub()
    _exporter_, pending = _exporter(hub)
    writer = _Writer()
    reads: "list[dict[str, Any]]" = []
    for step in range(3):
        writer.create(f"it_{step}")
        _publish_now()
        reads.append(_read())
    # The loop runs late, after every publish: each frame still holds ITS revision's
    # render, because the lookup happened inside that publish.
    for fn, args in pending:
        fn(*args)
    assert len(hub.calls) == 3
    for (slot, legacy, delta), read in zip(hub.calls, reads):
        assert slot == WORK_SLOT
        assert legacy["value"] == read, "an old tab gets the whole value"
        assert set(legacy) == {"slot", "fold", "revision", "value"}, "old frame shape"
        assert delta is not None and delta["revision"] == legacy["revision"]
    assert [d["kind"] for _s, _l, d in hub.calls] == ["snapshot_needed", "delta", "delta"]


def test_a_publish_with_no_recorded_render_goes_to_an_old_tab_bare():
    hub = _Hub()
    _exporter(hub)
    writer = _Writer()
    writer.create("it_a")
    older = crew_log.read_slot_projection(WORK_SLOT, "work")
    eager._publish_fold(WORK_SLOT, "work", older)
    writer.create("it_b")
    _publish_now()
    # The older render published again after a newer base was recorded.
    raw = _raw()
    eager._publish_fold(WORK_SLOT, "work", older)
    assert raw[-1].kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert raw[-1].delta == {"reason": fold_delta.REASON_REVISION}
    # The exporter itself was handed the event synchronously; its hop is pending.
    unpacked, frame = CrewLogPublisher._unpack_delta(raw[-1])
    assert unpacked is not None and unpacked[4] is None
    assert frame is not None and frame["kind"] == "snapshot_needed"
    sent = _Hub()
    CrewLogPublisher._push_fold(SimpleNamespace(_state=sent), *unpacked[:3], None, frame)
    assert sent.calls[0][1] == {"slot": WORK_SLOT, "fold": "work", "revision": older.revision}


# --------------------------------------------------------------------------- #
# 3. the hub: the whole frame is serialized only for a socket that reads it
# --------------------------------------------------------------------------- #


def _counting_dumps(monkeypatch) -> "list[str]":
    from kiro_crew.dashboard import websocket_hub as hub

    kinds: "list[str]" = []
    real = json.dumps

    def _dumps(obj: Any, *a: Any, **k: Any) -> str:
        if isinstance(obj, dict) and isinstance(obj.get("type"), str):
            kinds.append(obj["type"])
        return real(obj, *a, **k)

    monkeypatch.setattr(hub.json, "dumps", _dumps)
    return kinds


def _drain(state: Any, slot: str, legacy: Any, delta: Any) -> None:
    async def _run() -> None:
        state.broadcast_slot_fold(slot, legacy, delta)
        for _ in range(6):
            await asyncio.sleep(0)

    asyncio.run(_run())


def _socket(state: Any, *, caps: "tuple[bool, bool]", watch: "list[str] | None") -> Any:
    from test_fold_watch_routing import _FakeWS

    ws = _FakeWS()
    state.register_ws(ws, owner=True)
    state.set_fold_delta_capable(ws, caps[0])
    state.set_fold_apply_capable(ws, caps[1])
    if watch is not None:
        state.set_fold_watch(ws, watch)
    return ws


LEGACY = {"slot": "A", "fold": "work", "revision": 7, "value": {"items": [{"item_id": "x"}]}}
DELTA = {"v": 1, "slot": "A", "fold": "work", "kind": "delta", "revision": 7, "base_revision": 6}


def test_no_whole_frame_is_serialized_when_every_watcher_applies_deltas(tmp_path, monkeypatch):
    from test_fold_watch_routing import _make_state

    from kiro_crew.dashboard import websocket_hub as hub

    state = _make_state(tmp_path)
    applies = _socket(state, caps=(True, True), watch=["A"])
    other = _socket(state, caps=(True, False), watch=["B"])  # fold_delta.1, another board
    kinds = _counting_dumps(monkeypatch)
    _drain(state, "A", LEGACY, DELTA)
    assert applies.frames(hub.SLOT_DELTA_FRAME) == [DELTA]
    assert other.sent == []
    assert kinds == [hub.SLOT_DELTA_FRAME], "the whole work frame was serialized for no one"


def test_old_and_watch_only_tabs_get_the_whole_frame_serialized_once(tmp_path, monkeypatch):
    from test_fold_watch_routing import _make_state

    from kiro_crew.dashboard import websocket_hub as hub

    state = _make_state(tmp_path)
    applies = _socket(state, caps=(True, True), watch=["A"])
    watch_only = _socket(state, caps=(True, False), watch=["A"])
    no_cap = _socket(state, caps=(False, False), watch=None)
    kinds = _counting_dumps(monkeypatch)
    _drain(state, "A", LEGACY, DELTA)
    assert applies.frames(hub.SLOT_DELTA_FRAME) == [DELTA]
    assert applies.frames(hub.SLOT_FOLD_FRAME) == []
    assert watch_only.frames(hub.SLOT_FOLD_FRAME) == [LEGACY]
    assert no_cap.frames(hub.SLOT_FOLD_FRAME) == [LEGACY]
    assert sorted(kinds) == sorted([hub.SLOT_DELTA_FRAME, hub.SLOT_FOLD_FRAME])


def test_a_watch_only_tab_on_another_board_costs_no_serialization(tmp_path, monkeypatch):
    """No ``fold_delta.2`` socket open: the ``broadcast_ws_owners`` branch is lazy too."""
    from test_fold_watch_routing import _make_state

    state = _make_state(tmp_path)
    elsewhere = _socket(state, caps=(True, False), watch=["B"])
    kinds = _counting_dumps(monkeypatch)
    _drain(state, "A", LEGACY, DELTA)
    assert elsewhere.sent == [] and kinds == []


# --------------------------------------------------------------------------- #
# 4. in-process receivers recover by read on value-less events
# --------------------------------------------------------------------------- #


def test_a_baseline_subscriber_on_a_gap_gets_a_value_less_snapshot_needed():
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    stale = eager._published[(WORK_SLOT, "work")]
    writer.create("it_b")
    _publish_now()
    got: "list[bus.FoldAdvanced]" = []
    bus.subscribe(
        bus.FOLD_ADVANCED, got.append, scope="slot", key=WORK_SLOT, fold="work", baseline=True
    )
    assert got[0].kind == fold_delta.KIND_VALUE and got[0].value == _read()
    assert got[0].revision > stale[0]
    # The publisher's base goes back to an older one (as if this subscriber's baseline
    # came from a publish the next delta does not chain on): a gap for the subscriber.
    eager._published[(WORK_SLOT, "work")] = stale
    writer.create("it_c")
    _publish_now()
    last = got[-1]
    assert last.kind == fold_delta.KIND_SNAPSHOT_NEEDED and last.value is None
    assert last.delta == {"reason": fold_delta.REASON_GAP}
    assert fold_delta.value_after(got[0].value, got[0].revision, last) is None


def test_conductor_wake_reads_the_board_on_a_value_less_snapshot(monkeypatch):
    writer = _Writer()
    raw = _raw()
    writer.create("it_a")
    _publish_now()
    first = raw[-1]
    assert first.kind == fold_delta.KIND_SNAPSHOT_NEEDED and first.value is None
    assert conductor_wake._event_news(first) == (None, None)
    monkeypatch.setattr(conductor_wake, "work_ledger_loop_id", lambda _svc, _board: "loop")
    asyncio.run(conductor_wake._observe_board(object(), WORK_SLOT, None))
    assert set(conductor_wake._registry.boards[WORK_SLOT]) == {"it_a"}
    writer.create("it_b")
    _publish_now()
    items, order = conductor_wake._event_news(raw[-1])
    assert items is not None and order == ["it_a", "it_b"]


def test_dashboard_feed_takes_the_published_render_on_a_value_less_snapshot_then_applies():
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    changes: "list[str]" = []
    feed = DashboardFeed(WORK_SLOT, on_change=changes.append)
    feed._cells["work"] = _Cell()
    rereads: "list[str]" = []
    real_soon = feed._resync_soon

    def _soon(fold: str) -> None:
        rereads.append(fold)
        real_soon(fold)

    feed._resync_soon = _soon  # type: ignore[method-assign]
    bus.subscribe(
        bus.FOLD_ADVANCED,
        feed._make_callback("work"),
        scope="slot",
        key=WORK_SLOT,
        fold="work",
        baseline=True,
    )
    writer = _Writer()
    writer.create("it_a")
    _publish_now()  # snapshot_needed, no value: the publisher's render for it
    assert feed.fold_value("work") == _read()
    assert feed._cells["work"].revision == eager.published_revision(WORK_SLOT, "work")
    writer.report("it_a", "progress", "p")
    _publish_now()  # a delta on that revision
    assert feed.fold_value("work") == _read()
    assert rereads == [], "nothing was re-read: the render and the delta were enough"
    assert changes and set(changes) == {"work"}


def test_a_one_shot_read_waits_for_the_feeds_re_read(monkeypatch):
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    feed = DashboardFeed(WORK_SLOT)
    feed._cells["work"] = _Cell()
    feed._folds = frozenset({"work"})
    entered = threading.Event()
    release = threading.Event()

    def _slow(_fold: str) -> None:
        entered.set()
        release.wait(5)

    monkeypatch.setattr(feed, "_resync_once", _slow)
    feed._resync_soon("work")
    assert entered.wait(5)
    waiter = threading.Thread(target=feed.wait_for_primes, kwargs={"timeout": 5.0})
    waiter.start()
    waiter.join(timeout=0.2)  # flake-ok: a correct wait stays blocked until release below
    assert waiter.is_alive(), "the one-shot read returned before the re-read"
    release.set()
    waiter.join(timeout=5)
    assert not waiter.is_alive()


def test_a_one_shot_read_gives_up_on_a_re_read_at_its_deadline(monkeypatch):
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    feed = DashboardFeed(WORK_SLOT)
    feed._cells["work"] = _Cell()
    feed._folds = frozenset({"work"})
    release = threading.Event()
    monkeypatch.setattr(feed, "_resync_once", lambda _fold: release.wait(5))
    feed._resync_soon("work")
    feed.wait_for_primes(timeout=0.05)
    assert "work" in feed._resyncing
    release.set()
    wait_until(lambda: not feed._resyncing, describe=lambda: "the re-read finished")


def test_a_board_too_big_to_keep_warm_still_reaches_the_feed(monkeypatch):
    """A board above the warm-cache budget with no savepoint: a warm read refuses it.
    The value-less ``snapshot_needed`` still gives the feed its value, from the render
    the publisher recorded for that revision, and a one-shot read sees it."""
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    writer = _Writer()
    writer.create("it_a")
    folded = crew_log.read_slot_projection(WORK_SLOT, "work")
    real = crew_log.read_slot_projection

    def _too_big(slot: str, name: str, *a: Any, **k: Any) -> Any:
        if k.get("cold") is False:
            raise crew_log.SlotFoldNotReady(slot)
        return real(slot, name, *a, **k)

    monkeypatch.setattr(crew_log, "read_slot_projection", _too_big)
    feed = DashboardFeed(WORK_SLOT)
    feed._cells["work"] = _Cell()
    feed._folds = frozenset({"work"})
    feed._disposers.append(
        bus.subscribe(
            bus.FOLD_ADVANCED, feed._make_callback("work"), scope="slot", key=WORK_SLOT, fold="work"
        )
    )
    eager._publish_fold(WORK_SLOT, "work", folded)
    feed.wait_for_primes(timeout=2.0)
    assert feed._cells["work"].seen, "the feed stayed unseen on a board too big to keep"
    assert feed.fold_value("work") == dict(folded.value)
    assert feed._cells["work"].revision == folded.revision


def test_a_replayed_event_and_a_publish_never_deadlock_on_the_two_locks(monkeypatch):
    """Opposite lock order: a publish holds ``eager._published_lock`` and waits on a
    subscriber's delivery guard, while ``_release_baseline`` holds that guard and
    replays a value-less work event into ``DashboardFeed._accept``, which looks up the
    published render. The lookup must not wait on the publisher's lock."""
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    feed = DashboardFeed(WORK_SLOT)
    feed._cells["work"] = _Cell()
    rereads: "list[str]" = []
    monkeypatch.setattr(feed, "_resync_soon", rereads.append)
    publisher_locked = threading.Event()
    in_guard = threading.Event()
    accept = feed._make_callback("work")

    def _callback(event: Any) -> None:
        if event.kind == fold_delta.KIND_VALUE:
            # The baseline: hold the guard until the publisher holds its lock.
            in_guard.set()
            assert publisher_locked.wait(5)
        accept(event)

    sub = bus._Subscriber(_callback, 1, bus.FOLD_ADVANCED, ("slot", WORK_SLOT, "work"))
    sub.floor = 0
    snapshot = bus.FoldAdvanced(
        "slot", WORK_SLOT, "work", 9, None, 1, kind=fold_delta.KIND_SNAPSHOT_NEEDED
    )
    sub.pending = [snapshot]
    baseline = bus.FoldAdvanced("slot", WORK_SLOT, "work", 5, {"items": []}, 1)
    got_guard: "list[bool]" = []

    def _publisher() -> None:
        with eager._published_lock:
            assert in_guard.wait(5)
            publisher_locked.set()
            got_guard.append(sub.guard.acquire(timeout=2.0))
            if got_guard[-1]:
                sub.guard.release()

    publisher = threading.Thread(target=_publisher, daemon=True)
    replay = threading.Thread(target=bus._release_baseline, args=(sub, baseline), daemon=True)
    publisher.start()
    replay.start()
    replay.join(timeout=5.0)
    publisher.join(timeout=5.0)
    assert got_guard == [True], "the publish could not get the guard: the locks deadlocked"
    assert not replay.is_alive()
    assert rereads == ["work"], "a busy lock falls through to a re-read"
