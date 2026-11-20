"""One batch pull per crew per tick (remote-dispatch W3a).

Pinned here:

* the hub reads every item a crew holds in ONE ``POST api/chat/work-items/pull``
  and files each item's reports, with no per-item read and no slot list;
* a tick with nothing new gets rows with no copy, and files nothing;
* a crew without the route (a plain 404, or a reply that is not a batch) is read
  item by item as before;
* the peer route itself: the cursor's news only, the whole log when the cursor is
  not in it, a changed worker-field digest sends the copy again;
* rows a crew answered are filed before any per-item read.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
import test_remote_dispatch_hub_ingest as hub
import work_ledger_projection_helpers as helpers
from test_remote_dispatch_hub_ingest import (
    CREW,
    PEER_SLOT,
    StubCrew,
    _Content,
    _read,
    _remote_item,
    _state,
)
from work_ledger_projection_helpers import ACCEPTANCE, CONDUCTOR, _record

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import (
    remote_batch_pull,
    remote_ingest,
    remote_ingest_peer,
    remote_work_items_pull,
)

_isolated = helpers._isolated
recorded = hub.recorded


@pytest.fixture(autouse=True)
def _quiet(_floor_monkeypatch):
    from kiro_crew.dashboard.handlers import remote_puller

    _floor_monkeypatch.setattr(remote_puller, "ensure_running", lambda state: None)
    _floor_monkeypatch.setattr(remote_ingest, "_CLOSE_RETRY", {})


class BatchCrew(StubCrew):
    """A crew that serves the batch route over its copies, with the peer route's
    own cursor rule (``remote_work_items_pull._since``)."""

    def __init__(self) -> None:
        super().__init__()
        #: item id -> {"slot", "work_item", "events"}; the base stub's single copy
        #: is the one at PEER_SLOT.
        self.copies: dict[str, dict[str, Any]] = {}
        self.batch = True
        #: With ``batch`` off: answer the route with aiohttp's plain 404 (an older
        #: crew), else with the base stub's ``{"ok": true}`` (not a batch reply).
        self.batch_404 = True
        self.replies: list[Any] = []

    def copy(self, item_id: str, slot: str) -> dict[str, Any]:
        found = self.copies.setdefault(
            item_id,
            {
                "slot": slot,
                "work_item": {"item_id": item_id, "state": "open", "status": None},
                "events": [],
            },
        )
        return found

    def _rows(self, body: dict[str, Any]) -> dict[str, Any]:
        live = {s["key"]: s for s in self.slots}
        items: dict[str, Any] = {}
        for item_id, hint in body["items"].items():
            copy = self.copies.get(item_id)
            if copy is None and self.work_item.get("item_id") == item_id:
                copy = {"slot": PEER_SLOT, "work_item": self.work_item, "events": self.events}
            slot = live.get(hint["slot"])
            row: dict[str, Any] = {
                "live": slot is not None,
                "running": bool(slot and slot.get("running")),
                "pending_approval": False,
                "trust_active": bool(self.trust_active),
                "found": copy is not None and copy["slot"] == hint["slot"],
            }
            if row["found"]:
                assert copy is not None
                events = [SimpleNamespace(kind=e["kind"], id=e["id"]) for e in copy["events"]]
                after = tuple(hint.get("after", []))
                tail, newest = remote_work_items_pull._since(events, after)
                unchanged = (
                    copy["work_item"].get("last_report_at") == hint.get("stamp")
                    and (newest is None or newest in after)
                    and hint.get("digest") == remote_ingest_peer.worker_digest(copy["work_item"])
                )
                if not unchanged:
                    start = len(events) - len(tail)
                    row["change"] = {
                        "work_item": copy["work_item"],
                        "events": copy["events"][start:],
                    }
            items[item_id] = row
        return {"items": items}

    def _answer(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        if method == "POST" and path == remote_batch_pull.BATCH_PATH and self.batch:
            reply = self._rows(body)
            self.replies.append(reply)
            return 200, reply
        return super()._answer(method, path, body)

    @asynccontextmanager
    async def proxy_request(
        self, instance_id, method, path, *, params=None, data=None, content_type=""
    ):
        if (
            method == "POST"
            and path == remote_batch_pull.BATCH_PATH
            and not self.batch
            and self.batch_404
        ):
            # An older crew: aiohttp's plain-text 404.
            self.calls.append({"method": method, "path": path, "params": params, "body": None})
            yield SimpleNamespace(
                status=404, headers={"Content-Type": "text/plain"}, content=_Content(b"404")
            )
            return
        async with super().proxy_request(
            instance_id, method, path, params=params, data=data, content_type=content_type
        ) as upstream:
            yield upstream


@pytest.fixture
def peer(monkeypatch) -> BatchCrew:
    monkeypatch.setattr("kiro_crew.remote_dispatch.remote_dispatch_enabled", lambda: True)
    return BatchCrew()


def _calls(peer: StubCrew, mark: int) -> list[tuple[str, str]]:
    return [(c["method"], c["path"]) for c in peer.calls[mark:]]


def _worker_summaries(recorded) -> list[str]:
    return [d["summary"] for _u, d in recorded if d.get("actor") == "worker"]


async def _bind_more(peer: BatchCrew, n: int) -> list[str]:
    """*n* more items bound to their own slots on the same crew."""
    ids = []
    for i in range(n):
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": f"more {i}", "acceptance": ACCEPTANCE}
        )
        assert status == 200, body
        item_id = body["item"]["item_id"]
        slot = f"chat-{60 + i}-1793000000"
        work_ledger.apply_remote_bind(
            CONDUCTOR, item_id, worker_session_key=slot, worker_instance_id=CREW
        )
        peer.copy(item_id, slot)
        peer.slots.append({"key": slot, "running": False})
        ids.append(item_id)
    return ids


def _report(copy: dict[str, Any], status: str, summary: str, ts: str) -> None:
    copy["work_item"].update({"status": status, "summary": summary, "last_report_at": ts})
    copy["events"].append(
        {
            "id": work_ledger.event_id(ts, copy["work_item"]["item_id"], "report", summary),
            "ts": ts,
            "kind": "report",
            "status": status,
            "text": summary,
            "item_id": copy["work_item"]["item_id"],
        }
    )


# ── W3a: one call per crew per tick ──────────────────────────────────────


def test_one_batch_call_per_crew_files_every_items_reports(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        more = await _bind_more(peer, 2)
        peer.report("progress", "first", "2026-10-29T10:00:00+00:00")
        for n, item_id in enumerate(more):
            _report(peer.copies[item_id], "question", f"other {n}", "2026-10-29T10:01:00+00:00")
        mark = len(peer.calls)
        await _read(peer)
        # One call for the crew: no slot list, no per-item read.
        assert _calls(peer, mark) == [("POST", remote_batch_pull.BATCH_PATH)]
        assert sorted(_worker_summaries(recorded)) == ["first", "other 0", "other 1"]
        for item_id in more:
            record = work_ledger.read_remote_state(CONDUCTOR, item_id)
            assert record is not None and record.roster_read and record.error is None

    asyncio.run(go())


def test_a_tick_with_nothing_new_gets_no_copy_and_files_nothing(recorded, peer):
    async def go() -> None:
        await _remote_item(peer)
        peer.report("progress", "first", "2026-10-29T10:00:00+00:00")
        peer.report("question", "which branch?", "2026-10-29T10:01:00+00:00")
        await _read(peer)
        filed = len(recorded)
        await _read(peer)
        assert len(recorded) == filed
        (row,) = peer.replies[-1]["items"].values()
        assert row["found"] and "change" not in row
        # New news after that: only it arrives, from the cursor on.
        peer.report("done", "shipped", "2026-10-29T10:02:00+00:00")
        await _read(peer)
        (row,) = peer.replies[-1]["items"].values()
        assert [e["text"] for e in row["change"]["events"]] == ["which branch?", "shipped"]
        assert _worker_summaries(recorded) == ["first", "which branch?", "shipped"]

    asyncio.run(go())


@pytest.mark.parametrize("old", ["plain_404", "not_a_batch"])
def test_a_crew_without_the_batch_route_is_read_item_by_item(recorded, peer, old):
    async def go() -> None:
        await _remote_item(peer)
        peer.batch = False
        # The base stub answers an unknown POST with ``{"ok": true}``.
        peer.batch_404 = old == "plain_404"
        peer.report("progress", "first", "2026-10-29T10:00:00+00:00")
        mark = len(peer.calls)
        await _read(peer)
        calls = _calls(peer, mark)
        assert calls[0] == ("POST", remote_batch_pull.BATCH_PATH)
        assert ("GET", "api/chat/slots") in calls
        assert ("GET", f"api/chat/slots/{PEER_SLOT}/work-item") in calls
        assert _worker_summaries(recorded) == ["first"]

    asyncio.run(go())


# ── the peer route ────────────────────────────────────────────────────────


def test_since_answers_news_after_the_cursor_or_the_whole_log():
    ev = [
        SimpleNamespace(kind=k, id=i)
        for k, i in [("create", "c"), ("report", "a"), ("report", "b")]
    ]
    assert remote_work_items_pull._since(ev, ("a",)) == (ev[1:], "b")
    assert remote_work_items_pull._since(ev, ("b",)) == (ev[2:], "b")
    assert remote_work_items_pull._since(ev, ("zz",)) == (ev, "b")
    assert remote_work_items_pull._since(ev[:1], ()) == (ev[:1], None)


def test_the_peer_route_reads_a_real_mailbox(monkeypatch):
    from kiro_crew import remote_dispatch_mailbox as mailbox

    hub_key = "chat-hub-conductor"
    named = mailbox.parse_work_item(
        {"conductor_key": hub_key, "item_id": "it_0000aaaa", "title": "a", "acceptance": ACCEPTANCE}
    )
    mailbox.open_mailbox(named, "chat-1-1793000000")
    board = mailbox.mailbox_key(hub_key)
    state = SimpleNamespace(_slots={})
    monkeypatch.setattr(remote_work_items_pull, "remote_trust_active", lambda slot: False)

    hint = ("it_0000aaaa", "chat-1-1793000000", (), None, remote_ingest_peer.worker_digest({}))
    doc = remote_work_items_pull._read(board, [hint], remote_work_items_pull._flags(state, [hint]))
    row = doc["items"]["it_0000aaaa"]
    # Nothing reported: no news, and the slot is gone here.
    assert row["found"] and not row["live"] and "change" not in row

    work_ledger.apply_worker_report(board, "it_0000aaaa", status="progress", summary="go")
    doc = remote_work_items_pull._read(board, [hint], remote_work_items_pull._flags(state, [hint]))
    change = doc["items"]["it_0000aaaa"]["change"]
    assert change["work_item"]["summary"] == "go"
    report = [e for e in change["events"] if e["kind"] == "report"]
    stamp = change["work_item"]["last_report_at"]
    digest = remote_ingest_peer.worker_digest(change["work_item"])
    filed = ("it_0000aaaa", "chat-1-1793000000", (report[-1]["id"],), stamp, digest)
    assert (
        "change"
        not in remote_work_items_pull._read(
            board, [filed], remote_work_items_pull._flags(state, [filed])
        )["items"]["it_0000aaaa"]
    )
    # Same stamp and event id, another digest (a same-second repeat with new
    # fields): the copy is sent again.
    stale = (*filed[:4], "0" * 32)
    assert (
        "change"
        in remote_work_items_pull._read(
            board, [stale], remote_work_items_pull._flags(state, [stale])
        )["items"]["it_0000aaaa"]
    )
    # A slot the copy does not name reads as not found.
    wrong = ("it_0000aaaa", "chat-9-1793000000", (), None, None)
    assert not remote_work_items_pull._read(
        board, [wrong], remote_work_items_pull._flags(state, [wrong])
    )["items"]["it_0000aaaa"]["found"]


def test_a_same_second_repeat_with_new_fields_still_reaches_the_hub(recorded, peer):
    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("blocked", "need a login", "2026-10-29T10:00:00+00:00")
        await _read(peer)
        # Same stamp, status and summary (same event id), new reason.
        peer.work_item["reason"] = "needs_human"
        await _read(peer)
        return item_id

    item_id = asyncio.run(go())
    assert work_ledger.read_work_item(CONDUCTOR, item_id).reason == "needs_human"


def test_answered_rows_are_filed_before_any_per_item_read(recorded, peer, monkeypatch):
    """A crew read item by item (no batch, or stalled) may spend the read's
    deadline; rows another crew already answered need no peer call, so they are
    filed first and never wait behind it."""
    order: list[str] = []
    real_pull, real_batched = remote_batch_pull.pull_crew, remote_batch_pull.ingest_batched

    async def pull(mgr, board, crew, items):
        # The other crew serves no batch route.
        return None if crew != CREW else await real_pull(mgr, board, crew, items)

    async def batched(state, mgr, board, item, row):
        order.append(f"batched:{item.worker_instance_id}")
        await real_batched(state, mgr, board, item, row)

    async def per_item(state, mgr, board, item, slot_lists):
        order.append(f"per_item:{item.worker_instance_id}")

    monkeypatch.setattr(remote_batch_pull, "pull_crew", pull)
    monkeypatch.setattr(remote_batch_pull, "ingest_batched", batched)
    monkeypatch.setattr(remote_ingest, "_ingest_item", per_item)

    async def go() -> str:
        item_id = await _remote_item(peer)
        peer.report("progress", "first", "2026-10-29T10:00:00+00:00")
        status, body = await _record(
            CONDUCTOR, {"action": "create", "title": "elsewhere", "acceptance": ACCEPTANCE}
        )
        assert status == 200, body
        work_ledger.apply_remote_bind(
            CONDUCTOR,
            body["item"]["item_id"],
            worker_session_key="chat-61-1793000000",
            worker_instance_id="inst-b",
        )
        # The other crew's item comes first, as the oldest-attempt order may put it.
        items = sorted(
            work_ledger.list_work_items(CONDUCTOR), key=lambda i: i.worker_instance_id == CREW
        )
        await remote_ingest._pull_all(_state(peer), CONDUCTOR, items)
        return item_id

    item_id = asyncio.run(go())
    assert order == [f"batched:{CREW}", "per_item:inst-b"]
    assert work_ledger.read_work_item(CONDUCTOR, item_id).summary == "first"


def test_the_hub_and_the_peer_share_their_caps():
    """A hub asking past the peer's caps would be refused every tick."""
    assert remote_batch_pull.MAX_AFTER_IDS == remote_work_items_pull.MAX_AFTER_IDS
    assert remote_ingest.MAX_ITEMS_PER_READ <= remote_work_items_pull.MAX_PULL_ITEMS
