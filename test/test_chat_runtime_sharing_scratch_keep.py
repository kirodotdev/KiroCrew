"""Chat runtime sharing: a joiner's recorded scratch directory is kept on disk.

A resumed chat binds the scratch directory it was recorded in. When it JOINS a
process that mounts another directory, its own is not mounted, and its owner is
a process that exited with the gateway, so the sweep would reclaim it an hour
later. ``ChatSharePlacement.place`` holds that directory before the acquisition
and adds the joined process to its owner marker right after, so the directory
lives as long as the tree's process. An adoption that cannot keep it gives up
the join and founds the session its own process, which mounts the directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import pytest
from chat_runtime_sharing_helpers import _PlacementProvider, confirming, make_placement
from test_chat_runtime_sharing import spawner
from test_chat_runtime_sharing_cleanup import _PlacedRuntime

import kiro_crew.providers.chat_share as share_mod
from kiro_crew import agent_scratch as sc
from kiro_crew.runtime_ownership import RuntimeOwnership

_OK = ("gen-1", "acct-A", "auto", False, True)


@pytest.fixture
def fresh_table(monkeypatch):
    """A real lease table, and no spec-decided admission (the fake agents grant none)."""
    table = RuntimeOwnership()
    monkeypatch.setattr(share_mod, "RUNTIME_OWNERSHIP", table)
    monkeypatch.setattr(share_mod, "agent_spawns_native_subagents", lambda w, a, b="": False)
    monkeypatch.setattr(share_mod, "agent_pins_tool_surface", lambda w, a, t: False)
    return table


@pytest.fixture
def trees(monkeypatch, tmp_path):
    """A lead's live tree and a worker's idle tree whose owner exited with the gateway."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc, "config_dir", lambda: home)
    alive = {2002}
    monkeypatch.setattr(sc, "_pgroup_alive", lambda pid: pid in alive)
    lead_tree, old_tree = sc.allocate_scratch("runtime"), sc.allocate_scratch("runtime")
    sc.record_owner(old_tree, 1001)
    _make_idle(old_tree)
    return lead_tree, old_tree, alive


def _make_idle(tree: Path) -> None:
    idle = time.time() - 2 * sc._UNOWNED_GRACE_SECONDS
    for entry in (tree, tree / sc.OWNER_FILENAME):
        os.utime(entry, (idle, idle))


def _place_lead_then_worker(lead_tree: Path, old_tree: Path, *runtimes: _PlacedRuntime):
    lead_rt = _PlacedRuntime(pid=2002)
    lead_rt.work_scratch_dir = lead_tree
    spawn = spawner(lead_rt, *runtimes)
    ok = confirming(_OK)

    async def scenario():
        lead = make_placement(session_key="dashboard:chat-lead-1")
        lead._read_confirmation = ok
        provider = _PlacementProvider()
        provider._shared_scratch = old_tree
        worker = make_placement(provider=provider, session_key="dashboard:chat-worker-1")
        worker._read_confirmation = ok
        await lead.place(spawn, lead_tree)
        placed = await worker.place(spawn, old_tree)
        return worker, placed

    worker, placed = asyncio.run(scenario())
    return worker, placed, lead_rt, spawn


def test_a_joiners_recorded_tree_lives_as_long_as_the_shared_process(fresh_table, trees, caplog):
    lead_tree, old_tree, alive = trees
    lead_marker = (lead_tree / sc.OWNER_FILENAME).read_bytes()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.providers.chat_share"):
        worker, placed, lead_rt, _spawn = _place_lead_then_worker(lead_tree, old_tree)
    assert worker.joined is True and placed is lead_rt
    assert 2002 in sc._read_owner_pids(old_tree / sc.OWNER_FILENAME)
    # The founder's own mounted tree is not touched.
    assert (lead_tree / sc.OWNER_FILENAME).read_bytes() == lead_marker
    assert any(
        "dashboard:chat-worker-1" in r.getMessage() and old_tree.name in r.getMessage()
        for r in caplog.records
    ), "no WARNING names the session and its unmounted tree"
    _make_idle(old_tree)
    assert sc.sweep_dead_scratch() == 0 and old_tree.exists()
    alive.clear()  # the shared process exits: the ordinary sweep takes the tree
    _make_idle(old_tree)
    assert sc.sweep_dead_scratch() >= 1 and not old_tree.exists()


def test_a_sweep_between_the_join_and_the_adoption_keeps_the_recorded_tree(
    fresh_table, trees, monkeypatch
):
    """The hourly sweep lands while the join is being placed, before the owner update."""
    lead_tree, old_tree, _alive = trees
    adopt = sc.adopt_owner
    swept: list[int] = []

    def sweep_then_adopt(path: Path, pid: int):
        swept.append(sc.sweep_dead_scratch())
        return adopt(path, pid)

    monkeypatch.setattr(sc, "adopt_owner", sweep_then_adopt)
    worker, _placed, _lead_rt, _spawn = _place_lead_then_worker(lead_tree, old_tree)
    assert swept == [0], "the sweep reclaimed a tree the placement held"
    assert old_tree.exists() and worker.joined is True


@pytest.mark.parametrize("outcome", ["stale", "refused", "unwritable", "garbled", "recorded"])
def test_an_adoption_that_cannot_keep_the_tree_founds_the_session_its_own_process(
    fresh_table, trees, monkeypatch, outcome
):
    # "recorded" here writes nothing, as adopt_owner does on a tree with no marker.
    lead_tree, old_tree, _alive = trees
    monkeypatch.setattr(sc, "adopt_owner", lambda path, pid: outcome)
    own = _PlacedRuntime(pid=3003)
    worker, placed, lead_rt, spawn = _place_lead_then_worker(lead_tree, old_tree, own)
    assert placed is own and worker.joined is False
    assert spawn.calls == [lead_rt, own]
    assert lead_rt.kills == [], "the lead's process was killed with the worker's lease"
    assert fresh_table.leases_on_runtime(lead_rt) == 1


def test_a_cancellation_during_the_owner_update_releases_the_joiners_lease(
    fresh_table, trees, monkeypatch
):
    """The joiner already holds its lease when the owner update runs.

    A cancellation there propagates past the start path's release, so the placement
    releases the lease itself: the shared process keeps one tenant, and the last real
    chat leaving hands the process back to be closed.
    """
    lead_tree, old_tree, _alive = trees

    def cancelled(path: Path, pid: int):
        raise asyncio.CancelledError

    monkeypatch.setattr(sc, "adopt_owner", cancelled)
    lead_rt = _PlacedRuntime(pid=2002)
    lead_rt.work_scratch_dir = lead_tree
    spawn = spawner(lead_rt)
    ok = confirming(_OK)

    async def scenario():
        lead = make_placement(session_key="dashboard:chat-lead-1")
        lead._read_confirmation = ok
        provider = _PlacementProvider()
        provider._shared_scratch = old_tree
        worker = make_placement(provider=provider, session_key="dashboard:chat-worker-1")
        worker._read_confirmation = ok
        await lead.place(spawn, lead_tree)
        with pytest.raises(asyncio.CancelledError):
            await worker.place(spawn, old_tree)
        tenants = fresh_table.leases_on_runtime(lead_rt)
        last_out = await share_mod._settle_release(lead.lease)
        return worker, tenants, last_out

    worker, tenants, last_out = asyncio.run(scenario())
    assert worker.lease is None
    assert tenants == 1, "the cancelled joiner's lease is still held"
    assert last_out is lead_rt, "the last chat out did not hand the process back to be closed"


def test_an_unowned_recorded_tree_founds_the_session_its_own_process(fresh_table, trees):
    """No marker: the real ``adopt_owner`` answers "recorded" and writes nothing."""
    lead_tree, old_tree, _alive = trees
    (old_tree / sc.OWNER_FILENAME).unlink()
    own = _PlacedRuntime(pid=3003)
    worker, placed, lead_rt, spawn = _place_lead_then_worker(lead_tree, old_tree, own)
    assert placed is own and worker.joined is False
    assert spawn.calls == [lead_rt, own]
    assert fresh_table.leases_on_runtime(lead_rt) == 1
