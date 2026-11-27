"""A peer report's quiet window runs on the HUB's clock, not the peer's.

An ingested report keeps the peer's stamp as ``last_report_at`` (it keys ingest
dedup and the rebuild check) and adds ``received_at``, the hub's clock at filing,
which the stale / quiet / decided-after-report rules measure from. Pinned here: a
peer clock 300 s ahead still lets a hub ``decide`` answer its ``question`` and the
item go stale after the window; a peer stamp far in the future still goes stale;
the same snapshot pulled twice files once; a rebuild from the crew log gives the
same fields; a local report carries no ``received_at``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import (
    StubCrew,
    _read,
    _record_via,
    _remote_item,
    _req,
)
from work_ledger_projection_helpers import CONDUCTOR

from kiro_crew import work_ledger, work_ledger_rules
from kiro_crew.crew_log import projection_work
from kiro_crew.dashboard.handlers import work_ledger as routes

_isolated = helpers._isolated
recorded = hub.recorded

WINDOW = work_ledger_rules.DEFAULT_STALE_WINDOW_SECS


@pytest.fixture(autouse=True)
def _no_background_puller(_floor_monkeypatch):
    """These tests drive the read path's own pull, as the hub ingest tests do."""
    from kiro_crew.dashboard.handlers import remote_puller

    _floor_monkeypatch.setattr(remote_puller, "ensure_running", lambda state: None)


@pytest.fixture
def peer(monkeypatch) -> StubCrew:
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    return StubCrew()


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _stale(item: work_ledger.WorkItem, now: datetime) -> bool:
    return work_ledger.is_stale(item, worker_running=False, now=now, window_secs=WINDOW)


def test_a_peer_clock_ahead_still_lets_a_decide_answer_and_the_item_go_stale(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        ahead = _iso(datetime.now(timezone.utc) + timedelta(seconds=300))
        peer.report("question", "which way?", ahead)
        await _read(peer)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.last_report_at == ahead
        received = work_ledger.parse_stamp(item.received_at or "")
        assert received is not None and received < work_ledger.parse_stamp(ahead)
        status, body = await _record_via(
            peer, {"action": "decide", "item_id": item_id, "decision": "go left"}
        )
        assert status == 200, body
        decided = work_ledger.read_work_item(CONDUCTOR, item_id)
        # The decide 10 s after the hub filed the question: on the peer's clock it
        # would read as before the report and leave the item waiting forever.
        decided.decided_at = _iso(received + timedelta(seconds=10))
        assert work_ledger_rules._decided_after_report(decided) == decided.decided_at
        answered = work_ledger.parse_stamp(decided.decided_at)
        assert not _stale(decided, answered + timedelta(seconds=WINDOW - 1))
        assert _stale(decided, answered + timedelta(seconds=WINDOW + 1))

    asyncio.run(go())


def test_a_peer_stamp_far_in_the_future_still_goes_stale(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "building", "2099-01-01T00:00:00+00:00")
        await _read(peer)
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.last_report_at == "2099-01-01T00:00:00+00:00"
        received = work_ledger.parse_stamp(item.received_at or "")
        assert received is not None
        assert work_ledger.quiet_since(item) == item.received_at
        assert not _stale(item, received + timedelta(seconds=WINDOW - 1))
        assert _stale(item, received + timedelta(seconds=WINDOW + 1))

    asyncio.run(go())


def test_the_same_snapshot_pulled_twice_files_once(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "half way", _iso(datetime.now(timezone.utc) + timedelta(hours=1)))
        await _read(peer)
        first = work_ledger.read_work_item(CONDUCTOR, item_id)
        await _read(peer)
        second = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert second.received_at == first.received_at
        entries = [d for _u, d in recorded if d.get("actor") == "worker"]
        assert len(entries) == 1
        assert entries[0]["received_at"] == first.received_at
        assert entries[0]["last_report_at"] == first.last_report_at

    asyncio.run(go())


def test_a_rebuild_gives_the_same_report_fields(peer, monkeypatch):
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, projection, slot_prime

    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes, "unit_for_session_key", lambda sessions, key: "u-conductor")
    CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )

    def _append(unit: str, data: dict[str, Any]) -> bool:
        projection.open_session_log(unit).append("work/recorded", data, src="gateway")
        return True

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _append)

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("blocked", "waiting on CI", "2099-01-01T00:00:00+00:00")
        await _read(peer)
        assert await asyncio.to_thread(slot_prime.drain, 30.0), "a background prime never finished"
        await _read(peer)
        before = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert before.status == "blocked" and before.received_at
        work_ledger.item_path(CONDUCTOR, item_id).unlink()
        resp = await routes.api_work_ledger_rebuild(
            _req("POST", "/api/work-ledger/rebuild", peer, body={})
        )
        assert resp.status == 200, resp.text
        after = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert after.last_report_at == before.last_report_at
        assert after.received_at == before.received_at
        assert after.status == before.status and after.summary == before.summary

    asyncio.run(go())


def test_a_local_report_carries_no_received_at():
    assert (
        "received_at" not in work_ledger.WorkItem(last_report_at="2026-01-01T00:00:00Z").to_dict()
    )
    # The fold: a report without the field clears one an earlier filing set.
    item = projection_work._work_new_item("it_1", 0)
    item["received_at"] = "2026-01-01T00:00:00+00:00"
    projection_work._work_apply(
        item,
        {"actor": "worker", "action": "report", "status": "progress", "summary": "s"},
        1000,
    )
    assert "received_at" not in item
