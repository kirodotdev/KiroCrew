"""The Computer Use setup state machine (every D5 state) and its actions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.computer_use import setup
from kiro_crew.computer_use.driver_runtime import install as install_mod
from kiro_crew.computer_use.driver_runtime import pin
from kiro_crew.computer_use.driver_runtime import session as session_mod
from kiro_crew.computer_use.types import PLATFORM_LINUX, PLATFORM_MACOS, PLATFORM_WINDOWS

SHA = "ab" * 32
INSTALLED = install_mod.InstallState(installed=True, version="0.47.8", binary_sha256=SHA)
MISSING_INSTALL = install_mod.InstallState(installed=False, detail="not installed")
X11 = session_mod.detect_linux({"DISPLAY": ":1", "DBUS_SESSION_BUS_ADDRESS": "x"}, uinput=False)
AQUA = session_mod.SessionInfo(
    kind=session_mod.KIND_AQUA,
    detail="macOS desktop session",
    supported=True,
    capabilities={"observe_tree": True},
)
VERIFIED = {"verify": {"ok": True, "binary_sha256": SHA}}
NOW = 1_000_000.0
TEST_ASSET = pin.PinnedAsset(
    os="linux",
    arch="x86_64",
    name="test-asset.tar.gz",
    size=1,
    sha256="0" * 64,
    binary="cua-driver",
    binary_sha256=SHA,
)


def _status(**kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = dict(
        platform=PLATFORM_LINUX,
        choice="auto",
        keystone_enabled=True,
        locked=False,
        install_state=INSTALLED,
        install_progress={},
        session=X11,
        progress=dict(VERIFIED),
        now=NOW,
    )
    args.update(kw)
    return setup.build_status(**args)


def _mac(perms: dict, **kw: Any) -> dict[str, Any]:
    progress = {**VERIFIED, "permissions": perms, **kw.pop("progress", {})}
    return _status(
        platform=PLATFORM_MACOS, choice="upstream", session=AQUA, progress=progress, **kw
    )


def _steps(doc: dict) -> dict[str, str]:
    return {s["id"]: s["status"] for s in doc["steps"]}


class TestEveryState:
    def test_ready_needs_keystone_and_a_passed_check(self) -> None:
        doc = _status()
        assert doc["state"] == setup.READY
        assert set(_steps(doc).values()) == {setup.STEP_DONE}
        assert [s["id"] for s in doc["steps"]] == ["install", "session", "verify", "ready"]
        assert doc["driver"] == {
            "kind": "upstream",
            "version": "0.47.8",
            "installed": True,
            "healthy": True,
        }
        assert doc["permissions"]["accessibility"] == setup.NOT_APPLICABLE

    def test_disabled_when_the_keystone_is_off(self) -> None:
        doc = _status(keystone_enabled=False)
        assert doc["state"] == setup.DISABLED and _steps(doc)["ready"] == setup.STEP_CURRENT

    def test_disabled_by_policy(self) -> None:
        assert _status(locked=True)["state"] == setup.DISABLED

    def test_not_configured(self) -> None:
        doc = _status(install_state=MISSING_INSTALL, progress={})
        assert doc["state"] == setup.NOT_CONFIGURED and _steps(doc)["install"] == setup.STEP_CURRENT

    def test_installing(self) -> None:
        doc = _status(
            install_state=MISSING_INSTALL,
            install_progress={
                "running": True,
                "stage": "downloading",
                "bytes_done": 5,
                "bytes_total": 10,
            },
        )
        assert doc["state"] == setup.INSTALLING and doc["progress"]["bytes_total"] == 10

    def test_install_failed(self) -> None:
        doc = _status(install_state=MISSING_INSTALL, install_progress={"error": "sha256 mismatch"})
        assert doc["state"] == setup.INSTALL_FAILED and "sha256 mismatch" in doc["limitations"]
        assert _steps(doc)["install"] == setup.STEP_FAILED

    def test_never_checked_is_not_configured_at_verify(self) -> None:
        doc = _status(progress={})
        assert doc["state"] == setup.NOT_CONFIGURED and _steps(doc)["verify"] == setup.STEP_CURRENT
        assert (
            _steps(doc)["install"] == setup.STEP_DONE and _steps(doc)["session"] == setup.STEP_DONE
        )

    def test_a_check_on_an_older_binary_does_not_count(self) -> None:
        doc = _status(progress={"verify": {"ok": True, "binary_sha256": "cd" * 32}})
        assert doc["state"] == setup.NOT_CONFIGURED

    def test_validating(self) -> None:
        assert _status(progress={}, verifying=True)["state"] == setup.VALIDATING

    def test_error_on_a_failed_check(self) -> None:
        doc = _status(
            progress={"verify": {"ok": False, "binary_sha256": SHA, "detail": "no window"}}
        )
        assert doc["state"] == setup.ERROR and "no window" in doc["limitations"]

    def test_error_when_the_driver_fails_its_own_checks(self) -> None:
        doc = _status(supervisor_status={"state": "unavailable", "last_error": "hash mismatch"})
        assert doc["state"] == setup.ERROR and doc["driver"]["healthy"] is False

    def test_temporarily_unavailable_in_windows_session0(self) -> None:
        doc = _status(
            platform=PLATFORM_WINDOWS, choice="upstream", session=session_mod.detect_windows(0)
        )
        assert doc["state"] == setup.TEMPORARILY_UNAVAILABLE
        assert doc["session"]["kind"] == "session0"
        assert _steps(doc)["session"] == setup.STEP_FAILED
        assert not any(doc["capabilities"].values())

    def test_session0_is_reported_before_any_install(self) -> None:
        doc = _status(
            platform=PLATFORM_WINDOWS,
            choice="upstream",
            session=session_mod.detect_windows(0),
            install_state=MISSING_INSTALL,
            progress={},
        )
        assert doc["state"] == setup.TEMPORARILY_UNAVAILABLE
        assert _steps(doc)["install"] == setup.STEP_PENDING
        assert _steps(doc)["session"] == setup.STEP_FAILED

    def test_temporarily_unavailable_on_a_crash_loop(self) -> None:
        assert (
            _status(supervisor_status={"state": "crash_loop"})["state"]
            == setup.TEMPORARILY_UNAVAILABLE
        )

    def test_unsupported_without_an_archive_for_this_host(self) -> None:
        assert _status(has_asset=False)["state"] == setup.UNSUPPORTED

    def test_unsupported_on_wayland_without_xwayland(self) -> None:
        wl = session_mod.detect_linux(
            {"WAYLAND_DISPLAY": "w", "DBUS_SESSION_BUS_ADDRESS": "x"}, uinput=True
        )
        doc = _status(session=wl)
        assert doc["state"] == setup.UNSUPPORTED and doc["session"]["kind"] == "wayland"

    def test_linux_and_windows_never_show_a_permission_step(self) -> None:
        for platform in (PLATFORM_LINUX, PLATFORM_WINDOWS):
            doc = _status(platform=platform, choice="upstream")
            ids = [s["id"] for s in doc["steps"]]
            assert "accessibility" not in ids and "screen_recording" not in ids


class TestMacPermissions:
    def test_never_requested(self) -> None:
        doc = _mac({})
        assert doc["state"] == setup.REQUESTING_ACCESSIBILITY
        step = doc["steps"][1]
        assert step["id"] == "accessibility" and step["action"] == {
            "kind": "open_pane",
            "pane": "accessibility",
        }
        assert doc["permissions"]["owner_name"] == "CuaDriver"

    def test_waiting_for_system_settings_after_a_request(self) -> None:
        doc = _mac(
            {"accessibility": "missing"}, progress={"requested": {"accessibility": NOW - 30}}
        )
        assert doc["state"] == setup.WAITING_FOR_SYSTEM_SETTINGS

    def test_declined(self) -> None:
        doc = _mac(
            {"accessibility": "missing"}, progress={"requested": {"accessibility": NOW - 3600}}
        )
        assert doc["state"] == setup.ACCESSIBILITY_DENIED

    def test_revoked(self) -> None:
        doc = _mac({"accessibility": "missing"}, progress={"ever_granted": {"accessibility": True}})
        assert doc["state"] == setup.PERMISSION_REVOKED and doc["permissions"]["previously_granted"]

    def test_partial_accessibility_only(self) -> None:
        doc = _mac({"accessibility": "granted"})
        assert doc["state"] == setup.REQUESTING_SCREEN_RECORDING
        assert _steps(doc)["accessibility"] == setup.STEP_DONE

    def test_partial_screen_recording_declined(self) -> None:
        doc = _mac(
            {"accessibility": "granted", "screen_recording": "missing"},
            progress={"requested": {"screen_recording": NOW - 3600}},
        )
        assert doc["state"] == setup.SCREEN_RECORDING_DENIED

    def test_restart_required(self) -> None:
        doc = _mac(
            {"accessibility": "granted", "screen_recording": "granted"},
            progress={"restart_required": True},
        )
        assert doc["state"] == setup.RESTART_REQUIRED

    def test_ready_on_mac(self) -> None:
        assert (
            _mac({"accessibility": "granted", "screen_recording": "granted"})["state"]
            == setup.READY
        )

    def test_windows_auto_is_native_until_the_upstream_driver_is_installed(self) -> None:
        win = session_mod.detect_windows(1)
        before = _status(platform=PLATFORM_WINDOWS, session=win, install_state=MISSING_INSTALL)
        assert before["driver"]["kind"] == "native"
        after = _status(platform=PLATFORM_WINDOWS, session=win)
        assert after["driver"]["kind"] == "upstream" and after["state"] == setup.READY

    def test_native_default_on_mac_has_no_driver_steps(self) -> None:
        doc = _status(platform=PLATFORM_MACOS, choice="auto", session=AQUA)
        assert doc["driver"]["kind"] == "native" and doc["state"] == setup.READY
        assert [s["id"] for s in doc["steps"]] == ["verify", "ready"]

    def test_native_mac_shows_its_advisory_grants_and_the_responsible_process(self) -> None:
        # The native driver's grants have no setup step, so the readings and the
        # process to grant must reach the document. Advisory: never a refusal.
        probe = {
            "accessibility": "missing",
            "screen_recording": "granted",
            "responsible_hint": "Grant them to Terminal.",
            "owner_name": "Terminal",
        }
        doc = _status(
            platform=PLATFORM_MACOS, choice="auto", session=AQUA, native_permissions=probe
        )
        assert doc["driver"]["kind"] == "native" and doc["state"] == setup.READY
        assert doc["permissions"]["accessibility"] == setup.MISSING
        assert doc["permissions"]["screen_recording"] == setup.GRANTED
        assert doc["permissions"]["owner_name"] == "Terminal"
        assert "Grant them to Terminal." in doc["limitations"]
        granted = _status(
            platform=PLATFORM_MACOS,
            choice="auto",
            session=AQUA,
            native_permissions={**probe, "accessibility": "granted"},
        )
        assert "Grant them to Terminal." not in granted["limitations"]
        odd = _status(
            platform=PLATFORM_MACOS,
            choice="auto",
            session=AQUA,
            native_permissions={**probe, "accessibility": "unsupported"},
        )
        assert odd["permissions"]["accessibility"] == setup.UNKNOWN

    def test_native_driver_is_not_ready_without_a_desktop_session(self) -> None:
        # A Mac at the login window (no one signed in): the native driver cannot work.
        no_session = session_mod.detect_macos(console_uid=0)
        doc = _status(platform=PLATFORM_MACOS, choice="auto", session=no_session)
        assert doc["driver"]["kind"] == "native"
        assert doc["state"] == setup.TEMPORARILY_UNAVAILABLE
        assert "login window" in doc["user_action"]


class TestMacSession:
    def test_root_owned_console_is_the_login_window_even_for_root(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(session_mod.os, "getuid", lambda: 0, raising=False)
        info = session_mod.detect_macos(console_uid=0)
        assert not info.supported and info.kind == session_mod.KIND_NONE

    def test_own_console_is_an_aqua_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_mod.os, "getuid", lambda: 501, raising=False)
        assert session_mod.detect_macos(console_uid=501).kind == session_mod.KIND_AQUA
        assert not session_mod.detect_macos(console_uid=502).supported


def test_every_state_is_reachable_by_the_tests_above() -> None:
    assert len(setup.STATES) == 16 and len(set(setup.STATES)) == 16


# ── actions ──


class FakeSup:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.restarted = 0

    def call(self, tool: str, args: dict, *, timeout: float, idempotent: bool = False) -> dict:
        self.calls.append(tool)
        if tool == "list_windows":
            return {"structuredContent": {"windows": [{"pid": 5, "window_id": 9}]}}
        if tool == "get_window_state":
            Path(args["screenshot_out_file"]).write_bytes(b"png")
            return {"structuredContent": {"elements": []}}
        if tool == "health_report":
            return {"structuredContent": {"overall": "ok"}}
        if tool == "check_permissions":
            return {"structuredContent": {"accessibility": True, "screen_recording": True}}
        raise AssertionError(f"setup must not call {tool}")

    def restart(self) -> None:
        self.restarted += 1


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> "tuple[Path, FakeSup]":
    install_mod.runtime_root(tmp_path).mkdir(parents=True)
    install_mod.record_path(tmp_path).write_text(
        json.dumps({"version": "0.47.8", "asset": TEST_ASSET.name, "binary": "cua-driver"})
    )
    monkeypatch.setattr(install_mod, "_pinned_for_host", lambda asset: asset or TEST_ASSET)
    vdir = install_mod.version_dir(tmp_path)
    vdir.mkdir(parents=True)
    (vdir / "cua-driver").write_bytes(b"x")
    fake = FakeSup()
    monkeypatch.setattr(setup, "_supervisor", lambda root: fake)
    return tmp_path, fake


class TestActions:
    def test_verify_is_non_destructive_and_persists(self, installed) -> None:  # type: ignore[no-untyped-def]
        installed_home, fake = installed
        doc = setup.run_setup_action("verify", installed_home)
        assert fake.calls == ["list_windows", "get_window_state", "health_report"]
        progress = setup.load_progress(installed_home)
        assert progress["verify"]["ok"] is True and progress["verify"]["binary_sha256"] == SHA
        assert progress["last_step"] == "verify"
        assert not list((install_mod.runtime_root(installed_home) / "capture").iterdir())
        assert doc["state"] in (
            setup.READY,
            setup.DISABLED,
            setup.UNSUPPORTED,
            setup.TEMPORARILY_UNAVAILABLE,
            setup.NOT_CONFIGURED,
        )

    def test_a_policy_lock_runs_nothing_but_reset(
        self, installed, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        installed_home, fake = installed
        from kiro_crew.computer_use import enable_state

        monkeypatch.setattr(enable_state, "locked_by_policy", lambda: True)
        started: list[Path] = []
        monkeypatch.setattr(setup, "_start_install", lambda root: started.append(root))
        for action in ("install", "verify", "recheck", "request_permissions", "restart_driver"):
            setup.run_setup_action(action, installed_home)
        assert fake.calls == [] and fake.restarted == 0 and started == []
        setup.save_progress(installed_home, {"last_step": "verify"})
        setup.run_setup_action("reset", installed_home)
        assert setup.load_progress(installed_home) == {}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_verify_skips_a_denied_window_and_keeps_its_capture_private(
        self, installed
    ) -> None:  # type: ignore[no-untyped-def]
        installed_home, fake = installed
        read: list[int] = []
        original = fake.call

        def call(tool: str, args: dict, *, timeout: float, idempotent: bool = False) -> dict:
            if tool == "list_windows":
                fake.calls.append(tool)
                return {
                    "structuredContent": {
                        "windows": [
                            {
                                "pid": 3,
                                "window_id": 1,
                                "app_name": "firefox",
                                "title": "Settings - Kiro Crew",
                            },
                            {"pid": 5, "window_id": 9, "app_name": "gedit", "title": "notes"},
                        ]
                    }
                }
            if tool == "get_window_state":
                read.append(args["pid"])
            return original(tool, args, timeout=timeout, idempotent=idempotent)

        fake.call = call  # type: ignore[method-assign]
        setup.run_setup_action("verify", installed_home)
        assert read == [5]
        capture = install_mod.runtime_root(installed_home) / "capture"
        assert capture.stat().st_mode & 0o077 == 0

    def test_actions_before_install_do_not_touch_the_driver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeSup()
        monkeypatch.setattr(setup, "_supervisor", lambda root: fake)
        for action in ("verify", "recheck", "request_permissions", "restart_driver"):
            setup.run_setup_action(action, tmp_path)
        assert fake.calls == [] and fake.restarted == 0

    def test_restart_driver_restarts_and_clears_the_flag(self, installed) -> None:  # type: ignore[no-untyped-def]
        installed_home, fake = installed
        setup.save_progress(installed_home, {"restart_required": True})
        setup.run_setup_action("restart_driver", installed_home)
        assert fake.restarted == 1
        assert setup.load_progress(installed_home)["restart_required"] is False

    def test_reset_clears_progress(self, installed) -> None:  # type: ignore[no-untyped-def]
        installed_home, _fake = installed
        setup.save_progress(installed_home, {"last_step": "verify"})
        setup.run_setup_action("reset", installed_home)
        assert setup.load_progress(installed_home) == {}

    def test_unknown_action_is_reported_not_raised(self, tmp_path: Path) -> None:
        doc = setup.run_setup_action("format_disk", tmp_path)
        assert any("unknown setup action" in line for line in doc["limitations"])

    def test_install_runs_in_the_background(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import threading

        gate = threading.Event()
        seen: list[Path] = []

        def fake_install(root):  # type: ignore[no-untyped-def]
            seen.append(root)
            gate.wait(5)
            raise install_mod.InstallError("verifying", "the download's sha256 does not match")

        monkeypatch.setattr(install_mod, "install", fake_install)
        monkeypatch.setattr(install_mod, "current_os", lambda: "linux")
        monkeypatch.setattr(session_mod, "detect", lambda: X11)
        monkeypatch.setattr(setup, "asset_for", lambda os_id: object())
        progress = install_mod.InstallProgress()
        monkeypatch.setattr(install_mod, "PROGRESS", progress)
        doc = setup.run_setup_action("install", tmp_path)
        assert doc["state"] == setup.INSTALLING
        gate.set()
        setup._install_thread.join(5)  # type: ignore[union-attr]
        assert seen == [tmp_path]
        assert "sha256" in setup.load_progress(tmp_path)["install_error"]

    def test_status_never_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(setup, "_keystone", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        doc = setup.get_setup_status("/nonexistent")
        assert doc["state"] == setup.ERROR and doc["steps"] == []

    def test_set_driver_choice_rejects_unknown(self) -> None:
        with pytest.raises(ValueError):
            setup.set_driver_choice("something")


def test_a_foreign_macos_app_is_install_failed_with_its_reason() -> None:
    reason = "another version of CuaDriver is installed at /Applications/CuaDriver.app; remove it"
    doc = _status(
        platform=PLATFORM_MACOS,
        choice="upstream",
        session=AQUA,
        install_state=MISSING_INSTALL,
        progress={"install_error": reason},
    )
    assert doc["state"] == setup.INSTALL_FAILED and reason in doc["limitations"]


def test_the_macos_upstream_driver_is_labelled_a_preview() -> None:
    doc = _status(platform=PLATFORM_MACOS, choice="upstream", session=AQUA)
    assert any("preview" in line for line in doc["limitations"])
    assert any(
        "shared daemon" in line or "shares that daemon" in line for line in doc["limitations"]
    )
    assert not any("preview" in line for line in _status()["limitations"])


def test_reset_removes_the_installed_driver(installed) -> None:  # type: ignore[no-untyped-def]
    home, _fake = installed
    assert install_mod.version_dir(home).exists()
    setup.run_setup_action("reset", home)
    assert not install_mod.version_dir(home).exists()
    assert not install_mod.record_path(home).exists()
