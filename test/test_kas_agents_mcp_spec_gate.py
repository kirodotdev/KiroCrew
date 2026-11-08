"""A managed MCP server whose ``spec_gate`` is closed is not declared to KAS.

``kirocrew-computer`` carries the one gate. While Computer Use is off or
unsupported, both spec writers withhold its entry, but an agent file Kiro Crew did
not write (a legacy spec, or one edited by hand) can still declare it. Declared on
KAS, the engine launches a backend that advertises no tools, and the managed
readiness barrier waits out its whole budget on it, so every session on that
agent fails. The projection withholds it, reading the gate with the spec writers'
own fail-closed snapshot.
"""

from __future__ import annotations

import pytest
from kas_agents_helpers import _spec

import kiro_crew.agent as agent_mod
from kiro_crew.acp import kas_agents
from kiro_crew.acp.kas_agents import hoist_servers_for_session, to_client_custom_agent
from kiro_crew.acp.mcp_session_report import required_managed_servers

_COMPUTER = "kirocrew-computer"
_AGENT = "kirocrew"


@pytest.fixture(autouse=True)
def _not_registry_governed(_floor_monkeypatch):
    """Off unless a test says otherwise, so the host's config cannot decide."""
    _floor_monkeypatch.setattr(kas_agents, "_registry_governed", lambda: False)


def _set_gate(monkeypatch, gate):
    entry = dict(agent_mod._MANAGED_MCP_SERVERS[_COMPUTER], spec_gate=gate)
    monkeypatch.setitem(agent_mod._MANAGED_MCP_SERVERS, _COMPUTER, entry)


def _legacy_spec():
    """A spec that still carries the explicit block a fresh build would withhold."""
    return _spec(
        tools=["fs_read", "@kirocrew-core", f"@{_COMPUTER}"],
        mcpServers={
            "kirocrew-core": {"command": "kc", "args": ["mcp-core"]},
            _COMPUTER: {"command": "kc", "args": ["mcp-computer"]},
        },
    )


def _session_params():
    """The ``session/new`` params the KAS harness sends for the legacy spec."""
    projected = to_client_custom_agent(_AGENT, _legacy_spec(), "p", session_key="chat:k")
    agents, array = hoist_servers_for_session([projected], _AGENT, [])
    return {"mcpServers": array, "_meta": {"kiro": {"customAgents": agents}}}


def _sent_names(params):
    block = params["_meta"]["kiro"]["customAgents"][0].get("mcpServers", {})
    return {element["name"] for element in params["mcpServers"]} | set(block)


class TestAGateClosedManagedServerIsNotDeclared:
    def test_the_projection_omits_the_entry(self, monkeypatch):
        _set_gate(monkeypatch, lambda: False)
        out = to_client_custom_agent(_AGENT, _legacy_spec(), "p")
        assert set(out["mcpServers"]) == {"kirocrew-core"}
        assert f"@{_COMPUTER}" in out["tools"], "the ref stays, as on a spec Crew writes"

    def test_the_session_neither_sends_it_nor_waits_for_it(self, monkeypatch):
        _set_gate(monkeypatch, lambda: False)
        params = _session_params()
        assert _COMPUTER not in _sent_names(params)
        assert required_managed_servers(params, _AGENT) == ("kirocrew-core",)

    def test_a_gate_that_raises_reads_as_closed(self, monkeypatch):
        """The writers' emission predicate: an unreadable keystone is not evidence
        the capability is on, and declaring the entry is what launches it."""

        def unreadable():
            raise OSError("keystone unreadable")

        _set_gate(monkeypatch, unreadable)
        out = to_client_custom_agent(_AGENT, _legacy_spec(), "p")
        assert set(out["mcpServers"]) == {"kirocrew-core"}

    def test_an_open_gate_keeps_the_entry(self, monkeypatch):
        _set_gate(monkeypatch, lambda: True)
        params = _session_params()
        assert _COMPUTER in _sent_names(params)
        assert _COMPUTER in required_managed_servers(params, _AGENT)

    def test_the_withhold_is_explained(self, monkeypatch, caplog):
        _set_gate(monkeypatch, lambda: False)
        with caplog.at_level("INFO", logger="kiro_crew.acp.kas_agents"):
            to_client_custom_agent(_AGENT, _legacy_spec(), "p")
        assert _COMPUTER in caplog.text
        assert "spec_gate is closed" in caplog.text
