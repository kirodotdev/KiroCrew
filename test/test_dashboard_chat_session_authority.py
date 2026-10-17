"""Dashboard chat: which copy of a session is authoritative — memory over disk, reload, close broadcasts."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_app_with_agent_routes, _make_state

from kiro_crew.testing.wait import await_until

# ── Default view uses in-memory (not stale disk) ──


class TestInMemoryAuthority:
    @pytest.mark.asyncio
    async def test_default_view_shows_current_messages(self, tmp_path, monkeypatch):
        """Default slot detail should return in-memory messages, not stale disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        # Stale disk data
        log.append("dashboard:s1", "user", "old question")
        log.append("dashboard:s1", "assistant", "old answer")

        # Active slot with different messages
        slot = state.get_or_create_slot("s1")
        slot.append("user", "new question")
        slot.append("tool", "✅ running")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s1")
            data = await resp.json()
            # Should show in-memory (2 msgs), not disk (2 different msgs)
            assert data["total"] == 2
            assert data["messages"][0]["content"] == "new question"
            assert data["messages"][1]["content"] == "✅ running"

    @pytest.mark.asyncio
    async def test_full_load_prepends_older_disk_messages(self, tmp_path, monkeypatch):
        """No-limit path prepends older disk messages when restore truncated."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        # Simulate: 8 messages on disk total (5 older + 3 recent)
        for i in range(8):
            log.append("dashboard:s2", "user", f"msg {i}")
        # Slot has only the last 3 in memory (simulating truncated restore)
        slot = state.get_or_create_slot("s2")
        slot.append("user", "msg 5")
        slot.append("user", "msg 6")
        slot.append("user", "msg 7")
        slot.drain()
        # Flag that restore truncated older messages
        slot._disk_older_count = 5

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/chat/slots/s2")
            data = await resp.json()
            assert data["total"] == 8  # 5 older + 3 recent
            assert data["has_more"] is False
            assert data["messages"][0]["content"] == "msg 0"
            assert data["messages"][4]["content"] == "msg 4"
            assert data["messages"][5]["content"] == "msg 5"

    @pytest.mark.asyncio
    async def test_legacy_pagination_with_limit(self, tmp_path, monkeypatch):
        """Legacy limit-based pagination reads from chained disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        for i in range(10):
            log.append("dashboard:s3", "user", f"msg {i}")
        slot = state.get_or_create_slot("s3")  # noqa: F841

        async with TestClient(TestServer(_make_app(state))) as client:
            # limit=3 returns last 3, has_more=True
            resp = await client.get("/api/chat/slots/s3?limit=3")
            data = await resp.json()
            assert data["total"] == 10
            assert data["has_more"] is True
            assert len(data["messages"]) == 3
            assert data["messages"][-1]["content"] == "msg 9"

            # limit=3&before=5 returns msgs 2-4
            resp = await client.get("/api/chat/slots/s3?limit=3&before=5")
            data = await resp.json()
            assert data["has_more"] is True  # msgs 0, 1 still older
            assert [m["content"] for m in data["messages"]] == ["msg 2", "msg 3", "msg 4"]

            # before=2 returns last 2 older
            resp = await client.get("/api/chat/slots/s3?limit=100&before=2")
            data = await resp.json()
            assert data["has_more"] is False
            assert [m["content"] for m in data["messages"]] == ["msg 0", "msg 1"]

    def test_append_only_preserves_full_disk_history(self, tmp_path, monkeypatch):
        """Save keeps ALL on-disk messages; nothing dropped or archived.

        After a restart the slot holds only a recent window (the frozen prefix
        counted by _disk_older_count is OLDER on disk) while the JSONL still has
        the full history. Saving a new turn must preserve the frozen prefix
        byte-for-byte — no overwrite, no truncation, no archive.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        # 8 messages on disk total (the pre-restart full history).
        for i in range(8):
            log.append("dashboard:s4", "user", f"old msg {i}")
        # Restore truncated to the last 3 in memory; _disk_window_len mirrors
        # what restore sets (those 3 are already the on-disk window tail).
        # Mirror PRODUCTION restore, which re-appends each window message WITH its
        # persisted ``ts`` (chat_persistence restore uses
        # ``slot.append(..., ts=m.get("ts", ""))``). Preserving the ts is what lets
        # a steady save recognise the on-disk window-region copies as its OWN (a
        # ts-match) rather than as fresh-ts duplicates — so neither duplicating nor
        # archiving them.
        slot = state.get_or_create_slot("s4")
        disk_tail = log.read_messages("dashboard:s4")[5:]
        for m in disk_tail:
            slot.append("user", m["content"], ts=m.get("ts", ""))
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._disk_older_count = 5
        slot.append("user", "brand new msg")
        slot.drain()

        _save_slot_to_history(state, slot)

        # Nothing archived — the frozen prefix is never dropped.
        assert not (tmp_path / "archive").exists()
        # All 8 original messages + the new one are on disk, in order.
        disk = log.read_messages("dashboard:s4")
        contents = [m["content"] for m in disk]
        assert contents == [f"old msg {i}" for i in range(8)] + ["brand new msg"]

    def test_append_only_colliding_ts_no_duplicate(self, tmp_path, monkeypatch):
        """Colliding timestamps must not duplicate a genuine message on disk.

        A coarse system clock (notably Windows' ~15ms tick) can stamp a burst of
        rapid appends with an IDENTICAL ``datetime.now().isoformat()``. The
        append-safe save must still match each on-disk window-region line to its
        own window entry one-for-one — never mis-classifying a real window line
        as a phantom "foreign append" and duplicating it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        # Freeze history.py's clock so every on-disk append shares ONE ts,
        # reproducing the coarse-clock collision deterministically on any OS.
        import datetime as _dt

        from kiro_crew.dashboard.chat import _save_slot_to_history

        _FROZEN = _dt.datetime(2026, 7, 25, 0, 0, 0, tzinfo=_dt.timezone.utc)

        class _FrozenDateTime:
            @classmethod
            def now(cls, tz=None):
                return _FROZEN

        monkeypatch.setattr("kiro_crew.history.datetime", _FrozenDateTime)

        state = _make_state(tmp_path)
        log = state.conversation_log
        # 8 messages on disk, ALL sharing the frozen ts.
        for i in range(8):
            log.append("dashboard:s5c", "user", f"old msg {i}")
        disk = log.read_messages("dashboard:s5c")
        # Every on-disk ts is identical — the exact condition that broke matching.
        assert len({m.get("ts") for m in disk}) == 1

        # Restore truncated to the last 3, re-appending WITH the persisted ts
        # (mirrors production restore) — so those window entries also collide.
        slot = state.get_or_create_slot("s5c")
        for m in disk[5:]:
            slot.append("user", m["content"], ts=m.get("ts", ""))
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._disk_older_count = 5
        slot.append("user", "brand new msg")
        slot.drain()

        _save_slot_to_history(state, slot)

        # No phantom foreign append, nothing archived, full history preserved.
        assert not (tmp_path / "archive").exists()
        contents = [m["content"] for m in log.read_messages("dashboard:s5c")]
        assert contents == [f"old msg {i}" for i in range(8)] + ["brand new msg"]

    def test_append_only_colliding_ts_edit_preserves_foreign(self, tmp_path, monkeypatch):
        """Colliding ts + in-place edit + foreign append must NOT drop the
        foreign line.

        The on-disk window region holds, sharing ONE coarse-clock ts: unchanged
        ``A``, an acknowledged cross-process foreign append ``X``, and the
        pre-edit copy of ``B`` — in that adversarial order (foreign BEFORE the
        pre-edit copy). Memory holds ``A`` and the EDITED ``B'``. A greedy
        ts-only match would consume ``X`` as if it were ``B``'s in-place edit and
        silently DROP the acknowledged foreign append. The ambiguity guard must
        instead preserve ``X`` (favouring a rare stale duplicate over
        irreversible data loss).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import json as _json

        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        t = "2026-07-25T00:00:00+00:00"  # single colliding timestamp

        def _line(content: str) -> str:
            return _json.dumps({"role": "user", "content": content, "ts": t}) + "\n"

        # Craft the adversarial on-disk order directly: A, foreign X, pre-edit B.
        path = log._path("dashboard:s5e")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps({"_type": "metadata", "created_at": t, "last_consolidated": 0})
            + "\n"
            + _line("msg A")
            + _line("msg X (foreign)")
            + _line("msg B"),
            encoding="utf-8",
        )

        # Window: A unchanged, B edited in place (content changes, ts preserved).
        slot = state.get_or_create_slot("s5e")
        slot.append("user", "msg A", ts=t)
        slot.append("user", "msg B (edited in place)", ts=t)
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = True  # an in-place edit marks the slot dirty

        _save_slot_to_history(state, slot)

        contents = [m["content"] for m in log.read_messages("dashboard:s5e")]
        # The acknowledged foreign append survived (no data loss) ...
        assert "msg X (foreign)" in contents, "foreign append was dropped (data loss)"
        # ... and the edited window content is present.
        assert "msg B (edited in place)" in contents

    def test_append_only_no_duplicate_on_resave(self, tmp_path, monkeypatch):
        """Re-saving without new messages must not duplicate the tail on disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        for i in range(4):
            log.append("dashboard:s6", "user", f"msg {i}")
        slot = state.get_or_create_slot("s6")
        for i in range(4):
            slot.append("user", f"msg {i}")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)

        slot.append("assistant", "reply A")
        slot.drain()
        _save_slot_to_history(state, slot)
        # A redundant save (force) must not duplicate the already-written tail.
        _save_slot_to_history(state, slot, force=True)

        disk = log.read_messages("dashboard:s6")
        contents = [m["content"] for m in disk]
        assert contents == [f"msg {i}" for i in range(4)] + ["reply A"]

    def test_save_steady_state_does_not_archive(self, tmp_path, monkeypatch):
        """A normal append (slot is a superset of disk) archives nothing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s5")
        slot.append("user", "first")
        slot.drain()
        _save_slot_to_history(state, slot)
        slot.append("assistant", "reply")
        slot.drain()
        _save_slot_to_history(state, slot)

        assert not (tmp_path / "archive").exists()

    def test_append_only_does_not_merge_unrelated_session(self, tmp_path, monkeypatch):
        """Append-only writes ONLY to the slot's own file — never an unrelated one.

        Guards the regression the reporter flagged: a second, unrelated session
        must not get merged into this slot's history. Append-only touches a
        single session file, so a sibling file is left completely untouched.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        # Two independent on-disk sessions (no shared tab_id).
        for i in range(3):
            log.append("dashboard:sessA", "user", f"A{i}")
        for i in range(2):
            log.append("dashboard:sessB", "user", f"B{i}")
        b_before = log.read_messages("dashboard:sessB")

        slot = state.get_or_create_slot("sessA")
        for i in range(3):
            slot.append("user", f"A{i}")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._tab_id = ""  # no chaining
        slot.append("user", "A-new")
        slot.drain()
        _save_slot_to_history(state, slot)

        # sessA got its new turn; sessB is byte-for-byte untouched.
        assert [m["content"] for m in log.read_messages("dashboard:sessA")] == [
            "A0",
            "A1",
            "A2",
            "A-new",
        ]
        assert log.read_messages("dashboard:sessB") == b_before

    def test_rewrite_path_archives_dropped_tail(self, tmp_path, monkeypatch):
        """An explicit snapshot save (rewrite, e.g. rewind) archives dropped msgs."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("s7")
        for c in ("q1", "a1", "q2", "a2"):
            slot.append("user" if c.startswith("q") else "assistant", c)
        slot.drain()
        _save_slot_to_history(state, slot)  # append-only establishes the file
        assert not (tmp_path / "archive").exists()

        # Rewind-style: truncate to first turn and rewrite via explicit snapshot.
        snapshot = [dict(m) for m in slot.messages[:1]]
        _save_slot_to_history(state, slot, snapshot)

        # Disk now holds only the kept prefix; dropped tail is archived.
        disk = log.read_messages("dashboard:s7")
        assert [m["content"] for m in disk] == ["q1"]
        archives = list((tmp_path / "archive").glob("dashboard_s7__*.jsonl"))
        assert len(archives) == 1
        content = archives[0].read_text(encoding="utf-8")
        for dropped in ("a1", "q2", "a2"):
            assert dropped in content

    def test_rewrite_preserves_frozen_prefix(self, tmp_path, monkeypatch):
        """A rewrite (rewind) with a frozen prefix keeps the prefix, drops only the tail.

        With _disk_older_count > 0 the file is frozen_prefix + window. A rewrite
        that truncates the window must leave the frozen prefix byte-for-byte and
        archive only the dropped window tail — never the older history.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        # 6 messages on disk; only the last 2 are the in-memory window.
        for i in range(6):
            log.append("dashboard:s11", "user", f"old {i}")
        slot = state.get_or_create_slot("s11")
        slot.append("user", "old 4")
        slot.append("assistant", "old 5")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._disk_older_count = 4  # old 0..3 are the frozen prefix

        # Rewind-style truncate to just the first window message + rewrite.
        snapshot = [dict(m) for m in slot.messages[:1]]
        _save_slot_to_history(state, slot, snapshot)

        disk = log.read_messages("dashboard:s11")
        # Frozen prefix preserved + kept window head; dropped tail archived.
        assert [m["content"] for m in disk] == ["old 0", "old 1", "old 2", "old 3", "old 4"]
        archives = list((tmp_path / "archive").glob("dashboard_s11__*.jsonl"))
        assert len(archives) == 1
        content = archives[0].read_text(encoding="utf-8")
        assert "old 5" in content
        # The frozen prefix must NOT be archived.
        for frozen in ("old 0", "old 1", "old 2", "old 3"):
            assert frozen not in content

    def test_flush_then_finalized_reply_persists(self, tmp_path, monkeypatch):
        """#1: a mid-turn flush past a stop_event must not lose the later reply.

        Repro of the boundary-asymmetry bug: a stop happens mid-turn, the 5s
        flush persists the window ending in the stop_event, then _flush_segment
        finalizes the assistant reply and moves the stop_event AFTER it. The
        finalized reply must end up on disk (the old position-counter model
        committed past the stop_event and dropped the later assistant line).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import json

        from kiro_crew.dashboard.chat import _flush_segment, _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("s8")
        slot.append("user", "do a thing")
        # Streaming chunk + a stop_event interleaved with this segment.
        slot.append("chunk", "partial output")
        stop_cls = json.dumps({"kind": "stop_event", "id": "stop-1", "state": "stopping"})
        slot.append("system", stop_cls, cls=stop_cls)
        slot.drain()

        # 5s flush lands here — window ends in chunk + stop_event.
        _save_slot_to_history(state, slot)

        # _flush_segment finalizes the assistant reply and re-orders the
        # stop_event to land AFTER it.
        _flush_segment(state, slot, "final answer", broadcast=False)
        slot.drain()
        _save_slot_to_history(state, slot)

        disk = log.read_messages("dashboard:s8")
        contents = [m["content"] for m in disk]
        # The finalized reply is on disk, after the user prompt; chunk dropped.
        assert "final answer" in contents
        assert "do a thing" in contents
        assert "partial output" not in contents
        # Stop card persists after the finalized assistant reply.
        assert contents.index("final answer") < contents.index(stop_cls)

    def test_inplace_stop_resolution_persists(self, tmp_path, monkeypatch):
        """#2: an in-place edit of an already-flushed message must persist.

        The stop_event message is flushed as "stopping"; later it is mutated in
        place to "stopped" (mirrors _resolve_stop_event). The re-serialized
        window must carry the resolution to disk — the old append-only model
        only wrote new tail messages and dropped the in-place edit.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        import json

        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("s9")
        slot.append("user", "go")
        stopping = json.dumps({"kind": "stop_event", "id": "stop-x", "state": "stopping"})
        slot.append("system", stopping, cls=stopping)
        slot.drain()
        _save_slot_to_history(state, slot)  # flushed as "stopping"
        # Model a resumed slot: the window equals the resumed count and the
        # in-place edit adds NO new message — exercises the resumed-guard path.
        slot._resumed_count = len(slot.messages)
        slot._disk_window_len = len(slot.messages)
        slot._dirty = False

        # Resolve in place (same object already on disk at index < end).
        stopped = json.dumps({"kind": "stop_event", "id": "stop-x", "state": "stopped"})
        for m in slot.messages:
            if m.get("role") == "system":
                m["cls"] = stopped
                m["content"] = stopped
                slot._dirty = True  # _resolve_stop_event sets this
                break
        _save_slot_to_history(state, slot)

        disk = log.read_messages("dashboard:s9")
        sys_msgs = [m for m in disk if m.get("role") == "system"]
        assert len(sys_msgs) == 1
        assert json.loads(sys_msgs[0]["cls"])["state"] == "stopped"

    def test_failed_rewrite_does_not_strand_or_lose(self, tmp_path, monkeypatch):
        """#3: a failed inline rewrite must not lose the dropped tail or strand state.

        rewind/regenerate truncate the window then save with a snapshot. If that
        save raises, _pending_rewrite stays set so the next (flush) save still
        takes the archive-safe rewrite path — the dropped tail is archived, not
        silently overwritten, and the kept prefix is correct on disk.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_persistence
        from kiro_crew.dashboard.chat import _save_slot_to_history

        state = _make_state(tmp_path)
        log = state.conversation_log
        slot = state.get_or_create_slot("s10")
        for c in ("q1", "a1", "q2", "a2"):
            slot.append("user" if c.startswith("q") else "assistant", c)
        slot.drain()
        _save_slot_to_history(state, slot)

        # Simulate rewind: truncate window to first turn, mark pending rewrite,
        # and have the inline rewrite save blow up inside atomic_write.
        del slot.messages[1:]
        slot._pending_rewrite = True
        orig_atomic = chat_persistence.atomic_write
        calls = {"n": 0}

        def _boom(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return orig_atomic(*a, **k)

        monkeypatch.setattr(chat_persistence, "atomic_write", _boom)
        snapshot = [dict(m) for m in slot.messages]
        with pytest.raises(OSError):
            _save_slot_to_history(state, slot, snapshot)
        # Flag still set → flush retries the archive-safe rewrite (2nd write OK).
        assert slot._pending_rewrite is True
        _save_slot_to_history(state, slot)
        assert slot._pending_rewrite is False

        disk = log.read_messages("dashboard:s10")
        assert [m["content"] for m in disk] == ["q1"]
        archives = list((tmp_path / "archive").glob("dashboard_s10__*.jsonl"))
        assert len(archives) >= 1
        content = "".join(a.read_text(encoding="utf-8") for a in archives)
        for dropped in ("a1", "q2", "a2"):
            assert dropped in content


# ── Session reload endpoint (POST /api/chat/slots/{slot}/reload) ──


class TestSessionReload:
    """api_chat_slot_reload — relaunch the slot's agent process in place.

    The endpoint's contract: refuse while a turn is in flight (409), otherwise
    tear down via reset(skip_if_busy=True), append the session_reload feed
    notice, and eagerly re-arm the resume spawn.
    """

    @staticmethod
    def _idle_provider():
        provider = MagicMock()
        provider.has_active_turn = MagicMock(return_value=False)
        return provider

    @pytest.mark.asyncio
    async def test_reload_unknown_slot_404(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/nope/reload")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_reload_resets_notices_and_respawns(self, tmp_path, monkeypatch):
        """Happy path: atomic reset, feed notice tagged session_reload, eager resume."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=True)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        state.sessions.reset.assert_awaited_once_with("dashboard:s1", skip_if_busy=True)
        # The feed carries the durable confirmation, tagged so the follow-up
        # options / continuable scans skip it (isSystemNoticeKind frontend twin).
        notice = slot.messages[-1]
        assert notice["role"] == "assistant"
        assert notice["meta"]["kind"] == "session_reload"
        assert "Reloading session" in notice["content"]
        eager.assert_called_once()
        assert eager.call_args.kwargs.get("allow_resume") is True

    @pytest.mark.asyncio
    async def test_reload_refused_while_turn_in_flight(self, tmp_path, monkeypatch):
        """409 with no teardown, no notice, no eager spawn while a turn runs."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        busy = MagicMock()
        busy.has_active_turn = MagicMock(return_value=True)
        state.sessions.get_provider = MagicMock(return_value=busy)
        state.sessions.reset = AsyncMock()
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        state.sessions.reset.assert_not_awaited()
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_race_turn_started_after_fast_path(self, tmp_path, monkeypatch):
        """A turn slipping in between the fast path and the atomic guard is a 409.

        reset(skip_if_busy=True) returning False is ambiguous (no session OR
        busy); the re-check of has_active_turn is what disambiguates. Nothing
        may be appended or respawned for the busy case.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        idle = self._idle_provider()
        busy = MagicMock()
        busy.has_active_turn = MagicMock(return_value=True)
        # First lookup (fast path) sees idle, as does the teardown's
        # pre-reset identity capture; the post-reset re-check sees busy.
        state.sessions.get_provider = MagicMock(side_effect=[idle, idle, busy])
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_retries_when_racing_turn_already_finished(self, tmp_path, monkeypatch):
        """A declined reset whose racing turn already FINISHED retries once.

        Falling through would report success while the stale live process
        survives untouched -- the silent false-success the endpoint exists to
        prevent. The retry succeeds here; a second decline is the 409 case
        (covered below).
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(side_effect=[False, True])
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        assert state.sessions.reset.await_count == 2
        assert slot.messages[-1]["meta"]["kind"] == "session_reload"
        eager.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_double_decline_is_turn_in_flight(self, tmp_path, monkeypatch):
        """Two consecutive declines with a live session mean a genuinely racing
        turn: 409, no notice, no respawn -- never a false success."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "turn_in_flight"
        assert state.sessions.reset.await_count == 2
        assert len(slot.messages) == before
        eager.assert_not_called()

    def test_system_notice_kinds_backend_frontend_parity(self):
        """The two SYSTEM_NOTICE_KINDS twins must hold the same set.

        A kind skipped on one side but not the other leaves the sidebar showing
        notice boilerplate while the chat pane shows the real turn (or vice
        versa) -- the exact divergence the shared predicates exist to prevent.
        Asserted on the TS source because nothing else couples the two files.
        """
        import re
        from pathlib import Path

        from kiro_crew.dashboard.system_notices import SYSTEM_NOTICE_KINDS

        ts_path = Path(__file__).resolve().parents[1] / "website/src/lib/systemNotice.ts"
        ts_src = ts_path.read_text(encoding="utf-8")
        m = re.search(r"new Set\(\[([^\]]*)\]\)", ts_src)
        assert m, "SYSTEM_NOTICE_KINDS Set literal not found in systemNotice.ts"
        frontend = set(re.findall(r"'([^']+)'", m.group(1)))
        assert frontend == set(SYSTEM_NOTICE_KINDS)

    @pytest.mark.asyncio
    async def test_reload_without_live_session_is_ok(self, tmp_path, monkeypatch):
        """No live session: reload is a no-op teardown but still ok + notice + respawn.

        The goal state (next spawn picks up fresh config) already holds, so
        this must not be an error; reloaded=False tells the caller no process
        was actually torn down.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock(return_value=False)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 200
        assert data == {"ok": True}
        assert slot.messages[-1]["meta"]["kind"] == "session_reload"
        eager.assert_called_once()

    @pytest.mark.asyncio
    async def test_reload_cancels_pending_question_waits(self, tmp_path, monkeypatch):
        """A pending ask_question wait is released by the reload teardown.

        The card lives in dashboard state, not the session — without the
        unblock it would survive the reset and hold its MCP worker until
        timeout with no agent left to receive the answer.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            MagicMock(return_value=None),
        )
        cancelled = MagicMock(return_value=1)
        monkeypatch.setattr(state, "cancel_questions_for_slot", cancelled)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 200
        cancelled.assert_called_once_with("s1")

    @pytest.mark.asyncio
    async def test_reload_app_token_denied_on_foreign_slot(self, tmp_path, monkeypatch):
        """An app token gets the indistinguishable 404, and nothing is torn down.

        Reload is a teardown; without the cancel-route ownership policy, an app
        allowed /api/chat/* could reset a foreign slot's process and cancel its
        pending waits.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._app = "other-app"
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock()
        app = _make_app_with_agent_routes(state)

        @web.middleware
        async def _as_app(request, handler):
            request["app"] = "attacker-app"
            return await handler(request)

        app.middlewares.append(_as_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"
        state.sessions.reset.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reload_targets_the_linked_session_key(self, tmp_path, monkeypatch):
        """A channel-born slot reloads its LINKED session, not the phantom
        dashboard-prefixed key — otherwise reload reports success while the
        live process keeps its stale config.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.linked_session_key = "slack:123.456"
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock(return_value=True)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.schedule_eager_spawn",
            MagicMock(return_value=None),
        )

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 200
        state.sessions.reset.assert_awaited_once_with("slack:123.456", skip_if_busy=True)

    @pytest.mark.asyncio
    async def test_reload_refused_while_subagents_attached(self, tmp_path, monkeypatch):
        """409 while children are running/queued/delivering — the reset would
        tear down the shared subagent runtime and discard their work.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=self._idle_provider())
        state.sessions.reset = AsyncMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=["child-1"])
        state.subagents._queued_depth = MagicMock(return_value=0)
        eager = MagicMock(return_value=None)
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.schedule_eager_spawn", eager)
        before = len(slot.messages)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")
            data = await resp.json()

        assert resp.status == 409
        assert data["code"] == "slot_subagents_running"
        state.sessions.reset.assert_not_awaited()
        assert len(slot.messages) == before
        eager.assert_not_called()

    @pytest.mark.asyncio
    async def test_reload_fails_closed_on_unreadable_children_probe(self, tmp_path, monkeypatch):
        """A None running-probe is the probe FAILING, not zero children."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.sessions.get_provider = MagicMock(return_value=None)
        state.sessions.reset = AsyncMock()
        state.subagents = MagicMock()
        state.subagents.running_agents_for = MagicMock(return_value=None)

        async with TestClient(TestServer(_make_app_with_agent_routes(state))) as client:
            resp = await client.post("/api/chat/slots/s1/reload")

        assert resp.status == 409
        state.sessions.reset.assert_not_awaited()


class TestCloseBroadcastDurability:
    """A slot-list frame must never omit a slot that is coming back.

    Clients treat a frame omitting a slot as licence to drop that slot's cached
    per-slot state, including follow-up and folder-suggestion cards that only
    ever exist in the browser and are never re-sent. So the close may only
    broadcast once the history save is durable: past that point no rollback can
    put the slot back, and the omission can never be retracted.

    Every test records the ORDER of the close steps, because asserting only that
    a broadcast happened passes against any placement and proves nothing.
    """

    @staticmethod
    def _instrument(state, slot, monkeypatch, frames=None):
        """Record close steps in call order; append each frame's slot keys."""
        from kiro_crew.dashboard import chat_handlers

        calls: list[str] = []

        def _push():
            calls.append("broadcast")
            if frames is not None:
                frames.append(set(state._slots))

        state.push_slots_update = MagicMock(side_effect=_push)
        # A successful close announces the removal through push_slot_removed,
        # which is the same slot-list broadcast for this test's purpose.
        state.push_slot_removed = MagicMock(side_effect=lambda _key: _push())

        real_sync = chat_handlers._sync_dashboard_slots

        def _sync(st):
            calls.append("sync")
            return real_sync(st)

        monkeypatch.setattr(chat_handlers, "_sync_dashboard_slots", _sync)

        real_unblock = chat_handlers._unblock_pending_waits

        def _unblock(st, sl):
            calls.append("unblock_pending_waits")
            return real_unblock(st, sl)

        monkeypatch.setattr(chat_handlers, "_unblock_pending_waits", _unblock)

        async def _save(*args, **kwargs):
            calls.append("save")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _save)

        state.sessions.remove = AsyncMock(side_effect=lambda *a, **k: calls.append("remove"))

        eager = MagicMock()
        eager.done = MagicMock(return_value=False)
        eager.cancel = MagicMock(side_effect=lambda: calls.append("cancel_eager"))
        slot._eager_spawn_task = eager

        async def _never() -> None:
            await asyncio.sleep(30)

        task = asyncio.get_running_loop().create_task(_never())
        real_cancel = task.cancel

        def _task_cancel(*args, **kwargs):
            calls.append("cancel_task")
            return real_cancel(*args, **kwargs)

        task.cancel = _task_cancel  # type: ignore[method-assign]
        # `running` is derived from `task`, so assigning the task is enough to
        # reach the cancel-and-wait_for branch.
        slot.task = task

        return calls

    @pytest.mark.asyncio
    async def test_failed_close_never_emits_a_frame_without_the_slot(self, tmp_path, monkeypatch):
        """The regression guard: a rollback must not be preceded by an omission.

        A frame omitting the slot is what makes another client drop this slot's
        follow-up and folder-suggestion cards. Those are browser-only and the
        backend re-offers a folder suggestion at most once per slot, so a prune
        on a close that then rolls back loses them permanently.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        frames: list[set] = []
        calls = self._instrument(state, slot, monkeypatch, frames=frames)

        async def _boom(*args, **kwargs):
            calls.append("save")
            raise RuntimeError("disk full")

        monkeypatch.setattr(chat_handlers, "save_slot_off_loop", _boom)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 500

        assert frames, "no frame was broadcast at all, so this proves nothing"
        missing = [i for i, f in enumerate(frames) if "s1" not in f]
        assert not missing, (
            f"frame(s) {missing} omitted a slot that the rollback restores, so a "
            f"client pruned its cards for it: frames={frames} calls={calls}"
        )
        assert state._slots.get("s1") is slot, "slot was not restored"

    @pytest.mark.asyncio
    async def test_broadcast_precedes_the_session_teardown(self, tmp_path, monkeypatch):
        """On success, broadcast after the save but before the slow teardown."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

        for step in ("save", "remove", "broadcast"):
            assert step in calls, f"{step} never ran, so this proves nothing: {calls}"
        assert calls.index("save") < calls.index(
            "broadcast"
        ), f"broadcast ran before the save was durable: {calls}"
        assert calls.index("broadcast") < calls.index(
            "remove"
        ), f"broadcast waited out the session teardown: {calls}"

    @pytest.mark.asyncio
    async def test_broadcast_beats_a_slow_session_teardown(self, tmp_path, monkeypatch):
        """A session teardown that never returns must not hold the broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)
        seen = asyncio.Event()
        state.push_slots_update = MagicMock(
            side_effect=lambda: (calls.append("broadcast"), seen.set())
        )
        state.push_slot_removed = MagicMock(
            side_effect=lambda _key: (calls.append("broadcast"), seen.set())
        )

        async def _hang(*args, **kwargs):
            calls.append("remove")
            # flake-ok: the stubbed session remove hangs so the close stays in flight
            # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed session remove hangs so the close stays in flight
            await asyncio.sleep(30)

        state.sessions.remove = AsyncMock(side_effect=_hang)

        async with TestClient(TestServer(_make_app(state))) as client:
            close = asyncio.get_running_loop().create_task(client.delete("/api/chat/slots/s1"))
            await await_until(seen, what="the broadcast firing")
            assert "save" in calls and calls.index("save") < calls.index("broadcast")
            assert "s1" not in state._slots
            close.cancel()
            try:
                await close
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_active_slot_set_is_published_only_after_the_teardown(
        self, tmp_path, monkeypatch
    ):
        """_sync_dashboard_slots must stay after the teardown.

        Publishing the shrunken set earlier lets the idle sweep classify this
        session as orphaned and reap it mid-teardown, and that reap releases a
        shared subagent runtime which outlives the turn — the turn semaphore, the
        sweep's only busy guard, is free while subagents run.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        calls = self._instrument(state, slot, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            assert resp.status == 200

        assert "sync" in calls, f"the trailing sync never ran: {calls}"
        assert calls.index("save") < calls.index(
            "sync"
        ), f"active-slot set published before the save was durable: {calls}"
        assert calls.index("remove") < calls.index(
            "sync"
        ), f"active-slot set published before the session teardown: {calls}"
