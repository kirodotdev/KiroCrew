"""How the macOS driver presses and types into an addressed element, and when it refuses.

An accessibility click tries ``AXPress`` first, advances to ``AXConfirm`` only on an
action-unsupported answer and names the verb that worked, stops at the first real AX error,
opens a right click with ``AXShowMenu`` and never a press, and still attempts a verb the
element does not advertise. When every direct verb fails, the click presses a pressable
ancestor of comparable size and says so in its result; it declines an ancestor many times
larger, climbs at most ``MAX_ANCESTOR_PRESS_HOPS`` parents, never climbs from an element
with no frame, never falls back for a right click, leaks no CF reference, and is never
reached by a directly pressable element. When the ``AXFocused`` write fails, ``type_text``
and ``press_key`` refuse and deliver nothing; ``type_text`` still types when the focus
takes, and ``press_key`` with no addressed element is unaffected. The snapshot walk,
permissions, the driver's other paths, pointer delivery and point ownership are pinned in
``test_computer_use_snapshot_macos.py``, and element frames, traits, focus and child
collections in ``test_computer_use_snapshot_macos_tree_content.py``; the ``fakes`` fixture
and the staging helpers live in ``computer_use_snapshot_macos_helpers.py``.
"""

from __future__ import annotations

# Reuse the harness rather than forking a second set of fakes: a divergent copy
# would drift from the real symbol table and quietly stop proving anything.
import pytest
from computer_use_snapshot_macos_helpers import (  # noqa: F401 -- the fixture is requested by name
    _APP,
    _frame,
    _req,
    _stage_window,
    fakes_fixture,
)

# The fake-framework harness is shared with ``test_computer_use_ffi`` rather than
# forked (a divergent copy would drift from the real symbol table and quietly stop
# proving anything). Imported by MODULE NAME, not as ``test.test_computer_use_ffi``:
# ``test/`` has no ``__init__.py``, so it is not a package — the dotted form happens
# to resolve when the whole suite is collected from the repo root and fails with
# ``ModuleNotFoundError`` under CI's sharded collection, which is exactly how it
# broke on the 3.10 shard. pytest inserts each test file's directory on
# ``sys.path`` (rootdir-relative ``consider_namespace_packages``-free import mode),
# so the bare module name is the form that always resolves.
from test_computer_use_ffi import _Fakes

from kiro_crew.computer_use import macos_driver, macos_ffi, snapshot_macos
from kiro_crew.computer_use.types import (
    CLICK_METHOD_ACCESSIBILITY,
    MAX_ANCESTOR_PRESS_HOPS,
    MOUSE_BUTTON_RIGHT,
    ClickRequest,
)

# This file replaces ctypes.CDLL, find_library or a cached libc handle for a test,
# so the shared macOS handle caches are put back after each one (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


def _force_action_result(fake: _Fakes, action: str, code: int) -> None:
    """Make ``AXUIElementPerformAction(_, action)`` return *code* for ANY element.

    Keyed by ACTION rather than by handle: the addressed element's handle is minted
    during the tree walk, so a test cannot predict it.
    """
    real = fake.ax._perform

    def _perform(elem, name_ref):
        rc = real(elem, name_ref)
        if fake.ax.heap.payload(name_ref) == action:
            return code
        return rc

    fake.ax.AXUIElementPerformAction._fn = _perform  # type: ignore[attr-defined]


def _fail_focus(fake: _Fakes) -> None:
    """Make every ``AXFocused`` write fail, whatever handle the walk allocated."""
    real = fake.ax._set_attr

    def _set_attr(elem, key, value):
        rc = real(elem, key, value)
        if fake.ax.heap.payload(key) == "AXFocused":
            return macos_ffi.AX_ERR_CANNOT_COMPLETE
        return rc

    fake.ax.AXUIElementSetAttributeValue._fn = _set_attr  # type: ignore[attr-defined]


class TestTheAccessibilityPressLadder:
    """``AXPress`` alone was the driver's biggest avoidable refusal.

    ``AXUIElementPerformAction`` returns ``-25206`` (action unsupported) on a
    disclosure triangle, a Finder row and many web controls — all of which answer
    ``AXConfirm`` or ``AXOpen``. Before the ladder the model was told "the click
    failed" for elements that were perfectly clickable, and its only remaining move
    was to guess coordinates, which is exactly the trial-and-error this is meant to
    remove.
    """

    @staticmethod
    def _staged_button(fakes: _Fakes, *, actions: list[str] | None = None):
        """A driver plus an ElementRec whose advertised actions we control."""
        child: dict = {"role": "AXButton", "title": "Save"}
        if actions is not None:
            child["actions"] = actions
        _stage_window(fakes, children=[child])
        backend = macos_driver.MacOSBackend()
        snap = snapshot_macos.build_snapshot(_APP, _req())
        rec = next(r for r in snap.elements if r.role == "AXButton")
        return backend, rec

    @staticmethod
    def _performed(fakes: _Fakes) -> list:
        return [a[1] for (n, a) in fakes.ax.calls if n == "AXUIElementPerformAction"]

    def test_AXPress_alone_still_wins_and_is_tried_FIRST(self, fakes: _Fakes):
        backend, rec = self._staged_button(fakes)
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is True
        assert self._performed(fakes)[0] == "AXPress"

    def test_an_unsupported_press_falls_through_to_AXConfirm(self, fakes: _Fakes):
        """The recoverable case: the element advertises the verb but presses badly."""
        backend, rec = self._staged_button(fakes, actions=["AXPress", "AXConfirm"])
        _force_action_result(fakes, "AXPress", macos_ffi.AX_ERR_ACTION_UNSUPPORTED)
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is True
        assert "AXConfirm" in self._performed(fakes)
        # And the winning verb is NAMED, so the model learns what this element wants.
        assert "AXConfirm" in result.text

    def test_a_REAL_failure_stops_the_ladder(self, fakes: _Fakes):
        """Only "wrong verb" advances. A wedged app or destroyed widget must not have
        three different verbs thrown at it and then be reported under the last one —
        the FIRST real error is the true cause."""
        backend, rec = self._staged_button(fakes, actions=["AXPress", "AXConfirm"])
        _force_action_result(fakes, "AXPress", macos_ffi.AX_ERR_CANNOT_COMPLETE)
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False
        assert "AXConfirm" not in self._performed(fakes)

    def test_a_RIGHT_click_uses_AXShowMenu_and_never_falls_back_to_a_press(self, fakes: _Fakes):
        """A right click is a different GESTURE, not a press.

        Falling back to ``AXPress`` would activate the control instead of opening its
        context menu — performing a different action than the model asked for.
        """
        backend, rec = self._staged_button(fakes, actions=["AXPress", "AXShowMenu"])
        result = backend.click(
            _APP,
            rec,
            ClickRequest(method=CLICK_METHOD_ACCESSIBILITY, button=MOUSE_BUTTON_RIGHT),
        )
        assert result.ok is True
        performed = self._performed(fakes)
        assert "AXShowMenu" in performed
        assert "AXPress" not in performed

    def test_an_unadvertised_verb_is_still_ATTEMPTED(self, fakes: _Fakes):
        """Advertisement is unreliable on web content: a node can perform a press it
        never listed, so an empty/foreign action list must not short-circuit."""
        backend, rec = self._staged_button(fakes, actions=["AXSomethingElse"])
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is True
        assert "AXPress" in self._performed(fakes)


class TestKeyboardRefusesOnAFailedFocus:
    """A failed focus must not fall through to blind input.

    ``policy.check_input_target`` validated the element the model ADDRESSED — that is
    where ``ElementRec.secure`` is read from. If ``AXFocused`` does not take,
    ``post_text``/``post_key`` deliver to the app's EXISTING focus, which the policy
    layer never inspected and which can be the password field it just refused.
    "Best effort" there would silently defeat the secure-field floor.
    """

    @staticmethod
    def _staged(fakes: _Fakes):
        _stage_window(fakes, children=[{"role": "AXButton", "title": "Save"}])
        backend = macos_driver.MacOSBackend()
        snap = snapshot_macos.build_snapshot(_APP, _req())
        rec = next(r for r in snap.elements if r.role == "AXButton")
        return backend, rec

    def test_type_text_refuses_and_types_NOTHING(self, fakes: _Fakes):
        backend, rec = self._staged(fakes)
        _fail_focus(fakes)
        result = backend.type_text(_APP, rec, "secret")
        assert result.ok is False
        # THE assertion: no keystrokes reached the app at all.
        assert not [a for (n, a) in fakes.cg.calls if n == "CGEventKeyboardSetUnicodeString"]
        # And the refusal is actionable rather than a bare failure.
        assert "focus" in result.text.lower()

    def test_press_key_refuses_and_posts_NOTHING(self, fakes: _Fakes):
        backend, rec = self._staged(fakes)
        _fail_focus(fakes)
        result = backend.press_key(_APP, rec, "cmd+a")
        assert result.ok is False
        assert not [a for (n, a) in fakes.cg.calls if n == "CGEventPostToPid"]

    def test_a_SUCCESSFUL_focus_still_types(self, fakes: _Fakes):
        """Inverse guard: the refusal must not break the normal path."""
        backend, rec = self._staged(fakes)
        assert backend.type_text(_APP, rec, "hello").ok is True

    def test_indexless_keyboard_input_is_unaffected(self, fakes: _Fakes):
        """No element to focus means nothing to fail — the refusal must not fire on a
        path that never attempted a focus."""
        _stage_window(fakes, children=[{"role": "AXButton", "title": "Save"}])
        assert macos_driver.MacOSBackend().press_key(_APP, None, "cmd+s").ok is True


class TestTheAncestorPressFallback:
    """The last rung: press the container that visually IS the row.

    Web content renders a clickable row as a plain ``AXStaticText`` inside a
    pressable wrapper. The text node advertises no actions and refuses every verb,
    so an element click reported failure for a row a human clicks without thinking —
    leaving the model to guess coordinates, which is what the element path exists to
    avoid.

    Both guards are asserted here, because an unguarded version of this is a
    wrong-click generator: walk far enough up and something is always pressable, and
    that something is eventually the page.
    """

    @staticmethod
    def _staged(
        fakes: _Fakes,
        *,
        target_frame=(10.0, 10.0, 100.0, 20.0),
        parent_frame=(10.0, 10.0, 200.0, 20.0),
        parent_presses: bool = True,
        hops: int = 1,
    ):
        """A dead text node nested *hops* deep inside a pressable wrapper."""
        window = _stage_window(
            fakes,
            bounds=(0.0, 0.0, 800.0, 600.0),
            children=[{"role": "AXStaticText", "title": "Row label", "frame": target_frame}],
        )
        child = fakes.heap.payload(fakes.ax.attrs[(window, "AXChildren")])[0]
        # Chain of ancestors above the addressed node. Every level is given the SAME
        # frame, so a case that wants the walk to run out of hops (rather than trip
        # the area guard) can do so without the geometry deciding it first.
        current = child
        ancestors: list[int] = []
        for _ in range(hops):
            parent = fakes.new_element()
            fakes.ax.attrs[(parent, "AXRole")] = fakes.new_string("AXGroup")
            _frame(fakes, parent, *parent_frame)
            fakes.ax.attrs[(current, "AXParent")] = parent
            current = parent
            ancestors.append(parent)
        if not parent_presses:
            for parent in ancestors:
                fakes.ax.perform_results[(parent, "AXPress")] = macos_ffi.AX_ERR_ACTION_UNSUPPORTED
        backend = macos_driver.MacOSBackend()
        snap = snapshot_macos.build_snapshot(_APP, _req())
        rec = next(r for r in snap.elements if r.role == "AXStaticText")
        return backend, rec, child

    @staticmethod
    def _kill_direct_verbs(fakes: _Fakes, target: int) -> None:
        """Make every verb fail on *target* only, as a web text node does.

        Scoped to the addressed handle rather than applied globally: the whole point
        of the fallback is that the ANCESTOR still presses, so a blanket failure
        would make every case here fail for the wrong reason. The handle is known
        because the caller staged it.

        Note this cannot be built by calling ``_force_action_result`` once per verb —
        each call re-wraps the ORIGINAL ``_perform``, so only the last would survive.
        """
        for verb in ("AXPress", "AXConfirm", "AXOpen", "AXShowMenu"):
            fakes.ax.perform_results[(target, verb)] = macos_ffi.AX_ERR_ACTION_UNSUPPORTED

    def test_a_dead_text_node_is_activated_through_its_wrapper(self, fakes: _Fakes):
        backend, rec, target = self._staged(fakes)
        self._kill_direct_verbs(fakes, target)
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is True

    def test_the_result_SAYS_it_pressed_the_container(self, fakes: _Fakes):
        """The model has to know the press landed somewhere other than where it
        aimed: that is the difference between "my click worked" and "something near
        my click worked", and the only clue if the outcome looks wrong."""
        backend, rec, target = self._staged(fakes)
        self._kill_direct_verbs(fakes, target)
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert "enclosing" in result.text
        assert "verify" in result.text

    def test_an_ancestor_MANY_times_the_size_is_refused(self, fakes: _Fakes):
        """THE guard. A container far larger than the element addressed is a layout
        wrapper or the page body, not the row — pressing it would activate something
        the model never named, and report success for it."""
        backend, rec, target = self._staged(
            fakes,
            target_frame=(10.0, 10.0, 100.0, 20.0),
            parent_frame=(0.0, 0.0, 800.0, 600.0),
        )
        self._kill_direct_verbs(fakes, target)
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False

    def test_the_walk_stops_at_the_hop_bound(self, fakes: _Fakes):
        """Climb far enough and SOMETHING is always pressable — eventually the page.

        Staged with an ancestor chain longer than the bound, every level refusing the
        press and every level the same size (so the area guard cannot be what stops
        it). The assertion is on the number of ancestors actually VISITED, not merely
        that the click failed: a failed click proves nothing about where the walk
        stopped, and an unbounded version would still fail here while having pressed
        its way up the whole tree.
        """
        bound = MAX_ANCESTOR_PRESS_HOPS
        backend, rec, target = self._staged(fakes, hops=bound + 4, parent_presses=False)
        self._kill_direct_verbs(fakes, target)
        parents_before = len(
            [
                a
                for (n, a) in fakes.ax.calls
                if n == "AXUIElementCopyAttributeValue" and a[1] == "AXParent"
            ]
        )
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False
        climbed = (
            len(
                [
                    a
                    for (n, a) in fakes.ax.calls
                    if n == "AXUIElementCopyAttributeValue" and a[1] == "AXParent"
                ]
            )
            - parents_before
        )
        assert climbed == bound

    def test_no_frame_on_the_TARGET_declines_entirely(self, fakes: _Fakes):
        """Without the element's own rect there is nothing to compare an ancestor
        against, so there is no way to tell the row from the page. Declining is the
        only safe answer — guessing would press an unknown container."""
        window = _stage_window(fakes, children=[{"role": "AXStaticText", "title": "Row label"}])
        target = fakes.heap.payload(fakes.ax.attrs[(window, "AXChildren")])[0]
        # A pressable ancestor IS present, so the only thing that can decline here is
        # the missing-frame guard itself.
        parent = fakes.new_element()
        fakes.ax.attrs[(parent, "AXRole")] = fakes.new_string("AXGroup")
        _frame(fakes, parent, 0.0, 0.0, 120.0, 24.0)
        fakes.ax.attrs[(target, "AXParent")] = parent
        backend = macos_driver.MacOSBackend()
        snap = snapshot_macos.build_snapshot(_APP, _req())
        rec = next(r for r in snap.elements if r.role == "AXStaticText")
        assert rec.frame is None
        self._kill_direct_verbs(fakes, target)
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is False
        # And it never even asked for the parent — the guard is checked BEFORE the
        # climb, so no AX round-trips are spent on a walk that cannot be judged.
        assert not [
            a
            for (n, a) in fakes.ax.calls
            if n == "AXUIElementCopyAttributeValue" and a[1] == "AXParent"
        ]

    def test_a_RIGHT_click_never_uses_the_fallback(self, fakes: _Fakes):
        """A context menu on the wrapper is a different menu from one on the row.
        Same reasoning as the menu ladder never falling back to a press."""
        backend, rec, target = self._staged(fakes)
        # The addressed node refuses the menu; its ancestor would happily press.
        self._kill_direct_verbs(fakes, target)
        result = backend.click(
            _APP,
            rec,
            ClickRequest(method=CLICK_METHOD_ACCESSIBILITY, button=MOUSE_BUTTON_RIGHT),
        )
        assert result.ok is False
        # THE assertion: no press was attempted anywhere. Turning "open the context
        # menu" into "activate the enclosing control" is a different action, not a
        # recovery.
        performed = [a[1] for (n, a) in fakes.ax.calls if n == "AXUIElementPerformAction"]
        assert "AXPress" not in performed

    def test_a_DIRECTLY_pressable_element_never_reaches_the_fallback(self, fakes: _Fakes):
        """Inverse guard: the heuristic must not fire on the normal path, where it
        could press a container after the element had already been activated."""
        backend, rec, target = self._staged(fakes)
        result = backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert result.ok is True
        assert "enclosing" not in result.text

    def test_the_fallback_leaves_no_cf_reference_behind(self, fakes: _Fakes):
        """``ax_parent`` returns +1 references and the walk climbs several."""
        backend, rec, target = self._staged(fakes, hops=3, parent_presses=False)
        self._kill_direct_verbs(fakes, target)
        before = len(fakes.heap.live)
        backend.click(_APP, rec, ClickRequest(method=CLICK_METHOD_ACCESSIBILITY))
        assert len(fakes.heap.live) <= before
