"""Tests for CC plugin discovery — aim_agents.py CC functions.

The optional AIM plugin manager is not part of the public distribution, so the
install/sync helpers (``_ensure_standalone_mode``, ``install_cc_plugin``,
``installed_kiro_packages_missing_from_cc``) degrade to graceful no-ops. These
tests assert the public no-op behavior. The on-disk discovery helpers
(``list_cc_plugins``, ``is_cc_plugin_installed``) remain fully functional and
are covered with real fixtures.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.aim_agents import (
    _ensure_standalone_mode,
    install_cc_plugin,
    installed_kiro_packages_missing_from_cc,
    is_cc_plugin_installed,
    list_cc_plugins,
)


class TestListCcPlugins:
    """Tests for list_cc_plugins()."""

    def test_returns_empty_when_marketplace_missing(self, tmp_path: Path, monkeypatch):
        """Returns [] when marketplace.json does not exist."""
        monkeypatch.setattr(
            "kiro_crew.aim_agents._CC_PLUGINS_DIR", tmp_path / "cc-plugins"
        )
        assert list_cc_plugins() == []

    def test_parses_array_format(self, tmp_path: Path, monkeypatch):
        """Parses marketplace.json as array of {packageName: ...}."""
        plugins_dir = tmp_path / "cc-plugins" / ".claude-plugin"
        plugins_dir.mkdir(parents=True)
        data = [
            {"packageName": "PkgA", "version": "1.0"},
            {"packageName": "PkgB", "version": "2.0"},
        ]
        (plugins_dir / "marketplace.json").write_text(json.dumps(data))
        monkeypatch.setattr("kiro_crew.aim_agents._CC_PLUGINS_DIR", tmp_path / "cc-plugins")
        assert list_cc_plugins() == ["PkgA", "PkgB"]

    def test_parses_dict_format_with_plugins_key(self, tmp_path: Path, monkeypatch):
        """Parses marketplace.json as dict with 'plugins' array."""
        plugins_dir = tmp_path / "cc-plugins" / ".claude-plugin"
        plugins_dir.mkdir(parents=True)
        data = {"plugins": [{"packageName": "PkgC"}]}
        (plugins_dir / "marketplace.json").write_text(json.dumps(data))
        monkeypatch.setattr("kiro_crew.aim_agents._CC_PLUGINS_DIR", tmp_path / "cc-plugins")
        assert list_cc_plugins() == ["PkgC"]

    def test_returns_empty_on_malformed_json(self, tmp_path: Path, monkeypatch):
        """Returns [] when marketplace.json is not valid JSON."""
        plugins_dir = tmp_path / "cc-plugins" / ".claude-plugin"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "marketplace.json").write_text("not json")
        monkeypatch.setattr("kiro_crew.aim_agents._CC_PLUGINS_DIR", tmp_path / "cc-plugins")
        assert list_cc_plugins() == []

    def test_skips_entries_without_package_name(self, tmp_path: Path, monkeypatch):
        """Skips array entries missing the 'packageName' key."""
        plugins_dir = tmp_path / "cc-plugins" / ".claude-plugin"
        plugins_dir.mkdir(parents=True)
        data = [{"packageName": "Good"}, {"version": "1.0"}, {"packageName": ""}]
        (plugins_dir / "marketplace.json").write_text(json.dumps(data))
        monkeypatch.setattr("kiro_crew.aim_agents._CC_PLUGINS_DIR", tmp_path / "cc-plugins")
        assert list_cc_plugins() == ["Good"]


class TestIsCcPluginInstalled:
    """Tests for is_cc_plugin_installed()."""

    def test_returns_true_when_present(self, tmp_path: Path, monkeypatch):
        plugins_dir = tmp_path / "cc-plugins" / ".claude-plugin"
        plugins_dir.mkdir(parents=True)
        data = [{"packageName": "MyPkg"}]
        (plugins_dir / "marketplace.json").write_text(json.dumps(data))
        monkeypatch.setattr("kiro_crew.aim_agents._CC_PLUGINS_DIR", tmp_path / "cc-plugins")
        assert is_cc_plugin_installed("MyPkg") is True

    def test_returns_false_when_absent(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("kiro_crew.aim_agents._CC_PLUGINS_DIR", tmp_path / "cc-plugins")
        assert is_cc_plugin_installed("Missing") is False


class TestEnsureStandaloneMode:
    """Tests for _ensure_standalone_mode() — no-op in the public distribution."""

    def test_is_noop_returning_true(self):
        """Returns True without touching any AIM config (no-op in OSS)."""
        assert _ensure_standalone_mode() is True


class TestInstallCcPlugin:
    """Tests for install_cc_plugin() — graceful no-op in the public distribution.

    The optional AIM plugin manager binary is absent, so installs degrade to a
    no-op that returns ``(False, <message>)`` after validating the package name.
    """

    def test_rejects_invalid_package_name(self):
        """Returns error for package names with disallowed characters."""
        ok, msg = install_cc_plugin("../evil")
        assert ok is False
        assert "Invalid" in msg

    def test_returns_not_available_for_valid_package(self):
        """Returns (False, not-available) for an otherwise-valid package name."""
        ok, msg = install_cc_plugin("SomePackage")
        assert ok is False
        assert "not available" in msg

    def test_noop_ignores_standalone_flag(self):
        """The standalone kwarg is accepted but ignored; result is unchanged."""
        ok_true, _ = install_cc_plugin("TestPkg", standalone=True)
        ok_false, _ = install_cc_plugin("TestPkg", standalone=False)
        assert ok_true is False
        assert ok_false is False


class TestInstalledKiroPackagesMissingFromCc:
    """Tests for installed_kiro_packages_missing_from_cc() — empty in OSS.

    With no external plugin manager in the public distribution there is nothing
    to diff, so this always returns an empty list.
    """

    def test_returns_empty_no_op(self):
        """Always returns [] in the public distribution."""
        assert installed_kiro_packages_missing_from_cc() == []
