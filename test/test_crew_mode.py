"""Crew Mode: the conductor prompt.

Crew Mode is a UI over the existing ``kirocrew-conductor``: a chat with the
switch on simply runs on that agent, so there is no Crew Mode field on chat
creation or in config. This file pins the prompt section a person sees.
"""

from __future__ import annotations


def test_conductor_prompt_carries_the_intro_and_the_task_board() -> None:
    from kiro_crew import agent

    prompt = agent._CONDUCTOR_SYSTEM_PROMPT
    section = prompt.split("## Talking to the person", 1)[1].split("## If a conductor", 1)[0]
    assert "Crew Mode" in section
    assert "I'm your Conductor" in section
    assert '"Needs you" comes FIRST' in section
    assert "Task board" in section
    # A child conductor reports to its parent through work_report, not widgets.
    assert "when a conductor dispatched you" in section
    # Widgets are dashboard-only; channels and scheduled runs get plain text.
    assert "Widgets are for the dashboard chat only" in section
