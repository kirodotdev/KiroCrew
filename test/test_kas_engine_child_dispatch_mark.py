"""A sub-agent's own request to start another sub-agent is marked as a native child's.

The engine announces that dispatch with a ``tool_call`` frame of ``kind:
agent-subtask``, which reads as the parent's, so the tool-call ids the session
records for its children miss it. The request itself names the sub-agent that
asked (``_meta.kiro.requester.subagent``, engine 0.66.119), and that marks it. The
request is the one a real engine sent (``kas_engine_child_helpers``).
"""

from __future__ import annotations

import copy

import pytest
from kas_engine_child_helpers import (
    _DIRECT_OWN,
    _HELPER,
    _OWN_STARTING_THE_HELPER,
    _RECORDED,
    _agents_dir,
    _batch,
    _default_spec,
    _granted,
    _handle,
    _mounted_with_own_agents,
    _own_request,
    _replay,
    _setting,
    _sid,
    _verdict,
    _write,
)

from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, JsonRpcMessage
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, TOOL_DENY

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

_LABEL = _OWN_STARTING_THE_HELPER


@pytest.mark.asyncio
async def test_a_sub_agents_own_dispatch_is_marked(agents_dir, setting, tmp_path):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    # The frame that announces it is the parent's kind, not a child's call.
    assert _RECORDED[_LABEL]["tool_call"]["_meta"]["kiro"]["kind"] == "agent-subtask"

    event = _replay(_handle(batch, tmp_path, sid=_sid(_LABEL)), _LABEL)

    assert event.title == "Sub-agent: kirocrew-helper"
    assert event.native_child_call


@pytest.mark.asyncio
async def test_the_session_agents_own_dispatch_is_not_marked(agents_dir, setting, tmp_path):
    """The same request with no sub-agent named on it: the session's own agent asked."""
    batch = await _mounted_with_own_agents(agents_dir, setting)
    record = copy.deepcopy(_RECORDED[_LABEL])
    del record["request"]["_meta"]["kiro"]["requester"]["subagent"]
    handle = _handle(batch, tmp_path, sid=_sid(_LABEL))
    handle._handle_update(
        JsonRpcMessage(
            method="session/update",
            params={"sessionId": _sid(_LABEL), "update": record["tool_call"]},
        )
    )

    event = handle._build_permission_event(
        JsonRpcMessage(id=9, method="session/request_permission", params=record["request"])
    )

    assert event is not None and not event.native_child_call and _granted(event)


@pytest.mark.asyncio
async def test_another_harness_does_not_read_the_name(agents_dir, setting, tmp_path):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    handle = _handle(batch, tmp_path, sid=_sid(_LABEL), backend=ACP_BACKEND_CLAUDE)
    assert not _replay(handle, _LABEL).native_child_call


def _named_only(label: str) -> dict:
    """*label*'s recorded call, its frame stripped of the child id the session records,
    so only the request itself names the sub-agent that asked."""
    record = copy.deepcopy(_RECORDED[label])
    record["tool_call"]["toolCallId"] = record["request"]["toolCall"]["toolCallId"] = "named-only"
    del record["tool_call"]["_meta"]["kiro"]["agentSubtaskId"]
    return record


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["off", "unset"])
@pytest.mark.parametrize("denied", [False, True], ids=["granted", "denied"])
async def test_with_the_preview_off_a_named_request_only_loses_grants(
    agents_dir, setting, tmp_path, value, denied
):
    """Preview off or never set: a request the engine names a sub-agent on keeps no grant
    and every refusal the session agent's own call of the same command gets."""
    record = _named_only(_DIRECT_OWN)
    command = record["request"]["toolCall"]["title"]
    shell = {"deniedCommands": [command]} if denied else {}
    _write(agents_dir, _default_spec(toolsSettings={"shell": shell}))
    setting(value)
    batch = await _batch()
    assert _HELPER not in {a["id"] for a in batch}

    own = _own_request(_handle(batch, tmp_path, sid=_sid(_DIRECT_OWN)), _DIRECT_OWN)
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_OWN))
    handle._handle_update(
        JsonRpcMessage(
            method="session/update",
            params={"sessionId": _sid(_DIRECT_OWN), "update": record["tool_call"]},
        )
    )
    named = handle._build_permission_event(
        JsonRpcMessage(id=9, method="session/request_permission", params=record["request"])
    )

    assert named is not None and named.native_child_call and not _granted(named)
    if denied:
        assert _verdict(own)[0] == TOOL_DENY
        assert _verdict(named)[0] == TOOL_DENY
    else:
        assert _granted(own) and _verdict(own)[0] == TOOL_AUTO_APPROVE
        # No gate answer: the call goes to the normal approval path, not auto-approved.
        assert _verdict(named)[0] == TOOL_ALLOW
