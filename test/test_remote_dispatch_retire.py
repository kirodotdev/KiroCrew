"""Retire and redispatch, wave 8 (rfc-conductor-remote-dispatch §5b), hub half.

Drives the real work-ledger routes and store with the wave 4 stub crew. Pinned here:

* switch off: a crew down past the timeout is never retired, and no peer call is made;
* down for less than the timeout: nothing moves;
* down past it: the read retires the binding (the item is open and unbound, the pair
  and its last pushed branch are in ``retired_bindings``) and the row says
  ``redispatch_due``;
* a late ``done`` from the retired worker is never applied;
* the retired peer slot is never closed here (wave 8b); a reconnect sends nothing;
* a retired slot is reached by ``close`` only;
* a rebuild from the crew log keeps the history and the unbound pair.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import (
    CREW,
    PEER_SLOT,
    StubCrew,
    _read,
    _remote_item,
    _req,
    _row,
    _state,
)
from work_ledger_projection_helpers import CONDUCTOR

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew_routes
from kiro_crew.dashboard.handlers import remote_ingest
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.testing.wait import async_wait_until, await_until

_isolated = helpers._isolated
recorded = helpers.recorded


class _Clock:
    """The retire timer's monotonic clock, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(remote_ingest, "_monotonic", fake)
    monkeypatch.setattr(remote_ingest, "_DOWN_SEEN", {})
    monkeypatch.setattr(remote_ingest, "_CLOSE_TRIED", set())
    monkeypatch.setattr(remote_ingest, "_retire_minutes", lambda: 30.0)
    return fake


@pytest.fixture(autouse=True)
def _no_background_puller(_floor_monkeypatch):
    """These tests drive the read path's own pull; a background puller started by
    the read would race it (its own tests are ``test_remote_dispatch_puller``)."""
    from kiro_crew.dashboard.handlers import remote_puller

    _floor_monkeypatch.setattr(remote_puller, "ensure_running", lambda state: None)


@pytest.fixture
def peer(monkeypatch, clock) -> StubCrew:
    stub = StubCrew()
    stub.clock = clock  # type: ignore[attr-defined]
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    return stub


def _age_down_since(item_id: str, minutes: float) -> None:
    """Move the stored ``crew_down_since`` *minutes* into the past."""
    record = work_ledger.read_remote_state(CONDUCTOR, item_id)
    assert record is not None and record.crew_down_since
    then = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    record.crew_down_since = then.isoformat(timespec="seconds")
    work_ledger.write_remote_state(CONDUCTOR, item_id, record)


async def _down_for(peer: StubCrew, item_id: str, minutes: float) -> dict[str, Any]:
    """The crew goes down, a read sees it, *minutes* pass, a second read runs."""
    peer.state = "disconnected"
    await _read(peer)
    _age_down_since(item_id, minutes)
    peer.clock.now += minutes * 60  # type: ignore[attr-defined]
    return _row(await _read(peer), item_id)


def test_switch_off_never_retires_and_calls_no_peer(recorded, monkeypatch, clock):
    peer = StubCrew()

    async def go() -> None:
        monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _read(peer)
        _age_down_since(item_id, 31)
        clock.now += 31 * 60
        monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: False)
        peer.calls.clear()
        row = _row(await _read(peer), item_id)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_session_key == PEER_SLOT and item.retired_bindings == []
        assert "redispatch_due" not in row and "retired_bindings" not in item.to_dict()
        assert all(e.kind != "retire" for e in work_ledger.read_events(CONDUCTOR, item_id))
        assert peer.calls == []

    asyncio.run(go())


def test_a_crew_down_less_than_the_timeout_is_not_retired(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        row = await _down_for(peer, item_id, 29)
        assert row["worker_instance_id"] == CREW and "redispatch_due" not in row

    asyncio.run(go())


def test_a_crew_down_past_the_timeout_is_retired_and_the_row_says_redispatch_due(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report(
            "progress",
            "pushed",
            "2026-10-29T10:00:00+00:00",
            artifacts={"branch": "b1"},
        )
        await _read(peer)
        row = await _down_for(peer, item_id, 31)
        assert row["redispatch_due"] is True and row["retired_crews"] == [CREW]
        assert "worker_instance_id" not in row or row["worker_instance_id"] is None
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.state == "open"
        assert item.worker_session_key is None and item.worker_instance_id is None
        (entry,) = item.retired_bindings
        assert entry["worker_session_key"] == PEER_SLOT and entry["worker_instance_id"] == CREW
        assert entry["state"] == "retired" and entry["branch"] == "b1"
        assert entry["reason"] == "crew_unreachable"
        assert work_ledger.read_events(CONDUCTOR, item_id)[-1].kind == "retire"
        # Nothing on the hub reserves the dead crew any more.
        assert work_ledger.open_remote_bindings().get(CREW, 0) == 0
        # The compact row carries the same answer.
        compact = _row(await _read(peer, "compact=true"), item_id)
        assert compact["redispatch_due"] is True

    asyncio.run(go())


def test_a_late_done_from_the_retired_worker_is_never_applied(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        await _down_for(peer, item_id, 31)
        peer.slots = []  # the slot is already gone when the crew comes back
        peer.state = "connected"
        peer.report("done", "late", "2026-10-29T12:00:00+00:00", pr=9)
        await _read(peer)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.status is None and item.pr is None and item.state == "open"
        assert not any(c["path"].endswith("/work-item") for c in peer.calls[-3:])

    asyncio.run(go())


def test_a_retire_on_an_item_bound_elsewhere_is_refused():
    work_ledger.ensure_conductor(CONDUCTOR)
    item = work_ledger.apply_conductor_action(
        CONDUCTOR, "create", title="t", acceptance={"kind": "manual"}
    )["item"]
    work_ledger.apply_remote_bind(
        CONDUCTOR, item.item_id, worker_session_key=PEER_SLOT, worker_instance_id=CREW
    )
    with pytest.raises(work_ledger.WorkLedgerError):
        work_ledger.apply_remote_retire(
            CONDUCTOR,
            item.item_id,
            worker_session_key="chat-other",
            worker_instance_id=CREW,
            reason="crew_unreachable",
        )
    with pytest.raises(work_ledger.WorkLedgerError):
        work_ledger.apply_remote_retire(
            CONDUCTOR,
            item.item_id,
            worker_session_key=PEER_SLOT,
            worker_instance_id=CREW,
            reason="not-a-reason",
        )


def test_the_history_keeps_at_most_its_cap_dropping_destroyed_first():
    history = [
        {"worker_session_key": f"s{i}", "worker_instance_id": "c", "state": "destroyed"}
        for i in range(work_ledger.MAX_RETIRED_BINDINGS)
    ]
    history.insert(0, {"worker_session_key": "keep", "worker_instance_id": "c", "state": "retired"})
    kept = work_ledger._trim_history(history)
    assert len(kept) == work_ledger.MAX_RETIRED_BINDINGS
    assert kept[0]["worker_session_key"] == "keep"


def test_a_retired_entry_is_never_dropped_so_a_full_history_refuses_the_retire():
    history = [
        {"worker_session_key": f"s{i}", "worker_instance_id": "c", "state": "retired"}
        for i in range(work_ledger.MAX_RETIRED_BINDINGS + 1)
    ]
    with pytest.raises(work_ledger.WorkLedgerError) as refused:
        work_ledger._trim_history(history)
    assert refused.value.code == work_ledger.CODE_RETIRED_HISTORY_FULL


def test_a_stale_down_record_never_retires_a_crew_connected_now(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _read(peer)
        _age_down_since(item_id, 31)
        # Back, but this read's pull never refreshes the record (timed out).
        peer.state = "connected"

        async def _slow(*_a: Any, **_k: Any) -> None:
            raise asyncio.TimeoutError

        monkeypatch.setattr(remote_ingest, "_ingest_item", _slow)
        await _read(peer)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_session_key == PEER_SLOT and item.retired_bindings == []

    asyncio.run(go())


def test_a_rebuild_keeps_the_history_and_the_unbound_pair(peer, monkeypatch):
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, projection

    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: "u-conductor")
    CrewLog.create(
        lg.KIND_SESSION,
        "u-conductor",
        owner="raymond",
        agent="kirocrew",
        slot=CONDUCTOR,
    )

    def _append(unit: str, data: dict[str, Any]) -> bool:
        projection.open_session_log(unit).append("work/recorded", data, src="gateway")
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _append)

    async def go() -> None:
        item_id = await _remote_item(peer)
        await _down_for(peer, item_id, 31)
        before = work_ledger.read_work_item(CONDUCTOR, item_id)
        work_ledger.item_path(CONDUCTOR, item_id).unlink()
        resp = await routes.api_work_ledger_rebuild(
            _req("POST", "/api/work-ledger/rebuild", peer, body={})
        )
        assert resp.status == 200, resp.text
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_session_key is None and item.worker_instance_id is None
        assert item.retired_bindings == before.retired_bindings
        assert item.retired_bindings[0]["state"] == "retired"

    asyncio.run(go())


@pytest.mark.parametrize(
    ("resp_extra", "says"),
    [
        ({}, None),
        ({"previous_branch": "b1"}, "start from `b1` and push a NEW branch name"),
        ({"previous_branch": None}, "seed the worker to push a NEW branch name"),
    ],
)
def test_the_create_reply_names_the_branch_to_resume(resp_extra, says):
    from unittest.mock import MagicMock

    from kiro_crew.mcp_session_crew import run_crew_create

    ctx = MagicMock()
    ctx.client.post.return_value = {
        "target": "c",
        "crew": "lab",
        "placed": True,
        **resp_extra,
    }
    out = run_crew_create({"work_item": "it_0000abcd"}, ctx)
    if says is None:
        assert "retired" not in out
    else:
        assert says in out


async def _retire_on(state: Any, peer: StubCrew, item_id: str) -> None:
    """Retire *item_id*'s binding through *state*, the gateway that dispatched it."""
    peer.state = "disconnected"
    await remote_ingest.ingest_board(state, CONDUCTOR)
    _age_down_since(item_id, 31)
    peer.clock.now += 31 * 60  # type: ignore[attr-defined]
    await remote_ingest.ingest_board(state, CONDUCTOR)
    assert work_ledger.read_work_item(CONDUCTOR, item_id).retired_bindings


def test_a_retire_drops_the_slot_from_the_dispatch_record(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        await _retire_on(state, peer, item_id)
        assert not crew_routes.dispatched_by(state, CONDUCTOR, CREW, PEER_SLOT)

    asyncio.run(go())


def test_a_retired_slot_is_reached_by_close_only(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        await _retire_on(state, peer, item_id)
        for verb in ("send", "read", "stop"):
            with pytest.raises(crew_routes.CrewCallError) as refused:
                await crew_routes._fenced_target_or_item(
                    state, CONDUCTOR, CONDUCTOR, CREW, PEER_SLOT, verb
                )
            assert refused.value.code == remote_ingest.CODE_TARGET_RETIRED
        slot = await crew_routes._fenced_target_or_item(
            state, CONDUCTOR, CONDUCTOR, CREW, PEER_SLOT, "close"
        )
        assert slot == PEER_SLOT

    asyncio.run(go())


def test_a_retired_slot_stays_refused_when_its_item_file_is_unreadable(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        await _retire_on(state, peer, item_id)
        # The store now skips the item: the history cannot be read at all.
        work_ledger.item_path(CONDUCTOR, item_id).write_text("{torn", encoding="utf-8")
        assert work_ledger.list_work_items(CONDUCTOR) == []
        for verb in ("send", "read", "stop", "close"):
            with pytest.raises(crew_routes.CrewCallError) as refused:
                await crew_routes._fenced_target_or_item(
                    state, CONDUCTOR, CONDUCTOR, CREW, PEER_SLOT, verb
                )
            assert refused.value.code == "crew_target_not_dispatched"

    asyncio.run(go())


def test_a_plain_dispatched_slot_on_an_unreadable_board_is_reached_as_on_main(
    recorded, peer, monkeypatch
):
    async def go() -> None:
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, "chat-plain-1793000000")

        async def _dirty(*_a: Any, **_k: Any) -> object:
            return object()

        monkeypatch.setattr(remote_ingest, "_refuse_if_dirty", _dirty)
        for verb in ("send", "read", "stop", "close"):
            slot = await crew_routes._fenced_target_or_item(
                state, CONDUCTOR, CONDUCTOR, CREW, "chat-plain-1793000000", verb
            )
            assert slot == "chat-plain-1793000000"

    asyncio.run(go())


def test_a_replacement_is_not_gated_by_the_retired_workers_clock(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        # The first crew's clock runs ahead: its report is stamped in the future.
        peer.report("progress", "ahead", "2099-01-01T00:00:00+00:00")
        await _read(peer)
        await _down_for(peer, item_id, 31)
        # A replacement on another crew, with an honest clock.
        work_ledger.apply_remote_bind(
            CONDUCTOR, item_id, worker_session_key=PEER_SLOT, worker_instance_id=CREW
        )
        peer.state = "connected"
        peer.slots = [{"key": PEER_SLOT, "running": True}]
        peer.work_item = {"item_id": item_id, "state": "open", "status": None}
        peer.events = []
        peer.report("done", "shipped", "2026-11-01T08:00:00+00:00", pr=5)
        await _read(peer)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.status == "done" and item.pr == 5

    asyncio.run(go())


@pytest.mark.parametrize("state", [[], {}, 3, None, "gone"])
def test_a_malformed_retired_state_is_dropped_not_raised(state):
    raw = [{"worker_session_key": "s", "worker_instance_id": "c", "state": state}]
    assert work_ledger.coerce_retired_bindings(raw) == []
    assert work_ledger.WorkItem.from_dict({"retired_bindings": raw}).retired_bindings == []


def test_a_down_stamp_from_before_a_restart_restarts_the_clock(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _read(peer)
        _age_down_since(item_id, 120)  # stored two hours ago, before the restart
        remote_ingest._DOWN_SEEN.clear()  # a new hub process remembers nothing
        row = _row(await _read(peer), item_id)
        assert "redispatch_due" not in row  # the first down after start starts the clock
        peer.clock.now += 29 * 60
        assert "redispatch_due" not in _row(await _read(peer), item_id)
        peer.clock.now += 2 * 60
        assert _row(await _read(peer), item_id)["redispatch_due"] is True

    asyncio.run(go())


def test_a_host_sleep_is_not_counted_as_outage(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _read(peer)
        # The wall clock jumps an hour (the host slept); the monotonic one does not.
        _age_down_since(item_id, 60)
        peer.clock.now += 5
        row = _row(await _read(peer), item_id)
        assert "redispatch_due" not in row
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key == PEER_SLOT

    asyncio.run(go())


def test_connecting_counts_as_up_and_clears_the_outage(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _read(peer)
        peer.clock.now += 20 * 60
        peer.state = "connecting"
        row = _row(await _read(peer), item_id)
        assert row["crew_state"] == "connecting" and row["crew_down_since"] is None
        peer.state = "disconnected"
        await _read(peer)
        peer.clock.now += 20 * 60  # 40 minutes since the first down, 20 since the last
        assert "redispatch_due" not in _row(await _read(peer), item_id)

    asyncio.run(go())


def test_a_failed_fresh_record_fences_the_item_until_a_read_stores_it(recorded, peer, monkeypatch):
    real_store = remote_ingest._store
    failing = {"on": True}

    def _store(board: str, item_id: str, record: work_ledger.RemoteState) -> bool:
        fresh = record.binding is None and record.report_floor_known
        if failing["on"] and fresh:
            return False
        return real_store(board, item_id, record)

    monkeypatch.setattr(remote_ingest, "_store", _store)

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "ahead", "2099-01-01T00:00:00+00:00")
        await _read(peer)
        await _down_for(peer, item_id, 31)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.retired_bindings and item.worker_session_key is None
        stale = work_ledger.read_remote_state(CONDUCTOR, item_id)
        assert stale is not None and stale.binding == f"{CREW}/{PEER_SLOT}"
        # Fenced: no replacement may bind while the old record stands.
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await remote_ingest.open_unbound_item(CONDUCTOR, item_id)
        assert refused.value.code == remote_ingest.CODE_RETIRE_PENDING
        await _read(peer)  # still failing: still fenced
        with pytest.raises(crew_routes.CrewCallError):
            await remote_ingest.open_unbound_item(CONDUCTOR, item_id)
        failing["on"] = False
        await _read(peer)  # a later read stores the fresh record
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).binding is None
        assert (await remote_ingest.open_unbound_item(CONDUCTOR, item_id)).item_id == item_id

    asyncio.run(go())


def test_a_record_naming_another_binding_files_nothing(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        record = work_ledger.RemoteState(binding="inst-old/chat-old")
        work_ledger.write_remote_state(CONDUCTOR, item_id, record)
        peer.report("done", "shipped", "2026-11-01T08:00:00+00:00", pr=5)
        await _read(peer)
        assert work_ledger.read_work_item(CONDUCTOR, item_id).status is None
        # That read replaced the stale record; the next one files the report.
        await _read(peer)
        assert work_ledger.read_work_item(CONDUCTOR, item_id).status == "done"

    asyncio.run(go())


def test_the_retire_clock_is_the_awake_clock():
    from kiro_crew import platform_compat

    # The helper stands still while the host is suspended, Windows included
    # (QueryUnbiasedInterruptTime); time.monotonic counts a Windows sleep. That
    # the timer counts only this clock's time is
    # test_a_host_sleep_is_not_counted_as_outage.
    assert remote_ingest._monotonic is platform_compat.awake_now


def test_a_clock_survives_reads_whose_slice_leaves_its_item_out(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _read(peer)
        key = next(k for k in remote_ingest._DOWN_SEEN if k[1] == item_id)
        started = remote_ingest._DOWN_SEEN[key]
        # A read whose slice holds nothing still keeps the open item's clock.
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        await remote_ingest._retire_overdue(_state(peer), CONDUCTOR, [], [item])
        assert remote_ingest._DOWN_SEEN[key] == started
        # Once the item is gone from the board's open items, its clock goes too.
        await remote_ingest._retire_overdue(_state(peer), CONDUCTOR, [], [])
        assert key not in remote_ingest._DOWN_SEEN

    asyncio.run(go())


def test_a_rebound_item_with_no_remote_record_takes_the_replacements_report(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "ahead", "2099-01-01T00:00:00+00:00")
        await _read(peer)
        await _down_for(peer, item_id, 31)
        work_ledger.apply_remote_bind(
            CONDUCTOR, item_id, worker_session_key="chat-new", worker_instance_id=CREW
        )
        # The binding's record is lost: the floor is the binding's, so none applies.
        work_ledger.remote_state_path(CONDUCTOR, item_id).unlink()
        peer.state = "connected"
        peer.slots = [{"key": "chat-new", "running": True}]
        peer.work_item = {"item_id": item_id, "state": "open", "status": None}
        peer.events = []
        peer.report("done", "shipped", "2026-11-01T08:00:00+00:00", pr=5)
        peer.calls.clear()
        await _read(peer)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.status == "done" and item.pr == 5
        # Only the current binding is pulled; the retired slot is only ever closed.
        assert all(PEER_SLOT not in c["path"] for c in peer.calls if c["method"] != "DELETE")

    asyncio.run(go())


def test_a_retired_workers_report_is_refused_with_no_remote_record(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        await _down_for(peer, item_id, 31)
        work_ledger.remote_state_path(CONDUCTOR, item_id).unlink(missing_ok=True)
        peer.state = "connected"
        peer.report("done", "late", "2026-11-01T09:00:00+00:00", pr=9)
        await _read(peer)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.status is None and item.pr is None and item.state == "open"

    asyncio.run(go())


def test_a_never_retired_item_keeps_the_legacy_floor():
    item = work_ledger.WorkItem(item_id="it_0000000a", last_report_at="2026-11-01T08:00:00+00:00")
    assert remote_ingest._report_floor(work_ledger.RemoteState(), item) == item.last_report_at
    item.retired_bindings = [
        {"worker_session_key": "s", "worker_instance_id": "c", "state": "retired"}
    ]
    assert remote_ingest._report_floor(work_ledger.RemoteState(), item) is None


def test_a_reconnect_clears_the_crews_clocks_outside_the_read_slice(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.state = "disconnected"
        await _read(peer)
        assert any(k[1] == item_id for k in remote_ingest._DOWN_SEEN)
        # The crew is back, but this read's slice does not reach the item.
        peer.state = "connected"
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        await remote_ingest._retire_overdue(_state(peer), CONDUCTOR, [], [item])
        assert not any(k[1] == item_id for k in remote_ingest._DOWN_SEEN)

    asyncio.run(go())


async def _closes_done() -> None:
    if remote_ingest._CLOSE_TASKS:
        await asyncio.gather(*list(remote_ingest._CLOSE_TASKS))


def test_partition_redispatch_reconnect_closes_the_old_slot(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        # Partition: the crew stays down past the timeout, so the binding retires.
        await _retire_on(state, peer, item_id)
        # Redispatch: a new worker is bound.
        work_ledger.apply_remote_bind(
            CONDUCTOR, item_id, worker_session_key="chat-new", worker_instance_id=CREW
        )
        peer.calls.clear()
        # Reconnect: the next read closes the old slot once and records it.
        peer.state = "connected"
        await remote_ingest.ingest_board(state, CONDUCTOR)
        await _closes_done()
        deletes = [c["path"] for c in peer.calls if c["method"] == "DELETE"]
        assert deletes == [f"api/chat/slots/{PEER_SLOT}"]
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        (entry,) = item.retired_bindings
        assert entry["state"] == "destroyed" and entry["destroyed_at"]
        assert work_ledger.read_events(CONDUCTOR, item_id)[-1].kind == "destroy"
        assert item.worker_session_key == "chat-new"
        peer.calls.clear()
        await remote_ingest.ingest_board(state, CONDUCTOR)
        await _closes_done()
        assert all(c["method"] != "DELETE" for c in peer.calls)

    asyncio.run(go())


def test_a_failed_close_stays_fenced_and_is_not_retried(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        await _retire_on(state, peer, item_id)
        peer.state = "connected"
        real = peer._answer
        peer._answer = lambda m, p, b: (  # type: ignore[method-assign]
            (500, {"code": "boom"}) if m == "DELETE" else real(m, p, b)
        )
        await remote_ingest.ingest_board(state, CONDUCTOR)
        await _closes_done()
        (entry,) = work_ledger.read_work_item(CONDUCTOR, item_id).retired_bindings
        assert entry["state"] == "retired"
        peer.calls.clear()
        await remote_ingest.ingest_board(state, CONDUCTOR)
        await _closes_done()
        assert all(c["method"] != "DELETE" for c in peer.calls)
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await crew_routes._fenced_target_or_item(
                state, CONDUCTOR, CONDUCTOR, CREW, PEER_SLOT, "send"
            )
        assert refused.value.code == remote_ingest.CODE_TARGET_RETIRED

    asyncio.run(go())


def test_no_close_is_tried_while_the_crew_is_down(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        await _retire_on(state, peer, item_id)
        peer.calls.clear()
        await remote_ingest.ingest_board(state, CONDUCTOR)
        await _closes_done()
        assert all(c["method"] != "DELETE" for c in peer.calls)
        assert not remote_ingest._CLOSE_TRIED

    asyncio.run(go())


def test_switch_off_never_closes_a_retired_slot_or_records_destroy(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        await _retire_on(state, peer, item_id)  # retired with the switch on
        monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: False)
        peer.state = "connected"
        peer.calls.clear()
        # The ledger read: no ingest, no retire, no close, no destroy.
        await _read(peer)
        await _closes_done()
        assert peer.calls == [] and not remote_ingest._CLOSE_TRIED
        (entry,) = work_ledger.read_work_item(CONDUCTOR, item_id).retired_bindings
        assert entry["state"] == "retired"
        assert all(e.kind != "destroy" for e in work_ledger.read_events(CONDUCTOR, item_id))
        # A crew= close is refused before any peer call.
        resp = await crew_routes.handle_crew_call(
            _req("POST", "/api/session-control/close", peer, body={}),
            "close",
            {"crew": CREW, "target": PEER_SLOT},
        )
        assert resp.status == 403 and peer.calls == []

    asyncio.run(go())


def test_the_puller_keeps_a_board_whose_only_remote_state_is_a_pending_close(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        await _retire_on(_state(peer), peer, item_id)
        assert work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key is None
        assert work_ledger.boards_with_open_remote() == [CONDUCTOR]
        # Closed: nothing pending, so the board leaves the puller's walk.
        peer.state = "connected"
        await remote_ingest.ingest_board(_state(peer), CONDUCTOR)
        await _closes_done()
        assert work_ledger.boards_with_open_remote() == []
        assert work_ledger.open_remote_bindings().get(CREW, 0) == 0

    asyncio.run(go())


def test_a_background_retire_needs_no_live_conductor_runtime(recorded, peer, monkeypatch):
    async def go() -> None:
        item_id = await _remote_item(peer)
        # No live provider resolves the conductor: only its durable unit.
        monkeypatch.setattr(remote_ingest, "_acting_unit", lambda *a, **k: (None, None))
        await _retire_on(_state(peer), peer, item_id)
        assert work_ledger.read_work_item(CONDUCTOR, item_id).retired_bindings

    asyncio.run(go())


def test_a_retire_cancelled_after_it_commits_still_drops_the_dispatch(recorded, peer, monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger as wl_routes
    from kiro_crew.dashboard.handlers.work_ledger_recording import _drain_before_cancelling

    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        peer.state = "disconnected"
        await remote_ingest.ingest_board(state, CONDUCTOR)
        _age_down_since(item_id, 31)
        peer.clock.now += 31 * 60
        gate = asyncio.Event()
        entered = asyncio.Event()
        real = wl_routes._record_conductor_write

        async def _slow_write(*args: Any, **kwargs: Any) -> Any:
            # As the real write does: it commits, then a cancel re-raises only
            # after the transaction has drained.
            resp = await real(*args, **kwargs)
            entered.set()
            await _drain_before_cancelling(await_until(gate, what="the test opens the gate"))
            return resp

        monkeypatch.setattr(wl_routes, "_record_conductor_write", _slow_write)
        task = asyncio.ensure_future(remote_ingest.ingest_board(state, CONDUCTOR))
        try:
            await await_until(entered, what="the retire write committed")
            task.cancel()
        finally:
            gate.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled read ends")
        assert work_ledger.read_work_item(CONDUCTOR, item_id).retired_bindings
        assert not crew_routes.dispatched_by(state, CONDUCTOR, CREW, PEER_SLOT)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).binding is None

    asyncio.run(go())


def test_a_retire_still_settling_admits_no_send_through_the_cache(recorded, peer, monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger as wl_routes

    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        peer.state = "disconnected"
        await remote_ingest.ingest_board(state, CONDUCTOR)
        _age_down_since(item_id, 31)
        peer.clock.now += 31 * 60
        seen: list[bool] = []
        real = wl_routes._record_conductor_write

        async def _watch(*args: Any, **kwargs: Any) -> Any:
            # While the retire is being written, the cache does not name the slot.
            seen.append(crew_routes.dispatched_by(state, CONDUCTOR, CREW, PEER_SLOT))
            return await real(*args, **kwargs)

        monkeypatch.setattr(wl_routes, "_record_conductor_write", _watch)
        await remote_ingest.ingest_board(state, CONDUCTOR)
        assert seen == [False]
        assert work_ledger.read_work_item(CONDUCTOR, item_id).retired_bindings

    asyncio.run(go())


@pytest.mark.parametrize("failure", ["refused", "error"])
def test_a_retire_that_does_not_land_restores_the_dispatch_entry(
    recorded, peer, monkeypatch, failure
):
    from kiro_crew.dashboard.handlers import work_ledger as wl_routes

    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        for i in range(crew_routes.MAX_CREW_SLOTS_PER_CALLER - 1):
            crew_routes._record_dispatch(state, CONDUCTOR, CREW, f"chat-fill-{i}")
        peer.state = "disconnected"
        await remote_ingest.ingest_board(state, CONDUCTOR)
        _age_down_since(item_id, 31)
        peer.clock.now += 31 * 60

        async def _refused(*_a: Any, **_k: Any) -> Any:
            if failure == "error":
                raise RuntimeError("boom")
            return wl_routes._refuse_400("retired_history_full", "full")

        monkeypatch.setattr(wl_routes, "_record_conductor_write", _refused)
        await remote_ingest.ingest_board(state, CONDUCTOR)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_session_key == PEER_SLOT and not item.retired_bindings
        assert crew_routes.dispatched_by(state, CONDUCTOR, CREW, PEER_SLOT)
        # The still-bound worker counts toward the cap: one more create is refused.
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await crew_routes._create(state, peer, CONDUCTOR, CREW, {})
        assert refused.value.code == "crew_dispatch_limit"

    asyncio.run(go())


def test_a_create_racing_a_refused_retire_never_ends_above_the_cap(recorded, peer, monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger as wl_routes

    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        for i in range(crew_routes.MAX_CREW_SLOTS_PER_CALLER - 1):
            crew_routes._record_dispatch(state, CONDUCTOR, CREW, f"chat-fill-{i}")
        peer.state = "disconnected"
        await remote_ingest.ingest_board(state, CONDUCTOR)
        _age_down_since(item_id, 31)
        peer.clock.now += 31 * 60
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def _refused_late(*_a: Any, **_k: Any) -> Any:
            # The retire has evicted the slot and waits here before it is refused.
            entered.set()
            await await_until(gate, what="the test opens the gate")
            return wl_routes._refuse_400("retired_history_full", "full")

        monkeypatch.setattr(wl_routes, "_record_conductor_write", _refused_late)
        retire = asyncio.ensure_future(remote_ingest.ingest_board(state, CONDUCTOR))
        create: asyncio.Future[Any] | None = None
        try:
            await await_until(entered, what="the retire write started")
            create = asyncio.ensure_future(crew_routes._create(state, peer, CONDUCTOR, CREW, {}))
            # The create waits on the cap lock; it does not read the mid-retire record.
            await asyncio.sleep(0)
            assert not create.done()
        finally:
            gate.set()
        await await_until(retire, what="the refused retire ends")
        assert create is not None
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await await_until(create, what="the racing create ends")
        assert refused.value.code == "crew_dispatch_limit"
        held = crew_routes._record(state)[CONDUCTOR]
        assert len(held) == crew_routes.MAX_CREW_SLOTS_PER_CALLER

    asyncio.run(go())


def test_a_close_during_a_refused_retire_is_not_undone_by_the_restore(recorded, peer, monkeypatch):
    from kiro_crew.dashboard.handlers import work_ledger as wl_routes

    async def go() -> None:
        item_id = await _remote_item(peer)
        state = _state(peer)
        crew_routes._record_dispatch(state, CONDUCTOR, CREW, PEER_SLOT)
        for i in range(crew_routes.MAX_CREW_SLOTS_PER_CALLER - 1):
            crew_routes._record_dispatch(state, CONDUCTOR, CREW, f"chat-fill-{i}")
        peer.state = "disconnected"
        await remote_ingest.ingest_board(state, CONDUCTOR)
        _age_down_since(item_id, 31)
        peer.clock.now += 31 * 60
        gate = asyncio.Event()
        entered = asyncio.Event()

        async def _refused_late(*_a: Any, **_k: Any) -> Any:
            entered.set()
            await await_until(gate, what="the test opens the gate")
            return wl_routes._refuse_400("retired_history_full", "full")

        monkeypatch.setattr(wl_routes, "_record_conductor_write", _refused_late)
        retire = asyncio.ensure_future(remote_ingest.ingest_board(state, CONDUCTOR))
        close: asyncio.Future[Any] | None = None
        try:
            await await_until(entered, what="the retire write started")
            # The crew is back; the owner closes the still-bound slot mid-retire.
            peer.state = "connected"
            close = asyncio.ensure_future(
                crew_routes._close(state, peer, CONDUCTOR, CREW, PEER_SLOT, CONDUCTOR)
            )
            lock = crew_routes.cap_lock(state)
            # The crew answers the close; its record removal then queues on the
            # cap lock the retire holds, rather than landing mid-retire.
            await async_wait_until(lambda: close.done() or bool(getattr(lock, "_waiters", None)))
            assert any(c["method"] == "DELETE" for c in peer.calls)
            assert not close.done()
        finally:
            gate.set()
        await await_until(retire, what="the refused retire ends")
        assert close is not None
        assert (await await_until(close, what="the close ends"))["closed"] is True
        # The closed slot does not come back, and does not count toward the cap.
        assert not crew_routes.dispatched_by(state, CONDUCTOR, CREW, PEER_SLOT)
        held = crew_routes._record(state)[CONDUCTOR]
        assert len(held) == crew_routes.MAX_CREW_SLOTS_PER_CALLER - 1

    asyncio.run(go())
