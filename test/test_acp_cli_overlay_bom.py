"""The workspace ``cli.json`` overlay reader accepts a UTF-8 byte-order mark.

``<work_dir>/.kiro/settings/cli.json`` can be a person's own workspace settings
file, and Windows editors save UTF-8 "with BOM" by default. ``json.loads`` on
the decoded text refuses that mark, and a file that does not parse is left
unchanged while the overlay write fails, so a reader that kept the mark would
stop every effort and Tool Search overlay from applying in that workspace.
"""

from __future__ import annotations

import codecs
import json

from kiro_crew.providers.acp import (
    _clear_cli_overlay_effort,
    _write_cli_overlay,
    _write_tool_search_overlay,
)

_USER_SETTINGS = {
    "chat.defaultModel": "claude-opus-5",
    "chat.modelDefaults": {"claude-opus-5": {"output_config": {"effort": "low"}}},
}


def _write_bom_settings(work_dir) -> None:
    cli = work_dir / ".kiro" / "settings" / "cli.json"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(codecs.BOM_UTF8 + json.dumps(_USER_SETTINGS).encode("utf-8"))


def _on_disk(work_dir) -> dict:
    raw = (work_dir / ".kiro" / "settings" / "cli.json").read_bytes()
    assert not raw.startswith(codecs.BOM_UTF8)
    return json.loads(raw)


def test_tool_search_overlay_keeps_the_user_settings(tmp_path):
    _write_bom_settings(tmp_path)
    _write_tool_search_overlay(tmp_path, True)
    data = _on_disk(tmp_path)
    assert data["chat.defaultModel"] == "claude-opus-5"
    assert data["chat.modelDefaults"] == _USER_SETTINGS["chat.modelDefaults"]
    assert data["toolSearch.enabled"] is True


def test_effort_overlay_keeps_the_user_settings(tmp_path):
    _write_bom_settings(tmp_path)
    _write_cli_overlay(tmp_path, "claude-sonnet-5", "high")
    data = _on_disk(tmp_path)
    assert data["chat.defaultModel"] == "claude-opus-5"
    assert data["chat.modelDefaults"]["claude-opus-5"] == {"output_config": {"effort": "low"}}
    assert data["chat.modelDefaults"]["claude-sonnet-5"]["output_config"]["effort"] == "high"


def test_an_explicit_clear_removes_the_level_and_keeps_the_settings(tmp_path):
    _write_bom_settings(tmp_path)
    assert _clear_cli_overlay_effort(tmp_path, "claude-opus-5", owned_only=False) is True
    data = _on_disk(tmp_path)
    assert data["chat.defaultModel"] == "claude-opus-5"
    assert "claude-opus-5" not in data.get("chat.modelDefaults", {})
