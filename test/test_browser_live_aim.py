"""The cursor an app draws over the live view (``LiveHub.aim``): the hub half of an
app's ``PagePort.aim``. It is a field on the live event, for the view to draw over
the frame, never sent into the page.
"""

from __future__ import annotations

import math

import pytest
from browser_live_helpers import FakeRelay
from test_browser_live_hub import FakeStream, Wire, _settled

from kiro_crew.browser_harness import live_hub as lh

pytestmark = pytest.mark.asyncio


@pytest.fixture
def hub() -> tuple[lh.LiveHub, Wire]:
    wire = Wire()
    h = lh.LiveHub(clock=lambda: 100.0)
    h.bind(wire.publish, wire.send_frame)
    return h, wire


async def _stream(hub: lh.LiveHub) -> None:
    hub.bind_target("chat-1", "a")
    await hub.start_stream(
        "chat-1",
        FakeRelay(),
        "a",
        browser="kiro_chromium",
        headless=True,
        stream_factory=FakeStream,
    )


async def test_aim_puts_the_cursor_on_the_live_event_only_while_a_stream_shows_it(hub) -> None:
    h, wire = hub
    assert h.aim("chat-1", 10, 20, "here") is False, "no stream shows the session"
    await _stream(h)
    h.step("chat-1", "Read the page")
    await _settled(h)
    assert "aim" not in wire.events[-1][1], "older readers see the fields they always had"
    assert h.aim("chat-1", 10.04, 20, "here") is True
    await _settled(h)
    assert wire.events[-1][1]["aim"] == {"x": 10.0, "y": 20.0, "label": "here", "seq": 1}
    assert h.aim("chat-1", 1, 2, "x" * 200) is True
    await _settled(h)
    last = wire.events[-1][1]["aim"]
    assert last["seq"] == 2 and len(last["label"]) == lh.MAX_AIM_LABEL


@pytest.mark.parametrize("point", [(math.nan, 1), (1, math.inf), (True, 1), ("1", 2)])
async def test_aim_refuses_a_point_that_is_not_two_finite_numbers(hub, point) -> None:
    h, wire = hub
    await _stream(h)
    await _settled(h)
    seen = len(wire.events)
    assert h.aim("chat-1", *point) is False
    await _settled(h)
    assert len(wire.events) == seen
