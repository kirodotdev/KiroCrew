"""Tests for kirocrew-lite duplicate agent prevention."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.agent import _LITE_AGENT_FILENAME, _install_lite_agent_fallback


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    """Temporary agents directory."""
    d = tmp_path / "agents"
    d.mkdir()
    return d


# Legacy package-installed lite-agent filename (companion layout). Core no longer
# references the package name; the fork must still tolerate such a file if present.
AIM_LITE_FILENAME = "KiroCrewAICapabilities-kirocrew-lite.json"


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
