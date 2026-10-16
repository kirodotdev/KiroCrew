"""What the work fold carries: the board's stamps and its window of closed items.

``last_entry_at`` is the newest accepted stamp, ``decided_at`` survives the record, and
the fold forgets closed items past its window without resurrecting them.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
import work_ledger_projection_helpers
from work_ledger_projection_helpers import (
    ACCEPTANCE,
    CONDUCTOR,
    WORKER,
    _capture_probes,
    _record,
    _rendered,
    _width,
)

from kiro_crew import work_ledger as wl
from kiro_crew.crew_log import entry_types, projection, schema

# The shared fixtures, bound here by name so pytest registers them for this module.
_isolated = work_ledger_projection_helpers._isolated
recorded = work_ledger_projection_helpers.recorded


# ---------------------------------------------------------------------------
# _work_render reads last_entry_at directly, because the resume gate guarantees it
# ---------------------------------------------------------------------------


def test_work_render_surfaces_the_folded_last_entry_at() -> None:
    """The rendered conductor header carries the state's own ``last_entry_at``."""
    state = projection._work_start()
    state["last_entry_at"] = "2026-09-27T12:00:00+00:00"
    view = projection._work_render(state)
    assert view["conductor"]["last_entry_at"] == "2026-09-27T12:00:00+00:00"


def test_work_render_does_not_mask_a_missing_last_entry_at() -> None:
    """The pin. ``_work_render`` reads ``last_entry_at`` directly, like its sibling
    keys, so a state missing it surfaces loudly rather than as a silent ``""``.

    Reverting the read to ``state.get("last_entry_at", "")`` makes this state -- which
    the resume gate would never admit (see below) -- render without error, so the pin
    fails on the fix being mutated back.
    """
    state = projection._work_start()
    del state["last_entry_at"]
    with pytest.raises(KeyError):
        projection._work_render(state)


def test_a_work_checkpoint_missing_last_entry_at_cannot_resume() -> None:
    """Why the direct read is safe. ``_work_start`` declares ``last_entry_at`` in the
    fold's durable top-level shape, and ``_state_matches_fold`` refuses any state whose
    top-level keys differ from that shape. Such a checkpoint is discarded and
    cold-folded, so it never reaches ``_work_render`` with the key absent.
    """
    start = projection._work_start()
    assert "last_entry_at" in start, "the work fold's start shape must declare last_entry_at"
    missing = dict(start)
    del missing["last_entry_at"]
    assert projection._state_matches_fold("work", missing) is False


# ---------------------------------------------------------------------------
# last_entry_at is the board's NEWEST accepted stamp, not the last one folded
# ---------------------------------------------------------------------------


def _work_entry(*, seq: int, time_ms: int, actor: str, action: str, **fields: Any) -> schema.Entry:
    """One ``work/recorded`` entry for this board, at a chosen append time."""
    data: dict[str, Any] = {"slot": "member-board", "actor": actor, "by": "member-board"}
    data["action"] = action
    data.update(fields)
    return schema.Entry(
        type=entry_types.WORK_ENTRY_TYPE, seq=seq, time=time_ms, src="test", data=data
    )


def test_a_worker_entry_older_in_wall_time_does_not_pull_the_board_age_backwards() -> None:
    """THE fold-order defect. ``last_entry_at`` must be the newest stamp ACCEPTED, not
    the stamp of whichever entry happened to be folded last.

    ``_work_units`` returns the conductor's units first and then each bound worker's, so
    the fold order is by UNIT, never by wall time. A worker whose report was appended
    before the conductor's latest round is therefore folded after it, and an
    unconditional write hands the board that older stamp. The board's age is then
    computed from it, inflating to the gap between the two, and the drawer reads a
    current board as stale. It reproduces on every re-fold because the log order never
    changes.
    """
    state = projection._work_start()
    later = 1790000010000  # T+10s, the conductor's own round
    earlier = 1790000000000  # T+0, a worker's report appended before it

    projection._work_step(
        state, _work_entry(seq=1, time_ms=later, actor="conductor", action="goal")
    )
    assert state["last_entry_at"] == projection._work_iso(later)

    projection._work_step(
        state, _work_entry(seq=2, time_ms=earlier, actor="worker", action="report")
    )
    assert state["last_entry_at"] == projection._work_iso(
        later
    ), "a worker entry older in wall time must not become the board's newest stamp"


def test_a_genuinely_newer_entry_still_advances_the_board_age() -> None:
    """The control. Keeping the greatest stamp must not freeze the field at the first
    one: a fold that never advanced it would satisfy the test above too.
    """
    state = projection._work_start()
    first = 1790000000000
    newer = 1790000020000

    projection._work_step(
        state, _work_entry(seq=1, time_ms=first, actor="conductor", action="goal")
    )
    projection._work_step(
        state, _work_entry(seq=2, time_ms=newer, actor="conductor", action="decide")
    )
    assert state["last_entry_at"] == projection._work_iso(newer)


def test_a_new_board_generation_starts_its_age_over() -> None:
    """The greatest stamp is kept WITHIN one board generation, not across slots' history.

    A reset mints a new board under the same slot, and its age is its own. Carrying the
    previous board's newest stamp would make a board born now read as older than it is --
    and, worse, would pin the field where no entry of the new board could advance it.
    """
    state = projection._work_start()
    old = 1790000900000  # the previous board's newest entry, ahead of the new board's
    fresh = 1790000000000

    projection._work_step(
        state, _work_entry(seq=1, time_ms=old, actor="conductor", action="goal", generation="g1")
    )
    assert state["last_entry_at"] == projection._work_iso(old)

    projection._work_step(
        state, _work_entry(seq=2, time_ms=fresh, actor="conductor", action="goal", generation="g2")
    )
    assert state["generation"] == "g2"
    assert state["last_entry_at"] == projection._work_iso(
        fresh
    ), "a new generation's age is its own, not the purged board's"


# ---------------------------------------------------------------------------
# decided_at: the conductor's hand-back stamp survives the record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_decide_entry_carries_decided_at_and_the_probe_covers_it(recorded, monkeypatch):
    probes = _capture_probes(monkeypatch)
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    status, body = await _record(
        CONDUCTOR, {"action": "decide", "item_id": item_id, "decision": "go ahead"}
    )
    assert status == 200, body
    _unit, entry = recorded[-1]
    stored = wl.read_work_item(CONDUCTOR, item_id)
    assert stored is not None and stored.decided_at
    assert entry["decided_at"] == stored.decided_at
    assert set(entry) <= set(probes[-1]), set(entry) - set(probes[-1])
    assert _width(probes[-1]) >= _width(entry)


def test_the_fold_applies_decided_at_from_a_decide_entry() -> None:
    state = projection._work_start()
    projection._work_step(
        state,
        _work_entry(
            seq=1,
            time_ms=1790000000000,
            actor="conductor",
            action="create",
            item_id="it_0000abcd",
            title="t",
        ),
    )
    assert state["items"]["it_0000abcd"]["decided_at"] is None
    projection._work_step(
        state,
        _work_entry(
            seq=2,
            time_ms=1790000060000,
            actor="conductor",
            action="decide",
            item_id="it_0000abcd",
            decision="go ahead",
            decided_at="2026-09-20T10:06:00+00:00",
        ),
    )
    assert state["items"]["it_0000abcd"]["decided_at"] == "2026-09-20T10:06:00+00:00"


def test_a_baseline_entry_keeps_the_committed_decided_at() -> None:
    state = projection._work_start()
    projection._work_step(
        state,
        _work_entry(
            seq=1,
            time_ms=1790000000000,
            actor="conductor",
            action="verdict",
            item_id="it_0000abcd",
            baseline=True,
            title="t",
            verdict="fail",
            decided_at="2026-09-20T10:06:00+00:00",
        ),
    )
    assert state["items"]["it_0000abcd"]["decided_at"] == "2026-09-20T10:06:00+00:00"


def test_rebuild_carries_decided_at_from_the_fold(monkeypatch):
    item_id = "it_0000abcd"
    rendered = _rendered(CONDUCTOR, item_id)
    rendered["items"][0]["decided_at"] = "2026-09-20T10:06:00"
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=rendered if name == "work" else {}),
    )
    wl.rebuild_from_projection(CONDUCTOR)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.decided_at == "2026-09-20T10:06:00"


# ── the fold forgets closed items (rfc-work-fold-forget-closed) ─────────────


def _create_close_board(count: int, *, open_at_end: int = 0) -> tuple[dict[str, Any], list[str]]:
    """Fold a conductor log that creates and closes *count* items, interleaved,
    then creates *open_at_end* more it leaves open. Returns the state and the ids."""
    state = projection._work_start()
    projection._work_bind_slot(state, "member-board")
    seq = 0
    ids: list[str] = []
    peak = 0
    for index in range(count + open_at_end):
        item_id = f"it_{index:08x}"
        ids.append(item_id)
        seq += 1
        projection._work_step(
            state,
            _work_entry(
                seq=seq,
                time_ms=1790000000000 + seq,
                actor="conductor",
                action="create",
                item_id=item_id,
                title=f"t{index}",
                event_kind="create",
                event=f"t{index}",
            ),
        )
        peak = max(peak, len(state["items"]))
        if index < count:
            seq += 1
            projection._work_step(
                state,
                _work_entry(
                    seq=seq,
                    time_ms=1790000000000 + seq,
                    actor="conductor",
                    action="close",
                    item_id=item_id,
                    state="accepted",
                    event_kind="close",
                    event="accepted",
                ),
            )
    state["_peak"] = peak
    return state, ids


def test_a_long_board_folds_to_its_open_items_plus_the_closed_window() -> None:
    """Interleaved creates and closes past the old 256 bound fold without omission.

    The fold never holds more than the closed window plus what is open, whatever the
    board's lifetime total, and the forgotten counts keep the board's whole tally.
    """
    window = projection.WORK_CLOSED_WINDOW
    state, ids = _create_close_board(300, open_at_end=2)
    peak = state.pop("_peak")
    view = projection._work_render(state)

    assert view["omitted"] == 0
    assert peak <= window + 2
    held = [item["item_id"] for item in view["items"]]
    assert held == ids[300 - window :]  # the newest closed window, then the open two
    assert view["forgotten"] == {
        "created": 302,
        "items": 300 - window,
        "accepted": 300 - window,
        "rejected": 0,
        "abandoned": 0,
        "in_round": 300 - window,
        "baseline_ids": [],
    }
    assert set(state["tombstones"]) == set(ids[: 300 - window])


def test_forgotten_items_count_toward_added_this_round_until_the_round_moves() -> None:
    """``forgotten.in_round`` counts forgotten items created in the current round, and
    a goal entry that moves the round zeroes it: those items belong to an older round.
    """
    window = projection.WORK_CLOSED_WINDOW
    state, _ids = _create_close_board(window + 5)
    assert state["forgotten"]["in_round"] == 5

    projection._work_step(
        state,
        _work_entry(
            seq=100_000,
            time_ms=1790000900000,
            actor="conductor",
            action="goal",
            round=1,
            goal_version=1,
        ),
    )
    assert state["round"] == 1
    assert state["forgotten"]["in_round"] == 0
    assert projection._work_render(state)["forgotten"]["in_round"] == 0


def test_a_late_report_for_a_forgotten_item_is_dropped_not_parked() -> None:
    """A worker report written before a close but folded after it, once the item has
    left the window, is dropped: nothing parks, ``omitted`` stays 0."""
    window = projection.WORK_CLOSED_WINDOW
    state, ids = _create_close_board(window + 1)
    forgotten_id = ids[0]
    assert forgotten_id in state["tombstones"]

    projection._work_step(
        state,
        _work_entry(
            seq=10_000,
            time_ms=1790000000001,
            actor="worker",
            action="report",
            item_id=forgotten_id,
            status="done",
            summary="late",
            event_kind="report",
            event="late",
        ),
    )
    view = projection._work_render(state)

    assert state["parked"] == {}
    assert view["omitted"] == 0
    assert forgotten_id not in {item["item_id"] for item in view["items"]}


def test_a_late_report_for_an_item_still_in_the_window_still_applies() -> None:
    """Inside the window a closed item is whole, so a late report lands on it."""
    state, ids = _create_close_board(3)
    projection._work_step(
        state,
        _work_entry(
            seq=10_000,
            time_ms=1790000000001,
            actor="worker",
            action="report",
            item_id=ids[0],
            status="done",
            summary="late but kept",
            event_kind="report",
            event="late",
        ),
    )
    assert state["items"][ids[0]]["summary"] == "late but kept"


def test_the_tombstone_set_is_capped(monkeypatch) -> None:
    """Past the cap the oldest id goes first, so the set stays bounded."""
    monkeypatch.setattr(projection, "WORK_TOMBSTONE_LIMIT", 5)
    monkeypatch.setattr(projection, "WORK_CLOSED_WINDOW", 1)
    state, ids = _create_close_board(10)
    assert list(state["tombstones"]) == ids[4:9]
    assert state["forgotten"]["items"] == 9


def test_a_forgotten_baseline_item_is_not_resurrected_past_the_tombstone_cap(
    monkeypatch,
) -> None:
    """An item the log never created arrives by BASELINE, and a later baseline for
    the same id (a worker's report, folded after the conductor's units) would
    materialise it again once its tombstone was evicted. Such an id has no create to
    fall back on, so the cap evicts the ids of created items first."""
    monkeypatch.setattr(projection, "WORK_TOMBSTONE_LIMIT", 5)
    monkeypatch.setattr(projection, "WORK_CLOSED_WINDOW", 1)
    state = projection._work_start()
    projection._work_bind_slot(state, "member-board")
    legacy = "it_legacy00"
    projection._work_step(
        state,
        _work_entry(
            seq=1,
            time_ms=1789999990000,
            actor="conductor",
            action="close",
            item_id=legacy,
            baseline=True,
            title="legacy",
            state="accepted",
            event_kind="close",
            event="accepted",
        ),
    )
    seq = 1
    for index in range(10):
        item_id = f"it_{index:08x}"
        steps = (("create", {"title": f"t{index}"}), ("close", {"state": "accepted"}))
        for action, extra in steps:
            seq += 1
            projection._work_step(
                state,
                _work_entry(
                    seq=seq,
                    time_ms=1790000000000 + seq,
                    actor="conductor",
                    action=action,
                    item_id=item_id,
                    event_kind=action,
                    event=action,
                    **extra,
                ),
            )
    assert legacy in state["tombstones"]
    assert len(state["tombstones"]) == 5
    created = state["forgotten"]["created"]
    projection._work_step(
        state,
        _work_entry(
            seq=1,
            time_ms=1789999980000,
            actor="worker",
            action="report",
            item_id=legacy,
            baseline=True,
            title="legacy",
            state="open",
            summary="late",
            event_kind="report",
            event="late",
        ),
    )
    assert legacy not in state["items"]
    assert state["forgotten"]["created"] == created


def test_a_forgotten_created_item_whose_stamp_failed_is_not_resurrected(monkeypatch) -> None:
    """A created item whose recorded stamp failed carries a baseline on its next
    mutation, the conductor's bind, and on a worker's report after it. The worker's
    unit folds after the conductor's, so once the item is closed and forgotten that
    report arrives last; its tombstone must outlive the cap, or the report brings the
    item back as open and counts its create twice."""
    monkeypatch.setattr(projection, "WORK_TOMBSTONE_LIMIT", 5)
    monkeypatch.setattr(projection, "WORK_CLOSED_WINDOW", 1)
    state = projection._work_start()
    projection._work_bind_slot(state, "member-board")
    target = "it_unstamped"
    conductor_steps = (
        ("create", {"title": "t"}),
        ("bind", {"baseline": True, "title": "t", "worker_session_key": "worker-1"}),
        ("close", {"state": "accepted"}),
    )
    seq = 0
    for action, extra in conductor_steps:
        seq += 1
        projection._work_step(
            state,
            _work_entry(
                seq=seq,
                time_ms=1790000000000 + seq,
                actor="conductor",
                action=action,
                item_id=target,
                event_kind=action,
                event=action,
                **extra,
            ),
        )
        if action == "bind":
            # The marker is the fold's own: the rendered item does not carry it.
            (held,) = projection._work_render(state)["items"]
            assert "_unrecorded" not in held
    for index in range(10):
        item_id = f"it_{index:08x}"
        steps = (("create", {"title": f"t{index}"}), ("close", {"state": "accepted"}))
        for action, extra in steps:
            seq += 1
            projection._work_step(
                state,
                _work_entry(
                    seq=seq,
                    time_ms=1790000000000 + seq,
                    actor="conductor",
                    action=action,
                    item_id=item_id,
                    event_kind=action,
                    event=action,
                    **extra,
                ),
            )
    assert target in state["tombstones"]
    assert len(state["tombstones"]) == 5
    created = state["forgotten"]["created"]
    projection._work_step(
        state,
        _work_entry(
            seq=1,
            time_ms=1790000000002,
            actor="worker",
            action="report",
            item_id=target,
            baseline=True,
            title="t",
            state="open",
            summary="late",
            event_kind="report",
            event="late",
        ),
    )
    assert target not in state["items"]
    assert state["forgotten"]["created"] == created


def test_a_create_reusing_a_forgotten_id_is_a_new_item() -> None:
    """The log holds each entry once, so a create naming a forgotten id is not a
    replay: it folds as a new item, and the id stops being a tombstone. (The store
    keeps every record, so it never re-mints one; this covers a log written
    elsewhere.)"""
    window = projection.WORK_CLOSED_WINDOW
    state, ids = _create_close_board(window + 1)
    reused = ids[0]
    assert reused in state["tombstones"]
    projection._work_step(
        state,
        _work_entry(
            seq=10_000,
            time_ms=1790000009999,
            actor="conductor",
            action="create",
            item_id=reused,
            title="again",
            event_kind="create",
            event="again",
        ),
    )
    assert reused not in state["tombstones"]
    assert state["items"][reused]["title"] == "again"
    assert state["items"][reused]["state"] == "open"
    assert state["omitted"] == 0


def _closed_cached(title: str, closed_at: str) -> str:
    """A cached item, recorded and closed, with a pinned ``closed_at``."""
    item = wl.apply_conductor_action(CONDUCTOR, "create", title=title, acceptance={})["item"]
    wl.mark_item_recorded(CONDUCTOR, item.item_id)
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item.item_id, state="accepted")
    path = wl.item_path(CONDUCTOR, item.item_id)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["closed_at"] = closed_at
    path.write_text(json.dumps(raw), encoding="utf-8")
    return item.item_id


def _fold_with_one_closed(monkeypatch) -> dict[str, Any]:
    """A fold holding one closed item and reporting four closed items forgotten."""
    fold = _rendered(CONDUCTOR, "it_0000abcd")
    fold["items"][0]["state"] = "accepted"
    fold["items"][0]["closed_at"] = "2026-09-20T10:10:00"
    fold["forgotten"] = {"created": 5, "items": 4, "accepted": 4, "rejected": 0, "abandoned": 0}
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=fold if name == "work" else {}),
    )
    return fold


def test_a_rebuild_keeps_a_closed_record_the_fold_forgot(monkeypatch) -> None:
    """A cached closed item the fold does not hold is kept as the cache holds it --
    the cache is where a forgotten item stays whole -- and the create counter takes
    the fold's total, counting it once."""
    fold = _fold_with_one_closed(monkeypatch)
    fold["conductor"]["first_entry_at"] = "2000-01-01T00:00:00"
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    old = _closed_cached("old", "2026-09-20T10:05:00")
    before = wl.item_path(CONDUCTOR, old).read_bytes()

    counts = wl.rebuild_from_projection(CONDUCTOR)

    assert counts["removed"] == 0
    assert wl.item_path(CONDUCTOR, old).read_bytes() == before
    listed = {item.item_id for item in wl.list_work_items(CONDUCTOR)}
    assert listed == {"it_0000abcd", old}
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None and header.created_total == 5


def _closed_unrecorded(title: str) -> str:
    """A cached item closed by the store but NEVER recorded in the crew log."""
    item = wl.apply_conductor_action(CONDUCTOR, "create", title=title, acceptance={})["item"]
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item.item_id, state="accepted")
    return item.item_id


def test_a_closed_unrecorded_legacy_record_is_added_not_folded_in_a_rebuild(
    monkeypatch,
) -> None:
    """A CLOSED record the log never held (unrecorded, from before the board's first
    recorded entry) is terminal too, so the forgotten test would claim it and the total
    would lose it: ``max(held, forgotten.created)`` already covers the fold's creates.
    It is judged legacy and ADDED to the total."""
    fold = _fold_with_one_closed(monkeypatch)
    # The epoch is in the future (aware, like the store's stamps), so the record
    # closed below predates it.
    fold["conductor"]["first_entry_at"] = "2099-01-01T00:00:00+00:00"
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    legacy_closed = _closed_unrecorded("legacy")
    # No forgotten cache record is on disk: all four the fold forgot are "lost".

    counts = wl.rebuild_from_projection(CONDUCTOR)

    assert counts["removed"] == 0
    assert counts["legacy"] == 1
    assert wl.item_path(CONDUCTOR, legacy_closed).exists()
    header = wl.read_conductor(CONDUCTOR)
    # The fold's five creates plus the one legacy record the log never held.
    assert header is not None and header.created_total == 6


def test_a_dirty_rebuild_keeps_a_recorded_open_record_older_than_the_log(monkeypatch) -> None:
    """Retention pruned the unit holding a recorded open item's create, so the board's
    first surviving entry is newer than the item. On a dirty board the rebuild does not
    refuse a fold behind the cache, and the record must not be unlinked: the log cannot
    judge it, so it is kept as the cache holds it."""
    fold = _rendered(CONDUCTOR, "it_0000abcd")
    fold["conductor"]["first_entry_at"] = "2099-01-01T00:00:00+00:00"
    monkeypatch.setattr(
        projection,
        "read_slot_projection",
        lambda slot, name, **_kw: SimpleNamespace(value=fold if name == "work" else {}),
    )
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    item = wl.apply_conductor_action(CONDUCTOR, "create", title="pruned", acceptance={})["item"]
    wl.mark_item_recorded(CONDUCTOR, item.item_id)
    before = wl.item_path(CONDUCTOR, item.item_id).read_bytes()
    wl.mark_cache_dirty(CONDUCTOR, "an unrecorded write could not be undone")

    counts = wl.rebuild_from_projection(CONDUCTOR)

    assert counts["removed"] == 0
    assert counts["legacy"] == 0
    assert wl.item_path(CONDUCTOR, item.item_id).read_bytes() == before


def test_a_rebuild_keeps_the_binding_of_a_forgotten_record_it_keeps(monkeypatch) -> None:
    """A worker still bound to a closed item the fold forgot keeps its binding: the
    rebuild keeps the record, so it keeps the binding pointing at it too, and the
    worker's brief still resolves."""
    fold = _fold_with_one_closed(monkeypatch)
    fold["conductor"]["first_entry_at"] = "2000-01-01T00:00:00"
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    item = wl.apply_conductor_action(CONDUCTOR, "create", title="old", acceptance={})["item"]
    wl.mark_item_recorded(CONDUCTOR, item.item_id)
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item.item_id, worker_session_key=WORKER)
    wl.apply_conductor_action(CONDUCTOR, "close", item_id=item.item_id, state="accepted")
    assert wl.read_binding(WORKER) == (CONDUCTOR, item.item_id)

    counts = wl.rebuild_from_projection(CONDUCTOR)

    assert counts["removed"] == 0
    assert wl.item_path(CONDUCTOR, item.item_id).exists()
    assert wl.read_binding(WORKER) == (CONDUCTOR, item.item_id)


def test_a_forgotten_baseline_record_whose_stamp_failed_is_counted_once(monkeypatch) -> None:
    """A legacy record's baseline landed in the log, its recorded stamp then failed, and
    the fold let it go once closed: the record still reads unrecorded and predates the
    epoch, yet ``forgotten.created`` already counts it. The fold names it among its
    ``baseline_ids``, so the rebuild keeps it as forgotten and does not add it again."""
    fold = _fold_with_one_closed(monkeypatch)
    fold["conductor"]["first_entry_at"] = "2099-01-01T00:00:00+00:00"
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    folded_legacy = _closed_unrecorded("legacy")
    fold["forgotten"]["baseline_ids"] = [folded_legacy]

    counts = wl.rebuild_from_projection(CONDUCTOR)

    assert counts["removed"] == 0
    assert counts["legacy"] == 0
    assert wl.item_path(CONDUCTOR, folded_legacy).exists()
    header = wl.read_conductor(CONDUCTOR)
    # The fold's five creates, the forgotten baseline record among them.
    assert header is not None and header.created_total == 5


def test_the_fold_names_its_forgotten_baseline_items(monkeypatch) -> None:
    """The rendered ``forgotten`` block lists the ids whose tombstones are protected,
    and only those."""
    monkeypatch.setattr(projection, "WORK_CLOSED_WINDOW", 1)
    state = projection._work_start()
    projection._work_bind_slot(state, "member-board")
    steps = (
        ("it_legacy00", "close", {"baseline": True, "title": "legacy", "state": "accepted"}),
        ("it_00000001", "create", {"title": "a"}),
        ("it_00000001", "close", {"state": "accepted"}),
        ("it_00000002", "create", {"title": "b"}),
        ("it_00000002", "close", {"state": "accepted"}),
    )
    for seq, (item_id, action, extra) in enumerate(steps, start=1):
        projection._work_step(
            state,
            _work_entry(
                seq=seq,
                time_ms=1790000000000 + seq,
                actor="conductor",
                action=action,
                item_id=item_id,
                event_kind=action,
                event=action,
                **extra,
            ),
        )
    forgotten = projection._work_render(state)["forgotten"]
    assert forgotten["baseline_ids"] == ["it_legacy00"]
    assert forgotten["items"] == 2


def test_a_recorded_first_item_the_fold_forgot_is_counted_once(monkeypatch) -> None:
    """The board's first item shares its second with the epoch (the epoch IS its
    create entry), so stamp order alone would call it legacy. It is RECORDED, so the
    log holds its create and the fold's ``forgotten.created`` already counts it: it is
    kept as forgotten, not added again."""
    fold = _fold_with_one_closed(monkeypatch)
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    first = _closed_cached("first", "2026-09-20T10:05:00")
    cached = wl.read_work_item(CONDUCTOR, first)
    assert cached is not None and cached.recorded_at
    # The epoch is the first item's own create: the same second as its stamp.
    fold["conductor"]["first_entry_at"] = cached.created_at

    counts = wl.rebuild_from_projection(CONDUCTOR)

    assert counts["removed"] == 0
    assert counts["legacy"] == 0
    assert wl.item_path(CONDUCTOR, first).exists()
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None and header.created_total == 5


def test_a_torn_record_on_a_board_that_forgot_items_keeps_its_files(monkeypatch) -> None:
    """A record whose content does not parse, on a board whose fold has forgotten
    closed items, may be one of those items -- whose only whole copy is the cache.
    The rebuild leaves its record AND events file in place instead of deleting them,
    and does not count it."""
    _fold_with_one_closed(monkeypatch)
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    torn_id = _closed_cached("torn", "2026-09-20T10:05:00")
    events = wl.item_events_path(CONDUCTOR, torn_id)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text('{"kind": "report"}\n', encoding="utf-8")
    wl.item_path(CONDUCTOR, torn_id).write_text('{"item_id": "it_', encoding="utf-8")
    events_before = events.read_bytes()

    counts = wl.rebuild_from_projection(CONDUCTOR)

    assert counts["removed"] == 0
    assert wl.item_path(CONDUCTOR, torn_id).exists()
    assert events.read_bytes() == events_before
    header = wl.read_conductor(CONDUCTOR)
    assert header is not None and header.created_total == 5


def test_a_rebuild_still_refuses_an_open_item_the_fold_does_not_hold(monkeypatch) -> None:
    """Forgetting covers CLOSED items only: an open cached item the fold lacks still
    means a pruned unit, and the rebuild refuses rather than erase it."""
    fold = _fold_with_one_closed(monkeypatch)
    fold["conductor"]["first_entry_at"] = "2000-01-01T00:00:00"
    wl.ensure_conductor(CONDUCTOR, goal="ship it")
    live = wl.apply_conductor_action(CONDUCTOR, "create", title="live", acceptance={})["item"]
    wl.mark_item_recorded(CONDUCTOR, live.item_id)

    with pytest.raises(wl.WorkLedgerError) as caught:
        wl.rebuild_from_projection(CONDUCTOR)
    assert caught.value.code == wl.CODE_CREW_LOG_INCOMPLETE
    assert wl.item_path(CONDUCTOR, live.item_id).exists()
