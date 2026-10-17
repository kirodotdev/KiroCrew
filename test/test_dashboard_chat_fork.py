"""Dashboard chat: ``POST /api/chat/slots/{slot}/fork``."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_app_with_agent_routes, _make_state


class TestForkSlot:
    """Tests for POST /api/chat/slots/{slot}/fork."""

    @pytest.mark.asyncio
    async def test_fork_copies_all_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.title = "My Chat"
        slot._titled = True
        slot.append("user", "hello", "msg msg-u")
        slot.append("assistant", "hi there", "msg msg-a")
        slot.append("user", "how are you", "msg msg-u")
        slot.append("assistant", "good", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["messages"] == 4
            assert data["title"] == "↳ Fork of My Chat"

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.forked_from == "dashboard:src"
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 4

    @pytest.mark.asyncio
    async def test_fork_inherits_tags_under_a_fresh_revision(self, tmp_path, monkeypatch):
        """The fork is constructed with an empty tag list under its birth
        revision, then inherits the parent's tags. Those two states must never
        share a `tags_revision`: a slot fetch racing the fork could snapshot the
        empty newborn, and a client seeing that frame under the same revision as
        the inherited list would take it as the next toggle base and erase the
        inherited tags on its first write."""
        from kiro_crew.dashboard.state import _ChatSlot

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.tags = ["tag-a", "tag-b"]
        slot.append("user", "hello", "msg msg-u")
        slot.drain()
        birth_revisions: list[str] = []
        original_init = _ChatSlot.__init__

        def _recording_init(self, *args, **kwargs):
            original_init(self, *args, **kwargs)
            birth_revisions.append(self.tags_revision)

        monkeypatch.setattr(_ChatSlot, "__init__", _recording_init)

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.tags == ["tag-a", "tag-b"]
        assert new_slot.tags is not slot.tags
        assert new_slot.tags_revision not in birth_revisions
        assert new_slot.tags_revision > max(birth_revisions)

    @pytest.mark.asyncio
    async def test_fork_at_message_id(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "msg1", "msg msg-u")
        slot.append("assistant", "reply1", "msg msg-a")
        slot.append("user", "msg2", "msg msg-u")
        slot.append("assistant", "reply2", "msg msg-a")
        slot.drain()
        target_id = slot.messages[1]["meta"]["mid"]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_id": target_id},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == ["msg1", "reply1"]

    @pytest.mark.asyncio
    async def test_fork_at_message_id_takes_precedence_over_window_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "msg1", "msg msg-u")
        slot.append("assistant", "reply1", "msg msg-a")
        slot.append("user", "msg2", "msg msg-u")
        slot.append("assistant", "reply2", "msg msg-a")
        slot.drain()
        target_id = slot.messages[3]["meta"]["mid"]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"at_message_id": target_id, "at_message_index": 0},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 4

    @pytest.mark.asyncio
    async def test_fork_at_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "msg1", "msg msg-u")
        slot.append("assistant", "reply1", "msg msg-a")
        slot.append("user", "msg2", "msg msg-u")
        slot.append("assistant", "reply2", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": 1})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 2
        assert visible[-1]["content"] == "reply1"

    @pytest.mark.asyncio
    async def test_fork_aborts_and_keeps_dirty_when_source_save_fails(self, tmp_path):
        # Regression (double-persistence data-loss): the fork persists the dirty
        # source slot before reading it as the source of truth, then clears
        # `_dirty` (which also disables the periodic retry). If that save is
        # best-effort it can silently drop the write under a lock timeout / I/O
        # error, yet `_dirty` would still be cleared — permanently losing the
        # unwritten source messages on the next restart. The fork must persist
        # with best_effort=False and, on failure, abort (503) WITHOUT clearing
        # `_dirty`, so the periodic flush still retries.
        from unittest.mock import AsyncMock, patch

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "unsaved-source-msg", "msg msg-u")
        slot.append("assistant", "unsaved-reply", "msg msg-a")
        slot.drain()
        # Force the save branch: pretend nothing has reached disk yet.
        slot._dirty = True
        slot._resumed_count = 0

        failing_save = AsyncMock(side_effect=RuntimeError("lock timeout"))
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            with patch("kiro_crew.dashboard.chat_fork.save_slot_off_loop", failing_save):
                resp = await client.post("/api/chat/slots/src/fork", json={})
                assert resp.status == 503

        # best_effort must be explicitly False so the failure propagated.
        assert failing_save.await_count == 1
        assert failing_save.await_args.kwargs.get("best_effort") is False
        # Slot stays dirty → the periodic flush will retry; messages not lost.
        assert slot._dirty is True

    @pytest.mark.asyncio
    async def test_fork_preserves_meta(self, tmp_path):
        # chat_fork.py must preserve the `meta` dict when copying messages into
        # the new slot; dropping it silently breaks every meta-based feature
        # (knowledge chips, paste refs, future inline-comment rewrite badges).
        # Fork must preserve meta verbatim on copied messages.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append(
            "user",
            "find me a citation",
            "msg msg-u",
            meta={"paste_refs": ["ref-abc123"]},
        )
        slot.append(
            "assistant",
            "Here you go",
            "msg msg-a",
            meta={"knowledge_chips": [{"id": "kb-42", "title": "Cite-X"}]},
        )
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 2

        # User message: meta.paste_refs survived. Compared as a subset, not an
        # exact dict: `_ChatSlot.append` also stamps `meta.mid` (the per-row
        # delivery identity) on every persisted row, so an equality assertion
        # here would be asserting the absence of that id rather than the survival
        # of the caller's keys, which is what this regression is about.
        assert visible[0]["role"] == "user"
        user_meta = visible[0].get("meta") or {}
        assert user_meta.get("paste_refs") == [
            "ref-abc123"
        ], f"Fork dropped user meta. Got: {visible[0].get('meta')!r}"

        # Assistant message: meta.knowledge_chips survived
        assert visible[1]["role"] == "assistant"
        assistant_meta = visible[1].get("meta") or {}
        assert assistant_meta.get("knowledge_chips") == [
            {"id": "kb-42", "title": "Cite-X"}
        ], f"Fork dropped assistant meta. Got: {visible[1].get('meta')!r}"

    @pytest.mark.asyncio
    async def test_fork_handles_messages_without_meta(self, tmp_path):
        # The mirror of test_fork_preserves_meta: fork must not INVENT meta keys
        # the parent row did not have. Since `_ChatSlot.append` now stamps
        # `meta.mid` on every row, "no meta at all" is not the observable
        # invariant; the equivalent one is that the forked row's meta matches the
        # parent's exactly -- nothing added, nothing dropped.
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "plain msg", "msg msg-u")
        slot.append("assistant", "plain reply", "msg msg-a")
        slot.drain()
        parent_meta = [m.get("meta") for m in slot.messages if m["role"] in ("user", "assistant")]

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == len(parent_meta)
        for m, expected in zip(visible, parent_meta):
            assert (
                m.get("meta") == expected
            ), f"Fork changed a message's meta. Got: {m.get('meta')!r}, want: {expected!r}"

    @pytest.mark.asyncio
    async def test_fork_not_found(self, tmp_path):
        state = _make_state(tmp_path)
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/nope/fork", json={})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_fork_empty_slot(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("empty")
        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/empty/fork", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_inherits_agent_and_workspace(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", agent="my-agent", workspace="my-ws")
        slot.model = "custom-model"
        slot.mode = "custom-mode"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.agent == "my-agent"
        assert new_slot.workspace == "my-ws"
        assert new_slot.model == "custom-model"
        assert new_slot.mode == "custom-mode"

    @pytest.mark.asyncio
    async def test_fork_of_member_slot_is_not_member_mode(self, tmp_path):
        """A fork of a member DM thread must be an ordinary chat, never a second
        "member" slot: the fork mints a chat-* key, so member mode would make it
        invisible everywhere (excluded from Sessions by surface mode, and absent
        from the roster, whose threads live only on member-<slug> keys)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.mode = "member"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.mode == ""

    @pytest.mark.asyncio
    async def test_fork_mode_override_wins_over_inheritance(self, tmp_path):
        """An allowlisted ``mode`` in the body overrides the source slot's mode.
        The empty string is a legal override, so the override arm is selected on
        ``is not None``, not on truthiness."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.mode = "custom-mode"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"mode": ""})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.mode == ""

    @pytest.mark.asyncio
    async def test_fork_with_retired_orchestrator_mode_is_plain_chat(self, tmp_path):
        """Autopilot retired: a fork that still asks for it is coerced, not refused."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"mode": "orchestrator"})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.mode == ""

    @pytest.mark.asyncio
    async def test_create_with_retired_orchestrator_mode_is_plain_chat(self, tmp_path):
        """Autopilot retired: a create that still asks for it is coerced, not refused."""
        state = _make_state(tmp_path)

        app = _make_app_with_agent_routes(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots", json={"name": "retired-mode", "mode": "orchestrator"}
            )
            assert resp.status == 200

        new_slot = state._slots.get("retired-mode")
        assert new_slot is not None
        assert new_slot.mode == ""

    @pytest.mark.asyncio
    async def test_fork_inherits_folder(self, tmp_path):
        """Fork must land in the same project folder as the source slot."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.folder_id = "proj-abc"
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.folder_id == "proj-abc"

    @pytest.mark.asyncio
    async def test_fork_inherits_empty_folder(self, tmp_path):
        """Fork of an unfoldered slot stays unfoldered (root)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_fork_inherits_project(self, tmp_path):
        """Fork must carry the source slot's active project directory."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.project = str(tmp_path / "repo")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.project == str(tmp_path / "repo")

    @pytest.mark.asyncio
    async def test_fork_inherits_empty_project(self, tmp_path):
        """Fork of a projectless slot stays projectless."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.project == ""

    @pytest.mark.asyncio
    async def test_fork_inherits_tags(self, tmp_path):
        """Fork must carry the source slot's assigned tag ids."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.tags = ["tag-a", "tag-b"]
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        assert new_slot.tags == ["tag-a", "tag-b"]
        # Copied, not aliased: mutating the fork's list must not touch the parent.
        new_slot.tags.append("tag-c")
        assert slot.tags == ["tag-a", "tag-b"]

    @pytest.mark.asyncio
    async def test_fork_inherits_empty_tags(self, tmp_path):
        """Fork of an untagged slot stays untagged."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert new_slot.tags == []

    @pytest.mark.asyncio
    async def test_fork_with_prompt(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "context", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"prompt": "fix the bug"},
            )
            data = await resp.json()
            assert data["ok"] is True
            assert data["prompt"] == "fix the bug"
            assert data["messages"] == 1

        # Prompt is returned for frontend to send separately — must NOT be
        # injected into the forked slot server-side.
        new_slot = state._slots.get(data["key"])
        assert all(m["content"] != "fix the bug" for m in new_slot.messages)

    @pytest.mark.asyncio
    async def test_fork_redacts_credentials_in_assistant_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "show me the key", "msg msg-u")
        slot.append("assistant", "Here: AKIAIOSFODNN7EXAMPLE", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            assert data["ok"] is True

        new_slot = state._slots.get(data["key"])
        assistant_msgs = [m for m in new_slot.messages if m["role"] == "assistant"]
        assert "AKIAIOSFODNN7EXAMPLE" not in assistant_msgs[0]["content"]
        assert "[REDACTED" in assistant_msgs[0]["content"]

    @pytest.mark.asyncio
    async def test_fork_redacts_credentials_in_llm_generated_title(self, tmp_path):
        """Parent title is LLM-generated (via /api/chat/generate-title) and
        flows into the new slot's title + API response + dashboard JSON.
        Must be redacted like any other LLM output (AUTOSDE security-controls).
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.title = "Leaked AKIAIOSFODNN7EXAMPLE key"
        slot._titled = True
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "ok", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()

        assert "AKIAIOSFODNN7EXAMPLE" not in data["title"]
        assert "[REDACTED" in data["title"]
        assert data["title"].startswith("↳ Fork of ")
        new_slot = state._slots.get(data["key"])
        assert "AKIAIOSFODNN7EXAMPLE" not in new_slot.title

    @pytest.mark.asyncio
    async def test_fork_rejects_bool_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": True})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_rejects_negative_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_excludes_system_messages(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("system", "you are helpful", "msg msg-s")
        slot.append("user", "hello", "msg msg-u")
        slot.append("assistant", "hi", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            assert data["messages"] == 2

        new_slot = state._slots.get(data["key"])
        roles = [m["role"] for m in new_slot.messages]
        assert "system" not in roles

    @pytest.mark.asyncio
    async def test_fork_persists_to_disk(self, tmp_path):
        """Forked slot (and forked_from metadata) must survive a save/restore cycle."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "hello", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # Simulate a gateway restart by reading messages + metadata from disk
        from kiro_crew.dashboard.chat import _history_key_for

        hk = _history_key_for(new_key)
        meta = state.conversation_log.get_metadata(hk)
        disk_msgs = state.conversation_log.read_messages(hk)
        assert meta.get("forked_from") == "dashboard:src", f"forked_from not persisted; meta={meta}"
        assert len(disk_msgs) == 2, f"forked messages not persisted (got {len(disk_msgs)})"

    @pytest.mark.asyncio
    async def test_fork_rejects_oversized_prompt(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/src/fork",
                json={"prompt": "x" * 40_000},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_rejects_out_of_range_index(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "hello", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={"at_message_index": 5})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_fork_succeeds_while_streaming(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.append("assistant", "done reply", "msg msg-a")
        slot.drain()

        # Simulate a running session: task attribute non-None + not done
        class _FakeTask:
            def done(self):
                return False

        slot.task = _FakeTask()  # type: ignore[assignment]
        assert slot.running is True

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["messages"] == 2

    @pytest.mark.asyncio
    async def test_fork_emits_sel_audit_event(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        mock_sel.log_api_access.assert_called_once()
        kw = mock_sel.log_api_access.call_args[1]
        assert kw["operation"] == "chat.slot_fork"
        assert kw["outcome"] == "allowed"
        assert "from=src" in kw["resources"]
        assert f"to={data['key']}" in kw["resources"]
        # L5 audit enrichment: at_index + prompt_len present
        assert "at_index=last" in kw["resources"]
        assert "prompt_len=0" in kw["resources"]

    @pytest.mark.asyncio
    async def test_fork_of_incognito_parent_inherits_incognito(self, tmp_path):
        """A restricted session forks; the child is born restricted.

        A refusal would buy no privacy -- the parent's transcript is already on
        disk for tab recovery -- and would only cost the user the conversation.
        What must hold is that the child never LOOSENS the mode: it is registered
        restricted at birth, so consolidation and lessons stay closed to the
        copied content.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", memory_mode="incognito")
        slot.append("user", "secret", "msg msg-u")
        slot.append("assistant", "kept between us", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        assert data["ok"] is True
        assert data["memory_mode"] == "incognito"
        child = state._slots[data["key"]]
        assert child.memory_mode == "incognito"
        assert child.is_restricted is True
        assert child.blocks_reads is False
        assert f"dashboard:{data['key']}" in state._restricted_keys
        assert [m["content"] for m in child.messages] == ["secret", "kept between us"]

    @pytest.mark.asyncio
    async def test_fork_of_temporary_parent_stays_temporary_with_its_history(self, tmp_path):
        """Temporary blocks memory READS, not the session's own thread history.

        Cold provider replay uses the child's live message window, and the
        copied turns also reach the child's own conversation log -- a temporary
        fork is a chat the user reopens from History like any other -- under a
        metadata line that names the temporary mode, so nothing learns from it.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", memory_mode="temporary")
        slot.append("user", "parent question", "msg msg-u")
        slot.append("assistant", "parent answer", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        child = state._slots[data["key"]]
        assert child.memory_mode == "temporary"
        assert child.blocks_reads is True
        assert f"dashboard:{data['key']}" in state._restricted_keys

        from kiro_crew.context import build_session_replay
        from kiro_crew.dashboard.chat import _history_key_for

        history_key = _history_key_for(data["key"])
        visible = [m["content"] for m in child.messages if m.get("role") in ("user", "assistant")]
        assert visible == ["parent question", "parent answer"]
        replay = await asyncio.to_thread(
            build_session_replay,
            state.conversation_log,
            history_key,
            pending_messages=list(child.messages),
        )
        assert "parent question" in replay
        assert "parent answer" in replay
        recent = state.conversation_log.recent(history_key)
        assert [m["content"] for m in recent if m.get("role") in ("user", "assistant")] == [
            "parent question",
            "parent answer",
        ]
        assert state.conversation_log.get_metadata(history_key).get("memory_mode") == "temporary"

    @pytest.mark.asyncio
    async def test_fork_history_visible_to_new_kiro_via_context_builder(self, tmp_path):
        """Forked JSONL is the source build_session_context reads for the new slot.

        Guarantees the fresh kiro-cli process in the forked tab receives the
        copied user/assistant turns as thread-history context.
        """
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "parent question", "msg msg-u")
        slot.append("assistant", "parent answer", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # conversation_log.recent(forked_key) is what ContextBuilder.build_session_context
        # calls to assemble the thread-history section for the new kiro process.
        from kiro_crew.dashboard.chat import _history_key_for

        recent = state.conversation_log.recent(_history_key_for(new_key))
        visible = [m for m in recent if m.get("role") in ("user", "assistant")]
        assert [m["content"] for m in visible] == [
            "parent question",
            "parent answer",
        ], f"fork history not readable as new-session context: {visible}"

    @pytest.mark.asyncio
    async def test_fork_does_not_clone_parent_kiro_session_id(self, tmp_path, monkeypatch):
        """Parent's kiro-cli session id (session_map sid) must NOT carry to fork.

        Cloning the sid would make both tabs share one kiro process state and
        corrupt each other's view. Fork creates a FRESH kiro session on first
        prompt by leaving session_map unset for the new key.
        """
        from kiro_crew.session import SessionMap

        monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
        session_map = SessionMap()
        session_map.set("dashboard:src", "parent-kiro-sid-abc123")
        # A loop-side mutation defers its disk write; the fresh-instance
        # readback below needs the file current NOW.
        session_map.flush()

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            data = await resp.json()
            new_key = data["key"]

        # Re-read from disk so we're not trusting an in-process cache.
        # Inspect _data directly to skip SessionMap.get()'s kiro-session file
        # existence check (we don't spawn real kiro processes in unit tests).
        reloaded = SessionMap()
        assert (
            reloaded._data.get("dashboard:src", {}).get("sid") == "parent-kiro-sid-abc123"
        ), "parent's kiro sid should survive fork unchanged"
        assert (
            f"dashboard:{new_key}" not in reloaded._data
        ), "forked slot must NOT inherit parent's kiro sid"

    @pytest.mark.asyncio
    async def test_fork_of_fork_chains_forked_from(self, tmp_path):
        """M10: fork of a fork titles correctly and `forked_from` points to intermediate, not root."""
        state = _make_state(tmp_path)
        root = state.get_or_create_slot("root")
        root.title = "Original"
        root._titled = True
        root.append("user", "q1", "msg msg-u")
        root.append("assistant", "a1", "msg msg-a")
        root.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            r1 = await client.post("/api/chat/slots/root/fork", json={})
            d1 = await r1.json()
            mid_key = d1["key"]
            assert d1["title"] == "↳ Fork of Original"

            mid = state._slots.get(mid_key)
            mid.append("user", "q2", "msg msg-u")
            mid.append("assistant", "a2", "msg msg-a")
            mid.drain()

            r2 = await client.post(f"/api/chat/slots/{mid_key}/fork", json={})
            d2 = await r2.json()

        leaf = state._slots.get(d2["key"])
        assert d2["title"] == "↳ Fork of Fork of Original"
        assert (
            leaf.forked_from == f"dashboard:{mid_key}"
        ), f"leaf forked_from should point to intermediate, got {leaf.forked_from}"
        assert leaf.forked_from != "dashboard:root"
        visible = [m for m in leaf.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == ["q1", "a1", "q2", "a2"]

    @pytest.mark.asyncio
    async def test_fork_reads_full_history_from_disk_when_memory_capped(self, tmp_path):
        """M12: when in-memory snapshot is smaller than full history, fork reads from disk."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        for i in range(250):
            slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
        slot.drain()
        from kiro_crew.dashboard.chat import _save_slot_to_history

        _save_slot_to_history(state, slot)
        # Simulate restore cap: keep only last 50 in memory.
        # Clear _dirty so the endpoint's flush-if-dirty path doesn't overwrite disk.
        slot.messages = slot.messages[-50:]
        slot._dirty = False

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        assert (
            data["messages"] == 250
        ), f"fork should read full history from disk, got {data['messages']}"
        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert len(visible) == 250
        assert visible[0]["content"] == "m0"
        assert visible[-1]["content"] == "m249"

    @pytest.mark.asyncio
    async def test_fork_preserves_full_history_when_dirty_and_capped(self, tmp_path):
        """A1 regression: _dirty=True + capped in-memory must NOT truncate disk history."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        for i in range(250):
            slot.append("user" if i % 2 == 0 else "assistant", f"m{i}", "msg")
        slot.drain()
        from kiro_crew.dashboard.chat import _save_slot_to_history

        _save_slot_to_history(state, slot)
        # Simulate restore with cap: real path caps messages then sets
        # _resumed_count to the capped length. User then sends new messages.
        slot.messages = slot.messages[-50:]
        slot._resumed_count = len(slot.messages)
        slot.append("user", "new1", "msg")
        slot.append("assistant", "new2", "msg")
        slot.drain()
        assert slot._dirty is True

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        # Full 250 on disk + 2 new dirty messages = 252 total.
        assert (
            data["messages"] == 252
        ), f"fork must preserve full disk history + dirty tail, got {data['messages']}"
        new_slot = state._slots.get(data["key"])
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert visible[0]["content"] == "m0"
        assert visible[-2]["content"] == "new1"
        assert visible[-1]["content"] == "new2"

    @pytest.mark.asyncio
    async def test_fork_concurrent_requests_both_succeed(self, tmp_path):
        """R2-7: two rapid fork requests on the same slot both return 200 with
        identical visible-message counts. Each fork produces an independent new
        slot; no messages lost or duplicated."""
        import asyncio

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "q1", "msg msg-u")
        slot.append("assistant", "a1", "msg msg-a")
        slot.append("user", "q2", "msg msg-u")
        slot.append("assistant", "a2", "msg msg-a")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            r1, r2 = await asyncio.gather(
                client.post("/api/chat/slots/src/fork", json={}),
                client.post("/api/chat/slots/src/fork", json={}),
            )
            assert r1.status == 200 and r2.status == 200
            d1, d2 = await r1.json(), await r2.json()

        assert d1["key"] != d2["key"], "concurrent forks must produce distinct slot keys"
        assert (
            d1["messages"] == d2["messages"] == 4
        ), f"both forks must copy all 4 visible messages, got {d1['messages']}/{d2['messages']}"
        for key in (d1["key"], d2["key"]):
            new_slot = state._slots.get(key)
            visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
            assert [m["content"] for m in visible] == ["q1", "a1", "q2", "a2"]

    @pytest.mark.asyncio
    async def test_fork_audit_records_inherited_memory_mode(self, tmp_path, monkeypatch):
        """The SEL event for a restricted fork is ``allowed`` and names the mode.

        A restricted parent is not a denial ground, but the boundary the child
        inherited must still be visible in the audit log.
        """
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", memory_mode="incognito")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200

        calls = [c[1] for c in mock_sel.log_api_access.call_args_list]
        assert calls, "fork emitted no SEL event"
        assert all(kw["operation"] == "chat.slot_fork" for kw in calls)
        assert not [kw for kw in calls if kw["outcome"] == "denied"]
        allowed = [kw for kw in calls if kw["outcome"] == "allowed"]
        assert len(allowed) == 1
        assert "memory_mode=incognito" in allowed[0]["resources"]

    @pytest.mark.asyncio
    async def test_fork_app_isolation_rejects_cross_app(self, tmp_path, monkeypatch):
        """M-2 regression: app A cannot fork a slot owned by app B."""
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        # The refusal is the per-slot checkpoint's, so its row is written there.
        monkeypatch.setattr("kiro_crew.dashboard.slot_ownership.sel", lambda: mock_sel)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", app="app-B")
        slot.append("user", "secret", "msg msg-u")
        slot.drain()

        # aiohttp middleware populates request["app"]; test injects via middleware.
        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app_obj = _make_app(state)
        app_obj.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            # Cross-app access now returns 404 (indistinguishable from a missing
            # slot) to prevent slot enumeration — the true reason is still
            # recorded server-side via SEL (asserted below).
            assert resp.status == 404
            data = await resp.json()
            assert data["error"] == "not found"

        # denied event logged
        denied_calls = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied_calls) == 1
        assert denied_calls[0][1]["source"] == "app_isolation"

    @pytest.mark.asyncio
    async def test_fork_inherits_app_ownership(self, tmp_path):
        """I-1 regression: new_slot._app is the requesting app (or empty for dashboard)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src", app="app-X")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()

        @web.middleware
        async def inject_app(request, handler):
            request["app"] = "app-X"
            return await handler(request)

        app_obj = _make_app(state)
        app_obj.middlewares.insert(0, inject_app)

        async with TestClient(TestServer(app_obj)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 200
            data = await resp.json()

        new_slot = state._slots.get(data["key"])
        assert (
            new_slot._app == "app-X"
        ), f"forked slot must inherit caller's app, got {new_slot._app!r}"

    @pytest.mark.asyncio
    async def test_fork_rejects_when_slot_cap_reached(self, tmp_path, monkeypatch):
        """Review finding: fork must return 429 + denied audit when slot cap hit."""
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.sel", lambda: mock_sel)
        # Lower the cap so we don't need to create hundreds of slots.
        monkeypatch.setattr("kiro_crew.dashboard.chat_fork.MAX_LIVE_SLOTS", 3)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("src")
        slot.append("user", "hi", "msg msg-u")
        slot.drain()
        # Pre-populate to hit the cap (src + 2 dummies = 3).
        state.get_or_create_slot("dummy1")
        state.get_or_create_slot("dummy2")
        assert len(state._slots) == 3

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/src/fork", json={})
            assert resp.status == 429
            data = await resp.json()
            assert "cap" in data["error"].lower()

        denied = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied) == 1
        assert denied[0][1]["source"] == "rate_limit"

    @pytest.mark.asyncio
    async def test_fork_at_index_spans_chained_session_files(self, tmp_path):
        """Index space must match the frontend (chained), not the current file alone.

        The slot detail endpoint returns
        ``read_messages_chained`` (all sibling session files sharing the
        slot's ``tab_id``), and the frontend builds its fork-button index
        against that list. Pre-fix, fork called ``read_messages`` and
        rejected any index past the current file's boundary.
        """
        from kiro_crew.dashboard.chat_utils import _history_key_for

        state = _make_state(tmp_path)
        tab_id = "tab12345abcd"

        # Older sibling session file with same tab_id (4 visible messages).
        # The chained-read glob matches ``dashboard_chat-*.jsonl`` so the key
        # must start with ``chat-`` to participate in chaining. The chained
        # walker uses ``sorted(glob)`` so file names must lexicographically
        # match chronological order — production uses ``chat-N-<ts>`` which
        # naturally sorts; mirror that with explicit ordering here.
        older_key = "dashboard:chat-tab-1-old"
        state.conversation_log.append(older_key, "user", "old-q1", tab_id=tab_id)
        state.conversation_log.append(older_key, "assistant", "old-a1")
        state.conversation_log.append(older_key, "user", "old-q2")
        state.conversation_log.append(older_key, "assistant", "old-a2")

        # Current session file with same tab_id (2 visible messages).
        current_key = "dashboard:chat-tab-2-new"
        state.conversation_log.append(current_key, "user", "new-q1", tab_id=tab_id)
        state.conversation_log.append(current_key, "assistant", "new-a1")
        state.conversation_log.invalidate_tab_id_cache()

        # In-memory slot mirrors the current file's persisted view; mark
        # clean so the fork handler relies on the chained read alone.
        slot = state.get_or_create_slot("chat-tab-2-new")
        slot._tab_id = tab_id
        slot.append("user", "new-q1", "msg msg-u")
        slot.append("assistant", "new-a1", "msg msg-a")
        slot.drain()
        slot._resumed_count = len(slot.messages)
        slot._dirty = False

        # Frontend visibleIndexMap assigns index 5 to the last "new-a1"
        # (chained list has 6 user/assistant entries: 4 older + 2 new).
        assert _history_key_for("chat-tab-2-new") == current_key
        chained = state.conversation_log.read_messages_chained(current_key)
        assert len([m for m in chained if m.get("role") in ("user", "assistant")]) == 6

        app = _make_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat/slots/chat-tab-2-new/fork",
                json={"at_message_index": 5},
            )
            assert resp.status == 200, await resp.text()
            data = await resp.json()
            assert data["ok"] is True
            assert data["messages"] == 6  # full chained history up to index 5

        new_slot = state._slots.get(data["key"])
        assert new_slot is not None
        visible = [m for m in new_slot.messages if m["role"] in ("user", "assistant")]
        assert [m["content"] for m in visible] == [
            "old-q1",
            "old-a1",
            "old-q2",
            "old-a2",
            "new-q1",
            "new-a1",
        ]
