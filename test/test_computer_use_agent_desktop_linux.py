"""Computer use on Linux: agent-desktop with no native driver underneath.

No real agent-desktop and no desktop: a fake CLI answers with envelopes shaped
like the ones agent-desktop 1.0.2 printed in an Ubuntu 24.04 container (Xvfb,
openbox, at-spi2-core, gedit, Firefox). The Linux branch is reached by flipping
``platform_compat`` flags, never ``sys.platform``, so this runs on any CI host.
What is pinned:

* opaque AT-SPI window ids map to stable ints and back into argv;
* an AT-SPI bus that does not answer makes the backend unsupported, with a reason;
* the Linux-only fallbacks: ``type --headed`` only after ``ACTION_NOT_SUPPORTED``
  and only without a native driver; ``set-value`` and wheel ``scroll`` refuse with
  a next step, and scroll never goes ``--headed``;
* ``press_key`` refuses an unfocused element instead of focusing it;
* screenshots come from ``agent-desktop screenshot`` under the same floors;
* selection, the spec-gate predicate and the installer's Linux binary names.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

from kiro_crew import platform_compat
from kiro_crew.computer_use import agent_desktop_install as adi
from kiro_crew.computer_use import agent_desktop_locate as locate_mod
from kiro_crew.computer_use import backend as backend_mod
from kiro_crew.computer_use import capture_agent_desktop
from kiro_crew.computer_use.agent_desktop_driver import SESSION_ID, AgentDesktopBackend
from kiro_crew.computer_use.types import (
    CLICK_METHOD_GLOBAL,
    PLATFORM_LINUX,
    ClickRequest,
    DragRequest,
    SnapshotRequest,
)
from kiro_crew.testing.fake_computer_use import FakeNativeHelper

GEDIT_ID = "w-1.0-org-a11y-atspi-accessible-172"
FIREFOX_ID = "w-1.2-org-a11y-atspi-accessible-2"

TREE = {
    "role": "window",
    "name": "Login Test",
    "bounds": {"x": 0, "y": 0, "width": 1280, "height": 781},
    "children": [
        {
            "role": "textfield",
            "name": "Username",
            "ref_id": "@s1:e1",
            "available_actions": ["SetValue", "TypeText"],
        },
        {
            "role": "textfield",
            "name": "Password",
            "ref_id": "@s1:e2",
            "states": ["secure"],
            "available_actions": ["SetValue", "TypeText"],
        },
        {
            "role": "webarea",
            "name": "Page",
            "ref_id": "@s1:e3",
            "states": ["focused"],
            "available_actions": ["SetFocus", "Scroll"],
        },
    ],
}


def _ok(command, data):
    return json.dumps({"version": "2.5", "ok": True, "command": command, "data": data})


def _err(command, code, message="nope", details=None):
    error = {"code": code, "message": message, "disposition": {"delivery": "not_delivered"}}
    if details:
        error["details"] = details
    return json.dumps({"version": "2.5", "ok": False, "command": command, "error": error})


def _png() -> str:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 32), (200, 10, 10)).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode("ascii")


class LinuxCli:
    """Fake agent-desktop on a Linux desktop. ``answers`` overrides per command."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.answers: dict[str, list[str]] = {}
        #: Refs that hold keyboard focus now; the tree's <focused> node to start with.
        self.focused: set[str] = {"@s1:e3"}

    def __call__(self, argv, env, timeout):
        args = argv[1:]
        self.calls.append(args)
        command = args[0]
        queued = self.answers.get(command)
        if queued:
            return 1, queued.pop(0)
        if command == "focus":
            self.focused = {args[-1]}
        if command == "--version":
            return 0, "agent-desktop 1.0.2\n"
        if command == "status":
            return 0, _ok(command, {"platform": "linux", "supported_surfaces": ["window"]})
        if command == "list-windows":
            return 0, _ok(
                command,
                [
                    {
                        "app_name": "firefox-bin",
                        "pid": 91,
                        "id": FIREFOX_ID,
                        "title": "Login Test — Mozilla Firefox",
                        "visible": True,
                        "accessible": True,
                    },
                    {
                        "app_name": "gedit",
                        "pid": 82,
                        "id": GEDIT_ID,
                        "title": "Untitled Document 1 - gedit",
                        "visible": True,
                        "accessible": True,
                    },
                ],
            )
        if command == "list-apps":
            return 0, _ok(
                command,
                {
                    "apps": [
                        {"bundle_id": "org.gnome.gedit", "name": "gedit", "pid": 82},
                        {"name": "firefox-bin", "pid": 91},
                    ]
                },
            )
        if command == "snapshot":
            return 0, _ok(command, {"complete": True, "tree": TREE})
        if command == "get":
            ref, prop = args[-1], args[args.index("--property") + 1]
            if prop == "states":
                states = ["secure"] if ref == "@s1:e2" else []
                return 0, _ok(
                    command, {"value": states + (["focused"] if ref in self.focused else [])}
                )
            return 0, _ok(command, {"value": "textfield"})
        if command == "screenshot":
            return 0, _ok(command, {"format": "png", "width": 64, "height": 32, "data": _png()})
        return 0, _ok(command, {"action": command})

    def commands(self):
        return [c[0] for c in self.calls]


def _backend(cli, *, native=None, cli_capture=False):
    return AgentDesktopBackend(
        "/fake/agent-desktop", PLATFORM_LINUX, native=native, runner=cli, cli_capture=cli_capture
    )


def _firefox(be):
    return next(a for a in be.list_apps().apps if a.name == "firefox-bin")


def _walk(be, app, *, want_image=False):
    result = be.snapshot(app, SnapshotRequest(want_image=want_image))
    assert result.ok, result.text
    return result.snapshot


def _rec(snap, title):
    return next(e for e in snap.elements if e.title == title)


class TestOpaqueWindowIds:
    def test_list_apps_maps_atspi_ids_to_ints_and_joins_bundle_ids(self):
        apps = _backend(LinuxCli()).list_apps().apps
        assert [(a.name, a.bundle_id) for a in apps] == [
            ("firefox-bin", ""),
            ("gedit", "org.gnome.gedit"),
        ]
        assert all(a.window_id > 0 for a in apps)
        assert len({a.window_id for a in apps}) == 2

    def test_the_int_is_stable_across_backends(self):
        first = _firefox(_backend(LinuxCli())).window_id
        assert _firefox(_backend(LinuxCli())).window_id == first

    def test_snapshot_and_press_address_the_opaque_id(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        snap = _walk(be, app)
        assert cli.calls[-1][:3] == ["snapshot", "--window-id", FIREFOX_ID]
        assert be.press_key(app, _rec(snap, "Page"), "tab").ok
        assert cli.calls[-1] == ["press", "--window-id", FIREFOX_ID, "--", "tab"]

    def test_failed_list_apps_keeps_the_windows(self):
        cli = LinuxCli()
        cli.answers["list-apps"] = [_err("list-apps", "INTERNAL")]
        assert len(_backend(cli).list_apps().apps) == 2


class TestStatus:
    def test_bus_that_answers_is_supported(self, monkeypatch):
        monkeypatch.setattr(locate_mod, "verify", lambda path: None)
        assert _backend(LinuxCli()).status().supported is True

    def test_empty_surfaces_means_no_accessibility_bus(self, monkeypatch):
        monkeypatch.setattr(locate_mod, "verify", lambda path: None)
        cli = LinuxCli()
        cli.answers["status"] = [_ok("status", {"supported_surfaces": []})]
        # answers are served with rc=1, which _call ignores: the envelope decides.
        status = _backend(cli).status()
        assert status.supported is False
        assert "accessibility bus" in status.reason

    def test_a_bus_that_comes_up_later_is_seen_without_a_restart(self, monkeypatch):
        monkeypatch.setattr(locate_mod, "verify", lambda path: None)
        cli = LinuxCli()
        answer = {"surfaces": []}

        def bus(argv, env, timeout):
            if argv[1] == "status":
                return 0, _ok("status", {"supported_surfaces": answer["surfaces"]})
            return cli(argv, env, timeout)

        be = _backend(bus)
        assert be.status().supported is False
        answer["surfaces"] = ["window"]
        assert be.status().supported is True

    def test_the_bus_is_not_asked_when_a_native_driver_exists(self, monkeypatch):
        monkeypatch.setattr(locate_mod, "verify", lambda path: None)
        cli = LinuxCli()
        assert _backend(cli, native=FakeNativeHelper()).status().supported is True
        assert "status" not in cli.commands()


class TestInputFallbacks:
    def test_type_retries_headed_only_after_not_supported(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Username")
        cli.answers["type"] = [_err("type", "ACTION_NOT_SUPPORTED", "applies none of them")]
        result = be.type_text(app, rec, "alice")
        assert result.ok and "keyboard input" in result.text
        types = [c for c in cli.calls if c[0] == "type"]
        assert len(types) == 2
        assert "--headed" not in types[0] and "--headed" in types[1]
        assert types[1][-2:] == ["@s1:e1", "alice"]
        # The retry moves focus first and reads it back, as the other keyboard verbs do.
        assert ["focus", "--session", SESSION_ID, "--headed", "--", "@s1:e1"] in cli.calls

    def test_type_retry_sends_nothing_when_focus_does_not_land(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Username")
        cli.answers["type"] = [_err("type", "ACTION_NOT_SUPPORTED", "applies none of them")]
        original = cli.__call__

        def no_focus(argv, env, timeout):
            if argv[1] == "focus":
                cli.calls.append(argv[1:])
                return 0, _ok("focus", {"action": "focus"})
            return original(argv, env, timeout)

        be._run = no_focus
        result = be.type_text(app, rec, "alice")
        assert result.ok is False and "did not take keyboard focus" in result.text
        assert len([c for c in cli.calls if c[0] == "type"]) == 1

    def test_type_is_not_retried_when_the_first_write_may_have_landed(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Username")
        landed = json.loads(_err("type", "ACTION_NOT_SUPPORTED", "applies none of them"))
        landed["error"]["disposition"] = {"delivery": "delivered_unverified"}
        cli.answers["type"] = [json.dumps(landed)]
        result = be.type_text(app, rec, "alice")
        assert result.ok is False and "may have been delivered" in result.text
        assert len([c for c in cli.calls if c[0] == "type"]) == 1

    def test_type_is_not_retried_over_a_native_driver(self):
        cli = LinuxCli()
        be = _backend(cli, native=FakeNativeHelper())
        snap = _walk(be, _firefox(_backend(LinuxCli())))
        app = snap.app
        cli.answers["type"] = [_err("type", "ACTION_NOT_SUPPORTED")]
        assert be.type_text(app, _rec(snap, "Username"), "x").ok is False
        assert [c for c in cli.calls if c[0] == "type" and "--headed" in c] == []

    def test_type_other_failures_are_not_retried(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Username")
        cli.answers["type"] = [_err("type", "ACTION_FAILED")]
        assert be.type_text(app, rec, "x").ok is False
        assert len([c for c in cli.calls if c[0] == "type"]) == 1

    def test_secure_field_is_refused_before_either_attempt(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Password")
        assert be.type_text(app, rec, "hunter2").ok is False
        assert "type" not in cli.commands()

    def test_set_value_not_supported_points_at_type_text(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Username")
        cli.answers["set-value"] = [_err("set-value", "ACTION_NOT_SUPPORTED")]
        result = be.set_value(app, rec, "bob")
        assert result.ok is False and "computer_type_text" in result.text

    def test_wheel_scroll_refuses_and_never_goes_headed(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Page")
        cli.answers["scroll"] = [_err("scroll", "POLICY_DENIED", "Physical pointer input")]
        result = be.scroll(app, rec, "down", 1)
        assert result.ok is False and "pagedown" in result.text
        assert [c for c in cli.calls if c[0] == "scroll" and "--headed" in c] == []


class TestPressKey:
    def test_unfocused_element_is_refused_without_focusing_it(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        result = be.press_key(app, _rec(_walk(be, app), "Username"), "return")
        assert result.ok is False and "not focused" in result.text
        assert "focus" not in cli.commands() and "press" not in cli.commands()

    def test_background_window_says_bring_it_forward(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Page")
        cli.answers["press"] = [
            _err("press", "STALE_REF", details={"cause": "exact_window_focus_unconfirmed"})
        ]
        result = be.press_key(app, rec, "tab")
        assert result.ok is False
        assert "to the front" in result.text and "call it again" not in result.text

    def test_focus_that_moved_since_the_snapshot_sends_nothing(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Page")
        cli.focused = {"@s1:e2"}
        result = be.press_key(app, rec, "ctrl+v")
        assert result.ok is False and "no longer holds keyboard focus" in result.text
        assert "press" not in cli.commands()


class TestRealPointerWithNoHelper:
    """Linux has no window list that covers apps without accessibility, so a
    real-pointer gesture cannot be confined and is refused."""

    def test_a_global_click_is_refused_and_nothing_is_sent(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        req = ClickRequest(method=CLICK_METHOD_GLOBAL, point=(100.0, 100.0))
        result = be.click(app, None, req)
        assert result.ok is False and "cannot be confined" in result.text
        assert "mouse-click" not in cli.commands()

    def test_a_global_drag_is_refused_and_nothing_is_sent(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        req = DragRequest(start=(10.0, 10.0), end=(50.0, 10.0), method=CLICK_METHOD_GLOBAL)
        result = be.drag(app, req)
        assert result.ok is False and "cannot be confined" in result.text
        assert "drag" not in cli.commands()

    def test_a_headed_element_click_is_refused(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Username")
        result = be.click(app, rec, ClickRequest(method=CLICK_METHOD_GLOBAL, button="right"))
        # The refusal must not send the model to the x/y form, which refuses too.
        assert result.ok is False and "element_index" in result.text
        assert "pass x and y" not in result.text

    def test_a_double_click_on_an_element_names_no_global_route(self):
        cli = LinuxCli()
        be = _backend(cli)
        app = _firefox(be)
        rec = _rec(_walk(be, app), "Username")
        result = be.click(app, rec, ClickRequest(count=2))
        assert result.ok is False and "'global'" not in result.text


class TestCliCapture:
    @pytest.fixture(autouse=True)
    def _shots(self, monkeypatch, tmp_path):
        from kiro_crew.computer_use import capture_macos, screencast

        monkeypatch.setattr(capture_macos, "shots_dir", lambda: str(tmp_path / "shots"))
        monkeypatch.setattr(screencast, "emit_snapshot_frame", lambda snap: None)

    def test_screenshot_is_taken_through_the_cli_and_persisted(self):
        cli = LinuxCli()
        tree = dict(TREE, children=[c for c in TREE["children"] if c["name"] != "Password"])
        cli.answers["snapshot"] = [_ok("snapshot", {"complete": True, "tree": tree})]
        be = _backend(cli, cli_capture=True)
        snap = _walk(be, _firefox(be), want_image=True)
        assert ["screenshot", "--window-id", FIREFOX_ID] == cli.calls[-1]
        assert snap.image_path.endswith(".jpeg") and snap.image_jpeg[:2] == b"\xff\xd8"
        assert (snap.image_width, snap.image_height) == (64, 32)

    def test_secure_window_is_never_shot(self):
        cli = LinuxCli()
        be = _backend(cli, cli_capture=True)
        snap = _walk(be, _firefox(be), want_image=True)
        assert snap.has_secure and not snap.image_path
        assert "screenshot" not in cli.commands()

    def test_a_failed_screenshot_leaves_the_tree(self):
        cli = LinuxCli()
        tree = dict(TREE, children=TREE["children"][:1])
        cli.answers["snapshot"] = [_ok("snapshot", {"complete": True, "tree": tree})]
        cli.answers["screenshot"] = [_err("screenshot", "ACTION_FAILED", "covered")]
        be = _backend(cli, cli_capture=True)
        snap = _walk(be, _firefox(be), want_image=True)
        assert snap.elements and not snap.image_path

    @pytest.mark.parametrize("data", [None, {"format": "jpeg"}, {"format": "png", "data": "%%"}])
    def test_malformed_envelopes_yield_no_bytes(self, data):
        assert capture_agent_desktop._png_bytes(data) == b""


def _pin(monkeypatch, *, linux=False, macos=False, windows=False):
    monkeypatch.setattr(platform_compat, "IS_LINUX", linux)
    monkeypatch.setattr(platform_compat, "IS_MACOS", macos)
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", windows)


class TestLinuxSelection:
    def test_verified_binary_builds_agent_desktop_with_no_native(self, monkeypatch, tmp_path):
        _pin(monkeypatch, linux=True)
        monkeypatch.setattr(locate_mod, "locate", lambda: "/x")
        monkeypatch.setattr(locate_mod, "verify", lambda p: None)
        monkeypatch.setattr(locate_mod, "INSTALL_PREFIX", lambda: tmp_path)
        chosen = backend_mod.select_default_backend()
        assert isinstance(chosen, AgentDesktopBackend)
        assert chosen.platform_id == PLATFORM_LINUX
        assert chosen._native is None and chosen._capture is None and chosen._cli_capture

    def test_without_agent_desktop_the_reason_says_install_it(self, monkeypatch):
        _pin(monkeypatch, linux=True)
        monkeypatch.setattr(locate_mod, "locate", lambda: None)
        status = backend_mod.select_default_backend().status()
        assert status.supported is False and "install" in status.reason

    @pytest.mark.parametrize(
        "flags",
        [{"linux": True}, {"macos": True}, {"windows": True}],
        ids=["linux", "macos", "windows"],
    )
    @pytest.mark.parametrize("located", [None, "/x"])
    def test_spec_gate_predicate_does_not_wait_for_the_install(self, monkeypatch, flags, located):
        # The server is emitted before agent-desktop is installed, so an install
        # from Settings needs no agent-spec rebuild; the tools refuse in-band until then.
        _pin(monkeypatch, **flags)
        monkeypatch.setattr(locate_mod, "locate", lambda: located)
        assert backend_mod.platform_could_be_supported() is True

    def test_spec_gate_predicate_is_false_on_an_os_with_no_driver(self, monkeypatch):
        _pin(monkeypatch)
        assert backend_mod.platform_could_be_supported() is False


class TestInstallerOnLinux:
    @pytest.mark.parametrize("machine,arch", [("x86_64", "x64"), ("aarch64", "arm64")])
    def test_binary_names(self, monkeypatch, machine, arch):
        _pin(monkeypatch, linux=True)
        monkeypatch.setattr(adi.platform, "machine", lambda: machine)
        assert adi.platform_binary_name() == f"agent-desktop-linux-{arch}"
        helper = adi._helper_binary_name()
        assert helper == f"agent-desktop-linux-clipboard-owner-linux-{arch}"
        assert helper in locate_mod.PINNED_SHA256
        assert adi.platform_binary_name() in locate_mod.PINNED_SHA256

    def test_place_copies_cli_and_clipboard_helper(self, monkeypatch, tmp_path):
        _pin(monkeypatch, linux=True)
        monkeypatch.setattr(adi.platform, "machine", lambda: "x86_64")
        monkeypatch.setattr(adi, "package_dir", lambda: tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "agent-desktop-linux-x64").write_bytes(b"cli")
        (bin_dir / "agent-desktop-linux-clipboard-owner-linux-x64").write_bytes(b"helper")
        ok, detail = adi.place_platform_binary()
        assert ok, detail
        assert (bin_dir / "agent-desktop").read_bytes() == b"cli"
        assert (bin_dir / "agent-desktop-linux-clipboard-owner").read_bytes() == b"helper"
        assert "agent-desktop-linux-clipboard-owner" in locate_mod.HELPER_NAMES
