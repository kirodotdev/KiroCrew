"""``WindowsHelper`` — the seam contract: no exception crosses it, ever.

Runs on EVERY platform: the helper's native calls are monkeypatched, so a Linux or
macOS shard exercises the same seam Windows does.

* **Inventory reaches the helper.** ``list_apps`` / ``resolve_app`` return populated
  results rather than a refusal.
* **Every failure becomes ``ok=False`` with a message written for a model.** The
  gateway dispatches on a pooled worker thread, so an exception escaping takes the
  whole tool call down instead of returning something the model can act on.

The input verbs' own behaviour (the pointer confinement, the refused methods, the
``SendInput`` records) lives in ``test_computer_use_windows_input.py``.
"""

from __future__ import annotations

import pytest

from kiro_crew.computer_use import windows_driver
from kiro_crew.computer_use.backend import NativeHelper
from kiro_crew.computer_use.types import (
    CLICK_METHOD_GLOBAL,
    PERMISSION_GRANTED,
    PERMISSION_UNSUPPORTED,
    PLATFORM_WINDOWS,
    AppRef,
    ClickRequest,
    ComputerUseError,
    DragRequest,
)
from kiro_crew.computer_use.windows_driver import WindowsHelper

_APP = AppRef(
    name="explorer", pid=1234, bundle_id="explorer.exe", window_id=0x1234, window_title="Documents"
)


@pytest.fixture
def driver() -> WindowsHelper:
    return WindowsHelper()


class TestNoInputVerbRaisesAcrossTheSeam:
    """Every input verb, called where the native layer cannot do it."""

    @pytest.mark.parametrize(
        ("label", "call"),
        [
            (
                "click",
                lambda d: d.click(_APP, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(1.0, 2.0))),
            ),
            (
                "drag",
                lambda d: d.drag(
                    _APP,
                    DragRequest(start=(1.0, 2.0), end=(3.0, 4.0), method=CLICK_METHOD_GLOBAL),
                ),
            ),
            ("type_text", lambda d: d.type_text(_APP, "hello")),
            ("press_key", lambda d: d.press_key(_APP, "return")),
        ],
    )
    def test_it_fails_cleanly(self, driver: WindowsHelper, label: str, call, monkeypatch) -> None:
        def boom(*args, **kwargs):
            raise OSError("native failure")

        for name in ("post_mouse_click", "post_mouse_drag", "send_text", "send_key_chord"):
            monkeypatch.setattr(windows_driver.windows_ffi, name, boom)
        monkeypatch.setattr(windows_driver.apps_windows, "hwnd_owns_point", lambda *a: True)
        result = call(driver)
        assert result.ok is False
        assert result.text
        # The dispatch layer adds the ``Error: `` prefix exactly once.
        assert not result.text.startswith("Error: ")
        assert "Traceback" not in result.text

    def test_it_is_a_native_helper(self, driver: WindowsHelper) -> None:
        assert isinstance(driver, NativeHelper)


class TestNoExceptionCrossesTheSeam:
    def test_a_computer_use_error_passes_its_message_through(
        self, driver: WindowsHelper, monkeypatch
    ) -> None:
        """That message is written FOR a model, so it must not be replaced."""

        def boom() -> None:
            raise ComputerUseError("the window for 'explorer' is no longer on screen")

        monkeypatch.setattr(windows_driver.apps_windows, "list_apps", boom)
        result = driver.list_apps()
        assert result.ok is False
        assert "no longer on screen" in result.text

    def test_an_unexpected_exception_is_reported_generically(
        self, driver: WindowsHelper, monkeypatch
    ) -> None:
        """An arbitrary ``str(exc)`` can carry internal paths, so it is not relayed."""

        def boom() -> None:
            raise OSError(r"C:\Users\someone\secret\path failed")

        monkeypatch.setattr(windows_driver.apps_windows, "list_apps", boom)
        result = driver.list_apps()
        assert result.ok is False
        assert "secret" not in result.text
        assert "OSError" in result.text

    @pytest.mark.parametrize("verb", ["list_apps", "resolve_app"])
    def test_no_read_verb_raises(self, driver: WindowsHelper, monkeypatch, verb: str) -> None:
        def boom(*args, **kwargs) -> None:
            raise RuntimeError("native failure")

        monkeypatch.setattr(windows_driver.apps_windows, "list_apps", boom)
        monkeypatch.setattr(windows_driver.apps_windows, "resolve_app", boom)
        call = {"list_apps": driver.list_apps, "resolve_app": lambda: driver.resolve_app("x")}[verb]
        assert call().ok is False


class TestStatusAndPermissions:
    def test_status_is_supported_when_the_client_loads(
        self, driver: WindowsHelper, monkeypatch
    ) -> None:
        monkeypatch.setattr(windows_driver.windows_ffi, "available", lambda: True)
        status = driver.status()
        assert status.supported is True
        assert status.platform_id == PLATFORM_WINDOWS
        assert status.reason == ""

    def test_status_names_the_reason_when_it_does_not(
        self, driver: WindowsHelper, monkeypatch
    ) -> None:
        monkeypatch.setattr(windows_driver.windows_ffi, "available", lambda: False)
        status = driver.status()
        assert status.supported is False
        # Concrete rather than "not supported": a user should learn what is missing.
        assert "could not be loaded" in status.reason

    def test_the_probe_reports_no_screen_recording_concept(
        self, driver: WindowsHelper, monkeypatch
    ) -> None:
        """Windows has no TCC, so there is no grant for an operator to enable.

        Reporting ``missing`` would send them to a settings pane that cannot help —
        the same reasoning that makes the macOS probe advisory.
        """
        monkeypatch.setattr(windows_driver.windows_ffi, "available", lambda: True)
        probe = driver.probe_permissions()
        assert probe.accessibility == PERMISSION_GRANTED
        assert probe.screen_recording == PERMISSION_UNSUPPORTED

    def test_the_probe_reports_unsupported_when_the_libraries_do_not_load(
        self, driver: WindowsHelper, monkeypatch
    ) -> None:
        monkeypatch.setattr(windows_driver.windows_ffi, "available", lambda: False)
        assert driver.probe_permissions().accessibility == PERMISSION_UNSUPPORTED


class TestClose:
    def test_close_never_raises_and_is_repeatable(self, driver: WindowsHelper) -> None:
        driver.close()
        driver.close()
