"""Private scan failures retain safe branch facts, never filesystem payloads."""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path

import pytest

from kiro_crew import platform_compat, sandbox, session_map
from kiro_crew.workflows.runner import describe_agent_error

PREFIX = "memory_unavailable: cannot verify protected memory hardlinks"
PRIVATE = "private-canary-秘密-opaque"


@pytest.fixture
def scan_home(tmp_path, monkeypatch):
    root = (tmp_path / PRIVATE).resolve()
    root.mkdir()
    monkeypatch.setattr(sandbox, "config_dir", lambda: root)
    monkeypatch.setattr(session_map, "config_dir", lambda: root)
    # Every scan receives this explicit layout; never discover host roots.
    layout = sandbox._PrivateMemoryLayout((str(root),), (), ())
    return root, layout


def assert_diagnostic(error, operation, tree, code, *, winerror=None):
    windows_code = f" winerror={winerror}" if winerror is not None else ""
    expected = f"{PREFIX} (operation={operation} tree={tree} errno={code}{windows_code})"
    assert str(error) == expected
    assert describe_agent_error(error) == f"RuntimeError: {expected}"
    assert PRIVATE not in str(error)
    assert PRIVATE not in describe_agent_error(error)


@pytest.mark.parametrize("tree", ["root_tmp", "sessions", "snapshots", "memory"])
@pytest.mark.parametrize("prepare", [False, True], ids=["single-scan", "private-startup"])
def test_actual_map_publication_race_retains_operation_and_errno(
    scan_home, monkeypatch, tree, prepare
):
    root, layout = scan_home
    mapping = session_map.SessionMap()
    if tree != "root_tmp":
        mapping._path = root / tree / f"{PRIVATE}.json"
    staged, publish, done = threading.Event(), threading.Event(), threading.Event()
    temporary, failures = [], []
    original_replace, original_stat = os.replace, Path.stat

    def replace(source, destination, *args, **kwargs):
        if str(destination) == str(mapping._path):
            temporary.append(Path(source))
            staged.set()
            assert publish.wait(5), "scanner did not reach the staged file"
        return original_replace(source, destination, *args, **kwargs)

    def scan_stat(path, *args, **kwargs):
        if temporary and path == temporary[0]:
            # The file really exists with one link before the writer publishes.
            assert original_stat(path).st_nlink == 1
            publish.set()
            assert done.wait(5), "writer did not publish"
        # No fabricated exception: the OS stats the name removed by os.replace.
        return original_stat(path, *args, **kwargs)

    def write():
        try:
            mapping._write_payload('{"fixture":true}', 1)
        except BaseException as exc:
            failures.append(exc)
        finally:
            done.set()

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(Path, "stat", scan_stat)
    thread = threading.Thread(target=write)
    thread.start()
    try:
        assert staged.wait(5), "writer did not stage"
        if prepare:
            directory = Path(sandbox._prepare_private_log_dir(layout))
        else:
            with pytest.raises(RuntimeError, match=PREFIX) as caught:
                sandbox._validate_private_memory_hardlinks(layout)
    finally:
        publish.set()
        thread.join(5)
    assert not thread.is_alive()
    assert not failures
    assert mapping._written_seq == 1
    assert mapping._path.read_text(encoding="utf-8") == '{"fixture":true}'
    if prepare:
        assert directory.is_dir()
        assert directory.parent == root / "memory_stores" / ".execution-logs"
        assert not temporary[0].exists()
        return
    error = caught.value
    assert isinstance(error.__cause__, FileNotFoundError)
    assert error.__cause__.errno == errno.ENOENT
    assert error.__cause__.filename == str(temporary[0])
    assert_diagnostic(
        error, "entry_stat", tree, errno.ENOENT, winerror=getattr(error.__cause__, "winerror", None)
    )
    assert not (root / "memory_stores" / ".execution-logs").exists()
    # Publication settled: the same layout still admits healthy files.
    sandbox._validate_private_memory_hardlinks(layout)


@pytest.mark.parametrize(
    ("operation", "relative", "tree"),
    [
        ("root_iterdir", "", "memory"),
        ("entry_stat", f"{PRIVATE}.tmp", "root_tmp"),
        ("entry_iterdir", "sessions", "sessions"),
    ],
)
@pytest.mark.parametrize(
    ("code", "attempts"),
    [(errno.ENOENT, 3), (errno.EACCES, 1), (errno.EIO, 1)],
)
def test_scan_failure_retry_budget_preserves_final_diagnostic(
    scan_home, monkeypatch, operation, relative, tree, code, attempts
):
    root, layout = scan_home
    target = root / relative
    method = "stat" if operation == "entry_stat" else "iterdir"
    if method == "stat":
        target.write_text(PRIVATE, encoding="utf-8")
    else:
        target.mkdir(exist_ok=True)
    original = getattr(Path, method)
    failures = []

    def fail_scan(path, *args, **kwargs):
        if path == target:
            cause = OSError(code, PRIVATE, str(target))
            failures.append(cause)
            raise cause
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, method, fail_scan)
    with pytest.raises(RuntimeError, match=PREFIX) as caught:
        sandbox._prepare_private_log_dir(layout)
    assert len(failures) == attempts
    assert caught.value.__cause__ is failures[-1]
    assert_diagnostic(caught.value, operation, tree, code)
    assert not (root / "memory_stores" / ".execution-logs").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory mode enforcement")
@pytest.mark.parametrize("at_root", [True, False])
def test_real_unreadable_directory_still_refuses(scan_home, at_root):
    if platform_compat.local_user_id() == 0:
        pytest.skip("root bypasses POSIX directory mode checks")
    root, layout = scan_home
    target = root if at_root else root / "sessions" / PRIVATE
    target.mkdir(parents=True, exist_ok=True)
    platform_compat.chmod_safe(target, 0)
    try:
        with pytest.raises(RuntimeError, match=PREFIX) as caught:
            sandbox._prepare_private_log_dir(layout)
    finally:
        platform_compat.chmod_safe(target, 0o700)
    assert isinstance(caught.value.__cause__, PermissionError)
    assert_diagnostic(
        caught.value,
        "root_iterdir" if at_root else "entry_iterdir",
        "memory" if at_root else "sessions",
        errno.EACCES,
    )
    assert not (root / "memory_stores" / ".execution-logs").exists()


def test_real_hardlink_retains_distinct_refusal(scan_home):
    root, layout = scan_home
    protected = root / "memory.db"
    protected.write_text(PRIVATE, encoding="utf-8")
    alias = root / PRIVATE
    alias.hardlink_to(protected)
    assert protected.stat().st_nlink == 2
    with pytest.raises(RuntimeError, match="protected memory has a hardlink alias") as caught:
        sandbox._prepare_private_log_dir(layout)
    assert caught.value.__cause__ is None
    assert "operation=" not in str(caught.value)
    assert PRIVATE not in describe_agent_error(caught.value)
    assert not (root / "memory_stores" / ".execution-logs").exists()


def test_missing_required_workspace_retains_existing_refusal(scan_home):
    root, _ = scan_home
    missing = str(root / "required-workspace")
    layout = sandbox._PrivateMemoryLayout((str(root),), (missing,), (missing,))
    with pytest.raises(RuntimeError, match="required workspace directory") as caught:
        sandbox._prepare_private_log_dir(layout)
    assert missing in str(caught.value)  # Existing required-root remedy stays unchanged.
    assert "operation=" not in str(caught.value)
    assert not (root / "memory_stores" / ".execution-logs").exists()


@pytest.mark.parametrize("value", [None, True, PRIVATE, -1, 1 << 100])
def test_non_numeric_or_unbounded_codes_are_not_serialized(value):
    cause = OSError(errno.EACCES, PRIVATE, f"/{PRIVATE}")
    cause.errno = value
    cause.winerror = value
    error = sandbox._private_memory_scan_failure("entry_stat", "memory", cause)
    assert str(error) == f"{PREFIX} (operation=entry_stat tree=memory)"
    assert PRIVATE not in describe_agent_error(error)


def test_numeric_windows_code_survives_workflow_serialization():
    cause = OSError(errno.EACCES, PRIVATE, f"/{PRIVATE}")
    cause.winerror = 32
    error = sandbox._private_memory_scan_failure("entry_iterdir", "snapshots", cause)
    expected = f"{PREFIX} (operation=entry_iterdir tree=snapshots errno=13 winerror=32)"
    assert_diagnostic(error, "entry_iterdir", "snapshots", errno.EACCES, winerror=32)
    assert str(error) == expected
    assert describe_agent_error(error) == f"RuntimeError: {expected}"
    assert PRIVATE not in describe_agent_error(error)
