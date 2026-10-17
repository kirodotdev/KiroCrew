"""The workspace ``cli.json`` overlay readers accept a UTF-8 byte-order mark.

``<work_dir>/.kiro/settings/cli.json`` can be a person's own workspace settings
file, and Windows editors save UTF-8 "with BOM" by default. ``json.loads`` on
the decoded text refuses that mark, and the two overlay writers treat a refused
file as empty, so a reader that kept the mark would replace a BOM-saved file
with one holding only Kiro Crew's keys on the next spawn.
"""

from __future__ import annotations

import codecs
import json

from kiro_crew.providers.acp import (
    _clear_cli_overlay_effort,
    _read_cli_overlay,
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


def test_effort_is_recovered_and_cleared(tmp_path):
    _write_bom_settings(tmp_path)
    assert _read_cli_overlay(tmp_path) == {"claude-opus-5": "low"}
    assert _clear_cli_overlay_effort(tmp_path, "claude-opus-5") is True
    data = _on_disk(tmp_path)
    assert data["chat.defaultModel"] == "claude-opus-5"
    assert "claude-opus-5" not in data.get("chat.modelDefaults", {})
