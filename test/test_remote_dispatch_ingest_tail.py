"""Every peer report between two hub pulls is filed, not only the latest.

The peer's ``GET work-item`` carries an event tail; the hub files each report in it
after the one its last pull filed, in order, then the latest snapshot. Pinned here:
a ``question`` then a ``progress`` between two pulls both land, once; a report in
the same second as the last filed one still lands; a tail that rolled past the last
filed report files what is newer; one report that cannot file never stalls the rest;
a hostile tail (wrong types, bad stamps, future stamps, overlong text, a planted
credential, a thousand entries) is refused, clamped or capped, never filed raw.
The probe wake is pinned in ``test_remote_dispatch_puller`` and the retired-binding
fence in ``test_remote_dispatch_retire``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import (
    PLANTED,
    StubCrew,
    _read,
    _remote_item,
    _row,
)
from work_ledger_projection_helpers import CONDUCTOR

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_ingest
from kiro_crew.probes import work_ledger as probe_work_ledger

_isolated = helpers._isolated
recorded = helpers.recorded


@pytest.fixture(autouse=True)
def _no_background_puller(_floor_monkeypatch):
    """These tests drive the read path's own pull, as the hub ingest tests do."""
    from kiro_crew.dashboard.handlers import remote_puller

    _floor_monkeypatch.setattr(remote_puller, "ensure_running", lambda state: None)


@pytest.fixture
def peer(monkeypatch) -> StubCrew:
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    return StubCrew()


def _worker_statuses(recorded) -> list[str]:
    return [d["status"] for _u, d in recorded if d.get("actor") == "worker"]


def test_a_question_then_progress_between_two_pulls_files_both_once(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        # A repeat pull of the same tail adds nothing.
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["progress", "question", "progress"]
        row = _row(await _read(peer), item_id)
        assert row["status"] == "progress" and row["summary"] == "carrying on"

    asyncio.run(go())


def test_a_report_in_the_same_second_as_the_last_filed_one_is_still_filed(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["progress", "question", "progress"]

    asyncio.run(go())


def test_a_tail_that_no_longer_reaches_the_last_filed_report_files_what_is_newer(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:01:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:02:00+00:00")
        peer.report("done", "shipped", "2026-10-29T10:03:00+00:00", pr=4)
        # The filed report rolled off the peer's tail.
        peer.events = peer.events[1:]
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["progress", "blocked", "question", "done"]
        assert _row(await _read(peer), item_id)["pr"] == 4

    asyncio.run(go())


def test_a_refused_earlier_report_never_stalls_the_ones_after_it(recorded, peer, monkeypatch):
    real = remote_ingest._file_one

    async def flaky(state, board, unit, item_id, slot, cleaned):
        if cleaned["status"] == "question":
            return "work_entry_too_large"
        return await real(state, board, unit, item_id, slot, cleaned)

    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(remote_ingest, "_file_one", flaky)
        row = _row(await _read(peer), item_id)
        assert row["remote_error"] == "work_entry_too_large"
        await _read(peer)
        await _read(peer)
        return item_id

    item_id = asyncio.run(go())
    assert _worker_statuses(recorded) == ["blocked", "progress"]
    assert work_ledger.read_work_item(CONDUCTOR, item_id).summary == "carrying on"


def test_a_hostile_event_tail_is_refused_or_capped_never_filed_raw(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        stamp = "2026-10-29T10:00:00+00:00"
        peer.report("progress", "started", "2026-10-29T09:00:00+00:00")
        await _read(peer)
        hostile: list[Any] = [
            "not an event",
            {"id": "h1", "kind": "report", "status": "question", "text": "no stamp"},
            {
                "id": "h2",
                "kind": "report",
                "status": ["question"],
                "ts": stamp,
                "text": "list status",
            },
            {"id": "h3", "kind": "report", "status": "question", "ts": 7, "text": "number stamp"},
            {"id": "h4", "kind": "report", "status": "question", "ts": stamp, "text": {"a": 1}},
            {"id": "h5", "kind": "report", "status": "question", "ts": "soon", "text": "bad stamp"},
            {"id": "h6", "kind": "report", "status": "finished", "ts": stamp, "text": "bad status"},
            {"id": "h7", "kind": "report", "status": "question", "ts": stamp, "text": "W" * 5000},
            {
                "id": "hd",
                "kind": "decision",
                "status": "question",
                "ts": stamp,
                "text": "not a report",
            },
            {
                "id": "h8",
                "kind": "report",
                "status": "question",
                "ts": stamp,
                "text": f"key {PLANTED}",
            },
            # After the snapshot's own stamp: from the future, never filed.
            {
                "id": "h9",
                "kind": "report",
                "status": "blocked",
                "ts": "2099-01-01T00:00:00+00:00",
                "text": "x",
            },
            # Ids no cursor can hold.
            {"id": 5, "kind": "report", "status": "question", "ts": stamp, "text": "int id"},
            {"kind": "report", "status": "question", "ts": stamp, "text": "no id"},
            {"id": "x" * 65, "kind": "report", "status": "question", "ts": stamp, "text": "long"},
        ]
        peer.events = [*peer.events, *hostile]
        peer.work_item.update({"status": "done", "summary": "shipped", "last_report_at": stamp})
        peer.events.append(
            {"id": "e", "ts": stamp, "kind": "report", "status": "done", "text": "shipped"}
        )
        await _read(peer)
        # The overlong text is clamped to the report cap, as a local report's is.
        assert _worker_statuses(recorded) == ["progress", "question", "question", "done"]
        summaries = [d["summary"] for _u, d in recorded if d.get("actor") == "worker"]
        assert max(len(s) for s in summaries) == work_ledger.MAX_SUMMARY_CHARS
        assert PLANTED not in json.dumps(recorded, default=str)
        row = _row(await _read(peer), item_id)
        assert PLANTED not in json.dumps(row) and "2099" not in json.dumps(row)
        # An oversized tail: only the newest entries are read, and few are filed.
        peer.events = [
            {
                "ts": f"2026-10-29T11:{n // 60:02d}:{n % 60:02d}+00:00",
                "id": f"o{n}",
                "kind": "report",
                "status": "question" if n % 2 else "blocked",
                "text": f"r{n}",
            }
            for n in range(1000)
        ]
        last = peer.events[-1]
        peer.work_item.update(
            {"status": last["status"], "summary": last["text"], "last_report_at": last["ts"]}
        )
        recorded.clear()
        await _read(peer)
        filed = _worker_statuses(recorded)
        assert len(filed) == remote_ingest.MAX_EARLIER_REPORTS + 1
        # Each filed report stays inside the probe's tail, so each one can still wake.
        assert remote_ingest.MAX_EARLIER_REPORTS + 1 <= probe_work_ledger._EVENT_TAIL

    asyncio.run(go())


def test_a_later_report_repeating_the_last_filed_one_is_not_taken_for_it(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("progress", "working", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        # The filed progress coalesces away on the peer; a later one repeats its text.
        peer.report("progress", "checking", "2026-10-29T10:01:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:02:00+00:00")
        peer.report("progress", "working", "2026-10-29T10:03:00+00:00")
        peer.report("question", "which model?", "2026-10-29T10:04:00+00:00")
        peer.report("progress", "continuing", "2026-10-29T10:05:00+00:00")
        await _read(peer)
        await _read(peer)
        summaries = [d["summary"] for _u, d in recorded if d.get("actor") == "worker"]
        assert summaries == [
            "working",
            "checking",
            "which branch?",
            "working",
            "which model?",
            "continuing",
        ]

    asyncio.run(go())


def test_an_older_repeat_of_the_last_filed_report_is_not_taken_for_it(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:01:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        peer.report("progress", "done soon", "2026-10-29T10:03:00+00:00")
        for _ in range(3):
            await _read(peer)
        assert _worker_statuses(recorded) == ["question", "progress", "question", "progress"]

    asyncio.run(go())


def test_question_progress_question_in_one_second_files_each_once(recorded, peer):
    """The peer collapses the second question's event (same id), so the tail ends on
    the progress while the snapshot is the question: each lands once, and the pulls
    after it file nothing."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        stamp = "2026-10-29T10:00:00+00:00"
        peer.report("question", "which branch?", stamp)
        peer.report("progress", "carrying on", stamp)
        peer.report("question", "which branch?", stamp)
        assert [e["status"] for e in peer.events] == ["question", "progress"]
        await _read(peer)
        for _ in range(3):
            await _read(peer)
        assert _worker_statuses(recorded) == ["question", "progress", "question"]
        record = work_ledger.read_remote_state(CONDUCTOR, item_id)
        assert record.report_event_id == f"{peer.events[0]['id']}/{peer.events[1]['id']}"

    asyncio.run(go())


def test_a_pull_cut_off_between_two_earlier_reports_never_files_one_twice(
    recorded, peer, monkeypatch
):
    real = remote_ingest._file_one
    calls = {"n": 0}
    reader: list[asyncio.Task] = []

    async def cut(*a, **k):
        calls["n"] += 1
        result = await real(*a, **k)
        if calls["n"] == 1:
            # Cut the pull right after the first filing, at the same point on every run.
            reader[0].cancel()
        return result

    async def go() -> None:
        await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(remote_ingest, "_file_one", cut)

        task = asyncio.ensure_future(_read(peer))
        reader.append(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls["n"] == 1
        monkeypatch.setattr(remote_ingest, "_file_one", real)
        await _read(peer)
        await _read(peer)

    asyncio.run(go())
    assert _worker_statuses(recorded) == ["blocked", "question", "progress"]


def test_a_cursor_that_stores_only_for_the_snapshot_never_skips_an_unfiled_report(
    recorded, peer, monkeypatch
):
    real = remote_ingest._store
    fails = {"left": 1}

    def flaky(*a):
        if fails["left"]:
            fails["left"] -= 1
            return False
        return real(*a)

    async def go() -> None:
        await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(remote_ingest, "_store", flaky)
        # The question's cursor fails once; the snapshot's store would succeed.
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["progress", "progress", "question", "progress"]

    asyncio.run(go())


def test_a_first_pull_whose_cursor_fails_once_still_files_the_earlier_report_later(
    recorded, peer, monkeypatch
):
    real = remote_ingest._store
    fails = {"left": 1}

    def flaky(*a):
        if fails["left"]:
            fails["left"] -= 1
            return False
        return real(*a)

    async def go() -> None:
        item_id = await _remote_item(peer)
        # A record from before the per-binding floor: no floor, no cursor.
        work_ledger.write_remote_state(CONDUCTOR, item_id, work_ledger.RemoteState())
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(remote_ingest, "_store", flaky)
        await _read(peer)
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["progress", "question", "progress"]

    asyncio.run(go())


@pytest.mark.parametrize("code", ["crew_log_unrecorded", "ledger_write_failed"])
@pytest.mark.parametrize("floor_known", [True, False])
def test_an_earlier_report_that_failed_to_write_is_refiled_later(
    recorded, peer, monkeypatch, floor_known, code
):
    real = remote_ingest._file_one
    fails = {"left": 1}

    async def unrecorded_once(*a, **k):
        if fails["left"]:
            # As _file_one does when the write fails or the crew log refuses it.
            fails["left"] -= 1
            return code
        return await real(*a, **k)

    async def go() -> None:
        item_id = await _remote_item(peer)
        if not floor_known:
            # A record from before the per-binding floor: no floor, no cursor.
            work_ledger.write_remote_state(CONDUCTOR, item_id, work_ledger.RemoteState())
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(remote_ingest, "_file_one", unrecorded_once)
        monkeypatch.setattr(remote_ingest, "UNRECORDED_RETRY_SECS", 0)
        await _read(peer)
        assert _worker_statuses(recorded) == ["progress"]
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["progress", "question", "progress"]

    asyncio.run(go())


def test_an_earlier_report_whose_filing_raised_is_refiled_later(recorded, peer, monkeypatch):
    real = work_ledger.snapshot_for_write
    fails = {"left": 1}

    def raises_once(*a, **k):
        if fails["left"]:
            fails["left"] -= 1
            raise PermissionError("event log unreadable")
        return real(*a, **k)

    async def go() -> None:
        await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(work_ledger, "snapshot_for_write", raises_once)
        await _read(peer)
        assert _worker_statuses(recorded) == []
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["question", "progress"]

    asyncio.run(go())


@pytest.mark.parametrize("code", ["crew_log_unrecorded", "ledger_write_failed"])
def test_a_snapshot_that_failed_to_write_is_filed_after_a_newer_report(
    recorded, peer, monkeypatch, code
):
    real = remote_ingest._file_one
    fails = {"left": 1}

    async def fails_once(*a, **k):
        if fails["left"]:
            fails["left"] -= 1
            return code
        return await real(*a, **k)

    async def go() -> None:
        await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        monkeypatch.setattr(remote_ingest, "_file_one", fails_once)
        monkeypatch.setattr(remote_ingest, "UNRECORDED_RETRY_SECS", 0)
        await _read(peer)
        assert _worker_statuses(recorded) == []
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["question", "progress"]

    asyncio.run(go())


def test_a_record_from_before_the_cursor_reads_as_none_and_falls_back_to_the_floor():
    old = work_ledger.RemoteState(report_floor="2026-10-29T10:00:00+00:00", report_floor_known=True)
    raw = old.to_dict()
    raw.pop("report_event_id")
    assert work_ledger.RemoteState.from_dict(raw).report_event_id is None
    assert work_ledger.RemoteState.from_dict({**raw, "report_event_id": 7}).report_event_id is None


@pytest.mark.parametrize("before", [False, True])
def test_a_filed_progress_coalesced_away_in_the_same_second_loses_nothing(recorded, peer, before):
    async def go() -> None:
        await _remote_item(peer)
        stamp = "2026-10-29T10:00:00+00:00"
        if before:
            peer.report("blocked", "need a login", "2026-10-29T09:00:00+00:00")
        peer.report("progress", "p0", stamp)
        await _read(peer)
        # p1 replaces p0 in place on the peer; all in the floor's own second.
        peer.report("progress", "p1", stamp)
        peer.report("question", "which branch?", stamp)
        peer.report("progress", "p2", stamp)
        for _ in range(3):
            await _read(peer)
        summaries = [d["summary"] for _u, d in recorded if d.get("actor") == "worker"]
        assert summaries[-4:] == ["p0", "p1", "which branch?", "p2"]
        assert len(summaries) == (5 if before else 4)

    asyncio.run(go())


def test_a_cursor_that_will_not_store_still_files_the_latest_and_never_replays(
    recorded, peer, monkeypatch
):
    real = remote_ingest._store

    async def go() -> None:
        await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(remote_ingest, "_store", lambda *a: False)
        for _ in range(3):
            await _read(peer)
        # The latest still lands; the question waits for a cursor that stores.
        assert _worker_statuses(recorded) == ["progress", "progress"]
        monkeypatch.setattr(remote_ingest, "_store", real)
        for _ in range(2):
            await _read(peer)
        # Then the question, and the latest again after it so the item ends on it.
        assert _worker_statuses(recorded) == ["progress", "progress", "question", "progress"]
        summaries = [d["summary"] for _u, d in recorded if d.get("actor") == "worker"]
        assert summaries[-1] == "carrying on"

    asyncio.run(go())
