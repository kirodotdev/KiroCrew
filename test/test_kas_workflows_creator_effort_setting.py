"""``agent.kas_creator_effort``: the config value ``kas_workflows.chat`` reads for the creator step.

The engine is the setting's first reader (:func:`kiro_crew.kas_workflows.chat.creator_effort_setting`),
so the field, its loader and its levels live here: a KAS level is kept, anything
else is KAS's own default (""). The dashboard control comes with the dashboard PR.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.config.loader_builders import _build_agent_config
from kiro_crew.config.sections import KAS_CREATOR_EFFORT_LEVELS
from kiro_crew.config.sections_agent import AgentConfig
from kiro_crew.kas_workflows import chat


def test_the_levels_are_kas_workflow_creator_effort_levels() -> None:
    assert KAS_CREATOR_EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")


def test_the_field_defaults_to_kas_own_default() -> None:
    assert AgentConfig().kas_creator_effort == ""
    assert _build_agent_config({}).kas_creator_effort == ""


@pytest.mark.parametrize("level", KAS_CREATOR_EFFORT_LEVELS)
def test_the_loader_keeps_a_kas_level(level: str) -> None:
    assert _build_agent_config({"kas_creator_effort": level}).kas_creator_effort == level


@pytest.mark.parametrize("value", ["HIGH", "extreme", 3, None, ""])
def test_the_loader_reads_anything_else_as_the_default(value: object) -> None:
    assert _build_agent_config({"kas_creator_effort": value}).kas_creator_effort == ""


def test_the_chat_reads_the_configured_level(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(agent=_build_agent_config({"kas_creator_effort": "xhigh"}))
    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: cfg)
    assert chat.creator_effort_setting() == "xhigh"


def test_the_chat_reads_an_unset_level_as_kas_default(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SimpleNamespace(agent=_build_agent_config({}))
    monkeypatch.setattr("kiro_crew.config.live.snapshot", lambda: cfg)
    assert chat.creator_effort_setting() == ""
