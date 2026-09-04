"""Tests for the suggest_followup tool schema and POST /api/chat/slots/{slot}/followup.

Covers the two gates the feature relies on: the MCP-layer arg schema
(:data:`SUGGEST_FOLLOWUP_SCHEMA`) and the gateway endpoint that re-validates the
same payload and broadcasts the card.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_followup
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.validation import (
    MAX_FOLLOWUP_PROMPT,
    MAX_FOLLOWUP_TITLE,
    SUGGEST_FOLLOWUP_SCHEMA,
    ValidationError,
    validate_tool_args,
)


def _item(**over: object) -> dict:
    base = {
        "title": "Add rate limiting",
        "description": "The upload endpoint is unbounded.",
        "prompt": "Add a token-bucket rate limiter to POST /api/upload in server.py.",
    }
    base.update(over)
    return base


def _make_app(
    state: DashboardState,
    *,
    app_claim: str | None = "",
    user: str = "owner",
    internal: bool = False,
) -> web.Application:
    """App whose middleware mimics ``token_auth_middleware``'s request claims.

    ``app_claim=""`` is a dashboard user, a non-empty string is an app caller, and
    ``None`` leaves the key ABSENT (as if the auth middleware never ran) — which
    the endpoint must treat as unauthorized rather than falling through.
    """

    @web.middleware
    async def claims(request: web.Request, handler):
        if app_claim is not None:
            request["app"] = app_claim
        if internal:
            # What the auth middleware sets for a valid internal-secret request
            # from loopback: granted, but with NO app identity.
            request["internal_auth"] = True
        request["user"] = user
        return await handler(request)

    app = web.Application(middlewares=[claims])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/followup", api_chat_slot_followup)
    return app


def _mock_state(slot: _ChatSlot | None = None, ws_clients: int = 1) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    # `is_owner_dashboard_request` compares the request's subject with this, so a
    # MagicMock attribute here would refuse every request for the wrong reason.
    state.owner_id = "owner"
    state.broadcast_ws = MagicMock()
    state.ws_client_count = MagicMock(return_value=ws_clients)
    state.deliver_ws_owners = AsyncMock(return_value=ws_clients)
    return state


class TestSuggestFollowupSchema:
    def test_minimal_valid_item(self):
        cleaned = validate_tool_args({"items": [_item()]}, SUGGEST_FOLLOWUP_SCHEMA)
        assert cleaned["items"][0]["title"] == "Add rate limiting"
        assert "branch" not in cleaned["items"][0]

    def test_accepts_optional_branch(self):
        cleaned = validate_tool_args(
            {"items": [_item(branch="feat/upload-limit")]}, SUGGEST_FOLLOWUP_SCHEMA
        )
        assert cleaned["items"][0]["branch"] == "feat/upload-limit"

    def test_rejects_empty_items(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"items": []}, SUGGEST_FOLLOWUP_SCHEMA)

    def test_rejects_more_than_three_items(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"items": [_item()] * 4}, SUGGEST_FOLLOWUP_SCHEMA)

    def test_rejects_missing_prompt(self):
        bad = _item()
        del bad["prompt"]
        with pytest.raises(ValidationError):
            validate_tool_args({"items": [bad]}, SUGGEST_FOLLOWUP_SCHEMA)

    def test_rejects_non_object_item(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"items": ["just a string"]}, SUGGEST_FOLLOWUP_SCHEMA)

    def test_rejects_unknown_item_field(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"items": [_item(autoSend=True)]}, SUGGEST_FOLLOWUP_SCHEMA)

    def test_rejects_unknown_top_level_field(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"items": [_item()], "slot": "x"}, SUGGEST_FOLLOWUP_SCHEMA)

    def test_rejects_oversized_title(self):
        with pytest.raises(ValidationError):
            validate_tool_args(
                {"items": [_item(title="x" * (MAX_FOLLOWUP_TITLE + 1))]}, SUGGEST_FOLLOWUP_SCHEMA
            )

    def test_accepts_prompt_at_limit(self):
        cleaned = validate_tool_args(
            {"items": [_item(prompt="x" * MAX_FOLLOWUP_PROMPT)]}, SUGGEST_FOLLOWUP_SCHEMA
        )
        assert len(cleaned["items"][0]["prompt"]) == MAX_FOLLOWUP_PROMPT

    def test_rejects_whitespace_only_title(self):
        with pytest.raises(ValidationError):
            validate_tool_args({"items": [_item(title="   ")]}, SUGGEST_FOLLOWUP_SCHEMA)

    @pytest.mark.parametrize(
        "branch",
        [
            "--upload-limits",  # git would read a leading dash as a flag
            "feat/../../etc",  # path traversal via ref name
            "feat/upload limits",  # whitespace
            "feat/upload;rm -rf /",  # shell metacharacters
            "feat/upload@{0}",  # reflog syntax
            "feat/upload~1",
            "feat/upload^",
            "feat/upload:branch",
            "feat//double-slash",
            "/leading-slash",
        ],
    )
    def test_rejects_dangerous_branch_names(self, branch):
        with pytest.raises(ValidationError):
            validate_tool_args({"items": [_item(branch=branch)]}, SUGGEST_FOLLOWUP_SCHEMA)

    def test_empty_branch_is_treated_as_absent(self):
        cleaned = validate_tool_args({"items": [_item(branch="")]}, SUGGEST_FOLLOWUP_SCHEMA)
        assert "branch" not in cleaned["items"][0]

    def test_strips_hidden_unicode_from_title(self):
        # Zero-width space (Cf) must not survive into the rendered card.
        cleaned = validate_tool_args(
            {"items": [_item(title="Add\u200brate limiting")]}, SUGGEST_FOLLOWUP_SCHEMA
        )
        assert "\u200b" not in cleaned["items"][0]["title"]


class TestFollowupCallerIsolation:
    """Round 8 HIGH: an app caller could raise a card on a slot it does not own,
    and an all-clients broadcast handed it another user's handoff prompts."""

    @pytest.mark.asyncio
    async def test_non_owner_dashboard_subject_is_refused(self):
        """Round 12 BLOCKING: an app claim of "" is necessary, not sufficient.

        A dashboard token minted for a different subject carries ``app == ""``
        and sailed through the round-8 gate, so it could raise cards in the
        owner's composer — and, on the sibling endpoint, create worktrees in the
        owner's repositories.
        """
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        app = _make_app(state, app_claim="", user="somebody-else")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 403
        state.deliver_ws_owners.assert_not_called()
        state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_local_install_without_a_configured_owner_is_allowed(self):
        """The standalone-local case: no owner_id, browser token minted for
        `local-app`. Refusing it would break the feature for every install that
        has not configured an owner."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.owner_id = ""
        app = _make_app(state, app_claim="", user="local-app")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 200, await resp.text()
        state.deliver_ws_owners.assert_called_once()

    @pytest.mark.asyncio
    async def test_unsigned_subject_without_a_configured_owner_is_refused(self):
        """No owner configured is not a free pass: only the signed local
        bootstrap subjects are accepted in that mode."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.owner_id = ""
        app = _make_app(state, app_claim="", user="drive-by")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 403
        state.deliver_ws_owners.assert_not_called()

    @pytest.mark.asyncio
    async def test_app_caller_is_refused(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        app = _make_app(state, app_claim="some-app", user="some-app")
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 403
        state.deliver_ws_owners.assert_not_called()
        state.broadcast_ws.assert_not_called()

    @pytest.mark.asyncio
    async def test_internal_loopback_caller_is_allowed(self):
        """Round 9: the MCP path authenticates by internal secret and carries NO
        app claim, so a bare deny-on-absent gate 403'd every `suggest_followup`."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        app = _make_app(state, app_claim=None, internal=True)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 200, await resp.text()
        state.deliver_ws_owners.assert_called_once()

    @pytest.mark.asyncio
    async def test_absent_auth_claim_is_refused(self):
        """An absent claim means the auth middleware never ran — fail closed."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        app = _make_app(state, app_claim=None)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 403
        state.deliver_ws_owners.assert_not_called()

    @pytest.mark.asyncio
    async def test_card_never_uses_the_all_clients_broadcast(self):
        """An app caller can open /api/ws, so the card must not go to everyone."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 200
        state.broadcast_ws.assert_not_called()
        state.deliver_ws_owners.assert_called_once()

    @pytest.mark.asyncio
    async def test_delivered_counts_owner_clients_only(self):
        """`delivered` must describe the channel the card actually went down."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.ws_client_count = MagicMock(return_value=7)
        state.deliver_ws_owners = AsyncMock(return_value=0)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 200
            assert (await resp.json())["delivered"] == 0


class TestRealMiddlewareIntegration:
    """End-to-end through the REAL auth middleware, not a hand-rolled claims stub.

    Round 9 caught this with a stub-only suite: the loopback internal-secret branch
    grants the request but sets no app claim, so a deny-on-absent gate refused every
    MCP call. These tests pin the contract at the seam where it actually broke.
    """

    @staticmethod
    def _app(state: DashboardState, secret: str) -> web.Application:
        from kiro_crew.dashboard.token_auth import token_auth_middleware

        mw = token_auth_middleware(
            mixed_internal_paths=frozenset({"/api/chat"}),
            internal_secret=secret,
        )
        app = web.Application(middlewares=[mw])
        app["state"] = state
        app.router.add_post("/api/chat/slots/{slot}/followup", api_chat_slot_followup)
        return app

    @pytest.mark.asyncio
    async def test_internal_secret_from_loopback_reaches_the_handler(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(self._app(state, "s3cr3t"))) as client:
            resp = await client.post(
                "/api/chat/slots/test/followup",
                json={"items": [_item()]},
                headers={"X-Internal-Secret": "s3cr3t"},
            )
            assert resp.status == 200, await resp.text()
        state.deliver_ws_owners.assert_called_once()

    @pytest.mark.asyncio
    async def test_wrong_internal_secret_never_reaches_the_handler(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(self._app(state, "s3cr3t"))) as client:
            resp = await client.post(
                "/api/chat/slots/test/followup",
                json={"items": [_item()]},
                headers={"X-Internal-Secret": "wrong"},
            )
            assert resp.status in (401, 403)
        state.deliver_ws_owners.assert_not_called()


class TestFollowupEndpoint:
    @pytest.mark.asyncio
    async def test_broadcasts_card(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 200
            assert (await resp.json())["count"] == 1
        msg_type, payload = state.deliver_ws_owners.call_args[0]
        assert msg_type == "followup_card"
        assert payload["slot"] == "test"
        assert payload["items"][0]["title"] == "Add rate limiting"

    @pytest.mark.asyncio
    async def test_unknown_slot_returns_404(self):
        state = _mock_state()
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/followup", json={"items": [_item()]})
            assert resp.status == 404
        state.deliver_ws_owners.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/followup",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        state.deliver_ws_owners.assert_not_called()

    @pytest.mark.asyncio
    async def test_endpoint_revalidates_schema(self):
        """The endpoint is its own trust boundary, not a relay for the MCP layer."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/followup",
                json={"items": [_item(branch="-rf")]},
            )
            assert resp.status == 400
        state.deliver_ws_owners.assert_not_called()

    @pytest.mark.asyncio
    async def test_credentials_are_redacted_before_broadcast(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        secret = "AKIAIOSFODNN7EXAMPLE"
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/followup",
                json={"items": [_item(prompt=f"Rotate the key {secret} in config.")]},
            )
            assert resp.status == 200
        payload = state.deliver_ws_owners.call_args[0][1]
        assert secret not in payload["items"][0]["prompt"]

    @pytest.mark.asyncio
    async def test_credential_shaped_branch_is_dropped_not_broadcast(self):
        """GPT round 4 HIGH: `branch` skipped the redactors, yet it travels the
        furthest — into a git ref, a directory name, SEL records and logs. A
        credential-shaped value satisfies FOLLOWUP_BRANCH_RE, so the field is
        dropped whenever redaction would alter it; the card then derives a branch
        from the title instead.
        """
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/followup",
                json={"items": [_item(branch="AKIAIOSFODNN7EXAMPLE")]},
            )
            assert resp.status == 200
        payload = state.deliver_ws_owners.call_args[0][1]
        assert "branch" not in payload["items"][0]

    @pytest.mark.asyncio
    async def test_ordinary_branch_survives_redaction(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/followup",
                json={"items": [_item(branch="feat/upload-rate-limit")]},
            )
            assert resp.status == 200
        payload = state.deliver_ws_owners.call_args[0][1]
        assert payload["items"][0]["branch"] == "feat/upload-rate-limit"

    @pytest.mark.asyncio
    async def test_reports_connected_client_count(self):
        """The tool needs the truth: a card with no listener was not shown."""
        slot = _ChatSlot("test")
        state = _mock_state(slot, ws_clients=2)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert (await resp.json())["delivered"] == 2

    @pytest.mark.asyncio
    async def test_reports_zero_delivered_with_no_clients(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot, ws_clients=0)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 200
            assert (await resp.json())["delivered"] == 0

    @pytest.mark.asyncio
    async def test_client_count_failure_degrades_to_zero(self):
        """A delivery error must be reported as "nobody saw it", not a 500."""
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        state.deliver_ws_owners = AsyncMock(side_effect=RuntimeError("boom"))
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/followup", json={"items": [_item()]})
            assert resp.status == 200
            assert (await resp.json())["delivered"] == 0
        state.deliver_ws_owners.assert_called_once()

    @pytest.mark.asyncio
    async def test_json_array_body_rejected(self):
        slot = _ChatSlot("test")
        state = _mock_state(slot)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/followup", json=[_item()])
            assert resp.status == 400
        state.deliver_ws_owners.assert_not_called()
