"""Every peer report between two hub pulls is filed, not only the latest.

The peer's ``GET work-item`` carries an event tail; the hub files each report in it
after the newest one the crew log holds as filed (by the peer's own event id), in
order, the newest with the peer item's latest fields. Pinned here:
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
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import test_remote_dispatch_hub_ingest as hub
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
from kiro_crew.dashboard.handlers import remote_ingest, remote_ingest_peer
from kiro_crew.dashboard.handlers import work_ledger as routes_mod

_isolated = helpers._isolated
recorded = hub.recorded


@pytest.fixture(autouse=True)
def _no_background_puller(_floor_monkeypatch):
    """These tests drive the read path's own pull, as the hub ingest tests do."""
    from kiro_crew.dashboard.handlers import remote_puller

    _floor_monkeypatch.setattr(remote_puller, "ensure_running", lambda state: None)
    # Each test closes its retired slots afresh, as test_remote_dispatch_retire does.
    _floor_monkeypatch.setattr(remote_ingest, "_CLOSE_RETRY", {})


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

    async def flaky(state, board, unit, item_id, slot, cleaned, **k):
        if cleaned["status"] == "question":
            return "work_entry_too_large"
        return await real(state, board, unit, item_id, slot, cleaned, **k)

    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(remote_ingest, "_file_one", flaky)
        row = _row(await _read(peer), item_id)
        assert row["remote_error"] == "work_entry_too_large"
        # Passed over, and said: the row counts what never reached the ledger.
        assert row["remote_reports_skipped"] == 1
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
            {"id": "00000000000000a1", "kind": "report", "status": "question", "text": "no stamp"},
            {
                "id": "00000000000000a2",
                "kind": "report",
                "status": ["question"],
                "ts": stamp,
                "text": "list status",
            },
            {
                "id": "00000000000000a3",
                "kind": "report",
                "status": "question",
                "ts": 7,
                "text": "number stamp",
            },
            {
                "id": "00000000000000a4",
                "kind": "report",
                "status": "question",
                "ts": stamp,
                "text": {"a": 1},
            },
            {
                "id": "00000000000000a5",
                "kind": "report",
                "status": "question",
                "ts": "soon",
                "text": "bad stamp",
            },
            {
                "id": "00000000000000a6",
                "kind": "report",
                "status": "finished",
                "ts": stamp,
                "text": "bad status",
            },
            {
                "id": "00000000000000a7",
                "kind": "report",
                "status": "question",
                "ts": stamp,
                "text": "W" * 5000,
            },
            {
                "id": "00000000000000ad",
                "kind": "decision",
                "status": "question",
                "ts": stamp,
                "text": "not a report",
            },
            {
                "id": "00000000000000a8",
                "kind": "report",
                "status": "question",
                "ts": stamp,
                "text": f"key {PLANTED}",
            },
            # After the snapshot's own stamp: filed in log order, stamped no later
            # than the peer item, so the future stamp never reaches the hub.
            {
                "id": "00000000000000a9",
                "kind": "report",
                "status": "blocked",
                "ts": "2099-01-01T00:00:00+00:00",
                "text": "x",
            },
            # Ids that are not work event ids.
            {"id": 5, "kind": "report", "status": "question", "ts": stamp, "text": "int id"},
            {"kind": "report", "status": "question", "ts": stamp, "text": "no id"},
            {"id": "x" * 65, "kind": "report", "status": "question", "ts": stamp, "text": "long"},
        ]
        peer.events = [*peer.events, *hostile]
        peer.work_item.update({"status": "done", "summary": "shipped", "last_report_at": stamp})
        peer.events.append(
            {
                "id": "00000000000000ee",
                "ts": stamp,
                "kind": "report",
                "status": "done",
                "text": "shipped",
            }
        )
        await _read(peer)
        # The overlong text is clamped to the report cap, as a local report's is.
        assert _worker_statuses(recorded) == ["progress", "question", "question", "blocked", "done"]
        assert not [d for _u, d in recorded if "2099" in str(d.get("last_report_at"))]
        summaries = [d["summary"] for _u, d in recorded if d.get("actor") == "worker"]
        assert max(len(s) for s in summaries) == work_ledger.MAX_SUMMARY_CHARS
        assert PLANTED not in json.dumps(recorded, default=str)
        row = _row(await _read(peer), item_id)
        # The future stamp itself, not its year: a hex event id may hold "2099".
        assert PLANTED not in json.dumps(row) and "2099-01-01" not in json.dumps(row)
        # The three report events that fail the report schema (a list status, a dict
        # text, an unknown status) are passed over, and said on the row.
        assert row["remote_reports_skipped"] == 3
        # A log that rolled past the cursor: the whole log is read, every report in
        # it is filed, and what rolled away is counted on the row.
        peer.events = [
            {
                "ts": f"2026-10-29T11:{n // 60:02d}:{n % 60:02d}+00:00",
                "id": f"{n:016x}",
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
        # The fold reads the captured entries, so they are counted from a mark, not cleared.
        mark, calls_mark = len(recorded), len(peer.calls)
        want = [f"r{n}" for n in range(1000 - work_ledger.MAX_EVENTS_PER_ITEM, 1000)]
        row, filed = await _read_until_filed(peer, recorded, mark, item_id, len(want))
        assert filed == want
        # The roll past the cursor adds one.
        assert row["remote_reports_skipped"] == 4
        limits = [
            c["params"]["limit"]
            for c in peer.calls[calls_mark:]
            if c["path"].endswith("/work-item")
        ]
        assert limits[:2] == ["50", str(work_ledger.MAX_EVENTS_PER_ITEM)]
        mark = len(recorded)
        row = _row(await _read(peer), item_id)
        assert recorded[mark:] == [] and row["remote_reports_skipped"] == 4

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
        assert _cursor(item_id) == peer.events[1]["id"]

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
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(remote_ingest, "_file_one", cut)

        task = asyncio.ensure_future(_read(peer))
        reader.append(task)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls["n"] == 1
        # A cut-off pull writes no snapshot: the item stands at the one report filed.
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.status == "blocked" and _cursor(item_id) == peer.events[0]["id"]
        monkeypatch.setattr(remote_ingest, "_file_one", real)
        await _read(peer)
        await _read(peer)

    asyncio.run(go())
    assert _worker_statuses(recorded) == ["blocked", "question", "progress"]


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
        # The pull stops at the question: no snapshot runs ahead of it.
        assert _worker_statuses(recorded) == []
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["question", "progress"]

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


async def _read_until_filed(
    peer, recorded, mark: int, item_id: str, count: int
) -> tuple[dict[str, Any], list[str]]:
    """Read until *count* worker reports are filed after *mark*, as a slow host's
    pulls do when the per-item timeout cuts one off: each next pull carries on
    from the folded cursor. Bounded; the caller asserts what was filed."""
    filed: list[str] = []
    row: dict[str, Any] = {}
    for _ in range(20):
        row = _row(await _read(peer), item_id)
        filed = [d["summary"] for _u, d in recorded[mark:] if d.get("actor") == "worker"]
        if len(filed) >= count:
            break
    return row, filed


def _cursor(item_id: str) -> str | None:
    """The pull cursor: the newest peer event id the crew log holds as filed."""
    ids = remote_ingest_peer.read_folded_peer(CONDUCTOR, item_id).ids
    return ids[-1] if ids else None


def _folded_report_statuses(recorded, item_id: str) -> list[str | None]:
    events = hub.fold_recorded(recorded)["items"][item_id]["events"]
    return [e["status"] for e in events if e["kind"] == "report"]


def test_a_kill_between_two_filings_then_a_repull_files_each_report_once_in_order(
    recorded, peer, monkeypatch
):
    real = work_ledger.apply_remote_report
    calls = {"n": 0}

    class Killed(Exception):
        pass

    def killed_after_first(*a, **k):
        calls["n"] += 1
        result = real(*a, **k)
        if calls["n"] == 1:
            # The process dies right after the first report's ledger write.
            raise Killed
        return result

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:02:00+00:00")
        monkeypatch.setattr(work_ledger, "apply_remote_report", killed_after_first)
        await _read(peer)
        monkeypatch.setattr(work_ledger, "apply_remote_report", real)
        await _read(peer)
        await _read(peer)
        # The crew log is the record: each report once, in order.
        assert _folded_report_statuses(recorded, item_id) == ["blocked", "question", "progress"]
        item = work_ledger.read_work_item(CONDUCTOR, item_id)
        assert item.status == "progress" and item.summary == "carrying on"

    asyncio.run(go())


def test_more_than_six_reports_between_two_pulls_all_reach_the_ledger(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        for n in range(10):
            status = "question" if n % 2 else "blocked"
            peer.report(status, f"r{n}", f"2026-10-29T10:{n:02d}:00+00:00")
        row = _row(await _read(peer), item_id)
        await _read(peer)
        summaries = [d["summary"] for _u, d in recorded if d.get("actor") == "worker"]
        assert summaries == [f"r{n}" for n in range(10)]
        assert row["remote_reports_skipped"] == 0

    asyncio.run(go())


def test_a_retired_binding_clears_the_cursor_and_its_reports_are_never_pulled(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        assert _cursor(item_id)
        work_ledger.apply_remote_retire(
            CONDUCTOR,
            item_id,
            worker_session_key=hub.PEER_SLOT,
            worker_instance_id=hub.CREW,
            reason="crew_unreachable",
        )
        # The conductor's retire route records it; the store call above does not.
        conductor = next(d for _u, d in recorded if d.get("actor") == "conductor")
        recorded.append(
            (
                "unit:conductor",
                {
                    "actor": "conductor",
                    "action": "retire",
                    "slot": conductor["slot"],
                    "generation": conductor.get("generation"),
                    "item_id": item_id,
                    "worker_session_key": "",
                    "worker_instance_id": "",
                },
            )
        )
        assert _cursor(item_id) is None
        before = len(work_ledger.read_events(CONDUCTOR, item_id))
        peer.report("done", "shipped", "2026-10-29T10:05:00+00:00")
        await _read(peer)
        # The read also closes the retired peer slot in a background task, which
        # records ``destroy``; wait for it so the count below does not race it.
        if remote_ingest._CLOSE_TASKS:
            await asyncio.gather(*list(remote_ingest._CLOSE_TASKS))
        added = [e.kind for e in work_ledger.read_events(CONDUCTOR, item_id)[before:]]
        assert added == ["destroy"]

    asyncio.run(go())


def test_a_first_pull_past_the_short_tail_files_every_report(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        count = remote_ingest.PULL_EVENT_LIMIT + 5
        peer.events = [
            {
                "ts": f"2026-10-29T11:{n // 60:02d}:{n % 60:02d}+00:00",
                "id": f"{n + 0x10000:016x}",
                "kind": "report",
                "status": "question" if n % 2 else "blocked",
                "text": f"r{n}",
            }
            for n in range(count)
        ]
        last = peer.events[-1]
        peer.work_item.update(
            {"status": last["status"], "summary": last["text"], "last_report_at": last["ts"]}
        )
        row, filed = await _read_until_filed(peer, recorded, 0, item_id, count)
        assert filed == [f"r{n}" for n in range(count)]
        assert row["remote_reports_skipped"] == 0

    asyncio.run(go())


def test_an_unhashable_event_id_never_fails_the_pull(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        filler = [
            {"id": {"a": 1}, "kind": "decision", "ts": "2026-10-29T10:01:00+00:00", "text": "x"}
            for _ in range(remote_ingest.PULL_EVENT_LIMIT)
        ]
        peer.events = [*peer.events, *filler]
        peer.report("done", "shipped", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        assert work_ledger.read_work_item(CONDUCTOR, item_id).status == "done"

    asyncio.run(go())


def test_where_ingest_stands_is_folded_from_the_crew_log_alone(recorded, peer, monkeypatch):
    """A report whose item write landed but whose crew-log entry did not (a kill,
    or a refused append) leaves the cursor before it: the next pull files it."""
    real = routes_mod.crew_log_emit.on_work_recorded
    drops = {"left": 1}

    class Killed(Exception):
        pass

    def lost_once(unit, data):
        if data.get("actor") == "worker" and drops["left"]:
            drops["left"] -= 1
            raise Killed("the gateway died before the crew-log append")
        return real(unit, data)

    # Two fixed hub filing stamps, five seconds apart: the refile is filed at
    # another time than the lost write, with no wait on the wall clock.
    first = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
    clock = {"now": first.isoformat()}
    monkeypatch.setattr(work_ledger, "_now_iso", lambda: clock["now"])

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:01:00+00:00")
        monkeypatch.setattr(routes_mod.crew_log_emit, "on_work_recorded", lost_once)
        await _read(peer)
        # The item cache holds the question; the crew log does not.
        assert work_ledger.read_work_item(CONDUCTOR, item_id).status == "question"
        assert _cursor(item_id) is None
        monkeypatch.setattr(routes_mod.crew_log_emit, "on_work_recorded", real)
        # The refile is filed at a later stamp: its hub event id must not move with it.
        clock["now"] = (first + timedelta(seconds=5)).isoformat()
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["question", "progress"]
        cached = [
            e.status for e in work_ledger.read_events(CONDUCTOR, item_id) if e.kind == "report"
        ]
        assert cached == ["question", "progress"]
        # The cache holds nothing the record lacks, so a rebuild is not refused.
        state = hub.fold_recorded(recorded)
        fold_items = [
            {**state["items"][key], "events": state["items"][key]["events"]}
            for key in state["order"]
        ]
        work_ledger._refuse_fold_behind_cache(CONDUCTOR, [item_id], fold_items)

    asyncio.run(go())


def test_a_peer_event_filed_twice_folds_once(recorded, peer):
    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:01:00+00:00")
        await _read(peer)
        first = next(d for _u, d in recorded if d.get("peer_event_id"))
        recorded.append(("unit:replay", dict(first)))
        assert _folded_report_statuses(recorded, item_id) == ["question", "progress"]

    asyncio.run(go())


@pytest.mark.parametrize("where", ["apply", "preflight"])
def test_a_transient_item_read_error_leaves_the_report_for_the_next_pull(
    recorded, peer, monkeypatch, where
):
    """An I/O fault reading the item, in the ingest's own read or in the write's,
    files nothing, skips nothing and moves no floor: the next pull files both."""
    real_read = work_ledger.read_work_item
    fault = {"armed": False, "left": 1}

    def flaky(slot_key, item_id, *, strict=False):
        if fault["armed"] and fault["left"]:
            # A strict read raises the fault; a lenient one reads it as no item.
            fault["left"] -= 1
            if strict:
                raise OSError(5, "Input/output error")
            return None
        return real_read(slot_key, item_id, strict=strict)

    if where == "apply":
        real_apply = work_ledger.apply_remote_report

        def armed_apply(*a, **k):
            fault["armed"] = True
            try:
                return real_apply(*a, **k)
            finally:
                fault["armed"] = False

        monkeypatch.setattr(work_ledger, "apply_remote_report", armed_apply)
    else:
        real_dirty = remote_ingest._refuse_if_dirty

        async def armed_dirty(*a, **k):
            # The ingest's own item read comes right after this check.
            result = await real_dirty(*a, **k)
            fault["armed"] = True
            return result

        monkeypatch.setattr(remote_ingest, "_refuse_if_dirty", armed_dirty)

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:01:00+00:00")
        monkeypatch.setattr(work_ledger, "read_work_item", flaky)
        await _read(peer)
        # Not passed over: nothing filed and nothing counted as skipped.
        assert _worker_statuses(recorded) == []
        fault["armed"] = False
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["question", "progress"]
        assert remote_ingest_peer.read_folded_peer(CONDUCTOR, item_id).skipped == 0

    asyncio.run(go())


def test_each_filed_report_keeps_the_peer_event_id_verbatim(recorded, peer):
    """The crew-log entry and the hub event both carry the peer's own id: nothing is
    derived from a cursor, a stamp, a position or a salt."""

    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:01:00+00:00")
        await _read(peer)
        return item_id

    item_id = asyncio.run(go())
    peer_ids = [e["id"] for e in peer.events]
    filed = [d for _u, d in recorded if d.get("actor") == "worker"]
    assert [d["peer_event_id"] for d in filed] == peer_ids
    assert [d["event_id"] for d in filed] == peer_ids
    hub_ids = [e.id for e in work_ledger.read_events(CONDUCTOR, item_id) if e.kind == "report"]
    assert hub_ids == peer_ids


def test_a_cursor_coalesced_away_in_a_full_log_loses_no_question(recorded, peer):
    """The filed progress coalesces into the next one on a peer whose log is at its
    cap: the question after it still lands, and the possible roll is said once."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        filler = [
            {
                "id": f"{n + 0x20000:016x}",
                "ts": "2026-10-29T09:00:00+00:00",
                "kind": "decision",
                "status": None,
                "text": f"d{n}",
            }
            for n in range(work_ledger.MAX_EVENTS_PER_ITEM - 1)
        ]
        peer.events = filler
        peer.report("progress", "p0", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        assert _cursor(item_id) == peer.events[-1]["id"]
        stamp = "2026-10-29T10:01:00+00:00"
        peer.report("progress", "p1", stamp)
        peer.report("question", "which branch?", stamp)
        peer.report("progress", "p2", stamp)
        # The peer keeps its cap: the oldest events roll off.
        peer.events = peer.events[-work_ledger.MAX_EVENTS_PER_ITEM :]
        assert len(peer.events) == work_ledger.MAX_EVENTS_PER_ITEM
        await _read(peer)
        row = _row(await _read(peer), item_id)
        assert _worker_statuses(recorded) == ["progress", "progress", "question", "progress"]
        assert row["status"] == "progress" and row["summary"] == "p2"
        assert row["remote_reports_skipped"] == 1

    asyncio.run(go())


def test_a_failed_page_read_files_nothing_and_the_next_pull_files_all(recorded, peer):
    """A transient peer error on the whole-log read leaves where ingest stands as it
    was; the next pull pages again and files every report once."""
    fail = {"left": 1}

    async def go() -> None:
        item_id = await _remote_item(peer)
        count = remote_ingest.PULL_EVENT_LIMIT + 3
        for n in range(count):
            status = "question" if n % 2 else "blocked"
            peer.report(status, f"r{n}", f"2026-10-29T10:{n:02d}:00+00:00")
        from kiro_crew.instances.ssh_tunnel_manager import ProxyRequestError

        real_request = peer.proxy_request

        def request(instance_id, method, path, *, params=None, data=None, content_type=""):
            limit = (params or {}).get("limit")
            if limit == str(work_ledger.MAX_EVENTS_PER_ITEM) and fail["left"]:
                fail["left"] -= 1
                raise ProxyRequestError("proxy_peer_not_connected", "blip", http_status=503)
            return real_request(
                instance_id, method, path, params=params, data=data, content_type=content_type
            )

        peer.proxy_request = request  # type: ignore[method-assign]
        row = _row(await _read(peer), item_id)
        assert _worker_statuses(recorded) == [] and row["remote_error"]
        assert _cursor(item_id) is None
        await _read(peer)
        await _read(peer)
        filed = [d["summary"] for _u, d in recorded if d.get("actor") == "worker"]
        assert filed == [f"r{n}" for n in range(count)]

    asyncio.run(go())


def test_a_binding_filed_before_peer_ids_files_only_its_newest_report(recorded, peer):
    """A binding with a floor but no peer id filed (reports filed before this change)
    files only the newest report, as the hub did then, and never the old tail."""

    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("blocked", "old one", "2026-10-29T10:00:00+00:00")
        peer.report("question", "old two", "2026-10-29T10:01:00+00:00")
        record = work_ledger.read_remote_state(CONDUCTOR, item_id) or work_ledger.RemoteState()
        record.report_floor, record.report_floor_known = "2026-10-29T10:01:00+00:00", True
        work_ledger.write_remote_state(CONDUCTOR, item_id, record)
        peer.report("progress", "new", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        await _read(peer)
        peer.report("done", "shipped", "2026-10-29T10:03:00+00:00")
        await _read(peer)
        return item_id

    asyncio.run(go())
    assert [d["summary"] for _u, d in recorded if d.get("actor") == "worker"] == ["new", "shipped"]


def test_a_dirty_cache_rebuild_rederives_the_cursor_and_the_refused_report_files(peer, monkeypatch):
    """A question whose crew-log append was refused, then a rebuild from the crew
    log: the cursor is folded again from the log, which never held the question, so
    the next pull files it, once, and no report twice."""
    from kiro_crew import crew_log as lg
    from kiro_crew.crew_log import CrewLog, projection, slot_prime

    monkeypatch.setattr(routes_mod.crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(routes_mod, "unit_for_session_key", lambda sessions, key: "u-conductor")
    CrewLog.create(
        lg.KIND_SESSION, "u-conductor", owner="raymond", agent="kirocrew", slot=CONDUCTOR
    )
    refuse = {"on": False}

    def _append(unit: str, data: dict[str, Any]) -> bool:
        if refuse["on"] and data.get("actor") == "worker":
            return False
        projection.open_session_log(unit).append("work/recorded", data, src="gateway")
        return True

    monkeypatch.setattr(routes_mod.crew_log_emit, "on_work_recorded", _append)

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        assert await asyncio.to_thread(slot_prime.drain, 30.0), "a background prime never finished"
        await _read(peer)
        assert _cursor(item_id) == peer.events[-1]["id"]
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        refuse["on"] = True
        await _read(peer)
        refuse["on"] = False
        resp = await routes_mod.api_work_ledger_rebuild(
            hub._req("POST", "/api/work-ledger/rebuild", peer, body={})
        )
        assert resp.status == 200, resp.text
        # The record holds no question, so neither does the folded cursor.
        assert _cursor(item_id) == peer.events[0]["id"]
        # Out of the crew log's unrecorded window, the next pull files the question.
        record = work_ledger.read_remote_state(CONDUCTOR, item_id)
        record.unrecorded_key = record.unrecorded_at = None
        work_ledger.write_remote_state(CONDUCTOR, item_id, record)
        await _read(peer)
        await _read(peer)
        statuses = [
            e.status for e in work_ledger.read_events(CONDUCTOR, item_id) if e.kind == "report"
        ]
        assert statuses == ["progress", "question"]
        assert _cursor(item_id) == peer.events[-1]["id"]

    asyncio.run(go())


def test_a_binding_from_before_peer_ids_never_replays_its_history_after_a_coalesce(recorded, peer):
    """Its only filed id is a progress the peer then coalesces away: the reports from
    before it (a question, a done) are not taken for news."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("question", "old question", "2026-10-29T10:00:00+00:00")
        peer.report("done", "old done", "2026-10-29T10:01:00+00:00")
        peer.report("progress", "old progress", "2026-10-29T10:02:00+00:00")
        record = work_ledger.read_remote_state(CONDUCTOR, item_id) or work_ledger.RemoteState()
        record.report_floor, record.report_floor_known = "2026-10-29T10:02:00+00:00", True
        work_ledger.write_remote_state(CONDUCTOR, item_id, record)
        peer.report("progress", "p1", "2026-10-29T10:03:00+00:00")
        await _read(peer)
        assert _cursor(item_id) == peer.events[-1]["id"]
        peer.report("progress", "p2", "2026-10-29T10:04:00+00:00")
        await _read(peer)
        await _read(peer)
        assert [d["summary"] for _u, d in recorded if d.get("actor") == "worker"] == ["p1", "p2"]

    asyncio.run(go())


def test_a_fold_unread_at_paging_time_files_nothing_from_the_short_tail(
    recorded, peer, monkeypatch
):
    """The paging choice could not read the fold, the filing could: the short tail
    may stop before where ingest stands, so it files nothing and the next pull pages."""
    real = remote_ingest._filed_ids_hint
    cold = {"left": 1}

    async def hint(board, item_id):
        if cold["left"]:
            cold["left"] -= 1
            return None
        return await real(board, item_id)

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T09:00:00+00:00")
        await _read(peer)
        count = remote_ingest.PULL_EVENT_LIMIT + 5
        for n in range(count):
            status = "question" if n % 2 else "blocked"
            peer.report(status, f"r{n}", f"2026-10-29T10:{n:02d}:00+00:00")
        monkeypatch.setattr(remote_ingest, "_filed_ids_hint", hint)
        mark = len(recorded)
        row = _row(await _read(peer), item_id)
        assert recorded[mark:] == []
        assert row["remote_error"] == remote_ingest_peer.CODE_FOLD_UNREADABLE
        await _read(peer)
        await _read(peer)
        filed = [d["summary"] for _u, d in recorded[mark:] if d.get("actor") == "worker"]
        assert filed == [f"r{n}" for n in range(count)]

    asyncio.run(go())


def test_a_report_passed_over_is_counted_once_when_only_the_latest_fields_file(recorded, peer):
    """The newest event cannot file but the peer item's fields can: those file alone,
    and the passed-over report is counted once, by the next peer-id filing."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        stamp = "2026-10-29T10:01:00+00:00"
        peer.work_item.update(
            {"status": "blocked", "summary": "need a login", "last_report_at": stamp}
        )
        peer.events.append(
            {
                "id": "00000000000000b1",
                "ts": stamp,
                "kind": "report",
                "status": "blocked",
                "text": {"x": 1},
            }
        )
        await _read(peer)
        await _read(peer)
        peer.report("done", "shipped", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        row = _row(await _read(peer), item_id)
        assert _worker_statuses(recorded) == ["progress", "blocked", "done"]
        assert row["remote_reports_skipped"] == 1

    asyncio.run(go())


def test_a_fold_unread_at_paging_time_files_no_latest_fields_from_a_reportless_tail(
    recorded, peer, monkeypatch
):
    """A first pull whose short tail holds no report at all (only decisions): the
    latest fields are not filed alone either, so the next, paged pull still files
    every earlier report."""
    real = remote_ingest._filed_ids_hint
    cold = {"left": 1}

    async def hint(board, item_id):
        if cold["left"]:
            cold["left"] -= 1
            return None
        return await real(board, item_id)

    async def go() -> None:
        await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        peer.events.extend(
            {
                "id": f"{n + 0x30000:016x}",
                "ts": "2026-10-29T10:02:00+00:00",
                "kind": "decision",
                "status": None,
                "text": f"d{n}",
            }
            for n in range(remote_ingest.PULL_EVENT_LIMIT)
        )
        monkeypatch.setattr(remote_ingest, "_filed_ids_hint", hint)
        await _read(peer)
        assert _worker_statuses(recorded) == []
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["blocked", "question"]

    asyncio.run(go())


def test_a_peer_clock_that_steps_back_loses_no_report(recorded, peer):
    """The peer files a question, its clock steps back, then a progress: both land,
    in the peer log's order, whatever their stamps say."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:02:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:01:00+00:00")
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["question", "progress"]
        assert _cursor(item_id) == peer.events[-1]["id"]

    asyncio.run(go())


def test_a_binding_from_before_peer_ids_never_replays_a_report_from_the_same_second(recorded, peer):
    """The question filed before the upgrade shares its second with the first id
    filing, a progress the peer later coalesces away: the question is never news."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        stamp = "2026-10-29T10:00:00+00:00"
        peer.report("question", "old question", stamp)
        record = work_ledger.read_remote_state(CONDUCTOR, item_id) or work_ledger.RemoteState()
        record.report_floor, record.report_floor_known = stamp, True
        work_ledger.write_remote_state(CONDUCTOR, item_id, record)
        peer.report("progress", "p1", stamp)
        await _read(peer)
        peer.report("progress", "p2", "2026-10-29T10:01:00+00:00")
        await _read(peer)
        await _read(peer)
        assert [d["summary"] for _u, d in recorded if d.get("actor") == "worker"] == ["p1", "p2"]
        first = next(d for _u, d in recorded if d.get("actor") == "worker")
        assert first["peer_seen"] == [peer.events[0]["id"]]

    asyncio.run(go())


def test_a_clock_step_back_after_a_filing_still_files_the_new_reports(recorded, peer):
    """The peer's newest stamp is older than the one the hub last filed: the new
    reports are picked by id, so the stamp floor hides neither."""

    async def go() -> None:
        await _remote_item(peer)
        peer.report("progress", "started", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        peer.report("question", "which branch?", "2026-10-29T10:03:00+00:00")
        peer.report("progress", "carrying on", "2026-10-29T10:01:00+00:00")
        await _read(peer)
        await _read(peer)
        assert _worker_statuses(recorded) == ["progress", "question", "progress"]

    asyncio.run(go())


def test_a_report_that_can_never_file_is_counted_once_across_coalesces(recorded, peer):
    """A malformed report between two filed ones: its id is recorded as seen, so a
    later coalesce of the progress after it never counts it again."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        peer.events.append(
            {
                "id": "00000000000000c1",
                "ts": "2026-10-29T10:01:00+00:00",
                "kind": "report",
                "status": "finished",
                "text": "bad status",
            }
        )
        peer.report("progress", "p1", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        peer.report("progress", "p2", "2026-10-29T10:03:00+00:00")
        await _read(peer)
        peer.report("progress", "p3", "2026-10-29T10:04:00+00:00")
        await _read(peer)
        row = _row(await _read(peer), item_id)
        assert [d["summary"] for _u, d in recorded if d.get("actor") == "worker"] == [
            "need a login",
            "p1",
            "p2",
            "p3",
        ]
        assert row["remote_reports_skipped"] == 1

    asyncio.run(go())


def test_progress_churn_never_evicts_a_filed_question(recorded, peer):
    """A question, then more progress reports than the fold's id window, each on its
    own pull: the peer coalesces them and keeps the question, and so does the fold,
    so the question is never filed again."""

    async def go() -> None:
        item_id = await _remote_item(peer)
        peer.report("question", "which branch?", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        start = datetime(2026, 10, 29, 11, 0, tzinfo=timezone.utc)
        for n in range(work_ledger.MAX_EVENTS_PER_ITEM + 2):
            peer.report("progress", f"p{n}", (start + timedelta(seconds=n)).isoformat())
            await _read(peer)
        assert [e["status"] for e in peer.events] == ["question", "progress"]
        await _read(peer)
        assert _worker_statuses(recorded).count("question") == 1
        assert _cursor(item_id) == peer.events[-1]["id"]

    asyncio.run(go())
