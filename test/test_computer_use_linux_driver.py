"""Unit tests for the Linux computer-use driver (no display or bus needed)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

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
    AppRef,
    ClickRequest,
    ComputerUseUnsupported,
    DragRequest,
    SnapshotRequest,
)

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
        self.assertTrue(linux_driver._is_secure("password text"))
        self.assertFalse(linux_driver._is_secure("text"))


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
            node.role_id = hash(spec["role"]) & 0xFF
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
                member, body = msg.header.fields[linux_atspi.HeaderFields.member], msg.body
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


if __name__ == "__main__":
    unittest.main()
