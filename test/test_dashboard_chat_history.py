"""Dashboard chat: history persistence, history keys, resume dedupe and ``_prepare_messages``."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state, close_before_resume

from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.history import ConversationLog

# ── History persistence and disk fallback ──


class TestHistoryPersistence:
    def test_tool_messages_saved(self, tmp_path):
        log = ConversationLog(base_dir=tmp_path)
        log.append("dashboard:s1", "user", "hello")
        log.append("dashboard:s1", "tool", "✅ bash")
        log.append("dashboard:s1", "assistant", "hi there")
        msgs = log.read_messages("dashboard:s1")
        assert len(msgs) == 3
        assert msgs[1]["role"] == "tool"

    @pytest.mark.asyncio
    async def test_disk_fallback_for_trimmed_slot(self, tmp_path, monkeypatch):
        """Default view uses in-memory; pagination of older messages uses disk."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("big")
        log = state.conversation_log

        # Use a count that fits in memory — test disk pagination without trim
        for i in range(300):
            log.append("dashboard:big", "user", f"msg {i}")
            slot.append("user", f"msg {i}")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            # Default: in-memory
            resp = await client.get("/api/chat/slots/big?limit=200")
            data = await resp.json()
            assert data["total"] == 300
            assert data["has_more"] is True
            assert data["messages"][-1]["content"] == "msg 299"

            # Pagination with before: falls back to disk
            resp = await client.get("/api/chat/slots/big?limit=200&before=100")
            data = await resp.json()
            assert len(data["messages"]) == 100
            assert data["messages"][0]["content"] == "msg 0"
            assert data["has_more"] is False


# ── _prepare_messages filtering ──


class TestPrepareMessages:
    def test_queued_preserved_done_stripped(self):
        """queued messages must survive _prepare_messages so the frontend shows the banner after tab switch."""
        from kiro_crew.dashboard.chat import _prepare_messages

        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "queued", "content": "next msg"},
            {"role": "done", "content": ""},
            {"role": "assistant", "content": "hi"},
        ]
        out = _prepare_messages(msgs, running=False, live_child="")
        roles = [m["role"] for m in out]
        assert "queued" in roles, "queued must be preserved for tab-switch indicator"
        assert "done" not in roles, "done must be stripped"

    def test_chunks_collapsed_to_streaming(self):
        """Trailing chunks should be collapsed into a single streaming message."""
        from kiro_crew.dashboard.chat import _prepare_messages

        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "chunk", "content": "Hel"},
            {"role": "chunk", "content": "lo"},
        ]
        out = _prepare_messages(msgs, running=True, live_child="")
        assert out[-1]["role"] == "streaming"
        assert "Hel" in out[-1]["content"]

    def test_queued_placeholder_removed_on_processing(self):
        """When a queued message starts processing, its placeholder is replaced by a user entry."""
        import json

        from kiro_crew.dashboard.chat import _remove_queued_by_id

        slot = _ChatSlot("s1")
        slot.append("user", "first")
        qid = slot.queue_append("second")
        slot.append("queued", "second", json.dumps({"queue_id": qid}))

        item = slot.queue_pop(0)
        _remove_queued_by_id(slot.messages, item["id"])
        slot.append("user", item["content"], "msg msg-u")

        roles = [m["role"] for m in slot.messages]
        assert "queued" not in roles, "queued placeholder must be removed once processing starts"
        assert roles.count("user") == 2

    def test_duplicate_queued_removes_only_targeted(self):
        """When the same text is queued twice, only the targeted placeholder is removed by ID."""
        import json

        from kiro_crew.dashboard.chat import _remove_queued_by_id

        slot = _ChatSlot("s1")
        qid1 = slot.queue_append("hello")
        qid2 = slot.queue_append("hello")
        slot.append("queued", "hello", json.dumps({"queue_id": qid1}))
        slot.append("queued", "hello", json.dumps({"queue_id": qid2}))

        item = slot.queue_pop(0)
        _remove_queued_by_id(slot.messages, item["id"])
        slot.append("user", item["content"], "msg msg-u")

        queued = [m for m in slot.messages if m.get("role") == "queued"]
        assert len(queued) == 1, "second queued placeholder must survive"
        surviving_cls = json.loads(queued[0].get("cls", "{}"))
        assert surviving_cls.get("queue_id") == qid2


class TestPrepareMessagesInterleaved:
    """Tests for _prepare_messages with interleaved assistant/tool/chunk messages."""

    def test_interleaved_assistant_tool_chunk_structure(self):
        """_prepare_messages with interleaved assistant/tool/chunk returns
        correct structure.

        Validates: Requirements 6.1
        """
        from kiro_crew.dashboard.chat import _prepare_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Before tool", "cls": "msg msg-a"},
            {"role": "tool", "content": "✅ read_file", "cls": "msg msg-tool"},
            {"role": "assistant", "content": "After tool", "cls": "msg msg-a"},
            {"role": "chunk", "content": "still "},
            {"role": "chunk", "content": "streaming"},
        ]

        result = _prepare_messages(messages, running=True, live_child="")

        # user, assistant, tool, assistant, streaming (collapsed chunks)
        assert len(result) == 5
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[1]["content"] == "Before tool"
        assert result[2]["role"] == "tool"
        assert result[3]["role"] == "assistant"
        assert result[3]["content"] == "After tool"
        assert result[4]["role"] == "streaming"
        assert result[4]["content"] == "still streaming"

    def test_no_trailing_chunks_no_streaming(self):
        """Without trailing chunks, no streaming message is produced."""
        from kiro_crew.dashboard.chat import _prepare_messages

        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Segment 1", "cls": "msg msg-a"},
            {"role": "tool", "content": "✅ bash", "cls": "msg msg-tool"},
            {"role": "assistant", "content": "Segment 2", "cls": "msg msg-a"},
        ]

        result = _prepare_messages(messages, running=False, live_child="")

        assert len(result) == 4
        roles = [m["role"] for m in result]
        assert "streaming" not in roles
        assert "chunk" not in roles


class TestKiroReadinessQueueHandoff:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("wait_end", ["stop", "deadline"])
    async def test_turn_identity_precedes_memory_preparation_wait_and_is_retired_on_stop(
        self, tmp_path, monkeypatch, wait_end
    ):
        """A transient startup fence delays a turn without becoming its failure.

        The turn's identity is published BEFORE the fence (a cancel or a
        sibling switch's busy scan must see which session the parked turn is
        starting on) and retired by the refused turn itself afterwards.
        """
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        release = asyncio.Event()

        async def prepare_memory():
            await release.wait()

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.push_refresh = MagicMock()
        state.refresh_slot_source_status = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False

        async def stream(_message):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ready")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        state.sessions.record_failure = AsyncMock()
        state.conversation_log = MagicMock()
        state.conversation_log.read_messages.return_value = []
        # The turn-start read-back folds the on-disk line into the live carrier;
        # a mocked log must answer that read like an absent file (readable, no line).
        state.conversation_log.get_metadata_status.return_value = ({}, True)
        # The turn now reaches the transcript save; a MagicMock ``_path`` would
        # otherwise materialise a ``MagicMock/`` tree in the current directory.
        state.conversation_log._path.return_value = tmp_path / "transcript.jsonl"
        if wait_end == "deadline":
            monkeypatch.setattr("kiro_crew.memory_startup.MEMORY_ADMISSION_WAIT_SECONDS", 0.0)
        state.memory_startup_task = asyncio.create_task(prepare_memory())
        slot = state.get_or_create_slot("memory-startup-admission")
        slot.queue_append("keep queued during startup")
        turn = asyncio.create_task(_run_chat(state, slot, "first message"))
        slot.task = turn
        try:
            await asyncio.sleep(0)
            assert slot._active_turn_session_key == "dashboard:memory-startup-admission"
            state.sessions.get_or_create.assert_not_awaited()

            if wait_end == "stop":
                turn.cancel()
            await turn
            assert not state.memory_startup_task.cancelled()
            assert not state.memory_startup_task.done()
            assert slot.task is None
            assert slot.messages[-1]["role"] == "done"
            assert [item["content"] for item in slot._queue] == ["keep queued during startup"]
            assert any(call.args[0] == "chat_done" for call in state.broadcast_ws.call_args_list)
            assert slot._active_turn_session_key == ""
            state.sessions.get_or_create.assert_not_awaited()
            state.sessions.record_failure.assert_not_awaited()
            state.conversation_log.update_metadata.assert_not_called()
            if wait_end == "deadline":
                error = next(message for message in slot.messages if message["role"] == "error")
                assert error["meta"]["code"] == "memory_unavailable"
                assert "Retry shortly" in error["content"]

            slot._queue.clear()
            release.set()
            await state.memory_startup_task
            await _run_chat(state, slot, "retry after preparation")
            state.sessions.get_or_create.assert_awaited_once()
            state.sessions.record_failure.assert_not_awaited()
            assert any(
                message.get("role") == "assistant" and message.get("content") == "ready"
                for message in slot.messages
            )
        finally:
            release.set()
            await state.memory_startup_task

    @pytest.mark.asyncio
    async def test_dequeued_turn_runs_without_a_readiness_probe(
        self,
        tmp_path,
    ):
        """A queued item is never lost to a readiness check.

        Probing readiness on turn entry AND again before dequeueing would let
        a third false answer drop an item already popped off the queue.
        Both probes are gone — readiness is latched at boot and the ACP attempt
        reports auth failures — so the successor turn simply runs. This pins the
        no-loss invariant that outlives the probes.
        """
        from kiro_crew.dashboard.chat import _run_chat
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        delivered: list[str] = []

        async def stream(stream_message: str):
            delivered.append(stream_message)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=f"response to {stream_message}")
            yield LLMEvent(kind=EVENT_COMPLETE)

        client = MagicMock()
        client.stream = stream
        client.stream_command = stream
        client.context_usage_pct = MagicMock(return_value=1.0)
        state = _make_state(tmp_path)
        # A service whose latch would answer "not ready" if anything asked it.
        state.kiro_prerequisite_service = object()
        state.broadcast_ws = MagicMock()
        state.push_slots_update = MagicMock()
        state.context_builder = None
        state.consolidator = None
        state._hook_store = None
        state._yolo = False
        state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        slot = state.get_or_create_slot("queued-readiness")
        slot._titled = True
        queue_id = slot.queue_append("keep this queued")

        await _run_chat(state, slot, "first message")
        assert slot.task is not None
        await slot.task

        assert delivered[0] == "first message"
        assert delivered[1].endswith("keep this queued")
        assert slot._queue == []
        assert any(
            call.args[0] == "queue_pop" and call.args[1]["queue_id"] == queue_id
            for call in state.broadcast_ws.call_args_list
        )


# ── History save on close (not per-turn) ──


class TestHistorySaveOnClose:
    @pytest.mark.asyncio
    async def test_close_saves_to_history(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.delete("/api/chat/slots/s1")
            data = await resp.json()
            assert data["ok"] is True

        msgs = state.conversation_log.read_messages("dashboard:s1")
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_transient_roles_excluded_from_history(self, tmp_path, monkeypatch):
        """chunk, done, queued, permission should not be saved to history."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "run ls")
        slot.append("permission", "ls")
        slot.append("tool", "✅ ls")
        slot.append("queued", "next msg")
        slot.append("chunk", "partial")
        slot.append("done", "")
        slot.append("assistant", "done")
        slot.drain()

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.delete("/api/chat/slots/s1")

        msgs = state.conversation_log.read_messages("dashboard:s1")
        roles = [m["role"] for m in msgs]
        assert "chunk" not in roles
        assert "done" not in roles
        assert "queued" not in roles
        assert "permission" not in roles
        assert roles == ["user", "tool", "assistant"]

    @pytest.mark.asyncio
    async def test_resume_restores_the_channel_filing_marker(self, tmp_path, monkeypatch):
        """Resuming from History must carry the channel-filing marker into memory.

        Four paths rebuild a slot from history. This one (the History page's
        resume endpoint) is the one that reaches a channel conversation the user
        opens by hand. Without the marker in memory the slot reports itself as
        never-filed to every in-memory reader, and the save path would have to
        rescue it from disk.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:filed1", "user", "hello")
        log.update_metadata("dashboard:filed1", {"channel_folder_filed": True})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/filed1/resume", json={"key": "dashboard:filed1"}
            )
            assert resp.status == 200

        assert state._slots["filed1"]._channel_folder_filed is True, (
            "resume dropped the filing marker; the slot now claims it was never "
            "filed and a later save would re-file the conversation"
        )

    @pytest.mark.asyncio
    async def test_resume_restores_model_and_reasoning_effort(self, tmp_path, monkeypatch):
        """Resuming from History must bring back the model and effort the user picked.

        The restart loaders restore both; resume did not, so a reopened session
        ran on the default model, and its next save wrote ``model: ""`` over the
        pick on disk.
        """
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:modeled1", "user", "hello")
        log.update_metadata(
            "dashboard:modeled1", {"model": "test-model-x", "reasoning_effort": "high"}
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/modeled1/resume", json={"key": "dashboard:modeled1"}
            )
            assert resp.status == 200

        slot = state._slots["modeled1"]
        assert slot.model == "test-model-x"
        assert slot.reasoning_effort == "high"

        slot.append("user", "next turn")
        slot.drain()
        _save_slot_to_history(state, slot)
        meta = log._read_metadata("dashboard:modeled1")
        assert meta.get("model") == "test-model-x", "save after resume erased the model"
        assert meta.get("reasoning_effort") == "high"

    @pytest.mark.asyncio
    async def test_resume_ignores_a_non_string_model(self, tmp_path, monkeypatch):
        """A non-string ``model`` in transcript metadata must not crash resume."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:badmodel1", "user", "hello")
        log.update_metadata("dashboard:badmodel1", {"model": {"x": 1}})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/badmodel1/resume", json={"key": "dashboard:badmodel1"}
            )
            assert resp.status == 200

        assert state._slots["badmodel1"].model != {"x": 1}

    @pytest.mark.asyncio
    async def test_no_save_for_unchanged_resumed_session(self, tmp_path, monkeypatch):
        """Resumed session closed without new messages should not re-save."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:hist1", "user", "old msg")
        log.append("dashboard:hist1", "assistant", "old reply")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Resume
            await client.post(
                "/api/chat/slots/hist1/resume",
                json={"key": "dashboard:hist1", "title": "Old Chat"},
            )
            # Close without chatting
            await client.delete("/api/chat/slots/hist1")

        # Original history should be unchanged
        msgs = log.read_messages("dashboard:hist1")
        assert len(msgs) == 2
        assert msgs[0]["content"] == "old msg"

    def test_close_saves_mode_to_history(self, tmp_path, monkeypatch):
        """Slot mode is persisted in session metadata on close."""
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("orch1", mode="design-critique")
        slot.append("user", "plan")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:orch1")
        assert meta.get("mode") == "design-critique"

    def test_close_does_not_persist_trust(self, tmp_path, monkeypatch):
        """Trust flags are ephemeral — not written to session metadata."""
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("t1")
        slot._trust = True
        slot._trust_reads = True
        slot.append("user", "hi")
        slot.drain()
        _save_slot_to_history(state, slot, closed=True)
        meta = state.conversation_log._read_metadata("dashboard:t1")
        assert meta.get("trust") is None
        assert meta.get("trust_reads") is None


# ── Resume deduplication ──


class TestResumeDedupe:
    @pytest.mark.asyncio
    async def test_resume_of_retired_orchestrator_session_is_plain_chat(
        self, tmp_path, monkeypatch
    ):
        """Autopilot retired: a session persisted under mode='orchestrator'
        resumes as plain chat, with its transcript intact."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:orchhist", "user", "plan this")
        log.update_metadata("dashboard:orchhist", {"mode": "orchestrator"})

        async with TestClient(TestServer(_make_app(state))) as client:
            r = await (
                await client.post(
                    "/api/chat/slots/orchhist/resume",
                    json={"key": "dashboard:orchhist", "title": "Auto"},
                )
            ).json()

        assert r["ok"] is True
        assert r["mode"] == ""
        assert state._slots["orchhist"].mode == ""

    @pytest.mark.asyncio
    async def test_resume_from_disk_sends_the_older_history_cursor(self, tmp_path, monkeypatch):
        """A fresh resume must send next_before. The client pages by that field
        and treats its absence as 'no older history', so omitting it strands
        every row outside the returned window."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        for i in range(250):
            log.append("dashboard:deep", "user", f"m{i}")

        async with TestClient(TestServer(_make_app(state))) as client:
            r = await (
                await client.post("/api/chat/slots/deep/resume", json={"key": "dashboard:deep"})
            ).json()

        assert r["ok"] is True
        assert r["has_more"] is True, "250 rows past a 200-row page must report more"
        assert "next_before" in r, "resume omitted the cursor the client pages by"
        assert r["next_before"] == 250 - 200

    @pytest.mark.asyncio
    async def test_resume_existing_slot_cursor_counts_the_frozen_prefix(
        self, tmp_path, monkeypatch
    ):
        """`total` on this branch counts only the in-memory window, so the cursor
        has to add the frozen on-disk prefix back. Without that it points inside
        the range it is meant to skip past, and paging silently loses rows."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.conversation_log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            # A restored slot keeps its older on-disk rows outside slot.messages.
            state._slots["s1"]._disk_older_count = 300
            r = await (
                await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            ).json()

        assert r["ok"] is True
        # window is 1 row and is entirely returned, so the cursor is the prefix.
        assert (
            r["next_before"] == 300
        ), "cursor ignored _disk_older_count; it would page from inside the prefix"
        # A cursor the client is told not to use is dead: every reducer does
        # `hasMore ? nextBefore : 0`, so asserting the value alone would pass
        # while the fix stayed inert for exactly the slot shape it targets.
        assert (
            r["has_more"] is True
        ), "has_more counted only the in-memory window, so the client discards the cursor"

    @pytest.mark.asyncio
    async def test_resume_existing_slot_returns_it(self, tmp_path, monkeypatch):
        """Resuming a session that's already active should return existing slot."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            # First resume
            r1 = await (
                await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            ).json()
            assert r1["ok"] is True

            # Add a message to the active slot
            state._slots["s1"].append("user", "new msg")
            state._slots["s1"].drain()

            # Second resume — should return existing with new msg
            r2 = await (
                await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            ).json()
            assert r2["ok"] is True
            assert r2["total"] == 2  # original + new

            # Should still be one slot, not two
            resp = await client.get("/api/chat/slots")
            slots = await resp.json()
            assert sum(1 for s in slots if s["key"] == "s1") == 1

    @pytest.mark.asyncio
    async def test_resume_close_resume_no_duplicate_history(self, tmp_path, monkeypatch):
        """Resume → close → resume → close should not create duplicate history."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        log.append("dashboard:s1", "assistant", "hi")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Resume and add a message
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 200, await resp.text()
            state._slots["s1"].append("user", "new question")
            state._slots["s1"].append("assistant", "new answer")
            state._slots["s1"].drain()
            await client.delete("/api/chat/slots/s1")
            # Closed BEFORE the second resume begins, whatever the clock's resolution;
            # a refused resume would leave this second cycle with nothing to test.
            close_before_resume(log, "dashboard:s1")

            # Resume again and close without changes
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 200, await resp.text()
            await client.delete("/api/chat/slots/s1")

        # Should have 4 messages (original 2 + new 2), not duplicated
        msgs = log.read_messages("dashboard:s1")
        assert len(msgs) == 4


# ── History key prefix handling ──


class TestHistoryKeyPrefix:
    @pytest.mark.asyncio
    async def test_no_double_dashboard_prefix(self, tmp_path, monkeypatch):
        """Slot key starting with 'dashboard:' should not get double-prefixed.

        get_or_create_slot canonicalizes slot names (strips the transport
        prefix, folds to the filename charset), so resuming a full session key
        registers the slot under the bare canonical key — the API response's
        ``key`` field is authoritative for follow-up calls.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:chat-1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/dashboard:chat-1/resume",
                json={"key": "dashboard:chat-1"},
            )
            slot_key = (await resp.json())["key"]
            assert slot_key == "chat-1"  # canonical: prefix stripped, no fold needed
            state._slots[slot_key].append("user", "new msg")
            state._slots[slot_key].drain()
            await client.delete(f"/api/chat/slots/{slot_key}")

        # Should be saved under dashboard:chat-1, not dashboard:dashboard:chat-1
        msgs = log.read_messages("dashboard:chat-1")
        assert len(msgs) == 2
        assert log.read_messages("dashboard:dashboard:chat-1") == []


class TestHistoryKeyFor:
    """Tests for _history_key_for — canonical history key from slot key."""

    @pytest.mark.parametrize(
        "slot_key, expected",
        [
            pytest.param("dashboard:chat-1-100", "dashboard:chat-1-100", id="already_canonical"),
            pytest.param("dashboard_chat-1-100", "dashboard:chat-1-100", id="strips_single_prefix"),
            pytest.param(
                "dashboard_dashboard_chat-1-100", "dashboard:chat-1-100", id="strips_double_prefix"
            ),
            pytest.param(
                "dashboard_dashboard_dashboard_x", "dashboard:x", id="strips_triple_prefix"
            ),
            pytest.param("chat-1-100", "dashboard:chat-1-100", id="raw_key_gets_prefix"),
        ],
    )
    def test_history_key(self, slot_key: str, expected: str):
        from kiro_crew.dashboard.chat import _history_key_for

        assert _history_key_for(slot_key) == expected


class TestHistoryResumeInterruptedTurnMarker:
    """The History resume endpoint shares the startup restore's marker handling."""

    @staticmethod
    def _saved_state(tmp_path, monkeypatch, name: str, rows: list[tuple], generation: int):
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(name)
        for row in rows:
            slot.append(*row[:3], meta=row[3] if len(row) > 3 else None)
        slot._turn_in_flight_generation = generation
        assert _save_slot_to_history(state, slot, force=True)
        del state._slots[slot.key]
        return state

    @pytest.mark.asyncio
    async def test_resume_reconciles_partial_assistant_and_tool_tail(self, tmp_path, monkeypatch):
        state = self._saved_state(
            tmp_path,
            monkeypatch,
            "resume-interrupted",
            [
                ("user", "inspect it", "msg msg-u"),
                ("assistant", "partial result", "msg msg-a"),
                ("tool", "read complete", "tool", {"done": True}),
            ],
            generation=18,
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/resume-interrupted/resume",
                json={"key": "dashboard:resume-interrupted"},
            )
            payload = await response.json()

        assert response.status == 200
        restored = state.get_slot("resume-interrupted")
        assert restored is not None
        assert restored.to_dict()["interrupted"] is True
        assert restored.messages[-1]["meta"]["kind"] == "gateway_restart_interruption"
        # The paging cursor counts the recovered row, so the next older page
        # starts where this response's rows end.
        assert payload["total"] == 4
        assert payload["messages"][-1]["role"] == "error"
        assert payload["next_before"] == 0
        assert payload["has_more"] is False

    @pytest.mark.asyncio
    async def test_resume_preserves_a_deliberate_stop(self, tmp_path, monkeypatch):
        state = self._saved_state(
            tmp_path,
            monkeypatch,
            "resume-stopped",
            [
                ("user", "inspect it", "msg msg-u"),
                ("system", "Stopped", json.dumps({"kind": "stop_event"})),
            ],
            generation=19,
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/resume-stopped/resume",
                json={"key": "dashboard:resume-stopped"},
            )
            payload = await response.json()

        assert response.status == 200
        restored = state.get_slot("resume-stopped")
        assert restored is not None
        assert restored.to_dict()["interrupted"] is False
        assert all(
            (m.get("meta") or {}).get("kind") != "gateway_restart_interruption"
            for m in restored.messages
        )
        assert payload["total"] == 2

    @pytest.mark.asyncio
    async def test_resume_ignores_a_malformed_boolean_marker(self, tmp_path, monkeypatch):
        state = self._saved_state(
            tmp_path,
            monkeypatch,
            "resume-malformed",
            [("user", "hi", "msg msg-u"), ("assistant", "complete", "msg msg-a")],
            generation=0,
        )
        assert state.conversation_log is not None
        state.conversation_log.update_metadata(
            "dashboard:resume-malformed", {"turn_in_flight_generation": True}
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/resume-malformed/resume",
                json={"key": "dashboard:resume-malformed"},
            )

        assert response.status == 200
        restored = state.get_slot("resume-malformed")
        assert restored is not None
        assert restored.to_dict()["interrupted"] is False
        assert all(
            (m.get("meta") or {}).get("kind") != "gateway_restart_interruption"
            for m in restored.messages
        )
