"""Tests for the mid-turn steer branch in POST /api/chat (api_chat handler).

Exercises the steer path that reaches the running turn's live AcpClient via
``slot._acp_client``:
  * steered success -> broadcasts ``steer_push`` and returns ``{steered: True}``;
  * steer unavailable (no live client) -> safe fall-through to the queue;
  * steer raises -> caught, falls through to the queue (message never dropped).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state


@pytest.fixture
def _patch_sel():
    """Patch sel() so the handler doesn't touch a real SecurityEventLog."""
    mock_sel = MagicMock()
    with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel):
        yield mock_sel


def _running_slot(state, key="test"):
    """Create a slot and make it look like a turn is in flight.

    ``running`` is ``task is not None and not task.done()``.
    """
    slot = state.get_or_create_slot(key)
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


class TestApiChatSteer:
    @pytest.mark.asyncio
    async def test_steer_injects_into_running_turn(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("steered") is True
            assert data.get("queued") is not True

        client_mock.steer.assert_awaited_once_with("go left")
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" in events
        assert "queue_push" not in events  # steered, not queued

    @pytest.mark.asyncio
    async def test_app_authenticated_steer_falls_back_to_fail_closed_queue(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """An app cannot inject into a live human-origin turn and inherit its
        authority; its text waits as an automation-origin successor turn."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._app = "app-A"
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        @web.middleware
        async def _inject_app(request, handler):
            request["app"] = "app-A"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, _inject_app)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        client_mock.steer.assert_not_awaited()
        assert slot._queue[-1].get("_directive_user_origin") is not True

    @pytest.mark.asyncio
    async def test_steer_unavailable_falls_back_to_queue(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = None  # no live client -> cannot steer

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        # message queued (not dropped) and broadcast as queue_push, not steer_push
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "queue_push" in events
        assert "steer_push" not in events

    @pytest.mark.asyncio
    async def test_steer_error_falls_back_to_queue(self, tmp_path, monkeypatch, _patch_sel):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(side_effect=RuntimeError("boom"))
        slot._acp_client = client_mock

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "later", "steer": True}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("queued") is True
            assert data.get("steered") is not True

        client_mock.steer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_steer_cuts_segment_before_user_append(self, tmp_path, monkeypatch, _patch_sel):
        """The segment cut runs BEFORE the steer user message is persisted, so
        the flushed pre-steer assistant text lands ABOVE the steer bubble."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock

        roles_at_cut: list[str] = []

        def _cut() -> None:
            # Snapshot the transcript at cut time, then flush like the real
            # closure does (persist the accumulated segment as assistant).
            roles_at_cut.extend(m["role"] for m in slot.messages)
            slot.append("assistant", "pre-steer text", "msg msg-a")

        slot._steer_segment_cut = _cut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        # Cut ran before the user append: no user message in the snapshot.
        assert "user" not in roles_at_cut
        # Persisted order: pre-steer assistant ABOVE the steer user bubble.
        roles = [m["role"] for m in slot.messages]
        assert roles.index("assistant") < roles.index("user")
        steer_msg = next(m for m in slot.messages if m["role"] == "user")
        assert steer_msg.get("meta", {}).get("steer") is True

    @pytest.mark.asyncio
    async def test_steer_cut_failure_does_not_lose_steer(self, tmp_path, monkeypatch, _patch_sel):
        """A raising cut closure is best-effort: the steer user message is
        still persisted and steer_push still broadcast."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock
        slot._steer_segment_cut = MagicMock(side_effect=RuntimeError("boom"))

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        slot._steer_segment_cut.assert_called_once()
        assert any(m["role"] == "user" and m.get("meta", {}).get("steer") for m in slot.messages)
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" in events

    @staticmethod
    def _steer_capable_state(tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client_mock = MagicMock()
        client_mock.supports_steer = True
        client_mock.steer = AsyncMock(return_value=True)
        slot._acp_client = client_mock
        return state, slot

    @staticmethod
    def _steer_push_payload(state):
        return next(
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "steer_push"
        )

    @pytest.mark.asyncio
    async def test_steer_send_id_persists_and_broadcasts(self, tmp_path, monkeypatch, _patch_sel):
        """A client-minted meta.sendId rides the steer: the persisted steer row
        and the steer_push broadcast both carry it, so the client can reconcile
        its optimistic bubble by id instead of by text (#6075)."""
        state, slot = self._steer_capable_state(tmp_path, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "go left",
                    "steer": True,
                    "meta": {"sendId": "s-abc-123"},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert steer_row["meta"]["sendId"] == "s-abc-123"
        assert self._steer_push_payload(state)["sendId"] == "s-abc-123"

    @pytest.mark.asyncio
    async def test_steer_without_send_id_keeps_prior_shape(self, tmp_path, monkeypatch, _patch_sel):
        """No sendId in the request -> the persisted row's meta and the
        steer_push payload keep exactly the pre-sendId shape (no key at all,
        never a null): old clients see no new field."""
        state, slot = self._steer_capable_state(tmp_path, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"slot": "test", "message": "go left", "steer": True}
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert "sendId" not in steer_row["meta"]
        assert "sendId" not in self._steer_push_payload(state)

    @pytest.mark.asyncio
    async def test_raced_steer_persists_send_id_on_new_turn_row(
        self, tmp_path, monkeypatch, _patch_sel
    ):
        """A steer POST that lands on an IDLE slot (the POST raced chat_done)
        falls onto the new-turn path, whose generic client-meta persistence must
        carry the sendId onto the plain user row — with NO steer flag. That
        non-steer row is exactly what the client reads as proof of the new-turn
        path (#6075), so this pins the pass-through property the frontend half
        of the fix rests on: an allowlist that later drops sendId from persisted
        user meta would reopen the issue with every other test green.

        ``?ws=1`` requests the JSON receipt instead of the SSE stream; the row
        persistence under test happens before that response-shape fork.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")  # idle: no running task

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={
                    "slot": "test",
                    "message": "raced text",
                    "steer": True,
                    "meta": {"sendId": "s-raced-1"},
                },
            )
            assert resp.status == 200
            data = await resp.json()
            assert data.get("ok") is True
            assert data.get("steered") is not True

        user_row = next(m for m in slot.messages if m["role"] == "user")
        assert user_row["meta"]["sendId"] == "s-raced-1"
        assert not user_row["meta"].get("steer")
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "steer_push" not in events

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "bad_send_id",
        [
            123,
            "",
            "x" * 129,
            ["s-1"],
            {"id": "s-1"},
            "has spaces",
            "a/b+c=",
            "AKIAIOSFODNN7EXAMPLE",
        ],
        ids=[
            "non-string",
            "empty",
            "oversized",
            "list",
            "dict",
            "whitespace",
            "base64-charset",
            "credential-shaped",
        ],
    )
    async def test_steer_send_id_invalid_treated_absent(
        self, tmp_path, monkeypatch, _patch_sel, bad_send_id
    ):
        """sendId is raw client input that reaches slot history and the
        steer_push broadcast WITHOUT the outbound redaction message text goes
        through, so the sink refuses anything but the id alphabet — and, since
        a bare alphanumeric key shape fits that alphabet, anything the
        canonical credential scanner would redact (the AWS access-key-id case).
        Every refused value is treated as absent (the old-client shape), never
        persisted or echoed."""
        state, slot = self._steer_capable_state(tmp_path, monkeypatch)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "slot": "test",
                    "message": "go left",
                    "steer": True,
                    "meta": {"sendId": bad_send_id},
                },
            )
            assert resp.status == 200
            assert (await resp.json()).get("steered") is True

        steer_row = next(m for m in slot.messages if m.get("meta", {}).get("steer"))
        assert "sendId" not in steer_row["meta"]
        assert "sendId" not in self._steer_push_payload(state)


class TestFlushSegmentQuietPersist:
    """Pin the steer-cut persistence contract: quiet_persist suppresses the
    per-message chat_message broadcast slot.append emits for the finalized
    assistant message. At the cut boundary every client has already frozen its
    streaming message, so that broadcast would render a duplicate copy of the
    pre-steer text below the steer bubble."""

    def _slot_with_chunks(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("test")
        slot._on_message = MagicMock()
        slot.append("chunk", "pre-steer", "chunk", broadcast=False)
        return state, slot

    def test_quiet_persist_suppresses_message_broadcast(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _flush_segment

        state, slot = self._slot_with_chunks(tmp_path, monkeypatch)
        _flush_segment(state, slot, "pre-steer text", broadcast=False, quiet_persist=True)
        # Persisted (chunks collapsed into the assistant message)…
        roles = [m["role"] for m in slot.messages]
        assert "assistant" in roles and "chunk" not in roles
        # …but with NO chat_message broadcast and NO chat_segment event.
        slot._on_message.assert_not_called()
        events = [c.args[0] for c in state.broadcast_ws.call_args_list]
        assert "chat_segment" not in events

    def test_default_flush_still_broadcasts_message(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _flush_segment

        state, slot = self._slot_with_chunks(tmp_path, monkeypatch)
        _flush_segment(state, slot, "normal segment", broadcast=False)
        slot._on_message.assert_called_once()
