"""Receivers for work events that carry no value: DashboardFeed and conductor_wake."""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading
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
from kiro_crew.testing.wait import wait_until

FIXTURES = (
    pathlib.Path(__file__).resolve().parent.parent
    / "website"
    / "src"
    / "test"
    / "fixtures"
    / "work-delta-cases.json"
)


@pytest.fixture(autouse=True)
def _fresh_bus():
    bus.reset_for_tests()
    eager.reset_published_for_tests()
    conductor_wake.dispose_all()
    yield
    bus.reset_for_tests()
    eager.reset_published_for_tests()
    conductor_wake.dispose_all()


def _cases() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURES.read_text())["cases"])


def _delta(revision: int, base: int, value: Any = None, ops: Any = None) -> bus.FoldAdvanced:
    return bus.FoldAdvanced(
        scope=bus.SCOPE_SLOT,
        key=WORK_SLOT,
        fold="work",
        revision=revision,
        value=value if value is not None else {},
        seq=1,
        kind=fold_delta.KIND_DELTA,
        base_revision=base,
        delta=ops if ops is not None else {"header": {"order": []}, "items": {}, "remove": []},
    )


def _real_events() -> list[bus.FoldAdvanced]:
    """The ``work`` events a real board publishes over a run of conductor and worker
    writes, closes past the closed cap included, so ``remove`` ops appear."""
    events: list[bus.FoldAdvanced] = []
    bus.subscribe(bus.FOLD_ADVANCED, events.append, scope="slot", key=WORK_SLOT, fold="work")
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    writer.create("it_b")
    writer.report("it_a", "progress", "p1")
    _publish_now()
    writer.report("it_a", "progress", "p2")
    _publish_now()
    writer.report("it_b", "done", "b done")
    _publish_now()
    writer.decide("it_b", "accept")
    writer.report("it_a", "question", "which")
    _publish_now()
    writer.report("it_a", "question", "which")  # identical report: no stamp move
    _publish_now()
    for n in range(34):
        writer.create(f"it_x{n}")
        writer.close(f"it_x{n}")
    _publish_now()
    for n in range(34, 40):
        writer.create(f"it_x{n}")
        writer.close(f"it_x{n}")
    writer.report("it_a", "done", "a done")
    _publish_now()
    return events


def _feed() -> "Any":
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    changes: list[str] = []
    feed = DashboardFeed(WORK_SLOT, on_change=changes.append)
    feed._cells["work"] = _Cell()
    feed._disposers.append(
        bus.subscribe(
            bus.FOLD_ADVANCED,
            feed._make_callback("work"),
            scope="slot",
            key=WORK_SLOT,
            fold="work",
            baseline=True,
        )
    )
    return feed, changes


def test_value_after_applies_on_its_base_and_falls_back_to_the_carried_value():
    case = next(c for c in _cases() if c["name"] == "one item reports")
    before, after, ops = case["before"], case["after"], case["ops"]
    on_base = _delta(6, 5, value=after, ops=ops)
    assert fold_delta.value_after(before, 5, on_base) == after
    # The carried value is NOT what was returned: a delta on its base is applied.
    bare = _delta(6, 5, value={}, ops=ops)
    assert fold_delta.value_after(before, 5, bare) == after
    # A gap: the carried value while there is one, else ``None`` (pull a snapshot).
    assert fold_delta.value_after(before, 4, on_base) == after
    assert fold_delta.value_after(before, 4, bare) is None
    snapshot = bare._replace(kind=fold_delta.KIND_SNAPSHOT_NEEDED, base_revision=0)
    assert fold_delta.value_after(before, 5, snapshot) is None


def test_conductor_wake_reads_the_board_when_an_event_carries_neither_delta_nor_value(
    monkeypatch,
):
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    bare = bus.FoldAdvanced(
        "slot", WORK_SLOT, "work", 99, {}, 1, kind=fold_delta.KIND_SNAPSHOT_NEEDED
    )
    assert conductor_wake._event_news(bare) == (None, None)
    # A ``value`` event keeps its old reading, even an empty one.
    plain = bus.FoldAdvanced("slot", WORK_SLOT, "work", 99, {}, 1)
    assert conductor_wake._event_news(plain) == ([], None)
    monkeypatch.setattr(conductor_wake, "work_ledger_loop_id", lambda _svc, _board: "loop")
    asyncio.run(conductor_wake._observe_board(object(), WORK_SLOT, None))
    assert set(conductor_wake._registry.boards[WORK_SLOT]) == {"it_a"}


def test_dashboard_feed_applies_deltas_and_ends_equal_to_every_published_value(monkeypatch):
    applied: list[int] = []
    real = fold_delta.apply_work

    def _spy(value, ops):
        out = real(value, ops)
        applied.append(0 if out is None else 1)
        return out

    monkeypatch.setattr(fold_delta, "apply_work", _spy)
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    feed, changes = _feed()
    for n in range(4):
        writer.report("it_a", "progress", f"step {n}")
        _publish_now()
        published = eager._published[(WORK_SLOT, "work")]
        assert feed.fold_value("work") == published[1]
        assert feed._cells["work"].revision == published[0]
    assert applied == [1, 1, 1, 1], "each advance was a delta applied on the held value"
    assert changes == ["work"] * 5


def test_dashboard_feed_rereads_a_cell_it_cannot_apply_and_was_handed_no_value():
    writer = _Writer()
    writer.create("it_a")
    _publish_now()
    feed, changes = _feed()
    writer.report("it_a", "done", "d")
    _publish_now()
    held = feed._cells["work"].revision
    # As the publisher sends it: a gap and no whole value.
    feed._accept("work", _delta(held + 10, held + 7, value={}))
    wait_until(lambda: not feed._resyncing, describe=lambda: "the re-read finished")
    fresh = crew_log.read_slot_projection(WORK_SLOT, "work")
    assert feed.fold_value("work") == fresh.value
    assert feed._cells["work"].revision == eager.published_revision(WORK_SLOT, "work")
    assert changes[-1] == "work"


def test_dashboard_feed_reads_again_when_a_gap_lands_during_its_re_read(monkeypatch):
    from kiro_crew.dashboard_feed import DashboardFeed, _Cell

    feed = DashboardFeed(WORK_SLOT)
    feed._cells["work"] = _Cell()
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def _slow(_fold):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            release.wait(5)

    monkeypatch.setattr(feed, "_resync_once", _slow)
    feed._resync_soon("work")
    assert entered.wait(5)
    feed._resync_soon("work")
    release.set()
    wait_until(lambda: not feed._resyncing, describe=lambda: "both reads ran")
    assert len(calls) == 2
