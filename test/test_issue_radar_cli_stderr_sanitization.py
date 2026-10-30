"""Issue Radar returns provider CLI stderr only after ``sanitize_cli_stderr``.

Raw ``gh``/``glab`` stderr can carry local paths and private enterprise hosts, so
those are stripped while the diagnostics a user acts on survive.
"""

from __future__ import annotations

from kiro_crew.apps.builtins.issue_radar.backend.errors import sanitize_cli_stderr


class TestProviderStderrSanitization:
    def test_absolute_paths_are_stripped(self):
        out = sanitize_cli_stderr("open /home/alice/.config/gh/hosts.yml: denied")
        assert "/home/alice" not in out

    def test_windows_paths_are_stripped(self):
        out = sanitize_cli_stderr(r"open C:\Users\alice\AppData\gh: denied")
        assert "alice" not in out

    def test_private_enterprise_host_is_stripped(self):
        out = sanitize_cli_stderr('Post "https://github.acme.internal/api/v3": refused')
        assert "acme.internal" not in out

    def test_public_github_host_is_preserved(self):
        url = "https://api.github.com/repos/o/r"
        assert url in sanitize_cli_stderr(f"HTTP 403: not accessible ({url})")

    def test_empty_input(self):
        assert sanitize_cli_stderr("") == ""

    def test_actionable_diagnostics_survive(self):
        """A blanket 'upstream error' would strand the user, so these must remain."""
        for phrase in (
            "gh auth login",
            "HTTP 403",
            "Could not resolve to a Repository",
            "connection refused",
            "i/o timeout",
            "not found",
        ):
            assert phrase in sanitize_cli_stderr(f"gh: failed: {phrase}"), phrase
