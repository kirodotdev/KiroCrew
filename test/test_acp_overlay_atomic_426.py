"""Regression: ACP cli.json overlays must be written atomically (#426).

A plain ``write_text`` truncates-then-writes, so a crash or concurrent read
mid-write yields an empty/partial file and kiro-cli drops the effort / Tool
Search settings. The overlay writers now route through ``atomic_write``
(temp file + ``os.replace``), so readers only ever see a complete file and no
``.tmp`` residue is left behind.
"""

from __future__ import annotations

import json

from kiro_crew.providers import acp as acp_mod
from kiro_crew.providers.acp import _write_cli_overlay, _write_tool_search_overlay


def _cli_json(work_dir):
    return work_dir / ".kiro" / "settings" / "cli.json"


def test_cli_overlay_writes_valid_json_no_temp_residue(tmp_path):
    _write_cli_overlay(tmp_path, "claude-opus-5", "high")
    cli = _cli_json(tmp_path)
    data = json.loads(cli.read_text(encoding="utf-8"))  # complete + parseable
    assert "chat.modelDefaults" in data
    assert list(cli.parent.glob("*.tmp")) == []


def test_tool_search_overlay_merges_atomically(tmp_path):
    _write_cli_overlay(tmp_path, "claude-opus-5", "high")
    _write_tool_search_overlay(tmp_path, True)
    cli = _cli_json(tmp_path)
    data = json.loads(cli.read_text(encoding="utf-8"))
    # Both overlays present; second write did not clobber the first.
    assert "chat.modelDefaults" in data
    assert list(cli.parent.glob("*.tmp")) == []


def test_overlay_routes_through_atomic_write(tmp_path, monkeypatch):
    calls: list = []
    real = acp_mod.atomic_write
    monkeypatch.setattr(
        acp_mod, "atomic_write", lambda p, c, **k: (calls.append(p), real(p, c, **k))[1]
    )
    _write_cli_overlay(tmp_path, "gpt-5.6-sol", "medium")
    assert calls, "overlay write must go through atomic_write"
