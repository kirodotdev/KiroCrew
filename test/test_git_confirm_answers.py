"""A git-confirm card is settled only by a person's Allow once or Reject.

``hooks.confirm_git_writes`` puts a git command other than a known read (and a
git-free command holding a shell expansion) on a card marked ``git_confirm``.
No standing tier answers that card: not Trust, Trust reads, a command or family
grant, YOLO, nor a Slack Code channel's "Trust this channel"; and no app's
session-approval grant answers it either. Each refused answer leaves the card
pending and records no grant, and the surfaces that render the card offer no
such button on it.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_mode_security_helpers import (  # noqa: F401 -- fixtures are requested by name
    _app_client,
    _app_request,
    _hermetic_home,
    _isolate_safety_override,
    state_fixture,
)
from slack_code_channel_surface_helpers import env_fixture  # noqa: F401
from slack_code_channels_helpers import OWNER
from test_slack_code_channel_trust_from_slack import REQUEST as CHANNEL_REQUEST
from test_slack_code_channel_trust_from_slack import (  # noqa: F401
    _buttons,
    _click,
    _post_card,
    chan,
)
from test_slack_code_channel_worker_approvals import REQUEST as WORKER_REQUEST
from test_slack_code_channel_worker_approvals import _click as _worker_click
from test_slack_code_channel_worker_approvals import _run_relay, relay  # noqa: F401

from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
from kiro_crew.dashboard.git_confirm_card import git_confirm_refuses, is_git_confirm_card
from kiro_crew.dashboard.handlers.sessions import api_approval_resolve
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.slack.code_channels import approval_picker
from kiro_crew.slack.code_channels import worker_approvals as wa

RID = "req-g1"
STANDING = ["trust", "trust_reads", "trust_command", "trust_base"]


def _card_row(request_id: str, *, git_confirm: bool = True) -> dict[str, Any]:
    meta = {"request_id": request_id, "full_command": "git commit -m x"}
    if git_confirm:
        meta["git_confirm"] = "1"
    return {"role": "permission", "content": "Tool", "cls": json.dumps(meta)}


def _pending_card(state, name: str = "s1", *, git_confirm: bool = True):
    slot = state.get_or_create_slot(name, origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = _card_row(RID, git_confirm=git_confirm)
    msg = slot.append("permission", row["content"], row["cls"])
    slot.register_approval(RID, fut, msg)
    return slot, fut


@web.middleware
async def _owner_request(request: web.Request, handler):
    request["app"] = ""
    request["user"] = "local-app"
    return await handler(request)


def _owner_client(state) -> TestClient:
    app = web.Application(middlewares=[_owner_request])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return TestClient(TestServer(app))


def _no_standing_grant(slot) -> None:
    assert slot._trust is False
    assert slot._trust_reads is False
    assert not slot._trusted_patterns


class TestTheCardReader:
    def test_reads_the_newest_row_for_the_id(self):
        rows = [_card_row(RID, git_confirm=False), _card_row(RID)]
        assert is_git_confirm_card(rows, RID) is True
        assert is_git_confirm_card(list(reversed(rows)), RID) is False
        assert is_git_confirm_card(rows, "other") is False

    @pytest.mark.parametrize(
        "action, from_app, refused",
        [
            ("approved", False, False),
            ("rejected", False, False),
            ("rejected_once", False, False),
            ("deny_override_once", False, False),
            ("approved", True, True),
            ("rejected", True, True),
            ("yolo", False, True),
            ("something_new", False, True),
        ]
        + [(a, False, True) for a in STANDING],
    )
    def test_only_a_persons_one_shot_answer_is_taken(self, action, from_app, refused):
        assert git_confirm_refuses([_card_row(RID)], RID, action, from_app=from_app) is refused
        # An ordinary card refuses nothing here.
        assert not git_confirm_refuses(
            [_card_row(RID, git_confirm=False)], RID, action, from_app=from_app
        )


class TestTheApproveRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", STANDING)
    async def test_a_standing_tier_leaves_the_card_pending(self, state, action):
        slot, fut = _pending_card(state)
        body = {
            "action": action,
            "request_id": RID,
            "origin": "native",
            "request_mid": slot.approval_instance(RID),
            "pattern": "git commit -m x",
        }
        async with _owner_client(state) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json=body)
            payload = await resp.json()
        assert resp.status == 400
        assert payload["code"] == "git_confirm_allow_once_only"
        assert not fut.done()
        _no_standing_grant(slot)

    @pytest.mark.asyncio
    async def test_legacy_yolo_leaves_the_card_pending_and_arms_nothing(self, state):
        slot, fut = _pending_card(state)
        override = MagicMock()
        with patch("kiro_crew.dashboard.chat_handlers.safety_override", return_value=override):
            async with _owner_client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/approve", json={"action": "yolo", "request_id": RID}
                )
                payload = await resp.json()
        assert resp.status == 400
        assert payload["code"] == "git_confirm_allow_once_only"
        assert not fut.done()
        override.activate.assert_not_called()
        assert state.sessions.get_approval_policy(f"dashboard:{slot.key}") != "auto"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["approved", "rejected"])
    async def test_allow_once_and_reject_settle_it(self, state, action):
        slot, fut = _pending_card(state)
        body = {
            "action": action,
            "request_id": RID,
            "origin": "native",
            "request_mid": slot.approval_instance(RID),
        }
        async with _owner_client(state) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json=body)
        assert resp.status == 200
        assert fut.result() == action
        _no_standing_grant(slot)


class TestAnAppsAnswer:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["approved", "rejected"])
    async def test_the_slot_route_refuses_an_app(self, state, action):
        slot, fut = _pending_card(state)
        with patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True
        ):
            async with _app_client(state) as client:
                resp = await client.post(
                    "/api/chat/slots/s1/approve", json={"action": action, "request_id": RID}
                )
        assert resp.status == 403
        assert not fut.done()
        _no_standing_grant(slot)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["approve", "reject"])
    async def test_the_id_route_refuses_an_app(self, state, action):
        _slot, fut = _pending_card(state)
        app = web.Application(middlewares=[_app_request])
        app["state"] = state
        app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
        with patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(f"/api/approvals/{RID}/{action}", json={})
        assert resp.status == 404
        assert not fut.done()

    @pytest.mark.asyncio
    async def test_an_app_still_answers_an_ordinary_card(self, state):
        _slot, fut = _pending_card(state, git_confirm=False)
        app = web.Application(middlewares=[_app_request])
        app["state"] = state
        app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
        with patch(
            "kiro_crew.apps.permissions.app_can_manage_session_approvals", return_value=True
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(f"/api/approvals/{RID}/approve", json={})
        assert resp.status == 200
        assert fut.result() == "approved"


class TestSlackTrustThisChannel:
    def test_the_channel_card_offers_no_trust_button(self, chan, monkeypatch):  # noqa: F811
        import kiro_crew.slack.handler as slack_handler

        monkeypatch.setattr(slack_handler, "_dashboard_state", chan.state)
        chan.slot.messages.append(_card_row(CHANNEL_REQUEST))
        _post_card(chan)
        _channel, blocks, _text, _thread = chan.slack.blocks_posts[-1]
        ids = [b["action_id"] for b in _buttons(blocks)]
        assert approval_picker.TRUST_CALL_ACTION_ID not in ids

    def test_a_trust_click_leaves_the_card_pending(self, chan):  # noqa: F811
        chan.slot.messages.append(_card_row(CHANNEL_REQUEST))
        ts = _post_card(chan)

        async def click() -> asyncio.Future:
            fut = asyncio.get_running_loop().create_future()
            chan.slot._approval_futures[CHANNEL_REQUEST] = fut
            assert await approval_picker.handle_trust_call(chan.orch, _click(OWNER, ts))
            return fut

        fut = asyncio.run(click())
        assert not fut.done()
        assert chan.slot._trust is False
        assert chan.state.sessions.policies == {}
        assert chan.resolved == []
        assert not [r for r in chan.mode_rows if r["operation"] == "mode_change:trust"]


class TestSlackWorkerTrust:
    def test_the_worker_card_offers_no_trust_button(self, relay):  # noqa: F811
        relay.worker.messages.append(_card_row(WORKER_REQUEST))
        asyncio.run(_run_relay(relay, tool_input="git commit -m x"))
        _channel, blocks, _text, _thread = relay.slack.blocks_posts[-1]
        ids = [e["action_id"] for b in blocks if b["type"] == "actions" for e in b["elements"]]
        assert ids == [wa.APPROVE_ACTION_ID, wa.REJECT_ACTION_ID]

    def test_a_trust_click_leaves_the_card_pending(self, relay):  # noqa: F811
        relay.worker.messages.append(_card_row(WORKER_REQUEST))

        async def go() -> asyncio.Future:
            card, fut = await _run_relay(relay, tool_input="git commit -m x")
            assert await wa.handle_click(relay.orch, _worker_click(OWNER, card, wa.TRUST_ACTION_ID))
            return fut

        fut = asyncio.run(go())
        assert not fut.done()
        assert relay.slot._trust is False and relay.worker._trust is False
        assert relay.resolved == []
        assert not [r for r in relay.mode_rows if r["operation"] == "mode_change:trust"]

    def test_approve_still_answers_it_once(self, relay):  # noqa: F811
        relay.worker.messages.append(_card_row(WORKER_REQUEST))

        async def go() -> asyncio.Future:
            card, fut = await _run_relay(relay, tool_input="git commit -m x")
            assert await wa.handle_click(
                relay.orch, _worker_click(OWNER, card, wa.APPROVE_ACTION_ID)
            )
            return fut

        fut = asyncio.run(go())
        assert fut.result() == "approved"
        assert relay.worker._trust is False


def _fenced_sections(blocks: list[dict[str, Any]]) -> list[str]:
    return [
        b["text"]["text"]
        for b in blocks
        if b.get("type") == "section" and b["text"]["text"].startswith("```")
    ]


class TestSlackCodeChannelCardStaysEscaped:
    """Dropping the Trust button keeps the Code-channel card's escaping."""

    def test_the_channel_card(self, chan, monkeypatch):  # noqa: F811
        import kiro_crew.slack.handler as slack_handler
        from kiro_crew.slack.handler import post_linked_approval

        monkeypatch.setattr(slack_handler, "_dashboard_state", chan.state)
        chan.slot.messages.append(_card_row(CHANNEL_REQUEST))
        ts = asyncio.run(
            post_linked_approval(
                chan.slack,
                chan.slot._slack_channel,
                None,
                CHANNEL_REQUEST,
                f"dashboard:{chan.slot.key}",
                "<!channel> commit",
                "git commit -m 'a ``` b <!channel>'",
                channel_trust=True,
                git_confirm=True,
            )
        )
        assert ts
        _channel, blocks, _text, _thread = chan.slack.blocks_posts[-1]
        assert approval_picker.TRUST_CALL_ACTION_ID not in [
            b["action_id"] for b in _buttons(blocks)
        ]
        text = json.dumps(blocks)
        assert "<!channel>" not in text
        for section in _fenced_sections(blocks):
            assert section.count("```") == 2

    def test_the_worker_card(self, relay):  # noqa: F811
        relay.worker.messages.append(_card_row(WORKER_REQUEST))
        asyncio.run(_run_relay(relay, tool_input="git commit -m 'a ``` b <!channel>'"))
        _channel, blocks, _text, _thread = relay.slack.blocks_posts[-1]
        assert "<!channel>" not in json.dumps(blocks)
        for section in _fenced_sections(blocks):
            assert section.count("```") == 2


class TestSlackLinkedDmTrust:
    """A ``trust_tool`` callback on a linked git-confirm card settles nothing."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("permitted", [True, False])
    async def test_the_card_stays_pending(self, monkeypatch, permitted):
        from test_slack_linked_approval import _FakeSlot
        from test_slack_linked_approval import _make_state as _linked_state

        import kiro_crew.slack.handler as slack_handler

        monkeypatch.setattr(slack_handler, "trust_permitted", lambda: permitted)
        slack_handler._linked_approvals["D_OWNER:TS1"] = slack_handler._LinkedApproval(
            request_id="99",
            session_key="dashboard:chat-1-123",
            trust_grantable=True,
            slot_key="chat-1-123",
        )
        slot = _FakeSlot(request_id="99", grantable=True)
        slot.messages[-1] = _card_row("99")
        dstate = _linked_state(slot)
        sessions = MagicMock()
        try:
            with (
                patch.object(slack_handler, "_dashboard_state", dstate),
                patch.object(slack_handler, "is_allowed_user", return_value=True),
                patch.object(slack_handler, "sel"),
            ):
                result = await slack_handler.handle_interaction(
                    "D_OWNER",
                    "TS1",
                    slack_handler._ACTION_TRUST,
                    user_id="U_OWNER",
                    sessions=sessions,
                )
            assert result is None
            dstate.resolve_slot_approval.assert_not_called()
            assert slot._trust is False
            sessions.set_approval_policy.assert_not_called()
            # The card is still answerable by Approve or Reject.
            assert "D_OWNER:TS1" in slack_handler._linked_approvals
        finally:
            slack_handler._linked_approvals.clear()
