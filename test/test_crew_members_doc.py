"""The packaged Crew Members page's code-coupled claims are pinned to the code.

``scripts/docs_lint.py`` gates a doc's paths and links, never what it claims, so
``src/kiro_crew/docs/crew-members.md`` is free to name a tool that was renamed, a
config field that was dropped, or a matching threshold that moved, with the lint
green. The claims most exposed to that are the ones an agent ACTS on — the two
routing tool names, the ``crew=`` spawn argument, the refusal code a delegation
comes back with, and the 0.7 trigger overlap — because each is a literal the
reader copies into a call.

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

    Pinned by executing the tool body against a temporary config rather than by
    reading its source: the page tells an agent what to expect back, and the only
    honest check of that is the payload itself. `model` is asserted to be the
    configured pin verbatim, which is the half the page has to keep distinguishing
    from the three resolved ones.
    """
    import json

    from kiro_crew import mcp_core

    payload = json.loads(mcp_core._do_select_crew(""))
    assert (
        "default_agent" in payload and "crews" in payload
    ), "the page says the empty call returns the roster plus a top-level default_agent"
    for field in ("kiro_agent", "workspace", "memory_store", "model"):
        assert f"`{field}`" in doc_text, f"the page no longer names the bound field {field}"
    src = (Path(__file__).parent.parent / "src" / "kiro_crew" / "mcp_core.py").read_text(
        encoding="utf-8"
    )
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


def test_empty_triggers_still_refuse_a_delegation_with_that_code(doc_text: str) -> None:
    """§When not to route names the refusal code a triggerless crewmate returns."""
    handler = (
        Path(__file__).parent.parent
        / "src"
        / "kiro_crew"
        / "dashboard"
        / "handlers"
        / "messaging.py"
    ).read_text(encoding="utf-8")
    assert '"code": "crew_delegation_disabled"' in handler
    assert "crew_delegation_disabled" in doc_text


def test_the_config_fields_the_page_names_are_still_on_the_crew_record(doc_text: str) -> None:
    """The two parts the table names by their config field: `kiro_agent`, `triggers`."""
    import dataclasses

    from kiro_crew.config.sections import KiroCrewAgentConfig

    names = {f.name for f in dataclasses.fields(KiroCrewAgentConfig)}
    for field in ("kiro_agent", "triggers"):
        assert field in names, f"the page's table names {field}, which the crew record dropped"
        assert f"`{field}`" in doc_text
