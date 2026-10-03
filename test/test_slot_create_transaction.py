"""The chat-slot create transaction: journal, rollback order, commit-only publish.

``slot_create_transaction`` is the one place a refused or failed
``POST /api/chat/slots`` takes back what it already changed. These tests drive
the transaction at every failure point directly, and the binding restore its
assign step relies on (``kiro_crew.session_agent_selection``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from kiro_crew.dashboard.slot_create_transaction import SlotCreateTransaction
from kiro_crew.session_agent_selection import (
    note_published,
    restore_session_binding,
    snapshot_session_binding,
)


def _recorder(log: list[str], entry: str):
    def action() -> None:
        log.append(entry)

    return action


@pytest.mark.asyncio
async def test_commit_keeps_every_step_and_publishes_in_order() -> None:
    log: list[str] = []
    async with SlotCreateTransaction("t") as txn:
        txn.completed("reserve", _recorder(log, "undo reserve"))
        txn.completed("assign", _recorder(log, "undo assign"))
        txn.on_publish("count", _recorder(log, "count"))
        txn.on_publish("unhide", _recorder(log, "unhide"))
        assert log == [], "nothing publishes before commit"
        await txn.commit()
    assert log == ["count", "unhide"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["after-reserve", "after-assign", "after-persist"])
async def test_an_uncommitted_exit_undoes_the_completed_steps_newest_first(failure) -> None:
    """A refusal returns without committing: each failure point undoes exactly
    the steps completed by then, in reverse order, and publishes nothing."""
    log: list[str] = []
    async with SlotCreateTransaction("t") as txn:
        txn.on_publish("count", _recorder(log, "count"))
        txn.completed("reserve", _recorder(log, "undo reserve"))
        if failure != "after-reserve":
            txn.completed("assign", _recorder(log, "undo assign"))
        if failure == "after-persist":
            txn.completed("persist", _recorder(log, "undo persist"))
    expected = {
        "after-reserve": ["undo reserve"],
        "after-assign": ["undo assign", "undo reserve"],
        "after-persist": ["undo persist", "undo assign", "undo reserve"],
    }[failure]
    assert log == expected


@pytest.mark.asyncio
async def test_an_exception_rolls_back_and_still_propagates() -> None:
    log: list[str] = []
    with pytest.raises(RuntimeError, match="boom"):
        async with SlotCreateTransaction("t") as txn:
            txn.completed("reserve", _recorder(log, "undo reserve"))
            txn.on_publish("count", _recorder(log, "count"))
            raise RuntimeError("boom")
    assert log == ["undo reserve"]


@pytest.mark.asyncio
async def test_kept_steps_are_decided_before_the_first_undo_runs() -> None:
    """An undo that suspends cannot change whether an older step is kept: every
    step's fate is read once, synchronously, when the rollback starts."""
    log: list[str] = []
    state = {"turn_started": False}

    async def undo_assign() -> None:
        log.append("undo assign")
        await asyncio.sleep(0)
        state["turn_started"] = True

    async with SlotCreateTransaction("t") as txn:
        txn.completed("reserve", _recorder(log, "undo reserve"), kept=lambda: state["turn_started"])
        txn.completed("assign", undo_assign, kept=lambda: state["turn_started"])
    assert log == ["undo assign", "undo reserve"]


@pytest.mark.asyncio
async def test_a_kept_step_survives_the_rollback() -> None:
    log: list[str] = []
    async with SlotCreateTransaction("t") as txn:
        txn.completed("reserve", _recorder(log, "undo reserve"), kept=lambda: True)
        txn.completed("assign", _recorder(log, "undo assign"))
    assert log == ["undo assign"]


@pytest.mark.asyncio
async def test_a_failing_undo_stops_the_rollback_and_keeps_the_older_steps(caplog) -> None:
    """A binding whose undo failed keeps the slot it belongs to: the steps it
    was built on are kept rather than undone underneath it."""
    log: list[str] = []

    def failing() -> None:
        raise OSError("disk")

    with caplog.at_level(logging.WARNING):
        async with SlotCreateTransaction("t") as txn:
            txn.completed("reserve", _recorder(log, "undo reserve"))
            txn.completed("assign", failing)
            txn.completed("persist", _recorder(log, "undo persist"))
    assert log == ["undo persist"]
    assert "could not undo step 'assign'; keeping the steps before it" in caplog.text


@pytest.mark.asyncio
async def test_a_step_that_will_be_undone_is_fenced_before_any_undo_runs() -> None:
    """The fence runs in the same synchronous step that decides the fates, so a
    newer undo that suspends never runs while the older effect is reachable."""
    log: list[str] = []

    def fence() -> Any:
        log.append("fence reserve")
        return _recorder(log, "reinstate reserve")

    async def undo_assign() -> None:
        log.append("undo assign")
        await asyncio.sleep(0)

    async with SlotCreateTransaction("t") as txn:
        txn.completed("reserve", _recorder(log, "undo reserve"), fence=fence)
        txn.completed("assign", undo_assign)
    assert log == ["fence reserve", "undo assign", "undo reserve"]


@pytest.mark.asyncio
async def test_a_kept_step_is_not_fenced() -> None:
    log: list[str] = []
    async with SlotCreateTransaction("t") as txn:
        txn.completed(
            "reserve",
            _recorder(log, "undo reserve"),
            kept=lambda: True,
            fence=lambda: log.append("fence reserve") or _recorder(log, "reinstate"),
        )
    assert log == []


@pytest.mark.asyncio
async def test_a_failing_undo_reinstates_the_fenced_older_steps(caplog) -> None:
    log: list[str] = []

    def failing() -> None:
        raise OSError("disk")

    with caplog.at_level(logging.WARNING):
        async with SlotCreateTransaction("t") as txn:
            txn.completed(
                "reserve",
                _recorder(log, "undo reserve"),
                fence=lambda: log.append("fence reserve") or _recorder(log, "reinstate reserve"),
            )
            txn.completed("assign", failing)
    assert log == ["fence reserve", "reinstate reserve"]


@pytest.mark.asyncio
async def test_a_failing_publish_step_does_not_stop_the_others(caplog) -> None:
    log: list[str] = []

    async def failing() -> None:
        raise OSError("folders.json")

    with caplog.at_level(logging.WARNING):
        async with SlotCreateTransaction("t") as txn:
            txn.completed("reserve", _recorder(log, "undo reserve"))
            txn.on_publish("unhide", failing)
            txn.on_publish("count", _recorder(log, "count"))
            await txn.commit()
    assert log == ["count"]
    assert "publish step 'unhide' failed" in caplog.text


@pytest.mark.asyncio
async def test_held_locks_are_released_only_after_the_rollback() -> None:
    """A request queued on a held lock must find the create fully undone."""
    lock = asyncio.Lock()
    seen: list[bool] = []

    def undo() -> None:
        seen.append(lock.locked())

    async with SlotCreateTransaction("t") as txn:
        await txn.hold(lock)
        txn.completed("reserve", undo)
    assert seen == [True]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_undo_takes_one_step_back_now_and_keep_hands_another_over() -> None:
    """The rebound refusal: the assignment is undone, the reservation kept."""
    log: list[str] = []
    async with SlotCreateTransaction("t") as txn:
        txn.completed("reserve", _recorder(log, "undo reserve"))
        txn.completed("assign", _recorder(log, "undo assign"))
        await txn.undo("assign")
        txn.keep("reserve")
        assert log == ["undo assign"]
    assert log == ["undo assign"]


@pytest.mark.asyncio
async def test_no_step_can_be_recorded_once_settled() -> None:
    async with SlotCreateTransaction("t") as txn:
        await txn.commit()
        with pytest.raises(RuntimeError):
            txn.completed("late", lambda: None)
        await txn.rollback()  # a settled transaction ignores it


def _metadata(key: str) -> dict[str, Any]:
    from kiro_crew.history import ConversationLog

    return ConversationLog().get_metadata(key)


def _publish(key: str, record: dict[str, Any], store: str) -> None:
    from kiro_crew.history import ConversationLog

    ConversationLog().update_metadata(
        key, {"execution_context": record, "memory_store": store, "memory_mode": "persistent"}
    )


_PUBLISHED = {"marker": "published-by-this-create"}


def test_a_legacy_record_keeps_its_own_binding_fields() -> None:
    """A metadata-only record that predates ``execution_context`` carries its
    binding in ``memory_store``/``memory_mode``. The restore puts back exactly
    those raw values, rather than removing them with the published record."""
    from kiro_crew.history import ConversationLog

    key = "dashboard:legacy"
    ConversationLog().update_metadata(
        key, {"title": "Kept", "memory_store": "default", "memory_mode": "persistent"}
    )
    before = _metadata(key)
    snapshot = snapshot_session_binding(key)
    assert snapshot.had_log and "execution_context" not in snapshot.fields
    _publish(key, _PUBLISHED, "worker-store")
    note_published(snapshot, _PUBLISHED)
    restore_session_binding(snapshot)
    assert _metadata(key) == before


def test_the_restore_leaves_a_binding_someone_else_published_alone() -> None:
    key = "dashboard:foreign"
    from kiro_crew.history import ConversationLog

    ConversationLog().update_metadata(key, {"title": "Kept"})
    snapshot = snapshot_session_binding(key)
    note_published(snapshot, _PUBLISHED)
    foreign = {"marker": "written-by-another-request"}
    _publish(key, foreign, "other-store")
    restore_session_binding(snapshot)
    assert _metadata(key)["execution_context"] == foreign
    assert _metadata(key)["memory_store"] == "other-store"


def test_a_publishing_step_that_raised_after_writing_is_still_undone(monkeypatch) -> None:
    """``in_flight`` covers a step that wrote and then failed before the create
    could note what it wrote."""
    import kiro_crew.session_agent_selection as selection_mod
    from kiro_crew.history import ConversationLog

    key = "dashboard:inflight"
    ConversationLog().update_metadata(key, {"title": "Kept", "memory_mode": "persistent"})
    before = _metadata(key)
    snapshot = snapshot_session_binding(key)
    _publish(key, _PUBLISHED, "worker-store")
    snapshot.in_flight = True
    monkeypatch.setattr(selection_mod, "current_execution_record", lambda _key: _PUBLISHED)
    restore_session_binding(snapshot)
    assert _metadata(key) == before


def test_publishing_stays_in_flight_when_the_write_raises() -> None:
    """A publishing write that raised may have committed, so the mark stays set
    for the restore; one that completed clears it."""
    snapshot = snapshot_session_binding("dashboard:publishing")
    with snapshot.publishing():
        assert snapshot.in_flight
    assert not snapshot.in_flight
    with pytest.raises(OSError):
        with snapshot.publishing():
            raise OSError("write failed after committing")
    assert snapshot.in_flight


def test_a_transcript_the_assignment_created_is_removed() -> None:
    from kiro_crew.history import ConversationLog

    key = "dashboard:stub"
    snapshot = snapshot_session_binding(key)
    assert not snapshot.had_log
    _publish(key, _PUBLISHED, "worker-store")
    note_published(snapshot, _PUBLISHED)
    restore_session_binding(snapshot)
    assert not ConversationLog().has_log(key)


def test_a_message_appended_during_the_stub_cleanup_is_not_deleted(monkeypatch) -> None:
    """The stub's emptiness check and its delete share one transcript hold, so
    an append that arrives after the check waits for the delete rather than
    landing in a transcript the delete then removes."""
    import threading

    from kiro_crew.history import ConversationLog

    key = "dashboard:raced-stub"
    snapshot = snapshot_session_binding(key)
    _publish(key, _PUBLISHED, "worker-store")
    note_published(snapshot, _PUBLISHED)
    original = ConversationLog.has_messages
    appender: list[threading.Thread] = []

    def has_messages_then_race(self: Any, session_key: str) -> bool:
        answer = original(self, session_key)
        if session_key == key and not appender:
            thread = threading.Thread(
                target=lambda: ConversationLog().append(key, "user", "hello"), daemon=True
            )
            appender.append(thread)
            thread.start()
            # Long enough for an append that does not wait for the delete to land.
            thread.join(timeout=0.5)
        return answer

    monkeypatch.setattr(ConversationLog, "has_messages", has_messages_then_race)
    restore_session_binding(snapshot)
    appender[0].join(timeout=10)
    monkeypatch.setattr(ConversationLog, "has_messages", original)
    assert ConversationLog().has_messages(key), "the raced message was deleted with the stub"


def test_the_restore_rewrites_only_the_metadata_line() -> None:
    from kiro_crew.history import ConversationLog

    key = "dashboard:with-rows"
    log = ConversationLog()
    log.update_metadata(key, {"title": "Kept", "memory_mode": "persistent"})
    log.append(key, "user", "hello")
    snapshot = snapshot_session_binding(key)
    _publish(key, _PUBLISHED, "worker-store")
    note_published(snapshot, _PUBLISHED)
    restore_session_binding(snapshot)
    rows = log._path(key).read_text(encoding="utf-8").splitlines()
    metadata = json.loads(rows[0])
    assert "execution_context" not in metadata and "memory_store" not in metadata
    assert metadata["memory_mode"] == "persistent"
    assert log.has_messages(key)


def test_the_restore_keeps_the_transcript_mtime() -> None:
    """Restoring a binding is housekeeping, not activity: session lists order by
    mtime, so the rewrite must not float the session to the top."""
    import os

    from kiro_crew.history import ConversationLog

    key = "dashboard:old-thread"
    log = ConversationLog()
    log.update_metadata(key, {"title": "Old", "memory_mode": "persistent"})
    path = log._path(key)
    os.utime(path, (1_000_000_000, 1_000_000_000))
    snapshot = snapshot_session_binding(key)
    _publish(key, _PUBLISHED, "worker-store")
    os.utime(path, (1_000_000_000, 1_000_000_000))
    note_published(snapshot, _PUBLISHED)
    restore_session_binding(snapshot)
    assert path.stat().st_mtime == 1_000_000_000
    assert "execution_context" not in log.get_metadata(key)


def test_a_corrupt_metadata_line_is_snapshotted_not_refused() -> None:
    """A first line no retry will read must not by itself make the session
    impossible to re-open; the restore leaves the line to whatever healed it."""
    from kiro_crew.history import ConversationLog

    key = "dashboard:corrupt"
    log = ConversationLog()
    log.update_metadata(key, {"title": "Kept"})
    log.append(key, "user", "hello")
    path = log._path(key)
    rows = path.read_text(encoding="utf-8").splitlines(keepends=True)
    rows[0] = "{not json\n"
    path.write_text("".join(rows), encoding="utf-8")
    log._invalidate_cache(key)
    snapshot = snapshot_session_binding(key)
    assert snapshot.durable is False
    _publish(key, _PUBLISHED, "worker-store")
    healed = log.get_metadata(key)
    note_published(snapshot, _PUBLISHED)
    restore_session_binding(snapshot)
    assert log.get_metadata(key) == healed


def test_a_raised_write_on_a_still_corrupt_line_published_nothing() -> None:
    """A publishing write that raised on a corrupt line never wrote it (a write
    replaces the whole line), so the restore records no publication instead of
    failing on the unreadable reread."""
    from kiro_crew.history import ConversationLog

    key = "dashboard:corrupt-raised"
    log = ConversationLog()
    log.update_metadata(key, {"title": "Kept"})
    log.append(key, "user", "hello")
    path = log._path(key)
    rows = path.read_text(encoding="utf-8").splitlines(keepends=True)
    rows[0] = "{not json\n"
    path.write_text("".join(rows), encoding="utf-8")
    log._invalidate_cache(key)
    snapshot = snapshot_session_binding(key)
    snapshot.in_flight = True
    restore_session_binding(snapshot)
    assert snapshot.published == []
    assert path.read_text(encoding="utf-8").splitlines()[0] == "{not json"


def test_a_line_corrupted_after_a_durable_snapshot_still_refuses_the_undo() -> None:
    """Only a snapshot of a corrupt line may read a still-corrupt line as "the
    write published nothing". A line that was readable at the snapshot and is
    corrupt now cannot say whether the raised write landed, so the undo fails
    and the rollback keeps the steps it was built on."""
    from kiro_crew.history import ConversationLog

    key = "dashboard:corrupt-after-durable"
    log = ConversationLog()
    log.update_metadata(key, {"title": "Kept"})
    log.append(key, "user", "hello")
    snapshot = snapshot_session_binding(key)
    assert snapshot.durable is True
    path = log._path(key)
    rows = path.read_text(encoding="utf-8").splitlines(keepends=True)
    rows[0] = "{not json\n"
    path.write_text("".join(rows), encoding="utf-8")
    log._invalidate_cache(key)
    snapshot.in_flight = True
    with pytest.raises(Exception):
        restore_session_binding(snapshot)


def test_a_line_that_cannot_be_read_right_now_is_refused(monkeypatch) -> None:
    from kiro_crew.history import ConversationLog
    from kiro_crew.history_projection import METADATA_LINE_TRANSIENT

    key = "dashboard:busy"
    ConversationLog().update_metadata(key, {"title": "Kept"})
    monkeypatch.setattr(
        ConversationLog, "_read_metadata_state", lambda self, k: ({}, METADATA_LINE_TRANSIENT)
    )
    with pytest.raises(OSError, match="cannot be read right now"):
        snapshot_session_binding(key)


def test_a_read_that_fails_then_succeeds_is_refused_not_snapshotted_empty(monkeypatch) -> None:
    """The snapshot takes the line's state and its fields from ONE read. A
    first read that fails and a second that succeeds must not combine into an
    empty snapshot of a line that holds a binding: a later restore would write
    that empty set back and delete the binding."""
    from kiro_crew.history import ConversationLog
    from kiro_crew.history_projection import METADATA_LINE_TRANSIENT

    key = "dashboard:flaky-read"
    log = ConversationLog()
    log.update_metadata(key, {"title": "Kept", "memory_store": "worker-store"})
    real = ConversationLog._read_metadata_state
    calls: list[str] = []

    def flaky(self: ConversationLog, k: str) -> tuple[dict, str]:
        calls.append(k)
        if len(calls) == 1:
            return {}, METADATA_LINE_TRANSIENT
        return real(self, k)

    monkeypatch.setattr(ConversationLog, "_read_metadata_state", flaky)
    with pytest.raises(OSError, match="cannot be read right now"):
        snapshot_session_binding(key)


@pytest.mark.asyncio
async def test_a_cancelled_undo_reinstates_the_fenced_steps_and_propagates() -> None:
    """A rollback cancelled inside an undo keeps that step and the older ones,
    as a failed undo does, so no fence (the newborn's mint refusal) outlives
    it, and the cancellation still reaches the caller."""
    log: list[str] = []

    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        async with SlotCreateTransaction("t") as txn:
            txn.completed(
                "reserve",
                _recorder(log, "undo reserve"),
                fence=lambda: log.append("fence reserve") or _recorder(log, "reinstate reserve"),
            )
            txn.completed("assign", cancelled)
    assert log == ["fence reserve", "reinstate reserve"]


@pytest.mark.asyncio
async def test_a_cancel_during_the_assign_undo_finishes_the_rollback() -> None:
    """The caller is cancelled while the assign undo is in flight (its restore
    runs to the end in a worker thread either way). The rollback must not read
    that as a failed undo and reinstate the fenced newborn: it finishes every
    undo, keeps no fence, and only then raises the cancellation."""
    log: list[str] = []
    undo_started = asyncio.Event()
    release_undo = asyncio.Event()

    async def undo_assign() -> None:
        undo_started.set()
        await release_undo.wait()
        log.append("undo assign")

    async def create() -> None:
        async with SlotCreateTransaction("t") as txn:
            txn.completed(
                "reserve",
                _recorder(log, "undo reserve"),
                fence=lambda: log.append("fence reserve") or _recorder(log, "reinstate reserve"),
            )
            txn.completed("assign", undo_assign)

    task = asyncio.ensure_future(create())
    await undo_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # a second cancel, as a shutdown escalating after its timeout
    await asyncio.sleep(0)
    release_undo.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert log == ["fence reserve", "undo assign", "undo reserve"]


def test_a_restored_title_with_a_line_separator_stays_one_line() -> None:
    """The restore writes the metadata line ASCII-escaped, as history does: a
    literal U+2028 in a title would split the line for a ``splitlines``
    reader, which would then replace the header as corrupt."""
    from kiro_crew.history import ConversationLog

    key = "dashboard:separator"
    title = "before\u2028after"
    ConversationLog().update_metadata(key, {"title": title, "memory_mode": "persistent"})
    snapshot = snapshot_session_binding(key)
    _publish(key, _PUBLISHED, "worker-store")
    note_published(snapshot, _PUBLISHED)
    restore_session_binding(snapshot)
    log = ConversationLog()
    raw = log._path(key).read_text(encoding="utf-8")
    assert "\u2028" not in raw
    assert len(raw.splitlines()) == 1
    log._invalidate_cache(key)
    assert _metadata(key)["title"] == title
    assert "execution_context" not in _metadata(key)


def test_a_refused_stub_delete_fails_the_undo(monkeypatch) -> None:
    """``delete_session`` answers False when it could not remove the stub (an
    unreadable search index). The restore raises, so the rollback keeps the
    reservation instead of reporting the create undone with its stub left."""
    from kiro_crew.history import ConversationLog

    key = "dashboard:undeletable-stub"
    snapshot = snapshot_session_binding(key)
    _publish(key, _PUBLISHED, "worker-store")
    note_published(snapshot, _PUBLISHED)
    monkeypatch.setattr(ConversationLog, "delete_session", lambda self, k, **_kw: False)
    with pytest.raises(OSError, match="could not remove the transcript stub"):
        restore_session_binding(snapshot)
    assert ConversationLog().has_log(key)


def test_a_stub_the_snapshot_no_longer_owns_is_left() -> None:
    """Once the key may belong to a replacement (``owns_stub`` cleared), a
    metadata-only transcript there is the replacement's as much as these
    writes', so the restore puts the binding back and leaves the file."""
    from kiro_crew.history import ConversationLog

    key = "dashboard:handed-over-stub"
    snapshot = snapshot_session_binding(key)
    assert not snapshot.had_log
    _publish(key, _PUBLISHED, "worker-store")
    ConversationLog().update_metadata(key, {"title": "Replacement's"})
    note_published(snapshot, _PUBLISHED)
    snapshot.owns_stub = False
    restore_session_binding(snapshot)
    assert ConversationLog().has_log(key)
    metadata = _metadata(key)
    assert metadata["title"] == "Replacement's"
    assert "execution_context" not in metadata


def test_a_selection_change_restores_through_the_same_path() -> None:
    """A caller holding only one write's ``(prior, published)`` change restores
    through ``restore_session_binding`` too: the prior record's fields come
    back under the same compare-and-set, the transcript is never deleted, and
    no change restores nothing."""
    from kiro_crew.history import ConversationLog
    from kiro_crew.session_agent_selection import snapshot_from_selection_change

    assert snapshot_from_selection_change("dashboard:none", None) is None
    restore_session_binding(None)
    key = "dashboard:switched"
    prior = {"marker": "prior", "store": {"store_id": "default"}, "memory_mode": "persistent"}
    ConversationLog().update_metadata(key, {"title": "Kept"})
    _publish(key, prior, "")
    _publish(key, _PUBLISHED, "worker-store")
    snapshot = snapshot_from_selection_change(key, (prior, _PUBLISHED))
    assert snapshot is not None and snapshot.had_log
    restore_session_binding(snapshot)
    metadata = _metadata(key)
    assert metadata["execution_context"] == prior
    assert metadata["memory_store"] == "" and metadata["memory_mode"] == "persistent"
    assert metadata["title"] == "Kept"


def test_the_binding_has_one_restore() -> None:
    """``restore_session_binding`` is the only binding restore, and the metadata
    line it rewrites is written through the history module's public method,
    not through ``ConversationLog`` internals."""
    import inspect

    import kiro_crew.session_agent_selection as selection_mod

    assert not hasattr(selection_mod, "restore_agent_selection")
    source = inspect.getsource(selection_mod.restore_session_binding)
    assert "restore_binding_fields" in source
    for private in ("._locked", "._path", "._invalidate_cache", "_restore_mtime"):
        assert private not in source, private
