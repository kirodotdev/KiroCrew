"""The Cursor Motion PATH MODEL — pure geometry, zero platform contact.

This module is deliberately the boring half of Cursor Motion: given a start and
an end point it produces a **sampled list of screen points** plus a duration, and
that is all. No ctypes, no subprocess, no AppKit, no config read, no clock beyond
what the caller passes in. Everything here is a total function of its arguments,
which is what makes the visual behaviour of the feature unit-testable on a Linux
CI shard with no display at all.

The split is load-bearing. ``overlay_proc`` (AppKit, out of process) and
``overlay`` (the gateway-side supervisor) are the parts that can fail for
environmental reasons; keeping the *shape* of the motion here means a regression
in how the cursor moves is caught by an assertion on numbers rather than by
somebody watching a screen.

The model, reimplemented from the reference project's ``CursorMotionModel.swift``
(read for the algorithm, not copied):

* **One cubic Bezier** from ``start`` to ``end``, bowed sideways by a
  perpendicular *arc* whose magnitude is ``clamp(distance * 0.22, 28, 110) *
  curve_scale``. A straight interpolation reads as a teleport and is exactly what
  makes a synthetic cursor look synthetic; the arc is the whole illusion.
* **Asymmetric control points** (0.18/0.10 and 0.80/0.96 along/across the chord)
  so the cursor leaves quickly and arrives settling. ``curve_scale == 0``
  collapses the handles to the classic thirds placement, i.e. an exact straight
  line — the escape hatch for "I want no flourish".
* **A progress spring** (velocity-Verlet, fixed 1/240s step,
  ``response=1.4``/``damping=0.9``) integrated from 0 to 1 and then fed through
  the Bezier. Because the spring drives PROGRESS rather than position, its slight
  numerical overshoot past 1.0 is *clamped* before sampling, so the drawn cursor
  can never overshoot its target position — an important property when the point
  of the animation is to show the user where a click is about to land.

Coordinate convention: every point in this module is in **top-left screen
coordinates** (y grows downward), because that is what the rest of computer use
uses — the AX/CG surfaces, ``screencapture -R`` and the element frames all agree
on it. The bottom-left flip that AppKit's ``NSWindow`` origin needs happens in
``overlay_proc``, at the one place that actually talks to AppKit, and nowhere
else.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from kiro_crew.computer_use.types import (
    CURVE_AMOUNT_MAX,
    CURVE_AMOUNT_MIN,
    CURVE_C1_ACROSS,
    CURVE_C1_ALONG,
    CURVE_C2_ACROSS,
    CURVE_C2_ALONG,
    CURVE_C2_OFFSET_RATIO,
    CURVE_DISTANCE_RATIO,
    DEFAULT_CURVE_SCALE,
    DEFAULT_PATH_SAMPLES,
    FULL_SPEED_DISTANCE,
    MAX_CURVE_SCALE,
    MAX_MOVE_DURATION_MS,
    MAX_PATH_SAMPLES,
    MIN_MOTION_DISTANCE,
    MIN_MOVE_DURATION_MS,
    MIN_PATH_SAMPLES,
    MOTION_EPSILON,
    SPRING_DAMPING_FRACTION,
    SPRING_DT,
    SPRING_FALLBACK_SETTLE_SECS,
    SPRING_MAX_STEPS,
    SPRING_MAX_STIFFNESS,
    SPRING_RESPONSE,
    SPRING_SETTLE_DISTANCE,
    STRAIGHT_C1_FRACTION,
    STRAIGHT_C2_FRACTION,
    STRAIGHT_MOVE_DISTANCE,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CursorPath",
    "MotionPlan",
    "SpringConfig",
    "build_path",
    "plan_motion",
    "sample_path",
    "settle_time",
    "spring_progress_curve",
]


@dataclass(frozen=True)
class SpringConfig:
    """The progress spring's two tunables plus everything derived from them.

    ``stiffness`` and ``drag`` are computed in :meth:`create` rather than stored
    by hand so the pair can never disagree with ``response``/``damping``: a
    hand-set drag that does not match the stiffness is precisely how a "settle"
    animation turns into a visible bounce or an eternal crawl.
    """

    response: float = SPRING_RESPONSE
    damping: float = SPRING_DAMPING_FRACTION
    dt: float = SPRING_DT
    stiffness: float = 0.0
    drag: float = 0.0

    @classmethod
    def create(
        cls,
        *,
        response: float = SPRING_RESPONSE,
        damping: float = SPRING_DAMPING_FRACTION,
        dt: float = SPRING_DT,
    ) -> "SpringConfig":
        """Build a config with ``stiffness``/``drag`` derived from *response*.

        Every input is floored: a non-positive ``response`` would make stiffness
        infinite (``(2*pi/0)**2``) and the first integrator step NaN, and a
        non-positive ``dt`` would loop forever. Both are clamped rather than
        rejected, because this is a cosmetic subsystem and a caller passing a
        silly number should get an ugly animation, never an exception on the path
        of a tool call.
        """
        safe_response = max(float(response), MOTION_EPSILON)
        safe_dt = max(float(dt), MOTION_EPSILON)
        stiffness = min((2.0 * math.pi / safe_response) ** 2, SPRING_MAX_STIFFNESS)
        drag = 2.0 * max(float(damping), 0.0) * math.sqrt(stiffness)
        return cls(
            response=safe_response,
            damping=max(float(damping), 0.0),
            dt=safe_dt,
            stiffness=stiffness,
            drag=drag,
        )


@dataclass(frozen=True)
class CursorPath:
    """A cubic Bezier in top-left screen coordinates.

    Holding the control points (rather than only the sampled result) keeps the
    curve re-samplable at a different density and makes the geometry directly
    assertable in a test — the arc offset is the one number whose regression a
    human would notice and no other assertion would catch.
    """

    start: tuple[float, float]
    end: tuple[float, float]
    control1: tuple[float, float]
    control2: tuple[float, float]
    curve_scale: float = DEFAULT_CURVE_SCALE
    arc_amount: float = 0.0

    def point_at(self, t: float) -> tuple[float, float]:
        """The curve point at parameter *t*, clamped to ``[0, 1]``.

        Clamped rather than extrapolated: a ``t`` past 1 on a cubic Bezier flies
        off along the end tangent, which for a fake cursor means jumping past the
        thing it is supposed to be pointing at. The endpoints are returned
        EXACTLY (not evaluated) so ``point_at(0)``/``point_at(1)`` are bit-equal
        to ``start``/``end`` and cannot drift by a float epsilon.
        """
        if t <= 0.0:
            return self.start
        if t >= 1.0:
            return self.end
        clamped = float(t)
        omt = 1.0 - clamped
        a = omt * omt * omt
        b = 3.0 * omt * omt * clamped
        c = 3.0 * omt * clamped * clamped
        d = clamped * clamped * clamped
        return (
            a * self.start[0] + b * self.control1[0] + c * self.control2[0] + d * self.end[0],
            a * self.start[1] + b * self.control1[1] + c * self.control2[1] + d * self.end[1],
        )


@dataclass(frozen=True)
class MotionPlan:
    """A fully resolved animation: where to draw, and for how long.

    This is the ONLY thing the supervisor sends to the overlay process, which is
    why it is pre-sampled: the overlay is a dumb renderer with no Bezier in it, so
    every decision about the shape of the motion stays in this testable module.
    """

    points: tuple[tuple[float, float], ...]
    duration_ms: int
    path: CursorPath


def curve_amount(distance: float, curve_scale: float = DEFAULT_CURVE_SCALE) -> float:
    """The perpendicular arc magnitude for a move of *distance* pixels.

    ``clamp(distance * 0.22, 28, 110) * curve_scale`` — the reference's exact
    formula, kept as three named constants in ``types``. The floor is what makes a
    short nudge still read as a movement rather than a jump; the ceiling is what
    stops a cross-screen sweep from bowing into a semicircle.
    """
    scale = min(max(float(curve_scale), 0.0), MAX_CURVE_SCALE)
    raw = max(float(distance), 0.0) * CURVE_DISTANCE_RATIO
    return min(max(raw, CURVE_AMOUNT_MIN), CURVE_AMOUNT_MAX) * scale


def build_path(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    curve_scale: float = DEFAULT_CURVE_SCALE,
    curve_direction: float = 0.0,
) -> CursorPath:
    """Build the bowed cubic Bezier from *start* to *end*.

    *curve_direction* picks which side of the chord the bow falls on: positive is
    the left-hand normal, negative the right-hand one, and ``0`` means "derive it
    from the travel direction" (rightward moves bow one way, leftward the other,
    so a there-and-back pair traces two different arcs instead of retracing one
    line — the same trick the reference uses, and the reason repeated moves do not
    look like a metronome).

    Degenerate input is handled rather than guarded against by the caller: a
    zero-length move has no direction, so the normal would be ``0/0``. Distance is
    floored at 1px and the direction falls back to +x, which yields a valid (if
    pointless) path instead of a curve full of NaNs. That matters because NaN
    coordinates reach AppKit as an un-placeable window rather than as an error.
    """
    sx, sy = float(start[0]), float(start[1])
    ex, ey = float(end[0]), float(end[1])
    dx, dy = ex - sx, ey - sy
    raw_distance = math.hypot(dx, dy)
    distance = max(raw_distance, MIN_MOTION_DISTANCE)

    if raw_distance <= MOTION_EPSILON:
        # No travel at all: there is no direction to derive a normal from (the
        # naive normalize is 0/0), and more importantly there is nothing to
        # animate. Collapse to a degenerate all-endpoints path so every sample is
        # the same finite point — bowing a zero-length move would draw a pointless
        # 28px loop around a stationary target.
        return CursorPath(
            start=(sx, sy),
            end=(ex, ey),
            control1=(sx, sy),
            control2=(ex, ey),
            curve_scale=0.0,
            arc_amount=0.0,
        )
    # Left-hand perpendicular of the unit travel vector.
    norm_x, norm_y = -dy / raw_distance, dx / raw_distance

    direction = float(curve_direction)
    if abs(direction) <= MOTION_EPSILON:
        direction = 1.0 if dx >= 0.0 else -1.0
    else:
        direction = 1.0 if direction > 0.0 else -1.0

    scale = min(max(float(curve_scale), 0.0), MAX_CURVE_SCALE)
    amount = curve_amount(distance, scale)
    off_x = norm_x * amount * direction
    off_y = norm_y * amount * direction

    if scale <= MOTION_EPSILON:
        # curve_scale == 0: thirds placement with no offset is an exactly straight
        # line. Kept as an explicit branch (rather than relying on amount == 0)
        # so "no flourish" also means "no asymmetric easing in space".
        c1 = (sx + dx * STRAIGHT_C1_FRACTION, sy + dy * STRAIGHT_C1_FRACTION)
        c2 = (sx + dx * STRAIGHT_C2_FRACTION, sy + dy * STRAIGHT_C2_FRACTION)
        return CursorPath(
            start=(sx, sy),
            end=(ex, ey),
            control1=c1,
            control2=c2,
            curve_scale=0.0,
            arc_amount=0.0,
        )

    c1 = (sx + dx * CURVE_C1_ALONG + off_x, sy + dy * CURVE_C1_ACROSS + off_y)
    c2 = (
        sx + dx * CURVE_C2_ALONG + off_x * CURVE_C2_OFFSET_RATIO,
        sy + dy * CURVE_C2_ACROSS + off_y * CURVE_C2_OFFSET_RATIO,
    )
    return CursorPath(
        start=(sx, sy),
        end=(ex, ey),
        control1=c1,
        control2=c2,
        curve_scale=scale,
        arc_amount=amount,
    )


def spring_progress_curve(config: "SpringConfig | None" = None) -> tuple[float, ...]:
    """Integrate the progress spring 0 -> 1 and return every sampled value.

    Velocity-Verlet at a FIXED ``dt``: the shape of the easing must not depend on
    how fast the renderer happens to be running, so time is simulated here and the
    renderer only decides how many of these samples it draws.

    The returned tuple always starts at exactly ``0.0`` and ends at exactly
    ``1.0``. The final clamp is not cosmetic: the spring settles at
    ``1.0000139`` for the shipped constants, and feeding a ``t > 1`` into a cubic
    Bezier extrapolates past the target — the fake cursor would visibly shoot
    past the element it is pointing at. Clamping progress (rather than clamping
    the position afterwards) keeps the overshoot out of the model entirely.

    Bounded by ``SPRING_MAX_STEPS`` so a pathological configuration (a caller's
    absurd ``response``, or a damping of 0 that never settles) terminates with a
    usable-if-ugly curve instead of looping.
    """
    cfg = config or SpringConfig.create()
    values: list[float] = [0.0]
    current = 0.0
    velocity = 0.0
    force = 0.0
    half_dt = cfg.dt * 0.5
    for _ in range(SPRING_MAX_STEPS):
        velocity_half = velocity + force * half_dt
        current = current + velocity_half * cfg.dt
        force = cfg.stiffness * (1.0 - current) - cfg.drag * velocity_half
        velocity = velocity_half + force * half_dt
        if not math.isfinite(current):
            # A caller-supplied configuration diverged. Bail with what we have;
            # the sampler tolerates a short curve and the animation degrades to a
            # quick move rather than raising into a tool call.
            logger.debug("cursor-motion spring diverged; truncating progress curve")
            break
        values.append(current)
        if current >= 1.0 and abs(1.0 - current) <= SPRING_SETTLE_DISTANCE:
            break
    values[-1] = 1.0
    return tuple(values)


def settle_time(config: "SpringConfig | None" = None) -> float:
    """Simulated seconds for the progress spring to settle at 1.0.

    Derived from the same integration the easing uses, so the animation's
    DURATION and its SHAPE can never drift apart — a duration picked
    independently would either cut the settle off mid-ring-down or hold the
    overlay after the cursor has stopped moving. Falls back to the measured
    constant if the integrator bailed early.
    """
    cfg = config or SpringConfig.create()
    curve = spring_progress_curve(cfg)
    steps = len(curve) - 1
    if steps <= 0:
        return SPRING_FALLBACK_SETTLE_SECS
    return steps * cfg.dt


def sample_path(
    path: CursorPath,
    *,
    samples: int = DEFAULT_PATH_SAMPLES,
    config: "SpringConfig | None" = None,
) -> tuple[tuple[float, float], ...]:
    """Sample *path* at *samples* points, eased by the progress spring.

    The k-th output point is ``path.point_at(progress[k'])`` where ``k'`` walks the
    spring's progress curve at uniform *time* intervals — so the points are
    unevenly spaced in DISTANCE (dense at the start and end, sparse in the middle)
    and evenly spaced in TIME. That is what produces ease-in/ease-out when a
    renderer draws them at a constant frame rate, and it is why the renderer needs
    no easing logic of its own.

    The first and last samples are the path's exact endpoints. The endpoint
    guarantee is what the click pulse depends on: the pulse is drawn at the last
    sampled point, so a sampler that stopped a pixel short would put the visual
    click next to the element rather than on it.
    """
    count = min(max(int(samples), MIN_PATH_SAMPLES), MAX_PATH_SAMPLES)
    curve = spring_progress_curve(config)
    last = len(curve) - 1
    out: list[tuple[float, float]] = []
    for step in range(count):
        # Uniform in time across the spring's own step count, so a longer settle
        # simply spreads the same number of drawn points over more of the curve.
        idx = 0 if count == 1 else int(round(step * last / (count - 1)))
        out.append(path.point_at(curve[min(idx, last)]))
    out[0] = path.start
    out[-1] = path.end
    return tuple(out)


def plan_motion(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    curve_scale: float = DEFAULT_CURVE_SCALE,
    curve_direction: float = 0.0,
    samples: int = DEFAULT_PATH_SAMPLES,
    config: "SpringConfig | None" = None,
) -> MotionPlan:
    """Build the path, sample it, and clamp the duration — the one entry point.

    Duration comes from :func:`settle_time` (the spring's own settle point) and is
    then clamped to ``[100, 2000]`` ms. The clamp is a product decision recorded
    in ``types``: below 100ms the motion reads as a teleport and the affordance is
    lost, and nothing purely cosmetic is allowed to hold a caller for longer than
    2s no matter what the spring says.
    """
    cfg = config or SpringConfig.create()
    distance = math.hypot(float(end[0]) - float(start[0]), float(end[1]) - float(start[1]))

    # A short hop is drawn STRAIGHT. ``curve_amount`` floors the arc at 28px, so a
    # 1-3px nudge would otherwise bow ~28px out and back — a visible curlicue on a
    # move the eye reads as "it barely moved". Verified before the change: a 1px
    # move produced arc=28.0.
    if distance < STRAIGHT_MOVE_DISTANCE:
        curve_scale = 0.0
    path = build_path(start, end, curve_scale=curve_scale, curve_direction=curve_direction)
    points = sample_path(path, samples=samples, config=cfg)

    # Scale the duration by distance. The spring's settle point is
    # distance-INDEPENDENT, so using it raw gave a 1px nudge the same ~1429ms as a
    # 600px sweep — which reads as a hang, not as motion. Above
    # ``FULL_SPEED_DISTANCE`` the spring's own timing is used unchanged; below it
    # the duration tapers linearly toward the floor.
    raw_ms = settle_time(cfg) * 1000.0
    if distance < FULL_SPEED_DISTANCE:
        raw_ms *= max(distance, 0.0) / FULL_SPEED_DISTANCE
    duration_ms = min(max(int(round(raw_ms)), MIN_MOVE_DURATION_MS), MAX_MOVE_DURATION_MS)
    return MotionPlan(points=points, duration_ms=duration_ms, path=path)
