"""On ``agent.subagents = "engine"`` a KAS chat's contract names the engine's sub-agent tool.

Only the ``spawn_sub_agents`` sentence of the sub-agent bullet changes; every other
sentence of that bullet and the lifecycle bullet after it stay word for word, and
"These are the ONLY subagent mechanisms" stays true. On ``"crew"``, on any other
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
    def _set(value: str, *, sharing: bool = False) -> None:
        cfg = KiroCrewConfig()
        cfg.agent.subagents = value
        if sharing:
            cfg.dashboard.feature_previews = {"chat_runtime_sharing": True}
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))

    return _set


def _render(engine_on: bool) -> str:
    return agent_spec._render_contract(
        "kirocrew",
        _PROMPT,
        "dashboard:t1",
        cap_figure=lambda: "3",
        computer_use=lambda: False,
        browser=lambda: False,
        substitute_bot_name=lambda s: s,
        engine_subagents=lambda: engine_on,
    )


def test_the_shipped_prompt_holds_the_sentence_once():
    assert _PROMPT.count(engine.SPAWN_SUB_AGENTS_SENTENCE) == 1


def test_engine_changes_only_the_spawn_sub_agents_sentence():
    before, after = _render(False), _render(True)
    assert engine.SPAWN_SUB_AGENTS_SENTENCE in before
    assert engine.SPAWN_SUB_AGENTS_SENTENCE not in after
    assert engine.ENGINE_SUB_AGENT_SENTENCE in after
    assert after == before.replace(
        engine.SPAWN_SUB_AGENTS_SENTENCE, engine.ENGINE_SUB_AGENT_SENTENCE
    )
    bullet = next(line for line in after.splitlines() if line.startswith("- `spawn_run`:"))
    assert "These are the ONLY subagent mechanisms" in bullet
    assert "`invoke_sub_agent`" in bullet and "`spawn_run`" in bullet
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
        (PROVIDER_LABEL_KAS, "kirocrew", "dashboard:t1", "engine", True),
        (PROVIDER_LABEL_KAS, "kirocrew", "dashboard:t1", "crew", False),
        ("kiro", "kirocrew", "dashboard:t1", "engine", False),
        ("claude", "kirocrew", "dashboard:t1", "engine", False),
        (None, "kirocrew", "dashboard:t1", "engine", False),
        (PROVIDER_LABEL_KAS, "kirocrew-conductor", "dashboard:t1", "engine", False),
        (PROVIDER_LABEL_KAS, "kirocrew", "cron:j", "engine", False),
        (PROVIDER_LABEL_KAS, "kirocrew", "subagent:x", "engine", False),
        (PROVIDER_LABEL_KAS, "kirocrew", "slack:C:T", "engine", False),
        (PROVIDER_LABEL_KAS, "kirocrew", "dashboard:member-ada", "engine", False),
    ],
)
def test_who_reads_the_engine_sentence(setting, label, agent, key, value, expected):
    setting(value)
    assert engine.contract_names_engine_subagents(label, agent, key) is expected


def test_the_sharing_preview_keeps_todays_sentence(setting):
    setting("engine", sharing=True)
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

    def reading(start: bool) -> bool:
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

    setting("engine")
    assert reading(True) is True
    setting("crew")
    assert reading(False) is True
    assert reading(True) is False


@pytest.mark.parametrize("value", ["engine", "crew"])
def test_a_turn_with_no_agent_named_reads_like_the_default_agent_as_the_head_does(setting, value):
    """The turn renders the default contract for no agent, and the head names ``kirocrew``."""
    setting(value)
    assert agent_spec._live_engine_subagents(
        PROVIDER_LABEL_KAS, None, "dashboard:t1"
    ) is agent_spec._live_engine_subagents(PROVIDER_LABEL_KAS, "kirocrew", "dashboard:t1")
