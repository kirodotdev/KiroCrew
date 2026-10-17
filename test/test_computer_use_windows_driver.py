"""``WindowsBackend`` — the seam contract: no exception crosses it, ever.

Runs on EVERY platform: the driver's native helpers are monkeypatched, so a Linux
or macOS shard exercises the same seam Windows does. The established pattern is
flipping ``platform_compat.IS_WINDOWS``, and that is what the selection tests do.

Two guarantees are pinned here:

* **Observation reaches the driver.** ``list_apps`` / ``resolve_app`` /
  ``snapshot`` return populated results rather than a refusal.
* **Every failure becomes ``ok=False`` with a message written for a model.** The
  gateway dispatches driver calls on a pooled worker thread, so an exception
  escaping takes the whole tool call down instead of returning something the model
  can act on.

The INPUT verbs' own behaviour — the pattern ladder, the pointer confinement, the
focus rules and the secure-field floor — lives in
``test_computer_use_windows_input.py``, which drives them through fakes rather than
through the real desktop.
"""

from __future__ import annotations

import pytest

from kiro_crew import platform_compat
from kiro_crew.computer_use import windows_driver
from kiro_crew.computer_use.backend import ComputerUseBackend, select_default_backend
from kiro_crew.computer_use.types import (
    CLICK_METHOD_GLOBAL,
    PERMISSION_GRANTED,
    PERMISSION_UNSUPPORTED,
    PLATFORM_WINDOWS,
    AppRef,
    ClickRequest,
    ComputerUseError,
    DragRequest,
    ElementRec,
    Snapshot,
    SnapshotRequest,
)
from kiro_crew.computer_use.windows_driver import WindowsBackend

_APP = AppRef(
    name="explorer", pid=1234, bundle_id="explorer.exe", window_id=0x1234, window_title="Documents"
)
_REC = ElementRec(index=0, role="Button", title="Save")


@pytest.fixture
def driver() -> WindowsBackend:
    return WindowsBackend()


class TestNoInputVerbRaisesAcrossTheSeam:
    """Every mutating verb, called with a target that does not exist.

    Parametrized over the CALL rather than the method name so a signature change
    breaks the test instead of silently skipping it. The window in ``_APP`` is a
    fabricated handle, so each verb takes its "the window is gone" path — which is
    exactly the case that must produce a clean refusal rather than a ctypes error
    escaping into the worker thread.
    """

    @pytest.mark.parametrize(
        ("label", "call"),
        [
            ("click", lambda d: d.click(_APP, _REC, ClickRequest())),
            (
                "drag",
                lambda d: d.drag(
                    _APP,
                    DragRequest(start=(1.0, 2.0), end=(3.0, 4.0), method=CLICK_METHOD_GLOBAL),
                ),
            ),
            ("type_text", lambda d: d.type_text(_APP, _REC, "hello")),
            ("press_key", lambda d: d.press_key(_APP, _REC, "return")),
            ("set_value", lambda d: d.set_value(_APP, _REC, "value")),
            ("scroll", lambda d: d.scroll(_APP, _REC, "down", 1.0)),
            ("perform_action", lambda d: d.perform_action(_APP, _REC, "press")),
        ],
    )
    def test_it_fails_cleanly(self, driver: WindowsBackend, label: str, call) -> None:
        result = call(driver)
        assert result.ok is False
        assert result.text
        # The dispatch layer adds the ``Error: `` prefix exactly once, so a driver
        # that added its own would double it.
        assert not result.text.startswith("Error: ")
        assert "Traceback" not in result.text

    def test_a_failure_names_the_recovery_call(self, driver: WindowsBackend) -> None:
        """A model told only "no" retries; one told what to call next moves on.

        Asserted only where the native layer LOADS. Off Windows every verb short-
        circuits on ``ComputerUseUnsupported`` ("the Windows UI Automation driver
        needs Windows"), which is itself a correct and actionable refusal — but it is
        the platform's answer, not this message. The seam contract that every verb
        fails cleanly is covered above and does run everywhere.
        """
        if not driver.status().supported:
            # Keyed on the DRIVER, not on ``IS_WINDOWS``: what changes this message is
            # whether the native layer LOADED. Off Windows it cannot, and every verb
            # short-circuits on ``ComputerUseUnsupported`` instead — itself a correct
            # and actionable refusal, but the platform's answer rather than this one.
            pytest.skip("the driver's own refusal text needs the native layer to load")
        assert "computer_list_apps" in driver.click(_APP, _REC, ClickRequest()).text

    def test_every_abstract_method_is_implemented(self, driver: WindowsBackend) -> None:
        """A half-implemented driver must not be abstract-instantiable by accident."""
        assert isinstance(driver, ComputerUseBackend)
        for name in (
            "platform_id",
            "status",
            "probe_permissions",
            "list_apps",
            "resolve_app",
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


class TestObservationReachesTheDriver:
    def test_list_apps_returns_what_the_enumerator_found(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        monkeypatch.setattr(windows_driver.apps_windows, "list_apps", lambda: (_APP,))
        result = driver.list_apps()
        assert result.ok is True
        assert result.apps == (_APP,)

    def test_resolve_app_passes_the_query_through(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        seen: list[str] = []

        def resolve(query: str) -> AppRef:
            seen.append(query)
            return _APP

        monkeypatch.setattr(windows_driver.apps_windows, "resolve_app", resolve)
        assert driver.resolve_app("explorer").app == _APP
        assert seen == ["explorer"]

    def test_snapshot_returns_the_walk(self, driver: WindowsBackend, monkeypatch) -> None:
        snap = Snapshot(app=_APP, elements=(_REC,), captured_at=1.0)
        monkeypatch.setattr(
            windows_driver.snapshot_windows, "build_snapshot", lambda app, req: snap
        )
        result = driver.snapshot(_APP, SnapshotRequest(want_image=False))
        assert result.ok is True
        assert result.snapshot is snap

    def test_a_capture_is_a_separate_step_from_the_walk(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        """So the suppression rules live in exactly one place.

        The walk never decides whether to photograph; ``capture_snapshot_image``
        owns the secure-element and truncated-walk floors, and threading that
        decision into the walk would put it in two places.
        """
        snap = Snapshot(app=_APP, elements=(_REC,), captured_at=1.0)
        captured: list[Snapshot] = []

        def capture(s: Snapshot, *, max_px: int, quality: int) -> Snapshot:
            captured.append(s)
            return s

        monkeypatch.setattr(
            windows_driver.snapshot_windows, "build_snapshot", lambda app, req: snap
        )
        monkeypatch.setattr(windows_driver.capture_windows, "capture_snapshot_image", capture)

        driver.snapshot(_APP, SnapshotRequest(want_image=False))
        assert captured == []
        driver.snapshot(_APP, SnapshotRequest(want_image=True))
        assert captured == [snap]


class TestNoExceptionCrossesTheSeam:
    """The ABC's hard contract: convert every failure into ``ok=False``.

    The gateway dispatches on a pooled worker thread, so an exception escaping
    takes the tool call down instead of returning something the model can act on.
    """

    def test_a_computer_use_error_passes_its_message_through(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        """That message is written FOR a model, so it must not be replaced."""

        def boom() -> None:
            raise ComputerUseError("the window for 'explorer' is no longer on screen")

        monkeypatch.setattr(windows_driver.apps_windows, "list_apps", boom)
        result = driver.list_apps()
        assert result.ok is False
        assert "no longer on screen" in result.text

    def test_an_unexpected_exception_is_reported_generically(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        """An arbitrary ``str(exc)`` can carry internal paths, so it is not relayed."""

        def boom() -> None:
            raise OSError(r"C:\Users\someone\secret\path failed")

        monkeypatch.setattr(windows_driver.apps_windows, "list_apps", boom)
        result = driver.list_apps()
        assert result.ok is False
        assert "secret" not in result.text
        assert "OSError" in result.text

    @pytest.mark.parametrize("verb", ["list_apps", "resolve_app", "snapshot"])
    def test_no_read_verb_raises(self, driver: WindowsBackend, monkeypatch, verb: str) -> None:
        def boom(*args, **kwargs) -> None:
            raise RuntimeError("native failure")

        monkeypatch.setattr(windows_driver.apps_windows, "list_apps", boom)
        monkeypatch.setattr(windows_driver.apps_windows, "resolve_app", boom)
        monkeypatch.setattr(windows_driver.snapshot_windows, "build_snapshot", boom)
        call = {
            "list_apps": lambda: driver.list_apps(),
            "resolve_app": lambda: driver.resolve_app("x"),
            "snapshot": lambda: driver.snapshot(_APP, SnapshotRequest()),
        }[verb]
        assert call().ok is False


class TestStatusAndPermissions:
    def test_status_is_supported_when_the_client_loads(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        monkeypatch.setattr(windows_driver.windows_ffi, "available", lambda: True)
        status = driver.status()
        assert status.supported is True
        assert status.platform_id == PLATFORM_WINDOWS
        assert status.reason == ""

    def test_status_names_the_reason_when_it_does_not(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        monkeypatch.setattr(windows_driver.windows_ffi, "available", lambda: False)
        status = driver.status()
        assert status.supported is False
        # Concrete rather than "not supported": a user should learn what is missing.
        assert "UI Automation" in status.reason

    def test_the_probe_reports_no_screen_recording_concept(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        """Windows has no TCC, so there is no grant for an operator to enable.

        Reporting ``missing`` would send them to a settings pane that cannot help —
        the same reasoning that makes the macOS probe advisory.
        """
        monkeypatch.setattr(windows_driver.windows_ffi, "available", lambda: True)
        probe = driver.probe_permissions()
        assert probe.accessibility == PERMISSION_GRANTED
        assert probe.screen_recording == PERMISSION_UNSUPPORTED

    def test_the_probe_reports_unsupported_when_uia_is_unreachable(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        monkeypatch.setattr(windows_driver.windows_ffi, "available", lambda: False)
        assert driver.probe_permissions().accessibility == PERMISSION_UNSUPPORTED


class TestSelection:
    def test_windows_selects_this_driver_on_any_runner(self, monkeypatch) -> None:
        """Flipping one flag is how the Windows path is tested off Windows."""
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)
        assert select_default_backend().platform_id == PLATFORM_WINDOWS

    def test_a_failed_driver_import_degrades_rather_than_crashing(self, monkeypatch) -> None:
        """A partial install must disable ONE capability, not the process.

        Asserted by making the driver's constructor raise: the selector has to
        answer with a typed refusal, because the caller asking "is computer use
        available" must get an answer rather than an exception.
        """
        monkeypatch.setattr(platform_compat, "IS_MACOS", False)
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "IS_LINUX", False)

        def boom(*args, **kwargs):
            raise ImportError("UIAutomationCore missing")

        monkeypatch.setattr(windows_driver, "WindowsBackend", boom)
        driver = select_default_backend()
        assert driver.platform_id == PLATFORM_WINDOWS
        assert driver.status().supported is False

    def test_close_releases_every_worker_client(self, driver: WindowsBackend, monkeypatch) -> None:
        """close() must reach the pooled workers' clients, not just its own thread.

        release_all_clients, not reset_thread_state: close() runs on the caller's
        thread while the clients were created on worker threads whose thread-local
        it cannot see. A thread-local-only reset would release none of them.
        """
        calls: list[int] = []
        monkeypatch.setattr(
            windows_driver.windows_ffi, "release_all_clients", lambda: calls.append(1)
        )
        driver.close()
        driver.close()
        assert len(calls) == 2

    def test_close_never_raises(self, driver: WindowsBackend, monkeypatch) -> None:
        """A driver that cannot release its handles must not block a backend swap."""

        def boom() -> None:
            raise OSError("release failed")

        monkeypatch.setattr(windows_driver.windows_ffi, "release_all_clients", boom)
        driver.close()
