"""Structured metadata beside a channel approval message.

The approval card decides which trust tiers to offer. The server is the only
side that can refuse one (``handlers_channel.approve`` answers
``pattern_underivable``), so the message carries the server's verdict as flat
string facts next to the unchanged prose, and the card reads those instead of
re-deriving them from the text.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.channel import (
    _APPROVAL_FIELD_MAX_CHARS,
    _APPROVAL_SHELL_TITLE_PREFIX,
    Channel,
    ChannelMessage,
    _stream_task,
)
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST


def _make_agent():
    return SimpleNamespace(
        id="a1",
        role="dev",
        agent_name="dev",
        session_key="channel:test",
        _approval_future=None,
        _trusted_commands=set(),
        _trusted_bases=set(),
        _pending_approval_command="",
    )


def _make_channel(agent):
    """Channel stub whose ``post`` resolves the pending approval as rejected
    (the interactive path otherwise awaits it for 3600s)."""
    ch = SimpleNamespace(id="c1", trusted=False, members={})

    async def _post(*args, **kw):
        if kw.get("msg_type") == "approval":
            if agent._approval_future is not None and not agent._approval_future.done():
                agent._approval_future.set_result("rejected")

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


async def _approval_meta(event) -> dict[str, str]:
    agent = _make_agent()
    ch = _make_channel(agent)
    await _stream_task(agent, ch, _make_client([event, _done()]), "hi")
    posts = [kw for _args, kw in ch.post.call_args_list if kw.get("msg_type") == "approval"]
    assert len(posts) == 1, "expected exactly one approval message"
    meta = posts[0].get("meta")
    assert isinstance(meta, dict), "approval message must carry structured meta"
    assert all(isinstance(v, str) for v in meta.values()), "meta values are flat strings"
    return meta


@pytest.mark.asyncio
async def test_simple_shell_command_offers_both_per_command_tiers():
    meta = await _approval_meta(
        _perm_event(title="List files", tool_input=json.dumps({"command": "ls -la"}), is_shell=True)
    )
    assert meta["tool_title"] == "Running: ls -la"
    assert meta["tool_input"] == json.dumps({"command": "ls -la"})
    assert meta["command_grantable"] == "1"
    assert meta["base_derivable"] == "1"
    # The very binary the endpoint would grant -- not a first-token guess.
    assert meta["base_command"] == "ls"


@pytest.mark.asyncio
async def test_compound_command_has_no_derivable_base():
    """``cat f | wc -l``: the exact tier is available (one literal string), the
    base tier is not -- the endpoint refuses a base for a compound command, so
    the card must not offer "Trust all cat commands"."""
    meta = await _approval_meta(
        _perm_event(tool_input=json.dumps({"command": "cat f | wc -l"}), is_shell=True)
    )
    assert meta["command_grantable"] == "1"
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""


@pytest.mark.asyncio
async def test_non_shell_tool_offers_no_per_command_tier():
    meta = await _approval_meta(
        _perm_event(
            title="cron_add", tool_input=json.dumps({"command": "rm -rf /"}), is_shell=False
        )
    )
    assert meta["tool_title"] == "cron_add"
    assert meta["command_grantable"] == ""
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""


@pytest.mark.asyncio
async def test_redacted_command_is_display_only_in_meta():
    """A command the provider redacted is shown but never grantable: two
    commands differing only in a credential redact to the same text."""
    meta = await _approval_meta(
        _perm_event(
            tool_input=json.dumps({"command": "curl -H 'x' https://x"}),
            is_shell=True,
            tool_input_redacted=True,
        )
    )
    assert meta["command_grantable"] == ""
    assert meta["base_derivable"] == ""
    assert meta["base_command"] == ""
    assert meta["tool_title"].startswith("Shell command (exact text unverified): ")


async def _approval_post(cmd: str):
    agent = _make_agent()
    ch = _make_channel(agent)
    event = _perm_event(tool_input=json.dumps({"command": cmd}), is_shell=True)
    await _stream_task(agent, ch, _make_client([event, _done()]), "hi")
    return next((a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "approval")


async def _run(cmd: str):
    """Stream one shell permission request; return (channel stub, client stub, agent)."""
    agent = _make_agent()
    ch = _make_channel(agent)
    client = _make_client(
        [_perm_event(tool_input=json.dumps({"command": cmd}), is_shell=True), _done()]
    )
    await _stream_task(agent, ch, client, "hi")
    return ch, client, agent


@pytest.mark.asyncio
async def test_over_bound_command_is_refused_with_a_bounded_notice_not_a_cut_card():
    """A title past the retained-field bound can neither be cut (Approve beside a
    command the reader cannot read in full) nor retained whole (one command
    grows the persisted channel past the bound every other field obeys). So
    it is refused: no approval card, no future, the agent gets a rejection,
    and the reader gets a bounded system notice that says why."""
    long_cmd = "echo " + "x" * (_APPROVAL_FIELD_MAX_CHARS + 200)
    ch, client, agent = await _run(long_cmd)
    kinds = [k.get("msg_type") for _a, k in ch.post.call_args_list]
    assert "approval" not in kinds
    client.reject_tool.assert_awaited_once_with(7)
    client.approve_tool.assert_not_awaited()
    assert agent._approval_future is None and agent._pending_approval_command == ""
    ((args, kw),) = [(a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "system"]
    notice = args[1]
    assert "Approval refused" in notice and "Nothing was run" in notice
    assert str(len(f"{_APPROVAL_SHELL_TITLE_PREFIX}{long_cmd}")) in notice
    assert str(_APPROVAL_FIELD_MAX_CHARS) in notice
    # The notice is bounded too: it carries the same bounded excerpt the card
    # would have, never the whole command.
    assert "x" * (_APPROVAL_FIELD_MAX_CHARS + 1) not in notice
    assert kw.get("meta") is None


@pytest.mark.asyncio
async def test_title_bound_is_measured_on_the_title_the_reader_would_see():
    """The longest command whose ``Running: `` title fits is posted whole with
    every tier; one character more is refused. Measured on the title, not the
    command, so a posted title is always complete."""
    room = _APPROVAL_FIELD_MAX_CHARS - len(_APPROVAL_SHELL_TITLE_PREFIX)
    fits = "echo " + "y" * (room - len("echo "))
    assert len(fits) == room
    ch, client, _agent = await _run(fits)
    ((args, kw),) = [(a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "approval"]
    assert kw["meta"]["tool_title"] == f"{_APPROVAL_SHELL_TITLE_PREFIX}{fits}"
    assert kw["meta"]["command_grantable"] == "1" and kw["meta"]["base_command"] == "echo"
    assert all(len(v) <= _APPROVAL_FIELD_MAX_CHARS for v in kw["meta"].values())
    assert f"**{_APPROVAL_SHELL_TITLE_PREFIX}{fits}**" in args[1]
    client.reject_tool.assert_awaited_once_with(7)  # the stub channel rejects the card

    ch, client, _agent = await _run(fits + "y")
    assert not [k for _a, k in ch.post.call_args_list if k.get("msg_type") == "approval"]
    assert [k for _a, k in ch.post.call_args_list if k.get("msg_type") == "system"]
    client.reject_tool.assert_awaited_once_with(7)


@pytest.mark.asyncio
async def test_prose_content_is_unchanged_beside_meta():
    """Renderers that ignore ``meta`` (Slack mirrors, older dashboards) must
    see exactly the message they saw before the field existed."""
    agent = _make_agent()
    ch = _make_channel(agent)
    event = _perm_event(tool_input=json.dumps({"command": "ls -la"}), is_shell=True)
    await _stream_task(agent, ch, _make_client([event, _done()]), "hi")
    args, kw = next((a, k) for a, k in ch.post.call_args_list if k.get("msg_type") == "approval")
    assert args[1] == '⚠️ Approval needed: **Running: ls -la**\n```\n{"command": "ls -la"}\n```'
    assert kw["meta"]["tool_title"] == "Running: ls -la"


@pytest.mark.asyncio
async def test_post_threads_meta_and_defaults_to_none():
    ch = Channel(id="c1", topic="t")
    plain = await ch.post("human", "hello")
    assert plain.meta is None
    assert plain.to_dict()["meta"] is None
    facts = {"command_grantable": "1", "base_command": "ls"}
    tagged = await ch.post("a1", "card", msg_type="approval", meta=facts)
    assert tagged.meta == facts
    assert tagged.to_dict()["meta"] == facts


def test_serialize_round_trips_meta_and_tolerates_its_absence():
    ch = Channel(id="c1", topic="t")
    with_meta = ChannelMessage(
        id="m1",
        from_id="a1",
        from_role="dev",
        content="card",
        msg_type="approval",
        meta={"command_grantable": "1", "base_command": "ls"},
    )
    without = ChannelMessage(id="m2", from_id="human", from_role="human", content="hi")
    ch.messages.extend([with_meta, without])

    data = ch.serialize()
    # A message persisted before the field existed has no ``meta`` key at all.
    legacy = dict(without.to_dict())
    del legacy["meta"]
    data["messages"].append({**legacy, "id": "m3"})

    restored = Channel.deserialize(data)
    by_id = {m.id: m for m in restored.messages}
    assert by_id["m1"].meta == {"command_grantable": "1", "base_command": "ls"}
    assert by_id["m2"].meta is None
    assert by_id["m3"].meta is None
    assert by_id["m1"].to_dict() == with_meta.to_dict()
