"""A driver the operating system stops at launch (endpoint security software).

Seen on a managed Mac: a signed, notarized ``cua-driver`` is SIGKILLed the moment
it starts. That is reported as itself (a typed :class:`DriverBlocked`, a plain
reason, ``install_failed`` / ``temporarily_unavailable``), never as a version
mismatch, and never retried in a loop. The fake binary here kills itself with
SIGKILL.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from kiro_crew.computer_use import setup
from kiro_crew.computer_use.driver_runtime import install as install_mod
from kiro_crew.computer_use.driver_runtime import session as session_mod
from kiro_crew.computer_use.driver_runtime import supervisor as sup_mod
from kiro_crew.computer_use.driver_runtime.pin import OS_LINUX, OS_MACOS, OS_WINDOWS
from kiro_crew.computer_use.driver_runtime.supervisor import DriverSupervisor, DriverUnavailable
from kiro_crew.computer_use.types import PLATFORM_LINUX, PLATFORM_MACOS, PLATFORM_WINDOWS

posix_only = pytest.mark.skipif(os.name == "nt", reason="the fake binary kills itself with SIGKILL")

SHA = "ab" * 32
INSTALLED = install_mod.InstallState(installed=True, version="0.47.8", binary_sha256=SHA)
MISSING = install_mod.InstallState(installed=False, detail="not installed")
X11 = session_mod.detect_linux({"DISPLAY": ":1", "DBUS_SESSION_BUS_ADDRESS": "x"}, uinput=False)
WIN = session_mod.detect_windows(1)
VERIFIED = {"verify": {"ok": True, "binary_sha256": SHA}}


def _killed_binary(tmp_path: Path) -> Path:
    path = tmp_path / "cua-driver"
    path.write_text("#!/bin/sh\nkill -9 $$\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@posix_only
def test_a_sigkilled_version_check_is_a_block_not_a_version_mismatch(tmp_path: Path) -> None:
    with pytest.raises(install_mod.DriverBlocked) as info:
        install_mod.check_version(_killed_binary(tmp_path), tmp_path / "home")
    assert info.value.how == "signal 9"
    assert info.value.stage == install_mod.STAGE_BLOCKED
    assert "stopped as soon as it started (signal 9)" in info.value.detail
    assert "version" not in info.value.detail


@pytest.mark.parametrize(
    ("os_id", "rc", "error", "how"),
    [
        (OS_MACOS, -9, None, "signal 9"),
        (OS_LINUX, -15, None, "signal 15"),
        (OS_LINUX, 1, None, None),
        (OS_MACOS, 0, None, None),
        (
            OS_MACOS,
            None,
            PermissionError(1, "Operation not permitted"),
            "the system refused to start it",
        ),
        (OS_LINUX, None, FileNotFoundError(2, "missing"), None),
        (OS_WINDOWS, 0xC0000022, None, "access denied"),
        (OS_WINDOWS, -1073741790, None, "access denied"),  # the same code, signed
        (OS_WINDOWS, 0xC0000428, None, "invalid image hash"),
        (OS_WINDOWS, 1, None, None),
    ],
)
def test_blocked_how_names_only_a_launch_the_system_refused(
    os_id: str, rc: "int | None", error: "OSError | None", how: "str | None"
) -> None:
    assert install_mod.blocked_how(rc, error, os_id=os_id) == how


def test_a_windows_spawn_refused_by_policy_is_a_block() -> None:
    err = OSError(13, "blocked")
    err.winerror = 1260  # type: ignore[attr-defined]
    assert install_mod.blocked_how(error=err, os_id=OS_WINDOWS) == "blocked by policy"


def test_the_message_offers_the_built_in_driver_only_where_one_exists() -> None:
    assert install_mod.blocked_message("access denied", OS_WINDOWS).endswith(
        "Ask your IT team to allow the open-source driver, or keep using the built-in driver."
    )
    assert install_mod.blocked_message("signal 9", OS_LINUX).endswith(
        "allow the open-source driver."
    )


@posix_only
def test_the_supervisor_reports_the_block_latches_it_and_never_retries(tmp_path: Path) -> None:
    home = tmp_path / "home"
    binary = _killed_binary(tmp_path)
    spawned: list[list[str]] = []

    def spawn(argv, env, cwd):  # type: ignore[no-untyped-def]
        spawned.append(list(argv))
        return sup_mod._default_spawn(argv, env, cwd)

    sup = DriverSupervisor(
        home, resolve_binary=lambda: (binary, ""), spawn=spawn, mcp_args=(), backoff=(0.0,)
    )
    try:
        with pytest.raises(DriverUnavailable, match=r"stopped as soon as it started \(signal 9\)"):
            sup.call("list_windows", {})
        assert sup.status()["blocked"] == "signal 9"
        # Latched: the next call refuses with the same reason and starts nothing.
        with pytest.raises(DriverUnavailable, match="Security software"):
            sup.call("list_windows", {})
        assert len(spawned) == 1
        # Only an explicit restart allows one more attempt.
        sup.restart()
        assert sup.status()["blocked"] == ""
        with pytest.raises(DriverUnavailable):
            sup.call("list_windows", {})
        assert len(spawned) == 2
    finally:
        sup.stop()


def test_a_successful_install_clears_a_running_supervisors_latch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup = DriverSupervisor(tmp_path / "home", resolve_binary=lambda: (Path("/x"), ""))
    sup._blocked = "signal 9"
    monkeypatch.setattr(sup_mod, "_shared", sup)
    monkeypatch.setattr(install_mod, "install", lambda root: None)
    monkeypatch.setattr(install_mod, "PROGRESS", install_mod.InstallProgress())
    monkeypatch.setattr(setup, "_install_thread", None)
    monkeypatch.setattr(setup, "_reset_backend", lambda: None)
    setup._start_install(tmp_path)
    setup._install_thread.join(5)  # type: ignore[union-attr]
    assert sup.status()["blocked"] == ""


def test_a_blocked_resolve_is_reported_as_the_block(tmp_path: Path) -> None:
    def resolve():  # type: ignore[no-untyped-def]
        raise install_mod.DriverBlocked("signal 9", OS_WINDOWS)

    sup = DriverSupervisor(tmp_path / "home", resolve_binary=resolve, backoff=(0.0,))
    with pytest.raises(DriverUnavailable, match="keep using the built-in driver"):
        sup.call("list_windows", {})
    assert sup.status()["blocked"] == "signal 9"


def _blocked_progress(os_id: str = OS_LINUX) -> dict:
    """The progress snapshot a ``DriverBlocked`` install leaves."""
    return {"error": install_mod.blocked_message("signal 9", os_id), "blocked": "signal 9"}


def _status(**kw):  # type: ignore[no-untyped-def]
    args = dict(
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


def test_a_blocked_install_is_install_failed_with_the_plain_reason() -> None:
    doc = _status(
        platform=PLATFORM_WINDOWS,
        choice="upstream",
        session=WIN,
        install_state=MISSING,
        install_progress=_blocked_progress(OS_WINDOWS),
    )
    assert doc["state"] == setup.INSTALL_FAILED
    assert doc["blocked"] == "signal 9"
    assert doc["user_action"].startswith("The driver was stopped as soon as it started (signal 9).")
    assert "version" not in doc["user_action"]


def test_a_retried_install_while_running_no_longer_reads_as_blocked() -> None:
    doc = _status(
        install_state=MISSING,
        install_progress={
            **install_mod.InstallProgress().snapshot(),
            **_blocked_progress(),
            "running": True,
        },
    )
    assert doc["state"] == setup.INSTALLING
    assert doc["blocked"] == ""


def test_a_blocked_install_still_in_progress_memory_is_install_failed() -> None:
    progress = install_mod.InstallProgress()
    progress.error, progress.blocked = install_mod.blocked_message("signal 9", OS_LINUX), "signal 9"
    doc = _status(install_state=MISSING, install_progress=progress.snapshot())
    assert doc["state"] == setup.INSTALL_FAILED and doc["user_action"] == progress.error


def _fail_installs_with(monkeypatch: pytest.MonkeyPatch, outcomes: "list[Exception]") -> None:
    """Each install runs the real ``install()`` and fails with the next of *outcomes*."""

    def install_locked(root, **_kw):  # type: ignore[no-untyped-def]
        raise outcomes.pop(0)

    monkeypatch.setattr(install_mod, "_install_locked", install_locked)
    monkeypatch.setattr(install_mod, "PROGRESS", install_mod.InstallProgress())
    monkeypatch.setattr(setup, "_install_thread", None)


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (
            install_mod.InstallError(install_mod.STAGE_VERIFYING, "sha256 mismatch"),
            "sha256 mismatch",
        ),
        (RuntimeError("x"), "install failed: x"),
    ],
    ids=["install_error", "other_error"],
)
def test_a_retried_install_that_fails_otherwise_no_longer_reads_as_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, detail: str
) -> None:
    outcomes: list[Exception] = [install_mod.DriverBlocked("signal 9", OS_LINUX), error]
    _fail_installs_with(monkeypatch, outcomes)
    blocked: list[str] = []
    for _ in range(2):
        setup._start_install(tmp_path)
        setup._install_thread.join(5)  # type: ignore[union-attr]
        blocked.append(install_mod.PROGRESS.blocked)
    assert outcomes == [] and blocked == ["signal 9", ""]
    doc = _status(
        install_state=MISSING,
        install_progress=install_mod.PROGRESS.snapshot(),
        progress=setup.load_progress(tmp_path),
    )
    assert doc["state"] == setup.INSTALL_FAILED
    assert doc["problem"]["code"] == setup.PROBLEM_INSTALL_FAILED
    assert doc["problem"]["message"] == detail


def test_an_install_outcome_is_never_written_to_the_setup_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcomes: list[Exception] = [
        install_mod.DriverBlocked("signal 9", OS_LINUX),
        install_mod.InstallError(install_mod.STAGE_VERIFYING, "sha256 mismatch"),
    ]
    _fail_installs_with(monkeypatch, outcomes)
    monkeypatch.setattr(install_mod, "current_os", lambda: OS_LINUX)
    monkeypatch.setattr(session_mod, "detect", lambda: X11)
    monkeypatch.setattr(setup, "asset_for", lambda os_id: object())
    for _ in range(2):
        setup.run_setup_action("install", tmp_path)
        setup._install_thread.join(5)  # type: ignore[union-attr]
    assert outcomes == []
    assert setup.load_progress(tmp_path) == {"last_step": "install"}
    doc = setup.get_setup_status(tmp_path)
    assert doc["state"] == setup.INSTALL_FAILED and doc["blocked"] == ""
    assert doc["problem"]["code"] == setup.PROBLEM_INSTALL_FAILED
    assert doc["problem"]["message"] == "sha256 mismatch"


def test_a_block_on_a_later_start_is_temporarily_unavailable() -> None:
    doc = _status(supervisor_status={"state": "unavailable", "blocked": "signal 9"})
    assert doc["state"] == setup.TEMPORARILY_UNAVAILABLE
    assert "Security software" in doc["user_action"]


@posix_only
def test_our_own_handshake_timeout_kill_is_not_read_as_a_block(tmp_path: Path) -> None:
    """A driver that never answers is killed by the supervisor's own timeout (SIGKILL).
    That must stay an ordinary start failure, never "blocked by security software"."""
    home = tmp_path / "home"
    silent = tmp_path / "cua-driver"
    silent.write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
    silent.chmod(silent.stat().st_mode | stat.S_IXUSR)
    sup = DriverSupervisor(
        home,
        resolve_binary=lambda: (silent, ""),
        mcp_args=(),
        backoff=(0.0,),
        start_timeout=0.5,  # flake-ok: the child never answers, so this deadline always expires
    )
    try:
        with pytest.raises(DriverUnavailable) as info:
            sup.call("list_windows", {})
        assert "Security software" not in str(info.value)
        assert sup.status()["blocked"] == ""
    finally:
        sup.stop()


def test_a_linux_launch_gets_the_fixed_system_path_not_the_gateways(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.computer_use.driver_runtime import backend as cua_backend
    from kiro_crew.computer_use.driver_runtime import linux_apps

    seen: list[dict] = []
    monkeypatch.setattr(linux_apps, "spawn_detached", lambda target, env: seen.append(dict(env)))
    monkeypatch.setenv("PATH", "/opt/agent-writable/bin:/usr/bin")
    backend = cua_backend.CuaBackend(supervisor=object(), home=Path("/nonexistent"), os_id=OS_LINUX)  # type: ignore[arg-type]
    backend._launch_functions()["spawn"](object())
    assert seen and seen[0]["PATH"] == linux_apps.SYSTEM_PATH


@pytest.mark.parametrize(
    ("platform", "after"),
    [(PLATFORM_MACOS, "auto"), (PLATFORM_WINDOWS, "auto"), (PLATFORM_LINUX, "upstream")],
)
def test_use_builtin_driver_returns_to_auto_only_where_a_built_in_driver_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, after: str
) -> None:
    from kiro_crew.computer_use import enable_state

    saved: dict = {"enabled": True, "driver": "upstream"}
    from kiro_crew.computer_use import backend as backend_mod

    monkeypatch.setattr(setup, "_keystone_for_update", lambda: dict(saved))
    monkeypatch.setattr(backend_mod, "reset_shared_backend", lambda: None)
    monkeypatch.setattr(enable_state, "save_state", lambda state, **_k: saved.update(state))
    monkeypatch.setattr(enable_state, "locked_by_policy", lambda: False)
    monkeypatch.setattr(setup, "_platform", lambda: platform)
    monkeypatch.setattr(setup, "_reset_backend", lambda: None)
    monkeypatch.setattr(setup, "get_setup_status", lambda _root=None: {"state": "ready"})
    setup.run_setup_action("use_builtin_driver", tmp_path)
    assert saved["driver"] == after
    assert not install_mod.version_dir(tmp_path).exists()  # nothing installed or removed


def test_the_handler_accepts_the_built_in_driver_action() -> None:
    from kiro_crew.dashboard.handlers import computer_use_setup as handler

    assert "use_builtin_driver" in handler.SETUP_ACTIONS
    assert set(handler.SETUP_ACTIONS) == set(setup.ACTIONS)


@pytest.mark.parametrize("body", [b'{"allowed_apps": ["Calculator"],}', b"[1, 2]", b"\xff\xfe"])
def test_use_builtin_driver_never_overwrites_an_unreadable_keystone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    from kiro_crew.computer_use import enable_state
    from kiro_crew.config import loosening_provenance

    path = tmp_path / "computer_use.json"
    path.write_bytes(body)
    monkeypatch.setattr(enable_state, "computer_use_state_path", lambda: path)
    monkeypatch.setattr(
        loosening_provenance, "reconciled_bytes", lambda _leaf, p: Path(p).read_bytes()
    )
    monkeypatch.setattr(enable_state, "locked_by_policy", lambda: False)
    monkeypatch.setattr(setup, "_platform", lambda: PLATFORM_MACOS)
    monkeypatch.setattr(setup, "_reset_backend", lambda: None)
    from kiro_crew.computer_use import backend as backend_mod

    monkeypatch.setattr(backend_mod, "reset_shared_backend", lambda: None)
    monkeypatch.setattr(
        setup, "get_setup_status", lambda _root=None: {"state": "install_failed", "problem": None}
    )
    saved: list = []
    monkeypatch.setattr(enable_state, "save_state", lambda state, **_k: saved.append(state))
    doc = setup.run_setup_action("use_builtin_driver", tmp_path)
    assert saved == []
    assert path.read_bytes() == body
    assert doc["problem"]["code"] == setup.PROBLEM_KEYSTONE_UNREADABLE
    with pytest.raises(setup.KeystoneUnreadable):
        setup.set_driver_choice("auto")
    assert saved == []


def test_a_keystone_saved_with_a_byte_order_mark_still_changes_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.computer_use import backend as backend_mod
    from kiro_crew.computer_use import enable_state
    from kiro_crew.config import loosening_provenance

    path = tmp_path / "computer_use.json"
    path.write_bytes(b'\xef\xbb\xbf{"driver": "upstream", "allowed_apps": ["Calculator"]}')
    monkeypatch.setattr(enable_state, "computer_use_state_path", lambda: path)
    monkeypatch.setattr(
        loosening_provenance, "reconciled_bytes", lambda _leaf, p: Path(p).read_bytes()
    )
    monkeypatch.setattr(backend_mod, "reset_shared_backend", lambda: None)
    saved: list = []
    monkeypatch.setattr(enable_state, "save_state", lambda state, **_k: saved.append(state))
    setup.set_driver_choice("auto")
    assert saved == [{"driver": "auto", "allowed_apps": ["Calculator"]}]


_NO_CONSOLE = session_mod.detect_windows(0)


@pytest.mark.parametrize(
    ("kw", "code"),
    [
        # A signed-in desktop and a driver killed at launch: the block is THE problem,
        # ahead of anything that could only matter once the driver runs.
        (
            dict(install_state=MISSING, install_progress=_blocked_progress(OS_WINDOWS)),
            setup.PROBLEM_INSTALL_BLOCKED,
        ),
        (dict(supervisor_status={"blocked": "signal 9"}), setup.PROBLEM_LAUNCH_BLOCKED),
        (
            dict(supervisor_status={"state": "crash_loop", "blocked": "signal 9"}),
            setup.PROBLEM_LAUNCH_BLOCKED,
        ),
        (
            dict(
                supervisor_status={"state": "unavailable", "last_error": "x", "blocked": "signal 9"}
            ),
            setup.PROBLEM_LAUNCH_BLOCKED,
        ),
        (
            dict(
                supervisor_status={"blocked": "signal 9"},
                progress={"verify": {"ok": False, "binary_sha256": SHA, "detail": "x"}},
            ),
            setup.PROBLEM_LAUNCH_BLOCKED,
        ),
        # No desktop session at all: nothing could have been shown, so signing in comes first.
        (
            dict(session=_NO_CONSOLE, supervisor_status={"blocked": "signal 9"}),
            setup.PROBLEM_NO_DESKTOP_SESSION,
        ),
        (
            dict(
                session=_NO_CONSOLE,
                install_state=MISSING,
                install_progress=_blocked_progress(OS_WINDOWS),
            ),
            setup.PROBLEM_NO_DESKTOP_SESSION,
        ),
    ],
)
def test_windows_upstream_problem_order_puts_a_launch_block_first_when_a_session_exists(
    kw: dict, code: str
) -> None:
    args = dict(platform=PLATFORM_WINDOWS, choice="upstream", session=WIN)
    args.update(kw)
    doc = _status(**args)
    assert doc["problem"]["code"] == code
    if code in (setup.PROBLEM_INSTALL_BLOCKED, setup.PROBLEM_LAUNCH_BLOCKED):
        assert "Security software" in doc["problem"]["message"]
        assert doc["blocked"] == "signal 9"


def test_a_caught_action_failure_is_a_problem_never_a_limitation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew.computer_use import enable_state

    monkeypatch.setattr(enable_state, "locked_by_policy", lambda: False)
    monkeypatch.setattr(
        setup,
        "get_setup_status",
        lambda _root=None: {"state": "ready", "problem": None, "limitations": []},
    )
    monkeypatch.setattr(install_mod, "install_state", lambda _h, **_k: INSTALLED)

    def full_disk(_root, **_k):  # type: ignore[no-untyped-def]
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(setup, "_verify", full_disk)
    doc = setup.run_setup_action("verify", tmp_path)
    assert doc["problem"]["code"] == setup.PROBLEM_ACTION_FAILED
    assert "No space left" in doc["problem"]["message"]
    assert doc["limitations"] == []
    doc = setup.run_setup_action("bogus", tmp_path)
    assert doc["problem"]["code"] == setup.PROBLEM_ACTION_FAILED and doc["limitations"] == []
