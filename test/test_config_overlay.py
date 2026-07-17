"""Tests for config.local.json overlay mechanism (Mesh-1536)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from kiro_crew.config.loader import (
    KiroCrewConfig,
    _deep_merge,
    _subtract_overlay,
    config_local_path,
)


class TestDeepMerge:
    """Unit tests for the _deep_merge helper."""

    def test_flat_override(self) -> None:
        base = {"a": 1, "b": 2}
        overlay = {"b": 99}
        assert _deep_merge(base, overlay) == {"a": 1, "b": 99}

    def test_nested_merge(self) -> None:
        base = {"agent": {"model": "auto", "yolo": False}, "timezone": ""}
        overlay = {"agent": {"yolo": True}}
        result = _deep_merge(base, overlay)
        assert result["agent"]["yolo"] is True
        assert result["agent"]["model"] == "auto"
        assert result["timezone"] == ""

    def test_overlay_adds_new_keys(self) -> None:
        base = {"a": 1}
        overlay = {"b": 2, "c": {"nested": True}}
        result = _deep_merge(base, overlay)
        assert result == {"a": 1, "b": 2, "c": {"nested": True}}

    def test_overlay_replaces_non_dict_with_dict(self) -> None:
        base = {"a": "string_value"}
        overlay = {"a": {"nested": True}}
        result = _deep_merge(base, overlay)
        assert result == {"a": {"nested": True}}

    def test_overlay_replaces_dict_with_scalar(self) -> None:
        base = {"a": {"nested": True}}
        overlay = {"a": "replaced"}
        result = _deep_merge(base, overlay)
        assert result == {"a": "replaced"}

    def test_empty_overlay(self) -> None:
        base = {"a": 1, "b": {"c": 2}}
        assert _deep_merge(base, {}) == base

    def test_empty_base(self) -> None:
        overlay = {"a": 1}
        assert _deep_merge({}, overlay) == {"a": 1}

    def test_does_not_mutate_base(self) -> None:
        base = {"a": {"b": 1}}
        overlay = {"a": {"b": 2}}
        _deep_merge(base, overlay)
        assert base["a"]["b"] == 1

    def test_deeply_nested(self) -> None:
        base = {"l1": {"l2": {"l3": {"value": "original", "keep": True}}}}
        overlay = {"l1": {"l2": {"l3": {"value": "overridden"}}}}
        result = _deep_merge(base, overlay)
        assert result["l1"]["l2"]["l3"]["value"] == "overridden"
        assert result["l1"]["l2"]["l3"]["keep"] is True


class TestConfigOverlayLoad:
    """Integration tests for config.local.json merging during load()."""

    def test_local_overlay_merges_on_load(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base_config = {
            "agent": {"yolo": False, "model": "opus", "provider": "acp"},
        }
        (config_dir / "config.json").write_text(json.dumps(base_config))
        local_config = {"agent": {"yolo": True, "model": "auto"}}
        (config_dir / "config.local.json").write_text(json.dumps(local_config))

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()

        assert cfg.agent.yolo is True
        assert cfg.agent.model == "auto"
        assert cfg.agent.provider == "acp"

    def test_load_without_local_file(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base_config = {"agent": {"yolo": False}}
        (config_dir / "config.json").write_text(json.dumps(base_config))

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()

        assert cfg.agent.yolo is False

    def test_invalid_local_json_ignored(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base_config = {"agent": {"yolo": False}}
        (config_dir / "config.json").write_text(json.dumps(base_config))
        (config_dir / "config.local.json").write_text("not valid json {{{")

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()

        assert cfg.agent.yolo is False

    def test_non_dict_local_json_ignored(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base_config = {"agent": {"yolo": False}}
        (config_dir / "config.json").write_text(json.dumps(base_config))
        (config_dir / "config.local.json").write_text('"just a string"')

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()

        assert cfg.agent.yolo is False

    def test_local_overlay_adds_new_section(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base_config = {"agent": {"provider": "acp"}}
        (config_dir / "config.json").write_text(json.dumps(base_config))
        local_config = {"dashboard": {"auto_open_browser": False}}
        (config_dir / "config.local.json").write_text(json.dumps(local_config))

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()

        assert cfg.dashboard.auto_open_browser is False

    def test_overlay_applies_when_config_json_missing(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        local_config = {"agent": {"yolo": True, "provider": "acp"}}
        (config_dir / "config.local.json").write_text(json.dumps(local_config))

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()

        assert cfg.agent.yolo is True
        assert cfg.agent.provider == "acp"

    def test_overlay_applies_when_config_json_invalid(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text("not json {{{")
        local_config = {"agent": {"yolo": True}}
        (config_dir / "config.local.json").write_text(json.dumps(local_config))

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()

        assert cfg.agent.yolo is True

    def test_save_does_not_leak_overlay_into_config_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base_config = {"agent": {"yolo": False, "provider": "acp"}}
        (config_dir / "config.json").write_text(json.dumps(base_config))
        local_config = {"agent": {"yolo": True, "model": "auto"}}
        (config_dir / "config.local.json").write_text(json.dumps(local_config))

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()
            assert cfg.agent.yolo is True
            assert cfg.agent.model == "auto"
            cfg.save()
            saved = json.loads((config_dir / "config.json").read_text())

        assert "yolo" not in saved.get("agent", {})
        assert "model" not in saved.get("agent", {})

    def test_load_warns_when_config_json_is_non_dict(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        (config_dir / "config.json").write_text("[1, 2, 3]")

        with patch("kiro_crew.config.loader.config_dir", return_value=config_dir):
            cfg = KiroCrewConfig.load()

        assert cfg.agent.yolo is False

    def test_world_writable_warning_fires(self, tmp_path: Path, caplog) -> None:
        import logging
        import os

        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base = {"agent": {"provider": "acp"}}
        (config_dir / "config.json").write_text(json.dumps(base))
        local_file = config_dir / "config.local.json"
        local_file.write_text(json.dumps({"agent": {"yolo": True}}))
        os.chmod(local_file, 0o666)

        with (
            patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
            caplog.at_level(logging.WARNING, logger="kiro_crew.config.loader"),
        ):
            cfg = KiroCrewConfig.load()

        assert cfg.agent.yolo is True
        assert "world-writable" in caplog.text


class TestSubtractOverlay:
    """Unit tests for the _subtract_overlay helper."""

    def test_removes_matching_leaf_values(self) -> None:
        merged = {"a": 1, "b": 2, "c": 3}
        overlay = {"b": 2}
        assert _subtract_overlay(merged, overlay) == {"a": 1, "c": 3}

    def test_keeps_values_that_differ_from_overlay(self) -> None:
        merged = {"a": 1, "b": 99}
        overlay = {"b": 2}
        assert _subtract_overlay(merged, overlay) == {"a": 1, "b": 99}

    def test_nested_subtraction(self) -> None:
        merged = {"agent": {"yolo": True, "model": "auto"}, "timezone": ""}
        overlay = {"agent": {"yolo": True}}
        result = _subtract_overlay(merged, overlay)
        assert result["agent"] == {"model": "auto"}
        assert result["timezone"] == ""

    def test_removes_empty_parent_after_subtraction(self) -> None:
        merged = {"agent": {"yolo": True}, "other": 1}
        overlay = {"agent": {"yolo": True}}
        result = _subtract_overlay(merged, overlay)
        assert "agent" not in result
        assert result["other"] == 1

    def test_skips_keys_not_in_merged(self) -> None:
        merged = {"a": 1}
        overlay = {"b": 2, "c": 3}
        result = _subtract_overlay(merged, overlay)
        assert result == {"a": 1}


class TestConfigLocalPath:
    """Tests for config_local_path() function."""

    def test_returns_path_in_config_dir(self, tmp_path: Path) -> None:
        with patch("kiro_crew.config.loader.config_dir", return_value=tmp_path):
            assert config_local_path() == tmp_path / "config.local.json"


class TestDictSetCreate:
    """Tests for _dict_set_create helper in cli_config."""

    def test_creates_intermediate_dicts(self) -> None:
        from kiro_crew.cli_config import _dict_set_create

        d: dict = {}
        _dict_set_create(d, "agent.yolo", True)
        assert d == {"agent": {"yolo": True}}

    def test_creates_deeply_nested(self) -> None:
        from kiro_crew.cli_config import _dict_set_create

        d: dict = {}
        _dict_set_create(d, "a.b.c.d", 42)
        assert d == {"a": {"b": {"c": {"d": 42}}}}

    def test_overwrites_existing_value(self) -> None:
        from kiro_crew.cli_config import _dict_set_create

        d = {"agent": {"yolo": False}}
        _dict_set_create(d, "agent.yolo", True)
        assert d["agent"]["yolo"] is True

    def test_replaces_non_dict_intermediate(self) -> None:
        from kiro_crew.cli_config import _dict_set_create

        d = {"agent": "not_a_dict"}
        _dict_set_create(d, "agent.yolo", True)
        assert d == {"agent": {"yolo": True}}


class TestCliConfigSetLocal:
    """Tests for the --local config set path."""

    def test_local_set_writes_to_config_local_json(self, tmp_path: Path) -> None:
        import argparse

        from kiro_crew.cli_config import _config_cmd

        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()

        args = argparse.Namespace(
            config_action="set", key="agent.yolo", value="true", file=None, local=True
        )

        with patch("kiro_crew.cli_config.config_local_path", return_value=config_dir / "config.local.json"):
            with patch("kiro_crew.cli_config.sel"):
                _config_cmd(args)

        local_file = config_dir / "config.local.json"
        assert local_file.exists()
        data = json.loads(local_file.read_text())
        assert data["agent"]["yolo"] is True

    def test_local_set_unknown_section_warns(self, tmp_path: Path, capsys) -> None:
        import argparse

        from kiro_crew.cli_config import _config_cmd

        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()

        args = argparse.Namespace(
            config_action="set", key="bogus.key", value="hello", file=None, local=True
        )

        with patch("kiro_crew.cli_config.config_local_path", return_value=config_dir / "config.local.json"):
            with patch("kiro_crew.cli_config.sel"):
                _config_cmd(args)

        captured = capsys.readouterr()
        assert "not a recognized config section" in captured.err

    def test_nonlocal_set_subtracts_overlay(self, tmp_path: Path) -> None:
        import argparse

        from kiro_crew.cli_config import _config_cmd

        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base = {"agent": {"yolo": False, "streaming": True, "provider": "acp"}}
        (config_dir / "config.json").write_text(json.dumps(base))
        local = {"agent": {"streaming": True}}
        (config_dir / "config.local.json").write_text(json.dumps(local))

        args = argparse.Namespace(
            config_action="set", key="agent.yolo", value="true", file=None, local=False
        )

        with (
            patch("kiro_crew.cli_config.config_path", return_value=config_dir / "config.json"),
            patch("kiro_crew.cli_config.config_local_path", return_value=config_dir / "config.local.json"),
            patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
            patch("kiro_crew.cli_config.sel"),
        ):
            _config_cmd(args)

        saved = json.loads((config_dir / "config.json").read_text())
        assert saved["agent"]["yolo"] is True
        assert "streaming" not in saved.get("agent", {})

    def test_local_set_handles_corrupt_existing_file(self, tmp_path: Path) -> None:
        import argparse

        from kiro_crew.cli_config import _config_cmd

        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        (config_dir / "config.local.json").write_text("not json {{{")

        args = argparse.Namespace(
            config_action="set", key="agent.yolo", value="true", file=None, local=True
        )

        with patch("kiro_crew.cli_config.config_local_path", return_value=config_dir / "config.local.json"):
            with patch("kiro_crew.cli_config.sel"):
                _config_cmd(args)

        data = json.loads((config_dir / "config.local.json").read_text())
        assert data["agent"]["yolo"] is True

    def test_local_set_handles_non_dict_existing_file(self, tmp_path: Path) -> None:
        import argparse

        from kiro_crew.cli_config import _config_cmd

        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        (config_dir / "config.local.json").write_text('"just a string"')

        args = argparse.Namespace(
            config_action="set", key="agent.yolo", value="true", file=None, local=True
        )

        with patch("kiro_crew.cli_config.config_local_path", return_value=config_dir / "config.local.json"):
            with patch("kiro_crew.cli_config.sel"):
                _config_cmd(args)

        data = json.loads((config_dir / "config.local.json").read_text())
        assert data["agent"]["yolo"] is True

    def test_nonlocal_set_handles_corrupt_local_file(self, tmp_path: Path) -> None:
        import argparse

        from kiro_crew.cli_config import _config_cmd

        config_dir = tmp_path / ".kirocrew"
        config_dir.mkdir()
        base = {"agent": {"yolo": False, "provider": "acp"}}
        (config_dir / "config.json").write_text(json.dumps(base))
        (config_dir / "config.local.json").write_text("broken {{{")

        args = argparse.Namespace(
            config_action="set", key="agent.yolo", value="true", file=None, local=False
        )

        with (
            patch("kiro_crew.cli_config.config_path", return_value=config_dir / "config.json"),
            patch("kiro_crew.cli_config.config_local_path", return_value=config_dir / "config.local.json"),
            patch("kiro_crew.config.loader.config_dir", return_value=config_dir),
            patch("kiro_crew.cli_config.sel"),
        ):
            _config_cmd(args)

        saved = json.loads((config_dir / "config.json").read_text())
        assert saved["agent"]["yolo"] is True
