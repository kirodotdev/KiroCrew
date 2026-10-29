"""The ``work`` fold publishes deltas tied to a base revision (RFC fold-delta-push, PR c).

What is pinned here:

1. :func:`fold_delta.diff_work` / :func:`fold_delta.apply_work` round-trip: for each
   change the fold makes, ``apply(before, diff(before, after)) == after``.
2. Over a sequence of REAL advances (real crew log on disk, the real read path, the
   real publish), a subscriber that holds only the first value and then applies each
   delta ends equal to every published value, and each event's ``base_revision`` is the
   previous event's ``revision``.
3. A generation change, a first publish and a ``session/closed`` drop each publish
   ``snapshot_needed`` -- never a delta.
4. The old path is unchanged: every event still carries the whole value, the
   exporter's ``slot_projection`` frame for ``work`` is byte-identical to what it was,
   and a fold outside ``DELTA_FOLDS`` publishes ``kind="value"`` with no delta.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from crew_log_fold_eager_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    GATEWAY,
    WORK_SLOT,
    _isolated_home,
    _slot_log,
)

from kiro_crew.crew_log import bus, eager, fold_delta
from kiro_crew.crew_log import projection as crew_log


@pytest.fixture(autouse=True)
def _fresh_bus():
    bus.reset_for_tests()
    eager.reset_published_for_tests()
    yield
    bus.reset_for_tests()
    eager.reset_published_for_tests()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _item(item_id: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"item_id": item_id, "state": "open", "events": [], "schema": 1}
    base.update(fields)
    return base


def _board(items: list[dict[str, Any]], *, generation: str = "g1", omitted: int = 0) -> dict:
    return {
        "conductor": {"schema": 1, "slot_key": WORK_SLOT, "generation": generation},
        "items": items,
        "omitted": omitted,
        "forgotten": {"created": len(items)},
    }


class _Writer:
    """Writes ``work/recorded`` entries to a real conductor log for one board."""

    def __init__(self, unit_id: str = "s-delta", slot: str = WORK_SLOT) -> None:
        self.slot = slot
        self.handle = _slot_log(unit_id, slot)
        self.generation = "gen-1"
        self.n = 0

    def write(self, **data: Any) -> int:
        self.n += 1
        body = {"slot": self.slot, "by": self.slot, "generation": self.generation}
        body.setdefault("actor", "conductor")
        body.update(data)
        return int(self.handle.append("work/recorded", body, src=GATEWAY).seq)

    def create(self, item_id: str) -> int:
        return self.write(action="create", item_id=item_id, title=f"item {item_id}")

    def report(self, item_id: str, status: str, summary: str) -> int:
        return self.write(
            actor="worker",
            action="report",
            item_id=item_id,
            status=status,
            summary=summary,
            event_kind="report",
            event=summary,
        )

    def decide(self, item_id: str, decision: str) -> int:
        return self.write(
            action="decide",
            item_id=item_id,
            decision=decision,
            event_kind="decision",
            event=decision,
        )

    def close(self, item_id: str) -> int:
        return self.write(action="close", item_id=item_id, state="accepted", decision="done")


def _publish_now(slot: str = WORK_SLOT) -> None:
    """One real advance: the read path folds, then the real publish runs."""
    eager._publish_fold(slot, "work", crew_log.read_slot_projection(slot, "work"))


class _Receiver:
    """A bus subscriber that follows the receiver rule with the shared apply."""

    def __init__(self) -> None:
        self.events: list[bus.FoldAdvanced] = []
        self.value: dict[str, Any] | None = None
        self.revision = 0
        self.snapshots = 0
        bus.subscribe(bus.FOLD_ADVANCED, self._on, scope="slot", key=WORK_SLOT, fold="work")

    def _on(self, event: bus.FoldAdvanced) -> None:
        # A work event carries no whole value. Recorded beside it, for the assertions,
        # is the render it stands for: inside the publish that is
        # ``eager.published_value`` at its revision (what the WS exporter looks up).
        assert event.value is None, "a work event must not carry the whole render"
        whole = eager.published_value(event.key, event.fold, event.revision)
        event = event._replace(value=whole)
        self.events.append(event)
        if event.kind == fold_delta.KIND_DELTA and event.base_revision == self.revision:
            assert self.value is not None
            applied = fold_delta.apply_work(self.value, event.delta or {})
            assert applied is not None, "a delta on the held base must apply"
            self.value, self.revision = applied, event.revision
            return
        # snapshot_needed, or a base we do not hold: "pull" one snapshot. The pull here
        # is a deep copy of the render published at this revision, which is what a
        # REST read labelled with it returns.
        self.snapshots += 1
        assert event.value is not None
        self.value, self.revision = copy.deepcopy(dict(event.value)), event.revision


# --------------------------------------------------------------------------- #
# 1. diff / apply round-trip on hand-built renders
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(_board([_item("a")]), _board([_item("a"), _item("b")]), id="append"),
        pytest.param(
            _board([_item("a", events=[{"t": 1}, {"t": 3}])]),
            _board([_item("a", events=[{"t": 1}, {"t": 2}, {"t": 3}])]),
            id="time-order-insert",
        ),
        pytest.param(
            _board([_item("a", events=[{"k": "p1"}])]),
            _board([_item("a", events=[{"k": "p2"}])]),
            id="progress-collapse",
        ),
        pytest.param(
            _board([_item("a", state="accepted"), _item("b")]),
            _board([_item("b"), _item("c")], omitted=1),
            id="forget-and-create",
        ),
        pytest.param(_board([_item("a"), _item("b")]), _board([]), id="all-removed"),
        pytest.param(_board([_item("a")]), _board([_item("a")], omitted=5), id="header-only"),
    ],
)
def test_apply_of_diff_is_the_new_render(before, after):
    ops = fold_delta.diff_work(before, after)
    assert fold_delta.snapshot_reason(before, after) == ""
    assert fold_delta.apply_work(before, ops) == after
    # Only the items that differ travel.
    old = {i["item_id"]: i for i in before["items"]}
    assert set(ops["items"]) == {i["item_id"] for i in after["items"] if old.get(i["item_id"]) != i}


def test_apply_never_mutates_the_held_base():
    before = _board([_item("a"), _item("b")])
    held = copy.deepcopy(before)
    ops = fold_delta.diff_work(before, _board([_item("b", state="accepted")]))
    fold_delta.apply_work(held, ops)
    assert held == before


def test_apply_refuses_an_order_naming_an_unknown_item():
    ops = fold_delta.diff_work(_board([_item("a")]), _board([_item("a"), _item("b")]))
    ops["items"] = {}
    assert fold_delta.apply_work(_board([_item("a")]), ops) is None


def test_apply_drops_a_held_item_the_order_no_longer_names():
    held = _board([_item("a"), _item("x")])
    ops = fold_delta.diff_work(_board([_item("a")]), _board([_item("a")]))
    assert fold_delta.apply_work(held, ops) == _board([_item("a")]) | {"forgotten": {"created": 1}}


def test_a_generation_change_has_no_delta():
    before = _board([_item("a")], generation="g1")
    after = _board([_item("b")], generation="g2")
    assert fold_delta.snapshot_reason(before, after) == fold_delta.REASON_GENERATION


# --------------------------------------------------------------------------- #
# 2. real advances: delta == full-value diff, base_revision chains
# --------------------------------------------------------------------------- #


def test_deltas_over_real_advances_rebuild_every_published_value():
    writer = _Writer()
    receiver = _Receiver()
    steps = [
        lambda: writer.create("it-1"),
        lambda: writer.create("it-2"),
        lambda: writer.report("it-1", "progress", "started"),
        lambda: writer.report("it-1", "progress", "halfway"),  # collapses the first
        lambda: writer.decide("it-2", "go ahead"),
        lambda: writer.report("it-1", "done", "finished"),
        lambda: writer.close("it-1"),
        lambda: writer.create("it-3"),
    ]
    for step in steps:
        step()
        _publish_now()
    # Past the closed cap so the fold forgets the oldest closed items (``remove`` ops).
    for index in range(40):
        writer.create(f"bulk-{index}")
        writer.close(f"bulk-{index}")
        if index % 7 == 0:
            _publish_now()
    _publish_now()

    events = receiver.events
    assert len(events) >= len(steps) + 2
    assert events[0].kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert events[0].delta == {"reason": fold_delta.REASON_NO_BASE}
    for previous, event in zip(events, events[1:]):
        assert event.kind == fold_delta.KIND_DELTA, event.delta
        assert event.base_revision == previous.revision, "base_revision chains"
        assert event.revision > event.base_revision
        # The delta is exactly the full-value diff of the two published values.
        assert fold_delta.apply_work(previous.value, event.delta or {}) == event.value
    assert receiver.snapshots == 1, "only the first publish needs a snapshot"
    assert receiver.value == events[-1].value
    assert receiver.value == crew_log.read_slot_projection(WORK_SLOT, "work").value
    assert any(e.delta and e.delta.get("remove") for e in events), "the cap removed items"
    assert eager.published_revision(WORK_SLOT, "work") == events[-1].revision


def test_a_delta_ships_only_the_items_that_moved():
    writer = _Writer()
    receiver = _Receiver()
    for index in range(10):
        writer.create(f"it-{index}")
    _publish_now()
    writer.report("it-4", "progress", "moving")
    _publish_now()
    delta = receiver.events[-1].delta or {}
    assert list(delta["items"]) == ["it-4"]
    assert delta["remove"] == []
    full = len(json.dumps(receiver.events[-1].value))
    assert len(json.dumps(delta)) < full


def test_the_real_eager_worker_publishes_chained_deltas():
    """End to end: entries land, the real eager worker folds and publishes."""
    writer = _Writer("s-delta-e2e")
    receiver = _Receiver()
    for index in range(5):
        seq = writer.create(f"it-{index}")
        eager.note_commit("s-delta-e2e", "work/recorded", seq, board=WORK_SLOT)
        assert eager.drain(timeout=10.0)
        seq = writer.report(f"it-{index}", "progress", f"step {index}")
        eager.note_commit("s-delta-e2e", "work/recorded", seq, board=WORK_SLOT)
        assert eager.drain(timeout=10.0)
    assert receiver.events, "the eager worker published"
    assert receiver.snapshots == 1
    assert [e.kind for e in receiver.events[1:]] == [fold_delta.KIND_DELTA] * (
        len(receiver.events) - 1
    )
    assert receiver.value == crew_log.read_slot_projection(WORK_SLOT, "work").value


# --------------------------------------------------------------------------- #
# 3. snapshot_needed
# --------------------------------------------------------------------------- #


def test_a_generation_reset_publishes_snapshot_needed():
    writer = _Writer()
    receiver = _Receiver()
    writer.create("it-1")
    _publish_now()
    writer.report("it-1", "progress", "on it")
    _publish_now()
    writer.generation = "gen-2"
    writer.create("it-new")
    _publish_now()
    last = receiver.events[-1]
    assert last.kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert last.base_revision == 0
    assert last.delta == {"reason": fold_delta.REASON_GENERATION}
    assert [i["item_id"] for i in last.value["items"]] == ["it-new"]
    # The next advance chains off the new generation's value.
    writer.report("it-new", "progress", "fresh")
    _publish_now()
    assert receiver.events[-1].kind == fold_delta.KIND_DELTA
    assert receiver.events[-1].base_revision == last.revision
    assert receiver.snapshots == 2


def test_forgetting_a_closed_slot_makes_the_next_publish_a_snapshot():
    writer = _Writer()
    receiver = _Receiver()
    writer.create("it-1")
    _publish_now()
    writer.create("it-2")
    _publish_now()
    assert receiver.events[-1].kind == fold_delta.KIND_DELTA
    eager._forget_published(WORK_SLOT)
    assert eager.published_revision(WORK_SLOT, "work") == 0
    writer.create("it-3")
    _publish_now()
    assert receiver.events[-1].kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert receiver.events[-1].delta == {"reason": fold_delta.REASON_NO_BASE}


def test_a_repeated_revision_is_not_a_delta_and_keeps_the_base():
    writer = _Writer()
    receiver = _Receiver()
    writer.create("it-1")
    folded = crew_log.read_slot_projection(WORK_SLOT, "work")
    eager._publish_fold(WORK_SLOT, "work", folded)
    eager._publish_fold(WORK_SLOT, "work", folded)
    assert receiver.events[-1].kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert receiver.events[-1].delta == {"reason": fold_delta.REASON_REVISION}
    assert eager.published_revision(WORK_SLOT, "work") == folded.revision


def test_a_diff_that_raises_falls_back_to_snapshot_needed(monkeypatch):
    writer = _Writer()
    receiver = _Receiver()
    writer.create("it-1")
    _publish_now()

    def boom(_before, _after):
        raise RuntimeError("diff broke")

    monkeypatch.setattr(fold_delta, "diff_work", boom)
    writer.create("it-2")
    _publish_now()
    assert receiver.events[-1].kind == fold_delta.KIND_SNAPSHOT_NEEDED
    assert receiver.events[-1].delta == {"reason": fold_delta.REASON_SHAPE}


def test_the_lru_bound_drops_the_oldest_base(monkeypatch):
    monkeypatch.setattr(eager, "_PUBLISHED_CAP", 2)
    writers = [_Writer(f"s-lru-{n}", f"dashboard:lru{n}") for n in range(3)]
    for w in writers:
        w.create("it-1")
        _publish_now(w.slot)
    assert eager.published_revision("dashboard:lru0", "work") == 0
    assert eager.published_revision("dashboard:lru2", "work") > 0


# --------------------------------------------------------------------------- #
# 4. the old path is unchanged
# --------------------------------------------------------------------------- #


def test_no_work_event_carries_the_whole_value():
    """The bus carries only the delta for ``work``; the render it stands for is
    the publisher's recorded base, and equals a fresh read."""
    raw: list[bus.FoldAdvanced] = []
    bus.subscribe(bus.FOLD_ADVANCED, raw.append, scope="slot", key=WORK_SLOT, fold="work")
    writer = _Writer()
    writer.create("it-1")
    _publish_now()
    writer.create("it-2")
    _publish_now()
    assert [e.kind for e in raw] == [fold_delta.KIND_SNAPSHOT_NEEDED, fold_delta.KIND_DELTA]
    assert all(e.value is None for e in raw)
    read = crew_log.read_slot_projection(WORK_SLOT, "work").value
    assert eager.published_value(WORK_SLOT, "work", raw[-1].revision) == read
    # Any other revision has no recorded render: the base moved on.
    assert eager.published_value(WORK_SLOT, "work", raw[0].revision) is None


def test_a_fold_outside_delta_folds_publishes_a_plain_value():
    seen: list[bus.FoldAdvanced] = []
    bus.subscribe(bus.FOLD_ADVANCED, seen.append, scope="slot", key=WORK_SLOT, fold="ledger")

    class _Folded:
        revision = 7
        value = {"rows": []}
        seq = 3

    eager._publish_fold(WORK_SLOT, "ledger", _Folded())
    assert seen == [
        bus.FoldAdvanced(
            scope="slot", key=WORK_SLOT, fold="ledger", revision=7, value={"rows": []}, seq=3
        )
    ]
    assert seen[0].kind == fold_delta.KIND_VALUE
    assert seen[0].base_revision == 0 and seen[0].delta is None
    assert eager.published_revision(WORK_SLOT, "ledger") == 0


def test_the_exporter_frame_for_work_is_the_whole_value_as_before():
    """An old tab: a value-less delta event still produces the ``slot_projection`` frame
    a pre-delta event did, whole value included, from the publisher's recorded render."""
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers.crew_log import CrewLogPublisher

    sent: list[tuple[str, dict]] = []
    hub = SimpleNamespace(broadcast_ws_owners=lambda kind, data: sent.append((kind, data)))
    exporter = SimpleNamespace(_state=hub)

    raw: list[bus.FoldAdvanced] = []
    bus.subscribe(bus.FOLD_ADVANCED, raw.append, scope="slot", key=WORK_SLOT, fold="work")
    writer = _Writer()
    writer.create("it-1")
    _publish_now()
    writer.create("it-2")
    _publish_now()
    delta_event = raw[-1]
    assert delta_event.kind == fold_delta.KIND_DELTA and delta_event.value is None
    read = crew_log.read_slot_projection(WORK_SLOT, "work").value
    plain = delta_event._replace(kind="value", base_revision=0, delta=None, value=read)

    assert CrewLogPublisher._unpack(delta_event) is None
    unpacked, frame = CrewLogPublisher._unpack_delta(delta_event)
    assert unpacked == CrewLogPublisher._unpack(plain)
    assert frame is not None and frame["kind"] == fold_delta.KIND_DELTA
    for packed in (unpacked, CrewLogPublisher._unpack(plain)):
        assert packed is not None
        key, fold, revision, _seq, value = packed
        CrewLogPublisher._push_fold(exporter, key, fold, revision, value)  # type: ignore[arg-type]
    assert len(sent) == 2
    assert json.dumps(sent[0]) == json.dumps(sent[1])
    assert sent[0][1]["value"] == read
    assert "delta" not in sent[0][1] and "base_revision" not in sent[0][1]
