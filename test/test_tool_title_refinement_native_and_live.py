"""A refined tool title reaches the native thread turn and the dashboard live view.

claude-agent-acp names a call before its input has streamed ("Read") and sends the
real title in a ``tool_call_update``. On a backend whose capabilities say
``refines_tool_call_titles`` the native Slack turn renames the running call's card
in place (or, without a stream, its line in the answer), and the dashboard tab's
live view of a channel turn sends the runner's own refinement frame for the step.
Every other backend keeps today's output.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from slack_handler_helpers import _clean_approval_state  # noqa: F401
from slack_handler_helpers import FakeProvider, FakeSessionManager
from test_messaging_driver_tool_title_refinement import COMPLETE, _claude_read, _Provider

from conftest import MockSlackClient
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_TOOL_TITLE_REFINEMENT,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import channel_slots
from kiro_crew.dashboard.channel_live import ChannelLiveSink
from kiro_crew.messaging import TurnDriver
from kiro_crew.messaging.dashboard_live import DashboardLiveRenderer, TeeRenderer
from kiro_crew.messaging.renderer import SilentRenderer
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack.handler import handle_message

REFINED = "Read /proj/src/app.py"
BACKENDS = pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN), ids=lambda b: b or "kiro")


async def _native_turn(
    backend: str, *, stream: bool, events: list[Any] | None = None
) -> list[tuple[str, dict]]:
    slack = MockSlackClient()
    slack._stream_enabled = stream
    if events is None:
        events = _claude_read() + [LLMEvent(kind="text_chunk", text="It reads fine.")]
    provider = FakeProvider(events)
    provider.capabilities = capabilities_for(backend)
    await handle_message(slack, FakeSessionManager(provider), "C1", "read", None, "msg1", "U1")
    return slack.actions


@pytest.mark.asyncio
@BACKENDS
async def test_the_native_card_takes_the_refined_title_in_place(backend: str) -> None:
    actions = await _native_turn(backend, stream=True)
    cards = [a for kind, a in actions if kind == "append_task"]
    steps = [(card["title"].split("  ⏱")[0], card["status"]) for card in cards]
    assert {card["task_id"] for card in cards} == {"tool_1"}
    if backend in ACP_BACKENDS_TOOL_TITLE_REFINEMENT:
        # Renamed while the call runs (the repeated update redraws nothing), then closed.
        assert steps == [("Read", "in_progress"), (REFINED, "in_progress"), (REFINED, "complete")]
    else:
        assert steps == [("Read", "in_progress"), ("Read", "complete")]


@pytest.mark.asyncio
@BACKENDS
async def test_without_a_stream_the_calls_line_takes_the_refined_title(backend: str) -> None:
    actions = await _native_turn(backend, stream=False)
    texts = [a["text"] for kind, a in actions if kind == "update"]
    shown = REFINED if backend in ACP_BACKENDS_TOOL_TITLE_REFINEMENT else "Read"
    assert f"🫆 `{shown}`\n" in texts[-1]
    assert texts[-1].count("🫆") == 1
    # The message names the real title while the call runs, before the answer's text.
    assert any(f"🫆 `{shown}`" in text and "It reads fine." not in text for text in texts)


def _call(call_id: str, **kw: Any) -> LLMEvent:
    return LLMEvent(kind="tool_call", tool_call_id=call_id, title="Terminal", **kw)


def _update(call_id: str) -> LLMEvent:
    return LLMEvent(kind="tool_call_update", tool_call_id=call_id, title="ls -la")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stream, events, shown",
    [
        pytest.param(
            True, [_call("t1"), _call("t2"), _update("t1")], {"Terminal"}, id="card-closed"
        ),
        pytest.param(
            True,
            [_call("t1", tool_purpose="Check the build"), _update("t1")],
            {"Check the build"},
            id="purpose-titled",
        ),
        pytest.param(
            False, [_call("t1"), _call("t2"), _update("t1")], {"Terminal"}, id="line-closed"
        ),
        pytest.param(
            False,
            [_call("t1"), LLMEvent(kind="text_chunk", text="Done."), _update("t1")],
            {"Terminal"},
            id="line-followed",
        ),
    ],
)
async def test_a_step_that_no_longer_shows_the_stub_keeps_its_title(
    stream: bool, events: list[Any], shown: set[str]
) -> None:
    actions = await _native_turn(ACP_BACKEND_CLAUDE, stream=stream, events=events)
    if stream:
        titles = {a["title"].split("  ⏱")[0] for kind, a in actions if kind == "append_task"}
    else:
        final = [a["text"] for kind, a in actions if kind == "update"][-1]
        titles = {line.strip("🫆 `") for line in final.splitlines() if line.startswith("🫆")}
    assert titles == shown


@BACKENDS
def test_the_live_view_sends_the_runners_refinement_frame(
    backend: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames: list[tuple[str, dict]] = []
    state = SimpleNamespace(broadcast_ws=lambda kind, data: frames.append((kind, dict(data))))
    tab: Any = SimpleNamespace(key="tab-1", turn_running=False)
    monkeypatch.setattr(channel_slots, "linked_channel_slot", lambda _state, _key: tab)
    live = DashboardLiveRenderer(ChannelLiveSink(state, "slack:t1", None))
    driver = TurnDriver(
        _Provider(backend, _claude_read() + [COMPLETE]),
        TeeRenderer(SilentRenderer(channel_type="slack"), live),
        approval_mode="auto",
        event_tap=live.observe,
    )
    asyncio.run(driver.run("go"))
    calls = [data for kind, data in frames if kind == "tool_call"]
    assert [data["tool_call_id"] for data in calls] == ["t1"] * len(calls)
    if backend in ACP_BACKENDS_TOOL_TITLE_REFINEMENT:
        assert [(d["tool"], d.get("is_update", False)) for d in calls] == [
            ("Read", False),
            (REFINED, True),
        ]
        assert calls[1]["slot"] == "tab-1" and "purpose" not in calls[1]
        assert "/proj/src/app.py" in calls[1]["input_preview"]
    else:
        assert [d["tool"] for d in calls] == ["Read"]
