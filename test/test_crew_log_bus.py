"""The crew-log bus: disposers, keyed subscriptions, and a late joiner's baseline.

The eager folder's own publishing is pinned in ``test_crew_log_fold_eager.py`` and
``test_crew_log_session_eager.py``. This file pins the registry those publish into: what a
subscriber gets back, which events reach it, and how a subscriber that joins after a fold
already advanced starts from the current value without reading the store itself.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, bus, eager, emit
from kiro_crew.crew_log import projection as crew_log

GATEWAY = "gateway"
SLOT = "dashboard:bus-1"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Own data home, no warm cell and no subscriber left over from another case."""
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


def _event(key: str = SLOT, fold: str = "work", revision: int = 1, scope: str = "slot"):
    return bus.FoldAdvanced(
        scope=scope, key=key, fold=fold, revision=revision, value={"r": revision}, seq=revision
    )


# --------------------------------------------------------------------------- #
# Disposer
# --------------------------------------------------------------------------- #


def test_the_disposer_stops_delivery_and_is_idempotent():
    seen: list[Any] = []
    dispose = bus.subscribe("k", seen.append)
    bus.publish("k", 1)
    dispose()
    dispose()
    bus.publish("k", 2)
    assert seen == [1]
    assert bus.subscriber_count("k") == 0


def test_a_subscriber_can_dispose_itself_from_inside_its_callback():
    """MUTATION-SENSITIVE: the registry lock must not be held while a callback runs."""
    seen: list[Any] = []
    holder: dict[str, Any] = {}

    def once(event: Any) -> None:
        seen.append(event)
        holder["dispose"]()

    holder["dispose"] = bus.subscribe("k", once)
    bus.publish("k", 1)
    bus.publish("k", 2)
    assert seen == [1]


def test_a_subscriber_disposed_mid_publish_is_not_called_by_that_publish():
    """A sibling earlier in line disposes a later one; the later one must stay silent."""
    later: list[Any] = []
    holder: dict[str, Any] = {}
    bus.subscribe("k", lambda _event: holder["later"]())
    holder["later"] = bus.subscribe("k", later.append)
    bus.publish("k", 1)
    assert later == []


def test_disposing_one_of_two_identical_callbacks_keeps_the_other():
    seen: list[Any] = []
    first = bus.subscribe("k", seen.append)
    bus.subscribe("k", seen.append)
    first()
    bus.publish("k", 1)
    assert seen == [1]


# --------------------------------------------------------------------------- #
# Keyed subscriptions
# --------------------------------------------------------------------------- #


def test_filters_deliver_only_matching_events_and_unfiltered_sees_all():
    everything: list[Any] = []
    board: list[Any] = []
    work_on_board: list[Any] = []
    sessions: list[Any] = []
    bus.subscribe(bus.FOLD_ADVANCED, everything.append)
    bus.subscribe(bus.FOLD_ADVANCED, board.append, key=SLOT)
    bus.subscribe(bus.FOLD_ADVANCED, work_on_board.append, key=SLOT, fold="work")
    bus.subscribe(bus.FOLD_ADVANCED, sessions.append, scope=bus.SCOPE_SESSION)

    a = _event(fold="work")
    b = _event(fold="radar")
    c = _event(key="dashboard:other")
    d = _event(key="s-1", fold="status", scope=bus.SCOPE_SESSION)
    for event in (a, b, c, d):
        bus.publish(bus.FOLD_ADVANCED, event)

    assert everything == [a, b, c, d]
    assert board == [a, b]
    assert work_on_board == [a]
    assert sessions == [d]


def test_a_publish_reaches_matching_subscribers_once_each_in_registration_order():
    order: list[str] = []
    bus.subscribe(bus.FOLD_ADVANCED, lambda _e: order.append("keyed"), key=SLOT, fold="work")
    bus.subscribe(bus.FOLD_ADVANCED, lambda _e: order.append("all"))
    bus.subscribe(bus.FOLD_ADVANCED, lambda _e: order.append("fold"), fold="work")
    bus.publish(bus.FOLD_ADVANCED, _event())
    assert order == ["keyed", "all", "fold"]


def test_a_publish_does_not_walk_subscribers_whose_filter_cannot_match(monkeypatch):
    """MUTATION-SENSITIVE: the index, not a per-subscriber predicate, picks the listeners.

    A thousand boards each with a keyed subscriber: publishing about one must hand
    exactly one subscriber to the delivery loop.
    """
    for index in range(1000):
        bus.subscribe(bus.FOLD_ADVANCED, lambda _e: None, key=f"dashboard:{index}")
    delivered: list[Any] = []
    real = bus._Subscriber.deliver

    def counting(self, event):
        delivered.append(self)
        return real(self, event)

    monkeypatch.setattr(bus._Subscriber, "deliver", counting)
    bus.publish(bus.FOLD_ADVANCED, _event(key="dashboard:7"))
    assert len(delivered) == 1


def test_an_event_without_fold_attributes_reaches_only_unfiltered_subscribers():
    plain: list[Any] = []
    keyed: list[Any] = []
    bus.subscribe("k", plain.append)
    bus.subscribe("k", keyed.append, key="x")
    bus.publish("k", {"not": "a fold event"})
    assert plain == [{"not": "a fold event"}]
    assert keyed == []


def test_an_event_with_unhashable_attributes_does_not_break_the_publish():
    class Odd:
        scope = ["slot"]
        key = {"a": 1}
        fold = None

    plain: list[Any] = []
    bus.subscribe("k", plain.append)
    bus.subscribe("k", lambda _e: None, key="x")
    odd = Odd()
    bus.publish("k", odd)
    assert plain == [odd]


# --------------------------------------------------------------------------- #
# Baseline
# --------------------------------------------------------------------------- #


def test_a_baseline_needs_a_full_key():
    with pytest.raises(ValueError):
        bus.subscribe(bus.FOLD_ADVANCED, lambda _e: None, key=SLOT, baseline=True)
    with pytest.raises(ValueError):
        bus.subscribe("k", lambda _e: None, scope="slot", key=SLOT, fold="w", baseline=True)
    with pytest.raises(ValueError):
        bus.subscribe(
            bus.FOLD_ADVANCED, lambda _e: None, scope="x", key=SLOT, fold="w", baseline=True
        )
    assert bus.subscriber_count(bus.FOLD_ADVANCED) == 0


def test_events_pushed_during_the_baseline_read_are_held_then_filtered_by_revision(
    monkeypatch,
):
    """MUTATION-SENSITIVE: subscribe-then-read closes the join race.

    While the baseline read is running, the folder publishes revisions 4 and 6. The read
    answers revision 5. The subscriber must see 5 (the baseline), then 6, and never 4:
    4 is older than what it already holds, and 6 would be lost if the subscription were
    registered only after the read.
    """

    def racing_read(scope, key, fold):
        bus.publish(bus.FOLD_ADVANCED, _event(revision=4))
        bus.publish(bus.FOLD_ADVANCED, _event(revision=6))
        return _event(revision=5)

    monkeypatch.setattr(bus, "_read_baseline", racing_read)
    seen: list[int] = []
    bus.subscribe(
        bus.FOLD_ADVANCED,
        lambda e: seen.append(e.revision),
        scope=bus.SCOPE_SLOT,
        key=SLOT,
        fold="work",
        baseline=True,
    )
    for revision in (6, 3, 7):
        bus.publish(bus.FOLD_ADVANCED, _event(revision=revision))
    assert seen == [5, 6, 7]


def test_a_baseline_read_that_fails_removes_the_subscription(monkeypatch):
    def failing(scope, key, fold):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(bus, "_read_baseline", failing)
    with pytest.raises(RuntimeError):
        bus.subscribe(
            bus.FOLD_ADVANCED, lambda _e: None, scope="slot", key=SLOT, fold="work", baseline=True
        )
    assert bus.subscriber_count(bus.FOLD_ADVANCED) == 0


def test_a_live_publish_during_the_baseline_release_waits_and_stays_ordered(monkeypatch):
    """A publish on another thread while held events replay is serialized behind them."""
    replaying = threading.Event()
    proceed = threading.Event()
    seen: list[int] = []

    def slow_callback(event):
        seen.append(event.revision)
        if event.revision == 2:
            replaying.set()
            proceed.wait(timeout=5)

    def read(scope, key, fold):
        bus.publish(bus.FOLD_ADVANCED, _event(revision=2))
        return _event(revision=1)

    monkeypatch.setattr(bus, "_read_baseline", read)
    joiner = threading.Thread(
        target=bus.subscribe,
        args=(bus.FOLD_ADVANCED, slow_callback),
        kwargs={"scope": "slot", "key": SLOT, "fold": "work", "baseline": True},
    )
    joiner.start()
    assert replaying.wait(timeout=5)
    publisher = threading.Thread(target=bus.publish, args=(bus.FOLD_ADVANCED, _event(revision=3)))
    publisher.start()
    proceed.set()
    joiner.join(timeout=5)
    publisher.join(timeout=5)
    assert seen == [1, 2, 3]


def test_a_real_slot_baseline_is_the_current_board_with_its_revision():
    """End to end through the projection read path: a late joiner sees the board as is."""
    handle = CrewLog.create(
        lg.KIND_SESSION, "s-bus-1", owner="raymond", agent="kirocrew", slot=SLOT
    )
    entry = handle.append(
        "work/recorded",
        {
            "slot": SLOT,
            "actor": "conductor",
            "by": SLOT,
            "action": "create",
            "item_id": "it-baseline",
            "title": "a work item",
        },
        src=GATEWAY,
    )
    eager.note_commit("s-bus-1", "work/recorded", int(entry.seq), board=SLOT)
    assert eager.drain(timeout=10.0)
    current = crew_log.read_slot_projection(SLOT, "work")
    assert current.revision > 0

    seen: list[Any] = []
    bus.subscribe(
        bus.FOLD_ADVANCED, seen.append, scope="slot", key=SLOT, fold="work", baseline=True
    )
    assert len(seen) == 1
    baseline = seen[0]
    assert (baseline.scope, baseline.key, baseline.fold) == ("slot", SLOT, "work")
    assert baseline.revision == current.revision
    assert [item["item_id"] for item in baseline.value["items"]] == ["it-baseline"]

    # A re-publish of the same revision (the folder's next pass over an unmoved cell)
    # is dropped; a newer one lands.
    bus.publish(bus.FOLD_ADVANCED, baseline)
    bus.publish(bus.FOLD_ADVANCED, baseline._replace(revision=baseline.revision + 1))
    assert [event.revision for event in seen] == [baseline.revision, baseline.revision + 1]


def test_a_real_session_baseline_carries_the_warm_revision():
    handle = CrewLog.create(lg.KIND_SESSION, "s-bus-2", owner="raymond", agent="kirocrew")
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
    current = crew_log.read_projection("s-bus-2", "status")
    assert current.revision > 0
    seen: list[Any] = []
    bus.subscribe(
        bus.FOLD_ADVANCED,
        seen.append,
        scope=bus.SCOPE_SESSION,
        key="s-bus-2",
        fold="status",
        baseline=True,
    )
    assert [event.revision for event in seen] == [current.revision]


def test_a_fold_with_no_revision_yet_delivers_no_baseline(monkeypatch):
    monkeypatch.setattr(bus, "_read_baseline", lambda scope, key, fold: None)
    seen: list[int] = []
    bus.subscribe(
        bus.FOLD_ADVANCED,
        lambda e: seen.append(e.revision),
        scope="slot",
        key=SLOT,
        fold="work",
        baseline=True,
    )
    bus.publish(bus.FOLD_ADVANCED, _event(revision=1))
    assert seen == [1]
