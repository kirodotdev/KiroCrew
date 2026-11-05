"""Unit tests for the Linux computer-use driver (no display or bus needed)."""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import pytest

from kiro_crew.computer_use import linux_atspi, linux_driver, linux_input, linux_launch, linux_x11
from kiro_crew.computer_use.linux_atspi import (
    COMPONENT,
    COORD_SCREEN,
    COORD_WINDOW,
    STATE_ACTIVE,
    STATE_EDITABLE,
    STATE_FOCUSED,
    STATE_SHOWING,
    TEXT,
    VALUE,
    Node,
    Ref,
    _states,
)
from kiro_crew.computer_use.types import (
    MAX_TEXT_LIMIT,
    AppRef,
    ClickRequest,
    ComputerUseError,
    ComputerUseUnsupported,
    DragRequest,
    SnapshotRequest,
)

# The driver reads uids, /proc and root-owned paths; off Linux those facts differ,
# and Windows does not collect this file at all (windows-collect-ignore.txt).
pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="Linux driver"),
    # The libX11 loader tests replace ctypes.CDLL; put the shared libc handle
    # cache back after each test so a fake never leaks into later tests.
    pytest.mark.usefixtures("fresh_darwin_libc_handles"),
]

#: A root-owned system binary for the launch-vetting tests (Linux only).
_TRUE_BIN = os.path.join(os.sep, "usr", "bin", "true")

_real_wayland_session = linux_driver.wayland_session
_x11_session = mock.patch.object(linux_driver, "wayland_session", return_value=False)


def setUpModule() -> None:
    # The X11 tests must not flip when the developer's own desktop is Wayland.
    _x11_session.start()


def tearDownModule() -> None:
    _x11_session.stop()
    linux_atspi.default_coord_type = COORD_SCREEN


class StateBitsTest(unittest.TestCase):
    def test_decodes_both_words(self) -> None:
        self.assertEqual(_states([1 << 12 | 1 << 25, 1 << (43 - 32)]), frozenset({12, 25, 43}))


class RoleTest(unittest.TestCase):
    def test_compact_roles(self) -> None:
        self.assertEqual(linux_driver._compact_role("push button"), "button")
        self.assertEqual(linux_driver._compact_role("password text"), "textfield")
        self.assertEqual(linux_driver._compact_role("tree item"), "treeitem")
        self.assertEqual(linux_driver._compact_role("description list"), "descriptionlist")

    def test_secure_roles(self) -> None:
        def node(role: str, role_id: int = 61, unknown: bool = False) -> Node:
            return Node(ref=Ref(":1.1", "/a"), depth=1, role=role, role_id=role_id,
                        role_unknown=unknown)  # fmt: skip

        self.assertTrue(linux_driver._is_secure(node("password text")))
        self.assertFalse(linux_driver._is_secure(node("text")))
        # Fail closed: the role NUMBER alone, or an unreadable role, counts as secure.
        self.assertTrue(linux_driver._is_secure(node("unknown", linux_atspi.ROLE_PASSWORD_TEXT)))
        self.assertTrue(linux_driver._is_secure(node("unknown", unknown=True)))


class KeyChordTest(unittest.TestCase):
    def test_modifier_and_key_mapping(self) -> None:
        self.assertEqual(linux_input.chord("s", {"ctrl"}), "Control_L+s")
        self.assertEqual(linux_input.chord("pagedown", set()), "Next")
        self.assertEqual(linux_input.chord("f5", {"shift", "alt"}), "Alt_L+Shift_L+F5")
        self.assertEqual(linux_input.chord("keypad7", set()), "KP_7")
        self.assertEqual(linux_input.chord("d", {"win"}), "Super_L+d")

    def test_unholdable_modifier_refused(self) -> None:
        with self.assertRaises(ComputerUseUnsupported):
            linux_input.chord("a", {"fn"})

    def test_character_keysyms(self) -> None:
        # Latin-1 is the code point itself; everything else is 0x01000000 | code.
        self.assertEqual(linux_input.char_keysym("a"), 0x61)
        self.assertEqual(linux_input.char_keysym("ä"), 0xE4)
        self.assertEqual(linux_input.char_keysym("€"), 0x010020AC)
        self.assertEqual(linux_input.char_keysym("\n"), 0xFF0D)
        self.assertEqual(linux_input.char_keysym("\t"), 0xFF09)


class LaunchCatalogTest(unittest.TestCase):
    def _write(self, directory: str, name: str, body: str) -> str:
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def test_field_codes_are_stripped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "demo.desktop",
                "[Desktop Entry]\nType=Application\nName=Demo\nExec=true %U --x %f\n",
            )
            app = linux_launch._parse(path)
        assert app is not None
        self.assertEqual(os.path.basename(app.argv[0]), "true")
        self.assertEqual(app.argv[1:], ("--x",))

    def test_exec_argv_handles_codes_per_token(self) -> None:
        argv = linux_launch._exec_argv('app --url=%u "100%%" %F %z')
        self.assertEqual(argv, ["app", "--url=", "100%", "%z"])
        self.assertIsNone(linux_launch._exec_argv('app "unterminated'))

    def test_match_key_folds_case_and_punctuation(self) -> None:
        key = linux_launch.match_key
        self.assertEqual(key("LibreOffice Calc"), key("libreoffice-calc"))
        self.assertEqual(key("Straße"), "strasse")
        self.assertEqual(key(""), "")

    def test_clean_blanks_control_and_private_use(self) -> None:
        text = "a\x00b\ufffcc\ue001d\x7fe\tf"
        self.assertEqual(linux_driver._clean(text, 100), "a b c d e\tf")

    def test_user_writable_directory_is_never_a_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self._write(
                tmp, "evil.desktop", "[Desktop Entry]\nType=Application\nName=Evil\nExec=true\n"
            )
            with mock.patch.object(linux_launch, "APPLICATION_DIRS", (tmp,)):
                self.assertEqual(linux_launch.installed_apps(), ())

    def test_hidden_entries_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                "h.desktop",
                "[Desktop Entry]\nType=Application\nName=H\nExec=true\nNoDisplay=true\n",
            )
            self.assertIsNone(linux_launch._parse(path))


class _FakeClient:
    """A tiny in-memory accessibility tree."""

    def __init__(self, tree: "dict[str, dict]") -> None:
        self.tree = tree
        self.coord_type = COORD_SCREEN

    def available(self):
        return True, ""

    def applications(self):
        return [(Ref(":1.5", "/app"), 42, "demo")]

    def read_basic(self, nodes, **_kwargs):
        for node in nodes:
            spec = self.tree.get(node.ref.path)
            if spec is None:
                node.failed = True
                continue
            node.role = spec["role"]
            node.role_id = linux_atspi.ROLE_PASSWORD_TEXT if "password" in node.role else 61
            node.role_unknown = spec.get("role_unknown", False)
            node.children_failed = spec.get("children_failed", False)
            node.name = spec.get("name", "")
            node.states = frozenset(spec.get("states", {STATE_SHOWING}))
            node.interfaces = frozenset(spec.get("ifaces", {COMPONENT}))
            node.children = [Ref(":1.5", p) for p in spec.get("kids", [])]

    def read_details(self, nodes, *, text_limit, skip_text):
        for position, node in enumerate(nodes):
            spec = self.tree.get(node.ref.path, {})
            node.extents = spec.get("ext")
            if position not in skip_text:
                node.text = spec.get("text", "")
            node.actions = tuple(spec.get("actions", ()))
            node.value = spec.get("value")

    def call(self, *args, **kwargs):  # selected-text probe
        return (0,)


TREE = {
    "/app": {"role": "application", "kids": ["/frame"]},
    "/frame": {"role": "frame", "name": "Demo", "ext": (100, 100, 400, 300), "kids": ["/panel"]},
    "/panel": {
        "role": "panel",
        "ext": (100, 100, 400, 300),
        "kids": ["/ok", "/pw", "/hidden", "/user"],
    },
    "/ok": {"role": "push button", "name": "OK", "ext": (110, 110, 50, 20), "actions": ["click"]},
    "/pw": {
        "role": "password text",
        "name": "alice",
        "text": "hunter2",
        "ext": (110, 140, 100, 20),
        "ifaces": {COMPONENT, TEXT},
        "states": {STATE_SHOWING, STATE_FOCUSED, STATE_EDITABLE},
    },
    "/hidden": {"role": "push button", "name": "Ghost", "states": set()},
    "/user": {
        "role": "text",
        "name": "User",
        "text": "bob",
        "ext": (110, 170, 100, 20),
        "ifaces": {COMPONENT, TEXT},
        "states": {STATE_SHOWING, STATE_EDITABLE},
    },
}


class WalkTest(unittest.TestCase):
    def setUp(self) -> None:
        info = linux_x11.WindowInfo(
            xid=7, pid=42, title="Demo", wm_instance="demo", wm_class="Demo", window_type="",
            x=100, y=100, width=400, height=300, viewable=True, hidden=False,
        )  # fmt: skip
        self.patches = [
            mock.patch.object(linux_driver.linux_atspi, "client", return_value=_FakeClient(TREE)),
            mock.patch.object(linux_driver.linux_x11, "window_info", return_value=info),
            mock.patch.object(
                linux_driver.linux_x11, "window_bounds", return_value=(100.0, 100.0, 400.0, 300.0)
            ),
        ]
        for patch in self.patches:
            patch.start()
        self.app = AppRef(name="Demo", pid=42, window_id=7, window_title="Demo")

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()

    def test_numbering_elision_and_secure_redaction(self) -> None:
        snap = linux_driver.LinuxBackend()._walk(self.app, SnapshotRequest(want_image=False))
        roles = [(e.index, e.role, e.title, e.depth) for e in snap.elements]
        # The window keeps its own title, not the last element's name ("User").
        self.assertEqual(snap.window_title, "Demo")
        # The unnamed panel is elided (depth passes through), the hidden button skipped.
        self.assertEqual(
            roles,
            [
                (0, "window", "Demo", 0),
                (1, "button", "OK", 1),
                (2, "textfield", "", 1),
                (3, "textfield", "User", 1),
            ],
        )
        secure = snap.elements[2]
        self.assertTrue(secure.secure)
        self.assertEqual(secure.value, "")
        self.assertIsNone(secure.frame)
        self.assertTrue(snap.has_secure)
        self.assertEqual(snap.elements[3].value, "bob")
        self.assertEqual(snap.elements[1].frame, (10.0, 10.0, 50.0, 20.0))
        self.assertEqual(snap.elements[1].actions, ("click",))

    def test_secure_window_gets_no_image(self) -> None:
        backend = linux_driver.LinuxBackend()
        snap = backend._walk(self.app, SnapshotRequest(want_image=True))
        with mock.patch.object(linux_driver.linux_x11, "capture_window") as capture:
            out = backend._attach_image(snap, SnapshotRequest(want_image=True))
        capture.assert_not_called()
        self.assertEqual(out.image_jpeg, b"")


VALUE_TREE = {
    "/app": {"role": "application", "kids": ["/frame"]},
    "/frame": {"role": "frame", "name": "Demo", "ext": (100, 100, 400, 300), "kids": ["/btn", "/bar"]},
    # LibreOffice exposes Value on every toolbar button, always 0.
    "/btn": {
        "role": "push button", "name": "Save", "ext": (110, 110, 50, 20),
        "actions": ["press"], "ifaces": {COMPONENT, VALUE}, "value": 0.0,
    },
    "/bar": {"role": "scroll bar", "ext": (480, 100, 15, 300), "ifaces": {COMPONENT, VALUE}, "value": 37.0},
}  # fmt: skip


class ValueRoleTest(unittest.TestCase):
    def test_numeric_value_only_on_range_roles(self) -> None:
        info = linux_x11.WindowInfo(
            xid=7, pid=42, title="Demo", wm_instance="demo", wm_class="Demo", window_type="",
            x=100, y=100, width=400, height=300, viewable=True, hidden=False,
        )  # fmt: skip
        with (
            mock.patch.object(
                linux_driver.linux_atspi, "client", return_value=_FakeClient(VALUE_TREE)
            ),
            mock.patch.object(linux_driver.linux_x11, "window_info", return_value=info),
            mock.patch.object(
                linux_driver.linux_x11, "window_bounds", return_value=(100.0, 100.0, 400.0, 300.0)
            ),
        ):
            app = AppRef(name="Demo", pid=42, window_id=7, window_title="Demo")
            snap = linux_driver.LinuxBackend()._walk(app, SnapshotRequest(want_image=False))
        values = {e.role: e.value for e in snap.elements}
        self.assertEqual(values["button"], "")
        self.assertEqual(values["scrollbar"], "37")


class PointerRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = linux_driver.LinuxBackend()
        self.app = AppRef(name="Demo", pid=42, window_id=7, window_title="Demo")

    def test_macos_routes_refused_by_name(self) -> None:
        for method in ("app_post", "sky_click", "auto"):
            result = self.backend.click(self.app, None, ClickRequest(method=method, point=(1, 1)))
            self.assertFalse(result.ok, method)

    def test_global_click_confined_to_window(self) -> None:
        with (
            mock.patch.object(linux_driver.linux_x11, "owns_point", return_value=False),
            mock.patch.object(linux_driver.linux_input, "click") as click,
        ):
            result = self.backend.click(self.app, None, ClickRequest(method="global", point=(5, 5)))
        self.assertFalse(result.ok)
        click.assert_not_called()

    def test_ownership_is_checked_on_the_injected_pixel(self) -> None:
        checked: list = []
        with (
            mock.patch.object(
                linux_driver.linux_x11,
                "owns_point",
                side_effect=lambda _w, x, y: checked.append((x, y)) or True,
            ),
            mock.patch.object(linux_driver.linux_input, "click") as click,
        ):
            req = ClickRequest(method="global", point=(99.75, 50.2))
            self.assertTrue(self.backend.click(self.app, None, req).ok)
        self.assertEqual(checked, [(100, 50)])
        self.assertEqual(click.call_args.args, (100, 50))

    def test_drag_points_are_checked_as_injected(self) -> None:
        checked: list = []
        with (
            mock.patch.object(
                linux_driver.linux_x11,
                "owns_point",
                side_effect=lambda _w, x, y: checked.append((x, y)) or True,
            ),
            mock.patch.object(linux_driver.linux_input, "drag") as drag,
        ):
            req = DragRequest(start=(10.6, 10.4), end=(99.6, 20.0), method="global")
            self.assertTrue(self.backend.drag(self.app, req).ok)
        self.assertEqual(checked, list(drag.call_args.args[0]))
        self.assertTrue(all(isinstance(c, int) for point in checked for c in point))

    def test_pointer_injection_holds_the_input_lock(self) -> None:
        with (
            mock.patch.object(linux_driver.linux_x11, "owns_point", return_value=True),
            mock.patch.object(
                linux_driver.linux_input,
                "click",
                side_effect=lambda *a, **k: self.assertTrue(
                    linux_driver.linux_input.input_lock.locked()
                ),
            ),
        ):
            self.backend.click(self.app, None, ClickRequest(method="global", point=(5, 5)))

    def test_drag_requires_global(self) -> None:
        result = self.backend.drag(self.app, DragRequest(start=(1, 1), end=(2, 2)))
        self.assertFalse(result.ok)
        self.assertIn("global", result.text)


class TextReadTest(unittest.TestCase):
    """GetText must never ask past the end: LibreOffice answers that with ''."""

    def test_end_offset_clamped_to_length(self) -> None:
        from kiro_crew.computer_use import linux_atspi

        client = linux_atspi.AtspiClient()
        calls: list[tuple] = []

        def fake_batch(msgs, **_kw):
            out = []
            for msg in msgs:
                from jeepney import HeaderFields

                member, body = msg.header.fields[HeaderFields.member], msg.body
                calls.append((member, body))
                if member == "Get":
                    out.append((("i", 2),))
                elif member == "GetText":
                    start, end = body
                    out.append(("" if end > 2 else "42"[start:end],))
                else:
                    out.append(linux_atspi.CallError("x"))
            return out

        node = Node(ref=Ref(":1.1", "/a"), depth=1, interfaces=frozenset({TEXT}))
        with mock.patch.object(client, "batch", side_effect=fake_batch):
            client.read_details([node], text_limit=100, skip_text=set())
        self.assertEqual(node.text, "42")
        self.assertIn(("GetText", (0, 2)), calls)

    def test_secure_text_never_requested(self) -> None:
        from kiro_crew.computer_use import linux_atspi

        client = linux_atspi.AtspiClient()
        node = Node(ref=Ref(":1.1", "/a"), depth=1, interfaces=frozenset({TEXT}))
        with mock.patch.object(client, "batch", return_value=[]) as batch:
            client.read_details([node], text_limit=100, skip_text={0})
        self.assertEqual(batch.call_args[0][0], [])


class SessionDetectionTest(unittest.TestCase):
    def _detect(self, env: "dict[str, str]", sockets: "list[str] | None" = None) -> bool:
        listing = mock.patch.object(
            linux_driver.os,
            "listdir",
            side_effect=(lambda _d: sockets) if sockets is not None else OSError(2, "absent"),
        )
        with mock.patch.dict(os.environ, env, clear=True), listing:
            return _real_wayland_session()

    def test_session_type_decides(self) -> None:
        self.assertTrue(self._detect({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}))
        self.assertFalse(self._detect({"XDG_SESSION_TYPE": "x11"}, ["wayland-0"]))

    def test_xwayland_display_does_not_make_it_x11(self) -> None:
        self.assertTrue(self._detect({"DISPLAY": ":0", "WAYLAND_DISPLAY": "wayland-0"}))

    def test_service_without_session_vars_finds_the_socket(self) -> None:
        self.assertTrue(self._detect({}, ["bus", "wayland-0", "wayland-0.lock"]))
        self.assertFalse(self._detect({}, ["bus", "pulse"]))
        self.assertFalse(self._detect({}))

    def test_explicit_x_display_pins_x11(self) -> None:
        env = {"KIROCREW_CU_DISPLAY": ":1", "XDG_SESSION_TYPE": "wayland"}
        self.assertFalse(self._detect(env))


WAYLAND_TREE = {
    "/app": {"role": "application", "kids": ["/frame", "/menu"]},
    "/frame": {
        "role": "frame",
        "name": "Demo",
        "ext": (0, 0, 400, 300),
        "states": {STATE_SHOWING, STATE_ACTIVE},
        "kids": ["/ok", "/user"],
    },
    # A non-toplevel child of the application root is not a window.
    "/menu": {"role": "menu", "name": "Popup"},
    "/ok": {"role": "push button", "name": "OK", "ext": (10, 10, 50, 20), "actions": ["click"]},
    "/user": {
        "role": "label",
        "name": "User",
        "ext": (10, 40, 100, 20),
        "states": {STATE_SHOWING},
    },
}


class WaylandTest(unittest.TestCase):
    """Tree-only mode: everything from AT-SPI, nothing from X11 or XTest."""

    def setUp(self) -> None:
        def no_x11(*_a, **_k):
            raise AssertionError("an X11 call on a Wayland session")

        self.patches = [
            mock.patch.object(
                linux_driver.linux_atspi, "client", return_value=_FakeClient(WAYLAND_TREE)
            ),
            mock.patch.object(linux_driver, "wayland_session", return_value=True),
        ]
        for name in ("window_info", "window_bounds", "client_windows", "capture_window",
                     "active_window", "owns_point", "available"):  # fmt: skip
            self.patches.append(mock.patch.object(linux_driver.linux_x11, name, no_x11))
        for name in ("click", "drag", "type_text", "send_chord", "activate_window"):
            self.patches.append(mock.patch.object(linux_driver.linux_input, name, no_x11))
        for patch in self.patches:
            patch.start()
        self.backend = linux_driver.LinuxBackend()

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()
        linux_atspi.default_coord_type = COORD_SCREEN

    def test_extents_are_requested_in_window_space(self) -> None:
        self.assertEqual(linux_atspi.default_coord_type, COORD_WINDOW)

    def test_windows_come_from_the_accessibility_registry(self) -> None:
        result = self.backend.list_apps()
        self.assertTrue(result.ok, result.text)
        self.assertEqual([(a.name, a.window_title) for a in result.apps], [("demo", "Demo")])
        self.assertNotEqual(result.apps[0].window_id, 0)

    def test_snapshot_is_tree_only_and_window_relative(self) -> None:
        app = self.backend.resolve_app("demo").app
        result = self.backend.snapshot(app, SnapshotRequest(want_image=True))
        self.assertTrue(result.ok, result.text)
        snap = result.snapshot
        self.assertEqual(snap.image_jpeg, b"")
        self.assertEqual(snap.window_bounds, (0.0, 0.0, 400.0, 300.0))
        ok = next(e for e in snap.elements if e.title == "OK")
        self.assertEqual(ok.frame, (10.0, 10.0, 50.0, 20.0))

    def test_synthetic_input_verbs_refuse_with_the_reason(self) -> None:
        app = self.backend.resolve_app("demo").app
        snap = self.backend.snapshot(app, SnapshotRequest(want_image=False)).snapshot
        label = next(e for e in snap.elements if e.title == "User")
        for result in (
            self.backend.click(app, None, ClickRequest(method="global", point=(5, 5))),
            self.backend.drag(app, DragRequest(start=(1, 1), end=(2, 2), method="global")),
            self.backend.press_key(app, label, "return"),
            self.backend.type_text(app, label, "hi"),
        ):
            self.assertFalse(result.ok)
            self.assertIn("Wayland", result.text)

    def test_status_needs_no_x_display(self) -> None:
        status = self.backend.status()
        self.assertTrue(status.supported, status.reason)


class FailClosedWalkTest(unittest.TestCase):
    """A read the driver could not complete never yields a capturable window."""

    def _walk(self, tree: "dict[str, dict]"):
        info = linux_x11.WindowInfo(
            xid=7, pid=42, title="Demo", wm_instance="demo", wm_class="Demo", window_type="",
            x=100, y=100, width=400, height=300, viewable=True, hidden=False,
        )  # fmt: skip
        with (
            mock.patch.object(linux_driver.linux_atspi, "client", return_value=_FakeClient(tree)),
            mock.patch.object(linux_driver.linux_x11, "window_info", return_value=info),
        ):
            app = AppRef(name="Demo", pid=42, window_id=7, window_title="Demo")
            return linux_driver.LinuxBackend()._walk(app, SnapshotRequest(want_image=False))

    def _tree(self, **user: object) -> "dict[str, dict]":
        tree = {k: dict(v) for k, v in TREE.items()}
        tree["/panel"]["kids"] = ["/ok", "/user"]
        tree["/user"].update(user)
        return tree

    def test_unknown_role_is_treated_as_secure(self) -> None:
        snap = self._walk(self._tree(role="unknown", role_unknown=True))
        rec = next(e for e in snap.elements if e.secure)
        self.assertEqual((rec.title, rec.value), ("", ""))
        self.assertTrue(snap.has_secure)

    def test_failed_child_list_truncates_the_walk(self) -> None:
        tree = self._tree()
        tree["/panel"]["children_failed"] = True
        self.assertTrue(self._walk(tree).truncated)

    def test_unreadable_node_truncates_the_walk(self) -> None:
        tree = self._tree()
        tree["/panel"]["kids"] = ["/ok", "/gone"]  # /gone has no spec: read fails
        self.assertTrue(self._walk(tree).truncated)

    def test_failed_properties_read_marks_the_subtree_incomplete(self) -> None:
        client = linux_atspi.AtspiClient()
        node = Node(ref=Ref(":1.1", "/c"), depth=1)
        replies = [
            (61,),
            ([0, 0],),
            (["org.a11y.atspi.Accessible"],),
            linux_atspi.CallError("NoReply"),
        ]
        with (
            mock.patch.object(client, "batch", side_effect=[replies, []]),
            mock.patch.object(client, "_role_name", return_value="panel"),
        ):
            client.read_basic([node])
        self.assertFalse(node.failed)
        self.assertTrue(node.children_failed)

    def test_role_name_failure_is_not_cached(self) -> None:
        client = linux_atspi.AtspiClient()
        node = Node(ref=Ref(":1.1", "/a"), depth=1, role_id=61)
        with mock.patch.object(client, "call", side_effect=linux_atspi.CallError("NoReply")):
            self.assertEqual(client._role_name(node), "unknown")
        self.assertTrue(node.role_unknown)
        self.assertNotIn(61, client._role_names)
        fresh = Node(ref=Ref(":1.1", "/b"), depth=1, role_id=61)
        with mock.patch.object(client, "call", return_value=("push button",)):
            self.assertEqual(client._role_name(fresh), "push button")
        self.assertFalse(fresh.role_unknown)


class KeystrokeFallbackTest(unittest.TestCase):
    """The XTest fallback checks focus once, so the text must not be able to move it."""

    def setUp(self) -> None:
        info = linux_x11.WindowInfo(
            xid=7, pid=42, title="Demo", wm_instance="demo", wm_class="Demo", window_type="",
            x=100, y=100, width=400, height=300, viewable=True, hidden=False,
        )  # fmt: skip
        self.patches = [
            mock.patch.object(linux_driver.linux_atspi, "client", return_value=_FakeClient(TREE)),
            mock.patch.object(linux_driver.linux_x11, "window_info", return_value=info),
            mock.patch.object(linux_driver.linux_input, "type_text"),
            mock.patch.object(linux_driver.linux_input, "caps_lock_conflict", return_value=False),
        ]
        for patch in self.patches:
            patch.start()
        self.backend = linux_driver.LinuxBackend()
        self.app = AppRef(name="Demo", pid=42, window_id=7, window_title="Demo")
        snap = self.backend._walk(self.app, SnapshotRequest(want_image=False))
        self.ok = next(e for e in snap.elements if e.title == "OK")

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()

    def test_embedded_tab_refused_before_any_keystroke(self) -> None:
        for text in ("a\tx", "a\nx", "a\x1bx"):
            result = self.backend.type_text(self.app, self.ok, text)
            self.assertFalse(result.ok, text)
            self.assertIn("computer_press_key", result.text)
        linux_driver.linux_input.type_text.assert_not_called()

    def test_insert_text_length_is_utf8_bytes(self) -> None:
        snap = self.backend._walk(self.app, SnapshotRequest(want_image=False))
        user = next(e for e in snap.elements if e.title == "User")
        client = linux_driver.linux_atspi.client()
        counts = iter([3, 5])
        calls: list = []
        with (
            mock.patch.object(self.backend, "_char_count", side_effect=lambda _r: next(counts)),
            mock.patch.object(client, "get_prop", return_value=3, create=True),
            mock.patch.object(client, "call", side_effect=lambda *a, **k: calls.append(a) or (0,)),
            mock.patch.object(
                self.backend,
                "_live",
                return_value=Node(
                    ref=Ref(":1.5", "/user"), depth=1,
                    interfaces=frozenset({COMPONENT, TEXT, linux_atspi.EDITABLE_TEXT}),
                    states=frozenset({STATE_SHOWING, STATE_EDITABLE}),
                ),  # fmt: skip
            ),
        ):
            result = self.backend.type_text(self.app, user, "éx")
        self.assertTrue(result.ok, result.text)
        insert = next(c for c in calls if "InsertText" in c)
        self.assertEqual(insert[-1], (3, "éx", 3))

    def test_an_insert_whose_outcome_is_unverified_is_never_retyped(self) -> None:
        """A CharacterCount read that times out, or an InsertText with no reply, leaves
        the text possibly in the field: typing it again would insert it twice."""
        snap = self.backend._walk(self.app, SnapshotRequest(want_image=False))
        user = next(e for e in snap.elements if e.title == "User")
        client = linux_driver.linux_atspi.client()
        editable = Node(
            ref=Ref(":1.5", "/user"), depth=1,
            interfaces=frozenset({COMPONENT, TEXT, linux_atspi.EDITABLE_TEXT}),
            states=frozenset({STATE_SHOWING, STATE_EDITABLE}),
        )  # fmt: skip

        def no_reply(*_a, **_k):
            raise linux_atspi.CallError("NoReply", "no reply before the deadline")

        for name, insert, counts, ok in (
            ("count unreadable", lambda *a, **k: (True,), [3, None], True),
            ("insert unanswered", no_reply, [3, 3], False),
        ):
            with self.subTest(name=name):
                seen = iter(counts)
                with (
                    mock.patch.object(
                        self.backend, "_char_count", side_effect=lambda _r: next(seen)
                    ),
                    mock.patch.object(client, "get_prop", return_value=3, create=True),
                    mock.patch.object(client, "call", side_effect=insert),
                    mock.patch.object(self.backend, "_live", return_value=editable),
                ):
                    result = self.backend.type_text(self.app, user, "hi")
                self.assertIs(result.ok, ok, result.text)
                self.assertIn("computer_get_state", result.text)
        linux_driver.linux_input.type_text.assert_not_called()

    def test_focus_and_keystrokes_share_the_input_lock(self) -> None:
        lock = linux_driver.linux_input.input_lock
        seen: list = []
        with (
            mock.patch.object(
                self.backend, "_focus", side_effect=lambda *a: seen.append(lock.locked())
            ),
            mock.patch.object(
                linux_driver.linux_input,
                "type_text",
                side_effect=lambda _t: seen.append(lock.locked()),
            ),
        ):
            self.assertTrue(self.backend.type_text(self.app, self.ok, "abc").ok)
        self.assertEqual(seen, [True, True])

    def test_caps_lock_refused_before_any_keystroke(self) -> None:
        with mock.patch.object(linux_driver.linux_input, "caps_lock_conflict", return_value=True):
            result = self.backend.type_text(self.app, self.ok, "abc")
        self.assertFalse(result.ok)
        self.assertIn("Caps Lock", result.text)
        linux_driver.linux_input.type_text.assert_not_called()


class CapsLockTest(unittest.TestCase):
    def test_only_cased_text_conflicts(self) -> None:
        with mock.patch.object(linux_input, "_need") as need:
            self.assertFalse(linux_input.caps_lock_conflict("123 -+"))
        need.assert_not_called()

    def test_locked_caps_with_letters_conflicts(self) -> None:
        with (
            mock.patch.object(linux_input, "_need", return_value=(object(), object())),
            mock.patch.object(linux_input.linux_x11, "_dpy", return_value=1),
            mock.patch.object(linux_input, "_kb_state", return_value=(1, linux_input._LOCK_MASK)),
        ):
            self.assertTrue(linux_input.caps_lock_conflict("abc"))
        with (
            mock.patch.object(linux_input, "_need", return_value=(object(), object())),
            mock.patch.object(linux_input.linux_x11, "_dpy", return_value=1),
            mock.patch.object(linux_input, "_kb_state", return_value=(1, 0)),
        ):
            self.assertFalse(linux_input.caps_lock_conflict("abc"))


class LaunchExecutableTest(unittest.TestCase):
    """A bare Exec name never resolves through PATH, and the canonical path is run."""

    def test_planted_path_shim_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink(_TRUE_BIN, os.path.join(tmp, "thunar-shim"))
            with mock.patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ.get('PATH', '')}"}):
                self.assertIsNone(linux_launch._vetted_executable("thunar-shim"))

    def test_bare_name_resolves_from_system_bin(self) -> None:
        found = linux_launch._vetted_executable("true")
        assert found is not None
        self.assertEqual(found, os.path.realpath(found))

    def test_agent_owned_absolute_path_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            link = os.path.join(tmp, "app")
            os.symlink(_TRUE_BIN, link)
            # The link's target is root-owned, but the launch would run the canonical
            # path, so only that path's ownership decides; a user-owned copy fails.
            self.assertEqual(linux_launch._vetted_executable(link), os.path.realpath(_TRUE_BIN))
            copy = os.path.join(tmp, "copy")
            # copy2 keeps the exec bit, so the copy is refused for its owner alone.
            shutil.copy2(_TRUE_BIN, copy)
            self.assertIsNone(linux_launch._vetted_executable(copy))

    def test_launch_identity_carries_every_spelling(self) -> None:
        app = linux_launch.InstalledApp(
            desktop_id="org.gnome.Terminal", name="Terminal", argv=("gnome-terminal",),
            wm_class="Gnome-terminal", executable=_TRUE_BIN,
        )  # fmt: skip
        with mock.patch.object(linux_launch, "installed_apps", return_value=(app,)):
            who = linux_launch.target_identity("org.gnome.Terminal", "Terminal")
        keys = {ref.bundle_id for ref in who.app_refs()}
        # Spellings that differ only in case are one key: the policy lowercases both sides.
        self.assertEqual(keys, {"org.gnome.Terminal", "true", "gnome-terminal"})

    def test_launch_policy_checks_every_spelling(self) -> None:
        from kiro_crew.computer_use import tools
        from kiro_crew.computer_use.types import LaunchIdentity, PolicyConfig

        who = LaunchIdentity(
            display="Terminal", key="org.gnome.Terminal", aliases=("gnome-terminal",)
        )
        denied = PolicyConfig(extra_denied_apps=("gnome-terminal",))
        self.assertIsNotNone(tools._launch_refusal(who, denied))
        allowed = PolicyConfig(allowed_apps=("gnome-terminal",))
        self.assertIsNone(tools._launch_refusal(who, allowed))
        other = PolicyConfig(allowed_apps=("thunar",))
        self.assertIsNotNone(tools._launch_refusal(who, other))

    def test_spawn_runs_the_vetted_path(self) -> None:
        app = linux_launch.InstalledApp(
            desktop_id="demo", name="Demo", argv=("demo", "--x"), wm_class="",
            executable=_TRUE_BIN,
        )  # fmt: skip
        with (
            mock.patch.object(linux_launch, "installed_apps", return_value=(app,)),
            mock.patch.object(linux_launch.linux_x11, "session_env", return_value={}),
            mock.patch.object(linux_launch.subprocess, "Popen") as popen,
        ):
            linux_launch.spawn_detached("demo")
        self.assertEqual(popen.call_args.args[0], ["demo", "--x"])
        self.assertEqual(popen.call_args.kwargs["executable"], _TRUE_BIN)

    def test_a_launched_app_gets_only_the_trusted_path(self) -> None:
        """A launcher script resolves its helpers through PATH, so an agent-writable
        directory the gateway inherited must not reach it."""
        app = linux_launch.InstalledApp(
            desktop_id="demo", name="Demo", argv=("demo",), wm_class="", executable=_TRUE_BIN,
        )  # fmt: skip
        with (
            mock.patch.object(linux_launch, "installed_apps", return_value=(app,)),
            mock.patch.object(
                linux_launch.linux_x11,
                "session_env",
                return_value={"PATH": "/home/u/.local/bin:/usr/bin", "DISPLAY": ":0"},
            ),
            mock.patch.object(linux_launch, "trusted_system_path", return_value="/usr/bin:/bin"),
            mock.patch.object(linux_launch.subprocess, "Popen") as popen,
        ):
            linux_launch.spawn_detached("demo")
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["DISPLAY"], ":0")


class X11ConnectionLossTest(unittest.TestCase):
    def test_lost_connection_is_dropped_and_reopened(self) -> None:
        lib = mock.Mock()
        lib.XOpenDisplay.return_value = 0xBEEF
        with (
            mock.patch.object(linux_x11, "_display", 0xDEAD),
            mock.patch.object(linux_x11, "_ensure_lib", return_value=lib),
            mock.patch.object(linux_x11, "display_name", return_value=":1"),
            mock.patch.object(linux_x11, "xauthority", return_value=""),
        ):
            linux_x11._connection_lost[0] = True
            self.assertEqual(linux_x11._dpy(), 0xBEEF)
            self.assertFalse(linux_x11._connection_lost[0])
            lib.XCloseDisplay.assert_not_called()
            lib.XSetIOErrorExitHandler.assert_called_once()

    def test_old_libx11_is_refused(self) -> None:
        fake = mock.Mock(spec=["XInitThreads"])
        with (
            mock.patch.object(linux_x11, "_lib", None),
            mock.patch.object(linux_x11.ctypes, "CDLL", return_value=fake),
        ):
            with self.assertRaises(linux_x11.X11Unavailable):
                linux_x11._ensure_lib()


if __name__ == "__main__":
    unittest.main()


# ── Walk binding, bounded names, held modifiers, keyboard levels ──

#: The same window walked at a smaller depth budget: index 1 is another "OK" button.
TREE_SHALLOW = {
    "/app": {"role": "application", "kids": ["/frame"]},
    "/frame": {"role": "frame", "name": "Demo", "ext": (100, 100, 400, 300), "kids": ["/ok2"]},
    "/ok2": {"role": "push button", "name": "OK", "ext": (300, 300, 50, 20), "actions": ["click"]},
}


class WalkBindingTest(unittest.TestCase):
    """An action resolves against the walk its own thread verified."""

    def setUp(self) -> None:
        info = linux_x11.WindowInfo(
            xid=7, pid=42, title="Demo", wm_instance="demo", wm_class="Demo", window_type="",
            x=100, y=100, width=400, height=300, viewable=True, hidden=False,
        )  # fmt: skip
        self.client = _FakeClient(TREE)
        self.patches = [
            mock.patch.object(linux_driver.linux_atspi, "client", side_effect=lambda: self.client),
            mock.patch.object(linux_driver.linux_x11, "window_info", return_value=info),
            mock.patch.object(
                linux_driver.linux_x11, "window_bounds", return_value=(100.0, 100.0, 400.0, 300.0)
            ),
        ]
        for patch in self.patches:
            patch.start()
        self.app = AppRef(name="Demo", pid=42, window_id=7, window_title="Demo")

    def tearDown(self) -> None:
        for patch in self.patches:
            patch.stop()

    def test_another_threads_snapshot_does_not_rebind_the_verified_walk(self) -> None:
        backend = linux_driver.LinuxBackend()
        verified = backend._walk(self.app, SnapshotRequest(want_image=False))
        ok = verified.elements[1]
        self.assertEqual((ok.role, ok.title), ("button", "OK"))

        def other_snapshot() -> None:
            backend._walk(self.app, SnapshotRequest(want_image=False, max_depth=1))

        self.client = _FakeClient(TREE_SHALLOW)
        other = threading.Thread(target=other_snapshot)
        other.start()
        other.join()
        self.client = _FakeClient({**TREE, **TREE_SHALLOW})

        node = backend._live(self.app, ok)
        self.assertEqual(node.ref.path, "/ok")


class WalkBudgetTest(WalkBindingTest):
    """A wide level is read only as far as the node budget can still keep."""

    def test_a_wide_level_is_not_read_past_the_budget(self) -> None:
        tree: "dict[str, dict]" = {
            "/app": {"role": "application", "kids": ["/frame"]},
            "/frame": {
                "role": "frame", "name": "Demo", "ext": (100, 100, 400, 300),
                "kids": [f"/c{i}" for i in range(40)],
            },
        }  # fmt: skip
        for i in range(40):
            tree[f"/c{i}"] = {
                "role": "panel",
                "name": f"C{i}",
                "kids": [f"/c{i}/k{j}" for j in range(40)],
            }
            for j in range(40):
                tree[f"/c{i}/k{j}"] = {"role": "push button", "name": f"K{j}"}
        read: list[int] = []
        client = _FakeClient(tree)
        real = client.read_basic

        def counting(nodes, **kwargs):
            read.append(len(nodes))
            return real(nodes, **kwargs)

        client.read_basic = counting  # type: ignore[method-assign]
        self.client = client
        snap = linux_driver.LinuxBackend()._walk(
            self.app, SnapshotRequest(want_image=False, max_nodes=10)
        )
        self.assertTrue(snap.truncated)
        self.assertLessEqual(len(snap.elements), 10)
        # Without the bound: 1 + 40 + 1600 nodes read for a 10-node budget.
        self.assertLess(sum(read), 40)


class BoundedNameTest(unittest.TestCase):
    def test_a_huge_name_is_cut_when_read(self) -> None:
        client = linux_atspi.AtspiClient()
        node = Node(ref=Ref(":1.5", "/huge"), depth=0)
        props = {"Name": ("s", "x" * (MAX_TEXT_LIMIT * 5)), "ChildCount": ("i", 0)}
        with (
            mock.patch.object(client, "batch", return_value=[(61,), ([0, 0],), ([],), (props,)]),
            mock.patch.object(client, "_role_name", return_value="label"),
        ):
            client.read_basic([node])
        self.assertEqual(len(node.name), MAX_TEXT_LIMIT + 1)
        self.assertFalse(hasattr(node, "description"))


class _FakeX11:
    """A German-layout keyboard: ``7`` is level 0 of keycode 16, ``slash`` level 1."""

    SHIFT_L, CONTROL_L, KEY_7, KEY_A = 50, 37, 16, 38

    def __init__(self, *, held: "tuple[int, ...]" = (), latched: int = 0, locked: int = 0) -> None:
        self.held = set(held)
        self.latched = latched
        self.locked = locked
        self._levels = {
            self.KEY_7: (0x37, 0x2F),
            self.KEY_A: (0x61, 0x41),
            self.SHIFT_L: (0xFFE1, 0xFFE1),
            self.CONTROL_L: (0xFFE3, 0xFFE3),
            22: (0xFF08, 0xFF08),  # BackSpace
        }
        self._table = (ctypes.c_ubyte * 16)()

    def XKeysymToKeycode(self, _dpy, sym):
        for code, levels in self._levels.items():
            if sym in levels:
                return code
        return 0

    def XkbKeycodeToKeysym(self, _dpy, code, _group, level):
        return self._levels.get(code, (0, 0))[level]

    def XStringToKeysym(self, name):
        return {b"slash": 0x2F, b"a": 0x61, b"Control_L": 0xFFE3, b"BackSpace": 0xFF08}.get(name, 0)

    def XkbGetState(self, _dpy, _device, ref):
        state = ref._obj
        state.latched_mods = self.latched
        state.locked_mods = self.locked
        return 0

    def XQueryKeymap(self, _dpy, keys):
        for code in self.held:
            keys[code // 8] = bytes([keys[code // 8][0] | (1 << (code % 8))])

    def XGetModifierMapping(self, _dpy):
        self._table[0] = self.SHIFT_L  # Shift row
        self._table[4] = self.CONTROL_L  # Control row
        return ctypes.pointer(linux_input._XModifierKeymap(2, self._table))

    def XFreeModifiermap(self, _mapping):
        pass

    def XSync(self, *_args):
        pass


class _FakeXtst:
    def __init__(self) -> None:
        self.events: list[tuple[int, bool]] = []

    def XTestFakeKeyEvent(self, _dpy, code, down, _delay):
        self.events.append((code, bool(down)))


class KeyboardStateTest(unittest.TestCase):
    def _run(self, x11: _FakeX11, fn) -> "list[tuple[int, bool]]":
        xtst = _FakeXtst()
        with (
            mock.patch.object(linux_input, "_need", return_value=(x11, xtst)),
            mock.patch.object(linux_input.linux_x11, "_dpy", return_value=1),
        ):
            fn()
        return xtst.events

    def test_a_symbol_on_the_shift_level_gets_shift_in_a_chord(self) -> None:
        x11 = _FakeX11()
        events = self._run(x11, lambda: linux_input.send_chord("slash"))
        s, k = x11.SHIFT_L, x11.KEY_7
        self.assertEqual(events, [(s, True), (k, True), (k, False), (s, False)])

    def test_a_held_shift_is_released_while_typing_and_pressed_again(self) -> None:
        x11 = _FakeX11(held=(_FakeX11.SHIFT_L,))
        events = self._run(x11, lambda: linux_input.type_text("a"))
        s, a = x11.SHIFT_L, x11.KEY_A
        self.assertEqual(events, [(s, False), (a, True), (a, False), (s, True)])

    def test_a_held_ctrl_does_not_join_a_chord(self) -> None:
        x11 = _FakeX11(held=(_FakeX11.CONTROL_L,))
        events = self._run(x11, lambda: linux_input.send_chord("a"))
        c, a = x11.CONTROL_L, x11.KEY_A
        self.assertEqual(events, [(c, False), (a, True), (a, False), (c, True)])

    def test_a_latched_or_locked_modifier_refuses_before_any_key(self) -> None:
        """Sticky Keys: a locked Ctrl would turn Backspace into Ctrl+Backspace."""
        cases = [
            (_FakeX11(locked=0x04), lambda: linux_input.send_chord("BackSpace"), "Ctrl"),
            (_FakeX11(latched=0x01), lambda: linux_input.type_text("a"), "Shift"),
            (_FakeX11(locked=0x08), lambda: linux_input.send_chord("a"), "Alt"),
        ]
        for x11, fn, name in cases:
            with self.subTest(name=name):
                xtst = _FakeXtst()
                with (
                    mock.patch.object(linux_input, "_need", return_value=(x11, xtst)),
                    mock.patch.object(linux_input.linux_x11, "_dpy", return_value=1),
                    self.assertRaises(ComputerUseError) as caught,
                ):
                    fn()
                self.assertIn(name, str(caught.exception))
                self.assertIn("Sticky Keys", str(caught.exception))
                self.assertEqual(xtst.events, [])

    def test_caps_and_num_lock_do_not_refuse_a_chord(self) -> None:
        x11 = _FakeX11(locked=0x02 | 0x10)
        events = self._run(x11, lambda: linux_input.send_chord("a"))
        a = x11.KEY_A
        self.assertEqual(events, [(a, True), (a, False)])
