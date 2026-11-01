"""The sessions directory sheds staging temps and stale lock sentinels.

A writer stages a transcript as ``tmp<8>.tmp`` or ``.kc-incoming-<8>`` next to its
target and renames it into place. A process killed between the two leaves the
staging file, and nothing in the writer can remove it afterwards. The transcript
``flock`` sidecar ``<stem>.jsonl.lock`` outlives its session the same way.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew import session_staging_sweep as sweep

_NOW = 2_000_000_000.0
_OLD = _NOW - sweep.STAGING_TEMP_MIN_AGE_SECS - 1
_FRESH = _NOW - 5


def _file(directory: Path, name: str, mtime: float, data: str = "x") -> Path:
    path = directory / name
    path.write_text(data)
    os.utime(path, (mtime, mtime))
    return path


def test_an_old_default_prefix_temp_is_removed(tmp_path: Path) -> None:
    orphan = _file(tmp_path, "tmpab12_x9z.tmp", _OLD)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 1
    assert not orphan.exists()


def test_an_old_incoming_temp_is_removed(tmp_path: Path) -> None:
    orphan = _file(tmp_path, ".kc-incoming-q7w3e9r1", _OLD)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 1
    assert not orphan.exists()


def test_the_names_match_what_mkstemp_produces(tmp_path: Path) -> None:
    """The shapes are mkstemp's own output, not a guess about it."""
    for kwargs in ({"suffix": ".tmp"}, {"prefix": ".kc-incoming-"}):
        fd, name = tempfile.mkstemp(dir=tmp_path, **kwargs)
        os.close(fd)
        assert sweep.is_staging_temp_name(os.path.basename(name)), name


def test_a_fresh_temp_is_kept(tmp_path: Path) -> None:
    """A temp younger than the age gate may belong to a write in flight."""
    live = _file(tmp_path, "tmpab12_x9z.tmp", _FRESH)
    incoming = _file(tmp_path, ".kc-incoming-q7w3e9r1", _FRESH)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert live.exists() and incoming.exists()


@pytest.mark.parametrize(
    "name",
    [
        "dashboard_chat-abc.jsonl",
        ".jsonl.lock",
        "dashboard_chat-abc.lock",
        "dashboard_chat-abc.jsonl.lock.1",
        "tmpab12_x9z.jsonl",
        "tmpab12.tmp",
        "tmpab12_x9z0.tmp",
        "mytmpab12_x9z.tmp",
        ".kc-incoming-",
        ".kc-incoming-q7w3e9r1.jsonl",
        "notes.tmp",
    ],
)
def test_other_names_are_never_touched(tmp_path: Path, name: str) -> None:
    kept = _file(tmp_path, name, _OLD)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert kept.exists()


def test_a_directory_with_a_staging_name_is_kept(tmp_path: Path) -> None:
    folder = tmp_path / "tmpab12_x9z.tmp"
    folder.mkdir()
    os.utime(folder, (_OLD, _OLD))

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert folder.is_dir()


@requires_symlinks
def test_a_link_with_a_staging_name_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _file(outside, "keep.txt", _OLD)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "tmpab12_x9z.tmp").symlink_to(target)

    assert sweep.sweep_session_staging_temps(sessions, now=_NOW) == 0
    assert target.exists()
    assert os.path.lexists(sessions / "tmpab12_x9z.tmp")


def test_only_direct_children_are_examined(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    nested = _file(archive, "tmpab12_x9z.tmp", _OLD)

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert nested.exists()


def test_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert sweep.sweep_session_staging_temps(tmp_path / "absent", now=_NOW) == 0


def test_the_default_directory_is_the_data_home_sessions_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sweep, "data_home", lambda: tmp_path)
    sessions = tmp_path / sweep.SESSIONS_DIR_NAME
    sessions.mkdir()
    orphan = _file(sessions, "tmpab12_x9z.tmp", time.time() - sweep.STAGING_TEMP_MIN_AGE_SECS - 60)

    assert sweep.sweep_session_staging_temps() == 1
    assert not orphan.exists()


def test_the_cleanup_tick_runs_the_sweep_off_the_event_loop() -> None:
    """The janitor's pass calls the dependency on its maintenance executor."""
    from types import SimpleNamespace

    from kiro_crew.session_cleanup import SessionCleanup

    calls: list[str] = []
    loop_thread: list[int] = []

    def fake_sweep() -> int:
        import threading

        calls.append("swept")
        loop_thread.append(threading.get_ident())
        return 2

    pool = ThreadPoolExecutor(max_workers=1)
    deps = SimpleNamespace(
        get_maintenance_executor=lambda: pool,
        sweep_session_staging_temps=fake_sweep,
        logger=logging.getLogger("test.staging_sweep"),
    )
    service = SimpleNamespace(_deps=deps)

    async def run() -> int:
        import threading

        await SessionCleanup._sweep_session_staging_temps(service)  # type: ignore[arg-type]
        return threading.get_ident()

    try:
        main_thread = asyncio.run(run())
    finally:
        pool.shutdown(wait=True)

    assert calls == ["swept"]
    assert loop_thread[0] != main_thread


def test_a_failing_sweep_does_not_break_the_tick(caplog: pytest.LogCaptureFixture) -> None:
    from types import SimpleNamespace

    from kiro_crew.session_cleanup import SessionCleanup

    def boom() -> int:
        raise OSError("disk gone")

    pool = ThreadPoolExecutor(max_workers=1)
    deps = SimpleNamespace(
        get_maintenance_executor=lambda: pool,
        sweep_session_staging_temps=boom,
        logger=logging.getLogger("test.staging_sweep"),
    )
    try:
        with caplog.at_level(logging.DEBUG, logger="test.staging_sweep"):
            asyncio.run(
                SessionCleanup._sweep_session_staging_temps(SimpleNamespace(_deps=deps))  # type: ignore[arg-type]
            )
    finally:
        pool.shutdown(wait=True)
    own = [r.getMessage() for r in caplog.records if r.name == "test.staging_sweep"]
    assert own == ["session staging temp sweep failed: OSError"]


# ── *.jsonl.lock sentinels ────────────────────────────────────────────────────


def test_an_old_unheld_lock_without_a_transcript_is_removed(tmp_path: Path) -> None:
    departed = _file(tmp_path, "subagent_1.jsonl.lock", _OLD, data="")
    chat = _file(tmp_path, "dashboard_chat-abc.jsonl.lock", _OLD, data="")

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 2
    assert not departed.exists() and not chat.exists()


def test_a_lock_beside_its_transcript_is_kept(tmp_path: Path) -> None:
    _file(tmp_path, "dashboard_chat-abc.jsonl", _OLD)
    lock = _file(tmp_path, "dashboard_chat-abc.jsonl.lock", _OLD, data="")

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert lock.exists()


def test_a_fresh_lock_is_kept(tmp_path: Path) -> None:
    lock = _file(tmp_path, "subagent_1.jsonl.lock", _FRESH, data="")

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert lock.exists()


def test_a_lock_another_descriptor_holds_is_kept(tmp_path: Path) -> None:
    """A held lock belongs to a live writer, however old the file is."""
    from kiro_crew import platform_compat

    lock = _file(tmp_path, "subagent_1.jsonl.lock", _OLD, data="")
    fd = os.open(str(lock), os.O_RDWR)
    try:
        assert platform_compat.try_acquire_lock(fd, exclusive=True)
        try:
            assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
        finally:
            platform_compat.release_lock(fd)
    finally:
        os.close(fd)
    assert lock.exists()


def test_a_lock_the_transcript_log_holds_is_kept(tmp_path: Path) -> None:
    """The gateway's own in-process hold blocks the prune too."""
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    with log.atomic_appends("k"):
        lock = log._lock_path("k")
        os.utime(lock, (_OLD, _OLD))
        assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
        assert lock.exists()


def test_a_directory_with_a_lock_name_is_kept(tmp_path: Path) -> None:
    folder = tmp_path / "subagent_1.jsonl.lock"
    folder.mkdir()
    os.utime(folder, (_OLD, _OLD))

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert folder.is_dir()


@requires_symlinks
def test_a_link_with_a_lock_name_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = _file(outside, "keep.jsonl.lock", _OLD, data="")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "subagent_1.jsonl.lock").symlink_to(target)

    assert sweep.sweep_session_staging_temps(sessions, now=_NOW) == 0
    assert target.exists()
    assert os.path.lexists(sessions / "subagent_1.jsonl.lock")


def test_a_nested_lock_is_not_examined(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    nested = _file(archive, "subagent_1.jsonl.lock", _OLD, data="")

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 0
    assert nested.exists()


def test_a_pruned_session_lock_is_recreated_on_the_next_write(tmp_path: Path) -> None:
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    log.append("k", "user", "m")
    assert log.delete_session("k") is True
    lock = log._lock_path("k")
    os.utime(lock, (_OLD, _OLD))

    assert sweep.sweep_session_staging_temps(tmp_path, now=_NOW) == 1
    assert not lock.exists()

    log.append("k", "user", "again")
    assert lock.exists()
    assert log._path("k").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only: Windows cannot unlink an open sidecar")
def test_a_writer_that_locked_a_pruned_inode_reopens_the_name(tmp_path: Path) -> None:
    """A descriptor opened before the prune must not count as holding the lock.

    Otherwise that writer would hold an inode no one else can reach while the next
    opener creates and locks a fresh sidecar: two holders of one transcript.
    """
    from kiro_crew import history_locks, platform_compat
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    lock = log._lock_path("k")
    stale = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    os.unlink(lock)
    state = [stale, 1, 0]

    assert (
        history_locks._try_acquire_named_sidecar(lock, state, ConversationLog._flock_guard) is True
    )
    try:
        assert state[0] != stale
        held, named = os.fstat(state[0]), os.stat(lock)
        assert (held.st_dev, held.st_ino) == (named.st_dev, named.st_ino)
        probe = os.open(str(lock), os.O_RDWR)
        try:
            assert not platform_compat.try_acquire_lock(probe, exclusive=True)
        finally:
            os.close(probe)
    finally:
        platform_compat.release_lock(state[0])
        os.close(state[0])


def test_a_writer_whose_sidecar_was_not_pruned_keeps_its_descriptor(tmp_path: Path) -> None:
    from kiro_crew import history_locks, platform_compat
    from kiro_crew.history import ConversationLog

    log = ConversationLog(base_dir=tmp_path)
    lock = log._lock_path("k")
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
    state = [fd, 1, 0]
    try:
        assert (
            history_locks._try_acquire_named_sidecar(lock, state, ConversationLog._flock_guard)
            is True
        )
        assert state[0] == fd
        platform_compat.release_lock(fd)
    finally:
        os.close(fd)
