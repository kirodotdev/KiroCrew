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

from kiro_crew.computer_use import apps_windows, keymap, policy, snapshot_windows, windows_driver
from kiro_crew.computer_use import windows_ffi
from kiro_crew.computer_use import windows_ffi as ffi
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_AUTO,
    CLICK_METHOD_GLOBAL,
    CLICK_METHOD_SKY_CLICK,
    AppRef,
    ClickRequest,
    DragRequest,
    ElementRec,
    PolicyConfig,
    SnapshotRequest,
)
from kiro_crew.computer_use.windows_driver import WindowsBackend

_APP = AppRef(
    name="target", pid=99, bundle_id="target.exe", window_id=0x2222, window_title="Target"
)
_REC = ElementRec(index=3, role="Button", title="Save")
_E_FAIL = -2147467259


@pytest.fixture
def driver() -> WindowsBackend:
    return WindowsBackend()


@pytest.fixture
def element(monkeypatch):
    """Make ``addressed`` yield a sentinel element, and record ``for_write``.

    Patched at the driver's own reference so the walk never runs: the ladder and
    focus rules are what these tests are about, and resolving a real index would
    need a real window.
    """
    calls: dict = {"for_write": None}
    sentinel = object()

    @contextmanager
    def fake(app, rec, req, *, for_write: bool = False):
        calls["for_write"] = for_write
        yield sentinel

    monkeypatch.setattr(snapshot_windows, "addressed", fake)
    calls["sentinel"] = sentinel
    return calls


class TestThePatternLadder:
    """Specificity order, and the rule that a FAILURE is not a fall-through."""

    @staticmethod
    def _rungs(monkeypatch, **outcomes):
        """Install each rung's return value and record the call order.

        ``None`` means "the pattern is absent" — the only condition that may
        advance the ladder.
        """
        order: list[str] = []

        def make(name: str):
            def rung(element):
                order.append(name)
                return outcomes.get(name)

            return rung

        for name in ("invoke_element", "toggle_element", "select_element", "do_default_action"):
            monkeypatch.setattr(ffi, name, make(name))
        return order

    def test_invoke_is_tried_first_and_stops_the_ladder(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        order = self._rungs(monkeypatch, invoke_element=ffi.S_OK)
        result = driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is True
        assert order == ["invoke_element"]

    def test_an_absent_pattern_advances_to_the_next_rung(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """``None`` is "this element does not implement that pattern"."""
        order = self._rungs(monkeypatch, select_element=ffi.S_OK)
        assert driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY)).ok
        assert order == ["invoke_element", "toggle_element", "select_element"]

    def test_a_FAILED_rung_stops_the_ladder_rather_than_falling_through(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """**The defect this test exists for.**

        A present-but-failed Invoke means the right action was attempted and
        rejected. Advancing to Toggle would perform a DIFFERENT gesture than the one
        requested — and on a checkbox that is a state flip nobody asked for.
        """
        order = self._rungs(monkeypatch, invoke_element=_E_FAIL, toggle_element=ffi.S_OK)
        result = driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False
        assert order == ["invoke_element"], "a failure fell through to the next rung"
        assert "0x80004005" in result.text

    def test_the_legacy_default_action_is_last(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """It performs whatever the provider nominated, so it is the least specific.

        It also sits on nearly every node of a real Win32 tree, so trying it earlier
        would mask the specific patterns above it.
        """
        order = self._rungs(monkeypatch, do_default_action=ffi.S_OK)
        assert driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY)).ok
        assert order[-1] == "do_default_action"

    def test_no_pattern_at_all_names_what_was_tried(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """A model that knows which rungs failed can pick a different element."""
        self._rungs(monkeypatch)
        result = driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False
        for rung in ("invoke", "toggle", "select"):
            assert rung in result.text

    @pytest.mark.parametrize(
        ("rung", "named"),
        [
            ("invoke_element", "Invoke"),
            ("toggle_element", "Toggle"),
            ("select_element", "SelectionItem.Select"),
            ("do_default_action", "LegacyIAccessible.DoDefaultAction"),
        ],
    )
    def test_the_WINNING_rung_is_named_in_the_result(
        self, driver: WindowsBackend, element, monkeypatch, rung: str, named: str
    ) -> None:
        """Parity with the macOS driver, which names the AX action that succeeded.

        "pressed element 3" hides what the call actually did: a press that landed on
        ``Toggle`` flipped a checkbox, and a model told only "pressed" has to re-read
        the tree to discover that. Naming it also teaches which pattern this element
        answers, so the next turn does not re-derive it.
        """
        self._rungs(monkeypatch, **{rung: ffi.S_OK})
        result = driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is True
        assert named in result.text

    def test_a_press_does_NOT_ask_for_the_write_check(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """A press neither reads nor writes a value.

        Refusing it on a masked field would block ordinary interaction with a login
        form — clicking its Sign In button, or revealing the password.
        """
        self._rungs(monkeypatch, invoke_element=ffi.S_OK)
        driver.click(_APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert element["for_write"] is False


class TestAutoNeverReachesThePointer:
    """The one invariant standing between an ordinary click and the real cursor."""

    def test_auto_with_coordinates_refuses(self, driver: WindowsBackend) -> None:
        """On Windows the ONLY coordinate route warps the operator's cursor.

        ``policy.resolve_click_method`` resolves ``auto`` + coordinates onto
        ``app_post`` — a macOS method — so a driver that treated an unresolved
        ``auto`` as "pick whatever works" would pick the pointer. Refusing keeps
        "``auto`` never resolves onto a pointer-moving method" true on both
        platforms.
        """
        result = driver.click(
            _APP, None, ClickRequest(method=CLICK_METHOD_AUTO, point=(10.0, 20.0))
        )
        assert result.ok is False
        assert "global" in result.text, "the refusal must name the explicit opt-in"

    @pytest.mark.parametrize(
        "method", [CLICK_METHOD_APP_POST, CLICK_METHOD_SKY_CLICK], ids=["app_post", "sky_click"]
    )
    def test_a_macos_method_refuses_by_name_rather_than_downgrading(
        self, driver: WindowsBackend, method: str
    ) -> None:
        """Substituting the pointer path performs a different gesture than requested.

        ``app_post`` is the dangerous one: it is what ``auto`` resolves onto for a
        macOS coordinate click, so silently mapping it onto the only Windows
        coordinate route would hand over the cursor for a call that never asked.
        """
        result = driver.click(_APP, None, ClickRequest(method=method, point=(10.0, 20.0)))
        assert result.ok is False
        assert "macOS-only" in result.text
        assert "element_index" in result.text

    def test_accessibility_without_an_index_refuses(self, driver: WindowsBackend) -> None:
        """There is no coordinate form of a pattern action."""
        result = driver.click(_APP, None, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False


class TestANonLeftButtonIsNotSilentlyALeftClick:
    """A right-click on an element must not become "activate the control".

    UIA exposes no pattern that opens a context menu — there is no ``AXShowMenu``
    equivalent — so the pattern ladder can only activate. Performing that instead
    would do something the model did not ask for, which is the defect macOS's
    right-button ladder (``AXShowMenu`` only, never falling back to a press) exists
    to prevent. The refusal is the parity-preserving answer here.
    """

    @pytest.mark.parametrize("button", ["right", "middle"], ids=["right", "middle"])
    def test_it_refuses_rather_than_activating(
        self, driver: WindowsBackend, element, monkeypatch, button: str
    ) -> None:
        pressed: list[str] = []

        def forbidden(elem):  # pragma: no cover - reaching this IS the failure
            pressed.append("invoked")
            return ffi.S_OK

        monkeypatch.setattr(ffi, "invoke_element", forbidden)
        result = driver.click(
            _APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY, button=button)
        )
        assert result.ok is False
        assert pressed == [], f"a {button}-button click activated the control instead"
        assert "global" in result.text, "the refusal must name the route that CAN serve it"

    def test_the_left_button_still_takes_the_ladder(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """The guard must not cost the ordinary case: ``left`` is the default and the
        only button an element-addressed click ever had."""
        monkeypatch.setattr(ffi, "invoke_element", lambda elem: ffi.S_OK)
        result = driver.click(
            _APP, _REC, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY, button="left")
        )
        assert result.ok is True

    def test_a_right_click_by_COORDINATE_is_still_served(
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        """The button is honoured where a real right-click is what was asked for.

        ``global`` already means "move the operator's cursor", so a right-click there
        surprises nobody — the refusal above is about the route that promises not to.
        """
        sent: list = []
        monkeypatch.setattr(windows_driver.apps_windows, "hwnd_owns_point", lambda app, x, y: True)
        monkeypatch.setattr(
            ffi,
            "post_mouse_click",
            lambda x, y, button, count: sent.append((x, y, button, count)),
        )
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        result = driver.click(
            _APP,
            None,
            ClickRequest(method=CLICK_METHOD_GLOBAL, point=(10.0, 20.0), button="right"),
        )
        assert result.ok is True
        assert sent and sent[0][2] == "right"


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
        self, driver: WindowsBackend, monkeypatch
    ) -> None:
        """Naming an allowed app while passing coordinates over a denied one would
        otherwise pass ``policy.check_app`` on app A and click app B."""
        self._owns(monkeypatch, False)
        result = driver.click(
            _APP, None, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(5.0, 6.0))
        )
        assert result.ok is False
        assert "not owned by" in result.text
        # The refusal must name a move that can SUCCEED. "Re-read the window bounds"
        # could not terminate: the check compares the window that OWNS the pixel, so a
        # point inside the rect but under another window refuses while
        # ``computer_get_state`` keeps returning the same bounds — an identical retry,
        # forever. An element-addressed action needs no coordinate at all.
        assert "element_index" in result.text
        assert "Re-read the window bounds" not in result.text

    def test_a_click_inside_the_window_proceeds(self, driver: WindowsBackend, monkeypatch) -> None:
        seen = self._owns(monkeypatch, True)
        result = driver.click(
            _APP, None, ClickRequest(method=CLICK_METHOD_GLOBAL, point=(50.0, 60.0))
        )
        assert result.ok is True
        assert seen == [(50.0, 60.0)]
        assert "real cursor moved" in result.text, "the operator's cursor moved; say so"

    def test_a_drag_REFUSES_the_default_method(self, driver: WindowsBackend, monkeypatch) -> None:
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

    def test_a_drag_confines_BOTH_endpoints(self, driver: WindowsBackend, monkeypatch) -> None:
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
        self, driver: WindowsBackend, monkeypatch
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


class TestKeyboardTakesFocusAndSaysSo:
    def test_type_text_refuses_when_focus_FAILS(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """**The security case, not a robustness one.**

        ``policy.check_input_target`` cleared the element the model ADDRESSED, so if
        focus did not move, ``SendInput`` delivers to whatever the application
        already had focused — which can be the password field that check just
        refused to write into.
        """
        sent: list = []
        monkeypatch.setattr(ffi, "set_element_focus", lambda e: _E_FAIL)
        monkeypatch.setattr(ffi, "send_text", lambda text: sent.append(text) or len(text))
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        result = driver.type_text(_APP, _REC, "secret")
        assert result.ok is False
        assert sent == [], "keystrokes were sent past a failed focus"

    def test_press_key_refuses_when_focus_FAILS(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        sent: list = []
        monkeypatch.setattr(ffi, "set_element_focus", lambda e: _E_FAIL)
        monkeypatch.setattr(ffi, "send_key_chord", lambda k, m: sent.append(k))
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        assert driver.press_key(_APP, _REC, "return").ok is False
        assert sent == []

    def test_the_result_says_the_focus_moved(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """The operator's caret really did move, and they are the one who finds out."""
        monkeypatch.setattr(ffi, "set_element_focus", lambda e: ffi.S_OK)
        monkeypatch.setattr(ffi, "element_has_focus", lambda e: True)
        monkeypatch.setattr(ffi, "send_text", lambda text: len(text))
        monkeypatch.setattr(ffi, "send_key_chord", lambda k, m: None)
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        assert "focus" in driver.type_text(_APP, _REC, "hi").text.lower()
        assert "focus" in driver.press_key(_APP, _REC, "return").text.lower()

    def test_a_short_accept_is_a_FAILURE_not_a_success(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """A partial accept leaves the field holding half the text.

        Reporting success would leave the model believing it typed something it did
        not, and the next action would build on a value that is not there.
        """
        monkeypatch.setattr(ffi, "set_element_focus", lambda e: ffi.S_OK)
        monkeypatch.setattr(ffi, "element_has_focus", lambda e: True)
        monkeypatch.setattr(ffi, "send_text", lambda text: 2)
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        result = driver.type_text(_APP, _REC, "hello")
        assert result.ok is False
        assert "2 of 5" in result.text

    def test_an_unknown_key_is_refused_before_anything_is_sent(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """Re-parsed at the driver, not trusted from the chokepoint.

        This module is reachable from the CLI harness and from tests, and a
        keystroke is the one thing that must never be synthesized from an
        unvalidated string.
        """
        sent: list = []
        monkeypatch.setattr(ffi, "send_key_chord", lambda k, m: sent.append(k))
        monkeypatch.setattr(ffi, "set_element_focus", lambda e: ffi.S_OK)
        monkeypatch.setattr(ffi, "element_has_focus", lambda e: True)
        result = driver.press_key(_APP, _REC, "hyper+nope")
        assert result.ok is False
        assert sent == []

    def test_the_keyboard_verbs_ask_for_the_write_check(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """Focus is what makes the following keystrokes land on the target, so a
        masked field must refuse the FOCUS rather than the typing."""
        monkeypatch.setattr(ffi, "set_element_focus", lambda e: ffi.S_OK)
        monkeypatch.setattr(ffi, "element_has_focus", lambda e: True)
        monkeypatch.setattr(ffi, "send_text", lambda text: len(text))
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        driver.type_text(_APP, _REC, "hi")
        assert element["for_write"] is True


class TestFocusIsVERIFIEDNotAssumed:
    """``SetFocus`` returning ``S_OK`` means the REQUEST was accepted."""

    def test_an_accepted_request_that_did_not_MOVE_focus_refuses(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """**The defect this test exists for.**

        A container can hand focus to a child and a dialog can pull it back, both
        answering S_OK — measured on a real window whose focus stayed put across an
        S_OK ``SetFocus``. ``SendInput`` follows whatever HOLDS focus, so trusting the
        return code delivers the keystrokes to a control the policy check never
        inspected, which can be the password field it just refused.
        """
        sent: list = []
        monkeypatch.setattr(ffi, "set_element_focus", lambda e: ffi.S_OK)
        monkeypatch.setattr(ffi, "element_has_focus", lambda e: False)
        monkeypatch.setattr(ffi, "send_text", lambda t: sent.append(t) or len(t))
        monkeypatch.setattr(ffi, "dpi_awareness_scope", _null_scope)
        result = driver.type_text(_APP, _REC, "secret")
        assert result.ok is False
        assert sent == [], "keystrokes were sent to an element that lacked focus"
        assert "computer_set_value" in result.text, "name the focus-free alternative"

    def test_it_fails_CLOSED_on_an_unreadable_focus_flag(self, monkeypatch) -> None:
        """Only an explicit supported True counts.

        A failed read, the reserved not-supported sentinel, or a surprising type all
        mean "I cannot prove this element has focus" — which is not "it does".
        """
        for hr, vt, value in (
            (-2147467259, ffi.VT_BOOL, True),  # read failed
            (ffi.S_OK, ffi.VT_UNKNOWN, 0x1234),  # not-supported sentinel
            (ffi.S_OK, ffi.VT_BSTR, "yes"),  # wrong type
            (ffi.S_OK, ffi.VT_BOOL, False),  # honest False
        ):
            monkeypatch.setattr(ffi, "prop_value_ex", lambda e, pid: (hr, vt, value))
            assert ffi.element_has_focus(object()) is False

        def boom(elem, pid):
            raise OSError("the element is gone")

        monkeypatch.setattr(ffi, "prop_value_ex", boom)
        assert ffi.element_has_focus(object()) is False

    def test_an_explicit_true_is_accepted(self, monkeypatch) -> None:
        monkeypatch.setattr(ffi, "prop_value_ex", lambda e, pid: (ffi.S_OK, ffi.VT_BOOL, True))
        assert ffi.element_has_focus(object()) is True


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


class TestSetValue:
    def test_it_asks_for_the_live_secure_check(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        monkeypatch.setattr(ffi, "set_element_value", lambda e, v: ffi.S_OK)
        driver.set_value(_APP, _REC, "x")
        assert element["for_write"] is True

    def test_no_value_pattern_names_the_alternative(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        monkeypatch.setattr(ffi, "set_element_value", lambda e, v: None)
        result = driver.set_value(_APP, _REC, "x")
        assert result.ok is False
        assert "computer_type_text" in result.text

    def test_a_read_only_field_is_its_OWN_refusal(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """Distinct from a generic failure, because the next move differs.

        A model told only "failed" retries the same call forever; one told the field
        is read-only looks for a different target.
        """
        monkeypatch.setattr(ffi, "set_element_value", lambda e, v: ffi.E_ACCESSDENIED)
        result = driver.set_value(_APP, _REC, "x")
        assert result.ok is False
        assert "read-only" in result.text


class TestSetValueMarshalsARealBstr:
    """``SetValue`` takes a length-PREFIXED BSTR, not a bare LPCWSTR."""

    def test_the_argtype_is_not_c_wchar_p(self) -> None:
        """**The defect this test exists for, and it is memory disclosure.**

        ctypes marshals ``c_wchar_p`` as a plain ``LPCWSTR`` with no length prefix.
        A provider that calls ``SysStringLen`` on it reads the 4 bytes BEFORE the
        buffer as the length — measured returning 282 for an 11-character string,
        which writes roughly 540 bytes of adjacent process heap into the target
        field. The value round-trips correctly in a casual test, so nothing about
        the symptom points at the cause.
        """
        import ctypes

        argtypes = ffi._VTABLE[("IUIAutomationValuePattern", "SetValue")][2]
        assert argtypes == [ctypes.c_void_p], "SetValue must take an allocated BSTR"

    def test_a_bstr_is_allocated_and_freed_around_the_call(self, monkeypatch) -> None:
        """Allocated through the OS, and freed on every path.

        The callee copies what it needs, so the BSTR is ours to free immediately;
        leaking one per ``set_value`` would be an unbounded leak on a hot path.
        """
        events: list = []

        class _Oleaut:
            @staticmethod
            def SysAllocString(text):
                events.append(("alloc", text))
                return 0xB57A

            @staticmethod
            def SysFreeString(ptr):
                events.append(("free", ptr))

        def vcall(ptr, iface, method):
            def call(*args):
                if method == "get_CurrentIsReadOnly":
                    args[-1]._obj.value = 0
                    return ffi.S_OK
                if method == "SetValue":
                    events.append(("setvalue", args[-1]))
                    return ffi.S_OK
                return ffi.S_OK

            return call

        monkeypatch.setattr(ffi, "_libraries", lambda: {"oleaut32": _Oleaut()})
        monkeypatch.setattr(ffi, "vcall", vcall)
        monkeypatch.setattr(ffi, "pattern", lambda e, pid: object())
        monkeypatch.setattr(ffi, "release", lambda p: None)

        assert ffi.set_element_value(object(), "hello") == ffi.S_OK
        assert events == [("alloc", "hello"), ("setvalue", 0xB57A), ("free", 0xB57A)]

    def test_the_bstr_is_freed_even_when_setvalue_FAILS(self, monkeypatch) -> None:
        freed: list = []

        class _Oleaut:
            @staticmethod
            def SysAllocString(text):
                return 0xB57A

            @staticmethod
            def SysFreeString(ptr):
                freed.append(ptr)

        def vcall(ptr, iface, method):
            def call(*args):
                if method == "get_CurrentIsReadOnly":
                    args[-1]._obj.value = 0
                    return ffi.S_OK
                return _E_FAIL

            return call

        monkeypatch.setattr(ffi, "_libraries", lambda: {"oleaut32": _Oleaut()})
        monkeypatch.setattr(ffi, "vcall", vcall)
        monkeypatch.setattr(ffi, "pattern", lambda e, pid: object())
        monkeypatch.setattr(ffi, "release", lambda p: None)

        assert ffi.set_element_value(object(), "x") == _E_FAIL
        assert freed == [0xB57A]

    def test_a_read_only_field_allocates_NOTHING(self, monkeypatch) -> None:
        """Refused before the allocation, so the early exit cannot leak."""
        allocated: list = []

        class _Oleaut:
            @staticmethod
            def SysAllocString(text):
                allocated.append(text)
                return 0xB57A

            @staticmethod
            def SysFreeString(ptr):
                pass

        def vcall(ptr, iface, method):
            def call(*args):
                if method == "get_CurrentIsReadOnly":
                    args[-1]._obj.value = 1  # read-only
                    return ffi.S_OK
                raise AssertionError("SetValue ran on a read-only field")

            return call

        monkeypatch.setattr(ffi, "_libraries", lambda: {"oleaut32": _Oleaut()})
        monkeypatch.setattr(ffi, "vcall", vcall)
        monkeypatch.setattr(ffi, "pattern", lambda e, pid: object())
        monkeypatch.setattr(ffi, "release", lambda p: None)

        assert ffi.set_element_value(object(), "x") == ffi.E_ACCESSDENIED
        assert allocated == []


class TestScroll:
    def test_the_idle_axis_is_NoAmount_never_zero(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        """**Zero is ``LargeDecrement``, not "do not move".**

        ``Scroll`` takes both axes in one call, so passing zero for the axis the
        caller is not moving would scroll it BACKWARDS on every call.
        """
        seen: list = []

        def scroll(e, *, horizontal, vertical):
            seen.append((horizontal, vertical))
            return ffi.S_OK

        monkeypatch.setattr(ffi, "scroll_element", scroll)
        driver.scroll(_APP, _REC, "down", 1)
        assert seen == [(ffi.ScrollAmount_NoAmount, ffi.ScrollAmount_LargeIncrement)]
        seen.clear()
        driver.scroll(_APP, _REC, "right", 1)
        assert seen == [(ffi.ScrollAmount_LargeIncrement, ffi.ScrollAmount_NoAmount)]

    def test_pages_becomes_that_many_calls(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        calls: list = []
        monkeypatch.setattr(ffi, "scroll_element", lambda e, **k: calls.append(1) or ffi.S_OK)
        driver.scroll(_APP, _REC, "down", 3)
        assert len(calls) == 3

    def test_a_non_scrolling_axis_names_the_alternative(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        monkeypatch.setattr(ffi, "scroll_element", lambda e, **k: ffi.E_ACCESSDENIED)
        result = driver.scroll(_APP, _REC, "down", 1)
        assert result.ok is False
        assert "parent" in result.text

    def test_an_unknown_direction_lists_the_supported_ones(
        self, driver: WindowsBackend, element, monkeypatch
    ) -> None:
        result = driver.scroll(_APP, _REC, "sideways", 1)
        assert result.ok is False
        for name in ("up", "down", "left", "right"):
            assert name in result.text


class TestTheActionSlotsAreTheMEASUREDOnes:
    """Two interfaces did NOT match a naive reading of the header, and both failed
    in ways no behavioural test could see.

    A wrong ACTION slot is the dangerous class: it either faults (a dead session) or
    — worse — lands on a neighbouring method that returns ``S_OK`` while doing
    something else. Both cases happened here, so the slots are pinned as numbers
    with the anchor that fixed each one named in the assertion.
    """

    def test_scroll_is_slot_3_not_slot_4(self) -> None:
        """**Slot 4 is ``SetScrollPercent``, and it silently did nothing.**

        It returned ``S_OK`` for every call while the panel never moved, so the
        driver reported "scrolled 2 page(s)" over an unchanged window and the model
        would keep scrolling forever. Fixed by two shape-distinguishable anchors on a
        real scrollable panel: slot 8 returned 20.13 (a ViewSize FRACTION, not a
        percent) and slot 10 returned 1 for ``VerticallyScrollable`` (a VARIANT_BOOL,
        not a double).
        """
        assert ffi._VTABLE[("IUIAutomationScrollPattern", "Scroll")][0] == 3
        assert (
            ffi._VTABLE[("IUIAutomationScrollPattern", "get_CurrentVerticalScrollPercent")][0] == 6
        )
        assert (
            ffi._VTABLE[("IUIAutomationScrollPattern", "get_CurrentVerticallyScrollable")][0] == 10
        )

    def test_do_default_action_is_slot_3_not_slot_4(self) -> None:
        """Slot 4 on this interface is ``Select``.

        ``get_CurrentRole`` (slot 10) returning ROLE_SYSTEM_PUSHBUTTON = 43 on a real
        Button, and ``get_CurrentDefaultAction`` (slot 15) returning 'Press' on the
        same element, are the two anchors that place it at 3.
        """
        legacy = "IUIAutomationLegacyIAccessiblePattern"
        assert ffi._VTABLE[(legacy, "DoDefaultAction")][0] == 3
        assert ffi._VTABLE[(legacy, "get_CurrentRole")][0] == 10
        assert ffi._VTABLE[(legacy, "get_CurrentDefaultAction")][0] == 15

    def test_every_getter_out_param_is_a_pointer(self) -> None:
        """A bare value where the callee expects an out-param makes it write through
        an integer, which is a wild store rather than a wrong answer."""
        import ctypes

        for (iface, method), (_slot, _restype, argtypes) in ffi._VTABLE.items():
            if not method.startswith("get_") or not argtypes:
                continue
            last = argtypes[-1]
            assert hasattr(last, "_type_") or last is ctypes.c_void_p, f"{iface}.{method}"


class TestPerformActionVocabularyIsClosed:
    """The un-gated write path this closed set exists to prevent."""

    def test_an_unknown_action_is_refused_with_the_vocabulary(
        self, driver: WindowsBackend, element
    ) -> None:
        result = driver.perform_action(_APP, _REC, "SetValue")
        assert result.ok is False
        assert "SetValue" not in result.text.split("Supported:")[-1]
        for name in windows_driver.SUPPORTED_ACTIONS:
            assert name in result.text

    @pytest.mark.parametrize(
        ("action", "seam"),
        [
            ("toggle", "toggle_element"),
            ("select", "select_element"),
        ],
    )
    def test_each_name_maps_to_ONE_specific_pattern(
        self, driver: WindowsBackend, element, monkeypatch, action: str, seam: str
    ) -> None:
        """Not to a provider-named action string.

        Forwarding the caller's string to ``LegacyIAccessiblePattern`` would be an
        un-gated write path: that interface also exposes ``SetValue`` and sits on
        nearly every node of a real Win32 tree.
        """
        called: list[str] = []
        for name in ("invoke_element", "toggle_element", "select_element", "do_default_action"):
            monkeypatch.setattr(ffi, name, lambda e, _n=name: called.append(_n) or ffi.S_OK)
        assert driver.perform_action(_APP, _REC, action).ok is True
        assert called == [seam]

    @pytest.mark.parametrize("action", ["expand", "collapse"])
    def test_expand_and_collapse_pass_the_direction(
        self, driver: WindowsBackend, element, monkeypatch, action: str
    ) -> None:
        seen: list = []
        monkeypatch.setattr(
            ffi, "expand_element", lambda e, *, expand: seen.append(expand) or ffi.S_OK
        )
        assert driver.perform_action(_APP, _REC, action).ok is True
        assert seen == [action == "expand"]

    def test_the_legacy_set_value_slot_is_not_bound_anywhere(self) -> None:
        """Structural: the one guarantee no behavioural test can give.

        The vocabulary above can be widened by a future edit; an unbound vtable slot
        cannot be reached at all, so this is the durable half of the control.
        """
        legacy = {
            method
            for (iface, method) in ffi._VTABLE
            if iface == "IUIAutomationLegacyIAccessiblePattern"
        }
        assert "SetValue" not in legacy
        assert "DoDefaultAction" in legacy


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


class TestReAddressingChecksNameAndRole:
    """An index only means something relative to the walk that produced it."""

    @staticmethod
    def _walk(monkeypatch, role_ct: int, name: str):
        """Drive ``addressed`` over a one-element fake tree."""
        walk = ffi.WalkResult(elements=["E0"], depths=[1])
        import contextlib

        monkeypatch.setattr(ffi, "dpi_awareness_scope", contextlib.nullcontext)
        monkeypatch.setattr(ffi, "window_is_live", lambda h: True)
        monkeypatch.setattr(ffi, "element_from_hwnd", lambda h: "ROOT")
        monkeypatch.setattr(ffi, "create_cache_request", lambda **k: "CACHE")
        monkeypatch.setattr(ffi, "walk_bounded", lambda *a, **k: walk)
        monkeypatch.setattr(ffi, "owned", lambda p: contextlib.nullcontext(p))
        monkeypatch.setattr(
            ffi,
            "cached_prop",
            lambda e, pid: role_ct if pid == ffi.UIA_ControlTypePropertyId else name,
        )
        monkeypatch.setattr(snapshot_windows, "_secure_for_role", lambda e, r: False)
        monkeypatch.setattr(ffi, "add_ref", lambda p: None)
        monkeypatch.setattr(ffi, "release", lambda p: None)
        monkeypatch.setattr(ffi, "release_all", lambda ps: None)

    def test_a_same_role_DIFFERENT_name_control_is_refused(self, monkeypatch) -> None:
        """**The data-loss defect this test exists for.**

        A toolbar that swapped Save for Delete leaves both as ``Button`` at the same
        index. A role-only check accepts Delete, and the action presses it — so the
        model's "click Save" deletes the user's work. The NAME is what separates them.
        """
        BUTTON = 50000
        self._walk(monkeypatch, BUTTON, "Delete")
        rec = ElementRec(index=0, role="Button", title="Save")
        req = SnapshotRequest(max_nodes=50, max_depth=5, text_limit=40, want_image=False)
        with snapshot_windows.addressed(_APP, rec, req) as element:
            assert element is None, "a different control was accepted at the same index"

    def test_the_matching_control_is_yielded(self, monkeypatch) -> None:
        """The guard must not refuse the ordinary case."""
        BUTTON = 50000
        self._walk(monkeypatch, BUTTON, "Save")
        rec = ElementRec(index=0, role="Button", title="Save")
        req = SnapshotRequest(max_nodes=50, max_depth=5, text_limit=40, want_image=False)
        with snapshot_windows.addressed(_APP, rec, req) as element:
            assert element == "E0"

    def test_a_different_role_is_still_refused(self, monkeypatch) -> None:
        EDIT = 50004
        self._walk(monkeypatch, EDIT, "Save")
        rec = ElementRec(index=0, role="Button", title="Save")
        req = SnapshotRequest(max_nodes=50, max_depth=5, text_limit=40, want_image=False)
        with snapshot_windows.addressed(_APP, rec, req) as element:
            assert element is None


@contextmanager
def _null_scope():
    """A no-op stand-in for ``dpi_awareness_scope``: these tests fake user32."""
    yield
