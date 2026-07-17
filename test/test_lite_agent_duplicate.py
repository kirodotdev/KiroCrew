"""Tests for kirocrew-lite duplicate agent prevention."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.agent import (
    _KIROCREW_AIM_PACKAGE,
    _LITE_AGENT_FILENAME,
    _install_lite_agent_fallback,
    _remove_bare_lite_if_aim_installed,
)


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    """Temporary agents directory."""
    d = tmp_path / "agents"
    d.mkdir()
    return d


AIM_LITE_FILENAME = f"{_KIROCREW_AIM_PACKAGE}-kirocrew-lite.json"


class TestLiteAgentDuplicate:
    """Prevent duplicate kirocrew-lite agent configs (bare + AIM-installed)."""

    def test_fallback_writes_even_when_legacy_aim_file_present(self, agents_dir: Path) -> None:
        """Public fallback always writes the bare config.

        On the de-Amazoned fork the AIM package manager is neutralized, so the
        fallback no longer skips when a (legacy) AIM-named file happens to be
        present — the bare ``kirocrew-lite.json`` is always written for the
        claude_code provider's cheap background agent.
        """
        aim_file = agents_dir / AIM_LITE_FILENAME
        aim_file.write_text(json.dumps({"name": "kirocrew-lite"}))

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            _install_lite_agent_fallback()

        bare = agents_dir / _LITE_AGENT_FILENAME
        assert bare.exists(), "bare fallback should always be written on public installs"

    def test_fallback_writes_when_aim_version_missing(self, agents_dir: Path) -> None:
        """Fallback should write bare file when AIM version is absent."""
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            _install_lite_agent_fallback()

        bare = agents_dir / _LITE_AGENT_FILENAME
        assert bare.exists(), "bare fallback should be written when AIM version missing"

    def test_cleanup_is_noop_when_both_exist(self, agents_dir: Path) -> None:
        """Cleanup is a no-op on public installs (no AIM package manager).

        The AIM-managed duplicate path is removed on the de-Amazoned fork, so
        there is no AIM-owned copy to deduplicate against — the cleanup leaves
        any existing files untouched.
        """
        bare = agents_dir / _LITE_AGENT_FILENAME
        bare.write_text(json.dumps({"name": "kirocrew-lite"}))
        aim_file = agents_dir / AIM_LITE_FILENAME
        aim_file.write_text(json.dumps({"name": "kirocrew-lite"}))

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            _remove_bare_lite_if_aim_installed()

        assert bare.exists(), "bare file should remain (cleanup is a no-op)"
        assert aim_file.exists(), "legacy AIM-named file should remain"

    def test_cleanup_noop_when_only_aim_exists(self, agents_dir: Path) -> None:
        """Cleanup should be safe when bare file already gone."""
        aim_file = agents_dir / AIM_LITE_FILENAME
        aim_file.write_text(json.dumps({"name": "kirocrew-lite"}))

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            _remove_bare_lite_if_aim_installed()

        assert aim_file.exists()

    def test_cleanup_preserves_bare_when_aim_missing(self, agents_dir: Path) -> None:
        """Cleanup should NOT remove bare file when AIM version is absent."""
        bare = agents_dir / _LITE_AGENT_FILENAME
        bare.write_text(json.dumps({"name": "kirocrew-lite"}))

        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            _remove_bare_lite_if_aim_installed()

        assert bare.exists(), "bare should remain when AIM version not installed"
