"""The subagent_snapshot replay frame must carry the stall idle span (#3929).

The live ``subagent_stalled`` event sends ``idle_secs`` on the not-stalled →
stalled transition, and the stalled row renders "possibly stalled at <tool> —
no activity for Ns" when it has that span, falling back to a plain
"no activity" wording when it does not. That fallback exists for a gateway too
old to send the field -- but the reconnect replay frame omitted ``idle_secs``
too, so in practice ANY reconnect during an active stall took it. The user saw
a strictly less informative badge purely because their socket blipped.

These assert the frame's contents directly, via the extracted
``build_subagent_snapshot``: the handler that wraps it needs a live aiohttp WS,
which is why the omission survived.
"""

from __future__ import annotations

from types import SimpleNamespace

# ``ws.py`` imports ``handlers.updates`` mid-module, and ``handlers/__init__``
# imports ``handlers.side``, which imports back from ``ws`` — so importing
# ``ws`` FIRST in a fresh interpreter dies on the partially-initialized module.
# Importing the handlers package first resolves the cycle in the direction
# every green consumer uses, and keeps this file collectable when a
# pytest-xdist worker imports it before any other dashboard module.
import kiro_crew.dashboard.handlers  # noqa: F401
from kiro_crew.dashboard.ws import build_subagent_snapshot


def _agent(**over):
    """A minimal stand-in for the SubagentInfo fields the frame reads."""
    base = dict(
        id="a1",
        parent_session_key="dashboard:main",
        task="do a thing",
        agent="kirocrew",
        resolved_model="model-1",
        streaming_text="",
        last_tool="Running: sleep 600",
        tool_count=3,
        stalled=False,
        started=1000.0,
        last_activity=1000.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_a_stalled_agent_replays_with_its_idle_span():
    a = _agent(stalled=True, last_activity=1000.0)
    data = build_subagent_snapshot(a, now=1117.0)
    assert data["stalled"] is True
    assert data["idle_secs"] == 117


def test_the_span_is_measured_at_replay_time_not_at_the_transition():
    """By reconnect the agent has usually been idle longer than it was when
    flagged, and last_activity is the field the reaper itself measures -- so a
    later replay must report a larger span, not the frozen transition value."""
    a = _agent(stalled=True, last_activity=1000.0)
    assert build_subagent_snapshot(a, now=1200.0)["idle_secs"] == 200
    assert build_subagent_snapshot(a, now=1800.0)["idle_secs"] == 800


def test_a_healthy_agent_carries_no_idle_span():
    """Omitted rather than zero: the reducer pairs the span with the flag, so a
    healthy row must not be able to acquire an idle figure at all."""
    data = build_subagent_snapshot(_agent(stalled=False), now=9999.0)
    assert data["stalled"] is False
    assert "idle_secs" not in data


def test_a_backwards_clock_cannot_produce_a_negative_span():
    a = _agent(stalled=True, last_activity=2000.0)
    assert build_subagent_snapshot(a, now=1000.0)["idle_secs"] == 0


def test_the_rest_of_the_frame_is_unchanged():
    """The added key must not disturb the fields clients already read."""
    data = build_subagent_snapshot(_agent(stalled=True), now=1000.0)
    assert data["id"] == "a1"
    assert data["task"] == "do a thing"
    assert data["agent"] == "kirocrew"
    assert data["model"] == "model-1"
    assert data["last_tool"] == "Running: sleep 600"
    assert data["tool_count"] == 3
    assert data["started"] == 1000.0
    assert data["slot"]


def test_the_frame_still_redacts_credentials():
    """The extraction must not drop the redaction the inline builder applied."""
    a = _agent(stalled=True, task="curl -H 'Authorization: Bearer sk-ant-api03-SECRETVALUE'")
    data = build_subagent_snapshot(a, now=1000.0)
    assert "sk-ant-api03-SECRETVALUE" not in data["task"]
