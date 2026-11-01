"""``computer_use.apps_linux`` — AT-SPI/X11 identity, resolution, confinement.

Runs on every platform with no DISPLAY and no native library: ``apps_linux``
reaches the OS only through :mod:`linux_ffi`, and ``fake_linux_ffi`` replaces every
contract function on that module per test with a loud stub, so nothing here can
reach native code; each test wires the functions it exercises.

Two of these carry security weight rather than convenience:

* **``window_owns_point`` is the confinement floor** for every pointer gesture and
  fails CLOSED: refusing a legitimate click costs one clear refusal, permitting a
  mis-aimed one is an irreversible action in an app the operator never authorized.
* **The join is on pid**, and a window-less daemon (``gsd-*``) is dropped because
  it has no SHOWING frame — not listed with an empty window.
"""

from __future__ import annotations

import pytest
from fake_linux_ffi import _isolated_linux_ffi

from kiro_crew.computer_use import apps_linux
from kiro_crew.computer_use import linux_ffi as F
from kiro_crew.computer_use.types import AppRef, ComputerUseError

# Pytest discovers the autouse fixture through this module-level name.
isolated_linux_ffi = _isolated_linux_ffi


# ── A tiny in-memory AT-SPI world the fake ffi walks ──


class Node:
    """One accessible object: role, name, states, children, pid."""

    _next = 1000

    def __init__(self, *, role, name="", states=(), children=(), pid=0, extents=None):
        self.handle = Node._next
        Node._next += 1
        self.role = role
        self._name = name
        self.states = frozenset(states)
        self.children = list(children)
        self.pid = pid
        self.extents = extents


class World:
    """A desktop plus an X11 window stack, wired into the fake ffi for one test."""

    def __init__(
        self,
        desktop_children,
        *,
        windows=(),
        window_pids=None,
        window_titles=None,
        viewable=None,
        window_geom=None,
        window_at=None,
        screen=(3200, 600),
    ):
        self.nodes: dict[int, Node] = {}
        self.desktop = Node(role=F.ATSPI_ROLE_APPLICATION, name="desktop")
        self.desktop.children = list(desktop_children)
        self._register(self.desktop)
        for child in desktop_children:
            self._register_tree(child)
        self.windows = tuple(windows)
        self.window_pids = window_pids or {}
        self.window_titles = window_titles or {}
        self.viewable = viewable if viewable is not None else {w: True for w in windows}
        self.window_geom = window_geom or {}
        self.window_at = window_at or {}
        self.screen = screen

    def _register(self, node):
        self.nodes[node.handle] = node

    def _register_tree(self, node):
        self._register(node)
        for c in node.children:
            self._register_tree(c)

    def install(self, monkeypatch):
        released: list[int] = []
        monkeypatch.setattr(F, "desktop", lambda: self.desktop.handle)
        monkeypatch.setattr(F, "child_count", lambda h: len(self.nodes[h].children))
        monkeypatch.setattr(
            F,
            "child_at",
            lambda h, i: (
                self.nodes[h].children[i].handle if i < len(self.nodes[h].children) else None
            ),
        )
        monkeypatch.setattr(F, "role", lambda h: self.nodes[h].role)
        monkeypatch.setattr(F, "name", lambda h: self.nodes[h]._name)
        monkeypatch.setattr(F, "pid", lambda h: self.nodes[h].pid)
        monkeypatch.setattr(F, "has_state", lambda h, s: s in self.nodes[h].states)
        monkeypatch.setattr(F, "unref", lambda h: released.append(h))
        monkeypatch.setattr(F, "x_client_list", lambda: self.windows)
        monkeypatch.setattr(F, "x_window_pid", lambda w: self.window_pids.get(w))
        monkeypatch.setattr(F, "x_window_title", lambda w: self.window_titles.get(w, ""))
        monkeypatch.setattr(F, "x_window_is_viewable", lambda w: self.viewable.get(w, False))
        monkeypatch.setattr(F, "x_window_geometry", lambda w: self.window_geom.get(w))
        monkeypatch.setattr(F, "x_window_at", lambda x, y: self.window_at.get((x, y)))
        monkeypatch.setattr(F, "x_screen_size", lambda: self.screen)
        return released


def _frame(title, *, active=True, showing=True, role=None):
    states = []
    if showing:
        states.append(F.ATSPI_STATE_SHOWING)
    if active:
        states.append(F.ATSPI_STATE_ACTIVE)
    return Node(role=role if role is not None else F.ATSPI_ROLE_FRAME, name=title, states=states)


def _app(name, pid, frames):
    return Node(role=F.ATSPI_ROLE_APPLICATION, name=name, children=frames, pid=pid)


@pytest.fixture(autouse=True)
def _proc_exe(monkeypatch):
    """Make ``/proc/<pid>/exe`` deterministic: pid N -> ``/usr/bin/app<N>``."""
    monkeypatch.setattr(apps_linux.os, "readlink", lambda p: f"/usr/bin/app{p.split('/')[2]}")


class TestListApps:
    def test_a_windowless_daemon_is_dropped(self, monkeypatch) -> None:
        """An app with zero SHOWING frames is not 'an app with a window'.

        This is what drops the gsd-* daemons: they appear as desktop children with
        no frame child, exactly like ``gnome-shell``'s background helpers.
        """
        has_window = _app("Calculator", 100, [_frame("Calculator")])
        daemon = _app("gsd-power", 200, [])  # no frames at all
        world = World(
            [has_window, daemon],
            windows=(0x1,),
            window_pids={0x1: 100},
            window_titles={0x1: "Calculator"},
        )
        world.install(monkeypatch)
        apps = apps_linux.list_apps()
        assert [a.name for a in apps] == ["Calculator"]

    def test_a_showing_but_inactive_frame_still_counts(self, monkeypatch) -> None:
        """A visible-but-unfocused window is a real on-screen surface."""
        app = _app("Bg", 100, [_frame("Bg", active=False, showing=True)])
        world = World([app], windows=(0x1,), window_pids={0x1: 100}, window_titles={0x1: "Bg"})
        world.install(monkeypatch)
        assert [a.name for a in apps_linux.list_apps()] == ["Bg"]

    def test_a_non_showing_frame_is_dropped(self, monkeypatch) -> None:
        app = _app("Hidden", 100, [_frame("Hidden", showing=False)])
        World([app], windows=()).install(monkeypatch)
        assert apps_linux.list_apps() == ()

    def test_bundle_id_is_the_proc_exe_basename(self, monkeypatch) -> None:
        app = _app("Calc", 100, [_frame("Calc")])
        World([app], windows=(0x1,), window_pids={0x1: 100}, window_titles={0x1: "Calc"}).install(
            monkeypatch
        )
        (ref,) = apps_linux.list_apps()
        assert ref.bundle_id == "app100"  # basename of /usr/bin/app100
        assert ref.pid == 100

    def test_window_id_matches_on_pid_AND_title(self, monkeypatch) -> None:
        """An app with two X windows gets the one whose title is its active frame."""
        app = _app("Editor", 100, [_frame("doc.txt - Editor")])
        world = World(
            [app],
            windows=(0x10, 0x11),
            window_pids={0x10: 100, 0x11: 100},
            window_titles={0x10: "palette", 0x11: "doc.txt - Editor"},
        )
        world.install(monkeypatch)
        (ref,) = apps_linux.list_apps()
        assert ref.window_id == 0x11

    def test_a_single_viewable_window_matches_without_a_title(self, monkeypatch) -> None:
        app = _app("App", 100, [_frame("")])  # empty frame name
        world = World(
            [app], windows=(0x20,), window_pids={0x20: 100}, window_titles={0x20: "something else"}
        )
        world.install(monkeypatch)
        (ref,) = apps_linux.list_apps()
        assert ref.window_id == 0x20

    def test_no_x_match_yields_window_id_zero(self, monkeypatch) -> None:
        """A usable state: the tree still reads, and a gesture is refused."""
        app = _app("NoWin", 100, [_frame("NoWin")])
        world = World([app], windows=(0x30,), window_pids={0x30: 999})  # different pid
        world.install(monkeypatch)
        (ref,) = apps_linux.list_apps()
        assert ref.window_id == 0

    def test_order_is_x_stacking_topmost_first(self, monkeypatch) -> None:
        """The window the user is looking at (topmost) is the first a model reads."""
        a = _app("Aaa", 100, [_frame("Aaa")])
        b = _app("Bbb", 200, [_frame("Bbb")])
        # Stacking order: Bbb's window first (topmost), then Aaa's.
        world = World(
            [a, b],
            windows=(0xB, 0xA),
            window_pids={0xB: 200, 0xA: 100},
            window_titles={0xB: "Bbb", 0xA: "Aaa"},
        )
        world.install(monkeypatch)
        assert [r.name for r in apps_linux.list_apps()] == ["Bbb", "Aaa"]

    def test_unmatched_apps_sort_after_matched_then_by_name(self, monkeypatch) -> None:
        matched = _app("Zed", 100, [_frame("Zed")])
        unmatched_b = _app("Beta", 300, [_frame("Beta")])
        unmatched_a = _app("Alpha", 400, [_frame("Alpha")])
        world = World(
            [matched, unmatched_b, unmatched_a],
            windows=(0x1,),
            window_pids={0x1: 100},
            window_titles={0x1: "Zed"},
        )
        world.install(monkeypatch)
        # Zed matched (depth 0), then unmatched alphabetically.
        assert [r.name for r in apps_linux.list_apps()] == ["Zed", "Alpha", "Beta"]

    def test_enumeration_failure_PROPAGATES(self, monkeypatch) -> None:
        """() would read as an empty desktop and point the model at a call that
        just failed."""

        def boom():
            raise ComputerUseError("bus unreachable")

        World([]).install(monkeypatch)
        monkeypatch.setattr(F, "desktop", boom)
        with pytest.raises(ComputerUseError):
            apps_linux.list_apps()


class TestResolveApp:
    @staticmethod
    def _world(monkeypatch):
        calc = _app("Calculator", 100, [_frame("Calculator")])
        editor = _app("Text Editor", 200, [_frame("Text Editor")])
        world = World(
            [calc, editor],
            windows=(0x1, 0x2),
            window_pids={0x1: 100, 0x2: 200},
            window_titles={0x1: "Calculator", 0x2: "Text Editor"},
        )
        world.install(monkeypatch)

    @pytest.mark.parametrize(
        ("query", "expect"),
        [
            ("Calculator", "Calculator"),
            ("calculator", "Calculator"),  # case-insensitive
            ("app100", "Calculator"),  # by bundle id
            ("Text", "Text Editor"),  # prefix
            ("Calc", "Calculator"),  # prefix
        ],
    )
    def test_resolves_expected(self, monkeypatch, query, expect) -> None:
        self._world(monkeypatch)
        assert apps_linux.resolve_app(query).name == expect

    @pytest.mark.parametrize("query", ["", "   ", "nonexistent"])
    def test_no_match_refuses(self, monkeypatch, query) -> None:
        self._world(monkeypatch)
        with pytest.raises(ComputerUseError):
            apps_linux.resolve_app(query)

    def test_an_exact_name_beats_a_prefix_of_another(self, monkeypatch) -> None:
        a = _app("Text", 100, [_frame("Text")])
        b = _app("Text Editor", 200, [_frame("Text Editor")])
        world = World(
            [a, b],
            windows=(0x1, 0x2),
            window_pids={0x1: 100, 0x2: 200},
            window_titles={0x1: "Text", 0x2: "Text Editor"},
        )
        world.install(monkeypatch)
        assert apps_linux.resolve_app("Text").name == "Text"


class TestWindowOwnsPoint:
    """THE confinement floor — fails closed on every uncertainty."""

    _APP = AppRef(name="t", pid=42, bundle_id="t", window_id=0x500, window_title="T")

    def test_the_authorized_window_owns_its_point(self, monkeypatch) -> None:
        monkeypatch.setattr(F, "x_window_at", lambda x, y: 0x500)
        assert apps_linux.window_owns_point(self._APP, 10, 20) is True

    def test_a_different_window_is_refused(self, monkeypatch) -> None:
        """The app-A-authorized / app-B-clicked hole this closes."""
        monkeypatch.setattr(F, "x_window_at", lambda x, y: 0x999)
        assert apps_linux.window_owns_point(self._APP, 10, 20) is False

    def test_a_point_owned_by_nobody_is_refused(self, monkeypatch) -> None:
        monkeypatch.setattr(F, "x_window_at", lambda x, y: None)
        assert apps_linux.window_owns_point(self._APP, 10, 20) is False

    def test_a_zero_window_id_is_refused_without_asking_x(self, monkeypatch) -> None:
        def boom(x, y):  # pragma: no cover - must not be reached
            raise AssertionError("asked X about a window that does not exist")

        monkeypatch.setattr(F, "x_window_at", boom)
        app = AppRef(name="t", pid=1, bundle_id="t", window_id=0, window_title="T")
        assert apps_linux.window_owns_point(app, 1, 1) is False

    def test_a_typed_refusal_from_the_ffi_surfaces_verbatim(self, monkeypatch) -> None:
        """A ``ComputerUseError`` is the ffi's pre-native typed refusal (``no X display``
        on an unavailable host). It must reach the driver's ``_guarded`` as itself, so
        the model reads the real reason — not "another window owns the point" plus a
        "bring it to the front" remedy on a host with no display. (Regression judge.)"""

        def boom(x, y):
            raise ComputerUseError("no X display (DISPLAY unset)")

        monkeypatch.setattr(F, "x_window_at", boom)
        with pytest.raises(ComputerUseError, match="no X display"):
            apps_linux.window_owns_point(self._APP, 10, 20)

    def test_any_other_error_in_the_lookup_is_refused(self, monkeypatch) -> None:
        """Everything that is NOT the typed refusal still fails closed."""

        def boom(x, y):
            raise RuntimeError("X said no")

        monkeypatch.setattr(F, "x_window_at", boom)
        assert apps_linux.window_owns_point(self._APP, 10, 20) is False


class TestWindowBounds:
    _APP = AppRef(name="t", pid=1, bundle_id="t", window_id=9, window_title="T")

    def test_a_live_window_reports_its_rect(self, monkeypatch) -> None:
        """A server-side-decorated window (no ``_GTK_FRAME_EXTENTS``) is its own content."""
        monkeypatch.setattr(F, "x_window_geometry", lambda w: (1, 2, 3, 4))
        monkeypatch.setattr(F, "x_gtk_frame_extents", lambda w: None)
        assert apps_linux.window_bounds(self._APP) == (1, 2, 3, 4)

    def test_a_csd_window_reports_its_CONTENT_rect(self, monkeypatch) -> None:
        """The measured gnome-calculator case: X geometry ``482x748`` at ``(-61, -23)``
        with ``_GTK_FRAME_EXTENTS = 61, 61, 55, 67`` is a ``360x626`` content rect at
        ``(0, 32)`` — the rect AT-SPI's WINDOW-coordinate frames are measured in."""
        monkeypatch.setattr(F, "x_window_geometry", lambda w: (-61, -23, 482, 748))
        monkeypatch.setattr(F, "x_gtk_frame_extents", lambda w: (61, 61, 55, 67))
        assert apps_linux.window_bounds(self._APP) == (0.0, 32.0, 360.0, 626.0)

    def test_degenerate_frame_extents_fall_back_to_the_geometry(self, monkeypatch) -> None:
        """Margins that would leave no content are ignored, not applied into a negative rect."""
        monkeypatch.setattr(F, "x_window_geometry", lambda w: (0, 0, 100, 50))
        monkeypatch.setattr(F, "x_gtk_frame_extents", lambda w: (60, 60, 10, 10))
        assert apps_linux.window_bounds(self._APP) == (0.0, 0.0, 100.0, 50.0)

    def test_a_zero_window_id_is_none_without_asking(self, monkeypatch) -> None:
        def boom(w):  # pragma: no cover
            raise AssertionError("asked X about window 0")

        monkeypatch.setattr(F, "x_window_geometry", boom)
        app = AppRef(name="t", pid=1, bundle_id="t", window_id=0, window_title="T")
        assert apps_linux.window_bounds(app) is None

    def test_a_raising_read_degrades_to_none(self, monkeypatch) -> None:
        def boom(w):
            raise ComputerUseError("gone")

        monkeypatch.setattr(F, "x_window_geometry", boom)
        assert apps_linux.window_bounds(self._APP) is None


class TestApplicationFor:
    def test_it_finds_the_app_by_pid_and_releases_the_rest(self, monkeypatch) -> None:
        a = _app("A", 100, [_frame("A")])
        b = _app("B", 200, [_frame("B")])
        world = World([a, b])
        released = world.install(monkeypatch)
        ref = AppRef(name="B", pid=200, bundle_id="b", window_id=0, window_title="B")
        found = apps_linux.application_for(ref)
        assert found == b.handle
        # The non-matching app A and the desktop are released; the match is NOT.
        assert a.handle in released
        assert b.handle not in released

    def test_a_vanished_app_is_none(self, monkeypatch) -> None:
        a = _app("A", 100, [_frame("A")])
        World([a]).install(monkeypatch)
        ref = AppRef(name="X", pid=999, bundle_id="x", window_id=0, window_title="X")
        assert apps_linux.application_for(ref) is None


class TestImportSafety:
    """``apps_linux`` must import with no native library present.

    The real ffi lands on a sibling branch; the module must not touch ctypes or a
    display at import. This asserts the module object exists and exposes its public
    surface without any native call having run (the fake ffi provides only data
    constants until a test wires behaviour).
    """

    def test_the_module_imported_without_a_native_call(self) -> None:
        for name in ("list_apps", "resolve_app", "window_owns_point", "application_for"):
            assert callable(getattr(apps_linux, name))
