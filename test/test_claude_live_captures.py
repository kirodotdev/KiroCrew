"""claude memberships read off live captures rather than off construction.

Each test reads one committed capture in ``test/fixtures/acp_frames/claude/`` (taken
on claude-agent-acp 0.94.0) and pins the membership that capture is the evidence for.
The replay snapshot beside each capture pins the event shape; these pin the facts the
membership comments in ``agent_sdk/backends_turns.py``, ``backends_mcp.py`` and
``backends_members.py`` quote.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE as CLAUDE

FIXTURES = Path(__file__).parent / "fixtures" / "acp_frames" / "claude"


def _frames(name: str) -> list[dict]:
    lines = (FIXTURES / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:]]


def _updates(frames: list[dict]) -> list[dict]:
    return [
        f["params"]["update"]
        for f in frames
        if f.get("method") == "session/update" and isinstance(f["params"].get("update"), dict)
    ]


def _turn_ends(frames: list[dict]) -> list[int]:
    return [i for i, f in enumerate(frames) if "stopReason" in (f.get("result") or {})]


def test_claude_compacts_inline_on_a_live_capture() -> None:
    """A ``/compact`` turn shrinks the context inside the turn and ends with ``end_turn``."""
    frames = _frames("compact-live.jsonl")
    ends = _turn_ends(frames)
    assert len(ends) == 4, "two ordinary turns, the /compact turn, one ordinary turn"
    compact_turn = frames[ends[1] + 1 : ends[2] + 1]
    marked = [
        u
        for u in _updates(compact_turn)
        if (u.get("_meta") or {}).get("claudeCode", {}).get("toolName") == "compact"
    ]
    assert marked[0]["sessionUpdate"] == "tool_call"
    raw = next(u["rawOutput"] for u in marked if "rawOutput" in u)
    assert raw["trigger"] == "manual"
    assert raw["postTokens"] < raw["preTokens"]
    assert compact_turn[-1]["result"]["stopReason"] == "end_turn"

    def used(span: list[dict]) -> list[int]:
        return [u["used"] for u in _updates(span) if u.get("sessionUpdate") == "usage_update"]

    peak = max(used(frames[: ends[1] + 1]))
    after = used(frames[ends[2] + 1 :])
    assert raw["preTokens"] == peak
    assert max(after) < peak, "the next ordinary turn reads below the pre-compact peak"
    assert CLAUDE in sdk_backends.ACP_BACKENDS_COMPACT
    assert CLAUDE in sdk_backends.ACP_BACKENDS_INLINE_COMPACTION


def _round_trip(name: str, title: str, marker: str) -> None:
    frames = _frames(name)
    assert any(
        "sessionId" in (f.get("result") or {}) for f in frames
    ), "session/new accepted the array"
    calls = [u for u in _updates(frames) if u.get("title") == title]
    assert calls and calls[0]["sessionUpdate"] == "tool_call"
    call_id = calls[0]["toolCallId"]
    asks = [
        f
        for f in frames
        if f.get("method") == "session/request_permission"
        and f["params"]["toolCall"]["toolCallId"] == call_id
    ]
    assert len(asks) == 1, "the MCP call asks the host before it runs"
    done = [
        u
        for u in _updates(frames)
        if u.get("toolCallId") == call_id and u.get("status") == "completed"
    ]
    assert done and marker in json.dumps(done[-1]["content"])


def test_claude_mounts_a_crew_shaped_stdio_element_end_to_end() -> None:
    """An ``_acp_server_entry``-shaped element is mounted and its tool is reachable."""
    _round_trip(
        "mcp-stdio-mount-live.jsonl",
        "mcp__crew-probe__crew_probe_echo",
        "crew-member-mount-proved",
    )
    assert CLAUDE in sdk_backends.ACP_BACKENDS_SESSION_MCP_ARRAY


def test_claude_consumes_a_saved_spec_projection_end_to_end() -> None:
    """The server the mirror projected out of a saved spec reaches the live session."""
    _round_trip(
        "saved-spec-projection-live.jsonl",
        "mcp__spec-probe__spec_probe_echo",
        "saved-spec-projection-consumed",
    )
    assert CLAUDE in sdk_backends.ACP_BACKENDS_MEMBER_CAPABILITIES


def test_the_captured_element_is_what_the_claude_mirror_projects(tmp_path, monkeypatch) -> None:
    """The capture's one element is the mirror's own projection of a saved spec.

    Same spec shape as the capture's, so the server name the live title carries
    (``spec-probe``) is the one the projection emits, and the parse it emits it from
    is the one ``consumed_spec_matches`` confirms against the saved stamp.
    """
    import kiro_crew.agent as agent_mod
    from kiro_crew.acp import session_mcp
    from kiro_crew.agent_capabilities import _digest
    from kiro_crew.agent_capabilities_consumed import consumed_spec_matches
    from kiro_crew.providers.mirrors import mirror_for

    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents)
    monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", tmp_path / "settings-mcp.json")
    monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
    monkeypatch.setattr(session_mcp, "_mcp_registry_mode", lambda: False)
    saved = {
        "name": "probe-member",
        "tools": ["*"],
        "mcpServers": {"spec-probe": {"command": "python3", "args": ["probe_server.py"]}},
    }
    (agents / "probe-member.json").write_text(json.dumps(saved), encoding="utf-8")
    projection = mirror_for(CLAUDE).session_projection(
        "probe-member", work_dir=tmp_path, permission_surface_owned=True
    )
    names = [e["name"] for e in projection.params["mcpServers"]]
    assert "spec-probe" in names
    assert consumed_spec_matches(projection.agent_spec, {"materialized": _digest(saved)})
