"""Tests for DashboardState.mutate_folders — the shared serialized folder writer.

The primitive replaced seven bare ``save_folders()`` calls. Two defects motivated
it, and each has a test here:

* an ``fsync`` on the event loop stalls chat and heartbeat processing;
* an unserialized read-modify-write lets two writers each miss the other's
  change, so whichever write lands second silently drops it.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state

from kiro_crew.config.paths import config_dir


@pytest.fixture
def dashboard_state(tmp_path: Any) -> Any:
    return _make_state(tmp_path)


def _on_disk(state: Any) -> list[dict[str, Any]]:
    path = config_dir() / state._FOLDERS_FILE
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _append(fid: str) -> Any:
    def _mutate(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        folders.append({"id": fid, "name": fid, "order": len(folders)})
        return True, fid

    return _mutate


class TestMutateFolders:
    def test_persists_the_mutation(self, dashboard_state: Any) -> None:
        value = asyncio.run(dashboard_state.mutate_folders(_append("a")))
        assert value == "a"
        assert [f["id"] for f in _on_disk(dashboard_state)] == ["a"]

    def test_unchanged_writes_nothing(self, dashboard_state: Any) -> None:
        """A no-op mutation must not cost a write.

        The unhide path calls this on every session move; writing each time would
        turn a read into an fsync.
        """
        writes: list[Any] = []
        real = dashboard_state._atomic_write_json
        dashboard_state._atomic_write_json = lambda p, d: (  # type: ignore[method-assign]
            writes.append(1),
            real(p, d),
        )

        def _noop(folders: list[dict[str, Any]]) -> tuple[bool, str]:
            return False, "untouched"

        assert asyncio.run(dashboard_state.mutate_folders(_noop)) == "untouched"
        assert not writes

    def test_the_write_runs_off_the_event_loop(self, dashboard_state: Any) -> None:
        """The tempfile + fsync + replace must not sit on the loop."""
        loop_thread = threading.get_ident()
        write_threads: list[int] = []
        real = dashboard_state._atomic_write_json

        def recording(path: Any, data: Any) -> None:
            write_threads.append(threading.get_ident())
            real(path, data)

        dashboard_state._atomic_write_json = recording  # type: ignore[method-assign]

        async def _run() -> None:
            assert threading.get_ident() == loop_thread
            await dashboard_state.mutate_folders(_append("a"))

        asyncio.run(_run())
        assert write_threads and all(t != loop_thread for t in write_threads)

    def test_a_second_transaction_cannot_start_mid_write(self, dashboard_state: Any) -> None:
        """The defect this primitive exists for: two writers must not interleave.

        Without the lock, the second caller reads a folder list that lacks the
        first's entry and its write drops it. The check is deterministic rather
        than a race: the first write is genuinely PARKED inside its worker
        thread, and the loop is then pumped, so the second transaction has every
        opportunity to start. If it did, the lock is not holding.

        (A previous version of this test raced two ``gather``ed calls and passed
        with the lock removed, because whether the writes landed out of order
        depended on thread-pool scheduling. Parking the first write is what makes
        the observation real.)
        """
        started: list[str] = []
        first_in_write = threading.Event()
        release = threading.Event()
        real = dashboard_state._atomic_write_json
        calls = {"n": 0}

        def write(path: Any, data: Any) -> None:
            calls["n"] += 1
            if calls["n"] == 1:  # park only the first writer
                first_in_write.set()
                release.wait(timeout=5)
            real(path, data)

        dashboard_state._atomic_write_json = write  # type: ignore[method-assign]

        def mk(fid: str) -> Any:
            def _mutate(folders: list[dict[str, Any]]) -> tuple[bool, str]:
                started.append(fid)
                folders.append({"id": fid, "name": fid, "order": len(folders)})
                return True, fid

            return _mutate

        async def _run() -> list[str]:
            t1 = asyncio.create_task(dashboard_state.mutate_folders(mk("a")))
            t2 = asyncio.create_task(dashboard_state.mutate_folders(mk("b")))
            await asyncio.to_thread(first_in_write.wait, 5)
            for _ in range(50):  # pump: let t2 progress as far as it can
                await asyncio.sleep(0)
            observed = list(started)
            release.set()
            await asyncio.gather(t1, t2)
            return observed

        observed = asyncio.run(_run())
        assert observed == ["a"], (
            "a second folder transaction started while the first was still "
            f"persisting (saw {observed}); the store lock is not held across "
            "modify-and-persist, so concurrent writers can drop each other."
        )
        # And both survive: the second transaction reads the first's mutation.
        assert sorted(f["id"] for f in _on_disk(dashboard_state)) == ["a", "b"]
        assert sorted(f["id"] for f in dashboard_state._folders) == ["a", "b"]

    def test_a_mutation_seen_by_the_next_transaction(self, dashboard_state: Any) -> None:
        """Each transaction reads the live list, so ``order`` keeps counting up."""

        async def _run() -> None:
            await dashboard_state.mutate_folders(_append("a"))
            await dashboard_state.mutate_folders(_append("b"))

        asyncio.run(_run())
        assert [(f["id"], f["order"]) for f in _on_disk(dashboard_state)] == [("a", 0), ("b", 1)]

    def test_an_update_that_does_not_land_is_rolled_back(self, dashboard_state: Any) -> None:
        """A silently-failed UPDATE must be caught, not just a failed create.

        Renames, reparents, collapses and icon changes leave the folder ids
        untouched. If the persistence check only compared ids it would accept a
        write that landed the OLD record, the caller would be told the edit
        succeeded, and the stale value would reappear on the next restart. The
        in-memory list must not keep an edit that disk does not have.
        """
        asyncio.run(
            dashboard_state.mutate_folders(
                lambda folders: (True, folders.append({"id": "a", "name": "Before", "order": 0}))
            )
        )
        stale = _on_disk(dashboard_state)
        assert stale[0]["name"] == "Before"

        path = config_dir() / dashboard_state._FOLDERS_FILE

        def write_the_old_record(p: Any, data: Any) -> None:
            # Same ids, previous values: an id-only check cannot see this.
            path.write_text(json.dumps(stale), encoding="utf-8")

        dashboard_state._atomic_write_json = write_the_old_record  # type: ignore[method-assign]

        def _rename(folders: list[dict[str, Any]]) -> tuple[bool, None]:
            folders[0]["name"] = "After"
            return True, None

        with pytest.raises(OSError):
            asyncio.run(dashboard_state.mutate_folders(_rename))

        assert dashboard_state._folders[0]["name"] == "Before", (
            "the in-memory folder kept a rename that never reached disk"
        )
        assert _on_disk(dashboard_state)[0]["name"] == "Before"

    def test_a_failed_write_rolls_back_the_in_memory_list(self, dashboard_state: Any) -> None:
        """Memory must not diverge from disk when the persist raises.

        Without the rollback the caller would hold a folder that no restart can
        recover, and would hand its id to a session.
        """

        def boom(path: Any, data: Any) -> None:
            raise OSError("disk full")

        dashboard_state._atomic_write_json = boom  # type: ignore[method-assign]

        with pytest.raises(OSError):
            asyncio.run(dashboard_state.mutate_folders(_append("a")))
        assert dashboard_state._folders == []
        assert _on_disk(dashboard_state) == []

    def test_the_thread_never_serializes_a_live_list(self, dashboard_state: Any) -> None:
        """The worker gets a snapshot, not ``state._folders``.

        Handing the live list across the boundary would let the loop mutate it
        mid-serialization; the snapshot is taken under the lock instead.
        """
        seen: list[Any] = []
        real = dashboard_state._atomic_write_json

        def capturing(path: Any, data: Any) -> None:
            seen.append(data)
            real(path, data)

        dashboard_state._atomic_write_json = capturing  # type: ignore[method-assign]
        asyncio.run(dashboard_state.mutate_folders(_append("a")))

        assert seen and seen[0] is not dashboard_state._folders
        assert seen[0][0] is not dashboard_state._folders[0]


class TestPlacementRacesTheStore:
    """A placement decided outside the store lock must not outlive the folder."""

    @pytest.mark.asyncio
    async def test_assignment_is_rejected_when_the_folder_is_deleted_mid_request(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The unlocked existence check is not the decision point.

        api_chat_slot_folder validates against state._folders, then awaits. A
        delete committing in that window used to leave the slot pointing at a
        folder that no longer exists — persisted, with a 200 response.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.append("user", "hello")
        slot.drain()
        state._folders = [{"id": "f1", "name": "Test", "order": 0, "collapsed": False}]

        real = state.mutate_folders

        async def _delete_then_run(mutate: Any) -> Any:
            # Stand in for a concurrent DELETE committing between the handler's
            # unlocked check and its own transaction.
            state.mutate_folders = real
            state._folders[:] = []
            return await real(mutate)

        state.mutate_folders = _delete_then_run

        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            resp = await client.patch(
                "/api/chat/slots/myslot/folder", json={"folder_id": "f1"}
            )
            assert resp.status == 400
        assert slot.folder_id == ""

    @pytest.mark.asyncio
    async def test_delete_rollback_leaves_a_concurrently_moved_slot_alone(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """The user's move is newer intent than the rollback's snapshot.

        Restoring unconditionally both discards that move and files the slot back
        into the very folder the failed request was deleting.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.append("user", "hello")
        slot.drain()
        state._folders = [
            {"id": "doomed", "name": "Doomed", "order": 0, "collapsed": False},
            {"id": "elsewhere", "name": "Elsewhere", "order": 1, "collapsed": False},
        ]
        slot.folder_id = "doomed"

        real = state.mutate_folders

        async def _move_then_fail(mutate: Any) -> Any:
            # The slot has been unfiled by now; the user drags it somewhere else
            # while the folder transaction is in flight, and the transaction then
            # fails, so the rollback runs against a slot that has moved.
            slot.folder_id = "elsewhere"
            raise OSError("folder store did not persist as intended")

        state.mutate_folders = _move_then_fail

        app = _make_folder_app(state)
        async with TestClient(TestServer(app)) as client:
            await client.delete("/api/chat/folders/doomed")
        assert slot.folder_id == "elsewhere"
        del real


class TestGuardedMetadataMerge:
    """update_metadata_if re-decides under the lock, not before taking it."""

    def test_merge_is_skipped_when_the_guard_no_longer_holds(
        self, tmp_path: Any
    ) -> None:
        from kiro_crew.dashboard.channel_slots import needs_default_filing
        from kiro_crew.history import ConversationLog

        log = ConversationLog(tmp_path)
        log.update_metadata("k", {"title": "t"})
        # The user's move lands first: the record now carries a placement.
        log.update_metadata("k", {"folder_id": "user-picked"})

        applied = log.update_metadata_if(
            "k",
            {"folder_id": "channel-default", "channel_folder_filed": True},
            needs_default_filing,
        )
        assert applied is False
        assert log.get_metadata("k")["folder_id"] == "user-picked"
        assert "channel_folder_filed" not in log.get_metadata("k")

    def test_merge_applies_when_the_record_is_still_unplaced(
        self, tmp_path: Any
    ) -> None:
        from kiro_crew.dashboard.channel_slots import needs_default_filing
        from kiro_crew.history import ConversationLog

        log = ConversationLog(tmp_path)
        log.update_metadata("k", {"title": "t"})

        applied = log.update_metadata_if(
            "k",
            {"folder_id": "channel-default", "channel_folder_filed": True},
            needs_default_filing,
        )
        assert applied is True
        assert log.get_metadata("k")["folder_id"] == "channel-default"

    def test_the_guard_sees_a_write_that_landed_first(self, tmp_path: Any) -> None:
        """The guard must read the record, not a snapshot from before the lock.

        A write issued by another holder while this call waited on the lock is
        exactly the case the guard exists for, so it has to be visible to it.
        """
        from kiro_crew.history import ConversationLog

        log = ConversationLog(tmp_path)
        log.update_metadata("k", {"title": "t"})
        log.update_metadata("k", {"folder_id": "landed-first"})
        seen: list[str] = []

        def _guard(meta: dict[str, Any]) -> bool:
            seen.append(meta.get("folder_id", ""))
            return False

        assert log.update_metadata_if("k", {"folder_id": "ours"}, _guard) is False
        assert seen == ["landed-first"]


class TestGuardFailsClosed:
    """An unreadable record must not read as a blank one."""

    def test_unreadable_metadata_blocks_the_merge(self, tmp_path: Any) -> None:
        """A failed read is not evidence that the record is empty.

        `_read_metadata` collapses both cases to `{}`, and `{}` satisfies
        `needs_default_filing` — so consulting it let a transient read failure
        (on Windows, an AV scanner holding a freshly written file) authorise a
        write over a placement the user had made.
        """
        from kiro_crew.history import ConversationLog

        log = ConversationLog(tmp_path)
        log.update_metadata("k", {"folder_id": "user-picked"})

        def _unreadable(key: str) -> tuple[dict[str, Any], bool]:
            return {}, False

        log._read_metadata_status = _unreadable  # type: ignore[method-assign]

        applied = log.update_metadata_if("k", {"folder_id": "ours"}, lambda m: True)
        assert applied is False
        # Restore the real reader to confirm nothing was written.
        del log._read_metadata_status
        assert log.get_metadata("k")["folder_id"] == "user-picked"

    def test_a_session_with_no_file_yet_still_merges(self, tmp_path: Any) -> None:
        """Failing closed must not break the ordinary first-write path.

        A session with no file on disk is genuinely empty, not unreadable, so the
        guard still gets to decide.
        """
        from kiro_crew.history import ConversationLog

        log = ConversationLog(tmp_path)
        assert log.update_metadata_if("fresh", {"folder_id": "x"}, lambda m: True)
        assert log.get_metadata("fresh")["folder_id"] == "x"


class TestSlotCreateFolderAssignment:
    """A failed assignment abandons itself, not the slot's existing placement."""

    @pytest.mark.asyncio
    async def test_a_vanished_target_leaves_the_previous_folder_intact(
        self, tmp_path: Any, monkeypatch: Any
    ) -> None:
        """`name` can address an already-used slot.

        Clearing `folder_id` when the requested folder turns out to be deleted
        unfiled a conversation sitting in a different, perfectly valid folder — a
        move that failed took the old placement with it.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        from kiro_crew.dashboard import chat_handlers

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("myslot")
        slot.append("user", "hello")
        slot.drain()
        state._folders = [
            {"id": "home", "name": "Home", "order": 0, "collapsed": False},
            {"id": "target", "name": "Target", "order": 1, "collapsed": False},
        ]
        slot.folder_id = "home"

        # The target is deleted between the handler's unlocked check and the store
        # lock, which is what _unhide_folder reports from inside that lock.
        async def _gone(state_: Any, folder_id: str) -> bool:
            return folder_id != "target"

        monkeypatch.setattr(chat_handlers, "_unhide_folder", _gone)

        app = web.Application()
        app["state"] = state
        app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
        async with TestClient(TestServer(app)) as client:
            await client.post(
                "/api/chat/slots", json={"name": "myslot", "folder_id": "target"}
            )

        # The failed move must not have unfiled the conversation.
        assert state._slots["myslot"].folder_id == "home"


@pytest.mark.skipif(os.name == "nt", reason="asserts POSIX mode bits")
def test_the_folder_store_still_publishes_owner_only(dashboard_state: Any) -> None:
    """The folder store must land at 0o600, not at the umask default.

    The hand-rolled writer this replaced created its temp with
    ``tempfile.mkstemp`` and never widened it, so folders.json published at
    0o600. ``atomic_write`` falls back to ``_get_default_mode()``, normally
    0o644, when the caller passes no mode. The explicit ``mode=0o600`` is the
    whole defence against widening these four files, and nothing else in the
    suite would catch its removal, so it gets a guard rather than a comment.
    """
    asyncio.run(dashboard_state.mutate_folders(_append("a")))

    path = config_dir() / dashboard_state._FOLDERS_FILE
    assert path.exists(), "the folder store must have been written"
    assert path.stat().st_mode & 0o777 == 0o600
