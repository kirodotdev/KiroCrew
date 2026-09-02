"""App-sources checkouts are write-protected against agent tools.

``~/.kiro/crew/app-sources/{name}`` is the persistent tree every installed app
EXECUTES from. Writes must be refused at the agent file-edit gate; reads must
keep working, because app source carries no secret and the dashboard file
viewer, knowledge indexing and ordinary debugging all read it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.apps.registry import app_source_dir
from kiro_crew.security import (
    is_sensitive_bash_command,
    is_sensitive_path,
    is_sensitive_write_path,
    write_protected_home_paths,
)

_PREFIXES = ("~/.kiro/crew", "~/.kirocrew")


class TestAppSourcesWriteProtection:
    """Writes are refused for the checkout root and everything under it."""

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_checkout_root_is_write_protected(self, prefix: str) -> None:
        assert is_sensitive_write_path(f"{prefix}/app-sources")
        assert is_sensitive_write_path(str(Path.home() / prefix[2:] / "app-sources"))

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_files_under_a_checkout_are_write_protected(self, prefix: str) -> None:
        # The whole tree, not just the root: the executed file is what matters,
        # and it is always several levels down.
        for rel in (
            "app-sources/notes/server.py",
            "app-sources/notes/src/handlers/index.ts",
            "app-sources/notes/package.json",
            "app-sources/notes/.git/config",
        ):
            assert is_sensitive_write_path(f"{prefix}/{rel}"), rel
            assert is_sensitive_write_path(str(Path.home() / prefix[2:] / rel)), rel

    def test_registry_checkout_path_is_covered(self) -> None:
        """The gate covers the path the installer actually clones into.

        Pins the entry to ``app_source_dir`` rather than to the spelling
        ``app-sources``, so renaming the directory in ``apps.registry`` without
        updating the security entry fails here instead of silently unprotecting
        every installed app.
        """
        assert is_sensitive_write_path(str(app_source_dir("notes") / "server.py"))

    @pytest.mark.parametrize("prefix", _PREFIXES)
    def test_reads_stay_allowed(self, prefix: str) -> None:
        # Write-only, deliberately NOT read+write sensitive: the file viewer
        # lists app-sources as a browsable root and knowledge indexing walks it.
        path = f"{prefix}/app-sources/notes/server.py"
        assert is_sensitive_path(path) is False
        assert is_sensitive_bash_command(f"cat {path}") is None
        assert is_sensitive_bash_command(f"grep -rn handler {prefix}/app-sources/") is None

    def test_sibling_data_home_paths_unaffected(self) -> None:
        """A prefix-neighbour of the entry must not be caught by it."""
        # `app-sources-backup` shares the entry's string prefix but is a
        # different directory; the matcher compares whole segments.
        assert is_sensitive_write_path("~/.kiro/crew/app-sources-backup/x.py") is False
        # An installed app's DATA dir is a different tree and stays writable —
        # apps persist state there through the agent's own tools.
        assert is_sensitive_write_path("~/.kiro/crew/apps/notes/data/notes.json") is False

    def test_entry_is_published_on_the_posture_surface(self) -> None:
        """The posture page derives its list from the enforcing object."""
        entries = write_protected_home_paths()
        assert any(e.endswith("app-sources") for e in entries), entries
        # Both data-home spellings, so a legacy install is gated too.
        assert sum(1 for e in entries if e.endswith("app-sources")) == 2
