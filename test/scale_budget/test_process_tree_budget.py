"""Process-tree walk budget (F8): a full walk of a large harness tree forks nothing.

``_get_child_pids`` walks a harness root's descendants on every teardown, reap and
memory read. On Linux the kernel lists each task's children in
``/proc/<pid>/task/<tid>/children``. The walk here runs over a fake ``/proc`` of 300
roots with 5 leaves each, and counts every ``subprocess`` call the walk makes.
"""

from __future__ import annotations

import os
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from .conftest import harness_fail, optional_module

pytestmark = pytest.mark.scale_budget

ROOTS = 300
LEAVES = 5


def _fake_proc(tmp_path: Path) -> tuple[Path, int, int]:
    """A ``/proc`` with one top pid, 300 roots under it and 5 leaves under each root.

    Every pid gets a ``task/<pid>/children`` file, the leaves' files empty, which is
    what the kernel shows for a process with no children. ``self`` gets one too,
    for this process and its calling thread, so a walk that first checks the
    kernel offers the children interface (``/proc/self/task/<tid>/children``)
    finds it here as it would on a real Linux host.
    """
    proc = tmp_path / "proc"
    top = 10
    roots = list(range(1000, 1000 + ROOTS))
    next_pid = 100_000

    def task(pid: int, children: list[int]) -> None:
        d = proc / str(pid) / "task" / str(pid)
        d.mkdir(parents=True)
        (d / "children").write_text(" ".join(str(c) for c in children))

    task(top, roots)
    for tid in {os.getpid(), threading.get_native_id()}:
        d = proc / "self" / "task" / str(tid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "children").write_text("")
    total = 0
    for root in roots:
        leaves = list(range(next_pid, next_pid + LEAVES))
        next_pid += LEAVES
        task(root, leaves)
        total += 1
        for leaf in leaves:
            task(leaf, [])
            total += 1
    return proc, top, total


CHILDREN_INTERFACE_CACHE = "_CHILDREN_INTERFACE"


def _pin_children_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the walk's once-per-process "kernel has the children files" answer to yes.

    A walk that detects the interface once and caches it in a module-level name
    would otherwise keep whatever the first caller in this xdist worker saw, and a
    detection that read this fake ``/proc`` instead of the real kernel's would
    cache "no interface" and leave the F8 budget failing on a correct fix. Code
    without the cache has no such name, and nothing is pinned.
    """
    module = optional_module("kiro_crew.acp.runtime_process_tree")
    if module is not None and hasattr(module, CHILDREN_INTERFACE_CACHE):
        monkeypatch.setattr(module, CHILDREN_INTERFACE_CACHE, True)


@pytest.fixture
def walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The real walk over the fake ``/proc``, with every subprocess call counted.

    The seam is the ``kiro_crew.acp.client`` facade: ``_direct_children`` in
    ``runtime_process_tree.py`` imports ``Path``, ``sys``, ``subprocess_mod`` and
    ``platform_compat`` from it at call time. The fixture walks the top pid once
    before handing over, so a walk that does not read these names fails here as
    a harness break instead of as a budget's expected failure.
    """
    from kiro_crew.acp import client as client_facade
    from kiro_crew.acp import runtime_process_tree

    proc, top, total = _fake_proc(tmp_path)
    real_path = client_facade.Path

    def _path(arg: object, *rest: object) -> Path:
        text = str(arg)
        if text.startswith("/proc/"):
            return proc / text[len("/proc/") :]
        return real_path(arg, *rest)

    spawns = MagicMock()
    spawns.check_output.side_effect = lambda *a, **k: b""
    spawns.run.side_effect = AssertionError("the walk ran a process")
    spawns.DEVNULL = -3
    spawns.TimeoutExpired = TimeoutError
    monkeypatch.setattr(client_facade, "Path", _path)
    monkeypatch.setattr(client_facade, "sys", types.SimpleNamespace(platform="linux"))
    monkeypatch.setattr(client_facade, "subprocess_mod", spawns)
    monkeypatch.setattr(client_facade.platform_compat, "IS_WINDOWS", False)
    _pin_children_interface(monkeypatch)
    if not runtime_process_tree._get_child_pids(top):
        harness_fail("the walk found no descendant in the fake /proc; the facade seam moved")
    spawns.reset_mock()
    return types.SimpleNamespace(
        run=lambda pid=top: runtime_process_tree._get_child_pids(pid),
        spawns=spawns,
        total=total,
        top=top,
    )


def _spawn_count(spawns: MagicMock) -> int:
    return spawns.check_output.call_count + spawns.run.call_count


def test_a_full_tree_walk_finds_every_descendant(walk) -> None:
    """Ratchet on main: the walk returns all 1,800 descendants of the top pid."""
    found = walk.run()
    assert (
        len(found) == walk.total == ROOTS * (LEAVES + 1)
    ), f"F8: the walk found {len(found)} of {walk.total} descendants"
    assert len(set(found)) == len(found), "F8: the walk reported a pid twice"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F8: main forks pgrep for every leaf whose children file is empty",
)
def test_a_full_tree_walk_spawns_nothing(walk) -> None:
    """300 roots x 5 leaves: 0 subprocesses, because every children file was readable."""
    walk.run()
    spawned = _spawn_count(walk.spawns)
    assert spawned == 0, (
        f"F8: a {ROOTS}x{LEAVES} /proc walk forked {spawned} subprocesses (budget 0); "
        "an empty kernel children file is a leaf, not a reason to run pgrep"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F8: a pid with no /proc entry still falls through to pgrep",
)
def test_a_vanished_pid_is_a_leaf_without_pgrep(walk) -> None:
    """A pid with no ``/proc/<pid>/task`` has exited: 0 children and 0 spawns."""
    assert walk.run(4_000_000) == []
    spawned = _spawn_count(walk.spawns)
    assert spawned == 0, f"F8: a vanished pid forked {spawned} subprocesses (budget 0)"
