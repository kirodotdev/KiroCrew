"""Each small Kiro Crew MCP server carries its own usage text as MCP ``instructions``.

The text lives in ``kiro_crew.mcp_instructions``, not in ``config/prompt.md`` or
``context_assembly/sections.py``. These cases pin what that rests on: the shared
stdio loop returns the text in ``initialize``, each real server process returns ITS
text, and every text fits the harnesses' limits (Claude Code cuts a server's
instructions at 4,096 characters; pi lists only the first line, up to 250). The agent
prompt carries none of these bullets, so a session reads each one from the server
that owns it and only when that server is mounted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp_shared_helpers import _framing_undetected, _initialize, _LoopHarness  # noqa: F401

from kiro_crew import mcp_instructions
from kiro_crew.agent import _MANAGED_MCP_SERVERS

_PROMPT = Path(mcp_instructions.__file__).with_name("config") / "prompt.md"

#: Server name -> the module whose ``run_mcp_server`` serves it.
_SERVER_MODULES = {
    "kirocrew-cron": "kiro_crew.mcp_cron",
    "kirocrew-guide": "kiro_crew.mcp_guide",
    "kirocrew-dashboard": "kiro_crew.mcp_dashboard",
    "kirocrew-computer": "kiro_crew.mcp_computer",
}


class TestStdioLoopInstructions:
    def test_initialize_returns_the_instructions_it_was_given(self, monkeypatch) -> None:
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok", {"instructions": "Use it well."})
        try:
            harness.send(_initialize(1))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert harness.responses[0][1]["instructions"] == "Use it well."
        finally:
            harness.close()

    def test_initialize_has_no_instructions_key_without_text(self, monkeypatch) -> None:
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        try:
            harness.send(_initialize(1))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert "instructions" not in harness.responses[0][1]
        finally:
            harness.close()


@pytest.mark.parametrize("server", sorted(_SERVER_MODULES))
def test_each_real_server_returns_its_own_instructions(server: str, tmp_path: Path) -> None:
    """A real server process answers ``initialize`` with its entry from the table."""
    frame = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }
    env = {**os.environ, "KIROCREW_HOME": str(tmp_path / "home")}
    env.pop("KIROCREW_PORT", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"from {_SERVER_MODULES[server]} import run_mcp_server; run_mcp_server()",
        ],
        input=json.dumps(frame) + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
        timeout=60,
        check=False,
    )
    replies = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    result = next(r["result"] for r in replies if r.get("id") == 1)
    assert result["serverInfo"]["name"] == server
    assert result["instructions"] == mcp_instructions.BY_SERVER[server]


def test_the_table_names_exactly_the_servers_that_serve_it() -> None:
    assert set(mcp_instructions.BY_SERVER) == set(_SERVER_MODULES)
    assert set(mcp_instructions.BY_SERVER) <= set(_MANAGED_MCP_SERVERS)


@pytest.mark.parametrize("server", sorted(_SERVER_MODULES))
def test_each_text_fits_every_harness(server: str) -> None:
    text = mcp_instructions.BY_SERVER[server]
    assert len(text) <= mcp_instructions.MAX_INSTRUCTIONS_CHARS, (server, len(text))
    summary, _, body = text.partition("\n")
    assert summary and body, server
    assert len(summary) <= mcp_instructions.MAX_SUMMARY_CHARS, (server, len(summary))
    # The summary stands alone where pi lists it: no heading, no bullet.
    assert not summary.startswith(("#", "-", "*")), summary


def test_the_prompt_no_longer_carries_the_moved_bullets() -> None:
    """A moved bullet would reach a session twice, and one without the server once."""
    prompt = _PROMPT.read_text(encoding="utf-8")
    for line in prompt.splitlines():
        assert not line.startswith(
            (
                "- `cron_add`:",
                "- `cron_list`:",
                "- `cron_update(",
                "- `kirocrew-guide`",
                "- Peer sessions:",
            )
        ), line
    for phrase in ("Tree first, always", "computer_launch_app", "New sessions are EMPTY"):
        assert phrase not in prompt, phrase


def test_the_spawn_bullet_still_points_long_lived_work_at_peer_sessions() -> None:
    """The spawn-versus-peer-session choice is the spawn tools' and stays beside them."""
    prompt = _PROMPT.read_text(encoding="utf-8")
    spawn = next(line for line in prompt.splitlines() if line.startswith("- `spawn_run`:"))
    assert "These are the ONLY subagent mechanisms" in spawn
    assert "peer session (`session_create`) when that tool is present" in spawn
