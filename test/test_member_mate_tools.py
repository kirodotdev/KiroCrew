"""Every new crewmate gets its own-record tools, and the text it hears matches what it has.

``member_mate_tools.grant_mate_tools_to_new_member`` adds ``kirocrew-mate`` to a
just-created crewmate by editing its OWNED config file in ``mates/agents/`` in
place (no fork, no enroll) -- see the grant-mechanism tests in
``test_mate_owned_config.py``. Here we cover the WORDING that follows from whether
the tools are present, plus the spec predicate and project-shadow reads.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from agent_capabilities_helpers import _accepting_kiro_cli  # noqa: F401

from kiro_crew import agent, member_mate_tools
from kiro_crew.context_assembly import member as member_ctx
from kiro_crew.dashboard import goal_setup, mate_welcome

MATE_TOOLS = ("set_goal", "set_key_results", "set_check_ins", "update_self")

TRANSPORT = {"command": "/opt/kirocrew", "args": ["mcp", "mate"]}


def test_a_project_template_shadows_the_global_one(tmp_path, monkeypatch) -> None:
    project = tmp_path / "proj"
    (project / ".kiro" / "agents").mkdir(parents=True)
    glob = tmp_path / "glob"
    glob.mkdir()
    monkeypatch.setattr("kiro_crew.config.loader.default_project_dir", lambda _: str(project))
    monkeypatch.setattr(agent, "kiro_agents_dir_path", lambda: glob)
    parent = {
        "name": "parent",
        "prompt": "original",
        "tools": ["read"],
        "mcpServers": {"search": {"command": "search", "args": ["old"]}},
    }
    with_mate = {
        **parent,
        "tools": ["@kirocrew-mate"],
        "mcpServers": {"kirocrew-mate": dict(TRANSPORT)},
    }
    (glob / "parent.json").write_text(json.dumps(with_mate), encoding="utf-8")
    shadow = project / ".kiro" / "agents" / "parent.json"
    shadow.write_text(json.dumps(parent), encoding="utf-8")
    binding = SimpleNamespace(kiro_agent="parent", workspace="default")
    assert member_mate_tools.binding_has_mate_tools(binding) is False
    # An unreadable project copy keeps the default wording and never reads the
    # global template in its place.
    shadow.write_bytes(b"\xff")
    monkeypatch.setattr(
        agent, "agent_spec_path", lambda *a, **k: pytest.fail("fell back to a scan")
    )
    assert member_mate_tools.binding_has_mate_tools(binding) is True
    traversal = SimpleNamespace(kiro_agent="../parent", workspace="default")
    assert member_mate_tools.binding_has_mate_tools(traversal) is True


def test_spec_has_mate_tools_needs_the_server_and_a_tools_ref() -> None:
    entry = {"kirocrew-mate": dict(TRANSPORT)}
    assert member_mate_tools.spec_has_mate_tools({"mcpServers": entry, "tools": ["*"]})
    assert member_mate_tools.spec_has_mate_tools(
        {"mcpServers": entry, "tools": ["@kirocrew-mate/set_goal"]}
    )
    assert not member_mate_tools.spec_has_mate_tools({"mcpServers": entry, "tools": ["read"]})
    assert not member_mate_tools.spec_has_mate_tools({"tools": ["@kirocrew-mate"]})
    muted = {"kirocrew-mate": {**TRANSPORT, "disabled": True}}
    assert not member_mate_tools.spec_has_mate_tools({"mcpServers": muted, "tools": ["*"]})


def test_the_identity_sentence_names_the_tools_only_when_they_are_there() -> None:
    assert all(t in member_ctx._MATE_TOOLS_IDENTITY for t in MATE_TOOLS)
    assert not any(t in member_ctx._NO_MATE_TOOLS_IDENTITY for t in MATE_TOOLS)
    assert "Goals tab" in member_ctx._NO_MATE_TOOLS_IDENTITY


def test_the_first_welcomes_follow_the_tools() -> None:
    entry = SimpleNamespace(display_name="Scout", description="")
    with_tools = mate_welcome.goal_kickoff(entry, "scout")
    without = mate_welcome.goal_kickoff(entry, "scout", has_mate_tools=False)
    assert "set_goal" in with_tools
    assert "set_goal" not in without and "Goals tab" in without
    described = SimpleNamespace(display_name="Scout", description="ship notes")
    known = mate_welcome.goal_kickoff(described, "scout", has_mate_tools=False)
    assert "ship notes" in known and "set_goal" not in known
    for others in (False, True):
        mate = mate_welcome.welcome_kickoff(entry, has_other_crewmates=others)
        plain = mate_welcome.welcome_kickoff(
            entry, has_other_crewmates=others, has_mate_tools=False
        )
        assert "update_self" in mate
        assert "update_self" not in plain and "crewmate page" in plain


GOAL = {"id": "g1", "text": "Keep the release notes current"}


def test_the_goal_nudges_follow_the_tools() -> None:
    rec = {"goals": [GOAL], "key_results": [], "schedules": {}}
    assert "set_key_results" in goal_setup.kickoff_prompt(rec, "g1")
    bare = goal_setup.kickoff_prompt(rec, "g1", has_mate_tools=False)
    assert bare is not None and "set_key_results" not in bare and "Goals tab" in bare
    krs = {**rec, "key_results": [{"id": "k1", "text": "notes out", "goal_ids": ["g1"]}]}
    assert "set_check_ins" in goal_setup.milestones_prompt(krs)
    off = goal_setup.milestones_prompt(krs, has_mate_tools=False)
    assert off is not None and "set_check_ins" not in off and "Goals tab" in off
    removed = {
        "goals": [],
        "key_results": [{"id": "k1", "text": "notes out", "goal_ids": []}],
        "removed_goal": {"goal": GOAL, "index": 0, "at": 1, "key_result_ids": ["k1"]},
    }
    assert "set_key_results" in goal_setup.removed_prompt(removed, "g1")
    gone = goal_setup.removed_prompt(removed, "g1", has_mate_tools=False)
    assert gone is not None and "set_key_results" not in gone and "Goals tab" in gone


def test_the_shipped_defaults_auto_approve_every_mate_grant() -> None:
    from importlib import resources

    defaults = json.loads(
        resources.files("kiro_crew.config").joinpath("defaults.json").read_text(encoding="utf-8")
    )
    shipped = {t for t in defaults["allowedTools"] if t.startswith("@kirocrew-mate/")}
    assert shipped == set(agent._MATE_AUTO_GRANTS)


def test_a_found_capability_goes_straight_to_its_card():
    """The card is the question to the user, so nothing asks first in chat."""
    from kiro_crew import mcp_mate

    assert "request_capabilities right away" in member_ctx._MATE_TOOLS_IDENTITY
    identity = member_ctx._MATE_TOOLS_IDENTITY
    assert "before you tell the user something is not available" in identity
    # The hand-off result and a worker's MISSING CAPABILITY line both lead to the catalog.
    assert "session_create" in identity and "MISSING CAPABILITY" in identity
    assert "card is your question to the user" in member_ctx._MATE_TOOLS_IDENTITY
    find = next(t for t in mcp_mate._tool_definitions() if t["name"] == "find_capabilities")
    assert "straight away" in find["description"]
