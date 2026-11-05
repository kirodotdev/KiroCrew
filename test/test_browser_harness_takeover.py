"""Takeover: the input lock and the card-event-to-CDP mapping."""

from __future__ import annotations

from typing import Any

import pytest
from browser_live_helpers import SESSION, FakeRelay

from kiro_crew.browser_harness import takeover as tk


def test_lock_refuses_the_agent_while_the_person_holds_it() -> None:
    lock = tk.InputLock()
    assert lock.held() is False
    assert lock.claim(tk.USER) is True
    assert lock.held() is True and lock.owner == tk.USER
    assert lock.claim(tk.AGENT) is False
    assert lock.claim(tk.USER) is True  # re-claim by the holder is idempotent
    lock.release()
    assert lock.held() is False and lock.claim(tk.AGENT) is True


def test_lock_refuses_an_empty_owner() -> None:
    assert tk.InputLock().claim("") is False


@pytest.mark.parametrize(
    "raw",
    [
        "not an object",
        {"type": "touch", "x": 1, "y": 1},
        {"type": "mouse", "x": -1, "y": 0},
        {"type": "mouse", "x": tk.MAX_COORD + 1, "y": 0},
        {"type": "mouse", "x": True, "y": 0},
        {"type": "mouse", "x": 1, "y": 1, "button": "pinky"},
        {"type": "mouse", "x": 1, "y": 1, "action": "click"},  # a press needs a button
        {"type": "mouse", "x": 1, "y": 1, "button": "left", "clicks": 9},
        {"type": "mouse", "x": 1, "y": 1, "button": "left", "action": "jiggle"},
        {"type": "wheel", "x": 1, "y": 1, "dx": tk.MAX_DELTA + 1},
        {"type": "key"},
        {"type": "key", "key": "a" * (tk.MAX_KEY_CHARS + 1)},
        {"type": "key", "text": "a" * (tk.MAX_TEXT_CHARS + 1)},
        {"type": "key", "key": "a", "action": "hold"},
        {"type": "key", "key": "a", "modifiers": 16},
    ],
)
def test_malformed_events_are_refused_before_anything_is_sent(raw: object) -> None:
    with pytest.raises(tk.InputEventError) as info:
        tk.parse_input_event(raw)
    assert info.value.code == "invalid_input_event"


def test_a_click_is_a_press_and_a_release() -> None:
    ev = tk.parse_input_event({"type": "mouse", "x": 10, "y": 20, "button": "left"})
    assert tk.cdp_calls(ev) == [
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "button": "left",
                "clickCount": 1,
                "x": 10,
                "y": 20,
                "modifiers": 0,
            },
        ),
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "button": "left",
                "clickCount": 1,
                "x": 10,
                "y": 20,
                "modifiers": 0,
            },
        ),
    ]


def test_a_double_click_counts_up_and_a_bare_move_is_a_move() -> None:
    ev = tk.parse_input_event({"type": "mouse", "x": 1, "y": 2, "button": "left", "clicks": 2})
    counts = [p["clickCount"] for _, p in tk.cdp_calls(ev)]
    assert counts == [1, 1, 2, 2]
    move = tk.parse_input_event({"type": "mouse", "x": 3, "y": 4})
    assert tk.cdp_calls(move) == [
        ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 3, "y": 4, "modifiers": 0})
    ]


def test_down_and_up_map_separately_for_drags() -> None:
    down = tk.parse_input_event(
        {"type": "mouse", "x": 1, "y": 1, "button": "left", "action": "down"}
    )
    up = tk.parse_input_event({"type": "mouse", "x": 9, "y": 9, "button": "left", "action": "up"})
    assert [p["type"] for _, p in tk.cdp_calls(down)] == ["mousePressed"]
    assert [p["type"] for _, p in tk.cdp_calls(up)] == ["mouseReleased"]


def test_a_wheel_event_carries_its_deltas() -> None:
    ev = tk.parse_input_event(
        {"type": "wheel", "x": 5, "y": 6, "dx": -3, "dy": 120, "modifiers": 2}
    )
    assert tk.cdp_calls(ev) == [
        (
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": 5, "y": 6, "deltaX": -3, "deltaY": 120, "modifiers": 2},
        )
    ]


def test_a_key_press_is_key_down_with_text_then_key_up() -> None:
    ev = tk.parse_input_event({"type": "key", "key": "a", "code": "KeyA", "text": "a"})
    assert tk.cdp_calls(ev) == [
        (
            "Input.dispatchKeyEvent",
            {
                "modifiers": 0,
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "nativeVirtualKeyCode": 65,
                "type": "keyDown",
                "text": "a",
                "unmodifiedText": "a",
            },
        ),
        (
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "modifiers": 0,
                "key": "a",
                "code": "KeyA",
                "windowsVirtualKeyCode": 65,
                "nativeVirtualKeyCode": 65,
            },
        ),
    ]
    raw = tk.parse_input_event({"type": "key", "key": "Escape", "action": "down"})
    assert tk.cdp_calls(raw) == [
        (
            "Input.dispatchKeyEvent",
            {
                "modifiers": 0,
                "key": "Escape",
                "windowsVirtualKeyCode": 27,
                "nativeVirtualKeyCode": 27,
                "type": "rawKeyDown",
            },
        )
    ]


@pytest.mark.parametrize(
    ("key", "code", "vk"),
    [
        ("1", "Digit1", 49),
        ("!", "Digit1", 49),
        ("2", "Numpad2", 98),
        ("ArrowDown", "Numpad2", 40),
        ("Backspace", "Backspace", 8),
        ("Delete", "Delete", 46),
        ("Enter", "NumpadEnter", 13),
        ("ArrowLeft", "ArrowLeft", 37),
        ("-", "Minus", 189),
        ("F5", "F5", 116),
        ("Backspace", "", 8),
        ("7", "", 55),
        ("é", "", 0),
    ],
)
def test_every_key_carries_its_virtual_key_code(key: str, code: str, vk: int) -> None:
    # Without it keyCode reads 0: Backspace deletes nothing and a page that
    # filters by keyCode (a digits-only field) drops every digit.
    assert tk.virtual_key_code(key, code) == vk


def test_a_digit_and_a_numpad_digit_type_with_their_codes() -> None:
    top = tk.cdp_calls(
        tk.parse_input_event(
            {"type": "key", "action": "down", "key": "1", "code": "Digit1", "text": "1"}
        )
    )
    pad = tk.cdp_calls(
        tk.parse_input_event(
            {"type": "key", "action": "down", "key": "1", "code": "Numpad1", "text": "1"}
        )
    )
    assert top[0][1]["windowsVirtualKeyCode"] == 49 and top[0][1]["text"] == "1"
    assert pad[0][1]["windowsVirtualKeyCode"] == 97 and pad[0][1]["location"] == 3


def test_enter_types_a_line_break_unless_a_chord() -> None:
    down = tk.cdp_calls(
        tk.parse_input_event({"type": "key", "action": "down", "key": "Enter", "code": "Enter"})
    )
    assert down[0][1]["type"] == "keyDown" and down[0][1]["text"] == "\r"
    chord = tk.cdp_calls(
        tk.parse_input_event(
            {"type": "key", "action": "down", "key": "Enter", "code": "Enter", "modifiers": 4}
        )
    )
    assert chord[0][1]["type"] == "rawKeyDown" and "text" not in chord[0][1]


def test_keyless_text_is_inserted_as_typed() -> None:
    # What a phone keyboard or an IME commits: no key behind it.
    ev = tk.parse_input_event({"type": "key", "action": "press", "key": "", "text": "hello"})
    assert tk.cdp_calls(ev) == [("Input.insertText", {"text": "hello"})]
    up = tk.parse_input_event({"type": "key", "action": "up", "key": "", "text": "x"})
    assert tk.cdp_calls(up) == []


@pytest.mark.asyncio
async def test_focused_field_reports_kind_never_value_and_bounds_the_reply() -> None:
    relay = FakeRelay()
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": {"editable": True, "kind": "number", "mode": "numeric"}}
    }
    assert await tk.focused_field(relay, SESSION) == {
        "editable": True,
        "kind": "number",
        "mode": "numeric",
    }
    method, params, sid = relay.calls[0]
    assert method == "Runtime.evaluate" and sid == SESSION and params["returnByValue"] is True
    assert ".value" not in params["expression"]
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": {"editable": True, "kind": "<script>", "mode": "x"}}
    }
    assert await tk.focused_field(relay, SESSION) == {"editable": False, "kind": "", "mode": ""}
    relay.fail.add("Runtime.evaluate")
    assert await tk.focused_field(relay, SESSION) == {"editable": False, "kind": "", "mode": ""}


@pytest.mark.asyncio
async def test_dispatch_sends_on_the_relay_session_and_refuses_before_sending() -> None:
    relay = FakeRelay()
    sent = await tk.dispatch_input(
        relay, SESSION, {"type": "mouse", "x": 1, "y": 2, "button": "right"}
    )
    assert sent == 2
    assert [(m, s) for m, _, s in relay.calls] == [("Input.dispatchMouseEvent", SESSION)] * 2
    with pytest.raises(tk.InputEventError):
        await tk.dispatch_input(relay, SESSION, {"type": "key"})
    assert len(relay.calls) == 2


def test_a_move_accepts_the_cards_zero_click_count_and_carries_the_held_button() -> None:
    """The card sends ``clicks: 0`` on every pointer move (``pointerToEvent``), and a
    move with a button held is a drag: Chrome needs ``button`` and ``buttons`` on it."""
    hover = tk.parse_input_event({"type": "mouse", "action": "move", "x": 3, "y": 4, "clicks": 0})
    assert tk.cdp_calls(hover) == [
        ("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": 3.0, "y": 4.0, "modifiers": 0})
    ]
    drag = tk.parse_input_event(
        {"type": "mouse", "action": "move", "x": 5, "y": 6, "button": "left", "clicks": 0}
    )
    assert tk.cdp_calls(drag) == [
        (
            "Input.dispatchMouseEvent",
            {
                "type": "mouseMoved",
                "x": 5.0,
                "y": 6.0,
                "modifiers": 0,
                "button": "left",
                "buttons": 1,
            },
        )
    ]
    # A press still needs at least one click.
    with pytest.raises(tk.InputEventError):
        tk.parse_input_event({"type": "mouse", "x": 1, "y": 1, "button": "left", "clicks": 0})


def test_frame_pixels_are_divided_by_the_stream_scale_before_cdp() -> None:
    """A 1280-wide viewport streamed at 640: the card's x=100 is CSS x=200."""
    click = tk.parse_input_event({"type": "mouse", "x": 100, "y": 50, "button": "left"})
    for _, params in tk.cdp_calls(click, 0.5):
        assert (params["x"], params["y"]) == (200.0, 100.0)
    wheel = tk.parse_input_event({"type": "wheel", "x": 320, "y": 192, "dx": 0, "dy": 60})
    [(_, params)] = tk.cdp_calls(wheel, 0.5)
    assert (params["x"], params["y"], params["deltaY"]) == (640.0, 384.0, 120.0)
    # Scale 1 (top rung) and a nonsense scale change nothing.
    [(_, p1), _] = tk.cdp_calls(click, 1.0)
    assert (p1["x"], p1["y"]) == (100.0, 50.0)
    [(_, p0), _] = tk.cdp_calls(click, 0.0)
    assert (p0["x"], p0["y"]) == (100.0, 50.0)


def test_the_card_frame_width_is_bounded_and_sets_the_scale() -> None:
    """``w`` is the width of the frame the card mapped against (RM-1)."""
    ev = tk.parse_input_event({"type": "mouse", "x": 1, "y": 1, "button": "left", "w": 640})
    assert ev.frame_w == 640
    assert tk.frame_scale(ev, 1280.0, 1.0) == 0.5
    wheel = tk.parse_input_event({"type": "wheel", "x": 1, "y": 1, "dy": 1, "w": 960})
    assert tk.frame_scale(wheel, 1280.0, 1.0) == 0.75
    # No width, or no viewport width yet: the stream's own ratio.
    bare = tk.parse_input_event({"type": "mouse", "x": 1, "y": 1, "button": "left"})
    assert bare.frame_w == 0 and tk.frame_scale(bare, 1280.0, 0.625) == 0.625
    assert tk.frame_scale(ev, 0.0, 0.625) == 0.625
    for bad in (-1, tk.MAX_COORD + 1, "640", True, float("nan"), float("inf")):
        with pytest.raises(tk.InputEventError):
            tk.parse_input_event({"type": "mouse", "x": 1, "y": 1, "button": "left", "w": bad})


@pytest.mark.asyncio
async def test_dispatch_passes_the_scale_through() -> None:
    relay = FakeRelay()
    await tk.dispatch_input(
        relay, SESSION, {"type": "mouse", "x": 100, "y": 50, "button": "left"}, scale=0.625
    )
    assert relay.calls[0][1]["x"] == 160.0 and relay.calls[0][1]["y"] == 80.0


@pytest.mark.asyncio
async def test_dispatch_notes_each_acknowledged_command_on_the_held_input() -> None:
    """A click whose press lands and whose release fails leaves the button held."""
    relay = FakeRelay()
    held = tk.HeldInput()
    real_send = relay.send

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        if (params or {}).get("type") == "mouseReleased":
            raise RuntimeError("boom")
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await tk.dispatch_input(
            relay, SESSION, {"type": "mouse", "x": 4, "y": 5, "button": "left"}, held=held
        )
    [up] = held.drain()
    assert (up.type, up.action, up.button, up.x, up.y) == ("mouse", "up", "left", 4, 5)
    # A full key press, and committed text, leave nothing down.
    await tk.dispatch_input(relay, SESSION, {"type": "key", "key": "a", "code": "KeyA"}, held=held)
    await tk.dispatch_input(relay, SESSION, {"type": "key", "text": "é"}, held=held)
    assert not held and held.drain() == []


def test_held_input_tracks_at_most_the_cap() -> None:
    held = tk.HeldInput()
    for n in range(tk.MAX_HELD_INPUTS + 5):
        ev = tk.parse_input_event({"type": "key", "action": "down", "key": "x", "code": f"K{n}"})
        held.note_call(ev, "Input.dispatchKeyEvent", {"type": "rawKeyDown"})
    assert len(held) == tk.MAX_HELD_INPUTS
    # One already tracked is still updated at the cap; a new button is not taken on.
    again = tk.parse_input_event({"type": "key", "action": "down", "key": "y", "code": "K0"})
    held.note_call(again, "Input.dispatchKeyEvent", {"type": "keyDown"})
    press = tk.parse_input_event(
        {"type": "mouse", "action": "down", "x": 1, "y": 1, "button": "left"}
    )
    held.note_call(press, "Input.dispatchMouseEvent", {"type": "mousePressed"})
    ups = held.drain()
    assert len(ups) == tk.MAX_HELD_INPUTS and ups[0].key == "y"
    assert all(u.type == "key" for u in ups)
