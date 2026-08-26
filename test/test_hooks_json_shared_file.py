"""ScriptHookStore must not erase ``register_hook`` webhook contexts.

``hooks.json`` is shared between two writers: ``ScriptHookStore`` owns the
``hooks`` key, while the ``register_hook`` MCP tool stores one top-level key per
webhook resume context. The store used to write ``{"hooks": [...]}`` wholesale,
so any script-hook mutation silently dropped every pending context — the data a
webhook callback needs to resume with prior intent.
"""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from kiro_crew.hooks import ScriptHookStore
from kiro_crew.webhooks import WebhookStoreUnreadable


def _seed_context(tmp_path, hook_id: str = "review:pr-123") -> None:
    """Write a register_hook-shaped entry the way mcp_core does."""
    (tmp_path / "hooks.json").write_text(
        json.dumps(
            {
                hook_id: {
                    "session_key": f"hook:{hook_id}",
                    "context_summary": "Round 2 findings addressed; awaiting bot.",
                    "registered_at": 1753830000.0,
                    "compat_flags": 0x4D43,
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _read(tmp_path) -> dict:
    return json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))


class TestForeignKeyPreservation:
    def test_create_preserves_registered_context(self, tmp_path):
        _seed_context(tmp_path)
        store = ScriptHookStore(tmp_path)
        store.create({"name": "fmt", "event": "Stop", "command": "true"})

        data = _read(tmp_path)
        assert "review:pr-123" in data, "register_hook context was erased by create()"
        assert data["review:pr-123"]["context_summary"].startswith("Round 2")
        assert [h["name"] for h in data["hooks"]] == ["fmt"]

    def test_update_toggle_delete_all_preserve(self, tmp_path):
        _seed_context(tmp_path)
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "fmt", "event": "Stop", "command": "true"})

        store.update(hook.id, {"name": "fmt2"})
        assert "review:pr-123" in _read(tmp_path), "erased by update()"

        store.toggle(hook.id)
        assert "review:pr-123" in _read(tmp_path), "erased by toggle()"

        store.delete(hook.id)
        data = _read(tmp_path)
        assert "review:pr-123" in data, "erased by delete()"
        assert data["hooks"] == []

    def test_multiple_contexts_all_survive(self, tmp_path):
        (tmp_path / "hooks.json").write_text(
            json.dumps(
                {
                    "review:pr-1": {"session_key": "hook:review:pr-1", "registered_at": 1.0},
                    "deploy:prod-2": {"session_key": "hook:deploy:prod-2", "registered_at": 2.0},
                    "ci:build-3": {"session_key": "hook:ci:build-3", "registered_at": 3.0},
                }
            ),
            encoding="utf-8",
        )
        store = ScriptHookStore(tmp_path)
        store.create({"name": "guard", "event": "PreToolUse", "command": "true"})

        data = _read(tmp_path)
        assert {"review:pr-1", "deploy:prod-2", "ci:build-3"} <= set(data)

    def test_snapshot_write_preserves_context(self, tmp_path):
        """The async fire() path writes through _save_snapshot, not _save."""
        _seed_context(tmp_path)
        store = ScriptHookStore(tmp_path)
        store.create({"name": "fmt", "event": "Stop", "command": "true"})

        snapshot = [h.to_dict() for h in store.list_all()]
        asyncio.run(asyncio.to_thread(store._save_snapshot, snapshot))

        data = _read(tmp_path)
        assert "review:pr-123" in data, "erased by _save_snapshot()"
        assert len(data["hooks"]) == 1

    def test_a_corrupt_file_is_refused_rather_than_overwritten(self, tmp_path):
        """A corrupt hooks.json must abort the write, not replace the file.

        The file is shared: this store owns the ``hooks`` key while
        ``register_hook`` keeps webhook resume contexts as top-level keys beside
        it. When the read fails, those keys are UNKNOWN, so writing the merged
        result would replace the file with only what this store holds and erase
        every registered context. The earlier behaviour did exactly that on the
        reasoning that script hooks stay recoverable — but the contexts do not.
        """
        store = ScriptHookStore(tmp_path)
        store.create({"name": "keeper", "event": "Stop", "command": "true"})
        path = tmp_path / "hooks.json"
        good = path.read_text(encoding="utf-8")

        path.write_text("{truncated", encoding="utf-8")
        with pytest.raises(WebhookStoreUnreadable):
            store.create({"name": "second", "event": "Stop", "command": "false"})

        # The corrupt bytes are still there for an operator to repair, and the
        # good content was not silently replaced by a hooks-only file.
        assert path.read_text(encoding="utf-8") == "{truncated"
        assert "keeper" in good

    def test_a_failed_save_leaves_no_live_mutation(self, tmp_path):
        """A refused write must not leave the change live in memory.

        `_save` refuses to overwrite an unreadable hooks.json, and each CRUD
        method edits `self._hooks` first. Without a rollback the process keeps
        serving a change that never reached disk — a hook toggled on keeps
        firing, a deleted one keeps existing — while the API answers 503.
        """
        store = ScriptHookStore(tmp_path)
        keeper = store.create({"name": "keeper", "event": "Stop", "command": "true"})
        assert keeper.enabled is True
        before = [h.to_dict() for h in store.list_all()]

        (tmp_path / "hooks.json").write_text("{truncated", encoding="utf-8")

        # Every mutation must refuse AND leave memory untouched.
        with pytest.raises(WebhookStoreUnreadable):
            store.create({"name": "added", "event": "Stop", "command": "false"})
        assert [h.to_dict() for h in store.list_all()] == before

        with pytest.raises(WebhookStoreUnreadable):
            store.toggle(keeper.id)
        assert store.get(keeper.id).enabled is True, "toggle stayed live in memory"

        with pytest.raises(WebhookStoreUnreadable):
            store.update(keeper.id, {"name": "renamed"})
        assert store.get(keeper.id).name == "keeper", "update stayed live in memory"

        with pytest.raises(WebhookStoreUnreadable):
            store.delete(keeper.id)
        assert store.get(keeper.id) is not None, "delete stayed live in memory"

        assert [h.to_dict() for h in store.list_all()] == before

    def test_no_existing_file_is_fine(self, tmp_path):
        store = ScriptHookStore(tmp_path / "fresh")
        store.create({"name": "fmt", "event": "Stop", "command": "true"})

        data = json.loads((tmp_path / "fresh" / "hooks.json").read_text(encoding="utf-8"))
        assert list(data) == ["hooks"]


class TestFireSurvivesCorruptFile:
    """A corrupt `hooks.json` must not fail the turn that fired a hook.

    `_save` refuses to overwrite an unreadable file so it cannot destroy the
    webhook contexts sharing it. `fire()` is awaited from the PRE_TOOL_USE
    path, where a raised exception becomes a rejected tool call, so the refusal
    has to stop at the bookkeeping persist rather than propagate.
    """

    def test_fire_completes_when_the_file_is_corrupt(self, tmp_path):
        store = ScriptHookStore(tmp_path)
        hook = store.create({"name": "noop", "event": "Stop", "command": "true"})
        assert hook.enabled

        # Corrupt the file only AFTER the hook is registered, so the live set is
        # intact and the failure is purely on the write-back path.
        corrupt = '{"hooks": ['
        (tmp_path / "hooks.json").write_text(corrupt, encoding="utf-8")

        # The invariant is that fire() RETURNS rather than raising: it is awaited
        # from the PRE_TOOL_USE path, where an exception becomes a rejected tool
        # call. Deliberately not asserting the hook's exit code -- that runs a
        # real subprocess through the sandbox, which is not reliably available
        # under parallel test load and is incidental to this guard.
        results = asyncio.run(store.fire("Stop", context="done"))
        assert isinstance(results, list)

        # Prove the refusal actually happened, so the test cannot pass vacuously:
        # a refused write leaves the unreadable file exactly as it was.
        assert (tmp_path / "hooks.json").read_text(encoding="utf-8") == corrupt, (
            "expected _save to refuse and leave the corrupt file untouched"
        )

    def test_crud_still_fails_loud_on_the_same_file(self, tmp_path):
        """The guard is scoped to bookkeeping; a lost CRUD write still raises."""
        store = ScriptHookStore(tmp_path)
        (tmp_path / "hooks.json").write_text('{"hooks": [', encoding="utf-8")

        with pytest.raises(WebhookStoreUnreadable):
            store.create({"name": "fmt", "event": "Stop", "command": "true"})


class TestMergeIsSerialised:
    """Merging is not enough on its own — it has to hold the shared lock.

    Preserving foreign keys narrows the data-loss window but does not close it:
    a ``register_hook`` call that commits between the store's read and its write
    is still erased by the stale snapshot. Both writers must take the same
    ``hooks.json.lock``.
    """

    def test_write_holds_the_shared_lock(self, tmp_path, monkeypatch):
        """The merge must run inside webhooks.locked() for the same path."""
        from kiro_crew import webhooks

        held: list[str] = []
        real_locked = webhooks.locked

        @contextmanager
        def _tracking_locked(path):
            held.append(str(path))
            with real_locked(path):
                yield

        monkeypatch.setattr(webhooks, "locked", _tracking_locked)
        _seed_context(tmp_path)
        store = ScriptHookStore(tmp_path)
        store.create({"name": "fmt", "event": "Stop", "command": "true"})

        assert held, "script-hook write did not take hooks.json.lock"
        assert all(h.endswith("hooks.json") for h in held)

    def test_registration_committed_mid_merge_is_not_lost(self, tmp_path):
        """Simulate the interleaving: register_hook commits during the merge.

        The write is driven with a real concurrent mutation landing after the
        store read would have happened. With the lock held, the store's write
        cannot be based on a snapshot older than that mutation.
        """
        _seed_context(tmp_path)
        store = ScriptHookStore(tmp_path)
        store.create({"name": "fmt", "event": "Stop", "command": "true"})

        # A second writer registers a new context the way mcp_core does.
        from kiro_crew import webhooks

        path = tmp_path / "hooks.json"
        with webhooks.locked(path):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["deploy:prod-1"] = {
                "session_key": "hook:deploy:prod-1",
                "context_summary": "registered while the store was busy",
                "registered_at": 1753830001.0,
            }
            webhooks.write_json_atomic(path, data)

        # Another script-hook mutation must keep BOTH contexts.
        store.create({"name": "lint", "event": "Stop", "command": "true"})
        after = _read(tmp_path)
        assert "review:pr-123" in after
        assert "deploy:prod-1" in after
        assert len(after["hooks"]) == 2


class TestConcurrentMutationsAreSerialised:
    """Offloading persistence must not let a mutation be lost.

    The CRUD methods used to be implicitly serialised by running on the single
    event-loop thread; they are now dispatched with ``asyncio.to_thread`` because
    the persist path takes a file lock and fsyncs. Two hazards follow, and only
    the second is deterministic enough to pin:

    1. Iterating ``self._hooks`` to build the payload while another thread
       mutates it can raise "dictionary changed size during iteration". Real but
       GIL-timing-dependent, so not asserted here.
    2. A persist driven from a PRE-CAPTURED snapshot (what ``fire()`` used to do:
       snapshot on the loop, write later in a worker) drops any mutation that
       lands in between. That one can be forced exactly, below.
    """

    def test_a_mutation_during_an_in_flight_persist_is_not_lost(self, tmp_path):
        """Force the real window: payload built, file lock not yet acquired.

        ``_save`` builds its payload from the live dict and only then takes the
        file lock, so the lossy ordering is: A mutates, A builds its payload, B
        mutates and writes a payload containing both, then A writes its older
        payload and B's change is gone from disk. The gate below parks the FIRST
        writer between those two steps, which is the only place that ordering can
        be produced on demand.
        """
        from kiro_crew import webhooks

        store = ScriptHookStore(tmp_path)

        entered = threading.Event()
        release = threading.Event()
        real_locked = webhooks.locked
        seen: list[int] = []

        @contextmanager
        def _gated_lock(path):
            seen.append(1)
            if len(seen) == 1:
                entered.set()
                release.wait(5)
            with real_locked(path):
                yield

        with patch.object(webhooks, "locked", _gated_lock):
            first = threading.Thread(
                target=store.create,
                args=({"name": "first", "event": "Stop", "command": "true"},),
                daemon=True,
            )
            first.start()
            assert entered.wait(5)

            second = threading.Thread(
                target=store.create,
                args=({"name": "second", "event": "Stop", "command": "true"},),
                daemon=True,
            )
            second.start()
            # Serialised: the second mutation must not even build its payload
            # while the first writer holds the store.
            second.join(timeout=1.0)
            assert second.is_alive(), (
                "second mutation ran while the first persist was in flight; its "
                "change can be overwritten by the older payload"
            )

            release.set()
            first.join(5)
            second.join(5)

        on_disk = json.loads((tmp_path / "hooks.json").read_text(encoding="utf-8"))
        assert {h["name"] for h in on_disk["hooks"]} == {"first", "second"}

    def test_a_registered_context_survives_parallel_mutations(self, tmp_path):
        """The foreign-key merge must hold up under real concurrency too."""
        from concurrent.futures import ThreadPoolExecutor

        _seed_context(tmp_path)
        store = ScriptHookStore(tmp_path)

        def _add(i: int) -> None:
            store.create({"name": f"h{i}", "event": "Stop", "command": "true"})

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_add, range(16)))

        after = _read(tmp_path)
        assert "review:pr-123" in after
        assert len(after["hooks"]) == 16
        assert len(store.list_all()) == 16
