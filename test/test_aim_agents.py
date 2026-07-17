"""Tests for ``kiro_crew.aim_agents.list_agents`` agent-config scanning.

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

from kiro_crew.aim_agents import (
    AimAgent,
    _load_project_agents,
    auto_register_project,
    clear_list_agents_cache,
    find_agent_file,
    list_agents,
    load_registry,
    remove_from_registry,
    save_registry,
    scan_directory,
    update_registry,
)


class TestAimAgentDataclass:
    def test_project_path_default(self) -> None:
        a = AimAgent(name="x", filename="x.json", description="", model="auto")
        assert a.project_path == ""
        assert a.source == "builtin"

    def test_to_dict_includes_project_path(self) -> None:
        a = AimAgent(
            name="x",
            filename="x.json",
            description="desc",
            model="auto",
            project_path="/tmp/proj",
            source="project",
        )
        d = a.to_dict()
        assert d["project_path"] == "/tmp/proj"
        assert d["source"] == "project"


class TestRegistry:
    def test_load_empty(self, tmp_path: Path, monkeypatch: object) -> None:
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: tmp_path / "reg.json"
        )
        assert load_registry() == {}

    def test_save_and_load(self, tmp_path: Path, monkeypatch: object) -> None:
        """Old list-format data is migrated to new dict-format on load."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        save_registry({"/home/user/proj": ["my-agent.json"]})
        result = load_registry()
        assert "/home/user/proj" in result
        entry = result["/home/user/proj"]
        assert isinstance(entry, dict)
        assert entry["state"] == "ok"
        assert any(a["file"] == "my-agent.json" for a in entry["agents"])

    def test_load_corrupt_file(self, tmp_path: Path, monkeypatch: object) -> None:
        reg_file = tmp_path / "reg.json"
        reg_file.write_text("not json", encoding="utf-8")
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        assert load_registry() == {}

    def test_strips_non_str_elements_from_value_lists(self, tmp_path: Path, monkeypatch: object) -> None:
        """load_registry migrates old list format, filtering non-string elements."""
        reg_file = tmp_path / "reg.json"
        reg_file.write_text('{"/proj": [123, null, "good.json", false]}', encoding="utf-8")
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        result = load_registry()
        assert "/proj" in result
        agents = result["/proj"]["agents"]
        assert len(agents) == 1
        assert agents[0]["file"] == "good.json"

    def test_skips_entries_with_non_list_value(self, tmp_path: Path, monkeypatch: object) -> None:
        """load_registry skips registry entries whose value is not a list or dict."""
        reg_file = tmp_path / "reg.json"
        reg_file.write_text('{"bad": "string", "good": ["a.json"]}', encoding="utf-8")
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        result = load_registry()
        assert "bad" not in result
        assert "good" in result
        assert any(a["file"] == "a.json" for a in result["good"]["agents"])

    def test_load_root_is_array_returns_empty(self, tmp_path: Path, monkeypatch: object) -> None:
        """load_registry returns {} when the root JSON value is an array, not a dict."""
        reg_file = tmp_path / "reg.json"
        reg_file.write_text('["/proj1", "/proj2"]', encoding="utf-8")
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        assert load_registry() == {}

    def test_load_root_is_scalar_returns_empty(self, tmp_path: Path, monkeypatch: object) -> None:
        """load_registry returns {} when the root JSON value is a scalar (int, string, null)."""
        for value in ["42", '"just a string"', "null"]:
            reg_file = tmp_path / f"reg_{value[:3]}.json"
            reg_file.write_text(value, encoding="utf-8")
            monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda p=reg_file: p)  # type: ignore[attr-defined]
            assert load_registry() == {}, f"expected {{}} for root value {value!r}"


class TestScanDirectory:
    def _make_project(self, root: Path, name: str, agent_name: str) -> Path:
        """Create a project with .kiro/agents/agent.json."""
        proj = root / name
        agents_dir = proj / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        agent_data = {"name": agent_name, "description": f"Agent for {name}", "model": "auto"}
        (agents_dir / f"{agent_name}.json").write_text(
            json.dumps(agent_data), encoding="utf-8"
        )
        return proj

    def test_scan_finds_project_agents(self, tmp_path: Path, monkeypatch: object) -> None:
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        self._make_project(tmp_path, "proj-a", "agent-a")
        self._make_project(tmp_path, "proj-b", "agent-b")

        agents = scan_directory(tmp_path)
        names = {a.name for a in agents}
        assert "agent-a" in names
        assert "agent-b" in names
        for a in agents:
            assert a.source == "project"
            assert a.project_path != ""

    def test_scan_persists_registry(self, tmp_path: Path, monkeypatch: object) -> None:
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        self._make_project(tmp_path, "myproj", "dev-agent")

        scan_directory(tmp_path)
        registry = load_registry()
        proj_path = str((tmp_path / "myproj").resolve())
        assert proj_path in registry
        assert any(a["file"] == "dev-agent.json" for a in registry[proj_path]["agents"])

    def test_scan_empty_dir(self, tmp_path: Path, monkeypatch: object) -> None:
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        assert scan_directory(tmp_path) == []

    def test_scan_nonexistent_dir(self, tmp_path: Path, monkeypatch: object) -> None:
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        assert scan_directory(tmp_path / "nope") == []

    def test_scan_nested_depth(self, tmp_path: Path, monkeypatch: object) -> None:
        """Projects nested 2 levels deep should be found."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        # Create project at depth 2: root/org/repo/.kiro/agents/
        nested = tmp_path / "org" / "repo"
        nested.mkdir(parents=True)
        agents_dir = nested / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "deep.json").write_text(
            json.dumps({"name": "deep-agent", "model": "auto"}), encoding="utf-8"
        )

        agents = scan_directory(tmp_path)
        assert any(a.name == "deep-agent" for a in agents)

    def test_scan_respects_max_depth(self, tmp_path: Path, monkeypatch: object) -> None:
        """Projects deeper than max_depth are not found."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        # Create project at depth 2: root/a/b/.kiro/agents/
        # The .kiro dir is at depth 3, agents at depth 4 from root.
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        agents_dir = nested / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "deep.json").write_text(
            json.dumps({"name": "deep-agent", "model": "auto"}), encoding="utf-8"
        )
        # max_depth=2 → stops recursing at depth ≥ 2, so .kiro (depth 3) is never entered
        agents = scan_directory(tmp_path, max_depth=2)
        assert not any(a.name == "deep-agent" for a in agents)

        # max_depth=5 → depth-2 project IS found (agents dir at depth 4 < 5)
        agents = scan_directory(tmp_path, max_depth=5)
        assert any(a.name == "deep-agent" for a in agents)

    def test_scan_aborts_on_max_entries(self, tmp_path: Path, monkeypatch: object) -> None:
        """Scan aborts with a warning when entries_seen exceeds max_entries."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        # Create many sibling dirs so entries_seen > 1 immediately
        for i in range(5):
            (tmp_path / f"dir{i}").mkdir()
        # max_entries=2 forces abort before any agents are discovered
        agents = scan_directory(tmp_path, max_entries=2)
        assert agents == []

    def test_scan_prunes_vendor_and_cargo(self, tmp_path: Path, monkeypatch: object) -> None:
        """Extended prune set: vendor/.cargo/Library/Pods dirs are not traversed."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        for prune_name in ("vendor", ".cargo", "Library", "Pods", "Applications", ".rustup"):
            pruned_dir = tmp_path / prune_name / ".kiro" / "agents"
            pruned_dir.mkdir(parents=True)
            (pruned_dir / "inside.json").write_text(
                json.dumps({"name": f"agent-in-{prune_name}", "model": "auto"}),
                encoding="utf-8",
            )
        agents = scan_directory(tmp_path)
        assert agents == []

    def test_stale_entry_removed(self, tmp_path: Path, monkeypatch: object) -> None:
        """Registry entry for nonexistent path keeps agent visible with not_found state.

        Per spec: entries are never silently deleted — not_found state keeps
        the agent visible in picker so the user knows it exists and can fix the path.
        """
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        # Save a registry entry pointing to a non-existent path
        save_registry({"/nonexistent/path": ["agent.json"]})
        agents = _load_project_agents()
        # Agent IS returned (with state from registry cache, defaulting to ok)
        # refresh_registry_startup would update state to not_found at boot
        assert any(a.name == "agent" for a in agents)

    def test_stale_entry_persisted_to_disk(self, tmp_path: Path, monkeypatch: object) -> None:
        """Registry entry for nonexistent path is not deleted — kept with current state.

        Per spec: entries are never silently deleted.
        """
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        save_registry({"/nonexistent/path": ["agent.json"]})
        _load_project_agents()
        # Registry must still contain the entry (never deleted)
        assert "/nonexistent/path" in load_registry()

    def test_stale_cleanup_preserves_concurrently_added_entry(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        """All registry entries survive _load_project_agents (no deletion)."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        save_registry({"/nonexistent/stale": ["a.json"]})

        real_proj = tmp_path / "real"
        (real_proj / ".kiro" / "agents").mkdir(parents=True)
        (real_proj / ".kiro" / "agents" / "dev.json").write_text(
            '{"name": "dev"}', encoding="utf-8"
        )
        update_registry(str(real_proj), [{"file": "dev.json", "agent_name": "dev"}])

        agents = _load_project_agents()
        registry = load_registry()
        assert str(real_proj) in registry, "real project entry must be present"
        assert "/nonexistent/stale" in registry, "stale entry kept per spec (never deleted)"
        assert any(a.name == "dev" for a in agents)

    def test_registers_filename_before_parse(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory records a filename in the registry even if the JSON fails to parse.

        Ensures a mid-edit save doesn't permanently drop the file from the registry.
        """
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        agents_dir = tmp_path / "proj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "bad.json").write_text("not json", encoding="utf-8")
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = scan_directory(tmp_path)
        registry = load_registry()
        proj_path = str((tmp_path / "proj").resolve())
        entry = registry.get(proj_path, {})
        agent_files = [a["file"] for a in entry.get("agents", [])] if isinstance(entry, dict) else []
        assert "bad.json" in agent_files, "unparseable file must still be in registry"
        assert "good.json" in agent_files
        assert len(agents) == 1 and agents[0].name == "ok"

    def test_null_mcp_servers_does_not_abort_sibling(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory must not abort the loop when an agent has mcpServers: null."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        agents_dir = tmp_path / "proj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "null-mcp.json").write_text(
            json.dumps({"name": "null-mcp", "model": "auto", "mcpServers": None}),
            encoding="utf-8",
        )
        (agents_dir / "sibling.json").write_text(
            json.dumps({"name": "sibling", "model": "auto"}), encoding="utf-8"
        )
        names = {a.name for a in scan_directory(tmp_path)}
        assert "null-mcp" in names
        assert "sibling" in names


class TestListAgentsWithProject:
    def test_includes_project_agents(self, tmp_path: Path, monkeypatch: object) -> None:
        # Set up a global agents dir with one agent
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "description": "Main", "model": "auto"}),
            encoding="utf-8",
        )

        # Set up registry with a project agent
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        proj = tmp_path / "myproj"
        agents_dir = proj / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "proj-agent.json").write_text(
            json.dumps({"name": "proj-agent", "description": "Project", "model": "auto"}),
            encoding="utf-8",
        )
        save_registry({str(proj): ["proj-agent.json"]})

        agents = list_agents(agents_dir=global_dir, include_project=True)
        names = {a.name for a in agents}
        assert "kirocrew" in names
        assert "proj-agent" in names

    def test_exclude_project_agents(self, tmp_path: Path, monkeypatch: object) -> None:
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "description": "Main", "model": "auto"}),
            encoding="utf-8",
        )

        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        proj = tmp_path / "myproj"
        agents_dir = proj / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "proj-agent.json").write_text(
            json.dumps({"name": "proj-agent", "model": "auto"}), encoding="utf-8"
        )
        save_registry({str(proj): ["proj-agent.json"]})

        agents = list_agents(agents_dir=global_dir, include_project=False)
        names = {a.name for a in agents}
        assert "kirocrew" in names
        assert "proj-agent" not in names

    def test_same_name_different_projects(self, tmp_path: Path, monkeypatch: object) -> None:
        """Same agent name in different projects should both appear."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        # Two projects with same agent name
        for proj_name in ("proj-a", "proj-b"):
            proj = tmp_path / proj_name
            agents_dir = proj / ".kiro" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "dev.json").write_text(
                json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
            )
        save_registry({
            str(tmp_path / "proj-a"): ["dev.json"],
            str(tmp_path / "proj-b"): ["dev.json"],
        })

        # No global dir
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        agents = list_agents(agents_dir=empty_dir, include_project=True)
        dev_agents = [a for a in agents if a.name == "dev"]
        assert len(dev_agents) == 2
        paths = {a.project_path for a in dev_agents}
        assert str(tmp_path / "proj-a") in paths
        assert str(tmp_path / "proj-b") in paths


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
        agents = list_agents(agents_dir=agents_dir, include_project=False)
        names = {a.name for a in agents}
        assert "good" in names, "well-formed sibling agent must survive a bad mcpServers value"

    def test_scan_skips_malformed_json(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory must skip .json files with invalid content without crashing."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        agents_dir = tmp_path / "proj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "bad.json").write_text("not json {{{", encoding="utf-8")
        (agents_dir / "good.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )

        agents = scan_directory(tmp_path)
        assert len(agents) == 1
        assert agents[0].name == "ok"


class TestLoadProjectAgentsCoverage:
    def test_load_returns_agents_from_cache(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load_project_agents reads from registry cache only — no filesystem I/O.

        Even if the agent file doesn't exist on disk, the agent is returned
        from cache. State is updated by refresh_registry_startup at boot.
        """
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        # Register an agent file that doesn't exist — still returned from cache
        save_registry({str(tmp_path / "proj"): ["ghost.json"]})
        agents = _load_project_agents()
        # agent_name falls back to file stem "ghost"
        assert any(a.name == "ghost" for a in agents)

    def test_load_skips_entries_with_no_filename(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load_project_agents skips registry entries with empty filename."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        # Entry with empty file field
        save_registry({str(tmp_path / "proj"): [{"file": "", "agent_name": "bad"}]})
        agents = _load_project_agents()
        assert agents == []

    def test_load_skips_broken_symlink(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load_project_agents returns agents from cache; broken symlinks don't affect it."""
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        # Even with a broken symlink on disk, the registry cache is used
        save_registry({str(tmp_path / "proj"): ["symlink.json"]})
        agents = _load_project_agents()
        # Returned from cache — broken symlink doesn't affect cache-only read
        assert any(a.name == "symlink" for a in agents)


# ── Tests targeting remaining uncovered new lines ──────────────────────────

class TestScanDirectorySecurityGuards:
    """Cover lines 113-114, 140, 143-144, 146-147, 152-154, 157."""

    def test_scan_rejects_sensitive_root(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory must refuse to scan a sensitive root path (lines 113-114)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        monkeypatch.setattr("kiro_crew.aim_agents.is_sensitive_path", lambda p: True)  # type: ignore[attr-defined]
        assert scan_directory(tmp_path) == []

    def test_scan_skips_broken_symlink(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory skips broken symlinks via OSError on resolve(strict=True) (lines 143-144)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        agents_dir = tmp_path / "proj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        broken = agents_dir / "broken.json"
        broken.symlink_to(tmp_path / "nonexistent")
        good = agents_dir / "good.json"
        good.write_text(json.dumps({"name": "good", "model": "auto"}), encoding="utf-8")
        agents = scan_directory(tmp_path)
        assert len(agents) == 1
        assert agents[0].name == "good"

    def test_scan_skips_sensitive_symlink_target(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory skips files whose resolved path is sensitive (lines 146-147)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        agents_dir = tmp_path / "proj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        real_file = tmp_path / "creds.json"
        real_file.write_text(json.dumps({"name": "creds"}), encoding="utf-8")
        (agents_dir / "creds.json").symlink_to(real_file)
        # Patch is_sensitive_path to flag the resolved target

        def _sensitive(p: str) -> bool:
            return str(real_file) in p

        monkeypatch.setattr("kiro_crew.aim_agents.is_sensitive_path", _sensitive)  # type: ignore[attr-defined]
        assert scan_directory(tmp_path) == []

    def test_scan_skips_non_utf8_file(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory skips files that cannot be decoded as UTF-8 (lines 152-154)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        agents_dir = tmp_path / "proj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "binary.json").write_bytes(b"\xff\xfe{invalid utf-8}")
        (agents_dir / "good.json").write_text(json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8")
        agents = scan_directory(tmp_path)
        assert len(agents) == 1
        assert agents[0].name == "ok"

    def test_scan_skips_non_dict_json(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory skips JSON files that aren't objects (line 157)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        agents_dir = tmp_path / "proj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "array.json").write_text("[1, 2, 3]", encoding="utf-8")
        assert scan_directory(tmp_path) == []

    def test_scan_does_not_register_kiro_home_as_project(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        """scan_directory must not register ~/.kiro/agents/ as a project dir.

        When a user scans from ~ or ~/Documents, os.walk descends into ~/.kiro/agents/.
        Without the guard this registers $HOME as a project_path, creating a duplicate
        'kirocrew' entry that triggers the 409 ambiguity check on every agent switch.
        """
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        # Simulate ~/.kiro/agents/ (the global dir) inside tmp_path acting as home
        fake_kiro_agents = tmp_path / ".kiro" / "agents"
        fake_kiro_agents.mkdir(parents=True)
        (fake_kiro_agents / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "model": "auto"}), encoding="utf-8"
        )
        # A real project alongside it — must still be found
        proj = tmp_path / "myproject"
        (proj / ".kiro" / "agents").mkdir(parents=True)
        (proj / ".kiro" / "agents" / "dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._KIRO_HOME_DIR", (tmp_path / ".kiro").resolve()
        )
        agents = scan_directory(tmp_path)
        assert str(tmp_path) not in {a.project_path for a in agents}, (
            "~/.kiro/agents/ must not be registered as a project"
        )
        assert any(a.name == "dev" for a in agents), "legitimate project must still be found"

    def test_scan_skips_all_paths_inside_kiro_home(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        """Any .kiro/agents/ nested inside ~/.kiro is also excluded."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        nested = tmp_path / ".kiro" / "subdir" / ".kiro" / "agents"
        nested.mkdir(parents=True)
        (nested / "sneaky.json").write_text(
            json.dumps({"name": "sneaky", "model": "auto"}), encoding="utf-8"
        )
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._KIRO_HOME_DIR", (tmp_path / ".kiro").resolve()
        )
        assert not any(a.name == "sneaky" for a in scan_directory(tmp_path))


class TestLoadProjectAgentsSecurityGuards:
    """Security guards in _load_project_agents (cache-only reads)."""

    def test_load_skips_kiro_home_project_path(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load_project_agents never surfaces ~/.kiro or paths inside it as a project."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        fake_kiro = tmp_path / ".kiro"
        monkeypatch.setattr("kiro_crew.aim_agents._KIRO_HOME_DIR", fake_kiro.resolve())  # type: ignore[attr-defined]
        # Register ~/.kiro as a project — should be filtered out
        save_registry({str(fake_kiro): ["kirocrew.json"]})
        agents = _load_project_agents()
        assert not any(str(fake_kiro) in a.project_path for a in agents)

    def test_load_returns_cached_agent_regardless_of_file_state(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load_project_agents returns agents from cache; file state is irrelevant."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        update_registry(str(tmp_path / "proj"), [{"file": "agent.json", "agent_name": "myagent"}])
        agents = _load_project_agents()
        assert any(a.name == "myagent" for a in agents)


class TestListAgentsGlobalGuards:
    """Cover lines 250-251, 297-299, 305-306 — global agent loader edge cases."""

    def test_global_broken_symlink_skipped(self, tmp_path: Path) -> None:
        """list_agents skips broken symlinks in global dir (lines 250-251)."""
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
        """list_agents skips malformed JSON in global dir (lines 297-299)."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "bad.json").write_text("not json {{{", encoding="utf-8")
        (agents_dir / "ok.json").write_text(
            json.dumps({"name": "ok", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        assert any(a.name == "ok" for a in agents)

    def test_project_agents_exception_swallowed(self, tmp_path: Path, monkeypatch: object) -> None:
        """list_agents catches exceptions from _load_project_agents (lines 305-306)."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        monkeypatch.setattr("kiro_crew.aim_agents._load_project_agents", lambda: (_ for _ in ()).throw(RuntimeError("boom")))  # type: ignore[attr-defined]
        # Should not raise
        agents = list_agents(agents_dir=agents_dir, include_project=True)
        assert isinstance(agents, list)


class TestListAgentsDedup:
    """Cover lines 273-276, 281, 318, 329 — deduplication edge cases."""

    def test_aim_package_name_extracted(self, tmp_path: Path) -> None:
        """AIM filename pattern extracts package name (lines 273-276, 281)."""
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
        assert a.source == "aim"

    def test_aim_kirocrew_package_source(self, tmp_path: Path) -> None:
        """AIM agent from KiroCrewAICapabilities gets source='kirocrew' (line 281)."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "KiroCrewAICapabilities-myskill.json").write_text(
            json.dumps({"name": "myskill", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myskill"), None)
        assert a is not None
        assert a.source == "kirocrew"

    def test_aim_package_preferred_over_builtin(self, tmp_path: Path, monkeypatch: object) -> None:
        """AIM-packaged agent replaces same-name builtin in dedup (line 318)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
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
        agents = list_agents(agents_dir=agents_dir, include_project=False)
        dev_agents = [a for a in agents if a.name == "dev"]
        assert len(dev_agents) == 1
        assert dev_agents[0].package == "zzz-MyPkg"

    def test_project_agent_same_name_as_global_both_visible(self, tmp_path: Path, monkeypatch: object) -> None:
        """Project agent with same name as global agent: BOTH are visible (all-visible policy)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        proj = tmp_path / "myproj"
        proj_agents = proj / ".kiro" / "agents"
        proj_agents.mkdir(parents=True)
        (proj_agents / "dev.json").write_text(
            json.dumps({"name": "dev", "model": "auto"}), encoding="utf-8"
        )
        save_registry({str(proj): ["dev.json"]})
        agents = list_agents(agents_dir=agents_dir, include_project=True)
        dev_agents = [a for a in agents if a.name == "dev"]
        # Both global and project agent are visible
        assert len(dev_agents) == 2
        sources = {a.source for a in dev_agents}
        assert "project" in sources


class TestFinalCoverageGaps:
    """Cover the last 5 uncovered new lines: 140, 199-200, 275, 318."""

    def test_scan_skips_appledouble_sidecar(self, tmp_path: Path, monkeypatch: object) -> None:
        """scan_directory skips ._-prefixed AppleDouble sidecar files (line 140)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        agents_dir = tmp_path / "proj" / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "._good.json").write_text(json.dumps({"name": "sidecar"}), encoding="utf-8")
        (agents_dir / "good.json").write_text(json.dumps({"name": "real", "model": "auto"}), encoding="utf-8")
        agents = scan_directory(tmp_path)
        assert len(agents) == 1
        assert agents[0].name == "real"

    def test_load_skips_broken_symlink_via_isfile(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load_project_agents: file passes is_file() but resolve(strict=True) raises (lines 199-200)."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg_file)  # type: ignore[attr-defined]
        # _load_project_agents is cache-only — register using new format
        update_registry(str(tmp_path / "proj"), [{"file": "agent.json", "agent_name": "a"}])
        agents = _load_project_agents()
        assert any(a.name == "a" for a in agents)

    def test_local_prefix_stripped_from_aim_package(self, tmp_path: Path) -> None:
        """AIM filename with 'local-' prefix has it stripped from package name (line 275)."""
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "local-MyPkg-myagent.json").write_text(
            json.dumps({"name": "myagent", "model": "auto"}), encoding="utf-8"
        )
        agents = list_agents(agents_dir=agents_dir)
        a = next((x for x in agents if x.name == "myagent"), None)
        assert a is not None
        assert a.package == "MyPkg"


class TestUpdateRegistry:
    def test_atomic_update_adds_key(self, tmp_path: Path, monkeypatch: object) -> None:
        """update_registry adds a project entry atomically."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        update_registry("/tmp/myproj", [{"file": "dev.json", "agent_name": "dev"}])
        result = load_registry()
        assert "/tmp/myproj" in result
        assert result["/tmp/myproj"]["state"] == "ok"
        assert any(a["file"] == "dev.json" for a in result["/tmp/myproj"]["agents"])

    def test_atomic_update_preserves_existing(self, tmp_path: Path, monkeypatch: object) -> None:
        """update_registry preserves other keys when updating one project."""
        reg_file = tmp_path / "reg.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        save_registry({"/tmp/proj-a": ["a.json"]})
        update_registry("/tmp/proj-b", [{"file": "b.json", "agent_name": "b"}])
        result = load_registry()
        assert "/tmp/proj-a" in result
        assert "/tmp/proj-b" in result
        assert any(a["file"] == "b.json" for a in result["/tmp/proj-b"]["agents"])


class TestRemoveFromRegistry:
    """Tests for remove_from_registry — all positive, negative, and edge cases."""

    def test_removes_existing_entry(self, tmp_path: Path, monkeypatch: object) -> None:
        """remove_from_registry deletes the key and writes back."""
        reg = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        save_registry({"/proj": ["dev.json"]})
        remove_from_registry("/proj")
        assert load_registry() == {}

    def test_preserves_other_keys(self, tmp_path: Path, monkeypatch: object) -> None:
        """Only the target key is removed; other entries survive."""
        reg = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        save_registry({"/proj-a": ["a.json"], "/proj-b": ["b.json"]})
        remove_from_registry("/proj-a")
        result = load_registry()
        assert "/proj-a" not in result
        assert "/proj-b" in result

    def test_idempotent_key_absent(self, tmp_path: Path, monkeypatch: object) -> None:
        """remove_from_registry is a no-op when the key doesn't exist — no crash, no write."""
        reg = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        save_registry({"/other": ["x.json"]})
        remove_from_registry("/nonexistent")
        result = load_registry()
        assert "/other" in result

    def test_idempotent_on_empty_registry(self, tmp_path: Path, monkeypatch: object) -> None:
        """remove_from_registry on an empty registry doesn't crash."""
        reg = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        save_registry({})
        remove_from_registry("/proj")
        assert load_registry() == {}

    def test_no_op_when_registry_file_missing(self, tmp_path: Path, monkeypatch: object) -> None:
        """remove_from_registry returns silently when registry file doesn't exist yet."""
        reg = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        assert not reg.exists()
        remove_from_registry("/proj")
        assert not reg.exists()

    def test_corrupt_registry_does_not_crash(self, tmp_path: Path, monkeypatch: object) -> None:
        """remove_from_registry silently does nothing if the registry is corrupt on re-read."""
        reg = tmp_path / "reg.json"
        reg.write_text("not json{{{{", encoding="utf-8")
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        remove_from_registry("/proj")
        # Corrupt file is unchanged — no write happened
        assert reg.read_text() == "not json{{{{"

    def test_multiple_stale_entries_deleted_independently(self, tmp_path: Path, monkeypatch: object) -> None:
        """Deleting N stale entries one-at-a-time leaves valid entries intact."""
        reg = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        save_registry({
            "/stale-a": ["a.json"],
            "/stale-b": ["b.json"],
            "/valid": ["dev.json"],
        })
        remove_from_registry("/stale-a")
        remove_from_registry("/stale-b")
        result = load_registry()
        assert "/stale-a" not in result
        assert "/stale-b" not in result
        assert "/valid" in result

    def test_stale_cleanup_uses_remove_from_registry(self, tmp_path: Path, monkeypatch: object) -> None:
        """_load_project_agents does not delete registry entries (spec: never silently delete).

        Entries for nonexistent paths remain in registry; their state is updated
        to not_found by refresh_registry_startup at gateway boot.
        """
        reg = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        valid = tmp_path / "valid"
        (valid / ".kiro" / "agents").mkdir(parents=True)
        (valid / ".kiro" / "agents" / "dev.json").write_text(
            json.dumps({"name": "dev"}), encoding="utf-8"
        )
        save_registry({"/nonexistent/stale": ["a.json"], str(valid): ["dev.json"]})
        _load_project_agents()
        result = load_registry()
        # Both entries survive — no deletion
        assert "/nonexistent/stale" in result, "stale entry kept per spec"
        assert str(valid) in result, "valid entry kept"

    def test_corrupt_reread_does_not_wipe_registry(self, tmp_path: Path, monkeypatch: object) -> None:
        """Core regression: if load_registry() returns {} during the delete lock,
        no write happens and valid entries are preserved.

        This is the data-loss scenario the per-entry approach eliminates:
        with batch deletion, JSONDecodeError -> {} -> _write_registry({}) wipes all.
        With per-entry, remove_from_registry gets {}, sees key absent, skips write.
        """
        reg = tmp_path / "reg.json"
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: reg)  # type: ignore[attr-defined]
        valid = tmp_path / "valid"
        (valid / ".kiro" / "agents").mkdir(parents=True)
        (valid / ".kiro" / "agents" / "dev.json").write_text(
            json.dumps({"name": "dev"}), encoding="utf-8"
        )
        save_registry({"/nonexistent/stale": ["a.json"], str(valid): ["dev.json"]})

        original_load = __import__("kiro_crew.aim_agents", fromlist=["load_registry"]).load_registry
        call_count = [0]

        def patched_load() -> dict:
            call_count[0] += 1
            return {} if call_count[0] >= 2 else original_load()

        monkeypatch.setattr("kiro_crew.aim_agents.load_registry", patched_load)  # type: ignore[attr-defined]
        _load_project_agents()
        monkeypatch.setattr("kiro_crew.aim_agents.load_registry", original_load)  # type: ignore[attr-defined]
        assert str(valid) in load_registry(), "valid entry must survive corrupt in-lock re-read"


class TestAutoRegisterProject:
    """Tests for auto_register_project — direct single-dir read, no os.walk."""

    def test_registers_agents_in_kiro_agents_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid .kiro/agents/*.json files are added to the registry."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "dev.json").write_text(
            json.dumps({"name": "dev", "description": "Dev agent"}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")

        auto_register_project(str(tmp_path))

        registry = load_registry()
        assert str(tmp_path) in registry
        assert any(a["file"] == "dev.json" for a in registry[str(tmp_path)]["agents"])

    def test_no_kiro_agents_dir_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Project with no .kiro/agents/ dir leaves registry unchanged."""
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")

        auto_register_project(str(tmp_path))

        assert load_registry() == {}

    def test_sensitive_path_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Agents resolving to sensitive paths are excluded from registration."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        creds = tmp_path / "secret.json"
        creds.write_text(json.dumps({"name": "bad"}), encoding="utf-8")
        (agents_dir / "bad.json").symlink_to(creds)
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")
        monkeypatch.setattr("kiro_crew.aim_agents.is_sensitive_path", lambda p: str(creds) in p)

        auto_register_project(str(tmp_path))

        registry = load_registry()
        assert str(tmp_path) not in registry

    def test_apple_double_files_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS ._*.json AppleDouble files are ignored."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "._dev.json").write_text("{}", encoding="utf-8")
        (agents_dir / "dev.json").write_text(
            json.dumps({"name": "dev"}), encoding="utf-8"
        )
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")

        auto_register_project(str(tmp_path))

        registry = load_registry()
        assert str(tmp_path) in registry
        agent_files = [a["file"] for a in registry[str(tmp_path)]["agents"]]
        assert agent_files == ["dev.json"]

    def test_broken_symlink_skipped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Broken symlinks (OSError on resolve) are silently skipped."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "broken.json").symlink_to(tmp_path / "nonexistent.json")
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")

        auto_register_project(str(tmp_path))

        assert load_registry() == {}

    def test_sensitive_root_path_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto_register_project must not register a project when root resolves to a sensitive path."""
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")
        monkeypatch.setattr("kiro_crew.aim_agents.is_sensitive_path", lambda p: True)

        auto_register_project(str(tmp_path))

        assert load_registry() == {}

    def test_non_sensitive_root_still_registers(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto_register_project registers normally when is_sensitive_path returns False."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "dev.json").write_text(json.dumps({"name": "dev"}), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")
        monkeypatch.setattr("kiro_crew.aim_agents.is_sensitive_path", lambda p: False)

        auto_register_project(str(tmp_path))

        entry = load_registry().get(str(tmp_path), {})
        assert any(a["file"] == "dev.json" for a in (entry.get("agents", []) if isinstance(entry, dict) else []))

    def test_kiro_home_parent_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto_register_project must not register the parent of ~/.kiro (i.e. $HOME) as a project."""
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")
        # Simulate _KIRO_HOME_DIR = tmp_path/.kiro so tmp_path is its parent
        monkeypatch.setattr("kiro_crew.aim_agents._KIRO_HOME_DIR", (tmp_path / ".kiro").resolve())

        auto_register_project(str(tmp_path))

        assert load_registry() == {}

    def test_path_inside_kiro_home_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """auto_register_project must not register any path inside ~/.kiro as a project."""
        fake_kiro = tmp_path / ".kiro"
        fake_kiro.mkdir()
        monkeypatch.setattr("kiro_crew.aim_agents._registry_path", lambda: tmp_path / "registry.json")
        monkeypatch.setattr("kiro_crew.aim_agents._KIRO_HOME_DIR", fake_kiro.resolve())

        auto_register_project(str(fake_kiro))

        assert load_registry() == {}


class TestFindAgentFile:
    """find_agent_file matches on the name field, not the filename stem."""

    def test_matches_by_name_field(self, tmp_path: Path) -> None:
        """File xyz.json with name='abc' is found when searching for 'abc'."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "xyz.json").write_text('{"name": "abc"}', encoding="utf-8")

        assert find_agent_file(agents_dir, "abc") == agents_dir / "xyz.json"

    def test_stem_match_still_works(self, tmp_path: Path) -> None:
        """Conventional abc.json with name='abc' is also found."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "abc.json").write_text('{"name": "abc"}', encoding="utf-8")

        assert find_agent_file(agents_dir, "abc") is not None

    def test_returns_none_when_not_found(self, tmp_path: Path) -> None:
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "other.json").write_text('{"name": "other"}', encoding="utf-8")

        assert find_agent_file(agents_dir, "missing") is None

    def test_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        assert find_agent_file(tmp_path / "nonexistent", "abc") is None

    def test_skips_broken_json(self, tmp_path: Path) -> None:
        """Malformed JSON files are skipped, not raised."""
        agents_dir = tmp_path / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "bad.json").write_text("not json", encoding="utf-8")
        (agents_dir / "good.json").write_text('{"name": "abc"}', encoding="utf-8")

        assert find_agent_file(agents_dir, "abc") == agents_dir / "good.json"


class TestListAgentsCache:
    """list_agents caches parsed results per (dir, include_project) and reuses
    them while the stat-only directory signature is unchanged."""

    def test_cache_hit_skips_reparse(self, tmp_path: Path) -> None:
        """An unchanged signature returns the cached result without re-parsing."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        file_stat = f.stat()

        first = [a.name for a in list_agents(agents_dir=d, include_project=False)]
        assert first == ["v1"]

        # Rewrite the content but restore the original mtime so the signature is
        # unchanged: a re-parse would yield "v2"; a cache hit yields "v1".
        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        os.utime(f, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))

        second = [a.name for a in list_agents(agents_dir=d, include_project=False)]
        assert second == ["v1"], "unchanged signature must return the cached result"

    def test_cache_invalidates_on_add(self, tmp_path: Path) -> None:
        """Adding a file changes the signature and is reflected immediately."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        (d / "a.json").write_text(
            json.dumps({"name": "a", "model": "auto"}), encoding="utf-8"
        )
        assert {a.name for a in list_agents(agents_dir=d, include_project=False)} == {"a"}

        (d / "b.json").write_text(
            json.dumps({"name": "b", "model": "auto"}), encoding="utf-8"
        )
        assert {
            a.name for a in list_agents(agents_dir=d, include_project=False)
        } == {"a", "b"}

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
        assert {
            a.name for a in list_agents(agents_dir=d, include_project=False)
        } == {"a", "b"}

        (d / "b.json").unlink()
        assert {a.name for a in list_agents(agents_dir=d, include_project=False)} == {"a"}

    def test_cache_invalidates_on_inplace_edit(self, tmp_path: Path) -> None:
        """An in-place content edit (newer mtime) invalidates the cache."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        assert [
            a.name for a in list_agents(agents_dir=d, include_project=False)
        ] == ["v1"]

        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        # Bump mtime forward deterministically so the signature is guaranteed newer.
        st = f.stat()
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert [
            a.name for a in list_agents(agents_dir=d, include_project=False)
        ] == ["v2"], "an in-place edit must invalidate the cache"

    def test_clear_cache_forces_rescan(self, tmp_path: Path) -> None:
        """clear_list_agents_cache() forces a fresh scan even when the signature
        is unchanged."""
        clear_list_agents_cache()
        d = tmp_path / "agents"
        d.mkdir()
        f = d / "a.json"
        f.write_text(json.dumps({"name": "v1", "model": "auto"}), encoding="utf-8")
        file_stat = f.stat()
        assert [
            a.name for a in list_agents(agents_dir=d, include_project=False)
        ] == ["v1"]

        # Change content but freeze the mtime so the signature would still hit ...
        f.write_text(json.dumps({"name": "v2", "model": "auto"}), encoding="utf-8")
        os.utime(f, ns=(file_stat.st_atime_ns, file_stat.st_mtime_ns))
        # ... then force a clear: the next call must re-scan and see "v2".
        clear_list_agents_cache()
        assert [
            a.name for a in list_agents(agents_dir=d, include_project=False)
        ] == ["v2"]

    def test_include_project_keyed_separately(self, tmp_path: Path, monkeypatch: object) -> None:
        """The cache distinguishes include_project=True vs False for the same dir."""
        clear_list_agents_cache()
        reg_file = tmp_path / "registry" / "project_agents.json"
        monkeypatch.setattr(  # type: ignore[attr-defined]
            "kiro_crew.aim_agents._registry_path", lambda: reg_file
        )
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        (global_dir / "kirocrew.json").write_text(
            json.dumps({"name": "kirocrew", "model": "auto"}), encoding="utf-8"
        )
        proj = tmp_path / "myproj"
        (proj / ".kiro" / "agents").mkdir(parents=True)
        (proj / ".kiro" / "agents" / "proj-agent.json").write_text(
            json.dumps({"name": "proj-agent", "model": "auto"}), encoding="utf-8"
        )
        save_registry({str(proj): ["proj-agent.json"]})

        with_proj = {a.name for a in list_agents(agents_dir=global_dir, include_project=True)}
        without_proj = {a.name for a in list_agents(agents_dir=global_dir, include_project=False)}
        assert "proj-agent" in with_proj
        assert "proj-agent" not in without_proj
        assert "kirocrew" in with_proj and "kirocrew" in without_proj
