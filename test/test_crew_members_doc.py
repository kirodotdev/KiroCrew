"""The packaged Crew Members page's code-coupled claims are pinned to the code.

``scripts/docs_lint.py`` checks paths and links only, so these pin the literals an agent
copies into a call (routing tool names, ``crew=``, the refusal code, the 0.7 overlap)
against the code: values are imported, tool names read from the declarations.
"""

from pathlib import Path

import pytest

DOC = Path(__file__).parent.parent / "src" / "kiro_crew" / "docs" / "crew-members.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_two_routing_tools_are_still_declared_under_those_names(doc_text: str) -> None:
    """§Routing work to a crewmate tells the reader to call both tools by name."""
    from kiro_crew.mcp_tools.control import HANDLERS

    for tool in ("select_crew", "route_crew"):
        assert tool in HANDLERS, f"the page tells an agent to call {tool}, which is not dispatched"
        assert f"`{tool}" in doc_text


def test_spawn_still_takes_the_crew_argument_the_page_insists_on(doc_text: str) -> None:
    """The page's copyable call is `crew=`, and both arguments must still exist."""
    from kiro_crew.mcp_tools.spawn import schemas

    spawn = next(t for t in schemas() if t["name"] == "spawn_run")
    props = spawn["inputSchema"]["properties"]
    assert "crew" in props and "agent" in props
    assert 'spawn_run(crew="<name>")' in doc_text


def test_the_bound_block_the_page_lists_is_what_select_crew_returns(doc_text: str) -> None:
    """The page names `select_crew`'s four `bound` fields and which of them are resolved.

    Runs the tool against a temporary config and reads the payload. `model` is the
    configured pin verbatim; the other three are resolved.
    """
    import json

    from kiro_crew import mcp_core

    payload = json.loads(mcp_core._do_select_crew(""))
    assert (
        "default_agent" in payload and "crews" in payload
    ), "the page says the empty call returns the roster plus a top-level default_agent"
    for field in ("kiro_agent", "workspace", "memory_store", "model"):
        assert f"`{field}`" in doc_text, f"the page no longer names the bound field {field}"
    src = (
        Path(__file__).parent.parent / "src" / "kiro_crew" / "mcp_core_parts" / "crew.py"
    ).read_text(encoding="utf-8")
    assert '"model": cfg.agents[crew].model,' in src, (
        "the page says `model` is the configured pin verbatim; _do_select_crew no "
        "longer reads it straight off the crew record"
    )


def test_the_trigger_overlap_threshold_the_page_quotes_is_the_real_one(doc_text: str) -> None:
    """§How triggers are matched states the cut-off as a number."""
    from kiro_crew.trigger_match import MIN_TRIGGER_OVERLAP

    assert (
        f"{MIN_TRIGGER_OVERLAP}" in doc_text
    ), f"the page quotes a trigger threshold that is no longer {MIN_TRIGGER_OVERLAP}"


def test_empty_triggers_only_skip_routing_not_named_delegation(doc_text: str) -> None:
    """§When not to route says a triggerless crewmate can still be named."""
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
    """The two parts the table names by their config field: `kiro_agent`, `triggers`."""
    import dataclasses

    from kiro_crew.config.sections import KiroCrewAgentConfig

    names = {f.name for f in dataclasses.fields(KiroCrewAgentConfig)}
    for field in ("kiro_agent", "triggers"):
        assert field in names, f"the page's table names {field}, which the crew record dropped"
        assert f"`{field}`" in doc_text


def test_the_self_change_tool_the_page_names_is_still_declared(doc_text: str) -> None:
    """§Editing tells the reader a crewmate renames itself with `update_self`."""
    from kiro_crew import mcp_mate

    assert "update_self" in {tool["name"] for tool in mcp_mate._list_tools()}
    assert "`update_self`" in doc_text
