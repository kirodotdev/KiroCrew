"""Quarantine marks in the scratch tree index: storage, bounds and clearing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import agent_scratch as sc
from kiro_crew import agent_scratch_trees as trees


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

    def test_a_mark_writer_carries_another_directorys_held_mark(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        real_write_marks = trees._write_marks
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, own: False)
        scratch_root.mkdir(parents=True)
        held = scratch_root / "dashboard-chat-1-abcdef01"
        other = scratch_root / "dashboard-chat-2-abcdef02"
        held.mkdir()
        other.mkdir()
        assert not trees.quarantine_tree(held, frozenset({"dashboard:chat-1"}))
        monkeypatch.setattr(trees, "_write_marks", real_write_marks)
        assert trees.quarantine_tree(other, frozenset({"dashboard:chat-2"}))  # a mark write
        trees._QUARANTINE_LIVE.clear()  # a gateway restart
        assert trees.quarantined_keys(held) == frozenset({"dashboard:chat-1"})

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

    def test_memory_holds_unpersisted_marks_only_for_live_directories(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, own: False)
        scratch_root.mkdir(parents=True)
        dirs = [scratch_root / f"dashboard-chat-{n}-abcdef0{n}" for n in range(3)]
        for d in dirs:
            d.mkdir()
            assert not trees.quarantine_tree(d, frozenset({"dashboard:chat-1"}))
        dirs[0].rmdir()
        dirs[1].rmdir()
        trees.quarantine_tree(dirs[2], frozenset({"dashboard:chat-2"}))
        assert set(trees._QUARANTINE_LIVE) == {dirs[2].name}

    def test_an_unpersisted_mark_keeps_a_bounded_owner_set_and_refuses_past_it(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        real_write_marks = trees._write_marks
        monkeypatch.setattr(trees, "_QUARANTINE_MAX_OWNERS", 2)
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, own: False)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        for n in range(3):
            assert not trees.quarantine_tree(tree, frozenset({f"dashboard:chat-{n}"}))
        assert trees._QUARANTINE_LIVE[tree.name] is trees._TOO_MANY_OWNERS
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)
        # Writes recover: the overflow is written to the index and survives a restart.
        monkeypatch.setattr(trees, "_write_marks", real_write_marks)
        assert trees.quarantine_tree(tree, frozenset({"dashboard:chat-9"}))
        trees._QUARANTINE_LIVE.clear()
        trees._TREE_INDEX_LIVE.clear()
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)
        trees.record_tree("dashboard:chat-1", tree)  # an unrelated write keeps it
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)

    def test_an_overflow_mark_is_never_cleared(self, scratch_root: Path, monkeypatch) -> None:
        """A resume that read an ordinary mark cannot clear one that overflowed since."""
        monkeypatch.setattr(trees, "_QUARANTINE_MAX_OWNERS", 2)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        assert trees.quarantine_tree(tree, frozenset({"dashboard:chat-1"}))
        assert trees.quarantined_keys(tree) == frozenset({"dashboard:chat-1"})  # first read
        # Another resume adds owners past the bound before the clear.
        assert trees.quarantine_tree(tree, frozenset({"dashboard:chat-2", "dashboard:chat-3"}))
        assert not trees.clear_quarantine(tree, frozenset({"dashboard:chat-1"}))
        trees.record_tree("dashboard:chat-1", tree)
        assert trees.abandon_tree_all(tree, frozenset({"dashboard:chat-1"}))
        trees._QUARANTINE_LIVE.clear()  # a gateway restart
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)

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

    def test_a_retirement_writes_an_overflow_held_only_in_memory(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        real_write_marks = trees._write_marks
        monkeypatch.setattr(trees, "_QUARANTINE_MAX_OWNERS", 1)
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, own: False)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        assert not trees.quarantine_tree(tree, frozenset({"dashboard:chat-1", "dashboard:chat-2"}))
        assert trees._QUARANTINE_LIVE[tree.name] is trees._TOO_MANY_OWNERS
        monkeypatch.setattr(trees, "_write_marks", real_write_marks)
        trees.record_tree("dashboard:chat-1", tree)
        assert trees.abandon_tree_all(tree, frozenset({"dashboard:chat-1"}))
        trees._QUARANTINE_LIVE.clear()  # a gateway restart
        with pytest.raises(OSError):
            trees.quarantined_keys(tree)

    def test_a_retirement_too_large_with_held_owners_keeps_the_index_readable(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        other = scratch_root / "dashboard-chat-2-abcdef02"
        tree.mkdir()
        other.mkdir()
        trees.record_tree("dashboard:chat-1", tree)
        assert trees.quarantine_tree(other, frozenset({"dashboard:chat-2", "dashboard:chat-y"}))
        index = scratch_root / trees.TREE_INDEX_FILENAME
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", len(index.read_bytes()) + 40)
        many = frozenset(f"dashboard:chat-held-{n:04d}" for n in range(50))
        assert not trees.quarantine_tree(tree, many)  # too large: held in memory
        assert not trees.abandon_tree_all(tree, frozenset({"dashboard:chat-1"}))
        assert len(index.read_bytes()) <= trees._TREE_INDEX_MAX_BYTES
        assert "dashboard:chat-1" in json.loads(index.read_text())  # not retired without its mark
        assert trees.quarantined_keys(other) == frozenset({"dashboard:chat-2", "dashboard:chat-y"})
        assert trees.quarantined_keys(tree) == many  # still refused from memory

    def test_a_removal_too_large_with_held_marks_still_lands_and_stays_readable(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        scratch_root.mkdir(parents=True)
        marked = scratch_root / "dashboard-chat-1-abcdef01"
        held = scratch_root / "dashboard-chat-2-abcdef02"
        marked.mkdir()
        held.mkdir()
        trees.record_tree("dashboard:chat-1", marked)
        trees.record_tree("dashboard:chat-2", held)
        assert trees.quarantine_tree(marked, frozenset({"dashboard:chat-1", "dashboard:chat-x"}))
        index = scratch_root / trees.TREE_INDEX_FILENAME
        monkeypatch.setattr(trees, "_TREE_INDEX_MAX_BYTES", len(index.read_bytes()) + 40)
        many = frozenset(f"dashboard:chat-held-{n:04d}" for n in range(50))
        assert not trees.quarantine_tree(held, many)  # too large: held in memory
        trees.abandon_tree("dashboard:chat-2")
        assert len(index.read_bytes()) <= trees._TREE_INDEX_MAX_BYTES
        assert trees.quarantined_keys(marked) == frozenset({"dashboard:chat-1", "dashboard:chat-x"})
        assert "dashboard:chat-2" not in json.loads(index.read_text())

    def test_a_held_mark_goes_out_with_the_next_successful_index_write(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        real_write_marks = trees._write_marks
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, own: False)
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        other = scratch_root / "dashboard-chat-2-abcdef02"
        tree.mkdir()
        other.mkdir()
        keys = frozenset({"dashboard:chat-1", "dashboard:chat-2"})
        assert not trees.quarantine_tree(tree, keys)
        monkeypatch.setattr(trees, "_write_marks", real_write_marks)
        assert trees.record_tree("dashboard:chat-3", other)  # an unrelated binding write
        trees._QUARANTINE_LIVE.clear()  # a gateway restart
        assert trees.quarantined_keys(tree) == keys

    def test_an_unpersisted_mark_is_pruned_once_its_directory_is_reclaimed(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        monkeypatch.setattr(trees, "_write_marks", lambda root, marks, own: False)
        scratch_root.mkdir(parents=True)
        gone = scratch_root / "dashboard-chat-1-abcdef01"
        kept = scratch_root / "dashboard-chat-2-abcdef02"
        gone.mkdir()
        kept.mkdir()
        trees.quarantine_tree(gone, frozenset({"dashboard:chat-1"}))
        gone.rmdir()
        trees.quarantine_tree(kept, frozenset({"dashboard:chat-2"}))
        assert set(trees._QUARANTINE_LIVE) == {kept.name}

    def test_a_persisted_mark_is_not_also_kept_in_memory(self, scratch_root: Path) -> None:
        scratch_root.mkdir(parents=True)
        tree = scratch_root / "dashboard-chat-1-abcdef01"
        tree.mkdir()
        assert trees.quarantine_tree(tree, frozenset({"dashboard:chat-1"}))
        assert trees._QUARANTINE_LIVE == {}

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
            assert trees.record_parent("subagent:worker", recorded)
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
        assert trees.record_parent("subagent:worker", parent)
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


class TestAHeldMarkAtTheSizeCap:
    """An index too full for a mark never drops an owner of the marked directory."""

    @staticmethod
    def _full_index(scratch_root: Path, monkeypatch) -> tuple[Path, Path]:
        from kiro_crew.session_allocation_parts import scratch_ownership

        monkeypatch.setattr(
            scratch_ownership, "_lineage_unavailable_reason", lambda: "the crew log is off"
        )
        scratch_root.mkdir(parents=True)
        shared = scratch_root / "dashboard-chat-a-abcdef01"
        fresh = scratch_root / "dashboard-chat-a-abcdef02"
        shared.mkdir()
        fresh.mkdir()
        for key in ("dashboard:chat-a-1", "dashboard:chat-b-1"):
            trees.record_tree(key, shared)
        index = scratch_root / trees.TREE_INDEX_FILENAME
        # Pad the index to exactly 1,048,512 bytes with bindings of a live directory.
        rows = json.loads(index.read_text())
        pad = scratch_root / "dashboard-pad-abcdef09"
        pad.mkdir()
        n = 0
        while len(json.dumps(rows, sort_keys=True)) < 1_048_512 - 600:
            rows[f"cron:pad-{n:07d}-{'x' * 200}"] = pad.name
            n += 1
        fill = 1
        while len(json.dumps({**rows, "cron:" + "y" * fill: pad.name})) < 1_048_512:
            fill += 1
        rows["cron:" + "y" * fill] = pad.name
        assert len(json.dumps(rows, sort_keys=True).encode()) == 1_048_512
        index.write_text(json.dumps(rows, sort_keys=True))
        trees._TREE_INDEX_LIVE.clear()
        return shared, fresh

    @staticmethod
    def _restart() -> None:
        trees._TREE_INDEX_LIVE.clear()
        trees._QUARANTINE_LIVE.clear()
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
        assert shared.name in trees._QUARANTINE_LIVE  # the mark did not fit
        assert not trees.record_tree("dashboard:chat-a-1", fresh)  # refused, not thinned
        self._restart()
        assert await resume("dashboard:chat-b-1", MagicMock()) is None
        assert trees.keys_on_tree(shared) >= {"dashboard:chat-a-1", "dashboard:chat-b-1"}

    def test_a_rewrite_with_no_held_marks_still_writes_at_the_cap(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        shared, fresh = self._full_index(scratch_root, monkeypatch)
        assert trees.record_tree("dashboard:chat-a-1", fresh)
        assert trees.recorded_tree("dashboard:chat-a-1") == fresh


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
        trees._QUARANTINE_LIVE.clear()
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
        real = trees.quarantined_keys

        def recording(path):
            readers.append(threading.get_ident())
            return real(path)

        monkeypatch.setattr(trees, "quarantined_keys", recording)
        owner = SimpleNamespace(
            get_provider=lambda key: SimpleNamespace(work_scratch_dir=tree, client=None)
        )
        assert companions.parent_work_scratch_dir(owner, "dashboard:chat-a-1") == tree
        assert readers == []  # the provider read does no index I/O
        assert await companions.admit_shared_scratch(tree) == tree
        assert readers and loop_thread not in readers
