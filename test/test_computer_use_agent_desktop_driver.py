"""Computer use — the agent-desktop CLI driver, against a fake CLI.

No real agent-desktop binary runs here: every test injects a runner that answers
with canned JSON envelopes and records the argv it was given. What is pinned:

* the snapshot translation keeps ``ElementRec.secure`` honest (state AND role) and
  withholds a secure value, and an incomplete walk reads as truncated;
* input to a field that is secure NOW is refused even when the cached record was
  not, and no ``type``/``set-value`` call is issued;
* ``--headed`` is passed only for a pointer-moving request;
* a ref is used only for the record whose fingerprint it was stored with;
* the locator's order and the digest pin;
* backend selection: agent-desktop or a typed refusal.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from kiro_crew.computer_use import agent_desktop_locate as locate_mod
from kiro_crew.computer_use import backend as backend_mod
from kiro_crew.computer_use.agent_desktop_driver import (
    SESSION_ID,
    AgentDesktopBackend,
    build_snapshot,
    normalize_key,
)
from kiro_crew.computer_use.types import (
    CLICK_METHOD_APP_POST,
    CLICK_METHOD_GLOBAL,
    AppRef,
    ClickRequest,
    DragRequest,
    DriverResult,
    SnapshotRequest,
)
from kiro_crew.testing.fake_computer_use import FakeNativeHelper

APP = AppRef(name="Demo", pid=42, bundle_id="com.example.demo", window_id=7, window_title="Demo")

TREE = {
    "role": "window",
    "name": "Demo",
    "bounds": {"x": 100, "y": 200, "width": 800, "height": 600},
    "children": [
        {
            "role": "group",
            "bounds": {"x": 100, "y": 200, "width": 800, "height": 600},
            "children": [
                {
                    "role": "button",
                    "name": "Save",
                    "ref_id": "@s1:e1",
                    "available_actions": ["Click", "RightClick"],
                    "bounds": {"x": 110, "y": 220, "width": 60, "height": 20},
                },
                {
                    "role": "textfield",
                    "name": "Password",
                    "value": "hunter2",
                    "ref_id": "@s1:e2",
                    "states": ["secure"],
                    "available_actions": ["SetValue", "TypeText"],
                },
                {
                    "role": "textfield",
                    "name": "User",
                    "value": "bob",
                    "ref_id": "@s1:e3",
                    "states": ["focused"],
                    "available_actions": ["SetValue", "TypeText"],
                },
                {"role": "statictext", "name": "Hint"},
            ],
        }
    ],
}


def _ok(command, data):
    return json.dumps({"version": "2.5", "ok": True, "command": command, "data": data})


def _err(command, code, delivery="not_delivered", message="nope"):
    return json.dumps(
        {
            "version": "2.5",
            "ok": False,
            "command": command,
            "error": {"code": code, "message": message, "disposition": {"delivery": delivery}},
        }
    )


class FakeCli:
    """Answers agent-desktop argv with canned envelopes; records every call."""

    def __init__(self, tree=TREE, *, states=None, complete=True):
        self.calls: list[list[str]] = []
        self.tree = tree
        self.states = states or {}
        self.complete = complete
        self.errors: dict[str, str] = {}
        #: Whether a ``focus`` call moves focus (what a real provider usually does).
        self.focus_lands = True

    def __call__(self, argv, env, timeout):
        args = argv[1:]
        self.calls.append(args)
        command = args[0]
        if command in self.errors:
            return 1, self.errors[command]
        if command == "--version":
            return 0, "agent-desktop 1.0.2\n"
        if command == "focus" and self.focus_lands:
            ref = args[-1]
            self.states[ref] = [*self.states.get(ref, []), "focused"]
        if command == "status":
            return 0, _ok(command, {"supported_surfaces": ["window"]})
        if command == "snapshot":
            return 0, _ok(
                command,
                {
                    "complete": self.complete,
                    "snapshot_id": "s1",
                    "window": {"id": "w-7", "title": "Demo"},
                    "tree": self.tree,
                },
            )
        if command == "get":
            ref, prop = args[-1], args[args.index("--property") + 1]
            if prop == "states":
                return 0, _ok(command, {"value": self.states.get(ref, [])})
            return 0, _ok(command, {"value": "textfield"})
        if command == "list-windows":
            return 0, _ok(
                command,
                [
                    {
                        "app_name": "Demo",
                        "pid": 42,
                        "id": "w-7",
                        "title": "Demo",
                        "visible": True,
                        "accessible": True,
                    },
                    {
                        "app_name": "Ghost",
                        "pid": 9,
                        "id": "w-8",
                        "title": "",
                        "visible": False,
                        "accessible": True,
                    },
                ],
            )
        return 0, _ok(command, {"action": command})

    def commands(self):
        return [c[0] for c in self.calls]


def _backend(cli, native=None):
    return AgentDesktopBackend("/fake/agent-desktop", "macos", native=native, runner=cli)


def _snap(be, req=None):
    result = be.snapshot(APP, req or SnapshotRequest(want_image=False))
    assert result.ok, result.text
    return result.snapshot


def _rec(snap, title):
    return next(e for e in snap.elements if e.title == title)


class TestSnapshotTranslation:
    def test_secure_state_sets_secure_and_withholds_value(self):
        snap, _refs = build_snapshot(APP, {"tree": TREE, "complete": True}, SnapshotRequest())
        pw = _rec(snap, "Password")
        assert pw.secure is True
        assert pw.value == ""
        assert snap.has_secure is True
        assert _rec(snap, "User").value == "bob"

    def test_password_role_alone_is_secure(self):
        tree = {
            "role": "window",
            "children": [{"role": "passwordfield", "value": "x", "ref_id": "@s1:e1"}],
        }
        snap, _ = build_snapshot(APP, {"tree": tree}, SnapshotRequest())
        assert snap.elements[1].secure is True and snap.elements[1].value == ""

    def test_structural_group_is_elided_and_frames_are_window_local(self):
        snap, refs = build_snapshot(APP, {"tree": TREE}, SnapshotRequest())
        assert [e.role for e in snap.elements] == [
            "window",
            "button",
            "textfield",
            "textfield",
            "statictext",
        ]
        save = _rec(snap, "Save")
        assert save.frame == (10.0, 20.0, 60.0, 20.0)
        assert snap.window_bounds == (100.0, 200.0, 800.0, 600.0)
        assert refs[save.index][0] == "@s1:e1"
        assert _rec(snap, "Hint").index not in refs

    def test_incomplete_walk_and_node_budget_mark_truncated(self):
        snap, _ = build_snapshot(APP, {"tree": TREE, "complete": False}, SnapshotRequest())
        assert snap.truncated is True
        snap, _ = build_snapshot(APP, {"tree": TREE}, SnapshotRequest(max_nodes=2))
        assert snap.truncated is True and len(snap.elements) == 2
        # Secure nodes past the budget still count, so capture stays closed.
        assert snap.has_secure is True

    def test_focused_and_traits(self):
        snap, _ = build_snapshot(APP, {"tree": TREE}, SnapshotRequest())
        user = _rec(snap, "User")
        assert user.focused is True
        assert "editable" in user.traits

    def test_snapshot_passes_window_id_session_and_clamped_depth(self):
        cli = FakeCli()
        _snap(_backend(cli), SnapshotRequest(want_image=False, max_depth=128))
        args = cli.calls[0]
        assert args[:3] == ["snapshot", "--window-id", "w-7"]
        assert args[args.index("--max-depth") + 1] == "50"
        assert args[-2:] == ["--session", SESSION_ID]

    def test_agent_text_that_looks_like_a_flag_stays_text(self):
        """Typed text, values and keys go after ``--``, so none of them is a flag."""
        cli = FakeCli()
        be = _backend(cli)
        user = _rec(_snap(be), "User")
        be.type_text(APP, user, "--headed")
        be.set_value(APP, user, "--force")
        be.press_key(APP, None, "--force")
        for call in cli.calls[1:]:
            if call[0] in ("type", "set-value", "press"):
                cut = call.index("--")
                assert "--headed" not in call[:cut] and "--force" not in call[:cut], call


class TestSecureInputRefusal:
    def test_field_secure_now_is_refused_before_any_input(self):
        cli = FakeCli(states={"@s1:e3": ["secure"]})
        be = _backend(cli)
        user = _rec(_snap(be), "User")
        assert user.secure is False
        for result in (be.type_text(APP, user, "pw"), be.set_value(APP, user, "pw")):
            assert result.ok is False
            assert "secure (password) field" in result.text
        assert "type" not in cli.commands() and "set-value" not in cli.commands()

    def test_plain_field_types_through_the_ref(self):
        cli = FakeCli()
        be = _backend(cli)
        user = _rec(_snap(be), "User")
        assert be.type_text(APP, user, "hello").ok
        assert ["type", "--session", SESSION_ID, "--", "@s1:e3", "hello"] in cli.calls


class TestClick:
    def test_element_click_is_headless(self):
        cli = FakeCli()
        be = _backend(cli)
        save = _rec(_snap(be), "Save")
        assert be.click(APP, save, ClickRequest()).ok
        assert cli.calls[-1] == ["click", "--session", SESSION_ID, "--", "@s1:e1"]

    def test_a_global_point_click_goes_through_the_confining_helper(self):
        cli = FakeCli()
        native = FakeNativeHelper()
        be = _backend(cli, native=native)
        req = ClickRequest(method=CLICK_METHOD_GLOBAL, point=(5.0, 6.0))
        be.click(APP, None, req)
        assert "mouse-click" not in cli.commands()
        name, fields = native.backend.calls[-1]
        assert name == "click" and fields["method"] == CLICK_METHOD_GLOBAL

    def test_pointer_free_point_click_delegates_or_refuses(self):
        req = ClickRequest(method=CLICK_METHOD_APP_POST, point=(5.0, 6.0))
        refused = _backend(FakeCli()).click(APP, None, req)
        assert refused.ok is False and "cannot do" in refused.text
        native = FakeNativeHelper()
        cli = FakeCli()
        _backend(cli, native=native).click(APP, None, req)
        assert "mouse-click" not in cli.commands()

    def test_record_that_no_longer_matches_its_ref_is_refused(self):
        cli = FakeCli()
        be = _backend(cli)
        save = _rec(_snap(be), "Save")
        from dataclasses import replace

        moved = replace(save, title="Delete")
        result = be.click(APP, moved, ClickRequest())
        assert result.ok is False and "changed since" in result.text
        assert cli.commands().count("click") == 0

    def test_static_text_has_no_ref(self):
        be = _backend(FakeCli())
        result = be.click(APP, _rec(_snap(be), "Hint"), ClickRequest())
        assert result.ok is False and "not addressable" in result.text

    def test_drag_without_pointer_move_or_native_is_refused(self):
        result = _backend(FakeCli()).drag(APP, DragRequest(start=(0.0, 0.0), end=(1.0, 1.0)))
        assert result.ok is False


class TestErrors:
    def test_stale_ref_asks_for_fresh_state(self):
        cli = FakeCli()
        be = _backend(cli)
        save = _rec(_snap(be), "Save")
        cli.errors["click"] = _err("click", "STALE_REF")
        result = be.click(APP, save, ClickRequest())
        assert result.ok is False and "call it again" in result.text

    def test_possibly_delivered_failure_says_so(self):
        cli = FakeCli()
        be = _backend(cli)
        save = _rec(_snap(be), "Save")
        cli.errors["click"] = _err("click", "APP_UNRESPONSIVE", delivery="unknown")
        assert "may have been delivered" in be.click(APP, save, ClickRequest()).text

    def test_garbage_output_is_a_refusal_not_an_exception(self):
        cli = FakeCli()
        cli.errors["snapshot"] = "segfault\n"
        result = _backend(cli).snapshot(APP, SnapshotRequest(want_image=False))
        assert isinstance(result, DriverResult) and result.ok is False


class TestInventoryAndKeys:
    def test_list_apps_without_native_uses_visible_windows(self):
        result = _backend(FakeCli()).list_apps()
        assert [(a.name, a.pid, a.window_id) for a in result.apps] == [("Demo", 42, 7)]

    def test_list_apps_prefers_native(self):
        cli = FakeCli()
        _backend(cli, native=FakeNativeHelper()).list_apps()
        assert "list-windows" not in cli.commands()

    def test_launch_without_native_is_refused(self):
        assert _backend(FakeCli()).launch_app("Demo").ok is False

    def test_key_aliases(self):
        assert normalize_key("Command+Option+Control+a") == "cmd+alt+ctrl+a"

    def test_press_key_prefers_native(self):
        cli = FakeCli()
        assert _backend(cli, native=FakeNativeHelper()).press_key(APP, None, "cmd+s").ok
        assert "press" not in cli.commands()

    def test_press_key_targets_window(self):
        cli = FakeCli()
        assert _backend(cli).press_key(APP, None, "cmd+s").ok
        assert cli.calls[-1] == ["press", "--window-id", "w-7", "--", "cmd+s"]


class TestStatus:
    def test_unpinned_binary_is_unsupported(self, monkeypatch):
        monkeypatch.setattr(locate_mod, "verify", lambda path: "SHA-256 mismatch")
        status = _backend(FakeCli()).status()
        assert status.supported is False and "mismatch" in status.reason

    def test_wrong_version_is_unsupported(self, monkeypatch):
        monkeypatch.setattr(locate_mod, "verify", lambda path: None)
        cli = FakeCli()
        cli.errors["--version"] = "agent-desktop 9.9.9\n"
        assert _backend(cli).status().supported is False

    def test_pinned_version_is_supported(self, monkeypatch):
        monkeypatch.setattr(locate_mod, "verify", lambda path: None)
        assert _backend(FakeCli()).status().supported is True


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"binary")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class TestLocate:
    def test_env_var_wins(self, tmp_path, monkeypatch):
        exe = _exe(tmp_path / "x" / "agent-desktop")
        monkeypatch.setenv(locate_mod.ENV_BIN, str(exe))
        assert locate_mod.locate() == str(exe.resolve())

    def test_private_prefix_then_path(self, tmp_path, monkeypatch):
        monkeypatch.delenv(locate_mod.ENV_BIN, raising=False)
        monkeypatch.setattr(locate_mod, "INSTALL_PREFIX", lambda: tmp_path / "prefix")
        monkeypatch.setenv("PATH", str(tmp_path / "bin"))
        # ``shutil.which`` on Windows only finds names with a PATHEXT suffix.
        suffix = ".exe" if os.name == "nt" else ""
        on_path = _exe(tmp_path / "bin" / f"agent-desktop{suffix}")
        assert locate_mod.locate() == str(on_path.resolve())
        private = _exe(tmp_path / "prefix" / "node_modules" / ".bin" / "agent-desktop")
        assert locate_mod.locate() == str(private.resolve())

    def test_install_prefix_is_under_data_home(self):
        from kiro_crew.config.paths import data_home

        assert locate_mod.INSTALL_PREFIX() == data_home() / "tools" / "agent-desktop"

    def test_verify_pins_binary_and_helpers(self, tmp_path, monkeypatch):
        exe = _exe(tmp_path / "agent-desktop")
        assert "mismatch" in (locate_mod.verify(str(exe)) or "")
        monkeypatch.setattr(locate_mod, "_KNOWN_DIGESTS", frozenset({locate_mod.sha256_of(exe)}))
        assert locate_mod.verify(str(exe)) is None
        helper = tmp_path / "agent-desktop-macos-helper"
        helper.write_bytes(b"tampered")
        assert "helper" in (locate_mod.verify(str(exe)) or "")


class TestSelection:
    @pytest.fixture
    def native(self, monkeypatch):
        fake = FakeNativeHelper()
        monkeypatch.setattr(backend_mod, "select_native_helper", lambda: fake)
        monkeypatch.setattr(backend_mod, "_native_capture", lambda: None)
        monkeypatch.setattr(backend_mod, "platform_id_for_current_os", lambda: "macos")
        return fake

    def test_missing_agent_desktop_is_a_typed_refusal(self, monkeypatch, native):
        monkeypatch.setattr(locate_mod, "locate", lambda: None)
        chosen = backend_mod.select_default_backend()
        assert chosen.status().supported is False
        assert chosen.status().reason == backend_mod.AGENT_DESKTOP_MISSING_REASON

    def test_unverified_binary_is_a_typed_refusal(self, monkeypatch, native):
        monkeypatch.setattr(locate_mod, "locate", lambda: "/x")
        monkeypatch.setattr(locate_mod, "verify", lambda p: "SHA-256 mismatch")
        chosen = backend_mod.select_default_backend()
        assert chosen.status().supported is False
        assert "mismatch" in chosen.status().reason

    def test_verified_binary_runs_over_the_native_helper(self, monkeypatch, native, tmp_path):
        monkeypatch.setattr(locate_mod, "locate", lambda: "/x")
        monkeypatch.setattr(locate_mod, "verify", lambda p: None)
        monkeypatch.setattr(locate_mod, "INSTALL_PREFIX", lambda: tmp_path)
        chosen = backend_mod.select_default_backend()
        assert isinstance(chosen, AgentDesktopBackend)
        assert chosen.platform_id == "macos"
        assert chosen._native is native
        assert os.path.isdir(tmp_path / "state")


class _KeySpy(FakeNativeHelper):
    """Native fake that records what each keyboard verb sent."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    def press_key(self, app, key):
        self.sent.append(key)
        return super().press_key(app, key)

    def type_text(self, app, text):
        self.sent.append(text)
        return super().type_text(app, text)


def _win_backend(cli, native=None):
    return AgentDesktopBackend("/fake/agent-desktop", "windows", native=native, runner=cli)


class TestKeyboardVerbsOnAnElement:
    """Focus moves through the agent-desktop ref; the native helper only sends keys."""

    def test_press_key_focuses_through_the_ref_then_sends_native_keys(self):
        cli, spy = FakeCli(), _KeySpy()
        be = _backend(cli, native=spy)
        user = _rec(_snap(be), "User")
        result = be.press_key(APP, user, "cmd+a")
        assert result.ok, result.text
        assert spy.sent == ["cmd+a"]
        assert ["focus", "--session", SESSION_ID, "--", "@s1:e3"] in cli.calls
        assert "keyboard focus moved to element" in result.text

    def test_focus_is_headed_only_on_windows(self):
        cli = FakeCli()
        be = _win_backend(cli, native=_KeySpy())
        assert be.press_key(APP, _rec(_snap(be), "User"), "ctrl+a").ok
        focus = [c for c in cli.calls if c[0] == "focus"]
        assert focus == [["focus", "--session", SESSION_ID, "--headed", "--", "@s1:e3"]]
        assert all("--headed" not in c for c in cli.calls if c[0] != "focus")

    def test_focus_that_does_not_land_sends_nothing(self):
        cli, spy = FakeCli(), _KeySpy()
        cli.focus_lands = False
        be = _backend(cli, native=spy)
        result = be.press_key(APP, _rec(_snap(be), "User"), "cmd+a")
        assert result.ok is False and "did not take keyboard focus" in result.text
        assert spy.sent == []

    def test_secure_field_is_refused_before_focus(self):
        cli, spy = FakeCli(states={"@s1:e3": ["secure"]}), _KeySpy()
        be = _backend(cli, native=spy)
        result = be.press_key(APP, _rec(_snap(be), "User"), "cmd+v")
        assert result.ok is False and "secure (password) field" in result.text
        assert "focus" not in cli.commands() and spy.sent == []

    def test_windows_type_is_focus_plus_native_keys_not_the_cli(self):
        cli, spy = FakeCli(), _KeySpy()
        be = _win_backend(cli, native=spy)
        result = be.type_text(APP, _rec(_snap(be), "User"), "hello")
        assert result.ok, result.text
        assert spy.sent == ["hello"]
        assert "type" not in cli.commands()

    def test_windows_type_without_native_points_at_set_value(self):
        cli = FakeCli()
        be = _win_backend(cli)
        result = be.type_text(APP, _rec(_snap(be), "User"), "hello")
        assert result.ok is False and "computer_set_value" in result.text
        assert "type" not in cli.commands() and "focus" not in cli.commands()

    def test_policy_denied_names_the_global_route(self):
        cli = FakeCli()
        be = _win_backend(cli)
        user = _rec(_snap(be), "User")
        cli.errors["right-click"] = _err("right-click", "POLICY_DENIED")
        result = be.click(APP, user, ClickRequest(button="right"))
        assert result.ok is False and "click_method 'global'" in result.text


class TestLocateOnWindowsLayout:
    def test_package_exe_beats_the_npm_shim(self, tmp_path, monkeypatch):
        """npm's extensionless ``.bin`` shim reads as executable on Windows (no X bit)."""
        monkeypatch.delenv(locate_mod.ENV_BIN, raising=False)
        prefix = tmp_path / "prefix"
        monkeypatch.setattr(locate_mod, "INSTALL_PREFIX", lambda: prefix)
        _exe(prefix / "node_modules" / ".bin" / "agent-desktop")
        exe = _exe(prefix / "node_modules" / "agent-desktop" / "bin" / "agent-desktop.exe")
        assert locate_mod.locate() == str(exe.resolve())
