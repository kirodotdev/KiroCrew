"""The live stream: wire shape, the ladder on a fake clock, backpressure, watchdog, mask."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from browser_live_helpers import (
    CHROME_SESSION,
    SESSION,
    FakeRelay,
    frame_event,
    make_jpeg,
    make_stream,
)

from kiro_crew.browser_harness import screencast as sc

# ── wire shape ─────────────────────────────────────────────────────────────


def test_frame_header_round_trips_through_the_binary_message() -> None:
    header = sc.frame_header("chat-1", 7, 1280, 768, [[10, 20, 30, 40]], "s1")
    blob = sc.encode_frame(header, b"\xff\xd8jpeg")
    assert int.from_bytes(blob[:4], "big") == len(blob) - 4 - len(b"\xff\xd8jpeg")
    decoded, jpeg = sc.decode_frame(blob)
    assert decoded == {
        "type": "browser_frame",
        "session_key": "chat-1",
        "stream": "s1",
        "seq": 7,
        "w": 1280,
        "h": 768,
        "mask": [[10, 20, 30, 40]],
    }
    assert jpeg == b"\xff\xd8jpeg"


def test_header_length_prefix_is_four_byte_big_endian() -> None:
    blob = sc.encode_frame({"a": 1}, b"")
    assert blob[:4] == b"\x00\x00\x00\x07"
    assert blob[4:] == b'{"a":1}'


@pytest.mark.parametrize(
    "blob",
    [b"", b"\x00\x00", b"\x00\x00\x00\x09{}", b"\xff\xff\xff\xff{}", b"\x00\x00\x00\x02[]"],
)
def test_decode_refuses_short_oversized_or_non_object_headers(blob: bytes) -> None:
    with pytest.raises(ValueError):
        sc.decode_frame(blob)


def test_jpeg_size_reads_the_first_sof_segment() -> None:
    assert sc.jpeg_size(make_jpeg(640, 384)) == (640, 384)
    assert sc.jpeg_size(b"not a jpeg") is None
    assert sc.jpeg_size(b"\xff\xd8\xff\xd9") is None


def test_the_ladder_is_fps_steps_times_pictures() -> None:
    assert sc.FPS_STEPS == (60, 45, 30, 20, 15, 10, 5)
    assert [(p.quality, p.width) for p in sc.PICTURES] == [
        (60, 1280),
        (45, 960),
        (40, 800),
        (30, 640),
    ]
    assert len(sc.LADDER) == len(sc.FPS_STEPS) * len(sc.PICTURES)
    assert sc.LADDER[0] == sc.Rung(60, 60, 1280)
    assert sc.LADDER[sc.rung_index(2, 6)] == sc.Rung(5, 40, 800)
    # Takeover caps the frame rate only: the picture is the sharpest one.
    assert sc.LADDER[sc.TAKEOVER_RUNG] == sc.Rung(15, 60, 1280)


def test_live_config_reads_browser_live_with_defaults() -> None:
    assert sc.LiveConfig.from_config(SimpleNamespace()) == sc.LiveConfig()
    cfg = SimpleNamespace(
        browser=SimpleNamespace(
            live=SimpleNamespace(enabled=False, quality="low", max_fps=30, mask_passwords=False)
        )
    )
    live = sc.LiveConfig.from_config(cfg)
    assert live == sc.LiveConfig(enabled=False, quality="low", max_fps=30, mask_passwords=False)
    # max_fps 30 floors the frame rate at its step; quality "low" floors the picture.
    assert (live.fps_floor(), live.picture_floor()) == (2, 2)
    assert live.best_rung() == sc.rung_index(2, 2) and live.settles is False
    assert sc.LiveConfig(max_fps=15).fps_floor() == sc.TAKEOVER_FPS_STEP
    assert sc.LiveConfig().settles and sc.LiveConfig(quality="high").settles
    assert not sc.LiveConfig(quality="medium").settles
    bad = SimpleNamespace(browser=SimpleNamespace(live=SimpleNamespace(max_fps=7, quality="x")))
    assert sc.LiveConfig.from_config(bad) == sc.LiveConfig()


# ── the controller, pure ───────────────────────────────────────────────────


def _feed(ctrl: sc.AdaptiveController, frames: int, nbytes: int, delivery: float) -> None:
    for _ in range(frames):
        ctrl.observe(nbytes, delivery)


def test_controller_holds_the_top_rung_on_a_fast_link() -> None:
    ctrl = sc.AdaptiveController()
    _feed(ctrl, 60, 40_000, 0.002)
    stats = ctrl.tick()
    assert stats.rung == 0 and stats.fps == 60 and stats.late_share == 0


def test_late_frames_on_a_fast_link_lower_the_fps_one_step_and_keep_the_picture() -> None:
    ctrl = sc.AdaptiveController()
    period = sc.LADDER[0].period
    # 15 of 60 late (25%) on a link that is otherwise fast: one fps step down.
    _feed(ctrl, 45, 40_000, 0.002)
    _feed(ctrl, 15, 40_000, period * 2)
    ctrl.tick()
    assert (ctrl.picture, ctrl.fps_step) == (0, 1)
    # 10 of 60 late (17%): calm, holds.
    _feed(ctrl, 50, 40_000, 0.002)
    _feed(ctrl, 10, 40_000, period * 3)
    ctrl.tick()
    assert (ctrl.picture, ctrl.fps_step) == (0, 1)


def test_a_slow_link_costs_frame_rate_and_leaves_the_picture() -> None:
    ctrl = sc.AdaptiveController()
    # 40 kB frames taking 50 ms each: an 800 kB/s link. 60 fps would need 2.4 MB/s.
    _feed(ctrl, 5, 40_000, 0.05)
    ctrl.tick()
    assert ctrl.picture == 0
    assert sc.FPS_STEPS[ctrl.fps_step] == 15  # 600 kB/s fits 80% of the link; 20 fps would not


def test_a_bursty_high_rtt_link_never_changes_the_picture() -> None:
    """The remote case: every agent step is a burst of frames over a 150 ms link.

    Each burst is late at any rate the ack round trip cannot carry, and each
    pause between steps is a window with no frame. The frame rate settles low;
    the picture never moves, so nothing restarts and nothing goes soft.
    """
    ctrl = sc.AdaptiveController()
    pictures = set()
    for _step in range(20):
        # A 2 Mbit/s link with 150 ms round trip: a 40 kB frame takes ~0.31 s.
        _feed(ctrl, 3, 40_000, 0.15 + 40_000 / 250_000)
        ctrl.tick()
        pictures.add(ctrl.picture)
        for _idle in range(3):  # the agent thinks: no frames at all
            ctrl.tick()
            pictures.add(ctrl.picture)
    assert pictures == {0}
    assert sc.FPS_STEPS[ctrl.fps_step] == 5


def test_an_idle_window_is_no_evidence_and_changes_nothing() -> None:
    ctrl = sc.AdaptiveController()
    ctrl.restore(1, 5)
    for _ in range(10):
        stats = ctrl.tick()
        assert stats.frames == 0
    assert (ctrl.picture, ctrl.fps_step, ctrl.calm) == (1, 5, 0)


def test_the_picture_drops_only_after_two_heavy_windows_at_the_lowest_fps() -> None:
    ctrl = sc.AdaptiveController()
    # 40 kB frames taking 0.8 s each: under 2 frames a second even at 5 fps.
    _feed(ctrl, 2, 40_000, 0.8)
    ctrl.tick()
    # Frame rate first; one heavy window at the bottom is not enough to move the picture.
    assert (ctrl.picture, sc.FPS_STEPS[ctrl.fps_step]) == (0, 5)
    _feed(ctrl, 2, 40_000, 0.8)
    ctrl.tick()
    assert ctrl.picture > 0  # the second in a row: the picture gives way


def test_one_burst_between_good_windows_does_not_move_the_picture() -> None:
    ctrl = sc.AdaptiveController()
    ctrl.restore(0, len(sc.FPS_STEPS) - 1)
    for _ in range(5):
        _feed(ctrl, 2, 40_000, 0.8)  # squeezed
        ctrl.tick()
        _feed(ctrl, 2, 40_000, 0.05)  # fits again
        ctrl.tick()
    assert ctrl.picture == 0


def test_fps_climbs_after_two_calm_windows_and_the_picture_after_five() -> None:
    ctrl = sc.AdaptiveController()
    ctrl.restore(1, 5)
    # Very fast link: 15 kB in 1 ms.
    _feed(ctrl, 10, 15_000, 0.001)
    ctrl.tick()
    assert (ctrl.picture, ctrl.fps_step) == (1, 5)  # calm 1
    _feed(ctrl, 10, 15_000, 0.001)
    ctrl.tick()
    assert (ctrl.picture, ctrl.fps_step) == (1, 0)  # calm 2: fps straight up
    for _ in range(sc.PICTURE_UP_CALM - 2):
        _feed(ctrl, 10, 15_000, 0.001)
        ctrl.tick()
    assert ctrl.picture == 0  # calm 5: one picture step


def test_fps_climbs_only_as_far_as_the_link_covers() -> None:
    ctrl = sc.AdaptiveController()
    ctrl.restore(0, 6)
    # 20 kB frames taking 20 ms: a 1 MB/s link. With 30% headroom it covers 30 fps.
    for _ in range(2):
        _feed(ctrl, 5, 20_000, 0.02)
        ctrl.tick()
    assert sc.FPS_STEPS[ctrl.fps_step] == 30


def test_controller_floors_clamp_the_position() -> None:
    ctrl = sc.AdaptiveController()
    assert ctrl.set_floors(fps_floor=sc.TAKEOVER_FPS_STEP) == sc.TAKEOVER_RUNG
    for _ in range(2):
        _feed(ctrl, 15, 10_000, 0.001)
        ctrl.tick()
    assert ctrl.rung == sc.TAKEOVER_RUNG  # never climbs past the floor
    assert ctrl.set_floors(fps_floor=0) == sc.TAKEOVER_RUNG
    assert ctrl.set_floors(picture_floor=2) == sc.rung_index(2, sc.TAKEOVER_FPS_STEP)
    assert ctrl.restore(0, 0) == sc.rung_index(2, 0)


def test_link_memory_is_per_relay_and_card_and_expires() -> None:
    mem = sc.LinkMemory()
    relay_a, relay_b = FakeRelay(), FakeRelay()
    mem.remember(relay_a, "chat-1", 100.0, 1, 6)
    assert mem.recall(relay_a, "chat-1", 101.0) == (1, 6)
    assert mem.recall(relay_a, "chat-2", 101.0) is None
    assert mem.recall(relay_b, "chat-1", 101.0) is None
    assert mem.recall(relay_a, "chat-1", 100.0 + sc.LINK_MEMORY_S + 1) is None
    mem.remember(relay_a, "", 100.0, 1, 6)  # a stream with no card is not remembered
    assert mem.recall(relay_a, "", 100.0) is None


# ── the stream against a fake relay and clock ──────────────────────────────


@pytest.mark.asyncio
async def test_start_attaches_a_relay_session_and_starts_at_the_top_rung() -> None:
    stream, relay, _clock, _sink = make_stream()
    await stream.start()
    assert relay.attached == ["target-1"] and stream.session_id == SESSION
    assert relay.calls[0] == ("Page.enable", {}, SESSION)
    # The tab's cross-origin iframes attach to this session too (browser_harness.frames).
    method, params, sid = relay.calls[1]
    assert (method, sid) == ("Target.setAutoAttach", SESSION)
    assert params["flatten"] is True and params["waitForDebuggerOnStart"] is False
    method, params, sid = relay.calls[2]
    assert method == "Page.startScreencast" and sid == SESSION
    assert params == {
        "format": "jpeg",
        "quality": 60,
        "maxWidth": 1280,
        "maxHeight": 768,
        "everyNthFrame": 1,
    }
    await stream.stop()
    assert relay.methods("Page.stopScreencast") and relay.detached == [SESSION]


@pytest.mark.asyncio
async def test_a_frame_reaches_the_sink_then_chrome_is_acked_on_the_grid() -> None:
    stream, relay, clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    jpeg = make_jpeg(1280, 768)
    for _ in range(3):
        await relay.on_event(frame_event(jpeg))
    assert [h["seq"] for h, _ in sink.frames] == [1, 2, 3]
    header, blob = sink.frames[0]
    assert header == {
        "type": "browser_frame",
        "session_key": "chat-1",
        "stream": stream.stream_id,
        "seq": 1,
        "w": 1280,
        "h": 768,
        "mask": [],
    }
    assert len(stream.stream_id) == 8
    assert sc.decode_frame(blob)[1] == jpeg
    acks = relay.methods("Page.screencastFrameAck")
    assert [p for _, p, _ in acks] == [{"sessionId": CHROME_SESSION}] * 3
    # Target-time pacing on a fixed grid: the first ack is immediate (its slot is
    # ``now - period``), the second lands on ``now`` and the third waits one period.
    assert clock.sleeps == [pytest.approx(sc.LADDER[0].period)]
    assert stream.metrics.frames == 3 and stream.metrics.bytes == 3 * len(jpeg)


@pytest.mark.asyncio
async def test_backpressure_a_card_that_never_acks_makes_frames_late_and_steps_down() -> None:
    stream, relay, clock, sink = make_stream(
        ack_on_arrival=False, config=sc.LiveConfig(mask_passwords=False)
    )
    await stream.start()
    before = clock.now
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    # The frame waited the whole card-ack bound, then Chrome was acked anyway.
    assert clock.now - before >= sc.CARD_ACK_TIMEOUT_S
    assert len(relay.methods("Page.screencastFrameAck")) == 1
    starts_before = len(relay.methods("Page.startScreencast"))
    stats = await stream.tick()
    assert stats.late_share == 1.0 and stats.rung >= 1
    # A late frame costs frame rate: pacing only, Chrome is not restarted.
    assert stats.restarted is False and stream.controller.picture == 0
    assert len(relay.methods("Page.startScreencast")) == starts_before


@pytest.mark.asyncio
async def test_a_late_card_ack_is_still_accepted_once_the_frame_moved_on() -> None:
    stream, relay, _clock, _sink = make_stream(
        ack_on_arrival=False, config=sc.LiveConfig(mask_passwords=False)
    )
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(640, 384)))
    assert stream.ack(1) is False  # nothing waits on it any more
    assert stream.ack(99) is False


@pytest.mark.asyncio
async def test_only_a_picture_change_restarts_the_screencast() -> None:
    stream, relay, _clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    starts = len(relay.methods("Page.startScreencast"))
    # 60 -> 5 fps at the same picture: pacing only, Chrome is not touched.
    assert await stream._apply_rung(0, sc.rung_index(0, 6)) is False
    assert len(relay.methods("Page.startScreencast")) == starts
    # The next picture: a restart on the same session at its quality and width.
    stream.controller.restore(1, 0)
    assert await stream._apply_rung(0, stream.rung) is True
    assert len(relay.methods("Page.stopScreencast")) == 1
    last = relay.methods("Page.startScreencast")[-1]
    assert last[2] == SESSION and last[1]["quality"] == 45 and last[1]["maxWidth"] == 960


@pytest.mark.asyncio
async def test_a_bursty_late_link_lowers_fps_without_a_restart_end_to_end() -> None:
    """Agent steps over a slow link: frames late, fps lowered, picture and cast untouched."""
    stream, relay, clock, sink = make_stream(
        ack_on_arrival=False, config=sc.LiveConfig(mask_passwords=False)
    )

    async def slow_card(header: dict, blob: bytes) -> None:
        # 150 ms round trip plus the frame's bytes at 2 Mbit/s.
        clock.now += 0.15 + len(blob) / 250_000
        stream.ack(header["seq"])

    sink.frame = slow_card  # type: ignore[method-assign]
    await stream.start()
    starts = len(relay.methods("Page.startScreencast"))
    for _step in range(6):
        for _ in range(3):
            await relay.on_event(frame_event(make_jpeg(1280, 768, payload=40_000)))
        await stream.tick()
        clock.now += 3  # the agent thinks
        await stream.tick()
    assert stream.controller.picture == 0
    assert sc.FPS_STEPS[stream.controller.fps_step] < 60
    assert len(relay.methods("Page.startScreencast")) == starts
    assert stream.metrics.restarts == 0


@pytest.mark.asyncio
async def test_off_screen_stops_the_screencast_and_visible_starts_it_again() -> None:
    stream, relay, _clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await stream.set_visible(False)
    assert len(relay.methods("Page.stopScreencast")) == 1 and stream.casting is False
    # A rung change while hidden does not touch Chrome.
    assert await stream._apply_rung(0, 3) is False
    await stream.set_visible(True)
    assert len(relay.methods("Page.startScreencast")) == 2 and stream.casting is True
    await stream.set_visible(True)  # idempotent
    assert len(relay.methods("Page.startScreencast")) == 2


@pytest.mark.asyncio
async def test_watchdog_restarts_after_input_with_no_frame_for_three_seconds() -> None:
    stream, relay, clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    stream.note_input()
    clock.now += sc.WATCHDOG_S - 0.5
    assert (await stream.tick()).restarted is False
    clock.now += 0.5
    stats = await stream.tick()
    assert stats.restarted is True and stream.metrics.restarts == 1
    # Fires once per input: the next tick with no new input does nothing.
    clock.now += 10
    assert (await stream.tick()).restarted is False


@pytest.mark.asyncio
async def test_a_frame_after_input_clears_the_watchdog() -> None:
    stream, relay, clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    stream.note_input()
    clock.now += 1
    await relay.on_event(frame_event(make_jpeg(640, 384)))
    clock.now += sc.WATCHDOG_S
    assert (await stream.tick()).restarted is False


@pytest.mark.asyncio
async def test_password_boxes_are_asked_for_scaled_and_rate_limited() -> None:
    stream, relay, clock, sink = make_stream()
    # Two password fields in CSS px of a 1280-wide viewport; the frame is 640 wide.
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": [[100, 50, 200, 30], [0, 0, 0, 0], "junk"]}
    }
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(640, 384), device_w=1280))
    header, _ = sink.frames[0]
    assert header["mask"] == [[50, 25, 101, 16]]
    method, params, sid = relay.methods("Runtime.evaluate")[0]
    assert sid == SESSION and params["returnByValue"] is True
    assert "input[type=password]" in params["expression"]
    # Within the interval the cached boxes are reused: no second evaluate.
    clock.now += sc.MASK_MIN_INTERVAL_S / 2
    await relay.on_event(frame_event(make_jpeg(640, 384), device_w=1280))
    assert len(relay.methods("Runtime.evaluate")) == 1
    assert sink.frames[1][0]["mask"] == [[50, 25, 101, 16]]
    clock.now += sc.MASK_MIN_INTERVAL_S
    await relay.on_event(frame_event(make_jpeg(640, 384), device_w=1280))
    assert len(relay.methods("Runtime.evaluate")) == 2


@pytest.mark.asyncio
async def test_a_failed_mask_query_keeps_the_last_boxes_rather_than_unmasking() -> None:
    stream, relay, clock, sink = make_stream()
    relay.answers["Runtime.evaluate"] = {"result": {"value": [[10, 10, 20, 20]]}}
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    relay.fail.add("Runtime.evaluate")
    clock.now += 1
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    assert sink.frames[1][0]["mask"] == [[10, 10, 21, 21]]


@pytest.mark.asyncio
async def test_mask_off_in_config_never_asks_the_page() -> None:
    stream, relay, _clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    assert not relay.methods("Runtime.evaluate") and sink.frames[0][0]["mask"] == []


@pytest.mark.asyncio
async def test_takeover_caps_the_stream_at_fifteen_fps_and_releases_after() -> None:
    stream, relay, _clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await stream.set_takeover(True)
    assert stream.rung == sc.TAKEOVER_RUNG
    # Frame rate only: the picture the person works on stays sharp, no restart.
    assert len(relay.methods("Page.startScreencast")) == 1
    await stream.set_takeover(False)
    assert (
        stream.controller.fps_floor == 0 and stream.rung == sc.TAKEOVER_RUNG
    )  # climbs by measurement


@pytest.mark.asyncio
async def test_frames_are_ignored_after_stop_and_bad_payloads_are_dropped() -> None:
    stream, relay, _clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await relay.on_event({"method": "Page.screencastFrame", "params": {"data": "%%%"}})
    await relay.on_event({"method": "Page.loadEventFired", "params": {}})
    assert sink.frames == []
    await stream.stop()
    await relay.on_event(frame_event(make_jpeg(640, 384)))
    assert sink.frames == [] and not relay.methods("Page.screencastFrameAck")


@pytest.mark.asyncio
async def test_on_tick_reports_each_window_to_the_hub() -> None:
    seen: list[sc.TickStats] = []

    async def on_tick(stats: sc.TickStats) -> None:
        seen.append(stats)

    relay = FakeRelay()
    stream = sc.LiveStream(
        relay,
        "t",
        SimpleNamespace(frame=None),  # never reached: no frames pushed
        clock=lambda: 0.0,
        sleep=_no_sleep,
        auto_control=False,
        on_tick=on_tick,
    )
    await stream.start()
    await stream.tick()
    assert len(seen) == 1 and seen[0].frames == 0 and seen[0].rung == 0


async def _no_sleep(_s: float) -> None:
    return None


@pytest.mark.asyncio
async def test_a_full_width_password_field_is_clipped_to_the_frame() -> None:
    """A field spanning the viewport must never produce a mask wider than the frame.

    The card's decoder drops a frame whose mask reaches past its edge: an
    unclipped 1280-wide field in a 640-wide frame is ``[0, 10, 641, 16]`` and
    blacks out the login picture entirely. The clip keeps every visible field
    pixel covered and stops at the frame.
    """
    stream, relay, _clock, sink = make_stream()
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": [[0, 20, 1280, 30], [1270, 100, 50, 20], [-5, -5, 10, 10]]}
    }
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(640, 384), device_w=1280))
    header, _ = sink.frames[0]
    assert header["mask"] == [[0, 10, 640, 16], [635, 50, 5, 11], [0, 0, 3, 3]]
    for x, y, w, h in header["mask"]:
        assert 0 <= x and 0 <= y and x + w <= 640 and y + h <= 384 and w > 0 and h > 0
    assert stream.scale == pytest.approx(0.5)


def test_clip_mask_rects_drops_what_lies_wholly_off_frame() -> None:
    assert sc.clip_mask_rects([[1400, 10, 20, 20], [10, 800, 20, 20]], 0.5, 640, 384) == []
    assert sc.clip_mask_rects("junk", 1.0, 640, 384) == []
    assert sc.clip_mask_rects([[10, 10, 20, 20]], 1.0, 0, 0) == []


@pytest.mark.asyncio
async def test_each_frame_carries_the_stream_id_and_the_scale_follows_the_rung() -> None:
    """``stream`` on the header scopes the card's acks; ``scale`` is frame/viewport."""
    stream, relay, _clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await relay.on_event(frame_event(make_jpeg(1280, 768), device_w=1280))
    assert stream.scale == 1.0
    await relay.on_event(frame_event(make_jpeg(800, 480), device_w=1280))
    assert stream.scale == pytest.approx(0.625)
    # The viewport's CSS width, which takeover input divides the card's own frame by.
    assert stream.device_width == 1280.0
    assert {h["stream"] for h, _ in sink.frames} == {stream.stream_id}
    other, _r, _c, _s = make_stream()
    assert other.stream_id != stream.stream_id


# ── link memory across streams ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_next_stream_of_the_card_starts_where_the_last_one_stood() -> None:
    mem = sc.LinkMemory()
    stream, relay, clock, _sink = make_stream(link_memory=mem)
    await stream.start()
    stream.controller.restore(1, 6)
    await stream.tick()
    await stream.stop()
    nxt = sc.LiveStream(
        relay,
        "target-2",
        _sink,
        session_key="chat-1",
        clock=clock,
        sleep=clock.sleep,
        auto_control=False,
        link_memory=mem,
    )
    assert (nxt.controller.picture, nxt.controller.fps_step) == (1, 6)
    await nxt.start()
    first = relay.methods("Page.startScreencast")[-1][1]
    assert (first["quality"], first["maxWidth"]) == (45, 960)  # not the top rung
    other = sc.LiveStream(
        relay,
        "t3",
        _sink,
        session_key="chat-2",
        clock=clock,
        sleep=clock.sleep,
        auto_control=False,
        link_memory=mem,
    )
    assert other.rung == 0


# ── the settle frame ───────────────────────────────────────────────────────


async def _land_frame(stream: sc.LiveStream, relay: FakeRelay, jpeg: bytes) -> None:
    await relay.on_event(frame_event(jpeg))


@pytest.mark.asyncio
async def test_a_still_page_gets_one_sharp_settle_frame_masked() -> None:
    stream, relay, clock, sink = make_stream()
    relay.answers["Runtime.evaluate"] = {"result": {"value": [[100, 50, 200, 30]]}}
    stream.controller.restore(2, 6)  # a squeezed link: 5 fps at q40, 800 wide
    await stream.start()
    await _land_frame(stream, relay, make_jpeg(800, 480))
    assert await stream.settle() is False  # not still long enough yet
    clock.now += sc.SETTLE_S + 0.05
    assert await stream.settle() is True
    start = relay.methods("Page.startScreencast")[-1][1]
    assert (start["quality"], start["maxWidth"]) == (sc.SETTLE_QUALITY, sc.PICTURES[0].width)
    # Chrome answers the restart with one frame of the still page: masked at its scale.
    clock.now += 1
    await _land_frame(stream, relay, make_jpeg(1280, 768))
    header, _ = sink.frames[-1]
    assert header["w"] == 1280 and header["mask"] == [[100, 50, 201, 31]]
    # Once per still: no second settle while nothing changed.
    clock.now += 10
    assert await stream.settle() is False
    assert stream.metrics.settles == 1
    # The settle frame is not ladder evidence.
    assert stream.controller.window.frames == 1


@pytest.mark.asyncio
async def test_the_next_page_change_goes_back_to_the_ladder_picture() -> None:
    stream, relay, clock, sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    stream.controller.restore(2, 6)
    await stream.start()
    await _land_frame(stream, relay, make_jpeg(800, 480))
    clock.now += sc.SETTLE_S + 0.05
    await stream.settle()
    await _land_frame(stream, relay, make_jpeg(1280, 768))  # the still
    starts = len(relay.methods("Page.startScreencast"))
    # A frame inside the grace window still belongs to the settle restart.
    await _land_frame(stream, relay, make_jpeg(1280, 768))
    assert len(relay.methods("Page.startScreencast")) == starts
    clock.now += sc.SETTLE_GRACE_S + 0.1
    seen = stream.controller.window.frames
    await _land_frame(stream, relay, make_jpeg(1280, 768))  # the page moved
    assert stream.controller.window.frames == seen, "a sharp frame is never ladder evidence"
    last = relay.methods("Page.startScreencast")[-1][1]
    assert len(relay.methods("Page.startScreencast")) == starts + 1
    assert (last["quality"], last["maxWidth"]) == (40, 800)
    # A new still after the change is settled again, after the minimum interval.
    await _land_frame(stream, relay, make_jpeg(800, 480))
    clock.now += sc.SETTLE_S + 0.05
    clock.now += sc.SETTLE_MIN_INTERVAL_S
    assert await stream.settle() is True


@pytest.mark.asyncio
async def test_no_settle_during_takeover_input_or_with_a_pinned_low_quality() -> None:
    stream, relay, clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    await stream.set_takeover(True)
    await _land_frame(stream, relay, make_jpeg(1280, 768))
    stream.note_input()
    clock.now += sc.SETTLE_S / 2
    await _land_frame(stream, relay, make_jpeg(1280, 768))
    clock.now += sc.SETTLE_S / 2
    stream._input_at = clock.now  # another keystroke just went out
    assert await stream.settle() is False
    clock.now += sc.SETTLE_S + 0.05
    assert await stream.settle() is False  # the person still holds the browser
    await stream.set_takeover(False)
    assert await stream.settle() is True  # handed back: the page may settle

    low, relay2, clock2, _s2 = make_stream(
        config=sc.LiveConfig(quality="low", mask_passwords=False)
    )
    await low.start()
    await _land_frame(low, relay2, make_jpeg(800, 480))
    clock2.now += 5
    assert await low.settle() is False


@pytest.mark.asyncio
async def test_no_settle_while_hidden_or_before_any_frame() -> None:
    stream, relay, clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=False))
    await stream.start()
    clock.now += 5
    assert await stream.settle() is False  # nothing shown yet
    await _land_frame(stream, relay, make_jpeg(1280, 768))
    await stream.set_visible(False)
    clock.now += 5
    assert await stream.settle() is False


# ── cross-origin iframes ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_password_boxes_inside_a_cross_origin_frame_are_masked_on_the_live_frame() -> None:
    """The top query cannot see into an out-of-process iframe: the frame's own session
    is asked, and its boxes move by the owner's content box (measured by the browser)."""
    stream, relay, _clock, sink = make_stream()
    child = "CHILD"
    top_answer = {
        "result": {"value": {"boxes": [], "frames": [[100, 200, 400, 300]], "partial": False}}
    }
    child_answer = {
        "result": {"value": {"boxes": [[20, 80, 300, 30]], "frames": [], "partial": False}}
    }
    real_send = relay.send

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        if session_id == child and method == "Runtime.evaluate":
            relay.calls.append((method, dict(params or {}), session_id))
            expr = params["expression"]
            return {"result": {"value": [400, 300]}} if "innerWidth" in expr else child_answer
        if method == "Runtime.evaluate":
            relay.calls.append((method, dict(params or {}), session_id))
            return top_answer
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    relay.answers["DOM.getFrameOwner"] = {"backendNodeId": 9}
    relay.answers["DOM.getBoxModel"] = {
        "model": {"content": [100, 200, 500, 200, 500, 500, 100, 500]}
    }
    await stream.start()
    await stream._on_event(
        {
            "method": "Target.attachedToTarget",
            "sessionId": SESSION,
            "params": {"sessionId": child, "targetInfo": {"targetId": "F2", "type": "iframe"}},
        }
    )
    assert stream.frames.frames() == [("F2", child)]
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    header, _ = sink.frames[-1]
    assert header["mask"] == [[120, 280, 301, 31]]


@pytest.mark.asyncio
async def test_a_frame_that_answers_junk_boxes_is_masked_whole_and_the_frame_still_goes() -> None:
    stream, relay, _clock, sink = make_stream()
    child = "CHILD"
    real_send = relay.send

    async def send(method: str, params: Any = None, session_id: Any = None) -> Any:
        if method == "Runtime.evaluate":
            if session_id == child:
                expr = params["expression"]
                if "innerWidth" in expr:
                    return {"result": {"value": [400, 300]}}
                return {"result": {"value": {"boxes": [["1e3", "x", 2, 3]], "frames": []}}}
            return {
                "result": {
                    "value": {"boxes": [], "frames": [[100, 200, 400, 300]], "partial": False}
                }
            }
        return await real_send(method, params, session_id)

    relay.send = send  # type: ignore[method-assign]
    relay.answers["DOM.getFrameOwner"] = {"backendNodeId": 9}
    relay.answers["DOM.getBoxModel"] = {
        "model": {"content": [100, 200, 500, 200, 500, 500, 100, 500]}
    }
    await stream.start()
    await stream._on_event(
        {
            "method": "Target.attachedToTarget",
            "sessionId": SESSION,
            "params": {"sessionId": child, "targetInfo": {"targetId": "F2", "type": "iframe"}},
        }
    )
    await relay.on_event(frame_event(make_jpeg(1280, 768)))
    header, _ = sink.frames[-1]
    assert header["mask"] and header["mask"][0][:2] == [100, 200]
