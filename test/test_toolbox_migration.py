"""Tests for shell-profile cleanup helpers in cli.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch


def _load_cli():
    """Import cli module."""
    from kiro_crew import cli
    return cli


class TestFixShellProfiles:
    """Tests for _fix_shell_profiles."""

    def test_removes_kirocrew_app_path(self, tmp_path):
        cli = _load_cli()
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text(
            'export PATH="$HOME/.kirocrew-app/.venv/bin:$PATH"\n'
            'export PATH="$HOME/.toolbox/bin:$PATH"\n'
            'alias ll="ls -la"\n'
        )
        with patch.object(Path, "home", return_value=tmp_path):
            cli._fix_shell_profiles()
        content = zshrc.read_text()
        assert ".kirocrew-app" not in content
        assert ".toolbox" in content
        assert "alias ll" in content

    def test_preserves_unrelated_paths(self, tmp_path):
        cli = _load_cli()
        zshrc = tmp_path / ".zshrc"
        zshrc.write_text(
            'export PATH="$HOME/brazil-workspaces/OtherProject/bin:$PATH"\n'
            'export PATH="/usr/local/bin:$PATH"\n'
        )
        with patch.object(Path, "home", return_value=tmp_path):
            cli._fix_shell_profiles()
        content = zshrc.read_text()
        assert "OtherProject" in content
        assert "/usr/local/bin" in content

    def test_skips_missing_profiles(self, tmp_path):
        cli = _load_cli()
        with patch.object(Path, "home", return_value=tmp_path):
            cli._fix_shell_profiles()  # no crash

    def test_no_write_when_nothing_to_clean(self, tmp_path):
        cli = _load_cli()
        zshrc = tmp_path / ".zshrc"
        original = 'export PATH="/usr/local/bin:$PATH"\nalias vim=nvim\n'
        zshrc.write_text(original)
        with patch.object(Path, "home", return_value=tmp_path):
            cli._fix_shell_profiles()
        assert zshrc.read_text() == original
