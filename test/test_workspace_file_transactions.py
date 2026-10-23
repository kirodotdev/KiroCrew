"""Guest writes keep bytes and locks where they belong, and status runs nothing."""

from __future__ import annotations

import errno
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_dir_link
from kiro_crew import platform_lock_compat
from kiro_crew.platform.governance import WORKSPACE_TOOL_KINDS, classify_tool_args
from kiro_crew.remote_session import workspace_tools as wt


def _call(ident, name, **arguments):
    return {"id": ident, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def _serve(root, *messages) -> list[dict]:
    """Run the stdio tool server over ``messages``; return its replies."""
    out = io.BytesIO()
    inp = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
    wt.serve(wt.Server(wt.Workspace(str(root))), inp, out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _windows_guest(monkeypatch, release_at):
    """Make ``wt`` see Windows, with a fake ``msvcrt`` lock on a virtual clock."""
    from test_five_whys_windows_lock import _FakeCrt, _VirtualClock

    clock = _VirtualClock()
    crt = _FakeCrt(clock, release_at=release_at)
    monkeypatch.setattr(wt, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(wt, "time", clock)
    monkeypatch.setitem(sys.modules, "msvcrt", crt)
    return clock, crt


def test_tool_value_errors_do_not_end_the_stdio_server(tmp_path):
    (tmp_path / "latin.txt").write_bytes(b"caf\xe9")
    replies = _serve(
        tmp_path,
        _call(1, "edit_file", path="latin.txt", old_str="caf", new_str="x"),
        _call(2, "write_file", path="bad.txt", content="\ud800"),
        _call(3, "run_command", command="true", timeout="abc"),
        _call(4, "write_file", path="good.txt", content="ok"),
    )
    assert [reply["result"]["isError"] for reply in replies] == [True, True, True, False]
    assert (tmp_path / "good.txt").read_text() == "ok"
    assert (tmp_path / "latin.txt").read_bytes() == b"caf\xe9"


def test_lock_files_are_not_workspace_browser_entries(tmp_path):
    ws = wt.Workspace(str(tmp_path))
    ws.write_file("file.txt", "ok")
    assert ws.list_directory(".") == "file.txt"
    assert [Path(p).name for p in ws._walk(str(tmp_path))] == ["file.txt"]


def test_guest_entry_points_share_locks_outside_the_project(tmp_path, monkeypatch, capsys):
    home, project = tmp_path / "guest", tmp_path / "project"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    locks = home / ".kirocrew-worker" / "file-locks"
    for n, message in enumerate(
        [
            _call(1, "write_file", path="file.txt", content="base"),
            _call(2, "edit_file", path="file.txt", old_str="base", new_str="agent"),
        ]
    ):
        monkeypatch.setattr(
            wt.sys,
            "stdin",
            SimpleNamespace(buffer=io.BytesIO(json.dumps(message).encode() + b"\n")),
        )
        assert wt.main(["--root", str(project)]) == 0
        assert json.loads(capsys.readouterr().out)["result"]["isError"] is False
        assert len(list(locks.iterdir())) == 1, f"call {n} left its lock elsewhere"
        assert set(project.iterdir()) == {project / "file.txt"}
    assert (project / "file.txt").read_text() == "agent"


@pytest.mark.parametrize("replace_all", [None, 0, 1, 0.0, 1.0, "", "false", "true", [], {}, [1]])
@pytest.mark.parametrize("original", ["old", "old old"])
def test_edit_file_rejects_non_boolean_replace_all(tmp_path, replace_all, original):
    path = tmp_path / "file.txt"
    path.write_text(original)
    message = _call(
        1, "edit_file", path=path.name, old_str="old", new_str="new", replace_all=replace_all
    )
    [reply] = _serve(tmp_path, message)
    result = reply["result"]
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
    clock, crt = _windows_guest(monkeypatch, hold_secs)
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
    clock, crt = _windows_guest(monkeypatch, float("inf"))
    if failure == "bad_fd":
        crt.fail_with = errno.EBADF
    ws = wt.Workspace(str(tmp_path))
    expected = OSError if failure == "bad_fd" else wt.ToolError
    with pytest.raises(expected):
        with ws.write_lock("file.txt"):
            pytest.fail("unowned lock admitted a write")
    assert crt.LK_UNLCK not in crt.calls
    assert not crt.held
    assert clock.now == 0 if failure == "bad_fd" else clock.now >= wt._LOCK_TIMEOUT_SECS


needs_git = pytest.mark.skipif(not shutil.which("git"), reason="needs git")
_IDENT = ("-c", "user.email=a@b.c", "-c", "user.name=a")


def _git(where, *args):
    subprocess.run(["git", *_IDENT, "-C", str(where), *args], check=True, capture_output=True)


@needs_git
@pytest.mark.parametrize("key", ["clean", "process"])
def test_git_status_runs_no_repository_filter_driver(tmp_path, key):
    """A file-writing agent can plant a filter in .git/config; status must not run it."""
    root, marker = tmp_path / "repo", tmp_path / "ran"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "commit", "-q", "--allow-empty", "-m", "i")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "a")
    _git(root, "config", f"filter.x.y.{key}", f"touch {marker}; cat")
    (root / ".gitattributes").write_text("*.txt filter=x.y\n")
    (root / "a.txt").write_text("two\n")  # same size: status must hash it
    out = wt._git_status(wt.Workspace(str(root)))
    assert not marker.exists()
    assert out["git"] is True and "a.txt" in out["output"]


def test_read_file_pages_past_the_first_read_cap(tmp_path):
    lines = [f"line {i:06d}\n" for i in range(40000)]  # about 480 KiB
    (tmp_path / "big.txt").write_bytes("".join(lines).encode())  # LF on every OS
    ws = wt.Workspace(str(tmp_path))
    assert ws.read_file("big.txt", offset=39998, limit=5) == "".join(lines[39998:])
    whole = ws.read_file("big.txt")
    assert whole.endswith("use offset/limit]") and len(whole) < len("".join(lines))
    # A page larger than the cap is cut and says so.
    page = ws.read_file("big.txt", offset=0, limit=40000)
    assert page.endswith("use offset/limit]") and page.startswith(lines[0])


@needs_git
def test_git_status_runs_no_submodule_filter_driver(tmp_path):
    """A submodule's own config can name a filter too; status must not look inside."""
    sub, root, marker = tmp_path / "sub", tmp_path / "repo", tmp_path / "ran"

    def git(where, *args):
        _git(where, "-c", "protocol.file.allow=always", *args)

    for repo in (sub, root):
        repo.mkdir()
        git(repo, "init", "-q")
    (sub / "a.txt").write_text("one\n")
    git(sub, "add", "a.txt")
    git(sub, "commit", "-q", "-m", "a")
    git(root, "commit", "-q", "--allow-empty", "-m", "i")
    git(root, "submodule", "-q", "add", str(sub), "inner")
    git(root, "commit", "-q", "-m", "sub")
    inner = root / "inner"
    git(inner, "config", "filter.x.clean", f"touch {marker}; cat")
    (inner / ".gitattributes").write_text("*.txt filter=x\n")
    (inner / "a.txt").write_text("two\n")  # same size: status must hash it
    out = wt._git_status(wt.Workspace(str(root)))
    assert not marker.exists()
    assert out["git"] is True


def test_every_workspace_read_tool_hands_governance_a_path(tmp_path):
    # A pathless call would leave a filesystem.read ceiling nothing to judge.
    specs = {s["name"]: s["inputSchema"] for s in wt.tool_specs(str(tmp_path))}
    reads = sorted(name for name, kind in WORKSPACE_TOOL_KINDS.items() if kind == "read")
    assert {"grep", "glob", "list_directory", "read_file"} <= set(reads)
    for name in reads:
        schema = specs[name]
        assert "path" in schema["required"], name
        minimal = {key: "." for key in schema["required"]}
        assert ("filesystem.read", ".") in classify_tool_args("read", minimal), name
    (tmp_path / "secret.txt").write_text("token\n")
    out = _serve(tmp_path, _call(1, "grep", pattern="."))
    result = out[0]["result"]
    assert result["isError"] is True and "token" not in result["content"][0]["text"]


@needs_git
@pytest.mark.skipif(os.name == "nt", reason="POSIX hook script")
@pytest.mark.parametrize("where", ["hooks_dir", "hooks_path"])
def test_git_status_runs_no_repository_hook(tmp_path, where):
    """An agent can write a hook (or point core.hooksPath at one); status must not run it."""
    root, marker = tmp_path / "repo", tmp_path / "ran"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "a.txt").write_text("one\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "a")
    hooks = root / ".git" / "hooks" if where == "hooks_dir" else root / "agent-hooks"
    hooks.mkdir(exist_ok=True)
    hook = hooks / "post-index-change"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    if where == "hooks_path":
        _git(root, "config", "core.hooksPath", str(hooks))
    (root / "a.txt").write_text("two\n")  # stale stat: a plain status would refresh the index
    out = wt._git_status(wt.Workspace(str(root)))
    assert not marker.exists()
    assert out["git"] is True and "a.txt" in out["output"]


def test_no_write_reaches_the_worker_directory(tmp_path, monkeypatch):
    """The guest script runs from there, so a workspace write must never replace it."""
    worker = tmp_path / ".kirocrew-worker"
    worker.mkdir()
    monkeypatch.setattr(wt, "_SELF_DIR", str(worker))
    make_dir_link(tmp_path / "link", worker)
    ws = wt.Workspace(str(tmp_path))
    for path in (".kirocrew-worker/workspace_tools.py", "link/workspace_tools.py"):
        with pytest.raises(wt.ToolError, match="worker directory"):
            ws.write_file(path, "print('pwned')\n")
    assert not list(worker.iterdir())
    assert ws.write_file("ok.txt", "fine\n").startswith("wrote")


def test_write_allowed_when_workspace_is_on_a_different_drive(tmp_path, monkeypatch):
    """A workspace path on a different drive than _SELF_DIR must not crash or block writes.

    On a GitHub-hosted Windows runner the checkout lands on D: while TEMP/tmp_path
    lives on C:.  Before this fix ``os.path.commonpath`` raised
    ``ValueError: Paths don't have the same drive`` and the write failed even though
    the path was clearly not inside the worker directory.

    ntpath.splitdrive is injected so the cross-drive branch exercises on every platform.
    """
    import ntpath

    monkeypatch.setattr(wt.os.path, "splitdrive", ntpath.splitdrive)
    # _SELF_DIR appears to be on D: (the checkout drive on hosted Windows runners).
    monkeypatch.setattr(wt, "_SELF_DIR", "D:\\kirocrew-worker")
    ws = wt.Workspace(str(tmp_path))
    # Old code: commonpath(["C:\\...", "D:\\kirocrew-worker"]) raised ValueError.
    # New code: drives differ -> _is_within returns False -> write proceeds.
    result = ws.write_file("ok.txt", "fine\n")
    assert result.startswith("wrote")
    # Confirm the containment check still refuses a same-drive path inside the worker dir.
    worker = tmp_path / ".kirocrew-worker"
    worker.mkdir()
    monkeypatch.setattr(wt, "_SELF_DIR", str(worker))
    with pytest.raises(wt.ToolError, match="worker directory"):
        ws.write_file(".kirocrew-worker/workspace_tools.py", "x")
