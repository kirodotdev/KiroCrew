"""The eager folder skips a slot fold no bus subscriber wants, but only on demand.

Three properties, each the reason one piece exists.

OFF BY DEFAULT. A process with no dashboard, or a gateway before its exporter is
installed, folds every woken slot fold exactly as before, subscriber or not
(:func:`test_with_on_demand_off_a_fold_with_no_subscriber_still_advances`).

ON DEMAND, a cell nobody subscribes to is not read or rendered
(:func:`test_on_demand_skips_a_fold_with_no_matching_subscriber`), while one a keyed
reader wants -- ``conductor_wake``'s board filter, a ``DashboardFeed`` -- still is,
baseline and all.

A WILDCARD subscriber matches every cell, so while the exporter holds one nothing is
skipped (:func:`test_a_wildcard_subscriber_means_nothing_is_skipped`).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from crew_log_fold_eager_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    WORK_SLOT,
    _isolated_home,
    _memo_keys,
    _slot_log,
    _work_entry,
)

from kiro_crew.crew_log import bus, eager
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.dashboard_feed import DashboardFeed


@pytest.fixture(autouse=True)
def _clean_bus():
    bus.reset_for_tests()
    yield
    bus.reset_for_tests()


def _wake_work(unit: str, item: str) -> None:
    handle = _slot_log(unit, WORK_SLOT)
    seq = _work_entry(handle, item)
    eager.note_commit(unit, "work/recorded", seq, board=WORK_SLOT)
    assert eager.drain(timeout=10.0)


def _work_events(seen: list) -> list:
    return [e for e in seen if e.key == WORK_SLOT and e.fold == "work"]


def test_has_subscriber_matches_keyed_and_wildcard_filters():
    assert not bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key="a", fold="work")
    dispose = bus.subscribe(bus.FOLD_ADVANCED, lambda _e: None, scope="slot", key="a", fold="work")
    assert bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key="a", fold="work")
    assert not bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key="b", fold="work")
    assert not bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key="a", fold="radar")
    dispose()
    assert not bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key="a", fold="work")
    any_slot = bus.subscribe(bus.FOLD_ADVANCED, lambda _e: None, scope="slot")
    assert bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key="b", fold="radar")
    assert not bus.has_subscriber(bus.FOLD_ADVANCED, scope="session", key="b", fold="status")
    any_slot()


def test_with_on_demand_off_a_fold_with_no_subscriber_still_advances():
    assert not bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key=WORK_SLOT, fold="work")
    _wake_work("s-od-off", "it-1")
    assert (WORK_SLOT, "work") in _memo_keys(), "today's behaviour: eager folds with no reader"


def test_on_demand_skips_a_fold_with_no_matching_subscriber():
    bus.set_slot_folds_on_demand(True)
    other: list = []
    bus.subscribe(bus.FOLD_ADVANCED, other.append, scope="slot", key="dashboard:other", fold="work")
    _wake_work("s-od-skip", "it-1")
    assert (WORK_SLOT, "work") not in _memo_keys(), "a cell nobody wants was read anyway"
    assert not other


def test_on_demand_still_folds_for_the_conductor_wake_filter():
    """``conductor_wake._join``'s exact filter: keyed board, ``work``, with a baseline."""
    bus.set_slot_folds_on_demand(True)
    seen: list = []
    bus.subscribe(
        bus.FOLD_ADVANCED, seen.append, scope="slot", key=WORK_SLOT, fold="work", baseline=True
    )
    _wake_work("s-od-cw", "it-1")
    assert (WORK_SLOT, "work") in _memo_keys()
    events = _work_events(seen)
    # A work event carries no whole value (a delta, or snapshot_needed): the board the
    # receiver reads on it holds the item.
    assert events and events[-1].value is None
    board = crew_log.read_slot_projection(WORK_SLOT, "work", cold=False).value
    assert board["items"][0]["item_id"] == "it-1"
    # Other folds the same entry wakes have no subscriber and stay cold.
    assert {fold for (slot, fold) in _memo_keys() if slot == WORK_SLOT} == {"work"}


def test_on_demand_still_folds_for_a_dashboard_feed_with_its_baseline():
    first = _slot_log("s-od-feed-0", WORK_SLOT)
    _work_entry(first, "it-0")
    bus.set_slot_folds_on_demand(True)
    changes: list = []
    feed = DashboardFeed(WORK_SLOT, on_change=changes.append)
    unavailable = feed.subscribe(SimpleNamespace(folds=["work"]))  # type: ignore[arg-type]
    assert unavailable == []
    # A cold cell gives no baseline at subscribe time: one background prime folds it
    # and publishes, and that event is the feed's first value. ``drain`` waits for it.
    assert eager.drain(timeout=10.0)
    # That first publish is a value-less ``snapshot_needed``: the feed re-reads the cell
    # on its own thread, and ``wait_for_primes`` waits for that read too.
    feed.wait_for_primes()
    baseline = feed.fold_value("work")
    assert baseline and [i["item_id"] for i in baseline["items"]] == ["it-0"]
    _wake_work("s-od-feed-1", "it-1")
    after = feed.fold_value("work")
    assert after is not None
    assert {i["item_id"] for i in after["items"]} == {"it-0", "it-1"}
    feed.unsubscribe()


def test_a_wildcard_subscriber_means_nothing_is_skipped():
    bus.set_slot_folds_on_demand(True)
    seen: list = []
    bus.subscribe(bus.FOLD_ADVANCED, seen.append, scope="slot")
    _wake_work("s-od-wild", "it-1")
    assert (WORK_SLOT, "work") in _memo_keys()
    assert _work_events(seen)


def test_reset_for_tests_turns_on_demand_off():
    bus.set_slot_folds_on_demand(True)
    bus.reset_for_tests()
    assert bus.slot_fold_wanted("anything", "work")
