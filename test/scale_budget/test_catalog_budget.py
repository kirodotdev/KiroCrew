"""Agent catalog budgets (F4): directory scans per ``GET /api/agents/catalog``.

The picker fetches the catalog several times per tab on every page load. Each
fetch runs ``refresh_materialized_agents``, which parses every spec in the user
and crewmate agents directories through ``_scan_materialized_index``. These
tests drive the real handler over 150 user specs and 10 crewmate specs and count
the scans of the user directory, one per refresh.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import types
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from .conftest import harness_fail

pytestmark = pytest.mark.scale_budget

USER_SPECS = 150
MATE_SPECS = 10


def _write_spec(directory: Path, name: str, *, body: str | None = None, age: float = 3600) -> Path:
    """One spec with its mtime stamped *age* seconds in the past."""
    path = directory / f"{name}.json"
    path.write_text(body if body is not None else json.dumps({"name": name}), encoding="utf-8")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from kiro_crew.agent_discovery_parts import dir_revision, spec_reads
    from kiro_crew.config import loader_snapshots, paths
    from kiro_crew.dashboard.handlers import agent_catalog

    user_dir = tmp_path / "kiro" / "agents"
    mate_dir = tmp_path / "kiro" / "mates" / "agents"
    user_dir.mkdir(parents=True)
    mate_dir.mkdir(parents=True)
    for i in range(USER_SPECS):
        _write_spec(user_dir, f"spec-{i}")
    for i in range(MATE_SPECS):
        _write_spec(mate_dir, f"mate-{i}")
    monkeypatch.setattr(paths, "_agents_dir_override", lambda: user_dir)
    monkeypatch.setattr(paths, "_mate_agents_dir_override", lambda: mate_dir)
    # ``list_agents`` (the template rows) reads ``<kiro home>/agents`` unless this
    # is set; without it the response lists none of the seeded specs.
    monkeypatch.setattr(spec_reads, "_KIRO_AGENTS_DIR", user_dir)
    monkeypatch.setattr(dir_revision, "AGENTS_DIR_MEMO_ENABLED", True)
    # ``os.utime`` cannot move ctime, and the revision digests any entry whose
    # ctime is inside its racy window. Reading the clock five windows ahead makes
    # every seeded spec old, so the counts below measure the steady path.
    ahead = 5 * int(getattr(dir_revision, "_AGENTS_DIR_RACY_WINDOW_NS", 2_000_000_000))
    monkeypatch.setattr(
        dir_revision, "time", types.SimpleNamespace(time_ns=lambda: time.time_ns() + ahead)
    )
    monkeypatch.setattr(agent_catalog, "is_owner_dashboard_request", lambda request: True)
    gate = getattr(agent_catalog, "_RefreshGate", None)
    if gate is not None:
        monkeypatch.setattr(agent_catalog, "_REFRESH_GATE", gate())

    scans: list[Path] = []
    real_scan = loader_snapshots._scan_materialized_index

    def counted(agents_dir: Path):
        scans.append(Path(agents_dir))
        return real_scan(agents_dir)

    monkeypatch.setattr(loader_snapshots, "_scan_materialized_index", counted)

    app = web.Application()
    app["state"] = types.SimpleNamespace(_slots={})

    last: dict[str, object] = {}

    async def fetch(n: int = 1, *, concurrent: bool = False) -> None:
        def one():
            return agent_catalog.api_agent_catalog(
                make_mocked_request("GET", "/api/agents/catalog", app=app)
            )

        if concurrent:
            responses = await asyncio.gather(*(one() for _ in range(n)))
        else:
            responses = [await one() for _ in range(n)]
        if not all(r.status == 200 for r in responses):
            harness_fail(f"catalog fetches answered {[r.status for r in responses]}")
        last["body"] = json.loads(responses[-1].body)

    return types.SimpleNamespace(
        fetch=lambda n=1, concurrent=False: asyncio.run(fetch(n, concurrent=concurrent)),
        refreshes=lambda: sum(1 for d in scans if d == user_dir),
        scans=scans,
        user_dir=user_dir,
        last=last,
    )


def test_each_fetch_scans_each_directory_at_most_once(catalog) -> None:
    """Ratchet on main: one owner fetch reads each agents directory at most once."""
    catalog.fetch(3)
    if catalog.refreshes() == 0:
        harness_fail("3 catalog fetches scanned the seeded user directory 0 times")
    assert (
        len(catalog.scans) <= 3 * 2
    ), f"F4: 3 catalog fetches ran {len(catalog.scans)} directory scans (budget 6)"
    names = {a.get("name") for a in catalog.last["body"].get("agents", [])}
    missing = {f"spec-{i}" for i in range(USER_SPECS)} - names
    assert not missing, f"F4: the catalog left out {len(missing)} seeded specs"


def test_memo_off_scans_every_fetch(catalog, monkeypatch) -> None:
    """With the directory memo off, the Windows default, three fetches run three refreshes.

    ``agents_dir_revision`` answers ``None`` with the memo off, and a catalog
    gate keyed on that revision must then refresh on every fetch instead of
    skipping one. Today's catalog refreshes on every fetch whatever the flag,
    so this holds already; it fails once a gate starts skipping on ``None``.
    The test turns the memo off itself, so it runs on every platform.
    """
    from kiro_crew.agent_discovery_parts import dir_revision

    monkeypatch.setattr(dir_revision, "AGENTS_DIR_MEMO_ENABLED", False)
    catalog.fetch(3)
    assert catalog.refreshes() == 3, f"F4: memo off, 3 fetches ran {catalog.refreshes()} refreshes"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F4: main rescans every agents directory on every catalog fetch",
)
def test_unchanged_catalog_scans_at_most_twice(catalog) -> None:
    """20 sequential plus 8 concurrent owner fetches over unchanged specs: at most 2 refreshes."""
    catalog.fetch(20)
    catalog.fetch(8, concurrent=True)
    assert catalog.refreshes() <= 2, (
        f"F4: 28 fetches over {USER_SPECS} unchanged specs ran {catalog.refreshes()} "
        "refreshes (budget 2)"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F4: main rescans every agents directory on every catalog fetch",
)
def test_one_spec_edit_adds_exactly_one_scan(catalog) -> None:
    """After the catalog settles, rewriting one spec costs exactly one more refresh."""
    catalog.fetch(5)
    settled = catalog.refreshes()
    _write_spec(catalog.user_dir, "spec-7", body=json.dumps({"name": "spec-7", "x": 1}), age=1800)
    catalog.fetch(5)
    extra = catalog.refreshes() - settled
    assert extra == 1, f"F4: one spec edit cost {extra} refreshes over 5 fetches (budget 1)"


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="scale-F4: item 2.24 per-entry validity (may move to item 2.26 PR B)",
)
def test_one_broken_spec_keeps_the_gate(catalog) -> None:
    """A single ``{not json`` spec among 150 still lets 4 fetches run 1 refresh."""
    _write_spec(catalog.user_dir, "broken", body="{not json")
    catalog.fetch(4)
    assert (
        catalog.refreshes() == 1
    ), f"F4: with one broken spec, 4 fetches ran {catalog.refreshes()} refreshes (budget 1)"
