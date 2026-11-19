"""Reclaim the worker a lost create reply leaves on a crew (remote-dispatch M1b).

Pinned here:

* a create whose reply was lost leaves ``create_tried``; the next pull binds the
  crew's worker to the open, unbound item, forwards a decision recorded while it
  was unbound, and never sends a DELETE;
* the same on a closed item records the worker ``retired`` (``item_closed``): its
  Trust is ended and, while it is not idle and done, no DELETE is sent;
* a create in flight is never reclaimed; a bound create, or one a crew that
  ignored ``work_item`` answered, leaves no mark, but only once no other create of
  the item is open; a refusal never clears a mark, nor does a crew that
  answers with no batch route keep one;
* a crew that lists no copy clears a mark only past its grace, and a list cut at
  the crew's cap clears none;
* the reclaim runs after the reports are filed, inside its own budget.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any

import aiohttp
import pytest
import test_remote_dispatch_batch_pull as batch
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_batch_pull import BatchCrew, _calls, _report, _worker_summaries
from test_remote_dispatch_hub_ingest import CREW, _read, _record_via, _remote_item, _state
from work_ledger_projection_helpers import ACCEPTANCE, CONDUCTOR, _record

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_dispatch_routes as crew_routes
from kiro_crew.dashboard.handlers import remote_ingest, remote_reclaim

_isolated = helpers._isolated
recorded = hub.recorded
_quiet = batch._quiet

ORPHAN = "chat-88-1793000000"


class ReclaimCrew(BatchCrew):
    """A batch crew that also lists its unnamed open copies, and can lose a create's
    reply after making the worker."""

    def __init__(self) -> None:
        super().__init__()
        self.lose_create_reply = False
        self.workers_complete = True

    def _rows(self, body: dict[str, Any]) -> dict[str, Any]:
        reply = super()._rows(body)
        reply["workers"] = [
            {"item_id": item_id, "key": c["slot"]}
            for item_id, c in self.copies.items()
            if item_id not in body["items"] and c["work_item"].get("state") == "open"
        ]
        reply["workers_complete"] = self.workers_complete
        return reply

    @asynccontextmanager
    async def proxy_request(
        self, instance_id, method, path, *, params=None, data=None, content_type=""
    ):
        if method == "POST" and path == "api/chat/slots" and self.lose_create_reply:
            item_id = json.loads(data)["work_item"]["item_id"]
            self.copy(item_id, ORPHAN)
            self.slots.append({"key": ORPHAN, "running": True})
            self.calls.append({"method": method, "path": path, "params": params, "body": None})
            raise aiohttp.ClientPayloadError("tunnel dropped mid-reply")
        async with super().proxy_request(
            instance_id, method, path, params=params, data=data, content_type=content_type
        ) as upstream:
            yield upstream


@pytest.fixture
def peer(monkeypatch) -> ReclaimCrew:
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    return ReclaimCrew()


def _age_mark(item_id: str, seconds: float) -> None:
    """Move *item_id*'s create mark *seconds* into the past."""
    from datetime import datetime, timedelta, timezone

    record = work_ledger.read_remote_state(CONDUCTOR, item_id)
    assert record is not None
    then = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    record.create_tried_at = then.isoformat(timespec="seconds")
    work_ledger.write_remote_state(CONDUCTOR, item_id, record)


async def _lost_create(peer: ReclaimCrew) -> str:
    status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "ship it", "round": 1})
    assert status == 200, body
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "item one", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    peer.lose_create_reply = True
    with pytest.raises(crew_routes.CrewCallError) as refused:
        await remote_ingest.create_bound(
            _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
        )
    assert refused.value.code == "crew_peer_interrupted"
    peer.lose_create_reply = False
    record = work_ledger.read_remote_state(CONDUCTOR, item_id)
    assert record is not None and record.create_tried == [CREW]
    assert record.create_tried_at
    assert not work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key
    return item_id


async def _closes_settle() -> None:
    while remote_ingest._CLOSE_TASKS:
        await asyncio.gather(*list(remote_ingest._CLOSE_TASKS))


def test_reclaim_binds_the_lost_creates_worker_to_its_open_item(recorded, peer):
    async def go() -> None:
        item_id = await _lost_create(peer)
        mark = len(peer.calls)
        await _read(peer)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert (item.worker_instance_id, item.worker_session_key) == (CREW, ORPHAN)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []
        assert not any(m == "DELETE" for m, _p in _calls(peer, mark))
        # The next tick reads it as the item's own worker.
        _report(peer.copies[item_id], "progress", "picked up", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        assert _worker_summaries(recorded) == ["picked up"]

    asyncio.run(go())


def test_reclaim_on_a_closed_item_goes_through_pending_close_never_delete(recorded, peer):
    async def go() -> None:
        item_id = await _lost_create(peer)
        status, body = await _record_via(
            peer, {"action": "close", "item_id": item_id, "state": "abandoned"}
        )
        assert status == 200, body
        mark = len(peer.calls)
        await _read(peer)
        await _closes_settle()
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.is_terminal and not item.worker_session_key
        (entry,) = item.retired_bindings
        assert (entry["worker_instance_id"], entry["worker_session_key"]) == (CREW, ORPHAN)
        assert entry["state"] == "retired" and entry["reason"] == remote_ingest.CLOSE_BY_WAKE
        calls = _calls(peer, mark)
        assert ("PATCH", f"api/chat/slots/{ORPHAN}/work-item") in calls
        # Running and never ``done``: H1's rule sends no DELETE from this turn.
        assert not any(m == "DELETE" for m, _p in calls)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []

    asyncio.run(go())


def test_a_create_in_flight_is_never_reclaimed_under_it(recorded, peer):
    async def go() -> None:
        item_id = await _lost_create(peer)
        remote_reclaim._CREATING[(CONDUCTOR, item_id)] = 1
        try:
            await _read(peer)
        finally:
            remote_reclaim._CREATING.pop((CONDUCTOR, item_id), None)
        assert not work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]

    asyncio.run(go())


def test_a_bound_create_leaves_no_mark(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []

    asyncio.run(go())


def test_a_refused_create_keeps_its_mark_for_the_reclaim(recorded, peer, monkeypatch):
    async def go() -> None:
        status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "g", "round": 1})
        assert status == 200, body
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        real = peer._answer

        def busy(method, path, body):
            if method == "POST" and path == "api/chat/slots":
                return 503, {"code": crew_routes.CREW_BUSY, "short": "memory"}
            return real(method, path, body)

        monkeypatch.setattr(peer, "_answer", busy)
        with pytest.raises(crew_routes.CrewBusyError):
            await remote_ingest.create_bound(
                _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
            )
        # A refusal says nothing of what the crew holds: only the reclaim clears it.
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]
        assert remote_reclaim._CREATING == {}

    asyncio.run(go())


def test_a_reclaimed_worker_gets_the_decision_recorded_while_unbound(recorded, peer):
    async def go() -> None:
        item_id = await _lost_create(peer)
        status, body = await _record_via(
            peer, {"action": "decide", "item_id": item_id, "decision": "use branch B"}
        )
        assert status == 200, body
        mark = len(peer.calls)
        await _read(peer)
        patches = [
            c["body"]
            for c in peer.calls[mark:]
            if c["method"] == "PATCH" and c["path"] == f"api/chat/slots/{ORPHAN}/work-item"
        ]
        assert {"decision": "use branch B"} in patches
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []

    asyncio.run(go())


def test_a_cut_worker_list_clears_no_mark_on_absence(recorded, peer):
    async def go() -> None:
        item_id = await _lost_create(peer)
        tried = work_ledger.creates_to_reclaim(CONDUCTOR)
        await remote_reclaim.reclaim_crew(_state(peer), CONDUCTOR, CREW, [], tried, complete=False)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]

    asyncio.run(go())


def test_a_crew_that_ignored_the_work_item_leaves_no_mark(recorded, peer):
    async def go() -> None:
        status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "g", "round": 1})
        assert status == 200, body
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        peer.create_echo = False
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await remote_ingest.create_bound(
                _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
            )
        assert refused.value.code == remote_ingest.CODE_PEER_LACKS_WORK_ITEMS
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []
        assert not work_ledger.board_has_open_remote(CONDUCTOR)

    asyncio.run(go())


def test_absence_inside_the_grace_keeps_the_mark_past_it_clears(recorded, peer):
    """A hub that restarted mid-create (no in-memory hold) reads a crew whose create
    has not bound yet: no copy listed. The mark stays until the grace has passed."""

    async def go() -> None:
        item_id = await _lost_create(peer)
        # The peer has not finished the create: nothing listed yet.
        del peer.copies[item_id]
        await _read(peer)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]
        _age_mark(item_id, remote_reclaim.CREATE_GRACE_SECS + 1)
        await _read(peer)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []

    asyncio.run(go())


def test_reports_are_filed_before_the_reclaim_and_it_keeps_its_own_budget(
    recorded, peer, monkeypatch
):
    order: list[str] = []
    real_batched = batch.remote_batch_pull.ingest_batched

    async def batched(state, mgr, board, item, row):
        order.append("filed")
        await real_batched(state, mgr, board, item, row)

    async def stuck(state, board, crew, workers, tried, *, complete=True):
        order.append("reclaim")
        await asyncio.Event().wait()

    monkeypatch.setattr(batch.remote_batch_pull, "ingest_batched", batched)
    monkeypatch.setattr(remote_reclaim, "reclaim_crew", stuck)
    # The budget a reclaim that never ends is cut off at; nothing waits on it.
    monkeypatch.setattr(remote_reclaim, "RECLAIM_BUDGET_SECS", 0.2)  # flake-ok: cut-off budget

    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("progress", "first", "2026-10-29T10:00:00+00:00")
        await _lost_create_second(peer)
        await _read(peer)
        return item_id

    item_id = asyncio.run(go())
    assert order[:2] == ["filed", "reclaim"]
    assert work_ledger.read_work_item(CONDUCTOR, item_id).summary == "first"


async def _lost_create_second(peer: ReclaimCrew) -> str:
    """A second item whose create reply is lost, on a board that already has one."""
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "two", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    item_id = body["item"]["item_id"]
    peer.lose_create_reply = True
    with pytest.raises(crew_routes.CrewCallError):
        await remote_ingest.create_bound(
            _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
        )
    peer.lose_create_reply = False
    return item_id


def test_a_copy_of_an_item_bound_elsewhere_only_loses_its_trust(recorded, peer):
    async def go() -> None:
        item_id = await _lost_create(peer)
        work_ledger.apply_remote_bind(
            CONDUCTOR, item_id, worker_session_key="chat-61-1793000000", worker_instance_id="inst-b"
        )
        mark = len(peer.calls)
        await _read(peer)
        calls = peer.calls[mark:]
        assert {
            "method": "PATCH",
            "path": f"api/chat/slots/{ORPHAN}/work-item",
            "params": None,
            "body": {"trust": False},
        } in calls
        assert not any(c["method"] == "DELETE" for c in calls)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.worker_instance_id == "inst-b"
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []

    asyncio.run(go())


def test_a_mark_whose_record_will_not_store_refuses_the_create(recorded, peer, monkeypatch):
    async def go() -> None:
        status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "g", "round": 1})
        assert status == 200, body
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]

        def broken(*_a: Any, **_k: Any) -> None:
            raise OSError("disk")

        monkeypatch.setattr(work_ledger, "write_remote_state", broken)
        mark = len(peer.calls)
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await remote_ingest.create_bound(
                _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
            )
        assert refused.value.code == "ledger_write_failed"
        # Nothing was sent: the crew made nothing it could orphan.
        assert not any(c["path"] == "api/chat/slots" for c in peer.calls[mark:])

    asyncio.run(go())


def test_a_busy_retry_keeps_an_earlier_lost_creates_mark(recorded, peer, monkeypatch):
    """A retry refused busy says nothing of the earlier create whose reply was lost."""

    async def go() -> None:
        item_id = await _lost_create(peer)
        real = peer._answer

        def busy(method, path, body):
            if method == "POST" and path == "api/chat/slots":
                return 503, {"code": crew_routes.CREW_BUSY, "short": "memory"}
            return real(method, path, body)

        monkeypatch.setattr(peer, "_answer", busy)
        with pytest.raises(crew_routes.CrewBusyError):
            await remote_ingest.create_bound(
                _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
            )
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]

    asyncio.run(go())


def test_a_full_mark_list_refuses_a_create_on_one_more_crew(recorded, peer):
    async def go() -> None:
        status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "g", "round": 1})
        assert status == 200, body
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        record = work_ledger.RemoteState(
            create_tried=[f"inst-{n}" for n in range(work_ledger.MAX_CREATE_TRIED)],
            create_tried_at="2026-10-29T10:00:00+00:00",
        )
        work_ledger.write_remote_state(CONDUCTOR, item_id, record)
        mark = len(peer.calls)
        with pytest.raises(crew_routes.CrewCallError) as refused:
            await remote_ingest.create_bound(
                _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
            )
        assert refused.value.code == remote_reclaim.CODE_RECLAIM_PENDING
        assert not any(c["path"] == "api/chat/slots" for c in peer.calls[mark:])
        kept = work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried
        assert kept == record.create_tried

    asyncio.run(go())


def test_a_sweep_cut_off_on_one_copy_starts_after_it_next_time(recorded, peer, monkeypatch):
    seen: list[str] = []

    async def one(state, board, crew, item_id, key, **_kwargs):
        seen.append(item_id)
        if item_id == "it_00000001":
            await asyncio.Event().wait()
        return True

    monkeypatch.setattr(remote_reclaim, "_reclaim_one", one)
    monkeypatch.setattr(remote_reclaim, "_RECLAIM_NEXT", {})
    # The budget a copy that never ends is cut off at; nothing waits on it.
    monkeypatch.setattr(remote_reclaim, "RECLAIM_BUDGET_SECS", 0.2)  # flake-ok: cut-off budget
    batches = {
        CREW: batch.remote_batch_pull.CrewBatch(
            {}, [("it_00000001", "chat-1"), ("it_00000002", "chat-2")], True
        )
    }

    async def go() -> None:
        await remote_reclaim.reclaim_all(_state(peer), CONDUCTOR, batches, {})
        await remote_reclaim.reclaim_all(_state(peer), CONDUCTOR, batches, {})

    asyncio.run(go())
    assert seen[:2] == ["it_00000001", "it_00000002"]


def test_an_overlapping_busy_create_keeps_the_other_creates_mark(recorded, peer, monkeypatch):
    """A create refused busy while another create of the item is in flight leaves
    the mark that in-flight create shares."""

    async def go() -> None:
        status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "g", "round": 1})
        assert status == 200, body
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        real = peer._answer

        def busy(method, path, body):
            if method == "POST" and path == "api/chat/slots":
                return 503, {"code": crew_routes.CREW_BUSY, "short": "memory"}
            return real(method, path, body)

        monkeypatch.setattr(peer, "_answer", busy)
        # The other create is between its mark and its answer.
        remote_reclaim._CREATING[(CONDUCTOR, item_id)] = 1
        try:
            with pytest.raises(crew_routes.CrewBusyError):
                await remote_ingest.create_bound(
                    _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
                )
        finally:
            remote_reclaim._CREATING.pop((CONDUCTOR, item_id), None)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]

    asyncio.run(go())


def test_a_sweep_cut_off_on_one_crew_starts_after_it_next_time(recorded, peer, monkeypatch):
    seen: list[str] = []

    async def stuck_or_note(state, board, crew, workers, tried, *, complete=True):
        seen.append(crew)
        if crew == "inst-a":
            await asyncio.Event().wait()

    monkeypatch.setattr(remote_reclaim, "reclaim_crew", stuck_or_note)
    monkeypatch.setattr(remote_reclaim, "_RECLAIM_NEXT_CREW", {})
    # The budget a crew that never ends is cut off at; nothing waits on it.
    monkeypatch.setattr(remote_reclaim, "RECLAIM_BUDGET_SECS", 0.2)  # flake-ok: cut-off budget
    empty = batch.remote_batch_pull.CrewBatch({}, [], True)
    batches = {"inst-a": empty, "inst-b": empty}

    async def go() -> None:
        await remote_reclaim.reclaim_all(_state(peer), CONDUCTOR, batches, {})
        await remote_reclaim.reclaim_all(_state(peer), CONDUCTOR, batches, {})

    asyncio.run(go())
    assert seen[:2] == ["inst-a", "inst-b"]


def test_a_settled_create_keeps_the_mark_while_another_is_open(recorded, peer):
    async def go() -> str:
        status, body = await _record(CONDUCTOR, {"action": "goal", "goal": "g", "round": 1})
        assert status == 200, body
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": "t", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        remote_reclaim._CREATING[(CONDUCTOR, item_id)] = 1
        try:
            await remote_ingest.create_bound(
                _state(peer), peer, CONDUCTOR, CREW, item_id, {"memory_mode": "persistent"}
            )
        finally:
            remote_reclaim._CREATING.pop((CONDUCTOR, item_id), None)
        return item_id

    item_id = asyncio.run(go())
    assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]


def test_a_crew_with_no_batch_route_clears_its_marks(recorded, peer):
    async def go() -> None:
        item_id = await _lost_create(peer)
        peer.batch = False  # an older crew: plain 404 on the route
        await _read(peer)
        assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []

    asyncio.run(go())


def test_a_mark_write_lands_even_when_its_caller_is_cancelled(recorded, peer, monkeypatch):
    """A reclaim cut off by its budget mid-write never leaves an older write to land
    after the board lock is free."""
    import threading

    gate, started = threading.Event(), threading.Event()
    landed: list[list[str]] = []
    real = work_ledger.write_remote_state

    def slow(board, item_id, record):
        started.set()
        gate.wait(timeout=30)  # flake-ok: bounded hold released by the test itself
        real(board, item_id, record)
        landed.append(list(record.create_tried))

    async def go() -> str:
        item_id = await _lost_create(peer)
        monkeypatch.setattr(work_ledger, "write_remote_state", slow)
        task = asyncio.ensure_future(remote_reclaim.clear_tried(CONDUCTOR, item_id, CREW))
        await asyncio.to_thread(started.wait, 30)  # flake-ok: bounded, set by the write itself
        task.cancel()
        gate.set()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return item_id

    item_id = asyncio.run(go())
    assert landed == [[]]
    assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == []


def test_a_listed_copy_with_no_mark_is_never_bound(recorded, peer):
    """A copy the hub sent no create for (no mark on that crew) gets at most its Trust
    ended, never a bind to an open, unbound item."""

    async def go() -> str:
        await _remote_item(peer)
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": "two", "acceptance": ACCEPTANCE}
        )
        item_id = body["item"]["item_id"]
        peer.copy(item_id, ORPHAN)
        peer.slots.append({"key": ORPHAN, "running": False})
        # Another item's mark keeps reclaim running on this board.
        other = work_ledger.RemoteState(
            create_tried=["inst-b"], create_tried_at="2026-10-29T10:00:00+00:00"
        )
        work_ledger.write_remote_state(CONDUCTOR, "it_ffffffff", other)
        await _read(peer)
        return item_id

    item_id = asyncio.run(go())
    assert not work_ledger.read_work_item(CONDUCTOR, item_id).worker_session_key


def test_a_closed_orphan_whose_retire_fails_still_loses_its_trust(recorded, peer, monkeypatch):
    async def go() -> str:
        item_id = await _lost_create(peer)
        status, body = await _record_via(
            peer, {"action": "close", "item_id": item_id, "state": "abandoned"}
        )
        assert status == 200, body

        async def refused(*_a: Any, **_k: Any) -> Any:
            return None

        monkeypatch.setattr(remote_reclaim, "_record", refused)
        mark = len(peer.calls)
        await _read(peer)
        calls = peer.calls[mark:]
        assert {
            "method": "PATCH",
            "path": f"api/chat/slots/{ORPHAN}/work-item",
            "params": None,
            "body": {"trust": False},
        } in calls
        assert not any(c["method"] == "DELETE" for c in calls)
        return item_id

    item_id = asyncio.run(go())
    assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]


def test_an_absence_clear_yields_to_a_create_begun_meanwhile(recorded, peer):
    async def go() -> str:
        item_id = await _lost_create(peer)
        _age_mark(item_id, remote_reclaim.CREATE_GRACE_SECS + 1)
        remote_reclaim._CREATING[(CONDUCTOR, item_id)] = 1
        try:
            await remote_reclaim._clear_reclaimed(CONDUCTOR, item_id, CREW, absent=True)
        finally:
            remote_reclaim._CREATING.pop((CONDUCTOR, item_id), None)
        return item_id

    item_id = asyncio.run(go())
    assert work_ledger.read_remote_state(CONDUCTOR, item_id).create_tried == [CREW]
