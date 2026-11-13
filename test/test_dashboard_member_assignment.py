"""Dashboard member grants validate current ownership before publication."""

from __future__ import annotations

import asyncio
import copy
import os
import threading

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_persistence
from kiro_crew.execution_context import read_session_execution
from kiro_crew.member_memory_auth import read_private_session_store
from kiro_crew.memory_stores import UnknownMemoryStore, provision_member_memory
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture
def member_stores():
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    writer = provision_member_memory(cfg, "writer")
    reviewer = provision_member_memory(cfg, "reviewer")
    cfg.save()
    return writer, reviewer


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "recreated", "reassigned"])
async def test_dashboard_pin_refuses_stale_member_generation(
    tmp_path, member_stores, monkeypatch, change
):
    """A grant uses current membership, not its request-entry snapshot."""
    writer, reviewer = member_stores
    stale = KiroCrewConfig.load()
    current = copy.deepcopy(stale)
    if change == "deleted":
        del current.agents["writer"]
    elif change == "recreated":
        replacement = f"{writer}-recreated"
        current.agents["writer"].memory_store = replacement
        current.memory_stores[replacement] = copy.deepcopy(current.memory_stores[writer])
    else:
        current.agents["writer"].memory_store = reviewer

    called = False

    def assignment(*args, **kwargs):
        nonlocal called
        called = True
        return writer

    monkeypatch.setattr(chat_persistence.KiroCrewConfig, "load", lambda: current)
    monkeypatch.setattr(chat_persistence, "_pin_private_agent_assignment", assignment)
    state = _make_state(tmp_path)
    with pytest.raises(UnknownMemoryStore, match="changed during private memory assignment"):
        await chat_persistence.pin_private_agent_store(
            state, f"dashboard:stale-{change}", "writer", stale
        )
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["became_private", "became_non_private"])
async def test_dashboard_pin_refuses_privateness_changed_during_wait(
    tmp_path, member_stores, monkeypatch, direction
):
    """The private/non-private dispatch is decided from current config, not entry.

    A stale snapshot would write a newly private member's content to the shared store,
    or pin a retired store private.
    """
    writer, _ = member_stores
    private = KiroCrewConfig.load()
    non_private = copy.deepcopy(private)
    non_private.agents["writer"].memory_store = ""
    stale, current = (
        (non_private, private) if direction == "became_private" else (private, non_private)
    )

    called = False

    def assignment(*args, **kwargs):
        nonlocal called
        called = True
        return writer

    monkeypatch.setattr(chat_persistence.KiroCrewConfig, "load", lambda: current)
    monkeypatch.setattr(chat_persistence, "_pin_private_agent_assignment", assignment)
    state = _make_state(tmp_path)
    with pytest.raises(UnknownMemoryStore, match="changed during private memory assignment"):
        await chat_persistence.pin_private_agent_store(
            state, f"dashboard:privateness-{direction}", "writer", stale
        )
    assert not called
    assert (
        await asyncio.to_thread(read_private_session_store, f"dashboard:privateness-{direction}")
        is None
    )


def _writer_database(store: str):
    from kiro_crew.memory_stores import MEMORY_DB_FILE, _named_store_dir

    return _named_store_dir(store) / MEMORY_DB_FILE


@pytest.mark.asyncio
async def test_dashboard_pin_recreates_a_deleted_member_database(
    tmp_path, member_stores, monkeypatch
):
    """A Bot whose own memory.db was deleted opens on a new empty store with its identity."""
    import stat

    from kiro_crew import platform_compat
    from kiro_crew.vector_memory import read_member_database_identity

    writer, _ = member_stores
    database = _writer_database(writer)
    database.unlink()
    pinned: list[str] = []
    monkeypatch.setattr(
        chat_persistence,
        "_pin_private_agent_assignment",
        lambda *args, **kwargs: pinned.append(args[1]) or writer,
    )
    state = _make_state(tmp_path)
    assigned = await chat_persistence.pin_private_agent_store(
        state, "dashboard:deleted-db", "writer", KiroCrewConfig.load()
    )
    assert assigned == writer
    assert pinned == ["writer"]
    member_id = KiroCrewConfig.load().agents["writer"].member_id
    assert read_member_database_identity(database) == (member_id, writer)
    assert not list(database.parent.glob(f"{database.name}.repair-*"))
    assert stat.S_ISREG(database.lstat().st_mode)
    if platform_compat.IS_POSIX:
        assert stat.S_IMODE(database.stat().st_mode) == 0o600


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="reads the descriptor's access mode with fcntl")
async def test_dashboard_pin_fsyncs_the_recreated_database_through_a_writable_handle(
    tmp_path, member_stores, monkeypatch
):
    """Windows refuses fsync on a read-only file handle, so the repair must not use one."""
    import errno
    import fcntl
    import stat

    real_fsync = os.fsync

    def windows_like_fsync(fd: int) -> None:
        # Mirror Windows: a regular file opened read-only cannot be flushed.
        read_only = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        if stat.S_ISREG(os.fstat(fd).st_mode) and read_only:
            raise OSError(errno.EBADF, "Bad file descriptor")
        real_fsync(fd)

    writer, _ = member_stores
    database = _writer_database(writer)
    database.unlink()
    monkeypatch.setattr(
        chat_persistence, "_pin_private_agent_assignment", lambda *args, **kwargs: writer
    )
    monkeypatch.setattr(os, "fsync", windows_like_fsync)
    state = _make_state(tmp_path)
    assigned = await chat_persistence.pin_private_agent_store(
        state, "dashboard:fsync-handle", "writer", KiroCrewConfig.load()
    )
    assert assigned == writer
    assert database.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires unlinking an open SQLite database")
async def test_dashboard_pin_releases_cached_deleted_member_database(
    tmp_path, member_stores, monkeypatch
):
    """A prepared turn must not keep learning into the deleted database's handle."""
    from kiro_crew import context, embeddings

    writer, _ = member_stores
    monkeypatch.setattr(embeddings, "model_file_present", lambda: False)
    builder = context.ContextBuilder
    try:
        old = await builder.ensure_store(writer)
        assert old is not None
        memory = await asyncio.to_thread(builder.get_memory_for, memory_store=writer)
        assert memory.vector_store is old
        assert await asyncio.to_thread(
            old.write_lesson, "Keep database backups", rule_emb_resolved=True
        )
        assert await asyncio.to_thread(old.get_lessons)
        _writer_database(writer).unlink()

        state = _make_state(tmp_path)
        assert (
            await chat_persistence.pin_private_agent_store(
                state, "dashboard:cached-deleted-db", "writer", KiroCrewConfig.load()
            )
            == writer
        )
        assert writer not in context._vector_stores
        assert f"store:{writer}" not in context._memory_stores
        assert f"store:{writer}" not in context._lesson_stores
        assert memory.vector_store is None
        assert old._db is None
        fresh = await builder.ensure_store(writer)
        assert fresh is not None and fresh is not old
        assert await asyncio.to_thread(fresh.get_lessons) == []
        assert await asyncio.to_thread(
            fresh.write_lesson, "Check replacement databases", rule_emb_resolved=True
        )
        await asyncio.to_thread(context.release_cached_memory_store, writer)
        reopened = await builder.ensure_store(writer)
        assert reopened is not None
        assert len(await asyncio.to_thread(reopened.get_lessons)) == 1
    finally:
        await asyncio.to_thread(context.release_cached_memory_store, writer)


@pytest.mark.asyncio
async def test_dashboard_pin_releases_cached_database_before_namespace_lock(
    tmp_path, member_stores, monkeypatch
):
    """Cache eviction cannot invert context's cache-lock then namespace-lock order."""
    from kiro_crew import context, memory_stores

    writer, _ = member_stores
    _writer_database(writer).unlink()
    released: list[str] = []
    real_release = context.release_cached_memory_store

    def release_outside_namespace(store):
        assert not getattr(memory_stores._NAMESPACE_LOCK_STATE, "roots", set())
        released.append(store)
        real_release(store)

    monkeypatch.setattr(context, "release_cached_memory_store", release_outside_namespace)
    state = _make_state(tmp_path)
    assert (
        await chat_persistence.pin_private_agent_store(
            state, "dashboard:release-before-namespace", "writer", KiroCrewConfig.load()
        )
        == writer
    )
    assert released == [writer]


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="store-use admission uses POSIX shared locks")
async def test_dashboard_pin_refuses_deleted_database_still_open_elsewhere(tmp_path, member_stores):
    """Another holder's shared admission prevents repair and private publication."""
    from kiro_crew.member_memory_backup import acquire_store_use_lock, release_store_use_lock

    writer, _ = member_stores
    database = _writer_database(writer)
    fd = acquire_store_use_lock(database)
    try:
        database.unlink()
        journal = database.with_name(database.name + "-journal")
        journal.write_bytes(b"old journal")
        state = _make_state(tmp_path)
        key = "dashboard:busy-deleted-db"
        with pytest.raises(UnknownMemoryStore, match="still open in another process.*restart"):
            await chat_persistence.pin_private_agent_store(
                state, key, "writer", KiroCrewConfig.load()
            )
        assert not database.exists()
        assert journal.read_bytes() == b"old journal"
        assert not list(database.parent.glob("*.orphaned-*"))
        assert read_session_execution(key) is None
    finally:
        release_store_use_lock(fd)


@pytest.mark.asyncio
async def test_dashboard_pin_moves_leftover_side_files_aside_before_recreating(
    tmp_path, member_stores, monkeypatch
):
    """WAL/SHM files a deleted database left behind are renamed aside, never deleted."""
    from kiro_crew.vector_memory import read_member_database_identity

    writer, _ = member_stores
    database = _writer_database(writer)
    database.unlink()
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).write_bytes(b"stale " + suffix.encode())
    monkeypatch.setattr(
        chat_persistence, "_pin_private_agent_assignment", lambda *args, **kwargs: writer
    )
    state = _make_state(tmp_path)
    assert (
        await chat_persistence.pin_private_agent_store(
            state, "dashboard:side-files", "writer", KiroCrewConfig.load()
        )
        == writer
    )
    member_id = KiroCrewConfig.load().agents["writer"].member_id
    assert read_member_database_identity(database) == (member_id, writer)
    for suffix in ("-wal", "-shm"):
        moved = sorted(database.parent.glob(f"{database.name}{suffix}.orphaned-*"))
        assert [path.read_bytes() for path in moved] == [b"stale " + suffix.encode()]


@pytest.mark.asyncio
async def test_dashboard_pin_retries_after_failed_database_recreation(
    tmp_path, member_stores, monkeypatch
):
    """Failed creation removes only its fresh files, preserving archived bytes for retry."""
    from kiro_crew import vector_memory

    writer, _ = member_stores
    database = _writer_database(writer)
    database.unlink()
    suffixes = ("-wal", "-shm", "-journal")
    for suffix in suffixes:
        database.with_name(database.name + suffix).write_bytes(b"stale " + suffix.encode())

    def fail_creation(path, *, member_id, store_id):
        assert path.parent == database.parent
        assert path.name.startswith(f"{database.name}.repair-")
        assert not database.exists()
        for suffix in ("", *suffixes):
            with path.with_name(path.name + suffix).open("xb") as fresh:
                fresh.write(b"partial")
        raise OSError("database initialization failed")

    state = _make_state(tmp_path)
    key = "dashboard:retry-deleted-db"
    config = KiroCrewConfig.load()
    with monkeypatch.context() as patch:
        patch.setattr(vector_memory, "create_member_database", fail_creation)
        with pytest.raises(OSError, match="database initialization failed"):
            await chat_persistence.pin_private_agent_store(state, key, "writer", config)

    for suffix in ("", *suffixes):
        assert not database.with_name(database.name + suffix).exists()
    assert not list(database.parent.glob(f"{database.name}.repair-*"))
    assert read_session_execution(key) is None
    archived = {}
    for suffix in suffixes:
        moved = list(database.parent.glob(f"{database.name}{suffix}.orphaned-*"))
        assert len(moved) == 1
        assert moved[0].read_bytes() == b"stale " + suffix.encode()
        archived[moved[0]] = moved[0].read_bytes()

    assert await chat_persistence.pin_private_agent_store(state, key, "writer", config) == writer
    assert vector_memory.read_member_database_identity(database) == (
        config.agents["writer"].member_id,
        writer,
    )
    assert {path: path.read_bytes() for path in archived} == archived


@pytest.mark.asyncio
async def test_dashboard_pin_refuses_a_linked_side_file(tmp_path, member_stores, monkeypatch):
    """A side file that is a link keeps the store refused and is left in place."""
    writer, _ = member_stores
    database = _writer_database(writer)
    database.unlink()
    wal = database.with_name(database.name + "-wal")
    if wal.exists():
        wal.unlink()
    try:
        wal.symlink_to(tmp_path / "elsewhere-wal")
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    called = False

    def assignment(*args, **kwargs):
        nonlocal called
        called = True
        return writer

    monkeypatch.setattr(chat_persistence, "_pin_private_agent_assignment", assignment)
    state = _make_state(tmp_path)
    with pytest.raises(UnknownMemoryStore):
        await chat_persistence.pin_private_agent_store(
            state, "dashboard:linked-wal", "writer", KiroCrewConfig.load()
        )
    assert not called
    assert wal.is_symlink()
    assert not database.exists()


@pytest.mark.asyncio
async def test_new_chat_precheck_admits_a_repairable_deleted_database(member_stores):
    """The new-chat precheck lets the open heal a deleted memory.db instead of refusing."""
    from kiro_crew.dashboard.chat_handlers import _check_new_slot_assignment
    from kiro_crew.vector_memory import create_member_database

    writer, _ = member_stores
    database = _writer_database(writer)
    database.unlink()
    await _check_new_slot_assignment(KiroCrewConfig.load(), "writer", "", "", "persistent")
    assert not database.exists()

    create_member_database(database, member_id="someone-else", store_id=writer)
    with pytest.raises(UnknownMemoryStore, match="does not own its private memory store"):
        await _check_new_slot_assignment(KiroCrewConfig.load(), "writer", "", "", "persistent")


@pytest.mark.asyncio
async def test_dashboard_pin_refuses_a_database_of_another_identity(
    tmp_path, member_stores, monkeypatch
):
    """A database that exists but names someone else is refused with that reason."""
    from kiro_crew.vector_memory import create_member_database

    writer, _ = member_stores
    database = _writer_database(writer)
    database.unlink()
    create_member_database(database, member_id="someone-else", store_id=writer)
    called = False

    def assignment(*args, **kwargs):
        nonlocal called
        called = True
        return writer

    monkeypatch.setattr(chat_persistence, "_pin_private_agent_assignment", assignment)
    state = _make_state(tmp_path)
    with pytest.raises(UnknownMemoryStore) as refused:
        await chat_persistence.pin_private_agent_store(
            state, "dashboard:foreign-db", "writer", KiroCrewConfig.load()
        )
    message = str(refused.value)
    assert "Member database identity does not match its declaration" in message
    assert "changed during private memory assignment" not in message
    assert not called


@pytest.mark.asyncio
async def test_dashboard_pin_refuses_a_symlink_at_the_database_path(
    tmp_path, member_stores, monkeypatch
):
    """A link where the database should be is never replaced with a new database."""
    writer, _ = member_stores
    database = _writer_database(writer)
    database.unlink()
    try:
        database.symlink_to(tmp_path / "elsewhere.db")
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create symlinks")
    called = False

    def assignment(*args, **kwargs):
        nonlocal called
        called = True
        return writer

    monkeypatch.setattr(chat_persistence, "_pin_private_agent_assignment", assignment)
    state = _make_state(tmp_path)
    with pytest.raises(UnknownMemoryStore) as refused:
        await chat_persistence.pin_private_agent_store(
            state, "dashboard:linked-db", "writer", KiroCrewConfig.load()
        )
    assert "changed during private memory assignment" not in str(refused.value)
    assert not called
    assert database.is_symlink()
    assert not (tmp_path / "elsewhere.db").exists()


@pytest.mark.asyncio
async def test_dashboard_pin_holds_namespace_lock_through_publication(
    tmp_path, member_stores, monkeypatch
):
    """Deletion cannot retire a store between its final check and binding write."""
    from kiro_crew import execution_context, memory_stores, platform_compat

    writer, _ = member_stores
    state = _make_state(tmp_path)
    key = "dashboard:namespace-locked-pin"
    checked = threading.Event()
    release = threading.Event()
    real_bind = execution_context.bind_session_execution

    def pause_before_publication(session_key, execution, **kwargs):
        if session_key == key:
            checked.set()
            assert release.wait(timeout=5), "private binding write was not released"
        return real_bind(session_key, execution, **kwargs)

    monkeypatch.setattr(execution_context, "bind_session_execution", pause_before_publication)
    pin = asyncio.create_task(
        chat_persistence.pin_private_agent_store(state, key, "writer", KiroCrewConfig.load())
    )
    try:
        await async_wait_until(checked.is_set, describe=lambda: "the pin never reached its check")
        lock_path = (
            memory_stores.memory_stores_root().resolve()
            / memory_stores.MEMBER_BACKUPS_DIR_NAME
            / ".namespace.lock"
        )
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with pytest.raises(BlockingIOError):
                with platform_compat.file_lock(fd, exclusive=True, required=True, wait=False):
                    pass
        finally:
            os.close(fd)
    finally:
        release.set()
        await await_until(pin, what="the released pin finishing")
    assert pin.result() == writer
    assert await asyncio.to_thread(read_private_session_store, key) == writer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "expected"),
    [("writer", "private"), ("default", "none"), ("legacy", "none")],
)
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("validate_only", [False, True])
async def test_dashboard_pin_preserves_private_and_non_private_controls(
    tmp_path, member_stores, agent, expected, memory_mode, validate_only
):
    writer, _ = member_stores
    cfg = KiroCrewConfig.load()
    if agent == "legacy":
        cfg.agents[agent] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
    state = _make_state(tmp_path)
    key = f"dashboard:pin-control-{agent}"
    assigned = await chat_persistence.pin_private_agent_store(
        state, key, agent, cfg, memory_mode=memory_mode, validate_only=validate_only
    )
    assert assigned == (writer if expected == "private" else "")
    published = expected == "private" and not validate_only
    assert await asyncio.to_thread(read_private_session_store, key) == (
        writer if published else None
    )
    execution = await asyncio.to_thread(read_session_execution, key)
    if published:
        assert execution is not None
        assert execution.memory_mode == memory_mode
    else:
        assert execution is None
