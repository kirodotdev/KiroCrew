"""Tests for agent config installation."""

from __future__ import annotations

import json
import os
import sys
import unittest.mock
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import agent_state
from kiro_crew.agent import install_agent, migrate_agent_specs


def _bundled_defaults(tmp_path: Path) -> Path:
    """Write a minimal bundled defaults.json and return its parent dir."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    defaults = {
        "model": "claude-default",
        "tools": ["ReadFile"],
        "allowedTools": ["ReadFile"],
        "mcpServers": {},
        "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /"]}},
        "hooks": {"preToolUse": "audit"},
    }
    (cfg_dir / "defaults.json").write_text(json.dumps(defaults))
    (cfg_dir / "prompt.md").write_text("system prompt")
    return cfg_dir


_DEFAULT_MANAGED_MCPS = {
    "kirocrew-cron": {"command": "/usr/bin/kirocrew", "args": ["mcp-cron"]},
    "kirocrew-core": {"command": "/usr/bin/kirocrew", "args": ["mcp-core"]},
}


def _run_install(tmp_path: Path, cfg_dir: Path, managed_mcps: dict | None = None, **kwargs) -> Path:  # type: ignore[return]
    """Run install_agent with all module globals patched to tmp_path."""
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    prompt = cfg_dir / "prompt.md"

    # Isolate tests from the caller's real ~/.kiro/hooks/ by disabling
    # autoimport in the patched config.  Tests that want to exercise autoimport
    # should override config_path themselves.
    mc_config = tmp_path / "empty_mc_config.json"
    if not mc_config.exists():
        mc_config.write_text(json.dumps({"agent": {"kiro_hooks_autoimport": False}}))

    _user_home = tmp_path / "kirocrew_home"
    patches = [
        patch.multiple(
            "kiro_crew.agent",
            KIRO_AGENTS_DIR=kiro_dir,
            _BUNDLED_CFG_DIR=cfg_dir,
            _KIROCREW_BIN="/usr/bin/kirocrew",
            _MANAGED_MCP_SERVERS=(
                managed_mcps if managed_mcps is not None else _DEFAULT_MANAGED_MCPS
            ),
            _KIRO_MCP_JSON=tmp_path / "fake_kiro_mcp.json",
            _CC_MCP_JSON=tmp_path / "fake_cc_mcp.json",
        ),
        patch("kiro_crew.agent._user_dir", lambda: _user_home),
        patch("kiro_crew.agent._prompt_path", return_value=prompt),
        patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json"),
        patch("kiro_crew.agent._project_dir", return_value=None),
        patch("kiro_crew.agent._aim_skill_paths", return_value=[]),
        patch("kiro_crew.agent.shutil.which", side_effect=lambda c, **kw: c),
        patch("kiro_crew.agent._mc_config_path", return_value=mc_config),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return install_agent(**kwargs)


class TestInstallAgent:
    def test_fresh_install_generates_from_defaults(self, tmp_path: Path):
        """No existing kirocrew.json → config built from defaults."""
        cfg_dir = _bundled_defaults(tmp_path)
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"
        assert "ReadFile" in config["tools"]

    def test_existing_config_preserves_user_model(self, tmp_path: Path):
        """Existing kirocrew.json → user's model choice survives restart."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["ReadFile", "WriteFile"],
            "allowedTools": ["ReadFile", "WriteFile"],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": ["old"]}},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-user-custom"
        assert "WriteFile" in config["tools"]

    def test_fresh_install_marks_model_managed(self, tmp_path: Path):
        """Fresh install tracks the shipped default via the sidecar (not the spec)."""
        cfg_dir = _bundled_defaults(tmp_path)
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"
        # kiro spec must stay schema-clean; managed-state lives in the sidecar.
        assert "model_managed" not in config
        assert agent_state.get_model_managed("kirocrew") is True

    def test_managed_config_tracks_defaults_bump(self, tmp_path: Path):
        """A managed (sidecar) config re-syncs model from defaults.json on a bump."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Pre-migration polluted spec: model_managed lives in the spec. The
        # install migration lifts it into the sidecar and strips it.
        existing = {
            "model": "claude-old-default",
            "model_managed": True,
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"
        assert "model_managed" not in config
        assert agent_state.get_model_managed("kirocrew") is True

    def test_legacy_config_without_marker_frozen(self, tmp_path: Path):
        """A legacy config (no marker) is grandfathered: model untouched."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-legacy-4.6",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-legacy-4.6"
        assert "model_managed" not in config
        assert agent_state.get_model_managed("kirocrew") is None

    def test_explicitly_frozen_config_not_tracked(self, tmp_path: Path):
        """A frozen pick (model_managed=False) is never re-synced to default."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-pinned",
            "model_managed": False,
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-user-pinned"
        assert "model_managed" not in config
        assert agent_state.get_model_managed("kirocrew") is False

    def test_existing_config_refreshes_security_fields(self, tmp_path: Path):
        """hooks are always overwritten from bundled.

        ``deniedCommands`` are NO LONGER injected into the agent spec (command
        denial moved to KiroCrew's own hooks.py PreToolUse gate). A stale
        ``deniedCommands`` left by an older build is STRIPPED on refresh so
        kiro-cli stops enforcing it ahead of the hook gate — otherwise an
        upgraded install's Settings > Security opt-out would silently stay
        blocked. Here the whole (now-empty) ``toolsSettings`` is removed.
        """
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": ["stale"]}},
            "hooks": {"old": "hook"},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # Legacy deniedCommands injection is retired AND stripped on upgrade.
        assert "toolsSettings" not in config
        assert config["hooks"] == {"preToolUse": "audit"}

    def test_existing_config_refreshes_dynamic_mcp_servers(self, tmp_path: Path):
        """kirocrew-cron and kirocrew-core commands are always refreshed."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "kirocrew-cron": {"command": "/old/path/kirocrew", "args": ["mcp-cron"]},
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["mcpServers"]["kirocrew-cron"]["command"] == "/usr/bin/kirocrew"
        assert config["mcpServers"]["kirocrew-core"]["command"] == "/usr/bin/kirocrew"

    def test_existing_config_preserves_mcp_auto_approve(self, tmp_path: Path):
        """User autoApprove settings on MCP servers survive restart."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "kirocrew-cron": {
                    "command": "/old/path/kirocrew",
                    "args": ["mcp-cron"],
                    "autoApprove": ["cron_list", "cron_add"],
                },
                "kirocrew-core": {
                    "command": "/old/path/kirocrew",
                    "args": ["mcp-core"],
                    "autoApprove": ["learn_list"],
                },
                "builder-mcp": {
                    "command": "builder-mcp",
                    "autoApprove": ["ReadInternalWebsites"],
                },
            },
            "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            "hooks": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # kirocrew-cron/core: command refreshed, autoApprove preserved
        assert config["mcpServers"]["kirocrew-cron"]["command"] == "/usr/bin/kirocrew"
        assert config["mcpServers"]["kirocrew-cron"]["autoApprove"] == ["cron_list", "cron_add"]
        assert config["mcpServers"]["kirocrew-core"]["autoApprove"] == ["learn_list"]
        # other MCP servers: untouched
        assert config["mcpServers"]["builder-mcp"]["autoApprove"] == ["ReadInternalWebsites"]
        # hooks are always refreshed from bundled defaults; the retired
        # deniedCommands injection is stripped on refresh, so the emptied
        # toolsSettings scaffolding is removed entirely.
        assert "toolsSettings" not in config
        assert config["hooks"] == {"preToolUse": "audit"}

    def test_kirocrew_mcp_json_overrides_kiro_mcp(self, tmp_path: Path):
        """~/.kirocrew/mcp.json overrides ~/.kiro/settings/mcp.json for kirocrew agent."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Pre-existing agent config with builder-mcp from kiro settings
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "builder-mcp": {
                    "command": "builder-mcp",
                    "args": ["--include-tools", "ReadInternalWebsites"],
                    "autoApprove": ["ReadInternalWebsites"],
                },
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))
        # kirocrew mcp.json overrides args (removes --include-tools)
        mc_home = tmp_path / "kirocrew_home"
        mc_home.mkdir(exist_ok=True)
        (mc_home / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "builder-mcp": {"command": "builder-mcp", "args": []},
                        "new-server": {"command": "new-cmd", "args": ["start"]},
                    }
                }
            )
        )
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # builder-mcp args overridden by kirocrew mcp.json (no tool-tag injection
        # on a public install — that Amazon-specific wiring was removed)
        assert config["mcpServers"]["builder-mcp"]["args"] == []
        # autoApprove preserved (not in kirocrew mcp.json)
        assert config["mcpServers"]["builder-mcp"]["autoApprove"] == ["ReadInternalWebsites"]
        # new server added from kirocrew mcp.json
        assert config["mcpServers"]["new-server"]["command"] == "new-cmd"

    def test_new_managed_server_seeds_auto_approve(self, tmp_path: Path):
        """A new managed MCP server with autoApprove gets it seeded on first install."""
        cfg_dir = _bundled_defaults(tmp_path)
        mcps = {
            **_DEFAULT_MANAGED_MCPS,
            "playwright-mcp": {
                "command": "/usr/bin/playwright-mcp",
                "args": ["mcp", "start"],
                "autoApprove": ["browser_navigate"],
            },
        }
        path = _run_install(tmp_path, cfg_dir, managed_mcps=mcps)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["mcpServers"]["playwright-mcp"]["autoApprove"] == ["browser_navigate"]

    def test_new_managed_server_seeds_auto_approve_on_refresh(self, tmp_path: Path):
        """When a managed server is new to an existing config, autoApprove is seeded."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Existing config has kirocrew-cron/core but NOT playwright-mcp
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "kirocrew-cron": {"command": "/old/kirocrew", "args": ["mcp-cron"]},
                "kirocrew-core": {"command": "/old/kirocrew", "args": ["mcp-core"]},
            },
            "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            "hooks": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        mcps = {
            **_DEFAULT_MANAGED_MCPS,
            "playwright-mcp": {
                "command": "/usr/bin/playwright-mcp",
                "args": ["mcp", "start"],
                "autoApprove": ["browser_navigate"],
            },
        }
        path = _run_install(tmp_path, cfg_dir, managed_mcps=mcps)
        config = json.loads(path.read_text(encoding="utf-8"))
        # playwright-mcp is genuinely new → autoApprove should be seeded
        assert config["mcpServers"]["playwright-mcp"]["autoApprove"] == ["browser_navigate"]

    def test_user_removed_auto_approve_not_re_added(self, tmp_path: Path):
        """If user deliberately removed autoApprove, refresh must not re-add it."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        # Existing config has playwright-mcp but user removed autoApprove
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {
                "playwright-mcp": {
                    "command": "/old/playwright-mcp",
                    "args": ["mcp", "start"],
                },
            },
            "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            "hooks": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        mcps = {
            **_DEFAULT_MANAGED_MCPS,
            "playwright-mcp": {
                "command": "/usr/bin/playwright-mcp",
                "args": ["mcp", "start"],
                "autoApprove": ["browser_navigate"],
            },
        }
        path = _run_install(tmp_path, cfg_dir, managed_mcps=mcps)
        config = json.loads(path.read_text(encoding="utf-8"))
        # command/args refreshed, but autoApprove NOT re-added
        assert config["mcpServers"]["playwright-mcp"]["command"] == "/usr/bin/playwright-mcp"
        assert "autoApprove" not in config["mcpServers"]["playwright-mcp"]

    def test_clean_flag_ignores_existing(self, tmp_path: Path):
        """clean=True → regenerates from defaults even if file exists."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["UserTool"],
            "allowedTools": [],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir, clean=True)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"
        assert "UserTool" not in config["tools"]

    def test_corrupt_existing_falls_back_to_defaults(self, tmp_path: Path):
        """Corrupt kirocrew.json → falls back to build_agent_config()."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text("not valid json{{{")

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"

    def test_non_dict_json_falls_back_to_defaults(self, tmp_path: Path):
        """Valid JSON that is not a dict → falls back to build_agent_config()."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text("[]")

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["model"] == "claude-default"

    def test_missing_bundled_defaults_raises_when_existing_config_present(self, tmp_path: Path):
        """Error propagates when bundled defaults are absent during refresh."""
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        # No defaults.json written — bundled config is absent
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir()
        (kiro_dir / "kirocrew.json").write_text(json.dumps({"model": "x", "mcpServers": {}}))

        with pytest.raises(RuntimeError, match="Cannot build agent config"):
            _run_install(tmp_path, cfg_dir)


class TestAtomicJsonWrite:
    """Test 1.3: _atomic_json_write preserves permissions and handles new files."""

    def test_preserves_existing_permissions(self, tmp_path: Path):
        from kiro_crew.agent import _atomic_json_write

        target = tmp_path / "test.json"
        target.write_text("{}")
        target.chmod(0o664)

        _atomic_json_write(target, {"key": "value"})

        import stat

        assert stat.S_IMODE(target.stat().st_mode) == 0o664
        assert json.loads(target.read_text(encoding="utf-8")) == {"key": "value"}

    def test_new_file_gets_0o644(self, tmp_path: Path):
        from kiro_crew.agent import _atomic_json_write

        target = tmp_path / "new.json"
        _atomic_json_write(target, {"new": True})

        import stat

        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}

    def test_no_temp_file_left_on_success(self, tmp_path: Path):
        from kiro_crew.agent import _atomic_json_write

        target = tmp_path / "clean.json"
        _atomic_json_write(target, {"a": 1})

        tmp_files = [f for f in tmp_path.iterdir() if f.suffix == ".tmp"]
        assert tmp_files == []


class TestAllSkillPathsLocalSymlinks:
    """Test symlink resolution in _all_skill_paths for ~/.aim/skills/local/."""

    def test_resolves_local_symlink_with_skills_parent(self, tmp_path: Path):
        """Symlink target whose parent is named 'skills' is added."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        target_parent = tmp_path / "project" / "skills"
        target_skill = target_parent / "my-skill"
        target_skill.mkdir(parents=True)
        (target_skill / "SKILL.md").write_text("---\nname: my-skill\n---\n")

        (local_dir / "my-skill").symlink_to(target_skill)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(target_parent) in paths

    def test_skips_symlink_with_non_skills_parent(self, tmp_path: Path):
        """Symlink target whose parent is NOT named 'skills' is excluded."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        target_skill = tmp_path / "project" / "other" / "my-skill"
        target_skill.mkdir(parents=True)
        (local_dir / "my-skill").symlink_to(target_skill)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(tmp_path / "project" / "other") not in paths

    def test_skips_sensitive_parent_path(self, tmp_path: Path):
        """Symlink resolving into a sensitive directory is excluded."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        sensitive_skills = tmp_path / ".ssh" / "skills"
        target_skill = sensitive_skills / "bad-skill"
        target_skill.mkdir(parents=True)
        (local_dir / "bad-skill").symlink_to(target_skill)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(sensitive_skills) not in paths

    def test_skips_broken_symlink(self, tmp_path: Path):
        """Broken symlink raises OSError with strict=True and is logged."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        (local_dir / "broken").symlink_to(tmp_path / "nonexistent" / "skills" / "gone")

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                with patch("kiro_crew.agent.logger") as mock_logger:
                    paths = _all_skill_paths()

        assert str(tmp_path / "nonexistent" / "skills") not in paths
        # strict=True means resolve() raises OSError for broken symlinks,
        # which should be caught and logged (not silently swallowed)
        mock_logger.debug.assert_any_call(
            "Skipping unresolvable symlink %s: %s",
            local_dir / "broken",
            unittest.mock.ANY,
        )

    def test_ignores_regular_dir_in_local(self, tmp_path: Path):
        """Regular directories in local/ are not resolved as symlinks."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        regular = local_dir / "not-a-symlink" / "skills" / "some-skill"
        regular.mkdir(parents=True)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(local_dir / "not-a-symlink" / "skills") not in paths

    def test_ignores_symlink_to_file(self, tmp_path: Path):
        """Symlink pointing to a file (not directory) is skipped."""
        from kiro_crew.agent import _all_skill_paths

        aim_skills = tmp_path / ".aim" / "skills"
        local_dir = aim_skills / "local"
        local_dir.mkdir(parents=True)

        target_file = tmp_path / "project" / "skills" / "just-a-file.txt"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("not a skill dir")
        (local_dir / "file-link").symlink_to(target_file)

        with patch("kiro_crew.agent.Path.home", return_value=tmp_path):
            with patch("kiro_crew.agent._project_dir", return_value=None):
                paths = _all_skill_paths()

        assert str(tmp_path / "project" / "skills") not in paths


class TestResolveKirocrewBin:
    """Tests for lazy kirocrew binary resolution."""

    def test_finds_bin_in_parent_hierarchy(self, tmp_path: Path):
        """Walks up from package dir to find bin/kirocrew."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Create structure: venv/lib/python3.x/site-packages/kiro_crew
        #                   venv/bin/kirocrew
        venv = tmp_path / "venv"
        pkg_dir = venv / "lib" / "python3.11" / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        bin_dir = venv / "bin"
        bin_dir.mkdir()
        kirocrew_bin = bin_dir / "kirocrew"
        kirocrew_bin.write_text("#!/bin/bash\necho kirocrew")
        kirocrew_bin.chmod(0o755)

        # Mock kiro_crew.__file__ to point to our fake package
        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        # Reset global and mock the import
        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            assert result == str(kirocrew_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_falls_back_to_shutil_which(self, tmp_path: Path):
        """Falls back to PATH lookup when bin/ not found in hierarchy."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Package dir with no bin/ sibling anywhere (use /tmp which has no bin/)
        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir()
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        # Create the fallback binary so _usable() validation passes
        fallback_bin = tmp_path / "usr_local_bin_kirocrew"
        fallback_bin.write_text("#!/bin/sh\n")
        fallback_bin.chmod(0o755)

        # Selective isfile mock: only the explicit fallback passes validation;
        # any real /bin/kirocrew the walk might find gets rejected.
        _real_isfile = os.path.isfile

        def _fake_isfile(p):
            return p == str(fallback_bin) and _real_isfile(p)

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                with patch("os.path.isfile", side_effect=_fake_isfile):
                    # Skip brazil-path branch
                    def _which(cmd: str) -> str | None:
                        if cmd == "brazil-path":
                            return None
                        if cmd == "kirocrew":
                            return str(fallback_bin)
                        return None

                    with patch("shutil.which", side_effect=_which):
                        result = _resolve_kirocrew_bin()
            assert result == str(fallback_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_returns_kirocrew_when_not_found(self, tmp_path: Path):
        """Returns 'kirocrew' string when not found anywhere."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir(exist_ok=True)
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                # Blanket isfile=False blocks walk, brazil-path, and which fallback
                with patch("os.path.isfile", return_value=False):
                    with patch("shutil.which", return_value=None):
                        result = _resolve_kirocrew_bin()
            assert result == "kirocrew"
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_skips_stale_shutil_which_result(self, tmp_path: Path):
        """Falls through to bare 'kirocrew' when shutil.which returns a
        path that no longer exists (e.g. deleted after Toolbox migration).
        Regression test for scenario where
        ~/.local/bin/kirocrew was removed but still cached in PATH lookup.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir(exist_ok=True)
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        # Stub brazil-path so its subprocess call doesn't resolve to a real binary
        def _which(cmd: str) -> str | None:
            if cmd == "brazil-path":
                return None  # skip brazil-path branch
            if cmd == "kirocrew":
                return "/home/user/.local/bin/kirocrew-DELETED"
            return None

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                # Blanket isfile=False — the stale path must not pass validation
                with patch("os.path.isfile", return_value=False):
                    with patch("shutil.which", side_effect=_which):
                        result = _resolve_kirocrew_bin()
            # Must NOT cache the stale path — falls through to bare 'kirocrew'
            assert result == "kirocrew"
            assert agent_mod._KIROCREW_BIN is None  # didn't cache fallback
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_walk_and_path_miss_falls_back_to_bare(self, tmp_path: Path):
        """When the bin walk and PATH both miss, fall back to bare 'kirocrew'.

        The public install has no Brazil ``brazil-path run.runtimefarm`` step,
        so an unresolvable binary surfaces as the bare name instead of caching
        a stale absolute path.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # A real executable that lives somewhere NOT on the walk path or PATH.
        runtime_farm = tmp_path / "runtime"
        (runtime_farm / "bin").mkdir(parents=True)
        kirocrew_bin = runtime_farm / "bin" / "kirocrew"
        kirocrew_bin.write_text("#!/bin/sh\n")
        kirocrew_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir(exist_ok=True)
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                # Make the walk find nothing reachable from the package dir.
                _real_isfile = os.path.isfile

                def _fake_isfile(p):
                    return p == str(kirocrew_bin) and _real_isfile(p)

                with patch("os.path.isfile", side_effect=_fake_isfile):
                    with patch("shutil.which", return_value=None):
                        result = _resolve_kirocrew_bin()
            # No brazil-path fallback exists; unresolved -> bare name, not cached.
            assert result == "kirocrew"
            assert agent_mod._KIROCREW_BIN is None
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_brazil_path_failure_falls_through(self, tmp_path: Path):
        """brazil-path raising an exception falls through without crashing."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(tmp_path / "kiro_crew" / "__init__.py")
        (tmp_path / "kiro_crew").mkdir(exist_ok=True)
        (tmp_path / "kiro_crew" / "__init__.py").write_text("")

        def _which(cmd: str) -> str | None:
            if cmd == "brazil-path":
                return "/usr/bin/brazil-path"  # exists but subprocess will raise
            return None

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                with patch("os.path.isfile", return_value=False):
                    with patch("shutil.which", side_effect=_which):
                        with patch(
                            "subprocess.run",
                            side_effect=OSError("brazil-path blew up"),
                        ):
                            result = _resolve_kirocrew_bin()
            # Falls through to bare 'kirocrew', doesn't crash
            assert result == "kirocrew"
            assert agent_mod._KIROCREW_BIN is None
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_caches_result(self):
        """Result is cached in global _KIROCREW_BIN."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = "/cached/kirocrew"
            result = _resolve_kirocrew_bin()
            assert result == "/cached/kirocrew"
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_accepts_any_readable_bin_from_walk(self, tmp_path: Path):
        """The bin walk accepts any readable executable it finds.

        On a public install there is no Apollo/Brazil wrapper-rejection: any
        readable ``bin/kirocrew`` discovered by the walk is used as-is.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        venv = tmp_path / "venv"
        pkg_dir = venv / "lib" / "python3.10" / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        bin_dir = venv / "bin"
        bin_dir.mkdir()
        kirocrew_bin = bin_dir / "kirocrew"
        kirocrew_bin.write_text('#!/bin/sh\nexec kirocrew "$@"\n')
        kirocrew_bin.chmod(0o755)

        # A PATH fallback that should NOT be chosen — the walk finds bin first.
        fallback_bin = tmp_path / "toolbox" / "bin" / "kirocrew"
        fallback_bin.parent.mkdir(parents=True)
        fallback_bin.write_text("#!/bin/bash\n")
        fallback_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                with patch("shutil.which") as mock_which:
                    mock_which.side_effect = lambda cmd, **kw: (
                        str(fallback_bin) if cmd == "kirocrew" else None
                    )
                    result = _resolve_kirocrew_bin()
            # The walk finds venv/bin/kirocrew and accepts it directly.
            assert result == str(kirocrew_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_accepts_apollo_binary_with_envroot(self, tmp_path: Path):
        """Accepts Apollo binary when .envroot exists in a parent directory."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Create env/runtime/.envroot + env/runtime/bin/kirocrew (Apollo)
        runtime = tmp_path / "env" / "runtime"
        (runtime / "lib" / "python3.10" / "site-packages" / "kiro_crew").mkdir(parents=True)
        (runtime / "lib" / "python3.10" / "site-packages" / "kiro_crew" / "__init__.py").write_text(
            ""
        )
        (runtime / ".envroot").write_text("")

        bin_dir = runtime / "bin"
        bin_dir.mkdir()
        kirocrew_bin = bin_dir / "kirocrew"
        kirocrew_bin.write_bytes(
            b"#!/apollo/sbin/envroot $ENVROOT/python3.10/bin/python3.10\nimport sys\n"
        )
        kirocrew_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(
            runtime / "lib" / "python3.10" / "site-packages" / "kiro_crew" / "__init__.py"
        )

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            # Should accept — .envroot exists in parent of bin/
            assert result == str(kirocrew_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_accepts_wrapper_bin_from_walk(self, tmp_path: Path):
        """A shell-wrapper bin/kirocrew found by the walk is accepted as-is.

        Public installs no longer reject wrapper scripts (the Brazil-workspace
        check is a no-op), so the walk-discovered bin wins over PATH.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        project = tmp_path / "project"
        pkg_dir = project / "src" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        bin_dir = project / "bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "kirocrew"
        wrapper.write_text('#!/bin/sh\nexec kirocrew "$@"\n')
        wrapper.chmod(0o755)

        # A PATH fallback that should NOT be chosen.
        fallback_bin = tmp_path / "local" / "bin" / "kirocrew"
        fallback_bin.parent.mkdir(parents=True)
        fallback_bin.write_text("#!/bin/sh\n")
        fallback_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                with patch("shutil.which") as mock_which:
                    mock_which.side_effect = lambda cmd, **kw: (
                        str(fallback_bin) if cmd == "kirocrew" else None
                    )
                    result = _resolve_kirocrew_bin()
            # The walk finds project/bin/kirocrew and accepts it directly.
            assert result == str(wrapper)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_accepts_brazil_wrapper_inside_workspace(self, tmp_path: Path):
        """Accepts bin/kirocrew with brazil-runtime-exec when packageInfo exists."""
        from kiro_crew.agent import _bin_is_usable

        # Create structure with packageInfo (real Brazil workspace)
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "packageInfo").write_text("{}")
        bin_dir = workspace / "bin"
        bin_dir.mkdir()
        brazil_wrapper = bin_dir / "kirocrew"
        brazil_wrapper.write_text('#!/bin/sh\nexec brazil-runtime-exec kirocrew "$@"\n')
        brazil_wrapper.chmod(0o755)

        assert _bin_is_usable(brazil_wrapper) is True

    def test_prefers_venv_bin_over_project_bin(self, tmp_path: Path):
        """Resolves .venv/bin/kirocrew before bin/kirocrew in the same tree."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Create structure: project/src/kiro_crew + project/.venv/bin/kirocrew
        # + project/bin/kirocrew (Brazil wrapper)
        project = tmp_path / "project"
        pkg_dir = project / "src" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")

        # .venv/bin/kirocrew — the preferred candidate
        venv_bin = project / ".venv" / "bin" / "kirocrew"
        venv_bin.parent.mkdir(parents=True)
        venv_bin.write_text('#!/bin/sh\nexec python -m kiro_crew "$@"\n')
        venv_bin.chmod(0o755)

        # bin/kirocrew — Brazil wrapper (should be skipped)
        bin_dir = project / "bin"
        bin_dir.mkdir()
        brazil_wrapper = bin_dir / "kirocrew"
        brazil_wrapper.write_text('#!/bin/sh\nexec brazil-runtime-exec kirocrew "$@"\n')
        brazil_wrapper.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            # Should prefer .venv/bin/kirocrew over bin/kirocrew
            assert result == str(venv_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val

    def test_venv_install_falls_through_to_step1(self, tmp_path: Path):
        """When pkg_dir is inside .venv/, pyvenv.cfg breaks step 0."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.agent import _resolve_kirocrew_bin

        # Simulate pip-into-venv: .venv/lib/python3.x/site-packages/kiro_crew/
        venv_root = tmp_path / ".venv"
        pkg_dir = venv_root / "lib" / "python3.13" / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("")
        (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n")

        # .venv/bin/kirocrew exists (step 1 should find it)
        venv_bin = venv_root / "bin" / "kirocrew"
        venv_bin.parent.mkdir(parents=True)
        venv_bin.write_text('#!/bin/sh\nexec python -m kiro_crew "$@"\n')
        venv_bin.chmod(0o755)

        mock_mc = unittest.mock.MagicMock()
        mock_mc.__file__ = str(pkg_dir / "__init__.py")

        old_val = agent_mod._KIROCREW_BIN
        try:
            agent_mod._KIROCREW_BIN = None
            with patch.dict("sys.modules", {"kiro_crew": mock_mc}):
                result = _resolve_kirocrew_bin()
            # Step 0 breaks at pyvenv.cfg; step 1 finds bin/kirocrew
            assert result == str(venv_bin)
        finally:
            agent_mod._KIROCREW_BIN = old_val


class TestKirocrewMcpInvocation:
    """Tests for built-in MCP server invocation resolution.

    Regression: when ``_resolve_kirocrew_bin`` cannot find a usable
    standalone binary (e.g. the gateway runs as a systemd user service and
    ``kirocrew`` is not on the service PATH), the built-in cron/core servers
    must still get a runnable command instead of the bare ``"kirocrew"``
    sentinel, which fails validation and drops them from ``kirocrew.json`` on
    every refresh.
    """

    def test_uses_standalone_binary_when_resolved(self):
        from kiro_crew.agent import _kirocrew_mcp_invocation

        with patch("kiro_crew.agent._resolve_kirocrew_bin", return_value="/opt/bin/kirocrew"):
            cmd, args = _kirocrew_mcp_invocation("mcp-cron")
        assert cmd == "/opt/bin/kirocrew"
        assert args == ["mcp-cron"]

    def test_falls_back_to_interpreter_module_when_unresolved(self):
        from kiro_crew.agent import _kirocrew_mcp_invocation

        # Bare "kirocrew" is the unresolved sentinel from _resolve_kirocrew_bin.
        with patch("kiro_crew.agent._resolve_kirocrew_bin", return_value="kirocrew"):
            cmd, args = _kirocrew_mcp_invocation("mcp-core")
        assert cmd == sys.executable
        assert args == ["-m", "kiro_crew", "mcp-core"]


class TestKiroHooksMerge:
    """Tests for agent.kiro_hooks merge into kiro-cli agent config."""

    def _bundled_with_hooks(self, tmp_path: Path) -> Path:
        """Write bundled defaults with realistic list-based hooks."""
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir(exist_ok=True)
        defaults = {
            "model": "claude-default",
            "tools": ["ReadFile"],
            "allowedTools": ["ReadFile"],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /"]}},
            "hooks": {
                "postToolUse": [
                    {"matcher": "execute_bash", "command": "audit.sh"},
                ],
            },
        }
        (cfg_dir / "defaults.json").write_text(json.dumps(defaults))
        (cfg_dir / "prompt.md").write_text("system prompt")
        return cfg_dir

    def _make_hook(self, tmp_path: Path, name: str = "hook.sh") -> str:
        """Create a real executable hook script and return its absolute path."""
        hook = tmp_path / "hooks" / name
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        return str(hook)

    def _run_with_kiro_hooks(
        self,
        tmp_path: Path,
        kiro_hooks: dict,
        existing: dict | None = None,
    ) -> dict:
        """Install agent with kiro_hooks in config.json and return the result."""
        cfg_dir = self._bundled_with_hooks(tmp_path)
        mc_config = tmp_path / "mc_config.json"
        # Disable autoimport in this helper: these tests target the explicit
        # kiro_hooks merge path only. The autoimport path is covered by
        # TestKiroHooksAutoimport below.
        mc_config.write_text(
            json.dumps(
                {
                    "agent": {
                        "kiro_hooks": kiro_hooks,
                        "kiro_hooks_autoimport": False,
                    }
                }
            )
        )

        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        if existing:
            (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        prompt = cfg_dir / "prompt.md"
        patches = [
            patch.multiple(
                "kiro_crew.agent",
                KIRO_AGENTS_DIR=kiro_dir,
                _BUNDLED_CFG_DIR=cfg_dir,
                _KIROCREW_BIN="/usr/bin/kirocrew",
                _MANAGED_MCP_SERVERS=_DEFAULT_MANAGED_MCPS,
                _KIRO_MCP_JSON=tmp_path / "nonexistent_kiro_mcp.json",
                _CC_MCP_JSON=tmp_path / "nonexistent_cc_mcp.json",
            ),
            patch("kiro_crew.agent._prompt_path", return_value=prompt),
            patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json"),
            patch("kiro_crew.agent._project_dir", return_value=None),
            patch("kiro_crew.agent._aim_skill_paths", return_value=[]),
            patch("kiro_crew.agent.shutil.which", side_effect=lambda c, **kw: c),
            patch("kiro_crew.agent._mc_config_path", return_value=mc_config),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            path = install_agent(clean=existing is None)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_user_hooks_appended_to_bundled(self, tmp_path: Path):
        """User kiro_hooks are appended after bundled hooks per event."""
        hook = self._make_hook(tmp_path, "guardian.sh")
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "postToolUse": [{"matcher": "*", "command": hook}],
            },
        )
        post = config["hooks"]["postToolUse"]
        assert post[0] == {"matcher": "execute_bash", "command": "audit.sh"}  # bundled first
        assert post[1] == {"matcher": "*", "command": hook}  # user appended

    def test_user_hooks_new_event_type(self, tmp_path: Path):
        """User kiro_hooks can add hooks for event types not in bundled."""
        hook = self._make_hook(tmp_path, "guardian.sh")
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "preToolUse": [{"matcher": "*", "command": hook}],
            },
        )
        assert config["hooks"]["postToolUse"] == [
            {"matcher": "execute_bash", "command": "audit.sh"},
        ]
        assert config["hooks"]["preToolUse"] == [
            {"matcher": "*", "command": hook},
        ]

    def test_user_hooks_dedup_by_command(self, tmp_path: Path):
        """Duplicate commands are not added twice."""
        hook = self._make_hook(tmp_path)
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "preToolUse": [
                    {"command": hook},
                    {"command": hook},
                ],
            },
        )
        assert len(config["hooks"]["preToolUse"]) == 1

    def test_user_hooks_dedup_against_bundled(self, tmp_path: Path):
        """User hook whose command+matcher matches a bundled hook is not added twice."""
        from kiro_crew.agent import _merge_kiro_hooks

        hook = self._make_hook(tmp_path, "audit.sh")
        bundled = {"postToolUse": [{"matcher": "execute_bash", "command": hook}]}
        user = {"postToolUse": [{"matcher": "execute_bash", "command": hook}]}
        result = _merge_kiro_hooks(bundled, user)
        assert len(result["postToolUse"]) == 1
        assert result["postToolUse"][0] == {"matcher": "execute_bash", "command": hook}

    def test_user_hooks_same_command_different_matcher(self, tmp_path: Path):
        """Same command with different matchers are kept as separate entries."""
        hook = self._make_hook(tmp_path)
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "preToolUse": [
                    {"matcher": "execute_bash", "command": hook},
                    {"matcher": "ReadFile", "command": hook},
                ],
            },
        )
        assert len(config["hooks"]["preToolUse"]) == 2

    def test_user_hooks_malformed_skipped(self, tmp_path: Path):
        """Entries without command field are skipped."""
        hook = self._make_hook(tmp_path, "valid.sh")
        config = self._run_with_kiro_hooks(
            tmp_path,
            {
                "preToolUse": [
                    {"matcher": "*"},  # no command
                    {"command": hook},
                ],
            },
        )
        assert config["hooks"]["preToolUse"] == [{"command": hook}]

    def test_existing_config_merges_kiro_hooks(self, tmp_path: Path):
        """kiro_hooks are merged on refresh of existing config."""
        existing = {
            "model": "claude-user-custom",
            "tools": [],
            "allowedTools": [],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": []}},
            "hooks": {"old": "hook"},
        }
        config = self._run_with_kiro_hooks(
            tmp_path,
            {"preToolUse": [{"matcher": "*", "command": self._make_hook(tmp_path, "guardian.sh")}]},
            existing=existing,
        )
        # Bundled hooks overwrite old hooks
        assert config["hooks"]["postToolUse"] == [
            {"matcher": "execute_bash", "command": "audit.sh"},
        ]
        # User hooks appended
        assert len(config["hooks"]["preToolUse"]) == 1
        assert config["hooks"]["preToolUse"][0]["matcher"] == "*"

    # -- Direct unit tests for _merge_kiro_hooks defensive branches --
    def test_merge_kiro_hooks_non_dict_user_hooks_returns_original(self):
        from kiro_crew.agent import _merge_kiro_hooks

        bundled = {"postToolUse": [{"command": "audit.sh"}]}
        assert _merge_kiro_hooks(bundled, ["bad"]) == bundled

    def test_merge_kiro_hooks_non_list_event_entries_skipped(self):
        from kiro_crew.agent import _merge_kiro_hooks

        bundled = {"postToolUse": [{"command": "audit.sh"}]}
        result = _merge_kiro_hooks(bundled, {"postToolUse": "not-a-list"})
        assert result == bundled

    def test_merge_kiro_hooks_non_dict_entry_in_list_skipped(self):
        from kiro_crew.agent import _merge_kiro_hooks

        result = _merge_kiro_hooks({}, {"preToolUse": ["just-a-string"]})
        assert result["preToolUse"] == []

    # -- Direct unit tests for _validate_hook_command --

    def test_validate_rejects_relative_path(self, tmp_path: Path):
        from kiro_crew.agent import _validate_hook_command

        assert _validate_hook_command("relative/hook.sh", "test") is None

    def test_validate_rejects_shell_metacharacters(self, tmp_path: Path):
        from kiro_crew.agent import _validate_hook_command

        hook = self._make_hook(tmp_path)
        assert _validate_hook_command(hook + "; rm -rf /", "test") is None
        assert _validate_hook_command(hook + " | cat", "test") is None
        assert _validate_hook_command(hook + " $(evil)", "test") is None

    def test_validate_rejects_nonexistent_file(self):
        from kiro_crew.agent import _validate_hook_command

        assert _validate_hook_command("/nonexistent/hook.sh", "test") is None

    def test_validate_accepts_valid_hook(self, tmp_path: Path):
        from kiro_crew.agent import _validate_hook_command

        hook = self._make_hook(tmp_path)
        assert _validate_hook_command(hook, "test") is not None

    def test_merge_strips_extra_fields(self, tmp_path: Path):
        """Only command and matcher fields are kept; arbitrary keys are stripped."""
        from kiro_crew.agent import _merge_kiro_hooks

        hook = self._make_hook(tmp_path)
        user = {"preToolUse": [{"command": hook, "matcher": "*", "shell": True, "env": {"X": "1"}}]}
        result = _merge_kiro_hooks({}, user)
        assert result["preToolUse"] == [{"command": hook, "matcher": "*"}]

    def test_validate_rejects_symlink_to_sensitive(self, tmp_path: Path):
        """Symlinks resolving to sensitive paths are rejected."""
        from kiro_crew.agent import _validate_hook_command

        sensitive = tmp_path / ".ssh" / "key"
        sensitive.parent.mkdir(parents=True)
        sensitive.write_text("#!/bin/sh\n")
        sensitive.chmod(0o755)
        link = tmp_path / "hooks" / "sneaky.sh"
        link.parent.mkdir(parents=True)
        link.symlink_to(sensitive)
        with patch("kiro_crew.agent.is_sensitive_path", side_effect=lambda p: ".ssh" in p):
            assert _validate_hook_command(str(link), "test") is None

    def test_merge_rejects_non_string_matcher(self, tmp_path: Path):
        """Non-string matcher values are skipped (prevents TypeError and injection)."""
        from kiro_crew.agent import _merge_kiro_hooks

        hook = self._make_hook(tmp_path)
        user = {
            "preToolUse": [
                {"command": hook, "matcher": {"$regex": ".*"}},
                {"command": hook, "matcher": ["list"]},
                {"command": hook, "matcher": "*"},
            ]
        }
        result = _merge_kiro_hooks({}, user)
        assert len(result["preToolUse"]) == 1
        assert result["preToolUse"][0] == {"command": hook, "matcher": "*"}

    def test_merge_kiro_hooks_max_per_event_limit(self, tmp_path: Path):
        """At most _MAX_USER_HOOKS_PER_EVENT hooks are accepted per event."""
        from kiro_crew.agent import _MAX_USER_HOOKS_PER_EVENT, _merge_kiro_hooks

        hooks = [
            {"command": self._make_hook(tmp_path, f"hook_{i}.sh")}
            for i in range(_MAX_USER_HOOKS_PER_EVENT + 5)
        ]
        result = _merge_kiro_hooks({}, {"preToolUse": hooks})
        assert len(result["preToolUse"]) == _MAX_USER_HOOKS_PER_EVENT

    def test_merge_kiro_hooks_unknown_event_rejected(self):
        """Unknown event types are silently dropped."""
        from kiro_crew.agent import _merge_kiro_hooks

        result = _merge_kiro_hooks({}, {"onBadEvent": [{"command": "/bin/true"}]})
        assert "onBadEvent" not in result

    def test_merge_rejects_matcher_with_shell_metacharacters(self, tmp_path: Path):
        """Matchers with shell metacharacters are rejected."""
        from kiro_crew.agent import _merge_kiro_hooks

        hook = self._make_hook(tmp_path)
        user = {
            "preToolUse": [
                {"command": hook, "matcher": "tool; rm -rf /"},
                {"command": hook, "matcher": "tool | cat"},
                {"command": hook, "matcher": "$(evil)"},
                {"command": hook, "matcher": "tool name with spaces"},
                {"command": hook, "matcher": "*"},  # valid
            ]
        }
        result = _merge_kiro_hooks({}, user)
        assert len(result["preToolUse"]) == 1
        assert result["preToolUse"][0]["matcher"] == "*"

    def test_merge_rejects_oversized_matcher(self, tmp_path: Path):
        """Matchers exceeding max length are rejected."""
        from kiro_crew.agent import _MAX_MATCHER_LEN, _merge_kiro_hooks

        hook = self._make_hook(tmp_path)
        user = {
            "preToolUse": [
                {"command": hook, "matcher": "a" * (_MAX_MATCHER_LEN + 1)},
                {"command": hook, "matcher": "valid"},
            ]
        }
        result = _merge_kiro_hooks({}, user)
        assert len(result["preToolUse"]) == 1
        assert result["preToolUse"][0]["matcher"] == "valid"

    def test_merge_global_hooks_limit(self, tmp_path: Path):
        """Total hooks across all events are capped at _MAX_TOTAL_USER_HOOKS."""
        from kiro_crew.agent import _MAX_TOTAL_USER_HOOKS, _merge_kiro_hooks

        user = {}
        for event in ("preToolUse", "postToolUse", "userPromptSubmit"):
            user[event] = [
                {"command": self._make_hook(tmp_path, f"{event}_{i}.sh")}
                for i in range(_MAX_TOTAL_USER_HOOKS)
            ]
        result = _merge_kiro_hooks({}, user)
        total = sum(len(v) for v in result.values() if isinstance(v, list))
        assert total == _MAX_TOTAL_USER_HOOKS


class TestKiroHooksFiltering:
    """Tests that KiroCrew-internal hook keys are stripped from kiro-cli agent config."""

    def test_auto_approve_tools_stripped_from_agent_config(self, tmp_path: Path):
        """auto_approve_tools must not appear in kirocrew.json (kiro-cli rejects it)."""
        from kiro_crew.agent import _kiro_hooks_only

        hooks = {
            "auto_approve_tools": ["kirocrew browse *"],
            "postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}],
            "userPromptSubmit": [{"command": "metrics.sh"}],
        }
        result = _kiro_hooks_only(hooks)
        assert "auto_approve_tools" not in result
        assert "postToolUse" in result
        assert "userPromptSubmit" in result

    def test_auto_deny_tools_stripped_from_agent_config(self):
        """auto_deny_tools must not appear in kirocrew.json."""
        from kiro_crew.agent import _kiro_hooks_only

        hooks = {
            "auto_deny_tools": ["Dangerous*"],
            "preToolUse": [{"matcher": "*", "command": "/bin/true"}],
        }
        result = _kiro_hooks_only(hooks)
        assert "auto_deny_tools" not in result
        assert "preToolUse" in result

    def test_only_valid_kiro_events_preserved(self):
        """Only kiro-cli valid hook events survive filtering."""
        from kiro_crew.agent import _VALID_HOOK_EVENTS, _kiro_hooks_only

        hooks = {
            "auto_approve_tools": ["*"],
            "auto_deny_tools": ["*"],
            "auto_replies": [{"pattern": "x", "reply": "y"}],
            "transforms": [{"pattern": "x", "prefix": "y"}],
            "context_rules": [{"triggers": ["x"], "context": "y"}],
            "preToolUse": [{"command": "/bin/true"}],
            "postToolUse": [{"command": "/bin/true"}],
            "userPromptSubmit": [{"command": "/bin/true"}],
            "agentSpawn": [{"command": "/bin/true"}],
            "stop": [{"command": "/bin/true"}],
        }
        result = _kiro_hooks_only(hooks)
        assert set(result.keys()) == _VALID_HOOK_EVENTS

    def test_sanitize_agent_hooks_repairs_existing_file(self, tmp_path: Path):
        """_sanitize_agent_hooks removes invalid hook keys from existing configs."""
        from kiro_crew.agent import _hooks_sanitized_mtimes, _sanitize_agent_hooks

        kiro_dir = tmp_path / "agents"
        kiro_dir.mkdir()
        broken_config = {
            "name": "kirocrew",
            "hooks": {
                "auto_approve_tools": ["kirocrew browse *"],
                "postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}],
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(broken_config))

        _hooks_sanitized_mtimes.clear()
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir):
            _sanitize_agent_hooks()

        repaired = json.loads((kiro_dir / "kirocrew.json").read_text(encoding="utf-8"))
        assert "auto_approve_tools" not in repaired["hooks"]
        assert "postToolUse" in repaired["hooks"]

    def test_sanitize_agent_hooks_skips_clean_file(self, tmp_path: Path):
        """_sanitize_agent_hooks does not rewrite configs that are already clean."""
        from kiro_crew.agent import _hooks_sanitized_mtimes, _sanitize_agent_hooks

        kiro_dir = tmp_path / "agents"
        kiro_dir.mkdir()
        clean_config = {
            "name": "kirocrew",
            "hooks": {
                "postToolUse": [{"matcher": "execute_bash", "command": "audit.sh"}],
            },
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(clean_config))
        original_mtime = (kiro_dir / "kirocrew.json").stat().st_mtime

        _hooks_sanitized_mtimes.clear()
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro_dir):
            _sanitize_agent_hooks()

        assert (kiro_dir / "kirocrew.json").stat().st_mtime == original_mtime


class TestToolBloatFixes:
    """Tests for tool bloat prevention: rename migration, fresh_install gating, dedup."""

    def test_existing_config_tools_untouched(self, tmp_path: Path):
        """Existing tools/allowedTools are preserved exactly as-is (no renames, no additions)."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["execute_bash", "fs_read", "fs_write", "use_aws", "code"],
            "allowedTools": ["fs_read", "use_aws"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # Template (_bundled_defaults) does not grant tool_search, so the narrow
        # seed does not fire and the user's tools (MOUNT) list is preserved exactly.
        assert config["tools"] == ["execute_bash", "fs_read", "fs_write", "use_aws", "code"]
        # allowedTools (AUTO-APPROVE) is different: a floor builtin (fs_read —
        # sensitive-path) is withheld even from a user's explicit grant, because
        # auto-approve skips the always-on gate floor. It still auto-approves via
        # hooks after that floor runs. use_aws carries no such floor → preserved.
        assert config["allowedTools"] == ["use_aws"]

    def _tool_search_defaults(self, tmp_path: Path) -> Path:
        """Bundled defaults whose template grants the tool_search built-in."""
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        defaults = {
            "model": "claude-default",
            "tools": ["ReadFile", "tool_search"],
            "allowedTools": ["ReadFile"],
            "mcpServers": {},
            "toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf /"]}},
            "hooks": {"preToolUse": "audit"},
        }
        (cfg_dir / "defaults.json").write_text(json.dumps(defaults))
        (cfg_dir / "prompt.md").write_text("system prompt")
        return cfg_dir

    def test_existing_config_seeds_missing_tool_search(self, tmp_path: Path):
        """Existing config missing tool_search gains it (ADD-only) when the
        shipped template grants it, appended without disturbing other tools."""
        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["execute_bash", "fs_read", "code", "@builder-mcp"],
            "allowedTools": ["fs_read", "@builder-mcp"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # tool_search appended at the end; all prior tools preserved in order.
        assert config["tools"] == [
            "execute_bash",
            "fs_read",
            "code",
            "@builder-mcp",
            "tool_search",
        ]
        # Read-only auto-allowed built-in: NOT added to allowedTools.
        assert "tool_search" not in config["allowedTools"]
        # fs_read is a floor builtin (sensitive-path) → withheld from auto-approve
        # even though the user's config listed it; @builder-mcp (ungoverned MCP
        # ref, no ceiling) is preserved.
        assert config["allowedTools"] == ["@builder-mcp"]

    def test_existing_config_tool_search_idempotent(self, tmp_path: Path):
        """A config that already grants tool_search is left unchanged (no dup)."""
        cfg_dir = self._tool_search_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["execute_bash", "tool_search", "code"],
            "allowedTools": ["code"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["tools"] == ["execute_bash", "tool_search", "code"]
        assert config["tools"].count("tool_search") == 1

    def test_existing_config_no_tool_search_seed_when_template_omits(self, tmp_path: Path):
        """When the shipped template does NOT grant tool_search, an existing
        config's tools are left exactly as-is (seed is template-gated)."""
        cfg_dir = _bundled_defaults(tmp_path)  # tools == ["ReadFile"], no tool_search
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["execute_bash", "code"],
            "allowedTools": ["code"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["tools"] == ["execute_bash", "code"]
        assert "tool_search" not in config["tools"]

    def test_existing_config_no_managed_mcp_added(self, tmp_path: Path):
        """Existing configs don't get @managed-mcp refs injected."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["shell", "read"],
            "allowedTools": ["read"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["tools"] == ["shell", "read"]
        assert config["allowedTools"] == ["read"]

    def test_fresh_install_adds_managed_mcp_to_tools_only(self, tmp_path: Path):
        """Fresh install adds @managed-mcp to tools but NOT allowedTools."""
        cfg_dir = _bundled_defaults(tmp_path)
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert "@kirocrew-cron" in config["tools"]
        assert "@kirocrew-core" in config["tools"]
        assert "@kirocrew-cron" not in config["allowedTools"]
        assert "@kirocrew-core" not in config["allowedTools"]

    def test_malformed_allowedtools_entries_are_dropped(self, tmp_path: Path):
        """A non-string allowedTools entry (hand-edited config) must be dropped,
        not crash rebuild via may_skip_gate's ref.startswith()."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["use_aws"],
            "allowedTools": [1, "use_aws", None, "web_fetch"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)  # must not raise
        config = json.loads(path.read_text(encoding="utf-8"))
        # Non-string junk dropped; valid non-floor entries preserved.
        assert config["allowedTools"] == ["use_aws", "web_fetch"]

    def test_dedup_preserves_order(self, tmp_path: Path):
        """Duplicate tools are removed while preserving first-occurrence order."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        existing = {
            "model": "claude-user-custom",
            "tools": ["shell", "read", "shell", "use_aws", "read"],
            "allowedTools": ["read", "read", "use_aws"],
            "mcpServers": {},
        }
        (kiro_dir / "kirocrew.json").write_text(json.dumps(existing))

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert config["tools"] == ["shell", "read", "use_aws"]
        # Dedup preserves first-occurrence order. ("read"/"shell"/"use_aws" are
        # not floor builtins, so none is withheld from auto-approve here.)
        assert config["allowedTools"] == ["read", "use_aws"]

    def test_non_dict_json_treated_as_fresh_install(self, tmp_path: Path):
        """Valid JSON that is not a dict → treated as fresh install."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_dir = tmp_path / "kiro_agents"
        kiro_dir.mkdir(exist_ok=True)
        (kiro_dir / "kirocrew.json").write_text('"just a string"')

        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        # Should get defaults (fresh install path)
        assert config["model"] == "claude-default"
        assert "@kirocrew-cron" in config["tools"]


class TestKiroHooksAutoimport:
    """Tests for auto-discovery of executable hook scripts in ~/.kiro/hooks/."""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path: Path, monkeypatch):
        """Point Path.home() at tmp_path so hooks_dir validation accepts tmp dirs.

        The production rule rejects kiro_hooks_dir values that do not resolve
        under Path.home(), to prevent an LLM-writable config from pointing
        autoimport at /tmp or similar.  Tests legitimately use tmp_path for
        isolation, so we fake HOME = tmp_path for every test in this class.
        """
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    def _make_script(
        self,
        hooks_dir: Path,
        name: str,
        body: str = "exit 0\n",
        executable: bool = True,
    ) -> Path:
        """Write a hook script with the given body; mark it executable by default."""
        hooks_dir.mkdir(parents=True, exist_ok=True)
        p = hooks_dir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755 if executable else 0o644)
        return p

    def test_kiro_hooks_autoimport_loads_executable_scripts(self, tmp_path: Path):
        """Two executable scripts both land under preToolUse with their absolute paths."""
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        s1 = self._make_script(hooks_dir, "a.sh")
        s2 = self._make_script(hooks_dir, "b.sh")

        result = _autoimport_kiro_hooks(hooks_dir)

        commands = sorted(e["command"] for e in result["preToolUse"])
        assert commands == sorted([str(s1), str(s2)])
        assert list(result.keys()) == ["preToolUse"]

    def test_kiro_hooks_autoimport_parses_event_header(self, tmp_path: Path):
        """A ``# event: PostToolUse`` header routes the script to postToolUse."""
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        self._make_script(hooks_dir, "audit.sh", body="# event: PostToolUse\nexit 0\n")

        result = _autoimport_kiro_hooks(hooks_dir)

        assert "postToolUse" in result
        assert "preToolUse" not in result
        assert len(result["postToolUse"]) == 1

    def test_kiro_hooks_autoimport_parses_matcher_header(self, tmp_path: Path):
        """A ``# matcher:`` header is preserved on the resulting entry."""
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        self._make_script(hooks_dir, "guard.sh", body="# matcher: shell\nexit 0\n")

        result = _autoimport_kiro_hooks(hooks_dir)

        assert result["preToolUse"][0]["matcher"] == "shell"

    def test_kiro_hooks_autoimport_skips_non_executable(self, tmp_path: Path, caplog):
        """Non-executable ``.sh`` files are skipped; executable siblings still load."""
        import logging

        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        self._make_script(hooks_dir, "ok.sh")
        self._make_script(hooks_dir, "disabled.sh", executable=False)

        with caplog.at_level(logging.INFO, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert len(result["preToolUse"]) == 1
        assert result["preToolUse"][0]["command"].endswith("/ok.sh")
        assert any("not executable" in rec.message for rec in caplog.records)

    def test_kiro_hooks_autoimport_skips_sensitive_path(self, tmp_path: Path, monkeypatch):
        """Scripts resolving into a sensitive path (~/.ssh) are rejected."""
        from kiro_crew.agent import _autoimport_kiro_hooks

        # Pretend HOME is tmp_path so ~/.ssh is fabricated and isolated.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        sensitive = tmp_path / ".ssh" / "evil.sh"
        sensitive.parent.mkdir(parents=True, exist_ok=True)
        sensitive.write_text("#!/bin/sh\nexit 0\n")
        sensitive.chmod(0o755)

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        symlink = hooks_dir / "evil.sh"
        symlink.symlink_to(sensitive)

        result = _autoimport_kiro_hooks(hooks_dir)
        assert result == {}

    def test_kiro_hooks_autoimport_dedupes_with_explicit_config(self, tmp_path: Path):
        """A script listed both explicitly and in the autoimport dir yields one entry."""
        from kiro_crew.agent import _apply_user_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        script = self._make_script(hooks_dir, "shared.sh")

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": {"preToolUse": [{"command": str(script)}]},
                "kiro_hooks_dir": str(hooks_dir),
            }
        }

        _apply_user_kiro_hooks(config, mc_cfg)

        entries = config["hooks"]["preToolUse"]
        assert len(entries) == 1
        assert entries[0]["command"] == str(script)

    def test_kiro_hooks_autoimport_respects_disable_flag(self, tmp_path: Path):
        """``agent.kiro_hooks_autoimport=False`` skips the scan even when scripts exist."""
        from kiro_crew.agent import _apply_user_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        self._make_script(hooks_dir, "a.sh")

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks_autoimport": False,
                "kiro_hooks_dir": str(hooks_dir),
            }
        }

        _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}

    def test_kiro_hooks_autoimport_honors_custom_dir(self, tmp_path: Path):
        """``agent.kiro_hooks_dir`` overrides the default ~/.kiro/hooks path."""
        from kiro_crew.agent import _apply_user_kiro_hooks

        custom = tmp_path / "custom-hooks"
        self._make_script(custom, "only.sh")

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": str(custom)}}

        _apply_user_kiro_hooks(config, mc_cfg)

        assert len(config["hooks"]["preToolUse"]) == 1
        assert config["hooks"]["preToolUse"][0]["command"].endswith("/only.sh")

    def test_kiro_hooks_autoimport_respects_total_limit(self, tmp_path: Path, caplog):
        """More scripts than ``_MAX_TOTAL_USER_HOOKS`` get capped; one WARNING logged."""
        import logging

        from kiro_crew.agent import _MAX_TOTAL_USER_HOOKS, _apply_user_kiro_hooks

        # Spread across events so the per-event cap (10) does not fire first.
        # Filename suffixes are used so the scripts route to different events
        # without needing to write headers.
        hooks_dir = tmp_path / "hooks"
        suffixes = ["-pre.sh", "-post.sh", "-prompt.sh", "-spawn.sh", "-stop.sh"]
        total = _MAX_TOTAL_USER_HOOKS + 5
        for i in range(total):
            self._make_script(hooks_dir, f"h{i:02d}{suffixes[i % len(suffixes)]}")

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": str(hooks_dir)}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        merged_total = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
        assert merged_total == _MAX_TOTAL_USER_HOOKS
        cap_warnings = [r for r in caplog.records if "global limit" in r.message.lower()]
        # review-bot rev 8 fix: `_merge_kiro_hooks` re-checks the cap at the
        # start of each event's inner loop, so the number of WARNINGs
        # depends on how scripts are distributed across events -- which
        # shifts with dict ordering changes or minor test edits.  The
        # invariant we actually care about is `merged_total == cap`
        # (asserted above); at least one cap WARNING must fire as
        # evidence the branch was exercised, but the exact count is not
        # the contract.  This matches the sibling test
        # `test_kiro_hooks_total_limit_shared_across_explicit_and_autoimport`.
        assert cap_warnings, (
            "expected at least one global-limit WARNING when scripts exceed "
            "_MAX_TOTAL_USER_HOOKS; the merged_total cap is the real invariant"
        )

    def test_kiro_hooks_total_limit_shared_across_explicit_and_autoimport(
        self, tmp_path: Path, caplog
    ):
        """Regression: ``_MAX_TOTAL_USER_HOOKS`` caps combined explicit + autoimport.

        review-bot finding (agent.py:778, importance=0): the
        original code in ``_apply_user_kiro_hooks`` called
        ``_merge_kiro_hooks`` twice — once for explicit entries, once for
        auto-discovered scripts.  Because ``_merge_kiro_hooks`` initializes
        ``total_added = 0`` on each call, the per-call cap of
        ``_MAX_TOTAL_USER_HOOKS`` (20) applied to each source independently,
        yielding up to 40 total user hooks instead of the intended 20.  The
        fix merges both sources in a single pass so the total cap is
        enforced across the combined set.

        This test stages enough scripts in BOTH sources that each source
        alone would fit under the cap (each has ``_MAX_TOTAL_USER_HOOKS``
        entries, which equals the cap), but together they exceed it.  The
        merged result must land at exactly ``_MAX_TOTAL_USER_HOOKS``, not
        ``2 * _MAX_TOTAL_USER_HOOKS``.  Under the old code this test
        observed ``merged_total == 2 * _MAX_TOTAL_USER_HOOKS``; under the
        new single-pass code it observes exactly the cap.
        """
        import logging

        from kiro_crew.agent import _MAX_TOTAL_USER_HOOKS, _apply_user_kiro_hooks

        # Build explicit kiro_hooks with _MAX_TOTAL_USER_HOOKS entries,
        # spread across events so the per-event cap (10) is not what
        # limits the explicit source.  These scripts are real files on
        # disk so ``_validate_hook_command`` accepts them.
        explicit_dir = tmp_path / "explicit-scripts"
        explicit_dir.mkdir(parents=True, exist_ok=True)
        explicit_events = ["preToolUse", "postToolUse", "userPromptSubmit"]
        explicit_hooks: dict[str, list[dict[str, str]]] = {ev: [] for ev in explicit_events}
        for i in range(_MAX_TOTAL_USER_HOOKS):
            script = explicit_dir / f"e{i:02d}.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            explicit_hooks[explicit_events[i % len(explicit_events)]].append(
                {"command": str(script)}
            )

        # Autoimport dir: _MAX_TOTAL_USER_HOOKS more scripts, also spread
        # across events via filename suffix so per-event cap doesn't fire
        # within the autoimport source alone.
        autoimport_dir = tmp_path / "hooks"
        autoimport_suffixes = [
            "-pre.sh",
            "-post.sh",
            "-prompt.sh",
            "-spawn.sh",
            "-stop.sh",
        ]
        for i in range(_MAX_TOTAL_USER_HOOKS):
            self._make_script(
                autoimport_dir,
                f"a{i:02d}{autoimport_suffixes[i % len(autoimport_suffixes)]}",
            )

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": explicit_hooks,
                "kiro_hooks_dir": str(autoimport_dir),
                # Default is True, but set explicitly so the test does
                # not silently become a single-source test if the
                # default ever flips.
                "kiro_hooks_autoimport": True,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        merged_total = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
        assert merged_total == _MAX_TOTAL_USER_HOOKS, (
            f"regression: combined explicit + autoimport hooks exceeded "
            f"_MAX_TOTAL_USER_HOOKS ({_MAX_TOTAL_USER_HOOKS}); got "
            f"{merged_total}.  Both sources must be merged in a single "
            f"_merge_kiro_hooks pass so the total cap is enforced across "
            f"the combined set, not per-source."
        )
        # The cap warning should fire at least once because the single
        # merge pass trips the global-limit branch when the combined input
        # exceeds the cap.  It can fire multiple times because
        # ``_merge_kiro_hooks`` iterates events and re-checks the cap per
        # event after it is reached; the count is not the invariant here,
        # the total-merged count above is.
        cap_warnings = [r for r in caplog.records if "global limit" in r.message.lower()]
        assert cap_warnings, (
            "expected at least one global-limit WARNING from the single "
            "merge pass when combined input exceeds _MAX_TOTAL_USER_HOOKS"
        )

    def test_kiro_hooks_per_event_cap_emits_sel_audit(self, tmp_path: Path, monkeypatch, caplog):
        """Regression: per-event cap break must emit SEL audit.

        review-bot rev 8 finding (agent.py:682, importance=1,
        security-controls): when ``_merge_kiro_hooks`` hits the per-event
        cap ``_MAX_USER_HOOKS_PER_EVENT``, remaining entries for that
        event were silently dropped with only a ``logger.warning`` and
        no ``_sel_hook_rejected`` call.  Every other rejection branch
        in ``_merge_kiro_hooks`` (missing command, failed validation,
        non-string matcher, invalid matcher) correctly emits SEL audit.
        Closing this audit-trail gap so auditors can distinguish "user
        configured 15 preToolUse hooks and 5 were cap-dropped" from
        "user configured 10 and all loaded".
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _MAX_USER_HOOKS_PER_EVENT, _apply_user_kiro_hooks

        # Configure more scripts on a single event than the per-event cap.
        # All scripts route to preToolUse so the per-event cap fires before
        # the total cap (which is higher).
        explicit_dir = tmp_path / "scripts"
        explicit_dir.mkdir()
        over_cap = _MAX_USER_HOOKS_PER_EVENT + 3
        explicit_hooks: dict[str, list[dict[str, str]]] = {"preToolUse": []}
        for i in range(over_cap):
            script = explicit_dir / f"h{i:02d}.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            explicit_hooks["preToolUse"].append({"command": str(script)})

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": explicit_hooks,
                "kiro_hooks_autoimport": False,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        # Exactly _MAX_USER_HOOKS_PER_EVENT scripts should have been
        # merged for preToolUse (the cap); the other 3 must be dropped.
        assert len(config["hooks"]["preToolUse"]) == _MAX_USER_HOOKS_PER_EVENT
        # The per-event cap path must have emitted at least one SEL
        # audit tagged with the event (preToolUse) and the
        # "per-event limit exceeded" reason.  Under the pre-fix code
        # this assertion failed with zero SEL calls tagged that reason.
        cap_sel = [c for c in sel_calls if "per-event limit exceeded" in c[2].lower()]
        assert cap_sel, (
            f"regression: per-event cap must emit _sel_hook_rejected; got "
            f"zero calls with reason 'per-event limit exceeded'.  All SEL "
            f"calls: {sel_calls!r}"
        )
        # The tag should be the event name, not the literal "autoimport",
        # since this is inside _merge_kiro_hooks which uses the inferred
        # event as the SEL tag (consistent with other branches in this
        # function).
        assert cap_sel[0][0] == "preToolUse"

    def test_kiro_hooks_global_cap_emits_sel_audit(self, tmp_path: Path, monkeypatch, caplog):
        """Regression: global cap break must emit SEL audit.

        review-bot rev 8 finding (agent.py:688, importance=1,
        security-controls): sibling to the per-event cap gap.  When the
        global cap ``_MAX_TOTAL_USER_HOOKS`` is hit across all events,
        remaining hooks are silently dropped.  An auditor cannot
        distinguish "25 configured, 5 cap-dropped" from "20 configured,
        all loaded" without a SEL signal.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _MAX_TOTAL_USER_HOOKS, _apply_user_kiro_hooks

        # Spread over-cap scripts across events so the per-event cap (10)
        # does not fire first; only the global cap (20) gates.
        explicit_dir = tmp_path / "scripts"
        explicit_dir.mkdir()
        over_cap = _MAX_TOTAL_USER_HOOKS + 3
        events = ["preToolUse", "postToolUse", "userPromptSubmit"]
        explicit_hooks: dict[str, list[dict[str, str]]] = {e: [] for e in events}
        for i in range(over_cap):
            script = explicit_dir / f"g{i:02d}.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)
            explicit_hooks[events[i % len(events)]].append({"command": str(script)})

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        mc_cfg = {
            "agent": {
                "kiro_hooks": explicit_hooks,
                "kiro_hooks_autoimport": False,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        merged_total = sum(len(v) for v in config["hooks"].values() if isinstance(v, list))
        assert merged_total == _MAX_TOTAL_USER_HOOKS
        # Global-cap SEL audit must fire at least once.  The reason
        # string is the contract; the exact count depends on how many
        # events the loop visits after the cap is reached.
        cap_sel = [c for c in sel_calls if "global limit exceeded" in c[2].lower()]
        assert cap_sel, (
            f"regression: global cap must emit _sel_hook_rejected; got "
            f"zero calls with reason 'global limit exceeded'.  All SEL "
            f"calls: {sel_calls!r}"
        )

    def test_kiro_hooks_unknown_event_type_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: unknown event type in user_hooks must emit SEL audit.

        review-bot rev 9 follow-up: when ``_merge_kiro_hooks``
        encounters an event name not in ``_VALID_HOOK_EVENTS`` (e.g.
        typo or future-event-name from a newer kiro-cli), the entire
        event-bucket is dropped.  This is a permission decision per
        AUTOSDE.yaml security-controls and must emit SEL audit so an
        auditor can distinguish "user configured 0 hooks for event X"
        from "user configured 5 and the whole bucket was dropped for
        invalid event name".
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        # "bogusEvent" is not in _VALID_HOOK_EVENTS; bucket must be
        # dropped with SEL audit.
        mc_cfg = {
            "agent": {
                "kiro_hooks": {
                    "bogusEvent": [{"command": "/bin/true"}],
                },
                "kiro_hooks_autoimport": False,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}
        unknown_sel = [c for c in sel_calls if "unknown event" in c[2].lower()]
        assert unknown_sel, (
            f"regression: unknown event type must emit _sel_hook_rejected; " f"got {sel_calls!r}"
        )
        event_tag, _command, reason = unknown_sel[0]
        assert event_tag == "bogusEvent"
        assert "unknown event type" in reason.lower()

    def test_kiro_hooks_non_list_entries_emits_sel_audit(self, tmp_path: Path, monkeypatch, caplog):
        """Regression: non-list entries for a valid event must emit SEL audit.

        review-bot rev 9 finding (agent.py:669, importance=1,
        security-controls): when ``_merge_kiro_hooks`` sees
        ``user_hooks["preToolUse"]`` is e.g. a string or dict (not a
        list), the whole bucket is dropped silently.  Auditors need a
        SEL signal to distinguish "malformed config dropped all hooks
        for this event" from "no hooks configured".
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        # ``preToolUse`` is a valid event, but a string is not a list;
        # bucket must be dropped with SEL audit.
        mc_cfg = {
            "agent": {
                "kiro_hooks": {
                    "preToolUse": "not-a-list-but-a-string",
                },
                "kiro_hooks_autoimport": False,
            }
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        non_list_sel = [c for c in sel_calls if "not a list" in c[2].lower()]
        assert non_list_sel, (
            f"regression: non-list entries must emit _sel_hook_rejected; " f"got {sel_calls!r}"
        )
        event_tag, _command, reason = non_list_sel[0]
        assert event_tag == "preToolUse"
        assert "not a list" in reason.lower()

    def test_kiro_hooks_autoimport_missing_dir_is_noop(self, tmp_path: Path, caplog):
        """Missing directory returns empty dict with only a DEBUG log (no WARNINGs)."""
        import logging

        from kiro_crew.agent import _autoimport_kiro_hooks

        missing = tmp_path / "does-not-exist"

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(missing)

        assert result == {}
        # Scope to the kiro_crew.agent logger: under `pytest -n auto`, a leaked
        # asyncio task exception from an unrelated test (e.g. "Task exception was
        # never retrieved" on the root `asyncio` logger) can land in caplog during
        # this window and falsely trip a bare records scan. We only care that THIS
        # code path emits no WARNING+.
        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == "kiro_crew.agent"
        ]
        assert warnings == []

    def test_kiro_hooks_autoimport_invalid_matcher_skips_script(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """An invalid matcher header must skip the script entirely.

        Regression guard: silently demoting a tool-scoped hook to an unscoped
        hook (firing on every tool call) would be a privilege expansion.  The
        whole script must be rejected so the user notices and fixes the
        matcher instead of getting a silently-broader hook.

        Also asserts (rev 7 review-bot fix): the SEL audit event uses the
        literal ``"autoimport"`` tag for consistency with every other
        rejection branch in ``_autoimport_kiro_hooks``.  The pre-fix code
        passed the variable ``event`` (e.g. ``"preToolUse"``), which broke
        audit-trail consistency -- auditors filtering on
        ``event="autoimport"`` would miss invalid-matcher rejections.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        # Matcher with a space is rejected by _SAFE_MATCHER_RE.
        entry = self._make_script(
            hooks_dir, "bad.sh", body="# matcher: tool name with spaces\nexit 0\n"
        )

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert any(
            "matcher" in rec.message.lower() and "invalid" in rec.message.lower()
            for rec in caplog.records
        )
        # Rev 7 review-bot fix: SEL tag must be the literal "autoimport",
        # not the variable ``event`` (e.g. "preToolUse").  Under the
        # pre-fix code this assertion failed with event_tag == "preToolUse".
        assert len(sel_calls) == 1, (
            f"expected exactly one _sel_hook_rejected for invalid matcher; "
            f"got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport", (
            f"regression: invalid-matcher SEL audit used tag {event_tag!r}; "
            f"must be literal 'autoimport' for audit-trail consistency with "
            f"every other rejection branch in _autoimport_kiro_hooks."
        )
        assert command == str(entry)
        assert "invalid matcher" in reason.lower()

    def test_kiro_hooks_autoimport_unknown_event_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: unknown ``# event:`` rejection must emit a SEL audit event.

        review-bot finding (agent.py:586, importance=1,
        security-controls): when ``_infer_hook_event`` returns ``None``
        (the header declares an event name outside ``_HOOK_EVENT_CANONICAL``),
        the script is rejected.  Before this fix, the rejection only emitted
        a ``logger.warning`` — it did NOT call ``_sel_hook_rejected``.  Every
        other rejection branch in ``_autoimport_kiro_hooks`` (symlink-escape,
        failed-validation, invalid-matcher) correctly emits a SEL audit event
        per AUTOSDE.yaml's security-controls rule: "All tool invocations and
        permission decisions must emit SEL audit events via sel.py."

        This test stages a script whose ``# event:`` header is a bogus event
        name (not in ``_HOOK_EVENT_CANONICAL``), spies on
        ``_sel_hook_rejected``, and asserts the audit call was made with the
        ``"autoimport"`` source tag, the script path, and an
        ``"unknown event header"`` reason.  Under the pre-fix code, the spy
        observed zero calls and this test failed with the designed error
        message; under the fix, it observes exactly one.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        # ``NoSuchEvent`` is not in _HOOK_EVENT_CANONICAL, so _infer_hook_event
        # returns None and this rejection branch fires.  The filename has no
        # known suffix so fallback inference does not rescue it either.
        script = self._make_script(hooks_dir, "bogus.sh", body="# event: NoSuchEvent\nexit 0\n")

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        # The SEL audit must fire exactly once for this one rejected script,
        # tagged with the ``"autoimport"`` log label and the script path.
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when a "
            f"script's '# event:' header is unknown; got {len(sel_calls)}: "
            f"{sel_calls!r}.  Every rejection branch in _autoimport_kiro_hooks "
            f"must emit a SEL audit event per AUTOSDE.yaml security-controls."
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(script)
        assert "unknown event" in reason.lower()

    def test_kiro_hooks_autoimport_cannot_resolve_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: ``entry.resolve()`` failure rejection must emit SEL audit.

        review-bot finding (agent.py:556, rev 4, importance=1,
        security-controls): the ``cannot resolve entry`` rejection branch
        (when ``entry.resolve()`` raises ``OSError``) was missing the
        ``_sel_hook_rejected`` call.  Every other rejection branch in
        ``_autoimport_kiro_hooks`` emits a SEL audit event per
        AUTOSDE.yaml's security-controls rule.  Without this call, an
        auditor reconstructing agent-install activity from SEL would not
        see scripts dropped due to resolve() failures.

        This test forces ``Path.resolve`` to raise ``OSError`` for the
        entry and asserts a SEL audit is recorded with the
        ``"autoimport"`` source tag and a ``"cannot resolve entry"``
        reason.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "broken.sh")

        # Patch Path.resolve to raise OSError ONLY for the entry path,
        # letting hooks_dir.resolve() (the first resolve() call in
        # _autoimport_kiro_hooks) succeed.  Otherwise the function
        # returns early before the loop even starts.
        real_resolve = Path.resolve

        def _raising_resolve(self, *args, **kwargs):
            if self == entry:
                raise OSError("simulated resolve failure")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _raising_resolve)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"entry.resolve() raises; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(entry)
        assert "cannot resolve" in reason.lower()

    def test_kiro_hooks_autoimport_cannot_stat_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: ``resolved_entry.stat()`` failure must emit SEL audit.

        review-bot finding (agent.py:556, rev 4, importance=1,
        security-controls): the ``cannot stat entry`` rejection branch
        (when ``resolved_entry.stat()`` raises ``OSError``) was missing
        the ``_sel_hook_rejected`` call.  Same audit-completeness class
        of bug as the ``cannot resolve`` gap.

        This test forces ``Path.stat`` to raise ``OSError`` for the
        resolved entry and asserts a SEL audit is recorded with the
        ``"autoimport"`` source tag and a ``"cannot stat entry"`` reason.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "broken.sh")

        # Arm the stat failure only AFTER ``entry.is_file()`` has been
        # observed on this path.  The production loop calls ``is_file()``
        # first (internally a stat), then later ``resolved_entry.stat()``
        # explicitly.  We want only the explicit call to fail.
        #
        # Previous version used ``call_count >= 3`` which worked on
        # CPython 3.12 but is fragile: pathlib's internal stat-usage per
        # ``is_file()`` varies between 3.10, 3.11, 3.12, 3.13 (3.12
        # rewrote pathlib internals), so a hard-coded threshold is a
        # time bomb.  Gating on ``is_file`` being called instead is
        # stable across versions — no matter how many stats ``is_file``
        # makes internally, we only raise on stats that happen *after*
        # it completes, which is when ``resolved_entry.stat()`` runs.
        real_stat = Path.stat
        real_is_file = Path.is_file
        armed = False

        def _arming_is_file(self, *args, **kwargs):
            # Arm by Path identity (self == entry) rather than by
            # self.name, which would also fire on any path whose
            # basename happens to be "broken.sh" (e.g. a sibling
            # fixture in a future multi-script variant of this test).
            nonlocal armed
            rv = real_is_file(self, *args, **kwargs)
            if self == entry:
                armed = True
            return rv

        def _raising_stat(self, *args, **kwargs):
            if self == entry and armed:
                raise OSError("simulated stat failure")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "is_file", _arming_is_file)
        monkeypatch.setattr(Path, "stat", _raising_stat)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"resolved_entry.stat() raises; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(entry)
        assert "cannot stat" in reason.lower()

    def test_kiro_hooks_autoimport_non_executable_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: non-executable ``.sh`` skip must emit SEL audit.

        review-bot rev 6 follow-up (agent.py:581,
        importance=1, security-controls): the non-executable skip
        branch was the last rejection path in ``_autoimport_kiro_hooks``
        that logged at INFO only and did NOT call
        ``_sel_hook_rejected``.  Every other rejection branch
        (symlink-escape, cannot-resolve, cannot-stat, failed-validation,
        unknown-event, invalid-matcher, cannot-read-dir) emits a SEL
        audit event per AUTOSDE.yaml's security-controls rule: "All
        tool invocations and permission decisions must emit SEL audit
        events via sel.py."  The non-executable skip is also a
        permission decision (a discovered ``.sh`` file will NOT be
        loaded as a hook), so an auditor reconstructing agent-install
        activity from SEL must see it.

        This test drops a non-executable ``.sh`` file in the hooks dir,
        spies on ``_sel_hook_rejected``, and asserts exactly one audit
        call with the ``"autoimport"`` tag and a ``"not executable"``
        reason.  Under the pre-fix code, the spy observed zero calls
        and this test failed with the designed error message.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "disabled.sh", executable=False)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.INFO, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"a non-executable .sh is skipped; got {len(sel_calls)}: "
            f"{sel_calls!r}.  Every rejection branch in "
            f"_autoimport_kiro_hooks must emit a SEL audit event per "
            f"AUTOSDE.yaml security-controls."
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(entry)
        assert "not executable" in reason.lower()

    def test_kiro_hooks_autoimport_rejects_dir_equal_to_home(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: ``kiro_hooks_dir: "~"`` (resolved == HOME) must be rejected.

        review-bot finding (agent.py:746, rev 4, importance=0,
        default): the original containment check allowed
        ``resolved == home`` to pass because the condition was
        ``(resolved != home and home not in resolved.parents)``.  When
        ``resolved == home``, the left side of the ``and`` was ``False``,
        so the whole clause was ``False`` and the path was *accepted*.

        Impact: an LLM-writable config setting ``kiro_hooks_dir: "~"``
        would cause ``_autoimport_kiro_hooks`` to scan the *entire* home
        directory for executable ``*.sh`` files, auto-registering any
        executable script anywhere under ``$HOME``.

        The fix is strict containment: require ``resolved`` to be *under*
        HOME, not equal to it.  ``Path.parents`` of e.g. ``/home/user``
        is ``(/, /home)`` and does not include ``/home/user`` itself, so
        ``home not in resolved.parents`` rejects ``resolved == home``.

        This test sets ``kiro_hooks_dir`` to a path that resolves to HOME
        (the fake home we monkeypatch to ``tmp_path``) and asserts no
        scripts get merged — the "evil" script at HOME root is not
        auto-registered — and that the rejection is logged + SEL-audited.
        """
        import logging

        from kiro_crew.agent import _apply_user_kiro_hooks

        # ``_isolate_home`` fixture already sets Path.home() -> tmp_path.
        # Re-route the default fallback so failure does not touch the real
        # ~/.kiro/hooks.  Place an executable script directly at HOME root
        # to prove that home-root scanning would pick it up.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / ".kiro" / "hooks",
        )
        evil = tmp_path / "evil.sh"
        evil.write_text("#!/bin/sh\nexit 0\n")
        evil.chmod(0o755)

        config: dict = {"hooks": {}}
        # ``kiro_hooks_dir: "~"`` expands to HOME, which equals tmp_path
        # after the _isolate_home fixture runs.  Under the pre-fix code
        # this passed validation; under the fix it's rejected.
        mc_cfg = {"agent": {"kiro_hooks_dir": str(tmp_path)}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        # Critical invariant: nothing from HOME-root got auto-registered.
        # The "evil" script must NOT appear in config["hooks"].
        assert config["hooks"] == {}, (
            f"regression: kiro_hooks_dir resolving to HOME itself was accepted, "
            f"causing home-root scan; expected empty hooks dict, got "
            f"{config['hooks']!r}.  The containment check must reject "
            f"resolved == home."
        )
        assert any(
            "kiro_hooks_dir" in rec.message and "rejected" in rec.message.lower()
            for rec in caplog.records
        ), "expected 'kiro_hooks_dir ... rejected' WARNING from containment check"

    def test_kiro_hooks_autoimport_rejects_dir_outside_home(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """kiro_hooks_dir resolving outside HOME is rejected with a fallback warning.

        Regression guard: config.json is LLM-writable.  A malicious override
        pointing autoimport at /tmp or /var could auto-register any executable
        script an attacker lands there.  The code must fall back to the
        default ~/.kiro/hooks and log a WARNING + SEL audit.
        """
        import logging

        from kiro_crew.agent import _apply_user_kiro_hooks

        # Stage a fake HOME so our default hooks dir doesn't exist and so
        # tmp_path's /private/var/folders path is *outside* HOME.
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        # Also re-route the default hooks dir into the fake HOME so fallback
        # does not hit the caller's real ~/.kiro/hooks directory.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            fake_home / ".kiro" / "hooks",
        )

        # This dir is genuinely outside fake_home, since tmp_path itself is
        # the parent directory of fake_home.
        outside = tmp_path / "outside-hooks"
        outside.mkdir()
        self._make_script(outside, "evil.sh")

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": str(outside)}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        # Fallback is fake_home/.kiro/hooks (doesn't exist), so nothing gets
        # merged.  Critically: the "evil" script is not merged.
        assert config["hooks"] == {}
        assert any(
            "kiro_hooks_dir" in rec.message and "rejected" in rec.message.lower()
            for rec in caplog.records
        )

    def test_kiro_hooks_autoimport_rejects_symlink_escaping_dir(self, tmp_path: Path, caplog):
        """A symlink inside hooks_dir pointing at an outside script is rejected.

        Regression guard: entry.is_file() follows symlinks, and
        is_sensitive_path only matches a small set of $HOME subdirs.  A
        symlink named ``guard.sh`` inside ~/.kiro/hooks/ whose target is
        ~/elsewhere/attacker.sh would otherwise pass every other check.
        The resolved-path containment check must catch it.
        """
        import logging

        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        # Outside target, inside HOME so the is_sensitive_path check alone
        # wouldn't reject it - only the containment check does.
        outside_target = tmp_path / "elsewhere" / "attacker.sh"
        outside_target.parent.mkdir(parents=True, exist_ok=True)
        outside_target.write_text("#!/bin/sh\nexit 0\n")
        outside_target.chmod(0o755)
        (hooks_dir / "guard.sh").symlink_to(outside_target)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert any("resolves outside" in rec.message for rec in caplog.records)

    def test_kiro_hooks_autoimport_validates_before_parsing_headers(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: ``_validate_hook_command`` must run BEFORE header parsing.

        review-bot finding (agent.py:562, importance=1,
        security-controls): the original ordering parsed the script's
        ``# event:`` / ``# matcher:`` headers first and only then called
        ``_validate_hook_command``.  Defense-in-depth: even though the
        containment check above already rejects sensitive-path symlinks,
        the no-file-reads-on-rejected-paths invariant is worth keeping
        explicit.

        To prove the reorder (rather than the pre-existing containment
        check) is what stops header parsing, we force a rejection from
        ``_validate_hook_command`` specifically: monkeypatch it to return
        ``None`` for any path, then assert ``_parse_hook_script_headers``
        was never invoked.  Under the OLD code, headers were parsed first
        and this test would observe a call; under the NEW code, validation
        rejects before the parser runs.

        We also assert the rejection is SEL-audited via
        ``_sel_hook_rejected`` so a future refactor that silently drops
        the audit call is caught.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        # One legitimate script inside hooks_dir (passes containment).
        hooks_dir = tmp_path / "hooks"
        script = self._make_script(hooks_dir, "ok.sh")

        # Force _validate_hook_command to reject everything.  This isolates
        # the reorder: if header parsing ran before validation, we'd still
        # see a call; with the reorder, validation rejects first.
        monkeypatch.setattr(_agent_mod, "_validate_hook_command", lambda *_a, **_kw: None)

        header_calls: list[str] = []

        def _record_header_call(path: Path) -> tuple:
            header_calls.append(str(path))
            return None, None

        monkeypatch.setattr(_agent_mod, "_parse_hook_script_headers", _record_header_call)

        # Spy on the SEL audit sink so we can assert a rejection was
        # recorded with the ``"autoimport"`` source tag.  Without this,
        # a future refactor that silently drops the audit call would
        # still let the monkeypatched validate-to-None path pass.
        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert header_calls == [], (
            "regression: _parse_hook_script_headers was called before "
            "_validate_hook_command rejected the script. Validation must "
            "run first so no file reads happen on rejected paths."
        )
        # Exactly one SEL rejection recorded for the autoimport rejection
        # branch, tagged with the ``"autoimport"`` log label (a log-only
        # tag; see agent.py:562-570 note).
        assert len(sel_calls) == 1, (
            f"expected exactly one _sel_hook_rejected call for the rejected "
            f"script; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(script)
        assert "failed validation" in reason

    def test_kiro_hooks_dir_stored_as_resolved_path(self, tmp_path: Path, monkeypatch):
        """Regression: ``_autoimport_kiro_hooks`` receives the *resolved* hooks dir.

        review-bot finding (agent.py:744, TOCTOU): the original
        code did ``hooks_dir = requested`` after validating ``resolved``.
        If a path component of ``requested`` was a symlink, it could be
        swapped between the validate-in-HOME check in
        ``_apply_user_kiro_hooks`` and the resolve()-for-containment check
        inside ``_autoimport_kiro_hooks``, letting autoimport scan a
        directory outside HOME.  Storing the already-resolved path makes
        the downstream resolve() a no-op on an already-canonical path,
        eliminating the named symlink-swap window.

        This test does not (and cannot deterministically) reproduce the
        race itself; it verifies the observable contract that proves the
        mitigation is in place: the caller stores and forwards the
        resolved form of ``kiro_hooks_dir``.  The per-entry containment
        check already canonicalizes each entry, so asserting on the
        resulting command path cannot tell the fix apart from the bug -
        we instead intercept the call into ``_autoimport_kiro_hooks``
        and assert it was invoked with the *resolved* path, not the
        symlinked ``requested`` path.
        """
        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Real hooks directory plus a user-facing symlink that points at it.
        real_hooks = tmp_path / "real" / "hooks"
        real_hooks.mkdir(parents=True)
        link_hooks = tmp_path / "link-hooks"
        link_hooks.symlink_to(real_hooks)

        captured: list[Path] = []

        # Spy on _autoimport_kiro_hooks to capture what `hooks_dir` value
        # _apply_user_kiro_hooks stores and forwards.  Under the old
        # `hooks_dir = requested` code, we would see `link_hooks`; under
        # the new `hooks_dir = resolved` code, we see `real_hooks`.
        def _spy(hooks_dir: Path) -> dict:
            captured.append(hooks_dir)
            return {}

        monkeypatch.setattr(_agent_mod, "_autoimport_kiro_hooks", _spy)

        config: dict = {"hooks": {}}
        # Explicit ``kiro_hooks_autoimport: True`` so the test does not
        # silently become a no-op if the default ever flips.
        mc_cfg = {
            "agent": {
                "kiro_hooks_autoimport": True,
                "kiro_hooks_dir": str(link_hooks),
            }
        }

        _apply_user_kiro_hooks(config, mc_cfg)

        assert len(captured) == 1, "autoimport should have been invoked exactly once"
        forwarded = captured[0]
        # The forwarded path must equal the real (resolved) directory, NOT
        # the symlink requested by the user.  Comparing by resolve() on
        # both sides would mask the bug (since resolve(link) == real);
        # comparing the raw Path confirms the resolved form was stored.
        assert forwarded == real_hooks, (
            f"regression: _autoimport_kiro_hooks received {forwarded!r}; "
            f"expected the resolved path {real_hooks!r}. _apply_user_kiro_hooks "
            "must store the resolved path so the downstream resolve() is a "
            "no-op even under adversarial symlink swaps."
        )
        # Sanity: it really is NOT the symlink form (would be the bug).
        assert forwarded != link_hooks

    def test_kiro_hooks_dir_null_byte_does_not_crash(self, tmp_path: Path, monkeypatch):
        """Regression: ``kiro_hooks_dir`` with a null byte must not crash.

        deep-review adversarial finding: ``Path("\\x00")``
        raises ``ValueError: embedded null byte`` -- uncaught, this
        propagates up through ``install_agent()`` and crashes agent
        bootstrap (denial of service via LLM-writable config).

        Fix: ``except (OSError, ValueError)`` around the resolve() pair.
        This test feeds a null-byte ``kiro_hooks_dir`` and asserts the
        code neither raises nor registers any hooks (falls back to the
        default which we re-route away from the caller's real
        ``~/.kiro/hooks``).  Under the pre-fix code, ``ValueError`` would
        escape and the test body would fail with an unhandled exception.
        """
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Re-route the default so fallback doesn't touch caller's HOME.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / "nonexistent" / "hooks",
        )

        config: dict = {"hooks": {}}
        mc_cfg = {"agent": {"kiro_hooks_dir": "\x00"}}

        # Must not raise.  Under pre-fix code this line propagates
        # ``ValueError: embedded null byte`` from Path.resolve().
        _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}

    def test_kiro_hooks_autoimport_cannot_read_dir_emits_sel_audit(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: ``iterdir()`` OSError on hooks_dir must emit SEL audit.

        deep-review coverage finding: the ``cannot read
        <hooks_dir>`` rejection branch in ``_autoimport_kiro_hooks``
        (when ``hooks_dir.iterdir()`` raises ``OSError``, e.g. EACCES)
        was missing the ``_sel_hook_rejected`` call.  Without it, an
        auditor reconstructing agent-install activity cannot
        distinguish "hooks dir unreadable" from "no scripts configured"
        -- both show ``requested_autoimport=0`` in the merge summary.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        # Force iterdir() to raise OSError (simulating permission
        # denial).  Narrow the patch to Path.iterdir only.
        def _raising_iterdir(self):
            raise OSError("simulated permission denied")

        monkeypatch.setattr(Path, "iterdir", _raising_iterdir)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"iterdir() raises OSError; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(hooks_dir)
        assert "cannot read" in reason.lower()

    def test_kiro_hooks_dir_non_string_ignored_without_sel(self, tmp_path: Path, monkeypatch):
        """Regression: non-string ``kiro_hooks_dir`` reverts to default silently.

        deep-review coverage finding: an LLM-writable
        ``"kiro_hooks_dir": null`` or ``[]`` silently skips the
        containment check (the ``isinstance(custom_dir, str) and
        custom_dir`` guard) and uses the default ``~/.kiro/hooks``.
        A malicious config that intentionally sets the value to a
        non-string should NOT emit a false-positive SEL rejection
        (nothing was actually rejected), but it also should not
        crash or scan an unintended directory.

        This test asserts: (a) fallback to default, (b) no SEL call,
        (c) no crash.
        """
        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Re-route default so fallback is empty (hooks dir doesn't exist).
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / "nonexistent" / "hooks",
        )

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        for bogus in (None, [], 42, {"foo": "bar"}):
            config: dict = {"hooks": {}}
            mc_cfg = {"agent": {"kiro_hooks_dir": bogus}}
            # Must not raise.
            _apply_user_kiro_hooks(config, mc_cfg)
            assert config["hooks"] == {}, (
                f"non-string kiro_hooks_dir={bogus!r} produced hooks: " f"{config['hooks']!r}"
            )

        assert sel_calls == [], (
            f"non-string kiro_hooks_dir should NOT emit SEL "
            f"(nothing was rejected); got: {sel_calls!r}"
        )

    def test_kiro_hooks_autoimport_rejects_dir_equal_to_symlinked_home(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: HOME-containment survives HOME-as-symlink topology.

        deep-review coverage finding: corp devdesks and
        macOS laptops often have ``$HOME`` as a symlink (e.g.
        ``/home/user -> /mnt/fast-disk/user``).  The strict-containment
        check uses ``home = Path.home().resolve()`` and ``resolved =
        requested.resolve()`` -- both canonicalize, so the check should
        survive.  This test proves that contract: user points
        ``kiro_hooks_dir`` at the canonical (resolved) HOME target
        directly, while ``Path.home()`` returns the symlink; both
        must canonicalize to the same path and get rejected.

        Under a hypothetical regression where one side stops calling
        ``.resolve()``, the paths would mismatch and the test would
        accept the equal-to-HOME config, failing the assertion.
        """
        import logging

        from kiro_crew.agent import _apply_user_kiro_hooks

        # Construct a real directory and a symlink to it.
        real_home = tmp_path / "real_home"
        real_home.mkdir()
        symlink_home = tmp_path / "link_home"
        symlink_home.symlink_to(real_home)

        # Path.home() returns the symlink; .resolve() inside the code
        # should canonicalize it to real_home.
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: symlink_home))
        # Re-route default so fallback doesn't touch caller's real HOME.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            symlink_home / ".kiro" / "hooks",
        )

        # Plant an executable at the canonical HOME root to prove it
        # would be scanned under a buggy containment check.
        evil = real_home / "evil.sh"
        evil.write_text("#!/bin/sh\nexit 0\n")
        evil.chmod(0o755)

        config: dict = {"hooks": {}}
        # User points kiro_hooks_dir directly at the canonical HOME
        # (bypassing the symlink) -- should still be rejected because
        # resolved == canonical HOME.
        mc_cfg = {"agent": {"kiro_hooks_dir": str(real_home)}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}, (
            f"regression: kiro_hooks_dir resolving to canonical HOME "
            f"(via symlink'd Path.home()) was accepted; expected "
            f"empty hooks, got {config['hooks']!r}.  The containment "
            f"check must .resolve() both sides."
        )
        assert any(
            "kiro_hooks_dir" in rec.message and "rejected" in rec.message.lower()
            for rec in caplog.records
        )

    def test_kiro_hooks_autoimport_hooks_dir_resolve_oserror_emits_sel(
        self, tmp_path: Path, monkeypatch, caplog
    ):
        """Regression: ``hooks_dir.resolve()`` OSError must emit SEL audit.

        review-bot rev 5 follow-up (agent.py:509,
        security-controls): the initial ``hooks_dir.resolve()`` failure
        branch returned early with only a ``logger.debug`` -- no
        ``_sel_hook_rejected`` call.  Same audit-completeness gap class
        as rev 5 fixed for the per-entry cannot-resolve-entry branch,
        missed on the directory-level resolve.

        This test forces ``Path.resolve`` to raise ``OSError`` for the
        hooks_dir specifically and asserts a SEL audit is recorded with
        the ``"autoimport"`` source tag and a ``"cannot resolve
        hooks_dir"`` reason.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()

        real_resolve = Path.resolve

        def _raising_resolve(self, *args, **kwargs):
            # Fire only on the hooks_dir, not on entries or Path.home().
            if self == hooks_dir:
                raise OSError("simulated resolve failure on hooks_dir")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _raising_resolve)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.agent"):
            result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: expected exactly one _sel_hook_rejected call when "
            f"hooks_dir.resolve() raises; got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(hooks_dir)
        assert "cannot resolve hooks_dir" in reason.lower()

    def test_kiro_hooks_autoimport_handles_valueerror_on_entry_resolve(
        self, tmp_path: Path, monkeypatch
    ):
        """Regression: ``ValueError`` from ``entry.resolve()`` must not crash.

        review-bot rev 5 follow-up (agent.py:540, default):
        the two inner ``resolve()`` calls in ``_autoimport_kiro_hooks``
        only caught ``OSError``, not ``ValueError``.  A filename from
        ``iterdir()`` containing a null byte (FS shenanigans, adversarial
        filenames) would propagate ``ValueError: embedded null byte``
        uncaught and crash agent bootstrap.

        Fix: ``except (OSError, ValueError)`` on both inner resolve
        calls.  This test forces ``Path.resolve`` on an entry to raise
        ``ValueError`` and asserts the code rejects cleanly (no crash,
        SEL audited).
        """
        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _autoimport_kiro_hooks

        hooks_dir = tmp_path / "hooks"
        entry = self._make_script(hooks_dir, "bad.sh")

        real_resolve = Path.resolve

        def _raising_resolve(self, *args, **kwargs):
            if self == entry:
                raise ValueError("embedded null byte")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _raising_resolve)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        # Must not raise.
        result = _autoimport_kiro_hooks(hooks_dir)

        assert result == {}
        assert len(sel_calls) == 1, (
            f"regression: ValueError on entry.resolve() must trigger the "
            f"same SEL-audited rejection branch as OSError; got "
            f"{len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        assert command == str(entry)
        assert "cannot resolve" in reason.lower()

    def test_kiro_hooks_dir_resolve_oserror_falls_back(self, tmp_path: Path, monkeypatch, caplog):
        """Regression: OSError from ``Path.resolve()`` falls back cleanly.

        deep-review coverage finding: if
        ``requested.resolve()`` or ``Path.home().resolve()`` raises
        ``OSError`` (ENAMETOOLONG, ELOOP, EACCES on a path component),
        the code sets ``resolved = home = None`` and falls through to
        the rejection branch, logs a warning, and emits SEL audit.

        This test forces the OSError path and asserts: (a) no crash,
        (b) hooks empty (fallback), (c) SEL audit emitted, (d) warning
        logged.  Under a hypothetical regression where the except is
        narrowed back to only ``OSError`` without catching ValueError,
        this test would still pass -- it specifically exercises the
        ``OSError`` arm of the ``except (OSError, ValueError)`` block.
        """
        import logging

        from kiro_crew import agent as _agent_mod
        from kiro_crew.agent import _apply_user_kiro_hooks

        # Re-route default so fallback is inert.
        monkeypatch.setattr(
            "kiro_crew.agent._DEFAULT_KIRO_HOOKS_DIR",
            tmp_path / "nonexistent" / "hooks",
        )

        # Force Path.resolve to raise OSError.  Narrow the patch so only
        # resolve() on the "requested" path fails; Path.home() still
        # works so home=None comes from resolved=None cascade.
        real_resolve = Path.resolve

        def _raising_resolve(self, *args, **kwargs):
            # Raise only on the user-supplied path (a custom fake-home
            # target we pass below).  Leave other resolve() calls alone.
            if self.name == "too-long":
                raise OSError("ENAMETOOLONG simulated")
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", _raising_resolve)

        sel_calls: list[tuple[str, str, str]] = []

        def _record_sel(event: str, command: str, reason: str) -> None:
            sel_calls.append((event, command, reason))

        monkeypatch.setattr(_agent_mod, "_sel_hook_rejected", _record_sel)

        config: dict = {"hooks": {}}
        # A path whose name triggers our resolve-fail shim.
        mc_cfg = {"agent": {"kiro_hooks_dir": str(tmp_path / "too-long")}}

        with caplog.at_level(logging.WARNING, logger="kiro_crew.agent"):
            # Must not raise.
            _apply_user_kiro_hooks(config, mc_cfg)

        assert config["hooks"] == {}
        # Exactly one SEL call for the rejection.
        assert len(sel_calls) == 1, (
            f"expected exactly one _sel_hook_rejected on OSError resolve; "
            f"got {len(sel_calls)}: {sel_calls!r}"
        )
        event_tag, _command, reason = sel_calls[0]
        assert event_tag == "autoimport"
        # Reason currently says "outside HOME or sensitive" which is
        # broadly accurate (resolved=None does land in that branch),
        # but a future refinement may split OSError into its own
        # reason string -- either shape is acceptable here.
        assert (
            "kiro_hooks_dir" in reason.lower()
            or "hooks_dir" in reason.lower()
            or "home" in reason.lower()
            or "sensitive" in reason.lower()
        )
        # A warning mentioning "rejected" must be logged.
        assert any("rejected" in rec.message.lower() for rec in caplog.records)


class TestRefreshDynamicFieldsStripsStaleUrl:
    """Managed servers are stdio-only; a stale url from an old build must be
    removed on refresh so it can't propagate into the CC config."""

    def test_stale_url_and_headers_removed_from_managed_entry(self):
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "mcpServers": {
                "kirocrew-core": {
                    "url": "http://localhost:5476/api/mcp/core",
                    "headers": {"X-Stale": "1"},
                },
                "kirocrew-cron": {"url": "http://localhost:5476/api/mcp/cron"},
            }
        }
        _refresh_dynamic_fields(config)
        for name, args in (("kirocrew-core", ["mcp-core"]), ("kirocrew-cron", ["mcp-cron"])):
            entry = config["mcpServers"][name]
            assert "url" not in entry, f"{name} still has stale url"
            assert "headers" not in entry, f"{name} still has stale headers"
            assert entry["command"]
            assert entry["args"] == args

    def test_non_managed_server_url_preserved(self):
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "mcpServers": {
                "deepwiki": {"url": "https://mcp.deepwiki.com/mcp"},
            }
        }
        _refresh_dynamic_fields(config)
        # A genuine remote server is not a managed one — its url must survive.
        assert config["mcpServers"]["deepwiki"]["url"] == "https://mcp.deepwiki.com/mcp"

    def test_refresh_strips_legacy_denied_commands(self):
        # Upgrade path: an existing config injected by an older build carries a
        # stale toolsSettings.deniedCommands + autoAllowReadonly that kiro-cli
        # would keep enforcing ahead of the hooks gate. The refresh must remove
        # them so upgraded installs behave like a fresh (hooks-gate-only) one.
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "toolsSettings": {
                "execute_bash": {
                    "autoAllowReadonly": True,
                    "deniedCommands": ["aws .* delete-.*", "rm -rf /.*"],
                },
                "shell": {"deniedCommands": ["rm -rf /.*"]},
            }
        }
        _refresh_dynamic_fields(config)
        # The empty scaffolding is removed entirely.
        assert "toolsSettings" not in config

    def test_refresh_preserves_other_tools_settings(self):
        # Only the retired keys are stripped — a user-authored sibling stays.
        from kiro_crew.agent import _refresh_dynamic_fields

        config = {
            "toolsSettings": {
                "execute_bash": {
                    "deniedCommands": ["rm -rf /.*"],
                    "allowedCommands": ["ls", "cat"],
                }
            }
        }
        _refresh_dynamic_fields(config)
        assert "deniedCommands" not in config["toolsSettings"]["execute_bash"]
        assert config["toolsSettings"]["execute_bash"]["allowedCommands"] == ["ls", "cat"]


class TestMigrateAgentSpecs:
    """migrate_agent_specs lifts KiroCrew bookkeeping keys into the sidecar."""

    def test_strips_and_lifts_keys(self, tmp_path: Path):
        kiro = tmp_path / "kiro_agents"
        kiro.mkdir()
        (kiro / "a.json").write_text(
            json.dumps({"name": "alpha", "model": "m", "model_managed": False})
        )
        (kiro / "b.json").write_text(json.dumps({"name": "beta", "cc_model": "claude-sonnet-4.6"}))
        (kiro / "c.json").write_text(json.dumps({"name": "gamma", "model": "m"}))
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro):
            cleaned = migrate_agent_specs()
        assert cleaned == 2
        assert "model_managed" not in json.loads((kiro / "a.json").read_text(encoding="utf-8"))
        assert "cc_model" not in json.loads((kiro / "b.json").read_text(encoding="utf-8"))
        assert agent_state.get_model_managed("alpha") is False
        assert agent_state.get_cc_model("beta") == "claude-sonnet-4.6"

    def test_idempotent(self, tmp_path: Path):
        kiro = tmp_path / "kiro_agents"
        kiro.mkdir()
        (kiro / "a.json").write_text(json.dumps({"name": "alpha", "model_managed": True}))
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro):
            assert migrate_agent_specs() == 1
            assert migrate_agent_specs() == 0

    def test_does_not_clobber_authoritative_sidecar(self, tmp_path: Path):
        agent_state.set_model_managed("alpha", False)
        kiro = tmp_path / "kiro_agents"
        kiro.mkdir()
        (kiro / "a.json").write_text(json.dumps({"name": "alpha", "model_managed": True}))
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", kiro):
            migrate_agent_specs()
        assert agent_state.get_model_managed("alpha") is False

    def test_installed_spec_is_schema_clean(self, tmp_path: Path):
        """After install the written spec carries no KiroCrew bookkeeping keys."""
        cfg_dir = _bundled_defaults(tmp_path)
        path = _run_install(tmp_path, cfg_dir)
        config = json.loads(path.read_text(encoding="utf-8"))
        assert "model_managed" not in config
        assert "cc_model" not in config


# ---------------------------------------------------------------------------
# MCP merge: CC-global vs Kiro-global priority + resolution-aware fallback
# (regression coverage for the builder-mcp shadowing bug — a bare/unresolvable
# command in the higher-priority source must not shadow a resolvable command
# in a lower-priority source, and Kiro-global outranks CC-global.)
# ---------------------------------------------------------------------------


def _make_exec(tmp_path: Path, name: str) -> str:
    """Create a real executable file and return its absolute path."""
    p = tmp_path / "bin" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("#!/bin/sh\n")
    p.chmod(0o755)
    return str(p)


def _run_install_mcp_merge(
    tmp_path: Path,
    cfg_dir: Path,
    *,
    cc_servers: dict,
    kiro_servers: dict,
    kirocrew_servers: dict | None = None,
    which_side_effect=lambda c, **kw: c,
) -> dict:
    """Run install_agent with CC-global and Kiro-global mcp.json seeded and a
    customizable shutil.which. Returns the parsed kirocrew.json config."""
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    prompt = cfg_dir / "prompt.md"
    mc_config = tmp_path / "empty_mc_config.json"
    if not mc_config.exists():
        mc_config.write_text(json.dumps({"agent": {"kiro_hooks_autoimport": False}}))
    kiro_mcp = tmp_path / "fake_kiro_mcp.json"
    cc_mcp = tmp_path / "fake_cc_mcp.json"
    kiro_mcp.write_text(json.dumps({"mcpServers": kiro_servers}))
    cc_mcp.write_text(json.dumps({"mcpServers": cc_servers}))
    if kirocrew_servers is not None:
        kc_home = tmp_path / "kirocrew_home"
        kc_home.mkdir(parents=True, exist_ok=True)
        (kc_home / "mcp.json").write_text(json.dumps({"mcpServers": kirocrew_servers}))

    _user_home = tmp_path / "kirocrew_home"
    patches = [
        patch.multiple(
            "kiro_crew.agent",
            KIRO_AGENTS_DIR=kiro_dir,
            _BUNDLED_CFG_DIR=cfg_dir,
            _KIROCREW_BIN="/usr/bin/kirocrew",
            _MANAGED_MCP_SERVERS=_DEFAULT_MANAGED_MCPS,
            _KIRO_MCP_JSON=kiro_mcp,
            _CC_MCP_JSON=cc_mcp,
        ),
        patch("kiro_crew.agent._user_dir", lambda: _user_home),
        patch("kiro_crew.agent._prompt_path", return_value=prompt),
        patch("kiro_crew.agent._shipped_defaults", return_value=cfg_dir / "defaults.json"),
        patch("kiro_crew.agent._project_dir", return_value=None),
        patch("kiro_crew.agent._aim_skill_paths", return_value=[]),
        patch("kiro_crew.agent.shutil.which", side_effect=which_side_effect),
        patch("kiro_crew.agent._mc_config_path", return_value=mc_config),
        # A companion contributes the Claude Code scope via the CPP seam — the
        # core no longer reads ~/.claude.json directly at rebuild time (OSS is
        # Kiro-only). Point the seam at cc_mcp so these merge-priority tests
        # exercise the seam-routed provider-global merge.
        patch("kiro_crew.agent._extra_mcp_scope_globals", return_value=[cc_mcp]),
    ]
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        path = install_agent()
    return json.loads(path.read_text(encoding="utf-8"))


class TestRebuildReconcileRetainsEnabledAppServers:
    """The final kirocrew.json reconcile must not delete an ENABLED app's
    manifest-derived MCP server just because it is absent from on-disk — a clean
    rebuild (or a missing/empty config) starts with an empty on_disk, and the
    app's tools would vanish. It must drop a server only when its app is
    confirmed no longer enabled (a concurrent deregister).

    Pinned by source inspection: the reconcile is an inline block in
    ``install_agent`` gated on ``is_kirocrew_json`` (the written path equalling
    ``bridges._mcp_json_path()``), which the merge-priority harness does not
    reproduce — so the guarantee is asserted structurally.
    """

    def test_reconcile_drops_by_enabled_state_not_ondisk_absence(self) -> None:
        import inspect

        from kiro_crew import agent

        src = inspect.getsource(agent.install_agent)
        # The drop must be gated on the app being DISABLED (deregistered), not on
        # mere absence from on_disk — else a clean rebuild with an empty on_disk
        # would delete an enabled app's manifest-derived server.
        assert "_app_of_key_enabled" in src
        assert "if not _app_of_key_enabled(_k):" in src
        assert "is_app_enabled" in src
        # The old, buggy condition (delete when absent from on_disk) must be gone.
        assert "if _k not in on_disk_app:" not in src


class TestMcpMergePriority:
    def test_kiro_global_outranks_cc_global(self, tmp_path: Path):
        """Same server in both globals → Kiro-global command wins (CC down-ranked)."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_cmd = _make_exec(tmp_path, "kiro-srv")
        cc_cmd = _make_exec(tmp_path, "cc-srv")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={"shared-srv": {"command": cc_cmd}},
            kiro_servers={"shared-srv": {"command": kiro_cmd}},
        )
        assert config["mcpServers"]["shared-srv"]["command"] == kiro_cmd

    def test_unresolvable_cc_does_not_shadow_resolvable_kiro(self, tmp_path: Path):
        """CC bare/unresolvable command must not shadow Kiro's resolvable absolute path."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_cmd = _make_exec(tmp_path, "real-srv")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={"srv": {"command": "bare-builder-mcp"}},
            kiro_servers={"srv": {"command": kiro_cmd}},
            which_side_effect=lambda c, **kw: None if c == "bare-builder-mcp" else c,
        )
        assert "srv" in config["mcpServers"], "server was dropped (shadowed by bare CC command)"
        assert config["mcpServers"]["srv"]["command"] == kiro_cmd

    def test_fallback_to_resolvable_lower_source(self, tmp_path: Path):
        """If the winning source's command is unresolvable, fall back to a
        resolvable command from another source instead of dropping the server."""
        cfg_dir = _bundled_defaults(tmp_path)
        cc_cmd = _make_exec(tmp_path, "cc-real")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={"srv": {"command": cc_cmd}},
            kiro_servers={"srv": {"command": "bare-kiro"}},
            which_side_effect=lambda c, **kw: None if c == "bare-kiro" else c,
        )
        assert "srv" in config["mcpServers"], "server dropped instead of falling back"
        assert config["mcpServers"]["srv"]["command"] == cc_cmd

    def test_fallback_adopts_source_args_env_unit(self, tmp_path: Path):
        """On cross-source fallback, the resolving source's command/args/env
        are adopted as a unit — the winner's stale args/env must not leak in,
        but non-command fields (autoApprove) are preserved."""
        cfg_dir = _bundled_defaults(tmp_path)
        cc_cmd = _make_exec(tmp_path, "cc-real")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            # CC (lower priority) is the resolvable one and carries its own args.
            cc_servers={"srv": {"command": cc_cmd, "args": ["--cc"], "env": {"CC": "1"}}},
            # Kiro wins priority but is unresolvable and has different args +
            # a non-command field (autoApprove) that should survive.
            kiro_servers={
                "srv": {
                    "command": "bare-kiro",
                    "args": ["--kiro"],
                    "autoApprove": ["tool"],
                }
            },
            which_side_effect=lambda c, **kw: None if c == "bare-kiro" else c,
        )
        srv = config["mcpServers"]["srv"]
        assert srv["command"] == cc_cmd
        assert srv["args"] == ["--cc"]  # adopted from the resolving source
        assert srv.get("env") == {"CC": "1"}  # adopted from the resolving source
        assert srv.get("autoApprove") == ["tool"]  # winner's non-command field kept

    def test_server_without_command_is_dropped(self, tmp_path: Path):
        """A server with no command in any source is dropped (no crash)."""
        cfg_dir = _bundled_defaults(tmp_path)
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"srv": {"args": ["--x"]}},  # no command anywhere
        )
        assert "srv" not in config["mcpServers"]

    def test_kirocrew_override_does_not_corrupt_global_fallback(self, tmp_path: Path):
        """Regression (review-bot): a kirocrew mcp.json override carrying an
        unresolvable command must not mutate the shared kiro-global source dict
        that is reused as a fallback candidate. The resolvable kiro-global
        command must still be recovered via the fallback (server not dropped)."""
        cfg_dir = _bundled_defaults(tmp_path)
        kiro_cmd = _make_exec(tmp_path, "kiro-real")
        config = _run_install_mcp_merge(
            tmp_path,
            cfg_dir,
            cc_servers={},
            kiro_servers={"srv": {"command": kiro_cmd}},
            kirocrew_servers={"srv": {"command": "bare-unresolvable"}},
            which_side_effect=lambda c, **kw: None if c == "bare-unresolvable" else c,
        )
        assert (
            "srv" in config["mcpServers"]
        ), "server dropped: global source dict corrupted by override"
        assert config["mcpServers"]["srv"]["command"] == kiro_cmd


class TestRefreshDynamicFieldsSyncsConfigModel:
    """config.json agent.model must propagate into the kiro agent file so
    kiro-cli's --agent startup load matches it."""

    def _write_mc_config(self, tmp_path: Path, model) -> Path:
        mc = tmp_path / "config.json"
        body = {} if model is None else {"agent": {"model": model}}
        mc.write_text(json.dumps(body), encoding="utf-8")
        return mc

    def test_explicit_config_model_overrides_managed_default(self, tmp_path: Path):
        from kiro_crew.agent import _refresh_dynamic_fields

        # model_managed=True would re-sync the agent file from the shipped
        # default; an explicit config.json pick must still win.
        agent_state.set_model_managed("kirocrew", True)
        mc = self._write_mc_config(tmp_path, "claude-opus-4.8")
        config = {"name": "kirocrew", "model": "stale-from-install"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        assert config["model"] == "claude-opus-4.8"

    def test_auto_sentinel_does_not_clobber_agent_model(self, tmp_path: Path):
        from kiro_crew.agent import _refresh_dynamic_fields

        # "auto" defers to managed/shipped resolution; it must not overwrite
        # the existing agent-file model with the literal "auto".
        mc = self._write_mc_config(tmp_path, "auto")
        config = {"name": "kirocrew", "model": "claude-haiku-4.5"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        assert config["model"] == "claude-haiku-4.5"

    def test_no_config_model_leaves_agent_model_untouched(self, tmp_path: Path):
        from kiro_crew.agent import _refresh_dynamic_fields

        mc = self._write_mc_config(tmp_path, None)
        config = {"name": "kirocrew", "model": "claude-sonnet-4.6"}
        with patch("kiro_crew.agent._mc_config_path", return_value=mc):
            _refresh_dynamic_fields(config)
        assert config["model"] == "claude-sonnet-4.6"


# ── ensure_agent_materialized (self-heal for kiro-cli "Mode not found") ──


def test_ensure_agent_materialized_noop_for_non_managed_agent(tmp_path, monkeypatch):
    """A non-managed (app/custom) agent can't be regenerated here → returns
    False and never touches rebuild_agent_config."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)
    rebuild = unittest.mock.MagicMock()
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    assert agent_mod.ensure_agent_materialized("some-app-agent") is False
    rebuild.assert_not_called()


def test_ensure_agent_materialized_present_is_noop(tmp_path, monkeypatch):
    """Managed default already on disk → True, no regeneration."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)
    (tmp_path / agent_mod.AGENT_FILENAME).write_text("{}", encoding="utf-8")
    rebuild = unittest.mock.MagicMock()
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    managed = Path(agent_mod.AGENT_FILENAME).stem
    assert agent_mod.ensure_agent_materialized(managed) is True
    rebuild.assert_not_called()


def test_ensure_agent_materialized_regenerates_when_missing(tmp_path, monkeypatch):
    """Managed default missing → rebuild_agent_config is invoked and the file
    is materialized (the reporter's fresh-checkout case)."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)

    def _fake_rebuild(*_a, **_k):
        path = tmp_path / agent_mod.AGENT_FILENAME
        path.write_text("{}", encoding="utf-8")
        return path

    rebuild = unittest.mock.MagicMock(side_effect=_fake_rebuild)
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    managed = Path(agent_mod.AGENT_FILENAME).stem
    assert agent_mod.ensure_agent_materialized(managed) is True
    rebuild.assert_called_once()


def test_ensure_agent_materialized_swallows_errors(tmp_path, monkeypatch):
    """Best-effort: a rebuild failure never propagates (it sits on the spawn
    hot path) — returns False instead."""
    import kiro_crew.agent as agent_mod

    monkeypatch.setattr(agent_mod, "kiro_agents_dir_path", lambda: tmp_path)
    rebuild = unittest.mock.MagicMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)

    managed = Path(agent_mod.AGENT_FILENAME).stem
    assert agent_mod.ensure_agent_materialized(managed) is False
