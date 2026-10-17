"""Measured hot-path fixes found by profiling the ACP and MCP runtimes under load.

1. ``ScriptHookStore.fire`` rewrote ``hooks.json`` on EVERY fire, matched or not
   (every tool call and turn). It now writes only when a hook ran.
2. ``mcp_shared`` imported a constant from ``kiro_crew.acp.types``, which ran the
   whole ACP package in every stdio MCP server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew import mcp_shared
from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE, HOOK_EVENT_PRE_TOOL_USE, ScriptHookStore

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.asyncio
async def test_a_fire_that_matches_no_hook_does_not_rewrite_hooks_json(tmp_path):
    store = ScriptHookStore(tmp_path)
    store.create(
        {
            "name": "only-bash",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "matcher": "bash",
            "command": "true",
        }
    )
    with patch.object(store, "_persist_current") as persist:
        await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="read_file")
        await store.fire(HOOK_EVENT_POST_TOOL_USE, tool_name="bash")  # no hook for this event
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_a_fire_that_runs_a_hook_still_records_its_status(tmp_path):
    store = ScriptHookStore(tmp_path)
    hook = store.create(
        {"name": "bash", "event": HOOK_EVENT_PRE_TOOL_USE, "matcher": "bash", "command": "true"}
    )
    result = type(
        "R", (), {"exit_code": 0, "duration_ms": 1, "stdout": "", "stderr": "", "error": ""}
    )()

    async def _run(h, *_a, **_k):
        h.last_status = "ok"
        h.run_count += 1
        return result

    with patch("kiro_crew.hooks.run_script_hook", new=AsyncMock(side_effect=_run)):
        await store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="bash")
    saved = json.loads((tmp_path / "hooks.json").read_text())
    entry = next(h for h in saved["hooks"] if h["id"] == hook.id)
    assert entry["run_count"] == 1


def test_mcp_shared_import_does_not_load_the_acp_client():
    code = "import sys, kiro_crew.mcp_shared; print('kiro_crew.acp.client' in sys.modules)"
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": str(SRC)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert out.stdout.strip() == "False", out.stderr[-2000:]
    assert mcp_shared.JSONRPC_METHOD_NOT_FOUND == -32601
