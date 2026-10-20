"""The packaged Crew Members page's code-coupled claims are pinned to the code.

``scripts/docs_lint.py`` gates a doc's paths and links, never what it claims, so
``src/kiro_crew/docs/crew-members.md`` is free to name a tool that was renamed, a
config field that was dropped, or a refusal code that moved, with the lint
green. The claims most exposed to that are the ones an agent ACTS on — the
``crew=`` spawn argument, the refusal code a delegation comes back with, and the
config fields the parts table names — because each is a literal the reader
copies into a call.

Each assertion below is paired with the sentence in the page that states it, and
asserted against the code rather than against a second copy of the prose: where
the claim is about a VALUE the code owns, the value is imported; where it is
about a name the tool table declares, the declaration is read.
"""

from pathlib import Path

import pytest

DOC = Path(__file__).parent.parent / "src" / "kiro_crew" / "docs" / "crew-members.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_page_names_no_retired_routing_tool(doc_text: str) -> None:
    """§Delegating work to a crewmate says there is no routing tool, and the
    dispatch table agrees: neither retired name is callable."""
    from kiro_crew.mcp_tools.control import HANDLERS

    for tool in ("select_crew", "route_crew"):
        assert tool not in HANDLERS, f"{tool} was retired and must not be dispatched"
        assert f"`{tool}" not in doc_text, f"the page still tells an agent to call {tool}"


def test_spawn_still_takes_the_crew_argument_the_page_insists_on(doc_text: str) -> None:
    """The page's copyable call is `crew=`, and both arguments must still exist."""
    from kiro_crew.mcp_tools.spawn import schemas

    spawn = next(t for t in schemas() if t["name"] == "spawn_run")
    props = spawn["inputSchema"]["properties"]
    assert "crew" in props and "agent" in props
    assert 'spawn_run(crew="<name>")' in doc_text


def test_every_crewmate_can_be_delegated_to_by_name(doc_text: str) -> None:
    """§Delegating work to a crewmate says any crewmate can be named."""
    from kiro_crew.dashboard.handlers import messaging

    dashboard = Path(__file__).parent.parent / "src" / "kiro_crew" / "dashboard"
    # The spawn route runs from a messaging_api owner the facade composes, so the
    # handler is the facade and every owner, and the scan fails if the route lives
    # in a file it does not read.
    owners = sorted((dashboard / "messaging_api").glob("[!_]*.py"))
    assert owners, "the messaging_api owners were not found"
    files = [dashboard / "handlers" / "messaging.py", *owners]
    held = Path(messaging.api_spawn.__code__.co_filename)
    assert held.parts[-2:] in {path.parts[-2:] for path in files}, held
    handler = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "crew_delegation_disabled" not in handler
    assert "crew_delegation_disabled" not in doc_text
    assert "delegated to by name" in doc_text


def test_the_config_fields_the_page_names_are_still_on_the_crew_record(doc_text: str) -> None:
    """The parts table names `kiro_agent` by its config field, and the retired
    `triggers` / `session_color` fields are gone from both the record and the page."""
    import dataclasses

    from kiro_crew.config.sections import KiroCrewAgentConfig

    names = {f.name for f in dataclasses.fields(KiroCrewAgentConfig)}
    assert "kiro_agent" in names
    assert "`kiro_agent`" in doc_text
    for retired in ("triggers", "session_color"):
        assert retired not in names, f"the crew record still carries the retired {retired}"
        assert f"`{retired}`" not in doc_text, f"the page still names the retired {retired}"


def test_the_self_change_tool_the_page_names_is_still_declared(doc_text: str) -> None:
    """§Editing tells the reader a crewmate renames itself with `update_self`."""
    from kiro_crew import mcp_mate

    assert "update_self" in {tool["name"] for tool in mcp_mate._list_tools()}
    assert "`update_self`" in doc_text
