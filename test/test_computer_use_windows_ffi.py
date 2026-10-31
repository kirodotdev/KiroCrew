"""``computer_use.windows_ffi`` — the fail-closed secure read and the walk bounds.

Runs on EVERY platform: the native surface is reached only through module-level
seams these tests monkeypatch, so a Linux or macOS shard exercises the same
decision logic that runs on Windows. That matters because the branches with the
worst consequences are the ones a live probe cannot produce on demand — a
provider that does not implement ``IsPassword`` at all, or a read that fails.

Two properties are pinned here:

* **:func:`is_secure_element` fails CLOSED.** Three floors key off it (value
  redaction, keyboard-input refusal, whole-window screenshot suppression), so
  only an explicit, supported, live ``VARIANT_BOOL`` False may read as plain.
  Every other outcome — an error, the reserved not-supported sentinel, a
  surprising VARIANT type, an exception — must mean secure. A live probe on a
  real password box proves the happy path; these prove the rest.
* **:func:`walk_bounded` honours its budgets AND announces what it dropped.** A
  walk that silently truncated let the secure scan reflect only the nodes it
  reached, so a password field past the cut got the whole window captured.
"""

from __future__ import annotations

import ctypes
import inspect
from contextlib import contextmanager

import pytest

from kiro_crew.computer_use import windows_ffi as ffi


@contextmanager
def _null_scope():
    """A no-op stand-in for ``dpi_awareness_scope`` in tests that fake user32."""
    yield


class TestVtableTableIsTheOnlySourceOfSlotIndices:
    def test_only_iunknown_release_is_bound(self) -> None:
        """The UI Automation client is gone; the capture stream needs Release only."""
        assert set(ffi._VTABLE) == {("IUnknown", "Release")}
        assert ffi._VTABLE[("IUnknown", "Release")][0] == 2

    def test_every_row_declares_a_restype_and_argtypes(self) -> None:
        """A missing signature truncates a 64-bit pointer and segfaults."""
        for key, (slot, restype, argtypes) in ffi._VTABLE.items():
            assert isinstance(slot, int) and slot >= 0, key
            assert restype is not None, key
            assert isinstance(argtypes, list), key

    def test_vcall_refuses_an_unknown_method_rather_than_guessing(self) -> None:
        with pytest.raises(Exception):
            ffi.vcall(ctypes.c_void_p(1), "IUIAutomation", "NoSuchMethod")

    def test_every_flat_function_row_declares_both_halves(self) -> None:
        for lib_key, symbol, restype, argtypes in ffi._FN_SPECS:
            assert lib_key in ffi._LIB_NAMES, symbol
            assert argtypes is not None, symbol
            assert isinstance(argtypes, list), symbol

    def test_bind_raises_when_argtypes_is_missing(self) -> None:
        """The tripwire: a row without argtypes must be refused, not defaulted."""

        class _Lib:
            def __getattr__(self, name: str):
                raise AssertionError("must not reach the symbol")

        with pytest.raises(Exception):
            ffi._bind(_Lib(), "AnySymbol", None, None)


class TestAvailabilityIsPlatformGuarded:
    def test_available_is_false_off_windows_without_raising(self, monkeypatch) -> None:
        """The dashboard renders a Settings row on a host with no driver at all."""
        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        ffi.reset_libraries()
        try:
            assert ffi.available() is False
        finally:
            ffi.reset_libraries()

    def test_the_loader_raises_a_typed_error_off_windows(self, monkeypatch) -> None:
        """A typed refusal is what the backend converts into a tool refusal.

        A bare ``OSError`` from the loader would escape as an internal error
        instead.
        """
        from kiro_crew import platform_compat
        from kiro_crew.computer_use.types import ComputerUseUnsupported

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        ffi.reset_libraries()
        try:
            with pytest.raises(ComputerUseUnsupported):
                ffi.libraries()
        finally:
            ffi.reset_libraries()


@pytest.mark.skipif(
    not hasattr(ctypes, "WINFUNCTYPE"),
    reason=(
        "window_list builds a stdcall callback with ctypes.WINFUNCTYPE, which exists "
        "only in the Windows ctypes build. The rest of this module is deliberately "
        "cross-platform (its native surface is reached through patchable seams), but a "
        "callback TYPE is constructed at call time and cannot be faked without "
        "replacing the function under test."
    ),
)
class TestWindowListSurfacesACallbackFailure:
    """CPython cannot raise out of a ctypes callback."""

    @staticmethod
    def _reads(monkeypatch, *, title=lambda h: "A Window", exe=lambda pid: "app.exe") -> None:
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        monkeypatch.setattr(ffi, "window_text", title)
        monkeypatch.setattr(ffi, "window_root", lambda h: h)
        monkeypatch.setattr(ffi, "window_pid", lambda h: 42)
        monkeypatch.setattr(ffi, "window_class", lambda h: "Cls")
        monkeypatch.setattr(ffi, "window_bounds", lambda h: (0.0, 0.0, 10.0, 10.0))
        monkeypatch.setattr(ffi, "_exe_name", exe)

    @staticmethod
    def _enum(monkeypatch, handles, *, on_screen=True) -> None:
        class _User32:
            @staticmethod
            def IsWindowVisible(h):
                return True

            @staticmethod
            def IsIconic(h):
                return not on_screen

            @staticmethod
            def EnumWindows(callback, param):
                for handle in handles:
                    callback(handle, 0)
                return 1  # SUCCESS, which is the whole trap

        class _Dwmapi:
            @staticmethod
            def DwmGetWindowAttribute(h, attr, out, size):
                return ffi.S_OK  # not cloaked; ``out`` stays zero

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32(), "dwmapi": _Dwmapi()})

    def test_a_failing_callback_raises_rather_than_returning_a_short_list(
        self, monkeypatch
    ) -> None:
        """**The measured defect: 135 of 270 windows, and ``EnumWindows`` said OK.**

        A partial list reported as success is worse than an error, because
        ``apps_windows.list_apps`` then reports "no applications on screen" for a read
        it could not perform — and names ``computer_list_apps`` as the remedy, the
        call that just lied to the model.
        """

        def boom(pid):
            raise OSError("cannot query an elevated process")

        self._reads(monkeypatch, exe=boom)
        self._enum(monkeypatch, [11])
        with pytest.raises(ffi.ComputerUseUnsupported, match="enumerating windows failed"):
            ffi.window_list()

    def test_a_clean_enumeration_returns_its_windows(self, monkeypatch) -> None:
        self._reads(monkeypatch)
        self._enum(monkeypatch, [11, 12])
        assert [w.hwnd for w in ffi.window_list()] == [11, 12]

    def test_an_untitled_window_is_skipped_without_being_a_failure(self, monkeypatch) -> None:
        """Shell furniture is filtered, not reported as an enumeration error."""
        self._reads(monkeypatch, title=lambda h: "" if h == 11 else "Real")
        self._enum(monkeypatch, [11, 12])
        assert [w.hwnd for w in ffi.window_list()] == [12]


class TestDpiScopeRefusesAnUnknownAwareness:
    """A failed set must not silently run the block unaware."""

    def test_a_failed_set_raises(self, monkeypatch) -> None:
        """The worst outcome in the module: both halves agree on the wrong window.

        ``SetThreadDpiAwarenessContext`` returns NULL on failure, the same falsy value
        a successfully-restored default context can marshal as — so the return value
        alone cannot tell "the set failed" from "nothing to restore". Proceeding
        unaware is what made a physical point fed to ``WindowFromPoint`` resolve a
        DIFFERENT APPLICATION, which the confinement check cannot catch because it
        agrees with the click.
        """

        class _User32:
            @staticmethod
            def SetThreadDpiAwarenessContext(ctx):
                return None

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32()})
        monkeypatch.setattr(ffi._ct, "set_last_error", lambda code: None, raising=False)
        monkeypatch.setattr(
            ffi._ct, "get_last_error", lambda: 87, raising=False
        )  # ERROR_INVALID_PARAMETER

        with pytest.raises(ffi.ComputerUseUnsupported, match="SetThreadDpiAwarenessContext"):
            with ffi.dpi_awareness_scope():  # pragma: no cover - body must not run
                raise AssertionError("the block ran under an unknown DPI awareness")

    def test_a_falsy_previous_with_no_error_still_runs_the_block(self, monkeypatch) -> None:
        """NULL with no error means the set SUCCEEDED and there is nothing to restore."""

        class _User32:
            @staticmethod
            def SetThreadDpiAwarenessContext(ctx):
                return None

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32()})
        monkeypatch.setattr(ffi._ct, "set_last_error", lambda code: None, raising=False)
        monkeypatch.setattr(ffi._ct, "get_last_error", lambda: 0, raising=False)

        ran = []
        with ffi.dpi_awareness_scope():
            ran.append(1)
        assert ran == [1]

    def test_the_previous_context_is_always_restored(self, monkeypatch) -> None:
        """The pooled worker is shared with chat, terminal and browse work.

        A leaked PER_MONITOR_AWARE_V2 would make later ``GetWindowRect`` /
        ``GetCursorPos`` calls from unrelated code read physical rather than
        virtualized coordinates.
        """
        calls: list[object] = []

        class _User32:
            @staticmethod
            def SetThreadDpiAwarenessContext(ctx):
                calls.append(ctx)
                return 0x99  # a truthy previous context

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32()})
        monkeypatch.setattr(ffi._ct, "set_last_error", lambda code: None, raising=False)

        with pytest.raises(RuntimeError):
            with ffi.dpi_awareness_scope():
                raise RuntimeError("the caller's own failure")
        # Restored even when the body raised.
        assert calls[-1] == 0x99


class TestWindowIsOnScreen:
    """``IsWindowVisible`` means "not explicitly hidden", NOT "on screen".

    Three states pass it while being invisible to the operator, and each produced a
    window the model could read and photograph but the operator could not see —
    measured on one desktop, 4 of 12 enumerated windows. That is not cosmetic: the
    tool descriptions and the renderer both say "on-screen windows", a cloaked
    window's published origin OVERLAPS other applications' real pixels, and
    confinement's refusal then told the model to re-read bounds that never change,
    so the retry could not terminate. macOS gets this free from
    ``kCGWindowListOptionOnScreenOnly``.
    """

    @staticmethod
    def _libs(monkeypatch, *, iconic=False, cloaked=0, hr=None, bounds=(0.0, 0.0, 800.0, 600.0)):
        class _User32:
            @staticmethod
            def IsIconic(h):
                return iconic

        class _Dwmapi:
            @staticmethod
            def DwmGetWindowAttribute(h, attr, out, size):
                # ``out`` is a byref to a DWORD; write through it as the real call does.
                ctypes.cast(out, ctypes.POINTER(ctypes.c_ulong))[0] = cloaked
                return ffi.S_OK if hr is None else hr

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32(), "dwmapi": _Dwmapi()})
        monkeypatch.setattr(ffi, "window_bounds", lambda h: bounds)

    def test_an_ordinary_window_is_on_screen(self, monkeypatch) -> None:
        self._libs(monkeypatch)
        assert ffi.window_is_on_screen(0x10) is True

    def test_a_MINIMIZED_window_is_not(self, monkeypatch) -> None:
        """Its rect is parked at -32000,-32000, so no click can land inside it."""
        self._libs(monkeypatch, iconic=True)
        assert ffi.window_is_on_screen(0x10) is False

    def test_a_DWM_CLOAKED_window_is_not(self, monkeypatch) -> None:
        """A suspended packaged app, or a window on another virtual desktop. Measured
        live: HP Audio Control reported cloak value 2 while passing IsWindowVisible
        AND IsIconic-false, so neither ordinary check excludes it."""
        self._libs(monkeypatch, cloaked=2)
        assert ffi.window_is_on_screen(0x10) is False

    @pytest.mark.parametrize(
        "bounds",
        [(0.0, 0.0, 0.0, 600.0), (0.0, 0.0, 800.0, 0.0), None],
        ids=["no-width", "no-height", "unreadable"],
    )
    def test_a_zero_area_or_unreadable_rect_is_not(self, monkeypatch, bounds) -> None:
        self._libs(monkeypatch, bounds=bounds)
        assert ffi.window_is_on_screen(0x10) is False

    def test_an_unreadable_cloak_attribute_fails_OPEN(self, monkeypatch) -> None:
        """**Deliberately the permissive direction here.**

        ``DwmGetWindowAttribute`` is absent on a host without DWM composition, and
        treating "cannot ask" as "not on screen" would empty the entire application
        list — the model would be told the desktop has no windows. The ordinary
        ``IsIconic`` and rect reads still apply, and every downstream gesture is
        confined by ``apps_windows.hwnd_owns_point`` regardless.
        """
        self._libs(monkeypatch, hr=-2147024809)  # E_INVALIDARG
        assert ffi.window_is_on_screen(0x10) is True

    def test_a_RAISING_dwm_call_fails_OPEN_too(self, monkeypatch) -> None:
        class _User32:
            @staticmethod
            def IsIconic(h):
                return False

        class _Dwmapi:
            @staticmethod
            def DwmGetWindowAttribute(h, attr, out, size):
                raise OSError("dwmapi unavailable")

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32(), "dwmapi": _Dwmapi()})
        monkeypatch.setattr(ffi, "window_bounds", lambda h: (0.0, 0.0, 800.0, 600.0))
        assert ffi.window_is_on_screen(0x10) is True


class TestWindowRenderScale:
    """How much smaller a window draws itself than its DPI-aware rect.

    This ratio is what sizes a capture buffer. ``PrintWindow`` asks the window to
    draw in ITS OWN coordinate space, so a DPI-unaware window renders at its logical
    size into whatever buffer it is handed and an aware-sized one is left with a
    black margin — an image that does not map linearly onto the window rect the
    element frames use. Every failure answers 1.0, which is exactly right for an
    aware window and no worse than an unscaled capture for anything else.
    """

    @staticmethod
    def _libs(monkeypatch, *, window_dpi=96, monitor_dpi=96, monitor=0x500, hr=None):
        class _User32:
            @staticmethod
            def GetDpiForWindow(h):
                return window_dpi

            @staticmethod
            def MonitorFromWindow(h, flags):
                return monitor

        class _Shcore:
            @staticmethod
            def GetDpiForMonitor(mon, kind, dx, dy):
                dx._obj.value = monitor_dpi
                dy._obj.value = monitor_dpi
                return ffi.S_OK if hr is None else hr

        monkeypatch.setattr(ffi, "_libraries", lambda: {"user32": _User32(), "shcore": _Shcore()})

    @pytest.mark.parametrize(
        ("window_dpi", "monitor_dpi", "expected"),
        [
            (96, 96, 1.0),  # both unscaled
            (120, 120, 1.0),  # a per-monitor-aware app on a 125% display
            (96, 120, 0.8),  # THE case: an unaware window on a 125% display
            (96, 192, 0.5),  # unaware at 200%
            (192, 192, 1.0),  # aware at 200%
        ],
        ids=["unscaled", "aware-125", "unaware-125", "unaware-200", "aware-200"],
    )
    def test_the_ratio_is_the_windows_dpi_over_the_monitors(
        self, monkeypatch, window_dpi: int, monitor_dpi: int, expected: float
    ) -> None:
        """Measured on a 125% monitor: a WinForms window (96 over 120) drew 620x392
        into both a 620x400 buffer and a 775x500 one, while a Chromium window (120
        over 120) filled each buffer it was given."""
        self._libs(monkeypatch, window_dpi=window_dpi, monitor_dpi=monitor_dpi)
        assert ffi.window_render_scale(0x10) == pytest.approx(expected)

    def test_an_unreadable_window_dpi_is_1(self, monkeypatch) -> None:
        self._libs(monkeypatch, window_dpi=0)
        assert ffi.window_render_scale(0x10) == 1.0

    def test_no_monitor_is_1(self, monkeypatch) -> None:
        """A window with no monitor cannot be compared against one."""
        self._libs(monkeypatch, window_dpi=96, monitor=0)
        assert ffi.window_render_scale(0x10) == 1.0

    def test_a_failed_monitor_dpi_query_is_1(self, monkeypatch) -> None:
        self._libs(monkeypatch, window_dpi=96, monitor_dpi=120, hr=-2147024809)
        assert ffi.window_render_scale(0x10) == 1.0

    def test_a_zero_monitor_dpi_is_1_rather_than_a_ZeroDivisionError(self, monkeypatch) -> None:
        """S_OK with a zero out-param is the shape that would divide by zero on the
        observation path, where an exception costs the whole snapshot."""
        self._libs(monkeypatch, window_dpi=96, monitor_dpi=0)
        assert ffi.window_render_scale(0x10) == 1.0


class _FakeLib:
    """A flat DLL whose every symbol is a recorded, scriptable call.

    ``returns`` maps a symbol name to a value or a callable ``(*args) -> value``; an
    unlisted symbol answers ``S_OK``. Out-params are filled by the callable, which is
    how the native shape of each call is reproduced without a real DLL.
    """

    def __init__(self, **returns):
        self.returns = returns
        self.calls: list = []

    def __getattr__(self, name):
        def call(*args):
            self.calls.append((name, args))
            spec = self.returns.get(name, ffi.S_OK)
            return spec(*args) if callable(spec) else spec

        return call


class TestTheKeyboardRecords:
    """``send_key_chord`` / ``send_text`` build the batches ``SendInput`` takes."""

    @staticmethod
    def _spy(monkeypatch) -> list:
        batches: list = []

        def send(records):
            batches.append([(r.u.ki.wVk, r.u.ki.wScan, r.u.ki.dwFlags) for r in records])
            return len(records)

        monkeypatch.setattr(ffi, "_send", send)
        return batches

    def test_a_chord_presses_modifiers_first_and_releases_in_REVERSE(self, monkeypatch) -> None:
        """Reverse release is not cosmetic: releasing Ctrl before the key can register
        the key as an unmodified press in some targets."""
        batches = self._spy(monkeypatch)
        ffi.send_key_chord("c", ["ctrl", "shift"])
        down, up = batches
        assert [vk for vk, _s, _f in down] == [
            ffi.VK_MODIFIERS["ctrl"],
            ffi.VK_MODIFIERS["shift"],
            ffi.VK_CODES["c"],
        ]
        assert [vk for vk, _s, _f in up] == [
            ffi.VK_CODES["c"],
            ffi.VK_MODIFIERS["shift"],
            ffi.VK_MODIFIERS["ctrl"],
        ]
        assert all(flags & ffi.KEYEVENTF_KEYUP for _vk, _s, flags in up)

    def test_an_EXTENDED_key_carries_the_flag_in_BOTH_directions(self, monkeypatch) -> None:
        """Without it the injected scan code is the numpad twin, so an arrow key only
        moves the caret while NumLock happens to be off. A down carrying the flag and an
        up without it leaves the key logically held."""
        batches = self._spy(monkeypatch)
        ffi.send_key_chord("arrowleft", [])
        for batch in batches:
            assert all(flags & ffi.KEYEVENTF_EXTENDEDKEY for _vk, _s, flags in batch)

    def test_an_ordinary_key_does_NOT_carry_the_extended_flag(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        ffi.send_key_chord("a", [])
        for batch in batches:
            assert not any(flags & ffi.KEYEVENTF_EXTENDEDKEY for _vk, _s, flags in batch)

    def test_modifiers_are_RELEASED_even_when_the_press_is_rejected(self, monkeypatch) -> None:
        """A modifier left down is a stuck Ctrl on the operator's own keyboard, which
        changes every keystroke they type next."""
        batches: list = []

        def send(records):
            batches.append([(r.u.ki.wVk, r.u.ki.dwFlags) for r in records])
            return 0 if len(batches) == 1 else len(records)

        monkeypatch.setattr(ffi, "_send", send)
        ffi.send_key_chord("c", ["ctrl"])
        assert len(batches) == 2, "the release batch was not submitted"
        released = {vk for vk, fl in batches[1] if fl & ffi.KEYEVENTF_KEYUP}
        assert ffi.VK_MODIFIERS["ctrl"] in released

    @pytest.mark.parametrize(
        ("key", "mods"),
        [("definitely-not-a-key", []), ("a", ["hyper"]), ("a", ["fn"])],
        ids=["unknown-key", "unknown-modifier", "fn-has-no-vk"],
    )
    def test_an_unmappable_spec_RAISES_rather_than_dropping_it(
        self, monkeypatch, key: str, mods: list
    ) -> None:
        """A silently dropped modifier sends a DIFFERENT chord than the caller asked
        for. ``fn`` is the interesting one: it is a real modifier name with no virtual-key
        code at all, because Windows handles it in keyboard firmware."""
        sent = self._spy(monkeypatch)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.send_key_chord(key, mods)
        assert sent == []

    def test_text_becomes_UNICODE_records_two_per_code_unit(self, monkeypatch) -> None:
        """Layout-independent, unlike a per-character VK lookup which would type the
        wrong character on a non-US layout."""
        batches = self._spy(monkeypatch)
        assert ffi.send_text("hi") == 2
        (batch,) = batches
        assert len(batch) == 4  # two chars, down + up each
        assert all(flags & ffi.KEYEVENTF_UNICODE for _vk, _s, flags in batch)
        assert [scan for _vk, scan, _f in batch] == [ord("h"), ord("h"), ord("i"), ord("i")]

    def test_a_NON_BMP_character_is_two_code_units(self, monkeypatch) -> None:
        """An emoji is a surrogate PAIR, so it costs four records rather than two."""
        batches = self._spy(monkeypatch)
        assert ffi.send_text("\U0001f600") == 1
        (batch,) = batches
        assert len(batch) == 4, "a surrogate pair needs two code units"

    def test_empty_text_sends_nothing(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        assert ffi.send_text("") == 0
        assert batches == []

    def test_a_SHORT_accept_counts_whole_characters_only(self, monkeypatch) -> None:
        """A character half-written is not written. Dividing records by two reports an
        emoji-plus-``a`` as complete when only the emoji landed."""
        monkeypatch.setattr(ffi, "_send", lambda records: 4)
        assert ffi.send_text("\U0001f600a") == 1
        monkeypatch.setattr(ffi, "_send", lambda records: 2)
        assert ffi.send_text("abc") == 1


class TestReleaseBatchesAreVERIFIED:
    """A release is the one batch whose acceptance cannot be assumed.

    ``SendInput`` returns how many records it accepted and can stop early — a
    lower-level hook, or UIPI — and it returns NORMALLY. Submitting the release is
    therefore not enough: a discarded count leaves a modifier or a mouse button HELD
    on the operator's own machine, where a stuck Ctrl alters every keystroke they type
    next and a stuck button turns every motion of their hand into a drag.
    """

    @staticmethod
    def _spy(monkeypatch, accepts) -> list:
        """Install a ``_send`` whose accepted count follows *accepts* per call."""
        seen: list = []
        counts = list(accepts)

        def send(records):
            seen.append(len(records))
            want = counts.pop(0) if counts else len(records)
            return len(records) if want is None else want

        monkeypatch.setattr(ffi, "_send", send)
        return seen

    def test_a_fully_accepted_release_sends_once(self, monkeypatch) -> None:
        seen = self._spy(monkeypatch, [None])
        ffi._send_release([ffi._key_event(0x11, 0, ffi.KEYEVENTF_KEYUP)])
        assert seen == [1], "a clean release must not be retried"

    def test_an_unaccepted_TAIL_is_resubmitted(self, monkeypatch) -> None:
        """``SendInput`` accepts a PREFIX, so only ``records[accepted:]`` is resent.

        Resending the whole batch would re-release keys already up — harmless, but it
        muddies which record actually failed.
        """
        seen = self._spy(monkeypatch, [1, None])
        ups = [ffi._key_event(vk, 0, ffi.KEYEVENTF_KEYUP) for vk in (0x41, 0x10, 0x11)]
        ffi._send_release(ups)
        assert seen == [3, 2], "the retry must carry only the unaccepted tail"

    def test_an_ATOMIC_release_resubmits_the_WHOLE_batch(self, monkeypatch) -> None:
        """A drag's release is ``[move(end), up]`` and the two records are not independent.

        The move is what AIMS the up. A tail-only retry after a partial acceptance resends
        the bare ``up``, which carries no coordinate and therefore releases wherever the
        cursor happens to be — the drop lands outside the window ``hwnd_owns_point``
        authorized, which is exactly the defect the aimed release exists to prevent.
        Resending the move first is idempotent (a cursor position is state, not an edge).
        """
        seen = self._spy(monkeypatch, [1, None])
        # Opaque stand-ins rather than real records: ``_move_to`` normalizes against the
        # virtual screen through user32, which does not exist on a Linux shard, and
        # ``_send_release`` never inspects a record's contents.
        release = ["move(end)", "button_up"]
        ffi._send_release(release, atomic=True)
        assert seen == [2, 2], "the retry must carry the AIMING move, not just the button up"

    def test_a_NON_atomic_release_still_sends_only_the_tail(self, monkeypatch) -> None:
        # The default is unchanged: for independent key-ups, resending an already-released
        # key is harmless but muddies which record failed.
        seen = self._spy(monkeypatch, [1, None])
        ffi._send_release(["key_up_a", "key_up_shift"])
        assert seen == [2, 1]

    def test_the_DRAG_release_is_sent_atomically(self, monkeypatch) -> None:
        # The wiring, pinned. ``atomic`` defaults to False, so a drag that forgot to pass it
        # would silently keep the unaimed-retry defect with nothing going red.
        source = inspect.getsource(ffi.post_mouse_drag)
        assert "_send_release(release, atomic=True)" in source

    def test_a_release_that_never_lands_RAISES(self, monkeypatch) -> None:
        """Reporting success over a stuck modifier is what turns a failed keystroke
        into corrupted input, so this must not pass silently."""
        seen = self._spy(monkeypatch, [0, 0, 0])
        with pytest.raises(ffi.ComputerUseUnsupported, match="still be held"):
            ffi._send_release([ffi._key_event(0x11, 0, ffi.KEYEVENTF_KEYUP)])
        assert len(seen) == ffi._RELEASE_ATTEMPTS, "the retry budget must be bounded"

    def test_the_retry_budget_is_BOUNDED(self, monkeypatch) -> None:
        """An unbounded loop against a permanently-blocking hook would spin a pooled
        gateway worker forever."""
        assert 1 < ffi._RELEASE_ATTEMPTS <= 10

    def test_an_empty_release_is_a_no_op(self, monkeypatch) -> None:
        seen = self._spy(monkeypatch, [])
        ffi._send_release([])
        assert seen == []

    def test_a_CHORD_release_is_verified_not_merely_submitted(self, monkeypatch) -> None:
        """**The defect this class exists for.**

        ``send_key_chord`` released in a ``finally`` — which covers an exception but
        not a truncated release batch. A rejected modifier key-up left Ctrl held while
        the call reported success.
        """
        calls: list = []

        def send(records):
            calls.append([r.u.ki.wVk for r in records])
            # Reject the release batch outright, every time.
            return 0 if len(calls) > 1 else len(records)

        monkeypatch.setattr(ffi, "_send", send)
        with pytest.raises(ffi.ComputerUseUnsupported, match="still be held"):
            ffi.send_key_chord("a", ("ctrl",))
        assert len(calls) == 1 + ffi._RELEASE_ATTEMPTS


class TestTheMouseRecords:
    """Every function here moves the operator's REAL cursor."""

    @staticmethod
    def _spy(monkeypatch, *, accept=None, raises=None, on_call=1) -> list:
        """Record every batch. *accept* truncates the *on_call*-th batch's count.

        ``SendInput`` returning fewer than it was given is the case that leaves the
        operator's real mouse button held, and it returns NORMALLY — so a spy that
        always reports full acceptance cannot exercise it.

        *on_call* exists because a drag is not one batch. It submits
        ``[move, down]``, then one call per path point, then ``[move, up]`` — so the
        state where the button is PHYSICALLY HELD is a truncation on an intermediate
        MOVE batch (call 2+), not on the press. Truncating call 1 only means the press
        never landed, which strands nothing. A spy pinned to the first batch could no
        longer reach the hazard at all, which is how a truncation test can keep passing
        while testing nothing.
        """
        batches: list = []
        calls = {"n": 0}

        def send(records):
            batches.append([(r.type, r.u.mi.dwFlags, r.u.mi.dx, r.u.mi.dy) for r in records])
            calls["n"] += 1
            if calls["n"] == on_call:
                if raises is not None:
                    raise raises
                if accept is not None:
                    return accept
            return len(records)

        monkeypatch.setattr(ffi, "_send", send)
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 1920, 1200))
        return batches

    def test_a_click_moves_THEN_presses_in_one_batch(self, monkeypatch) -> None:
        """One batch so no other thread's input interleaves: a move and a press
        submitted separately can have the pointer moved between them."""
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(100.0, 200.0, button="left", count=1)
        (batch,) = batches
        assert batch[0][1] & ffi.MOUSEEVENTF_MOVE
        assert batch[1][1] == ffi.MOUSEEVENTF_LEFTDOWN
        assert batch[2][1] == ffi.MOUSEEVENTF_LEFTUP

    def test_a_double_click_repeats_the_button_pair(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(10.0, 10.0, button="left", count=2)
        (batch,) = batches
        downs = [f for _t, f, _x, _y in batch if f == ffi.MOUSEEVENTF_LEFTDOWN]
        assert len(downs) == 2

    @pytest.mark.parametrize(
        ("button", "down"),
        [
            ("left", ffi.MOUSEEVENTF_LEFTDOWN),
            ("right", ffi.MOUSEEVENTF_RIGHTDOWN),
            ("middle", ffi.MOUSEEVENTF_MIDDLEDOWN),
        ],
    )
    def test_each_button_sends_its_OWN_codes(self, monkeypatch, button: str, down: int) -> None:
        """Substituting a button performs a different gesture: a right-click that
        quietly becomes a left-click activates instead of opening a menu."""
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(10.0, 10.0, button=button, count=1)
        assert any(f == down for _t, f, _x, _y in batches[0])

    def test_an_unknown_button_RAISES_rather_than_defaulting(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.post_mouse_click(1.0, 1.0, button="pinky", count=1)
        assert batches == []

    @pytest.mark.parametrize("accepted", [1, 2], ids=["after-move", "after-button-down"])
    def test_a_TRUNCATED_click_batch_releases_the_button(self, monkeypatch, accepted: int) -> None:
        """**A stuck real mouse button is data loss, not a failed click.**

        ``SendInput`` returns how many records it accepted and can stop early — a
        lower-level hook, or UIPI blocking the injection mid-batch — and it returns
        NORMALLY, so nothing raises. A batch cut between the button-down and its
        button-up leaves the operator's own mouse button physically held, and every
        subsequent motion of their hand becomes a drag: on a file manager that moves
        their files, on a canvas it destroys their work.

        ``accepted=2`` is the dangerous case (move + down delivered, up dropped);
        ``accepted=1`` is checked too, because the release must not depend on
        correctly guessing which side of the pair the cut fell on.
        """
        batches = self._spy(monkeypatch, accept=accepted)
        with pytest.raises(ffi.ComputerUseUnsupported, match="partly delivered"):
            ffi.post_mouse_click(10.0, 20.0, button="left", count=1)
        assert len(batches) == 2, "no release batch was submitted"
        assert batches[1] == [
            (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)
        ], "the follow-up batch must be exactly the button RELEASE"

    def test_a_RAISING_click_batch_also_releases(self, monkeypatch) -> None:
        """A batch can be partly applied before the call fails, so the release runs on
        the exception path as well — and the original error still propagates."""
        boom = OSError("SendInput exploded")
        batches = self._spy(monkeypatch, raises=boom)
        with pytest.raises(OSError, match="exploded"):
            ffi.post_mouse_click(10.0, 20.0, button="right", count=1)
        assert batches[-1] == [(ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_RIGHTUP, 0, 0)]

    def test_a_FULLY_accepted_click_sends_no_extra_release(self, monkeypatch) -> None:
        """The guard must not double-click: the batch already contains its own up."""
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(10.0, 20.0, button="left", count=1)
        assert len(batches) == 1

    def test_a_TRUNCATED_drag_releases_and_REPORTS(self, monkeypatch) -> None:
        """A drag that pressed and moved but never released IS the stuck-button state,
        and reporting success would leave the caller believing the drop landed.

        The ``finally`` covers an exception; this covers the quieter truncation, which
        returns normally.

        **The truncation has to land on an INTERMEDIATE move**, which is why this uses
        ``on_call=2``: the press batch is ``[move, down]``, so a short accept there means
        the down never landed and no button is held. The dangerous state — button
        physically down, remaining motion dropped — is reachable only after the press
        succeeded. A version of this test that truncated the first batch was passing
        while exercising nothing.
        """
        batches = self._spy(monkeypatch, accept=0, on_call=2)
        with pytest.raises(ffi.ComputerUseUnsupported, match="partly delivered"):
            ffi.post_mouse_drag(
                (1.0, 2.0),
                (30.0, 40.0),
                button="left",
                points=[(1.0, 2.0), (15.0, 20.0), (30.0, 40.0)],
            )
        # The press DID land, so a button really was held when the truncation happened.
        pressed = [rec for batch in batches for rec in batch if rec[1] == ffi.MOUSEEVENTF_LEFTDOWN]
        assert pressed, "the press never landed, so this did not exercise a held button"
        assert batches[-1][-1] == (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)

    def test_a_truncated_PRESS_batch_is_also_reported(self, monkeypatch) -> None:
        # The other half: a short accept on the press means nothing was pressed, so
        # there is no held button — but the caller must still not be told the drag
        # landed, because the drop did not happen either.
        batches = self._spy(monkeypatch, accept=1, on_call=1)
        with pytest.raises(ffi.ComputerUseUnsupported, match="partly delivered"):
            ffi.post_mouse_drag((1.0, 2.0), (30.0, 40.0), button="left")
        assert batches[-1][-1] == (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)

    def test_a_FULLY_accepted_drag_still_releases_exactly_once(self, monkeypatch) -> None:
        batches = self._spy(monkeypatch)
        ffi.post_mouse_drag((1.0, 2.0), (30.0, 40.0), button="left")
        # Asserted on the RELEASE rather than on the batch count, because the number
        # of batches is now a function of the path length (one call per point) and
        # pinning it here would make every path-shape change edit this test.
        assert batches[-1][-1] == (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)
        ups = [rec for batch in batches for rec in batch if rec[1] == ffi.MOUSEEVENTF_LEFTUP]
        assert len(ups) == 1, "the button was released more than once"

    def test_the_RELEASE_is_AIMED_at_the_end_point(self, monkeypatch) -> None:
        """A bare ``up`` releases wherever the cursor IS, not where it was aimed.

        The button-up record carries no coordinate. That was harmless while the whole
        drag went out as one batch — nothing could interleave — but the path is now
        submitted as one ``SendInput`` per point, so there are N yield points where a
        foreign mouse move (the operator's own hand, another injector) can land between
        the last path point and the release.

        A release is where a DROP lands, and ``end`` is one of the two points
        ``hwnd_owns_point`` authorized, so the release is re-aimed immediately before
        the ``up`` — in the SAME batch, which is what makes the pair uninterruptible.
        """
        batches = self._spy(monkeypatch)
        ffi.post_mouse_drag((1.0, 2.0), (300.0, 400.0), button="left")
        release = batches[-1]
        assert len(release) == 2, release
        assert release[0][1] & ffi.MOUSEEVENTF_MOVE
        assert (release[0][2], release[0][3]) == ffi._normalized(300.0, 400.0)
        assert release[1] == (ffi.INPUT_MOUSE, ffi.MOUSEEVENTF_LEFTUP, 0, 0)

    def test_the_release_batch_is_built_BEFORE_the_travel(self, monkeypatch) -> None:
        """``_move_to`` can raise, and a release that raises is a HELD button.

        ``_normalized`` raises on a zero-extent virtual screen. If the release batch
        were constructed inside the ``finally``, that raise would happen on the way to
        sending the release rather than before the press — leaving the operator's
        button down, which is the one outcome the whole discipline exists to prevent.

        Driven by making construction fail: with a zero extent NOTHING is submitted, so
        the press never happens either and there is no button to strand.
        """
        batches: list = []
        monkeypatch.setattr(
            ffi, "_send", lambda records: batches.append(list(records)) or len(records)
        )
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 0, 0))
        with pytest.raises(ffi.ComputerUseUnsupported, match="zero extent"):
            ffi.post_mouse_drag((1.0, 2.0), (30.0, 40.0), button="left")
        assert batches == [], "a batch was submitted despite an unbuildable release"

    def test_a_drag_MOVES_between_press_and_release(self, monkeypatch) -> None:
        """A press and release at two points with no motion between is not a drag to
        most applications: they start the drag on the first move after button-down.

        The aim and the press stay in ONE batch — nothing may interleave between
        aiming and pressing — and the motion follows.
        """
        batches = self._spy(monkeypatch)
        ffi.post_mouse_drag((10.0, 10.0), (90.0, 90.0), button="left")
        press = batches[0]
        assert press[0][1] & ffi.MOUSEEVENTF_MOVE
        assert press[1][1] == ffi.MOUSEEVENTF_LEFTDOWN
        flat = [rec for batch in batches for rec in batch]
        down_at = next(i for i, rec in enumerate(flat) if rec[1] == ffi.MOUSEEVENTF_LEFTDOWN)
        up_at = next(i for i, rec in enumerate(flat) if rec[1] == ffi.MOUSEEVENTF_LEFTUP)
        moved_between = [rec for rec in flat[down_at + 1 : up_at] if rec[1] & ffi.MOUSEEVENTF_MOVE]
        assert moved_between, "no motion between the press and the release"

    def test_each_path_POINT_is_its_own_SendInput_call(self, monkeypatch) -> None:
        """THE fidelity invariant, and it looks backwards without the measurement.

        Every other input path in this module batches deliberately (``_send``'s own
        docstring explains why: nothing may interleave inside a chord). A drag path is
        the exception: 65 move records in ONE batch drew a visibly corner-y polyline,
        while the same 65 points as 65 separate calls drew a smooth curve. A
        ``WH_MOUSE_LL`` hook counted 65 of 65 events reaching the queue in BOTH shapes,
        so nothing is dropped — Windows synthesizes ``WM_MOUSEMOVE`` from queue STATE
        when the target's pump asks, so one batch mutates the position many times
        before the target samples it once.

        Pinned because a reviewer reading ``_send``'s docstring would reasonably
        "fix" this back into a single batch, and the regression is invisible in code:
        it produces a drag that still works and does not draw.
        """
        batches = self._spy(monkeypatch)
        points = [(float(i * 10), 0.0) for i in range(6)]
        ffi.post_mouse_drag(points[0], points[-1], button="left", points=points)
        # One batch for (aim + press), then one per remaining point, then the release.
        assert len(batches) == 1 + (len(points) - 1) + 1
        for batch in batches[1:-1]:
            assert len(batch) == 1, "an intermediate move shared a batch"
            assert batch[0][1] & ffi.MOUSEEVENTF_MOVE

    def test_a_drag_RELEASES_even_when_the_press_batch_fails(self, monkeypatch) -> None:
        """A button left down is a stuck mouse button, and every later motion becomes an
        unintended drag.

        A wholly rejected batch (0 accepted) is also REPORTED, not just released: the
        caller would otherwise be told the drag succeeded while nothing was delivered,
        and would believe a drop landed somewhere it never did.
        """
        batches: list = []

        def send(records):
            batches.append([r.u.mi.dwFlags for r in records])
            return 0 if len(batches) == 1 else len(records)

        monkeypatch.setattr(ffi, "_send", send)
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 1920, 1200))
        with pytest.raises(ffi.ComputerUseUnsupported, match="partly delivered"):
            ffi.post_mouse_drag((1.0, 1.0), (2.0, 2.0), button="left")
        assert ffi.MOUSEEVENTF_LEFTUP in batches[-1]

    def test_a_drag_PRESSES_and_RELEASES_at_the_callers_endpoints(self, monkeypatch) -> None:
        """The path's own ends must not move the press or the release.

        Those two points are what the confinement check authorized, and a release is
        where a drop lands — so a path whose endpoints drifted by a float epsilon (or
        a caller that passed a path with different ends) must not relocate either.
        """
        batches = self._spy(monkeypatch)
        # A path deliberately disagreeing with the endpoints at BOTH ends.
        ffi.post_mouse_drag(
            (100.0, 100.0),
            (400.0, 400.0),
            button="left",
            points=[(1.0, 1.0), (250.0, 250.0), (999.0, 999.0)],
        )
        flat = [rec for batch in batches for rec in batch]
        aim = flat[0]
        release_batch = batches[-1]
        assert aim[1] & ffi.MOUSEEVENTF_MOVE
        assert (aim[2], aim[3]) == ffi._normalized(100.0, 100.0)
        assert release_batch[-1][1] == ffi.MOUSEEVENTF_LEFTUP
        # The last MOVE is the caller's end point, not the path's 999,999 — and it is
        # the one paired with the release, so the drop lands at the authorized point.
        moves = [rec for rec in flat if rec[1] & ffi.MOUSEEVENTF_MOVE]
        assert (moves[-1][2], moves[-1][3]) == ffi._normalized(400.0, 400.0)
        assert (release_batch[0][2], release_batch[0][3]) == ffi._normalized(400.0, 400.0)

    def test_an_unknown_drag_button_RAISES(self, monkeypatch) -> None:
        self._spy(monkeypatch)
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi.post_mouse_drag((1.0, 1.0), (2.0, 2.0), button="pinky")

    def test_a_move_is_ABSOLUTE_over_the_VIRTUAL_desktop(self, monkeypatch) -> None:
        """``VIRTUALDESK`` is required for a multi-monitor desktop; without it the
        coordinates are interpreted against the primary monitor alone."""
        batches = self._spy(monkeypatch)
        ffi.post_mouse_click(0.0, 0.0, button="left", count=1)
        move_flags = batches[0][0][1]
        assert move_flags & ffi.MOUSEEVENTF_ABSOLUTE
        assert move_flags & ffi.MOUSEEVENTF_VIRTUALDESK
