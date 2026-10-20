"""Shared fixtures and helpers for the native skill projection suites.

``test_native_skill_projection.py`` and its ``test_native_skill_projection_<topic>.py``
siblings use these by name: the two autouse pins (a prune on every spawn, no eager prune
when a projection ends), the ``native_tree`` and ``cyclic_gc_quiesced`` fixtures, the
frozen prune clock, and the alias, sidecar and lease helpers.
"""

from __future__ import annotations

import gc
import json
import os
import time
from types import SimpleNamespace

import pytest

from kiro_crew.acp import skill_projection as projection


def _hold_the_prune_clock(monkeypatch):
    """Freeze ``monotonic`` for a test that asserts how many aliases one prune
    walk RECLAIMS rather than how the time budget cuts it short.

    Those counts only hold if the walk finishes. The budget is real wall-clock
    time, so on a slow CI runner it ends a nine-candidate walk one reclaim early
    and the count assertion reads that as a wrong cap. A clock that never
    advances leaves the budget itself untouched and simply never spends it;
    the budget's own semantics are pinned by :func:`_prune_walk`, which drives
    this same clock one tick per candidate, and by the zero-budget tests.

    Freeze ONLY the projection module's own ``monotonic`` reads: rebinding
    ``projection.time`` to a delegating proxy overrides ``monotonic`` while every
    other name (``sleep``, ``time``, ...) falls through to the real module. The
    shared stdlib ``time.monotonic`` is left untouched, so unrelated readers --
    asyncio's ``loop.time()`` among them -- keep the real clock.
    """

    class _FrozenMonotonicTime:
        monotonic = staticmethod(lambda: 0.0)

        def __getattr__(self, name):
            return getattr(time, name)

    monkeypatch.setattr(projection, "time", _FrozenMonotonicTime())


def _alias_is_live(directory, alias):
    """Test-only single-alias liveness over the one product lease scan.

    The product ``_alias_has_external_lease`` wrapper was removed -- it had zero
    product callers, so retaining it as a "for direct callers" convenience was
    misleading. The prune consults ``_scan_projection_leases`` once and tests
    membership in its returned ``live_aliases`` set; these tests do the same via
    this helper so they exercise the real scan (including its crash-residue
    reclamation and uncertainty accounting) rather than a shim that outlived its
    only users. Uncertainty is live, matching the fail-closed prune contract.
    """
    live_aliases, uncertain = projection._scan_projection_leases(directory)
    if uncertain:
        return True
    return alias in live_aliases


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the native skill projection suites' autouse prune pin; none of them calls monkeypatch.undo()
def _prune_on_every_spawn(monkeypatch):
    """These tests prepare views back to back and assert each one's prune; the
    burst throttle is pinned by its own test."""
    monkeypatch.setattr(projection, "_PER_SPAWN_PRUNE_MIN_INTERVAL_SECS", 0.0)
    monkeypatch.setattr(projection, "_LAST_PER_SPAWN_PRUNE", {})


@pytest.fixture(autouse=True)
def _no_eager_prune(_floor_monkeypatch):
    """Keep a dropped projection's views on disk until the NEXT prune walks.

    A projection that ends now prunes this home's unleased views at once
    (``_end_projection``). Most tests here build a backlog by dropping
    projections and then assert what one per-spawn prune, one boot drain or one
    census does with it, so the eager prune would empty the directory under
    them. It is switched off by default and pinned by its own tests, which put
    the real one back (:data:`_REAL_EAGER_PRUNE`).
    """
    _floor_monkeypatch.setattr(
        projection, "_eager_prune", lambda directory, crew_home_id, *, force=False: None
    )


@pytest.fixture
def cyclic_gc_quiesced():
    """Keep a cyclic-GC pass (and any finalizer it runs) out of a deep JSON parse.

    A recursion-limit fixture drives the parser to the bottom of the stack; a
    collection firing there could run an inherited finalizer with no stack
    left. Drain first, disable for the parse, then restore and drain again.
    """
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    crew_home = tmp_path / "crew"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: crew_home, raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    # The projection reads the settings files the control-plane mount reads;
    # point the global one into the temp tree so no test reads this host's own.
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    return home, agents, project


def _alias_file(agents, prepared, name="custom"):
    return agents / f"{prepared.agent(name)}.json"


def _metadata_file(agents, prepared, name="custom"):
    return projection._projection_metadata_dir() / f"{prepared.agent(name)}.json"


def _legacy_alias(agents, digest="0" * 24, name=None, resources=None, age_secs=3600.0, owned=True):
    """Write an alias the way a pre-lifecycle build did: no record of any kind.

    Backdated by default. A freshly written one is indistinguishable from a
    pre-lease publisher's in-flight spawn, which the reclaim deliberately spares.
    """
    stem = f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{digest}"
    view = {"name": name if name is not None else stem, "resources": resources or ["file://R.md"]}
    if owned:
        # One positive mark the projection itself writes; shape alone must not
        # authorize an unlink.
        view["mcpServers"] = {"kirocrew-core": {"command": "test-core", "args": []}}
    path = agents / f"{stem}.json"
    path.write_text(json.dumps(view), encoding="utf-8")
    if age_secs:
        old = time.time() - age_secs
        os.utime(path, (old, old))
    return path


def _crew_home_id():
    """The owner id the spawn path passes, for a test that drives the prune itself."""
    return projection.data_home().absolute().as_posix()
