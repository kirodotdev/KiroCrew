"""Protected publication and bounded reclamation; all state belongs to the fixture."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import member_memory_auth as auth
from kiro_crew import member_process_records as records
from kiro_crew import platform_compat as pc

PID = 424242


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    pc.make_owner_only_dir(root)
    pc.restrict_dir_to_owner(root)
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    with records.record_directory(root, create=True):
        pass
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: None)
    monkeypatch.setattr(pc, "pid_liveness", lambda pid: pc.PID_UNSIGNALABLE)
    # No fabricated PID reaches a host Windows table either.
    monkeypatch.setattr(pc, "pid_confirmed_absent", lambda pid: False)
    return root


def _row(*, namespace=False, version=2, start="old", store=""):
    if namespace:
        return {"process_start": start, "namespaces": [[1, 2], [1, 3]], "private_memory": False}
    return {
        "version": version,
        "session_key": "test:session",
        "process_start": start,
        "memory_store": store,
    }


def _put(home, *, namespace=False, row=None, pid=PID):
    path = (
        home / "member-memory-bindings" / "pids" / f"{pid}{'.namespace' if namespace else ''}.json"
    )
    path.write_text(
        json.dumps(row if row is not None else _row(namespace=namespace)), encoding="utf-8"
    )
    pc.restrict_to_owner(path)
    return path


def _sweep(home, cursor=None):
    return records.reclaim_stale_member_bindings(data_home=home, cursor=cursor)


@pytest.mark.parametrize("namespace", [False, True])
@pytest.mark.parametrize("verdict", ["dead", "reused", "alive", "unknown"])
def test_both_families_without_txt(home, monkeypatch, namespace, verdict):
    path = _put(home, namespace=namespace)
    monkeypatch.setattr(
        pc, "get_process_start_id", lambda pid: {"reused": "new", "alive": "old"}.get(verdict)
    )
    monkeypatch.setattr(
        pc, "pid_liveness", lambda pid: pc.PID_DEAD if verdict == "dead" else pc.PID_UNSIGNALABLE
    )
    monkeypatch.setattr(pc, "pid_confirmed_absent", lambda pid: verdict == "dead")
    count, cursor = _sweep(home)
    assert cursor is None
    assert count == int(verdict in {"dead", "reused"})
    assert path.exists() is (verdict not in {"dead", "reused"})
    assert not list(home.glob("session_pid_*.txt"))


@pytest.mark.parametrize("version,store", [(1, "member-x"), (2, ""), (2, "member-x")])
def test_live_binding_schemas_are_preserved(home, monkeypatch, version, store):
    row = _row(version=version, store=store)
    path = _put(home, row=row)
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "old")
    assert _sweep(home) == (0, None)
    assert json.loads(path.read_text()) == row


@pytest.mark.parametrize("namespace", [False, True])
@pytest.mark.parametrize(
    "mutation",
    ["bool-version", "future", "empty", "wrong-family", "extra", "bad-start", "bad-shape"],
)
def test_malformed_future_and_wrong_family_retained(home, monkeypatch, namespace, mutation):
    row = _row(namespace=namespace)
    if mutation == "bool-version":
        row = {**_row(store="member-x"), "version": True}
    elif mutation == "future":
        row["version"] = 3
    elif mutation == "empty":
        row = {}
    elif mutation == "wrong-family":
        row = _row(namespace=not namespace)
    elif mutation == "extra":
        row["future_field"] = 1
    elif mutation == "bad-start":
        row["process_start"] = False
    elif namespace:
        row["namespaces"] = [[True, 2], [1, 3]]
    else:
        row["session_key"] = []
    path = _put(home, namespace=namespace, row=row)
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "new")
    assert _sweep(home) == (0, None)
    assert path.exists()


@pytest.mark.parametrize("payload", ["{bad", "[]", '"text"', "x" * (records.MAX_RECORD_BYTES + 1)])
def test_unreadable_payload_retained(home, monkeypatch, payload):
    path = _put(home)
    path.write_text(payload)
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "new")
    assert _sweep(home) == (0, None)
    assert path.read_text() == payload


@pytest.mark.parametrize(
    "name", ["01.json", "1.json", "424242.txt", "tmpfoo.tmp", "4294967296.json"]
)
def test_noncanonical_names_retained(home, monkeypatch, name):
    path = _put(home).with_name(name)
    path.write_text(json.dumps(_row()))
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "new")
    _sweep(home)
    assert path.exists()


@pytest.mark.parametrize(
    "kind",
    [
        pytest.param(
            "symlink", marks=pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX symlink fixture")
        ),
        "hardlink",
        pytest.param("fifo", marks=pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX FIFO")),
        "directory",
        pytest.param(
            "foreign", marks=pytest.mark.skipif(not pc.IS_POSIX, reason="POSIX uid injection")
        ),
    ],
)
@pytest.mark.parametrize("lock", [False, True])
def test_unsafe_lock_and_records_refused(home, monkeypatch, kind, lock):
    path = _put(home)
    target = path.with_name(records.LOCK_NAME) if lock else path
    target.unlink(missing_ok=True)
    foreign = home / "foreign"
    foreign.write_text("untouched")
    if kind == "symlink":
        target.symlink_to(foreign)
    elif kind == "hardlink":
        os.link(foreign, target)
    elif kind == "fifo":
        os.mkfifo(target)
        native_open = os.open

        def nonblocking_open(name, flags, *args, **kwargs):
            if os.fspath(name) in (target.name, str(target)):
                assert flags & os.O_NONBLOCK, "FIFO must be rejected without waiting for a writer"
            return native_open(name, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", nonblocking_open)
    elif kind == "directory":
        target.mkdir()
    else:
        target.write_text(json.dumps(_row()))
        real = os.fstat

        def foreign_stat(fd):
            info = real(fd)
            if (info.st_dev, info.st_ino) == (target.stat().st_dev, target.stat().st_ino):
                fields = {
                    key: getattr(info, key)
                    for key in ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size")
                }
                return SimpleNamespace(**fields, st_uid=-1)
            return info

        monkeypatch.setattr(os, "fstat", foreign_stat)
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "new")
    assert _sweep(home) == (0, None)
    if lock:
        auth.publish_member_session_pid(PID, "replacement", home=home, memory_store="")
        assert json.loads(path.read_text())["process_start"] == "old"
    assert os.path.lexists(target)
    assert foreign.read_text() == "untouched"


def test_lock_body_error_is_not_reentered(home):
    with records.record_directory(home) as directory:
        with pytest.raises(OSError, match="body fault"):
            with records.record_lock(directory):
                raise OSError("body fault")
        with records.record_lock(directory):
            pass


@pytest.mark.parametrize("start,key", [("new", "valid"), (None, "valid"), ("new", "")])
def test_publication_and_each_invalidation_skip_when_contention_outlives_bound(
    home, monkeypatch, caplog, start, key
):
    path = _put(home)
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: start)
    monkeypatch.setattr(records, "LOCK_WAIT_SECS", 0.1)
    with records.record_directory(home) as directory, records.record_lock(directory):
        began = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=records.__name__):
            auth.publish_member_session_pid(PID, key, home=home, memory_store="")
        waited = time.monotonic() - began
        # Bounded: waited for the bound, never for the holder, and said so.
        assert 0.1 <= waited < 1.5, waited
        assert "process record lock held for more than 0.1s" in caplog.text
        assert _sweep(home) == (0, None)
        assert json.loads(path.read_text())["process_start"] == "old"
    auth.publish_member_session_pid(PID, key, home=home, memory_store="")
    assert path.exists() is bool(start and key)
    if path.exists():
        assert json.loads(path.read_text())["process_start"] == "new"


@pytest.mark.parametrize("start,key", [("new", "valid"), (None, "valid"), ("new", "")])
def test_publication_racing_held_lock_lands_once_released(home, monkeypatch, caplog, start, key):
    path = _put(home)
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: start)
    contended, landed = threading.Event(), threading.Event()
    native_sleep = records.time.sleep

    def sleep(secs):
        # The module sleeps only inside its contention retry loop.
        contended.set()
        native_sleep(secs)

    monkeypatch.setattr(records.time, "sleep", sleep)

    def publish():
        auth.publish_member_session_pid(PID, key, home=home, memory_store="")
        landed.set()

    publisher = threading.Thread(target=publish, daemon=True)
    try:
        with records.record_directory(home) as directory, records.record_lock(directory):
            with caplog.at_level(logging.WARNING, logger=records.__name__):
                publisher.start()
                # Causal: the publisher observed our lock and is waiting, not skipping.
                assert contended.wait(5), "publisher never contended for the held lock"
                assert not landed.is_set()
                assert json.loads(path.read_text())["process_start"] == "old"
        assert landed.wait(records.LOCK_WAIT_SECS), "publisher did not land after release"
    finally:
        publisher.join(timeout=records.LOCK_WAIT_SECS + 5)
    assert not publisher.is_alive()
    assert caplog.text == ""
    assert path.exists() is bool(start and key)
    if path.exists():
        assert json.loads(path.read_text())["process_start"] == "new"


def test_unsafe_lock_object_is_refused_without_waiting(home, monkeypatch):
    path = _put(home)
    lock = path.with_name(records.LOCK_NAME)
    lock.unlink(missing_ok=True)
    lock.mkdir()
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "new")
    began = time.monotonic()
    auth.publish_member_session_pid(PID, "valid", home=home, memory_store="")
    # Not contention: an unsafe lock object skips at once, no bounded wait.
    assert time.monotonic() - began < records.LOCK_WAIT_SECS / 2
    assert json.loads(path.read_text())["process_start"] == "old"


def test_lock_failure_never_publishes(home, monkeypatch):
    @contextmanager
    def refuse(*args, **kwargs):
        raise OSError("cannot lock")
        yield

    monkeypatch.setattr(pc, "file_lock", refuse)
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "new")
    auth.publish_member_session_pid(PID, "valid", home=home, memory_store="")
    assert not (home / "member-memory-bindings" / "pids" / f"{PID}.json").exists()


def _read_line(stream, budget: float) -> str:
    """One handshake line within ``budget`` on a reader thread (no select on pipes)."""
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(stream.readline()), daemon=True)
    reader.start()
    reader.join(budget)
    assert box, f"no handshake line within {budget}s"
    return box[0].strip()


def test_subprocess_publisher_waits_on_same_lock_then_lands(home):
    path = _put(home)
    source = Path(records.__file__).resolve().parents[1]
    script = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, {str(source)!r})
        from kiro_crew import member_process_records as r
        r.platform_compat.get_process_start_id = lambda pid: 'new'
        _sleep, _seen = time.sleep, []
        def sleep(secs):
            # The module sleeps only inside its contention retry loop.
            if not _seen:
                _seen.append(1)
                print('contending', flush=True)
            _sleep(secs)
        r.time.sleep = sleep
        r.publish_binding(Path({str(home)!r}), {PID}, 'test:new', '')
        """)
    child = None
    try:
        # Hold the lock BEFORE the publisher starts, so it must contend.
        with records.record_directory(home) as directory, records.record_lock(directory):
            child = subprocess.Popen(
                [sys.executable, "-I", "-c", script],
                cwd=home,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            assert _read_line(child.stdout, 10) == "contending"
            assert json.loads(path.read_text())["process_start"] == "old"
        _, stderr = child.communicate(timeout=records.LOCK_WAIT_SECS + 5)
        assert child.returncode == 0, stderr
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
    assert json.loads(path.read_text())["process_start"] == "new"


def test_bounded_enumeration_progress_includes_nonrecords(home, monkeypatch):
    pids = home / "member-memory-bindings" / "pids"
    names = []
    for i in range(9):
        name = f"tmp{i}.tmp"
        (pids / name).write_text("keep")
        names.append(name)
    for i in range(5):
        names.append(_put(home, pid=PID + i).name)
    visited = []

    class Entries:
        closed = False

        def __init__(self):
            self.names = iter(names)

        def __next__(self):
            name = next(self.names)
            visited.append(name)
            return SimpleNamespace(name=name)

        def close(self):
            self.closed = True

    entries = Entries()
    real_scandir = records.os.scandir
    monkeypatch.setattr(records.os, "scandir", lambda _: entries)
    monkeypatch.setattr(records, "SCAN_BUDGET", 3)
    monkeypatch.setattr(records, "_owner_gone", lambda *args: True)
    cursor = None
    removed = 0
    try:
        for _ in range(6):
            before = len(visited)
            count, cursor = _sweep(home, cursor)
            removed += count
            assert len(visited) - before <= 3
            if cursor is None:
                break
        assert visited == names
        assert removed == 5
        assert entries.closed
    finally:
        monkeypatch.setattr(records.os, "scandir", real_scandir)
        if cursor is not None:
            cursor.close()


@pytest.mark.parametrize(
    "handle,error,query,code,expected",
    [
        (0, 87, False, 0, True),
        (0, 5, False, 0, False),
        (0, 0, False, 0, False),
        (0, 8, False, 0, False),
        (42, 0, True, 0, True),
        (42, 0, True, 259, False),
        (42, 0, False, 0, False),
    ],
)
def test_windows_positive_absence_not_unknown(monkeypatch, handle, error, query, code, expected):
    from unittest.mock import Mock

    closed = []

    def exit_code(h, output):
        output._obj.value = code
        return query

    kernel = SimpleNamespace(
        OpenProcess=Mock(return_value=handle),
        GetExitCodeProcess=Mock(side_effect=exit_code),
        CloseHandle=Mock(side_effect=closed.append),
    )
    monkeypatch.setattr(pc, "IS_POSIX", False)
    monkeypatch.setattr(pc.ctypes, "WinDLL", lambda *a, **kw: kernel, raising=False)
    monkeypatch.setattr(pc, "_windows_last_error", lambda: error)
    assert pc.pid_confirmed_absent(PID) is expected
    assert closed == ([handle] if handle else [])


@pytest.mark.asyncio
async def test_maintenance_progress_and_cancellation_drain(home):
    from kiro_crew.session_cleanup import CleanupState, SessionCleanup

    started, release = threading.Event(), threading.Event()
    cursor = SimpleNamespace(close=lambda: closed.append(True))
    calls, closed = [], []

    def sweep(previous):
        calls.append(previous)
        started.set()
        assert release.wait(5)
        return 1, cursor

    owner = SimpleNamespace(
        state=CleanupState(),
        _deps=SimpleNamespace(
            get_maintenance_executor=lambda: None,
            reclaim_member_bindings=sweep,
            logger=SimpleNamespace(info=lambda *a: None, debug=lambda *a: None),
        ),
    )
    task = asyncio.create_task(SessionCleanup._sweep_member_bindings(owner))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert calls == [None]
        assert closed == [True]
        assert owner.state.member_reclaim_cursor is None
    finally:
        release.set()
        if not task.done():
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_cursor", [False, True])
async def test_maintenance_double_cancellation_drains_worker(existing_cursor):
    from kiro_crew.session_cleanup import CleanupState, SessionCleanup

    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release, settled = threading.Event(), threading.Event()
    calls, closed = [], []
    cursor = records.RecordScan(
        SimpleNamespace(close=lambda: closed.append(settled.is_set())), (1, 2)
    )
    previous = cursor if existing_cursor else None

    def sweep(current):
        calls.append(current)
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5), "test did not release the worker"
            return 1, cursor
        finally:
            settled.set()

    async def loop_barrier():
        # FIFO callback ordering lets the cancelled task run before this test
        # resumes: the second cancel reaches the drain, not the initial await.
        reached = loop.create_future()
        loop.call_soon(reached.set_result, None)
        await reached

    owner = SimpleNamespace(
        state=CleanupState(member_reclaim_cursor=previous),
        _deps=SimpleNamespace(
            get_maintenance_executor=lambda: None,
            reclaim_member_bindings=sweep,
            logger=SimpleNamespace(info=lambda *a: None, debug=lambda *a: None),
        ),
    )
    task = asyncio.create_task(SessionCleanup._sweep_member_bindings(owner))
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert task.cancel("first cancellation")
        await loop_barrier()
        assert task.cancel("second cancellation")
        await loop_barrier()
        assert not settled.is_set()
        assert not task.done(), "cleanup completed while its executor worker was still active"
        assert closed == [], "cursor closed while its executor worker was still active"
        assert owner.state.member_reclaim_cursor is previous
        release.set()
        with pytest.raises(asyncio.CancelledError, match="first cancellation"):
            await asyncio.wait_for(task, 5)
        assert settled.is_set()
        assert calls == [previous]
        assert closed == [True]
        assert owner.state.member_reclaim_cursor is None
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        assert await asyncio.to_thread(settled.wait, 5)
        # The negative control leaks the newly returned cursor; close it only
        # after the worker settles, keeping that failing run isolated too.
        if not closed:
            cursor.close()


@pytest.mark.skipif(not pc.IS_POSIX, reason="inject native Windows mechanics on a POSIX test host")
@pytest.mark.parametrize("trusted", [True, False])
def test_windows_reclamation_uses_pinned_paths_not_dir_fd(home, monkeypatch, trusted):
    from kiro_crew import windows_acl

    path = _put(home)
    native_open, native_scandir = os.open, os.scandir
    pins = []

    def pin(path):
        pins.append(Path(path))
        return native_open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)

    def open_by_path(path, flags, mode=0o777, **kwargs):
        assert "dir_fd" not in kwargs, "Windows cannot use descriptor-relative opens"
        return native_open(path, flags, mode)

    def scan_by_path(path):
        assert isinstance(path, Path), "Windows cannot enumerate a directory fd"
        return native_scandir(path)

    @contextmanager
    def locked(*args, **kwargs):
        yield

    monkeypatch.setattr(pc, "IS_POSIX", False)
    monkeypatch.setattr(pc, "pin_directory", pin)
    monkeypatch.setattr(pc, "open_file_no_reparse", lambda path, **kw: pin(path))
    monkeypatch.setattr(pc, "file_lock", locked)
    monkeypatch.setattr(pc, "current_user_sid", lambda: "fixture-user")
    monkeypatch.setattr(pc, "pid_confirmed_absent", lambda pid: True)
    monkeypatch.setattr(
        windows_acl,
        "describe",
        lambda path: SimpleNamespace(
            owner_sid="fixture-user" if trusted else "foreign",
            volume_is_local=True,
            null_dacl=False,
            unparsable_ace_types=(),
            writers=(),
        ),
    )
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_by_path)
        patch.setattr(os, "scandir", scan_by_path)
        assert _sweep(home) == (int(trusted), None)
    assert path.exists() is (not trusted)
    assert home in pins


def test_real_streaming_cursor_reaches_dead_records_after_live_prefix(home, monkeypatch):
    for i in range(12):
        _put(home, pid=PID + i)
    monkeypatch.setattr(records, "SCAN_BUDGET", 3)
    monkeypatch.setattr(records, "_owner_gone", lambda pid, start: pid >= PID + 6)
    cursor = None
    removed = 0
    try:
        for _ in range(8):
            count, cursor = _sweep(home, cursor)
            removed += count
            if cursor is None:
                break
        assert cursor is None
        assert removed == 6
        remaining = list((home / "member-memory-bindings" / "pids").glob("*.json"))
        assert {int(path.stem) for path in remaining} == set(range(PID, PID + 6))
    finally:
        if cursor is not None:
            cursor.close()


@pytest.mark.parametrize("operation", ["publish", "unknown-start", "invalid-key", "reap"])
def test_actual_module_mutation_keeps_lock_held(home, monkeypatch, operation):
    _put(home)
    monkeypatch.setattr(
        pc, "get_process_start_id", lambda pid: None if operation == "unknown-start" else "new"
    )
    observed = []

    def assert_locked():
        with records.record_directory(home) as directory:
            with pytest.raises(BlockingIOError):
                with records.record_lock(directory):
                    pass
        observed.append(operation)

    native_unlink = records.PinnedTargetDir.unlink

    def unlink(directory, name):
        assert_locked()
        return native_unlink(directory, name)

    native_at, native_atomic = records.atomic_write_at, records.atomic_write

    def write_at(*args, **kwargs):
        assert_locked()
        return native_at(*args, **kwargs)

    def write(*args, **kwargs):
        assert_locked()
        return native_atomic(*args, **kwargs)

    monkeypatch.setattr(records.PinnedTargetDir, "unlink", unlink)
    monkeypatch.setattr(records, "atomic_write_at", write_at)
    monkeypatch.setattr(records, "atomic_write", write)
    if operation == "reap":
        assert _sweep(home) == (1, None)
    else:
        auth.publish_member_session_pid(
            PID, "" if operation == "invalid-key" else "new", home=home, memory_store=""
        )
    assert observed == [operation]


def test_non_utf8_record_is_retained(home, monkeypatch):
    path = _put(home)
    path.write_bytes(json.dumps(_row()).encode("utf-16"))
    monkeypatch.setattr(pc, "get_process_start_id", lambda pid: "new")
    assert _sweep(home) == (0, None)
    assert path.exists()


@pytest.mark.asyncio
async def test_cleanup_loop_closes_scan_at_shutdown():
    from unittest.mock import AsyncMock, Mock

    from kiro_crew.session_cleanup import CleanupState, SessionCleanup

    cursor = Mock()
    owner = SimpleNamespace(
        state=CleanupState(member_reclaim_cursor=cursor),
        _adopt_idle_policy=lambda: 300,
        _sweep_sandbox_artifacts=AsyncMock(),
        _run_cleanup_ticks=AsyncMock(),
    )
    await SessionCleanup._cleanup_loop(owner)
    cursor.close.assert_called_once()
    assert owner.state.member_reclaim_cursor is None
