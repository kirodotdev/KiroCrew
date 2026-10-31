"""The KAS MCP launcher, run in-process: every refusal and both start paths.

``test_kas_env_launch.py`` drives the launcher end to end through a real child
process. These tests call it directly, with snapshots written by hand, so each
refusal is pinned to its reason and the exec / Windows-spawn tails are reached
without starting a server.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import kas_env_shim

SESSION, AGENT, SERVER, VERSION = "dashboard_chat-shim", "kirocrew", "srv", "a" * 32
REF = ["--session", SESSION, "--agent", AGENT, "--server", SERVER, "--version", VERSION]


@pytest.fixture
def home(tmp_path) -> Path:
    data_home = tmp_path / "crew-home"
    (data_home / kas_env_shim.SNAPSHOT_LEAF).mkdir(parents=True, mode=0o700)
    return data_home


def _path(home: Path) -> Path:
    return (
        home
        / kas_env_shim.SNAPSHOT_LEAF
        / kas_env_shim.snapshot_name(SESSION, AGENT, SERVER, VERSION)
    )


def _doc(**overrides) -> dict:
    doc = {
        kas_env_shim.SNAPSHOT_MARKER: kas_env_shim.SNAPSHOT_FORMAT,
        "session": SESSION,
        "agent": AGENT,
        "server": SERVER,
        "version": VERSION,
        "command": "server-bin",
        "args": ["--flag"],
        "env": {"TOKEN_FOR_TEST": "value"},
    }
    doc.update(overrides)
    return doc


def _write(home: Path, payload: bytes) -> Path:
    path = _path(home)
    path.write_bytes(payload)
    path.chmod(0o600)
    return path


def _environ(home: Path) -> dict[str, str]:
    return {"KIROCREW_HOME": str(home), "PATH": os.environ.get("PATH", "")}


def _refused(home: Path, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        kas_env_shim.resolve_launch(list(REF), _environ(home))
    assert exc.value.code == 78
    err = capsys.readouterr().err
    assert "value" not in err, "a refusal never prints an env value"
    return err


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({kas_env_shim.SNAPSHOT_MARKER: 2}, "unknown format"),
        ({"command": "  "}, "names no command"),
        ({"args": ["ok", 3]}, "malformed arguments"),
        ({"env": {"K": 1}}, "malformed environment"),
    ],
)
def test_a_snapshot_with_a_bad_field_is_refused(home, capsys, overrides, reason):
    _write(home, json.dumps(_doc(**overrides)).encode())
    assert reason in _refused(home, capsys)


@pytest.mark.parametrize(
    ("payload", "reason"), [(b"{not json", "not valid JSON"), (b"[]", "wrong shape")]
)
def test_a_snapshot_that_is_not_a_json_object_is_refused(home, capsys, payload, reason):
    _write(home, payload)
    assert reason in _refused(home, capsys)


def test_a_snapshot_that_is_too_large_is_refused(home, capsys):
    _write(home, b" " * (kas_env_shim._MAX_SNAPSHOT_BYTES + 1))
    assert "too large" in _refused(home, capsys)


@pytest.mark.skipif(os.name != "posix", reason="a directory opens read-only on POSIX")
def test_a_snapshot_that_is_not_a_regular_file_is_refused(home, capsys):
    _path(home).mkdir(mode=0o700)
    assert "not a regular file" in _refused(home, capsys)


@pytest.mark.skipif(os.name != "posix", reason="symlink creation")
def test_a_linked_launch_directory_is_refused(home, tmp_path, capsys):
    leaf = home / kas_env_shim.SNAPSHOT_LEAF
    real = tmp_path / "elsewhere"
    leaf.rename(real)
    leaf.symlink_to(real, target_is_directory=True)
    _write(home, json.dumps(_doc()).encode())
    assert "launch directory is a link" in _refused(home, capsys)


_LAUNCH = (["server-bin", "--flag"], {"TOKEN_FOR_TEST": "value"})


def _stub_resolve(monkeypatch) -> list[list[str]]:
    """Replace the snapshot read, so these tests reach only the exec tail.

    They force ``os.name`` to ``posix`` to pick the exec branch; the real read
    would then run POSIX-only ownership checks on a Windows host. The read
    itself is covered by the refusal tests above and by test_kas_env_launch.py.
    """
    refs: list[list[str]] = []

    def _resolve(argv, _environ):
        refs.append(list(argv))
        return list(_LAUNCH[0]), dict(_LAUNCH[1])

    monkeypatch.setattr(kas_env_shim, "resolve_launch", _resolve)
    return refs


def test_main_execs_the_recorded_command_with_its_env(monkeypatch):
    refs = _stub_resolve(monkeypatch)
    seen: list[tuple] = []

    def _exec(file, argv, env):
        seen.append((file, argv, env))
        raise SystemExit(0)

    monkeypatch.setattr(kas_env_shim.os, "name", "posix")
    monkeypatch.setattr(kas_env_shim.os, "execvpe", _exec)
    with pytest.raises(SystemExit):
        kas_env_shim.main(list(REF))
    assert refs == [REF]
    ((file, argv, env),) = seen
    assert (file, argv) == ("server-bin", ["server-bin", "--flag"])
    assert env == {"TOKEN_FOR_TEST": "value"}


def test_main_reads_its_own_argv_when_given_none(monkeypatch):
    refs = _stub_resolve(monkeypatch)
    monkeypatch.setattr(kas_env_shim.os, "name", "posix")
    monkeypatch.setattr(kas_env_shim.os, "execvpe", lambda *_a: None)
    monkeypatch.setattr(kas_env_shim.sys, "argv", ["kas_env_shim", *REF])
    with pytest.raises(SystemExit) as exc:
        kas_env_shim.main()
    assert refs == [REF]
    assert exc.value.code == 1, "an exec that returns is a failure, not a start"


def test_main_reports_a_command_that_cannot_start(monkeypatch, capsys):
    _stub_resolve(monkeypatch)

    def _missing(*_a):
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(kas_env_shim.os, "name", "posix")
    monkeypatch.setattr(kas_env_shim.os, "execvpe", _missing)
    with pytest.raises(SystemExit) as exc:
        kas_env_shim.main(list(REF))
    assert exc.value.code == 127
    err = capsys.readouterr().err
    assert "'server-bin' could not be started" in err and "value" not in err


def test_main_on_windows_runs_the_server_as_a_child(home, monkeypatch):
    _write(home, json.dumps(_doc()).encode())
    seen: list[list[str]] = []

    def _spawn(argv, env):
        seen.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(kas_env_shim.os, "name", "nt")
    monkeypatch.setattr(kas_env_shim, "_spawn_child", _spawn)
    monkeypatch.setattr(kas_env_shim.os, "environ", _environ(home))
    with pytest.raises(SystemExit):
        kas_env_shim.main(list(REF))
    assert seen == [["server-bin", "--flag"]]


class _Proc:
    def __init__(self, rc=None, error: BaseException | None = None):
        self.rc, self.error, self.terminated = rc, error, False

    def wait(self):
        if self.error is not None:
            raise self.error
        return self.rc

    def terminate(self):
        self.terminated = True


def _exit_raises(code):
    raise SystemExit(code)


@pytest.mark.parametrize(("rc", "code"), [(3, 3), (-9, 1)])
def test_the_windows_child_exits_with_the_servers_status(monkeypatch, rc, code):
    started: list[list[str]] = []

    def _popen(argv, **kwargs):
        assert (kwargs["stdin"], kwargs["stdout"], kwargs["stderr"]) == (0, 1, 2)
        started.append(argv)
        return _Proc(rc)

    monkeypatch.setattr(kas_env_shim.shutil, "which", lambda name, path=None: f"/bin/{name}")
    monkeypatch.setattr(kas_env_shim.subprocess, "Popen", _popen)
    monkeypatch.setattr(kas_env_shim.os, "_exit", _exit_raises)
    with pytest.raises(SystemExit) as exc:
        kas_env_shim._spawn_child(["server-bin", "--flag"], {"PATH": "/bin"})
    assert exc.value.code == code
    assert started == [["/bin/server-bin", "--flag"]]


def test_the_windows_child_is_stopped_when_the_launcher_is_interrupted(monkeypatch):
    proc = _Proc(error=KeyboardInterrupt())
    monkeypatch.setattr(kas_env_shim.shutil, "which", lambda name, path=None: None)
    monkeypatch.setattr(kas_env_shim.subprocess, "Popen", lambda argv, **_k: proc)
    with pytest.raises(KeyboardInterrupt):
        kas_env_shim._spawn_child(["server-bin"], {})
    assert proc.terminated
