"""A channel member that names a crewmate starts that crewmate's template.

The Add Agent form could send the roster's default crewmate alias (``default``).
Passed to ``get_or_create`` as the kiro-cli agent, it reached the skill view as an
agent with no spec, and every turn failed with "Agent 'default' has no prepared
skill discovery view" (CREW-27282).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import channel as channel_mod
from kiro_crew.channel import Channel, run_channel_agent
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig


def _cfg() -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents["default"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    return cfg


def test_a_crewmate_name_starts_its_template_under_its_crew_identity() -> None:
    with patch.object(channel_mod.KiroCrewConfig, "load", return_value=_cfg()):
        kwargs = channel_mod._session_agent_kwargs("default")
    assert kwargs == {"agent": "kirocrew", "crew_agent": "default"}


def test_a_template_name_and_no_name_pass_through() -> None:
    with patch.object(channel_mod.KiroCrewConfig, "load", return_value=_cfg()):
        assert channel_mod._session_agent_kwargs("deep-researcher") == {
            "agent": "deep-researcher",
            "crew_agent": None,
        }
        assert channel_mod._session_agent_kwargs("") == {"agent": None}


@pytest.mark.asyncio
async def test_run_channel_agent_hands_the_template_to_get_or_create() -> None:
    ch = Channel(id="ch1", topic="t", _broadcast_fn=lambda _t, _d: None)
    agent = ch.add_agent(role="Helper", agent_name="default", task="t")
    assert agent is not None
    captured: dict = {}

    async def get_or_create(key, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after the call under test")

    sessions = MagicMock()
    sessions.get_or_create = get_or_create
    with patch.object(channel_mod.KiroCrewConfig, "load", return_value=_cfg()):
        await run_channel_agent(agent, ch, sessions)

    assert captured["agent"] == "kirocrew"
    assert captured["crew_agent"] == "default"
    assert agent.state == "failed"
