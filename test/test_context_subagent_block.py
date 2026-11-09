"""A crewmate's conductor gets its own delegation rule where the sub-agent span was.

The shipped prompt's default is "do the task yourself, spawn only when", taught
through ``spawn_run``. The conductor mounts none of the spawn tools and its own
instructions dispatch longer work to a worker session, so it gets a short rule
pointing at those instructions instead. Outside its own thread it has no worker
either, so there the rule says to do the work in the chat. Every other agent
keeps the span.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.context import ContextBuilder
from kiro_crew.context_assembly import sections
from kiro_crew.mate_worker import (
    MATE_CONDUCTOR_AGENT,
    MATE_CONDUCTOR_EXCLUDED_TOOLS,
    MATE_WORKER_AGENT,
)
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

_SHIPPED_PROMPT = Path(sections.__file__).resolve().parents[1] / "config" / "prompt.md"
# Phrases only the full span carries.
_SPAN_ONLY = (
    "Do the task yourself by default",
    "Submit all independent tasks in ONE `spawn_run",
    "Scope context deliberately",
    "Reuse runs instead of re-spawning",
    # The other tools the conductor's rule names as missing.
    "- `task_run`:",
    "- `workflow_run(intent=",
)


@pytest.fixture
def builder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ContextBuilder:
    monkeypatch.setattr("kiro_crew.context._prompt_path", lambda **_kw: _SHIPPED_PROMPT)
    return ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
    )


# The bot's own thread, as the session-map alias spells it.
_OWN_THREAD = "dashboard:member-aria"


def _resolve(
    builder: ContextBuilder, agent: str | None, session_key: str = "dashboard:subagents"
) -> str:
    return builder._resolve_agent_prompt(
        agent,
        project=None,
        mode="",
        session_key=session_key,
        is_cc=False,
        private_owner=False,
        session_start=True,
    )


class TestTheConductor:
    def test_gets_its_delegation_rule_instead_of_the_span(self, builder: ContextBuilder) -> None:
        prompt = _resolve(builder, MATE_CONDUCTOR_AGENT, _OWN_THREAD)
        for phrase in _SPAN_ONLY:
            assert phrase not in prompt, phrase
        assert "### Delegation" in prompt
        assert "[HOW YOU WORK]" in prompt
        assert not re.search(r"\{\{[A-Z_]+\}\}", prompt)

    def test_keeps_the_capabilities_around_the_span(self, builder: ContextBuilder) -> None:
        prompt = _resolve(builder, MATE_CONDUCTOR_AGENT, _OWN_THREAD)
        assert "- `resource_status`:" in prompt
        assert "- `learn_add`:" in prompt
        assert prompt.index("- `resource_status`:") < prompt.index("### Delegation")
        assert prompt.index("### Delegation") < prompt.index("- `learn_add`:")

    # A folder or agent-picker chat, and an agent channel.
    @pytest.mark.parametrize("session_key", ["dashboard:chat-1", "slack:C0123ABC"])
    def test_outside_its_own_thread_it_does_the_work_itself(
        self, builder: ContextBuilder, session_key: str
    ) -> None:
        prompt = _resolve(builder, MATE_CONDUCTOR_AGENT, session_key)
        for phrase in _SPAN_ONLY:
            assert phrase not in prompt, phrase
        assert "### Delegation" in prompt
        assert "[HOW YOU WORK]" not in prompt
        assert "[CREW MEMBER OPERATING MODE]" not in prompt
        assert not re.search(r"\{\{[A-Z_]+\}\}", prompt)

    @pytest.mark.parametrize("own_thread", [True, False])
    def test_the_tools_it_names_as_missing_really_are(self, own_thread: bool) -> None:
        excluded = {ref.rsplit("/", 1)[1] for ref in MATE_CONDUCTOR_EXCLUDED_TOOLS}
        rule = sections.subagent_section(
            f"{sections.SUBAGENTS_BEGIN}\nX\n{sections.SUBAGENTS_END}",
            conductor=True,
            own_thread=own_thread,
        )
        assert set(re.findall(r"`([a-z_]+)`", rule)) <= excluded


@pytest.mark.parametrize("agent", [None, "kirocrew", MATE_WORKER_AGENT])
def test_every_other_agent_keeps_the_span(builder: ContextBuilder, agent: str | None) -> None:
    prompt = _resolve(builder, agent)
    for phrase in _SPAN_ONLY:
        assert phrase in prompt, (agent, phrase)
    assert "### Delegation" not in prompt
    assert not re.search(r"\{\{[A-Z_]+\}\}", prompt)


def test_kept_span_reads_as_if_the_file_had_no_markers() -> None:
    raw = _SHIPPED_PROMPT.read_text(encoding="utf-8")
    unmarked = raw.replace(f"{sections.SUBAGENTS_BEGIN}\n", "").replace(
        f"\n{sections.SUBAGENTS_END}", ""
    )
    assert sections.subagent_section(raw, conductor=False) == unmarked


@pytest.mark.parametrize(
    "text", [f"a\n{sections.SUBAGENTS_BEGIN}\nb", f"a\n{sections.SUBAGENTS_END}\nb"]
)
def test_an_unpaired_marker_is_dropped_and_its_text_kept(text: str) -> None:
    for conductor in (True, False):
        assert sections.subagent_section(text, conductor=conductor) == "a\n\nb"


def test_a_caller_without_the_agent_keeps_the_span() -> None:
    out = ContextBuilder._resolve_prompt_templates(
        f"a\n{sections.SUBAGENTS_BEGIN}\nX\n{sections.SUBAGENTS_END}\nb", "cli:local"
    )
    assert out.startswith("a\nX\nb")
