"""The mimocode Bots captures: what they show, and that Crew's projection builds them.

``test/fixtures/acp_frames/mimocode/saved-spec-projection-live.jsonl`` and
``worker-kit-live.jsonl`` were driven with arrays Crew's mimocode mirror built. These
tests read back what each capture shows (the spec's server answered the model's call
with its own result) and rebuild the saved-spec element with the same mirror, so the
capture and the projection cannot drift apart unnoticed.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_MIMOCODE,
    ACP_BACKENDS_MATE_WORKER_KIT,
    ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT,
    ACP_BACKENDS_MEMBER_CAPABILITIES,
    ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN,
)

_FRAMES = Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode"


def _updates(name: str) -> list[dict]:
    lines = (_FRAMES / name).read_text(encoding="utf-8").splitlines()[1:]
    return [
        f["params"]["update"]
        for f in (json.loads(line) for line in lines if line.strip())
        if f.get("method") == "session/update"
    ]


def _call(name: str) -> tuple[str, str]:
    """The one tool call's title and the output of its completed update."""
    updates = _updates(name)
    (started,) = [u for u in updates if u.get("sessionUpdate") == "tool_call"]
    done = [
        u
        for u in updates
        if u.get("sessionUpdate") == "tool_call_update" and u.get("status") == "completed"
    ]
    assert len(done) == 1
    return started["title"], done[0]["rawOutput"]["output"]


def test_the_saved_spec_server_answered_the_models_call() -> None:
    assert _call("saved-spec-projection-live.jsonl") == ("probe_probe_codeword", "tangerine-42")
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_MEMBER_CAPABILITIES
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_TEMPLATE_ADDITIONS_AT_SPAWN


def test_the_crewmates_server_answered_the_workers_call() -> None:
    title, output = _call("worker-kit-live.jsonl")
    assert (title, output) == ("pixel-probe_pixel-probe_codeword", "kumquat-17")
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_MATE_WORKER_KIT
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT


def test_the_captured_element_is_what_the_mimocode_mirror_projects(tmp_path, monkeypatch) -> None:
    """The capture's one element is the mirror's own projection of a saved spec, from
    the parse ``consumed_spec_matches`` confirms against the saved stamp."""
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
        "tools": ["@probe"],
        "mcpServers": {"probe": {"command": "python3", "args": ["probe_server.py"]}},
    }
    (agents / "probe-member.json").write_text(json.dumps(saved), encoding="utf-8")
    projection = mirror_for(ACP_BACKEND_MIMOCODE).session_projection(
        "probe-member", work_dir=tmp_path
    )
    assert [e["name"] for e in projection.params["mcpServers"]] == ["probe"]
    assert consumed_spec_matches(projection.agent_spec, {"materialized": _digest(saved)})
    edited = {**saved, "tools": ["*"]}
    assert not consumed_spec_matches(projection.agent_spec, {"materialized": _digest(edited)})
