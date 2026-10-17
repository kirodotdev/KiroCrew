"""Ownership fences and audit attribution on PATCH /api/chat/slots/{slot}/pin.

``chat_session_pin`` reaches this route on behalf of app agents and crew
members, so the route applies the same three fences ``api_chat_slot_folder``
does: the unattributable-caller refusal, the member ``member_owns_slot`` fence,
and App Kit ownership of both the slot object and the transcript it routes to.
A refusal is the same 404 the folder route returns, so the route is not an
existence oracle. The audit line names the internal caller, so a pin the agent
made reads differently from one the person clicked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state

from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_folders import api_chat_slot_pin
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.dashboard.token_auth import MEMBER_CHAT_PRINCIPAL_KEY


def _make_app(
    state: DashboardState, *, declared_app: str = "", member_principal: str = ""
) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_claims(request: web.Request, handler):
        # Stands in for the token middleware (the validated app claim) and the
        # chat-route gate (the verified member principal).
        request["app"] = declared_app
        if member_principal:
            request[MEMBER_CHAT_PRINCIPAL_KEY] = member_principal
        return await handler(request)

    app.middlewares.append(_publish_claims)
    app.router.add_patch("/api/chat/slots/{slot}/pin", api_chat_slot_pin)
    return app


def _state(*slots: _ChatSlot) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {s.key: s for s in slots}
    state.push_slots_update = MagicMock()
    return state


def _app_slot(key: str, app: str) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._app = app
    return slot


async def _pin(app: web.Application, slot: str, caller: str, pinned: bool = True) -> Any:
    with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock(return_value=True)):
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                f"/api/chat/slots/{slot}/pin",
                json={"pinned": pinned},
                headers={"X-Session-Key": caller},
            )
            return resp.status, await resp.json()


class TestPinIsAppScoped:
    @pytest.mark.asyncio
    async def test_an_app_cannot_pin_another_apps_session(self) -> None:
        caller = _app_slot("chat-1-100", "issue-radar")
        target = _app_slot("chat-2-200", "spec-builder")
        status, body = await _pin(
            _make_app(_state(caller, target)), "chat-2-200", "dashboard:chat-1-100"
        )
        assert status == 404 and body["code"] == "slot_not_found"
        assert target.pinned is False

    @pytest.mark.asyncio
    async def test_an_app_cannot_pin_the_users_own_session(self) -> None:
        caller = _app_slot("chat-1-100", "issue-radar")
        target = _ChatSlot("chat-2-200")  # no _app: the person's session
        status, _ = await _pin(
            _make_app(_state(caller, target)), "chat-2-200", "dashboard:chat-1-100"
        )
        assert status == 404
        assert target.pinned is False

    @pytest.mark.asyncio
    async def test_an_app_can_pin_its_own_session(self) -> None:
        caller = _app_slot("chat-1-100", "issue-radar")
        target = _app_slot("chat-2-200", "issue-radar")
        status, body = await _pin(
            _make_app(_state(caller, target)), "chat-2-200", "dashboard:chat-1-100"
        )
        assert status == 200 and body["pinned"] is True
        assert target.pinned is True

    @pytest.mark.asyncio
    async def test_the_person_can_pin_any_session(self) -> None:
        caller = _ChatSlot("chat-1-100")
        target = _app_slot("chat-2-200", "issue-radar")
        status, _ = await _pin(
            _make_app(_state(caller, target)), "chat-2-200", "dashboard:chat-1-100"
        )
        assert status == 200
        assert target.pinned is True

    @pytest.mark.asyncio
    async def test_a_caller_whose_tab_closed_is_not_read_as_the_person(self) -> None:
        """A ``dashboard:`` key naming no live slot must not inherit the person's reach."""
        target = _ChatSlot("chat-2-200")
        status, _ = await _pin(_make_app(_state(target)), "chat-2-200", "dashboard:chat-9-999")
        assert status != 200
        assert target.pinned is False


class TestPinIsMemberScoped:
    MEMBER = "member:member-kirocrew-conductor-deadbeef"

    @pytest.mark.asyncio
    async def test_a_member_cannot_pin_a_session_it_does_not_own(self, monkeypatch) -> None:
        monkeypatch.setattr(sc, "member_owns_slot", lambda state, slot, key: False)
        caller = _ChatSlot("member-conductor")
        target = _ChatSlot("chat-2-200")
        status, body = await _pin(
            _make_app(_state(caller, target), member_principal=self.MEMBER),
            "chat-2-200",
            "dashboard:member-conductor",
        )
        assert status == 404 and body["code"] == "slot_not_found"
        assert target.pinned is False

    @pytest.mark.asyncio
    async def test_a_member_can_pin_a_session_it_owns(self, monkeypatch) -> None:
        monkeypatch.setattr(sc, "member_owns_slot", lambda state, slot, key: True)
        caller = _ChatSlot("member-conductor")
        target = _ChatSlot("chat-2-200")
        status, _ = await _pin(
            _make_app(_state(caller, target), member_principal=self.MEMBER),
            "chat-2-200",
            "dashboard:member-conductor",
        )
        assert status == 200
        assert target.pinned is True


class _RecordingSel:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **kw: Any) -> None:
        self.events.append(kw)

    def __getattr__(self, _name: str) -> Any:  # pragma: no cover - unused legs
        return lambda *a, **k: None


class TestPinAuditOrigin:
    @pytest.mark.asyncio
    async def test_mcp_pin_is_audited_as_the_declared_caller(self, tmp_path, monkeypatch) -> None:
        rec = _RecordingSel()
        monkeypatch.setattr("kiro_crew.dashboard.chat_folders.sel", lambda: rec)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch(
                "/api/chat/slots/myslot/pin",
                json={"pinned": True},
                headers={
                    "X-Internal-Secret": "s3cret",
                    "X-Internal-Caller": "kirocrew-dashboard",
                },
            )
            assert resp.status == 200
        event = next(e for e in rec.events if e["operation"] == "chat.slot_pin")
        assert event["outcome"] == "allowed"
        assert event["source"] == "mcp"
        assert event["caller"] == "kirocrew-dashboard"

    @pytest.mark.asyncio
    async def test_browser_pin_is_audited_as_dashboard(self, tmp_path, monkeypatch) -> None:
        rec = _RecordingSel()
        monkeypatch.setattr("kiro_crew.dashboard.chat_folders.sel", lambda: rec)
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("myslot")
        async with TestClient(TestServer(_make_folder_app(state))) as client:
            resp = await client.patch("/api/chat/slots/myslot/pin", json={"pinned": True})
            assert resp.status == 200
        event = next(e for e in rec.events if e["operation"] == "chat.slot_pin")
        assert event["source"] == "dashboard" and event["caller"] == "dashboard"


async def _pin_with_generation(
    app: web.Application, slot: str, caller: str, expected_created: str
) -> Any:
    with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", AsyncMock(return_value=True)):
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                f"/api/chat/slots/{slot}/pin",
                json={"pinned": True, "expected_created": expected_created},
                headers={"X-Session-Key": caller},
            )
            return resp.status, await resp.json()


class TestPinIsBoundToTheResolvedGeneration:
    """A slot key recreated after the MCP tool resolved it must not be pinned."""

    @pytest.mark.asyncio
    async def test_a_stale_generation_is_refused_and_writes_nothing(self) -> None:
        caller = _ChatSlot("chat-1-100")
        target = _ChatSlot("chat-2-200")
        status, body = await _pin_with_generation(
            _make_app(_state(caller, target)),
            "chat-2-200",
            "dashboard:chat-1-100",
            target.created_at + "-older",
        )
        assert status == 409 and body["code"] == "session_gone"
        assert target.pinned is False

    @pytest.mark.asyncio
    async def test_the_resolved_generation_is_accepted(self) -> None:
        caller = _ChatSlot("chat-1-100")
        target = _ChatSlot("chat-2-200")
        status, body = await _pin_with_generation(
            _make_app(_state(caller, target)),
            "chat-2-200",
            "dashboard:chat-1-100",
            target.created_at,
        )
        assert status == 200 and body["pinned"] is True
        assert target.pinned is True


class TestPinReportsWhetherItChanged:
    """The no-op answer comes from the lock-held state, not a caller's list read."""

    @pytest.mark.asyncio
    async def test_pinning_an_unpinned_session_reports_changed(self) -> None:
        caller = _ChatSlot("chat-1-100")
        target = _ChatSlot("chat-2-200")
        status, body = await _pin(
            _make_app(_state(caller, target)), "chat-2-200", "dashboard:chat-1-100"
        )
        assert status == 200 and body["changed"] is True

    @pytest.mark.asyncio
    async def test_pinning_a_pinned_session_reports_unchanged(self) -> None:
        caller = _ChatSlot("chat-1-100")
        target = _ChatSlot("chat-2-200")
        target.pinned = True
        save = AsyncMock(return_value=True)
        with patch("kiro_crew.dashboard.chat_folders.save_slot_off_loop", save):
            async with TestClient(TestServer(_make_app(_state(caller, target)))) as client:
                resp = await client.patch(
                    "/api/chat/slots/chat-2-200/pin",
                    json={"pinned": True},
                    headers={"X-Session-Key": "dashboard:chat-1-100"},
                )
                status, body = resp.status, await resp.json()
        assert status == 200 and body["changed"] is False and body["pinned"] is True
        save.assert_not_called()


class TestPinChecksTranscriptOwnership:
    """``_app`` alone is not enough: the transcript the write lands on must be the app's too."""

    @pytest.mark.asyncio
    async def test_an_app_slot_linked_to_a_foreign_transcript_is_refused(self) -> None:
        caller = _app_slot("chat-1-100", "issue-radar")
        target = _app_slot("chat-2-200", "issue-radar")
        foreign = _app_slot("chat-3-300", "spec-builder")
        target.linked_session_key = "dashboard:chat-3-300"
        status, body = await _pin(
            _make_app(_state(caller, target, foreign)), "chat-2-200", "dashboard:chat-1-100"
        )
        assert status == 404 and body["code"] == "slot_not_found"
        assert target.pinned is False
