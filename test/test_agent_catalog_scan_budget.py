"""How many snapshot scans the agent catalog's fetches cost over a realistic agents home."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from kiro_crew import agent_discovery
from kiro_crew.agent_discovery_parts import dir_revision
from kiro_crew.config import loader
from kiro_crew.dashboard.handlers import agent_catalog

_BOUND = 30


def _write_specs(directory: Path, count: int, prefix: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    past = time.time() - 60
    for i in range(count):
        path = directory / f"{prefix}-{i}.json"
        path.write_text(json.dumps({"name": f"{prefix}-{i}", "description": "d"}), encoding="utf-8")
        os.utime(path, (past, past))


@pytest.fixture
def home(monkeypatch, tmp_path):
    kiro = tmp_path / "kiro"
    _write_specs(kiro / "agents", 150, "user")
    _write_specs(kiro / "mates" / "agents", 10, "mate")
    monkeypatch.setenv("KIRO_HOME", str(kiro))
    monkeypatch.setattr(agent_discovery, "AGENTS_DIR_MEMO_ENABLED", True)
    # A freshly written file's ctime cannot be set back, and more than 64 young
    # specs make the revision unprovable (the boot window), so the window is closed
    # here; every edit below changes a spec's size, which metadata alone pins.
    monkeypatch.setattr(dir_revision, "_AGENTS_DIR_RACY_WINDOW_NS", 0)
    monkeypatch.setattr(agent_catalog, "_REFRESH_GATE", agent_catalog._RefreshGate())
    scans: list[Path] = []
    real_scan = loader._scan_materialized_index

    def counted(agents_dir: Path):
        scans.append(agents_dir)
        return real_scan(agents_dir)

    monkeypatch.setattr(loader, "_scan_materialized_index", counted)
    return kiro, scans


def _fetch() -> None:
    agent_catalog._refresh_for_catalog(owner=True)


def _refreshes(scans: list[Path]) -> int:
    # One refresh scans the user and the crewmate directory.
    assert len(scans) % 2 == 0
    return len(scans) // 2


def test_agent_catalog_scan_budget(home):
    kiro, scans = home
    for _ in range(20):
        _fetch()
    threads = [threading.Thread(target=_fetch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_BOUND)
    assert not any(t.is_alive() for t in threads)
    assert _refreshes(scans) <= 2
    before = _refreshes(scans)
    edited = kiro / "agents" / "user-3.json"
    edited.write_text(json.dumps({"name": "user-3", "description": "edited"}), encoding="utf-8")
    _fetch()
    _fetch()
    assert _refreshes(scans) == before + 1
    loader.publish_mate_agents(["x"])
    _fetch()
    _fetch()
    assert _refreshes(scans) == before + 2
