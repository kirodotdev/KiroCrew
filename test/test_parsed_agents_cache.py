"""Tests for the parsed agent-specs snapshot cache in ``agent_discovery``.

Covers:
- warm calls reuse the snapshot (no per-spec re-read) while the agents dir is
  unchanged
- the stat-only directory signature invalidates on add, remove, in-place edit
  (mtime bump), and rename
- ``clear_list_agents_cache`` drops the parsed-specs snapshot too — the one
  invalidation point the agent write paths already call
- ``agent_skill_globs`` keeps its contract on the cached path: name match,
  stem match, no-mapping agents, unknown agents, empty agent
- the dashboard's ``_load_parsed_agents`` adapter reads through the same
  snapshot (no second parse)
- the returned list is a fresh copy, so caller mutation cannot corrupt the
  cached snapshot
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import kiro_crew.agent_discovery as agent_discovery
from kiro_crew.agent_discovery import (
    agent_skill_globs,
    clear_list_agents_cache,
    parsed_agent_specs,
)


@pytest.fixture(autouse=True)
def clean_cache():
    """Module-level cache state must never leak between tests."""
    clear_list_agents_cache()
    yield
    clear_list_agents_cache()


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so ``kiro_agents_dir()`` resolves to a sandbox."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _agents_dir(fake_home: Path) -> Path:
    d = fake_home / ".kiro" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_agent(agents_dir: Path, stem: str, data: dict) -> Path:
    p = agents_dir / f"{stem}.json"
    p.write_text(json.dumps(data))
    return p


def _bump_mtime(path: Path) -> None:
    """Advance the file's mtime past the cached signature's recorded mtime.

    An in-place rewrite can land within the same ``st_mtime_ns`` tick on
    coarse-timestamp filesystems, which is exactly the staleness the signature
    accepts — the explicit bump makes the invalidation deterministic to test.
    """
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


class _ReadCounter:
    """Count pass-throughs to the real hardened reader."""

    def __init__(self, monkeypatch):
        self.n = 0
        real = agent_discovery._read_agent_spec

        def counting(path, **kwargs):
            self.n += 1
            return real(path, **kwargs)

        monkeypatch.setattr(agent_discovery, "_read_agent_spec", counting)


def _names(rows) -> list[str]:
    return [data.get("name") or path.stem for data, path in rows]


class TestParsedSpecsCache:
    def test_warm_call_reads_nothing(self, fake_home, monkeypatch):
        agents_dir = _agents_dir(fake_home)
        _write_agent(agents_dir, "a", {"name": "alpha", "resources": []})
        _write_agent(agents_dir, "b", {"name": "beta", "resources": []})

        counter = _ReadCounter(monkeypatch)
        first = parsed_agent_specs(agents_dir, operation="test", source="unknown")
        assert counter.n == 2
        second = parsed_agent_specs(agents_dir, operation="test", source="unknown")
        assert counter.n == 2  # warm: signature check only, no re-read
        assert first == second

    def test_add_invalidates(self, fake_home):
        agents_dir = _agents_dir(fake_home)
        _write_agent(agents_dir, "a", {"name": "alpha", "resources": []})
        assert _names(parsed_agent_specs(agents_dir, operation="test", source="unknown")) == [
            "alpha"
        ]

        _write_agent(agents_dir, "b", {"name": "beta", "resources": []})
        assert sorted(
            _names(parsed_agent_specs(agents_dir, operation="test", source="unknown"))
        ) == ["alpha", "beta"]

    def test_remove_invalidates(self, fake_home):
        agents_dir = _agents_dir(fake_home)
        a = _write_agent(agents_dir, "a", {"name": "alpha", "resources": []})
        _write_agent(agents_dir, "b", {"name": "beta", "resources": []})
        assert len(parsed_agent_specs(agents_dir, operation="test", source="unknown")) == 2

        a.unlink()
        assert _names(parsed_agent_specs(agents_dir, operation="test", source="unknown")) == [
            "beta"
        ]

    def test_edit_invalidates(self, fake_home):
        agents_dir = _agents_dir(fake_home)
        a = _write_agent(agents_dir, "a", {"name": "alpha", "resources": []})
        assert _names(parsed_agent_specs(agents_dir, operation="test", source="unknown")) == [
            "alpha"
        ]

        _write_agent(agents_dir, "a", {"name": "renamed", "resources": []})
        _bump_mtime(a)
        assert _names(parsed_agent_specs(agents_dir, operation="test", source="unknown")) == [
            "renamed"
        ]

    def test_rename_invalidates(self, fake_home):
        """A rename changes neither the file count nor any file's mtime, so a
        count+newest-mtime signature would miss it — the stem-derived name and
        relative-glob anchoring would stay stale forever. The filename-aware
        signature catches it."""
        agents_dir = _agents_dir(fake_home)
        a = _write_agent(agents_dir, "old-stem", {"resources": []})  # name falls back to stem
        assert _names(parsed_agent_specs(agents_dir, operation="test", source="unknown")) == [
            "old-stem"
        ]

        a.rename(agents_dir / "new-stem.json")
        assert _names(parsed_agent_specs(agents_dir, operation="test", source="unknown")) == [
            "new-stem"
        ]

    def test_uppercase_suffix_is_in_the_signature(self, fake_home):
        """A case-insensitive filesystem serves ``Foo.JSON`` to the
        ``glob("*.json")`` scans, so the signature must count it too — a
        case-sensitive suffix would omit it and its direct on-disk edits
        would never invalidate the snapshot."""
        agents_dir = _agents_dir(fake_home)
        upper = agents_dir / "Upper.JSON"
        upper.write_text(json.dumps({"name": "upper", "resources": []}))
        sig_with = agent_discovery._dir_signature(agents_dir)
        assert any(name == "Upper.JSON" for name, _m in sig_with)
        upper.unlink()
        assert agent_discovery._dir_signature(agents_dir) != sig_with

    def test_clear_list_agents_cache_drops_snapshot(self, fake_home, monkeypatch):
        """The agent write paths call ``clear_list_agents_cache()`` after a
        mutation precisely to cover a write landing inside one mtime tick;
        the parsed-specs snapshot must be dropped by the same call."""
        agents_dir = _agents_dir(fake_home)
        _write_agent(agents_dir, "a", {"name": "alpha", "resources": []})
        counter = _ReadCounter(monkeypatch)
        parsed_agent_specs(agents_dir, operation="test", source="unknown")
        assert counter.n == 1
        clear_list_agents_cache()
        parsed_agent_specs(agents_dir, operation="test", source="unknown")
        assert counter.n == 2

    def test_clear_during_parse_is_not_lost(self, fake_home, monkeypatch):
        """A ``clear_list_agents_cache()`` landing while a parse is in flight
        must invalidate that parse's snapshot: the scan may predate the write
        the clear announced, and inside one mtime tick the signature cannot
        tell. The generation check discards the stale store, so the next call
        re-reads."""
        agents_dir = _agents_dir(fake_home)
        _write_agent(agents_dir, "a", {"name": "alpha", "resources": []})

        real = agent_discovery._read_agent_spec
        calls = {"n": 0}

        def clearing_read(path, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                clear_list_agents_cache()  # a write path fires mid-parse
            return real(path, **kwargs)

        monkeypatch.setattr(agent_discovery, "_read_agent_spec", clearing_read)
        parsed_agent_specs(
            agents_dir, operation="test", source="unknown"
        )  # parse raced by the clear: not stored
        assert calls["n"] == 1
        parsed_agent_specs(
            agents_dir, operation="test", source="unknown"
        )  # must re-read, not serve the stale rows
        assert calls["n"] == 2

    def test_returned_list_is_a_copy(self, fake_home):
        agents_dir = _agents_dir(fake_home)
        _write_agent(agents_dir, "a", {"name": "alpha", "resources": []})
        first = parsed_agent_specs(agents_dir, operation="test", source="unknown")
        first.clear()  # caller mutation of the LIST must not corrupt the cache
        assert _names(parsed_agent_specs(agents_dir, operation="test", source="unknown")) == [
            "alpha"
        ]

    def test_missing_agents_dir_is_empty(self, fake_home):
        assert (
            parsed_agent_specs(fake_home / ".kiro" / "agents", operation="test", source="unknown")
            == []
        )


class TestAgentSkillGlobsCachedPath:
    """``agent_skill_globs`` keeps its contract while reading the snapshot."""

    def _seed(self, fake_home: Path) -> Path:
        agents_dir = _agents_dir(fake_home)
        _write_agent(
            agents_dir,
            "default",
            {"name": "default", "resources": ["skill://~/.kiro/skills/*/SKILL.md"]},
        )
        # Spec name differs from the file stem: both must resolve.
        _write_agent(
            agents_dir,
            "stem-only",
            {"name": "fancy-name", "resources": ["skill://~/.kiro/skills/x/SKILL.md"]},
        )
        _write_agent(agents_dir, "no-mapping", {"name": "no-mapping", "resources": []})
        return agents_dir

    def test_name_match_resolves(self, fake_home):
        agents_dir = self._seed(fake_home)
        globs = agent_skill_globs("default", agents_dir)
        assert globs and all("SKILL.md" in g for g in globs)

    def test_spec_name_and_stem_both_resolve(self, fake_home):
        agents_dir = self._seed(fake_home)
        assert agent_skill_globs("fancy-name", agents_dir) == agent_skill_globs(
            "stem-only", agents_dir
        )
        assert agent_skill_globs("fancy-name", agents_dir)

    def test_no_mapping_and_unknown_are_empty(self, fake_home):
        agents_dir = self._seed(fake_home)
        assert agent_skill_globs("no-mapping", agents_dir) == []
        assert agent_skill_globs("ghost", agents_dir) == []
        assert agent_skill_globs("", agents_dir) == []

    def test_warm_lookup_reads_nothing(self, fake_home, monkeypatch):
        agents_dir = self._seed(fake_home)
        counter = _ReadCounter(monkeypatch)
        first = agent_skill_globs("default", agents_dir)
        assert counter.n == 3
        assert first  # the wildcard mapping resolved to at least one glob
        assert agent_skill_globs("default", agents_dir) == first
        assert counter.n == 3  # snapshot reused: no per-spec re-read


class TestDashboardAdapterSharesSnapshot:
    def test_load_parsed_agents_reads_through_the_snapshot(self, fake_home, monkeypatch):
        import kiro_crew.dashboard.handlers._shared as shared

        agents_dir = _agents_dir(fake_home)
        _write_agent(agents_dir, "a", {"name": "alpha", "resources": []})
        # Pin the adapter's directory resolution to the sandbox: the snapshot
        # is keyed by directory path, so both readers must name the same dir.
        monkeypatch.setattr(shared, "kiro_agents_dir", lambda: agents_dir)

        counter = _ReadCounter(monkeypatch)
        assert agent_skill_globs("alpha", agents_dir) == []  # warms the snapshot (1 read)
        assert counter.n == 1
        rows = shared._load_parsed_agents()
        assert [n for n, _d, _p in rows] == ["alpha"]
        assert counter.n == 1  # adapter reused the same snapshot: no re-parse
