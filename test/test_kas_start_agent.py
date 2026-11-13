"""The server-less start agent a new KAS session is registered with and started in.

``with_start_agent`` decides when a batch gets it, and ``KasHarness.session_request``
names it as the mode ``session/new`` starts in. What that buys at session start is
pinned against a model of the engine's connect queue in
``test_kas_session_start_queue.py``.
"""

from __future__ import annotations

import copy

import pytest

from kiro_crew.acp.harness import harness_for
from kiro_crew.acp.kas_agents import KAS_MAX_CUSTOM_AGENTS
from kiro_crew.acp.kas_start_agent import START_AGENT_ID, with_start_agent
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
)

_SERVERS = {"github": {"command": "gh-mcp"}}


def _own(**extra: object) -> dict:
    return {"id": "kirocrew", "prompt": "You are Kiro.", "tools": ["fs_read"], **extra}


class TestWithStartAgent:
    def test_an_agent_that_declares_servers_gets_the_start_agent_after_it(self):
        batch = [_own(mcpServers=_SERVERS)]
        before = copy.deepcopy(batch)
        held = with_start_agent(batch)
        assert held is not None
        assert [entry["id"] for entry in held] == ["kirocrew", START_AGENT_ID]
        assert held[0] is batch[0]
        assert batch == before

    def test_the_start_agent_declares_no_server_and_mounts_no_tool(self):
        held = with_start_agent([_own(mcpServers=_SERVERS)])
        assert held is not None
        start = held[-1]
        assert "mcpServers" not in start
        assert start["tools"] == []
        assert start["includeMcpJson"] is False
        # An engine that drops a prompt-less entry would start the session in vibe.
        assert start["prompt"]

    def test_each_call_returns_its_own_start_entry(self):
        first = with_start_agent([_own(mcpServers=_SERVERS)])
        second = with_start_agent([_own(mcpServers=_SERVERS)])
        assert first is not None and second is not None
        first[-1]["tools"].append("fs_write")
        assert second[-1]["tools"] == []

    @pytest.mark.parametrize(
        "batch",
        [
            [],
            ["kirocrew"],
            [_own()],
            [_own(mcpServers={})],
            [_own(mcpServers=["github"])],
            [_own(mcpServers=_SERVERS, tools="*")],
            [_own(mcpServers=_SERVERS, tools=["fs_read", "@subagent"])],
            [_own(mcpServers=_SERVERS), {"id": START_AGENT_ID, "prompt": "mine"}],
            [_own(mcpServers=_SERVERS)]
            + [{"id": f"target-{i}", "prompt": "p"} for i in range(KAS_MAX_CUSTOM_AGENTS - 1)],
        ],
        ids=[
            "empty",
            "not-a-dict",
            "no-block",
            "empty-block",
            "block-not-a-dict",
            "all-tools",
            "sub-agent-tool",
            "id-taken",
            "batch-full",
        ],
    )
    def test_the_session_starts_in_its_own_agent(self, batch):
        assert with_start_agent(batch) is None

    def test_a_batch_one_short_of_the_engine_limit_still_gets_it(self):
        batch = [_own(mcpServers=_SERVERS)] + [
            {"id": f"target-{i}", "prompt": "p"} for i in range(KAS_MAX_CUSTOM_AGENTS - 2)
        ]
        held = with_start_agent(batch)
        assert held is not None
        assert len(held) == KAS_MAX_CUSTOM_AGENTS


def _request(*entries: object) -> dict:
    return {"cwd": "/w", "mcpServers": [], "_meta": {"kiro": {"customAgents": list(entries)}}}


class TestSessionRequest:
    def test_a_new_session_starts_in_the_start_agent_and_activates_its_own(self):
        params = _request(_own(mcpServers=_SERVERS))
        before = copy.deepcopy(params)
        out, activated = harness_for(ACP_BACKEND_KAS).session_request(
            METHOD_SESSION_NEW, params, None, agent_capabilities={}
        )
        kiro = out["_meta"]["kiro"]
        assert kiro["modeId"] == START_AGENT_ID
        assert [entry["id"] for entry in kiro["customAgents"]] == ["kirocrew", START_AGENT_ID]
        # The agent's servers stay where they were declared.
        assert kiro["customAgents"][0]["mcpServers"] == _SERVERS
        assert activated is None
        assert params == before

    def test_an_agent_without_servers_starts_in_itself(self):
        params = _request(_own())
        out, _activated = harness_for(ACP_BACKEND_KAS).session_request(
            METHOD_SESSION_NEW, params, None, agent_capabilities={}
        )
        assert out["_meta"]["kiro"] == {"customAgents": [_own()], "modeId": "kirocrew"}

    def test_a_load_is_sent_as_built(self):
        params = {**_request(_own(mcpServers=_SERVERS)), "sessionId": "s1"}
        out, activated = harness_for(ACP_BACKEND_KAS).session_request(
            METHOD_SESSION_LOAD, params, "kirocrew", agent_capabilities={}
        )
        assert out is params
        assert activated == "kirocrew"

    def test_only_the_start_agent_is_a_start_mode_without_servers(self):
        """Reports staged in the start agent still count; any other start mode's do not."""
        kas = harness_for(ACP_BACKEND_KAS)
        assert kas.start_mode_ran_servers(START_AGENT_ID) is False
        for mode_id in ("vibe", "kirocrew", ""):
            assert kas.start_mode_ran_servers(mode_id) is True
        assert harness_for(ACP_BACKEND_KIRO).start_mode_ran_servers(START_AGENT_ID) is True
