"""``LinuxBackend`` — the seam contract, the click-method matrix, and the floors.

Runs on EVERY platform, and in particular is GREEN under ``env -u DISPLAY``: the
native surface is reached only through ``linux_ffi`` / ``apps_linux`` /
``snapshot_linux`` seams these tests monkeypatch, so no X server and no library
load is involved. The branches with the worst consequences — a global click over a
window the app does not own, a write to a field that started masking, a focus that
did not move — cannot be produced on demand against a real desktop, so they are
driven through fakes here.

The two CI-critical guarantees the computer-use spec names:

* **Construction loads NO native library**, and ``status()`` reports unsupported
  with the no-display reason on a no-DISPLAY runner — so the whole upstream Linux
  CI fleet can build a real ``LinuxBackend`` without importing a lib.
* **No exception crosses the seam**: every verb returns ``DriverResult`` even when
  its target does not exist.
"""

from __future__ import annotations

import builtins
import contextlib

import pytest
from fake_linux_ffi import _isolated_linux_ffi

from kiro_crew import platform_compat
from kiro_crew.computer_use import linux_driver, linux_ffi, snapshot_linux
from kiro_crew.computer_use.backend import ComputerUseBackend, select_default_backend
from kiro_crew.computer_use.linux_driver import LinuxBackend
from kiro_crew.computer_use.snapshot_linux import SecureTargetError
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    PERMISSION_GRANTED,
    PERMISSION_UNSUPPORTED,
    PLATFORM_LINUX,
    AppRef,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    ElementRec,
)

# Pytest discovers the autouse fixture through this module-level name: every test
# runs against loud stubs on the REAL ``linux_ffi``, never the live X server or bus.
isolated_linux_ffi = _isolated_linux_ffi

# This file patches ``ctypes.CDLL``; ``platform_compat`` caches its first libc handle
# (``None`` included), so request the per-test reset or a fake poisons the worker.
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_APP = AppRef(
    name="gnome-calculator",
    pid=4242,
    bundle_id="gnome-calculator",
    window_id=0x5555,
    window_title="Calculator",
)
_REC = ElementRec(index=3, role="Button", title="Equals")


def _keyboard_ready(monkeypatch, *, focused: bool = True, active: bool = True) -> None:
    """Make keystrokes deliverable: the element reports FOCUSED (the only thing the
    driver trusts) and the app's window is the active X window."""
    monkeypatch.setattr(linux_ffi, "grab_focus", lambda o: False)
    monkeypatch.setattr(
        linux_ffi,
        "has_state",
        lambda o, s: focused if s == linux_ffi.ATSPI_STATE_FOCUSED else False,
    )
    monkeypatch.setattr(linux_ffi, "x_active_window", lambda: _APP.window_id if active else 0x9999)


@pytest.fixture
def driver() -> LinuxBackend:
    return LinuxBackend()


@pytest.fixture
def element(monkeypatch):
    """Make ``snapshot_linux.addressed`` yield a sentinel object, recording ``for_write``.

    Patched at the driver's own reference so no real walk runs — the ladder, focus
    and secure rules are what these tests are about, and resolving a real index
    would need a real AT-SPI tree.
    """
    calls: dict = {"for_write": None, "sentinel": object()}

    @contextlib.contextmanager
    def fake(app, rec, req, *, for_write: bool = False):
        calls["for_write"] = for_write
        yield calls["sentinel"]

    monkeypatch.setattr(snapshot_linux, "addressed", fake)
    return calls


# ──────────────────────────────────────────────────────────────────────────
# Construction loads nothing; status() is lazy and honest with no DISPLAY
# ──────────────────────────────────────────────────────────────────────────
class TestStatusIsLazyAndLibraryFree:
    def test_construction_does_not_probe_availability(self, monkeypatch) -> None:
        """**The CI guarantee.** Building a ``LinuxBackend`` must touch no library.

        ``available()`` is the one call that would load a lib; it must not run until
        the first ``status()``. Asserted by making it record each call and checking
        the count is zero after construction.
        """
        calls: list[int] = []
        monkeypatch.setattr(linux_ffi, "available", lambda: calls.append(1) or False)
        # The real ``unavailable_reason()`` re-enters ``available()`` when the module's
        # own cache is cold, and a patched ``available`` never warms that cache. Pin
        # the reason too, so the count below measures the DRIVER's probing only.
        monkeypatch.setattr(linux_ffi, "unavailable_reason", lambda: "no X display (DISPLAY unset)")
        d = LinuxBackend()
        assert calls == [], "constructing a LinuxBackend probed availability"
        d.status()
        assert calls == [1], "status() did not probe, or probed more than once"

    def test_status_caches_the_probe(self, monkeypatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(linux_ffi, "available", lambda: calls.append(1) or True)
        d = LinuxBackend()
        d.status()
        d.status()
        assert calls == [1], "the availability probe is not cached"

    def test_status_reports_the_no_display_reason(self, monkeypatch) -> None:
        """On a no-DISPLAY runner: unsupported, with the LIVE first-failing step."""
        monkeypatch.setattr(linux_ffi, "available", lambda: False)
        monkeypatch.setattr(linux_ffi, "unavailable_reason", lambda: "no X display (DISPLAY unset)")
        status = LinuxBackend().status()
        assert status.supported is False
        assert status.platform_id == PLATFORM_LINUX
        assert "no X display" in status.reason

    def test_status_is_supported_when_the_client_loads(self, monkeypatch) -> None:
        monkeypatch.setattr(linux_ffi, "available", lambda: True)
        status = LinuxBackend().status()
        assert status.supported is True
        assert status.reason == ""

    def test_status_never_loads_a_lib_even_under_a_raising_cdll(self, monkeypatch) -> None:
        """A real-ish probe path: with every ``CDLL`` raising, the real ``available()``
        still answers False (the session check runs first and loads nothing; a host
        with a display then fails at the library step), so the status is a clean
        unsupported, never an exception at construction.

        This is the shape the no-DISPLAY CI fleet is in; the module-scope import-safety
        is pinned structurally in ``test_computer_use_unsupported.py``.
        """
        import ctypes

        # This ONE test wants the REAL availability path, so drop the module's autouse
        # isolation stubs (they share this ``monkeypatch``), then clear the cached verdict.
        monkeypatch.undo()
        linux_ffi.reset_libraries()

        def boom(*a, **k):
            raise OSError("no native libraries here")

        monkeypatch.setattr(ctypes, "CDLL", boom)
        monkeypatch.setenv("DISPLAY", ":0")  # past the session check, to the library step
        try:
            d = LinuxBackend()
            status = d.status()
        finally:
            linux_ffi.reset_libraries()
        assert status.supported is False
        assert "no native libraries here" in status.reason


# ──────────────────────────────────────────────────────────────────────────
# Permissions
# ──────────────────────────────────────────────────────────────────────────
class TestPermissions:
    def test_granted_when_available_with_the_bridge_hint(self, monkeypatch) -> None:
        monkeypatch.setattr(linux_ffi, "available", lambda: True)
        probe = LinuxBackend().probe_permissions()
        assert probe.accessibility == PERMISSION_GRANTED
        assert probe.screen_recording == PERMISSION_GRANTED
        # The Linux failure mode that LOOKS like a missing permission is a disabled
        # bridge, so the hint names the env vars rather than a settings pane.
        assert "atk-bridge" in probe.responsible_hint
        assert "QT_ACCESSIBILITY" in probe.responsible_hint

    def test_unsupported_when_unavailable(self, monkeypatch) -> None:
        monkeypatch.setattr(linux_ffi, "available", lambda: False)
        probe = LinuxBackend().probe_permissions()
        assert probe.accessibility == PERMISSION_UNSUPPORTED
        assert probe.screen_recording == PERMISSION_UNSUPPORTED


# ──────────────────────────────────────────────────────────────────────────
# Selection and import-failure degradation
# ──────────────────────────────────────────────────────────────────────────
class TestSelection:
    def test_linux_selects_this_driver_on_any_runner(self, monkeypatch) -> None:
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        assert select_default_backend().platform_id == PLATFORM_LINUX

    def test_a_failed_driver_import_degrades_rather_than_crashing(self, monkeypatch) -> None:
        """A partial install must disable ONE capability, not the process.

        The caller asking "is computer use available" must get an answer, not an
        exception, so a module that will not import degrades to a typed refusal.
        """
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)

        real_import = builtins.__import__

        def fail(name, *args, **kwargs):
            if name == "kiro_crew.computer_use.linux_driver":
                raise ImportError("AT-SPI/X11 driver module failed to import")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fail)
        d = select_default_backend()
        assert d.platform_id == PLATFORM_LINUX
        assert d.status().supported is False


# ──────────────────────────────────────────────────────────────────────────
# No exception crosses the seam
# ──────────────────────────────────────────────────────────────────────────
class TestNoExceptionCrossesTheSeam:
    def test_a_computer_use_error_passes_its_message_through(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        def boom() -> None:
            raise ComputerUseError("no application matches 'ghost'")

        monkeypatch.setattr(linux_driver.apps_linux, "list_apps", boom)
        result = driver.list_apps()
        assert result.ok is False
        assert "no application matches 'ghost'" in result.text
        assert not result.text.startswith("Error: ")

    def test_an_unexpected_exception_is_reported_generically(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        def boom() -> None:
            raise OSError("/home/someone/secret/path failed")

        monkeypatch.setattr(linux_driver.apps_linux, "list_apps", boom)
        result = driver.list_apps()
        assert result.ok is False
        assert "secret" not in result.text
        assert "OSError" in result.text

    @pytest.mark.parametrize(
        ("label", "call"),
        [
            ("click", lambda d: d.click(_APP, _REC, ClickRequest())),
            (
                "drag",
                lambda d: d.drag(
                    _APP, DragRequest(start=(1.0, 2.0), end=(3.0, 4.0), method=CLICK_METHOD_GLOBAL)
                ),
            ),
            ("type_text", lambda d: d.type_text(_APP, _REC, "hi")),
            ("press_key", lambda d: d.press_key(_APP, _REC, "return")),
            ("set_value", lambda d: d.set_value(_APP, _REC, "v")),
            ("scroll", lambda d: d.scroll(_APP, _REC, "down", 1.0)),
            ("perform_action", lambda d: d.perform_action(_APP, _REC, "click")),
        ],
    )
    def test_every_input_verb_fails_cleanly(self, driver, element, label, call) -> None:
        """Each verb against a fake tree that yields a sentinel but whose ffi calls
        all raise (the isolation stubs' default): a clean refusal, not a ctypes error."""
        result = call(driver)
        assert result.ok is False
        assert result.text
        assert not result.text.startswith("Error: ")
        assert "Traceback" not in result.text

    def test_every_abstract_method_is_implemented(self, driver: LinuxBackend) -> None:
        assert isinstance(driver, ComputerUseBackend)
        for name in (
            "platform_id",
            "status",
            "probe_permissions",
            "list_apps",
            "resolve_app",
            "launch_app",
            "snapshot",
            "click",
            "drag",
            "type_text",
            "press_key",
            "set_value",
            "scroll",
            "perform_action",
            "close",
        ):
            assert hasattr(driver, name), name


# ──────────────────────────────────────────────────────────────────────────
# launch_app is a typed refusal, never a raise
# ──────────────────────────────────────────────────────────────────────────
class TestLaunchAppRefuses:
    def test_launch_is_not_implemented_and_points_at_the_running_path(
        self, driver: LinuxBackend
    ) -> None:
        result = driver.launch_app("gnome-text-editor")
        assert result.ok is False
        assert "not implemented" in result.text
        assert "computer_list_apps" in result.text


# ──────────────────────────────────────────────────────────────────────────
# auto never reaches the pointer; sky_click refused by name
# ──────────────────────────────────────────────────────────────────────────
class TestAutoNeverReachesThePointer:
    def test_auto_with_coordinates_refuses_naming_the_opt_ins(self, driver: LinuxBackend) -> None:
        result = driver.click(
            _APP, None, ClickRequest(method=CLICK_METHOD_AUTO, point=(10.0, 20.0))
        )
        assert result.ok is False
        assert "global" in result.text
        assert "app_post" in result.text

    def test_sky_click_refuses_by_name(self, driver: LinuxBackend) -> None:
        result = driver.click(
            _APP, None, ClickRequest(method=CLICK_METHOD_SKY_CLICK, point=(1.0, 2.0))
        )
        assert result.ok is False
        assert "macOS-only" in result.text

    def test_accessibility_without_an_index_refuses(self, driver: LinuxBackend) -> None:
        result = driver.click(_APP, None, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False


# ──────────────────────────────────────────────────────────────────────────
# The AT-SPI click (element-addressed, no pointer)
# ──────────────────────────────────────────────────────────────────────────
class TestElementClick:
    def test_it_names_the_winning_action(self, driver: LinuxBackend, element, monkeypatch) -> None:
        monkeypatch.setattr(linux_driver.input_linux, "perform_activate", lambda obj, role: "click")
        result = driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is True
        assert "via click" in result.text

    def test_no_action_refuses(self, driver: LinuxBackend, element, monkeypatch) -> None:
        monkeypatch.setattr(linux_driver.input_linux, "perform_activate", lambda obj, role: None)
        result = driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False
        assert "no action" in result.text

    def test_a_non_left_button_on_the_element_path_refuses(self, driver: LinuxBackend) -> None:
        """An accessibility action is a left activation; a right-click must name the
        route that can serve it rather than silently activating the control."""
        result = driver.click(
            _APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY, button="right")
        )
        assert result.ok is False
        assert "global" in result.text

    def test_the_element_path_moves_no_pointer(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        """**No XTest on the safe path.** The ladder must not touch the real cursor."""
        xtest: list = []
        monkeypatch.setattr(linux_driver.input_linux, "perform_activate", lambda obj, role: "click")
        monkeypatch.setattr(linux_ffi, "xtest_motion", lambda *a: xtest.append("motion"))
        monkeypatch.setattr(linux_ffi, "xtest_button", lambda *a: xtest.append("button"))
        driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert xtest == [], "the element path moved the real pointer"


# ──────────────────────────────────────────────────────────────────────────
# Pointer confinement for the global path
# ──────────────────────────────────────────────────────────────────────────
class TestGlobalClickConfinement:
    @staticmethod
    def _owns(monkeypatch, verdict: bool) -> list:
        seen: list = []

        def owns(app, x, y):
            seen.append((x, y))
            return verdict

        monkeypatch.setattr(linux_driver.apps_linux, "window_owns_point", owns)
        monkeypatch.setattr(linux_ffi, "xtest_motion", lambda *a: None)
        monkeypatch.setattr(linux_ffi, "xtest_button", lambda *a: None)
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)
        return seen

    def test_a_click_outside_the_window_is_refused(self, driver: LinuxBackend, monkeypatch) -> None:
        """Naming an allowed app while passing coordinates over a denied one would
        otherwise click the denied one — the point names whatever owns the pixel."""
        self._owns(monkeypatch, False)
        result = driver.click(
            _APP, None, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(5.0, 6.0))
        )
        assert result.ok is False
        assert "not owned by" in result.text
        assert "element_index" in result.text

    def test_a_click_inside_the_window_proceeds_and_says_the_cursor_moved(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        seen = self._owns(monkeypatch, True)
        result = driver.click(
            _APP, None, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(50.0, 60.0))
        )
        assert result.ok is True
        assert seen == [(50.0, 60.0)]
        assert "the real cursor moved" in result.text

    def test_a_right_click_by_global_is_served(self, driver: LinuxBackend, monkeypatch) -> None:
        buttons: list = []
        self._owns(monkeypatch, True)
        monkeypatch.setattr(
            linux_ffi, "xtest_button", lambda button, press: buttons.append((button, press))
        )
        result = driver.click(
            _APP, None, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(10.0, 20.0), button="right")
        )
        assert result.ok is True
        assert buttons[0] == (3, True), "a right-click did not post X button 3"


# ──────────────────────────────────────────────────────────────────────────
# app_post never moves the pointer
# ──────────────────────────────────────────────────────────────────────────
class TestAppPostNeverMovesThePointer:
    def test_app_post_posts_a_window_event_and_calls_no_xtest(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        """**The whole point of app_post: no pointer.** It must use ``x_send_button``
        (per-window XSendEvent) and never any ``xtest_*`` (global)."""
        xtest: list = []
        sent: list = []
        monkeypatch.setattr(linux_driver.apps_linux, "window_owns_point", lambda app, x, y: True)
        monkeypatch.setattr(
            linux_ffi, "x_send_button", lambda w, x, y, b, press: sent.append((w, b, press))
        )
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)
        monkeypatch.setattr(linux_ffi, "xtest_motion", lambda *a: xtest.append("motion"))
        monkeypatch.setattr(linux_ffi, "xtest_button", lambda *a: xtest.append("button"))
        result = driver.click(
            _APP, _REC, ClickRequest(method=CLICK_METHOD_APP_POST, point=(10.0, 20.0))
        )
        assert result.ok is True
        assert xtest == [], "app_post moved the real pointer"
        assert sent, "app_post posted no window event"
        assert sent[0][0] == _APP.window_id

    def test_app_post_outside_the_window_is_refused(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        monkeypatch.setattr(linux_driver.apps_linux, "window_owns_point", lambda app, x, y: False)
        result = driver.click(
            _APP, _REC, ClickRequest(method=CLICK_METHOD_APP_POST, point=(5.0, 6.0))
        )
        assert result.ok is False
        assert "not owned by" in result.text


# ──────────────────────────────────────────────────────────────────────────
# Drag: explicit method, every point confined
# ──────────────────────────────────────────────────────────────────────────
class TestDrag:
    def test_the_default_app_post_method_is_served_per_window(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        """Unlike Windows (which has only the real-cursor route), Linux has a
        per-window XSendEvent drag, so ``DragRequest``'s default ``app_post`` is a
        legitimate route — served, with window events and NO XTest, and the result
        says the toolkit may ignore them."""
        xtest: list = []
        sent: list = []
        monkeypatch.setattr(linux_driver.apps_linux, "window_owns_point", lambda app, x, y: True)
        monkeypatch.setattr(
            linux_ffi, "x_send_button", lambda w, x, y, b, press: sent.append(press)
        )
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)
        monkeypatch.setattr(linux_ffi, "xtest_button", lambda *a: xtest.append(1))
        monkeypatch.setattr(linux_ffi, "xtest_motion", lambda *a: xtest.append(1))
        result = driver.drag(_APP, DragRequest(start=(1.0, 2.0), end=(3.0, 4.0)))
        assert result.ok is True
        assert xtest == [], "the app_post drag moved the real pointer"
        assert sent, "the app_post drag posted no window event"
        assert "may ignore" in result.text

    def test_the_app_post_drag_posts_motion_through_every_interior_point(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        """**A path is a stroke, not two clicks.** Between button-down at the first
        point and button-up at the last, every interior confined point is posted as a
        ``MotionNotify`` with the button held — otherwise a ``curved`` app_post drag
        cannot trace its path and the docstring's "Motion events between" is false."""
        events: list = []
        monkeypatch.setattr(linux_driver.apps_linux, "window_owns_point", lambda app, x, y: True)
        monkeypatch.setattr(
            linux_ffi,
            "x_send_button",
            lambda w, x, y, b, press: events.append(("button", x, y, b, press)) or True,
        )
        monkeypatch.setattr(
            linux_ffi,
            "x_send_motion",
            lambda w, x, y, b: events.append(("motion", x, y, b)) or True,
        )
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)
        monkeypatch.setattr(linux_ffi, "xtest_button", lambda *a: events.append(("xtest",)))
        monkeypatch.setattr(linux_ffi, "xtest_motion", lambda *a: events.append(("xtest",)))
        result = driver.drag(
            _APP, DragRequest(start=(10.0, 10.0), end=(50.0, 10.0), steps=6, path="curved")
        )
        assert result.ok is True
        assert ("xtest",) not in events
        kinds = [e[0] for e in events]
        assert kinds[0] == "button" and events[0][4] is True, "no button-down first"
        assert kinds[-1] == "button" and events[-1][4] is False, "no button-up last"
        motions = [e for e in events if e[0] == "motion"]
        assert len(motions) >= 1, "no interior motion was posted"
        assert kinds[1:-1] == ["motion"] * len(motions), "motion not strictly between the buttons"
        assert all(m[3] == 1 for m in motions), "motion did not carry the held button"
        # The curved path bows off the straight chord: at least one interior y differs.
        assert any(m[2] != 10 for m in motions), "interior motion did not follow the path"

    def test_an_unknown_method_is_refused_naming_the_opt_ins(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        """``sky_click`` names no X11 drag route, so it is refused with the two that
        exist named in the message."""
        monkeypatch.setattr(linux_driver.apps_linux, "window_owns_point", lambda app, x, y: True)
        result = driver.drag(
            _APP, DragRequest(start=(1.0, 2.0), end=(3.0, 4.0), method=CLICK_METHOD_SKY_CLICK)
        )
        assert result.ok is False
        assert "global" in result.text

    def test_a_global_drag_moves_the_cursor(self, driver: LinuxBackend, monkeypatch) -> None:
        buttons: list = []
        monkeypatch.setattr(linux_driver.apps_linux, "window_owns_point", lambda app, x, y: True)
        monkeypatch.setattr(linux_ffi, "xtest_motion", lambda *a: None)
        monkeypatch.setattr(linux_ffi, "xtest_button", lambda b, press: buttons.append((b, press)))
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)
        result = driver.drag(
            _APP, DragRequest(start=(1.0, 2.0), end=(3.0, 4.0), method=CLICK_METHOD_GLOBAL)
        )
        assert result.ok is True
        assert buttons == [(1, True), (1, False)], "press and release were not posted"
        assert "the real cursor moved" in result.text

    def test_a_drag_confines_every_point(self, driver: LinuxBackend, monkeypatch) -> None:
        seen: list = []

        def owns(app, x, y):
            seen.append((x, y))
            return (x, y) != (3.0, 4.0)  # deny the endpoint

        monkeypatch.setattr(linux_driver.apps_linux, "window_owns_point", owns)
        monkeypatch.setattr(linux_ffi, "xtest_motion", lambda *a: None)
        monkeypatch.setattr(linux_ffi, "xtest_button", lambda *a: None)
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)
        result = driver.drag(
            _APP, DragRequest(start=(1.0, 2.0), end=(3.0, 4.0), method=CLICK_METHOD_GLOBAL)
        )
        assert result.ok is False, "a drag with a denied endpoint was performed"
        assert (3.0, 4.0) in seen


# ──────────────────────────────────────────────────────────────────────────
# The secure-field floor, re-read LIVE through for_write
# ──────────────────────────────────────────────────────────────────────────
class TestTheSecureFloor:
    @staticmethod
    def _secure_addressed(monkeypatch):
        """``addressed(for_write=True)`` RAISES ``SecureTargetError`` — the live
        re-read found a password field.  ``for_write=False`` yields normally."""

        @contextlib.contextmanager
        def fake(app, rec, req, *, for_write: bool = False):
            if for_write:
                raise SecureTargetError(
                    "refusing to write element 3 of gnome-calculator (role PasswordText)"
                )
            yield object()

        monkeypatch.setattr(snapshot_linux, "addressed", fake)

    def test_set_value_refuses_a_field_that_started_masking(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        """**The live re-read, not the stale snapshot flag.** A field can flip plain
        to password on the app's own timer, so ``set_value`` re-reads through
        ``for_write`` and the ``SecureTargetError`` reaches the model verbatim."""
        self._secure_addressed(monkeypatch)
        written: list = []
        monkeypatch.setattr(
            linux_ffi, "set_text_contents", lambda obj, v: written.append(v) or True
        )
        result = driver.set_value(_APP, _REC, "pa55word")
        assert result.ok is False
        assert "PasswordText" in result.text
        assert written == [], "a value was written to a secure field"

    def test_type_text_refuses_a_field_that_started_masking(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        self._secure_addressed(monkeypatch)
        typed: list = []
        monkeypatch.setattr(
            linux_driver.input_linux, "write_text_via_atspi", lambda o, t: typed.append(t) or True
        )
        monkeypatch.setattr(
            linux_driver.input_linux, "type_text_via_xtest", lambda t: typed.append(t) or len(t)
        )
        result = driver.type_text(_APP, _REC, "secret")
        assert result.ok is False
        assert "PasswordText" in result.text
        assert typed == [], "text was typed into a secure field"

    def test_perform_action_refuses_a_field_that_started_masking(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        """**`perform_action` is a write path too.** On Linux its vocabulary is the
        element's OWN AT-SPI actions, and a text field advertises ``clipboard.copy``
        / ``clipboard.paste`` / ``selection.delete`` — content reads and writes. A
        field that flipped plain→password after the snapshot must refuse here through
        the same live ``for_write`` re-read as ``set_value``, and no ``do_action`` may
        fire. (Found by the red-team pass: this verb addressed without ``for_write``.)"""
        self._secure_addressed(monkeypatch)
        performed: list = []
        monkeypatch.setattr(linux_ffi, "actions", lambda obj: ("clipboard.copy", "clipboard.paste"))
        monkeypatch.setattr(linux_ffi, "do_action", lambda obj, i: performed.append(i) or True)
        result = driver.perform_action(_APP, _REC, "clipboard.copy")
        assert result.ok is False
        assert "PasswordText" in result.text
        assert performed == [], "an action was performed on a secure field"


# ──────────────────────────────────────────────────────────────────────────
# type_text: EditableText is focus-free; the keystroke fallback takes focus
# ──────────────────────────────────────────────────────────────────────────
class TestTypeText:
    def test_an_editable_element_is_written_without_taking_focus(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        grabbed: list = []
        monkeypatch.setattr(linux_driver.input_linux, "write_text_via_atspi", lambda o, t: True)
        monkeypatch.setattr(linux_ffi, "grab_focus", lambda o: grabbed.append(1) or True)
        result = driver.type_text(_APP, _REC, "hello")
        assert result.ok is True
        assert "no focus change" in result.text
        assert grabbed == [], "an editable write stole focus"

    def test_a_non_editable_element_falls_back_to_keystrokes_and_takes_focus(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        monkeypatch.setattr(linux_driver.input_linux, "write_text_via_atspi", lambda o, t: False)
        _keyboard_ready(monkeypatch)
        monkeypatch.setattr(linux_driver.input_linux, "type_text_via_xtest", lambda t: len(t))
        result = driver.type_text(_APP, _REC, "hello")
        assert result.ok is True
        assert "focus was taken" in result.text

    def test_a_focus_failure_on_a_non_editable_element_REFUSES(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        """Typing past a failed aim reaches whatever the app had focused — possibly
        the password field the policy check refused."""
        typed: list = []
        monkeypatch.setattr(linux_driver.input_linux, "write_text_via_atspi", lambda o, t: False)
        _keyboard_ready(monkeypatch, focused=False)
        monkeypatch.setattr(
            linux_driver.input_linux, "type_text_via_xtest", lambda t: typed.append(t) or len(t)
        )
        result = driver.type_text(_APP, _REC, "secret")
        assert result.ok is False
        assert typed == [], "keystrokes were sent past a failed focus"

    def test_a_short_type_is_a_FAILURE(self, driver: LinuxBackend, monkeypatch) -> None:
        """A character the layout cannot reach is skipped, so a short count means a
        partial field — reported as a failure, not a success."""
        monkeypatch.setattr(linux_driver.input_linux, "type_text_via_xtest", lambda t: 2)
        monkeypatch.setattr(linux_ffi, "x_active_window", lambda: _APP.window_id)
        result = driver.type_text(_APP, None, "hello")
        assert result.ok is False
        assert "2 of 5" in result.text

    def test_typing_into_the_focused_element_of_a_BACKGROUND_window_refuses(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        """``rec is None`` means "whatever the app has focused" — which is only this
        app's control when its window is the active one. XTest delivers to the
        active window, so a background app's request would type into the foreground one."""
        typed: list = []
        monkeypatch.setattr(
            linux_driver.input_linux, "type_text_via_xtest", lambda t: typed.append(t) or len(t)
        )
        monkeypatch.setattr(linux_ffi, "x_active_window", lambda: 0x9999)
        result = driver.type_text(_APP, None, "hello")
        assert result.ok is False
        assert "active window" in result.text
        assert typed == []


# ──────────────────────────────────────────────────────────────────────────
# press_key: five specs, chords, focus
# ──────────────────────────────────────────────────────────────────────────
class TestPressKey:
    @staticmethod
    def _wire(monkeypatch, *, focused_after_grab=True, already_focused=False, active=True):
        """A keyboard-ready world: the element takes focus (verified by STATE, the
        only thing the driver trusts) and the app's window is the active X window."""
        events: list[tuple[int, bool]] = []
        state = {"focused": already_focused}

        def grab(o):
            state["focused"] = focused_after_grab
            return False  # the GTK 4 shape: the return is noise; the state is the truth

        monkeypatch.setattr(linux_ffi, "grab_focus", grab)
        monkeypatch.setattr(
            linux_ffi,
            "has_state",
            lambda o, s: state["focused"] if s == linux_ffi.ATSPI_STATE_FOCUSED else False,
        )
        monkeypatch.setattr(
            linux_ffi, "x_active_window", lambda: _APP.window_id if active else 0x9999
        )
        monkeypatch.setattr(linux_ffi, "x_keysym_to_keycode", lambda k: k)
        monkeypatch.setattr(
            linux_ffi, "xtest_key", lambda code, press: events.append((code, press))
        )
        monkeypatch.setattr(linux_ffi, "x_flush", lambda: None)
        return events

    def test_focus_is_verified_by_state_not_by_grab_focus_s_return(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        """Measured on GTK 4.16: ``grab_focus`` on the element that ALREADY holds focus
        fails with ``atspi_error (1)``. Trusting the return refused every keystroke
        into the one field a user would type into."""
        events = self._wire(monkeypatch, already_focused=True, focused_after_grab=False)
        result = driver.press_key(_APP, _REC, "return")
        assert result.ok is True, result.text
        assert events, "no keystroke was sent to an element that already had focus"

    def test_a_grab_that_reports_success_but_moves_nothing_REFUSES(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        """The Windows rule, same reason: an accepted request is not a moved focus."""
        events = self._wire(monkeypatch, focused_after_grab=False)
        monkeypatch.setattr(linux_ffi, "grab_focus", lambda o: True)
        result = driver.press_key(_APP, _REC, "return")
        assert result.ok is False
        assert events == [], "a chord was sent past an unverified focus"

    def test_a_background_window_REFUSES_even_with_element_focus(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        """Every toplevel remembers its focused widget, but XTest delivers to the ACTIVE
        window. An element focused inside a background window would receive nothing —
        and whatever is in front would receive everything."""
        events = self._wire(monkeypatch, active=False)
        result = driver.press_key(_APP, _REC, "return")
        assert result.ok is False
        assert "active window" in result.text
        assert events == []

    def test_no_active_window_property_REFUSES(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        """Measured: gnome-shell publishes no ``_NET_ACTIVE_WINDOW`` while the Activities
        overview is up. Nothing can be proven, so nothing is sent."""
        events = self._wire(monkeypatch)
        monkeypatch.setattr(linux_ffi, "x_active_window", lambda: None)
        result = driver.press_key(_APP, _REC, "return")
        assert result.ok is False
        assert events == []

    def test_an_element_less_chord_into_a_BACKGROUND_window_refuses(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        """The wrong-target hole found live: ``press_key(calculator, None, "9")`` said
        "sent 9" while the calculator was behind the text editor, and the editor's
        document gained a "9". With no element there is no focus to verify, so the
        active-window check is the ONLY thing standing between the authorized app and
        whichever one is actually in front."""
        events = self._wire(monkeypatch, active=False)
        result = driver.press_key(_APP, None, "9")
        assert result.ok is False
        assert "active window" in result.text
        assert events == [], "a chord was sent to a background window's app"

    def test_an_element_less_chord_into_the_ACTIVE_window_is_sent(
        self, driver: LinuxBackend, monkeypatch
    ) -> None:
        events = self._wire(monkeypatch)
        result = driver.press_key(_APP, None, "9")
        assert result.ok is True, result.text
        assert events

    @pytest.mark.parametrize("spec", ["a", "return", "ctrl+c", "cmd+s", "ctrl+shift+a"])
    def test_five_specs_are_accepted(
        self, driver: LinuxBackend, element, monkeypatch, spec: str
    ) -> None:
        self._wire(monkeypatch)
        result = driver.press_key(_APP, _REC, spec)
        assert result.ok is True
        assert "focus was taken" in result.text

    def test_ctrl_shift_a_releases_in_reverse_order(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        """**No modifier left stuck down.** The chord releases key then modifiers in
        reverse — asserted on the exact XTest events (keysym→keycode is identity)."""
        events = self._wire(monkeypatch)
        driver.press_key(_APP, _REC, "ctrl+shift+a")
        ctrl, shift, a = 0xFFE3, 0xFFE1, 0x0061
        assert events == [
            (ctrl, True),
            (shift, True),
            (a, True),
            (a, False),
            (shift, False),
            (ctrl, False),
        ]

    def test_a_capslock_modifier_is_refused(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        self._wire(monkeypatch)
        # "A" implies shift; a bare capslock chord is built directly.
        result = driver.press_key(_APP, _REC, "capslock+a")
        assert result.ok is False
        assert "shift+" in result.text

    def test_a_focus_failure_refuses_before_sending(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        sent: list = []
        _keyboard_ready(monkeypatch, focused=False)
        monkeypatch.setattr(linux_ffi, "x_keysym_to_keycode", lambda k: k)
        monkeypatch.setattr(linux_ffi, "xtest_key", lambda code, press: sent.append(code))
        result = driver.press_key(_APP, _REC, "ctrl+c")
        assert result.ok is False
        assert sent == [], "a chord was sent past a failed focus"

    def test_an_unknown_key_is_refused_before_anything_is_sent(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        sent: list = []
        _keyboard_ready(monkeypatch)
        monkeypatch.setattr(linux_ffi, "xtest_key", lambda code, press: sent.append(code))
        result = driver.press_key(_APP, _REC, "hyper+nope")
        assert result.ok is False
        assert sent == []


# ──────────────────────────────────────────────────────────────────────────
# set_value and perform_action
# ──────────────────────────────────────────────────────────────────────────
class TestSetValue:
    def test_it_asks_for_the_live_secure_check(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        monkeypatch.setattr(linux_ffi, "set_text_contents", lambda o, v: True)
        driver.set_value(_APP, _REC, "x")
        assert element["for_write"] is True

    def test_no_editable_interface_names_the_alternative(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        monkeypatch.setattr(linux_ffi, "set_text_contents", lambda o, v: False)
        result = driver.set_value(_APP, _REC, "x")
        assert result.ok is False
        assert "computer_type_text" in result.text


class TestPerformActionVocabularyIsTheElementsOwn:
    def test_an_action_the_element_does_not_advertise_is_refused(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        """A CLOSED vocabulary: only the element's own advertised actions, so this is
        not a free-form write path."""
        monkeypatch.setattr(linux_ffi, "actions", lambda o: ("click", "press"))
        monkeypatch.setattr(linux_ffi, "do_action", lambda o, i: True)
        result = driver.perform_action(_APP, _REC, "setvalue")
        assert result.ok is False
        assert "click" in result.text and "press" in result.text

    def test_an_advertised_action_is_performed(
        self, driver: LinuxBackend, element, monkeypatch
    ) -> None:
        done: list = []
        monkeypatch.setattr(linux_ffi, "actions", lambda o: ("click", "toggle"))
        monkeypatch.setattr(linux_ffi, "do_action", lambda o, i: done.append(i) or True)
        result = driver.perform_action(_APP, _REC, "toggle")
        assert result.ok is True
        assert done == [1], "the wrong action index was performed"


# ──────────────────────────────────────────────────────────────────────────
# Negative control #2 — paste the evidence
# ──────────────────────────────────────────────────────────────────────────
# Reverting the secure floor's ``for_write=True`` on the write path makes
# TestTheSecureFloor::test_set_value_refuses_a_field_that_started_masking FAIL.
# Verified on a scratch copy of linux_driver.py with ``set_value``'s
# ``self._addressed(app, rec, for_write=True)`` changed to ``for_write=False``:
# the fake ``addressed`` then yields normally instead of raising, so the write
# lands and the test fails:
#
#   $ env -u DISPLAY .venv/bin/python -m pytest -n0 \
#       "test/test_computer_use_linux_driver.py::TestTheSecureFloor::\
#       test_set_value_refuses_a_field_that_started_masking"
#   >       assert result.ok is False
#   E       assert True is False
#   1 failed
#
# The guard restored, the test passes. (See reports/driver.md § Negative controls.)
