"""The live hub: turn and step state, the lock, the stream's lifecycle, owner-only delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from browser_live_helpers import SESSION, FakeClock, FakeRelay, frame_event, make_jpeg

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import screencast as sc
from kiro_crew.browser_harness.takeover import InputEventError
from kiro_crew.testing.wait import (
    FALLBACK_TIMEOUT_SECS,
    async_wait_until,
    await_until,
    until_parked,
)

pytestmark = pytest.mark.asyncio


class FakeStream:
    """Stands in for ``LiveStream``: records calls, pushes a frame on demand."""

    def __init__(self, relay: Any, target_id: str, sink: Any, **kwargs: Any) -> None:
        self.relay = relay
        self.target_id = target_id
        self.sink = sink
        self.kwargs = kwargs
        self.session_id = SESSION
        self.visible = True
        self.calls: list[tuple[str, Any]] = []
        self.rung = 0
        self.inputs = 0

    async def start(self) -> None:
        self.calls.append(("start", None))

    async def stop(self) -> None:
        self.calls.append(("stop", None))

    async def set_visible(self, visible: bool) -> None:
        self.calls.append(("visible", visible))

    async def restart(self) -> None:
        self.calls.append(("restart", None))

    async def set_takeover(self, active: bool) -> None:
        self.calls.append(("takeover", active))

    def ack(self, seq: int) -> bool:
        self.calls.append(("ack", seq))
        return True

    def note_input(self) -> None:
        self.inputs += 1

    async def push(self, blob: bytes) -> None:
        await self.sink.frame({"seq": 1}, blob)


class Wire:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.frames: list[bytes] = []

    async def publish(self, name: str, data: Any) -> int:
        self.events.append((name, dict(data)))
        return 1

    async def send_frame(self, blob: bytes) -> int:
        self.frames.append(bytes(blob))
        return 1


async def _settled(hub: lh.LiveHub) -> None:
    """Wait for the hub's scheduled publishes, which are the signal a sync call leaves."""
    while hub._tasks:
        await asyncio.gather(*list(hub._tasks), return_exceptions=True)


@pytest.fixture
def hub() -> tuple[lh.LiveHub, Wire]:
    wire = Wire()
    h = lh.LiveHub(clock=lambda: 100.0)
    h.bind(wire.publish, wire.send_frame)
    return h, wire


async def test_steps_build_the_strip_and_each_change_publishes_one_event(hub) -> None:
    h, wire = hub
    h.turn_started("chat-1", "turn-9")
    h.set_state("chat-1", lh.STATE_WAITING_ALLOW, browser="user_chrome", headless=False)
    h.step("chat-1", "Open example.com")
    h.step("chat-1", "Click Sign in")
    await _settled(h)
    assert [n for n, _ in wire.events] == [lh.LIVE_EVENT] * 3
    last = dict(wire.events[-1][1])
    # The turn's own wall-clock start, so a reloaded tab keeps it; not ended yet.
    assert last.pop("started_at_ms") > 0
    assert last.pop("ended_at_ms") == 0
    assert last == {
        "session_key": "chat-1",
        "turn_id": "turn-9",
        "state": "waiting_allow",
        "url": "",
        "title": "",
        "browser": "user_chrome",
        "headless": False,
        "step": {"index": 1, "label": "Click Sign in"},
        "steps": [
            {"label": "Open example.com", "status": "done"},
            {"label": "Click Sign in", "status": "active"},
        ],
        "rung": 0,
        "fps": 0.0,
        "turn_over": False,
        "tab": "",
        "loop": None,
        "handback_options": [],
        "ask": None,
    }


async def test_nothing_is_published_before_the_card_has_a_state(hub) -> None:
    h, wire = hub
    h.turn_started("chat-1", "t")
    h.step("chat-1", "first")
    await _settled(h)
    assert wire.events == []
    assert h.in_control("chat-1") is False and h.in_control("nope") is False


async def test_a_new_owner_tab_gets_each_chats_current_state_as_last_published(hub) -> None:
    """A reloaded tab sees the card (a parked quick ask included) without waiting for a
    change: the snapshot is the last published payload, and a chat with no state yet
    sends none."""
    h, wire = hub
    h.turn_started("chat-1", "t1")
    h.set_state("chat-1", lh.STATE_WAITING_ALLOW, browser="user_chrome", headless=False)
    h.turn_started("chat-2", "t2")
    await _settled(h)
    assert h.snapshot_events() == [wire.events[-1][1]]


async def test_the_owner_socket_is_sent_the_browser_snapshot_frames() -> None:
    import json

    from kiro_crew.dashboard import ws as ws_mod

    wire = Wire()
    h = lh.LiveHub(clock=lambda: 100.0)
    h.bind(wire.publish, wire.send_frame)
    h.turn_started("chat-1", "t1")
    h.set_state("chat-1", lh.STATE_WAITING_ALLOW, browser="user_chrome", headless=False)
    sent: list[str] = []

    class _Ws:
        async def send_str(self, text: str) -> None:
            sent.append(text)

    lh.reset_live_hub()
    lh._HUB = h
    try:
        await ws_mod._send_browser_live_snapshot(_Ws(), owner_reader=True)  # type: ignore[arg-type]
    finally:
        lh.reset_live_hub()
    frames = [json.loads(t) for t in sent]
    assert [f["type"] for f in frames] == [lh.LIVE_EVENT]
    assert frames[0]["data"]["session_key"] == "chat-1"
    assert frames[0]["data"]["state"] == "waiting_allow"


async def test_start_stream_goes_live_and_frames_reach_the_owner_sender(hub) -> None:
    h, wire = hub
    relay = FakeRelay()
    h.turn_started("chat-1", "t1")
    stream = await h.start_stream(
        "chat-1",
        relay,
        "target-1",
        browser="kiro_chromium",
        headless=True,
        url="https://example.com",
        title="Example",
        stream_factory=FakeStream,
    )
    assert isinstance(stream, FakeStream) and stream.calls == [("start", None)]
    assert stream.kwargs["session_key"] == "chat-1" and stream.kwargs["config"] == h.config
    await _settled(h)
    ev = wire.events[-1][1]
    assert ev["state"] == "live" and ev["browser"] == "kiro_chromium" and ev["headless"] is True
    assert ev["url"] == "https://example.com" and ev["title"] == "Example"
    await stream.push(b"\x00\x00\x00\x02{}jpeg")
    assert wire.frames == [b"\x00\x00\x00\x02{}jpeg"]


async def test_a_second_stream_replaces_the_first(hub) -> None:
    h, _wire = hub
    first = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    second = await h.start_stream(
        "chat-1", FakeRelay(), "b", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert first.calls == [("start", None), ("stop", None)]
    assert h.get("chat-1").stream is second


async def test_live_view_off_in_config_starts_no_stream(hub) -> None:
    h, wire = hub
    h.configure(SimpleNamespace(browser=SimpleNamespace(live=SimpleNamespace(enabled=False))))
    assert h.config == sc.LiveConfig(enabled=False)
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream is None and h.get("chat-1").stream is None
    await _settled(h)
    # No card is announced: an event would mount a live card that can never show a
    # frame. The browser op itself is untouched (the hub still tracks the session).
    assert wire.events == []
    h.step("chat-1", "Open x")
    await _settled(h)
    assert wire.events == []


async def test_claim_takes_control_caps_the_stream_and_release_hands_back(hub) -> None:
    h, wire = hub
    relay = FakeRelay()
    stream = await h.start_stream(
        "chat-1", relay, "a", browser="user_chrome", headless=False, stream_factory=FakeStream
    )
    assert await h.claim("chat-1") is True
    assert h.in_control("chat-1") is True
    assert ("takeover", True) in stream.calls
    assert wire.events[-1][1]["state"] == "in_control"
    # An agent op asks the hub and gets the refusal code's condition.
    assert h.in_control("chat-1") is True
    sent = await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1, "button": "left"})
    assert sent == 2 and stream.inputs == 1
    assert [s for _, _, s in relay.calls] == [SESSION, SESSION]
    await h.release("chat-1")
    assert h.in_control("chat-1") is False
    assert stream.calls[-1] == ("takeover", False)
    assert wire.events[-1][1]["state"] == "live"


async def test_input_is_scaled_by_the_frame_the_card_drew_not_the_newest(hub) -> None:
    """RM-1: over a slow link the screencast mixes picture widths (a 640 px rung, the
    1280 px settle still), and frames the card never drew still move the stream's
    scale. The card drew a 1280 px frame of a 1280 CSS px viewport and clicked at
    (404, 424); the stream's newest frame was 640 px (scale 0.5). Scaling by the
    stream put the click at (808, 848), off the field and below the page."""
    h, _wire = hub
    relay = FakeRelay()
    stream = await h.start_stream(
        "chat-1", relay, "a", browser="kiro_chromium", headless=True, stream_factory=FakeStream
    )
    stream.scale, stream.device_width = 0.5, 1280.0
    await h.claim("chat-1")
    click = {"type": "mouse", "action": "down", "x": 404, "y": 424, "button": "left"}
    await h.input_event("chat-1", {**click, "w": 1280})
    assert (relay.calls[-1][1]["x"], relay.calls[-1][1]["y"]) == (404, 424)
    # The same point on the 640 px frame the card may draw next is the same CSS point.
    await h.input_event("chat-1", {**click, "x": 202, "y": 212, "w": 640})
    assert (relay.calls[-1][1]["x"], relay.calls[-1][1]["y"]) == (404, 424)
    # A wheel is scaled the same way (it glides: its steps add up to the turn).
    await h.input_event("chat-1", {"type": "wheel", "x": 404, "y": 424, "dy": 100, "w": 1280})
    # A card that sends no width keeps the stream's own ratio (the old behaviour).
    await h.input_event("chat-1", click)
    steps = [c[1]["deltaY"] for c in relay.calls if c[1].get("type") == "mouseWheel"]
    assert sum(steps) == 100, steps
    assert relay.calls[-1][1]["x"] == 808
    # One glide fed from a 1280 px frame and then a 640 px frame: each turn and the
    # pointer spot keep their own frame's scale (200 CSS px, at (404, 424)).
    relay.calls.clear()
    await h.input_event("chat-1", {"type": "wheel", "x": 404, "y": 424, "dy": 100, "w": 1280})
    await h.input_event("chat-1", {"type": "wheel", "x": 202, "y": 212, "dy": 50, "w": 640})
    await h.input_event("chat-1", {**click, "w": 1280})
    wheels = [c[1] for c in relay.calls if c[1].get("type") == "mouseWheel"]
    assert sum(w["deltaY"] for w in wheels) == 200, wheels
    assert {(w["x"], w["y"]) for w in wheels} == {(404, 424)}, wheels
    # Before any frame metadata (device_width 0) the width cannot be used.
    stream.device_width = 0.0
    await h.input_event("chat-1", {**click, "w": 1280})
    assert relay.calls[-1][1]["x"] == 808


async def test_a_refused_move_is_logged_and_a_dropped_glide_sends_nothing_more() -> None:
    """A move the page refused costs one pointer position and never raises; a glide
    whose owner is gone clears its turns, and a dropped settle forgets the rest."""

    async def refused() -> int:
        raise RuntimeError("page refused the move")

    await lh._settle([asyncio.ensure_future(refused())])
    sess = SimpleNamespace(
        wheel_marks=[(0.0, 0, 100)],
        wheel_sent=[0, 40],
        wheel_now=False,
        wheel_task=None,
        lock=SimpleNamespace(owner="agent"),
        stream=None,
    )
    await lh._wheel_glide(sess, None, None)  # type: ignore[arg-type]
    assert (sess.wheel_marks, sess.wheel_sent) == ([], [0, 0])
    gate = asyncio.Event()
    sess.wheel_marks, sess.wheel_sent = [(0.0, 0, 100)], [0, 40]
    sess.wheel_task = asyncio.ensure_future(gate.wait())
    settling = asyncio.ensure_future(lh._wheel_settle(sess, drop=True))  # type: ignore[arg-type]
    await async_wait_until(lambda: sess.wheel_now)
    assert (sess.wheel_marks, sess.wheel_sent) == ([], [0, 0])
    gate.set()
    await settling


@pytest.fixture
def glide_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The wheel glide on a clock only its own waits move: a stalled loop never
    makes a turn look finished. Returns the clock's one-element cell."""
    from kiro_crew.browser_harness import live_hub_wheel

    now = [0.0]

    async def sleep(seconds: float) -> None:
        now[0] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(live_hub_wheel, "_clock", lambda: now[0])
    monkeypatch.setattr(live_hub_wheel, "_sleep", sleep)
    return now


async def test_a_wheel_turn_glides_and_a_press_lands_after_it(hub, glide_clock) -> None:
    """r19 (video review, Stack Overflow u4r8b f187-227): Chrome takes a CDP wheel as
    one jump, so the page moved once per wheel event and a 30 fps film repeated
    frames 4 times in 0.6 s. A turn goes out in steps, one a frame, easing out; a
    press sent meanwhile lands after the rest of the turn."""
    h, _wire = hub
    relay = FakeRelay()
    real_send = relay.send
    third = asyncio.Event()

    def wheels() -> list[Any]:
        return [c[1]["deltaY"] for c in relay.calls if c[1].get("type") == "mouseWheel"]

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        out = await real_send(method, params, session_id)
        if len(wheels()) >= 3:
            third.set()
        return out

    relay.send = send  # type: ignore[method-assign]
    await h.start_stream(
        "chat-1", relay, "a", browser="kiro_chromium", headless=True, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    await h.input_event("chat-1", {"type": "wheel", "x": 10, "y": 10, "dy": 300})
    await await_until(third, what="the turn's first steps")
    steps = wheels()
    assert steps[0] < 300 and all(x > 0 for x in steps), steps
    await h.input_event(
        "chat-1", {"type": "mouse", "action": "down", "x": 10, "y": 10, "button": "left"}
    )
    assert sum(wheels()) == 300
    assert relay.calls[-1][1]["type"] == "mousePressed"


async def test_a_turn_at_another_spot_lands_after_the_first_turn_finished(hub, glide_clock) -> None:
    """A second pane scrolled before the first glide ends: every step of the first
    turn goes to the first spot, and the second turn's steps to its own."""
    h, _wire = hub
    relay = FakeRelay()
    await h.start_stream(
        "chat-1", relay, "a", browser="kiro_chromium", headless=True, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    await h.input_event("chat-1", {"type": "wheel", "x": 10, "y": 10, "dy": 300})
    await h.input_event("chat-1", {"type": "wheel", "x": 500, "y": 400, "dy": 100})
    await h.input_event(
        "chat-1", {"type": "mouse", "action": "down", "x": 1, "y": 1, "button": "left"}
    )
    wheels = [c[1] for c in relay.calls if c[1].get("type") == "mouseWheel"]
    first = [w["deltaY"] for w in wheels if (w["x"], w["y"]) == (10, 10)]
    second = [w["deltaY"] for w in wheels if (w["x"], w["y"]) == (500, 400)]
    assert (sum(first), sum(second)) == (300, 100), wheels
    assert len(first) + len(second) == len(wheels), wheels
    assert all(w["x"] == 10 for w in wheels[: len(first)]), wheels


async def test_a_press_waiting_behind_a_move_is_released_by_the_hand_back_that_raced_it(
    hub,
) -> None:
    """A press that waits for a move in flight while a hand-back starts: the press
    goes into the held input that hand-back releases, never into the fresh one."""
    h, _wire = hub
    relay = FakeRelay()
    real_send = relay.send
    gate = asyncio.Event()

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        if isinstance(params, dict) and params.get("type") == "mouseMoved":
            await gate.wait()
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    await h.start_stream(
        "chat-1", relay, "a", browser="kiro_chromium", headless=True, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    sess = h._sessions["chat-1"]
    await h.input_event("chat-1", {"type": "mouse", "action": "move", "x": 5, "y": 5})
    press = asyncio.ensure_future(
        h.input_event(
            "chat-1", {"type": "mouse", "action": "down", "x": 5, "y": 5, "button": "left"}
        )
    )
    await until_parked(gate)
    for _ in range(5):  # the press reads its held input, then waits for the move
        await asyncio.sleep(0)
    assert not press.done()
    released = sess.held
    sess.held = type(released)()  # what a hand-back's _take_held does meanwhile
    gate.set()
    await press
    assert released and not sess.held


def test_a_gliding_wheel_eases_out_evenly_frame_by_frame() -> None:
    """r19 (video review, Stack Overflow u4r10, u4r17): the film showed 71, 0, 123 and
    185, 0, 176 px a frame; a first step that large made Chrome take two frames for it.
    Over even frames a glide starts small, rises for a few frames, then every step is
    smaller than the one before, none of them 0, and they add up to the turn."""
    marks = [(0.0, 0, 380)]
    pos = [lh._wheel_target(marks, k / 60)[1] for k in range(1, 60)]
    steps = [b - a for a, b in zip([0.0, *pos], pos)]
    steps = [s for s in steps if s >= 0.5]
    assert steps[0] < 30, steps
    peak = steps.index(max(steps))
    assert 1 <= peak <= 4, steps
    assert all(b < a for a, b in zip(steps[peak:], steps[peak + 1 :])), steps
    assert round(pos[-1]) == 380, pos[-1]
    # r19 (u4r20): at 30 frames a second, each frame moves less than twice the one
    # next to it, from the second frame of the turn until under 2 px are left.
    film = [lh._wheel_target(marks, k / 30)[1] for k in range(0, 40)]
    moves = [b - a for a, b in zip(film, film[1:])]
    body = [m for m in moves[1:] if m >= 2]
    assert all(max(a, b) / min(a, b) <= 2 for a, b in zip(body, body[1:])), body


def test_a_wheel_turn_that_comes_mid_glide_speeds_the_page_up_smoothly() -> None:
    """r19 (video review, u4r20): a turn that came late in another's glide was sent
    almost at once (205 px in one frame between steps of 30): each turn now glides on
    its own curve from when it came, and the two add up."""
    marks = [(0.0, 0, 100), (0.15, 0, 100)]
    film = [lh._wheel_target(marks, k / 60)[1] for k in range(0, 60)]
    moves = [b - a for a, b in zip(film, film[1:])]
    assert max(moves) < 25, moves
    assert round(film[-1]) == 200


async def test_wheel_steps_keep_going_while_the_page_is_slow_to_take_one(hub, glide_clock) -> None:
    """r19 (video review, u4r20 f185): each step waited for the page to take the one
    before; a page that took 40-70 ms got no step for a whole frame, so the film stood
    still and then jumped twice as far. Steps go out up to WHEELS_IN_FLIGHT ahead."""
    h, _wire = hub
    relay = FakeRelay()
    real_send = relay.send
    gate = asyncio.Event()
    started: list[float] = []

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        if isinstance(params, dict) and params.get("type") == "mouseWheel":
            started.append(asyncio.get_running_loop().time())
            await gate.wait()  # the page is busy and takes no step yet
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    await h.start_stream(
        "chat-1", relay, "a", browser="kiro_chromium", headless=True, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    await h.input_event("chat-1", {"type": "wheel", "x": 10, "y": 10, "dy": 300})
    await until_parked(gate, count=lh.WHEELS_IN_FLIGHT)
    # WHEELS_IN_FLIGHT steps are with the page before it took the first; no more.
    assert len(started) == lh.WHEELS_IN_FLIGHT, started
    # Three step gaps later the glide still waits for the page.
    loop = asyncio.get_running_loop()
    await async_wait_until(lambda: loop.time() >= started[-1] + 3 * lh.WHEEL_MIN_GAP_S)
    assert len(started) == lh.WHEELS_IN_FLIGHT, started
    gate.set()
    await h.input_event(
        "chat-1", {"type": "mouse", "action": "down", "x": 10, "y": 10, "button": "left"}
    )
    wheels = [c[1]["deltaY"] for c in relay.calls if c[1].get("type") == "mouseWheel"]
    assert sum(wheels) == 300 and relay.calls[-1][1]["type"] == "mousePressed", wheels


async def test_input_is_refused_without_a_claim_or_without_a_stream(hub) -> None:
    h, _wire = hub
    with pytest.raises(LookupError, match="not_in_control"):
        await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1})
    assert await h.claim("chat-1") is True  # the lock exists before the stream does
    with pytest.raises(LookupError, match="no_stream"):
        await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1})
    await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    with pytest.raises(InputEventError):
        await h.input_event("chat-1", {"type": "key"})


async def test_a_stream_started_under_a_claim_is_capped_at_once(hub) -> None:
    h, wire = hub
    await h.claim("chat-1")
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream.calls == [("start", None), ("takeover", True)]
    assert wire.events[-1][1]["state"] == "in_control"


async def test_turn_ended_stops_the_stream_opens_the_lock_and_folds_the_card(hub) -> None:
    h, wire = hub
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    h.step("chat-1", "one")
    h.turn_ended("chat-1")
    await _settled(h)
    assert ("stop", None) in stream.calls
    assert h.in_control("chat-1") is False and h.get("chat-1").stream is None
    ev = wire.events[-1][1]
    assert ev["state"] == "done" and ev["steps"] == [{"label": "one", "status": "done"}]
    assert ev["step"] == {"index": 0, "label": "one"}
    h.turn_ended("never-started")  # no record, no error


async def test_a_turn_that_ends_under_a_takeover_folds_only_at_the_hand_back(hub) -> None:
    """The agent, unable to wait for the hand-back, ends its turn while the person is
    drawing; the card must not fold under their hands and drop their strokes."""
    h, wire = hub
    relay = FakeRelay()
    stream = await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    h.step("chat-1", "one")
    await h.claim("chat-1")
    h.turn_ended("chat-1")
    await _settled(h)
    # Still the person's: stream running, lock held, state in_control, steps settled.
    assert ("stop", None) not in stream.calls
    assert h.in_control("chat-1") is True and h.get("chat-1").stream is stream
    ev = wire.events[-1][1]
    assert ev["state"] == "in_control" and ev["steps"] == [{"label": "one", "status": "done"}]
    sent = await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1, "button": "left"})
    assert sent == 2
    # The hand-back is what folds the card.
    await h.release("chat-1")
    await _settled(h)
    assert ("stop", None) in stream.calls and stream.calls[-2] == ("takeover", False)
    assert h.in_control("chat-1") is False and h.get("chat-1").stream is None
    assert wire.events[-1][1]["state"] == "done"
    # The next turn opens fresh: ``turn_started`` on a done record is a new turn.
    h.turn_started("chat-1", "t2")
    assert h.get("chat-1").state != "done" and h.get("chat-1").fold_on_release is False


async def test_visibility_and_acks_reach_the_stream(hub) -> None:
    h, _wire = hub
    await h.set_visible("chat-1", False)  # before any stream: remembered
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream.visible is False
    await h.set_visible("chat-1", True)
    assert ("visible", True) in stream.calls
    assert h.ack("chat-1", 4) is True and ("ack", 4) in stream.calls
    assert h.ack("other", 1) is False


async def test_open_in_browser_activates_and_brings_the_target_to_front(hub) -> None:
    h, _wire = hub
    relay = FakeRelay()
    await h.start_stream(
        "chat-1", relay, "tgt", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert await h.open_in_browser("chat-1") is True
    assert relay.calls == [
        ("Target.activateTarget", {"targetId": "tgt"}, None),
        ("Page.bringToFront", {}, SESSION),
    ]
    assert await h.open_in_browser("nope") is False
    relay.fail.add("Target.activateTarget")
    assert await h.open_in_browser("chat-1") is False


async def test_tick_stats_update_rung_and_fps_and_publish_on_change(hub) -> None:
    h, wire = hub
    sess = h._session("chat-1")
    sess.state = lh.STATE_LIVE
    on_tick = h._tick_for(sess)
    await on_tick(sc.TickStats(rung=0, fps=0.0, frames=0, late_share=0.0, kb_per_frame=0.0))
    assert wire.events == []  # unchanged
    await on_tick(sc.TickStats(rung=2, fps=29.0, frames=29, late_share=0.1, kb_per_frame=40.0))
    assert wire.events[-1][1]["rung"] == 2 and wire.events[-1][1]["fps"] == 29.0


async def test_set_state_refuses_unknown_states_and_in_control_without_a_lock(hub) -> None:
    h, wire = hub
    with pytest.raises(ValueError):
        h.set_state("chat-1", "sleeping")
    h.set_state("chat-1", lh.STATE_IN_CONTROL)
    await _settled(h)
    assert wire.events[-1][1]["state"] == "live"


async def test_a_supervisor_state_during_a_takeover_waits_for_the_hand_back(hub) -> None:
    h, wire = hub
    await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="user_chrome", headless=False, stream_factory=FakeStream
    )
    assert await h.claim("chat-1") is True
    h.set_state("chat-1", lh.STATE_WAITING_ALLOW)
    h.set_state("chat-1", lh.STATE_ERROR)
    await _settled(h)
    assert h.get("chat-1").state == "in_control"
    assert wire.events[-1][1]["state"] == "in_control"
    await h.release("chat-1")
    assert h.get("chat-1").state == "error"
    assert wire.events[-1][1]["state"] == "error"


async def test_the_singleton_is_one_hub_and_resettable() -> None:
    lh.reset_live_hub()
    a = lh.get_live_hub()
    assert lh.get_live_hub() is a
    lh.reset_live_hub()
    assert lh.get_live_hub() is not a
    lh.reset_live_hub()


async def test_close_stops_every_stream(hub) -> None:
    h, _wire = hub
    s1 = await h.start_stream(
        "a", FakeRelay(), "x", browser="edge", headless=False, stream_factory=FakeStream
    )
    s2 = await h.start_stream(
        "b", FakeRelay(), "y", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.close()
    assert ("stop", None) in s1.calls and ("stop", None) in s2.calls


async def test_open_in_browser_still_focuses_the_tab_after_the_turn_ended(hub) -> None:
    """The finished card's "Open in Chrome": the stream is gone, the tab is not.

    ``turn_ended`` drops the stream; the retained target is activated through the
    relay's flat connection, which needs no screencast session.
    """
    h, _wire = hub
    relay = FakeRelay()
    h.bind_target("chat-1", "tgt")
    await h.start_stream(
        "chat-1", relay, "tgt", browser="user_chrome", headless=False, stream_factory=FakeStream
    )
    h.turn_ended("chat-1")
    await _settled(h)
    relay.calls.clear()
    assert h.get("chat-1").stream is None and h.get("chat-1").state == lh.STATE_DONE
    assert await h.open_in_browser("chat-1") is True
    assert relay.calls == [("Target.activateTarget", {"targetId": "tgt"}, None)]
    # A relay that has since closed (gateway restart, Disconnect) is not asked.
    relay.closed = True  # type: ignore[attr-defined]
    assert await h.open_in_browser("chat-1") is False


async def test_a_stream_whose_start_raised_is_not_installed(hub) -> None:
    """Otherwise every later op on that tab sees "a stream with the right target"
    and only refreshes url/title: no frames for the rest of the turn."""
    h, _wire = hub

    class Failing(FakeStream):
        async def start(self) -> None:
            raise RuntimeError("Target closed")

    with pytest.raises(RuntimeError):
        await h.start_stream(
            "chat-1", FakeRelay(), "tgt", browser="edge", headless=False, stream_factory=Failing
        )
    assert h.get("chat-1").stream is None
    # The next attempt on the same tab starts a stream.
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "tgt", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream is not None and h.get("chat-1").stream is stream


async def test_claim_does_not_publish_in_control_after_an_esc_that_landed_mid_claim(hub) -> None:
    """``claim`` awaits the stream's takeover cap; a release during that await has
    already said ``live``, and the claim must not overwrite it with ``in_control``."""
    h, wire = hub
    gate = asyncio.Event()

    class Slow(FakeStream):
        async def set_takeover(self, active: bool) -> None:
            self.calls.append(("takeover", active))
            if active:
                await gate.wait()

    await h.start_stream(
        "chat-1", FakeRelay(), "tgt", browser="edge", headless=False, stream_factory=Slow
    )
    h.turn_started("chat-1", "t1")
    await _settled(h)
    wire.events.clear()
    claim = asyncio.ensure_future(h.claim("chat-1"))
    await asyncio.sleep(0)
    await h.release("chat-1")  # Esc while the claim is inside set_takeover
    gate.set()
    assert await claim is True
    await _settled(h)
    assert h.get("chat-1").lock.held() is False
    states = [e["state"] for _, e in wire.events]
    assert states and states[-1] == lh.STATE_LIVE
    assert lh.STATE_IN_CONTROL not in states[-1:]


async def test_claim_waits_for_the_op_fence_and_cancels_the_op_inside_it(hub) -> None:
    """The route holds ``op_fence`` for bind + op; a claim cancels the op in flight,
    waits for the fence, and only then confirms control (``op_interrupted`` tells
    the route to answer ``user_in_control`` instead of propagating the cancel)."""
    h, _wire = hub
    h.turn_started("chat-1", "t1")
    fence = h.op_fence("chat-1")
    cancelled = asyncio.Event()

    async def op() -> None:
        try:
            await asyncio.Event().wait()  # never set: only the cancel ends it
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def route() -> bool:
        async with fence:
            task = asyncio.ensure_future(op())
            h.op_started("chat-1", task)
            try:
                await task
            except asyncio.CancelledError:
                return h.op_finished("chat-1")
            return False

    route_task = asyncio.ensure_future(route())
    await asyncio.sleep(0)
    assert fence.locked()
    claim = asyncio.ensure_future(h.claim("chat-1"))
    await asyncio.sleep(0)
    assert h.in_control("chat-1") is True, "the lock is taken first, so no new op starts"
    assert await route_task is True, "the route learns the cancel was the claim's"
    assert cancelled.is_set()
    assert await claim is True
    assert not fence.locked()
    assert h.get("chat-1").state == lh.STATE_IN_CONTROL


async def test_wait_for_release_returns_on_the_hand_back_and_on_the_bound(hub) -> None:
    """The hold's awaitable, on an injected clock: a release ends it with ``True``;
    the bound (the hub's sleep) ends it with ``False`` while the lock is held."""
    h, _wire = hub
    h.turn_started("chat-1", "t1")
    assert await h.wait_for_release("chat-1", 600) is True, "free at once when not held"
    await h.claim("chat-1")
    waiter = asyncio.ensure_future(h.wait_for_release("chat-1", 600))
    for _ in range(5):
        await asyncio.sleep(0)
    assert not waiter.done()
    await h.release("chat-1")
    assert await waiter is True
    # The bound: a hub whose sleep returns at once.
    slept: list[float] = []

    async def instant(seconds: float) -> None:
        slept.append(seconds)

    quick = lh.LiveHub(clock=lambda: 0.0, sleep=instant)
    quick.turn_started("chat-2", "t2")
    await quick.claim("chat-2")
    assert await quick.wait_for_release("chat-2", 600) is False
    assert slept == [600]
    assert quick.in_control("chat-2") is True
    # ``turn_ended`` under a takeover leaves the browser with the person (the card
    # stays theirs past the turn); their hand-back is the release, and folds it.
    quick.turn_ended("chat-2")
    assert await quick.wait_for_release("chat-2", 600) is False
    await quick.release("chat-2")
    assert await quick.wait_for_release("chat-2", 600) is True
    assert quick.get("chat-2").state == lh.STATE_DONE


async def test_claim_on_a_finished_card_is_refused(hub) -> None:
    """No "in control" on a done card: there is no stream and no agent to fence."""
    h, wire = hub
    h.turn_started("chat-1", "t1")
    h.turn_ended("chat-1")
    await _settled(h)
    wire.events.clear()
    with pytest.raises(LookupError, match="turn_over"):
        await h.claim("chat-1")
    assert h.in_control("chat-1") is False
    assert wire.events == []
    h.set_state("chat-1", lh.STATE_ERROR)
    with pytest.raises(LookupError, match="turn_over"):
        await h.claim("chat-1")


async def test_an_ack_naming_another_stream_releases_nothing(hub) -> None:
    """The header promises ``(stream, seq)``: a late ack for the stream a tab switch
    just replaced must not free the new stream's frame of the same number."""
    h, _wire = hub
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "tgt", browser="edge", headless=False, stream_factory=FakeStream
    )
    assert stream is not None
    stream.stream_id = "new1"  # type: ignore[attr-defined]
    assert h.ack("chat-1", 7, "old0") is False
    assert stream.calls.count(("ack", 7)) == 0
    assert h.ack("chat-1", 7, "new1") is True
    assert h.ack("chat-1", 8) is True, "no stream named: the current one"


# ── reopen: the finished card's "Open live" ──────────────────────────────


class LedgerRelay(FakeRelay):
    """A relay with the tab ledger ``CdpRelay`` keeps (``agent_targets``)."""

    def __init__(self, targets: tuple[str, ...] = ("tgt",)) -> None:
        super().__init__()
        self.targets = list(targets)
        self.closed = False

    def agent_targets(self) -> list[str]:
        return list(self.targets)


async def _finished(h: lh.LiveHub, relay: Any, *, browser: str = "kiro_chromium") -> FakeStream:
    """A turn that browsed tab ``tgt`` on *relay* and ended: the card is done."""
    h.turn_started("chat-1", "t1")
    h.bind_target("chat-1", "tgt")
    stream = await h.start_stream(
        "chat-1", relay, "tgt", browser=browser, headless=True, stream_factory=FakeStream
    )
    h.step("chat-1", "open")
    h.turn_ended("chat-1")
    await _settled(h)
    assert h.get("chat-1").state == lh.STATE_DONE and ("stop", None) in stream.calls
    return stream


@pytest.mark.parametrize("browser", ["kiro_chromium", "user_chrome", "edge"])
async def test_reopen_streams_the_tab_again_under_the_same_turn_and_takes_over(
    hub, browser: str
) -> None:
    h, wire = hub
    relay = LedgerRelay()
    await _finished(h, relay, browser=browser)
    done = wire.events[-1][1]
    assert done["state"] == "done" and done["turn_over"] is True and done["tab"] == "open"

    assert await h.reopen("chat-1", relay, stream_factory=FakeStream) == lh.REOPEN_LIVE
    await _settled(h)
    sess = h.get("chat-1")
    stream = sess.stream
    assert isinstance(stream, FakeStream) and stream.target_id == "tgt" and stream.relay is relay
    ev = wire.events[-1][1]
    # The same card grows back: same turn, live, its steps kept, still a finished turn.
    assert (ev["turn_id"], ev["state"], ev["turn_over"], ev["tab"]) == ("t1", "live", True, "open")
    assert ev["browser"] == browser and ev["steps"] == [{"label": "open", "status": "done"}]
    # Again is a no-op while it streams.
    assert await h.reopen("chat-1", relay, stream_factory=FakeStream) == lh.REOPEN_LIVE
    assert sess.stream is stream

    # Take over and hand back exactly as during a turn.
    assert await h.claim("chat-1") is True
    assert ("takeover", True) in stream.calls and h.in_control("chat-1")
    assert await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1, "button": "left"}) == 2
    await h.release("chat-1")
    await _settled(h)
    assert h.in_control("chat-1") is False and sess.stream is stream
    assert wire.events[-1][1]["state"] == "live", "a hand-back keeps the reopened view"

    # A later turn that never browses ends without folding the person's view.
    h.turn_ended("chat-1")
    await _settled(h)
    assert sess.stream is stream and sess.state == lh.STATE_LIVE

    # Close folds it, back to the done line, and Open live is offered again.
    assert await h.close_view("chat-1") is True
    await _settled(h)
    assert sess.stream is None and ("stop", None) in stream.calls
    assert wire.events[-1][1]["state"] == "done" and wire.events[-1][1]["tab"] == "open"
    assert await h.close_view("chat-1") is False


async def test_reopen_of_a_closed_tab_says_so_and_starts_nothing(hub) -> None:
    h, wire = hub
    relay = LedgerRelay()
    await _finished(h, relay)
    relay.targets.clear()  # the tab was closed (Target.targetDestroyed)
    made: list[FakeStream] = []

    def factory(*a: Any, **k: Any) -> FakeStream:
        made.append(FakeStream(*a, **k))
        return made[-1]

    assert await h.reopen("chat-1", relay, stream_factory=factory) == lh.REOPEN_TAB_GONE
    await _settled(h)
    assert made == [] and h.get("chat-1").stream is None
    ev = wire.events[-1][1]
    assert ev["state"] == "done" and ev["tab"] == "closed"
    # A relay that hung up, or none at all, is a gone tab too.
    relay.targets.append("tgt")
    relay.closed = True
    assert await h.reopen("chat-1", relay, stream_factory=factory) == lh.REOPEN_TAB_GONE
    assert await h.reopen("never", None) == lh.REOPEN_TAB_GONE


async def test_reopen_whose_attach_fails_is_a_gone_tab_not_a_crash(hub) -> None:
    h, wire = hub
    relay = LedgerRelay()
    await _finished(h, relay)

    class Dead(FakeStream):
        async def start(self) -> None:
            raise RuntimeError("No target with given id found")

    assert await h.reopen("chat-1", relay, stream_factory=Dead) == lh.REOPEN_TAB_GONE
    await _settled(h)
    assert h.get("chat-1").stream is None and wire.events[-1][1]["tab"] == "closed"


async def test_reopen_refuses_a_running_turn_another_chats_tab_and_live_off(hub) -> None:
    h, _wire = hub
    relay = LedgerRelay()
    h.turn_started("chat-1", "t1")
    h.bind_target("chat-1", "tgt")
    await h.start_stream(
        "chat-1", relay, "tgt", browser="kiro_chromium", headless=True, stream_factory=FakeStream
    )
    await h.stop_stream("chat-1")
    with pytest.raises(LookupError):
        await h.reopen("chat-1", relay, stream_factory=FakeStream)  # the agent's turn
    h.turn_ended("chat-1")
    h.bind_target("chat-2", "tgt")  # another chat owns that tab now
    assert await h.reopen("chat-1", relay, stream_factory=FakeStream) == lh.REOPEN_TAB_GONE
    h.get("chat-2").targets.clear()
    h.get("chat-1").tab_closed = False
    h._config = sc.LiveConfig(enabled=False)
    assert await h.reopen("chat-1", relay, stream_factory=FakeStream) == lh.REOPEN_LIVE_OFF


async def test_a_turn_that_browses_after_a_reopen_joins_the_running_stream(hub) -> None:
    h, wire = hub
    relay = LedgerRelay()
    await _finished(h, relay)
    await h.reopen("chat-1", relay, stream_factory=FakeStream)
    stream = h.get("chat-1").stream
    h.turn_started("chat-1", "t2")
    await _settled(h)
    sess = h.get("chat-1")
    assert sess.turn_over is False and sess.stream is stream
    ev = wire.events[-1][1]
    assert (ev["turn_id"], ev["state"], ev["turn_over"]) == ("t2", "live", False)
    h.turn_ended("chat-1")  # this turn's end is what folds it now
    await _settled(h)
    assert sess.stream is None and wire.events[-1][1]["state"] == "done"


class StillPageRelay(LedgerRelay):
    """Chrome showing a still page: each screencast start answers with one frame of
    it, and nothing more comes until the page changes (measured on Chromium: a
    start answers in about 10 ms, then 0 frames in 3 s of a still page)."""

    def __init__(self) -> None:
        super().__init__()
        self.pushes: list[asyncio.Future[Any]] = []

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        answer = await super().send(method, params, session_id)
        if method == "Page.startScreencast" and self.on_event is not None:
            self.pushes.append(asyncio.ensure_future(self.on_event(frame_event(make_jpeg(64, 40)))))
        return answer

    async def drained(self) -> None:
        while not all(p.done() for p in self.pushes):
            await asyncio.gather(*self.pushes, return_exceptions=True)


class OrderedWire(Wire):
    """Events and frames in the one order they went out."""

    def __init__(self) -> None:
        super().__init__()
        self.order: list[str] = []

    async def publish(self, name: str, data: Any) -> int:
        self.order.append(f"event:{data.get('turn_id')}")
        return await super().publish(name, data)

    async def send_frame(self, blob: bytes) -> int:
        self.order.append("frame")
        return await super().send_frame(blob)


def _real_stream(*a: Any, **k: Any) -> sc.LiveStream:
    clock = FakeClock()
    return sc.LiveStream(*a, clock=clock, sleep=clock.sleep, auto_control=False, **k)


async def test_a_turn_that_joins_a_running_stream_gets_a_picture_of_a_still_page_at_once() -> None:
    wire = OrderedWire()
    h = lh.LiveHub(clock=lambda: 100.0)
    h.bind(wire.publish, wire.send_frame)
    relay = StillPageRelay()

    async def settle() -> None:
        await _settled(h)
        await relay.drained()
        await _settled(h)

    h.turn_started("chat-1", "t1")
    h.bind_target("chat-1", "tgt")
    await h.start_stream(
        "chat-1", relay, "tgt", browser="user_chrome", headless=False, stream_factory=_real_stream
    )
    h.turn_ended("chat-1")
    await settle()
    # The view folded and came back (a stayLive app view, or Open live): one picture.
    assert await h.reopen("chat-1", relay, stream_factory=_real_stream) == lh.REOPEN_LIVE
    await settle()
    stream = h.get("chat-1").stream
    assert stream is not None and stream.casting
    wire.order.clear()

    # The next turn joins that stream on a page that is not changing: the card opens
    # the turn with no picture, and the frame that answers the restart follows its
    # event, so it lands on the new turn.
    h.turn_started("chat-1", "t2")
    await settle()
    assert h.get("chat-1").stream is stream
    assert wire.order[0] == "event:t2" and wire.order.count("frame") == 1, wire.order

    # No card sees the view: nothing is asked of Chrome, and the cast stays stopped.
    await h.set_visible("chat-1", True, "tab-a")
    await h.set_visible("chat-1", False, "tab-a")
    await settle()
    starts = len(relay.methods("Page.startScreencast"))
    h.turn_ended("chat-1")
    await settle()
    assert await h.reopen("chat-1", relay, stream_factory=_real_stream) == lh.REOPEN_LIVE
    wire.order.clear()
    h.turn_started("chat-1", "t3")
    await settle()
    assert "frame" not in wire.order and h.get("chat-1").stream.casting is False
    assert len(relay.methods("Page.startScreencast")) == starts


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _held_past_its_turn(clock: _Clock) -> tuple[lh.LiveHub, Wire, FakeStream]:
    wire = Wire()
    h = lh.LiveHub(clock=clock)
    h.bind(wire.publish, wire.send_frame)
    h.turn_started("chat-1", "t1")
    stream = await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    h.turn_ended("chat-1")
    await _settled(h)
    return h, wire, stream


async def test_a_takeover_held_past_its_turn_is_handed_back_once_the_card_goes_quiet() -> None:
    """The claim is plain HTTP: a card closed mid-takeover sends no release, and a
    takeover outlives its turn, so a quiet card must not hold the chat's browser
    for good."""
    clock = _Clock()
    h, wire, stream = await _held_past_its_turn(clock)
    clock.t += lh.TAKEOVER_IDLE_S - 1
    assert h.in_control("chat-1") is True
    clock.t += 2
    assert h.in_control("chat-1") is False
    await _settled(h)
    assert h.get("chat-1").state == "done" and h.get("chat-1").stream is None
    assert ("stop", None) in stream.calls
    assert wire.events[-1][1]["state"] == "done"


async def test_input_acks_and_scrolling_into_view_keep_the_takeover() -> None:
    clock = _Clock()
    h, _wire, _stream = await _held_past_its_turn(clock)
    for touch in (
        lambda: h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1, "button": "left"}),
        lambda: h.set_visible("chat-1", True),
    ):
        clock.t += lh.TAKEOVER_IDLE_S - 1
        await touch()
        assert h.in_control("chat-1") is True
    clock.t += lh.TAKEOVER_IDLE_S - 1
    h.ack("chat-1", 1)
    clock.t += lh.TAKEOVER_IDLE_S - 1
    assert h.in_control("chat-1") is True


async def test_a_claim_inside_its_own_running_turn_is_never_expired_by_idleness() -> None:
    clock = _Clock()
    h = lh.LiveHub(clock=clock)
    h.turn_started("chat-1", "t1")
    await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    clock.t += lh.TAKEOVER_IDLE_S * 10
    assert h.in_control("chat-1") is True


async def test_a_quiet_claim_from_an_earlier_turn_does_not_hold_the_next_turns_ops() -> None:
    clock = _Clock()
    h, _wire, _stream = await _held_past_its_turn(clock)
    clock.t += lh.TAKEOVER_IDLE_S + 1
    h.turn_started("chat-1", "t2")
    assert h.in_control("chat-1") is False
    assert h.get("chat-1").state != "done"


async def test_the_handback_poll_frees_a_quiet_claim_from_an_earlier_turn() -> None:
    clock = _Clock()

    async def instant(_s: float) -> None:
        clock.t += lh.TAKEOVER_IDLE_S

    wire = Wire()
    h = lh.LiveHub(clock=clock, sleep=instant)
    h.bind(wire.publish, wire.send_frame)
    h.turn_started("chat-1", "t1")
    await h.start_stream(
        "chat-1", FakeRelay(), "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    h.turn_ended("chat-1")
    assert await h.wait_for_release("chat-1", 3.0) is True


# ── hand-back releases what the person held down ─────────────────────────


async def test_hand_back_releases_the_keys_and_buttons_still_down(hub) -> None:
    """The card queues its key-up/mouse-up behind an input in flight, and the
    hand-back request can reach the hub first: the page must not keep them down."""
    h, _wire = hub
    relay = FakeRelay()
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    await h.input_event("chat-1", {"type": "key", "action": "down", "key": "a", "code": "KeyA"})
    await h.input_event(
        "chat-1", {"type": "key", "action": "down", "key": "Shift", "code": "ShiftLeft"}
    )
    await h.input_event("chat-1", {"type": "key", "action": "up", "key": "a", "code": "KeyA"})
    down = {"type": "mouse", "action": "down", "x": 10, "y": 20, "button": "left"}
    await h.input_event("chat-1", down)
    await h.input_event("chat-1", {**down, "action": "move", "x": 30, "y": 40})
    before = len(relay.calls)
    await h.release("chat-1")
    released = relay.calls[before:]
    assert [(m, p["type"]) for m, p, _s in released] == [
        ("Input.dispatchKeyEvent", "keyUp"),
        ("Input.dispatchMouseEvent", "mouseReleased"),
    ]
    assert released[0][1]["code"] == "ShiftLeft" and released[0][2] == SESSION
    # The release lands where the pointer is, not where the press was.
    assert (released[1][1]["x"], released[1][1]["y"], released[1][1]["button"]) == (30, 40, "left")
    # The card's own late release meets an open lock: refused, and nothing is sent.
    with pytest.raises(LookupError, match="not_in_control"):
        await h.input_event("chat-1", {"type": "key", "action": "up", "key": "Shift"})
    assert len(relay.calls) == before + 2


async def test_hand_back_with_nothing_held_sends_no_input(hub) -> None:
    h, _wire = hub
    relay = FakeRelay()
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    # A click and a key press each release themselves.
    await h.input_event("chat-1", {"type": "mouse", "x": 1, "y": 1, "button": "left"})
    await h.input_event("chat-1", {"type": "key", "key": "a", "code": "KeyA"})
    await h.input_event(
        "chat-1", {"type": "mouse", "action": "down", "x": 1, "y": 1, "button": "right"}
    )
    await h.input_event(
        "chat-1", {"type": "mouse", "action": "up", "x": 1, "y": 1, "button": "right"}
    )
    before = len(relay.calls)
    await h.release("chat-1")
    assert relay.calls[before:] == []


async def test_a_server_side_hand_back_releases_held_input_before_the_stream_stops() -> None:
    """An idle takeover past its turn folds without the card (no release request):
    the held key and button are still released, before the stream goes away."""
    clock = _Clock()
    h, _wire, stream = await _held_past_its_turn(clock)
    relay = stream.relay
    order: list[str] = []
    real_send, real_stop = relay.send, stream.stop

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        order.append(f"{method}:{(params or {}).get('type')}")
        return await real_send(method, params, session_id)

    async def stop() -> None:
        order.append("stop")
        await real_stop()

    relay.send, stream.stop = send, stop
    await h.input_event(
        "chat-1", {"type": "key", "action": "down", "key": "Enter", "code": "Enter"}
    )
    await h.input_event(
        "chat-1", {"type": "mouse", "action": "down", "x": 5, "y": 6, "button": "middle"}
    )
    order.clear()
    clock.t += lh.TAKEOVER_IDLE_S + 1
    # The lock stays shut while the releases go out; the fold follows them.
    assert h.in_control("chat-1") is True
    assert await h.wait_for_release("chat-1", 5.0) is True
    await _settled(h)
    assert h.get("chat-1").state == "done"
    assert order == [
        "Input.dispatchKeyEvent:keyUp",
        "Input.dispatchMouseEvent:mouseReleased",
        "stop",
    ]


async def test_closing_a_view_held_past_its_turn_releases_held_input(hub) -> None:
    h, _wire = hub
    relay = FakeRelay()
    h.turn_started("chat-1", "t1")
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    await h.input_event("chat-1", {"type": "key", "action": "down", "key": "x", "code": "KeyX"})
    h.turn_ended("chat-1")
    before = len(relay.calls)
    assert await h.close_view("chat-1") is True
    await _settled(h)
    assert [(m, p["type"]) for m, p, _s in relay.calls[before:]] == [
        ("Input.dispatchKeyEvent", "keyUp")
    ]


class _GatedRelay(FakeRelay):
    """A relay whose releases (keyUp, mouseReleased) wait for ``gate`` to open.

    ``entered`` is set once a release reaches the relay, so a test knows the hub is
    inside its cleanup with the lock still shut.
    """

    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.entered = asyncio.Event()

    async def send(
        self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None
    ) -> dict[str, Any]:
        if (params or {}).get("type") in ("keyUp", "mouseReleased"):
            self.entered.set()
            await self.gate.wait()
        return await super().send(method, params, session_id)


def _types(calls: list[tuple[str, dict[str, Any], str | None]]) -> list[str]:
    return [p.get("type", m) for m, p, _s in calls]


async def test_an_idle_expiry_keeps_the_agent_op_behind_the_synthetic_releases() -> None:
    """The expiry is found by the agent's own ``in_control`` check: its op must wait
    until the page has let go of what the person held, not race those releases."""
    clock = _Clock()
    wire = Wire()
    h = lh.LiveHub(clock=clock)
    h.bind(wire.publish, wire.send_frame)
    relay = _GatedRelay()
    h.turn_started("chat-1", "t1")
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    await h.input_event("chat-1", {"type": "key", "action": "down", "key": "a", "code": "KeyA"})
    h.turn_ended("chat-1")
    clock.t += lh.TAKEOVER_IDLE_S + 1
    assert h.in_control("chat-1") is True
    agent = asyncio.ensure_future(h.wait_for_release("chat-1", 30.0))
    await await_until(relay.entered, what="the relay to enter the op")
    # Inside the cleanup: the lock is shut, the agent waits, and card input is refused.
    assert h.in_control("chat-1") is True and not agent.done()
    with pytest.raises(LookupError, match="not_in_control"):
        await h.input_event("chat-1", {"type": "key", "action": "down", "key": "b"})
    relay.gate.set()
    assert await agent is True
    assert _types(relay.calls) == ["rawKeyDown", "keyUp"]
    await _settled(h)
    assert h.get("chat-1").state == "done" and h.get("chat-1").stream is None


async def test_a_new_takeover_keeps_its_own_held_input_from_the_last_cleanup(hub) -> None:
    """A retake waits for the last hand-back's releases; what it presses is its own
    and goes out only with its own hand-back."""
    h, _wire = hub
    relay = _GatedRelay()
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    await h.input_event("chat-1", {"type": "key", "action": "down", "key": "a", "code": "KeyA"})
    first = h.get("chat-1").held
    hand_back = asyncio.ensure_future(h.release("chat-1"))
    await await_until(relay.entered, what="the relay to enter the op")
    # The release owns the old held input; the session already holds a fresh one.
    assert h.get("chat-1").held is not first and not h.get("chat-1").held
    retake = asyncio.ensure_future(h.claim("chat-1"))
    relay.gate.set()
    assert await hand_back == "" and await retake is True
    await h.input_event("chat-1", {"type": "key", "action": "down", "key": "b", "code": "KeyB"})
    assert len(h.get("chat-1").held) == 1
    await h.release("chat-1")
    ups = [p for _m, p, _s in relay.calls if p.get("type") == "keyUp"]
    assert [p["code"] for p in ups] == ["KeyA", "KeyB"]


async def test_a_hung_page_is_bounded_by_the_release_deadline_and_the_lock_still_opens(
    hub, monkeypatch
) -> None:
    h, _wire = hub
    monkeypatch.setattr(lh, "HELD_RELEASE_DEADLINE_S", 0.05)
    relay = _GatedRelay()  # the gate never opens: every release hangs
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    for code in ("KeyA", "KeyB", "KeyC"):
        await h.input_event("chat-1", {"type": "key", "action": "down", "key": "x", "code": code})
    await h.input_event(
        "chat-1", {"type": "mouse", "action": "down", "x": 1, "y": 1, "button": "left"}
    )
    assert await h.release("chat-1") == ""
    sess = h.get("chat-1")
    assert h.in_control("chat-1") is False and sess.released.is_set()
    assert sess.releasing is False and sess.state == lh.STATE_LIVE
    # Only the first release was tried before the one deadline ran out.
    assert _types(relay.calls).count("keyUp") == 0 and relay.entered.is_set()


async def test_the_stream_knows_the_person_drives_before_their_press_lands(hub) -> None:
    """r19 (craigslist u6m5 in the pod): a slider press took 40 ms to land, the stream
    was told of the input only after it, and the frame sent meanwhile waited 160 ms for
    the recorder's mask query: the thumb stood 9 frames at the drag's start."""
    h, _wire = hub
    relay = FakeRelay()
    real_send = relay.send
    gate = asyncio.Event()
    sending = asyncio.Event()

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        if (params or {}).get("type") == "mousePressed":
            sending.set()
            await gate.wait()
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    stream = h._sessions["chat-1"].stream
    down = asyncio.ensure_future(
        h.input_event(
            "chat-1",
            {"type": "mouse", "action": "down", "x": 10, "y": 10, "button": "left", "clicks": 1},
        )
    )
    await await_until(sending, what="the press to reach the page")
    assert stream.inputs == 1, "told only after the press landed"
    gate.set()
    await await_until(down, what="the press to finish")


async def test_a_slow_input_in_flight_lands_and_is_released_before_the_lock_opens(
    hub, monkeypatch
) -> None:
    """The release deadline bounds the releases, not the wait for the event in flight:
    a key-down still being sent at hand-back must not land after the lock opens."""
    h, _wire = hub
    monkeypatch.setattr(lh, "HELD_RELEASE_DEADLINE_S", 0.05)
    relay = FakeRelay()
    real_send = relay.send
    gate = asyncio.Event()
    sending = asyncio.Event()

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        if (params or {}).get("type") == "rawKeyDown":
            sending.set()
            await gate.wait()
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    down = asyncio.ensure_future(
        h.input_event("chat-1", {"type": "key", "action": "down", "key": "a", "code": "KeyA"})
    )
    await await_until(sending, what="the key-down to reach the page")
    release = asyncio.ensure_future(h.release("chat-1"))
    # flake-ok: proving the lock stays shut PAST the release deadline needs that much time
    await asyncio.sleep(lh.HELD_RELEASE_DEADLINE_S * 4)
    assert not release.done() and h.in_control("chat-1"), "opened with a press in flight"
    gate.set()
    await await_until(down, what="the key-down to finish")
    assert await await_until(release, what="the release to finish") == ""
    assert h.in_control("chat-1") is False
    assert _types(relay.calls)[-2:] == ["rawKeyDown", "keyUp"]


async def test_a_press_acknowledged_before_its_release_failed_is_released_at_hand_back(
    hub,
) -> None:
    """A click is two CDP commands: when the press lands and the release fails, the
    page holds the button, so the hand-back must let it go."""
    h, _wire = hub
    relay = FakeRelay()
    real_send = relay.send
    failing = {"mouseReleased", "keyUp"}

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        if (params or {}).get("type") in failing:
            raise RuntimeError("page gone quiet")
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    with pytest.raises(RuntimeError):
        await h.input_event("chat-1", {"type": "mouse", "x": 7, "y": 8, "button": "left"})
    with pytest.raises(RuntimeError):
        await h.input_event("chat-1", {"type": "key", "key": "Enter", "code": "Enter"})
    assert len(h.get("chat-1").held) == 2
    failing.clear()
    before = len(relay.calls)
    await h.release("chat-1")
    released = relay.calls[before:]
    assert _types(released) == ["keyUp", "mouseReleased"]
    assert (released[1][1]["x"], released[1][1]["button"]) == (7, "left")


async def test_closing_a_view_while_an_expiry_releases_waits_for_that_release() -> None:
    clock = _Clock()
    wire = Wire()
    h = lh.LiveHub(clock=clock)
    h.bind(wire.publish, wire.send_frame)
    relay = _GatedRelay()
    h.turn_started("chat-1", "t1")
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    await h.input_event("chat-1", {"type": "key", "action": "down", "key": "a", "code": "KeyA"})
    h.turn_ended("chat-1")
    clock.t += lh.TAKEOVER_IDLE_S + 1
    h.in_control("chat-1")
    await await_until(relay.entered, what="the relay to enter the op")
    closing = asyncio.ensure_future(h.close_view("chat-1"))
    handing = asyncio.ensure_future(h.release("chat-1"))
    relay.gate.set()
    # The expiry folded the card; neither caller sends a second release.
    assert await closing is False and await handing == ""
    assert _types(relay.calls) == ["rawKeyDown", "keyUp"]


async def test_one_tab_reporting_not_visible_does_not_stop_another_tabs_view(hub) -> None:
    """Two dashboard tabs: the stream pauses only when no tab shows the card."""
    h, _wire = hub
    relay = FakeRelay()
    stream = await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )

    def last() -> bool:
        return [v for name, v in stream.calls if name == "visible"][-1]

    await h.set_visible("chat-1", True, "tab-a")
    await h.set_visible("chat-1", False, "tab-b")
    assert h.get("chat-1").visible is True and last() is True
    await h.set_visible("chat-1", True, "tab-b")
    await h.set_visible("chat-1", False, "tab-a")
    assert last() is True
    await h.set_visible("chat-1", False, "tab-b")
    assert h.get("chat-1").visible is False and last() is False


async def test_the_visible_tab_set_is_capped(hub, monkeypatch) -> None:
    h, _wire = hub
    monkeypatch.setattr(lh, "VISIBLE_TABS_CAP", 2)
    await h.set_visible("chat-1", True, "a")
    await h.set_visible("chat-1", True, "b")
    await h.set_visible("chat-1", True, "c")
    assert h.get("chat-1").visible_tabs == {"a", "b"}


async def test_a_drag_s_moves_go_out_before_the_page_took_the_last_one_but_a_press_waits(
    hub,
) -> None:
    """r19 (video review, craigslist drag in the pod): each move waited for the page to
    take the one before (12-59 ms while the slider's handler ran), so the newest
    position reached it every 28-58 ms. Moves go out ahead of the page's ack, two at
    most; a release of the button waits for them, so it never overtakes a move."""
    h, _wire = hub
    relay = FakeRelay()
    real_send = relay.send
    gate = asyncio.Event()
    sent: list[str] = []

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        kind = (params or {}).get("type", "")
        sent.append(kind)
        if kind == "mouseMoved":
            await gate.wait()  # the page is busy: no move is taken yet
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    await h.start_stream(
        "chat-1", relay, "a", browser="edge", headless=False, stream_factory=FakeStream
    )
    await h.claim("chat-1")
    move = {"type": "mouse", "action": "move", "x": 10, "y": 20, "button": "left"}
    # Two moves return at once, both already sent, though the page took neither.
    for x in (10, 20):
        await asyncio.wait_for(h.input_event("chat-1", {**move, "x": x}), FALLBACK_TIMEOUT_SECS)
    assert sent.count("mouseMoved") == 2, sent
    # A third waits for the first to be taken.
    third = asyncio.ensure_future(h.input_event("chat-1", {**move, "x": 30}))
    up = asyncio.ensure_future(
        h.input_event(
            "chat-1", {"type": "mouse", "action": "up", "x": 30, "y": 20, "button": "left"}
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert not third.done() and "mouseReleased" not in sent, sent
    gate.set()
    await asyncio.wait_for(asyncio.gather(third, up), FALLBACK_TIMEOUT_SECS)
    assert sent.count("mouseMoved") == 3 and sent[-1] == "mouseReleased", sent
