"""Shared harness for the ``test_agent_unreadable_main_spec*.py`` modules.

``custom_spec`` writes a user-customised ``kirocrew.json`` a reset to defaults would
erase. A test module is not importable from another one, so what both modules share
lives here; their ``held_flag`` fixture is in ``conftest.py``.
"""

from __future__ import annotations

import json
from pathlib import Path


def custom_spec(tmp_path: Path) -> Path:
    """A user-customised ``kirocrew.json`` a reset to defaults would erase."""
    kiro_dir = tmp_path / "kiro_agents"
    kiro_dir.mkdir(exist_ok=True)
    spec = kiro_dir / "kirocrew.json"
    existing = {
        "model": "claude-user-custom",
        "tools": ["@user-server"],
        "allowedTools": [],
        "mcpServers": {"user-server": {"command": "/opt/user-server", "args": []}},
    }
    spec.write_text(json.dumps(existing), encoding="utf-8")
    return spec
