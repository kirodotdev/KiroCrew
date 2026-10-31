"""The Windows INPUT path: the pattern ladder, confinement, focus, and the floors.

Runs on EVERY platform. The native surface is reached only through module-level
seams these tests monkeypatch, so a Linux or macOS shard exercises the same
decision logic that runs on Windows — which matters because the branches with the
worst consequences cannot be produced on demand against a real desktop (a focus
that fails, a field that starts masking mid-action, a coordinate that belongs to
another application).

The properties pinned here are the ones whose violation is irreversible:

* **The ladder never falls through a FAILURE.** A rung is tried when the previous
  pattern is ABSENT, never when it was present and rejected the action — otherwise
  a refused Invoke silently becomes a Toggle, which is a different gesture.
* **``auto`` never reaches the pointer.** On Windows the only coordinate route
  moves the operator's real cursor, so ``auto`` + coordinates must REFUSE rather
  than resolve; and ``app_post``/``sky_click`` must refuse BY NAME rather than
  downgrade onto that same route.
* **Every pointer gesture is confined**, both endpoints for a drag, and the check
  compares top-level HANDLES rather than pids.
* **A failed focus refuses the keystrokes.** Typing past a failed aim delivers to
  whatever the application already had focused, which can be the password field
  the policy check just refused.
* **The secure-field floor is re-read live**, so a field that starts masking after
  the model saw it still refuses the write.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from kiro_crew.computer_use import apps_windows, keymap, policy, windows_driver
from kiro_crew.computer_use import windows_ffi
from kiro_crew.computer_use import windows_ffi as ffi
from kiro_crew.computer_use.types import (
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    AppRef,
    ClickRequest,
    DragRequest,
    ElementRec,
    PolicyConfig,
)
from kiro_crew.computer_use.windows_driver import WindowsHelper

_APP = AppRef(
    name="target", pid=99, bundle_id="target.exe", window_id=0x2222, window_title="Target"
)
_REC = ElementRec(index=3, role="Button", title="Save")
_E_FAIL = -2147467259


@pytest.fixture
def driver() -> WindowsHelper:
    return WindowsHelper()


class TestAutoNeverReachesThePointer:
    """The one invariant standing between an ordinary click and the real cursor."""

    def test_auto_with_coordinates_refuses(self, driver: WindowsHelper) -> None:
        """On Windows the ONLY coordinate route warps the operator's cursor.

        ``policy.resolve_click_method`` resolves ``auto`` + coordinates onto
        ``app_post`` — a macOS method — so a driver that treated an unresolved
        ``auto`` as "pick whatever works" would pick the pointer. Refusing keeps
        "``auto`` never resolves onto a pointer-moving method" true on both
        platforms.
        """
        result = driver.click(_APP, ClickRequest(method=CLICK_METHOD_AUTO, point=(10.0, 20.0)))
        assert result.ok is False
        assert "global" in result.text, "the refusal must name the explicit opt-in"

    @pytest.mark.parametrize(
        "method", [CLICK_METHOD_APP_POST, CLICK_METHOD_SKY_CLICK], ids=["app_post", "sky_click"]
    )
    def test_a_macos_method_refuses_by_name_rather_than_downgrading(
        self, driver: WindowsHelper, method: str
    ) -> None:
        """Substituting the pointer path performs a different gesture than requested.

        ``app_post`` is the dangerous one: it is what ``auto`` resolves onto for a
        macOS coordinate click, so silently mapping it onto the only Windows
        coordinate route would hand over the cursor for a call that never asked.
        """
        result = driver.click(_APP, ClickRequest(method=method, point=(10.0, 20.0)))
        assert result.ok is False
        assert "macOS-only" in result.text
        assert "element_index" in result.text


class TestPointerConfinement:
    """A global event lands on whatever owns the pixel, so the pixel is checked."""

    @staticmethod
    def _owns(monkeypatch, verdict: bool) -> list:
        seen: list = []

        def owns(app, x, y):
            seen.append((x, y))
            return verdict

        monkeypatch.setattr(windows_driver.apps_windows, "hwnd_owns_point", owns)
        monkeypatch.setattr(ffi, "post_mouse_click", lambda *a, **k: None)
        monkeypatch.setattr(ffi, "post_mouse_drag", lambda *a, **k: None)
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        return seen

    def test_a_click_outside_the_window_is_refused(
        self, driver: WindowsHelper, monkeypatch
    ) -> None:
        """Naming an allowed app while passing coordinates over a denied one would
        otherwise pass ``policy.check_app`` on app A and click app B."""
        self._owns(monkeypatch, False)
        result = driver.click(_APP, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(5.0, 6.0)))
        assert result.ok is False
        assert "not owned by" in result.text
        # The refusal must name a move that can SUCCEED. "Re-read the window bounds"
        # could not terminate: the check compares the window that OWNS the pixel, so a
        # point inside the rect but under another window refuses while
        # ``computer_get_state`` keeps returning the same bounds — an identical retry,
        # forever. An element-addressed action needs no coordinate at all.
        assert "element_index" in result.text
        assert "Re-read the window bounds" not in result.text

    def test_a_click_inside_the_window_proceeds(self, driver: WindowsHelper, monkeypatch) -> None:
        seen = self._owns(monkeypatch, True)
        result = driver.click(_APP, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(50.0, 60.0)))
        assert result.ok is True
        assert seen == [(50.0, 60.0)]
        assert "real cursor moved" in result.text, "the operator's cursor moved; say so"

    def test_a_drag_REFUSES_the_default_method(self, driver: WindowsHelper, monkeypatch) -> None:
        """**The pointer-audit bypass this test exists for.**

        ``DragRequest.method`` defaults to ``app_post`` — the macOS app-scoped route,
        for which ``moves_pointer`` is False. So the upstream SEL audit and the
        cursor-motion overlay both SKIP a call carrying that default, while the only
        drag route Windows has moves the operator's real cursor. Serving it would warp
        their pointer on a gesture nothing recorded as pointer-moving.
        """
        posted: list = []
        monkeypatch.setattr(ffi, "post_mouse_drag", lambda *a, **k: posted.append(1))
        monkeypatch.setattr(windows_driver.apps_windows, "hwnd_owns_point", lambda app, x, y: True)
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        result = driver.drag(_APP, DragRequest(start=(1.0, 2.0), end=(3.0, 4.0)))
        assert result.ok is False
        assert posted == [], "a drag with the default method moved the real cursor"
        assert "global" in result.text, "the refusal must name the explicit opt-in"
        assert (
            DragRequest(start=(0.0, 0.0), end=(1.0, 1.0)).moves_pointer is False
        ), "the default's moves_pointer is what makes the audit skip it"

    def test_a_drag_confines_BOTH_endpoints(self, driver: WindowsHelper, monkeypatch) -> None:
        """The RELEASE is where a drop lands.

        A sweep that starts inside the authorized window and ends over a denied app
        would otherwise release the button there.
        """
        seen = self._owns(monkeypatch, True)
        driver.drag(
            _APP,
            DragRequest(start=(1.0, 2.0), end=(3.0, 4.0), method=CLICK_METHOD_GLOBAL),
        )
        assert seen == [(1.0, 2.0), (3.0, 4.0)]

    def test_a_drag_with_a_denied_endpoint_sends_NOTHING(
        self, driver: WindowsHelper, monkeypatch
    ) -> None:
        """Refused BEFORE the press, not midway: a press already happened is a
        stuck button and any later motion becomes an unintended drag."""
        posted: list = []
        monkeypatch.setattr(
            windows_driver.apps_windows,
            "hwnd_owns_point",
            lambda app, x, y: (x, y) == (1.0, 2.0),
        )
        monkeypatch.setattr(ffi, "post_mouse_drag", lambda *a, **k: posted.append(1))
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        result = driver.drag(
            _APP,
            DragRequest(start=(1.0, 2.0), end=(9.0, 9.0), method=CLICK_METHOD_GLOBAL),
        )
        assert result.ok is False
        assert posted == [], "a partly-confined drag still moved the mouse"


class TestAbsoluteCoordinatesArePixelExact:
    """``SendInput`` normalizes to 0..65535 and the OS FLOORS on the way back."""

    def test_the_denominator_is_the_span_not_the_extent(self, monkeypatch) -> None:
        """**A 1px error is a different window at the edge.**

        Measured by moving the real cursor and reading ``GetCursorPos`` over 13 x
        values on a 1920-wide desktop: ``round(x*65535/width)`` was wrong 10 times,
        ``round(x*65535/(width-1))`` 3 times, ``ceil(x*65535/(width-1))`` zero. The
        confinement check validated the point we were ASKED for, not the one we send,
        so an off-by-one lands input in whatever sits behind the authorized window.
        """
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 1920, 1200))
        # The last pixel must map to the TOP of the range, not one step short.
        assert ffi._normalized(1919, 1199) == (65535, 65535)
        assert ffi._normalized(0, 0) == (0, 0)
        # And a small x must not floor back to x-1: ceil keeps it above the boundary.
        nx, _ny = ffi._normalized(1, 0)
        assert nx * 1919 / 65535 >= 1.0, "the OS floor would land this on pixel 0"

    def test_a_negative_origin_is_honoured(self, monkeypatch) -> None:
        """A monitor left of or above the primary gives the desktop a negative origin.

        Normalizing against an assumed (0, 0) would put every click on the wrong
        monitor.
        """
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (-1920, -100, 3840, 1200))
        assert ffi._normalized(-1920, -100) == (0, 0)
        assert ffi._normalized(1919, 1099) == (65535, 65535)

    def test_a_single_pixel_extent_does_not_divide_by_zero(self, monkeypatch) -> None:
        """The degenerate screen has exactly one coordinate."""
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 1, 1))
        assert ffi._normalized(0, 0) == (0, 0)

    def test_a_zero_extent_refuses(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "_virtual_screen", lambda: (0, 0, 0, 0))
        with pytest.raises(ffi.ComputerUseUnsupported):
            ffi._normalized(0, 0)


class TestTheVkTableCoversEveryCanonicalKey:
    def test_every_canonical_keymap_name_has_a_vk_code(self) -> None:
        """A key ``keymap`` accepts but this table lacks would be refused at the
        driver AFTER the chokepoint said yes — a dead end the model cannot act on."""
        from kiro_crew.computer_use import keymap

        missing = sorted(set(keymap.KEY_ALIASES.values()) - set(ffi.VK_CODES) - {"spacebar"})
        assert not missing, f"canonical keys with no VK code: {missing}"

    def test_delete_and_backspace_are_DIFFERENT_vk_codes(self) -> None:
        """macOS shares one keycode between them; Windows does not.

        This is the pair whose collapse would send VK_BACK for
        ``press_key("delete")`` — destroying the character BEFORE the caret instead
        of after it.
        """
        assert ffi.VK_CODES["delete"] != ffi.VK_CODES["backspace"]
        assert ffi.VK_CODES["delete"] == 0x2E
        assert ffi.VK_CODES["backspace"] == 0x08

    def test_the_arrow_and_editing_keys_are_EXTENDED(self) -> None:
        """Without the extended flag the injected scan code is the NUMPAD twin, so
        an arrow key only moves the caret while NumLock happens to be off."""
        for key in ("arrowleft", "arrowright", "home", "end", "delete", "insert"):
            assert key in ffi._EXTENDED_KEYS, key

    def test_fn_has_no_vk_code_and_is_not_faked(self) -> None:
        """``fn`` is handled in keyboard firmware below the OS.

        Mapping it to something plausible would send a chord the caller did not ask
        for; absent means ``send_key_chord`` refuses it loudly instead.
        """
        assert "fn" not in ffi.VK_MODIFIERS
        assert "fn" in keymap.MODIFIER_ALIASES.values()

    def test_CAPSLOCK_is_not_a_holdable_modifier(self) -> None:
        """**A residual-state defect, and the only modifier that cannot be undone.**

        macOS models ``capslock`` as ``FLAG_ALPHA_SHIFT`` — a per-event flag that
        applies to the one synthesized event and changes no machine state. Windows has
        no such flag: ``VK_CAPITAL`` down/up TOGGLES a machine-wide lock, and the flip
        OUTLIVES the call. So ``capslock+a`` would capitalize everything the OPERATOR
        types next, and releasing the key does not undo it the way every other
        modifier's release does.

        It stays in the platform-free vocabulary (macOS serves it), so this is a
        per-platform refusal rather than a vocabulary change.
        """
        assert "capslock" not in ffi.VK_MODIFIERS
        assert "capslock" in keymap.MODIFIERS, "still valid in the shared vocabulary"

    @pytest.mark.parametrize(
        ("modifier", "must_mention"),
        [("capslock", "shift+"), ("fn", "firmware")],
    )
    def test_a_deliberately_absent_modifier_says_WHY(
        self, modifier: str, must_mention: str
    ) -> None:
        """A bare "no virtual-key code" reads as a gap to work around.

        These are decisions, so the refusal names the reason and the alternative that
        works — a refusal a model cannot act on costs a turn and teaches it nothing.
        """
        with pytest.raises(ffi.ComputerUseUnsupported) as excinfo:
            ffi.send_key_chord("a", (modifier,))
        assert must_mention in str(excinfo.value)


class TestABrokerHostedWindowGetsAPolicyMatchableIdentity:
    """A host process's name identifies no application, so it must not be the one
    ``policy.check_app`` matches against.

    Measured on a real desktop: *Settings* and *HP Audio Control* both report
    ``ApplicationFrameHost.exe``. Taking that name gives an operator two bad
    options and no good one — a deny rule on the real app matches NOTHING, and the
    only rule that does match blocks every packaged app at once. The permissive half
    is silent, which is what makes it a security defect rather than an annoyance.
    """

    @staticmethod
    def _info(exe: str, title: str):
        return windows_ffi.WindowInfo(
            hwnd=0x10,
            root_hwnd=0x10,
            pid=42,
            title=title,
            class_name="Cls",
            exe_name=exe,
            bounds=(0.0, 0.0, 100.0, 100.0),
        )

    def test_the_hosted_windows_own_title_becomes_its_identity(self) -> None:
        """**The defect this test exists for.**

        Both policy-matched fields carry the title, because ``check_app`` matches
        ``bundle_id`` AND ``name`` — leaving the host's image name in either one
        keeps the bypass open.
        """
        ref = apps_windows._app_ref(self._info("ApplicationFrameHost.exe", "Settings"))
        assert ref.name == "Settings"
        assert ref.bundle_id == "Settings"
        assert "ApplicationFrameHost" not in f"{ref.name}{ref.bundle_id}"

    def test_an_operator_deny_rule_now_matches_the_real_app(self) -> None:
        """The whole point: the name the operator SEES is the name that blocks."""
        ref = apps_windows._app_ref(self._info("ApplicationFrameHost.exe", "Settings"))
        cfg = PolicyConfig(extra_denied_apps=["Settings"])
        assert policy.check_app(ref, cfg) is not None

    def test_the_rule_stays_scoped_to_ONE_app(self) -> None:
        """Two packaged apps share the host, so a rule for one must not catch both.

        This is the other half of the bypass: before the fix, the only pattern that
        matched anything was the host name, and it blocked every packaged app.
        """
        settings = apps_windows._app_ref(self._info("ApplicationFrameHost.exe", "Settings"))
        audio = apps_windows._app_ref(self._info("ApplicationFrameHost.exe", "HP Audio Control"))
        cfg = PolicyConfig(extra_denied_apps=["Settings"])
        assert policy.check_app(settings, cfg) is not None
        assert policy.check_app(audio, cfg) is None

    def test_an_ordinary_app_keeps_its_executable_name(self) -> None:
        """The exe name is stable across documents, so it stays the identity where it
        actually identifies something — a title changes with the open file."""
        ref = apps_windows._app_ref(self._info("chrome.exe", "Some Page - Google Chrome"))
        assert ref.name == "chrome"
        assert ref.bundle_id == "chrome.exe"

    def test_a_hosted_window_with_no_title_falls_back(self) -> None:
        """An empty title is no identity at all, so there is nothing better to use."""
        ref = apps_windows._app_ref(self._info("ApplicationFrameHost.exe", "   "))
        assert ref.bundle_id == "ApplicationFrameHost.exe"


@contextmanager
def _null_scope():
    """A no-op stand-in for ``dpi_awareness_scope``: these tests fake user32."""
    yield
