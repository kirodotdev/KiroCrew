"""One session start resolves each managed MCP entry and the registry mode once.

A skill-view preparation asks for them for every agent it projects. Inside one
``build_reuse.one_build`` block the first answer is reused and each caller gets its
own copy; the next block, and every call outside a block, resolves again.
"""

from __future__ import annotations

from kiro_crew import agent as agent_mod
from kiro_crew.agent_materialization import managed_mcp
from kiro_crew.build_reuse import one_build


def _counting_invocation(monkeypatch):
    calls: list[int] = []

    def invocation():
        calls.append(1)
        return f"/opt/kc-{len(calls)}/bin/kirocrew", ["mcp-core"]

    spec = dict(agent_mod._MANAGED_MCP_SERVERS["kirocrew-core"], invocation_fn=invocation)
    monkeypatch.setitem(agent_mod._MANAGED_MCP_SERVERS, "kirocrew-core", spec)
    monkeypatch.setattr(managed_mcp, "_managed_mcp_env", lambda: {})
    return calls


def test_one_start_resolves_an_entry_once_and_hands_out_copies(monkeypatch):
    calls = _counting_invocation(monkeypatch)

    with one_build():
        first = managed_mcp.managed_mcp_spec_entry("kirocrew-core")
        first["args"].append("mutated by a caller")
        second = managed_mcp.managed_mcp_spec_entry("kirocrew-core")

    assert len(calls) == 1
    assert second == {"command": "/opt/kc-1/bin/kirocrew", "args": ["mcp-core"]}


def test_the_next_start_and_calls_outside_a_start_resolve_again(monkeypatch):
    calls = _counting_invocation(monkeypatch)

    with one_build():
        managed_mcp.managed_mcp_spec_entry("kirocrew-core")
    with one_build():
        assert managed_mcp.managed_mcp_spec_entry("kirocrew-core")["command"] == (
            "/opt/kc-2/bin/kirocrew"
        )
    managed_mcp.managed_mcp_spec_entry("kirocrew-core")
    managed_mcp.managed_mcp_spec_entry("kirocrew-core")

    assert len(calls) == 4


def test_registry_mode_is_read_once_per_start(monkeypatch):
    reads: list[bool] = []

    def read() -> bool:
        reads.append(True)
        return len(reads) % 2 == 1

    monkeypatch.setattr(managed_mcp, "_read_mcp_registry_mode", read)

    with one_build():
        assert [managed_mcp._mcp_registry_mode() for _ in range(3)] == [True] * 3
    with one_build():
        assert managed_mcp._mcp_registry_mode() is False

    assert len(reads) == 2
