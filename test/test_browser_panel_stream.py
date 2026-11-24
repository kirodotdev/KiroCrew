"""A panel's pictures: its size, one in flight, no repeat, one still, the mask always on.

Also the pure half of a panel tab (``panel_page``): the address rule and the
in-page scripts' fixed shape. The stream is the real ``PanelStream`` on a fake
relay and a fake clock.
"""

from __future__ import annotations

import asyncio
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from browser_live_helpers import FakeClock, RecordingSink, frame_event, make_jpeg
from browser_panel_helpers import ORIGIN, Client, PanelRelay, make_hub, open_live

from kiro_crew.browser_harness import panel_page
from kiro_crew.browser_harness import panel_stream as ps
from kiro_crew.browser_harness import screencast as sc
from kiro_crew.browser_harness.takeover import InputEventError

pytestmark = pytest.mark.asyncio


def make_png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk))
    )


def stills(relay: PanelRelay) -> list[tuple[str, dict[str, Any], str | None]]:
    """Every still that was asked for: a PNG cast of its own."""
    return [c for c in relay.methods("Page.startScreencast") if c[1].get("format") == "png"]


#: So many pages held for no pointer at once that each one's wait (their number over 30 a second) stops its cast.
HELD_MANY = 9
PERIOD_MANY = HELD_MANY / ps.HELD_PICTURES_PER_S


def pointer_on_from_the_start(stream: ps.PanelStream) -> None:
    """A pointer was on the page when its cast started: the cast runs at the pointer's quality."""
    stream.idle = False
    stream._cast_quality = stream.quality()


async def make_stream(
    *, texels: float = 2.0, ack: bool = True
) -> tuple[ps.PanelStream, PanelRelay, FakeClock, RecordingSink]:
    relay = PanelRelay()
    clock = FakeClock()
    ref: dict[str, Any] = {}
    sink = RecordingSink(ref, ack_on_arrival=ack)
    stream = ps.PanelStream(
        relay,
        "T1",
        sink,
        panel_id="a",
        css_w=1024,
        css_h=720,
        texels=texels,
        clock=clock,
        sleep=clock.sleep,
        auto_control=False,
    )
    ref["stream"] = stream
    await stream.start()
    return stream, relay, clock, sink


async def push(
    relay: PanelRelay,
    payload: int = 512,
    width: int = 2048,
    height: int = 1440,
    view: tuple[Any, Any] | None = (1024, 720),
) -> None:
    """One frame of the cast, made from *view* (``None``: the cast does not say)."""
    event = frame_event(make_jpeg(width, height, payload))
    event["params"]["metadata"] = (
        {} if view is None else {"deviceWidth": view[0], "deviceHeight": view[1]}
    )
    await relay.push("T1", event["method"], event["params"])


# ── sizes ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("asked", "step"),
    [(0.1, 0.5), (0.5, 0.5), (0.6, 0.75), (1.0, 1.0), (1.2, 1.4), (1.4, 1.4), (1.5, 2.0), (9, 2.0)],
)
async def test_detail_is_rounded_up_to_a_step(asked: float, step: float) -> None:
    assert ps.texel_step(asked) == step


@pytest.mark.parametrize("junk", [None, "sharp", float("nan"), [2]])
async def test_junk_detail_is_the_sharpest_step(junk: Any) -> None:
    assert ps.texel_step(junk) == ps.MAX_TEXELS


@pytest.mark.parametrize(
    ("w", "h", "ok"),
    [
        (1024, 720, True),
        (320, 240, True),
        (2560, 1600, True),
        (319, 720, False),
        (2561, 720, False),
        (1024, 99999, False),
        (True, 720, False),
        ("1024", 720, False),
        (float("inf"), 720, False),
        (float("nan"), 720, False),
    ],
)
async def test_a_page_size_is_bounded(w: Any, h: Any, ok: bool) -> None:
    assert (ps.css_size(w, h) is not None) is ok


async def test_png_size_reads_the_header_and_refuses_anything_else() -> None:
    assert ps.png_size(make_png(1434, 1008)) == (1434, 1008)
    assert ps.png_size(make_jpeg(10, 10)) is None
    assert ps.png_size(b"\x89PNG") is None


# ── the screencast a panel asks for ─────────────────────────────────────────


async def test_the_picture_is_the_panels_size_with_one_picture_in_flight() -> None:
    stream, relay, _clock, _sink = await make_stream(texels=1.4)
    (cast,) = relay.methods("Page.startScreencast")
    assert cast[1] == {
        "format": "jpeg",
        # A new panel has no pointer on it yet.
        "quality": ps.HELD_QUALITY,
        "maxWidth": 1434,
        "maxHeight": 1008,
        "everyNthFrame": 1,
        "maxFramesInFlight": 1,
        "sendLastFrame": True,
    }
    assert stream.picture_size() == (1434, 1008)


async def test_a_moving_picture_is_at_most_1434_across_and_the_still_keeps_the_full_size() -> None:
    stream, relay, clock, sink = await make_stream(texels=2.0)
    pointer_on_from_the_start(stream)
    # The page is 1024 x 720 at 2 picture pixels: the moving cast is asked for in the same shape, smaller.
    (cast,) = relay.methods("Page.startScreencast")
    assert (cast[1]["maxWidth"], cast[1]["maxHeight"]) == (ps.MOVING_MAX_WIDTH, 1008)
    assert stream.moving_size() == (1434, 1008) and stream.picture_size() == (2048, 1440)
    await push(relay, width=1434, height=1008)
    assert (sink.frames[-1][0]["w"], sink.frames[-1][0]["h"]) == (1434, 1008)
    # At rest the lossless still is the panel's full size.
    relay.screenshot = make_png(2048, 1440)
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is True
    (shot,) = stills(relay)
    assert shot[1] == {"format": "png", "maxWidth": 2048, "maxHeight": 1440, "everyNthFrame": 1}
    header, blob = sink.frames[-1]
    assert (header["w"], header["h"]) == (2048, 1440) and sc.decode_frame(blob)[1].startswith(
        b"\x89PNG"
    )


async def test_a_page_that_moves_under_a_pointer_keeps_quality_60() -> None:
    # D136 (M117b, M120): at 80 a real page's moving picture grew to about 270 kB and got 10 a second.
    assert ps.MOVING_QUALITY == ps.HELD_QUALITY == 60
    stream, relay, _clock, _sink = await make_stream()
    assert stream.quality() == 60
    pointer_on_from_the_start(stream)
    assert stream.idle is False and stream.quality() == 60
    (cast,) = relay.methods("Page.startScreencast")
    assert cast[1]["quality"] == 60


async def test_a_picture_no_wider_than_the_bound_is_not_made_smaller() -> None:
    stream, _relay, _clock, _sink = await make_stream(texels=1.0)
    assert stream.moving_size() == stream.picture_size() == (1024, 720)


async def test_the_ladder_never_changes_a_panels_picture() -> None:
    stream, relay, _clock, _sink = await make_stream()
    assert await stream._apply_rung(0, sc.rung_index(3, 0)) is False
    assert len(relay.methods("Page.startScreencast")) == 1


async def test_a_new_detail_restarts_the_cast_once_and_the_same_detail_does_not() -> None:
    stream, relay, _clock, _sink = await make_stream()
    assert await stream.set_texels(0.9) is True
    assert relay.methods("Page.startScreencast")[-1][1]["maxWidth"] == 1024
    assert await stream.set_texels(1.0) is False
    assert len(relay.methods("Page.startScreencast")) == 2


async def test_a_new_size_lays_the_page_out_again() -> None:
    stream, relay, _clock, _sink = await make_stream()
    assert await stream.set_size(1400, 900) is True
    metrics = relay.methods("Emulation.setDeviceMetricsOverride")[-1][1]
    assert metrics == {"width": 1400, "height": 900, "deviceScaleFactor": 0, "mobile": False}
    assert relay.methods("Page.startScreencast")[-1][1]["maxWidth"] == ps.MOVING_MAX_WIDTH
    assert await stream.set_size(1400, 900) is False


def a_window(relay: PanelRelay) -> None:
    """The browser says its window and the view in it, as Chromium's own do."""
    relay.windows["T1"] = 7
    relay.window_bounds = {"left": 20, "top": 20, "width": 1280, "height": 800}
    relay.view = {"pageX": 0, "pageY": 0, "clientWidth": 1280, "clientHeight": 657}


async def test_the_window_gets_the_page_size_and_its_own_frame() -> None:
    stream, relay, _clock, _sink = await make_stream()
    a_window(relay)
    await stream.fit_window()
    assert relay.methods("Browser.setWindowBounds")[-1][1] == {
        "windowId": 7,
        "bounds": {"width": 1024, "height": 720 + 143},
    }
    # At a new size too, before the page is laid out. The frame is not read
    # again: a tab that is laid out answers with the page's size, not its view's.
    relay.view = {"clientWidth": 1024, "clientHeight": 720}
    await stream.set_size(1400, 900)
    order = [
        c[0]
        for c in relay.calls
        if c[0] in ("Browser.setWindowBounds", "Emulation.setDeviceMetricsOverride")
    ]
    assert order[-2:] == ["Browser.setWindowBounds", "Emulation.setDeviceMetricsOverride"]
    assert relay.methods("Browser.setWindowBounds")[-1][1]["bounds"] == {
        "width": 1400,
        "height": 900 + 143,
    }
    assert len(relay.methods("Page.getLayoutMetrics")) == 1


@pytest.mark.parametrize(
    ("bounds", "view"),
    [
        (None, {"clientWidth": 1280, "clientHeight": 657}),
        ({"width": 1280, "height": 800}, None),
        ({"width": 1280, "height": 800}, {"clientWidth": 1280}),
        ({"width": 1280, "height": 800}, {"clientWidth": 1280.5, "clientHeight": 657}),
        ({"width": True, "height": 800}, {"clientWidth": 1, "clientHeight": 657}),
        # A view larger than its window, or a frame of a size no window has.
        ({"width": 1024, "height": 600}, {"clientWidth": 1280, "clientHeight": 657}),
        ({"width": 1280, "height": 1800}, {"clientWidth": 1280, "clientHeight": 657}),
    ],
)
async def test_a_browser_that_does_not_say_its_window_keeps_it(bounds: Any, view: Any) -> None:
    stream, relay, _clock, _sink = await make_stream()
    relay.windows["T1"] = 7
    relay.window_bounds, relay.view = bounds, view
    await stream.fit_window()
    # Not asked a second time either: by then the tab is laid out.
    a_window(relay)
    assert await stream.set_size(1400, 900) is True
    assert relay.methods("Browser.setWindowBounds") == []
    assert relay.methods("Emulation.setDeviceMetricsOverride")[-1][1]["width"] == 1400


async def test_a_window_that_refuses_its_size_does_not_stop_a_new_size() -> None:
    stream, relay, _clock, _sink = await make_stream()
    a_window(relay)
    relay.window_refuses = True
    await stream.fit_window()
    assert await stream.set_size(1400, 900) is True
    assert len(relay.methods("Browser.setWindowBounds")) == 2
    assert relay.methods("Emulation.setDeviceMetricsOverride")[-1][1]["width"] == 1400
    assert relay.methods("Page.startScreencast")[-1][1]["maxWidth"] == ps.MOVING_MAX_WIDTH


# ── a new size waits for the page's own layout (D96 7) ──────────────────────


def layout_answers(relay: PanelRelay, answer: Any) -> None:
    """The browser's answer to the two-frames question is *answer*'s; every other call is the fake's."""
    real = relay.send

    async def send(
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        *,
        quiet: bool = False,
    ) -> dict[str, Any]:
        if method == "Runtime.evaluate" and (params or {}).get("expression") == ps.TWO_FRAMES_JS:
            relay.calls.append((method, dict(params or {}), session_id))
            return await answer()
        return await real(method, params, session_id, quiet=quiet)

    relay.send = send  # type: ignore[method-assign]


def layout_questions(relay: PanelRelay) -> list[tuple[str, dict[str, Any], str | None]]:
    return [
        c for c in relay.methods("Runtime.evaluate") if c[1].get("expression") == ps.TWO_FRAMES_JS
    ]


async def in_view_with_a_world(stream: ps.PanelStream) -> None:
    stream.visible = True
    stream.world_of = lambda: 9
    assert stream.casting


async def test_a_new_size_holds_its_pictures_back_until_the_page_had_two_frames() -> None:
    stream, relay, _clock, sink = await make_stream()
    await in_view_with_a_world(stream)
    await push(relay, payload=500)
    sent = len(sink.frames)
    order: list[str] = []

    async def two_frames() -> dict[str, Any]:
        # The browser's frame of the old layout in the new view arrives while the page lays out.
        acks = len(relay.methods("Page.screencastFrameAck"))
        await push(relay, payload=700, width=2800, height=1800, view=(1400, 900))
        assert len(relay.methods("Page.screencastFrameAck")) == acks + 1
        order.append("asked")
        return {"result": {"value": True}}

    layout_answers(relay, two_frames)
    starts = len(relay.methods("Page.startScreencast"))
    assert await stream.set_size(1400, 900) is True
    # Acked and not sent, as a still's frame is.
    assert len(sink.frames) == sent
    (question,) = layout_questions(relay)
    # The gateway's own fixed text, in the hub's own world, nothing else.
    assert question[1] == {
        "expression": ps.TWO_FRAMES_JS,
        "contextId": 9,
        "awaitPromise": True,
        "returnByValue": True,
    }
    assert question[2] == stream.session_id
    assert order == ["asked"]
    # The cast starts again after the question, as before.
    assert len(relay.methods("Page.startScreencast")) == starts + 1
    names = [c[0] for c in relay.calls]
    assert names.index("Runtime.evaluate", names.index("Emulation.setDeviceMetricsOverride")) < (
        len(names) - 1 - names[::-1].index("Page.startScreencast")
    )
    assert stream._settling is False
    # The next frame is sent as any is.
    await push(relay, payload=900, width=2800, height=1800, view=(1400, 900))
    assert len(sink.frames) == sent + 1


async def never_answers() -> dict[str, Any]:
    await asyncio.Event().wait()
    return {}


async def a_navigation_ends_the_question() -> dict[str, Any]:
    raise ps.CdpError("Runtime.evaluate", {"message": "Execution context was destroyed."})


async def the_link_closes() -> dict[str, Any]:
    raise ps.RelayClosed("closed")


@pytest.mark.parametrize("answer", [never_answers, a_navigation_ends_the_question, the_link_closes])
async def test_a_page_that_does_not_answer_costs_the_limit_and_the_size_goes_on_as_before(
    answer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ps, "LAYOUT_WAIT_S", 0.01)
    stream, relay, _clock, sink = await make_stream()
    await in_view_with_a_world(stream)
    layout_answers(relay, answer)
    starts = len(relay.methods("Page.startScreencast"))
    assert await stream.set_size(1400, 900) is True
    assert len(layout_questions(relay)) == 1
    assert len(relay.methods("Page.startScreencast")) == starts + 1
    assert stream._settling is False
    await push(relay, payload=900, width=2800, height=1800, view=(1400, 900))
    assert len(sink.frames) == 1


@pytest.mark.parametrize("which", ["out of view", "not casting", "no world"])
async def test_only_a_stream_in_view_that_casts_asks_the_page(which: str) -> None:
    stream, relay, _clock, _sink = await make_stream()
    await in_view_with_a_world(stream)
    if which == "out of view":
        stream.visible = False
    elif which == "not casting":
        stream.casting = False
    else:
        stream.world_of = lambda: None
    layout_answers(relay, never_answers)
    assert await stream.set_size(1400, 900) is True
    assert layout_questions(relay) == []
    assert relay.methods("Emulation.setDeviceMetricsOverride")[-1][1]["width"] == 1400


async def test_a_still_asked_during_the_wait_comes_after_it() -> None:
    # M60: the still's own cast would catch the page before its layout and send it lossless.
    stream, relay, clock, sink = await make_stream()
    await in_view_with_a_world(stream)
    stream.idle = False
    relay.screenshot = make_png(2800, 1800)
    asked: list[bool] = []

    async def two_frames() -> dict[str, Any]:
        await push(relay, payload=700, width=2800, height=1800, view=(1400, 900))
        clock.now += sc.SETTLE_S + 0.1
        asked.append(await stream.settle())
        return {"result": {"value": True}}

    layout_answers(relay, two_frames)
    assert await stream.set_size(1400, 900) is True
    assert asked == [False]
    assert stills(relay) == []
    # After the wait the next look takes it, of the page laid out at the new size.
    relay.still_view = {"deviceWidth": 1400, "deviceHeight": 900}
    await push(relay, payload=800, width=2800, height=1800, view=(1400, 900))
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is True
    assert len(stills(relay)) == 1


async def test_a_wait_cancelled_from_outside_leaves_nothing_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # L5, L6: the size op is cancelled mid wait (the socket's chain ends). The question's answer is let go by the
    # relay, `_settling` goes false, and the restart does not run: the cast keeps its old picture box until the next
    # size, which restarts it as any size does.
    monkeypatch.setattr(ps, "LAYOUT_WAIT_S", 30.0)
    stream, relay, _clock, sink = await make_stream()
    await in_view_with_a_world(stream)
    started = asyncio.Event()

    async def held() -> dict[str, Any]:
        started.set()
        await asyncio.Event().wait()
        return {}

    layout_answers(relay, held)
    starts = len(relay.methods("Page.startScreencast"))
    task = asyncio.ensure_future(stream.set_size(1400, 900))
    await asyncio.wait_for(started.wait(), 5)
    assert stream._settling is True
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert stream._settling is False
    assert len(relay.methods("Page.startScreencast")) == starts
    # Frames are sent again at once.
    await push(relay, payload=900, width=2800, height=1800, view=(1400, 900))
    assert len(sink.frames) == 1
    # The next size restarts the cast at its own picture box.
    layout_answers(relay, a_quick_answer)
    assert await stream.set_size(1200, 800) is True
    assert relay.methods("Page.startScreencast")[-1][1]["maxWidth"] == ps.MOVING_MAX_WIDTH


async def a_quick_answer() -> dict[str, Any]:
    return {"result": {"value": True}}


async def test_a_still_under_way_when_a_size_comes_is_dropped() -> None:
    # M73: the still's cast began, the size came while the browser made its frame. The frame is the page at the new
    # size and could pass the shape test with its old layout, so it is not sent; the next look takes a still again.
    stream, relay, clock, sink = await make_stream()
    await in_view_with_a_world(stream)
    stream.idle = False
    layout_answers(relay, a_quick_answer)
    relay.screenshot = make_png(2800, 1800)
    relay.still_view = {"deviceWidth": 1400, "deviceHeight": 900}
    sized: list[asyncio.Future[bool]] = []

    async def size_comes() -> None:
        sized.append(asyncio.ensure_future(stream.set_size(1400, 900)))
        await asyncio.sleep(0)

    relay.on_screenshot = size_comes
    await push(relay, payload=700, width=2048, height=1440, view=(1024, 720))
    sent = len(sink.frames)
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is False
    assert await asyncio.wait_for(sized[0], 5) is True
    # Its cast was asked for, and nothing of it was sent.
    assert len(stills(relay)) == 1
    assert len(sink.frames) == sent
    # After the size the next look takes it, and sends it.
    relay.on_screenshot = None
    await push(relay, payload=800, width=2800, height=1800, view=(1400, 900))
    sent = len(sink.frames)
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is True
    assert len(stills(relay)) == 2
    assert len(sink.frames) == sent + 1


async def test_a_slow_fit_window_holds_the_pictures_one_second_at_most(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M78, R12: the browser takes the new size slowly. Within the first second no moving picture of the old layout is
    # sent and no still is taken; past it the pictures go on, though `fit_window` has not returned.
    stream, relay, clock, sink = await make_stream()
    await in_view_with_a_world(stream)
    stream.idle = False
    layout_answers(relay, a_quick_answer)
    seen: list[tuple[str, int]] = []
    fit = stream.fit_window

    async def slow_fit() -> None:
        before = len(sink.frames)
        await push(relay, payload=650, width=2800, height=1800, view=(1400, 900))
        seen.append(("within", len(sink.frames) - before))
        clock.now += ps.SIZE_HOLD_MOST_S + 0.1
        before = len(sink.frames)
        await push(relay, payload=660, width=2800, height=1800, view=(1400, 900))
        seen.append(("past", len(sink.frames) - before))
        await fit()

    monkeypatch.setattr(stream, "fit_window", slow_fit)
    assert await stream.set_size(1400, 900) is True
    assert seen == [("within", 0), ("past", 1)]
    assert stream._settling is False


async def test_the_hold_starts_before_the_browser_takes_the_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # M73: a still asked while `fit_window` runs is not taken, and a moving frame that arrives then is not sent.
    stream, relay, clock, sink = await make_stream()
    await in_view_with_a_world(stream)
    stream.idle = False
    layout_answers(relay, a_quick_answer)
    relay.screenshot = make_png(2800, 1800)
    seen: list[tuple[bool, bool]] = []
    fit = stream.fit_window

    async def fit_and_look() -> None:
        before = len(sink.frames)
        await push(relay, payload=650, width=2800, height=1800, view=(1400, 900))
        clock.now += sc.SETTLE_S + 0.1
        seen.append((await stream.settle(), len(sink.frames) > before))
        await fit()

    monkeypatch.setattr(stream, "fit_window", fit_and_look)
    assert await stream.set_size(1400, 900) is True
    assert seen == [(False, False)]
    assert stills(relay) == []
    assert stream._settling is False


async def test_two_sizes_in_a_row_each_ask_once_and_restart_once() -> None:
    stream, relay, _clock, _sink = await make_stream()
    await in_view_with_a_world(stream)
    layout_answers(relay, a_quick_answer)
    starts = len(relay.methods("Page.startScreencast"))
    assert await stream.set_size(1400, 900) is True
    assert await stream.set_size(1200, 800) is True
    assert len(layout_questions(relay)) == 2
    assert len(relay.methods("Page.startScreencast")) == starts + 2
    assert relay.methods("Page.startScreencast")[-1][1]["maxWidth"] == ps.MOVING_MAX_WIDTH
    assert stream._settling is False


async def test_a_page_no_pointer_is_on_holds_its_frames_back_too() -> None:
    # The paced ack of a page held for no pointer waits its idle period first; the frame is still not sent.
    stream, relay, _clock, sink = await make_stream()
    await in_view_with_a_world(stream)
    stream.idle = True
    acks: list[int] = []

    async def two_frames() -> dict[str, Any]:
        before = len(relay.methods("Page.screencastFrameAck"))
        await push(relay, payload=700, width=2800, height=1800, view=(1400, 900))
        acks.append(len(relay.methods("Page.screencastFrameAck")) - before)
        return {"result": {"value": True}}

    layout_answers(relay, two_frames)
    assert await stream.set_size(1400, 900) is True
    assert sink.frames == []
    assert acks == [1]


async def test_the_hub_gives_the_stream_its_own_world_of_the_page() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, "owner-session-token-A")
    panel = await open_live(hub, browser, client)
    assert panel.stream is not None
    assert panel.world is not None
    assert panel.stream.world_of() == panel.world
    panel.world = None
    assert panel.stream.world_of() is None


# ── pictures ────────────────────────────────────────────────────────────────


async def test_a_picture_names_its_panel_and_its_css_size() -> None:
    stream, relay, _clock, sink = await make_stream()
    await push(relay)
    ((header, _blob),) = sink.frames
    assert header == {
        "type": "xr_panel_frame",
        "panel": "a",
        "stream": stream.stream_id,
        "seq": 1,
        "w": 2048,
        "h": 1440,
        "css": [1024, 720],
        "mask": [],
        "misshapen": 0,
        "round_ms": None,
        "pace": sc.LADDER[stream.controller.rung].fps,
        "held": True,
    }


async def test_a_picture_carries_the_round_trip_of_the_one_before_and_whether_it_is_held() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    stream.idle = False
    ref = sink

    async def ack_later(header: Any, blob: bytes) -> None:
        ref.frames.append((header, blob))
        # The panel's ack comes 40 ms after the picture went out.
        clock.now += 0.04
        stream.ack(header["seq"])

    sink.frame = ack_later  # type: ignore[method-assign]
    await push(relay, payload=500)
    await push(relay, payload=600)
    first, second = (h for h, _ in sink.frames)
    assert first["round_ms"] is None and second["round_ms"] == 40
    assert second["held"] is False


async def test_the_pace_hears_decoded_and_the_held_wait_hears_landed() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    seen: list[str] = []

    async def ack_twice(header: Any, blob: bytes) -> None:
        sink.frames.append((header, blob))
        # Decoded at once; in the texture 20 ms later, after the pace has gone on.
        assert stream.ack(header["seq"], "decoded") is True
        seen.append(f"decoded {stream.landed_seq}")
        clock.now += 0.02
        stream.ack(header["seq"], "landed")
        seen.append(f"landed {stream.landed_seq}")

    sink.frame = ack_twice  # type: ignore[method-assign]
    await push(relay, payload=500)
    assert seen == ["decoded 0", "landed 1"]
    assert stream.landed_seq == 1
    # A word that is neither is no ack; no word is an older XR page's one ack, which is both.
    assert stream.ack(1, "seen") is False
    assert stream.landed_seq == 1


def test_an_ack_releases_every_picture_up_to_it_and_times_only_its_own() -> None:
    from kiro_crew.browser_harness.panel_pace import Flight

    f = Flight()
    f.sent(1, 10.0, 100, measured=True)
    f.sent(2, 10.1, 200, measured=True)
    f.sent(3, 10.2, 300, measured=True)
    assert f.older_than(3) == 2 and f.bytes() == 600
    # The ack of 2 releases 1 too; 1 has no delivery of its own (it was replaced before its upload).
    # Its busy time runs from its own send: no decoded ack came before it.
    assert f.acked(2, 10.3) == (True, (200, pytest.approx(0.2), pytest.approx(0.2)))
    assert sorted(f.pictures) == [3]
    assert f.acked(2, 10.4) == (False, None)
    # A still is released like any picture and is no evidence for the pace.
    f.sent(4, 11.0, 9000, measured=False)
    assert f.acked(4, 11.5) == (True, None)
    # Sent before the last ack: its busy time runs from that ack, not from its send.
    f.sent(6, 11.4, 500, measured=True)
    assert f.acked(6, 11.6) == (True, (500, pytest.approx(0.2), pytest.approx(0.1)))
    # A picture no ack came for is given up as a late delivery.
    f.sent(5, 12.0, 100, measured=True)
    assert f.given_up(5, 14.0) == [(100, pytest.approx(2.0))]
    assert not f.pictures


async def test_a_picture_released_by_a_newer_ack_is_not_timed() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    pointer_on_from_the_start(stream)
    timed: list[tuple[int, float]] = []
    stream.pace.record = lambda n, d, b=None: timed.append((n, d))  # type: ignore[method-assign]
    await push(relay, payload=500)
    clock.now += 0.05
    stream.flight.sent(2, clock.now, 777, measured=True)
    clock.now += 0.03
    assert stream.ack(2, "decoded") is True
    assert timed == [(777, pytest.approx(0.03))]
    assert not stream.flight.pictures


async def test_a_second_picture_waits_for_the_first_when_the_socket_holds_enough_bytes() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    pointer_on_from_the_start(stream)
    # The other panels on the socket hold nearly all the bytes a socket may have on their way.
    stream.socket_flight = lambda: stream.flight.bytes() + ps.PANEL_SOCKET_BYTES - 100
    await push(relay, payload=500)
    # The picture waited for its own ack (none came: the whole bound) before the browser was asked again.
    assert sc.CARD_ACK_TIMEOUT_S in clock.sleeps


async def test_a_held_page_asks_for_its_next_picture_on_its_clock_with_one_on_its_way() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    acks = len(relay.methods("Page.screencastFrameAck"))
    await push(relay, payload=500)
    # Released at once (no wait for the panel's ack), and the browser was asked again only after the period.
    assert sc.CARD_ACK_TIMEOUT_S not in clock.sleeps
    assert pytest.approx(ps.IDLE_PERIOD_S, abs=0.02) in clock.sleeps
    assert len(relay.methods("Page.screencastFrameAck")) == acks + 1
    assert sorted(stream.flight.pictures) == [1]


async def test_a_held_page_stops_its_cast_only_once_a_lost_picture_is_given_up() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    stream.held_count = lambda: HELD_MANY
    clock.now += 30
    late: list[float] = []
    stream.pace.record = lambda n, d, b=None: late.append(d)  # type: ignore[method-assign]
    stops: list[float] = []
    stop = stream._stop_cast

    async def stop_at() -> None:
        stops.append(clock.now)
        await stop()

    stream._stop_cast = stop_at  # type: ignore[method-assign]
    sent_at = clock.now
    await push(relay, payload=500)
    # No ack came and the page went quiet: the cast was stopped, but only once the
    # picture was CARD_ACK_TIMEOUT_S old and given up as a late delivery.
    assert len(stops) == 1 and stops[0] - sent_at >= sc.CARD_ACK_TIMEOUT_S - 1e-6
    assert late == [pytest.approx(sc.CARD_ACK_TIMEOUT_S, abs=0.01)]
    assert not stream.flight.pictures


async def test_a_held_page_waits_for_its_picture_to_land_before_its_cast_is_stopped() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    # Enough pages held at once that the wait is long enough to stop the cast for.
    stream.held_count = lambda: HELD_MANY
    clock.now += 30

    async def decoded_only(header: Any, blob: bytes) -> None:
        sink.frames.append((header, blob))
        stream.ack(header["seq"], "decoded")

    sink.frame = decoded_only  # type: ignore[method-assign]
    stops = len(relay.methods("Page.stopScreencast"))
    await push(relay, payload=500)
    # No landed ack came: the stream waited its bound for it, then stopped the cast for the idle wait.
    assert ps.LANDED_WAIT_S in clock.sleeps
    assert clock.sleeps.index(ps.LANDED_WAIT_S) < clock.sleeps.index(pytest.approx(PERIOD_MANY))
    assert len(relay.methods("Page.stopScreencast")) == stops + 1


def _panel_ladder() -> Any:
    from kiro_crew.browser_harness.panel_pace import PanelController

    c = PanelController()
    c.set_floors(fps_floor=sc.FPS_STEPS.index(ps.PANEL_MAX_FPS))
    return c


def _pace_after(*windows: list[tuple[int, float]]) -> int:
    """The fps a panel's ladder stands at after one second of pictures per window."""
    c = _panel_ladder()
    for window in windows:
        for nbytes, delivery in window:
            c.record(nbytes, delivery)
        c.tick()
    return sc.FPS_STEPS[c.fps_step]


def _late(pictures: list[tuple[int, float]]) -> int:
    """How many of *pictures*, recorded in one window, count as late for a panel."""
    c = _panel_ladder()
    for nbytes, delivery in pictures:
        c.record(nbytes, delivery)
    return int(c.window.late)


def test_a_long_round_trip_alone_is_never_late() -> None:
    # 100 ms there and back plus 75 kB on 6 Mbit/s: every picture takes about 200 ms.
    # A card's rule counts each one late; past the panel's floor, none is.
    assert _late([(75_000, 0.2 + 0.004 * (i % 5)) for i in range(30)]) == 0


def test_a_small_picture_does_not_make_the_bigger_ones_after_it_late() -> None:
    # A nearly blank page while it loads comes back fast; the page's own pictures then do not.
    assert _late([(10_000, 0.05)] + [(75_000, 0.2)] * 29) == 0
    # On a link that carries the page, the pace stays.
    second = [(10_000, 0.005)] + [(130_000, 0.03)] * 19
    assert _pace_after(second, second, second) == ps.PANEL_MAX_FPS


def test_a_full_link_still_steps_a_panels_pace_down() -> None:
    # The bytes ahead of each picture queue it: deliveries climb past the floor.
    second = [(75_000, 0.2 + 0.12 * i) for i in range(5)]
    assert _pace_after(second, second) < ps.PANEL_MAX_FPS


def test_a_link_full_from_the_start_steps_a_panels_pace_down() -> None:
    # A queue that stands: two in flight, every delivery the same and long. Nothing is
    # past a floor, and the link's rate, the whole bytes over the whole time, steps it down.
    second = [(75_000, 0.3)] * 6
    assert _pace_after(second, second) < ps.PANEL_MAX_FPS


def test_the_ladder_reads_a_panels_whole_deliveries() -> None:
    c = _panel_ladder()
    for nbytes, delivery in [(50_000, 0.2), (50_000, 0.3), (100_000, 0.5)]:
        c.record(nbytes, delivery)
    # The link's rate is these bytes over this time; the mean delivery is this time over three.
    assert c.window.bytes == 200_000
    assert c.window.delivery_s == pytest.approx(1.0)


def test_a_link_of_twenty_a_second_with_two_in_flight_keeps_the_pace_at_fifteen() -> None:
    from kiro_crew.browser_harness.panel_pace import Flight

    # 75 kB pictures on a link that carries 20 a second (1500 kB/s, 50 ms each) with a
    # round trip of 20 ms. Two are on their way: the next goes at the ack of the one
    # before the last, so each waits behind the one ahead and its whole delivery holds that wait.
    c, f = _panel_ladder(), Flight()
    sent, done, link_free = [0.0, 0.0], [], 0.0
    for i in range(80):
        at = sent[i]
        link_free = max(at, link_free) + 0.05
        done.append(link_free + 0.02)
        f.sent(i, at, 75_000, measured=True)
        sent.append(done[i])
    window_end = 1.0
    for i in sorted(range(80), key=lambda k: done[k]):
        if done[i] > window_end:
            c.tick()
            window_end += 1.0
        _, own = f.acked(i, done[i])
        assert own is not None
        c.record(*own)
    # Read by whole deliveries the link looks like 750 kB/s and the pace falls to 5; by busy time it is 1500.
    assert sc.FPS_STEPS[c.fps_step] >= 15


def test_the_first_big_picture_after_a_load_is_not_late_and_the_pace_stays() -> None:
    from kiro_crew.browser_harness.panel_pace import PANEL_FLOOR_CAP_S

    c = _panel_ladder()
    for _ in range(20):
        c.record(20_000, 0.01)
    # No recent picture of half its size: the floor is the cap, not nothing.
    assert c.floor(150_000) == PANEL_FLOOR_CAP_S
    c.tick()
    big = [(150_000, 0.3)] * 4
    for _ in range(3):
        for nbytes, delivery in big:
            c.record(nbytes, delivery, 0.02)
        assert c.window.late == 0
        c.tick()
    # Not late, so the pace is not stepped down for lateness. It stands where four pictures
    # in flight put it at a round of 0.3 s (13 a second: `panel_pace.link_time`).
    assert sc.FPS_STEPS[c.fps_step] == 10
    assert c.window.late == 0


def test_a_slow_xr_decode_still_steps_a_panels_pace_down() -> None:
    from kiro_crew.browser_harness.panel_pace import PANEL_FLOOR_CAP_S

    # A decode that turns slow: from 20 ms to 400 ms.
    fast, slow = [(20_000, 0.02)] * 20, [(20_000, 0.4)] * 3
    assert _pace_after(fast, slow, slow) < ps.PANEL_MAX_FPS
    assert _late([(20_000, 0.02)] * 5 + [(20_000, 0.4)] * 3) == 3
    # A decode slow from the first picture: the floor counts no more than its cap.
    assert _late([(20_000, 0.4)] * 3) == 3
    c = _panel_ladder()
    c.record(20_000, 0.4)
    assert c.floor(20_000) == PANEL_FLOOR_CAP_S


async def test_a_panel_stream_paces_by_its_own_controller() -> None:
    from kiro_crew.browser_harness.panel_pace import PanelController

    stream, _relay, _clock, _sink = await make_stream()
    assert isinstance(stream.controller, PanelController)
    assert stream.controller.fps_floor == stream.config.fps_floor()


# ── a picture has its page's shape ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("w", "h", "css", "ok"),
    [
        (2048, 1440, (1024, 720), True),
        (1434, 1008, (1024, 720), True),
        # Scaled into its box and rounded: one pixel off is the same shape.
        (1618, 1136, (1156, 812), True),
        (1618, 1137, (1156, 812), True),
        (1618, 1139, (1156, 812), False),
        (1433, 1008, (1024, 720), True),
        # A tab with its window's own view, 1280 by 657, scaled into the page's box.
        (2048, 1051, (1024, 720), False),
        (1280, 657, (1024, 720), False),
        (1280, 720, (1024, 720), False),
        # The first frame after a show, made on a surface 56 px taller than the view.
        (1330, 1008, (1024, 720), False),
        (1024, 800, (1024, 720), False),
        (1434, 1005, (1024, 720), False),
        (0, 720, (1024, 720), False),
        (1024, 720, (0, 720), False),
    ],
)
async def test_which_pictures_have_the_shape_of_a_page(
    w: int, h: int, css: tuple[int, int], ok: bool
) -> None:
    assert ps.has_shape(w, h, *css) is ok


async def test_a_frame_of_another_shape_is_dropped_and_the_page_is_laid_out_again() -> None:
    stream, relay, clock, sink = await make_stream()
    laid = len(relay.methods("Emulation.setDeviceMetricsOverride"))
    # The tab came in front with its window's view: the frame has the window's shape.
    before = clock.now
    await push(relay, width=2048, height=1051)
    assert sink.frames == []
    # No pointer is on the page, and the frame is acked at once all the same: the
    # page has had no picture from it, so the idle period is not waited out.
    assert clock.now == before
    assert stream.misshapen == 1
    metrics = relay.methods("Emulation.setDeviceMetricsOverride")
    assert len(metrics) == laid + 1
    assert (metrics[-1][1]["width"], metrics[-1][1]["height"]) == (1024, 720)
    # It is acked all the same, or the browser would make no further picture.
    assert len(relay.methods("Page.screencastFrameAck")) == 1
    # The next one is judged the same way: a second one of that shape is dropped too,
    await push(relay, payload=600, width=2048, height=1051)
    assert sink.frames == [] and stream.misshapen == 2
    assert len(relay.methods("Emulation.setDeviceMetricsOverride")) == laid + 2
    # and one of the page's shape goes out, and says how many were dropped.
    await push(relay, payload=700)
    ((header, _blob),) = sink.frames
    assert (header["w"], header["h"], header["misshapen"]) == (2048, 1440, 2)
    assert len(relay.methods("Emulation.setDeviceMetricsOverride")) == laid + 2


@pytest.mark.parametrize(
    ("metadata", "css", "ok"),
    [
        ({"deviceWidth": 1024, "deviceHeight": 720}, (1024, 720), True),
        ({"deviceWidth": 1024.5, "deviceHeight": 719}, (1024, 720), True),
        ({"deviceWidth": 1026, "deviceHeight": 720}, (1024, 720), False),
        ({"deviceWidth": 1280, "deviceHeight": 657}, (1024, 720), False),
        # The window's own view has the shape of this page, and is not its view.
        ({"deviceWidth": 1280, "deviceHeight": 657}, (1403, 720), False),
        ({"deviceWidth": 1403, "deviceHeight": 720}, (1403, 720), True),
        # A cast that does not say, or says no size.
        ({}, (1024, 720), None),
        (None, (1024, 720), None),
        ({"deviceWidth": 1024}, (1024, 720), None),
        ({"deviceWidth": "1024", "deviceHeight": 720}, (1024, 720), None),
        ({"deviceWidth": True, "deviceHeight": 720}, (1024, 720), None),
        ({"deviceWidth": 0, "deviceHeight": 720}, (1024, 720), None),
        ({"deviceWidth": float("nan"), "deviceHeight": 720}, (1024, 720), None),
    ],
)
async def test_which_view_a_frame_says_it_was_made_from(
    metadata: Any, css: tuple[int, int], ok: bool | None
) -> None:
    assert ps.from_view(metadata, *css) is ok


async def test_a_frame_from_another_view_is_dropped_whatever_its_shape() -> None:
    stream, relay, _clock, sink = await make_stream()
    # A corner's drag gave the panel the shape of the window's own view.
    await stream.set_size(1403, 720)
    assert ps.has_shape(2560, 1314, 1403, 720)
    laid = len(relay.methods("Emulation.setDeviceMetricsOverride"))
    # The tab came in front with its window's view, 1280 by 657: the frame has the
    # page's shape, and is the page cut at the right and at the bottom.
    await push(relay, width=2560, height=1314, view=(1280, 657))
    assert sink.frames == [] and stream.misshapen == 1
    assert len(relay.methods("Emulation.setDeviceMetricsOverride")) == laid + 1
    assert len(relay.methods("Page.screencastFrameAck")) == 1
    # The same picture from the page's own view goes out.
    await push(relay, payload=600, width=2560, height=1314, view=(1403, 720))
    ((header, _blob),) = sink.frames
    assert (header["w"], header["h"], header["misshapen"]) == (2560, 1314, 1)


async def test_a_frame_that_names_no_view_is_judged_by_its_shape() -> None:
    stream, relay, _clock, sink = await make_stream()
    await push(relay, width=2048, height=1051, view=None)
    assert sink.frames == [] and stream.misshapen == 1
    await push(relay, payload=600, view=None)
    assert len(sink.frames) == 1


async def test_a_frame_of_another_shape_from_the_pages_own_view_is_dropped_too() -> None:
    # The first frame after a show is at times made on a surface 56 px taller than
    # the view it names: 1024 by 776 scaled into the box of a page at detail 1.4.
    stream, relay, _clock, sink = await make_stream(texels=1.4)
    await push(relay, width=1330, height=1008, view=(1024, 720))
    assert sink.frames == [] and stream.misshapen == 1
    await push(relay, payload=600, width=1434, height=1008, view=(1024, 720))
    assert len(sink.frames) == 1


async def test_frames_of_another_shape_are_acked_no_faster_than_four_a_second() -> None:
    stream, relay, clock, _sink = await make_stream()
    # A pointer is on the page: nothing else holds its pictures back.
    stream.idle = False
    # The ack grid lets the first two through after a quiet spell; from then on
    # each one waits for its slot.
    await push(relay, width=2048, height=1051)
    await push(relay, payload=600, width=2048, height=1051)
    before = clock.now
    await push(relay, payload=700, width=2048, height=1051)
    await push(relay, payload=800, width=2048, height=1051)
    assert clock.now - before >= 2 * ps.MISSHAPEN_ACK_S
    assert len(relay.methods("Page.screencastFrameAck")) == 4


async def test_a_still_of_another_shape_is_not_sent_and_the_next_look_takes_one_again() -> None:
    stream, relay, clock, sink = await make_stream(texels=1.4)
    stream.idle = False
    await push(relay, width=1434, height=1008)
    laid = len(relay.methods("Emulation.setDeviceMetricsOverride"))
    relay.screenshot = make_png(1434, 736)
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is False
    assert len(sink.frames) == 1 and stream.misshapen == 1
    assert len(relay.methods("Emulation.setDeviceMetricsOverride")) == laid + 1
    relay.screenshot = make_png(1434, 1008)
    clock.now += sc.SETTLE_MIN_INTERVAL_S + 0.1
    assert await stream.settle() is True
    header, blob = sink.frames[-1]
    assert (header["w"], header["h"], header["misshapen"]) == (1434, 1008, 1)
    assert sc.decode_frame(blob)[1].startswith(b"\x89PNG")


async def test_a_frame_with_the_bytes_of_the_last_picture_is_not_sent_again() -> None:
    _stream, relay, _clock, sink = await make_stream()
    # Enough pages held at once that the wait is long enough to stop the cast for.
    _stream.held_count = lambda: HELD_MANY
    await push(relay)
    await push(relay)
    await push(relay)
    assert len(sink.frames) == 1
    # Chromium is still acked for each repeat, or it would send nothing more. (The
    # picture's own go-ahead is the start after the idle wait, which stopped the cast.)
    assert len(relay.methods("Page.screencastFrameAck")) == 2
    await push(relay, payload=600)
    assert len(sink.frames) == 2


async def test_a_frame_that_was_never_sent_is_not_the_last_picture_sent() -> None:
    stream, relay, _clock, sink = await make_stream()
    answer = relay.send
    left: list[bool] = []

    async def leave_view_while_the_page_is_asked(
        method: str, params: Any = None, session_id: Any = None, **more: Any
    ) -> Any:
        # The panel leaves the person's view while the frame's mask query is on its way.
        if method == "Runtime.evaluate" and not left:
            left.append(True)
            await stream.set_visible(False)
        return await answer(method, params, session_id, **more)

    relay.send = leave_view_while_the_page_is_asked  # type: ignore[method-assign]
    await push(relay)
    assert left and sink.frames == []
    # Back in view, the page has not changed: the browser sends the same bytes again.
    # They are the page's first picture, not a repeat of one the panel has.
    await stream.set_visible(True)
    await push(relay)
    assert len(sink.frames) == 1
    await push(relay)
    assert len(sink.frames) == 1


async def test_a_page_at_rest_is_sent_once_more_when_its_panel_comes_back_into_view() -> None:
    stream, relay, _clock, sink = await make_stream()
    await push(relay)
    assert len(sink.frames) == 1
    # In view and at rest: the same bytes again show nothing new.
    await push(relay)
    assert len(sink.frames) == 1
    # The panel leaves the person's view and comes back. The XR page takes no press on it until a
    # picture lands, and the page has not changed: its first frame has the bytes of the last one sent.
    await stream.set_visible(False)
    await stream.set_visible(True)
    await push(relay)
    assert len(sink.frames) == 2
    # Once only: the frame after it, with the same bytes, is nothing new again.
    await push(relay)
    assert len(sink.frames) == 2


class OneInFlight:
    """The browser's side of ``maxFramesInFlight: 1``.

    It makes its next frame only once the last one was acked, or the cast was
    started again (a start makes a frame at once), as Chromium does. A test that
    pushes a second frame by hand says nothing about a frame that was never
    acked: the real browser would not have made the second one.
    """

    def __init__(self, relay: PanelRelay) -> None:
        self.relay = relay
        self.made = 0

    async def paints(self, payload: int = 512) -> bool:
        """The page painted: a frame goes to the stream, unless one is still in flight."""
        go = len(self.relay.methods("Page.screencastFrameAck")) + len(moving_starts(self.relay)) - 1
        if go < self.made:
            return False
        self.made += 1
        await push(self.relay, payload)
        return True


async def test_a_picture_the_socket_did_not_take_is_acked_so_the_browser_makes_the_next() -> None:
    _stream, relay, _clock, sink = await make_stream()
    browser = OneInFlight(relay)
    take = sink.frame
    refused: list[bool] = []

    async def refuse_the_first(header: Any, blob: bytes) -> None:
        if not refused:
            refused.append(True)
            raise asyncio.TimeoutError
        await take(header, blob)

    sink.frame = refuse_the_first  # type: ignore[method-assign]
    assert await browser.paints()
    assert refused and sink.frames == []
    # The browser was told that the frame is done, so its next one comes: the same
    # page, the same bytes. They are not a repeat of a picture the panel has.
    assert await browser.paints()
    assert len(sink.frames) == 1
    # And the page goes on from there.
    assert await browser.paints(payload=600)
    assert len(sink.frames) == 2


async def test_a_socket_that_takes_no_picture_gets_two_frames_a_second_with_a_hand_on_the_page() -> (
    None
):
    stream, relay, clock, sink = await make_stream()
    browser = OneInFlight(relay)
    take = sink.frame
    dead = [True]
    asked: list[int] = []

    async def closing(header: Any, blob: bytes) -> None:
        asked.append(1)
        if dead[0]:
            raise ConnectionResetError("Cannot write to closing transport")
        await take(header, blob)

    sink.frame = closing  # type: ignore[method-assign]
    # A hand on the page: thirty pictures a second when they arrive.
    stream.idle = False
    start = clock.now
    for i in range(20):
        assert await browser.paints(payload=512 + i)
    assert len(asked) == 20 and sink.frames == []
    # The first two find the grid at rest; from the third on, one each half second.
    assert clock.now - start >= 18 * sc.FAILED_FRAME_PERIOD_S
    # The socket takes pictures again: the pace is the panel's own from the next one on.
    dead[0] = False
    start = clock.now
    for i in range(30):
        assert await browser.paints(payload=700 + i)
    assert len(sink.frames) == 30 and clock.now - start <= 1.6


async def test_frames_go_through_the_stream_one_at_a_time() -> None:
    stream, relay, _clock, sink = await make_stream()
    answer = relay.send
    reached, go = asyncio.Event(), asyncio.Event()

    async def hold_the_first_question(
        method: str, params: Any = None, session_id: Any = None, **more: Any
    ) -> Any:
        if method == "Runtime.evaluate" and not reached.is_set():
            reached.set()
            await go.wait()
        return await answer(method, params, session_id, **more)

    relay.send = hold_the_first_question  # type: ignore[method-assign]
    first = asyncio.ensure_future(push(relay, payload=512))
    await asyncio.wait_for(reached.wait(), 5)
    # The first frame is being asked about its secret fields when the second arrives.
    second = asyncio.ensure_future(push(relay, payload=700))
    for _ in range(8):
        await asyncio.sleep(0)
    assert stream._frame_lock.locked() and not second.done() and sink.frames == []
    go.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert [header["seq"] for header, _ in sink.frames] == [1, 2]


async def test_a_panel_that_leaves_the_view_while_its_cast_starts_ends_with_no_cast() -> None:
    stream, relay, _clock, _sink = await make_stream()
    await stream.set_visible(False)
    answer = relay.send
    reached, go = asyncio.Event(), asyncio.Event()

    async def hold_the_start(
        method: str, params: Any = None, session_id: Any = None, **more: Any
    ) -> Any:
        if method == "Page.startScreencast" and not reached.is_set():
            reached.set()
            await go.wait()
        return await answer(method, params, session_id, **more)

    relay.send = hold_the_start  # type: ignore[method-assign]
    show = asyncio.ensure_future(stream.set_visible(True))
    await asyncio.wait_for(reached.wait(), 5)
    # The browser has not answered yet, so no cast "runs", and the panel leaves the view.
    hide = asyncio.ensure_future(stream.set_visible(False))
    go.set()
    await asyncio.wait_for(asyncio.gather(show, hide), 5)
    assert not stream.visible and not stream.casting
    casts = [m for m, _, _ in relay.calls if m in ("Page.startScreencast", "Page.stopScreencast")]
    assert casts[-2:] == ["Page.startScreencast", "Page.stopScreencast"]
    # A cast that ran unseen would have kept the hub from ever starting one again.
    await stream.set_visible(True)
    assert stream.casting


async def test_a_hide_waits_behind_a_restart_and_ends_with_no_cast() -> None:
    stream, relay, _clock, _sink = await make_stream()
    answer = relay.send
    reached, go = asyncio.Event(), asyncio.Event()

    async def hold_the_restarts_start(
        method: str, params: Any = None, session_id: Any = None, **more: Any
    ) -> Any:
        if method == "Page.startScreencast" and not reached.is_set():
            reached.set()
            await go.wait()
        return await answer(method, params, session_id, **more)

    relay.send = hold_the_restarts_start  # type: ignore[method-assign]
    # The watchdog starts the cast again: a stop, then a start that the browser has not answered...
    again = asyncio.ensure_future(stream.restart())
    await asyncio.wait_for(reached.wait(), 5)
    # ...when the panel leaves the view.
    hide = asyncio.ensure_future(stream.set_visible(False))
    go.set()
    await asyncio.wait_for(asyncio.gather(again, hide), 5)
    assert not stream.visible and not stream.casting and relay.casts == set()


async def test_a_start_that_the_browser_refuses_is_asked_again_behind_a_stop() -> None:
    stream, relay, _clock, _sink = await make_stream()
    await stream.set_visible(False)
    # The browser starts the cast, and its answer never arrives.
    relay.lose_starts = 1
    with pytest.raises(asyncio.TimeoutError):
        await stream.set_visible(True)
    assert not stream.visible and not stream.casting and relay.casts == {stream.session_id}
    # The next look: the browser refuses a second cast. The stream stops the one
    # that runs and asks again, so the cast is the one this panel asked for.
    mark = len(relay.calls)
    await stream.set_visible(True)
    asked = [m for m, _, _ in relay.calls[mark:] if m.endswith("Screencast")]
    assert asked == ["Page.startScreencast", "Page.stopScreencast", "Page.startScreencast"]
    assert stream.visible and stream.casting


async def test_a_cast_counts_as_stopped_only_when_the_browser_answered_the_stop() -> None:
    stream, relay, _clock, sink = await make_stream()
    relay.lose_stops = 1
    with pytest.raises(asyncio.TimeoutError):
        await stream.set_visible(False)
    # Out of view, and the cast still runs in the browser: the stream says so too.
    assert not stream.visible and stream.casting and relay.casts == {stream.session_id}
    # A frame of that cast goes to no panel.
    await push(relay)
    assert sink.frames == []
    # Back in view, the pictures come: the cast is started anew.
    await stream.set_visible(True)
    assert stream.visible and stream.casting
    await push(relay)
    assert len(sink.frames) == 1


async def test_four_pictures_are_in_flight_and_a_fifth_waits_for_the_panels_ack() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    pointer_on_from_the_start(stream)
    for n in range(3):
        await push(relay, payload=500 + n)
    # Three are on their way and the browser is asked for the next at once each time.
    assert sc.CARD_ACK_TIMEOUT_S not in clock.sleeps
    assert len(relay.methods("Page.screencastFrameAck")) == 3
    await push(relay, payload=600)
    # No ack came: the fourth waited the whole bound before Chromium was asked for a fifth.
    assert sc.CARD_ACK_TIMEOUT_S in clock.sleeps
    assert len(sink.frames) == 4 and stream.ack(1) is False


async def test_a_panel_holds_its_own_bytes_whatever_room_the_socket_has() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    pointer_on_from_the_start(stream)
    # The socket is empty but for this panel, and this panel's one picture is over half its own bound.
    stream.socket_flight = lambda: stream.flight.bytes()
    await push(relay, payload=ps.PANEL_FLIGHT_BYTES // 2 + 1000)
    assert sc.CARD_ACK_TIMEOUT_S in clock.sleeps
    assert len(sink.frames) == 1


async def test_after_a_picture_was_given_up_one_goes_at_a_time_until_an_ack_comes() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    pointer_on_from_the_start(stream)
    for n in range(4):
        await push(relay, payload=500 + n)
    # No ack came for any: the fourth waited the bound, and all four were given up.
    assert not stream.flight.pictures and stream._gave_up is True
    # From here one goes at a time: the next picture waits for its own ack, though nothing else is on its way.
    clock.sleeps.clear()
    await push(relay, payload=700)
    assert sc.CARD_ACK_TIMEOUT_S in clock.sleeps
    # An ack comes for a picture on its way: pictures go four at a time again.
    stream.flight.sent(99, clock.now, 100, measured=True)
    assert stream.ack(99, "decoded") is True and stream._gave_up is False
    clock.sleeps.clear()
    for n in range(3):
        await push(relay, payload=800 + n)
    assert sc.CARD_ACK_TIMEOUT_S not in clock.sleeps


async def test_one_picture_goes_at_a_time_right_after_a_climb_that_failed() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    pointer_on_from_the_start(stream)
    stream.pace.flight_cap = lambda: 1  # type: ignore[method-assign]
    await push(relay, payload=500)
    # The first waited for its own ack (none came: the whole bound) before the browser was asked again.
    assert sc.CARD_ACK_TIMEOUT_S in clock.sleeps
    assert len(sink.frames) == 1


async def test_the_next_picture_goes_when_the_older_ones_lost_ack_is_given_up() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    pointer_on_from_the_start(stream)
    late: list[float] = []
    stream.pace.record = lambda n, d, b=None: late.append(d)  # type: ignore[method-assign]
    for n in range(3):
        await push(relay, payload=500 + n)
    first = stream.flight.pictures[1].sent_at
    clock.now = first + 1.25
    clock.sleeps.clear()
    await push(relay, payload=600)
    # The fourth waited for the first only until the first was CARD_ACK_TIMEOUT_S old, not its own whole bound.
    assert pytest.approx(sc.CARD_ACK_TIMEOUT_S - 1.25) in clock.sleeps
    assert 1 not in stream.flight.pictures and late and late[0] >= sc.CARD_ACK_TIMEOUT_S - 1e-6
    assert len(relay.methods("Page.screencastFrameAck")) == 4


def test_a_lost_picture_given_up_wakes_the_wait_for_it() -> None:
    from kiro_crew.browser_harness.panel_pace import Flight

    stream = ps.PanelStream.__new__(ps.PanelStream)
    stream.flight = Flight()
    stream._flight_moved = asyncio.Event()
    stream._clock = lambda: 10.0  # type: ignore[method-assign]
    stream.pace = type("Pace", (), {"record": lambda *_a: None})()  # type: ignore[assignment]
    stream.flight.sent(1, 7.0, 100, measured=True)
    assert stream._expire_flight() is True and stream._flight_moved.is_set()
    stream._flight_moved.clear()
    assert stream._expire_flight() is False and not stream._flight_moved.is_set()


async def test_secret_fields_are_painted_over_on_every_picture_and_no_setting_turns_it_off() -> (
    None
):
    stream, relay, clock, sink = await make_stream()
    assert stream.config.mask_passwords is True
    relay.mask_boxes = [[100.0, 200.0, 300.0, 40.0]]
    await push(relay)
    assert sink.frames[-1][0]["mask"] == [[200, 400, 601, 81]]
    # The lossless still is masked too.
    relay.screenshot = make_png(2048, 1440)
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is True
    assert sink.frames[-1][0]["mask"] == [[200, 400, 601, 81]]


# ── the still ───────────────────────────────────────────────────────────────


async def test_a_page_that_stops_gets_one_lossless_picture_and_no_second_one() -> None:
    stream, relay, clock, sink = await make_stream(texels=1.4)
    # A pointer is on the page: its pictures are not held back, so the clock stands.
    pointer_on_from_the_start(stream)
    await push(relay, width=1434, height=1008)
    assert await stream.settle() is False  # still moving
    relay.screenshot = make_png(1434, 1008)
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is True
    # The still is the first frame of a PNG cast of its own, on a second session of the tab: a
    # capture would show what the browser draws over the page by itself, and no cast does.
    (shot,) = stills(relay)
    assert shot[1] == {"format": "png", "maxWidth": 1434, "maxHeight": 1008, "everyNthFrame": 1}
    assert shot[2] == "S2-T1" and stream.session_id == "S-T1"
    assert relay.methods("Page.captureScreenshot") == []
    # That cast is stopped once its frame is in, and the stream's own cast ran on beside it.
    assert [c[2] for c in relay.methods("Page.stopScreencast")] == ["S2-T1"]
    assert relay.casts == {"S-T1"}
    header, blob = sink.frames[-1]
    assert (header["w"], header["h"], header["seq"]) == (1434, 1008, 2)
    assert sc.decode_frame(blob)[1].startswith(b"\x89PNG")
    # The stream's own cast was never restarted for it, and nothing asks for another still:
    assert len(relay.methods("Page.startScreencast")) - len(stills(relay)) == 1
    clock.now += 60
    assert await stream.settle() is False
    # not even when the still makes Chromium repeat the frame it sent last.
    await push(relay, width=1434, height=1008)
    clock.now += 60
    assert await stream.settle() is False
    assert len(stills(relay)) == 1


async def test_a_frame_made_while_the_still_is_taken_sets_off_no_new_picture() -> None:
    # Below 2 picture pixels for a CSS pixel the browser takes the still at another
    # scale, and makes one screencast frame at that scale while it does. Sent on,
    # that frame and the one after it would each be a new picture, and the page
    # would get a still again every two seconds for ever.
    stream, relay, clock, sink = await make_stream(texels=0.75)
    stream.idle = False
    relay.screenshot = make_png(768, 540)
    await push(relay, payload=500, width=768, height=540)
    pending: list[asyncio.Future[None]] = []

    async def frame_at_the_stills_scale() -> None:
        pending.append(asyncio.ensure_future(push(relay, payload=900, width=768, height=540)))
        await asyncio.sleep(0)  # the frame's handler runs up to the stream's lock

    relay.on_screenshot = frame_at_the_stills_scale
    clock.now += sc.SETTLE_S + 0.1
    acks = len(relay.methods("Page.screencastFrameAck"))
    assert await stream.settle() is True
    await asyncio.wait_for(pending[0], 5)
    kinds = [
        "png" if sc.decode_frame(blob)[1].startswith(b"\x89PNG") else "jpeg"
        for _h, blob in sink.frames
    ]
    assert kinds == ["jpeg", "png"]
    assert len(relay.methods("Page.screencastFrameAck")) == acks + 1
    # The browser's next frame is the page as it was: the bytes that were sent last.
    relay.on_screenshot = None
    await push(relay, payload=500, width=768, height=540)
    clock.now += 60
    assert await stream.settle() is False
    assert len(sink.frames) == 2 and len(stills(relay)) == 1


async def test_a_still_made_from_another_view_is_not_sent_and_the_page_is_laid_out_again() -> None:
    # The frame of a still's cast names the view it was made from, as a moving picture's does:
    # a still is judged by that as well as by its shape.
    stream, relay, clock, sink = await make_stream()
    stream.idle = False
    await push(relay)
    laid = len(relay.methods("Emulation.setDeviceMetricsOverride"))
    relay.screenshot = make_png(2048, 1440)
    relay.still_view = {"deviceWidth": 1280, "deviceHeight": 900}
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is False
    assert len(sink.frames) == 1 and stream.misshapen == 1
    assert len(relay.methods("Emulation.setDeviceMetricsOverride")) == laid + 1
    relay.still_view = {"deviceWidth": 1024, "deviceHeight": 720}
    clock.now += sc.SETTLE_MIN_INTERVAL_S + 0.1
    assert await stream.settle() is True
    assert sc.decode_frame(sink.frames[-1][1])[1].startswith(b"\x89PNG")


async def test_a_still_whose_frame_does_not_come_is_not_sent_and_its_cast_is_stopped() -> None:
    stream, relay, clock, sink = await make_stream()
    pointer_on_from_the_start(stream)
    await push(relay)
    # The browser makes no frame for the still's cast: the wait is bounded, on the stream's own clock.
    forget = relay.attach

    async def attach_and_forget(target_id: str, on_event: Any) -> str:
        session = await forget(target_id, on_event)
        relay.second_sessions.pop(session, None)
        return session

    relay.attach = attach_and_forget  # type: ignore[method-assign]
    clock.now += sc.SETTLE_S + 0.1
    clock.sleeps.clear()
    assert await stream.settle() is False
    assert len(sink.frames) == 1 and ps.STILL_WAIT_S in clock.sleeps
    assert [c[2] for c in relay.methods("Page.stopScreencast")] == ["S2-T1"]


async def test_the_stills_own_session_is_let_go_when_the_stream_stops() -> None:
    stream, relay, clock, _sink = await make_stream()
    stream.idle = False
    await push(relay)
    relay.screenshot = make_png(2048, 1440)
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is True
    await stream.stop()
    assert relay.detached == ["S-T1", "S2-T1"]


async def test_a_page_that_changed_again_gets_its_next_still() -> None:
    stream, relay, clock, _sink = await make_stream()
    relay.screenshot = make_png(2048, 1440)
    await push(relay)
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is True
    await push(relay, payload=700)
    clock.now += sc.SETTLE_MIN_INTERVAL_S + 0.1
    assert await stream.settle() is True


async def test_a_still_waits_for_the_panels_ack_as_a_moving_picture_does() -> None:
    stream, relay, clock, sink = await make_stream(ack=False)
    stream.idle = False
    await push(relay)
    relay.screenshot = make_png(2048, 1440)
    clock.now += sc.SETTLE_S + 0.1
    clock.sleeps.clear()
    assert await stream.settle() is True
    assert len(sink.frames) == 2
    # No ack came for the still: it waited the whole bound before the stream went on.
    assert sc.CARD_ACK_TIMEOUT_S in clock.sleeps


async def test_a_stream_out_of_view_takes_no_still_even_while_its_cast_still_runs() -> None:
    stream, relay, clock, sink = await make_stream()
    stream.idle = False
    await push(relay)
    # The stop does not reach the browser: out of view, with a cast that runs.
    relay.lose_stops = 1
    with pytest.raises(asyncio.TimeoutError):
        await stream.set_visible(False)
    assert stream.casting and not stream.visible
    clock.now += sc.SETTLE_S + 0.1
    assert await stream.settle() is False
    assert stills(relay) == [] and len(sink.frames) == 1


async def test_no_still_goes_to_a_panel_that_left_the_view_while_it_was_taken() -> None:
    stream, relay, clock, sink = await make_stream()
    stream.idle = False
    await push(relay)
    relay.screenshot = make_png(2048, 1440)
    clock.now += sc.SETTLE_S + 0.1

    async def leave_the_view() -> None:
        await stream.set_visible(False)

    # The browser is taking the picture when the panel leaves the person's view.
    relay.on_screenshot = leave_the_view
    assert await stream.settle() is False
    assert len(stills(relay)) == 1
    assert not stream.visible and len(sink.frames) == 1


async def test_a_page_no_pointer_is_on_is_paced_at_the_idle_period() -> None:
    stream, relay, clock, _sink = await make_stream()
    assert stream.idle is True
    for payload in (500, 600, 700):
        await push(relay, payload=payload)
    assert any(abs(s - ps.IDLE_PERIOD_S) < 0.01 for s in clock.sleeps)
    stream.idle = False
    clock.sleeps.clear()
    for payload in (800, 900, 1000):
        await push(relay, payload=payload)
    assert clock.sleeps and all(s < ps.IDLE_PERIOD_S / 2 for s in clock.sleeps)


async def test_the_page_he_looks_at_gets_the_moving_pace_at_the_held_quality() -> None:
    """No pointer on it, and he looks at it: it is not held to the idle period, and its JPEG stays at 60."""
    stream, relay, clock, sink = await make_stream()
    stream.watched = True
    assert stream.held is False and stream.quality() == ps.HELD_QUALITY
    for payload in (500, 600, 700):
        await push(relay, payload=payload)
    assert clock.sleeps and all(s < ps.IDLE_PERIOD_S / 2 for s in clock.sleeps)
    assert sink.frames[-1][0]["held"] is False
    assert moving_starts(relay)[-1][1]["quality"] == ps.HELD_QUALITY
    # It leaves his look: held again, as a page no pointer is on.
    stream.watched = False
    clock.sleeps.clear()
    for payload in (800, 900, 1000):
        await push(relay, payload=payload)
    assert any(abs(s - ps.IDLE_PERIOD_S) < 0.01 for s in clock.sleeps)
    assert sink.frames[-1][0]["held"] is True


@pytest.mark.parametrize(
    ("capture", "link", "wait"),
    [(0.06, 0.0, 0.04), (0.05, 0.02, 0.03), (0.05, 0.3, 0.0)],
)
async def test_a_held_pages_period_counts_from_its_last_ack_to_the_browser(
    capture: float, link: float, wait: float
) -> None:
    """The capture, the send and the panel's decode run inside the period, not before it."""
    stream, relay, clock, sink = await make_stream(ack=False)

    async def over_the_link(header: Any, blob: bytes) -> None:
        sink.frames.append((header, blob))
        clock.now += link
        stream.ack(header["seq"])

    sink.frame = over_the_link  # type: ignore[method-assign]
    await push(relay, payload=500)
    clock.sleeps.clear()
    # The browser makes the next frame `capture` seconds after the ack that asked for it.
    clock.now += capture
    await push(relay, payload=600)
    assert len(sink.frames) == 2
    assert sum(clock.sleeps) == pytest.approx(wait)


async def test_a_page_no_pointer_is_on_moves_at_a_lower_quality_and_a_pointer_brings_it_back() -> (
    None
):
    stream, relay, _clock, sink = await make_stream()
    assert moving_starts(relay)[-1][1]["quality"] == ps.HELD_QUALITY
    await push(relay, payload=500)
    # A pointer comes onto the page: after the next picture the cast starts again at the full quality.
    stream.idle = False
    stream.hurry()
    await push(relay, payload=600)
    assert moving_starts(relay)[-1][1]["quality"] == ps.MOVING_QUALITY
    starts = len(moving_starts(relay))
    await push(relay, payload=700)
    assert len(moving_starts(relay)) == starts, "the same quality keeps its cast"
    # It leaves: the next picture brings the lower quality back.
    stream.idle = True
    await push(relay, payload=800)
    assert moving_starts(relay)[-1][1]["quality"] == ps.HELD_QUALITY
    assert len(sink.frames) == 4


def moving_starts(relay: PanelRelay) -> list[tuple[str, dict[str, Any], str | None]]:
    """Every start of the cast of moving pictures (a still's cast is a PNG one)."""
    return [c for c in relay.methods("Page.startScreencast") if c[1].get("format") == "jpeg"]


def counts(relay: PanelRelay) -> tuple[int, int, int]:
    """Stops, starts of the moving cast, and acks so far."""
    return (
        len(relay.methods("Page.stopScreencast")),
        len(moving_starts(relay)),
        len(relay.methods("Page.screencastFrameAck")),
    )


async def test_after_a_quiet_spell_an_idle_page_sends_one_picture_and_waits_a_whole_period() -> (
    None
):
    stream, relay, clock, sink = await make_stream()
    # Enough pages held at once that the wait is long enough to stop the cast for.
    stream.held_count = lambda: HELD_MANY
    clock.now += 30
    stops, starts, acks = counts(relay)
    await push(relay, payload=500)
    # The picture went out at once. For the wait the cast was stopped, and started
    # again after it: the start's picture is the next one, so nothing is acked.
    assert len(sink.frames) == 1
    assert clock.sleeps == [pytest.approx(PERIOD_MANY)]
    assert counts(relay) == (stops + 1, starts + 1, acks)
    assert stream.casting and not stream.holding
    await push(relay, payload=600)
    assert clock.sleeps == [pytest.approx(PERIOD_MANY)] * 2
    assert counts(relay) == (stops + 2, starts + 2, acks)


async def test_a_repeat_of_the_last_picture_is_acked_after_the_wait_with_the_cast_left_on() -> None:
    stream, relay, clock, sink = await make_stream()
    await push(relay, payload=500)
    stops, starts, acks = counts(relay)
    # The start's picture of a page that did not change: a page at rest costs the
    # page browser nothing with its cast on, and a stop would only bring another start.
    await push(relay, payload=500)
    assert len(sink.frames) == 1
    assert counts(relay) == (stops, starts, acks + 1)
    assert stream.casting


async def test_a_page_with_a_pointer_on_it_keeps_its_cast() -> None:
    stream, relay, _clock, sink = await make_stream()
    pointer_on_from_the_start(stream)
    stops, starts, acks = counts(relay)
    for payload in (500, 600, 700):
        await push(relay, payload=payload)
    assert len(sink.frames) == 3
    assert counts(relay) == (stops, starts, acks + 3)


async def test_a_page_in_view_with_no_pointer_gets_ten_pictures_a_second_and_the_held_share_thirty() -> (
    None
):
    stream, _relay, clock, _sink = await make_stream()
    assert stream.idle_period() == pytest.approx(0.1)
    stream.held_count = lambda: 3
    assert stream.idle_period() == pytest.approx(0.1)
    stream.held_count = lambda: 6
    assert stream.idle_period() == pytest.approx(0.2)
    # A page counts as held lately for a second after its last picture, while no pointer is on it.
    stream.visible, stream.idle = True, True
    stream.metrics.seq = 1
    stream._sent_at = clock.now
    assert stream.sent_lately(clock.now + 0.5) is True
    assert stream.sent_lately(clock.now + 1.5) is False
    stream.idle = False
    assert stream.sent_lately(clock.now + 0.5) is False


async def test_a_hand_that_comes_while_a_held_page_waits_for_its_picture_to_land_ends_that_wait_too() -> (
    None
):
    stream, relay, _clock, sink = await make_stream(ack=False)
    stream.held_count = lambda: HELD_MANY

    async def decoded_only(header: Any, blob: bytes) -> None:
        sink.frames.append((header, blob))
        stream.ack(header["seq"], "decoded")

    sink.frame = decoded_only  # type: ignore[method-assign]
    asleep, _never, naps = hold_forever(stream)
    stops, starts, acks = counts(relay)
    pushing = asyncio.ensure_future(push(relay, payload=500))
    await asyncio.wait_for(asleep.wait(), 5)
    # The first wait is the one for the picture to land; a hand comes 100 ms into it.
    assert naps == [pytest.approx(ps.LANDED_WAIT_S)]
    stream.idle = False
    stream.hurry()
    await asyncio.wait_for(pushing, 5)
    # Neither the landed wait nor the idle wait after it kept the page: its cast runs again at once.
    assert naps == [pytest.approx(ps.LANDED_WAIT_S)]
    assert counts(relay)[1] == starts + 1 and stream.casting and not stream.holding
    assert counts(relay)[0] == stops + 1 and counts(relay)[2] == acks


def hold_forever(stream: ps.PanelStream) -> tuple[asyncio.Event, asyncio.Event, list[float]]:
    """The idle wait becomes a sleep that never ends by itself; what the stream sleeps after it ends at once."""
    asleep, never = asyncio.Event(), asyncio.Event()
    naps: list[float] = []

    async def sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 1:
            asleep.set()
            await never.wait()

    stream._sleep = sleep
    return asleep, never, naps


@pytest.mark.parametrize("how", ["a hand comes onto the page", "an input is sent to the page"])
async def test_an_idle_wait_ends_the_moment_a_hand_is_there(how: str) -> None:
    stream, relay, _clock, _sink = await make_stream()
    # Enough pages held at once that the wait is long enough to stop the cast for.
    stream.held_count = lambda: HELD_MANY
    assert stream.idle is True
    asleep, _never, naps = hold_forever(stream)
    stops, starts, acks = counts(relay)
    pushing = asyncio.ensure_future(push(relay, payload=500))
    await asyncio.wait_for(asleep.wait(), 5)
    # The picture is out, and the cast is stopped: the page counts as shown.
    assert counts(relay) == (stops + 1, starts, acks)
    assert stream.holding and not stream.casting and stream.visible
    if how == "a hand comes onto the page":
        stream.idle = False
        stream.hurry()
    else:
        stream.note_input()
    await asyncio.wait_for(pushing, 5)
    # The cast starts at once: its first picture is the page as it is now.
    assert counts(relay) == (stops + 1, starts + 1, acks)
    assert stream.casting and not stream.holding
    assert naps[0] == pytest.approx(PERIOD_MANY)


async def test_a_panel_that_leaves_the_view_in_the_wait_gets_no_cast_after_it() -> None:
    stream, relay, _clock, _sink = await make_stream()
    asleep, never, _naps = hold_forever(stream)
    pushing = asyncio.ensure_future(push(relay, payload=500))
    await asyncio.wait_for(asleep.wait(), 5)
    await stream.set_visible(False)
    _stops, starts, _acks = counts(relay)
    never.set()
    await asyncio.wait_for(pushing, 5)
    assert len(moving_starts(relay)) == starts
    assert not stream.casting and not stream.holding
    # Back in view, it is started as any panel that comes back.
    await stream.set_visible(True)
    assert stream.casting and len(moving_starts(relay)) == starts + 1


async def test_a_start_that_fails_after_the_wait_leaves_a_page_the_hub_starts_again() -> None:
    stream, relay, _clock, _sink = await make_stream()
    # Enough pages held at once that the wait is long enough to stop the cast for.
    stream.held_count = lambda: HELD_MANY
    asleep, never, _naps = hold_forever(stream)
    pushing = asyncio.ensure_future(push(relay, payload=500))
    await asyncio.wait_for(asleep.wait(), 5)
    # The browser starts the cast, and its answer never arrives.
    relay.lose_starts = 1
    never.set()
    await asyncio.wait_for(pushing, 5)
    # Not holding and not casting while in view: the hub's look shows it again
    # (``PanelHub._show``), and that start, refused while the lost one runs, is
    # asked again behind a stop.
    assert stream.visible and not stream.casting and not stream.holding
    stream.visible = False
    await stream.set_visible(True)
    assert stream.casting and relay.casts == {stream.session_id}


async def test_page_events_reach_the_hub_and_frames_do_not() -> None:
    seen: list[str] = []

    async def on_event(msg: dict[str, Any]) -> None:
        seen.append(msg["method"])

    relay = PanelRelay()
    clock = FakeClock()
    stream = ps.PanelStream(
        relay,
        "T1",
        RecordingSink(),
        panel_id="a",
        css_w=1024,
        css_h=720,
        on_page_event=on_event,
        clock=clock,
        sleep=clock.sleep,
        auto_control=False,
    )
    await stream.start()
    await relay.push("T1", "Page.loadEventFired", {})
    await push(relay)
    assert seen == ["Page.loadEventFired"]


# ── the page: addresses and scripts ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        (ORIGIN + "/chat", True),
        (ORIGIN + "/chat/abc?x=1", True),
        (ORIGIN + "/settings/browser", True),
        (ORIGIN + "/artifacts/report-1", True),
        (ORIGIN + "/members", True),
        (ORIGIN + "/artifacts", True),
        (ORIGIN + "/capabilities", False),
        (ORIGIN + "/artifacts/deploy", False),
        (ORIGIN + "/settings/people", False),
        (ORIGIN + "/artifacts/a/b", False),
        (ORIGIN + "/", False),
        ("http://localhost:6777/chat", False),
        ("http://127.0.0.1:6778/chat", False),
        ("https://127.0.0.1:6777/chat", False),
        ("http://user:pw@127.0.0.1:6777/chat", False),
        ("http://127.0.0.1:6777.evil.example/chat", False),
        ("javascript:alert(1)", False),
        ("data:text/html,x", False),
        ("about:blank", False),
        ("http://127.0.0.1:notaport/chat", False),
        (None, False),
    ],
)
async def test_the_address_rule(url: Any, ok: bool) -> None:
    assert panel_page.allowed_url(url, ORIGIN) is ok


async def test_the_address_rule_holds_on_the_ipv6_loopback_too() -> None:
    origin = "http://[::1]:6777"
    assert panel_page.allowed_url(origin + "/chat/abc", origin) is True
    assert panel_page.allowed_url("http://127.0.0.1:6777/chat", origin) is False
    assert panel_page.allowed_url(origin + "/capabilities", origin) is False


@pytest.mark.parametrize(
    ("url", "launch"),
    [
        (ORIGIN + "/chat", False),
        (ORIGIN + "/chat?new=1", False),
        (ORIGIN + "/chat?tokens=1&auto=1", False),
        (ORIGIN + "/chat#token=abc", False),
        (ORIGIN + "/chat?token=abc", True),
        (ORIGIN + "/chat?token", True),
        (ORIGIN + "/chat?token=", True),
        (ORIGIN + "/chat?x=1&autoSend=1", True),
        (ORIGIN + "/chat?AUTOSEND=0", True),
        (ORIGIN + "/chat?%74oken=abc", True),
        (ORIGIN + "/chat?a=b&+token+=abc", True),
        # The Task Runner executes a planned run for this one, with no press.
        (ORIGIN + "/projects?applied=t-1&autoRun=true", True),
        (ORIGIN + "/projects?autoRun=true", True),
        (ORIGIN + "/projects?AUTORUN=true", True),
        (ORIGIN + "/projects?auto%52un=true", True),
        (ORIGIN + "/projects?autoRun=false&autoRun=true", True),
        # By itself `applied` only selects a run.
        (ORIGIN + "/projects?applied=t-1", False),
        # The page reads its query as the browser splits it, at `&` alone: a name after a `;` is
        # part of the value before it, and the page does not see it. Nor does it read the part after `#`.
        (ORIGIN + "/projects?applied=t-1;autoRun=true", False),
        (ORIGIN + "/projects?applied=t-1#autoRun=true", False),
        (ORIGIN + "/projects#?autoRun=true", False),
        ("http://[::1:6777/chat", True),
        (None, True),
    ],
)
async def test_an_address_that_makes_the_dashboard_act_as_it_loads(url: Any, launch: bool) -> None:
    assert panel_page.carries_launch(url) is launch


@pytest.mark.parametrize(
    ("url", "left_out"),
    [
        (ORIGIN + "/settings?tab=connections", True),
        (ORIGIN + "/settings?tab=people", True),
        (ORIGIN + "/settings?tab=instances", True),
        (ORIGIN + "/settings?tab=%63onnections", True),
        (ORIGIN + "/settings?tab=peopl%65", True),
        (ORIGIN + "/settings?q=1&tab=inst%61nces", True),
        # Given twice, every value must pass, whichever the page would take.
        (ORIGIN + "/settings?tab=browser&tab=connections", True),
        (ORIGIN + "/settings?tab=people&tab=browser", True),
        (ORIGIN + "/settings?TAB=connections", True),
        (ORIGIN + "/settings?+tab+=connections", True),
        # Only a value that the list lets through as a tab's path passes.
        (ORIGIN + "/settings?tab=connections/x", True),
        (ORIGIN + "/settings?tab=Connections", True),
        (ORIGIN + "/settings?tab=..", True),
        (ORIGIN + "/settings?tab=", True),
        (ORIGIN + "/settings?tab", True),
        (ORIGIN + "/settings?tab=browser", False),
        (ORIGIN + "/settings?tab=agent&tab=browser", False),
        (ORIGIN + "/settings", False),
        (ORIGIN + "/settings?q=connections", False),
        (ORIGIN + "/settings?tabs=connections", False),
        # The path's tab wins, and no other page takes its tab from the query in place of a path.
        (ORIGIN + "/settings/x?tab=connections", False),
        (ORIGIN + "/settings/browser?tab=people", False),
        (ORIGIN + "/chat?tab=connections", False),
        # The page splits at `&` alone and does not read the part after `#`.
        (ORIGIN + "/settings?q=1;tab=connections", False),
        (ORIGIN + "/settings#tab=connections", False),
        ("http://[::1:6777/settings", True),
        (None, True),
    ],
)
async def test_a_settings_tab_that_the_list_leaves_out_is_not_asked_for_by_the_query(
    url: Any, left_out: bool
) -> None:
    assert panel_page.names_unlisted_tab(url) is left_out
    assert panel_page.query_refused(url) is left_out


async def test_the_one_question_of_a_query_is_both_rules() -> None:
    assert panel_page.query_refused(ORIGIN + "/projects?applied=t-1&autoRun=true")
    assert panel_page.query_refused(ORIGIN + "/settings?tab=people")
    assert not panel_page.query_refused(ORIGIN + "/projects?applied=t-1")
    assert not panel_page.query_refused(ORIGIN + "/settings?tab=browser")


async def test_the_move_guard_carries_the_gateways_own_list_and_its_longest_path() -> None:
    script = panel_page.move_guard_script()
    for shape in panel_page.PATH_SHAPES:
        assert json.dumps(shape.pattern) in script
    assert script.endswith(panel_page.MOVE_JS)
    # And the names of the parameters that act, as the gateway holds them.
    assert json.dumps(sorted(panel_page.LAUNCH_PARAMS)) in script
    assert panel_page.LAUNCH_PARAMS == {"token", "autosend", "autorun"}
    assert "(globalThis.__kcPanelLaunch||[])" in panel_page.MOVE_JS
    assert "u.pathname!=='/settings'" in panel_page.MOVE_JS and "==='tab'&&" in panel_page.MOVE_JS
    assert f"p.length<={panel_page.MAX_PATH_CHARS}&&" in panel_page.MOVE_JS
    # With no list in front of it, every pattern is missing: no move passes.
    assert "(globalThis.__kcPanelShapes||[])" in panel_page.MOVE_JS


@pytest.mark.parametrize(
    ("path", "looking"),
    [
        ("/projects", True),
        ("/projects/", True),
        ("/projects/p1", True),
        ("/projects/p1/tasks/t-2", True),
        ("/tasks", True),
        ("/schedule", True),
        ("/schedule/nightly", True),
        # A path that is not known, or is off the list, counts as one.
        ("", True),
        (None, True),
        ("/", True),
        ("/capabilities", True),
        ("/projectsx", True),
        ("/chat", False),
        ("/chat/dashboard-ab12", False),
        ("/members", False),
        ("/notifications", False),
        ("/settings/browser", False),
        ("/artifacts/report-1", False),
    ],
)
async def test_the_pages_that_a_panel_shows_for_looking_at_only(path: Any, looking: bool) -> None:
    assert panel_page.look_only(path) is looking


async def test_every_page_for_looking_at_is_one_that_a_panel_may_show() -> None:
    for path in ("/projects", "/projects/p1/tasks/t-2", "/tasks", "/schedule"):
        assert panel_page.allowed_path(path) == path and panel_page.look_only(path)


async def test_the_path_a_panel_tells_is_the_path_alone() -> None:
    assert panel_page.path_of(ORIGIN + "/chat/abc?sid=1#row") == "/chat/abc"
    assert panel_page.path_of(ORIGIN) == "/"
    assert panel_page.path_of(None) == "" and panel_page.path_of("http://[::1:6777/chat") == ""


@pytest.mark.parametrize(
    ("host", "port", "origin"),
    [
        ("127.0.0.1", 6777, "http://127.0.0.1:6777"),
        ("127.0.0.9", 6777, "http://127.0.0.9:6777"),
        ("::1", 6777, "http://[::1]:6777"),
        ("10.0.0.5", 6777, None),
        ("0.0.0.0", 6777, None),
        ("::", 6777, None),
        ("localhost", 6777, None),
        ("127.0.0.1", 0, None),
        ("127.0.0.1", 70000, None),
        ("127.0.0.1", True, None),
        (None, 6777, None),
    ],
)
async def test_a_tabs_origin_is_the_gateways_own_loopback_address(
    host: Any, port: Any, origin: str | None
) -> None:
    assert panel_page.loopback_origin(host, port) == origin


async def test_prefs_are_bounded_and_cannot_break_out_of_their_script() -> None:
    prefs = panel_page.clean_prefs(
        {"theme": "dark", "big": "x" * 20_000, 7: "n", "obj": {"a": 1}, "tag": "</script><b>"}
    )
    assert prefs == {"theme": "dark", "tag": "</script><b>"}
    assert "</script>" not in panel_page.prefs_script(prefs)
    assert panel_page.clean_prefs("theme=dark") == {}
    many = panel_page.clean_prefs({f"k{n}": "v" for n in range(1000)})
    assert len(many) == panel_page.MAX_PREF_KEYS


async def test_the_guard_reads_the_dashboards_two_marks_and_stops_trusted_presses() -> None:
    js = panel_page.GUARD_JS
    assert "data-agent-grant" in js and "data-guide-trust-root" in js
    assert "isTrusted" in js and "stopImmediatePropagation" in js
    for event in ("pointerdown", "mousedown", "click", "keydown"):
        assert f"'{event}'" in js
    # Text is inserted with no key event, and a paste or a drop is neither a press nor a key:
    # each is stopped at the field's own event.
    for event in ("beforeinput", "paste", "drop"):
        assert f"'{event}'" in js
    # The focus reader answers with the guard's own walk, from the element that holds the focus.
    assert "guarded:guarded(e)" in js
    # Nothing in it reads what the page holds as data.
    for word in ("cookie", "localStorage", "fetch(", "XMLHttpRequest", ".value"):
        assert word not in js


async def test_the_state_answer_says_where_the_marked_controls_are_drawn_in_both_ways() -> None:
    js = panel_page.GUARD_JS
    reader = js[js.index("const grants=") : js.index("const state=")]
    # Every element that carries the grant mark, whatever the mark says: Allow once says yes, Reject says no.
    assert "document.querySelectorAll('['+GRANT+'],['+ANSWER+']')" in reader
    assert "getAttribute" not in reader
    # Only what is drawn in the window, as rounded rectangles, and a bounded count of them.
    assert (
        "r.width<2||r.height<2" in reader
        and "r.top>=innerHeight" in reader
        and "r.left>=innerWidth" in reader
    )
    assert "checkVisibility" in reader
    assert "out.push(box(r));if(out.length>=40)break;" in reader
    # The same with the guard on and off: the word that takes the guard off is not read here.
    assert "OFF" not in reader
    assert "grants:grants()};};" in js[js.index("const state=") : js.index("let still=")]
    assert panel_page.guard_script(guarded=False).endswith(js)
    # The hub hands the page's answer on as it is.
    hub, browser, _clock = make_hub()
    client = Client(1, "owner-session-token-A")
    panel = await open_live(hub, browser, client)
    places = [[30, 649, 96, 29], [132, 649, 78, 29], [216, 649, 85, 29]]
    browser.relay.state_answer = {
        "path": "/members",
        "words": 120,
        "scrollMax": 0,
        "grants": places,
    }
    assert (await hub.ask(client.socket.token, panel.handle, "state"))["grants"] == places


async def test_the_controls_that_answer_an_ask_include_a_question_cards_own_two() -> None:
    js = panel_page.GUARD_JS
    reader = js[js.index("const grants=") : js.index("const state=")]
    assert panel_page.ANSWER_ATTR == "data-xr-answer"
    assert "document.querySelectorAll('['+GRANT+'],['+ANSWER+']')" in reader
    # The guard does not read the XR page's own mark: whether an answer grants something is not its word.
    guard = js[js.index("const guarded=") : js.index("const at=")]
    assert "ANSWER" not in guard
    # The question card's Dismiss and Submit carry it, and an option does not.
    card = (REPO_ROOT / "website/src/components/QuestionCard.tsx").read_text(encoding="utf-8")
    assert card.count('data-xr-answer=""') == 2
    dismiss = card[
        card.index("onClick={onDismiss}") - 200 : card.index("onClick={onDismiss}") + 400
    ]
    submit = card[
        card.index("onClick={handleSubmit}") - 200 : card.index("onClick={handleSubmit}") + 400
    ]
    assert 'data-xr-answer=""' in dismiss and 'data-xr-answer=""' in submit


REPO_ROOT = Path(__file__).resolve().parents[1]


def _press_with(places: Any) -> dict[str, Any]:
    return {"type": "mouse", "action": "click", "x": 78, "y": 664, "grants": places}


CARD = [[30, 649, 96, 29], [132, 649, 78, 29], [216, 649, 85, 29]]


async def test_a_press_that_carries_the_places_the_page_still_has_is_sent() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, "owner-session-token-A")
    panel = await open_live(hub, browser, client)
    browser.relay.state_answer = {"path": "/members", "words": 120, "grants": CARD}
    out = await hub.input(client.socket.token, panel.handle, _press_with(CARD))
    assert out["sent"] > 0 and "moved" not in out
    assert any(p.get("type") == "mousePressed" for _, p in browser.relay.dispatched())


async def test_a_press_is_not_sent_when_the_controls_moved_since_the_xr_page_saw_them() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, "owner-session-token-A")
    panel = await open_live(hub, browser, client)
    # The page drew a card where the XR page last saw only the message box.
    browser.relay.state_answer = {"path": "/members", "words": 140, "grants": CARD}
    out = await hub.input(client.socket.token, panel.handle, _press_with([[14, 649, 362, 40]]))
    assert out == {"sent": 0, "moved": True, "grants": CARD}
    assert browser.relay.dispatched() == []
    # Gone, too: a press that carries the card's places when the page has none is not sent either.
    browser.relay.state_answer = {"path": "/members", "words": 140, "grants": []}
    out = await hub.input(client.socket.token, panel.handle, _press_with(CARD))
    assert out == {"sent": 0, "moved": True, "grants": []}
    # A page that cannot say where they are is not sent a press blind.
    browser.relay.state_answer = {"path": "/members", "words": 140}
    out = await hub.input(client.socket.token, panel.handle, _press_with(CARD))
    assert out == {"sent": 0, "unread": True}
    assert browser.relay.dispatched() == []


async def test_a_release_and_a_press_with_no_places_are_sent_as_before_with_no_look() -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, "owner-session-token-A")
    panel = await open_live(hub, browser, client)
    browser.relay.state_answer = {"path": "/members", "words": 140, "grants": CARD}
    asked = len(browser.relay.methods("Runtime.evaluate"))
    out = await hub.input(
        client.socket.token,
        panel.handle,
        {"type": "mouse", "action": "up", "x": 78, "y": 664, "grants": []},
    )
    assert "moved" not in out
    out = await hub.input(
        client.socket.token, panel.handle, {"type": "mouse", "action": "click", "x": 78, "y": 664}
    )
    assert out["sent"] > 0
    questions = [p["expression"] for _, p, _ in browser.relay.methods("Runtime.evaluate")[asked:]]
    assert not any(".state()" in q for q in questions)


@pytest.mark.parametrize(
    "places",
    [
        "[[1,2,3,4]]",
        [[1, 2, 3]],
        [[1, 2, 3, "4"]],
        [[1, 2, 3, True]],
        [[1, 2, 3, float("inf")]],
        [1, 2, 3, 4],
        [[0, 0, 1, 1]] * 41,
        {"a": 1},
    ],
)
async def test_places_that_are_not_a_list_of_four_numbers_are_refused(places: Any) -> None:
    hub, browser, _clock = make_hub()
    client = Client(1, "owner-session-token-A")
    panel = await open_live(hub, browser, client)
    with pytest.raises(InputEventError):
        await hub.input(client.socket.token, panel.handle, _press_with(places))
    assert browser.relay.dispatched() == []


async def test_the_question_under_a_press_says_when_the_browsers_own_picker_is_open() -> None:
    js = panel_page.GUARD_JS
    # A select or an input that is open, asked of the element with the focus and of the document.
    assert js.count("'select:open,input:open'") == 2
    # A select whose list the panel's sheet draws in the page is not one: its list is in the page.
    # The control and its picker are both read: a page that takes the sheet's appearance from
    # either of them gets the browser's own list back.
    assert "const inPage=(e,part)=>getComputedStyle(e,part).appearance==='base-select';" in js
    assert (
        "const drawnOutside=e=>e.tagName==='INPUT'||!inPage(e)||!inPage(e,'::picker(select)');"
        in js
    )
    # The answer is an answer of its own, before the page is asked what lies under the point.
    assert "if(picker())return {rect:[0,0,0,0],kind:'',label:'',guarded:false,picker:true};" in js
    assert js.index("if(picker())return") < js.index("const el=deep(x,y)")
    # A browser that does not know ``:open`` throws at the selector: the answer is then "none
    # is open", and a press is read as on any other page.
    assert "catch(_){return false;}};" in js[js.index("const picker=") : js.index("const target=")]


async def test_every_question_first_puts_the_panels_sheet_back() -> None:
    # A page's script can empty ``document.adoptedStyleSheets``, and every secret field is then
    # drawn as the page draws it. The sheet is adopted again in front of each question.
    keep = "globalThis.__kcCoverKeep&&globalThis.__kcCoverKeep()"
    assert panel_page.KEEP_SHEET_JS == keep
    for question in ("target", "focus", "state"):
        expression = panel_page.question_js(question, 1, 2)
        assert expression is not None and expression.startswith(f"({keep},globalThis.__kcPanel?")
    # What the hub asks the tab is that expression, in the panel's own world.
    hub, browser, _clock = make_hub()
    client = Client(1, "owner-session-token-A")
    panel = await open_live(hub, browser, client)
    await hub.ask(client.socket.token, panel.handle, "focus")
    asked = [p["expression"] for _, p, _ in browser.relay.methods("Runtime.evaluate")]
    assert asked[-1] == panel_page.question_js("focus")


async def test_only_three_questions_have_an_expression() -> None:
    # ``false`` when the reader is not in the page: not the same as "nothing here" (``null``).
    assert panel_page.question_js("target", 10, 20).endswith("target(10.0,20.0):false)")
    assert panel_page.question_js("focus").endswith("focus():false)")
    assert panel_page.question_js("focus") and panel_page.question_js("state")
    assert panel_page.question_js("cookies") is None
