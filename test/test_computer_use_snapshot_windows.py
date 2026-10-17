"""``computer_use.snapshot_windows`` — the pure transforms over a fake element.

Runs on every platform: the module's decision functions are pure over a fake
``cached_prop``/``is_secure_element`` source, so no desktop and no native call is
needed. These cover the seams a code review found shipping green because the only
Windows tests monkeypatched the whole module away:

* the frame decoder, which read UIA's ``[left, top, WIDTH, HEIGHT]`` as
  ``[l, t, right, bottom]`` and either dropped or distorted every frame;
* the secure-role narrowing, which must fail closed on a maskable/unknown role
  and stay plain on a display role whose value is its visible label;
* the ``editable`` trait, which a cached default made appear on non-text controls.
"""

from __future__ import annotations

import pytest

from kiro_crew.computer_use import snapshot_windows as S
from kiro_crew.computer_use import windows_ffi
from kiro_crew.computer_use.types import (
    WINDOWS_SUPPORTED_ACTIONS,
    AppRef,
    ComputerUseError,
    SnapshotRequest,
)


class TestLocalFrame:
    """UIA returns [left, top, WIDTH, HEIGHT], verified against GetWindowRect."""

    def test_a_width_height_rect_is_read_as_is(self) -> None:
        # (screen l=100, t=50, w=200, h=80), origin (10, 20) -> local (90, 30, 200, 80).
        frame = S._local_frame([100.0, 50.0, 200.0, 80.0], (10.0, 20.0, 1000.0, 700.0))
        assert frame == (90.0, 30.0, 200.0, 80.0)

    def test_a_child_below_its_origin_keeps_a_positive_height(self) -> None:
        """The regression: reading w/h as right/bottom gave a negative height here.

        A real Explorer child measured [67, 63, 970, 28] — width 970, height 28.
        The old subtraction produced height 28-63 = -35 and the guard dropped it.
        """
        frame = S._local_frame([67.0, 63.0, 970.0, 28.0], (0.0, 0.0, 1200.0, 800.0))
        assert frame is not None
        assert frame[2] == 970.0
        assert frame[3] == 28.0

    def test_a_zero_area_rect_is_dropped(self) -> None:
        # An off-screen/collapsed element: no clickable point, so no frame.
        assert S._local_frame([10.0, 10.0, 0.0, 40.0], (0.0, 0.0, 100.0, 100.0)) is None

    @pytest.mark.parametrize("bad", [None, [1.0, 2.0, 3.0], "x", [1, 2, 3, 4, 5]])
    def test_a_malformed_rect_is_none_not_a_partial(self, bad: object) -> None:
        assert S._local_frame(bad, (0.0, 0.0, 100.0, 100.0)) is None

    def test_no_origin_yields_no_frame_rather_than_a_screen_rect(self) -> None:
        """An unlabelled coordinate is worse than no coordinate.

        ``Snapshot.window_bounds`` is None exactly when the origin is unavailable (a
        window closing mid-walk, minimized, UIPI-restricted), so a screen-absolute
        rect returned here would reach the model with nothing marking it as a
        different coordinate space: it cannot tell a window-local (12, 40) from a
        screen-absolute one, and the difference is the whole position of the window.
        On a window at (1200, 400) the model would read x=1240 as window-local and
        aim ~1200px away. ``snapshot_macos._local_frame`` returns None for the same
        reason.
        """
        assert S._local_frame([100.0, 50.0, 200.0, 80.0], None) is None


class _FakeElem:
    """A cached-property source. Maps property id -> value."""

    def __init__(self, props: dict) -> None:
        self.props = props


class TestSecureForRole:
    """The narrowing that keeps the fail-closed floor where it protects."""

    @staticmethod
    def _install(monkeypatch, *, is_secure: bool | None) -> None:
        # is_secure=True/False -> is_secure_element verdict; None -> inconclusive
        # (prop_value_ex returns an unsupported sentinel shape).
        from kiro_crew.computer_use import windows_ffi as W

        if is_secure is None:
            monkeypatch.setattr(
                W, "prop_value_ex", lambda elem, pid: (W.S_OK, W.VT_UNKNOWN, 0x1234)
            )
            monkeypatch.setattr(
                S.windows_ffi,
                "is_secure_element",
                lambda elem: True,  # strict path treats inconclusive as secure
            )
        else:
            monkeypatch.setattr(S.windows_ffi, "is_secure_element", lambda elem: is_secure)
            monkeypatch.setattr(
                W,
                "prop_value_ex",
                lambda elem, pid: (W.S_OK, W.VT_BOOL, is_secure),
            )

    def test_a_definite_true_is_secure_on_a_maskable_role(self, monkeypatch) -> None:
        """A framework that reports a masked control is believed on a maskable role."""
        monkeypatch.setattr(S.windows_ffi, "is_secure_element", lambda elem: True)
        assert S._secure_for_role(_FakeElem({}), "Edit") is True

    def test_a_definite_true_is_secure_even_on_a_display_role(self, monkeypatch) -> None:
        """A display role is not consulted via the strict path, but a definite
        ``IsPassword=True`` on it is still honoured through the value-property route
        — so a framework that genuinely masks a Text node is believed."""
        from kiro_crew.computer_use import windows_ffi as W

        monkeypatch.setattr(W, "prop_value_ex", lambda elem, pid: (W.S_OK, W.VT_BOOL, True))
        assert S._secure_for_role(_FakeElem({}), "Text") is True

    def test_an_inconclusive_read_on_a_maskable_role_is_secure(self, monkeypatch) -> None:
        monkeypatch.setattr(S.windows_ffi, "is_secure_element", lambda elem: True)
        assert S._secure_for_role(_FakeElem({}), "Edit") is True

    def test_an_inconclusive_read_on_an_unknown_role_is_secure(self, monkeypatch) -> None:
        monkeypatch.setattr(S.windows_ffi, "is_secure_element", lambda elem: True)
        assert S._secure_for_role(_FakeElem({}), S._UNKNOWN_ROLE) is True

    def test_an_inconclusive_read_on_a_display_role_is_plain(self, monkeypatch) -> None:
        """A TreeItem's value is its label, not a masked secret.

        This is the exclusion that stops File Explorer from suppressing its
        screenshot. The strict read is NOT consulted for a display role; the
        value-property route is, and an explicit non-boolean there resolves plain.
        """
        from kiro_crew.computer_use import windows_ffi as W

        # is_secure_element would say secure, but the role is excluded, so the
        # value-property route decides — and a supported non-True is plain.
        monkeypatch.setattr(S.windows_ffi, "is_secure_element", lambda elem: True)
        monkeypatch.setattr(W, "prop_value_ex", lambda elem, pid: (W.S_OK, W.VT_BOOL, False))
        assert S._secure_for_role(_FakeElem({}), "TreeItem") is False
        assert "TreeItem" not in S._SECURE_QUESTION_APPLIES
        assert "Custom" in S._SECURE_QUESTION_APPLIES

    def test_the_unsupported_sentinel_on_a_display_role_is_plain(self, monkeypatch) -> None:
        """THIS is the case the role narrowing exists for, and the only one.

        The provider answered — it simply has no opinion — and on a display role whose
        value is its visible label there is no masked secret to protect. Explorer's
        tree provider implements ``IsPassword`` for nothing, so 13 of 87 nodes came
        back here and treating them as secure suppressed an ordinary file-manager
        window's screenshot.
        """
        from kiro_crew.computer_use import windows_ffi as W

        monkeypatch.setattr(W, "prop_value_ex", lambda elem, pid: (W.S_OK, W.VT_UNKNOWN, 0x1234))
        assert S._secure_for_role(_FakeElem({}), "TreeItem") is False

    @pytest.mark.parametrize(
        ("hr", "vt", "value", "why"),
        [
            (0x80131509, 0, None, "a managed failure (WPF's own value read does this)"),
            (0x80070005, 0, None, "access denied — a UIPI-restricted elevated window"),
            (0, 8, "", "a VT_BSTR: a type we cannot interpret is not a False"),
            (0, 3, 0, "a VT_I4: likewise"),
        ],
        ids=["e-fail", "access-denied", "bstr", "i4"],
    )
    def test_a_read_FAILURE_on_a_display_role_is_secure(
        self, monkeypatch, hr: int, vt: int, value: object, why: str
    ) -> None:
        """**The narrowing covers "not implemented", NOT "the read failed".**

        Those are different answers and only the first is evidence of anything. A
        dead provider, a UIPI-blocked elevated window, or an element torn down
        mid-relayout tells us nothing — so it must fall back to the same fail-CLOSED
        posture ``is_secure_element`` takes, whose docstring notes a wrong answer
        defeats value redaction, input refusal AND whole-window screenshot
        suppression at once.

        Windows' own driver notes Chromium exposes a real password field's value as a
        bullet run rather than the empty string Win32 returns, so its UIA mapping is
        not guaranteed to report ``Edit`` — which puts a masked field on exactly the
        roles this branch decides.
        """
        from kiro_crew.computer_use import windows_ffi as W

        monkeypatch.setattr(W, "prop_value_ex", lambda elem, pid: (hr, vt, value))
        assert S._secure_for_role(_FakeElem({}), "Text") is True, why

    def test_a_raising_read_on_a_display_role_is_secure(self, monkeypatch) -> None:
        """A debug line must not be the only trace of a photographed password."""
        from kiro_crew.computer_use import windows_ffi as W

        def boom(elem, pid):
            raise OSError("the element is gone")

        monkeypatch.setattr(W, "prop_value_ex", boom)
        assert S._secure_for_role(_FakeElem({}), "Text") is True


class TestEditableTrait:
    """editable must reflect a real writable field, not a cached default."""

    def test_a_non_text_role_is_never_editable(self, monkeypatch) -> None:
        """A TitleBar's cached ValueIsReadOnly defaults False; the role gate wins."""
        monkeypatch.setattr(S.windows_ffi, "cached_prop", lambda elem, pid: False)
        assert S.TRAIT_EDITABLE not in S._traits(_FakeElem({}), "TitleBar", secure=False)
        assert S.TRAIT_EDITABLE not in S._traits(_FakeElem({}), "Button", secure=False)

    def test_a_writable_edit_is_editable(self, monkeypatch) -> None:
        # ValueIsReadOnly is False AND the role accepts input.
        monkeypatch.setattr(
            S.windows_ffi,
            "cached_prop",
            lambda elem, pid: False if pid == S.windows_ffi.UIA_ValueIsReadOnlyPropertyId else None,
        )
        assert S.TRAIT_EDITABLE in S._traits(_FakeElem({}), "Edit", secure=False)

    def test_a_read_only_edit_is_not_editable(self, monkeypatch) -> None:
        monkeypatch.setattr(
            S.windows_ffi,
            "cached_prop",
            lambda elem, pid: True if pid == S.windows_ffi.UIA_ValueIsReadOnlyPropertyId else None,
        )
        assert S.TRAIT_EDITABLE not in S._traits(_FakeElem({}), "Edit", secure=False)

    def test_a_secure_element_has_no_traits(self, monkeypatch) -> None:
        """editable would confirm the password box accepts input."""
        monkeypatch.setattr(S.windows_ffi, "cached_prop", lambda elem, pid: False)
        assert S._traits(_FakeElem({}), "Edit", secure=True) == ()


class TestSelectedTrait:
    """``selected`` means list/table selection, NOT keyboard focus."""

    def test_it_comes_from_the_selection_property_not_focus(self, monkeypatch) -> None:
        """**The wrong-variable defect.**

        ``types.TRAIT_SELECTED`` is defined as list/table selection, and macOS sources
        it from ``AX_SELECTED``. Reading ``HasKeyboardFocus`` instead published one
        signal twice under two names — the element already carries ``focused`` and the
        ``<focused>`` marker — while a genuinely selected but unfocused row got no
        trait at all, so "which row is selected?" always named the caret's element.
        """
        reads: list[int] = []

        def cached(elem, pid):
            reads.append(pid)
            # Focused but NOT selected: the two must not be conflated.
            return True if pid == S.windows_ffi.UIA_HasKeyboardFocusPropertyId else False

        monkeypatch.setattr(S.windows_ffi, "cached_prop", cached)
        traits = S._traits(_FakeElem({}), "ListItem", secure=False)
        assert S.TRAIT_SELECTED not in traits
        assert S.windows_ffi.UIA_SelectionItemIsSelectedPropertyId in reads

    def test_a_selected_but_unfocused_row_is_selected(self, monkeypatch) -> None:
        """The case the focus-sourced version could never report."""
        monkeypatch.setattr(
            S.windows_ffi,
            "cached_prop",
            lambda elem, pid: pid == S.windows_ffi.UIA_SelectionItemIsSelectedPropertyId,
        )
        assert S.TRAIT_SELECTED in S._traits(_FakeElem({}), "ListItem", secure=False)

    def test_only_a_definite_true_counts(self, monkeypatch) -> None:
        """A provider without SelectionItemPattern reads as the property's default.

        The tri-state check is what stops that becoming a trait on every node.
        """
        monkeypatch.setattr(S.windows_ffi, "cached_prop", lambda elem, pid: None)
        assert S.TRAIT_SELECTED not in S._traits(_FakeElem({}), "ListItem", secure=False)

    def test_the_selection_property_is_in_the_cached_walk(self) -> None:
        """A cache miss would make the trait silently absent on every node.

        ``AddProperty`` on an already-built cache is refused rather than extending it,
        so a property the walk reads must be registered BEFORE the walk.
        """
        assert (
            S.windows_ffi.UIA_SelectionItemIsSelectedPropertyId
            in S.windows_ffi.CACHED_WALK_PROPERTIES
        )


class TestAdvertisedActions:
    """``ElementRec.actions`` — the channel the shipped skill tells the model to read.

    It was hardcoded empty on Windows while macOS populated it, so the model had to
    ATTEMPT a verb to discover whether an element supports it — and a refusal is
    indistinguishable from having addressed the wrong element.
    """

    @staticmethod
    def _available(monkeypatch, *available: int) -> "tuple[str, ...]":
        monkeypatch.setattr(
            S.windows_ffi, "cached_prop", lambda elem, pid: True if pid in available else None
        )
        return S._actions(_FakeElem({}), secure=False)

    def test_an_invokable_element_advertises_press(self, monkeypatch) -> None:
        actions = self._available(monkeypatch, S.windows_ffi.UIA_IsInvokePatternAvailablePropertyId)
        assert actions == (S.WINDOWS_ACTION_PRESS,)

    def test_a_checkbox_advertises_both_press_and_toggle(self, monkeypatch) -> None:
        """Measured on a real WinForms ``CheckBox``: it implements both patterns."""
        actions = self._available(
            monkeypatch,
            S.windows_ffi.UIA_IsInvokePatternAvailablePropertyId,
            S.windows_ffi.UIA_IsTogglePatternAvailablePropertyId,
        )
        assert actions == (S.WINDOWS_ACTION_PRESS, S.WINDOWS_ACTION_TOGGLE)

    def test_expand_collapse_advertises_BOTH_directions(self, monkeypatch) -> None:
        """One pattern serves both, and which is applicable is the ``expanded``
        trait's job rather than this list's."""
        actions = self._available(
            monkeypatch, S.windows_ffi.UIA_IsExpandCollapsePatternAvailablePropertyId
        )
        assert actions == (S.WINDOWS_ACTION_EXPAND, S.WINDOWS_ACTION_COLLAPSE)

    def test_an_element_with_no_patterns_advertises_nothing(self, monkeypatch) -> None:
        assert self._available(monkeypatch) == ()

    def test_only_a_definite_TRUE_advertises(self, monkeypatch) -> None:
        """A provider that does not implement the property answers the reserved
        not-supported sentinel. Treating that as True would advertise a verb the
        element then refuses, which is worse than advertising nothing.
        """
        monkeypatch.setattr(S.windows_ffi, "cached_prop", lambda elem, pid: None)
        assert S._actions(_FakeElem({}), secure=False) == ()
        monkeypatch.setattr(S.windows_ffi, "cached_prop", lambda elem, pid: False)
        assert S._actions(_FakeElem({}), secure=False) == ()

    def test_a_secure_element_advertises_NOTHING(self, monkeypatch) -> None:
        """``press`` on a masked field would confirm the control is interactive, part
        of what the redaction exists to withhold."""
        monkeypatch.setattr(S.windows_ffi, "cached_prop", lambda elem, pid: True)
        assert S._actions(_FakeElem({}), secure=True) == ()

    def test_every_advertised_name_is_one_the_driver_SERVES(self) -> None:
        """**The invariant that makes the list trustworthy.**

        A name published here but not accepted by ``perform_action`` is a refusal the
        model was invited to trigger. Both ends read the same tuple in ``types``, and
        this pins that they have not drifted apart.
        """
        published = {name for _pid, names in S._PATTERN_ACTIONS for name in names}
        assert published <= set(WINDOWS_SUPPORTED_ACTIONS)

    def test_every_availability_property_is_in_the_cached_walk(self) -> None:
        """A live ``GetCurrentPattern`` probe is a cross-process round trip PER
        element — the cost the cached walk exists to avoid — so these must ride along
        in the walk that already happened."""
        for prop_id, _names in S._PATTERN_ACTIONS:
            assert prop_id in S.windows_ffi.CACHED_WALK_PROPERTIES


class TestExpandedTrait:
    """``expanded`` is the parity counterpart of the macOS walk's ``AXDisclosing``.

    Without it a model cannot tell an open tree node from a closed one, so it spends
    a turn expanding something already open — and, worse, reads a ``collapse`` refusal
    as though the node were not expandable.
    """

    @staticmethod
    def _state(monkeypatch, value) -> "tuple[str, ...]":
        prop = S.windows_ffi.UIA_ExpandCollapseExpandCollapseStatePropertyId
        monkeypatch.setattr(
            S.windows_ffi, "cached_prop", lambda elem, pid: value if pid == prop else None
        )
        return S._traits(_FakeElem({}), "TreeItem", secure=False)

    def test_an_expanded_node_carries_the_trait(self, monkeypatch) -> None:
        traits = self._state(monkeypatch, S.windows_ffi.ExpandCollapseState_Expanded)
        assert S.TRAIT_EXPANDED in traits

    @pytest.mark.parametrize(
        ("state", "why"),
        [
            ("Collapsed", "a closed node is not open"),
            ("PartiallyExpanded", "partly-realized children are not an open node"),
            ("LeafNode", "the control cannot expand at all"),
        ],
    )
    def test_every_other_state_is_NOT_expanded(self, monkeypatch, state: str, why: str) -> None:
        """Publishing any of these would misdescribe what a collapse would do."""
        value = getattr(S.windows_ffi, f"ExpandCollapseState_{state}")
        assert S.TRAIT_EXPANDED not in self._state(monkeypatch, value), why

    def test_a_provider_without_the_pattern_gets_no_trait(self, monkeypatch) -> None:
        """The not-supported sentinel must not read as a state.

        Measured on a real window: every non-expandable element — buttons, the title
        bar, an Edit — answered ``VT_UNKNOWN`` for this property while two TreeView
        nodes answered ``Collapsed`` and ``Expanded``.
        """
        assert S.TRAIT_EXPANDED not in self._state(monkeypatch, None)

    def test_a_secure_element_never_reports_expanded(self, monkeypatch) -> None:
        prop = S.windows_ffi.UIA_ExpandCollapseExpandCollapseStatePropertyId
        monkeypatch.setattr(
            S.windows_ffi,
            "cached_prop",
            lambda elem, pid: (S.windows_ffi.ExpandCollapseState_Expanded if pid == prop else None),
        )
        assert S._traits(_FakeElem({}), "TreeItem", secure=True) == ()

    def test_the_state_property_is_in_the_cached_walk(self) -> None:
        """A cache miss would make the trait silently absent on every node."""
        assert (
            S.windows_ffi.UIA_ExpandCollapseExpandCollapseStatePropertyId
            in S.windows_ffi.CACHED_WALK_PROPERTIES
        )


class TestRoleMapping:
    def test_a_known_control_type_maps_to_its_role(self) -> None:
        assert S._role_for(50004) == "Edit"
        assert S._role_for(50000) == "Button"

    def test_an_unmapped_type_is_the_unknown_role(self) -> None:
        assert S._role_for(99999) == S._UNKNOWN_ROLE
        assert S._role_for(None) == S._UNKNOWN_ROLE

    def test_an_elidable_role_with_no_content_is_dropped(self) -> None:
        assert S._is_elidable("Pane", "", "") is True
        # A named pane carries information and is kept.
        assert S._is_elidable("Pane", "Sidebar", "") is False
        # A non-structural role is always kept.
        assert S._is_elidable("Button", "", "") is False


class TestBuildSnapshot:
    """The walk-to-records pass, driven over a fake element tree.

    ``build_snapshot`` is the function every observation goes through, and the
    properties worth pinning are the ones a consumer downstream depends on rather than
    the shape of the loop: dense indices, an honest truncation flag, the secure floor,
    and a window that vanished mid-walk becoming a refusal instead of a partial tree.
    """

    @staticmethod
    def _tree(
        monkeypatch,
        elements,
        *,
        props,
        truncated=False,
        depth_truncated=False,
        origin=(10.0, 20.0, 800.0, 600.0),
        live=True,
        secure=None,
    ):
        """Install a fake walk whose per-element properties the test supplies.

        *props* maps an element to ``{property_id: value}``; *secure* maps an element to
        its secure verdict (default: nothing is secure).
        """
        import contextlib

        walk = windows_ffi.WalkResult(
            elements=list(elements),
            depths=[1] * len(elements),
            truncated=truncated,
            depth_truncated=depth_truncated,
        )
        released: list = []
        monkeypatch.setattr(windows_ffi, "dpi_awareness_scope", contextlib.nullcontext)
        monkeypatch.setattr(windows_ffi, "window_is_live", lambda h: live)
        monkeypatch.setattr(windows_ffi, "window_bounds", lambda h: origin)
        monkeypatch.setattr(windows_ffi, "window_text", lambda h: "The Window")
        monkeypatch.setattr(windows_ffi, "element_from_hwnd", lambda h: "ROOT")
        monkeypatch.setattr(windows_ffi, "create_cache_request", lambda **k: "CACHE")
        monkeypatch.setattr(windows_ffi, "walk_bounded", lambda *a, **k: walk)
        monkeypatch.setattr(windows_ffi, "owned", lambda p: contextlib.nullcontext(p))
        monkeypatch.setattr(windows_ffi, "cached_prop", lambda e, pid: props.get(e, {}).get(pid))
        monkeypatch.setattr(S, "_secure_for_role", lambda e, r: (secure or {}).get(e, False))
        monkeypatch.setattr(windows_ffi, "release_all", lambda ps: released.extend(ps))
        monkeypatch.setattr(windows_ffi, "add_ref", lambda p: None)
        monkeypatch.setattr(windows_ffi, "release", lambda p: None)
        return released

    @staticmethod
    def _req(**kw):
        base = dict(max_nodes=100, max_depth=10, text_limit=40, want_image=False)
        base.update(kw)
        return SnapshotRequest(**base)

    _APP = AppRef(name="app", pid=1, bundle_id="app.exe", window_id=0x77, window_title="T")
    _CT = 30003  # UIA_ControlTypePropertyId
    _NAME = 30005
    _VALUE = 30045
    _RECT = 30001
    _ENABLED = 30010
    _FOCUS = 30008

    def test_indices_are_DENSE_across_elided_wrappers(self, monkeypatch) -> None:
        """A structural wrapper is dropped WITHOUT consuming an index, so the numbering
        a model addresses stays compact — and ``index.resolve`` looks a record up by its
        ``index`` field, so position and index are not interchangeable."""
        props = {
            "a": {self._CT: 50000, self._NAME: "Save"},  # Button, kept
            "pane": {self._CT: 50033, self._NAME: "", self._VALUE: ""},  # Pane, elided
            "b": {self._CT: 50004, self._NAME: "Field"},  # Edit, kept
        }
        self._tree(monkeypatch, ["a", "pane", "b"], props=props)
        snap = S.build_snapshot(self._APP, self._req())
        assert [r.index for r in snap.elements] == [0, 1]
        assert [r.title for r in snap.elements] == ["Save", "Field"]

    def test_a_NAMED_wrapper_is_kept(self, monkeypatch) -> None:
        """A named pane carries information a model can act on."""
        props = {"p": {self._CT: 50033, self._NAME: "Sidebar", self._VALUE: ""}}
        self._tree(monkeypatch, ["p"], props=props)
        assert len(S.build_snapshot(self._APP, self._req()).elements) == 1

    def test_a_secure_element_has_its_value_and_frame_WITHHELD(self, monkeypatch) -> None:
        """The rendered glyphs are a credential even though the tree redacted the value,
        and a frame would let a coordinate click aim at the field."""
        props = {
            "s": {
                self._CT: 50004,
                self._NAME: "Password",
                self._VALUE: "hunter2",
                self._RECT: [100.0, 50.0, 200.0, 30.0],
            }
        }
        self._tree(monkeypatch, ["s"], props=props, secure={"s": True})
        snap = S.build_snapshot(self._APP, self._req())
        (rec,) = snap.elements
        assert rec.secure is True
        assert rec.value == "", "the masked value must not be published"
        assert rec.frame is None, "a frame would let a click aim at the field"
        assert rec.traits == (), "editable would confirm the box accepts input"
        assert snap.has_secure is True

    def test_a_secure_node_is_KEPT_even_when_its_role_is_elidable(self, monkeypatch) -> None:
        """**A suppression with no visible cause is a dead end, not a floor.**

        ``secure`` flips ``has_secure``, which suppresses the whole-window screenshot.
        Eliding the node as well left a window with NO tree and NO pixels, explained
        only as "this window contains a secure (password) field" — and that combination
        is reachable with no password at all: ``Custom`` is both an elidable role and
        one where an inconclusive secure read fails closed, so a bridge-less Java/Qt
        app or a game canvas produces exactly this shape. The model was then told a
        canvas holds a password, and the skill's documented response to that message is
        to ask the user to type it themselves.

        The floor is unchanged — the read still fails closed and the screenshot is
        still withheld — but the node stays visible, so the suppression has a stated
        cause and there is something to address.
        """
        props = {"c": {self._CT: 50025, self._NAME: "", self._VALUE: ""}}  # Custom
        self._tree(monkeypatch, ["c"], props=props, secure={"c": True})
        snap = S.build_snapshot(self._APP, self._req())
        assert len(snap.elements) == 1, "a secure node was elided into an empty tree"
        assert snap.elements[0].secure is True
        assert snap.has_secure is True

    def test_an_unnamed_NON_secure_wrapper_is_still_elided(self, monkeypatch) -> None:
        """The exception is narrow: ordinary structural wrappers still cost no index."""
        props = {"c": {self._CT: 50025, self._NAME: "", self._VALUE: ""}}
        self._tree(monkeypatch, ["c"], props=props)
        snap = S.build_snapshot(self._APP, self._req())
        assert snap.elements == ()
        assert snap.has_secure is False

    def test_BOTH_walks_apply_the_SAME_elision_rule(self) -> None:
        """**An index only means anything if both walks number identically.**

        ``build_snapshot`` assigns the numbering the model addresses; ``addressed``
        re-walks to resolve one of those numbers. Any predicate that differs between
        them shifts every later index by one, so ``rec.index`` resolves to a DIFFERENT
        control — and the only thing that would notice is the role+name check, which
        passes whenever the neighbour happens to share both.

        Asserted structurally over the source rather than through a fake tree: this
        broke by one call site keeping a default argument the other passed, which no
        single-element fixture can reproduce. Both sites must supply ``secure=``.
        """
        import inspect

        source = inspect.getsource(S)
        calls = [ln.strip() for ln in source.splitlines() if "_is_elidable(" in ln]
        # One definition plus two call sites; the docstring reference carries no "(".
        invocations = [c for c in calls if not c.startswith("def ")]
        assert len(invocations) == 2, f"unexpected _is_elidable call sites: {invocations}"
        for call in invocations:
            assert "secure=" in call, f"call site omits the secure argument: {call}"

    def test_frames_are_WINDOW_LOCAL(self, monkeypatch) -> None:
        props = {
            "a": {
                self._CT: 50000,
                self._NAME: "B",
                self._RECT: [110.0, 70.0, 200.0, 30.0],
            }
        }
        self._tree(monkeypatch, ["a"], props=props, origin=(10.0, 20.0, 800.0, 600.0))
        (rec,) = S.build_snapshot(self._APP, self._req()).elements
        assert rec.frame == (100.0, 50.0, 200.0, 30.0)

    def test_the_truncation_flags_are_CARRIED_THROUGH(self, monkeypatch) -> None:
        """They are not diagnostics: the capture gate refuses a screenshot on either,
        because a silently-dropped subtree makes ``has_secure`` mean "none seen"."""
        props = {"a": {self._CT: 50000, self._NAME: "B"}}
        self._tree(monkeypatch, ["a"], props=props, truncated=True, depth_truncated=True)
        snap = S.build_snapshot(self._APP, self._req())
        assert snap.truncated is True
        assert snap.depth_truncated is True

    def test_enabled_defaults_TRUE_on_an_unreadable_flag(self, monkeypatch) -> None:
        """Only an explicit False disables: a provider that does not implement the
        property would otherwise make every control look unusable."""
        props = {"a": {self._CT: 50000, self._NAME: "B"}}  # no IsEnabled at all
        self._tree(monkeypatch, ["a"], props=props)
        (rec,) = S.build_snapshot(self._APP, self._req()).elements
        assert rec.enabled is True

    def test_an_explicit_false_disables(self, monkeypatch) -> None:
        props = {"a": {self._CT: 50000, self._NAME: "B", self._ENABLED: False}}
        self._tree(monkeypatch, ["a"], props=props)
        (rec,) = S.build_snapshot(self._APP, self._req()).elements
        assert rec.enabled is False

    def test_the_focused_element_is_marked(self, monkeypatch) -> None:
        props = {
            "a": {self._CT: 50004, self._NAME: "F", self._FOCUS: True},
            "b": {self._CT: 50004, self._NAME: "G"},
        }
        self._tree(monkeypatch, ["a", "b"], props=props)
        recs = S.build_snapshot(self._APP, self._req()).elements
        assert [r.focused for r in recs] == [True, False]

    def test_text_is_CLIPPED_to_the_caller_budget(self, monkeypatch) -> None:
        props = {"a": {self._CT: 50004, self._NAME: "x" * 500}}
        self._tree(monkeypatch, ["a"], props=props)
        (rec,) = S.build_snapshot(self._APP, self._req(text_limit=10)).elements
        assert len(rec.title) == 10

    def test_newlines_are_FLATTENED(self, monkeypatch) -> None:
        """A record is one tree line, so an embedded newline would break the rendering
        the model reads."""
        props = {"a": {self._CT: 50004, self._NAME: "one\r\ntwo\nthree"}}
        self._tree(monkeypatch, ["a"], props=props)
        (rec,) = S.build_snapshot(self._APP, self._req()).elements
        assert "\n" not in rec.title and "\r" not in rec.title

    def test_a_VANISHED_window_is_a_refusal_not_an_empty_tree(self, monkeypatch) -> None:
        """An empty tree would read as "this app has no controls"; the refusal names
        ``computer_list_apps`` so the model's next move is obvious."""
        self._tree(monkeypatch, [], props={}, live=False)
        with pytest.raises(ComputerUseError):
            S.build_snapshot(self._APP, self._req())

    def test_every_walked_reference_is_RELEASED(self, monkeypatch) -> None:
        """A walk mints one COM reference per node; leaking them is a real RSS bug."""
        props = {e: {self._CT: 50000, self._NAME: e} for e in ("a", "b", "c")}
        released = self._tree(monkeypatch, ["a", "b", "c"], props=props)
        S.build_snapshot(self._APP, self._req())
        assert set(released) >= {"a", "b", "c"}

    def test_the_window_ORIGIN_is_published(self, monkeypatch) -> None:
        """Frames are relative to it, so a consumer cannot interpret them without it."""
        props = {"a": {self._CT: 50000, self._NAME: "B"}}
        self._tree(monkeypatch, ["a"], props=props, origin=(5.0, 6.0, 7.0, 8.0))
        assert S.build_snapshot(self._APP, self._req()).window_bounds == (5.0, 6.0, 7.0, 8.0)

    def test_the_LIVE_window_title_wins_over_the_stale_AppRef(self, monkeypatch) -> None:
        """The title changes with the open document, so the record the model reads
        should be the current one."""
        props = {"a": {self._CT: 50000, self._NAME: "B"}}
        self._tree(monkeypatch, ["a"], props=props)
        assert S.build_snapshot(self._APP, self._req()).window_title == "The Window"

    def test_the_subrole_is_EMPTY_rather_than_invented(self, monkeypatch) -> None:
        """UIA has no subrole, and ``render.fingerprint`` includes it — a fabricated
        value would make drift detection depend on our own invention."""
        props = {"a": {self._CT: 50000, self._NAME: "B"}}
        self._tree(monkeypatch, ["a"], props=props)
        (rec,) = S.build_snapshot(self._APP, self._req()).elements
        assert rec.subrole == ""

    def test_actions_are_resolved_at_ACTION_time_not_here(self, monkeypatch) -> None:
        """A pattern probe is a live round trip per element, which is exactly the cost
        the cached walk exists to avoid."""
        props = {"a": {self._CT: 50000, self._NAME: "B"}}
        self._tree(monkeypatch, ["a"], props=props)
        (rec,) = S.build_snapshot(self._APP, self._req()).elements
        assert rec.actions == ()

    def test_refresh_fingerprints_is_a_FULL_walk(self, monkeypatch) -> None:
        """An index only means anything relative to a complete walk, so verifying
        "index 7 is still Save" requires reproducing the numbering that produced 7."""
        props = {"a": {self._CT: 50000, self._NAME: "B"}}
        self._tree(monkeypatch, ["a"], props=props)
        snap = S.refresh_fingerprints(self._APP, self._req())
        assert [r.title for r in snap.elements] == ["B"]

    def test_captured_at_is_MONOTONIC(self, monkeypatch) -> None:
        """A clock adjustment must not make a stale snapshot look fresh."""
        props = {"a": {self._CT: 50000, self._NAME: "B"}}
        self._tree(monkeypatch, ["a"], props=props)
        monkeypatch.setattr(S.time, "monotonic", lambda: 1234.5)
        assert S.build_snapshot(self._APP, self._req()).captured_at == 1234.5
