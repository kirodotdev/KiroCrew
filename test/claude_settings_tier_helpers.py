"""Keep the host's own Claude Code settings out of tests that render Crew's seed.

The seed of ``<work_dir>/.claude/settings.local.json`` reads the user tier
(``CLAUDE_CONFIG_DIR``, else ``~/.claude``) and the managed tier: it writes
``availableModels`` only when one of them defines it, and unions in the models the
user's own list covers. A test that leaves those tiers on the real machine
therefore passes or fails depending on that machine's ``~/.claude/settings.json``.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def empty_claude_settings_tiers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user and managed Claude settings tiers at one empty directory.

    Callers pass the isolation floor's own ``_floor_monkeypatch``, so a test body's
    ``monkeypatch.undo()`` cannot drop the isolation. A test that needs a user file
    repoints ``CLAUDE_CONFIG_DIR`` through its own ``monkeypatch``, which wins.
    """
    empty = tmp_path / "claude-empty-tiers"
    empty.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(empty))
    monkeypatch.setattr("kiro_crew.platform_compat.claude_code_managed_settings_dir", lambda: empty)
    return empty
