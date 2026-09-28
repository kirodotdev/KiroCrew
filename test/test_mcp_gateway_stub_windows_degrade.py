"""The stub's degrade path must not end this process on Windows.

When the broker is unreachable the stub hands the session to the real MCP
backend instead of failing. On POSIX that is ``execvpe``: the image is replaced
in place, the pid is kept, and kiro-cli -- which spawned this process, holds its
stdio pipe and waits on its pid -- never notices the swap.

Windows has no in-place exec. CPython documents the replacement as in-place
*"On Unix"* only; on Windows the ``exec*`` family is emulated as
spawn-then-exit, so the backend comes up under a NEW pid and this process dies.
kiro-cli reads that as the server hanging up and reports
``connection closed: initialize response``, with the server's whole tool surface
missing from the session. Reported from the field on 0.6.0.16: every stubbed
server (``kirocrew-core``, ``kirocrew-cron``) failed exactly that way while every
directly-launched one in the same session was fine, and the real servers all
answered ``initialize`` correctly when started by hand -- the middle hop was the
only broken thing.

Both branches are asserted on every platform (``IS_WINDOWS`` is monkeypatched
either way) rather than skipping one, because the bug was precisely that the
untested platform took a path nobody had exercised.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import stub


class _FakeProc:
    """Stands in for the spawned backend. ``wait`` is the only call the code makes."""

    def __init__(self, rc: int = 0, raise_on_wait: BaseException | None = None) -> None:
        self._rc = rc
        self._raise = raise_on_wait
        self.terminated = False

    def wait(self) -> int:
        if self._raise is not None:
            raise self._raise
        return self._rc

    def terminate(self) -> None:
        self.terminated = True


def _args(command: str = "backend-bin", env_file: str = "") -> argparse.Namespace:
    return argparse.Namespace(
        target_command=command,
        target_args="--serve|--stdio",
        target_args_sep="|",
        env_file=env_file,
    )


@pytest.fixture
def degrade(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Instrument every exit the degrade path can take, and let none of them run.

    ``os._exit`` really would end the test session, and ``execvpe`` really would
    replace it, so both are recorded instead. ``shutil.which`` is stubbed to a
    fixed answer so a machine without the fake binary still exercises the
    resolution call.
    """
    seen: dict[str, Any] = {"exec": [], "popen": [], "exit": [], "which": []}

    def _fake_exec(path: str, argv: list[str], env: dict[str, str]) -> None:
        seen["exec"].append((path, list(argv), dict(env)))
        raise AssertionError("execvpe would have replaced the test process")

    def _fake_which(cmd: str, path: str | None = None) -> str | None:
        seen["which"].append((cmd, path))
        return f"/resolved/{cmd}"

    def _fake_exit(rc: int) -> None:
        seen["exit"].append(rc)
        raise SystemExit(rc)

    monkeypatch.setattr(stub.os, "execvpe", _fake_exec)
    monkeypatch.setattr(stub.shutil, "which", _fake_which)
    monkeypatch.setattr(stub.os, "_exit", _fake_exit)
    return seen


def _install_popen(monkeypatch: pytest.MonkeyPatch, seen: dict[str, Any], proc: _FakeProc) -> None:
    def _fake_popen(argv: list[str], **kwargs: Any) -> _FakeProc:
        seen["popen"].append((list(argv), kwargs))
        return proc

    monkeypatch.setattr(stub.subprocess, "Popen", _fake_popen)


def test_the_windows_degrade_runs_the_backend_as_a_child_of_this_process(
    monkeypatch: pytest.MonkeyPatch, degrade: dict[str, Any]
) -> None:
    """The pid kiro-cli waits on must survive the hand-off.

    This is the whole fix. An ``exec`` here ends this process, and its death --
    not any fault in the backend -- is what kiro-cli reports as a closed
    connection.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    proc = _FakeProc(rc=0)
    _install_popen(monkeypatch, degrade, proc)

    with pytest.raises(SystemExit):
        stub.fallback_exec(_args())

    assert degrade["exec"] == [], "the backend was exec'd, which kills the session"
    assert len(degrade["popen"]) == 1, "the backend was not started as a child"
    argv, kwargs = degrade["popen"][0]
    assert argv == ["/resolved/backend-bin", "--serve", "--stdio"]
    assert kwargs["stdin"] == stub._inherited_std_fd(sys.stdin, 0)
    assert kwargs["stdout"] == stub._inherited_std_fd(sys.stdout, 1)
    assert kwargs["stderr"] == stub._inherited_std_fd(sys.stderr, 2)


def test_the_posix_degrade_still_replaces_this_process_in_place(
    monkeypatch: pytest.MonkeyPatch, degrade: dict[str, Any]
) -> None:
    """POSIX keeps ``execvpe``: same pid, same fds, and no child to reap."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    _install_popen(monkeypatch, degrade, _FakeProc())

    with pytest.raises(AssertionError, match="execvpe would have replaced"):
        stub.fallback_exec(_args())

    assert degrade["popen"] == [], "POSIX must not gain a child process"
    path, argv, _env = degrade["exec"][0]
    assert path == "backend-bin"
    assert argv == ["backend-bin", "--serve", "--stdio"]


def test_the_windows_degrade_carries_the_declared_env_to_the_child(
    monkeypatch: pytest.MonkeyPatch, degrade: dict[str, Any], tmp_path: Path
) -> None:
    """The sidecar env is why the degrade is correct at all.

    The rewriter moves the server's declared env (routinely tokens) out of the
    spec into a 0600 sidecar, so a child started without it is a server missing
    the credential it needs -- the POSIX path restores it and the Windows path
    has to as well.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    sidecar = tmp_path / "env.json"
    sidecar.write_text('{"BACKEND_TOKEN": "declared-value"}', encoding="utf-8")
    _install_popen(monkeypatch, degrade, _FakeProc())

    with pytest.raises(SystemExit):
        stub.fallback_exec(_args(env_file=str(sidecar)))

    _argv, kwargs = degrade["popen"][0]
    assert kwargs["env"]["BACKEND_TOKEN"] == "declared-value"
    assert "PATH" in kwargs["env"], "the child lost the inherited environment"


def test_the_windows_degrade_resolves_the_command_on_the_childs_path(
    monkeypatch: pytest.MonkeyPatch, degrade: dict[str, Any], tmp_path: Path
) -> None:
    """Resolution must follow ``execvpe``'s rule, not ``CreateProcess``'s.

    ``execvpe`` searches the PATH it is handed; Windows ``CreateProcess`` searches
    the CALLING process's, so a relative command would otherwise resolve
    differently on the two platforms.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    sidecar = tmp_path / "env.json"
    sidecar.write_text('{"PATH": "C:\\\\declared\\\\bin"}', encoding="utf-8")
    _install_popen(monkeypatch, degrade, _FakeProc())

    with pytest.raises(SystemExit):
        stub.fallback_exec(_args(env_file=str(sidecar)))

    assert degrade["which"] == [("backend-bin", "C:\\declared\\bin")]


def test_the_windows_degrade_exits_with_the_backends_own_status(
    monkeypatch: pytest.MonkeyPatch, degrade: dict[str, Any]
) -> None:
    """Stand in for a replaced image: this process's status is the backend's."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _install_popen(monkeypatch, degrade, _FakeProc(rc=3))

    with pytest.raises(SystemExit):
        stub.fallback_exec(_args())

    assert degrade["exit"] == [3]


def test_the_windows_degrade_relays_a_crash_status_without_overflowing(
    monkeypatch: pytest.MonkeyPatch, degrade: dict[str, Any]
) -> None:
    """A crashing backend is the common case, not a corner.

    Windows reports an exit code as an unsigned DWORD and an access violation is
    ``0xC0000005`` (3221225477). ``os._exit`` parses a C ``int``, so relaying that
    raw would raise ``OverflowError`` and the stub would die on an exception
    instead of reporting the status. Signed-32-bit reinterpretation is what keeps
    the code kiro-cli reads identical to the one the backend exited with.
    """
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    _install_popen(monkeypatch, degrade, _FakeProc(rc=0xC0000005))

    with pytest.raises(SystemExit):
        stub.fallback_exec(_args())

    assert degrade["exit"] == [-0x3FFFFFFB]
    # The OS reads the low 32 bits back, so what a caller sees is the original.
    assert degrade["exit"][0] & 0xFFFFFFFF == 0xC0000005


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (3, 3),
        (0x7FFFFFFF, 0x7FFFFFFF),
        (0xC0000005, -0x3FFFFFFB),  # Windows access violation
        (0xFFFFFFFF, -1),
        (0x1FFFFFFFF, 1),  # wider than a DWORD: unrepresentable
        (None, 1),
        (-1, -1),  # already signed (a POSIX-shaped status)
    ],
)
def test_the_relayed_status_always_fits_a_c_int(raw: object, expected: int) -> None:
    """Total over what ``wait`` can answer, so no status can crash the relay."""
    assert stub._child_exit_status(raw) == expected


def test_the_windows_degrade_does_not_orphan_the_backend_when_interrupted(
    monkeypatch: pytest.MonkeyPatch, degrade: dict[str, Any]
) -> None:
    """A backend left holding kiro-cli's pipe with nobody waiting is worse than none."""
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    proc = _FakeProc(raise_on_wait=KeyboardInterrupt())
    _install_popen(monkeypatch, degrade, proc)

    with pytest.raises(KeyboardInterrupt):
        stub.fallback_exec(_args())

    assert proc.terminated, "the child was left running with no parent waiting on it"
    assert degrade["exit"] == [], "an interrupted wait must not report a clean status"


def test_the_module_still_exposes_the_real_subprocess_and_exit_calls() -> None:
    """Guard the monkeypatch targets above against a rename or a lost import.

    Every test in this file patches ``stub.subprocess.Popen`` / ``stub.os._exit``;
    if the module stopped importing ``subprocess``, or the code switched to a
    different spawn call, those patches would silence themselves and the suite
    would go green while asserting nothing.
    """
    assert stub.subprocess is subprocess
    assert stub.os is os
    assert "subprocess.Popen(" in Path(stub.__file__).read_text(encoding="utf-8")
