"""Turn replay: per-turn recording of masked frames and actions, its caps and its routes.

The hard rules (``browser_harness/recording.py``) each have a test here: no
unmasked or unknown-mask frame on disk, no typed secret on disk, change-only at
<= 5 fps, byte caps evicting oldest turns first, owner-only routes that refuse
traversal and leak nothing, cleanup on session delete, 0700/0600 on disk.
"""

from __future__ import annotations

import io
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from browser_live_helpers import FakeRelay, frame_event, make_stream
from PIL import Image

from kiro_crew.browser_harness import live_hub as lh
from kiro_crew.browser_harness import recording as rc
from kiro_crew.browser_harness import screencast as sc
from kiro_crew.browser_harness.takeover import parse_input_event
from kiro_crew.dashboard.handlers import browser_live as live_mod
from kiro_crew.dashboard.handlers.sessions_parts import slot_teardown
from kiro_crew.dashboard.routes import browser_live as route_slice

pytestmark = pytest.mark.asyncio

KEY = "chat-1"
TURN = "a" * 32


def jpeg(color: tuple[int, int, int] = (250, 250, 250), size: tuple[int, int] = (64, 40)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, format="JPEG", quality=95)
    return out.getvalue()


def pixel(data: bytes, x: int, y: int) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.convert("RGB").getpixel((x, y))


class Clock:
    def __init__(self, start: int = 1_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    rc.reset_recorder()
    lh.reset_live_hub()
    yield
    rc.reset_recorder()
    lh.reset_live_hub()


def make_recorder(tmp_path: Path, **cfg: Any) -> tuple[rc.LiveRecorder, rc.RecordingStore, Clock]:
    store = rc.RecordingStore(lambda: tmp_path, rc.RecordingConfig(**cfg))
    clock = Clock()
    return rc.LiveRecorder(store, clock_ms=clock), store, clock


def turn_dir(tmp_path: Path, key: str = KEY, turn: str = TURN) -> Path:
    return tmp_path / rc.RECORDINGS_DIRNAME / rc.session_dirname(key) / turn


def all_bytes(tmp_path: Path) -> bytes:
    root = tmp_path / rc.RECORDINGS_DIRNAME
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


# ── frames: mask, fail closed, change-only, 5 fps ──────────────────────────


async def test_a_frame_with_an_unknown_mask_never_reaches_disk(tmp_path: Path) -> None:
    rec, store, clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    rec.frame(KEY, jpeg(), 64, 40, [], mask_ok=False)
    clock.now += 1000
    rec.frame(KEY, jpeg((1, 2, 3)), 64, 40, [], mask_ok=False)
    rec.turn_ended(KEY)
    await rec.flush()
    assert store.list_turns(KEY)[0]["frame_count"] == 0
    assert list((turn_dir(tmp_path) / "frames").iterdir()) == []


async def test_password_boxes_are_burned_into_the_kept_jpeg(tmp_path: Path) -> None:
    rec, store, _clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    rec.frame(KEY, jpeg((250, 0, 0)), 64, 40, [[10, 10, 20, 10]], mask_ok=True)
    await rec.flush()
    kept = store.read_frame(KEY, TURN, 0)
    assert kept is not None
    inside, outside = pixel(kept, 20, 15), pixel(kept, 50, 35)
    edge = pixel(kept, 10 - rc.MASK_PAD_PX + 1, 10 - rc.MASK_PAD_PX + 1)
    assert max(inside) < 80 and max(edge) < 80, (inside, edge)  # painted, with a margin
    assert outside[0] > 200, outside  # the rest of the page is untouched


@pytest.mark.parametrize(
    "boxes",
    [
        [[60, 30, 20, 20]],  # reaches past the frame
        [[-1, 0, 4, 4]],
        [[0, 0, 0, 4]],
        [["1", 0, 4, 4]],
        [[1, 2, 3]],
        "nope",
    ],
)
async def test_a_box_that_cannot_be_painted_exactly_drops_the_frame(tmp_path: Path, boxes) -> None:
    _rec, store, _clock = make_recorder(tmp_path)
    store.start_turn(KEY, TURN, 1)
    assert store.add_frame(KEY, TURN, jpeg(), 64, 40, boxes, True, 2) is False
    assert store.turn_detail(KEY, TURN)["frames"] == []


async def test_an_undecodable_or_missized_frame_with_boxes_is_dropped(tmp_path: Path) -> None:
    _rec, store, _clock = make_recorder(tmp_path)
    store.start_turn(KEY, TURN, 1)
    assert store.add_frame(KEY, TURN, b"\xff\xd8junk", 64, 40, [[1, 1, 4, 4]], True, 2) is False
    assert store.add_frame(KEY, TURN, jpeg(), 32, 40, [[1, 1, 4, 4]], True, 3) is False
    assert store.turn_detail(KEY, TURN)["frames"] == []


async def test_no_imaging_library_means_no_masked_frame(tmp_path: Path, monkeypatch) -> None:
    raw = jpeg()
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert rc.burn_mask(raw, 64, 40, [[1, 1, 4, 4]]) is None
    # A frame with no password box needs no painting and is kept as it came.
    assert rc.burn_mask(raw, 64, 40, []) == raw


async def test_frames_are_kept_on_change_at_most_five_per_second(tmp_path: Path) -> None:
    rec, store, clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    a, b, c, d = jpeg((10, 10, 10)), jpeg((90, 90, 90)), jpeg((170, 170, 170)), jpeg((240,) * 3)
    rec.frame(KEY, a, 64, 40, [], True)  # t=0 kept
    clock.now += 50
    rec.frame(KEY, b, 64, 40, [], True)  # 50 ms: held
    clock.now += 50
    rec.frame(KEY, c, 64, 40, [], True)  # 100 ms: replaces the held one
    clock.now += 150
    rec.frame(KEY, c, 64, 40, [], True)  # unchanged from the held? it was never kept
    clock.now += 10
    rec.frame(KEY, c, 64, 40, [], True)  # identical to the last kept: skipped
    clock.now += 300
    rec.frame(KEY, d, 64, 40, [], True)
    rec.turn_ended(KEY)
    await rec.flush()
    frames = store.turn_detail(KEY, TURN)["frames"]
    gaps = [b["ts"] - a["ts"] for a, b in zip(frames, frames[1:])]
    assert len(frames) == 3 and all(g >= rc.MIN_FRAME_INTERVAL_MS for g in gaps), frames
    assert store.read_frame(KEY, TURN, 1) == c  # the latest of the burst, not the first


async def test_a_held_frame_is_kept_when_the_turn_ends(tmp_path: Path) -> None:
    rec, store, clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    rec.frame(KEY, jpeg((10, 10, 10)), 64, 40, [], True)
    clock.now += 20
    last = jpeg((200, 200, 200))
    rec.frame(KEY, last, 64, 40, [], True)
    rec.turn_ended(KEY)
    await rec.flush()
    assert store.read_frame(KEY, TURN, 1) == last


async def test_an_unknown_mask_also_drops_the_frame_held_before_it(tmp_path: Path) -> None:
    rec, store, clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    rec.frame(KEY, jpeg((10, 10, 10)), 64, 40, [], True)
    clock.now += 20
    rec.frame(KEY, jpeg((200, 200, 200)), 64, 40, [], True)  # held
    rec.frame(KEY, jpeg((99, 99, 99)), 64, 40, [], False)  # unknown mask
    rec.turn_ended(KEY)
    await rec.flush()
    assert store.list_turns(KEY)[0]["frame_count"] == 1


# ── the stream's half of the seam ──────────────────────────────────────────


class RecordingProbeSink:
    records = True

    def __init__(self) -> None:
        self.recorded: list[tuple[int, int, list, bool]] = []
        self.frames: list[dict] = []
        self.stream: Any = None

    def record(
        self, data: bytes, w: int, h: int, boxes: list, mask_ok: bool, age_s: float = 0.0
    ) -> None:
        self.recorded.append((w, h, boxes, mask_ok))

    async def frame(self, header: dict, blob: bytes) -> None:
        self.frames.append(dict(header))
        self.stream.ack(header["seq"])


def probe_stream(mask_passwords: bool) -> tuple[sc.LiveStream, FakeRelay, RecordingProbeSink]:
    stream, relay, _clock, _sink = make_stream(config=sc.LiveConfig(mask_passwords=mask_passwords))
    sink = RecordingProbeSink()
    sink.stream = stream
    stream.sink = sink
    return stream, relay, sink


async def test_the_stream_queries_boxes_for_the_recorder_even_with_the_card_mask_off() -> None:
    stream, relay, sink = probe_stream(mask_passwords=False)
    relay.answers["Runtime.evaluate"] = {"result": {"value": [[0, 0, 10, 10]]}}
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    assert sink.recorded == [(1280, 800, [[0, 0, 11, 11]], True)]
    assert sink.frames[0]["mask"] == []  # the card was asked for no mask


@pytest.mark.parametrize("answer", [{"result": {"value": None}}, {"result": {}}, "raise"])
async def test_a_failed_box_query_reaches_the_recorder_as_unknown(answer) -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    if answer == "raise":
        relay.fail.add("Runtime.evaluate")
    else:
        relay.answers["Runtime.evaluate"] = answer
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    assert sink.recorded[0][3] is False


async def test_every_recorded_frame_gets_its_own_box_query_never_a_cached_one() -> None:
    """A login field that appears inside the card's 250 ms cache must still be masked."""
    stream, relay, sink = probe_stream(mask_passwords=True)
    answers = iter([{"result": {"value": []}}, {"result": {"value": [[0, 0, 10, 10]]}}])
    relay.answers["Runtime.evaluate"] = lambda: next(answers)
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    await relay.on_event(frame_event(jpeg((1, 1, 1), size=(1280, 800))))  # same clock tick
    assert [r[2] for r in sink.recorded] == [[], [[0, 0, 11, 11]]]
    assert all(r[3] for r in sink.recorded)


async def test_a_frame_whose_page_navigated_before_its_box_query_is_not_kept_as_masked() -> None:
    """A submitted one-time code navigates after Chrome captured the frame: the
    query then reads the NEW document (its world had to be made again), which
    says nothing about the old frame's fields, so that frame is never kept."""
    stream, relay, sink = probe_stream(mask_passwords=True)
    calls = {"n": 0}

    def _evaluate() -> dict:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("Cannot find context with specified id")  # navigated
        return {"result": {"value": []}}

    relay.answers["Runtime.evaluate"] = _evaluate
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    await relay.on_event(frame_event(jpeg((1, 1, 1), size=(1280, 800))))
    await relay.on_event(frame_event(jpeg((2, 2, 2), size=(1280, 800))))
    assert [r[3] for r in sink.recorded] == [True, False, True]


async def test_boxes_follow_the_frame_size_after_a_ladder_step() -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    relay.answers["Runtime.evaluate"] = {"result": {"value": [[100, 100, 200, 30]]}}
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    await relay.on_event(frame_event(jpeg(size=(640, 400)), device_w=1280))
    assert sink.recorded[1][2] == [[50, 50, 101, 16]]


async def test_an_opaque_iframe_is_painted_over_for_the_recorder_only() -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": {"boxes": [[0, 0, 10, 10]], "frames": [[200, 100, 300, 200]]}}
    }
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    assert sink.recorded[0][2] == [[0, 0, 11, 11], [200, 100, 301, 201]] and sink.recorded[0][3]
    assert sink.frames[0]["mask"] == [[0, 0, 11, 11]]  # the card paints password boxes only


async def test_more_boxes_than_the_limit_is_an_unknown_mask() -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    many = [[i, 0, 2, 2] for i in range(sc.MASK_BOX_LIMIT + 1)]
    relay.answers["Runtime.evaluate"] = {"result": {"value": many}}
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    assert sink.recorded[0][3] is False


async def _held_frame(answer_now: Any, *, size: tuple[int, int] = (1280, 800)):
    """One offered frame (boxes ``[[0,0,10,10]]``), one held, the page then answering *answer_now*."""
    stream, relay, sink = probe_stream(mask_passwords=False)
    gate = {"open": True}
    sink.wants_frame = lambda: gate["open"]  # type: ignore[attr-defined]
    relay.answers["Runtime.evaluate"] = {"result": {"value": [[0, 0, 10, 10]]}}
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    gate["open"] = False
    queries = len(relay.methods("Runtime.evaluate"))
    await relay.on_event(frame_event(jpeg((5, 5, 5), size=size), device_w=1280))
    assert len(sink.recorded) == 1 and len(relay.methods("Runtime.evaluate")) == queries
    gate["open"] = True
    relay.answers["Runtime.evaluate"] = answer_now
    return stream, relay, sink


async def test_a_held_frame_is_offered_when_the_boxes_stood_still_around_it() -> None:
    stream, _relay, sink = await _held_frame({"result": {"value": [[0, 0, 10, 10]]}})
    assert await stream.flush_held() is True
    assert len(sink.recorded) == 2 and sink.recorded[1][2:] == ([[0, 0, 11, 11]], True)
    assert await stream.flush_held() is False  # offered once


@pytest.mark.parametrize(
    "answer_now",
    [
        {"result": {"value": []}},  # the field went away after the frame was captured
        {"result": {"value": [[0, 0, 10, 10], [50, 50, 10, 10]]}},  # one appeared
        "raise",
    ],
)
async def test_a_held_frame_is_dropped_when_its_boxes_cannot_be_proved(answer_now) -> None:
    stream, relay, sink = await _held_frame(answer_now if answer_now != "raise" else {})
    if answer_now == "raise":
        relay.fail.add("Runtime.evaluate")
    assert await stream.flush_held() is False
    assert len(sink.recorded) == 1


async def test_a_held_frame_of_another_size_is_dropped() -> None:
    stream, _relay, sink = await _held_frame(
        {"result": {"value": [[0, 0, 10, 10]]}}, size=(640, 400)
    )
    assert await stream.flush_held() is False and len(sink.recorded) == 1


async def test_a_held_frame_from_before_the_turn_is_not_kept(tmp_path: Path) -> None:
    rec, store, clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    clock.now += 500
    rec.frame(KEY, jpeg(), 64, 40, [], True, age_s=2.0)  # captured before the turn began
    rec.turn_ended(KEY)
    await rec.flush()
    assert store.list_turns(KEY)[0]["frame_count"] == 0


def _scrolled(event: dict[str, Any], x: float, y: float) -> dict[str, Any]:
    """The frame as Chrome stamps it: the page's scroll offset when it was captured."""
    event["params"]["metadata"].update({"scrollOffsetX": x, "scrollOffsetY": y})
    return event


async def test_boxes_read_at_the_frames_scroll_are_kept() -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": {"boxes": [[0, 0, 10, 10]], "frames": [], "sx": 0, "sy": 120.5}}
    }
    await stream.start()
    await relay.on_event(_scrolled(frame_event(jpeg(size=(1280, 800))), 0, 120))
    assert sink.recorded[0][2:] == ([[0, 0, 11, 11]], True)


@pytest.mark.parametrize(
    "value",
    [
        # A wheel scroll moved the page between the capture and the query: the field
        # the frame shows was read off screen, so the boxes say nothing about it.
        {"boxes": [], "frames": [], "sx": 0, "sy": 355},
        {"boxes": [[0, 0, 10, 10]], "frames": [], "sx": 40, "sy": 55},
        # A query that does not say where the page was cannot be matched to a frame.
        {"boxes": [[0, 0, 10, 10]], "frames": []},
        {"boxes": [[0, 0, 10, 10]], "frames": [], "sx": True, "sy": 55},
    ],
)
async def test_boxes_read_at_another_scroll_are_an_unknown_mask(value) -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    relay.answers["Runtime.evaluate"] = {"result": {"value": value}}
    await stream.start()
    await relay.on_event(_scrolled(frame_event(jpeg(size=(1280, 800))), 0, 55))
    assert sink.recorded[0][3] is False


async def test_a_held_frame_is_dropped_when_the_page_scrolled_since_it() -> None:
    stream, relay, sink = probe_stream(mask_passwords=False)
    gate = {"open": True}
    sink.wants_frame = lambda: gate["open"]  # type: ignore[attr-defined]
    same = {"result": {"value": {"boxes": [], "frames": [], "sx": 0, "sy": 55}}}
    relay.answers["Runtime.evaluate"] = same
    await stream.start()
    await relay.on_event(_scrolled(frame_event(jpeg(size=(1280, 800))), 0, 55))
    gate["open"] = False
    await relay.on_event(_scrolled(frame_event(jpeg((5, 5, 5), size=(1280, 800))), 0, 55))
    gate["open"] = True
    # Same (empty) boxes as before, but read 300 px further down the page.
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": {"boxes": [], "frames": [], "sx": 0, "sy": 355}}
    }
    assert await stream.flush_held() is False and len(sink.recorded) == 1


async def test_a_partial_walk_is_an_unknown_mask() -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    relay.answers["Runtime.evaluate"] = {
        "result": {"value": {"boxes": [], "frames": [], "partial": True}}
    }
    await stream.start()
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    assert sink.recorded[0][3] is False


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("javascript:login('u','SECRET5')", "javascript:"),
        ("data:text/html,SECRET6", "data:"),
        ("https://a.example/app;jsessionid=SECRET4/x", "https://a.example/app/x"),
        ("http://[::1]:8080/p?t=SECRET", "http://[::1]:8080/p"),
        ("about:blank", "about:blank"),
        ("not a url", ""),
    ],
)
def test_clean_url_keeps_the_place_never_the_payload(url: str, kept: str) -> None:
    assert rc.clean_url(url) == kept


@pytest.mark.parametrize(
    "label",
    [
        "Open https://a.example/x ?token=SECRET",
        "Open https://a.example/x\t#token=SECRET",
        "Open a.example/?token=SECRET",
        "Open javascript:steal('SECRET')",
    ],
)
def test_a_label_never_keeps_a_query_or_payload(label: str) -> None:
    ev = rc.agent_event("tabs", {}, {"ok": True}, ts=1, label=label, url="", scale=1)
    assert "SECRET" not in ev["label"] and ev["label"].startswith("Open")


async def test_a_hidden_card_keeps_the_cast_for_the_recorder_only() -> None:
    stream, relay, sink = probe_stream(mask_passwords=True)
    relay.answers["Runtime.evaluate"] = {"result": {"value": []}}
    await stream.start()
    await stream.set_visible(False)
    assert relay.methods("Page.stopScreencast") == []
    await relay.on_event(frame_event(jpeg(size=(1280, 800))))
    assert len(sink.recorded) == 1 and sink.frames == []  # nothing sent to the card
    assert len(relay.methods("Page.screencastFrameAck")) == 1
    await stream.set_visible(True)
    assert len(relay.methods("Page.startScreencast")) == 1  # never started twice


async def test_hiding_a_settled_recorder_cast_puts_it_back_on_the_ladder_picture() -> None:
    stream, relay, sink = probe_stream(mask_passwords=False)
    await stream.start()
    stream._settled = True  # the still page was sharpened (quality 85)
    await stream.set_visible(False)
    await stream.set_visible(True)
    ladder = stream._cast_picture()
    assert ladder.quality < 85
    last = relay.methods("Page.startScreencast")[-1][1]
    assert (last["quality"], last["maxWidth"]) == (ladder.quality, ladder.width)
    assert stream._settled is False and stream.casting is True


async def test_the_hub_sink_records_through_the_real_recorder(tmp_path: Path) -> None:
    """The real recorder behind the hub's sink: ``records`` follows an open turn."""
    from kiro_crew.browser_harness import live_hub

    rec, store, _clock = make_recorder(tmp_path)
    hub = SimpleNamespace(recorder=rec)
    sink = live_hub._StreamSink(hub, live_hub.LiveSession(session_key=KEY))  # type: ignore[arg-type]
    assert sink.records is False, "no turn open: a hidden card stops its cast"
    rec.turn_started(KEY, TURN)
    assert sink.records is True
    sink.note_size(64, 40)
    rec.event(KEY, rc.user_event("click", 1, "u", point={"x": 3, "y": 4}))
    rec.turn_ended(KEY)
    await rec.flush()
    assert sink.records is False
    (event,) = store.turn_detail(KEY, TURN)["events"]
    assert (event["fw"], event["fh"]) == (64, 40)


async def test_without_a_recorder_a_hidden_card_still_stops_the_cast() -> None:
    stream, relay, _clock, _sink = make_stream()
    await stream.start()
    await stream.set_visible(False)
    assert len(relay.methods("Page.stopScreencast")) == 1


# ── events: agent ops, the person's input, secrets ─────────────────────────


def test_a_person_event_carries_no_english_label_of_its_own() -> None:
    """The card words it (and badges it as the person's); only the person's text is kept."""
    assert rc.user_event("click", 1, "https://a.example/")["label"] == ""
    said = rc.user_event("other", 2, "https://a.example/", label="Didn't work - wrong password")
    assert said["label"] == "Didn't work - wrong password" and said["actor"] == "user"


def test_agent_points_are_scaled_onto_the_frame() -> None:
    click = rc.agent_event(
        "click",
        {"ref": "e3"},
        {"ok": True, "x": 100.0, "y": 50.0},
        ts=5,
        label="Click e3",
        url="https://a.example/",
        scale=0.5,
    )
    assert click["op"] == "click" and click["point"] == {"x": 50, "y": 25}
    assert click["actor"] == "agent" and click["status"] == "ok" and click["label"] == "Click e3"
    drag = rc.agent_event(
        "drag",
        {},
        {"ok": True, "from": [10, 10], "to": [30, 40]},
        ts=6,
        label="",
        url="",
        scale=2.0,
    )
    assert drag["op"] == "drag" and drag["path"] == [{"x": 20, "y": 20}, {"x": 60, "y": 80}]
    refused = rc.agent_event(
        "click", {}, {"ok": False, "code": "ref_stale"}, ts=7, label="", url="", scale=1.0
    )
    assert refused["status"] == "error" and refused["point"] is None
    assert rc.agent_event("snapshot", {}, {"ok": True}, ts=8, label="", url="", scale=1)["op"] == (
        "other"
    )


def test_navigate_keeps_the_destination_without_credentials_or_tokens() -> None:
    url = "https://user:hunter2@a.example/cb?code=OAUTHCODE#access_token=TOKEN"
    ev = rc.agent_event(
        "navigate", {"url": url}, {"ok": True}, ts=1, label=f"Open {url}"[:60], url="", scale=1
    )
    assert ev["url"] == "https://a.example/cb" and ev["args"]["url"] == "https://a.example/cb"
    assert ev["label"] == "Open https://a.example/cb"  # a label cut mid-URL is cleaned too
    for leaked in ("hunter2", "OAUTHCODE", "TOKEN", "user"):
        assert leaked not in repr(ev)


@pytest.mark.parametrize("code", ["ref_stale", "no_focus", "interrupted", "browser_error"])
def test_text_that_did_not_land_is_kept_as_a_length(code: str) -> None:
    ev = rc.agent_event(
        "type", {"text": "hunter2"}, {"ok": False, "code": code}, ts=1, label="", url="", scale=1
    )
    assert ev["args"] == {"name": "type", "chars": 7, "secret": True}


def test_a_refused_key_press_keeps_no_character() -> None:
    ev = rc.agent_event(
        "press_key",
        {"key": "h"},
        {"ok": False, "code": "password_field"},
        ts=1,
        label="",
        url="",
        scale=1,
    )
    assert "key" not in ev["args"]
    named = rc.agent_event(
        "press_key",
        {"key": "Enter"},
        {"ok": True, "key": "Enter"},
        ts=1,
        label="Press Enter",
        url="",
        scale=1,
    )
    assert named["args"]["key"] == "Enter" and named["op"] == "key"


@pytest.mark.parametrize(
    ("args", "result"),
    [
        ({"text": "hunter2-SECRET", "secret": True}, {"ok": True}),
        ({"text": "hunter2-SECRET", "ref": "e2"}, {"ok": False, "code": "password_field"}),
    ],
)
async def test_a_secret_type_keeps_its_length_only(tmp_path: Path, args, result) -> None:
    rec, store, _clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    rec.event(KEY, rc.agent_event("type", args, result, ts=1, label="", url="", scale=1))
    rec.turn_ended(KEY)
    await rec.flush()
    (event,) = store.turn_detail(KEY, TURN)["events"]
    assert event["args"]["chars"] == len("hunter2-SECRET") and event["args"]["secret"] is True
    assert b"hunter2" not in all_bytes(tmp_path)


def test_unlisted_args_never_reach_the_event() -> None:
    kept = rc.sanitize_args(
        "click", {"ref": "e1", "password": "p", "cookie": "c", "x": 1, "nested": {"a": 1}}
    )
    assert kept == {"ref": "e1", "x": 1.0}


async def test_the_persons_keystrokes_are_counted_never_kept(tmp_path: Path) -> None:
    rec, store, clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    for ch in "s3cr3t-pass":
        rec.user_input(KEY, parse_input_event({"type": "key", "key": ch, "text": ch}), "u")
        clock.now += 5
    rec.user_input(KEY, parse_input_event({"type": "key", "key": "Enter"}), "u")
    rec.turn_ended(KEY)
    await rec.flush()
    events = store.turn_detail(KEY, TURN)["events"]
    assert [(e["op"], e["actor"]) for e in events] == [("type", "user"), ("key", "user")]
    assert events[0]["args"] == {"chars": 11, "secret": True}
    blob = all_bytes(tmp_path)
    assert b"s3cr3t" not in blob and b"pass" not in blob


async def test_the_persons_press_move_release_is_a_drag_or_a_click(tmp_path: Path) -> None:
    rec, store, _clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)

    def mouse(action: str, x: int, y: int, button: str = "left") -> None:
        ev = {"type": "mouse", "action": action, "x": x, "y": y, "button": button}
        rec.user_input(KEY, parse_input_event(ev), "")

    mouse("down", 10, 10)
    mouse("move", 20, 30)
    mouse("up", 40, 50)
    mouse("down", 5, 5)
    mouse("up", 6, 6)
    rec.user_input(KEY, parse_input_event({"type": "wheel", "x": 1, "y": 2, "dy": 120}), "")
    rec.turn_ended(KEY)
    await rec.flush()
    drag, click, scroll = store.turn_detail(KEY, TURN)["events"]
    assert drag["op"] == "drag" and drag["path"] == [
        {"x": 10, "y": 10},
        {"x": 20, "y": 30},
        {"x": 40, "y": 50},
    ]
    assert click["op"] == "click" and click["point"] == {"x": 5, "y": 5}
    assert scroll["op"] == "scroll" and scroll["args"] == {"dx": 0, "dy": 120}


async def test_a_named_key_keeps_the_key_not_an_english_label(tmp_path: Path) -> None:
    rec, store, _clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    rec.user_input(KEY, parse_input_event({"type": "key", "key": "Enter"}), "u")
    rec.turn_ended(KEY)
    await rec.flush()
    (event,) = store.turn_detail(KEY, TURN)["events"]
    assert event["label"] == "" and event["args"] == {"key": "Enter"}


async def test_a_point_carries_the_size_of_the_frame_it_was_drawn_in(tmp_path: Path) -> None:
    rec, store, clock = make_recorder(tmp_path)
    rec.turn_started(KEY, TURN)
    rec.frame(KEY, jpeg(size=(128, 80)), 128, 80, [], mask_ok=True)
    rec.note_size(KEY, 64, 40)  # the stream stepped down after the last kept frame
    clock.now += 1000
    click = rc.agent_event(
        "click", {}, {"ok": True, "x": 8.0, "y": 4.0}, ts=1, label="", url="", scale=0.5
    )
    rec.event(KEY, click)
    rec.event(KEY, rc.agent_event("snapshot", {}, {"ok": True}, ts=2, label="", url="", scale=0.5))
    rec.turn_ended(KEY)
    await rec.flush()
    shot, snap = store.turn_detail(KEY, TURN)["events"]
    assert (shot["fw"], shot["fh"]) == (64, 40) and shot["point"] == {"x": 4, "y": 2}
    assert "fw" not in snap, "no point, no frame size"


async def test_a_new_turn_forgets_the_last_turns_frame_size(tmp_path: Path) -> None:
    rec, store, _clock = make_recorder(tmp_path)
    rec.turn_started(KEY, "t1")
    rec.note_size(KEY, 64, 40)
    rec.turn_ended(KEY)
    rec.turn_started(KEY, "t2")
    rec.event(KEY, rc.user_event("click", 1, "u", point={"x": 3, "y": 4}))
    rec.turn_ended(KEY)
    await rec.flush()
    (event,) = store.turn_detail(KEY, "t2")["events"]
    assert "fw" not in event


def test_turns_with_no_frames_still_count_against_the_cap(tmp_path: Path) -> None:
    store = rc.RecordingStore(
        lambda: tmp_path, rc.RecordingConfig(max_session_bytes=rc.TURN_META_BYTES * 3)
    )
    for i in range(10):
        store.start_turn(KEY, f"t{i}", 100 + i)
        store.end_turn(KEY, f"t{i}", 101 + i)
    assert len(store.list_turns(KEY)) <= 3


# ── caps and eviction ──────────────────────────────────────────────────────


def _fill(store: rc.RecordingStore, key: str, turn: str, ts: int, n: int = 3) -> None:
    store.start_turn(key, turn, ts)
    for i in range(n):
        store.add_frame(key, turn, jpeg((i * 40, 0, 0)), 64, 40, [], True, ts + i)
    store.end_turn(key, turn, ts + n)


def test_the_session_cap_evicts_the_oldest_finished_turn(tmp_path: Path) -> None:
    size = len(jpeg())
    store = rc.RecordingStore(
        lambda: tmp_path, rc.RecordingConfig(max_session_bytes=size * 7 + rc.TURN_META_BYTES * 2)
    )
    _fill(store, KEY, "t1", 100)
    _fill(store, KEY, "t2", 200)
    _fill(store, KEY, "t3", 300)
    ids = [t["turn_id"] for t in store.list_turns(KEY)]
    assert ids == ["t2", "t3"]
    assert not turn_dir(tmp_path, turn="t1").exists()


def test_the_global_cap_evicts_the_oldest_turn_of_any_session(tmp_path: Path) -> None:
    size = len(jpeg())
    store = rc.RecordingStore(
        lambda: tmp_path, rc.RecordingConfig(max_total_bytes=size * 7 + rc.TURN_META_BYTES * 2)
    )
    _fill(store, "chat-a", "t1", 100)
    _fill(store, "chat-b", "t2", 200)
    _fill(store, "chat-a", "t3", 300)
    assert [t["turn_id"] for t in store.list_turns("chat-a")] == ["t3"]
    assert [t["turn_id"] for t in store.list_turns("chat-b")] == ["t2"]


def test_a_running_turn_past_the_cap_stops_keeping_frames_not_events(tmp_path: Path) -> None:
    size = len(jpeg())
    store = rc.RecordingStore(
        lambda: tmp_path, rc.RecordingConfig(max_session_bytes=size * 2 + rc.TURN_META_BYTES)
    )
    store.start_turn(KEY, TURN, 1)
    kept = [store.add_frame(KEY, TURN, jpeg((i * 50, 0, 0)), 64, 40, [], True, i) for i in range(4)]
    assert kept[0] and not kept[-1]
    assert store.add_event(KEY, TURN, {"ts": 9, "op": "other", "actor": "agent"})
    (turn,) = store.list_turns(KEY)
    assert turn["truncated"] is True and turn["live"] is True and turn["event_count"] == 1


def test_a_long_turns_events_stay_inside_the_cap_and_the_turn_survives_its_end(
    tmp_path: Path,
) -> None:
    """Events count against the cap like frames, so a turn that keeps logging past
    its frames cannot outgrow the cap and then evict itself when it ends."""
    size = len(jpeg())
    cap = size * 2 + rc.TURN_META_BYTES
    store = rc.RecordingStore(lambda: tmp_path, rc.RecordingConfig(max_session_bytes=cap))
    store.start_turn(KEY, TURN, 1)
    store.add_frame(KEY, TURN, jpeg(), 64, 40, [], True, 1)
    kept = [
        store.add_event(
            KEY, TURN, {"ts": 2 + i, "op": "other", "actor": "agent", "label": "x" * 200}
        )
        for i in range(400)
    ]
    assert kept[0] and not kept[-1]
    store.end_turn(KEY, TURN, 500)
    (turn,) = store.list_turns(KEY)
    assert turn["truncated"] is True and turn["frame_count"] == 1
    assert sum(p.stat().st_size for p in turn_dir(tmp_path).rglob("*") if p.is_file()) <= cap


def test_a_failed_frame_write_leaves_nothing_behind_for_the_next_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails (disk full) removes its JPEG and its half index line, so
    the next frame, which reuses the number, is kept once the disk recovers."""
    store = rc.RecordingStore(lambda: tmp_path)
    store.start_turn(KEY, TURN, 1)
    real_append = rc.RecordingStore._append

    def _full(path: Path, data: bytes) -> int:
        with open(path, "ab") as fh:
            fh.write(data[:5])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rc.RecordingStore, "_append", staticmethod(_full))
    assert store.add_frame(KEY, TURN, jpeg((10, 0, 0)), 64, 40, [], True, 2) is False
    assert not (turn_dir(tmp_path) / "frames" / "0.jpg").exists()
    monkeypatch.setattr(rc.RecordingStore, "_append", staticmethod(real_append))
    assert store.add_frame(KEY, TURN, jpeg((90, 0, 0)), 64, 40, [], True, 3) is True
    lines = (turn_dir(tmp_path) / "frames.jsonl").read_bytes().splitlines()
    assert len(lines) == 1 and lines[0].startswith(b'{"i":0')


def test_a_restart_reads_the_index_back_and_ends_a_torn_turn(tmp_path: Path) -> None:
    store = rc.RecordingStore(lambda: tmp_path)
    store.start_turn(KEY, TURN, 10, url="https://a.example/")
    store.add_frame(KEY, TURN, jpeg(), 64, 40, [], True, 20)
    again = rc.RecordingStore(lambda: tmp_path)
    (turn,) = again.list_turns(KEY)
    assert turn["live"] is False and turn["ended_ts"] == 20 and turn["frame_count"] == 1
    assert turn["start_url"] == "https://a.example/"


# ── on disk ────────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_directories_are_0700_and_files_0600(tmp_path: Path) -> None:
    store = rc.RecordingStore(lambda: tmp_path)
    store.start_turn(KEY, TURN, 1)
    store.add_frame(KEY, TURN, jpeg(), 64, 40, [[1, 1, 4, 4]], True, 2)
    store.add_event(KEY, TURN, {"ts": 3, "op": "click", "actor": "agent"})
    root = tmp_path / rc.RECORDINGS_DIRNAME
    paths = [root, *root.rglob("*")]
    assert any(p.is_file() for p in paths)
    for p in paths:
        mode = stat.S_IMODE(p.stat().st_mode)
        assert mode == (0o700 if p.is_dir() else 0o600), (p, oct(mode))


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_symlinked_frame_is_not_followed(tmp_path: Path) -> None:
    store = rc.RecordingStore(lambda: tmp_path)
    store.start_turn(KEY, TURN, 1)
    store.add_frame(KEY, TURN, jpeg(), 64, 40, [], True, 2)
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    frame = turn_dir(tmp_path) / "frames" / "0.jpg"
    frame.unlink()
    frame.symlink_to(secret)
    assert store.read_frame(KEY, TURN, 0) is None


def test_a_turn_id_that_is_not_an_id_records_nothing(tmp_path: Path) -> None:
    store = rc.RecordingStore(lambda: tmp_path)
    for bad in ("../x", "a/b", "", "." * 3, "x" * 65):
        assert store.start_turn(KEY, bad, 1) is False
    assert not (tmp_path / rc.RECORDINGS_DIRNAME).exists()


def test_recording_off_records_nothing(tmp_path: Path) -> None:
    store = rc.RecordingStore(lambda: tmp_path, rc.RecordingConfig(enabled=False))
    assert store.start_turn(KEY, TURN, 1) is False
    assert not (tmp_path / rc.RECORDINGS_DIRNAME).exists()


def test_the_config_section_drives_the_recorder() -> None:
    from kiro_crew.config.sections import BrowserConfig

    cfg = SimpleNamespace(
        browser=BrowserConfig.from_raw(
            {"recording": {"enabled": False, "max_session_mb": 5, "max_total_mb": "junk"}}
        )
    )
    got = rc.RecordingConfig.from_config(cfg)
    assert got.enabled is False and got.max_session_bytes == 5 * 1024 * 1024
    assert got.max_total_bytes == 1024 * 1024 * 1024


# ── the hub drives it ──────────────────────────────────────────────────────


async def test_the_hub_records_a_turn_with_its_ops_and_ends_it(tmp_path: Path) -> None:
    rec, store, _clock = make_recorder(tmp_path)
    hub = lh.LiveHub(clock=lambda: 1.0)
    hub.attach_recorder(rec)
    hub.turn_started(KEY, TURN)
    mark = hub.op_mark(KEY)
    hub.step(KEY, "Click e7")
    hub.record_op(KEY, "click", {"ref": "e7"}, {"ok": True, "x": 3.0, "y": 4.0}, mark)
    hub.page_changed(KEY, url="https://a.example/")
    hub.turn_ended(KEY)
    await rec.flush()
    detail = store.turn_detail(KEY, TURN)
    assert detail["live"] is False and detail["ended_ts"] is not None
    (event,) = detail["events"]
    assert event["label"] == "Click e7" and event["point"] == {"x": 3, "y": 4}
    assert store.list_turns(KEY)[0]["start_url"] == "https://a.example/"


# ── routes ─────────────────────────────────────────────────────────────────


def _identity(kind: str):
    @web.middleware
    async def middleware(request: web.Request, handler):
        if kind == "owner":
            request["user"], request["app"] = "local-app", ""
        elif kind == "app":
            request["user"], request["app"] = "some-app", "some-app"
        elif kind == "internal":
            request["internal_auth"] = True
            request["user"], request["app"] = "local-app", ""
        return await handler(request)

    return middleware


def _app(kind: str = "owner") -> web.Application:
    app = web.Application(middlewares=[_identity(kind)])
    app["state"] = SimpleNamespace(
        owner_id="", _slots={KEY: object()}, deliver_ws_owners=None, deliver_ws_owners_bytes=None
    )
    route_slice.register(app)
    return app


@pytest.fixture
def recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> rc.RecordingStore:
    monkeypatch.setattr(live_mod, "_sel", lambda: SimpleNamespace(log_api_access=lambda **_: None))
    store = rc.get_recorder().store
    store.start_turn(KEY, TURN, 1000, url="https://a.example/")
    store.add_frame(KEY, TURN, jpeg(), 64, 40, [[1, 1, 4, 4]], True, 1100)
    store.add_event(
        KEY,
        TURN,
        rc.agent_event(
            "click",
            {"ref": "e1"},
            {"ok": True, "x": 2, "y": 3},
            ts=1200,
            label="Click e1",
            url="https://a.example/",
            scale=1.0,
        ),
    )
    store.end_turn(KEY, TURN, 1300)
    return store


BASE = f"/api/browser/live/{KEY}/recordings"
PATHS = (BASE, f"{BASE}/{TURN}", f"{BASE}/{TURN}/frames/0.jpg")


async def test_the_owner_reads_the_contract_shapes(recorded) -> None:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(BASE)
        assert resp.status == 200 and resp.headers["Cache-Control"] == "no-store"
        (turn,) = (await resp.json())["turns"]
        assert {
            "turn_id",
            "started_ts",
            "ended_ts",
            "live",
            "frame_count",
            "event_count",
            "bytes",
            "start_url",
        } <= set(turn)
        assert turn["frame_count"] == 1 and turn["live"] is False and turn["ended_ts"] == 1300
        detail = await (await client.get(f"{BASE}/{TURN}")).json()
        assert {
            "turn_id",
            "started_ts",
            "ended_ts",
            "live",
            "width",
            "height",
            "frames",
            "events",
        } <= set(detail)
        assert detail["frames"] == [{"i": 0, "ts": 1100, "w": 64, "h": 40}]
        (event,) = detail["events"]
        assert {
            "id",
            "ts",
            "op",
            "label",
            "args",
            "point",
            "path",
            "url",
            "status",
            "actor",
        } <= set(event)
        assert event["op"] == "click" and event["point"] == {"x": 2, "y": 3}
        frame = await client.get(f"{BASE}/{TURN}/frames/0.jpg")
        assert frame.status == 200 and frame.content_type == "image/jpeg"
        assert frame.headers["Cache-Control"] == "no-store"
        assert (await frame.read())[:2] == b"\xff\xd8"


@pytest.mark.parametrize("kind", ["app", "anon"])
@pytest.mark.parametrize("path", [*PATHS, "/api/browser/live/chat-9/recordings"])
async def test_a_non_owner_is_refused_the_same_way_whether_it_exists(
    recorded, kind: str, path: str
) -> None:
    async with TestClient(TestServer(_app(kind))) as client:
        resp = await client.get(path)
        assert resp.status == 403 and (await resp.json())["code"] == "owner_only"


@pytest.mark.parametrize("path", PATHS)
async def test_the_internal_secret_is_refused(recorded, path: str) -> None:
    async with TestClient(TestServer(_app("internal"))) as client:
        resp = await client.get(path)
        assert resp.status == 403 and (await resp.json())["code"] == "internal_secret_required"


@pytest.mark.parametrize(
    "path",
    [
        "/api/browser/live/chat-9/recordings",  # not this dashboard's session
        f"/api/browser/live/%2e%2e/recordings/{TURN}",
        "/api/browser/live/..%2F..%2Fetc/recordings",
        f"{BASE}/..%2Fmeta.json",
        f"{BASE}/{'b' * 32}",
        f"{BASE}/{TURN}/frames/1.jpg",  # past the last frame
        f"{BASE}/{TURN}/frames/..%2Fmeta.json",
        f"{BASE}/{TURN}/frames/0.png",
        f"{BASE}/{TURN}/frames/-1.jpg",
        f"{BASE}/{TURN}/frames/%2e%2e%2f%2e%2e%2fmeta.json",
        f"{BASE}/{TURN}/frames/00000000.jpg",
    ],
)
async def test_a_miss_or_a_traversal_is_one_not_found(recorded, path: str) -> None:
    async with TestClient(TestServer(_app())) as client:
        resp = await client.get(path)
        assert resp.status == 404, (path, await resp.text())


# ── session delete ─────────────────────────────────────────────────────────


async def test_a_deleted_session_takes_its_recordings_with_it(recorded, tmp_path: Path) -> None:
    state = SimpleNamespace(_slots={})
    await slot_teardown._remove_browser_recordings(state, f"dashboard:{KEY}", {f"dashboard:{KEY}"})
    assert recorded.list_turns(KEY) == []
    assert not (tmp_path / rc.RECORDINGS_DIRNAME / rc.session_dirname(KEY)).exists()


async def test_a_key_a_live_slot_still_holds_keeps_its_recordings(recorded) -> None:
    state = SimpleNamespace(_slots={KEY: object()})
    await slot_teardown._remove_browser_recordings(state, KEY, {KEY})
    assert len(recorded.list_turns(KEY)) == 1


async def test_a_delete_during_a_running_turn_leaves_nothing_behind(tmp_path: Path) -> None:
    rec = rc.get_recorder()
    rec.turn_started(KEY, TURN)
    rec.frame(KEY, jpeg(), 64, 40, [], True)
    await rec.delete_session(KEY)
    rec.frame(KEY, jpeg((1, 1, 1)), 64, 40, [], True)  # a late frame from the stream
    rec.event(KEY, {"ts": 1, "op": "other", "actor": "agent"})
    await rec.flush()
    assert not (tmp_path / rc.RECORDINGS_DIRNAME / rc.session_dirname(KEY)).exists()


async def test_the_recordings_root_is_hidden_from_every_sandbox_and_file_tool() -> None:
    """Another chat's agent must not read or forge replays with a plain ``open()``.

    The replay routes are owner-only, which a sandboxed shell or a file tool would
    skip; the root is created on a session's first recorded turn, so it must also
    be precreated for the mask to have a name to bind over.
    """
    from kiro_crew import sandbox
    from kiro_crew.security.paths import sensitive_home_dirs

    assert rc.RECORDINGS_DIRNAME in sandbox._CREW_HIDDEN_LEAVES
    assert rc.RECORDINGS_DIRNAME in sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES
    assert any(Path(d).name == rc.RECORDINGS_DIRNAME for d in sensitive_home_dirs())


async def test_a_writer_that_falls_behind_drops_frames_and_marks_the_turn_truncated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Frames wait in memory only up to the queue limit; past it they are dropped and
    the replay says it is incomplete, so a stalled disk cannot fill the gateway."""
    import threading

    rec, store, clock = make_recorder(tmp_path)
    monkeypatch.setattr(rc, "QUEUED_FRAMES_MAX", 2)
    gate = threading.Event()
    real_add = store.add_frame

    def _slow_add(*args: Any, **kwargs: Any) -> bool:
        gate.wait(5)  # flake-ok: released below once the frames are queued
        return real_add(*args, **kwargs)

    monkeypatch.setattr(store, "add_frame", _slow_add)
    rec.turn_started(KEY, TURN, "https://a.example/")
    for i in range(6):
        clock.now += 1000
        rec.frame(KEY, jpeg((i * 40, 0, 0)), 64, 40, [], True)
    assert rec._recs[KEY].queued_frames <= 2
    # Only the queued writes hold a frame: a dropped frame's truncation mark holds
    # its turn id alone, and the drops queue one mark between them.
    jobs = list(rec._recs[KEY].jobs)
    held = [
        job
        for job in jobs
        if any(isinstance(c.cell_contents, rc._PendingFrame) for c in (job.__closure__ or ()))
    ]
    assert len(held) <= 2
    assert len(jobs) - len(held) <= 2
    gate.set()
    await rec.flush()
    assert rec._recs[KEY].queued_frames == 0 and rec._recs[KEY].queued_bytes == 0
    assert store.turn_detail(KEY, TURN)["truncated"] is True
