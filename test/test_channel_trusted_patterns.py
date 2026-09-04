"""Channel-agent per-command trust grants (issue #5231).

``trust_command`` / ``trust_base`` record agent-scoped patterns via the
approve endpoint; ``_stream_task`` must auto-approve a subsequent tool call
whose ACTUAL command (extracted from ``tool_input`` only when the provider
classified the tool as shell, never the LLM-authored display title) matches
a granted pattern — and must keep prompting for everything else
(deny-by-default).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import name_grant
from kiro_crew.channel import ChannelAgent, _stream_task
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST


def _make_agent(commands: set[str] | None = None, bases: set[str] | None = None):
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:test",
        _approval_future=None,
        _trusted_commands=commands or set(),
        _trusted_bases=bases or set(),
        _pending_approval_command="",
    )


def _make_channel(agent=None, decision: str = "rejected", capture=None):
    """Channel stub whose ``post`` resolves the agent's pending approval
    future — the interactive path otherwise awaits it for 3600s. ``capture``
    (optional list) records the agent's stashed pending command at post time,
    i.e. what the approve endpoint would bind a trust grant to."""
    ch = SimpleNamespace(id="c1", trusted=False, members={})
    ch._broadcast = MagicMock()

    async def _post(*args, **kw):
        if agent is not None and kw.get("msg_type") == "approval":
            if capture is not None:
                capture.append(agent._pending_approval_command)
            if agent._approval_future is not None and not agent._approval_future.done():
                agent._approval_future.set_result(decision)

    ch.post = AsyncMock(side_effect=_post)
    return ch


def _make_client(events):
    client = SimpleNamespace()

    async def _stream(message):
        for ev in events:
            yield ev

    client.stream = _stream
    client.approve_tool = AsyncMock()
    client.reject_tool = AsyncMock()
    return client


def _perm_event(*, title="", text="", tool_input="", is_shell=False, tool_input_redacted=False):
    # Mirror AcpEvent.shell_command: recovered from JSON ``tool_input``, and
    # None when the input is a raw non-JSON string (json.loads raises) — the
    # exact shape that must NOT let the identity check vouch for nothing.
    _shell_command = None
    if is_shell and tool_input:
        try:
            _shell_command = json.loads(tool_input).get("command")
        except (ValueError, AttributeError):
            _shell_command = None
    return SimpleNamespace(
        kind=EVENT_PERMISSION_REQUEST,
        text=text,
        title=title,
        request_id=7,
        tool_input=tool_input,
        is_shell=is_shell,
        tool_input_redacted=tool_input_redacted,
        shell_command=_shell_command,
    )


def _done():
    return SimpleNamespace(kind=EVENT_COMPLETE)


def _approval_posts(ch):
    return [args for args, kw in ch.post.call_args_list if kw.get("msg_type") == "approval"]


@pytest.mark.asyncio
async def test_matching_shell_command_is_auto_approved(monkeypatch):
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    _stub_name_grant_verdict(monkeypatch, None)
    agent = _make_agent(bases={"ls"})
    ch = _make_channel(agent)
    event = _perm_event(
        title="Running: ls /tmp",
        tool_input=json.dumps({"command": "ls /tmp"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    client.approve_tool.assert_awaited_once_with(7)
    client.reject_tool.assert_not_awaited()
    # Auto-approved without posting an interactive approval card.
    assert not _approval_posts(ch)
    outcomes = [kw.get("outcome") for _, kw in sel_mock.log_tool_invocation.call_args_list]
    assert "auto_approved_trusted_pattern" in outcomes


def _stub_name_grant_verdict(monkeypatch, refusal):
    """Pin the off-loop name-grant verdict (the seam test_name_grant_surfaces
    stubs): the real check resolves names against the host PATH and declines
    everything on Windows, so flow tests must not depend on it."""

    async def stub(command):
        return refusal

    monkeypatch.setattr(name_grant, "refusal_for_command_off_loop", stub)


@pytest.mark.asyncio
async def test_name_grant_refusal_falls_through_to_interactive_card(monkeypatch):
    """A matched grant whose program name can no longer be vouched for (e.g.
    the file behind a trusted ./deploy.sh was replaced) must NOT auto-approve
    — and must not reject either: the request takes the interactive card."""
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    _stub_name_grant_verdict(
        monkeypatch, name_grant.Refusal("SHADOWED", "resolved outside system bin")
    )
    agent = _make_agent(bases={"ls"})
    ch = _make_channel(agent, decision="rejected")
    event = _perm_event(
        title="Running: ls /tmp",
        tool_input=json.dumps({"command": "ls /tmp"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    # Fell through to the card (posted), decided there — never auto-approved.
    assert _approval_posts(ch)
    client.approve_tool.assert_not_awaited()
    client.reject_tool.assert_awaited_once_with(7)
    outcomes = [kw.get("outcome") for _, kw in sel_mock.log_tool_invocation.call_args_list]
    assert "auto_approved_trusted_pattern" not in outcomes


@pytest.mark.asyncio
async def test_identity_check_reaches_raw_shell_input(monkeypatch):
    """RAW (non-JSON) shell tool_input: ``event.shell_command`` is None, but
    the grant matched the command ``extract_bash_command`` recovered — the
    identity check must evaluate THAT command, not vouch for nothing. A
    refused verdict on the raw-input shape must fall through to the card."""
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    checked: list[str] = []

    async def stub(command):
        checked.append(command)
        return name_grant.Refusal("SHADOWED", "resolved outside system bin")

    monkeypatch.setattr(name_grant, "refusal_for_command_off_loop", stub)
    agent = _make_agent(bases={"ls"})
    ch = _make_channel(agent, decision="rejected")
    event = _perm_event(title="Running: ls /tmp", tool_input="ls /tmp", is_shell=True)
    assert event.shell_command is None  # the shape under test
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    assert checked == ["ls /tmp"]
    client.approve_tool.assert_not_awaited()
    client.reject_tool.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_human_approval_pins_the_shell_command_identity(monkeypatch):
    """A human approving a shell command records the executable identities
    behind its names BEFORE execution is released — approving first would let
    a self-replacing script swap the file and get the replacement pinned."""
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    order: list[str] = []
    monkeypatch.setattr(name_grant, "pin_human_approval", lambda cmd: order.append(f"pin:{cmd}"))
    agent = _make_agent()
    ch = _make_channel(agent, decision="approved")
    event = _perm_event(
        title="Running: ./deploy.sh",
        tool_input=json.dumps({"command": "./deploy.sh"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])
    client.approve_tool = AsyncMock(side_effect=lambda rid: order.append("approve"))

    await _stream_task(agent, ch, client, "hi")

    assert order == ["pin:./deploy.sh", "approve"]


@pytest.mark.asyncio
async def test_rejected_approval_pins_nothing(monkeypatch):
    sel_mock = MagicMock()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: sel_mock)
    pins: list[str] = []
    monkeypatch.setattr(name_grant, "pin_human_approval", pins.append)
    agent = _make_agent()
    ch = _make_channel(agent, decision="rejected")
    event = _perm_event(
        title="Running: ./deploy.sh",
        tool_input=json.dumps({"command": "./deploy.sh"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    client.reject_tool.assert_awaited_once_with(7)
    assert pins == []


@pytest.mark.asyncio
async def test_non_shell_tool_with_nested_command_never_matches():
    """A non-shell MCP tool (e.g. cron_add) whose arguments carry a nested
    "command" key must NOT inherit a shell grant — the provider's is_shell
    classification, not the payload shape, gates matching."""
    agent = _make_agent(bases={"ls"})
    ch = _make_channel(agent)
    event = _perm_event(
        title="cron_add",
        tool_input=json.dumps({"name": "job", "command": "ls /tmp"}),
        is_shell=False,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    client.approve_tool.assert_not_awaited()
    assert _approval_posts(ch)


@pytest.mark.asyncio
async def test_non_matching_command_still_prompts():
    agent = _make_agent(bases={"ls"})
    ch = _make_channel(agent)
    event = _perm_event(
        title="Running: rm -rf /tmp/x",
        tool_input=json.dumps({"command": "rm -rf /tmp/x"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    assert _approval_posts(ch)
    client.approve_tool.assert_not_awaited()
    client.reject_tool.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_pattern_matches_tool_input_not_llm_title():
    """The LLM-authored title must not be able to spoof a trusted command."""
    agent = _make_agent(bases={"ls"})
    ch = _make_channel(agent)
    # Title claims a trusted "ls", but the real command is rm.
    event = _perm_event(
        title="Running: ls /tmp",
        tool_input=json.dumps({"command": "rm -rf /"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    client.approve_tool.assert_not_awaited()
    assert _approval_posts(ch)


@pytest.mark.asyncio
async def test_pending_command_is_stashed_for_shell_and_cleared_after():
    """The approve endpoint binds trust grants to the stashed canonical
    command; it must hold the tool_input-extracted command while the approval
    is pending and be cleared once the decision lands."""
    agent = _make_agent(set())
    stash: list[str] = []
    ch = _make_channel(agent, capture=stash)
    event = _perm_event(
        title="Running: ls /tmp",
        tool_input=json.dumps({"command": "ls /tmp"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    assert stash == ["ls /tmp"]
    assert agent._pending_approval_command == ""


@pytest.mark.asyncio
async def test_pending_command_is_empty_for_non_shell():
    """A non-shell pending tool stashes no command, so the approve endpoint
    fails closed (pattern_underivable) instead of recording a dead grant."""
    agent = _make_agent(set())
    stash: list[str] = []
    ch = _make_channel(agent, capture=stash)
    event = _perm_event(
        title="cron_add",
        tool_input=json.dumps({"name": "job", "command": "ls /tmp"}),
        is_shell=False,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    assert stash == [""]


@pytest.mark.asyncio
async def test_redacted_command_is_never_a_grant_target():
    """The provider redacts credentials in tool_input, so two commands
    differing only in their credentials redact to the SAME text — a grant
    scoped to the redacted form would cover commands the user never
    consented to. The stash must fail closed."""
    agent = _make_agent(set())
    stash: list[str] = []
    ch = _make_channel(agent, capture=stash)
    event = _perm_event(
        title="Running: curl -H auth",
        tool_input=json.dumps({"command": "curl -H [REDACTED: credential] https://x"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    assert stash == [""]
    posts = _approval_posts(ch)
    assert "**Shell command (allow once):" in posts[0][1]
    assert "**Running:" not in posts[0][1]


@pytest.mark.asyncio
async def test_transport_redaction_provenance_is_never_a_grant_target():
    """A transport may redact bytes without leaving a marker the channel can
    rediscover. Its provenance bit is authoritative and must keep the card
    allow-once-only even when the remaining command looks harmless."""
    agent = _make_agent(set())
    stash: list[str] = []
    ch = _make_channel(agent, capture=stash)
    event = _perm_event(
        title="Running: curl https://example.invalid",
        tool_input=json.dumps({"command": "curl https://example.invalid"}),
        is_shell=True,
        tool_input_redacted=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    assert stash == [""]
    posts = _approval_posts(ch)
    assert "**Shell command (allow once):" in posts[0][1]
    assert "**Running:" not in posts[0][1]


@pytest.mark.asyncio
async def test_failed_approval_post_releases_future_and_command_authority():
    """The post and wait share one ownership scope. A failed broadcast must
    not leave a live Future or a stale command for a later approval click."""
    agent = _make_agent(set())
    ch = _make_channel(agent)
    # First call is the approval post; the second is _stream_task's ordinary
    # error notice, which still has to be deliverable for this regression.
    ch.post = AsyncMock(side_effect=[RuntimeError("broadcast failed"), None])
    event = _perm_event(
        title="Running: ls /tmp",
        tool_input=json.dumps({"command": "ls /tmp"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    assert agent._approval_future is None
    assert agent._pending_approval_command == ""
    client.approve_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_message_name_falls_back_to_title():
    """ACP permission events populate only ``title`` (``text`` is empty); the
    posted approval card must still carry the tool name for display."""
    agent = _make_agent(set())
    ch = _make_channel(agent)
    event = _perm_event(title="Running: ls /tmp", tool_input="{}", is_shell=True)
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    posts = _approval_posts(ch)
    assert posts, "expected an approval message"
    assert "**Running: ls /tmp**" in posts[0][1]


@pytest.mark.asyncio
async def test_shell_card_name_is_the_canonical_command_not_the_prose_title():
    """kiro's shell ``title`` can be a model-authored prose description; the
    card name (which the trust tiers derive their consent-proof pattern from)
    must be the canonical command so a trust click matches the pending
    command instead of failing as superseded."""
    agent = _make_agent(set())
    ch = _make_channel(agent)
    event = _perm_event(
        title="List the project files",
        tool_input=json.dumps({"command": "ls -la /workplace/project"}),
        is_shell=True,
    )
    client = _make_client([event, _done()])

    await _stream_task(agent, ch, client, "hi")

    posts = _approval_posts(ch)
    assert posts, "expected an approval message"
    assert "**Running: ls -la /workplace/project**" in posts[0][1]
    assert "List the project files" not in posts[0][1]


def test_channel_agent_trust_grants_are_runtime_only():
    """Grants are session-scoped: not serialized, so a restarted gateway
    (agents relaunched with fresh sessions) starts with a clean slate."""
    agent = ChannelAgent(id="a1", role="dev", agent_name="dev", task="")
    agent._trusted_commands.add("ls /tmp")
    agent._trusted_bases.add("ls")
    agent._pending_approval_command = "ls /tmp"
    d = agent.to_dict()
    assert "_trusted_commands" not in d
    assert "_trusted_bases" not in d
    assert "trusted_commands" not in d
    assert "_pending_approval_command" not in d
