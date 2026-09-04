"""Rotated-archive pagination: the corpus behind `before`/`next_before` cursors.

A size rotation moves a transcript's HEAD into ``sessions/archive/`` — and the
plain chained read never looks there, so pagination used to declare the
transcript complete at the rotation boundary: the reader's oldest messages
became permanently unreachable from the UI (observed live: a session's true
first message sat in ``archive/…__20260901-043212.jsonl`` while "load previous"
retired at a mid-conversation row).

``read_messages_chained_full`` is the fix's foundation: per chain key, the
rotate-archived head followed by the surviving file. These tests pin:

- the full read restores the pre-rotation timeline, oldest first;
- non-rotate archive segments (``compact`` — content the product DISCARDED)
  never resurface;
- the per-key cache invalidates when a new rotation lands.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.history import ConversationLog


def _contents(msgs: list[dict]) -> list[str]:
    return [m.get("content", "") for m in msgs]


@pytest.fixture()
def rotated_log(tmp_path, monkeypatch):
    """A log whose session 't1' rotated at least once, with 20 known messages."""
    monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
    monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
    log = ConversationLog(base_dir=tmp_path)
    for i in range(20):
        log.append("t1", "user", f"message number {i:02d} with enough text to exceed limits")
    archives = list((tmp_path / "archive").glob("t1__*.jsonl"))
    assert archives, "fixture must rotate"
    return log, tmp_path


class TestReadMessagesChainedFull:
    def test_restores_pre_rotation_timeline(self, rotated_log):
        log, tmp_path = rotated_log
        plain = log.read_messages_chained("t1")
        full = log.read_messages_chained_full("t1")
        # The plain read lost the rotated head; the full read has every message
        # in send order.
        assert len(plain) < 20
        assert _contents(full) == [
            f"message number {i:02d} with enough text to exceed limits" for i in range(20)
        ]

    def test_compact_segments_do_not_resurface(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t2", "user", "kept message")
        log.rewrite_session("t2", [{"role": "user", "content": "kept message", "ts": "x"}])
        # rewrite_session archives what it drops under reason="compact"; craft
        # one explicitly to make the exclusion unmistakable.
        adir = tmp_path / "archive"
        adir.mkdir(exist_ok=True)
        seg = adir / "t2__20990101-000000.jsonl"
        seg.write_text(
            json.dumps({"_type": "archive", "reason": "compact", "count": 1})
            + "\n"
            + json.dumps({"role": "user", "content": "discarded by rewind"})
            + "\n",
            encoding="utf-8",
        )
        full = log.read_messages_chained_full("t2")
        assert "discarded by rewind" not in _contents(full)
        assert "kept message" in _contents(full)

    def test_cache_invalidates_on_new_rotation(self, rotated_log, monkeypatch):
        log, tmp_path = rotated_log
        first = log.read_messages_chained_full("t1")
        # Append enough to rotate again; the full read must pick up the new
        # segment rather than serve the cached rows.
        for i in range(20, 30):
            log.append("t1", "user", f"message number {i:02d} with enough text to exceed limits")
        second = log.read_messages_chained_full("t1")
        assert len(second) > len(first)
        assert _contents(second)[0].startswith("message number 00")
        assert _contents(second)[-1].startswith("message number 29")

    def test_rotated_chain_read_returns_only_archived_head(self, rotated_log):
        log, tmp_path = rotated_log
        rotated = log.read_rotated_messages_chained("t1")
        plain = log.read_messages_chained("t1")
        assert len(rotated) + len(plain) == 20
        # Oldest first, and strictly the head the rotation removed.
        assert _contents(rotated) == [
            f"message number {i:02d} with enough text to exceed limits" for i in range(len(rotated))
        ]

    def test_no_archive_is_identity(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("t3", "user", "only message")
        assert _contents(log.read_messages_chained_full("t3")) == ["only message"]
        assert log.read_rotated_messages_chained("t3") == []


class TestChainMidRotation:
    """The probe that keeps the slot-detail fast cursor honest.

    The fast path's `next_before = <collapsed archive head count>` is exact
    only while every archived row precedes every row the response carries.
    A rotation on a LATER chain member breaks that (its archive is sandwiched
    mid-corpus), and the handler must serve the true chained corpus instead.
    """

    def test_single_key_rotation_is_not_mid_chain(self, rotated_log):
        log, _ = rotated_log
        assert log.chain_mid_rotation("t1") is False

    def test_first_member_rotation_is_not_mid_chain(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
        log = ConversationLog(base_dir=tmp_path)
        tab = "tabchain0001"
        k1 = "dashboard:chat-1-100"
        k2 = "dashboard:chat-2-200"
        for i in range(20):
            log.append(k1, "user", f"first member row {i:02d} padded to rotate", tab_id=tab)
        for i in range(3):
            log.append(k2, "user", f"tail row {i}", tab_id=tab)
        assert list((tmp_path / "archive").glob("dashboard_chat-1-100__*.jsonl")), "k1 must rotate"
        assert not list((tmp_path / "archive").glob("dashboard_chat-2-200__*.jsonl"))
        # Chain resolves from either member's key.
        assert log.chain_mid_rotation(k2) is False

    def test_later_member_rotation_is_mid_chain(self, tmp_path, monkeypatch):
        log = ConversationLog(base_dir=tmp_path)
        tab = "tabchain0002"
        k1 = "dashboard:chat-1-100"
        k2 = "dashboard:chat-2-200"
        for i in range(3):
            log.append(k1, "user", f"head row {i}", tab_id=tab)
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
        for i in range(20):
            log.append(k2, "user", f"second member row {i:02d} padded to rotate", tab_id=tab)
        assert list((tmp_path / "archive").glob("dashboard_chat-2-200__*.jsonl")), "k2 must rotate"
        assert log.chain_mid_rotation(k2) is True

    def test_sandwich_shape_is_why(self, tmp_path, monkeypatch):
        """Pin the corpus shape the probe exists for: the later member's
        archived rows sit BETWEEN rows the fast path returns, so the head-count
        cursor cannot address them."""
        log = ConversationLog(base_dir=tmp_path)
        tab = "tabchain0003"
        k1 = "dashboard:chat-1-100"
        k2 = "dashboard:chat-2-200"
        for i in range(3):
            log.append(k1, "user", f"head row {i}", tab_id=tab)
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
        for i in range(20):
            log.append(k2, "user", f"second member row {i:02d} padded to rotate", tab_id=tab)
        full = _contents(log.read_messages_chained_full(k2))
        plain = _contents(log.read_messages_chained(k2))
        rotated = _contents(log.read_rotated_messages_chained(k2))
        # Nothing lost: full == plain + rotated as SETS...
        assert sorted(full) == sorted(plain + rotated)
        # ...but the archived rows are NOT a prefix of the full corpus — they
        # start after k1's live rows, which is what breaks the single cursor.
        assert full[: len(rotated)] != rotated
        assert full.index(rotated[0]) == 3  # right after k1's three live rows


class TestSlotDetailMidChainRotation:
    """The slot-detail fast path must not advertise a single cursor for a
    corpus whose archived rows are NOT a prefix (a later chain member
    rotated): those rows would be unreachable by paging. It serves the true
    chained corpus instead, every row at its real position."""

    @pytest.mark.asyncio
    async def test_full_load_serves_true_corpus_on_mid_chain_rotation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        tab = "tabmidrot001"

        # Older chain member: 3 live rows, never rotates.
        older_key = "dashboard:chat-1-100"
        for i in range(3):
            log.append(older_key, "user", f"old row {i}", tab_id=tab)

        # Current member rotates: its archived head is SANDWICHED after the
        # older member's rows in the true corpus.
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
        cur_slot = "chat-2-200"
        cur_key = f"dashboard:{cur_slot}"
        slot = state.get_or_create_slot(cur_slot)
        for i in range(20):
            log.append(cur_key, "user", f"cur row {i:02d} padded to force a rotation", tab_id=tab)
            slot.append("user", f"cur row {i:02d} padded to force a rotation")
        slot.drain()
        assert list(
            (tmp_path / "sessions" / "archive").glob("dashboard_chat-2-200__*.jsonl")
        ) or list(
            (tmp_path / "archive").glob("dashboard_chat-2-200__*.jsonl")
        ), "current member must rotate"
        assert log.chain_mid_rotation(cur_key) is True

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get(f"/api/chat/slots/{cur_slot}")
            assert resp.status == 200
            data = await resp.json()
            contents = [m.get("content", "") for m in data["messages"]]
            # The true corpus is served whole: the older member's rows AND the
            # current member's rotate-archived head are present inline...
            assert "old row 0" in contents
            assert any(c.startswith("cur row 00") for c in contents)
            # ...so there is nothing left to page toward.
            assert data["has_more"] is False


class TestForkMidChainRotation:
    """Fork indices arrive in the paginated corpus's visible-row space. When a
    later chain member rotated, that corpus interleaves rot/live per key -- a
    flat archived-head prepend would shift ``at_message_index`` by the
    sandwiched rows and fork the WRONG message."""

    @pytest.mark.asyncio
    async def test_fork_index_resolves_in_the_interleaved_corpus(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        tab = "tabforkmid01"

        # Older member: 2 visible rows, never rotates.
        older_key = "dashboard:chat-1-100"
        log.append(older_key, "user", "old-q1", tab_id=tab)
        log.append(older_key, "assistant", "old-a1")

        # Current member rotates: its archived head lands AFTER the older
        # member's rows in the true corpus (sandwiched).
        monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
        monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
        cur_slot = "chat-2-200"
        cur_key = f"dashboard:{cur_slot}"
        for i in range(12):
            log.append(
                cur_key, "user", f"cur row {i:02d} padded well past the rotation budget", tab_id=tab
            )
        log.invalidate_tab_id_cache()
        assert log.chain_mid_rotation(cur_key) is True

        slot = state.get_or_create_slot(cur_slot)
        slot._tab_id = tab
        for m in log.read_messages_chained(cur_key):
            slot.append(m["role"], m["content"])
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._dirty = False

        # The client's index space: the true chained corpus's visible rows.
        full = log.read_messages_chained_full(cur_key)
        visible = [m for m in full if m.get("role") in ("user", "assistant")]
        # Fork at the OLDER member's second row -- under a flat archived-head
        # prepend this index would land inside the archived block instead.
        target_index = 1
        assert visible[target_index]["content"] == "old-a1"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                f"/api/chat/slots/{cur_slot}/fork",
                json={"at_message_index": target_index},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert data["ok"] is True

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        forked_visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        # The fork carries exactly the rows up to and including the target --
        # resolved in the SAME corpus the client indexed against.
        assert [m["content"] for m in forked_visible] == ["old-q1", "old-a1"]
