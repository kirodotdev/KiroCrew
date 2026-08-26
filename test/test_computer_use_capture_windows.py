"""``computer_use.capture_windows`` — the blank-frame gate and the capture floors.

Runs on every platform: the decision functions here are pure over a byte buffer,
and the two that are not are driven through fakes, so no window is captured and no
native library loads.

This module shipped with NO direct test, which is why a review found four defects
in it at once. The one that mattered is pinned first: :func:`_has_content` is the
ONLY thing standing between a swapchain window that reports a successful capture
and a blank rectangle relayed to the model as though it were the application.
``PrintWindow`` returns success while producing a uniform buffer, so the return
value carries no information at all.

Three later cases carry more weight than the rest, because each was a defect that
no test could see and no code reading would reveal:

* the capture must not fall back to ``BitBlt``, which reads the SCREEN and so can
  put an unauthorized application's pixels in a frame labelled as the target's;
* the buffer must be 32bpp, because ``PW_RENDERFULLCONTENT`` renders through DWM
  and writes NOTHING into a 24bpp one while still returning 1;
* the buffer must be sized to what the window RENDERS, not to its DPI-aware rect,
  because ``PrintWindow`` draws in the target's own coordinate space.
"""

from __future__ import annotations

import ctypes
import pathlib
import threading

import pytest

from kiro_crew.computer_use import capture_windows as C

#: Bytes per pixel at the module's current depth. Derived rather than written as a
#: literal: the depth is dictated by what ``PW_RENDERFULLCONTENT`` will render into
#: (see ``capture_windows._BITSPIXEL``), so a test that hardcodes 3 or 4 stops
#: describing the buffer the code actually builds the moment that changes.
_BPP = C._BITSPIXEL // 8


def _stride(width: int, *, bpp: int = _BPP) -> int:
    """The DIB's DWORD-aligned row length, as ``_capture_window_bitmap`` computes it."""
    return ((width * bpp * 8 + 31) // 32) * 4


def _frame(width: int, height: int, *, fill: int = 0xFF, pad: int = 0x00, bpp: int = _BPP) -> bytes:
    """A uniform top-down DIB of *width* x *height*, alignment padding included.

    This is the shape of a real blank capture: the pixels are one repeated value and
    the alignment padding is a DIFFERENT one, which is exactly what defeated the
    original distinct-byte count. *bpp* is overridable so the padding-exclusion
    tests can build a genuinely padded buffer regardless of the current depth — at
    32bpp every row is already aligned, so a same-depth frame could not exercise it.
    """
    row = bytes([fill]) * (width * bpp) + bytes([pad]) * (_stride(width, bpp=bpp) - width * bpp)
    return row * height


class TestHasContentRejectsABlankFrame:
    """A uniform frame is a FAILED capture and must never be relayed."""

    @pytest.mark.parametrize("width", [1005, 301, 302, 807, 1919])
    def test_row_padding_does_not_count_as_content(self, width: int) -> None:
        """**The defect this test exists for**, kept at the depth that HAS padding.

        Rows are DWORD-aligned, so a 24bpp frame of most widths carries 1-3 bytes of
        ``0x00`` padding per row. Those bytes are not pixels, but a naive
        distinct-byte count over the raw buffer sees them as a second value and
        passes a genuinely blank frame — measured on 694 of the 1245 padded widths in
        300..1959, including a real 1005x733 window. The model then receives a blank
        rectangle, which is worse than no image because it reasons about the blank as
        though it were the application.

        Pinned at an explicit 24bpp because a 32bpp row is inherently aligned: at the
        current depth there is no padding to mistake for a pixel, so a same-depth
        frame would assert nothing and the exclusion could be deleted unnoticed.
        """
        buf = _frame(width, 40, bpp=3)
        assert _stride(width, bpp=3) != width * 3, "this width must actually be padded"
        assert C._has_content(buf, width=width, bits_per_pixel=24) is False

    @pytest.mark.parametrize("width", [1920, 2560, 3840, 640])
    def test_an_unpadded_blank_frame_is_still_blank(self, width: int) -> None:
        """The current depth's shape: every row aligned, nothing but pixel bytes."""
        assert _stride(width) == width * _BPP
        assert C._has_content(_frame(width, 20), width=width) is False

    def test_an_all_zero_frame_is_blank(self) -> None:
        assert C._has_content(_frame(1005, 30, fill=0x00), width=1005) is False

    def test_an_empty_buffer_is_blank(self) -> None:
        assert C._has_content(b"", width=100) is False


class TestHasContentAcceptsARealFrame:
    """The gate must not discard captures that DID work."""

    def test_a_frame_with_a_varied_body_is_content(self) -> None:
        width, height = 1005, 200
        stride = _stride(width)
        buf = bytearray(_frame(width, height))
        # A gradient across the body, the shape of any real window content.
        for row in range(height):
            for col in range(0, width * _BPP, 7):
                buf[row * stride + col] = (row * 3 + col) % 256
        assert C._has_content(bytes(buf), width=width) is True

    @staticmethod
    def _vary_channel(width: int, height: int, channel: int) -> bytes:
        """A uniform frame whose *channel* alone varies across every pixel."""
        stride = _stride(width)
        buf = bytearray(_frame(width, height))
        for row in range(height):
            for pixel in range(width):
                buf[row * stride + pixel * _BPP + channel] = (pixel * 5) % 256
        return bytes(buf)

    @pytest.mark.parametrize("width", [1920, 2560, 3840])
    @pytest.mark.parametrize("channel", [0, 1, 2], ids=["blue", "green", "red"])
    def test_content_in_ANY_COLOUR_channel_is_not_discarded(self, width: int, channel: int) -> None:
        """The other direction of the bug: a comparison blind to some channels.

        A byte-wise probe step can alias onto a byte lane (1920x60 at 32bpp gives an
        even step, visiting two of four), so a frame differing only in the unvisited
        channels reads as blank and a real screenshot is thrown away. Comparing whole
        PIXELS removes the question — every channel is part of the compared value — and
        this is parametrized over all three colour channels to pin exactly that.
        """
        assert C._has_content(self._vary_channel(width, 60, channel), width=width) is True

    @pytest.mark.parametrize("width", [1920, 2560])
    def test_a_varying_ALPHA_lane_alone_is_NOT_content(self, width: int) -> None:
        """**Alpha is excluded, and that is a floor rather than a nicety.**

        GDI leaves alpha undefined on the ``PrintWindow`` path — a fresh DIB section is
        zeroed and what DWM writes there is unspecified — so a verdict that reads it
        depends on a byte no renderer promises. It fails in both directions, and the
        dangerous one is here: 32bpp opaque grey is ``20 20 20 FF``, two distinct BYTE
        values from ONE repeated pixel, so a byte-wise threshold of two passes a blank
        window on its alpha lane alone.
        """
        assert C._has_content(self._vary_channel(width, 60, 3), width=width) is False

    @pytest.mark.parametrize(
        ("pixel", "what"),
        [
            ((0x20, 0x20, 0x20, 0xFF), "opaque grey"),
            ((0xFF, 0xFF, 0xFF, 0xFF), "opaque white"),
            ((0x00, 0x00, 0x00, 0xFF), "opaque black"),
        ],
    )
    def test_a_uniform_OPAQUE_frame_is_blank(self, pixel: "tuple[int, ...]", what: str) -> None:
        """One repeated pixel is a failed capture whatever its alpha says."""
        width, height = 800, 60
        buf = (bytes(pixel) * width) * height
        assert C._has_content(buf, width=width) is False, f"a uniform {what} frame passed"

    def test_content_in_the_body_is_found_not_only_the_caption(self) -> None:
        """The DIB is top-down, so a head-only sample sees just the title bar.

        A fixed prefix produced two real failures: a solid caption over varied
        content read as blank, and WindowsTerminal's present caption over an absent
        swapchain body read as content.
        """
        width, height = 1005, 400
        stride = _stride(width)
        buf = bytearray(_frame(width, height))
        for row in range(height - 30, height):
            for col in range(0, width * _BPP, 5):
                buf[row * stride + col] = (col + row) % 256
        assert C._has_content(bytes(buf), width=width) is True


class TestHasContentWithoutAWidth:
    def test_it_stays_total_when_the_width_is_unknown(self) -> None:
        """Callers all pass a width; the fallback must not raise."""
        assert C._has_content(b"\x00" * 4096) is False
        assert C._has_content(b"\x00\xff" * 2048) is True


class TestBindGdiRefusesAnUnspecifiedSignature:
    """A missing ``argtypes`` truncates a 64-bit handle to 32 bits."""

    def test_a_row_without_argtypes_raises(self) -> None:
        """``windows_ffi._bind`` raises on this and is tested; the GDI twin was not.

        Without the check, a ``_GDI_FN_SPECS`` row that forgot its argtypes would
        bind silently and every HDC/HBITMAP over 2^31 would reach GDI corrupt — an
        access violation with no Python traceback.
        """

        class _Lib:
            def some_symbol(self) -> None: ...

        with pytest.raises(OSError, match="without argtypes"):
            C._bind_gdi(_Lib(), "some_symbol", ctypes.c_int, None)

    def test_every_shipped_row_declares_its_argtypes(self) -> None:
        for lib_key, symbol, _restype, argtypes in C._GDI_FN_SPECS:
            assert argtypes is not None, f"{lib_key}.{symbol} has no argtypes"
            assert isinstance(argtypes, list)

    def test_a_complete_row_binds(self) -> None:
        class _Fn:
            argtypes: object = None
            restype: object = None

        class _Lib:
            def __init__(self) -> None:
                self.some_symbol = _Fn()

        lib = _Lib()
        C._bind_gdi(lib, "some_symbol", ctypes.c_int, [ctypes.c_void_p])
        assert lib.some_symbol.argtypes == [ctypes.c_void_p]
        assert lib.some_symbol.restype is ctypes.c_int


class TestGdiLibrariesInitIsGuarded:
    """One-time native init on a POOLED executor is genuinely concurrent."""

    def test_concurrent_callers_initialize_once(self, monkeypatch) -> None:
        """``WinDLL("gdi32")`` returns a DISTINCT object per call.

        So two unlocked threads bind their own copies and one can publish while the
        other is mid-bind, handing a caller a function whose ``argtypes`` are unset —
        the truncation hazard the table exists to prevent. It also leaked a GDI+
        startup token per thread while only the last was retained.
        """
        monkeypatch.setattr(C, "_gdiplus", None)
        monkeypatch.setattr(C, "_gdi32", None)
        monkeypatch.setattr(C, "_gdiplus_token", None)

        startups: list[int] = []
        loads: list[str] = []

        class _FakeLib:
            def __getattr__(self, name: str):
                def call(*args, **kwargs):
                    if name == "GdiplusStartup":
                        startups.append(1)
                        return C._GDIP_OK
                    return C._GDIP_OK

                return call

        def fake_windll(name: str, **kwargs):
            loads.append(name)
            return _FakeLib()

        monkeypatch.setattr(C.ctypes, "WinDLL", fake_windll, raising=False)
        monkeypatch.setattr(C._bind_gdi, "__call__", lambda *a, **k: None, raising=False)
        monkeypatch.setattr(C, "_bind_gdi", lambda *a, **k: None)
        monkeypatch.setattr(C.windows_ffi, "libraries", lambda: {"user32": _FakeLib()})

        barrier = threading.Barrier(4)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                C._gdi_libraries()
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, errors
        assert len(startups) == 1, f"GdiplusStartup ran {len(startups)} times"
        assert loads.count("gdi32") == 1, f"gdi32 loaded {loads.count('gdi32')} times"


class TestEncoderQualityOwnership:
    """The quality LONG must be owned by the structure that points at it."""

    def test_a_pointer_field_registers_its_target(self) -> None:
        """A ``byref`` cast into a ``c_void_p`` field records NO ctypes ownership.

        The value then survives only while the local outlives the call, so a later
        refactor can silently make the encoder read a garbage quality. A real pointer
        object ties the LONG's lifetime to the structure.
        """
        params = C.EncoderParameters()
        value = ctypes.c_long(55)
        params.Parameter[0].Value = ctypes.cast(ctypes.pointer(value), ctypes.c_void_p)
        assert params._objects, "the pointed-at LONG is not owned by the structure"
        readback = ctypes.cast(params.Parameter[0].Value, ctypes.POINTER(ctypes.c_long))
        assert readback.contents.value == 55


class _Snap:
    """The Snapshot fields ``capture_snapshot_image`` reads."""

    def __init__(self, *, has_secure=False, truncated=False, depth_truncated=False, window_id=1):
        self.has_secure = has_secure
        self.truncated = truncated
        self.depth_truncated = depth_truncated

        class _App:
            pass

        self.app = _App()
        self.app.window_id = window_id


class TestCaptureSuppressionFloors:
    """The always-on floors, asserted at the function that enforces them."""

    @pytest.mark.parametrize(
        "snap",
        [
            _Snap(has_secure=True),
            _Snap(truncated=True),
            _Snap(depth_truncated=True),
            _Snap(window_id=0),
        ],
        ids=["secure", "truncated", "depth-truncated", "no-window"],
    )
    def test_a_suppressed_snapshot_is_returned_unchanged(self, snap, monkeypatch) -> None:
        """No native call may happen at all for a suppressed capture.

        ``has_secure`` is the credential floor (rendered password glyphs are a
        credential even though the tree redacted the value), and a truncated walk
        means ``has_secure`` is "none SEEN", not "none present" — so a password field
        past the cut would leave rendered credentials capturable.
        """

        def boom(*args, **kwargs):
            raise AssertionError("a suppressed snapshot must not reach the capture")

        monkeypatch.setattr(C, "_capture_window_bitmap", boom)
        monkeypatch.setattr(C.windows_ffi, "dpi_awareness_scope", boom)
        assert C.capture_snapshot_image(snap) is snap


class TestTheEncodeAndSpoolPath:
    """``_encode_jpeg`` / ``_read_stream`` / ``persist_jpeg``, driven through fakes.

    These are the functions between a captured bitmap and a file on disk, and the parts
    worth pinning are the DEGRADATIONS: each failure must return empty bytes or an empty
    path rather than a half-written screenshot, because the caller treats a non-empty
    result as a usable image.
    """

    @staticmethod
    def _gdiplus(monkeypatch, *, encoder=True, **status):
        """Install a fake GDI+ whose per-call status the test chooses."""

        class _GdiPlus:
            def __getattr__(self, name):
                def call(*args, **kwargs):
                    if name == "GdipCreateBitmapFromScan0" and args:
                        args[-1]._obj.value = 0x7000
                    if name == "GdipGetImageGraphicsContext" and args:
                        args[-1]._obj.value = 0x7100
                    return status.get(name, C._GDIP_OK)

                return call

        monkeypatch.setattr(C, "_gdi_libraries", lambda: (object(), object(), _GdiPlus()))
        clsid = C.windows_ffi.GUID() if encoder else None
        monkeypatch.setattr(C, "_jpeg_encoder_clsid", lambda g: clsid)

        class _Ole32:
            @staticmethod
            def CreateStreamOnHGlobal(a, b, out):
                out._obj.value = 0x8000
                return C.windows_ffi.S_OK

        monkeypatch.setattr(
            C.windows_ffi, "libraries", lambda: {"ole32": _Ole32(), "kernel32": object()}
        )
        monkeypatch.setattr(C.windows_ffi, "release", lambda p: None)
        monkeypatch.setattr(C, "_read_stream", lambda s: b"JPEGBYTES")
        # ``_encode_jpeg`` asks ``windows_ffi._guid`` for the quality-parameter GUID,
        # and that helper loads the real DLLs through ``_libraries`` — which does not
        # exist off Windows. Stubbed so this test measures the ENCODE's branches rather
        # than the platform's ctypes build.
        monkeypatch.setattr(C.windows_ffi, "_guid", lambda text: C.windows_ffi.GUID())

    def test_no_jpeg_encoder_returns_empty_rather_than_a_wrong_format(self, monkeypatch) -> None:
        """A host whose codec set lacks JPEG gets no image, not another format
        mislabelled as one."""
        self._gdiplus(monkeypatch, encoder=False)
        assert C._encode_jpeg(object(), 100, 50, max_px=64, quality=60) == (b"", 0, 0)

    def test_a_successful_downscale_reports_the_SCALED_dimensions(self, monkeypatch) -> None:
        self._gdiplus(monkeypatch)
        raw, w, h = C._encode_jpeg(object(), 1000, 500, max_px=500, quality=60)
        assert raw == b"JPEGBYTES"
        assert (w, h) == (500, 250), "the caller must be told what was actually encoded"

    def test_a_failed_scale_ALLOCATION_falls_back_to_the_original(self, monkeypatch) -> None:
        """Reporting the requested size for an image that was never scaled would
        mislabel a full-size screenshot as a thumbnail."""
        self._gdiplus(monkeypatch, GdipCreateBitmapFromScan0=1)
        _raw, w, h = C._encode_jpeg(object(), 1000, 500, max_px=500, quality=60)
        assert (w, h) == (1000, 500)

    def test_a_failed_DRAW_falls_back_to_the_original(self, monkeypatch) -> None:
        """A fresh Scan0 bitmap is fully transparent, so committing to it after a failed
        draw encodes a BLANK image and reports it as a screenshot — after the
        blank-frame gate has already run."""
        self._gdiplus(monkeypatch, GdipDrawImageRectI=1)
        _raw, w, h = C._encode_jpeg(object(), 1000, 500, max_px=500, quality=60)
        assert (w, h) == (1000, 500)

    def test_no_downscale_when_the_image_already_fits(self, monkeypatch) -> None:
        self._gdiplus(monkeypatch)
        _raw, w, h = C._encode_jpeg(object(), 80, 40, max_px=1280, quality=60)
        assert (w, h) == (80, 40)

    def test_a_failed_ENCODE_returns_empty(self, monkeypatch) -> None:
        self._gdiplus(monkeypatch, GdipSaveImageToStream=1)
        assert C._encode_jpeg(object(), 80, 40, max_px=64, quality=60) == (b"", 0, 0)

    @pytest.mark.parametrize(
        ("max_px", "expected"),
        [
            (0, (320, 160)),  # a config zero would otherwise be "no scaling at all"
            (-1, (320, 160)),
            (64, (320, 160)),  # below MIN_SCREENSHOT_MAX_PX
            (99999, (4096, 2048)),  # above MAX_SCREENSHOT_MAX_PX
        ],
        ids=["zero", "negative", "below-floor", "above-ceiling"],
    )
    def test_max_px_is_CLAMPED_as_it_is_on_macos(
        self, monkeypatch, max_px: int, expected: "tuple[int, int]"
    ) -> None:
        """The MCP schemas validate agent input, but this path is also reachable from
        config — where a zero or negative budget yields a degenerate image and an
        unbounded one yields a frame far larger than any consumer expects.
        ``capture_macos._encode_window`` clamps identically."""
        self._gdiplus(monkeypatch)
        _raw, w, h = C._encode_jpeg(object(), 10000, 5000, max_px=max_px, quality=60)
        assert (w, h) == expected

    @pytest.mark.parametrize("quality", [0, -5, 101, 9999])
    def test_an_out_of_range_quality_still_encodes(self, monkeypatch, quality: int) -> None:
        """Clamped to 1..100 rather than refused: a bad config value must not cost the
        observation, and GDI+ rejects the call outright outside that range."""
        self._gdiplus(monkeypatch)
        raw, _w, _h = C._encode_jpeg(object(), 800, 400, max_px=1280, quality=quality)
        assert raw == b"JPEGBYTES"


class TestPersistJpeg:
    def test_it_refuses_empty_bytes(self) -> None:
        """Nothing to write is not a file to report."""
        assert C.persist_jpeg(b"") == ""

    def test_it_writes_and_tightens_the_frame(self, monkeypatch, tmp_path) -> None:
        """The frame can contain anything on screen, so the file is locked down.

        ``restrict_to_owner`` emits a NON-inheritable ACE, which is why this per-file
        call is needed even though the directory is tightened too.
        """
        tightened: list = []
        monkeypatch.setattr(C, "shots_dir", lambda: str(tmp_path))
        monkeypatch.setattr(C, "trim_shots_dir", lambda: None)
        monkeypatch.setattr(C, "_dir_ready", True)
        monkeypatch.setattr(C.platform_compat, "restrict_to_owner", lambda p: tightened.append(p))
        path = C.persist_jpeg(b"JPEGBYTES")
        assert path and pathlib.Path(path).read_bytes() == b"JPEGBYTES"
        assert tightened == [path], "the frame was written without an owner-only DACL"

    def test_a_failed_tighten_KEEPS_the_frame(self, monkeypatch, tmp_path) -> None:
        """The image is already written; discarding it would lose the observation over a
        defence-in-depth step the per-user %TEMP% partly covers."""

        def boom(p):
            raise OSError("icacls unavailable")

        monkeypatch.setattr(C, "shots_dir", lambda: str(tmp_path))
        monkeypatch.setattr(C, "trim_shots_dir", lambda: None)
        monkeypatch.setattr(C, "_dir_ready", True)
        monkeypatch.setattr(C.platform_compat, "restrict_to_owner", boom)
        assert C.persist_jpeg(b"JPEGBYTES") != ""

    def test_an_unwritable_directory_returns_no_path(self, monkeypatch) -> None:
        def boom():
            raise OSError("read-only filesystem")

        monkeypatch.setattr(C, "ensure_shots_dir", boom)
        assert C.persist_jpeg(b"JPEGBYTES") == ""

    def test_a_failed_write_removes_the_partial_frame(self, monkeypatch, tmp_path) -> None:
        """A write failure must not strand the just-allocated frame in the spool.

        ``mkstemp`` succeeds and the SUBSEQUENT write raises: the caller is handed
        ``""`` and can never learn the path, so a leftover here is an invisible,
        partially-written file of the operator's screen pixels that nothing owns.
        The path is this invocation's own allocation, so removing exactly it is
        safe — pre-existing frames are untouched.
        """
        allocated: list[str] = []
        allocated_fds: list[int] = []
        real_mkstemp = C.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            allocated_fds.append(fd)
            allocated.append(path)
            return fd, path

        closed_by_os_close: list[int] = []
        real_close = C.os.close

        def recording_close(fd):
            closed_by_os_close.append(fd)
            real_close(fd)

        real_fdopen = C.os.fdopen

        def failing_fdopen(fd, *args, **kwargs):
            handle = real_fdopen(fd, *args, **kwargs)

            class _FailingHandle:
                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    # Close like the real ``with`` would, so the unlink under
                    # test runs against a closed file — the state Windows needs.
                    handle.close()
                    return False

                def write(self, data):
                    raise OSError("disk full")

            return _FailingHandle()

        monkeypatch.setattr(C, "shots_dir", lambda: str(tmp_path))
        monkeypatch.setattr(C, "trim_shots_dir", lambda: None)
        monkeypatch.setattr(C, "_dir_ready", True)
        monkeypatch.setattr(C.tempfile, "mkstemp", recording_mkstemp)
        monkeypatch.setattr(C.os, "fdopen", failing_fdopen)
        monkeypatch.setattr(C.os, "close", recording_close)
        assert C.persist_jpeg(b"JPEGBYTES") == ""
        assert allocated, "the seam never allocated a file, so the test exercised nothing"
        assert not pathlib.Path(allocated[0]).exists()
        assert list(tmp_path.iterdir()) == []
        # ``fdopen`` ADOPTED the descriptor, so the ``with`` owns closing it. An
        # explicit ``os.close`` on the raw number here would be a double-close
        # that can hit an unrelated descriptor another thread was just handed.
        assert not (set(allocated_fds) & set(closed_by_os_close))

    def test_a_failed_fdopen_closes_the_fd_and_removes_the_frame(
        self, monkeypatch, tmp_path
    ) -> None:
        """``fdopen`` raising BEFORE adopting the fd must not leak it or the file.

        Nothing else will ever close that descriptor, and Windows refuses to
        unlink a file with an open handle — so this branch (and only this
        branch) closes the raw fd before the unlink.
        """
        allocated: list[str] = []
        allocated_fds: list[int] = []
        real_mkstemp = C.tempfile.mkstemp

        def recording_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            allocated_fds.append(fd)
            allocated.append(path)
            return fd, path

        closed_by_os_close: list[int] = []
        real_close = C.os.close

        def recording_close(fd):
            closed_by_os_close.append(fd)
            real_close(fd)

        def boom_fdopen(fd, *args, **kwargs):
            raise OSError("out of file descriptors")

        monkeypatch.setattr(C, "shots_dir", lambda: str(tmp_path))
        monkeypatch.setattr(C, "trim_shots_dir", lambda: None)
        monkeypatch.setattr(C, "_dir_ready", True)
        monkeypatch.setattr(C.tempfile, "mkstemp", recording_mkstemp)
        monkeypatch.setattr(C.os, "fdopen", boom_fdopen)
        monkeypatch.setattr(C.os, "close", recording_close)
        assert C.persist_jpeg(b"JPEGBYTES") == ""
        assert allocated, "the seam never allocated a file, so the test exercised nothing"
        assert not pathlib.Path(allocated[0]).exists()
        assert list(tmp_path.iterdir()) == []
        assert closed_by_os_close.count(allocated_fds[0]) == 1


class TestEnsureShotsDir:
    def test_it_creates_the_directory_owner_only(self, monkeypatch, tmp_path) -> None:
        """A bare ``mkdir(mode=0o700)`` is inert on Windows, where access comes from the
        DACL — so the helper that applies BOTH is the one to call."""
        made: list = []
        target = tmp_path / "shots"
        monkeypatch.setattr(C, "shots_dir", lambda: str(target))
        monkeypatch.setattr(C, "_dir_ready", False)
        monkeypatch.setattr(C.platform_compat, "make_owner_only_dir", lambda p: made.append(str(p)))
        assert C.ensure_shots_dir() == str(target)
        assert made == [str(target)]

    def test_the_tighten_runs_ONCE_per_process(self, monkeypatch, tmp_path) -> None:
        """``restrict_to_owner`` shells out to icacls on Windows; once per screenshot
        would put a subprocess on the capture path."""
        calls: list = []
        monkeypatch.setattr(C, "shots_dir", lambda: str(tmp_path / "s"))
        monkeypatch.setattr(C, "_dir_ready", False)
        monkeypatch.setattr(C.platform_compat, "make_owner_only_dir", lambda p: calls.append(1))
        C.ensure_shots_dir()
        C.ensure_shots_dir()
        C.ensure_shots_dir()
        assert calls == [1]


class TestReadStream:
    def test_an_empty_global_returns_no_bytes(self, monkeypatch) -> None:
        class _Ole32:
            @staticmethod
            def GetHGlobalFromStream(s, out):
                out._obj.value = 0x9000
                return C.windows_ffi.S_OK

        class _Kernel32:
            @staticmethod
            def GlobalSize(h):
                return 0

        monkeypatch.setattr(
            C.windows_ffi,
            "libraries",
            lambda: {"ole32": _Ole32(), "kernel32": _Kernel32()},
        )
        assert C._read_stream(object()) == b""

    def test_a_failed_handle_lookup_returns_no_bytes(self, monkeypatch) -> None:
        class _Ole32:
            @staticmethod
            def GetHGlobalFromStream(s, out):
                return -2147467259

        monkeypatch.setattr(
            C.windows_ffi, "libraries", lambda: {"ole32": _Ole32(), "kernel32": object()}
        )
        assert C._read_stream(object()) == b""


class TestCaptureWindowBitmap:
    """The GDI path: window DC -> memory DC -> DIB -> pixel validation.

    Driven through a fake gdi32/gdiplus so the branch structure is exercised on any
    platform. What matters here is that every failure returns ``None`` and every handle
    is freed — a leaked HBITMAP or HDC is a kernel-pool object with a 10k per-process
    cap, so a leak on the observation path eventually kills the gateway.
    """

    @staticmethod
    def _gdi(monkeypatch, *, pixels=None, calls=None, scale=1.0, **status):
        """Install fake user32/gdi32/gdiplus, recording every handle-freeing call.

        *calls* collects the native calls whose ARGUMENTS carry meaning (the
        ``PrintWindow`` flag word), so a test can assert what was asked for rather
        than only what came back.
        """
        freed: list = []
        if calls is None:
            calls = []
        # A properly sized 64x8 DIB with VARIED pixels at the module's own depth.
        # ``_has_content`` samples by stride and excludes row padding, so a buffer of
        # the wrong LENGTH reads as blank and the success path is never exercised.
        _stride = ((64 * C._BITSPIXEL + 31) // 32) * 4
        _varied = bytes((i * 7) % 256 for i in range(_stride)) * 8
        content = pixels if pixels is not None else _varied

        class _User32:
            @staticmethod
            def GetWindowDC(h):
                return status.get("GetWindowDC", 0x100)

            @staticmethod
            def ReleaseDC(h, dc):
                freed.append(("ReleaseDC", dc))
                return 1

            @staticmethod
            def PrintWindow(h, dc, flags):
                calls.append(("PrintWindow", flags))
                return status.get("PrintWindow", 1)

        class _Gdi32:
            @staticmethod
            def CreateCompatibleDC(dc):
                return status.get("CreateCompatibleDC", 0x200)

            @staticmethod
            def DeleteDC(dc):
                freed.append(("DeleteDC", dc))
                return 1

            @staticmethod
            def CreateDIBSection(dc, info, usage, bits, section, offset):
                bits._obj.value = status.get("bits", 0x300)
                return status.get("CreateDIBSection", 0x400)

            @staticmethod
            def SelectObject(dc, obj):
                return 0x500

            @staticmethod
            def DeleteObject(obj):
                freed.append(("DeleteObject", obj))
                return 1

            @staticmethod
            def BitBlt(*args):
                return status.get("BitBlt", 1)

        class _GdiPlus:
            @staticmethod
            def GdipCreateBitmapFromHBITMAP(bmp, pal, out):
                out._obj.value = status.get("gdi_bitmap", 0x600)
                return status.get("GdipCreateBitmapFromHBITMAP", C._GDIP_OK)

        monkeypatch.setattr(C, "_gdi_libraries", lambda: (_User32(), _Gdi32(), _GdiPlus()))
        monkeypatch.setattr(
            C.windows_ffi, "window_bounds", lambda h: status.get("bounds", (0.0, 0.0, 64.0, 8.0))
        )
        monkeypatch.setattr(C.windows_ffi, "window_render_scale", lambda h: scale)
        monkeypatch.setattr(C.ctypes, "string_at", lambda addr, size: content)
        return freed

    def test_a_captured_window_returns_its_bitmap_and_size(self, monkeypatch) -> None:
        self._gdi(monkeypatch)
        result = C._capture_window_bitmap(0x10)
        assert result is not None
        _bmp, w, h = result
        assert (w, h) == (64, 8)

    def test_an_unknown_window_rect_returns_None(self, monkeypatch) -> None:
        self._gdi(monkeypatch, bounds=None)
        assert C._capture_window_bitmap(0x10) is None

    @pytest.mark.parametrize(
        "bounds", [(0.0, 0.0, 0.0, 8.0), (0.0, 0.0, 64.0, 0.0)], ids=["no-width", "no-height"]
    )
    def test_a_zero_area_window_returns_None(self, monkeypatch, bounds) -> None:
        """A minimized or collapsed window has no pixels to copy."""
        self._gdi(monkeypatch, bounds=bounds)
        assert C._capture_window_bitmap(0x10) is None

    def test_no_window_DC_returns_None(self, monkeypatch) -> None:
        self._gdi(monkeypatch, GetWindowDC=0)
        assert C._capture_window_bitmap(0x10) is None

    def test_a_failed_memory_DC_releases_the_window_DC(self, monkeypatch) -> None:
        freed = self._gdi(monkeypatch, CreateCompatibleDC=0)
        assert C._capture_window_bitmap(0x10) is None
        assert ("ReleaseDC", 0x100) in freed

    def test_a_failed_DIB_frees_every_handle_taken(self, monkeypatch) -> None:
        freed = self._gdi(monkeypatch, CreateDIBSection=0)
        assert C._capture_window_bitmap(0x10) is None
        assert ("DeleteDC", 0x200) in freed
        assert ("ReleaseDC", 0x100) in freed

    def test_a_BLANK_frame_returns_None(self, monkeypatch) -> None:
        """``PrintWindow`` reports success over a uniform buffer for a swapchain
        surface, so the pixels decide and a uniform frame is never relayed."""
        _blank = bytes([0xFF]) * (((64 * C._BITSPIXEL + 31) // 32) * 4 * 8)
        freed = self._gdi(monkeypatch, pixels=_blank)
        assert C._capture_window_bitmap(0x10) is None
        assert ("DeleteObject", 0x400) in freed, "the DIB leaked on the blank path"

    def test_a_blank_frame_is_NOT_retried_through_BitBlt(self, monkeypatch) -> None:
        """**A confinement floor, not an optimisation.**

        A window DC is a view onto the SCREEN, so ``BitBlt`` from it copies whatever
        occupies those pixels — including an overlapping window from an application
        the caller was never authorized for. The secure-field floor cannot catch that:
        it walks the TARGET's tree, so a password box owned by the window on top is
        invisible to it. A blank frame must therefore degrade to a tree-only snapshot
        rather than fall back to a path that can capture a third party.
        """
        called: list = []

        def forbidden(*args):  # pragma: no cover - reaching this IS the failure
            called.append(args)
            return 1

        _blank = bytes([0xFF]) * (((64 * C._BITSPIXEL + 31) // 32) * 4 * 8)
        self._gdi(monkeypatch, pixels=_blank)
        monkeypatch.setattr(C._gdi_libraries()[1], "BitBlt", forbidden, raising=False)
        assert C._capture_window_bitmap(0x10) is None
        assert called == [], "a blank PrintWindow fell back to BitBlt, which reads the screen"

    def test_the_capture_asks_for_PW_RENDERFULLCONTENT(self, monkeypatch) -> None:
        """The flag and the 32bpp buffer are one mechanism, so the flag is pinned.

        ``PW_RENDERFULLCONTENT`` renders through DWM, which is what makes a
        DirectComposition surface (any Chromium window) capturable at all: measured on
        one, every other flag and depth combination produced a single distinct byte
        value and only this flag at 32bpp produced a frame.
        """
        calls: list = []
        self._gdi(monkeypatch, calls=calls)
        assert C._capture_window_bitmap(0x10) is not None
        assert calls == [("PrintWindow", C.PW_RENDERFULLCONTENT)]

    def test_the_buffer_is_32bpp_because_DWM_will_not_render_into_24(self) -> None:
        """Not a size/quality preference: at 24bpp ``PW_RENDERFULLCONTENT`` returns 1
        and writes NOTHING, so the flag above is silently disabled and every capture
        falls through. Measured on two windows — a WinForms one yielded 1 distinct byte
        value at 24bpp against 180 at 32bpp."""
        assert C._BITSPIXEL == 32

    @pytest.mark.parametrize(
        ("scale", "expected"),
        [(1.0, (64, 8)), (0.8, (51, 6)), (0.5, (32, 4))],
        ids=["dpi-aware-window", "unaware-at-125pct", "unaware-at-200pct"],
    )
    def test_the_buffer_is_sized_to_what_the_window_RENDERS(
        self, monkeypatch, scale: float, expected: "tuple[int, int]"
    ) -> None:
        """``PrintWindow`` draws in the TARGET's coordinate space, not the caller's.

        A DPI-unaware window renders at its logical size into whatever buffer it is
        given, so sizing from the caller's DPI-aware rect leaves a black L-shaped
        margin and an image that no longer maps linearly onto the window rect the
        element frames are expressed in. Measured: a WinForms window drew 620x392 into
        both a 620x400 buffer and a 775x500 one.
        """
        self._gdi(monkeypatch, scale=scale)
        captured = C._capture_window_bitmap(0x10)
        assert captured is not None
        _bmp, width, height = captured
        assert (width, height) == expected

    def test_a_scale_that_collapses_the_window_returns_None(self, monkeypatch) -> None:
        """A zero-area buffer is not a capture, and ``CreateDIBSection`` would fail on
        it anyway — refused before any handle is taken."""
        self._gdi(monkeypatch, scale=0.0)
        assert C._capture_window_bitmap(0x10) is None

    def test_a_failed_GDIPLUS_wrap_frees_the_dib(self, monkeypatch) -> None:
        freed = self._gdi(monkeypatch, GdipCreateBitmapFromHBITMAP=1)
        assert C._capture_window_bitmap(0x10) is None
        assert ("DeleteObject", 0x400) in freed

    def test_a_NULL_gdiplus_bitmap_is_treated_as_failure(self, monkeypatch) -> None:
        """S_OK with a NULL out-param is the shape that would hand the encoder nothing."""
        self._gdi(monkeypatch, gdi_bitmap=0)
        assert C._capture_window_bitmap(0x10) is None

    def test_the_dib_is_ALWAYS_freed_on_the_success_path_too(self, monkeypatch) -> None:
        """The GDI+ bitmap owns a copy of the pixels, so the DIB is ours to release."""
        freed = self._gdi(monkeypatch)
        assert C._capture_window_bitmap(0x10) is not None
        assert ("DeleteObject", 0x400) in freed
        assert ("DeleteDC", 0x200) in freed
        assert ("ReleaseDC", 0x100) in freed


class TestCaptureSnapshotImageEndToEnd:
    """``capture_snapshot_image`` — the floors, then the happy path, then degradation."""

    @staticmethod
    def _snap(**kw):
        from kiro_crew.computer_use.types import AppRef, Snapshot

        app = AppRef(
            name="app",
            pid=1,
            bundle_id="app.exe",
            window_id=kw.pop("window_id", 0x10),
            window_title="T",
        )
        return Snapshot(app=app, elements=(), captured_at=1.0, **kw)

    @staticmethod
    def _plumbing(
        monkeypatch,
        *,
        minimized=False,
        captured=True,
        raw=b"JPEG",
        path="/tmp/s.jpeg",
        relayed=None,
    ):
        import contextlib

        monkeypatch.setattr(C.windows_ffi, "dpi_awareness_scope", contextlib.nullcontext)
        monkeypatch.setattr(C.windows_ffi, "window_is_minimized", lambda h: minimized)
        monkeypatch.setattr(
            C, "_capture_window_bitmap", lambda h: (object(), 100, 50) if captured else None
        )
        monkeypatch.setattr(C, "_encode_jpeg", lambda b, w, h, **k: (raw, 64, 32))
        monkeypatch.setattr(C, "persist_jpeg", lambda r: path)
        monkeypatch.setattr(C, "_gdi_libraries", lambda: (object(), object(), _DisposeSpy()))
        # The relay spawns a POST thread, so it is always replaced — a real one would
        # reach the network from a unit test.
        monkeypatch.setattr(
            C.screencast,
            "emit_snapshot_frame",
            lambda snap: (relayed.append(snap) if relayed is not None else True),
        )

    def test_a_full_capture_attaches_the_image_and_its_REAL_dimensions(self, monkeypatch) -> None:
        """The encode may have declined to scale, so recomputing from ``max_px`` here
        could mislabel a full-size image as downscaled."""
        self._plumbing(monkeypatch)
        out = C.capture_snapshot_image(self._snap())
        assert out.image_jpeg == b"JPEG"
        assert (out.image_width, out.image_height) == (64, 32)
        assert out.image_path == "/tmp/s.jpeg"

    def test_the_frame_is_MIRRORED_to_the_live_view(self, monkeypatch) -> None:
        """Without this call the dashboard's live-view panel is permanently blank on
        Windows, while working on macOS — the relay itself is platform-agnostic.

        The ENCODED snapshot is relayed, not the bare one: the mirror shows these exact
        already-downscaled bytes rather than capturing the window a second time.
        """
        relayed: list = []
        self._plumbing(monkeypatch, relayed=relayed)
        out = C.capture_snapshot_image(self._snap())
        assert len(relayed) == 1, "the live-view relay was never called"
        assert relayed[0] is out
        assert relayed[0].image_jpeg == b"JPEG"

    def test_a_RAISING_relay_does_not_lose_the_capture(self, monkeypatch) -> None:
        """A decorative mirror must never turn a successful observation into a failure.

        The relay is itself contracted never to raise; this pins the outer guard that
        holds if that inner contract is ever broken.
        """

        def boom(snap):
            raise RuntimeError("the relay broke its contract")

        self._plumbing(monkeypatch)
        monkeypatch.setattr(C.screencast, "emit_snapshot_frame", boom)
        assert C.capture_snapshot_image(self._snap()).image_jpeg == b"JPEG"

    def test_a_SUPPRESSED_capture_is_never_relayed(self, monkeypatch) -> None:
        """The secure floor returns before the relay exists to be called.

        The relay re-checks ``has_secure`` itself, but the frame must not reach it at
        all: a suppressed window has no encoded bytes to mirror.
        """
        relayed: list = []
        self._plumbing(monkeypatch, relayed=relayed)
        snap = self._snap(has_secure=True)
        assert C.capture_snapshot_image(snap) is snap
        assert relayed == []

    def test_a_MINIMIZED_window_is_refused_with_the_tree_intact(self, monkeypatch) -> None:
        """It stays IsWindowVisible with its rect parked off-screen, so the capture would
        succeed over a uniform buffer and the blank gate would reject it with no stated
        cause. The UIA tree of a minimized window reads perfectly well."""
        self._plumbing(monkeypatch, minimized=True)
        snap = self._snap()
        assert C.capture_snapshot_image(snap) is snap

    def test_a_failed_CAPTURE_degrades_to_the_tree(self, monkeypatch) -> None:
        self._plumbing(monkeypatch, captured=False)
        snap = self._snap()
        assert C.capture_snapshot_image(snap) is snap

    def test_an_EMPTY_encode_degrades_to_the_tree(self, monkeypatch) -> None:
        self._plumbing(monkeypatch, raw=b"")
        snap = self._snap()
        assert C.capture_snapshot_image(snap) is snap

    def test_a_failed_PERSIST_degrades_to_the_tree(self, monkeypatch) -> None:
        """No path means nothing to hand the model, even though bytes exist."""
        self._plumbing(monkeypatch, path="")
        snap = self._snap()
        assert C.capture_snapshot_image(snap) is snap

    def test_a_RAISING_capture_never_propagates(self, monkeypatch) -> None:
        """The tree is the primary channel, so a capture failure degrades the result
        rather than failing the whole observation."""
        import contextlib

        def boom(h):
            raise OSError("GDI exploded")

        monkeypatch.setattr(C.windows_ffi, "dpi_awareness_scope", contextlib.nullcontext)
        monkeypatch.setattr(C.windows_ffi, "window_is_minimized", lambda h: False)
        monkeypatch.setattr(C, "_capture_window_bitmap", boom)
        snap = self._snap()
        assert C.capture_snapshot_image(snap) is snap

    def test_an_unreadable_MINIMIZED_probe_still_attempts_the_capture(self, monkeypatch) -> None:
        """Never fatal: an unreadable window state degrades to trying, not to failing."""

        def boom(h):
            raise OSError("cannot query")

        self._plumbing(monkeypatch)
        monkeypatch.setattr(C.windows_ffi, "window_is_minimized", boom)
        assert C.capture_snapshot_image(self._snap()).image_jpeg == b"JPEG"

    def test_the_gdiplus_bitmap_is_DISPOSED_even_when_the_encode_raises(self, monkeypatch) -> None:
        """A GDI+ image is a native allocation; leaking one per capture is unbounded."""
        import contextlib

        spy = _DisposeSpy()

        def boom(b, w, h, **k):
            raise OSError("encoder exploded")

        monkeypatch.setattr(C.windows_ffi, "dpi_awareness_scope", contextlib.nullcontext)
        monkeypatch.setattr(C.windows_ffi, "window_is_minimized", lambda h: False)
        monkeypatch.setattr(C, "_capture_window_bitmap", lambda h: (object(), 100, 50))
        monkeypatch.setattr(C, "_encode_jpeg", boom)
        monkeypatch.setattr(C, "_gdi_libraries", lambda: (object(), object(), spy))
        snap = self._snap()
        assert C.capture_snapshot_image(snap) is snap
        assert spy.disposed, "the GDI+ bitmap leaked when the encode raised"


class _DisposeSpy:
    """Records ``GdipDisposeImage`` so a leak on an error path is visible."""

    def __init__(self) -> None:
        self.disposed: list = []

    def GdipDisposeImage(self, bitmap):  # noqa: N802 - the native symbol's name
        self.disposed.append(bitmap)
        return 0


class TestJpegEncoderLookup:
    """The CLSID is asked for rather than hardcoded, so an absent codec is clean."""

    @staticmethod
    def _gdiplus(monkeypatch, *, count=1, size=64, mime="image/jpeg", **status):
        class _GdiPlus:
            @staticmethod
            def GdipGetImageEncodersSize(c, s):
                c._obj.value = count
                s._obj.value = size
                return status.get("GdipGetImageEncodersSize", C._GDIP_OK)

            @staticmethod
            def GdipGetImageEncoders(c, s, buf):
                return status.get("GdipGetImageEncoders", C._GDIP_OK)

        monkeypatch.setattr(
            C,
            "ImageCodecInfo",
            type(
                "_FakeCodec",
                (),
                {"MimeType": mime, "Clsid": C.windows_ffi.GUID()},
            ),
        )
        return _GdiPlus()

    def test_a_failed_SIZE_query_is_None(self, monkeypatch) -> None:
        gp = self._gdiplus(monkeypatch, GdipGetImageEncodersSize=1)
        assert C._jpeg_encoder_clsid(gp) is None

    def test_no_encoders_at_all_is_None(self, monkeypatch) -> None:
        gp = self._gdiplus(monkeypatch, count=0, size=0)
        assert C._jpeg_encoder_clsid(gp) is None

    def test_a_failed_ENUMERATION_is_None(self, monkeypatch) -> None:
        gp = self._gdiplus(monkeypatch, GdipGetImageEncoders=1)
        assert C._jpeg_encoder_clsid(gp) is None


class TestResetDirGuardForTest:
    def test_it_clears_the_one_time_guard(self, monkeypatch) -> None:
        """Exists so a test can re-exercise the first-capture path; a real caller must
        never reset it, since the tighten spawns a process."""
        monkeypatch.setattr(C, "_dir_ready", True)
        C._reset_dir_guard_for_test()
        assert C._dir_ready is False
