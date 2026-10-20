"""``computer_use.snapshot_linux`` — the AT-SPI walk-to-records transforms.

Runs with no DISPLAY and no native library: the walk is driven over a fake
in-memory AT-SPI tree wired into the fake :mod:`linux_ffi`. These execute the
seams a reviewer cares about rather than the loop's shape: role-driven secure
(fail closed), dense indices across elided wrappers, the never-elide-a-secure-node
rule, window-local frames, the carried-through truncation flags, and the
re-walk addressing that both resolves an index AND refuses a secure write.
"""

from __future__ import annotations

import pytest
from fake_linux_ffi import _isolated_linux_ffi

from kiro_crew.computer_use import linux_ffi as F
from kiro_crew.computer_use import snapshot_linux as S
from kiro_crew.computer_use.types import (
    TRAIT_EDITABLE,
    TRAIT_EXPANDED,
    TRAIT_SELECTED,
    AppRef,
    ComputerUseError,
    ElementRec,
    SnapshotRequest,
)

# Pytest discovers the autouse fixture through this module-level name.
isolated_linux_ffi = _isolated_linux_ffi


class Node:
    """One accessible object with the properties the walk reads."""

    _next = 2000

    def __init__(
        self,
        *,
        role,
        name="",
        value=None,
        text="",
        states=(),
        children=(),
        extents=None,
        actions=(),
        role_name="",
    ):
        self.handle = Node._next
        Node._next += 1
        self.role = role
        self.name = name
        self.value = value
        self.text = text
        self.states = set(states)
        self.children = list(children)
        self.extents = extents
        self.actions = tuple(actions)
        self.role_name = role_name


def _showing(*extra):
    return {F.ATSPI_STATE_SHOWING, *extra}


class Tree:
    """Wire a fake AT-SPI world (one app, one active frame) into the ffi."""

    def __init__(self, frame, *, origin=(10, 20, 800, 600), app_pid=100, app_name="App"):
        self.nodes: dict[int, Node] = {}
        self.frame = frame
        self._register(frame)
        self.app = Node(role=F.ATSPI_ROLE_APPLICATION, name=app_name, children=[frame])
        self.app.handle = 1  # stable
        self.nodes[1] = self.app
        self.app_pid = app_pid
        self.desktop = Node(role=F.ATSPI_ROLE_APPLICATION, name="desktop", children=[self.app])
        self.desktop.handle = 0
        self.nodes[0] = self.desktop
        self.origin = origin

    def _register(self, node):
        self.nodes[node.handle] = node
        for c in node.children:
            self._register(c)

    def install(self, monkeypatch, *, live=True, roles_fail=()):
        released: list[int] = []
        monkeypatch.setattr(F, "desktop", lambda: 0)
        monkeypatch.setattr(F, "child_count", lambda h: len(self.nodes[h].children))
        monkeypatch.setattr(
            F,
            "child_at",
            lambda h, i: (
                self.nodes[h].children[i].handle if i < len(self.nodes[h].children) else None
            ),
        )

        def role(h):
            if h in roles_fail:
                raise ComputerUseError("role unreadable")
            return self.nodes[h].role

        monkeypatch.setattr(F, "role", role)
        monkeypatch.setattr(F, "role_name", lambda h: self.nodes[h].role_name)
        monkeypatch.setattr(F, "name", lambda h: self.nodes[h].name)
        monkeypatch.setattr(F, "pid", lambda h: self.app_pid if h in (0, 1) else 0)
        monkeypatch.setattr(F, "states", lambda h: frozenset(self.nodes[h].states))
        monkeypatch.setattr(F, "has_state", lambda h, s: s in self.nodes[h].states)

        # Extents are served ONLY for a WINDOW-coordinate read: a SCREEN read is the
        # GTK 4 defect (x = y = 0 for every widget), so a walker asking for it fails
        # loudly here rather than passing with coordinates that are wrong live.
        def _extents(h, coord=F.ATSPI_COORD_SCREEN):
            assert coord == F.ATSPI_COORD_WINDOW, "frames must be read in WINDOW coords"
            return self.nodes[h].extents

        monkeypatch.setattr(F, "extents", _extents)
        monkeypatch.setattr(F, "value", lambda h: self.nodes[h].value)
        monkeypatch.setattr(F, "text", lambda h, limit: self.nodes[h].text[:limit])
        monkeypatch.setattr(F, "actions", lambda h: self.nodes[h].actions)
        monkeypatch.setattr(F, "unref", lambda h: released.append(h))
        monkeypatch.setattr(F, "x_window_geometry", lambda w: self.origin)
        monkeypatch.setattr(F, "x_gtk_frame_extents", lambda w: None)
        # application_for walks the desktop; active_frame reads the app's frames.
        return released


_APP = AppRef(name="App", pid=100, bundle_id="app", window_id=0x5, window_title="T")


def _req(**kw):
    base = dict(max_nodes=100, max_depth=10, text_limit=40, want_image=False)
    base.update(kw)
    return SnapshotRequest(**base)


def _frame(children=(), name="The Window"):
    return Node(
        role=F.ATSPI_ROLE_FRAME,
        name=name,
        states=_showing(F.ATSPI_STATE_ACTIVE),
        children=list(children),
    )


def _btn(name, *, states=None, **kw):
    return Node(
        role=F.ATSPI_ROLE_PUSH_BUTTON,
        name=name,
        states=states if states is not None else _showing(),
        **kw,
    )


def _edit(name, *, states=None, **kw):
    return Node(
        role=F.ATSPI_ROLE_ENTRY,
        name=name,
        states=states if states is not None else _showing(),
        **kw,
    )


def _pane(children=(), name=""):
    return Node(role=F.ATSPI_ROLE_PANEL, name=name, states=_showing(), children=list(children))


class TestRoleFor:
    def test_a_password_role_is_secure(self, monkeypatch) -> None:
        n = Node(role=F.ATSPI_ROLE_PASSWORD_TEXT, name="pw", states=_showing())
        Tree(_frame([n])).install(monkeypatch)
        assert S._role_for(n.handle) == ("Edit", True)

    def test_a_known_role_maps_to_its_human_name(self, monkeypatch) -> None:
        n = _btn("Save")
        Tree(_frame([n])).install(monkeypatch)
        assert S._role_for(n.handle) == ("Button", False)

    def test_an_unreadable_role_FAILS_CLOSED_to_secure(self, monkeypatch) -> None:
        """The security-critical branch: an unreadable role is where a masked field
        could hide, so it is treated as secure."""
        n = _btn("x")
        Tree(_frame([n])).install(monkeypatch, roles_fail={n.handle})
        role, secure = S._role_for(n.handle)
        assert secure is True
        assert role == S._UNKNOWN_ROLE

    def test_an_unmapped_role_uses_the_localised_name(self, monkeypatch) -> None:
        n = Node(role=9999, name="x", states=_showing(), role_name="slider")
        Tree(_frame([n])).install(monkeypatch)
        assert S._role_for(n.handle) == ("Slider", False)


class TestBuildSnapshot:
    def test_indices_are_DENSE_across_elided_wrappers(self, monkeypatch) -> None:
        frame = _frame([_btn("Save"), _pane(name=""), _edit("Field")])
        Tree(frame).install(monkeypatch)
        snap = S.build_snapshot(_APP, _req())
        assert [r.index for r in snap.elements] == [0, 1]
        assert [r.title for r in snap.elements] == ["Save", "Field"]

    def test_a_NAMED_wrapper_is_kept(self, monkeypatch) -> None:
        frame = _frame([_pane(name="Sidebar")])
        Tree(frame).install(monkeypatch)
        assert len(S.build_snapshot(_APP, _req()).elements) == 1

    def test_a_secure_element_has_value_frame_actions_traits_WITHHELD(self, monkeypatch) -> None:
        pw = Node(
            role=F.ATSPI_ROLE_PASSWORD_TEXT,
            name="Password",
            states=_showing(F.ATSPI_STATE_EDITABLE),
            extents=(100, 50, 200, 30),
            actions=("activate",),
        )
        Tree(_frame([pw])).install(monkeypatch)
        snap = S.build_snapshot(_APP, _req())
        (rec,) = snap.elements
        assert rec.secure is True
        assert rec.subrole == S._SECURE_SUBROLE
        assert rec.value == ""
        assert rec.frame is None
        assert rec.actions == ()
        assert rec.traits == ()
        assert snap.has_secure is True

    def test_a_secure_element_s_TEXT_is_never_read(self, monkeypatch) -> None:
        """The Text interface of a password box returns the PLAINTEXT. The record's
        ``value`` is withheld by never calling ``text()`` on it at all — not by reading
        and discarding — because a read that happened is a credential that crossed."""
        pw = Node(
            role=F.ATSPI_ROLE_PASSWORD_TEXT,
            name="Password",
            text="hunter2",
            states=_showing(F.ATSPI_STATE_EDITABLE),
        )
        tree = Tree(_frame([pw]))
        tree.install(monkeypatch)
        reads: list[int] = []

        def spy(h, limit):
            reads.append(h)
            return tree.nodes[h].text[:limit]

        monkeypatch.setattr(F, "text", spy)
        (rec,) = S.build_snapshot(_APP, _req()).elements
        assert rec.secure is True and rec.value == ""
        assert pw.handle not in reads, "text() was called on a secure node"

    def test_value_is_the_TEXT_interface_first(self, monkeypatch) -> None:
        """Measured on gnome-calculator: the result field (role TEXT) carries ``"15"``
        in its Text interface and nothing in Value. A Value-only read showed an empty
        display after every calculation."""
        field = Node(role=F.ATSPI_ROLE_TEXT, text="15", states=_showing())
        Tree(_frame([field])).install(monkeypatch)
        (rec,) = S.build_snapshot(_APP, _req()).elements
        assert rec.role == "Edit" and rec.value == "15"

    def test_value_falls_back_to_the_numeric_Value_interface(self, monkeypatch) -> None:
        slider = Node(role=F.ATSPI_ROLE_PUSH_BUTTON, name="Volume", value=0.5, states=_showing())
        Tree(_frame([slider])).install(monkeypatch)
        (rec,) = S.build_snapshot(_APP, _req()).elements
        assert rec.value == "0.5"

    def test_value_text_honours_the_text_limit(self, monkeypatch) -> None:
        field = Node(role=F.ATSPI_ROLE_TEXT, text="x" * 1000, states=_showing())
        Tree(_frame([field])).install(monkeypatch)
        (rec,) = S.build_snapshot(_APP, _req(text_limit=40)).elements
        assert len(rec.value) == 40

    def test_a_secure_node_is_KEPT_even_when_its_role_is_elidable(self, monkeypatch) -> None:
        """A suppression with no visible cause is a dead end, not a floor.

        A ``PANEL`` (elidable Pane) whose role read FAILS is both elidable-shaped
        and secure (fail-closed), the exact combination that produced an empty tree
        with ``has_secure=True`` on Windows. The node must stay visible so the
        screenshot suppression has a stated cause.
        """
        pane = _pane(name="")  # Pane, empty -> normally elided
        Tree(_frame([pane])).install(monkeypatch, roles_fail={pane.handle})
        snap = S.build_snapshot(_APP, _req())
        assert len(snap.elements) == 1, "a secure node was elided into an empty tree"
        assert snap.elements[0].secure is True
        assert snap.has_secure is True

    def test_an_unnamed_NON_secure_wrapper_is_still_elided(self, monkeypatch) -> None:
        frame = _frame([_pane(name="")])
        Tree(frame).install(monkeypatch)
        snap = S.build_snapshot(_APP, _req())
        assert snap.elements == ()
        assert snap.has_secure is False

    def test_frames_are_WINDOW_LOCAL(self, monkeypatch) -> None:
        """The WINDOW-coordinate read is published verbatim — NOT screen-minus-origin.

        The origin here is deliberately non-zero: under the old arithmetic this frame
        would have come out as ``(100, 50, ...)``, which is what GTK 4's all-zero SCREEN
        extents turned into "every control sits at the window's own origin" live.
        """
        b = _btn("B", extents=(110, 70, 200, 30))
        Tree(_frame([b]), origin=(10, 20, 800, 600)).install(monkeypatch)
        (rec,) = S.build_snapshot(_APP, _req()).elements
        assert rec.frame == (110.0, 70.0, 200.0, 30.0)

    def test_no_origin_yields_no_frame(self, monkeypatch) -> None:
        b = _btn("B", extents=(110, 70, 200, 30))
        tree = Tree(_frame([b]))
        tree.install(monkeypatch)
        monkeypatch.setattr(F, "x_window_geometry", lambda w: None)
        (rec,) = S.build_snapshot(_APP, _req()).elements
        assert rec.frame is None

    def test_the_truncation_flags_are_CARRIED_THROUGH(self, monkeypatch) -> None:
        frame = _frame([_btn("a"), _btn("b"), _btn("c")])
        Tree(frame).install(monkeypatch)
        snap = S.build_snapshot(_APP, _req(max_nodes=2))
        assert snap.truncated is True

    def test_depth_truncation_is_reported(self, monkeypatch) -> None:
        deep = _pane([_pane([_btn("deep")], name="p2")], name="p1")
        Tree(_frame([deep])).install(monkeypatch)
        snap = S.build_snapshot(_APP, _req(max_depth=1))
        assert snap.depth_truncated is True

    def test_enabled_requires_SENSITIVE_and_ENABLED(self, monkeypatch) -> None:
        on = _btn("On", states=_showing(F.ATSPI_STATE_SENSITIVE, F.ATSPI_STATE_ENABLED))
        off = _btn("Off")  # showing only
        Tree(_frame([on, off])).install(monkeypatch)
        recs = S.build_snapshot(_APP, _req()).elements
        assert [r.enabled for r in recs] == [True, False]

    def test_the_focused_element_is_marked(self, monkeypatch) -> None:
        a = _edit("F", states=_showing(F.ATSPI_STATE_FOCUSED))
        b = _edit("G")
        Tree(_frame([a, b])).install(monkeypatch)
        recs = S.build_snapshot(_APP, _req()).elements
        assert [r.focused for r in recs] == [True, False]

    def test_traits_come_from_states(self, monkeypatch) -> None:
        e = _edit(
            "E",
            states=_showing(F.ATSPI_STATE_EDITABLE, F.ATSPI_STATE_SELECTED, F.ATSPI_STATE_EXPANDED),
        )
        Tree(_frame([e])).install(monkeypatch)
        (rec,) = S.build_snapshot(_APP, _req()).elements
        assert set(rec.traits) == {TRAIT_EDITABLE, TRAIT_SELECTED, TRAIT_EXPANDED}

    def test_editable_is_gated_on_a_text_role(self, monkeypatch) -> None:
        """A Button with EDITABLE state must not advertise ``editable``."""
        b = _btn("B", states=_showing(F.ATSPI_STATE_EDITABLE))
        Tree(_frame([b])).install(monkeypatch)
        (rec,) = S.build_snapshot(_APP, _req()).elements
        assert TRAIT_EDITABLE not in rec.traits

    def test_a_non_showing_subtree_is_skipped(self, monkeypatch) -> None:
        """What keeps the walk to the visible window."""
        hidden = _btn("Hidden")
        hidden.states = set()  # not showing
        frame = _frame([_btn("Visible"), hidden])
        Tree(frame).install(monkeypatch)
        recs = S.build_snapshot(_APP, _req()).elements
        assert [r.title for r in recs] == ["Visible"]

    def test_a_defunct_node_is_skipped(self, monkeypatch) -> None:
        dead = _btn("Dead", states={F.ATSPI_STATE_SHOWING, F.ATSPI_STATE_DEFUNCT})
        frame = _frame([_btn("Live"), dead])
        Tree(frame).install(monkeypatch)
        recs = S.build_snapshot(_APP, _req()).elements
        assert [r.title for r in recs] == ["Live"]

    def test_captured_at_is_MONOTONIC(self, monkeypatch) -> None:
        Tree(_frame([_btn("B")])).install(monkeypatch)
        monkeypatch.setattr(
            S.time, "monotonic", lambda: 4242.0
        )  # flake-ok: a fixed clock pins captured_at to a known value; nothing waits on it
        assert S.build_snapshot(_APP, _req()).captured_at == 4242.0

    def test_a_vanished_app_is_a_refusal(self, monkeypatch) -> None:
        tree = Tree(_frame([_btn("B")]))
        tree.install(monkeypatch)
        monkeypatch.setattr(F, "desktop", lambda: 0)
        # Desktop has no children with the app's pid -> application_for None.
        monkeypatch.setattr(F, "child_count", lambda h: 0 if h == 0 else 0)
        with pytest.raises(ComputerUseError):
            S.build_snapshot(_APP, _req())

    def test_the_window_origin_is_published(self, monkeypatch) -> None:
        Tree(_frame([_btn("B")]), origin=(5, 6, 7, 8)).install(monkeypatch)
        assert S.build_snapshot(_APP, _req()).window_bounds == (5, 6, 7, 8)

    def test_depth_is_meaningful(self, monkeypatch) -> None:
        nested = _pane([_btn("child")], name="named")
        Tree(_frame([nested])).install(monkeypatch)
        recs = S.build_snapshot(_APP, _req()).elements
        # The frame is the unrecorded root; its named child pane is depth 0 and the
        # button nested inside it is depth 1.
        by_title = {r.title: r.depth for r in recs}
        assert by_title["named"] == 0
        assert by_title["child"] == 1


class TestElisionParityAcrossWalks:
    def test_BOTH_walks_apply_the_SAME_elision_rule(self) -> None:
        """An index only means anything if both walks number identically.

        ``build_snapshot`` and ``addressed`` must pass ``secure=`` to
        ``_is_elidable`` so a kept secure node does not shift later indices in one
        walk but not the other. Asserted structurally because a single fixture
        cannot reproduce a one-call-site default-argument drift.
        """
        import inspect

        source = inspect.getsource(S)
        calls = [ln.strip() for ln in source.splitlines() if "_is_elidable(" in ln]
        invocations = [c for c in calls if not c.startswith("def ")]
        assert len(invocations) == 2, f"unexpected _is_elidable call sites: {invocations}"
        for call in invocations:
            assert "secure=" in call, f"call site omits the secure argument: {call}"


class TestAddressed:
    def test_it_resolves_the_live_object_at_an_index(self, monkeypatch) -> None:
        save = _btn("Save")
        field = _edit("Field")
        Tree(_frame([save, _pane(name=""), field])).install(monkeypatch)
        rec = ElementRec(index=1, role="Edit", title="Field")
        with S.addressed(_APP, rec, _req()) as obj:
            assert obj == field.handle

    def test_a_role_or_title_mismatch_yields_none(self, monkeypatch) -> None:
        """A toolbar that swapped Save for Delete leaves both Button at this index;
        the title check refuses the wrong one."""
        Tree(_frame([_btn("Delete")])).install(monkeypatch)
        rec = ElementRec(index=0, role="Button", title="Save")
        with S.addressed(_APP, rec, _req()) as obj:
            assert obj is None

    def test_a_missing_index_yields_none(self, monkeypatch) -> None:
        Tree(_frame([_btn("Only")])).install(monkeypatch)
        rec = ElementRec(index=5, role="Button", title="Only")
        with S.addressed(_APP, rec, _req()) as obj:
            assert obj is None

    def test_for_write_on_a_secure_target_RAISES(self, monkeypatch) -> None:
        """The live secure re-read: a field that flipped to password since the walk
        must refuse a write even if the record says plain."""
        pw = Node(role=F.ATSPI_ROLE_PASSWORD_TEXT, name="pw", states=_showing())
        Tree(_frame([pw])).install(monkeypatch)
        # The record the model saw claims a plain Edit at index 0.
        rec = ElementRec(index=0, role="Edit", title="pw", secure=False)
        with pytest.raises(S.SecureTargetError):
            with S.addressed(_APP, rec, _req(), for_write=True):
                pass

    def test_for_write_on_an_unreadable_role_RAISES_fail_closed(self, monkeypatch) -> None:
        n = _edit("field")
        Tree(_frame([n])).install(monkeypatch, roles_fail={n.handle})
        rec = ElementRec(index=0, role=S._UNKNOWN_ROLE, title="field")
        with pytest.raises(S.SecureTargetError):
            with S.addressed(_APP, rec, _req(), for_write=True):
                pass
