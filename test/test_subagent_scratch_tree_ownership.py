"""A resume never remounts a scratch directory another session tree recorded.

The ownership-resume scenarios of the scratch tree index, split out of
``test_subagent_scratch_tree_index.py`` for size; the shared harness (the settled
lineage fixture and the session manager builder) is that module's.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Pytest fixtures register in the module that imports them.
from test_subagent_scratch_tree_index import TestAResumedKeyRejoinsItsTree as _Harness
from test_subagent_scratch_tree_index import isolated_tree_bindings  # noqa: F401
from test_subagent_scratch_tree_index import scratch_root_fixture  # noqa: F401

from kiro_crew import agent_scratch_trees as trees
from kiro_crew.testing.wait import await_until


class TestAResumeNeverRemountsAnotherTreesDirectory:
    """A resumed key rejoins a recorded directory only when every owner is its own tree."""

    settled_lineage = _Harness.settled_lineage
    _manager = _Harness._manager

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
            assert trees.record_tree("subagent:worker", tree, parent=recorded)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        found = await scratch_ownership._recorded_tree_of_own_session_tree(
            "dashboard:chat-1-1790000000", MagicMock()
        )
        return found, tree

    @pytest.mark.asyncio
    async def test_a_failed_parent_write_before_a_prune_fails_closed(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Documents a declared residual: a same-tree subagent row with no parents
        row, a write of the parent that fails, then a prune and a restart. The
        parent's directory is quarantined, the parent starts fresh, the files stay
        on disk, and no key of another tree can mount the directory."""
        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import companions, scratch_ownership

        chat, worker = "dashboard:chat-1-1790000000", "subagent:worker"
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        monkeypatch.setattr(subagent_persistence, "read_state", lambda run_id: None)  # pruned
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        (tree / "staged.txt").write_text("chat-1's file")
        assert trees.record_tree(chat, tree)
        assert trees.record_tree(worker, tree)  # written before the parents row existed

        def failing_write(path, payload):
            raise OSError("index unwritable")

        real_write = trees.atomic_write
        monkeypatch.setattr(trees, "atomic_write", failing_write)
        assert not trees.record_tree(worker, tree, parent=chat)
        monkeypatch.setattr(trees, "atomic_write", real_write)
        assert trees.recorded_parent(worker) is None
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        found = await scratch_ownership._recorded_tree_of_own_session_tree(chat, MagicMock())
        assert found is None  # the parent starts in fresh scratch
        assert trees.quarantined_keys(tree) is not None
        assert (tree / "staged.txt").read_text() == "chat-1's file"
        assert trees.refused_across_trees(tree)
        assert await companions.admit_shared_scratch(tree) is None

    @pytest.mark.asyncio
    async def test_a_resume_backfills_the_parent_so_a_later_prune_still_rejoins(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A binding written with no parent row (as before this change) keeps its
        placement: the first resume reads the parent off the live run record and
        writes the row, so the resume after the record is pruned still rejoins."""
        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import scratch_ownership

        chat = "dashboard:chat-1-1790000000"
        self._real_reader(monkeypatch, {"chat-1-1790000000": ""})
        records: dict[str, dict] = {"worker": {"parent_session": chat}}
        monkeypatch.setattr(subagent_persistence, "read_state", records.get)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in (chat, "subagent:worker"):
            trees.record_tree(key, tree)
        assert trees.recorded_parent("subagent:worker") is None
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        found = await scratch_ownership._recorded_tree_of_own_session_tree(chat, MagicMock())
        assert found == tree
        records.clear()  # the run record is pruned
        trees._TREE_INDEX_LIVE.clear()
        found = await scratch_ownership._recorded_tree_of_own_session_tree(chat, MagicMock())
        assert found == tree
        assert trees.quarantined_keys(tree) is None
        assert trees.recorded_parent("subagent:worker") == chat

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
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, former=None: False)
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
        # A's move off the shared directory owed a mark nothing could write, so A's
        # row still names it: both owners stay bound, and B is refused again.
        rows = json.loads((scratch_root / trees.TREE_INDEX_FILENAME).read_text())
        assert rows[a] == tree.name
        assert trees.keys_on_tree(tree) >= {a, b}

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
        monkeypatch.setattr(trees, "atomic_write", real_write)
        # A gateway restart: only what is on disk remains, and the failed write
        # changed none of it.
        trees._TREE_INDEX_LIVE.clear()
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
        assert trees.keys_on_tree(tree) >= {a, b}  # the failed write changed nothing
        monkeypatch.setattr(trees, "atomic_write", real_write)
        (scratch_root / "dashboard-chat-1-fresh002").mkdir()
        # A moves away: the move leaves {A, B} as the directory's mark.
        assert trees.record_tree(a, scratch_root / "dashboard-chat-1-fresh002")
        # A gateway restart: B is the only binding left on the directory.
        trees._TREE_INDEX_LIVE.clear()
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

    def _persisted_lineage(
        self, monkeypatch, parents: dict[str, str], *, incomplete=False, suspect_sid=""
    ) -> None:
        """A seeded crew-log session tree holding *parents*, read as the gateway reads it."""
        from kiro_crew import crew_mode
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log import session_tree_projection as stp
        from kiro_crew.crew_log.session_tree import OpenedRecord, TreeNode

        class _Seeded(stp.SessionTreeProjection):
            seeded_for_current_store = True
            seed_retry_due = False

        tree = _Seeded()
        tree._nodes = {child: TreeNode(child, parent, False) for child, parent in parents.items()}
        tree._incomplete = incomplete
        if suspect_sid:
            slot = suspect_sid.split("@", 1)[0]
            tree._records[suspect_sid] = MagicMock(spec=OpenedRecord, sid=suspect_sid, slot=slot)
            tree._suspect_sids.add(suspect_sid)
        monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
        monkeypatch.setattr(stp, "projection", lambda: tree)
        # The other readers of the lineage see the same edges.
        monkeypatch.setattr(crew_mode, "persisted_parents", lambda: dict(parents))
        return tree

    @pytest.mark.parametrize(
        ("incomplete", "suspect_sid", "verified"),
        [
            (True, "", False),  # a seed that missed a release unit
            (False, "chat-2-1790000000@u1", False),  # the closed child's unit is suspect
            (False, "chat-1-1790000000@u1", False),  # the live parent's unit is suspect
            (False, "", True),  # complete and clean: the persisted edge certifies
        ],
    )
    def test_a_persisted_hop_verifies_only_on_a_complete_unsuspect_reading(
        self, monkeypatch, incomplete: bool, suspect_sid: str, verified: bool
    ) -> None:
        from types import SimpleNamespace

        from kiro_crew import crew_mode

        lead, closed = "chat-1-1790000000", "chat-2-1790000000"
        self._persisted_lineage(
            monkeypatch, {closed: lead}, incomplete=incomplete, suspect_sid=suspect_sid
        )
        slots = {lead: SimpleNamespace(key=lead, _created_by="", _saved_created_by="", agent="x")}
        assert crew_mode.share_tree_placement(slots, closed) == (lead, verified)

    def test_an_edge_a_release_removed_is_never_certified_by_a_later_clean_reading(
        self, monkeypatch
    ) -> None:
        """The edge and its verdict come from one snapshot. Before, the walk took B->A
        from one read and a clean verdict from a later one, after B's release."""
        from types import SimpleNamespace

        from kiro_crew import crew_mode

        lead, closed = "chat-1-1790000000", "chat-2-1790000000"
        self._persisted_lineage(monkeypatch, {})  # complete, clean, B released
        monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {closed: lead})  # stale
        slots = {lead: SimpleNamespace(key=lead, _created_by="", _saved_created_by="", agent="x")}
        assert crew_mode.share_tree_placement(slots, closed) == (closed, False)

    def test_a_frame_edge_the_projection_no_longer_holds_is_unverified(self, monkeypatch) -> None:
        from types import SimpleNamespace

        from kiro_crew import crew_mode

        lead, worker = "chat-1-1790000000", "chat-2-1790000000"
        self._persisted_lineage(monkeypatch, {})  # complete and clean, but no B->A edge now
        slots = {
            key: SimpleNamespace(key=key, _created_by="", _saved_created_by="", agent="x")
            for key in (lead, worker)
        }
        frame = {worker: {"slot": lead}, lead: None}
        assert crew_mode.share_tree_placement(slots, worker, frame, settled=True) == (lead, False)

    def test_the_lineage_walk_reads_edges_only_through_the_one_snapshot(self) -> None:
        """Guard: no separate parent map and trust verdict to pair again."""
        import inspect

        from kiro_crew import crew_mode
        from kiro_crew.crew_log import session_tree_projection as stp

        source = inspect.getsource(crew_mode.share_tree_placement)
        assert "persisted_parents" not in source
        assert not hasattr(crew_mode, "persisted_lineage_trust")
        assert not hasattr(crew_mode, "persisted_edge")  # no per-hop read to mix generations
        assert not hasattr(stp.SessionTreeProjection, "edge_trust")
        assert not hasattr(stp.SessionTreeProjection, "persisted_edge")

    @pytest.mark.asyncio
    async def test_an_incomplete_persisted_lineage_quarantines_instead_of_rejoining(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A closed B placed under live A only by an incomplete seed never hands A B's files."""
        lead, closed = "chat-1-1790000000", "chat-2-1790000000"
        self._real_reader(monkeypatch, {lead: ""})
        self._persisted_lineage(monkeypatch, {closed: lead}, incomplete=True)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        a, b = f"dashboard:{lead}", f"dashboard:{closed}"
        for key in (a, b):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(a, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None
        assert trees.quarantined_keys(tree) == frozenset({a, b})

    @pytest.mark.parametrize(
        ("incomplete", "suspect_sid", "verified"),
        [
            (True, "", False),  # the frame carries an edge a lost release would have moved
            (False, "chat-2-1790000000@u1", False),  # the child's unit is suspect
            (False, "chat-1-1790000000@u1", False),  # the parent's unit is suspect
            (False, "", True),  # complete and clean: the frame edge certifies
        ],
    )
    def test_a_frame_edge_verifies_only_on_a_complete_unsuspect_reading(
        self, monkeypatch, incomplete: bool, suspect_sid: str, verified: bool
    ) -> None:
        from types import SimpleNamespace

        from kiro_crew import crew_mode

        lead, worker = "chat-1-1790000000", "chat-2-1790000000"
        self._persisted_lineage(
            monkeypatch, {worker: lead}, incomplete=incomplete, suspect_sid=suspect_sid
        )
        slots = {
            key: SimpleNamespace(key=key, _created_by="", _saved_created_by="", agent="x")
            for key in (lead, worker)
        }
        frame = {worker: {"slot": lead}, lead: None}
        placement = crew_mode.share_tree_placement(slots, worker, frame, settled=True)
        assert placement == (lead, verified)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("incomplete", "shared"), [(True, False), (False, True)], ids=["incomplete", "complete"]
    )
    async def test_a_stale_frame_edge_never_shares_two_live_chats_a_directory(
        self, scratch_root: Path, monkeypatch, incomplete: bool, shared: bool
    ) -> None:
        """Live A and B, joined only by a frame edge an incomplete reading kept."""
        from kiro_crew.dashboard.session_control_parts import crew_filing

        lead, worker = "chat-1-1790000000", "chat-2-1790000000"
        self._real_reader(monkeypatch, {lead: "", worker: ""})
        self._persisted_lineage(monkeypatch, {worker: lead}, incomplete=incomplete)
        monkeypatch.setattr(
            crew_filing, "_frame_parents", lambda state: {worker: {"slot": lead}, lead: None}
        )
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        a, b = f"dashboard:{lead}", f"dashboard:{worker}"
        for key in (a, b):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(a, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert (factory.call_args.kwargs.get("shared_scratch") == tree) is shared
        if not shared:
            assert trees.quarantined_keys(tree) == frozenset({a, b})

    # -- a key leaving a shared directory leaves its owner set behind ------------------

    def _shared_with_two_owners(self, scratch_root: Path) -> tuple[Path, Path, str, str]:
        scratch_root.mkdir(parents=True, exist_ok=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        fresh = scratch_root / "dashboard-chat-2-fresh002"
        tree.mkdir()
        fresh.mkdir()
        (tree / "staged.txt").write_text("B's file")
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        for key in (a, b):
            trees.record_tree(key, tree)
        return tree, fresh, a, b

    @pytest.mark.asyncio
    @pytest.mark.parametrize("released", [True, False])
    async def test_a_mount_counts_the_owners_a_settle_kept_as_provenance(
        self, scratch_root: Path, monkeypatch, released: bool
    ) -> None:
        """After a settle clears D's former-owner row, a mount still weighs the owners
        it kept: refused once B released into its own tree, allowed while B stays,
        and the provenance row is never cleared by a passing check."""
        from kiro_crew.session_allocation_parts import companions

        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)
        assert await trees.resolve_open_ownership(tree)  # one tree: the former row clears
        assert trees.former_owners(tree) is None
        if released:
            self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
            assert await companions.admit_shared_scratch(tree) is None
            assert trees.refused_across_trees(tree)
        else:
            assert await companions.admit_shared_scratch(tree) == tree
            assert {a, b} <= trees._read_owners(scratch_root)["provenance"][tree.name]

    @pytest.mark.asyncio
    async def test_a_sole_owner_row_with_no_provenance_still_mounts(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """An owner row for a binding the index could not take, one tree only."""
        from kiro_crew.session_allocation_parts import companions

        self._real_reader(monkeypatch, {"chat-1-1790000000": ""})
        scratch_root.mkdir(parents=True)
        d = scratch_root / "dashboard-chat-1-abcdef01"
        d.mkdir()
        assert trees.record_owner_marker("dashboard:chat-1-1790000000", d)
        assert await companions.admit_shared_scratch(d) == d

    @pytest.mark.asyncio
    async def test_a_settled_former_owner_still_counts_after_it_leaves_the_tree(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """B left A's directory while both were one tree, so A's check cleared the row.
        B then released into a tree of its own: after a restart A must not remount
        the directory B staged files in."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)  # B switches providers: D keeps {A, B} as former
        assert trees.former_owners(tree) is not None
        assert await trees.resolve_open_ownership(tree)  # one tree: the row clears
        assert trees.former_owners(tree) is None
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        trees._TREE_INDEX_LIVE.clear()  # B released into its own tree; a restart
        trees._REFUSED_UNPERSISTED.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shared", [True, False])
    async def test_a_join_the_binding_cache_refuses_is_still_recorded_or_refused(
        self, scratch_root: Path, monkeypatch, shared: bool
    ) -> None:
        """The in-memory binding cache is full, so B's binding is refused. In A's
        shared directory B is recorded by an owner row (or the join fails); a sole
        owner is recorded too, so a key that joins later and a restart still count it."""
        from types import SimpleNamespace

        from kiro_crew.session_allocation_parts import get_or_create

        scratch_root.mkdir(parents=True)
        d = scratch_root / "dashboard-chat-1-abcdef01"
        other = scratch_root / "dashboard-chat-9-abcdef09"
        d.mkdir()
        other.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        assert trees.record_tree(
            a if shared else "dashboard:chat-9-1790000000", d if shared else other
        )
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 1)  # the cache is full
        owner = SimpleNamespace(_deps=SimpleNamespace(logger=MagicMock()))
        result = await get_or_create._record_scratch_tree(owner, b, d)
        if shared:
            assert result == "retire" and b in trees.keys_on_tree(d)
            monkeypatch.setattr(trees, "_write_owner_row", lambda *args: False)
            c = "dashboard:chat-3-1790000000"
            assert await get_or_create._record_scratch_tree(owner, c, d) == "unrecorded"
        else:
            assert result == "retire"
            monkeypatch.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 4096)
            c = "dashboard:chat-3-1790000000"
            assert await get_or_create._record_scratch_tree(owner, c, d) == "kept"
            trees._TREE_INDEX_LIVE.clear()  # a restart
            assert b in trees.keys_on_tree(d)

    @pytest.mark.asyncio
    async def test_a_sole_owner_the_cache_refuses_starts_when_its_row_cannot_be_written(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """No other key owns D, the cache refuses B and the owner row cannot be
        written: B still starts (retire and continue), with the warning logged."""
        from types import SimpleNamespace

        from kiro_crew.session_allocation_parts import get_or_create

        scratch_root.mkdir(parents=True)
        d = scratch_root / "dashboard-chat-2-abcdef02"
        d.mkdir()
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_ENTRIES", 0)  # the cache is full
        monkeypatch.setattr(trees, "_write_owner_row", lambda *args: False)
        owner = SimpleNamespace(_deps=SimpleNamespace(logger=MagicMock()))
        b = "dashboard:chat-2-1790000000"
        assert await get_or_create._record_scratch_tree(owner, b, d) == "retire"
        owner._deps.logger.warning.assert_called()

    @pytest.mark.asyncio
    async def test_a_join_records_a_founder_whose_own_write_failed(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A owns D alone but its index write failed; B joins D and persists. After B
        releases into its own tree and the gateway restarts, B must still see A."""
        from types import SimpleNamespace

        from kiro_crew.session_allocation_parts import get_or_create, scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        d = scratch_root / "dashboard-chat-1-abcdef01"
        d.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        trees._TREE_INDEX_LIVE[a] = d  # A's binding; its index write failed
        owner = SimpleNamespace(_deps=SimpleNamespace(logger=MagicMock()))
        assert await get_or_create._record_scratch_tree(owner, b, d) == "kept"
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        trees._REFUSED_UNPERSISTED.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(b, MagicMock()) is None

    @pytest.mark.asyncio
    async def test_a_rebind_off_a_shared_directory_leaves_the_owners_as_its_mark(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """B moving to fresh scratch leaves {A, B} on D, so A alone never rejoins D."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)
        assert trees.former_owners(tree) >= {a, b}
        assert trees.quarantined_keys(tree) is None  # an owner set, not a refusal
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None
        assert (tree / "staged.txt").read_text() == "B's file"

    @pytest.mark.asyncio
    async def test_a_rebind_inside_one_tree_still_lets_the_lead_rejoin(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """No over-refusal: a mark whose owners are one tree settles and the lead rejoins."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) == tree
        assert trees.quarantined_keys(tree) is None

    def test_a_mark_that_cannot_be_written_keeps_the_leaving_row(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)

        def refuse(root, marks, former=None):
            raise OSError("marks file unwritable")

        monkeypatch.setattr(trees, "_write_marks", refuse)
        assert trees.record_tree(b, fresh) is False
        rows = json.loads((scratch_root / trees.TREE_INDEX_FILENAME).read_text())
        assert rows[b] == tree.name
        assert trees.keys_on_tree(tree) >= {a, b}

    def test_a_rebind_off_a_directory_owned_alone_writes_no_mark(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        fresh = scratch_root / "dashboard-chat-1-fresh002"
        tree.mkdir()
        fresh.mkdir()
        trees.record_tree("dashboard:chat-1", tree)
        assert trees.record_tree("dashboard:chat-1", fresh)
        assert trees.quarantined_keys(tree) is None

    @pytest.mark.asyncio
    async def test_a_provider_switch_start_never_hands_its_directory_to_the_chat_left(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """B starts after a provider switch (no ownership check) and binds fresh scratch;
        A then resumes and is handed a fresh directory, never D."""
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        mgr, factory = self._manager("sid-1", fresh)
        mgr._session_map.get_provider = MagicMock(return_value="another-harness")
        try:
            await mgr.get_or_create(b, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert trees.recorded_tree(b) == fresh
        mgr, factory = self._manager("sid-1", None)
        try:
            await mgr.get_or_create(a, agent="kirocrew")
        finally:
            await mgr.close_all()
        assert factory.call_args.kwargs.get("shared_scratch") is None

    @pytest.mark.parametrize("writer", ["abandon_tree", "drop_other_row"])
    def test_every_single_row_writer_leaves_the_owners_as_the_mark(
        self, scratch_root: Path, writer: str
    ) -> None:
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        if writer == "abandon_tree":
            assert trees.abandon_tree(b)
        else:
            assert trees.drop_other_row(b, fresh)
        assert trees.former_owners(tree) >= {a, b}
        assert trees.quarantined_keys(tree) is None  # an owner set, not a refusal

    # -- a same-tree move leaves owners, never a refusal ---------------------------------

    @pytest.mark.asyncio
    async def test_a_same_tree_move_never_cuts_the_live_co_owner_off_its_directory(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A and B share D in one tree; B moves to D2 (a provider switch). A's next
        subagent is still handed D, a respawn still mounts it, and A's resume rejoins it."""
        from kiro_crew.session_allocation_parts import companions, scratch_ownership

        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)
        assert await companions.admit_shared_scratch(tree) == tree  # A's next subagent
        assert not trees.refused_across_trees(tree)  # the respawn's mount check
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) == tree

    @pytest.mark.asyncio
    async def test_a_cross_tree_move_still_refuses_the_directory_to_the_owner_left(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The owners a move leaves behind still count at the resume: two trees refuse."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None
        assert trees.refused_across_trees(tree)  # now a refusal, written by the resume

    def test_every_re_entry_of_get_or_create_carries_the_scratch_parent(self) -> None:
        """``scratch_parent`` is popped before the claim, like ``fallback_hidden_cwd``; each
        re-entry that hands the popped kwargs back must hand it back too, or a subagent
        cold start reached through the re-entry records no parent row."""
        import ast
        import inspect

        from kiro_crew.session_allocation_parts import get_or_create

        tree = ast.parse(inspect.getsource(get_or_create))
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_or_create"
            and any(k.arg == "fallback_hidden_cwd" for k in node.keywords)
        ]
        assert calls  # the re-entries this pins exist
        missing = [
            c.lineno for c in calls if not any(k.arg == "scratch_parent" for k in c.keywords)
        ]
        assert missing == []

    def test_reading_one_edge_does_not_walk_every_record(self) -> None:
        """The lineage snapshot is taken per decision on the event loop: its cost must
        not grow with the store. A record map that counts its walks stays at zero."""
        from types import SimpleNamespace

        from kiro_crew.crew_log import session_tree_projection as stp
        from kiro_crew.crew_log.session_tree import TreeNode

        class _Counting(dict):
            walks = 0

            def items(self):
                type(self).walks += 1
                return super().items()

            def values(self):
                type(self).walks += 1
                return super().values()

        tree = stp.SessionTreeProjection()
        tree._records = _Counting({f"s{n}": SimpleNamespace(slot=f"c{n}") for n in range(1000)})
        tree._nodes = {"c5": TreeNode("c5", "lead", False)}
        tree._suspect_sids = {"s7"}
        snap = tree.edge_snapshot()
        assert snap.edge("c5") == ("lead", True)
        assert snap.edge("c7") == ("", False)  # its own unit is suspect
        assert tree.snapshot_is_current(snap)
        assert _Counting.walks == 0

    # -- one walk, one generation ---------------------------------------------------------

    def _moving_lineage(
        self, monkeypatch, before: dict[str, str], after: dict[str, str], keep_moving=False
    ):
        """A seeded tree holding *before*, which a crew-log writer moves to *after* just
        after a reader snapshots it (every time with *keep_moving*): the commit lands
        between the snapshot and the decision."""
        from kiro_crew.crew_log.session_tree import TreeNode

        tree = self._persisted_lineage(monkeypatch, before)
        moved = []

        def commit() -> None:
            if not moved or keep_moving:
                moved.append(True)
                tree._nodes = {c: TreeNode(c, p, False) for c, p in after.items()}

        # Every locked read the lineage offers commits the writer right after it, so
        # the same injection reaches a walk that reads once and one that reads per hop.
        for name in ("edge_snapshot", "persisted_edge"):
            real = getattr(type(tree), name, None)
            if real is None:
                continue

            def read_then_commit(self, *args, _real=real):
                result = _real(self, *args)
                commit()
                return result

            monkeypatch.setattr(type(tree), name, read_then_commit)
        return tree, commit

    def test_a_walk_never_combines_edges_from_two_generations(self, monkeypatch) -> None:
        """B->A in one generation, A->C in the next: neither placed B under C, so the
        walk must never return C verified."""
        from types import SimpleNamespace

        from kiro_crew import crew_mode

        a, b, c = "chat-a-1790000000", "chat-b-1790000000", "chat-c-1790000000"
        self._moving_lineage(monkeypatch, {b: a}, {a: c})
        slots = {c: SimpleNamespace(key=c, _created_by="", _saved_created_by="", agent="x")}
        assert crew_mode.share_tree_placement(slots, b) != (c, True)

    @pytest.mark.asyncio
    async def test_owners_placed_while_the_lineage_moves_are_unknown_ownership(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The lineage moves after every reading the resume takes: no reading is ever
        current, so no root is trusted and the resume quarantines the directory."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        lead, worker = "chat-1-1790000000", "chat-2-1790000000"
        self._real_reader(monkeypatch, {lead: "", worker: lead})
        self._moving_lineage(monkeypatch, {worker: lead}, {worker: lead}, keep_moving=True)
        tree, _fresh, a, b = self._shared_with_two_owners(scratch_root)
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None
        assert trees.quarantined_keys(tree) == frozenset({a, b})

    @pytest.mark.asyncio
    async def test_a_lineage_that_moved_once_is_decided_on_a_settled_reading(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """One commit during the first reading: that reading counts for nothing, and the
        reload's own, current reading places both owners in one tree."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        lead, worker = "chat-1-1790000000", "chat-2-1790000000"
        self._real_reader(monkeypatch, {lead: "", worker: lead})
        self._moving_lineage(monkeypatch, {worker: lead}, {worker: lead})
        tree, _fresh, a, _b = self._shared_with_two_owners(scratch_root)
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) == tree

    def test_a_dedicated_subagent_names_its_parent_whatever_the_parents_scratch(self) -> None:
        """The spawn parent goes with the binding even when the parent's directory is
        unavailable or refused, or a later resume could not place the subagent."""
        import ast
        import inspect

        from kiro_crew.subagent_manager import run_turn

        tree = ast.parse(inspect.getsource(run_turn))
        nested = []
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and "shared_scratch is not None" in ast.unparse(node.test):
                nested += [
                    n
                    for n in ast.walk(node)
                    if isinstance(n, ast.Subscript) and "scratch_parent" in ast.unparse(n)
                ]
        assigned = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign) and "'scratch_parent'" in ast.unparse(n.targets[0])
        ]
        assert assigned and not nested

    # -- a refusal held in this process settles like a mark --------------------------------

    @staticmethod
    def _unpersisted_admission(monkeypatch, tree: Path):
        """A record_tree whose index write fails after it bound *tree* live."""
        real = trees.record_tree

        def failing_record(key, path, **kwargs):
            trees._TREE_INDEX_LIVE[key] = path
            return False

        monkeypatch.setattr(trees, "record_tree", failing_record)
        return real

    @pytest.mark.asyncio
    async def test_an_unpersisted_co_owner_survives_a_restart_and_refuses_the_remount(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """B's binding to A's directory never reached the index; after a restart A
        must still see B as an owner and never remount the directory alone."""
        from types import SimpleNamespace

        from kiro_crew.session_allocation_parts import get_or_create, scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        d = scratch_root / "dashboard-chat-1-abcdef01"
        d.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        assert trees.record_tree(a, d)
        real = self._unpersisted_admission(monkeypatch, d)
        owner = SimpleNamespace(_deps=SimpleNamespace(logger=MagicMock()))
        assert await get_or_create._record_scratch_tree(owner, b, d) == "kept"  # B admitted
        monkeypatch.setattr(trees, "record_tree", real)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart: B's live binding is gone
        trees._REFUSED_UNPERSISTED.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None

    @pytest.mark.asyncio
    async def test_a_binding_whose_owner_record_cannot_be_written_is_not_admitted(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """B joins A's directory, but neither the index nor a marker can record it:
        the binding is refused, so B starts fresh."""
        from types import SimpleNamespace

        from kiro_crew.session_allocation_parts import get_or_create

        scratch_root.mkdir(parents=True)
        d = scratch_root / "dashboard-chat-1-abcdef01"
        d.mkdir()
        assert trees.record_tree("dashboard:chat-1-1790000000", d)  # A owns D
        self._unpersisted_admission(monkeypatch, d)
        monkeypatch.setattr(trees, "_write_owner_row", lambda *args: False, raising=False)
        owner = SimpleNamespace(_deps=SimpleNamespace(logger=MagicMock()))
        result = await get_or_create._record_scratch_tree(owner, "dashboard:chat-2-1790000000", d)
        assert result == "unrecorded"

    @pytest.mark.asyncio
    async def test_a_retirement_at_the_cap_still_refuses_after_a_restart(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The retirement's mark does not fit the index; a sentinel inside the
        directory refuses it after a restart until its owners settle."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        d = scratch_root / "dashboard-chat-1-abcdef01"
        d.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        assert trees.record_tree(a, d)
        assert trees.record_tree(b, d)
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", 10)
        assert not trees.abandon_tree_all(d, frozenset({a, b}))
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", 1 << 20)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        trees._REFUSED_UNPERSISTED.clear()
        assert trees.refused_across_trees(d)
        assert trees.quarantined_keys(d) == frozenset({a, b})
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None
        assert trees.clear_quarantine(d, frozenset({a, b}))
        assert not trees.refused_across_trees(d)

    @pytest.mark.asyncio
    async def test_a_key_leaving_a_directory_held_only_in_memory_keeps_its_owners(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A retirement's mark did not fit, so A and B are held only in memory. A's
        row then leaves D: the held owners count, so D keeps its former owners and
        a chat bound later is never D's sole owner after a restart."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(
            monkeypatch,
            {"chat-1-1790000000": "", "chat-2-1790000000": "", "chat-3-1790000000": ""},
        )
        scratch_root.mkdir(parents=True)
        d = scratch_root / "dashboard-chat-1-abcdef01"
        fresh = scratch_root / "dashboard-chat-1-abcdef0f"
        d.mkdir()
        fresh.mkdir()
        (d / "staged.txt").write_text("B's file")
        a, b, c = (f"dashboard:chat-{n}-1790000000" for n in (1, 2, 3))
        assert trees.record_tree(a, d)
        trees._TREE_INDEX_LIVE[b] = d  # B is bound live only
        real_write = trees.atomic_write

        def failing_write(path, payload):
            raise OSError("the index is at its cap")

        monkeypatch.setattr(trees, "atomic_write", failing_write)
        assert not scratch_ownership._retire_or_mark(d, frozenset({a, b}))
        monkeypatch.setattr(trees, "atomic_write", real_write)
        assert trees.held_refusal(d) == frozenset({a, b})
        trees._TREE_INDEX_LIVE[a] = fresh  # A's persist of its fresh directory failed
        trees.drop_other_row(a, fresh)
        assert trees._read_tree_index(scratch_root).get(a) == d.name or trees.former_owners(d)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        trees._REFUSED_UNPERSISTED.clear()
        assert trees.record_tree(c, d)
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(c, MagicMock()) is None

    async def _resume_while_overflowed(self, scratch_root: Path, monkeypatch) -> tuple[str, Path]:
        """One chat's directory, resumed while the held refusals have overflowed."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        chat = "dashboard:chat-1-1790000000"
        self._real_reader(monkeypatch, {"chat-1-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        assert trees.record_tree(chat, tree)
        monkeypatch.setattr(trees, "_REFUSALS_OVERFLOWED", True)
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(chat, MagicMock()) is None
        return chat, tree

    @pytest.mark.asyncio
    async def test_an_overflow_refuses_in_process_and_writes_no_overflow_owner(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The overflow refuses the mount in this process; its owner never reaches disk."""
        _chat, tree = await self._resume_while_overflowed(scratch_root, monkeypatch)
        assert trees.refused_across_trees(tree)
        marked = trees.quarantined_keys(tree) or frozenset()
        assert not any("\x00" in key for key in marked)

    @pytest.mark.asyncio
    async def test_an_overflow_tells_the_log_the_files_are_untouched(
        self, scratch_root: Path, monkeypatch, caplog
    ) -> None:
        """Tripping the overflow and each resume it sends to fresh scratch log a
        warning naming the directory and saying its staged files are untouched."""
        import logging

        from kiro_crew.session_allocation_parts import scratch_ownership

        chat = "dashboard:chat-1-1790000000"
        self._real_reader(monkeypatch, {"chat-1-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        assert trees.record_tree(chat, tree)
        monkeypatch.setattr(trees, "_REFUSED_MAX_ENTRIES", 0)
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, former=None: False)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent_scratch_trees"):
            trees.quarantine_tree(scratch_root / "dashboard-chat-9-abcdef09", frozenset({"k"}))
        assert trees._REFUSALS_OVERFLOWED
        assert any("untouched on disk" in r.getMessage() for r in caplog.records)
        trees._TREE_INDEX_LIVE.clear()
        log = MagicMock()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(chat, log) is None
        warned = [c.args for c in log.warning.call_args_list]
        assert any("untouched on disk" in a[0] and tree.name in a for a in warned)

    @pytest.mark.asyncio
    async def test_after_the_overflow_is_gone_the_sole_owner_settles(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A restart drops the overflow, and the directory settles for its one owner."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        chat, tree = await self._resume_while_overflowed(scratch_root, monkeypatch)
        monkeypatch.setattr(trees, "_REFUSALS_OVERFLOWED", False)  # a restart
        trees._REFUSED_UNPERSISTED.clear()
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(chat, MagicMock()) == tree

    @pytest.mark.asyncio
    async def test_a_held_refusal_clears_once_its_owners_settle_to_one_tree(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The mark could not be written, so only this process refuses D. When the
        lineage settles to one tree, the next resume clears the refusal and rejoins D."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        tree, _fresh, a, b = self._shared_with_two_owners(scratch_root)
        real_write = trees._write_marks
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, former=None: False)
        assert not trees.quarantine_tree(tree, frozenset({a, b}))
        assert trees.refused_across_trees(tree)
        monkeypatch.setattr(trees, "_write_marks", real_write)
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) == tree
        assert not trees.refused_across_trees(tree)

    @pytest.mark.asyncio
    async def test_a_held_refusal_with_an_owner_nobody_checked_stays(
        self, scratch_root: Path, monkeypatch
    ) -> None:

        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        tree, _fresh, a, b = self._shared_with_two_owners(scratch_root)
        real_write = trees._write_marks
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, former=None: False)
        assert not trees.quarantine_tree(tree, frozenset({a, b}))
        monkeypatch.setattr(trees, "_write_marks", real_write)
        assert not trees.clear_quarantine(tree, frozenset({a}))  # b was never checked
        assert trees.refused_across_trees(tree)

    # -- the mount seams answer an open ownership question ------------------------------

    async def _respawn_check(self, tree: Path) -> bool:
        """What launch() does for a respawn: resolve, then read the refusal. Without
        a resolver the seam reads the refusal alone, as it did before one existed."""
        resolve = getattr(trees, "resolve_open_ownership", None)
        if resolve is not None:
            await resolve(tree)
        return trees.refused_across_trees(tree)

    @pytest.mark.asyncio
    async def test_a_cross_tree_former_owner_refuses_a_respawn_before_any_resume(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.session_allocation_parts import companions

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)  # B leaves; only the former row records it
        assert await self._respawn_check(tree)  # A's respawn
        assert await companions.admit_shared_scratch(tree) is None  # A's next subagent
        assert trees.quarantined_keys(tree) is not None

    @pytest.mark.asyncio
    async def test_a_same_tree_former_owner_lets_the_respawn_mount_and_clears_the_row(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)
        assert trees.former_owners(tree) is not None
        assert not await self._respawn_check(tree)
        assert trees.former_owners(tree) is None  # the resolver settled it

    @pytest.mark.asyncio
    async def test_a_key_leaving_a_live_tree_its_disk_row_does_not_name_marks_that_tree(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """B's write moving it to A's D2 failed, so B's disk row still names D1 while
        B is live on D2. Retiring B must leave D2 its former owners, so A, of
        another tree, never resumes into D2 as its sole owner."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        d1 = scratch_root / "dashboard-chat-2-abcdef02"
        d2 = scratch_root / "dashboard-chat-1-abcdef01"
        d1.mkdir()
        d2.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        assert trees.record_tree(a, d2)
        assert trees.record_tree(b, d1)
        trees._TREE_INDEX_LIVE[b] = d2  # B -> D2 bound live; its index write failed
        trees.abandon_tree(b)
        assert trees.former_owners(d2) == frozenset({a, b}) or trees.held_refusal(d2)
        trees._TREE_INDEX_LIVE.clear()  # A's next resume
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None

    @pytest.mark.asyncio
    async def test_a_former_owner_recorded_during_the_settlement_refuses_both_seams(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A key from another tree leaves the directory after the resolver read the
        owners: the clear refuses, writes no mark, and neither seam mounts it."""
        from kiro_crew.session_allocation_parts import companions

        self._real_reader(
            monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": "chat-1-1790000000"}
        )
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)  # B leaves: one tree, so the row would clear
        late = "dashboard:chat-9-1790000000"
        other = scratch_root / "dashboard-chat-9-abcdef09"
        other.mkdir()
        real_clear = trees.clear_quarantine

        def clear_after_a_late_departure(tree_arg, checked):
            assert trees.record_tree(late, tree_arg) and trees.record_tree(late, other)
            monkeypatch.setattr(trees, "clear_quarantine", real_clear)
            return real_clear(tree_arg, checked)

        monkeypatch.setattr(trees, "clear_quarantine", clear_after_a_late_departure)
        assert not await trees.resolve_open_ownership(tree)  # A's respawn
        assert trees.refused_across_trees(tree)
        assert await companions.admit_shared_scratch(tree) is None  # A's next subagent

    @pytest.mark.asyncio
    async def test_an_unreadable_former_row_refuses_the_respawn(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        assert trees.record_tree(b, fresh)

        def unreadable(root):
            raise OSError("the former row does not parse")

        monkeypatch.setattr(trees, "_read_former", unreadable)
        assert await self._respawn_check(tree)

    @pytest.mark.asyncio
    async def test_a_lineage_write_during_the_disk_reads_never_quarantines_one_tree(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A chat and its own dedicated subagent share T. An unrelated chat's
        ``session/opened`` lands while the subagent's run record is read off the loop:
        the disk reads happen before the lineage snapshot, so the resume still mounts T."""
        from kiro_crew import subagent_persistence
        from kiro_crew.crew_log.session_tree import TreeNode
        from kiro_crew.session_allocation_parts import scratch_ownership

        lead = "chat-1-1790000000"
        self._real_reader(monkeypatch, {lead: ""})
        tree_state = self._persisted_lineage(monkeypatch, {})
        a, sub = f"dashboard:{lead}", "subagent:run1"

        def read_state_while_a_write_lands(run_id):
            # The crew-log writer commits an unrelated chat's session/opened now.
            tree_state._nodes = {"chat-x-1790000000": TreeNode("chat-x-1790000000", None, False)}
            return {"parent_session": a} if run_id == "run1" else None

        monkeypatch.setattr(subagent_persistence, "read_state", read_state_while_a_write_lands)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in (a, sub):
            trees.record_tree(key, tree)
        trees._TREE_INDEX_LIVE.clear()
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) == tree
        assert trees.quarantined_keys(tree) is None

    # -- an owner known only in memory counts when it leaves ---------------------------

    @pytest.mark.asyncio
    async def test_a_live_only_owner_leaving_is_recorded_as_a_former_owner(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """B's binding to D never reached the index. B cold-starts into a fresh tree:
        D's former-owners row still names A and B, so A's resume weighs B and never
        remounts D as its sole owner."""
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        fresh = scratch_root / "dashboard-chat-2-fresh002"
        tree.mkdir()
        fresh.mkdir()
        a, b = "dashboard:chat-1-1790000000", "dashboard:chat-2-1790000000"
        assert trees.record_tree(a, tree)
        real_write = trees.atomic_write

        def index_write_fails(path, *args, **kwargs):
            if Path(path).name == trees.TREE_INDEX_FILENAME:
                raise OSError("disk full")
            return real_write(path, *args, **kwargs)

        monkeypatch.setattr(trees, "atomic_write", index_write_fails)
        assert not trees.record_tree(b, tree)  # B is bound to D in memory only
        monkeypatch.setattr(trees, "atomic_write", real_write)
        assert trees.holds_live_tree(b, tree)
        assert trees.record_tree(b, fresh)  # B cold-starts into a fresh tree
        assert trees.former_owners(tree) >= {a, b}
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None

    def test_a_persisted_owner_leaving_still_marks_as_before(self, scratch_root: Path) -> None:
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)
        trees._TREE_INDEX_LIVE.clear()  # only the disk rows know them
        assert trees.record_tree(b, fresh)
        assert trees.former_owners(tree) >= {a, b}

    # -- a mark that does not land drops the live binding and is held here ------------

    def _cap_index(self, scratch_root: Path, monkeypatch) -> tuple[Path, Path, str, str]:
        tree, fresh, k, s = self._shared_with_two_owners(scratch_root)
        index = scratch_root / trees.TREE_INDEX_FILENAME
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", len(index.read_bytes()) + 10)
        return tree, fresh, k, s

    @pytest.mark.asyncio
    async def test_a_key_that_left_while_its_mark_failed_never_resumes_into_the_old_tree(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        tree, fresh, k, s = self._cap_index(scratch_root, monkeypatch)
        assert not trees.record_tree(k, fresh)  # K starts W; the mark does not fit
        assert trees.recorded_tree(k) != tree
        assert await scratch_ownership._recorded_tree_of_own_session_tree(k, MagicMock()) != tree

    @pytest.mark.asyncio
    async def test_the_co_owner_left_never_remounts_the_tree_as_its_sole_owner(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.session_allocation_parts import scratch_ownership

        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        tree, fresh, k, s = self._cap_index(scratch_root, monkeypatch)
        assert not trees.record_tree(k, fresh)
        assert await scratch_ownership._recorded_tree_of_own_session_tree(s, MagicMock()) is None
        assert trees.refused_across_trees(tree)
