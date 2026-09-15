"""The Cursor Motion PATH MODEL — pure-geometry assertions, no platform at all.

``cursor_motion`` is the one part of the overlay subsystem that is a total
function of its arguments: no ctypes, no subprocess, no clock, no config. So it
gets the kind of test the rest of the subsystem cannot have — numeric assertions
on the actual shape of the motion, running identically on a Linux CI shard with no
display.

What is asserted, and why each one is worth pinning:

* **Endpoints are EXACT.** The click pulse is drawn at the last sampled point, so
  a sampler that stopped a float-epsilon short would put the visual "click"
  beside the element it is announcing rather than on it. Asserted as equality,
  not ``approx``.
* **No overshoot past the target.** The progress spring settles at ``1.0000139``,
  and feeding a ``t > 1`` into a cubic Bezier extrapolates along the end tangent —
  the fake cursor would visibly shoot past the thing it is pointing at. The model
  clamps progress, and this file proves the clamp holds by bounding every sample
  inside the curve's own convex hull.
* **The arc formula is the reference's, exactly.** ``clamp(distance*0.22, 28,
  110) * curve_scale`` IS the feel of the animation; a drifted constant is a
  regression no other assertion would catch.
* **Monotone progress.** A non-monotone easing curve reads as a stutter or a
  visible backwards jump, which for an affordance that says "I am about to click
  HERE" is actively misleading.
* **No NaN anywhere, including for degenerate input.** A NaN coordinate reaches
  AppKit as an un-placeable window rather than as an error, so a zero-length move
  must produce finite numbers rather than a crash three layers away.
* **Everything is bounded.** Sample counts, curve scales and durations are all
  clamped, because the caller is ultimately relaying agent-influenced coordinates.
"""

from __future__ import annotations

import math

import pytest

from kiro_crew.computer_use import cursor_motion
from kiro_crew.computer_use.cursor_motion import (
    SpringConfig,
    build_path,
    curve_amount,
    plan_motion,
    sample_path,
    settle_time,
    spring_progress_curve,
)
from kiro_crew.computer_use.types import (
    CURVE_AMOUNT_MAX,
    CURVE_AMOUNT_MIN,
    CURVE_DISTANCE_RATIO,
    FULL_SPEED_DISTANCE,
    MAX_CURVE_SCALE,
    MAX_MOVE_DURATION_MS,
    MAX_PATH_SAMPLES,
    MIN_MOVE_DURATION_MS,
    MIN_PATH_SAMPLES,
    SPRING_SETTLE_DISTANCE,
    STRAIGHT_MOVE_DISTANCE,
)


def _finite(points) -> bool:
    return all(math.isfinite(x) and math.isfinite(y) for x, y in points)


def _hull_bounds(path):
    """Axis-aligned bounding box of the four control points.

    A cubic Bezier is contained in the convex hull of its control points, so this
    box is a sound (if loose) bound on every legitimately sampled point. A sample
    outside it can only mean ``t`` escaped ``[0, 1]``.
    """
    xs = [path.start[0], path.control1[0], path.control2[0], path.end[0]]
    ys = [path.start[1], path.control1[1], path.control2[1], path.end[1]]
    return min(xs), max(xs), min(ys), max(ys)


class TestCurveAmount:
    def test_reproduces_the_reference_clamp_exactly(self):
        # The proportional band: 0.22 * distance, untouched by either clamp.
        assert curve_amount(300.0) == pytest.approx(300.0 * CURVE_DISTANCE_RATIO)
        # Below the floor a short nudge still visibly curves.
        assert curve_amount(10.0) == pytest.approx(CURVE_AMOUNT_MIN)
        # Above the ceiling a cross-screen sweep does not become a semicircle.
        assert curve_amount(4000.0) == pytest.approx(CURVE_AMOUNT_MAX)

    def test_curve_scale_multiplies_and_zero_means_straight(self):
        assert curve_amount(300.0, 2.0) == pytest.approx(2.0 * 300.0 * CURVE_DISTANCE_RATIO)
        assert curve_amount(300.0, 0.0) == 0.0

    def test_curve_scale_is_clamped_both_ways(self):
        # A negative scale would mirror the bow to the other side of the chord,
        # silently inverting a caller's explicit curve_direction.
        assert curve_amount(300.0, -5.0) == 0.0
        assert curve_amount(300.0, 999.0) == curve_amount(300.0, MAX_CURVE_SCALE)

    def test_negative_distance_cannot_produce_a_negative_arc(self):
        assert curve_amount(-100.0) == pytest.approx(CURVE_AMOUNT_MIN)


class TestBuildPath:
    def test_endpoints_are_exact(self):
        path = build_path((10.0, 20.0), (400.0, 300.0))
        assert path.start == (10.0, 20.0)
        assert path.end == (400.0, 300.0)
        assert path.point_at(0.0) == (10.0, 20.0)
        assert path.point_at(1.0) == (400.0, 300.0)

    def test_control_points_are_offset_perpendicular_to_the_chord(self):
        """The bow is what makes the motion read as a movement, not a jump."""
        start, end = (100.0, 100.0), (500.0, 100.0)
        straight = build_path(start, end, curve_scale=0.0)
        bowed = build_path(start, end, curve_scale=1.0)
        # A purely horizontal chord: the perpendicular offset is purely vertical,
        # so a straight path keeps y == 100 and a bowed one must not.
        assert straight.control1[1] == pytest.approx(100.0)
        assert bowed.control1[1] != pytest.approx(100.0)
        assert bowed.arc_amount == pytest.approx(curve_amount(400.0))

    def test_travel_direction_picks_the_bow_side(self):
        """There-and-back traces two DIFFERENT arcs, not one retraced line.

        Rightward derives ``direction=+1`` and leftward ``-1``, so the two bows
        land on the same side of the screen rather than mirroring — which is what
        stops a repeated back-and-forth from looking like a metronome sweeping one
        line. Compared as sampled midpoints, because that is the pixel a user
        actually sees.
        """
        out = build_path((100.0, 100.0), (500.0, 100.0))
        back = build_path((500.0, 100.0), (100.0, 100.0))
        out_mid = out.point_at(0.5)
        back_mid = back.point_at(0.5)
        # Both bow away from the chord...
        assert out_mid[1] != pytest.approx(100.0)
        assert back_mid[1] != pytest.approx(100.0)
        # ...to the SAME side of the chord (the offsets share a sign), which is what
        # makes a back-and-forth read as two arcs rather than one line swept twice.
        assert (out_mid[1] - 100.0) * (back_mid[1] - 100.0) > 0.0
        # And the two curves are genuinely distinct as POINT SETS, not merely
        # traversed in opposite order: the arc is front-loaded along the chord, so
        # at a matched position on the chord the outbound and return trips sit at
        # different heights. (Comparing midpoints alone cannot show this — the
        # midpoints coincide in y by symmetry.)
        out_quarter = out.point_at(0.25)
        back_quarter = back.point_at(0.75)
        assert out_quarter[0] == pytest.approx(back_quarter[0], abs=8.0)
        assert out_quarter[1] != pytest.approx(back_quarter[1], abs=1.0)

    def test_explicit_curve_direction_overrides_the_derived_side(self):
        left = build_path((100.0, 100.0), (500.0, 100.0), curve_direction=1.0)
        right = build_path((100.0, 100.0), (500.0, 100.0), curve_direction=-1.0)
        assert (left.control1[1] - 100.0) == pytest.approx(-(right.control1[1] - 100.0))

    def test_second_handle_bows_less_than_the_first(self):
        """The arc decays toward the target so the approach is along the chord."""
        path = build_path((0.0, 0.0), (600.0, 0.0))
        assert abs(path.control2[1]) < abs(path.control1[1])

    def test_zero_length_move_is_finite_not_nan(self):
        """A zero-length delta has no normal — the naive normalize is 0/0."""
        path = build_path((250.0, 250.0), (250.0, 250.0))
        assert _finite([path.start, path.end, path.control1, path.control2])
        assert _finite(sample_path(path, samples=16))

    def test_straight_mode_uses_thirds_placement(self):
        path = build_path((0.0, 0.0), (300.0, 300.0), curve_scale=0.0)
        assert path.control1 == pytest.approx((100.0, 100.0))
        assert path.control2 == pytest.approx((200.0, 200.0))
        assert path.arc_amount == 0.0

    def test_straight_mode_samples_lie_on_the_chord(self):
        points = sample_path(build_path((0.0, 0.0), (400.0, 400.0), curve_scale=0.0), samples=40)
        for x, y in points:
            assert x == pytest.approx(y, abs=1e-6)

    def test_point_at_clamps_rather_than_extrapolating(self):
        """A ``t`` past 1 on a cubic flies off along the end tangent."""
        path = build_path((0.0, 0.0), (400.0, 0.0))
        assert path.point_at(1.5) == path.end
        assert path.point_at(-3.0) == path.start


class TestSpringProgress:
    def test_starts_at_zero_and_ends_at_exactly_one(self):
        curve = spring_progress_curve()
        assert curve[0] == 0.0
        # EXACTLY 1.0, not approx: the value is fed to ``point_at`` and the
        # endpoint guarantee depends on hitting the clamped branch.
        assert curve[-1] == 1.0

    def test_progress_is_monotone(self):
        """A non-monotone easing reads as a stutter or a backwards jump."""
        curve = spring_progress_curve()
        for earlier, later in zip(curve, curve[1:]):
            assert later >= earlier - 1e-12

    def test_no_nan_and_settles_within_the_step_bound(self):
        curve = spring_progress_curve()
        assert all(math.isfinite(value) for value in curve)
        # ~343 steps at 1/240s for the shipped constants; the bound is 4096.
        assert 1 < len(curve) < 4096

    def test_settle_time_matches_the_measured_reference_value(self):
        # The reference's own calibrated travel duration for these constants.
        assert settle_time() == pytest.approx(1.429, abs=0.01)

    def test_settle_definition_requires_both_reached_and_close(self):
        """The rising edge must not count as settled."""
        curve = spring_progress_curve()
        final = curve[-2] if len(curve) > 1 else curve[-1]
        assert final >= 1.0 - SPRING_SETTLE_DISTANCE

    def test_zero_response_is_floored_not_divided_by_zero(self):
        """``(2*pi/0)**2`` would be inf and the first step NaN."""
        cfg = SpringConfig.create(response=0.0)
        assert math.isfinite(cfg.stiffness)
        assert math.isfinite(cfg.drag)
        curve = spring_progress_curve(cfg)
        assert all(math.isfinite(value) for value in curve)
        assert curve[-1] == 1.0

    def test_negative_damping_is_floored_to_zero(self):
        # A negative drag term is energy INJECTION: progress would diverge.
        cfg = SpringConfig.create(damping=-2.0)
        assert cfg.drag == 0.0
        assert all(math.isfinite(value) for value in spring_progress_curve(cfg))

    def test_stiffness_and_drag_are_derived_from_response(self):
        cfg = SpringConfig.create(response=1.4, damping=0.9)
        assert cfg.stiffness == pytest.approx((2.0 * math.pi / 1.4) ** 2)
        assert cfg.drag == pytest.approx(2.0 * 0.9 * math.sqrt(cfg.stiffness))

    def test_undamped_spring_still_terminates(self):
        """Zero damping never settles — the step bound is the only exit."""
        cfg = SpringConfig.create(damping=0.0)
        curve = spring_progress_curve(cfg)
        assert len(curve) <= 4097
        assert curve[-1] == 1.0

    def test_settle_time_falls_back_when_the_curve_is_degenerate(self, monkeypatch):
        monkeypatch.setattr(cursor_motion, "spring_progress_curve", lambda config=None: (1.0,))
        assert settle_time() == pytest.approx(1.43)


class TestSamplePath:
    def test_first_and_last_samples_are_the_exact_endpoints(self):
        path = build_path((7.0, 9.0), (611.0, 452.0))
        points = sample_path(path, samples=64)
        assert points[0] == path.start
        assert points[-1] == path.end

    def test_sample_count_is_honored_and_clamped(self):
        path = build_path((0.0, 0.0), (300.0, 300.0))
        assert len(sample_path(path, samples=32)) == 32
        assert len(sample_path(path, samples=0)) == MIN_PATH_SAMPLES
        assert len(sample_path(path, samples=-99)) == MIN_PATH_SAMPLES
        assert len(sample_path(path, samples=10_000)) == MAX_PATH_SAMPLES

    def test_no_sample_escapes_the_control_point_hull(self):
        """The overshoot guard, stated geometrically.

        A cubic Bezier lies within the convex hull of its control points, so a
        sample outside that box proves ``t`` escaped ``[0, 1]`` — i.e. the spring's
        ``1.0000139`` overshoot reached the curve unclamped.
        """
        path = build_path((120.0, 640.0), (1500.0, 180.0))
        min_x, max_x, min_y, max_y = _hull_bounds(path)
        for x, y in sample_path(path, samples=200):
            assert min_x - 1e-6 <= x <= max_x + 1e-6
            assert min_y - 1e-6 <= y <= max_y + 1e-6

    def test_samples_progress_toward_the_target_without_reversing(self):
        """Distance-to-target must never grow: a visible backwards drift."""
        path = build_path((0.0, 0.0), (900.0, 0.0), curve_scale=0.0)
        points = sample_path(path, samples=120)
        distances = [abs(900.0 - x) for x, _ in points]
        for earlier, later in zip(distances, distances[1:]):
            assert later <= earlier + 1e-6

    def test_easing_is_dense_at_both_ends(self):
        """Ease-in/ease-out, expressed as per-sample step lengths.

        Points are uniform in TIME, so the middle of the path must advance further
        per sample than either end. That is what makes a constant-frame-rate
        renderer show acceleration without any easing logic of its own.
        """
        path = build_path((0.0, 0.0), (1000.0, 0.0), curve_scale=0.0)
        points = sample_path(path, samples=60)
        steps = [abs(b[0] - a[0]) for a, b in zip(points, points[1:])]
        first, middle, last = steps[0], steps[len(steps) // 2], steps[-1]
        assert middle > first
        assert middle > last

    def test_all_samples_are_finite_for_a_degenerate_path(self):
        assert _finite(sample_path(build_path((5.0, 5.0), (5.0, 5.0)), samples=32))

    def test_single_effective_sample_still_returns_endpoints(self):
        path = build_path((0.0, 0.0), (100.0, 100.0))
        points = sample_path(path, samples=MIN_PATH_SAMPLES)
        assert points == (path.start, path.end)


class TestPlanMotion:
    def test_plan_carries_points_duration_and_the_path(self):
        plan = plan_motion((10.0, 10.0), (800.0, 400.0))
        assert plan.points[0] == (10.0, 10.0)
        assert plan.points[-1] == (800.0, 400.0)
        assert plan.path.end == (800.0, 400.0)
        assert MIN_MOVE_DURATION_MS <= plan.duration_ms <= MAX_MOVE_DURATION_MS

    def test_duration_comes_from_the_spring_settle_time(self):
        """Duration and easing must be derived from ONE integration.

        Picking them independently would either cut the settle off mid-ring-down
        or hold the overlay after the cursor visually stopped.
        """
        plan = plan_motion((0.0, 0.0), (600.0, 600.0))
        assert plan.duration_ms == pytest.approx(int(round(settle_time() * 1000)), abs=1)

    def test_duration_is_clamped_for_a_pathological_spring(self, monkeypatch):
        """Both clamps still bind whatever the spring says.

        Uses a FULL-distance move deliberately: the duration is scaled by distance
        (a 1px nudge tapers to the floor no matter how long the spring settles), so
        a short move would hit the floor for the wrong reason and the ceiling
        assertion would prove nothing.
        """
        far = (FULL_SPEED_DISTANCE, 0.0)
        monkeypatch.setattr(cursor_motion, "settle_time", lambda config=None: 99.0)
        assert plan_motion((0.0, 0.0), far).duration_ms == MAX_MOVE_DURATION_MS
        monkeypatch.setattr(cursor_motion, "settle_time", lambda config=None: 0.0)
        assert plan_motion((0.0, 0.0), far).duration_ms == MIN_MOVE_DURATION_MS

    def test_duration_tapers_with_distance_below_full_speed(self):
        """The spring's settle point is distance-INDEPENDENT.

        Using it raw gave a 1px nudge the same ~1429ms as a 600px sweep, which reads
        as a hang rather than as motion. Below ``FULL_SPEED_DISTANCE`` the duration
        tapers linearly toward the floor, so a short hop looks short.
        """
        short = plan_motion((0.0, 0.0), (FULL_SPEED_DISTANCE / 4.0, 0.0)).duration_ms
        mid = plan_motion((0.0, 0.0), (FULL_SPEED_DISTANCE / 2.0, 0.0)).duration_ms
        full = plan_motion((0.0, 0.0), (FULL_SPEED_DISTANCE, 0.0)).duration_ms
        assert short < mid < full
        # At and above FULL_SPEED_DISTANCE the spring's own timing is used unchanged.
        far = plan_motion((0.0, 0.0), (FULL_SPEED_DISTANCE * 4.0, 0.0)).duration_ms
        assert far == full

    def test_a_tiny_nudge_is_drawn_straight_not_as_a_curlicue(self):
        """``curve_amount`` floors the arc at 28px.

        Without the short-move override a 1px nudge would bow ~28px out and back —
        a visible loop on a move the eye reads as "it barely went anywhere".
        """
        tiny = plan_motion((500.0, 500.0), (500.0 + STRAIGHT_MOVE_DISTANCE / 4.0, 500.0))
        assert tiny.path.arc_amount == 0.0
        for _, y in tiny.points:
            assert y == pytest.approx(500.0, abs=1e-6)
        # Just past the threshold the arc comes back — the override is a floor, not
        # a permanent disabling of the curve.
        normal = plan_motion((500.0, 500.0), (500.0 + STRAIGHT_MOVE_DISTANCE * 4.0, 500.0))
        assert normal.path.arc_amount > 0.0

    def test_a_short_move_still_respects_the_duration_floor(self):
        assert plan_motion((0.0, 0.0), (1.0, 0.0)).duration_ms == MIN_MOVE_DURATION_MS

    def test_zero_length_plan_collapses_to_the_single_point(self):
        """A stationary target must not draw a 28px loop around itself.

        ``approx`` rather than equality for the INTERIOR samples: only the
        endpoints are contractually exact (they short-circuit ``point_at``), while
        an interior sample is a Bernstein sum of four identical points and lands
        within ~1e-13 of it. That is a sub-nanometre error on a pixel coordinate —
        pinning it exactly would be pinning float associativity, not behaviour.
        """
        plan = plan_motion((300.0, 300.0), (300.0, 300.0))
        assert _finite(plan.points)
        assert plan.points[0] == (300.0, 300.0)
        assert plan.points[-1] == (300.0, 300.0)
        for x, y in plan.points:
            assert x == pytest.approx(300.0, abs=1e-9)
            assert y == pytest.approx(300.0, abs=1e-9)

    def test_extreme_coordinates_stay_finite(self):
        """The caller ultimately relays agent-influenced numbers."""
        plan = plan_motion((-1e6, -1e6), (1e6, 1e6))
        assert _finite(plan.points)
        assert plan.points[-1] == (1e6, 1e6)

    def test_straight_plan_has_no_arc(self):
        plan = plan_motion((0.0, 0.0), (500.0, 0.0), curve_scale=0.0)
        assert plan.path.arc_amount == 0.0
        for _, y in plan.points:
            assert y == pytest.approx(0.0, abs=1e-6)


class TestPurity:
    """The module must stay platform-free — that is what makes it CI-testable."""

    def test_no_platform_module_is_imported(self):
        """Asserted over the IMPORT GRAPH by AST, not over the source text.

        A substring scan would trip over the docstring that explains the rule, and
        (worse) would pass for a module that reached ctypes through
        ``importlib.import_module``. The real claim is about what this module binds
        at import time, so the AST is the right instrument.
        """
        import ast
        import inspect
        from pathlib import Path

        tree = ast.parse(Path(inspect.getfile(cursor_motion)).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert imported <= {"__future__", "logging", "math", "dataclasses", "kiro_crew"}, imported

    def test_planning_is_deterministic(self):
        # No clock, no randomness: two identical calls must be byte-identical, or a
        # visual regression could not be reproduced from a bug report.
        first = plan_motion((11.0, 22.0), (633.0, 471.0))
        second = plan_motion((11.0, 22.0), (633.0, 471.0))
        assert first.points == second.points
        assert first.duration_ms == second.duration_ms


class TestDistanceScaledMotion:
    """A move's duration and arc must both scale with its DISTANCE.

    Found by driving the planner over a distance sweep rather than by reading it:
    the progress spring's settle point is distance-INDEPENDENT, so before this the
    planner gave a 1px nudge the same ~1429ms and the same 28px arc as a 600px
    sweep. On screen a 1.4s animation for a one-pixel move reads as a hang, and a
    28px bow on a 1px move is a visible curlicue.
    """

    def test_a_long_sweep_keeps_the_full_spring_timing(self):
        plan = plan_motion((0.0, 0.0), (600.0, 0.0))
        assert plan.duration_ms == MAX_MOVE_DURATION_MS or plan.duration_ms > 1000

    def test_duration_scales_down_with_distance(self):
        far = plan_motion((0.0, 0.0), (600.0, 0.0)).duration_ms
        mid = plan_motion((0.0, 0.0), (200.0, 0.0)).duration_ms
        near = plan_motion((0.0, 0.0), (60.0, 0.0)).duration_ms
        assert far > mid > near, (far, mid, near)

    def test_a_tiny_move_collapses_to_the_duration_floor(self):
        for distance in (0.0, 1.0, 3.0, 10.0):
            plan = plan_motion((700.0, 500.0), (700.0 + distance, 500.0))
            assert plan.duration_ms == MIN_MOVE_DURATION_MS, (distance, plan.duration_ms)

    def test_a_short_hop_is_drawn_straight(self):
        """The arc floor is 28px, so a sub-threshold move must drop the arc entirely."""
        for distance in (0.0, 1.0, 3.0, 10.0):
            plan = plan_motion((700.0, 500.0), (700.0 + distance, 500.0))
            assert plan.path.arc_amount == 0.0, (distance, plan.path.arc_amount)

    def test_a_long_move_still_arcs(self):
        """The inverse assertion, so the straight-hop rule cannot swallow everything."""
        assert plan_motion((0.0, 0.0), (600.0, 0.0)).path.arc_amount > 0.0

    def test_no_move_ever_exceeds_the_duration_ceiling(self):
        for distance in (0.0, 50.0, 5000.0, 100000.0):
            plan = plan_motion((0.0, 0.0), (distance, 0.0))
            assert MIN_MOVE_DURATION_MS <= plan.duration_ms <= MAX_MOVE_DURATION_MS

    def test_endpoints_stay_exact_at_every_distance(self):
        """The click pulse is drawn at the last point, so it must land ON the target."""
        for distance in (0.0, 1.0, 24.0, 600.0):
            end = (700.0 + distance, 500.0)
            plan = plan_motion((700.0, 500.0), end)
            assert plan.points[0] == (700.0, 500.0)
            assert plan.points[-1] == end
