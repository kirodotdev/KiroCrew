# SPDX-License-Identifier: Apache-2.0
"""A crewmate's worker runs where a member chat loads its saved spec.

One test per backend family: a projecting host (its session MCP projection reads
the worker's own spec by the session), KAS (the per-session wire spec, unchanged)
and kiro-cli (refused: it cannot find the worker spec), plus the single-tool-limit
refusal for a crewmate whose file turns tools off.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew import mate_worker
from kiro_crew.acp_backends import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS,
)
from kiro_crew.mate_worker import MATE_WORKER_AGENT

BASE = {
    "name": MATE_WORKER_AGENT,
    "prompt": "You are the worker.",
    "tools": ["fs_read", "@kirocrew-core"],
    "mcpServers": {"kirocrew-core": {"command": "kirocrew", "args": ["mcp", "core"]}},
}
PIXEL = {
    "name": "pixel",
    "tools": ["@jira"],
    "mcpServers": {"jira": {"command": "jira-mcp"}},
}


@pytest.fixture
def pixel_worker(tmp_path, monkeypatch):
    """``pixel``'s own file beside the worker base; its worker session is ``chat-9``."""
    from kiro_crew.config import paths as paths_mod

    mates = tmp_path / "mates"
    mates.mkdir()
    (mates / f"{MATE_WORKER_AGENT}.json").write_text(json.dumps(BASE), encoding="utf-8")
    (mates / "pixel.json").write_text(json.dumps(PIXEL), encoding="utf-8")
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member, config=None: "pixel")
    monkeypatch.setattr(mate_worker, "globally_disabled_refs", lambda names: [])
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: SimpleNamespace(member_id="pixel") if key == "dashboard:chat-9" else None,
    )
    return mates


def test_a_projecting_host_mounts_the_crewmates_own_server_on_its_worker(pixel_worker):
    """codex, claude and deepseek read one projection; the session picks the worker's spec."""
    from kiro_crew.acp.session_mcp import session_mcp_projection

    assert mate_worker.worker_kit_backend_refusal(ACP_BACKEND_CODEX) is None
    names = lambda p: {e.get("name") for e in p.servers}  # noqa: E731
    worker = session_mcp_projection(MATE_WORKER_AGENT, session_key="dashboard:chat-9")
    assert "jira" in names(worker)
    # Without the session the projection is the bare base every worker shares.
    assert "jira" not in names(session_mcp_projection(MATE_WORKER_AGENT))


@pytest.mark.parametrize("damage", ["missing", "malformed"])
def test_a_projecting_host_refuses_a_worker_whose_base_is_unreadable(pixel_worker, damage):
    """A worker never runs without its kit: with its base gone or unparseable the
    projection refuses, the way KAS's does, instead of mounting the control plane only."""
    from kiro_crew.acp.session_mcp import session_mcp_projection
    from kiro_crew.mate_worker import WorkerKitUnreadable

    base = pixel_worker / f"{MATE_WORKER_AGENT}.json"
    if damage == "missing":
        base.unlink()
    else:
        base.write_text("{not json", encoding="utf-8")
    with pytest.raises(WorkerKitUnreadable):
        session_mcp_projection(MATE_WORKER_AGENT, session_key="dashboard:chat-9")
    # Not a worker session: no kit to lose, so no refusal.
    session_mcp_projection(MATE_WORKER_AGENT)


def test_kas_keeps_the_per_session_wire_spec(pixel_worker):
    assert mate_worker.worker_kit_backend_refusal(ACP_BACKEND_KAS) is None
    spec = mate_worker.worker_session_spec(dict(BASE), "dashboard:chat-9")
    assert "jira" in spec["mcpServers"]


def test_a_stored_kiro_member_backend_runs_the_worker_on_kas():
    """The loader reads a stored ``kiro`` member backend as KAS (test_acp_backend_kas),
    and the worker takes that backend."""
    agent = SimpleNamespace(member_acp_backend=ACP_BACKEND_KAS)
    assert mate_worker.crewmate_worker_backend(agent) == ACP_BACKEND_KAS


def test_kiro_cli_cannot_carry_the_worker_and_refuses_it():
    """kiro-cli loads saved member specs but reads ``--agent`` off disk from
    ``~/.kiro/agents/``, where the worker spec is not, so a worker that lands there
    (KAS not selectable) is refused with a remedy naming the hosts that carry it."""
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.providers.acp import AcpProvider

    refusal = mate_worker.worker_kit_backend_refusal(ACP_BACKEND_KIRO)
    assert refusal is not None
    assert "(kiro)" in refusal and "agent.member_acp_backend" in refusal
    assert "kas" in refusal and "kiro," not in refusal
    client = SimpleNamespace(backend=ACP_BACKEND_KIRO)
    assert AcpProvider.mate_worker_kit_supported.fget(SimpleNamespace(_client=client)) is False
    runtime = SimpleNamespace(acp_backend=ACP_BACKEND_KIRO)
    session = SimpleNamespace(_runtime=runtime)
    assert AcpSessionProvider.mate_worker_kit_supported.fget(session) is False
    runtime.acp_backend = ACP_BACKEND_CODEX
    assert AcpSessionProvider.mate_worker_kit_supported.fget(session) is True


def test_a_crewmate_turning_off_single_tools_names_the_server(pixel_worker):
    limited = {**PIXEL, "mcpServers": {"jira": {"command": "jira-mcp", "disabledTools": ["x"]}}}
    (pixel_worker / "pixel.json").write_text(json.dumps(limited), encoding="utf-8")
    assert mate_worker.crewmate_tool_limited_servers("pixel") == ("jira",)
    from kiro_crew.session_allocation_parts.get_or_create import _worker_tool_limited_servers

    assert _worker_tool_limited_servers("dashboard:chat-9") == ("jira",)


def test_a_crewmates_own_excluded_tools_count_as_limits(pixel_worker):
    """A copied kit keeps the template's ``excludedTools``; the mirrors drop that
    field, so a server or native tool it names refuses the worker there like
    ``disabledTools`` does."""
    from kiro_crew.session_allocation_parts.get_or_create import _worker_tool_limited_servers

    excluded = {**PIXEL, "excludedTools": ["@jira/delete_issue"]}
    (pixel_worker / "pixel.json").write_text(json.dumps(excluded), encoding="utf-8")
    assert mate_worker.crewmate_tool_limited_servers("pixel") == ("jira",)
    assert _worker_tool_limited_servers("dashboard:chat-9") == ("jira",)
    # A native tool the file excludes is a limit too: no mirror projects it.
    native = {**PIXEL, "excludedTools": ["fs_write"]}
    (pixel_worker / "pixel.json").write_text(json.dumps(native), encoding="utf-8")
    assert mate_worker.crewmate_tool_limited_servers("pixel") == ("fs_write",)
    whole = {**PIXEL, "excludedTools": ["@kirocrew-core"]}
    (pixel_worker / "pixel.json").write_text(json.dumps(whole), encoding="utf-8")
    assert mate_worker.crewmate_tool_limited_servers("pixel") == ("kirocrew-core",)
    muted = {
        **PIXEL,
        "mcpServers": {"jira": {"command": "jira-mcp", "disabled": True}},
        "excludedTools": ["@jira/delete_issue"],
    }
    (pixel_worker / "pixel.json").write_text(json.dumps(muted), encoding="utf-8")
    assert mate_worker.crewmate_tool_limited_servers("pixel") == ()


def test_kas_honours_a_workers_single_tool_limits_and_the_mirrors_refuse_them(pixel_worker):
    """KAS carries the limit as the worker spec's ``excludedTools``, so it is not refused;
    claude, codex and deepseek mount servers whole, so a limited worker is refused there."""
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_DEEPSEEK
    from kiro_crew.providers.base import LLMProvider

    limited = {**PIXEL, "mcpServers": {"jira": {"command": "jira-mcp", "disabledTools": ["x"]}}}
    (pixel_worker / "pixel.json").write_text(json.dumps(limited), encoding="utf-8")
    spec = mate_worker.worker_session_spec(dict(BASE), "dashboard:chat-9")
    assert "@jira/x" in spec["excludedTools"]
    assert ACP_BACKEND_KAS in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS
    for mirror in (ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX, ACP_BACKEND_DEEPSEEK):
        assert mirror not in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS
    # Opt-in: a provider that does not declare it refuses a limited worker.
    assert LLMProvider.mate_worker_tool_limits_supported.fget(object()) is False


def test_the_agent_system_prompt_block_carries_the_crewmates_prompt(pixel_worker, monkeypatch):
    """claude, codex and deepseek get the worker's contract only from the
    ``[AGENT SYSTEM PROMPT]`` block, so it carries the worker's own principles and
    the crewmate's prompt too, in the order the on-disk worker base has them."""
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
    from kiro_crew.context_assembly.agent_spec import _crewmate_worker_prompt
    from kiro_crew.mate_worker import MATE_WORKER_PROMPT

    with_prompt = {**PIXEL, "prompt": "Follow the release SOP."}
    (pixel_worker / "pixel.json").write_text(json.dumps(with_prompt), encoding="utf-8")
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda _a: ACP_BACKEND_CLAUDE)
    block = _crewmate_worker_prompt(MATE_WORKER_AGENT, BASE["prompt"], "dashboard:chat-9")
    assert block == f"You are the worker.\n\n{MATE_WORKER_PROMPT}\n\nFollow the release SOP."
    # KAS's wire spec already carries both, so its block keeps the base.
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda _a: ACP_BACKEND_KAS)
    assert _crewmate_worker_prompt(MATE_WORKER_AGENT, BASE["prompt"], "dashboard:chat-9") == (
        BASE["prompt"]
    )
    # No session, or any other agent, keeps the prompt it was given.
    assert _crewmate_worker_prompt(MATE_WORKER_AGENT, BASE["prompt"], "") == BASE["prompt"]
    assert _crewmate_worker_prompt("pixel", "Own prompt.", "dashboard:chat-9") == "Own prompt."


@pytest.mark.parametrize("is_cc", [False, True])
def test_the_resolved_contract_block_carries_the_crewmates_prompt(
    pixel_worker, tmp_path, monkeypatch, is_cc
):
    """The worker is a default-named agent, so the block takes the default contract;
    the worker's own principles and the crewmate's prompt must still follow it on
    every provider branch, since the mirrored hosts project no spec prompt."""
    from kiro_crew.context import ContextBuilder
    from kiro_crew.memory import MemoryStore
    from kiro_crew.skills import SkillsLoader

    contract = tmp_path / "prompt.md"
    contract.write_text("You are the worker.", encoding="utf-8")
    monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: contract)
    with_prompt = {**PIXEL, "prompt": "Follow the release SOP."}
    (pixel_worker / "pixel.json").write_text(json.dumps(with_prompt), encoding="utf-8")
    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )

    def resolve(session_key: str) -> str:
        return builder._resolve_agent_prompt(
            MATE_WORKER_AGENT,
            project=None,
            mode="",
            session_key=session_key,
            is_cc=is_cc,
            private_owner=False,
            session_start=True,
        )

    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
    from kiro_crew.mate_worker import MATE_WORKER_PROMPT

    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda _a: ACP_BACKEND_CLAUDE)
    block = resolve("dashboard:chat-9")
    assert block.endswith(f"\n\n{MATE_WORKER_PROMPT}\n\nFollow the release SOP.")
    # A session with no crewmate record still runs the worker's own principles.
    other = resolve("dashboard:other")
    assert MATE_WORKER_PROMPT in other and "Follow the release SOP." not in other
    # On KAS the wire spec carries the worker's prompt; the block is the base alone.
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda _a: ACP_BACKEND_KAS)
    assert resolve("dashboard:chat-9") == "You are the worker."
