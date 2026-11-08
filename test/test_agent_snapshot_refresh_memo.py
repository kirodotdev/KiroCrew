"""The agent snapshot refresh skips its scan while nothing it reads has moved.

``refresh_materialized_agents`` keeps one memo for every caller: the pinned
``agents_dir_revision`` of both agents directories and the publish count a scan
read, with the ticket of the snapshot it installed. These tests run the real
refresh over tmp directories and count the scans.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent_discovery
from kiro_crew.agent_discovery_parts import dir_revision, spec_reads
from kiro_crew.config import loader, loader_snapshots
from kiro_crew.dashboard.handlers import agent_catalog
from kiro_crew.testing.wait import default_timeout

_REAL_REVISION_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="agents_dir_revision pins every entry on its bytes on Windows; "
    "test_windows_scans_every_refresh covers that platform's default",
)


def _write_spec(directory: Path, name: str, description: str = "d") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"name": name, "description": description}), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _memo_on(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """The revision the memo rests on is off on Windows by default; on here.

    Its young-entry cutoff is pinned to the fixture's start: a spec written here
    is young on every refresh, so it is pinned on its bytes, and a slow worker
    crossing the 2 s window between two refreshes cannot move the key.
    """
    _floor_monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", True)
    frozen = time.time_ns()
    _floor_monkeypatch.setattr(dir_revision, "time", SimpleNamespace(time_ns=lambda: frozen))


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Both agents directories under tmp_path, each scan of either counted.

    The crewmate folder is not created: it does not exist until the first
    crewmate file is written, and its revision is then that of an empty one.
    """
    user_dir = tmp_path / "kiro" / "agents"
    mate_dir = tmp_path / "kiro" / "mates" / "agents"
    _write_spec(user_dir, "reviewer")
    monkeypatch.setattr(loader, "kiro_agents_dir", lambda: user_dir)
    monkeypatch.setattr(loader, "mate_agents_dir", lambda: mate_dir)
    scans: list[Path] = []
    real_scan = loader_snapshots._scan_materialized_index

    def counted(agents_dir: Path):
        scans.append(Path(agents_dir))
        return real_scan(agents_dir)

    monkeypatch.setattr(loader, "_scan_materialized_index", counted)
    return SimpleNamespace(
        user_dir=user_dir,
        mate_dir=mate_dir,
        scans=scans,
        refreshes=lambda: sum(1 for d in scans if d == user_dir),
    )


@_REAL_REVISION_ONLY
def test_repeated_refreshes_with_unchanged_inputs_scan_once(home):
    hits = loader_snapshots._MATERIALIZED_SCAN_COUNTS["hits"]
    assert [loader.refresh_materialized_agents() for _ in range(3)] == [True, True, True]
    assert home.refreshes() == 1
    assert loader_snapshots._MATERIALIZED_SCAN_COUNTS["hits"] == hits + 2
    assert "reviewer" in loader_snapshots._MATERIALIZED_AGENTS


@_REAL_REVISION_ONLY
@pytest.mark.parametrize("change", ["edit", "add", "remove", "mate-add", "publish", "generation"])
def test_any_input_change_rescans_on_the_next_refresh(home, change):
    loader.refresh_materialized_agents()
    if change == "edit":
        _write_spec(home.user_dir, "reviewer", description="edited")
    elif change == "add":
        _write_spec(home.user_dir, "planner")
    elif change == "remove":
        (home.user_dir / "reviewer.json").unlink()
    elif change == "mate-add":
        _write_spec(home.mate_dir, "mate-1")
    elif change == "publish":
        loader.publish_materialized_agents(["published"])
    else:
        # An in-process spec writer's signal, which moves every revision.
        agent_discovery.clear_list_agents_cache()
    loader.refresh_materialized_agents()
    loader.refresh_materialized_agents()
    assert home.refreshes() == 2


@_REAL_REVISION_ONLY
def test_a_missing_directory_has_the_revision_of_an_empty_one(home, tmp_path):
    absent = tmp_path / "absent"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert agent_discovery.agents_dir_revision(absent) == agent_discovery.agents_dir_revision(empty)
    assert agent_discovery.agents_dir_revision(absent) is not None
    assert not home.mate_dir.exists()
    loader.refresh_materialized_agents()
    loader.refresh_materialized_agents()
    assert home.refreshes() == 1


@_REAL_REVISION_ONLY
def test_a_raising_scan_is_not_memoized(home, monkeypatch):
    counted = loader_snapshots._scan_materialized_index
    failures = {"left": 1}

    def failing_once(agents_dir: Path):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("scan failed")
        return counted(agents_dir)

    monkeypatch.setattr(loader, "_scan_materialized_index", failing_once)
    assert loader.refresh_materialized_agents() is False
    assert loader.refresh_materialized_agents() is True
    assert loader.refresh_materialized_agents() is True
    assert home.refreshes() == 1


@_REAL_REVISION_ONLY
def test_a_failed_spec_read_is_not_memoized(home, monkeypatch):
    # A read can fail for a moment with no change to the entry's metadata, so a
    # scan that hit one is not memoized and the next refresh reads it again.
    from kiro_crew import hooks

    for i in range(3):
        _write_spec(home.user_dir, f"agent-{i}")
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
        loader.refresh_materialized_agents()
    assert home.refreshes() == 2
    assert loader.materialized_snapshot_complete() is True


@_REAL_REVISION_ONLY
def test_a_failed_listing_is_not_memoized(home, monkeypatch):
    # A listing that fails is not an empty directory: the scan keeps the old
    # snapshot, nothing is memoized, and the next refresh lists again.
    real_list = loader_snapshots.iter_agent_spec_files_strict
    failures = {"count": 1}

    def flaky_list(directory):
        if directory == home.user_dir and failures["count"]:
            failures["count"] -= 1
            raise PermissionError("listing refused")
        return real_list(directory)

    monkeypatch.setattr(loader_snapshots, "iter_agent_spec_files_strict", flaky_list)
    for _ in range(3):
        loader.refresh_materialized_agents()
    assert home.refreshes() == 2


@_REAL_REVISION_ONLY
@pytest.mark.parametrize("where", ["user", "mate"])
def test_an_unparseable_spec_is_memoized_under_its_own_bytes(home, where):
    # A spec that does not parse fails the same way on the same bytes, so one
    # broken file among real ones does not turn the memo off, in either folder.
    for i in range(20):
        _write_spec(home.user_dir, f"agent-{i}")
    directory = home.user_dir if where == "user" else home.mate_dir
    directory.mkdir(parents=True, exist_ok=True)
    broken = directory / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    for _ in range(4):
        loader.refresh_materialized_agents()
    assert home.refreshes() == 1
    broken.write_text(json.dumps({"name": "fixed"}), encoding="utf-8")
    loader.refresh_materialized_agents()
    assert home.refreshes() == 2
    assert "fixed" in loader_snapshots._MATERIALIZED_AGENTS


@_REAL_REVISION_ONLY
@pytest.mark.parametrize("during", ["write", "publish"])
def test_an_input_that_moves_during_the_scan_is_not_memoized(home, monkeypatch, during):
    counted = loader_snapshots._scan_materialized_index
    moved = {"done": False}

    def moving_scan(agents_dir: Path):
        if not moved["done"] and Path(agents_dir) == home.user_dir:
            moved["done"] = True
            if during == "write":
                _write_spec(home.user_dir, "late")
            else:
                loader.publish_materialized_agents(["late"])
        return counted(agents_dir)

    monkeypatch.setattr(loader, "_scan_materialized_index", moving_scan)
    loader.refresh_materialized_agents()
    loader.refresh_materialized_agents()
    loader.refresh_materialized_agents()
    assert home.refreshes() == 2
    # The second scan is the authoritative view: a written spec stays, a name
    # published with no file behind it is dropped.
    assert ("late" in loader_snapshots._MATERIALIZED_AGENTS) is (during == "write")


@pytest.mark.parametrize("probe", ["unprovable", "raising"])
def test_an_unprovable_revision_always_scans(home, monkeypatch, probe):
    def revision(_directory):
        if probe == "raising":
            raise RuntimeError("probe failed")
        return None

    monkeypatch.setattr(agent_discovery, "agents_dir_revision", revision)
    for _ in range(3):
        assert loader.refresh_materialized_agents() is True
    assert home.refreshes() == 3


@_REAL_REVISION_ONLY
def test_a_declined_memo_logs_once_per_key(home, monkeypatch, caplog):
    from kiro_crew import hooks

    def refused(path, *args, **kwargs):
        raise PermissionError("resolver busy")

    monkeypatch.setattr(hooks, "safe_read_file", refused)
    with caplog.at_level(logging.INFO, logger=loader_snapshots.logger.name):
        for _ in range(3):
            loader.refresh_materialized_agents()
    assert home.refreshes() == 3
    declined = [r for r in caplog.records if "not memoized" in r.getMessage()]
    assert len(declined) == 1


@_REAL_REVISION_ONLY
def test_concurrent_refreshes_scan_once(home):
    # The memo is checked and recorded under the flight lock, so whichever
    # thread scans first, every other one then finds the memo.
    errors: list[BaseException] = []
    start = threading.Barrier(8, timeout=default_timeout())

    def refresh() -> None:
        try:
            start.wait()
            loader.refresh_materialized_agents()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=refresh) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(default_timeout())
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert home.refreshes() == 1


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows default leaves the memo off")
def test_windows_scans_every_refresh(home, monkeypatch):
    monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", not spec_reads._WINDOWS)
    for _ in range(3):
        loader.refresh_materialized_agents()
    assert home.refreshes() == 3


@pytest.fixture
def heals(monkeypatch):
    counted: list[None] = []

    def count_heal() -> bool:
        counted.append(None)
        return False

    monkeypatch.setattr(loader, "reset_dangling_default_agent", count_heal)
    monkeypatch.setattr(agent_catalog, "reset_dangling_default_agent", count_heal)
    return counted


@_REAL_REVISION_ONLY
def test_the_heal_runs_on_a_reuse_as_on_a_scan(home, heals):
    for _ in range(3):
        loader.refresh_materialized_agents(heal_default=True)
    assert (home.refreshes(), len(heals)) == (1, 3)


@_REAL_REVISION_ONLY
def test_the_heal_waits_for_a_complete_snapshot(home, heals, monkeypatch):
    from kiro_crew import hooks

    monkeypatch.setattr(hooks, "safe_read_file", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    loader.refresh_materialized_agents(heal_default=True)
    assert loader.materialized_snapshot_complete() is False
    assert heals == []


@_REAL_REVISION_ONLY
def test_catalog_fetches_share_the_memo_and_only_the_owner_heals(home, heals):
    for owner in (True, True, False):
        agent_catalog._refresh_for_catalog(owner=owner)
    assert (home.refreshes(), len(heals)) == (1, 2)


def _aged(directory: Path) -> None:
    """Every spec in *directory* older than the revision's young-entry window."""
    past = time.time() - 60
    for path in directory.glob("*.json"):
        os.utime(path, (past, past))


@_REAL_REVISION_ONLY
def test_owner_fetch_heals_after_the_workspace_spec_is_deleted(monkeypatch, tmp_path):
    # ``pkg`` runs ``reviewer`` in workspace ``proj``. The global spec is removed
    # while the checkout still declares it, so the heal declines. Then the
    # checkout's spec goes too: no input the memo covers moved, the refresh
    # reuses the snapshot, and the owner's next fetch still resets the default.
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

    def default_on_disk() -> str:
        return json.loads(config.read_text(encoding="utf-8"))["default_agent"]

    agent_catalog._refresh_for_catalog(owner=True)
    global_spec.unlink()
    _aged(agents)
    agent_catalog._refresh_for_catalog(owner=True)
    assert default_on_disk() == "pkg"
    hits = loader_snapshots._MATERIALIZED_SCAN_COUNTS["hits"]
    project_spec.unlink()
    agent_catalog._refresh_for_catalog(owner=True)
    assert loader_snapshots._MATERIALIZED_SCAN_COUNTS["hits"] == hits + 1
    assert default_on_disk() == "default"
