"""The check after a spawn weighs ownership evidence written while the child spawned.

Split out of ``test_subagent_scratch_tree_ownership.py`` for size; the harness (the
settled lineage fixture, the slot reader and the two-owner directory) is that
module's.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Pytest fixtures register in the module that imports them.
from test_subagent_scratch_tree_index import isolated_tree_bindings  # noqa: F401
from test_subagent_scratch_tree_index import scratch_root_fixture  # noqa: F401
from test_subagent_scratch_tree_ownership import (
    TestAResumeNeverRemountsAnotherTreesDirectory as _Owners,
)

from kiro_crew import agent_scratch_trees as trees


class TestThePostSpawnCheckWeighsOwnership:
    """Evidence that lands between the check before the spawn and the one after it."""

    settled_lineage = _Owners.settled_lineage
    _real_reader = _Owners._real_reader
    _shared_with_two_owners = _Owners._shared_with_two_owners

    @pytest.mark.asyncio
    @pytest.mark.parametrize("during", ["other_tree", "nothing", "own_row", "unreadable"])
    async def test_the_post_spawn_check_weighs_ownership_written_during_the_spawn(
        self, scratch_root: Path, monkeypatch, during: str
    ) -> None:
        """Evidence that lands while the child spawns is weighed by the check after
        the spawn: another tree's owner, or an unreadable record, stops the child."""
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from kiro_crew.acp import launch
        from kiro_crew.acp.transport_errors import AcpError

        self._real_reader(
            monkeypatch,
            {
                "chat-1-1790000000": "",
                "chat-2-1790000000": "chat-1-1790000000",
                "chat-3-1790000000": "chat-1-1790000000",
            },
        )
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)

        async def hop(fn, *args):
            if during == "other_tree":  # B rebinds and leaves A's tree mid-spawn
                assert trees.record_tree(b, fresh)
                self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
            elif during == "own_row":  # a key of A's own tree
                assert trees.record_owner_marker("dashboard:chat-3-1790000000", tree)
            elif during == "unreadable":
                (scratch_root / trees.OWNERS_FILENAME).write_text("[")
            return fn(*args)

        from kiro_crew import platform_compat

        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(
            platform_compat, "kill_process_tree_async", AsyncMock(return_value=True)
        )
        process = MagicMock(pid=99_999_999_999, returncode=None)  # not yet reaped
        process.wait = AsyncMock(return_value=0)
        host = SimpleNamespace(
            _shared_scratch=tree,
            _discard_bound_workspace=AsyncMock(),
            _discard_sandbox_cleanup=MagicMock(),
        )
        request = SimpleNamespace(scratch_label="dashboard:chat-1-1790000000", backend="")
        run = launch._stop_a_child_that_mounted_a_refused_tree(
            host, request, process, False, hop, MagicMock()
        )
        if during in ("other_tree", "unreadable"):
            with pytest.raises(AcpError):
                await run
            assert host._shared_scratch is None
            assert tree.is_dir()  # the directory is left for the sweep
        else:
            await run
            assert host._shared_scratch == tree

    @pytest.mark.asyncio
    async def test_a_former_row_written_between_the_resolvers_reads_is_seen(
        self, scratch_root: Path, monkeypatch
    ) -> None:
        """B leaves for its own tree while the check reads the retained owners: the
        former-owner row is read after them, so the check refuses the mount."""
        self._real_reader(monkeypatch, {"chat-1-1790000000": "", "chat-2-1790000000": ""})
        tree, fresh, a, b = self._shared_with_two_owners(scratch_root)

        def retained_while_b_leaves(tree_arg: Path) -> frozenset[str]:
            assert trees.record_tree(b, fresh)  # writes only the former-owner row
            return frozenset()

        monkeypatch.setattr(trees, "_retained_owners", retained_while_b_leaves)
        assert not await trees.resolve_open_ownership(tree)
        assert trees.refused_across_trees(tree)
