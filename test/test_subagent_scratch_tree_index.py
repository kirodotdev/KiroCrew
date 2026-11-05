"""Scratch tree index persistence and conversation resume coverage."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import requires_symlinks
from kiro_crew import agent_scratch as sc
from kiro_crew import agent_scratch_trees as trees
from kiro_crew.testing.wait import await_until

_UNALLOCATABLE_PID = 99_999_999_999


@pytest.fixture(autouse=True)
def isolated_tree_bindings(_floor_monkeypatch) -> None:
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_LIVE", {})
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_TICKETS", {})
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_UNPERSISTED", set(), raising=False)
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_RETIRED_TICKET", -1, raising=False)
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_DISK_SNAPSHOT", None, raising=False)


@pytest.fixture(name="scratch_root")
def scratch_root_fixture(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


class TestAResumedKeyRejoinsItsTree:
    """A resumed conversation rejoins the scratch tree recorded for its key.

    The gateway keeps the index inside the masked root and passes the recorded
    tree as ``shared_scratch`` when the conversation's process starts.
    """

    def test_the_index_round_trips_and_prunes_reclaimed_trees(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        old = scratch_root / "dashboard-chat-1-abcdef01"
        old.mkdir()
        trees.record_tree("dashboard:chat-1", old)
        assert trees.recorded_tree("dashboard:chat-1") == old
        old.rmdir()  # swept
        new = scratch_root / "dashboard-chat-2-12345678"
        new.mkdir()
        trees.record_tree("dashboard:chat-2", new)
        assert trees.recorded_tree("dashboard:chat-1") is None
        assert trees.recorded_tree("dashboard:chat-2") == new

    def test_index_overflow_preserves_existing_bindings(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        row_size = len(json.dumps({"dashboard:chat-00000": tree.name}).encode("utf-8"))
        count = (trees._TREE_INDEX_MAX_BYTES - 2) // row_size
        rows = {f"dashboard:chat-{i:05d}": tree.name for i in range(count)}
        original = json.dumps(rows, sort_keys=True).encode("utf-8")
        assert len(original) <= trees._TREE_INDEX_MAX_BYTES
        index = scratch_root / trees.TREE_INDEX_FILENAME
        index.write_bytes(original)
        key = f"dashboard:chat-{count:05d}"
        assert (
            len(json.dumps({**rows, key: tree.name}).encode("utf-8")) > trees._TREE_INDEX_MAX_BYTES
        )

        assert not trees.record_tree(key, tree, ticket=1)
        assert index.read_bytes() == original
        assert trees.recorded_tree("dashboard:chat-00000") == tree
        assert trees.recorded_tree(key) == tree

    def test_only_a_direct_child_of_the_root_is_recorded(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        scratch_root.mkdir(parents=True)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        trees.record_tree("dashboard:chat-1", outside)
        assert trees.recorded_tree("dashboard:chat-1") is None

    def test_a_delayed_predecessor_record_cannot_retire_its_successor(
        self, scratch_root: Path
    ) -> None:
        # Executor writes can acquire the lock out of submission order.
        # The ticket fence skips an older record,
        # so a delayed predecessor cannot overwrite the successor's mapping.
        scratch_root.mkdir(parents=True)
        first = scratch_root / "dashboard-chat-fence-11111111"
        second = scratch_root / "dashboard-chat-fence-22222222"
        first.mkdir()
        second.mkdir()
        trees.record_tree("dashboard:chat-fence", second, ticket=2)
        trees.record_tree("dashboard:chat-fence", first, ticket=1)
        assert trees.recorded_tree("dashboard:chat-fence") == second

    def test_a_row_naming_a_path_is_not_a_tree(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        (scratch_root / trees.TREE_INDEX_FILENAME).write_text(
            '{"dashboard:chat-1": "../../etc", "dashboard:chat-2": ".."}'
        )
        assert trees.recorded_tree("dashboard:chat-1") is None
        assert trees.recorded_tree("dashboard:chat-2") is None

    def test_the_sweep_leaves_the_index_alone(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        trees.record_tree("dashboard:chat-1", tree)
        sc.sweep_dead_scratch()
        assert (scratch_root / trees.TREE_INDEX_FILENAME).is_file()

    def _manager(self, resume_sid: str, work_scratch: Path | None):
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 0
        cfg.session.pool_agent = "kirocrew"
        cfg.session.pool_ttl_secs = 1800
        cfg.session.timeout_secs = 3600
        cfg.agent.default_agent = ""
        cfg.agent.model = "auto"

        def _provider():
            p = MagicMock()
            p.start = AsyncMock()
            p.shutdown = AsyncMock()
            p.is_process_alive = MagicMock(return_value=True)
            p.exit_code = None
            p.cwd = ""
            p.work_scratch_dir = work_scratch
            return p

        factory = MagicMock(side_effect=lambda *a, **kw: _provider())
        with patch("kiro_crew.session.default_project_dir", return_value=""):
            mgr = SessionManager(cfg, provider_factory=factory)
        mgr._session_map = MagicMock()
        mgr._session_map.get = MagicMock(return_value=resume_sid or None)
        mgr._session_map.get_cwd = MagicMock(return_value="")
        mgr._session_map.get_provider = MagicMock(return_value="acp")
        return mgr, factory

    @pytest.mark.asyncio
    async def test_a_resume_is_handed_the_recorded_tree(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        trees.record_tree("dashboard:chat-1", tree)
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs["shared_scratch"] == tree

    @pytest.mark.asyncio
    async def test_a_resume_keeps_a_cwd_inside_its_rejoined_tree(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The hidden-scratch check sees the tree a resume rejoins, not only a caller's."""
        from kiro_crew import platform_compat, sandbox_tiers

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(sandbox_tiers, "configured_sandbox_mode", lambda: "strict")
        monkeypatch.setattr(sandbox_tiers, "_clamp_sandbox_mode", lambda mode: mode)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        work = tree / "project"
        work.mkdir(parents=True)
        trees.record_tree("dashboard:chat-1", tree)
        mgr, factory = self._manager("sid-1", None)
        mgr._session_map.get_cwd = MagicMock(return_value=str(work))
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs["shared_scratch"] == tree
        assert factory.call_args.kwargs["cwd"] == str(work)

    def _hidden_cwd_in_recorded_tree(self, scratch_root: Path, monkeypatch) -> tuple[Path, Path]:
        from kiro_crew import platform_compat, sandbox_tiers

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(sandbox_tiers, "configured_sandbox_mode", lambda: "strict")
        monkeypatch.setattr(sandbox_tiers, "_clamp_sandbox_mode", lambda mode: mode)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        work = tree / "project"
        work.mkdir(parents=True)
        trees.record_tree("dashboard:chat-1", tree)
        return tree, work

    @pytest.mark.asyncio
    async def test_a_provider_switch_drops_a_cwd_only_the_recorded_tree_kept(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The switch drops the resume, so the tree is not bound and the cwd is hidden."""
        _tree, work = self._hidden_cwd_in_recorded_tree(scratch_root, monkeypatch)
        mgr, factory = self._manager("sid-1", None)
        mgr._session_map.get_cwd = MagicMock(return_value=str(work))
        mgr._session_map.get_provider = MagicMock(return_value="another-harness")
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert factory.call_args.kwargs["cwd"] is None

    @pytest.mark.asyncio
    async def test_a_failed_join_retry_drops_a_cwd_only_the_recorded_tree_kept(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The retry runs without the recorded tree, so the cwd inside it is hidden."""
        tree, work = self._hidden_cwd_in_recorded_tree(scratch_root, monkeypatch)
        mgr, factory = self._manager("sid-1", None)
        mgr._session_map.get_cwd = MagicMock(return_value=str(work))
        make_provider = factory.side_effect
        monkeypatch.setattr(mgr, "_dispatch_hard_kill", MagicMock())

        def build(*args, **kwargs):
            provider = make_provider(*args, **kwargs)
            if kwargs.get("shared_scratch") is not None:
                provider.start = AsyncMock(side_effect=sc.SharedScratchJoinError("no join"))
            return provider

        factory.side_effect = build
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        calls = [c.kwargs for c in factory.call_args_list]
        assert any(kw.get("shared_scratch") == tree and kw["cwd"] == str(work) for kw in calls)
        last = calls[-1]
        assert last.get("shared_scratch") is None
        assert last["cwd"] is None

    @requires_symlinks
    @pytest.mark.asyncio
    async def test_a_linked_recorded_owner_is_retired_and_resume_starts_fresh(
        self, scratch_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "recorded"
        fresh = scratch_root / "fresh"
        tree.mkdir()
        fresh.mkdir()
        target = tmp_path / "owner-target"
        target.write_text("untouched")
        (tree / sc.OWNER_FILENAME).symlink_to(target)
        key = "dashboard:chat-1"
        assert trees.record_tree(key, tree)
        mgr, factory = self._manager("sid-1", fresh)
        make_provider = factory.side_effect
        starts = []
        retired = []
        kill = MagicMock()
        monkeypatch.setattr(mgr, "_dispatch_hard_kill", kill)

        def build(*args, **kwargs):
            provider = make_provider(*args, **kwargs)
            shared = kwargs.get("shared_scratch")

            async def start():
                starts.append(shared)
                if shared is not None:
                    assert sc.adopt_owner(shared, _UNALLOCATABLE_PID) == "refused"
                    raise sc.SharedScratchJoinError("linked owner")
                retired.append(trees.recorded_tree(key))

            provider.start = AsyncMock(side_effect=start)
            return provider

        factory.side_effect = build
        try:
            provider, _, _ = await mgr.get_or_create(key, agent="kirocrew")
            assert provider.work_scratch_dir == fresh
            assert starts == [tree, None]
            assert retired == [None]
            kill.assert_called_once()
            assert trees.recorded_tree(key) == fresh
            assert (
                json.loads((scratch_root / trees.TREE_INDEX_FILENAME).read_text())[key]
                == fresh.name
            )
            assert target.read_text() == "untouched"
            assert tree.is_dir()
        finally:
            await mgr.close_all()

    @pytest.mark.parametrize("caller_tree", [False, True])
    @pytest.mark.asyncio
    async def test_join_recovery_is_once_and_only_for_a_recorded_tree(
        self, scratch_root: Path, monkeypatch, caller_tree: bool
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "recorded"
        tree.mkdir()
        key = "dashboard:chat-1"
        assert trees.record_tree(key, tree)
        mgr, factory = self._manager("sid-1", None)
        make_provider = factory.side_effect
        starts = []
        monkeypatch.setattr(mgr, "_dispatch_hard_kill", MagicMock())

        def build(*args, **kwargs):
            provider = make_provider(*args, **kwargs)

            async def start():
                starts.append(kwargs.get("shared_scratch"))
                raise sc.SharedScratchJoinError("cannot join")

            provider.start = AsyncMock(side_effect=start)
            return provider

        factory.side_effect = build
        kwargs = {"shared_scratch": tree} if caller_tree else {}
        try:
            with pytest.raises(sc.SharedScratchJoinError, match="cannot join"):
                await mgr.get_or_create(key, agent="kirocrew", **kwargs)
            assert starts == ([tree] if caller_tree else [tree, None])
            assert trees.recorded_tree(key) == (tree if caller_tree else None)
        finally:
            await mgr.close_all()

    @pytest.mark.parametrize("memory_mode", ["persistent", "temporary", "incognito"])
    @pytest.mark.asyncio
    async def test_a_provider_switch_or_restricted_mode_does_not_adopt_the_tree(
        self, scratch_root: Path, memory_mode: str
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "recorded"
        tree.mkdir()
        assert trees.record_tree("dashboard:chat-1", tree)
        mgr, factory = self._manager("sid-1", None)
        mgr._session_map.get_provider.return_value = "claude_code"
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew", memory_mode=memory_mode)
            assert all("shared_scratch" not in call.kwargs for call in factory.call_args_list)
            if memory_mode == "persistent":
                mgr._session_map.clear_sid.assert_called_once_with("dashboard:chat-1")
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_fresh_conversation_starts_its_own_tree(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        trees.record_tree("dashboard:chat-1", tree)
        mgr, factory = self._manager("", None)
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert "shared_scratch" not in factory.call_args.kwargs

    @pytest.mark.asyncio
    async def test_failed_replacement_binding_cannot_resume_the_reset_tree(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "discarded-conversation"
        successor = scratch_root / "replacement-conversation"
        predecessor.mkdir()
        successor.mkdir()
        key = "dashboard:chat-reset"
        mgr, factory = self._manager("", predecessor)
        try:
            await mgr.get_or_create(key, agent="kirocrew")
            assert trees.recorded_tree(key) == predecessor
            mgr.release(key)
            assert await mgr.reset(key, clear_conversation=True)
            mgr._session_map.clear_sid.assert_called_once_with(key)

            make_provider = factory.side_effect

            def replacement(*args, **kwargs):
                provider = make_provider(*args, **kwargs)
                provider.work_scratch_dir = successor
                return provider

            factory.side_effect = replacement
            record = MagicMock(return_value=False)
            monkeypatch.setattr(trees, "record_tree", record)
            await mgr.get_or_create(key, agent="kirocrew")
            record.assert_called_once_with(key, successor, ticket=record.call_args.kwargs["ticket"])
            assert "shared_scratch" not in factory.call_args.kwargs
        finally:
            await mgr.close_all()

        trees._TREE_INDEX_LIVE.clear()
        trees._TREE_INDEX_TICKETS.clear()
        trees._TREE_INDEX_UNPERSISTED.clear()
        monkeypatch.setattr(trees, "_TREE_INDEX_RETIRED_TICKET", -1)
        monkeypatch.setattr(trees, "_TREE_INDEX_DISK_SNAPSHOT", None)
        assert trees.recorded_tree(key) is None
        assert predecessor.is_dir()

    @pytest.mark.asyncio
    async def test_a_new_process_records_the_tree_it_exposes(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-12345678"
        tree.mkdir()
        mgr, _factory = self._manager("", tree)
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert trees.recorded_tree("dashboard:chat-1") == tree

    def test_a_cached_binding_takes_precedence_over_its_predecessor(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        # Failed persistence can leave the predecessor on disk; the live
        # successor still owns this process's resumed conversation.
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "dashboard-chat-1-aaaaaaaa"
        successor = scratch_root / "dashboard-chat-1-bbbbbbbb"
        predecessor.mkdir()
        successor.mkdir()
        trees.record_tree("dashboard:chat-1", predecessor)  # persisted
        monkeypatch.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
        assert not trees.record_tree("dashboard:chat-1", successor)
        assert trees.recorded_tree("dashboard:chat-1") == successor

    def test_a_reclaimed_failed_successor_never_resurrects_the_disk_predecessor(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "dashboard-chat-1-aaaaaaaa"
        successor = scratch_root / "dashboard-chat-1-bbbbbbbb"
        predecessor.mkdir()
        successor.mkdir()
        trees.record_tree("dashboard:chat-1", predecessor)
        monkeypatch.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
        assert not trees.record_tree("dashboard:chat-1", successor)
        successor.rmdir()  # swept
        assert trees.recorded_tree("dashboard:chat-1") is None
        assert not trees._TREE_INDEX_LIVE

    @pytest.mark.parametrize("failure", ["unwritable", "overflow"])
    def test_failed_bindings_have_bounded_admission_and_keep_existing_guards(
        self, scratch_root: Path, monkeypatch, caplog, failure: str
    ) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "predecessor"
        successor = scratch_root / "successor"
        predecessor.mkdir()
        successor.mkdir()
        keys = [f"chat:{i}" for i in range(4)]
        (scratch_root / trees.TREE_INDEX_FILENAME).write_text(
            json.dumps(dict.fromkeys(keys, predecessor.name))
        )
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 2, raising=False)
        if failure == "unwritable":
            monkeypatch.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
        else:
            monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", 2)
        for ticket, key in enumerate(keys):
            assert not trees.record_tree(key, successor, ticket=ticket)
            assert len(trees._TREE_INDEX_LIVE) <= 2
            assert len(trees._TREE_INDEX_TICKETS) <= 2
            assert trees._TREE_INDEX_UNPERSISTED <= trees._TREE_INDEX_LIVE.keys()
        assert trees.recorded_tree(keys[0]) == successor
        assert trees.recorded_tree(keys[1]) == successor
        assert trees.recorded_tree(keys[2]) is None
        assert trees.recorded_tree(keys[3]) is None
        warnings = [r for r in caplog.records if "uncached scratch resumes" in r.message]
        assert len(warnings) == 1

    @pytest.mark.parametrize("guard_loss", ["admission", "retirement"])
    def test_lost_guard_preserves_unrelated_resume_and_refuses_stale_writer(
        self, scratch_root: Path, monkeypatch, guard_loss: str
    ) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "predecessor"
        successor = scratch_root / "successor"
        disk_only = scratch_root / "disk-only"
        for tree in (predecessor, successor, disk_only):
            tree.mkdir()
        key = "dashboard:chat-affected"
        unrelated = "dashboard:chat-disk-only"
        index = scratch_root / trees.TREE_INDEX_FILENAME
        original = json.dumps({key: predecessor.name, unrelated: disk_only.name})
        index.write_text(original, encoding="utf-8")
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 1)

        if guard_loss == "admission":
            assert trees.record_tree("dashboard:chat-retained", successor, ticket=1)
            assert not trees.record_tree(key, successor, ticket=3)
            successor.rmdir()
        else:
            with monkeypatch.context() as failed:
                failed.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
                assert not trees.record_tree(key, successor, ticket=3)
            successor.rmdir()
            assert trees.recorded_tree(key) is None

        assert trees.recorded_tree(unrelated) == disk_only
        assert trees.recorded_tree(key) is None
        assert key not in trees._TREE_INDEX_UNPERSISTED
        before_stale_write = index.read_bytes()
        assert not trees.record_tree(key, predecessor, ticket=2)
        assert index.read_bytes() == before_stale_write
        assert trees.recorded_tree(key) is None
        assert trees.recorded_tree(unrelated) == disk_only
        assert trees.record_tree(key, predecessor, ticket=4)
        assert trees.recorded_tree(key) == predecessor

    @pytest.mark.parametrize("admitted_before_snapshot", [True, False])
    def test_snapshot_cannot_restore_a_successfully_persisted_keys_predecessor(
        self, scratch_root: Path, monkeypatch, admitted_before_snapshot: bool
    ) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "predecessor"
        successor = scratch_root / "successor"
        predecessor.mkdir()
        successor.mkdir()
        key = "dashboard:chat-affected"
        (scratch_root / trees.TREE_INDEX_FILENAME).write_text(
            json.dumps({key: predecessor.name}), encoding="utf-8"
        )
        if admitted_before_snapshot:
            with monkeypatch.context() as failed:
                failed.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
                assert not trees.record_tree(key, successor, ticket=1)
        with monkeypatch.context() as full:
            full.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 0)
            assert not trees.record_tree("dashboard:chat-rejected", successor, ticket=2)
        assert trees.record_tree(key, successor, ticket=3)
        assert key not in trees._TREE_INDEX_UNPERSISTED
        successor.rmdir()
        assert trees.recorded_tree(key) is None
        assert trees.recorded_tree(key) is None
        assert predecessor.is_dir()

    def test_snapshot_only_shrinks_under_rejected_registration_churn(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "tree"
        tree.mkdir()
        key = "dashboard:chat-disk-only"
        index = scratch_root / trees.TREE_INDEX_FILENAME
        index.write_text(json.dumps({key: tree.name}), encoding="utf-8")
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 1)
        assert trees.record_tree("dashboard:chat-retained", tree, ticket=1)
        for ticket in range(2, 12):
            assert not trees.record_tree(f"dashboard:chat-rejected-{ticket}", tree, ticket=ticket)
            assert len(trees._TREE_INDEX_LIVE) == len(trees._TREE_INDEX_TICKETS) == 1
            assert trees._TREE_INDEX_DISK_SNAPSHOT == (scratch_root, {key: tree.name})
        assert trees.recorded_tree(key) == tree
        assert not trees.record_tree(key, tree, ticket=12)
        assert trees._TREE_INDEX_DISK_SNAPSHOT == (scratch_root, {})
        assert trees.recorded_tree(key) is None
        assert key in json.loads(index.read_text(encoding="utf-8"))

    def test_admission_retires_reclaimed_bindings_and_their_tickets(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "tree"
        tree.mkdir()
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 1, raising=False)
        assert trees.record_tree("chat:1", tree, ticket=2)
        tree.rmdir()
        successor = scratch_root / "successor"
        successor.mkdir()
        assert trees.record_tree("chat:2", successor, ticket=3)
        assert set(trees._TREE_INDEX_LIVE) == {"chat:2"}
        assert set(trees._TREE_INDEX_TICKETS) == {"chat:2"}
        tree.mkdir()
        assert not trees.record_tree("chat:1", tree, ticket=1)
        assert trees.recorded_tree("chat:1") is None
        assert trees.recorded_tree("chat:2") == successor

    def test_successful_retirement_keeps_unrelated_disk_only_resumes(
        self, scratch_root: Path
    ) -> None:
        scratch_root.mkdir(parents=True)
        retired = scratch_root / "retired"
        disk_only = scratch_root / "disk-only"
        retired.mkdir()
        disk_only.mkdir()
        (scratch_root / trees.TREE_INDEX_FILENAME).write_text(
            json.dumps({"chat:disk-only": disk_only.name})
        )
        assert trees.record_tree("chat:retired", retired, ticket=1)
        assert not trees._TREE_INDEX_UNPERSISTED
        retired.rmdir()
        assert trees.recorded_tree("chat:retired") is None
        assert not trees._TREE_INDEX_TICKETS
        assert trees.recorded_tree("chat:disk-only") == disk_only

    @pytest.mark.asyncio
    async def test_allocation_admission_bounds_failed_persistence_history(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "tree"
        tree.mkdir()
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 2)
        monkeypatch.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
        mgr, _factory = self._manager("", tree)
        try:
            for i in range(4):
                await mgr._allocation_boundary()._record_scratch_tree(f"chat:{i}", tree)
                assert len(trees._TREE_INDEX_LIVE) <= 2
                assert len(trees._TREE_INDEX_TICKETS) <= 2
                assert trees._TREE_INDEX_UNPERSISTED <= trees._TREE_INDEX_LIVE.keys()
            assert trees.recorded_tree("chat:0") == tree
            assert trees.recorded_tree("chat:2") is None
        finally:
            await mgr.close_all()

    def test_delayed_writer_is_refused_after_failed_successor_and_guard_retirement(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "predecessor"
        successor = scratch_root / "successor"
        predecessor.mkdir()
        successor.mkdir()
        assert trees.record_tree("chat:1", predecessor, ticket=1)
        with monkeypatch.context() as failed:
            failed.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
            assert not trees.record_tree("chat:1", successor, ticket=3)
        assert not trees.record_tree("chat:1", predecessor, ticket=2)
        assert trees.recorded_tree("chat:1") == successor
        assert trees.record_tree("chat:1", successor, ticket=4)
        successor.rmdir()
        assert trees.recorded_tree("chat:1") is None
        assert not trees.record_tree("chat:1", predecessor, ticket=2)
        assert trees.recorded_tree("chat:1") is None
        assert trees.record_tree("chat:1", predecessor, ticket=5)
        assert trees.recorded_tree("chat:1") == predecessor

    def test_cross_key_out_of_order_write_can_persist_a_retained_binding(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "tree"
        tree.mkdir()
        with monkeypatch.context() as failed:
            failed.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
            assert not trees.record_tree("chat:older", tree, ticket=1)
        assert trees.record_tree("chat:newer", tree, ticket=3)
        assert trees.record_tree("chat:older", tree, ticket=2)
        assert trees.recorded_tree("chat:older") == tree
        assert trees.record_tree("chat:older", tree, ticket=4)
        assert "chat:older" not in trees._TREE_INDEX_UNPERSISTED

    def test_retired_floor_refuses_unknown_older_keys_but_keeps_retained_ordering(
        self, scratch_root: Path
    ) -> None:
        scratch_root.mkdir(parents=True)
        retained = scratch_root / "retained"
        retired = scratch_root / "retired"
        retained.mkdir()
        retired.mkdir()
        assert trees.record_tree("chat:retained", retained, ticket=1)
        assert trees.record_tree("chat:retired", retired, ticket=3)
        retired.rmdir()
        assert trees.recorded_tree("chat:retired") is None
        assert not trees.record_tree("chat:unknown", retained, ticket=2)
        assert trees.recorded_tree("chat:unknown") is None
        assert trees.record_tree("chat:retained", retained, ticket=2)
        assert trees.record_tree("chat:unknown", retained, ticket=4)
        assert trees.recorded_tree("chat:unknown") == retained

    def test_disk_reader_rechecks_a_successor_admitted_while_waiting_for_the_lock(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "predecessor"
        successor = scratch_root / "successor"
        predecessor.mkdir()
        successor.mkdir()
        (scratch_root / trees.TREE_INDEX_FILENAME).write_text(
            json.dumps({"chat:1": predecessor.name})
        )

        class AdmitBeforeDiskRead:
            def __enter__(self):
                monkeypatch.setitem(trees._TREE_INDEX_LIVE, "chat:1", successor)

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr(trees, "_TREE_INDEX_LOCK", AdmitBeforeDiskRead())
        assert trees.recorded_tree("chat:1") == successor

    def test_oversized_directory_name_is_not_retained(self, scratch_root: Path, monkeypatch):
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "long-name"
        tree.mkdir()
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_NAME_CHARS", len(tree.name) - 1)
        assert not trees.record_tree("chat:1", tree, ticket=1)
        assert not trees._TREE_INDEX_LIVE
        assert not trees._TREE_INDEX_TICKETS
        assert trees.recorded_tree("chat:1") is None

    def test_oversized_full_path_is_not_retained(self, scratch_root: Path, monkeypatch):
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "tree"
        tree.mkdir()
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_PATH_CHARS", len(str(tree)) - 1)
        assert not trees.record_tree("chat:1", tree, ticket=1)
        assert not trees._TREE_INDEX_LIVE
        assert not trees._TREE_INDEX_TICKETS

    def test_data_home_change_cannot_remap_a_cached_basename(
        self, scratch_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        original = scratch_root / "tree"
        original.mkdir()
        assert trees.record_tree("chat:1", original, ticket=1)
        other_home = tmp_path / "other-home"
        unrelated = other_home / "scratch" / original.name
        unrelated.mkdir(parents=True)
        monkeypatch.setattr(sc, "config_dir", lambda: other_home)
        assert trees.recorded_tree("chat:1") is None
        assert "chat:1" not in trees._TREE_INDEX_LIVE
        assert "chat:1" not in trees._TREE_INDEX_TICKETS
        assert trees.record_tree("chat:1", unrelated, ticket=2)
        assert trees.recorded_tree("chat:1") == unrelated

    def test_oversized_failed_key_is_not_retained_or_resumed_from_disk(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "predecessor"
        successor = scratch_root / "successor"
        predecessor.mkdir()
        successor.mkdir()
        key = "chat:" + "x" * 32
        (scratch_root / trees.TREE_INDEX_FILENAME).write_text(json.dumps({key: predecessor.name}))
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_KEY_CHARS", len(key) - 1, raising=False)
        monkeypatch.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
        assert not trees.record_tree(key, successor, ticket=1)
        assert not trees._TREE_INDEX_LIVE
        assert trees.recorded_tree(key) is None

    def test_only_a_direct_child_of_the_root_is_bound(
        self, scratch_root: Path, tmp_path: Path
    ) -> None:
        scratch_root.mkdir(parents=True)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        assert not trees.record_tree("dashboard:chat-1", outside)
        assert trees.recorded_tree("dashboard:chat-1") is None

    @pytest.mark.asyncio
    async def test_the_tree_is_bound_before_the_sid_is_published(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        predecessor = scratch_root / "dashboard-chat-1-aaaaaaaa"
        predecessor.mkdir()
        trees.record_tree("dashboard:chat-1", predecessor)
        successor = scratch_root / "dashboard-chat-1-bbbbbbbb"
        successor.mkdir()

        mgr, _factory = self._manager("", successor)
        service = mgr._allocation_boundary()
        service._deps = dataclasses.replace(
            service._deps, is_acp_provider=MagicMock(return_value=True)
        )
        observed: list[Path | None] = []

        def read_after_restart(*args, **kwargs):
            trees._TREE_INDEX_LIVE.clear()
            observed.append(trees.recorded_tree("dashboard:chat-1"))

        mgr._session_map.set = MagicMock(side_effect=read_after_restart)
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert observed == [successor], "the SID was published before the tree was bound"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exposes_tree", [False, True])
    async def test_failed_retirement_names_the_index_and_refuses_allocation(
        self, scratch_root: Path, monkeypatch, exposes_tree: bool
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "recorded"
        tree.mkdir()
        key = "dashboard:chat-1"
        assert trees.record_tree(key, tree)
        # A refused admission: the provider's tree never became the live binding.
        successor = scratch_root / "successor"
        successor.mkdir()
        mgr, _factory = self._manager("sid-1", successor if exposes_tree else None)
        monkeypatch.setattr(trees, "record_tree", MagicMock(return_value=False))
        monkeypatch.setattr(trees, "abandon_tree", MagicMock(return_value=False))
        try:
            with pytest.raises(OSError) as caught:
                await mgr.get_or_create(key, agent="kirocrew")
            assert str(scratch_root / trees.TREE_INDEX_FILENAME) in str(caught.value)
            assert "repair or remove this index" in str(caught.value)
            assert mgr.get_provider(key) is None
            mgr._session_map.set.assert_not_called()
        finally:
            await mgr.close_all()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["write_raises", "index_unreadable"])
    async def test_failed_write_keeps_the_live_binding_for_a_same_process_resume(
        self, scratch_root: Path, monkeypatch, failure: str
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-12345678"
        tree.mkdir()
        key = "dashboard:chat-1"
        with monkeypatch.context() as broken:
            if failure == "write_raises":
                broken.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
            else:
                broken.setattr(
                    trees, "_read_tree_index", MagicMock(side_effect=PermissionError("unreadable"))
                )
            mgr, _factory = self._manager("", tree)
            try:
                await mgr.get_or_create(key, agent="kirocrew")
                assert mgr.get_provider(key) is not None
            finally:
                await mgr.close_all()
        assert trees.recorded_tree(key) == tree
        resumed, factory = self._manager("sid-1", None)
        try:
            await resumed.get_or_create(key, agent="kirocrew")
        finally:
            await resumed.close_all()
        assert factory.call_args.kwargs["shared_scratch"] == tree

    def test_a_late_failed_join_keeps_a_successor_binding(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        failed = scratch_root / "failed"
        successor = scratch_root / "successor"
        failed.mkdir()
        successor.mkdir()
        key = "dashboard:chat-1"
        assert trees.record_tree(key, failed)
        assert trees.record_tree(key, successor)
        assert trees.abandon_tree(key, failed)
        assert trees.recorded_tree(key) == successor
        assert trees._read_tree_index(scratch_root) == {key: successor.name}
        # A fresh process reads the row the late retirement left in place.
        trees._TREE_INDEX_LIVE.clear()
        assert trees.recorded_tree(key) == successor
        assert trees.abandon_tree(key, successor)
        assert trees.recorded_tree(key) is None

    @pytest.mark.asyncio
    async def test_an_undroppable_earlier_row_refuses_publishing_the_successor(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        earlier = scratch_root / "earlier"
        successor = scratch_root / "successor"
        earlier.mkdir()
        successor.mkdir()
        key = "dashboard:chat-1"
        assert trees.record_tree(key, earlier)
        trees._TREE_INDEX_LIVE.clear()
        monkeypatch.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
        mgr, _factory = self._manager("", successor)
        try:
            with pytest.raises(OSError, match="Could not persist scratch retirement"):
                await mgr.get_or_create(key, agent="kirocrew")
            mgr._session_map.set.assert_not_called()
        finally:
            await mgr.close_all()
        assert trees._read_tree_index(scratch_root) == {key: earlier.name}

    def test_drop_other_row_reports_a_surviving_earlier_row(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        earlier = scratch_root / "earlier"
        current = scratch_root / "current"
        earlier.mkdir()
        current.mkdir()
        assert trees.record_tree("chat:1", earlier)
        with monkeypatch.context() as broken:
            broken.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
            assert not trees.drop_other_row("chat:1", current)
            assert trees.drop_other_row("chat:1", earlier)
            assert trees.drop_other_row("chat:2", current)
        with monkeypatch.context() as unreadable:
            unreadable.setattr(
                trees, "_read_tree_index", MagicMock(side_effect=PermissionError("unreadable"))
            )
            assert trees.drop_other_row("chat:1", current)

    def test_drop_other_row_drops_only_an_earlier_tree(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        earlier = scratch_root / "earlier"
        current = scratch_root / "current"
        other = scratch_root / "other"
        for tree in (earlier, current, other):
            tree.mkdir()
        assert trees.record_tree("chat:1", earlier)
        assert trees.record_tree("chat:2", other)
        assert trees.drop_other_row("chat:2", other)
        assert trees.drop_other_row("chat:1", current)
        assert trees._read_tree_index(scratch_root) == {"chat:2": other.name}

    @pytest.mark.asyncio
    async def test_sid_waits_for_the_off_loop_write(self, scratch_root: Path, monkeypatch) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-12345678"
        tree.mkdir()
        mgr, _factory = self._manager("", tree)
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        record = trees.record_tree

        def paused_record(*args, **kwargs):
            assert threading.get_ident() != loop_thread
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5), "test did not release the scratch writer"
            return record(*args, **kwargs)

        monkeypatch.setattr(trees, "record_tree", paused_record)
        task = asyncio.create_task(mgr.get_or_create("dashboard:chat-1", agent="kirocrew"))
        try:
            await await_until(started, what="the off-loop scratch write to start")
            assert not task.done()
            mgr._session_map.set.assert_not_called()
        finally:
            release.set()
            await task
            await mgr.close_all()
        trees._TREE_INDEX_LIVE.clear()
        assert trees.recorded_tree("dashboard:chat-1") == tree

    @pytest.mark.asyncio
    @pytest.mark.parametrize("write_failure", ["raises", "missing_row", "full_cap"])
    async def test_failed_index_persistence_allows_session_start(
        self, scratch_root: Path, monkeypatch, caplog, write_failure: str
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-12345678"
        tree.mkdir()
        mgr, _factory = self._manager("", tree)
        service = mgr._allocation_boundary()
        service._deps = dataclasses.replace(
            service._deps, is_acp_provider=MagicMock(return_value=True)
        )

        def failed_write(*args, **kwargs):
            if write_failure == "raises":
                raise OSError("scratch index is unwritable")

        if write_failure == "full_cap":
            assert trees._TREE_INDEX_MAX_ENTRIES == 4096
            monkeypatch.setattr(trees, "_TREE_INDEX_LIVE", {f"chat:{i}": tree for i in range(4096)})
        else:
            monkeypatch.setattr(trees, "atomic_write", failed_write)
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
            assert mgr.get_provider("dashboard:chat-1") is not None
            mgr._session_map.set.assert_called_once()
            assert "dashboard:chat-1" not in trees._read_tree_index(scratch_root)
            assert any(
                record.levelname == "WARNING"
                and "Could not persist scratch binding; continuing session allocation"
                in record.message
                for record in caplog.records
            )
            if write_failure == "full_cap":
                assert len(trees._TREE_INDEX_LIVE) == 4096
                assert "dashboard:chat-1" not in trees._TREE_INDEX_LIVE
        finally:
            await mgr.close_all()
