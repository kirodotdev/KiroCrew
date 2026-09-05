"""Behavioural tests for the ``GET /api/skills`` single-flight join.

Covers:
- concurrent readers coalesce onto ONE assembly (the measured fix: 0% -> 87.5%
  redundant-scan elimination at 8-way)
- NOTHING is retained past the burst, so a later read always rescans and the
  base's recorded "no result cache" default still holds
- a different loader is a different catalog and never joins
- readers of different projects serialize on the one global assembly lock
- no module owes the catalog an invalidation: no invalidator exists to call

Every test stubs the two expensive collaborators (the edition capability manager
and ``collect_skills_blocking``) and counts calls, so "shared one assembly" is
asserted by the assembler NOT running rather than by a latency measurement.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

# Reused rather than re-implemented: these are the same scaffolding helpers the
# skills-browser suite builds its endpoint tests from. Imported instead of
# copied, and the test lives HERE instead of being appended there, because that
# module carries 155 lines of pre-existing black drift -- touching it would bury
# this fix under unrelated reformatting.
from test_skill_browser import _make_app, _write_skill  # type: ignore[import-not-found]

from kiro_crew.dashboard.handlers import prompts


@pytest.fixture(autouse=True)
def clean_protocol_state():
    """Each test starts and ends with no in-flight bookkeeping.

    This is module state, so a leaked handoff or waiter count from a neighbour
    could serve one test from another's rows -- which would make a broken join
    look correct.
    """
    prompts._catalog_handoff.clear()
    prompts._catalog_waiters.clear()
    prompts._catalog_assembly_locks.clear()
    yield
    prompts._catalog_handoff.clear()
    prompts._catalog_waiters.clear()
    prompts._catalog_assembly_locks.clear()


@pytest.fixture
def stub_assembly(monkeypatch):
    """Replace both expensive collaborators and count assembler invocations."""
    calls = {"n": 0}

    class _NoCapabilities:
        def available(self) -> bool:
            return False

    def _collect(skills, package_skills, project_dir):
        calls["n"] += 1
        # Echo BOTH inputs the catalog varies by, so a key that omits either
        # shows up as a wrong value rather than only as a wrong call count.
        return [
            {
                "key": f"skill-for-{project_dir}",
                "name": "s",
                "description": "",
                "loader": id(skills),
            }
        ]

    monkeypatch.setattr(prompts, "_capability_manager", lambda: _NoCapabilities())
    monkeypatch.setattr(prompts, "collect_skills_blocking", _collect)
    return calls


@pytest.fixture
def loader():
    """One SkillsLoader stand-in per test.

    Reused across calls WITHIN a test on purpose: a gateway has a single loader,
    so passing a fresh object per call would exercise the isolation gate instead
    of the behaviour each test names.
    """
    return object()


class TestSkillsCatalogSingleFlight:
    """Concurrent readers must share one assembly.

    This is the whole fix. Measured against a counting assembler, 8 simultaneous
    reads produced 8 assemblies -- a 0% elimination rate precisely under the
    contention the slow samples came from.
    """

    @pytest.mark.asyncio
    async def test_concurrent_misses_coalesce_into_one_assembly(self, stub_assembly, loader):
        results = await asyncio.gather(
            *[prompts._assemble_skills_catalog(loader, Path("/proj/a")) for _ in range(8)]
        )
        assert stub_assembly["n"] == 1, (
            "each concurrent reader assembled its own catalog; without coalescing the "
            "endpoint pays N scans exactly when it is contended"
        )
        assert all(r is results[0] for r in results), "joined readers got different objects"

    @pytest.mark.asyncio
    async def test_a_later_sequential_read_reassembles(self, stub_assembly, loader):
        """Nothing survives the burst, so the recorded no-result-cache default holds.

        This is the property that removes every invalidation obligation: a read
        that is not part of a concurrent burst scans current on-disk state, so an
        out-of-app edit can never be hidden and no mutator owes this code a call.
        """
        await prompts._assemble_skills_catalog(loader, Path("/proj/a"))
        await prompts._assemble_skills_catalog(loader, Path("/proj/a"))
        assert stub_assembly["n"] == 2, (
            "a second, non-concurrent read was served from retained rows -- the join "
            "is storing a result past its burst, which reintroduces staleness"
        )
        assert not prompts._catalog_handoff, "the handoff outlived its burst"
        assert not prompts._catalog_waiters, "waiter bookkeeping leaked"

    @pytest.mark.asyncio
    async def test_different_keys_assemble_in_parallel(self, monkeypatch, loader):
        """Two projects hold DIFFERENT assembly locks, so neither waits on the other.

        Coalescing is per key: it must not turn unrelated catalogs into a queue.
        Assembly is not reliably sub-second, so a shared lock would give a
        multi-project burst worse tail latency than the base's parallel scans. If a
        single global lock is ever reintroduced, this fails.
        """
        overlap = {"now": 0, "max": 0}

        async def _assemble(skills, project_dir):
            overlap["now"] += 1
            overlap["max"] = max(overlap["max"], overlap["now"])
            await asyncio.sleep(0)
            overlap["now"] -= 1
            return [{"name": str(project_dir)}]

        monkeypatch.setattr(prompts, "_assemble_skills_catalog_uncached", _assemble)
        await asyncio.gather(
            prompts._assemble_skills_catalog(loader, Path("/proj/a")),
            prompts._assemble_skills_catalog(loader, Path("/proj/b")),
        )
        assert overlap["max"] == 2, (
            "two different projects did not assemble concurrently, so they are "
            "serializing on a shared lock and one project's burst now delays another"
        )

    @pytest.mark.asyncio
    async def test_a_key_lock_does_not_outlive_its_burst(self, stub_assembly, loader):
        """The per-key lock registry is bounded by the waiter count, not unbounded.

        A lock dict keyed on (loader, project) would otherwise retain an entry per
        project ever browsed, holding the loader alive with it.
        """
        await prompts._assemble_skills_catalog(loader, Path("/proj/a"))
        assert prompts._catalog_assembly_locks == {}, (
            "a key's assembly lock survived the last reader leaving, so the registry "
            "grows once per project visited"
        )

    @pytest.mark.asyncio
    async def test_a_different_loader_does_not_join(self, stub_assembly):
        """Two readers holding DIFFERENT loaders never share one assembly.

        The loader is the key's first element, so a different loader is a different
        key and the two cannot collide on one in-flight entry. Were the loader left
        out of the key, these two would collide and the second would be handed rows
        assembled from a catalog it never asked for.
        """
        loader_a = object()
        loader_b = object()
        results = await asyncio.gather(
            prompts._assemble_skills_catalog(loader_a, Path("/proj/a")),
            prompts._assemble_skills_catalog(loader_b, Path("/proj/a")),
        )
        assert stub_assembly["n"] == 2, (
            "a reader with a different loader joined an assembly started under another "
            "loader, so it received rows for a catalog it never asked for"
        )
        assert results[0] is not results[1], "two distinct loaders were served the same object"


class TestCrossAgentIsolationEndToEnd:
    """Two agents in ONE process must each get their own listing, either order.

    This is the shape a coarsened cache key produces: one caller sees too few
    skills and another too many, decided by which asked first — an
    order-dependent pair that reads like flakiness. Driving both agents through
    the real endpoint in one process pins it directly, so a join that ever shared
    the FILTERED result fails whichever agent asked second.

    ``?agent=`` is applied downstream as a comprehension over the assembled rows,
    so it correctly does NOT belong in the key — this test is what keeps that true
    rather than merely currently-true.
    """

    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("KIROCREW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        # agent_skill_globs resolves its agents dir from a module constant
        # computed at import time off the REAL home, so $HOME alone would leave
        # it reading the operator's own ~/.kiro/agents and skipping the filter.
        monkeypatch.setattr(
            "kiro_crew.agent_discovery._KIRO_AGENTS_DIR", tmp_path / ".kiro" / "agents"
        )
        return tmp_path

    @staticmethod
    def _state() -> MagicMock:
        from kiro_crew.skills import SkillsLoader

        # A real loader, not a bare MagicMock: _get_skills treats any attribute
        # as "already built", and a mock would be serialized into the response.
        state = MagicMock(_slots={}, context_builder=None)
        state._standalone_skills = SkillsLoader(install_builtins=False)
        return state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("order", [("custom", "plain"), ("plain", "custom")])
    async def test_each_agent_gets_its_own_listing(self, home, order):
        _write_skill(home / ".kiro" / "skills", "alpha")
        _write_skill(home / ".kiro" / "skills", "beta")
        agents_dir = home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        # `custom` maps one skill explicitly -> filtered envelope, {alpha}.
        agents_dir.joinpath("custom.json").write_text(
            json.dumps({"name": "custom", "resources": ["skill://~/.kiro/skills/alpha/SKILL.md"]})
        )
        # `plain` maps none -> legacy bare array, the whole catalog.
        agents_dir.joinpath("plain.json").write_text(json.dumps({"name": "plain"}))

        expected = {"custom": ({"alpha"}, True), "plain": ({"alpha", "beta"}, False)}
        async with TestClient(TestServer(_make_app(self._state()))) as client:
            for position, agent in enumerate(order, start=1):
                resp = await client.get("/api/skills", params={"agent": agent})
                assert resp.status == 200
                payload = await resp.json()
                want_names, want_envelope = expected[agent]
                assert isinstance(
                    payload, dict if want_envelope else list
                ), f"{agent} got the wrong response shape when asked {position} of {len(order)}"
                rows = payload["skills"] if want_envelope else payload
                assert {
                    s["name"] for s in rows
                } == want_names, f"{agent} was served another agent's listing (order {order})"
