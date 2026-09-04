"""Tests for /api/channels/{id}/agents/{aid}/approve handler.

Covers the trust-tier allowlist (issue #5231): channel approval cards offer
``trust_command`` / ``trust_base`` via the shared TrustDropdown. Grants are
derived SERVER-SIDE from the pending approval's canonical shell command, the
client-supplied pattern serves as the consent proof (mismatch fails closed),
and the grants themselves are OPAQUE LITERALS — the exact tier stores the
whole command text (string equality), the base tier stores one shlex-derived
binary name (refused for compound / quoted / env-prefixed commands). No
pattern language exists: every derivation scheme reviewed on this surface
widened scope beyond what the card displayed.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.channel import _match_trusted_channel_command
from kiro_crew.dashboard.handlers_channel import api_channel_approve_agent


def _make_agent(pending: bool = True, pending_command: str = "ls /tmp"):
    agent = MagicMock()
    agent.id = "a1"
    agent.session_key = "channel:ch1:a1"
    agent.agent_name = "dev"
    agent._trusted_commands = set()
    agent._trusted_bases = set()
    agent._pending_approval_command = pending_command if pending else ""
    if pending:
        agent._approval_future = asyncio.get_running_loop().create_future()
    else:
        agent._approval_future = None
    return agent


def _make_channel(agent):
    ch = MagicMock()
    ch.id = "ch1"
    ch.members = {"a1": agent}
    ch.trusted = False
    ch._save = MagicMock()
    return ch


def _make_request(body: dict):
    request = MagicMock()
    request.match_info = {"id": "ch1", "aid": "a1"}
    request.json = AsyncMock(return_value=body)
    request.app = {"state": MagicMock()}
    return request


async def _call(body: dict, agent):
    ch = _make_channel(agent)
    request = _make_request(body)
    with (
        patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ),
        patch("kiro_crew.dashboard.handlers_channel.sel", return_value=MagicMock()),
    ):
        resp = await api_channel_approve_agent(request)
    return resp, ch


class TestChannelApproveTrustTiers:
    @pytest.mark.asyncio
    async def test_trust_command_binds_pending_command_and_approves(self):
        agent = _make_agent(pending_command="grep -r foo .")
        resp, ch = await _call({"action": "trust_command", "pattern": "grep -r foo ."}, agent)
        assert resp.status == 200
        assert json.loads(resp.body)["ok"] is True
        assert agent._trusted_commands == {"grep -r foo ."}
        assert agent._trusted_bases == set()
        # The pending tool is approved — the waiter must proceed, not time out.
        assert agent._approval_future.result() == "approved"
        # Per-command trust NEVER widens to channel-level trust.
        assert ch.trusted is False
        ch._save.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_card_pattern_is_refused(self):
        """A click on an OLD approval card (its pattern describes a command
        that is no longer the pending one) must not trust the newer pending
        command."""
        agent = _make_agent(pending_command="rm -rf /data")
        resp, _ = await _call({"action": "trust_command", "pattern": "ls /tmp"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "approval_superseded"
        assert agent._trusted_commands == set()
        assert not agent._approval_future.done()

    @pytest.mark.asyncio
    async def test_wildcard_client_pattern_never_scopes_the_grant(self):
        """An LLM-authored title like 'Running: *' would make the card send
        pattern='*'; it mismatches the real pending command and fails closed."""
        agent = _make_agent(pending_command="ls /tmp")
        resp, _ = await _call({"action": "trust_command", "pattern": "*"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "approval_superseded"
        assert agent._trusted_commands == set()

    @pytest.mark.asyncio
    async def test_exact_grant_is_a_literal_not_a_glob(self):
        """Trusting 'rm *.tmp' must trust that literal command text, not
        every 'rm <anything>.tmp' — grants carry no pattern semantics."""
        agent = _make_agent(pending_command="rm *.tmp")
        resp, _ = await _call({"action": "trust_command", "pattern": "rm *.tmp"}, agent)
        assert resp.status == 200
        assert _match_trusted_channel_command("rm *.tmp", agent)
        assert not _match_trusted_channel_command("rm secret.tmp", agent)

    @pytest.mark.asyncio
    async def test_exact_grant_preserves_leading_and_trailing_whitespace(self):
        """The exact tier binds the command bytes the user saw. Trimming in
        the handler would silently change both the consent proof and grant."""
        cmd = "  printf safe  "
        agent = _make_agent(pending_command=cmd)
        resp, _ = await _call({"action": "trust_command", "pattern": cmd}, agent)
        assert resp.status == 200
        assert _match_trusted_channel_command(cmd, agent)
        assert not _match_trusted_channel_command(cmd.strip(), agent)

    @pytest.mark.asyncio
    async def test_matching_is_case_sensitive(self):
        """On POSIX, ./Deploy.sh and ./deploy.sh are different executables."""
        agent = _make_agent(pending_command="./Deploy.sh")
        resp, _ = await _call({"action": "trust_command", "pattern": "./Deploy.sh"}, agent)
        assert resp.status == 200
        assert _match_trusted_channel_command("./Deploy.sh", agent)
        assert not _match_trusted_channel_command("./deploy.sh", agent)

    @pytest.mark.asyncio
    async def test_compound_exact_grant_matches_only_the_identical_pipeline(self):
        """A compound command is granted as ONE opaque literal: the identical
        pipeline auto-approves, but a segment lifted out of its shell context
        does NOT ('cd /tmp/safe && rm target' never licenses a bare
        'rm target' elsewhere)."""
        cmd = "cd /tmp/safe && rm target"
        agent = _make_agent(pending_command=cmd)
        resp, _ = await _call({"action": "trust_command", "pattern": cmd}, agent)
        assert resp.status == 200
        assert _match_trusted_channel_command(cmd, agent)
        assert not _match_trusted_channel_command("rm target", agent)
        assert not _match_trusted_channel_command(f"{cmd} && rm -rf /", agent)

    @pytest.mark.asyncio
    async def test_trust_base_grants_exactly_the_consented_binary(self):
        agent = _make_agent(pending_command="ls /tmp")
        resp, _ = await _call({"action": "trust_base", "pattern": "ls *"}, agent)
        assert resp.status == 200
        assert agent._trusted_bases == {"ls"}
        assert agent._approval_future.result() == "approved"
        # The grant covers other simple ls invocations...
        assert _match_trusted_channel_command("ls -la /var", agent)
        assert _match_trusted_channel_command("ls", agent)
        # ...but never a compound command or a different binary.
        assert not _match_trusted_channel_command("ls /tmp | rm -rf /", agent)
        assert not _match_trusted_channel_command("lsof", agent)
        assert not _match_trusted_channel_command("LS /tmp", agent)

    @pytest.mark.asyncio
    async def test_trust_base_refused_for_compound_commands(self):
        """A compound command has no single base to consent to — a later
        standalone segment would run outside the shell context the user
        read."""
        agent = _make_agent(pending_command="cat /etc/hosts | wc -l")
        resp, _ = await _call({"action": "trust_base", "pattern": "cat *"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "pattern_underivable"
        assert agent._trusted_bases == set()

    @pytest.mark.asyncio
    async def test_trust_base_refused_for_quoted_executables(self):
        """Naive first-token slicing turned '"./my tool" --safe' into a
        './my'-prefixed grant covering different executables; shlex-aware
        derivation refuses quoted/space-bearing bases outright."""
        agent = _make_agent(pending_command='"./my tool" --safe')
        resp, _ = await _call({"action": "trust_base", "pattern": '"./my *'}, agent)
        assert resp.status == 400
        assert agent._trusted_bases == set()

    @pytest.mark.asyncio
    async def test_trust_base_refused_for_env_assignment_prefix(self):
        """'FOO=bar echo safe' has no unambiguous binary first token — a
        grant keyed on the prefix would cover ANY command run under it."""
        agent = _make_agent(pending_command="FOO=bar echo safe")
        resp, _ = await _call({"action": "trust_base", "pattern": "FOO=bar *"}, agent)
        assert resp.status == 400
        assert agent._trusted_bases == set()

    @pytest.mark.asyncio
    async def test_trust_base_refused_for_subshell_grouping(self):
        """'(rm /tmp/a)' must not yield '(rm' as a trusted binary — a grant
        keyed on it would auto-approve '(rm <anything>)'."""
        agent = _make_agent(pending_command="(rm /tmp/a)")
        resp, _ = await _call({"action": "trust_base", "pattern": "(rm *"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "pattern_underivable"
        assert agent._trusted_bases == set()

    @pytest.mark.asyncio
    async def test_trust_base_refused_for_carriage_return(self):
        """'\\r' is a shlex word separator but NOT a shell one — the shell
        executes 'ls\\r/bin/true' as one planted relative path while shlex
        reads a trusted 'ls'. A parser differential must have no derivable
        base."""
        agent = _make_agent(pending_command="ls\r/bin/true")
        resp, _ = await _call({"action": "trust_base", "pattern": "ls *"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "pattern_underivable"
        assert agent._trusted_bases == set()

    @pytest.mark.asyncio
    async def test_trust_base_refused_for_shell_words_and_odd_bases(self):
        """First tokens the shell interprets (or that don't look like a
        binary at all) must have no derivable base: '! false' would grant
        '!', 'time rm x' would grant 'time', '[ -f x ]' would grant '['."""
        for cmd in ("! false", "time rm /tmp/a", "if true", "[ -f /tmp/x ]"):
            agent = _make_agent(pending_command=cmd)
            resp, _ = await _call({"action": "trust_base", "pattern": "*"}, agent)
            assert resp.status == 400, cmd
            assert json.loads(resp.body)["code"] == "pattern_underivable", cmd
            assert agent._trusted_bases == set(), cmd

    @pytest.mark.asyncio
    async def test_trust_base_foreign_base_is_refused(self):
        """A base that is not the pending command's binary is a stale card."""
        agent = _make_agent(pending_command="ls /tmp")
        resp, _ = await _call({"action": "trust_base", "pattern": "rm *"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "approval_superseded"
        assert agent._trusted_bases == set()

    @pytest.mark.asyncio
    async def test_substitution_command_is_exact_only(self):
        """A substitution-bearing command can be trusted as its exact literal
        text (identical bytes re-approve), but never as a base grant."""
        cmd = "echo $(whoami)"
        agent = _make_agent(pending_command=cmd)
        resp, _ = await _call({"action": "trust_base", "pattern": "echo *"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "pattern_underivable"
        resp, _ = await _call({"action": "trust_command", "pattern": cmd}, agent)
        assert resp.status == 200
        assert _match_trusted_channel_command(cmd, agent)
        assert not _match_trusted_channel_command("echo $(id)", agent)

    @pytest.mark.asyncio
    async def test_trust_denials_are_sel_audited(self):
        """Every trust refusal must land in the audit trail — an unlogged
        denial hides a stale-card click from the security record."""
        agent = _make_agent(pending_command="rm -rf /data")
        ch = _make_channel(agent)
        request = _make_request({"action": "trust_command", "pattern": "ls /tmp"})
        sel_mock = MagicMock()
        with (
            patch(
                "kiro_crew.dashboard.handlers_channel._mgr",
                return_value=MagicMock(get=MagicMock(return_value=ch)),
            ),
            patch("kiro_crew.dashboard.handlers_channel.sel", return_value=sel_mock),
        ):
            resp = await api_channel_approve_agent(request)
        assert resp.status == 400
        outcomes = [kw.get("outcome") for _, kw in sel_mock.log_tool_invocation.call_args_list]
        assert "trust_pattern_denied" in outcomes

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["trust_command", "trust_base"])
    async def test_missing_pattern_is_a_distinct_400(self, action):
        agent = _make_agent(pending_command="ls /tmp")
        resp, _ = await _call({"action": action}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "pattern_required"
        assert agent._trusted_commands == set()
        assert agent._trusted_bases == set()
        assert not agent._approval_future.done()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["trust_command", "trust_base"])
    async def test_non_shell_pending_tool_is_a_distinct_400(self, action):
        """A pending MCP/non-shell tool has no canonical command to bind to."""
        agent = _make_agent(pending_command="")
        resp, _ = await _call({"action": action, "pattern": "ls *"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["code"] == "pattern_underivable"
        assert agent._trusted_commands == set()
        assert agent._trusted_bases == set()
        assert not agent._approval_future.done()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["trust_command", "trust_base"])
    async def test_trust_tier_without_pending_approval_is_400(self, action):
        agent = _make_agent(pending=False)
        resp, _ = await _call({"action": action, "pattern": "ls *"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["error"] == "no pending approval"
        assert agent._trusted_commands == set()
        assert agent._trusted_bases == set()


class TestChannelApproveRegression:
    @pytest.mark.asyncio
    async def test_invalid_action_still_400(self):
        agent = _make_agent()
        resp, _ = await _call({"action": "yolo"}, agent)
        assert resp.status == 400
        assert json.loads(resp.body)["error"] == "invalid action"

    @pytest.mark.asyncio
    async def test_approved_resolves_future(self):
        agent = _make_agent()
        resp, ch = await _call({"action": "approved"}, agent)
        assert resp.status == 200
        assert agent._approval_future.result() == "approved"
        assert ch.trusted is False

    @pytest.mark.asyncio
    async def test_rejected_resolves_future(self):
        agent = _make_agent()
        resp, _ = await _call({"action": "rejected"}, agent)
        assert resp.status == 200
        assert agent._approval_future.result() == "rejected"

    @pytest.mark.asyncio
    async def test_trust_sets_channel_trusted_and_saves(self):
        agent = _make_agent()
        resp, ch = await _call({"action": "trust"}, agent)
        assert resp.status == 200
        assert agent._approval_future.result() == "trust"
        assert ch.trusted is True
        ch._save.assert_called_once()
