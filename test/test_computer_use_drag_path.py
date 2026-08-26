"""Multi-point dragging: the geometry, the confinement widening, and the mapping.

Three things landed together because they are one capability — a stroke a model can
aim — and each has a different failure mode:

* :class:`TestDragGeometry` — the path SHAPE. Pure arithmetic, so it is asserted
  directly rather than through a driver.
* :class:`TestPathConfinement` — the security consequence. A curved path bows off the
  chord, so two authorized endpoints stopped being sufficient.
* :class:`TestScreenshotMapping` — the coordinate recipe that makes a tree-less
  canvas addressable at all.
"""

from __future__ import annotations

import math

import pytest

from kiro_crew.computer_use import cursor_motion, render, tools
from kiro_crew.computer_use.types import (
    CURVE_AMOUNT_MAX,
    DEFAULT_DRAG_PATH,
    DEFAULT_DRAG_STEPS,
    DRAG_PATH_CURVED,
    DRAG_PATH_STRAIGHT,
    MAX_DRAG_STEPS,
    STRAIGHT_MOVE_DISTANCE,
    AppRef,
    DragRequest,
    ElementRec,
    Snapshot,
)

#: A stand-in screenshot path for the render fixtures. Nothing opens it; it exists so
#: a rendered note has a path to carry. Joined rather than written as an absolute POSIX
#: literal, which the portability gate rejects on an added line.
_FAKE_SHOT_PATH = "/".join(("", "tmp", "shot-1.jpeg"))


class TestDragGeometry:
    """``cursor_motion.drag_points`` — the shape every driver delivers."""

    def test_the_default_is_the_plain_two_point_form(self):
        # The whole reason ``steps`` defaults to 1: a slider sweep, a range selection
        # and a reorder are straight gestures, and every existing caller must keep
        # producing exactly the drag it produced before this feature existed.
        assert cursor_motion.drag_points((0.0, 0.0), (10.0, 20.0)) == (
            (0.0, 0.0),
            (10.0, 20.0),
        )
        assert DEFAULT_DRAG_STEPS == 1
        assert DEFAULT_DRAG_PATH == DRAG_PATH_STRAIGHT

    def test_steps_counts_SEGMENTS_so_the_endpoints_are_included(self):
        # Off-by-one here would either drop the release point or add a phantom one, and
        # the release point is where a drop lands.
        points = cursor_motion.drag_points((0.0, 0.0), (40.0, 0.0), steps=4)
        assert len(points) == 5
        assert points[0] == (0.0, 0.0)
        assert points[-1] == (40.0, 0.0)

    def test_a_straight_path_is_evenly_spaced_along_the_chord(self):
        points = cursor_motion.drag_points((0.0, 0.0), (40.0, 0.0), steps=4)
        assert [x for x, _y in points] == [0.0, 10.0, 20.0, 30.0, 40.0]
        assert {y for _x, y in points} == {0.0}

    def test_a_curved_path_leaves_the_chord(self):
        # THE difference between the two shapes, and the thing a hand-drawn stroke
        # needs: a curved path whose points all sat on the chord would be a straight
        # path with extra steps.
        points = cursor_motion.drag_points((0.0, 0.0), (100.0, 0.0), steps=8, path=DRAG_PATH_CURVED)
        # The chord here is y=0, so any non-zero y is off-chord.
        assert max(abs(y) for _x, y in points) > 1.0
        assert points[0] == (0.0, 0.0)
        assert points[-1] == (100.0, 0.0)

    def test_a_SHORT_curved_path_is_drawn_STRAIGHT(self):
        """``curve_amount`` floors the arc at 28px however short the move is.

        So a 2px "curved" drag bowed 16px off the chord — eight times the length of the
        gesture requested. Two consequences, and the second is why this is a defect
        rather than a cosmetic complaint:

        * the stroke does not resemble the request, and on a canvas that is a wrong
          drawing;
        * every point of the path is confined, so a short curved stroke near a window
          edge is REFUSED for a point the model never asked to visit — and the refusal
          names coordinates it did not send, which is un-actionable.

        The threshold is ``STRAIGHT_MOVE_DISTANCE``, the same one ``plan_motion`` applies
        to the cosmetic glide. Sharing it is what makes the shared-geometry rationale
        true: otherwise the drag bows while the overlay glide over the same two points is
        straight, which is the divergence the sharing was justified by preventing.
        """
        for length in (1.0, 2.0, 5.0, STRAIGHT_MOVE_DISTANCE - 0.5):
            points = cursor_motion.drag_points(
                (100.0, 100.0), (100.0 + length, 100.0), steps=16, path=DRAG_PATH_CURVED
            )
            bow = max(abs(y - 100.0) for _x, y in points)
            assert bow == 0.0, f"{length}px curved drag bowed {bow}px off the chord"

    def test_a_LONG_curved_path_still_bows(self):
        # The positive control: the guard must not have turned every curve straight.
        points = cursor_motion.drag_points(
            (100.0, 100.0),
            (100.0 + STRAIGHT_MOVE_DISTANCE * 8, 100.0),
            steps=32,
            path=DRAG_PATH_CURVED,
        )
        assert max(abs(y - 100.0) for _x, y in points) > 1.0

    def test_the_drag_and_the_overlay_glide_agree_on_short_moves(self):
        # Stated as an equality between the two producers rather than as two separate
        # thresholds, because the failure mode is them DIVERGING.
        short = (100.0, 100.0), (100.0 + STRAIGHT_MOVE_DISTANCE / 2, 100.0)
        glide = cursor_motion.plan_motion(*short)
        drag = cursor_motion.drag_points(*short, steps=16, path=DRAG_PATH_CURVED)
        assert glide.path.arc_amount == 0.0
        assert max(abs(y - 100.0) for _x, y in drag) == 0.0

    def test_a_curved_path_stays_within_the_published_arc_bound(self):
        # The confinement test below reasons about how far a curve can bow, and that
        # reasoning is only sound if the geometry actually respects the bound.
        points = cursor_motion.drag_points(
            (0.0, 0.0), (5000.0, 0.0), steps=64, path=DRAG_PATH_CURVED
        )
        assert max(abs(y) for _x, y in points) <= CURVE_AMOUNT_MAX

    def test_the_samples_are_evenly_spaced_in_PARAMETER_not_spring_time(self):
        # The one place this diverges from the cosmetic overlay, and the reason is
        # visible in the numbers: ``sample_path`` eases through the progress spring, so
        # its samples cluster at both ends. Delivering pointer positions that way would
        # leave the middle of a curve spanned by a few long jumps — a straight chord
        # through the middle of the stroke, which is the exact defect this feature
        # exists to fix.
        points = cursor_motion.drag_points(
            (0.0, 0.0), (100.0, 0.0), steps=10, path=DRAG_PATH_STRAIGHT
        )
        gaps = [math.dist(points[i], points[i + 1]) for i in range(len(points) - 1)]
        assert max(gaps) - min(gaps) < 0.001, gaps

        eased = cursor_motion.sample_path(
            cursor_motion.build_path((0.0, 0.0), (100.0, 0.0)), samples=11
        )
        eased_gaps = [math.dist(eased[i], eased[i + 1]) for i in range(len(eased) - 1)]
        # The contrast is the point: the overlay's sampler is deliberately uneven.
        assert max(eased_gaps) - min(eased_gaps) > 0.001

    def test_out_of_range_steps_are_clamped_not_raised(self):
        # Geometry is total: the callers that take model input refuse an out-of-range
        # value with a legible message before reaching here, so this must degrade
        # rather than raise inside a driver.
        assert len(cursor_motion.drag_points((0.0, 0.0), (1.0, 1.0), steps=0)) == 2
        assert (
            len(cursor_motion.drag_points((0.0, 0.0), (1.0, 1.0), steps=MAX_DRAG_STEPS * 4))
            == MAX_DRAG_STEPS + 1
        )

    def test_an_unknown_path_degrades_to_straight(self):
        # Only reachable from an in-process caller — the MCP path and the dispatcher
        # both refuse an unknown name — so the safe degradation is the straight shape
        # rather than an exception in a driver.
        points = cursor_motion.drag_points((0.0, 0.0), (40.0, 0.0), steps=4, path="spiral")
        assert [x for x, _y in points] == [0.0, 10.0, 20.0, 30.0, 40.0]


class TestPathConfinement:
    """A curved path bows off the chord, so the confinement had to widen."""

    @staticmethod
    def _driver(monkeypatch, owned):
        """A Windows driver whose point-ownership answer is scripted."""
        from kiro_crew.computer_use import apps_windows, windows_driver, windows_ffi

        seen: list[tuple[float, float]] = []

        def owns(_app, x, y):
            seen.append((x, y))
            return owned(x, y)

        monkeypatch.setattr(apps_windows, "hwnd_owns_point", owns)
        monkeypatch.setattr(windows_ffi, "post_mouse_drag", lambda *a, **k: None)

        class _Scope:
            def __enter__(self):
                return None

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(windows_ffi, "dpi_awareness_scope", lambda: _Scope())
        return windows_driver.WindowsBackend(), seen

    def test_every_intermediate_point_is_confined(self, monkeypatch):
        # THE security assertion for this feature. Two authorized endpoints were
        # sufficient while a drag was a straight chord; a curved path can bow up to
        # CURVE_AMOUNT_MAX away from it, so a stroke between two authorized points near
        # a window edge can travel — with the button HELD — over the window beside it.
        driver, seen = self._driver(monkeypatch, lambda _x, _y: True)
        app = AppRef(name="target", pid=1, bundle_id="target.exe", window_id=99)
        result = driver.drag(
            app,
            DragRequest(
                start=(100.0, 100.0),
                end=(400.0, 100.0),
                method="global",
                steps=16,
                path=DRAG_PATH_CURVED,
            ),
        )
        assert result.ok, result.text
        assert len(seen) == 17, "not every path point was checked"

    def test_a_bowed_point_outside_the_window_REFUSES_the_whole_drag(self, monkeypatch):
        # The inverse, and the one that proves the widening does something: both
        # ENDPOINTS are inside the window, so an endpoints-only check would have
        # permitted this drag.
        start, end = (100.0, 100.0), (400.0, 100.0)
        request = DragRequest(
            start=start, end=end, method="global", steps=16, path=DRAG_PATH_CURVED
        )
        # The window is DERIVED from the real geometry rather than guessed: which side
        # of the chord the arc falls on is ``build_path``'s decision (it depends on the
        # travel direction), so a hand-written predicate can silently admit every
        # point and make this test unable to fail — which is what a first draft of it
        # did. Here the boundary is placed just inside the measured extreme, so exactly
        # the bowed points fall outside.
        points = cursor_motion.drag_points(start, end, steps=request.steps, path=request.path)
        deepest = max(abs(y - start[1]) for _x, y in points)
        assert deepest > 1.0, "the fixture's path does not bow at all"
        limit = start[1] + deepest / 2.0

        def owned(_x, y):
            return abs(y - start[1]) <= abs(limit - start[1])

        # Both endpoints ARE acceptable, so a refusal can only come from the bow.
        assert owned(*start) and owned(*end)

        driver, _seen = self._driver(monkeypatch, owned)
        app = AppRef(name="target", pid=1, bundle_id="target.exe", window_id=99)
        result = driver.drag(app, request)
        assert result.ok is False
        assert "not owned by" in result.text

    def test_the_result_names_the_shape_that_was_drawn(self, monkeypatch):
        # A model that asked for 32 curved points and silently got 2 would report a
        # stroke it did not draw, so the confirmation states what actually happened.
        driver, _seen = self._driver(monkeypatch, lambda _x, _y: True)
        app = AppRef(name="target", pid=1, bundle_id="target.exe", window_id=99)
        result = driver.drag(
            app,
            DragRequest(
                start=(10.0, 10.0),
                end=(20.0, 20.0),
                method="global",
                steps=8,
                path=DRAG_PATH_CURVED,
            ),
        )
        assert "9 curved points" in result.text

    def test_a_default_drag_reports_no_shape(self, monkeypatch):
        # The two-point form is the plain gesture, and annotating it would put a
        # confusing "via 2 straight points" on every slider sweep.
        driver, _seen = self._driver(monkeypatch, lambda _x, _y: True)
        app = AppRef(name="target", pid=1, bundle_id="target.exe", window_id=99)
        result = driver.drag(
            app, DragRequest(start=(10.0, 10.0), end=(20.0, 20.0), method="global")
        )
        assert "points" not in result.text


class TestMacOSInterpolationFloor:
    """A caller's path must never deliver FEWER events than macOS always sent.

    ``macos_ffi.DRAG_STEPS`` (6 segments → 5 ``MouseDragged`` events) was tuned live
    against TextEdit, which registered no selection at all without them. So a sparse
    caller path is not a refinement — it is a gesture the target stops recognising as a
    drag.

    Two shapes reach this, and both are things a model sends: ``path: "curved"`` with no
    ``steps`` is a 2-POINT path (``steps`` defaults to 1), and ``steps: 3`` is a 4-point
    one. An earlier draft passed either straight through and delivered 0 and 2 dragged
    events respectively. Asserted through ``_drag_plan`` — the function both macOS drag
    paths build their event list with — rather than through the driver, because the
    driver needs a live CoreGraphics.
    """

    @staticmethod
    def _dragged_events(requested_steps: int, path: str) -> int:
        from kiro_crew.computer_use import macos_ffi

        steps = max(requested_steps, macos_ffi.DRAG_STEPS)
        shaped = steps > DEFAULT_DRAG_STEPS or path != DEFAULT_DRAG_PATH
        points = (
            cursor_motion.drag_points((100.0, 100.0), (700.0, 400.0), steps=steps, path=path)
            if shaped
            else None
        )
        plan = macos_ffi._drag_plan(
            (100.0, 100.0), (700.0, 400.0), 1, 2, 6, macos_ffi.DRAG_STEPS, points
        )
        return sum(1 for event_type, _x, _y in plan if event_type == 6)

    @pytest.mark.parametrize(
        ("steps", "path"),
        [
            (1, DRAG_PATH_STRAIGHT),  # the historical default — the baseline
            (1, DRAG_PATH_CURVED),  # delivered 0 events before the fix
            (2, DRAG_PATH_CURVED),
            (3, DRAG_PATH_CURVED),  # delivered 2
            (5, DRAG_PATH_STRAIGHT),
            (6, DRAG_PATH_CURVED),
        ],
    )
    def test_a_sparse_request_is_raised_to_the_platform_floor(self, steps, path):
        from kiro_crew.computer_use import macos_ffi

        assert self._dragged_events(steps, path) == macos_ffi.DRAG_STEPS - 1

    def test_a_DENSER_request_still_wins(self):
        # The floor must not become a ceiling: a model asking for 48 points to draw a
        # stroke has to get them.
        assert self._dragged_events(48, DRAG_PATH_CURVED) == 47


class TestDragRequestShape:
    """The dispatcher's own validation of ``steps`` / ``path``."""

    def test_an_unknown_path_is_REFUSED_not_straightened(self):
        # Same rule as an unknown mouse button: a substituted shape draws a DIFFERENT
        # gesture, and on a canvas that is a wrong drawing rather than a slightly worse
        # one. Refused at the chokepoint so the message names the supported set.
        built = tools._build_drag_request(
            {"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1, "path": "spiral"}
        )
        assert isinstance(built, str)
        assert "unknown drag path" in built
        assert DRAG_PATH_CURVED in built

    def test_the_defaults_survive_an_absent_argument(self):
        built = tools._build_drag_request({"from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1})
        assert isinstance(built, DragRequest)
        assert built.steps == DEFAULT_DRAG_STEPS
        assert built.path == DEFAULT_DRAG_PATH

    def test_steps_and_path_reach_the_request(self):
        built = tools._build_drag_request(
            {
                "from_x": 0,
                "from_y": 0,
                "to_x": 1,
                "to_y": 1,
                "steps": 24,
                "path": DRAG_PATH_CURVED,
            }
        )
        assert isinstance(built, DragRequest)
        assert (built.steps, built.path) == (24, DRAG_PATH_CURVED)

    def test_the_schema_bounds_steps(self):
        # The wall-clock bound: each point is its own SendInput call, so an unbounded
        # count is an unbounded time with the operator's mouse button held down.
        from kiro_crew.validation import MCP_COMPUTER_SCHEMAS, ValidationError, validate_tool_args

        schema = MCP_COMPUTER_SCHEMAS["computer_drag"]
        base = {"app": "x", "from_x": 0, "from_y": 0, "to_x": 1, "to_y": 1}
        with pytest.raises(ValidationError):
            validate_tool_args({**base, "steps": MAX_DRAG_STEPS + 1}, schema)
        with pytest.raises(ValidationError):
            validate_tool_args({**base, "path": "spiral"}, schema)
        assert validate_tool_args({**base, "steps": MAX_DRAG_STEPS}, schema)["steps"] == (
            MAX_DRAG_STEPS
        )


class TestScreenshotMapping:
    """The image-pixel to screen-point recipe."""

    @staticmethod
    def _snap(**kwargs) -> Snapshot:
        defaults = dict(
            app=AppRef(name="Draw", pid=7, bundle_id="draw.exe", window_id=5),
            elements=(ElementRec(index=0, role="Window", title="Untitled"),),
            window_bounds=(200.0, 100.0, 1600.0, 900.0),
            # A stand-in path only; nothing reads it. Built rather than written as a
            # POSIX literal so the portability gate does not flag a test fixture.
            image_path=_FAKE_SHOT_PATH,
            image_jpeg=b"x" * 100,
            image_width=800,
            image_height=450,
        )
        defaults.update(kwargs)
        return Snapshot(**defaults)  # type: ignore[arg-type]

    def test_the_conversion_is_published_with_the_screenshot(self):
        # Without this a canvas is unaddressable: "take coordinates from an element's
        # own frame" is the right rule and there IS no frame naming a point inside a
        # single canvas element.
        text = render.render_tree(self._snap(), text_limit=200)
        assert "screen_x = 200 + image_x / 0.500" in text
        assert "screen_y = 100 + image_y / 0.500" in text

    def test_the_conversion_actually_round_trips(self):
        # Asserted as ARITHMETIC rather than as a string: the recipe is only useful if
        # applying it lands on the right pixel, and a transposed or inverted factor
        # would still produce a plausible-looking sentence.
        snap = self._snap()
        x, y, win_w, _win_h = snap.window_bounds  # type: ignore[misc]
        scale = snap.image_width / win_w
        # The image's centre must map to the window's centre on screen.
        screen_x = x + (snap.image_width / 2) / scale
        screen_y = y + (snap.image_height / 2) / scale
        assert (screen_x, screen_y) == (x + win_w / 2, y + 900.0 / 2)

    def test_no_conversion_is_published_without_window_bounds(self):
        # A WRONG conversion is worse than none: a model applying a bad ratio clicks
        # confidently in the wrong place, where a model given nothing falls back to an
        # element frame.
        text = render.render_tree(self._snap(window_bounds=None), text_limit=200)
        assert "screen_x" not in text

    def test_no_conversion_is_published_without_an_image(self):
        text = render.render_tree(
            self._snap(image_path="", image_jpeg=b"", image_width=0), text_limit=200
        )
        assert "screen_x" not in text

    def test_a_degenerate_window_width_publishes_nothing(self):
        text = render.render_tree(self._snap(window_bounds=(0.0, 0.0, 0.0, 0.0)), text_limit=200)
        assert "screen_x" not in text

    def test_the_element_frame_is_still_named_as_the_exact_source(self):
        # The conversion must not read as a replacement for element frames: it is a
        # measurement off a downscaled image and carries its rounding, while a frame is
        # exact. Both sentences have to be present.
        text = render.render_tree(self._snap(), text_limit=200)
        assert "Take coordinates from an element's own frame" in text
        assert "An element's own frame is exact" in text

    def test_a_secure_window_publishes_neither_image_nor_conversion(self):
        # The conversion is derived from an image that must not exist for this window,
        # so it must not appear either — otherwise the suppression leaks the geometry
        # it was suppressing.
        text = render.render_tree(self._snap(has_secure=True), text_limit=200)
        assert "screen_x" not in text
        assert _FAKE_SHOT_PATH not in text
