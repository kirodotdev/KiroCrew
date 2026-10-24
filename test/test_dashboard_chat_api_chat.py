"""Dashboard chat: the ``/api/chat`` handler — request forwarding, receipts, disconnects, stalled readers."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.testing.wait import async_wait_until


@pytest.mark.asyncio
class TestApiChatDrainOnDisconnect:
    """Cover the slot.drain() call in chat_handlers' SSE finally block."""

    async def test_sse_reader_drains_pending_on_cancel(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "partial answer", "chunk")
            # nosemgrep: kirocrew.test-sleep-as-barrier -- not a barrier: the fake turn parks here until the disconnect cancels it; nothing waits on this sleep
            await asyncio.sleep(60)

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "hello", "slot": "s1"},
                timeout=None,
            )
            line = b""
            async for chunk in resp.content.iter_any():
                line += chunk
                if b"partial answer" in line:
                    break

            resp.close()
            # The SSE handler's finally block drains after the close and sets no
            # signal to await, so poll the drained state the asserts below check.
            await async_wait_until(
                lambda: slot._pending == [] and slot._has_reader is False,
                describe=lambda: f"_pending={slot._pending!r} _has_reader={slot._has_reader!r}",
            )

        assert slot._pending == []
        assert slot._has_reader is False


@pytest.mark.asyncio
class TestApiChatMemoryModeForwarding:
    """api_chat propagates body.memory_mode to auto-created slot.

    AgentRock skill dispatch sends `memory_mode: "temporary"` so one-shot
    skill invocations don't bleed into the user's persistent memory. The
    chat endpoint must honor this on the auto-create path because
    AgentRock does not pre-create the slot via /api/chat/slots.
    """

    async def test_temporary_memory_mode_propagates_to_new_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "hello",
                    "slot": "agentrock-skill-1",
                    "memory_mode": "temporary",
                },
                timeout=None,
            )
            async for _chunk in resp.content.iter_any():
                break  # only need to drive slot creation
            resp.close()
            # The closed request's handler sets no signal before it creates the
            # slot, so poll until the auto-created slot appears.
            await async_wait_until(
                lambda: state._slots.get("agentrock-skill-1") is not None,
                describe=lambda: f"slots={list(state._slots)}",
            )

        slot = state._slots.get("agentrock-skill-1")
        assert slot is not None
        assert slot.memory_mode == "temporary"

    async def test_missing_memory_mode_defaults_to_persistent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "hello", "slot": "default-slot"},
                timeout=None,
            )
            async for _chunk in resp.content.iter_any():
                break
            resp.close()
            # The closed request's handler sets no signal before it creates the
            # slot, so poll until the auto-created slot appears.
            await async_wait_until(
                lambda: state._slots.get("default-slot") is not None,
                describe=lambda: f"slots={list(state._slots)}",
            )

        slot = state._slots.get("default-slot")
        assert slot is not None
        assert slot.memory_mode == "persistent"

    async def test_invalid_memory_mode_is_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "hello",
                    "slot": "garbage-mode-slot",
                    "memory_mode": "garbage",
                },
                timeout=None,
            )
            async for _chunk in resp.content.iter_any():
                break
            resp.close()
            # The closed request's handler sets no signal before it creates the
            # slot, so poll until the auto-created slot appears.
            await async_wait_until(
                lambda: state._slots.get("garbage-mode-slot") is not None,
                describe=lambda: f"slots={list(state._slots)}",
            )

        slot = state._slots.get("garbage-mode-slot")
        assert slot is not None
        assert slot.memory_mode == "persistent"

    async def test_mismatched_memory_mode_on_existing_slot_returns_409(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        mock_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers.sel", lambda: mock_sel)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        # Pre-create a persistent slot
        state.get_or_create_slot("locked", memory_mode="persistent")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={
                    "message": "hello",
                    "slot": "locked",
                    "memory_mode": "temporary",
                },
                timeout=None,
            )
            assert resp.status == 409
            data = await resp.json()
            assert "memory_mode" in data["error"]

        # SEL audit event for the denial
        denied_calls = [
            c for c in mock_sel.log_api_access.call_args_list if c[1].get("outcome") == "denied"
        ]
        assert len(denied_calls) == 1
        kw = denied_calls[0][1]
        assert kw["operation"] == "chat_send"
        assert kw["source"] == "memory_mode_mismatch"
        assert "slot=locked" in kw["resources"]


@pytest.mark.asyncio
class TestApiChatModeForwarding:
    """api_chat propagates a validated body.mode to the auto-created slot.

    The Design Critique app's worker slot (mode="design-critique") lives only
    in gateway memory. After a gateway restart the app's next send() recreates
    the slot through this auto-create path; without mode forwarding the slot
    is reborn with mode="" — it then serializes surface="" and passes the chat
    sidebar's surface allowlist, leaking the throwaway worker session into the
    user's chat list. Mirrors TestApiChatMemoryModeForwarding above.
    """

    async def _post_chat(self, state, body):
        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post("/api/chat", json=body, timeout=None)
                assert resp.status == 200
                async for _chunk in resp.content.iter_any():
                    break  # only need to drive slot creation
                resp.close()
                await asyncio.sleep(0.05)

    async def test_design_critique_mode_propagates_to_new_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        await self._post_chat(
            state,
            {
                "message": "critique this",
                "slot": "dc-12345",
                "memory_mode": "temporary",
                "mode": "design-critique",
            },
        )

        slot = state._slots.get("dc-12345")
        assert slot is not None
        assert slot.mode == "design-critique"
        # The leak this guards against: the sidebar allowlist admits the plain
        # "" surface — the recreated worker must not serialize it.
        assert slot.mode != ""

    async def test_bogus_mode_is_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        await self._post_chat(
            state,
            {"message": "hello", "slot": "bogus-mode-slot", "mode": "not-a-mode"},
        )

        slot = state._slots.get("bogus-mode-slot")
        assert slot is not None
        assert slot.mode == ""

    async def test_non_string_mode_is_dropped(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        await self._post_chat(
            state,
            {"message": "hello", "slot": "nonstring-mode-slot", "mode": 42},
        )

        slot = state._slots.get("nonstring-mode-slot")
        assert slot is not None
        assert slot.mode == ""

    async def test_retired_crew_mode_is_dropped(self, tmp_path, monkeypatch):
        """Crew Mode retired: a caller still sending ``mode: "crew"`` (an old
        client, a saved script) gets an ordinary slot, not an error — the mode
        is absent from ``_CREATABLE_MODES``, so it is dropped like any other
        unknown value."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        await self._post_chat(
            state,
            {"message": "hello", "slot": "old-crew-client", "mode": "crew"},
        )

        slot = state._slots.get("old-crew-client")
        assert slot is not None, "slot should still be created, just as plain chat"
        assert slot.mode == ""

    async def test_mode_ignored_for_existing_slot(self, tmp_path, monkeypatch):
        """Repeating mode on send() must be safe when the slot survived: an
        existing slot keeps its stored mode and no error is raised."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("dc-alive", mode="design-critique", memory_mode="temporary")

        await self._post_chat(
            state,
            {
                "message": "hello again",
                "slot": "dc-alive",
                "memory_mode": "temporary",
                "mode": "design-critique",
            },
        )

        slot = state._slots.get("dc-alive")
        assert slot is not None
        assert slot.mode == "design-critique"


@pytest.mark.asyncio
class TestApiChatNoBrowseMarker:
    """Browse is gated by tool AVAILABILITY, not a per-message marker: the chat
    handler injects nothing into the user message, and the agent itself decides
    whether to operate a browser or read with web_fetch. This pins that the
    persisted message is verbatim (no `[BROWSE]` prefix), regardless of any legacy
    `browse` field a client might still send."""

    async def _send(self, tmp_path, monkeypatch, *, body_extra: dict):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            assert _directive_user_origin is True
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        slot_key = body_extra.get("slot", "browse-slot")
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat",
                json={"message": "look at example.com", **body_extra},
                timeout=None,
            )
            async for _chunk in resp.content.iter_any():
                break
            resp.close()
            await asyncio.sleep(0.05)
        return state._slots.get(slot_key)

    async def test_message_is_never_marked(self, tmp_path, monkeypatch):
        slot = await self._send(tmp_path, monkeypatch, body_extra={"slot": "plain-slot"})
        assert slot is not None
        user_msgs = [m for m in slot.messages if m.get("role") == "user"]
        assert user_msgs and user_msgs[-1]["content"] == "look at example.com"

    async def test_legacy_browse_field_is_ignored(self, tmp_path, monkeypatch):
        # A client that still sends the old `browse` field must not change the
        # stored message: the marker mechanism is gone entirely.
        slot = await self._send(
            tmp_path, monkeypatch, body_extra={"slot": "legacy-slot", "browse": True}
        )
        assert slot is not None
        user_msgs = [m for m in slot.messages if m.get("role") == "user"]
        assert user_msgs and not user_msgs[-1]["content"].startswith("[BROWSE]")


# ── Agent passing via /api/chat (AgentRock integration) ──


class TestApiChatAgentPassing:
    @pytest.mark.asyncio
    async def test_agent_set_on_new_slot(self, tmp_path, monkeypatch):
        from kiro_crew.config import loader

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "agentrock--my-aim-agent.json").write_text(
            json.dumps({"name": "my-aim-agent"}), encoding="utf-8"
        )
        monkeypatch.setattr(loader, "kiro_agents_dir", lambda: agents_dir)
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS", frozenset())
        monkeypatch.setattr(loader, "_MATERIALIZED_AGENTS_READY", False)
        await asyncio.to_thread(loader.refresh_materialized_agents)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hello", "slot": "agentrock-my-skill", "agent": "my-aim-agent"},
            )
            data = await resp.json()
            assert resp.status == 200, data
            assert data["ok"] is True
            assert state._slots["agentrock-my-skill"].agent == "my-aim-agent"

    @pytest.mark.asyncio
    async def test_agent_mismatch_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-x")
        slot.agent = "agent-a"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hello", "slot": "slot-x", "agent": "agent-b"},
            )
            assert resp.status == 409

    @pytest.mark.asyncio
    async def test_empty_agent_on_agent_slot_allowed(self, tmp_path, monkeypatch):
        """Follow-up message with no agent on an agent-bound slot must not 409."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-y")
        slot.agent = "agent-a"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "follow-up", "slot": "slot-y", "agent": ""},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True

    @pytest.mark.asyncio
    async def test_invalid_agent_name_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hello", "slot": "s1", "agent": "../evil"},
                )
            assert resp.status == 400
            mock_emit.assert_called_once_with("s1", "../evil", outcome="denied_invalid")

    @pytest.mark.asyncio
    async def test_credential_shaped_invalid_agent_is_redacted_in_sel(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import MagicMock, patch

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("member-dr-eggbot", agent="dr. eggbot", mode="member")
        unsafe = "crew password=shortvalue"
        audit = MagicMock()
        with patch("kiro_crew.dashboard.chat_utils.sel", return_value=audit):
            async with TestClient(TestServer(_make_app(state))) as client:
                response = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hello", "slot": slot.key, "agent": unsafe},
                )

        assert response.status == 400
        event = audit.log.call_args.args[0]
        assert event.agent == "crew password=[REDACTED]"
        assert unsafe not in event.agent

    @pytest.mark.asyncio
    async def test_non_string_agent_logs_actual_value(self, tmp_path, monkeypatch):
        """Fix for Post 22: str(agent) preserves malicious input in audit trail."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hello", "slot": "s1", "agent": 123},
                )
            assert resp.status == 400
            mock_emit.assert_called_once_with("s1", "123", outcome="denied_invalid")

    @pytest.mark.asyncio
    async def test_no_agent_no_emit(self, tmp_path, monkeypatch):
        """Fix for Post 23: no SEL event when no agent involved (reduces audit noise)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import patch

        state = _make_state(tmp_path)
        with patch("kiro_crew.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "no-agent-slot"},
                )
            assert resp.status == 200
            mock_emit.assert_not_called()

    @pytest.mark.asyncio
    async def test_sel_event_on_running_slot_rejection(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from unittest.mock import MagicMock, patch

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-r")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task
        with patch("kiro_crew.dashboard.chat_handlers._emit_agent_assignment") as mock_emit:
            async with TestClient(TestServer(_make_app(state))) as client:
                resp = await client.post(
                    "/api/chat?ws=1",
                    json={"message": "hi", "slot": "slot-r", "agent": "new-agent"},
                )
                assert resp.status == 409
            mock_emit.assert_called_once_with("slot-r", "new-agent", outcome="denied_running")


class TestApiChatSendReceiptMid:
    """The immediate-dispatch receipt carries the user row's server-minted `mid`.

    Correlated dashboard sends also echo the row before their reply starts.
    The receipt and echo must agree on identity regardless of arrival order.
    """

    @pytest.mark.asyncio
    async def test_idle_steer_echoes_user_before_the_reply(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("echo-slot")
        echoed: list[dict] = []
        broadcast_ws = state.broadcast_ws

        def record_echo(kind, data):
            if kind == "chat_message":
                echoed.append(data)
            broadcast_ws(kind, data)

        monkeypatch.setattr(state, "broadcast_ws", record_echo)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            # The frontend can see this turn's first chunk before the HTTP
            # receipt. Its user row must already be on the ordered event stream.
            assert [(row["slot"], row["role"]) for row in echoed] == [("echo-slot", "user")]
            sl.append("assistant", "reply")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={
                    "message": "pasted\ttable\r\n1\t42",
                    "slot": "echo-slot",
                    "steer": True,
                    "meta": {"sendId": "s-echo", "pastes": [{"seq": 1, "content": "table"}]},
                },
            )
            assert resp.status == 200
            receipt = await resp.json()
            await slot.task

        assert len(echoed) == 1
        assert [row["role"] for row in slot.messages] == ["user", "assistant"]
        user = echoed[0]
        assert user["content"] == "pasted\ttable\r\n1\t42"
        assert user["meta"]["sendId"] == "s-echo"
        assert user["meta"]["mid"] == receipt["mid"]
        assert user["meta"]["pastes"][0]["content"] == "table"

    @pytest.mark.asyncio
    async def test_immediate_dispatch_receipt_carries_the_user_row_mid(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
            sl.append("chunk", "ack", "chunk")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "hello", "slot": "mid-slot"},
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["ok"] is True
        # The receipt id must be the SAME id stamped on the appended user row —
        # that is the identity the client stamps on the optimistic bubble and
        # the pin endpoint keys on.
        slot = state._slots["mid-slot"]
        user_rows = [m for m in slot.messages if m["role"] == "user"]
        assert user_rows, "the send must have appended a user row"
        row_mid = user_rows[-1]["meta"]["mid"]
        assert row_mid
        assert data["mid"] == row_mid

    @pytest.mark.asyncio
    async def test_queued_send_receipt_carries_no_mid(self, tmp_path, monkeypatch):
        """A busy slot queues the message and broadcasts its own queue card; the
        optimistic bubble is not this row's receipt, so no `mid` is returned
        (mirrors `confirmedDelivered`, which excludes a queued acceptance)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("busy-slot")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        slot.task = mock_task  # slot.running is True → the busy/queue branch

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat?ws=1",
                json={"message": "queue me", "slot": "busy-slot"},
            )
            assert resp.status == 200
            data = await resp.json()

        assert data["queued"] is True
        assert "mid" not in data


def _long_turn(n_chunks: int, finished: asyncio.Event):
    """A turn that streams *n_chunks* rows of about 1 KB through the real ``append``."""

    async def fake_run_chat(st, sl, msg, *, _directive_user_origin):
        for i in range(n_chunks):
            sl.append("chunk", f"{i:06d}" + "x" * 1024, "chunk")
            if i % 16 == 0:
                await asyncio.sleep(0)
        sl.append("assistant", "final answer", "msg")
        sl.purge_chunks()  # the runner's turn end
        sl.append("done", "", "done")
        finished.set()

    return fake_run_chat


async def _post_and_never_read(client, slot_key: str):
    """A client that sends a whole ``POST /api/chat`` and then never reads its socket."""
    import socket

    host, port = client.server.host, client.server.port
    loop = asyncio.get_running_loop()
    raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    raw.setblocking(False)
    await loop.sock_connect(raw, (host, port))
    body = json.dumps({"message": "hello", "slot": slot_key}).encode()
    head = (
        f"POST /api/chat HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode()
    await loop.sock_sendall(raw, head + body)
    return raw


@pytest.mark.asyncio
class TestApiChatStalledReader:
    """An SSE reader that stops reading is cut off, so it stops holding its slot."""

    async def test_a_stalled_reader_is_cut_off_and_releases_the_slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._SSE_WRITE_DEADLINE_SECS", 0.5, raising=False
        )
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        broadcast: list[str] = []
        slot._on_message = lambda _key, msg: broadcast.append(str(msg.get("content", "")))
        finished = asyncio.Event()
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._run_chat", _long_turn(8000, finished)
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            raw = await _post_and_never_read(client, "s1")
            try:
                await asyncio.wait_for(finished.wait(), 60)
                await async_wait_until(  # the write deadline cuts the stalled reader off
                    lambda: slot._pending_consumers == 0,
                    describe=lambda: f"pending_consumers={slot._pending_consumers}",
                )
                consumers, reader = slot._pending_consumers, slot._has_reader
                held = [m for m in slot._pending if m.get("role") == "chunk"]
                # A row from outside the turn, as a cron or sub-agent notice adds one.
                slot.append("assistant", "a later notice", "msg")
                # The next turn on the slot, sent from a dashboard tab.
                finished.clear()
                sent = await client.post("/api/chat?ws=1", json={"message": "again", "slot": "s1"})
                assert (await sent.json())["ok"] is True
                await asyncio.wait_for(finished.wait(), 60)
                held_next = [m for m in slot._pending if m.get("role") == "chunk"]
            finally:
                raw.close()

        noticed = "a later notice" in broadcast
        assert (consumers, reader, len(held), noticed, len(held_next)) == (0, False, 0, True, 0), (
            f"a stalled SSE reader still holds its slot after the turn: "
            f"pending_consumers={consumers}, _has_reader={reader}, {len(held)} chunk rows "
            f"({sum(len(m['content']) for m in held)} bytes) held, a later row broadcast to other "
            f"tabs={noticed}; after the next turn {len(held_next)} chunk rows "
            f"({sum(len(m['content']) for m in held_next)} bytes) stay held"
        )

    async def test_a_reading_client_gets_the_whole_turn(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        finished = asyncio.Event()
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._run_chat", _long_turn(8000, finished)
        )

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat", json={"message": "hello", "slot": "s1"}, timeout=None
            )
            body = await resp.read()
            await asyncio.wait_for(finished.wait(), 60)

        assert body.endswith(b"data: [DONE]\n\n")
        assert all(f"{i:06d}x".encode() in body for i in (0, 3999, 7999))
        assert slot._pending_consumers == 0 and slot._has_reader is False
        assert not [m for m in slot._pending if m.get("role") == "chunk"]
