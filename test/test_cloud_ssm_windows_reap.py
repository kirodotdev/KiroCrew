"""Windows teardown of a plugin that outlived its ``aws`` wrapper; nt is faked off Windows."""

from __future__ import annotations

import contextlib
import logging
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from kiro_crew import platform_compat
from kiro_crew.cloud import ssm
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

_WRAPPER_PID = 4242
_WRAPPER_TOKEN = "133000000000000000"
_PLUGIN_PID = 5151
_PLUGIN_TOKEN = "133000000000000100"
_TASKKILL_BIN = r"C:\Windows\System32\taskkill.exe"


class _Forward:
    """A Popen stand-in carrying the record ``open_port_forward`` leaves on Windows."""

    def __init__(self, *, poll: int | None = 0, record: bool = True) -> None:
        self.pid = _WRAPPER_PID
        self._poll = poll
        self.terminated = False
        if record:
            self.ssm_pgid = None
            self.ssm_start_time: str | None = _WRAPPER_TOKEN
            self.ssm_local_port = 18443

    def poll(self) -> int | None:
        return self._poll

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _kill(proc: _Forward) -> None:
    ssm.kill_port_forward(cast(subprocess.Popen, proc))


class _Table:
    """The process table the reaper reads: pid -> creation token of what is alive."""

    def __init__(self, alive: dict[int, str], *, killable: bool = True) -> None:
        self.alive = dict(alive)
        self.killable = killable
        self.walked: list[tuple[int, str]] = []
        self.killed: list[tuple[int, str, int]] = []

    def descendants(self, root_pid: int, root_token: str) -> list[int]:
        self.walked.append((root_pid, root_token))
        return sorted(pid for pid in self.alive if pid != root_pid)

    def start_time(self, pid: int) -> str | None:
        return self.alive.get(pid)

    def kill(self, pid: int, token: str, sig: int) -> bool:
        self.killed.append((pid, token, sig))
        if self.killable and self.alive.get(pid) == token:
            del self.alive[pid]
        return True


@pytest.fixture
def table(monkeypatch):
    def _install(alive: dict[int, str], *, killable: bool = True) -> _Table:
        state = _Table(alive, killable=killable)
        monkeypatch.setattr(ssm.os, "name", "nt")
        monkeypatch.setattr(ssm.platform_compat, "attributed_descendants", state.descendants)
        monkeypatch.setattr(ssm.platform_compat, "process_start_time", state.start_time)
        monkeypatch.setattr(ssm.platform_compat, "kill_pid_pinned", state.kill)
        monkeypatch.setattr(ssm.platform_compat, "pid_exists", lambda pid: pid in state.alive)
        monkeypatch.setattr(
            ssm.subprocess, "run", lambda argv, **_k: pytest.fail(f"spawned {argv!r}")
        )
        return state

    return _install


def test_exited_wrapper_with_no_surviving_child_signals_nothing(table) -> None:
    state = table({})

    _kill(_Forward())

    assert state.killed == []


def test_a_forward_without_the_spawn_record_is_left_alone(table) -> None:
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN})

    _kill(_Forward(record=False))

    assert state.walked == []
    assert state.killed == []


def test_a_child_whose_identity_cannot_be_read_is_reported_not_signalled(
    table, monkeypatch, caplog
) -> None:
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN})
    monkeypatch.setattr(ssm.platform_compat, "process_start_time", lambda pid: None)

    with caplog.at_level(logging.WARNING, logger=ssm.logger.name):
        _kill(_Forward())

    assert state.killed == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(_PLUGIN_PID) in warnings[0] and "18443" in warnings[0]


def test_a_pid_recycled_before_its_identity_is_read_is_not_signalled(table, monkeypatch) -> None:
    """A stranger takes the plugin's pid before the read; the second walk drops it."""
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN})
    stranger = "133000000000000900"

    def _read(pid: int) -> str | None:
        state.alive[pid] = stranger
        return stranger

    def _walk(root_pid: int, root_token: str) -> list[int]:
        state.walked.append((root_pid, root_token))
        return [_PLUGIN_PID] if len(state.walked) == 1 else []

    monkeypatch.setattr(ssm.platform_compat, "process_start_time", _read)
    monkeypatch.setattr(ssm.platform_compat, "attributed_descendants", _walk)

    _kill(_Forward())

    assert len(state.walked) == 2
    assert state.killed == []


@pytest.mark.parametrize("readable_after_kill", [True, False])
def test_a_survivor_is_logged_with_the_forwarded_port(
    table, monkeypatch, caplog, readable_after_kill
) -> None:
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN}, killable=False)
    monkeypatch.setattr(ssm, "_GROUP_KILL_WAIT_SECS", 0.0)
    if not readable_after_kill:  # only the first identity read succeeds
        reads = iter([_PLUGIN_TOKEN])
        monkeypatch.setattr(ssm.platform_compat, "process_start_time", lambda p: next(reads, None))

    with caplog.at_level(logging.WARNING, logger=ssm.logger.name):
        _kill(_Forward())

    assert state.killed == [(_PLUGIN_PID, _PLUGIN_TOKEN, platform_compat.SIGKILL)]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(_PLUGIN_PID) in warnings[0] and "18443" in warnings[0]


def test_a_plugin_that_ends_during_the_wait_is_not_reported(table, monkeypatch, caplog) -> None:
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN}, killable=False)
    slept: list[float] = []

    def _sleep(secs: float) -> None:
        slept.append(secs)
        state.alive.clear()

    monkeypatch.setattr(ssm, "_sleep", _sleep)

    with caplog.at_level(logging.WARNING, logger=ssm.logger.name):
        _kill(_Forward())

    assert slept == [ssm._GROUP_EMPTY_POLL_SECS]
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_an_unreadable_wrapper_identity_is_reported(table, caplog) -> None:
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN})
    proc = _Forward()
    proc.ssm_start_time = None

    with caplog.at_level(logging.WARNING, logger=ssm.logger.name):
        _kill(proc)

    assert state.walked == [] and state.killed == []
    assert any("18443" in r.getMessage() for r in caplog.records)


def test_a_failed_tree_kill_reaps_what_the_parent_only_kill_left(table, monkeypatch) -> None:
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN})
    monkeypatch.setattr(ssm.platform_compat, "trusted_system_bin", lambda name: None)
    monkeypatch.setattr(
        ssm.os,
        "getpgid",
        lambda pid: (_ for _ in ()).throw(ProcessLookupError()),
        raising=False,
    )
    proc = _Forward(poll=None)

    _kill(proc)

    assert proc.terminated is True
    assert state.killed == [(_PLUGIN_PID, _PLUGIN_TOKEN, platform_compat.SIGKILL)]


def test_a_live_wrapper_is_still_ended_by_one_tree_kill(table, monkeypatch) -> None:
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN})
    calls: list[list[str]] = []

    class _R:
        returncode = 0

    def _run(argv, **_kwargs):
        calls.append(argv)
        return _R()

    monkeypatch.setattr(ssm.subprocess, "run", _run)
    monkeypatch.setattr(ssm.platform_compat, "trusted_system_bin", lambda name: _TASKKILL_BIN)

    _kill(_Forward(poll=None))

    assert calls == [[_TASKKILL_BIN, "/T", "/F", "/PID", str(_WRAPPER_PID)]]
    assert state.killed == []


def test_open_port_forward_records_what_the_teardown_reaps(table, monkeypatch) -> None:
    state = table({_PLUGIN_PID: _PLUGIN_TOKEN, _WRAPPER_PID: _WRAPPER_TOKEN})
    monkeypatch.setattr(ssm.platform_compat, "pgroup_of", lambda pid: None)
    monkeypatch.setattr(ssm, "require_session_manager_plugin", lambda: None)
    # The faked os.name gives Path the Windows flavour, so no path may be resolved.
    monkeypatch.setattr(ssm, "build_port_forward_argv", lambda *_a: ["aws"])
    monkeypatch.setattr(ssm, "aws_spawn_env", lambda head: {})
    monkeypatch.setattr(ssm.subprocess, "Popen", lambda argv, **_k: _Forward(record=False))

    proc = ssm.open_port_forward("i-0abc", 5476, 18443, "dev", "us-east-1")
    assert (proc.ssm_start_time, proc.ssm_local_port) == (_WRAPPER_TOKEN, 18443)  # type: ignore[attr-defined]
    ssm.kill_port_forward(proc)

    assert state.walked == [(_WRAPPER_PID, _WRAPPER_TOKEN)] * 2
    assert state.killed == [(_PLUGIN_PID, _PLUGIN_TOKEN, platform_compat.SIGKILL)]
    assert state.alive == {_WRAPPER_PID: _WRAPPER_TOKEN}
    monkeypatch.setattr(ssm.os, "name", "posix")
    assert ssm._spawned_start_time(proc) is None


#: Runs until its stop file appears, so cleanup ends it without signalling any pid.
_SLEEPING_PLUGIN = (
    "import os,sys,time\n"
    "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
    "end = time.monotonic() + 60\n"
    "while not os.path.exists(sys.argv[2]) and time.monotonic() < end:\n"
    "    time.sleep(0.05)\n"
)


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="Windows process tree")
def test_live_plugin_of_an_exited_wrapper_is_ended(tmp_path: Path, request) -> None:
    plugin = tmp_path / "plugin.py"
    plugin.write_text(_SLEEPING_PLUGIN, encoding="utf-8")
    pidfile = tmp_path / "plugin.pid"
    stop = tmp_path / "plugin.stop"
    wrapper = (
        "import os,subprocess,sys,time\n"
        f"subprocess.Popen([sys.executable, {str(plugin)!r}, {str(pidfile)!r}, {str(stop)!r}])\n"
        "end = time.monotonic() + 30\n"
        f"while not os.path.exists({str(pidfile)!r}) and time.monotonic() < end:\n"
        "    time.sleep(0.05)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", wrapper], cwd=tmp_path)

    def _cleanup() -> None:
        # The stop file ends the child; the wrapper exits once the child wrote its
        # pid, so the pidfile then names the child to wait for. No pid is signalled.
        stop.write_text("", encoding="utf-8")
        try:
            proc.wait(timeout=FALLBACK_TIMEOUT_SECS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError):
                proc.kill()
            proc.wait(timeout=FALLBACK_TIMEOUT_SECS)
        with contextlib.suppress(OSError, ValueError, AssertionError):
            child = int(pidfile.read_text(encoding="utf-8").strip())
            wait_until(lambda: not platform_compat.pid_exists(child))

    request.addfinalizer(_cleanup)
    proc.ssm_start_time = ssm._spawned_start_time(proc)  # type: ignore[attr-defined]
    proc.ssm_local_port = 0  # type: ignore[attr-defined]
    proc.wait(timeout=FALLBACK_TIMEOUT_SECS)
    wait_until(
        lambda: pidfile.exists() and pidfile.read_text(encoding="utf-8").strip(),
        describe=lambda: f"{pidfile} was never written",
    )
    plugin_pid = [int(pidfile.read_text(encoding="utf-8").strip())]
    assert platform_compat.pid_exists(plugin_pid[0])

    ssm.kill_port_forward(proc)

    wait_until(
        lambda: not platform_compat.pid_exists(plugin_pid[0]),
        describe=lambda: "plugin that outlived its wrapper must be ended",
    )
