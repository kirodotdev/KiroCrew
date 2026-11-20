"""Input pacing for the live hub: pointer moves sent ahead and wheel turns that glide.

:meth:`kiro_crew.browser_harness.live_hub.LiveHub.input_event` hands a move or a
wheel turn here so the page gets the newest position every frame instead of one
jump per event. ``live_hub`` stays the import path for every name.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from typing import TYPE_CHECKING, Any

from kiro_crew.browser_harness.takeover import USER, dispatch_input

if TYPE_CHECKING:
    from kiro_crew.browser_harness.live_hub import LiveSession
    from kiro_crew.browser_harness.screencast import LiveStream
    from kiro_crew.browser_harness.takeover import HeldInput

logger = logging.getLogger("kiro_crew.browser_harness.live_hub")

#: Pointer moves sent ahead of the page's acknowledgement (see ``input_event``).
MOVES_IN_FLIGHT = 2
#: A wheel turn glides (see ``_wheel_glide``) along a critically damped curve with
#: this time constant: it starts slow, is fastest one time constant in, and settles
#: in about half a second. At 60 ms two neighbouring frames of a 30 fps view differ
#: by less than twice, and the first step is small enough for Chrome to draw at once.
WHEEL_TAU_S = 0.060
#: Time between two steps of a glide. Chrome takes the newest wheel at each frame it
#: draws, so steps this close together give every frame its own move.
WHEEL_MIN_GAP_S = 0.008
#: Wheel steps sent ahead of the page's acknowledgement, so a page that takes 40-70
#: ms for one step still gets a step every frame. Chrome adds up the wheels queued
#: for one frame, so steps sent ahead never land as separate jumps.
WHEELS_IN_FLIGHT = 6
#: Wheel distance this close to a glide's end goes in one step: a 1 px step every
#: other frame reads as a still frame inside the scroll.
WHEEL_SNAP_PX = 3


async def _settle(tasks: list[asyncio.Task[int]]) -> None:
    """Wait for moves already sent; one the page refused is logged, never raised."""
    for t in tasks:
        try:
            await t
        except Exception:  # noqa: BLE001 - a lost move costs one pointer position
            logger.debug("browser live: a pointer move was not taken", exc_info=True)


def _clock() -> float:
    """The glide's clock: the running loop's. A test swaps it with ``_sleep``."""
    return asyncio.get_running_loop().time()


async def _sleep(seconds: float) -> None:
    """The glide's wait between steps, on the clock of :func:`_clock`."""
    await asyncio.sleep(seconds)


async def _add_turn(
    sess: LiveSession,
    stream: LiveStream,
    event: dict[str, Any],
    dx: int,
    dy: int,
    scale: float,
    held: HeldInput,
) -> None:
    """Queue one wheel turn on the session's glide, starting the glide if none runs.

    The turn and its pointer spot are kept in viewport CSS pixels at the turn's own
    frame's *scale*: the card's frame width can change between two turns of one
    glide. A turn at another spot or with other modifiers first finishes the glide
    in flight, so no earlier turn's rest lands where the later one points.
    """
    css = scale if scale > 0 else 1.0
    at = {**event, "x": round(event.get("x", 0) / css), "y": round(event.get("y", 0) / css)}
    spot = (at["x"], at["y"], at.get("modifiers"))
    prev = sess.wheel_at
    if sess.wheel_marks and spot != (prev.get("x"), prev.get("y"), prev.get("modifiers")):
        await _wheel_settle(sess)
    if dx or dy:
        sess.wheel_marks.append((_clock(), round(dx / css), round(dy / css)))
    sess.wheel_at = at
    if sess.wheel_task is None or sess.wheel_task.done():
        sess.wheel_task = asyncio.ensure_future(_wheel_glide(sess, stream, held))


def _wheel_curve(t: float) -> float:
    """How far along a glide is *t* seconds in, 0 to 1 (critically damped)."""
    x = max(t, 0.0) / WHEEL_TAU_S
    return 1.0 - (1.0 + x) * math.exp(-x)


def _wheel_target(marks: list[tuple[float, int, int]], t: float) -> tuple[float, float]:
    """How far the wheel turns in *marks* have taken the page by time *t* (CSS px).

    Each turn glides on its own curve from when it came, and their glides add up: a
    turn that comes while another glides speeds the page up smoothly, never all at once.
    """
    x = y = 0.0
    for at, dx, dy in marks:
        k = _wheel_curve(t - at)
        x += dx * k
        y += dy * k
    return x, y


async def _wheel_glide(sess: LiveSession, stream: LiveStream, held: HeldInput) -> None:
    """Send the wheel turns in ``sess.wheel_marks`` as small steps until all have glided.

    Turns and the pointer spot are in viewport CSS pixels (``input_event`` converts
    each at its own frame's scale), so steps go out at scale 1. Each step sends what
    the turns' curves (``_wheel_target``) moved through since the last one, so where
    the page is at each frame is a smooth function of time however the steps fall.
    Steps go every :data:`WHEEL_MIN_GAP_S`, up to :data:`WHEELS_IN_FLIGHT` ahead of
    the page's acknowledgement, so a slow page delays a step without leaving a frame
    without one.
    """
    inflight: list[asyncio.Task[int]] = []
    sent_at = -1.0
    try:
        while True:
            if not sess.wheel_marks:
                await _settle(inflight)
                inflight = []
                if not sess.wheel_marks:
                    return
            if sess.lock.owner != USER or sess.stream is not stream:
                sess.wheel_marks = []
                sess.wheel_sent = [0, 0]
                return
            now = _clock()
            total = (sum(m[1] for m in sess.wheel_marks), sum(m[2] for m in sess.wheel_marks))
            tx, ty = _wheel_target(sess.wheel_marks, now)
            done = (
                sess.wheel_now
                or all(_wheel_curve(now - m[0]) >= 0.9995 for m in sess.wheel_marks)
                or (abs(total[0] - tx) <= WHEEL_SNAP_PX and abs(total[1] - ty) <= WHEEL_SNAP_PX)
            )
            if done:
                tx, ty = total
            take = (int(round(tx)) - sess.wheel_sent[0], int(round(ty)) - sess.wheel_sent[1])
            if done:
                sess.wheel_marks = []
                sess.wheel_sent = [0, 0]
            else:
                sess.wheel_sent = [sess.wheel_sent[0] + take[0], sess.wheel_sent[1] + take[1]]
            if take != (0, 0):
                inflight = [t for t in inflight if not t.done()]
                if len(inflight) >= WHEELS_IN_FLIGHT:
                    await _settle(inflight[:1])
                    inflight = inflight[1:]
                task = asyncio.ensure_future(
                    dispatch_input(
                        stream.relay,
                        stream.session_id,
                        {**sess.wheel_at, "dx": take[0], "dy": take[1]},
                        scale=1.0,
                        held=held,
                    )
                )
                # A step the page refused costs that much scroll; read here so it is
                # never an unretrieved task error.
                task.add_done_callback(lambda t: t.cancelled() or t.exception())
                inflight.append(task)
                stream.note_input()
                sent_at = _clock()
            if sess.wheel_marks and not sess.wheel_now:
                # The gap runs from the last send, so a wait for the page never
                # bunches the steps that follow it into one frame.
                gap = WHEEL_MIN_GAP_S if sent_at < 0 else sent_at + WHEEL_MIN_GAP_S - _clock()
                await _sleep(max(0.0, gap))
    finally:
        sess.wheel_now = False


async def _wheel_settle(sess: LiveSession, *, drop: bool = False) -> None:
    """Finish a gliding wheel now: the rest in one step, or (*drop*) none of it."""
    task = sess.wheel_task
    if task is None or task.done():
        return
    if drop:
        sess.wheel_marks = []
        sess.wheel_sent = [0, 0]
    sess.wheel_now = True
    with contextlib.suppress(Exception):
        await task
