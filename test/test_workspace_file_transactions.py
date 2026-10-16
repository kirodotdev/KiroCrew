"""Guest saves preserve bytes and serialize with every participating writer."""

from __future__ import annotations

import io
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from kiro_crew.remote_session import workspace_tools as wt


def test_non_utf8_is_read_only_and_cannot_be_saved(tmp_path):
    original = b"caf\xe9\n"
    path = tmp_path / "latin.txt"
    path.write_bytes(original)
    ws = wt.Workspace(str(tmp_path))
    result = wt.browse(ws, {"op": "read", "path": path.name})
    assert result["binary"] is True
    assert result["content"] == ""
    for expected in (original.decode("utf-8", "replace"), None):
        saved = wt.write_request(ws, {"path": path.name, "content": "edit", "expected": expected})
        assert "error" in saved and "ok" not in saved
        assert path.read_bytes() == original


def test_tool_value_errors_do_not_end_the_stdio_server(tmp_path):
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9")
    messages = [
        {"name": "edit_file", "arguments": {"path": "latin.txt", "old_str": "caf", "new_str": "x"}},
        {"name": "write_file", "arguments": {"path": "bad.txt", "content": "\ud800"}},
        {"name": "run_command", "arguments": {"command": "true", "timeout": "abc"}},
        {"name": "write_file", "arguments": {"path": "good.txt", "content": "ok"}},
    ]
    inp = io.BytesIO(
        b"".join(
            json.dumps({"id": i, "method": "tools/call", "params": params}).encode() + b"\n"
            for i, params in enumerate(messages, 1)
        )
    )
    out = io.BytesIO()
    wt.serve(wt.Server(wt.Workspace(str(tmp_path))), inp, out)
    replies = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [reply["result"]["isError"] for reply in replies] == [True, True, True, False]
    assert (tmp_path / "good.txt").read_text() == "ok"
    assert (tmp_path / "latin.txt").read_bytes() == b"caf\xe9"


def _agent_writer(root, operation, observed):
    import fcntl

    real_flock = fcntl.flock

    def flock(fd, flags):
        if flags == fcntl.LOCK_EX:
            try:
                real_flock(fd, flags | fcntl.LOCK_NB)
            except BlockingIOError:
                observed.send("blocked")
            else:
                observed.send("free")
        return real_flock(fd, flags)

    fcntl.flock = flock
    try:
        ws = wt.Workspace(root)
        if operation == "write":
            ws.write_file("file.txt", "agent")
        else:
            ws.edit_file("file.txt", "dashboard", "agent")
        observed.send("done")
    except Exception as exc:
        observed.send(type(exc).__name__)
    finally:
        observed.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX guest cross-process flock")
@pytest.mark.parametrize("operation", ["write", "edit"])
def test_dashboard_compare_and_replace_hold_the_agent_writer_lock(tmp_path, monkeypatch, operation):
    path = tmp_path / "file.txt"
    path.write_text("base")
    ws = wt.Workspace(str(tmp_path))
    compared, release = Event(), Event()
    real_write = ws._write_file_locked

    def paused_write(real, text):
        compared.set()
        assert release.wait(30), "dashboard write was not released"
        return real_write(real, text)

    monkeypatch.setattr(ws, "_write_file_locked", paused_write)
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_agent_writer, args=(str(tmp_path), operation, sender))
    with ThreadPoolExecutor(max_workers=1) as pool:
        dashboard = pool.submit(
            wt.write_request,
            ws,
            {
                "path": path.name,
                "content": "dashboard",
                "expected": "base",
            },
        )
        try:
            assert compared.wait(30), "dashboard did not compare the original file"
            child.start()
            sender.close()
            assert receiver.poll(30), "agent did not attempt its lock"
            assert receiver.recv() == "blocked"
        finally:
            release.set()
            dashboard_result = dashboard.result(timeout=30)
            if child.pid is not None:
                child.join(30)
                if child.is_alive():
                    child.terminate()
                    child.join(30)
        assert dashboard_result["ok"] is True
        assert receiver.poll(30) and receiver.recv() == "done"
        assert child.exitcode == 0
        receiver.close()
    assert path.read_text() == "agent"


def test_stale_and_deleted_files_are_not_replaced(tmp_path):
    ws = wt.Workspace(str(tmp_path))
    path = tmp_path / "file.txt"
    path.write_text("agent")
    request = {"path": path.name, "content": "dashboard", "expected": "base"}
    assert wt.write_request(ws, request)["conflict"] is True
    assert path.read_text() == "agent"
    path.unlink()
    assert wt.write_request(ws, request)["conflict"] is True
    assert not path.exists()


def test_lock_files_are_not_workspace_browser_entries(tmp_path):
    ws = wt.Workspace(str(tmp_path))
    ws.write_file("file.txt", "ok")
    assert ws.list_directory() == "file.txt"
    assert wt.browse(ws, {"op": "list"})["entries"] == [{"name": "file.txt", "dir": False}]
    assert [Path(p).name for p in ws._walk(str(tmp_path))] == ["file.txt"]


def test_guest_entry_points_share_locks_outside_the_project(tmp_path, monkeypatch, capsys):
    home, project = tmp_path / "guest", tmp_path / "project"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    request = {"path": "file.txt", "content": "base"}
    monkeypatch.setattr(
        wt.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
    )
    assert wt.main(["--root", str(project), "--write"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert set(project.iterdir()) == {project / "file.txt"}
    locks = home / ".kirocrew-worker" / "file-locks"
    assert len(list(locks.iterdir())) == 1
    message = {
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "edit_file",
            "arguments": {"path": "file.txt", "old_str": "base", "new_str": "agent"},
        },
    }
    monkeypatch.setattr(
        wt.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode() + b"\n"))
    )
    assert wt.main(["--root", str(project)]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["isError"] is False
    assert len(list(locks.iterdir())) == 1
    assert (project / "file.txt").read_text() == "agent"
    assert set(project.iterdir()) == {project / "file.txt"}


@pytest.mark.parametrize("replace_all", [None, 0, 1, 0.0, 1.0, "", "false", "true", [], {}, [1]])
@pytest.mark.parametrize("original", ["old", "old old"])
def test_edit_file_rejects_non_boolean_replace_all(tmp_path, replace_all, original):
    path = tmp_path / "file.txt"
    path.write_text(original)
    message = {
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "edit_file",
            "arguments": {
                "path": path.name,
                "old_str": "old",
                "new_str": "new",
                "replace_all": replace_all,
            },
        },
    }
    out = io.BytesIO()
    wt.serve(
        wt.Server(wt.Workspace(str(tmp_path))),
        io.BytesIO(json.dumps(message).encode() + b"\n"),
        out,
    )
    result = json.loads(out.getvalue())["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == "replace_all must be a boolean"
    assert path.read_text() == original


@pytest.mark.parametrize("flags", [{}, {"replace_all": False}, {"replace_all": True}])
def test_edit_file_only_true_allows_multiple_replacements(tmp_path, flags):
    path = tmp_path / "file.txt"
    ws = wt.Workspace(str(tmp_path))
    path.write_text("old old")
    if flags.get("replace_all") is True:
        assert "2 occurrence(s)" in ws.edit_file(path.name, "old", "new", **flags)
        assert path.read_text() == "new new"
    else:
        with pytest.raises(wt.ToolError, match="occurs 2 times"):
            ws.edit_file(path.name, "old", "new", **flags)
        assert path.read_text() == "old old"
    path.write_text("old")
    assert "1 occurrence(s)" in ws.edit_file(path.name, "old", "new", **flags)
    assert path.read_text() == "new"


@pytest.mark.parametrize("hold_secs", [0.0, 13.0, 120.0])
def test_windows_guest_lock_waits_without_writing_a_sentinel(tmp_path, monkeypatch, hold_secs):
    import sys

    from test_five_whys_windows_lock import _FakeCrt, _VirtualClock

    from kiro_crew import platform_lock_compat

    clock = _VirtualClock()
    crt = _FakeCrt(clock, release_at=hold_secs)
    monkeypatch.setattr(wt, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(wt, "time", clock)
    monkeypatch.setitem(sys.modules, "msvcrt", crt)
    ws = wt.Workspace(str(tmp_path))
    with ws.write_lock("file.txt"):
        assert crt.held
        assert clock.now >= hold_secs
        assert all(path.stat().st_size == 0 for path in Path(ws.lock_dir).iterdir())
    assert not crt.held
    assert set(crt.calls) <= {crt.LK_NBLCK, crt.LK_UNLCK}
    assert wt._LOCK_POLL_SECS == platform_lock_compat._LOCK_POLL_SECS
    assert wt._LOCK_TIMEOUT_SECS == platform_lock_compat._LOCK_TIMEOUT_SECS


@pytest.mark.parametrize("failure", ["held", "bad_fd"])
def test_windows_guest_lock_refuses_without_unlocking_an_unowned_lock(
    tmp_path, monkeypatch, failure
):
    import errno
    import sys

    from test_five_whys_windows_lock import _FakeCrt, _VirtualClock

    clock = _VirtualClock()
    crt = _FakeCrt(clock, release_at=float("inf"))
    if failure == "bad_fd":
        crt.fail_with = errno.EBADF
    monkeypatch.setattr(wt, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(wt, "time", clock)
    monkeypatch.setitem(sys.modules, "msvcrt", crt)
    ws = wt.Workspace(str(tmp_path))
    expected = OSError if failure == "bad_fd" else wt.ToolError
    with pytest.raises(expected):
        with ws.write_lock("file.txt"):
            pytest.fail("unowned lock admitted a write")
    assert crt.LK_UNLCK not in crt.calls
    assert not crt.held
    assert clock.now == 0 if failure == "bad_fd" else clock.now >= wt._LOCK_TIMEOUT_SECS
