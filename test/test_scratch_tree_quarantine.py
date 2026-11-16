"""Quarantine marks in the scratch tree index: storage, bounds and clearing."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import agent_scratch as sc
from kiro_crew import agent_scratch_trees as trees
from kiro_crew.testing.links import make_dir_link


@pytest.fixture(autouse=True)
def isolated_tree_bindings(_floor_monkeypatch) -> None:
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_LIVE", {})
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_TICKETS", {})
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_UNPERSISTED", set(), raising=False)
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_RETIRED_TICKET", -1, raising=False)
    _floor_monkeypatch.setattr(trees, "_TREE_INDEX_DISK_SNAPSHOT", None, raising=False)
    _floor_monkeypatch.setattr(trees, "_REFUSED_UNPERSISTED", {}, raising=False)
    _floor_monkeypatch.setattr(trees, "_REFUSALS_OVERFLOWED", False, raising=False)


@pytest.fixture(name="scratch_root")
def scratch_root_fixture(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    return home / "scratch"


class TestQuarantineMarks:
    """A mark keeps a directory refused until every owner it names is settled."""

    @pytest.mark.parametrize(
        "marks", ["x", [], {"dashboard-chat-1-abcdef01": "x"}, {"../up": ["k"]}]
    )
    def test_a_mark_row_this_module_did_not_write_refuses(self, scratch_root: Path, marks) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        index = scratch_root / trees.TREE_INDEX_FILENAME
        index.write_text(json.dumps({"dashboard:chat-1": tree.name, trees._MARKS_ROW: marks}))
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)
        trees.record_tree("dashboard:chat-2", tree)  # a rewrite carries the row unchanged
        assert json.loads(index.read_text())[trees._MARKS_ROW] == marks

    @pytest.mark.parametrize("row", ["_MARKS_ROW", "_FORMER_ROW", "_PARENTS_ROW"])
    def test_a_key_named_like_a_reserved_row_is_never_recorded(
        self, scratch_root: Path, row: str
    ) -> None:
        """A binding under a reserved row's name would make that row unreadable, so
        every recorded directory would refuse."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        other = scratch_root / "dashboard-chat-2-abcdef02"
        tree.mkdir()
        other.mkdir()
        assert trees.record_tree("dashboard:chat-2", other)
        recorded = trees.record_tree(getattr(trees, row), tree)
        assert not trees.refused_across_trees(other)
        assert trees.quarantined_keys(other) is None
        assert not recorded

    @pytest.mark.parametrize("failure", ["size_cap", "write_error"])
    def test_a_failed_retirement_is_held_before_the_lock_is_released(
        self, scratch_root: Path, monkeypatch, failure: str
    ) -> None:
        """No co-owner can mount the directory between a failed retirement and the
        quarantine that follows it: the refusal is held on the False return."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        owners = frozenset({"dashboard:chat-1", "dashboard:chat-2"})
        for key in owners:
            assert trees.record_tree(key, tree)
        if failure == "size_cap":
            monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", 10)
        else:

            def failing_write(path, payload):
                raise OSError("index unwritable")

            monkeypatch.setattr(trees, "atomic_write", failing_write)
        assert not trees.abandon_tree_all(tree, owners)
        assert trees.held_refusal(tree) == owners
        assert trees.refused_across_trees(tree)

    def test_a_hold_landing_during_the_disk_read_still_refuses(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A concurrent quarantine whose write failed holds its refusal while this
        check reads the disk marks: the check still refuses the directory."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        real = trees._read_marks

        def read_while_a_hold_lands(root):
            trees._REFUSED_UNPERSISTED[tree.name] = frozenset({"k"})
            return real(root)

        monkeypatch.setattr(trees, "_read_marks", read_while_a_hold_lands)
        assert trees.refused_across_trees(tree)

    def test_an_owner_row_that_would_pass_the_cap_is_not_written(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """The owner file never grows past what its own reader accepts."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        assert trees.record_owner_marker("dashboard:chat-1", tree)
        before = (scratch_root / trees.OWNERS_FILENAME).read_bytes()
        monkeypatch.setattr(trees, "_OWNERS_MAX_BYTES", len(before) + 4)
        assert not trees.record_owner_marker("dashboard:chat-2-with-a-long-key", tree)
        assert (scratch_root / trees.OWNERS_FILENAME).read_bytes() == before
        assert not trees.refused_across_trees(tree)  # the file stays readable

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
    def test_a_fifo_in_place_of_the_owner_file_refuses_without_hanging(
        self, scratch_root: Path
    ) -> None:
        """A FIFO where the owner file belongs is read without blocking and refuses."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        os.mkfifo(scratch_root / ".owners.json")
        assert trees.refused_across_trees(tree)
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)

    @pytest.mark.parametrize("content", ["{truncated", "[]"])
    def test_an_unusable_index_refuses_rather_than_reading_as_no_marks(
        self, scratch_root: Path, content: str
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        (scratch_root / trees.TREE_INDEX_FILENAME).write_text(content)
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)

    def test_an_index_written_by_main_reads_as_before(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        index = scratch_root / trees.TREE_INDEX_FILENAME
        index.write_text(json.dumps({"dashboard:chat-1": tree.name}))
        assert trees.recorded_tree("dashboard:chat-1") == tree
        assert trees.quarantined_keys(tree) is None
        assert trees.quarantine_tree(tree, frozenset({"dashboard:chat-1"}))
        assert trees.recorded_tree("dashboard:chat-1") == tree  # bindings untouched
        assert trees.keys_on_tree(tree) == frozenset({"dashboard:chat-1"})

    def test_a_mark_that_cannot_be_written_changes_nothing(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """No mark lives only in memory: a failed write leaves the index as it was,
        so every owner stays bound and the next ownership check refuses again."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = frozenset({"dashboard:chat-1", "dashboard:chat-2"})
        for key in keys:
            trees.record_tree(key, tree)
        index = scratch_root / trees.TREE_INDEX_FILENAME
        before = index.read_text()
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, former=None: False)
        assert not trees.quarantine_tree(tree, keys)
        assert index.read_text() == before
        assert trees.quarantined_keys(tree) is None
        assert trees.keys_on_tree(tree) == keys

    def test_a_retirement_too_large_for_its_mark_leaves_every_row(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """A mark that does not fit fails the whole write: the bindings stay on disk
        (so a restart still sees both owners), the index stays readable, and this
        process still never hands the directory back."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-1", "dashboard:chat-2"):
            trees.record_tree(key, tree)
        index = scratch_root / trees.TREE_INDEX_FILENAME
        before = index.read_text()
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", len(before) + 40)
        many = frozenset(f"dashboard:chat-held-{n:04d}" for n in range(50))
        assert not trees.abandon_tree_all(tree, many)
        assert index.read_text() == before
        assert tree.name not in trees._read_marks(scratch_root)  # the index took no mark
        assert trees.quarantined_keys(tree) == many  # the sentinel in the directory holds it
        assert trees.recorded_tree("dashboard:chat-1") is None  # retired in this process
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        assert trees.keys_on_tree(tree) == frozenset({"dashboard:chat-1", "dashboard:chat-2"})

    def test_a_mark_with_many_owners_is_written_in_full(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        many = frozenset(f"dashboard:chat-{n:04d}" for n in range(300))
        assert trees.quarantine_tree(tree, many)
        assert trees.quarantined_keys(tree) == many

    def test_an_overflow_value_from_an_earlier_build_refuses(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        (scratch_root / trees.TREE_INDEX_FILENAME).write_text(
            json.dumps({trees._MARKS_ROW: {tree.name: "overflow"}})
        )
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)
        assert trees.refused_across_trees(tree)

    def test_a_mark_that_gained_owners_after_the_check_is_kept(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        checked = frozenset({"dashboard:chat-1"})
        assert trees.quarantine_tree(tree, checked)  # what resume A read
        assert trees.quarantine_tree(tree, frozenset({"dashboard:chat-new"}))  # added since
        assert not trees.clear_quarantine(tree, checked)
        trees.record_tree("dashboard:chat-1", tree)
        assert trees.abandon_tree_all(tree, checked)  # bindings go, the mark stays
        assert trees.quarantined_keys(tree) == checked | {"dashboard:chat-new"}

    def test_a_quarantine_mark_survives_a_fresh_process(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        keys = frozenset({"dashboard:chat-1", "dashboard:chat-2"})
        assert trees.quarantine_tree(tree, keys)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart: only what is on disk remains
        assert trees.quarantined_keys(tree) == keys
        assert trees._MARKS_ROW in json.loads(
            (scratch_root / trees.TREE_INDEX_FILENAME).read_text()
        )

    def test_a_refused_directory_is_retired_for_every_key_in_one_write(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        other = scratch_root / "dashboard-chat-9-abcdef09"
        tree.mkdir()
        other.mkdir()
        for key in ("dashboard:chat-1", "dashboard:chat-2", "subagent:abc123"):
            trees.record_tree(key, tree)
        trees.record_tree("dashboard:chat-9", other)
        writes = []
        real_write = trees.atomic_write

        def counting_write(*args, **kwargs):
            writes.append(args[0])
            return real_write(*args, **kwargs)

        monkeypatch.setattr(trees, "atomic_write", counting_write)
        assert trees.abandon_tree_all(tree, frozenset({"dashboard:chat-1"}))
        assert len(writes) == 1
        assert trees.keys_on_tree(tree) == frozenset()
        assert trees.recorded_tree("dashboard:chat-9") == other


class TestChannelAndCronParents:
    """A channel or cron chat's own subagent places it no differently from itself alone."""

    @staticmethod
    async def _resume(scratch_root: Path, monkeypatch, parent: str, recorded: str | None, record):
        from unittest.mock import MagicMock

        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(subagent_persistence, "read_state", lambda run_id: record)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "channel-abcdef01"
        tree.mkdir()
        for key in (parent, "subagent:worker"):
            trees.record_tree(key, tree)
        if recorded is not None:
            assert trees.record_tree("subagent:worker", tree, parent=recorded)
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        found = await scratch_ownership._recorded_tree_of_own_session_tree(parent, MagicMock())
        return found, tree

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent", ["slack:C1:171.1", "cron:job-1"])
    @pytest.mark.parametrize("live", [False, True])
    async def test_its_own_subagent_keeps_the_directory(
        self, scratch_root: Path, monkeypatch, parent: str, live: bool
    ) -> None:
        record = {"parent_session": parent} if live else None
        found, tree = await self._resume(scratch_root, monkeypatch, parent, parent, record)
        assert found == tree
        assert trees.quarantined_keys(tree) is None

    @pytest.mark.asyncio
    async def test_a_recorded_parent_that_is_not_an_owner_quarantines(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        found, tree = await self._resume(
            scratch_root, monkeypatch, "slack:C1:171.1", "slack:C9:999.9", None
        )
        assert found is None
        assert trees.quarantined_keys(tree) is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent", ["slack:C1:171.1", "cron:job-1"])
    async def test_the_collapse_keeps_the_parents_own_flag(
        self, scratch_root: Path, monkeypatch, parent: str
    ) -> None:
        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(subagent_persistence, "read_state", lambda run_id: None)
        scratch_root.mkdir(parents=True)
        (scratch_root / "channel-abcdef01").mkdir()
        trees.record_tree("subagent:worker", scratch_root / "channel-abcdef01")
        assert trees.record_tree(
            "subagent:worker", scratch_root / "channel-abcdef01", parent=parent
        )
        alone = await scratch_ownership._owner_roots(frozenset({parent}))
        with_child = await scratch_ownership._owner_roots(frozenset({parent, "subagent:worker"}))
        assert with_child == alone == ({parent}, False)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "record",
        [
            {"parent_session": "cron:other"},  # live record disagrees with the row
            {"parent_session": "subagent:worker"},  # self-cycle
            {"parent_session": "subagent:middle"},  # through another subagent
        ],
    )
    async def test_a_live_record_that_disagrees_with_the_row_quarantines(
        self, scratch_root: Path, monkeypatch, record
    ) -> None:
        found, tree = await self._resume(
            scratch_root, monkeypatch, "slack:C1:171.1", "slack:C1:171.1", record
        )
        assert found is None
        assert trees.quarantined_keys(tree) is not None

    @pytest.mark.asyncio
    async def test_a_parents_row_on_a_channel_key_never_hides_it(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(trees, "recorded_parent", lambda key: "slack:C1:171.1")
        roots, _ = await scratch_ownership._owner_roots(
            frozenset({"slack:C1:171.1", "slack:F:1.1"})
        )
        assert roots == {"slack:C1:171.1", "slack:F:1.1"}

    @pytest.mark.asyncio
    async def test_a_thread_from_before_parent_rows_keeps_its_directory(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        parent = "slack:C1:171.1"
        found, tree = await self._resume(
            scratch_root, monkeypatch, parent, None, {"parent_session": parent}
        )
        assert found == tree
        assert trees.quarantined_keys(tree) is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live", [False, True])
    @pytest.mark.parametrize("middle_owns", [True, False])
    async def test_a_nested_subagent_of_a_channel_chat(
        self, scratch_root: Path, monkeypatch, live: bool, middle_owns: bool
    ) -> None:
        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import scratch_ownership

        rows = {"subagent:middle": "slack:C1:171.1", "subagent:worker": "subagent:middle"}
        monkeypatch.setattr(trees, "recorded_parent", rows.get)
        records = {"middle": rows["subagent:middle"], "worker": rows["subagent:worker"]}
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda run_id: {"parent_session": records[run_id]} if live else None,
        )
        owners = {"slack:C1:171.1", "subagent:worker"} | (
            {"subagent:middle"} if middle_owns else set()
        )
        roots, _ = await scratch_ownership._owner_roots(frozenset(owners))
        assert (roots == {"slack:C1:171.1"}) is middle_owns  # a hop off the directory refuses

    @pytest.mark.asyncio
    async def test_a_chain_at_the_deepest_work_depth_is_one_tree(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Every subagent of a chain as deep as ``work_ledger.max_depth`` allows is placed."""
        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import scratch_ownership
        from kiro_crew.work_vocab import WORK_MAX_DEPTH_MAX

        root = "slack:C1:171.1"
        chain = [f"subagent:s{n}" for n in range(WORK_MAX_DEPTH_MAX)]
        parents = dict(zip(chain, [root, *chain]))
        monkeypatch.setattr(trees, "recorded_parent", parents.get)
        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda run_id: {"parent_session": parents[f"subagent:{run_id}"]},
        )
        roots, _ = await scratch_ownership._owner_roots(frozenset({root, *chain}))
        assert roots == {root}


class TestAHeldMarkAtTheSizeCap:
    """An index too full for a mark never drops an owner of the marked directory."""

    @staticmethod
    def _full_index(
        scratch_root: Path,
        monkeypatch,
        shared_name: str = "dashboard-chat-a-abcdef01",
        fresh_name: str = "dashboard-chat-a-abcdef02",
        size: int = 1_048_512,
    ) -> tuple[Path, Path]:
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership, "_lineage_unavailable_reason", lambda: "the crew log is off"
        )
        scratch_root.mkdir(parents=True)
        shared = scratch_root / shared_name
        fresh = scratch_root / fresh_name
        shared.mkdir()
        fresh.mkdir()
        for key in ("dashboard:chat-a-1", "dashboard:chat-b-1"):
            trees.record_tree(key, shared)
        index = scratch_root / trees.TREE_INDEX_FILENAME
        # Pad the index to exactly *size* bytes with bindings of a live directory.
        rows = json.loads(index.read_text())
        pad = scratch_root / "dashboard-pad-abcdef09"
        pad.mkdir()
        n = 0
        while len(json.dumps(rows, sort_keys=True)) < size - 600:
            rows[f"cron:pad-{n:07d}-{'x' * 200}"] = pad.name
            n += 1
        fill = 1
        while len(json.dumps({**rows, "cron:" + "y" * fill: pad.name})) < size:
            fill += 1
        rows["cron:" + "y" * fill] = pad.name
        assert len(json.dumps(rows, sort_keys=True).encode()) == size
        index.write_text(json.dumps(rows, sort_keys=True))
        trees._TREE_INDEX_LIVE.clear()
        return shared, fresh

    @staticmethod
    def _restart() -> None:
        trees._TREE_INDEX_LIVE.clear()
        trees._TREE_INDEX_DISK_SNAPSHOT = None

    @pytest.mark.asyncio
    async def test_a_resume_rebinding_off_a_held_directory_never_lets_the_other_owner_rejoin(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from unittest.mock import MagicMock

        from kiro_crew.session_allocation_parts import scratch_ownership

        shared, fresh = self._full_index(scratch_root, monkeypatch)
        resume = scratch_ownership._recorded_tree_of_own_session_tree
        assert await resume("dashboard:chat-a-1", MagicMock()) is None
        assert trees.quarantined_keys(shared) is None  # the mark did not fit: nothing changed
        assert not trees.record_tree("dashboard:chat-a-1", fresh)  # refused, not thinned
        self._restart()
        assert await resume("dashboard:chat-b-1", MagicMock()) is None
        assert trees.keys_on_tree(shared) >= {"dashboard:chat-a-1", "dashboard:chat-b-1"}

    @pytest.mark.asyncio
    async def test_a_rebind_between_a_failed_retire_and_its_mark_never_lets_one_owner_rejoin(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Retire and mark both overflow the cap, B rebinds between them, the gateway
        restarts: A must not be handed the directory B staged files in."""
        from unittest.mock import MagicMock

        from kiro_crew.session_allocation_parts import scratch_ownership

        a, b = "dashboard:chat-a-1", "dashboard:chat-b-1"
        # At the cap exactly, with names short enough that retiring two rows frees
        # less than the mark adds, and B's rebind to *fresh* keeps the size.
        shared, fresh = self._full_index(
            scratch_root, monkeypatch, "abcdef01", "abcdef02", trees._TREE_INDEX_MAX_BYTES
        )
        real_quarantine = trees.quarantine_tree

        def b_rebinds_first(tree, keys):
            trees.record_tree(b, fresh)  # B's own resume, between the two lock holds
            return real_quarantine(tree, keys)

        monkeypatch.setattr(trees, "quarantine_tree", b_rebinds_first)
        assert not scratch_ownership._retire_or_mark(shared, frozenset({a, b}))
        self._restart()
        monkeypatch.setattr(scratch_ownership, "_lineage_unavailable_reason", lambda: "")
        assert await scratch_ownership._recorded_tree_of_own_session_tree(a, MagicMock()) is None

    def test_a_rebind_at_the_cap_that_owes_a_mark_drops_the_live_binding(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """Leaving a directory a co-owner still holds owes it a mark; at the cap the
        mark cannot be written, so it is held in this process, the disk row stays,
        and the live binding moves off the old directory: this process never hands
        it back."""
        shared, fresh = self._full_index(scratch_root, monkeypatch)
        assert not trees.record_tree("dashboard:chat-a-1", fresh)
        rows = json.loads((scratch_root / trees.TREE_INDEX_FILENAME).read_text())
        assert rows["dashboard:chat-a-1"] == shared.name
        assert trees.recorded_tree("dashboard:chat-a-1") != shared
        assert trees.held_refusal(shared) >= {"dashboard:chat-a-1", "dashboard:chat-b-1"}
        self._restart()
        assert trees.keys_on_tree(shared) >= {"dashboard:chat-a-1", "dashboard:chat-b-1"}


class TestARetiredDirectoryStaysMarked:
    """A directory retired across two trees keeps refusing every key later bound to it."""

    A, B, CHILD = "dashboard:chat-a-1", "dashboard:chat-b-1", "subagent:kept"

    async def _retire_on_as_resume(self, scratch_root: Path, monkeypatch) -> Path:
        from unittest.mock import MagicMock

        from kiro_crew import subagent_persistence
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            subagent_persistence,
            "read_state",
            lambda run_id: {"parent_session": self.B} if run_id == "kept" else None,
        )
        monkeypatch.setattr(scratch_ownership, "_lineage_unavailable_reason", lambda: "")
        scratch_root.mkdir(parents=True)
        shared = scratch_root / "dashboard-chat-a-abcdef01"
        shared.mkdir()
        (shared / "a-staged.txt").write_text("A's file")
        for key in (self.A, self.B):
            trees.record_tree(key, shared)
        real_roots = scratch_ownership._owner_roots

        async def two_verified_trees(owners):
            return set(owners), True

        monkeypatch.setattr(scratch_ownership, "_owner_roots", two_verified_trees)
        assert (
            await scratch_ownership._recorded_tree_of_own_session_tree(self.A, MagicMock()) is None
        )
        monkeypatch.setattr(scratch_ownership, "_owner_roots", real_roots)
        return shared

    @staticmethod
    def _restart() -> None:
        trees._TREE_INDEX_LIVE.clear()
        trees._TREE_INDEX_DISK_SNAPSHOT = None

    @pytest.mark.asyncio
    async def test_a_live_co_owners_kept_child_never_remounts_it_after_a_restart(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from kiro_crew.session_allocation_parts import scratch_ownership
        from kiro_crew.session_allocation_parts.companions import (
            admit_shared_scratch,
            parent_work_scratch_dir,
        )

        shared = await self._retire_on_as_resume(scratch_root, monkeypatch)
        assert trees.keys_on_tree(shared) == frozenset()
        # B is still live in the directory; its kept child is not handed it.
        live_b = SimpleNamespace(get_provider=lambda key: SimpleNamespace(work_scratch_dir=shared))
        assert await admit_shared_scratch(parent_work_scratch_dir(live_b, self.B)) is None
        # Even a child bound to it anyway is refused on its resume after a restart.
        trees.record_tree(self.CHILD, shared)
        self._restart()
        assert trees.quarantined_keys(shared) >= {self.A, self.B}  # the mark survived
        resume = scratch_ownership._recorded_tree_of_own_session_tree
        assert await resume(self.CHILD, MagicMock()) is None
        assert (shared / "a-staged.txt").read_text() == "A's file"

    @pytest.mark.asyncio
    async def test_the_mark_clears_once_its_owners_are_one_tree_or_the_directory_is_reclaimed(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        shared = await self._retire_on_as_resume(scratch_root, monkeypatch)
        assert trees.clear_quarantine(shared, frozenset({self.A, self.B}))
        assert trees.quarantined_keys(shared) is None
        other = scratch_root / "dashboard-chat-b-abcdef02"
        other.mkdir()
        assert trees.abandon_tree_all(other, frozenset({self.A, self.B}))
        other.rmdir()  # the sweep reclaims it
        trees.record_tree(self.B, shared)  # any later index write prunes the mark
        assert other.name not in json.loads(
            (scratch_root / trees.TREE_INDEX_FILENAME).read_text()
        ).get(trees._MARKS_ROW, {})

    def test_a_single_keys_retirement_leaves_no_mark(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-a-abcdef01"
        tree.mkdir()
        trees.record_tree(self.A, tree)
        assert trees.abandon_tree(self.A)
        assert trees.quarantined_keys(tree) is None


class TestTheSpawnPathChecksTheMarkOffTheLoop:
    """A spawn on a parent's behalf never reads the tree index on the event loop."""

    @pytest.mark.asyncio
    async def test_both_spawn_paths_read_the_mark_off_the_loop(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        import threading
        from types import SimpleNamespace

        from kiro_crew.session_allocation_parts import companions

        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-a-abcdef01"
        tree.mkdir()
        loop_thread = threading.get_ident()
        readers: list[int] = []
        real = trees._marks_locked

        def recording(root):
            readers.append(threading.get_ident())
            return real(root)

        monkeypatch.setattr(trees, "_marks_locked", recording)
        owner = SimpleNamespace(
            get_provider=lambda key: SimpleNamespace(work_scratch_dir=tree, client=None)
        )
        assert companions.parent_work_scratch_dir(owner, "dashboard:chat-a-1") == tree
        assert readers == []  # the provider read does no index I/O
        assert await companions.admit_shared_scratch(tree) == tree
        assert readers and loop_thread not in readers


class TestARetirementWithNoRootLeavesNoLiveBinding:
    @pytest.mark.parametrize("root_state", ["missing", "linked"])
    def test_a_retirement_with_no_usable_root_retires_the_live_bindings(
        self, scratch_root: Path, tmp_path: Path, root_state: str
    ) -> None:
        """Nothing is left on disk to write, but this process still holds the bindings
        in memory; a retirement that reports success must not leave them handing the
        directory back."""
        import shutil

        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        trees.record_tree("dashboard:chat-1", tree)
        assert trees.holds_live_tree("dashboard:chat-1", tree)
        shutil.rmtree(scratch_root)
        if root_state == "linked":
            elsewhere = tmp_path / "elsewhere"
            elsewhere.mkdir()
            make_dir_link(scratch_root, elsewhere)
        assert trees.abandon_tree_all(tree, frozenset({"dashboard:chat-1"}))
        assert not trees.holds_live_tree("dashboard:chat-1", tree)


class TestOnceMarkedTheOtherTreeNeverRemounts:
    def test_a_child_admitted_before_the_mark_leaves_no_binding_after_it(
        self, scratch_root: Path
    ) -> None:
        """What stays open is bounded to processes admitted before the mark: a
        child passes its final check, then a co-owner's resume retires D under a mark,
        and from then on no key is bound to D and every mount check refuses it."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-a-abcdef01"
        tree.mkdir()
        a, b = "dashboard:chat-a-1", "dashboard:chat-b-1"
        for key in (a, b):
            trees.record_tree(key, tree)
        assert trees.refused_across_trees(tree) is False  # the child's final check
        assert trees.abandon_tree_all(tree, frozenset({a, b}))  # B's resume, after it
        assert trees.refused_across_trees(tree) is True
        assert trees.keys_on_tree(tree) == frozenset()
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart changes nothing
        assert trees.refused_across_trees(tree) is True
        assert trees.recorded_tree(b) is None


class TestAQuarantineTheIndexDidNotTakeStillRefuses:
    """A live co-owner's respawn checks the mark only, so a mark the index could not
    take must still refuse the directory in this process."""

    def test_a_failed_quarantine_refuses_the_respawn_in_this_process(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-a-abcdef01"
        tree.mkdir()
        for key in ("dashboard:chat-a-1", "dashboard:chat-b-1"):
            trees.record_tree(key, tree)
        index = scratch_root / trees.TREE_INDEX_FILENAME
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", len(index.read_bytes()) + 10)
        many = frozenset(f"dashboard:chat-held-{n:04d}" for n in range(50))
        assert not trees.quarantine_tree(tree, many)  # the payload does not fit
        assert trees.quarantined_keys(tree) is None  # nothing on disk
        assert trees.refused_across_trees(tree)  # the respawn's mount check

    def test_a_durable_mark_releases_the_held_refusal_and_a_reclaim_prunes_it(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-a-abcdef01"
        other = scratch_root / "dashboard-chat-b-abcdef02"
        tree.mkdir()
        other.mkdir()
        real_write = trees._write_marks
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, former=None: False)
        assert not trees.quarantine_tree(tree, frozenset({"dashboard:chat-a-1"}))
        assert not trees.quarantine_tree(other, frozenset({"dashboard:chat-b-1"}))
        assert set(trees._REFUSED_UNPERSISTED) == {tree.name, other.name}
        monkeypatch.setattr(trees, "_write_marks", real_write)
        assert trees.quarantine_tree(tree, frozenset({"dashboard:chat-a-1"}))  # durable now
        assert set(trees._REFUSED_UNPERSISTED) == {other.name}
        assert trees.refused_across_trees(tree)  # by its mark
        other.rmdir()  # the sweep reclaims the other directory
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, former=None: False)
        assert not trees.quarantine_tree(tree, frozenset({"dashboard:chat-x-1"}))
        assert set(trees._REFUSED_UNPERSISTED) == {tree.name}


class TestABindingNeverLandsWithoutItsParent:
    def test_the_binding_and_its_parent_land_in_one_write(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-a-abcdef01"
        tree.mkdir()
        assert trees.record_tree("subagent:w", tree, parent="dashboard:chat-a-1")
        trees._TREE_INDEX_LIVE.clear()  # a gateway restart
        assert trees.recorded_tree("subagent:w") == tree
        assert trees.recorded_parent("subagent:w") == "dashboard:chat-a-1"

    def test_a_parent_that_does_not_fit_leaves_no_parentless_binding(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """After a prune and a restart a parentless binding would strand the directory:
        the binding is not persisted unless its parent is."""
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-a-abcdef01"
        tree.mkdir()
        trees.record_tree("dashboard:chat-a-1", tree)
        index = scratch_root / trees.TREE_INDEX_FILENAME
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", len(index.read_bytes()) + 40)
        parent = "dashboard:chat-a-1-" + "x" * 80
        assert not trees.record_tree("subagent:w", tree, parent=parent)
        rows = json.loads(index.read_text())
        assert "subagent:w" not in rows
        assert trees.recorded_parent("subagent:w") is None


class TestHeldRefusalsAreBounded:
    """The process-local refusals never outgrow the index they stand in for."""

    def _failing(self, monkeypatch) -> None:
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, former=None: False)

    @pytest.mark.parametrize("overflow", ["entries", "owners"])
    def test_an_overflow_refuses_without_growing_and_never_settles(
        self, scratch_root: Path, monkeypatch, overflow: str
    ) -> None:
        scratch_root.mkdir(parents=True)
        dirs = [scratch_root / f"dashboard-chat-{n}-abcdef0{n}" for n in range(3)]
        for d in dirs:
            d.mkdir()
        self._failing(monkeypatch)
        if overflow == "entries":
            monkeypatch.setattr(trees, "_REFUSED_MAX_ENTRIES", 2)
            for n, d in enumerate(dirs):
                trees.quarantine_tree(d, frozenset({f"dashboard:chat-{n}"}))
            assert len(trees._REFUSED_UNPERSISTED) == 2
        else:
            monkeypatch.setattr(trees, "_REFUSED_MAX_OWNERS", 2)
            many = frozenset(f"dashboard:chat-{n}" for n in range(3))
            trees.quarantine_tree(dirs[0], many)
            assert dirs[0].name not in trees._REFUSED_UNPERSISTED
        assert trees._REFUSALS_OVERFLOWED
        unmarked = scratch_root / "dashboard-chat-9-abcdef09"
        unmarked.mkdir()
        assert trees.refused_across_trees(unmarked)  # fail closed for every tree
        assert not trees.clear_quarantine(
            dirs[0], frozenset(f"dashboard:chat-{n}" for n in range(9))
        )
        assert trees.refused_across_trees(dirs[0])

    def test_under_the_caps_nothing_overflows(self, scratch_root: Path, monkeypatch) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        self._failing(monkeypatch)
        assert not trees.quarantine_tree(tree, frozenset({"dashboard:chat-1"}))
        assert not trees._REFUSALS_OVERFLOWED
        assert trees.held_refusal(tree) == frozenset({"dashboard:chat-1"})
