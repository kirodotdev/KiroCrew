"""Tests for agent discovery in ``agent_discovery.py``.

Focus on the robustness/security guards around scanning ``~/.kiro/agents/*.json``:
- macOS AppleDouble (``._*.json``) and non-UTF-8 files must not crash the scan.
- A ``*.json`` symlink pointing at a sensitive credential file must NOT be read.

Tests use a tmp_path fake $HOME so the real filesystem is never touched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew.agent_discovery import clear_list_agents_cache, list_agents


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _agents_dir(home: Path) -> Path:
    d = home / ".kiro" / "agents"
    d.mkdir(parents=True)
    return d


class TestListAgentsRobustness:
    def test_survives_non_utf8_and_appledouble(self, fake_home):
        """A non-UTF-8 file (AppleDouble ``._*.json`` sidecar or arbitrary
        binary ``*.json``) must be skipped, not raise UnicodeDecodeError."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        # AppleDouble sidecar: starts with "._" and is non-UTF-8 binary.
        (d / "._good.json").write_bytes(b"\x02\x00\x00\x00\xa3\x80\x81 not utf-8")
        # Arbitrary non-UTF-8 *.json that is not an AppleDouble name either.
        (d / "binary.json").write_bytes(b"\xff\xfe\x00\x01\xa3")

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]

    def test_skips_non_dict_json(self, fake_home):
        """Valid JSON that is not an object (e.g. a top-level array) must be
        skipped, not raise AttributeError on data.get()."""
        d = _agents_dir(fake_home)
        (d / "good.json").write_text(json.dumps({"name": "good"}))
        (d / "array.json").write_text(json.dumps([1, 2, 3]))
        (d / "scalar.json").write_text(json.dumps("just a string"))

        names = [a.name for a in list_agents(agents_dir=d)]
        assert names == ["good"]

    def test_skips_symlink_to_sensitive_file(self, fake_home):
        """A ``*.json`` symlink under ~/.kiro/agents/ that resolves to a
        sensitive credential path must NOT be read or returned."""
        d = _agents_dir(fake_home)
        (d / "real.json").write_text(json.dumps({"name": "real"}))

        # Plant a credential file under the sensitive ~/.aws dir and symlink
        # it in as a fake agent config. Even though it is valid JSON that
        # would parse, the sensitive-path guard must skip it.
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text(json.dumps({"name": "evil"}))
        (d / "evil.json").symlink_to(creds)

        names = [a.name for a in list_agents(agents_dir=d)]
        assert "evil" not in names
        assert names == ["real"]

    def test_skips_non_dict_mcp_servers(self, tmp_path: Path) -> None:
        """list_agents must not crash when mcpServers is a list instead of a dict.

        AttributeError: 'list' object has no attribute 'keys' previously escaped
        the except clause, aborting the entire loop and dropping all sibling agents.
        """
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.json").write_text(
            json.dumps({"name": "bad", "model": "auto", "mcpServers": ["a", "b"]}),
            encoding="utf-8",
        )
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "good", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        names = {a.name for a in agents}
        assert "good" in names, "well-formed sibling agent must survive a bad mcpServers value"


class TestListAgentsGlobalGuards:
    """Global agent loader edge cases."""

    def test_global_broken_symlink_skipped(self, tmp_path: Path) -> None:
        """list_agents skips broken symlinks in the global dir."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        broken = agents_dir / "broken.json"
        broken.symlink_to(tmp_path / "nonexistent.json")
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        assert any(a.name == "ok" for a in agents)
        assert not any(a.name == "broken" for a in agents)

    def test_global_bad_json_skipped(self, tmp_path: Path) -> None:
        """list_agents skips malformed JSON in the global dir."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.json").write_text("not json {{{", encoding="utf-8")
        (agents_dir / "ok.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        assert any(a.name == "ok" for a in agents)


class TestListAgentsDedup:
    """Deduplication and AIM package-name extraction edge cases."""

    def test_aim_package_name_extracted(self, tmp_path: Path) -> None:
        """AIM filename pattern extracts package name."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # AIM filename pattern: {package}-{agent_name}.json
        (agents_dir / "MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myagent"), None)
        assert a is not None
        assert a.package == "MyPkg"
        assert a.source == "package"

    def test_aim_kirocrew_package_source(self, tmp_path: Path) -> None:
        """A package-installed agent (e.g. KiroCrewAICapabilities) gets source='package'."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "KiroCrewAICapabilities-myskill.json").write_text(
            json.dumps({"name": "myskill", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myskill"), None)
        assert a is not None
        assert a.source == "package"

    def test_aim_package_preferred_over_builtin(self, tmp_path: Path) -> None:
        """AIM-packaged agent replaces same-name builtin in dedup."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        # "dev.json" is builtin (stem == name). "zzz-MyPkg-dev.json" is AIM-packaged.
        # sorted() puts "dev.json" first, so builtin is seen first, then AIM replaces it.
        (agents_dir / "dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        (agents_dir / "zzz-MyPkg-dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        dev_agents = [a for a in agents if a.name == "dev"]
        assert len(dev_agents) == 1
        assert dev_agents[0].package == "zzz-MyPkg"

    def test_local_prefix_stripped_from_aim_package(self, tmp_path: Path) -> None:
        """AIM filename with 'local-' prefix has it stripped from package name."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "local-MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myagent"), None)
        assert a is not None
        assert a.package == "MyPkg"


class TestListAgentsCache:
    """list_agents caches parsed results per directory and reuses them while the
    stat-only directory signature is unchanged."""

    def test_cache_hit_skips_reparse(self, tmp_path: Path) -> None:
        """An unchanged signature returns the cached result without re-parsing."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        file_stat = f.stat()

        first = [a.name for a in list_agents(agents_dir=d)]
        assert first == ["v1"]

        # Rewrite the content but restore the original mtime so the signature is
        # unchanged: a re-parse would yield "v2"; a cache hit yields "v1".
        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        os.utime(f, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))

        second = [a.name for a in list_agents(agents_dir=d)]
        assert second == ["v1"], "unchanged signature must return the cached result"

    def test_cache_invalidates_on_add(self, tmp_path: Path) -> None:
        """Adding a file changes the signature and is reflected immediately."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        (d / "a.json").write_text(
            json.dumps({"name": "a", "model": "auto"}), encoding="utf-8"
        )
        assert {a.name for a in list_agents(agents_dir=d)} == {"a"}

        (d / "b.json").write_text(
            json.dumps({"name": "b", "model": "auto"}), encoding="utf-8"
        )
        assert {a.name for a in list_agents(agents_dir=d)} == {"a", "b"}

    def test_cache_invalidates_on_remove(self, tmp_path: Path) -> None:
        """Removing a file changes the signature and is reflected immediately."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        (d / "a.json").write_text(
            json.dumps({"name": "a", "model": "auto"}), encoding="utf-8"
        )
        (d / "b.json").write_text(
            json.dumps({"name": "b", "model": "auto"}), encoding="utf-8"
        )
        assert {a.name for a in list_agents(agents_dir=d)} == {"a", "b"}

        (d / "b.json").unlink()
        assert {a.name for a in list_agents(agents_dir=d)} == {"a"}

    def test_cache_invalidates_on_inplace_edit(self, tmp_path: Path) -> None:
        """An in-place content edit (newer mtime) invalidates the cache."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        assert [a.name for a in list_agents(agents_dir=d)] == ["v1"]

        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        # Bump mtime forward deterministically so the signature is guaranteed newer.
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert [a.name for a in list_agents(agents_dir=d)] == ["v2"], (
            "an in-place edit must invalidate the cache"
        )

    def test_clear_cache_forces_rescan(self, tmp_path: Path) -> None:
        """clear_list_agents_cache() forces a fresh scan even when the signature
        is unchanged."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        file_stat = f.stat()
        assert [a.name for a in list_agents(agents_dir=d)] == ["v1"]

        # Change content but freeze the mtime so the signature would still hit ...
        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        os.utime(f, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))
        # ... then force a clear: the next call must re-scan and see "v2".
        clear_list_agents_cache()
        assert [a.name for a in list_agents(agents_dir=d)] == ["v2"]
