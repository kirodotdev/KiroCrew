"""The catalog fetch rescans the agents directories only when they changed."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent_discovery
from kiro_crew.agent_discovery_parts import dir_revision, spec_reads
from kiro_crew.dashboard.handlers import agent_catalog

_BOUND = 30


_REAL_REVISION_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="agents_dir_revision cannot prove a revision on Windows; "
    "test_windows_scans_every_fetch covers that platform",
)


def _content_revision(directory: Path) -> object:
    """A revision that moves with every spec's name and bytes, on any platform."""
    return tuple(sorted((p.name, p.read_bytes()) for p in directory.glob("*.json")))


def _write_spec(directory: Path, name: str, description: str = "d") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"name": name, "description": description}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _memo_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The revision the stamp rests on is off on Windows by default; on here.

    Its young-entry cutoff is pinned to the fixture's start: a spec written here
    is young on every fetch, so a slow worker crossing the 2 s window between two
    fetches cannot move the stamp and change a scan count.
    """
    _floor_monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", True)
    frozen = time.time_ns()
    _floor_monkeypatch.setattr(dir_revision, "time", SimpleNamespace(time_ns=lambda: frozen))


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """A fresh gate over tmp directories, with the snapshot refresh counted, not run.

    The real refresh replaces the process-wide materialized snapshot, which other
    tests on this worker read; what the gate decides depends only on its inputs
    and on what the fake refresh reports.
    """
    user_dir = tmp_path / "kiro" / "agents"
    mate_dir = tmp_path / "kiro" / "mates" / "agents"
    user_dir.mkdir(parents=True)
    _write_spec(user_dir, "reviewer")
    state = SimpleNamespace(landed=True, skipped=frozenset(), generation=0, scans=0, ticket=0)

    def fake_refresh(*, heal_default):
        assert heal_default is False
        state.scans += 1
        if state.landed:
            state.ticket += 1
        return state.landed

    monkeypatch.setattr(agent_catalog, "kiro_agents_dir", lambda: user_dir)
    monkeypatch.setattr(agent_catalog, "mate_agents_dir", lambda: mate_dir)
    # What the gate decides from a revision is under test here, not how the
    # revision is taken, so it is one any platform and any clock can prove.
    monkeypatch.setattr(agent_catalog, "agents_dir_revision", _content_revision)
    monkeypatch.setattr(agent_catalog, "materialized_skipped_stems", lambda: state.skipped)
    monkeypatch.setattr(agent_catalog, "materialized_agents_generation", lambda: state.generation)
    monkeypatch.setattr(agent_catalog, "materialized_refresh_ticket", lambda: state.ticket)
    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", fake_refresh)
    return SimpleNamespace(
        gate=agent_catalog._RefreshGate(), state=state, user_dir=user_dir, mate_dir=mate_dir
    )


def test_repeated_fetches_with_unchanged_inputs_scan_once(gate):
    assert [gate.gate.refresh() for _ in range(4)] == [True, False, False, False]
    assert gate.state.scans == 1


@pytest.mark.parametrize("change", ["edit", "add", "remove", "mate-add", "publish"])
def test_any_input_change_rescans_on_the_next_fetch(gate, change):
    gate.gate.refresh()
    if change == "edit":
        # Same size, same tick: the revision digests a young entry's bytes.
        _write_spec(gate.user_dir, "reviewer", description="e")
    elif change == "add":
        _write_spec(gate.user_dir, "test-writer")
    elif change == "remove":
        (gate.user_dir / "reviewer.json").unlink()
    elif change == "mate-add":
        _write_spec(gate.mate_dir, "captain")
    else:
        gate.state.generation += 1
    assert gate.gate.refresh() is True
    assert gate.gate.refresh() is False
    assert gate.state.scans == 2


def test_out_of_order_refresh_is_not_recorded(gate):
    gate.state.landed = False
    gate.gate.refresh()
    gate.gate.refresh()
    assert gate.state.scans == 2
    gate.state.landed = True
    gate.gate.refresh()
    gate.gate.refresh()
    assert gate.state.scans == 3


def test_raising_refresh_records_nothing(gate, monkeypatch):
    def boom(*, heal_default):
        gate.state.scans += 1
        raise RuntimeError("scan failed")

    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", boom)
    with pytest.raises(RuntimeError):
        gate.gate.refresh()
    with pytest.raises(RuntimeError):
        gate.gate.refresh()
    assert gate.state.scans == 2


def test_incomplete_snapshot_from_an_ungated_caller_is_rescanned(gate):
    gate.gate.refresh()
    # Another caller's refresh installed a snapshot whose listing failed.
    gate.state.skipped = None
    assert gate.gate.refresh() is True
    # What that rescan skipped is unknown too, so nothing new is recorded.
    assert gate.gate.refresh() is True
    assert gate.state.scans == 3


def test_a_snapshot_another_caller_installed_is_rescanned(gate):
    gate.gate.refresh()
    # Turn admission, a lazy build or a scheduled refresh landed a scan.
    gate.state.ticket += 1
    assert gate.gate.refresh() is True
    assert gate.gate.refresh() is False
    assert gate.state.scans == 2


def test_a_refresh_that_lands_beside_the_scan_is_not_recorded(gate, monkeypatch):
    # Another caller's refresh lands after the gate's scan began, so the
    # installed snapshot may not be the one the gate read: nothing is recorded.
    def refresh_with_a_neighbour(*, heal_default):
        gate.state.scans += 1
        gate.state.ticket += 2 if gate.state.scans == 1 else 1
        return True

    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", refresh_with_a_neighbour)
    gate.gate.refresh()
    assert gate.gate.refresh() is True
    assert gate.gate.refresh() is False
    assert gate.state.scans == 2


def test_a_skipped_spec_is_kept_under_its_own_key(gate):
    gate.state.skipped = frozenset({"broken"})
    assert [gate.gate.refresh() for _ in range(3)] == [True, False, False]
    # A snapshot that skipped something else is not the recorded one.
    gate.state.skipped = frozenset({"other"})
    assert gate.gate.refresh() is True
    assert gate.state.scans == 2


def test_a_write_during_the_scan_is_not_recorded(gate, monkeypatch):
    def refresh_racing_a_write(*, heal_default):
        gate.state.scans += 1
        gate.state.ticket += 1
        if gate.state.scans == 1:
            _write_spec(gate.user_dir, "written-mid-scan")
        return True

    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", refresh_racing_a_write)
    gate.gate.refresh()
    assert gate.gate.refresh() is True
    assert gate.gate.refresh() is False
    assert gate.state.scans == 2


def test_a_declined_record_logs_once_per_stamp(gate, caplog):
    gate.state.landed = False
    with caplog.at_level("INFO", logger=agent_catalog.logger.name):
        for _ in range(3):
            gate.gate.refresh()
    assert sum("not recorded" in r.getMessage() for r in caplog.records) == 1


def test_an_unprovable_revision_always_rescans(gate, monkeypatch):
    monkeypatch.setattr(agent_catalog, "agents_dir_revision", lambda _d: None)
    for _ in range(3):
        gate.gate.refresh()
    assert gate.state.scans == 3


def test_none_stamp_scans_concurrently(gate, monkeypatch):
    # An unprovable stamp scans outside the lock, as every fetch did before.
    monkeypatch.setattr(agent_catalog, "agents_dir_revision", lambda _d: None)
    both_in = threading.Barrier(2, timeout=_BOUND)

    def meeting_refresh(*, heal_default):
        both_in.wait()
        return True

    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", meeting_refresh)
    errors: list[BaseException] = []

    def fetch() -> None:
        try:
            gate.gate.refresh()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=fetch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_BOUND)
    assert not any(t.is_alive() for t in threads)
    assert errors == []


def test_concurrent_misses_scan_once(gate, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def slow_refresh(*, heal_default):
        gate.state.scans += 1
        entered.set()
        assert release.wait(_BOUND)
        gate.state.ticket += 1
        return True

    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", slow_refresh)
    results: list[bool] = []
    first = threading.Thread(target=lambda: results.append(gate.gate.refresh()))
    first.start()
    assert entered.wait(_BOUND)
    second = threading.Thread(target=lambda: results.append(gate.gate.refresh()))
    second.start()
    assert second.is_alive()
    assert gate.state.scans == 1
    release.set()
    first.join(_BOUND)
    second.join(_BOUND)
    assert not first.is_alive() and not second.is_alive()
    assert sorted(results) == [False, True]
    assert gate.state.scans == 1


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows default leaves the memo off")
def test_windows_scans_every_fetch(gate, monkeypatch):
    monkeypatch.setattr(agent_catalog, "agents_dir_revision", agent_discovery.agents_dir_revision)
    monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", not spec_reads._WINDOWS)
    for _ in range(3):
        gate.gate.refresh()
    assert gate.state.scans == 3


@pytest.fixture
def heal(monkeypatch):
    """``_refresh_for_catalog`` with the gate's scan stubbed and the heal counted."""
    state = SimpleNamespace(complete=True, heals=0, scans=0)

    class _CountingGate:
        def refresh(self) -> bool:
            state.scans += 1
            return False

    def count_heal() -> bool:
        state.heals += 1
        return False

    monkeypatch.setattr(agent_catalog, "_REFRESH_GATE", _CountingGate())
    monkeypatch.setattr(agent_catalog, "materialized_snapshot_complete", lambda: state.complete)
    monkeypatch.setattr(agent_catalog, "reset_dangling_default_agent", count_heal)
    return state


def test_the_heal_runs_on_every_owner_fetch_even_on_a_gate_hit(heal):
    for _ in range(3):
        agent_catalog._refresh_for_catalog(owner=True)
    assert (heal.scans, heal.heals) == (3, 3)


def test_a_non_owner_fetch_never_heals(heal):
    agent_catalog._refresh_for_catalog(owner=False)
    assert (heal.scans, heal.heals) == (1, 0)


def test_the_heal_waits_for_a_complete_snapshot(heal):
    heal.complete = False
    agent_catalog._refresh_for_catalog(owner=True)
    assert heal.heals == 0


def _aged(directory: Path) -> None:
    """Every spec in *directory* older than the revision's young-entry window."""
    past = time.time() - 60
    for path in directory.glob("*.json"):
        os.utime(path, (past, past))


@pytest.fixture
def real_home(monkeypatch, tmp_path):
    """The real snapshot refresh over a tmp ``KIRO_HOME``, its scans counted."""
    from kiro_crew.config import loader

    kiro = tmp_path / "kiro"
    monkeypatch.setenv("KIRO_HOME", str(kiro))
    monkeypatch.setattr(agent_catalog, "_REFRESH_GATE", agent_catalog._RefreshGate())
    scans: list[bool] = []
    real = loader.refresh_materialized_agents

    def counted(*, heal_default):
        scans.append(heal_default)
        return real(heal_default=heal_default)

    monkeypatch.setattr(agent_catalog, "refresh_materialized_agents", counted)
    return SimpleNamespace(
        user_dir=kiro / "agents", mate_dir=kiro / "mates" / "agents", scans=scans
    )


@_REAL_REVISION_ONLY
def test_one_broken_spec_keeps_the_gate(real_home):
    for i in range(20):
        _write_spec(real_home.user_dir, f"agent-{i}")
    (real_home.user_dir / "broken.json").write_text("{not json", encoding="utf-8")
    _aged(real_home.user_dir)
    for _ in range(4):
        agent_catalog._REFRESH_GATE.refresh()
    assert real_home.scans == [False]


@_REAL_REVISION_ONLY
def test_owner_fetch_heals_after_the_workspace_spec_is_deleted(monkeypatch, tmp_path):
    # ``pkg`` runs ``reviewer`` in workspace ``proj``. The global spec is removed
    # while the checkout still declares it, so the heal declines. Then the
    # checkout's spec goes too: no input the stamp covers moved, the gate skips
    # the scan, and the owner's next fetch still resets the default.
    from kiro_crew.config import loader

    project = tmp_path / "proj"
    project_spec = _write_spec(project / ".kiro" / "agents", "reviewer")
    agents = tmp_path / "agents"
    _write_spec(agents, "kirocrew")
    global_spec = _write_spec(agents, "reviewer")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "default_agent": "pkg",
                "agents": {
                    "default": {"kiro_agent": "kirocrew"},
                    "pkg": {"kiro_agent": "reviewer", "workspace": "proj"},
                },
                "workspaces": {"proj": {"dir": str(project)}},
            }
        ),
        encoding="utf-8",
    )
    loader._invalidate_config_cache()
    monkeypatch.setattr(loader, "config_path", lambda: config)
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: tmp_path / "no-mates")
    monkeypatch.setattr(agent_catalog, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_catalog, "mate_agents_dir", lambda: tmp_path / "no-mates")
    monkeypatch.setattr(agent_catalog, "_REFRESH_GATE", agent_catalog._RefreshGate())

    def default_on_disk() -> str:
        return json.loads(config.read_text(encoding="utf-8"))["default_agent"]

    agent_catalog._refresh_for_catalog(owner=True)
    global_spec.unlink()
    _aged(agents)
    agent_catalog._refresh_for_catalog(owner=True)
    assert default_on_disk() == "pkg"
    hits = agent_catalog.catalog_scan_hit
    project_spec.unlink()
    agent_catalog._refresh_for_catalog(owner=True)
    assert agent_catalog.catalog_scan_hit == hits + 1
    assert default_on_disk() == "default"


@_REAL_REVISION_ONLY
def test_a_failed_spec_read_is_not_recorded(real_home, monkeypatch):
    # A read can fail for a moment with no change to the entry's metadata, so a
    # scan that hit one is not recorded and the next fetch reads it again.
    from kiro_crew import hooks

    for i in range(3):
        _write_spec(real_home.user_dir, f"agent-{i}")
    real_read = hooks.safe_read_file
    failures = {"agent-1.json": 1}

    def flaky_read(path, *args, **kwargs):
        name = Path(path).name
        if failures.get(name):
            failures[name] -= 1
            raise PermissionError("resolver busy")
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(hooks, "safe_read_file", flaky_read)
    for _ in range(3):
        agent_catalog._REFRESH_GATE.refresh()
    assert real_home.scans == [False, False]


@_REAL_REVISION_ONLY
def test_a_failed_listing_is_not_recorded(real_home, monkeypatch):
    # A listing that fails is not an empty directory: the scan keeps the old
    # snapshot, nothing is recorded, and the next fetch lists again.
    from kiro_crew.config import loader_snapshots

    for i in range(3):
        _write_spec(real_home.user_dir, f"agent-{i}")
    real_list = loader_snapshots.iter_agent_spec_files_strict
    failures = {"count": 1}

    def flaky_list(directory):
        if directory == real_home.user_dir and failures["count"]:
            failures["count"] -= 1
            raise PermissionError("listing refused")
        return real_list(directory)

    monkeypatch.setattr(loader_snapshots, "iter_agent_spec_files_strict", flaky_list)
    for _ in range(3):
        agent_catalog._REFRESH_GATE.refresh()
    assert real_home.scans == [False, False]


def test_a_failed_directory_probe_scans_instead_of_raising(gate, monkeypatch):
    # A crewmate folder whose parent denies search raises on stat. The stamp is
    # then unprovable, so each fetch scans as it would without the gate.
    real_stat = Path.stat

    def denied_stat(self, *args, **kwargs):
        if self == gate.mate_dir:
            raise PermissionError("search denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    assert [gate.gate.refresh() for _ in range(3)] == [True, True, True]
    assert gate.state.scans == 3


def test_skipped_stems_past_the_bound_are_not_kept(monkeypatch, tmp_path, caplog):
    # The skipped set is bounded like the other retained maps: past the bound it
    # is not kept at all, so nothing memoizes on a truncated set.
    from kiro_crew.config import loader

    agents = tmp_path / "agents"
    agents.mkdir()
    for i in range(4):
        (agents / f"broken-{i}.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: tmp_path / "no-mates")
    monkeypatch.setattr(loader, "_MATERIALIZED_SEEN_MAX", 3)
    with caplog.at_level("WARNING", logger="kiro_crew.config.loader_snapshots"):
        assert loader.refresh_materialized_agents() is True
    assert loader.materialized_skipped_stems() is None
    assert any("skipped spec stem" in r.getMessage() for r in caplog.records)
    monkeypatch.setattr(loader, "_MATERIALIZED_SEEN_MAX", 4)
    assert loader.refresh_materialized_agents() is True
    assert loader.materialized_skipped_stems() == frozenset(f"broken-{i}" for i in range(4))


@_REAL_REVISION_ONLY
def test_a_directory_back_in_its_recorded_state_rescans_another_snapshot(real_home):
    # Recorded over state A; another caller's refresh installs the snapshot of
    # state B; the directory returns to A. The stamp matches again, but the
    # installed snapshot is B's, so the next fetch rescans.
    from kiro_crew.config import loader

    _write_spec(real_home.user_dir, "base")
    agent_catalog._REFRESH_GATE.refresh()
    extra = _write_spec(real_home.user_dir, "extra")
    assert loader.refresh_materialized_agents() is True
    extra.unlink()
    agent_catalog._REFRESH_GATE.refresh()
    assert real_home.scans == [False, False]
    assert agent_catalog._REFRESH_GATE.refresh() is False
