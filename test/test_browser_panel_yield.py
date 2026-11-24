"""A page nobody looks at gives the page browser's one compositing thread back to a page that moves."""

from __future__ import annotations

import asyncio

import pytest
from browser_panel_helpers import PanelRelay
from test_browser_panel_stream import counts, hold_forever, make_stream, push

from kiro_crew.browser_harness import panel_stream as ps

pytestmark = pytest.mark.asyncio


def frames(stream: ps.PanelStream, clock_now: float, make_s: float, count: int) -> float:
    """*count* frames of the cast, each *make_s* after it was asked for. Returns the time of the last."""
    now = clock_now
    for _ in range(count):
        stream._chrome_acked_at = now
        now += make_s
        stream._frame_in_at = now
        stream._note_make(now)
    return now


async def moving_and_held() -> tuple[ps.PanelStream, ps.PanelStream, float]:
    moving, relay, clock, _sink = await make_stream()
    held, _relay, _clock, _sink2 = await make_stream()
    # Both are pages of one page browser.
    held.relay = relay
    ps._BROWSER_PAGES[relay].add(held)
    for stream in (moving, held):
        stream.visible = True
    moving.watched = True
    assert moving.held is False and held.held is True
    return moving, held, clock.now


async def test_a_held_page_waits_a_second_while_a_page_that_moves_is_short_for_the_browser() -> (
    None
):
    moving, held, now = await moving_and_held()
    period = 1.0 / moving.pace.aim()
    assert held.idle_period() == pytest.approx(ps.IDLE_PERIOD_S)
    # Seven late pictures are not eight.
    now = frames(moving, now, period * 1.5, ps.BROWSER_SHORT_PICTURES - 1)
    assert moving.short_for_browser(now) is False
    now = frames(moving, now, period * 1.5, 1)
    assert moving.short_for_browser(now) is True
    assert held.idle_period() == pytest.approx(ps.HELD_YIELD_PERIOD_S)
    assert ps.HELD_YIELD_PERIOD_S >= ps.HOLD_WITHOUT_CAST_MIN_S
    # The page stops moving: a second after its last frame the held page is back at its own pace.
    assert moving.short_for_browser(now + ps.BROWSER_MOVES_S - 0.1) is True
    assert moving.short_for_browser(now + ps.BROWSER_MOVES_S) is False
    # It moves on, with the thread to itself: after the hold the held page comes back.
    now = frames(moving, now, period * 0.5, int(ps.HELD_YIELD_S / (period * 0.5)) + 16)
    assert moving.short_for_browser(now) is False


async def test_held_pages_stay_away_twice_as_long_when_their_return_made_the_page_short_again() -> (
    None
):
    moving, _held, now = await moving_and_held()
    late = 1.5 / moving.pace.aim()
    now = frames(moving, now, late, ps.BROWSER_SHORT_PICTURES)
    # They came back, and within a few pictures the page is short again.
    now = frames(moving, now + ps.HELD_YIELD_S, late, ps.BROWSER_SHORT_PICTURES)
    assert moving._yield_s == 2 * ps.HELD_YIELD_S
    # Never longer than the most.
    for _ in range(8):
        now = frames(moving, now + moving._yield_s, late, ps.BROWSER_SHORT_PICTURES)
    assert moving._yield_s == ps.HELD_YIELD_MAX_S
    # Short again long after they came back is a new matter: the first hold.
    now = frames(
        moving, now + ps.HELD_YIELD_MAX_S + ps.HELD_RETURN_S + 1, late, ps.BROWSER_SHORT_PICTURES
    )
    assert moving._yield_s == ps.HELD_YIELD_S


async def test_a_page_the_browser_keeps_up_with_makes_no_held_page_wait() -> None:
    moving, held, now = await moving_and_held()
    period = 1.0 / moving.pace.aim()
    now = frames(moving, now, period * 0.5, ps.BROWSER_SHORT_PICTURES * 3)
    assert moving.short_for_browser(now) is False
    # At an aim of 30 a picture every 26th of a second is late for the aim and not under 24.
    assert moving.pace.aim() == 30
    now = frames(moving, now, 1 / 26, ps.BROWSER_SHORT_PICTURES * 3)
    assert moving.short_for_browser(now) is False
    assert held.idle_period() == pytest.approx(ps.IDLE_PERIOD_S)
    # A page that was quiet (its frame came long after it was asked for) says nothing of the browser.
    now = frames(moving, now, ps.BROWSER_MAKE_MAX_S + 0.1, 1)
    now = frames(moving, now, period * 1.5, ps.BROWSER_SHORT_PICTURES - 1)
    assert moving.short_for_browser(now) is False
    now = frames(moving, now, period * 1.5, 1)
    assert moving.short_for_browser(now) is True


async def test_a_held_page_is_never_short_and_another_browsers_page_does_not_wait() -> None:
    moving, held, now = await moving_and_held()
    other, _relay, _clock, _sink = await make_stream()
    other.visible = True
    period = 1.0 / moving.pace.aim()
    # The held page's own late pictures count for nothing.
    frames(held, now, period * 1.5, ps.BROWSER_SHORT_PICTURES * 2)
    assert held.short_for_browser(now) is False
    now = frames(moving, now, period * 1.5, ps.BROWSER_SHORT_PICTURES)
    assert held.idle_period() == pytest.approx(ps.HELD_YIELD_PERIOD_S)
    assert other.idle_period() == pytest.approx(ps.IDLE_PERIOD_S)
    # A page that moves and left the view makes no page wait.
    moving.visible = False
    assert held.idle_period() == pytest.approx(ps.IDLE_PERIOD_S)


# ── a frame of the cast is what is noted ────────────────────────────────────


async def test_frames_of_the_cast_that_come_late_make_the_page_short_and_frames_in_time_do_not() -> (
    None
):
    late, relay, clock, sink = await make_stream()
    late.watched = True
    for i in range(ps.BROWSER_SHORT_PICTURES + 1):
        # The browser's next frame comes 60 ms after it was asked for: later than a period of 24 a second.
        clock.now += 0.06
        await push(relay, payload=500 + i)
    assert len(sink.frames) == ps.BROWSER_SHORT_PICTURES + 1
    assert late.short_for_browser(clock.now) is True

    quick, relay2, clock2, _sink2 = await make_stream()
    quick.watched = True
    for i in range(ps.BROWSER_SHORT_PICTURES * 2):
        clock2.now += 0.01
        await push(relay2, payload=500 + i)
    assert quick.short_for_browser(clock2.now) is False


# ── each guard alone ────────────────────────────────────────────────────────


async def test_he_looks_away_from_the_short_page_and_the_held_pages_come_back_at_once() -> None:
    moving, held, now = await moving_and_held()
    now = frames(moving, now, 1.5 / moving.pace.aim(), ps.BROWSER_SHORT_PICTURES)
    assert held.idle_period() == pytest.approx(ps.HELD_YIELD_PERIOD_S)
    # He looks at another page: this one is held itself now, in view and running as before.
    moving.watched = False
    assert moving.held and moving.visible and moving.running
    assert moving.short_for_browser(now) is False
    assert held.idle_period() == pytest.approx(ps.IDLE_PERIOD_S)


async def test_a_page_that_closed_while_short_does_not_keep_the_others_held() -> None:
    moving, held, now = await moving_and_held()
    now = frames(moving, now, 1.5 / moving.pace.aim(), ps.BROWSER_SHORT_PICTURES)
    assert held.idle_period() == pytest.approx(ps.HELD_YIELD_PERIOD_S)
    await moving.stop()
    # Nothing but its having stopped says so: it was in view and looked at when it closed.
    moving.visible, moving.watched = True, True
    assert moving.running is False and moving.held is False
    assert moving.short_for_browser(now) is False
    assert held.idle_period() == pytest.approx(ps.IDLE_PERIOD_S)


async def test_late_frames_of_a_page_while_it_is_held_do_not_count_once_he_looks_at_it() -> None:
    page, _held, now = await moving_and_held()
    late = 1.5 / page.pace.aim()
    page.watched = False
    now = frames(page, now, late, ps.BROWSER_SHORT_PICTURES - 1)
    page.watched = True
    now = frames(page, now, late, 1)
    assert page.short_for_browser(now) is False


async def test_late_frames_of_a_page_out_of_view_do_not_count_once_it_is_back() -> None:
    page, _held, now = await moving_and_held()
    late = 1.5 / page.pace.aim()
    page.visible = False
    now = frames(page, now, late, ps.BROWSER_SHORT_PICTURES - 1)
    page.visible = True
    now = frames(page, now, late, 1)
    assert page.short_for_browser(now) is False


# ── the other side: a held page that yields is fresh the moment he turns to it ──


async def yielding() -> tuple[ps.PanelStream, PanelRelay, ps.PanelStream]:
    """A held page of the same page browser as a page that moves and is short: its wait is the yield's."""
    held, relay, clock, _sink = await make_stream()
    moving, _relay, _clock, _sink2 = await make_stream()
    ps._BROWSER_PAGES[held.relay].add(moving)
    moving.visible, moving.watched = True, True
    frames(moving, clock.now, 1.5 / moving.pace.aim(), ps.BROWSER_SHORT_PICTURES)
    assert held.idle_period() == pytest.approx(ps.HELD_YIELD_PERIOD_S)
    return held, relay, moving


@pytest.mark.parametrize("how", ["he looks at it", "his pointer comes onto it"])
async def test_a_page_that_yields_is_fresh_the_moment_he_turns_to_it(how: str) -> None:
    held, relay, _moving = await yielding()
    asleep, _never, naps = hold_forever(held)
    stops, starts, acks = counts(relay)
    pushing = asyncio.ensure_future(push(relay, payload=500))
    await asyncio.wait_for(asleep.wait(), 5)
    # Its picture is out and its cast is stopped for the yield's wait.
    assert naps[0] == pytest.approx(ps.HELD_YIELD_PERIOD_S)
    assert counts(relay) == (stops + 1, starts, acks)
    assert held.holding and not held.casting
    if how == "he looks at it":
        # As the hub's `set_watched` does.
        held.watched = True
    else:
        held.idle = False
    held.hurry()
    await asyncio.wait_for(pushing, 5)
    # The cast starts at once: its first picture is the page as it is now. And it is held no more.
    assert counts(relay) == (stops + 1, starts + 1, acks)
    assert held.casting and not held.holding and held.held is False


async def test_a_page_he_looks_at_is_never_made_to_wait_by_a_page_that_is_short() -> None:
    looked, relay, clock, sink = await make_stream()
    moving, _relay, _clock, _sink2 = await make_stream()
    ps._BROWSER_PAGES[looked.relay].add(moving)
    moving.visible, moving.watched = True, True
    looked.watched = True
    frames(moving, clock.now, 1.5 / moving.pace.aim(), ps.BROWSER_SHORT_PICTURES)
    assert moving.short_for_browser(clock.now) is True
    stops, _starts, acks = counts(relay)
    for i in range(3):
        await push(relay, payload=500 + i)
    # Each picture went out and was acked to the browser, with no stop of its cast and no wait of the yield's.
    assert len(sink.frames) == 3
    assert counts(relay)[0] == stops and counts(relay)[2] == acks + 3
    assert all(nap < ps.HOLD_WITHOUT_CAST_MIN_S for nap in clock.sleeps)
