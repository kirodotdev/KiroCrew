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


class TestTransientPopupsAreNotApplications:
    """A WinUI popup is its app's own tooltip, not an addressable surface.

    Found by driving the real desktop: MS Paint publishes a 97x52 ``PopupHost``
    (class ``Microsoft.UI.Content.PopupWindowSiteBridge``) alongside its real
    1536x960 document window. It carries Paint's image name and passes every other
    filter, so it became a second ``list_apps`` entry indistinguishable by name — and
    ``resolve_app`` returns the FIRST match.
    """

    @staticmethod
    def _paint_pair(monkeypatch, popup_first: bool):
        popup = _info(
            hwnd=0x33807C8,
            pid=900,
            title="PopupHost",
            cls="Microsoft.UI.Content.PopupWindowSiteBridge",
            exe="mspaint.exe",
            bounds=(478.0, 161.0, 97.0, 52.0),
        )
        document = _info(
            hwnd=0x3950136,
            pid=900,
            title="Untitled - Paint",
            cls="MSPaintApp",
            exe="mspaint.exe",
            bounds=(191.0, 119.0, 1536.0, 960.0),
        )
        order = (popup, document) if popup_first else (document, popup)
        monkeypatch.setattr(ffi, "window_list", lambda: order)

    def test_the_popup_is_not_listed_as_an_application(self, monkeypatch) -> None:
        self._paint_pair(monkeypatch, popup_first=True)
        apps = apps_windows.list_apps()
        assert [app.window_title for app in apps] == ["Untitled - Paint"]

    def test_resolve_app_reaches_the_DOCUMENT_window_whatever_the_z_order(
        self, monkeypatch
    ) -> None:
        """The consequence, and the reason this is a defect rather than a tidy-up.

        With the popup listed, a coordinate gesture aimed at the document was refused
        by ``hwnd_owns_point`` — CORRECTLY, since the point really was outside the
        97x52 rect it had resolved to — and the refusal told the model to re-read
        bounds that would never change. That is a retry loop with no exit.

        Both orderings are asserted because ``window_list`` returns front-to-back
        z-order, so which one comes first depends on whether a tooltip happens to be
        showing.
        """
        for popup_first in (True, False):
            self._paint_pair(monkeypatch, popup_first=popup_first)
            resolved = apps_windows.resolve_app("mspaint")
            assert resolved.window_id == 0x3950136, f"popup_first={popup_first}"
            assert resolved.window_title == "Untitled - Paint"

    def test_a_real_window_whose_class_merely_CONTAINS_popup_is_kept(self, monkeypatch) -> None:
        # The exclusion is a PREFIX test, so an ordinary application that happens to
        # have "Popup" somewhere in its class name is not silently unreachable.
        monkeypatch.setattr(
            ffi,
            "window_list",
            lambda: (_info(hwnd=5, title="Real", cls="MyAppPopupEditor", exe="app.exe"),),
        )
        assert [app.window_title for app in apps_windows.list_apps()] == ["Real"]

    def test_a_DENIED_popup_is_still_listed_so_it_can_be_refused(self, monkeypatch) -> None:
        """The popup filter must not drop a window the denylist wants to refuse.

        Dropping a window before it claims its handle slot lets an innocuous
        same-handle sibling occupy it — the exact overwrite ``list_apps``' guard forbids
        ("a denied window is never overwritten by an innocuous sibling, so the refusal
        cannot be dodged"). A window that is not listed cannot be refused.
        """
        denied = _info(
            hwnd=7,
            pid=901,
            title="Kiro Crew",
            cls="Microsoft.UI.Content.PopupWindowSiteBridge",
            exe="chrome.exe",
        )
        sibling = _info(
            hwnd=7, pid=901, title="Innocuous", cls="Chrome_WidgetWin_1", exe="chrome.exe"
        )
        monkeypatch.setattr(ffi, "window_list", lambda: (denied, sibling))
        apps = apps_windows.list_apps()
        assert [app.window_title for app in apps] == ["Kiro Crew"]
        assert policy.check_app(apps[0], PolicyConfig()) is not None


class TestFindWindowFor:
    """The launch verb's non-raising lookup, on both of its questions."""

    def test_it_sees_a_HOSTED_window_by_its_title(self, monkeypatch) -> None:
        """A packaged app's identity IS its title, so a stem-only lookup misses it.

        ``_app_ref`` deliberately replaces both ``name`` and ``bundle_id`` with the
        window title for a window fronted by ``ApplicationFrameHost``, because the
        host's image name identifies no application. The launch catalog resolves an
        executable STEM, so for a packaged app the two never agree — and both of the
        launch verb's questions then answer wrongly: the already-running pre-check does
        not fire (so the model opens a SECOND copy) and the post-launch poll burns its
        whole timeout reporting "no window appeared" for a window that is on screen.
        """
        monkeypatch.setattr(
            ffi,
            "window_list",
            lambda: (
                _info(
                    hwnd=11,
                    pid=902,
                    title="Microsoft Store",
                    cls="ApplicationFrameWindow",
                    exe="ApplicationFrameHost.exe",
                ),
            ),
        )
        hosted = apps_windows.list_apps()[0]
        # The frame-host substitution: neither policy-matched field carries the stem.
        assert hosted.name == hosted.bundle_id == "Microsoft Store"
        assert apps_windows.find_window_for("Microsoft Store") is not None
        # And the stem the catalog resolves ('store') reaches it through the title.
        assert apps_windows.find_window_for("store") is not None

    def test_it_prefers_an_EXACT_stem_over_a_title_substring(self, monkeypatch) -> None:
        # The title tier is a substring, so it runs only after the exact forms — the
        # ordering ``resolve_app`` uses, for the reason it gives: otherwise asking for
        # ``chrome`` could resolve to a window whose title merely contains the word.
        monkeypatch.setattr(
            ffi,
            "window_list",
            lambda: (
                _info(hwnd=1, pid=1, title="notes about chrome", exe="editor.exe"),
                _info(hwnd=2, pid=2, title="New Tab", exe="chrome.exe"),
            ),
        )
        found = apps_windows.find_window_for("chrome")
        assert found is not None
        assert found.bundle_id == "chrome.exe"

    def test_an_absent_app_is_None_not_an_exception(self, monkeypatch) -> None:
        # Not-running is the case a launch exists to FIX, so it must be an ordinary
        # negative rather than the error path.
        monkeypatch.setattr(ffi, "window_list", lambda: ())
        assert apps_windows.find_window_for("anything") is None

    def test_an_enumeration_failure_is_None_not_an_exception(self, monkeypatch) -> None:
        # During a launch poll a transient failure is indistinguishable from "not there
        # yet", and the poll's own timeout is the bound.
        def boom():
            raise ComputerUseError("enumeration failed")

        monkeypatch.setattr(ffi, "window_list", boom)
        assert apps_windows.find_window_for("anything") is None


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
