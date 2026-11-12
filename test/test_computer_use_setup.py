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
    )
    args.update(kw)
    return setup.build_status(**args)


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
        assert (
            doc["state"] == setup.INSTALL_FAILED and doc["problem"]["message"] == "sha256 mismatch"
        )
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
        assert doc["state"] == setup.ERROR and doc["problem"]["message"] == "no window"

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


class TestMacNative:
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

    def test_the_upstream_driver_is_not_offered_on_mac(self) -> None:
        # Before or after an install, ``driver: upstream`` on macOS is refused
        # with a plain reason: no install step, no permission step, no capability.
        for install_state in (MISSING_INSTALL, INSTALLED):
            doc = _status(
                platform=PLATFORM_MACOS,
                choice="upstream",
                session=AQUA,
                install_state=install_state,
            )
            assert doc["state"] == setup.UNSUPPORTED
            assert doc["driver"] == {
                "kind": "upstream",
                "version": "",
                "installed": False,
                "healthy": False,
            }
            assert [s["id"] for s in doc["steps"]] == ["verify", "ready"]
            assert _steps(doc)["verify"] == setup.STEP_FAILED
            assert "not offered on macOS" in doc["user_action"]
            assert doc["problem"]["code"] == setup.PROBLEM_UNSUPPORTED_PLATFORM
            assert not any(doc["capabilities"].values())
            assert doc["permissions"]["owner_name"] == ""


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
            assert "screenshot_out_file" not in args, "the verify capture must stay off disk"
            return {
                "content": [{"type": "image", "mimeType": "image/png", "data": "cG5n"}],
                "structuredContent": {"elements": []},
            }
        if tool == "health_report":
            return {"structuredContent": {"overall": "ok"}}
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
        # The capture came back inline: no capture file or directory exists.
        assert not (install_mod.runtime_root(installed_home) / "capture").exists()
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
        for action in ("install", "verify", "recheck", "restart_driver"):
            setup.run_setup_action(action, installed_home)
        assert fake.calls == [] and fake.restarted == 0 and started == []
        setup.save_progress(installed_home, {"last_step": "verify"})
        setup.run_setup_action("reset", installed_home)
        assert setup.load_progress(installed_home) == {}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_verify_skips_a_denied_window_and_writes_no_capture(
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
        assert not (install_mod.runtime_root(installed_home) / "capture").exists()

    def test_actions_before_install_do_not_touch_the_driver(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeSup()
        monkeypatch.setattr(setup, "_supervisor", lambda root: fake)
        for action in ("verify", "recheck", "restart_driver"):
            setup.run_setup_action(action, tmp_path)
        assert fake.calls == [] and fake.restarted == 0

    def test_restart_driver_restarts_it(self, installed) -> None:  # type: ignore[no-untyped-def]
        installed_home, fake = installed
        setup.run_setup_action("restart_driver", installed_home)
        assert fake.restarted == 1
        assert fake.calls == []
        assert setup.load_progress(installed_home)["last_step"] == "session"

    def test_permission_requests_are_not_an_action(self, tmp_path: Path) -> None:
        # Only the upstream macOS driver asked for grants; it is not offered.
        assert "request_permissions" not in setup.ACTIONS
        doc = setup.run_setup_action("request_permissions", tmp_path)
        assert "unknown setup action" in doc["problem"]["message"]

    def test_reset_is_refused_while_an_install_runs(
        self, installed, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        # Removing the tree under a running install would fail its final rename
        # and republish an install error over the reset.
        installed_home, _fake = installed
        progress = install_mod.InstallProgress()
        progress.running = True
        monkeypatch.setattr(install_mod, "PROGRESS", progress)
        removed: list[Path] = []
        monkeypatch.setattr(install_mod, "uninstall", lambda root: removed.append(root) or "")
        setup.save_progress(installed_home, {"last_step": "install"})
        doc = setup.run_setup_action("reset", installed_home)
        assert removed == []
        assert "still installing" in doc["problem"]["message"]
        assert setup.load_progress(installed_home) == {"last_step": "install"}

    def test_reset_clears_progress(self, installed) -> None:  # type: ignore[no-untyped-def]
        installed_home, _fake = installed
        setup.save_progress(installed_home, {"last_step": "verify"})
        setup.run_setup_action("reset", installed_home)
        assert setup.load_progress(installed_home) == {}

    def test_unknown_action_is_reported_not_raised(self, tmp_path: Path) -> None:
        doc = setup.run_setup_action("format_disk", tmp_path)
        assert "unknown setup action" in doc["problem"]["message"]
        assert not any("unknown setup action" in line for line in doc["limitations"])

    def test_install_and_reset_rebuild_the_shared_backend(
        self, installed, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # type: ignore[no-untyped-def]
        # Windows ``auto`` switches driver when the upstream one is installed or
        # removed, so the process-wide backend must be rebuilt both times.
        installed_home, _fake = installed
        from kiro_crew.computer_use import backend as backend_mod

        resets: list[int] = []
        monkeypatch.setattr(backend_mod, "reset_shared_backend", lambda: resets.append(1))
        # Like the real install, the fake clears ``running`` when it ends.
        monkeypatch.setattr(
            install_mod, "install", lambda root: setattr(install_mod.PROGRESS, "running", False)
        )
        monkeypatch.setattr(install_mod, "PROGRESS", install_mod.InstallProgress())
        monkeypatch.setattr(setup, "_install_thread", None)
        setup._start_install(installed_home)
        setup._install_thread.join(5)  # type: ignore[union-attr]
        assert resets == [1]
        setup.run_setup_action("reset", installed_home)
        assert resets == [1, 1]

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


def test_reset_removes_the_installed_driver(installed) -> None:  # type: ignore[no-untyped-def]
    home, _fake = installed
    assert install_mod.version_dir(home).exists()
    setup.run_setup_action("reset", home)
    assert not install_mod.version_dir(home).exists()
    assert not install_mod.record_path(home).exists()


class TestProgressReadForUpdate:
    """A rewrite of the progress file never starts from an unreadable one."""

    def test_an_unreadable_file_abandons_the_update(self, tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
        setup.save_progress(tmp_path, {"last_step": "install"})
        real_read = Path.read_text

        def denied(self: Path, *args: Any, **kwargs: Any) -> str:
            if self.name == setup.PROGRESS_FILE:
                raise PermissionError(13, "denied")
            return real_read(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", denied)
        assert setup.load_progress(tmp_path) == {}
        with pytest.raises(PermissionError):
            setup._update_progress(tmp_path, last_step="session")
        monkeypatch.setattr(Path, "read_text", real_read)
        assert setup.load_progress(tmp_path) == {"last_step": "install"}

    def test_a_missing_file_reads_as_empty_for_an_update(self, tmp_path: Path) -> None:
        assert setup._update_progress(tmp_path, last_step="session") == {"last_step": "session"}


_WIN_SESSION = session_mod.detect_windows(1)
_SESSION_FOR = {PLATFORM_LINUX: X11, PLATFORM_MACOS: AQUA, PLATFORM_WINDOWS: _WIN_SESSION}


@pytest.mark.parametrize(
    ("platform", "choice", "kind", "state"),
    [
        # Linux has no native driver: choosing it must never read as working.
        (PLATFORM_LINUX, "native", "native", setup.UNSUPPORTED),
        (PLATFORM_LINUX, "auto", "upstream", setup.READY),
        (PLATFORM_LINUX, "upstream", "upstream", setup.READY),
        (PLATFORM_MACOS, "native", "native", setup.READY),
        (PLATFORM_MACOS, "auto", "native", setup.READY),
        # The upstream driver is not offered on macOS yet.
        (PLATFORM_MACOS, "upstream", "upstream", setup.UNSUPPORTED),
        (PLATFORM_WINDOWS, "native", "native", setup.READY),
        (PLATFORM_WINDOWS, "auto", "upstream", setup.READY),
        (PLATFORM_WINDOWS, "upstream", "upstream", setup.READY),
        ("unknown", "native", "native", setup.UNSUPPORTED),
        ("unknown", "auto", "native", setup.UNSUPPORTED),
    ],
)
def test_every_platform_and_driver_pair_reports_what_the_backend_can_do(
    platform: str, choice: str, kind: str, state: str
) -> None:
    session = _SESSION_FOR.get(platform, X11)
    doc = _status(platform=platform, choice=choice, session=session)
    assert doc["driver"]["kind"] == kind
    assert doc["state"] == state
    if state == setup.UNSUPPORTED:
        # Nothing claims a usable driver or capability on a refused pair.
        assert doc["driver"]["installed"] is False
        assert not any(doc["capabilities"].values())
        assert doc["user_action"]


def test_linux_native_names_the_switch_to_the_upstream_driver() -> None:
    doc = _status(platform=PLATFORM_LINUX, choice="native")
    assert doc["state"] == setup.UNSUPPORTED
    assert "no native Linux driver" in doc["user_action"]
    assert _steps(doc)["verify"] == "failed"


# ── the problem field: failure reasons travel there, never in ``limitations`` ──

_FAILURE_CASES = {
    "linux_native": (
        dict(platform=PLATFORM_LINUX, choice="native"),
        setup.UNSUPPORTED,
        setup.PROBLEM_NO_NATIVE_DRIVER,
        False,
    ),
    "no_asset": (
        dict(has_asset=False),
        setup.UNSUPPORTED,
        setup.PROBLEM_UNSUPPORTED_PLATFORM,
        False,
    ),
    "session0": (
        dict(platform=PLATFORM_WINDOWS, session=session_mod.detect_windows(0)),
        setup.TEMPORARILY_UNAVAILABLE,
        setup.PROBLEM_NO_DESKTOP_SESSION,
        True,
    ),
    "wayland_no_x": (
        dict(session=session_mod.detect_linux({"WAYLAND_DISPLAY": "wayland-0"}, uinput=False)),
        setup.UNSUPPORTED,
        setup.PROBLEM_UNSUPPORTED_SESSION,
        False,
    ),
    "install_error": (
        dict(install_state=MISSING_INSTALL, progress={"install_error": "sha256 mismatch"}),
        setup.INSTALL_FAILED,
        setup.PROBLEM_INSTALL_FAILED,
        True,
    ),
    "install_blocked": (
        dict(install_state=MISSING_INSTALL, progress={"install_blocked": "signal 9"}),
        setup.INSTALL_FAILED,
        setup.PROBLEM_INSTALL_BLOCKED,
        True,
    ),
    "launch_blocked": (
        dict(blocked="signal 9"),
        setup.TEMPORARILY_UNAVAILABLE,
        setup.PROBLEM_LAUNCH_BLOCKED,
        True,
    ),
    "crash_loop": (
        dict(supervisor_status={"state": "crash_loop"}),
        setup.TEMPORARILY_UNAVAILABLE,
        setup.PROBLEM_CRASH_LOOP,
        True,
    ),
    "driver_check": (
        dict(supervisor_status={"state": "unavailable", "last_error": "hash mismatch"}),
        setup.ERROR,
        setup.PROBLEM_DRIVER_CHECK_FAILED,
        True,
    ),
    "verify_failed": (
        dict(
            progress={
                "verify": {"ok": False, "binary_sha256": SHA, "detail": "no on-screen window"}
            }
        ),
        setup.ERROR,
        setup.PROBLEM_VERIFY_FAILED,
        True,
    ),
    "macos_upstream": (
        dict(platform=PLATFORM_MACOS, choice="upstream", session=AQUA),
        setup.UNSUPPORTED,
        setup.PROBLEM_UNSUPPORTED_PLATFORM,
        False,
    ),
}


@pytest.mark.parametrize("case", sorted(_FAILURE_CASES))
def test_every_failure_state_carries_a_problem_and_no_failure_limitation(case: str) -> None:
    kw, state, code, recoverable = _FAILURE_CASES[case]
    doc = _status(**kw)
    assert doc["state"] == state
    problem = doc["problem"]
    assert problem["code"] == code and problem["code"] in setup.PROBLEM_CODES
    assert problem["message"] and problem["recoverable"] is recoverable
    # The failure reason is never also a "limitation".
    assert problem["message"] not in doc["limitations"]


def test_the_failure_table_covers_every_failure_state() -> None:
    covered = {state for _kw, state, _code, _r in _FAILURE_CASES.values()}
    # The grant failures belong to a driver that owns its own macOS grants; the
    # D5 contract keeps them, but no driver offered today reports them.
    assert setup.FAILURE_STATES - covered == {
        setup.ACCESSIBILITY_DENIED,
        setup.SCREEN_RECORDING_DENIED,
        setup.PERMISSION_REVOKED,
    }


def test_a_specific_failure_reason_is_the_message_and_user_action_is_the_next_step() -> None:
    doc = _status(install_state=MISSING_INSTALL, progress={"install_error": "sha256 mismatch"})
    assert doc["problem"]["message"] == "sha256 mismatch"
    assert doc["user_action"] == "Try the install again."
    assert "sha256 mismatch" not in doc["limitations"]


@pytest.mark.parametrize(
    "kw",
    [
        dict(),  # ready
        dict(install_state=MISSING_INSTALL),  # not configured
        dict(install_progress={"running": True}),  # installing
        dict(verifying=True),  # validating
        dict(keystone_enabled=False),  # disabled
    ],
)
def test_non_failure_states_carry_no_problem(kw: dict) -> None:
    doc = _status(**kw)
    assert doc["state"] not in setup.FAILURE_STATES
    assert doc["problem"] is None


def test_the_handler_allowlists_exactly_the_setup_problem_codes() -> None:
    from kiro_crew.dashboard.handlers import computer_use_setup as handler

    assert handler._PROBLEM_CODES == frozenset(setup.PROBLEM_CODES)
    assert handler._FAILURE_STATES == setup.FAILURE_STATES
