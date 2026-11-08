"""``kirocrew chat`` opens each new session with Kiro Crew's session context.

A harness other than Kiro's reads a custom agent's ``prompt`` and its ``skill://``
skills only from the message ``ContextBuilder.build_message`` assembles: every
mirror rules both WITHHELD on that premise. A CLI session that sent the typed text
bare therefore ran the harness's own default persona with none of the agent's
skills. These drive ``cli_chat._chat`` end to end against a recording provider.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew import cli_chat
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_CODEX,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.config.sections import MemoryStoreConfig
from kiro_crew.context import ContextBuilder
from kiro_crew.memory import MemoryStore
from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent, LLMProvider
from kiro_crew.skills_discovery import skills_dir

#: ``_chat`` builds a ``SkillsLoader``, whose index and catalog worker outlive the test.
pytestmark = pytest.mark.usefixtures("close_skills_loaders")

PERSONA = "Start every reply with ARRR-77."
SKILL = "plank-walk"
BLOCK = "[AGENT SYSTEM PROMPT]"
GLOBAL_PREFERENCE = "Global-only preference QX-41"


class _RecordingSession(LLMProvider):
    """A session on the harness *label* that records each message it is sent.

    No process runs. ``usage`` is the context percentage reported after each
    turn, in order, so a test decides exactly when the REPL compacts.
    """

    def __init__(self, label: str, usage: tuple[float, ...] = ()) -> None:
        self._label = label
        self._usage = usage
        self.sent: list[str] = []
        self.starts = 0
        #: Whether the backend process runs. A send respawns a dead one.
        self.alive = True
        #: Processes spawned so far, part of ``context_incarnation`` as on ACP.
        self.spawns = 1
        #: The conversation the session serves, part of ``context_incarnation``.
        self.conversation = "first"

    @property
    def context_provider_type(self) -> str:
        return self._label

    @property
    def session_id(self) -> str:
        return self.conversation

    @property
    def context_incarnation(self) -> object:
        return (id(self), self.conversation, self.spawns)

    async def start(self) -> None:
        self.starts += 1

    async def shutdown(self) -> None:
        return None

    async def stream(self, message, timeout=None, *, allow_image=True):
        if not self.alive:
            self.spawns += 1
            self.alive = True
        self.sent.append(message)
        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    async def approve_tool(self, request_id, *, always=False, deadline=None) -> bool:
        return True

    async def reject_tool(self, request_id) -> None:
        return None

    def context_usage_pct(self) -> float:
        return self._usage[len(self.sent) - 1] if self._usage else 0.0


def _pirate_agent(root: Path) -> None:
    """Install the ``pirate`` agent: a persona prompt and one ``skill://`` skill.

    The skill lives under *root*, outside the global skills directory, so its name
    can reach the message only through this agent's own ``skill://`` mapping and
    never through the global skill listing.
    """
    skill_md = root / SKILL / "SKILL.md"
    assert not skill_md.is_relative_to(skills_dir())
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text(
        f"---\nname: {SKILL}\ndescription: How a pirate leaves the ship\n---\n\nWalk it.\n",
        encoding="utf-8",
    )
    agents = kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "pirate.json").write_text(
        json.dumps(
            {
                "name": "pirate",
                "prompt": PERSONA,
                "resources": [f"skill://{skill_md.as_posix()}"],
            }
        ),
        encoding="utf-8",
    )


def _run_on(monkeypatch, provider: _RecordingSession) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CLAUDE
    monkeypatch.setattr(cli_chat.KiroCrewConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(cli_chat, "build_provider_factory", lambda config: lambda *a, **k: provider)
    return cfg


def _type(monkeypatch, *lines: str, between=lambda line: None) -> None:
    """Feed *lines* to the REPL, calling *between* with each before it is read."""
    feed = iter(lines)

    def _input(_prompt: str = "") -> str:
        try:
            line = next(feed)
        except StopIteration:
            raise EOFError from None
        between(line)
        return line

    monkeypatch.setattr("builtins.input", _input)


def _carries_the_agent(message: str, typed: str) -> bool:
    return (
        f"{BLOCK}\n{PERSONA}\n" in message and SKILL in message and message.rstrip().endswith(typed)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [PROVIDER_LABEL_CLAUDE, PROVIDER_LABEL_CODEX])
async def test_a_single_message_carries_the_agents_prompt_and_skills(
    monkeypatch, label, tmp_path
) -> None:
    _pirate_agent(tmp_path / "agent-skills")
    provider = _RecordingSession(label)
    _run_on(monkeypatch, provider)

    await cli_chat._chat("say hello", None, agent="pirate")

    assert len(provider.sent) == 1, provider.sent
    assert _carries_the_agent(provider.sent[0], "say hello"), provider.sent[0]


@pytest.mark.asyncio
async def test_the_repl_sends_context_once_per_session(monkeypatch, capsys, tmp_path) -> None:
    """The first turn and the first turn after a compaction restart carry it; the
    turn between them goes to a session that already holds it, so it is sent bare."""
    _pirate_agent(tmp_path / "agent-skills")
    # Turn two crosses the compaction threshold, so the REPL restarts the session.
    provider = _RecordingSession(PROVIDER_LABEL_CLAUDE, usage=(10.0, 99.0, 10.0))
    _run_on(monkeypatch, provider)
    _type(monkeypatch, "one", "two", "three")

    await cli_chat._chat(None, None, agent="pirate")

    assert provider.starts == 2
    first, second, third = provider.sent
    assert _carries_the_agent(first, "one"), first
    assert second == "two"
    assert _carries_the_agent(third, "three"), third


@pytest.mark.asyncio
async def test_a_harness_command_goes_as_typed_and_leaves_the_context_owed(
    monkeypatch, tmp_path
) -> None:
    """A harness runs a command only when it opens the prompt, so a command is sent
    bare and the next ordinary turn carries the context. A quick prompt is a macro,
    not a command, and a command that clears the history owes the context again."""
    _pirate_agent(tmp_path / "agent-skills")
    provider = _RecordingSession(PROVIDER_LABEL_CLAUDE)
    _run_on(monkeypatch, provider)
    _type(monkeypatch, "/context", "/plain one", "/clear", "two", "three")

    await cli_chat._chat(None, None, agent="pirate")

    command, quick, clear, second, third = provider.sent
    assert command == "/context"
    assert f"{BLOCK}\n{PERSONA}\n" in quick, quick
    assert clear == "/clear"
    assert _carries_the_agent(second, "two"), second
    assert third == "three"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["process exited", "conversation replaced"])
async def test_a_session_the_backend_replaced_gets_the_context_again(
    monkeypatch, tmp_path, change
) -> None:
    """A replaced conversation gets the context on its first turn. A backend process
    that exits between turns is respawned by the next send, which goes as typed
    because only that send shows the new process; the turn after it carries it."""
    _pirate_agent(tmp_path / "agent-skills")
    provider = _RecordingSession(PROVIDER_LABEL_CLAUDE)
    _run_on(monkeypatch, provider)

    def _between(line: str) -> None:
        if line != "two":
            return
        if change == "process exited":
            provider.alive = False
        else:
            provider.conversation = "second"

    _type(monkeypatch, "one", "two", "three", between=_between)

    await cli_chat._chat(None, None, agent="pirate")

    first, second, third = provider.sent
    assert _carries_the_agent(first, "one"), first
    if change == "process exited":
        assert second == "two"
        assert _carries_the_agent(third, "three"), third
    else:
        assert _carries_the_agent(second, "two"), second
        assert third == "three"


class _KiroHarnessSession(_RecordingSession):
    """A session on kiro-cli or KAS, named by the provider capability *flag*."""

    def __init__(self, flag: str) -> None:
        super().__init__(PROVIDER_LABEL_CLAUDE)
        self._flag = flag

    @property
    def is_kiro_backend(self) -> bool:
        return self._flag == "is_kiro_backend"

    @property
    def is_kas_backend(self) -> bool:
        return self._flag == "is_kas_backend"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["is_kiro_backend", "is_kas_backend"])
async def test_a_kiro_harness_gets_the_typed_text_as_before(monkeypatch, tmp_path, flag) -> None:
    """kiro-cli and KAS load the agent themselves, so the CLI adds no session context."""
    _pirate_agent(tmp_path / "agent-skills")
    provider = _KiroHarnessSession(flag)
    _run_on(monkeypatch, provider)

    await cli_chat._chat("say hello", None, agent="pirate")
    _type(monkeypatch, "one", "two")
    await cli_chat._chat(None, None, agent="pirate")

    assert provider.sent == ["say hello", "one", "two"]


@pytest.mark.asyncio
async def test_a_failed_build_sends_the_typed_text_and_retries_next_turn(
    monkeypatch, capsys, tmp_path
) -> None:
    _pirate_agent(tmp_path / "agent-skills")
    provider = _RecordingSession(PROVIDER_LABEL_CLAUDE)
    _run_on(monkeypatch, provider)
    real_build = ContextBuilder.build_message
    builds: list[str] = []

    def _fails_once(self, text, *args, **kwargs):
        builds.append(text)
        if len(builds) == 1:
            raise OSError("memory file unreadable")
        return real_build(self, text, *args, **kwargs)

    monkeypatch.setattr(ContextBuilder, "build_message", _fails_once)
    _type(monkeypatch, "one", "two", "three")

    await cli_chat._chat(None, None, agent="pirate")

    first, second, third = provider.sent
    assert first == "one"
    assert _carries_the_agent(second, "two"), second
    assert third == "three"
    assert "Session context not built (memory file unreadable)" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "reads_global"), [("pirate", True), ("acme", False)])
async def test_only_a_session_on_the_default_store_reads_global_memory(
    monkeypatch, tmp_path, agent, reads_global
) -> None:
    """A member bound to a named store reads that store or nothing, never Global: the
    CLI does not bind the store, so its session context withholds memory and lessons."""
    _pirate_agent(tmp_path / "agent-skills")
    MemoryStore().add_preference(GLOBAL_PREFERENCE)
    provider = _RecordingSession(PROVIDER_LABEL_CLAUDE)
    cfg = _run_on(monkeypatch, provider)
    cfg.agents["acme"] = KiroCrewAgentConfig(kiro_agent="pirate", memory_store="acme")
    cfg.memory_stores["acme"] = MemoryStoreConfig()

    await cli_chat._chat("say hello", None, agent=agent)

    (message,) = provider.sent
    assert _carries_the_agent(message, "say hello"), message
    assert (GLOBAL_PREFERENCE in message) is reads_global, message
    assert ("[CONTEXT SCOPE]" in message) is not reads_global, message
