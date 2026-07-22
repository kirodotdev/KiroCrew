"""Tests for _enforce_denied_commands scope config."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from kiro_crew.agent import _denied_cmd_mtimes, _enforce_denied_commands, _load_json

# Non-UTF-8 bytes (a macOS AppleDouble "._foo.json" stub) — exercises.
_APPLEDOUBLE_BYTES = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X\xa3\xff\xfe" + b"\x00" * 32


def _setup(tmp_path: Path, scope: str = "all"):
    """Create bundled defaults and agent configs for testing."""
    bundled_dir = tmp_path / "bundled"
    bundled_dir.mkdir()
    defaults = {
        "toolsSettings": {
            "execute_bash": {"deniedCommands": ["rm -rf /", "git push --force"]},
            "shell": {"deniedCommands": ["rm -rf /", "git push --force"]},
        }
    }
    (bundled_dir / "defaults.json").write_text(json.dumps(defaults))

    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "kirocrew.json").write_text(json.dumps({"model": "claude"}))
    (agents_dir / "other-agent.json").write_text(json.dumps({"model": "claude"}))
    (agents_dir / "kirocrew-lite.json").write_text(json.dumps({"model": "lite"}))

    mock_cfg = MagicMock()
    mock_cfg.agent.enforce_denied_commands = scope

    return bundled_dir, agents_dir, mock_cfg


class TestEnforceDeniedScope:
    def setup_method(self):
        _denied_cmd_mtimes.clear()

    def test_scope_all_enforces_on_all_agents(self, tmp_path: Path):
        bundled_dir, agents_dir, mock_cfg = _setup(tmp_path, "all")

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=mock_cfg),
        ):
            _enforce_denied_commands()

        for name in ("kirocrew.json", "other-agent.json"):
            data = json.loads((agents_dir / name).read_text())
            assert "rm -rf /" in data["toolsSettings"]["execute_bash"]["deniedCommands"]

    def test_scope_kirocrew_skips_other_agents(self, tmp_path: Path):
        bundled_dir, agents_dir, mock_cfg = _setup(tmp_path, "kirocrew")

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=mock_cfg),
        ):
            _enforce_denied_commands()

        mc = json.loads((agents_dir / "kirocrew.json").read_text())
        assert "rm -rf /" in mc["toolsSettings"]["execute_bash"]["deniedCommands"]

        other = json.loads((agents_dir / "other-agent.json").read_text())
        assert "toolsSettings" not in other

    def test_lite_agents_always_skipped(self, tmp_path: Path):
        """Lite agents are skipped regardless of scope."""
        bundled_dir, agents_dir, mock_cfg = _setup(tmp_path, "all")

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.agent._LITE_AGENT_NAMES", frozenset({"kirocrew-lite.json"})),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=mock_cfg),
        ):
            _enforce_denied_commands()

        lite = json.loads((agents_dir / "kirocrew-lite.json").read_text())
        assert "toolsSettings" not in lite

    def test_config_load_failure_falls_back_to_all(self, tmp_path: Path):
        bundled_dir, agents_dir, _ = _setup(tmp_path)

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.config.KiroCrewConfig.load", side_effect=Exception("boom")),
        ):
            _enforce_denied_commands()

        for name in ("kirocrew.json", "other-agent.json"):
            data = json.loads((agents_dir / name).read_text())
            assert "rm -rf /" in data["toolsSettings"]["execute_bash"]["deniedCommands"]

    def test_non_utf8_agent_file_does_not_crash(self, tmp_path: Path):
        """A non-UTF-8 *.json file (e.g. a macOS AppleDouble "._foo.json"
        resource-fork stub) must be skipped, not crash gateway startup.

        Regression for read_text raises UnicodeDecodeError (a
        ValueError, not an OSError), which escaped the old
        (json.JSONDecodeError, OSError) except clause.
        """
        bundled_dir, agents_dir, mock_cfg = _setup(tmp_path, "all")
        # Drop a binary AppleDouble stub alongside the valid configs.
        (agents_dir / "._kirocrew.json").write_bytes(_APPLEDOUBLE_BYTES)

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=mock_cfg),
        ):
            # Must not raise UnicodeDecodeError.
            _enforce_denied_commands()

        # The binary stub is left untouched (not rewritten as JSON).
        assert (agents_dir / "._kirocrew.json").read_bytes() == _APPLEDOUBLE_BYTES
        # Valid sibling configs are still enforced despite the bad file.
        for name in ("kirocrew.json", "other-agent.json"):
            data = json.loads((agents_dir / name).read_text())
            assert "rm -rf /" in data["toolsSettings"]["execute_bash"]["deniedCommands"]

    def test_load_json_returns_empty_on_non_utf8(self, tmp_path: Path):
        """_load_json (the central loader used by _sanitize_agent_hooks and
        many other startup-repair paths) returns {} for a non-UTF-8 file
        instead of raising UnicodeDecodeError."""
        bad = tmp_path / "._agent.json"
        bad.write_bytes(_APPLEDOUBLE_BYTES)

        assert _load_json(bad) == {}

    def test_non_object_root_agent_file_does_not_abort_enforcement(self, tmp_path: Path):
        """A valid-JSON-but-non-object root ("[]", 42, "str") must be skipped,
        not abort enforcement for every agent iterated after it.

        The 90e3cccc fix added UnicodeDecodeError to the except clause, but a
        non-object root raises AttributeError on data.setdefault() — which is
        NOT in that tuple — so it escaped the per-file loop and silently left
        deniedCommands (a security control) un-refreshed on all later agents.
        """
        bundled_dir, agents_dir, mock_cfg = _setup(tmp_path, "all")
        # A "._"-prefixed name sorts before the real configs, so pre-fix the
        # AttributeError it raises aborts the loop before they are enforced.
        (agents_dir / "._list_root.json").write_text("[]")
        (agents_dir / "._scalar_root.json").write_text("42")

        with (
            patch("kiro_crew.agent._BUNDLED_CFG_DIR", bundled_dir),
            patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir),
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=mock_cfg),
        ):
            # Must not raise AttributeError.
            _enforce_denied_commands()

        # The non-object files are left untouched (not rewritten).
        assert (agents_dir / "._list_root.json").read_text() == "[]"
        # Valid sibling configs are still enforced despite the bad files.
        for name in ("kirocrew.json", "other-agent.json"):
            data = json.loads((agents_dir / name).read_text())
            assert "rm -rf /" in data["toolsSettings"]["execute_bash"]["deniedCommands"]
