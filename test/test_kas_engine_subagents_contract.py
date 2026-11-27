"""With the ``kas_subagents`` preview on, a KAS chat's contract names the engine's sub-agent tool.

Only the ``spawn_sub_agents`` sentence of the sub-agent bullet changes; every other
sentence of that bullet and the lifecycle bullet after it stay word for word, and
"These are the ONLY subagent mechanisms" stays true. Off or never set, on any other
backend and in any other session, the contract is exactly today's. A session keeps
the reading it started with.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.acp import kas_engine_subagents as engine
from kiro_crew.acp.provider_labels import PROVIDER_LABEL_KAS
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context_assembly import agent_spec

_PROMPT = (Path(engine.__file__).parents[1] / "config" / "prompt.md").read_text(encoding="utf-8")


@pytest.fixture
def setting(monkeypatch):
    def _set(on: bool, *, sharing: bool = False) -> None:
        cfg = KiroCrewConfig()
        previews = {engine.KAS_SUBAGENTS_PREVIEW: True} if on else {}
        if sharing:
            previews["chat_runtime_sharing"] = True
        cfg.dashboard.feature_previews = previews
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))

    return _set


def _render(engine_on: bool | str) -> str:
    """The contract with the engine reading *engine_on*: a mode, or True for "replace"."""
    if isinstance(engine_on, bool):
        mode = "replace" if engine_on else ""
    else:
        mode = engine_on
    return agent_spec._render_contract(
        "kirocrew",
        _PROMPT,
        "dashboard:t1",
        cap_figure=lambda: "3",
        computer_use=lambda: False,
        browser=lambda: False,
        substitute_bot_name=lambda s: s,
        engine_subagents=lambda: mode,
    )


def test_the_shipped_prompt_holds_the_sentence_once():
    assert _PROMPT.count(engine.SPAWN_SUB_AGENTS_SENTENCE) == 1


def test_replace_names_only_the_engines_tool_where_both_spawn_tools_are_gone():
    before, after = _render(""), _render("replace")
    lead = engine.SPAWN_RUN_LEAD + engine.SPAWN_SUB_AGENTS_SENTENCE
    assert lead in before and lead not in after
    assert after == engine._with_engine_steps(before).replace(
        lead, engine.ENGINE_ONLY_SUB_AGENT_SENTENCE
    )
    # The three spawn_run steps become the in-turn steps; the fallback stays named.
    assert engine.ENGINE_STEPS in after
    assert "Report the dispatch and **END YOUR TURN**" not in after
    assert "Report the dispatch and **END YOUR TURN**" in before
    bullet = next(line for line in after.splitlines() if line.startswith("- `invoke_sub_agent`"))
    assert "These are the ONLY subagent mechanisms" in bullet
    # The spawn tools are named only for a chat whose mount was refused, which keeps them.
    head, _, tail = bullet.partition("When it is not in your tool list, ")
    assert "`spawn_run`" not in head and "`spawn_sub_agents`" not in head
    assert tail.startswith("`spawn_run` runs background subagents")
    lifecycle = next(line for line in before.splitlines() if line.startswith("- Reuse runs"))
    assert lifecycle in after.splitlines()


def test_crew_renders_todays_contract():
    assert _render(False) == agent_spec._render_contract(
        "kirocrew",
        _PROMPT,
        "dashboard:t1",
        cap_figure=lambda: "3",
        computer_use=lambda: False,
        browser=lambda: False,
        substitute_bot_name=lambda s: s,
    )


@pytest.mark.parametrize(
    ("label", "agent", "key", "value", "expected"),
    [
        (PROVIDER_LABEL_KAS, "kirocrew", "dashboard:t1", True, "replace"),
        (PROVIDER_LABEL_KAS, "kirocrew", "dashboard:t1", False, ""),
        ("kiro", "kirocrew", "dashboard:t1", True, ""),
        ("claude", "kirocrew", "dashboard:t1", True, ""),
        (None, "kirocrew", "dashboard:t1", True, ""),
        (PROVIDER_LABEL_KAS, "kirocrew-research", "dashboard:t1", True, "replace"),
        (PROVIDER_LABEL_KAS, "kirocrew-worker", "dashboard:t1", True, "replace"),
        (PROVIDER_LABEL_KAS, "kirocrew-mate-worker", "dashboard:member-ada", True, "replace"),
        (PROVIDER_LABEL_KAS, "kirocrew-conductor", "dashboard:t1", True, ""),
        (PROVIDER_LABEL_KAS, "kirocrew-mate-conductor", "dashboard:member-ada", True, ""),
        (PROVIDER_LABEL_KAS, "kirocrew", "cron:j", True, "replace"),
        (PROVIDER_LABEL_KAS, "kirocrew", "subagent:x", True, ""),
        (PROVIDER_LABEL_KAS, "kirocrew", "slack:C:T", True, "replace"),
        (PROVIDER_LABEL_KAS, "kirocrew", "dashboard:member-ada", True, "replace"),
    ],
)
def test_who_reads_the_engine_sentence(setting, label, agent, key, value, expected):
    setting(value)
    assert engine.contract_engine_subagent_mode(label, agent, key) == expected
    assert engine.contract_names_engine_subagents(label, agent, key) is bool(expected)


def test_the_sharing_preview_keeps_todays_sentence(setting):
    setting(True, sharing=True)
    assert (
        engine.contract_names_engine_subagents(PROVIDER_LABEL_KAS, "kirocrew", "dashboard:t1")
        is False
    )


def test_a_session_keeps_the_reading_it_started_with(setting):
    from kiro_crew.context import ContextBuilder

    builder = ContextBuilder.__new__(ContextBuilder)
    builder._engine_subagent_gates = {}
    import threading

    builder._engine_subagent_gates_lock = threading.Lock()
    builder._CAP_FIGURE_SESSIONS = 64

    def reading(start: bool) -> str:
        return agent_spec._session_reading(
            builder,
            builder._engine_subagent_gates,
            builder._engine_subagent_gates_lock,
            "dashboard:t1",
            lambda: agent_spec._live_engine_subagents(
                PROVIDER_LABEL_KAS, "kirocrew", "dashboard:t1"
            ),
            refresh=start,
        )

    setting(True)
    assert reading(True) == "replace"
    setting(False)
    assert reading(False) == "replace"
    assert reading(True) == ""


@pytest.mark.parametrize("value", [True, False])
def test_a_turn_with_no_agent_named_reads_like_the_default_agent_as_the_head_does(setting, value):
    """The turn renders the default contract for no agent, and the head names ``kirocrew``."""
    setting(value)
    assert agent_spec._live_engine_subagents(
        PROVIDER_LABEL_KAS, None, "dashboard:t1"
    ) is agent_spec._live_engine_subagents(PROVIDER_LABEL_KAS, "kirocrew", "dashboard:t1")


def test_the_engine_sentence_says_who_runs_and_what_a_refusal_means():
    """The engine's own tool text lists every agent it finds; the sentence says which run."""
    only = engine.ENGINE_ONLY_SUB_AGENT_SENTENCE
    # The helper (the chat's own agent) and the registered own agents run; nothing
    # else the tool lists does.
    assert "with agent `kirocrew-helper` (this chat's own agent) or one of" in only
    assert "Its list names agents it will not run" in only
    # An own agent the mount never tried is refused under the deny on every name,
    # whose quoted match is ``*``.
    assert 'a refusal that reads `deny subagent matching "*"` refuses that one agent' in only
    # What no agent the tool runs has.
    assert "No sub-agent it runs has a Kiro Crew tool or can start sub-agents" in only
    assert "none takes a model, folder or turn limit" in only
    head, _, tail = only.partition("When it is not in your tool list, ")
    assert "in this chat's own process" in head and "spawn_run" not in head
    assert tail.endswith(engine.SPAWN_SUB_AGENTS_SENTENCE)
    assert only in _render("replace")
    assert "invoke_sub_agent" not in _render(False)
