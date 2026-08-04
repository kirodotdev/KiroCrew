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
import threading
from typing import Any

import pytest
from chat_test_helpers import _make_state

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
