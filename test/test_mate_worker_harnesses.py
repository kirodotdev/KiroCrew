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
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_DEEPSEEK,
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
    from kiro_crew.members_parts import member_sessions

    monkeypatch.setattr(member_sessions, "_CREWMATE_WORKER_PROMPT_IN_CONTEXT", set())
    return mates


def _served_by(backend: str, *keys: str) -> None:
    """Record what the live provider of each worker session *keys* answers, as the
    allocation does."""
    from kiro_crew.acp_backends import ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT
    from kiro_crew.members_parts.member_sessions import note_crewmate_worker_prompt_channel

    for key in keys:
        note_crewmate_worker_prompt_channel(
            key, backend in ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT
        )


def test_a_projecting_host_mounts_the_crewmates_own_server_on_its_worker(pixel_worker):
    """claude reads this projection; the session picks the worker's spec."""
    from kiro_crew.acp.session_mcp import session_mcp_projection

    assert mate_worker.worker_kit_backend_refusal(ACP_BACKEND_CLAUDE) is None
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
    config = SimpleNamespace(agent=SimpleNamespace(member_acp_backend=ACP_BACKEND_KAS))
    assert mate_worker.crewmate_worker_backend(config) == ACP_BACKEND_KAS


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
    runtime.acp_backend = ACP_BACKEND_CLAUDE
    assert AcpSessionProvider.mate_worker_kit_supported.fget(session) is True


@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_DEEPSEEK])
def test_codex_and_deepseek_refuse_the_worker_until_captured(backend):
    """No live capture of a worker session yet, so neither is in the kit set."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    refusal = mate_worker.worker_kit_backend_refusal(backend)
    assert refusal is not None and f"({backend})" in refusal
    session = SimpleNamespace(_runtime=SimpleNamespace(acp_backend=backend))
    assert AcpSessionProvider.mate_worker_kit_supported.fget(session) is False


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
    claude mounts servers whole, so a limited worker is refused there."""
    from kiro_crew.providers.base import LLMProvider

    limited = {**PIXEL, "mcpServers": {"jira": {"command": "jira-mcp", "disabledTools": ["x"]}}}
    (pixel_worker / "pixel.json").write_text(json.dumps(limited), encoding="utf-8")
    spec = mate_worker.worker_session_spec(dict(BASE), "dashboard:chat-9")
    assert "@jira/x" in spec["excludedTools"]
    assert ACP_BACKEND_KAS in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS
    assert ACP_BACKEND_CLAUDE not in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS
    # Opt-in: a provider that does not declare it refuses a limited worker.
    assert LLMProvider.mate_worker_tool_limits_supported.fget(object()) is False


def test_a_default_template_crewmate_adds_no_stub_to_the_block(pixel_worker, monkeypatch):
    """A crewmate copied from the default template keeps its native prompt stub,
    which the on-disk worker base opens with too. KAS's spec gains nothing from it,
    so claude's block gains nothing either: no stub text inside the block."""
    from kiro_crew.agent_parts.spec_build import _NATIVE_PROMPT_STUB
    from kiro_crew.context_assembly.agent_spec import _crewmate_worker_prompt
    from kiro_crew.mate_worker import MATE_WORKER_PROMPT

    disk_base = {**BASE, "prompt": f"{_NATIVE_PROMPT_STUB}\n\n{MATE_WORKER_PROMPT}"}
    (pixel_worker / f"{MATE_WORKER_AGENT}.json").write_text(json.dumps(disk_base), encoding="utf-8")
    stub_file = {**PIXEL, "prompt": _NATIVE_PROMPT_STUB}
    (pixel_worker / "pixel.json").write_text(json.dumps(stub_file), encoding="utf-8")
    # KAS: the wire spec built from the on-disk base gains no prompt.
    kas = mate_worker.worker_session_spec(dict(disk_base), "dashboard:chat-9")
    assert kas["prompt"] == disk_base["prompt"]
    # claude: the block is the contract plus the principles, and nothing more.
    _served_by(ACP_BACKEND_CLAUDE, "dashboard:chat-9")
    block = _crewmate_worker_prompt(MATE_WORKER_AGENT, "The full contract.", "dashboard:chat-9")
    assert block == f"The full contract.\n\n{MATE_WORKER_PROMPT}"
    assert _NATIVE_PROMPT_STUB not in block


def test_the_agent_system_prompt_block_carries_the_crewmates_prompt(pixel_worker, monkeypatch):
    """claude gets the worker's contract only from the
    ``[AGENT SYSTEM PROMPT]`` block, so it carries the worker's own principles and
    the crewmate's prompt too, in the order the on-disk worker base has them."""
    from kiro_crew.context_assembly.agent_spec import _crewmate_worker_prompt
    from kiro_crew.mate_worker import MATE_WORKER_PROMPT

    with_prompt = {**PIXEL, "prompt": "Follow the release SOP."}
    (pixel_worker / "pixel.json").write_text(json.dumps(with_prompt), encoding="utf-8")
    _served_by(ACP_BACKEND_CLAUDE, "dashboard:chat-9")
    block = _crewmate_worker_prompt(MATE_WORKER_AGENT, BASE["prompt"], "dashboard:chat-9")
    assert block == f"You are the worker.\n\n{MATE_WORKER_PROMPT}\n\nFollow the release SOP."
    # KAS's wire spec already carries both, so its block keeps the base.
    _served_by(ACP_BACKEND_KAS, "dashboard:chat-9")
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

    from kiro_crew.mate_worker import MATE_WORKER_PROMPT

    _served_by(ACP_BACKEND_CLAUDE, "dashboard:chat-9", "dashboard:other")
    block = resolve("dashboard:chat-9")
    assert block.endswith(f"\n\n{MATE_WORKER_PROMPT}\n\nFollow the release SOP.")
    # A session with no crewmate record still runs the worker's own principles.
    other = resolve("dashboard:other")
    assert MATE_WORKER_PROMPT in other and "Follow the release SOP." not in other
    # On KAS the wire spec carries the worker's prompt; the block is the base alone.
    _served_by(ACP_BACKEND_KAS, "dashboard:chat-9")
    assert resolve("dashboard:chat-9") == "You are the worker."


def _claude_worker_provider(key, **_kwargs):
    """A provider that answers the two worker questions the way claude's does."""
    from kiro_crew.acp_backends import ACP_BACKENDS_MATE_WORKER_KIT
    from kiro_crew.providers.base import LLMProvider

    class _ClaudeWorker(LLMProvider):
        def __init__(self) -> None:
            self.starts = 0

        async def start(self):
            self.starts += 1

        async def shutdown(self):
            pass

        async def stream(self, message):
            if False:
                yield None

        async def approve_tool(self, request_id, *, always=False):
            pass

        async def reject_tool(self, request_id):
            pass

        def context_usage_pct(self):
            return 0

        def is_process_alive(self):
            return False

        def is_alive(self):
            return False

        @property
        def mate_worker_kit_supported(self):
            return ACP_BACKEND_CLAUDE in ACP_BACKENDS_MATE_WORKER_KIT

        @property
        def mate_worker_prompt_in_context(self):
            from kiro_crew.acp_backends import ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT

            return ACP_BACKEND_CLAUDE in ACP_BACKENDS_MATE_WORKER_PROMPT_IN_CONTEXT

        @property
        def mate_worker_tool_limits_supported(self):
            return ACP_BACKEND_CLAUDE in ACP_BACKENDS_MATE_WORKER_TOOL_LIMITS

    return _ClaudeWorker()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limits",
    [
        {"mcpServers": {"jira": {"command": "jira-mcp", "disabledTools": ["x"]}}},
        {"excludedTools": ["@jira/delete_issue"]},
        {"excludedTools": ["fs_write"]},
    ],
    ids=["disabledTools", "excluded-server-tool", "excluded-native-tool"],
)
async def test_the_allocation_refuses_a_tool_limited_worker_on_claude(
    pixel_worker, tmp_path, monkeypatch, limits
):
    """The allocation itself, not only its source: a worker whose crewmate's file
    turns off single tools is refused on claude before anything starts."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.members_parts import member_sessions
    from kiro_crew.session import SessionManager
    from kiro_crew.session_capabilities import CapabilityStartupError

    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr(member_sessions, "_CREWMATE_WORKER_SLOTS", set())
    record = SimpleNamespace(member_id="pixel", memory_mode="persistent")
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key, **_kw: record if key == "dashboard:chat-9" else None,
    )
    key = "dashboard:chat-9"
    member_sessions.note_crewmate_worker_session(key)
    (pixel_worker / "pixel.json").write_text(json.dumps({**PIXEL, **limits}), encoding="utf-8")
    made = []

    def factory(key, **kwargs):
        provider = _claude_worker_provider(key, **kwargs)
        made.append(provider)
        return provider

    manager = SessionManager(KiroCrewConfig.load(), provider_factory=factory)
    try:
        with pytest.raises(CapabilityStartupError) as refused:
            await manager.get_or_create(key, agent=MATE_WORKER_AGENT, cwd=str(tmp_path))
        assert str(refused.value) == "capability_tool_limits_unsupported"
        assert made and made[0].starts == 0
        assert not manager.has_session(key)
        # The same worker with no limits starts.
        (pixel_worker / "pixel.json").write_text(json.dumps(PIXEL), encoding="utf-8")
        await manager.get_or_create(key, agent=MATE_WORKER_AGENT, cwd=str(tmp_path))
        assert made[-1].starts == 1
        # The allocation recorded the live provider's prompt channel for this session.
        assert member_sessions.crewmate_worker_prompt_in_context(key)
    finally:
        await manager.close_all(drain_timeout=0)


@pytest.mark.parametrize(
    "started_on, flipped_to",
    [(ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS), (ACP_BACKEND_KAS, ACP_BACKEND_CLAUDE)],
    ids=["claude-then-config-kas", "kas-then-config-claude"],
)
def test_the_prompt_channel_follows_the_live_provider_not_the_config(
    pixel_worker, monkeypatch, started_on, flipped_to
):
    """A worker started on one backend keeps that backend's channel after
    ``agent.member_acp_backend`` changes: the re-injected block after a compaction is
    the one the start built (claude: both prompts; KAS: the base alone)."""
    from kiro_crew.context_assembly.agent_spec import _crewmate_worker_prompt
    from kiro_crew.mate_worker import MATE_WORKER_PROMPT

    with_prompt = {**PIXEL, "prompt": "Follow the release SOP."}
    (pixel_worker / "pixel.json").write_text(json.dumps(with_prompt), encoding="utf-8")
    key = "dashboard:chat-9"
    provider = SimpleNamespace(_runtime=SimpleNamespace(acp_backend=started_on))
    from kiro_crew.acp.session_provider import AcpSessionProvider
    from kiro_crew.members_parts.member_sessions import note_crewmate_worker_prompt_channel

    note_crewmate_worker_prompt_channel(
        key, AcpSessionProvider.mate_worker_prompt_in_context.fget(provider)
    )
    at_start = _crewmate_worker_prompt(MATE_WORKER_AGENT, BASE["prompt"], key)
    monkeypatch.setattr(mate_worker, "crewmate_worker_backend", lambda *_a: flipped_to)
    after_compaction = _crewmate_worker_prompt(MATE_WORKER_AGENT, BASE["prompt"], key)
    assert after_compaction == at_start
    if started_on == ACP_BACKEND_CLAUDE:
        assert MATE_WORKER_PROMPT in at_start and "Follow the release SOP." in at_start
    else:
        assert at_start == BASE["prompt"]


def test_the_projection_refuses_limits_added_after_the_allocation_check(pixel_worker):
    """The allocation's limit check and the projection read the crewmate file apart.
    An edit landing between them must still refuse on claude: the projection judges
    the limits on the very read it builds the worker from. KAS, which honours the
    limits, builds the spec."""
    from kiro_crew.acp.session_mcp import session_mcp_projection
    from kiro_crew.session_allocation_parts.get_or_create import _worker_tool_limited_servers
    from kiro_crew.session_capabilities import CapabilityStartupError

    key = "dashboard:chat-9"
    assert _worker_tool_limited_servers(key) == ()  # the allocation lets it through
    limited = {**PIXEL, "excludedTools": ["@jira/delete_issue"]}
    (pixel_worker / "pixel.json").write_text(json.dumps(limited), encoding="utf-8")
    with pytest.raises(CapabilityStartupError) as refused:
        session_mcp_projection(MATE_WORKER_AGENT, session_key=key)
    assert str(refused.value) == "capability_tool_limits_unsupported"
    projection = session_mcp_projection(
        MATE_WORKER_AGENT, session_key=key, worker_tool_limits_honoured=True
    )
    assert "jira" in {e.get("name") for e in projection.servers}
