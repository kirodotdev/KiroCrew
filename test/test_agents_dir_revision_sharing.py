"""One agents-directory revision shared by every memo and snapshot, where sharing is sound.

A :class:`~kiro_crew.agent_discovery.AgentsDirMemo` hit proves freshness with a
walk of the whole directory (one ``stat`` per spec), and every holder of a memo or
snapshot proves it for itself. Two sharings bound those walks:

- :func:`~kiro_crew.agent_discovery.agents_dir_revision_scope`: an operation that
  resolves against a directory more than once opens a scope, and every lookup in
  it reads the revision walked first in the scope. Hold-free and keyed on the
  in-process spec generation, so nothing a lookup saw before is hidden: a spec
  edited from outside the process is seen by the next scope, exactly as a write
  landing during one walk is seen by the next walk.
- ``agent.agents_dir_revision_hold_ms``, default 0: with a hold, a revision walked
  within it answers every lookup without a walk. An in-process write is seen at
  once (the generation keys the held entry); a write from outside the process is
  seen within the hold. Off by default, so every default lookup walks as before.

Each pin here counts the directory walks (``scandir``) and entry ``stat`` calls the
real revision makes, so a change that walks per hit again fails these.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from config_loader_helpers import _load_from_dict

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


@pytest.fixture
def hold() -> Iterator[Any]:
    """Publish a hold for one test, as a config load does, and restore 0 after it."""
    from kiro_crew.config.loader_snapshots import publish_agents_dir_revision_hold_ms

    with dir_revision._HELD_REVISIONS_LOCK:
        dir_revision._HELD_REVISIONS.clear()

    def publish(hold_ms: object, ticket: int | None = None) -> None:
        # A ticket above any config load this test triggers: the loads' own publish
        # (the file's default, 0) is then the older one and is dropped, which is the
        # slot's contract, so the hold under test stays in force.
        publish_agents_dir_revision_hold_ms(hold_ms, 10**9 if ticket is None else ticket)

    yield publish
    # Back to a cold slot: no hold, and a ticket every real load outranks.
    publish_agents_dir_revision_hold_ms(0, 10**9 + 1)
    _reset_hold_ticket()


def _reset_hold_ticket() -> None:
    from kiro_crew.config import loader_snapshots

    with loader_snapshots._AGENTS_DIR_REVISION_HOLD_LOCK:
        loader_snapshots._AGENTS_DIR_REVISION_HOLD_TICKET = 0
    # A later test starts with nothing held, as a process that never had a hold does.
    with dir_revision._HELD_REVISIONS_LOCK:
        dir_revision._HELD_REVISIONS.clear()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """A monotonic clock the test moves; the wall clock stays real."""
    now = [1_000_000_000]
    monkeypatch.setattr(
        dir_revision, "time", SimpleNamespace(time_ns=time.time_ns, monotonic_ns=lambda: now[0])
    )
    return now


def _holders() -> list[agent_discovery.AgentsDirMemo[int]]:
    return [agent_discovery.AgentsDirMemo() for _ in range(HOLDERS)]


def _warm(agents: Path, holders: list[agent_discovery.AgentsDirMemo[int]]) -> None:
    for memo in holders:
        memo.get(agents, "agent-001", lambda: 1)


# --- the default: no hold, every lookup outside a scope walks as before ----------


def test_the_hold_is_off_by_default() -> None:
    from kiro_crew.config import KiroCrewConfig

    assert KiroCrewConfig().agent.agents_dir_revision_hold_ms == 0
    assert agent_discovery.agents_dir_revision_hold_ns() == 0


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


# --- the opt-in hold -----------------------------------------------------------------


def test_a_hold_serves_every_hit_without_a_walk(
    agents: Path, walks: dict[str, int], hold: Any, clock: list[int]
) -> None:
    hold(500)
    holders = _holders()
    _warm(agents, holders)
    walks.update(scandir=0, stat=0)

    for _ in range(RESOLVES):
        for memo in holders:
            assert memo.get(agents, "agent-001", lambda: 2) == 1

    assert walks == {"scandir": 0, "stat": 0}


def test_an_outside_write_is_seen_within_the_hold(
    agents: Path, hold: Any, clock: list[int]
) -> None:
    """The bound a hold trades for: an edit no in-process writer announced is
    served stale until the hold ends, and seen on the first lookup after it."""
    hold(500)
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert memo.get(agents, "agent-001", compute) == 1
    (agents / "agent-001.json").write_text(json.dumps({"name": "edited"}), encoding="utf-8")
    clock[0] += 499_999_999
    assert memo.get(agents, "agent-001", compute) == 1, "the hold was not honoured"
    clock[0] += 1
    assert memo.get(agents, "agent-001", compute) == 2, "the edit outlived the hold"


def test_an_in_process_write_is_seen_at_once_under_the_hold(agents: Path, hold: Any) -> None:
    hold(500)
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert memo.get(agents, "agent-001", compute) == 1
    (agents / "agent-001.json").write_text(json.dumps({"name": "edited"}), encoding="utf-8")
    agent_discovery.clear_list_agents_cache()
    assert memo.get(agents, "agent-001", compute) == 2


def test_a_write_during_the_read_is_not_stored_under_the_hold(agents: Path, hold: Any) -> None:
    hold(500)
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    calls = 0

    def compute_and_write() -> int:
        nonlocal calls
        calls += 1
        (agents / f"newcomer-{calls}.json").write_text(json.dumps({"name": "n"}), encoding="utf-8")
        return calls

    assert memo.get(agents, "agent-001", compute_and_write) == 1
    assert memo.get(agents, "agent-001", compute_and_write) == 2, "a racing write was stored"


def test_the_hold_is_clamped_below_the_racy_window(hold: Any) -> None:
    """An entry written during a hold must still be young at the next walk, so it
    is pinned on its bytes there; the loader and the publisher clamp alike."""
    from kiro_crew.config.sections_limits import AGENTS_DIR_REVISION_HOLD_MS_MAX

    # The shipped window, not the one the module fixture zeroes for these tests.
    shipped_racy_window_ns = 2_000_000_000
    assert AGENTS_DIR_REVISION_HOLD_MS_MAX * 1_000_000 < shipped_racy_window_ns
    hold(5000)
    assert (
        agent_discovery.agents_dir_revision_hold_ns() == AGENTS_DIR_REVISION_HOLD_MS_MAX * 1_000_000
    )
    hold(-7)
    assert agent_discovery.agents_dir_revision_hold_ns() == 0
    hold(True)
    assert agent_discovery.agents_dir_revision_hold_ns() == 0, "a bool is not a hold"
    hold("250")
    assert agent_discovery.agents_dir_revision_hold_ns() == 0, "a string is not a hold"

    assert (
        _load_from_dict(
            {"agent": {"agents_dir_revision_hold_ms": 5000}}
        ).agent.agents_dir_revision_hold_ms
        == 1000
    )


def test_a_config_load_publishes_the_hold(hold: Any) -> None:
    """Every load publishes, so a settings write reaches a running gateway through
    the config watcher's reload and the lookup path never reads config.json."""
    _reset_hold_ticket()
    assert (
        _load_from_dict(
            {"agent": {"agents_dir_revision_hold_ms": 250}}
        ).agent.agents_dir_revision_hold_ms
        == 250
    )
    assert agent_discovery.agents_dir_revision_hold_ns() == 250_000_000
    _load_from_dict({})
    assert agent_discovery.agents_dir_revision_hold_ns() == 0


def test_a_changed_hold_drops_what_was_held(
    agents: Path, walks: dict[str, int], hold: Any, clock: list[int]
) -> None:
    hold(500)
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()
    _warm(agents, [memo])
    hold(200)
    walks.update(scandir=0, stat=0)
    memo.get(agents, "agent-001", lambda: 1)
    assert walks["scandir"] == 1, "a revision held under the old bound was served"


def test_an_expired_held_revision_is_dropped_on_the_next_walk(
    tmp_path: Path, hold: Any, clock: list[int]
) -> None:
    """The held store keeps only the directories walked within one hold: an
    entry the hold has passed can never be served, so the next store drops it."""
    hold(500)
    first, second = tmp_path / "first", tmp_path / "second"
    for folder in (first, second):
        folder.mkdir()
        (folder / "agent.json").write_text(json.dumps({"name": "agent"}), encoding="utf-8")
    memo: agent_discovery.AgentsDirMemo[int] = agent_discovery.AgentsDirMemo()

    memo.get(first, "agent", lambda: 1)
    assert set(dir_revision._HELD_REVISIONS) == {str(first)}
    clock[0] += 500_000_000
    memo.get(second, "agent", lambda: 1)
    assert set(dir_revision._HELD_REVISIONS) == {str(second)}, "an expired entry was retained"


def test_an_app_deregistration_is_seen_at_once_under_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: Any
) -> None:
    """Disabling an app unlinks its specs in process without going through a spec
    writer that announces itself, so the deregistration moves the spec generation
    itself: a memo answer held from before the unlink is not served after it."""
    from kiro_crew.apps import bridges

    hold(500)
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "someapp--main.json").write_text(json.dumps({"name": "main"}), encoding="utf-8")
    monkeypatch.setattr(bridges, "_kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(bridges, "schedule_materialized_agents_refresh", lambda: None)
    memo: agent_discovery.AgentsDirMemo[list[str]] = agent_discovery.AgentsDirMemo()

    def names() -> list[str]:
        return sorted(p.name for p in agents.iterdir())

    assert memo.get(agents, "main", names) == ["someapp--main.json"]
    assert bridges._deregister_agents("someapp") == 1
    assert memo.get(agents, "main", names) == [], "the removed spec was served from the hold"


# --- every in-process spec writer moves the generation at the write -----------------


def _counting_memo(agents: Path) -> tuple[agent_discovery.AgentsDirMemo[list[str]], Any]:
    memo: agent_discovery.AgentsDirMemo[list[str]] = agent_discovery.AgentsDirMemo()

    def names() -> list[str]:
        return sorted(p.name for p in agents.iterdir() if p.suffix == ".json")

    return memo, names


def test_the_json_spec_primitive_is_seen_at_once_under_the_hold(tmp_path: Path, hold: Any) -> None:
    """`_atomic_json_write` (and `write_owner_derived_spec` through the same
    primitive) announces the write, so the next resolve sees the new spec."""
    import kiro_crew.agent as agent_mod

    hold(1000)
    agents = tmp_path / "agents"
    agents.mkdir()
    spec = agents / "worker.json"
    agent_mod._atomic_json_write(spec, {"name": "worker", "tools": ["a"]})
    memo: agent_discovery.AgentsDirMemo[list[str]] = agent_discovery.AgentsDirMemo()

    def tools() -> list[str]:
        return json.loads(spec.read_text(encoding="utf-8"))["tools"]

    assert memo.get(agents, "worker", tools) == ["a"]
    agent_mod._atomic_json_write(spec, {"name": "worker", "tools": ["b"]})
    assert memo.get(agents, "worker", tools) == ["b"], "the pre-write spec was served"


def test_write_mate_spec_is_seen_at_once_under_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: Any
) -> None:
    import kiro_crew.config.paths as paths_mod
    from kiro_crew import mate_config

    hold(1000)
    kiro = tmp_path / "home"
    (kiro / "agents").mkdir(parents=True)
    mates = kiro / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_home", lambda: kiro)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: kiro / "agents")
    monkeypatch.setattr(paths_mod, "data_home", lambda: tmp_path / "data")
    from kiro_crew.config import loader

    monkeypatch.setattr(loader, "publish_mate_agents", lambda names: None)
    memo, names = _counting_memo(mates)

    assert memo.get(mates, "zxc", names) == []
    mate_config.write_mate_spec("zxc", {"name": "zxc", "tools": []})
    assert memo.get(mates, "zxc", names) == ["zxc.json"], "the pre-write listing was served"


def test_the_service_agent_unlink_is_seen_at_once_under_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hold: Any
) -> None:
    import kiro_crew.agent as agent_mod
    from kiro_crew.agent_materialization import service_agents

    hold(1000)
    agents = tmp_path / "agents"
    agents.mkdir()
    config = {"name": "kirocrew-worker", "description": "the worker"}
    (agents / "kirocrew-worker.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: agents)
    memo, names = _counting_memo(agents)

    assert memo.get(agents, "kirocrew-worker", names) == ["kirocrew-worker.json"]
    service_agents._remove_user_level_copy("kirocrew-worker.json", config)
    assert memo.get(agents, "kirocrew-worker", names) == [], "the unlinked spec was served"


def test_a_non_spec_write_through_the_primitive_moves_nothing(tmp_path: Path) -> None:
    """The primitive also writes non-spec JSON; only a spec name moves the generation."""
    import kiro_crew.agent as agent_mod

    before = agent_discovery.spec_cache_generation()
    agent_mod._atomic_json_write(tmp_path / "rows.template", {"a": 1})
    assert agent_discovery.spec_cache_generation() == before
    agent_mod._atomic_json_write(tmp_path / "spec.json", {"a": 1})
    assert agent_discovery.spec_cache_generation() == before + 1


#: A module that writes into an agents directory names one of these helpers.
_AGENTS_DIR_HELPERS = (
    "kiro_agents_dir(",
    "kiro_agents_dir_path(",
    "mate_agents_dir(",
    "mate_config_path(",
)
#: Raw write, unlink or replace sites in those modules that never touch a spec,
#: by a fragment within three lines of the call. Anything else must announce
#: within the next eight lines.
_NOT_A_SPEC_SITE = (
    "os.unlink(tmp_name)",  # the primitive's own temp file
    "os.unlink(tmp)",  # a seal's temp file
    "_marker(",  # materialization markers beside the specs
    "forwarder",  # the launcher's forwarder script
    "staged.unlink",  # the launcher's staged binary
    "link.unlink()",  # the launcher's link
    "_owners_path()",  # the crewmate owner record
    "_ledger_path()",  # the crewmate migration ledger
    "= _path()",  # revoked-approvals.json and tool-access.json beside the mates folder
    "record_path",  # the app template record (.template)
    "target_dir / agent_path.name",  # a rendered copy outside the agents dir
    "unlink_verified_by_name",  # an app lock file
    "overlay.unlink",  # an MCP overlay
    "stale.unlink",  # a .bak.* beside a spec, or an MCP overlay
    "entry.name.endswith(_TEMPLATE_RECORD_SUFFIX)",
    "_mcp_json_path()",  # the global mcp.json
    "crumb",  # the data-home crumb
    "alias_path",  # a skill-view alias, which the walks leave out
    "locked_settings",  # kiro-cli settings beside the alias
    "_sandbox_cleanup",  # a sandbox cleanup script
    "stderr_file",  # a cron script's stderr capture
    "dst.unlink",  # a container bundle copy
    "launcher.write_text",  # the test harness launcher
    "Path(recorded)",  # a mint's recorded callback file
)


def test_every_raw_spec_writer_announces_its_write() -> None:
    """Ratchet: in every module that names an agents directory, a raw
    `atomic_write`/`unlink`/`replace` of a spec is followed by `note_spec_change`
    (or `clear_list_agents_cache`), so the generation moves at every writer."""
    import re

    root = Path(agent_discovery.__file__).resolve().parents[2]
    site = re.compile(
        r"\b(atomic_write\(|\.unlink\(|os\.unlink\(|os\.remove\(|os\.replace\(|\.rename\(|\.write_text\()"
    )
    missing: list[str] = []
    for file in sorted((root / "src" / "kiro_crew").rglob("*.py")):
        rel = file.relative_to(root).as_posix()
        if "/builtins/" in rel or "/tests/" in rel or "/testing/" in rel:
            continue
        text = file.read_text(encoding="utf-8")
        if not any(helper in text for helper in _AGENTS_DIR_HELPERS):
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not site.search(line) or stripped.startswith(("#", '"', "def ")):
                continue
            nearby = "\n".join(lines[max(0, i - 3) : i + 3])
            if any(fragment in nearby for fragment in _NOT_A_SPEC_SITE):
                continue
            window = "\n".join(lines[i : i + 8])
            if "note_spec_change(" not in window and "clear_list_agents_cache(" not in window:
                missing.append(f"{rel}:{i + 1}: {stripped}")
    assert missing == [], "spec writers without note_spec_change:\n" + "\n".join(missing)


def test_an_older_config_load_cannot_put_a_hold_back(hold: Any) -> None:
    """Two loads interleave: the newer publishes 0, then the older finishes and
    publishes 1000. The older ticket is dropped under the lock."""
    from kiro_crew.config.loader_snapshots import next_config_load_ticket

    _reset_hold_ticket()
    older = next_config_load_ticket()
    newer = next_config_load_ticket()
    hold(1000, older)
    assert agent_discovery.agents_dir_revision_hold_ns() == 1_000_000_000
    hold(0, newer)
    assert agent_discovery.agents_dir_revision_hold_ns() == 0
    hold(1000, older)
    assert agent_discovery.agents_dir_revision_hold_ns() == 0, "an older load re-enabled the hold"


def test_a_config_load_does_not_import_the_discovery_family() -> None:
    """The hold is published into ``config.loader_snapshots``, never pushed through
    ``agent_discovery``: a config load must not pull the discovery family into a
    process that only lists MCP tools (the import-budget ceilings pin that)."""
    import subprocess
    import sys

    from kiro_crew.subprocess_utf8 import UTF8_TEXT
    from kiro_crew.testing.wait import default_timeout

    probe = (
        "import sys\n"
        "from kiro_crew.config import KiroCrewConfig\n"
        "KiroCrewConfig.load()\n"
        "print(sorted(m for m in sys.modules if m.startswith('kiro_crew.agent_discovery')))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        check=False,
        timeout=default_timeout(),
        **UTF8_TEXT,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip() == "[]", out.stdout


# --- the five holders the issue names, measured as the gateway reaches them -----------
#
# The tool-policy read, the spawn-allowlist resolve and the snapshot refresh each
# run in their own ``asyncio.to_thread`` hop, so no request-level scope can span
# them: with no hold, each walks the directory once per operation, as on main.
# Sharing across holders is what the hold is for. The pins below fix both bounds.


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


def test_five_holders_without_a_hold_walk_once_each(home: Path, walks: dict[str, int]) -> None:
    """The bound at the shipped default: a warmed request through the five holders
    walks the directory once per holder whose lookup is its own thread hop (four:
    the KAS resolve shares its two lookups; the bare lookup is the fifth)."""
    _five_holders(home)
    walks.update(scandir=0, stat=0)
    _five_holders(home)
    assert walks["stat"] == 5 * SPECS, f'{walks["stat"] // SPECS} walks, not 5'


def test_five_holders_under_a_hold_walk_once_per_request(
    home: Path, walks: dict[str, int], hold: Any, clock: list[int]
) -> None:
    """With a hold, the same request walks the directory once: every later holder
    reads the held revision. Clock-driven, so the walk's own duration is not counted."""
    hold(1000)
    _five_holders(home)
    clock[0] += 1_000_000_000
    walks.update(scandir=0, stat=0)
    _five_holders(home)
    assert walks["stat"] == SPECS, f'{walks["stat"] // SPECS} walks, not 1'
