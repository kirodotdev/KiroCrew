"""Per-tool session trust for MCP calls that carry arguments.

``approval_command`` deliberately returns no key once structured params are
present, so an argument-bearing MCP call has no exact tier. The per-tool tier binds a
SEPARATE identity-only key, ``mcp-trust-any:v1:<server>:<tool>``, so:

* a click on it covers later calls to the same tool with any arguments;
* the existing argument-free grant is never widened by it, nor it by that;
* the matcher still keys on the cached ACP identity, never the title.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_chat_runner_coverage import (
    _complete,
    _drive,
    _permission,
    _runner_state,
    _set_stream,
    _slot,
)
from test_trust_patterns import _make_app, _make_state

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.trust_patterns import (
    approval_command,
    approval_tool_scope_key,
    base_consent_pattern,
    canonical_non_shell_any_args_trust_key,
    canonical_non_shell_tool,
    canonical_non_shell_trust_key,
    exact_trust_pattern,
    matches_trusted_pattern,
    shell_grant_names_reserved_key,
)

SERVER = "github"
TOOL = "get_issue"
PARAMS = {"owner": "o", "repo": "r", "issue_number": 1}


def _mcp_permission(**overrides):
    kwargs = {
        "title": "Fetching the issue",
        "tool_input": "",
        "tool_kind": "other",
        "is_shell": False,
        "tool_name": TOOL,
        "mcp_server_name": SERVER,
        "raw_tool_params": dict(PARAMS),
    }
    kwargs.update(overrides)
    return _permission(**kwargs)


class TestToolScopeKey:
    def test_key_is_identity_only_and_ignores_arguments(self):
        key = approval_tool_scope_key(is_shell=False, tool_name=TOOL, mcp_server_name=SERVER)
        assert key == canonical_non_shell_any_args_trust_key(SERVER, TOOL)
        assert key.startswith("mcp-trust-any:v1:")

    def test_key_differs_from_the_argument_free_key(self):
        # The two tiers must never stand in for each other.
        any_args = canonical_non_shell_any_args_trust_key(SERVER, TOOL)
        exact = canonical_non_shell_trust_key(SERVER, TOOL)
        assert any_args != exact
        assert matches_trusted_pattern(exact, {exact_trust_pattern(any_args)}) is None
        assert matches_trusted_pattern(any_args, {exact_trust_pattern(exact)}) is None

    def test_component_encoding_still_prevents_separator_collision(self):
        assert canonical_non_shell_any_args_trust_key(
            "github", "repo__delete"
        ) != canonical_non_shell_any_args_trust_key("github__repo", "delete")

    @pytest.mark.parametrize("server,tool", [("", TOOL), (SERVER, ""), ("", "")])
    def test_incomplete_identity_fails_closed(self, server, tool):
        assert approval_tool_scope_key(is_shell=False, tool_name=tool, mcp_server_name=server) == ""

    def test_shell_never_gets_a_tool_scope_key(self):
        assert approval_tool_scope_key(is_shell=True, tool_name=TOOL, mcp_server_name=SERVER) == ""

    def test_argument_free_key_still_refuses_structured_params(self):
        # Unchanged contract: the exact tier stays argument-free only.
        assert (
            approval_command(
                "", is_shell=False, tool_name=TOOL, mcp_server_name=SERVER, raw_tool_params=PARAMS
            )
            == ""
        )


def _card_meta(slot):
    (card,) = [m for m in slot.messages if m.get("role") == "permission"]
    return json.loads(card["cls"])


class TestCardOffersTheToolTier:
    @pytest.mark.asyncio
    async def test_argument_bearing_mcp_card_offers_per_tool_tier(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_mcp_permission(), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        meta = _card_meta(slot)
        assert meta["trust_base_grantable"] == "1"
        assert meta["base_command"] == canonical_non_shell_tool(SERVER, TOOL)
        assert meta["trust_base_key"] == canonical_non_shell_any_args_trust_key(SERVER, TOOL)
        # The exact tier is still withheld for an argument-bearing call.
        assert "trust_command_grantable" not in meta
        assert meta["trust_grantable"] == "1"

    @pytest.mark.asyncio
    async def test_redacted_call_offers_no_tool_tier(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        permission = _mcp_permission()
        permission.tool_input_redacted = True
        _set_stream(client, [permission, _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        meta = _card_meta(slot)
        assert "trust_base_grantable" not in meta
        assert "trust_base_key" not in meta

    @pytest.mark.asyncio
    async def test_missing_identity_offers_no_tool_tier(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_mcp_permission(mcp_server_name=""), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        meta = _card_meta(slot)
        assert "trust_base_grantable" not in meta

    @pytest.mark.asyncio
    async def test_shell_card_base_tier_is_unchanged(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(
            client, [_permission(tool_input=json.dumps({"command": "git status"})), _complete()]
        )

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        meta = _card_meta(slot)
        assert meta["base_command"] == "git"
        assert "trust_base_key" not in meta


class TestMatcherHonoursTheToolGrant:
    @pytest.mark.asyncio
    async def test_tool_grant_auto_approves_an_argument_bearing_call(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {
            exact_trust_pattern(canonical_non_shell_any_args_trust_key(SERVER, TOOL))
        }
        _set_stream(client, [_mcp_permission(), _complete()])

        await _drive(state, slot)

        client.approve_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_tool_grant_does_not_cover_another_tool_on_the_same_server(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {
            exact_trust_pattern(canonical_non_shell_any_args_trust_key(SERVER, TOOL))
        }
        _set_stream(client, [_mcp_permission(tool_name="delete_issue"), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()
        client.reject_tool.assert_awaited_once_with("req-cov-1")

    @pytest.mark.asyncio
    async def test_argument_free_grant_still_does_not_cover_arguments(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {exact_trust_pattern(canonical_non_shell_trust_key(SERVER, TOOL))}
        _set_stream(client, [_mcp_permission(), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_redacted_call_never_matches_the_tool_grant(self, tmp_path):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        slot._trusted_patterns = {
            exact_trust_pattern(canonical_non_shell_any_args_trust_key(SERVER, TOOL))
        }
        permission = _mcp_permission()
        permission.tool_input_redacted = True
        _set_stream(client, [permission, _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        client.approve_tool.assert_not_awaited()


class TestHandlerStoresTheServerKey:
    @staticmethod
    def _pending(slot, request_id, *, with_key=True):
        meta = {
            "request_id": request_id,
            "base_command": canonical_non_shell_tool(SERVER, TOOL),
            "trust_base_grantable": "1",
            "trust_grantable": "1",
        }
        if with_key:
            meta["trust_base_key"] = canonical_non_shell_any_args_trust_key(SERVER, TOOL)
        slot.messages.append({"role": "permission", "content": "Fetching", "cls": json.dumps(meta)})

    @pytest.mark.asyncio
    async def test_trust_base_on_mcp_card_stores_only_the_identity_key(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _ChatSlot(key="slot-1")
        state._slots["slot-1"] = slot
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-tool"] = fut
        self._pending(slot, "req-tool")

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/slot-1/approve",
                json={
                    "action": "trust_base",
                    "request_id": "req-tool",
                    # utils/trustPatterns.trustBasePattern of the display label
                    "pattern": base_consent_pattern(canonical_non_shell_tool(SERVER, TOOL)),
                },
            )
            assert response.status == 200

        key = canonical_non_shell_any_args_trust_key(SERVER, TOOL)
        assert slot._trusted_patterns == {exact_trust_pattern(key)}
        # The display spelling never became a glob grant.
        assert (
            matches_trusted_pattern("mcp__github__get_issue anything", slot._trusted_patterns)
            is None
        )
        assert fut.result() == "approved"

    @pytest.mark.asyncio
    async def test_mismatched_consent_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        slot = _ChatSlot(key="slot-1")
        state._slots["slot-1"] = slot
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        slot._approval_futures["req-tool"] = fut
        self._pending(slot, "req-tool")

        async with TestClient(TestServer(_make_app(state))) as client:
            response = await client.post(
                "/api/chat/slots/slot-1/approve",
                json={
                    "action": "trust_base",
                    "request_id": "req-tool",
                    "pattern": "mcp__github__* *",
                },
            )
            assert response.status == 400

        assert slot._trusted_patterns == set()
        assert not fut.done()


class TestShellGrantsCannotSpellAnMcpKey:
    """Shell and MCP grants share one store; a shell grant must not mint an MCP key."""

    @pytest.mark.parametrize(
        "command",
        [
            canonical_non_shell_any_args_trust_key(SERVER, TOOL),
            canonical_non_shell_trust_key(SERVER, TOOL),
            canonical_non_shell_any_args_trust_key(SERVER, TOOL).upper(),
            canonical_non_shell_any_args_trust_key(SERVER, TOOL) + " --flag",
            "echo hi | " + canonical_non_shell_any_args_trust_key(SERVER, TOOL),
        ],
    )
    def test_reserved_spellings_are_detected(self, command):
        assert shell_grant_names_reserved_key(command)

    @pytest.mark.parametrize("command", ["git status", "echo mcp-trust-any:v1:aa:bb", "ls | wc -l"])
    def test_ordinary_commands_are_not_reserved(self, command):
        assert not shell_grant_names_reserved_key(command)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "command",
        [
            canonical_non_shell_any_args_trust_key(SERVER, TOOL),
            canonical_non_shell_any_args_trust_key(SERVER, TOOL) + " x",
        ],
    )
    async def test_shell_card_spelling_an_mcp_key_offers_no_scoped_tier(self, tmp_path, command):
        state, client = _runner_state(tmp_path)
        slot = _slot()
        _set_stream(client, [_permission(tool_input=json.dumps({"command": command})), _complete()])

        with patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0):
            await _drive(state, slot)

        meta = _card_meta(slot)
        assert "trust_command_grantable" not in meta
        assert "trust_command_key" not in meta
        assert "trust_base_grantable" not in meta
        assert meta["trust_grantable"] == "1"
