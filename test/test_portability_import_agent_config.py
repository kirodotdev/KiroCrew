"""The dashboard zip import never installs an archive's workspace agent config.

``apply_import_zip`` is a second way into the same ``workspace/`` tree the CLI restore
writes, so it holds back the same names (``.claude/``, ``.mcp.json``, ``.kiro/`` ...) before
either mode reads the extraction, and names them in the summary. Replace keeps this
install's own copy; Merge adds nothing for them. Plain files beside them still import.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

import kiro_crew.portability as port

LIVE = '{"mcpServers": {}}'
HOSTILE = '{"mcpServers": {"x": {"command": "sh"}}}'


def _zip(path: Path) -> Path:
    members = {
        "snap/MANIFEST.json": json.dumps({"version": 2}),
        "snap/workspace/.mcp.json": HOSTILE,
        "snap/workspace/.claude/skills/evil/SKILL.md": "do bad things",
        "snap/workspace/proj/.mcp.json": HOSTILE,
        "snap/workspace/a/b/.kiro/steering/x.md": "steer",
        "snap/workspace/notes/plain.md": "archive plain",
    }
    with zipfile.ZipFile(str(path), "w") as zf:
        for name, text in members.items():
            zf.writestr(name, text)
    return path


def _import(tmp_path: Path, mode: str) -> tuple[dict, Path]:
    target = tmp_path / "target_mc"
    ws = target / "workspace"
    ws.mkdir(parents=True)
    (ws / ".mcp.json").write_text(LIVE)
    with patch.object(port, "config_dir", return_value=target):
        with patch.dict(os.environ, {"KIROCREW_HOME": str(target)}):
            summary = port.apply_import_zip(_zip(tmp_path / "imp.zip"), mode=mode)
    return summary, ws


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_import_holds_back_the_archives_agent_config(tmp_path, mode):
    summary, ws = _import(tmp_path, mode)
    assert (ws / ".mcp.json").read_text() == LIVE, "the live .mcp.json was replaced"
    assert not (ws / ".claude").exists()
    assert not (ws / "proj" / ".mcp.json").exists()
    assert not (ws / "a" / "b" / ".kiro").exists()
    # Everything else in the archive still imports.
    assert (ws / "notes" / "plain.md").read_text() == "archive plain"


@pytest.mark.parametrize("mode", ["replace", "merge"])
def test_import_names_each_held_back_path(tmp_path, mode):
    summary, _ws = _import(tmp_path, mode)
    held = set(summary.get("held_agent_config", []))
    assert held == {
        "workspace/.mcp.json",
        "workspace/.claude",
        "workspace/proj/.mcp.json",
        "workspace/a/b/.kiro",
    }
