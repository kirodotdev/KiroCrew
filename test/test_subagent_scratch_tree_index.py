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
    _floor_monkeypatch.setattr(trees, "_QUARANTINE_LIVE", {}, raising=False)


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

    @pytest.fixture(autouse=True)
    def settled_lineage(self, _floor_monkeypatch) -> None:
        """The persisted session-tree lineage reads as loaded unless a test says not."""
        import importlib.util

        self.real_lineage_reason = None
        if importlib.util.find_spec("kiro_crew.session_allocation_parts.scratch_ownership") is None:
            return  # a tree without the check: nothing to stub
        from kiro_crew.session_allocation_parts import scratch_ownership

        self.real_lineage_reason = getattr(scratch_ownership, "_lineage_unavailable_reason", None)
        _floor_monkeypatch.setattr(
            scratch_ownership, "_lineage_unavailable_reason", lambda: "", raising=False
        )

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
    async def test_unrelated_trees_recording_one_directory_resume_into_their_own(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Three unrelated trees recorded on one directory each resume without it."""
        from kiro_crew.acp import chat_runtime_sharing

        monkeypatch.setattr(
            chat_runtime_sharing, "_tree_placement_reader", lambda key: (key, True), raising=False
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = ("dashboard:chat-1", "dashboard:chat-2", "dashboard:chat-3")
        for key in keys:
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart: only the disk rows remain
        bound = []
        for key in keys:
            mgr, factory = self._manager("sid-1", None)
            try:
                await mgr.get_or_create(key, agent="kirocrew")
            finally:
                await mgr.close_all()
            bound.append(factory.call_args.kwargs.get("shared_scratch"))
        assert bound == [None, None, None]

    @pytest.mark.asyncio
    async def test_members_of_one_tree_recording_one_directory_still_rejoin_it(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.acp import chat_runtime_sharing

        monkeypatch.setattr(
            chat_runtime_sharing,
            "_tree_placement_reader",
            lambda key: ("dashboard:chat-lead", True),
            raising=False,
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-lead-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-lead", "dashboard:chat-worker"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create("dashboard:chat-worker", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs["shared_scratch"] == tree

    def _real_reader(
        self, monkeypatch, slots: dict[str, str], saved: dict[str, str] | None = None
    ) -> None:
        """The gateway's own slot readers over *slots* (slot name -> creator)."""
        from types import SimpleNamespace

        from kiro_crew.acp import chat_runtime_sharing
        from kiro_crew.dashboard.state_parts import dashboard_state

        state = SimpleNamespace(
            _slots={
                name: SimpleNamespace(
                    key=name,
                    _created_by=creator,
                    _saved_created_by=(saved or {}).get(name, ""),
                    agent="x",
                )
                for name, creator in slots.items()
            }
        )
        placement = getattr(dashboard_state, "_share_tree_placement", None)
        if placement is not None:
            monkeypatch.setattr(
                chat_runtime_sharing,
                "_tree_placement_reader",
                lambda key: placement(state, key),
                raising=False,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("crew_log_on", [False, True])
    async def test_two_chats_with_an_unverified_creator_never_share_a_directory(
        self, scratch_root: Path, monkeypatch, crew_log_on: bool
    ) -> None:
        """Unverified claims (crew log off, or a forged created_by) end at one sentinel."""
        from kiro_crew.dashboard.session_control_parts.session_revive import UNVERIFIED_CREATOR
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership,
            "_lineage_unavailable_reason",
            (lambda: "") if crew_log_on else (lambda: "the crew log is off"),
            raising=False,
        )
        self._real_reader(
            monkeypatch,
            {"chat-1-1790000000": UNVERIFIED_CREATOR, "chat-2-1790000000": UNVERIFIED_CREATOR},
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000")
        for key in keys:
            trees.record_tree(key, tree)
        trees.quarantine_tree(tree, frozenset(keys))  # an earlier refusal's mark
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        for key in keys:
            assert (
                await scratch_ownership._recorded_tree_of_own_session_tree(key, MagicMock()) is None
            )
        assert trees.quarantined_keys(tree) == frozenset(keys)  # never cleared
        assert trees.keys_on_tree(tree) == frozenset(keys)  # nothing retired

    @pytest.mark.asyncio
    async def test_a_forged_saved_creator_under_a_settled_lineage_never_merges_two_chats(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Restore keeps the forged claim as ``_saved_created_by``; the walk uses it."""
        from kiro_crew import crew_mode
        from kiro_crew.dashboard.session_control_parts.session_revive import UNVERIFIED_CREATOR
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(crew_mode, "lineage_is_settled", lambda: True)
        monkeypatch.setattr(crew_mode, "persisted_tree_slots", lambda: frozenset())
        monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
        from kiro_crew.dashboard.session_control_parts import crew_filing

        monkeypatch.setattr(
            crew_filing, "_frame_parents", lambda state: {k: None for k in state._slots}
        )
        self._real_reader(
            monkeypatch,
            {
                "chat-1-1790000000": UNVERIFIED_CREATOR,
                "chat-2-1790000000": UNVERIFIED_CREATOR,
                "chat-9-1790000000": "",
            },
            saved={
                "chat-1-1790000000": "chat-9-1790000000",
                "chat-2-1790000000": "chat-9-1790000000",
            },
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000")
        for key in keys:
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        assert (
            await scratch_ownership._recorded_tree_of_own_session_tree(keys[0], MagicMock()) is None
        )
        assert trees.quarantined_keys(tree) == frozenset(keys)
        assert trees.keys_on_tree(tree) == frozenset(keys)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("creator", ["cron:job-j", "chat-lead-1790000000"])
    async def test_chats_whose_creator_is_no_live_slot_stay_distinct_owners(
        self, scratch_root: Path, monkeypatch, creator: str
    ) -> None:
        """Two chats of one cron job, or of a lead whose slot did not come back."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": creator, "chat-2-1790000000": creator})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000")
        for key in keys:
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        assert (
            await scratch_ownership._recorded_tree_of_own_session_tree(keys[0], MagicMock()) is None
        )
        assert trees.quarantined_keys(tree) == frozenset(keys)  # quarantined, never retired
        assert trees.keys_on_tree(tree) == frozenset(keys)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("creator", ["", "unverified:creator"])
    async def test_a_directory_whose_only_owner_is_the_resuming_chat_rejoins(
        self, scratch_root: Path, monkeypatch, creator: str
    ) -> None:
        """Including a crew-log-off chat whose own creator is unverified."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": creator})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        trees.record_tree("dashboard:chat-1-1790000000", tree)
        trees._TREE_INDEX_LIVE.clear()
        found = await scratch_ownership._recorded_tree_of_own_session_tree(
            "dashboard:chat-1-1790000000", MagicMock()
        )
        assert found == tree

    @pytest.mark.asyncio
    async def test_a_worker_with_a_verified_shared_creator_still_rejoins_its_lead(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        found = await scratch_ownership._recorded_tree_of_own_session_tree(
            "dashboard:chat-2-1790000000", MagicMock()
        )
        assert found == tree

    @pytest.mark.asyncio
    async def test_a_chat_rejoins_the_directory_its_dedicated_subagent_also_recorded(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A subagent key is placed through its run's parent, so it is no foreign tree."""
        from kiro_crew import subagent_persistence

        self._real_reader(monkeypatch, {"chat-1-1790000000": ""})
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda run_id: (
                {"parent_session": "dashboard:chat-1-1790000000"} if run_id == "abc123" else None
            ),
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1-1790000000", "subagent:abc123"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create("dashboard:chat-1-1790000000", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs["shared_scratch"] == tree

    @pytest.mark.asyncio
    async def test_a_co_owner_that_cannot_be_placed_quarantines_an_unmarked_directory(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """GPT's sequence: B's dedicated subagent's run record is pruned, then A resumes."""
        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        monkeypatch.setattr(subagent_persistence, "read_state", lambda run_id: None)  # pruned
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        (tree / "staged.txt").write_text("B's file")
        keys = ("dashboard:chat-1-1790000000", "subagent:b-worker")
        for key in keys:
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        assert (
            await scratch_ownership._recorded_tree_of_own_session_tree(keys[0], MagicMock()) is None
        )
        assert trees.quarantined_keys(tree) == frozenset(keys)
        assert trees.keys_on_tree(tree) == frozenset(keys)  # quarantined, never retired
        assert (tree / "staged.txt").read_text() == "B's file"

    async def _resume_with_pruned_subagent(
        self, scratch_root: Path, monkeypatch, recorded: str | None
    ) -> tuple[Path | None, Path]:
        """Chat A and a dedicated subagent on A's directory; the run record is pruned."""
        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        monkeypatch.setattr(subagent_persistence, "read_state", lambda run_id: None)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1-1790000000", "subagent:worker"):
            trees.record_tree(key, tree)
        if recorded is not None:
            assert trees.record_parent("subagent:worker", recorded)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        found = await scratch_ownership._recorded_tree_of_own_session_tree(
            "dashboard:chat-1-1790000000", MagicMock()
        )
        return found, tree

    @pytest.mark.asyncio
    async def test_a_chat_keeps_its_directory_after_its_own_subagents_record_is_pruned(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        found, tree = await self._resume_with_pruned_subagent(
            scratch_root, monkeypatch, "dashboard:chat-1-1790000000"
        )
        assert found == tree
        assert trees.quarantined_keys(tree) is None

    @pytest.mark.asyncio
    async def test_a_recorded_parent_in_another_tree_quarantines(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        found, tree = await self._resume_with_pruned_subagent(
            scratch_root, monkeypatch, "dashboard:chat-2-1790000000"
        )
        assert found is None
        assert trees.quarantined_keys(tree) is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("recorded", [None, "dashboard:chat-gone-1790000000"])
    async def test_a_missing_or_unresolvable_recorded_parent_quarantines(
        self, scratch_root: Path, monkeypatch, recorded: str | None
    ) -> None:
        found, tree = await self._resume_with_pruned_subagent(scratch_root, monkeypatch, recorded)
        assert found is None
        assert trees.quarantined_keys(tree) is not None

    @pytest.mark.asyncio
    async def test_an_unseeded_lineage_retires_nothing_and_does_not_hand_the_directory_back(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Unsettled, a shared directory is neither rejoined nor retired: fresh scratch."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership,
            "_lineage_unavailable_reason",
            lambda: "the session-tree lineage is not settled",
            raising=False,
        )
        self._real_reader(monkeypatch, {"chat-1-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        fresh = scratch_root / "dashboard-chat-1-fresh002"
        tree.mkdir()
        fresh.mkdir()
        for key in ("dashboard:chat-1-1790000000", "dashboard:chat-closed-1790000000"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", fresh)
        try:
            await mgr.get_or_create("dashboard:chat-1-1790000000", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert tree.is_dir()
        assert trees.keys_on_tree(tree) == frozenset({"dashboard:chat-closed-1790000000"})
        assert trees.recorded_tree("dashboard:chat-1-1790000000") == fresh

    @pytest.mark.asyncio
    async def test_with_the_crew_log_off_two_unrelated_chats_never_share_a_directory(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership,
            "_lineage_unavailable_reason",
            self.real_lineage_reason,
            raising=False,
        )
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000")
        for key in keys:
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        bound = []
        for key in keys:
            mgr, factory = self._manager("sid-1", None)
            try:
                await mgr.get_or_create(key, agent="kirocrew")
            finally:
                await mgr.close_all()
            bound.append(factory.call_args.kwargs.get("shared_scratch"))
        assert bound == [None, None]
        assert trees.quarantined_keys(tree) == frozenset(keys)

    @pytest.mark.asyncio
    async def test_a_chat_left_alone_on_a_refused_directory_does_not_rejoin_it(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A is refused and moves away; B, now the only binding, is still refused."""
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership,
            "_lineage_unavailable_reason",
            self.real_lineage_reason,
            raising=False,
        )
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        fresh = scratch_root / "dashboard-chat-1-fresh002"
        tree.mkdir()
        fresh.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        for key in (a, b):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", fresh)
        try:
            await mgr.get_or_create(a, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert trees.keys_on_tree(tree) == frozenset({b})
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(b, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None

    @pytest.mark.asyncio
    async def test_a_mark_that_cannot_be_written_still_refuses_the_chat_left_alone(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership,
            "_lineage_unavailable_reason",
            self.real_lineage_reason,
            raising=False,
        )
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, own: False)
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        fresh = scratch_root / "dashboard-chat-1-fresh002"
        tree.mkdir()
        fresh.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        for key in (a, b):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        for key, scratch in ((a, fresh), (b, None)):
            mgr, factory = self._manager("sid-1", scratch)
            try:
                await mgr.get_or_create(key, agent="kirocrew")
            finally:
                await mgr.close_all()
            assert factory.call_args.kwargs.get("shared_scratch") is None
        # The held mark went out with the next binding write that succeeded.
        assert trees._MARKS_ROW in json.loads(
            (scratch_root / trees.TREE_INDEX_FILENAME).read_text()
        )

    @pytest.mark.asyncio
    async def test_a_failed_retirement_keeps_the_mark(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        monkeypatch.setattr(trees, "abandon_tree_all", lambda tree, owners: False)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        trees.record_tree(b, tree)
        trees.quarantine_tree(tree, frozenset({a, b}))
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(b, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert trees.quarantined_keys(tree) == frozenset({a, b})

    @pytest.mark.asyncio
    async def test_a_failed_retirement_write_survives_a_restart_without_mounting(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """GPT's sequence: retirement write fails, gateway restarts, the owner resumes."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        (tree / "staged.txt").write_text("the other tree's file")
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        trees.record_tree(b, tree)
        trees.quarantine_tree(tree, frozenset({a, b}))
        trees._TREE_INDEX_LIVE.clear()
        real_write = trees.atomic_write

        def index_write_fails(path, *args, **kwargs):
            if Path(path).name == trees.TREE_INDEX_FILENAME:
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(trees, "atomic_write", index_write_fails)
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(b, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert trees._QUARANTINE_LIVE[tree.name] == frozenset({a, b})  # held in memory
        monkeypatch.setattr(trees, "atomic_write", real_write)
        assert trees.quarantine_tree(tree, frozenset({b}))  # the next refusal retries
        assert tree.name not in trees._QUARANTINE_LIVE
        # A gateway restart: only what is on disk remains.
        trees._TREE_INDEX_LIVE.clear()
        trees._QUARANTINE_LIVE.clear()
        monkeypatch.setattr(trees, "_TREE_INDEX_DISK_SNAPSHOT", None)
        assert trees.quarantined_keys(tree) == frozenset({a, b})
        monkeypatch.setattr(
            scratch_ownership, "_lineage_unavailable_reason", lambda: "the crew log is off"
        )
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(b, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert (tree / "staged.txt").is_file()

    @pytest.mark.asyncio
    async def test_with_the_crew_log_off_a_chat_and_its_own_subagent_rejoin_after_a_restart(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew import subagent_persistence
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership,
            "_lineage_unavailable_reason",
            self.real_lineage_reason,
            raising=False,
        )
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
        self._real_reader(monkeypatch, {"chat-1-1790000000": ""})
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda run_id: {"parent_session": "dashboard:chat-1-1790000000"},
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1-1790000000", "subagent:abc123"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        mgr, factory = self._manager("sid-1", tree)
        try:
            await mgr.get_or_create("dashboard:chat-1-1790000000", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs["shared_scratch"] == tree
        assert trees.quarantined_keys(tree) is None

    @pytest.mark.asyncio
    async def test_with_the_crew_log_off_a_chat_with_no_live_slot_still_refuses(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership,
            "_lineage_unavailable_reason",
            self.real_lineage_reason,
            raising=False,
        )
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
        self._real_reader(monkeypatch, {"chat-1-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = ("dashboard:chat-1-1790000000", "dashboard:chat-closed-1790000000")
        for key in keys:
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(keys[0], agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert trees.quarantined_keys(tree) == frozenset(keys)

    @pytest.mark.asyncio
    async def test_a_marked_owner_that_can_no_longer_be_placed_keeps_the_directory_refused(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """GPT's sequence: quarantine, cancel before rebind, prune the run record, resume A."""
        from kiro_crew import subagent_persistence
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership,
            "_lineage_unavailable_reason",
            self.real_lineage_reason,
            raising=False,
        )
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: False)
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        records = {"abc123": {"parent_session": "dashboard:chat-2-1790000000"}}
        monkeypatch.setattr(subagent_persistence, "read_state", lambda run_id: records.get(run_id))
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        (tree / "staged.txt").write_text("B's file")
        a, sub = "dashboard:chat-1-1790000000", "subagent:abc123"
        for key in (a, sub):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        # A's resume quarantines the directory; it is cancelled before A rebinds.
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None
        assert trees.quarantined_keys(tree) == frozenset({a, sub})
        records.clear()  # the reaper prunes the finished subagent's run record
        trees.abandon_tree(sub)
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None
        assert trees.quarantined_keys(tree) == frozenset({a, sub})
        assert (tree / "staged.txt").read_text() == "B's file"

    @pytest.mark.asyncio
    async def test_a_cancelled_retirement_with_a_failed_write_still_leaves_the_mark(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """GPT's sequence: the resume is cancelled while the index write fails."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        (tree / "staged.txt").write_text("A's file")
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        for key in (a, b):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        real_write = trees.atomic_write
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        def failing_write(path, *args, **kwargs):
            loop.call_soon_threadsafe(started.set)
            if not release.wait(30):  # a lost-run bound; the test releases it in finally
                raise OSError("the test never released the failing write")
            raise OSError("disk full")

        monkeypatch.setattr(trees, "atomic_write", failing_write)
        task = asyncio.ensure_future(
            scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock())
        )
        try:
            await await_until(started, what="the retirement's index write")
            task.cancel()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled resume to finish its shielded write")
        assert trees._QUARANTINE_LIVE[tree.name] == frozenset({a, b})  # held in memory
        monkeypatch.setattr(trees, "atomic_write", real_write)
        assert trees.quarantine_tree(tree, frozenset())  # the writes recover
        (scratch_root / "dashboard-chat-1-fresh002").mkdir()
        trees.record_tree(a, scratch_root / "dashboard-chat-1-fresh002")
        # A gateway restart: B is the only binding left on the directory.
        trees._TREE_INDEX_LIVE.clear()
        trees._QUARANTINE_LIVE.clear()
        monkeypatch.setattr(trees, "_TREE_INDEX_DISK_SNAPSHOT", None)
        assert await scratch_ownership._recorded_tree_of_own_session_tree(b, MagicMock()) is None
        assert (tree / "staged.txt").read_text() == "A's file"

    @pytest.mark.asyncio
    async def test_a_settled_lineage_of_one_tree_clears_the_mark_and_rejoins(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        lead, worker = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        trees.record_tree(worker, tree)
        trees.quarantine_tree(tree, frozenset({lead, worker}))
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", tree)
        try:
            await mgr.get_or_create(worker, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs["shared_scratch"] == tree
        assert trees.quarantined_keys(tree) is None

    @pytest.mark.asyncio
    async def test_a_settled_lineage_of_two_trees_retires_a_marked_directory(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        trees.record_tree(b, tree)
        trees.quarantine_tree(tree, frozenset({a, b}))
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(b, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert trees.keys_on_tree(tree) == frozenset()
        assert trees.quarantined_keys(tree) == frozenset({a, b})  # kept for later keys

    @pytest.mark.asyncio
    async def test_a_settled_lineage_of_two_trees_still_retires_the_directory(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create("dashboard:chat-1-1790000000", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert trees.keys_on_tree(tree) == frozenset()

    @pytest.mark.asyncio
    async def test_two_chats_with_no_live_slot_on_one_directory_are_refused(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A chat whose slot is not restored yet is its own tree, not unknown."""
        self._real_reader(monkeypatch, {})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create("dashboard:chat-1-1790000000", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        # No live slot: neither placement is verified, so quarantine, never retire.
        assert trees.keys_on_tree(tree) != frozenset()
        assert trees.quarantined_keys(tree) is not None

    @pytest.mark.asyncio
    async def test_two_slot_chats_of_different_trees_on_one_directory_are_refused(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create("dashboard:chat-2-1790000000", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert trees.keys_on_tree(tree) == frozenset()

    @pytest.mark.asyncio
    async def test_two_unrelated_resumes_at_once_neither_mounts_their_shared_directory(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000")
        for key in keys:
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        managers = [self._manager("sid-1", None) for _ in keys]
        try:
            await asyncio.gather(
                *(mgr.get_or_create(key, agent="kirocrew") for (mgr, _), key in zip(managers, keys))
            )
        finally:
            for mgr, _ in managers:
                await mgr.close_all()
        assert [f.call_args.kwargs.get("shared_scratch") for _, f in managers] == [None, None]

    @pytest.mark.asyncio
    async def test_a_resume_racing_the_refusal_of_its_directory_does_not_mount_it(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Retired between its two reads, a resume starts fresh instead of rebinding."""
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        real = trees.recorded_tree

        def recorded_then_refused(key: str):
            found = real(key)
            trees.abandon_tree_all(tree, frozenset())  # the other chat's resume refuses it now
            return found

        monkeypatch.setattr(trees, "recorded_tree", recorded_then_refused)
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create("dashboard:chat-2-1790000000", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None

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
    async def test_a_dropped_cwd_does_not_keep_its_project_profile(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The default directory starts without the dropped project's AWS profile."""
        _tree, work = self._hidden_cwd_in_recorded_tree(scratch_root, monkeypatch)
        mgr, factory = self._manager("sid-1", None)
        mgr._cfg.aws.project_profiles = {str(work): "project-account"}
        mgr._session_map.get_cwd = MagicMock(return_value=str(work))
        mgr._session_map.get_provider = MagicMock(return_value="another-harness")
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
            bound = mgr._sessions[mgr._fold_key("dashboard:chat-1")].aws_project_profile
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs["cwd"] is None
        assert "AWS_PROFILE" not in (factory.call_args.kwargs.get("extra_env") or {})
        assert bound is None

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

    @pytest.mark.asyncio
    async def test_a_failed_join_retry_preserves_focus_resume(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        trees.record_tree("dashboard:chat-1", tree)
        mgr, factory = self._manager("sid-1", None)
        make_provider = factory.side_effect
        starts = []
        monkeypatch.setattr(mgr, "_dispatch_hard_kill", MagicMock())

        def build(*args, **kwargs):
            provider = make_provider(*args, **kwargs)

            async def start():
                starts.append(provider.focus_resume)
                if kwargs.get("shared_scratch") is not None:
                    raise sc.SharedScratchJoinError("no join")

            provider.start = AsyncMock(side_effect=start)
            return provider

        factory.side_effect = build
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew", focus_resume=True)
        finally:
            await mgr.close_all()
        assert starts == [True, True]

    def _cwd_bound_manager(self, made: list):
        """A manager whose providers run a harness that serves a session only in its folder."""
        from kiro_crew.acp.types import ACP_BACKEND_OPENCODE
        from kiro_crew.providers.acp import AcpProvider, provider_label

        mgr, factory = self._manager("sid-1", None)

        def make(*args, **kwargs):
            provider = MagicMock(spec=AcpProvider)
            provider.start = AsyncMock()
            provider.shutdown = AsyncMock()
            provider.is_process_alive = MagicMock(return_value=True)
            provider.exit_code = None
            provider.cwd = kwargs.get("cwd") or ""
            provider.work_scratch_dir = None
            provider.client.backend = ACP_BACKEND_OPENCODE
            if kwargs.get("shared_scratch") is not None:
                provider.start = AsyncMock(side_effect=sc.SharedScratchJoinError("no join"))
            made.append((kwargs, provider))
            return provider

        factory.side_effect = make
        mgr._session_map.get_provider = MagicMock(return_value=provider_label(make(cwd="")))
        made.clear()
        return mgr

    @pytest.mark.asyncio
    async def test_a_failed_join_retry_does_not_load_a_cwd_bound_session_elsewhere(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The retry drops the cwd the recorded tree kept, so the saved session would move."""
        tree, work = self._hidden_cwd_in_recorded_tree(scratch_root, monkeypatch)
        made: list = []
        mgr = self._cwd_bound_manager(made)
        mgr._session_map.get_cwd = MagicMock(return_value=str(work))
        monkeypatch.setattr(mgr, "_dispatch_hard_kill", MagicMock())
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        joined = [p for kw, p in made if kw.get("shared_scratch") == tree]
        assert joined and joined[-1].cwd == str(work)
        joined[-1].client.set_resume_session_id.assert_called_once_with("sid-1")
        last_kwargs, last = made[-1]
        assert last_kwargs.get("shared_scratch") is None
        assert last_kwargs["cwd"] is None
        last.client.set_resume_session_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_recorded_tree_gone_on_reread_does_not_load_a_cwd_bound_session_elsewhere(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The second read finds no tree, so the cwd the first read kept is dropped."""
        _tree, work = self._hidden_cwd_in_recorded_tree(scratch_root, monkeypatch)
        real = trees.recorded_tree
        reads: list = []

        def recorded_once(key):
            reads.append(key)
            return real(key) if len(reads) == 1 else None

        monkeypatch.setattr(trees, "recorded_tree", recorded_once)
        made: list = []
        mgr = self._cwd_bound_manager(made)
        mgr._session_map.get_cwd = MagicMock(return_value=str(work))
        try:
            await mgr.get_or_create("dashboard:chat-1", agent="kirocrew")
        finally:
            await mgr.close_all()
        assert len(reads) >= 2
        last_kwargs, last = made[-1]
        assert last_kwargs.get("shared_scratch") is None
        assert last_kwargs["cwd"] is None
        last.client.set_resume_session_id.assert_not_called()

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
    async def test_failed_retirement_warns_and_starts_the_session(
        self, scratch_root: Path, monkeypatch, caplog, exposes_tree: bool
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
        service = mgr._allocation_boundary()
        service._deps = dataclasses.replace(
            service._deps, is_acp_provider=MagicMock(return_value=True)
        )
        monkeypatch.setattr(trees, "record_tree", MagicMock(return_value=False))
        monkeypatch.setattr(trees, "atomic_write", MagicMock(side_effect=OSError("full disk")))
        try:
            await mgr.get_or_create(key, agent="kirocrew")
            assert mgr.get_provider(key) is not None
            mgr._session_map.set.assert_called()
            assert any(
                record.levelname == "WARNING"
                and "Could not persist scratch retirement" in record.message
                and str(scratch_root / trees.TREE_INDEX_FILENAME) in record.message
                for record in caplog.records
            )
            # The row stays on disk, but this process never hands the tree back.
            assert trees._read_tree_index(scratch_root) == {key: tree.name}
            assert trees.recorded_tree(key) is None
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
    async def test_an_undroppable_earlier_row_warns_and_publishes_the_successor(
        self, scratch_root: Path, monkeypatch, caplog
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
        service = mgr._allocation_boundary()
        service._deps = dataclasses.replace(
            service._deps, is_acp_provider=MagicMock(return_value=True)
        )
        try:
            await mgr.get_or_create(key, agent="kirocrew")
            assert mgr.get_provider(key) is not None
            mgr._session_map.set.assert_called()
            assert any(
                record.levelname == "WARNING"
                and "Could not persist scratch retirement" in record.message
                for record in caplog.records
            )
            assert trees.recorded_tree(key) is None
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
