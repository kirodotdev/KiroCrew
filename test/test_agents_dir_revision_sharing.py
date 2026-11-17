"""One agents-directory revision shared by every memo and snapshot, where sharing is sound.

A :class:`~kiro_crew.agent_discovery.AgentsDirMemo` hit proves freshness with a
walk of the whole directory (one ``stat`` per spec), and every holder of a memo or
snapshot proves it for itself. One sharing bounds those walks:

- :func:`~kiro_crew.agent_discovery.agents_dir_revision_scope`: an operation that
  resolves against a directory more than once opens a scope, and every lookup in
  it reads the revision walked first in the scope. Keyed on the
  in-process spec generation, so nothing a lookup saw before is hidden: a spec
  edited from outside the process is seen by the next scope, exactly as a write
  landing during one walk is seen by the next walk.

Each pin here counts the directory walks (``scandir``) and entry ``stat`` calls the
real revision makes, so a change that walks per hit again fails these.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import agent_discovery
from kiro_crew.agent_discovery_parts import dir_revision

SPECS = 40
HOLDERS = 4
RESOLVES = 20


@pytest.fixture(autouse=True)
def _memo_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The memo on (off on Windows by default), every entry old enough to be pinned
    on metadata alone, and metadata read through ``DirEntry.stat`` on every
    platform (Windows pins every entry on its bytes through ``os.stat`` otherwise),
    so a walk is one ``scandir`` plus one counted ``stat`` per spec everywhere."""
    _floor_monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", True)
    _floor_monkeypatch.setattr(agent_discovery, "_AGENTS_DIR_RACY_WINDOW_NS", 0)
    _floor_monkeypatch.setattr(agent_discovery, "_PIN_EVERY_ENTRY_ON_BYTES", False)


@pytest.fixture
def agents(tmp_path: Path) -> Path:
    for i in range(SPECS):
        (tmp_path / f"agent-{i:03d}.json").write_text(
            json.dumps({"name": f"agent-{i}"}), encoding="utf-8"
        )
    return tmp_path


class _CountedEntry:
    def __init__(self, entry: Any, counts: dict[str, int]) -> None:
        self._entry = entry
        self._counts = counts

    def __getattr__(self, name: str) -> Any:
        return getattr(self._entry, name)

    @property
    def name(self) -> str:
        return self._entry.name

    def stat(self, **kwargs: Any) -> os.stat_result:
        self._counts["stat"] += 1
        return self._entry.stat(**kwargs)


class _CountedScan:
    def __init__(self, scan: Any, counts: dict[str, int]) -> None:
        self._scan = scan
        self._counts = counts

    def __enter__(self) -> "_CountedScan":
        return self

    def __exit__(self, *exc: object) -> bool:
        self._scan.close()
        return False

    def __iter__(self) -> "_CountedScan":
        return self

    def __next__(self) -> Any:
        # ``os.walk`` calls ``next`` on the scandir object itself.
        return _CountedEntry(next(self._scan), self._counts)


class _CountingOs:
    """``os`` as the discovery family sees it: every name is the real one except
    ``scandir``, which is counted. Patched through the ``agent_discovery`` facade,
    which mirrors the binding to each owner that reads it and to nothing outside the
    family, so a ``scandir`` from another module or thread (the log writer's segment
    scan, say) leaves the counts alone."""

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def scandir(self, path: Any = ".", *args: Any, **kwargs: Any) -> Any:
        self._counts["scandir"] += 1
        return _CountedScan(os.scandir(path, *args, **kwargs), self._counts)


@pytest.fixture
def walks(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """``scandir`` calls and entry ``stat`` calls the revision walks make."""
    counts = {"scandir": 0, "stat": 0}
    monkeypatch.setattr(agent_discovery, "os", _CountingOs(counts))
    return counts


def test_the_walk_counter_sees_only_the_spec_walk(walks: dict[str, int], agents: Path) -> None:
    """A ``scandir`` from anywhere but the spec walk is not a counted walk, so a
    background scan cannot move an exact walk count."""
    with os.scandir(agents) as it:
        for entry in it:
            entry.stat()
    assert walks == {"scandir": 0, "stat": 0}
    agent_discovery.fresh_agents_dir_revision(agents)
    assert walks == {"scandir": 1, "stat": SPECS}


def _holders() -> list[agent_discovery.AgentsDirMemo[int]]:
    return [agent_discovery.AgentsDirMemo() for _ in range(HOLDERS)]


def _warm(agents: Path, holders: list[agent_discovery.AgentsDirMemo[int]]) -> None:
    for memo in holders:
        memo.get(agents, "agent-001", lambda: 1)


# --- the default: every lookup outside a scope walks as before -------------------


def test_outside_a_scope_every_hit_still_walks(agents: Path, walks: dict[str, int]) -> None:
    """The pin of today's behaviour: a hit proves freshness with its own walk."""
    holders = _holders()
    _warm(agents, holders)
    walks.update(scandir=0, stat=0)

    for _ in range(RESOLVES):
        for memo in holders:
            memo.get(agents, "agent-001", lambda: 1)

    assert walks == {"scandir": RESOLVES * HOLDERS, "stat": RESOLVES * HOLDERS * SPECS}


# --- the scope: one walk per directory per resolve -------------------------------


def test_a_scope_walks_the_directory_once_for_every_holder(
    agents: Path, walks: dict[str, int]
) -> None:
    """Twenty resolves, each touching four holders: twenty walks, not eighty."""
    holders = _holders()
    _warm(agents, holders)
    walks.update(scandir=0, stat=0)

    for _ in range(RESOLVES):
        with agent_discovery.agents_dir_revision_scope():
            for memo in holders:
                assert memo.get(agents, "agent-001", lambda: 2) == 1

    assert walks == {"scandir": RESOLVES, "stat": RESOLVES * SPECS}


def test_a_scope_does_not_hide_an_in_process_write(agents: Path) -> None:
    """A write that moves the spec generation is seen inside the open scope."""
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    with agent_discovery.agents_dir_revision_scope():
        assert memo.get(agents, "agent-001", compute) == 1
        assert memo.get(agents, "agent-001", compute) == 1
        (agents / "agent-001.json").write_text(json.dumps({"name": "edited"}), encoding="utf-8")
        agent_discovery.clear_list_agents_cache()
        assert memo.get(agents, "agent-001", compute) == 2


def test_the_next_scope_sees_an_outside_write(agents: Path) -> None:
    """A scope is one resolve: an outside edit is seen by the next one, as today."""
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    with agent_discovery.agents_dir_revision_scope():
        assert memo.get(agents, "agent-001", compute) == 1
    (agents / "agent-001.json").write_text(json.dumps({"name": "edited"}), encoding="utf-8")
    with agent_discovery.agents_dir_revision_scope():
        assert memo.get(agents, "agent-001", compute) == 2


def test_a_write_during_the_read_is_not_stored_inside_a_scope(agents: Path) -> None:
    """The check after ``compute`` walks afresh, so the scope cannot vouch for a
    directory the read itself changed."""
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute_and_write() -> int:
        nonlocal calls
        calls += 1
        (agents / f"newcomer-{calls}.json").write_text(json.dumps({"name": "n"}), encoding="utf-8")
        return calls

    with agent_discovery.agents_dir_revision_scope():
        assert memo.get(agents, "agent-001", compute_and_write) == 1
        assert memo.get(agents, "agent-001", compute_and_write) == 2, "a racing write was stored"


def test_a_nested_scope_shares_its_callers_walk(agents: Path, walks: dict[str, int]) -> None:
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    _warm(agents, [memo])
    walks.update(scandir=0, stat=0)

    with agent_discovery.agents_dir_revision_scope():
        memo.get(agents, "agent-001", lambda: 1)
        with agent_discovery.agents_dir_revision_scope():
            memo.get(agents, "agent-001", lambda: 1)
        memo.get(agents, "agent-001", lambda: 1)

    assert walks["scandir"] == 1
    assert dir_revision._REVISION_SCOPE.get() is None, "the scope outlived its block"


def test_the_scope_decorator_opens_a_fresh_scope_per_call() -> None:
    calls = 0

    @agent_discovery.in_agents_dir_revision_scope
    def resolve() -> None:
        nonlocal calls
        calls += 1
        scope = dir_revision._REVISION_SCOPE.get()
        assert scope is not None, "the decorated call ran outside a scope"
        assert "marker" not in scope, "a second call reused the first call's scope"
        scope["marker"] = None

    resolve()
    resolve()
    assert calls == 2
    assert dir_revision._REVISION_SCOPE.get() is None, "the scope outlived its call"


# --- the five holders the issue names, measured as the gateway reaches them -----------
#
# The tool-policy read, the spawn-allowlist resolve and the snapshot refresh are
# reached from separate call paths (a session start, the spawn schema, the
# discovery executor), so no one operation opens a scope across them: each walks
# the directory once per operation, as on main. The pin below fixes that bound.


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """One user-level agents directory of SPECS specs, wired into every holder's
    path helper, and the crewmate directory beside it (empty)."""
    import kiro_crew.config.paths as paths_mod
    from kiro_crew.config import loader, loader_snapshots
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    agents = tmp_path / "agents"
    agents.mkdir()
    (tmp_path / "mates" / "agents").mkdir(parents=True)
    for i in range(SPECS):
        (agents / f"agent-{i:03d}.json").write_text(
            json.dumps({"name": f"agent-{i}", "managedToolPolicy": {"exclude": []}}),
            encoding="utf-8",
        )
    monkeypatch.setattr(agent_discovery, "_KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: tmp_path / "mates" / "agents")
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: tmp_path / "mates" / "agents")
    monkeypatch.setattr(sessions_mod, "_TOOL_POLICY_MEMO", agent_discovery.AgentsDirMemo())
    monkeypatch.setattr(loader_snapshots, "_MATERIALIZED_MEMO", None)
    return agents


def _five_holders(home: Path) -> None:
    """One gateway request's reads, as the five holders make them: no outer scope."""
    from kiro_crew import subagent as sa
    from kiro_crew.acp.harness import kas as kas_harness
    from kiro_crew.config import loader
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    kas_harness.resolve_projected_spec(home, "agent-001", None)
    sessions_mod._read_managed_tool_policy_sync(home, "agent-001")
    sa.parent_spawn_allowlists("agent-001")
    loader.refresh_materialized_agents()
    agent_discovery.shared_agents_dir_revision(home)


def test_a_tool_policy_miss_costs_two_walks_and_one_read(home: Path, walks: dict[str, int]) -> None:
    """The first evaluation: one revision walk before, the prefill's read of every
    spec, one fresh walk after. A hit costs one walk."""
    from kiro_crew.dashboard.handlers import sessions as sessions_mod

    walks.update(scandir=0, stat=0)
    sessions_mod._read_managed_tool_policy_sync(home, "agent-001")
    # One shared revision walk before the read, the prefill's own read of every
    # spec (one stat each, the parse), and one fresh walk after the prefill.
    assert walks["stat"] == 3 * SPECS, "the prefill walked the directory a fourth time"


def test_a_spawn_allowlist_miss_costs_two_walks(home: Path, walks: dict[str, int]) -> None:
    from kiro_crew import subagent as sa

    walks.update(scandir=0, stat=0)
    sa.parent_spawn_allowlists("agent-001")
    # One shared walk before the scan, one fresh walk after it.
    assert walks["stat"] == 2 * SPECS, "a spawn-allowlist miss walked more than twice"


def test_a_snapshot_scan_costs_two_walks(home: Path, walks: dict[str, int]) -> None:
    from kiro_crew.config import loader

    walks.update(scandir=0, stat=0)
    loader.refresh_materialized_agents()
    # One shared walk before the scan, one fresh walk after it; the crewmate dir is empty.
    assert walks["stat"] == 2 * SPECS, "a snapshot scan walked more than twice"


def test_five_holders_walk_once_each(home: Path, walks: dict[str, int]) -> None:
    """The bound outside a scope: a warmed request through the five holders
    walks the directory once per holder whose lookup is its own thread hop (four:
    the KAS resolve shares its two lookups; the bare lookup is the fifth)."""
    _five_holders(home)
    walks.update(scandir=0, stat=0)
    _five_holders(home)
    assert walks["stat"] == 5 * SPECS, f'{walks["stat"] // SPECS} walks, not 5'
