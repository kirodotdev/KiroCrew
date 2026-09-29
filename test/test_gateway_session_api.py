"""Generic Gateway session API behavior and isolation."""

from __future__ import annotations

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat import api_chat_slot_mcp, api_chat_slot_project
from kiro_crew.dashboard.chat_handlers import RESERVED_ROW_META_KEYS
from kiro_crew.dashboard.chat_runner import (
    _finish_queue_cycle,
    _start_next_queued_turn,
    _turn_session_mcp_servers,
)
from kiro_crew.dashboard.handlers.ask_question import (
    api_ask_question_slot_answer,
    api_ask_question_slot_pending,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot, stage_boundary_for
from kiro_crew.dashboard.websocket_hub import WebSocketHub
from kiro_crew.dashboard.ws import _session_subscription_keys
from kiro_crew.gateway.constants import (
    MAX_SESSION_EVENT_KEY_CHARS,
    MAX_SESSION_EVENT_KEYS,
    MCP_OWNER_HEADER,
    QUEUED_GATEWAY_MCP_META_KEY,
    SESSION_MESSAGE_EVENT,
    SESSION_PLAN_EVENT,
    TURN_ORIGIN_HEADER,
)
from kiro_crew.session import SessionManager, _mcp_fingerprint


def _server(name: str, script: str = "") -> dict:
    return {"name": name, "command": sys.executable, "args": ["-c", script]}


def _state_with(slot: _ChatSlot) -> MagicMock:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    return state


def _internal_app(state: object, *, app_name: str = "") -> web.Application:
    @web.middleware
    async def internal(request: web.Request, handler):
        request["internal_auth"] = True
        if app_name:
            request["app"] = app_name
        return await handler(request)

    app = web.Application(middlewares=[internal])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/mcp", api_chat_slot_mcp)
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    app.router.add_get("/api/chat/slots/{slot_key}/questions", api_ask_question_slot_pending)
    app.router.add_post(
        "/api/chat/slots/{slot_key}/questions/{card_id}/answer",
        api_ask_question_slot_answer,
    )
    return app


class TestSessionMcpRoute:
    @pytest.mark.asyncio
    async def test_remote_slot_refuses_configured_registration(self) -> None:
        slot = _ChatSlot("slot-a")
        slot.executor = "remote"
        slot.instance_id = "peer-a"
        slot.remote_slot = "peer-slot-a"
        state = _state_with(slot)
        async with TestClient(TestServer(_internal_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/slot-a/mcp",
                json={"servers": [], "owner": "client-a"},
            )
            body = await response.json()

        assert response.status == 409
        assert body["code"] == "remote_slot_unsupported"
        assert slot.session_mcp_servers == []
        assert slot.session_mcp_owner == ""
        assert slot._session_mcp_configured is False

    @pytest.mark.asyncio
    async def test_registers_canonical_servers(self) -> None:
        slot = _ChatSlot("slot-a")
        state = _state_with(slot)
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            async with TestClient(TestServer(_internal_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/slot-a/mcp",
                    json={
                        "servers": [
                            {
                                "name": "filesystem",
                                "command": "mcp-fs",
                                "args": ["--root", "/repo"],
                                "env": {"MODE": "readonly"},
                            }
                        ],
                        "owner": "client-a",
                        "mutation_id": "register-1",
                    },
                )
                assert response.status == 200
                body = await response.json()
        assert body["servers"] == ["filesystem"]
        assert body["applied"] is True
        assert slot.session_mcp_owner == "client-a"
        assert slot._session_mcp_configured is True
        assert slot.session_mcp_servers == [
            {
                "name": "filesystem",
                "command": "mcp-fs",
                "args": ["--root", "/repo"],
                "env": [{"name": "MODE", "value": "readonly"}],
            }
        ]

    @pytest.mark.asyncio
    async def test_empty_replace_is_explicit_and_clear_releases_ownership(self) -> None:
        slot = _ChatSlot("slot-a")
        state = _state_with(slot)
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            async with TestClient(TestServer(_internal_app(state))) as client:
                replaced = await client.post(
                    "/api/chat/slots/slot-a/mcp",
                    json={"servers": [], "owner": "client-a"},
                )
                assert await replaced.json() == {
                    "ok": True,
                    "servers": [],
                    "generation": slot._session_mcp_generation,
                    "configured": True,
                    "applied": True,
                }
                assert slot._session_mcp_configured is True
                cleared = await client.post(
                    "/api/chat/slots/slot-a/mcp",
                    json={"mode": "clear_if_owner", "servers": [], "owner": "client-a"},
                )
                assert (await cleared.json())["configured"] is False
        assert slot.session_mcp_servers == []
        assert slot.session_mcp_owner == ""
        assert slot._session_mcp_configured is False

    @pytest.mark.asyncio
    async def test_configured_registration_requires_nonempty_owner(self) -> None:
        slot = _ChatSlot("slot-a")
        state = _state_with(slot)
        async with TestClient(TestServer(_internal_app(state))) as client:
            replaced = await client.post(
                "/api/chat/slots/slot-a/mcp",
                json={"servers": []},
            )
            restored = await client.post(
                "/api/chat/slots/slot-a/mcp",
                json={
                    "mode": "restore_if_owner",
                    "servers": [],
                    "configured": True,
                    "expected_owner": "client-a",
                },
            )
        assert replaced.status == 400
        assert restored.status == 400
        assert slot._session_mcp_configured is False
        assert slot.session_mcp_owner == ""

    @pytest.mark.asyncio
    async def test_derived_app_identity_cannot_use_internal_exemption(self) -> None:
        state = _state_with(_ChatSlot("slot-a"))
        async with TestClient(TestServer(_internal_app(state, app_name="app-a"))) as client:
            mcp = await client.post("/api/chat/slots/slot-a/mcp", json={"servers": []})
            questions = await client.get("/api/chat/slots/slot-a/questions")
        assert mcp.status == 403
        assert questions.status == 403

    @pytest.mark.asyncio
    async def test_mutation_retry_replays_without_overwriting_new_owner(self) -> None:
        slot = _ChatSlot("slot-a")
        state = _state_with(slot)
        first = {
            "servers": [{"name": "first", "command": "/first"}],
            "owner": "client-a",
            "mutation_id": "stable-id",
        }
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            async with TestClient(TestServer(_internal_app(state))) as client:
                initial = await client.post("/api/chat/slots/slot-a/mcp", json=first)
                initial_body = await initial.json()
                await client.post(
                    "/api/chat/slots/slot-a/mcp",
                    json={
                        "servers": [{"name": "newer", "command": "/newer"}],
                        "owner": "client-b",
                        "mutation_id": "newer-id",
                    },
                )
                replay = await client.post("/api/chat/slots/slot-a/mcp", json=first)
                assert await replay.json() == initial_body
        assert slot.session_mcp_owner == "client-b"
        assert [item["name"] for item in slot.session_mcp_servers] == ["newer"]

    @pytest.mark.asyncio
    async def test_reused_mutation_id_with_other_payload_conflicts(self) -> None:
        slot = _ChatSlot("slot-a")
        state = _state_with(slot)
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            async with TestClient(TestServer(_internal_app(state))) as client:
                first = await client.post(
                    "/api/chat/slots/slot-a/mcp",
                    json={"servers": [], "owner": "client-a", "mutation_id": "same"},
                )
                assert first.status == 200
                conflict = await client.post(
                    "/api/chat/slots/slot-a/mcp",
                    json={
                        "servers": [{"name": "other", "command": "/other"}],
                        "owner": "client-a",
                        "mutation_id": "same",
                    },
                )
                assert conflict.status == 409
                assert (await conflict.json())["code"] == "mutation_conflict"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler", "payload"),
        [
            (
                api_chat_slot_mcp,
                {"servers": [{"name": "new", "command": "/new"}], "owner": "client-a"},
            ),
            (api_chat_slot_project, {"project": ""}),
        ],
    )
    async def test_slot_replacement_during_body_read_is_refused(self, handler, payload) -> None:
        slot = _ChatSlot("slot-a")
        replacement = _ChatSlot("slot-a")
        state = _state_with(slot)

        class _Request:
            app = {"state": state}
            match_info = {"slot": "slot-a"}

            def get(self, key, default=""):
                return True if key == "internal_auth" else default

        async def _read_body(_request):
            state._slots["slot-a"] = replacement
            return payload, None

        with (
            patch("kiro_crew.dashboard.chat_handlers.read_bounded_json", _read_body),
            patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()),
        ):
            response = await handler(_Request())

        assert response.status == 404
        assert json.loads(response.text)["code"] == "slot_not_found"
        assert slot._session_mcp_configured is False
        assert replacement._session_mcp_configured is False
        assert slot.project == ""
        assert replacement.project == ""

    @pytest.mark.asyncio
    async def test_turn_refuses_mcp_owner_configured_during_private_store_admission(
        self, tmp_path, monkeypatch
    ) -> None:
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)

        async def replace_mcp_owner(*_args, **_kwargs):
            slot = state._slots["slot-a"]
            slot.session_mcp_servers = [_server("new-owner")]
            slot.session_mcp_owner = "client-b"
            slot._session_mcp_configured = True
            slot._session_mcp_generation = "replacement-generation"
            return ""

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers.pin_private_agent_store", replace_mcp_owner
        )
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._record_explicit_agent_selection",
            AsyncMock(return_value=None),
        )

        @web.middleware
        async def internal(request, handler):
            request["internal_auth"] = True
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, internal)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat",
                headers={TURN_ORIGIN_HEADER: "slot-a"},
                json={"slot": "slot-a", "message": "hello"},
            )
            body = await response.json()

        assert response.status == 409
        assert body["code"] == "mcp_owner_stale"
        assert state._slots["slot-a"].messages == []
        assert state._slots["slot-a"].task is None


class TestTurnOriginAdmission:
    @pytest.mark.asyncio
    async def test_turn_refuses_origin_that_does_not_match_body_slot(
        self, tmp_path, monkeypatch
    ) -> None:
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("slot-a")
        target = state.get_or_create_slot("slot-b")
        run_chat = AsyncMock()
        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", run_chat)

        @web.middleware
        async def internal(request, handler):
            request["internal_auth"] = True
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, internal)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat",
                headers={TURN_ORIGIN_HEADER: "slot-a"},
                json={"slot": "slot-b", "message": "wrong conversation"},
            )
            body = await response.json()

        assert response.status == 409
        assert body["code"] == "invalid_origin"
        assert target.messages == []
        assert target.task is None
        assert target._human_seen is False
        run_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_turn_refuses_an_owner_released_before_admission(
        self, tmp_path, monkeypatch
    ) -> None:
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-a")

        @web.middleware
        async def internal(request, handler):
            request["internal_auth"] = True
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, internal)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat",
                headers={TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"},
                json={"slot": "slot-a", "message": "hello"},
            )
            body = await response.json()

        assert response.status == 409
        assert body["code"] == "mcp_owner_stale"
        assert slot.messages == []
        assert slot.task is None

    @pytest.mark.asyncio
    async def test_app_derived_internal_turn_cannot_stamp_gateway_origin(
        self, tmp_path, monkeypatch
    ) -> None:
        from chat_test_helpers import _make_app, _make_state

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-a", app="test-app")

        async def fake_run_chat(_state, owned_slot, _message, **_kwargs):
            owned_slot.append("assistant", "ack")
            owned_slot.append("done", "", "done")

        monkeypatch.setattr("kiro_crew.dashboard.chat_handlers._run_chat", fake_run_chat)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._maybe_auto_title",
            AsyncMock(return_value=None),
        )

        @web.middleware
        async def internal_app(request, handler):
            request["internal_auth"] = True
            request["app"] = "test-app"
            return await handler(request)

        app = _make_app(state)
        app.middlewares.insert(0, internal_app)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/api/chat",
                headers={TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"},
                json={"slot": "slot-a", "message": "hello"},
            )
            await response.read()

        assert response.status == 200
        assert [row["role"] for row in slot.messages] == ["user", "assistant", "done"]
        assert all("_gateway_turn_origin" not in row.get("meta", {}) for row in slot.messages)


class TestProjectMutationReceipts:
    @pytest.mark.asyncio
    async def test_generation_and_mutation_conflict(self, tmp_path) -> None:
        slot = _ChatSlot("slot-a")
        state = _state_with(slot)
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            async with TestClient(TestServer(_internal_app(state))) as client:
                first = await client.post(
                    "/api/chat/slots/slot-a/project",
                    json={"project": "", "mutation_id": "project-1"},
                )
                assert first.status == 200
                body = await first.json()
                assert body["generation"] == slot._project_generation
                conflict = await client.post(
                    "/api/chat/slots/slot-a/project",
                    json={"project": str(tmp_path), "mutation_id": "project-1"},
                )
                assert conflict.status == 409
                assert (await conflict.json())["code"] == "mutation_conflict"


class TestSlotQuestionApi:
    @pytest.mark.asyncio
    async def test_pending_and_answer_return_completed_prompt(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [
                {
                    "question": "Which path?",
                    "options": [{"label": "Safe"}, {"label": "Fast"}],
                }
            ],
        }
        state = _state_with(slot)
        state.pending_question_cards.side_effect = (
            lambda key: DashboardState.pending_question_cards(state, key)
        )
        state.answer_question_card.side_effect = (
            lambda key, card, answers: DashboardState.answer_question_card(
                state, key, card, answers
            )
        )
        state._broadcast_question_retired = MagicMock()
        state._push_slots = MagicMock()
        running = MagicMock()
        running.done.return_value = False
        slot.task = running
        with patch("kiro_crew.dashboard.handlers.ask_question.queue_for_next_turn") as queue:
            async with TestClient(TestServer(_internal_app(state))) as client:
                pending = await client.get("/api/chat/slots/slot-a/questions")
                assert pending.status == 200
                assert (await pending.json())["questions"][0]["question_id"] == "card-a"
                answered = await client.post(
                    "/api/chat/slots/slot-a/questions/card-a/answer",
                    json={"answers": {"Which path?": "Safe"}},
                )
                assert answered.status == 200
                assert await answered.json() == {"ok": True, "completed": True}
        queue.assert_called_once_with(state, slot, "Which path?: Safe", directive_user_origin=True)
        assert "card-a" not in slot._question_pending

    @pytest.mark.asyncio
    async def test_origin_answer_refuses_owner_released_before_admission(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [{"question": "Which path?", "options": [{"label": "Safe"}]}],
        }
        state = _state_with(slot)
        async with TestClient(TestServer(_internal_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/slot-a/questions/card-a/answer",
                headers={TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"},
                json={"answers": {"Which path?": "Safe"}},
            )
            body = await response.json()

        assert response.status == 409
        assert body["code"] == "mcp_owner_stale"
        assert "card-a" in slot._question_pending
        assert slot.messages == []
        assert slot.task is None

    @pytest.mark.asyncio
    async def test_multi_select_accepts_custom_answer_text(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [
                {
                    "question": "Which features?",
                    "options": [{"label": "Search"}, {"label": "Export"}],
                    "multiSelect": True,
                }
            ],
        }
        state = _state_with(slot)
        state.answer_question_card.side_effect = (
            lambda key, card, answers: DashboardState.answer_question_card(
                state, key, card, answers
            )
        )
        state._broadcast_question_retired = MagicMock()
        state._push_slots = MagicMock()
        running = MagicMock()
        running.done.return_value = False
        slot.task = running

        with patch("kiro_crew.dashboard.handlers.ask_question.queue_for_next_turn") as queue:
            async with TestClient(TestServer(_internal_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/slot-a/questions/card-a/answer",
                    json={"answers": {"Which features?": "A custom integration"}},
                )
                body = await response.json()

        assert response.status == 200
        assert body == {"ok": True, "completed": True}
        queue.assert_called_once_with(
            state,
            slot,
            "Which features?: A custom integration",
            directive_user_origin=True,
        )
        assert "card-a" not in slot._question_pending

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("outcome", "status", "card_remains"),
        [("steered", 200, False), ("unavailable", 409, True)],
    )
    async def test_native_live_answer_is_steered_transactionally(
        self, outcome: str, status: int, card_remains: bool
    ) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-native"] = {
            "blocking": False,
            "native": True,
            "questions": [{"question": "Which path?", "options": [{"label": "Safe"}]}],
        }
        slot.session_mcp_owner = "client-a"
        running = MagicMock()
        running.done.return_value = False
        slot.task = running
        state = _state_with(slot)
        state.answer_question_card.side_effect = lambda key, card, answers, **kwargs: (
            DashboardState.answer_question_card(state, key, card, answers, **kwargs)
        )
        state.release_question_answer_claim.side_effect = (
            lambda key, card, claim: DashboardState.release_question_answer_claim(
                state, key, card, claim
            )
        )
        state.clear_question_pending.side_effect = (
            lambda key, **kwargs: slot._question_pending.pop(kwargs["card_id"], None) is not None
        )
        seen_origins: list[str] = []

        async def _steer(_state, _slot, _message, **_kwargs):
            seen_origins.append(_slot._turn_origin.get())
            return outcome

        with (
            patch(
                "kiro_crew.dashboard.handlers.ask_question.steer_into_running_turn",
                new_callable=AsyncMock,
                side_effect=_steer,
            ) as steer,
            patch("kiro_crew.dashboard.handlers.ask_question.containment_meta", return_value={}),
        ):
            async with TestClient(TestServer(_internal_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/slot-a/questions/card-native/answer",
                    headers={TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"},
                    json={"answers": {"Which path?": "Safe"}},
                )
                body = await response.json()
        assert response.status == status
        assert ("card-native" in slot._question_pending) is card_remains
        assert seen_origins == ["slot-a"]
        assert slot._turn_origin.get() == ""
        steer.assert_awaited_once()
        if status == 200:
            assert body == {"ok": True, "completed": True}
        else:
            assert body["code"] == "native_answer_not_delivered"
            assert "_answer_claim" not in slot._question_pending["card-native"]

    @pytest.mark.asyncio
    async def test_origin_native_requeue_keeps_admitted_mcp_snapshot(self) -> None:
        slot = _ChatSlot("slot-a")
        admitted = [_server("client-a")]
        replacement = [_server("client-b")]
        slot.session_mcp_servers = admitted
        slot.session_mcp_owner = "client-a"
        slot._session_mcp_configured = True
        slot._question_pending["card-native"] = {
            "blocking": False,
            "native": True,
            "questions": [{"question": "Which path?", "options": [{"label": "Safe"}]}],
        }
        running = MagicMock()
        running.done.return_value = False
        slot.task = running
        state = _state_with(slot)
        state.answer_question_card.side_effect = lambda key, card, answers, **kwargs: (
            DashboardState.answer_question_card(state, key, card, answers, **kwargs)
        )
        state.clear_question_pending.side_effect = (
            lambda key, **kwargs: slot._question_pending.pop(kwargs["card_id"], None) is not None
        )

        async def _requeue_during_rpc(_message: str) -> bool:
            from kiro_crew.dashboard.chat_runner import _requeue_unconsumed_steers

            _requeue_unconsumed_steers(state, slot)
            slot.session_mcp_servers = replacement
            slot.session_mcp_owner = "client-b"
            return True

        acp_client = MagicMock()
        acp_client.supports_steer = True
        acp_client.steer = AsyncMock(side_effect=_requeue_during_rpc)
        slot._acp_client = acp_client

        with patch("kiro_crew.dashboard.handlers.ask_question.containment_meta", return_value={}):
            async with TestClient(TestServer(_internal_app(state))) as http:
                response = await http.post(
                    "/api/chat/slots/slot-a/questions/card-native/answer",
                    headers={TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"},
                    json={"answers": {"Which path?": "Safe"}},
                )

        assert response.status == 200
        assert "card-native" not in slot._question_pending
        assert len(slot._queue) == 1
        assert slot._queue[0]["meta"][QUEUED_GATEWAY_MCP_META_KEY] == admitted

    @pytest.mark.asyncio
    async def test_concurrent_native_answers_admit_only_one_delivery(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-native"] = {
            "blocking": False,
            "native": True,
            "questions": [
                {
                    "question": "Which path?",
                    "options": [{"label": "Safe"}, {"label": "Fast"}],
                }
            ],
        }
        slot.session_mcp_owner = "client-a"
        running = MagicMock()
        running.done.return_value = False
        slot.task = running
        state = _state_with(slot)
        state.answer_question_card.side_effect = lambda key, card, answers, **kwargs: (
            DashboardState.answer_question_card(state, key, card, answers, **kwargs)
        )
        state.release_question_answer_claim.side_effect = (
            lambda key, card, claim: DashboardState.release_question_answer_claim(
                state, key, card, claim
            )
        )
        state.clear_question_pending.side_effect = (
            lambda key, **kwargs: slot._question_pending.pop(kwargs["card_id"], None) is not None
        )
        first_started = asyncio.Event()
        finish_first = asyncio.Event()
        steered: list[str] = []

        async def _steer(_state, _slot, message, **_kwargs):
            steered.append(message)
            first_started.set()
            await finish_first.wait()
            return "steered"

        headers = {TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"}
        with (
            patch(
                "kiro_crew.dashboard.handlers.ask_question.steer_into_running_turn",
                side_effect=_steer,
            ),
            patch("kiro_crew.dashboard.handlers.ask_question.containment_meta", return_value={}),
        ):
            async with TestClient(TestServer(_internal_app(state))) as client:
                first = asyncio.create_task(
                    client.post(
                        "/api/chat/slots/slot-a/questions/card-native/answer",
                        headers=headers,
                        json={"answers": {"Which path?": "Safe"}},
                    )
                )
                await first_started.wait()
                second = await client.post(
                    "/api/chat/slots/slot-a/questions/card-native/answer",
                    headers=headers,
                    json={"answers": {"Which path?": "Fast"}},
                )
                second_body = await second.json()
                finish_first.set()
                first_response = await first

        assert first_response.status == 200
        assert second.status == 409
        assert second_body["code"] == "native_answer_in_progress"
        assert steered == ["Which path?: Safe"]
        assert "card-native" not in slot._question_pending

    @pytest.mark.asyncio
    async def test_origin_scoped_answer_refuses_live_turn_without_retiring_card(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [{"question": "Which path?", "options": [{"label": "Safe"}]}],
        }
        slot.session_mcp_owner = "client-a"
        running = MagicMock()
        running.done.return_value = False
        slot.task = running
        state = _state_with(slot)
        state.answer_question_card.side_effect = (
            lambda key, card, answers: DashboardState.answer_question_card(
                state, key, card, answers
            )
        )
        with patch("kiro_crew.dashboard.handlers.ask_question.queue_for_next_turn") as queue:
            async with TestClient(TestServer(_internal_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/slot-a/questions/card-a/answer",
                    headers={TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"},
                    json={"answers": {"Which path?": "Safe"}},
                )
                body = await response.json()
        assert response.status == 409
        assert body["code"] == "slot_busy"
        assert "card-a" in slot._question_pending
        queue.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("origin_scoped", [False, True])
    async def test_answer_respects_armed_stage_boundary(self, origin_scoped: bool) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [{"question": "Which path?", "options": [{"label": "Safe"}]}],
        }
        if origin_scoped:
            slot.session_mcp_owner = "client-a"
        stage_boundary_for(slot).arm(1)
        state = _state_with(slot)
        state.answer_question_card.side_effect = (
            lambda key, card, answers: DashboardState.answer_question_card(
                state, key, card, answers
            )
        )
        state._broadcast_question_retired = MagicMock()
        state._push_slots = MagicMock()
        headers = (
            {TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"} if origin_scoped else {}
        )

        with patch("kiro_crew.dashboard.handlers.ask_question.queue_for_next_turn") as queue:
            async with TestClient(TestServer(_internal_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/slot-a/questions/card-a/answer",
                    headers=headers,
                    json={"answers": {"Which path?": "Safe"}},
                )
                body = await response.json()

        if origin_scoped:
            assert response.status == 409
            assert body["code"] == "slot_busy"
            assert "card-a" in slot._question_pending
            queue.assert_not_called()
        else:
            assert response.status == 200
            assert body == {"ok": True, "completed": True}
            assert "card-a" not in slot._question_pending
            queue.assert_called_once_with(
                state, slot, "Which path?: Safe", directive_user_origin=True
            )
        assert slot.task is None

    @pytest.mark.asyncio
    async def test_no_origin_answers_accumulate_and_queue_while_subagents_hold_slot(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [
                {"question": "Which path?", "options": [{"label": "Safe"}]},
                {"question": "Proceed?", "options": [{"label": "Yes"}]},
            ],
        }
        state = _state_with(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = ["child-a"]
        state.answer_question_card.side_effect = (
            lambda key, card, answers: DashboardState.answer_question_card(
                state, key, card, answers
            )
        )
        state._broadcast_question_retired = MagicMock()
        state._push_slots = MagicMock()

        with patch("kiro_crew.dashboard.handlers.ask_question.queue_for_next_turn") as queue:
            async with TestClient(TestServer(_internal_app(state))) as client:
                partial = await client.post(
                    "/api/chat/slots/slot-a/questions/card-a/answer",
                    json={"answers": {"Which path?": "Safe"}},
                )
                completed = await client.post(
                    "/api/chat/slots/slot-a/questions/card-a/answer",
                    json={"answers": {"Proceed?": "Yes"}},
                )
                partial_body = await partial.json()
                completed_body = await completed.json()

        assert partial.status == 200
        assert partial_body == {"ok": True, "completed": False}
        assert completed.status == 200
        assert completed_body == {"ok": True, "completed": True}
        queue.assert_called_once_with(
            state,
            slot,
            "Which path?: Safe\nProceed?: Yes",
            directive_user_origin=True,
        )
        assert "card-a" not in slot._question_pending

    @pytest.mark.asyncio
    async def test_origin_scoped_answer_refuses_subagent_hold_without_retiring_card(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [{"question": "Which path?", "options": [{"label": "Safe"}]}],
        }
        slot.session_mcp_owner = "client-a"
        state = _state_with(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = ["child-a"]
        state.answer_question_card.side_effect = (
            lambda key, card, answers: DashboardState.answer_question_card(
                state, key, card, answers
            )
        )

        with patch("kiro_crew.dashboard.handlers.ask_question.queue_for_next_turn") as queue:
            async with TestClient(TestServer(_internal_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/slot-a/questions/card-a/answer",
                    headers={TURN_ORIGIN_HEADER: "slot-a", MCP_OWNER_HEADER: "client-a"},
                    json={"answers": {"Which path?": "Safe"}},
                )
                body = await response.json()

        assert response.status == 409
        assert body["code"] == "slot_busy"
        assert "card-a" in slot._question_pending
        queue.assert_not_called()

    @pytest.mark.asyncio
    async def test_native_live_answer_steers_while_subagents_hold_slot(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-native"] = {
            "blocking": False,
            "native": True,
            "questions": [{"question": "Which path?", "options": [{"label": "Safe"}]}],
        }
        running = MagicMock()
        running.done.return_value = False
        slot.task = running
        state = _state_with(slot)
        state.subagents = MagicMock()
        state.subagents.running_agents_for.return_value = ["child-a"]
        state.answer_question_card.side_effect = lambda key, card, answers, **kwargs: (
            DashboardState.answer_question_card(state, key, card, answers, **kwargs)
        )
        state.release_question_answer_claim.side_effect = (
            lambda key, card, claim: DashboardState.release_question_answer_claim(
                state, key, card, claim
            )
        )
        state.clear_question_pending.side_effect = (
            lambda key, **kwargs: slot._question_pending.pop(kwargs["card_id"], None) is not None
        )

        with (
            patch(
                "kiro_crew.dashboard.handlers.ask_question.steer_into_running_turn",
                new_callable=AsyncMock,
                return_value="steered",
            ) as steer,
            patch("kiro_crew.dashboard.handlers.ask_question.containment_meta", return_value={}),
        ):
            async with TestClient(TestServer(_internal_app(state))) as client:
                response = await client.post(
                    "/api/chat/slots/slot-a/questions/card-native/answer",
                    json={"answers": {"Which path?": "Safe"}},
                )
                body = await response.json()

        assert response.status == 200
        assert body == {"ok": True, "completed": True}
        steer.assert_awaited_once()
        assert "card-native" not in slot._question_pending

    @pytest.mark.asyncio
    async def test_unknown_question_is_rejected_without_retiring_card(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [{"question": "Known?", "options": [{"label": "Yes"}]}],
        }
        state = _state_with(slot)
        state.answer_question_card.side_effect = (
            lambda key, card, answers: DashboardState.answer_question_card(
                state, key, card, answers
            )
        )
        async with TestClient(TestServer(_internal_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/slot-a/questions/card-a/answer",
                json={"answers": {"Other?": "Yes"}},
            )
            assert response.status == 400
            assert (await response.json())["code"] == "invalid_answers"
        assert "card-a" in slot._question_pending


class TestSessionEvents:
    @staticmethod
    def _hub() -> WebSocketHub:
        owner = MagicMock()
        owner._background_tasks = set()
        return WebSocketHub(
            owner,
            serving_loop_provider=lambda: None,
            logger_provider=lambda: MagicMock(),
            redact_credentials_provider=lambda: (lambda text: (text, False)),
            redact_exfiltration_urls_provider=lambda: (lambda text: (text, False)),
        )

    def test_dedicated_socket_receives_only_subscribed_session_events(self) -> None:
        hub = self._hub()
        ws = {
            "_session_events_subscription": True,
            "_session_event_keys": {"slot-a"},
        }
        assert hub._ws_client_allowed(  # type: ignore[arg-type]
            ws, SESSION_MESSAGE_EVENT, {"slot": "slot-a"}
        )
        assert hub._ws_client_allowed(  # type: ignore[arg-type]
            ws, SESSION_PLAN_EVENT, {"slot": "slot-a"}
        )
        assert not hub._ws_client_allowed(  # type: ignore[arg-type]
            ws, SESSION_MESSAGE_EVENT, {"slot": "slot-b"}
        )
        assert not hub._ws_client_allowed(  # type: ignore[arg-type]
            ws, "chat_chunk", {"slot": "slot-a"}
        )

    def test_session_event_decisions_are_audited(self) -> None:
        hub = self._hub()
        ws = {
            "_session_events_subscription": True,
            "_session_event_keys": {"slot-a"},
        }
        with patch("kiro_crew.dashboard.websocket_hub._audit_allow") as allowed:
            assert hub._ws_client_allowed(  # type: ignore[arg-type]
                ws, SESSION_MESSAGE_EVENT, {"slot": "slot-a"}
            )
        allowed.assert_called_once_with("<session-events>", SESSION_MESSAGE_EVENT)
        with patch("kiro_crew.dashboard.websocket_hub._audit_deny") as denied:
            assert not hub._ws_client_allowed(  # type: ignore[arg-type]
                ws, SESSION_MESSAGE_EVENT, {"slot": "slot-b"}
            )
        denied.assert_called_once_with(
            "<session-events>", SESSION_MESSAGE_EVENT, "slot_scope_denied"
        )

    def test_non_subscriber_session_event_denial_has_a_distinct_audit_reason(self) -> None:
        hub = self._hub()
        ws = {"_is_dashboard_user": True}

        with patch("kiro_crew.dashboard.websocket_hub._audit_deny") as denied:
            assert not hub._ws_client_allowed(  # type: ignore[arg-type]
                ws, SESSION_MESSAGE_EVENT, {"slot": "slot-a"}
            )

        denied.assert_called_once_with(
            "<dashboard-user>",
            SESSION_MESSAGE_EVENT,
            "session_subscription_required",
        )

    def test_cleared_plan_is_published_as_an_empty_snapshot(self) -> None:
        state = MagicMock(spec=DashboardState)

        DashboardState._broadcast_session_plan(state, "slot-a", None)

        state._send_ws_all.assert_called_once_with(
            SESSION_PLAN_EVENT,
            {"slot": "slot-a", "description": "", "tasks": []},
            json.dumps(
                {
                    "type": SESSION_PLAN_EVENT,
                    "data": {"slot": "slot-a", "description": "", "tasks": []},
                }
            ),
        )

    def test_dedicated_socket_redacts_slot_titles(self) -> None:
        hub = self._hub()
        hub._redact_exfiltration_urls_provider = lambda: (
            lambda text: (text.replace("LEAK_URL", "<url>"), True)
        )
        hub._redact_credentials_provider = lambda: (
            lambda text: (text.replace("LEAK_CRED", "<cred>"), True)
        )
        ws = {
            "_session_events_subscription": True,
            "_is_dashboard_user": True,
        }

        encoded = hub._serialize_for_client(  # type: ignore[arg-type]
            ws,
            "slot_title",
            {"key": "slot-a", "title": "LEAK_URL LEAK_CRED"},
            "UNREDACTED",
        )

        assert json.loads(encoded) == {
            "type": "slot_title",
            "data": {"key": "slot-a", "title": "<url> <cred>"},
        }

    def test_session_subscription_keys_are_bounded(self) -> None:
        assert _session_subscription_keys(["slot-a", "slot-a"]) == {"slot-a"}
        assert _session_subscription_keys(["x"] * (MAX_SESSION_EVENT_KEYS + 1)) is None
        assert _session_subscription_keys(["x" * (MAX_SESSION_EVENT_KEY_CHARS + 1)]) is None

    @pytest.mark.parametrize("role", ["user", "assistant"])
    def test_active_sse_reader_does_not_suppress_session_event(self, role: str) -> None:
        slot = _ChatSlot("slot-a")
        ordinary = MagicMock()
        dedicated = MagicMock()
        slot._on_message = ordinary
        slot._on_session_message = dedicated
        slot._has_reader = True

        row = slot.append(role, "finalized")

        ordinary.assert_not_called()
        dedicated.assert_called_once_with("slot-a", row)

    def test_turn_origin_is_scoped_to_rows_created_in_that_context(self) -> None:
        slot = _ChatSlot("slot-a")
        callback = MagicMock()
        slot._on_session_message = callback
        previous_origin = slot._turn_origin.get()
        slot._turn_origin.set("slot-a")
        try:
            row = slot.append("user", "hello", "msg msg-u")
        finally:
            slot._turn_origin.set(previous_origin)
        next_row = slot.append("user", "dashboard", "msg msg-u")
        assert row["meta"]["_gateway_turn_origin"] == "slot-a"
        assert "_gateway_turn_origin" not in next_row["meta"]
        assert callback.call_count == 2

    def test_gateway_turn_origin_is_reserved_from_client_metadata(self) -> None:
        assert "_gateway_turn_origin" in RESERVED_ROW_META_KEYS

    @pytest.mark.asyncio
    async def test_queued_successor_uses_its_own_gateway_context(
        self, tmp_path, monkeypatch
    ) -> None:
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_runner
        from kiro_crew.dashboard.session_control import containment_meta

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-a")
        request_snapshot = [_server("request-a")]
        current_servers = [_server("current-b")]
        slot._session_mcp_configured = True
        slot.session_mcp_servers = current_servers
        slot.queue_append(
            "dashboard follow-up",
            meta=containment_meta(state, slot),
            directive_user_origin=True,
        )
        observed: list[tuple[str, list[dict] | None, list[dict] | None]] = []

        async def capture_context(_state, owned_slot, _message, **_kwargs):
            observed.append(
                (
                    owned_slot._turn_origin.get(),
                    owned_slot._request_mcp_servers.get(),
                    _turn_session_mcp_servers(owned_slot),
                )
            )

        monkeypatch.setattr(chat_runner, "_run_chat", capture_context)
        previous_origin = slot._turn_origin.get()
        previous_mcp = slot._request_mcp_servers.get()
        slot._turn_origin.set(slot.key)
        slot._request_mcp_servers.set(request_snapshot)
        try:
            assert await _start_next_queued_turn(state, slot) is True
            await slot.task
            assert slot._turn_origin.get() == slot.key
            assert slot._request_mcp_servers.get() == request_snapshot
        finally:
            slot._request_mcp_servers.set(previous_mcp)
            slot._turn_origin.set(previous_origin)

        row = next(
            message for message in slot.messages if message.get("content") == "dashboard follow-up"
        )
        assert "_gateway_turn_origin" not in row.get("meta", {})
        assert observed == [("", None, current_servers)]

    @pytest.mark.asyncio
    async def test_synthesis_successor_uses_its_own_gateway_context(
        self, tmp_path, monkeypatch
    ) -> None:
        from chat_test_helpers import _make_state

        from kiro_crew.dashboard import chat_runner

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slot-a")
        state._slots[slot.key] = slot
        state.subagents = MagicMock(running_agents_for=MagicMock(return_value=[]))
        slot._pending_synthesis = True
        request_snapshot = [_server("request-a")]
        current_servers = [_server("current-b")]
        slot._session_mcp_configured = True
        slot.session_mcp_servers = current_servers
        observed: list[tuple[str, list[dict] | None, list[dict] | None]] = []

        async def capture_context(_state, owned_slot, _message, **_kwargs):
            observed.append(
                (
                    owned_slot._turn_origin.get(),
                    owned_slot._request_mcp_servers.get(),
                    _turn_session_mcp_servers(owned_slot),
                )
            )

        monkeypatch.setattr(chat_runner, "_run_chat", capture_context)
        previous_origin = slot._turn_origin.get()
        previous_mcp = slot._request_mcp_servers.get()
        slot._turn_origin.set(slot.key)
        slot._request_mcp_servers.set(request_snapshot)
        try:
            await _finish_queue_cycle(state, slot)
            assert slot.task is not None
            await slot.task
            assert slot._turn_origin.get() == slot.key
            assert slot._request_mcp_servers.get() == request_snapshot
        finally:
            slot._request_mcp_servers.set(previous_mcp)
            slot._turn_origin.set(previous_origin)

        row = next(
            message
            for message in slot.messages
            if (message.get("meta") or {}).get("injectKind") == "synthesis"
        )
        assert "_gateway_turn_origin" not in row.get("meta", {})
        assert observed == [("", None, current_servers)]

    def test_gateway_sse_row_exposes_only_turn_origin_metadata(self) -> None:
        from kiro_crew.dashboard.chat_utils import _build_stream_chunk

        row = {
            "role": "assistant",
            "content": "done",
            "meta": {"_gateway_turn_origin": "slot-a", "private": "withheld"},
        }
        ordinary = json.loads(_build_stream_chunk(row))
        gateway = json.loads(_build_stream_chunk(row, include_turn_origin=True))
        assert "meta" not in ordinary
        assert gateway["meta"] == {"_gateway_turn_origin": "slot-a"}

    def test_message_event_redacts_user_and_assistant_content(self) -> None:
        state = DashboardState.__new__(DashboardState)
        state._send_ws_all = MagicMock()  # type: ignore[method-assign]
        with (
            patch(
                "kiro_crew.dashboard.state.redact_exfiltration_urls",
                side_effect=lambda text: (text.replace("LEAK_URL", "<url>"), 1),
            ),
            patch(
                "kiro_crew.dashboard.state.redact_credentials",
                side_effect=lambda text: (text.replace("LEAK_CRED", "<cred>"), 1),
            ),
        ):
            for role in ("user", "assistant"):
                DashboardState._broadcast_session_message(
                    state,
                    "slot-a",
                    {
                        "role": role,
                        "content": "LEAK_URL LEAK_CRED",
                        "meta": {"mid": f"m-{role}"},
                    },
                )
        assert [call.args[1]["content"] for call in state._send_ws_all.call_args_list] == [
            "<url> <cred>",
            "<url> <cred>",
        ]

    def test_message_event_is_minimal_and_origin_tagged(self) -> None:
        state = DashboardState.__new__(DashboardState)
        state._send_ws_all = MagicMock()  # type: ignore[method-assign]
        DashboardState._broadcast_session_message(
            state,
            "slot-a",
            {
                "role": "assistant",
                "content": "done",
                "meta": {"mid": "m1", "_gateway_turn_origin": "slot-a", "private": "x"},
            },
        )
        event, data, encoded = state._send_ws_all.call_args.args
        assert event == SESSION_MESSAGE_EVENT
        assert data == {
            "slot": "slot-a",
            "role": "assistant",
            "content": "done",
            "messageId": "m1",
            "origin": "slot-a",
        }
        assert json.loads(encoded) == {"type": SESSION_MESSAGE_EVENT, "data": data}


@pytest.fixture
def session_config() -> KiroCrewConfig:
    config = KiroCrewConfig()
    config.session.timeout_secs = 2
    return config


def _recording_factory(calls: list):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        calls.append(kwargs.get("session_mcp_servers"))
        provider = AsyncMock()
        provider.start = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.is_process_alive = lambda: True
        provider.is_alive = lambda: True
        provider.context_usage_pct = lambda: 0.0
        return provider

    return factory


class TestSessionMcpIdentity:
    def test_origin_scoped_absence_is_a_definitive_snapshot(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._session_mcp_configured = True
        slot.session_mcp_servers = [_server("later")]
        previous_origin = slot._turn_origin.get()
        previous_mcp = slot._request_mcp_servers.get()
        slot._turn_origin.set("slot-a")
        slot._request_mcp_servers.set(None)
        try:
            assert _turn_session_mcp_servers(slot) is None
        finally:
            slot._request_mcp_servers.set(previous_mcp)
            slot._turn_origin.set(previous_origin)
        assert _turn_session_mcp_servers(slot) == [_server("later")]

    @pytest.mark.asyncio
    async def test_previous_receipt_conditionally_restores_replaced_state(self) -> None:
        slot = _ChatSlot("slot-a")
        state = _state_with(slot)
        with patch("kiro_crew.dashboard.chat_handlers.sel", return_value=MagicMock()):
            async with TestClient(TestServer(_internal_app(state))) as client:
                replaced = await client.post(
                    "/api/chat/slots/slot-a/mcp",
                    json={
                        "servers": [{"name": "new", "command": "/new"}],
                        "owner": "client-a",
                        "return_previous": True,
                    },
                )
                previous = (await replaced.json())["previous"]
                restored = await client.post(
                    "/api/chat/slots/slot-a/mcp",
                    json={"mode": "restore_if_owner", **previous},
                )
                restored_body = await restored.json()

        assert restored_body["applied"] is True
        assert slot.session_mcp_owner == ""
        assert slot._session_mcp_configured is False
        assert slot.session_mcp_servers == []

    @pytest.mark.asyncio
    async def test_restore_rejects_a_non_boolean_configured_value(self) -> None:
        slot = _ChatSlot("slot-a")
        slot.session_mcp_owner = "current-owner"
        state = _state_with(slot)
        async with TestClient(TestServer(_internal_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/slot-a/mcp",
                json={
                    "mode": "restore_if_owner",
                    "owner": "previous-owner",
                    "expected_owner": "current-owner",
                    "servers": [],
                    "configured": "false",
                },
            )
            body = await response.json()

        assert response.status == 400
        assert body["code"] == "invalid_request"
        assert slot.session_mcp_owner == "current-owner"
        assert slot._session_mcp_configured is False

    @pytest.mark.asyncio
    async def test_malformed_card_answer_is_not_reported_as_missing(self) -> None:
        slot = _ChatSlot("slot-a")
        slot._question_pending["card-a"] = {
            "blocking": False,
            "questions": [
                {
                    "question": "Which features?",
                    "options": [{"label": "Search"}, {"label": "Export"}],
                    "multiSelect": True,
                }
            ],
        }
        state = _state_with(slot)
        state.answer_question_card.side_effect = (
            lambda key, card, answers: DashboardState.answer_question_card(
                state, key, card, answers
            )
        )
        async with TestClient(TestServer(_internal_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/slot-a/questions/card-a/answer",
                json={"answers": {"Which features?": ["Not offered"]}},
            )
            body = await response.json()

        assert response.status == 400
        assert body["code"] == "invalid_answers"
        assert "card-a" in slot._question_pending

    def test_fingerprint_preserves_array_order_and_is_opaque(self) -> None:
        first = [_server("my server", "one"), _server("my_server", "two")]
        second = list(reversed(first))
        fingerprint = _mcp_fingerprint(first)
        assert fingerprint != _mcp_fingerprint(second)
        assert fingerprint == _mcp_fingerprint(list(first))
        assert _mcp_fingerprint(None) == ""
        assert _mcp_fingerprint([]) != _mcp_fingerprint(None)
        assert len(fingerprint) == 64
        assert "one" not in fingerprint

    @pytest.mark.asyncio
    async def test_reordered_array_recreates_the_provider(
        self, session_config: KiroCrewConfig
    ) -> None:
        calls: list = []
        manager = SessionManager(session_config, provider_factory=_recording_factory(calls))
        first_servers = [_server("my server", "one"), _server("my_server", "two")]
        try:
            first, _, _ = await manager.get_or_create("slot-a", session_mcp_servers=first_servers)
            manager.release("slot-a")
            reordered, reordered_new, _ = await manager.get_or_create(
                "slot-a", session_mcp_servers=list(reversed(first_servers))
            )
            manager.release("slot-a")
            assert reordered is not first
            assert reordered_new is True
            assert len(calls) == 2
            first.shutdown.assert_awaited_once()
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_explicit_empty_set_bypasses_ambient_factory_defaults(
        self, session_config: KiroCrewConfig
    ) -> None:
        calls: list = []
        manager = SessionManager(session_config, provider_factory=_recording_factory(calls))
        try:
            provider, _, _ = await manager.get_or_create("slot-a", session_mcp_servers=[])
            manager.release("slot-a")
            assert calls == [[]]
            assert provider is not None
        finally:
            await manager.close_all()

    @pytest.mark.asyncio
    async def test_same_set_reuses_and_changed_set_recreates(
        self, session_config: KiroCrewConfig
    ) -> None:
        calls: list = []
        manager = SessionManager(session_config, provider_factory=_recording_factory(calls))
        try:
            first, first_new, _ = await manager.get_or_create(
                "slot-a", session_mcp_servers=[_server("echo", "one")]
            )
            manager.release("slot-a")
            same, same_new, _ = await manager.get_or_create(
                "slot-a", session_mcp_servers=[_server("echo", "one")]
            )
            manager.release("slot-a")
            changed, changed_new, _ = await manager.get_or_create(
                "slot-a", session_mcp_servers=[_server("echo", "two")]
            )
            manager.release("slot-a")
            assert first is same
            assert first_new is True and same_new is False
            assert changed is not first and changed_new is True
            assert len(calls) == 2
            first.shutdown.assert_awaited_once()
        finally:
            await manager.close_all()


class _LoadHandle:
    session_id = "session-1"


class _LoadRuntime:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    async def load_session(self, session_file, resume_sid, **kwargs):
        self.kwargs = kwargs
        return _LoadHandle()

    def is_alive(self) -> bool:
        return True


class TestProviderSessionMcpForwarding:
    @pytest.mark.asyncio
    async def test_resume_redeclares_explicit_mcp_servers(self, tmp_path) -> None:
        from kiro_crew.providers.acp import AcpProvider

        servers = [_server("echo", "one")]
        provider = AcpProvider(
            work_dir=tmp_path,
            model="",
            session_mcp_servers=servers,
        )
        assert provider.client._requested_session_mcp_servers == servers
        runtime = _LoadRuntime()
        await provider._load_session_with_retry(
            runtime,  # type: ignore[arg-type]
            str(tmp_path / "session.json"),
            "resume-1",
            tmp_path,
            "",
        )
        assert runtime.kwargs["mcp_servers"] == servers

    @pytest.mark.asyncio
    async def test_resume_redeclares_explicit_empty_mcp_array(self, tmp_path) -> None:
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(
            work_dir=tmp_path,
            model="",
            session_mcp_servers=[],
        )
        assert provider.client._requested_session_mcp_servers == []
        runtime = _LoadRuntime()
        await provider._load_session_with_retry(
            runtime,  # type: ignore[arg-type]
            str(tmp_path / "session.json"),
            "resume-1",
            tmp_path,
            "",
        )
        assert runtime.kwargs["mcp_servers"] == []
