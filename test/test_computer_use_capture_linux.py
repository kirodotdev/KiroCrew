"""``computer_use.capture_linux`` — the capture gates, in the same order as the
Windows/macOS captures, driven over the fake :mod:`linux_ffi`.

Runs with no DISPLAY and no native library. These execute the always-on floors
(a secure window and a truncated walk are never captured), the blank-frame
rejection, the off-screen clip, and the never-raises contract.
"""

from __future__ import annotations

import pytest
from fake_linux_ffi import _isolated_linux_ffi

from kiro_crew.computer_use import capture_linux as C
from kiro_crew.computer_use import linux_ffi as F
from kiro_crew.computer_use.types import AppRef, Snapshot

# Pytest discovers the autouse fixture through this module-level name.
isolated_linux_ffi = _isolated_linux_ffi


def _snap(**kw) -> Snapshot:
    base = dict(
        app=AppRef(name="a", pid=1, bundle_id="a", window_id=0x5, window_title="t"),
        has_secure=False,
        truncated=False,
        depth_truncated=False,
    )
    base.update(kw)
    return Snapshot(**base)


def _varied_rgb(pixels: int) -> bytes:
    """A buffer of distinct pixels (passes the blank-frame gate)."""
    return bytes(
        (i % 256, (i * 2) % 256, (i * 3) % 256)[j] for i in range(pixels) for j in range(3)
    )


@pytest.fixture(autouse=True)
def _no_relay(monkeypatch):
    """The live-view relay POSTs to the gateway; stub it so the capture is pure."""
    monkeypatch.setattr(C.screencast, "emit_snapshot_frame", lambda snap: None)


class TestAlwaysOnFloors:
    def test_a_secure_window_is_never_captured(self, monkeypatch) -> None:
        def boom(*a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("captured a secure window")

        monkeypatch.setattr(F, "x_window_is_viewable", boom)
        out = C.capture_snapshot_image(_snap(has_secure=True))
        assert out.image_jpeg == b""
        assert out.image_path == ""

    def test_a_truncated_walk_is_never_captured(self, monkeypatch) -> None:
        """``has_secure`` means 'none seen', not 'none present', past the cut."""

        def boom(*a, **k):  # pragma: no cover
            raise AssertionError("captured a truncated snapshot")

        monkeypatch.setattr(F, "x_window_is_viewable", boom)
        assert C.capture_snapshot_image(_snap(truncated=True)).image_jpeg == b""
        assert C.capture_snapshot_image(_snap(depth_truncated=True)).image_jpeg == b""

    def test_a_zero_window_id_is_not_captured(self, monkeypatch) -> None:
        def boom(*a, **k):  # pragma: no cover
            raise AssertionError("asked X about window 0")

        monkeypatch.setattr(F, "x_window_is_viewable", boom)
        app = AppRef(name="a", pid=1, bundle_id="a", window_id=0, window_title="t")
        assert C.capture_snapshot_image(_snap(app=app)).image_jpeg == b""

    def test_the_secure_gate_runs_BEFORE_the_window_gate(self) -> None:
        """Order parity with Windows/macOS: has_secure is checked first, so a secure
        snapshot with a live window still yields nothing."""
        out = C.capture_snapshot_image(_snap(has_secure=True))
        assert out.image_jpeg == b""


class TestViewable:
    def test_a_non_viewable_window_is_not_captured(self, monkeypatch) -> None:
        monkeypatch.setattr(F, "x_window_is_viewable", lambda w: False)
        assert C.capture_snapshot_image(_snap()).image_jpeg == b""

    def test_an_unreadable_viewable_state_still_attempts_capture(self, monkeypatch) -> None:
        """A failed viewable probe degrades to attempting the capture, not failing."""

        def boom(w):
            raise RuntimeError("x state unreadable")

        monkeypatch.setattr(F, "x_window_is_viewable", boom)
        # Geometry then returns None -> no image, but no raise.
        monkeypatch.setattr(F, "x_window_geometry", lambda w: None)
        out = C.capture_snapshot_image(_snap())
        assert out.image_jpeg == b""


class TestCaptureHappyAndBlank:
    @staticmethod
    def _wire(
        monkeypatch,
        *,
        rgb,
        geom=(0, 0, 100, 50),
        screen=(3200, 600),
        encoded=(b"\xff\xd8jpeg", 80, 40),
    ):
        monkeypatch.setattr(F, "x_window_is_viewable", lambda w: True)
        monkeypatch.setattr(F, "x_window_geometry", lambda w: geom)
        monkeypatch.setattr(F, "x_gtk_frame_extents", lambda w: None)
        monkeypatch.setattr(F, "x_screen_size", lambda: screen)
        monkeypatch.setattr(F, "x_get_image_rgb", lambda x, y, w, h: (rgb, w, h))
        monkeypatch.setattr(F, "encode_jpeg_rgb", lambda rgb, w, h, **k: encoded)
        monkeypatch.setattr(C, "persist_jpeg", lambda raw: "/tmp/shot.jpg")

    def test_a_varied_frame_is_captured_and_persisted(self, monkeypatch) -> None:
        self._wire(monkeypatch, rgb=_varied_rgb(100 * 50))
        out = C.capture_snapshot_image(_snap())
        assert out.image_jpeg == b"\xff\xd8jpeg"
        assert out.image_path == "/tmp/shot.jpg"
        assert (out.image_width, out.image_height) == (80, 40)

    def test_a_blank_frame_is_rejected(self, monkeypatch) -> None:
        """XGetImage returns a uniform buffer for a withdrawn surface; relaying it
        would hand the model a blank rectangle to reason about."""
        blank = b"\x20\x20\x20" * (100 * 50)
        self._wire(monkeypatch, rgb=blank)
        out = C.capture_snapshot_image(_snap())
        assert out.image_jpeg == b""

    def test_a_failed_encode_yields_no_image(self, monkeypatch) -> None:
        self._wire(monkeypatch, rgb=_varied_rgb(100 * 50), encoded=None)
        assert C.capture_snapshot_image(_snap()).image_jpeg == b""

    def test_a_failed_persist_yields_no_image(self, monkeypatch) -> None:
        self._wire(monkeypatch, rgb=_varied_rgb(100 * 50))
        monkeypatch.setattr(C, "persist_jpeg", lambda raw: "")
        assert C.capture_snapshot_image(_snap()).image_path == ""

    def test_the_encoded_dimensions_are_reported_not_recomputed(self, monkeypatch) -> None:
        """gdk-pixbuf may decline to scale; the reported size is what it produced."""
        self._wire(monkeypatch, rgb=_varied_rgb(100 * 50), encoded=(b"\xff\xd8x", 100, 50))
        out = C.capture_snapshot_image(_snap())
        assert (out.image_width, out.image_height) == (100, 50)


class TestClipToScreen:
    def test_a_window_off_the_right_edge_is_clipped(self) -> None:
        # window at x=3100 width 400 on a 3200-wide screen -> clipped to width 100.
        assert C._clip_to_screen((3100, 0, 400, 50), (3200, 600)) == (3100, 0, 100, 50)

    def test_a_window_off_the_left_edge_is_clipped(self) -> None:
        assert C._clip_to_screen((-50, 0, 200, 50), (3200, 600)) == (0, 0, 150, 50)

    def test_a_fully_off_screen_window_is_none(self) -> None:
        assert C._clip_to_screen((-500, 0, 100, 50), (3200, 600)) is None
        assert C._clip_to_screen((5000, 0, 100, 50), (3200, 600)) is None

    def test_capture_clips_before_grabbing(self, monkeypatch) -> None:
        """A rect past the root pixmap would make XGetImage raise BadMatch; the clip
        is what prevents it, so the grab must receive the clipped rect."""
        monkeypatch.setattr(F, "x_window_is_viewable", lambda w: True)
        monkeypatch.setattr(F, "x_window_geometry", lambda w: (3100, 0, 400, 50))
        monkeypatch.setattr(F, "x_gtk_frame_extents", lambda w: None)
        monkeypatch.setattr(F, "x_screen_size", lambda: (3200, 600))
        grabbed_rect = {}

        def grab(x, y, w, h):
            grabbed_rect.update(x=x, y=y, w=w, h=h)
            return (_varied_rgb(w * h), w, h)

        monkeypatch.setattr(F, "x_get_image_rgb", grab)
        monkeypatch.setattr(F, "encode_jpeg_rgb", lambda rgb, w, h, **k: (b"\xff\xd8", w, h))
        monkeypatch.setattr(C, "persist_jpeg", lambda raw: "/tmp/s.jpg")
        C.capture_snapshot_image(_snap())
        assert grabbed_rect == {"x": 3100, "y": 0, "w": 100, "h": 50}

    def test_the_published_window_bounds_win_over_the_raw_geometry(self, monkeypatch) -> None:
        """The capture rect IS ``Snapshot.window_bounds`` — the content rect the walk
        measured frames against — so a pixel in the image and a window-local frame
        coordinate are the same number. The raw X geometry (which, for a CSD window,
        includes the shadow band) is only the fallback when no bounds were published."""
        monkeypatch.setattr(F, "x_window_is_viewable", lambda w: True)
        monkeypatch.setattr(F, "x_window_geometry", lambda w: (-61, -23, 482, 748))
        monkeypatch.setattr(F, "x_screen_size", lambda: (3200, 600))
        grabbed_rect = {}

        def grab(x, y, w, h):
            grabbed_rect.update(x=x, y=y, w=w, h=h)
            return (_varied_rgb(w * h), w, h)

        monkeypatch.setattr(F, "x_get_image_rgb", grab)
        monkeypatch.setattr(F, "encode_jpeg_rgb", lambda rgb, w, h, **k: (b"\xff\xd8", w, h))
        monkeypatch.setattr(C, "persist_jpeg", lambda raw: "/tmp/s.jpg")
        C.capture_snapshot_image(_snap(window_bounds=(0.0, 32.0, 360.0, 626.0)))
        # Clipped to the 600px-tall screen: y 32..600 is 568 rows.
        assert grabbed_rect == {"x": 0, "y": 32, "w": 360, "h": 568}


class TestNeverRaises:
    def test_a_raising_geometry_read_degrades_to_no_image(self, monkeypatch) -> None:
        monkeypatch.setattr(F, "x_window_is_viewable", lambda w: True)

        def boom(w):
            raise RuntimeError("X error")

        monkeypatch.setattr(F, "x_window_geometry", boom)
        out = C.capture_snapshot_image(_snap())
        assert out.image_jpeg == b""

    def test_a_raising_grab_degrades_to_no_image(self, monkeypatch) -> None:
        monkeypatch.setattr(F, "x_window_is_viewable", lambda w: True)
        monkeypatch.setattr(F, "x_window_geometry", lambda w: (0, 0, 100, 50))
        monkeypatch.setattr(F, "x_screen_size", lambda: (3200, 600))

        def boom(x, y, w, h):
            raise RuntimeError("BadMatch")

        monkeypatch.setattr(F, "x_get_image_rgb", boom)
        assert C.capture_snapshot_image(_snap()).image_jpeg == b""


class TestHasContent:
    def test_a_uniform_buffer_is_blank(self) -> None:
        assert C._has_content(b"\x10\x20\x30" * 100) is False

    def test_a_varied_buffer_is_content(self) -> None:
        assert C._has_content(_varied_rgb(100)) is True

    def test_a_too_small_buffer_is_blank(self) -> None:
        assert C._has_content(b"\x00\x00") is False


class TestReusesMacosSpoolPrimitives:
    def test_persist_and_shots_dir_are_the_shared_platform_free_ones(self) -> None:
        """One definition of how a frame of the operator's screen is protected."""
        from kiro_crew.computer_use import capture_macos

        assert C.persist_jpeg is capture_macos.persist_jpeg
        assert C.shots_dir is capture_macos.shots_dir
        assert C.trim_shots_dir is capture_macos.trim_shots_dir
