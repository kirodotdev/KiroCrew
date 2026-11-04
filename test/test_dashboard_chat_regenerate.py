"""Dashboard chat: regenerating a reply and switching between its variants."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

# ── Regenerate + variant switching ──


class TestRegenerateAndVariants:
    @pytest.mark.asyncio
    async def test_regenerate_truncates_and_stashes_variant(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "hello v1")
        turn_stats = {"model": "model-a", "credits": 0.25, "elapsed_ms": 1200}
        secret = "AKIAIOSFODNN7EXAMPLE"
        slot.messages[-1]["meta"] = {"turn_stats": turn_stats}
        slot.messages[-1]["variants"] = [
            {"content": "older variant", "ts": "t0", "turn_stats": {"model": secret}}
        ]
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [m["role"] for m in slot.messages] == ["user"]
        assert len(captured) == 2
        assert captured[0]["content"] == "older variant"
        assert secret not in repr(captured[0]["turn_stats"])
        assert captured[1]["content"] == "hello v1"
        assert captured[1]["turn_stats"] == turn_stats

    @pytest.mark.asyncio
    async def test_regenerate_backfills_stats_on_existing_current_variant(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "current reply")
        stats = {"model": "model-a", "credits": 0.25}
        slot.messages[-1]["meta"] = {"turn_stats": stats}
        slot.messages[-1]["variants"] = [{"content": "current reply", "ts": "t1"}]
        slot.messages[-1]["variant_idx"] = 0
        captured = []
        captured_event = asyncio.Event()

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))
            captured_event.set()

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.wait_for(captured_event.wait(), timeout=5)

        assert len(captured) == 1
        assert captured[0]["turn_stats"] == stats

    def test_variant_stats_are_redacted_when_stashed_or_adopted(self):
        from kiro_crew.dashboard.chat_utils_display import (
            adopt_variant_text,
            variant_from_row,
            with_redacted_turn_stats,
        )

        secret = "AKIAIOSFODNN7EXAMPLE"
        stats = {"model": f"provider-{secret}", "details": {"label": secret}}
        stashed = variant_from_row({"content": "reply", "meta": {"turn_stats": stats}})
        assert secret not in repr(stashed["turn_stats"])

        row = {}
        adopt_variant_text(row, {"content": "reply", "turn_stats": stats})
        assert secret not in repr(row["meta"]["turn_stats"])
        malformed = with_redacted_turn_stats({"content": "reply", "turn_stats": secret})
        assert "turn_stats" not in malformed

    @pytest.mark.asyncio
    async def test_regenerate_rejects_when_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "hello")

        async def _noop():
            # nosemgrep: kirocrew.test-sleep-as-barrier -- not a barrier: a stand-in task that keeps the slot busy; nothing waits on this sleep
            await asyncio.sleep(10)

        slot.task = asyncio.create_task(_noop())
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 409
        finally:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_regenerate_requires_prior_assistant(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "only user")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_updates_content(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200
            assert slot.messages[-1]["content"] == "v1"
            assert slot.messages[-1]["variant_idx"] == 0

    @pytest.mark.asyncio
    async def test_switch_variant_updates_turn_stats(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        old_stats = {"model": "model-a", "credits": 0.25, "elapsed_ms": 1200}
        latest_stats = {"model": "model-b", "credits": 1.5, "elapsed_ms": 4200}
        slot.messages[-1]["meta"] = {"turn_stats": latest_stats}
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1", "turn_stats": old_stats},
            {"content": "v2", "ts": "t2", "turn_stats": latest_stats},
        ]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200
        assert slot.messages[-1]["content"] == "v1"
        assert slot.messages[-1]["meta"]["turn_stats"] == old_stats

    @pytest.mark.asyncio
    async def test_switch_variant_redacts_stats_and_backfills_leaving_variant(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v2")
        row = slot.messages[-1]
        secret = "AKIAIOSFODNN7EXAMPLE"
        row["meta"] = {"turn_stats": {"model": secret, "credits": 1.5}}
        row["variants"] = [
            {"content": "v1", "ts": "t1", "turn_stats": {"model": secret, "credits": 0.25}},
            {"content": "v2", "ts": "t2"},
        ]
        row["variant_idx"] = 1

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200

        assert row["variants"][1]["turn_stats"]["credits"] == 1.5
        assert row["meta"]["turn_stats"]["credits"] == 0.25
        assert secret not in repr(row["variants"])
        assert secret not in repr(row["meta"])

    @pytest.mark.asyncio
    async def test_switch_variant_without_legacy_turn_stats_drops_stale_stats(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["meta"] = {"turn_stats": {"model": "latest"}, "other": "preserved"}
        slot.messages[-1]["variants"] = [{"content": "legacy v1", "ts": "t1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200
        assert slot.messages[-1]["meta"] == {"other": "preserved"}

    @pytest.mark.asyncio
    async def test_switch_away_from_legacy_newest_variant_keeps_its_stats(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        row = slot.messages[-1]
        row["meta"] = {"turn_stats": {"model": "latest", "credits": 0.5}}
        row["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        row["variant_idx"] = 1
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 200
            assert "turn_stats" not in row.get("meta", {})
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 1})
            assert resp.status == 200
        assert row["meta"]["turn_stats"] == {"model": "latest", "credits": 0.5}
        assert "turn_stats" not in row["variants"][0]

    @pytest.mark.asyncio
    async def test_switch_variant_index_out_of_range(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 5})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_passes_hint_to_run_chat(self, tmp_path, monkeypatch):
        """_run_chat should receive a non-empty regenerate_hint kwarg."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()
        mock_run = AsyncMock()
        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=mock_run):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                # Let the scheduled task actually run so the mock records args
                await asyncio.sleep(0)
        mock_run.assert_called_once()
        _args, kwargs = mock_run.call_args
        assert kwargs.get("regenerate_hint"), "regenerate_hint must be non-empty"

    @pytest.mark.asyncio
    async def test_regenerate_preserves_existing_variants(self, tmp_path, monkeypatch):
        """When assistant already has variants[], regenerate keeps them and adds current."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [v["content"] for v in captured] == ["v1", "v2"]

    @pytest.mark.asyncio
    async def test_regenerate_when_active_is_old_variant_no_dup(self, tmp_path, monkeypatch):
        """If user switched back to v1 then regenerates, v1 should not be appended twice."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 0
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert [v["content"] for v in captured] == ["v1", "v2"]

    @pytest.mark.asyncio
    async def test_regenerate_caps_variants(self, tmp_path, monkeypatch):
        """Variant list is capped; oldest entries drop when over _MAX_VARIANTS."""
        from kiro_crew.dashboard.chat import _MAX_VARIANTS

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "newest")
        existing = [{"content": f"v{i}", "ts": f"t{i}"} for i in range(_MAX_VARIANTS)]
        slot.messages[-1]["variants"] = existing
        slot.messages[-1]["variant_idx"] = len(existing) - 1
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        assert len(captured) <= _MAX_VARIANTS
        assert captured[-1]["content"] == "newest"

    @pytest.mark.asyncio
    async def test_regenerate_keeps_the_shown_oldest_version_under_the_cap(
        self, tmp_path, monkeypatch
    ):
        """A full list (the flush appends the new reply past the stash) with the
        user reading its oldest version: that version is the reply being
        replaced, so the cap drops the oldest OTHER version, never it."""
        from kiro_crew.dashboard.chat import _MAX_VARIANTS

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v0")
        existing = [{"content": f"v{i}", "ts": f"t{i}"} for i in range(_MAX_VARIANTS + 1)]
        slot.messages[-1]["variants"] = existing
        slot.messages[-1]["variant_idx"] = 0
        slot.drain()
        captured = []

        async def _capture(*a, **kw):
            captured.extend(list(slot._pending_variants))

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_capture):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                await asyncio.sleep(0)
        contents = [v["content"] for v in captured]
        assert len(contents) == _MAX_VARIANTS
        assert contents[0] == "v0"
        assert "v1" not in contents
        assert contents[-1] == f"v{_MAX_VARIANTS}"

    def test_cap_keeping_shown_falls_back_to_the_text_when_the_index_is_stale(self):
        """A ``variant_idx`` that does not point at the shown text: the newest
        entry holding that text is the one kept."""
        from kiro_crew.dashboard.chat import _MAX_VARIANTS
        from kiro_crew.dashboard.chat_regenerate import _cap_keeping_shown

        full = [{"content": f"v{i}"} for i in range(_MAX_VARIANTS + 2)]
        kept = [v["content"] for v in _cap_keeping_shown(full, "v1", 5)]
        assert len(kept) == _MAX_VARIANTS
        assert kept[0] == "v1"
        assert "v0" not in kept and "v2" not in kept
        newest = [v["content"] for v in _cap_keeping_shown(full, f"v{_MAX_VARIANTS + 1}", None)]
        assert newest == [f"v{i}" for i in range(2, _MAX_VARIANTS + 2)]

    @pytest.mark.asyncio
    async def test_regenerate_rejects_missing_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/nonexistent/regenerate")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_regenerate_rejects_empty_user_message(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "")
        slot.append("assistant", "reply")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_persists_to_disk(self, tmp_path, monkeypatch):
        """After regenerate, on-disk history should reflect the truncation."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "old")
        slot.drain()
        # Save first so a file exists
        from kiro_crew.dashboard.chat import _history_key_for, _save_slot_to_history

        _save_slot_to_history(state, slot)
        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=AsyncMock()):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
        # File should now only contain the user message (assistant truncated)
        key = _history_key_for(slot.key)
        persisted = state.conversation_log.read_messages(key)
        roles = [m.get("role") for m in persisted]
        assert roles == ["user"]

    @pytest.mark.asyncio
    async def test_save_slot_redacts_variants(self, tmp_path, monkeypatch):
        """Variant content and statistics written to disk must be redacted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "safe content")
        # Plant a fake credential inside a variant
        secret = "AKIAIOSFODNN7EXAMPLE"
        slot.messages[-1]["variants"] = [
            {
                "content": f"{secret} secret stuff",
                "ts": "t1",
                "turn_stats": {"model": secret},
            },
            {"content": "safe content", "ts": "t2", "turn_stats": {"model": "safe-model"}},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        from kiro_crew.dashboard.chat import _history_key_for, _save_slot_to_history

        _save_slot_to_history(state, slot)
        key = _history_key_for(slot.key)
        persisted = state.conversation_log.read_messages(key)
        ai = [m for m in persisted if m.get("role") == "assistant"][0]
        assert "variants" in ai
        for v in ai["variants"]:
            assert secret not in repr(v)

    @pytest.mark.asyncio
    async def test_switch_variant_rejects_when_running(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]

        async def _noop():
            # nosemgrep: kirocrew.test-sleep-as-barrier -- not a barrier: a stand-in task that keeps the slot busy; nothing waits on this sleep
            await asyncio.sleep(10)

        slot.task = asyncio.create_task(_noop())
        try:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
                assert resp.status == 409
        finally:
            slot.task.cancel()

    @pytest.mark.asyncio
    async def test_switch_variant_missing_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/none/switch-variant", json={"index": 0})
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_switch_variant_no_variants(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "plain")  # no variants[]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_invalid_json_body(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", data="not-json")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_switch_variant_non_int_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": "abc"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_edit_resend_non_dict_body_is_400_not_500(self, tmp_path, monkeypatch):
        # api_chat_slot_edit_resend parsed the body but never checked
        # isinstance(dict); a valid-JSON array/scalar reached body.get("index")
        # and raised AttributeError -> 500. Must be 400, like switch_variant.
        from kiro_crew.dashboard.chat import api_chat_slot_edit_resend

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/edit-resend", api_chat_slot_edit_resend)
        async with TestClient(TestServer(app)) as client:
            for bad in ("[1, 2]", '"hi"', "42"):
                resp = await client.post(
                    "/api/chat/slots/s1/edit-resend",
                    data=bad,
                    headers={"Content-Type": "application/json"},
                )
                assert resp.status == 400, f"body={bad!r} gave {resp.status}"

    @pytest.mark.asyncio
    async def test_regenerate_clears_pending_on_task_error(self, tmp_path, monkeypatch):
        """If _run_chat raises, _pending_variants must be cleared to prevent leak."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _boom(*a, **kw):
            raise RuntimeError("llm blew up")

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_boom):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                # Let the failing task propagate through done_callback
                for _ in range(5):
                    await asyncio.sleep(0)
        assert slot._pending_variants == [], "pending variants must be cleared when task errors"

    @pytest.mark.asyncio
    async def test_flush_segment_attaches_pending_variants(self, tmp_path, monkeypatch):
        """_flush_segment should attach _pending_variants to the new assistant message."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        # Simulate pending variants from a regenerate
        slot._pending_variants = [
            {"content": "old v1", "ts": "t1"},
            {"content": "old v2", "ts": "t2"},
        ]
        from kiro_crew.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "new reply", broadcast=False)
        last = slot.messages[-1]
        assert last["role"] == "assistant"
        assert last["content"] == "new reply"
        assert len(last["variants"]) == 3  # old v1, old v2, new reply
        assert last["variant_idx"] == 2
        assert slot._pending_variants == []

    @pytest.mark.asyncio
    async def test_switch_variant_negative_index(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": -1})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_regenerate_only_system_and_assistant(self, tmp_path, monkeypatch):
        """Regenerate should fail if there's no user message (only system + assistant)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("system", "you are helpful")
        slot.append("assistant", "hello")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/regenerate")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_flush_segment_no_pending_no_variants(self, tmp_path, monkeypatch):
        """Normal flush without pending variants should not add variants field."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        from kiro_crew.dashboard.chat import _flush_segment

        _flush_segment(state, slot, "reply", broadcast=False)
        last = slot.messages[-1]
        assert "variants" not in last

    @pytest.mark.asyncio
    async def test_switch_variant_missing_index_key(self, tmp_path, monkeypatch):
        """Request body without 'index' key should return 400."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = [{"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_restore_preserves_variants(self, tmp_path, monkeypatch):
        """Variants written to disk should be restored via production code path."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "v2")
        slot.messages[-1]["variants"] = [
            {"content": "v1", "ts": "t1"},
            {"content": "v2", "ts": "t2"},
        ]
        slot.messages[-1]["variant_idx"] = 1
        slot.drain()
        from kiro_crew.dashboard.chat import _save_slot_to_history, restore_recent_sessions

        _save_slot_to_history(state, slot)
        # Clear in-memory state and restore via production path
        state._slots.clear()
        restore_recent_sessions(state, window_minutes=9999)
        restored_slot = state._slots.get("s1")
        assert restored_slot is not None
        ai = [m for m in restored_slot.messages if m.get("role") == "assistant"][0]
        assert "variants" in ai
        assert len(ai["variants"]) == 2
        assert ai["variant_idx"] == 1

    @pytest.mark.asyncio
    async def test_regenerate_clears_pending_on_cancel(self, tmp_path, monkeypatch):
        """If user stops a regeneration (cancel), _pending_variants must be cleared."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _hang(*a, **kw):
            # nosemgrep: kirocrew.test-sleep-as-barrier -- not a barrier: the stubbed turn hangs so the request finds a turn in flight; nothing waits on this sleep
            await asyncio.sleep(999)

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_hang):
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat/slots/s1/regenerate")
                assert resp.status == 200
                assert slot._pending_variants != []
                # Cancel the task (simulates user clicking Stop)
                slot.task.cancel()
                for _ in range(5):
                    await asyncio.sleep(0)
        assert (
            slot._pending_variants == []
        ), "pending variants must be cleared when task is cancelled"

    @pytest.mark.asyncio
    async def test_prepare_messages_redacts_variant_content_and_stats(self, tmp_path, monkeypatch):
        """Variant content and statistics exposed via API must be redacted."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "safe")
        secret = "AKIAIOSFODNN7EXAMPLE"
        slot.messages[-1]["variants"] = [
            {"content": f"{secret} leaked key", "ts": "t1", "turn_stats": {"model": secret}},
            {"content": "safe", "ts": "t2", "turn_stats": {"model": "safe-model"}},
        ]
        from kiro_crew.dashboard.chat import _prepare_messages

        prepared = _prepare_messages(slot.messages, False, live_child="")
        ai = [m for m in prepared if m.get("role") == "assistant"][0]
        for v in ai["variants"]:
            assert secret not in repr(v)

    @pytest.mark.asyncio
    async def test_switch_variant_corrupt_entry(self, tmp_path, monkeypatch):
        """If a variant entry is not a dict, switch-variant should return 400."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("assistant", "v1")
        slot.messages[-1]["variants"] = ["not-a-dict", {"content": "v1"}]
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/switch-variant", json={"index": 0})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_concurrent_regenerate_one_succeeds_one_409(self, tmp_path, monkeypatch):
        """Two simultaneous regenerate requests: one gets 200, the other gets 409."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hi")
        slot.append("assistant", "reply")
        slot.drain()

        async def _hang(*a, **kw):
            # nosemgrep: kirocrew.test-sleep-as-barrier -- not a barrier: the stubbed turn hangs so the request finds a turn in flight; nothing waits on this sleep
            await asyncio.sleep(999)

        with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=_hang):
            async with TestClient(TestServer(_make_app(state))) as client:
                r1, r2 = await asyncio.gather(
                    client.post("/api/chat/slots/s1/regenerate"),
                    client.post("/api/chat/slots/s1/regenerate"),
                )
                statuses = sorted([r1.status, r2.status])
                assert statuses == [200, 409], f"Expected one 200 and one 409, got {statuses}"
        if slot.task:
            slot.task.cancel()
