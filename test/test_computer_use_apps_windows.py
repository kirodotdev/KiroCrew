"""``computer_use.apps_windows`` — app identity, resolution, and the confinement floor.

Runs on every platform: the module reaches Windows only through
``windows_ffi.window_list`` / ``window_bounds`` / ``root_window_at_point``, which these
tests replace, so a Linux shard exercises the same decisions.

Two of these carry security weight rather than convenience:

* **Identity is what ``policy.check_app`` matches**, so a window fronted by a host
  process must not take the host's name — `ApplicationFrameHost.exe` fronts every
  packaged app, and an operator's rule on the real app would match nothing while a
  rule on the host would block all of them.
* **``hwnd_owns_point`` is the confinement floor** for every pointer gesture, and it
  fails CLOSED: refusing a legitimate click costs one clear refusal, permitting a
  mis-aimed one is an irreversible action in an app the operator never authorized.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from kiro_crew.computer_use import apps_windows, policy
from kiro_crew.computer_use import windows_ffi as ffi
from kiro_crew.computer_use.types import AppRef, ComputerUseError, PolicyConfig


def _info(
    *,
    hwnd: int = 0x10,
    root: int | None = None,
    pid: int = 42,
    title: str = "A Window",
    cls: str = "Cls",
    exe: str = "app.exe",
    bounds: "tuple[float, float, float, float] | None" = (0.0, 0.0, 100.0, 100.0),
) -> ffi.WindowInfo:
    return ffi.WindowInfo(
        hwnd=hwnd,
        root_hwnd=root if root is not None else hwnd,
        pid=pid,
        title=title,
        class_name=cls,
        exe_name=exe,
        bounds=bounds,
    )


class TestListApps:
    def test_one_entry_per_WINDOW_not_per_process(self, monkeypatch) -> None:
        """A pid is not an app identity here.

        One broker fronts many packaged apps, so collapsing by pid would let two
        unrelated applications share a single grant.
        """
        monkeypatch.setattr(
            ffi,
            "window_list",
            lambda: (
                _info(hwnd=1, pid=7, title="Doc A", exe="editor.exe"),
                _info(hwnd=2, pid=7, title="Doc B", exe="editor.exe"),
            ),
        )
        apps = apps_windows.list_apps()
        assert [a.window_id for a in apps] == [1, 2]
        assert len({a.window_id for a in apps}) == 2

    def test_kirocrews_own_window_is_refused_at_the_ENFORCEMENT_layer(self, monkeypatch) -> None:
        """The one built-in target refusal: the agent must not drive its own UI.

        Asserted through ``policy.check_app`` rather than through the shape of
        ``list_apps``, because that is where the floor actually is — the dispatch
        chokepoint calls it for every verb, and AGENTS.md requires this class of
        refusal to run in band on that path rather than at a fail-open filter.
        Whether the entry is also HIDDEN from the listing is cosmetic by comparison:
        a hidden-but-permitted window would be the dangerous shape, and a
        visible-but-refused one costs the model one clear refusal.
        """
        monkeypatch.setattr(
            ffi,
            "window_list",
            lambda: (
                _info(hwnd=1, title="Kiro Crew", exe="KiroCrew Nightly.exe"),
                _info(hwnd=2, title="Notepad", exe="notepad.exe"),
            ),
        )
        by_title = {a.window_title: a for a in apps_windows.list_apps()}
        assert policy.check_app(by_title["Kiro Crew"], PolicyConfig()) is not None
        assert policy.check_app(by_title["Notepad"], PolicyConfig()) is None

    def test_the_on_screen_filter_is_UPSTREAM_and_not_re_derived_here(self, monkeypatch) -> None:
        """``list_apps`` inherits the on-screen guarantee; it must not second-guess it.

        The renderer and the tool descriptions both call these "on-screen windows", and
        ``IsWindowVisible`` does not mean that — a minimized or DWM-cloaked window
        passes it. Measured on one desktop: 4 of 12 enumerated windows were invisible to
        the operator, and one captured a full bitmap of a window nobody could see. The
        filter lives in ``window_list`` because it needs the native reads
        (``IsIconic`` + ``DWMWA_CLOAKED``), the Windows equivalent of the macOS list's
        ``kCGWindowListOptionOnScreenOnly``.

        What this pins is that the two layers cannot DISAGREE: whatever
        ``window_list`` yields is listed, so a window can never be listable here while
        being off-screen there (or the reverse — silently dropped, which would look like
        an empty desktop).
        """
        yielded = (_info(hwnd=1, title="On Screen", exe="a.exe"),)
        monkeypatch.setattr(ffi, "window_list", lambda: yielded)
        assert [a.window_id for a in apps_windows.list_apps()] == [1]
        # And nothing is added back: an empty upstream list means an empty listing,
        # never a fallback enumeration with a looser filter.
        monkeypatch.setattr(ffi, "window_list", lambda: ())
        assert list(apps_windows.list_apps()) == []

    def test_an_enumeration_failure_PROPAGATES(self, monkeypatch) -> None:
        """Returning () would report "no applications on screen" for a read that
        failed — indistinguishable from an empty desktop, and it would send the model
        back to the very call that just lied to it."""

        def boom():
            raise ffi.ComputerUseUnsupported("enumerating windows failed")

        monkeypatch.setattr(ffi, "window_list", boom)
        with pytest.raises(ComputerUseError):
            apps_windows.list_apps()


class TestResolveApp:
    @staticmethod
    def _apps(monkeypatch) -> None:
        monkeypatch.setattr(
            ffi,
            "window_list",
            lambda: (
                _info(hwnd=1, title="Untitled - Notepad", exe="notepad.exe"),
                _info(hwnd=2, title="Some Page - Google Chrome", exe="chrome.exe"),
                _info(hwnd=3, title="Settings", exe="ApplicationFrameHost.exe"),
            ),
        )

    @pytest.mark.parametrize(
        ("query", "expect_hwnd"),
        [
            ("notepad", 1),  # exe stem
            ("notepad.exe", 1),  # full image name
            ("NOTEPAD", 1),  # case-insensitive
            ("chrome", 2),
            ("Settings", 3),  # a hosted window, by its title identity
            ("Google Chrome", 2),  # window-title substring, the last resort
        ],
    )
    def test_it_resolves_the_expected_window(
        self, monkeypatch, query: str, expect_hwnd: int
    ) -> None:
        self._apps(monkeypatch)
        assert apps_windows.resolve_app(query).window_id == expect_hwnd

    @pytest.mark.parametrize("query", ["", "   ", "definitely-not-running"])
    def test_no_match_refuses_rather_than_guessing(self, monkeypatch, query: str) -> None:
        """Picking the closest window would act on an app the caller did not name."""
        self._apps(monkeypatch)
        with pytest.raises(ComputerUseError):
            apps_windows.resolve_app(query)


class TestHostedWindowIdentity:
    """A host process's name identifies no application."""

    def test_both_policy_matched_fields_carry_the_title(self) -> None:
        ref = apps_windows._app_ref(_info(exe="ApplicationFrameHost.exe", title="Settings"))
        assert (ref.name, ref.bundle_id) == ("Settings", "Settings")

    def test_a_deny_rule_on_the_real_app_now_matches(self) -> None:
        ref = apps_windows._app_ref(_info(exe="ApplicationFrameHost.exe", title="Settings"))
        assert policy.check_app(ref, PolicyConfig(extra_denied_apps=["Settings"])) is not None

    def test_an_ordinary_app_keeps_its_exe_name(self) -> None:
        """Stable across documents, where a title is not."""
        ref = apps_windows._app_ref(_info(exe="chrome.exe", title="Page - Google Chrome"))
        assert (ref.name, ref.bundle_id) == ("chrome", "chrome.exe")

    def test_an_unreadable_exe_falls_back_to_the_title(self) -> None:
        """A process the token cannot open still needs SOME identity."""
        ref = apps_windows._app_ref(_info(exe="", title="Mystery"))
        assert ref.name == "Mystery"


class TestWindowBounds:
    def test_a_live_window_reports_its_rect(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "window_is_live", lambda h: True)
        monkeypatch.setattr(ffi, "window_bounds", lambda h: (1.0, 2.0, 3.0, 4.0))
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        app = AppRef(name="a", pid=1, bundle_id="a.exe", window_id=9, window_title="t")
        assert apps_windows.window_bounds(app) == (1.0, 2.0, 3.0, 4.0)

    def test_a_dead_window_is_None_not_an_exception(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "window_is_live", lambda h: False)
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        app = AppRef(name="a", pid=1, bundle_id="a.exe", window_id=9, window_title="t")
        assert apps_windows.window_bounds(app) is None

    def test_a_raising_read_degrades_to_None(self, monkeypatch) -> None:
        """The bounds feed the tree's frame origin; a failure there must not fail the
        whole observation."""

        def boom(h):
            raise OSError("gone")

        monkeypatch.setattr(ffi, "window_is_live", lambda h: True)
        monkeypatch.setattr(ffi, "window_bounds", boom)
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        app = AppRef(name="a", pid=1, bundle_id="a.exe", window_id=9, window_title="t")
        assert apps_windows.window_bounds(app) is None


class TestHwndOwnsPointFailsClosed:
    """THE confinement floor for every pointer gesture."""

    @staticmethod
    def _app(window_id: int = 0x500) -> AppRef:
        return AppRef(
            name="target", pid=42, bundle_id="target.exe", window_id=window_id, window_title="T"
        )

    def test_the_authorized_window_owns_its_own_pixel(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        monkeypatch.setattr(ffi, "window_is_live", lambda h: True)
        monkeypatch.setattr(ffi, "root_window_at_point", lambda x, y: 0x500)
        assert apps_windows.hwnd_owns_point(self._app(), 10.0, 20.0) is True

    def test_a_DIFFERENT_root_is_refused(self, monkeypatch) -> None:
        """The app-A-authorized / app-B-clicked hole this function exists to close."""
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        monkeypatch.setattr(ffi, "window_is_live", lambda h: True)
        monkeypatch.setattr(ffi, "root_window_at_point", lambda x, y: 0x999)
        assert apps_windows.hwnd_owns_point(self._app(), 10.0, 20.0) is False

    def test_a_pixel_owned_by_NOBODY_is_refused(self, monkeypatch) -> None:
        """A zero handle cannot prove the point belongs to the authorized window."""
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        monkeypatch.setattr(ffi, "window_is_live", lambda h: True)
        monkeypatch.setattr(ffi, "root_window_at_point", lambda x, y: 0)
        assert apps_windows.hwnd_owns_point(self._app(), 10.0, 20.0) is False

    def test_a_dead_window_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        monkeypatch.setattr(ffi, "window_is_live", lambda h: False)
        monkeypatch.setattr(ffi, "root_window_at_point", lambda x, y: 0x500)
        assert apps_windows.hwnd_owns_point(self._app(), 10.0, 20.0) is False

    def test_a_zero_window_id_is_refused_without_asking(self, monkeypatch) -> None:
        """An AppRef with no window cannot own any pixel."""

        def boom(x, y):  # pragma: no cover - must not be reached
            raise AssertionError("asked the OS about a window that does not exist")

        monkeypatch.setattr(ffi, "root_window_at_point", boom)
        assert apps_windows.hwnd_owns_point(self._app(window_id=0), 1.0, 1.0) is False

    def test_a_RAISING_lookup_is_refused(self, monkeypatch) -> None:
        """Fails closed on any error: an exception is not proof of ownership."""

        def boom(x, y):
            raise OSError("the window server said no")

        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        monkeypatch.setattr(ffi, "window_is_live", lambda h: True)
        monkeypatch.setattr(ffi, "root_window_at_point", boom)
        assert apps_windows.hwnd_owns_point(self._app(), 10.0, 20.0) is False


@contextmanager
def _null_scope():
    """A no-op stand-in for ``dpi_awareness_scope``: these tests fake user32."""
    yield
