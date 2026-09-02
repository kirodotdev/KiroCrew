"""Regression: package-prefixed agents are matched by overlay `name` (#925).

Package-installed agents are written under a package-qualified filename while
the session requests them by bare name, so a filename-only lookup silently
missed and disabled pooling. `_load_overlay_for_agent` now falls back to the
overlay whose parsed `name` equals the requested agent.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.mcp_gateway.session_servers import _load_overlay_for_agent


def test_bare_filename_fast_path(tmp_path: Path) -> None:
    spec = {"name": "gpu-dev", "mcpServers": {}}
    (tmp_path / "gpu-dev.json").write_text(json.dumps(spec), encoding="utf-8")
    assert _load_overlay_for_agent(tmp_path, "gpu-dev") == spec


def test_package_qualified_matched_by_name(tmp_path: Path) -> None:
    spec = {"name": "gpu-dev", "mcpServers": {"x": {}}}
    (tmp_path / "AIPowerUserCapabilities-gpu-dev.json").write_text(
        json.dumps(spec), encoding="utf-8"
    )
    got = _load_overlay_for_agent(tmp_path, "gpu-dev")
    assert got is not None and got["name"] == "gpu-dev"


def test_no_matching_overlay_returns_none(tmp_path: Path) -> None:
    (tmp_path / "Other-thing.json").write_text(
        json.dumps({"name": "thing", "mcpServers": {}}), encoding="utf-8"
    )
    assert _load_overlay_for_agent(tmp_path, "gpu-dev") is None


def test_empty_dir_returns_none(tmp_path: Path) -> None:
    assert _load_overlay_for_agent(tmp_path, "gpu-dev") is None


def test_glob_metacharacter_agent_fails_soft(tmp_path: Path) -> None:
    """An agent name with glob metacharacters must not raise (issue #925).

    ``Path.glob('**.json')`` raises ``ValueError``; the lookup runs on the
    session-creation path, so it must fail soft to ``None`` (unpooled) rather
    than aborting the turn.
    """
    (tmp_path / "star.json").write_text(
        json.dumps({"name": "star", "mcpServers": {}}), encoding="utf-8"
    )
    assert _load_overlay_for_agent(tmp_path, "*") is None
    assert _load_overlay_for_agent(tmp_path, "**") is None
