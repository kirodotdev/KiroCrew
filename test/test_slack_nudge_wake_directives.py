"""A Slack thread's plain auto-nudge wake applies the directives its turn emits.

The plain (prompt-loop) wake streams with ``stream_and_collect``, which has no
session of its own. ``autonudge_stop``, ``monitor_update`` and ``nothing_to_do``
only return a marker, so unless the fire path applies it the tool reports
"Stop REQUESTED" and the loop keeps firing. These tests drive the real fire
path and the real applier against a real ``AutoNudgeService``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from slack_gateway_coverage_helpers import _make_orchestrator

from kiro_crew import session_directive
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.messaging import dispatch as messaging_dispatch
from kiro_crew.slack import gateway as gw

_KEY = "slack:111.222"


def _orchestrator(provider: object) -> Any:
    orch = _make_orchestrator()
    sessions = MagicMock()
    sessions.is_busy = MagicMock(return_value=False)
    sessions.get_channel = MagicMock(return_value="C123")
    sessions.get_thread = MagicMock(return_value="111.222")
    sessions.get_or_create = AsyncMock(return_value=(provider, False, False))
    sessions.cancel_current = AsyncMock()
    sessions.release = MagicMock()
    orch.sessions = sessions
    orch.slack = MagicMock()
    orch.slack.post_message = AsyncMock()
    orch.ctx_builder = SimpleNamespace(hooks=object(), build_message=lambda *a, **k: ("MSG", None))
    orch.conv_log = None

    async def _approve(_event: object, _parent: str = "") -> bool:
        return True

    orch._interactive_approval = lambda *a, **k: _approve
    return orch


class _OneToolProvider:
    """A provider whose turn calls one core directive tool and then ends."""

    def __init__(self, tool: str, args: dict[str, Any], *, server: str = "") -> None:
        self.tool = tool
        self.args = args
        self.server = server or session_directive.CORE_MCP_SERVER

    async def stream(self, _message: str, **_kwargs: Any):
        yield AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-1",
            title=self.tool,
            tool_name=self.tool,
            mcp_server_name=self.server,
        )
        yield AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="tc-1",
            tool_output=session_directive.encode(self.tool, self.args, "Requested."),
            tool_final=True,
        )
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, _request_id: str, *, always: bool = False) -> None:
        return None

    async def reject_tool(self, _request_id: str) -> None:
        return None


async def _armed(tmp_path, monkeypatch) -> tuple[AutoNudgeService, Any]:
    service = AutoNudgeService(base_dir=tmp_path)
    monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: service)
    monkeypatch.setattr(gw, "_persist_turn_row", AsyncMock())
    loop = await service.add(slot_key=_KEY, message="watch the PR", idle_secs=3600)
    return service, loop


@pytest.mark.asyncio
async def test_autonudge_stop_in_a_slack_wake_stops_the_loop(tmp_path, monkeypatch):
    service, loop = await _armed(tmp_path, monkeypatch)
    orch = _orchestrator(_OneToolProvider("autonudge_stop", {"reason": "PR merged"}))
    orch.autonudge_svc = service

    assert await orch._fire_slack_nudge(loop) is True

    assert service.get_by_slot(_KEY) is None
    assert service.get_by_id(loop.id) is None


@pytest.mark.asyncio
async def test_a_forged_marker_under_another_server_does_not_stop_the_loop(tmp_path, monkeypatch):
    service, loop = await _armed(tmp_path, monkeypatch)
    orch = _orchestrator(
        _OneToolProvider("autonudge_stop", {"reason": "x"}, server="third-party-server")
    )
    orch.autonudge_svc = service

    assert await orch._fire_slack_nudge(loop) is True

    assert service.get_by_slot(_KEY) is not None


@pytest.mark.asyncio
async def test_nothing_to_do_in_a_slack_wake_is_a_quiet_end(tmp_path, monkeypatch):
    """The wake is the loop's own, so the applier honours the quiet end."""
    service, loop = await _armed(tmp_path, monkeypatch)
    outcomes: list[Any] = []
    real = messaging_dispatch.build_directive_consumer

    def _recording(**kwargs: Any):
        consume = real(**kwargs)

        async def _consume(kind: str, args: dict[str, Any]) -> bool:
            ends = await consume(kind, args)
            outcomes.append((kind, ends))
            return ends

        return _consume

    monkeypatch.setattr(gw, "build_directive_consumer", _recording)
    orch = _orchestrator(_OneToolProvider("nothing_to_do", {"note": "no change"}))
    orch.autonudge_svc = service

    assert await orch._fire_slack_nudge(loop) is True

    assert outcomes == [("nothing_to_do", True)]
    orch.slack.post_message.assert_not_called()
    assert service.get_by_slot(_KEY) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "server", "quiet"),
    [
        ("nothing_to_do", "", True),
        ("nothing_to_do", "third-party-server", False),
        ("autonudge_stop", "", False),
    ],
)
async def test_text_after_a_directive_is_quiet_only_for_an_applied_terminal(
    tmp_path, monkeypatch, tool, server, quiet
):
    class _TrailingTextProvider(_OneToolProvider):
        async def stream(self, message: str, **kwargs: Any):
            async for event in super().stream(message, **kwargs):
                if event.kind == EVENT_COMPLETE:
                    yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="Trailing reply.")
                yield event

    service, loop = await _armed(tmp_path, monkeypatch)
    orch = _orchestrator(_TrailingTextProvider(tool, {}, server=server))
    orch.autonudge_svc = service
    orch.conv_log = MagicMock()
    save_turn = AsyncMock()
    monkeypatch.setattr(gw, "save_conversation_turn_off_loop", save_turn)
    monkeypatch.setattr(gw, "is_thread_temporary", lambda _key: False)
    monkeypatch.setattr(gw, "is_thread_incognito", lambda _key: False)

    assert await orch._fire_slack_nudge(loop) is True

    if quiet:
        orch.slack.post_message.assert_not_called()
    else:
        orch.slack.post_message.assert_awaited_once_with("C123", "Trailing reply.", "111.222")
    save_turn.assert_awaited_once()
    assert save_turn.await_args.args[3] == ("" if quiet else "Trailing reply.")
    gw._persist_turn_row.assert_awaited_once()
    orch.sessions.release.assert_called_once_with(_KEY)


@pytest.mark.asyncio
async def test_an_inbound_slack_turn_still_refuses_nothing_to_do(monkeypatch):
    """Without a wake loop id the turn is person-opened: the quiet end is refused."""
    consume = messaging_dispatch.build_directive_consumer(session_key=_KEY, sessions=MagicMock())
    assert await consume("nothing_to_do", {"note": ""}) is False
