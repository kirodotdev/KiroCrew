"""A sub-agent run on KAS does not mount ``kirocrew-mate``.

Every ``kirocrew-mate`` tool refuses a ``subagent:`` caller (403
``subagent_caller``), so the server was a process each child started and could
never use. These tests pin that only a ``subagent:`` session loses it, server
and refs together, and that every other session gets its batch back as is.
"""

from __future__ import annotations

import copy
import json

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_agents_session_hoist import without_subagent_refused_servers

MATE = {"command": "kirocrew", "args": ["mcp-mate"]}
CORE = {"command": "kirocrew", "args": ["mcp-core"]}


def _batch() -> list[dict]:
    return [
        {
            "name": "kirocrew",
            "tools": ["read", "@kirocrew-core", "@kirocrew-mate", "@kirocrew-mate/goal_read"],
            "allowedTools": ["read", "@kirocrew-mate/set_goal", "@kirocrew-core/wait"],
            "mcpServers": {"kirocrew-core": dict(CORE), "kirocrew-mate": dict(MATE)},
        },
        {"name": "reviewer", "tools": ["read"], "mcpServers": {"kirocrew-core": dict(CORE)}},
    ]


def test_a_subagent_run_loses_mate_and_its_refs() -> None:
    batch = _batch()
    before = copy.deepcopy(batch)
    out = without_subagent_refused_servers(batch, "subagent:abc123")
    assert batch == before, "the caller's batch is never written"
    head = out[0]
    assert head["mcpServers"] == {"kirocrew-core": CORE}
    assert head["tools"] == ["read", "@kirocrew-core"]
    assert head["allowedTools"] == ["read", "@kirocrew-core/wait"]
    assert out[1] is batch[1], "an entry without mate is passed through"


@pytest.mark.parametrize(
    "key",
    ["dashboard:chat-12", "dashboard:member-ar-tpm-bot", "member-mate.worker-1", ""],
    ids=["plain-chat", "bot-chat", "bot-worker", "no-key"],
)
def test_every_other_session_keeps_mate(key) -> None:
    batch = _batch()
    assert without_subagent_refused_servers(batch, key) is batch


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "mounted"),
    [("subagent:c1", False), ("dashboard:parent", True), ("member-mate.worker-1", True)],
    ids=["subagent", "parent-chat", "bot-worker"],
)
async def test_the_kas_session_start_applies_it(monkeypatch, tmp_path, key, mounted) -> None:
    """``session_extras`` serves both the shared and the dedicated KAS start."""
    spec = {
        "name": "w",
        "prompt": "p",
        "tools": ["read", "@kirocrew-mate", "@kirocrew-core"],
        "mcpServers": {"kirocrew-mate": dict(MATE), "kirocrew-core": dict(CORE)},
    }
    user = tmp_path / "agents"
    user.mkdir()
    (user / "w.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)

    extras = await KasHarness().session_extras("w", work_dir=None, session_key=key)

    entry = next(a for a in extras.custom_agents if a.get("id") == "w")
    assert ("kirocrew-mate" in entry["mcpServers"]) is mounted
    assert ("@kirocrew-mate" in entry["tools"]) is mounted
    assert "kirocrew-core" in entry["mcpServers"]
    assert entry["mcpServers"]["kirocrew-core"]["env"]["KIROCREW_SESSION_KEY"] == key


def test_a_name_that_only_looks_like_mate_is_kept() -> None:
    batch = [
        {
            "name": "kirocrew",
            "tools": ["kirocrew-mate", "@kirocrew-mate-extra"],
            "mcpServers": {"kirocrew-mate": dict(MATE), "kirocrew-mate-extra": {"command": "x"}},
        }
    ]
    out = without_subagent_refused_servers(batch, "subagent:abc123")
    assert out[0]["tools"] == ["kirocrew-mate", "@kirocrew-mate-extra"]
    assert list(out[0]["mcpServers"]) == ["kirocrew-mate-extra"]
