"""A crew or inherited spawn's card names the agent the run executes on.

``spawn_run(crew=...)`` passes no ``agent``, so ``SubagentInfo.agent`` is empty
while the run executes on its execution record's template. The dashboard store
draws an empty agent as "kirocrew", so every frame that feeds the Subagents
panel must send the resolved name, not the raw spawn argument.
"""

from __future__ import annotations

from types import SimpleNamespace

# Import cycle workaround: see test_subagent_snapshot_idle_secs.py for rationale.
import kiro_crew.dashboard.handlers  # noqa: F401
from kiro_crew.dashboard.ws import build_subagent_snapshot
from kiro_crew.subagent_parts.run_record import SubagentInfo


def _info(agent: str, template_id: str | None) -> SubagentInfo:
    execution = None if template_id is None else SimpleNamespace(template_id=template_id)
    return SubagentInfo(
        id="a1",
        task="do a thing",
        parent_session_key="dashboard:main",
        agent=agent,
        execution_context=execution,  # type: ignore[arg-type]
    )


def test_run_agent_falls_back_to_the_execution_template():
    assert _info("", "crew-agent").run_agent == "crew-agent"


def test_run_agent_prefers_the_named_agent():
    assert _info("named", "crew-agent").run_agent == "named"


def test_run_agent_is_empty_when_nothing_is_known():
    assert _info("", None).run_agent == ""
    assert _info("", "").run_agent == ""


def test_snapshot_names_the_crew_agent_for_a_crew_spawn():
    data = build_subagent_snapshot(_info("", "crew-agent"), now=1000.0)
    assert data["agent"] == "crew-agent"


def test_crew_log_spawn_record_names_the_crew_agent(monkeypatch):
    """A restarted gateway rebuilds finished cards from ``subagent/spawned``, so
    that record must carry the run's agent too, not the empty spawn argument."""
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.subagent_manager.admission.gate_reporting import (
        _record_crew_log_spawn_started,
    )

    recorded: list[dict] = []
    monkeypatch.setattr(crew_log_emit, "enabled", lambda: True)
    monkeypatch.setattr(crew_log_emit, "open_child_origin", lambda _id: ("sid-1", 3))
    monkeypatch.setattr(
        crew_log_emit, "on_subagent_spawned", lambda sid, turn, **data: recorded.append(data)
    )

    _record_crew_log_spawn_started(None, _info("", "crew-agent"))  # type: ignore[arg-type]

    assert [entry["agent"] for entry in recorded] == ["crew-agent"]
