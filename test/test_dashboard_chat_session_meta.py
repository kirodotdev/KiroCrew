"""Dashboard chat: session rename, color, color theme, and title generation's session cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


async def _seed_metadata(log, key: str, fields: dict) -> None:
    """Seed a session's metadata the way an async gateway caller must.

    ``update_metadata`` ends in ``atomic_write.replace_with_retry``, which makes a
    single rename attempt on the event-loop thread by design, so a Windows
    sharing violation on the transcript the test just appended to fails the
    setup outright. Offloading it, as ``history_offload`` tells async callers
    to, runs the write on a worker that keeps the bounded retry.
    """
    await asyncio.to_thread(log.update_metadata, key, fields)


# ── Session rename tests ──


class TestSessionRename:
    @pytest.mark.asyncio
    async def test_rename_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slot_title = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": "My Chat"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["title"] == "My Chat"
            assert slot.title == "My Chat"
            assert slot._titled is True
            state.push_slot_title.assert_called_once_with("s1", "My Chat", full=False)

    @pytest.mark.asyncio
    async def test_rename_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/nonexistent/title", json={"title": "X"})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_rename_empty_title(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": "  "})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/s1/title",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_rename_truncates_at_200(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slot_title = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            long_title = "x" * 300
            resp = await client.patch("/api/chat/slots/s1/title", json={"title": long_title})
            data = await resp.json()
            assert resp.status == 200
            assert len(data["title"]) == 200
            assert state._slots["s1"].title == "x" * 200
            state.push_slot_title.assert_called_once_with("s1", "x" * 200, full=False)

    @pytest.mark.asyncio
    async def test_resumed_session_preserves_title(self, tmp_path, monkeypatch):
        """Resumed session should set _titled=True so auto-title doesn't overwrite."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard:s1", "title": "My Custom Title"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.title == "My Custom Title"
            assert slot._titled is True

    @pytest.mark.asyncio
    async def test_resumed_session_restores_tags_and_auto_tagged(self, tmp_path, monkeypatch):
        """Regression: resume must restore tags AND the auto-tag once-flag.
        Without the flag, resuming a session whose auto-tag the user removed
        re-runs maybe_auto_tag on the next message and silently re-adds it."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        # The id must exist in the vocabulary — resume prunes unknown ids
        # (crash-atomic delete can leave dangling ids on disk).
        state._tags.append({"id": "tag-abc", "name": "ABC", "color": "#6b7280", "order": 0})
        await _seed_metadata(
            log,
            "dashboard:s1",
            {"tags": ["tag-abc"], "auto_tagged": True, "project": "/x/repos/MyRepo"},
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/resume",
                json={"key": "dashboard:s1"},
            )
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.tags == ["tag-abc"]
            assert slot._auto_tagged is True

    @pytest.mark.asyncio
    async def test_resume_unreadable_vocab_does_not_wipe_tags(self, tmp_path, monkeypatch):
        """FAIL-OPEN: if tags.json failed to load (vocabulary UNKNOWN), resume
        must NOT prune — pruning against an unknown vocab would wipe every
        assignment and the next save persists the loss."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._tags = []
        state._tags_authoritative = False  # load_tags() hit a parse/I/O error
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        await _seed_metadata(log, "dashboard:s1", {"tags": ["tag-abc"], "auto_tagged": True})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.tags == ["tag-abc"]  # preserved, not wiped
            assert slot._auto_tagged is True

    @pytest.mark.asyncio
    async def test_resume_empty_authoritative_vocab_prunes_dangling_ids(
        self, tmp_path, monkeypatch
    ):
        """A legitimately-empty vocabulary (last tag deleted, tags.json parsed
        fine as []) IS authoritative: resume must prune the dangling id, or a
        crash between the vocab commit and slot cleanup would resurrect the
        deleted tag id on this slot forever."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state._tags = []
        state._tags_authoritative = True  # tags.json parsed OK as []
        log = state.conversation_log
        log.append("dashboard:s1", "user", "hello")
        await _seed_metadata(log, "dashboard:s1", {"tags": ["tag-abc"], "auto_tagged": True})

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/resume", json={"key": "dashboard:s1"})
            assert resp.status == 200
            slot = state._slots["s1"]
            assert slot.tags == []  # dangling id pruned
            assert slot._auto_tagged is True

    def test_load_tags_sets_authoritative_flag(self, tmp_path, monkeypatch):
        """load_tags() marks the vocabulary authoritative on a clean parse
        (including a legitimately-empty []) and NOT authoritative on a parse
        failure — the signal the restore-time pruning fail-open relies on."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        # Legitimately-empty vocabulary: parsed OK -> authoritative.
        (tmp_path / "tags.json").write_text("[]", encoding="utf-8")
        state._tags = []
        state._tags_authoritative = False
        state.load_tags()
        assert state._tags_authoritative is True
        assert state._tags == []

        # Corrupt file: parse failure -> NOT authoritative, data untouched.
        (tmp_path / "tags.json").write_text("{not json", encoding="utf-8")
        state._tags = [{"id": "keep-me", "name": "Keep", "status": False}]
        state._tags_authoritative = True
        state.load_tags()
        assert state._tags_authoritative is False
        assert state._tags[0]["id"] == "keep-me"  # not re-seeded, not wiped

        # Valid JSON but NOT a list (e.g. {}): vocabulary state is unknown,
        # same as a parse failure -- NOT authoritative, data untouched.
        (tmp_path / "tags.json").write_text("{}", encoding="utf-8")
        state._tags = [{"id": "keep-me", "name": "Keep", "status": False}]
        state._tags_authoritative = True
        state.load_tags()
        assert state._tags_authoritative is False
        assert state._tags[0]["id"] == "keep-me"


# ── Session color tests ──


class TestSessionColor:
    @pytest.mark.asyncio
    async def test_set_color_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 3})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["color_index"] == 3
            assert state._slots["s1"].color_index == 3
            state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    async def test_set_color_null(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.color_index = 5

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": None})
            data = await resp.json()
            assert resp.status == 200
            assert data["color_index"] is None
            assert slot.color_index is None

    @pytest.mark.asyncio
    async def test_set_color_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/nope/color", json={"color_index": 0})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_set_color_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/s1/color",
                data=b"not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_color_negative_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "color_index",
        [
            pytest.param(True, id="set_color_bool_rejected"),
            pytest.param(99999, id="set_color_large_index"),
        ],
    )
    async def test_set_color_rejected(self, tmp_path, monkeypatch, color_index):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": color_index})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_set_color_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 0})
            data = await resp.json()
            assert resp.status == 200
            assert data["color_index"] == 0
            assert slot.color_index == 0

    @pytest.mark.parametrize(
        "color_index",
        [
            pytest.param(0, id="color_zero_persisted"),
            pytest.param(4, id="color_persisted_in_history"),
        ],
    )
    def test_color_persisted(self, tmp_path, monkeypatch, color_index):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_index = color_index
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("color_index") == color_index

    def test_color_null_not_persisted(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert "color_index" not in meta

    @pytest.mark.asyncio
    async def test_set_color_hex_success_and_lowercased(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_hex": "#A1B2C3"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert data["color_hex"] == "#a1b2c3"
            assert state._slots["s1"].color_hex == "#a1b2c3"
            state.push_slots_update.assert_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad", ["#GGGGGG", "#abc", "abcdef", "#abcdef00", 123, True, ["#abcdef"]]
    )
    async def test_set_color_hex_malformed_rejected(self, tmp_path, monkeypatch, bad):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_hex": bad})
            assert resp.status == 400
            assert state._slots["s1"].color_hex is None

    @pytest.mark.asyncio
    async def test_color_hex_and_index_mutually_exclusive(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            # Setting a hex clears an existing index.
            slot.color_index = 4
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_hex": "#112233"})
            data = await resp.json()
            assert resp.status == 200
            assert data == {"ok": True, "color_index": None, "color_hex": "#112233"}
            assert slot.color_index is None and slot.color_hex == "#112233"
            # Setting an index clears the hex.
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": 2})
            data = await resp.json()
            assert data == {"ok": True, "color_index": 2, "color_hex": None}
            assert slot.color_index == 2 and slot.color_hex is None

    @pytest.mark.asyncio
    async def test_color_index_only_patch_keeps_existing_hex_when_null(self, tmp_path, monkeypatch):
        """An index-only PATCH with null must not silently null a hex (in-body gating)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.color_hex = "#445566"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_index": None})
            assert resp.status == 200
            assert slot.color_hex == "#445566"

    @pytest.mark.asyncio
    async def test_color_hex_null_clears(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.push_slots_update = MagicMock()
        slot = state.get_or_create_slot("s1")
        slot.color_hex = "#778899"

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.patch("/api/chat/slots/s1/color", json={"color_hex": None})
            data = await resp.json()
            assert resp.status == 200
            assert data["color_hex"] is None
            assert slot.color_hex is None

    @pytest.mark.asyncio
    async def test_color_hex_in_to_dict(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_hex = "#0a0b0c"
        assert slot.to_dict()["color_hex"] == "#0a0b0c"

    @pytest.mark.asyncio
    async def test_color_hex_persisted_in_history(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat import _save_slot_to_history

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.color_hex = "#0a0b0c"
        slot.append("user", "hello")
        slot.drain()

        _save_slot_to_history(state, slot, closed=True)

        meta = state.conversation_log._read_metadata("dashboard:s1")
        assert meta.get("color_hex") == "#0a0b0c"


# ── Background session leak regression ──


class TestTitleGenerationSessionLeak:
    """_generate_title_via_kiro must destroy its ephemeral bg session even on error."""

    @pytest.mark.asyncio
    async def test_background_session_destroyed_on_stream_error(self, tmp_path):
        from kiro_crew.dashboard.chat import _generate_title_via_kiro

        state = _make_state(tmp_path)

        # Mock session whose prompt() raises mid-iteration
        mock_client = MagicMock()
        mock_client.destroy = AsyncMock()

        async def _exploding_prompt(prompt, *, allow_image=True):
            raise RuntimeError("throttle / ACP error")
            yield  # noqa: unreachable — makes this an async generator

        mock_client.prompt = _exploding_prompt
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]

        with pytest.raises(RuntimeError, match="throttle"):
            await _generate_title_via_kiro(state, messages)

        # The critical assertion: destroy MUST be called even though prompt() raised
        mock_client.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_permission_request_rejected_during_title_gen(self, tmp_path):
        from kiro_crew.dashboard.chat import _generate_title_via_kiro
        from kiro_crew.providers.base import (
            EVENT_COMPLETE,
            EVENT_PERMISSION_REQUEST,
            EVENT_TEXT_CHUNK,
            LLMEvent,
        )

        state = _make_state(tmp_path)
        mock_client = MagicMock()
        mock_client.reject_tool = AsyncMock()
        mock_client.destroy = AsyncMock()

        async def _prompt(prompt, *, allow_image=True):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="My Title")
            yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id="req-1")
            yield LLMEvent(kind=EVENT_COMPLETE)

        mock_client.prompt = _prompt
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        title = await _generate_title_via_kiro(state, messages)

        mock_client.reject_tool.assert_called_once_with("req-1")
        assert title == "My Title"
        mock_client.destroy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_complete_event_breaks_stream(self, tmp_path):
        from kiro_crew.dashboard.chat import _generate_title_via_kiro
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

        state = _make_state(tmp_path)
        mock_client = MagicMock()
        mock_client.destroy = AsyncMock()

        async def _prompt(prompt, *, allow_image=True):
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Good")
            yield LLMEvent(kind=EVENT_COMPLETE)
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=" SHOULD NOT APPEAR")

        mock_client.prompt = _prompt
        state.sessions.get_bg_session = AsyncMock(return_value=mock_client)

        messages = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
        title = await _generate_title_via_kiro(state, messages)

        assert title == "Good"
        mock_client.destroy.assert_awaited_once()


# ── Color theme tests ──


class TestColorTheme:
    """Tests for color_theme validation and slot assignment. Only "" and
    ``custom-<slug>`` (installed packs) are valid; any built-in visual-theme
    slug or junk value is coerced to "" (no persona path)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "color_theme, stored",
        [
            pytest.param("custom-mypack", "custom-mypack", id="color_theme_set_on_slot"),
            pytest.param("evil", "", id="invalid_color_theme_coerced_to_empty"),
            pytest.param(42, "", id="non_string_color_theme_coerced"),
        ],
    )
    async def test_color_theme(self, tmp_path, monkeypatch, color_theme, stored):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": color_theme},
                )
                assert resp.status == 200
                assert state._slots["theme-slot"].color_theme == stored

    @pytest.mark.asyncio
    async def test_color_theme_cleared_to_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("theme-slot")
        slot.color_theme = "custom-mypack"
        with patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot", "color_theme": ""},
                )
                assert resp.status == 200
                assert slot.color_theme == ""

    @pytest.mark.asyncio
    async def test_color_theme_not_cleared_when_absent(self, tmp_path, monkeypatch):
        """Omitting color_theme from body must not reset an existing theme."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("theme-slot")
        slot.color_theme = "custom-mypack"
        with patch("kiro_crew.dashboard.chat_handlers._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "theme-slot"},
                )
                assert resp.status == 200
                assert slot.color_theme == "custom-mypack"
