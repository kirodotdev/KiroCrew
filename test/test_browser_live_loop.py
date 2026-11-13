"""The live card's loop line: ``browser_live``'s ``loop`` field and loop-tagged steps.

What is pinned:

* ``loop`` is ``null`` until a loop op reports, then carries every key of the
  contract (``moves``, ``rate``, ``steps``, ``max_steps``, ``seconds``,
  ``max_seconds``, ``stop``), each ``null`` when the op sent none or junk;
* steps reported while the loop runs are tagged ``loop: true``; the stop itself
  and steps after it are not loop steps of a running loop;
* the line belongs to the op that ran the loop: the next op clears it, and so
  does a new turn;
* an action the loop op ran inside itself is its own replay event, tagged
  ``loop``, and the loop op then records no summary event of its own.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from kiro_crew.browser_harness import live_hub as lh

pytestmark = pytest.mark.asyncio

KEY = "chat-1"
FULL = {
    "moves": 37,
    "rate": 6.24,
    "steps": 40,
    "max_steps": 120,
    "seconds": 5.9,
    "max_seconds": 45,
    "stop": None,
}


class Wire:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, _name: str, data: Any) -> int:
        self.events.append(dict(data))
        return 1

    async def send_frame(self, _blob: bytes) -> int:
        return 1


async def _settled(hub: lh.LiveHub) -> None:
    while hub._tasks:
        await asyncio.gather(*list(hub._tasks), return_exceptions=True)


class Recorder:
    """The hub's recorder seam, keeping the events it is handed."""

    enabled = True

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def turn_started(self, *_a: Any) -> None: ...

    def turn_ended(self, *_a: Any) -> None: ...

    def event(self, _key: str, event: dict[str, Any]) -> None:
        self.events.append(event)


@pytest.fixture
def hub() -> tuple[lh.LiveHub, Wire]:
    wire = Wire()
    h = lh.LiveHub(clock=lambda: 100.0)
    h.bind(wire.publish, wire.send_frame)
    h.turn_started(KEY, "turn-1")
    h.set_state(KEY, lh.STATE_LIVE, browser="kiro_chromium", headless=True)
    return h, wire


async def test_loop_is_null_until_a_loop_op_reports(hub) -> None:
    h, wire = hub
    h.step(KEY, "Open example.com")
    await _settled(h)
    assert wire.events[-1]["loop"] is None
    assert wire.events[-1]["steps"] == [{"label": "Open example.com", "status": "active"}]


async def test_the_loop_field_carries_the_counters(hub) -> None:
    h, wire = hub
    h.loop(KEY, FULL)
    await _settled(h)
    assert wire.events[-1]["loop"] == FULL


@pytest.mark.parametrize(
    "raw,field,want",
    [
        ({"moves": "37"}, "moves", None),
        ({"moves": True}, "moves", None),
        ({"moves": -1}, "moves", None),
        ({"rate": float("nan")}, "rate", None),
        ({"rate": 3.14159}, "rate", 3.14),
        ({"max_steps": 12.0}, "max_steps", 12),
        ({}, "max_seconds", None),
        ({"stop": "Budget!"}, "stop", None),
        ({"stop": "x" * 40}, "stop", None),
    ],
)
async def test_a_missing_or_malformed_counter_is_null(raw, field, want) -> None:
    info = lh.loop_info(raw)
    assert info is not None and set(info) == set(FULL) and info[field] == want


async def test_a_report_that_is_not_a_mapping_is_ignored() -> None:
    assert lh.loop_info(["moves", 3]) is None


@pytest.mark.parametrize(
    "stop", ["done", "until", "stuck", "risk", "budget", "user_in_control", "off_path"]
)
async def test_each_stop_reason_reaches_the_card(hub, stop) -> None:
    h, wire = hub
    h.loop(KEY, {**FULL, "stop": stop})
    await _settled(h)
    assert wire.events[-1]["loop"]["stop"] == stop


async def test_steps_while_the_loop_runs_are_loop_steps(hub) -> None:
    h, wire = hub
    h.step(KEY, "Open play2048.co")
    h.loop(KEY, {**FULL, "steps": 0, "moves": 0})
    h.step(KEY, "Reflex 1: Press ArrowLeft")
    h.step(KEY, "Reflex 2: Press ArrowUp (2.1/s)")
    h.loop(KEY, {**FULL, "stop": "budget"})
    h.step(KEY, "Take a screenshot")
    await _settled(h)
    flags = [s.get("loop", False) for s in wire.events[-1]["steps"]]
    assert flags == [False, True, True, False]


async def test_the_next_op_and_the_next_turn_clear_the_line(hub) -> None:
    h, wire = hub
    h.loop(KEY, {**FULL, "stop": "done"})
    h.op_mark(KEY)
    await _settled(h)
    assert wire.events[-1]["loop"] is None
    h.loop(KEY, FULL)
    h.turn_started(KEY, "turn-2")
    h.set_state(KEY, lh.STATE_LIVE, browser="kiro_chromium", headless=True)
    await _settled(h)
    assert wire.events[-1]["loop"] is None


async def test_inner_actions_are_loop_events_and_replace_the_op_s_own(hub) -> None:
    h, _wire = hub
    rec = Recorder()
    h.attach_recorder(rec)  # type: ignore[arg-type]
    outer = h.op_mark(KEY)
    for n in (1, 2):
        mark = h.inner_mark(KEY)
        h.step(KEY, f"Reflex {n}: Press ArrowLeft")
        h.record_op(KEY, "press_key", {"key": "ArrowLeft"}, {"ok": True}, mark, loop=True)
    h.record_op(KEY, "reflex", {"goal": "g"}, {"ok": True, "stop": "budget"}, outer)
    assert [(e["op"], e.get("loop"), e["label"]) for e in rec.events] == [
        ("key", True, "Reflex 1: Press ArrowLeft"),
        ("key", True, "Reflex 2: Press ArrowLeft"),
    ]
    # An ordinary op after it records itself as before.
    plain = h.op_mark(KEY)
    h.step(KEY, "Click e3")
    h.record_op(KEY, "click", {"ref": "e3"}, {"ok": True, "x": 3, "y": 4}, plain)
    assert rec.events[-1]["op"] == "click" and "loop" not in rec.events[-1]
